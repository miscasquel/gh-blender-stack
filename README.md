# Stageloom

Drive Grasshopper definitions from Blender as stackable modifiers, next to
native Geometry Nodes modifiers, in one visible object per stack. Geometry and
parameters can flow through the stack, so parameters can be keyframed, driven,
and played back.

That is the v1 goal. **v0.7.0 is the current release**: geometry (meshes,
curves, points, SubD cages) driven by a real Grasshopper definition, with named
attributes and Grasshopper blocks (instances, including nested and multi-part
definitions) travelling both ways, parameters you can edit and keyframe in
Blender, packaged as a Blender extension. 0.8.0 is in progress on `main`
(T1–T9 and T10a done; the T10b coverage/scale/soak and T11 release remain); the
manifest and shipped version stay **0.7.0** until the T11 release bump.
[DEMO.md](DEMO.md) walks through the loop.

```
Blender                                          Rhino / Grasshopper (wine)
┌───────────────────────────┐      WebSocket     ┌─────────────────────────────────┐
│ Cube                      │◄──────────────────►│ Stage In ──► Scale ──► Stage Out│
│ Stageloom · Demo (stage)  │       59130        │                 ▲               │
│   Bevel (native, optional)│                    │               Param             │
└───────────────────────────┘                    └─────────────────────────────────┘
```

## What works today

- **The loop.** Geometry goes to Grasshopper, a definition runs, the result comes
  back and shows through a modifier in the stack. Base-geometry edits flow on the
  next tick; no buttons.
- **Stacks.** Native modifiers and GH stages compose in either order: a stage
  sees everything before its own modifier in the stack, so `GH → GN → GH` chains
  evaluate as a prefix, and a waiting stage pauses the ones after it. Duplicating
  a stack object gives the copy its own stage key, stack identity, node group and
  helpers, with its parameter values and animation, so the two cannot cross-talk.
  Two stages never share a key: a file or undo that restores such a state shows
  the `conflict` state on every affected stage and sends or applies nothing for
  them; it is resolved per stage from the panel with **Resolve duplicate key**.
- **Geometry kinds.** Meshes (n-gons and vertex colours), curves (poly, Bezier,
  NURBS, circles, ellipses, arcs), point clouds, SubD control cages with
  edge-crease tags, and Breps/surfaces (meshed on the way out, lossy). Stage In
  delivers everything through one generic `Geometry` output — mesh, SubD cage,
  curves, point cloud, in that order; Stage Out takes any of them on one generic
  input. A Blender surface object cannot hold a stage (it cannot
  take a Geometry Nodes modifier), so convert it to a mesh first. A wire mesh
  (no faces, e.g. a truss line network) travels as one
  two-point curve per loose edge plus a point cloud for vertices with no edge,
  so EDGE attributes stay one-per-curve and POINT attributes stay lists per
  curve; you do not need a `Mesh to Curve` node.
- **Attributes.** Blender's named attributes (floats, ints, booleans, strings,
  vectors, colours, quaternions, matrices — every type, on every mesh domain)
  travel with the geometry: Stage In attaches them to the Rhino geometry and
  lists their names on its `Attributes` output, Stage Out sends back what the
  graph attached. **Attribute Get** and **Attribute Set** read and write them in
  Grasshopper. The active colour attribute and edge creases keep their native
  channels (vertex colours, SubD tags).
- **Blocks (instances).** Blender's instanced geometry crosses as Grasshopper
  block definitions and instances, built graph-side with no document write;
  nested and multi-part blocks work both ways. On the Blender side each
  definition is drawn through `Instance on Points` — nested placements are true
  instances, one per placement — and all of the helper data lives in a
  dedicated `Stageloom Stage` scene, so your own scene and outliner stay clean.
- **Parameters.** `Param` components declare float, int, bool, vector3, colour,
  value list, and text parameters. Blender draws them in the N-panel with
  independent min/max ranges, a value-list dropdown, keyframe decorators, and
  drivers. A new Param node starts compact with Key, Label and Type;
  **Show all inputs** reveals
  Default, Min, Max, Step, Values and Group, and **Hide unused inputs** removes
  any optional input that has no value and no wire. The parameter list is saved
  with the definition, so the choice sticks.
- **Protocol.** Binary frames with a canonical JSON header; geometry and
  parameters in the same stream. The Python and C# codecs share fourteen golden
  fixtures; the dtype, hash-order, attribute, warning and fixture tables are
  generated from `protocol/sections.json` and checked for drift in CI.
- **Lifecycle.** States (`idle`, `waiting`, `ok`, `error`, `disconnected`,
  `disabled`, `frozen`, `unbound`, `conflict`), timeouts, latest-wins requests,
  and reconnect with backoff. `disabled`/`frozen`/`conflict` follow the stage's
  flags and key ownership rather than ad-hoc writes. The stage's modifier is its
  lifecycle: its toggles enable and disable it, deleting it removes the stage,
  Pull refreshes it once, Freeze parks it. Inspectable from the UI.
- **Live stages.** Every delivered request is a solver step: the stage asks
  again whenever its own inputs or the frame move, one request is outstanding at
  a time, and a late answer for a superseded request is dropped. A disconnected
  stage keeps its last output; there is no custom frame cache to replay.
- **Playback.** In **Play Every Frame**, the playhead waits for each stage's
  output for the frame before advancing; a disconnected stage keeps its last
  output. **Frame Drop** keeps requests latest-wins. A **Sync wait limit**
  preference caps the wait per stage (0 waits until the answer, a disconnect,
  or the ten-minute backstop).
- **Bake.** Each stage group carries a native Geometry Nodes Bake node. **Bake
  stage** steps the scene frame range through the same gate, shows cursor
  progress, bakes only the selected stage, and freezes the stage with every
  upstream stage it depends on (including stages on other objects). It then
  triggers each downstream dependent once, so the dependent sees the baked
  output, and freezes it on that one-frame answer. Only the selected stage has
  per-frame bake data: a dependent that animates and needs offline playback is
  baked in turn. Clear Bake deletes only this stage's bake and unfreezes the
  stages this bake froze (upstream or downstream); another stage's bake and a
  manual freeze are untouched. A packed bake is kept when the `.blend` is saved.
- **Tests.** `scripts/check-all` is the local gate: docs, protocol-table drift,
  core, Blender headless (plus the offline gate), benchmarks, the installed
  extension round trip, the reproducible-zip and `.gha` checks and the C#
  suites, with per-step timing. The current tree is checked by 133 core tests,
  468 Blender headless tests, fourteen golden fixtures and the C# suites (689
  passing checks across the codec fixture runner and eight console suites, plus
  a fixed-seed 10,000-case frame fuzz; the native Rhino halves skip under
  wine). The Blender headless suite also runs deterministic randomized
  geometry, attribute, instance and bake-marker round trips.
  `scripts/check-protocol-tables` (generated tables and fixture inventory),
  `scripts/test-core` (standard library only),
  `scripts/test-blender` (Blender headless against `tools/fake_gh`),
  `scripts/test-gha` (the C# codec against the golden fixtures, the 10,000-case
  frame fuzz, plus the conversion, attribute, instance, transport, link,
  component and stage suites, under wine), `scripts/test-extension` (installed
  extension round trip), `scripts/test-reproducible` (two-build extension zip
  comparison) and
  `scripts/test-reproducible-gha` (two-build plus copied-tree `.gha`
  comparison) are its steps.

## What is deliberately not here yet

- Breps and surfaces arrive as meshes (lossy, warned). There is no B-rep on the
  wire. Blender surface objects cannot hold a Stageloom stage;
  convert them to a mesh first. There are some issues with the meshing, and it
  is obviously a lossy conversion. That won't be solved without a proper surface
  kernel inside blender (surface psycho may bridge this gap)
- There is no custom frame cache. Freeze and native **Bake stage** are the
  offline path; a simulation-zone cache remains later R&D. A bake freezes its
  downstream dependents with one frame each; bake an animated dependent in turn
  if it needs offline per-frame data.
- **Clear Bake cannot be undone.** It deletes the stage's native Geometry Nodes
  bake immediately; recreating it means baking the scene range again. The panel
  asks for confirmation before deleting a bake.
- There is no separate Preview operator: a stage's output is its modifier, so
  the viewport and render toggles are the preview switch. Freeze, Pull, Bake
  stage, Clear Bake, Key all and Purge orphans are in the Stageloom panel.

The authoritative roadmap and scope are in [docs/plan.md](docs/plan.md).

## Requirements

- Blender 5.2 or newer; 1.0 targets Blender 5.3 (Python 3.13). The add-on
  declares the `network` permission — it listens on localhost — so Blender's
  **Online Access** must be on
  (Preferences > System); with it off the panel says so and nothing listens.
- **Rhino 8 with Grasshopper** to run real definitions, under wine or Windows
  (mac needs testing). Development and testing happen on one Linux machine; the
  build scripts assume `~/.local/share/wine-rhino` and a `rhino8` wine prefix,
  both overridable with the `WINE` and `WINEPREFIX` environment variables.
  Mono's `csc.exe` (ships with wine) builds the C# 5 plugin; a
  `net48`/`net7.0-windows` migration is a recorded no-go through 1.0
  ([ADR 0019](docs/decisions/0019-keep-csharp5-mono.md)), revisited only after
  native Windows/Rhino acceptance. **Native Windows/Rhino acceptance is
  deferred until after 0.8.0**; this release makes no native-parity claim and
  the C# suite remains a local gate (a stock CI runner has neither wine with
  Mono's `csc` nor Rhino 8's managed assemblies).

The Grasshopper half is built from this same repository (`grasshopper/`,
GPL-3.0-or-later), so no closed component is required. **Without Rhino** the
Blender half still runs and is inspectable: install the add-on, add a stage,
then answer the loop with the bundled stand-in —

```
python3 tools/fake_gh/fake_gh.py --stage <stage key from the panel>
```

It connects like the plugin, echoes the geometry back, and (with `--report`)
writes what it received and sent to a JSON file. `scripts/test-blender` drives
the same tool across the whole engine surface.

### Reviewer quick start (no Rhino)

1. Install the extension zip and enable **Stageloom**.
2. Enable **Online Access** in Preferences > System.
3. Select an object, open the **Stageloom** sidebar tab and press **Add Stage**.
   The stage reports `disconnected` until something connects.
4. Run the stand-in with the stage key from the panel:

   ```
   python3 tools/fake_gh/fake_gh.py --stage <stage key>
   ```

   It connects like the plugin, echoes the geometry back, and the stage reports
   `ok`. `--report <path>` writes what it received and sent to JSON.

The add-on's logs live in its per-user extension data directory by default; the
**Data root** preference in the add-on settings overrides it.

## Install

Download the add-on zip and the Grasshopper `.gha` from the
[v0.7.0 release](https://github.com/miscasquel/gh-blender-stack/releases/tag/v0.7.0).
The commands below are the build path.

Release path: the add-on ships as a Blender extension with the official
pure-Python `websockets` wheel. `blender_manifest.toml` is the only version
source (`scripts/version` prints it), and the build normalizes the zip
(sorted entries, fixed `1980-01-01` timestamps and modes), validates the
rewritten artifact and prints/writes its SHA-256. `scripts/test-reproducible`
builds twice and asserts the fixed metadata as well as comparing bytes.
`grasshopper/build/build-gha.sh` passes Mono `csc` `/deterministic`; two builds
in the tree and one from a copy at a different absolute path are byte-identical
(same toolchain and tree; no `/pathmap` is needed because the build emits no
debug info). `scripts/test-reproducible-gha` checks that locally.

```
scripts/build-extension          # builds dist/stageloom-<version>.zip
scripts/install-extension        # installs and enables it in Blender

grasshopper/build/build-gha.sh   # build Stageloom.gha (Rhino 8)
grasshopper/deploy/deploy.sh --dry-run  # print the resolved wine user and path
grasshopper/deploy/deploy.sh     # copy it into the Grasshopper Libraries
```

`scripts/check-deployed` says whether the copy Grasshopper will load is the one
you last built. A `.gha` is scanned only when Rhino starts, so a build is not
live until the copy *and* the restart have both happened; `scripts/test-gha`
prints the same warning when the deployed plugin is stale. `deploy.sh` finds
the Windows user under the prefix; if the prefix has more than one, set
`STAGELOOM_WINE_USER` to the Rhino 8 user.

Development path: `scripts/install-addon` symlinks the working tree into
Blender's user scripts and unpacks the same declared `websockets` wheel beside
it. Both installs appear as **Stageloom** in Preferences > Add-ons.

Then follow [DEMO.md](DEMO.md). The server listens on port 59130 (configurable in
the add-on preferences).

## Endpoint

The two halves talk over one loopback WebSocket; the port must match on both
sides. Nothing outside the machine can connect.

- **Blender.** Preferences > Add-ons > **Stageloom** > **Server port** (default
  59130). The server binds `127.0.0.1` only, and the N-panel shows the full
  `ws://127.0.0.1:<port>/` endpoint it is listening on.
- **Grasshopper.** Add **Stageloom Connection** (Stageloom tab, Settings) to any
  document and set **Port**. It applies immediately and the saved setting is
  global to the whole Rhino/Grasshopper installation, in Grasshopper's app-data
  settings (`%APPDATA%\Grasshopper\Stageloom.xml`, key `endpoint.port`; under
  wine, `<prefix>/drive_c/users/<user>/AppData/Roaming/Grasshopper/Stageloom.xml`),
  not stored as the authoritative endpoint in the `.gh` file. On load the
  component's input is reseeded from that global value. The component does not
  have to stay on the canvas: Stage In and Stage Out read the saved port when
  the plugin loads, so a document containing only those components connects
  after a restart.
- **Development override.** `STAGELOOM_PORT` sets the plugin's port when no
  valid saved port exists. The precedence is saved setting > `STAGELOOM_PORT` >
  59130. This is a convenience for scripts and tests; the Connection component
  is the supported way to configure a normal install.

The add-on itself always reads its port from the **Server port** preference.
Hosts other than `127.0.0.1`/`localhost` are rejected, so neither half can be
pointed at a remote machine.

## Working on it

```
scripts/check-all          # the gate: docs, protocol tables, core, Blender, bench, extension, reproducibility, .gha, C#
scripts/check-all --quick  # inner loop: docs, protocol tables, core and ruff only
scripts/check-protocol-tables # generated tables and fixture inventory are current
scripts/test-core        # layer A: pure Python core, no Blender needed
scripts/test-blender     # layer B: Blender headless, against tools/fake_gh
scripts/test-gha         # layer C: the C# codec and conversion suites under wine
scripts/test-extension   # layer D-lite: builds, installs into a throwaway config, round trip
scripts/test-reproducible # builds the extension zip twice and compares the bytes
scripts/test-reproducible-gha # builds the .gha twice plus a copied-tree build and compares
scripts/version          # prints the version from blender_manifest.toml
scripts/bench            # reference performance matrix and its budgets (not a test layer)
scripts/bench-baseline   # 3 sequential runs, best p50/p95 vs the stored 2026-11-17 baseline
scripts/bench-gha        # C# parameter/schema microbenchmark (wine/Mono)
scripts/code-health      # regenerate docs/code-health.md (not a gate)

python3 scripts/make-fixtures   # regenerate protocol/fixtures after a protocol change
```

CI runs the core layer on Python 3.13 and 3.14, including the protocol-table and
fixture-inventory drift check. A second job installs Blender
5.3 on Ubuntu and runs `scripts/check-all --ci`: docs, protocol-table drift,
ruff, core, Blender headless with the offline gate, the installed-extension
round trip, the baseline-gate self-test and the two-build extension-zip
reproducibility check.
The timing benchmark is **not** run in CI: the stored 2026-11-17 best-of-three
baseline is reference-machine-specific, so `scripts/bench-baseline` is a
local/release check and CI makes no performance claim. That job names and skips
the C# and `.gha` reproduction steps because a stock runner has no
wine/wine-mono and no Rhino 8 managed assemblies; the C# suite and
`scripts/test-reproducible-gha` are local gates (C# 5/Mono,
[ADR 0019](docs/decisions/0019-keep-csharp5-mono.md)). No stock-runner codec job
is claimed. Native Windows/Rhino acceptance is deferred until after 0.8.0.

## Support

Issues and support: <https://github.com/miscasquel/gh-blender-stack/issues>.

## Layout

```
blender/            Blender add-on (extension support) and its tests
  stageloom/core/    pure Python: codec, hashing, params (no bpy)
  stageloom/blender/ the bpy layer: server, engine, stack, geometry, UI
  stageloom/wheels/  the pinned official websockets wheel (the only source)
  tests/core/       layer A tests
  tests/headless/   layer B tests
grasshopper/        Grasshopper plugin (C#, built with wine csc.exe)
  src/core/         codec, hashing, conversion (RhinoCommon only)
  src/gh/           Stage In, Stage Out, Param components
protocol/fixtures/  golden frames shared by both codecs
tools/fake_gh/      Python stand-in for the GH side, used in Blender tests
benchmarks/         reference performance matrix and recorded baselines
scripts/            test, benchmark and build entry points
docs/
  plan.md           the project plan (authoritative, roadmap)
  protocol.md       protocol v2 specification
  decisions/        decision records
```

## Licence

GPL-3.0-or-later. See [LICENSE](LICENSE).
