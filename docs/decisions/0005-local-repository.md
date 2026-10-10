# 0005 — Repository on the local machine

**Status:** Accepted, 2026-10-10 (D5)

## Context

The project folder in a shared network folder (`a shared project folder`) is a CIFS mount to `a home server`, not local storage. Measured: a 4 KB write takes about 2 ms there and effectively nothing locally. CIFS is mounted with `nounix`, so Unix permissions, including the executable bit that build and deploy scripts need, cannot be preserved. The mount also disappears when the network does.

All tooling (Blender 5.2.2, the Rhino 8 wine prefix, `csc.exe`) is on this machine. None of it is on `a home server`.

## Decision

The repository lives at `~/workspace/gh-blender-stack` on the local machine. All building, testing, and editing happen there. The a shared network folder folder keeps the plan as a mirror of `docs/plan.md`.

## Consequences

- The working loop has no network hops.
- A remote for backup can be added later (`a home server` or a hosted service). The repository is not configured with one.
- The old `~/workspace/an earlier link project` runtime mirror and the `a home server` repository are not used by this project.
