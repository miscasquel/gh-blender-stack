# Repository review: code quality, leanness and efficiency

> **Review record.** Tree at commit `001964f` (v0.3.0), reviewed 2026-10-20.
> This was a read-only review: no product code, tests or scripts were changed
> for it. The live work list remains `docs/backlog.md`; items already recorded
> there are cross-referenced rather than duplicated.
>
> **Follow-up (2026-10-20).** The Tier 1 batch from §7 was applied and
> verified; see §13. Phase 2A followed in §14, and the Phase 2B build tooling in
> §15. The findings below describe the pre-fix tree.

## 1. Verdict

**Overall: 7.5 / 10.**

This is a disciplined, unusually well-tested repository. The protocol, the pure
Python core, the golden fixtures and the layer A/B test strategy are better than
most projects at this stage. The score is held down by three things:

1. a 2,438-line engine that owns too much and holds 22 module-level mutable
   globals;
2. a Grasshopper link built on static state, three threads and a hand-rolled
   WebSocket client, with a few real concurrency and lifecycle risks;
3. a conversion layer with dead shipped code, duplicated paths and a 1,116-line
   file.

The highest-value findings, in order, are:

| # | Finding | Severity |
|---|---|---|
| 1 | Output helpers can keep attributes and colours that a new response no longer carries (`B-ENG-4`) | High — potential stale data |
| 2 | The GH side publishes a frame across several fields that the UI thread reads without a snapshot (`C-LINK-1`) | High — race risk |
| 3 | Hot cache accounts wire bytes, not Python object memory, and misses orphaned entries (`B-ENG-2`) | Medium — memory |
| 4 | Protocol boundary gaps: non-finite encode, invalid UTF-8, raw control chars (`P-1`) | Medium — codec divergence |
| 5 | The sender thread is not stopped when the receive loop raises (`C-LINK-2`) | Medium — lifecycle |
| 6 | `_process_stage` is 220 lines; full-scene scans run per message (`B-ENG-1`, `B-ENG-3`) | Medium — maintainability/CPU |

## 2. Method and limits

Reviewed:

- every Python file in `blender/gh_stack/` outside `vendor/` (6,998 lines);
- every C# file in `grasshopper/src/` (5,726 lines);
- `blender/tests/` and `grasshopper/tests/` (10,051 lines);
- `tools/fake_gh/`, `protocol/fixtures/`, `scripts/`, `docs/`, `spikes/`;
- the git tree, ignore rules and tracked artefacts.

Executed:

```text
scripts/test-core        # 53 tests, all pass
```

Not executed, because this environment is not set up for them:

```text
scripts/test-blender     # needs Blender 5.2 headless
scripts/test-gha         # needs wine + Mono csc + Rhino assemblies
scripts/test-extension   # needs a Blender install and config directory
```

The layer B and C ratings therefore come from reading the suites, not from a run.
`ruff` is not installed in this environment, so no lint result is claimed.

### Rubric

Scores are whole numbers from 1 to 10, weighting correctness/clarity at roughly
60% and leanness/efficiency at roughly 40%:

| Score | Meaning |
|---:|---|
| 9–10 | Clear, focused, tested; only polish left |
| 7–8 | Solid; localised debt or duplication |
| 5–6 | Works, but costly to change or risky to operate |
| 3–4 | Significant structural or hygiene problems |
| 1–2 | Unusable or unsafe |

Severity in the findings below means **impact on this product**, not abstract
style:

- **High**: can produce wrong data, a lost update, a leak or a hard-to-debug
  failure.
- **Medium**: real cost (memory, CPU, maintenance) or a plausible edge-case bug.
- **Low**: polish, duplication, documentation drift, minor risk.

## 3. Scorecard

| Module | Approx. LOC | Score | Verdict |
|---|---:|---:|---|
| Blender core: `codec.py`, `hashing.py`, `params.py`, `attributes.py` | 558 | **9** | Pure, deterministic, dependency-free, fixture-tested. |
| Blender core: `cache.py` | 304 | **8** | Clean index/store split; atomic writes; one surprising API side effect. |
| Blender `engine.py` | 2,438 | **6** | Strong intent and comments, poor decomposition. |
| Blender `geometry_io.py` | 1,277 | **7** | Correct and thorough; too many parallel read paths and stringly-typed dispatch. |
| Blender `nodes.py` | 418 | **8** | Dense recursive builder, well reasoned and versioned. |
| Blender `helpers.py` | 334 | **8** | Small and explicit; name-based identity has a rename cost. |
| Blender `stack.py` + `props.py` | 658 | **8** | Clean property groups; a string-centric schema representation. |
| Blender `server.py` + `bridge.py` | 291 | **8** | Good thread boundary; broad exception swallowing. |
| Blender `ui.py` + `ops.py` + `prefs.py` | 658 | **8** | Idiomatic Blender; small duplication. |
| C# core: `Json.cs`, `Frame.cs`, `ParamValues.cs` | 946 | **9** | Rigorous decoder and boundary checks; golden-fixture proven. |
| C# core: `Attributes.cs` | 394 | **8** | Good mapping; malformed string offsets are under-validated. |
| C# core: `Conversion.cs` | 1,116 | **7** | Impressive curve work; dead wrappers and spike-only overloads. |
| C# `GhLink.cs` + `MiniWsClient.cs` | 1,185 | **6** | Correct on the tested path, but static and thread-heavy. |
| C# `Components.cs` | 611 | **7** | Clear, repetitive, and one unguarded Stage In path. |
| C# `Instances.cs` | 701 | **7** | Dense recursive definition handling; coherent but heavy. |
| C# `AttributeComponents.cs`, `GooDispatch.cs`, `StageGeometry.cs` | 676 | **8** | Well factored and defensive. |
| Tests: layer A | 564 | **9** | Fast, isolated, thorough. |
| Tests: layer B | ~8,000 | **8** | Exceptional bug-driven coverage; environment-bound. |
| Tests: layer C | ~1,500 | **8** | Real round trips; hand-rolled harness. |
| `tools/fake_gh` | 510 | **8** | Valuable test peer; feature-crept but every flag maps to a failure mode. |
| `protocol/fixtures` | — | **9** | Byte hashes, re-encode equality, both codecs. |
| Docs | ~3,400 | **7** | Strong content, some drift and historical bulk. |
| Scripts/build | ~10 scripts | **6** | Works; duplicated wine setup and masked compiler exit status. |
| `spikes/` | 676 KB | **3** | Useful evidence, poor repository hygiene. |
| Vendored `websockets` | 2.0 MB | **4** | Dev/test-only, tracked `.so`, duplicated by the wheel. |
| **Overall** | | **7.5** | Strong, disciplined, with three oversized modules. |

## 4. Findings by module

### 4.1 Blender pure core — 9/10

Files: `core/codec.py`, `core/hashing.py`, `core/params.py`,
`core/attributes.py`.

**What is good**

- The core really is pure: no `numpy`, no `bpy`, no network. The layer A test
  `test_core_is_pure.py` enforces the important half of that contract.
- The frame format is deterministic: canonical JSON, generated section table,
  fixed hash order, size caps and explicit error codes.
- Every dtype has one table (`_DTYPE_FMT`), and the numpy path avoids importing
  numpy.
- The golden fixtures pin behaviour across both codecs, including a
  byte-identical re-encode.

**B-CORE-1 — `params.canonical_param_json` re-implements canonical JSON (Low).**
`core/params.py:118-124` builds its own `json.dumps(...)` call with the same
options as `codec.canonical_json` (`core/codec.py:61-67`). Two definitions of
"canonical" can drift. Recommendation: call `codec.canonical_json` from
`params`, or move the helper into one module.

**B-CORE-2 — Error classification is encoded in exception text (Low).**
`codec.code_of` (`core/codec.py:47-51`) recovers a protocol code by splitting
`str(exc)` on `:`. It works because every raiser prefixes the message, but a
message without the prefix silently becomes `bad_frame`. Recommendation: give
`HeaderError`/`SectionError`/`CodecError` a `code` attribute set at construction,
or make `code_of` inspect the exception class.

**B-CORE-3 — `ResponseStore.valid` deletes on read (Low).**
`core/cache.py:227-241` treats "stale" as "remove now". That is intentional and
documented, but a query method with a deletion side effect is easy to misuse from
diagnostics or tests. Recommendation: keep `valid` read-only and add an explicit
`drop_stale`.

**B-CORE-4 — Function-local `import json` (Low).**
`core/params.py:120` does this inside `canonical_param_json` while the rest of
the module has no import block. Move it to the top.

### 4.2 Blender engine — 6/10

**What is good**

- The engine is the most carefully reasoned code in the repository. Comments
  explain the depsgraph guard, undo/redo reset, generation bumps, soft retry,
  latest-wins requests and idle stability.
- The prefix memo is keyed by identity and a global epoch, not by a
  hand-maintained dependency list — the right call for Blender's Python API.
- Cached frames, disk frames and live responses share one request-id path.

**B-ENG-1 — The engine is a god module (High, maintainability).**
`engine.py` is 2,438 lines and contains 22 module-level mutable globals
(`engine.py:56-141`). `_process_stage` alone is 220 lines
(`engine.py:1676-1895`); `_handle_response` is 109 lines and `_timer` 80.
Scheduling, caching, lifecycle, prefix evaluation, diagnostics and message
routing all live here. The comments make it navigable, but the file is the
single largest barrier to change. Recommendation: split into modules with seams
that already exist:

- `engine/cache_ops.py`: `_store_for`, `_store_frame`, `_remember`,
  `_memory_usage`, `_cached_response`;
- `engine/request.py`: prefix → rid → send; the middle of `_process_stage`;
- `engine/lifecycle.py`: `_drop_removed_stages`, `_split_duplicate_stages`,
  `_repair_plumbing`, `register`/`unregister`;
- `engine/diagnostics.py`: `_trace`, `_heartbeat`, `_timings`, public readers.

Keep `engine.py` as the timer, trigger and message dispatch facade. This is a
behaviour-preserving refactor; the existing layer B tests are the safety net.

**B-ENG-2 — The hot cache counts wire bytes, not Python memory (Medium).**
`_remember` (`engine.py:1404-1416`) stores decoded `(header, sections)` tuples.
`codec.unpack_section` (`core/codec.py:123-130`) returns a tuple of Python
numbers: a tuple slot costs 8 bytes and each f32 becomes a 24-byte Python float,
about 32 bytes per value, versus the 4 bytes `_memory_usage` assumes. u8
elements are at least an 8-byte pointer each, even when small integers are
shared, versus the 1 byte assumed (`engine.py:1418-1440`). A 256 MB "wire-size"
budget can therefore hold well over a gigabyte of Python objects.

The same function also recomputes the whole cache total on every response:
`while len(cache) > 1 and _memory_usage(cache) > limit` (`engine.py:1411`) makes
insertion O(n) and a long run of inserts O(n²). Two independent fixes:

1. Keep a running byte total on insertion/removal, as `CacheIndex` already does
   for the disk index (`core/cache.py:86-116`).
2. Either store raw encoded frames in the hot cache and decode on apply, or
   store numpy arrays rather than Python tuples. The raw frame existed on the
   wire; `server._on_connect` decodes it and drops the bytes, so `_store_frame`
   encodes it again for disk (`engine.py:1378-1401`). Keep the received bytes
   alongside the decoded sections and hand them to the store.

**B-ENG-3 — Full-scene scans on every message (Medium, CPU).**
These loops walk every object in the blend and then search each stack's stages:

- `_mark_dirty_all` (`engine.py:170-173`);
- `_after_history_step` (`engine.py:302-313`);
- `_on_load` (`engine.py:363-388`);
- `_nudge_stalled_waits` (`engine.py:520-533`);
- `_clear_failed_response` (`engine.py:606-622`);
- `_on_client_hello` (`engine.py:667-687`);
- `_handle_schema` (`engine.py:696-722`);
- `_handle_response` (`engine.py:732-780`);
- `_handle_error` (`engine.py:851-880`).

For a scene with many objects this is O(objects × stages) per response and per
schema heartbeat. The schema heartbeat makes it a recurring idle cost (see
`C-LINK-3`). Recommendation: maintain a `stage_key -> [(object, record, stage)]`
index, rebuilt on register/load and patched on stage creation/removal, and use
`_dirty` as the per-object work queue instead of re-scanning the scene.

**B-ENG-4 — Output helpers keep stale attributes and colours (High, potential
wrong data).**
`_apply_attributes` (`engine.py:1202-1236`) writes only the attributes the
response carries; `geometry_io.write_attributes` (`geometry_io.py:264-325`)
only creates or updates, never removes. `sections_to_mesh` writes
`mesh.colors` only when the section is present (`geometry_io.py:1022-1030`),
and `write_colors`/`write_point_colors` (`geometry_io.py:1045-1061`,
`1217-1231`) likewise only update. In contrast, `write_creases` always creates
or zeroes the crease channel (`geometry_io.py:1064-1092`), which shows the
intended "absent means none" rule.

Scenario: a definition attaches `panel_id` (or a colour), the user removes it in
Grasshopper, and the next response has no such descriptor. The helper keeps the
old attribute. The next prefix read (`mesh_attributes` / `points_attributes`)
then sends it back to Grasshopper as if it were still present. This is a
round-trip resurrection, and may also affect downstream native modifiers reading
the helper.

Recommendation: on every apply, reconcile the helper's generic attributes
against the frame's descriptors, remove ones this stage owns that are absent,
and clear the native `Col` channel when `mesh.colors`/`pts.colors` is absent.
Add a layer B test: attach a custom attribute and a colour, echo a frame without
them, assert both are gone, then echo again and assert they do not return.
This is the first correctness test I would add.

**B-ENG-5 — Cache and store entries are not pruned on stage removal or split
(Low, memory).**
`_drop_removed_stages` (`engine.py:1597-1629`) deliberately keeps cache entries,
but it does not pop `_cache`, `_stores`, `_state_since`, `_timings` or
`_last_no_send` for the removed `(stack_id, stage_key)`. `_split_duplicate_stages`
(`engine.py:1534-1574`) assigns a new `stack_id` without moving or dropping the
old entries. A long session that adds/removes/duplicates stages accumulates
decoded frames and per-stage stores with no reader. If the retention is needed
for undo (a removed stage can come back with the same key), document it and
bound it; otherwise clear the entries on removal/split.

**B-ENG-6 — Redundant encode/hash work per pass (Low, CPU).**
`_prefix_sections` hashes every section through `codec.section_bytes`
(`engine.py:2124`), `_same_prefix` encodes sections again for exact comparison
(`engine.py:1993`), and `_store_frame` re-encodes the decoded response for disk
(`engine.py:1388`). For large meshes this is three passes over the payload. The
wire bytes already existed when the frame arrived; the server could keep them
alongside the decoded sections and hand them to the store. This is an
optimisation, not a correctness issue.

**B-ENG-7 — `_apply_mesh`/`_apply_curves`/`_apply_points` duplicate a prep loop
and `_dtype_of` re-derives the type table (Low).**
`engine.py:1238-1308` repeats the same `prepared` loop three times, and
`_dtype_of` infers dtypes from section names while `core/attributes.WIRE_TYPES`
already defines the attribute vocabulary. Consolidate into one section-to-array
preparer; keep the name-based fallback for non-attribute sections only.

**B-ENG-8 — `_store_for` reaches through `ResponseStore.index` (Low).**
`engine.py:1329-1339` reads `store.index.base_dir` and writes
`store.index.size_limit`. That is an internal detail of `ResponseStore`. Add
`base_dir`/`size_limit` accessors or a `configure` method so the engine does not
need to know the store's internals.

**B-ENG-9 — Small drift (Low).**
- The comment at `engine.py:53-54` ("How long a stage may wait…") is detached
  from the `_timeout()` function and sits above `_cache`.
- `bridge.py:4` says the timer runs "5 ms while waiting, 50 ms otherwise"; the
  actual constants are `WAIT_INTERVAL = 0.005`, `HOT_INTERVAL = 0.005`,
  `IDLE_INTERVAL = 0.02` (`engine.py:34-44`).

### 4.3 Blender geometry I/O — 7/10

**What is good**

- The hard parts are handled explicitly: n-gons, vertex colours, creases,
  Bezier-to-NURBS conversion, cyclic curves, instance definitions, nested
  blocks, and GeometrySet prefixes.
- The unit and coordinate conventions are documented in one place.
- `write_attributes` refuses count mismatches instead of corrupting data.

**B-GEO-1 — 1,277 lines with three parallel read paths (Medium).**
The file contains:

- base datablock readers (`mesh_to_sections`, `curve_to_sections`,
  `points_to_sections`);
- evaluated `GeometrySet` readers (`geometry_set_to_sections`,
  `_serialize_geometry`);
- Curves-component readers (`curves_component_to_sections`);
- write-back functions for the same three kinds.

`curve_to_sections` (`geometry_io.py:382-443`) and
`curves_component_to_sections` (`845-948`) build the same six `crv.*` sections
with duplicated accumulation logic; `_bezier_net` and `_component_bezier_net`
are near twins (`446-465`, `951-977`). Recommendation: one small `CurveNet`
abstraction that both readers feed, and one section emitter. This is the second
highest-value refactor after the engine, but it is behaviour-preserving.

**B-GEO-2 — `_FOREACH` duplicates the type vocabulary (Low).**
`_FOREACH` (`geometry_io.py:157-171`) lists the same 14 Blender attribute types
as `core/attributes.py:22-37`, with a different payload (property name and
buffer dtype). Keeping both is defensible, but they can drift when a new type is
added. Add a test or a derivation that fails when the two key sets differ.

**B-GEO-3 — Function-local `import bpy` (Low).**
`geometry_io.py:662,708,1256,1270` import `bpy` inside functions even though the
module is unambiguously the bpy layer, and `prefs` at the top already imports
bpy. Hoist it or, if the intent is to keep this module importable outside
Blender, state that in the docstring and remove the top-level `prefs` import.
The current mix is inconsistent.

**B-GEO-4 — `write_creases` builds a Python dict over every edge (Low).**
`geometry_io.py:1080-1091` loops over all mesh edges and then all crease edges.
At 100k triangles this is a noticeable Python cost on every apply. A numpy
`searchsorted` on a sorted edge-key array, or Blender's edge lookup, would keep
it in C. The current code is correct; this is an optimisation for the same
budget the backlog already watches.

**B-GEO-5 — Point colours and radii are one-way (Medium; shared with
`C-CONV-3`).**
Blender sends `pts.colors` and `pts.radii` (`points_to_sections`,
`geometry_io.py:468-487`). The GH side decodes the colours into the Rhino point
cloud (`Conversion.cs:807-827`) but Stage Out emits only `pts.positions`
(`Conversion.cs:830-839`); radii are dropped on the GH side by design (comment
at `Conversion.cs:804-805`). A point cloud that round-trips through
Grasshopper loses its native colour and radius channels. If this is accepted,
document it next to the curve-attribute limitation; otherwise emit
`pts.colors` (and radii where the target supports them) from the source cloud.

### 4.4 Blender nodes and helpers — 8/10

**What is good**

- The node group has an explicit format stamp and a deterministic nested
  signature, so a same-hash frame can repair an old group without rebuilding on
  every tick.
- Cycle detection (`_has_path`, `_wired_edges`) and definition reachability are
  handled before building links.
- Helper objects are hidden and owned by a dedicated scene.

**B-HELP-1 — Helper names depend on the stack object's name (Medium,
orphan leak).**
Every helper identity is derived from `stack_obj.name`:

- `helper_name` (`helpers.py:80-82`);
- `definition_collection_name` (`120-121`);
- `definition_name` (`124-127`);
- `instance_name` (`130-132`);
- `nested_name` (`135-138`).

Renaming the stack object changes every lookup key. `ensure_helper` then creates
a new helper, and nothing removes the old one: `remove_helper` only runs when a
stage is deleted (or a duplicated stage is split), and `adopt_existing` only
moves collections. The old objects stay in the hidden `GH Stage` scene, and
definition collections do the same. This is invisible in the outliner but costs
datablocks and disk.
Recommendation: key helper names by `stack_id` (already stable and cheap to
ensure) with the object name only as a display prefix, or add a rename handler
that migrates/removes the old set. Add a headless test that renames the stack
object and asserts no new helper set is left behind.

**B-HELP-2 — Name-based identity is a Blender constraint, but it is everywhere
(Low).**
`bpy.data.objects.get(name)` is used for helpers, definitions and row clouds.
This is understandable: modifiers and node groups have no attachable ID
properties. Keep the comments that explain it, and prefer `as_pointer()` or
`stack_id` where an object reference is available.

**B-NOD-1 — `_group_matches` assumes the generated graph was not hand-edited
(Low).**
It compares the object set, format stamp and nested signature
(`nodes.py:299-322`), not links or node properties. If a user edits the internal
node group but leaves the same Object Info objects, the engine will not rebuild
it. That is probably acceptable (the group is internal), but worth a note in the
docstring so the next maintainer does not treat it as a general validity check.

### 4.5 Blender stack, props, server, UI — 8/10

**What is good**

- The property groups and `gh/<stage>/<param>` naming are coherent, and the
  animation-preserving rename path (`props.move_stage_params`,
  `_rename_animation`) is careful.
- `server.py` keeps all bpy access on the main thread and uses a simple queue
  bridge; the online-access gate is checked on every start.
- The UI is Blender-native and the Diagnostics panel exposes the state that
  usually hides in logs.

**B-PRO-1 — The schema is represented as strings (Low).**
`GhParam.default`, `min`, `max`, `values` are strings; conversions are spread
across `props._typed_default`, `_default_text`, `_matches_type`,
`core/params.cast` and the C# `ParamValues.ParseDefault`. This is pragmatic for
custom properties and keyframing, but it is the main place where a value can
mean different things on the two sides. Recommendation: one table of
`(type, parse, format, matches)` in `core/params.py`, used by `props` and tested
against the C# cases.

**B-PRO-2 — `_safe_unregister` is duplicated (Low).**
`ui.py:276-286` and `ops.py:178-182` are identical. Move it to a shared module
or `stack.py`. The same pattern appears inline in `stack.py` and `prefs.py`.

**B-SRV-1 — Unused import and broad exception swallowing (Low).**
`server.py:17` imports `core` only to keep it imported; the `# noqa: F401`
confirms it has no use. `_refuse` and the sender use `except Exception: pass`
(`server.py:39-42`, `168-172`), which is the right policy for a dead peer but
hides programming errors. Log at debug level or narrow to `ConnectionClosed` and
`OSError` where possible.

**B-SRV-2 — `GhServer.stop` can block the main thread for 3 s (Low).**
`server.py:80-91` joins the thread with a 3 s timeout. It is called from
`unregister` and from `start` when the port changes. A hung worker therefore
freezes the UI for up to three seconds. Acceptable for a shutdown path; a
diagnostic line when the join times out would make it visible.

**B-UI-1 — `_state_text` is a pass-through (Low).**
`ui.py:230-231` returns `stage.state` unchanged. Remove it or make it produce
the user-facing label the function name promises.

### 4.6 C# core codec — 9/10

Files: `Json.cs`, `Frame.cs`, `ParamValues.cs`.

**What is good**

- The decoder validates the header length, UTF-8/JSON errors, version,
  section table shape, dtype, byte counts, integer widths and trailing bytes.
- The version check accepts `2.0` and rejects `"2"`; the comment records why.
- The canonical writer matches Python for the fixture range, and the limitation
  at 16–17 significant digits is documented rather than hidden.
- `ParamValues.Cast` is total by design, with the Python rounding/truncation
  semantics written down.

**C-CORE-1 — Invalid UTF-8 handling diverges from Python (Medium).**
`FrameCodec.Decode` uses `Encoding.UTF8.GetString(frame, 4, (int)headLen)`
(`Frame.cs:207`). .NET's default UTF-8 decoder replaces invalid bytes with
U+FFFD; Python's `bytes.decode("utf-8")` raises and the Python codec reports
`bad_frame` (`codec.py:201-204`). A malformed header can therefore be accepted
by C# and rejected by Python. Use a throwing decoder:

```csharp
private static readonly Encoding StrictUtf8 =
    new UTF8Encoding(encoderShouldEmitUTF8Identifier: false, throwOnInvalidBytes: true);
```

and wrap the decode in the same `bad_frame` classification. The same strictness
should apply to attribute STRING sections (`Attributes.cs:237`).

**C-CORE-2 — Raw control characters are accepted inside JSON strings (Medium).**
`Json.ParseString` (`Json.cs:102-137`) appends any character that is not `"` or
`\`, including U+0000–U+001F. Python's `json` rejects unescaped control
characters in strings. The two codecs therefore disagree on what is a valid
header. Reject `c < 0x20` in the unescaped branch (and cover it with a fixture
or a console check).

**C-CORE-3 — Python can encode non-finite numbers that C# refuses (Medium;
cross-codec).**
`codec.canonical_json` (`codec.py:61-67`) calls `json.dumps` without
`allow_nan=False`; `float("nan")` becomes the literal `NaN`, and `inf` becomes
`Infinity`. The Python decoder rejects those literals via `_reject_constant`
(`codec.py:54-58`), and the C# writer throws (`Json.cs:307-308`). A NaN can
enter through a float parameter (`params.cast` performs no finiteness check), so
Python can produce a frame its own decoder and the C# peer reject. Add
`allow_nan=False` to the canonical encoder and validate/clamp non-finite
parameter values with a clear error.

**C-CORE-4 — `new byte[] { 0 }` per hashed section (Low).**
`FrameCodec.UpdateSection` (`Frame.cs:393-399`) allocates the separator on every
call and `GeometryHash`/`OutputHash` call it per section. A `private static
readonly byte[] Zero = { 0 }` avoids the allocation. Trivial, but this is the
hot path for every request hash.

**C-CORE-5 — `Json.FormatDouble` is a documented approximation (Low).**
The comment at `Json.cs:294-304` is honest: Mono's `"R"` and Python's `repr` can
differ for 16–17 significant digits. The protocol uses short decimals and fixed
fixtures, so it is not a wire bug. Keep the limitation visible in
`docs/protocol.md` as well, so a future fixture with a long float does not look
like a random failure.

**C-CORE-6 — `Json.GetLong` can round a non-integer (Low).**
`Json.GetLong` uses `Convert.ToInt64` (`Json.cs:373-377`), which rounds 1.5 to
2. `FrameCodec.Decode` avoids this for section byte counts by accepting only
`long`/`int` (`Frame.cs:261-266`), but `Attributes.DefinitionOf` uses
`GetLong` on a field a malformed frame could send as a double. Low risk because
the field is only an attribute marker; reject non-integers there for symmetry.

### 4.7 C# attribute store — 8/10

**What is good**

- The wire vocabulary and the storage vocabulary are kept apart, with explicit
  `KindOf`/`ComponentsOf`/`LabelOf` tables.
- The root `ArchivableDictionary` design is sound: attributes travel with the
  geometry, duplicate and transform with it, and serialize.
- `Attach` skips descriptors whose sections are missing, so a truncated frame
  does not take the whole stage down.

**C-ATT-1 — Three parallel dictionaries (Low).**
`KindOf`, `ComponentsOf` and `LabelOf` (`Attributes.cs:35-81`) all key the same
14 types. A single table of `(kind, components, label)` would make adding a type
a one-line change and remove the possibility of a missing entry.

**C-ATT-2 — Malformed string offsets are not validated (Medium).**
`SplitStrings` (`Attributes.cs:228-241`) casts each `uint` length to `int` and
uses `Math.Min(length, raw.Length - at)` before `Encoding.UTF8.GetString`. A
declared length above `int.MaxValue` becomes negative, and `GetString` then
throws `ArgumentOutOfRangeException`; a length beyond the remaining buffer is
silently truncated, while `at` still advances. The Python side slices with
`raw[at:at+length]` and silently returns a short string. Both behaviours are
lenient in different ways, and neither validates that the offsets sum to the
values length. Recommendation: validate in `Attach`/`SplitStrings`: each length
non-negative, `at + length <= raw.Length`, and the final `at` equal to the raw
length (or report a precise error). This is exactly the kind of frame the
decoder already tries to reject elsewhere.

**C-ATT-3 — `Attach` scans sections linearly per descriptor (Low).**
`Find` loops over the section list for each descriptor and again for offsets
(`Attributes.cs:184-188`, `336-342`). Frames have a handful of attributes, so
this is fine; a name → section dictionary would be simpler at the call site.

### 4.8 C# conversion — 7/10

**What is good**

- The curve path is genuinely careful: exact Blender NURBS modes, lossless
  Bezier span decomposition, a documented fallback with a deviation budget, and
  rational-weight handling that avoids the homogeneous/Euclidean trap.
- Units are centralised in `FromWire`/`WriteWire`; the comment records the four
  drifted copies that existed before.
- Every lossy path emits a coded `WireWarning`.

**C-CONV-1 — Dead shipped code and spike-only overloads (Medium,
maintainability).**
- `SectionsFromMesh` (`Conversion.cs:168-171`) has **no call sites** in
  `grasshopper/src` or `grasshopper/tests`; one spike,
  `spikes/s1/GhStackSpike.cs:445`, still calls it.
- `PointsFromSections` (`Conversion.cs:792-801`) is used only by
  `spikes/s6/S6Checks.cs`.
- The text-only overloads of `SectionsFromCurves`, `SubDFromSections`,
  `SectionsFromSubD` and `SectionsFromGeometry` (`Conversion.cs:1070-1107`) are
  used only by the spike checks.
- `Instances.Emit` has a matching text-only overload (`Instances.cs:366-379`)
  also used only by spikes.

The spike checks are useful evidence, but shipping their compatibility shims in
the product assembly is dead weight and invites drift. Recommendation: move
`SectionsFromMesh` and `PointsFromSections` into a `spikes/GhStackSpikeSupport.cs`
compiled only for those checks, and move the text-only wrappers there too — or
make the spikes use the `WireWarning` API directly. Do not delete
`SectionsFromMesh` until the S1 spike is retired or updated.

**C-CONV-2 — Allocation-heavy geometry loops (Medium, performance).**
`BuildMesh` adds vertices and faces one at a time (`Conversion.cs:82-106`);
`MeshSections` builds `List<uint>`/`List<float>` and per-vertex loops
(`173-223`); `SectionsFromCurves` appends four floats at a time to `List<float>`
(`372-389`). The S3 measurement (about 10 ms C# mesh build at 100k triangles) is
acceptable, but this is the code that dominates large frames. Preallocate arrays
from the known counts and use `Buffer.BlockCopy` and array-level API calls where
the API allows. Keep the Mono-compatible loops where the profile requires them;
the comments already explain the `AddVertices` metadata problem.

**C-CONV-3 — Native point colours and radii are lost on the return path
(Medium; see also `B-GEO-5`).**
`PointCloudFromSections` decodes `pts.colors` into the Rhino cloud
(`Conversion.cs:807-827`), but Stage Out's `SectionsFromPoints` emits only
`pts.positions` (`830-839`), and `GooDispatch` expands a point cloud to
`Point3d` values while adding the cloud to `sources` only for generic
`AttributeStore` collection (`GooDispatch.cs:83-92`). Native Rhino point cloud
colours are not in the UserDictionary, so they never come back. Radii are
explicitly dropped on the GH side (`Conversion.cs:804-805`) and were never
emitted by `SectionsFromPoints`. Either implement the return channel or add the
limitation to README/`docs/protocol.md` next to curve attributes.

**C-CONV-4 — Brep warning counts nulls (Low).**
`SectionsFromGeometry` warns `breps.Count` even if every entry is null
(`Conversion.cs:956-966`). Trivial; count non-null converted breps instead.

**C-CONV-5 — Duplicated point-scaling is fixed; keep it that way (Low).**
`FromWire`/`WriteWire` (`Conversion.cs:776-787`) are now the single scale
points, with the history in the comment. Do not reintroduce per-loop copies.

### 4.9 C# link and transport — 6/10

**What is good**

- The threading model is documented and mostly disciplined: GH objects on the
  UI thread, `ScheduleSolution(10, …)` because zero coalesces, replies on a
  sender thread, `MiniWsClient` writes serialised by a lock.
- `unknown_ref`, `proto_mismatch`, `too_large` and `unknown_stage` have explicit
  recovery paths; the link does not silently drop requests.
- Slots pin `SolveRid` so an answer is labelled with the request whose inputs it
  read, not the newest rid (`GhLink.cs:263-269`, `613-623`).
- The outbox is bounded and drops the oldest frame, never the newest.

**C-LINK-1 — A frame is published across several fields without a snapshot
(High, race risk).**
`Handle` writes the slot under `Gate` (`GhLink.cs:514-541`):

```csharp
slot.Sections = decoded.Sections;
slot.Attributes = AttributeList(header);
slot.Definitions = HeaderList(header, "definitions");
slot.GeometryHash = hash ?? "";
```

`StageIn.SolveInstance` reads `slot.Sections`, `slot.Attributes` and
`slot.Definitions` without taking `Gate` (`Components.cs:108-140`), and
`StageGeometry.Build` then uses them. A request arriving between the reads can
pair old sections with a new attribute list, a new definition table or a new
rid. `StageSlot.Params` is deliberately volatile and whole-snapshot
(`GhLink.cs:33-42`), but the frame fields do not get the same treatment. This is
the same class of bug the `SolveRid` pin was added to fix, one level down.
Stage In also reports `slot.Rid` (`Components.cs:136`) rather than
`GhLink.AnswerRid(slot)`, while the output is documented as "the request this
geometry answers" (`Components.cs:71`); a request arriving mid-solve can label
the old geometry with the new rid.

Recommendation: make the frame an immutable object:

```csharp
sealed class HeldFrame {
    public readonly List<Section> Sections;
    public readonly List<Dictionary<string, object>> Attributes;
    public readonly List<Dictionary<string, object>> Definitions;
    public readonly string GeometryHash;
    public readonly string Rid;
}
```

Publish it with one reference assignment under `Gate` (or `volatile`), and have
Stage In read one local. This also removes the "copy `Definitions` into a
`List<object>`" workaround in `Components.cs:121-124`, because the snapshot can
hold the exact shape the reader expects.

**C-LINK-2 — The sender thread survives a receive-loop exception (Medium,
lifecycle).**
`Loop` creates `stopSend` and `sender`, starts the sender, then calls
`ReceiveLoop` (`GhLink.cs:363-391`). `stopSend.Set()` and `sender.Join(500)` run
only if `ReceiveLoop` returns normally. If `ReceiveBinary` throws (socket reset,
dispose, malformed stream), control jumps to the outer `catch`, the sender is
never signalled, and `sender.Join` is skipped. The old sender stays alive (and
keeps calling `RetryStaleSolves`/`SendPublication`) until `Stop()` or until an
`OutboxSignal` makes its send fail; on a fast reconnect there is a window with
two sender threads and a stale one still publishing. Fix
with a `try/finally` around `ReceiveLoop`:

```csharp
try { ReceiveLoop(client, worker); }
finally { stopSend.Set(); sender.Join(500); }
```

**C-LINK-3 — Schemas are re-sent every two seconds (Medium, idle cost).**
`SendLoop` calls `SendPublication()` when `_connected` and two seconds have
passed (`GhLink.cs:741-746`). `SendPublication` gates only the `hello` frame
(`874-901`); the schema loop at `903-912` runs unconditionally and enqueues one
`schema` frame per stage on every heartbeat. Blender compares
`schema_signature` and skips the UI work (`engine.py:705-710`), but it still
decodes each message and scans every object to find the stage. Recommendation:
track the last published schema per stage (a string signature or the same
dictionary) and only enqueue when it changes; keep a periodic `hello` as the
reconnect/safety net.

**C-LINK-4 — `RetryStaleSolves` can double-solve a slow stage (Medium, CPU).**
`RetryStaleSolves` is called every 250 ms from the sender loop
(`GhLink.cs:741`, `753-770`). A slot in `waiting` older than 250 ms is retried,
up to eight times. If the solve itself takes longer than 250 ms, the retry
schedules another solve while the first is still running. The backlog already
records this hypothesis (`docs/backlog.md`, "Stage In latency with instances",
hypothesis 1). Recommendation: mark a solve in flight from the
`ScheduleSolution` delegate start to `SolutionEnd`, and skip the retry for that
slot while the flag is set; keep the attempt budget for genuinely dropped
delegates.

**C-LINK-5 — `MiniWsClient` is a spike-grade WebSocket (Medium, security and
cost).**
The docstring still says "for the S1 spike" (`MiniWsClient.cs:1-2`), and the
handshake explicitly does not verify `Sec-WebSocket-Accept`
(`57-65`). It also:

- constructs `new Random()` for the handshake key and every mask (`44`, `111`);
- builds each frame in a `MemoryStream`, masks byte by byte, then copies the
  whole payload with `ToArray()` (`88-114`) — three copies for a large mesh;
- trusts the 64-bit length in `ReadExactly` and allocates it directly
  (`194-205`), with no cap.

On loopback with a trusted peer this is acceptable, but it is the least lean and
least defensive code in the product. Recommendation, in order: validate the
accept header (SHA-1 of key + GUID, or at least require `101` and the header),
use `RandomNumberGenerator`, write the frame and masked payload directly to the
stream in chunks, and cap `len` at `FrameCodec.MaxMessageBytes`. The transport
is already an explicit decision (`docs/decisions/0001-own-websocket-transport.md`);
make its trust boundary explicit too.

**C-LINK-6 — Dead `Connected` property (Low).**
`MiniWsClient.Connected` (`68-71`) has no callers; `GhLink` keeps its own
`_connected`. Remove it or use it in `Loop` for the send gate.

**C-LINK-7 — Static state makes the link hard to test and unload (Low).**
`GhLink` has roughly 20 static fields (`Endpoint`, `Gate`, `Slots`,
`Components`, `Notes`, `Outbox`, `OutboxBytes`, `OutboxSignal`, `_worker`,
`_thread`, `_connected`, `_publishedThisConnection`, `_client`, `Claims`,
`StageEnabled`, `StageParams`, `_lastDocName`, `_lastPublishTicks`,
`_lastStages`). The console tests can drive it, but tests cannot run two links
in one process, and a host that unloads the plugin relies on `Stop()` clearing
everything. A small `GhLinkHost` instance owned by the assembly would make the
lifecycle explicit; this is a larger change and not urgent.

### 4.10 C# components and instances — 7/10

**What is good**

- The components map cleanly to the protocol: Stage In, Stage Out, Param,
  Point Cloud Points, Attribute Get/Set.
- Stage Out answers a failed solve with a protocol error instead of leaving
  Blender to time out (`Components.cs:310-344`).
- Instances are genuinely graph-side named blocks, with cycle guards, stable
  reference ids and content hashes. The comments record the probes behind the
  design.

**C-COMP-1 — Repeated component scaffolding (Low).**
`AddedToDocument`/`RemovedFromDocument` are repeated in three classes
(`Components.cs:153-163`, `354-364`, `485-495`); the error-response construction
in Stage Out (`310-344`) duplicates the normal response header one field at a
time. Extract a `GhStackComponent` base or helpers:
`RegisterLink(this)`/`UnregisterLink(this)` and `ErrorResponse(key, rid, code,
message)`.

**C-COMP-2 — Stage In has no exception guard around `StageGeometry.Build`
(Medium).**
Stage Out wraps its conversion in `try/catch` and reports `gh_solve_failed`
(`Components.cs:233-345`). Stage In calls `StageGeometry.Build` directly
(`126-129`); `AttributeStore.Attach`, `Conversion.*` and `Instances.Build` can
throw on a malformed frame (see `C-ATT-2`). A throw escapes `SolveInstance`,
which Grasshopper surfaces as a component error, but the stage state and the
message are less controlled than Stage Out's. Wrap the build in the same
try/catch and emit a runtime error/status; this is the right place to turn a bad
frame into a readable stage state.

**C-COMP-3 — Stage Out sends on every solve, including unsolicited ones (Low).**
This is intentional for GH-side edits, but it means every canvas solve encodes
and queues the full response when connected. The outbox bound and out-hash
comparison keep it safe; document the cost next to the `Sent` output so a user
with a heavy document knows why a slider drag sends full frames.

**C-INST-1 — `Instances.cs` is dense but cohesive (Low).**
The nested `DefinitionBuilder` and `DefinitionWriter` are 700 lines together.
The recursion and the two key spaces (`idef` index vs document GUID) are the
hard part; the comments are good. Keep it as one file unless the definition
cache change lands (below).

**C-INST-2 — Definitions are rebuilt every solve (Medium, known).**
`StageGeometry.Build` → `Instances.Build` → `DefinitionBuilder.Definition`
constructs a `ModelInstanceDefinition` and converts its geometry on every solve
(`Instances.cs:126-236`). The backlog already names this as the second
hypothesis for Stage In latency. If it is addressed, cache by content hash
(`ContentHash` already exists) and invalidate on transform/definition change;
transform-only edits should only rewrite the row cloud, not the definition.

**C-INST-3 — Text-only `Emit` overload is spike-only (Low).**
See `C-CONV-1`; it is part of the same dead-shipped-code cleanup.

### 4.11 Tests — 9/10 (A), 8/10 (B), 8/10 (C)

**What is good**

- Layer A is fast, isolated and enforces the pure-core boundary.
- Layer B tests are written from live failure reports, and the docstrings name
  the report. That is why the engine's most subtle behaviour is covered:
  Edit Mode flush, depsgraph drain, guard watchdog, reconnect verification,
  undo/redo, duplicate stacks, frame cache, parameters and instances.
- Layer C runs the real codec against the same fixtures and drives `GhLink`
  against a scripted server. It checks the error paths, not just the happy path.
- Test volume is high relative to product code (10,051 test lines for 12,724
  product lines), and the suites are readable.

**T-1 — CI runs only layer A (Medium, coverage gap).**
`.github/workflows/tests.yml` runs `scripts/test-core` only. The README explains
that layers B and C need Blender/wine, but the C# codec console tests need only
wine + Mono `csc`, no Rhino; they could run on a Linux CI job with wine
installed. Layer B is harder, but a self-hosted or scheduled job would catch the
regressions that currently depend on the owner's machine. Recommendation:
add a second job for `grasshopper/build/build.sh` + `GhStackCoreTests.exe` under
wine (the fixture half is Rhino-free), and document a local pre-release gate for
layers B and D.

**T-2 — Tests reach into private APIs and fixed ports (Low).**
`test_live_sync.py` drives `engine._timer()` directly and inspects `_pending`;
layer B uses fixed ports 9148/9150 and sleeps. This is pragmatic for headless
Blender, but it couples tests to internals and can flake under parallel runs.
Recommendation: keep the timer pump (there is no alternative in `-b`), but
allocate ports from a small helper and make the polling helpers the only way
tests wait.

**T-3 — Missing tests for the new findings (Medium).**
Add layer B/C coverage for:

- stale attributes/colours after a response that omits them (`B-ENG-4`);
- renaming a stack object and checking helper orphans (`B-HELP-1`);
- malformed STRING offsets (`C-ATT-2`);
- invalid UTF-8, raw control characters and non-finite numbers
  (`C-CORE-1`–`C-CORE-3`);
- a receive-loop exception and the sender thread count (`C-LINK-2`);
- point colours/radii round trip (`C-CONV-3`).

These are cheap tests relative to their value; most can be pure console checks
or a direct `geometry_io` test.

### 4.12 Tools, fixtures, docs, scripts and hygiene

**`tools/fake_gh` — 8/10.**
A strong idea: one Python peer that models full/ref requests, refusals, echoes,
GH-side edits, schema changes, coalescing and delays. It has grown into a small
product (510 lines) but every flag corresponds to a real test scenario. No
change recommended beyond moving the fixture-report schema next to the tests if
it grows further.

**`protocol/fixtures` — 9/10.**
Twelve fixtures, each with a frame hash, per-section hashes/dtypes/shapes, and a
byte-identical re-encode requirement. This is the strongest part of the repo.
The only gap is malformed frames: fixtures cover valid frames; the decoder
boundary is covered by unit tests, not shared golden frames. That is a
reasonable split, but the new boundary findings (`C-CORE-1`–`C-CORE-3`) deserve
explicit negative tests.

**D-1 — Documentation drift (Low).**
- `README.md:57` says "eleven shared golden fixtures"; there are 12 (the
  protocol doc at `docs/protocol.md:320` correctly says twelve).
- `bridge.py:4` says "5 ms while waiting, 50 ms otherwise"; the engine uses
  5 ms hot and 20 ms idle.
- `engine.py:53-54` has a detached comment about `_timeout()`.
- `MiniWsClient.cs:1-2` still calls the shipped client a "S1 spike".
- `GhLink.cs:895` hardcodes `"GhStack 0.3.0"` and
  `tests/console/LinkTests.cs:47` asserts the same string; the manifest is the
  documented single source (`blender_manifest.toml`). Add a release check or
  generate the constant.
- Port `59130` is repeated in `prefs.py:13`, `GhLink.cs:127`, `fake_gh.py:83`
  and the docs. At least a test could assert they agree.

**D-2 — `docs/polish-plan.md` is historical but heavy (Low).**
1,208 lines, clearly marked as a historical record. It is valuable evidence and
should stay, but consider moving it under `docs/archive/` so the live docs
(`backlog.md`, `plan.md`, `protocol.md`) are obvious.

**S-1 — Build scripts mask the compiler exit status (Medium, tooling).**
All C# build invocations pipe through:

```bash
2>&1 | grep -vE 'fixme:|MESA-EGL|pci id|^$' || true
```

(`grasshopper/build/build.sh:38,62,89`; `build-core.sh:43`;
`build-gha.sh:49`). The `|| true` masks `csc`'s exit status; the later
`[ -f "$OUT" ]` catches the missing artefact, but the build does not stop at
the compiler error, and the failure is reported as "output not produced" rather
than as the error above it. Recommendation: capture `PIPESTATUS[0]`, filter the
known wine noise in a helper, and print the full log on non-zero. The same
applies to `scripts/build-extension`'s `grep -v '^DEBUG' || true`, though
Blender's exit code is checked by the artefact/validate path.

**S-2 — Wine/`csc.exe` boilerplate is duplicated (Low, known).**
`build.sh`, `build-core.sh` and `build-gha.sh` each resolve the prefix, wine path
and Mono `csc.exe` (`CSC_PATH=...`, `win_path`). The backlog already tracks this
("Build scripts share a strain of boilerplate"). A shared
`grasshopper/build/wine-env.sh` is the obvious fix.

**H-1 — Repository hygiene (Medium, checkout cost).**
Tracked:

- `blender/gh_stack/vendor/` — 64 files, 2.0 MB, including a Linux-only
  `speedups.cpython-314-x86_64-linux-gnu.so`. It is dev/test-only and excluded
  from the extension zip, but it is still a platform binary in the source tree.
- `spikes/` — 74 files, 676 KB, including 28 result files and duplicated probe
  scripts.

Ignored but present locally: `grasshopper/build/out/` (11 MB), `dist/` (1.4 MB),
`__pycache__`, `.ruff_cache`. They do not travel in git, but they make a working
checkout heavier and can confuse a reviewer looking for the source of truth.

Recommendation: keep the vendored wheel only if the development install truly
requires it offline; otherwise fetch it in `scripts/install-addon` as the
extension build already does. Move the spike scripts that are still evidence to
a `spikes/archive/` folder or a separate branch, keeping only `spikes/README.md`
and the results the decisions cite. Add a `git clean -ndX` note to the
contributor docs.

## 5. Cross-codec protocol boundary

The positive case first: the two codecs agree on every golden fixture, including
canonical header bytes, hashes and section payloads. The following are the gaps
that fixtures cannot catch because they are about *invalid* input or *unusual*
values.

| ID | Gap | Python | C# | Impact |
|---|---|---|---|---|
| `P-1` | Invalid UTF-8 header | raises → `bad_frame` | replacement chars → may parse | One side accepts a frame the other rejects. |
| `P-2` | Raw control chars in a JSON string | `json` rejects | `ParseString` accepts | Same class; a malformed header can pass C# only. |
| `P-3` | Non-finite header values | `json.dumps` emits `NaN`/`Infinity` | writer throws | Python can produce a frame Python's own decoder rejects. |
| `P-4` | STRING offsets | slices, truncates silently | `Math.Min`, negative cast can throw | Neither validates; failures differ. |
| `P-5` | Header float text above 15 significant digits | Python `repr` | Mono `"R"` | Documented; headers are not hashed. |

Recommended negative tests: a frame with an invalid UTF-8 byte in the header; a
header JSON string containing a literal newline; a `params` value of NaN; a
STRING attribute whose offsets sum to more than the values length. Run each
through both codecs and assert the same classification (`bad_frame` or a precise
error), not merely "both fail somehow".

## 6. What is notably good

This section exists because the scorecard can make a strong repository look
worse than it is. The following are above the norm and should be preserved:

1. **The protocol is a real contract.** Section table, canonical JSON, fixed
   hash order, error codes, size caps, and a specification that matches the
   code. Both implementations are tested against the same bytes.
2. **The layering is enforced, not just described.** `core` has no bpy/numpy and
   a test proves it. The Blender layer's one bpy read in `server.start` is
   deliberate and documented.
3. **The tests are written from failures.** The layer B docstrings are a bug
   history: Edit Mode flush, depsgraph mute, reopen refs, undo/redo, duplicate
   stacks. That is why the trickiest behaviour is covered without a formal
   model.
4. **The hard Blender details are handled.** Evaluated prefixes, identity-keyed
   memos, Edit Mode private data, hidden helper scene, modifier-as-lifecycle,
   driver/keyframe preservation, undo bookkeeping.
5. **The hard geometry details are handled.** n-gons, SubD creases, rational
   NURBS, Bezier nets, cyclic curves, nested blocks, units, and lossy paths that
   warn.
6. **The comments are unusually useful.** They record probes, measured numbers,
   rejected alternatives and the bug that motivated a guard. Keep this style;
   the review's complaints are about size and seams, not about clarity.

## 7. Prioritised remediation plan

The plan is ordered by risk × cost, not by score. Items already tracked in
`docs/backlog.md` are marked "known".

| Priority | Item | Findings | Effort | Acceptance |
|---|---|---|---|---|
| P0 | Reconcile helper attributes/colours on apply | `B-ENG-4` | M | New layer B test: attribute and colour disappear, do not return on echo. |
| P0 | Publish one immutable frame snapshot in `GhLink` | `C-LINK-1` | M | Stage In reads one local; a concurrent-request test cannot mix sections/attributes. |
| P0 | Close the protocol boundary gaps | `P-1`–`P-4` | M | Negative tests pass identically on both codecs; `canonical_json` rejects non-finite. |
| P0 | Stop/join the sender on receive-loop failure | `C-LINK-2` | S | Fault-injection test reports one sender thread after reconnect. |
| P1 | Make hot-cache accounting O(1) and memory-aware | `B-ENG-2` | M | Running total; a configurable bound in decoded bytes; no O(n²) insert. |
| P1 | Index stage keys instead of scanning the scene | `B-ENG-3` | M | No `for obj in bpy.data.objects` on response/schema/hello paths. |
| P1 | Gate schema publication on change | `C-LINK-3` | S | Idle link sends no schema frames; hello still periodic. |
| P1 | Fix or document point colour/radius return | `B-GEO-5`, `C-CONV-3` | M | Round-trip test or an explicit limitation in README/protocol. |
| P1 | Mark slow solves in flight | `C-LINK-4` (known) | S | `RetryStaleSolves` skips the active solve; latency re-measured. |
| P2 | Split `engine.py` along the proposed seams | `B-ENG-1` | L | No behaviour change; layer B green; each module under ~600 lines. |
| P2 | Remove dead C# wrappers and spike overloads | `C-CONV-1`, `C-INST-3` | S | No unused public conversion methods in the assembly; spikes still compile. |
| P2 | Migrate helper identity off the object name | `B-HELP-1` | M | Rename test leaves no orphan helpers. |
| P2 | Validate STRING offsets | `C-ATT-2` | S | Both codecs reject overlong/negative offsets with a precise error. |
| P2 | Harden `MiniWsClient` | `C-LINK-5`, `C-LINK-6` | M | Accept header checked; RNG; direct stream writes; length cap. |
| P3 | Build-script diagnostics and dedup | `S-1`, `S-2` | S/M | Compiler failure surfaced; one wine helper; known backlog item. |
| P3 | Doc/version/port single source | `D-1` | S | README says twelve; bridge timing correct; version/port checked by a test. |
| P3 | Repo hygiene | `H-1` | S/M | Vendor decision documented; spikes archived; no tracked platform binary unless justified. |
| P3 | CI for the Rhino-free C# codec | `T-1` | S/M | A wine job builds and runs `GhStackCoreTests.exe` on every push. |

Suggested order for a single session: **P0 attribute reset → P0 snapshot →
P0 boundary tests → P1 cache total → P1 schema gating**. These are small,
independently testable, and remove the findings with real data/race risk. The
engine split is valuable but should come after the correctness items so the
tests added there become part of the refactor's safety net.

## 8. Metrics

### Source and tests

| Area | Lines |
|---|---:|
| Python product (`blender/gh_stack`, no vendor) | 6,998 |
| C# product (`grasshopper/src`) | 5,726 |
| Product total | 12,724 |
| Python tests | 8,531 |
| C# tests | 1,520 |
| Test total | 10,051 |
| Test-to-product ratio | ~0.79 |

### Suite size

| Suite | Size |
|---|---|
| Layer A | 53 tests |
| Layer B | 182 `test_` methods across 14 files |
| Layer C | 55 `Check(...)` calls in 3 console programs |
| Fixtures | 12 golden frames |
| Docs | 15 pre-existing Markdown files, ~3,421 lines (plus README/CHANGELOG) |

### Largest product files

| File | Lines |
|---|---:|
| `blender/gh_stack/blender/engine.py` | 2,438 |
| `blender/gh_stack/blender/geometry_io.py` | 1,277 |
| `grasshopper/src/core/Conversion.cs` | 1,116 |
| `grasshopper/src/gh/GhLink.cs` | 959 |
| `grasshopper/src/gh/Instances.cs` | 701 |
| `blender/gh_stack/blender/props.py` | 432 |
| `grasshopper/src/gh/AttributeComponents.cs` | 429 |
| `grasshopper/src/core/Frame.cs` | 426 |

## 9. Commands used

```text
scripts/test-core
find ... -name '*.py' -o -name '*.cs' | xargs wc -l
grep -n '^class |^def |^    def ' engine.py geometry_io.py ...
python3 - <<'EOF'  # AST function-length measurement
EOF
git ls-files | grep -E 'vendor|spikes|...'
du -sh vendor dist spikes grasshopper/build/out
```

No formatter, linter, headless Blender run or wine run was performed.

## 10. Finding index

| ID | Severity | File(s) | Summary |
|---|---|---|---|
| `B-CORE-1` | Low | `core/params.py:118-124` | Canonical JSON duplicated. |
| `B-CORE-2` | Low | `core/codec.py:47-51` | Error code parsed from exception text. |
| `B-CORE-3` | Low | `core/cache.py:227-241` | `valid()` deletes as a side effect. |
| `B-CORE-4` | Low | `core/params.py:120` | Function-local import. |
| `B-ENG-1` | High | `engine.py:56-141, 1676-1895` | God module / 22 globals / 220-line pass. |
| `B-ENG-2` | Medium | `engine.py:1404-1440` | Hot-cache memory undercount, O(n²), orphan entries. |
| `B-ENG-3` | Medium | `engine.py` (10 scan sites) | Full-scene scans per message. |
| `B-ENG-4` | High | `engine.py:1202-1236`, `geometry_io.py:264-325, 1022-1061` | Stale attributes/colours survive a response. |
| `B-ENG-5` | Low | `engine.py:1534-1629` | Cache/store entries retained after removal/split. |
| `B-ENG-6` | Low | `engine.py:1388, 1993, 2124` | Redundant encode/hash passes. |
| `B-ENG-7` | Low | `engine.py:1238-1308` | Duplicated apply prep and dtype mapping. |
| `B-ENG-8` | Low | `engine.py:1329-1339` | Store internals reached through `.index`. |
| `B-ENG-9` | Low | `engine.py:53-54`, `bridge.py:4` | Detached/stale comments. |
| `B-GEO-1` | Medium | `geometry_io.py` | 1,277 lines, three read paths. |
| `B-GEO-2` | Low | `geometry_io.py:157-171` | `_FOREACH` duplicates `WIRE_TYPES`. |
| `B-GEO-3` | Low | `geometry_io.py:662,708,1256,1270` | Function-local `bpy` imports. |
| `B-GEO-4` | Low | `geometry_io.py:1080-1091` | Per-edge Python dictionary in crease write. |
| `B-GEO-5` | Medium | `geometry_io.py:468-487`, `Conversion.cs:830-839` | Point colours/radii one-way. |
| `B-HELP-1` | Medium | `helpers.py:80-138` | Helper names depend on object name; rename leaks. |
| `B-HELP-2` | Low | `helpers.py`, `nodes.py` | Name-based identity is a Blender constraint but pervasive. |
| `B-NOD-1` | Low | `nodes.py:299-322` | Group match does not detect hand-edited links. |
| `B-PRO-1` | Low | `props.py` | Stringly schema conversion. |
| `B-PRO-2` | Low | `ui.py:276`, `ops.py:178` | Duplicated unregister helper. |
| `B-SRV-1` | Low | `server.py:17,39-42,168-172` | Unused import; broad exception swallowing. |
| `B-SRV-2` | Low | `server.py:80-91` | Stop can block the UI for 3 s. |
| `B-UI-1` | Low | `ui.py:230-231` | Pass-through helper. |
| `C-CORE-1` | Medium | `Frame.cs:207` | Invalid UTF-8 accepted via replacement. |
| `C-CORE-2` | Medium | `Json.cs:102-137` | Raw control characters accepted in strings. |
| `C-CORE-3` | Medium | `codec.py:61-67` | Python can emit non-finite JSON. |
| `C-CORE-4` | Low | `Frame.cs:393-399` | Per-section zero-byte allocation. |
| `C-CORE-5` | Low | `Json.cs:294-304` | Float formatting limitation. |
| `C-CORE-6` | Low | `Json.cs:373-377` | `GetLong` rounds. |
| `C-ATT-1` | Low | `Attributes.cs:35-81` | Three parallel type dictionaries. |
| `C-ATT-2` | Medium | `Attributes.cs:228-241` | Malformed STRING offsets. |
| `C-ATT-3` | Low | `Attributes.cs:184-188,336-342` | Linear section lookup. |
| `C-CONV-1` | Medium | `Conversion.cs:168-171,792-801,1070-1107` | Dead/shipped wrappers and spike overloads. |
| `C-CONV-2` | Medium | `Conversion.cs:82-106,173-223,372-389` | Allocation-heavy geometry loops. |
| `C-CONV-3` | Medium | `Conversion.cs:830-839` | Point colours/radii dropped on Stage Out. |
| `C-CONV-4` | Low | `Conversion.cs:956-966` | Null Breps counted in warning. |
| `C-CONV-5` | Low | `Conversion.cs:776-787` | Keep centralised scaling. |
| `C-LINK-1` | High | `GhLink.cs:514-541`, `Components.cs:108-140` | Non-atomic frame publication. |
| `C-LINK-2` | Medium | `GhLink.cs:363-391` | Sender thread not stopped on receive exception. |
| `C-LINK-3` | Medium | `GhLink.cs:741-746,903-912` | Schema re-sent every 2 s. |
| `C-LINK-4` | Medium | `GhLink.cs:753-770` | Retry can double-solve (known). |
| `C-LINK-5` | Medium | `MiniWsClient.cs:1-2,44,57-65,88-114,194-205` | Spike-grade WebSocket. |
| `C-LINK-6` | Low | `MiniWsClient.cs:68-71` | Dead `Connected`. |
| `C-LINK-7` | Low | `GhLink.cs` | Static state/testability. |
| `C-COMP-1` | Low | `Components.cs:153-163,310-344,354-364,485-495` | Boilerplate and duplicated error header. |
| `C-COMP-2` | Medium | `Components.cs:126-129` | Stage In has no build exception guard. |
| `C-COMP-3` | Low | `Components.cs:233-303` | Unsolicited full responses. |
| `C-INST-1` | Low | `Instances.cs` | Dense but cohesive. |
| `C-INST-2` | Medium | `Instances.cs:126-236` | Definitions rebuilt every solve (known). |
| `C-INST-3` | Low | `Instances.cs:366-379` | Spike-only `Emit` overload. |
| `P-1` | Medium | `codec.py:61-67`, `Frame.cs:207` | Invalid UTF-8 accepted by C# only (see `C-CORE-1`). |
| `P-2` | Medium | `Json.cs:102-137` | Raw control characters accepted by C# only (see `C-CORE-2`). |
| `P-3` | Medium | `codec.py:61-67` | Python can emit non-finite JSON (see `C-CORE-3`). |
| `P-4` | Medium | `Attributes.cs:228-241`, `geometry_io.py:363-376` | STRING offsets are under-validated in both codecs. |
| `P-5` | Low | `Json.cs:294-304` | Header float text can differ above 15 significant digits. |
| `T-1` | Medium | `.github/workflows/tests.yml` | CI only runs layer A. |
| `T-2` | Low | `tests/headless/*` | Private APIs and fixed ports. |
| `T-3` | Medium | tests | Missing tests for the findings above. |
| `D-1` | Low | README, bridge, engine, MiniWs, GhLink | Doc/version/port drift. |
| `D-2` | Low | `docs/polish-plan.md` | Historical bulk. |
| `S-1` | Medium | `grasshopper/build/*.sh` | Compiler exit status masked. |
| `S-2` | Low | `grasshopper/build/*.sh` | Duplicated wine boilerplate (known). |
| `H-1` | Medium | `vendor/`, `spikes/` | Checkout hygiene. |

## 11. Existing backlog cross-reference

Already tracked, and deliberately not re-planned here:

- Stage In latency with instances / duplicate solves / definition rebuild
  (`docs/backlog.md`, "Performance" and hypotheses 1–3) — maps to `C-LINK-4`,
  `C-INST-2`.
- Old components keep their old parameter list (`docs/backlog.md`,
  "Reliability") — the `HasOutput` shim; not a defect, a deliberate migration
  tradeoff.
- Build-script boilerplate (`docs/backlog.md`, "Polish") — maps to `S-2`.
- Vendored wheel decision (`docs/backlog.md`, "Polish") — maps to `H-1`.
- Curve attributes read-only (`docs/backlog.md`, "Design constraints") —
  related to the point-colour limitation in `C-CONV-3`; document both together.

New in this review, not in the backlog: `B-ENG-4` (stale attributes),
`B-HELP-1` (rename orphans), `B-ENG-2` (hot-cache memory), `B-ENG-3` (scene
scans), `C-LINK-1` (snapshot), `C-LINK-2` (sender leak), `C-LINK-3` (schema
heartbeat), `C-CORE-1`–`C-CORE-3` (boundary), `C-ATT-2` (STRING offsets),
`C-CONV-1` (dead conversion code), `C-CONV-3` (point colours), `S-1` (build
diagnostics), `D-1` (drift), and the missing tests in `T-3`.

## 12. Closing assessment

The repository is in good shape for a 0.3.0 demo and better than its size
suggests: the protocol and core are clean, the tests are meaningful, and the
hard platform behaviour is documented. The next quality step is not "write more
tests" in the abstract; it is to turn the three largest files into smaller
modules with the same behaviour, fix the two high-risk correctness findings
(stale helper channels, non-atomic frame publication), and make the cache
account for the memory it actually uses. If those land, the same tree would
comfortably be an 8.5–9/10.

## 13. Tier 1 follow-up (applied 2026-10-20)

The Tier 1 batch from §7 was applied after the review. It is deliberately
behaviour-preserving; the only functional changes are a test addition and the
store API used by the engine.

| Finding | Applied change |
|---|---|
| `D-1` (subset) | README fixture count 11 → 12; `bridge.py` interval 50 → 20 ms; removed the detached `_timeout()` comment; reworded the `MiniWsClient` “S1 spike” docstring. |
| `B-SRV-1` | Removed the unused `from .. import core` in `server.py`. |
| `B-UI-1` | Removed the `_state_text` pass-through; the panel uses `stage.state`. |
| `C-LINK-6` | Removed the unused `MiniWsClient.Connected` property. |
| `C-CORE-4` | `FrameCodec.UpdateSection` uses one static zero byte instead of allocating per section. |
| `B-CORE-4` | `core/params.py` no longer imports `json` inside `canonical_param_json`. |
| `B-CORE-1` | `params.canonical_param_json` now calls `codec.canonical_json`, so there is one canonical encoder. |
| `B-GEO-2` | Added a layer B test asserting `_FOREACH` plus `STRING` covers `WIRE_TYPES` exactly. |
| `B-ENG-8` | `ResponseStore` now exposes `base_dir`, `size_limit` and `entry`; the engine no longer reaches through `store.index` in `_store_for`/`_handle_response`. |
| `B-ENG-9` (subset) | The detached comment and the stale bridge interval are gone. |

Verification after the batch:

```text
scripts/test-core        54 tests pass
scripts/test-blender     184 tests pass
scripts/test-gha        12 fixtures, attribute store, link round-trip: pass
```

Deliberately not applied in this batch: `B-PRO-2` (a shared module for four
lines is not worth the coupling), `C-CONV-4` (cosmetic warning count),
`B-GEO-3` (no functional benefit), `C-CORE-6` (part of the boundary-hardening
work), and the version/port single-sourcing half of `D-1` (needs a design, not
a text edit). `C-CONV-1` is corrected above: `SectionsFromMesh` is used by the
S1 spike, so it is not a pure deletion; it stays until the spike support is
moved or retired.

## 14. Phase 2A follow-up (applied 2026-10-20)

Phase 2A covered the small, testable items that did not need an architectural
change.

| Finding | Applied change |
|---|---|
| `C-CORE-3` | `codec.canonical_json` now sets `allow_nan=False`; `encode_frame` turns the resulting `ValueError` into `HeaderError`. `params.cast` rejects non-finite float, int, value-list, vector and colour values with `SchemaError`, so `props._safe_values` falls back to the schema default instead of sending an invalid number. |
| `C-ATT-1` | Replaced `KindOf`, `ComponentsOf` and `LabelOf` with one `TypeInfo` table (`kind`, `components`, `label`). `Label`, `Set`, `Attach`, `Decode`, `Collect` and `Encode` all read it. |
| `C-ATT-3` | `AttributeStore.Attach` builds a name → `Section` dictionary once instead of calling `Find` for every descriptor and offsets section; the private `Find` is gone. |
| `C-LINK-2` | `GhLink.Loop` wraps `ReceiveLoop` in `try/finally`, so `stopSend.Set()` and `sender.Join(500)` always run. A small `ActiveSenders` counter and a `FakeServer.ResetClient()` (RST, not clean close) drive a new link regression test. |

Verification after Phase 2A:

```text
scripts/test-core        57 tests pass
scripts/test-blender     184 tests pass
scripts/test-gha        12 fixtures, attribute store, link round-trip: pass
                        link sender lifecycle: one sender after connect,
                        reset takes the exception path, sender stops with the
                        failed connection, one sender after reconnect
```

Deliberately deferred from Phase 2A: `C-ATT-2` (STRING offset validation), the
rest of the boundary work (`C-CORE-1`, `C-CORE-2`, `C-CORE-6`), `C-COMP-2`
(there is no existing harness for Stage In's malformed-frame path), and the
Phase 2B build-tooling batch (`S-1`, `S-2`). The performance and structural
items (`B-ENG-2`, `B-ENG-4`, `C-LINK-1`, `B-ENG-1`, `B-ENG-3`, `B-HELP-1`)
remain later phases.

## 15. Phase 2B follow-up (applied 2026-10-20)

Phase 2B replaced the duplicated wine/Mono setup with one sourced helper and
made compiler failures visible.

| Finding | Applied change |
|---|---|
| `S-2` | Added `grasshopper/build/wine-env.sh`: prefix, wine, Mono `csc.exe`, Rhino library paths, `win_path`, `run_csc` and `run_wine` live there. `build.sh`, `build-core.sh`, `build-gha.sh` and `scripts/test-gha` source it. |
| `S-1` | `run_csc` captures the compiler log, filters only the known wine noise, preserves the caller's errexit state, and returns `csc`'s own exit status. The build scripts no longer pipe `csc` through a filter that swallowed its status. |

Verification:

```text
bash -n                 wine-env.sh, build.sh, build-core.sh, build-gha.sh, test-gha
scripts/test-gha       12 fixtures, attribute store, link round-trip: pass
build-core.sh          GhStack.Core.dll built
build-gha.sh           GhStack.gha built
negative compile       deliberate syntax error returns status 1 and prints
                       CS1519/CS1002/CS1040 instead of falling through to
                       "output not produced"
```

The same `blender … | grep … || true` pattern still exists in
`scripts/build-extension`, `scripts/install-extension` and
`scripts/test-extension`. It is a separate packaging-script change and remains
a follow-up; Phase 2B deliberately stayed with the C# build path.

## 16. Phase 3A follow-up (applied 2026-10-20)

Phase 3A covered the Stage In and GhLink correctness batch. It deliberately
stopped before the engine changes.

| Finding | Applied change |
|---|---|
| `C-LINK-1` | Added `HeldFrame`, one immutable snapshot holding sections, attributes, definitions, geometry hash and rid. `StageSlot` publishes it with one reference assignment; `ref` reuses the geometry lists under a new rid. Stage In reads the frame once and reports that frame's rid; Stage Out keeps `AnswerRid`. |
| `C-COMP-2` | `StageBuildGuard.Try` wraps `StageGeometry.Build`. Stage In reports a runtime error and `error` status without letting the exception escape `SolveInstance`; it does not write the shared slot status (a newer request may already own it). |
| `C-LINK-3` | `SendPublication` caches a canonical per-stage schema signature and enqueues only when it changes. Signatures are cleared on reconnect, `Stop`, and the last unregister; periodic `hello` remains. |
| `C-LINK-4` | A scheduled solve is marked in flight from the `ScheduleSolution` delegate to `SolutionEnd`; `RetryStaleSolves` skips it. A 30 s watchdog recovers a missed `SolutionEnd`, and the attempt budget remains for delegates that never start. |
| `C-ATT-2` | C# `SplitStrings` and Python `_decode_strings` now require every declared length to fit and the offsets to cover the value bytes exactly. A malformed STRING is skipped with a note; valid attributes in the same frame still attach. |

Verification after Phase 3A:

```text
scripts/test-core        57 tests pass
scripts/test-blender     186 tests pass
scripts/test-gha        12 fixtures, attribute store, link round-trip,
                        solve retry, stage build guard: pass
build-gha.sh            GhStack.gha built
```

New regression coverage:

- `LinkTests`: an old frame stays coherent after a newer full request; an
  unchanged schema sends nothing and a changed one is sent; an in-flight solve
  is not retried and a lost `SolutionEnd` does not block the retry.
- `AttrTests`: overlong, sum-mismatched and missing-offset STRINGs are skipped
  with notes while a valid neighbour survives.
- `StageTests`: the guard returns the build error instead of throwing.
- `test_attributes.py`: the Python side validates the same offset rules.

Still open from §7: `B-ENG-4`, `B-ENG-2`, `B-ENG-3`, then `B-ENG-1`, plus the
unrelated P2/P3 findings.

## 17. Phase 3B follow-up (applied 2026-10-20)

Phase 3B covered the engine batch.

| Finding | Applied change |
|---|---|
| `B-ENG-4` | Every apply now reconciles generic attributes against the frame's descriptors, including definition helpers. Absent attributes are removed; the native `Col` channel is cleared when `mesh.colors`/`pts.colors` is absent. `crease_edge`, leading-dot internals, `position` and the native colour channel are protected. |
| `B-ENG-2` | The hot cache stores the received wire frame plus its output hash, with a running byte total per stage cache. Eviction is O(1) on insert/remove; replay decodes the bytes. The socket path passes the raw frame through `bridge.py`, so the disk store no longer re-encodes it. `B-ENG-5` cleanup and `B-ENG-6` raw-byte reuse are folded in. |
| `B-ENG-3` | Added a direct-reference stage index plus a stack-object list, rebuilt on register/load/undo and patched on stage add/remove/split. Response, schema, hello, error, failed-apply and stall paths now use it; `_mark_dirty_all` and frame changes use the stack list. A new `test_stage_index.py` proves warm message paths do not rebuild the index and that rename/new-object cases are picked up. |
| `B-ENG-1` | `engine.py` became the `engine/` package: `triggers`, `messages`, `request`, `apply`, `cache_ops`, `lifecycle` and `diagnostics`, with `__init__.py` keeping the state and registration facade. The split is behaviour-preserving; every module is under ~600 lines. |

Verification after Phase 3B:

```text
scripts/test-core        57 tests pass
scripts/test-blender     193 tests pass
scripts/test-gha        pass (C# unchanged in this batch)
scripts/test-extension  installed extension round trip: pass
build-gha.sh            GhStack.gha built
```

FiraTower re-check: all four stages report `_payload_missing = False`, the first
dirty pass costs ~115 ms (prefix clones), and repeated idle passes settle at
1–2 ms with zero applies. That confirms the definition-only reapply loop stays
fixed through the cache/index refactor.

The remaining performance item was the prefix clone itself: ~37–39 ms per Cube
stage on a dirty pass. §18 covers the follow-up that batches those reads.

## 18. Prefix batch follow-up (applied 2026-10-21)

The FiraTower pass cloned and evaluated one object per stage, and every clone
paid a full `evaluated_depsgraph_get()` update. On the 73-object scene that was
~17 ms per stage; three stages plus cleanup made a Cube pass ~115 ms.

The engine now builds all missing prefixes for one object in one batch:

- `request._build_prefix_batch` collects every stage's modifier index.
- `request._clone_prefixes` links all clones, performs one depsgraph update,
  reads each evaluated geometry set, then removes the clones in one guarded
  cleanup. One `view_layer.update()` flushes the removals while the guard is
  still up; skipping it made the live depsgraph deliver the clone link/remove
  tags after the guard dropped, marking every stack dirty and rebuilding the
  batch (a live-only feedback loop).
- `_process_object` marks the batch lifetime; the cache is cleared when the
  object pass ends. Single-stage objects and surfaces keep the old path.

FiraTower measurements:

```text
before    Cube dirty pass ~115 ms (3 depsgraph updates)
after     Cube dirty pass  ~46 ms (1 depsgraph update + one cleanup flush)
idle      ~1.5 ms per pass, zero applies
```

The batched section hashes were identical to the per-stage hashes for all four
FiraTower stages. `test_prefix.test_05` now asserts that a two-stage chain makes
one batch call and clears the batch state afterwards.

Verification after the follow-up:

```text
scripts/test-blender     194 tests pass
```

Residual cost is now the one depsgraph update plus the geometry read/apply. A
single-stage complex object still pays one update; a definition-heavy GH solve
still belongs to `C-INST-2`.
