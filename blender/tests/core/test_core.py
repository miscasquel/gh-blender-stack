import json
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from blender.gh_stack.core import codec, hashing, params  # noqa: E402


class CanonicalJsonTests(unittest.TestCase):
    def test_sorted_keys_and_tight_separators(self):
        raw = codec.canonical_json({"b": 1, "a": [1, 2, {"d": 4, "c": 3}]})
        self.assertEqual(raw, '{"a":[1,2,{"c":3,"d":4}],"b":1}')

    def test_version_defaults_to_current(self):
        head = json.loads(codec.canonical_json({"type": "ping", "v": 2}))
        self.assertEqual(head["v"], 2)


class FrameRoundTripTests(unittest.TestCase):
    def test_round_trip_without_sections(self):
        frame = codec.encode_frame({"type": "ping", "t": 12.5})
        header, sections = codec.decode_frame(frame)
        self.assertEqual(header["type"], "ping")
        self.assertEqual(header["t"], 12.5)
        self.assertEqual(sections, {})
        self.assertNotIn("sections", header)

    def test_round_trip_with_sections(self):
        frame = codec.encode_frame(
            {"type": "request", "stage": "panelize-3f9a"},
            [
                ("mesh.verts", "f32", (0.0, 1.0, 2.0, 3.0)),
                ("mesh.face_counts", "u32", (3, 4)),
            ],
        )
        header, sections = codec.decode_frame(frame)
        self.assertEqual(header["type"], "request")
        self.assertEqual(sections["mesh.verts"], (0.0, 1.0, 2.0, 3.0))
        self.assertEqual(sections["mesh.face_counts"], (3, 4))
        # The header declares sizes that match the bytes.
        by_name = {s["name"]: s for s in header["sections"]}
        self.assertEqual(by_name["mesh.verts"]["bytes"], 16)
        self.assertEqual(by_name["mesh.verts"]["shape"][0], 4)
        self.assertEqual(by_name["mesh.face_counts"]["bytes"], 8)

    def test_empty_section_list_means_absent(self):
        frame = codec.encode_frame({"type": "hello", "stages": []})
        header, _ = codec.decode_frame(frame)
        self.assertNotIn("sections", header)

    def test_i32_round_trip(self):
        """i32 exists for Blender's signed INT attribute type; it is four bytes
        per element and round-trips negative values."""
        frame = codec.encode_frame(
            {"type": "response", "stage": "s"},
            [("attr.0.values", "i32", (-3, 0, 7, 2147483647, -2147483648))],
        )
        header, sections = codec.decode_frame(frame)
        entry = header["sections"][0]
        self.assertEqual(entry["dtype"], "i32")
        self.assertEqual(entry["bytes"], 20)
        self.assertEqual(sections["attr.0.values"], (-3, 0, 7, 2147483647, -2147483648))

    def test_attribute_descriptors_name_their_sections(self):
        from blender.gh_stack.core import attributes as attr_mod

        entry = attr_mod.descriptor("mesh", "panel_id", "FACE", "INT", 2, 3)
        self.assertEqual(entry["section"], "attr.3.values")
        self.assertEqual(entry["components"], 1)
        self.assertNotIn("offsets", entry)
        strings = attr_mod.descriptor("pts", "tag", "POINT", "STRING", 6, 4)
        self.assertEqual(strings["offsets"], "attr.4.offsets")
        self.assertEqual(attr_mod.wire_dtype("FLOAT4X4"), "f32")
        self.assertEqual(attr_mod.components("FLOAT4X4"), 16)

    def test_section_order_is_preserved(self):
        frame = codec.encode_frame(
            {"type": "request"},
            [("crv.counts", "u32", (2,)), ("crv.points", "f32", (0.0, 0.0, 0.0, 1.0))],
        )
        _, sections = codec.decode_frame(frame)
        self.assertEqual(sections["crv.counts"], (2,))
        self.assertEqual(len(sections["crv.points"]), 4)

    def test_frame_length_prefix_is_little_endian(self):
        frame = codec.encode_frame({"type": "ping"})
        import struct

        (head_len,) = struct.unpack_from("<I", frame, 0)
        self.assertEqual(head_len + 4 + len(frame[4 + head_len :]), len(frame))

    def test_codec_errors(self):
        with self.assertRaises(codec.HeaderError):
            codec.decode_frame(b"")
        with self.assertRaises(codec.HeaderError):
            codec.decode_frame(b"\x05\x00\x00\x00short")
        # Truncated payload after a valid header.
        head = codec.canonical_json({"type": "x", "v": 2}).encode()
        bad = codec._HEADER_LEN.pack(len(head)) + head + b"\x00"
        with self.assertRaises(codec.HeaderError):
            codec.decode_frame(bad)

    def test_proto_mismatch(self):
        frame = codec.encode_frame({"type": "x"})
        frame = frame.replace(b'"v":2', b'"v":3', 1)
        with self.assertRaises(codec.CodecError) as ctx:
            codec.decode_frame(frame)
        self.assertIn("proto_mismatch", str(ctx.exception))

    def test_unknown_dtype_rejected(self):
        with self.assertRaises(codec.SectionError):
            codec.section_bytes("mesh.verts", "f64", (0.0,))

    def test_header_requires_a_type(self):
        with self.assertRaises(codec.HeaderError):
            codec.encode_frame({"t": 1})


class GeometryHashTests(unittest.TestCase):
    def test_same_bytes_same_hash(self):
        a = hashing.geometry_hash({"mesh.verts": b"\x00\x01\x02\x03"})
        b = hashing.geometry_hash({"mesh.verts": b"\x00\x01\x02\x03"})
        self.assertEqual(a, b)
        self.assertTrue(a.startswith("sha256:"))

    def test_different_bytes_different_hash(self):
        a = hashing.geometry_hash({"mesh.verts": b"\x00"})
        b = hashing.geometry_hash({"mesh.verts": b"\x01"})
        self.assertNotEqual(a, b)

    def test_name_matters(self):
        a = hashing.geometry_hash({"mesh.verts": b"\x00"})
        b = hashing.geometry_hash({"subd.verts": b"\x00"})
        self.assertNotEqual(a, b)

    def test_order_is_independent_of_insertion(self):
        a = hashing.geometry_hash({"mesh.verts": b"ab", "mesh.face_counts": b"cd"})
        b = hashing.geometry_hash({"mesh.face_counts": b"cd", "mesh.verts": b"ab"})
        self.assertEqual(a, b)

    def test_absent_sections_are_skipped(self):
        only_mesh = hashing.geometry_hash({"mesh.verts": b"ab"})
        explicit_gaps = hashing.geometry_hash(
            {"mesh.verts": b"ab", "pts.positions": b""}
        )
        self.assertNotEqual(only_mesh, explicit_gaps)

    def test_definition_sections_are_order_independent(self):
        a = hashing.geometry_hash({"idef.1.mesh.verts": b"ab", "idef.0.mesh.verts": b"cd"})
        b = hashing.geometry_hash({"idef.0.mesh.verts": b"cd", "idef.1.mesh.verts": b"ab"})
        self.assertEqual(a, b)

    def test_definition_index_order_is_numeric_not_lexicographic(self):
        names = hashing.ordered_names(
            ["idef.10.mesh.verts", "idef.2.mesh.verts", "mesh.verts"]
        )
        self.assertEqual(
            names, ["mesh.verts", "idef.2.mesh.verts", "idef.10.mesh.verts"]
        )

    def test_output_hash_uses_header_order(self):
        a = hashing.output_hash([("mesh.verts", b"ab"), ("crv.counts", b"cd")])
        b = hashing.output_hash([("crv.counts", b"cd"), ("mesh.verts", b"ab")])
        self.assertNotEqual(a, b)

    def test_request_id_is_stable_and_input_sensitive(self):
        base = hashing.request_id(
            "panelize-3f9a", '{"a":1}', "sha256:dead", "m", "stack-local", 0
        )
        again = hashing.request_id(
            "panelize-3f9a", '{"a":1}', "sha256:dead", "m", "stack-local", 0
        )
        self.assertEqual(base, again)
        self.assertNotEqual(
            base,
            hashing.request_id(
                "panelize-3f9a", '{"a":1}', "sha256:dead", "m", "stack-local", 1
            ),
        )
        self.assertNotEqual(
            base,
            hashing.request_id(
                "panelize-3f9b", '{"a":1}', "sha256:dead", "m", "stack-local", 0
            ),
        )
        self.assertNotEqual(
            base,
            hashing.request_id(
                "panelize-3f9a", '{"a":2}', "sha256:dead", "m", "stack-local", 0
            ),
        )


class ParamsTests(unittest.TestCase):
    def _schema(self):
        return [
            {"id": "aaa", "label": "Radius", "type": "float", "default": 2.0, "min": 0.0, "max": 10.0},
            {"id": "bbb", "label": "Count", "type": "int", "default": 3},
            {"id": "ccc", "label": "On", "type": "bool", "default": False},
            {"id": "ddd", "label": "Pos", "type": "vector3"},
            {"id": "eee", "label": "Which", "type": "value_list", "values": ["a", "b"], "default": 1},
            {"id": "fff", "label": "Note", "type": "text", "default": "hi"},
        ]

    def test_cast_types(self):
        self.assertEqual(params.cast("float", 3), 3.0)
        self.assertEqual(params.cast("int", 3.6), 4)
        self.assertIs(params.cast("bool", 0), False)
        self.assertIs(params.cast("bool", 1), True)
        self.assertEqual(params.cast("vector3", [1, 2, 3]), (1.0, 2.0, 3.0))
        self.assertEqual(params.cast("value_list", 2), 2)
        self.assertEqual(params.cast("text", 5), "5")

    def test_cast_rejects_short_vectors(self):
        with self.assertRaises(params.SchemaError):
            params.cast("vector3", (1, 2))

    def test_cast_params_uses_defaults(self):
        schema = self._schema()
        out = params.cast_params(schema, {})
        self.assertEqual(out["aaa"], 2.0)
        self.assertEqual(out["ddd"], (0.0, 0.0, 0.0))
        self.assertEqual(out["fff"], "hi")

    def test_canonical_json_is_sorted_and_tight(self):
        schema = self._schema()
        out = params.canonical_param_json(schema, {"aaa": 5.5, "bbb": 2.9})
        self.assertIn('"aaa":5.5', out)
        self.assertIn('"bbb":3', out)
        self.assertEqual(out.index('"aaa"'), out.index('"aaa"'))
        self.assertNotIn(", ", out)

    def test_clamp(self):
        schema = [{"id": "x", "label": "R", "type": "float", "default": 1.0, "min": 0.0, "max": 10.0}]
        self.assertEqual(params.clamp(schema[0], 50.0), 10.0)
        self.assertEqual(params.clamp(schema[0], -5.0), 0.0)
        self.assertEqual(params.clamp(schema[0], 4.0), 4.0)

    def test_text_not_animatable(self):
        self.assertNotIn("text", params.ANIMATABLE)
        self.assertIn("float", params.ANIMATABLE)

    def test_default_for_unknown_type(self):
        with self.assertRaises(params.SchemaError):
            params.default_for("nonsense")



class CacheTests(unittest.TestCase):
    def test_add_and_contains(self):
        from blender.gh_stack.core.cache import CacheIndex

        idx = CacheIndex("/tmp/base", "stack1", "panelize-3f9a")
        idx.add("sha256:a", 10)
        self.assertIn("sha256:a", idx)
        self.assertIsNotNone(idx.get("sha256:a"))
        self.assertNotIn("sha256:b", idx)

    def test_path_layout(self):
        from blender.gh_stack.core.cache import CacheIndex

        idx = CacheIndex("/tmp/base", "stack1", "panelize-3f9a")
        path = idx.path_for("sha256:abc")
        self.assertEqual(
            path, "/tmp/base/cache/stack1/panelize-3f9a/sha256_abc.bin"
        )

    def test_fifo_eviction_by_size(self):
        from blender.gh_stack.core.cache import CacheIndex

        idx = CacheIndex("/tmp/base", "s", "k", size_limit=100)
        idx.add("sha256:a", 60)
        idx.add("sha256:b", 60)
        # The oldest (a) is dropped, the newest (b) stays.
        self.assertNotIn("sha256:a", idx)
        self.assertIn("sha256:b", idx)
        self.assertEqual(idx.total_size(), 60)

    def test_touch_promotes(self):
        from blender.gh_stack.core.cache import CacheIndex

        idx = CacheIndex("/tmp/base", "s", "k", size_limit=150)
        idx.add("sha256:a", 60)
        idx.add("sha256:b", 60)
        idx.touch("sha256:a")
        idx.add("sha256:c", 60)
        # a was touched, so b is the oldest and gets evicted first.
        self.assertIn("sha256:a", idx)
        self.assertNotIn("sha256:b", idx)
        self.assertIn("sha256:c", idx)

    def test_remove(self):
        from blender.gh_stack.core.cache import CacheIndex

        idx = CacheIndex("/tmp/base", "s", "k")
        idx.add("sha256:a", 1)
        idx.remove("sha256:a")
        self.assertNotIn("sha256:a", idx)


class ResponseStoreTests(unittest.TestCase):
    """The disk half of the cache: raw frames plus generation/fingerprint sidecars."""

    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _store(self, base=None, **kwargs):
        from blender.gh_stack.core.cache import ResponseStore

        return ResponseStore(base or self.tmp.name, "stack1", "panelize-3f9a", **kwargs)

    def test_store_exposes_base_dir_and_size_limit(self):
        store = self._store(size_limit=1234)
        self.assertEqual(store.base_dir, self.tmp.name)
        self.assertEqual(store.size_limit, 1234)
        store.size_limit = 99
        self.assertEqual(store.index.size_limit, 99)
        self.assertIsNone(store.entry("sha256:missing"))

    def test_store_and_load_round_trip(self):
        store = self._store()
        store.store("sha256:aa", b"frame-bytes", generation=3, definition="def-1", out_hash="sha256:o1")
        self.assertEqual(store.load("sha256:aa"), b"frame-bytes")
        entry = store.index.get("sha256:aa")
        self.assertEqual(entry.generation, 3)
        self.assertEqual(entry.definition, "def-1")
        self.assertEqual(entry.out_hash, "sha256:o1")

    def test_scan_rebuilds_the_index(self):
        store = self._store()
        store.store("sha256:aa", b"a" * 10, generation=1, definition="d", out_hash="o")
        store.store("sha256:bb", b"b" * 20, generation=1, definition="d", out_hash="p")
        other = self._store()
        other.scan()
        self.assertIn("sha256:aa", other.index)
        self.assertIn("sha256:bb", other.index)
        self.assertEqual(other.index.total_size(), 30)

    def test_missing_sidecar_drops_the_frame(self):
        import os

        store = self._store()
        store.store("sha256:aa", b"a", generation=0, definition="d")
        os.remove(store.index.meta_path_for("sha256:aa"))
        other = self._store()
        other.scan()
        self.assertNotIn("sha256:aa", other.index)
        self.assertFalse(os.path.exists(store.index.path_for("sha256:aa")))

    def test_valid_checks_generation_and_definition(self):
        store = self._store()
        store.store("sha256:aa", b"a", generation=2, definition="def")
        self.assertIsNotNone(store.valid("sha256:aa", generation=2, definition="def"))
        # A mismatch is invalid and pruned on the spot, so it can never replay.
        self.assertIsNone(store.valid("sha256:aa", generation=3, definition="def"))
        self.assertNotIn("sha256:aa", store.index)
        store.store("sha256:aa", b"a", generation=2, definition="def")
        self.assertIsNone(store.valid("sha256:aa", generation=2, definition="other"))
        self.assertNotIn("sha256:aa", store.index)

    def test_prune_removes_by_generation(self):
        import os

        store = self._store()
        store.store("sha256:aa", b"a", generation=1, definition="d")
        store.store("sha256:bb", b"b", generation=1, definition="d")
        store.store("sha256:cc", b"c", generation=2, definition="d")
        removed = store.prune(generation=2)
        self.assertEqual(sorted(removed), ["sha256:aa", "sha256:bb"])
        self.assertEqual(store.rids(), ["sha256:cc"])
        self.assertFalse(os.path.exists(store.index.path_for("sha256:aa")))
        self.assertFalse(os.path.exists(store.index.meta_path_for("sha256:aa")))

    def test_prune_removes_by_definition(self):
        store = self._store()
        store.store("sha256:aa", b"a", generation=0, definition="old")
        store.store("sha256:bb", b"b", generation=0, definition="new")
        self.assertEqual(store.prune(definition="new"), ["sha256:aa"])
        self.assertEqual(store.rids(), ["sha256:bb"])

    def test_eviction_deletes_files(self):
        import os

        store = self._store(size_limit=100)
        store.store("sha256:aa", b"a" * 60)
        store.store("sha256:bb", b"b" * 60)
        self.assertNotIn("sha256:aa", store.index)
        self.assertIn("sha256:bb", store.index)
        self.assertFalse(os.path.exists(store.index.path_for("sha256:aa")))
        self.assertFalse(os.path.exists(store.index.meta_path_for("sha256:aa")))

    def test_touch_survives_eviction(self):
        store = self._store(size_limit=150)
        store.store("sha256:aa", b"a" * 60)
        store.store("sha256:bb", b"b" * 60)
        store.load("sha256:aa")
        store.store("sha256:cc", b"c" * 60)
        self.assertIn("sha256:aa", store.index)
        self.assertNotIn("sha256:bb", store.index)

    def test_remove_deletes_files(self):
        import os

        store = self._store()
        store.store("sha256:aa", b"a")
        store.remove("sha256:aa")
        self.assertNotIn("sha256:aa", store.index)
        self.assertFalse(os.path.exists(store.index.path_for("sha256:aa")))

    def test_stale_temp_files_are_cleaned(self):
        import os

        store = self._store()
        os.makedirs(store.index.stage_dir, exist_ok=True)
        tmp = os.path.join(store.index.stage_dir, "sha256_zz.bin.tmp")
        with open(tmp, "wb") as handle:
            handle.write(b"half")
        store.scan()
        self.assertFalse(os.path.exists(tmp))


if __name__ == "__main__":
    unittest.main()
