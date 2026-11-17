"""The Stageloom rebrand: display names migrate, persisted keys do not."""

import unittest

import bpy

from stageloom.blender import helpers


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

    def test_object_name_helpers_move_to_the_stable_identity(self):
        """0.6/early-0.7 helpers were named after the object; a later rename
        then addressed a second, empty set. The stable stack id must adopt the
        old set (and its data) in place."""
        bpy.ops.mesh.primitive_cube_add(size=2.0)
        obj = bpy.context.object
        obj.name = "Old Name"
        bpy.ops.stageloom.add_stage(label="Old")
        stage = obj.gh_stack.stages[0]

        stable = helpers.helper_name(obj, stage.key)
        existing = bpy.data.objects.get(stable)
        self.assertIsNotNone(existing)
        bpy.data.objects.remove(existing, do_unlink=True)

        old_name = "SL %s.%s" % (obj.name, stage.key)
        old_mesh = bpy.data.meshes.new(old_name)
        old_mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
        old_mesh.update()
        old = bpy.data.objects.new(old_name, old_mesh)
        bpy.context.scene.collection.objects.link(old)

        helpers.adopt_existing()

        migrated = bpy.data.objects.get(stable)
        self.assertIsNotNone(migrated, "the old helper name was not adopted")
        self.assertIs(migrated.data, old_mesh, "the migration replaced the helper data")
        self.assertIsNone(bpy.data.objects.get(old_name))


if __name__ == "__main__":
    unittest.main()
