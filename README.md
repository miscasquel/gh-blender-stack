# GH Stack

Drive Grasshopper definitions from Blender as stackable modifiers, next to native Geometry Nodes modifiers, in one visible object per stack. Geometry and parameters both flow through the stack, so parameters can be keyframed, driven, and baked.

**Status:** milestone M0 (repository skeleton). No runnable add-on or plugin yet. Next is spike S1, the gate for the transport.

## How it fits together

- **Blender 5.2** runs the add-on (`blender/`): a WebSocket server, the stack engine, and the parameter properties.
- **Grasshopper / Rhino 8** runs under wine. The plugin (`grasshopper/`, `GhStack.gha`) is a WebSocket client with Stage In, Stage Out, and Param components.
- Both sides speak the protocol in `docs/protocol.md`.
- The project runs entirely on one machine. Portal (`Portal.Gh` and the patched Portal.blender) stays installed for ad-hoc work and shares nothing with this project.

## Layout

```
blender/            Blender add-on (extension) and its tests
  gh_stack/core/    pure Python: codec, hashing, rules, cache (no bpy)
  tests/core/       layer A tests
grasshopper/        Grasshopper plugin (C#, built with wine csc.exe)
protocol/fixtures/  golden frames shared by both codecs
tools/fake_gh/      Python stand-in for the GH side, used in Blender tests
spikes/             feasibility scripts and results behind the plan
scripts/            test and build entry points
docs/
  plan.md           the project plan (authoritative)
  protocol.md       protocol v2 draft
  decisions/        decision records
```

## Working

Run the pure-core tests:

```
scripts/test-core
```

Plans and decisions: `docs/plan.md`, `docs/decisions/`.

## Licence

GPL-3.0. See `LICENSE`.
