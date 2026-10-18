# Backlog

The live list. Resolved work lives in the commits and the CHANGELOG; the
measured evidence behind the bigger items lives in `polish-plan.md`, which is a
record of one review rather than a plan. If something is not here, it is either
done or was judged not worth doing — it is not missing.

**Next session:** **W3/W4 landed on `main`** (unreleased): the operator set is
Add Stage, Push, Pull, Freeze, Key all and Purge orphans; the stage lifecycle is
native — the modifier's viewport/render/edit-mode toggles are the enable switch,
X removes the stage, drag reorders it — and the `frozen`/`unbound` states,
load-time plumbing repair and the parameter-orphan list are in. What remains for
0.3.0: the S6 display half and S7 in the GUI, then the acceptance pass A1–A10.
The portal upload is its own batch (compliance Phase 1, G7 pre-ask included) and
is independent.

Each item says what it is, where, and why it matters. Effort is not estimated:
the last three batches each looked like an afternoon and were not.

---

## Reliability

### Blender engine — smaller

- **`NestedGroupPrefixTests.test_02` flaked once** (2026-10-17, during the 0.2.0
  release run): the edit inside the nested group did not reach `fake_gh` within
  the 10 s pump. It passed in isolation and in every later full run, and its
  failure message now carries the fake's report, the stage state and both rids,
  so the next occurrence is diagnosable. Remove this item once a reason is found
  or a release cycle passes without it.
- **Duplicated objects share one node group** (verified 2026-10-18 by an
  adversarial probe of the W3/W4 batch): object duplication copies the stage
  record, the modifier and its `persistent_uid` verbatim, and the node group
  datablock is keyed only by the stage key — so both objects' stages resolve to
  the same group, and every engine pass rebuilds it for whichever object ran
  last. Both objects then draw the last-processed object's helpers. Plan §5
  already prescribes the fix (detect shared groups each tick, give the duplicate
  its own group, key and helpers); this is acceptance item A10.

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
