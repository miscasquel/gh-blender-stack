import hashlib
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from blender.gh_stack.core import codec, hashing  # noqa: E402

FIXTURE_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "protocol", "fixtures")
)


def fixture_names():
    names = []
    for name in sorted(os.listdir(FIXTURE_ROOT)):
        path = os.path.join(FIXTURE_ROOT, name)
        if os.path.isdir(path):
            names.append(name)
    return names


class GoldenFrameTests(unittest.TestCase):
    """Every fixture must decode to expectation and re-encode to the same bytes.

    This is the Python half of the cross-codec contract (protocol §9). The C#
    codec is checked against the same fixtures when it lands.
    """

    def load(self, name):
        folder = os.path.join(FIXTURE_ROOT, name)
        with open(os.path.join(folder, "frame.bin"), "rb") as f:
            frame = f.read()
        with open(os.path.join(folder, "expect.json")) as f:
            expect = json.load(f)
        return frame, expect

    def test_fixtures_exist(self):
        names = fixture_names()
        for required in (
            "ping", "hello", "schema", "request-ref", "request-mesh", "request-subd",
            "response-mesh", "response-mixed", "response-attributes", "response-error",
            "error-unknown-ref",
        ):
            self.assertIn(required, names)

    def test_frame_sha256_matches_expectation(self):
        for name in fixture_names():
            with self.subTest(fixture=name):
                frame, expect = self.load(name)
                self.assertEqual(
                    "sha256:" + hashlib.sha256(frame).hexdigest(),
                    expect["frame_sha256"],
                    name,
                )
                self.assertEqual(len(frame), expect["frame_bytes"], name)

    def test_decode_matches_expectation(self):
        for name in fixture_names():
            with self.subTest(fixture=name):
                frame, expect = self.load(name)
                header, sections = codec.decode_frame(frame)
                self.assertEqual(header, expect["header"], name)

                declared = set()
                for entry in header.get("sections", []):
                    declared.add(entry["name"])
                    meta = expect["sections"][entry["name"]]
                    self.assertEqual(entry["dtype"], meta["dtype"], name)
                    self.assertEqual(entry["shape"], meta["shape"], name)
                    self.assertEqual(entry["bytes"], meta["bytes"], name)
                self.assertEqual(declared, set(expect["sections"]), name)

                for section_name, meta in expect["sections"].items():
                    raw = codec.section_bytes(section_name, meta["dtype"], sections[section_name])
                    self.assertEqual(
                        "sha256:" + hashlib.sha256(raw).hexdigest(),
                        meta["sha256"],
                        section_name,
                    )

    def test_reencode_is_byte_identical(self):
        for name in fixture_names():
            with self.subTest(fixture=name):
                frame, expect = self.load(name)
                header, sections = codec.decode_frame(frame)
                # Rebuild the section list in header order from the decoded values.
                rebuilt = []
                for entry in header.get("sections", []):
                    rebuilt.append(
                        (entry["name"], entry["dtype"], sections[entry["name"]])
                    )
                encoded = codec.encode_frame(header, rebuilt)
                self.assertEqual(encoded, frame, name)

    def test_geometry_hash_is_stable_across_runs(self):
        # Two fixtures with the same mesh sections in a different order must hash
        # identically, because the hash order is fixed by the protocol (§6.1).
        frame_a, _ = self.load("request-mesh")
        _, sections_a = codec.decode_frame(frame_a)
        raw = {}
        for entry in codec.decode_frame(frame_a)[0]["sections"]:
            raw[entry["name"]] = codec.section_bytes(
                entry["name"], entry["dtype"], sections_a[entry["name"]]
            )
        first = hashing.geometry_hash(raw)
        # Rebuild the dict in a different insertion order.
        shuffled = dict(reversed(list(raw.items())))
        self.assertEqual(first, hashing.geometry_hash(shuffled))

    def test_error_fixture_has_no_sections(self):
        frame, expect = self.load("error-unknown-ref")
        self.assertEqual(expect["header"]["type"], "error")
        self.assertEqual(expect["header"]["code"], "unknown_ref")
        self.assertEqual(expect["sections"], {})

    def test_mixed_fixture_covers_all_section_groups(self):
        frame, expect = self.load("response-mixed")
        names = set(expect["sections"])
        self.assertIn("mesh.verts", names)
        self.assertIn("crv.points", names)
        self.assertIn("pts.positions", names)


if __name__ == "__main__":
    unittest.main()
