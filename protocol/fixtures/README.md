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

## The fixtures

| Name | Covers |
|---|---|
| `ping` | a message with no sections and a float field |
| `hello` | the stage list, including a disabled stage |
| `schema` | float and value-list parameters, with ranges and values |
| `request-ref` | a parameter-only request (`geom.mode = "ref"`, no sections) |
| `request-mesh` | `mesh.*` sections including colours and crease edges |
| `request-subd` | `subd.*` sections with a crease |
| `response-mesh` | a two-triangle mesh response |
| `response-mixed` | mesh + curves + points in one response, with a warning |
| `response-error` | a solve failure (`status = "error"`) |
| `error-unknown-ref` | the `error` message type for an unknown geometry ref |

## Float values

Fixture float values are exactly representable in f32 (0.0, 0.5, 0.25, 1.0, …), so
both codecs agree byte for byte without a rounding tolerance. Header floats are
formatted the way Python's `repr` does for the ranges the protocol uses: shortest
round-trip text, with `.0` appended to integral values (`2.0`, not `2`). The C#
writer in `grasshopper/src/core/Json.cs` matches that; scientific-notation ranges
are not exercised by the fixtures.
