# 0003 — Approximate and warn for curves that cannot be exact

**Status:** Accepted, 2026-10-10 (D3)

## Context

Blender's NURBS splines store weights, order, and a knot mode (`normal`, `endpoint`, `bezier`). They do not store arbitrary knot vectors. Rhino NURBS curves can have interior knots that Blender cannot express. Rational spans have no Blender equivalent in Bezier form.

Verified: a NURBS spline's weights, order, and knots mode survive a Geometry Nodes object reference. Rhino provides `NurbsCurve.MakePiecewiseBezier` and `ConvertSpanToBezier`.

## Decision

- Curves that Blender's knot modes can express are transferred exactly. That
  set is uniform-open, clamped-uniform (endpoint) and uniform-periodic knots,
  any order 2..6, weights included; they travel NURBS to NURBS, as one curve.
- Other curves are converted to Bezier spans when the span split can carry
  them (every span a single rational Bezier). Rational spans become
  non-rational approximations only when neither route applies.
- The engine measures the deviation by sampling. A warning is emitted when it exceeds the threshold (default 0.1 mm, configurable). Every approximated curve is counted in the stage status regardless of deviation.
- Approximation never blocks a transfer. The warning is the signal.

## Consequences

- No silent geometry loss. A user always sees what was approximated and by how much.
- The threshold and the sampling density are preferences, so they can be tuned without changing the protocol.
