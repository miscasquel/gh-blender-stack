"""Layer B, instances: Blender's instance component <-> protocol §5.2.

Run with:

    scripts/test-blender

Covers the Blender -> GH half: an evaluated GeometrySet whose instances
reference a collection (the Collection Info shape, hoisted into the definition)
or an object, with per-instance transforms; a nested case keeps the inner rows
as the definition's own ``idef.<i>.inst.*`` pair. The GH -> Blender apply half
is covered in the engine tests, and the live S9 pass covers the two together.
"""

import os
import sys
import unittest

import bpy
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
BLENDER_PKG = os.path.join(REPO, "blender")
if BLENDER_PKG not in sys.path:
    sys.path.insert(0, BLENDER_PKG)

from blender.gh_stack.blender import geometry_io  # noqa: E402
from blender.gh_stack.core import codec  # noqa: E402


def _quad(name):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(
        [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [], [(0, 1, 2, 3)]
    )
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def _collection(name, *objects):
    coll = bpy.data.collections.new(name)
    bpy.context.scene.collection.children.link(coll)
    for obj in objects:
        coll.objects.link(obj)
    return coll


def _points(name, positions, rotations=None, scales=None):
    cloud = bpy.data.pointclouds.new(name)
    cloud.resize(len(positions))
    cloud.points.foreach_set(
        "co", np.asarray(positions, dtype=np.float32).reshape(-1)
    )
    if rotations is not None:
        attr = cloud.attributes.new("rotation", "QUATERNION", "POINT")
        for i, quat in enumerate(rotations):
            attr.data[i].value = quat
    if scales is not None:
        attr = cloud.attributes.new("scale", "FLOAT_VECTOR", "POINT")
        for i, scale in enumerate(scales):
            attr.data[i].vector = scale
    cloud.points.update()
    obj = bpy.data.objects.new(name, cloud)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def _instance_holder(name, points_obj, source, source_kind="collection"):
    """An empty object whose GN modifier instances ``source`` at ``points_obj``'s
    points. ``source_kind`` selects the reference shape: a Collection (the
    Collection Info shape) or an Object (an Object Info shape)."""
    group = bpy.data.node_groups.new(name, "GeometryNodeTree")
    group.interface.new_socket(
        "Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry"
    )
    output = group.nodes.new("NodeGroupOutput")
    iono = group.nodes.new("GeometryNodeInstanceOnPoints")

    points_info = group.nodes.new("GeometryNodeObjectInfo")
    points_info.inputs["Object"].default_value = points_obj
    points_info.transform_space = "ORIGINAL"
    group.links.new(points_info.outputs["Geometry"], iono.inputs["Points"])

    if source_kind == "collection":
        source_node = group.nodes.new("GeometryNodeCollectionInfo")
        source_node.inputs["Collection"].default_value = source
        source_node.inputs["Separate Children"].default_value = False
        source_node.inputs["Reset Children"].default_value = False
        group.links.new(source_node.outputs["Instances"], iono.inputs["Instance"])
    else:
        source_node = group.nodes.new("GeometryNodeObjectInfo")
        source_node.inputs["Object"].default_value = source
        source_node.transform_space = "ORIGINAL"
        group.links.new(source_node.outputs["Geometry"], iono.inputs["Instance"])

    nattr_rot = group.nodes.new("GeometryNodeInputNamedAttribute")
    nattr_rot.data_type = "QUATERNION"
    nattr_rot.inputs["Name"].default_value = "rotation"
    nattr_scale = group.nodes.new("GeometryNodeInputNamedAttribute")
    nattr_scale.data_type = "FLOAT_VECTOR"
    nattr_scale.inputs["Name"].default_value = "scale"
    # Wire the fields only when the points carry them: a wired-but-missing
    # attribute evaluates to its zero value, which is a degenerate transform.
    if points_obj.data.attributes.get("rotation") is not None:
        group.links.new(nattr_rot.outputs["Attribute"], iono.inputs["Rotation"])
    if points_obj.data.attributes.get("scale") is not None:
        group.links.new(nattr_scale.outputs["Attribute"], iono.inputs["Scale"])

    group.links.new(iono.outputs["Instances"], output.inputs["Geometry"])
    holder = bpy.data.objects.new(name, None)
    bpy.context.scene.collection.objects.link(holder)
    holder.modifiers.new("GN", "NODES").node_group = group
    return holder


def _serialize(holder):
    bpy.context.view_layer.update()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    geometry = holder.evaluated_get(depsgraph).evaluated_geometry()
    return geometry_io.geometry_set_to_sections(geometry)


def _by_name(sections):
    return {name: np.asarray(values) for name, _dtype, values in sections}


class InstanceReadTests(unittest.TestCase):
    def setUp(self):
        bpy.ops.wm.read_factory_settings(use_empty=True)

    def test_collection_instance_becomes_a_definition(self):
        quad = _quad("read-quad")
        attr = quad.data.attributes.new("load", "FLOAT", "POINT")
        attr.data.foreach_set("value", np.array([0.5, 1.0, 1.5, 2.0], dtype=np.float32))
        coll = _collection("ProtoColl", quad)
        points = _points("Pts", [(0, 0, 0), (3, 0, 0)])
        holder = _instance_holder("Holder", points, coll, "collection")

        sections, attributes, definitions = _serialize(holder)
        by_name = _by_name(sections)

        # The collection is one definition; each member is its own definition
        # placed by a row (a member's own transform applies).
        self.assertEqual([entry["name"] for entry in definitions], ["ProtoColl", "read-quad"])
        self.assertIn("idef.1.mesh.verts", by_name)
        self.assertEqual(by_name["idef.1.mesh.verts"].shape, (4, 3))
        self.assertEqual(by_name["idef.1.mesh.face_counts"].shape, (1,))
        self.assertEqual(list(by_name["idef.0.inst.ref_def"]), [1])
        np.testing.assert_allclose(
            by_name["idef.0.inst.transforms"].reshape(4, 4), np.eye(4), atol=1e-6
        )

        # The per-member attribute carries the member's definition index.
        load = [a for a in attributes if a["name"] == "load"]
        self.assertEqual(len(load), 1)
        self.assertEqual(load[0]["definition"], 1)
        self.assertTrue(load[0]["section"].startswith("idef.1.attr."))
        self.assertEqual(list(by_name[load[0]["section"]]), [0.5, 1.0, 1.5, 2.0])

        # Two top rows, one definition.
        self.assertEqual(list(by_name["inst.ref_def"]), [0, 0])
        transforms = by_name["inst.transforms"].reshape(-1, 16)
        self.assertEqual(transforms.shape, (2, 16))
        # Row-major, translation in the fourth column (protocol §5.2).
        self.assertEqual([transforms[0][3], transforms[0][7], transforms[0][11]], [0.0, 0.0, 0.0])
        self.assertEqual([transforms[1][3], transforms[1][7], transforms[1][11]], [3.0, 0.0, 0.0])
        np.testing.assert_allclose(transforms[0].reshape(4, 4), np.eye(4), atol=1e-6)

    def test_a_collection_member_transform_rides_its_row(self):
        # Blender applies a member's own transform when instancing a collection;
        # the wire carries it as the member's placement row.
        member = _quad("member-moved")
        member.location.x = 5.0
        coll = _collection("MovedColl", member)
        points = _points("MovedPts", [(0, 0, 0)])
        holder = _instance_holder("MovedHolder", points, coll, "collection")

        sections, _attributes, definitions = _serialize(holder)
        by_name = _by_name(sections)
        self.assertEqual([entry["name"] for entry in definitions], ["MovedColl", "member-moved"])
        row = by_name["idef.0.inst.transforms"].reshape(4, 4)
        self.assertEqual([row[0][3], row[1][3], row[2][3]], [5.0, 0.0, 0.0])

    def test_two_collection_members_keep_separate_definitions(self):
        # Two members used to collapse into one definition with colliding
        # section names; all but one member was lost, and Python and the GH
        # codec disagreed on which survived.
        first = _quad("two-a")
        second = _quad("two-b")
        second.location.x = 10.0
        coll = _collection("TwoColl", first, second)
        points = _points("TwoPts", [(0, 0, 0)])
        holder = _instance_holder("TwoHolder", points, coll, "collection")

        sections, _attributes, definitions = _serialize(holder)
        by_name = _by_name(sections)
        self.assertEqual([entry["name"] for entry in definitions], ["TwoColl", "two-a", "two-b"])
        self.assertEqual(by_name["idef.1.mesh.verts"].shape, (4, 3))
        self.assertEqual(by_name["idef.2.mesh.verts"].shape, (4, 3))
        self.assertEqual(list(by_name["idef.0.inst.ref_def"]), [1, 2])
        rows = by_name["idef.0.inst.transforms"].reshape(-1, 16)
        self.assertEqual([rows[1][3], rows[1][7], rows[1][11]], [10.0, 0.0, 0.0])

    def test_a_member_with_its_own_instances_carries_them(self):
        # The live shape: a cube whose own modifier instances a sphere, kept as
        # surface plus instances, instanced by a collection. The member's
        # definition must carry the sphere and its rows, not just the surface.
        sphere = _quad("nested-sphere")
        points = _points("nested-points", [(0, 0, 0), (2, 0, 0)])
        cube = _quad("nested-cube")
        group = bpy.data.node_groups.new("nested-cube-group", "GeometryNodeTree")
        group.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
        group.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
        source = group.nodes.new("NodeGroupInput")
        output = group.nodes.new("NodeGroupOutput")
        iono = group.nodes.new("GeometryNodeInstanceOnPoints")
        join = group.nodes.new("GeometryNodeJoinGeometry")
        instance_info = group.nodes.new("GeometryNodeObjectInfo")
        instance_info.inputs["Object"].default_value = sphere
        instance_info.transform_space = "ORIGINAL"
        points_info = group.nodes.new("GeometryNodeObjectInfo")
        points_info.inputs["Object"].default_value = points
        points_info.transform_space = "ORIGINAL"
        group.links.new(instance_info.outputs["Geometry"], iono.inputs["Instance"])
        group.links.new(points_info.outputs["Geometry"], iono.inputs["Points"])
        group.links.new(source.outputs["Geometry"], join.inputs["Geometry"])
        group.links.new(iono.outputs["Instances"], join.inputs["Geometry"])
        group.links.new(join.outputs["Geometry"], output.inputs["Geometry"])
        modifier = cube.modifiers.new("GN", "NODES")
        modifier.node_group = group
        coll = _collection("NestedColl", cube)
        holder_points = _points("NestedHolderPts", [(0, 0, 0)])
        holder = _instance_holder("NestedHolder", holder_points, coll, "collection")

        sections, _attributes, definitions = _serialize(holder)
        by_name = _by_name(sections)

        self.assertEqual(
            [entry["name"] for entry in definitions],
            ["NestedColl", "nested-cube", "nested-sphere"],
        )
        self.assertIn("idef.1.mesh.verts", by_name)              # the cube's surface
        self.assertIn("idef.2.mesh.verts", by_name)              # the sphere
        self.assertEqual(list(by_name["idef.1.inst.ref_def"]), [2, 2])
        nested = by_name["idef.1.inst.transforms"].reshape(-1, 16)
        self.assertEqual([nested[1][3], nested[1][7], nested[1][11]], [2.0, 0.0, 0.0])

    def test_rotation_and_scale_ride_the_row_transform(self):
        import mathutils

        quad = _quad("rot-quad")
        points = _points(
            "RotPts",
            [(0, 0, 0)],
            rotations=[mathutils.Quaternion((0.0, 0.0, 0.0, 1.0))],  # 180° about Z
            scales=[(2.0, 2.0, 2.0)],
        )
        holder = _instance_holder("RotHolder", points, quad, "object")

        sections, _attributes, _definitions = _serialize(holder)
        transforms = _by_name(sections)["inst.transforms"].reshape(-1, 16)
        np.testing.assert_allclose(
            transforms[0].reshape(4, 4),
            np.array([
                [-2.0, 0.0, 0.0, 0.0],
                [0.0, -2.0, 0.0, 0.0],
                [0.0, 0.0, 2.0, 0.0],
                [0.0, 0.0, 0.0, 1.0],
            ]),
            atol=1e-6,
        )

    def test_nested_instances_keep_a_row_pair_on_the_definition(self):
        quad = _quad("nest-quad")
        inner_points = _points("InnerPts", [(0, 0, 0), (2, 0, 0)])
        inner = _instance_holder("Inner", inner_points, quad, "object")
        outer_points = _points("OuterPts", [(0, 0, 5)])
        outer = _instance_holder("Outer", outer_points, inner, "object")

        sections, _attributes, definitions = _serialize(outer)
        by_name = _by_name(sections)

        self.assertEqual(len(definitions), 2)
        self.assertEqual(list(by_name["inst.ref_def"]), [0])
        # The outer definition is pure nesting: its rows carry the inner rows.
        self.assertEqual(list(by_name["idef.0.inst.ref_def"]), [1, 1])
        nested = by_name["idef.0.inst.transforms"].reshape(-1, 16)
        self.assertEqual([nested[1][3], nested[1][7], nested[1][11]], [2.0, 0.0, 0.0])
        # Definition 1 is the quad itself.
        self.assertEqual(by_name["idef.1.mesh.verts"].shape, (4, 3))

    def test_a_collection_instance_inside_a_collection_is_a_nested_row(self):
        # The shape Blender's own collection instancing makes: an empty with
        # instance_collection inside the instanced collection. It must cross as
        # a nested row (the placement), not be dropped as "not readable".
        inner = _collection("nest-inner", _quad("nest-inner-cube"))
        outer = bpy.data.collections.new("nest-outer")
        bpy.context.scene.collection.children.link(outer)
        empty = bpy.data.objects.new("nest-empty", None)
        outer.objects.link(empty)
        empty.instance_type = "COLLECTION"
        empty.instance_collection = inner
        points = _points("nest-points", [(0, 0, 0)])
        holder = _instance_holder("nest-holder", points, outer, "collection")

        sections, _attributes, definitions = _serialize(holder)
        by_name = _by_name(sections)

        # outer -> empty (row) -> inner cube (row): the empty is its own level,
        # as in the scene.
        self.assertEqual(
            [entry["name"] for entry in definitions],
            ["nest-outer", "nest-empty", "nest-inner-cube"],
        )
        self.assertIn("idef.2.mesh.verts", by_name)
        self.assertEqual(list(by_name["idef.0.inst.ref_def"]), [1])
        self.assertEqual(list(by_name["idef.1.inst.ref_def"]), [2])
        self.assertEqual(list(by_name["inst.ref_def"]), [0])

    def test_child_collection_members_become_nested_rows(self):
        # A child collection inside the instanced one: Blender instances both
        # as one thing; each member crosses as a placement row with its own
        # definition, so nothing is dropped and transforms apply.
        child = bpy.data.collections.new("nest-child-coll")
        bpy.context.scene.collection.children.link(child)
        cube = _quad("nest-child-cube")
        cube.location.x = 4.0
        child.objects.link(cube)
        parent = bpy.data.collections.new("nest-parent-coll")
        bpy.context.scene.collection.children.link(parent)
        parent.children.link(child)
        points = _points("nest-b-points", [(0, 0, 0)])
        holder = _instance_holder("nest-b-holder", points, parent, "collection")

        sections, _attributes, definitions = _serialize(holder)
        by_name = _by_name(sections)

        self.assertEqual(
            [entry["name"] for entry in definitions],
            ["nest-parent-coll", "nest-child-cube"],
        )
        self.assertIn("idef.1.mesh.verts", by_name)
        self.assertEqual(list(by_name["idef.0.inst.ref_def"]), [1])
        row = by_name["idef.0.inst.transforms"].reshape(4, 4)
        self.assertEqual([row[0][3], row[1][3], row[2][3]], [4.0, 0.0, 0.0])

    def test_separate_children_does_not_apply_the_empty_twice(self):
        # With Separate Children on, Blender bakes the child's world transform
        # into the row that references it; the empty's own matrix must not be
        # applied again on top.
        inner = _collection("sep-inner", _quad("sep-cube"))
        outer = bpy.data.collections.new("sep-outer")
        bpy.context.scene.collection.children.link(outer)
        empty = bpy.data.objects.new("sep-empty", None)
        outer.objects.link(empty)
        empty.instance_type = "COLLECTION"
        empty.instance_collection = inner
        empty.location.x = 2.0
        points = _points("sep-points", [(0, 0, 0)])

        group = bpy.data.node_groups.new("sep-group", "GeometryNodeTree")
        group.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
        output = group.nodes.new("NodeGroupOutput")
        iono = group.nodes.new("GeometryNodeInstanceOnPoints")
        cinfo = group.nodes.new("GeometryNodeCollectionInfo")
        cinfo.inputs["Collection"].default_value = outer
        cinfo.inputs["Separate Children"].default_value = True
        cinfo.inputs["Reset Children"].default_value = False
        pinfo = group.nodes.new("GeometryNodeObjectInfo")
        pinfo.inputs["Object"].default_value = points
        pinfo.transform_space = "ORIGINAL"
        group.links.new(cinfo.outputs["Instances"], iono.inputs["Instance"])
        group.links.new(pinfo.outputs["Geometry"], iono.inputs["Points"])
        group.links.new(iono.outputs["Instances"], output.inputs["Geometry"])
        holder = bpy.data.objects.new("sep-holder", None)
        bpy.context.scene.collection.objects.link(holder)
        holder.modifiers.new("GN", "NODES").node_group = group

        sections, _attributes, definitions = _serialize(holder)
        by_name = _by_name(sections)
        self.assertEqual(
            [entry["name"] for entry in definitions],
            ["sep-outer", "sep-empty", "sep-cube"],
        )
        # The empty's 2 appears once: in the row that places it inside the
        # collection's definition; the cube sits at the origin inside the
        # empty's definition (no second 2).
        top_row = by_name["inst.transforms"].reshape(4, 4)
        self.assertEqual([top_row[0][3], top_row[1][3], top_row[2][3]], [0.0, 0.0, 0.0])
        empty_row = by_name["idef.0.inst.transforms"].reshape(4, 4)
        self.assertEqual([empty_row[0][3], empty_row[1][3], empty_row[2][3]], [2.0, 0.0, 0.0])
        cube_row = by_name["idef.1.inst.transforms"].reshape(4, 4)
        self.assertEqual([cube_row[0][3], cube_row[1][3], cube_row[2][3]], [0.0, 0.0, 0.0])

    def test_definition_names_take_part_in_the_request_hash(self):
        from blender.gh_stack.blender import engine

        quad = _quad("hash-quad")
        coll = _collection("HashColl", quad)
        points = _points("HashPts", [(0, 0, 0)])
        holder = _instance_holder("HashHolder", points, coll, "collection")

        sections, attributes, definitions = _serialize(holder)
        first = engine._sections_hash(sections, attributes, definitions)
        renamed = [dict(entry, name="Other") for entry in definitions]
        self.assertNotEqual(
            first, engine._sections_hash(sections, attributes, renamed)
        )

    def test_frames_encode_and_decode_with_instances(self):
        quad = _quad("frame-quad")
        coll = _collection("FrameColl", quad)
        points = _points("FramePts", [(1, 2, 3)])
        holder = _instance_holder("FrameHolder", points, coll, "collection")

        sections, attributes, definitions = _serialize(holder)
        header = {
            "type": "request",
            "stage": "inst-1a2b",
            "rid": "sha256:" + "0" * 64,
            "nonce": 1,
            "params": {},
            "geom": {"mode": "full", "hash": "sha256:" + "1" * 64, "subd": False},
            "units": "m",
            "space": "stack-local",
            "definitions": definitions,
            "attributes": attributes,
        }
        frame = codec.encode_frame(header, sections)
        decoded_header, decoded_sections = codec.decode_frame(frame)
        self.assertEqual(
            [entry["name"] for entry in decoded_header["definitions"]],
            ["FrameColl", "frame-quad"],
        )
        self.assertIn("idef.1.mesh.verts", decoded_sections)
        self.assertEqual(list(decoded_sections["inst.ref_def"]), [0])


class InstanceApplyTests(unittest.TestCase):
    """The GH -> Blender half: a response with definitions and rows lands as
    prototype collections + row clouds, and the stage's node group realizes the
    blocks through Instance on Points."""

    def setUp(self):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="gh_stack")

    def _stage(self):
        bpy.ops.mesh.primitive_cube_add(size=2.0)
        obj = bpy.context.object
        obj.name = "Blocks"
        bpy.ops.gh_stack.add_stage(label="Blocks")
        return obj, obj.gh_stack.stages[0]

    @staticmethod
    def _quad_sections(prefix=""):
        verts = np.array(
            [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], dtype=np.float32
        ).reshape(-1)
        return {
            prefix + "mesh.verts": verts,
            prefix + "mesh.face_counts": np.array([4], dtype=np.uint32),
            prefix + "mesh.face_indices": np.array([0, 1, 2, 3], dtype=np.uint32),
        }

    @staticmethod
    def _wire_transform(translation):
        m = np.eye(4, dtype=np.float32)
        m[0, 3], m[1, 3], m[2, 3] = translation
        return m.reshape(-1)

    def _realized_mesh(self, obj):
        # The stage's output is instances; a native modifier after the stage is
        # what realizes them for an object mesh (to_mesh alone shows the empty
        # object mesh, because instancing is not part of it). A Realize Instances
        # group stands in for what the renderer, or the next native modifier,
        # actually sees.
        group = bpy.data.node_groups.new("RealizeProbe", "GeometryNodeTree")
        group.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
        group.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
        source = group.nodes.new("NodeGroupInput")
        destination = group.nodes.new("NodeGroupOutput")
        realize = group.nodes.new("GeometryNodeRealizeInstances")
        group.links.new(source.outputs["Geometry"], realize.inputs["Geometry"])
        group.links.new(realize.outputs["Geometry"], destination.inputs["Geometry"])
        modifier = obj.modifiers.new("RealizeProbe", "NODES")
        modifier.node_group = group
        bpy.context.view_layer.update()
        depsgraph = bpy.context.evaluated_depsgraph_get()
        evaluated = obj.evaluated_get(depsgraph)
        return evaluated, evaluated.to_mesh()

    def test_apply_realizes_block_instances(self):
        from blender.gh_stack.blender import engine

        obj, stage = self._stage()
        sections = dict(self._quad_sections("idef.0."))
        sections["inst.ref_def"] = np.array([0, 0], dtype=np.uint32)
        sections["inst.transforms"] = np.concatenate([
            self._wire_transform((0, 0, 0)),
            self._wire_transform((5, 0, 0)),
        ])

        engine._apply(obj, stage, sections, None)

        evaluated, mesh = self._realized_mesh(obj)
        try:
            self.assertEqual(len(mesh.vertices), 8)
            self.assertEqual(len(mesh.polygons), 2)
            xs = sorted(round(v.co.x, 3) for v in mesh.vertices)
            self.assertIn(5.0, xs)
        finally:
            evaluated.to_mesh_clear()

    def test_apply_rotates_and_scales_rows(self):
        from blender.gh_stack.blender import engine

        obj, stage = self._stage()
        sections = dict(self._quad_sections("idef.0."))
        sections["inst.ref_def"] = np.array([0], dtype=np.uint32)
        # 180° about Z with scale 2: x and y flip and double.
        m = np.zeros((4, 4), dtype=np.float32)
        m[0, 0] = -2.0
        m[1, 1] = -2.0
        m[2, 2] = 2.0
        m[3, 3] = 1.0
        sections["inst.transforms"] = m.reshape(-1)

        engine._apply(obj, stage, sections, None)

        evaluated, mesh = self._realized_mesh(obj)
        try:
            self.assertEqual(len(mesh.vertices), 4)
            xs = sorted(round(v.co.x, 3) for v in mesh.vertices)
            # The quad spans (0,0)..(1,1); -2× puts it at (-2,0)..(0,-2).
            self.assertEqual(xs, [-2.0, -2.0, 0.0, 0.0])
        finally:
            evaluated.to_mesh_clear()

    def test_apply_draws_nested_rows(self):
        # A nested placement reaches the parent's prototype chain through a
        # Transform node, so the parent's rows instance it with the parent.
        from blender.gh_stack.blender import engine, helpers, nodes

        obj, stage = self._stage()
        sections = dict(self._quad_sections("idef.0."))
        sections["idef.1.inst.ref_def"] = np.array([0], dtype=np.uint32)
        sections["idef.1.inst.transforms"] = self._wire_transform((0, 0, 3))
        sections["inst.ref_def"] = np.array([1], dtype=np.uint32)
        sections["inst.transforms"] = self._wire_transform((0, 0, 0))

        engine._apply(obj, stage, sections, None)

        collections = helpers.definition_collections(obj, stage.key)
        self.assertIn(0, collections)
        self.assertIn(1, collections)
        # Definition 1 is the top-level one (it has a row cloud); definition 0
        # is nested-only, so it has none.
        self.assertEqual(list(helpers.instance_clouds(obj, stage.key)), [1])
        for collection in collections.values():
            self.assertEqual([o for o in collection.objects if o.type == "EMPTY"], [])

        group = bpy.data.node_groups[nodes.group_name(stage.key)]
        self.assertTrue(any(n.bl_idname == "GeometryNodeTransform" for n in group.nodes))
        self.assertFalse(any(n.bl_idname == "GeometryNodeCollectionInfo" for n in group.nodes))

        evaluated, mesh = self._realized_mesh(obj)
        try:
            self.assertEqual(len(mesh.vertices), 4)
            zs = sorted(round(v.co.z, 3) for v in mesh.vertices)
            self.assertEqual(zs, [3.0, 3.0, 3.0, 3.0])
        finally:
            evaluated.to_mesh_clear()

    def test_apply_draws_two_levels_of_nesting(self):
        # def0: a quad; def1 nests it 3 up; def2 nests def1 1 up; one top row
        # of def2 — the quad lands at z = 4.
        from blender.gh_stack.blender import engine

        obj, stage = self._stage()
        sections = dict(self._quad_sections("idef.0."))
        sections["idef.1.inst.ref_def"] = np.array([0], dtype=np.uint32)
        sections["idef.1.inst.transforms"] = self._wire_transform((0, 0, 3))
        sections["idef.2.inst.ref_def"] = np.array([1], dtype=np.uint32)
        sections["idef.2.inst.transforms"] = self._wire_transform((0, 0, 1))
        sections["inst.ref_def"] = np.array([2], dtype=np.uint32)
        sections["inst.transforms"] = self._wire_transform((0, 0, 0))

        engine._apply(obj, stage, sections, None)

        evaluated, mesh = self._realized_mesh(obj)
        try:
            self.assertEqual(len(mesh.vertices), 4)
            zs = sorted(round(v.co.z, 3) for v in mesh.vertices)
            self.assertEqual(zs, [4.0, 4.0, 4.0, 4.0])
        finally:
            evaluated.to_mesh_clear()

    def test_the_group_rebuilds_when_the_nested_structure_changes(self):
        from blender.gh_stack.blender import engine, helpers, nodes

        obj, stage = self._stage()
        sections = dict(self._quad_sections("idef.0."))
        sections["idef.1.inst.ref_def"] = np.array([0], dtype=np.uint32)
        sections["idef.1.inst.transforms"] = self._wire_transform((0, 0, 3))
        sections["inst.ref_def"] = np.array([1], dtype=np.uint32)
        sections["inst.transforms"] = self._wire_transform((0, 0, 0))
        engine._apply(obj, stage, sections, None)

        group = bpy.data.node_groups[nodes.group_name(stage.key)]
        helpers_map = helpers.ensure_helpers(obj, stage.key)
        definitions = nodes._definitions(obj, stage.key)
        nested = engine._nested_rows_of(sections)
        signature = group["gh_stack_nested"]
        self.assertTrue(nodes._group_matches(group, helpers_map, definitions, signature, nested))
        self.assertFalse(nodes._group_matches(group, helpers_map, definitions, "changed", nested))

        # Move the nested placement: the structure signature changes, so the
        # next apply rebuilds the chain instead of keeping the old transform.
        sections["idef.1.inst.transforms"] = self._wire_transform((0, 0, 5))
        engine._apply(obj, stage, sections, None)
        self.assertNotEqual(group["gh_stack_nested"], signature)

        evaluated, mesh = self._realized_mesh(obj)
        try:
            zs = sorted(round(v.co.z, 3) for v in mesh.vertices)
            self.assertEqual(zs, [5.0, 5.0, 5.0, 5.0])
        finally:
            evaluated.to_mesh_clear()

    def test_a_group_without_the_format_stamp_is_rebuilt(self):
        # A group written by an older build has no format stamp (and no nested
        # chain): a same-hash frame must still be able to repair it, so the
        # stamp is part of the match.
        from blender.gh_stack.blender import engine, helpers, nodes

        obj, stage = self._stage()
        sections = dict(self._quad_sections("idef.0."))
        sections["idef.1.inst.ref_def"] = np.array([0], dtype=np.uint32)
        sections["idef.1.inst.transforms"] = self._wire_transform((0, 0, 3))
        sections["inst.ref_def"] = np.array([1], dtype=np.uint32)
        sections["inst.transforms"] = self._wire_transform((0, 0, 0))
        engine._apply(obj, stage, sections, None)

        group = bpy.data.node_groups[nodes.group_name(stage.key)]
        helpers_map = helpers.ensure_helpers(obj, stage.key)
        definitions = nodes._definitions(obj, stage.key)
        nested = engine._nested_rows_of(sections)

        group["gh_stack_format"] = 1  # what an older build left behind
        self.assertFalse(nodes._group_matches(group, helpers_map, definitions,
                                              group["gh_stack_nested"], nested))
        nodes.ensure_group(obj, stage.key, nested)
        self.assertEqual(group["gh_stack_format"], nodes.GROUP_FORMAT)
        self.assertTrue(nodes._group_matches(group, helpers_map, definitions,
                                             group["gh_stack_nested"], nested))

    def test_an_orphan_definition_is_not_expected_in_the_group(self):
        # A definition no row reaches gets no chain; expecting its objects would
        # make every apply see a mismatch and rebuild forever.
        from blender.gh_stack.blender import engine, helpers, nodes

        obj, stage = self._stage()
        sections = dict(self._quad_sections("idef.0."))
        sections["inst.ref_def"] = np.array([0], dtype=np.uint32)
        sections["inst.transforms"] = self._wire_transform((0, 0, 0))
        engine._apply(obj, stage, sections, None)

        group = bpy.data.node_groups[nodes.group_name(stage.key)]
        helpers_map = helpers.ensure_helpers(obj, stage.key)
        definitions = nodes._definitions(obj, stage.key)
        definitions[99] = definitions[0]  # a definition with no row anywhere
        self.assertTrue(nodes._group_matches(group, helpers_map, definitions,
                                             group["gh_stack_nested"], {}))

    def test_apply_combined_base_and_blocks(self):
        # The live shape: base mesh + definition geometry + a definition-marked
        # attribute + four rows, all in one response. The base mesh draws as
        # real geometry, the four quads as instances, and each attribute lands
        # on its own helper.
        from blender.gh_stack.blender import engine

        obj, stage = self._stage()
        sections = dict(self._quad_sections())
        sections.update(self._quad_sections("idef.0."))
        sections["inst.ref_def"] = np.array([0, 0, 0, 0], dtype=np.uint32)
        sections["inst.transforms"] = np.concatenate([
            self._wire_transform((0, 0, 0)),
            self._wire_transform((3, 0, 0)),
            self._wire_transform((3, 3, 0)),
            self._wire_transform((0, 3, 0)),
        ])
        attributes = [
            {
                "group": "mesh", "name": "base_load", "domain": "POINT", "type": "FLOAT",
                "components": 1, "count": 4, "section": "attr.0.values",
            },
            {
                "group": "mesh", "name": "def_load", "domain": "POINT", "type": "FLOAT",
                "components": 1, "count": 4, "definition": 0,
                "section": "idef.0.attr.1.values",
            },
        ]
        sections["attr.0.values"] = np.array([1.0, 2.0, 3.0, 4.0], dtype=np.float32)
        sections["idef.0.attr.1.values"] = np.array([5.0, 6.0, 7.0, 8.0], dtype=np.float32)

        engine._apply(obj, stage, sections, attributes)

        evaluated, mesh = self._realized_mesh(obj)
        try:
            # 4 base vertices + 4 quads of 4.
            self.assertEqual(len(mesh.vertices), 20)
            self.assertEqual(len(mesh.polygons), 5)
        finally:
            evaluated.to_mesh_clear()

        from blender.gh_stack.blender import helpers

        base = helpers.helper_for(obj, stage.key, "mesh")
        definition = helpers.definition_name(obj, stage.key, 0)
        definition_object = bpy.data.objects[definition]
        self.assertIsNotNone(base.data.attributes.get("base_load"))
        self.assertIsNone(base.data.attributes.get("def_load"))
        self.assertIsNotNone(definition_object.data.attributes.get("def_load"))
        self.assertIsNone(definition_object.data.attributes.get("base_load"))

    def test_the_group_is_reused_when_nothing_changed(self):
        # The rebuild check must agree with the wiring it just produced (it
        # used to count the base points helper as a row cloud and rebuild on
        # every apply — churn, caught by the 2026-10-16 audit).
        from blender.gh_stack.blender import engine, helpers, nodes

        obj, stage = self._stage()
        sections = dict(self._quad_sections("idef.0."))
        sections["inst.ref_def"] = np.array([0], dtype=np.uint32)
        sections["inst.transforms"] = self._wire_transform((0, 0, 0))
        engine._apply(obj, stage, sections, None)

        group = bpy.data.node_groups[nodes.group_name(stage.key)]
        helpers_map = helpers.ensure_helpers(obj, stage.key)
        definitions = nodes._definitions(obj, stage.key)
        self.assertTrue(nodes._group_matches(group, helpers_map, definitions))
        self.assertFalse(any(n.bl_idname == "GeometryNodeCollectionInfo" for n in group.nodes))

        # A definition leaving the *expected* set must make the current group
        # stale, so the next ensure_group rebuilds instead of drawing a ghost.
        self.assertFalse(nodes._group_matches(group, helpers_map, {}))

        # And a response without blocks must actually remove it.
        engine._apply(obj, stage, {}, None)
        self.assertEqual(helpers.definition_collections(obj, stage.key), {})
        self.assertEqual(helpers.instance_clouds(obj, stage.key), {})

    def test_apply_clears_definitions_that_go_away(self):
        from blender.gh_stack.blender import engine

        obj, stage = self._stage()
        sections = dict(self._quad_sections("idef.0."))
        sections["inst.ref_def"] = np.array([0], dtype=np.uint32)
        sections["inst.transforms"] = self._wire_transform((0, 0, 0))
        engine._apply(obj, stage, sections, None)

        # A later response without blocks removes the prototype and its cloud.
        from blender.gh_stack.blender import helpers

        engine._apply(obj, stage, {}, None)
        self.assertEqual(helpers.definition_collections(obj, stage.key), {})
        self.assertEqual(helpers.instance_clouds(obj, stage.key), {})

        evaluated, mesh = self._realized_mesh(obj)
        try:
            self.assertEqual(len(mesh.vertices), 0)
        finally:
            evaluated.to_mesh_clear()


class EnginePrefixTests(unittest.TestCase):
    """The product's prefix path end to end: a real stack object, a real
    Instance-on-Points modifier, a real stage added through the operator.
    Locks the setup the live loop depends on and the two semantics that are
    easy to trip over."""

    def setUp(self):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="gh_stack")

    def _instance_group(self, name, points_obj, source_obj):
        group = bpy.data.node_groups.new(name, "GeometryNodeTree")
        group.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
        output = group.nodes.new("NodeGroupOutput")
        iono = group.nodes.new("GeometryNodeInstanceOnPoints")
        source = group.nodes.new("GeometryNodeObjectInfo")
        source.inputs["Object"].default_value = source_obj
        source.transform_space = "ORIGINAL"
        points = group.nodes.new("GeometryNodeObjectInfo")
        points.inputs["Object"].default_value = points_obj
        points.transform_space = "ORIGINAL"
        group.links.new(source.outputs["Geometry"], iono.inputs["Instance"])
        group.links.new(points.outputs["Geometry"], iono.inputs["Points"])
        group.links.new(iono.outputs["Instances"], output.inputs["Geometry"])
        return group

    def _scene(self):
        from blender.gh_stack.blender import stack as stack_mod

        source = _quad("prefix-source")
        points = _points("prefix-points", [(0, 0, 0), (3, 0, 0)])
        holder = _quad("prefix-holder")
        group = self._instance_group("prefix-IoP", points, source)
        modifier = holder.modifiers.new("GN", "NODES")
        modifier.node_group = group
        bpy.context.view_layer.objects.active = holder
        holder.modifiers.active = modifier
        bpy.ops.gh_stack.add_stage(label="Prefix")
        stage = holder.gh_stack.stages[-1]
        return holder, stage, group, modifier, stack_mod

    @staticmethod
    def _prefix(holder, stage, stack_mod):
        from blender.gh_stack.blender import engine

        return engine._prefix_sections(holder, stack_mod.stack_of(holder), stage)

    def test_stage_after_the_instance_modifier_ships_definitions(self):
        holder, stage, _group, _modifier, stack_mod = self._scene()
        sections, _hash, _attributes, definitions = self._prefix(holder, stage, stack_mod)
        names = [name for name, _dtype, _values in sections]
        self.assertEqual(definitions, [{"name": "prefix-source"}])
        self.assertIn("idef.0.mesh.verts", names)
        self.assertIn("inst.ref_def", names)
        self.assertEqual(list(dict((n, v) for n, _d, v in sections)["inst.ref_def"]), [0, 0])

    def test_stage_before_the_instance_modifier_ships_none(self):
        holder, stage, _group, _modifier, stack_mod = self._scene()
        order = [m.name for m in holder.modifiers]
        holder.modifiers.move(order.index("GH · Prefix"), 0)
        sections, _hash, _attributes, definitions = self._prefix(holder, stage, stack_mod)
        self.assertEqual(definitions, [])
        self.assertFalse(any(name == "inst.ref_def" for name, _d, _v in sections))

    def test_graph_edit_after_the_first_read_is_seen(self):
        # The memo follows edits inside the node group: a graph that stops
        # creating instances stops shipping definitions, and one that starts
        # again ships them (the epoch rule, exercised from the product side).
        holder, stage, group, modifier, stack_mod = self._scene()
        _sections, _hash, _attributes, definitions = self._prefix(holder, stage, stack_mod)
        self.assertEqual(len(definitions), 1)

        group.nodes.clear()
        group.interface.clear()
        _sections, _hash, _attributes, definitions = self._prefix(holder, stage, stack_mod)
        self.assertEqual(definitions, [])

        points = bpy.data.objects["prefix-points"]
        source = bpy.data.objects["prefix-source"]
        modifier.node_group = self._instance_group("prefix-IoP2", points, source)
        _sections, _hash, _attributes, definitions = self._prefix(holder, stage, stack_mod)
        self.assertEqual(len(definitions), 1)


class EngineUpdateTests(unittest.TestCase):
    """A geometry edit the prefix reads must ship a new full frame with the new
    definition. The depsgraph handler cannot fire under ``-b``, so these tests
    bump the epoch keys it would name (its exact behavior in a session) and
    capture what the engine would send.

    Regression for the live report "updating the instance geometry does not
    update the blocks": instancing a *collection* left its members out of the
    reference list, so editing one never moved the revision."""

    def setUp(self):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="gh_stack")
        from blender.gh_stack.blender import bridge, server

        self.server = server
        self.bridge = bridge
        self._original_send = server.SERVER.send
        self._original_connected = bridge.state.get("connected", False)
        self.frames = []
        server.SERVER.send = lambda frame: self.frames.append(frame) or True
        bridge.state["connected"] = True

    def tearDown(self):
        self.server.SERVER.send = self._original_send
        self.bridge.state["connected"] = self._original_connected

    def _capture(self, obj, record, stage):
        from blender.gh_stack.blender import engine
        from blender.gh_stack.core import codec

        engine._process_stage(obj, record, stage)
        header, sections = codec.decode_frame(self.frames[-1])
        return header, sections

    @staticmethod
    def _edit(owner, object_name):
        from blender.gh_stack.blender import engine

        owner.data.vertices[0].co.x += 0.5
        owner.data.update()
        for key in (("Mesh", owner.data.name), ("Object", object_name)):
            engine._epochs[key] = engine._epochs.get(key, 0) + 1

    def _collection_scene(self):
        from blender.gh_stack.blender import stack as stack_mod

        member = _quad("update-member")
        collection = bpy.data.collections.new("update-coll")
        bpy.context.scene.collection.children.link(collection)
        collection.objects.link(member)
        points = _points("update-points", [(0, 0, 0), (1, 0, 0)])
        holder = _quad("update-holder")

        group = bpy.data.node_groups.new("update-IoP", "GeometryNodeTree")
        group.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
        output = group.nodes.new("NodeGroupOutput")
        iono = group.nodes.new("GeometryNodeInstanceOnPoints")
        cinfo = group.nodes.new("GeometryNodeCollectionInfo")
        cinfo.inputs["Collection"].default_value = collection
        cinfo.inputs["Separate Children"].default_value = False
        cinfo.inputs["Reset Children"].default_value = False
        pinfo = group.nodes.new("GeometryNodeObjectInfo")
        pinfo.inputs["Object"].default_value = points
        pinfo.transform_space = "ORIGINAL"
        group.links.new(cinfo.outputs["Instances"], iono.inputs["Instance"])
        group.links.new(pinfo.outputs["Geometry"], iono.inputs["Points"])
        group.links.new(iono.outputs["Instances"], output.inputs["Geometry"])

        modifier = holder.modifiers.new("GN", "NODES")
        modifier.node_group = group
        bpy.context.view_layer.objects.active = holder
        holder.modifiers.active = modifier
        bpy.ops.gh_stack.add_stage(label="Update")
        stage = holder.gh_stack.stages[-1]
        return holder, member, stage, stack_mod.stack_of(holder)

    def test_a_collection_members_edit_ships_a_new_definition(self):
        from blender.gh_stack.blender import engine

        holder, member, stage, record = self._collection_scene()
        header, sections = self._capture(holder, record, stage)
        self.assertEqual((header.get("geom") or {}).get("mode"), "full")
        self.assertEqual(
            [entry["name"] for entry in header.get("definitions")],
            ["update-coll", "update-member"],
        )
        before = np.asarray(sections["idef.1.mesh.verts"], dtype=np.float32)

        self._edit(member, "update-member")
        engine._pending_of(record.stack_id, stage.key).clear()
        header, sections = self._capture(holder, record, stage)

        self.assertEqual((header.get("geom") or {}).get("mode"), "full")
        after = np.asarray(sections["idef.1.mesh.verts"], dtype=np.float32)
        self.assertFalse(np.array_equal(before, after))

    def test_a_lost_answer_is_retried_without_an_edit(self):
        # The timeout lives in the per-stage pass, which runs only for dirty
        # objects; a lost answer (a disconnect) dirties nothing, so the timer
        # nudges a stage that has waited past the timeout.
        import time

        from blender.gh_stack.blender import engine

        holder, member, stage, record = self._collection_scene()
        self._capture(holder, record, stage)
        self.assertIn(holder.name, engine._waiting_objects)

        pending = engine._pending_of(record.stack_id, stage.key)
        self.assertTrue(pending)
        for rid in list(pending):
            pending[rid] = time.monotonic() - 1000.0
        engine._dirty.clear()
        engine._last_stall_check = 0.0
        engine._nudge_stalled_waits()
        self.assertIn(holder.name, engine._dirty)

    def test_an_object_sources_edit_ships_a_new_definition(self):
        from blender.gh_stack.blender import engine

        source = _quad("update-source")
        points = _points("update-opoints", [(0, 0, 0), (1, 0, 0)])
        holder = _quad("update-oholder")
        group = bpy.data.node_groups.new("update-oIoP", "GeometryNodeTree")
        group.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
        output = group.nodes.new("NodeGroupOutput")
        iono = group.nodes.new("GeometryNodeInstanceOnPoints")
        src = group.nodes.new("GeometryNodeObjectInfo")
        src.inputs["Object"].default_value = source
        src.transform_space = "ORIGINAL"
        pinfo = group.nodes.new("GeometryNodeObjectInfo")
        pinfo.inputs["Object"].default_value = points
        pinfo.transform_space = "ORIGINAL"
        group.links.new(src.outputs["Geometry"], iono.inputs["Instance"])
        group.links.new(pinfo.outputs["Geometry"], iono.inputs["Points"])
        group.links.new(iono.outputs["Instances"], output.inputs["Geometry"])
        modifier = holder.modifiers.new("GN", "NODES")
        modifier.node_group = group
        bpy.context.view_layer.objects.active = holder
        holder.modifiers.active = modifier
        bpy.ops.gh_stack.add_stage(label="UpdateO")
        stage = holder.gh_stack.stages[-1]
        from blender.gh_stack.blender import stack as stack_mod

        record = stack_mod.stack_of(holder)

        header, sections = self._capture(holder, record, stage)
        self.assertEqual(header.get("definitions"), [{"name": "update-source"}])
        before = np.asarray(sections["idef.0.mesh.verts"], dtype=np.float32)

        self._edit(source, "update-source")
        engine._pending_of(record.stack_id, stage.key).clear()
        header, sections = self._capture(holder, record, stage)

        self.assertEqual((header.get("geom") or {}).get("mode"), "full")
        after = np.asarray(sections["idef.0.mesh.verts"], dtype=np.float32)
        self.assertFalse(np.array_equal(before, after))


if __name__ == "__main__":
    unittest.main()
