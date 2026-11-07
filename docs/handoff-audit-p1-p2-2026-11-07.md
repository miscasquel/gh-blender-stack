# Handoff: P1/P2 audit — verified findings and next work

**Date:** 2026-11-07
**Tree:** `main` at `16161cd` (clean at audit start), three commits ahead of
`origin/main`
**Audited range:** `2dcef0b..16161cd`

- `7559733` — `Plugin: fix n-gon encoding in Stage Out`
- `8205919` — `Add-on/Plugin: harden multi-stage response handling`
- `16161cd` — `Add-on/Plugin: harden stage addressing and prefix replay`

**Status:** audit complete and follow-up fixes applied. H1/M1–M4 are fixed in
`2b9c426`; M5/M6 and L1–L6 are fixed in `ce61b43`, `fa37571`, `3b33e5d`,
`0d2d71d`, `f8b3635`, and `1ca977d`.

The audit was run independently four ways and the material findings were
re-verified directly on this tree. The original n-gon crash and the
empty-`rid` cache-poisoning path are genuinely fixed.

**Fix batches:** `2b9c426` keeps legacy stages with ambiguous modifier names,
moves the prefix generation on every apply, gates retry/error handling on real
requests, and clears pending state when a stage is disabled. The later batch
covers downstream-only prefix-batch invalidation, Stage In status ownership,
the `SetSlotStatus` compatibility alias, surface rejection, file-load state,
empty block definitions, modifier-order UI/operator rows, and holey n-gon
fallback. The new `.gha` is built and deployed; Rhino must be restarted to load
it.

---

## 1. Verification actually run

Current tree, current deployed plugin:

```text
scripts/test-core          57 tests pass
headless Blender suite     215 tests pass
offline gate               PASS on port 59131
scripts/test-gha           12 fixtures + attribute store + link + stage guard pass
grasshopper/build/build-gha.sh  PASS
uvx ruff check             All checks passed
```

The offline gate was run on port 59131 because a live Blender session already
holds the default 59130. That is an environment conflict, not a code failure.

Live Rhino check against the deployed P2 `.gha`:

```text
n-gon mesh: counts=33 pentagons=1 indices=101
```

The live `StageSlot` reflection also confirms the P1/P2 fields are loaded:
`InStatus`, `InMessage`, `OutStatus`, `OutMessage`.

**P2 deployment hash:**
`e2401c1d5485cf63c2c627591a420053931c3efdaf267d00d1b60fe50dce0dd5`

---

## 2. Verified working

- **n-gon Stage Out fix.** `MeshNgon.FaceIndexList()` is used for both
  `GetNgonBoundary` and the fan-face mask. The live Rhino check passes.
- **Empty-`rid` response gate.** Empty/non-string rids, invalid statuses, and
  `ok` frames without `out_hash` are rejected before cache or apply.
- **Frozen Pull.** Pull on a frozen stage still applies because the answer
  carries the stage’s current rid; freeze clears the rid.
- **Modifier-order evaluation.** `_ordered_stages()` processes stages in the
  modifier stack order, with record order as a tie-breaker.
- **Apply invalidates the prefix batch.** The mesh/curve/point/attribute paths
  now clear the batch and bump the epoch on a non-empty clear.
- **Modifier uid ambiguity.** A repeated uid is resolved by node-group name
  when possible, otherwise `modifier_of()` refuses to guess.
- **Null SubD/definition guards.** Null SubDs are filtered before the control
  net encoder; null definition objects are skipped.

---

## 3. Findings

### H1 — HIGH — Legacy ambiguous modifier names delete stage records

**Regression from `16161cd`.**

`nodes.py:426-445` returns `None` when more than one unattached modifier matches
`Stageloom · <label>[.nnn]`. `lifecycle.py:283-305` treats that `None` as “the
modifier was deleted” and removes the stage record, helpers, definition objects,
parameter properties, and node group.

Reproduced on this tree:

```text
legacy-names before: 2 ['Stageloom · Stage', 'Stageloom · Stage.001']
legacy-names after:  0
```

Scenario: a pre-`modifier_uid` `.blend` (uid `0`) with two stages both labelled
`Stage`, both modifiers unattached because their node groups were deleted. This
is a supported repair case, not a missing modifier. Old code kept the records;
new code deletes both.

Fix direction: keep the stage whenever any name candidate exists, even if
ambiguous; mark it error/unbound instead of deleting. Add a test that runs
`_drop_removed_stages()` on this exact shape, not just the helper.

### M1 — MEDIUM — Prefix memo still serves pre-apply geometry for instance-only changes

**Incomplete fix from `16161cd`.**

`apply.py:127-131` clears `_prefix_batch`, but `_prefix_sections()` checks
`_prefix_memo` first (`request.py:283-296`). `_apply_instance_rows()` and
`_write_rows_cloud()` (`apply.py:260-362`) do not call `_bump_id`. A response
that changes only instance rows or nested placements leaves the prefix epoch
unchanged, so a downstream stage reuses its old memo even though the upstream
helper changed.

Verified with an empty-definition instance frame:

```text
prefix epoch: 0 -> 0
prefix before: ['inst.ref_def', 'inst.transforms'] sha256:3b73435b...
prefix after (memo): same sections, sha256:3b73435b...
prefix fresh: same names, sha256:d44bd8ee...
transform before: [1, 0, 0, 0]
transform memo:   [1, 0, 0, 0]
transform fresh:  [1, 0, 0, 10]
```

Fix direction: bump the object/global prefix generation (or clear the object’s
memo) on every real apply, including instance-only and definition-only writes.
Add a downstream-prefix test with a block-only upstream response.

### M2 — MEDIUM — No-request Stage Out leaves the slot `waiting`, causing retry solves

**Regression from `8205919`.**

The no-request branch (`Components.cs:378-384`) changes only the local status;
it never calls `SetOutStatus`. `StageSlot.Status` defaults to `"waiting"`
(`GhLink.cs:86`), and `RetryStaleSolves()` retries every waiting slot every
250 ms up to eight times (`GhLink.cs:905-938`).

Verified with a focused C# probe:

```text
retry 0 -> 1 attempts=1 status=waiting
```

A document-wide solve for another stage can therefore trigger repeated
full-document solves for a Stage Out that has never been requested. Fix:
gate retries on a non-empty `Rid`/explicit request, or set the aggregate status
to an idle state in the no-request branch. Add a component-level test.

### M3 — MEDIUM — Error responses evict good cache entries for stale/unsolicited rids

**New behavior from `8205919`, exposed by pre-existing stale-rid sends.**

`messages.py:206-216` forgets the cached frame for **any** non-empty error rid.
The C# side can answer a document-wide solve with the slot’s last rid
(`GhLink.cs:328-332`, `746-755`). If stage B is idle with cached frame R and its
Stage Out fails transiently during A’s solve, Blender evicts B’s good frame R
and marks B error.

Verified:

```text
cached before error: True
cached after stale error: False
new pending after stale error: {}
state after stale error: error
```

The same branch also clears pending and ignores `frozen`/rid equality, so a
late error for R1 can clear a newer pending R2. Fix: act/evict only for the
rid actually awaited, preserve frozen state, and do not clear a different
current pending entry.

### M4 — MEDIUM — Disabling a stage with an in-flight request never clears pending

**Pre-existing, but exposed by the new response gate.**

`request.py:58-61` returns on disabled before the pending/timeout block. If a
request was sent and GH never answers, `_pending` survives forever; the stall
nudge keeps the object hot and dirty. The new `messages.py:202-204` check only
helps when an answer actually arrives.

Verified:

```text
disabled outcome: idle
disabled pending remains: {'sha256:disabled-pending': ...}
```

Fix: forget pending/sent state when a stage becomes disabled, as freeze does.

### M5 — MEDIUM — Batch invalidation is O(N²) on cached multi-stage playback

**New performance risk from `16161cd`.**

Every apply clears the global `_prefix_batch`. `_process_stage` computes the
prefix before the cache lookup, so with N stages applying cached frames in one
pass, the next stage rebuilds all N prefixes again. That is N batch builds × N
prefixes on the cached-playback path.

Prefix costs measured in this environment were roughly 6.5 ms at 10k triangles
and 56 ms at 100k. A 3-stage 100k cached pass can exceed the plan’s 16 ms tick
budget. Consider invalidating only downstream batch entries, or building the
batch lazily after each apply.

### M6 — MEDIUM — The multi-stage surface guard is unreachable and the claim is unsupported

`lifecycle.py:231-241` refuses a surface stack with more than one stage, but
Blender 5.2 surface objects cannot take a Geometry Nodes modifier:
`obj.modifiers.new(type="NODES")` returns `None`, and `add_stage` fails at
`nodes.py:350-355` before the guard can run. This is pre-existing, but the new
guard has no real test and the changelog’s “refused with a clear error” claim is
not reachable through normal UI. The guard also marks disabled/frozen stages
error and does not clear pending/cache.

### L1 — LOW — Stage In status split is only half-wired

`Components.cs:112-113` reads `slot.InStatus`, but Stage In never writes it on
success or failure; it only sets a local output value. With no frame, a protocol
error message can remain while the status reports `waiting`. `OutMessage` is
also never exposed. The new `RunSlotStatus` test only proves `SetOutStatus` does
not clobber `InStatus`.

### L2 — LOW — n-gons with holes/slits can be silently corrupted

`Conversion.cs:200-212` uses `GetNgonBoundary`, which returns the outer boundary
of a face set. The wire format carries one polygon per `face_counts` entry, so
holes/slits are lost. `MeshNgonList.NgonHasHoles(int)` exists in RhinoCommon;
the encoder does not check it. Before this fix such meshes failed loudly; now
they can succeed with changed topology and no warning. Use
`BoundaryVertexIndexList()` plus a hole check and warn or reject. Not reproduced
live; this is a static API risk.

### L3 — LOW — Public helper rename can break saved script components

`GhLink.SetSlotStatus` was removed in favor of `SetOutStatus`
(`GhLink.cs:348`). Normal component/parameter serialization is unchanged, but
saved GH C#/VB script components calling the old public static method will fail
to compile. Consider a compatibility alias.

### L4 — LOW — Stage order is fixed for evaluation, not the UI/operator indices

`_ordered_stages()` sorts only the processing loop. The N-panel and operators
still use `record.stages` creation order (`ui.py:65,191`; `ops.py:17-23`).
After dragging a modifier, panel rows and Pull/Freeze indices can be out of
stack order even though evaluation is correct. Mirror the order into the
record/UI or document the limitation.

### L5 — LOW — Frozen/offline state has two unverified gaps

- `_on_load()` clears `rid` only for frozen stages; a non-frozen saved rid can
  still match a response queued at load before `_verify`. Consider clearing or
  namespacing `rid` for all stages on load.
- `_pull_permits` is not cleared on load, so a Pull armed before a save can
  send one request after reopening a frozen stage.

### L6 — LOW — Null definition objects can become empty blocks

`Instances.cs:500-505` adds the definition-table entry before iterating;
`CollectObject` now silently returns for null objects (`Instances.cs:571-576`).
If every object is null, the response still carries the definition and an
instance row, so Blender creates an empty block instead of a skipped/warned
instance. Add a warning or skip the definition.

---

## 4. Test gaps confirmed

- No test runs `_drop_removed_stages()` on two same-label pre-uid stages with
  lost node groups; the helper test alone does not catch the H1 data loss.
- No downstream-prefix test for an instance/definition-only apply; the current
  apply test checks only `_prefix_batch`/epoch.
- No C# component-level test for the no-request Stage Out gate, the status
  split, or `RequestSolve` rid pinning; `RunSlotStatus` only tests fields.
- No test for an unsolicited/stale-rid error arriving while a good cached frame
  exists.
- No test for disabling a stage with a pending request.
- The frozen test (`test_multistage_link.py:185-203`) sets `stage.rid=""` itself
  and passes on old code; it does not exercise `freeze()` or `_on_load()`.
- The empty-rid test uses a non-empty `stage.rid`, so it does not cover the
  dangerous `stage.rid=""` apply path.
- The stage-order test stubs `_process_stage`; it checks call order, not actual
  downstream geometry.
- `NgonCheck.cs` is manual and not invoked by any script/CI; it asserts only
  face count and number of pentagons, not exact boundary indices.
- `scripts/test-gha` does not build `Components.cs`, `GooDispatch.cs`, or
  `Instances.cs`; only `build-gha.sh` compiles the full plugin. A compile error
  in the component/instance code would pass the test script.
- No runtime test constructs a null SubD or null definition object.

---

## 5. Recommended order

1. Fix H1 and add the legacy-ambiguity deletion test.
2. Fix M1 (instance-only prefix memo) and add the downstream-prefix test.
3. Fix M2 (no-request retry loop) and M3 (error-rid gating), with component and
   cache tests.
4. Fix M4 (disabled pending) and M5 (batch invalidation cost).
5. Decide M6: remove the unreachable surface claim or implement surface stage
   support/test.
6. Address L1 and L2, then the remaining low findings.
7. Add the missing tests and, if practical, make `test-gha` compile the full
   plugin.

---

## 6. Environment / live state

- The final `.gha` is deployed to Grasshopper’s Libraries
  (`25a9e0637e020110c96256ac3991dfae7db6abb7bae0e5c889fca387f7515dd1`); Rhino
  must be restarted to load it.
- Blender is running and holds the default add-on port 59130. The full offline
  gate was verified on port 59131.
- P1/P2 and fix-batch Python changes still require a Blender add-on reload or
  restart to be active in the user’s live session.
- The audited code commits, the fix batches, and this handoff are local; `main`
  is ahead of `origin/main` and has not been pushed.
- The Rhino-side n-gon check remains a manual live test; it is not part of the
  wine test suite because RhinoCommon’s mesh/ngon types need native RhinoCore.
