# Backlog

The live list. Resolved work lives in the commits and the CHANGELOG; the
measured evidence behind the bigger items lives in `polish-plan.md`, which is a
record of one review rather than a plan. If something is not here, it is either
done or was judged not worth doing — it is not missing.

**Active handoff:** [`docs/handoff-stageloom-0.4.0-2026-10-22.md`](handoff-stageloom-0.4.0-2026-10-22.md)
— the Stageloom 0.4.0 rebrand and GitHub release. The release is published; the
extensions.blender.org upload is deferred.

**Next session:** v0.3.1 is published (2026-10-21); v0.4.0 is published (2026-10-29) under the active handoff (rebrand plus GitHub release; platform upload deferred). The post-0.3.0 review
batches are in it: the Stage In/GhLink batch (`C-LINK-1`, `C-COMP-2`,
`C-LINK-3`, `C-LINK-4`, `C-ATT-2`; review §16), the engine batch (`B-ENG-4`,
`B-ENG-2`, `B-ENG-3`, `B-ENG-1`; review §17), the prefix batch (review §18) and
the Phase 0 registration diagnostics. On FiraTower a dirty Cube pass is now
~46 ms (was ~115 ms) because the three stage prefixes share one depsgraph
update; idle passes are ~1.5 ms. The residual cost is the single update, the
cleanup flush and geometry read/apply. Phase 7 live acceptance passed
2026-10-19; the S6 display half, S7 and acceptance A1–A10 passed 2026-10-20
(`docs/live-sync-report-2026-10-19.md` §5). The viewport choppiness was the
unconditional per-tick depsgraph drain; gating it on work fixed it. A10 is done
and covered headless, and the icon pass is in. The next work is the
instance-latency re-measure and the old-component migration. The platform
upload is deferred.

Each item says what it is, where, and why it matters. Effort is not estimated:
the last three batches each looked like an afternoon and were not.

---

## Reliability

### Grasshopper plugin

- **Old components keep their old parameter list** (2026-10-18). A Stage In
  saved before the `Attributes` output existed now opens and solves, because the
  plugin skips writes to outputs the loaded component does not have (the
  document keeps its older output list). A proper fix is a `Read()` migration
  that registers missing parameters on load and so lets an old document gain the
  new output; not worth the untested surface while pre-attribute documents
  matter only to this machine's owner.

---

## Performance

Live concern recorded 2026-10-19; no cause measured yet. Transport is ruled
out (S3: 30 ms round trip at 100k triangles, 10 ms C# mesh build). **In 0.3.1**
the instance latency is still accepted as a known issue (the release notes name
it); the investigation starts by re-measuring it with the rebuilt `.gha`
loaded. The viewport choppiness from the same session was the unconditional idle
depsgraph drain; gating it on work (`f69f5fc`) fixed it.

- **Stage In latency with instances (300–800 ms).** One geometry instanced 13×:
  the Stage In update in Rhino took ~300 ms typically and 800 ms at peak, while
  a plain cube is instant. Turning preview off on every Grasshopper component
  lowers the delay. Re-testing the same file later halved the worst case
  (800 → 300 ms), unexplained: the session did not record whether the rebuilt
  `.gha` (Phase 4 rid pinning) had been loaded — Rhino scans Libraries only on
  load. Hypotheses, in order:
  1. **Duplicate solves.** `RetryStaleSolves` re-scheduled every 250 ms while a
     slot was `waiting` (`GhLink.cs:726`), so any solve over 250 ms ran
     two-deep. The solve is now marked in flight from the delegate start to
     `SolutionEnd` and retries skip it (`C-LINK-4`, review §16); the latency
     re-measure is still pending.
  2. **Definitions rebuilt every solve.** `Instances.Build` converts each
     `idef.<i>` group to Rhino geometry and constructs a
     `ModelInstanceDefinition` on every solve, even when the content did not
     change. Fix: cache by content hash (the unnamed-definition hash already
     exists) and invalidate on change; transforms-only edits then rebuild only
     the row list.
  3. **The definition's solve dominates.** Then it is the Grasshopper document,
     not the bridge; measure before changing code.
  Measure first with the env-gated timestamps in the report §6. Preview off is
  a usable mitigation in the meantime (a per-component toggle, or
  `IsPreviewCapable => false` on Stage In/Out).
- **A multi-stage dirty pass paid one depsgraph update per stage (fixed
  2026-10-21).** FiraTower's three Cube stages each called
  `evaluated_depsgraph_get()` on their own clone, ~17 ms per stage plus cleanup,
  for ~115 ms per dirty pass. The prefixes are now cloned together and read
  after one update; the same pass is ~46 ms and the idle pass ~1.5 ms (review
  §18). The single evaluation and the cleanup flush are the residual cost.
- **Viewport stutter (parked 2026-10-21).** Panning hitched intermittently
  after reloads, but Phase 0 found no duplicate generation: one enabled
  install, one handler set, one registered timer, no dirty/hot state and two
  stack objects. A 60 s trace while smooth showed 34–50 idle passes/s and zero
  prefix clones, applies or sends. The hitch also appeared with both installs
  disabled while Rhino/Wine and a browser GPU process kept the host busy, and
  the extension and development engine code were identical apart from the
  Phase 0 diagnostics and the later load-timer fallback fix. Assumed
  external/system-side; re-open only if it
  reproduces with both installs disabled and the host quiet. Reproduction and
  the diagnostics are in `docs/handoff-viewport-stutter-2026-10-21.md`.

---

## Design constraints

Settled by evidence; the code that rests on them must not drift from them.

- **Instances are graph-side named blocks.** Probed live 2026-10-16
  (`spikes/results/s9.txt` §D): a `ModelInstanceDefinition` from Attributes is
  valid only with a Name set; named, it previews and bakes with no document
  write, and Rhino materializes the blocks at bake, reusing them across bakes.
  Stage In builds them children-first; Stage Out reads a definition through its
  paired objects, and reaches a nested block through the `GH_InstanceReference`
  goo cast (`CastTo<InstanceReferenceGeometry>` refuses a block-instance
  ModelObject). Do not reintroduce the document write at solve time.
- **Curve attributes are one-way (Blender → GH) for now** (the legacy Curve
  helper cannot store them on the return path; also in `docs/plan.md` §6, README
  and code). Revisit only if returning curves matters — it means changing the
  curve helper representation.
- **`sharp_face`/`UVMap` stay in the generic attribute path.** Measured
  2026-10-17 at 100k triangles: UVMap adds 1.61 MB per frame and ~0.9 ms
  (tick 9.1 ms of the 16 ms budget), `sharp_face` ~50 KB and ~0.02 ms; a
  triangulated 100k leaves ~2 ms headroom. Both are real user data; keep them
  (`docs/plan.md` §19).
- **The prefix memo tolerates last-bit float noise in attribute payloads only**
  (`engine._same_prefix`, rtol 1e-5); an exact hash there reintroduces the
  churn. The CHANGELOG explains why; the code is the reference.

---

## Polish

- **The vendored wheel.** `blender/stageloom/vendor/websockets` is now
  development- and test-only (the shipped zip carries the official wheel from
  PyPI, digest-pinned in `scripts/build-extension`). The Linux-only speedups
  build stays out of the repo on purpose; both facts are easy to re-derive from
  the scripts.

---

## Review records

- `docs/repo-review-2026-10-20.md` — a code-quality, leanness and
  efficiency review of the v0.3.0 tree, written read-only. It carries the
  per-module scores, the `B-*`/`C-*`/`P-*` finding IDs and a prioritised
  remediation table. The Tier 1, Phase 2A, Phase 2B, Phase 3A and Phase 3B
  batches have since been applied (review §§13–17); the items that were already
  known here are cross-referenced in the review rather than duplicated.
