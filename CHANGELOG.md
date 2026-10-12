# Changelog

Notable changes to this project, following
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

### Fixed

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
