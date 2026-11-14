"""Layer B: Clear Bake's irreversible-data guard.

The native Bake delete is not an undo step (see
docs/decisions/0012-bake-data-irreversible.md), so the operator must ask before
deleting data and must not ask when there is nothing to lose. These tests pin
the decision helper and the non-modal parts of the operator; showing the actual
popup is Blender's job and is not exercised headless.
"""

import os
import sys
import tempfile
import types
import unittest

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
BLENDER_PKG = os.path.join(REPO, "blender")
if BLENDER_PKG not in sys.path:
    sys.path.insert(0, BLENDER_PKG)


class BakeSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="stageloom")
        bpy.context.preferences.addons["stageloom"].preferences.data_root = tempfile.mkdtemp(
            prefix="stageloom-bake-safety-"
        )
        from stageloom.blender import nodes, ops

        cls.nodes = nodes
        cls.ops = ops
        bpy.ops.mesh.primitive_cube_add(size=2.0)
        cls.obj = bpy.context.object
        cls.obj.name = "BakeSafety"
        bpy.context.view_layer.objects.active = cls.obj
        cls.obj.select_set(True)
        bpy.ops.stageloom.add_stage(label="Bake safety")
        cls.stage = cls.obj.gh_stack.stages[0]

    @classmethod
    def tearDownClass(cls):
        bpy.ops.preferences.addon_disable(module="stageloom")

    def test_01_the_decision_is_about_data_not_the_native_entry(self):
        entry = types.SimpleNamespace(directory="", data_blocks=[])
        stage = types.SimpleNamespace(baked=False)
        self.assertFalse(
            self.ops.clear_bake_needs_confirmation(stage, None),
            "an unbaked stage with no entry would prompt",
        )
        self.assertFalse(
            self.ops.clear_bake_needs_confirmation(stage, entry),
            "a bare native entry is metadata, not data",
        )
        stage.baked = True
        self.assertTrue(
            self.ops.clear_bake_needs_confirmation(stage, entry),
            "the add-on's own bake flag would not prompt",
        )
        stage.baked = False
        entry.directory = "/tmp/a-disk-bake"
        self.assertTrue(
            self.ops.clear_bake_needs_confirmation(stage, entry),
            "a native disk bake would not prompt",
        )

    def test_02_the_live_stage_has_only_the_native_metadata_entry(self):
        modifier = self.nodes.modifier_of(self.obj, self.stage)
        self.assertIsNotNone(modifier)
        bpy.context.view_layer.update()
        entry = self.ops._native_bake_entry(modifier)
        self.assertIsNotNone(entry, "the stage has no native bake entry to address")
        self.assertEqual(entry.directory, "", "an unbaked entry unexpectedly has a bake path")
        self.assertFalse(
            self.ops.clear_bake_needs_confirmation(self.stage, entry),
            "an unbaked stage would prompt",
        )

    def test_03_a_no_op_clear_finishes_without_a_prompt(self):
        self.stage.baked = False
        self.stage.frozen = True
        self.assertEqual(bpy.ops.stageloom.clear_bake(index=0), {"FINISHED"})
        self.assertFalse(self.stage.baked)
        self.assertFalse(self.stage.frozen, "a no-op clear did not unfreeze the stage")

    def test_04_invoke_only_confirms_when_a_bake_would_be_deleted(self):
        calls = []
        executed = []

        class WindowManager:
            def invoke_confirm(self, operator, event, **kwargs):
                calls.append(kwargs)
                return {"RUNNING_MODAL"}

        context = types.SimpleNamespace(
            object=self.obj,
            view_layer=bpy.context.view_layer,
            window_manager=WindowManager(),
        )
        # Operator classes cannot be instantiated directly in Blender's Python
        # API; a plain object with the two attributes invoke() uses exercises
        # the same decision path without turning this into a modal test.
        operator = types.SimpleNamespace(
            index=0,
            execute=lambda _context: (executed.append(1), {"FINISHED"})[1],
        )

        self.stage.baked = False
        self.assertEqual(
            self.ops.STAGELOOM_OT_clear_bake.invoke(operator, context, None),
            {"FINISHED"},
        )
        self.assertEqual(calls, [], "a no-op clear asked for confirmation")
        self.assertEqual(executed, [1], "a no-op clear did not execute")

        self.stage.baked = True
        try:
            self.assertEqual(
                self.ops.STAGELOOM_OT_clear_bake.invoke(operator, context, None),
                {"RUNNING_MODAL"},
            )
        finally:
            self.stage.baked = False
        self.assertEqual(len(calls), 1, "a baked stage did not ask for confirmation")
        self.assertEqual(calls[0]["title"], self.ops.CLEAR_BAKE_CONFIRM_TITLE)
        self.assertIn("cannot be recovered", calls[0]["message"])
        self.assertEqual(calls[0]["confirm_text"], self.ops.CLEAR_BAKE_CONFIRM_TEXT)
        self.assertEqual(executed, [1], "a confirmed clear executed before confirmation")

    def test_05_the_operators_advertise_the_irreversible_route(self):
        clear = self.ops.STAGELOOM_OT_clear_bake
        self.assertTrue(callable(getattr(clear, "invoke", None)), "Clear Bake has no invoke")
        self.assertIn("cannot be undone", clear.bl_description.lower())
        self.assertIn("cannot be undone", self.ops.CLEAR_BAKE_WARNING.lower())
        self.assertNotIn("UNDO", clear.bl_options, "Clear Bake claims undo safety")
        self.assertNotIn(
            "UNDO",
            self.ops.STAGELOOM_OT_bake.bl_options,
            "Bake stage claims undo safety",
        )


if __name__ == "__main__":
    unittest.main()
