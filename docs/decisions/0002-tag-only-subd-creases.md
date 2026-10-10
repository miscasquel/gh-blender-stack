# 0002 — Tag-only SubD creases

**Status:** Accepted, 2026-10-10 (D2)

## Context

Rhino SubD edges carry a tag (`SubDEdgeTag`: `Crease`, `Smooth`, `SmoothX`, `Unset`). RhinoCommon's public API, as shipped with Rhino 8 in this environment, does not expose semi-sharp crease values or vertex creases. The rhino3dm binary contains sharpness symbols, so a fallback through 3dm bytes exists, but it would reintroduce rhino3dm as a Blender dependency.

## Decision

- GH → Blender: `Crease` tags become crease value 1.0. `Smooth`, `SmoothX`, and `Unset` become 0.0. Vertex creases are not transferred.
- Blender → GH: edges with crease value ≥ 0.5 get the `Crease` tag. Others get `Smooth`. Fractional values are rounded, and the count is reported as a warning.
- Every stage with SubD output reports that creases are transferred as tags only.
- No rhino3dm fallback. This record is the reason it is not used.

## Consequences

- Semi-sharp creases and vertex creases are lost. This is documented and warned, not hidden.
- Spike S4 must confirm that `SubDEdge.Tag` can be set. If it cannot, Blender → GH drops creases and warns.
- If RhinoCommon later exposes richer crease data, a new record supersedes this one.
