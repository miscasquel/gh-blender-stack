"""Layer A boundary tests for the protocol codec.

Every malformed frame here is built in memory. The codec is a trust boundary:
the only acceptable outcome for untrusted bytes is a typed ``CodecError`` with
one of the protocol §4.6 codes, never ``KeyError``/``TypeError``/``IndexError``/
``struct.error``/``UnicodeError``. The encoder tests hold the other half of the
same contract: it must refuse what its decoder would refuse, and it must keep
valid frames byte-identical.
"""

import os
import struct
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from blender.stageloom.core import codec  # noqa: E402

_MISSING = object()


def canonical_head(header):
    return codec.canonical_json(header).encode("utf-8")


def frame_from_header(header, body=b""):
    head = canonical_head(header)
    return struct.pack("<I", len(head)) + head + body


def frame_from_text(text, body=b""):
    head = text.encode("utf-8")
    return struct.pack("<I", len(head)) + head + body


def frame_from_raw_head(head, body=b""):
    return struct.pack("<I", len(head)) + head + body


def section(name="mesh.verts", dtype="f32", nbytes=4, shape=_MISSING):
    entry = {"name": name, "dtype": dtype, "bytes": nbytes}
    if shape is not _MISSING:
        entry["shape"] = shape
    return entry


def sections_frame(entries, body=b"", **header):
    value = {"type": "response", "v": 2, "sections": entries}
    value.update(header)
    return frame_from_header(value, body)


class CodecBoundaryTestCase(unittest.TestCase):
    def assert_code(self, code, exc_type, function, *args):
        with self.assertRaises(exc_type) as ctx:
            function(*args)
        self.assertIn(code, codec.CODEC_CODES)
        self.assertEqual(ctx.exception.code, code)
        return ctx.exception

    def assert_decode_code(self, code, exc_type, frame):
        return self.assert_code(code, exc_type, codec.decode_frame, frame)

    def assert_encode_code(self, code, exc_type, header, sections=None):
        return self.assert_code(code, exc_type, codec.encode_frame, header, sections)


class DecodeFrameTests(CodecBoundaryTestCase):
    def test_non_bytes_input_is_a_header_error(self):
        for value in (None, "a frame", 42, [1, 2, 3], object()):
            with self.subTest(value=type(value).__name__):
                self.assert_decode_code("bad_frame", codec.HeaderError, value)

    def test_short_or_missing_length_prefix(self):
        for frame in (b"", b"\x00", b"\x00\x00\x00"):
            with self.subTest(frame=frame):
                self.assert_decode_code("bad_frame", codec.HeaderError, frame)

    def test_truncated_header(self):
        self.assert_decode_code(
            "bad_frame", codec.HeaderError, struct.pack("<I", 16) + b'{"v":2}'
        )

    def test_truncated_payload(self):
        frame = frame_from_header(
            {"type": "response", "v": 2, "sections": [section(nbytes=8)]}, b"\x00" * 7
        )
        self.assert_decode_code("bad_frame", codec.HeaderError, frame)

    def test_trailing_payload_bytes(self):
        frame = frame_from_header({"type": "ping", "v": 2}, b"\x00")
        self.assert_decode_code("bad_frame", codec.HeaderError, frame)

    def test_header_over_the_cap_is_too_large(self):
        frame = frame_from_header({"type": "ping", "v": 2})
        with mock.patch.object(codec, "MAX_HEADER_BYTES", 8):
            self.assert_decode_code("too_large", codec.CodecError, frame)

    def test_frame_over_the_cap_is_too_large(self):
        frame = frame_from_header({"type": "ping", "v": 2})
        with mock.patch.object(codec, "MAX_MESSAGE_BYTES", 8):
            self.assert_decode_code("too_large", codec.CodecError, frame)

    def test_bytes_like_inputs_decode_identically(self):
        frame = codec.encode_frame(
            {"type": "response", "v": 2},
            [("mesh.verts", "f32", (0.0, 1.0, 2.0)), ("mesh.face_counts", "u32", (3,))],
        )
        expected = codec.decode_frame(frame)
        for value in (bytearray(frame), memoryview(frame), memoryview(bytearray(frame))):
            with self.subTest(value=type(value).__name__):
                self.assertEqual(codec.decode_frame(value), expected)

    def test_deeply_nested_header_is_classified(self):
        frame = frame_from_text("[" * 4000 + "]" * 4000)
        self.assert_decode_code("bad_frame", codec.HeaderError, frame)


class DecodeHeaderValueTests(CodecBoundaryTestCase):
    def test_header_must_be_a_json_object(self):
        for text in ("[]", "42", '"frame"', "null", "true"):
            with self.subTest(text=text):
                self.assert_decode_code(
                    "bad_frame", codec.HeaderError, frame_from_text(text)
                )

    def test_header_type_must_be_a_non_empty_string(self):
        for header in (
            {"v": 2},
            {"v": 2, "type": ""},
            {"v": 2, "type": None},
            {"v": 2, "type": 5},
            {"v": 2, "type": []},
        ):
            with self.subTest(header=header):
                self.assert_decode_code(
                    "bad_frame", codec.HeaderError, frame_from_header(header)
                )

    def test_version_must_be_the_integer_two(self):
        headers = [
            {"type": "x"},
            {"type": "x", "v": None},
            {"type": "x", "v": 1},
            {"type": "x", "v": 3},
            {"type": "x", "v": 2.0},
            {"type": "x", "v": True},
            {"type": "x", "v": "2"},
        ]
        for header in headers:
            with self.subTest(header=header):
                self.assert_decode_code(
                    "proto_mismatch", codec.CodecError, frame_from_header(header)
                )

    def test_non_finite_literals_are_rejected(self):
        for literal in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(literal=literal):
                frame = frame_from_text(
                    '{"type":"x","v":2,"number":%s}' % literal
                )
                self.assert_decode_code("bad_frame", codec.HeaderError, frame)

    def test_float_overflow_is_rejected(self):
        for literal in ("1e400", "-1e400"):
            with self.subTest(literal=literal):
                frame = frame_from_text(
                    '{"type":"x","v":2,"number":%s}' % literal
                )
                self.assert_decode_code("bad_frame", codec.HeaderError, frame)

    def test_lone_surrogates_are_rejected(self):
        texts = (
            r'{"type":"x","v":2,"note":"\ud800"}',
            r'{"type":"x","v":2,"\ud800":1}',
            r'{"type":"x","v":2,"items":["ok","\udfff"]}',
        )
        for text in texts:
            with self.subTest(text=text):
                self.assert_decode_code(
                    "bad_frame", codec.HeaderError, frame_from_text(text)
                )

    def test_valid_escape_and_finite_numbers_still_decode(self):
        frame = frame_from_text(
            r'{"type":"x","v":2,"note":"caf\u00e9","number":1e308}'
        )
        header, sections = codec.decode_frame(frame)
        self.assertEqual(header["note"], "caf\u00e9")
        self.assertGreater(header["number"], 1e307)
        self.assertEqual(sections, {})

    def test_duplicate_json_keys_are_rejected(self):
        texts = (
            '{"type":"x","v":2,"type":"y"}',
            '{"type":"x","v":2,"outer":{"a":1,"a":2}}',
        )
        for text in texts:
            with self.subTest(text=text):
                self.assert_decode_code(
                    "bad_frame", codec.HeaderError, frame_from_text(text)
                )

    def test_raw_control_characters_are_rejected(self):
        for text in (
            '{"type":"x","v":2,"note":"line\nbreak"}',
            '{"type":"x","v":2,"note":"tab\there"}',
        ):
            with self.subTest(text=text):
                self.assert_decode_code(
                    "bad_frame", codec.HeaderError, frame_from_text(text)
                )

    def test_invalid_utf8_is_rejected(self):
        frame = frame_from_raw_head(b'{"type":"x","v":2,"note":"\xff"}')
        self.assert_decode_code("bad_frame", codec.HeaderError, frame)


class DecodeSectionTableTests(CodecBoundaryTestCase):
    def test_sections_must_be_a_list(self):
        for value in ({}, "mesh.verts", None, 7, True):
            with self.subTest(value=value):
                self.assert_decode_code(
                    "bad_frame",
                    codec.HeaderError,
                    frame_from_header({"type": "response", "v": 2, "sections": value}),
                )

    def test_section_entry_must_be_an_object(self):
        for entry in (42, "mesh.verts", [], None, True):
            with self.subTest(entry=entry):
                self.assert_decode_code(
                    "bad_frame", codec.SectionError, sections_frame([entry])
                )

    def test_section_name_is_required_and_non_empty(self):
        for name in (_MISSING, "", None, 5, []):
            entry = section()
            if name is _MISSING:
                del entry["name"]
            else:
                entry["name"] = name
            with self.subTest(name=name):
                self.assert_decode_code(
                    "bad_frame", codec.SectionError, sections_frame([entry], b"\x00" * 4)
                )

    def test_section_dtype_is_required_and_known(self):
        for dtype in (_MISSING, None, 5, "", "f64", "float32"):
            entry = section()
            if dtype is _MISSING:
                del entry["dtype"]
            else:
                entry["dtype"] = dtype
            with self.subTest(dtype=dtype):
                self.assert_decode_code(
                    "bad_frame", codec.SectionError, sections_frame([entry], b"\x00" * 4)
                )

    def test_section_bytes_must_be_a_non_negative_integer(self):
        for nbytes in (_MISSING, None, -4, 4.0, True, "4"):
            entry = section()
            if nbytes is _MISSING:
                del entry["bytes"]
            else:
                entry["bytes"] = nbytes
            with self.subTest(nbytes=nbytes):
                self.assert_decode_code(
                    "bad_frame", codec.SectionError, sections_frame([entry], b"\x00" * 4)
                )

    def test_duplicate_section_names_are_rejected(self):
        frame = sections_frame(
            [section(name="mesh.verts"), section(name="mesh.verts")], b"\x00" * 8
        )
        self.assert_decode_code("bad_frame", codec.SectionError, frame)

    def test_shape_must_be_a_list(self):
        for shape in ("4", 4, None, {}):
            with self.subTest(shape=shape):
                self.assert_decode_code(
                    "bad_frame",
                    codec.SectionError,
                    sections_frame([section(shape=shape)], b"\x00" * 4),
                )

    def test_shape_values_must_be_non_negative_integers(self):
        for shape in ([True], [-1], [1.0], ["1"], [[1]], [None]):
            with self.subTest(shape=shape):
                self.assert_decode_code(
                    "bad_frame",
                    codec.SectionError,
                    sections_frame([section(shape=shape)], b"\x00" * 4),
                )

    def test_shape_product_must_match_bytes(self):
        for shape, nbytes in (([2], 4), ([1], 8), ([2, 2], 8), ([], 8)):
            with self.subTest(shape=shape, nbytes=nbytes):
                self.assert_decode_code(
                    "bad_frame",
                    codec.SectionError,
                    sections_frame([section(shape=shape, nbytes=nbytes)], b"\x00" * nbytes),
                )

    def test_shape_product_matches_for_multi_dimensional_and_empty_shapes(self):
        for entry, body in (
            (section(dtype="u8", shape=[2, 2], nbytes=4), b"\x01\x02\x03\x04"),
            (section(dtype="u8", shape=[], nbytes=1), b"\x05"),
            (section(dtype="f32", shape=[0], nbytes=0), b""),
        ):
            with self.subTest(shape=entry["shape"]):
                header, decoded = codec.decode_frame(sections_frame([entry], body))
                self.assertIn(entry["name"], decoded)
                self.assertEqual(len(header["sections"]), 1)

    def test_shape_is_optional(self):
        frame = sections_frame([section(nbytes=4)], b"\x00" * 4)
        _, decoded = codec.decode_frame(frame)
        self.assertEqual(decoded["mesh.verts"], (0.0,))

    def test_misaligned_section_length_is_rejected(self):
        for entry, body in (
            (section(dtype="f32", nbytes=3), b"\x00" * 3),
            (section(dtype="i32", nbytes=6), b"\x00" * 6),
            (section(dtype="u32", nbytes=1), b"\x00"),
        ):
            with self.subTest(dtype=entry["dtype"], nbytes=entry["bytes"]):
                self.assert_decode_code(
                    "bad_frame", codec.SectionError, sections_frame([entry], body)
                )

    def test_section_past_the_payload_is_rejected(self):
        frame = sections_frame([section(nbytes=4)], b"\x00\x00")
        self.assert_decode_code("bad_frame", codec.HeaderError, frame)

    def test_oversized_declared_bytes_is_rejected(self):
        frame = sections_frame([section(nbytes=10**30)], b"\x00" * 4)
        self.assert_decode_code("bad_frame", codec.HeaderError, frame)

    def test_multiple_sections_cannot_leave_trailing_bytes(self):
        frame = sections_frame(
            [section(name="a", dtype="u8", nbytes=2), section(name="b", dtype="u8", nbytes=2)],
            b"\x00" * 5,
        )
        self.assert_decode_code("bad_frame", codec.HeaderError, frame)

    def test_empty_sections_list_is_valid(self):
        header, decoded = codec.decode_frame(
            frame_from_header({"type": "response", "v": 2, "sections": []})
        )
        self.assertEqual(header["sections"], [])
        self.assertEqual(decoded, {})


class EncodeValidationTests(CodecBoundaryTestCase):
    def test_header_must_be_a_mapping(self):
        for header in (None, "ping", 42, b"ping"):
            with self.subTest(header=type(header).__name__):
                self.assert_encode_code("bad_frame", codec.HeaderError, header)

    def test_type_must_be_a_non_empty_string(self):
        for header in (
            {"v": 2},
            {"type": "", "v": 2},
            {"type": None, "v": 2},
            {"type": 5, "v": 2},
            {"type": [], "v": 2},
        ):
            with self.subTest(header=header):
                self.assert_encode_code("bad_frame", codec.HeaderError, header)

    def test_version_is_exactly_two(self):
        for v in (1, 3, 2.0, True, "2", None):
            with self.subTest(v=v):
                self.assert_encode_code(
                    "proto_mismatch", codec.CodecError, {"type": "x", "v": v}
                )

    def test_version_defaults_to_two(self):
        frame = codec.encode_frame({"type": "ping"})
        header, _ = codec.decode_frame(frame)
        self.assertEqual(header["v"], 2)

    def test_duplicate_section_names_are_rejected(self):
        self.assert_encode_code(
            "bad_frame",
            codec.SectionError,
            {"type": "x"},
            [("a", "u8", (1,)), ("a", "u8", (2,))],
        )

    def test_unknown_dtype_is_rejected(self):
        for dtype in ("f64", "", None, 5):
            with self.subTest(dtype=dtype):
                self.assert_encode_code(
                    "bad_frame",
                    codec.SectionError,
                    {"type": "x"},
                    [("a", dtype, (1,))],
                )

    def test_malformed_section_tuples_are_rejected(self):
        for sections in (
            [("a",)],
            [("a", "f32")],
            [("a", "f32", (), 1)],
            ["a"],
            [None],
            [42],
            42,
        ):
            with self.subTest(sections=sections):
                self.assert_encode_code(
                    "bad_frame", codec.SectionError, {"type": "x"}, sections
                )

    def test_section_name_must_be_a_non_empty_string(self):
        for name in ("", None, 5, []):
            with self.subTest(name=name):
                self.assert_encode_code(
                    "bad_frame",
                    codec.SectionError,
                    {"type": "x"},
                    [(name, "u8", (1,))],
                )

    def test_section_name_must_not_contain_a_lone_surrogate(self):
        self.assert_encode_code(
            "bad_frame",
            codec.SectionError,
            {"type": "x"},
            [("a\ud800", "u8", (1,))],
        )

    def test_unpackable_section_values_are_rejected(self):
        for dtype, values in (
            ("u32", (-1,)),
            ("u32", (2**32,)),
            ("i32", (2**31,)),
            ("f32", ("not a number",)),
            ("u8", (256,)),
            ("f32", 42),
        ):
            with self.subTest(dtype=dtype, values=values):
                self.assert_encode_code(
                    "bad_frame",
                    codec.SectionError,
                    {"type": "x"},
                    [("a", dtype, values)],
                )

    def test_non_finite_header_numbers_are_rejected(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                self.assert_encode_code(
                    "bad_frame", codec.HeaderError, {"type": "x", "number": value}
                )
                self.assert_encode_code(
                    "bad_frame", codec.HeaderError, {"type": "x", "v": value}
                )

    def test_lone_surrogates_are_rejected(self):
        for header in (
            {"type": "x", "note": "\ud800"},
            {"type": "x", "\ud800": 1},
            {"type": "x", "items": ["ok", "\udfff"]},
        ):
            with self.subTest(header=header):
                self.assert_encode_code("bad_frame", codec.HeaderError, header)

    def test_non_string_header_keys_are_rejected(self):
        with self.assertRaises(codec.HeaderError) as ctx:
            codec.encode_frame({1: "one", "type": "x"})
        self.assertEqual(ctx.exception.code, "bad_frame")

    def test_oversized_header_is_too_large(self):
        with mock.patch.object(codec, "MAX_HEADER_BYTES", 32):
            exc = self.assert_encode_code(
                "too_large", codec.HeaderError, {"type": "x" * 128}
            )
        self.assertNotEqual(exc.code, "bad_frame")

    def test_oversized_frame_is_too_large(self):
        with mock.patch.object(codec, "MAX_MESSAGE_BYTES", 16):
            self.assert_encode_code(
                "too_large",
                codec.CodecError,
                {"type": "x"},
                [("a", "u8", tuple(range(64)))],
            )

    def test_encoder_does_not_mutate_the_caller_header(self):
        header = {"type": "ping"}
        codec.encode_frame(header)
        self.assertEqual(header, {"type": "ping"})


class RoundTripTests(CodecBoundaryTestCase):
    def test_multi_section_round_trip_preserves_order_and_bytes(self):
        sections = [
            ("mesh.verts", "f32", (0.0, 1.5, -2.0, 3.25)),
            ("mesh.face_counts", "u32", (3, 4)),
            ("attr.0.values", "i32", (-1, 0, 7)),
            ("mesh.colors", "u8", (1, 2, 3, 4)),
        ]
        frame = codec.encode_frame({"type": "response", "stage": "s"}, sections)
        header, decoded = codec.decode_frame(frame)

        names = [name for name, _dtype, _values in sections]
        self.assertEqual([entry["name"] for entry in header["sections"]], names)
        self.assertEqual(list(decoded), names)
        for name, dtype, values in sections:
            with self.subTest(section=name):
                self.assertEqual(
                    codec.section_bytes(name, dtype, decoded[name]),
                    codec.section_bytes(name, dtype, values),
                )

        rebuilt = [
            (entry["name"], entry["dtype"], decoded[entry["name"]])
            for entry in header["sections"]
        ]
        self.assertEqual(codec.encode_frame(header, rebuilt), frame)

    def test_canonical_bytes_are_exact(self):
        expected_head = b'{"sections":[{"bytes":4,"dtype":"f32","name":"mesh.verts","shape":[1]}],"type":"response","v":2}'
        expected = (
            struct.pack("<I", len(expected_head))
            + expected_head
            + struct.pack("<f", 1.5)
        )
        frame = codec.encode_frame(
            {"type": "response"},
            [("mesh.verts", "f32", (1.5,))],
        )
        self.assertEqual(frame, expected)

        expected_head = b'{"t":12.5,"type":"ping","v":2}'
        self.assertEqual(
            codec.encode_frame({"type": "ping", "t": 12.5}),
            struct.pack("<I", len(expected_head)) + expected_head,
        )

    def test_decode_then_encode_keeps_payload(self):
        frame = codec.encode_frame(
            {"type": "x"},
            [("a", "u8", (1, 2, 3)), ("b", "f32", (0.5, -0.5))],
        )
        head_len = struct.unpack_from("<I", frame, 0)[0]
        header, decoded = codec.decode_frame(frame)
        rebuilt = codec.encode_frame(
            header, [(name, header["sections"][i]["dtype"], decoded[name])
                     for i, name in enumerate(decoded)]
        )
        self.assertEqual(rebuilt[4 + head_len :], frame[4 + head_len :])

    def test_empty_section_round_trips(self):
        frame = codec.encode_frame({"type": "x"}, [("empty", "f32", ())])
        header, decoded = codec.decode_frame(frame)
        self.assertEqual(
            header["sections"],
            [{"name": "empty", "dtype": "f32", "shape": [0], "bytes": 0}],
        )
        self.assertEqual(decoded, {"empty": ()})


class ErrorContractTests(CodecBoundaryTestCase):
    def test_every_codec_error_carries_a_protocol_code(self):
        for error_type in (codec.CodecError, codec.HeaderError, codec.SectionError):
            with self.subTest(error_type=error_type.__name__):
                exc = error_type("bad_frame", "message")
                self.assertEqual(exc.code, "bad_frame")
                self.assertIn(exc.code, codec.CODEC_CODES)

    def test_message_prefix_parser_is_gone(self):
        # Codes travel on the exception; a second, textual source would drift.
        self.assertFalse(hasattr(codec, "code_of"))

    def test_truncating_a_valid_frame_never_escapes_classification(self):
        frame = codec.encode_frame(
            {"type": "response"},
            [("mesh.verts", "f32", (0.0, 1.0, 2.0)), ("mesh.face_counts", "u32", (3,))],
        )
        for cut in range(len(frame)):
            with self.subTest(cut=cut):
                with self.assertRaises(codec.CodecError) as ctx:
                    codec.decode_frame(frame[:cut])
                self.assertIn(ctx.exception.code, codec.CODEC_CODES)


if __name__ == "__main__":
    unittest.main()
