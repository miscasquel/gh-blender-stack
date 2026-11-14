# Stageloom — 1.0 plan

**Status:** planning, 2026-11-14. Baseline: **v0.6.0**.
**Target:** Blender 5.3. **Compatibility floor:** Blender 5.2.
**Goal:** ship a stable 1.0 for the declared v1 scope: Grasshopper definitions as
stackable Blender modifiers, with geometry and parameters flowing both ways,
animation and playback, a frame cache, and renderable native bakes.

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
- Release artifacts are reproducible from a tagged commit, validated, and
  installable by the supported routes.
- The release test matrix passes on every supported platform.

No new user-facing feature is required for 1.0 unless it is needed to close one
of the gaps below.

---

## 2. Baseline: v0.6.0

**Shipped:** meshes, curves, point clouds, SubD cages, Breps (meshed, warned),
attributes, Grasshopper blocks/instances, parameters with keyframes and drivers,
mid-stack `GH → GN → GH` chains, stage lifecycle (Pull, Freeze, Bake stage,
Clear Bake, Key all, Purge orphans), Play Every Frame sync, Frame Drop caching,
a bounded disk frame cache, multi-stage duplication handling, selective stage
solves, generator and live stages, one-way Stage In-only feeds, nested instance
baking, strictly unique stage keys with per-stage conflict resolution, the
persisted loopback endpoint component, the non-metre units matrix, and the
reference performance matrix with its recorded Linux baseline.

**Verified at release:** 57 core tests, 279 Blender headless tests plus the
offline gate, the C# fixture/attribute/link/component/stage suites, the
installed extension round trip, and the reference benchmark matrix (all budgets
met on 2026-11-14).

**Known gaps carried forward:**

| Gap | Why it blocks 1.0 |
|---|---|
| Instance-heavy Stage In latency (Blender's half measured at 10.5 ms; the live Rhino Stage In/solve split is still unmeasured) | The remaining latency is not located; 1.0 needs the live split. |
| No native Windows Rhino acceptance run | Wine is a development environment, not the target user platform. |
| No GH component migration | Old documents keep old parameter/output lists and never gain newer capabilities. |
| No chain-level playback/bake proof | `Bake stage` freezes one stage; a multi-stage render is not proved Grasshopper-free. |
| Release verification is manual and single-machine | A 1.0 needs a reproducible, multi-platform gate. |

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

- Everything already shipped in v0.6.0.
- The hardening work in §5.
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
- Grasshopper 2 / Rhino 9 support; see [`plan-gh2.md`](plan-gh2.md).
- A stable frame-cache disk format; the cache is disposable by design.

These are scope decisions, not hidden gaps. If 1.0 changes scope, this section
changes first.

---

## 5. Workstreams

### A. Performance and responsiveness

**Problem.** The Blender half is measured: a 13× 100k-triangle definition costs
10.5 ms to read and encode and 2.4 ms to re-apply, and the reference matrix
passes its budgets. The live Rhino Stage In/solve split is still unmeasured, so
the remaining 300–800 ms report has not been located.

**Work**

- The benchmark harness is in place: `scripts/bench` builds the reference scenes
  and enforces the budgets; its 2026-11-14 Linux baseline is in
  `benchmarks/results/`.
- Re-measure with the current `.gha` against a live Rhino; record Stage In,
  engine pass, transport, solve and apply times separately.
- Fix the dominant cause. The existing hypotheses are, in order: duplicate
  solves (fixed), definition rebuilding on every solve, and the Grasshopper
  definition itself.
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

**Exit:** the reference matrix is in the repository, the Blender-side instance
case is inside budget, and the 1.0 performance report is reproducible.

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
- Decide whether the frame cache remains disposable across minor versions or
  gains a versioned sidecar. Prefer disposable; it is not user data.

**Exit:** projects saved by the last two public releases open and work, old GH
components gain new outputs, and the compatibility policy is enforced by tests
or a checked handshake.

### D. Playback and bake at chain scale

**Problem.** `Bake stage` freezes exactly one stage. The tests cover one stage.
A multi-stage chain with a live upstream stage has no proof that a render is
Grasshopper-free, and Play Every Frame is tested with one stage.

**Work**

- Add a multi-stage Play Every Frame test: each stage in stack order, one request
  per frame, no skipped frames, cached replay and disconnect behavior.
- Add a chain-level bake acceptance. Either:
  - add **Bake all**/freeze-upstream behavior so the whole chain is independent
    of Grasshopper, or
  - prove the native Bake node shields the full prefix and document the required
    bake order.
- Test: bake over a scene range, render with Grasshopper closed, Clear Bake
  resumes live output, and a packed bake survives save/reload.
- Ensure a failed or timed-out stage cannot leave a bake half-written or the
  stage frozen without a visible error.

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
- **Scale:** test large geometry, many stacks, many parameters and cache
  eviction under sustained playback.
- **Failure injection:** malformed frames, dropped connections, stale responses,
  port conflicts, multiple Blender instances, and a missing/deleted node group.
- **Threading:** keep the GH-object UI-thread invariant, including the `hello`
  units read.

**Exit:** the robustness matrix passes; no known silent data-loss path remains.

### F. Release engineering and distribution

**Work**

- CI: run layer A and layer B (Blender headless against `fake_gh`) on pull
  requests. Run the C# console suite under wine or on a Windows runner. Add a
  self-hosted Rhino smoke job for the live half.
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
| **0.6** | Performance harness and instance fix; units matrix; plugin endpoint; bake undo safety | Instance case inside budget; units and endpoint tests pass; bake is safe to undo or clearly irreversible |
| **0.7** | Compatibility/migration; GH `Read()`; chain-level playback/bake | Last two releases upgrade cleanly; a multi-stage chain renders offline from a bake |
| **0.8** | Windows platform acceptance; CI layers; tag-triggered builds; distribution routes | Windows matrix passes; CI builds and validates artifacts from a tag |
| **0.9** | Release candidate; soak; freeze protocol v2 and file formats; full acceptance on supported platforms | No open release-blocking defect; all gates below pass |
| **1.0** | Release | Artifacts, notes and hashes published; support matrix signed off |

---

## 7. 1.0 acceptance gates

The old A1–A10 remain the functional core. Add the release gates that the
0.5.x line did not cover.

- **A1 Keyframes.** Keyframed parameters drive the GH output per frame; warm
  frames replay from the cache.
- **A2 Drivers.** A driver on a custom property moves the GH output live.
- **A3 Chain.** A `GH → GN → GH` chain evaluates without manual steps.
- **A4 Stack edits.** Reorder, disable and enable create no datablocks; restored
  states are cache hits.
- **A5 Curves.** Representable curves round-trip exactly; approximations are
  counted and warned.
- **A6 SubD.** Cage and edge creases round-trip; the semi-sharp limitation is
  visible.
- **A7 Persistence.** Undo/redo, save/reload, and upgrades from 0.4.1 and 0.5.0
  preserve stages, parameters, animation and helpers.
- **A8 GH lifecycle.** Closed, reopened and restarted Grasshopper reconnects with
  no manual step and keeps the last good output while disconnected.
- **A9 Performance.** The reference matrix meets the budgets in §5A, including
  the instance-heavy case.
- **A10 Duplicates.** Duplicated stacks and stages keep independent keys,
  caches, groups, helpers and animation; a saved or undone shared key is a
  visible, resolvable conflict instead of a silent split.
- **A11 Platform.** Windows 11 + Rhino 8 + Blender 5.3 passes the full matrix;
  the Linux/wine development matrix remains green.
- **A12 Chain playback/bake.** Multi-stage Play Every Frame works, and a
  multi-stage bake renders with Grasshopper closed and restores live behavior
  with Clear Bake.
- **A13 Compatibility.** Old GH components gain new outputs through migration;
  an incompatible add-on/plugin pair reports a clear mismatch; protocol v2
  remains interoperable.
- **A14 Units and Breps.** Non-metre Rhino documents round-trip correctly; the
  lossy Brep path is covered and warned.
- **A15 Release.** Tagged artifacts are built from a clean tree, validated,
  hashed, installable by the supported routes, and reproducible in CI.

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
| Baking a chain still leaves upstream stages live | Add Bake all/freeze-upstream or prove the Bake node shields the prefix; test with Grasshopper closed. |
| CI cannot run Rhino | Use a self-hosted Windows/Rhino runner; keep the headless Blender and C# suites in public CI. |
| Scope creep delays 1.0 | Freeze scope at 0.8; new features wait for 1.1 or later. |

**Decisions to confirm**

1. Is macOS Rhino 8 in the 1.0 support matrix, or explicitly out?
2. Is extensions.blender.org a 1.0 requirement or a post-1.0 distribution step?
3. Is Yak packaging for the `.gha` required for 1.0?
4. Does 1.0 ship Rhino 8/GH1 only, with GH2 in a later release?
5. Is the frame-cache format allowed to remain disposable across 1.0, or does it
   need a versioned sidecar?

---

## 10. References

- [`protocol.md`](protocol.md) — wire protocol v2.
- [`decisions/`](decisions/) — architecture decisions, including the frame
  cache and native bake.
- [`plan-gh2.md`](plan-gh2.md) — separate Grasshopper 2 / Rhino 9 target.
- [`extensions-compliance.md`](extensions-compliance.md) — current platform
  compliance record.
- [`../README.md`](../README.md) — product overview and supported requirements.
- [`../CHANGELOG.md`](../CHANGELOG.md) — release history and known limitations.
