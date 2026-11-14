# Live Rhino checks

These checks need a running Rhino process, because RhinoCommon's `Mesh` and
`MeshNgon` types load native `rhcommon_c.dll`. The wine console harness
(`scripts/test-gha`) deliberately runs without Rhino, so it cannot exercise
them.

Run a check through Rhino's C# script editor, or through the Rhino MCP
`run_csharp` tool, with the Stageloom `.gha` loaded.

## `NgonCheck.cs`

Builds a triangulated mesh with a pentagon at the end, calls the product
`Conversion.SectionsFromMesh`, and expects:

```text
PASS: n-gon encoding (pentagon boundary exact, holey n-gon flattened)
```

The first case is the regression check for the live 2026-11-07 failure: the old
code passed `ngon[k]` (boundary vertices) to `GetNgonBoundary` instead of
`ngon.FaceIndexList()` (fan faces), so Stage Out threw
`NullReferenceException` on every n-gon. It now checks the exact pentagon
boundary indices (`101` indices, the last five are the pentagon) and that the
three fan triangles did not leak into the output beside it (`32` triangles).

The second case builds a square ring of four quads registered as one n-gon with
an inner boundary. The wire format carries one polygon per `face_counts` entry
and cannot carry holes, so the encoder must send the four underlying quads with
an `ngon_holes` warning. It also checks that the no-warning overload refuses
the same mesh with a readable `FrameException` instead of silently flattening
it.

The check must be run after rebuilding and redeploying `Stageloom.gha`; Rhino
loads plugins only at startup.

## `UnitsCheck.cs`

The non-metre half of the units matrix. It exercises `Conversion` with explicit
document scales (millimetres, centimetres, metres, inches, feet): a mesh, a
polyline and a point list must return to the same document coordinates, and the
same sections read at scale 1 must be metres.

It also exercises the block row path (`Instances.ReadRows`): a wire placement
of 5 metres in a millimetre document must become a translation of 5000. This is
the part Blender cannot test, because Blender has no document unit system.

**Result, 2026-11-14:** the first run found `ReadRows` returning 5 for a
millimetre scale — the placement translation had never been converted. The row
reader and writer now scale the translation only (the linear part is
dimensionless), and the full matrix passes. The shared `TransformFromWire` /
`TransformToWire` helpers are covered headlessly in `ComponentTests`; `Emit`
uses the writer helper with the same document scale.

Expected on a correct build:

```text
PASS: units matrix (mm/cm/m/in/ft, mesh/curve/points, block placements)
```
