# Changelog

Notable changes to this project, following
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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
