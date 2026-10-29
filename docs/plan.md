# Stageloom — Project Plan (rev 2)

Stageloom is the new name for GH Stack. The wire protocol is unchanged.

**Status:** rev 2 approved. M0 done (2026-10-10): repository skeleton, this plan, protocol draft (`docs/protocol.md`), decision records (`docs/decisions/`), spike scripts (`spikes/`). **S2 passed 2026-10-11** (transport on a background thread, `spikes/results/s2-gui.txt`, `s2-background.txt`): `websockets` 17.2 runs on a plain thread in Blender 5.2, queue handoff works, and a 4 ms `bpy.app.timers` drain holds ~5 ms median under a 2 MB load. **S1 passed 2026-10-11** (the transport gate, `spikes/results/s1.txt`): a C# `.gha` under wine connects, a background message triggers `SolveInstance` on the UI thread through `ScheduleSolution`, replies come back in 20–65 ms, and save/reopen reconnects with no dialog. The plan's `ClientWebSocket` choice was replaced by a TcpClient client (see §19). **M2 done 2026-10-11**: protocol core, ten golden fixtures verified by both codecs. **S3 passed 2026-10-11** (`spikes/results/s3.txt`): mesh round trips match exactly at 1k/10k/100k; 100k costs 30.3 ms end to end, inside the §13 budget. **S4 passed 2026-10-11** (`spikes/results/s4.txt`): the SubD tag setter works, cage SubDs are valid, curve conversion is lossless except when weights are dropped. **Demo v0 works** (`DEMO.md`): a mesh in Blender driven through Stage In/Out and a Param in Grasshopper. **S5 passed 2026-10-12**: the parameter UI (keyframes, decorators, arrays, value-list dropdown) is confirmed in the GUI. **v0.1.0 published on GitHub 2026-10-12** (ADR 0007). **S6 conversions passed 2026-10-12** (`spikes/results/s6.txt`): curves, points, SubD cages with tags, and Breps round-trip through the product conversions inside Rhino. Curves became exact by construction — each piecewise-Bezier span is a single rational Bezier Blender holds exactly — after the live run exposed a homogeneous-vs-Euclidean coordinate mix-up that distorted every rational curve. **S8 passed 2026-10-12**: the add-on ships as a Blender extension with the official pure-Python `websockets` wheel. **Prefix evaluation done 2026-10-12**: a stage sees everything before its own modifier (`GH → GN → GH` chains), a waiting stage pauses the ones after it, and layer B covers the chain (54 headless tests); reconnect resends (§8.8) and `unknown_ref` full resends (§4.3) are implemented. **W2 done 2026-10-12** (ADR 0008): the simulation-style frame cache replaced the bake operator — frames fill as the user plays/scrubs, live on disk under the data root, reset per stage on a Grasshopper-side change, schema change, document change or Push, replay with Grasshopper closed, and idle passes write nothing so Cycles accumulates (60 core + 69 headless tests). **v0.3.1 published (2026-10-21):** the post-0.3.0 review batches (review §§16–18) and the Phase 0 registration diagnostics are in; on FiraTower a dirty multi-stage pass is ~46 ms (was ~115 ms) and an idle pass ~1.5 ms. Phase 7 live acceptance, the S6 display half, S7 and acceptance A1–A10 all passed. Next: the 0.4.0 GitHub release, the instance-latency re-measure and the old-component migration. **v0.4.0 published (2026-10-29):** the full Stageloom rebrand and the GitHub release; the first extensions.blender.org upload is deferred.
**Authoritative copy:** `docs/plan.md` in the repository.
**Date:** 2026-10-10.
**Replaces:** `handoff-1-grasshopper-3dm-plugin.md` and `handoff-2-blender-3dm-addon.md` (deleted; their useful facts are in §19).
**Rev 2 change:** the repository lives on the local machine (D5). No cross-machine working.

**Machines (working model)**
- **One Linux machine** runs everything in the build loop: code, git, Blender 5.2.2 (Python 3.14.7), the Rhino 8 wine prefix, `csc.exe`, GH plugin builds, tests, benches.
- Rhino and Grasshopper run under wine on the same machine. No other machine is involved.

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

Drive Grasshopper definitions from Blender as stackable modifiers, alongside native Geometry Nodes modifiers, in one visible object per stack. Both geometry and parameters flow through the stack. Parameters are Blender properties, so they can be keyframed, driven by drivers, and baked by Blender's own tools. The result must feel immediate: edits, keyframes, and playback should be snappy and seamless.

---

## 2. Scope

**In v1**
- Meshes with vertex colors and n-gons.
- SubD as a control cage with tag-based edge creases (D2).
- Curves: poly, Bezier, and NURBS, with approximation and warnings where needed (D3).
- Points.
- Brep, converted to mesh on the GH side (lossy by nature; warned).
- Attributes: Blender's named attributes (every type, mesh POINT/EDGE/FACE/CORNER
  domains, SubD cages, point clouds) travel with the geometry in both directions.
  Curves components carry `CURVE` and `POINT` (curve-point) attributes from
  Blender to Grasshopper; returning curve attributes to Blender is still out of
  scope because the stage helper is a legacy Curve datablock.
- Parameters: float, int, bool, vector3, color, value list. Text parameters are supported but not animatable.
- Instances: Blender's instanced geometry crosses as Grasshopper block definitions and instances, nested both ways.
- Mid-stack GH stages (`GH → GN → GH`), enable/disable, reorder, Pull, Freeze, Key all and orphan purge. A stage's modifier visibility is its preview; there is no separate Preview operator.
- Keyframes, drivers, and playback from the frame cache.
- Undo and redo, save and reload.
- Units in meters on the wire.

**Out of v1**
- SubD semi-sharp creases and vertex creases (D2).
- GH → Blender values as drivers. Planned for v1.1 as read-only outputs.
- Materials, layers, and named object identity from GH.
- Multiple GH documents answering one stage key.
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
| `stageloom` add-on | Blender | UI, stack model, engine, WebSocket server, geometry conversion, parameter properties, frame cache |
| `Stageloom.gha` | Grasshopper | Stage In, Stage Out, Param components; WebSocket client; schema publication |
| Protocol | both | Versioned binary-plus-JSON messages (§8) |
| Data root | this machine | `~/.local/share/gh-stack/`: frame cache and logs |

Portal (`Portal.Gh` 0.5.2 and the patched Portal.blender 0.3.0) stays installed side by side. It uses its own port and shares no code or state with this project.

---

## 5. Blender data model

| Thing | Visible | Contents |
|---|---|---|
| Stack object | Yes | Base geometry, native modifiers, `gh_stack_id` (uuid), stage records (key, label, state, current request ID, applied output hash, last error, timings, frozen flag), and the parameter custom props. |
| GH stage modifier | Yes, in the modifier panel | Geometry Nodes modifier built from the "Stageloom Stage" template. Named by its label, e.g. `Stageloom · Panelize`. |
| Stage node group | No | One per stage, owned by that modifier. Named `SL <key>`. Custom props: `gh_stage_key`, `gh_stack_id`. Nodes: one Object Info per geometry type → Join Geometry → Group Output. The group input is ignored. |
| Helpers | No | Hidden objects in `Stageloom Stage Data`, owned by the dedicated **Stageloom Stage** scene (never linked into the user's scene, so the outliner stays theirs). Created on first use, one per geometry type per stage (mesh, curves, points). Identity transform, data in stack-local space. Named `SL <stack>.<key>.<kind>`. |
| Parameters | Yes, on the stack object | Custom props `gh/<stage_key>/<param_id>` with UI ranges and defaults. |
| Cache | Disk and memory | Content-addressed responses, keyed by request ID. |

**Rules**
- **Stage keys** are `<slug>-<6 hex>`, fixed at creation. Labels can change; keys never do. Keys are what you paste into GH.
- **Parameter keys use a GUID** stored on the GH Param component, not the display name. Renaming a parameter in GH keeps its keyframes.
- **Each stage owns its node group.** Duplicating a modifier shares the group in Blender. On each engine tick, the add-on detects a stage key shared with another object (a duplicated stack) and gives the copy its own stack namespace, stage key, group and helpers; its parameter values, keyframes and drivers move with the key, and it shows as unbound until it is wired in GH.
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
| Arc, circle, NURBS with clamped, uniform, or Bezier knots | NURBS spline with weights, order, and knots mode | Exact | Weights, order, and knots mode survive a GN object reference (verified). Uniform-open, clamped-uniform and uniform-periodic NURBS travel as one wire NURBS (same order, knot mode, cyclic flag, weights). |
| NURBS with interior non-uniform knots | Single-span rational Beziers (`NurbsCurve.MakePiecewiseBezier`, one wire curve per span) | Exact | Weights kept; spans are single rational Beziers Blender holds exactly. A rational circle keeps its span boundaries this way. |
| Bezier (Blender) → GH | NurbsCurve, degree 3 | Exact | |
| Point | Point cloud | Exact | |
| Brep | Mesh (`Mesh.CreateFromBrep`) | Lossy by nature | Blender has no B-rep. Warned once per stage. |

**SubD crease rules (D2)**
- *GH → Blender:* an edge with tag `Crease` gets `crease_edge = 1.0`. Tags `Smooth`, `SmoothX`, and `Unset` get `0.0`. Vertex sharpness is not transferred.
- *Blender → GH:* edges with `crease_edge >= 0.5` get the `Crease` tag. Others get `Smooth`. Fractional values are rounded to the nearest tag, and a warning reports how many edges were affected.
- *Setter risk:* resolved by S4 — `SubDEdge.Tag` has a working setter. Note that boundary edges of a cage read `Crease` by default, so the transfer compares tags instead of assuming `Smooth`.
- *Warning text:* every stage with SubD output reports "creases transferred as tags; semi-sharp and vertex creases are not available from RhinoCommon".

**Curve approximation policy (D3)**
- A curve is transferred exactly whenever Blender's NURBS modes can hold it:
  uniform-open, clamped-uniform (endpoint) and uniform-periodic knots, any
  order 2–6, weights included. Those travel as one wire NURBS, exactly as
  Blender sent them, so a cyclic spline stays one cyclic spline.
- Everything else is carried by the span split when the spans allow it: every
  span is a single rational Bezier, and Blender holds a single span exactly
  (any order 2–6, weights included). This covers poly, Bezier, NURBS,
  circles, ellipses, and arcs of any angle.
- Only a curve both routes cannot carry (degree above 5, interior knots
  Blender cannot express, degenerate weights) is fitted with non-rational
  cubic Beziers (`BezierCurve.CreateCubicBeziers`) inside a 0.1 mm budget.
  Every fitted curve is counted in the stage status, and a warning reports the
  measured deviation when it exceeds the budget.
- Rational coordinates: Rhino stores homogeneous control points, the wire
  carries Euclidean + weight. Both directions convert explicitly (this mix-up
  distorted every rational curve in the first live S6 run).
- The engine samples the deviation between the original and the approximation
  (the S4 checks carry the recipe).
- Approximation never blocks the transfer. The warning is the signal.

**Mixed outputs.** A stage can emit any combination. Blender keeps them as geometry-set components. Native mesh-only modifiers such as Bevel and Subsurf act on the mesh component only. This is standard Blender behavior and is checked in spike S6.

**Attributes.** Blender's named attributes travel with the geometry in both
directions. The wire carries one section plus one descriptor per attribute
(protocol §5.1); Rhino attaches each attribute to the geometry object's
UserDictionary, so it rides along with duplication and transforms the way it
does in Blender's dependency graph. Mesh and SubD cage attributes cover
POINT/EDGE/FACE/CORNER, point clouds cover POINT; the active colour attribute and
edge creases keep their native channels instead (vertex colours, SubD tags). In a
definition, `Attribute Get` and `Attribute Set` read and write the values. Curve
attributes are read-only: Blender's curve helper is a legacy Curve datablock,
which has no attribute API to write back into.

**Stack type.** The stack object's base type sets what the native modifiers act on. GH curves and points ride along as components. Stacks whose base is empty are allowed. GH can supply everything.

---

## 7. Parameters and animation

**GH side.** A `Param` component declares a parameter:
- A GUID, a label, and a type: float, int, bool, vector3, color, value list, or text.
- A default, a range, and a step.
- A group label.

It outputs the value Blender sends when connected, or the default otherwise.

**Blender side.** The schema becomes custom props on the stack object, named `gh/<stage_key>/<param_id>`:

| GH type | Blender representation | Animatable | Notes |
|---|---|:--|---|
| Float | Float property | Yes | |
| Int | Int-valued float property | Yes, constant interpolation | Cast to int on the GH side. |
| Bool | 0/1 property | Yes, constant interpolation | |
| Vector3 | Float array of 3, subtype XYZ | Yes, per component | |
| Color | Float array of 3, subtype COLOR | Yes, per component | |
| Value list | Int index plus a stored list of entries | Yes, constant interpolation | Dropdown drawn in the add-on panel; the index is what travels. |
| Text | String property | No | Documented limitation. |

**Verified:**
- Keyframes on a custom property interpolate (1.0 → 5.0 across frames 1–10 gives 2.668 at frame 5).
- A driver with a scripted expression on a custom property drives it (`frame * 0.5` gives 2.0 at frame 4).
- Ranges are set through `id_properties_ui`.
- In Blender 5.2, `action.fcurves` no longer exists. Access is through `action.layers[].strips[].channelbags[].fcurves`. Any code touching keyframe interpolation must use that path.

**Parameter UI.** A "GH Parameters" panel per stage shows the widgets, with a "Key all" button that keys every parameter at the current frame (int, bool and value-list keys are constant-interpolated; text is not animatable). Single parameters are keyed and cleared through Blender's own decorators (the keyframe diamond, the context menu, the Dope Sheet). Keyframe and driver decorators are verified in spike S5. Parameters removed from GH move to an orphan list, so their keyframes are not dropped silently. Orphans can be purged explicitly.

**Frame cache.** The cache behaves like Geometry Nodes simulation caches: it fills as the user plays and scrubs, with no bake setup and no frame range fields. The content-addressed response store *is* the frame cache — a keyframed parameter produces a different request ID per frame, so frames are keyed by their content, not by frame number. Frames are written to the disk store as they arrive (raw protocol frame plus a generation/fingerprint sidecar) and replay in any order. A per-stage generation resets the stage's frames when Grasshopper sends something that makes them describe the wrong definition: a slider upstream of Stage Out (the same request ID answered with a different output hash), a schema change, a reconnecting document, or a manual Push. A re-opened session reuses frames whose generation and definition fingerprint still match (ADR 0008). No bake operator exists.

**Playback without a warm cache.** Frames that are not cached show last-good geometry; the state row reads "waiting". One request is in flight per stage (latest-wins): when the playhead moves while an answer is coming, the requested frames coalesce to wherever the playhead is when the answer arrives. A response for a request the scene has moved past is cached, never displayed. Cached frames replay with Grasshopper closed, so renders run from the cache.

---

## 8. Protocol

### 8.1 Transport
- Blender runs the server on `127.0.0.1:59130` (configurable), using the `websockets` package. Version 17.2 is pure Python (`py3-none-any`) and was tested on Python 3.14.7.
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
| `schema` | GH → Blender | `stage`, `params`: list of `{id, label, type, default, min, max, step, values, group}` |
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
1. Evaluate the stack up to this stage on a clone: `stack.copy()`, remove the later modifiers, then read `evaluated_geometry()` for each component type. Always the clone, even at the head of the stack: the base datablocks are stale while Edit Mode is open and blind to shape keys, and a private copy of the flushed datablock is what the depsgraph will evaluate in Edit Mode.
2. Read the stage's current parameter values from the custom props.
3. Compute the request ID.
4. If the cache holds the request ID, apply it.
5. Otherwise send `request` (full or ref) and **stop**. Later stages would run on stale input.

A stage's prefix never includes its own parameters, so a parameter change cannot feed back into its own request.

### 9.2 States
`idle` · `waiting` · `ok` · `error` · `disconnected` · `disabled` (no requests; downstream receives input directly) · `frozen` (no requests; the helper keeps its last output) · `unbound` (no GH document has answered yet; also the state a rebuilt stage carries until an answer arrives).

`disabled` is derived, not stored: a stage is active while its modifier is visible in any context — viewport, render, or edit mode — so all three toggles off is the off switch, and the add-on never writes a toggle back (plan §11). `frozen` is its own flag: it stops requests *and* cache replay, and Push skips frozen stages entirely.

### 9.3 Latest-wins
- One request is in flight per stage. A new request supersedes the previous one.
- Stale responses are cached, not discarded.

### 9.4 Triggers
- Depsgraph updates, coalesced to one pass per tick. The timer drains the depsgraph before a pass that has work, so an update that is already tagged is delivered to the handler before a prefix clone can mute it; a GUI tick with nothing to process does not force an evaluation (background sessions, where the tests drive the timer directly, keep the drain every tick). The active Edit Mode datablock is flushed in the same breath. Any tagged update retires every memoized prefix (one global generation; see §9.6), and a reconnect or file load verifies each stage against Grasshopper once even when a cached frame answers. A pass whose stage has a young request outstanding returns before reading the prefix; past the soft retry (1 s) the current state supersedes the stale request, and the hard timeout resends in full.
- Frame change, which also reaches parameters through the animation system.
- Incoming messages, drained from a queue by a main-thread timer: every 5 ms while any stage is waiting, every 20 ms otherwise. **Verified in S2:** a 4 ms timer request holds ~5 ms median in the GUI, including under a 2 MB load, so the 5 ms target is reachable. Two findings bound the design: timers only fire after a script returns to the event loop (they are inert during a blocking main-thread script), and `bpy.app.timers` does not tick at all in `blender -b`. Layer B tests therefore call the same drain function directly instead of waiting on timers.
- Operators.

### 9.5 Timeouts and failures
- Default timeout: 30 s, configurable.
- A timeout or GH error sets `error`, keeps the last good output, and shows the message.
- The engine retries on the next change or on Push. It does not loop on failures.

### 9.6 Loop guard and ownership
- Engine writes are tagged and ignored by the depsgraph handler while the prefix clone evaluates (`_evaluating`); the timer drains pending updates first, so the mute can only eat the engine's own events.
- A stage's input is always upstream of its output, so the engine cannot form cycles.
- A stage key shared with another object (a duplicated stack) is split on each pass: the copy gets its own stack namespace, key, group and helpers, and its parameter properties move with the key.
- The prefix memo is keyed by one global depsgraph generation, not by a list of the datablocks a prefix reads: Python cannot enumerate a node tree's dependencies, and a missing entry was silent stale geometry. Tagged is tagged; a false alarm costs one re-evaluation.

### 9.7 Undo, save, reload
- Stage records, helper data, and node groups live in the `.blend`.
- The disk cache is content-addressed, so restoring an earlier state is a cache hit.
- On load, stages are re-bound by key. Missing helpers or node groups are rebuilt and marked `unbound`; a missing *modifier* is not repaired — deleting it removes the stage (§9.8).
- The stage's modifier is linked by its `persistent_uid`, which survives a deleted or swapped node group; the group-name link stays as the fallback for records saved before the uid and for duplicated objects.
- The engine's timer and handlers are persistent (Blender clears non-persistent ones on every file load), and a load resets mid-request stages, marks every stack for evaluation, and verifies each stage against Grasshopper once — cache or not — so a reopened file talks to Grasshopper without an edit and a document that changed while the file was closed cannot replay from the frame cache.

### 9.8 Operators
Add Stage, Push, Pull, Freeze, Key all, Purge orphans. All are undo-safe. (Bake range is removed: the frame cache fills during playback, ADR 0008.)

- **Add Stage** creates a node group from the template, assigns a new key, creates the helpers on first use, and inserts the modifier after the active one.
- **Remove Stage** is native: deleting the stage's modifier removes the stage, its helpers, its block definitions and its parameter properties. The node group is removed when it has no users. Cache entries are kept.
- **Push** forces a fresh request for every live stage and resets the stack's frame cache. Frozen stages are skipped entirely.
- **Pull** asks Grasshopper for one stage's output again without resetting the frame cache; it is also how a frozen stage is refreshed once, on demand.
- **Freeze** stops requests and cache replay; the helpers keep their last output.
- **Key all** keys every parameter of a stage at the current frame (see §7).
- **Purge orphans** deletes parameter properties Grasshopper no longer declares, with their keyframes; until then they stay in the orphan list (protocol §4.2).

Enable, disable and reorder are the modifier's own controls: the viewport, render and edit-mode toggles are the switch (a stage is active while visible in any context), and the add-on never writes them back after creation.

---

## 10. Grasshopper plugin

**Name:** `Stageloom.gha`. Tab: "Stageloom". Install path: `…/rhino8/drive_c/users/miquel/AppData/Roaming/Grasshopper/Libraries/Stageloom/`.

**Components**

| Component | Inputs | Outputs |
|---|---|---|
| Stage In | Key, Root (ignored in v2, reserved), Enabled | Geometry (generic list: mesh, SubD cage, curves, point cloud), Request ID, Status, Message |
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
blender/stageloom/
  __init__.py
  blender_manifest.toml          extension manifest
  core/                          pure Python, no bpy
    codec.py                     frame encode and decode, sections
    hashing.py                   geometry hash, request IDs
    cache.py                     cache index and eviction
    params.py                    schema types and value casting
  blender/
    server.py                    WebSocket server on a background thread
    bridge.py                    queues between the server thread and the main thread
    stack.py                     prefix evaluation on clones
    geometry_io.py               numpy in and out, in-place writes
    nodes.py                     templates, ownership, Join and Object Info wiring
    helpers.py                   helper objects, definitions, row clouds (Stageloom Stage scene)
    props.py                     parameter custom props and UI ranges
    engine/                      tick loop, states, triggers, guard, frame cache
      __init__.py                facade: state, registration, re-exports
      triggers.py                handlers, timer, depsgraph/edit-mode triggers
      messages.py                schema, response, error and hello handling
      request.py                 prefix evaluation, request IDs, sends
      apply.py                   response sections -> hidden helpers
      cache_ops.py               hot cache and disk-backed response store
      lifecycle.py               stage/stack index and lifecycle entry points
      diagnostics.py             trace, heartbeat, timings, wait ages
    ops.py, ui.py, prefs.py
  vendor/websockets/             pure-Python wheel
blender/tests/
```

**Packaging.** A Blender extension (`blender/stageloom/blender_manifest.toml`). `websockets` ships as the official pure-Python wheel (`wheels/websockets-17.2-py3-none-any.whl`), so one zip covers Linux and Windows; Blender installs it into the extension's `_libs`. `scripts/build-extension` stages the source without `vendor/` and builds `dist/stageloom-<version>.zip`; `scripts/install-extension` installs and enables it; `scripts/test-extension` proves the installed extension round-trips. Development installs (`scripts/install-addon`) have no wheel and fall back to the vendored copy under `vendor/` on `sys.path`, as Portal does.

**Preferences**
- Server port (default 59130; 9130 through 0.2.2, where BlenderKit sits).
- Timeout (default 30 s).
- Curve deviation warning threshold (default 0.1 mm).
- Cache size limit.
- Data root (default `~/.local/share/gh-stack/`).
- Log level.

**UI**
- Modifier panel: native and authoritative for lifecycle. GH stages appear as normal modifiers: the viewport, render and edit-mode toggles enable and disable the stage, drag reorders it, and X removes the stage with its helpers and parameters.
- N-panel "Stageloom": per-stage rows showing label, copyable key, state, warnings, and errors, with Pull and Freeze buttons; Add Stage and Push at the stack. The Diagnostics child panel lists orphaned parameters and purges them.
- N-panel "GH Parameters": one section per stage, with widgets, keyframe decorators, and Key all.
- Status bar or header indicator for connection: listening, connected (with GH document name), or error.

---

## 12. Repository and environments

**Repository:** `gh-blender-stack`, GPL-3.0. Created in M0; published on GitHub 2026-10-12 (ADR 0007).

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
8. Uncached frames never block playback; the cache fills as frames arrive and is bounded by the cache size limit.
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

**Budgets to validate in S3 (targets, not guarantees)** — **validated 2026-10-11**, see `spikes/results/s3.txt`:
- Parameter change to viewport, light definitions: GH solve plus about 1 ms of transport.
- Geometry hop at 100k triangles: about 100 ms of transport and conversion on top of the GH solve. **Measured 30.3 ms** for the full round trip (1.61 MB each way; C# 10.0 ms in, 5.1 ms out; Blender read+encode 7.7 ms, in-place apply ~22 ms).
- Cached playback: at least 24 fps for 10k-triangle outputs. 100k triangles is expected to be borderline.
- No UI tick above 16 ms during steady updates, except one large apply.

GH solve time will dominate real definitions. The transport is not the bottleneck.

---

## 14. Testing

- **Layer A, pure core.** Codec round trips, hashing, request-ID stability, rule decisions, latest-wins, stale responses, cache behavior. Standard-library `unittest`.
- **Layer B, Blender headless.** `blender -b --factory-startup --python-exit-code 1`. Templates and ownership, in-place updates, prefix evaluation, parameters through keyframes and drivers, the frame cache (fill, replay, resets, eviction), undo and redo, save and reload, disconnect and reconnect. Runs against `tools/fake_gh`. Note: `bpy.app.timers` is inert under `-b` (S2), so layer B drives the queue drain directly rather than through the timer the GUI uses.
- **Layer C, GH.** Codec and conversion tests under wine on Linux. Live tests: one round trip, parameter changes, save and reopen, GH closed and reopened.
- **Layer D, end-to-end and bench.** Real definitions and a GN chain. Timings at 1k, 10k, and 100k triangles per hop.
- **Golden frames** in `protocol/fixtures/`. Both sides must encode and decode them identically.
- **Acceptance** (§16) is the release gate.

---

## 15. Milestones and spikes

**Spikes (M1)**

| Spike | Question | Gate |
|---|---|---|
| **S1** | Does a C# `Stageloom.gha` under wine connect with `ClientWebSocket`, trigger a solve from a background message via `ScheduleSolution`, send a response to Blender, and save and reopen the document with no dialog? | Yes: continue. No: fall back to Portal's transport (its WebSocket components exist upstream), and the engine stays the same. **Passed 2026-10-11 with a TcpClient client instead of `ClientWebSocket`** (Mono cannot reference it; see §19). Connect, background-message solve, reply in 20–65 ms, and save/reopen reconnected with no dialog: `spikes/results/s1.txt` |
| **S2** | Does `websockets` run in a Blender 5.2 background thread, with queue handoff and no UI stalls? | Yes: continue. |
| **S3** | Geometry round trip with the C# side at 10k and 100k triangles. | Budgets in §13 met or revised with data. **Passed 2026-10-11**: geometry matches at 1k/10k/100k; the 100k round trip is 30.3 ms (C# 10.0 + 5.1 ms, 1.61 MB each way). `spikes/results/s3.txt` |
| **S4** | SubD: can `SubDEdge.Tag` be set? Does `SubD.CreateFromMesh` produce a usable SubD from a cage? Are the curve approximation paths behaving? | Setter exists: tags round-trip. No setter: tags are dropped on Blender → GH with a warning (D2). **Passed 2026-10-11**: setter works, cage SubD is valid, `MakePiecewiseBezier` is lossless; the loss is dropping weights (0.73 units at radius 5). `spikes/results/s4.txt` |
| **S5** | Parameter UI in the GUI: keyframe and driver decorators on custom props, vector and color arrays per component, value-list dropdown. | Decorators and arrays work, or the UI is adjusted. **Passed 2026-10-12**: keyframes animate the GH output live, decorators sit on Blender's split rows, per-component vector keys and drivers are covered by `blender/tests/headless/test_param_ui.py`, and the value-list dropdown works end to end (index on the wire). |
| **S6** | Mixed output (mesh plus curves) through a stage on a mesh stack: display, native modifiers, and what `evaluated_geometry()` returns downstream. | Behaves as §6 says, or the mixed-output rules are revised. **Conversions passed 2026-10-12** (`spikes/results/s6.txt`): curves, points, SubD cages with tag creases, and Breps all round-trip through the product code inside Rhino. The live run drove a rewrite: curves are exact by construction (one wire curve per piecewise-Bezier span, weights kept), the homogeneous-vs-Euclidean coordinate mix-up was fixed, and all curve goos dispatch. **Update 2026-10-18:** cyclic uniform NURBS curves are exact too, and travel as one NURBS instead of a span split; the S6 checks gained cyclic cases (CHANGELOG 0.2.2). **Display half passed 2026-10-20**: mixed output through a stage with native modifiers and `evaluated_geometry()` downstream, plus the headless coverage in `blender/tests/headless/test_geometry_kinds.py`. |
| **S7** | Helpers in the GUI: Object Info in Original space for mesh, curve, and point helpers. Duplicate and reload ownership. | **Passed 2026-10-20**: the helpers display in Original space for mesh, curve and point, and duplicated/reloaded stages keep their own ownership. |
| **S8** | Extension install on 5.2 with the vendored `websockets` wheel. | Installs. Fallback is `vendor/`. **Passed 2026-10-12**: `blender_manifest.toml` with the official pure-Python wheel; `scripts/test-extension` installs into a throwaway config and completes a `fake_gh` round trip from the installed extension (websockets imported from the wheel's site-packages). |

**Milestones**

| | What | Exit criteria |
|---|---|---|
| M0 | Create `~/workspace/gh-blender-stack`. Move this plan into `docs/plan.md`. Write `docs/protocol.md` draft and ADRs for D1–D5. Copy the probe scripts from `/tmp/opencode` into `spikes/`. | Repo exists. Pure-core tests run (empty) |
| M1 | Spikes S1–S8 | All gates passed, or the fallback is chosen and recorded. **S1–S5 passed** |
| M2 | Protocol v2 core, golden frames, rules | Layer A green. **Done 2026-10-11**: codec, hashing, params, rules, cache (43 tests), ten golden fixtures verified by both the Python codec (`scripts/test-core`, 50 tests) and the C# codec under wine (`scripts/test-gha`) |
| M3 | Engine against `tools/fake_gh` | Layer B green, including the frame cache, undo, save and reload |
| M4 | GH plugin: Stage In, Stage Out, Param | Layer C green: live round trip, parameter change, save and reopen, GH closed and reopened |
| M5 | End-to-end with real definitions | Acceptance A1–A10 pass. Latency reported. **Done 2026-10-20**: all ten passed; latency minimal, with the earlier measured figures still standing |
| M6 | UI: stack panel, parameter panel, add stage, status, preferences, Pull, Freeze, Key all, orphan purge | Manual checklist on this machine. **Done 2026-10-20** (S6/S7 GUI pass included) |
| M7 | Hardening: large meshes, multiple stacks, GH and Blender restarts, extension build, docs | Release notes. Optional Windows-native pass. **0.3.0 ready 2026-10-20**: extension and `.gha` built and verified; Windows-native pass still out |

---

## 16. Acceptance

- **A1, keyframes.** Keyframe a GH float parameter in the Dope Sheet. Playback drives the GH output per frame. After the cache is warm (played or scrubbed once), playback is served from the cache.
- **A2, drivers.** A driver on a custom prop, driven by an Empty's distance, moves the GH output live.
- **A3, chain.** Mesh → GH A → GN (bevel, array) → GH C → native GN. Editing the base updates the whole chain with no manual steps.
- **A4, stack edits.** Reorder, disable, and enable create no datablocks. Restored states are cache hits.
- **A5, curves.** Representable curves round-trip exactly. Approximated curves are reported with counts and deviations.
- **A6, SubD.** The cage round-trips. Edge creases round-trip as tags. A native Subdivision Surface modifier after the stage displays correctly. The semi-sharp limitation warning appears.
- **A7, persistence.** Undo and redo are consistent. Save and reload keep keyframes and bindings. Renaming a parameter in GH keeps its keyframes.
- **A8, GH lifecycle.** GH closed shows `disconnected` and keeps the last good output. Reopening GH resyncs with no manual step.
- **A9, snappiness.** Measured against the §13 budgets and reported.
- **A10, duplicates.** Duplicated stacks and stages get independent keys and helpers, with no cross-talk. Covered headless by `blender/tests/headless/test_duplicates.py` (key, namespace, group, helpers, parameter values/keyframes/drivers, load split); the live check passed 2026-10-20.

**Acceptance passed 2026-10-20.** All ten checks ran on this machine. A9 reported
minimal latency and no budget regression; the measured baseline remains 30.3 ms
for a 100k-triangle round trip and ~10% engine main-thread share on the 60-edit
drag. Instance-heavy definitions remain the one accepted performance limitation
(CHANGELOG known limitations).

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
| Playback outruns GH | Latest-wins, cache-served playback; frames replay from the content-addressed cache |
| Int, bool, and value-list keyframes interpolate between values | Constant interpolation. GH casts the value |
| Two GH documents claim one stage key | Conflict detection and error state |
| Port clash with Portal's server | Separate default port, configurable |
| Shared node groups after duplicate | Ownership check each tick. Covered by A10 |
| Helper deletion or manual edits in the GUI | Engine detects and rebuilds. Documented as unsupported |

---

## 18. Non-goals and later

- SubD semi-sharp creases and vertex creases, if RhinoCommon ever exposes them.
- GH → Blender values as drivers read-only outputs on the stack.
- Materials, layers, and object identity from GH.
- Multiple GH documents per stage key.
- A tested Windows-native Rhino pass (designed for now).
- Native B-rep.
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
- Helpers, definitions and row clouds owned by a separate `Stageloom Stage` scene still resolve through Object Info from the user's scene: evaluation, instance references, a Cycles render (lit pixels counted) and save/reload all checked (2026-10-17).
- `sharp_face` and `UVMap` stay in the generic attribute path (measured 2026-10-17, 100k triangles): UVMap adds 1.61 MB per frame (1.61 → 3.22 MB) and ~0.9 ms (hash + encode + read: 8.2 → 9.1 ms against the 16 ms tick budget); `sharp_face` adds ~50 KB and ~0.02 ms. A triangulated 100k (301k loops) leaves ~2 ms tick headroom; the round-trip figure is extrapolated, not measured. Decision: keep both.
- GN PLY import caches by path. Rewriting the same path leaves stale geometry.
- Setting an object-typed modifier input from Python hung headless evaluation.
- **Instances and blocks (verified 2026-10-16/17).** Definitions are named graph-side `ModelInstanceDefinition`s (the Model Block Definition route): valid, previewing and baking with no document write at solve time. Nested placements are one `Instance on Points` per distinct child fed by placement clouds; Blender 5.2 cannot evaluate recursive node groups, so the chains are shared and static (`nodes.GROUP_FORMAT` 3). A `List<T>` never casts to `List<object>`: wire-parsed headers must be read through non-generic `IList` or kept in wire shape (two silent block drops). Blender does not draw instances that reference a viewport-hidden datablock: prototypes reach `Instance on Points` through Object Info + Join copies, not Collection Info references (A/B render: 0 vs 1613 lit pixels). A referenced collection's members are references too.
- **Live debugging recipes.** Open the autosave/`quit.blend`, enable the add-on and call `engine._prefix_sections(obj, record, stage)` to see exactly what Blender would send. Decode the newest cache file under `~/.local/share/gh-stack/cache/<stack_id>/<stage_key>/` with `codec.decode_frame` to see what Grasshopper answered. Push forces a fresh exchange; a tiny Cycles render with lit pixels counted proves drawing, not just evaluation.
- Blender ships Python 3.14.7 with numpy.
- **S2 (2026-10-11):** `websockets` 17.2 runs on a plain background thread in Blender 5.2, with queue handoff; a 4 ms `bpy.app.timers` request holds ~5 ms median under a 2 MB stream load (max 73 ms). Two bounds: timers only fire once a script returns to the event loop, and `bpy.app.timers` is inert under `blender -b`, so layer B tests call the drain directly instead.
- **S1 build (2026-10-11):** the wine `csc.exe` is **Mono's mcs**, not Microsoft's compiler. `ClientWebSocket` exists in Mono's real `System.dll`, but referencing it makes mcs import two `System.dll`s and fail on ambiguous types, and `/nostdlib` is not honoured by this build. The spike therefore uses a hand-rolled client over `TcpClient` (`spikes/s1/miniws.cs`), which compiles with `/sdk:4.5` and runs. `DataContractJsonSerializer` works via `/r:System.ServiceModel.Web.dll` + `/r:System.Runtime.Serialization.dll`. M4 must revisit the client choice before the transport is settled.
- **S1 live (2026-10-11):** under wine-mono, a background thread's message triggers a UI-thread solve via `OnPingDocument().ScheduleSolution(delay, delegate)` + `ExpireSolution`, and `SolveInstance` runs for every message. Three behaviours to carry into the engine: `ScheduleSolution(0, …)` coalesces and silently drops the delegate (10 ms is reliable); the reply must be sent from a thread that is not blocked on the next receive, or the peer's request/response cycle deadlocks; and `IGH_DataAccess.GetData`/`SetData` match the parameter **name**, not the nickname. Evidence: `spikes/results/s1.txt`.
- **S3 (2026-10-11):** a mesh round trip at 1k/10k/100k triangles matches exactly and costs 30.3 ms at 100k end to end (both conversions, both directions, 1.61 MB frames). The C# conversion adds vertices one at a time because RhinoCommon's `AddVertices` cannot be referenced with the profile mcs compiles against. WebSocket frames over 64 KiB need the 64-bit length path in both directions; on send, shift a `long` — C# masks `int` shift counts to five bits. Evidence: `spikes/results/s3.txt`.
- **S6 (2026-10-12):** the geometry-kind conversions pass live in Rhino (`spikes/results/s6.txt`). The first live run exposed and fixed two product bugs: `Stage Out` only matched `GH_Curve` (so Circle outputs, which are `GH_Circle`, were dropped), and Rhino's homogeneous control coordinates were mixed with the wire's Euclidean + weight, distorting every rational curve. The conversion is now exact by construction: `MakePiecewiseBezier` spans travel as single rational Beziers, one wire curve each, which Blender holds exactly. Validated locally with the real kernel: wire -> Rhino 1.8e-15, wire -> Blender 4.8e-07 on a radius-5 circle. Two facts for the code: `Mesh.CreateFromSubDControlNet` returns seam-duplicated control points (19 for a 16-point open cage) and vertex order is not stable across SubD round trips, so crease transfer maps by vertex pair; and `GH_Circle`/`GH_Arc`/`GH_Line`/`GH_Rectangle` have no `CastTo<Curve>` and fail `GH_Convert.ToCurve(Primary)`, so they need explicit unwrapping.

**Rhino 8 under wine (RhinoCommon and Grasshopper XML docs in the wine prefix, this machine)**
- `GH_DocumentObject.OnPingDocument`, `GH_Document.ScheduleSolution(int, GH_ScheduleDelegate)`, `IGH_DocumentObject.ExpireSolution`, `GH_ActiveObject.AddRuntimeMessage`.
- `RhinoApp.InvokeOnUiThread(Delegate, object[])`.
- `RhinoDoc.ModelUnitSystem`.
- `Mesh.Ngons`, `Mesh.VertexColors`, `Mesh.CreateFromBrep`.
- `NurbsCurve.MakePiecewiseBezier`, `NurbsCurve.ConvertSpanToBezier`, `Curve.ToNurbsCurve`.
- `SubD`, `SubD.CreateFromMesh`, `SubD.ToBrep`, `SubDEdge.Tag` with `SubDEdgeTag` values `Crease`, `Smooth`, `SmoothX`, `Unset`.
- No public semi-sharp edge value or vertex crease property was found.
- `SubDEdge.Tag` **has a working setter** (confirmed live by S4); `SubD.CreateFromMesh` yields a valid cage SubD; boundary edges read `Crease` by default.
- `NurbsCurve.MakePiecewiseBezier` is lossless (knot insertion). `ConvertSpanToBezier` returns a `BezierCurve` (no `Domain`; a span is 0..1). Dropping weights on a rational circle (radius 5) distorts it by 0.73 units; the wire therefore keeps the weights and sends one wire curve per span, each a single rational Bezier (S6).
- `NurbsCurve.Create(periodic, degree, points)` takes a **degree** (order - 1); passing an order builds a curve one degree too high. `NurbsCurvePointList.SetWeight` writes the **homogeneous W** slot (`ON_NurbsCurve_SetWeight`), which moves a control point to `location/w`; the wire's Euclidean + weight needs `MakeRational()` then `Points.SetPoint(i, location, weight)` (the `(x*w, y*w, z*w, w)` slot). `MakeRational` preserves periodicity. Verified 2026-10-18 with rhino3dm 8.35 and the C#/S6 checks.

**Portal (upstream and local copies)**
- Upstream source is public under Apache-2.0: `Portal.Core` (including a WebSocket manager) and `Portal.Gh` (components `Remote` with WebSocket client and server, `Local`, `Serialization`, `Utils`).
- Wire format: `pk` magic, flags, CRC16, size, then optional gzip of JSON `{Items, Meta}`.
- Local Portal.blender 0.3.0 defines curve type enums but has no curve serialization code. Checked case-insensitively.
- Installed `Portal.Core` is 0.5.2, matching the compatibility table for Portal.blender 0.3.0.

**Benchmarks and transport** (§13)

**A shared network folder was measured to be a CIFS mount**, not local storage. That is why the repository lives on local disk (D5).

**Earlier findings superseded by this plan**
- rhino3dm: no longer used, since all Rhino work happens on the GH side. Its binary does contain SubD sharpness symbols, which remains a possible fallback, but D2 rules it out.
- File protocol, `E<k>_<slug>` naming, the Base/Result split, the custom stack list, the GN Bake stage, per-piece `link.id`, and curves-as-loose-edges are all dropped.

---

## 20. Next step

v0.1.0 is published (ADR 0007); the S6 conversion checks and S8 packaging have
passed (`spikes/results/s6.txt`, `scripts/test-extension`), prefix evaluation is
implemented (`blender/tests/headless/test_prefix.py`), and the simulation-style
frame cache landed (ADR 0008, `blender/tests/headless/test_frame_cache.py`) with
the bake operator dropped. Attributes landed 2026-10-16: Blender's named
attributes travel with the geometry as `attr.*` sections (§5.1 of the protocol),
stage in and out carry them, `Attribute Get`/`Set` read and write them, and the
attribute store has console coverage. **S9 answered both of its questions**
(`spikes/results/s9.txt`): attributes ride on the geometry through Rhino's own
operations, and instances cross as named graph-side block definitions — valid,
previewing and baking without a document write at solve time, the route
Grasshopper's own Model Block Definition components use.
Instances as blocks landed 2026-10-17 (nested blocks both ways, helper data in
the dedicated `Stageloom Stage` scene), and W3/W4 landed 2026-10-18: the operators are
Add Stage, Push, Pull, Freeze, Key all and Purge orphans; the modifier panel is
the lifecycle (its toggles enable, drag reorders, X removes); the `frozen` and
`unbound` states, load-time plumbing repair and the parameter-orphan list are in
(§9.2, §9.7, §9.8). The live-sync rebuild landed 2026-10-19 (§9.4, §9.6), and
A10 landed with it: a duplicated stack is split onto its own key, cache
namespace, group and helpers, with its parameter animation moved along. The
live-sync Phase 7 acceptance passed 2026-10-19
(`docs/live-sync-report-2026-10-19.md` §5), and the S6 display half, S7 and
acceptance A1–A10 passed 2026-10-20. The post-0.3.0 review batches landed and
v0.3.1 is published (2026-10-21): the extension zip and `.gha` are built and
verified, and on FiraTower a dirty multi-stage pass is ~46 ms (was ~115 ms).
v0.4.0 is published (2026-10-29): the full Stageloom rebrand and the GitHub
release, followed by the instance-latency re-measure and the old-component
migration. The first extensions.blender.org upload is deferred.
