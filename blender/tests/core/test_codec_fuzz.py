"""Deterministic frame-mutation fuzz for the codec trust boundary (T10a).

The fixed seed and iteration count are part of the test contract:

    FUZZ_SEED = 20261118
    FUZZ_CASES = 10000

Every case starts from a valid encoded frame and applies one of ten bounded
mutation strategies (plus an optional second mutation): length prefix, raw
byte, truncation, appended junk, random bytes, header text, structured header,
frame splice, oversized declaration, and section-table/payload surgery. Small
frames keep the whole run in the normal core suite.

The only acceptable outcome for mutated peer bytes is a :class:`codec.CodecError`
(or its classified subclasses) carrying one of ``CODEC_CODES``. Any other
exception is a fuzz failure with the seed, case, strategy and frame hex in the
message. A successful decode is checked for coherence: the declared table and
the decoded values agree, every decoded byte comes from the frame payload, and
re-encoding the decoded values reproduces the same sections.
"""

import os
import random
import struct
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from blender.stageloom.core import codec  # noqa: E402

# Fixed for reproducibility; changing either is a test-contract change.
FUZZ_SEED = 20261118
FUZZ_CASES = 10000

_MISSING = object()


def _frame_from_header(header, body=b""):
    head = codec.canonical_json(header).encode("utf-8")
    return struct.pack("<I", len(head)) + head + body


def _valid_frames():
    """Small valid frames the mutator starts from."""
    frames = [
        codec.encode_frame({"type": "ping"}),
        codec.encode_frame(
            {"type": "response", "stage": "fuzz"},
            [("mesh.verts", "f32", (0.0, 1.0, 2.0)), ("mesh.face_counts", "u32", (3,))],
        ),
        codec.encode_frame(
            {
                "type": "response",
                "stage": "fuzz",
                "rid": "sha256:" + "0" * 64,
                "out_hash": "sha256:" + "1" * 64,
                "definitions": [{"name": "Block"}],
                "attributes": [
                    {
                        "group": "mesh",
                        "name": "load",
                        "domain": "POINT",
                        "type": "FLOAT",
                        "components": 1,
                        "count": 4,
                        "section": "attr.0.values",
                    }
                ],
            },
            [
                ("mesh.verts", "f32", (0.0, 0.0, 0.0, 1.0, 0.0, 0.0,
                                       1.0, 1.0, 0.0, 0.0, 1.0, 0.0)),
                ("mesh.face_counts", "u32", (4,)),
                ("mesh.face_indices", "u32", (0, 1, 2, 3)),
                ("mesh.colors", "u8", (255, 0, 0, 255, 0, 255, 0, 255,
                                       0, 0, 255, 255, 255, 255, 0, 255)),
                ("attr.0.values", "f32", (0.5, 1.0, 1.5, 2.0)),
            ],
        ),
        codec.encode_frame(
            {"type": "request", "stage": "fuzz", "definitions": [{"name": "Parts", "parts": 2}]},
            [
                ("idef.0.0.mesh.verts", "f32", (0.0, 0.0, 0.0, 1.0, 0.0, 0.0,
                                                1.0, 1.0, 0.0, 0.0, 1.0, 0.0)),
                ("idef.0.0.mesh.face_counts", "u32", (4,)),
                ("idef.0.0.mesh.face_indices", "u32", (0, 1, 2, 3)),
                ("idef.0.1.crv.points", "f32", (0.0, 0.0, 0.0, 1.0, 1.0, 0.0, 0.0, 1.0)),
                ("idef.0.1.crv.counts", "u32", (2,)),
                ("idef.0.1.crv.types", "u8", (0,)),
                ("idef.0.1.crv.orders", "u8", (0,)),
                ("idef.0.1.crv.knots_mode", "u8", (0,)),
                ("idef.0.1.crv.cyclic", "u8", (0,)),
                ("inst.ref_def", "u32", (0, 0)),
                ("inst.transforms", "f32", tuple(float(v) for v in range(32))),
            ],
        ),
        # A valid non-canonical table shape (shape is optional) and multi-dim shape.
        _frame_from_header(
            {"type": "response", "v": 2,
             "sections": [{"name": "a", "dtype": "u8", "shape": [2, 2], "bytes": 4}]},
            b"\x01\x02\x03\x04",
        ),
        _frame_from_header(
            {"type": "response", "v": 2,
             "sections": [{"name": "a", "dtype": "f32", "bytes": 4}]},
            struct.pack("<f", 1.5),
        ),
        _frame_from_header({"type": "response", "v": 2, "sections": []}),
    ]
    for frame in frames:
        header, _sections = codec.decode_frame(frame)
        assert header["type"]
    return frames


def _length_prefix(rng, frame, _pool):
    value = rng.choice(
        [0, 1, 2, 3, 4, len(frame) - 1, len(frame), len(frame) + 1,
         codec.MAX_HEADER_BYTES + 1, 0xFFFFFFFF, rng.getrandbits(32)]
    )
    return struct.pack("<I", value & 0xFFFFFFFF) + frame[4:]


def _flip_byte(rng, frame, _pool):
    if not frame:
        return frame
    data = bytearray(frame)
    index = rng.randrange(len(data))
    data[index] ^= 1 << rng.randrange(8)
    return bytes(data)


def _truncate(rng, frame, _pool):
    return frame[: rng.randrange(len(frame) + 1)]


def _append_junk(rng, frame, _pool):
    return frame + bytes(rng.randrange(256) for _ in range(rng.randrange(1, 17)))


def _random_frame(rng, _frame, _pool):
    return bytes(rng.randrange(256) for _ in range(rng.randrange(0, 65)))


def _header_bounds(frame):
    if len(frame) < 4:
        return None
    (head_len,) = struct.unpack_from("<I", frame, 0)
    if head_len > len(frame) - 4:
        return None
    return 4, 4 + head_len


def _rebuild_header_bytes(frame, head):
    start, _end = _header_bounds(frame) or (4, 4)
    # Keep any payload that followed the original header.
    return struct.pack("<I", len(head)) + head + frame[_end:]


def _header_text(rng, frame, _pool):
    bounds = _header_bounds(frame)
    if bounds is None:
        return frame
    start, end = bounds
    try:
        text = frame[start:end].decode("utf-8")
    except UnicodeDecodeError:
        return frame
    mutations = [
        lambda value: value.replace('"f32"', '"f64"', 1),
        lambda value: value.replace('"dtype":"u32"', '"dtype":"u8"', 1),
        lambda value: value.replace('"bytes":4', '"bytes":3', 1),
        lambda value: value.replace('"v":2', '"v":3', 1),
        lambda value: value.replace('"type":"response"', '"type":""', 1),
        lambda value: value.replace('"name":"mesh.verts"', '"name":"mesh.other"', 1),
        lambda value: value.replace('"name":"attr.0.values"', '"name":"attr.1.values"', 1),
        lambda value: value.replace('"shape":[1]', '"shape":[2]', 1),
        lambda value: value.replace(":", ",", 1),
        lambda value: value[: rng.randrange(len(value) + 1)],
        lambda value: value + '{"a":1,"a":2}',
        lambda value: value.replace('"v":2,', '"v":2,"note":"\\ud800",', 1),
        lambda value: value.replace('"v":2,', '"v":2,"note":"line\nbreak",', 1),
        lambda value: value.replace('"sections"', '"sections":{},"old_sections"', 1),
        lambda value: value + ' ',
        lambda value: value[:1] + value[2:],
        lambda value: '[' * 80 + value + ']' * 80,
    ]
    head = rng.choice(mutations)(text).encode("utf-8")
    if rng.random() < 0.2:
        # Explicitly malformed UTF-8 in the header, not only JSON syntax.
        cut = rng.randrange(len(head) + 1)
        head = head[:cut] + b"\xff" + head[cut:]
    return _rebuild_header_bytes(frame, head)


def _structured_header(rng, frame, _pool):
    bounds = _header_bounds(frame)
    if bounds is None:
        return frame
    start, end = bounds
    try:
        import json

        header = json.loads(frame[start:end].decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return frame
    if not isinstance(header, dict):
        return frame
    header = json.loads(codec.canonical_json(header))

    change = rng.randrange(9)
    if change == 0:
        header["v"] = rng.choice([1, 3, 2.0, True, None, "2", []])
    elif change == 1:
        header["type"] = rng.choice(["", None, 5, [], {}])
    elif change == 2:
        header.pop("type", None)
    elif change == 3:
        header["sections"] = rng.choice([None, {}, "sections", 7, True])
    elif change == 4:
        table = header.get("sections")
        if isinstance(table, list) and table:
            entry = table[rng.randrange(len(table))]
            if isinstance(entry, dict):
                entry[rng.choice(["name", "dtype", "bytes", "shape"])] = rng.choice(
                    [None, "", -1, 0, 4.0, True, "x", [], {}, 10**20]
                )
    elif change == 5:
        table = header.get("sections")
        if isinstance(table, list) and table:
            table.append(dict(table[0]))
    elif change == 6:
        table = header.get("sections")
        if isinstance(table, list) and table:
            entry = table[0]
            if isinstance(entry, dict):
                entry["bytes"] = rng.choice([10**9, 10**18, codec.INT64_MAX, -1])
                entry["shape"] = rng.choice([[0], [codec.INT64_MAX], [2, 2], [], None])
    elif change == 7:
        header["sections"] = [
            {"name": rng.choice(["a", "", None, 5]), "dtype": rng.choice(["f32", "f64", None, 5]),
             "bytes": rng.choice([0, 1, 4, -4, 10**18, "4"]), "shape": rng.choice([None, [], [2], "2"])}
        ]
    else:
        header["extra"] = rng.choice([None, True, 0, -1, 1e100, "x", [], {}])
    try:
        head = codec.canonical_json(header).encode("utf-8")
    except (TypeError, ValueError):
        head = b"not json"
    return _rebuild_header_bytes(frame, head)


def _splice(rng, frame, pool):
    other = pool[rng.randrange(len(pool))]
    frame_bounds = _header_bounds(frame)
    other_bounds = _header_bounds(other)
    if frame_bounds is None or other_bounds is None:
        return frame
    _start, frame_end = frame_bounds
    other_start, other_end = other_bounds
    head = frame[4:frame_end]
    body = other[other_end:]
    return struct.pack("<I", len(head)) + head + body + frame[frame_end:other_start]


def _oversized_declaration(rng, frame, _pool):
    bounds = _header_bounds(frame)
    if bounds is None:
        return frame
    start, end = bounds
    try:
        import json

        header = json.loads(frame[start:end].decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return frame
    if not isinstance(header, dict):
        return frame
    entry = {
        "name": "mesh.verts",
        "dtype": "f32",
        "bytes": rng.choice([10**18, codec.INT64_MAX, 10**30]),
        "shape": rng.choice([[10**18], [codec.INT64_MAX, 2], [10**30]]),
    }
    header["sections"] = [entry, dict(entry, name="mesh.more")]
    head = codec.canonical_json(header).encode("utf-8")
    return struct.pack("<I", len(head)) + head + frame[end:]


def _section_surgery(rng, frame, _pool):
    bounds = _header_bounds(frame)
    if bounds is None:
        return frame
    start, end = bounds
    try:
        import json

        header = json.loads(frame[start:end].decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return frame
    if not isinstance(header, dict):
        return frame
    table = header.get("sections")
    if not isinstance(table, list) or not table:
        return frame
    choice = rng.randrange(5)
    if choice == 0:
        table[rng.randrange(len(table))]["bytes"] = 0
    elif choice == 1:
        table.append(table[rng.randrange(len(table))])
    elif choice == 2:
        table[rng.randrange(len(table))]["dtype"] = rng.choice(["u8", "u32", "i32"])
    elif choice == 3:
        del table[rng.randrange(len(table))]
    else:
        table.reverse()
    head = codec.canonical_json(header).encode("utf-8")
    body = frame[end:]
    if rng.random() < 0.5:
        body = body[: rng.randrange(len(body) + 1)]
    return struct.pack("<I", len(head)) + head + body


_STRATEGIES = (
    _length_prefix,
    _flip_byte,
    _truncate,
    _append_junk,
    _random_frame,
    _header_text,
    _structured_header,
    _splice,
    _oversized_declaration,
    _section_surgery,
)


def _check_decoded(frame, header, sections):
    """Assert a successful decode is internally coherent and invented nothing."""
    assert isinstance(header, dict), "header is not an object"
    assert header.get("v") == 2, "decoded v is not 2"
    assert isinstance(header.get("type"), str) and header["type"], "decoded type is invalid"
    table = header.get("sections", [])
    assert isinstance(table, list), "decoded sections table is not a list"
    assert len(sections) == len(table), "decoded section count differs from the table"
    assert list(sections) == [entry["name"] for entry in table], "section order/names differ"

    (head_len,) = struct.unpack_from("<I", frame, 0)
    body = memoryview(frame)[4 + head_len:]
    offset = 0
    rebuilt = []
    for entry in table:
        name = entry["name"]
        dtype = entry["dtype"]
        nbytes = entry["bytes"]
        values = sections[name]
        item_size = codec._DTYPE_FMT[dtype][1]
        assert isinstance(values, tuple), "decoded values are not a tuple"
        assert len(values) * item_size == nbytes, "decoded element count disagrees with bytes"
        raw = codec.section_bytes(name, dtype, values)
        assert raw == body[offset:offset + nbytes].tobytes(), "decoded bytes differ from the payload"
        offset += nbytes
        rebuilt.append((name, dtype, values))
    assert offset == len(body), "decoded sections do not cover the payload"
    rebuilt_frame = codec.encode_frame(header, rebuilt)
    header_again, sections_again = codec.decode_frame(rebuilt_frame)
    assert list(sections_again) == list(sections), "re-encode changed the section names"
    for name, dtype, _values in rebuilt:
        assert (
            codec.section_bytes(name, dtype, sections_again[name])
            == codec.section_bytes(name, dtype, sections[name])
        ), "re-encode changed section bytes"
    assert header_again.get("type") == header["type"], "re-encode changed the header type"


def run_frame_fuzz(cases=FUZZ_CASES, seed=FUZZ_SEED):
    """Run the deterministic mutation campaign; return outcome counts.

    Exposed as a function so the iteration count and seed are visible and the
    test itself only asserts the returned ledger.
    """
    rng = random.Random(seed)
    pool = _valid_frames()
    codes = {code: 0 for code in codec.CODEC_CODES}
    strategy_counts = [0] * len(_STRATEGIES)
    accepted = 0
    for case in range(cases):
        strategy_index = case % len(_STRATEGIES)
        frame = _STRATEGIES[strategy_index](rng, pool[rng.randrange(len(pool))], pool)
        strategy_counts[strategy_index] += 1
        # A second, independent mutation broadens the combinations while every
        # case remains reproducible from (seed, case).
        if rng.random() < 0.35:
            second = _STRATEGIES[rng.randrange(len(_STRATEGIES))]
            frame = second(rng, frame, pool)
        try:
            header, sections = codec.decode_frame(frame)
        except codec.CodecError as exc:
            if exc.code not in codec.CODEC_CODES:
                raise AssertionError(
                    "case %d (seed %d, %s): unclassified code %r"
                    % (case, seed, _STRATEGIES[strategy_index].__name__, exc.code)
                )
            codes[exc.code] += 1
            continue
        except BaseException as exc:  # noqa: BLE001 - the fuzz asserts no escape
            raise AssertionError(
                "case %d (seed %d, %s): unclassified %s: %s; frame=%s"
                % (
                    case,
                    seed,
                    _STRATEGIES[strategy_index].__name__,
                    type(exc).__name__,
                    exc,
                    frame.hex(),
                )
            )
        try:
            _check_decoded(frame, header, sections)
        except AssertionError as exc:
            raise AssertionError(
                "case %d (seed %d, %s): incoherent success: %s; frame=%s"
                % (
                    case,
                    seed,
                    _STRATEGIES[strategy_index].__name__,
                    exc,
                    frame.hex(),
                )
            )
        accepted += 1

    return {
        "cases": cases,
        "seed": seed,
        "accepted": accepted,
        "codes": codes,
        "strategies": {
            _STRATEGIES[index].__name__: count
            for index, count in enumerate(strategy_counts)
        },
    }


class CodecFuzzTests(unittest.TestCase):
    def test_ten_thousand_mutated_frames_are_classified_or_coherent(self):
        """10,000 fixed-seed mutations: typed codes only, coherent successes."""
        result = run_frame_fuzz()
        self.assertEqual(result["cases"], FUZZ_CASES)
        self.assertEqual(result["seed"], FUZZ_SEED)
        self.assertGreater(result["accepted"], 0, "the campaign never accepted a valid mutation")
        self.assertEqual(sum(result["codes"].values()) + result["accepted"], FUZZ_CASES)
        for code, count in result["codes"].items():
            self.assertIn(code, codec.CODEC_CODES)
            self.assertGreater(count, 0, "no case exercised code %r" % (code,))
        for strategy, count in result["strategies"].items():
            self.assertGreater(count, 0, "strategy %r never ran" % (strategy,))


if __name__ == "__main__":
    unittest.main()
