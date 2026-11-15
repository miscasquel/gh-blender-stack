# 2026-11-15 — Live Rhino chain bake and upstream freeze

First live acceptance of the terminal-stage bake with the W2 add-on code and a
real Grasshopper definition. Raw report:
[`2026-11-15-live-chain-bake.json`](2026-11-15-live-chain-bake.json).

Environment: Blender 5.3.0 with the working-tree add-on in a throwaway 5.3
config, `Stageloom.gha` 0.6.0 in Rhino 8 under wine, Linux 7.3.4 (CachyOS).
The Grasshopper graph was three Stage In/Out passthrough pairs, one per stage.

## Scene

```
Holder:  Stage A -> native Bevel -> Geometry Nodes "ReadsSource" -> Stage B
Source:  Stage C
```

Stage B was selected. Its upstream set was:

- `Holder/A` (same object, before B)
- `Source/C` (cross-object, read by the Geometry Nodes modifier before B)

The `ReadsSource` modifier joined Source's evaluated geometry into Holder's
prefix, so the dependency was real and the walker found both upstream stages.

## Results

| Check | Result |
|---|---|
| Bake B over frames 1–3 | **FINISHED**, 3 ready, 0 failures |
| B after bake | `baked=True`, `frozen=True`, token `f43e252010333476` |
| A and C after bake | frozen, `frozen_by_bake` = B's token |
| Offline evaluation with the server stopped | frames 1–3 stable; sends **9 → 9** |
| Offline render | 3 Cycles PNGs written (`offline-0001..0003.png`) |
| Save/reload of the packed bake | B stayed baked/frozen; A/C stayed frozen with the token |
| Reloaded offline evaluation | identical geometry hashes; no sends |
| Clear Bake after reload | B/A/C unfrozen, token and markers cleared |
| Live resume | all three stages `ok`; sends **9 → 14** |

The Holder evaluated to 32 vertices with the same hash on every offline frame;
Source stayed at 8 vertices. The baked B output and the frozen A/C helpers were
stable with no Grasshopper client.

## What this proves

- The real Bake stage operator bakes the selected stage, then freezes the
  selected stage and its transitive upstream set, including a cross-object
  upstream stage.
- The packed bake survives a save/reload with the freeze bookkeeping intact.
- The chain evaluates and renders with no client connection.
- Clear Bake unfreezes exactly the stages that bake froze and the live chain
  requests again.

## Caveats

- The chain is static upstream: A and C stay at one value; only B's bake covers
  the three frames. That is the intended static-upstream limitation.
- The Cycles render is 64×64, one sample, 3 frames; it proves the render path,
  not image quality.
- The live run used the working-tree add-on and the deployed 0.6.0 `.gha`; no
  protocol or `.gha` change is involved in W2.
