# Stageloom 0.7.0 / 0.8.0 — quality plan (rev. 2)

**Status:** proposed, 2026-11-15, revised with owner decisions.
**Baseline:** v0.6.0 plus the 2026-11-15 audit (seven module reviews), plus the
post-audit bake/upstream work (`102f9c8..254d796`). Current local baseline:
**305 Blender headless tests in ~86 s, green**, 57 core tests in ~8 ms, C# and
extension suites green.
**Goal:** reach the highest practical point on the rubric without changing the
product's design or feature set, except for the deliberate simplifications
recorded in §2.
**Authority:** `plan.md` remains the 1.0 roadmap, `protocol.md` the wire
contract, ADRs the decision log. This document owns the 0.7/0.8 quality work and
records where those documents must change.

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
  add/delete/rename/undo, playback, bake, and cache/bake churn.
- Zero silent-loss paths: every approximated or dropped entity produces a
  user-visible stage warning; fuzz/round-trip tests show only classified errors.
- No dead exported symbol; no test-only API in shipped code; no TODO/FIXME.
- `scripts/check-all` completes in ≤ 3 minutes on the development machine and
  is what CI runs.
- Benchmarks: p50/p95 within 10% of the stored baseline or the build fails.
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
| D7 | **Blender extension compliance is a first-class constraint.** | W9. The add-on stays pure Python + official wheel; evaluate dropping the vendored tree; validate the artifact. |

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
- make the **wheel** the only `websockets` source (evaluate deleting the 21k-line
  `vendor/` tree; dev install can install the wheel or the extension);
- validate the manifest and the built zip in CI;
- 0.8.0 build artifact reproducible and traceable to a tag.

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
- Static checks: correct ruff vendor path, shellcheck, C# warnings-as-errors.

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
  stored JSON; fail over 10% or a documented absolute limit. Add bake/cascade
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

**Exit:** baseline gate green; p95 within budget; no unbounded growth in a
10k-touch/1k-frame soak; 200-stage panel draw and 100k-triangle apply measured.

---

### W9 — Lean, legacy, docs, CI, framework (0.8.0)

- **Delete dead code:** cache/live residue (W4); `params.clamp`/`ANIMATABLE`/
  `PARAM_TYPES`, `attributes.GROUPS`, duplicate `PROTOCOL_VERSION`,
  `_waiting_objects` (or consumer), SURFACE engine path, `Instances.Emit`
  overload, unused icons, duplicate `FirstLine`, `build-core.sh`, unused
  `.gitkeep`, old local artifacts.
- **Compatibility policy (D3):** inventory every shim; ADR + migration test +
  removal version; rename internal node-group keys with a format bump.
- **Protocol tables:** one source (e.g. `protocol/sections.json`) generating
  dtype/hash-order/fixture/warning-code tables for Python and C#; CI checks the
  generated files are current.
- **Docs truth pass:** Python 3.13; `MiniWsClient` transport; ADR 0006/0008 data
  root and cache removal; ADR 0009/0014/0015 bake semantics; ADR 0010 status;
  ADR 0003 configurable-vs-fixed deviation; unsupported §8.8; fixture list;
  `__init__`/`DEMO` "v0"; ruff vendor path; `.gitignore` duplicates.
- **Archive:** move `spikes/` to `docs/evidence/` with curated results and a
  short index, or remove runnable pre-rebrand scripts from the 1.0 tree; archive
  `plan-gh2.md` and the 2026-11-13 handoff, or mark them historical.
- **Build:** reproducible extension zip (fixed mtimes, sorted entries, recorded
  SHA-256); deploy script derives the wine user; install-addon stops deleting a
  shared package; `test-gha --require-all` fails on skipped suites; version
  extracted in one helper.
- **Extension/vendor:** evaluate making the official wheel the only
  `websockets` source and deleting `vendor/`; if kept, it is dev-only and
  license-complete. Validate manifest and zip in CI.
- **Framework spike (D7):** run the `net48`/`net7.0-windows` spike; adopt
  transport/JSON migration only with a documented go decision; otherwise record
  the no-go and harden C# 5.
- **CI:** core, Blender, codec, bench baseline, docs, extension build; timeouts,
  least privilege, tag/release workflow, artifact validation. Name the
  codec-only job honestly instead of silently skipping.

**Exit:** no dead code; every shim has a policy; docs match reality; root
contains only current material; artifacts reproducible; framework decision
recorded.

---

### W10 — Verification (continuous, completed in 0.8.0)

- **Regression rule:** every closed defect gets a test that fails before the fix.
- **Fuzzing:** deterministic frame mutator (10k cases/language) asserting only
  classified errors; random geometry/attribute/instance round trips; malformed
  bake/cache state.
- **Conformance corpus:** Python↔C# frames, number formatting, hashes,
  attributes, empty geometry, multi-part/nested blocks; both suites consume it.
- **Coverage:** core 90%, engine/geometry 80%, GH core 80%; explicit exclude
  list for vendored/bpy glue.
- **Scale/soak:** 200 stages, 100k triangles, 10k instances, 1-hour churn,
  bake/cascade churn.
- **Docs checks:** links, version/port/path consistency, ADR status, fixture
  count.
- **Test hygiene:** delete vacuous tests; bind real panel methods; no shared
  class state; dynamic ports; standalone modules; per-test temp roots.

**Exit:** the suite proves invariants, not just happy paths; a bug without a
test is an incomplete fix.

---

## 5. Milestones

### 0.7.0 — "No wrong data, no freeze, no leak, one cache fewer"

Includes W0, W1a, W1b, W2, W3, W4, W5, W6, and the regression tests from W10.
W7–W9 are limited to what these fixes need. No cosmetic refactors while
correctness work is open.

**Exit criteria:**

- All blockers and majors from the audit are closed; no module below 7.
- Cache and Live are gone; every stage is live; Freeze and native Bake are the
  offline path.
- Nested blocks/instances round-trip both ways, including multi-part
  definitions; baking a stage triggers and freezes its dependents without baking
  them, and Clear Bake is scoped to the selected stage's bake.
- Regression tests exist for every closed defect.
- `check-all` is green and CI-enforced; 1-hour churn soak clean.
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
- Performance baseline gate green; scale tests documented.
- Docs/ADR/CI factually consistent; spikes and stale planning archived.
- Reproducible artifacts and a tag-triggered release workflow.
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

| ID | Task | Acceptance |
|---|---|---|
| Q1 | W0a runner path, standalone modules, per-test timeout | `test_stage_index.py` passes standalone; all modules do; runner adds `REPO/blender` once; timeout fails with a useful log |
| Q2 | W0b shared harness, dynamic ports, temp roots | one `harness.py`; no fixed ports or order-dependent state; duplicated `start_fake`/`pump`/`isolate_data_root` removed |
| Q3 | W0c `check-all`, CI, docs/version check | `scripts/check-all` runs core, Blender, C#, extension, bench, docs; CI invokes it; runtime recorded |
| Q4 | W4a remove custom frame cache | `core/cache.py`, `cache_ops.py`, cache tests and call sites gone; suite green; migration note |
| Q5 | W4b remove Live toggle and live branches | no `live`/`live_frame`; every stage live; playback/freeze/bake tests green; ADR/README updated |
| Q6 | W2a total Python codec | malformed frames only raise typed errors; boundary suite; encoder validation; join |
| Q7 | W2b C# validation choke point | section/descriptor dtype, lengths, casts, mesh/curve bounds; tests; no unclassified exceptions |
| Q8 | W2c C# number parity and totality | Python-repr parity corpus; big integers classified; non-finite impossible; tests |
| Q9 | W1a geometry correctness | topology signature; colour round trip; radii clear; warnings channel; mixed groups; curve validation; tests |
| Q10 | W1b nested blocks/multi-part definitions | grammar + caps; both codecs; both directions; nesting/cycles/parts; fixtures; tests |
| Q11 | W3a GH component base, Param lifecycle, document isolation | no registry leak; two documents isolated; duplicate keys strict; tests |
| Q12 | W3b GH threading, one-way/empty, stop, MiniWs | no off-thread Rhino; empty refs; no retry storm; stop token; hardened transport; tests |
| Q13 | W3c Blender playback/error/send/frozen pull | no infinite gate; rid-scoped recovery; no send race; frozen pull deadline; tests |
| Q14 | W3d Blender object lifecycle, load, multi-scene, server | rename/delete cleanup; queue cleared on load; per-scene frames; bounded stop; tests |
| Q15 | W5 bake dependents | downstream closure; trigger+freeze each; only target baked; scoped Clear Bake; ADR 0015; tests |
| Q16 | W6a state machine and diagnostics | explicit transitions; flags vs states; no test-only state; legal-transition tests |
| Q17 | W6b props ranges, migrations, ids | min-only/max-only/max-zero; int↔float and all type migrations; by_id order; tests |
| Q18 | W6c UI purity, labels, operators | no draw writes; editable labels; correct undo flags/polls; panel tests draw params |
| Q19 | W10/docs/0.7 release prep | blocked regressions fixed; docs/plan/README/ADRs true; full gate; version/changelog |

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
| .NET spike breaks the wine dev loop | Time-boxed; test both `net48` and `net7.0-windows`; adopt only on Windows + wine; otherwise record no-go and keep C# 5. |
| Simulation-zone cache duplicates the Bake node | Deferred to later R&D; the criteria remain documented, and no second cache ships in 0.7/0.8. |
| Refactor changes behavior | Characterization tests before moves; refactor commits contain no behavior changes; extract one module at a time. |
| CI cannot run Rhino | Explicit codec-only job; full layers on the dev machine and release checklist; do not claim coverage that does not exist. |
| Performance work destabilizes correctness | Gate first, then zero-copy; no optimization without a failing measurement. |
| Scope creep into features | Any new feature request is deferred post-1.0; this plan changes behavior only for the recorded simplifications and bug fixes. |
| Single-platform blind spots | Windows acceptance run for 0.8.0; record Blender/macOS results in release notes. |

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
| T1 | Deploy script hardcodes a user | major | W9 |
| T2 | Install-addon deletes a shared package | major | W9 |
| T4 | Extension zip not reproducible | major | W9 |
| T11 | Bench budgets 3–300× loose, no baseline | major | W8/W10 |
| T18 | Test modules depend on import order | major | W0/W10 |
| T24 | README claims Python 3.14 (Blender uses 3.13) | major | W9 |
| T25 | Protocol claims `ClientWebSocket` | major | W9 |
| T38 | CI runs only 57 core tests | major | W0/W10 |
| CACHE1 | Custom frame cache, Live toggle, cache-based request paths | removed | W4 |
| CACHE2 | Cache/P7–P10, E3, E8 defects | removed | W4 |
| BAKE1 | Bake freezes upstream only; downstream stays live | changed | W5 (trigger + freeze downstream, bake only the selected stage) |
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
