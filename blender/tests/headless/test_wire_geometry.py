"""Wire meshes and curve components: curves, points, and their attributes.

A mesh with no faces has no mesh sections to carry. Its loose edges travel as
one two-point polyline each (so an EDGE attribute stays one value per curve),
its isolated vertices travel as a point cloud, and a Curves component uses the
same per-curve/per-point attribute mapping. These tests pin that contract.
"""

import os
import sys
import unittest

import bpy
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from blender.stageloom.blender import geometry_io  # noqa: E402


def by_name(sections):
    return {name: np.asarray(values) for name, _dtype, values in sections}


def new_wire_object():
    """A 3-edge chain, plus one isolated vertex."""
    mesh = bpy.data.meshes.new("wire")
    mesh.from_pydata(
        [(0, 0, 0), (1, 0, 0), (2, 0, 0), (3, 0, 0), (9, 9, 9)],
        [(0, 1), (1, 2), (2, 3)],
        [],
    )
    mesh.update()
    obj = bpy.data.objects.new("wire", mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def add_attributes(mesh):
    top = mesh.attributes.new("Top", "BOOLEAN", "EDGE")
    for i, value in enumerate([True, True, False]):
        top.data[i].value = value
    diag = mesh.attributes.new("Diag", "BOOLEAN", "EDGE")
    for i, value in enumerate([False, False, True]):
        diag.data[i].value = value
    endpoints = mesh.attributes.new("endpoints", "BOOLEAN", "POINT")
    for i in range(len(mesh.vertices)):
        endpoints.data[i].value = i in (0, 4)


def descriptor(attributes, group, name):
    for entry in attributes:
        if entry["group"] == group and entry["name"] == name:
            return entry
    return None


class WireMeshTests(unittest.TestCase):
    def setUp(self):
        bpy.ops.wm.read_factory_settings(use_empty=True)

    def test_wire_mesh_exports_edges_as_curves_and_points(self):
        obj = new_wire_object()
        add_attributes(obj.data)

        sections, attributes = geometry_io.object_to_sections(obj)
        by = by_name(sections)

        self.assertFalse(any(name.startswith("mesh.") for name in by), list(by))
        np.testing.assert_array_equal(by["crv.counts"], [2, 2, 2])
        np.testing.assert_allclose(
            by["crv.points"][:, :3],
            [[0, 0, 0], [1, 0, 0], [1, 0, 0], [2, 0, 0], [2, 0, 0], [3, 0, 0]],
            rtol=0,
            atol=1e-6,
        )
        np.testing.assert_allclose(by["pts.positions"], [[9, 9, 9]], rtol=0, atol=1e-6)

        top = descriptor(attributes, "crv", "Top")
        self.assertIsNotNone(top)
        self.assertEqual((top["domain"], top["count"]), ("CURVE", 3))
        np.testing.assert_array_equal(by[top["section"]], [1, 1, 0])

        diag = descriptor(attributes, "crv", "Diag")
        self.assertIsNotNone(diag)
        self.assertEqual((diag["domain"], diag["count"]), ("CURVE", 3))
        np.testing.assert_array_equal(by[diag["section"]], [0, 0, 1])

        curve_points = descriptor(attributes, "crv", "endpoints")
        self.assertIsNotNone(curve_points)
        self.assertEqual((curve_points["domain"], curve_points["count"]), ("POINT", 6))
        np.testing.assert_array_equal(by[curve_points["section"]], [1, 0, 0, 0, 0, 0])

        point_points = descriptor(attributes, "pts", "endpoints")
        self.assertIsNotNone(point_points)
        self.assertEqual((point_points["domain"], point_points["count"]), ("POINT", 1))
        np.testing.assert_array_equal(by[point_points["section"]], [1])

    def test_the_evaluated_stage_prefix_uses_the_same_wire_path(self):
        obj = new_wire_object()
        add_attributes(obj.data)

        depsgraph = bpy.context.evaluated_depsgraph_get()
        geometry = obj.evaluated_get(depsgraph).evaluated_geometry()
        sections, attributes, _definitions = geometry_io.geometry_set_to_sections(geometry)
        by = by_name(sections)

        self.assertFalse(any(name.startswith("mesh.") for name in by), list(by))
        np.testing.assert_array_equal(by["crv.counts"], [2, 2, 2])
        self.assertEqual(len(by["pts.positions"]), 1)
        self.assertIsNotNone(descriptor(attributes, "crv", "Top"))
        self.assertIsNotNone(descriptor(attributes, "pts", "endpoints"))

    def test_mixed_mesh_keeps_faces_and_adds_the_loose_remainder(self):
        mesh = bpy.data.meshes.new("mixed")
        mesh.from_pydata(
            [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (5, 0, 0), (6, 0, 0), (9, 9, 9)],
            [(0, 1), (1, 2), (2, 3), (3, 0), (4, 5)],
            [(0, 1, 2, 3)],
        )
        mesh.update()
        obj = bpy.data.objects.new("mixed", mesh)
        bpy.context.scene.collection.objects.link(obj)

        sections, _attributes = geometry_io.object_to_sections(obj)
        by = by_name(sections)
        self.assertIn("mesh.verts", by)
        self.assertIn("crv.points", by)
        self.assertIn("pts.positions", by)
        np.testing.assert_array_equal(by["crv.counts"], [2])
        np.testing.assert_allclose(by["pts.positions"], [[9, 9, 9]], rtol=0, atol=1e-6)


def curve_component(obj):
    """The Curves component plus the GeometrySet that owns it.

    The component's RNA dies with the set, so the caller must keep the set
    referenced while it reads sections.
    """
    depsgraph = bpy.context.evaluated_depsgraph_get()
    geometry = obj.evaluated_get(depsgraph).evaluated_geometry()
    return geometry, geometry.curves


class CurveComponentTests(unittest.TestCase):
    def setUp(self):
        bpy.ops.wm.read_factory_settings(use_empty=True)

    def test_curve_component_uses_curve_and_curve_point_domains(self):
        obj = new_wire_object()
        group = bpy.data.node_groups.new("curve-test", "GeometryNodeTree")
        group.interface.new_socket(
            name="Geometry", in_out="INPUT", socket_type="NodeSocketGeometry"
        )
        group.interface.new_socket(
            name="Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry"
        )
        source = group.nodes.new("NodeGroupInput")
        sink = group.nodes.new("NodeGroupOutput")
        to_curve = group.nodes.new("GeometryNodeMeshToCurve")
        index = group.nodes.new("GeometryNodeInputIndex")
        group.links.new(source.outputs[0], to_curve.inputs[0])

        curve_attr = group.nodes.new("GeometryNodeStoreNamedAttribute")
        curve_attr.data_type = "INT"
        curve_attr.domain = "CURVE"
        curve_attr.inputs["Name"].default_value = "cidx"
        group.links.new(to_curve.outputs[0], curve_attr.inputs["Geometry"])
        group.links.new(index.outputs[0], curve_attr.inputs["Value"])

        point_attr = group.nodes.new("GeometryNodeStoreNamedAttribute")
        point_attr.data_type = "INT"
        point_attr.domain = "POINT"
        point_attr.inputs["Name"].default_value = "pidx"
        group.links.new(curve_attr.outputs[0], point_attr.inputs["Geometry"])
        group.links.new(index.outputs[0], point_attr.inputs["Value"])
        group.links.new(point_attr.outputs[0], sink.inputs[0])

        modifier = obj.modifiers.new("to-curves", "NODES")
        modifier.node_group = group

        geometry, curves = curve_component(obj)
        self.assertIsNotNone(curves)
        sections, attributes = geometry_io.curves_component_to_sections(curves)
        by = by_name(sections)

        self.assertEqual(len(by["crv.counts"]), len(curves.curves))
        self.assertEqual(int(by["crv.counts"].sum()), len(curves.points))

        curve_values = descriptor(attributes, "crv", "cidx")
        self.assertIsNotNone(curve_values)
        self.assertEqual((curve_values["domain"], curve_values["count"]),
                         ("CURVE", len(curves.curves)))
        np.testing.assert_array_equal(
            by[curve_values["section"]], list(range(len(curves.curves)))
        )

        point_values = descriptor(attributes, "crv", "pidx")
        self.assertIsNotNone(point_values)
        self.assertEqual((point_values["domain"], point_values["count"]),
                         ("POINT", len(curves.points)))
        np.testing.assert_array_equal(
            by[point_values["section"]], list(range(len(curves.points)))
        )

        names = [entry["name"] for entry in attributes]
        self.assertNotIn("curve_type", names)
        self.assertNotIn("position", names)


if __name__ == "__main__":
    unittest.main()
