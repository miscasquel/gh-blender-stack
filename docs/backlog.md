# Backlog

The live list. Resolved work lives in the commits and the CHANGELOG; the
measured evidence behind the bigger items lives in `polish-plan.md`, which is a
record of one review rather than a plan. If something is not here, it is either
done or was judged not worth doing — it is not missing.

**Next session:** A10 is done (unreleased): a duplicated stack is split onto its
own stage key, cache namespace, node group and helpers, and its parameter values,
keyframes and drivers move with the key. The live-sync rebuild also landed: the
depsgraph is the source of truth for the prefix, every Edit Mode object syncs, a
wedge cannot outlive a tick, a slow request supersedes itself at one second, and
the plugin pins a solve's request id (rebuilt and deployed; **restart Rhino** to
load it). What remains is the live acceptance pass on both batches
(`docs/live-sync-report-2026-10-19.md` §3, Phase 7), then the S6 display half
and S7 in the GUI, then acceptance A1–A10 for 0.3.0. The portal upload is its
own batch (compliance Phase 1, G7 pre-ask included) and is independent.

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

Live concerns recorded 2026-10-19; no cause measured yet. Transport is ruled
out (S3: 30 ms round trip at 100k triangles, 10 ms C# mesh build). **For 0.3.0**
both are accepted as known issues; the release notes should name them, and the
live pass should still re-measure the instance case with the rebuilt `.gha`
loaded.

- **Stage In latency with instances (300–800 ms).** One geometry instanced 13×:
  the Stage In update in Rhino took ~300 ms typically and 800 ms at peak, while
  a plain cube is instant. Turning preview off on every Grasshopper component
  lowers the delay. Re-testing the same file later halved the worst case
  (800 → 300 ms), unexplained: the session did not record whether the rebuilt
  `.gha` (Phase 4 rid pinning) had been loaded — Rhino scans Libraries only on
  load. Hypotheses, in order:
  1. **Duplicate solves.** `RetryStaleSolves` re-schedules every 250 ms while a
     slot is `waiting` (`GhLink.cs:726`), so any solve over 250 ms runs
     two-deep. Fix: mark the solve in flight (delegate start → `SolutionEnd`)
     and skip retries for it; keep the budget for dropped delegates.
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
- **Choppy render viewport.** Reported in the same session, not investigated.
  Baseline with the add-on disabled vs enabled; watch `stats["tags"]`,
  `stats["passes"]` and the Diagnostics trigger count while the viewport idles;
  check whether the per-tick depsgraph drain (`_drain_depsgraph`) plus helper
  writes keep re-tagging the scene. ADR 0008 promises a quiet idle, so
  choppiness means something is still evaluating per tick.

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
- **Curve attributes are read-only** (the legacy Curve helper cannot store them;
  also in `docs/plan.md` §6, README and code). Revisit only if curves matter — it
  means changing the curve helper representation.
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

- **Build scripts share a strain of boilerplate.** `grasshopper/build/*.sh`
  repeat the same wine/`csc.exe` setup; one shared helper would keep the wine
  trap handling (match whichever wineserver owns the prefix) in one place.
- **The vendored wheel.** `blender/gh_stack/vendor/websockets` is now
  development- and test-only (the shipped zip carries the official wheel from
  PyPI, digest-pinned in `scripts/build-extension`). The Linux-only speedups
  build stays out of the repo on purpose; both facts are easy to re-derive from
  the scripts.
