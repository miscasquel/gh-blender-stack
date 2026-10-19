# GH Stack (placeholder name)

Drive Grasshopper definitions from Blender as stackable modifiers, next to
native Geometry Nodes modifiers, in one visible object per stack. Geometry and
parameters can flow through the stack, so parameters can be keyframed, driven,
and played back.

That is the v1 goal. **v0.3.0 is a working demo**: geometry (meshes, curves,
points, SubD cages) driven by a real Grasshopper definition, with named
attributes and Grasshopper blocks (instances) travelling both ways, parameters
you can edit and keyframe in Blender, packaged as a Blender extension.
[DEMO.md](DEMO.md) walks through the loop.

```
Blender                                          Rhino / Grasshopper (wine)
┌───────────────────────────┐      WebSocket     ┌─────────────────────────────────┐
│ Cube                      │◄──────────────────►│ Stage In ──► Scale ──► Stage Out│
│   GH · Demo (stage)       │       59130        │                 ▲               │
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
  a stack object gives the copy its own stage key, cache, node group and
  helpers, with its parameter values and animation, so the two cannot cross-talk.
- **Geometry kinds.** Meshes (n-gons and vertex colours), curves (poly, Bezier,
  NURBS, circles, ellipses, arcs), point clouds, SubD control cages with
  edge-crease tags, and Breps/surfaces (meshed on the way out, lossy). Stage In
  delivers everything through one generic `Geometry` output — mesh, SubD cage,
  curves, point cloud, in that order; Stage Out takes any of them on one generic
  input.
- **Attributes.** Blender's named attributes (floats, ints, booleans, strings,
  vectors, colours, quaternions, matrices — every type, on every mesh domain)
  travel with the geometry: Stage In attaches them to the Rhino geometry and
  lists their names on its `Attributes` output, Stage Out sends back what the
  graph attached. **Attribute Get** and **Attribute Set** read and write them in
  Grasshopper. The active colour attribute and edge creases keep their native
  channels (vertex colours, SubD tags).
- **Blocks (instances).** Blender's instanced geometry crosses as Grasshopper
  block definitions and instances, built graph-side with no document write;
  nested blocks work both ways. On the Blender side each definition is drawn
  through `Instance on Points` — nested placements are true instances, one per
  placement — and all of the helper data lives in a dedicated `GH Stage`
  scene, so your own scene and outliner stay clean.
- **Parameters.** `Param` components declare float, int, bool, vector3, colour,
  value list, and text parameters. Blender draws them in the N-panel with
  ranges, a value-list dropdown, keyframe decorators, and drivers.
- **Protocol.** Binary frames with a canonical JSON header; geometry and
  parameters in the same stream. The Python and C# codecs are checked against
  eleven shared golden fixtures.
- **Lifecycle.** States (`waiting`, `ok`, `error`, `disconnected`, `disabled`,
  `frozen`, `unbound`), timeouts, latest-wins requests, and reconnect with
  backoff. The stage's modifier is its lifecycle: its toggles enable and disable
  it, deleting it removes the stage, Pull refreshes it once, Freeze parks it.
  Inspectable from the UI.
- **Frame cache.** The cache behaves like a simulation cache: it fills as you
  play and scrub, stores frames on disk (content-addressed, bounded, resettable
  per stage), replays with Grasshopper closed, and leaves an idle scene alone so
  Cycles keeps accumulating samples. No explicit bake operator.
- **Tests.** `scripts/test-core` (standard library only), `scripts/test-blender`
  (Blender headless against `tools/fake_gh`), `scripts/test-gha` (the C# codec
  against the golden fixtures, under wine), `scripts/test-extension` (installed
  extension round trip).

## What is deliberately not here yet

- Breps and surfaces arrive as meshes (lossy, warned); Blender surface objects
  are meshed on the way in. There is no B-rep on the wire. There are some issues with the meshing an it's obviously a lossy conversion. That won't be solved without a proper surface kernel inside blender (surface psycho may bridge this gap)
- The frame cache's disk format is per machine and has no persistent promise.
- There is no separate Preview operator: a stage's output is its modifier, so
  the viewport and render toggles are the preview switch. Freeze, Pull, Key all
  and Purge orphans are in the GH Stack panel.

The authoritative roadmap and scope are in [docs/plan.md](docs/plan.md).

## Requirements

- Blender 5.2 (Python 3.14). The add-on declares the `network` permission — it
  listens on localhost — so Blender's **Online Access** must be on
  (Preferences > System); with it off the panel says so and nothing listens.
- **Rhino 8 with Grasshopper** to run real definitions, under wine or Windows
  (mac needs testing). Development and testing happen on one Linux machine; the
  build scripts assume `~/.local/share/wine-rhino` and a `rhino8` wine prefix,
  both overridable with the `WINE` and `WINEPREFIX` environment variables.
  Mono's `csc.exe` (ships with wine) builds the plugin.

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

## Install

Release path: the add-on ships as a Blender extension with the official
pure-Python `websockets` wheel.

```
scripts/build-extension          # builds dist/gh_stack-<version>.zip
scripts/install-extension        # installs and enables it in Blender

grasshopper/build/build-gha.sh   # build GhStack.gha (Rhino 8)
grasshopper/deploy/deploy.sh     # copy it into the Grasshopper Libraries
```

`scripts/check-deployed` says whether the copy Grasshopper will load is the one
you last built. A `.gha` is scanned only when Rhino starts, so a build is not
live until the copy *and* the restart have both happened; `scripts/test-gha`
prints the same warning when the deployed plugin is stale.

Development path: `scripts/install-addon` symlinks the working tree into
Blender's user scripts (no wheel; the vendored copy under `vendor/` is used).
Both installs appear as **GH Stack** in Preferences > Add-ons.

Then follow [DEMO.md](DEMO.md). The server listens on port 59130 (configurable in
the add-on preferences).

## Working on it

```
scripts/test-core        # layer A: pure Python core, no Blender needed
scripts/test-blender     # layer B: Blender headless, against tools/fake_gh
scripts/test-gha         # layer C: the C# codec under wine, the same golden fixtures
scripts/test-extension   # layer D-lite: builds, installs into a throwaway config, round trip

python3 scripts/make-fixtures   # regenerate protocol/fixtures after a protocol change
```

CI runs the pure-core layer only. Layers B and C run on the development
machine, which is Linux: Blender 5.2 headless for layer B, and a wine prefix
with Rhino 8 installed for layer C (`scripts/test-gha`).

## Layout

```
blender/            Blender add-on (extension support) and its tests
  gh_stack/core/    pure Python: codec, hashing, rules, cache, params (no bpy)
  gh_stack/blender/ the bpy layer: server, engine, stack, geometry, UI
  tests/core/       layer A tests
  tests/headless/   layer B tests
grasshopper/        Grasshopper plugin (C#, built with wine csc.exe)
  src/core/         codec, hashing, conversion (RhinoCommon only)
  src/gh/           Stage In, Stage Out, Param components
protocol/fixtures/  golden frames shared by both codecs
tools/fake_gh/      Python stand-in for the GH side, used in Blender tests
spikes/             feasibility scripts and their results behind the plan
scripts/            test and build entry points
docs/
  plan.md           the project plan (authoritative, roadmap)
  protocol.md       protocol v2 specification
  decisions/        decision records
```

## Licence

GPL-3.0-or-later. See [LICENSE](LICENSE).
