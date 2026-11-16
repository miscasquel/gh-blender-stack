# 0015 — Bake triggers and freezes downstream dependents

**Status:** Accepted, 2026-11-16. Amends
[0014](0014-bake-freezes-upstream.md) for the downstream rule: the selected
stage is still the only bake, but its consumers no longer stay live.

## Context

ADR 0014 baked one stage and froze its upstream closure, so the baked stage had
a stable input. Downstream consumers stayed live. After a bake, the next stage
in the chain was therefore still asking Grasshopper for a fresh answer, and an
offline render or a saved file could show the baked stage followed by a stale or
disconnected downstream output. Baking every stage into a second chain-wide
operation would add bake data, a second operator and a second thing to clear.
The owner decision (D4) is narrower: bake the selected stage, make the chain
consistent around it, and let the user bake each downstream stage only when it
needs per-frame offline data.

## Decision

- **Bake only the selected stage.** Its native Geometry Nodes Bake node is the
  only native animation bake this action writes.
- **Compute the full downstream closure.** The reverse of the upstream walker:
  later stages on the same object, and stages on other objects or collections
  whose prefix reads this object, transitively, cycle-guarded and deduplicated
  by `(object, stage)`. The graph is built by reversing
  `stage_dependencies` over every stack object. It is O(S²) in the number of
  stages, which is acceptable because it runs once per bake, not per pass.
- **Trigger and freeze the dependents in dependency order.** After the target
  bake succeeds, each downstream stage is asked exactly once, at the scene's
  current frame, and frozen only when that answer lands. The wait drives the
  engine timer; it is not the playback gate. No dependent is frozen without an
  answer.
- **A dependent is not baked.** It keeps the single frame it evaluated. If it
  animates and needs offline playback, the user bakes it in turn; that bake
  clears the old freeze marker and owns the stage from then on.
- **A dependent that is already baked is left alone.** Its own bake is the
  answer; triggering it would replace the held output. A disabled stage has no
  live output and is skipped. A one-way stage (no Stage Out) cannot answer and
  is skipped with a visible warning.
- **Freeze bookkeeping stays per bake.** The target carries `bake_token`; every
  stage this bake froze, upstream or downstream, carries that token in
  `frozen_by_bake`. A stage the user had frozen before the bake keeps its own
  freeze and gets no marker; a pre-frozen dependent is refreshed with one Pull
  and stays frozen. A stage frozen by another bake is not adopted.
- **Failure rolls back the whole action.** If a dependent disconnects, errors or
  times out, the target's partial native bake is deleted, only the stages this
  bake froze are unfrozen, the target's prior freeze state is restored, and the
  failing stage and frame are reported. There is no half-frozen chain.
- **Rebake releases this stage's previous token first**, thaws the target and
  builds the new set. It does not adopt another stage's markers.
- **Clear Bake is scoped to the selected stage.** It deletes only that stage's
  native bake and releases only the markers carrying its token, upstream or
  downstream. A stage with its own bake keeps it and stays frozen; an unrelated
  token and a user's manual freeze are untouched. There is no chain-wide Clear
  Bake.

## Consequences

- A chain is consistent after one Bake action: the selected stage has per-frame
  data and its consumers hold the matching one-frame output without asking
  Grasshopper.
- Rendering an animated downstream stage for a range still needs its own bake.
  The frozen downstream output is a single frame, not an animation.
- A one-way downstream stage receives the baked input on its next delivery but
  cannot be frozen to an answer; the warning says so.
- Cross-object dependencies are included by reading the same prefix references
  the upstream walker uses. A missed reference would leave a live consumer, so
  the walk is conservative; the test suite covers the object, collection and
  native-modifier cases.
- Save/reload preserves the target bake, its token and every marker. The freeze
  markers are ordinary stage properties, not a new registry or cache.
