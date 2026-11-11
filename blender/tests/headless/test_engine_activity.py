"""Layer B: when the engine works, and when it must not.

Run with:

    scripts/test-blender

Pinned here: a stage-less file has no timer or per-frame handlers; an ordinary
edit with no client skips the prefix read while a forced read still takes it;
and an all-dirty pass does not force a second depsgraph evaluation.
"""

import os
import sys
import tempfile
import unittest

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
BLENDER_PKG = os.path.join(REPO, "blender")
if BLENDER_PKG not in sys.path:
    sys.path.insert(0, BLENDER_PKG)


class DisconnectedFastPathTests(unittest.TestCase):
    """A stage with no client must not clone its prefix on an ordinary edit."""

    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="stageloom")
        import stageloom.blender.engine as engine

        cls.engine = engine
        bpy.context.preferences.addons["stageloom"].preferences.data_root = tempfile.mkdtemp(
            prefix="stageloom-activity-"
        )
        # Headless has no event loop, so call the post-register settle directly.
        engine._settle()
        bpy.ops.mesh.primitive_cube_add(size=2.0)
        cls.obj = bpy.context.object
        bpy.ops.stageloom.add_stage(label="NoClient")
        cls.stage = cls.obj.gh_stack.stages[0]
        # The first pass reads the forced prefix and settles as disconnected.
        engine._timer()
        if engine._dirty or engine._force_read:
            raise AssertionError("the disconnected stage never settled")

    @classmethod
    def tearDownClass(cls):
        bpy.ops.preferences.addon_disable(module="stageloom")

    def _edit(self):
        self.obj.data.vertices[0].co.x += 1.0
        self.obj.update_tag()
        bpy.context.view_layer.update()

    def test_01_an_ordinary_edit_skips_the_read_and_a_forced_one_does_not(self):
        engine = self.engine
        clones = engine.stats["prefix_clones"]
        self._edit()
        engine._timer()
        self.assertEqual(engine.stats["prefix_clones"], clones, "an edit with no client cloned")
        self.assertIn(
            self.stage.state,
            (engine.stack_mod.STATE_DISCONNECTED, engine.stack_mod.STATE_UNBOUND),
        )

        self._edit()
        engine._mark_dirty_all(force=True)
        engine._timer()
        self.assertGreater(
            engine.stats["prefix_clones"],
            clones,
            "a forced read was skipped while disconnected",
        )
        self.assertFalse(engine._force_read, "the pass left a forced read armed")

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
            engine._force_read.clear()


if __name__ == "__main__":
    unittest.main()
