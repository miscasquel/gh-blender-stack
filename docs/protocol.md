# GH Stack protocol v2

**Status:** draft (M0). Frozen at M2, after spikes S1–S4 have run. Until then, changes are made here and recorded in the changelog at the end.
**Scope:** the messages between the Blender add-on (`gh_stack`, WebSocket server) and the Grasshopper plugin (`GhStack.gha`, WebSocket client).

---

## 1. Overview

- Blender listens on `127.0.0.1:9130` (configurable). GH connects.
- Each message is one binary WebSocket message: a small JSON header describing the message, followed by raw binary sections for geometry.
- Blender owns identity and caching. It computes geometry hashes and request IDs. GH echoes them back and never computes them.
- Parameters travel as JSON values in the header. Geometry travels as binary sections.
- Nothing is polled. Both sides push.

## 2. Transport

- Library on the Blender side: `websockets` 17.2 (pure Python). The server runs on a background thread. Blender's main thread reads a queue.
- Library on the GH side: `System.Net.WebSockets.ClientWebSocket`, on a background thread.
- Loopback only. No authentication in v2. Connections from non-loopback addresses are refused.
- Maximum message size: 512 MiB. Maximum header size: 1 MiB.
- Keepalive: `ping` every 2 s from Blender. No `pong` within 6 s means `disconnected`.

## 3. Frame

```
+-----------------------+-------------------------+----------------------+
| u32 little-endian     | header: UTF-8 JSON      | sections: raw bytes  |
| header length (bytes) | (no BOM, canonical)     | in header order      |
+-----------------------+-------------------------+----------------------+
```

- All numbers in binary sections are little-endian. Floats are IEEE 754 single precision (`f32`) unless stated.
- The header is a JSON object with at least `type` and `v`. A `sections` array may be present (see §5). Each entry gives `name`, `dtype`, `shape`, and `bytes`.
- The sum of `bytes` over all sections must equal the length of the payload after the header. Anything else is a `bad_frame` error.
- Canonical header encoding: UTF-8, keys sorted, no insignificant whitespace (`separators=(",", ":")` in Python; the equivalent in C#). Fixtures depend on this.

## 4. Message types

Every message carries `"v": 2`. A message with a different `v` is answered with `error` code `proto_mismatch` and the connection is closed.

### 4.1 `hello` (GH → Blender)

Sent once per connection, first.

```json
{"type":"hello","v":2,"client":"GhStack 0.1.3","doc":"Panels.gh",
 "doc_units":"Millimeters",
 "stages":[{"key":"panelize-3f9a","enabled":true}]}
```

- `stages` lists every Stage In or Stage Out in the document, with its key and enabled flag.
- `doc` is the document's file name. It is for display only.

### 4.2 `schema` (GH → Blender)

Sent after `hello`, and again whenever a Param component's declaration changes.

```json
{"type":"schema","v":2,"stage":"panelize-3f9a","params":[
  {"id":"6f1c…guid…","label":"Radius","type":"float",
   "default":2.0,"min":0.0,"max":10.0,"step":0.01,"values":[],"group":"Shape"}]}
```

- `type` is one of: `float`, `int`, `bool`, `vector3`, `color`, `value_list`, `text`.
- `values` is used only for `value_list`: the entries offered as a dropdown in Blender (a list of strings). The parameter value is the index into that list, so the wire value is an integer.
- `id` is a GUID generated once per Param component. It never changes, so keyframes survive renames.
- Removed parameters are omitted from later `schema` messages. Blender moves their keys to the orphan list. It does not delete them.

### 4.3 `request` (Blender → GH)

```json
{"type":"request","v":2,"stage":"panelize-3f9a",
 "rid":"sha256:…","nonce":0,
 "params":{"6f1c…guid…":2.5,"0b7e…guid…":3},
 "geom":{"mode":"full","hash":"sha256:…"},
 "units":"m","space":"stack-local",
 "sections":[ … ]}
```

- `params` maps parameter `id` to a value cast to the schema type before encoding: floats as JSON numbers, ints as integers, bools as `true`/`false`, vectors and colors as arrays of three numbers, value lists as integers (the index), text as strings.
- `attributes` (optional) lists the attribute sections the request carries (§5.1). It is present only with `sections`, so a `ref` request never carries it.
- `geom.mode` is `full` or `ref`.
  - `full`: `sections` carries the geometry (§5). `hash` is the geometry hash.
  - `ref`: `sections` is empty. `hash` names geometry GH already holds for this stage. If GH does not hold it, GH answers `unknown_ref` and Blender resends `full`.
- `units` is always `m`. GH converts to and from document units.
- `space` is always `stack-local`. The geometry is in the stack object's local coordinates.

### 4.4 `response` (GH → Blender)

```json
{"type":"response","v":2,"stage":"panelize-3f9a","rid":"sha256:…",
 "out_hash":"sha256:…","status":"ok","error":null,
 "warnings":[{"code":"subd_creases","message":"creases transferred as tags; semi-sharp and vertex creases are not available"}],
 "counts":{"meshes":1,"subd":0,"curves":0,"points":0,"approximated_curves":0},
 "sections":[ … ]}
```

- `rid` echoes the request's `rid`. For a GH-initiated change (a slider upstream of Stage Out moved), `rid` is the last one received for that stage.
- `status` is `ok` or `error`. An `error` response has no sections, and `error` holds `{"code": …, "message": …}`.
- `out_hash` identifies the output. Blender compares it for equality and never recomputes it (see §6.3).
- `attributes` (optional) lists the attribute sections the response carries (§5.1), exactly like a request's.
- `warnings` carries every loss or approximation, each with a code and a message. Blender shows them in the stage status and logs them.

### 4.5 `ping` / `pong`

```json
{"type":"ping","v":2,"t":1234.5}
{"type":"pong","v":2,"t":1234.5}
```

`t` is echoed unchanged.

### 4.6 `error`

```json
{"type":"error","v":2,"code":"unknown_ref","message":"no geometry for hash","stage":"panelize-3f9a","rid":"sha256:…"}
```

`stage` and `rid` are optional.

**Error codes**

| Code | Meaning | Sender's recovery |
|---|---|---|
| `proto_mismatch` | `v` differs | Close the connection |
| `bad_frame` | Header or sections inconsistent | Drop the message, log |
| `unknown_stage` | Stage key not present in the document | Show error on the stage |
| `unknown_ref` | `ref` for geometry GH does not hold | Blender resends `full` |
| `duplicate_stage` | Two documents claim one key | Both stages to `error`, naming the other document |
| `gh_solve_failed` | The GH document raised an error during solve | Keep last good output, show the message |
| `too_large` | Message exceeds the size limit | Reject |

## 5. Sections

Sections come in two groups. Plain meshes use `mesh.*`. SubD cages use `subd.*`. A message can contain both. Names, dtypes, and shapes are fixed:

| Name | dtype | Shape | Meaning |
|---|---|---|---|
| `mesh.verts` | f32 | [N,3] | Positions |
| `mesh.face_counts` | u32 | [F] | Vertices per face (3, 4, or more) |
| `mesh.face_indices` | u32 | [sum of face_counts] | Vertex indices |
| `mesh.colors` | u8 | [N,4] | Optional vertex colors (RGBA) |
| `mesh.crease_edges` | u32 | [E,2] | Optional edge vertex pairs |
| `mesh.crease_values` | f32 | [E] | Crease value per edge in `mesh.crease_edges` |
| `subd.verts` | f32 | [N,3] | SubD control cage positions |
| `subd.face_counts` | u32 | [F] | Vertices per cage face |
| `subd.face_indices` | u32 | [sum of face_counts] | Cage vertex indices |
| `subd.crease_edges` | u32 | [E,2] | Optional edge pairs |
| `subd.crease_values` | f32 | [E] | Crease value per edge: 1.0 crease, 0.0 smooth (D2) |
| `crv.points` | f32 | [M,4] | x, y, z, weight |
| `crv.counts` | u32 | [C] | Points per curve |
| `crv.types` | u8 | [C] | 0 poly, 1 Bezier, 2 NURBS |
| `crv.orders` | u8 | [C] | NURBS order (0 for other types) |
| `crv.knots_mode` | u8 | [C] | 0 normal, 1 endpoint, 2 Bezier (NURBS only) |
| `crv.cyclic` | u8 | [C] | 0 or 1 |
| `pts.positions` | f32 | [P,3] | |
| `pts.radii` | f32 | [P] | Optional |
| `pts.colors` | u8 | [P,4] | Optional RGBA |

Rules:
- A section is present only if it has data. Absent sections mean "none".
- `*.face_indices` length equals the sum of the matching `*.face_counts`.
- Curve `crv.points` length equals the sum of `crv.counts`.
- Blender sends `subd.*` for a mesh its stack object flags as SubD (the `gh_subd` marker, §6 of the plan). Otherwise it sends `mesh.*`.
- `subd.colors` does not exist in v2.
- `i32` is the signed 32-bit dtype, four bytes per element. It exists for Blender's signed `INT` attribute type.

### 5.1 Attributes

Attributes are per-element named arrays attached to one geometry group. Each
attribute contributes one `attr.<i>.values` section plus one entry in the frame's
`attributes` header list:

```json
{"group":"mesh","name":"panel_id","domain":"FACE","type":"INT",
 "components":1,"count":2,"section":"attr.0.values"}
```

- `group` is the geometry group it belongs to: `mesh`, `subd`, `crv`, or `pts`.
- `name` is the attribute's name; `domain` is `POINT`, `EDGE`, `FACE`, `CORNER`,
  `CURVE` or `INSTANCE`; `type` is Blender's attribute vocabulary: `FLOAT`,
  `INT`, `BOOLEAN`, `STRING`, `FLOAT_VECTOR`, `FLOAT2`, `FLOAT4`, `FLOAT_COLOR`,
  `BYTE_COLOR`, `QUATERNION`, `FLOAT4X4`, `INT8`, `INT16_2D`, `INT32_2D`.
- `components` is the values per element, `count` the elements. The section dtype
  follows from the type: `f32` for floats and compound types, `i32` for
  `INT`/`INT8`/`INT16_2D`/`INT32_2D`, `u8` for `BOOLEAN` (0/1) and `BYTE_COLOR`.
- STRING adds an `attr.<i>.offsets` section (u32 byte lengths, one per string);
  `attr.<i>.values` is the concatenated UTF-8 bytes.
- The list is present only when its sections are: no geometry, no attributes.
- Platforms map what they can natively. Blender carries the active colour
  attribute and the edge creases through `mesh.colors` / the crease sections, and
  everything else through this format; Rhino attaches each attribute to the
  geometry object (UserDictionary), so it rides along with duplication and
  transforms.

### 5.2 Instances

Instances are Blender's instance rows (a `GeometrySet`'s instances component) and
Rhino's block instances. A frame's rows reference *definitions* — reusable
geometry — carried in the same frame, so one definition crosses once however
many rows use it. Nesting is a DAG: a definition's own rows reference other
definitions in the same table by index.

The header carries the definition table, one entry per definition, in index
order:

```json
"definitions": [{"name":"Tree"}, {"name":""}]
```

- `name` is a display name and may be empty. It is not identity: definition
  identity is content-based on each side, and the whole list partakes in the
  geometry and output hashes as `definitions` (§6.1, §6.3), so a rename is not
  silent.
- Definition `i`'s geometry is a complete §5 section group prefixed
  `idef.<i>.`: `idef.0.mesh.verts`, `idef.0.crv.points`, and so on, with the
  same names and dtypes. Attribute descriptors for definition `i` carry
  `"definition": i` and their sections are prefixed too:
  `idef.<i>.attr.<j>.values` (with `.<j>.offsets` for STRING).
- Rows are `inst.ref_def` (u32, `[R]`) and `inst.transforms` (f32, `[R,16]`):
  one definition index and one 4x4 matrix per row. Both sections are
  all-or-nothing; `inst.ref_def` length is `R` and `inst.transforms` length is
  `R*16`.
- Definition `i`'s own nested rows are `idef.<i>.inst.ref_def` /
  `idef.<i>.inst.transforms`, referencing other definitions in the table.
- The matrices are row-major (`m[row][col]`); a point transforms as a row vector
  (Rhino's `Transform` convention), so the translation sits in the last row —
  elements 12, 13, 14. Blender transposes on read and write.
- Absent `definitions` means no instances. A definition with no geometry
  sections is a legal empty definition and still occupies its index.
- A definition index outside the table is a `bad_frame` on decode.

## 6. Identifiers, hashes, and apply rules

### 6.1 Geometry hash (Blender only)

SHA-256 over the canonical section list:

```
for section in [sections in the order of §5 table, only those present]:
    update(name as UTF-8) ; update(0x00) ; update(raw little-endian bytes)
```

Prefixed with `sha256:` and hex-encoded. Computed with numpy on the Blender side. Never a hash of a file.

**Attributes take part.** When the frame carries attributes, their descriptor list (§5.1) is hashed as one extra entry named `attr.descriptors`, holding the canonical JSON of the list. Section names are index-based (`attr.0.values`), so without the descriptors a renamed attribute would hash identically and a cache hit would silently keep the old name.

**Instances take part.** Definition sections (`idef.<i>.*`) hash in definition-index order, each definition after the other, so the hash does not depend on section order; `inst.ref_def` / `inst.transforms` hash in the §5-order after the geometry sections. The `definitions` header list is hashed as one extra entry named `definitions`, canonical JSON, for the same reason as the attribute descriptors: a rename alone must not look like a cache hit.

### 6.2 Request ID (Blender only)

SHA-256 over these fields, separated by 0x00 bytes, in this order:

1. The literal `gh-stack/rid/v2`
2. Stage key (UTF-8)
3. Canonical parameter JSON (UTF-8). Keys sorted, values cast to schema types, floats encoded by Python's `repr`.
4. Geometry hash (the string, including `sha256:`)
5. Units (`m`)
6. Space (`stack-local`)
7. Nonce as a decimal integer. Push increments it.

Prefixed with `sha256:`. GH never computes or validates request IDs. It echoes them.

### 6.3 Output hash (GH only)

SHA-256 over the response sections, in header order: name (UTF-8), 0x00, raw bytes. Prefixed with `sha256:`. Blender treats it as an opaque string. Attribute descriptors take part the same way as in the geometry hash: one extra entry named `attr.descriptors` after the sections, holding the canonical JSON of the list. The `definitions` list takes part as one extra entry named `definitions`, canonical JSON, so a renamed definition is not a silent no-op.

### 6.4 Apply rules (Blender)

1. A response is applied when its `stage` matches, its `rid` equals the stage's current request ID, and its `out_hash` differs from the applied one.
2. A response for another `rid` is stored in the cache under its own `rid`. It is not applied.
3. A response with the current `rid` and a new `out_hash` (GH-initiated change) is applied.
4. A cached result for the current `rid` is applied without sending anything to GH.
5. An `error` response keeps the last good output and sets the stage to `error`. A later applied response clears the error.

## 7. Lifecycle

**Connect.** GH sends `hello`, then one `schema` per stage. Blender sends the current `request` for each stage, with `geom.mode = "full"` for the first request after a connection (GH may have lost its geometry).

**Steady state.** Blender sends `request`s as the stack changes. GH sends `response`s after each solve whose output changed, and `schema` when declarations change.

**Disconnect.** Blender keeps the last good output and sets stages to `disconnected`. GH retries with backoff, starting at 0.5 s and capped at 5 s. No user action is needed.

**Timeouts.** A request with no response after 30 s (configurable) sets the stage to `error` with the message "timed out". The stage retries only on the next change or on Push.

**GH disabled.** A Stage In or Stage Out set to disabled reports itself in `hello` as `enabled: false`. It sends no responses and holds no geometry.

**Conflicts.** Two documents that claim one stage key both receive `duplicate_stage`.

## 8. Versioning

- `v` is the protocol version. This document is version 2.
- Additive changes inside a version (new optional fields, new warning codes) are allowed. Readers ignore unknown fields.
- Breaking changes bump `v`.

## 9. Fixtures

`protocol/fixtures/<name>/` holds:
- `frame.bin`: the encoded frame.
- `expect.json`: the decoded header, per-section metadata (`dtype`, `shape`, `bytes`, `sha256` of the raw section bytes), the frame `sha256`, and `sections_hash` when the fixture has sections.

Both the Python and C# codecs must decode every fixture to the same values, and encode the expected values back to the same bytes (canonical header, §3). Created 2026-10-11:

- Python: `scripts/test-core`, via `blender/tests/core/test_fixtures.py`.
- C# under wine: `scripts/test-gha`, via `grasshopper/tests/console/GhStackCoreTests.exe`.
- Regenerate with `python3 scripts/make-fixtures`.

Twelve fixtures, covering `ping`, `hello`, `schema`, a `ref` request, a mesh request, a SubD request, a request with a definition table, nested instance rows and a per-definition attribute, a mesh response, a mixed mesh/curve/point response, a response with attributes (`i32`, `f32`, `u8` and a STRING offsets section), an error response, and the `error` message. Floats in fixtures are exactly representable in f32, so both codecs agree byte for byte without a tolerance.

## 10. Changes from plan rev 2

- SubD and plain meshes use separate section groups (`subd.*` and `mesh.*`), not a flag on a single mesh. Each group has its own creases.
- `mesh.crease_edges` and `mesh.crease_values` are two sections, not one.
- The `error` message type is added. Protocol mismatches and unknown references need it.
- `unknown_ref` is added so a `ref` request can fall back to `full`.
- Blender does not verify output hashes. It compares them for equality only.
- Canonical header encoding is specified, because golden fixtures depend on it.

## 11. Changelog

- 2026-10-10: draft created for M0.
- 2026-10-11: fixtures created and verified by both codecs (M2); the `sha256:`
  prefix convention for fixture hashes is fixed; the C# header writer is
  hand-rolled canonical JSON instead of `DataContractJsonSerializer`, which cannot
  sort keys (§10's no-third-party rule still holds).
- 2026-10-12: the `choice` parameter type is renamed `value_list` and its schema
  field `choices` to `values`, because "value list" is the Grasshopper term for
  it. The fixture generator was re-run; no other change.
- 2026-10-15: attributes add §5.1 (`attr.*` sections with an `attributes` header
  list) and the `i32` dtype; the geometry and output hashes fold the descriptor
  list in as `attr.descriptors` so a rename invalidates the cache. The
  `response-attributes` fixture pins i32, `f32`, `u8` and the STRING offsets
  section through both codecs. Additive inside v2.
- 2026-10-16: instances add §5.2 (`idef.<i>.*` groups, an `inst.*` row pair and
  a `definitions` header list); the geometry hash orders definition sections by
  definition index and folds the list in as `definitions`, and the output hash
  folds it in the same way. The `request-instances` fixture pins nested rows and
  a per-definition attribute through both codecs. Additive inside v2.
