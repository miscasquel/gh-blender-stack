# 0005 — Repository on the local disk

**Status:** Accepted, 2026-10-10 (D5)

## Context

The project originally lived in a shared network folder mounted over CIFS,
not on local storage. A 4 KB write took milliseconds there and effectively
nothing locally; CIFS preserved no Unix permissions, so the executable bit the
build and deploy scripts need was lost; and the mount disappeared when the
network did.

All tooling (Blender 5.2.2, the Rhino 8 wine prefix, `csc.exe`) is on one
machine.

## Decision

The repository lives on the local disk (`~/workspace/gh-blender-stack`).
All building, testing, and editing happen there.

## Consequences

- The working loop has no network hops.
- A remote for backup and publication was added later: GitHub (ADR 0007).
- An earlier Portal-based runtime mirror is not used by this project.
