"""Layer B: generator stages (no Stage In) and live stages (no frame cache).

A generator's request names the empty geometry hash, so base geometry edits
cannot move its request id. A live stage is a stateful solver: it advances once
per delivered request, ignores the frame cache, and never resets its generation
for a same-rid answer.
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

PORT = 9156
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


def start_fake(stage_key, report, *extra, port=PORT):
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
                [os.path.join(REPO, "blender", "stageloom", "vendor"), REPO]
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


def requests_of(path):
    report = report_of(path)
    return report.get("full", 0) + report.get("ref", 0)


def helper_max(obj, stage_key):
    """The largest absolute coordinate in a stage's mesh helper."""
    import stageloom.blender.helpers as helpers

    helper = helpers.helper_for(obj, stage_key, "mesh")
    if helper is None or not len(helper.data.vertices):
        return 0.0
    count = len(helper.data.vertices)
    co = np.empty(count * 3, dtype=np.float32)
    helper.data.vertices.foreach_get("co", co)
    return float(np.abs(co.reshape(-1, 3)).max())


def settle(engine, stack_mod, objects, settled, total=25.0):
    """Pump until settled, re-arming only requests that have gone quiet.

    A lost first request during the socket transition is normal; a re-arm while
    an answer is young would starve it, so the re-arm waits for the wait to age.
    """
    deadline = time.time() + total
    while time.time() < deadline and not settled():
        quiet = True
        for obj in objects:
            record = stack_mod.stack_of(obj)
            if record is None:
                continue
            for stage in record.stages:
                age = engine.waiting_seconds(record.stack_id, stage.key)
                if age and age < 6.0:
                    quiet = False
        if quiet:
            for obj in objects:
                engine.request_again(obj)
        pump(seconds=2.0, until=settled)
    if not settled():
        states = []
        for obj in objects:
            record = stack_mod.stack_of(obj)
            if record is None:
                continue
            for stage in record.stages:
                states.append((obj.name, stage.key, stage.state, stage.error))
        raise AssertionError("the fake never settled: %s" % (states,))


class LiveTestCase(unittest.TestCase):
    """Fresh factory scene, private data root and server, one fake per class."""

    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="stageloom")
        prefs = bpy.context.preferences.addons["stageloom"].preferences
        prefs.data_root = tempfile.mkdtemp(prefix="stageloom-live-generator-")
        import stageloom.blender.engine as engine
        import stageloom.blender.stack as stack_mod

        cls.engine = engine
        cls.stack_mod = stack_mod
        engine.start_server(PORT)
        time.sleep(0.5)

    @classmethod
    def tearDownClass(cls):
        for name in ("fake", "fake_a", "fake_b"):
            stop_fake(getattr(cls, name, None))
        bpy.ops.preferences.addon_disable(module="stageloom")

    @classmethod
    def stack_object(cls, name, label=None):
        bpy.ops.mesh.primitive_cube_add(size=2.0)
        obj = bpy.context.object
        obj.name = name
        bpy.context.view_layer.objects.active = obj
        obj.select_set(True)
        bpy.ops.stageloom.add_stage(label=label or name)
        obj[cls.param_name(obj.gh_stack.stages[0], SCALE)] = 1.0
        return obj, obj.gh_stack.stages[0]

    @classmethod
    def param_name(cls, stage, param_id):
        return cls.stack_mod.param_prop_name(stage.key, param_id)

    @classmethod
    def report_path(cls, name):
        path = os.path.join(tempfile.gettempdir(), "fake_gh_live_%s.json" % name)
        if os.path.exists(path):
            os.remove(path)
        return path


class GeneratorStageTests(LiveTestCase):
    """A stage with no Stage In: no prefix read, no geometry in the request."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.obj, cls.stage = cls.stack_object("Generator")
        cls.stage.input_mode = "generator"
        cls.report = cls.report_path("generator")
        cls.fake = start_fake(cls.stage.key, cls.report)

        def settled():
            return cls.stage.state == "ok" and report_of(cls.report).get("responses", 0) >= 1

        settle(cls.engine, cls.stack_mod, [cls.obj], settled)

    def test_01_a_generator_skips_the_prefix_and_sends_empty_geometry(self):
        engine = self.engine
        record = self.stack_mod.stack_of(self.obj)
        clones = engine.stats["prefix_clones"]

        sections, geom_hash, attributes, definitions = engine._prefix_sections(
            self.obj, record, self.stage
        )

        self.assertEqual(sections, [], "a generator evaluated a prefix")
        self.assertEqual(attributes, [])
        self.assertEqual(definitions, [])
        self.assertEqual(
            geom_hash,
            engine.hashing.geometry_hash({}),
            "a generator named a geometry hash for a prefix it does not read",
        )
        self.assertEqual(
            engine.stats["prefix_clones"], clones, "a generator cloned the object"
        )
        report = report_of(self.report)
        self.assertEqual(report.get("received_verts"), 0, report)
        self.assertEqual(report.get("sections"), [], report)
        self.assertGreaterEqual(report.get("full", 0), 1, report)
        self.assertEqual(self.stage.state, "ok", self.stage.error)

    def test_02_a_base_geometry_edit_does_not_move_the_request(self):
        engine = self.engine
        rid = self.stage.rid
        sends = engine.stats["sends"]
        requests = requests_of(self.report)
        clones = engine.stats["prefix_clones"]

        self.obj.data.vertices[0].co.x += 3.0
        self.obj.data.update()
        bpy.context.view_layer.update()
        engine._mark_object_dirty(self.obj)
        self.assertTrue(
            pump(seconds=5.0, until=lambda: not engine._dirty and self.stage.state == "ok"),
            "the pass after a base edit never settled",
        )

        self.assertEqual(self.stage.rid, rid, "a base edit moved a generator's request id")
        self.assertEqual(engine.stats["sends"], sends, "a base edit sent a generator request")
        self.assertEqual(
            requests_of(self.report), requests, "a base edit reached the generator's client"
        )
        self.assertEqual(
            engine.stats["prefix_clones"], clones, "the base edit cloned a generator prefix"
        )

    def test_03_hello_presence_controls_the_mode_and_absence_keeps_it(self):
        engine = self.engine
        self.assertEqual(self.stage.input_mode, "generator")

        # An older client does not publish the field: the saved mode stays.
        engine._on_client_hello(
            {"doc": "fake.gh", "stages": [{"key": self.stage.key}]}
        )
        self.assertEqual(
            self.stage.input_mode,
            "generator",
            "a hello without has_stage_in overwrote the saved mode",
        )

        engine._on_client_hello(
            {
                "doc": "fake.gh",
                "stages": [{"key": self.stage.key, "has_stage_in": True}],
            }
        )
        self.assertEqual(self.stage.input_mode, "prefix")
        self.assertEqual(
            self.stage.geom_hash, "", "a mode change kept the old geometry claim"
        )

        engine._on_client_hello(
            {
                "doc": "fake.gh",
                "stages": [{"key": self.stage.key, "has_stage_in": False}],
            }
        )
        self.assertEqual(self.stage.input_mode, "generator")


class LiveFrameTests(LiveTestCase):
    """A live stage advances once per frame and never touches the frame cache."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.obj, cls.stage = cls.stack_object("Live")
        cls.stage.live = True
        cls.report = cls.report_path("live")
        # The fake answers its first request twice: the second answer has the
        # same rid and a changed output, the GH-side-edit shape a live stage
        # must apply without resetting its generation.
        cls.fake = start_fake(cls.stage.key, cls.report, "--gh-edit-after", "1")

        def settled():
            report = report_of(cls.report)
            return (
                cls.stage.state == "ok"
                and report.get("responses", 0) >= 1
                and report.get("edits", 0) >= 1
                and abs(helper_max(cls.obj, cls.stage.key) - 42.0) < 0.01
            )

        settle(cls.engine, cls.stack_mod, [cls.obj], settled)

    def test_01_a_same_rid_change_applies_without_a_generation_bump(self):
        generation = self.stage.generation
        self.assertEqual(
            helper_max(self.obj, self.stage.key),
            42.0,
            "the changed same-rid answer did not apply (helper max %r)"
            % helper_max(self.obj, self.stage.key),
        )

        pump(seconds=1.0, until=lambda: not self.engine._dirty)
        self.assertEqual(
            self.stage.generation,
            generation,
            "a same-rid answer reset a live stage's generation",
        )

    def test_02_a_frame_change_sends_once_and_the_same_frame_is_quiet(self):
        engine = self.engine
        sends = engine.stats["sends"]
        responses = report_of(self.report).get("responses", 0)
        rid = self.stage.rid

        bpy.context.scene.frame_set(2)
        engine._on_frame_change(bpy.context.scene)

        self.assertTrue(
            pump(seconds=5.0, until=lambda: engine.stats["sends"] == sends + 1),
            "a live stage did not send once on a frame change",
        )
        self.assertEqual(
            self.stage.rid, rid, "the frame-change request id moved; the test proves nothing"
        )
        self.assertTrue(
            pump(
                seconds=10.0,
                until=lambda: self.stage.state == "ok"
                and report_of(self.report).get("responses", 0) > responses
                and not engine._dirty,
            ),
            "the frame-change answer never settled (state=%s, error=%s)"
            % (self.stage.state, self.stage.error),
        )
        self.assertEqual(self.stage.live_frame, 2.0)

        # No frame change, no input change: the stage must stay idle.
        sends = engine.stats["sends"]
        responses = report_of(self.report).get("responses", 0)
        pump(seconds=0.5)
        self.assertEqual(
            engine.stats["sends"], sends, "a live stage sent without a frame change"
        )
        self.assertEqual(
            report_of(self.report).get("responses", 0),
            responses,
            "a live stage's client answered a request nobody asked for",
        )

    def test_03_a_live_stage_keeps_no_frame_cache_entry(self):
        record = self.stack_mod.stack_of(self.obj)
        key = (record.stack_id, self.stage.key)
        self.assertFalse(
            self.engine._cache.get(key), "a live stage stored a replayable hot frame"
        )
        self.assertEqual(
            self.engine._store_for(record, self.stage).rids(),
            [],
            "a live stage stored a replayable frame on disk",
        )


class SharedObjectIsolationTests(LiveTestCase):
    """A parameter edit on stage B must not advance live stage A."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.obj, cls.stage_a = cls.stack_object("Shared")
        bpy.context.view_layer.objects.active = cls.obj
        cls.obj.select_set(True)
        bpy.ops.stageloom.add_stage(label="SharedB")
        cls.stage_b = cls.obj.gh_stack.stages[1]
        cls.obj[cls.param_name(cls.stage_b, SCALE)] = 1.0
        # A is above B in the modifier stack, so B's parameter cannot change A's
        # prefix: the only reason A could send is the shared object being dirty.
        cls.stage_a.live = True
        cls.report_a = cls.report_path("shared_a")
        cls.report_b = cls.report_path("shared_b")
        cls.fake_a = start_fake(cls.stage_a.key, cls.report_a)
        cls.fake_b = start_fake(cls.stage_b.key, cls.report_b)

        def settled():
            return (
                cls.stage_a.state == "ok"
                and cls.stage_b.state == "ok"
                and report_of(cls.report_a).get("responses", 0) >= 1
                and report_of(cls.report_b).get("responses", 0) >= 1
            )

        settle(cls.engine, cls.stack_mod, [cls.obj], settled)

    def test_01_another_stages_parameter_does_not_advance_the_live_stage(self):
        engine = self.engine
        self.assertTrue(
            pump(
                seconds=10.0,
                until=lambda: not engine._dirty
                and self.stage_a.state == "ok"
                and self.stage_b.state == "ok",
            ),
            "the shared stack never went quiet",
        )
        self.assertEqual(self.stage_a.state, "ok", self.stage_a.error)
        self.assertEqual(self.stage_b.state, "ok", self.stage_b.error)
        live_requests = requests_of(self.report_a)
        live_rid = self.stage_a.rid
        live_frame = self.stage_a.live_frame
        b_requests = requests_of(self.report_b)

        self.obj[self.param_name(self.stage_b, SCALE)] = 2.0
        engine._mark_object_dirty(self.obj)

        self.assertTrue(
            pump(
                seconds=10.0,
                until=lambda: requests_of(self.report_b) > b_requests
                and abs(helper_max(self.obj, self.stage_b.key) - 2.0) < 0.01
                and self.stage_b.state == "ok",
            ),
            "stage B's parameter change never reached its client",
        )
        self.assertTrue(
            pump(seconds=2.0, until=lambda: not engine._dirty),
            "the shared stack did not settle after B's change",
        )

        self.assertEqual(
            requests_of(self.report_a),
            live_requests,
            "the live stage advanced for another stage's parameter change",
        )
        self.assertEqual(
            self.stage_a.rid, live_rid, "the live stage's request id moved on B's edit"
        )
        self.assertEqual(
            self.stage_a.live_frame,
            live_frame,
            "the live stage recorded a new frame for another stage's edit",
        )


class OneWayStageTests(LiveTestCase):
    """A Stage In with no Stage Out: deliver geometry, never wait for an answer."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.obj, cls.stage = cls.stack_object("OneWay")
        cls.report = cls.report_path("one_way")
        cls.fake = start_fake(cls.stage.key, cls.report, "--no-stage-out")

        def ready():
            return (
                not cls.stage.has_stage_out
                and requests_of(cls.report) >= 1
                and cls.stage.state == "ok"
            )

        if not pump(seconds=15.0, until=ready):
            raise AssertionError(
                "the one-way stage never settled: has_stage_out=%r state=%r requests=%r"
                % (cls.stage.has_stage_out, cls.stage.state, report_of(cls.report))
            )

    def test_01_a_stage_in_only_stage_is_one_way_and_never_waits(self):
        self.assertFalse(
            self.stage.has_stage_out,
            "hello did not report the missing Stage Out",
        )
        self.assertEqual(self.stage.state, "ok")
        self.assertGreaterEqual(
            requests_of(self.report), 1, "the Stage In never received the prefix"
        )
        self.assertTrue(self.stage.rid, "the delivered request was not recorded")
        self.assertFalse(
            self.engine._pending.get((self.obj.gh_stack.stack_id, self.stage.key)),
            "a one-way stage armed a response wait",
        )
        self.assertNotIn(
            self.obj.name,
            self.engine._waiting_objects,
            "a one-way stage kept the engine waiting on the object",
        )

    def test_02_a_frame_change_delivers_again_without_waiting(self):
        before = requests_of(self.report)
        bpy.context.scene.frame_set(2)
        self.engine._on_frame_change(bpy.context.scene)
        self.assertTrue(
            pump(seconds=5.0, until=lambda: requests_of(self.report) > before),
            "a live one-way stage did not deliver the next frame",
        )
        self.assertTrue(
            pump(seconds=2.0, until=lambda: self.stage.state == "ok"),
            "the one-way stage did not settle after delivering",
        )
        self.assertEqual(self.stage.live_frame, 2.0)
        self.assertFalse(
            self.engine._pending.get((self.obj.gh_stack.stack_id, self.stage.key)),
            "the frame delivery armed a response wait",
        )

    def test_03_playback_does_not_block_on_a_one_way_stage(self):
        prefs = bpy.context.preferences.addons["stageloom"].preferences
        original = prefs.playback_timeout
        bpy.context.scene.frame_set(3)
        try:
            prefs.playback_timeout = 0.5
            started = time.monotonic()
            ready = self.engine._sync_playback_frame(bpy.context.scene)
            elapsed = time.monotonic() - started
        finally:
            prefs.playback_timeout = original
        self.assertTrue(ready, "a one-way stage failed the playback gate")
        self.assertLess(
            elapsed,
            0.5,
            "the playback gate waited for a stage that has no Stage Out",
        )

    def test_04_hello_claims_update_the_mode_and_absence_keeps_it(self):
        engine = self.engine
        record = self.obj.gh_stack
        engine._on_client_hello(
            {"stages": [{"key": self.stage.key, "has_stage_in": True}]}
        )
        self.assertFalse(
            self.stage.has_stage_out,
            "an old hello without has_stage_out overwrote the saved mode",
        )
        try:
            engine._on_client_hello(
                {
                    "stages": [
                        {
                            "key": self.stage.key,
                            "has_stage_in": True,
                            "has_stage_out": True,
                        }
                    ]
                }
            )
            self.assertTrue(
                self.stage.has_stage_out,
                "hello did not restore the two-way path",
            )
            outcome = engine._process_stage(self.obj, record, self.stage, force=True)
            self.assertEqual(
                outcome,
                "waiting",
                "a two-way stage did not wait again for its Stage Out",
            )
        finally:
            engine._forget(record.stack_id, self.stage.key)
            engine._set(self.stage, "state", "idle")


class LiveToggleTests(LiveTestCase):
    """Switching Live semantics retires the frames from the other mode."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.obj, cls.stage = cls.stack_object("Toggle")

    def test_00_a_new_stage_is_live_by_default_and_explains_itself(self):
        # A fresh object: adding to the class stack would leave a second stage
        # behind for the toggle tests.
        bpy.ops.object.select_all(action="DESELECT")
        bpy.ops.mesh.primitive_cube_add(size=1.0)
        obj = bpy.context.object
        obj.name = "DefaultLive"
        bpy.context.view_layer.objects.active = obj
        obj.select_set(True)
        bpy.ops.stageloom.add_stage(label="DefaultLive")
        try:
            stage = obj.gh_stack.stages[0]
            self.assertTrue(
                stage.live,
                "a new stage must be stateful by default",
            )
            self.assertEqual(stage.live_frame, self.stack_mod.LIVE_FRAME_UNSET)
            description = stage.bl_rna.properties["live"].description
            self.assertIn(
                "Stateful",
                description,
                "the Live checkbox has no tooltip explaining the mode",
            )
        finally:
            bpy.data.objects.remove(obj, do_unlink=True)

    def test_01_toggling_live_clears_the_stage_frame_cache(self):
        engine = self.engine
        record = self.stack_mod.stack_of(self.obj)
        if not record.stack_id:
            record.stack_id = self.stack_mod.ensure_stack(self.obj).stack_id
        key = (record.stack_id, self.stage.key)
        self.stage.live = False
        engine._cache[key] = {"sha256:old": (b"", "sha256:old-output")}
        before = self.stage.generation

        self.stage.live = True

        self.assertGreater(
            self.stage.generation,
            before,
            "a Live transition left the old frame generation valid",
        )
        self.assertNotIn(
            key, engine._cache, "a Live transition left its hot frames in place"
        )
        self.assertEqual(self.stage.live_frame, self.stack_mod.LIVE_FRAME_UNSET)

    def test_02_undo_forces_a_live_stage_to_ask_again(self):
        self.stage.live = True
        self.stage.live_frame = 7.0

        self.engine._after_history_step()

        self.assertEqual(
            self.stage.live_frame,
            self.stack_mod.LIVE_FRAME_UNSET,
            "undo left a live stage believing the solver is on the restored frame",
        )


class StaleAnswerTests(unittest.TestCase):
    """A stale ok must not reset the stage or clear a newer pending request."""

    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="stageloom")
        prefs = bpy.context.preferences.addons["stageloom"].preferences
        prefs.data_root = tempfile.mkdtemp(prefix="stageloom-stale-")
        import stageloom.blender.engine as engine
        import stageloom.blender.stack as stack_mod

        cls.engine = engine
        cls.stack_mod = stack_mod
        bpy.ops.mesh.primitive_cube_add(size=2.0)
        cls.obj = bpy.context.object
        cls.obj.name = "Stale"
        bpy.context.view_layer.objects.active = cls.obj
        cls.obj.select_set(True)
        bpy.ops.stageloom.add_stage(label="Stale")
        cls.stage = cls.obj.gh_stack.stages[0]
        cls.record = stack_mod.stack_of(cls.obj)
        if not cls.record.stack_id:
            cls.record.stack_id = stack_mod.ensure_stack(cls.obj).stack_id
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
        bpy.ops.preferences.addon_disable(module="stageloom")

    def _reset_stale_state(self, live):
        engine = self.engine
        key = (self.record.stack_id, self.stage.key)
        stale_rid = "sha256:stale-live" if live else "sha256:stale"
        engine._cache.clear()
        engine._cache_bytes.clear()
        engine._pending.clear()
        # Assigning Live runs the cache-reset hook; set the generation and the
        # test's own cache state afterwards.
        self.stage.live = live
        self.stage.generation = 0
        self.stage.rid = "sha256:current"
        self.stage.applied_out_hash = "sha256:applied-before"
        engine._pending[key] = {"sha256:newer": time.monotonic()}
        if not live:
            # Seed the same rid with a different output, so the stale answer is
            # a genuine same-rid definition change rather than a first answer.
            old_header = {
                "type": "response",
                "stage": self.stage.key,
                "rid": stale_rid,
                "status": "ok",
                "out_hash": "sha256:old-output",
                "warnings": [],
            }
            engine._remember(
                self.record, self.stage, stale_rid, old_header, {}, raw=None
            )
        return key, stale_rid

    def _deliver_stale(self, rid):
        self.engine._handle_response(
            {
                "type": "response",
                "stage": self.stage.key,
                "rid": rid,
                "status": "ok",
                "out_hash": "sha256:new-output",
                "warnings": [],
            },
            self.sections,
        )

    def test_01_a_stale_ok_does_not_reset_a_deterministic_stage(self):
        key, rid = self._reset_stale_state(live=False)

        self._deliver_stale(rid)

        self.assertEqual(
            self.stage.generation, 0, "a stale ok bumped the stage generation"
        )
        self.assertIn(
            "sha256:newer",
            self.engine._pending.get(key, {}),
            "a stale ok cleared a newer pending request",
        )
        self.assertEqual(
            self.stage.applied_out_hash,
            "sha256:applied-before",
            "a stale ok applied its output",
        )
        self.assertIn(
            rid,
            self.engine._cache.get(key, {}),
            "a deterministic stale ok was not stored under its own rid",
        )

    def test_02_a_stale_ok_to_a_live_stage_is_dropped_not_cached(self):
        key, rid = self._reset_stale_state(live=True)

        self._deliver_stale(rid)

        self.assertEqual(
            self.stage.generation, 0, "a stale live ok bumped the generation"
        )
        self.assertIn(
            "sha256:newer",
            self.engine._pending.get(key, {}),
            "a stale live ok cleared a newer pending request",
        )
        self.assertEqual(
            self.stage.applied_out_hash,
            "sha256:applied-before",
            "a stale live ok applied its output",
        )
        self.assertNotIn(
            rid,
            self.engine._cache.get(key, {}),
            "a stale live answer was cached for replay",
        )
        self.assertNotIn(
            rid,
            self.engine._store_for(self.record, self.stage).rids(),
            "a stale live answer was stored for replay",
        )


if __name__ == "__main__":
    unittest.main()
