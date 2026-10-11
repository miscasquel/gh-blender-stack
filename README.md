# GH Stack

Drive Grasshopper definitions from Blender as stackable modifiers, next to native Geometry Nodes modifiers, in one visible object per stack. Geometry and parameters both flow through the stack, so parameters can be keyframed, driven, and baked.

**Status:** M2 done (2026-10-11). The pure protocol core exists in Python and C# and both pass the same golden fixtures. Spikes S1 (transport) and S2 (Blender background thread) have passed; S3 (geometry round trip) is next. No runnable add-on or plugin yet.

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

Run the pure-core tests (Python, no Blender):

```
scripts/test-core
```

Check the C# core against the same golden fixtures, under wine:

```
scripts/test-gha
```

Regenerate the fixtures after a protocol change, and commit the diff:

```
python3 scripts/make-fixtures
```

Plans and decisions: `docs/plan.md`, `docs/decisions/`.

## Licence

GPL-3.0. See `LICENSE`.
