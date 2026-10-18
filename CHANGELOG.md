# Changelog

<!-- Release posts are assembled from the version's section below, so write it for
     the person upgrading: what changed since the previous version, in their terms,
     and nothing else. No release ordinals; no internal churn that landed and was
     replaced inside the same unreleased window; no inventories of deleted code;
     and the post ends where this version's section ends — older sections are not
     repeated. The commits carry the engineering record. -->
Notable changes to this project, following
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **Cyclic NURBS curves are exact in both directions.** The wire-side builder
  passed Blender's NURBS *order* to `NurbsCurve.Create`'s *degree* parameter, so
  every cyclic curve was built one degree too high (the reported FiraTower
  curve sat 262 mm off its Blender shape). The too-high degree then defeated the
  encoder's exact-span guard, so the curve came back as a cubic fit with a
  `curve_approximated` warning, and cyclic curves with weights silently lost
  their periodicity. Curves whose knots match Blender's NURBS modes —
  uniform-open, clamped-uniform, uniform-periodic — now travel as one wire NURBS
  with their order, knot mode and cyclic flag, and come back as one spline
  instead of a span split. No protocol change; the `.gha` needs a rebuild and a
  Rhino restart to pick it up.

## [0.2.1] — 2026-10-17

A hygiene patch: the add-on is extension-platform ready (online access,
permissions, licence, packaging), the plugin's threading and key registry are
made safe, and both codecs now agree on exactly what they accept.

### Added

- **Behavioural coverage for the plugin.** `GhStackLinkTests.exe` drives the
  real `GhLink` against a scripted fake server — hello content, stage and
  parameter publishing, slot bookkeeping, error answers, unowned-key pruning,
  teardown — and runs with layer C. The solve half still needs Rhino.
- **`unknown_stage` answers.** A request for a key no component owns is
  answered with the protocol's error instead of being left to the timeout, and
  the stage shows it.
- **Instance counts.** The response header's `counts` now carries
  `definitions` and `instances`, so a definition mismatch is visible without
  decoding sections.

### Fixed

- **The plugin's thread-safety cluster.** `Stop()` actually stops the socket
  thread and runs when the last component leaves; `slot.Params` is published as
  a snapshot (the torn read is gone); the heartbeat timestamp and the
  hello-per-connection flag are atomic; slots and stage parameters are bounded
  to keys the document's components own, reconciled at solve end. A SubD crease
  that cannot be mapped now warns, and every conversion warning carries a real
  code instead of a blanket `"conversion"`.
- **The decoders are total and symmetric.** The C# `Decode` no longer throws on
  a numeric section `name`, a string, negative or overflowing `bytes`, a
  non-array `sections`, or 1000-deep nesting; `NaN`/`Infinity` are rejected on
  both sides (Python accepts them by default); a header over the cap is
  `too_large` on both sides; the `v` boundary is pinned by console cases.
  `proto_mismatch` and `too_large` are actually sent now — an error frame
  precedes the drop — and `FormatDouble`'s claim matches its behaviour.
- **Cache eviction is linear.** The index found the oldest entry with a scan
  and re-summed the total per removal — quadratic in the structure every
  response touches. A lazy heap and a running total replace it.
- **Unnamed block definitions can no longer collide.** The graph-side fallback
  name is a content identity (own sections, nested rows, recursive children),
  so two different contents never share `GHStack <hash>`.
- **An `int` parameter rounds like Python does** (banker's rounding, overflow
  to the neutral value) in defaults and in the wire cast.
- **The `log_level` preference works**: `Quiet` silences the `[gh-stack]`
  console diagnostics, `Info` (the default) prints them; stage states stay in
  the panel either way.

### Changed

- **Extension-platform hygiene.** The server is gated on Blender's Online
  Access (the `network` permission contract) and says so in the panel when it
  is off; the add-on no longer touches `sys.path` (the development install
  places `websockets` on Blender's addons path instead); `files` and
  `clipboard` permissions are declared; the licence and copyright read
  GPL-3.0-or-later everywhere; `bl_info` is generated from the manifest by
  `scripts/install-addon`, so the version has one owner; `build-extension`
  pins the wheel's SHA-256, validates the built zip and refuses to ship
  platform-specific files; a pinned `ruff.toml` defines the lint scope; and
  the README's Requirements section explains what to do with Blender alone
  (`tools/fake_gh`).
- **Keepalive described as it is.** Protocol §2 documents the transport's 5 s
  ping / 20 s timeout and marks the application-level `ping`/`pong` messages
  reserved; §4.6's `proto_mismatch` row matches both sides' behaviour.

## [0.2.0] — 2026-10-17

Fifth release: named attributes travel with the geometry both ways, Grasshopper
blocks cross as Blender instances and back — nested blocks included — and the
helper data moves into a dedicated `GH Stage` scene so the outliner stays yours.

### Added

- **Attributes travel with the geometry, both ways.** Blender's named attributes
  — every type it has (floats, ints, booleans, strings, vectors, 2D/4D values,
  colours, quaternions, 4x4 matrices, `INT8`, 2D pairs) and every Mesh domain
  (POINT, EDGE, FACE, CORNER), plus point clouds — are serialized as `attr.*`
  sections with a descriptor each, and attached to the Rhino geometry's
  UserDictionary, so they ride along with duplication and transforms. Stage In
  fills them, Stage Out sends back whatever the graph attached. On the Blender
  side the format round-trips through `mesh.attributes` / `pointcloud.attributes`
  (layer B covers every type). The active colour attribute and edge creases keep
  their native channels, so nothing travels twice.
- **`Attribute Get` / `Attribute Set` components** (GH Stack → Attributes) read
  and write attributes in a definition; Stage In also lists the attribute names
  it received on its `Attributes` output, so a graph can discover them. Get
  emits native values (numbers, integers, booleans, texts, vectors, colours;
  compound types flattened) and reports Blender's own type names (**Float**,
  **Boolean**, **4x4 Matrix**, …), Set infers the type from the values and the
  domain from the element count (or takes an explicit Domain).
- **The attribute store has console coverage.** `GhStackAttrTests.exe` (built by
  `grasshopper/build/build.sh`, run by `scripts/test-gha`) pins mixed types
  keeping their own types, every wire type surviving attach → collect
  byte-exact, prefix names like `test`/`test2` staying distinct, and the labels
  matching Blender's vocabulary.
- **`i32` wire dtype** for Blender's signed `INT` attribute type; the codec, the
  canonical dtypes and both fixture suites cover it.
- **A new fixture**, `response-attributes`, pins `i32`, `f32`, `u8` and the
  STRING offsets section through both codecs (eleven fixtures total).
- **Instances travel as blocks, both ways.** Blender's instanced geometry
  crosses as a definition table (`definitions`, `idef.<i>.*` groups and the
  `inst.ref_def` / `inst.transforms` rows, protocol §5.2). Stage In builds each
  definition as a **named graph-side block** — the route Grasshopper's own Model
  Block Definition/Instance components use — so nothing is written to the
  document at solve time; Rhino materializes the blocks when the user bakes and
  reuses them, nested blocks included. Stage Out turns the graph's
  `GH_InstanceReference` items back into the same table and rows, reading
  definition geometry from the paired definition (or the document, for a
  reference that names one). On the Blender side each definition becomes a
  hidden prototype collection instanced through `Instance on Points`, driven by
  a per-definition point cloud carrying position, rotation and scale; nested
  placements instance the child's prototype chain at the parent's placement
  points, so a child placed at many nested rows costs one instance per
  placement, never baked copies, and moving a placement updates the drawing
  without rebuilding the graph. A shear the point-cloud channel cannot carry is
  applied lossily and logged.
- **Spike S9** (`spikes/s9/`): the user-dictionary and block checks that the
  attributes and instances designs rest on, run inside Rhino through the spike
  `.gha` (`check=s9`). **All rounds passed 2026-10-16** (`spikes/results/s9.txt`):
  the attribute store survives duplication, transforms, goo duplication and bake
  for every geometry kind. Blocks: a graph-side definition needs a **Name** to
  be valid — named, it previews and bakes with no document write, nested blocks
  included, and repeated bakes materialize the blocks once. The rounds caught
  two product bugs (definition attributes attached after the document copy was
  taken; a block-instance `ModelObject` refusing the geometry cast) and a probe
  bug (a matrix written in the wrong layout).

### Fixed

- **Attribute noise no longer makes an idle scene churn.** Blender evaluates
  modifier-generated attribute data (a Bevel's UVs, for example) with last-bit
  float variation between evaluations. Hashing it verbatim made every evaluation
  look like an edit, so the engine re-requested and re-applied forever. The
  prefix memo now treats a re-read that differs only in f32 attribute payloads
  within float noise as the same prefix; user-sized edits, topology, positions,
  colors and creases stay exact.

### Changed

- **Your scene stays yours.** Helper objects, block definitions and their row
  clouds now live in a dedicated `GH Stage` scene instead of your own scene, so
  the outliner and scene graph show only your data. Sessions saved before the
  move are migrated on first use; drawing and renders are unaffected.
- **Stage In has one geometry output.** `Geometry` is now a generic list that
  carries everything Blender sent, in order: mesh, SubD cage, curves, point
  cloud. The separate `Curves`, `Points`, and `SubD` outputs are gone; wires
  from them are dropped when an older document loads (re-wire from `Geometry`).
- **Points arrive as one point cloud per stage** (the geometry that can carry
  point colours and attributes). New **Point Cloud Points** component explodes
  it into a point list; Stage Out accepts both the cloud and individual points.

## [0.1.3] — 2026-10-15

Geometry that a stage reads through a node graph now follows it, and the prefix
memo follows Blender's depsgraph instead of a description of what a prefix reads.

### Fixed

- **Geometry a node graph references now reaches Grasshopper.** A prefix that
  reads another object — an Object Info node, a modifier input of type Object or
  Collection — kept sending the geometry from before that object moved: the stage
  looked stuck, and only Push, a frame change or a file reload shook it loose.
- **A slider on a prefix Geometry Nodes modifier reaches Grasshopper.** Its stored
  group input values are ID properties rather than RNA, so the revision could not
  see them move.
- **Push clears the memoized prefix along with the frame cache.** The manual
  recovery dropped the cached frames but kept serving the geometry from before the
  change, so the next request produced the same output again.
- **The panel reports the port the server is bound to**, and restarting on another
  port no longer leaves a stale "server stopped" error behind a running server.

### Changed

- **The prefix memo is invalidated by Blender's depsgraph.** The revision used to
  freeze the values of everything a prefix reads — base geometry, modifier
  settings, node-graph wiring, referenced objects, shape keys — and compare them
  on every pass. A frozen value cannot follow a reference, which is why each of
  the three fixes above was possible to miss; now what Blender tags is what
  retires the memo, so the class of failure is gone rather than the instances.
  Measured on a 50k-triangle prefix: a memo hit costs 0.010 ms where it cost
  8.1 ms, and a stage at the head of the stack — which had no memo at all — 0.006
  ms where it re-read its base on every pass.
- **Reactions are immediate.** The engine runs at its fast cadence for a second
  after an edit, a response or a frame change instead of waiting up to an idle
  interval, so playback fills frames as each answer arrives. A quiet scene still
  idles.
- **The Diagnostics panel reports how long a stage has been waiting**, past a
  second — the shape a sync fault takes when nothing else is visible.
- **A saved port is the port the server binds.** The preferences class registered
  after the engine, so the server started before the port was known and silently
  used the default.
- **The plugin writes frames under a lock**, so a keepalive pong can no longer
  interleave with a large frame and corrupt the connection, and the server's
  keepalive is explicit (5 s ping, 20 s timeout) rather than the library defaults.

## [0.1.2] — 2026-10-12

Third release: the simulation-style frame cache, mid-stack prefix evaluation, and
a hardening pass over the plugin and the decode boundary.

### Added

- Simulation-style frame cache (ADR 0008, supersedes the planned bake operator):
  - Frames are stored on disk under the data root (`~/.local/share/gh-stack/`,
    configurable) as the raw protocol frame plus a sidecar, keyed by request ID.
    Because a keyframed parameter produces a different request ID per frame,
    the content-addressed response store *is* the frame cache: frames fill as
    you play and scrub, replay in any order, and need no bake setup or frame
    range.
  - A per-stage generation resets the stage's frames when Grasshopper sends
    something that invalidates them: the same request answered with a different
    output hash, a changed schema, a reconnect with a different document, or
    Push. A re-opened session reuses frames whose generation and definition
    fingerprint still match.
  - Cached frames replay with Grasshopper closed (renders use the cache).
    Uncached frames keep the last good output; an answer for a frame the
    scene has moved past is cached, never displayed.
  - The memory cache is bounded by the same cache size limit (256 MB default)
    and falls back to the disk store.
  - Preferences: data root and cache size limit.
- Idle stability: engine writes are change-gated (modifier visibility, stage
  state/warnings, helper flags), a no-op response neither applies nor tags, and
  the evaluated prefix is memoized against a revision (base geometry, modifier
  settings, referenced objects, upstream outputs, shape keys) so an unchanged
  pass never re-clones the object. Cycles can accumulate viewport samples while
  the scene is idle. Measured prefix evaluation: 1.1 ms memo hit vs 5.3 ms clone
  at 10k triangles, 8.3 ms vs 56.1 ms at 100k.
- Layer B coverage for the cache: progressive fill, replay with Grasshopper
  closed, GH-side and schema resets, document-change resets, quiet no-op and
  idle behavior, eviction, and uncached-frame behavior (15 tests). `fake_gh`
  gained `--gh-edit-after`, `--schema-after`, `--echo-after`, and `--doc`.
- Layer A coverage for the disk store: write/load round trip, scan, stale
  sidecar cleanup, generation/fingerprint validity, prune, and file deletion on
  eviction (10 tests).
- Mid-stack composition: a stage's input is the stack prefix before its own
  modifier, evaluated on a clone, so native modifiers and earlier GH stages
  compose in either order (`GH → GN → GH`). A waiting stage pauses the ones
  after it, so no stage runs on a stale prefix. Layer B covers a two-stage chain
  with modifiers between, curve and point-cloud prefixes, and the quiet steady
  state (54 headless tests; prefix evaluation 4.3 ms at 10k triangles, 50.9 ms
  at 100k).
- Reconnect resends: a GH client that says `hello` makes Blender re-send the
  current request for every waiting stage (a request sent while the client was
  away is lost), and an `unknown_ref` answer makes Blender resend the geometry
  in full (protocol §8.8 and §4.3). Error responses are no longer cached as
  results.
- Deployment discipline and test tooling for the plugin (which has no automated
  layer of its own):
  - `scripts/check-deployed` compares the deployed `GhStack.gha` (sha256) against
    the last build, so a stale copy is visible before it costs a debugging
    session; `scripts/test-gha` prints the warning, and the README's install
    section says a build is not live until the copy *and* a Rhino restart have
    happened.
  - `fake_gh --fail-after N` answers response N with `gh_solve_failed`;
    `--coalesce` models the plugin's collapsed `ScheduleSolution` solves — only
    the newest request of a solve window is answered, and the report counts the
    dropped ones as `dropped`.

### Fixed

- **A failed `Stage Out` now answers immediately** instead of leaving Blender to
  wait out the 30 s timeout: the component enqueues the protocol's error response
  (`gh_solve_failed`, empty hash, zeroed counts) when nothing was sent, and
  records the slot status under the same lock the retry path reads. The engine's
  error branch also no longer re-sends on the same tick — a solve that just
  failed fails again, so retries now follow protocol §7 (next change or Push).
  Layer B pins both halves of that behavior (`SolveFailureTests`), with
  `fake_gh --fail-after` and the negative control in the commit.
- **The plugin's outbox can no longer jam or grow without bound.** A failed send
  dequeued nothing, so that frame stayed at the head and every later frame queued
  behind it forever; the queue had no cap and was never cleared on disconnect, so
  a stalled link accumulated a frame per solve and delivered the backlog on
  reconnect. `SendFrames` now dequeues before sending, `Enqueue` keeps the queue
  under 64 MB (oldest first, never the newest), and a disconnect clears it.
- **A malformed frame can no longer tear down a healthy connection.** Every
  malformed path in the C# JSON parser raises `JsonException` (`\uZZZZ`, `1.2.3`,
  `1e`), `ReceiveLoop` has a last-resort catch that notes and continues, a
  non-numeric `v` is a `proto_mismatch` instead of a converter crash (`"2"` is
  rejected as in Python), and `Decode` rejects unknown dtypes and nameless
  sections exactly as the Python codec does. The console suite gained a
  `decoder-boundary` group; against the old code it reports six problems.
- **Edits inside nested node groups now invalidate the prefix memo.** The
  signature walked only the group's top level, so a prefix modifier built as
  `Outer → Inner` kept serving the geometry from before an edit inside `Inner`.
  The walk is now recursive over every node with a `node_tree` (Blender exposes
  one exactly on the group node types), identity-keyed so a recursive group
  cannot loop, and depth-capped in a way that disables the memo rather than
  truncating the signature silently. Layer B builds the nested case and fails
  with the old walk (negative control).

- **A prefix modifier that reads scene time produced the same geometry for every
  frame**, so the frame cache answered every frame with the first frame's result
  and the stage never moved on the timeline. `_prefix_revision` enumerates what
  the evaluated prefix depends on, and the frame was not in that list: a Scene
  Time node — like a simulation zone or a driver expression — is read at solve
  time rather than stored on the modifier, so the revision (modifier properties,
  node-graph wiring, base geometry, upstream outputs, shape keys) was
  byte-identical across frames and the prefix memo returned the first evaluation.
  The geometry hash never moved, so the request ID never moved, so the cache
  answered every frame from one entry and Grasshopper was never asked again.
  The revision now includes `scene.frame_float`, and its docstring states the
  invariant it has to hold: **every input the evaluated prefix reads belongs in
  that list**. Measured before the fix: a prefix offset by 1.0 at frame 24 still
  served the frame-1 geometry and the request ID was unchanged. Layer B covers a
  time-driven prefix (3 tests); with the one line removed, all three fail, which
  is the negative control for exactly this bug.
- Two holes in the same memo. It was keyed by object *name*, so an object could
  inherit what a different object of that name had evaluated; it is keyed by
  object identity now (`as_pointer()`, which holds no reference). And the prefix
  memo's revision read the whole base mesh on every pass, not only on a clone, so
  a *memo hit* cost 8.7 ms at 50k triangles against 45 ms for the clone — see
  P4-1 in `docs/polish-plan.md`.
- `register()` cleared only the disk stores, so a decoded frame, an evaluated
  prefix, a pending request and the counters all survived a disable/enable. All
  four are cleared now, so a session starts with nothing it did not evaluate.
- Playback did not evaluate most frames, so it never got smoother. Only one
  request could be in flight per stage and a frame that arrived while it was in
  flight was not requested at all, so a pass over a range evaluated a fraction of
  it (measured: 3 of 10 frames). Each frame is now evaluated on demand: **one
  evaluation at a time**, and the frame the playhead is on is requested on the
  first pass after an answer lands. Frames merely passed over are **not queued** —
  see the entry below for why — but every answer is stored, so a frame evaluated
  once is free afterwards. Measured over 24 frames scrubbed at 20 fps: 3 requests
  at a 400 ms round trip and 2 at 800 ms, against one per frame before, with the
  frame the playhead stops on always evaluated. A `stage.rid` regression from the
  same work is fixed with it: it was written on every pass rather than when a
  request went out, so a reply still in flight stopped matching the stage and was
  discarded. Layer B covers one-at-a-time and replay (4 tests).
- Asking for every frame the playhead passes over queued a backlog at the client.
  The engine briefly requested each frame as it arrived, abandoning the request in
  hand; Grasshopper solves on one thread in arrival order, so scrubbing handed it
  a queue of frames the user had already moved past and it stopped responding
  until the queue drained. A frame that arrives while an answer is outstanding is
  not requested now: the stage waits, and asks for whatever frame the playhead is
  on once the answer lands. A cached frame never reaches that gate, so a replay is
  not throttled by an outstanding request.
- **A cached frame answered while a request was in flight cleared that request's
  bookkeeping**, so the next pass saw an open gate and sent a second request: the
  one-at-a-time guarantee was not actually held. Measured with the engine's send
  and response paths instrumented while scrubbing over a mix of cached and
  uncached frames: **a peak of two requests in flight**, and 11 requests where 7
  do the same work. That is the backlog above returning by another route, and,
  because the plugin coalesces its solves, it can answer a request the engine has
  stopped waiting for — which left a stage looking stuck until a manual Push. The
  in-flight entry now survives a cache hit and is cleared only when the answer
  arrives, when the timeout gives up on it, or on an explicit reset (Push, a
  client `hello`, a load). Layer B covers the invariant directly
  (`OneRequestOutstandingTests`) and fails with the one line restored.
- A request that was never delivered stalled the stage for a whole timeout. With
  no client socket to carry it, `GhServer.send` dropped the frame silently while
  the engine recorded it as pending, so the stage waited for an answer nobody had
  been asked for — and the retry was gated on that same entry. `send` now reports
  whether a client received the frame, the engine leaves the stage `disconnected`
  instead of pending, and `bridge.state["clients"]` exposes the socket count.
- Reopened files could end up with no geometry and no visible error. A stage
  keeps its geometry hash in the `.blend`, so the first request after a load was
  a `ref` — for geometry the client may no longer hold. The plugin read
  `geom.mode` and then ignored it, so an unservable `ref` was dropped without a
  reply: Blender waited out its timeout, retried with another `ref`, and looped
  forever. Both sides are fixed:
  - A load, and a `hello`, now clear the geometry hash, so the next request goes
    out in full — the rule protocol §8.8 already states for the first request
    after a connection.
  - The plugin tracks the hash its sections belong to and answers `unknown_ref`
    (protocol §4.3) for a `ref` it cannot serve, so Blender resends in full
    instead of hanging.
  - Blender now handles the standalone `error` message (protocol §4.6), which it
    previously ignored entirely — only the error-status response shape was
    handled, so a spec-shaped refusal passed unnoticed.
  - A request that does time out no longer retries the same `ref`: it is retried
    in full, and the failure stays visible instead of being overwritten by the
    next pass's `waiting` state.
  - `fake_gh` gained `--forget-after` and `--refusal` (both shapes) and now
    reports `refusals`. Layer B covers the recovery through the error message and
    through the error response (2 tests).
- `_handle_response` returned from inside its loop over objects, so a response
  for one stack ended the pass for every other stack sharing that stage key: the
  second object never applied the answer. The outcomes are per-stage, so they
  `continue` now.
- Reopened files: opening a `.blend` (File → Open, Open Recent, Revert, or
  Blender started with a file) cleared the engine's timer and app handlers,
  because Blender only preserves persistent ones. After any load the add-on
  went silent and never talked to Grasshopper again. The timer and handlers are
  persistent now, and a load behaves like a reconnect: stages saved mid-request
  are reset and every stack is evaluated against the cache and Grasshopper
  without waiting for an edit. Layer B covers the save/open ordering where
  Grasshopper connected before the file was opened (3 tests).

### Changed

- The planned Bake operator is dropped (ADR 0008): the frame cache fills as you
  play and scrub, and Push now also resets the stage's cached frames.
- A cached frame that is already applied is a complete no-op: no scene writes,
  no depsgraph tag.

## [0.1.1] — 2026-10-12

Second release: all geometry kinds, exact curves, and a real Blender extension.

### Added

- Blender extension packaging (S8): `blender_manifest.toml` with the network
  permission and the official pure-Python `websockets` wheel,
  `scripts/build-extension`, `scripts/install-extension`, and
  `scripts/test-extension` (installs into a throwaway config and round-trips
  against `tools/fake_gh`). The development symlink install stays and falls
  back to `vendor/`.
- Curves, points, and SubD flow through a stage in both directions:
  - `Stage In` outputs `Curves`, `Points`, and `SubD` alongside the mesh.
  - `Stage Out` takes one generic `Geometry` input (mesh, curve, point, SubD,
    or Brep) and converts it to protocol sections. All curve goos are accepted
    (`GH_Circle`, `GH_Arc`, `GH_Line`, `GH_Rectangle`, `GH_Curve`, custom goos).
  - SubD cages carry edge-crease tags; fractional values are rounded to the
    nearest tag with a warning, and the semi-sharp/vertex-crease limitation is
    reported on every SubD stage (D2).
  - Curves are exact by construction: `MakePiecewiseBezier` splits them into
    single rational Bezier spans (weights kept), each of which Blender holds
    exactly as a NURBS spline. Circles, ellipses, and arcs of any angle come
    back exactly; only curves the decomposition cannot carry (degree above 5,
    periodic) are fitted and warned (D3).
  - Breps and surfaces are meshed on the way out, warned once per stage.
  - Blender mesh stacks can be flagged as SubD cages (`Base is a SubD cage`);
    Blender surface objects are meshed on the way in.
- Response warnings are shown in the stage panel; the stage record keeps them
  across ticks.
- Layer B coverage for curve, point, SubD, and mixed-geometry responses
  (42 headless tests).

### Fixed

- WebSocket control frames: the client answers pings and skips pongs, so
  Blender's 2 s keepalive no longer arrives as a bogus protocol frame
  ("header length over the cap") and the connection no longer drops.
- Rational curves: Rhino stores homogeneous control points while the wire (and
  Blender) carry Euclidean coordinates plus a weight. Mixing the two distorted
  every rational curve — arcs and ellipses arrived as parabolas. Both
  directions convert explicitly now.

## [0.1.0] — 2026-10-12

First public release: a working demo of the Blender ↔ Grasshopper loop.

### Added

- Protocol v2: frame codec, hashing, request rules, and cache index in both
  Python and C#, verified against ten shared golden fixtures
  (`protocol/fixtures/`).
- Blender add-on (`blender/gh_stack/`): WebSocket server on a background
  thread, stack engine with states, timeouts, latest-wins requests and an
  in-memory cache; hidden helper objects; one Geometry Nodes modifier per
  stage; N-panel with stage rows, copyable keys, diagnostics, and a
  connection status.
- Parameters: float, int, bool, vector3, colour, value list, and text, with
  ranges, a value-list dropdown, keyframes, and drivers. Values are custom
  properties, so Blender's own animation and undo apply.
- Grasshopper plugin (`GhStack.gha`): Stage In, Stage Out, and Param
  components with a background WebSocket client, reconnect with backoff, and
  runtime messages only (no dialogs).
- Demo v0, documented in `DEMO.md`: Cube → Stage In → Scale → Stage Out, with
  a Param on the factor.
- Test suites: layer A (pure core), layer B (Blender headless against
  `tools/fake_gh`), layer C (C# codec and conversions under wine on Linux).
- CI running the pure-core tests on every push.

### Known limitations

- Meshes only. Curves, points, and SubD have verified conversion primitives
  (spike S4) but are not wired through the components and engine yet.
- A stage always reads the stack's base mesh; mid-stack composition
  (`GH → GN → GH`) is not evaluated as a prefix yet.
- In-memory cache only: no bake, freeze, preview, or orphan list yet.
- Install is a symlink script; no packaged Blender extension yet.

[0.1.0]: https://github.com/miscasquel/gh-blender-stack/releases/tag/v0.1.0
[0.1.1]: https://github.com/miscasquel/gh-blender-stack/releases/tag/v0.1.1
[0.1.2]: https://github.com/miscasquel/gh-blender-stack/releases/tag/v0.1.2
[0.1.3]: https://github.com/miscasquel/gh-blender-stack/releases/tag/v0.1.3
[0.2.0]: https://github.com/miscasquel/gh-blender-stack/releases/tag/v0.2.0
