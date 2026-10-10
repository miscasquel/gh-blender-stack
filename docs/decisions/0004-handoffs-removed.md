# 0004 — Handoffs removed; this plan replaces them

**Status:** Accepted, 2026-10-10 (D4)

## Context

Two handoff documents specified the earlier native track: a `.3dm` file-based Grasshopper plugin and a Blender addon with a file protocol. The track was re-planned, and the handoffs became inaccurate. They were rough and not verified against the environment.

## Decision

- Both handoffs were deleted from the project folder and from `a home server`'s `docs/` (commit `4121236` in that repository).
- `a home server`'s README and the retired track's notes were repointed at the new plan. The notes remain the failure-mode record.
- `docs/plan.md` in this repository is the authoritative plan.

## Consequences

- The handoffs remain in git history on `a home server` (commit `6af0f21`) if ever needed.
- Facts from the handoffs that still hold were carried into the plan's evidence log.
