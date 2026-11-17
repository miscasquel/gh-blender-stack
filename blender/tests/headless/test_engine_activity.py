"""Layer B: when the engine works, and when it must not.

Run with:

    scripts/test-blender

Pinned here: a stage-less file has no timer or per-frame handlers; an edit with
no client skips the prefix read; and an all-dirty pass does not force a second
depsgraph evaluation.
"""

import unittest

import bpy

from harness import isolate_data_root
from stageloom.blender import stack as stack_mod
from stageloom.blender.engine import ENGINE
from stageloom.blender.engine import lifecycle as engine_lifecycle
from stageloom.blender.engine import triggers as engine_triggers


class DisconnectedFastPathTests(unittest.TestCase):
    """A stage with no client must not clone its prefix on an ordinary edit."""

    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="stageloom")
        cls.enterClassContext(isolate_data_root("stageloom-activity-"))
        import stageloom.blender.engine as engine

        cls.engine = engine
        # Headless has no event loop, so call the post-register settle directly.
        ENGINE.settle()
        bpy.ops.mesh.primitive_cube_add(size=2.0)
        cls.obj = bpy.context.object
        bpy.ops.stageloom.add_stage(label="NoClient")
        cls.stage = cls.obj.gh_stack.stages[0]
        engine_triggers._timer()
        if ENGINE.dirty:
            raise AssertionError("the disconnected stage never settled")

    @classmethod
    def tearDownClass(cls):
        bpy.ops.preferences.addon_disable(module="stageloom")

    def _edit(self):
        self.obj.data.vertices[0].co.x += 1.0
        self.obj.update_tag()
        bpy.context.view_layer.update()

    def test_01_an_edit_with_no_client_skips_the_read(self):
        clones = ENGINE.stats()["prefix_clones"]
        self._edit()
        engine_triggers._timer()
        self.assertEqual(ENGINE.stats()["prefix_clones"], clones, "an edit with no client cloned")
        self.assertIn(
            self.stage.state,
            (stack_mod.STATE_DISCONNECTED, stack_mod.STATE_UNBOUND),
        )

    def test_02_the_drain_policy(self):
        real_background = engine_triggers._in_background
        engine_triggers._in_background = lambda: False
        try:
            engine_lifecycle._mark_dirty_all()
            self.assertFalse(
                engine_triggers._needs_drain(True),
                "an all-dirty pass would force a redundant depsgraph evaluation",
            )
            ENGINE.dirty.clear()
            self.assertTrue(engine_triggers._needs_drain(True), "a narrower pass did not drain")
        finally:
            engine_triggers._in_background = real_background
            ENGINE.dirty.clear()

    def test_03_an_unrelated_scene_does_not_wake_the_stage(self):
        other = bpy.data.scenes.new("Activity Other Scene")
        try:
            ENGINE.dirty.clear()
            engine_triggers._on_depsgraph(other)
            self.assertNotIn(self.obj.name, ENGINE.dirty, "an unrelated depsgraph woke the stage")
            ENGINE.dirty.clear()
            engine_triggers._on_frame_change(other)
            self.assertNotIn(self.obj.name, ENGINE.dirty, "an unrelated frame woke the stage")

            ENGINE.dirty.clear()
            engine_triggers._on_frame_change(bpy.context.scene)
            self.assertIn(self.obj.name, ENGINE.dirty, "the stage's own frame did not wake it")
        finally:
            ENGINE.dirty.clear()
            bpy.data.scenes.remove(other)


if __name__ == "__main__":
    unittest.main()
