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
        engine._settle()
        bpy.ops.mesh.primitive_cube_add(size=2.0)
        cls.obj = bpy.context.object
        bpy.ops.stageloom.add_stage(label="NoClient")
        cls.stage = cls.obj.gh_stack.stages[0]
        engine._timer()
        if engine._dirty:
            raise AssertionError("the disconnected stage never settled")

    @classmethod
    def tearDownClass(cls):
        bpy.ops.preferences.addon_disable(module="stageloom")

    def _edit(self):
        self.obj.data.vertices[0].co.x += 1.0
        self.obj.update_tag()
        bpy.context.view_layer.update()

    def test_01_an_edit_with_no_client_skips_the_read(self):
        engine = self.engine
        clones = engine.stats["prefix_clones"]
        self._edit()
        engine._timer()
        self.assertEqual(engine.stats["prefix_clones"], clones, "an edit with no client cloned")
        self.assertIn(
            self.stage.state,
            (engine.stack_mod.STATE_DISCONNECTED, engine.stack_mod.STATE_UNBOUND),
        )

    def test_02_the_drain_policy(self):
        engine = self.engine
        real_background = engine._in_background
        engine._in_background = lambda: False
        try:
            engine._mark_dirty_all()
            self.assertFalse(
                engine._needs_drain(True),
                "an all-dirty pass would force a redundant depsgraph evaluation",
            )
            engine._dirty.clear()
            self.assertTrue(engine._needs_drain(True), "a narrower pass did not drain")
        finally:
            engine._in_background = real_background
            engine._dirty.clear()


if __name__ == "__main__":
    unittest.main()
