"""Layer B, T10a: deterministic randomized round trips and malformed state.

These are property tests, not copies of the hand-picked cases in
``test_geometry_io*``, ``test_attributes`` and ``test_instances``. Every test
uses a fixed seed and bounded sizes so a failure is reproducible from the seed
alone:

    FUZZ_SEED = 20261118

Covered:
- random tri/quad/n-gon meshes (including empty and isolated vertices) through
  ``mesh_to_sections``/``sections_to_mesh`` and both topology-token paths;
- loose edges and isolated vertices through the wire ``crv``/``pts`` path, with
  the expected crease loss surfaced as a note;
- all supported generic attribute types/domains, prune/absent behaviour and
  malformed wire values (notes, never a crash);
- nested/multi-part instance definitions, rows, transforms, cycles, empty parts
  and attributes, built directly and from the existing Geometry Nodes builders;
- malformed/frozen/bake-marker state and stale records: safe recovery, never a
  crash.
"""

import math
import random
import unittest
from unittest import mock

import bpy
import numpy as np

from harness import isolate_data_root
from stageloom.blender import geometry_io
from stageloom.blender.geometry_io import attributes as geometry_attributes
from stageloom.blender.geometry_io import mesh as geometry_mesh
from stageloom.core import attributes as attr_mod
from stageloom.core import codec

FUZZ_SEED = 20261118
MESH_CASES = 80
ATTRIBUTE_CASES = 40
MALFORMED_ATTRIBUTE_CASES = 60
INSTANCE_CASES = 20
SERIALIZER_CASES = 8
BAKE_CASES = 80

TOKEN_A = "sha256:" + "a" * 64
TOKEN_B = "sha256:" + "b" * 64


def _new_mesh(name, verts, faces=(), edges=()):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(
        [tuple(float(v) for v in vertex) for vertex in verts],
        [tuple(int(v) for v in edge) for edge in edges],
        [tuple(int(v) for v in face) for face in faces],
    )
    mesh.update()
    return mesh


def _quad(name, offset=(0.0, 0.0, 0.0)):
    ox, oy, oz = offset
    return _new_mesh(
        name,
        [(ox, oy, oz), (ox + 1.0, oy, oz), (ox + 1.0, oy + 1.0, oz), (ox, oy + 1.0, oz)],
        [(0, 1, 2, 3)],
    )


def _mesh_object(name, mesh):
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def _random_mesh(rng, name, allow_loose=False):
    """A bounded random mesh: mixed faces, sometimes empty or vertex-only."""
    n_verts = rng.randrange(0, 19)
    verts = [
        (rng.uniform(-5.0, 5.0), rng.uniform(-5.0, 5.0), rng.uniform(-5.0, 5.0))
        for _ in range(n_verts)
    ]
    faces = []
    if n_verts >= 3:
        for _ in range(rng.randrange(0, 6)):
            size = rng.choice([3, 3, 4, 4, 5, 6, min(7, n_verts)])
            size = min(size, n_verts)
            if size < 3:
                continue
            face = tuple(rng.sample(range(n_verts), size))
            if face not in faces:
                faces.append(face)
    edges = []
    if allow_loose:
        for _ in range(rng.randrange(0, 5)):
            if n_verts < 2:
                break
            a, b = rng.sample(range(n_verts), 2)
            pair = (min(a, b), max(a, b))
            if pair not in edges:
                edges.append(pair)
    return _new_mesh(name, verts, faces, edges)


def _mesh_snapshot(mesh):
    coordinates = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", coordinates)
    loops = np.empty(len(mesh.loops), dtype=np.uint32)
    mesh.loops.foreach_get("vertex_index", loops)
    totals = np.empty(len(mesh.polygons), dtype=np.uint32)
    mesh.polygons.foreach_get("loop_total", totals)
    edge_vertices = np.empty(len(mesh.edges) * 2, dtype=np.uint32)
    mesh.edges.foreach_get("vertices", edge_vertices)
    pairs = {
        (min(int(a), int(b)), max(int(a), int(b)))
        for a, b in edge_vertices.reshape(-1, 2)
    }
    return {
        "co": coordinates.reshape(-1, 3),
        "loops": loops,
        "totals": totals,
        "pairs": pairs,
    }


def _assert_mesh_matches(test, source, target):
    left = _mesh_snapshot(source)
    right = _mesh_snapshot(target)
    np.testing.assert_allclose(right["co"], left["co"], rtol=0, atol=1e-6)
    np.testing.assert_array_equal(right["loops"], left["loops"])
    np.testing.assert_array_equal(right["totals"], left["totals"])
    test.assertEqual(right["pairs"], left["pairs"])
    test.assertEqual(len(target.polygons), len(source.polygons))


def _color_bytes(attr):
    values = np.empty(len(attr.data) * 4, dtype=np.float32)
    if attr.data_type == "BYTE_COLOR":
        attr.data.foreach_get("color_srgb", values)
    else:
        attr.data.foreach_get("color", values)
        rgba = values.reshape(-1, 4)
        rgba[:, :3] = np.clip(rgba[:, :3], 0.0, 1.0)
    return np.clip(np.rint(values * 255.0), 0.0, 255.0).astype(np.uint8).reshape(-1, 4)


def _add_random_color_and_crease(rng, mesh):
    expected_color = None
    if len(mesh.vertices):
        data_type = rng.choice(["BYTE_COLOR", "FLOAT_COLOR"])
        domain = rng.choice(["POINT", "CORNER"])
        count = len(mesh.vertices) if domain == "POINT" else len(mesh.loops)
        attr = mesh.color_attributes.new("FuzzColor", data_type, domain)
        if data_type == "BYTE_COLOR":
            raw = np.array(
                [[rng.randrange(256) for _ in range(4)] for _ in range(count)],
                dtype=np.float32,
            ) / 255.0
            attr.data.foreach_set("color_srgb", raw.reshape(-1))
        else:
            raw = np.array(
                [[rng.random() for _ in range(4)] for _ in range(count)],
                dtype=np.float32,
            )
            attr.data.foreach_set("color", raw.reshape(-1))
        mesh.color_attributes.active_color = attr
        expected_color = geometry_io.read_colors(mesh)

    expected_creases = None
    if len(mesh.edges):
        attr = mesh.attributes.new(geometry_io.CREASE_ATTRIBUTE, "FLOAT", "EDGE")
        values = np.array([rng.choice([0.0, rng.random()]) for _ in range(len(mesh.edges))],
                          dtype=np.float32)
        attr.data.foreach_set("value", values)
        expected_creases = geometry_io.read_creases(mesh)
    mesh.update()
    return expected_color, expected_creases


def _crease_map(mesh):
    creases = geometry_io.read_creases(mesh)
    if creases is None:
        return {}
    edges, values = creases
    return {
        (min(int(a), int(b)), max(int(a), int(b))): float(value)
        for (a, b), value in zip(edges, values)
    }


class MeshFuzzTests(unittest.TestCase):
    def setUp(self):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        self.rng = random.Random(FUZZ_SEED + 1)

    def test_random_face_meshes_round_trip_with_colors_and_creases(self):
        for case in range(MESH_CASES):
            source = _random_mesh(self.rng, "fuzz-mesh-%d" % case)
            expected_color, expected_creases = _add_random_color_and_crease(
                self.rng, source
            )
            sections = geometry_io.mesh_to_sections(source)
            target = bpy.data.meshes.new("fuzz-target-%d" % case)

            with self.subTest(case=case):
                geometry_io.sections_to_mesh(target, sections)
                _assert_mesh_matches(self, source, target)

                if expected_color is not None:
                    got = geometry_io.read_colors(target)
                    self.assertIsNotNone(got)
                    np.testing.assert_array_equal(got, expected_color)
                else:
                    self.assertIsNone(geometry_io.read_colors(target))

                expected = {} if expected_creases is None else _crease_map(source)
                got = _crease_map(target)
                self.assertEqual(set(got), set(expected))
                for edge, value in expected.items():
                    self.assertAlmostEqual(got[edge], value, places=5)

    def test_the_token_paths_match_the_verified_topology_path(self):
        for case in range(MESH_CASES):
            source = _random_mesh(self.rng, "fuzz-token-source-%d" % case)
            sections = geometry_io.mesh_to_sections(source)
            by_name = {name: values for name, _dtype, values in sections}
            target = bpy.data.meshes.new("fuzz-token-target-%d" % case)
            geometry_io.sections_to_mesh(target, sections)

            with self.subTest(case=case):
                moved = np.asarray(by_name["mesh.verts"], dtype=np.float32).reshape(-1, 3)
                moved = moved + np.float32(2.0)
                with mock.patch.object(
                    geometry_mesh,
                    "_topology_matches",
                    side_effect=AssertionError("the token path read the topology"),
                ):
                    geometry_io.sections_to_mesh(
                        target,
                        [("mesh.verts", "f32", moved)]
                        + [(name, dtype, values) for name, dtype, values in sections
                           if name != "mesh.verts"],
                        topology=TOKEN_A,
                        previous=TOKEN_A,
                    )
                np.testing.assert_allclose(_mesh_snapshot(target)["co"], moved,
                                           rtol=0, atol=1e-6)

                # A changed token proves a rebuild even without reading loops.
                flipped = np.asarray(by_name["mesh.face_indices"], dtype=np.uint32).copy()
                if len(flipped) >= 3:
                    flipped[:3] = flipped[[1, 0, 2]]
                with mock.patch.object(
                    geometry_mesh,
                    "_topology_matches",
                    side_effect=AssertionError("a changed token still read the topology"),
                ):
                    geometry_io.sections_to_mesh(
                        target,
                        [
                            ("mesh.verts", "f32", moved),
                            ("mesh.face_counts", "u32", by_name["mesh.face_counts"]),
                            ("mesh.face_indices", "u32", flipped),
                        ],
                        topology=TOKEN_B,
                        previous=TOKEN_A,
                    )
                np.testing.assert_array_equal(_mesh_snapshot(target)["loops"], flipped)

    def test_wire_meshes_round_trip_through_curves_and_points(self):
        for case in range(MESH_CASES):
            source = _random_mesh(self.rng, "fuzz-wire-%d" % case, allow_loose=True)
            source_edges = np.empty(len(source.edges) * 2, dtype=np.uint32)
            source.edges.foreach_get("vertices", source_edges)
            if len(source.polygons):
                # This test is the wire path; skip face meshes.
                continue
            crease = source.attributes.new(geometry_io.CREASE_ATTRIBUTE, "FLOAT", "EDGE")
            if len(source.edges):
                values = np.full(len(source.edges), 0.75, dtype=np.float32)
                crease.data.foreach_set("value", values)
            source.update()
            obj = _mesh_object("fuzz-wire-obj-%d" % case, source)
            notes = []
            sections, _attributes = geometry_io.object_to_sections(obj, notes=notes)
            by_name = {name: values for name, _dtype, values in sections}

            with self.subTest(case=case):
                loose = geometry_io.loose_edge_indices(source)
                isolated = geometry_io.isolated_vertex_indices(source)
                if len(loose):
                    self.assertIn("crv.points", by_name)
                    np.testing.assert_array_equal(by_name["crv.counts"],
                                                  np.full(len(loose), 2, dtype=np.uint32))
                    expected_points = np.empty((len(loose) * 2, 3), dtype=np.float32)
                    edge_vertices = source_edges.reshape(-1, 2)[loose].reshape(-1)
                    co = np.empty(len(source.vertices) * 3, dtype=np.float32)
                    source.vertices.foreach_get("co", co)
                    expected_points[:] = co.reshape(-1, 3)[edge_vertices]
                    np.testing.assert_allclose(by_name["crv.points"][:, :3],
                                               expected_points, rtol=0, atol=1e-6)

                    curve_sections = [
                        (name, dtype, values) for name, dtype, values in sections
                        if name.startswith("crv.")
                    ]
                    curve = bpy.data.curves.new("fuzz-curve-%d" % case, "CURVE")
                    curve.dimensions = "3D"
                    geometry_io.sections_to_curve(curve, curve_sections)
                    self.assertEqual(len(curve.splines), len(loose))
                    for index, spline in enumerate(curve.splines):
                        self.assertEqual(spline.type, "POLY")
                        self.assertEqual(len(spline.points), 2)
                        for point_index in (0, 1):
                            np.testing.assert_allclose(
                                spline.points[point_index].co[:3],
                                expected_points[2 * index + point_index],
                                rtol=0,
                                atol=1e-5,
                            )
                    bpy.data.curves.remove(curve)
                else:
                    self.assertNotIn("crv.points", by_name)

                if len(isolated):
                    self.assertIn("pts.positions", by_name)
                    co = np.empty(len(source.vertices) * 3, dtype=np.float32)
                    source.vertices.foreach_get("co", co)
                    np.testing.assert_allclose(
                        by_name["pts.positions"], co.reshape(-1, 3)[isolated],
                        rtol=0, atol=1e-6,
                    )
                    point_sections = [
                        (name, dtype, values) for name, dtype, values in sections
                        if name.startswith("pts.")
                    ]
                    cloud = bpy.data.pointclouds.new("fuzz-cloud-%d" % case)
                    geometry_io.sections_to_points(cloud, point_sections)
                    self.assertEqual(len(cloud.points), len(isolated))
                    got = np.empty(len(cloud.points) * 3, dtype=np.float32)
                    cloud.points.foreach_get("co", got)
                    np.testing.assert_allclose(
                        got.reshape(-1, 3), co.reshape(-1, 3)[isolated],
                        rtol=0, atol=1e-6,
                    )
                    bpy.data.pointclouds.remove(cloud)
                else:
                    self.assertNotIn("pts.positions", by_name)

                if len(source.edges) and not len(source.polygons):
                    self.assertTrue(
                        any("crease" in note and "wire" in note for note in notes),
                        "a wire-mesh crease loss was silent: %r" % (notes,),
                    )


class AttributeFuzzTests(unittest.TestCase):
    def setUp(self):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        self.rng = random.Random(FUZZ_SEED + 2)

    @staticmethod
    def _add_native_color(mesh):
        # The stage-owned channel carries the protocol's fixed name, which is
        # what the prune path knows to keep.
        attr = mesh.color_attributes.new(
            geometry_io.COLOR_ATTRIBUTE, "BYTE_COLOR", "POINT"
        )
        values = np.zeros((len(mesh.vertices), 4), dtype=np.float32)
        values[:, 3] = 1.0
        attr.data.foreach_set("color_srgb", values.reshape(-1))
        mesh.color_attributes.active_color = attr

    def _add_random_attribute(self, rng, mesh, index):
        type_name = rng.choice(sorted(attr_mod.WIRE_TYPES))
        domain = rng.choice(["POINT", "EDGE", "FACE", "CORNER"])
        counts = {
            "POINT": len(mesh.vertices),
            "EDGE": len(mesh.edges),
            "FACE": len(mesh.polygons),
            "CORNER": len(mesh.loops),
        }
        count = counts[domain]
        if count == 0:
            return None
        name = "fuzz_%d_%s" % (index, type_name)
        comps = attr_mod.components(type_name)
        if type_name == "STRING":
            values = ["piece-%d-%d" % (index, i) for i in range(count)]
        elif type_name == "BOOLEAN":
            values = [bool(rng.randrange(2)) for _ in range(count * comps)]
        elif type_name == "BYTE_COLOR":
            values = [rng.randrange(256) for _ in range(count * comps)]
        elif type_name in ("INT", "INT8", "INT16_2D", "INT32_2D"):
            values = [rng.randrange(-100, 101) for _ in range(count * comps)]
        else:
            values = [rng.uniform(-10.0, 10.0) for _ in range(count * comps)]
        try:
            attr = mesh.attributes.new(name, type_name, domain)
        except (RuntimeError, TypeError):
            return None
        if type_name == "STRING":
            for i, value in enumerate(values):
                attr.data[i].value = value.encode("ascii")
        elif type_name == "BOOLEAN":
            attr.data.foreach_set("value", np.asarray(values, dtype=np.bool_))
        elif type_name == "BYTE_COLOR":
            attr.data.foreach_set(
                "color_srgb", np.asarray(values, dtype=np.float32) / 255.0
            )
        elif type_name in ("INT", "INT8", "INT16_2D", "INT32_2D"):
            dtype = np.int8 if type_name == "INT8" else np.int32
            attr.data.foreach_set("value", np.asarray(values, dtype=dtype))
        elif type_name in ("FLOAT_VECTOR", "FLOAT2", "FLOAT4"):
            attr.data.foreach_set("vector", np.asarray(values, dtype=np.float32))
        elif type_name == "FLOAT_COLOR":
            attr.data.foreach_set("color", np.asarray(values, dtype=np.float32))
        else:
            attr.data.foreach_set("value", np.asarray(values, dtype=np.float32))
        return name

    def test_random_attributes_round_trip_across_types_and_domains(self):
        source = _quad("fuzz-attr-source")
        self._add_native_color(source)
        names = []
        for index in range(6):
            name = self._add_random_attribute(self.rng, source, index)
            if name is not None:
                names.append(name)
        source.update()
        self.assertTrue(names, "the generator produced no attribute at all")

        sections, descriptors = geometry_io.mesh_attributes(source)
        chosen = [entry for entry in descriptors if entry["name"] in names]
        self.assertEqual({entry["name"] for entry in chosen}, set(names))
        section_by_name = {name: values for name, _dtype, values in sections}

        target = _quad("fuzz-attr-target")
        entries = []
        expected = {}
        for descriptor in chosen:
            attr = source.attributes[descriptor["name"]]
            expected[descriptor["name"]] = geometry_attributes._attribute_values(attr)
            offsets = None
            if descriptor.get("offsets"):
                offsets = section_by_name[descriptor["offsets"]]
            entries.append((descriptor, section_by_name[descriptor["section"]], offsets))
        notes = geometry_io.write_attributes(target, entries)
        self.assertEqual(notes, [])

        for name in names:
            attr = target.attributes.get(name)
            self.assertIsNotNone(attr, name)
            got = geometry_attributes._attribute_values(attr)
            want = expected[name]
            if isinstance(want, list):
                self.assertEqual(got, want)
            elif np.issubdtype(np.asarray(want).dtype, np.floating):
                np.testing.assert_allclose(got, want, rtol=0, atol=1e-6)
            else:
                np.testing.assert_array_equal(got, want)

    def test_malformed_wire_attributes_are_noted_not_crashed(self):
        for case in range(MALFORMED_ATTRIBUTE_CASES):
            target = _quad("fuzz-malformed-%d" % case)
            descriptor = {
                "group": "mesh",
                "name": "bad_%d" % case,
                "domain": self.rng.choice(["POINT", "EDGE", "FACE", "CORNER",
                                           "CURVE", "NOPE"]),
                "type": self.rng.choice(["FLOAT", "STRING", "MYSTERY", "BYTE_COLOR"]),
            }
            domain_counts = {
                "POINT": len(target.vertices),
                "EDGE": len(target.edges),
                "FACE": len(target.polygons),
                "CORNER": len(target.loops),
            }
            target_count = domain_counts.get(descriptor["domain"], 0)
            if descriptor["type"] == "STRING":
                values = np.frombuffer(b"abcdef", dtype=np.uint8)
                offsets = self.rng.choice(
                    [None,
                     np.asarray([2, 2], dtype=np.uint32),
                     np.asarray([1, 2, 9], dtype=np.uint32),
                     np.asarray([4], dtype=np.uint32),
                     np.asarray([-1, 3], dtype=np.int32)]
                )
            elif descriptor["type"] == "BYTE_COLOR":
                values = np.arange(target_count * 4 + 4, dtype=np.uint8)
                offsets = None
            else:
                # A count that never matches the target domain, so this case is
                # malformed by construction rather than by a lucky draw.
                values = np.arange(target_count + 1, dtype=np.float32)
                offsets = None
            good = attr_mod.descriptor("mesh", "good_%d" % case, "POINT", "FLOAT", 4, 0)
            notes = geometry_io.write_attributes(
                target,
                [
                    (descriptor, values, offsets),
                    (good, np.asarray([1.0, 2.0, 3.0, 4.0], dtype=np.float32), None),
                ],
            )
            with self.subTest(case=case, type=descriptor["type"],
                              domain=descriptor["domain"]):
                self.assertTrue(notes, "a malformed attribute was silently accepted")
                self.assertIsNotNone(target.attributes.get(good["name"]))
                self.assertIsNone(target.attributes.get(descriptor["name"]))

    def test_prune_removes_only_random_generic_attributes(self):
        mesh = _quad("fuzz-prune")
        self._add_native_color(mesh)
        names = []
        for index in range(5):
            name = self._add_random_attribute(self.rng, mesh, index)
            if name is not None:
                names.append(name)
        mesh.attributes.new("fuzz_manual", "FLOAT", "POINT")
        crease = mesh.attributes.new(geometry_io.CREASE_ATTRIBUTE, "FLOAT", "EDGE")
        mesh.update()

        notes = geometry_io.write_attributes(mesh, [], prune=True)
        self.assertEqual(notes, [])
        for name in names:
            self.assertIsNone(mesh.attributes.get(name), name)
        self.assertIsNone(mesh.attributes.get("fuzz_manual"))
        self.assertIsNotNone(mesh.color_attributes.get(geometry_io.COLOR_ATTRIBUTE))
        self.assertIsNotNone(mesh.attributes.get(geometry_io.CREASE_ATTRIBUTE))
        self.assertIsNotNone(crease)


def _wire_transform(rng, translation=None):
    if translation is None:
        translation = (rng.uniform(-8.0, 8.0), rng.uniform(-8.0, 8.0),
                       rng.uniform(-8.0, 8.0))
    angle = rng.uniform(-math.pi, math.pi)
    scale = rng.uniform(0.5, 2.0)
    matrix = np.eye(4, dtype=np.float32)
    matrix[0, 0] = math.cos(angle) * scale
    matrix[0, 1] = -math.sin(angle) * scale
    matrix[1, 0] = math.sin(angle) * scale
    matrix[1, 1] = math.cos(angle) * scale
    matrix[0, 3], matrix[1, 3], matrix[2, 3] = translation
    return matrix.reshape(-1)


def _dtype_for(name):
    if name.endswith((".face_counts", ".face_indices", ".ref_def", ".counts",
                      ".orders", ".knots_mode", ".cyclic", ".types")):
        return "u32" if name.endswith((".face_counts", ".face_indices", ".ref_def", ".counts")) else "u8"
    if name.endswith((".colors",)):
        return "u8"
    return "f32"


def _quad_wire(prefix, offset):
    ox, oy, oz = offset
    verts = np.array(
        [[ox, oy, oz], [ox + 1, oy, oz], [ox + 1, oy + 1, oz], [ox, oy + 1, oz]],
        dtype=np.float32,
    )
    return {
        prefix + "mesh.verts": verts,
        prefix + "mesh.face_counts": np.array([4], dtype=np.uint32),
        prefix + "mesh.face_indices": np.array([0, 1, 2, 3], dtype=np.uint32),
    }


def _random_definition_frame(rng):
    """A valid wire-level instance frame with random parts/rows/attributes/cycles."""
    n_defs = rng.randint(1, 2)
    definitions = []
    sections = {}
    attributes = []
    generated_geometry = {}
    offset = 0
    for definition in range(n_defs):
        parts = rng.choice([1, 1, 2])
        entry = {"name": "FuzzBlock%d" % definition}
        if parts > 1:
            entry["parts"] = parts
        definitions.append(entry)
        for part in range(parts):
            prefix = (
                "idef.%d." % definition
                if parts == 1
                else "idef.%d.%d." % (definition, part)
            )
            empty = rng.random() < 0.2
            generated_geometry[(definition, part)] = not empty
            if empty:
                continue
            sections.update(_quad_wire(prefix, (offset, 0.0, 0.0)))
            offset += 3.0
            if rng.random() < 0.6:
                value = np.array([rng.uniform(-5, 5) for _ in range(4)], dtype=np.float32)
                section = prefix + "attr.%d.values" % (len(generated_geometry))
                sections[section] = value
                descriptor = {
                    "group": "mesh",
                    "name": "fuzz_load_%d_%d" % (definition, part),
                    "domain": "POINT",
                    "type": "FLOAT",
                    "components": 1,
                    "count": 4,
                    "section": section,
                    "definition": definition,
                }
                if parts > 1:
                    descriptor["part"] = part
                attributes.append(descriptor)

    nested = {}
    for definition in range(n_defs):
        if rng.random() < 0.6:
            rows = []
            refs = []
            for _ in range(rng.randint(1, 2)):
                refs.append(rng.randrange(n_defs))
                rows.append(_wire_transform(rng))
            nested[definition] = rows
            sections["idef.%d.inst.ref_def" % definition] = np.array(
                refs, dtype=np.uint32
            )
            sections["idef.%d.inst.transforms" % definition] = np.concatenate(rows)

    top_rows = []
    if rng.random() < 0.85:
        count = rng.randint(1, 3)
        top_rows = [_wire_transform(rng) for _ in range(count)]
        sections["inst.ref_def"] = np.array(
            [rng.randrange(n_defs) for _ in range(count)], dtype=np.uint32
        )
        sections["inst.transforms"] = np.concatenate(top_rows)

    if rng.random() < 0.35:
        sections.update(_quad_wire("", (-10.0, 0.0, 0.0)))
        sections["attr.base.values"] = np.array(
            [rng.uniform(-5, 5) for _ in range(4)], dtype=np.float32
        )
        attributes.append({
            "group": "mesh",
            "name": "fuzz_base",
            "domain": "POINT",
            "type": "FLOAT",
            "components": 1,
            "count": 4,
            "section": "attr.base.values",
        })

    header = {
        "type": "response",
        "v": 2,
        "stage": "fuzz",
        "definitions": definitions,
        "attributes": attributes,
    }
    return header, sections, definitions, attributes, generated_geometry, nested


class InstanceFuzzTests(unittest.TestCase):
    def setUp(self):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="stageloom")
        self.rng = random.Random(FUZZ_SEED + 3)
        bpy.ops.mesh.primitive_cube_add(size=2.0)
        self.obj = bpy.context.object
        self.obj.name = "FuzzBlocks"
        bpy.ops.stageloom.add_stage(label="Fuzz blocks")
        self.stage = self.obj.gh_stack.stages[0]
        from stageloom.blender import helpers

        self.helpers = helpers
        from stageloom.blender.engine import apply as engine_apply

        self.engine_apply = engine_apply

    def test_random_definition_frames_apply_without_crash(self):
        for case in range(INSTANCE_CASES):
            (header, sections, definitions, attributes,
             generated_geometry, nested) = _random_definition_frame(self.rng)
            encoded = [
                (name, _dtype_for(name), values) for name, values in sections.items()
            ]
            frame = codec.encode_frame(header, encoded)
            decoded_header, decoded = codec.decode_frame(frame)

            with self.subTest(case=case):
                self.assertEqual(decoded_header["definitions"], definitions)
                applied = self.engine_apply._apply(
                    self.obj, self.stage, decoded,
                    decoded_header.get("attributes"),
                    decoded_header.get("definitions"),
                )
                self.assertIsNone(applied)

                collections = self.helpers.definition_collections(
                    self.obj, self.stage.key
                )
                self.assertEqual(sorted(collections), list(range(len(definitions))))
                for (definition, part), has_geometry in generated_geometry.items():
                    parts = definitions[definition].get("parts", 1)
                    name = self.helpers.definition_name(
                        self.obj, self.stage.key, definition, "mesh",
                        part=part if parts > 1 else None,
                    )
                    helper = bpy.data.objects.get(name)
                    self.assertIsNotNone(helper, name)
                    self.assertEqual(len(helper.data.vertices), 4 if has_geometry else 0)

                top_clouds = self.helpers.instance_clouds(self.obj, self.stage.key)
                expected_top = {}
                for target in sections.get("inst.ref_def", []):
                    expected_top[int(target)] = expected_top.get(int(target), 0) + 1
                self.assertEqual(
                    {index: len(cloud.data.points) for index, cloud in top_clouds.items()},
                    expected_top,
                )

                nested_clouds = self.helpers.nested_clouds(self.obj, self.stage.key)
                expected_nested = {
                    index: len(rows) for index, rows in nested.items() if rows
                }
                self.assertEqual(
                    {index: len(cloud.data.points)
                     for index, cloud in nested_clouds.items()},
                    expected_nested,
                )
                for index, rows in nested.items():
                    if not rows:
                        continue
                    cloud = nested_clouds[index]
                    child = cloud.data.attributes.get("child")
                    self.assertIsNotNone(child)
                    got = [element.value for element in child.data]
                    self.assertEqual(len(got), len(rows))

                for descriptor in attributes:
                    definition = descriptor.get("definition")
                    if definition is None:
                        helper = self.helpers.helper_for(
                            self.obj, self.stage.key, "mesh"
                        )
                    else:
                        parts = definitions[definition].get("parts", 1)
                        name = self.helpers.definition_name(
                            self.obj, self.stage.key, definition, "mesh",
                            part=descriptor.get("part") if parts > 1 else None,
                        )
                        helper = bpy.data.objects.get(name)
                    self.assertIsNotNone(helper)
                    attr = helper.data.attributes.get(descriptor["name"])
                    self.assertIsNotNone(attr, descriptor["name"])
                    values = np.empty(descriptor["count"], dtype=np.float32)
                    attr.data.foreach_get("value", values)
                    np.testing.assert_allclose(
                        values,
                        np.asarray(sections[descriptor["section"]], dtype=np.float32),
                        rtol=0, atol=1e-6,
                    )

    def test_malformed_definition_frames_are_classified(self):
        for case in range(INSTANCE_CASES * 2):
            definitions, sections = self._malformed_case(case)
            with self.subTest(case=case):
                with self.assertRaises(geometry_io.GeometryIOError):
                    self.engine_apply._apply(
                        self.obj, self.stage, sections, None, definitions
                    )

    def _malformed_case(self, case):
        definitions = [{"name": "Block"}]
        sections = _quad_wire("idef.0.", (0, 0, 0))
        kind = case % 8
        if kind == 0:
            definitions[0]["parts"] = 0
        elif kind == 1:
            definitions[0]["parts"] = True
        elif kind == 2:
            definitions[0]["parts"] = 4097
        elif kind == 3:
            sections = _quad_wire("idef.0.2.", (0, 0, 0))
            definitions[0]["parts"] = 2
        elif kind == 4:
            sections = _quad_wire("idef.1.", (0, 0, 0))
        elif kind == 5:
            sections["idef.x.mesh.verts"] = np.zeros((4, 3), dtype=np.float32)
        elif kind == 6:
            sections["inst.ref_def"] = np.array([1], dtype=np.uint32)
            sections["inst.transforms"] = _wire_transform(self.rng)
        else:
            definitions = [{"name": "Parts", "parts": 2}]
            sections = _quad_wire("idef.0.0.", (0, 0, 0))
            sections["idef.0.1.inst.ref_def"] = np.array([0], dtype=np.uint32)
            sections["idef.0.1.inst.transforms"] = _wire_transform(self.rng)
        return definitions, sections

    def test_existing_builders_serialize_a_random_multipart_scene(self):
        from test_instances import _collection, _instance_holder, _points, _serialize

        for case in range(SERIALIZER_CASES):
            first = _mesh_object(
                "fuzz-serial-%d-a" % case,
                _quad("fuzz-serial-%d-a-mesh" % case),
            )
            second = _mesh_object(
                "fuzz-serial-%d-b" % case,
                _quad("fuzz-serial-%d-b-mesh" % case),
            )
            second.data.vertices.foreach_set(
                "co",
                np.asarray(
                    [(5, 0, 0), (6, 0, 0), (6, 1, 0), (5, 1, 0)], dtype=np.float32
                ).reshape(-1),
            )
            second.data.update()
            if self.rng.random() < 0.7:
                load = first.data.attributes.new("load", "FLOAT", "POINT")
                load.data.foreach_set(
                    "value",
                    np.asarray([self.rng.uniform(-5, 5) for _ in range(4)],
                               dtype=np.float32),
                )
            collection = _collection("FuzzSerialColl%d" % case, first, second)
            count = self.rng.randint(1, 3)
            points = _points(
                "FuzzSerialPts%d" % case,
                [(self.rng.uniform(-5, 5), self.rng.uniform(-5, 5), 0.0)
                 for _ in range(count)],
                rotations=[(1, 0, 0, 0) for _ in range(count)],
                scales=[(1, 1, 1) for _ in range(count)],
            )
            holder = _instance_holder(
                "FuzzSerialHolder%d" % case, points, collection,
                separate_children=True,
            )
            sections, attributes, definitions = _serialize(holder)

            with self.subTest(case=case):
                names = [name for name, _dtype, _values in sections]
                self.assertEqual(len(names), len(set(names)), "duplicate section name")
                self.assertEqual(definitions[0]["parts"], 2)
                by_name = {name: values for name, _dtype, values in sections}
                self.assertEqual(set(by_name["inst.ref_def"]), {0})
                self.assertEqual(len(by_name["inst.transforms"]), count * 16)
                for definition, entry in enumerate(definitions):
                    prefix = "idef.%d." % definition
                    for name in names:
                        if not name.startswith(prefix):
                            continue
                        pieces = name[len(prefix):].split(".")
                        if entry.get("parts", 1) > 1:
                            self.assertTrue(pieces[0].isdigit(), name)
                            self.assertLess(int(pieces[0]), entry["parts"])
                self.assertTrue(any(name.startswith("idef.0.0.mesh.verts") for name in names))
                self.assertTrue(any(name.startswith("idef.0.1.mesh.verts") for name in names))
                if attributes:
                    for descriptor in attributes:
                        self.assertIn(descriptor["section"], by_name)
                        if descriptor.get("offsets"):
                            self.assertIn(descriptor["offsets"], by_name)


class BakeStateFuzzTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="stageloom")
        cls.enterClassContext(isolate_data_root("stageloom-fuzz-bake-"))
        from stageloom.blender.engine import bake as bake_mod
        from stageloom.blender import ops

        cls.bake = bake_mod
        cls.ops = ops
        bpy.ops.mesh.primitive_cube_add(size=2.0)
        cls.obj = bpy.context.object
        cls.obj.name = "FuzzBake"
        bpy.ops.stageloom.add_stage(label="Fuzz bake")
        cls.stage = cls.obj.gh_stack.stages[0]

    @classmethod
    def tearDownClass(cls):
        bpy.ops.preferences.addon_disable(module="stageloom")

    def setUp(self):
        self.rng = random.Random(FUZZ_SEED + 4)
        stage = self.stage
        stage.baked = False
        stage.bake_token = ""
        stage.frozen = False
        stage.frozen_by_bake = ""
        stage.enabled = True

    def _token(self):
        return self.rng.choice([
            "", "stale", "another-bake", "\x00token", "caf\u00e9",
            "x" * self.rng.randint(1, 240),
        ])

    def test_release_token_only_unfreezes_the_stages_that_token_froze(self):
        for case in range(BAKE_CASES):
            self.setUp()
            token = self._token() or "non-empty-token"
            marker = self._token()
            self.stage.baked = bool(self.rng.randrange(2))
            self.stage.frozen = bool(self.rng.randrange(2))
            self.stage.frozen_by_bake = marker
            before_frozen = self.stage.frozen
            before_baked = self.stage.baked

            with self.subTest(case=case, token=token, marker=marker):
                released = self.bake.release_token(token)
                self.assertGreaterEqual(released, 0)
                if marker == token:
                    self.assertEqual(self.stage.frozen_by_bake, "")
                    if before_baked:
                        self.assertTrue(self.stage.frozen,
                                        "a stage with its own bake was unfrozen")
                    else:
                        self.assertFalse(self.stage.frozen,
                                         "a marker match did not unfreeze")
                else:
                    self.assertEqual(self.stage.frozen_by_bake, marker)
                    self.assertEqual(self.stage.frozen, before_frozen)

    def test_a_manual_freeze_and_an_empty_marker_are_never_released(self):
        self.stage.frozen = True
        self.stage.frozen_by_bake = ""
        self.assertEqual(self.bake.release_token(""), 0)
        self.assertTrue(self.stage.frozen)
        self.assertEqual(self.bake.release_token("anything"), 0)
        self.assertTrue(self.stage.frozen)
        self.assertEqual(self.stage.frozen_by_bake, "")

    def test_stale_deleted_records_are_skipped(self):
        token = "stale-" + str(self.rng.randrange(1000))
        self.stage.frozen_by_bake = token
        self.stage.frozen = True
        bpy.ops.mesh.primitive_cube_add(size=1.0)
        doomed = bpy.context.object
        doomed.name = "FuzzBakeDoomed"
        bpy.ops.stageloom.add_stage(label="Doomed")
        doomed_stage = doomed.gh_stack.stages[0]
        doomed_stage.frozen_by_bake = token
        doomed_stage.frozen = True
        from stageloom.blender import helpers

        doomed_helpers = {
            helper.name
            for kind in helpers.KINDS
            for helper in (helpers.helper_for(doomed, doomed_stage.key, kind),)
            if helper is not None
        }
        self.assertTrue(doomed_helpers, "the doomed stage created no helpers")
        bpy.data.objects.remove(doomed, do_unlink=True)

        released = self.bake.release_token(token)
        self.assertEqual(released, 1)
        self.assertFalse(self.stage.frozen)
        self.assertEqual(self.stage.frozen_by_bake, "")
        for name in doomed_helpers:
            self.assertIsNone(
                bpy.data.objects.get(name),
                "releasing the live token left orphan helper %s" % name,
            )

    def test_malformed_bake_markers_clear_safely(self):
        self.stage.baked = True
        self.stage.frozen = True
        self.stage.bake_token = "malformed-\x00-" + "z" * 120
        self.stage.frozen_by_bake = "another-token"
        operator = type("FuzzOperator", (), {
            "index": 0,
            "report": lambda self, level, message: None,
        })()
        result = self.ops.STAGELOOM_OT_clear_bake.execute(operator, bpy.context)
        self.assertEqual(result, {"FINISHED"})
        self.assertFalse(self.stage.baked)
        self.assertEqual(self.stage.bake_token, "")
        self.assertFalse(self.stage.frozen)
        # The marker names another bake: clearing this stage's own (malformed)
        # token must not clobber that other bake's ownership proof.
        self.assertEqual(self.stage.frozen_by_bake, "another-token")


if __name__ == "__main__":
    unittest.main()
