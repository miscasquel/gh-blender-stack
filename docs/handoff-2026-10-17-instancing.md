# Handoff — rebuild the Blender-side instance node graph (2026-10-17)

**Read this first if you are working on the Blender side of instances.** The
GH → Blender direction works live (the owner: "from the grasshopper side it
works quite nicely"); this file is the agreed redesign of how the *Blender*
node graph represents instances and nesting. The general state of the batch is
in `docs/handoff-2026-10-17.md`; the live work list in `docs/backlog.md`.

**Status (later session): implemented.** The chain is now
`Join(own, Instance-on-Points(child chain) per distinct child)` fed by
per-definition placement clouds (`GHS …nest<i>`, position/rotation/scale plus a
`child` index attribute), so a placement is one instance and the graph grows
with definitions and distinct placements, never with depth or row count.
`GROUP_FORMAT` is 3 and old groups repair themselves on the next same-hash
frame. The repeat zone was dropped deliberately: Blender 5.2 cannot evaluate
recursive node groups (a self-referencing group exposes no sockets), and the
wire's DAG order is not guaranteed, so a runtime walk would need a topological
remap and dynamic per-iteration selection for no gain over shared static
chains. The target design below stays as the record of the ask; the live
verification (§Verification, and the batch handoff §3) is still open.

---

## Why (the owner's critique, verbatim)

> The blender geometry modifier transforms a mesh and then joins, this results
> in a single mesh object that then you instance on certain points with a
> rotation and scale. This is the wrong approach as it results in only one
> instance and scales poorly. I'd use a nodegroup that instances and rotates
> the geometry to the desired location and nest it using a repeat zone.

What that maps to today (`blender/gh_stack/blender/nodes.py`, `ensure_group`):
each definition's prototype chain is

```
proto(i) = Join( own objects,  for each nested row r:  Transform( proto(child_r), T_r ) )
```

The `Transform` node **bakes a transformed copy** of the child's evaluated
geometry into the parent's prototype for *every* nested row. A parent with N
nested placements of a heavy child materializes N copies; every top-level
instance of the parent then carries that blob. It draws correctly (verified),
but it is not a true instance hierarchy and scales with copies × child size.

## Target design

- **Instance, don't bake.** A nested placement becomes an instance: per
  definition, a placement point cloud — position/rotation/scale attributes
  built from `idef.<i>.inst.*`, exactly like the top-level row clouds —
  feeding `Instance on Points` whose `Instance` input is the child's prototype
  chain. Child geometry stays a reference; one instance per placement.
- **Nesting via a repeat zone.** The owner's prescribed structure: one node
  group that walks the definition levels with a **Repeat Zone** (or an
  equivalent recursion — check what Blender 5.2 allows for recursive node
  groups; the repeat zone carries the accumulated geometry across levels), so
  depth is handled at runtime and the graph does not grow per level.
- Keep the top-level row-cloud instancing as it is (correct: one instance per
  row, rotation/scale through named attributes).

## Constraints (hard-won; do not relearn)

- Explicit `rotation` / `scale` **Named Attribute** links are required; the
  implicit attribute fallback does not fire for datablock-backed attributes
  (probed 2026-10-16).
- Prototype collections are hidden **by flag**, never view-layer-excluded:
  excluded collections are not evaluated, and Collection Info reads them
  empty. Instances that reference a *datablock* are not drawn while that
  datablock is viewport-hidden — prototype geometry is read through
  `Object Info` (copies), which is exactly why the current chains draw, and
  why datablocks in *another scene* resolve too: the plumbing (helpers,
  definitions, row clouds) is owned by the dedicated `GH Stage` scene since
  the decluttering pass, verified through evaluation, a Cycles render and
  save/reload.
- The group carries `gh_stack_format` and `gh_stack_nested` stamps;
  `_group_matches` requires both, and both same-hash paths call `ensure_group`
  so a stale group repairs itself. **Bump `nodes.GROUP_FORMAT` whenever the
  layout changes.**
- The wire is unchanged: nested rows are `idef.<i>.inst.ref_def` +
  `idef.<i>.inst.transforms`; `engine._nested_rows_of(sections)` is the one
  parser both paths use. The GH side needs no change at all.

## Where the pieces live

- `nodes.py` — `ensure_group` (the recursion, `prototype_join`),
  `_definitions`, `_reachable`, `_group_matches`, `_nested_signature`,
  `GROUP_FORMAT`.
- `engine.py` — `_apply` → `_apply_instance_rows` (row clouds, legacy-empty
  cleanup, missing-target warning), `_nested_rows_of`, `_rows_of`.
- `helpers.py` — definition collections and per-kind prototype objects,
  `ensure_instance_cloud`, `matrix_of`, `decomposes_cleanly`.
- Tests — `blender/tests/headless/test_instances.py`: `test_apply_draws_nested_rows`,
  `test_apply_draws_two_levels_of_nesting`,
  `test_the_group_rebuilds_when_the_nested_structure_changes`,
  `test_a_group_without_the_format_stamp_is_rebuilt`,
  `test_an_orphan_definition_is_not_expected_in_the_group`.

## Verification

- Layer B: keep the realize-based nested tests (single and two levels) and the
  Cycles pixel check; add a **scaling** case — a child placed at many nested
  rows (e.g. 100 and 1000) with many top-level rows — asserting the evaluated
  geometry keeps **one instance per placement** (not copies) and stays inside
  a time budget the current Transform+Join cannot meet.
- Live: the owner's cube-with-a-sphere scene (Blender → GH) and the GH nested
  block (GH → Blender: a Block Definition whose `Objects` take a Block
  Instance, wired to Stage Out) both draw parent and child.

## Working notes

- Build/deploy, restart rules and the wine trap (`WINE=/usr/bin/wine` when the
  session owns the wineserver) are in `docs/handoff-2026-10-17.md` §4.
- Extension rebuild/install: `scripts/build-extension` +
  `scripts/install-extension`; **Blender must restart** to load them.
- Layer B port for the instance tests: 9151. To realize instances headlessly,
  append a temporary Realize Instances modifier after the stage; to check
  *drawing*, render (Cycles CPU works headless) and count pixels.
