"""Layer B, stage addressing and prefix invalidation.

The multi-stage live report exposed three latent addressing assumptions:
1. record order was treated as stack order, so dragging a stage modifier could
   make a downstream stage evaluate its prefix before the upstream stage;
2. a prefix batch built earlier in the pass was reused after an apply, so a
   downstream stage could read the pre-apply helper;
3. a surface object silently gave every stage the base surface, so a second
   surface stage ran on the first stage's input.

The modifier-uid tests use small fakes: the point is the ambiguity rule, not a
real Blender modifier.
"""

import os
import sys
import tempfile
import unittest

import bpy
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
BLENDER_PKG = os.path.join(REPO, "blender")
if BLENDER_PKG not in sys.path:
    sys.path.insert(0, BLENDER_PKG)


def enable():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="stageloom")
    bpy.context.preferences.addons["stageloom"].preferences.data_root = (
        tempfile.mkdtemp(prefix="stageloom-addressing-")
    )


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
        enable()
        import stageloom.blender.engine as engine
        import stageloom.blender.nodes as nodes
        import stageloom.blender.stack as stack_mod

        cls.engine = engine
        cls.nodes = nodes
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
        finally:
            # Restore the original order for any later pass in this class.
            current = list(self.obj.modifiers)
            if current.index(mod_a) > current.index(mod_b):
                self.obj.modifiers.move(current.index(mod_a), current.index(mod_b))


class ApplyInvalidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        enable()
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

    def test_01_apply_clears_the_prefix_batch_and_bumps_an_empty_clear(self):
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

        self.engine._prefix_batch[("stale", "entry")] = ({}, [], [])
        self.engine._prefix_batch_built = True
        before = self.engine._prefix_epoch

        self.engine._apply(self.obj, self.stage, {})

        self.assertFalse(self.engine._prefix_batch, "the stale prefix batch survived")
        self.assertFalse(self.engine._prefix_batch_built)
        self.assertGreater(
            self.engine._prefix_epoch,
            before,
            "clearing a non-empty helper did not move the prefix generation",
        )


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
        self.assertIsNone(nodes.modifier_by_name(_FakeObject([one, two]), stage))
