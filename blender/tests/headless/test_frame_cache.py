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
    import gh_stack.blender.engine as engine

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
    import gh_stack.blender.engine as engine

    bpy.context.scene.frame_set(frame)
    engine._mark_object_dirty(obj)


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
                [os.path.join(REPO, "blender", "gh_stack", "vendor"), REPO]
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
    import gh_stack.blender.helpers as helpers

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


def store_of(obj, stage):
    import gh_stack.blender.engine as engine
    import gh_stack.blender.stack as stack_mod

    return engine._store_for(stack_mod.stack_of(obj), stage)


def memory_count(obj, stage):
    import gh_stack.blender.engine as engine
    import gh_stack.blender.stack as stack_mod

    record = stack_mod.stack_of(obj)
    return len(engine._cache.get((record.stack_id, stage.key), {}))


def keyframe_scale(obj, stage, values):
    """Keyframe the stage's float parameter at frames 1..len(values)."""
    import gh_stack.blender.stack as stack_mod

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
        bpy.ops.preferences.addon_enable(module="gh_stack")
        import gh_stack.blender.engine as engine
        import gh_stack.blender.stack as stack_mod

        cls.engine = engine
        cls.stack_mod = stack_mod
        prefs = bpy.context.preferences.addons["gh_stack"].preferences
        cls.data_root = tempfile.mkdtemp(prefix="gh-stack-cache-test-")
        prefs.data_root = cls.data_root
        if cls.cache_mb is not None:
            prefs.cache_size_mb = cls.cache_mb

        engine.start_server(PORT)
        time.sleep(0.5)

    @classmethod
    def tearDownClass(cls):
        bpy.ops.preferences.addon_disable(module="gh_stack")

    @classmethod
    def add_cube(cls, label="Cache"):
        bpy.ops.mesh.primitive_cube_add(size=2.0)
        obj = bpy.context.object
        obj.name = label
        bpy.ops.gh_stack.add_stage(label=label)
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
        import gh_stack.blender.bridge as bridge

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
    """A schema change is a definition change: cached frames must not replay."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.obj, cls.stage = cls.add_cube("Schema")
        keyframe_scale(cls.obj, cls.stage, [1.0, 2.0])
        cls.report = cls.report_path("schema")
        cls.fake = start_fake(cls.stage.key, cls.report, "--schema-after", "1")
        scrub(cls.obj, 1)
        pump(until=lambda: memory_count(cls.obj, cls.stage) >= 1 and cls.stage.params[0].label.endswith("v2"))
        scrub(cls.obj, 2)
        pump(until=lambda: memory_count(cls.obj, cls.stage) == 1 and cls.stage.rid)
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
        bpy.ops.gh_stack.add_stage(label="Evict")
        cls.obj = obj
        cls.stage = obj.gh_stack.stages[0]
        keyframe_scale(cls.obj, cls.stage, [1.0, 2.0, 3.0, 4.0])
        cls.report = cls.report_path("evict")
        cls.fake = start_fake(cls.stage.key, cls.report)
        for frame in range(1, 5):
            scrub(cls.obj, frame)
            pump(seconds=15.0, until=lambda f=frame: requests_of(cls.report) >= f and cls.stage.state in ("ok", "error"))
        cls.store = store_of(cls.obj, cls.stage)

    @classmethod
    def tearDownClass(cls):
        stop_fake(getattr(cls, "fake", None))
        super().tearDownClass()

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


class UncachedFrameTests(FrameCacheTestCase):
    """Playback never blocks, and a stale answer never replaces the last good
    output of a frame the scene has already moved past."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.obj, cls.stage = cls.add_cube("Slow")
        keyframe_scale(cls.obj, cls.stage, [1.0, 2.0, 3.0])
        cls.report = cls.report_path("slow")
        cls.fake = start_fake(cls.stage.key, cls.report, "--delay-ms", "800")
        scrub(cls.obj, 1)
        pump(seconds=10.0, until=lambda: cls.stage.state == "ok")
        pump(until=lambda: abs(helper_max(cls.obj, cls.stage.key) - 1.0) < 0.01)
        cls.applies = cls.engine.stats["applies"]

    @classmethod
    def tearDownClass(cls):
        stop_fake(getattr(cls, "fake", None))
        super().tearDownClass()

    def test_01_uncached_frames_keep_the_last_good_output(self):
        sends = self.engine.stats["sends"]
        type(self).sends_before_answer = sends
        scrub(self.obj, 2)
        self.assertTrue(
            pump(seconds=5.0, until=lambda: self.engine.stats["sends"] == sends + 1),
            "the uncached frame was never requested",
        )
        self.assertEqual(self.stage.state, "waiting")

        scrub(self.obj, 3)  # the playhead moves on while the answer is in flight
        pump(seconds=0.4)
        self.assertEqual(
            self.engine.stats["sends"], sends + 1, "a second request went out while one was in flight"
        )
        self.assertEqual(self.stage.state, "waiting")
        self.assertAlmostEqual(helper_max(self.obj, self.stage.key), 1.0, places=2)

    def test_02_wanted_frame_is_fetched_after_the_answer(self):
        # The frame-2 answer arrives, is cached, and must not be displayed; the
        # wanted frame (3) is requested next and applies.
        sends = type(self).sends_before_answer
        self.assertTrue(
            pump(seconds=10.0, until=lambda: self.engine.stats["sends"] >= sends + 2),
            "the wanted frame was never requested after the answer",
        )
        pump(seconds=0.3)
        self.assertEqual(self.engine.stats["applies"], self.applies, "a stale answer was applied")
        self.assertAlmostEqual(helper_max(self.obj, self.stage.key), 1.0, places=2)

        self.assertTrue(
            pump(seconds=10.0, until=lambda: abs(helper_max(self.obj, self.stage.key) - 3.0) < 0.01),
            "the wanted frame never applied",
        )
        self.assertEqual(self.stage.state, "ok")


if __name__ == "__main__":
    unittest.main()
