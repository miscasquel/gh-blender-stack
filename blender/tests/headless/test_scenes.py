"""Layer B: scene-scoped frames, depsgraph work and playback gating.

A stage belongs to the scene its object is linked into. A frame change in
another scene must not queue it, its request bookkeeping must compare that
scene's frame, and neither the playback nor the bake gate may run a frame event
from a scene that did not arm it.
"""

import unittest

import bpy
import numpy as np

from harness import isolate_data_root
from stageloom.blender.engine import ENGINE
from stageloom.blender.engine import lifecycle as engine_lifecycle
from stageloom.blender.engine import playback as engine_playback
from stageloom.blender.engine import request as engine_request
from stageloom.blender.engine import triggers as engine_triggers


class SceneScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="stageloom")
        import stageloom.blender.bridge as bridge
        import stageloom.blender.engine as engine
        import stageloom.blender.helpers as helpers
        import stageloom.blender.server as server
        import stageloom.blender.stack as stack_mod

        cls.bridge = bridge
        cls.engine = engine
        cls.helpers = helpers
        cls.server = server
        cls.stack_mod = stack_mod
        cls.real_send = server.SERVER.send
        cls.enterClassContext(isolate_data_root("stageloom-scenes-"))
        cls.previous_connected = dict(bridge.state)

        scene_a = bpy.context.scene
        scene_b = bpy.data.scenes.new("Other Stage Scene")
        cls.scene_a = scene_a
        cls.scene_b = scene_b
        cls.window = bpy.context.window
        cls.window.scene = scene_a
        bpy.ops.mesh.primitive_cube_add(size=2.0)
        cls.obj_a = bpy.context.object
        cls.obj_a.name = "Scene A Cube"
        bpy.ops.stageloom.add_stage(label="Scene A")
        cls.stage_a = cls.obj_a.gh_stack.stages[0]

        cls.window.scene = scene_b
        bpy.ops.mesh.primitive_cube_add(size=2.0)
        cls.obj_b = bpy.context.object
        cls.obj_b.name = "Scene B Cube"
        # A frame-dependent modifier *above* the stage: the prefix must be read
        # at the object's scene frame, not the active scene's.
        deform = cls.obj_b.modifiers.new(name="Frame Deform", type="SIMPLE_DEFORM")
        deform.angle = 0.0
        deform.keyframe_insert(data_path="angle", frame=1)
        deform.angle = 1.0
        deform.keyframe_insert(data_path="angle", frame=10)
        bpy.context.view_layer.objects.active = cls.obj_b
        bpy.ops.stageloom.add_stage(label="Scene B")
        cls.stage_b = cls.obj_b.gh_stack.stages[0]
        cls.record_b = cls.obj_b.gh_stack
        cls.window.scene = scene_a

    @classmethod
    def tearDownClass(cls):
        try:
            bpy.data.objects.remove(cls.obj_a, do_unlink=True)
            bpy.data.objects.remove(cls.obj_b, do_unlink=True)
        except ReferenceError:
            pass
        bpy.data.scenes.remove(cls.scene_b)
        bpy.ops.preferences.addon_disable(module="stageloom")

    def setUp(self):
        self.window.scene = self.scene_a
        self.scene_a.frame_set(1)
        self.scene_b.frame_set(10)
        ENGINE.clear_runtimes()
        ENGINE.clear_runtimes()
        ENGINE.clear_runtimes()
        ENGINE.verify.clear()
        ENGINE.dirty.clear()
        ENGINE.playback_sync = False
        ENGINE.playback_scene = None
        engine_lifecycle._rebuild_index()
        self.bridge.state.update(connected=True, clients=1, doc="multi-scene.gh")

    def tearDown(self):
        ENGINE.clear_runtimes()
        ENGINE.clear_runtimes()
        ENGINE.clear_runtimes()
        ENGINE.verify.clear()
        ENGINE.dirty.clear()
        ENGINE.playback_sync = False
        ENGINE.playback_scene = None
        self.server.SERVER.send = self.real_send
        self.bridge.state.clear()
        self.bridge.state.update(self.previous_connected)
        self.window.scene = self.scene_a

    def test_01_each_request_uses_its_objects_scene_frame(self):
        self.server.SERVER.send = lambda frame: True
        try:
            # Process B while the *active* scene is A at frame 1, while B is at
            # frame 10. The old code compared against A and recorded 1.
            self.assertEqual(self.scene_a.frame_float, 1.0)
            self.assertEqual(self.scene_b.frame_float, 10.0)
            engine_lifecycle._process_object(self.obj_b)
            key_b = (self.record_b.stack_id, self.stage_b.key)
            self.assertEqual(
                ENGINE.peek_runtime(*key_b).last_frame,
                10.0,
                "the request recorded the active scene's frame, not the object's",
            )
            self.assertFalse(ENGINE.dirty, "the scene switch queued a synthetic frame pass")
            self.assertFalse(ENGINE.switching_scene)

            # B's frame did not move. Moving A must not make B ask again.
            ENGINE.forget(self.record_b.stack_id, self.stage_b.key)
            ENGINE.runtime(*key_b).pending.clear()
            self.scene_a.frame_set(2)
            sends = ENGINE.stats()["sends"]
            engine_lifecycle._process_object(self.obj_b)
            self.assertEqual(ENGINE.stats()["sends"], sends, "an unrelated scene's frame woke B")

            # A's own scene frame moved, so A asks.
            engine_lifecycle._process_object(self.obj_a)
            self.assertGreater(ENGINE.stats()["sends"], sends, "A's own frame change did not ask")
            record_a = self.obj_a.gh_stack
            self.assertEqual(
                (ENGINE.peek_runtime(*(record_a.stack_id, self.stage_a.key)).last_frame if ENGINE.peek_runtime(*(record_a.stack_id, self.stage_a.key)) else None),
                2.0,
            )
        finally:
            self.server.SERVER.send = self.real_send

    def test_02_the_playback_gate_ignores_another_scenes_frame(self):
        self.scene_a.sync_mode = "NONE"
        self.scene_b.sync_mode = "NONE"
        engine_playback._on_playback_pre(self.scene_a)
        try:
            self.assertTrue(ENGINE.playback_sync)
            frames = ENGINE.stats()["playback_frames"]
            engine_playback._on_frame_change_sync(self.scene_b)
            self.assertEqual(
                ENGINE.stats()["playback_frames"],
                frames,
                "a frame event from another scene entered the playback gate",
            )
            # A late post from the other scene must not disarm A's gate.
            engine_playback._on_playback_post(self.scene_b)
            self.assertTrue(ENGINE.playback_sync, "another scene disarmed the gate")
        finally:
            engine_playback._on_playback_post(self.scene_a)
        self.assertFalse(ENGINE.playback_sync)
        self.assertIsNone(ENGINE.playback_scene)

    def test_03_a_depsgraph_event_marks_only_its_scenes_stacks(self):
        ENGINE.dirty.clear()
        engine_triggers._on_depsgraph(self.scene_b)
        self.assertIn(self.obj_b.name, ENGINE.dirty)
        self.assertNotIn(self.obj_a.name, ENGINE.dirty)

        ENGINE.dirty.clear()
        engine_triggers._on_frame_change(self.scene_a)
        self.assertIn(self.obj_a.name, ENGINE.dirty)
        self.assertNotIn(self.obj_b.name, ENGINE.dirty)

    def test_04_the_prefix_is_read_at_the_objects_scene_frame(self):
        # Active scene A is at frame 1; B is at frame 10. The animated deform
        # above B's stage must be evaluated at 10.
        self.assertEqual(self.scene_a.frame_float, 1.0)
        self.assertEqual(self.scene_b.frame_float, 10.0)
        ENGINE.dirty.clear()
        sections_10, _hash, _attributes, _definitions = engine_request._prefix_sections(
            self.obj_b, self.record_b, self.stage_b
        )
        self.scene_b.frame_set(2)
        ENGINE.dirty.clear()
        sections_2, _hash, _attributes, _definitions = engine_request._prefix_sections(
            self.obj_b, self.record_b, self.stage_b
        )
        self.assertFalse(
            ENGINE.dirty,
            "evaluating another scene's prefix queued a synthetic frame pass",
        )
        by_name_10 = {name: values for name, _dtype, values in sections_10}
        by_name_2 = {name: values for name, _dtype, values in sections_2}
        verts_10 = by_name_10.get("mesh.verts")
        verts_2 = by_name_2.get("mesh.verts")
        self.assertIsNotNone(verts_10)
        self.assertIsNotNone(verts_2)
        self.assertFalse(
            np.array_equal(np.asarray(verts_10), np.asarray(verts_2)),
            "the prefix ignored the object's scene frame and replayed the active scene's",
        )


if __name__ == "__main__":
    unittest.main()
