# GH Stack — Project Plan (rev 2)

**Status:** rev 2 approved. M0 done (2026-10-10): repository skeleton, this plan, protocol draft (`docs/protocol.md`), decision records (`docs/decisions/`), spike scripts (`spikes/`). **S2 passed 2026-10-11** (transport on a background thread, `spikes/results/s2-gui.txt`, `s2-background.txt`): `websockets` 17.2 runs on a plain thread in Blender 5.2, queue handoff works, and a 4 ms `bpy.app.timers` drain holds ~5 ms median under a 2 MB load. **S1 passed 2026-10-11** (the transport gate, `spikes/results/s1.txt`): a C# `.gha` under wine connects, a background message triggers `SolveInstance` on the UI thread through `ScheduleSolution`, replies come back in 20–65 ms, and save/reopen reconnects with no dialog. The plan's `ClientWebSocket` choice was replaced by a TcpClient client (see §19). **M2 done 2026-10-11**: protocol core, ten golden fixtures verified by both codecs. Next: S3.
**Authoritative copy:** `docs/plan.md` in `~/workspace/gh-blender-stack`. The a shared network folder copy is a mirror.
**Date:** 2026-10-10.
**Replaces:** `handoff-1-grasshopper-3dm-plugin.md` and `handoff-2-blender-3dm-addon.md` (deleted; their useful facts are in §19).
**Rev 2 change:** the repository lives on the local machine (D5). No cross-machine working.

**Machines (working model)**
- **This machine (`the dev machine`)** — everything in the build loop: code, git, Blender 5.2.2 (Python 3.14.7), the Rhino 8 wine prefix, `csc.exe`, GH plugin builds, tests, benches.
- **`a home server`** — the a shared network folder host and the old `an earlier link project` git repo with Portal. Not involved in day-to-day work. The project's a shared network folder folder remains the coordination surface, and its `PLAN.md` is superseded by this revision once the repo exists.

---

## 0. Decisions

| # | Decision | Status |
|---|---|---|
| D1 | Transport is our own WebSocket protocol. Portal stays installed and untouched for ad-hoc work. | Decided |
| D2 | SubD creases: edge crease tags transfer. Semi-sharp values and vertex creases do not, because RhinoCommon does not expose them. Documented, not worked around. | Decided |
| D3 | Curves that cannot be represented exactly are approximated and reported as warnings. Never silent. | Decided |
| D4 | The handoffs are removed. This document replaces them. | Done |
| D5 | The repository (`gh-blender-stack`) lives on the local machine at `~/workspace/gh-blender-stack`. All work happens here. | Decided |
| — | Carried over: GPL-3.0, Blender extension packaging, data root `~/.local/share/gh-stack/`, no file protocol, no rhino3dm, no GhPython. | Decided |

---

## 1. Goal

Drive Grasshopper definitions from Blender as stackable modifiers, alongside native Geometry Nodes modifiers, in one visible object per stack. Both geometry and parameters flow through the stack. Parameters are Blender properties, so they can be keyframed, driven by drivers, and baked. The result must feel immediate: edits, keyframes, and playback should be snappy and seamless.

---

## 2. Scope

**In v1**
- Meshes with vertex colors and n-gons.
- SubD as a control cage with tag-based edge creases (D2).
- Curves: poly, Bezier, and NURBS, with approximation and warnings where needed (D3).
- Points.
- Brep, converted to mesh on the GH side (lossy by nature; warned).
- Parameters: float, int, bool, vector3, color, choice. Text parameters are supported but not animatable.
- Mid-stack GH stages (`GH → GN → GH`), enable/disable, reorder, freeze, preview.
- Keyframes, drivers, bake, and playback from the bake cache.
- Undo and redo, save and reload.
- Units in meters on the wire.

**Out of v1**
- SubD semi-sharp creases and vertex creases (D2).
- GH → Blender values as drivers. Planned for v1.1 as read-only outputs.
- Materials, layers, and named object identity from GH.
- Multiple GH documents answering one stage key.
- Instancing.
- Windows-native Rhino as a tested target. The code is written to allow it, tested later.

---

## 3. Principles

1. **The modifier stack is the graph.** No second copy of the stack exists.
2. **Push, never poll.** WebSocket in both directions. The only timer drains an in-process queue.
3. **State is derived and content-addressed.** Undo, reorder, disable, and playback mostly hit the cache, not GH.
4. **One visible object per stack.** Helper objects are hidden plumbing.
5. **Definitions still run standalone.** A GH definition with Param components works without Blender, using defaults.
6. **Lossless where representable; approximations are reported.** Every loss appears in a status warning, a GH runtime message, and the log.
7. **Thread discipline.** `bpy` only on the Blender main thread. GH objects only on the GH UI thread. Sockets live on background threads and communicate through queues or slots.
8. **No dialogs in GH.** Runtime messages only. The old track died on a message viewer under wine.

---

## 4. Architecture

```
Blender 5.2 (this machine)                     Grasshopper / Rhino 8 (wine, this machine)
┌────────────────────────────────────────┐      ┌────────────────────────────────────┐
│ Stack object (the one visible object)  │      │ GH definition                      │
│   base geometry                        │      │   [Param] ──► user graph           │
│   native modifiers:                    │ WS   │   [Stage In] ──► user graph        │
│     [GH stage A] [GN …] [GH stage C]   │◄════►│   user graph ──► [Stage Out]       │
│   custom props gh/<stage>/<param>      │binary│                                    │
│   hidden helpers (per stage, per type) │      │                                    │
│ Engine + WS server (background thread) │      │ WS client (background thread)      │
└────────────────────────────────────────┘      └────────────────────────────────────┘
```

**Components**

| Component | Where | Responsibility |
|---|---|---|
| `gh_stack` add-on | Blender | UI, stack model, engine, WebSocket server, geometry conversion, parameter properties, bake |
| `GhStack.gha` | Grasshopper | Stage In, Stage Out, Param components; WebSocket client; schema publication |
| Protocol | both | Versioned binary-plus-JSON messages (§8) |
| Data root | this machine | `~/.local/share/gh-stack/`: bake cache and logs |

Portal (`Portal.Gh` 0.5.2 and the patched Portal.blender 0.3.0) stays installed side by side. It uses its own port and shares no code or state with this project.

---

## 5. Blender data model

| Thing | Visible | Contents |
|---|---|---|
| Stack object | Yes | Base geometry, native modifiers, `gh_stack_id` (uuid), stage records (key, label, state, current request ID, applied output hash, last error, timings, frozen flag), and the parameter custom props. |
| GH stage modifier | Yes, in the modifier panel | Geometry Nodes modifier built from the "GH Stage" template. Named by its label, e.g. `GH · Panelize`. |
| Stage node group | No | One per stage, owned by that modifier. Named `GHS <key>`. Custom props: `gh_stage_key`, `gh_stack_id`. Nodes: one Object Info per geometry type → Join Geometry → Group Output. The group input is ignored. |
| Helpers | No | Hidden objects in the excluded collection `GH Stage Data`. Created on first use, one per geometry type per stage (mesh, curves, points). Identity transform, data in stack-local space. Named `GHS <stack>.<key>.<kind>`. |
| Parameters | Yes, on the stack object | Custom props `gh/<stage_key>/<param_id>` with UI ranges and defaults. |
| Cache | Disk and memory | Content-addressed responses, keyed by request ID. |

**Rules**
- **Stage keys** are `<slug>-<6 hex>`, fixed at creation. Labels can change; keys never do. Keys are what you paste into GH.
- **Parameter keys use a GUID** stored on the GH Param component, not the display name. Renaming a parameter in GH keeps its keyframes.
- **Each stage owns its node group.** Duplicating a modifier shares the group in Blender. On each engine tick, the add-on detects shared groups and gives the duplicate its own group, key, and helpers. The duplicate shows as unbound until it is wired in GH.
- **Object Info uses Original transform space.** Verified: with the stack object rotated and moved, the output stays in stack-local coordinates. The default Relative space gives wrong coordinates.
- **Modifier inputs are never set from Python.** Setting an object-typed modifier input hung headless evaluation in testing. Everything is wired through node default values.
- **Stored state lives in the `.blend`**, so undo and save/reload stay consistent.

---

## 6. Geometry model

Blender reads geometry components through `Object.evaluated_geometry()`, which returns mesh, curves, and point components separately. Reading `evaluated.data` on a mesh object drops the other components.

| Type | Blender representation | Round trip | Notes |
|---|---|---|---|
| Mesh | Mesh with vertex colors | Exact | N-gons kept. Rhino 8 meshes support n-gons (`Mesh.Ngons`). |
| SubD | Control cage mesh, `crease_edge` attribute, and a `mesh.subd` flag | Cage exact. Edge creases as tags (D2) | Display with a native Subdivision Surface modifier you place. The add-on never subdivides on its own. |
| Line, polyline | Poly spline | Exact | |
| Arc, circle, NURBS with clamped, uniform, or Bezier knots | NURBS spline with weights, order, and knots mode | Exact | Weights, order, and knots mode survive a GN object reference (verified). |
| NURBS with interior non-uniform knots | Bezier spans (`NurbsCurve.MakePiecewiseBezier`) | Approximated where rational (D3) | Warned. |
| Bezier (Blender) → GH | NurbsCurve, degree 3 | Exact | |
| Point | Point cloud | Exact | |
| Brep | Mesh (`Mesh.CreateFromBrep`) | Lossy by nature | Blender has no B-rep. Warned once per stage. |

**SubD crease rules (D2)**
- *GH → Blender:* an edge with tag `Crease` gets `crease_edge = 1.0`. Tags `Smooth`, `SmoothX`, and `Unset` get `0.0`. Vertex sharpness is not transferred.
- *Blender → GH:* edges with `crease_edge >= 0.5` get the `Crease` tag. Others get `Smooth`. Fractional values are rounded to the nearest tag, and a warning reports how many edges were affected.
- *Setter risk:* RhinoCommon's public XML shows `SubDEdge.Tag` as a property but does not show a setter. Spike S4 confirms the setter. If none exists, Blender → GH builds the SubD without creases and warns.
- *Warning text:* every stage with SubD output reports "creases transferred as tags; semi-sharp and vertex creases are not available from RhinoCommon".

**Curve approximation policy (D3)**
- A curve is transferred exactly whenever Blender's knot modes can express it.
- Otherwise it is converted to Bezier spans (Rhino's `MakePiecewiseBezier`). Rational spans become non-rational approximations.
- The engine samples the deviation between the original and the approximation.
- A warning is emitted when the maximum deviation exceeds the threshold (default 0.1 mm). Every approximated curve is counted in the stage status regardless of deviation.
- Approximation never blocks the transfer. The warning is the signal.

**Mixed outputs.** A stage can emit any combination. Blender keeps them as geometry-set components. Native mesh-only modifiers such as Bevel and Subsurf act on the mesh component only. This is standard Blender behavior and is checked in spike S6.

**Stack type.** The stack object's base type sets what the native modifiers act on. GH curves and points ride along as components. Stacks whose base is empty are allowed. GH can supply everything.

---

## 7. Parameters and animation

**GH side.** A `Param` component declares a parameter:
- A GUID, a label, and a type: float, int, bool, vector3, color, choice, or text.
- A default, a range, and a step.
- A group label.

It outputs the value Blender sends when connected, or the default otherwise.

**Blender side.** The schema becomes custom props on the stack object, named `gh/<stage_key>/<param_id>`:

| GH type | Blender representation | Animatable | Notes |
|---|---|---|---|
| Float | Float property | Yes | |
| Int | Int-valued float property | Yes, constant interpolation | Cast to int on the GH side. |
| Bool | 0/1 property | Yes, constant interpolation | |
| Vector3 | Float array of 3, subtype XYZ | Yes, per component | |
| Color | Float array of 3, subtype COLOR | Yes, per component | |
| Choice | Int index plus a stored choice list | Yes, constant interpolation | Dropdown drawn in the add-on panel. |
| Text | String property | No | Documented limitation. |

**Verified:**
- Keyframes on a custom property interpolate (1.0 → 5.0 across frames 1–10 gives 2.668 at frame 5).
- A driver with a scripted expression on a custom property drives it (`frame * 0.5` gives 2.0 at frame 4).
- Ranges are set through `id_properties_ui`.
- In Blender 5.2, `action.fcurves` no longer exists. Access is through `action.layers[].strips[].channelbags[].fcurves`. Any code touching keyframe interpolation must use that path.

**Parameter UI.** A "GH Parameters" panel per stage shows the widgets, with "Key all" and "Clear keys" buttons. Keyframe and driver decorators are verified in spike S5. Parameters removed from GH move to an orphan list, so their keyframes are not dropped silently. Orphans can be purged explicitly.

**Bake.** An operator steps through a frame range. For each frame it evaluates the upstream prefix, computes the request ID, and waits for the GH response if it is not cached. Results are stored in the disk cache under their request IDs. Playback reads from that cache. Editing changes request IDs, so stale frames stop matching without any explicit invalidation. Bake is cancelable and shows progress.

**Playback without bake.** Frames that are not cached show last-good geometry with an "updating" indicator. Requests are latest-wins, so GH is never asked for a stale frame.

---

## 8. Protocol v2

### 8.1 Transport
- Blender runs the server on `127.0.0.1:9130` (configurable), using the `websockets` package. Version 17.2 is pure Python (`py3-none-any`) and was tested on Python 3.14.7.
- GH runs the client on a background thread with `System.Net.WebSockets.ClientWebSocket`.
- Loopback only. No authentication in v1.

### 8.2 Frame
One binary WebSocket message:
```
[uint32 little-endian: header length][header: UTF-8 JSON][sections: raw bytes, in header order]
```
The header lists each section's name, dtype, shape, and byte length. Anything not listed in the header is invalid.

### 8.3 Message types

| `type` | Direction | Header fields |
|---|---|---|
| `hello` | GH → Blender | `proto` (2), `client`, `doc`, `doc_units`, `stages` (keys present and their enabled flags) |
| `schema` | GH → Blender | `stage`, `params`: list of `{id, label, type, default, min, max, step, choices, group}` |
| `request` | Blender → GH | `stage`, `rid`, `params` (id → value), `geom` (`{mode: "full"\|"ref", hash, subd}`), `units` ("m"), `space` ("stack-local") |
| `response` | GH → Blender | `stage`, `rid`, `out_hash`, `status` ("ok"\|"error"), `error`, `warnings`, counts |
| `ping` / `pong` | both | `t` |

Protocol mismatches (different `proto`) are rejected with an error message in both directions.

### 8.4 Sections

| Name | dtype and shape | Meaning |
|---|---|---|
| `mesh.verts` | f32 N×3 | Positions |
| `mesh.face_counts` | u32 F | Vertices per face (3, 4, or n) |
| `mesh.face_indices` | u32 sum | Vertex indices |
| `mesh.colors` | u8 N×4 | Optional vertex colors |
| `mesh.crease_edges` | u32 E×2 and f32 E | Edge pairs and crease values (0 or 1 from GH; fractional allowed from Blender) |
| `crv.points` | f32 M×4 | x, y, z, weight |
| `crv.counts` | u32 C | Points per curve |
| `crv.types` | u8 C | 0 poly, 1 Bezier, 2 NURBS |
| `crv.orders` | u8 C | NURBS order |
| `crv.knots_mode` | u8 C | 0 normal, 1 endpoint, 2 Bezier |
| `crv.cyclic` | u8 C | 0 or 1 |
| `pts.positions` | f32 P×3 | |
| `pts.radii` | f32 P | Optional |
| `pts.colors` | u8 P×4 | Optional |

### 8.5 Request IDs and hashes
- **Geometry hash:** SHA-256 over the canonical section bytes in the order above, with section names included. Computed with numpy. Never a hash of a file.
- **Request ID:** SHA-256 over the stage key, the canonical parameter JSON (sorted keys, fixed float formatting), the geometry hash, the units, the space, and a nonce. Push increments the nonce.
- **Output hash:** SHA-256 of the response sections, computed on the GH side.

### 8.6 Apply rules
- A response is applied when its stage matches, its `rid` equals the stage's current request ID, and its `out_hash` differs from what is applied.
- A response for an older `rid` is cached under its own ID but not applied.
- A GH-initiated change (a slider upstream of Stage Out moves) arrives as a new response with the same `rid` and a new `out_hash`. It is applied.
- A cached result for the current `rid` is applied without contacting GH.

### 8.7 Geometry transfer
- `full` sends sections. Used when the geometry hash changes.
- `ref` sends only the hash. GH reuses the geometry it last received for that stage.
- Parameter-only changes therefore send only a few bytes.

### 8.8 Lifecycle
- On connect, GH sends `hello` and `schema`. Blender replies with the current request for each stage.
- Disconnect keeps last-good geometry and sets stages to `disconnected`.
- GH reconnects with backoff. No manual step is needed.
- Two GH documents claiming one stage key put both stages into `error`, with the other document named in the message.

---

## 9. Engine

### 9.1 Per tick, for each enabled GH stage in stack order
1. Evaluate the stack up to this stage on a clone: `stack.copy()`, remove the later modifiers, then read `evaluated_geometry()` for each component type.
2. Read the stage's current parameter values from the custom props.
3. Compute the request ID.
4. If the cache holds the request ID, apply it.
5. Otherwise send `request` (full or ref) and **stop**. Later stages would run on stale input.

A stage's prefix never includes its own parameters, so a parameter change cannot feed back into its own request.

### 9.2 States
`idle` · `waiting` · `ok` · `error` · `disconnected` · `disabled` (no requests; downstream receives input directly) · `frozen` (no requests; the helper keeps its last output) · `unbound` (no GH document has answered yet).

### 9.3 Latest-wins
- One request is in flight per stage. A new request supersedes the previous one.
- Stale responses are cached, not discarded.

### 9.4 Triggers
- Depsgraph updates, coalesced to one pass per tick.
- Frame change, which also reaches parameters through the animation system.
- Incoming messages, drained from a queue by a main-thread timer: every 5 ms while any stage is waiting, every 50 ms otherwise. **Verified in S2:** a 4 ms timer request holds ~5 ms median in the GUI, including under a 2 MB load, so the 5 ms target is reachable. Two findings bound the design: timers only fire after a script returns to the event loop (they are inert during a blocking main-thread script), and `bpy.app.timers` does not tick at all in `blender -b`. Layer B tests therefore call the same drain function directly instead of waiting on timers.
- Operators.

### 9.5 Timeouts and failures
- Default timeout: 30 s, configurable.
- A timeout or GH error sets `error`, keeps the last good output, and shows the message.
- The engine retries on the next change or on Push. It does not loop on failures.

### 9.6 Loop guard and ownership
- Engine writes are tagged and ignored by the depsgraph handler.
- A stage's input is always upstream of its output, so the engine cannot form cycles.
- Shared node groups are detected and split on each tick.

### 9.7 Undo, save, reload
- Stage records, helper data, and node groups live in the `.blend`.
- The disk cache is content-addressed, so restoring an earlier state is a cache hit.
- On load, stages are re-bound by key. Missing helpers or groups are rebuilt and marked `unbound`.

### 9.8 Operators
Add Stage, Remove Stage, Push, Pull, Recompute from here, Freeze, Preview, Key all, Clear keys, Bake range, Purge orphans. All are undo-safe.

- **Add Stage** creates a node group from the template, assigns a new key, creates the helpers on first use, and inserts the modifier after the active one.
- **Remove Stage** removes the modifier and its helpers. The node group is removed when it has no users. Cache entries are kept.
- **Freeze** stops requests. The helpers keep their last output.
- **Preview** temporarily includes the stage's helpers in the view layer as a display object outside the stack.

---

## 10. Grasshopper plugin

**Name:** `GhStack.gha`. Tab: "GH Stack". Install path: `…/rhino8/drive_c/users/miquel/AppData/Roaming/Grasshopper/Libraries/GhStack/`.

**Components**

| Component | Inputs | Outputs |
|---|---|---|
| Stage In | Key, Root (ignored in v2, reserved), Enabled | Geometry (Mesh, Curve, Point, SubD), Request ID, Status, Message |
| Stage Out | Key, Geometry (any Rhino geometry), Request ID, Enabled | Sent (bool), Output hash, Status |
| Param | GUID (generated, hidden), Label, Type (menu), Default, Min, Max, Step, Choices, Group, Override (optional) | Value |

Component GUIDs are generated once and never change. Save and reload depend on them.

**Threads**
- A background thread owns the socket. It parses incoming messages and writes them into a per-stage latest slot under a lock.
- The socket thread asks the UI thread to solve through `GH_Document.ScheduleSolution(int, delegate)`. The delegate calls `ExpireSolution` on the affected components.
- Stage In reads its slot during `SolveInstance`, on the UI thread.
- Stage Out queues its response at the end of the solve, and the socket thread sends it.
- `RhinoApp.InvokeOnUiThread` is the fallback if `ScheduleSolution` misbehaves under wine.

**Conversion**
- Incoming: sections become Rhino Mesh, NurbsCurve, PolylineCurve, or point objects. Units are converted from meters using `RhinoDoc.ModelUnitSystem`.
- Outgoing: Rhino geometry becomes sections. Units are converted back to meters.

**Rules**
- C# 5, built with the wine `csc.exe` (v4.0.30319).
- References: `GH_IO.dll`, `Grasshopper.dll`, `RhinoCommon.dll`, `System`, `System.Runtime.Serialization.dll`.
- Header JSON uses `DataContractJsonSerializer`. No third-party DLLs, so nothing conflicts with Portal's copies.
- Runtime messages only, never dialogs.
- A disabled stage (Enabled = false) reports itself in `hello` and sends no responses.

**Build and deploy**
- Build on this machine with `~/.local/share/wine-rhino/bin/wine` and `WINEPREFIX=~/.local/share/wineprefixes/rhino8`.
- Copy the `.gha` into the Libraries folder.
- Never kill the wineserver. The live Rhino session lives in it.

---

## 11. Blender add-on

**Layout**
```
blender/gh_stack/
  __init__.py
  blender_manifest.toml          extension manifest
  core/                          pure Python, no bpy
    codec.py                     frame encode and decode, sections
    hashing.py                   geometry hash, request IDs
    rules.py                     the per-stage decision function
    cache.py                     cache index and eviction
    params.py                    schema types and value casting
  blender/
    server.py                    WebSocket server on a background thread
    bridge.py                    queues between the server thread and the main thread
    stack.py                     prefix evaluation on clones
    geometry_io.py               numpy in and out, in-place writes
    nodes.py                     templates, ownership, Join and Object Info wiring
    helpers.py                   helper objects and the GH Stage Data collection
    props.py                     parameter custom props and UI ranges
    engine.py                    tick loop, states, triggers, guard
    bake.py                      frame stepping and cache fill
    ops.py, ui.py, prefs.py
  vendor/websockets/             pure-Python wheel
blender/tests/
```

**Packaging.** A Blender extension (`blender_manifest.toml`). `websockets` is platform-neutral, so one zip covers Linux and Windows. Spike S8 confirms vendoring works on 5.2. Fallback: plain `vendor/` on `sys.path`, as Portal does.

**Preferences**
- Server port (default 9130).
- Timeout (default 30 s).
- Curve deviation warning threshold (default 0.1 mm).
- Cache size limit.
- Data root (default `~/.local/share/gh-stack/`).
- Log level.

**UI**
- Modifier panel: native. GH stages appear as normal modifiers, with enable, reorder, and delete.
- N-panel "GH Stack": per-stage rows showing label, copyable key, state, last solve time, warnings, and errors. Buttons for the operators in §9.8.
- N-panel "GH Parameters": one section per stage, with widgets, keyframe decorators, Key all, and Clear keys.
- Status bar or header indicator for connection: listening, connected (with GH document name), or error.

---

## 12. Repository and environments

**Repository:** `~/workspace/gh-blender-stack` on this machine, GPL-3.0. Created in M0. This plan moves into its `docs/` at that point, and the a shared network folder folder becomes a documentation archive that stays approximately current. Optional later: add `a home server` or another host as a git remote for backup.

```
gh-blender-stack/
  README.md
  docs/
    plan.md                      this document (rev 2)
    protocol.md                  protocol v2 spec
    decisions/                   ADRs for D1–D5 and later decisions
  protocol/fixtures/             golden frames shared by the Python and C# tests
  blender/                       the add-on (§11)
  grasshopper/
    src/core/                    codec, hashing, conversion (RhinoCommon only)
    src/gh/                      components
    tests/console/               codec and conversion tests under wine
    build/                       csc build
    deploy/                      install into Libraries
  tools/fake_gh/                 Python WebSocket client implementing the GH side
  spikes/                        spike scripts and results (from /tmp/opencode)
  scripts/                       build, deploy, bench
```

**Working loop.** Everything is local: edit, `csc` under wine, Blender headless tests, live tests with GH under wine. No SSH steps. The old cross-machine scripts are not needed.

**Data root:** `~/.local/share/gh-stack/`
- `cache/<stack_id>/<stage_key>/<request_id>.bin`
- `logs/`
- In wine, `Z:\home\<user>\.local\share\gh-stack\`

**Left untouched:** the `~/workspace/an earlier link project` runtime mirror (Portal examples and smoke test) and the `an earlier link project` repo on `a home server`.

---

## 13. Performance rules and budgets

**Rules**
1. Push, never poll.
2. Latest-wins everywhere.
3. Parameter-only messages carry no geometry.
4. Binary arrays through `foreach_get` and `foreach_set`. Never `from_pydata` on the update path.
5. Update mesh data in place. Objects and modifiers are never recreated.
6. Drags are coalesced to one update per tick.
7. Playback never blocks. Uncached frames show last-good output.
8. Bake fills the cache off the critical path, cancelable.
9. Per-tick engine work stays small. A large apply may cost one frame.

**Measured this session** (Blender 5.2.2; `websockets` 17.2 locally; about 100k triangles unless noted)

| Operation | Result |
|---|---|
| Read mesh arrays into numpy | 15 ms |
| Write mesh arrays via `foreach_set` | 22 ms |
| Write via `from_pydata` (avoid on the update path) | 89 ms |
| JSON encode, Portal-style | 74 ms, 3.8 MB |
| Binary blob for the same mesh | 1.8 MB |
| Small-message WebSocket round trip | 0.05 ms median |
| 2 MB binary echo round trip, both directions, pure Python | 57 ms |
| `websockets` server on a Blender background thread, queue handoff (S2) | 401 messages, max enqueue-to-apply 2.1 ms |
| 4 ms `bpy.app.timers` drain under a 2 MB stream load (S2) | 5.07 ms median gap, max 76 ms |

**Budgets to validate in S3 (targets, not guarantees)**
- Parameter change to viewport, light definitions: GH solve plus about 1 ms of transport.
- Geometry hop at 100k triangles: about 100 ms of transport and conversion on top of the GH solve. The C# conversion cost is measured in S3.
- Cached playback: at least 24 fps for 10k-triangle outputs. 100k triangles is expected to be borderline.
- No UI tick above 16 ms during steady updates, except one large apply.

GH solve time will dominate real definitions. The transport is not the bottleneck.

---

## 14. Testing

- **Layer A, pure core.** Codec round trips, hashing, request-ID stability, rule decisions, latest-wins, stale responses, cache behavior. Standard-library `unittest`.
- **Layer B, Blender headless.** `blender -b --factory-startup --python-exit-code 1`. Templates and ownership, in-place updates, prefix evaluation, parameters through keyframes and drivers, undo and redo, save and reload, disconnect and reconnect, bake. Runs against `tools/fake_gh`. Note: `bpy.app.timers` is inert under `-b` (S2), so layer B drives the queue drain directly rather than through the timer the GUI uses.
- **Layer C, GH.** Codec and conversion tests under wine. Live tests: one round trip, parameter changes, save and reopen, GH closed and reopened.
- **Layer D, end-to-end and bench.** Real definitions and a GN chain. Timings at 1k, 10k, and 100k triangles per hop.
- **Golden frames** in `protocol/fixtures/`. Both sides must encode and decode them identically.
- **Acceptance** (§16) is the release gate.

---

## 15. Milestones and spikes

**Spikes (M1)**

| Spike | Question | Gate |
|---|---|---|
| **S1** | Does a C# `GhStack.gha` under wine connect with `ClientWebSocket`, trigger a solve from a background message via `ScheduleSolution`, send a response to Blender, and save and reopen the document with no dialog? | Yes: continue. No: fall back to Portal's transport (its WebSocket components exist upstream), and the engine stays the same. **Passed 2026-10-11 with a TcpClient client instead of `ClientWebSocket`** (Mono cannot reference it; see §19). Connect, background-message solve, reply in 20–65 ms, and save/reopen reconnected with no dialog: `spikes/results/s1.txt` |
| **S2** | Does `websockets` run in a Blender 5.2 background thread, with queue handoff and no UI stalls? | Yes: continue. |
| **S3** | Geometry round trip with the C# side at 10k and 100k triangles. | Budgets in §13 met or revised with data. |
| **S4** | SubD: can `SubDEdge.Tag` be set? Does `SubD.CreateFromMesh` produce a usable SubD from a cage? Are the curve approximation paths behaving? | Setter exists: tags round-trip. No setter: tags are dropped on Blender → GH with a warning (D2). |
| **S5** | Parameter UI in the GUI: keyframe and driver decorators on custom props, vector and color arrays per component, choice dropdown. | Decorators and arrays work, or the UI is adjusted. |
| **S6** | Mixed output (mesh plus curves) through a stage on a mesh stack: display, native modifiers, and what `evaluated_geometry()` returns downstream. | Behaves as §6 says, or the mixed-output rules are revised. |
| **S7** | Helpers in the GUI: Object Info in Original space for mesh, curve, and point helpers. Duplicate and reload ownership. | Works in the GUI. |
| **S8** | Extension install on 5.2 with the vendored `websockets` wheel. | Installs. Fallback is `vendor/`. |

**Milestones**

| | What | Exit criteria |
|---|---|---|
| M0 | Create `~/workspace/gh-blender-stack`. Move this plan into `docs/plan.md`. Write `docs/protocol.md` draft and ADRs for D1–D5. Copy the probe scripts from `/tmp/opencode` into `spikes/`. | Repo exists. Pure-core tests run (empty) |
| M1 | Spikes S1–S8 | All gates passed, or the fallback is chosen and recorded. **S1 and S2 passed** |
| M2 | Protocol v2 core, golden frames, rules | Layer A green. **Done 2026-10-11**: codec, hashing, params, rules, cache (43 tests), ten golden fixtures verified by both the Python codec (`scripts/test-core`, 50 tests) and the C# codec under wine (`scripts/test-gha`) |
| M3 | Engine against `tools/fake_gh` | Layer B green, including bake, undo, save and reload |
| M4 | GH plugin: Stage In, Stage Out, Param | Layer C green: live round trip, parameter change, save and reopen, GH closed and reopened |
| M5 | End-to-end with real definitions | Acceptance A1–A10 pass. Latency reported |
| M6 | UI: stack panel, parameter panel, add stage, status, preferences, bake, preview, freeze | Manual checklist on this machine |
| M7 | Hardening: large meshes, multiple stacks, GH and Blender restarts, extension build, docs | Release notes. Optional Windows-native pass |

---

## 16. Acceptance

- **A1, keyframes.** Keyframe a GH float parameter in the Dope Sheet. Playback drives the GH output per frame. After a bake, playback is served from the cache.
- **A2, drivers.** A driver on a custom prop, driven by an Empty's distance, moves the GH output live.
- **A3, chain.** Mesh → GH A → GN (bevel, array) → GH C → native GN. Editing the base updates the whole chain with no manual steps.
- **A4, stack edits.** Reorder, disable, and enable create no datablocks. Restored states are cache hits.
- **A5, curves.** Representable curves round-trip exactly. Approximated curves are reported with counts and deviations.
- **A6, SubD.** The cage round-trips. Edge creases round-trip as tags. A native Subdivision Surface modifier after the stage displays correctly. The semi-sharp limitation warning appears.
- **A7, persistence.** Undo and redo are consistent. Save and reload keep keyframes and bindings. Renaming a parameter in GH keeps its keyframes.
- **A8, GH lifecycle.** GH closed shows `disconnected` and keeps the last good output. Reopening GH resyncs with no manual step.
- **A9, snappiness.** Measured against the §13 budgets and reported.
- **A10, duplicates.** Duplicated stacks and stages get independent keys and helpers, with no cross-talk.

---

## 17. Risks

| Risk | Mitigation |
|---|---|
| C# WebSocket or solve trigger misbehaves under wine | S1 gate. Portal transport as fallback. The engine does not change |
| GH solve time dominates latency | Measure per definition. Report numbers, not promises |
| UI stalls on large applies | Binary path, in-place writes, budgets. One-frame hitch accepted |
| Keyframe decorators or array keys behave differently in the GUI | S5 before the parameter panel is built |
| SubD tag setter missing | S4. Warn and drop creases on Blender → GH |
| Semi-sharp and vertex creases lost | Accepted (D2). Warned on every SubD stage. Revisit if RhinoCommon exposes them |
| Mixed geometry does not display or propagate as expected | S6 before stage types are finalized |
| Curves need approximation | Approximate, count, and warn (D3) |
| Parameter renames and schema changes orphan keyframes | Keys use GUIDs. Orphan list and purge |
| Playback outruns GH | Latest-wins, cache-served playback, bake for exact frames |
| Int, bool, and choice keyframes interpolate between values | Constant interpolation. GH casts the value |
| Two GH documents claim one stage key | Conflict detection and error state |
| Port clash with Portal's server | Separate default port, configurable |
| Shared node groups after duplicate | Ownership check each tick. Covered by A10 |
| Helper deletion or manual edits in the GUI | Engine detects and rebuilds. Documented as unsupported |

---

## 18. Non-goals and later

- SubD semi-sharp creases and vertex creases, if RhinoCommon ever exposes them.
- GH → Blender values as drivers (v1.1): read-only outputs on the stack.
- Materials, layers, and object identity from GH.
- Multiple GH documents per stage key.
- A tested Windows-native Rhino pass (designed for now).
- Native B-rep, instancing.
- A Portal bridge, if ever needed.

---

## 19. Evidence log

Facts this plan relies on, and how each was checked. Scratch scripts live in `/tmp/opencode` until M0 moves them to `spikes/`.

**Blender 5.2.2 (headless, this machine)**
- Keyframes on a custom property interpolate. Drivers with a scripted expression drive a custom property.
- `action.fcurves` is gone. Use `action.layers[].strips[].channelbags[].fcurves`.
- Mesh crease attributes `crease_edge`, `crease_vert`, and `sharp_edge` exist. Subsurf works on the mesh. The GN Subdivision Surface node exists.
- `Object.evaluated_geometry()` returns `mesh`, `curves`, and `pointcloud` components. The evaluated mesh of a mesh object drops curve output.
- An Object Info reference to a legacy NURBS curve keeps `nurbs_weight`, `nurbs_order`, and `knots_mode` on the GN curve component. Weight and order read back correctly.
- Object Info in Original space keeps stack-local coordinates when the stack object is rotated and moved. Relative space does not.
- Modifier `persistent_uid` is stable across reorder and new on duplicate. A duplicate shares its node group.
- `stack.copy()` copies modifiers with the same UIDs and evaluates a prefix.
- Hidden helpers and helpers in an excluded collection still resolve through Object Info (checked by vertex count).
- GN PLY import caches by path. Rewriting the same path leaves stale geometry.
- Setting an object-typed modifier input from Python hung headless evaluation.
- Blender ships Python 3.14.7 with numpy.
- **S2 (2026-10-11):** `websockets` 17.2 runs on a plain background thread in Blender 5.2, with queue handoff; a 4 ms `bpy.app.timers` request holds ~5 ms median under a 2 MB stream load (max 73 ms). Two bounds: timers only fire once a script returns to the event loop, and `bpy.app.timers` is inert under `blender -b`, so layer B tests call the drain directly instead.
- **S1 build (2026-10-11):** the wine `csc.exe` is **Mono's mcs**, not Microsoft's compiler. `ClientWebSocket` exists in Mono's real `System.dll`, but referencing it makes mcs import two `System.dll`s and fail on ambiguous types, and `/nostdlib` is not honoured by this build. The spike therefore uses a hand-rolled client over `TcpClient` (`spikes/s1/miniws.cs`), which compiles with `/sdk:4.5` and runs. `DataContractJsonSerializer` works via `/r:System.ServiceModel.Web.dll` + `/r:System.Runtime.Serialization.dll`. M4 must revisit the client choice before the transport is settled.
- **S1 live (2026-10-11):** under wine-mono, a background thread's message triggers a UI-thread solve via `OnPingDocument().ScheduleSolution(delay, delegate)` + `ExpireSolution`, and `SolveInstance` runs for every message. Three behaviours to carry into the engine: `ScheduleSolution(0, …)` coalesces and silently drops the delegate (10 ms is reliable); the reply must be sent from a thread that is not blocked on the next receive, or the peer's request/response cycle deadlocks; and `IGH_DataAccess.GetData`/`SetData` match the parameter **name**, not the nickname. Evidence: `spikes/results/s1.txt`.

**Rhino 8 under wine (RhinoCommon and Grasshopper XML docs in the wine prefix, this machine)**
- `GH_DocumentObject.OnPingDocument`, `GH_Document.ScheduleSolution(int, GH_ScheduleDelegate)`, `IGH_DocumentObject.ExpireSolution`, `GH_ActiveObject.AddRuntimeMessage`.
- `RhinoApp.InvokeOnUiThread(Delegate, object[])`.
- `RhinoDoc.ModelUnitSystem`.
- `Mesh.Ngons`, `Mesh.VertexColors`, `Mesh.CreateFromBrep`.
- `NurbsCurve.MakePiecewiseBezier`, `NurbsCurve.ConvertSpanToBezier`, `Curve.ToNurbsCurve`.
- `SubD`, `SubD.CreateFromMesh`, `SubD.ToBrep`, `SubDEdge.Tag` with `SubDEdgeTag` values `Crease`, `Smooth`, `SmoothX`, `Unset`.
- No public semi-sharp edge value or vertex crease property was found.
- The `SubDEdge.Tag` setter is not confirmed (S4).

**Portal (upstream and local copies)**
- Upstream source is public under Apache-2.0: `Portal.Core` (including a WebSocket manager) and `Portal.Gh` (components `Remote` with WebSocket client and server, `Local`, `Serialization`, `Utils`).
- Wire format: `pk` magic, flags, CRC16, size, then optional gzip of JSON `{Items, Meta}`.
- Local Portal.blender 0.3.0 defines curve type enums but has no curve serialization code. Checked case-insensitively.
- Installed `Portal.Core` is 0.5.2, matching the compatibility table for Portal.blender 0.3.0.

**Benchmarks and transport** (§13)

**The a shared network folder folder is a CIFS mount** (`a shared folder`). It is not local storage. That is why the repository lives on local disk (D5).

**Earlier findings superseded by this plan**
- rhino3dm: no longer used, since all Rhino work happens on the GH side. Its binary does contain SubD sharpness symbols, which remains a possible fallback, but D2 rules it out.
- File protocol, `E<k>_<slug>` naming, the Base/Result split, the custom stack list, the GN Bake stage, per-piece `link.id`, and curves-as-loose-edges are all dropped.

---

## 20. Next step

M0 is done. Next: S1, the gate for the whole transport decision. S1 needs a live Grasshopper session under wine: its save-and-reopen check is a GUI step the operator confirms.
