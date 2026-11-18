"""The protocol vocabulary has one checked-in source and generated tables.

``protocol/sections.json`` owns the version, dtypes, hash order, attribute
types, warning codes, codec error codes and fixture names. This suite checks
that the generated Python constants, the live production imports, the fixture
directories and the checked-in C#/Python tables all agree, and that the drift
check fails on a mutated source or fixture inventory.
"""

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from blender.stageloom.core import _protocol_tables as tables  # noqa: E402
from blender.stageloom.core import attributes, codec, hashing  # noqa: E402

SOURCE = ROOT / "protocol" / "sections.json"
FIXTURE_ROOT = ROOT / "protocol" / "fixtures"
GENERATOR = ROOT / "scripts" / "generate-protocol-tables"
PYTHON_TABLE = ROOT / "blender" / "stageloom" / "core" / "_protocol_tables.py"
CSHARP_TABLE = ROOT / "grasshopper" / "src" / "core" / "ProtocolTables.generated.cs"


def load_generator():
    loader = SourceFileLoader("generate_protocol_tables", str(GENERATOR))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class ProtocolSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = json.loads(SOURCE.read_text(encoding="utf-8"))
        cls.generator = load_generator()

    def test_source_is_canonical_and_valid(self):
        # load_source enforces key order, sorted inventories and cross-checks.
        validated = self.generator.load_source(SOURCE)
        self.assertEqual(validated, self.source)
        self.assertEqual(
            tuple(self.source.keys()), tuple(sorted(self.source.keys()))
        )

    def test_generated_python_matches_source(self):
        source = self.source
        self.assertEqual(tables.PROTOCOL_VERSION, source["version"])
        self.assertEqual(
            tables.DTYPE_FORMATS,
            {
                row["name"]: (row["struct"], row["bytes"])
                for row in source["dtypes"]
            },
        )
        self.assertEqual(
            tables.NUMPY_DTYPES,
            {row["name"]: row["numpy"] for row in source["dtypes"]},
        )
        self.assertEqual(
            tables.HASH_SECTION_ORDER, tuple(source["hash_section_order"])
        )
        self.assertEqual(
            tables.WIRE_TYPES,
            {
                row["type"]: (row["dtype"], row["components"])
                for row in source["attribute_types"]
            },
        )
        self.assertEqual(tables.CODEC_CODES, tuple(source["codec_error_codes"]))

    def test_live_python_tables_match_source(self):
        self.assertEqual(codec.PROTOCOL_VERSION, self.source["version"])
        self.assertEqual(
            codec.CODEC_CODES, tuple(self.source["codec_error_codes"])
        )
        self.assertEqual(
            codec._DTYPE_FMT,
            {
                row["name"]: (row["struct"], row["bytes"])
                for row in self.source["dtypes"]
            },
        )
        self.assertEqual(
            codec._NP_DTYPE,
            {row["name"]: row["numpy"] for row in self.source["dtypes"]},
        )
        self.assertEqual(
            hashing.HASH_SECTION_ORDER, tuple(self.source["hash_section_order"])
        )
        self.assertEqual(
            attributes.WIRE_TYPES,
            {
                row["type"]: (row["dtype"], row["components"])
                for row in self.source["attribute_types"]
            },
        )

    def test_fixture_inventory_matches_directories(self):
        directories = sorted(
            path.name for path in FIXTURE_ROOT.iterdir() if path.is_dir()
        )
        names = [row["name"] for row in self.source["fixtures"]]
        self.assertEqual(names, directories)
        for name in names:
            with self.subTest(fixture=name):
                self.assertTrue((FIXTURE_ROOT / name / "frame.bin").is_file())
                self.assertTrue((FIXTURE_ROOT / name / "expect.json").is_file())

    def test_csharp_table_carries_the_source_vocabulary(self):
        text = CSHARP_TABLE.read_text(encoding="utf-8")
        self.assertIn(
            "public const int ProtocolVersion = %d;" % (self.source["version"],),
            text,
        )
        for row in self.source["attribute_types"]:
            self.assertIn(
                'new ProtocolAttributeType("%s", "%s", %d, "%s")'
                % (row["type"], row["dtype"], row["components"], row["label"]),
                text,
            )
        for row in self.source["warning_codes"]:
            self.assertIn(
                'public const string %s = "%s";' % (row["name"], row["value"]),
                text,
            )
        for name in self.source["hash_section_order"]:
            self.assertIn('"%s"' % (name,), text)


class ProtocolDriftTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = json.loads(SOURCE.read_text(encoding="utf-8"))

    def _temp_tree(self, root):
        """A minimal checkout: source, both generated tables and fixture dirs."""
        protocol = Path(root) / "protocol"
        fixtures = protocol / "fixtures"
        fixtures.mkdir(parents=True)
        shutil.copyfile(SOURCE, protocol / "sections.json")
        for relative in (
            PYTHON_TABLE.relative_to(ROOT),
            CSHARP_TABLE.relative_to(ROOT),
        ):
            target = Path(root) / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, target)
        for row in self.source["fixtures"]:
            folder = fixtures / row["name"]
            folder.mkdir()
            (folder / "frame.bin").write_bytes(b"")
            (folder / "expect.json").write_text("{}\n", encoding="utf-8")
        return Path(root)

    def _run_check(self, root):
        return subprocess.run(
            [sys.executable, str(GENERATOR), "--check", "--root", str(root)],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
        )

    def test_check_accepts_the_current_tree(self):
        result = self._run_check(ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("check-protocol-tables: ok", result.stdout)

    def test_mutated_source_fails_the_drift_check(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._temp_tree(temp)
            source_path = root / "protocol" / "sections.json"
            mutated = json.loads(source_path.read_text(encoding="utf-8"))
            mutated["version"] += 1
            source_path.write_text(
                json.dumps(mutated, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            result = self._run_check(root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("generated table is stale", result.stderr)

    def test_missing_fixture_directory_fails_the_drift_check(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._temp_tree(temp)
            victim = self.source["fixtures"][0]["name"]
            shutil.rmtree(root / "protocol" / "fixtures" / victim)
            result = self._run_check(root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing directories", result.stderr)
        self.assertIn(victim, result.stderr)


if __name__ == "__main__":
    unittest.main()
