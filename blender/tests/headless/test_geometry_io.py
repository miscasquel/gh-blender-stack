"""Layer B, geometry half: the section extraction and apply path.

Run with:

    scripts/test-blender

which is ``blender -b --factory-startup --python-exit-code 1 --python
blender/tests/headless/run.py``. Asserts are real: a failure sets the exit code.
"""

import time
import unittest

import bpy
import numpy as np

from blender.stageloom.blender import geometry_io  # noqa: E402
from blender.stageloom.core import codec  # noqa: E402


def new_mesh_object(name, verts, faces):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([tuple(v) for v in verts], [], faces)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def grid_verts_faces(nx, ny, size=10.0):
    """An (nx-1) x (ny-1) quad grid, so (nx-1)*(ny-1)*2 triangles."""
    xs = np.linspace(0.0, size, nx, dtype=np.float32)
    ys = np.linspace(0.0, size, ny, dtype=np.float32)
    gx, gy = np.meshgrid(xs, ys, indexing="ij")
    verts = np.stack([gx.reshape(-1), gy.reshape(-1), np.zeros(nx * ny, dtype=np.float32)], axis=1)
    faces = []
    for i in range(nx - 1):
        for j in range(ny - 1):
            a = i * ny + j
            b = a + 1
            c = a + ny
            d = c + 1
            faces.append((a, c, d, b))
    return verts, faces


def sections_dict(sections):
    return {name: values for name, dtype, values in sections}


def _linear_to_srgb(values):
    linear = np.clip(np.asarray(values, dtype=np.float32), 0.0, 1.0)
    return np.where(
        linear <= 0.0031308,
        linear * 12.92,
        1.055 * np.power(linear, 1.0 / 2.4) - 0.055,
    )


class SectionRoundTripTests(unittest.TestCase):
    def setUp(self):
        # A clean file per test: remove everything the previous test made.
        bpy.ops.wm.read_factory_settings(use_empty=True)

    def test_quad_and_ngon(self):
        verts = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (2, 0, 0), (2, 1, 0)]
        faces = [(0, 1, 2, 3), (4, 5, 3, 1, 0)]  # a quad and an n-gon
        obj = new_mesh_object("ngon", verts, faces)
        sections = geometry_io.mesh_to_sections(obj.data)
        names = [s[0] for s in sections]
        self.assertEqual(names[:3], ["mesh.verts", "mesh.face_counts", "mesh.face_indices"])
        by = sections_dict(sections)
        np.testing.assert_array_equal(by["mesh.face_counts"], [4, 5])
        self.assertEqual(len(by["mesh.face_indices"]), 9)
        np.testing.assert_allclose(by["mesh.verts"][3], [0.0, 1.0, 0.0])

    def test_sections_to_mesh_rebuilds_and_matches(self):
        verts, faces = grid_verts_faces(4, 4)
        obj = new_mesh_object("src", verts, faces)
        sections = geometry_io.mesh_to_sections(obj.data)

        target = bpy.data.meshes.new("dst")
        geometry_io.sections_to_mesh(target, sections)

        by = sections_dict(sections)
        self.assertEqual(len(target.vertices), len(by["mesh.verts"]))
        self.assertEqual(len(target.polygons), len(by["mesh.face_counts"]))
        co = np.empty(len(target.vertices) * 3, dtype=np.float32)
        target.vertices.foreach_get("co", co)
        np.testing.assert_allclose(co.reshape(-1, 3), by["mesh.verts"], rtol=0, atol=1e-6)

    def test_sections_to_mesh_in_place_position_update(self):
        verts, faces = grid_verts_faces(5, 5)
        obj = new_mesh_object("inplace", verts, faces)
        sections = geometry_io.mesh_to_sections(obj.data)
        by = sections_dict(sections)
        moved = by["mesh.verts"] + np.float32(2.0)
        before_polys = len(obj.data.polygons)
        geometry_io.sections_to_mesh(obj.data, [("mesh.verts", "f32", moved)] + [
            (n, d, v) for n, d, v in sections if n != "mesh.verts"
        ])
        self.assertEqual(len(obj.data.polygons), before_polys)
        co = np.empty(len(obj.data.vertices) * 3, dtype=np.float32)
        obj.data.vertices.foreach_get("co", co)
        np.testing.assert_allclose(co.reshape(-1, 3), moved, rtol=0, atol=1e-6)

    def test_topology_change_with_equal_loop_count_rebuilds_faces(self):
        """[4,4] -> [3,5] keeps six vertices and eight loops; a length-only
        check would leave the old faces in place."""
        verts = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (2, 0, 0), (2, 1, 0)]
        target = new_mesh_object(
            "topology-target", verts, [(0, 1, 2, 3), (1, 2, 3, 4)]
        )
        sections = [
            ("mesh.verts", "f32", np.asarray(verts, dtype=np.float32)),
            ("mesh.face_counts", "u32", np.array([3, 5], dtype=np.uint32)),
            ("mesh.face_indices", "u32", np.array([0, 1, 2, 0, 1, 2, 3, 4], dtype=np.uint32)),
        ]
        geometry_io.sections_to_mesh(target.data, sections)

        counts = np.empty(len(target.data.polygons), dtype=np.uint32)
        target.data.polygons.foreach_get("loop_total", counts)
        np.testing.assert_array_equal(counts, [3, 5])
        indices = np.empty(len(target.data.loops), dtype=np.uint32)
        target.data.loops.foreach_get("vertex_index", indices)
        np.testing.assert_array_equal(indices, [0, 1, 2, 0, 1, 2, 3, 4])

    def test_quad_diagonal_flip_with_identical_counts_rebuilds_faces(self):
        verts = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (2, 0, 0), (2, 1, 0)]
        target = new_mesh_object(
            "flip-target", verts, [(0, 1, 2, 3), (1, 2, 3, 4)]
        )
        flipped = np.array([0, 1, 3, 2, 1, 2, 4, 3], dtype=np.uint32)
        sections = [
            ("mesh.verts", "f32", np.asarray(verts, dtype=np.float32)),
            ("mesh.face_counts", "u32", np.array([4, 4], dtype=np.uint32)),
            ("mesh.face_indices", "u32", flipped),
        ]
        geometry_io.sections_to_mesh(target.data, sections)

        indices = np.empty(len(target.data.loops), dtype=np.uint32)
        target.data.loops.foreach_get("vertex_index", indices)
        np.testing.assert_array_equal(indices, flipped)

    def test_identical_topology_takes_the_position_only_fast_path(self):
        verts, faces = grid_verts_faces(4, 4)
        obj = new_mesh_object("fast-path", verts, faces)
        sections = geometry_io.mesh_to_sections(obj.data)
        by = sections_dict(sections)
        moved = by["mesh.verts"] + np.float32(1.0)

        original_faces_from = geometry_io._faces_from

        def must_not_rebuild(*args):
            raise AssertionError("a true topology match rebuilt the mesh")

        geometry_io._faces_from = must_not_rebuild
        try:
            geometry_io.sections_to_mesh(
                obj.data,
                [("mesh.verts", "f32", moved)]
                + [(n, d, v) for n, d, v in sections if n != "mesh.verts"],
            )
        finally:
            geometry_io._faces_from = original_faces_from

        co = np.empty(len(obj.data.vertices) * 3, dtype=np.float32)
        obj.data.vertices.foreach_get("co", co)
        np.testing.assert_allclose(co.reshape(-1, 3), moved, rtol=0, atol=1e-6)

    def test_mesh_and_subd_groups_are_selected_as_units(self):
        verts = np.asarray(
            [(0, 0, 0), (1, 0, 0), (0, 1, 0), (1, 1, 0)], dtype=np.float32
        )
        mesh_sections = [
            ("mesh.verts", "f32", verts),
            ("mesh.face_counts", "u32", np.array([3], dtype=np.uint32)),
            ("mesh.face_indices", "u32", np.array([0, 1, 2], dtype=np.uint32)),
        ]
        subd_sections = [
            ("subd.verts", "f32", verts + np.float32(5.0)),
            ("subd.face_counts", "u32", np.array([4], dtype=np.uint32)),
            ("subd.face_indices", "u32", np.array([0, 1, 3, 2], dtype=np.uint32)),
        ]

        mesh_only = bpy.data.meshes.new("mesh-only")
        geometry_io.sections_to_mesh(mesh_only, mesh_sections)
        self.assertEqual(len(mesh_only.polygons), 1)
        self.assertEqual(mesh_only.polygons[0].loop_total, 3)

        subd_only = bpy.data.meshes.new("subd-only")
        geometry_io.sections_to_mesh(subd_only, subd_sections)
        self.assertEqual(subd_only.polygons[0].loop_total, 4)
        np.testing.assert_allclose(
            [subd_only.vertices[0].co[:]], [[5.0, 5.0, 5.0]], rtol=0, atol=1e-6
        )

        both = bpy.data.meshes.new("both")
        geometry_io.sections_to_mesh(both, mesh_sections + subd_sections)
        self.assertEqual(both.polygons[0].loop_total, 3, "mesh must win when both are complete")
        np.testing.assert_allclose(
            [both.vertices[0].co[:]], [[0.0, 0.0, 0.0]], rtol=0, atol=1e-6
        )

        for sections in (
            mesh_sections + subd_sections[:1],
            mesh_sections[:1] + subd_sections,
        ):
            with self.subTest(sections=[name for name, _dtype, _values in sections]):
                with self.assertRaises(geometry_io.GeometryIOError):
                    geometry_io.sections_to_mesh(bpy.data.meshes.new("partial"), sections)

    def test_unmatched_crease_edges_are_noted(self):
        mesh = bpy.data.meshes.new("crease-note")
        mesh.from_pydata(
            [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [], [(0, 1, 2, 3)]
        )
        mesh.update()
        notes = []
        geometry_io.write_creases(
            mesh,
            (
                np.array([[0, 99], [0, 1]], dtype=np.uint32),
                np.array([1.0, 0.5], dtype=np.float32),
            ),
            notes=notes,
        )
        self.assertEqual(len(notes), 1)
        self.assertIn("1 crease edge", notes[0])

    def test_colors_round_trip(self):
        verts, faces = grid_verts_faces(3, 3)
        obj = new_mesh_object("colors", verts, faces)
        mesh = obj.data
        attr = mesh.color_attributes.new("VCol", "BYTE_COLOR", "POINT")
        raw = (np.arange(len(mesh.vertices) * 4, dtype=np.float32).reshape(-1, 4) * 3.0)
        raw = np.clip(raw, 0, 255).astype(np.uint8)
        # color_srgb writes floats 0..1; the store quantises to bytes.
        attr.data.foreach_set("color_srgb", raw.reshape(-1).astype(np.float32) / 255.0)
        sections = geometry_io.mesh_to_sections(mesh)
        by = sections_dict(sections)
        self.assertIn("mesh.colors", by)
        np.testing.assert_array_equal(by["mesh.colors"], raw)

        target = bpy.data.meshes.new("dst-colors")
        geometry_io.sections_to_mesh(target, sections)
        got = target.color_attributes[geometry_io.COLOR_ATTRIBUTE].data
        back_f = np.empty(len(target.vertices) * 4, dtype=np.float32)
        got.foreach_get("color_srgb", back_f)
        back = np.clip(np.rint(back_f * 255.0), 0.0, 255.0).astype(np.uint8).reshape(-1, 4)
        np.testing.assert_array_equal(back, raw)

    def test_float_color_native_reads_scene_linear_as_srgb(self):
        verts, faces = grid_verts_faces(3, 3)
        obj = new_mesh_object("float-colors", verts, faces)
        mesh = obj.data
        attr = mesh.color_attributes.new("FCol", "FLOAT_COLOR", "POINT")
        linear = np.array(
            [
                [0.0, 0.0, 0.0, 0.25],
                [0.5, 0.5, 0.5, 0.5],
                [1.0, 0.25, 0.75, 1.0],
                [0.0031308, 0.01, 0.9, 0.75],
            ]
            * 4,
            dtype=np.float32,
        )[: len(mesh.vertices)]
        attr.data.foreach_set("color", linear.reshape(-1))
        mesh.color_attributes.active_color = attr

        sections = geometry_io.mesh_to_sections(mesh)
        by = sections_dict(sections)
        expected = np.clip(
            np.rint(_linear_to_srgb(linear[:, :3]) * 255.0), 0.0, 255.0
        ).astype(np.uint8)
        got = by["mesh.colors"]
        self.assertLessEqual(
            int(np.max(np.abs(got[:, :3].astype(np.int16) - expected.astype(np.int16)))),
            1,
        )
        np.testing.assert_array_equal(got[:, 3], np.rint(linear[:, 3] * 255.0))

        target = bpy.data.meshes.new("dst-float-colors")
        geometry_io.sections_to_mesh(target, sections)
        back_attr = target.color_attributes[geometry_io.COLOR_ATTRIBUTE]
        self.assertEqual(back_attr.data_type, "BYTE_COLOR")
        back_f = np.empty(len(target.vertices) * 4, dtype=np.float32)
        back_attr.data.foreach_get("color_srgb", back_f)
        back = np.clip(np.rint(back_f * 255.0), 0.0, 255.0).astype(np.uint8).reshape(-1, 4)
        np.testing.assert_array_equal(back, got)

    def test_byte_color_native_stays_exact(self):
        verts, faces = grid_verts_faces(3, 3)
        obj = new_mesh_object("byte-colors", verts, faces)
        mesh = obj.data
        attr = mesh.color_attributes.new("BCol", "BYTE_COLOR", "POINT")
        raw = np.arange(len(mesh.vertices) * 4, dtype=np.uint32).reshape(-1, 4) * 17
        raw = np.clip(raw, 0, 255).astype(np.uint8)
        # A byte colour's color_srgb is already the wire's sRGB value.
        attr.data.foreach_set("color_srgb", raw.reshape(-1).astype(np.float32) / 255.0)
        mesh.color_attributes.active_color = attr

        by = sections_dict(geometry_io.mesh_to_sections(mesh))
        np.testing.assert_array_equal(by["mesh.colors"], raw)

    def test_creases_round_trip(self):
        verts, faces = grid_verts_faces(3, 3)
        obj = new_mesh_object("creases", verts, faces)
        mesh = obj.data
        attr = mesh.attributes.new("crease_edge", "FLOAT", "EDGE")
        values = np.linspace(0.0, 1.0, len(mesh.edges), dtype=np.float32)
        attr.data.foreach_set("value", values)

        sections = geometry_io.mesh_to_sections(mesh)
        by = sections_dict(sections)
        self.assertIn("mesh.crease_edges", by)
        expected_edges = int((values > 0).sum())
        self.assertEqual(len(by["mesh.crease_edges"]), expected_edges)
        # Fractional values pass through.
        np.testing.assert_allclose(
            by["mesh.crease_values"], values[values > 0], rtol=0, atol=1e-6
        )

        target = bpy.data.meshes.new("dst-creases")
        geometry_io.sections_to_mesh(target, sections)
        back = np.empty(len(target.edges), dtype=np.float32)
        target.attributes["crease_edge"].data.foreach_get("value", back)
        # Rebuild order can differ, so compare the set of creased edges as pairs.
        edge_verts = np.empty(len(target.edges) * 2, dtype=np.uint32)
        target.edges.foreach_get("vertices", edge_verts)
        edge_verts = edge_verts.reshape(-1, 2)
        original = {}
        src_edges = np.empty(len(mesh.edges) * 2, dtype=np.uint32)
        mesh.edges.foreach_get("vertices", src_edges)
        for i, (a, b) in enumerate(src_edges.reshape(-1, 2)):
            if values[i] > 0:
                original[(min(a, b), max(a, b))] = values[i]
        for i, (a, b) in enumerate(edge_verts):
            key = (min(int(a), int(b)), max(int(a), int(b)))
            if key in original:
                self.assertAlmostEqual(float(back[i]), float(original[key]), places=5)
            else:
                self.assertEqual(float(back[i]), 0.0)

    def test_subd_names_and_no_colors(self):
        verts, faces = grid_verts_faces(3, 3)
        obj = new_mesh_object("subd", verts, faces)
        mesh = obj.data
        attr = mesh.color_attributes.new("VCol", "BYTE_COLOR", "POINT")
        attr.data.foreach_set("color_srgb", np.zeros(len(mesh.vertices) * 4, dtype=np.uint8))
        sections = geometry_io.mesh_to_sections(mesh, as_subd=True)
        names = [s[0] for s in sections]
        self.assertTrue(all(n.startswith("subd.") for n in names), names)
        self.assertNotIn("subd.colors", names)

    def test_frame_round_trip(self):
        verts, faces = grid_verts_faces(6, 6)
        obj = new_mesh_object("frame", verts, faces)
        sections = geometry_io.mesh_to_sections(obj.data)
        frame = codec.encode_frame({"type": "request", "stage": "k"}, sections)
        header, decoded = codec.decode_frame(frame)
        by = sections_dict(sections)
        np.testing.assert_array_equal(decoded["mesh.face_counts"], by["mesh.face_counts"])
        np.testing.assert_allclose(
            np.array(decoded["mesh.verts"]).reshape(-1, 3),
            by["mesh.verts"],
            rtol=0,
            atol=1e-6,
        )


class PerformanceTests(unittest.TestCase):
    """The measured Blender-side numbers: extraction, frame encode, apply."""

    def setUp(self):
        bpy.ops.wm.read_factory_settings(use_empty=True)

    def _measure(self, nx, ny, budget_ms):
        verts, faces = grid_verts_faces(nx, ny)
        obj = new_mesh_object("perf", verts, faces)
        mesh = obj.data
        tris = (nx - 1) * (ny - 1) * 2

        t0 = time.perf_counter()
        sections = geometry_io.mesh_to_sections(mesh)
        t1 = time.perf_counter()
        frame = codec.encode_frame({"type": "request"}, sections)
        t2 = time.perf_counter()
        header, decoded = codec.decode_frame(frame)
        target = bpy.data.meshes.new("perf-dst")
        geometry_io.sections_to_mesh(target, sections)
        t3 = time.perf_counter()

        extract_ms = (t1 - t0) * 1000.0
        encode_ms = (t2 - t1) * 1000.0
        decode_ms = 0.0  # decode measured below
        t3a = time.perf_counter()
        codec.decode_frame(frame)
        decode_ms = (time.perf_counter() - t3a) * 1000.0
        apply_ms = (t3 - t2) * 1000.0

        print(
            "[geometry] %6d tris: extract+hash %.1f ms, encode %.1f ms, decode %.1f ms, "
            "apply %.1f ms, frame %.2f MB"
            % (tris, extract_ms, encode_ms, decode_ms, apply_ms, len(frame) / 1e6)
        )
        self.assertLess(extract_ms + encode_ms, budget_ms, "extraction over budget")
        self.assertLess(apply_ms, budget_ms, "apply over budget")
        return frame

    def test_10k_triangles(self):
        frame = self._measure(72, 72, 1000)
        self.assertTrue(0.1e6 < len(frame) < 1e6)

    def test_100k_triangles(self):
        frame = self._measure(225, 225, 4000)
        self.assertTrue(1e6 < len(frame) < 5e6)


if __name__ == "__main__":
    unittest.main()
