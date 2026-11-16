"""Layer B: the Play Every Frame playback gate.

Run with:

    scripts/test-blender

What it proves:

1. On Play Every Frame (``Scene.sync_mode == "NONE"``) the frame pass waits for
   each stage's answer before the playhead can advance. With a delayed fake
   Grasshopper, three frames produce three requests, in order, no skips.
2. Frame Drop leaves the gate off and the existing asynchronous engine in
   charge.
3. The engine timer is suppressed while the gate owns the frame.
4. A disconnected client fails the frame quickly, keeps the last output, and
   playback continues.
5. The wait limit defaults to "no timeout", with a preference to cap it.
"""

import os
import tempfile
import threading
import time
import unittest

import bpy
import bmesh
import numpy as np

from harness import (
    SCALE,
    free_port,
    helper_max,
    isolate_data_root,
    keyframe_scale,
    pump,
    report_of,
    requests_of,
    start_fake,
    stop_fake,
)


class PlaybackSyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="stageloom")
        import stageloom.blender.engine as engine
        import stageloom.blender.engine.playback as playback

        cls.engine = engine
        cls.playback = playback
        cls.data_root = cls.enterClassContext(
            isolate_data_root("stageloom-playback-test-")
        )
        cls.port = free_port()
        engine.start_server(cls.port)
        time.sleep(0.5)

        bpy.ops.mesh.primitive_cube_add(size=2.0)
        cls.obj = bpy.context.object
        cls.obj.name = "Playback"
        bpy.ops.stageloom.add_stage(label="Playback")
        cls.stage = cls.obj.gh_stack.stages[0]
        cls.record = cls.obj.gh_stack
        keyframe_scale(cls.obj, cls.stage, [1.0, 2.0, 3.0])

        cls.report = os.path.join(tempfile.gettempdir(), "fake_gh_playback.json")
        if os.path.exists(cls.report):
            os.remove(cls.report)
        # Slow enough that a frame pass must actually wait, the condition the
        # old playback lost every frame to.
        cls.fake = start_fake(
            cls.stage.key, cls.report, "--delay-ms", "120", port=cls.port
        )
        cls.await_client()
        if not pump(
            seconds=10.0,
            until=lambda: any(p.param_id == SCALE for p in cls.stage.params),
        ):
            raise AssertionError("the fake never published its schema")

    @classmethod
    def tearDownClass(cls):
        stop_fake(getattr(cls, "fake", None))
        cls.engine._playback_sync = False
        bpy.ops.preferences.addon_disable(module="stageloom")

    @classmethod
    def await_client(cls, seconds=15.0):
        import stageloom.blender.bridge as bridge

        deadline = time.time() + seconds
        while time.time() < deadline:
            if bridge.state["clients"] > 0:
                return
            time.sleep(0.02)
        raise AssertionError("the fake never connected to the engine")

    def setUp(self):
        engine = self.engine
        engine._pending.clear()
        engine._sent.clear()
        engine._verify.clear()
        engine._last_frame.clear()
        engine._dirty.clear()
        engine._playback_sync = False
        self.playback._reported.clear()
        bpy.context.scene.sync_mode = "NONE"
        bpy.context.scene.frame_set(1)
        self.sends = engine.stats["sends"]

    def tearDown(self):
        self.engine._playback_sync = False
        self.playback._reported.clear()

    def _play(self, frames):
        """Drive the gate the way Play Every Frame does: frame_set, then sync."""
        scene = bpy.context.scene
        self.engine._on_playback_pre(scene)
        try:
            for frame in frames:
                scene.frame_set(frame)
                # frame_change_post is not guaranteed to fire under -b; call
                # the same gate the handler calls. A duplicate call is a
                # cache hit and sends nothing.
                self.engine._sync_playback_frame(scene)
        finally:
            self.engine._on_playback_post(scene)

    def test_00_pre_and_post_handlers_choose_the_mode(self):
        scene = bpy.context.scene
        self.assertIn(self.engine._on_frame_change_sync, bpy.app.handlers.frame_change_post)
        self.assertIn(self.engine._on_playback_pre, bpy.app.handlers.animation_playback_pre)
        self.assertIn(self.engine._on_playback_post, bpy.app.handlers.animation_playback_post)

        scene.sync_mode = "NONE"
        self.engine._on_playback_pre(scene)
        self.assertTrue(self.engine._playback_sync, "Play Every Frame did not arm the gate")
        self.engine._on_playback_post(scene)
        self.assertFalse(self.engine._playback_sync)

        scene.sync_mode = "FRAME_DROP"
        self.engine._on_playback_pre(scene)
        self.assertFalse(self.engine._playback_sync, "Frame Drop armed the gate")
        self.engine._on_playback_post(scene)
        scene.sync_mode = "NONE"

    def test_01_play_every_frame_asks_once_per_frame_and_waits(self):
        self._play((1, 2, 3))
        self.assertAlmostEqual(helper_max(self.obj, self.stage.key), 3.0, places=2)
        self.assertEqual(
            self.engine.stats["sends"] - self.sends,
            3,
            "the playback gate did not ask for each frame exactly once (report=%s)"
            % report_of(self.report),
        )
        self.assertGreaterEqual(requests_of(self.report), 3)
        self.assertGreaterEqual(self.engine.stats["playback_frames"], 3)
        self.assertGreaterEqual(self.engine.stats["playback_synced"], 3)
        self.assertEqual(self.engine.stats["playback_failures"], 0)

    def test_02_a_repeated_frame_is_not_asked_twice(self):
        self._play((1,))
        sends = self.engine.stats["sends"]
        self._play((1,))
        self.assertEqual(
            self.engine.stats["sends"],
            sends,
            "the gate asked again for a frame already delivered",
        )

    def test_03_timer_is_suppressed_while_the_gate_owns_the_frame(self):
        passes = self.engine.stats["passes"]
        self.engine._playback_sync = True
        try:
            interval = self.engine._timer()
        finally:
            self.engine._playback_sync = False
        self.assertEqual(self.engine.stats["passes"], passes, "the timer ran during a synced playback")
        self.assertEqual(interval, self.engine.IDLE_INTERVAL)

    def test_04_disconnect_keeps_the_last_output_and_continues(self):
        import stageloom.blender.bridge as bridge

        stop_fake(type(self).fake)
        type(self).fake = None
        self.assertTrue(
            pump(seconds=5.0, until=lambda: not bridge.state["connected"]),
            "the fake never disconnected",
        )
        self.engine._playback_sync = True
        scene = bpy.context.scene
        scene.frame_set(2)
        ready = self.engine._sync_playback_frame(scene)
        self.assertFalse(ready, "a disconnected stage reported the frame ready")
        self.assertGreaterEqual(self.engine.stats["playback_failures"], 1)
        self.assertIn(
            self.stage.state,
            (
                self.engine.stack_mod.STATE_DISCONNECTED,
                self.engine.stack_mod.STATE_UNBOUND,
            ),
        )

        # This test owns the only fake; the later wait tests need a connected
        # client again, so restore one before leaving.
        self.engine._playback_sync = False
        type(self).fake = start_fake(
            self.stage.key, self.report, "--delay-ms", "120", port=self.port
        )
        self.assertTrue(
            pump(
                seconds=15.0,
                until=lambda: bridge.state["connected"] and self.stage.state == "ok",
            ),
            "the fake never reconnected after the disconnect test (state=%s, bridge=%r, running=%r, clients=%r, fake=%r)"
            % (
                self.stage.state,
                dict(bridge.state),
                self.engine.server.SERVER.running,
                self.engine.server.SERVER._clients,
                type(self).fake.process.poll() if type(self).fake else None,
            ),
        )

    def test_05_the_default_wait_still_has_a_finite_hard_cap(self):
        prefs = bpy.context.preferences.addons["stageloom"].preferences
        self.assertEqual(float(prefs.playback_timeout), 0.0)
        cap = self.playback.PLAYBACK_HARD_CAP_SECONDS
        deadline = self.playback._playback_deadline()
        self.assertIsNotNone(
            deadline,
            "the default wait has no finite backstop: a lost answer pins the main thread",
        )
        self.assertLessEqual(deadline - time.monotonic(), cap + 1.0)
        self.assertGreater(deadline - time.monotonic(), cap - 5.0)
        prefs.playback_timeout = 0.25
        try:
            deadline = self.playback._playback_deadline()
            self.assertIsNotNone(deadline)
            self.assertGreater(deadline, time.monotonic())
            self.assertLessEqual(deadline - time.monotonic(), 0.3)
        finally:
            prefs.playback_timeout = 0.0

    def test_06_a_driven_default_is_not_a_declaration_change(self):
        import stageloom.blender.props as props

        declared = [
            {
                "id": SCALE,
                "label": "frame time",
                "type": "float",
                "default": 1.0,
                "min": 0.0,
                "max": 1.0,
                "step": 0.1,
                "values": [],
            }
        ]
        next_frame = [dict(declared[0], default=2.0)]
        self.assertEqual(
            props.incoming_signature(declared),
            props.incoming_signature(next_frame),
            "a driven default looked like a declaration change and would rebuild the schema",
        )

    def test_07_stage_group_carries_the_native_bake_node(self):
        import stageloom.blender.nodes as nodes

        group = bpy.data.node_groups[nodes.group_name(self.stage.key)]
        bake = next((node for node in group.nodes if node.bl_idname == "GeometryNodeBake"), None)
        self.assertIsNotNone(bake, "the stage group has no native Bake node")
        self.assertTrue(
            any(item.socket_type == "GEOMETRY" for item in bake.bake_items),
            "the stage Bake node has no Geometry item; it would output empty geometry",
        )
        # Without a bake, the node must pass the live stage output through.
        depsgraph = bpy.context.evaluated_depsgraph_get()
        evaluated = self.obj.evaluated_get(depsgraph)
        mesh = evaluated.to_mesh()
        try:
            self.assertGreater(len(mesh.vertices), 0, "the Bake node swallowed the live output")
        finally:
            evaluated.to_mesh_clear()

    def test_08_the_node_graph_is_frozen_during_a_bake(self):
        """A native bake holds the evaluated graph; touching it per frame
        crashes Blender 5.3 with nested instances (2026-11-13). The bake
        operator holds updates, so ensure_group must be lookup-only then."""
        import stageloom.blender.nodes as nodes

        group = bpy.data.node_groups[nodes.group_name(self.stage.key)]
        original = nodes._group_matches

        def inspect_forbidden(*args, **kwargs):
            raise AssertionError("ensure_group inspected the graph during a bake")

        nodes._group_matches = inspect_forbidden
        try:
            nodes.hold_group_updates(True)
            try:
                returned = nodes.ensure_group(self.obj, self.stage.key)
            finally:
                nodes.hold_group_updates(False)
        finally:
            nodes._group_matches = original
        self.assertIs(
            returned,
            group,
            "the bake guard did not return the existing stage group",
        )

    def test_09_bake_progress_follows_the_baked_frames(self):
        import stageloom.blender.engine as engine
        from stageloom.blender.engine import playback

        class Cursor:
            def __init__(self):
                self.values = []

            def progress_update(self, value):
                self.values.append(value)

        cursor = Cursor()
        engine._bake_progress = (cursor, 10, 5)
        try:
            for frame in (9, 10, 12, 14, 20):
                playback._update_bake_progress(
                    type("Scene", (), {"frame_current": frame})()
                )
        finally:
            engine._bake_progress = None
        self.assertEqual(cursor.values, [0, 1, 3, 5, 5])

    def _waiting(self, rid):
        key = (self.record.stack_id, self.stage.key)
        self.engine._pending.clear()
        self.engine._pending[key] = {rid: time.monotonic()}
        self.stage.rid = rid
        self.stage.state = self.engine.stack_mod.STATE_WAITING
        return key

    def _response(self, rid):
        sections = {
            "mesh.verts": np.array(
                [[9.0, 0.0, 0.0], [10.0, 0.0, 0.0], [9.0, 1.0, 0.0]],
                dtype=np.float32,
            ),
            "mesh.face_counts": np.array([3], dtype=np.uint32),
            "mesh.face_indices": np.array([0, 1, 2], dtype=np.uint32),
        }
        return {
            "type": "response",
            "stage": self.stage.key,
            "rid": rid,
            "out_hash": "sha256:superseded-answer",
            "status": "ok",
            "warnings": [],
        }, sections

    def test_10_a_hello_supersedes_a_playback_wait(self):
        import stageloom.blender.bridge as bridge

        rid = "sha256:playback-waited"
        key = self._waiting(rid)
        self.stage.applied_out_hash = "sha256:before-hello"
        hello = {
            "type": "hello",
            "doc": "replacement.gh",
            "stages": [{"key": self.stage.key}],
        }
        timer = threading.Timer(0.15, bridge.post_incoming, args=(hello, {}))
        timer.daemon = True
        timer.start()
        started = time.monotonic()
        try:
            ready = self.playback._pump_until_settled(
                self.record, self.stage, rid, self.playback._playback_deadline()
            )
        finally:
            timer.cancel()
        elapsed = time.monotonic() - started
        self.assertFalse(ready, "a superseded wait reported the frame ready")
        self.assertLess(
            elapsed,
            2.0,
            "the hello during playback did not release the main thread",
        )
        self.assertNotIn(key, self.engine._pending, "the hello left the old wait pending")
        self.assertEqual(self.stage.rid, "", "the hello kept the old session's request id")
        self.assertEqual(self.stage.state, self.engine.stack_mod.STATE_IDLE)

        # The old client's answer must not apply to the new session.
        header, sections = self._response(rid)
        bridge.post_incoming(header, sections)
        self.engine._handle_incoming()
        self.assertEqual(
            self.stage.applied_out_hash,
            "sha256:before-hello",
            "a superseded answer applied after the hello",
        )

    def test_11_a_document_change_releases_the_wait_without_an_answer(self):
        import stageloom.blender.bridge as bridge

        rid = "sha256:playback-doc"
        self._waiting(rid)
        previous_doc = bridge.state["doc"]
        previous_at = bridge.state["connected_at"]

        def replace_document():
            bridge.state["doc"] = "another-document.gh"

        timer = threading.Timer(0.1, replace_document)
        timer.daemon = True
        timer.start()
        started = time.monotonic()
        try:
            ready = self.playback._pump_until_settled(
                self.record, self.stage, rid, self.playback._playback_deadline()
            )
            elapsed = time.monotonic() - started
        finally:
            timer.cancel()
            bridge.state["doc"] = previous_doc
            bridge.state["connected_at"] = previous_at
            self.engine._forget(self.record.stack_id, self.stage.key)
            self.stage.rid = ""
            self.stage.state = self.engine.stack_mod.STATE_IDLE
        self.assertFalse(ready)
        self.assertLess(elapsed, 1.0, "a changed document blocked the playback wait")

    def test_12_the_hard_cap_reports_a_timeout_and_keeps_the_output(self):
        rid = "sha256:playback-timeout"
        key = self._waiting(rid)
        previous_cap = self.playback.PLAYBACK_HARD_CAP_SECONDS
        self.playback.PLAYBACK_HARD_CAP_SECONDS = 0.25
        try:
            started = time.monotonic()
            ready = self.playback._pump_until_settled(
                self.record, self.stage, rid, self.playback._playback_deadline()
            )
            elapsed = time.monotonic() - started
        finally:
            self.playback.PLAYBACK_HARD_CAP_SECONDS = previous_cap
        self.assertFalse(ready)
        self.assertLess(elapsed, 3.0, "the hard cap did not end the wait")
        self.assertNotIn(key, self.engine._pending, "the timeout left the request pending")
        self.assertEqual(self.stage.state, self.engine.stack_mod.STATE_ERROR)
        self.assertIn("no answer within", self.stage.error)
        self.stage.state = self.engine.stack_mod.STATE_IDLE
        self.stage.error = ""

    def test_13_a_stage_exception_stays_in_that_stage(self):
        import stageloom.blender.engine as engine

        self.stage.applied_out_hash = ""
        self.stage.error = ""
        real_apply = engine._apply

        def failing_apply(
            obj, stage, payload, attributes=None, definitions=None, topology=None
        ):
            raise RuntimeError("playback apply fault")

        engine._apply = failing_apply
        scene = bpy.context.scene
        try:
            scene.frame_set(3)
            engine._playback_sync = True
            try:
                ready = engine._sync_playback_frame(scene)
            finally:
                engine._playback_sync = False
        finally:
            engine._apply = real_apply
        self.assertFalse(ready, "a stage whose answer failed reported the frame ready")
        self.assertEqual(self.stage.state, engine.stack_mod.STATE_ERROR)
        self.assertIn("playback apply fault", self.stage.error)
        self.assertEqual(
            engine._pending.get((self.record.stack_id, self.stage.key), {}),
            {},
            "the failed answer left the request pending",
        )
        self.stage.state = engine.stack_mod.STATE_OK
        self.stage.error = ""

    def test_15_the_gate_ignores_another_scenes_frame_event(self):
        """Play Every Frame arms one scene; a frame event from another scene
        must not run (or block on) that scene's stages."""
        scene = bpy.context.scene
        other = bpy.data.scenes.new("Playback Other Scene")
        other.sync_mode = "NONE"
        try:
            self.engine._on_playback_pre(scene)
            self.assertTrue(self.engine._playback_sync)
            frames = self.engine.stats["playback_frames"]
            self.engine._on_frame_change_sync(other)
            self.assertEqual(
                self.engine.stats["playback_frames"],
                frames,
                "another scene's frame entered the playback gate",
            )
            self.engine._on_playback_post(other)
            self.assertTrue(self.engine._playback_sync, "another scene disarmed the gate")
        finally:
            self.engine._on_playback_post(scene)
            bpy.data.scenes.remove(other)
        self.assertFalse(self.engine._playback_sync)

    def test_14_playback_flushes_edit_mode_before_reading_the_prefix(self):
        engine = self.engine
        self.stage.applied_out_hash = ""
        self.stage.error = ""
        engine._prefix_memo.clear()
        scene = bpy.context.scene
        scene.sync_mode = "NONE"
        scene.frame_set(1)
        bpy.context.view_layer.objects.active = self.obj
        self.obj.select_set(True)
        bpy.ops.object.mode_set(mode="EDIT")
        bm = bmesh.from_edit_mesh(self.obj.data)
        try:
            bm.verts.ensure_lookup_table()
            moved = float(bm.verts[0].co.x) + 5.0
            bm.verts[0].co.x = moved
            bmesh.update_edit_mesh(self.obj.data)
            engine._playback_sync = True
            try:
                ready = engine._sync_playback_frame(scene)
            finally:
                engine._playback_sync = False
            self.assertTrue(ready, "the playback gate failed in Edit Mode")
            report = {}
            deadline = time.time() + 3.0
            while time.time() < deadline:
                report = report_of(self.report)
                first = report.get("received_first") or [None]
                if first[0] is not None and abs(first[0] - moved) < 0.05:
                    break
                time.sleep(0.01)
            first = report.get("received_first") or [None]
            self.assertIsNotNone(first[0])
            self.assertAlmostEqual(
                first[0],
                moved,
                places=2,
                msg="the playback gate read the pre-edit cage (report=%s)" % (report,),
            )
        finally:
            bm = bmesh.from_edit_mesh(self.obj.data)
            bm.verts.ensure_lookup_table()
            bm.verts[0].co = (-1.0, -1.0, -1.0)
            bmesh.update_edit_mesh(self.obj.data)
            bpy.ops.object.mode_set(mode="OBJECT")
