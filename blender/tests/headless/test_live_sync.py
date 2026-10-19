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
5. A raise between setting the clone guard and clearing it used to mute the
   engine for the rest of the session (every update dropped, every edit
   ignored). The watchdog must clear it, explain it, and process the missed
   edits — the F2 wedge.
6. When a pass that saw a changed scene sends nothing, the reason must be in
   the trace (frozen, waiting on a rid, cache hit, disconnected…), once per
   decision instead of once per tick — silence made explainable.
7. Trigger health and per-stage timings are visible in Diagnostics, so "the
   engine never heard Blender" is distinguishable from "the engine stayed
   quiet", and a slow round trip names its slowest part.
8. The clone guard is reset in a nested `finally`, so no cleanup failure can
   leave the engine mute; the watchdog remains as belt-and-braces.
9. One bad incoming message does not eat the rest of the batch, and a response
   whose apply failed clears its pending entry and poisoned frame so the next
   pass retries instead of replaying the failure.
10. A lost answer is still nudged after the object is renamed: the stall scan
    walks the scene, not a set of names captured at send time.
11. A wedged guard does not survive `register()` or a file load.
12. Every object in Edit Mode is flushed, not only the active one: a stroke on
    a non-active stack object must reach Grasshopper while the mode is open, and
    Push must copy the target's edit before it builds the request (F1).
13. A slow answer must not swallow the edits behind it: after the soft retry the
    current state supersedes the stale request, and the bookkeeping around an
    error or an `unknown_ref` releases its gates instead of waiting out the
    timeout (F3).
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


def start_fake(stage_key, report, port=PORT, *extra):
    return subprocess.Popen(
        [
            sys.executable,
            os.path.join(REPO, "tools", "fake_gh", "fake_gh.py"),
            "--port",
            str(port),
            "--stage",
            stage_key,
            "--report",
            report,
            *extra,
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

        # A class transition can leave the server holding a socket whose process
        # is already gone (the window the frame-cache suite's `await_client`
        # documents): the first request is written there and lost, and the wait
        # below would spend its budget on an answer that was never asked for.
        # Re-arm once with a fresh request; a genuinely broken client still
        # fails the assertions below, with its own message.
        if not pump(seconds=8.0, until=settled):
            engine.request_again(cls.obj)
            pump(seconds=8.0, until=settled)

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


class MultiObjectEditTests(unittest.TestCase):
    """F1: every object in Edit Mode must be flushed, not only the active one.

    Blender supports multi-object editing: `bpy.context.edit_object` is only the
    *active* edit object, and every other selected object carries its own BMesh
    edits next to it. The old flush copied one object per tick, so a stroke on a
    non-active stack object never changed the datablock the prefix clone read:
    the geometry hash stayed put and the pass replayed the cached frame. A
    non-stack object being active had the same effect one step earlier — the
    flush early-returned before it looked at any other edit object.

    Two stack objects (A, B) and one plain object (C) live in the scene; a
    stroke lands on B, the object that is *not* active. The fake for B's stage
    is the witness: it must receive the changed coordinate while the mode is
    still open.
    """

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

        cls.obj_a = cls._stack_object("MultiA", "A")
        cls.stage_a = cls.obj_a.gh_stack.stages[0]
        cls.obj_b = cls._stack_object("MultiB", "B")
        cls.stage_b = cls.obj_b.gh_stack.stages[0]

        bpy.ops.mesh.primitive_cube_add(size=2.0)
        cls.obj_c = bpy.context.object
        cls.obj_c.name = "PlainC"

        cls.report_a = os.path.join(tempfile.gettempdir(), "fake_gh_multi_a.json")
        cls.report_b = os.path.join(tempfile.gettempdir(), "fake_gh_multi_b.json")
        for path in (cls.report_a, cls.report_b):
            if os.path.exists(path):
                os.remove(path)
        # Both stages share the port: each fake answers only its own stage.
        cls.fake_a = start_fake(cls.stage_a.key, cls.report_a)
        cls.fake_b = start_fake(cls.stage_b.key, cls.report_b)

        def settled():
            return (
                cls.stage_a.state == "ok"
                and cls.stage_b.state == "ok"
                and report_of(cls.report_a).get("responses", 0) >= 1
                and report_of(cls.report_b).get("responses", 0) >= 1
            )

        # The same transition window the single-object suite documents: a
        # socket from the previous class's process can swallow the first
        # request, so re-arm once before asserting.
        if not pump(seconds=8.0, until=settled):
            engine.request_again(cls.obj_a)
            engine.request_again(cls.obj_b)
            pump(seconds=8.0, until=settled)

    @classmethod
    def _stack_object(cls, name, label):
        bpy.ops.mesh.primitive_cube_add(size=2.0)
        obj = bpy.context.object
        obj.name = name
        bpy.ops.gh_stack.add_stage(label=label)
        return obj

    @classmethod
    def tearDownClass(cls):
        stop_fake(cls.fake_a)
        stop_fake(cls.fake_b)
        bpy.ops.preferences.addon_disable(module="gh_stack")

    def tearDown(self):
        self.leave_edit_mode()

    def enter_edit(self, objects, active):
        bpy.ops.object.select_all(action="DESELECT")
        for obj in objects:
            obj.select_set(True)
        bpy.context.view_layer.objects.active = active
        bpy.ops.object.mode_set(mode="EDIT")

    def leave_edit_mode(self):
        if bpy.context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")

    def settle(self):
        """Let the entry tag and every answer land, so a test measures only the
        stroke it makes: both stages at rest and nothing marked dirty."""
        return pump(
            seconds=15.0,
            until=lambda: not self.engine._dirty
            and self.stage_a.state == "ok"
            and self.stage_b.state == "ok",
        )

    def stroke(self, obj, delta):
        """Move the first vertex of `obj` by `delta` in x, in the BMesh.

        The datablock is deliberately not touched: only `update_from_editmode`
        may copy the edit into it, which is exactly what the engine has to do.
        """
        bm = bmesh.from_edit_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        bm.verts[0].co.x += delta
        bmesh.update_edit_mesh(obj.data)

    def flush_lines(self):
        return [
            line
            for line in self.engine.recent_activity(200)
            if line.startswith("flushed Edit Mode data")
        ]

    def test_01_a_stroke_on_the_non_active_edit_object_reaches_gh(self):
        self.enter_edit([self.obj_a, self.obj_b], active=self.obj_a)
        self.assertTrue(self.settle(), "entering Edit Mode never settled")
        before = first_x(report_of(self.report_b))
        self.assertIsNotNone(before)
        sends = self.engine.stats["sends"]

        self.stroke(self.obj_b, 5.0)

        self.assertTrue(
            pump(
                seconds=15.0,
                until=lambda: first_x(report_of(self.report_b)) is not None
                and abs(first_x(report_of(self.report_b)) - (before + 5.0)) < 0.05,
            ),
            "a stroke on the non-active Edit Mode object never reached Grasshopper "
            "(report=%s)" % report_of(self.report_b),
        )
        self.assertGreater(self.engine.stats["sends"], sends)
        self.assertTrue(
            any(
                "flushed Edit Mode data for %s" % self.obj_b.name in line
                for line in self.flush_lines()
            ),
            "the non-active object carried no flush line: %s" % self.flush_lines(),
        )
        self.assertTrue(
            pump(seconds=15.0, until=lambda: self.stage_b.state == "ok"),
            "the stage never settled (state=%s, error=%s)"
            % (self.stage_b.state, self.stage_b.error),
        )
        self.assertEqual(self.obj_b.mode, "EDIT", "the test left Edit Mode; the point was to stay in it")
        self.assertEqual(self.stage_b.state, "ok", self.stage_b.error)

    def test_02_a_stack_object_behind_a_non_stack_active_is_flushed(self):
        self.enter_edit([self.obj_b, self.obj_c], active=self.obj_c)
        self.assertTrue(self.settle(), "entering Edit Mode never settled")
        before = first_x(report_of(self.report_b))
        self.assertIsNotNone(before)

        self.stroke(self.obj_b, 5.0)

        self.assertTrue(
            pump(
                seconds=15.0,
                until=lambda: first_x(report_of(self.report_b)) is not None
                and abs(first_x(report_of(self.report_b)) - (before + 5.0)) < 0.05,
            ),
            "a stack object in Edit Mode was ignored because the active object "
            "has no stack (report=%s)" % report_of(self.report_b),
        )
        self.assertEqual(self.obj_c.mode, "EDIT", "the test left Edit Mode; the point was to stay in it")

    def test_03_push_flushes_the_target_before_it_builds_the_request(self):
        self.enter_edit([self.obj_a, self.obj_b], active=self.obj_a)
        self.assertTrue(self.settle(), "entering Edit Mode never settled")
        report = report_of(self.report_b)
        before = first_x(report)
        self.assertIsNotNone(before)
        full_before = report.get("full", 0)

        self.stroke(self.obj_b, 3.0)
        # Deliberately no pump here: Push itself must copy the edit into the
        # datablock, or the request goes out as a `ref` with the stale hash and
        # Grasshopper never hears about the stroke.
        self.engine.push(self.obj_b)

        self.assertTrue(
            pump(
                seconds=15.0,
                until=lambda: first_x(report_of(self.report_b)) is not None
                and abs(first_x(report_of(self.report_b)) - (before + 3.0)) < 0.05,
            ),
            "Push sent a ref with the stale hash instead of the edited geometry "
            "(report=%s)" % report_of(self.report_b),
        )
        self.assertGreaterEqual(
            report_of(self.report_b).get("full", 0),
            full_before + 1,
            "Push did not send the geometry in full (report=%s)" % report_of(self.report_b),
        )
        self.assertEqual(self.obj_b.mode, "EDIT", "the test left Edit Mode; the point was to stay in it")

    def test_04_an_idle_edit_mode_session_does_not_flush(self):
        self.enter_edit([self.obj_a, self.obj_b], active=self.obj_a)
        self.assertTrue(self.settle(), "entering Edit Mode never settled")

        before = len(self.flush_lines())
        for _ in range(10):
            self.engine._timer()
            time.sleep(0.01)

        self.assertEqual(
            len(self.flush_lines()),
            before,
            "an idle Edit Mode session kept copying meshes: %s" % self.flush_lines(),
        )
        self.assertFalse(self.engine._dirty, "an idle pass left objects dirty")


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


class GuardWatchdogTests(LiveSyncTestCase):
    """F2: a guard left up by a dead pass must not mute the engine.

    Since the cleanup reset cannot be skipped, the watchdog's input is injected
    directly: any future leak must still be noticed, explained, cleared, and
    the edit dropped while it was up must still be sent. The second test drives
    the cleanup fault that used to wedge the guard and asserts it no longer can
    — the reset happens before any watchdog can see it.
    """

    def tearDown(self):
        self.leave_edit_mode()

    def test_01_a_stuck_guard_is_cleared_and_the_edit_still_sends(self):
        engine = self.engine
        self.assertEqual(self.stage.state, "ok", self.stage.error)
        sends = engine.stats["sends"]

        # A guard a dead pass left up. No tick is running, so this is the wedge.
        engine._evaluating = True
        engine._guard_owner = (self.obj.name, self.stage.key)
        engine._evaluating_since = time.monotonic()

        # An edit made while the guard is up: the handler must drop it (that is
        # the mute) and say so in the trace.
        epoch = engine._prefix_epoch
        self.obj.data.vertices[0].co.x += 2.0
        bpy.context.view_layer.update()
        self.assertEqual(engine._prefix_epoch, epoch, "a dropped update still moved the epoch")
        self.assertTrue(
            any("guard up" in line for line in engine.recent_activity(50)),
            "the skipped update left no trace: %s" % engine.recent_activity(50),
        )

        # One tick must notice the guard, clear it, explain it, and pick up the
        # edit that was dropped.
        engine._timer()
        self.assertFalse(engine._evaluating, "the watchdog did not clear the guard")
        watch = [line for line in engine.recent_activity(50) if "guard watchdog" in line]
        self.assertEqual(len(watch), 1, "no single watchdog line: %s" % engine.recent_activity(50))
        self.assertIn(self.obj.name, watch[0])
        self.assertIn(self.stage.key, watch[0])
        self.assertGreater(
            engine.stats["sends"],
            sends,
            "the dropped edit was not sent after the watchdog cleared the guard",
        )
        self.assertTrue(
            pump(seconds=15.0, until=lambda: self.stage.state == "ok"),
            "the recovered stage never settled (state=%s, error=%s)"
            % (self.stage.state, self.stage.error),
        )

    def test_02_a_cleanup_raise_clears_the_guard_without_the_watchdog(self):
        import gh_stack.blender.bridge as bridge

        engine = self.engine
        self.assertEqual(self.stage.state, "ok", self.stage.error)
        self.edit_mode()
        watchdog_lines = sum(
            "guard watchdog" in line for line in engine.recent_activity(200)
        )

        real_drop = engine._drop_owned_data

        def failing_drop(id_block):
            raise RuntimeError("test: cleanup fault")

        engine._drop_owned_data = failing_drop
        try:
            bm = bmesh.from_edit_mesh(self.obj.data)
            bm.verts.ensure_lookup_table()
            bm.verts[0].co.x += 4.0
            bmesh.update_edit_mesh(self.obj.data)
            # One pass, driven directly: the cleanup fault must fail the pass
            # but the guard must already be down when `_timer` returns.
            engine._mark_object_dirty(self.obj)
            engine._timer()
        finally:
            engine._drop_owned_data = real_drop

        self.assertFalse(
            engine._evaluating, "a cleanup raise left the prefix guard up"
        )
        self.assertEqual(
            sum("guard watchdog" in line for line in engine.recent_activity(200)),
            watchdog_lines,
            "the watchdog, not the nested finally, cleared the guard",
        )
        self.assertIn(
            "cleanup fault",
            bridge.state["error"],
            "the cleanup failure was not recorded: %r" % (bridge.state["error"],),
        )

        # The next edit is processed and reaches Grasshopper: the earlier +4
        # stroke was never sent, so the fake must see the whole current mesh.
        sends = engine.stats["sends"]
        bm = bmesh.from_edit_mesh(self.obj.data)
        bm.verts.ensure_lookup_table()
        bm.verts[0].co.x += 1.0
        bmesh.update_edit_mesh(self.obj.data)
        bpy.context.view_layer.update()
        self.assertTrue(
            pump(seconds=15.0, until=lambda: engine.stats["sends"] > sends),
            "the first edit after a cleanup failure was not sent",
        )
        # The pump's pass flushed Edit Mode into the datablock, so the expected
        # value is readable here (the class's earlier tests moved the vertex
        # too; an absolute would depend on test order).
        expected = float(self.obj.data.vertices[0].co.x)
        self.assertTrue(
            pump(
                seconds=15.0,
                until=lambda: first_x(report_of(self.report)) is not None
                and abs(first_x(report_of(self.report)) - expected) < 0.05,
            ),
            "the edit after recovery never reached Grasshopper (report=%s)"
            % report_of(self.report),
        )
        self.assertEqual(self.obj.mode, "EDIT", "the test left Edit Mode; the point was to stay in it")


class BatchIsolationTests(LiveSyncTestCase):
    """F4: a bad frame must not eat the messages behind it.

    `_handle_incoming` drains the queue into a list, so without a per-message
    guard a raise in `_apply` aborted the pass. `_handle_response` also stores
    the payload and forgets the pending entry *before* applying, so the old
    failure left the stage `waiting` with nothing outstanding and replayed the
    poisoned frame on every later pass. Now the failing message is reported,
    the poisoned frame is dropped, the stage is dirty, and the next message in
    the same batch is still handled.
    """

    def test_01_one_bad_response_does_not_eat_the_batch(self):
        import gh_stack.blender.bridge as bridge
        import gh_stack.blender.server as server

        engine = self.engine
        self.assertEqual(self.stage.state, "ok", self.stage.error)
        record = self.stack_mod.stack_of(self.obj)

        # Put the stage in flight without letting the real client answer, so
        # the rid belongs to a wait this test controls.
        real_send = server.SERVER.send
        server.SERVER.send = lambda frame: True
        try:
            engine.request_again(self.obj)
            engine._timer()
        finally:
            server.SERVER.send = real_send
        self.assertEqual(self.stage.state, "waiting", self.stage.error)
        pending = engine._pending_of(record.stack_id, self.stage.key)
        self.assertTrue(pending, "the swallowed send recorded no pending request")
        rid = next(iter(pending))

        sections = {
            "mesh.verts": np.array(
                [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], dtype=np.float32
            ),
            "mesh.face_counts": np.array([3], dtype=np.uint32),
            "mesh.face_indices": np.array([0, 1, 2], dtype=np.uint32),
        }
        response = {
            "type": "response",
            "stage": self.stage.key,
            "rid": rid,
            "out_hash": "test-poisoned-frame",
            "status": "ok",
            "error": None,
            "warnings": [],
        }

        real_apply = engine._apply
        calls = {"n": 0}

        def failing_apply(obj, stage, payload, attributes=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("test: apply fault")
            return real_apply(obj, stage, payload, attributes)

        engine._apply = failing_apply
        try:
            bridge.post_incoming(response, sections)
            # The second message of the same batch must still be handled.
            bridge.post_incoming(
                {"type": "hello", "doc": "batch.gh", "stages": [{"key": self.stage.key}]},
                {},
            )
            engine._handle_incoming()
        finally:
            engine._apply = real_apply

        self.assertEqual(calls["n"], 1, "the poisoned frame was applied twice")
        self.assertIn(
            "apply fault",
            bridge.state["error"],
            "the failed message was not recorded: %r" % (bridge.state["error"],),
        )
        self.assertTrue(
            any("client hello" in line for line in engine.recent_activity(50)),
            "the message after the failure was dropped: %s" % engine.recent_activity(50),
        )
        self.assertFalse(
            engine._pending_of(record.stack_id, self.stage.key),
            "the stage was left waiting with an empty pending map",
        )
        self.assertNotEqual(
            self.stage.state,
            self.stack_mod.STATE_WAITING,
            "the failed response left the stage in waiting",
        )
        self.assertIn(self.obj.name, engine._dirty, "the failed stage was not marked dirty")
        self.assertIsNone(
            engine._cached_response(record, self.stage, rid),
            "the frame whose apply failed was kept and would replay the failure",
        )

        # The next pass asks again, and the real client answers.
        self.assertTrue(
            pump(seconds=15.0, until=lambda: self.stage.state == "ok"),
            "the stage never recovered after the apply failure (state=%s, error=%s)"
            % (self.stage.state, self.stage.error),
        )
        self.assertGreaterEqual(engine.stats["applies"], 1)


class StallRenameTests(LiveSyncTestCase):
    """A rename between send and stall check must not hide a lost answer.

    The old stall set was keyed by the name recorded at send time; renaming the
    object made `bpy.data.objects.get(old_name)` fail, the name was discarded,
    and the lost request never got its retry nudge until the next send. The
    scan now walks the scene's stack objects and their pending entries.
    """

    def test_01_a_renamed_object_still_gets_its_stall_nudge(self):
        import gh_stack.blender.server as server

        engine = self.engine
        prefs = bpy.context.preferences.addons["gh_stack"].preferences
        original_name = self.obj.name
        original_timeout = prefs.timeout
        real_send = server.SERVER.send
        try:
            self.assertEqual(self.stage.state, "ok", self.stage.error)
            record = self.stack_mod.stack_of(self.obj)

            # A request whose answer never comes: swallow the socket write.
            server.SERVER.send = lambda frame: True
            engine.request_again(self.obj)
            engine._timer()
            self.assertEqual(self.stage.state, "waiting", self.stage.error)
            pending = engine._pending_of(record.stack_id, self.stage.key)
            self.assertTrue(pending, "the swallowed send recorded no pending request")

            # Rename after the send: a name captured then is now stale.
            self.obj.name = original_name + "Renamed"
            self.assertIn("Renamed", self.obj.name)

            prefs.timeout = 1.0
            for key in list(pending):
                pending[key] = time.monotonic() - 5.0
            engine._dirty.discard(self.obj.name)
            engine._last_stall_check = 0.0
            engine._nudge_stalled_waits()
            self.assertIn(
                self.obj.name,
                engine._dirty,
                "the renamed object missed the stall nudge",
            )

            # Restore the socket and recover: the pass must not leave the stage
            # waiting on a request nobody can answer. (The rename also makes
            # the pass rebuild its plumbing, which forgets the wait itself —
            # either way, the stage settles.)
            server.SERVER.send = real_send
            self.assertTrue(
                pump(seconds=15.0, until=lambda: self.stage.state == "ok"),
                "the stage did not recover after the rename and nudge (state=%s, error=%s)"
                % (self.stage.state, self.stage.error),
            )

            # A lost answer under the *new* name, no rename this time: the nudge
            # must reach `_process_stage`'s timeout, which resends in full
            # (the timeout cannot know what GH still holds).
            record = self.stack_mod.stack_of(self.obj)
            server.SERVER.send = lambda frame: True
            engine.request_again(self.obj)
            engine._timer()
            self.assertEqual(self.stage.state, "waiting", self.stage.error)
            pending = engine._pending_of(record.stack_id, self.stage.key)
            self.assertTrue(pending, "the swallowed send recorded no pending request")
            for key in list(pending):
                pending[key] = time.monotonic() - 5.0
            engine._dirty.discard(self.obj.name)
            engine._last_stall_check = 0.0
            engine._nudge_stalled_waits()
            self.assertIn(self.obj.name, engine._dirty)

            server.SERVER.send = real_send
            sends = engine.stats["sends"]
            self.assertTrue(
                pump(seconds=15.0, until=lambda: engine.stats["sends"] > sends),
                "the timed-out stage did not retry",
            )
            self.assertTrue(
                pump(seconds=15.0, until=lambda: self.stage.state == "ok"),
                "the full retry never landed (state=%s, error=%s)"
                % (self.stage.state, self.stage.error),
            )
            self.assertGreaterEqual(
                report_of(self.report).get("full", 0),
                2,
                "the timeout retry did not resend the geometry in full",
            )
        finally:
            server.SERVER.send = real_send
            prefs.timeout = original_timeout
            if self.obj.name != original_name:
                self.obj.name = original_name



class WhyNoSendTests(LiveSyncTestCase):
    """A pass that saw the scene move and sent nothing must say why.

    Two decisions are covered: freeze (a state flag) and an outstanding request
    (a state each new edit has to wait behind). Both must be recorded once per
    decision — a line per tick would bury the rest of the ring.
    """

    def settle(self):
        """Unfreeze, force a request and wait for the answer: each test starts
        from a restful stage whether or not the one before it left a decision
        recorded."""
        self.stage.frozen = False
        self.engine.request_again(self.obj)
        self.assertTrue(
            pump(seconds=15.0, until=lambda: self.stage.state == "ok"),
            "the stage never settled (state=%s, error=%s)" % (self.stage.state, self.stage.error),
        )

    def lines(self, needle):
        """This stage's trace lines only: the ring survives across classes."""
        return [
            line
            for line in self.engine.recent_activity(200)
            if needle in line and self.stage.key in line
        ]

    def test_01_a_frozen_stage_explains_the_silence_once(self):
        engine = self.engine
        self.assertEqual(self.stage.state, "ok", self.stage.error)
        self.stage.frozen = True
        try:
            engine._mark_object_dirty(self.obj)
            engine._timer()
            self.assertEqual(
                len(self.lines("no send (frozen)")),
                1,
                "no frozen explanation: %s" % engine.recent_activity(50),
            )
            engine._mark_object_dirty(self.obj)
            engine._timer()
            self.assertEqual(
                len(self.lines("no send (frozen)")),
                1,
                "the frozen explanation repeated per tick: %s" % engine.recent_activity(50),
            )
        finally:
            self.stage.frozen = False

        # A send re-arms the explanation: freezing again is a new decision.
        sends = engine.stats["sends"]
        engine.request_again(self.obj)
        self.assertTrue(
            pump(seconds=15.0, until=lambda: engine.stats["sends"] > sends),
            "an unfrozen stage did not send",
        )
        self.stage.frozen = True
        try:
            engine._mark_object_dirty(self.obj)
            engine._timer()
            self.assertEqual(
                len(self.lines("no send (frozen)")),
                2,
                "the second freeze left no explanation: %s" % engine.recent_activity(50),
            )
        finally:
            self.stage.frozen = False

    def test_02_a_pending_request_explains_each_new_state_once(self):
        import gh_stack.blender.server as server

        engine = self.engine
        self.settle()
        name = self.stack_mod.param_prop_name(self.stage.key, SCALE)

        # Swallow the request instead of sending it: the stage now believes an
        # answer is in flight, which is the state the live loop waits in while
        # Grasshopper solves.
        real_send = server.SERVER.send
        server.SERVER.send = lambda frame: True
        try:
            engine.request_again(self.obj)
            engine._timer()
            self.assertEqual(self.stage.state, "waiting", self.stage.error)
            self.assertEqual(
                self.lines("no send (waiting on rid"),
                [],
                "a sent request was reported as a silence",
            )

            # A new scene state (a different request id) behind the outstanding
            # request: explained once, not once per tick.
            self.obj[name] = 2.0
            engine._mark_object_dirty(self.obj)
            engine._timer()
            self.assertEqual(len(self.lines("no send (waiting on rid")), 1, self.lines("no send"))
            engine._mark_object_dirty(self.obj)
            engine._timer()
            self.assertEqual(
                len(self.lines("no send (waiting on rid")),
                1,
                "the wait repeated per tick: %s" % self.lines("no send"),
            )

            # The state moves on again: the rid in the token changed, so the
            # silence is a new decision and is explained again.
            self.obj[name] = 3.0
            engine._mark_object_dirty(self.obj)
            engine._timer()
            self.assertEqual(len(self.lines("no send (waiting on rid")), 2, self.lines("no send"))
        finally:
            server.SERVER.send = real_send

        # Recover: forget the swallowed wait and let the real client answer.
        engine.request_again(self.obj)
        self.assertTrue(
            pump(seconds=15.0, until=lambda: self.stage.state == "ok"),
            "the swallowed wait never recovered (state=%s, error=%s)"
            % (self.stage.state, self.stage.error),
        )
        self.assertTrue(
            pump(
                seconds=15.0,
                until=lambda: abs(helper_max(self.obj, self.stage.key) - 3.0) < 0.01,
            ),
            "the recovered value never applied",
        )


class SessionResetTests(LiveSyncTestCase):
    """A wedged guard must not survive a new session (F2).

    The guard used to outlive disable/enable and a file reload: the add-on came
    back mute and dropped every edit until the watchdog's first tick. Both entry
    points now clear the whole session state, including the tick flag.

    No socket work is needed here, so `start_server` is stubbed: `register()`
    would otherwise stop the class's server (it follows the port preference,
    not the test port) and strand the fake client.
    """

    def test_01_register_clears_a_wedged_guard(self):
        engine = self.engine
        real_start = engine.start_server
        engine.unregister()
        engine.start_server = lambda port=None: False
        try:
            engine._evaluating = True
            engine._guard_owner = ("Wedged", self.stage.key)
            engine._evaluating_since = time.monotonic()
            engine._ticking = True

            engine.register()
            self.assertFalse(engine._evaluating, "register() kept a wedged prefix guard")
            self.assertIsNone(engine._guard_owner)
            self.assertIsNone(engine._evaluating_since)
            self.assertFalse(engine._ticking, "register() kept the previous session's tick flag")

            # A retry of register() (a partial failure is the reason it can run
            # again) must not append the same handler twice.
            handlers = len(bpy.app.handlers.depsgraph_update_post)
            engine._registered = False
            engine.register()
            self.assertEqual(
                len(bpy.app.handlers.depsgraph_update_post),
                handlers,
                "a second register() duplicated the depsgraph handler",
            )
        finally:
            engine.start_server = real_start
            if not engine._registered:
                engine.register()

    def test_02_a_file_load_clears_a_wedged_guard(self):
        engine = self.engine
        engine._evaluating = True
        engine._guard_owner = ("Wedged", self.stage.key)
        engine._evaluating_since = time.monotonic()
        engine._ticking = True

        engine._on_load()
        self.assertFalse(engine._evaluating, "_on_load() kept a wedged prefix guard")
        self.assertIsNone(engine._guard_owner)
        self.assertIsNone(engine._evaluating_since)
        self.assertFalse(engine._ticking, "_on_load() kept the previous session's tick flag")


class DiagnosticsTimingTests(LiveSyncTestCase):
    """The Diagnostics numbers Phase 0 adds: trigger health and stage timings."""

    def test_01_a_round_trip_leaves_timings_and_a_heartbeat(self):
        engine = self.engine
        record = self.stack_mod.stack_of(self.obj)
        timings = engine.stage_timings(record.stack_id, self.stage.key)
        for field in ("prefix_ms", "encode_ms", "rtt_ms", "apply_ms"):
            self.assertIsNotNone(
                timings.get(field), "%s missing after a round trip: %s" % (field, timings)
            )
            self.assertGreaterEqual(timings[field], 0.0)
            self.assertLess(timings[field], 60000.0)
        # The helper hands out a copy: the panel can never write engine state.
        timings["rtt_ms"] = -1.0
        self.assertNotEqual(
            engine.stage_timings(record.stack_id, self.stage.key)["rtt_ms"],
            -1.0,
            "stage_timings handed out the engine's live dict",
        )

        health = engine.trigger_health()
        self.assertIsNotNone(health["trigger_age"], "no depsgraph update was ever heard")
        self.assertIsNotNone(health["send_age"])
        self.assertIsNotNone(health["apply_age"])
        self.assertGreaterEqual(health["trigger_count"], 1)
        self.assertGreaterEqual(health["trigger_age"], 0.0)


class SupersedeTests(unittest.TestCase):
    """F3: a slow answer must not swallow the edits that follow it.

    While one request was outstanding every uncached edit waited for the answer
    (or the 30 s timeout), so a stranded request looked like a dead add-on.
    After SOFT_RETRY_SECONDS a pass supersedes the stale request with the
    current state; the slow fake's echo proves the newest value is what gets
    answered and applied.
    """

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
        cls.obj.name = "Slow"
        bpy.ops.gh_stack.add_stage(label="Slow")
        cls.stage = cls.obj.gh_stack.stages[0]
        cls.obj[stack_mod.param_prop_name(cls.stage.key, SCALE)] = 1.0

        cls.report = os.path.join(tempfile.gettempdir(), "fake_gh_live_supersede.json")
        if os.path.exists(cls.report):
            os.remove(cls.report)
        cls.fake = start_fake(cls.stage.key, cls.report, PORT, "--delay-ms", "3000")

        def settled():
            return cls.stage.state == "ok" and report_of(cls.report).get("responses", 0) >= 1

        pump(seconds=20.0, until=settled)

    @classmethod
    def tearDownClass(cls):
        stop_fake(cls.fake)
        bpy.ops.preferences.addon_disable(module="gh_stack")

    def test_01_a_new_value_supersedes_a_slow_request(self):
        engine = self.engine
        name = self.stack_mod.param_prop_name(self.stage.key, SCALE)
        self.obj[name] = 2.0
        engine._mark_object_dirty(self.obj)
        self.assertTrue(
            pump(seconds=5.0, until=lambda: self.stage.state == "waiting"),
            "the slow fake never left a request outstanding",
        )
        sends = engine.stats["sends"]
        pump(seconds=0.4)
        self.assertEqual(
            engine.stats["sends"], sends, "a request went out before the soft retry"
        )
        self.obj[name] = 5.0
        engine._mark_object_dirty(self.obj)
        self.assertTrue(
            pump(seconds=6.0, until=lambda: engine.stats["sends"] > sends),
            "the newer value never superseded the in-flight request",
        )
        self.assertTrue(
            pump(
                seconds=20.0,
                until=lambda: abs(helper_max(self.obj, self.stage.key) - 5.0) < 0.01,
            ),
            "the newest value never applied (helper max %r)"
            % helper_max(self.obj, self.stage.key),
        )
        self.assertEqual(self.stage.state, "ok", self.stage.error)


class SelfHealingTests(LiveSyncTestCase):
    """The bookkeeping around a failed or lost request must not gate recovery.

    Three one-line holes from the audit: a standalone error left the stage
    `waiting` for the full timeout; an `unknown_ref` resend was answered by the
    frame cache before it could be sent; and a Pull permit was spent before the
    send it caused, so a failed send lost the Pull silently.
    """

    def test_01_a_standalone_error_releases_the_pending_gate(self):
        engine = self.engine
        record = self.stack_mod.stack_of(self.obj)
        key = (record.stack_id, self.stage.key)
        engine._pending[key] = {"stale-rid": time.monotonic()}
        self.stage.state = "waiting"

        engine._handle_error(
            {
                "type": "error",
                "code": "unknown_stage",
                "stage": self.stage.key,
                "message": "no claim",
            }
        )

        self.assertFalse(engine._pending.get(key), "the error left a pending request behind")
        self.assertEqual(self.stage.state, "error")
        self.assertIn("unknown_stage", self.stage.error)

    def test_02_an_unknown_ref_resend_bypasses_the_frame_cache(self):
        engine = self.engine
        record = self.stack_mod.stack_of(self.obj)
        key = (record.stack_id, self.stage.key)
        engine._resend_in_full(self.obj, record, self.stage, "test")

        self.assertIn(key, engine._verify, "the promised full resend would be swallowed by the cache")
        self.assertEqual(self.stage.geom_hash, "")
        self.assertIn(self.obj.name, engine._dirty)

    def test_03_a_pull_permit_survives_an_undelivered_send(self):
        engine = self.engine
        record = self.stack_mod.stack_of(self.obj)
        key = (record.stack_id, self.stage.key)

        self.stage.frozen = True
        engine.freeze(self.obj, self.stage)
        engine.request_again(self.obj, self.stage)
        self.assertIn(key, engine._pull_permits)

        real_send = engine.server.SERVER.send
        engine.server.SERVER.send = lambda frame: False
        try:
            engine._mark_object_dirty(self.obj)
            engine._timer()
        finally:
            engine.server.SERVER.send = real_send
        self.assertIn(
            key,
            engine._pull_permits,
            "the Pull permit was spent by a send that never left",
        )

        engine._mark_object_dirty(self.obj)
        self.assertTrue(
            pump(seconds=10.0, until=lambda: key not in engine._pull_permits),
            "the armed Pull never went out after the socket recovered",
        )
        self.stage.frozen = False
        engine.unfreeze(self.obj, self.stage)


if __name__ == "__main__":
    unittest.main()
