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

        self.assertEqual(definitions, [{"name": "ProtoColl"}])
        self.assertIn("idef.0.mesh.verts", by_name)
        self.assertEqual(by_name["idef.0.mesh.verts"].shape, (4, 3))
        self.assertEqual(list(by_name["idef.0.mesh.face_counts"]), [4])

        # The per-definition attribute carries the definition index.
        load = [a for a in attributes if a["name"] == "load"]
        self.assertEqual(len(load), 1)
        self.assertEqual(load[0]["definition"], 0)
        self.assertTrue(load[0]["section"].startswith("idef.0.attr."))
        self.assertEqual(list(by_name[load[0]["section"]]), [0.5, 1.0, 1.5, 2.0])

        # Two rows, one definition.
        self.assertEqual(list(by_name["inst.ref_def"]), [0, 0])
        transforms = by_name["inst.transforms"].reshape(-1, 16)
        self.assertEqual(transforms.shape, (2, 16))
        # Row-major, translation in the last row (protocol §5.2).
        self.assertEqual(list(transforms[0][12:15]), [0.0, 0.0, 0.0])
        self.assertEqual(list(transforms[1][12:15]), [3.0, 0.0, 0.0])
        np.testing.assert_allclose(transforms[0].reshape(4, 4), np.eye(4), atol=1e-6)

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
        self.assertEqual(list(nested[1][12:15]), [2.0, 0.0, 0.0])
        # Definition 1 is the quad itself.
        self.assertEqual(by_name["idef.1.mesh.verts"].shape, (4, 3))

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
        self.assertEqual(decoded_header["definitions"], [{"name": "FrameColl"}])
        self.assertIn("idef.0.mesh.verts", decoded_sections)
        self.assertEqual(list(decoded_sections["inst.ref_def"]), [0])


if __name__ == "__main__":
    unittest.main()
