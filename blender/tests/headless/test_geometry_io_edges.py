"""Layer B: geometry_io boundary and clearing behavior (T4 characterization).

These tests pin the conversion contracts a package split must not change:
classified malformed groups, absent-means-clear, counted notes for losses, and
the per-kind dispatch surface. They deliberately use the public module
functions; the one private helper named here (``_reject_duplicate_sections``)
is the only deterministic observer of the aggregate writer's duplicate guard
and lives in the instances module.
"""

import unittest

import bpy
import numpy as np

from blender.stageloom.blender import geometry_io  # noqa: E402
from blender.stageloom.blender.geometry_io import instances as geometry_instances  # noqa: E402


def quad_mesh(name="edge-quad"):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(
        [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [], [(0, 1, 2, 3)]
    )
    mesh.update()
    return mesh


def mesh_snapshot(mesh):
    co = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", co)
    loops = np.empty(len(mesh.loops), dtype=np.uint32)
    mesh.loops.foreach_get("vertex_index", loops)
    return co, loops, len(mesh.polygons)


class SectionGroupTests(unittest.TestCase):
    def test_a_colours_only_group_is_an_incomplete_mesh(self):
        with self.assertRaisesRegex(
            geometry_io.GeometryIOError, "mesh group is incomplete"
        ):
            geometry_io.select_mesh_group(
                {"mesh.colors": np.zeros((1, 4), dtype=np.uint8)}
            )

    def test_an_absent_or_foreign_group_selects_nothing(self):
        self.assertIsNone(geometry_io.select_mesh_group({}))
        self.assertIsNone(
            geometry_io.select_mesh_group(
                {"crv.points": np.zeros(4, dtype=np.float32)}
            )
        )

    def test_duplicate_section_names_are_rejected(self):
        geometry_instances._reject_duplicate_sections(
            [("mesh.verts", "f32", np.zeros(3))]
        )
        with self.assertRaisesRegex(
            geometry_io.GeometryIOError, "duplicate section name"
        ):
            geometry_instances._reject_duplicate_sections(
                [
                    ("mesh.verts", "f32", np.zeros(3)),
                    ("mesh.verts", "f32", np.zeros(3)),
                ]
            )


class MeshWriteValidationTests(unittest.TestCase):
    def setUp(self):
        bpy.ops.wm.read_factory_settings(use_empty=True)

    def test_malformed_mesh_groups_are_classified_and_leave_the_target_untouched(self):
        target = quad_mesh("target")
        verts = np.asarray(
            [(0, 0, 0), (1, 0, 0), (0, 1, 0)], dtype=np.float32
        )
        base = [
            ("mesh.verts", "f32", verts),
            ("mesh.face_counts", "u32", np.array([3], dtype=np.uint32)),
            ("mesh.face_indices", "u32", np.array([0, 1, 2], dtype=np.uint32)),
        ]
        cases = {
            "empty": [],
            "partial": base[:2],
            "face_count_sum": base[:1]
            + [
                ("mesh.face_counts", "u32", np.array([4], dtype=np.uint32)),
                ("mesh.face_indices", "u32", np.array([0, 1, 2], dtype=np.uint32)),
            ],
            "face_below_three": base[:1]
            + [
                ("mesh.face_counts", "u32", np.array([2], dtype=np.uint32)),
                ("mesh.face_indices", "u32", np.array([0, 1], dtype=np.uint32)),
            ],
            "index_out_of_range": base[:1]
            + [
                ("mesh.face_counts", "u32", np.array([3], dtype=np.uint32)),
                ("mesh.face_indices", "u32", np.array([0, 1, 9], dtype=np.uint32)),
            ],
            "verts_shape": [
                ("mesh.verts", "f32", np.array([0, 0, 0, 1], dtype=np.float32)),
                ("mesh.face_counts", "u32", np.array([3], dtype=np.uint32)),
                ("mesh.face_indices", "u32", np.array([0, 1, 2], dtype=np.uint32)),
            ],
        }
        before = mesh_snapshot(target)
        for label, sections in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(geometry_io.GeometryIOError):
                    geometry_io.sections_to_mesh(target, sections)
                after = mesh_snapshot(target)
                np.testing.assert_array_equal(after[0], before[0])
                np.testing.assert_array_equal(after[1], before[1])
                self.assertEqual(after[2], before[2])

    def test_absent_mesh_colours_remove_only_the_native_channel(self):
        mesh = quad_mesh("colours")
        native = mesh.color_attributes.new("Col", "BYTE_COLOR", "POINT")
        native.data[0].color_srgb = (1.0, 0.0, 0.0, 1.0)
        mesh.color_attributes.active_color = native
        mesh.color_attributes.new("tint", "BYTE_COLOR", "POINT")

        sections = [
            ("mesh.verts", "f32", np.asarray(
                [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)],
                dtype=np.float32,
            )),
            ("mesh.face_counts", "u32", np.array([4], dtype=np.uint32)),
            ("mesh.face_indices", "u32", np.array([0, 1, 2, 3], dtype=np.uint32)),
        ]
        geometry_io.sections_to_mesh(mesh, sections)

        self.assertIsNone(mesh.color_attributes.get("Col"))
        self.assertIsNotNone(
            mesh.color_attributes.get("tint"),
            "an absent mesh.colors removed a user colour attribute",
        )

    def test_absent_point_colours_remove_only_the_native_channel(self):
        cloud = bpy.data.pointclouds.new("cloud")
        cloud.resize(2)
        cloud.color_attributes.new("Col", "BYTE_COLOR", "POINT")
        cloud.color_attributes.new("tint", "BYTE_COLOR", "POINT")

        geometry_io.write_point_colors(cloud, None)

        self.assertIsNone(cloud.color_attributes.get("Col"))
        self.assertIsNotNone(cloud.color_attributes.get("tint"))

    def test_absent_creases_are_written_as_zero_not_left_stale(self):
        mesh = quad_mesh("stale-creases")
        attr = mesh.attributes.new(geometry_io.CREASE_ATTRIBUTE, "FLOAT", "EDGE")
        for element in attr.data:
            element.value = 0.75

        geometry_io.write_creases(mesh, None)

        values = np.empty(len(mesh.edges), dtype=np.float32)
        mesh.attributes[geometry_io.CREASE_ATTRIBUTE].data.foreach_get("value", values)
        np.testing.assert_array_equal(values, np.zeros(len(mesh.edges), np.float32))

    def test_corner_colour_folds_to_one_value_per_vertex(self):
        mesh = bpy.data.meshes.new("corner-colour")
        mesh.from_pydata(
            [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)],
            [],
            [(0, 1, 2), (0, 2, 3)],
        )
        mesh.update()
        attr = mesh.color_attributes.new("Col", "BYTE_COLOR", "CORNER")
        for i in range(len(mesh.loops)):
            attr.data[i].color_srgb = (1.0, 0.0, 0.0, 1.0)
        # The corner loop that follows vertex 0 last wins.
        attr.data[3].color_srgb = (0.0, 1.0, 0.0, 1.0)
        mesh.color_attributes.active_color = attr

        by_name = {
            name: values for name, _dtype, values in geometry_io.mesh_to_sections(mesh)
        }
        colours = by_name["mesh.colors"]
        self.assertEqual(len(colours), len(mesh.vertices))
        np.testing.assert_array_equal(colours[0], [0, 255, 0, 255])
        np.testing.assert_array_equal(
            colours[1:], np.tile(np.array([255, 0, 0, 255], dtype=np.uint8), (3, 1))
        )

    def test_a_crease_on_the_wrong_domain_is_warned_and_skipped(self):
        mesh = quad_mesh("wrong-crease")
        attr = mesh.attributes.new(geometry_io.CREASE_ATTRIBUTE, "FLOAT", "POINT")
        attr.data[0].value = 1.0
        notes = []

        by_name = {
            name: values
            for name, _dtype, values in geometry_io.mesh_to_sections(mesh, notes=notes)
        }

        self.assertNotIn("mesh.crease_edges", by_name)
        self.assertEqual(len(notes), 1)
        self.assertIn("not EDGE/FLOAT", notes[0])
        self.assertIn("creases were skipped", notes[0])


class AttributeWriteBoundaryTests(unittest.TestCase):
    def setUp(self):
        bpy.ops.wm.read_factory_settings(use_empty=True)

    def test_unknown_type_and_domain_are_noted_and_skipped(self):
        mesh = quad_mesh("attributes")
        entries = [
            (
                {"name": "mystery", "domain": "POINT", "type": "MYSTERY", "group": "mesh"},
                np.zeros(4, dtype=np.float32),
                None,
            ),
            (
                {"name": "bad_domain", "domain": "CURVE", "type": "FLOAT", "group": "mesh"},
                np.zeros(4, dtype=np.float32),
                None,
            ),
            (
                {"name": "weight", "domain": "POINT", "type": "FLOAT", "group": "mesh"},
                np.asarray([1, 2, 3, 4], dtype=np.float32),
                None,
            ),
        ]

        notes = geometry_io.write_attributes(mesh, entries)

        self.assertEqual(len(notes), 2, notes)
        self.assertTrue(any("unknown type" in note for note in notes), notes)
        self.assertTrue(any("has no CURVE domain" in note for note in notes), notes)
        self.assertIsNone(mesh.attributes.get("mystery"))
        self.assertIsNone(mesh.attributes.get("bad_domain"))
        self.assertIsNotNone(mesh.attributes.get("weight"))

    def test_prune_removes_generic_attributes_but_keeps_native_channels(self):
        mesh = quad_mesh("prune")
        mesh.attributes.new("old", "FLOAT", "POINT")
        mesh.attributes.new("weight", "FLOAT", "POINT")
        mesh.color_attributes.new("Col", "BYTE_COLOR", "POINT")
        mesh.attributes.new(geometry_io.CREASE_ATTRIBUTE, "FLOAT", "EDGE")

        notes = geometry_io.write_attributes(mesh, [], prune=True)

        self.assertEqual(notes, [])
        self.assertIsNone(mesh.attributes.get("old"))
        self.assertIsNone(mesh.attributes.get("weight"))
        self.assertIsNotNone(mesh.color_attributes.get("Col"))
        self.assertIsNotNone(mesh.attributes.get(geometry_io.CREASE_ATTRIBUTE))
        self.assertIn("position", mesh.attributes)
        self.assertIn(".select_vert", mesh.attributes)


class PointWriteValidationTests(unittest.TestCase):
    def setUp(self):
        bpy.ops.wm.read_factory_settings(use_empty=True)

    def test_an_incomplete_point_group_leaves_the_cloud_untouched(self):
        cloud = bpy.data.pointclouds.new("points")
        cloud.resize(2)
        cloud.points.foreach_set(
            "co", np.asarray([1, 2, 3, 4, 5, 6], dtype=np.float32)
        )
        cloud.points.update()

        with self.assertRaisesRegex(
            geometry_io.GeometryIOError, "point group is incomplete"
        ):
            geometry_io.sections_to_points(
                cloud, [("pts.radii", "f32", np.asarray([1.0, 2.0], dtype=np.float32))]
            )

        co = np.empty(6, dtype=np.float32)
        cloud.points.foreach_get("co", co)
        np.testing.assert_array_equal(co, [1, 2, 3, 4, 5, 6])

    def test_a_malformed_position_array_leaves_the_cloud_untouched(self):
        cloud = bpy.data.pointclouds.new("points-shape")
        cloud.resize(1)
        cloud.points.foreach_set("co", np.asarray([9, 9, 9], dtype=np.float32))
        cloud.points.update()

        with self.assertRaisesRegex(
            geometry_io.GeometryIOError, "pts.positions must be N x 3"
        ):
            geometry_io.sections_to_points(
                cloud,
                [("pts.positions", "f32", np.asarray([0, 0, 0, 1], dtype=np.float32))],
            )

        co = np.empty(3, dtype=np.float32)
        cloud.points.foreach_get("co", co)
        np.testing.assert_array_equal(co, [9, 9, 9])

    def test_a_radii_count_mismatch_is_classified(self):
        cloud = bpy.data.pointclouds.new("points-radii")
        cloud.resize(1)
        cloud.points.foreach_set("co", np.asarray([0, 0, 0, 1, 0, 0], dtype=np.float32))
        # Known gap, reported in docs/seams-0.8.md: the classified error is
        # raised after the cloud has been resized and its positions written.
        # This pins the error, not the partial write; an atomic validation in
        # T4 is an improvement, not a regression.
        with self.assertRaisesRegex(
            geometry_io.GeometryIOError, "pts.radii has 1 entries for 2 points"
        ):
            geometry_io.sections_to_points(
                cloud,
                [
                    ("pts.positions", "f32", np.asarray(
                        [0, 0, 0, 1, 0, 0], dtype=np.float32
                    )),
                    ("pts.radii", "f32", np.asarray([1.0], dtype=np.float32)),
                ],
            )


class CurveReadLossTests(unittest.TestCase):
    def setUp(self):
        bpy.ops.wm.read_factory_settings(use_empty=True)

    def test_short_and_spanless_splines_are_counted(self):
        curve = bpy.data.curves.new("lossy", "CURVE")
        curve.dimensions = "3D"
        curve.splines.new("POLY")  # one point, no span
        curve.splines.new("BEZIER")  # one control point, no span
        valid = curve.splines.new("POLY")
        valid.points.add(1)
        valid.points[0].co = (0, 0, 0, 1)
        valid.points[1].co = (1, 0, 0, 1)

        notes = []
        sections = geometry_io.curve_to_sections(curve, notes=notes)
        by_name = {name: values for name, _dtype, values in sections}

        np.testing.assert_array_equal(by_name["crv.counts"], [2])
        self.assertEqual(len(notes), 2, notes)
        self.assertTrue(any("Bezier spline had no span" in note for note in notes), notes)
        self.assertTrue(any("fewer than two points" in note for note in notes), notes)

    def test_an_absent_curve_group_is_a_classified_error_not_a_clear(self):
        target = bpy.data.curves.new("kept", "CURVE")
        spline = target.splines.new("POLY")
        spline.points.add(1)

        with self.assertRaisesRegex(
            geometry_io.GeometryIOError, "curve sections need crv.points"
        ):
            geometry_io.sections_to_curve(target, [])

        self.assertEqual(len(target.splines), 1, "an absent group cleared the curve")


class ObjectDispatchTests(unittest.TestCase):
    def setUp(self):
        bpy.ops.wm.read_factory_settings(use_empty=True)

    def test_a_surface_is_meshed_with_a_note(self):
        curve = bpy.data.curves.new("surface", "SURFACE")
        curve.dimensions = "3D"
        spline = curve.splines.new("NURBS")
        spline.points.add(3)
        for index, point in enumerate(
            [(0, 0, 0, 1), (1, 0, 0, 1), (1, 1, 0, 1), (0, 1, 0, 1)]
        ):
            spline.points[index].co = point
        spline.order_u = 2
        spline.order_v = 2
        spline.use_endpoint_u = True
        spline.use_endpoint_v = True
        obj = bpy.data.objects.new("surface", curve)
        bpy.context.scene.collection.objects.link(obj)
        notes = []

        sections, _attributes = geometry_io.object_to_sections(obj, notes=notes)

        names = [name for name, _dtype, _values in sections]
        self.assertTrue(names and names[0].startswith("mesh."), names)
        self.assertTrue(
            any("surface was meshed" in note for note in notes), notes
        )

    def test_empty_geometry_is_no_sections_and_no_warning(self):
        mesh = bpy.data.meshes.new("empty-mesh")
        mesh_obj = bpy.data.objects.new("empty-mesh", mesh)
        bpy.context.scene.collection.objects.link(mesh_obj)
        cloud = bpy.data.pointclouds.new("empty-cloud")
        cloud_obj = bpy.data.objects.new("empty-cloud", cloud)
        bpy.context.scene.collection.objects.link(cloud_obj)
        empty = bpy.data.objects.new("empty", None)
        bpy.context.scene.collection.objects.link(empty)

        for obj in (mesh_obj, cloud_obj):
            with self.subTest(object=obj.name):
                notes = []
                self.assertEqual(geometry_io.object_to_sections(obj, notes=notes), ([], []))
                self.assertEqual(notes, [], "empty geometry was reported as a loss")

        notes = []
        self.assertEqual(geometry_io.object_to_sections(empty, notes=notes), ([], []))
        self.assertEqual(len(notes), 1)
        self.assertIn("EMPTY object has no wire geometry", notes[0])


if __name__ == "__main__":
    unittest.main()
