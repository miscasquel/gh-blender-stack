"""Layer B, the live path: the depsgraph is the trigger and the read.

Run with:

    scripts/test-blender

Facts this file locks, each one a live report:

1. Editing a mesh in Edit Mode must reach Grasshopper *while the mode is still
   open*. The datablock is stale until `update_from_editmode()`, so the engine
   has to flush before it reads — the "editing meshes does not sync" report.
2. An edit that is tagged but not yet evaluated must not be lost when the pass
   starts: the timer drains the depsgraph before any prefix clone can mute the
   handler — the "random, needs Push" report.
3. A reconnect (or a file load) must ask Grasshopper once instead of replaying
   the cached frame: the definition may have changed while no one was
   listening — the "needs Pull after GH restarts" report.
4. A head-of-stack stage reads the *evaluated* object, so shape keys — and
   anything else the dependency graph applies before the first modifier — are
   part of the prefix. The base-datablock read this replaced was blind to them.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

import bpy
import bmesh
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
BLENDER_PKG = os.path.join(REPO, "blender")
if BLENDER_PKG not in sys.path:
    sys.path.insert(0, BLENDER_PKG)

PORT = 9148
SCALE = "00000000-0000-4000-8000-0000000000aa"


def pump(seconds=10.0, until=None):
    """Drive the engine timer directly: bpy.app.timers is inert under -b (S2)."""
    import gh_stack.blender.engine as engine

    deadline = time.time() + seconds
    while time.time() < deadline:
        engine._timer()
        if until is not None and until():
            return True
        time.sleep(0.01)
    return until is None


def start_fake(stage_key, report):
    return subprocess.Popen(
        [
            sys.executable,
            os.path.join(REPO, "tools", "fake_gh", "fake_gh.py"),
            "--port",
            str(PORT),
            "--stage",
            stage_key,
            "--report",
            report,
        ],
        env=dict(
            os.environ,
            PYTHONPATH=os.pathsep.join(
                [os.path.join(REPO, "blender", "gh_stack", "vendor"), REPO]
            ),
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def stop_fake(process):
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


def report_of(path):
    if not os.path.exists(path):
        return {}
    try:
        return json.load(open(path))
    except ValueError:
        return {}


def isolate_data_root():
    prefs = bpy.context.preferences.addons["gh_stack"].preferences
    prefs.data_root = tempfile.mkdtemp(prefix="gh-stack-live-")


def first_x(report):
    first = report.get("received_first") or [None]
    return first[0]


def helper_max(obj, stage_key):
    """The largest absolute coordinate in a stage's mesh helper: what actually
    landed on screen (the fake scales the stored geometry by the parameter)."""
    import gh_stack.blender.helpers as helpers

    helper = helpers.helper_for(obj, stage_key, "mesh")
    if helper is None or not len(helper.data.vertices):
        return 0.0
    count = len(helper.data.vertices)
    co = np.empty(count * 3, dtype=np.float32)
    helper.data.vertices.foreach_get("co", co)
    return float(np.abs(co.reshape(-1, 3)).max())


class LiveSyncTestCase(unittest.TestCase):
    """A cube, one stage, one fake client on a private port."""

    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="gh_stack")
        isolate_data_root()
        import gh_stack.blender.engine as engine
        import gh_stack.blender.stack as stack_mod

        cls.engine = engine
        cls.stack_mod = stack_mod
        engine.start_server(PORT)
        time.sleep(0.5)

        bpy.ops.mesh.primitive_cube_add(size=2.0)
        cls.obj = bpy.context.object
        cls.obj.name = "Live"
        bpy.ops.gh_stack.add_stage(label="Live")
        cls.stage = cls.obj.gh_stack.stages[0]
        cls.obj[stack_mod.param_prop_name(cls.stage.key, SCALE)] = 1.0

        cls.report = os.path.join(tempfile.gettempdir(), "fake_gh_live_%s.json" % cls.__name__)
        if os.path.exists(cls.report):
            os.remove(cls.report)
        cls.fake = start_fake(cls.stage.key, cls.report)

        def settled():
            return cls.stage.state == "ok" and report_of(cls.report).get("responses", 0) >= 1

        pump(seconds=15.0, until=settled)

    @classmethod
    def tearDownClass(cls):
        stop_fake(cls.fake)
        bpy.ops.preferences.addon_disable(module="gh_stack")

    def edit_mode(self):
        bpy.context.view_layer.objects.active = self.obj
        self.obj.select_set(True)
        bpy.ops.object.mode_set(mode="EDIT")

    def leave_edit_mode(self):
        if self.obj.mode == "EDIT":
            bpy.ops.object.mode_set(mode="OBJECT")


class EditModeTests(LiveSyncTestCase):
    def tearDown(self):
        self.leave_edit_mode()

    def test_01_an_edit_mode_stroke_reaches_grasshopper(self):
        self.assertEqual(self.stage.state, "ok", self.stage.error)
        # The cube's first vertex starts at x = -1.
        before = first_x(report_of(self.report))
        self.assertIsNotNone(before)

        self.edit_mode()
        bm = bmesh.from_edit_mesh(self.obj.data)
        bm.verts.ensure_lookup_table()
        bm.verts[0].co.x += 5.0
        bmesh.update_edit_mesh(self.obj.data)

        # No mode exit, no manual tag: the timer drains, flushes and sends.
        reached = pump(
            seconds=15.0,
            until=lambda: first_x(report_of(self.report)) is not None
            and abs(first_x(report_of(self.report)) - (before + 5.0)) < 0.05,
        )
        self.assertTrue(
            reached,
            "an Edit Mode stroke never reached Grasshopper (report=%s)" % report_of(self.report),
        )
        self.assertEqual(self.obj.mode, "EDIT", "the test left Edit Mode; the point was to stay in it")
        self.assertEqual(self.stage.state, "ok", self.stage.error)


class PendingUpdateTests(LiveSyncTestCase):
    def test_01_a_tagged_edit_is_drained_before_the_pass(self):
        # The write tags the depsgraph; nothing has evaluated yet (no
        # `view_layer.update()` here). One timer pass must deliver it to the
        # handler and send — a clone earlier in the pass would otherwise mute
        # the handler and lose the edit.
        sends = self.engine.stats["sends"]
        self.obj.data.vertices[0].co.x += 1.0
        self.engine._timer()
        self.assertGreater(
            self.engine.stats["sends"],
            sends,
            "a pending depsgraph update was swallowed by the pass",
        )
        self.assertTrue(
            pump(seconds=15.0, until=lambda: abs(first_x(report_of(self.report)) - 0.0) < 0.05),
            "the drained edit never reached Grasshopper (report=%s)" % report_of(self.report),
        )


class ReconnectVerifyTests(LiveSyncTestCase):
    def test_01_a_reconnect_asks_instead_of_replaying(self):
        # Settle so the current rid is cached in memory and on disk.
        pump(until=lambda: self.stage.state == "ok")
        record = self.stack_mod.stack_of(self.obj)
        cached_frame = self.engine._cached_response(record, self.stage, self.stage.rid)
        self.assertIsNotNone(cached_frame, "the frame was not cached; the test proves nothing")

        sends = self.engine.stats["sends"]
        self.engine._on_client_hello(
            {"doc": "verify.gh", "stages": [{"key": self.stage.key}]}
        )
        self.assertTrue(
            pump(seconds=15.0, until=lambda: self.engine.stats["sends"] > sends),
            "a reconnect replayed the cached frame instead of asking",
        )
        self.assertTrue(
            pump(seconds=15.0, until=lambda: self.stage.state == "ok"),
            "the verification request was never answered (report=%s)" % report_of(self.report),
        )
        self.assertEqual(self.stage.state, "ok", self.stage.error)


class EvaluatedHeadTests(LiveSyncTestCase):
    """No fake, no timer: the prefix read itself must be the evaluated object."""

    def test_01_shape_keys_are_part_of_a_head_of_stack_prefix(self):
        import gh_stack.blender.engine as engine

        key = self.obj.shape_key_add(name="Basis")
        self.assertIsNotNone(key)
        moved = self.obj.shape_key_add(name="Moved")
        moved.data[0].co.x += 5.0
        moved.value = 1.0
        bpy.context.view_layer.update()
        engine._forget_prefix(self.obj, self.stage.key)

        record = self.stack_mod.stack_of(self.obj)
        sections, _hash, _attributes, _definitions = engine._prefix_sections(
            self.obj, record, self.stage
        )
        verts = dict((name, values) for name, _dtype, values in sections).get("mesh.verts")
        self.assertIsNotNone(verts, "the prefix carried no mesh.verts")
        x = float(np.asarray(verts).reshape(-1, 3)[0][0])
        self.assertAlmostEqual(
            x, 4.0, places=2,
            msg="a head-of-stack prefix read the base mesh, not the evaluated shape key (x=%r)" % x,
        )
