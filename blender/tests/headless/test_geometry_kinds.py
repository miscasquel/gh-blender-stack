"""Layer B, geometry kinds: curves, point clouds, SubD cages, mixed output.

Run with:

    scripts/test-blender

Covers the section round trips for each geometry kind in Blender, then the
engine path: a GH response carrying mesh + curves + points lands in the three
per-kind helpers, and a later response without curves or points clears them.
"""

import json
import math
import os
import subprocess
import sys
import tempfile
import time
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

PORT = 9143


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
        bpy.ops.preferences.addon_enable(module="gh_stack")
        bpy.context.preferences.addons["gh_stack"].preferences.data_root = tempfile.mkdtemp(
            prefix="gh-stack-test-"
        )
        import gh_stack.blender.engine as engine
        import gh_stack.blender.stack as stack_mod

        cls.engine = engine
        cls.stack_mod = stack_mod
        cls.report_path = os.path.join(tempfile.gettempdir(), "fake_gh_mixed_report.json")
        if os.path.exists(cls.report_path):
            os.remove(cls.report_path)

        bpy.ops.mesh.primitive_cube_add(size=2.0)
        cls.obj = bpy.context.object
        cls.obj.name = "Mixed"

        engine.start_server(PORT)
        time.sleep(0.5)

        bpy.ops.gh_stack.add_stage(label="Mixed")
        cls.stage = cls.obj.gh_stack.stages[0]

        cls.fake = subprocess.Popen(
            [
                sys.executable,
                os.path.join(REPO, "tools", "fake_gh", "fake_gh.py"),
                "--port", str(PORT),
                "--stage", cls.stage.key,
                "--report", cls.report_path,
                "--extras",
            ],
            env=dict(
                os.environ,
                PYTHONPATH=os.pathsep.join(
                    [os.path.join(REPO, "blender", "gh_stack", "vendor"), REPO]
                ),
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

        def ready():
            return cls.stage.state in ("ok", "error")

        cls._pump(until=ready)
        cls.engine.push(cls.obj)
        cls._pump(until=ready)

    @classmethod
    def _pump(cls, seconds=8.0, until=None):
        deadline = time.time() + seconds
        while time.time() < deadline:
            cls.engine._timer()
            if until is not None and until():
                return True
            time.sleep(0.01)
        return until is None

    @classmethod
    def tearDownClass(cls):
        if cls.fake.poll() is None:
            cls.fake.terminate()
            try:
                cls.fake.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cls.fake.kill()
        bpy.ops.preferences.addon_disable(module="gh_stack")

    def test_01_response_reaches_all_three_helpers(self):
        import gh_stack.blender.helpers as helpers

        self.assertEqual(self.stage.state, "ok", self.stage.error)
        mesh_helper = helpers.ensure_helper(self.obj, self.stage.key, "mesh")
        curve_helper = helpers.ensure_helper(self.obj, self.stage.key, "curves")
        point_helper = helpers.ensure_helper(self.obj, self.stage.key, "points")
        self.assertEqual(len(mesh_helper.data.vertices), 8)
        self.assertEqual(len(curve_helper.data.splines), 1)
        self.assertEqual(len(curve_helper.data.splines[0].points), 5)
        self.assertEqual(len(point_helper.data.points), 8)

    def test_02_group_reads_every_helper(self):
        import gh_stack.blender.nodes as nodes

        group = bpy.data.node_groups.get(nodes.group_name(self.stage.key))
        self.assertIsNotNone(group)
        infos = [n for n in group.nodes if n.bl_idname == "GeometryNodeObjectInfo"]
        self.assertEqual(len(infos), 3)
        self.assertTrue(all(n.transform_space == "ORIGINAL" for n in infos))

    def test_03_a_response_without_curves_or_points_clears_them(self):
        import gh_stack.blender.helpers as helpers
        import gh_stack.blender.stack as stack_mod
        from blender.gh_stack.blender import engine as engine_mod

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

        self.assertTrue(self._pump(until=updated), "fake never saw subd sections")
        # Wait for the response to be applied too: the next test reads the
        # evaluated components, which the subd response repopulates.
        self.assertTrue(
            self._pump(until=lambda: self.stage.state == "ok"), "subd response never applied"
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


if __name__ == "__main__":
    unittest.main()
