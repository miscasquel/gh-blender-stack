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
    """F2: a raise between setting the clone guard and clearing it must not mute
    the engine for the rest of the session.

    The fault is injected where the report found it: `_drop_owned_data` is the
    first statement after the clone is removed, before the guard reset. With
    `_evaluating` stuck, `_on_depsgraph` and `_drain_depsgraph` return early, so
    nothing ever marks a stack dirty again. The test drives that failure, then
    one tick that must notice it, clear it, say so in the trace, and process the
    edit that was dropped in the meantime.
    """

    def tearDown(self):
        self.leave_edit_mode()

    def test_01_a_stuck_guard_is_cleared_and_the_edit_still_sends(self):
        engine = self.engine
        self.assertEqual(self.stage.state, "ok", self.stage.error)
        sends = engine.stats["sends"]
        self.edit_mode()

        real_drop = engine._drop_owned_data
        calls = {"n": 0}

        def flaky(id_block):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("test: cleanup fault")
            return real_drop(id_block)

        engine._drop_owned_data = flaky
        try:
            bm = bmesh.from_edit_mesh(self.obj.data)
            bm.verts.ensure_lookup_table()
            bm.verts[0].co.x += 4.0
            bmesh.update_edit_mesh(self.obj.data)
            bpy.context.view_layer.update()
            self.assertTrue(
                pump(seconds=10.0, until=lambda: engine._evaluating),
                "the cleanup fault never fired; the guard was not left up",
            )
            self.assertEqual(engine.stats["sends"], sends, "a request went out despite the fault")
        finally:
            engine._drop_owned_data = real_drop

        # While the guard is up, the handler drops an update it has to assume is
        # ours: the trace must name that exact mute.
        epoch = engine._prefix_epoch
        engine._on_depsgraph(bpy.context.scene)
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

        # And a stroke after recovery goes out too.
        sends = engine.stats["sends"]
        bm = bmesh.from_edit_mesh(self.obj.data)
        bm.verts.ensure_lookup_table()
        bm.verts[0].co.x += 1.0
        bmesh.update_edit_mesh(self.obj.data)
        bpy.context.view_layer.update()
        self.assertTrue(
            pump(seconds=15.0, until=lambda: engine.stats["sends"] > sends),
            "the first edit after recovery was not sent",
        )
        self.assertTrue(
            pump(
                seconds=15.0,
                until=lambda: first_x(report_of(self.report)) is not None
                and abs(first_x(report_of(self.report)) - 4.0) < 0.05,
            ),
            "the edit after recovery never reached Grasshopper (report=%s)"
            % report_of(self.report),
        )
        self.assertEqual(self.obj.mode, "EDIT", "the test left Edit Mode; the point was to stay in it")


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


if __name__ == "__main__":
    unittest.main()
