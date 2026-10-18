# Cyclic NURBS losslessness — investigation and fix (2026-10-18)

Prompted by the live report: "the conversion from stage `stage-0a1764` curves
to rhino is not perfectly lossless ... (it's a simple nurbs curve)". This file
carries the wire evidence, the root causes, what changed, and how it was
verified. The live FiraTower pass was owner-verified the same day. The work
list stays in `docs/backlog.md`; the roadmap in `docs/plan.md`.

---

## The report, and what the wire actually carries

Live scene: `FiraTower.blend` (open in Blender), stack id `40d29f71857ec345`,
stage `stage-0a1764` (a Stage between the "Initial Nurbs Circle Array" and
"Initial Rotation+deform" node groups). The stage panel shows
`1 curve(s) approximated as cubic Bezier spans`, and the cached response
(`~/.local/share/gh-stack/cache/40d29f71857ec345/stage-0a1764/…`) carries a
`curve_approximated` warning, one definition, 13 instances.

Reconstructing the prefix exactly (clone the stack object, drop its modifiers
from the stage on, read the `GeometrySet`) shows what Blender sends:

- the prefix is 13 instances of one definition;
- the definition holds exactly one curve: **type NURBS, order 6,
  `knots_mode` 0 (uniform/normal), `cyclic` 1, 6 control points, all
  weights 1** — a uniform cyclic B-spline of degree 5;
- the control polygon is a regular hexagon at radius ≈ 7.67 (the "Initial
  Nurbs Circle Array" output; the evaluated curve is likewise a closed curve
  of radius 5.8156..5.8164 m — close to, but not exactly, a circle).

A cached frame is a response; the request above was reconstructed from the
saved blend (its prefix sections are deterministic).

## Root cause 1 — order/degree off-by-one: the curve built in Rhino was wrong

`Conversion.BuildNurbs` (`grasshopper/src/core/Conversion.cs`):

```csharp
NurbsCurve periodic = NurbsCurve.Create(true, ord, ps);
```

RhinoCommon's XML docs for `NurbsCurve.Create(bool periodic, int degree,
…)`: *"degree: (>=1) degree=order-1"*. `ord` here is Blender's **order**
(degree + 1), so every cyclic non-rational NURBS was built **one degree too
high**. For the reported curve: a degree-6 periodic curve instead of degree 5.

Measured against Blender's own evaluation of the same spline (samples of a
legacy NURBS spline at `resolution_u = 256`), with `rhino3dm` 8.35 — the same
openNURBS the Rhino 8 install wraps:

| wire curve (uniform, cyclic) | max deviation of what the old code built |
|---|---|
| order 6, 6 pts (the FiraTower case) | **262.5 mm** |
| order 3, 8 pts (octagon) | **122.0 mm** |

This is not an approximation error: the curve in Rhino was a different curve
(the reported one sat at radius ≈ 5.554 m instead of ≈ 5.816 m).

## Root cause 2 — why the wrong curve came home as a cubic fit

`SectionsFromCurves` accepted the exact span decomposition only when

```csharp
decomposed && degree >= 1 && degree <= 5
    && piece.Points.Count == spans * degree + 1 && WeightsPositive(piece)
```

The degree-6 curve root cause 1 built failed the `degree <= 5` guard, so the
encoder took the `BezierCurve.CreateCubicBeziers` fallback — that was the
warning in the stage panel. The returned definition curve (73 cubic control
points) fits the *wrong* curve, so Blender's geometry was 262.5 mm off as
well. `MakePiecewiseBezier` itself was never the blocker: on the correct
degree-5 periodic curve it succeeds (order 6, 31 CVs, 6 spans,
`31 == 6*5+1`), so the span path carries a fixed cyclic curve exactly.

## Root cause 3 — cyclic *rational* NURBS silently became non-periodic

`BuildNurbs` only entered the periodic branch when `cyclic && !rational`. A
cyclic NURBS with weights fell through to the manual `new NurbsCurve(3,
rational, ord, n)` construction — uniform but **not periodic**. Same class of
silent shape error, rarer because weights on cyclic curves are uncommon.

## What changed (`grasshopper/src/core/Conversion.cs`)

- **P0 — the degree.** `NurbsCurve.Create(true, ord - 1, ps)`; the null
  fall-through stays for curves too small for the periodic construction.
- **P1 — cyclic rational.** The periodic curve is created non-rational, made
  rational, and every stored control point is re-set with its wrapping weight:
  `Points.SetPoint(i, ps[i % n], ws[i % n])`, which writes the kernel's
  homogeneous `(x*w, y*w, z*w, w)`. (`SetWeight` was tried first and is
  **wrong**: `ON_NurbsCurve_SetWeight` writes the homogeneous W alone, moving
  each point to `location/w`; an independent check measured that as 0.681 m
  off.) openNURBS stores the first `degree` control points repeated at the
  end, so the weights repeat with them. Falls back to the old construction
  only when `Create` returns null.
- **P2 — a direct NURBS route on the encode side.** `TryBlenderNurbs` accepts
  a NURBS whose knots are one of Blender's modes — order 2..6, positive
  weights, distinct knots with a constant step, repeated knots only as clamped
  ends — and `AppendNurbs` sends it as **one wire NURBS** with its order,
  knot mode, cyclic flag and Euclidean+weight control points. A periodic curve
  sends its unique polygon (`Points.Count - Degree` CVs, with the duplicated
  tail verified); clamped curves send endpoint mode, uniform open curves
  normal mode. Everything else keeps the old routes: `MakePiecewiseBezier`
  spans (a rational circle's interior knot multiplicities land here), then
  the cubic fit and its warning.

No protocol change and no Blender add-on change: the wire already carried
exactly this form.

## Verification

- **Kernel measurements (rhino3dm 8.35, the same openNURBS).** The FiraTower
  curve decodes to 262.5 mm off with the old degree call and to the
  tessellation floor (≤ 13 µm, i.e. float32 wire + sampling noise) with
  `Create(true, 5, …)`; the octagon 122.0 mm → 5.9 µm. A periodic curve's
  unique polygon + cyclic flag round-trips to the same curve.
- **Independent port validation** (a fresh session ported `BuildNurbs` /
  `TryBlenderNurbs` / `AppendNurbs` to Python against rhino3dm and ran the
  matrix): decode 1.16e-14 m on the FiraTower wire (old call 0.262 m as the
  control), 6.3e-15 m on the octagon; encode accepts the periodic (plain and
  rational), clamped and unclamped forms with the expected wire fields and
  rejects the circle (interior multiplicities), non-uniform knots, a
  mismatched periodic tail, order 7, and `unique < order`; round trips stay
  within float32 wire noise (≤ 8.1e-8 m). **It also caught the `SetWeight`
  trap in P1** (0.681 m), which was fixed and re-validated against an
  independent homogeneous construction: cyclic-rational decode 1.23e-14 m,
  round trip 8.1e-8 m (float32 wire) / 1.2e-14 m (float64), and the updated
  S6 reference catches a relapse to `SetWeight` at 0.681 m. Final verdict:
  PASS. Report: `/tmp/opencode/subagent_nurbs/REPORT.md` (scratch; the durable
  check is the S6 `Cyclic` block).
- **C# builds**: `build-core.sh`, `build.sh`, `build-gha.sh` and the spike
  build all compile.
- **Suites**: `scripts/test-core` 53 OK · `scripts/test-gha` 12 fixtures +
  attribute store + link round trip PASS · `scripts/test-blender` 142 OK
  (three new layer B tests pin the cyclic wire form, cyclic rational
  weights, and the evaluated-component wire form) · `scripts/test-extension`
  PASS.
- **Live checks prepared** in `spikes/s6/S6Checks.cs` (`Cyclic` block plus
  updated span expectations): decode deviation of the exact stage-0a1764
  wire, encode as one cyclic NURBS with `approximated == 0` and no warnings,
  wire round trip, cyclic rational weights, clamped and unclamped direct
  forms, and the circle still on the span path. Run them through the spike
  `.gha` (`spikes/s6/driver.py`) in a Rhino that has the rebuilt spike
  deployed.
- **Owner-verified live (2026-10-18).** After the rebuild + deploy and a
  Rhino restart, the FiraTower scene passes: `stage-0a1764` shows no warning
  and the returned definition curve is a single cyclic NURBS matching the
  "Initial Nurbs Circle Array" output.

A scripted second Rhino under wine was tried for an automated live run and
does not execute `/runscript` in this setup (no output, no script start); the
in-Rhino spike stays the live path, like `spikes/s6/probe.sh`'s standalone
probe dead end.

## Known limitation (unchanged, noted here)

Blender's `use_endpoint_u` *combined with* `use_cyclic_u` is a distinct
configuration whose knot vector (`calculate_knots` with `head = order-1`,
`tail = 2*order-1` and an offset) has no standard counterpart; the periodic
construction is used for it, as before. The combinations the UI presents as
meaningful — cyclic, endpoint, and open-uniform — are exact.
