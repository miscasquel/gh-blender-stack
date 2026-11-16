"""Layer B: the engine's public facade and its read-only diagnostics.

T5 replaces the package-global hub with an ``Engine`` object and per-stage
runtime records. These tests pin the entry points that must keep their no-op
and failure behavior: a missing object/stage, a unique key at
``resolve_duplicate_key``, a malformed client hello, and the diagnostic
snapshots a dormant or active session reports.
"""

import unittest

import bpy

from harness import isolate_data_root


class DormantDiagnosticsTests(unittest.TestCase):
    """A stage-less file has no timer, so the diagnostics must say so."""

    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="stageloom")
        cls.enterClassContext(isolate_data_root("stageloom-diagnostics-"))
        import stageloom.blender.engine as engine

        cls.engine = engine
        # Headless has no event loop for the post-register settle.
        engine._settle()
        engine._on_load()

    @classmethod
    def tearDownClass(cls):
        bpy.ops.preferences.addon_disable(module="stageloom")

    def test_a_dormant_engine_reports_no_timer_and_a_zero_tick_rate(self):
        report = self.engine.registration_report()
        self.assertFalse(report["active"])
        self.assertFalse(report["timer_registered"])
        self.assertEqual(report["stack_objects"], 0)
        self.assertEqual(self.engine.tick_rate(0.1), 0.0)

    def test_recent_activity_is_a_bounded_copy(self):
        marker = "t3-diagnostics-marker"
        self.engine._trace.append(marker)
        activity = self.engine.recent_activity(1)
        self.assertEqual(activity, [marker])
        activity.append("mutated")
        self.assertNotIn("mutated", self.engine.recent_activity(10))
        for index in range(5):
            self.engine._trace.append("%s-%d" % (marker, index))
        self.assertEqual(len(self.engine.recent_activity(2)), 2)

    def test_trigger_health_is_empty_before_an_event(self):
        health = self.engine.trigger_health()
        self.assertIsNone(health["trigger_age"])
        self.assertEqual(health["trigger_count"], 0)
        self.assertEqual(health["trigger_updates"], 0)
        self.assertIsNone(health["send_age"])
        self.assertIsNone(health["apply_age"])


class EngineFacadeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="stageloom")
        cls.enterClassContext(isolate_data_root("stageloom-facade-"))
        import stageloom.blender.engine as engine
        import stageloom.blender.stack as stack_mod

        cls.engine = engine
        cls.stack_mod = stack_mod
        engine._deactivate()
        bpy.ops.mesh.primitive_cube_add(size=2.0)
        cls.obj = bpy.context.object
        cls.obj.name = "Facade"
        bpy.ops.stageloom.add_stage(label="Facade")
        cls.stage = cls.obj.gh_stack.stages[0]
        engine._deactivate()
        engine._dirty.clear()

    @classmethod
    def tearDownClass(cls):
        bpy.ops.preferences.addon_disable(module="stageloom")

    def test_01_push_with_no_active_object_is_a_noop(self):
        active = bpy.context.view_layer.objects.active
        try:
            bpy.context.view_layer.objects.active = None
            self.assertIsNone(bpy.context.object)
            before = self.stage.nonce
            self.engine.push()
            self.assertEqual(self.stage.nonce, before, "push with no object sent a request")
        finally:
            bpy.context.view_layer.objects.active = active

    def test_02_a_missing_stack_object_is_a_noop(self):
        mesh = bpy.data.meshes.new("FacadeBareMesh")
        bare = bpy.data.objects.new("FacadeBare", mesh)
        bpy.context.scene.collection.objects.link(bare)
        state = self.stage.state
        nonce = self.stage.nonce

        self.engine.request_again(bare)
        self.engine.freeze(bare, self.stage)

        self.assertEqual(self.stage.nonce, nonce)
        self.assertEqual(self.stage.state, state)

    def test_03_a_unique_key_is_not_resolved(self):
        key = self.stage.key
        result = self.engine.resolve_duplicate_key(
            self.obj, self.obj.gh_stack, self.stage
        )
        self.assertIsNone(result)
        self.assertEqual(self.stage.key, key)

    def test_04_a_malformed_hello_keeps_the_saved_input_modes(self):
        stage = self.stage
        stage.input_mode = self.stack_mod.INPUT_PREFIX
        stage.has_stage_out = True
        nonce = stage.nonce

        self.engine._on_client_hello(
            {
                "doc": "malformed.gh",
                "caps": "not-a-list",
                "stages": [
                    "not-an-entry",
                    None,
                    {"key": ""},
                    {
                        "key": stage.key,
                        "has_stage_in": "yes",
                        "has_stage_out": 1,
                    },
                ],
            }
        )

        self.assertEqual(self.engine.bridge.state["caps"], [])
        self.assertEqual(stage.input_mode, self.stack_mod.INPUT_PREFIX)
        self.assertTrue(stage.has_stage_out)
        self.assertEqual(stage.nonce, nonce, "hello itself sent a request")
        self.assertTrue(
            any(
                "client hello: malformed.gh" in line
                for line in self.engine.recent_activity(20)
            ),
            self.engine.recent_activity(20),
        )

    def test_05_an_active_engine_reports_one_generation(self):
        self.assertTrue(self.engine._ensure_active())
        try:
            report = self.engine.registration_report()
            self.assertTrue(report["registered"])
            self.assertTrue(report["active"])
            self.assertTrue(report["timer_registered"])
            self.assertEqual(report["stack_objects"], 1)
            self.assertEqual(
                report["handler_counts"],
                {
                    "depsgraph_update_post": 1,
                    "frame_change_post": 2,
                    "animation_playback_pre": 1,
                    "animation_playback_post": 1,
                    "load_post": 1,
                    "undo_post": 1,
                    "redo_post": 1,
                },
            )
            self.assertEqual(
                report["timer_module"], "stageloom.blender.engine.triggers"
            )
        finally:
            self.engine._deactivate()


if __name__ == "__main__":
    unittest.main()
