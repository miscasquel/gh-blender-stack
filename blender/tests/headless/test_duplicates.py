"""Layer B: duplicated stacks get independent stage keys, or a strict conflict."""

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


def enable():
    """A clean scene with the add-on enabled and no socket connection."""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="stageloom")
    from stageloom.blender import bridge, engine, helpers, nodes, props
    from stageloom.blender import stack as stack_mod

    bpy.context.preferences.addons["stageloom"].preferences.data_root = tempfile.mkdtemp(
        prefix="stageloom-duplicates-"
    )
    # Direct passes only; no socket, no timer.
    bridge.state.update(connected=False, clients=0, listening=False)
    engine._dirty.clear()
    return engine, helpers, nodes, props, stack_mod, bridge


def make_stack(name, label, params=PARAMS):
    bpy.ops.mesh.primitive_cube_add(size=2.0)
    obj = bpy.context.object
    obj.name = name
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.stageloom.add_stage(label=label)
    stage = obj.gh_stack.stages[-1]
    if params:
        from stageloom.blender import props as props_mod

        props_mod.apply_schema(obj, stage, params)
    return obj, stage


def duplicate_object(obj, name):
    copy = obj.copy()
    bpy.context.scene.collection.objects.link(copy)
    copy.name = name
    return copy


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
        cls.engine, cls.helpers, cls.nodes, cls.props, cls.stack_mod, cls.bridge = enable()

        bpy.ops.mesh.primitive_cube_add(size=2.0)
        cls.obj = bpy.context.object
        cls.obj.name = "Original"
        bpy.ops.stageloom.add_stage(label="Original")
        cls.stage = cls.obj.gh_stack.stages[0]
        cls.original_key = cls.stage.key
        cls.props.apply_schema(cls.obj, cls.stage, PARAMS)

        cls.scale = cls.stack_mod.param_prop_name(cls.stage.key, PARAMS[0]["id"])
        cls.driven = cls.stack_mod.param_prop_name(cls.stage.key, PARAMS[1]["id"])
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
        cls.engine._process_object(cls.dup)

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

    def test_04_a_loaded_file_reports_a_stale_duplicate(self):
        # A save taken after duplication but before a pass carries the shared
        # key. A load cannot prove which claimant is the original, so both are
        # errored and neither is silently split or sent.
        copy = self.obj.copy()
        bpy.context.scene.collection.objects.link(copy)
        copy.name = "Original.002"
        self.engine._on_load()

        copy_stage = copy.gh_stack.stages[0]
        self.assertEqual(copy_stage.key, self.original_key, "the load split by name")
        self.assertEqual(self.stage.key, self.original_key)
        for stage in (self.stage, copy_stage):
            self.assertEqual(stage.state, "error")
            self.assertIn(self.original_key, stage.error)
            self.assertIn("Original", stage.error)
            self.assertIn("Original.002", stage.error)
        self.assertIn(self.original_key, self.engine._stage_conflicts)
        self.assertNotIn(self.original_key, self.engine._stage_owners)
        self.assertEqual(list(self.engine._stage_views(self.original_key)), [])

        sends = self.engine.stats["sends"]
        applies = self.engine.stats["applies"]
        self.engine._dirty.clear()
        self.engine._process_object(self.obj)
        self.engine._process_object(copy)
        self.engine._handle_response(
            {
                "type": "response",
                "stage": self.original_key,
                "rid": "sha256:" + "0" * 64,
                "status": "ok",
                "out_hash": "sha256:" + "1" * 64,
            },
            {},
        )
        self.assertEqual(self.engine.stats["sends"], sends)
        self.assertEqual(self.engine.stats["applies"], applies)

        bpy.data.objects.remove(copy, do_unlink=True)
        self.engine._rebuild_index()
        self.assertEqual(self.stage.state, "idle", "the survivor did not leave the conflict")
        self.assertNotIn(self.original_key, self.engine._stage_conflicts)
        self.assertIs(self.engine._stage_owners[self.original_key], self.obj)


class RuntimeDuplicateIdentityTests(unittest.TestCase):
    """Ownership comes from the previous index's object pointers, not names."""

    def setUp(self):
        self.engine, self.helpers, self.nodes, self.props, self.stack_mod, self.bridge = enable()

    def tearDown(self):
        bpy.ops.preferences.addon_disable(module="stageloom")

    def test_01_a_renamed_copy_is_split_and_the_original_keeps_its_key(self):
        original, stage = make_stack("Chair", "Chair")
        key = stage.key
        copy = duplicate_object(original, "AChair")
        # Both names now sort on the wrong side for the old name rule.
        original.name = "ZOriginal"
        self.engine._process_object(copy)

        copy_stage = copy.gh_stack.stages[0]
        self.assertEqual(stage.key, key, "the original lost its key to the rename")
        self.assertNotEqual(copy_stage.key, key, "the copy kept the stage key")
        self.assertEqual(
            copy_stage.key,
            self.stack_mod.duplicate_stage_key(key, copy.name),
        )
        self.assertNotEqual(copy.gh_stack.stack_id, original.gh_stack.stack_id)
        self.assertIs(self.engine._stage_owners[key], original)
        self.assertIs(self.engine._stage_owners[copy_stage.key], copy)
        self.assertNotIn(key, self.engine._stage_conflicts)

        group = self.nodes.modifier_of(original, stage).node_group
        self.assertEqual(
            _group_objects(group),
            {
                self.helpers.helper_name(original, key, kind)
                for kind in self.helpers.KINDS
            },
            "the original group points at the copy's helpers",
        )

    def test_02_a_gone_original_leaves_the_remaining_claimant_on_the_key(self):
        original, stage = make_stack("Solo", "Solo")
        key = stage.key
        copy = duplicate_object(original, "SoloCopy")
        bpy.data.objects.remove(original, do_unlink=True)

        self.engine._process_object(copy)
        copy_stage = copy.gh_stack.stages[0]
        self.assertEqual(copy_stage.key, key, "the only claimant was split")
        self.assertNotEqual(copy_stage.state, "error")
        self.assertIs(self.engine._stage_owners[key], copy)
        self.assertNotIn(key, self.engine._stage_conflicts)

    def test_03_a_script_copy_is_a_new_claimant(self):
        original, stage = make_stack("Scripted", "Scripted")
        key = stage.key
        copy = original.copy()
        bpy.context.scene.collection.objects.link(copy)
        copy.name = "Scripted Copy"

        self.engine._process_object(copy)
        self.assertEqual(stage.key, key)
        self.assertNotEqual(copy.gh_stack.stages[0].key, key)
        self.assertNotIn(key, self.engine._stage_conflicts)

    def test_04_undo_restoring_a_shared_key_is_a_conflict(self):
        original, stage = make_stack("Undone", "Undone")
        key = stage.key
        copy = duplicate_object(original, "UndoneCopy")
        self.engine._process_object(copy)
        copy_stage = copy.gh_stack.stages[0]
        self.assertNotEqual(copy_stage.key, key)

        # Undo restores the pre-split record: the shared key is ambiguous, so
        # the history hook must conflict instead of splitting by name again.
        copy_stage.key = key
        self.engine._after_history_step()

        self.assertEqual(stage.key, key)
        self.assertEqual(copy_stage.key, key, "undo silently split the key")
        for item in (stage, copy_stage):
            self.assertEqual(item.state, "error")
            self.assertIn(key, item.error)
        self.assertIn(key, self.engine._stage_conflicts)
        self.assertNotIn(key, self.engine._stage_owners)


class StrictConflictTests(unittest.TestCase):
    """A load cannot prove an owner: a shared key is a conflict, not a split."""

    def setUp(self):
        self.engine, self.helpers, self.nodes, self.props, self.stack_mod, self.bridge = enable()
        self.chair, self.chair_stage = make_stack("Chair", "Chair")
        self.scale = self.stack_mod.param_prop_name(
            self.chair_stage.key, PARAMS[0]["id"]
        )
        self.chair[self.scale] = 2.5
        self.copy = duplicate_object(self.chair, "AChair")
        self.engine._on_load()
        self.copy_stage = self.copy.gh_stack.stages[0]
        self.key = self.chair_stage.key

    def tearDown(self):
        bpy.ops.preferences.addon_disable(module="stageloom")

    def test_01_a_legacy_duplicate_errors_both_stages_and_never_splits(self):
        self.assertEqual(self.copy_stage.key, self.key, "the load split by name")
        self.assertEqual(self.chair_stage.key, self.key)
        self.assertEqual(self.chair.gh_stack.stack_id, self.copy.gh_stack.stack_id)
        for stage in (self.chair_stage, self.copy_stage):
            self.assertEqual(stage.state, "error")
            self.assertIn(self.key, stage.error)
            self.assertIn("Chair", stage.error)
            self.assertIn("AChair", stage.error)
        self.assertEqual(self.engine._stage_claims[self.key], 2)
        self.assertIn(self.key, self.engine._stage_conflicts)
        self.assertNotIn(self.key, self.engine._stage_owners)
        self.assertEqual(list(self.engine._stage_views(self.key)), [])

        sends = self.engine.stats["sends"]
        applies = self.engine.stats["applies"]
        before = (self.chair_stage.applied_out_hash, self.copy_stage.applied_out_hash)
        self.engine._dirty.clear()
        self.engine._process_object(self.chair)
        self.engine._process_object(self.copy)
        self.engine._handle_response(
            {
                "type": "response",
                "stage": self.key,
                "rid": "sha256:" + "0" * 64,
                "status": "ok",
                "out_hash": "sha256:" + "1" * 64,
            },
            {},
        )
        self.assertEqual(self.engine.stats["sends"], sends)
        self.assertEqual(self.engine.stats["applies"], applies)
        self.assertEqual(
            (self.chair_stage.applied_out_hash, self.copy_stage.applied_out_hash),
            before,
        )
        self.assertFalse(any(key == self.key for _stack, key in self.engine._pending))
        self.assertFalse(any(key == self.key for _stack, key in self.engine._sent))

    def test_02_a_saved_conflict_error_clears_when_the_key_becomes_unique(self):
        # A file saved while conflicted can carry the derived error after the
        # duplicate was deleted. Loading it again must not leave a unique stage
        # looking broken.
        bpy.data.objects.remove(self.copy, do_unlink=True)
        self.engine._on_load()
        self.assertEqual(self.chair_stage.key, self.key)
        self.assertEqual(self.chair_stage.error, "")
        self.assertNotEqual(self.chair_stage.state, "error")
        self.assertNotIn(self.key, self.engine._stage_conflicts)

    def test_03_conflicted_keys_never_reach_process_or_response(self):
        def unexpected(*args, **kwargs):
            raise AssertionError("a conflicted key reached the send/apply path")

        real_process = self.engine._process_stage
        real_views = self.engine._stage_views
        real_apply = self.engine._apply
        self.engine._process_stage = unexpected
        self.engine._stage_views = unexpected
        self.engine._apply = unexpected
        try:
            self.engine._process_object(self.chair)
            self.engine._process_object(self.copy)
            self.engine._handle_response(
                {
                    "type": "response",
                    "stage": self.key,
                    "rid": "sha256:" + "0" * 64,
                    "status": "ok",
                    "out_hash": "sha256:" + "1" * 64,
                },
                {},
            )
        finally:
            self.engine._process_stage = real_process
            self.engine._stage_views = real_views
            self.engine._apply = real_apply

    def test_04_the_panel_operator_resolves_one_stage_per_key(self):
        old_key = self.key
        # Leave plumbing behind so the resolution has something stale to remove.
        self.helpers.ensure_helpers(self.copy, old_key)
        self.nodes.ensure_group(self.copy, old_key)

        bpy.context.view_layer.objects.active = self.copy
        self.copy.select_set(True)
        result = bpy.ops.stageloom.resolve_duplicate_key(index=0)
        self.assertEqual(result, {"FINISHED"})

        new_key = self.copy_stage.key
        self.assertNotEqual(new_key, old_key)
        self.assertNotEqual(
            new_key,
            self.stack_mod.duplicate_stage_key(old_key, self.copy.name),
            "the resolution reused the name-based split key",
        )
        self.assertEqual(self.chair_stage.key, old_key)
        self.assertIs(self.engine._stage_owners[old_key], self.chair)
        self.assertIs(self.engine._stage_owners[new_key], self.copy)
        self.assertNotIn(old_key, self.engine._stage_conflicts)
        self.assertNotIn(new_key, self.engine._stage_conflicts)
        self.assertEqual(self.chair_stage.error, "")
        self.assertEqual(self.copy_stage.error, "")
        self.assertEqual(self.engine._stage_claims[old_key], 1)

        new_scale = self.stack_mod.param_prop_name(new_key, PARAMS[0]["id"])
        self.assertNotIn(self.scale, self.copy)
        self.assertIn(new_scale, self.copy)
        self.assertAlmostEqual(float(self.copy[new_scale]), 2.5, places=5)
        self.assertEqual(self.copy_stage.params[0].stage_key, new_key)

        for key in (old_key, new_key):
            for kind in self.helpers.KINDS:
                self.assertIsNone(self.helpers.helper_for(self.copy, key, kind))
            self.assertEqual(self.helpers.definition_collections(self.copy, key), {})

        # The next pass repairs the group under the new key.
        self.engine._dirty.clear()
        self.engine._process_object(self.copy)
        modifier = self.nodes.modifier_of(self.copy, self.copy_stage)
        self.assertIsNotNone(modifier.node_group)
        self.assertEqual(modifier.node_group.name, self.nodes.group_name(new_key))
        self.assertNotEqual(self.copy_stage.state, "error")


class SameObjectConflictTests(unittest.TestCase):
    """Two stage records on one object sharing a key are ambiguous too."""

    def setUp(self):
        self.engine, self.helpers, self.nodes, self.props, self.stack_mod, self.bridge = enable()
        self.obj, self.stage_a = make_stack("Stacked", "A")
        bpy.context.view_layer.objects.active = self.obj
        self.obj.select_set(True)
        bpy.ops.stageloom.add_stage(label="B")
        self.stage_b = self.obj.gh_stack.stages[-1]
        self.key = self.stage_a.key
        self.stage_b.key = self.key
        self.engine._reindex_object(self.obj)

    def tearDown(self):
        bpy.ops.preferences.addon_disable(module="stageloom")

    def test_01_same_object_claims_are_a_conflict(self):
        self.assertEqual(self.engine._stage_claims[self.key], 2)
        self.assertIn(self.key, self.engine._stage_conflicts)
        self.assertNotIn(self.key, self.engine._stage_owners)
        for stage in (self.stage_a, self.stage_b):
            self.assertEqual(stage.state, "error")
            self.assertIn(self.key, stage.error)
            self.assertIn("2 stages", stage.error)
        self.assertEqual(list(self.engine._stage_views(self.key)), [])

        sends = self.engine.stats["sends"]
        self.engine._dirty.clear()
        self.engine._process_object(self.obj)
        self.assertEqual(self.engine.stats["sends"], sends)

    def test_02_resolving_one_stage_leaves_the_other_on_the_old_key(self):
        bpy.context.view_layer.objects.active = self.obj
        self.obj.select_set(True)
        self.assertEqual(
            bpy.ops.stageloom.resolve_duplicate_key(index=0), {"FINISHED"}
        )
        new_key = self.stage_a.key
        self.assertNotEqual(new_key, self.key)
        self.assertEqual(self.stage_b.key, self.key)
        self.assertIs(self.engine._stage_owners[self.key], self.obj)
        self.assertIs(self.engine._stage_owners[new_key], self.obj)
        self.assertNotIn(self.key, self.engine._stage_conflicts)
        self.assertNotIn(new_key, self.engine._stage_conflicts)
        self.assertEqual(self.engine._stage_claims[self.key], 1)
        self.assertEqual(self.engine._stage_claims[new_key], 1)
        self.assertEqual(self.stage_a.error, "")
        self.assertEqual(self.stage_b.error, "")


class NormalReloadTests(unittest.TestCase):
    """Distinct keys keep their owner across a real save and reopen."""

    @classmethod
    def setUpClass(cls):
        cls.engine, cls.helpers, cls.nodes, cls.props, cls.stack_mod, cls.bridge = enable()
        _obj_a, stage_a = make_stack("NormalA", "NormalA", params=None)
        _obj_b, stage_b = make_stack("NormalB", "NormalB", params=None)
        cls.keys = (stage_a.key, stage_b.key)
        cls.blend = os.path.join(
            tempfile.mkdtemp(prefix="stageloom-normal-reload-"), "normal.blend"
        )
        bpy.ops.wm.save_as_mainfile(filepath=cls.blend)
        bpy.ops.wm.open_mainfile(filepath=cls.blend)

    @classmethod
    def tearDownClass(cls):
        bpy.ops.preferences.addon_disable(module="stageloom")

    def test_01_distinct_keys_survive_the_reload(self):
        obj_a = bpy.data.objects["NormalA"]
        obj_b = bpy.data.objects["NormalB"]
        stage_a = obj_a.gh_stack.stages[0]
        stage_b = obj_b.gh_stack.stages[0]
        self.assertEqual((stage_a.key, stage_b.key), self.keys)
        self.assertNotEqual(stage_a.key, stage_b.key)
        for obj, stage in ((obj_a, stage_a), (obj_b, stage_b)):
            self.assertNotEqual(stage.state, "error")
            self.assertNotIn(stage.key, self.engine._stage_conflicts)
            self.assertIs(self.engine._stage_owners[stage.key], obj)


if __name__ == "__main__":
    unittest.main()
