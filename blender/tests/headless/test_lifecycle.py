"""Layer B: stable helper identity across rename, delete, undo and migration.

The plumbing used to be named after the stack object (`SL <object>.<stage>`).
Renaming the object addressed a second, empty helper set while the old one was
orphaned; deleting the object left the helpers, definition collections and row
clouds behind. These tests pin the stable stack-id identity, the old-name
migration, and the index-rebuild sweep.
"""

import os
import shutil
import tempfile
import unittest

import bpy

from harness import isolate_data_root


def _stage_prefix(obj, key):
    """The stable object-name prefix every helper/definition/cloud shares."""
    from stageloom.blender import helpers

    return helpers.helper_name(obj, key)


def _stage_datablocks(prefix):
    return {
        datablock.name
        for datablock in list(bpy.data.objects) + list(bpy.data.collections)
        if datablock.name == prefix or datablock.name.startswith(prefix + ".")
    }


def _add_triangle(mesh):
    mesh.from_pydata([(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)], [], [(0, 1, 2)])
    mesh.update()


class LifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="stageloom")
        import stageloom.blender.engine as engine
        import stageloom.blender.helpers as helpers
        import stageloom.blender.nodes as nodes
        import stageloom.blender.stack as stack_mod

        cls.engine = engine
        cls.helpers = helpers
        cls.nodes = nodes
        cls.stack_mod = stack_mod
        cls.enterClassContext(isolate_data_root("stageloom-lifecycle-"))

    @classmethod
    def tearDownClass(cls):
        bpy.ops.preferences.addon_disable(module="stageloom")

    def setUp(self):
        self.engine._dirty.clear()
        self.engine._rebuild_index()

    def _make_stack(self, name):
        bpy.ops.mesh.primitive_cube_add(size=2.0)
        obj = bpy.context.object
        obj.name = name
        bpy.ops.stageloom.add_stage(label=name)
        stage = obj.gh_stack.stages[0]
        self.addCleanup(self._drop_stack, obj)
        return obj, stage

    def _drop_stack(self, obj):
        try:
            bpy.data.objects.remove(obj, do_unlink=True)
        except ReferenceError:
            pass
        self.engine._rebuild_index()

    def _add_definition_and_clouds(self, obj, key):
        """One two-part definition and both row-cloud families, populated."""
        _collection, parts = self.helpers.ensure_definition_parts(obj, key, 0, 2)
        for part in parts:
            _add_triangle(part["mesh"].data)
        instance = self.helpers.ensure_instance_cloud(obj, key, 0)
        instance.data.resize(1)
        nested = self.helpers.ensure_nested_cloud(obj, key, 0)
        nested.data.resize(1)
        return parts, instance, nested

    def test_01_rename_keeps_the_helper_set_and_group(self):
        obj, stage = self._make_stack("Rename Root")
        helpers = self.helpers
        nodes = self.nodes
        helper = helpers.helper_for(obj, stage.key, "mesh")
        _add_triangle(helper.data)
        definition, instance, nested = self._add_definition_and_clouds(obj, stage.key)
        group = nodes.ensure_group(obj, stage.key)
        before_name = helper.name
        before = _stage_datablocks(_stage_prefix(obj, stage.key))

        obj.name = "Rename Root Renamed"
        self.engine._process_object(obj)

        self.assertIs(
            helpers.helper_for(obj, stage.key, "mesh"),
            helper,
            "the rename addressed a different helper",
        )
        self.assertEqual(helper.name, before_name, "the helper name moved with the object")
        self.assertEqual(
            _stage_datablocks(_stage_prefix(obj, stage.key)),
            before,
            "the rename created or orphaned plumbing",
        )
        self.assertFalse(
            any(obj.name.startswith("SL Rename Root.") for obj in bpy.data.objects),
            "an object-name-based helper set was left behind",
        )
        modifier = nodes.modifier_of(obj, stage)
        self.assertIsNotNone(modifier)
        self.assertIs(modifier.node_group, group, "the rename rebuilt the stage group")
        self.assertEqual(len(helper.data.vertices), 3, "the helper lost its geometry")
        self.assertEqual(len(definition[0]["mesh"].data.vertices), 3)
        self.assertEqual(len(instance.data.points), 1)
        self.assertEqual(len(nested.data.points), 1)

    def test_02_delete_removes_helpers_definitions_clouds_and_group(self):
        obj, stage = self._make_stack("Delete Root")
        record = self.stack_mod.stack_of(obj)
        stack_id = record.stack_id
        prefix = _stage_prefix(obj, stage.key)
        _add_triangle(self.helpers.helper_for(obj, stage.key, "mesh").data)
        parts, _instance, _nested = self._add_definition_and_clouds(obj, stage.key)
        part_data = [part["mesh"].data.name for part in parts]
        group_name = self.nodes.group_name(stage.key)
        self.assertIsNotNone(bpy.data.node_groups.get(group_name))

        bpy.data.objects.remove(obj, do_unlink=True)
        self.engine._rebuild_index()

        self.assertEqual(
            _stage_datablocks(prefix),
            set(),
            "deleting the object left plumbing behind",
        )
        self.assertIsNone(bpy.data.node_groups.get(group_name), "the stage group was orphaned")
        for name in part_data:
            self.assertIsNone(bpy.data.meshes.get(name), "definition data was orphaned")
        self.assertNotIn(stack_id, {
            record.stack_id for _obj, record in self.helpers._live_records()
        })

    def test_03_an_object_name_helper_set_migrates_with_its_geometry(self):
        obj, stage = self._make_stack("Legacy Root")
        helpers = self.helpers
        helper = helpers.helper_for(obj, stage.key, "mesh")
        _add_triangle(helper.data)
        parts, instance, nested = self._add_definition_and_clouds(obj, stage.key)
        mesh_data = helper.data.as_pointer()
        part_data = [part["mesh"].data.as_pointer() for part in parts]
        cloud_data = [instance.data.as_pointer(), nested.data.as_pointer()]
        stable_prefix = _stage_prefix(obj, stage.key)
        old_prefix = "SL %s.%s" % (obj.name, stage.key)

        # Rebuild the 0.6/early-0.7 naming exactly: every plumbing datablock
        # carries the object name instead of the stack id.
        for datablock in list(bpy.data.objects) + list(bpy.data.collections):
            if datablock.name == stable_prefix or datablock.name.startswith(stable_prefix + "."):
                datablock.name = datablock.name.replace(stable_prefix, old_prefix, 1)
        self.assertIsNone(helpers.helper_for(obj, stage.key, "mesh"))
        self.assertIn(old_prefix, _stage_datablocks(old_prefix))

        # 0.6 allowed a rename before saving: the old helper names carry the
        # previous object name, so migration cannot depend on the current one.
        obj.name = "Legacy Root Renamed"
        helpers.adopt_existing()

        self.assertIs(
            helpers.helper_for(obj, stage.key, "mesh"),
            helper,
            "migration replaced the helper instead of renaming it",
        )
        self.assertEqual(helper.data.as_pointer(), mesh_data, "migration lost the mesh")
        self.assertEqual(
            [part["mesh"].data.as_pointer() for part in parts],
            part_data,
            "migration lost definition geometry",
        )
        self.assertEqual(
            [instance.data.as_pointer(), nested.data.as_pointer()],
            cloud_data,
            "migration lost a row cloud",
        )
        self.assertEqual(_stage_datablocks(old_prefix), set(), "old plumbing names survived")
        self.assertEqual(len(helper.data.vertices), 3)

    def test_04_undo_and_redo_keep_one_identity(self):
        obj, stage = self._make_stack("Undo Root")
        helper = self.helpers.helper_for(obj, stage.key, "mesh")
        _add_triangle(helper.data)
        group = self.nodes.ensure_group(obj, stage.key)
        before = _stage_datablocks(_stage_prefix(obj, stage.key))

        obj.name = "Undo Root Renamed"
        self.engine._on_undo()
        self.engine._on_redo()

        self.assertIs(self.helpers.helper_for(obj, stage.key, "mesh"), helper)
        self.assertEqual(_stage_datablocks(_stage_prefix(obj, stage.key)), before)
        self.assertIs(self.nodes.modifier_of(obj, stage).node_group, group)
        self.assertEqual(len(helper.data.vertices), 3)

    def test_05_a_live_stack_with_a_missing_stage_record_is_not_swept(self):
        obj, stage = self._make_stack("Live Root")
        helper = self.helpers.helper_for(obj, stage.key, "mesh")
        _add_triangle(helper.data)
        record = self.stack_mod.stack_of(obj)
        prefix = _stage_prefix(obj, stage.key)
        before = _stage_datablocks(prefix)

        # What an undo step or a split between records can look like: the stack
        # still owns the identity, but the stage record is momentarily gone.
        record.stages.remove(0)
        self.engine._rebuild_index()

        self.assertIsNotNone(bpy.data.objects.get(helper.name), "a live stack's helper was swept")
        self.assertEqual(_stage_datablocks(prefix), before)

    def test_06_remove_helper_removes_every_part_and_cloud(self):
        obj, stage = self._make_stack("Remove Root")
        prefix = _stage_prefix(obj, stage.key)
        _add_triangle(self.helpers.helper_for(obj, stage.key, "mesh").data)
        parts, _instance, _nested = self._add_definition_and_clouds(obj, stage.key)
        part_names = {part["mesh"].name for part in parts}

        self.helpers.remove_helper(obj, stage.key)

        self.assertEqual(_stage_datablocks(prefix), set(), "remove_helper left plumbing")
        for name in part_names:
            self.assertIsNone(bpy.data.objects.get(name))


class LegacyFileMigrationTests(unittest.TestCase):
    """A 0.6/early-0.7 file saved with object-name plumbing keeps its output
    when it is opened by this build."""

    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="stageloom")
        import stageloom.blender.engine as engine
        import stageloom.blender.helpers as helpers

        cls.engine = engine
        cls.helpers = helpers
        cls.enterClassContext(isolate_data_root("stageloom-legacy-file-"))
        blend_dir = tempfile.mkdtemp(prefix="stageloom-legacy-blend-")
        cls.addClassCleanup(shutil.rmtree, blend_dir, ignore_errors=True)

        bpy.ops.mesh.primitive_cube_add(size=2.0)
        obj = bpy.context.object
        obj.name = "Legacy File Root"
        bpy.ops.stageloom.add_stage(label="Legacy File")
        stage = obj.gh_stack.stages[0]
        cls.key = stage.key
        helper = helpers.helper_for(obj, stage.key, "mesh")
        _add_triangle(helper.data)
        _collection, parts = helpers.ensure_definition_parts(obj, stage.key, 0, 2)
        for part in parts:
            _add_triangle(part["mesh"].data)
        instance = helpers.ensure_instance_cloud(obj, stage.key, 0)
        instance.data.resize(1)

        stable_prefix = helpers.helper_name(obj, stage.key)
        cls.old_prefix = "SL %s.%s" % (obj.name, stage.key)
        for datablock in list(bpy.data.objects) + list(bpy.data.collections):
            if datablock.name == stable_prefix or datablock.name.startswith(stable_prefix + "."):
                datablock.name = datablock.name.replace(stable_prefix, cls.old_prefix, 1)

        cls.blend = os.path.join(blend_dir, "legacy.blend")
        bpy.ops.wm.save_as_mainfile(filepath=cls.blend)
        bpy.ops.wm.open_mainfile(filepath=cls.blend)

    @classmethod
    def tearDownClass(cls):
        bpy.ops.preferences.addon_disable(module="stageloom")

    def test_the_old_file_keeps_its_geometry_under_the_stable_name(self):
        obj = bpy.data.objects["Legacy File Root"]
        stage = obj.gh_stack.stages[0]
        self.assertEqual(stage.key, self.key)
        helper = self.helpers.helper_for(obj, stage.key, "mesh")
        self.assertIsNotNone(helper, "the migrated helper is missing")
        self.assertEqual(len(helper.data.vertices), 3, "migration lost the helper geometry")
        self.assertTrue(helper.name.startswith("SL %s." % obj.gh_stack.stack_id))
        self.assertEqual(
            [
                item.name
                for item in list(bpy.data.objects) + list(bpy.data.collections)
                if item.name == self.old_prefix or item.name.startswith(self.old_prefix + ".")
            ],
            [],
            "old object-name plumbing survived the load",
        )
        self.assertIn(0, self.helpers.definition_collections(obj, stage.key))
        _collection, parts = self.helpers.ensure_definition_layout(obj, stage.key, 0)
        self.assertEqual(len(parts), 2, "the pasted part count did not survive")
        for part in parts:
            self.assertEqual(len(part["mesh"].data.vertices), 3)
        self.assertIsNotNone(
            self.helpers.instance_clouds(obj, stage.key).get(0),
            "the top-level row cloud did not survive",
        )


if __name__ == "__main__":
    unittest.main()
