"""The cross-language double-format contract.

protocol/number-corpus.json is static: each entry is a finite IEEE 754 double's
16-hex-digit bit pattern and Python's repr of that double. This test is the
Python half; grasshopper/tests/console/Program.cs checks Json.FormatDouble
against the same entries. A change to either language's float text must
regenerate the corpus deliberately, not silently move the goalposts.
"""

import json
import math
import os
import re
import struct
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from blender.stageloom.core import codec  # noqa: E402

CORPUS_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "protocol", "number-corpus.json")
)

HEX_BITS = re.compile(r"^[0-9a-f]{16}$")


def bits_of(value):
    return "%016x" % struct.unpack(">Q", struct.pack(">d", value))[0]


def value_of(bits):
    return struct.unpack(">d", bytes.fromhex(bits))[0]


class NumberCorpusTests(unittest.TestCase):
    def load(self):
        with open(CORPUS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        self.assertIsInstance(data, list)
        return data

    def test_every_entry_is_python_repr(self):
        data = self.load()
        self.assertGreaterEqual(len(data), 100)
        self.assertEqual(len({entry["bits"] for entry in data}), len(data))
        for entry in data:
            with self.subTest(bits=entry.get("bits")):
                self.assertEqual(set(entry), {"bits", "text"})
                self.assertRegex(entry["bits"], HEX_BITS)
                value = value_of(entry["bits"])
                self.assertTrue(math.isfinite(value), "non-finite corpus value")
                self.assertEqual(entry["text"], repr(value))
                # The canonical writer uses the same repr, so a header float
                # is byte-identical to the corpus text Python produces.
                self.assertEqual(codec.canonical_json(value), entry["text"])

    def test_corpus_covers_required_anchors(self):
        data = self.load()
        text_by_bits = {entry["bits"]: entry["text"] for entry in data}
        required = {
            bits_of(0.0): "0.0",
            bits_of(-0.0): "-0.0",
            bits_of(1.0): "1.0",
            bits_of(-1.0): "-1.0",
            bits_of(1e-4): "0.0001",
            bits_of(1e-5): "1e-05",
            bits_of(1e15): "1000000000000000.0",
            bits_of(1e16): "1e+16",
            bits_of(1e21): "1e+21",
            bits_of(0.1): "0.1",
            bits_of(0.2 + 0.1): "0.30000000000000004",
            bits_of(12345678901234568.0): "1.2345678901234568e+16",
            bits_of(math.pi): "3.141592653589793",
            bits_of(2.0**53): "9007199254740992.0",
            bits_of(2.0**63): "9.223372036854776e+18",
            "0000000000000001": "5e-324",
            "7fefffffffffffff": "1.7976931348623157e+308",
        }
        for bits, text in required.items():
            with self.subTest(bits=bits):
                self.assertIn(bits, text_by_bits)
                self.assertEqual(text_by_bits[bits], text)


if __name__ == "__main__":
    unittest.main()
