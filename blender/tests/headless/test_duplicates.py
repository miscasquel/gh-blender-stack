"""Layer B, duplicated stacks: object duplication must not share a stage.

Run with:

    scripts/test-blender

Object duplication copies the stage record, the modifier and its node group
verbatim, so both objects used to claim the same stage key and rebuilt one node
group for whichever object ran last (acceptance A10). What this proves:

1. A pass splits the copy onto its own stage key and cache namespace, with its
   own node group and helpers; the original keeps everything it had.
2. Parameter values, keyframes, drivers and their UI settings move to the new
   key with the stage, so the copy does not silently lose its animation.
3. The original's node group is rebuilt against its own helpers even when the
   shared group had last been rebuilt for the copy.
4. The split key is deterministic, so redoing the duplication cannot strand the
   key the user wired into Grasshopper.
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

PARAMS = [
    {
        "id": "00000000-0000-4000-8000-0000000000aa",
        "label": "Scale",
        "type": "float",
        "default": 1.0,
        "min": 0.1,
        "max": 10.0,
        "step": 0.1,
    },
    {
        "id": "00000000-0000-4000-8000-0000000000bb",
        "label": "Driven",
        "type": "float",
        "default": 0.0,
        "min": 0.0,
        "max": 1.0,
        "step": 0.01,
    },
]


def _group_objects(group):
    """The Object Info datablocks a stage group reads."""
    if group is None:
        return set()
    return {
        node.inputs["Object"].default_value.name
        for node in group.nodes
        if node.bl_idname == "GeometryNodeObjectInfo"
        and node.inputs["Object"].default_value is not None
    }


class DuplicateStageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="stageloom")
        from stageloom.blender import bridge, engine, helpers, nodes, props
        from stageloom.blender import stack as stack_mod

        cls.engine = engine
        cls.helpers = helpers
        cls.nodes = nodes
        cls.props = props
        cls.stack_mod = stack_mod
        bpy.context.preferences.addons["stageloom"].preferences.data_root = tempfile.mkdtemp(
            prefix="stageloom-duplicates-"
        )
        # Direct passes only; no socket, no timer.
        bridge.state.update(connected=False, clients=0, listening=False)
        engine._dirty.clear()

        bpy.ops.mesh.primitive_cube_add(size=2.0)
        cls.obj = bpy.context.object
        cls.obj.name = "Original"
        bpy.ops.stageloom.add_stage(label="Original")
        cls.stage = cls.obj.gh_stack.stages[0]
        cls.original_key = cls.stage.key
        props.apply_schema(cls.obj, cls.stage, PARAMS)

        cls.scale = stack_mod.param_prop_name(cls.stage.key, PARAMS[0]["id"])
        cls.driven = stack_mod.param_prop_name(cls.stage.key, PARAMS[1]["id"])
        cls.obj[cls.scale] = 2.5
        cls.obj.keyframe_insert(data_path='["%s"]' % cls.scale, frame=1)
        cls.obj.keyframe_insert(data_path='["%s"]' % cls.scale, frame=10)
        cls.obj[cls.driven] = 0.25
        driver = cls.obj.driver_add('["%s"]' % cls.driven)
        driver.driver.type = "SCRIPTED"
        driver.driver.expression = "0.25"

        cls.dup = cls.obj.copy()
        bpy.context.scene.collection.objects.link(cls.dup)
        cls.dup.name = "Original.001"
        # Simulate the copy having been the last to rebuild the shared group.
        cls.nodes.ensure_group(cls.dup, cls.stage.key)
        engine._process_object(cls.dup)

    @classmethod
    def tearDownClass(cls):
        bpy.ops.preferences.addon_disable(module="stageloom")

    def _paths(self, obj):
        return [fcurve.data_path for _bag, fcurve in self.props._fcurve_slots(obj)]

    def _drivers(self, obj):
        return [driver.data_path for driver in obj.animation_data.drivers]

    def test_01_the_copy_gets_its_own_stage(self):
        dup_stage = self.dup.gh_stack.stages[0]
        self.assertNotEqual(dup_stage.key, self.stage.key, "the copy kept the stage key")
        self.assertNotEqual(
            self.dup.gh_stack.stack_id,
            self.obj.gh_stack.stack_id,
            "the copy kept the cache namespace",
        )
        self.assertEqual(
            dup_stage.key,
            self.stack_mod.duplicate_stage_key(self.stage.key, self.dup.name),
            "the split key is not deterministic",
        )
        self.assertEqual(dup_stage.state, "unbound")
        self.assertEqual(dup_stage.applied_out_hash, "", "the copy inherited an applied output")
        self.assertEqual(dup_stage.params[0].stage_key, dup_stage.key)

        orig_group = self.nodes.modifier_of(self.obj, self.stage).node_group
        dup_group = self.nodes.modifier_of(self.dup, dup_stage).node_group
        self.assertEqual(orig_group.name, self.nodes.group_name(self.stage.key))
        self.assertEqual(dup_group.name, self.nodes.group_name(dup_stage.key))
        self.assertNotEqual(orig_group.name, dup_group.name)
        self.assertEqual(
            _group_objects(orig_group),
            {self.helpers.helper_name(self.obj, self.stage.key, kind) for kind in self.helpers.KINDS},
            "the original group points at the copy's helpers",
        )
        self.assertEqual(
            _group_objects(dup_group),
            {self.helpers.helper_name(self.dup, dup_stage.key, kind) for kind in self.helpers.KINDS},
        )

    def test_02_parameters_move_with_the_stage(self):
        dup_stage = self.dup.gh_stack.stages[0]
        new_scale = self.stack_mod.param_prop_name(dup_stage.key, PARAMS[0]["id"])
        new_driven = self.stack_mod.param_prop_name(dup_stage.key, PARAMS[1]["id"])

        self.assertNotIn(self.scale, self.dup, "the copy kept the old parameter property")
        self.assertNotIn(self.driven, self.dup)
        self.assertIn(new_scale, self.dup)
        self.assertIn(new_driven, self.dup)
        self.assertAlmostEqual(float(self.dup[new_scale]), 2.5, places=5)
        self.assertAlmostEqual(float(self.dup[new_driven]), 0.25, places=5)
        # UI settings survive with the value.
        ui = self.dup.id_properties_ui(new_scale).as_dict()
        self.assertAlmostEqual(ui["min"], 0.1, places=5)
        self.assertAlmostEqual(ui["max"], 10.0, places=5)

        # The original still owns its properties and its animation.
        self.assertIn(self.scale, self.obj)
        self.assertIn(self.driven, self.obj)
        self.assertEqual(self.obj.animation_data.action.users, 1)
        self.assertEqual(self.dup.animation_data.action.users, 1)
        self.assertIn('["%s"]' % self.scale, self._paths(self.obj))
        self.assertNotIn('["%s"]' % new_scale, self._paths(self.obj))
        self.assertIn('["%s"]' % new_scale, self._paths(self.dup))
        self.assertIn('["%s"]' % self.driven, self._drivers(self.obj))
        self.assertIn('["%s"]' % new_driven, self._drivers(self.dup))

        fcurve = next(
            fcurve
            for _bag, fcurve in self.props._fcurve_slots(self.dup)
            if fcurve.data_path == '["%s"]' % new_scale
        )
        self.assertEqual(len(fcurve.keyframe_points), 2, "the copy lost keyframes")

    def test_03_one_object_cannot_take_the_other_group_back(self):
        dup_stage = self.dup.gh_stack.stages[0]
        self.engine._process_object(self.obj)
        self.engine._process_object(self.dup)

        self.assertEqual(self.stage.key, self.original_key, "the original was renamed")
        orig_group = self.nodes.modifier_of(self.obj, self.stage).node_group
        dup_group = self.nodes.modifier_of(self.dup, dup_stage).node_group
        self.assertEqual(
            _group_objects(orig_group),
            {self.helpers.helper_name(self.obj, self.stage.key, kind) for kind in self.helpers.KINDS},
        )
        self.assertEqual(
            _group_objects(dup_group),
            {self.helpers.helper_name(self.dup, dup_stage.key, kind) for kind in self.helpers.KINDS},
        )

    def test_04_a_loaded_file_splits_a_stale_duplicate(self):
        # A save taken after the duplication but before a pass carries the
        # shared key; the load hook must split it before the repair loop.
        copy = self.obj.copy()
        bpy.context.scene.collection.objects.link(copy)
        copy.name = "Original.002"
        self.engine._on_load()

        copy_stage = copy.gh_stack.stages[0]
        self.assertNotEqual(copy_stage.key, self.original_key)
        self.assertNotEqual(copy.gh_stack.stack_id, self.obj.gh_stack.stack_id)
        self.assertEqual(
            self.nodes.modifier_of(copy, copy_stage).node_group.name,
            self.nodes.group_name(copy_stage.key),
        )
        self.assertEqual(self.obj.gh_stack.stages[0].key, self.original_key)
        bpy.data.objects.remove(copy, do_unlink=True)
