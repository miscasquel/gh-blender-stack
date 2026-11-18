# Golden frames

Each fixture is a folder `<name>/` with:

- `frame.bin`: one encoded protocol frame (see `docs/protocol.md` §3), canonical
  header encoding.
- `expect.json`: the decoded header, per-section metadata (`dtype`, `shape`,
  `bytes`, `sha256` of the raw little-endian section bytes), the `sha256` of the
  whole frame, and `sections_hash` when the fixture has sections. Hashes carry the
  protocol's `sha256:` prefix.

Both the Python and C# codecs must decode every fixture to the same values, and
encode the expected values back to the same bytes. That is checked by:

```
scripts/test-core          # Python: decode, compare, re-encode, hash
scripts/test-gha           # C# under wine: the same checks via the console runner
```

Regenerate the fixtures (only needed when the protocol adds a message or a section
type; an uncommitted diff means a codec change broke the canonical form):

```
python3 scripts/make-fixtures
```

`protocol/sections.json` owns the fixture names and their coverage notes; this
table is a checked mirror. `scripts/check-protocol-tables` validates the source
against these directories, and `scripts/check-docs` validates this table against
the source. The directories remain the source of truth for the bytes.

## The fixtures

| Name | Covers |
|---|---|
| `ping` | a message with no sections and a float field |
| `hello` | the stage list, including a disabled stage |
| `schema` | float and value-list parameters, with ranges and values |
| `request-ref` | a parameter-only request (`geom.mode = "ref"`, no sections) |
| `request-mesh` | `mesh.*` sections including colours and crease edges |
| `request-subd` | `subd.*` sections with a crease |
| `request-instances` | block definitions and instances, including a nested placement and a per-definition point attribute |
| `request-instances-multipart` | a two-part definition (`parts: 2`), both part namespaces, an attribute on part 1, a definition-level nested row and a single-part definition that omits `parts` |
| `response-mesh` | a two-triangle mesh response |
| `response-mixed` | mesh + curves + points in one response, with a warning |
| `response-attributes` | attributes: `i32` face ids, `f32` point loads, boolean flags, an UTF-8 string tag with its offsets section |
| `response-instances-multipart` | the response direction of the two-part definition: an attribute on part 0, a nested row and a top-level placement |
| `response-error` | a solve failure (`status = "error"`) |
| `error-unknown-ref` | the `error` message type for an unknown geometry ref |

## Float values

Fixture float values are exactly representable in f32 (0.0, 0.5, 0.25, 1.0, …), so
both codecs agree byte for byte without a rounding tolerance.

Header floats are formatted as Python's `repr` for the full finite double range:
shortest round-trip digits, fixed notation for decimal exponents `-4..15`,
scientific outside, a lowercase exponent with at least two digits, `.0` on
integral fixed values, and `-0.0` for negative zero. The C# formatter derives
the digits from the exact IEEE 754 value rather than Mono's `R`/`G17`, which
diverge at `1e15`, `-0.0` and some 16–17-digit values.

`protocol/number-corpus.json` is the static cross-language contract for this:
an array of `{"bits": "<16 hex IEEE 754>", "text": "<Python repr>"}` entries
covering the notation thresholds, subnormals, the finite extremes, exact
decimal ties and deterministic random doubles. `scripts/make-number-corpus`
regenerates it; the Python core test and the C# console test both validate every
entry. Regenerate it only for a deliberate contract change.
