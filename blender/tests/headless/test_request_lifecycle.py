"""Layer B: live request lifecycle — one outstanding, latest-wins, recovery.

Every stage is live: a delivered request is a solver step. These suites pin the
request bookkeeping that outlived the removed frame cache: coalescing, dropped
and superseded requests, refusals, solve failures, per-frame requests and the
quiet unchanged answer.
"""

import os
import tempfile
import time
import unittest

import bpy
import numpy as np

from harness import (
    SCALE,
    free_port,
    helper_max,
    isolate_data_root,
    keyframe_scale,
    pump,
    pump_dirty,
    report_of,
    requests_of,
    start_fake,
    stop_fake,
)
from stageloom.blender.engine import ENGINE
from stageloom.blender.engine import triggers as engine_triggers
from stageloom.blender.engine import messages as engine_messages


def scrub(obj, frame):
    """Move the timeline and make sure the engine sees it (frame_change_post is
    not guaranteed to run under -b)."""

    bpy.context.scene.frame_set(frame)
    ENGINE.mark_object_dirty(obj)


def helper_bounds(obj, stage_key):
    """(min, max) of the helper mesh's X, so a test can see a prefix offset."""
    import stageloom.blender.helpers as helpers

    helper = helpers.helper_for(obj, stage_key, "mesh")
    if helper is None or not len(helper.data.vertices):
        return None
    count = len(helper.data.vertices)
    co = np.empty(count * 3, dtype=np.float32)
    helper.data.vertices.foreach_get("co", co)
    x = co.reshape(-1, 3)[:, 0]
    return float(x.min()), float(x.max())


def time_driven_modifier(obj):
    """Put a Geometry Nodes modifier reading scene time at the head of the stack.

    The group offsets the geometry along X by the scene time in seconds, so the
    evaluated prefix moves with the timeline while nothing about the modifier's
    properties or its node graph changes."""
    tree = bpy.data.node_groups.new("TimePrefix", "GeometryNodeTree")
    tree.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    tree.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    source = tree.nodes.new("NodeGroupInput")
    sink = tree.nodes.new("NodeGroupOutput")
    clock = tree.nodes.new("GeometryNodeInputSceneTime")
    combine = tree.nodes.new("ShaderNodeCombineXYZ")
    offset = tree.nodes.new("GeometryNodeSetPosition")
    offset.location = (200.0, 0.0)
    tree.links.new(source.outputs["Geometry"], offset.inputs["Geometry"])
    tree.links.new(clock.outputs["Seconds"], combine.inputs["X"])
    tree.links.new(combine.outputs["Vector"], offset.inputs["Offset"])
    tree.links.new(offset.outputs["Geometry"], sink.inputs["Geometry"])

    modifier = obj.modifiers.new("TimePrefix", "NODES")
    modifier.node_group = tree
    obj.modifiers.move(len(obj.modifiers) - 1, 0)
    return modifier


def expected_offset(frame):
    """Scene Time's Seconds output: the frame over the scene's frame rate."""
    scene = bpy.context.scene
    return float(frame) * scene.render.fps_base / float(scene.render.fps)


class RequestLifecycleTestCase(unittest.TestCase):
    """Shared setup: fresh scene, temp data root, one server and a free port."""

    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="stageloom")
        import stageloom.blender.engine as engine
        import stageloom.blender.stack as stack_mod

        cls.engine = engine
        cls.stack_mod = stack_mod
        cls.data_root = cls.enterClassContext(
            isolate_data_root("stageloom-request-test-")
        )
        cls.port = free_port()
        engine.start_server(cls.port)
        time.sleep(0.5)

    @classmethod
    def tearDownClass(cls):
        bpy.ops.preferences.addon_disable(module="stageloom")

    @classmethod
    def await_client(cls, seconds=15.0):
        """Wait for the fake's handshake without ticking the engine.

        Running a pass here would shift the response counts that the fake's
        scripted options are timed against."""
        import stageloom.blender.bridge as bridge

        deadline = time.time() + seconds
        while time.time() < deadline:
            if bridge.state["clients"] > 0:
                return
            time.sleep(0.02)
        raise AssertionError("the fake never connected to the engine")

    @classmethod
    def add_cube(cls, label="Request"):
        bpy.ops.mesh.primitive_cube_add(size=2.0)
        obj = bpy.context.object
        obj.name = label
        bpy.ops.stageloom.add_stage(label=label)
        return obj, obj.gh_stack.stages[0]

    @classmethod
    def report_path(cls, name):
        path = os.path.join(tempfile.gettempdir(), "fake_gh_request_%s.json" % name)
        if os.path.exists(path):
            os.remove(path)
        return path


class OneEvaluationAtATimeTests(RequestLifecycleTestCase):
    """A frame that arrives while an answer is outstanding is not requested. The
    stage waits, then evaluates whatever frame the playhead is on when the answer
    lands: it never queues frames behind a working Grasshopper."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.obj, cls.stage = cls.add_cube("Slow")
        keyframe_scale(cls.obj, cls.stage, [1.0, 2.0, 3.0])
        cls.report = cls.report_path("slow")
        cls.fake = start_fake(cls.stage.key, cls.report, "--delay-ms", "800", port=cls.port)
        cls.await_client()
        scrub(cls.obj, 1)
        pump(seconds=10.0, until=lambda: cls.stage.state == "ok")
        pump(until=lambda: abs(helper_max(cls.obj, cls.stage.key) - 1.0) < 0.01)

    @classmethod
    def tearDownClass(cls):
        stop_fake(getattr(cls, "fake", None))
        super().tearDownClass()

    def test_01_a_frame_while_one_is_outstanding_is_not_queued(self):
        sends = ENGINE.stats()["sends"]
        type(self).sends_with_one_in_flight = sends + 1
        scrub(self.obj, 2)
        self.assertTrue(
            pump(seconds=5.0, until=lambda: ENGINE.stats()["sends"] == sends + 1),
            "the next frame was never requested",
        )
        self.assertEqual(self.stage.state, "waiting")

        scrub(self.obj, 3)  # the playhead moves on while the answer is in flight
        pump(seconds=0.4)
        self.assertEqual(
            ENGINE.stats()["sends"],
            sends + 1,
            "a second request went out while the first was still outstanding",
        )
        self.assertEqual(self.stage.state, "waiting")
        # Nothing has been displayed for either frame yet: their answers are not in.
        self.assertAlmostEqual(helper_max(self.obj, self.stage.key), 1.0, places=2)

    def test_02_the_answer_lands_and_the_current_frame_is_asked_for_next(self):
        sends = type(self).sends_with_one_in_flight
        self.assertTrue(
            pump(seconds=10.0, until=lambda: ENGINE.stats()["sends"] > sends),
            "the frame the playhead moved to was never requested after the answer",
        )
        self.assertTrue(
            pump(seconds=10.0, until=lambda: abs(helper_max(self.obj, self.stage.key) - 3.0) < 0.01),
            "the frame the playhead stopped on never applied",
        )
        self.assertEqual(self.stage.state, "ok")
        self.assertGreaterEqual(requests_of(self.report), 3)


class PlaybackScrubTests(RequestLifecycleTestCase):
    """A scrub evaluates one frame per round trip and never queues every frame
    it passes: that backlog is what made the client stop responding while it
    worked through frames nobody was looking at."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.obj, cls.stage = cls.add_cube("Play")
        keyframe_scale(cls.obj, cls.stage, [float(f) for f in range(1, 9)])
        cls.report = cls.report_path("play")
        # A round trip slower than the playhead step, as a real GH solve can be.
        cls.fake = start_fake(cls.stage.key, cls.report, "--delay-ms", "400", port=cls.port)
        cls.await_client()
        scrub(cls.obj, 1)
        pump(seconds=10.0, until=lambda: cls.stage.state == "ok")

    @classmethod
    def tearDownClass(cls):
        stop_fake(getattr(cls, "fake", None))
        super().tearDownClass()

    @classmethod
    def play(cls, frames, hold=0.08):
        for frame in frames:
            scrub(cls.obj, frame)
            until = time.time() + hold
            while time.time() < until:
                engine_triggers._timer()
                time.sleep(0.005)

    def test_01_a_scrub_does_not_queue_every_frame_it_passes(self):
        before = ENGINE.stats()["sends"]
        type(self).play(range(1, 9))
        requested = ENGINE.stats()["sends"] - before
        self.assertLess(
            requested,
            8,
            "a fast scrub asked for every frame it passed over (%d of 8): that is the "
            "backlog that blocks the client" % requested,
        )
        self.assertGreaterEqual(requested, 1, "a scrub asked for nothing at all")

    def test_02_the_frame_it_stopped_on_lands_then_stays_quiet(self):
        cls = type(self)
        cls.play([8], hold=0.8)
        self.assertTrue(
            pump(seconds=15.0, until=lambda: abs(helper_max(self.obj, cls.stage.key) - 8.0) < 0.01),
            "the frame the playhead stopped on was never evaluated",
        )
        self.assertEqual(self.stage.state, "ok")

        sends = ENGINE.stats()["sends"]
        cls.play([8], hold=0.3)
        self.assertEqual(
            ENGINE.stats()["sends"],
            sends,
            "the same frame and inputs were asked for again",
        )


class OneRequestOutstandingTests(RequestLifecycleTestCase):
    """The client is never handed a second request while one is in flight.

    A superseded frame must not open the gate early. The client solves serially,
    so two requests in flight is a backlog the user feels; worse, a client that
    coalesces its solves can then answer a request the engine has stopped
    waiting for, which is how a stage came to look stuck until a manual Push.
    Measured before the fix: a peak of two requests in flight, and 11 requests
    where 7 do the same work."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.obj, cls.stage = cls.add_cube("Outstanding")
        keyframe_scale(cls.obj, cls.stage, [float((f % 7) + 1) for f in range(1, 21)])
        cls.report = cls.report_path("outstanding")
        cls.fake = start_fake(cls.stage.key, cls.report, "--delay-ms", "300", port=cls.port)
        cls.await_client()

    @classmethod
    def tearDownClass(cls):
        stop_fake(getattr(cls, "fake", None))
        super().tearDownClass()

    def test_01_only_one_request_is_ever_outstanding(self):
        import stageloom.blender.server as server

        seen = {"outstanding": 0, "peak": 0}
        real_send = server.SERVER.send
        real_response = engine_messages._handle_response

        def counting_send(frame):
            sent = real_send(frame)
            if sent:
                seen["outstanding"] += 1
                seen["peak"] = max(seen["peak"], seen["outstanding"])
            return sent

        def counting_response(header, sections):
            seen["outstanding"] = max(0, seen["outstanding"] - 1)
            return real_response(header, sections)

        server.SERVER.send = counting_send
        engine_messages._handle_response = counting_response
        try:
            for frame in list(range(1, 21)) + list(range(20, 0, -1)):
                scrub(self.obj, frame)
                until = time.time() + 0.04
                while time.time() < until:
                    engine_triggers._timer()
                    time.sleep(0.004)
            pump(seconds=2.0)
        finally:
            server.SERVER.send = real_send
            engine_messages._handle_response = real_response

        self.assertGreater(seen["peak"], 0, "no request was ever sent")
        self.assertEqual(
            seen["peak"],
            1,
            "up to %d requests were outstanding at once; the client solves serially "
            "and a second request is a backlog it works through after the user has "
            "moved on" % seen["peak"],
        )


class UnknownRefTests(RequestLifecycleTestCase):
    """A ref the client cannot serve is refused, and Blender resends in full.

    The regression: the plugin read `geom.mode` and then ignored it, so a ref it
    could not serve was dropped without a reply. Blender waited out its timeout,
    its retry was another ref, and the stage sat with no geometry and no visible
    error — the reopened-.blend bug. Blender must also understand the refusal in
    both shapes the protocol defines: the standalone error message (4.6, which the
    plugin sends) and an error-status response (4.4)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.obj, cls.stage = cls.add_cube("Ref")
        keyframe_scale(cls.obj, cls.stage, [1.0, 2.0])
        cls.report = cls.report_path("unknown_ref")
        cls.fake = start_fake(cls.stage.key, cls.report, "--forget-after", "1", port=cls.port)
        cls.await_client()
        scrub(cls.obj, 1)
        pump(seconds=10.0, until=lambda: cls.stage.state == "ok")

    @classmethod
    def tearDownClass(cls):
        stop_fake(getattr(cls, "fake", None))
        super().tearDownClass()

    def test_01_a_ref_that_cannot_be_served_is_refused_and_resent(self):
        # The geometry has not changed, so frame 2 travels as a ref; the client has
        # forgotten the geometry, so it refuses and Blender must resend in full.
        fulls = report_of(self.report).get("full", 0)
        self.assertEqual(self.stage.state, "ok", self.stage.error)

        scrub(self.obj, 2)
        self.assertTrue(
            pump(seconds=15.0, until=lambda: report_of(self.report).get("full", 0) > fulls),
            "the refused ref never triggered a full resend (report=%s)" % report_of(self.report),
        )
        self.assertTrue(
            pump(seconds=15.0, until=lambda: abs(helper_max(self.obj, self.stage.key) - 2.0) < 0.01),
            "the recovered frame never applied",
        )
        self.assertEqual(self.stage.state, "ok")
        self.assertEqual(self.stage.error, "")
        self.assertEqual(
            ENGINE.stats()["timeouts"], 0, "the recovery went through a timeout instead"
        )


class UnknownRefResponseTests(UnknownRefTests):
    """The same recovery when the refusal arrives as an error-status response."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        stop_fake(cls.fake)
        cls.report = cls.report_path("unknown_ref_response")
        cls.fake = start_fake(
            cls.stage.key,
            cls.report,
            "--forget-after",
            "1",
            "--refusal",
            "response",
            port=cls.port,
        )
        cls.await_client()
        cls.stage.geom_hash = ""
        scrub(cls.obj, 1)
        pump(seconds=10.0, until=lambda: cls.stage.state == "ok")


class SolveFailureTests(RequestLifecycleTestCase):
    """A failed solve answers immediately, and is not immediately re-asked.

    When a Stage Out fails, the plugin answers with an error response (protocol
    §4.4); the stage must land on `error` without waiting out the 30 s timeout.
    The engine must then wait for a change — a component that just failed fails
    again, so an instant retry is a request loop against the client (protocol §7:
    retry on the next change or Push). The plugin's own half cannot be reached by
    any suite that drives the fake."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.obj, cls.stage = cls.add_cube("Fails")
        keyframe_scale(cls.obj, cls.stage, [1.0, 2.0, 3.0])
        cls.report = cls.report_path("solve_failure")
        # The second response fails; the first (frame 1) settles the class, and
        # the third is the recovery after the next change.
        cls.fake = start_fake(cls.stage.key, cls.report, "--fail-after", "2", port=cls.port)
        cls.await_client()
        scrub(cls.obj, 1)
        pump(seconds=10.0, until=lambda: cls.stage.state == "ok")

    @classmethod
    def tearDownClass(cls):
        stop_fake(getattr(cls, "fake", None))
        super().tearDownClass()

    def test_01_error_arrives_without_the_timeout(self):
        self.assertEqual(self.stage.state, "ok", self.stage.error)
        timeouts_before = ENGINE.stats()["timeouts"]
        started = time.time()
        scrub(self.obj, 2)
        self.assertTrue(
            pump(seconds=10.0, until=lambda: self.stage.state == "error"),
            "the stage never reached error",
        )
        elapsed = time.time() - started
        self.assertLess(
            elapsed,
            5.0,
            "the error took %.1f s: it arrived through the timeout, not as an answer" % elapsed,
        )
        self.assertEqual(
            ENGINE.stats()["timeouts"], timeouts_before, "the error went through a timeout"
        )
        self.assertIn("fake solve failure", self.stage.error)

    def test_02_a_failed_stage_is_not_immediately_retried(self):
        # The error must stick until something changes: an instant retry asks a
        # component that just failed to solve again, in a loop.
        responses = report_of(self.report).get("responses", 0)
        pump(seconds=1.0)
        self.assertEqual(self.stage.state, "error")
        self.assertEqual(
            report_of(self.report).get("responses", 0),
            responses,
            "the engine re-asked after the error without a change",
        )

    def test_03_the_next_change_retries_and_clears_the_error(self):
        scrub(self.obj, 3)
        self.assertTrue(
            pump(seconds=10.0, until=lambda: self.stage.state == "ok"),
            "the stage never recovered (state=%s, error=%s)" % (self.stage.state, self.stage.error),
        )
        self.assertEqual(self.stage.error, "")
        self.assertTrue(
            pump(seconds=10.0, until=lambda: abs(helper_max(self.obj, self.stage.key) - 3.0) < 0.01),
            "the recovered frame never applied",
        )


class CoalescedAnswerTests(RequestLifecycleTestCase):
    """A client that coalesces a burst of requests — the plugin collapses
    ScheduleSolution calls and answers only the newest — must not strand the
    stage.

    The class of bug this guards: an early gate opening sent a second request
    while the first was outstanding; the client answered the newest and dropped
    the first; the engine waited on the dropped rid and the stage looked stuck
    until a Push. Here the burst is forced deterministically: the timeout retry
    carries a changed parameter, so the dropped request and the answered one have
    different rids, and the engine's apply rule has to take the newest."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        prefs = bpy.context.preferences.addons["stageloom"].preferences
        cls.timeout_before = prefs.timeout
        prefs.timeout = 1.0
        cls.obj, cls.stage = cls.add_cube("Coalesce")
        cls.report = cls.report_path("coalesce")
        cls.fake = start_fake(cls.stage.key, cls.report, "--coalesce", "--delay-ms", "1800", port=cls.port)
        cls.await_client()

    @classmethod
    def tearDownClass(cls):
        bpy.context.preferences.addons["stageloom"].preferences.timeout = cls.timeout_before
        stop_fake(getattr(cls, "fake", None))
        super().tearDownClass()

    def test_01_a_dropped_request_is_recovered_by_the_retry(self):
        timeouts_before = ENGINE.stats()["timeouts"]
        ENGINE.mark_object_dirty(self.obj)
        self.assertTrue(
            pump(seconds=5.0, until=lambda: requests_of(self.report) >= 1),
            "the first request never reached the client",
        )

        # The user moves the slider while the slow client is solving. With one
        # request in flight nothing goes out now; at the timeout the retry
        # carries the new value and a new rid, and the client coalesces the
        # burst — the first request is never answered, only the retry.
        name = "gh/%s/%s" % (self.stage.key, SCALE)
        self.obj[name] = 2.0
        self.assertTrue(
            pump_dirty(
                self.obj,
                seconds=12.0,
                until=lambda: abs(helper_max(self.obj, self.stage.key) - 2.0) < 0.01,
            ),
            "the recovered answer never applied (report=%s)" % report_of(self.report),
        )
        self.assertEqual(self.stage.state, "ok", self.stage.error)
        report = report_of(self.report)
        self.assertGreaterEqual(
            report.get("dropped", 0),
            1,
            "no request was coalesced, so the test proved nothing (report=%s)" % report,
        )
        self.assertGreaterEqual(ENGINE.stats()["timeouts"], timeouts_before + 1)


class TimeDrivenPrefixTests(RequestLifecycleTestCase):
    """A native modifier before the stage reads scene time, so the geometry fed to
    Grasshopper moves with the timeline.

    Nothing about the modifier changes between frames: the same node graph, the
    same property values. Only the evaluated prefix moves. This fails unless the
    prefix memo's revision includes the frame: the memo would serve one frame's
    geometry for every frame and the request ID would never move."""

    # The cube is size 2, so the unshifted helper reaches x = 1.0.
    CUBE_MAX_X = 1.0

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import stageloom.blender.stack as stack_mod

        bpy.ops.mesh.primitive_cube_add(size=2.0)
        cls.obj = bpy.context.object
        cls.obj.name = "Clock"
        bpy.ops.stageloom.add_stage(label="Clock")
        cls.stage = cls.obj.gh_stack.stages[0]
        # A constant parameter: the prefix is the only thing that can differ
        # between frames, so no keyframes are involved anywhere.
        cls.obj[stack_mod.param_prop_name(cls.stage.key, SCALE)] = 1.0
        time_driven_modifier(cls.obj)
        cls.report = cls.report_path("clock")
        cls.fake = start_fake(cls.stage.key, cls.report, port=cls.port)
        cls.await_client()

    @classmethod
    def tearDownClass(cls):
        stop_fake(getattr(cls, "fake", None))
        super().tearDownClass()

    def at_frame(self, frame, seconds=15.0):
        """Move to a frame and wait until the helper actually shows it."""
        scrub(self.obj, frame)
        expected = self.CUBE_MAX_X + expected_offset(frame)
        reached = pump(
            seconds=seconds,
            until=lambda: self.max_x() is not None and abs(self.max_x() - expected) < 0.05,
        )
        self.assertTrue(
            reached,
            "frame %d never reached the helper: max x %r, expected %.3f"
            % (frame, self.max_x(), expected),
        )
        return self.max_x()

    def max_x(self):
        bounds = helper_bounds(self.obj, self.stage.key)
        return None if bounds is None else bounds[1]

    def test_01_each_frame_is_its_own_request(self):
        # Two frames, two request IDs: the frame is part of what a request means.
        before = ENGINE.stats()["sends"]
        first = self.at_frame(1)
        rid_one = self.stage.rid
        second = self.at_frame(24)
        rid_two = self.stage.rid

        self.assertNotEqual(rid_one, rid_two, "both frames produced the same request ID")
        self.assertGreater(abs(second - first), 0.5, "the prefix did not move with the frame")
        self.assertGreaterEqual(
            requests_of(self.report), 2,
            "the second frame was never requested (report=%s)" % report_of(self.report),
        )
        self.assertGreaterEqual(ENGINE.stats()["sends"] - before, 2)

    def test_02_the_frame_moves_the_geometry_both_ways(self):
        first = self.at_frame(1)
        second = self.at_frame(24)
        self.assertLess(first, second, "the evaluation went the wrong way: %r then %r" % (first, second))

    def test_03_an_unchanged_frame_still_hits_the_memo(self):
        # The memo must keep earning its place: a pass at the frame already
        # evaluated is the wait-free no-op that lets Cycles accumulate samples.
        # The pass is forced the way the depsgraph handler forces it, because
        # `_timer` only touches objects something has marked dirty.
        self.at_frame(24)
        record = self.stack_mod.stack_of(self.obj)
        self.assertTrue(
            pump(
                seconds=10.0,
                until=lambda: self.stage.state == "ok"
                and not ENGINE.dirty
                and not ENGINE.pending_of(*(record.stack_id, self.stage.key)),
            ),
            "the frame's request never settled",
        )

        def idle_passes(count):
            for _ in range(count):
                ENGINE.mark_object_dirty(self.obj)
                engine_triggers._timer()

        idle_passes(3)
        hits = ENGINE.stats()["prefix_memo_hits"]
        clones = ENGINE.stats()["prefix_clones"]
        sends = ENGINE.stats()["sends"]
        idle_passes(5)
        self.assertGreater(
            ENGINE.stats()["prefix_memo_hits"], hits, "an idle pass re-evaluated the prefix"
        )
        self.assertEqual(
            ENGINE.stats()["prefix_clones"], clones, "an idle pass cloned the object"
        )
        self.assertEqual(
            ENGINE.stats()["sends"], sends, "an idle pass asked for an unchanged frame"
        )


class QuietAnswerTests(RequestLifecycleTestCase):
    """An answer already applied is a complete no-op."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.obj, cls.stage = cls.add_cube("Quiet")
        # A native modifier before the stage: prefix evaluation without the memo
        # would clone and tag the depsgraph on every pass.
        pre = cls.obj.modifiers.new("PreBevel", "BEVEL")
        pre.width = 0.2
        cls.obj.modifiers.move(len(cls.obj.modifiers) - 1, 0)
        cls.report = cls.report_path("quiet")
        cls.fake = start_fake(cls.stage.key, cls.report, "--echo-after", "1", port=cls.port)
        cls.await_client()
        scrub(cls.obj, 1)
        pump(until=lambda: cls.stage.state == "ok" and report_of(cls.report).get("echoes", 0) >= 1)
        pump(seconds=0.3)

    @classmethod
    def tearDownClass(cls):
        stop_fake(getattr(cls, "fake", None))
        super().tearDownClass()

    def test_01_repeated_identical_response_is_quiet(self):
        stats = dict(ENGINE.stats())
        dirty = set(ENGINE.dirty)
        pump(seconds=0.5)
        self.assertEqual(ENGINE.stats()["applies"], stats["applies"], "a no-op response applied geometry")
        self.assertEqual(ENGINE.stats()["tags"], stats["tags"], "a no-op response tagged the depsgraph")
        self.assertEqual(ENGINE.stats()["sends"], stats["sends"], "a no-op response sent a request")
        self.assertFalse(ENGINE.dirty, "a no-op response left the engine dirty")
        self.assertEqual(self.stage.state, "ok")
        self.assertEqual(dirty, set())

    def test_02_event_loop_idle_is_quiet(self):
        # Drain first: a session evaluates continuously, `-b` does not, and `pump`
        # leaves the reports of its own passes (the clone the engine makes, the
        # visibility write) queued for the next evaluation. Draining them is what
        # "idle" means; the loop below is the part that must not clone.
        bpy.context.view_layer.update()
        engine_triggers._timer()
        stats = dict(ENGINE.stats())
        for _ in range(5):
            bpy.context.view_layer.update()
            engine_triggers._timer()
        self.assertEqual(ENGINE.stats()["prefix_clones"], stats["prefix_clones"], "idle passes cloned the prefix")
        self.assertEqual(ENGINE.stats()["applies"], stats["applies"], "idle passes applied geometry")
        self.assertEqual(ENGINE.stats()["tags"], stats["tags"], "idle passes tagged the depsgraph")
        self.assertEqual(ENGINE.stats()["sends"], stats["sends"], "idle passes sent a request")
        self.assertFalse(ENGINE.dirty, "the engine never settled")
        self.assertEqual(self.stage.state, "ok")

    def test_03_modifier_edits_reach_grasshopper(self):
        # A native prefix modifier edit must invalidate the memo even though the
        # base mesh did not change.
        sends = ENGINE.stats()["sends"]
        pre = self.obj.modifiers.get("PreBevel")
        self.assertIsNotNone(pre)
        pre.segments = 2
        # A session evaluates after every write, and the evaluation is what
        # carries the change to the engine now (the depsgraph is the oracle for
        # what moved); `-b` has no loop to do it, so it is asked for.
        bpy.context.view_layer.update()
        ENGINE.mark_object_dirty(self.obj)
        self.assertTrue(
            pump(seconds=8.0, until=lambda: ENGINE.stats()["sends"] > sends),
            "a prefix modifier edit did not invalidate the memo",
        )
        self.assertTrue(
            pump(until=lambda: report_of(self.report).get("received_faces", 0) > 26),
            "the edit did not reach Grasshopper",
        )
        self.assertEqual(self.stage.state, "ok")


class RemovedModeFieldsTests(RequestLifecycleTestCase):
    """A 0.6-like stage carries fields this build no longer declares.

    Blender drops unknown saved RNA properties on load, so the migration is the
    absence of `live`, `live_frame` and `generation`, not a shim that reads
    them. A stage with those names as ordinary custom properties must still open
    and settle: unknown data is ignored, not interpreted.
    """

    def test_01_the_removed_mode_fields_are_not_part_of_the_record(self):
        _obj, stage = self.add_cube("Migrated")
        fields = set(stage.bl_rna.properties.keys())
        for removed in ("live", "live_frame", "generation"):
            self.assertNotIn(
                removed,
                fields,
                "the removed %s field is back in the stage record" % removed,
            )

    def test_02_a_stage_with_legacy_extra_properties_still_runs(self):
        obj, _stage = self.add_cube("LegacyData")
        obj["live"] = True
        obj["live_frame"] = 12.0
        obj["generation"] = 3
        engine_triggers._on_load()
        ENGINE.mark_object_dirty(obj)
        self.assertTrue(
            pump(until=lambda: not ENGINE.dirty),
            "a stage with legacy custom properties never settled",
        )
        self.assertIn(
            obj.gh_stack.stages[0].state,
            (self.stack_mod.STATE_DISCONNECTED, self.stack_mod.STATE_UNBOUND),
        )


class AnswerRoutingTests(RequestLifecycleTestCase):
    """Answers are applied by rid, and only the current rid is applied."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.obj, cls.stage = cls.add_cube("Answers")
        cls.record = cls.stack_mod.stack_of(cls.obj)
        cls.sections = {
            "mesh.verts": np.array(
                [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
                dtype=np.float32,
            ),
            "mesh.face_counts": np.array([3], dtype=np.uint32),
            "mesh.face_indices": np.array([0, 1, 2], dtype=np.uint32),
        }

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()

    def setUp(self):
        ENGINE.clear_runtimes()
        ENGINE.dirty.clear()
        self.stage.frozen = False
        self.stage.enabled = True
        self.stage.state = self.stack_mod.STATE_WAITING
        self.stage.rid = "sha256:current"
        self.stage.applied_out_hash = "sha256:applied-before"
        self.stage.error = ""
        ENGINE.runtime(*(self.record.stack_id, self.stage.key)).pending = {
            "sha256:current": time.monotonic()
        }

    def _answer(self, rid, out_hash):
        engine_messages._handle_response(
            {
                "type": "response",
                "stage": self.stage.key,
                "rid": rid,
                "status": "ok",
                "out_hash": out_hash,
                "warnings": [],
            },
            self.sections,
        )

    def test_01_a_changed_answer_for_the_current_request_applies(self):
        applies = ENGINE.stats()["applies"]
        self._answer("sha256:current", "sha256:after")

        self.assertEqual(self.stage.applied_out_hash, "sha256:after")
        self.assertEqual(ENGINE.stats()["applies"], applies + 1)
        self.assertEqual(self.stage.state, self.stack_mod.STATE_OK)
        self.assertFalse(
            ENGINE.pending_of(*(self.record.stack_id, self.stage.key)),
            "the current answer left its request pending",
        )

    def test_02_an_answer_for_a_superseded_request_is_dropped(self):
        applies = ENGINE.stats()["applies"]
        key = (self.record.stack_id, self.stage.key)
        self._answer("sha256:older", "sha256:older-output")

        self.assertEqual(self.stage.applied_out_hash, "sha256:applied-before")
        self.assertEqual(ENGINE.stats()["applies"], applies, "a stale answer applied")
        self.assertIn("sha256:current", ENGINE.pending_of(*key))
        self.assertEqual(self.stage.rid, "sha256:current")
        self.assertTrue(
            any("dropped stale answer" in line for line in ENGINE.recent_activity(50)),
            "the dropped answer left no trace: %s" % ENGINE.recent_activity(50),
        )

    def test_03_an_unchanged_answer_is_a_no_op(self):
        self.stage.applied_out_hash = "sha256:same"
        self.stage.state = self.stack_mod.STATE_OK
        applies = ENGINE.stats()["applies"]
        tags = ENGINE.stats()["tags"]
        self._answer("sha256:current", "sha256:same")

        self.assertEqual(ENGINE.stats()["applies"], applies, "an unchanged answer applied")
        self.assertEqual(ENGINE.stats()["tags"], tags, "an unchanged answer tagged the graph")
        self.assertEqual(self.stage.state, self.stack_mod.STATE_OK)


if __name__ == "__main__":
    unittest.main()
