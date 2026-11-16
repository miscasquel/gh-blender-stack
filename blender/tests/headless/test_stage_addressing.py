"""Layer B: stage addressing, modifier order and prefix invalidation."""

import unittest
from types import SimpleNamespace

import bpy
import numpy as np

from harness import isolate_data_root


def enable(case):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="stageloom")
    case.enterClassContext(isolate_data_root("stageloom-addressing-"))


def make_stack_object(name):
    bpy.ops.mesh.primitive_cube_add(size=2.0)
    obj = bpy.context.object
    obj.name = name
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    return obj


class StageOrderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        enable(cls)
        import stageloom.blender.engine as engine
        import stageloom.blender.nodes as nodes
        import stageloom.blender.ops as ops
        import stageloom.blender.stack as stack_mod

        cls.engine = engine
        cls.nodes = nodes
        cls.ops = ops
        cls.stack_mod = stack_mod
        cls.obj = make_stack_object("Ordered")
        bpy.ops.stageloom.add_stage(label="First")
        cls.stage_a = cls.obj.gh_stack.stages[0]
        bpy.ops.stageloom.add_stage(label="Second")
        cls.stage_b = cls.obj.gh_stack.stages[1]
        cls.record = stack_mod.stack_of(cls.obj)

    @classmethod
    def tearDownClass(cls):
        bpy.ops.preferences.addon_disable(module="stageloom")

    def test_01_processing_follows_modifier_order(self):
        mod_a = self.nodes.modifier_of(self.obj, self.stage_a)
        mod_b = self.nodes.modifier_of(self.obj, self.stage_b)
        self.assertIsNotNone(mod_a)
        self.assertIsNotNone(mod_b)

        order = list(self.obj.modifiers)
        try:
            self.obj.modifiers.move(order.index(mod_b), order.index(mod_a))
            calls = []
            real = self.engine._process_stage

            def record_stage(obj, record, stage):
                calls.append(stage.key)
                return "idle"

            self.engine._process_stage = record_stage
            try:
                self.engine._process_object(self.obj)
            finally:
                self.engine._process_stage = real
            self.assertEqual(
                calls,
                [self.stage_b.key, self.stage_a.key],
                "stages were processed in record order, not modifier order",
            )

            ordered = self.engine._ordered_stages(self.obj, self.record)
            self.assertEqual(
                [stage.key for stage in ordered],
                [self.stage_b.key, self.stage_a.key],
                "the panel/operator order did not follow the modifier stack",
            )
            # The operators receive the panel row number, so the same index must
            # name the same stage the panel drew on that row.
            context = SimpleNamespace(object=self.obj)
            for index, expected in enumerate(ordered):
                obj, record, stage = self.ops._stage(context, index)
                self.assertIs(obj, self.obj)
                self.assertEqual(record.stack_id, self.record.stack_id)
                self.assertEqual(
                    stage.key,
                    expected.key,
                    "operator index %d addressed %r, not %r"
                    % (index, getattr(stage, "key", None), expected.key),
                )
            self.assertEqual(
                self.ops._stage(context, len(ordered)),
                (None, None, None),
                "an out-of-range operator index resolved to a stage",
            )
        finally:
            # Restore the original order for any later pass in this class.
            current = list(self.obj.modifiers)
            if current.index(mod_a) > current.index(mod_b):
                self.obj.modifiers.move(current.index(mod_a), current.index(mod_b))


class ApplyInvalidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        enable(cls)
        import stageloom.blender.engine as engine
        import stageloom.blender.stack as stack_mod

        cls.engine = engine
        cls.stack_mod = stack_mod
        cls.obj = make_stack_object("Invalidated")
        bpy.ops.stageloom.add_stage(label="Apply")
        cls.stage = cls.obj.gh_stack.stages[0]

    @classmethod
    def tearDownClass(cls):
        bpy.ops.preferences.addon_disable(module="stageloom")

    def test_01_apply_keeps_entries_outside_its_stack_and_bumps_an_empty_clear(self):
        self.engine._apply(
            self.obj,
            self.stage,
            {
                "mesh.verts": np.array(
                    [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
                    dtype=np.float32,
                ),
                "mesh.face_counts": np.array([3], dtype=np.uint32),
                "mesh.face_indices": np.array([0, 1, 2], dtype=np.uint32),
            },
        )

        pointer = self.obj.as_pointer()
        own = (pointer, self.stage.key)
        unrelated = ("stale", "entry")
        self.engine._prefix_batch[own] = ({}, [], [])
        self.engine._prefix_batch[unrelated] = ({}, [], [])
        self.engine._prefix_batch_built = True
        before = self.engine._prefix_epoch

        self.engine._apply(self.obj, self.stage, {})

        # The stage's own prefix ends before its modifier, so this write cannot
        # change it; an unrelated object's entry must not be touched either.
        # Only downstream stages (none here) are invalidated.
        self.assertIn(own, self.engine._prefix_batch, "the applied stage's own entry was dropped")
        self.assertIn(unrelated, self.engine._prefix_batch, "an unrelated batch entry was dropped")
        self.assertGreater(
            self.engine._prefix_epoch,
            before,
            "clearing a non-empty helper did not move the prefix generation",
        )


class PrefixBatchTests(unittest.TestCase):
    """A multi-stage pass rebuilds only the prefixes it still needs.

    An apply can only stale the prefixes downstream of the writing stage, so the
    other entries survive and the pass rebuilds one prefix at a time.
    """

    @classmethod
    def setUpClass(cls):
        enable(cls)
        import stageloom.blender.engine as engine
        import stageloom.blender.stack as stack_mod

        cls.engine = engine
        cls.stack_mod = stack_mod
        cls.obj = make_stack_object("Batch")
        for label in ("BatchA", "BatchB", "BatchC"):
            bpy.ops.stageloom.add_stage(label=label)
        cls.record = stack_mod.stack_of(cls.obj)
        cls.stages = list(cls.record.stages)
        cls.position = {stage.key: index for index, stage in enumerate(cls.stages)}

    @classmethod
    def tearDownClass(cls):
        bpy.ops.preferences.addon_disable(module="stageloom")

    def tearDown(self):
        self._clear_session()

    def _clear_session(self):
        engine = self.engine
        engine._prefix_memo.clear()
        engine._prefix_batch.clear()
        engine._prefix_batch_built = False
        engine._prefix_batching = False
        engine._pending.clear()
        engine._sent.clear()
        engine._verify.clear()
        for stage in self.stages:
            stage.applied_out_hash = ""
            stage.rid = ""
            stage.state = "ok"

    def test_01_a_one_stage_rebuild_evaluates_only_that_stage(self):
        engine = self.engine
        pointer = self.obj.as_pointer()
        stage_b = self.stages[1]
        for stage in self.stages:
            engine._prefix_batch[(pointer, stage.key)] = ("old", stage.key)
        engine._prefix_batch_built = True
        calls = []
        real_clone_prefixes = engine._clone_prefixes

        def counting_clone_prefixes(obj, stages, subd):
            calls.append([stage.key for stage, _index in stages])
            return real_clone_prefixes(obj, stages, subd)

        engine._clone_prefixes = counting_clone_prefixes
        try:
            engine._build_prefix_batch(self.obj, self.record, stage_b)
        finally:
            engine._clone_prefixes = real_clone_prefixes

        self.assertEqual(calls, [[stage_b.key]], "a one-stage rebuild evaluated the whole stack")
        for stage in self.stages:
            if stage is stage_b:
                continue
            self.assertEqual(
                engine._prefix_batch[(pointer, stage.key)],
                ("old", stage.key),
                "a one-stage rebuild replaced another stage's prefix",
            )

    def test_02_invalidation_follows_the_modifier_stack_not_the_record(self):
        engine = self.engine
        pointer = self.obj.as_pointer()
        stage_a, stage_b, stage_c = self.stages
        modifier_a = engine.nodes.modifier_of(self.obj, stage_a)
        modifier_c = engine.nodes.modifier_of(self.obj, stage_c)
        original = list(self.obj.modifiers)
        current = list(self.obj.modifiers)
        self._clear_session()
        try:
            # Stack: C, A, B. The record still says A, B, C, so an apply of A
            # must stale B (below it in the stack), not C (above it).
            self.obj.modifiers.move(current.index(modifier_c), current.index(modifier_a))
            for stage in self.stages:
                engine._prefix_batch[(pointer, stage.key)] = ({}, [], [])
            engine._prefix_batch_built = True

            engine._apply(self.obj, stage_a, {})

            keys = {key for (owner, key) in engine._prefix_batch if owner == pointer}
            self.assertIn(stage_c.key, keys, "an upstream stack entry was invalidated")
            self.assertIn(stage_a.key, keys, "the applied stage's own prefix was invalidated")
            self.assertNotIn(stage_b.key, keys, "a downstream stack entry survived")
        finally:
            for position, modifier in enumerate(original):
                stacked = list(self.obj.modifiers)
                self.obj.modifiers.move(stacked.index(modifier), position)


class SurfaceStageRejectionTests(unittest.TestCase):
    """A surface object cannot take the Geometry Nodes modifier a stage needs."""

    @classmethod
    def setUpClass(cls):
        enable(cls)
        import stageloom.blender.ops as ops

        cls.ops = ops

    @classmethod
    def tearDownClass(cls):
        bpy.ops.preferences.addon_disable(module="stageloom")

    def test_01_surface_is_not_a_stack_type_and_add_stage_refuses_cleanly(self):
        self.assertNotIn("SURFACE", self.ops.STACK_TYPES)
        bpy.ops.surface.primitive_nurbs_surface_sphere_add(radius=1.0)
        obj = bpy.context.object
        self.assertEqual(obj.type, "SURFACE")

        with self.assertRaises(RuntimeError) as caught:
            bpy.ops.stageloom.add_stage(label="Surface")

        message = str(caught.exception).lower()
        self.assertIn("surface objects cannot hold", message)
        self.assertEqual(
            len(obj.gh_stack.stages),
            0,
            "the refused add left a stage record behind",
        )
        self.assertEqual(
            [modifier for modifier in obj.modifiers if modifier.type == "NODES"],
            [],
            "the refused add created a Geometry Nodes modifier",
        )

    def test_02_surface_object_sections_still_mesh_for_an_old_document(self):
        """A document saved before 0.4.1 can hold a stage on a surface. The
        read path meshes that surface rather than dropping it; the refusal is
        only for adding a new stage."""
        from stageloom.blender import geometry_io

        bpy.ops.surface.primitive_nurbs_surface_sphere_add(radius=1.0)
        obj = bpy.context.object
        self.assertEqual(obj.type, "SURFACE")

        notes = []
        sections, _attributes = geometry_io.object_to_sections(obj, notes=notes)
        names = [section[0] for section in sections]
        self.assertTrue(any(name.startswith("mesh.") for name in names), names)
        self.assertTrue(any("surface was meshed" in note for note in notes), notes)


class _FakeGroup(object):
    def __init__(self, name):
        self.name = name


class _FakeModifier(object):
    def __init__(self, uid, name, group):
        self.persistent_uid = uid
        self.name = name
        self.type = "NODES"
        self.node_group = group


class _FakeObject(object):
    def __init__(self, modifiers):
        self.modifiers = modifiers


class _FakeStage(object):
    def __init__(self, key, label, uid):
        self.key = key
        self.label = label
        self.modifier_uid = uid


class ModifierAmbiguityTests(unittest.TestCase):
    def test_01_one_uid_match_wins(self):
        import stageloom.blender.nodes as nodes

        group = _FakeGroup(nodes.group_name("stage-a"))
        mod = _FakeModifier(11, "Stageloom · A", group)
        stage = _FakeStage("stage-a", "A", 11)
        stack = _FakeObject([mod])
        self.assertIs(nodes.modifier_of(stack, stage), mod)

    def test_02_repeated_uid_prefers_the_group_name(self):
        import stageloom.blender.nodes as nodes

        wanted = _FakeGroup(nodes.group_name("stage-a"))
        other = _FakeGroup(nodes.group_name("stage-b"))
        mod_a = _FakeModifier(7, "Stageloom · A", wanted)
        mod_b = _FakeModifier(7, "Stageloom · B", other)
        stage = _FakeStage("stage-a", "A", 7)
        stack = _FakeObject([mod_b, mod_a])
        self.assertIs(nodes.modifier_of(stack, stage), mod_a)

    def test_03_repeated_uid_without_a_name_match_is_ambiguous(self):
        import stageloom.blender.nodes as nodes

        mod_a = _FakeModifier(7, "Stageloom · A", _FakeGroup("other-a"))
        mod_b = _FakeModifier(7, "Stageloom · B", _FakeGroup("other-b"))
        stage = _FakeStage("stage-a", "A", 7)
        stack = _FakeObject([mod_a, mod_b])
        self.assertIsNone(nodes.modifier_of(stack, stage))
        self.assertEqual(nodes.modifier_uid_count(stack, 7), 2)

    def test_04_by_name_requires_one_unattached_candidate(self):
        import stageloom.blender.nodes as nodes

        stage = _FakeStage("stage-a", "A", 0)
        one = _FakeModifier(0, "Stageloom · A.001", None)
        self.assertIs(nodes.modifier_by_name(_FakeObject([one]), stage), one)

        two = _FakeModifier(0, "Stageloom · A.002", None)
        stack = _FakeObject([one, two])
        self.assertIsNone(nodes.modifier_by_name(stack, stage))
        self.assertEqual(nodes.modifier_name_candidates(stack, stage), [one, two])


class LegacyNameAmbiguityTests(unittest.TestCase):
    """Two pre-uid stages with the same label must survive an ambiguous name.

    Before the modifier uid existed, a lost node group left the creation name
    as the only link. Two same-label stages therefore have two candidate
    modifiers and no safe way to tell which is which. That is a repair case,
    not a deletion: removing both records destroyed the stage data.
    """

    @classmethod
    def setUpClass(cls):
        enable(cls)
        import stageloom.blender.engine as engine
        import stageloom.blender.stack as stack_mod

        cls.engine = engine
        cls.stack_mod = stack_mod
        cls.obj = make_stack_object("LegacyNames")
        bpy.ops.stageloom.add_stage(label="Stage")
        bpy.ops.stageloom.add_stage(label="Stage")
        cls.record = stack_mod.stack_of(cls.obj)

    @classmethod
    def tearDownClass(cls):
        bpy.ops.preferences.addon_disable(module="stageloom")

    def test_two_same_label_pre_uid_stages_are_kept(self):
        stages = list(self.record.stages)
        self.assertEqual(len(stages), 2)
        modifiers = [mod for mod in self.obj.modifiers if mod.type == "NODES"]
        self.assertEqual(len(modifiers), 2)
        for modifier in modifiers:
            modifier.node_group = None
        for stage in stages:
            stage.modifier_uid = 0

        keys = [stage.key for stage in stages]
        removed = self.engine._drop_removed_stages(self.obj, self.record)

        self.assertFalse(removed, "an ambiguous name deleted the stage records")
        self.assertEqual(
            [stage.key for stage in self.record.stages],
            keys,
            "the ambiguous stage records were not preserved",
        )
        for stage in self.record.stages:
            self.assertEqual(stage.state, self.stack_mod.STATE_ERROR)
            self.assertIn("ambiguous modifier name", stage.error)
