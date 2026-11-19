# Stageloom 0.7.0 / 0.8.0 — quality plan (rev. 2)

**Status:** 0.7.0 complete, 2026-11-16. 0.8.0 is in progress: T1 (baseline,
performance gate, code health) through T8 (performance follow-through) are
complete; T9c (the .NET no-go), T9a/T9b (build/CI/docs truth and the protocol
single-source tables), T10a (fuzz/invariants), the T10b coverage floors, the
T10c scale run and the T10d 1-hour soak are complete; T11 (release) remains.
W7's `geometry_io`, engine-object, `GhLink` and `Instances` splits are done; W8
(performance, T8) is done, including the 200-stage/100k-triangle/10k-instance
scale evidence; the W9/W10 items are closed and only T11 (release) remains.
**Baseline:** v0.6.0 plus the 2026-11-15 audit (seven module reviews), plus the
post-audit bake/upstream work (`102f9c8..254d796`). That was the starting point:
305 Blender headless tests in ~86 s, 57 core tests, C# and extension suites
green.
**Goal:** reach the highest practical point on the rubric without changing the
product's design or feature set, except for the deliberate simplifications
recorded in §2.
**Authority:** `plan.md` remains the 1.0 roadmap, `protocol.md` the wire
contract, ADRs the decision log. This document owns the 0.7/0.8 quality work and
records where those documents must change.

**0.8.0 completion record so far (2026-11-16).** T1 added the three-run best
p50/p95 baseline and `scripts/check-all` gate, the C# parameter/schema
microbenchmark and the generated code-health inventory. T2 removed the
`spikes/` evidence tree, the stale planning handoff, the archived Grasshopper 2
plan, the unused C# build script, the 21k-line vendored `websockets` tree, dead
parameter/attribute exports, test-only C# aliases and unused icons; it also
wrote the shim inventory ([ADR 0017](decisions/0017-compatibility-shims.md)) and
the official-wheel decision ([ADR 0018](decisions/0018-official-wheel-only.md)).
No production behavior changed. Production is **24,946 physical lines** across
49 files (from 25,096); the tracked tree is **82,866 lines / 231 files** (from
112,513 / 380). The full gate passed alone in **175.8 s** with the baseline
enforced: 123 core tests, 427 Blender headless tests plus the offline gate,
fourteen golden fixtures, the seven C# console suites and the installed
extension round trip. The measured values are in the T2 section of
[`performance-ledger.md`](performance-ledger.md) and the regenerated
[`code-health.md`](code-health.md). T3 then added the characterization suites
and the seam map [`seams-0.8.md`](seams-0.8.md) with **no production change**;
the full gate passed alone in **174.0 s** with 454 Blender headless tests and
681 C# checks, and the local harness explicitly skips the native Rhino halves
under wine. T3 is complete; the measured values are in the T3 section of the ledger and
code-health. T4 then split `geometry_io.py` into a package with one owner per
kind behind the same facade: **24,946 physical / 19,394 code lines across 56
production files** (flat physical, −20 code), with 454 Blender tests and 681 C#
checks unchanged. The full gate passed alone in **179.5 s** with 26/26 gated
metrics green (100k in-place +2.85% p50, 1M in-place +0.84%, instance repeat
+2.29%). T5 then replaced the package-global engine hub with the explicit
`ENGINE`/`StageRuntime` owner in `engine/runtime.py`; the facade is 42 lines and
no submodule uses `sys.modules[__package__]` or the old 123-name `__all__`.
Production is **24,738 physical / 19,376 code lines across 57 files**
(−208 physical, −18 code against T4), with 454 Blender tests and 681 C# checks
unchanged. The 2026-11-17 best-of-three baseline was rebuilt from three clean
solo runs after the old median baseline was shown to be non-comparable under the
host CPU power/core state; a pinned T4/T5 A/B showed no T5 regression. The full
gate passed 26/26 in **174.1 s** (decode +0.80%, 100k in-place +0.95%, 1M
+0.32%, instance repeat +2.06%). See [`seams-0.8.md`](seams-0.8.md) and the
ledger. T6 then split `GhLink.cs` into `StageRegistry`, `LinkConnection`,
`RequestScheduler`, `ProtocolHandler`, `Publication` and `StageSlot`/
`HeldFrame`, leaving `GhLink` as a 173-code-line facade. Production is
**24,719 physical / 19,714 code lines across 63 files** (−19 physical, +338
code against T5; the code delta is the explicit facade and owner-boundary
accessors); the former 2,489-line `GhLink` type is 173 code lines and the
largest link type is `StageRegistry` at 810. The 454 Blender tests are unchanged and
the C# runtime count is **685** (+4 direct owner guards). The full gate passed
26/26 in **173.1 s**; the worst positive p50 is +1.49% (p95 +1.14% on the
untouched 100k in-place apply) and no metric exceeds +5%. See [`seams-0.8.md`](seams-0.8.md) §3 and the ledger.
T7 then split `Instances.cs` into `InstanceBuild`/`InstancePlan`, `InstanceEmit`,
`DefinitionTableWriter`, `DefinitionTable`, `DefinitionIdentity` and a 36-code-line
facade, and replaced the `Conversion`/`Attributes` kind switches with tables.
Production is **24,711 physical / 19,800 code lines across 69 files** (−8 physical;
+86 code for the explicit owner boundaries, offset by removed curve appenders
and type/kind dispatch). The former 2,135-line `Instances` type is
gone; the largest instance type is `InstanceEmit` at 450 and `DefinitionBuilder`
dropped from 940 to 424. The 454 Blender tests are unchanged and the C# runtime
count is **685**. The full gate passed 26/26 in **176.1 s**; the worst positive
p50 is +2.55% (`total_ms`, p95 +3.36%) and no metric exceeds +5%. A native Rhino
probe built and emitted a real mesh/curve/point definition, round-tripped it and
checked the `blocks/2` fallback; the wine harness still prints the native skips.
See [`seams-0.8.md`](seams-0.8.md) §4 and the ledger.

T8 then measured before editing, fixed the three highest-value remaining costs
and kept every gated metric green. Production is **24,918 physical / 19,945 code
lines across 69 files** (+207 physical / +145 code): the mesh rebuild writes
validated arrays through `vertices.add`/`loops.add`/`polygons.add`, decoded C#
sections are owner/offset/length views with the `Raw` contract preserved, and a
publication generation makes a clean heartbeat skip its snapshot and schema
encodes. `pipeline.100k.apply_fresh_ms` fell **77.95/87.13 → 8.96/9.10 ms**
(−88.51%/−89.55%); C# `frame_decode` fell **0.4556/0.5167 → 0.0050/0.0055 ms**
and `frame_mesh_arrays` **0.9210/1.0338 → 0.4434/0.4684 ms**. The full gate
passed 26/26 in **159.7 s**; the worst positive p50 is **+1.81%**
(`total_ms`, p95 +3.93% on the unchanged in-place apply). Playback measured **12.7994/13.2626 ms** vs
the stored 12.8405/13.3398: the handoff's +7–9% does not reproduce. The 455 Blender tests
(+1 n-gon rebuild) and **687** C# checks (+2) pass; the wine harness still
prints its native skips. See the T8 verification below and the ledger.

T9c then recorded the framework decision as a **no-go**: C# 5/Mono and the
hand-written `MiniWsClient`/`Json.cs` stay through 1.0, revisitable only after
native Windows/Rhino acceptance ([ADR 0019](decisions/0019-keep-csharp5-mono.md)).
T9a then made the build/CI/docs claims true without touching production: the
manifest is the only version source (`scripts/version`), the extension zip and
`.gha` are reproducible (`scripts/test-reproducible`,
`scripts/test-reproducible-gha`), the deploy script derives the wine user
instead of hardcoding one, and CI explicitly skips the reference-machine
benchmark and the local-only C# suite and `.gha` check. T9b then made
`protocol/sections.json` the single source for the version, dtypes, hash order,
attribute types, warning codes, codec error codes and fixture inventory; both
codecs consume generated tables, `scripts/check-protocol-tables` gates drift,
and `scripts/check-docs` checks the fixture source and README together. T9 is
complete; T10a (fuzz/invariants) then added the deterministic frame campaigns
(10,000 fixed-seed cases per language), randomized mesh/attribute/instance
round trips and malformed bake-marker recovery without a production change.
T10b coverage then enforced the floors with tooling-only changes: core 93.50%
(90% floor), engine/geometry 89.43% (80%) and pure C# core 93.36% (80%, with
the two RhinoCommon files excluded by name and reason). T10c then added the
scale runner and the focused scale-shape test: a 200-stage stack, exactly
100,000 triangles and 10,000 instance rows, all structurally asserted and
recorded; it adds no timing gate. T10d then ran the 1-hour churn soak: 10,238
cycles, 0 invariant failures, 0 unclassified exceptions, 30/30 native bake
cycles, 15 reconnects and a clean final teardown. It also exposed and fixed a
whole-object deletion leak in the engine's per-stage session state (regression
test added). T11 (release) remains. See the T9a, T9b, T10a, T10b coverage,
T10c scale and T10d soak verification below.

**0.7.0 completion record (2026-11-16).** Q1–Q19 are complete; Q5 was merged
into Q4 (the cache and the `Live` toggle were one removal). The release was
measured, not inferred: 110 core tests, 423 Blender headless tests plus the
offline gate, fourteen golden fixtures in the Python and C# codecs, the C#
codec and its seven console suites, the installed extension round trip, and
`scripts/check-all` green in 116.0 s on the development machine. The release
notes, README and `plan.md` carry the measured test counts; the compliance
record carries the same verification. **W7 and W8 are explicitly out of
0.7.0** and remain the 0.8.0 plan; nothing in this record claims those seams or
performance gates are complete.

---

## 1. What "10/10" means here

A perfect score is not "no code left to improve"; it is "every quality invariant
is owned, tested, and enforced, and the remaining exceptions are deliberate,
documented decisions". The rubric:

| Axis | Weight | 10/10 condition |
|---|---:|---|
| Architecture / layering | 25% | One owner per invariant. Dependencies point one way. No mutable global state outside an owned registry/engine object. Modules are split by interface, not by line count. |
| Correctness / robustness | 25% | Every peer input validated at one choke point. No silent geometry/attribute loss. No unbounded main-thread waits. Legal state transitions only. Lifecycle create/delete pairs owned in one place. |
| Lean-ness | 15% | No dead code, test-only production APIs, or unused exports. Every compatibility shim has an ADR, a migration test, and a removal version. No second caching mechanism. |
| Elegance / consistency | 15% | One naming convention per concept; one table per protocol vocabulary; functions do what their names say; no history comments describing old behavior. |
| Performance | 10% | p50/p95 budgets gated against a stored baseline. No quadratic concatenation, per-frame full copies, or unbounded bookkeeping. Scale tests at 200 stages / 100k triangles / large instance counts. |
| Tests / docs | 10% | Every confirmed defect has a regression test. Fuzzing proves only classified failures. Docs are checked for factual drift. Full local gate under 3 minutes. |

**Size is a review trigger, not a gate.** A module over ~600 lines or a function
over ~80 lines is a candidate for a seam, not a failure. The gate is: one reason
to change, one owner, no duplicated invariant, an explicit interface, and a
test. Exceptions are allowed with a short comment explaining why the unit is
cohesive. This replaces the arbitrary LOC thresholds in rev. 1.

**Release target:** 0.7.0 — no module below 7/10, overall ≈ 8/10.
0.8.0 — every module ≥ 8.5/10, overall ≥ 9/10, with residual exceptions listed
as accepted ADRs rather than open findings.

### Measurable 0.8.0 gates

- Zero crashes, hangs, or freezes in a 1-hour soak with reconnect churn, stage
  add/delete/rename/undo, playback, bake, and cache/bake churn. **Met by T10d:
  10,238 cycles, 0 invariant failures, 0 unclassified exceptions.**
- Zero silent-loss paths: every approximated or dropped entity produces a
  user-visible stage warning; fuzz/round-trip tests show only classified errors.
- No dead exported symbol; no test-only API in shipped code; no TODO/FIXME.
- `scripts/check-all` completes in ≤ 3 minutes on the development machine and
  is what CI runs.
- Benchmarks: p50/p95 within **5%** of the stored baseline or the build fails.
  A 0–5% regression is accepted only with a quantified offsetting gain in the
  same task.
- Two real Rhino/Blender documents open from 0.6.0 with stages, params,
  keyframes, helpers and animation intact (migration test where possible).
- Nested blocks/instances round-trip both directions, including multi-part
  definitions and two levels of nesting.
- No custom frame cache or `Live` toggle in the shipped code; freeze and native
  bakes are the offline path; no two competing cache mechanisms.

---

## 2. Scope and decisions

### Preserved design choices

- One loopback WebSocket; Blender is the server, Grasshopper the client.
- Binary frames with a canonical JSON header; request/response correlation.
- Stage = modifier; prefix evaluation; latest-wins requests; Play Every Frame
  gate; native Bake node; Freeze.
- Geometry kinds, attributes, blocks, parameter schema, endpoint settings.
- Python add-on, C# 5/Mono plugin until a framework spike says otherwise,
  Blender 5.2 floor, Rhino 8 / GH1.

### Resolved decisions (from owner notes)

| ID | Decision | Consequence |
|---|---|---|
| D1 | **Nested blocks/instances must work both ways**, including multi-part block definitions. | New W1b. Protocol grammar extended for multi-part definitions; `caps` negotiation; fixtures and both-direction tests. |
| D2 | **Remove the custom frame cache and the `Live` toggle. Treat every stage as live.** Rely on Freeze and native bakes. A Simulation Zone cache is deferred to later R&D, not part of 0.7/0.8. | New W4. Deletes `core/cache.py`, `cache_ops.py`, cache prefs/UI, cache tests, and the cache-specific request/message branches. Deletes several audit defects outright. |
| D3 | **Compatibility shims need a policy.** | See §2.1. Each shim: ADR/comment, migration test, removal version. |
| D4 | **Bake only the selected stage. Compute the full downstream dependency closure, trigger each dependent once, and freeze it when its answer lands. Clear Bake touches only that stage's bake and the freeze markers it created.** | New W5. Supersedes ADR 0014's "downstream stays live" decision with ADR 0015. No chain-wide Clear Bake; no downstream native bakes. |
| D5 | **Stage labels are editable; the stage key stays immutable.** | W6. Modifier and helper naming derive from the label; keys, caches and requests use the key. |
| D6 | **No breaking protocol redesign.** | Clarifies rev. 1's "no protocol v3": see §2.2. |
| D7 | **Blender extension compliance is a first-class constraint.** | W9. The add-on stays pure Python + official wheel; T2 deleted the vendored tree and kept the wheel as the only source (ADR 0018); validate the artifact. |

### 2.1 What a compatibility shim is (D3)

A shim is code kept only to read or accept an older persisted form or caller.
Current examples:

- `RootKey = "GHStack.Attributes"` — reads attributes saved before the rebrand.
- The legacy data-root migration — moves a preference from the old path.
- `gh_stack_*` node-group metadata — reads/reuses node groups built before the
  current format.
- `HasOutput` — tolerates an old saved Stage Out without newer outputs.
- `PublishStage` / `SetSlotStatus` — aliases only tests call.
- Text-only warning overloads — used by live Rhino scripts, not the product.

**Policy:** every shim must have (a) an ADR or a comment stating why, (b) a test
that exercises the old form, and (c) a version after which it is removed. No new
shim without those. Shims used only by tests are deleted now. This is what
prevents "legacy stuff" from accumulating invisibly.

### 2.2 Protocol versioning (D6)

`v` is the major framing/semantic version; the current value is **2**. There is
no protocol v3 in this plan. Rev. 1's "no protocol v3" meant: no breaking
redesign of framing, correlation, or geometry semantics.

- Additive optional fields are allowed within v2.
- A semantic change old peers would silently misread needs a capability flag or
  a major bump. For D1 we use an optional `caps` list in `hello` and `request`
  (for example `"blocks/2"`), and the sender refuses multi-part definitions to a
  peer that did not advertise it, with a classified error or a warned fallback.
- Both halves ship from one repository; the capability exists to make mismatched
  installs explicit, not to preserve old behavior indefinitely.
- A hypothetical v3 would be a breaking re-cut of the frame/header contract. It
  is out of scope.

### 2.3 Simulation-zone cache (deferred)

A native per-frame cache built from a Geometry Nodes Simulation Zone
(`Simulation Input`/`Output`, Blender 5.3's `simulation_nodes_cache_bake` /
`calculate_to_frame` / `delete`) is **deferred to later R&D**. It is not part of
0.7.0 or 0.8.0 and is not a release gate. The criteria below are kept for that
later evaluation; after W4 there is no custom cache to confuse with it.

- **Go if:** the zone can store the applied stage output per frame; scrub and
  render replay without Grasshopper; the cache survives `.blend` save/reload;
  invalidation on schema/parameter/topology change is clean; it can be baked to
  disk; per-stage or chain granularity is controllable; and it replaces the
  native Bake node rather than adding a second cache.
- **No-go:** if it duplicates the Bake node, cannot invalidate cleanly, or
  bakes all modifiers indiscriminately. Then keep the native Bake node + Freeze
  and document the decision.
- **Rule:** never ship two cache mechanisms. The custom disk cache is gone in
  0.7; a simulation-zone cache, if adopted, replaces the Bake-node cache.

### 2.4 Framework assessment (D7)

**Blender add-on — no framework rewrite.** It is a `bpy` application; there is
no alternative framework that improves extension compliance. Keep pure Python,
the official `websockets` wheel, `blender_manifest.toml`, permissions, and no
`sys.path` manipulation in shipped code. The compliance work is packaging and
architecture, not a new framework:
- the **official wheel** is the only `websockets` source (0.8.0 T2 deleted the
  21k-line vendored tree; the dev install extracts the same wheel; ADR 0018);
- validate the manifest and the built zip in CI;
- 0.8.0 extension zip and `.gha` reproducible for the same tree/toolchain and
  traceable to a tag.

**Grasshopper plugin — one section is worth a framework spike, nothing else.**
The current C# 5/Mono build forces a hand-rolled WebSocket (`MiniWsClient`) and
JSON (`Json.cs`). A modern target (`net48` with Roslyn, or `net7.0-windows`)
could replace those with `ClientWebSocket` and `System.Text.Json`, and bring
async, analyzers, nullable, and `dotnet test`. But it risks the wine dev loop
and Rhino 8/GH1 loading. Therefore:

- **Spike (3–5 days, 0.8.0):** build the smallest possible GH component for both
  `net48` and `net7.0-windows`, load it in Rhino 8 on Windows and under wine,
  connect `ClientWebSocket` to the Blender server, and run it in CI.
- **Adopt only if** it loads reliably on the target and in the dev loop. Then
  migrate the transport and JSON layers; keep the domain code (`Conversion`,
  `Instances`, components).
- **If not,** keep C# 5 and harden/test the hand-rolled transport and JSON. The
  rubric does not require a framework change; it requires correctness and clear
  ownership.
- Do **not** rewrite `Instances`, `Conversion`, or the components: that is churn
  with no architectural gain.
- Regardless of the spike, generate protocol tables from one source so Python
  and C# cannot drift.

**Keep the domain design.** The audit's issues are boundaries, lifecycle, and
ownership, not the product model. Rewriting the Blender engine or geometry
modules would add risk without raising the ceiling.

---

## 3. Architecture invariants

These rules prevent micropatches. A change that violates one is rejected in
review.

1. **One owner per invariant.** Frame validity belongs to the codec; geometry
   topology to `sections_to_mesh`; stage lifecycle to the stage registry; helper
   identity to the stage key; schema reconciliation to one function; bake-chain
   ownership to one bake record.
2. **One validator per trust boundary.** Peer bytes, JSON values, section
   tables, attribute descriptors, and conversion inputs each have exactly one
   validation choke point. Everything below may assume validity.
3. **Classified failure or visible warning — never silence.** A valid input that
   cannot be represented exactly produces a stage warning with a count. An
   invalid input produces a typed error with a protocol code.
4. **Lifecycle symmetry is structural.** Registration/deregistration,
   create/delete, send/clear, dirty/clean, freeze/unfreeze are methods of one
   owner, not copies in each component/operator.
5. **No unbounded waits, queues, heaps, or scans on the main thread.** Every
   loop has a deadline, a supersession check, or a bounded size.
6. **Identity is stable.** Keys, uids, and document ids — never mutable object
   names — identify stages, helpers, caches, definitions, and bake chains.
7. **UI and operators are adapters.** They read state and call engine/registry
   methods. They never orchestrate multi-step engine work or write RNA during
   draw.
8. **One cache, or none.** Native Bake/Freeze is the only offline path in 0.7.
   A simulation-zone cache may replace it later; never run both.
9. **No dead code.** Removing a feature removes its fields, branches, tests,
   docs. A shim without an ADR and a test is dead code.
10. **Refactors are behavior-preserving.** Characterization tests land before a
    module move; behavior changes are bug fixes with their own tests.
11. **Docs are claims with owners.** Every version, transport, path, and status
    claim is either generated from source or checked by a script.

---

## 4. Workstreams

### W0 — Baseline and guardrails (start immediately)

- Baseline is green: 305 Blender tests, 57 core tests, C#/extension suites.
  Re-run and record it after every structural change.
- Add `scripts/check-all`: core, Blender, C#, installed extension, bench, docs
  check. Target ≤ 3 minutes.
- Add per-suite and per-test timeouts; fail on timeout with the last log lines.
- Fix the headless runner path: add `REPO/blender` once; remove per-module
  `sys.path` mutation; every module passes standalone (verified failure today in
  `test_stage_index.py`).
- Shared `harness.py`: `free_port`, `start_fake`, `pump`, `isolate_data_root`,
  `wait_for`, `stop_fake`; one global temp data root; dynamic ports; no
  order-dependent state.
- CI: core (3.13 and 3.14), Blender 5.3 headless, C# codec, bench baseline,
  extension build validation. Pin versions, cache downloads, set timeouts and
  least-privilege permissions.
- Static checks: correct ruff scope, shellcheck, C# warnings-as-errors.

**Exit:** `check-all` green; CI runs it; no test depends on another module's
side effects; no fixed ports or unbounded waits in the harness.

---

### W1a — Geometry correctness (0.7.0 blocker)

- **Topology change:** replace the length-only check in `sections_to_mesh` with
  a real topology signature (vertex count, face counts, face indices). Rebuild
  on change; write positions only on a true match. Replace
  `from_pydata(verts.tolist())` with direct array writes; keep a reference build
  for n-gons if necessary.
  Tests: `[4,4] → [3,5]`, flipped quad diagonal with identical counts, n-gon
  reorder, position-only fast path, randomized round trip against a reference.
- **Colour space:** define the wire colour as sRGB u8; convert linear
  `FLOAT_COLOR` to sRGB on read and back on write; `BYTE_COLOR` stays direct.
  Test native float colour round trip within one byte and byte colour exactly.
- **Absent means clear:** clear point radii when `pts.radii` is absent; test the
  non-zero → absent transition.
- **No silent drops:** a notes/warnings channel from geometry conversion;
  Catmull-Rom, missing Bezier handles, clamped NURBS order, dropped crease
  edges, and ignored mixed groups produce stage warnings. ADR 0003's
  "approximate and warn" becomes true.
- **Group atomicity:** choose mesh or SubD as a unit; reject a mixed frame or
  apply both explicitly; never pair mesh vertices with SubD faces.
- **Malformed curve arrays:** validate lengths, orders, weights; classified
  error instead of `IndexError`; no silent zero-weight substitution.

**Exit:** no known silent geometry corruption or loss; every approximation is
visible in `stage.warnings`.

---

### W1b — Nested blocks and multi-part definitions (0.7.0, D1)

This is a protocol and both-halves workstream, not just a Blender fix.

**Wire grammar (v2 extension):**

- A definition entry gains an optional `parts: N` (default 1).
- Single-part definitions keep the current `idef.<i>.*` names, so existing
  fixtures and saved behavior are unchanged.
- Multi-part definitions use `idef.<i>.<part>.*` for geometry and attributes
  (`mesh`, `subd`, `crv`, `pts`, `attr`), with `part` in `0..N-1`.
- Nested placements stay at definition level: `idef.<i>.inst.ref_def` and
  `idef.<i>.inst.transforms`; child rows index the same flat definition table.
- `caps: ["blocks/2"]` on `hello` and `request`; the sender refuses multi-part
  output to a peer without it, or falls back to one definition per part with a
  warning. No silent loss.
- Hash order includes `definitions`, `attr.descriptors`, multi-part sections,
  and nested rows. Add golden vectors.

**Blender → Grasshopper:**

- `_definition_parts` assigns each hoisted unit a part index; serialize each
  unit under its part; keep nested rows at definition level.
- Round-trip a two-member `Separate Children` collection, a mixed
  object+collection, nested collections two levels deep, and an empty member.
- Deterministic definition naming: source name + content hash; dedupe by
  content; never name-collide. Hash includes attribute descriptors.
- Cycle guards for outbound graph-side definitions; warn, do not stack-overflow.

**Grasshopper → Blender:**

- `Instances.Build`/`Emit` read `parts` and the part namespaces; build a
  definition with multiple geometry sources; preserve nested rows; cache
  definitions by content hash.
- Support Point members; either return curve attributes or warn (no silent
  drop).
- Build blocks without writing the Rhino document (current design); the bake
  path must produce equivalent nested blocks.
- Validate row counts/transforms; reject cycles with a warning.

**Tests and fixtures:** multi-part definition, two-level nesting, cycle, empty
member, attributes on parts, transforms composed parent→child, both directions,
and a Blender render/bake check.

**Exit:** nested blocks/instances round-trip both ways at the fixture,
console-test, and live-test levels; multi-part collections no longer collapse.

---

### W2 — Trust boundaries: codecs and attributes (0.7.0 blocker)

- **Python decoder totality:** validate header type, version, section table
  shape, names, dtypes, byte counts, duplicate names, shape consistency;
  `FrameError.code` instead of message parsing. Mirror the C# boundary tests in
  a new layer-A suite.
- **Python encoder:** reject non-string type, dtype/length mismatch, duplicate
  names; `b"".join`; classify oversized headers.
- **C# payload validation:** one `SectionSchema`/`FrameValidator` choke point
  for dtype↔descriptor type, `count`/`components`, array lengths, JSON casts,
  mesh bounds, curve arrays. All failures become `FrameException("bad_frame")`.
- **C# number formatting:** `FormatDouble` must be Python-repr compatible across
  the full double range (`1e15`/`1e16`, `-0.0`, shortest round trip, notation
  thresholds). Keep integers integral or fail classified. Non-finite values
  must be impossible through `ParamValues`/schema publish. Add a generated
  corpus checked by both languages.
- **Strict JSON:** reject raw control characters, invalid UTF-8, lone
  surrogates; validate `\u` escapes; type-aware `DeepEquals`.
- **Attributes:** validate name/type/domain/count and section dtype; never
  reinterpret or truncate; unknown types and malformed entries warn.
- **Protocol/fixtures:** document integer range, float format, duplicate names,
  strict UTF-8, empty geometry, multi-part definitions, capabilities. Add
  rejection fixtures and regenerate via `make-fixtures`.

**Exit:** fuzzing 10k mutated frames per language produces only classified
errors; no malformed input reaches geometry/attribute code; both codecs agree
on every fixture and the number corpus.

---

### W3 — Lifecycle, ownership, concurrency (0.7.0 blocker)

**Grasshopper**

- **Component base:** own `AddedToDocument`/`RemovedFromDocument`, notes,
  publication dirtying, claim registration, document binding. Migrate Stage
  In/Out, Param, Connection, Attribute components. Test add/delete of every
  type and assert an empty registry.
- **Threading:** no RhinoCommon/GH calls from socket/sender threads. Publish
  `doc_units`/scale from a UI-thread snapshot; solve only on the UI thread; add
  a debug thread assertion.
- **Document isolation:** registries keyed by document identity; remove
  `Components[0]`; solve in the document that owns the slot. Test two documents
  with identical keys.
- **Duplicate keys:** one claim per key per document; strict conflict; never
  last-writer merge.
- **One-way stages:** a terminal status without Stage Out; no retry storm.
- **Empty geometry:** separate "have a frame" from "has sections"; serve refs
  for zero-section frames. Test full(empty) → ref.
- **Removed keys:** send a publication or refuse with `unknown_stage`.
- **Stop/restart:** per-worker generation token; a stopped connect cannot revive
  or clear the next outbox; detach solution handlers. Test stop during a blocked
  handshake.
- **MiniWs:** verify `Sec-WebSocket-Accept`; cap before allocation; close and
  continuation correctness; buffer reuse; tests for 64-bit lengths,
  fragmentation, ping/pong.
- **Instances:** part of W1b; cycle/name/point/curve-attribute items there.

**Blender**

- **Playback:** supersession-aware pump (rid change, pending cleared, document
  change, state change); finite safety cap even when the preference is 0;
  per-stage exception isolation; flush Edit Mode before playback.
- **Bookkeeping:** rid-scoped error/recovery; a stale error cannot clear a newer
  pending.
- **Send race:** enqueue under the same ownership that observes clients, or
  record pending only after a successful enqueue; test disconnect between
  check and send.
- **Object lifecycle:** one cleanup routine for stage delete, object delete,
  rename, undo, duplicate; helpers/definitions/clouds keyed by stage key/stack
  id; no orphan datablocks or cache namespaces.
- **Load/register:** clear incoming queue, memos, pending, stores (stores go
  away in W4); test a load with queued stale responses.
- **Multi-scene:** handlers use their `scene`; per-scene frame/depsgraph; test
  two scenes on different frames.
- **Server:** bounded stop without a 3 s main-thread join where possible; real
  reconnect backoff; test bind failure and a wedged client.

**Exit:** no unbounded wait, no stale-answer cross-talk, no registry/helper
leak; a 1-hour churn soak is clean.

---

### W4 — Remove the custom frame cache and the Live toggle (0.7.0, D2)

Purpose: delete a whole subsystem whose complexity now outweighs its value.
This is a deliberate simplification, not a bug fix.

**Remove:**

- `blender/stageloom/core/cache.py` and `blender/stageloom/blender/engine/cache_ops.py`.
- `ResponseStore`, `_stores`, cache size preference/UI, cache statistics,
  cache namespaces, disk paths under the data root.
- `stage.live`, `stage.live_frame`, `_live_send_due`, `_live_changed`, the Live
  panel row, cache-vs-live branches in `request.py`, `messages.py`, `playback.py`,
  `lifecycle.py`, `triggers.py`, `props.py`.
- `generation` fields used only for cache validity; `_bump_generation` /
  cache-reset paths. Keep `geom_hash` (prefix/ref optimization) and
  `applied_out_hash` (skip identical applies).
- Cache tests (`test_frame_cache.py` and cache cases elsewhere), cache docs,
  cache benchmark scenarios, the `files` permission description change.
- `Frame Drop` remains as latest-wins behavior; it no longer means "read the
  cache". `Play Every Frame` remains the per-stage gate.

**Behavior after W4:**

- Every stage is live: a delivered request is a solver step; every frame/dirty
  pass asks again (latest-wins).
- Late answers for a superseded rid are dropped, not cached.
- Freeze stops requests and keeps the last applied output; Pull refreshes once.
- Bake produces the native per-frame bake; Clear Bake removes it.
- Scrub/playback with Grasshopper disconnected keeps the last output; use Bake
  for offline frames. This is the intended UX.
- No disk cache means no cache corruption, eviction, sidecar, or replay-failure
  class of bugs. Audit items P7–P10, E3, E8, and cache parts of E6 disappear.

**Migration:** opening a 0.6 project drops the removed RNA fields; Blender
ignores unknown properties. The cache on disk can be left in place or cleaned
by a one-time helper; it is disposable by ADR 0008. Add a test that a 0.6-like
stage with `live`/cache fields loads and runs.

**Docs:** README "what works", `plan.md` §2/§4, ADR 0008 (superseded), ADR 0009
(amended), new ADR "live-only stages; native bake is the offline path".

**Exit:** no cache/live code remains; all remaining tests express live
semantics; the suite is smaller and faster; freeze/bake cover the offline path.

---

### W5 — Bake dependents (0.7.0, D4)

Current code bakes the selected stage and freezes its **upstream** closure
(ADR 0014). The owner decision adds the dependent side, with a deliberate limit:
**only the selected stage is baked**. Every downstream stage that depends on it
is triggered so it sees the baked input, and is frozen when its answer lands.

**Design:**

- Compute the **downstream closure** symmetrically to the upstream walker:
  later stages on the same object, and stages on other objects/collections whose
  prefix reads this object, transitively, with a cycle guard. The closure is the
  connected dependency component, not only the same object.
- Bake only the selected stage's native Bake node over the scene range. Keep the
  current upstream closure freeze so the baked input is stable.
- After the target bake succeeds, walk the downstream closure in dependency
  order. Unfreeze each stage, force one request, wait for its answer using the
  same gate as Play Every Frame, and freeze it. Trigger and freeze one stage at
  a time so each sees the previous stage's output.
- Downstream stages are **not baked**. A frozen downstream stage keeps the single
  frame it evaluated; if it animates and needs offline playback, the user bakes
  it in turn. Document this limitation in ADR 0015 and the tooltip.
- Freeze bookkeeping stays per bake: the target carries its `bake_token`; every
  stage it froze (upstream or downstream) carries that token in
  `frozen_by_bake`. A stage the user had frozen before the bake keeps its own
  state and gets no marker.
- **Clear Bake on the selected stage clears only that stage's native bake and
  the freezes tagged with its token.** It never deletes or clears another
  stage's bake, and it never performs a chain-wide reset. If a downstream stage
  was frozen by this bake, clearing reverts it to live; if it had its own bake,
  that bake is untouched.
- Starting a bake on a stage that already carries a token: clear that stage's
  previous freeze set first (current rebake behavior), then create a new token.
  Do not adopt another stage's markers.
- Failure/cancel: delete the partial native bake created by this operation,
  unfreeze only the stages this operation froze, restore prior states, and
  report which frame/stage failed. Never leave a half-frozen dependent.
- One-way stages (no Stage Out) cannot be triggered for an answer; skip with a
  warning. Disabled stages have no live output; leave them as they are.
- Progress: stage name + frame for the target bake, then "triggered k/n
  dependent(s)" for the freeze pass. Move orchestration into `engine/bake.py`;
  the operator is a thin adapter.
- UI: Bake stage tooltip and a preview that names the target and the dependents
  it will trigger/freeze. No second "Bake all" operator.

**Tests:** 3-stage same-object chain; cross-object reference chain; bake middle
→ downstream is triggered with the baked input and frozen; only the middle stage
has native bake data; clear the middle bake unfreezes only its markers and
leaves a downstream stage's own bake intact; pre-frozen user stage preserved;
failed/cancelled bake rolls back the target bake and any downstream freezes;
rebake replaces the previous token; one-way skip; no Grasshopper request during
scrub/render for the baked stage; save/reload.

**ADR:** new 0015 supersedes 0014's downstream-stays-live decision; update 0014
status and the chain-bake benchmark.

**Exit:** baking a stage makes its dependents consistent and frozen without
baking them; Clear Bake restores exactly what that bake changed; no partial
state.

---

### W6 — State, diagnostics, UI purity (0.7.0)

- **State machine:** one state/transition table; `enabled` and `frozen` are
  flags, not states; `conflict` is explicit; illegal transitions fail tests.
  Remove `live` from the model (W4).
- **Props:** independent min/max bounds; apply only declared sides; preserve
  `max: 0`; a real type-migration function tested for int↔float, vector3,
  color, value_list, text, bool; index new entries after assigning the id; no
  blanket `except: pass`.
- **UI purity:** no RNA writes or depsgraph tags during draw; enum updates in
  callbacks; throttle per-stage error logging to state changes; linear
  group/orphan scans; explicit notice severity/expiry; expose Start Server where
  an offline user can find it.
- **Labels (D5):** editable stage label, unique default naming, modifier/helper
  names derived from it, key immutable; rename test with helper cleanup.
- **Operators:** correct `UNDO` flags, polls with reports, linked-object
  refusal, a shared stage-operator mixin, one safe-unregister helper.
- **Diagnostics:** remove test-only `_waiting_objects` or give it a consumer;
  `waiting_seconds` is zero for non-waiting states.

**Exit:** UI tests exercise parameter rows and Bake; every property migration is
tested; draw has no side effects.

---

### W7 — Architecture seams (0.8.0)

- **Grasshopper:** split `GhLink` into `StageRegistry`, `LinkConnection`,
  `RequestScheduler`, `ProtocolHandler`, `Publication`, `StageSlot`/`HeldFrame`;
  `GhLink` becomes a facade. Components become thin adapters with
  `StageInBuilder` and `StageResponseBuilder`; a shared base owns lifecycle.
  Unit-test each unit; keep `LinkTests` as integration proof. A module over ~600
  lines is a review trigger, not a failure.
- **Blender geometry:** split `geometry_io.py` into a package: `mesh`,
  `attributes`, `curves`, `points`, `instances`, `dispatch`. Table-drive the
  read/write type dispatch and the curve-section builders; keep a facade.
- **Blender engine:** replace the package-global hub with an `Engine` object and
  per-stage runtime records; submodules import explicit interfaces, not
  `sys.modules[__package__]`; delete the 157-name `__all__` and extraction
  residue; bake orchestration in `engine/bake.py`.
- **Nodes/helpers:** stable identity for helpers/definitions; rename migration;
  `_group_matches` verifies structure or bumps the format hash;
  `ensure_modifier` handles a missing group; correct insertion index.
- **Props/prefs:** no dead `None` branches; public APIs instead of
  `ops.STACK_TYPES`/`prefs._pref` imports from the UI; platform-correct data root
  without import side effects.
- **Characterization tests first:** snapshot behavior for add/delete, prefix
  evaluation, freeze/bake, and solve timing before moving code.

**Exit:** no god module; dependencies point one way; each extracted unit has
direct tests; behavior unchanged except documented fixes.

---

### W8 — Performance (0.8.0)

- **Gates:** `bench.py --check-baseline` compares p50/p95 per scenario to the
  stored JSON. **Fail over 5% on any metric.** A 0–5% regression is allowed
  only with a quantified offsetting gain recorded in the task; add bake/cascade
  time and memory; a 200-stage scale scenario; assert scenario setup (instance
  rows, vertex counts).
- **C# hot path:** `Section` as owner+offset+length views; avoid the 3–5 mesh
  copies; typed decode with one copy; reuse hashing/hex; JSON/attribute
  allocation cleanup.
- **MiniWs:** single-buffer send with in-place masking; cap before allocation;
  large-frame tests. (If the .NET spike succeeds, this file disappears.)
- **Publication:** dirty-flag publication at `SolutionEnd`/heartbeat; schema
  signature excludes `default`; one hello/schema pass per change.
- **Stage In/Out:** reuse the last built geometry for a `ref` with the same hash;
  skip serialization/hash when the answer cannot be sent; cache section name
  maps; fewer `ActiveDoc`/`Find` lookups.
- **Blender:** cache `ensure_group` scene signature per pass; avoid full
  `bpy.data` scans on every apply; direct array writes in `sections_to_mesh`;
  avoid re-reading edge/loop indices; linear stage/param grouping.
- **Python codec:** `b"".join`, `memoryview`/`unpack_from`, numpy fast path for
  large sections if profiling justifies it.
- **Cache/bake:** no custom cache. Measure native bake/cascade, simulation-zone
  cache if adopted.

**Exit:** baseline gate green; p95 within budget; no >5% regression without an
accepted offsetting gain; no unbounded growth in a 10k-touch/1k-frame soak;
200-stage panel draw and 100k-triangle apply measured.

**T10c scale evidence (2026-11-19):** the 200-stage panel draw measured
9.69/9.78 ms p50/p95, the exactly-100k-triangle in-place apply 2.17/2.22 ms,
and the 10k-instance repeated apply 81.88/91.93 ms; the runner records
structural assertions and RSS rather than adding a new timing gate. See the
T10c verification below and [`performance-ledger.md`](performance-ledger.md).

---

### W9 — Lean, legacy, docs, CI, framework (0.8.0)

- **Delete dead code:** cache/live residue (W4); `params.clamp`/`ANIMATABLE`/
  `PARAM_TYPES`, `attributes.GROUPS`, duplicate `PROTOCOL_VERSION`,
  `_waiting_objects` (or consumer), SURFACE engine path, `Instances.Emit`
  overload, unused icons, duplicate `FirstLine`, `build-core.sh`, unused
  `.gitkeep`, old local artifacts.
- **Compatibility policy (D3):** inventory every shim; ADR + migration test +
  removal version; rename internal node-group keys with a format bump.
- **Protocol tables (done T9b):** `protocol/sections.json` is the one source for
  dtype/hash-order/fixture/warning/version tables, generating the Python and C#
  tables; `scripts/check-protocol-tables` fails CI when either or the fixture
  inventory drifts.
- **Docs truth pass:** Python 3.13; `MiniWsClient` transport; ADR 0006/0008 data
  root and cache removal; ADR 0009/0014/0015 bake semantics; ADR 0010 status;
  ADR 0003 configurable-vs-fixed deviation; unsupported §8.8; fixture list;
  `__init__`/`DEMO` "v0"; the ruff scope after the wheel move; `.gitignore`
  duplicates.
- **Archive (done in T2):** the `spikes/` evidence tree, the archived
  Grasshopper 2 plan and the 2026-11-13 handoff are removed; the history stays in
  git and in `CHANGELOG.md`, not in the current tree.
- **Build:** reproducible extension zip (fixed mtimes, sorted entries, recorded
  SHA-256) and reproducible `.gha` (Mono `csc` `/deterministic`; copied-tree
  check); deploy script derives the wine user; install-addon stops deleting a
  shared package; `test-gha --require-all` fails on skipped suites; version
  extracted in one helper.
- **Extension/wheel (done in T2):** the official wheel is the only `websockets`
  source; the vendored tree is deleted and the wheel digest stays pinned (ADR
  0018). Validate manifest and zip in CI.
- **Framework spike (D7):** run the `net48`/`net7.0-windows` spike; adopt
  transport/JSON migration only with a documented go decision; otherwise record
  the no-go and harden C# 5.
- **CI:** core, Blender, docs, extension and reproducible-zip checks; timeouts,
  least privilege, caching, artifact validation. The reference-machine
  benchmark, the local-only C# suite and the local `.gha` reproduction check
  are explicit skips, not silent gaps; no stock-runner codec job is claimed
  because its install was not verified.

**Exit:** no dead code; every shim has a policy; docs match reality; root
contains only current material; extension zip and `.gha` reproducible for the
same tree/toolchain; framework decision recorded.

---

### W10 — Verification (continuous, completed in 0.8.0)

- **Regression rule:** every closed defect gets a test that fails before the fix.
- **Fuzzing (T10a, done):** deterministic frame mutator (10k cases/language)
  asserting only classified errors; random geometry/attribute/instance round
  trips; malformed bake-marker state.
- **Conformance corpus:** Python↔C# frames, number formatting, hashes,
  attributes, empty geometry, multi-part/nested blocks; both suites consume it.
- **Coverage (T10b, done 2026-11-18):** core 90%, engine/geometry 80%, GH core
  80%; explicit exclude list for bpy glue and RhinoCommon-only C# files.
  `scripts/coverage` enforces the Python floors; measured 93.50% core and
  89.43% engine/geometry. `scripts/coverage csharp` measured 93.36% of the pure
  C# core with Linux Mono; `Attributes.cs` and `Conversion.cs` are the named
  RhinoCommon exclusions.
- **Scale/soak:** 200 stages, 100k triangles and 10k instances measured by
  `scripts/scale` with structural assertions (T10c, done 2026-11-19). The
  1-hour churn and bake/cascade soak (T10d, done 2026-11-19) ran 10,238 cycles
  with stage/object churn, playback, native bake/Clear Bake, reconnect and
  stale-answer checks, and passed with no invariant failure or unclassified
  exception.
- **Docs checks:** links, version/port/path consistency, ADR status, fixture
  count.
- **Test hygiene:** delete vacuous tests; bind real panel methods; no shared
  class state; dynamic ports; standalone modules; per-test temp roots.

**Exit:** the suite proves invariants, not just happy paths; a bug without a
test is an incomplete fix.

---

## 5. Milestones

### 0.7.0 — "No wrong data, no freeze, no leak, one cache fewer"

**Status: shipped 2026-11-16.** W0, W1a, W1b, W2, W3, W4, W5, W6 and the
0.7.0 regression tests from W10 are implemented through Q1–Q19 (Q5 merged into
Q4). W7–W9 were limited to what these fixes needed; they are not complete.

**Measured at close-out:** 110 core tests; 423 Blender headless tests plus the
offline gate; the C# codec and its seven console suites; fourteen golden
fixtures; the installed extension round trip; `scripts/check-all` green in
116.0 s.

**Exit criteria:**

- All blockers and majors from the audit are closed; no module below 7.
- Cache and Live are gone; every stage is live; Freeze and native Bake are the
  offline path.
- Nested blocks/instances round-trip both ways, including multi-part
  definitions; baking a stage triggers and freezes its dependents without baking
  them, and Clear Bake is scoped to the selected stage's bake.
- Regression tests exist for every closed defect.
- `check-all` is green and CI-enforced. The 1-hour churn soak is not claimed by
  0.7.0; it remains a W10 verification item for 0.8.0.
- 0.6.0 project migration test passes (stages, params, keyframes, helpers,
  animation; removed cache fields ignored).
- CHANGELOG, README, plan, protocol and ADRs describe the release accurately.

### 0.8.0 — "Architecture, speed, truth"

Includes W7, W8, W9 and completes W10:

- .NET framework: a time-boxed value check for a transport/JSON migration, with
  a written go/no-go. The value is maintainability and testability, not speed;
  it is not a release gate.
- Simulation-zone caching stays deferred to later R&D; it is not evaluated in
  0.8.0.

**Exit criteria:**

- Every module ≥ 8.5; no god module; no global mutable registry outside its
  owner; no dead code or un-owned shim.
- Performance baseline gate green; scale tests documented; no >5% regression
  without a quantified offsetting gain.
- Docs/ADR/CI factually consistent; spikes and stale planning archived.
- Reproducible extension zip and `.gha` for the same tree/toolchain, with a
  tag-triggered release workflow.
- A fresh audit against the same rubric finds no critical or major findings.

---

## 6. Sequencing and effort

| Phase | Workstreams | Effort (dev-days) | Depends on |
|---|---|---:|---|
| A. Guardrails | W0 | 2–3 | — |
| B. Simplification | W4 (cache/live removal) | 3–5 | W0, D2 |
| C. Correctness | W1a, W2 in parallel | 10–14 | W0, D1 |
| D. Blocks | W1b | 5–7 | W2 grammar, W1a |
| E. Lifecycle | W3 | 6–8 | W0, parts of W2 |
| F. Bake dependents | W5 | 5–7 | W3, W4, D4 |
| G. State/UI | W6 | 4–6 | W3, W4, W5 |
| **0.7.0** | A–G + regression tests | **35–50** | |
| H. Refactor | W7 | 8–12 | 0.7.0 |
| I. Performance | W8 | 6–9 | W7 |
| J. Lean/docs/CI/framework | W9 | 6–9 | 0.7.0 (continuous) |
| K. Verification | W10 | 5–7 | continuous |
| **0.8.0** | H–K | **25–37** | |

Total ≈ 59–86 dev-days for one developer (roughly 12–17 weeks), or ~7–10
calendar weeks with two people. The critical path is W4 → W1a/W2 → W1b → W3 →
W5/W6 → 0.7.0, then W7 → W8 → 0.8.0. W9 and W10 run continuously; do not leave
them to the end.

**First ten pull requests:**

1. W0 harness/CI/`check-all` + standalone test fix.
2. W4 remove custom cache and Live toggle; delete cache tests; migration test.
3. W2 Python total decoder + typed codes + boundary suite.
4. W2 C# validation choke point + bounds tests.
5. W2 number-format parity + corpus.
6. W1a topology signature + direct writes + tests.
7. W1b multi-part definitions + caps + fixtures (both directions).
8. W3 component base + Param lifecycle + document isolation.
9. W3 playback supersession + rid-scoped recovery + threading fix.
10. W5 trigger the downstream dependents after a bake; freeze each; keep Clear Bake scoped to one bake.

### 0.7.0 execution order (subagent tasks)

Each task is implemented by one subagent, audited against §3 and the task's
acceptance criteria, then committed before the next starts.

| ID | Task | Acceptance | Status |
|---|---|---|---|
| Q1 | W0a runner path, standalone modules, per-test timeout | `test_stage_index.py` passes standalone; all modules do; runner adds `REPO/blender` once; timeout fails with a useful log | Done |
| Q2 | W0b shared harness, dynamic ports, temp roots | one `harness.py`; no fixed ports or order-dependent state; duplicated `start_fake`/`pump`/`isolate_data_root` removed | Done |
| Q3 | W0c `check-all`, CI, docs/version check | `scripts/check-all` runs core, Blender, C#, extension, bench, docs; CI invokes it; runtime recorded | Done |
| Q4 | W4a remove custom frame cache | `core/cache.py`, `cache_ops.py`, cache tests and call sites gone; suite green; migration note | Done (Q5 merged here) |
| Q5 | W4b remove Live toggle and live branches | no `live`/`live_frame`; every stage live; playback/freeze/bake tests green; ADR/README updated | Done (merged into Q4) |
| Q6 | W2a total Python codec | malformed frames only raise typed errors; boundary suite; encoder validation; join | Done |
| Q7 | W2b C# validation choke point | section/descriptor dtype, lengths, casts, mesh/curve bounds; tests; no unclassified exceptions | Done |
| Q8 | W2c C# number parity and totality | Python-repr parity corpus; big integers classified; non-finite impossible; tests | Done |
| Q9 | W1a geometry correctness | topology signature; colour round trip; radii clear; warnings channel; mixed groups; curve validation; tests | Done |
| Q10 | W1b nested blocks/multi-part definitions | grammar + caps; both codecs; both directions; nesting/cycles/parts; fixtures; tests | Done |
| Q11 | W3a GH component base, Param lifecycle, document isolation | no registry leak; two documents isolated; duplicate keys strict; tests | Done |
| Q12 | W3b GH threading, one-way/empty, stop, MiniWs | no off-thread Rhino; empty refs; no retry storm; stop token; hardened transport; tests | Done |
| Q13 | W3c Blender playback/error/send/frozen pull | no infinite gate; rid-scoped recovery; no send race; frozen pull deadline; tests | Done |
| Q14 | W3d Blender object lifecycle, load, multi-scene, server | rename/delete cleanup; queue cleared on load; per-scene frames; bounded stop; tests | Done |
| Q15 | W5 bake dependents | downstream closure; trigger+freeze each; only target baked; scoped Clear Bake; ADR 0015; tests | Done |
| Q16 | W6a state machine and diagnostics | explicit transitions; flags vs states; no test-only state; legal-transition tests | Done |
| Q17 | W6b props ranges, migrations, ids | min-only/max-only/max-zero; int↔float and all type migrations; by_id order; tests | Done |
| Q18 | W6c UI purity, labels, operators | no draw writes; editable labels; correct undo flags/polls; panel tests draw params | Done |
| Q19 | W10/docs/0.7 release prep | blocked regressions fixed; docs/plan/README/ADRs true; full gate; version/changelog | Done |

### 0.8.0 execution order (subagent tasks)

| ID | Task | Acceptance | Status |
|---|---|---|---|
| T1 | Baseline, performance gate, code health | three-run best baseline; `--check-baseline` fails a +6% regression; ledger and code-health inventory; no production behavior change | Done 2026-11-16 (`c2ed250`) |
| T2 | Subtraction before refactor | dead material/aliases/vendored tree removed; shim ADR and wheel ADR; no behavior change; full gate green; production and total tree smaller | Done 2026-11-16 |
| T3 | Characterization tests for the seams | tests pin `geometry_io`, the engine hub, `GhLink` and `Instances`; no production edits | Done 2026-11-16 |
| T4 | `geometry_io` package split | one responsibility per module; no production net growth; full gate; in-place benchmarks within 5% | Done 2026-11-17 |
| T5 | Blender engine object | explicit `Engine`/runtime records; no package-global hub; lifecycle/playback/bake tests unchanged | Done 2026-11-17; 26/26 on the 2026-11-17 best baseline |
| T6 | `GhLink` split | registry/connection/scheduler/protocol/publication/slots split; document isolation and threading preserved | Done 2026-11-17; 685 C# checks, 26/26 in 173.1 s |
| T7 | `Instances` split and table-driven conversion | single-part/multi-part/nested/attribute round trips unchanged; production LOC down or flat | Done 2026-11-17; 685 C# checks, 26/26 in 176.1 s; native probe green |
| T8 | Performance follow-through | playback resolved or traded; p50/p95 within 5%; each fixed path measured | Done 2026-11-17; 26/26 in 159.7 s, worst p50 +1.81%; fresh apply −88.5%, C# decode −98.9% |
| T9 | Lean/docs/CI/build/framework | protocol tables generated; docs/ADR truth pass; reproducible extension zip and `.gha`; framework go/no-go | T9c no-go 2026-11-18 ([ADR 0019](decisions/0019-keep-csharp5-mono.md)); T9a done 2026-11-18 (one version source, reproducible zip and `.gha`, derived wine user, honest CI); T9b done 2026-11-18 (`protocol/sections.json`, generated Python/C# tables, drift and fixture checks; [ADR 0021](decisions/0021-generated-protocol-tables.md)); T9 complete |
| T10 | Verification | fuzz, coverage, scale and 1-hour soak green | T10a done 2026-11-18: 10,000 fixed-seed Python and C# frame mutations, geometry/attribute/instance round-trip fuzz, malformed bake markers; 133 core, 468 Blender and 689 C# checks. T10b coverage done 2026-11-18: core **93.50%** (floor 90%), engine/geometry **89.43%** (80%), pure C# core **93.36%** (80%; RhinoCommon files excluded). T10c scale done 2026-11-19: `scripts/scale` records 200 stages / exactly 100,000 triangles / 10,000 instances with 38/38 structural checks, 4.58 s and 524.0 MiB peak RSS. T10d soak done 2026-11-19: `scripts/soak` completed **10,238 cycles** in 3,600.2 s churn with **0 invariant failures**, 0 unclassified exceptions, 30/30 native bakes and 15 reconnects; the soak exposed and fixed a whole-object deletion session-state leak, raising the Blender count to **472** and production to 25,083 / 20,072. The post-fix full gate passed **26/26 in 181.7 s** (worst +0.31% p50 / +1.47% p95; no >5%; zip `fdbf973e…`, `.gha` `18c629e3…`). Native Windows/Rhino acceptance remains explicitly deferred after 0.8.0; no native-parity claim |
| T11 | 0.8.0 release | version/changelog/artifacts match the tagged commit | Pending |

### T8 verification (performance follow-through)

T8 measured before editing, then fixed the highest-value remaining costs. It
changes no protocol, fixture, `out_hash` or topology-token byte.

- **Blender mesh rebuild.** `geometry_io/mesh.py` writes the validated
  vertex/loop/polygon table in one bulk pass instead of a Python tuple per face
  plus `from_pydata`. The mixed 3/4/5-gon regression compares loop start/total
  and edge count against a `from_pydata` reference, and the existing
  `test_identical_topology_takes_the_position_only_fast_path` now patches the
  rebuild owner. `pipeline.100k.apply_fresh_ms` **77.9507 / 87.1304 →
  8.9585 / 9.1022 ms** (−88.51% / −89.55%).
- **C# section views.** A decoded `Section` is a frame owner/offset/length
  view; `ByteLength` reads the extent without a copy, `Raw` still returns the
  bytes with one cached materialisation, and typed access copies once. Fixtures,
  re-encode and hashes are byte-identical. `frame_decode` **0.4556 / 0.5167 →
  0.0050 / 0.0055 ms**; `frame_mesh_arrays` **0.9210 / 1.0338 → 0.4434 /
  0.4684 ms**. `Program.CheckSectionViews` pins the view/Raw semantics.
- **Publication generation.** `RebuildPublicationLocked` marks a generation
  only when the derived stages/roles/parameters change; connection open and
  doc/unit changes mark it too. A clean heartbeat returns before snapshotting
  or encoding signatures, and a mark made during a send stays pending.
  `LinkTests` adds "a clean publication sends no hello"; schema/reconnect/unit
  tests are unchanged and green.
- **Instance owner.** `InstanceEmit` accepts the caller's `RhinoDoc`; Stage Out
  passes `OnPingDocument().RhinoDocument`, and the `ActiveDoc` fallback remains
  for scripts/tests. The wire result is unchanged.
- **Playback.** Three quiet pre-edit solo runs measured p50 **13.0435 / 12.8746
  / 12.9812 ms** (p95 13.4197 / 13.3667 / 13.3492) against the stored
  12.8405/13.3398; the accepted gate measured **12.7994 / 13.2626**. That is
  +0.78% p50 against the historical 0.6 same-machine 12.7 ms, not +7–9%. The
  timer path was not edited; the exception is accepted with the actual ratio and
  the loaded-host caveat in the ledger.
- **Deferred, measured.** `ensure_group` 26.7 µs/apply, `_ordered_stages` 6.5 µs,
  `props.schema_of` 0.3 µs, `props.canonical_json` 2.5 µs,
  `bake.token_stages("")` 0.1 µs, `_flush_edit_mode` 0.4 µs and `_ensure_index`
  0.5 µs on the measured path. No C# profiler showed `StageRegistry`
  registration/slot scans or `DecodedFrame.Find` hot; `BuildMesh`/`MeshSections`
  have no verifiable native harness here. They are deferred with those reasons
  in the ledger, not silently dropped.

The full gate passed **26/26** in **159.7 s**; C# `scripts/test-gha` passed
**687** checks plus fourteen fixtures (the two native Rhino halves still print
their skips); the Blender suite is **455** tests plus the offline gate. The
complete p50/p95 table is in the
[performance ledger](performance-ledger.md) T8 verification.

### T9c framework decision (no-go)

**2026-11-18.** The `net48`/`net7.0-windows` spike is a **no-go for 0.8.0**.
C# 5/Mono and the hand-written `MiniWsClient`/`Json.cs` stay; no transport or
JSON migration is added now. The reasons are the verified `csc.exe`/wine loop as
the only development/test path, no `dotnet` SDK (T9c had no host Mono coverage
tool; T10b later measured the existing C# 5 code with a local Linux Mono
profiler, which is not a trial target), native Windows/Rhino acceptance
deferred until after 0.8.0, and the second runtime/platform or replacement
dev-loop cost of `net7.0-windows`/`net48`. The spike is closed in
[ADR 0019](decisions/0019-keep-csharp5-mono.md) and revisits only after native
Windows acceptance (post-0.8 / 1.0). **T9a and T9b are complete (2026-11-18);
T10a later completed the fuzz/invariant half, T10b added the coverage floors,
T10c added the scale run and T10d completed the soak; T11 remains, so T9 is
complete.**

### T9a verification (build/CI/docs truth)

**2026-11-18.** T9a changes no production, protocol, fixture or `out_hash`
byte, and runs no benchmark. It makes the surrounding claims true
([ADR 0020](decisions/0020-version-source-and-reproducible-artifacts.md)):

- **One version source.** `blender_manifest.toml` is the only release version
  literal. `scripts/version` reads it (default checkout path, a path argument,
  or `-` for stdin); `scripts/build-extension`, `install-extension`,
  `test-extension` and `install-addon` call it instead of carrying a grep/cut
  copy. `scripts/check-docs` keeps its direct manifest read (it validates the
  source) and now compares the C# `hello` string in `Publication.cs` and the
  protocol example to the manifest. The version stays **0.7.0** until T11.
- **Reproducible extension zip.** `scripts/build-extension` normalizes the
  Blender-built archive before validation: entries sorted by name, timestamp
  `1980-01-01 00:00:00`, fixed Unix file modes, preserved per-entry compression
  (the wheel stays stored), then Blender validates the rewritten zip. It prints
  and writes `dist/stageloom-<version>.zip.sha256`. Two independent builds from
  the **T9a tree** (historical; T9b later changed production source and rebuilt
  both artifacts) produced the same archive:
  `bfb250609feb188a458541c020be8c96049dab53ffb627e7d56a2f128882eb80`;
  `scripts/test-reproducible` proves it and is part of the full gate.
  The manifest, wheel digest and extracted contents were compared with the
  source: unchanged.
- **Reproducible `.gha`.** `grasshopper/build/build-gha.sh` passes Mono `csc`
  `/deterministic`, which fixes the assembly timestamp, module version GUID and
  MVID. No `/pathmap` is used: the build emits no debug info, and a build from
  a copy of the tree at another absolute path matched. Three builds of the
  **T9a tree** (historical; two in the tree, one copied tree) produced SHA-256
  `05c904d5bf5cd2012347aa9680edc3d8ade7f3854cc526e19a4cd77879d5efe4`;
  `scripts/test-reproducible-gha` proves it and restores the original built
  `.gha` on exit. Same tree + same Mono/`csc` and referenced Rhino/Grasshopper
  assemblies is the claim; a different toolchain can differ. T9b rebuilt both
  artifacts on the current tree; see the T9b verification below for those
  hashes.
- **Deploy user.** `grasshopper/deploy/deploy.sh` resolves the Windows user
  under `$WINEPREFIX/drive_c/users` rather than hardcoding one; it prefers
  `STAGELOOM_WINE_USER`, skips `Public`, and fails with the candidates when
  there are zero or multiple users. `--dry-run` prints the result without
  copying. On the current prefix it resolves user `miquel` and the existing
  Libraries path; no redeploy was performed. `scripts/check-deployed` already
  scanned every user and was left with an explicit comment not to narrow it.
- **CI truth.** The quality job still runs docs, ruff, core, Blender headless
  plus offline gate, the installed-extension round trip, the baseline-gate
  self-test and the two-build extension-zip reproducibility check. It no longer
  runs the timing benchmark: the stored 2026-11-17 best-of-three baseline is
  reference-machine-only. C# and the `.gha` reproduction check remain
  local-only: a stock Ubuntu runner has no wine/wine-mono and no
  RhinoCommon/Grasshopper. `scripts/test-gha --codec-only` passed locally
  against a fresh `WINEPREFIX` with system wine (no Rhino assemblies), but a
  stock-runner install was not verified, so no codec-only CI job was added and
  the workflow claims no C# coverage. Native Windows/Rhino acceptance is
  deferred until after 0.8.0; no native-parity or framework migration is
  claimed ([ADR 0019](decisions/0019-keep-csharp5-mono.md)).
- **Deferred in T9a, later completed in T9b.** The full gate was not run during
  T9a because the user was using the machine. `scripts/test-blender` was also
  blocked because the live Blender session held `127.0.0.1:59130`; the same
  three `EngineUpdateTests` errors reproduce on a clean `git archive HEAD`
  tree, so they are environmental/test isolation, not a T9a regression. T9b
  later ran the full gate alone: **177.7 s, 26/26, baseline enforced**; core,
  extension, extension-reproducibility, `.gha` reproducibility, docs, ruff and
  codec-only checks passed.

### T9b verification (protocol single-source tables)

**2026-11-18.** T9b adds one checked-in protocol source and generated language
tables without changing a wire byte, fixture, `out_hash` or runtime algorithm
([ADR 0021](decisions/0021-generated-protocol-tables.md)):

- **Single source.** `protocol/sections.json` is canonical JSON for the protocol
  version, the four dtypes (size, struct format, numpy dtype), the fixed
  geometry-hash section order, all 14 attribute types (wire dtype, components,
  UI label), the 10 warning codes, the three codec error codes and the 14 fixture
  names with coverage notes. Fixture directories still own the bytes.
- **Generated outputs.** `scripts/generate-protocol-tables` writes
  `blender/stageloom/core/_protocol_tables.py` (Python 3.13) and
  `grasshopper/src/core/ProtocolTables.generated.cs` (C# 5). Python production
  imports the generated version/dtype/codec-code/hash-order/`WIRE_TYPES`
  constants with the existing public names; `FrameCodec` aliases the generated
  version, hash order, dtype set and item sizes; `AttributeStore.Types` is built
  from the generated attribute rows plus the fixed `StorageKind` selection; and
  the generated `WarningCodes` partial class replaces the hand-written literals.
- **Drift gate.** `scripts/check-protocol-tables` regenerates in memory, compares
  both files byte for byte and validates the source fixture names against the
  directories and their `frame.bin`/`expect.json`. It is wired into
  `scripts/check-all` quick and full and the stock-runner `core` CI job.
  `scripts/check-docs` now requires the source names to match the directories and
  the `protocol/fixtures/README.md` table to match the source.
- **Tests.** The new `blender/tests/core/test_protocol_tables.py` checks the
  source, generated Python, live Python tables and fixture dirs, and proves a
  mutated temporary source and a missing fixture directory make the drift check
  fail. The C# console suite pins the generated version, dtype sizes, hash order,
  attribute rows and warning values; `AttrTests` checks every generated label in
  `AttributeStore`.
- **Measurements.** Production is **25,040 physical / 20,035 code lines across
  71 files** (+122 / +90 against T8's 24,918 / 19,945 / 69). The two generated
  files are 184 physical / 156 code lines; the hand-written diff is
  +59/−121 physical (net −62) and −66 code lines, so the duplicate literals are
  gone and the checked-in generated tables are the net addition. The full
  `scripts/check-all` gate then ran alone in **177.7 s** with the 2026-11-17
  baseline enforced (26/26 checks; worst p50 +2.23% and p95 +3.31% on
  `pipeline.100k.total_ms`; 132 core, 455 Blender and 689 C# checks; both
  artifacts reproduced). The baseline identity now compares processor, OS,
  architecture, Blender and Python, not the kernel patch; the host moved from
  Linux 7.3.4 to 7.3.5 without invalidating the reference.
- **Artifacts.** T9b adds generated files to the shipped add-on and plugin, so
  the T9a artifact hashes no longer describe this tree. On the T9b working tree,
  `scripts/test-reproducible` built the extension twice and reproduced
  `d12b6f504196d24ceaeb8c3896f162f48dbdef36346dd7112100b8e539b79b88`;
  `scripts/test-reproducible-gha` built twice in-tree and once from a copied
  tree and reproduced
  `18c629e3fba03d30b6605ea53b2dd77221533f3f469e9b10a3ff1c57ee8ceafb`;
  both passed. `scripts/test-extension` passed the installed-extension round trip
  on the rebuilt zip. The hashes differ from T9a because the zip now contains
  `core/_protocol_tables.py` and the edited core modules, and the `.gha`
  contains `ProtocolTables.generated.cs` plus the edited `Frame.cs`,
  `Attributes.cs` and `Conversion.cs`; the version and build toolchain are
  unchanged. T11 records the final release-tree hashes.
- **Verification.** `scripts/test-core` passed **132 tests** (123 before; +9
  protocol-table tests); `scripts/test-gha` passed **689 checks** including the
  fourteen fixture checks across the eight console suites (687 before; +2
  generated-table checks, the attribute-store label conformance included);
  `--codec-only` passed as well. `scripts/check-protocol-tables`, `scripts/check-docs`,
  `uvx ruff@0.17.2 check .`, `git diff --check` and
  `scripts/code-health --check` passed. The fixture tree is byte-identical
  (aggregate `sha256sum` of all `frame.bin` files before/after:
  `ebb0e33cded09bc48c0742a14e1468c8bb3a191b78a7f5e181b04050315a6a59`), and a
  before/after run over every fixture produced identical frame, `sections_hash`
  and Python `output_hash` values. The C# `GeometryHash`/`OutputHash` bodies were
  not edited.

### T10a verification (fuzz and invariants)

**2026-11-18.** T10a adds tests only; production source is unchanged and no
benchmark was run. Seeds are fixed (`20261118` in both languages) and every
campaign is bounded.

- **Python frame fuzz.** `blender/tests/core/test_codec_fuzz.py` runs exactly
  **10,000** fixed-seed cases in `scripts/test-core` (0.27 s standalone, 0.38 s
  for the 133-test core suite). Ten strategies mutate valid encoded frames:
  length prefix, raw byte, truncation, appended junk, random bytes, header text,
  structured header, frame splice, oversized declaration and section/payload
  surgery; 35% of cases add a second mutation. Every failure must be
  `codec.CodecError` with a code in `CODEC_CODES`; successes must agree with the
  declared table and payload bytes and survive a re-encode. The campaign
  observed `bad_frame`, `too_large` and `proto_mismatch`; no unclassified
  exception was found, so no production fix was needed.
- **C# frame fuzz.** `grasshopper/tests/console/FuzzTests.cs` builds
  `StageloomFuzzTests.exe` in `grasshopper/build/build.sh` and runs it from
  `scripts/test-gha` (full and `--codec-only`). It mutates the same ten
  categories with a deterministic xorshift32 stream: **10,000** frame cases plus
  **1,000** direct `Json.Parse` cases (0.53 s standalone under wine). Only
  `FrameException` with `bad_frame`, `too_large` or `proto_mismatch` (or
  `JsonException` on the direct JSON boundary) is accepted; successful frames
  get the same table/payload/re-encode coherence check. No unclassified
  exception was found.
- **Geometry/attribute/instance fuzz.** `blender/tests/headless/test_fuzz_roundtrip.py`
  adds 13 tests: 80 random mixed tri/quad/n-gon meshes through
  `mesh_to_sections`/`sections_to_mesh`, colours (POINT and folded CORNER,
  BYTE/FLOAT) and creases; 80 topology-token cases proving the fast path reads
  no polygon/loop arrays and a changed token rebuilds; 80 generated meshes
  exercised through the wire `crv`/`pts` path (face meshes skipped), including
  loose edges, isolated vertices and the counted crease loss note; random
  attributes across every wire type and domain, 60 malformed
  wire attributes (notes, never a crash) and a randomized prune; 20 valid
  definition frames with multi-part/empty parts/nested rows/cycles/attributes,
  40 malformed definition frames classified as `GeometryIOError`, and 8
  randomized scenes through the existing Geometry Nodes builders; 80 bake-marker
  cases plus stale deleted records and a malformed clear-bake recovery. No
  production bug was exposed.
- **Counts and deferrals.** `scripts/test-core` passed **133 tests**;
  `scripts/test-blender` passed **468 tests** in 86.9 s plus the offline gate;
  `scripts/test-gha` passed the fixture runner and all eight console suites
  (**689 checks**, 14 fixtures, **10,000** fuzz cases) in 25.5 s. Per the task
  constraints, the reference benchmark, `scripts/bench-baseline`, the full
  `scripts/check-all` and the 1-hour soak were not run. T10b later added the
  coverage floors (below), T10c added the scale run and T10d ran the soak.

### T10b verification (coverage floors)

**2026-11-18.** Tooling/tests only: no production source changed, no benchmark
or full gate was run, and the version stays 0.7.0. Coverage is an additional
measurement; `scripts/test-core`, `scripts/test-blender` and `scripts/test-gha`
keep working without a coverage package.

- **Core (floor 90%).** `scripts/coverage core` runs the 133-test core suite
  under coverage.py 7.17.3 (pinned; this run used `uvx coverage@7.17.3`) with
  source `blender/stageloom/core`. There are **no omit patterns**: the source
  root is the shipped package, tests live outside it, and the generated
  `_protocol_tables.py` is measured (100%). Result **93.50%** (374/400
  statements; 26 missed), PASS in 1.0 s. The remaining misses are
  `attributes.py` 67,69, `codec.py` error/edge branches, `hashing.py` 60 and
  `params.py` cast branches.
- **Engine/geometry (floor 80%).** `scripts/coverage engine` installs the same
  pinned coverage.py into a private target with Blender 5.3's bundled Python
  3.13 pip (no shipped `sys.path` change), then runs the existing headless suite
  through `scripts/coverage-blender.py` with source
  `blender/stageloom/blender/engine` and
  `blender/stageloom/blender/geometry_io`. No omit patterns. Result **89.43%**
  (3,594/4,019 statements; 425 missed), PASS. All 468 tests passed in 89.2 s
  (90.2 s including Blender startup). A preceding run measured 89.45%
  (3,595/4,019) because one timer-dependent `bake.py` branch flipped, so the
  number is stable to one statement, not exact to the byte. The lowest files
  are `playback.py` 84.05%, `instances.py` 84.80% and `messages.py` 86.15%; no
  file is below the floor.
- **C# Option A — measured, not an exception (floor 80%).** The machine has
  no system `mono`/`mcs`/`monocov`/`dotnet` and no `sudo`; wine-mono supplies
  the compiler/runtime inside wine but no host executable or coverage tool. A
  Linux Mono 6.12.0.206-2 package (Arch `extra`, fetched from a mirror and
  unpacked to a temporary prefix for this run; not committed and not a
  shipped/CI dependency) provided `mono`, `mcs` and Mono's built-in
  `--profile=coverage` profiler; the separate `monocov` GUI was not needed
  because the profiler emits per-line statement counts. Reproduce with
  `MONO_HOME=<mono-prefix> scripts/coverage csharp`. The core
  and fuzz suites were rebuilt from the same `grasshopper/src/core` sources with
  `mcs -debug` (no Rhino types) and run under the profiler. The union of
  `StageloomCoreTests.exe` (14 fixtures) and `StageloomFuzzTests.exe` (10,000
  frame + 1,000 JSON cases) covers **93.36%** (1,209/1,295 statement lines) of
  the pure core, PASS in 2.0 s: `Frame.cs` 91.98%, `Json.cs` 94.59%,
  `ParamValues.cs` 91.45%, `Endpoint.cs` 96.43%, generated
  `ProtocolTables.generated.cs` 100%. **Excluded by name and reason**:
  `Attributes.cs` (RhinoCommon `ArchivableDictionary`; exercised by
  `StageloomAttrTests.exe`, 43 checks under wine) and `Conversion.cs`
  (RhinoCommon Mesh/Curve/Point; exercised by `StageloomConversionTests.exe`,
  37 checks under wine). `scripts/coverage-csharp.py` fails if any other
  `src/core` file is neither measured nor excluded. No `dotnet`, Rhino or
  Windows dependency was added; the C# measurement is local-only
  (ADR 0019/CI).
- **CI.** The Python 3.13 core job installs coverage 7.17.3 and runs
  `scripts/coverage core`; the Blender quality job runs
  `scripts/coverage engine` after `scripts/check-all --ci`. C# coverage is not
  a CI job because a stock runner has no Mono/Rhino toolchain.
- **Not run.** Benchmarks, `scripts/bench-baseline` and full `scripts/check-all`;
  the T10c scale run and T10d soak are recorded separately below.

### T10c verification (scale)

**2026-11-19.** Runner/tests/docs only; production source is unchanged and no
benchmark baseline was rebuilt or compared. The dedicated runner is
`benchmarks/scale.py` + `scripts/scale`; it runs from the working tree under
`blender -b --factory-startup --online-mode` and is not part of the default
benchmark matrix or `scripts/check-all`.

- **Shapes and assertions.** **200 stages** are one mesh object with 200 stage
  records, built through the real `stageloom.add_stage` operator: 200 labels in
  creation order, 200 unique keys, 200 modifiers, a forced index rebuild with
  200 keys / 200 owners / one stack object / zero conflicts, `ordered_stages` in
  record order, a real N-panel draw of all 200 rows with one Pull per stage and
  no index rebuild, and a no-client engine pass with zero prefix clones.
  **100k triangles** are exactly 100,000 triangles as a 250 × 200 quad grid
  (50,000 quads, 50,451 vertices): the source, fresh apply and in-place target
  each report 100,000 loop triangles, and the in-place call runs with
  `_topology_matches` patched to raise so a topology re-read fails. **10k
  instances** are one 2-triangle definition on a 10,000-point cloud: one
  definition, 10,000 `inst.ref_def` rows all pointing at definition 0, 10,000
  4×4 transforms, one 10,000-point row cloud, one definition collection, 10,000
  evaluated instances, one instance reference and group reuse on the repeated
  apply.
- **Run.** `scripts/scale --out benchmarks/results/2026-11-19-scale.json` passed
  **38/38** structural checks in **4.58 s** with **524.0 MiB** process peak RSS
  (`VmHWM`). p50 / p95: 200-stage index rebuild 1.13 / 1.17 ms, ordered stages
  7.55 / 7.56 ms, panel draw 9.69 / 9.78 ms, no-client engine pass 34.64 /
  34.66 ms (200-stage build 2,957 ms, one sample); 100k-triangle read
  4.93 / 4.95 ms, fresh apply 8.97 / 9.05 ms, in-place apply 2.17 / 2.22 ms;
  10k-instance cold prefix 13.63 / 13.99 ms, first apply 84.30 ms (one sample),
  repeated apply 81.88 / 91.93 ms. The table and caveats are in
  [`performance-ledger.md`](performance-ledger.md) and
  [`../benchmarks/README.md`](../benchmarks/README.md).
- **Gate policy.** No new timing gate: the 100k in-place path is already gated
  by `pipeline.100k.apply_inplace_ms` (4 ms budget) and the repeated instance
  apply at 13 rows by `instances.13x.apply_repeat_ms`; neither metric changed.
  The 200-stage index/panel/no-client pass and the 10,000-row apply are not in
  the default matrix, so their times are diagnostic and no baseline-relative
  claim is made.
- **Tests.** `blender/tests/headless/test_scale_shapes.py` asserts the same
  three shapes without timing; the focused module passed 3 tests in 3.3 s. The
  full `scripts/test-blender` suite passed **471 tests** in 90.0 s plus the
  offline gate, up from 468. `scripts/test-core` is unchanged at 133 tests; the
  C# checks are unchanged at 689.
- **Not run.** The reference benchmark, `scripts/bench-baseline`, full
  `scripts/check-all` and native Windows/Rhino acceptance. Production changed
  nowhere; the scale paths are measured, not modified. T10d later ran the soak
  separately.

### T10d verification (1-hour churn soak)

**2026-11-19.** Runner/tooling/docs plus one minimal cleanup fix; no protocol,
fixture or wire byte changed and no benchmark baseline was rebuilt or compared.
The dedicated runner is `benchmarks/soak.py` + `scripts/soak`; it runs from the
working tree under `blender -b --factory-startup --online-mode`, uses one real
server and real `tools/fake_gh` clients, and is not part of `scripts/check-all`
or the benchmark matrix.

```bash
scripts/soak --duration 3600 --out benchmarks/results/2026-11-19-soak.json
```

- **Per cycle.** Add a stage and start its fake; send full and parameter-only
  requests; rename the stage label and stack object; run the engine's undo/redo
  handlers; disable/enable; freeze/unfreeze; seed a two-part definition and both
  row-cloud families; delete the stage; assert no helper, definition, cloud,
  parameter, runtime, verify, prefix-memo, playback-report or node-group entry
  survived. Every fifth cycle adds and deletes a whole stack object. Every cycle
  runs three keyed playback frames. Scheduled work covers a native Geometry Nodes
  bake over three frames, an offline output check, Clear Bake, and a full client
  stop/start reconnect with a stale answer from the old client.
- **Guards.** A 60 s per-cycle deadline, a hard wall-clock guard, a bounded
  playback/bake wait (5 s per stage) and an outer `timeout(1)`; any wait,
  engine trace error or invariant failure exits non-zero.
- **Result.** **PASS** — **10,238 cycles** in **3,600.2 s** of churn
  (**3,600.4 s** wall), **0 invariant failures**, **0 unclassified exceptions**
  and 0 engine errors. 10,238 stage adds/deletes, full/ref requests,
  stage/object renames, undo/redo handler passes, disable/enable cycles,
  freeze/unfreeze cycles and definition seeds; 10,238 playback cycles
  (**30,714 frames**); 2,047 whole-object delete cycles;
  **30/30** native bakes with 0 marker fallbacks; **15** reconnects; **10,253**
  classified stale-answer recoveries. RSS **368.6 → 399.0 MiB** (peak 399.0);
  slowest cycle 0.64 s. Final live scene: 3 stack objects, 4 stages, 4 registry
  keys/owners, 12 helpers, 4 groups, 0 pending/dirty/verify/reports; teardown
  zeroes every registry/helper/session count. All 71 named invariant groups
  passed.
- **Defect fixed.** Whole-object deletion left the stage's `StageRuntime`,
  verify request, prefix memo and playback report behind forever; the
  modifier-deletion path already cleaned them. `Engine.prune_session_state`
  (called from `_rebuild_index`) now retires absent keys and dirty names, and
  `_drop_removed_stages` drops the playback report. Regression:
  `test_lifecycle.py::test_07_object_delete_drops_only_the_dead_session_state`
  and the existing stage-control deletion test. Production is **25,083 physical
  / 20,072 code lines across 71 files** (+43 / +37); `scripts/test-core` passes
  133 tests and `scripts/test-blender` passes **472** tests in 89.8 s plus the
  offline gate. The coverage floors were rechecked after the fix: core
  **93.50%** (90%), engine/geometry **89.46%** (80%; `runtime.py` 95.67%) and
  pure C# core **93.36%** (80%, unchanged).
- **Deferrals.** The native bake is Blender's Geometry Nodes bake run headlessly,
  not native Windows/Rhino acceptance. Native Windows/Rhino acceptance is
  deferred until after 0.8.0 by owner decision; no native Windows run was
  attempted and no native-parity claim is made. Chain evidence is the
  2026-11-15 live chain-bake under wine (real Grasshopper, three Stage In/Out
  pairs; bake/freeze/offline render/save-reload/Clear Bake/live resume) and the
  headless `test_chain_bake.py` / `test_chain_playback.py` suites. Blender's real
  undo history cannot run in `-b`; the engine handlers a real history step runs
  are exercised, with global undo disabled so the harness does not accumulate
  steps.
- **Post-fix gate.** After the cleanup fix, `scripts/check-all` ran alone on a
  quiet host and passed **26/26 in 181.7 s** with the baseline enforced (no
  baseline was rebuilt). The worst positive delta was **+0.31% p50**
  (`playback.frame_ms`) and **+1.47% p95**
  (`pipeline.100k.apply_inplace_ms`); no metric exceeded +5%. It ran 133 core,
  472 Blender and 689 C# checks. The extension zip rebuilt to
  `fdbf973e710c65ceb37c89520827c5a7f992742b44b9ebf0c3519196bccbb98d`
  because the cleanup fix changed the add-on source; the unchanged C# tree
  still reproduces the `.gha` at
  `18c629e3fba03d30b6605ea53b2dd77221533f3f469e9b10a3ff1c57ee8ceafb`.
  T11 owns the version bump, tag, release notes and final release-tree
  hashes.

**Commit rule:** a task is committed only after its acceptance tests plus
`scripts/test-core` and the relevant Blender/C# suite pass, and the audit finds
no unresolved blocker or major issue. Commit messages name the task ID and the
invariant it strengthens.

---

## 7. Risk register

| Risk | Mitigation |
|---|---|
| Removing the cache regresses scrub/playback UX | Freeze/Bake are the documented offline path; measure live scrub; the simulation-zone cache is deferred until a later R&D decision; release notes explain the change. |
| Downstream bake dependents become complex or slow | One `engine/bake.py` owner; dependency closure with cycle guard; trigger-and-freeze one dependent at a time; rollback on failure; ADR 0015; tests for every branch. |
| Multi-part protocol change breaks a saved workflow | Single-part frames stay byte-identical; `caps` negotiation; fixtures; both halves ship together; fallback with warning. |
| .NET spike breaks the wine dev loop | No-go recorded (T9c, ADR 0019); C# 5/Mono stays. Revisit only after native Windows/Rhino acceptance. |
| Simulation-zone cache duplicates the Bake node | Deferred to later R&D; the criteria remain documented, and no second cache ships in 0.7/0.8. |
| Refactor changes behavior | Characterization tests before moves; refactor commits contain no behavior changes; extract one module at a time. |
| CI cannot run Rhino | CI skips C# and the `.gha` reproduction check explicitly and claims neither; `scripts/test-gha --codec-only` is verified locally with a fresh prefix, and the full suite plus `.gha` check are local/release gates. |
| Performance work destabilizes correctness | Gate first, then zero-copy; no optimization without a failing measurement. |
| Scope creep into features | Any new feature request is deferred post-1.0; this plan changes behavior only for the recorded simplifications and bug fixes. |
| Single-platform blind spots | Native Windows/Rhino acceptance is deferred until after 0.8.0; 0.8 makes no native-parity claim and records the manual results when they exist. |

---

## 8. Tracking

- Create one issue per row of the defect register, labelled `0.7.0` or `0.8.0`,
  with the acceptance test named.
- A release is blocked while any `blocker` or `major` issue for that milestone
  is open.
- Every PR states which invariant it strengthens and which test proves it.
- Re-run the audit rubric at the 0.7.0 and 0.8.0 code freezes; publish scores in
  the release notes.

### Defect register (audit → workstream)

| ID | Finding | Sev | Workstream |
|---|---|---|---|
| G1 | `sections_to_mesh` ignores same-count topology changes | blocker | W1a |
| G2 | `Separate Children` writes duplicate `idef.*` sections | blocker | W1b |
| E1 | Playback pump can block the main thread forever | blocker | W3 |
| H1 | `ParamComponent` never unregisters | blocker | W3 |
| H2 | RhinoCommon accessed off the UI thread | blocker | W3 |
| B1 | Multi-part block definitions collapse | blocker | W1b |
| B2 | Nested blocks/instances not proven both ways | major | W1b |
| B3 | Outbound graph-side cycles unguarded | major | W1b/W3 |
| B4 | Instance names collide; content hash omits descriptors | major | W1b |
| B5 | Point members/curve attributes lost | major | W1b |
| P1 | Python decoder raises `TypeError`/`KeyError` on malformed sections | major | W2 |
| P11 | Hashing goldens absent (`request_id`, `output_hash`) | major | W10 |
| G3 | `FLOAT_COLOR` linear/sRGB mismatch | major | W1a |
| G4 | Point radii not cleared when absent | major | W1a |
| G5 | Catmull-Rom/handle drops without a stage warning | major | W1a |
| G6 | Helper/definition identity tied to object names | major | W3/W7 |
| E2 | Error recovery ignores rid | major | W3 |
| E4 | `send()` check-then-enqueue race | major | W3 |
| E5 | Frozen Pull lost answer loops once a second | major | W3 |
| E6 | Object delete/rename leaks helpers/caches | major | W3 |
| E7 | `_ensure_index` O(N) per stage pass | major | W8 |
| E9 | Multi-scene uses active scene's frame | major | W3 |
| E10 | Incoming queue not cleared on load | major | W3 |
| E12 | States are patched strings; illegal transitions reachable | major | W6 |
| U1 | One-sided ranges invent 0/1 | major | W6 |
| U2 | int→float migration silently dropped | major | W6 |
| U4 | Bake orchestration lives in an operator, untested | major | W5/W7 |
| U5 | Every stage labelled "Stage" | major | W6 |
| C5 | `FormatDouble` diverges from Python repr | major | W2 |
| C6 | Big integers silently become doubles | major | W2 |
| C7 | `NaN` breaks schema publishing | major | W2 |
| C11 | Mesh decode lacks bounds checks | major | W2 |
| C12 | Mesh/SubD groups mixed | major | W2 |
| C13 | Null curve reported as sampled | major | W1a/W2 |
| C19 | Attribute dtype not validated | major | W2 |
| C20 | Descriptor casts throw unclassified | major | W2 |
| C21 | `count`/`components` ignored | major | W2 |
| C29 | `Conversion.cs` has no console coverage | major | W10 |
| C30 | `OutputHash` untested | major | W10 |
| H3 | `GhLink` is a 1,635-line god object | major | W7 |
| H4 | Process-global registries, no per-document isolation | major | W3/W7 |
| H5 | Duplicate keys last-writer merged | major | W3 |
| H6 | One-way stages retry up to eight times | major | W3 |
| H7 | Empty geometry cannot be served by ref | major | W3 |
| H9 | Components own 100–180 lines of business logic | major | W7 |
| H12 | Outbound instance cycles unguarded | major | W1b/W3 |
| H19 | MiniWs handshake/accept unverified | major | W2/W3 |
| H21 | MiniWs allocates before the frame cap | major | W3 |
| H35 | Publication O(components × stages) per solve | major | W8 |
| H36 | Stage In rebuilds all geometry for refs | major | W8 |
| H40–H44 | Lifecycle/instances/param/MiniWs/attribute tests absent | major | W10 |
| T1 | Deploy script hardcodes a user | major | W9 — resolved T9a |
| T2 | Install-addon deletes a shared package | major | W9 |
| T4 | Extension zip and `.gha` not reproducible | major | W9 — resolved T9a |
| T11 | Bench budgets 3–300× loose, no baseline | major | W8/W10 |
| T18 | Test modules depend on import order | major | W0/W10 |
| T24 | README claims Python 3.14 (Blender uses 3.13) | major | W9 |
| T25 | Protocol claims `ClientWebSocket` | major | W9 |
| T38 | CI runs only 57 core tests | major | W0/W10 |
| CACHE1 | Custom frame cache, Live toggle, cache-based request paths | removed | W4 |
| CACHE2 | Cache/P7–P10, E3, E8 defects | removed | W4 |
| BAKE1 | Bake froze upstream only; downstream stayed live | fixed in W5 | W5: bake the selected stage, trigger + freeze each downstream dependent once, Clear Bake scoped to its token (ADR 0015) |
| FW1 | Hand-rolled MiniWs/JSON on C# 5 | spike | W9 |
| UI6/U10–U14, P12–P19, G7–G13, E13–E20, C14–C18, C22–C28, C31–C33, H25–H34, H37–H39, H45, T3, T5–T10, T12–T17, T19–T23, T26–T37 | Minor/nit sweep | minor | W6–W9 |

**TBD:** the full minor/nit list lives in the review notes; each item is assigned
to a workstream above and closed or explicitly accepted with a rationale in the
0.8.0 release notes.

---

## 9. Non-goals

- No new user-facing features beyond the D1 nested-blocks correctness work
  (read-only GH→Blender values, materials, layers, native B-rep, GH2 stay
  post-1.0).
- No breaking protocol redesign (no v3).
- No custom frame cache; no second cache mechanism.
- No Blender add-on framework change; no C# domain rewrite.
- No promise of a stable bake/cache disk format.
