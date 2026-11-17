"""Layer B: generator stages (no Stage In) and live request semantics.

A generator's request names the empty geometry hash, so base geometry edits
cannot move its request id. Every stage is live: it advances once per delivered
request, a frame change asks again, and a same-rid answer is applied when its
output changed.
"""

import os
import tempfile
import time
import unittest

import bpy

from harness import (
    SCALE,
    free_port,
    helper_max,
    isolate_data_root,
    pump,
    report_of,
    requests_of,
    settle,
    start_fake,
    stop_fake,
)
from stageloom.blender.engine import ENGINE
from stageloom.blender.engine import messages as engine_messages
from stageloom.blender.engine import playback as engine_playback
from stageloom.blender.engine import request as engine_request
from stageloom.blender.engine import triggers as engine_triggers
from stageloom.core import hashing


class LiveTestCase(unittest.TestCase):
    """Fresh factory scene, private data root and server, one fake per class."""

    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="stageloom")
        cls.enterClassContext(isolate_data_root("stageloom-live-generator-"))
        import stageloom.blender.engine as engine
        import stageloom.blender.stack as stack_mod

        cls.engine = engine
        cls.stack_mod = stack_mod
        cls.port = free_port()
        engine.start_server(cls.port)
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
        cls.fake = start_fake(cls.stage.key, cls.report, port=cls.port)

        def settled():
            return cls.stage.state == "ok" and report_of(cls.report).get("responses", 0) >= 1

        settle(
            cls.engine,
            cls.stack_mod,
            [cls.obj],
            settled,
            total=25.0,
            include_error=True,
        )

    def test_01_a_generator_skips_the_prefix_and_sends_empty_geometry(self):
        record = self.stack_mod.stack_of(self.obj)
        clones = ENGINE.stats()["prefix_clones"]

        sections, geom_hash, attributes, definitions = engine_request._prefix_sections(
            self.obj, record, self.stage
        )

        self.assertEqual(sections, [], "a generator evaluated a prefix")
        self.assertEqual(attributes, [])
        self.assertEqual(definitions, [])
        self.assertEqual(
            geom_hash,
            hashing.geometry_hash({}),
            "a generator named a geometry hash for a prefix it does not read",
        )
        self.assertEqual(
            ENGINE.stats()["prefix_clones"], clones, "a generator cloned the object"
        )
        report = report_of(self.report)
        self.assertEqual(report.get("received_verts"), 0, report)
        self.assertEqual(report.get("sections"), [], report)
        self.assertGreaterEqual(report.get("full", 0), 1, report)
        self.assertEqual(self.stage.state, "ok", self.stage.error)

    def test_02_a_base_geometry_edit_does_not_move_the_request(self):
        rid = self.stage.rid
        sends = ENGINE.stats()["sends"]
        requests = requests_of(self.report)
        clones = ENGINE.stats()["prefix_clones"]

        self.obj.data.vertices[0].co.x += 3.0
        self.obj.data.update()
        bpy.context.view_layer.update()
        ENGINE.mark_object_dirty(self.obj)
        self.assertTrue(
            pump(seconds=5.0, until=lambda: not ENGINE.dirty and self.stage.state == "ok"),
            "the pass after a base edit never settled",
        )

        self.assertEqual(self.stage.rid, rid, "a base edit moved a generator's request id")
        self.assertEqual(ENGINE.stats()["sends"], sends, "a base edit sent a generator request")
        self.assertEqual(
            requests_of(self.report), requests, "a base edit reached the generator's client"
        )
        self.assertEqual(
            ENGINE.stats()["prefix_clones"], clones, "the base edit cloned a generator prefix"
        )

    def test_03_hello_presence_controls_the_mode_and_absence_keeps_it(self):
        self.assertEqual(self.stage.input_mode, "generator")

        # An older client does not publish the field: the saved mode stays.
        engine_messages._on_client_hello(
            {"doc": "fake.gh", "stages": [{"key": self.stage.key}]}
        )
        self.assertEqual(
            self.stage.input_mode,
            "generator",
            "a hello without has_stage_in overwrote the saved mode",
        )

        engine_messages._on_client_hello(
            {
                "doc": "fake.gh",
                "stages": [{"key": self.stage.key, "has_stage_in": True}],
            }
        )
        self.assertEqual(self.stage.input_mode, "prefix")
        self.assertEqual(
            self.stage.geom_hash, "", "a mode change kept the old geometry claim"
        )

        engine_messages._on_client_hello(
            {
                "doc": "fake.gh",
                "stages": [{"key": self.stage.key, "has_stage_in": False}],
            }
        )
        self.assertEqual(self.stage.input_mode, "generator")


class LiveStageTests(LiveTestCase):
    """A stage advances once per frame and applies a changed same-rid answer."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.obj, cls.stage = cls.stack_object("Live")
        cls.report = cls.report_path("live")
        # The fake answers its first request twice: the second answer has the
        # same rid and a changed output, the GH-side-edit shape a stage must
        # apply.
        cls.fake = start_fake(cls.stage.key, cls.report, "--gh-edit-after", "1", port=cls.port)

        def settled():
            report = report_of(cls.report)
            return (
                cls.stage.state == "ok"
                and report.get("responses", 0) >= 1
                and report.get("edits", 0) >= 1
                and abs(helper_max(cls.obj, cls.stage.key) - 42.0) < 0.01
            )

        settle(
            cls.engine,
            cls.stack_mod,
            [cls.obj],
            settled,
            total=25.0,
            include_error=True,
        )

    def test_01_a_same_rid_change_applies(self):
        self.assertEqual(
            helper_max(self.obj, self.stage.key),
            42.0,
            "the changed same-rid answer did not apply (helper max %r)"
            % helper_max(self.obj, self.stage.key),
        )
        pump(seconds=1.0, until=lambda: not ENGINE.dirty)
        self.assertEqual(self.stage.state, "ok", self.stage.error)

    def test_02_a_frame_change_sends_once_and_the_same_frame_is_quiet(self):
        sends = ENGINE.stats()["sends"]
        responses = report_of(self.report).get("responses", 0)
        rid = self.stage.rid

        bpy.context.scene.frame_set(2)
        engine_triggers._on_frame_change(bpy.context.scene)

        self.assertTrue(
            pump(seconds=5.0, until=lambda: ENGINE.stats()["sends"] == sends + 1),
            "a stage did not send once on a frame change",
        )
        self.assertEqual(
            self.stage.rid, rid, "the frame-change request id moved; the test proves nothing"
        )
        self.assertTrue(
            pump(
                seconds=10.0,
                until=lambda: self.stage.state == "ok"
                and report_of(self.report).get("responses", 0) > responses
                and not ENGINE.dirty,
            ),
            "the frame-change answer never settled (state=%s, error=%s)"
            % (self.stage.state, self.stage.error),
        )
        # No frame change, no input change: the stage must stay idle.
        sends = ENGINE.stats()["sends"]
        responses = report_of(self.report).get("responses", 0)
        pump(seconds=0.5)
        self.assertEqual(
            ENGINE.stats()["sends"], sends, "a stage sent without a frame change"
        )
        self.assertEqual(
            report_of(self.report).get("responses", 0),
            responses,
            "a stage's client answered a request nobody asked for",
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
        cls.report_a = cls.report_path("shared_a")
        cls.report_b = cls.report_path("shared_b")
        cls.fake_a = start_fake(cls.stage_a.key, cls.report_a, port=cls.port)
        cls.fake_b = start_fake(cls.stage_b.key, cls.report_b, port=cls.port)

        def settled():
            return (
                cls.stage_a.state == "ok"
                and cls.stage_b.state == "ok"
                and report_of(cls.report_a).get("responses", 0) >= 1
                and report_of(cls.report_b).get("responses", 0) >= 1
            )

        settle(
            cls.engine,
            cls.stack_mod,
            [cls.obj],
            settled,
            total=25.0,
            include_error=True,
        )

    def test_01_another_stages_parameter_does_not_advance_the_live_stage(self):
        self.assertTrue(
            pump(
                seconds=10.0,
                until=lambda: not ENGINE.dirty
                and self.stage_a.state == "ok"
                and self.stage_b.state == "ok",
            ),
            "the shared stack never went quiet",
        )
        self.assertEqual(self.stage_a.state, "ok", self.stage_a.error)
        self.assertEqual(self.stage_b.state, "ok", self.stage_b.error)
        live_requests = requests_of(self.report_a)
        live_rid = self.stage_a.rid
        b_requests = requests_of(self.report_b)

        self.obj[self.param_name(self.stage_b, SCALE)] = 2.0
        ENGINE.mark_object_dirty(self.obj)

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
            pump(seconds=2.0, until=lambda: not ENGINE.dirty),
            "the shared stack did not settle after B's change",
        )

        self.assertEqual(
            requests_of(self.report_a),
            live_requests,
            "the stage advanced for another stage's parameter change",
        )
        self.assertEqual(
            self.stage_a.rid, live_rid, "the stage's request id moved on B's edit"
        )


class OneWayStageTests(LiveTestCase):
    """A Stage In with no Stage Out: deliver geometry, never wait for an answer."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.obj, cls.stage = cls.stack_object("OneWay")
        cls.report = cls.report_path("one_way")
        cls.fake = start_fake(cls.stage.key, cls.report, "--no-stage-out", port=cls.port)

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
            ENGINE.pending_of(*(self.obj.gh_stack.stack_id, self.stage.key)),
            "a one-way stage armed a response wait",
        )
        self.assertEqual(
            ENGINE.waiting_seconds(self.obj.gh_stack.stack_id, self.stage.key),
            0.0,
            "a one-way stage reported a wait it cannot have",
        )

    def test_02_a_frame_change_delivers_again_without_waiting(self):
        before = requests_of(self.report)
        bpy.context.scene.frame_set(2)
        engine_triggers._on_frame_change(bpy.context.scene)
        self.assertTrue(
            pump(seconds=5.0, until=lambda: requests_of(self.report) > before),
            "a one-way stage did not deliver the next frame",
        )
        self.assertTrue(
            pump(seconds=2.0, until=lambda: self.stage.state == "ok"),
            "the one-way stage did not settle after delivering",
        )
        self.assertFalse(
            ENGINE.pending_of(*(self.obj.gh_stack.stack_id, self.stage.key)),
            "the frame delivery armed a response wait",
        )

    def test_03_playback_does_not_block_on_a_one_way_stage(self):
        prefs = bpy.context.preferences.addons["stageloom"].preferences
        original = prefs.playback_timeout
        bpy.context.scene.frame_set(3)
        try:
            prefs.playback_timeout = 0.5
            started = time.monotonic()
            ready = engine_playback._sync_playback_frame(bpy.context.scene)
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
        record = self.obj.gh_stack
        engine_messages._on_client_hello(
            {"stages": [{"key": self.stage.key, "has_stage_in": True}]}
        )
        self.assertFalse(
            self.stage.has_stage_out,
            "an old hello without has_stage_out overwrote the saved mode",
        )
        try:
            engine_messages._on_client_hello(
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
            outcome = engine_request._process_stage(self.obj, record, self.stage)
            self.assertEqual(
                outcome,
                "waiting",
                "a two-way stage did not wait again for its Stage Out",
            )
        finally:
            ENGINE.forget(record.stack_id, self.stage.key)
            engine_request._set(self.stage, "state", "idle")


if __name__ == "__main__":
    unittest.main()
