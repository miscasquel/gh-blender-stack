"""Layer B, the simulation-style frame cache (handoff 2026-10-12, W2).

Run with:

    scripts/test-blender

What it proves (handoff tests 1-8):
1. Progressive fill: scrubbing keyframed frames requests one frame each and
   stores them in memory and on disk.
2. Replay: with Grasshopper gone, the same frames apply from disk, no requests.
3. Reset on a GH-side change: a same-rid response with a different out hash
   bumps the generation and drops the stage's frames; they are re-requested.
4. Reset on a schema change, the same way.
5. Quiet: a repeated identical response applies nothing and tags nothing.
6. Quiet idle: with an event loop evaluating between passes, nothing is
   cloned, applied or tagged once the stack has settled.
7. Eviction: a tiny budget drops the oldest frames and playback still works.
8. Uncached frames never block: the last good output stays until the wanted
   frame's answer arrives; a stale answer is cached, not displayed.
9. A time-driven prefix: a native modifier before the stage that reads scene time
   moves the geometry every frame, so each frame gets its own request and the
   cache keys them apart. This is the case that made the whole cache inert — the
   prefix was memoized against a revision that did not include the frame, so the
   same request ID came back for every frame and one frame's geometry answered
   them all.
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

PORT = 9146
SCALE = "00000000-0000-4000-8000-0000000000aa"


def pump(seconds=8.0, until=None):
    """Drive the engine timer directly: bpy.app.timers is inert under -b (S2)."""
    import stageloom.blender.engine as engine

    deadline = time.time() + seconds
    while time.time() < deadline:
        engine._timer()
        if until is not None and until():
            return True
        time.sleep(0.01)
    return until is None


def scrub(obj, frame):
    """Move the timeline and make sure the engine sees it (frame_change_post is
    not guaranteed to run under -b)."""
    import stageloom.blender.engine as engine

    bpy.context.scene.frame_set(frame)
    engine._mark_object_dirty(obj)


def pump_dirty(obj, seconds=8.0, until=None):
    """Like pump, but keeps the object dirty. The request timeout is only
    noticed on a pass, so a test waiting for a retry has to ask for passes (a
    scene change would do the same in a session)."""
    import stageloom.blender.engine as engine

    deadline = time.time() + seconds
    while time.time() < deadline:
        engine._mark_object_dirty(obj)
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
    """The largest absolute coordinate in a stage's mesh helper: the fake scales
    the whole mesh by the parameter, so this is the parameter value."""
    import stageloom.blender.helpers as helpers

    helper = helpers.helper_for(obj, stage_key, "mesh")
    if helper is None:
        return 0.0
    mesh = helper.data
    count = len(mesh.vertices)
    if not count:
        return 0.0
    co = np.empty(count * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", co)
    return float(np.abs(co.reshape(-1, 3)).max())


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


def store_of(obj, stage):
    import stageloom.blender.engine as engine
    import stageloom.blender.stack as stack_mod

    return engine._store_for(stack_mod.stack_of(obj), stage)


def memory_count(obj, stage):
    import stageloom.blender.engine as engine
    import stageloom.blender.stack as stack_mod

    record = stack_mod.stack_of(obj)
    return len(engine._cache.get((record.stack_id, stage.key), {}))


def keyframe_scale(obj, stage, values):
    """Keyframe the stage's float parameter at frames 1..len(values)."""
    import stageloom.blender.stack as stack_mod

    name = stack_mod.param_prop_name(stage.key, SCALE)
    if name not in obj:
        obj[name] = 1.0
    for frame, value in enumerate(values, start=1):
        obj[name] = float(value)
        obj.keyframe_insert(data_path='["%s"]' % name, frame=frame)


class FrameCacheTestCase(unittest.TestCase):
    """Shared setup: fresh scene, temp data root, one stack object."""

    cache_mb = None  # override to shrink the disk budget in a subclass

    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="stageloom")
        import stageloom.blender.engine as engine
        import stageloom.blender.stack as stack_mod

        cls.engine = engine
        cls.stack_mod = stack_mod
        prefs = bpy.context.preferences.addons["stageloom"].preferences
        cls.data_root = tempfile.mkdtemp(prefix="stageloom-cache-test-")
        prefs.data_root = cls.data_root
        if cls.cache_mb is not None:
            prefs.cache_size_mb = cls.cache_mb

        engine.start_server(PORT)
        time.sleep(0.5)

    @classmethod
    def tearDownClass(cls):
        bpy.ops.preferences.addon_disable(module="stageloom")

    @classmethod
    def await_client(cls, seconds=15.0):
        """The fake connects on its own retry loop, so a test that needs a live
        client must wait for one instead of assuming it: without this a class can
        race the connect and spend its pumps with nothing on the other end, which
        showed up as an unrelated assertion failing only in a full-suite run.

        This deliberately does not tick the engine. The client count is set by the
        socket thread as soon as the handshake completes, and running a pass here
        would send a request early — which shifts the response counts that
        `--echo-after`, `--gh-edit-after`, `--schema-after` and `--forget-after`
        are scripted against."""
        import stageloom.blender.bridge as bridge

        deadline = time.time() + seconds
        while time.time() < deadline:
            if bridge.state["clients"] > 0:
                return
            time.sleep(0.02)
        raise AssertionError("the fake never connected to the engine")

    @classmethod
    def add_cube(cls, label="Cache"):
        bpy.ops.mesh.primitive_cube_add(size=2.0)
        obj = bpy.context.object
        obj.name = label
        bpy.ops.stageloom.add_stage(label=label)
        return obj, obj.gh_stack.stages[0]

    @classmethod
    def report_path(cls, name):
        path = os.path.join(tempfile.gettempdir(), "fake_gh_cache_%s.json" % name)
        if os.path.exists(path):
            os.remove(path)
        return path


class ProgressiveFillTests(FrameCacheTestCase):
    """Scrub frames 1-5, then replay them with Grasshopper gone."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.obj, cls.stage = cls.add_cube("Fill")
        keyframe_scale(cls.obj, cls.stage, [1.0, 2.0, 3.0, 4.0, 5.0])
        cls.report = cls.report_path("fill")
        cls.fake = start_fake(cls.stage.key, cls.report)
        cls.await_client()

    @classmethod
    def tearDownClass(cls):
        stop_fake(getattr(cls, "fake", None))
        super().tearDownClass()

    def test_01_progressive_fill(self):
        for frame in range(1, 6):
            scrub(self.obj, frame)
            filled = pump(until=lambda f=frame: memory_count(self.obj, self.stage) >= f)
            self.assertTrue(filled, "frame %d never filled the cache" % frame)
        store = store_of(self.obj, self.stage)
        self.assertEqual(memory_count(self.obj, self.stage), 5)
        self.assertEqual(len(store), 5)
        self.assertGreater(store.index.total_size(), 0)
        self.assertGreaterEqual(requests_of(self.report), 5)

    def test_02_replay_with_grasshopper_closed(self):
        import stageloom.blender.bridge as bridge

        stop_fake(type(self).fake)
        type(self).fake = None

        def gone():
            self.engine._timer()
            return not bridge.state["connected"]

        self.assertTrue(pump(seconds=5.0, until=gone), "the fake never disconnected")
        self.engine._cache.clear()
        sends_before = self.engine.stats["sends"]

        # Replay backwards: any order works, frames are keyed by content.
        for frame in (5, 4, 3, 2, 1):
            scrub(self.obj, frame)
            seen = pump(until=lambda f=frame: abs(helper_max(self.obj, self.stage.key) - f) < 0.01)
            self.assertTrue(seen, "frame %d did not replay from disk" % frame)
        self.assertEqual(self.engine.stats["sends"], sends_before, "replay sent a request")
        self.assertFalse(bridge.state["connected"])


class GhEditResetTests(FrameCacheTestCase):
    """The same rid answered differently: the definition changed in Grasshopper."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.obj, cls.stage = cls.add_cube("Edit")
        keyframe_scale(cls.obj, cls.stage, [1.0, 2.0, 3.0])
        cls.report = cls.report_path("edit")
        cls.fake = start_fake(cls.stage.key, cls.report, "--gh-edit-after", "3")
        cls.await_client()
        # Cache two frames first; the third response triggers the GH-side edit
        # inside test_01, where the generation is captured beforehand.
        for frame in (1, 2):
            scrub(cls.obj, frame)
            pump(until=lambda f=frame: memory_count(cls.obj, cls.stage) >= f)
        cls.generation_before = cls.stage.generation

    @classmethod
    def tearDownClass(cls):
        stop_fake(getattr(cls, "fake", None))
        super().tearDownClass()

    def test_01_changed_answer_resets(self):
        scrub(self.obj, 3)
        bumped = pump(
            seconds=5.0,
            until=lambda: self.stage.generation > self.generation_before,
        )
        self.assertTrue(bumped, "the GH-side edit did not reset the cache (report=%s)" % report_of(self.report))
        self.assertTrue(
            pump(until=lambda: abs(helper_max(self.obj, self.stage.key) - 42.0) < 0.01),
            "the edited answer never applied",
        )
        store = store_of(self.obj, self.stage)
        # Everything cached for the old definition is gone; only the edited
        # answer (stored after the reset) remains.
        self.assertEqual(len(store), 1)
        self.assertEqual(memory_count(self.obj, self.stage), 1)

    def test_02_reset_frame_is_requested_again(self):
        requests = requests_of(self.report)
        scrub(self.obj, 1)
        again = pump(until=lambda: requests_of(self.report) > requests)
        self.assertTrue(again, "the pruned frame was never re-requested")
        self.assertTrue(
            pump(until=lambda: abs(helper_max(self.obj, self.stage.key) - 1.0) < 0.01),
            "the re-requested frame never applied",
        )


class SchemaResetTests(FrameCacheTestCase):
    """A schema change is a definition change: cached frames must not replay.

    The script visits both frames before the change, so the bump has two
    old-definition frames to prune: frame 2 is re-evaluated immediately (the
    playhead is there), and frame 1 must be re-requested on the way back. That
    ordering matters: were frame 1 the current frame when the schema landed,
    the engine would re-evaluate it right away and it would be cached under the
    *new* generation by the time a test asked for it, which made the old
    "requested again" assertion a race it only won by unlucky bookkeeping."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.obj, cls.stage = cls.add_cube("Schema")
        keyframe_scale(cls.obj, cls.stage, [1.0, 2.0])
        cls.report = cls.report_path("schema")
        cls.fake = start_fake(cls.stage.key, cls.report, "--schema-after", "2")
        cls.await_client()
        scrub(cls.obj, 1)
        pump(until=lambda: memory_count(cls.obj, cls.stage) >= 1)
        scrub(cls.obj, 2)
        pump(until=lambda: report_of(cls.report).get("responses", 0) >= 2)

        # The schema arrives after response 2: both old frames are pruned, the
        # playhead's frame is re-evaluated, and only then is the request budget
        # recorded — so the next request is genuinely frame 1's.
        def bumped():
            return cls.stage.params[0].label.endswith("v2") and cls.stage.state == "ok"

        pump(until=bumped)
        cls.generation = cls.stage.generation
        cls.requests = requests_of(cls.report)

    @classmethod
    def tearDownClass(cls):
        stop_fake(getattr(cls, "fake", None))
        super().tearDownClass()

    def test_01_schema_change_resets(self):
        self.assertTrue(self.stage.params[0].label.endswith("v2"), self.stage.params[0].label)
        self.assertGreaterEqual(self.generation, 1, "a schema change did not bump the generation")
        store = store_of(self.obj, self.stage)
        for rid in store.rids():
            entry = store.index.get(rid)
            self.assertEqual(entry.generation, self.generation)

    def test_02_old_frame_is_requested_again(self):
        self.assertGreaterEqual(requests_of(self.report), 2)
        scrub(self.obj, 1)
        again = pump(until=lambda: requests_of(self.report) > self.requests)
        self.assertTrue(again, "a frame from the old schema was served from the cache")


class NoOpQuietTests(FrameCacheTestCase):
    """A cached frame already applied is a complete no-op."""

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
        cls.fake = start_fake(cls.stage.key, cls.report, "--echo-after", "1")
        cls.await_client()
        scrub(cls.obj, 1)
        pump(until=lambda: cls.stage.state == "ok" and report_of(cls.report).get("echoes", 0) >= 1)
        pump(seconds=0.3)

    @classmethod
    def tearDownClass(cls):
        stop_fake(getattr(cls, "fake", None))
        super().tearDownClass()

    def test_01_repeated_identical_response_is_quiet(self):
        stats = dict(self.engine.stats)
        dirty = set(self.engine._dirty)
        pump(seconds=0.5)
        self.assertEqual(self.engine.stats["applies"], stats["applies"], "a no-op response applied geometry")
        self.assertEqual(self.engine.stats["tags"], stats["tags"], "a no-op response tagged the depsgraph")
        self.assertEqual(self.engine.stats["sends"], stats["sends"], "a no-op response sent a request")
        self.assertFalse(self.engine._dirty, "a no-op response left the engine dirty")
        self.assertEqual(self.stage.state, "ok")
        self.assertEqual(dirty, set())

    def test_02_event_loop_idle_is_quiet(self):
        # Drain first: a session evaluates continuously, `-b` does not, and `pump`
        # leaves the reports of its own passes (the clone the engine makes, the
        # visibility write) queued for the next evaluation. Draining them is what
        # "idle" means; the loop below is the part that must not clone.
        bpy.context.view_layer.update()
        self.engine._timer()
        stats = dict(self.engine.stats)
        for _ in range(5):
            bpy.context.view_layer.update()
            self.engine._timer()
        self.assertEqual(self.engine.stats["prefix_clones"], stats["prefix_clones"], "idle passes cloned the prefix")
        self.assertEqual(self.engine.stats["applies"], stats["applies"], "idle passes applied geometry")
        self.assertEqual(self.engine.stats["tags"], stats["tags"], "idle passes tagged the depsgraph")
        self.assertEqual(self.engine.stats["sends"], stats["sends"], "idle passes sent a request")
        self.assertFalse(self.engine._dirty, "the engine never settled")
        self.assertEqual(self.stage.state, "ok")

    def test_03_modifier_edits_reach_grasshopper(self):
        # A native prefix modifier edit must invalidate the memo even though the
        # base mesh did not change.
        sends = self.engine.stats["sends"]
        pre = self.obj.modifiers.get("PreBevel")
        self.assertIsNotNone(pre)
        pre.segments = 2
        # A session evaluates after every write, and the evaluation is what
        # carries the change to the engine now (the depsgraph is the oracle for
        # what moved); `-b` has no loop to do it, so it is asked for.
        bpy.context.view_layer.update()
        self.engine._mark_object_dirty(self.obj)
        self.assertTrue(
            pump(seconds=8.0, until=lambda: self.engine.stats["sends"] > sends),
            "a prefix modifier edit did not invalidate the memo",
        )
        self.assertTrue(
            pump(until=lambda: report_of(self.report).get("received_faces", 0) > 26),
            "the edit did not reach Grasshopper",
        )
        self.assertEqual(self.stage.state, "ok")


class DocumentResetTests(FrameCacheTestCase):
    """Reconnecting with a different document drops the frames cached against
    the old one."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.obj, cls.stage = cls.add_cube("Doc")
        keyframe_scale(cls.obj, cls.stage, [1.0, 2.0])
        cls.report = cls.report_path("doc")
        cls.fake = start_fake(cls.stage.key, cls.report, "--doc", "first.gh")
        cls.await_client()
        scrub(cls.obj, 1)
        pump(until=lambda: memory_count(cls.obj, cls.stage) >= 1)
        stop_fake(cls.fake)
        cls.fake = None

    @classmethod
    def tearDownClass(cls):
        stop_fake(getattr(cls, "fake", None))
        super().tearDownClass()

    def test_01_document_change_resets(self):
        generation = self.stage.generation
        type(self).fake = start_fake(self.stage.key, self.report, "--doc", "second.gh")
        self.await_client()

        def reset():
            self.engine._timer()
            return self.stage.generation > generation

        self.assertTrue(pump(seconds=8.0, until=reset), "a different document did not reset the cache")
        type(self).reset_generation = self.stage.generation
        store = store_of(self.obj, self.stage)
        self.assertEqual(len(store), 0, "frames from the old document survived")
        self.assertEqual(memory_count(self.obj, self.stage), 0)

    def test_02_frame_is_fetched_under_the_new_generation(self):
        scrub(self.obj, 1)
        expected = type(self).reset_generation

        def fetched():
            store = store_of(self.obj, self.stage)
            return len(store) >= 1 and all(
                store.index.get(rid).generation == expected for rid in store.rids()
            )

        self.assertTrue(
            pump(seconds=8.0, until=fetched),
            "the frame was not fetched under the new generation (report=%s)" % report_of(self.report),
        )
        self.assertTrue(pump(until=lambda: abs(helper_max(self.obj, self.stage.key) - 1.0) < 0.01))


class EvictionTests(FrameCacheTestCase):
    """A tiny disk budget evicts oldest-first; playback still works."""

    cache_mb = 1

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        bpy.ops.mesh.primitive_grid_add(x_subdivisions=225, y_subdivisions=225, size=2.0)
        obj = bpy.context.object
        obj.name = "Evict"
        bpy.ops.stageloom.add_stage(label="Evict")
        cls.obj = obj
        cls.stage = obj.gh_stack.stages[0]
        keyframe_scale(cls.obj, cls.stage, [1.0, 2.0, 3.0, 4.0])
        cls.report = cls.report_path("evict")
        cls.fake = start_fake(cls.stage.key, cls.report)
        cls.await_client()
        for frame in range(1, 5):
            scrub(cls.obj, frame)
            pump(seconds=15.0, until=lambda f=frame: requests_of(cls.report) >= f and cls.stage.state in ("ok", "error"))
        cls.store = store_of(cls.obj, cls.stage)

    @classmethod
    def tearDownClass(cls):
        stop_fake(getattr(cls, "fake", None))
        super().tearDownClass()

    def test_00_hot_cache_stores_wire_bytes_with_an_exact_total(self):
        key = (self.obj.gh_stack.stack_id, self.stage.key)
        cache = self.engine._cache.get(key, {})
        self.assertTrue(cache)
        total = sum(len(raw) + len(out_hash) for raw, out_hash in cache.values())
        self.assertEqual(self.engine._cache_bytes.get(key), total)
        for raw, _out_hash in cache.values():
            self.assertIsInstance(raw, (bytes, bytearray))

    def test_01_eviction_keeps_the_budget(self):
        self.assertGreaterEqual(requests_of(self.report), 4)
        # Four ~1 MB frames do not fit a 1 MB budget: only the newest survives.
        self.assertLess(len(self.store), 4)
        self.assertGreaterEqual(len(self.store), 1)

    def test_02_evicted_frame_is_requested_again(self):
        # A fresh session starts with an empty memory cache: the frame that
        # fell off the disk budget must be fetched again, not replayed.
        self.engine._cache.clear()
        requests = requests_of(self.report)
        scrub(self.obj, 1)
        again = pump(seconds=15.0, until=lambda: requests_of(self.report) > requests)
        self.assertTrue(again, "the evicted frame was served from disk")
        self.assertTrue(
            pump(seconds=15.0, until=lambda: abs(helper_max(self.obj, self.stage.key) - 1.0) < 0.01),
            "the re-requested frame never applied",
        )


class OneEvaluationAtATimeTests(FrameCacheTestCase):
    """A frame that arrives while an answer is outstanding is not requested. The
    stage waits, then evaluates whatever frame the playhead is on when the answer
    lands: it never queues frames behind a working Grasshopper."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.obj, cls.stage = cls.add_cube("Slow")
        keyframe_scale(cls.obj, cls.stage, [1.0, 2.0, 3.0])
        cls.report = cls.report_path("slow")
        cls.fake = start_fake(cls.stage.key, cls.report, "--delay-ms", "800")
        cls.await_client()
        scrub(cls.obj, 1)
        pump(seconds=10.0, until=lambda: cls.stage.state == "ok")
        pump(until=lambda: abs(helper_max(cls.obj, cls.stage.key) - 1.0) < 0.01)

    @classmethod
    def tearDownClass(cls):
        stop_fake(getattr(cls, "fake", None))
        super().tearDownClass()

    def test_01_a_frame_while_one_is_outstanding_is_not_queued(self):
        sends = self.engine.stats["sends"]
        type(self).sends_with_one_in_flight = sends + 1
        scrub(self.obj, 2)
        self.assertTrue(
            pump(seconds=5.0, until=lambda: self.engine.stats["sends"] == sends + 1),
            "the uncached frame was never requested",
        )
        self.assertEqual(self.stage.state, "waiting")

        scrub(self.obj, 3)  # the playhead moves on while the answer is in flight
        pump(seconds=0.4)
        self.assertEqual(
            self.engine.stats["sends"],
            sends + 1,
            "a second request went out while the first was still outstanding",
        )
        self.assertEqual(self.stage.state, "waiting")
        # Nothing has been displayed for either frame yet: their answers are not in.
        self.assertAlmostEqual(helper_max(self.obj, self.stage.key), 1.0, places=2)

    def test_02_the_answer_lands_and_the_current_frame_is_asked_for_next(self):
        sends = type(self).sends_with_one_in_flight
        self.assertTrue(
            pump(seconds=10.0, until=lambda: self.engine.stats["sends"] > sends),
            "the frame the playhead moved to was never requested after the answer",
        )
        self.assertTrue(
            pump(seconds=10.0, until=lambda: abs(helper_max(self.obj, self.stage.key) - 3.0) < 0.01),
            "the frame the playhead stopped on never applied",
        )
        self.assertEqual(self.stage.state, "ok")
        # The answer for the frame the playhead had left was stored, not dropped: a
        # later pass over it replays from the cache without asking again.
        self.assertGreaterEqual(memory_count(self.obj, self.stage), 3)


class PlaybackTests(FrameCacheTestCase):
    """A scrub evaluates one frame per round trip and stores what it evaluated.
    Frames merely passed over are not queued: that backlog is what made the client
    stop responding while it worked through frames nobody was looking at."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.obj, cls.stage = cls.add_cube("Play")
        keyframe_scale(cls.obj, cls.stage, [float(f) for f in range(1, 9)])
        cls.report = cls.report_path("play")
        # A round trip slower than the playhead step, as a real GH solve can be.
        cls.fake = start_fake(cls.stage.key, cls.report, "--delay-ms", "400")
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
                cls.engine._timer()
                time.sleep(0.005)

    def test_01_a_scrub_does_not_queue_every_frame_it_passes(self):
        before = self.engine.stats["sends"]
        type(self).play(range(1, 9))
        requested = self.engine.stats["sends"] - before
        self.assertLess(
            requested,
            8,
            "a fast scrub asked for every frame it passed over (%d of 8): that is the "
            "backlog that blocks the client" % requested,
        )
        self.assertGreaterEqual(requested, 1, "a scrub asked for nothing at all")

    def test_02_the_frame_it_stopped_on_lands_then_replays(self):
        cls = type(self)
        cls.play([8], hold=0.8)
        self.assertTrue(
            pump(seconds=15.0, until=lambda: abs(helper_max(self.obj, cls.stage.key) - 8.0) < 0.01),
            "the frame the playhead stopped on was never evaluated",
        )
        self.assertEqual(self.stage.state, "ok")

        sends = self.engine.stats["sends"]
        cls.play([8], hold=0.3)
        self.assertEqual(self.engine.stats["sends"], sends, "a cached frame was asked for again")



class OneRequestOutstandingTests(FrameCacheTestCase):
    """The client is never handed a second request while one is in flight.

    A cached frame answered while a request was outstanding used to clear that
    request's bookkeeping, so the next frame's pass saw an open gate and sent a
    second request. The client solves serially, so two requests in flight is a
    backlog the user feels; worse, a client that coalesces its solves can then
    answer a request the engine has stopped waiting for, which is how a stage came
    to look stuck until a manual Push. Measured before the fix: a peak of two
    requests in flight, and 11 requests where 7 do the same work."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.obj, cls.stage = cls.add_cube("Outstanding")
        keyframe_scale(cls.obj, cls.stage, [float((f % 7) + 1) for f in range(1, 21)])
        cls.report = cls.report_path("outstanding")
        cls.fake = start_fake(cls.stage.key, cls.report, "--delay-ms", "300")
        cls.await_client()

    @classmethod
    def tearDownClass(cls):
        stop_fake(getattr(cls, "fake", None))
        super().tearDownClass()

    def test_01_only_one_request_is_ever_outstanding(self):
        import stageloom.blender.server as server

        engine = self.engine
        seen = {"outstanding": 0, "peak": 0}
        real_send = server.SERVER.send
        real_response = engine._handle_response

        def counting_send(frame):
            sent = real_send(frame)
            if sent:
                seen["outstanding"] += 1
                seen["peak"] = max(seen["peak"], seen["outstanding"])
            return sent

        def counting_response(header, sections, raw=None):
            seen["outstanding"] = max(0, seen["outstanding"] - 1)
            return real_response(header, sections, raw)

        server.SERVER.send = counting_send
        engine._handle_response = counting_response
        try:
            # Back and forth, so cached and uncached frames interleave: that mix is
            # what opened the gate early.
            for frame in list(range(1, 21)) + list(range(20, 0, -1)):
                scrub(self.obj, frame)
                until = time.time() + 0.04
                while time.time() < until:
                    engine._timer()
                    time.sleep(0.004)
            pump(seconds=2.0)
        finally:
            server.SERVER.send = real_send
            engine._handle_response = real_response

        self.assertGreater(seen["peak"], 0, "no request was ever sent")
        self.assertEqual(
            seen["peak"],
            1,
            "up to %d requests were outstanding at once; the client solves serially "
            "and a second request is a backlog it works through after the user has "
            "moved on" % seen["peak"],
        )


class UnknownRefTests(FrameCacheTestCase):
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
        cls.fake = start_fake(cls.stage.key, cls.report, "--forget-after", "1")
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
            self.engine.stats["timeouts"], 0, "the recovery went through a timeout instead"
        )


class UnknownRefResponseTests(UnknownRefTests):
    """The same recovery when the refusal arrives as an error-status response."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        stop_fake(cls.fake)
        cls.report = cls.report_path("unknown_ref_response")
        cls.fake = start_fake(
            cls.stage.key, cls.report, "--forget-after", "1", "--refusal", "response"
        )
        cls.await_client()
        cls.stage.geom_hash = ""
        scrub(cls.obj, 1)
        pump(seconds=10.0, until=lambda: cls.stage.state == "ok")


class SolveFailureTests(FrameCacheTestCase):
    """A failed solve answers immediately, and is not immediately re-asked.

    P1-1's Blender half: when a Stage Out fails, the plugin answers with an
    error response (protocol §4.4); the stage must land on `error` without
    waiting out the 30 s timeout. The engine must then wait for a change — a
    component that just failed fails again, so an instant retry is a request
    loop against the client (protocol §7: retry on the next change or Push).
    The plugin's own half cannot be reached by any suite that drives the fake.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.obj, cls.stage = cls.add_cube("Fails")
        keyframe_scale(cls.obj, cls.stage, [1.0, 2.0, 3.0])
        cls.report = cls.report_path("solve_failure")
        # The second response fails; the first (frame 1) settles the class, and
        # the third is the recovery after the next change.
        cls.fake = start_fake(cls.stage.key, cls.report, "--fail-after", "2")
        cls.await_client()
        scrub(cls.obj, 1)
        pump(seconds=10.0, until=lambda: cls.stage.state == "ok")

    @classmethod
    def tearDownClass(cls):
        stop_fake(getattr(cls, "fake", None))
        super().tearDownClass()

    def test_01_error_arrives_without_the_timeout(self):
        self.assertEqual(self.stage.state, "ok", self.stage.error)
        timeouts_before = self.engine.stats["timeouts"]
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
            self.engine.stats["timeouts"], timeouts_before, "the error went through a timeout"
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


class CoalescedAnswerTests(FrameCacheTestCase):
    """A client that coalesces a burst of requests — the plugin collapses
    ScheduleSolution calls and answers only the newest — must not strand the
    stage.

    The class of bug this guards (P0-4): an early gate opening sent a second
    request while the first was outstanding; the client answered the newest and
    dropped the first; the engine waited on the dropped rid and the stage looked
    stuck until a Push. Here the burst is forced deterministically: the timeout
    retry carries a changed parameter, so the dropped request and the answered
    one have different rids, and the engine's apply rule has to take the newest.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        prefs = bpy.context.preferences.addons["stageloom"].preferences
        cls.timeout_before = prefs.timeout
        prefs.timeout = 1.0
        cls.obj, cls.stage = cls.add_cube("Coalesce")
        cls.report = cls.report_path("coalesce")
        cls.fake = start_fake(cls.stage.key, cls.report, "--coalesce", "--delay-ms", "1800")
        cls.await_client()

    @classmethod
    def tearDownClass(cls):
        bpy.context.preferences.addons["stageloom"].preferences.timeout = cls.timeout_before
        stop_fake(getattr(cls, "fake", None))
        super().tearDownClass()

    def test_01_a_dropped_request_is_recovered_by_the_retry(self):
        timeouts_before = self.engine.stats["timeouts"]
        self.engine._mark_object_dirty(self.obj)
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
        self.assertGreaterEqual(self.engine.stats["timeouts"], timeouts_before + 1)


class TimeDrivenPrefixTests(FrameCacheTestCase):
    """A native modifier before the stage reads scene time, so the geometry fed to
    Grasshopper moves with the timeline — the case that made the frame cache inert.

    Nothing about the modifier changes between frames: the same node graph, the
    same property values. Only the evaluated prefix moves. So this fails unless the
    prefix memo's revision includes the frame, which was exactly the bug: the memo
    served one frame's geometry for every frame, the geometry hash never moved, the
    request ID never moved, and the cache answered every frame with the first
    frame's result."""

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
        cls.fake = start_fake(cls.stage.key, cls.report)
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
        before = self.engine.stats["sends"]
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
        self.assertGreaterEqual(self.engine.stats["sends"] - before, 2)

    def test_02_the_frame_moves_the_geometry_both_ways(self):
        # The second pass: both frames are cached, so this is a replay, and the
        # geometry must still follow the frame in both directions.
        first = self.at_frame(1)
        second = self.at_frame(24)
        self.assertLess(first, second, "the replay went the wrong way: %r then %r" % (first, second))

    def test_03_an_unchanged_frame_still_hits_the_memo(self):
        # The memo must keep earning its place: a pass at the frame already
        # evaluated is the wait-free no-op that lets Cycles accumulate samples.
        # The pass is forced the way the depsgraph handler forces it, because
        # `_timer` only touches objects something has marked dirty.
        self.at_frame(24)
        # A replay for a state the client was not told sends a sync request. It
        # is not an idle pass until that answer lands (a young pending would
        # short-circuit before the prefix read and prove nothing).
        record = self.stack_mod.stack_of(self.obj)
        self.assertTrue(
            pump(
                seconds=10.0,
                until=lambda: self.stage.state == "ok"
                and not self.engine._dirty
                and not self.engine._pending.get((record.stack_id, self.stage.key)),
            ),
            "the frame's sync request never settled",
        )

        def idle_passes(count):
            for _ in range(count):
                self.engine._mark_object_dirty(self.obj)
                self.engine._timer()

        idle_passes(3)
        hits = self.engine.stats["prefix_memo_hits"]
        clones = self.engine.stats["prefix_clones"]
        idle_passes(5)
        self.assertGreater(
            self.engine.stats["prefix_memo_hits"], hits, "an idle pass re-evaluated the prefix"
        )
        self.assertEqual(
            self.engine.stats["prefix_clones"], clones, "an idle pass cloned the object"
        )


if __name__ == "__main__":
    unittest.main()
