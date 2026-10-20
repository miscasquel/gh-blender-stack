"""Layer B, attributes: the wire format, the helper write path, and the engine
round trip through fake_gh.

Run with:

    scripts/test-blender

The first class exercises geometry_io directly (every supported Blender type and
domain, both directions, the count-mismatch guard and the native-channel
exclusions). The second drives a real stage against fake_gh, which echoes the
attributes it received, so one pass proves both directions: Blender sent them
and Blender accepted them back.
"""

import json
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
from blender.gh_stack.core import attributes as attr_mod  # noqa: E402

PORT = 9150


def quad_mesh(name="attr-quad"):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [], [(0, 1, 2, 3)])
    mesh.update()
    return mesh


class AttributeIOTests(unittest.TestCase):
    def setUp(self):
        bpy.ops.wm.read_factory_settings(use_empty=True)

    def test_round_trip_every_supported_type(self):
        source = quad_mesh("source")
        expected = {}

        # The first colour attribute becomes the active one and travels through
        # mesh.colors; a second one is what the generic path carries.
        native = source.color_attributes.new("Col", "BYTE_COLOR", "CORNER")
        for i, element in enumerate(native.data):
            element.color_srgb = (1.0, 0.0, 0.0, 1.0) if i == 0 else (0.0, 0.0, 0.0, 1.0)
        source.color_attributes.active_color = native

        def add(name, dtype, domain, values):
            attr = source.attributes.new(name, dtype, domain)
            if dtype == "STRING":
                for i, value in enumerate(values):
                    attr.data[i].value = value
            else:
                buf = np.asarray(values, dtype=_buffer_dtype(dtype))
                prop = "vector" if dtype in ("FLOAT_VECTOR", "FLOAT2", "FLOAT4") else (
                    "color" if dtype == "FLOAT_COLOR" else (
                        "color_srgb" if dtype == "BYTE_COLOR" else "value"))
                if dtype == "BYTE_COLOR":
                    attr.data.foreach_set(prop, buf.astype(np.float32) / 255.0)
                else:
                    attr.data.foreach_set(prop, buf)
            expected[name] = (dtype, domain)

        add("weight", "FLOAT", "POINT", [0.5, 1.5, 2.5, 3.5])
        add("panel_id", "INT", "FACE", [7])
        add("flagged", "BOOLEAN", "EDGE", [True, False, True, False])
        add("offset", "FLOAT_VECTOR", "POINT", [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12])
        add("tint", "FLOAT_COLOR", "POINT", [1, 0, 0, 1, 0, 1, 0, 1, 0, 0, 1, 1, 1, 1, 1, 1])
        add("corner_uv", "FLOAT2", "CORNER", [0, 0, 1, 0, 1, 1, 0, 1])
        add("corner_rgba", "BYTE_COLOR", "CORNER", [255, 0, 0, 255, 0, 255, 0, 255,
                                                    0, 0, 255, 255, 255, 255, 0, 255])
        add("spin", "QUATERNION", "POINT", [1, 0, 0, 0] * 4)
        add("matrix", "FLOAT4X4", "POINT", [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1] * 4)
        add("small", "INT8", "POINT", [1, 2, 3, 4])
        add("pair", "INT32_2D", "FACE", [3, 4])
        add("grain", "INT16_2D", "EDGE", [1, 2] * 4)
        add("part", "STRING", "POINT", [b"north", b"east", b"south", b"west"])

        sections, descriptors = geometry_io.mesh_attributes(source)
        # Blender's own flat/smooth shading attribute rides along; it is not
        # part of what this test pins.
        descriptors = [d for d in descriptors if d["name"] != "sharp_face"]
        self.assertEqual(len(descriptors), len(expected))
        by_name = {d["name"]: d for d in descriptors}
        for name, (dtype, domain) in expected.items():
            self.assertIn(name, by_name)
            self.assertEqual(by_name[name]["type"], dtype, name)
            self.assertEqual(by_name[name]["domain"], domain, name)
            self.assertEqual([s[0] for s in sections if s[0] == by_name[name]["section"]],
                             [by_name[name]["section"]])

        # Every section the descriptors name exists, with the declared dtype.
        section_by_name = {s[0]: s for s in sections}
        for descriptor in descriptors:
            section = section_by_name[descriptor["section"]]
            self.assertEqual(section[1], attr_mod.wire_dtype(descriptor["type"]))
            if descriptor.get("offsets"):
                self.assertEqual(section_by_name[descriptor["offsets"]][1], "u32")

        # Write onto a fresh mesh of the same topology and read it back.
        target = quad_mesh("target")
        entries = []
        for descriptor in descriptors:
            values = section_by_name[descriptor["section"]][2]
            offsets = None
            if descriptor.get("offsets"):
                offsets = section_by_name[descriptor["offsets"]][2]
            entries.append((descriptor, values, offsets))
        notes = geometry_io.write_attributes(target, entries)
        self.assertEqual(notes, [])

        for name, (dtype, domain) in expected.items():
            attr = target.attributes.get(name)
            self.assertIsNotNone(attr, name)
            self.assertEqual(attr.data_type, dtype, name)
            self.assertEqual(attr.domain, domain, name)

        self.assertEqual(list(target.attributes["panel_id"].data.values())[0].value, 7)
        self.assertEqual(list(target.attributes["part"].data.values())[3].value, b"west")
        self.assertAlmostEqual(target.attributes["weight"].data[1].value, 1.5, places=6)
        self.assertTrue(target.attributes["flagged"].data[2].value)
        self.assertEqual(
            tuple(round(v, 5) for v in target.attributes["offset"].data[1].vector), (4.0, 5.0, 6.0)
        )

    def test_a_wrong_count_is_dropped_with_a_note(self):
        target = quad_mesh("target")
        descriptor = attr_mod.descriptor("mesh", "too_many", "FACE", "INT", 3, 0)
        notes = geometry_io.write_attributes(
            target, [(descriptor, np.asarray([1, 2, 3], dtype=np.int32), None)]
        )
        self.assertEqual(len(notes), 1)
        self.assertIn("too_many", notes[0])
        self.assertIsNone(target.attributes.get("too_many"))

    def test_native_channels_travel_once(self):
        """The active colour attribute and the crease attribute go through
        mesh.colors / the crease sections, never as generic attributes."""
        mesh = quad_mesh("native")
        colors = mesh.color_attributes.new("Col", "BYTE_COLOR", "POINT")
        colors.data[0].color_srgb = (1.0, 0.0, 0.0, 1.0)
        crease = mesh.attributes.new("crease_edge", "FLOAT", "EDGE")
        crease.data[0].value = 1.0

        mesh.update()
        _sections, descriptors = geometry_io.mesh_attributes(mesh)
        names = [d["name"] for d in descriptors]
        self.assertNotIn("Col", names)
        self.assertNotIn("crease_edge", names)

    def test_a_second_color_attribute_travels_generically(self):
        mesh = quad_mesh("extra-color")
        mesh.color_attributes.new("Col", "BYTE_COLOR", "POINT")
        extra = mesh.color_attributes.new("glow", "FLOAT_COLOR", "POINT")
        extra.data[2].color = (1.0, 0.5, 0.0, 1.0)
        mesh.color_attributes.active_color = mesh.color_attributes["Col"]
        mesh.update()

        _sections, descriptors = geometry_io.mesh_attributes(mesh)
        names = [d["name"] for d in descriptors if d["name"] != "sharp_face"]
        self.assertEqual(names, ["glow"])
        self.assertEqual(next(d for d in descriptors if d["name"] == "glow")["type"], "FLOAT_COLOR")

    def test_every_wire_type_has_a_foreach_spec(self):
        """The wire vocabulary and the Blender foreach table must not drift.

        STRING is the one type with no foreach path: it is read and written
        element by element in geometry_io, so it is intentionally absent from
        ``_FOREACH``.
        """
        self.assertEqual(set(geometry_io._FOREACH) | {"STRING"}, set(attr_mod.WIRE_TYPES))


def _buffer_dtype(dtype):
    return {
        "FLOAT": np.float32, "INT": np.int32, "BOOLEAN": np.bool_,
        "FLOAT_VECTOR": np.float32, "FLOAT2": np.float32, "FLOAT4": np.float32,
        "FLOAT_COLOR": np.float32, "BYTE_COLOR": np.uint8,
        "QUATERNION": np.float32, "FLOAT4X4": np.float32, "INT8": np.int8,
        "INT16_2D": np.int32, "INT32_2D": np.int32,
    }[dtype]


class AttributeEngineTests(unittest.TestCase):
    """One stage against fake_gh: attributes leave Blender and come back."""

    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="gh_stack")
        bpy.context.preferences.addons["gh_stack"].preferences.data_root = tempfile.mkdtemp(
            prefix="gh-stack-attr-test-"
        )
        import gh_stack.blender.engine as engine
        import gh_stack.blender.helpers as helpers

        cls.engine = engine
        cls.helpers = helpers
        cls.report_path = os.path.join(tempfile.gettempdir(), "fake_gh_attr_report.json")
        if os.path.exists(cls.report_path):
            os.remove(cls.report_path)

        bpy.ops.mesh.primitive_cube_add(size=1.0)
        cls.obj = bpy.context.object
        cls.obj.name = "Attributed"
        mesh = cls.obj.data
        panel = mesh.attributes.new("panel_id", "INT", "FACE")
        for i, element in enumerate(panel.data):
            element.value = i + 1
        weight = mesh.attributes.new("load", "FLOAT", "POINT")
        for i, element in enumerate(weight.data):
            element.value = 0.25 * (i + 1)
        part = mesh.attributes.new("part", "STRING", "POINT")
        for i, element in enumerate(part.data):
            element.value = b"piece-%d" % i
        flag = mesh.attributes.new("is_glass", "BOOLEAN", "FACE")
        flag.data[0].value = True

        engine.start_server(PORT)
        time.sleep(0.5)

        bpy.ops.gh_stack.add_stage(label="Attributes")
        cls.stage = cls.obj.gh_stack.stages[0]

        cls.fake = subprocess.Popen(
            [
                sys.executable,
                os.path.join(REPO, "tools", "fake_gh", "fake_gh.py"),
                "--port", str(PORT),
                "--stage", cls.stage.key,
                "--report", cls.report_path,
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
        cls._pump(until=lambda: cls.stage.state in ("ok", "error"))
        cls.engine.push(cls.obj)
        cls._pump(until=lambda: cls.stage.state == "ok" and cls._applied())

    @classmethod
    def _applied(cls):
        helper = cls.helpers.helper_for(cls.obj, cls.stage.key, "mesh")
        return helper is not None and helper.data.attributes.get("part") is not None

    @classmethod
    def _pump(cls, seconds=10.0, until=None):
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

    def test_01_the_request_carried_the_attributes(self):
        report = json.load(open(self.report_path))
        received = report.get("received_attributes", [])
        self.assertIn("panel_id/FACE/INT", received)
        self.assertIn("load/POINT/FLOAT", received)
        self.assertIn("part/POINT/STRING", received)
        self.assertIn("is_glass/FACE/BOOLEAN", received)

    def test_02_the_response_wrote_them_back(self):
        helper = self.helpers.helper_for(self.obj, self.stage.key, "mesh")
        mesh = helper.data
        self.assertEqual(self.stage.state, "ok", self.stage.error)

        panel = mesh.attributes["panel_id"]
        self.assertEqual(panel.domain, "FACE")
        self.assertEqual(panel.data_type, "INT")
        self.assertEqual([element.value for element in panel.data], [1, 2, 3, 4, 5, 6])

        load = mesh.attributes["load"]
        self.assertEqual(len(load.data), len(mesh.vertices))
        self.assertAlmostEqual(load.data[0].value, 0.25, places=6)

        part = mesh.attributes["part"]
        self.assertEqual(part.data[1].value, b"piece-1")

        glass = mesh.attributes["is_glass"]
        self.assertTrue(glass.data[0].value)

    def test_03_an_attribute_value_change_is_new_geometry(self):
        """Attribute values are part of the geometry hash: editing one makes the
        stage request again instead of hitting the cache."""
        import gh_stack.blender.engine as engine

        mesh = quad_mesh("hash-quad")
        attr = mesh.attributes.new("v", "FLOAT", "POINT")
        sections, _descriptors = geometry_io.mesh_attributes(mesh)
        before = engine._sections_hash(sections)
        attr.data[0].value = 1.0
        sections, _descriptors = geometry_io.mesh_attributes(mesh)
        self.assertNotEqual(before, engine._sections_hash(sections))

    def test_04_float_noise_is_not_a_prefix_change(self):
        """Blender evaluates modifier-generated attribute data with last-bit
        float variation. Two reads that differ only that much are the same
        prefix; anything more is a real edit."""
        import gh_stack.blender.engine as engine
        from blender.gh_stack.core import attributes as attr_mod

        vertices = np.array((0.0, 0.0, 0.0), dtype=np.float32).reshape(-1, 3)
        descriptors = [attr_mod.descriptor("mesh", "load", "POINT", "FLOAT", 2, 0)]
        a = [("mesh.verts", "f32", vertices), ("attr.0.values", "f32", np.array((0.25, 0.5), dtype=np.float32))]

        # Last-bit variation on one attribute value: the same prefix.
        noisy = [
            ("mesh.verts", "f32", vertices),
            ("attr.0.values", "f32", np.array((0.25, 0.50000006), dtype=np.float32)),
        ]
        self.assertTrue(engine._same_prefix(a, descriptors, [], noisy, descriptors, []))

        # A user-sized edit: a real change.
        edited = [
            ("mesh.verts", "f32", vertices),
            ("attr.0.values", "f32", np.array((0.25, 0.5001), dtype=np.float32)),
        ]
        self.assertFalse(engine._same_prefix(a, descriptors, [], edited, descriptors, []))

        # Positions are never toleranced.
        moved = [
            ("mesh.verts", "f32", np.array((0.0, 0.0, 1e-6), dtype=np.float32).reshape(-1, 3)),
            ("attr.0.values", "f32", np.array((0.25, 0.5), dtype=np.float32)),
        ]
        self.assertFalse(engine._same_prefix(a, descriptors, [], moved, descriptors, []))
