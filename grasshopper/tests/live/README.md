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
