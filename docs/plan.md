# Stageloom — 1.0 plan

**Status:** 1.0 planning, 2026-11-16; 0.8.0 in progress, 2026-11-18. Baseline:
**v0.7.0**.
**Target:** Blender 5.3. **Compatibility floor:** Blender 5.2.
**Goal:** ship a stable 1.0 for the declared v1 scope: Grasshopper definitions as
stackable Blender modifiers, with geometry and parameters flowing both ways,
animation and playback, live stages, and renderable native bakes.

**0.8.0 status (2026-11-18).** T1–T9, T10a (fuzz/invariants) and the T10b
coverage floors are complete; T10b scale/1-hour soak and T11 (release) remain.
Production is 25,040 physical / 20,035 code lines across 71 files, with 133
core, 468 Blender headless and 689 C# checks plus the fixed-seed 10,000-case
Python and C# frame fuzz campaigns. Coverage is measured at **93.50%** core
(90% floor), **89.43%** engine/geometry (80%) and **93.36%** of the pure C# core
(80%; the two RhinoCommon files are named exclusions). The release version
stays **0.7.0** until T11. Native Windows/Rhino acceptance is deferred until
after 0.8.0; 0.8.0 makes no native-parity claim. The `.NET` framework migration
is a recorded no-go through 1.0 ([ADR 0019](decisions/0019-keep-csharp5-mono.md));
C# 5/Mono and the hand-written MiniWs/JSON transport stay. The protocol tables
are generated from `protocol/sections.json` and drift-checked in CI
([ADR 0021](decisions/0021-generated-protocol-tables.md)).

**Authority:** this document is the roadmap for 1.0. The wire contract is
[`protocol.md`](protocol.md); architecture decisions are in
[`decisions/`](decisions/); [`../README.md`](../README.md) describes the shipped
product; [`../CHANGELOG.md`](../CHANGELOG.md) records what changed. The previous
plan is in git history.

---

## 1. What 1.0 means

1.0 is a stability and compatibility release, not a feature expansion. It ships
when all of the following are true:

- The declared v1 feature set works without known correctness defects.
- There is no known release-blocking performance cliff.
- Opening a project from an earlier release preserves stages, parameters,
  keyframes, helpers and animation, and the upgrade path is tested.
- The supported platforms have been exercised end to end, not only under wine.
- The wire protocol and component identities are frozen as a compatibility
  contract.
- The extension zip and `.gha` are reproducible from a tagged tree for the same
  toolchain, validated, and installable by the supported routes.
- The release test matrix passes on every supported platform.

No new user-facing feature is required for 1.0 unless it is needed to close one
of the gaps below.

---

## 2. Baseline: v0.7.0

**Shipped:** live-only stages (the custom frame cache and the `Live` toggle are
removed; Freeze and the native Bake node are the offline path), nested and
multi-part Grasshopper block definitions in both directions with the `blocks/2`
capability, bake stages that trigger and freeze downstream dependents while only
the selected stage is baked, a total Python/C# codec (typed errors, strict
JSON/UTF-8/number validation, exact Python-repr float formatting, payload
bounds), geometry correctness for topology, colour, point radii, mixed groups
and curves with visible conversion warnings, document-scoped Grasshopper
ownership and UI-thread marshaling, a hardened WebSocket transport,
supersession-aware playback and rid-scoped recovery, stable helper identity
across rename/delete/load/multi-scene, editable stage labels, independent
parameter ranges with real type migrations, and an explicit `conflict` state.
Meshes, curves, point clouds, SubD cages, Breps (meshed, warned), attributes,
parameters with keyframes and drivers, mid-stack `GH → GN → GH` chains, Pull,
Freeze, Clear Bake, Key all, Purge orphans, Play Every Frame, Frame Drop,
generator stages, one-way Stage In feeds, the persisted loopback endpoint
component, the non-metre units matrix and the reference performance matrix
remain.

**Verified at release:** 110 core tests, 423 Blender headless tests plus the
offline gate, the C# codec and its seven console suites, fourteen golden
fixtures, the installed extension round trip, and `scripts/check-all` in 116.0 s
on the development machine.

**Performance check (2026-11-14):** the 13× 99,458-triangle reference scene
updates in 60.6 ms median (64.5 ms p95) with preview on, inside the 100 ms
target; preview off is within noise. See
[`../benchmarks/results/2026-11-14-live-rhino.md`](../benchmarks/results/2026-11-14-live-rhino.md).

**Known gaps carried forward:**

| Gap | Why it blocks 1.0 |
|---|---|
| No native Windows Rhino acceptance run | Wine is a development environment, not the target user platform. Native acceptance is deferred until after 0.8.0; 0.8.0 makes no native-parity claim. |
| No GH component migration | Old documents keep old parameter/output lists and never gain newer capabilities. |
| No chain-level Windows/Rhino playback/bake proof | Bake stage freezes the upstream chain and triggers plus freezes downstream dependents (ADR 0015); the 0.7.0 acceptance is headless, so a Windows/Rhino chain render is still missing. |
| Release verification is manual and single-machine | The extension zip and `.gha` are reproducible for the same tree/toolchain and checked, but the C# suite, `.gha` check and full platform matrix remain a local/release gate rather than a stock-runner CI claim. |

---

## 3. Supported platforms

**Tier 1 — must pass for 1.0**

- Windows 11 x64
- Rhino 8 with Grasshopper 1
- Blender 5.3

**Tier 2 — supported add-on, no Rhino on the same OS**

- Blender 5.3 on Linux and macOS
- The `.gha` is Windows-only; Linux + wine remains the development and test
  environment, not a user-facing Rhino platform.

**Not supported in 1.0**

- Rhino 9 / Grasshopper 2
- Rhino 8 on macOS (revisit after Windows parity; decide and record)
- Blender versions below 5.2 (the parameter UI uses the layered-action API)
- Remote or non-loopback connections

The support matrix is a release gate, not a documentation claim. The acceptance
matrix runs on the target, Blender 5.3; Blender 5.2 remains the compatibility
floor.

---

## 4. Scope

### In 1.0

- Everything already shipped in v0.7.0.
- The remaining hardening work in §5.
- Migrations and compatibility guarantees in §5C.
- Platform and distribution work in §5B and §5F.

### Out of 1.0

- GH → Blender values as read-only outputs or drivers (planned for v1.1).
- Materials, layers, and named object identity from GH.
- Multiple Grasshopper documents answering one stage key.
- SubD semi-sharp and vertex creases.
- Returning curve attributes to Blender (the legacy Curve helper cannot store
  them).
- Native B-rep/surface transfer; Breps remain a warned, lossy mesh conversion.
- Grasshopper 2 / Rhino 9 support; out of scope through 1.0, with no separate
  plan carried in this repository.
- A .NET framework migration (`net48`/`net7.0-windows`): C# 5/Mono and the
  hand-written MiniWs/JSON transport remain through 1.0, and the spike is a
  recorded no-go ([ADR 0019](decisions/0019-keep-csharp5-mono.md)); revisit it
  only after native Windows/Rhino acceptance (1.0 or later).
- A custom frame cache or any second offline cache; Freeze and the native Bake
  node are the offline path. A simulation-zone cache stays later R&D.

These are scope decisions, not hidden gaps. If 1.0 changes scope, this section
changes first.

---

## 5. Workstreams

### A. Performance and responsiveness

**Problem.** The 300–800 ms instance report did not reproduce on the reference
scene. The 13× 99,458-triangle case updates in 60.6 ms median (64.5 ms p95)
with preview on, inside the 100 ms target; preview off is within run-to-run
noise. The original FiraTower file is not in the repository, so that specific
report remains unexplained until it is re-run.

**Work**

- The benchmark harness is in place: `scripts/bench` builds the reference scenes
  and enforces the budgets; its 2026-11-14 Linux baseline is in
  `benchmarks/results/`.
- The live half is scripted too: `benchmarks/live_scene.py` saves the cube and
  13× scenes, and `benchmarks/live_client.py` measures one scene against a live
  Rhino. The first result is
  `benchmarks/results/2026-11-14-live-rhino.md`: ~13 ms of Blender-side work,
  ~47 ms of Grasshopper round trip, ~60 ms total.
- Re-run the original FiraTower file if it becomes available; otherwise the
  reference scene is the performance budget.
- Keep the regression budgets current so future changes cannot silently
  reintroduce the cliff.

**Proposed targets** (confirm against the measured baseline):

- 100k-triangle round trip remains ≤ 50 ms.
- Instance-heavy Stage In update < 100 ms on the reference scene with preview on
  (live Rhino).
- Idle engine pass < 2 ms; multi-stage dirty pass < 100 ms.
- 1M-triangle scene evaluates without a crash or an unbounded memory increase;
  record the budget rather than promising 60 fps.
- Bake time and memory are measured on the reference scene range.

**Exit:** the reference matrix is in the repository, both the Blender-side and
live instance cases are inside budget, and the 1.0 performance report is
reproducible from the scripted harness.

### B. Platform support

**Work**

- Run the full acceptance matrix on Windows 11 with Rhino 8 and Blender 5.3.
- Build the `.gha` on Windows as well as under wine; verify the deployed copy and
  the Rhino restart path.
- Test the Blender add-on on Windows: extension install, data root, paths,
  permissions, port binding and restart.
- **Endpoint:** done in 0.6.0 (ADR 0011): the **Stageloom Connection**
  component persists a validated loopback port for the installation;
  `STAGELOOM_PORT` remains a development fallback only.
- Decide and record the macOS position. If it is supported, run the Blender
  add-on acceptance there; if not, say so explicitly.
- Keep loopback-only as a hard rule. Do not add remote networking in 1.0.

**Exit:** the platform matrix in §3 passes, and platform-specific installation and
path behavior is covered by tests or a recorded manual checklist.

### C. Compatibility and migration

**Work**

- Freeze protocol v2 as the 1.0 wire contract. Additive fields are allowed;
  breaking changes require protocol v3. Both codecs already share golden
  fixtures; extend them for any additive field.
- Keep stage keys, parameter GUIDs, component GUIDs and persisted custom-property
  names stable.
- Add a `Read()` migration to the Grasshopper components so documents saved by
  earlier versions gain newer parameters and outputs without being rebuilt.
- Add `.blend` migration coverage for stage records, node-group formats, helper
  ownership, parameter properties, keyframes, drivers, frozen/baked flags and
  duplicated stacks.
- Add an add-on ↔ `.gha` version handshake. If the two sides are incompatible,
  say so in the panel and in Grasshopper; if they are compatible, connect
  silently.
- Keep the 0.7.0 simplification: there is no custom frame cache to version
  ([ADR 0016](decisions/0016-live-only-stages.md)). Native bake data is
  Blender's, not the add-on's.

**Exit:** projects saved by the last two public releases open and work, old GH
components gain new outputs, and the compatibility policy is enforced by tests
or a checked handshake.

### D. Playback and bake at chain scale

**Problem.** Bake stage bakes the selected stage, freezes its upstream closure
and triggers plus freezes its downstream dependents ([ADR 0015](decisions/0015-bake-triggers-downstream.md)),
but the chain-scale acceptance is headless. A Windows/Rhino chain render and the
one-frame limit of a frozen dependent still need an explicit acceptance run.

**Work**

- Keep the multi-stage Play Every Frame test: each stage in stack order, one
  request per frame, no skipped frames, and disconnect behavior.
- Keep the chain-level bake acceptance on the D4 shape: one native bake on the
  selected stage, upstream frozen for a stable input, downstream asked once and
  frozen on that answer. Do not add **Bake all**.
- Test: bake over a scene range, render with Grasshopper closed, Clear Bake
  resumes live output, and a packed bake survives save/reload. Cover a
  downstream stage that needs its own bake for per-frame offline output.
- Ensure a failed or timed-out dependent rolls the whole bake back and reports
  the stage and frame; no half-frozen chain.

**Exit:** a `GH → GN → GH` chain can render with Grasshopper closed from a baked
state, and Clear Bake restores live behavior without restarting Blender.

### E. Robustness and quality

**Work**

- **Units:** covered for millimetre, centimetre, metre, inch and foot documents
  by `grasshopper/tests/live/UnitsCheck.cs` and `ComponentTests`; keep meshes,
  curves, points, blocks and attributes in the matrix as they change.
- **Brep/surface meshing:** add automated coverage for the lossy Brep path and
  resolve or bound the known meshing issues. Keep the warning path explicit.
- **Bake undo:** settled as irreversible with confirmation (ADR 0012); keep the
  warning and prompt covered by `test_bake_safety.py`.
- **Scale:** test large geometry, many stacks, many parameters and sustained
  live playback.
- **Failure injection:** malformed frames, dropped connections, stale responses,
  port conflicts, multiple Blender instances, and a missing/deleted node group.
- **Threading:** keep the GH-object UI-thread invariant, including the `hello`
  units read.

**Exit:** the robustness matrix passes; no known silent data-loss path remains.

### F. Release engineering and distribution

**Work**

- CI: layer A, the Blender quality gate (headless against `fake_gh`, offline
  gate, extension round trip, baseline-gate self-test and the reproducible-zip
  check) run on pull requests. Benchmarks are reference-machine-only and C#
  (including the reproducible `.gha` check) is local-only (ADR 0019); a
  stock-runner C# codec job and a self-hosted Rhino smoke job remain future
  work, and neither is claimed by the current workflow.
- Add tag-triggered build and validation for `stageloom-<version>.zip` and
  `Stageloom.gha`; fail on a version mismatch or a validator error.
- Build both artifacts from the tagged commit; record their SHA-256 hashes.
- Decide the distribution routes:
  - GitHub release (required)
  - extensions.blender.org (recommended for 1.0; recreate the listing packet
    from the release notes at that time)
  - Yak package for `Stageloom.gha` (recommended; manual copy remains the
    fallback)
- Add a version-mismatch warning to the panel and the plugin.

**Exit:** a tag produces validated artifacts with recorded hashes, and a clean
machine can install through each supported route.

---

## 6. Milestones

Milestones are engineering gates, not feature releases. No new scope is added
after 0.8 except fixes required by a gate.

| Milestone | Content | Exit |
|---|---|---|
| **0.6** | Performance harness and instance fix; units matrix; plugin endpoint; bake undo safety | Shipped 2026-11-14 |
| **0.7** | Quality release: live-only stages, nested/multi-part blocks, bake dependents, codec/geometry hardening, lifecycle/UI/state; custom cache and `Live` removed | Shipped 2026-11-16; full gate green, no known blocker or major from the 2026-11-15 audit |
| **0.8** | Architecture seams (W7), performance gates (W8), lean/docs/CI (W9), verification (W10), tag-triggered distribution; native Windows/Rhino acceptance deferred beyond 0.8; .NET migration recorded as a no-go ([ADR 0019](decisions/0019-keep-csharp5-mono.md)) | Every module ≥ 8.5; baseline gate green; docs/CI true; reproducible extension zip and `.gha` from a tag for the same tree/toolchain; no native-parity or framework-migration claim |
| **0.9** | Release candidate; soak; freeze protocol v2 and file formats; full acceptance on supported platforms | No open release-blocking defect; all gates below pass |
| **1.0** | Release | Artifacts, notes and hashes published; support matrix signed off |

---

## 7. 1.0 acceptance gates

The old A1–A10 remain the functional core. Add the release gates that the
0.5.x line did not cover.

- **A1 Keyframes.** Keyframed parameters drive the GH output per frame; every
  frame is a live request, and a disconnected stage keeps its last output.
- **A2 Drivers.** A driver on a custom property moves the GH output live.
- **A3 Chain.** A `GH → GN → GH` chain evaluates without manual steps.
- **A4 Stack edits.** Reorder, disable and enable create no datablocks; restored
  states queue one pass and settle without a duplicate apply.
- **A5 Curves.** Representable curves round-trip exactly; approximations are
  counted and warned.
- **A6 SubD.** Cage and edge creases round-trip; the semi-sharp limitation is
  visible.
- **A7 Persistence.** Undo/redo, save/reload, and upgrades from 0.6.0 and 0.7.0
  preserve stages, parameters, animation and helpers.
- **A8 GH lifecycle.** Closed, reopened and restarted Grasshopper reconnects with
  no manual step and keeps the last good output while disconnected.
- **A9 Performance.** The reference matrix meets the budgets in §5A, including
  the instance-heavy case.
- **A10 Duplicates.** Duplicated stacks and stages keep independent keys,
  session identities, groups, helpers and animation; a saved or undone shared key
  shows the explicit `conflict` state and sends/applies nothing for the affected
  stages instead of silently splitting them.
- **A11 Platform.** Windows 11 + Rhino 8 + Blender 5.3 passes the full matrix;
  the Linux/wine development matrix remains green.
- **A12 Chain playback/bake.** Multi-stage Play Every Frame works, and a
  multi-stage bake renders with Grasshopper closed and restores live behavior
  with Clear Bake. The headless chain is green in 0.7.0; the Windows/Rhino
  render remains the 1.0 acceptance.
- **A13 Compatibility.** Old GH components gain new outputs through migration;
  an incompatible add-on/plugin pair reports a clear mismatch; protocol v2
  remains interoperable.
- **A14 Units and Breps.** Non-metre Rhino documents round-trip correctly; the
  lossy Brep path is covered and warned.
- **A15 Release.** Tagged artifacts are built from a clean tree, validated,
  hashed, installable by the supported routes, and reproducible for the same
  toolchain (extension zip in CI, `.gha` in the local/release gate).

**1.0 is blocked by any failing A-gate or any known silent data-loss path.**

---

## 8. Release checklist

- [ ] Version bumped everywhere (manifest, `.gha` hello, protocol example,
      fixtures comment, README, tests).
- [ ] `CHANGELOG.md` has the 1.0 section with upgrade notes and known
      limitations.
- [ ] `docs/plan.md`, `README.md`, `docs/protocol.md` and compliance notes agree.
- [ ] Core, Blender, C#/codec, extension and platform suites pass.
- [ ] Windows live acceptance and multi-stage bake acceptance recorded.
- [ ] `stageloom-1.0.0.zip` and `Stageloom.gha` built from the tag and validated.
- [ ] SHA-256 hashes recorded in the release notes.
- [ ] Tag `v1.0.0` pushed; GitHub release published.
- [ ] Extension-platform and Yak artifacts published, or explicitly deferred
      with the reason recorded.
- [ ] Deployed `.gha` matches the release artifact; Rhino restart verified.

---

## 9. Risks and decisions

| Risk | Mitigation |
|---|---|
| Instance latency is caused by the Grasshopper document, not the bridge | Measure first; publish the reference definition and budget. If the document dominates, document the limit and keep the bridge out of the blame. |
| Native Windows exposes compiler, path or threading differences | Make Windows a Tier 1 gate; build and run the full matrix there before 1.0. |
| A `Read()` migration silently corrupts old definitions | Build fixture documents from older releases; test parameters, outputs and component GUIDs. |
| Baking a chain leaves a downstream stage live or stale | Bake stage triggers and freezes every downstream dependent on its answer (ADR 0015); only the selected stage is baked. Test the chain with Grasshopper closed. |
| CI cannot run Rhino | Use a self-hosted Windows/Rhino runner; keep the headless Blender and C# suites in public CI. |
| Scope creep delays 1.0 | Freeze scope at 0.8; new features wait for 1.1 or later. |

**Decisions to confirm**

1. Is macOS Rhino 8 in the 1.0 support matrix, or explicitly out?
2. Is extensions.blender.org a 1.0 requirement or a post-1.0 distribution step?
3. Is Yak packaging for the `.gha` required for 1.0?
4. Does 1.0 ship Rhino 8/GH1 only, with GH2 in a later release?
5. Is the 0.7 native-bake scope (only the selected stage is baked; downstream
   dependents are frozen on one frame each) enough for animation-heavy 1.0
   chains, or does 1.0 need a chain-level bake? (The custom frame cache is gone;
   see ADR 0016.)

---

## 10. References

- [`protocol.md`](protocol.md) — wire protocol v2.
- [`decisions/`](decisions/) — architecture decisions, including live-only
  stages (0016), compatibility shims (0017), the official wheel (0018) and the
  C# 5/Mono no-go (0019).
- [`extensions-compliance.md`](extensions-compliance.md) — current platform
  compliance record.
- [`../README.md`](../README.md) — product overview and supported requirements.
- [`../CHANGELOG.md`](../CHANGELOG.md) — release history and known limitations.
