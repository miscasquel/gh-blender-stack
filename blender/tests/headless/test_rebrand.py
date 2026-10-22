"""The 0.4.0 rebrand: old display names migrate, persisted keys do not.

A pre-0.4.0 file keeps its stage records, parameters and animation; only the
scene, collection, helper, node-group and modifier *names* change. The internal
keys (`gh_stack`, `gh_stack_id`, `gh/...`) deliberately keep their old names.
"""

import unittest

import bpy

from blender.stageloom.blender import helpers


class RebrandMigrationTests(unittest.TestCase):
    def setUp(self):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="stageloom")

    def tearDown(self):
        bpy.ops.preferences.addon_disable(module="stageloom")

    def test_old_names_are_migrated_and_persisted_keys_stay(self):
        old_scene = bpy.data.scenes.new("GH Stage")
        old_coll = bpy.data.collections.new("GH Stage Data")
        old_scene.collection.children.link(old_coll)

        mesh = bpy.data.meshes.new("GHS cube")
        helper = bpy.data.objects.new("GHS stack.key", mesh)
        old_coll.objects.link(helper)
        helper["gh_stack_id"] = "stack-key"

        group = bpy.data.node_groups.new("GHS key", "GeometryNodeTree")
        holder = bpy.data.objects.new("Holder", bpy.data.meshes.new("Holder"))
        bpy.context.scene.collection.objects.link(holder)
        modifier = holder.modifiers.new(name="GH · Demo", type="NODES")
        modifier.node_group = group

        helpers.adopt_existing()

        self.assertIsNone(bpy.data.scenes.get("GH Stage"))
        self.assertIsNotNone(bpy.data.scenes.get("Stageloom Stage"))
        self.assertIsNone(bpy.data.collections.get("GH Stage Data"))
        self.assertIsNotNone(bpy.data.collections.get("Stageloom Stage Data"))
        self.assertIsNone(bpy.data.objects.get("GHS stack.key"))
        self.assertIsNotNone(bpy.data.objects.get("SL stack.key"))
        self.assertIsNone(bpy.data.node_groups.get("GHS key"))
        self.assertIsNotNone(bpy.data.node_groups.get("SL key"))
        self.assertEqual(holder.modifiers[0].name, "Stageloom · Demo")
        # The persisted keys are the compatibility contract.
        self.assertEqual(helper["gh_stack_id"], "stack-key")
        self.assertEqual(helper.name, "SL stack.key")


if __name__ == "__main__":
    unittest.main()
