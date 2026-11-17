"""Layer B: Play Every Frame over a two-stage chain.

Run with:

    scripts/test-blender

What it proves: one object with ``stage A -> native Bevel -> stage B`` asks
each stage once per frame, upstream first, and stage B's prefix carries stage
A's applied output. With every client gone, playback keeps the last output while
the playhead goes on.

The single-stage gate lives in ``test_playback_sync.py``; this file reuses its
fakes and assertions instead of growing a second copy of the scaffolding.
"""

import os
import tempfile
import time
import unittest

import bpy

from harness import (
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
from stageloom.blender.engine import ENGINE
from stageloom.blender.engine import playback as engine_playback
from stageloom.blender.engine import request as engine_request


def wait_for(read, check, seconds=2.0):
    """Poll a fake's report until its write catches up with the answer."""
    deadline = time.time() + seconds
    value = read()
    while not check(value) and time.time() < deadline:
        time.sleep(0.01)
        value = read()
    return value


class ChainPlaybackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="stageloom")
        import stageloom.blender.engine as engine
        import stageloom.blender.engine.playback as playback

        cls.engine = engine
        cls.playback = playback
        cls.enterClassContext(isolate_data_root("stageloom-chain-playback-"))
        cls.port = free_port()
        engine.start_server(cls.port)
        time.sleep(0.5)

        # Blender's Bevel re-evaluates its interpolated UVs with last-bit (and
        # occasionally larger) variation between identical prefix reads, and a
        # request ref keys on the exact prefix hash. This test proves the gate,
        # not that noise: keep the cube's UV layer out of the fixture.
        bpy.ops.mesh.primitive_cube_add(size=2.0, calc_uvs=False)
        cls.obj = bpy.context.object
        cls.obj.name = "ChainPlayback"

        # Stack: stage A, a native Bevel, stage B. Adding a stage places it
        # after the active modifier, so the Bevel is inserted before B.
        bpy.ops.stageloom.add_stage(label="A")
        cls.stage_a = cls.obj.gh_stack.stages[0]
        bevel = cls.obj.modifiers.new("ChainBevel", "BEVEL")
        bevel.width = 0.1
        cls.obj.modifiers.active = bevel
        bpy.ops.stageloom.add_stage(label="B")
        cls.stage_b = cls.obj.gh_stack.stages[1]

        keyframe_scale(cls.obj, cls.stage_a, [1.0, 2.0, 3.0])
        keyframe_scale(cls.obj, cls.stage_b, [0.5, 1.0, 1.5])

        cls.report_a = os.path.join(tempfile.gettempdir(), "fake_gh_chain_a.json")
        cls.report_b = os.path.join(tempfile.gettempdir(), "fake_gh_chain_b.json")
        cls.fake_a = None
        cls.fake_b = None
        cls.start_fakes()
        if not pump(seconds=15.0, until=cls.settled):
            raise AssertionError("the chain never settled (report=%s)" % cls.reports())

    @classmethod
    def settled(cls):
        return (
            cls.stage_a.state == "ok"
            and cls.stage_b.state == "ok"
            and requests_of(cls.report_a) >= 1
            and requests_of(cls.report_b) >= 1
        )

    @classmethod
    def reports(cls):
        return report_of(cls.report_a), report_of(cls.report_b)

    @classmethod
    def start_fakes(cls):
        if cls.fake_a is not None:
            return
        import stageloom.blender.bridge as bridge

        # A restarted fake starts its counters at zero: drop the previous
        # report so `settled` cannot accept a stale count as the new answer.
        for path in (cls.report_a, cls.report_b):
            if os.path.exists(path):
                os.remove(path)
        cls.fake_a = start_fake(
            cls.stage_a.key, cls.report_a, "--delay-ms", "60", port=cls.port
        )
        cls.fake_b = start_fake(
            cls.stage_b.key, cls.report_b, "--delay-ms", "60", port=cls.port
        )
        deadline = time.time() + 10.0
        while time.time() < deadline and bridge.state["clients"] < 2:
            time.sleep(0.02)
        if bridge.state["clients"] < 2:
            raise AssertionError("the fakes never connected")

    @classmethod
    def stop_fakes(cls):
        stop_fake(cls.fake_a)
        stop_fake(cls.fake_b)
        cls.fake_a = None
        cls.fake_b = None
        import stageloom.blender.bridge as bridge

        if not pump(seconds=5.0, until=lambda: not bridge.state["clients"]):
            raise AssertionError("the fakes never disconnected")

    @classmethod
    def tearDownClass(cls):
        cls.stop_fakes()
        ENGINE.playback_sync = False
        bpy.ops.preferences.addon_disable(module="stageloom")

    def setUp(self):
        ENGINE.clear_runtimes()
        ENGINE.clear_runtimes()
        ENGINE.verify.clear()
        ENGINE.clear_runtimes()
        ENGINE.dirty.clear()
        ENGINE.playback_sync = False
        ENGINE.playback_reports.clear()
        bpy.context.scene.sync_mode = "NONE"
        bpy.context.scene.frame_set(1)
        self.sends = ENGINE.stats()["sends"]
        self.requests = (requests_of(self.report_a), requests_of(self.report_b))
        self.marker = 0

    def tearDown(self):
        ENGINE.playback_sync = False
        ENGINE.playback_reports.clear()

    def _frame_sends(self, frame):
        """Play one frame and return the sends its pass made, in trace order."""
        scene = bpy.context.scene
        self.marker += 1
        marker = "chain frame %d:%d" % (id(self), self.marker)
        ENGINE.trace_lines.append(marker)
        scene.frame_set(frame)
        # frame_change_post may or may not fire under -b; call the same gate the
        # handler calls. A duplicate call at the same frame sends nothing.
        ready = engine_playback._sync_playback_frame(scene)
        lines = list(ENGINE.trace_lines)
        if marker not in lines:
            raise AssertionError("the trace ring lost the frame marker")
        lines = lines[lines.index(marker) + 1:]
        sent_a = [i for i, line in enumerate(lines) if line.startswith(self.stage_a.key + ": sent")]
        sent_b = [i for i, line in enumerate(lines) if line.startswith(self.stage_b.key + ": sent")]
        return ready, sent_a, sent_b

    def test_01_each_frame_asks_a_then_b_once_and_b_sees_a(self):
        frames = (1, 2, 3)
        # A scales the cube, B multiplies A's output: 1 * .5, 2 * 1, 3 * 1.5.
        expected_prefix = {1: 1.0, 2: 2.0, 3: 3.0}
        expected_final = {1: 0.5, 2: 2.0, 3: 4.5}
        scene = bpy.context.scene
        seen = {}
        engine_playback._on_playback_pre(scene)
        try:
            for frame in frames:
                ready, sent_a, sent_b = self._frame_sends(frame)
                self.assertTrue(ready, "frame %d was not ready" % frame)
                self.assertEqual(len(sent_a), 1, "stage A did not ask once on frame %d" % frame)
                self.assertEqual(len(sent_b), 1, "stage B did not ask once on frame %d" % frame)
                self.assertLess(sent_a[0], sent_b[0], "frame %d asked B before A" % frame)
                self.assertAlmostEqual(helper_max(self.obj, self.stage_a.key), expected_prefix[frame], places=2)
                self.assertAlmostEqual(helper_max(self.obj, self.stage_b.key), expected_final[frame], places=2)
                # The fake writes its report after sending the answer; poll
                # rather than race the socket thread.
                seen[frame] = wait_for(
                    lambda: report_of(self.report_b).get("received_max", 0.0),
                    lambda value: abs(value - expected_prefix[frame]) < 0.01,
                )
        finally:
            engine_playback._on_playback_post(scene)

        for frame in frames:
            self.assertAlmostEqual(
                seen[frame],
                expected_prefix[frame],
                places=2,
                msg="stage B's prefix on frame %d did not include stage A's output" % frame,
            )
        self.assertAlmostEqual(helper_max(self.obj, self.stage_a.key), 3.0, places=2)
        self.assertAlmostEqual(helper_max(self.obj, self.stage_b.key), 4.5, places=2)
        self.assertEqual(
            ENGINE.stats()["sends"] - self.sends,
            2 * len(frames),
            "the chain did not ask once per stage per frame",
        )
        expected_requests = (self.requests[0] + len(frames), self.requests[1] + len(frames))
        counts = wait_for(
            lambda: (requests_of(self.report_a), requests_of(self.report_b)),
            lambda value: value == expected_requests,
        )
        self.assertEqual(counts, expected_requests, "the fakes did not each receive one request per frame")
        stats = ENGINE.stats()
        self.assertGreaterEqual(stats["playback_frames"], len(frames))
        self.assertEqual(stats["playback_synced"], stats["playback_frames"])
        self.assertEqual(stats["playback_failures"], 0)

    def test_02_without_clients_playback_keeps_the_last_output(self):
        type(self).start_fakes()
        self.assertTrue(pump(seconds=15.0, until=type(self).settled), "the chain never re-settled")
        scene = bpy.context.scene
        engine_playback._on_playback_pre(scene)
        try:
            for frame in (1, 2):
                self.assertTrue(self._frame_sends(frame)[0])
        finally:
            engine_playback._on_playback_post(scene)

        last_a = helper_max(self.obj, self.stage_a.key)
        last_b = helper_max(self.obj, self.stage_b.key)
        type(self).stop_fakes()
        requests = (requests_of(self.report_a), requests_of(self.report_b))
        sends = ENGINE.stats()["sends"]

        engine_playback._on_playback_pre(scene)
        try:
            for frame in (1, 2, 3, 4):
                ready, sent_a, sent_b = self._frame_sends(frame)
                self.assertFalse(ready, "a disconnected stage reported frame %d ready" % frame)
                self.assertEqual((sent_a, sent_b), ([], []), "a request left with no client")
                # Playback continued past the failed frame and kept the
                # previous output on both stages.
                self.assertAlmostEqual(helper_max(self.obj, self.stage_a.key), last_a, places=2)
                self.assertAlmostEqual(helper_max(self.obj, self.stage_b.key), last_b, places=2)
        finally:
            engine_playback._on_playback_post(scene)

        self.assertEqual(ENGINE.stats()["sends"], sends, "a request left with no client")
        self.assertEqual(
            (requests_of(self.report_a), requests_of(self.report_b)),
            requests,
            "a stopped fake received a request",
        )
        stats = ENGINE.stats()
        # One gate pass per frame normally; frame_change_post may add a second,
        # unchanged pass per frame under -b. A failed pass fails once per stage.
        self.assertGreaterEqual(stats["playback_frames"], 4)
        self.assertGreaterEqual(stats["playback_frames"] - stats["playback_synced"], 4)
        self.assertEqual(
            stats["playback_failures"],
            2 * (stats["playback_frames"] - stats["playback_synced"]),
            "the disconnected frames did not fail once per stage per gate pass",
        )

    def test_03_a_raising_stage_does_not_stop_the_next_stage(self):
        type(self).start_fakes()
        self.assertTrue(
            pump(seconds=15.0, until=type(self).settled),
            "the chain never settled before the fault",
        )
        real_process = engine_request._process_stage

        def failing_process(obj, record, stage):
            if stage.key == self.stage_a.key:
                raise RuntimeError("stage A pass fault")
            return real_process(obj, record, stage)

        scene = bpy.context.scene
        engine_request._process_stage = failing_process
        try:
            scene.frame_set(2)
            ENGINE.playback_sync = True
            try:
                ready = engine_playback._sync_playback_frame(scene)
            finally:
                ENGINE.playback_sync = False
        finally:
            engine_request._process_stage = real_process

        self.assertFalse(ready, "a failed stage reported the chain ready")
        self.assertEqual(self.stage_a.state, "error")
        self.assertIn("stage A pass fault", self.stage_a.error)
        self.assertTrue(
            pump(seconds=10.0, until=lambda: self.stage_b.state == "ok"),
            "the stage after the fault was not processed (state=%s, error=%s)"
            % (self.stage_b.state, self.stage_b.error),
        )
        self.assertEqual(self.stage_b.state, "ok", self.stage_b.error)
