# 0014 — Bake stage freezes its upstream chain

**Status:** Accepted, 2026-11-15. Live Rhino acceptance recorded in
`benchmarks/results/2026-11-15-live-chain-bake.md`.

## Context

`Bake stage` baked one stage's native Geometry Nodes Bake node and froze that
stage. A chain could still change underneath the bake: an upstream stage, or a
stage on another object read through Object Info/Collection Info, remained live,
so the baked inputs no longer matched the scene. The alternative considered was
**Bake all**, which would add another operator and bake every stage in the
chain. That is more UI and more bake data than the chain needs.

## Decision

- **Keep the existing Bake stage operator.** There is no Bake chain button and
  no new operator. The user selects any stage and presses Bake stage. To bake a
  dependency tree, select its last stage.
- **Bake the selected stage, then freeze its upstream closure.** The operator
  computes the stages the selected stage depends on, bakes the selected stage
  with those upstream stages live (so an animated upstream chain steps per
  frame), and freezes the selected stage plus every upstream stage not already
  frozen.
- **The dependency walker is upstream only.** It covers earlier stages on the
  same object, Object Info/Collection Info references in the prefix, native
  modifier object/collection properties, Geometry Nodes modifier inputs, and
  transitive references. Downstream consumers stay live.
- **Freeze bookkeeping is per bake.** The baked stage carries a `bake_token`;
  every upstream stage the bake froze carries the same token in
  `frozen_by_bake`. Clear Bake unfreezes exactly those stages. A stage the user
  had frozen before the bake has no marker and stays frozen.
- **No partial bake leaves a frozen chain.** A failed or incomplete bake
  (including `playback_failures > 0`) freezes no upstream stage, leaves the
  selected stage unfrozen and visible with its error, clears the token, and
  best-effort deletes partial native data.
- **A split stage drops both markers.** A runtime duplicate or user key
  resolution cannot be unfrozen by the original's Clear Bake and owns no stale
  token.
- No new panel row, dialog or freeze registry. The Bake stage tooltip and
  report mention the upstream freeze; the existing per-stage rows show the
  frozen state.

## Consequences

- Baking the last stage of a tree makes the whole upstream chain static in one
  action. Downstream stages remain live.
- The frozen upstream objects are static after the bake; rendering an animated
  upstream object for a range still needs its own bake or cache. The selected
  stage's native bake covers its own range.
- A failed rebake may lose the previous bake because the native data is
  replaced; the operator reports the failure and leaves no stale freeze token.
- Save/reload preserves the bake, token and markers. The packed bake is the
  durable render source.
- The dependency walker is conservative: it may include an upstream stage
  reached through a disabled or unused modifier, but it must not miss a real
  input. It runs only when Bake stage is invoked.
