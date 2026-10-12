# GH Stack

Drive Grasshopper definitions from Blender as stackable modifiers, next to
native Geometry Nodes modifiers, in one visible object per stack. Geometry and
parameters both flow through the stack, so parameters can be keyframed, driven,
and baked.

That is the v1 goal. **v0.1.0 is a working demo**: a mesh in Blender driven by a
real Grasshopper definition, with parameters you can edit and keyframe in
Blender. [DEMO.md](DEMO.md) walks through the loop.

```
Blender                                          Rhino / Grasshopper (wine)
┌───────────────────────────┐      WebSocket     ┌────────────────────────────────┐
│ Cube                      │◄──────────────────►│ Stage In ──► Scale ──► Stage Out│
│   GH · Demo (stage)       │        9130        │                 ▲              │
│   Bevel (native, optional)│                    │               Param            │
└───────────────────────────┘                    └────────────────────────────────┘
```

## What works today

- **The loop.** Geometry goes to Grasshopper, a definition runs, the result comes
  back and shows through a modifier in the stack. Base-geometry edits flow on the
  next tick; no buttons.
- **Geometry kinds.** Meshes (n-gons and vertex colours), curves (poly, Bezier,
  NURBS, circles, ellipses, arcs — all exact), points, SubD control cages with
  edge-crease tags, and Breps/surfaces (meshed on the way out, warned). Stage In
  outputs `Geometry`, `Curves`, `Points`, and `SubD`; Stage Out takes any of
  them on one generic input.
- **Parameters.** `Param` components declare float, int, bool, vector3, colour,
  value list, and text parameters. Blender draws them in the N-panel with
  ranges, a value-list dropdown, keyframe decorators, and drivers.
- **Protocol v2.** Binary frames with a canonical JSON header; geometry and
  parameters in the same stream. The Python and C# codecs are checked against
  ten shared golden fixtures.
- **Lifecycle.** States (`waiting`, `ok`, `error`, `disconnected`, `disabled`),
  timeouts, latest-wins requests, an in-memory cache, and reconnect with backoff.
- **Tests.** `scripts/test-core` (50 tests, standard library only),
  `scripts/test-blender` (39 headless tests), `scripts/test-gha` (the C# codec
  against the golden fixtures, under wine).

## What is deliberately not here yet

- One stage per object, and a stage always reads the stack's base geometry.
  Mid-stack composition (`GH → GN → GH`) is not evaluated as a prefix yet.
- Breps and surfaces arrive as meshes (lossy, warned); Blender surface objects
  are meshed on the way in. There is no B-rep on the wire.
- The cache is in memory. No bake, freeze, preview, or orphan list yet.
- The add-on installs as a symlink (`scripts/install-addon`); packaging it as a
  Blender extension is planned (S8).
- Undo is not special-cased; stage records are ordinary properties, so undo
  works as Blender's own property undo.

The authoritative roadmap and scope are in [docs/plan.md](docs/plan.md).

## Requirements

Development and testing happen on one Linux machine; Rhino 8 and Grasshopper
run under wine there.

- Blender 5.2 (Python 3.14).
- Rhino 8 with Grasshopper under wine. The build scripts assume
  `~/.local/share/wine-rhino` and a `rhino8` wine prefix; both are overridable
  with the `WINE` and `WINEPREFIX` environment variables. Native Windows Rhino
  is designed for but untested.
- Mono's `csc.exe` (ships with wine) to build the plugin.

## Install

Release path: the add-on ships as a Blender extension with the official
pure-Python `websockets` wheel.

```
scripts/build-extension          # builds dist/gh_stack-<version>.zip
scripts/install-extension        # installs and enables it in Blender

grasshopper/build/build-gha.sh   # build GhStack.gha (needs wine + Rhino 8)
grasshopper/deploy/deploy.sh     # copy it into the Grasshopper Libraries
```

Development path: `scripts/install-addon` symlinks the working tree into
Blender's user scripts (no wheel; the vendored copy under `vendor/` is used).
Both installs appear as **GH Stack** in Preferences > Add-ons.

Then follow [DEMO.md](DEMO.md). The server listens on port 9130 (configurable in
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

GPL-3.0. See [LICENSE](LICENSE).
