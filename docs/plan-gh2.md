# Stageloom for Grasshopper 2 — Implementation Plan

**Status:** draft for implementation.
**Date:** 2026-11-11.
**Target:** Rhino 9 / Grasshopper 2 (GH2), `Stageloom.rhp`, `net8.0`.
**Relationship to the existing plan:** this document covers the GH2 target only.
`docs/plan.md` remains the authoritative plan for the existing Grasshopper 1
(GH1) plugin and the Blender add-on.

---

## 0. Recommendation

Build a **new, native GH2 plugin** as a sibling target to the existing GH1
plugin. Do not port the GH1 integration layer.

- Keep `grasshopper/` (GH1, Rhino 8) unchanged and supported.
- Add `grasshopper2/` (GH2, Rhino 9), a `net8.0` plugin built with the .NET SDK.
- Keep the Blender add-on and protocol v2 unchanged for parity. The GH2 plugin
  speaks protocol v2 exactly.
- Use GH2's native component, data, document, and threading model instead of
  recreating GH1's workarounds.
- Preserve the wire format and golden fixtures. Add optional v2 fields; reserve
  protocol v3 for genuinely breaking capabilities such as multi-document
  routing.
- Treat "complete" as: every feature the GH1 plugin has today, plus the losses
  GH2 can remove, with no silent degradation.

Recommended defaults for the open decisions are in §13.

---

## 1. Verified environment

Everything below was verified on this machine during planning.

| Item | Evidence |
|---|---|
| Rhino 9 | `9.0.26300.9401`, Commercial licence, runs under wine |
| Grasshopper 2 | `2.0 Wip`, hosted on .NET `10.0.2` |
| GH2 assemblies | `.../Rhino 9/Plug-ins/Grasshopper2/net8.0/` (`Grasshopper2.dll`, `GrasshopperIO.dll`, 13 `G2*.rhp` component libraries) |
| GH2 XML docs | `.../Grasshopper2/net8.0/Grasshopper2.xml` (7.6 MB, 23,908 documented members) |
| Rhino 9 .NET assemblies | net8.0 under `.../Rhino 9/System/netcore/`; net48 under `.../System/` |
| .NET SDK | 8.0.428 at `/tmp/opencode/dotnet` (verified with `--list-sdks`) |
| NuGet | `Grasshopper2 9.0.26300.9401`, `RhinoCommon 9.0.26300.9401` |
| GH2 log | `%LOCALAPPDATA%\Grasshopper2\Diagnostics\*.ghlog` confirms plugin scoping and solves |
| Headless load/solve | A trivial plugin loaded via `PluginServer.ScopePlugin` → `LoadPlugin` → `ObjectProxies.FindByType` → `Emit` → `Document.NewInertDocument` → `StartWait`, and solved `2 + 3 = 5` |
| Build path | Linux .NET SDK built a `net8.0` `.rhp` against both the installed DLLs and the official NuGet package |

Key API facts established:

- A GH2 plugin is one public, parameterless-constructible subclass of
  `Grasshopper2.Framework.Plugin`, packaged as an `.rhp`.
- A component derives from `Grasshopper2.Components.Component`, carries a
  stable `[IoId]`, has a `(IReader)` constructor, and overrides
  `AddInputs`, `AddOutputs`, and `Process(IDataAccess)`.
- GH2 has no `IGH_Goo`. Data are `IPear`/`Pear<T>` (value + `MetaData`),
  `ITwig`/`Twig<T>`, `ITree`/`Tree<T>`. Geometry items are raw RhinoCommon
  objects.
- GH2 solves components serially; iterations inside a component are parallel by
  default (`ThreadingState.MultiThreaded`).
- GH2 has no async compute and no task-returning `Process`. Network I/O must be
  handled by plugin-owned background workers, with
  `Document.Solution.DelayedExpire(this)` to request a re-solve.
- GH2 has no native block/instance data type. Blocks require plugin-defined
  item types and `ITypeAssistant`s, or an explicit flattening fallback.
- Rhino 9 adds `SubDEdgeSharpness` (per-end 0–4), unlike Rhino 8. GH2 can be
  less lossy than GH1 if the wire carries sharpness values.
- GH2 has no plugin unload hook. Sockets and workers must be torn down on
  `RhinoApp.Closing` and on document close.

---

## 2. Scope

### 2.1 In scope

- Stage In, Stage Out, and Param components, GH2-native.
- Meshes with n-gons and vertex colours.
- SubD control cages with crease tags; Rhino 9 semi-sharp values where the wire
  can carry them.
- Curves: poly, Bezier, NURBS, with exact transfer or explicit approximation
  warnings.
- Point clouds, with colours and radii preserved where representable.
- Breps: native GH2 input; meshed on export with a warning.
- Named attributes: all Blender types and domains; mesh, SubD, curve, point,
  and instance attributes.
- Blocks/instances: nested definitions and rows, using GH2 custom item types.
- Attribute Get, Attribute Set, and the Point Cloud Points utility.
- GH2-side persistence, undo, copy/paste, save/reload.
- GH1 → GH2 document migration rules (optional phase).
- Protocol v2 compatibility, including the frame cache, keyframes, drivers, and
  all existing Blender behaviour.

### 2.2 Out of scope for this version

- GH1 interop inside GH2. The `Grasshopper1` interop layer is for document
  migration only, not for product components.
- `net48` builds. Rhino 9 hosts GH2 on .NET 10; a `net8.0` assembly is the
  supported target.
- Multiple GH documents claiming stage keys at the same time. This needs
  protocol v3; v2 allows one link owner and reports `duplicate_stage`.
- Materials, layers, named object identity, and render materials.
- Native B-rep in Blender. GH2→Blender remains a mesh conversion.
- Windows-native Rhino as a tested target. The design must allow it, but testing
  remains wine-first.
- A second Blender-side geometry kernel.

### 2.3 Complete-and-performant parity matrix

| Feature | GH1 today | GH2 target |
|---|---|---|
| Stage In / Stage Out / Param | Yes | Yes, native GH2 components |
| Mesh, n-gons, vertex colours | Yes | Yes, bulk conversion, validated |
| SubD cage + crease tags | Yes | Yes; Rhino 9 semi-sharp where representable |
| Curves: poly / Bezier / NURBS | Exact or warned approximation | Same, via `CurveBroker`, no GH1 special cases |
| Points | Yes | Yes, plus colours/radii |
| Breps | Meshed, lossy, warned | Native GH2 input; meshed on export, warned |
| Attributes, all types/domains | Yes | Yes; curve attributes outbound fixed; deterministic order; typed validation |
| Blocks / nested instances | GH1-specific | Custom GH2 types + display/bake |
| Parameter keyframes, drivers, cache | Blender side | Unchanged |
| Undo, save/reload, copy/paste | Yes | Proper `IReader`/`CustomValues` round-trip |
| Multiple GH documents | Not supported | Deferred to protocol v3 |
| GH1 → GH2 migration | N/A | Optional migration rules |
| Materials / layers / object identity | No | Explicit non-goal |
| Native B-rep in Blender | No | Non-goal |

---

## 3. Module evaluations

### 3.1 Protocol core

Files: `grasshopper/src/core/Frame.cs`, `Json.cs`, `ParamValues.cs`.

**Verdict: reuse the algorithms; rewrite the implementation for .NET 8.**

The files are BCL-only and already platform-neutral. Keep the wire format,
canonical JSON semantics, section ordering, hashing rules, and size caps. The
GH2 implementation should be a fresh `net8.0` project rather than a shared
source compromise with the GH1 `.gha` build.

Required improvements:

- `ReadOnlyMemory<byte>` / `Span<byte>` instead of `byte[]` copies.
- `BinaryPrimitives` for explicit little-endian access.
- `IncrementalHash` / `SHA256.HashData` instead of per-call hashing objects.
- A section name → index map instead of linear `Find`.
- Strict UTF-8 decoding; reject duplicate section names; validate `shape`,
  dtype, and element counts.
- A frame-level geometry hash that includes attribute descriptors and
  definition tables, as protocol §6.1 requires.
- Typed JSON accessors; no `Json.GetString` casts that throw
  `InvalidCastException`.
- A typed parameter value model with finite checks and Python-compatible
  casts; no broad `catch (Exception)` that turns programming errors into
  neutral values.

**Critical risk: float formatting.** `Json.FormatDouble` uses `"R"` under the
Mono runtime. .NET 8's shortest-round-trip formatter can differ from Python's
`repr` for the same value, and the golden fixtures are byte-sensitive. P1 must
add a float-format conformance corpus covering:

- integers and `-0.0`,
- very small and very large magnitudes,
- exponent boundaries,
- subnormals,
- values around `f32` precision limits,
- NaN and Infinity rejection.

Either match Python's formatting exactly or define a protocol-owned canonical
format. Do not let this be discovered in production.

### 3.2 Geometry conversion

File: `grasshopper/src/core/Conversion.cs`.

**Verdict: rewrite the mesh and curve hot paths; keep the algorithms and
warning vocabulary.**

The file is RhinoCommon-only and contains valuable geometry knowledge, but it is
not a suitable GH2 performance core.

Strengths to keep:

- Section names and warning codes.
- Metres on the wire, document units at the boundary.
- Exact curve routes: uniform/clamped/periodic NURBS, piecewise rational
  Beziers, and the approximation fallback.
- SubD crease tag semantics.
- Row-major instance transforms.

Problems to fix:

- Mesh decode adds vertices and faces one at a time. Rhino 9 has bulk
  `Vertices.AddVertices`, `Faces.AddFaces`, and `VertexColors.SetColors`.
  Measured locally: 100k vertices 5 ms bulk vs 12 ms loop; 100k faces 10 ms.
- Mesh encode reorders faces when ordinary faces and n-gons are mixed. This
  breaks FACE/CORNER attribute alignment. This is the most serious correctness
  bug found.
- No cardinality validation: short sections throw `IndexOutOfRangeException` or
  silently truncate.
- `SectionsFromGeometry` bakes Stage Out policy into "core": joining meshes,
  meshing Breps, joining SubDs. That policy belongs in the GH2 adapter.
- Point colours and radii are lost; mesh creases are not emitted; curve
  attributes are asymmetric.
- Silent fallbacks: zero weights become 1, malformed Bezier nets become NURBS,
  orders clamp. Replace with typed `ConversionResult<T>` and warnings.

Target design: per-kind codecs returning typed results and warnings:

- `MeshCodec`
- `CurveCodec`
- `PointCodec`
- `SubDCodec`
- `BrepCodec`
- `InstanceCodec`

Each validator checks lengths, index bounds, dtypes, finite values, and
topology before construction.

### 3.3 Attributes

File: `grasshopper/src/core/Attributes.cs`.

**Verdict: redesign around GH2's metadata model; keep `UserDictionary` as the
per-element store.**

GH2 has no per-element metadata. `MetaData` is one bundle per item, with scalar
values plus `byte[]`. Per-element arrays still belong in
`GeometryBase.UserDictionary`.

Use both layers:

- `GeometryBase.UserDictionary["Stageloom.Attributes"]` for canonical
  per-element arrays, persistence, bake, and geometry that came from Rhino.
- `Pear.Meta["Stageloom.Attributes"]` as a compact `byte[]`/JSON sidecar so
  attributes ride through GH2 components. GH2 operations do not know about
  `UserDictionary`.

Required improvements:

- Typed descriptors and arrays; no stringly typed `object` dictionaries.
- Validate cardinality against the geometry's element count and domain.
- Validate dtype against the attribute type.
- Deterministic attribute order; do not depend on `ArchivableDictionary.Keys`
  order.
- Make curve attribute collection symmetric with curve attachment.
- Support point colours/radii and mesh creases explicitly.
- Return immutable/owned values; do not return a section's `byte[]` by
  reference.
- Attribute Set must duplicate geometry before mutating; GH2 inputs are shared
  and immutable by convention.

One experiment is required: confirm that a `byte[]` meta value survives
`MetaData` → save/load → `MetaDataExtensions.Extract`, and that GH2 geometry
operations inherit the sidecar or lose it predictably.

### 3.4 GH1 integration layer

Files: `grasshopper/src/gh/*.cs`.

**Verdict: rewrite. This is the part that cannot be ported.**

GH1-specific and unavailable in a `net8.0` GH2 plugin:

- `GH_Component`, `IGH_DataAccess`, `GH_ParamAccess`, `IGH_Goo`.
- `GH_Mesh`, `GH_Curve`, `GH_PointCloud`, `GH_InstanceReference`.
- `GH_Document.ScheduleSolution`, `ExpireSolution`, `SolutionEnd`,
  `OnPingDocument`.
- `GH_IO` `Write`/`Read`.
- `GH_AssemblyPriority`, `ComponentServer`, ribbon.
- `Grasshopper.Rhinoceros.Model.ModelInstanceDefinition` and `ModelObject`;
  these are net48-only and cannot be referenced from GH2.

Design problems that must not survive:

- A process-wide static `GhLink` singleton; one document, one link, one slot
  registry.
- Rid pinning is document-wide: a solve for stage A pins stage B and can produce
  cross-stage answers.
- Retry/watchdog polling to compensate for `ScheduleSolution` coalescing.
- Inline geometry conversion on every solve, including solves caused by an
  unrelated stage.
- Status coupling between Stage In and Stage Out.
- A 2-second hello/schema heartbeat to paper over missing schema events.

GH2 gives better primitives. Use them instead of recreating GH1's workarounds.

### 3.5 Blender side

**Verdict: no changes required for v2 parity.**

The Blender add-on has no GH1 API dependency. It is protocol-v2 only. A GH2
plugin that speaks v2 exactly works without Blender changes.

Optional additive changes worth doing early:

- Read `hello.doc_id` if present and use it for cache generation identity
  instead of `doc` (a display name that causes a cache reset when an unsaved
  document is saved).
- Recognise `duplicate_stage` as a distinct error rather than a generic stage
  error.
- Store `hello.capabilities` per connection and show GH2 features in the UI.
- Do not add a server→client handshake or change the codec; that is protocol
  v3.

The GH2-specific work is entirely on the Grasshopper side.

### 3.6 Tests, build, and deployment

**Verdict: add a first-class `grasshopper2/` target; keep GH1 untouched.**

- New SDK-style projects, `net8.0`, `TargetExt=.rhp`,
  `EnableDynamicLoading=true`,
  `PackageReference Grasshopper2 9.0.26300.9401 ExcludeAssets="runtime"`.
- Pin the NuGet version to the Rhino 9 build being supported. GH2 is WIP;
  floating versions will break.
- Build with the Linux .NET 8 SDK, not wine's Mono `csc.exe`.
- Do not put GH2 sources in `grasshopper/src/core` or `grasshopper/src/gh`:
  the GH1 build globs every `.cs` in those directories.

Deployment:

- Development: `GH2Plugins` → *Install…* the `.rhp`, or
  `PluginServer.InstallCustomPlugin`.
- The custom plugin list is almost certainly
  `%APPDATA%\Grasshopper2\Settings\CustomPluginInclude.txt` (string evidence in
  `Grasshopper2.dll`), but P0 must confirm the exact file and format.
- Release: a Yak package (`yak build`). GH2 scans Rhino plugin folders and Yak
  packages, and resolves `net48`/`net8.0` variants.

The current GH1 `check-deployed` is red on this machine because the built and
deployed `.gha` hashes differ. Fix or record that baseline before GH2 work.

### 3.7 Geometry mapping

| Wire | GH2 item | Notes |
|---|---|---|
| `mesh.*` | `Rhino.Geometry.Mesh` | Bulk vertices/faces; `MeshNgon.Create` + `Ngons.AddNgon`; `VertexColors.SetColors`; preserve face order |
| `subd.*` | `Rhino.Geometry.SubD` | Build cage from `SubD.Vertices`/`Faces`, not `CreateFromSubDControlNet`; map crease tags; Rhino 9 `SubDEdgeSharpness` for semi-sharp |
| `crv.*` | `Curve` / `Line` / `Polyline` / `Circle` / `Arc` / `Rectangle3d` | `CurveBroker.ToRhinoCurve` handles struct types; no `GH_Circle` special cases |
| `pts.*` | `PointCloud` or `Point3d` twig | `PointCloud.AddRange`; preserve colours/radii |
| mesh/surface/brep input | `Mesh` / `Surface` / `Brep` | GH2 holds Brep natively; export meshes with `brep_meshed` warning |
| `definitions` + `inst.*` | `StageloomDefinition` + `StageloomInstance` | No native GH2 block type; custom `ITypeAssistant` for display/bake; accept `InstanceReferenceGeometry` from document blocks |

Losses that remain even with perfect GH2 code:

- SubD per-end sharpness and vertex creases unless the wire is extended.
- Point radii unless modelled explicitly.
- N-gons with holes/slits.
- Brep→Blender.
- Materials and layers.

Every loss must be an explicit warning.

---

## 4. Architecture

```
Blender add-on (protocol v2, unchanged)
        │  binary WebSocket
        ▼
Stageloom GH2 plugin (.rhp, net8.0)
  StageloomPlugin : Grasshopper2.Framework.Plugin
    └── StageHub                         process-wide lifetime container
          └── DocumentStageState         per GH2 Document
                ├── LinkSession          ClientWebSocket, rx/tx tasks
                ├── StageRegistry        stage key → StageRuntime
                ├── ParameterRegistry    stage key → schema declarations
                ├── Encoder              background geometry → wire frame
                └── DocumentInfo         name, id, unit scale, capabilities
  Components (GH2 solve pool)
    StageIn, StageOut, Param, Attribute Get/Set, Point Cloud Points
  Custom types
    StageloomDefinition, StageloomInstance, ITypeAssistant(s)
```

### 4.1 Threading model

| Thread | Owns | Must not do |
|---|---|---|
| UI thread (Rhino/GH2) | Plugin load, document add/close, component add/remove, icons, document info snapshots | Block on network; mutate component state during a solve |
| Socket receive task | `ClientWebSocket.ReceiveAsync`, frame decode, publish immutable `RequestSnapshot`, `DelayedExpire` affected Stage In | Touch `Document`, `RhinoDoc`, or component fields |
| GH2 solve pool | `Process`: read snapshots, set trees, enqueue out jobs, read unit scale | Block on socket; encode 100k meshes; touch `RhinoDoc` |
| Socket send task | Drain bounded control/data queues, send frames, latest-wins per stage | Call GH2 APIs |
| Geometry worker | Stage Out encode; optional Stage In prebuild | Touch `RhinoDoc` or component UI |

GH2's solve loop is serial across components. One slow `Process` stalls the whole
document. Therefore:

- `Process` must be O(1) in the size of the geometry wherever possible.
- Geometry conversion happens on workers, not in `Process`.
- `Document.Solution.DelayedExpire(this)` is the re-solve trigger from workers.
- `Document.Solution.Start()` is never called from inside `Process`; it cancels
  the running solution.
- `StartWait()` is never called on the UI thread.

### 4.2 State model

- `RequestSnapshot`: immutable `{ rid, params, frame sections, attributes,
  definitions, geometry hash, revision }`.
- `StageRuntime`: latest request, `InStatus`/`OutStatus`, last applied
  `out_hash`, revision, pending out-job.
- `DocumentStageState`: link session, stages, parameters, document info,
  encoders.
- Publication uses `Volatile`/`Interlocked.Exchange`; no locks in `Process`.
- Rid pinning is per stage, not document-wide.
- Status epochs prevent late writes after a newer request.

### 4.3 Behavioural decisions

1. **Stage In never blocks.** The receive task decodes the wire frame and stores
   it. Geometry is built once per `(revision, unit scale)` and cached as an
   immutable `ITree`; `Process` sets that tree. Repeated solves with unchanged
   geometry are O(1).
2. **Stage Out never encodes in `Process`.** `Process` captures the input
   `ITwig`, rid, and unit scale into an `OutJob`; a worker encodes and publishes
   a response. `Process` returns immediately with "queued". The worker calls
   `Document.Solution.DelayedExpire(this)` when the status changes.
3. **Latest-wins per stage.** If a new request arrives while an encode runs,
   cancel the old job. If a new response is ready before the previous was sent,
   replace it. Control frames (hello/schema/errors) are separate and never
   dropped.
4. **No `RhinoDoc.ActiveDoc` off-thread.** Unit scale and document name are
   captured in `Process` or document events and stored in `DocumentInfo`.
5. **One link owner per document.** In v2, only one GH2 document may own a
   stage key. Duplicate keys produce a `duplicate_stage` error. True
   multi-document routing is protocol v3.
6. **No process-wide mutable link state in components.** `StageHub` is a
   lifetime container; all stage state is document-scoped.
7. **Shutdown is explicit.** `Plugin.OnLoaded` subscribes
   `RhinoApp.Closing`; `Document.StateChanged → Closed` disposes that
   document's state. GH2 has no plugin unload hook; assume process lifetime.

### 4.4 Transport

Replace the hand-rolled `MiniWsClient` with
`System.Net.WebSockets.ClientWebSocket` on .NET 8:

- Async receive/send with `CancellationToken`.
- `KeepAliveInterval` for ping/pong; no manual framing.
- `ArrayPool<byte>` buffers; enforce the 512 MiB / 1 MiB caps before allocating.
- Bounded send queues; latest-wins per stage; a separate control queue.
- Reconnect with jittered backoff. On reconnect, clear pending data frames and
  republish `hello`/`schema`.
- If `ClientWebSocket` misbehaves under wine, fall back to a hardened
  `MiniWsClient`, but do not start there.

This removes the largest piece of avoidable custom code in GH1 and is the right
choice on .NET 8.

---

## 5. Component mapping

| GH1 | GH2 |
|---|---|
| `GH_Component` | `Grasshopper2.Components.Component` |
| `ComponentGuid` | `[IoId("...")]` |
| `SolveInstance(IGH_DataAccess)` | `Process(IDataAccess)` |
| `GH_InputParamManager` | `InputAdder` |
| `GH_OutputParamManager` | `OutputAdder` |
| `GH_ParamAccess.item/list` | `Access.Item` / `Access.Twig` / `Access.Tree` |
| `IGH_Goo` / `GH_Mesh` / `GH_Curve` | `IPear`/`object` + raw Rhino geometry |
| `GH_Document.ScheduleSolution` + `ExpireSolution` | `Expire()` + `Document.Solution.DelayedExpire` / `Start` |
| `GH_Document.SolutionEnd` | `Solution.SolutionCompleted` / `SolutionStopped` |
| `GH_IWriter` / `GH_IReader` | `IWriter` / `IReader`, or `CustomValues` for primitives |
| `AddRuntimeMessage` | `access.AddRemark/Warning/Error` |
| `GH_AssemblyPriority` / ribbon | `Plugin` + `Nomen` + `.ghicon` |
| `RhinoDoc.ActiveDoc.ModelUnitSystem` | `access.GetUnitSystem` / `access.GetUnitScaling` |
| `GH_InstanceReference` / `ModelInstanceDefinition` | `StageloomInstance` / `StageloomDefinition` |
| `GooDispatch` special cases | `CurveBroker` / `SurfaceBroker` + pattern matching |

### 5.1 Stage In

- Inputs: `Key` (text, item), `Enabled` (boolean, item, default true).
- Outputs: `Geometry` (generic twig), `Request ID`, `Status`, `Message`,
  `Attributes` (twig of strings).
- `Process`: read the latest request snapshot, set the cached tree, add
  remarks/warnings, publish status.
- The tree order is mesh, SubD, curves, point cloud, instances.

### 5.2 Stage Out

- Inputs: `Key`, `Geometry` (generic twig), `Enabled`.
- Outputs: `Sent`, `Output hash`, `Status`.
- `Process`: read the generic twig, capture the rid and unit scale, enqueue an
  `OutJob`, return. Never convert or encode inline.
- The worker dispatches by runtime type:
  `Mesh`, `SubD`, `Brep`, `Surface`, `Curve`/`Line`/`Polyline`/`Circle`/`Arc`/
  `Rectangle3d`, `PointCloud`, `Point3d`, `InstanceReferenceGeometry`, custom
  `StageloomInstance`.
- Unknown types produce a remark, not a silent drop.

### 5.3 Param

- Inputs: `Key`, `Label`, `Type`, `Default`, `Min`, `Max`, `Step`, `Values`,
  `Group`.
- Output: generic value item.
- Use **`InstanceId` as the parameter GUID**. It is stable across save/load,
  new on copy/paste, and removes a hand-written `Write`/`Read` pair. Test
  copy/paste and undo explicitly.
- Fall back to `CustomValues.Get/Set("ParamId", Guid)` if `InstanceId` proves
  unsuitable.
- On user edit, take a `CustomValueAction` undo snapshot, set the value, and
  `Expire()`.
- Publish the schema on change and on first request for a stage; do not rely on
  a 2-second heartbeat.

### 5.4 Attribute Get / Set

- Get: generic item/twig in, `Name`; outputs `Values`, `Type`, `Domain`.
  Read the `MetaData` sidecar first, then `UserDictionary`.
- Set: generic item/twig in, `Name`, `Values`, `Domain`; duplicate the geometry
  before mutating, update both stores, and return the new pear.
- Values are typed `Twig<object>` entries, not flattened strings.

### 5.5 Point Cloud Points

Keep for parity. `PointCloud` is a raw item, not a GH2 parameter; a generic input
plus a `Point3d` twig output works.

### 5.6 Icons and metadata

- Embed `.ghicon` resources named after each component class.
- Plugin icon via `AbstractIcon.FromResource`.
- Use `[Author("Stageloom")]` and `Nomen` chapter/section names for the
  Stageloom tab.
- Documentation: GH2 `.spec`/`.topic` files next to the `.rhp`, with language
  folders if needed.

---

## 6. Protocol strategy

**Recommendation: v2 parity first, no breaking change. Add v3 only when
multi-document or async cancellation becomes a product requirement.**

### 6.1 v2 additive fields

Safe for current Blender because unknown fields are ignored:

- `hello.capabilities`: e.g. `["gh2", "metadata", "blocks", "semi-sharp"]`.
- `hello.doc_id`: stable document GUID. Blender can use it later for cache
  identity.
- `hello.stages[]`: optional `kind` (`in`/`out`/`param`) and `label`.
- `definitions[].id`: stable definition identity alongside `name`.
- `response.counts`: fill accurately.
- Optional SubD sharpness sections for GH2 semi-sharp values. Old readers
  ignore unknown sections; Blender needs a change to send/apply them.

### 6.2 v3 backlog

Only if the product needs it:

- Per-connection version negotiation and a server `welcome`/ack.
- `doc_id` on every message and multi-document routing.
- Per-document caches and generations.
- `cancel`, `progress`, `partial`/`final` responses.
- Response-side geometry refs, batch requests.
- First-class materials/layers/object identity.
- Native B-rep.
- Configurable units/space.
- Versioned fixture roots (`protocol/fixtures/v2`, `v3`).

Do not bump the protocol just because GH2 is new. v2 is sufficient for a
complete GH2 parity release and keeps the Blender side stable.

---

## 7. Geometry and attributes

### 7.1 Mesh

Decode:

- Overlay `mesh.verts` as `float3` spans, copy to `Point3f[]` once.
- `Mesh.Vertices.AddVertices(points)`.
- Build `MeshFace[]` for triangles/quads; `Faces.AddFaces(faces)`.
- N-gons: `MeshNgon.Create(boundary, fanFaces)` + `Ngons.AddNgon`.
- `VertexColors.SetColors` in bulk.
- Do not call `Compact()` after attribute arrays are index-aligned.

Encode:

- Emit vertices once.
- Emit faces in original mesh order; do not group n-gons at the end.
- If a face has holes/slits, fan it and add an `ngon_holes` warning.
- Align FACE/CORNER attributes with the emitted face order.
- Preserve vertex colours and crease metadata.

### 7.2 SubD

- Build the cage from `SubD.Vertices`/`SubD.Faces` component indices, not from
  `Mesh.CreateFromSubDControlNet`, which can unweld at creases and change
  vertex indices.
- Map wire crease values:
  - `1.0` → `SubDEdgeTag.Crease`.
  - `0.0` → `Smooth`.
  - fractional values → `SubDEdgeSharpness(v * 4)` on Rhino 9, with a warning
    if the wire or reader cannot carry it.
- Vertex creases: `SubDVertexTag.Crease` is binary; no numeric vertex sharpness
  exists.
- On export, emit crease/sharpness values and warn about unsupported
  per-end/vertex cases.

### 7.3 Curves

- Poly: `PolylineCurve`.
- Bezier/NURBS: exact route for uniform/clamped/periodic knots and weights.
- Piecewise rational Beziers: one wire curve per span.
- Fallback: cubic Bezier fit inside the deviation budget, with counts and
  measured deviation warnings.
- Accept `Line`, `Polyline`, `Circle`, `Arc`, `Rectangle3d`, and GH2
  `Triangle` on the export side via `CurveBroker.ToRhinoCurve`.
- No `GH_Circle`/`GH_Arc` special cases.

### 7.4 Points

- `PointCloud.AddRange(points, colors)` for bulk decode.
- Preserve `pts.colors` and `pts.radii` where the model supports them.
- GH2 has no `PointCloudParameter`; a custom `TypeAssistant<PointCloud>` may be
  needed for display/bake. Test this in P4.
- On export, emit positions, colours, and radii; warn if radii cannot be
  represented.

### 7.5 Breps and surfaces

- GH2 holds `Brep`/`Surface` natively.
- On export, mesh with `Mesh.CreateFromBrep` and add a `brep_meshed` warning.
- Blender has no B-rep; do not add a native B-rep wire section.

### 7.6 Attributes

- Canonical storage: `GeometryBase.UserDictionary` under
  `Stageloom.Attributes`.
- GH2 sidecar: `MetaData` key `Stageloom.Attributes`, `byte[]` or JSON.
- Descriptor: `{ group, name, domain, type, components, count, section }`.
- Validate counts and domains against the geometry.
- Deterministic order: sort attribute names before emitting descriptors and
  sections.
- Curve attributes: support `CURVE` and curve-point `POINT` domains in both
  directions.
- Instance attributes: carry on the definition and row metadata.
- If a GH2 operation changes topology, attribute counts no longer match.
  Stage Out must drop them with an `attribute_count_mismatch` warning, not
  silently misapply them.

### 7.7 Blocks and instances

GH2 has no native block type. Implement first-class plugin types:

- `StageloomDefinition`: name, geometry list, nested `(definition, transform)`
  rows.
- `StageloomInstance`: definition reference + transform.
- `StageloomInstanceAssistant : TypeAssistant<StageloomInstance>` for canvas
  display, bake, and serialization.
- Stage In emits definition items and instance items.
- Stage Out accepts custom items and `InstanceReferenceGeometry` from Rhino
  document blocks.
- Nested rows are a DAG by reference; definitions are content-addressed and
  cached across solves.
- No document write at solve time. Bake is a separate, explicit action.
- Bake path: `InstanceDefinitionTable.Add(...)` bottom-up, then
  `Objects.AddInstanceObject(...)`.
- If display/bake of a custom item is not viable in the P5 spike, fall back to
  flattened geometry with an explicit `instance_flattened` warning. Do not
  silently lose blocks.

---

## 8. Performance

### 8.1 Targets

| Scenario | Target |
|---|---|
| 100k-triangle mesh, Stage In conversion | ≤ 25 ms |
| 100k-triangle mesh, Stage Out encode | ≤ 25 ms |
| 100k round trip, transport + conversions, excluding GH solve | ≤ 100 ms |
| Parameter-only change | ≤ 5 ms plugin overhead + GH solve |
| Cached playback, 10k triangles | 60 fps |
| Plugin-induced solve pause | ≤ 16 ms except a deliberate large apply |
| Memory | bounded; no per-solve LOH growth; pooled buffers |

GH1 measured 30.3 ms for a 100k triangle round trip. GH2 should match or beat
that; the transport is not the bottleneck.

### 8.2 Techniques

- Zero-copy section decode over WebSocket buffers.
- `MemoryMarshal.Cast<byte, float/uint>` and `BinaryPrimitives`.
- Bulk RhinoCommon calls.
- Cache immutable trees per `(revision, scale)`.
- Off-thread Stage Out encode.
- Latest-wins response slots.
- `ArrayPool<byte>` for frames and receive buffers.
- `IncrementalHash` for output hashes.
- `CollectionsMarshal.AsSpan` for lists.
- `ParallelBudget` only where per-item conversion is independent.
- No per-item `Twig.Add`; build arrays and call `SetTwig<T>` or
  `Garden.TwigFromList`.

### 8.3 Benchmarks

- 1k / 10k / 100k triangle meshes.
- 10k curves.
- 10k instances.
- 100k attributes.
- Parameter-only updates.
- Cached playback.
- Multiple stages on one object.
- Report median and p95, plus allocations.

---

## 9. Testing and CI

| Layer | What | Where |
|---|---|---|
| 0. Protocol | 12 golden fixtures + float-format corpus; encode/decode/hash | GitHub-hosted Linux, .NET 8 |
| 1. Link core | hello/schema/request/response, ref/unknown_ref, reconnect, latest-wins, bounded queues, status epochs | GitHub-hosted Linux, fake server |
| 2. Headless GH2 components | Load plugin, emit components, inert document, `StartWait`, fake Blender server | Linux .NET 8/10 where possible; otherwise wine |
| 3. Geometry under Rhino 9 | Mesh 1k/10k/100k, n-gons, colours, creases, curves exact/approx, points, attributes, blocks | Wine + Rhino 9, self-hosted |
| 4. Blender end-to-end | A1–A10 equivalent, cache, playback, save/reload, reconnect, duplicates, multi-stage | Blender GUI + Rhino 9, manual release gate |
| 5. Performance | 1k/10k/100k, parameter-only, cached playback, allocations | Self-hosted benchmark |

Commands:

```
scripts/build-rhp             # dotnet build -> Stageloom.rhp
scripts/test-gh2-core         # net8 fixtures + link core
scripts/test-gh2              # headless GH2 component harness
scripts/test-gh2-live         # Rhino 9 + Blender e2e
scripts/bench-gh2             # performance report
scripts/check-deployed-gh2    # hash + GH2 log verification
```

CI split:

- **GitHub-hosted:** protocol core, link core, compile-only `.rhp`, static checks
  (`[IoId]`, `(IReader)`, assembly GUID, TFM, no GH1 references).
- **Self-hosted/licensed:** Rhino 9 wine load/solve, geometry, live e2e.
- **GH1 regression:** keep the current local/self-hosted GH1 build and tests.
  Make the build fail if optional GH1 suites are silently skipped.

Fixture policy:

- Freeze `protocol/fixtures/` for v2.
- Add a `float-format` corpus under a separate fixture root.
- If v3 is added, use `protocol/fixtures/v3/`; never rewrite v2 fixtures in
  place.

Headless harness:

- Reuse the verified sequence:
  `PluginServer.ScopePlugin` → `LoadPlugin` → `ObjectProxies.FindByType` →
  `Emit` → `Document.NewInertDocument` → `StartWait` → read outputs.
- Use a fake Blender WebSocket server for requests and responses.
- This is the main CI protection against GH2 API drift.

---

## 10. Repository, build, and deployment

```
grasshopper/                     # GH1 target, frozen
grasshopper2/
  src/
    Stageloom.Protocol/          # net8.0, no Rhino/GH: Frame, Json, ParamValues, hashing
    Stageloom.Rhino/             # net8.0, RhinoCommon: Conversion, Attributes, block types
    Stageloom.GH2/               # net8.0, Grasshopper2: plugin, components, link, hub
  tests/
    CoreTests/                   # fixtures + float corpus
    LinkTests/                   # fake Blender server
    HeadlessTests/               # GH2 load/emit/solve harness
    LiveTests/                   # Rhino 9 + Blender
  build/
  deploy/
protocol/
  fixtures/                      # frozen v2
  fixtures-v3/                   # only if v3
docs/
  plan-gh2.md                    # this document
  protocol.md                    # additive GH2 fields
```

Project shape:

```xml
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
    <EnableDynamicLoading>true</EnableDynamicLoading>
    <TargetExt>.rhp</TargetExt>
    <AssemblyName>Stageloom</AssemblyName>
    <RootNamespace>Stageloom</RootNamespace>
    <Version>1.0.0</Version>
    <Title>Stageloom</Title>
    <Company>...</Company>
    <Description>...</Description>
    <NoWarn>NU1701</NoWarn>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Grasshopper2" Version="9.0.26300.9401"
                      ExcludeAssets="runtime" />
    <EmbeddedResource Include="Icons\*.ghicon" />
  </ItemGroup>
</Project>
```

Add `global.json` pinned to SDK 8.0.x. Build with a repo-local/CI .NET SDK, not
the wine prefix.

Deployment:

- Development: `GH2Plugins` → *Install…*; confirm
  `CustomPluginInclude.txt` in P0.
- Release: Yak package (`yak build`), `net8.0` folder, `Stageloom.rhp`.
- Verify load in `%LOCALAPPDATA%\Grasshopper2\Diagnostics\*.ghlog`:
  `Plugin scoped:` and `Completed loading plugin`.
- Do not deploy into `Plug-ins/Grasshopper2/net8.0/Components/`; that is the
  core component folder.
- Add `*.rhp` and `grasshopper2/build/out/` to `.gitignore`.

---

## 11. Phases

| Phase | Work | Gate |
|---|---|---|
| **P0. Toolchain and deployment spike** | Pin .NET 8 SDK; build a trivial `.rhp`; load it in Rhino 9 wine; confirm `GH2Plugins`/`CustomPluginInclude.txt`; verify `ClientWebSocket` under wine; verify the headless `StartWait` harness | Trivial plugin loads, appears in GH2, connects to a fake Blender server, and solves; deployment file format confirmed |
| **P1. Protocol core** | `Stageloom.Protocol`: frame, JSON, hashing, params, float corpus; net8 fixture runner | All 12 fixtures + float corpus pass on net8 Linux |
| **P2. Link core** | `LinkSession`, `StageHub`, immutable snapshots, `ClientWebSocket`, queues, reconnect, publication, duplicate detection; fake-server tests | hello/schema/request/response/ref/reconnect/latest-wins pass headlessly |
| **P3. GH2 skeleton and components** | Plugin class, `[IoId]` components, `(IReader)`, `AddInputs/Outputs`, `Process`, `Nomen`, `.ghicon`; Stage In/Out/Param shells | Blender ↔ GH2 parameter-only round trip; save/reload/copy/paste/undo |
| **P4. Geometry and attributes** | Mesh/SubD/curves/points/Brep codecs; typed attributes; `MetaData` sidecar; Attribute Get/Set; performance pass | 100k mesh round trip, attribute parity, SubD creases, curve exact/approx, targets met |
| **P5. Blocks/instances** | `StageloomDefinition`/`StageloomInstance`, `ITypeAssistant` display/bake, nested DAG, `InstanceReferenceGeometry` read-back | Nested blocks both ways; display and bake verified; no document write at solve |
| **P6. Migration and release** | GH1→GH2 `IMigrateComponent` rules; Yak package; docs; acceptance run | A1–A10 equivalent pass; GH1 still green; release artifacts reproducible |

Indicative effort for one experienced developer: **5–7 weeks**. P0–P2 can run in
parallel across two people (protocol/link vs GH2 skeleton/build). P4 and P5 are
the long poles.

### Parallel workstreams

- **Track A:** Protocol core + float conformance.
- **Track B:** Transport/link core + fake server.
- **Track C:** GH2 plugin/components/document lifecycle.
- **Track D:** Geometry/attributes.
- **Track E:** Blocks/custom types.
- **Track F:** Build/deploy/CI/docs.

A and B start after P0 in parallel. C starts against the link interfaces. D
depends on A's section model. E depends on D's type model. F runs throughout.

---

## 12. Risks

| Risk | Mitigation |
|---|---|
| GH2 is 2.0 WIP; API churn | Pin NuGet `9.0.26300.9401`; adapter layer around GH2 types; nightly compile against the next 9.x |
| No async compute in GH2 | Immutable snapshots + background workers + `DelayedExpire`; never block `Process` |
| No plugin unload | Tear down on `RhinoApp.Closing` and `Document.Closed`; assume process lifetime |
| No native blocks | Custom `StageloomDefinition`/`StageloomInstance` + `ITypeAssistant`; P5 spike; explicit warning fallback |
| `ClientWebSocket` under wine | P0 spike; fallback to hardened `MiniWsClient` |
| Deployment ambiguity | P0 experiment with `GH2Plugins`; release via Yak |
| .NET 8 float formatting differs from Python | Float conformance corpus in P1; custom formatter or protocol-owned format |
| `InstanceId` semantics for Param GUID | Test save/load/copy/paste/undo in P3; keep `CustomValues` fallback |
| `MetaData` `byte[]` round-trip | P4 experiment; fall back to `UserDictionary` + JSON string |
| Mixed `Twig<object>` acceptance downstream | P4 test with Mesh/Curve/SubD/Brep/Point3d parameters |
| SubD cage vertex identity drifts | Build cage from `SubD.Vertices`/`Faces`; remap creases; test |
| Multi-document v2 limitation | Single link owner + `duplicate_stage` error; v3 for true multi-doc |
| Rhino 9 licensing in CI | GitHub-hosted compile/core only; self-hosted licensed runner for live tests |
| GH1 regression | Keep GH1 sources/outputs untouched; add a GH1 build gate |
| Stale deployed `.gha` already red | Fix or record baseline in P0 |

---

## 13. Decisions to confirm

Recommended defaults are shown first.

1. **Protocol:** v2 parity first, with v3 only for multi-document/async
   cancellation. Or multi-document in the first GH2 release?
2. **Blocks:** include custom GH2 block types in the first release, or ship
   Stage In/Out/Param/geometry first and add blocks in a follow-up?
3. **GH1 migration:** add `IMigrateComponent` rules so existing GH1 definitions
   can move to GH2, or treat GH2 as a fresh start?
4. **net48:** net8.0 only for Rhino 9, or also ship a net48 variant?
5. **Protocol core:** a separate modern `Stageloom.Protocol` project for GH2, or
   force shared source with the GH1 `.gha` build?
6. **Blender small patches:** include additive `doc_id`/`duplicate_stage`/
   `capabilities` changes now, or keep the Blender side completely frozen for
   the first GH2 release?

Recommended: **v2 parity first, blocks included, GH1 migration after parity,
net8.0 only, separate GH2 protocol project, and the small additive Blender
patches included.**

---

## Appendix A. GH2 API evidence

| Need | GH2 API |
|---|---|
| Plugin entry | `Grasshopper2.Framework.Plugin`, public parameterless ctor |
| Component base | `Grasshopper2.Components.Component` |
| Stable identity | `[IoId]` from `GrasshopperIO` |
| Deserialization | constructor `(IReader reader)` |
| Inputs / outputs | `AddInputs(InputAdder)`, `AddOutputs(OutputAdder)` |
| Solve | `Process(IDataAccess)` |
| Access modes | `Access.Item` / `Access.Twig` / `Access.Tree` |
| Data | `IPear`/`Pear<T>`, `ITwig`/`Twig<T>`, `ITree`/`Tree<T>`, `MetaData` |
| Diagnostics | `access.AddRemark/AddWarning/AddError` |
| Persistence | `IReader`/`IWriter`, `CustomValues` (`KeyedValues`) |
| Undo | `AddUndoRecord(VerbNoun, Action)`, `CustomValueAction` |
| Document | `Grasshopper2.Doc.Document`, `Document.Solution` |
| Expire / re-solve | `IDocumentObject.Expire()`, `Solution.DelayedExpire(obj)` |
| Solve start | `Solution.Start()`, `Solution.StartWait()` |
| Solve events | `SolutionAboutToStart`, `SolutionStarted/Stopped/Completed/Cancelled/Faulted` |
| Threading | `ThreadingState.MultiThreaded` / `SingleThreaded` / `UiSingleThreaded` |
| Cancellation | `access.Solution.Token` |
| Parallel budget | `Grasshopper2.Parallel.ParallelBudget` |
| Units | `access.GetUnitSystem`, `access.GetUnitScaling` |
| Icons | `.ghicon`, `SvgIcon`, `AbstractIcon` |
| Block-like types | custom item types + `ITypeAssistant` |
| Plugin discovery | `PluginServer.ScopePlugin`, `LoadPlugin`, `InstallCustomPlugin` |
| Deployment | Yak; `CustomPluginInclude.txt` for custom plugins |
| Headless | `Document.NewInertDocument`, `SolutionMode.Headless`, `StartWait` |

## Appendix B. Local evidence paths

- GH2 assemblies and XML:
  `~/.local/share/wineprefixes/rhino9/drive_c/Program Files/Rhino 9/Plug-ins/Grasshopper2/net8.0/`
- Rhino 9 net8 assemblies:
  `~/.local/share/wineprefixes/rhino9/drive_c/Program Files/Rhino 9/System/netcore/`
- GH2 log:
  `~/.local/share/wineprefixes/rhino9/drive_c/users/miquel/AppData/Local/Grasshopper2/Diagnostics/`
- GH2 settings:
  `~/.local/share/wineprefixes/rhino9/drive_c/users/miquel/AppData/Roaming/Grasshopper2/Settings/`
- .NET SDK used for verification: `/tmp/opencode/dotnet` (8.0.428)
- Working headless/build experiments:
  `/tmp/opencode/gh2/` and `/tmp/opencode/dumptest/`
