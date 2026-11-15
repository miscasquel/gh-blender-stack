"""Layer B: upstream stage dependencies, and later the chain bake.

Run with:

    scripts/test-blender

W2.2: a stage depends on the stages before it on its own object and on the
stages of objects or collections its prefix reads, transitively. A downstream
consumer is not an upstream dependency, even when it reads the selected stage.
The bake tests (W2.1) and the offline render acceptance (W2.3) extend this file.
"""

import os
import sys
import unittest

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
BLENDER_PKG = os.path.join(REPO, "blender")
for path in (BLENDER_PKG, HERE):
    if path not in sys.path:
        sys.path.insert(0, path)

from test_duplicates import enable, make_stack  # noqa: E402

from stageloom.blender.engine.dependencies import stage_dependencies  # noqa: E402


def _cube(name):
    bpy.ops.mesh.primitive_cube_add(size=2.0)
    obj = bpy.context.object
    obj.name = name
    return obj


def _add_stage(obj, label):
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.stageloom.add_stage(label=label)
    return obj.gh_stack.stages[-1]


def _reading_group(name, reference):
    """A Geometry Nodes group that reads an Object or a Collection."""
    group = bpy.data.node_groups.new(name, "GeometryNodeTree")
    group.interface.new_socket(
        "Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry"
    )
    if isinstance(reference, bpy.types.Collection):
        node = group.nodes.new("GeometryNodeCollectionInfo")
        node.inputs["Collection"].default_value = reference
        output = node.outputs["Instances"]
    else:
        node = group.nodes.new("GeometryNodeObjectInfo")
        node.inputs["Object"].default_value = reference
        node.transform_space = "ORIGINAL"
        output = node.outputs["Geometry"]
    group_out = group.nodes.new("NodeGroupOutput")
    group.links.new(output, group_out.inputs["Geometry"])
    return group


def _read_before(obj, name, reference):
    """Insert a reading modifier so a stage added next sits after it."""
    modifier = obj.modifiers.new(name, "NODES")
    modifier.node_group = _reading_group(name + "Group", reference)
    obj.modifiers.active = modifier
    return modifier


class DependencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine, cls.helpers, cls.nodes, cls.props, cls.stack_mod, cls.bridge = enable()

    @classmethod
    def tearDownClass(cls):
        bpy.ops.preferences.addon_disable(module="stageloom")

    def setUp(self):
        # Every test owns its scene: remove the user objects and the staging
        # plumbing they link, then build the few objects the test names.
        for obj in list(bpy.data.objects):
            bpy.data.objects.remove(obj, do_unlink=True)

    def _assert_stages(self, pairs, expected):
        self.assertEqual(len(pairs), len(expected), "dependency count differs")
        for (owner, stage), (want_owner, want_stage) in zip(pairs, expected):
            self.assertEqual(
                owner.as_pointer(),
                want_owner.as_pointer(),
                "dependency object differs",
            )
            self.assertEqual(
                stage.as_pointer(),
                want_stage.as_pointer(),
                "dependency stage differs",
            )

    def test_01_same_object_chain_is_upstream_only(self):
        obj = _cube("Chain")
        stage_a = _add_stage(obj, "A")
        bevel = obj.modifiers.new("MidBevel", "BEVEL")
        obj.modifiers.active = bevel
        stage_b = _add_stage(obj, "B")

        self._assert_stages(stage_dependencies(obj, stage_b), [(obj, stage_a)])
        self.assertEqual(
            stage_dependencies(obj, stage_a),
            [],
            "the first stage depends on a downstream stage",
        )

    def test_02_a_prefix_object_reference_reaches_its_stage(self):
        source, source_stage = make_stack("Source", "Source", params=None)
        holder = _cube("Holder")
        _read_before(holder, "ReadsSource", source)
        holder_stage = _add_stage(holder, "Holder")

        self._assert_stages(
            stage_dependencies(holder, holder_stage), [(source, source_stage)]
        )

    def test_03_transitive_object_references_are_walked(self):
        source, source_stage = make_stack("Source", "Source", params=None)
        middle = _cube("Middle")
        _read_before(middle, "ReadsSource", source)
        middle_stage = _add_stage(middle, "Middle")
        holder = _cube("Holder")
        _read_before(holder, "ReadsMiddle", middle)
        holder_stage = _add_stage(holder, "Holder")

        self._assert_stages(
            stage_dependencies(holder, holder_stage),
            [(middle, middle_stage), (source, source_stage)],
        )

    def test_04_a_collection_reference_contributes_its_members(self):
        source, source_stage = make_stack("Source", "Source", params=None)
        collection = bpy.data.collections.new("DependencySources")
        bpy.context.scene.collection.children.link(collection)
        collection.objects.link(source)
        holder = _cube("Holder")
        _read_before(holder, "ReadsCollection", collection)
        holder_stage = _add_stage(holder, "Holder")

        self._assert_stages(
            stage_dependencies(holder, holder_stage), [(source, source_stage)]
        )

    def test_05_a_downstream_consumer_is_not_upstream(self):
        source, source_stage = make_stack("Source", "Source", params=None)
        holder = _cube("Holder")
        _read_before(holder, "ReadsSource", source)
        holder_stage = _add_stage(holder, "Holder")

        dependencies = stage_dependencies(source, source_stage)
        self.assertEqual(
            dependencies, [], "a stage that reads the selected stage was included"
        )
        self.assertNotIn(
            holder_stage.key,
            [stage.key for _obj, stage in dependencies],
            "the downstream consumer stage was included",
        )

    def test_06_an_unrelated_object_is_absent(self):
        source, source_stage = make_stack("Source", "Source", params=None)
        holder = _cube("Holder")
        _read_before(holder, "ReadsSource", source)
        holder_stage = _add_stage(holder, "Holder")
        _unrelated, unrelated_stage = make_stack("Unrelated", "Unrelated", params=None)

        dependencies = stage_dependencies(holder, holder_stage)
        self._assert_stages(dependencies, [(source, source_stage)])
        self.assertNotIn(
            unrelated_stage.key,
            [stage.key for _obj, stage in dependencies],
            "an unrelated object's stage was included",
        )

    def test_07_a_cycle_terminates_and_returns_each_stage_once(self):
        first = _cube("First")
        second = _cube("Second")
        _read_before(first, "ReadsSecond", second)
        first_stage = _add_stage(first, "First")
        _read_before(second, "ReadsFirst", first)
        second_stage = _add_stage(second, "Second")

        first_dependencies = stage_dependencies(first, first_stage)
        second_dependencies = stage_dependencies(second, second_stage)
        self._assert_stages(first_dependencies, [(second, second_stage)])
        self._assert_stages(second_dependencies, [(first, first_stage)])
        self.assertEqual(
            len(first_dependencies),
            len(set((owner.as_pointer(), stage.key) for owner, stage in first_dependencies)),
            "the cycle returned a stage twice",
        )

    def test_08_an_intermediate_object_without_a_stage_is_traversed(self):
        source, source_stage = make_stack("Source", "Source", params=None)
        middle = _cube("Middle")
        _read_before(middle, "ReadsSource", source)
        holder = _cube("Holder")
        _read_before(holder, "ReadsMiddle", middle)
        holder_stage = _add_stage(holder, "Holder")

        self._assert_stages(
            stage_dependencies(holder, holder_stage), [(source, source_stage)]
        )

    def test_09_nested_node_groups_are_walked(self):
        source, source_stage = make_stack("Source", "Source", params=None)
        inner = _reading_group("InnerReadsSource", source)
        outer = bpy.data.node_groups.new("OuterReadsSource", "GeometryNodeTree")
        outer.interface.new_socket(
            "Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry"
        )
        group_node = outer.nodes.new("GeometryNodeGroup")
        group_node.node_tree = inner
        group_out = outer.nodes.new("NodeGroupOutput")
        outer.links.new(group_node.outputs["Geometry"], group_out.inputs["Geometry"])

        holder = _cube("Holder")
        modifier = holder.modifiers.new("ReadsNested", "NODES")
        modifier.node_group = outer
        holder.modifiers.active = modifier
        holder_stage = _add_stage(holder, "Holder")

        self._assert_stages(
            stage_dependencies(holder, holder_stage), [(source, source_stage)]
        )

    def test_10_a_geometry_nodes_modifier_object_input_is_read(self):
        source, source_stage = make_stack("Source", "Source", params=None)
        group = bpy.data.node_groups.new("InputReadsSource", "GeometryNodeTree")
        group.interface.new_socket(
            "Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry"
        )
        group.interface.new_socket("Source", in_out="INPUT", socket_type="NodeSocketObject")
        group_in = group.nodes.new("NodeGroupInput")
        info = group.nodes.new("GeometryNodeObjectInfo")
        group.links.new(group_in.outputs["Source"], info.inputs["Object"])
        group_out = group.nodes.new("NodeGroupOutput")
        group.links.new(info.outputs["Geometry"], group_out.inputs["Geometry"])

        holder = _cube("Holder")
        modifier = holder.modifiers.new("ReadsInput", "NODES")
        modifier.node_group = group
        for key in modifier.properties.inputs.keys():
            modifier.properties.inputs[key]["value"] = source
        holder.modifiers.active = modifier
        holder_stage = _add_stage(holder, "Holder")

        self._assert_stages(
            stage_dependencies(holder, holder_stage), [(source, source_stage)]
        )

    def test_11_a_native_modifier_object_property_is_read(self):
        source, source_stage = make_stack("Source", "Source", params=None)
        holder = _cube("Holder")
        boolean = holder.modifiers.new("ReadsSource", "BOOLEAN")
        boolean.object = source
        holder.modifiers.active = boolean
        holder_stage = _add_stage(holder, "Holder")

        self._assert_stages(
            stage_dependencies(holder, holder_stage), [(source, source_stage)]
        )

    def test_12_a_generator_stage_has_no_upstream_stages(self):
        source, _source_stage = make_stack("Source", "Source", params=None)
        obj = _cube("Generator")
        _read_before(obj, "ReadsSource", source)
        _add_stage(obj, "Earlier")
        generator = _add_stage(obj, "Generator")
        generator.input_mode = self.stack_mod.INPUT_GENERATOR

        self.assertEqual(
            stage_dependencies(obj, generator),
            [],
            "a generator stage depends on a prefix it never reads",
        )

    def test_13_a_referenced_objects_post_stage_modifier_is_walked(self):
        source, source_stage = make_stack("Source", "Source", params=None)
        target, target_stage = make_stack("Target", "Target", params=None)
        boolean = source.modifiers.new("ReadsTarget", "BOOLEAN")
        boolean.object = target
        holder = _cube("Holder")
        _read_before(holder, "ReadsSource", source)
        holder_stage = _add_stage(holder, "Holder")

        self._assert_stages(
            stage_dependencies(holder, holder_stage),
            [(source, source_stage), (target, target_stage)],
        )

    def test_14_sl_named_user_data_is_not_plumbing(self):
        source, source_stage = make_stack("SL Source", "Source", params=None)
        collection = bpy.data.collections.new("SL User Sources")
        bpy.context.scene.collection.children.link(collection)
        collection.objects.link(source)
        holder = _cube("Holder")
        _read_before(holder, "ReadsCollection", collection)
        holder_stage = _add_stage(holder, "Holder")

        self._assert_stages(
            stage_dependencies(holder, holder_stage), [(source, source_stage)]
        )
