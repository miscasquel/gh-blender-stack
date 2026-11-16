"""Layer B: the additive ``geom.topology`` token (protocol §4.4)."""

import unittest
from unittest import mock

import bpy
import numpy as np

from harness import isolate_data_root

from blender.stageloom.core import attributes as attr_mod  # noqa: E402

TOKEN_A = "sha256:" + "a" * 64
TOKEN_B = "sha256:" + "b" * 64
_NO_GEOM = object()
DEFINITIONS = [{"name": "Block"}]


def quad_sections(z=0.0, flipped=False, values=None):
    """One quad, optionally moved/flipped, plus one optional FLOAT attribute."""
    verts = np.array(
        [[0.0, 0.0, z], [1.0, 0.0, z], [1.0, 1.0, z], [0.0, 1.0, z]],
        dtype=np.float32,
    )
    indices = [0, 1, 3, 2] if flipped else [0, 1, 2, 3]
    sections = {
        "mesh.verts": verts,
        "mesh.face_counts": np.array([4], dtype=np.uint32),
        "mesh.face_indices": np.array(indices, dtype=np.uint32),
    }
    if values is not None:
        sections["attr.0.values"] = np.asarray(values, dtype=np.float32)
    return sections


def definition_sections(z=0.0, flipped=False, values=None):
    """The same quad as one definition part (``idef.0.*``)."""
    sections = {
        "idef.0." + name: values
        for name, values in quad_sections(z, flipped, values=None).items()
    }
    if values is not None:
        sections["idef.0.attr.0.values"] = np.asarray(values, dtype=np.float32)
    return sections


class TopologyTokenApplyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="stageloom")
        cls.enterClassContext(isolate_data_root("stageloom-token-"))
        import stageloom.blender.engine as engine
        import stageloom.blender.geometry_io as geometry_io_module
        import stageloom.blender.helpers as helpers
        import stageloom.blender.stack as stack_mod

        cls.engine = engine
        cls.geometry_io = geometry_io_module
        cls.helpers = helpers
        cls.stack_mod = stack_mod

        bpy.ops.mesh.primitive_cube_add(size=2.0)
        cls.obj = bpy.context.object
        cls.obj.name = "Token"
        bpy.ops.stageloom.add_stage(label="Token")
        cls.stage = cls.obj.gh_stack.stages[0]

    @classmethod
    def tearDownClass(cls):
        bpy.ops.preferences.addon_disable(module="stageloom")

    def setUp(self):
        self.engine._pending.clear()
        self.engine._sent.clear()
        self.engine._verify.clear()
        self.stage.enabled = True
        self.stage.frozen = False
        self.stage.rid = ""
        self.stage.applied_out_hash = ""
        self.stage.applied_topology = ""
        self.stage.error = ""
        self.helper().data.clear_geometry()
        self.definition_helper().data.clear_geometry()

    def helper(self):
        return self.helpers.ensure_helper(self.obj, self.stage.key, "mesh")

    def definition_helper(self):
        _collection, parts = self.helpers.ensure_definition_parts(
            self.obj, self.stage.key, 0, 1
        )
        return parts[0]["mesh"]

    def coords(self, helper=None):
        mesh = (helper or self.helper()).data
        co = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
        mesh.vertices.foreach_get("co", co)
        return co.reshape(-1, 3)

    def loops(self, helper=None):
        mesh = (helper or self.helper()).data
        values = np.empty(len(mesh.loops), dtype=np.uint32)
        mesh.loops.foreach_get("vertex_index", values)
        return values

    def faces(self, helper=None):
        return [tuple(p.vertices) for p in (helper or self.helper()).data.polygons]

    def apply(
        self,
        rid,
        out_hash,
        sections,
        topology=_NO_GEOM,
        attributes=None,
        definitions=None,
    ):
        header = {
            "type": "response",
            "stage": self.stage.key,
            "rid": rid,
            "out_hash": out_hash,
            "status": "ok",
            "warnings": [],
        }
        if topology is not _NO_GEOM:
            header["geom"] = {"topology": topology}
        if attributes is not None:
            header["attributes"] = attributes
        if definitions is not None:
            header["definitions"] = definitions
        self.stage.rid = rid
        self.engine._handle_response(header, sections)

    def forbid_topology_reads(self):
        return mock.patch.object(
            self.geometry_io,
            "_topology_matches",
            side_effect=AssertionError("the token path read polygon/loop arrays"),
        )

    def test_01_same_token_updates_positions_without_topology_reads(self):
        self.apply("sha256:one", "sha256:out-1", quad_sections(0.0), TOKEN_A)
        faces = self.faces()
        self.assertEqual(self.stage.applied_topology, TOKEN_A)

        with self.forbid_topology_reads():
            self.apply("sha256:two", "sha256:out-2", quad_sections(3.0), TOKEN_A)

        self.assertEqual(self.faces(), faces)
        np.testing.assert_allclose(
            self.coords()[:, 2], np.full(4, 3.0, dtype=np.float32), rtol=0, atol=1e-6
        )
        self.assertEqual(self.stage.applied_topology, TOKEN_A)

    def test_02_changed_token_rebuilds_same_length_topology(self):
        self.apply("sha256:three", "sha256:out-3", quad_sections(0.0), TOKEN_A)

        with self.forbid_topology_reads():
            self.apply(
                "sha256:four", "sha256:out-4", quad_sections(0.0, flipped=True), TOKEN_B
            )

        np.testing.assert_array_equal(self.loops(), [0, 1, 3, 2])
        self.assertEqual(self.stage.applied_topology, TOKEN_B)

    def test_03_attribute_only_response_applies_with_the_same_token(self):
        descriptor = attr_mod.descriptor("mesh", "weight", "POINT", "FLOAT", 4, 0)
        self.apply(
            "sha256:five",
            "sha256:out-5",
            quad_sections(values=[1, 2, 3, 4]),
            TOKEN_A,
            attributes=[descriptor],
        )
        values = [entry.value for entry in self.helper().data.attributes["weight"].data]
        self.assertEqual(values, [1.0, 2.0, 3.0, 4.0])

        with self.forbid_topology_reads():
            self.apply(
                "sha256:six",
                "sha256:out-6",
                quad_sections(values=[5, 6, 7, 8]),
                TOKEN_A,
                attributes=[descriptor],
            )

        values = [entry.value for entry in self.helper().data.attributes["weight"].data]
        self.assertEqual(values, [5.0, 6.0, 7.0, 8.0], "attributes were skipped")
        self.assertEqual(self.stage.applied_topology, TOKEN_A)

    def test_04_missing_or_malformed_token_keeps_the_verified_path(self):
        self.apply("sha256:seven", "sha256:out-7", quad_sections(0.0), TOKEN_A)

        with mock.patch.object(
            self.geometry_io,
            "_topology_matches",
            wraps=self.geometry_io._topology_matches,
        ) as matches:
            self.apply("sha256:eight", "sha256:out-8", quad_sections(2.0))
        self.assertEqual(matches.call_count, 1, "a missing token skipped verification")
        self.assertEqual(self.stage.applied_topology, "", "a missing token was stored")

        with mock.patch.object(
            self.geometry_io,
            "_topology_matches",
            wraps=self.geometry_io._topology_matches,
        ) as matches:
            self.apply("sha256:nine", "sha256:out-9", quad_sections(4.0), "not-a-token")
        self.assertEqual(matches.call_count, 1, "a malformed token skipped verification")
        self.assertEqual(self.stage.applied_topology, "", "a malformed token was stored")
        np.testing.assert_allclose(
            self.coords()[:, 2], np.full(4, 4.0, dtype=np.float32), rtol=0, atol=1e-6
        )

    def test_05_clearing_the_mesh_helper_clears_the_token(self):
        self.apply("sha256:ten", "sha256:out-10", quad_sections(0.0), TOKEN_A)
        self.assertEqual(self.stage.applied_topology, TOKEN_A)

        self.apply("sha256:eleven", "sha256:out-11", {})

        self.assertEqual(len(self.helper().data.vertices), 0)
        self.assertEqual(self.stage.applied_topology, "")

    def test_06_definition_same_token_updates_without_topology_reads(self):
        helper = self.definition_helper()
        self.apply(
            "sha256:twelve",
            "sha256:out-12",
            definition_sections(0.0),
            TOKEN_A,
            definitions=DEFINITIONS,
        )
        faces = self.faces(helper)
        self.assertEqual(self.stage.applied_topology, TOKEN_A)

        with self.forbid_topology_reads():
            self.apply(
                "sha256:thirteen",
                "sha256:out-13",
                definition_sections(3.0),
                TOKEN_A,
                definitions=DEFINITIONS,
            )

        self.assertEqual(self.faces(helper), faces)
        np.testing.assert_allclose(
            self.coords(helper)[:, 2],
            np.full(4, 3.0, dtype=np.float32),
            rtol=0,
            atol=1e-6,
        )
        self.assertEqual(self.stage.applied_topology, TOKEN_A)

    def test_07_definition_changed_token_rebuilds(self):
        helper = self.definition_helper()
        self.apply(
            "sha256:fourteen",
            "sha256:out-14",
            definition_sections(0.0),
            TOKEN_A,
            definitions=DEFINITIONS,
        )

        with self.forbid_topology_reads():
            self.apply(
                "sha256:fifteen",
                "sha256:out-15",
                definition_sections(0.0, flipped=True),
                TOKEN_B,
                definitions=DEFINITIONS,
            )

        np.testing.assert_array_equal(self.loops(helper), [0, 1, 3, 2])
        self.assertEqual(self.stage.applied_topology, TOKEN_B)

    def test_08_definition_attribute_only_response_applies(self):
        helper = self.definition_helper()
        descriptor = attr_mod.descriptor(
            "mesh", "weight", "POINT", "FLOAT", 4, 0,
            prefix="idef.0.", definition=0,
        )
        self.apply(
            "sha256:sixteen",
            "sha256:out-16",
            definition_sections(values=[1, 2, 3, 4]),
            TOKEN_A,
            attributes=[descriptor],
            definitions=DEFINITIONS,
        )
        values = [entry.value for entry in helper.data.attributes["weight"].data]
        self.assertEqual(values, [1.0, 2.0, 3.0, 4.0])

        with self.forbid_topology_reads():
            self.apply(
                "sha256:seventeen",
                "sha256:out-17",
                definition_sections(values=[5, 6, 7, 8]),
                TOKEN_A,
                attributes=[descriptor],
                definitions=DEFINITIONS,
            )

        values = [entry.value for entry in helper.data.attributes["weight"].data]
        self.assertEqual(values, [5.0, 6.0, 7.0, 8.0], "definition attributes were skipped")
        self.assertEqual(self.stage.applied_topology, TOKEN_A)


if __name__ == "__main__":
    unittest.main()
