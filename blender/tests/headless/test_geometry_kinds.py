"""Layer B: curves, point clouds, SubD cages and mixed output."""

import json
import math
import os
import tempfile
import time
import unittest

import bpy
import numpy as np

from harness import free_port, isolate_data_root, pump, start_fake, stop_fake

from blender.stageloom.blender import geometry_io  # noqa: E402


def new_curve_object(name="curve"):
    curve = bpy.data.curves.new(name, "CURVE")
    curve.dimensions = "3D"
    obj = bpy.data.objects.new(name, curve)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def new_pointcloud_object(name="points", positions=(), radii=None):
    cloud = bpy.data.pointclouds.new(name)
    cloud.resize(len(positions))
    if positions:
        flat = np.asarray(positions, dtype=np.float32).reshape(-1)
        cloud.points.foreach_set("co", flat)
    if radii is not None:
        cloud.points.foreach_set("radius", np.asarray(radii, dtype=np.float32))
    cloud.points.update()
    obj = bpy.data.objects.new(name, cloud)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def by_name(sections):
    return {name: values for name, dtype, values in sections}


def circle_bezier_net(radius=1.0, segments=8):
    """A circle as a closed cubic Bezier net (co0, hr0, hl1, co1, ...)."""
    k = (4.0 / 3.0) * math.tan(math.pi / (2 * segments)) * radius
    net = []

    def point(a):
        return (radius * math.cos(a), radius * math.sin(a), 0.0)

    def tangent(a):
        return (-math.sin(a), math.cos(a), 0.0)

    a0 = 0.0
    net.append(point(a0))
    for i in range(segments):
        a1 = 2.0 * math.pi * (i + 1) / segments
        p0, t0 = point(a0), tangent(a0)
        p1, t1 = point(a1), tangent(a1)
        net.append((p0[0] + k * t0[0], p0[1] + k * t0[1], 0.0))
        net.append((p1[0] - k * t1[0], p1[1] - k * t1[1], 0.0))
        net.append(p1)
        a0 = a1
    return net


def curve_samples(curve, resolution=12):
    """The polyline mesh of a curve datablock, as N x 3 positions."""
    curve.resolution_u = resolution
    obj = bpy.data.objects.new("eval-curve", curve)
    bpy.context.scene.collection.objects.link(obj)
    try:
        dg = bpy.context.evaluated_depsgraph_get()
        mesh = bpy.data.meshes.new_from_object(obj.evaluated_get(dg))
        co = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
        mesh.vertices.foreach_get("co", co)
        return co.reshape(-1, 3)
    finally:
        bpy.data.objects.remove(obj, do_unlink=True)


class CurveRoundTripTests(unittest.TestCase):
    def setUp(self):
        bpy.ops.wm.read_factory_settings(use_empty=True)

    def test_poly_round_trip(self):
        obj = new_curve_object("poly")
        spline = obj.data.splines.new("POLY")
        spline.points.add(4)
        pts = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (0, 0, 1)]
        for i, p in enumerate(pts):
            spline.points[i].co = (p[0], p[1], p[2], 1.0)
        spline.use_cyclic_u = True

        sections = geometry_io.curve_to_sections(obj.data)
        by = by_name(sections)
        self.assertEqual(list(by["crv.types"]), [geometry_io.CURVE_POLY])
        self.assertEqual(list(by["crv.counts"]), [5])
        self.assertEqual(list(by["crv.cyclic"]), [1])

        target = bpy.data.curves.new("poly-dst", "CURVE")
        geometry_io.sections_to_curve(target, sections)
        self.assertEqual(len(target.splines), 1)
        back = target.splines[0]
        self.assertEqual(back.type, "POLY")
        self.assertTrue(back.use_cyclic_u)
        co = np.empty(len(back.points) * 4, dtype=np.float32)
        back.points.foreach_get("co", co)
        np.testing.assert_allclose(co.reshape(-1, 4)[:, :3], np.asarray(pts, dtype=np.float32))

    def test_nurbs_round_trip_keeps_order_and_weights(self):
        obj = new_curve_object("nurbs")
        spline = obj.data.splines.new("NURBS")
        spline.points.add(5)
        pts = [(i, float(i % 2), 0.0) for i in range(6)]
        weights = [1.0, 1.0, 2.5, 1.0, 1.0, 1.0]
        for i, p in enumerate(pts):
            spline.points[i].co = (p[0], p[1], p[2], 1.0)
            spline.points[i].weight = weights[i]
        spline.order_u = 4
        spline.use_endpoint_u = True

        sections = geometry_io.curve_to_sections(obj.data)
        by = by_name(sections)
        self.assertEqual(list(by["crv.types"]), [geometry_io.CURVE_NURBS])
        self.assertEqual(list(by["crv.orders"]), [4])
        self.assertEqual(list(by["crv.knots_mode"]), [geometry_io.KNOTS_ENDPOINT])
        np.testing.assert_allclose(by["crv.points"][:, 3], weights, rtol=0, atol=1e-6)

        target = bpy.data.curves.new("nurbs-dst", "CURVE")
        geometry_io.sections_to_curve(target, sections)
        back = target.splines[0]
        self.assertEqual(back.type, "NURBS")
        self.assertEqual(back.order_u, 4)
        self.assertTrue(back.use_endpoint_u)
        got_w = np.empty(len(back.points), dtype=np.float32)
        back.points.foreach_get("weight", got_w)
        np.testing.assert_allclose(got_w, weights, rtol=0, atol=1e-6)

    def test_cyclic_nurbs_keeps_one_cyclic_spline_across_the_wire(self):
        """A uniform cyclic NURBS travels as one NURBS with its order, knot mode
        and cyclic flag — the same form Blender sent — so it comes back as one
        cyclic spline instead of a span split (the stage-0a1764 shape)."""
        obj = new_curve_object("cyc")
        spline = obj.data.splines.new("NURBS")
        spline.points.add(5)
        pts = [(7.67, 0.0, 0.0), (3.835, 6.6424155, 0.0), (-3.835, 6.642415, 0.0),
               (-7.67, 0.0, 0.0), (-3.835, -6.6424155, 0.0), (3.835, -6.6424155, 0.0)]
        for i, p in enumerate(pts):
            spline.points[i].co = (p[0], p[1], p[2], 1.0)
        spline.order_u = 6
        spline.use_endpoint_u = False
        spline.use_cyclic_u = True
        source_samples = curve_samples(obj.data)

        sections = geometry_io.curve_to_sections(obj.data)
        by = by_name(sections)
        self.assertEqual(list(by["crv.types"]), [geometry_io.CURVE_NURBS])
        self.assertEqual(list(by["crv.orders"]), [6])
        self.assertEqual(list(by["crv.knots_mode"]), [geometry_io.KNOTS_NORMAL])
        self.assertEqual(list(by["crv.cyclic"]), [1])
        self.assertEqual(list(by["crv.counts"]), [6])

        target = bpy.data.curves.new("cyc-dst", "CURVE")
        geometry_io.sections_to_curve(target, sections)
        self.assertEqual(len(target.splines), 1)
        back = target.splines[0]
        self.assertEqual(back.type, "NURBS")
        self.assertEqual(back.order_u, 6)
        self.assertTrue(back.use_cyclic_u)
        self.assertFalse(back.use_endpoint_u)
        np.testing.assert_allclose(curve_samples(target), source_samples, rtol=0, atol=1e-5)

    def test_cyclic_rational_nurbs_wire_keeps_weights(self):
        obj = new_curve_object("cyc-rational")
        spline = obj.data.splines.new("NURBS")
        spline.points.add(5)
        pts = [(0, 0, 0), (1, 2, 0), (-1, 2, 0), (-2, 0, 0), (-1, -2, 0), (1, -2, 0)]
        weights = [1.0, 0.8, 1.0, 0.8, 1.0, 0.8]
        for i, p in enumerate(pts):
            spline.points[i].co = (p[0], p[1], p[2], 1.0)
            spline.points[i].weight = weights[i]
        spline.order_u = 6
        spline.use_endpoint_u = False
        spline.use_cyclic_u = True

        sections = geometry_io.curve_to_sections(obj.data)
        by = by_name(sections)
        np.testing.assert_allclose(by["crv.points"][:, 3], weights, rtol=0, atol=1e-6)
        self.assertEqual(list(by["crv.cyclic"]), [1])

        target = bpy.data.curves.new("cyc-rational-dst", "CURVE")
        geometry_io.sections_to_curve(target, sections)
        back = target.splines[0]
        self.assertTrue(back.use_cyclic_u)
        got_w = np.empty(6, dtype=np.float32)
        back.points.foreach_get("weight", got_w)
        np.testing.assert_allclose(got_w, weights, rtol=0, atol=1e-6)

    def test_evaluated_nurbs_component_is_the_cyclic_wire_form(self):
        """The prefix path (a node group's evaluated curves component) sends a
        cyclic NURBS as one cyclic wire curve with its own order and knot mode."""
        obj = new_curve_object("cyc-component")
        spline = obj.data.splines.new("NURBS")
        spline.points.add(5)
        pts = [(7.67, 0.0, 0.0), (3.835, 6.6424155, 0.0), (-3.835, 6.642415, 0.0),
               (-7.67, 0.0, 0.0), (-3.835, -6.6424155, 0.0), (3.835, -6.6424155, 0.0)]
        for i, p in enumerate(pts):
            spline.points[i].co = (p[0], p[1], p[2], 1.0)
        spline.order_u = 6
        spline.use_endpoint_u = False
        spline.use_cyclic_u = True

        dg = bpy.context.evaluated_depsgraph_get()
        geometry = obj.evaluated_get(dg).evaluated_geometry()
        sections, _attributes, _definitions = geometry_io.geometry_set_to_sections(geometry)
        by = by_name(sections)
        self.assertEqual(list(by["crv.types"]), [geometry_io.CURVE_NURBS])
        self.assertEqual(list(by["crv.orders"]), [6])
        self.assertEqual(list(by["crv.knots_mode"]), [geometry_io.KNOTS_NORMAL])
        self.assertEqual(list(by["crv.cyclic"]), [1])
        self.assertEqual(list(by["crv.counts"]), [6])
        self.assertEqual(len(by["crv.points"]), 6)

    def test_bezier_travels_as_a_bezier_knot_net_and_returns(self):
        obj = new_curve_object("bez")
        spline = obj.data.splines.new("BEZIER")
        spline.bezier_points.add(2)
        bp = spline.bezier_points
        co = [(0, 0, 0), (2, 0, 0), (4, 0, 0)]
        hl = [(-1, 0, 0), (1.5, -1, 0), (3, -1, 0)]
        hr = [(1, 1, 0), (3, 1, 0), (5, 0, 0)]
        for i in range(3):
            bp[i].handle_left_type = "FREE"
            bp[i].handle_right_type = "FREE"
            bp[i].co = co[i]
            bp[i].handle_left = hl[i]
            bp[i].handle_right = hr[i]

        sections = geometry_io.curve_to_sections(obj.data)
        by = by_name(sections)
        self.assertEqual(list(by["crv.knots_mode"]), [geometry_io.KNOTS_BEZIER])
        self.assertEqual(list(by["crv.orders"]), [4])
        # 2 spans -> 3k+1 = 7 control points.
        self.assertEqual(len(by["crv.points"]), 7)

        target = bpy.data.curves.new("bez-dst", "CURVE")
        geometry_io.sections_to_curve(target, sections)
        back = target.splines[0]
        self.assertEqual(back.type, "BEZIER")
        self.assertEqual(len(back.bezier_points), 3)
        for i in range(3):
            np.testing.assert_allclose(back.bezier_points[i].co[:], co[i], rtol=0, atol=1e-6)
        # The first left handle and the last right handle are not part of the
        # open net; every interior handle must survive exactly.
        np.testing.assert_allclose(back.bezier_points[0].handle_right[:], hr[0], rtol=0, atol=1e-6)
        np.testing.assert_allclose(back.bezier_points[1].handle_left[:], hl[1], rtol=0, atol=1e-6)
        np.testing.assert_allclose(back.bezier_points[1].handle_right[:], hr[1], rtol=0, atol=1e-6)
        np.testing.assert_allclose(back.bezier_points[2].handle_left[:], hl[2], rtol=0, atol=1e-6)

    def test_rational_quadratic_spans_make_a_circle(self):
        """The wire form the GH side now sends for a circle: four rational
        quadratic spans, one wire curve each, Euclidean points plus weights.
        Blender must reconstruct an exact circle."""
        k = math.sqrt(0.5)
        # Euclidean control points plus weight: the tangent-intersection corners
        # carry the weight, exactly what the wire carries.
        unique = [
            (5.0, 0.0, 0.0, 1.0), (5.0, 5.0, 0.0, k), (0.0, 5.0, 0.0, 1.0),
            (-5.0, 5.0, 0.0, k), (-5.0, 0.0, 0.0, 1.0), (-5.0, -5.0, 0.0, k),
            (0.0, -5.0, 0.0, 1.0), (5.0, -5.0, 0.0, k), (5.0, 0.0, 0.0, 1.0),
        ]
        # Each span is sent with its own control points, so shared endpoints
        # appear in two spans: span s is unique[2s .. 2s+2].
        cps = unique[0:3] + unique[2:5] + unique[4:7] + unique[6:9]
        sections = [
            ("crv.points", "f32", np.asarray(cps, dtype=np.float32).reshape(-1, 4)),
            ("crv.counts", "u32", np.array([3, 3, 3, 3], dtype=np.uint32)),
            ("crv.types", "u8", np.array([2, 2, 2, 2], dtype=np.uint8)),
            ("crv.orders", "u8", np.array([3, 3, 3, 3], dtype=np.uint8)),
            ("crv.knots_mode", "u8", np.array([1, 1, 1, 1], dtype=np.uint8)),
            ("crv.cyclic", "u8", np.array([0, 0, 0, 0], dtype=np.uint8)),
        ]
        target = bpy.data.curves.new("spans-dst", "CURVE")
        geometry_io.sections_to_curve(target, sections)
        self.assertEqual(len(target.splines), 4)
        samples = curve_samples(target)
        radii = np.linalg.norm(samples[:, :2], axis=1)
        self.assertLess(float(np.max(np.abs(radii - 5.0))), 1e-3)

    def test_closed_cubic_bezier_net_reconstructs_a_circle(self):
        """The net the GH side now sends for a fitted circle: 8 cubic segments,
        closed. The reconstructed Bezier spline must sample a circle."""
        net = circle_bezier_net(1.0, 8)
        points = np.hstack([np.asarray(net, dtype=np.float32),
                            np.ones((len(net), 1), dtype=np.float32)])
        sections = [
            ("crv.points", "f32", points),
            ("crv.counts", "u32", np.array([len(net)], dtype=np.uint32)),
            ("crv.types", "u8", np.array([geometry_io.CURVE_NURBS], dtype=np.uint8)),
            ("crv.orders", "u8", np.array([4], dtype=np.uint8)),
            ("crv.knots_mode", "u8", np.array([geometry_io.KNOTS_BEZIER], dtype=np.uint8)),
            ("crv.cyclic", "u8", np.array([0], dtype=np.uint8)),
        ]
        target = bpy.data.curves.new("circle-dst", "CURVE")
        geometry_io.sections_to_curve(target, sections)
        self.assertEqual(len(target.splines[0].bezier_points), 9)
        samples = curve_samples(target)
        radii = np.linalg.norm(samples[:, :2], axis=1)
        self.assertLess(float(np.max(np.abs(radii - 1.0))), 1e-3)

    def test_cyclic_bezier_closes(self):
        obj = new_curve_object("bez-cyclic")
        spline = obj.data.splines.new("BEZIER")
        spline.bezier_points.add(2)
        bp = spline.bezier_points
        for i, p in enumerate([(0, 0, 0), (2, 0, 0), (1, 2, 0)]):
            bp[i].co = p
            bp[i].handle_left_type = "FREE"
            bp[i].handle_right_type = "FREE"
            bp[i].handle_left = (p[0] - 1, p[1], 0)
            bp[i].handle_right = (p[0] + 1, p[1], 0)
        spline.use_cyclic_u = True

        sections = geometry_io.curve_to_sections(obj.data)
        by = by_name(sections)
        net = by["crv.points"]
        self.assertEqual(len(net), 10)  # 3 segments -> 3k+1
        np.testing.assert_allclose(net[0], net[-1], rtol=0, atol=1e-6)

        target = bpy.data.curves.new("bez-cyclic-dst", "CURVE")
        geometry_io.sections_to_curve(target, sections)
        back = target.splines[0]
        self.assertEqual(len(back.bezier_points), 4)
        np.testing.assert_allclose(
            back.bezier_points[0].co[:], back.bezier_points[3].co[:], rtol=0, atol=1e-6
        )


class CurveValidationTests(unittest.TestCase):
    """A malformed crv.* group is a classified error, never a short rebuild."""

    def setUp(self):
        bpy.ops.wm.read_factory_settings(use_empty=True)

    def sections(self, n=3, types=(geometry_io.CURVE_NURBS,), orders=(3,),
                 modes=(geometry_io.KNOTS_ENDPOINT,), cyclic=(0,), weights=None):
        counts = np.full(len(types), n, dtype=np.uint32)
        total = int(counts.sum())
        points = np.ones((total, 4), dtype=np.float32)
        if weights is not None:
            points[:, 3] = np.asarray(weights, dtype=np.float32)
        return [
            ("crv.points", "f32", points),
            ("crv.counts", "u32", counts),
            ("crv.types", "u8", np.asarray(types, dtype=np.uint8)),
            ("crv.orders", "u8", np.asarray(orders, dtype=np.uint8)),
            ("crv.knots_mode", "u8", np.asarray(modes, dtype=np.uint8)),
            ("crv.cyclic", "u8", np.asarray(cyclic, dtype=np.uint8)),
        ]

    def assert_invalid(self, sections, target=None):
        target = target or bpy.data.curves.new("invalid-dst", "CURVE")
        with self.assertRaises(geometry_io.GeometryIOError):
            geometry_io.sections_to_curve(target, sections)

    def test_nurbs_order_out_of_range_is_rejected_not_clamped(self):
        for order in (1, 7):
            with self.subTest(order=order):
                self.assert_invalid(self.sections(n=4, orders=(order,)))

    def test_nurbs_order_above_the_control_point_count_is_rejected(self):
        self.assert_invalid(self.sections(n=3, orders=(4,)))

    def test_array_lengths_and_counts_are_validated(self):
        good = self.sections(n=3)
        cases = {
            "missing_counts": [entry for entry in good if entry[0] != "crv.counts"],
            "short_types": [
                (name, dtype, values[:0] if name == "crv.types" else values)
                for name, dtype, values in good
            ],
            "count_sum_mismatch": [
                (name, dtype, np.array([4], dtype=np.uint32) if name == "crv.counts" else values)
                for name, dtype, values in good
            ],
            "point_not_xyz w": [
                (name, dtype, values.reshape(-1)[:11] if name == "crv.points" else values)
                for name, dtype, values in good
            ],
        }
        for label, sections in cases.items():
            with self.subTest(case=label):
                self.assert_invalid(sections)

    def test_type_mode_cyclic_and_bezier_shape_are_validated(self):
        cases = {
            "type": self.sections(types=(3,)),
            "mode": self.sections(modes=(3,)),
            "cyclic": self.sections(cyclic=(2,)),
            "bezier_shape": self.sections(
                n=3, types=(geometry_io.CURVE_NURBS,),
                modes=(geometry_io.KNOTS_BEZIER,),
            ),
        }
        for label, sections in cases.items():
            with self.subTest(case=label):
                self.assert_invalid(sections)

    def test_weights_must_be_finite_and_positive(self):
        for weight in (0.0, -1.0, float("nan"), float("inf")):
            with self.subTest(weight=weight):
                self.assert_invalid(self.sections(n=3, weights=[1.0, weight, 1.0]))

    def test_a_malformed_group_leaves_the_destination_untouched(self):
        target = bpy.data.curves.new("kept", "CURVE")
        spline = target.splines.new("POLY")
        spline.points.add(1)
        self.assertEqual(len(target.splines), 1)
        self.assert_invalid(self.sections(n=1), target=target)
        self.assertEqual(len(target.splines), 1, "a rejected group cleared the target")


class PointRoundTripTests(unittest.TestCase):
    def setUp(self):
        bpy.ops.wm.read_factory_settings(use_empty=True)

    def test_points_round_trip(self):
        positions = [(0, 0, 0), (1, 2, 3), (-1, 0.5, 2)]
        radii = [0.1, 0.2, 0.3]
        obj = new_pointcloud_object("pts", positions, radii)

        sections = geometry_io.points_to_sections(obj.data)
        by = by_name(sections)
        np.testing.assert_allclose(by["pts.positions"], np.asarray(positions, dtype=np.float32))
        np.testing.assert_allclose(by["pts.radii"], np.asarray(radii, dtype=np.float32))

        target = bpy.data.pointclouds.new("pts-dst")
        geometry_io.sections_to_points(target, sections)
        self.assertEqual(len(target.points), 3)
        co = np.empty(9, dtype=np.float32)
        target.points.foreach_get("co", co)
        np.testing.assert_allclose(co.reshape(-1, 3), np.asarray(positions, dtype=np.float32))
        got_r = np.empty(3, dtype=np.float32)
        target.points.foreach_get("radius", got_r)
        np.testing.assert_allclose(got_r, np.asarray(radii, dtype=np.float32))

    def test_absent_radii_clear_a_previous_response(self):
        obj = new_pointcloud_object("pts-radii-clear", [(0, 0, 0), (1, 0, 0)], [2.0, 3.0])
        sections = geometry_io.points_to_sections(obj.data)
        self.assertIn("pts.radii", by_name(sections))

        without_radii = [entry for entry in sections if entry[0] != "pts.radii"]
        geometry_io.sections_to_points(obj.data, without_radii)
        got = np.empty(2, dtype=np.float32)
        obj.data.points.foreach_get("radius", got)
        np.testing.assert_allclose(
            got, np.full(2, geometry_io.POINT_RADIUS_DEFAULT, dtype=np.float32)
        )

    def test_empty_points_clear(self):
        obj = new_pointcloud_object("pts-clear", [(1, 1, 1)], [0.1])
        sections = geometry_io.points_to_sections(obj.data)
        self.assertTrue(sections)
        geometry_io.sections_to_points(obj.data, [])
        self.assertEqual(len(obj.data.points), 0)


class SubDRoundTripTests(unittest.TestCase):
    def setUp(self):
        bpy.ops.wm.read_factory_settings(use_empty=True)

    def test_subd_sections_keep_creases(self):
        mesh = bpy.data.meshes.new("cage")
        verts = [(0, 0, 0), (1, 0, 0), (2, 0, 0), (0, 1, 0), (1, 1, 0), (2, 1, 0)]
        faces = [(0, 1, 4, 3), (1, 2, 5, 4)]
        mesh.from_pydata(verts, [], faces)
        mesh.update()
        attr = mesh.attributes.new("crease_edge", "FLOAT", "EDGE")
        values = np.zeros(len(mesh.edges), dtype=np.float32)
        values[1] = 1.0
        values[3] = 0.5
        attr.data.foreach_set("value", values)

        sections = geometry_io.mesh_to_sections(mesh, as_subd=True)
        names = [s[0] for s in sections]
        self.assertIn("subd.crease_edges", names)

        target = bpy.data.meshes.new("cage-dst")
        geometry_io.sections_to_mesh(target, sections)
        back = np.empty(len(target.edges), dtype=np.float32)
        target.attributes["crease_edge"].data.foreach_get("value", back)
        # The two creased edges survive by vertex pair; everything else is zero.
        src_edges = np.empty(len(mesh.edges) * 2, dtype=np.uint32)
        mesh.edges.foreach_get("vertices", src_edges)
        dst_edges = np.empty(len(target.edges) * 2, dtype=np.uint32)
        target.edges.foreach_get("vertices", dst_edges)
        wanted = {}
        for i, (a, b) in enumerate(src_edges.reshape(-1, 2)):
            if values[i] > 0:
                wanted[(min(a, b), max(a, b))] = values[i]
        got = {}
        for i, (a, b) in enumerate(dst_edges.reshape(-1, 2)):
            if back[i] > 0:
                got[(min(int(a), int(b)), max(int(a), int(b)))] = float(back[i])
        self.assertEqual(wanted, got)


class ObjectDispatchTests(unittest.TestCase):
    def setUp(self):
        bpy.ops.wm.read_factory_settings(use_empty=True)

    def test_object_to_sections_by_type(self):
        obj = new_curve_object("dispatch-curve")
        spline = obj.data.splines.new("POLY")
        spline.points.add(1)
        names = [s[0] for s in geometry_io.object_to_sections(obj)[0]]
        self.assertTrue(names[0].startswith("crv."), names)

        cloud = new_pointcloud_object("dispatch-points", [(0, 0, 0)])
        names = [s[0] for s in geometry_io.object_to_sections(cloud)[0]]
        self.assertTrue(names[0].startswith("pts."), names)

        mesh = bpy.data.meshes.new("dispatch-mesh")
        mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
        mesh.update()
        names = [s[0] for s in geometry_io.object_to_sections(
            bpy.data.objects.new("dispatch-mesh", mesh))[0]]
        self.assertTrue(names[0].startswith("mesh."), names)


class MixedEngineTests(unittest.TestCase):
    """The engine against fake_gh --extras: one response with all three kinds."""

    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="stageloom")
        cls.enterClassContext(isolate_data_root("stageloom-test-"))
        import stageloom.blender.engine as engine
        import stageloom.blender.stack as stack_mod

        cls.engine = engine
        cls.stack_mod = stack_mod
        cls.port = free_port()
        cls.report_path = os.path.join(tempfile.gettempdir(), "fake_gh_mixed_report.json")
        if os.path.exists(cls.report_path):
            os.remove(cls.report_path)

        bpy.ops.mesh.primitive_cube_add(size=2.0)
        cls.obj = bpy.context.object
        cls.obj.name = "Mixed"

        engine.start_server(cls.port)
        time.sleep(0.5)

        bpy.ops.stageloom.add_stage(label="Mixed")
        cls.stage = cls.obj.gh_stack.stages[0]

        cls.fake = start_fake(
            cls.stage.key, cls.report_path, "--extras", port=cls.port
        )

        def ready():
            return cls.stage.state in ("ok", "error")

        pump(until=ready)
        cls.engine.push(cls.obj)
        pump(until=ready)

    @classmethod
    def tearDownClass(cls):
        stop_fake(cls.fake)
        bpy.ops.preferences.addon_disable(module="stageloom")

    def test_01_response_reaches_all_three_helpers(self):
        import stageloom.blender.helpers as helpers

        self.assertEqual(self.stage.state, "ok", self.stage.error)
        mesh_helper = helpers.ensure_helper(self.obj, self.stage.key, "mesh")
        curve_helper = helpers.ensure_helper(self.obj, self.stage.key, "curves")
        point_helper = helpers.ensure_helper(self.obj, self.stage.key, "points")
        self.assertEqual(len(mesh_helper.data.vertices), 8)
        self.assertEqual(len(curve_helper.data.splines), 1)
        self.assertEqual(len(curve_helper.data.splines[0].points), 5)
        self.assertEqual(len(point_helper.data.points), 8)

    def test_02_group_reads_every_helper(self):
        import stageloom.blender.nodes as nodes

        group = bpy.data.node_groups.get(nodes.group_name(self.stage.key))
        self.assertIsNotNone(group)
        infos = [n for n in group.nodes if n.bl_idname == "GeometryNodeObjectInfo"]
        self.assertEqual(len(infos), 3)
        self.assertTrue(all(n.transform_space == "ORIGINAL" for n in infos))

    def test_03_a_response_without_curves_or_points_clears_them(self):
        import stageloom.blender.helpers as helpers
        from blender.stageloom.blender import engine as engine_mod

        mesh_sections = {"mesh.verts": [(0, 0, 0), (1, 0, 0), (0, 1, 0)],
                         "mesh.face_counts": [3],
                         "mesh.face_indices": [0, 1, 2]}
        engine_mod._apply(self.obj, self.stage, mesh_sections)
        curve_helper = helpers.ensure_helper(self.obj, self.stage.key, "curves")
        point_helper = helpers.ensure_helper(self.obj, self.stage.key, "points")
        self.assertEqual(len(curve_helper.data.splines), 0)
        self.assertEqual(len(point_helper.data.points), 0)

    def test_04_subd_flag_selects_subd_sections(self):
        self.obj.gh_stack.subd = True
        self.engine.push(self.obj)

        def updated():
            if os.path.exists(self.report_path):
                report = json.load(open(self.report_path))
                return "subd.verts" in report.get("sections", [])
            return False

        self.assertTrue(pump(until=updated), "fake never saw subd sections")
        # Wait for the response to be applied too: the next test reads the
        # evaluated components, which the subd response repopulates.
        self.assertTrue(
            pump(until=lambda: self.stage.state == "ok"), "subd response never applied"
        )
        self.obj.gh_stack.subd = False

    def test_05_evaluated_geometry_carries_all_components(self):
        """Downstream, the evaluated stack object is a geometry set with mesh,
        curve, and point components — what native modifiers act on."""
        dg = bpy.context.evaluated_depsgraph_get()
        geometry = self.obj.evaluated_get(dg).evaluated_geometry()
        self.assertIsNotNone(geometry.mesh)
        self.assertGreaterEqual(len(geometry.mesh.vertices), 3)

        curves = geometry.curves
        self.assertIsNotNone(curves, "no curve component in the evaluated geometry")
        count = 0
        for attr in ("splines", "curves"):
            if hasattr(curves, attr):
                count = len(getattr(curves, attr))
                break
        self.assertGreaterEqual(count, 1)

        pointcloud = geometry.pointcloud
        self.assertIsNotNone(pointcloud, "no point component in the evaluated geometry")
        self.assertGreaterEqual(len(pointcloud.points), 1)


class InputWarningsEngineTests(unittest.TestCase):
    """The prefix read owns ``stage.input_warnings``; response warnings do not."""

    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="stageloom")
        cls.enterClassContext(isolate_data_root("stageloom-input-warnings-"))
        import stageloom.blender.engine as engine

        cls.engine = engine
        bpy.ops.mesh.primitive_cube_add(size=1.0)
        cls.obj = bpy.context.object
        cls.obj.name = "InputWarnings"
        bpy.ops.stageloom.add_stage(label="Warnings")
        cls.stage = cls.obj.gh_stack.stages[0]

    @classmethod
    def tearDownClass(cls):
        bpy.ops.preferences.addon_disable(module="stageloom")

    def setUp(self):
        self.engine._prefix_memo.clear()
        self.engine._prefix_batch.clear()
        self.engine._prefix_batch_built = False
        self.stage.input_warnings = ""

    def _read(self, appended_notes):
        module = self.engine.geometry_io
        real = module.geometry_set_to_sections

        def with_notes(geometry, as_subd=False, notes=None):
            result = real(geometry, as_subd=as_subd, notes=notes)
            if notes is not None:
                notes.extend(appended_notes)
            return result

        module.geometry_set_to_sections = with_notes
        try:
            return self.engine._prefix_sections(
                self.obj, self.obj.gh_stack, self.stage
            )
        finally:
            module.geometry_set_to_sections = real

    def test_notes_reach_the_stage_and_clear_when_absent(self):
        self._read(["2 Catmull-Rom curve components were skipped"])
        self.assertEqual(
            self.stage.input_warnings,
            "2 Catmull-Rom curve components were skipped",
        )

        self.engine._prefix_memo.clear()
        self._read([])
        self.assertEqual(self.stage.input_warnings, "")

    def test_catmull_rom_notes_reach_the_stage_from_real_geometry(self):
        curves = bpy.data.hair_curves.new("catmull-prefix-src")
        curves.add_curves([4, 4])
        kind = curves.attributes.new("curve_type", "INT", "CURVE")
        for element in kind.data:
            element.value = geometry_io.CURVE_TYPE_CATMULL_ROM
        source = bpy.data.objects.new("catmull-prefix-src", curves)
        bpy.context.scene.collection.objects.link(source)

        group = bpy.data.node_groups.new("catmull-prefix", "GeometryNodeTree")
        group.interface.new_socket(
            name="Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry"
        )
        output = group.nodes.new("NodeGroupOutput")
        info = group.nodes.new("GeometryNodeObjectInfo")
        info.inputs["Object"].default_value = source
        info.transform_space = "ORIGINAL"
        group.links.new(info.outputs["Geometry"], output.inputs[0])
        modifier = self.obj.modifiers.new("catmull-prefix", "NODES")
        modifier.node_group = group
        self.obj.modifiers.move(
            [entry.name for entry in self.obj.modifiers].index(modifier.name), 0
        )
        try:
            self.engine._prefix_memo.clear()
            self.engine._prefix_batch.clear()
            self.engine._prefix_batch_built = False
            self.engine._prefix_sections(
                self.obj, self.obj.gh_stack, self.stage
            )
            self.assertIn("2 Catmull-Rom", self.stage.input_warnings)
        finally:
            self.obj.modifiers.remove(modifier)
            bpy.data.objects.remove(source, do_unlink=True)
            bpy.data.hair_curves.remove(curves)
            bpy.data.node_groups.remove(group)

    def test_an_unchanged_read_does_not_rewrite_the_channel(self):
        self._read(["1 conversion note"])
        writes = []
        real_set = self.engine._set

        def counting(stage, field, value):
            if field == "input_warnings" and getattr(stage, field) != value:
                writes.append(value)
            return real_set(stage, field, value)

        self.engine._set = counting
        try:
            self._read(["1 conversion note"])
        finally:
            self.engine._set = real_set

        self.assertEqual(writes, [], "an unchanged prefix rewrote input_warnings")

    def test_response_warnings_are_a_separate_field(self):
        self.stage.warnings = "response warning"
        self._read(["input note"])
        self.assertEqual(self.stage.warnings, "response warning")
        self.assertEqual(self.stage.input_warnings, "input note")


if __name__ == "__main__":
    unittest.main()
