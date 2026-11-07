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
PASS: 33 output faces, one pentagon
```

This is the regression check for the live 2026-11-07 failure: the old code
passed `ngon[k]` (boundary vertices) to `GetNgonBoundary` instead of
`ngon.FaceIndexList()` (fan faces), so Stage Out threw
`NullReferenceException` on every n-gon.

The check must be run after rebuilding and redeploying `Stageloom.gha`; Rhino
loads plugins only at startup.
