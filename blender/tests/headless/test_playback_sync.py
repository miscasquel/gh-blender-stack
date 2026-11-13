"""Layer B: the Play Every Frame playback gate.

Run with:

    scripts/test-blender

What it proves:

1. On Play Every Frame (``Scene.sync_mode == "NONE"``) the frame pass waits for
   each stage's answer before the playhead can advance. With a delayed fake
   Grasshopper, three frames produce three requests, in order, no skips.
2. Frame Drop leaves the gate off and the existing asynchronous engine in
   charge.
3. A warm frame replays during playback with Grasshopper gone and sends
   nothing.
4. The engine timer is suppressed while the gate owns the frame.
5. A disconnected client fails the frame quickly, keeps the last output, and
   playback continues.
6. The wait limit defaults to "no timeout", with a preference to cap it.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

import bpy
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
BLENDER_PKG = os.path.join(REPO, "blender")
if BLENDER_PKG not in sys.path:
    sys.path.insert(0, BLENDER_PKG)

PORT = 9155
SCALE = "00000000-0000-4000-8000-0000000000aa"


def pump(seconds=10.0, until=None):
    """Drive the engine timer directly: bpy.app.timers is inert under -b."""
    import stageloom.blender.engine as engine

    deadline = time.time() + seconds
    while time.time() < deadline:
        engine._timer()
        if until is not None and until():
            return True
        time.sleep(0.01)
    return until is None


def start_fake(stage_key, report, *extra):
    return subprocess.Popen(
        [
            sys.executable,
            os.path.join(REPO, "tools", "fake_gh", "fake_gh.py"),
            "--port", str(PORT),
            "--stage", stage_key,
            "--report", report,
            *extra,
        ],
        env=dict(
            os.environ,
            PYTHONPATH=os.pathsep.join(
                [os.path.join(REPO, "blender", "stageloom", "vendor"), REPO]
            ),
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def stop_fake(process):
    if process is None:
        return
    if process.poll() is None:
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


def requests_of(path):
    report = report_of(path)
    return report.get("full", 0) + report.get("ref", 0)


def helper_max(obj, stage_key):
    """The largest coordinate in the stage's mesh helper: the fake scales the
    whole mesh by the parameter, so this is the parameter value."""
    import stageloom.blender.helpers as helpers

    helper = helpers.helper_for(obj, stage_key, "mesh")
    if helper is None or not len(helper.data.vertices):
        return 0.0
    count = len(helper.data.vertices)
    co = np.empty(count * 3, dtype=np.float32)
    helper.data.vertices.foreach_get("co", co)
    return float(np.abs(co.reshape(-1, 3)).max())


def keyframe_scale(obj, stage, values):
    import stageloom.blender.stack as stack_mod

    name = stack_mod.param_prop_name(stage.key, SCALE)
    if name not in obj:
        obj[name] = 1.0
    for frame, value in enumerate(values, start=1):
        obj[name] = float(value)
        obj.keyframe_insert(data_path='["%s"]' % name, frame=frame)


class PlaybackSyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="stageloom")
        import stageloom.blender.engine as engine
        import stageloom.blender.engine.playback as playback

        cls.engine = engine
        cls.playback = playback
        prefs = bpy.context.preferences.addons["stageloom"].preferences
        cls.data_root = tempfile.mkdtemp(prefix="stageloom-playback-test-")
        prefs.data_root = cls.data_root
        engine.start_server(PORT)
        time.sleep(0.5)

        bpy.ops.mesh.primitive_cube_add(size=2.0)
        cls.obj = bpy.context.object
        cls.obj.name = "Playback"
        bpy.ops.stageloom.add_stage(label="Playback")
        cls.stage = cls.obj.gh_stack.stages[0]
        cls.stage.live = False  # playback replays deterministic frames from the cache
        cls.record = cls.obj.gh_stack
        keyframe_scale(cls.obj, cls.stage, [1.0, 2.0, 3.0])

        cls.report = os.path.join(tempfile.gettempdir(), "fake_gh_playback.json")
        if os.path.exists(cls.report):
            os.remove(cls.report)
        # Slow enough that a frame pass must actually wait, the condition the
        # old playback lost every frame to.
        cls.fake = start_fake(cls.stage.key, cls.report, "--delay-ms", "120")
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
        engine._bump_generation(self.obj, self.record, self.stage, "playback test")
        engine._cache.clear()
        engine._cache_bytes.clear()
        engine._pending.clear()
        engine._sent.clear()
        engine._verify.clear()
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

    def test_02_warm_cache_replays_with_grasshopper_closed(self):
        self._play((1, 2, 3))
        import stageloom.blender.bridge as bridge

        stop_fake(type(self).fake)
        type(self).fake = None
        self.assertTrue(
            pump(seconds=5.0, until=lambda: not bridge.state["connected"]),
            "the fake never disconnected",
        )
        self.engine._cache.clear()  # force the disk store, the cross-session path
        sends = self.engine.stats["sends"]

        scene = bpy.context.scene
        self.engine._on_playback_pre(scene)
        try:
            for frame in (1, 2, 3):
                scene.frame_set(frame)
                self.engine._sync_playback_frame(scene)
        finally:
            self.engine._on_playback_post(scene)
        self.assertEqual(self.engine.stats["sends"], sends, "a cached frame was requested again")
        self.assertAlmostEqual(helper_max(self.obj, self.stage.key), 3.0, places=2)
        self.assertEqual(self.engine.stats["playback_failures"], 0)

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

    def test_05_no_timeout_by_default_and_can_be_capped(self):
        prefs = bpy.context.preferences.addons["stageloom"].preferences
        self.assertEqual(float(prefs.playback_timeout), 0.0)
        self.assertIsNone(self.playback._playback_deadline())
        prefs.playback_timeout = 0.25
        try:
            deadline = self.playback._playback_deadline()
            self.assertIsNotNone(deadline)
            self.assertGreater(deadline, time.monotonic())
        finally:
            prefs.playback_timeout = 0.0

    def test_06_a_driven_default_does_not_reset_the_cache(self):
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
            "a driven default looked like a definition change and would wipe the cache",
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
