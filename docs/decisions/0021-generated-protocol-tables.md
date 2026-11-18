# 0021 — One source for the protocol tables, generated outputs and drift checks

**Status:** Accepted, 2026-11-18 (0.8.0 T9b).

## Context

The same protocol facts lived in several hand-written places: the version and
dtype formats in `blender/stageloom/core/codec.py` and `grasshopper/src/core/Frame.cs`,
the geometry-hash order in `codec.py`/`hashing.py` and `Frame.cs`, the 14
attribute types in `core/attributes.py` and `AttributeStore.Types`, the warning
codes in `Conversion.cs`, the codec error codes in Python, and the fixture names
in `protocol/fixtures/README.md` and the fixture tests. A change to one copy
could leave the other language, the docs or the fixture inventory silently
wrong; the codecs would then accept different frames or hash them differently.

## Decision

- **`protocol/sections.json` is the hand-written single source.** It is canonical
  JSON (sorted object keys; sorted dtype, attribute-type, warning-code and
  fixture arrays) and owns:
  - the protocol version;
  - the dtypes (`f32`, `i32`, `u32`, `u8`) with byte size, struct format and
    numpy dtype;
  - the fixed geometry-hash section order;
  - the attribute types (wire dtype, components, UI label);
  - the warning code names and wire values;
  - the codec error codes (`CODEC_CODES`);
  - the fixture inventory and a short coverage note per fixture.
  The fixture directories remain the source of truth for the bytes; the source
  owns their names.
- **`scripts/generate-protocol-tables` generates both language projections.**
  It is pure Python 3.13 standard library and writes:
  - `blender/stageloom/core/_protocol_tables.py` with the constants Python
    production consumes (`PROTOCOL_VERSION`, `DTYPE_FORMATS`, `NUMPY_DTYPES`,
    `HASH_SECTION_ORDER`, `WIRE_TYPES`, `CODEC_CODES`);
  - `grasshopper/src/core/ProtocolTables.generated.cs` with the C# 5 projection:
    `ProtocolTables` (version, dtype sizes/known set, hash order, attribute rows)
    and the `WarningCodes` partial class.
  Both files carry a generated header. They are production files but are never
  edited by hand.
- **Production consumes the generated tables.** `codec.py`, `attributes.py` and
  `hashing.py` import them while keeping their existing public names (`PROTOCOL_VERSION`,
  `CODEC_CODES`, `WIRE_TYPES`, `HASH_SECTION_ORDER`, `_DTYPE_FMT`, `_NP_DTYPE`).
  `FrameCodec.ProtocolVersion` and `FrameCodec.HashSectionOrder` remain the
  public C# API and now alias the generated values; dtype validation and item
  sizes call `ProtocolTables`. `AttributeStore.Types` is built from the generated
  attribute rows plus the small hand-written `StorageKind` selection; the
  generated `WarningCodes` class replaces the hand-written literals in
  `Conversion.cs`.
- **Drift is a gate.** `scripts/check-protocol-tables` regenerates in memory,
  compares both files byte for byte, and validates the source inventory against
  the fixture directories (including `frame.bin`/`expect.json`). It needs no
  Blender, Rhino or network. `scripts/check-all` runs it in the quick and full
  gates, and the stock-runner `core` CI job runs it before the core tests.
- **Docs and tests check the same source.** `scripts/check-docs` reads
  `protocol/sections.json`, requires its names to match the fixture directories,
  and requires the `protocol/fixtures/README.md` table to match the source. The
  Python core suite compares the source with the generated and live Python
  tables and proves that a mutated source or a missing fixture directory fails
  the drift check. The C# console suite pins the generated version, dtype sizes,
  hash order, attribute rows and warning values.

## Consequences

- Changing a protocol fact is one source edit plus
  `scripts/generate-protocol-tables`; forgetting either generated file, a
  fixture name or a docs table fails locally and in CI.
- No wire behavior changes: the source reproduces the exact version, dtype
  formats, hash order, attribute map, warning values and fixture names that were
  already shipped. Fixture bytes and the Python `sections_hash`/output hashes are
  unchanged; the C# `OutputHash`/`GeometryHash` algorithms were not edited.
- The generated Python file only projects what Python production uses; Python
  does not gain unused warning or fixture constants. The C# file projects what
  the shipped plugin consumes.
- The generator is deterministic and dependency-free. It validates canonical
  key order, sorted inventories, dtype/struct/numpy consistency, attribute dtypes
  and the fixture directory list before writing.
- Residual hand-written protocol literals are deliberate: the historical
  `RID_DOMAIN = b"gh-stack/rid/v2"` must not change with a table edit (saved
  request IDs depend on it), the hashing/framing algorithms remain code, and
  protocol prose in `docs/protocol.md` stays prose. The generated tables own the
  vocabulary, not the algorithms.
