# Stageloom — extensions.blender.org submission packet

**Status:** deferred. Stageloom 0.4.0 ships on GitHub first; this packet is
retained for a later platform upload.

Paste-ready copy for the upload. Version **0.4.0**, extension id
**`stageloom`**. Stageloom is the new name for GH Stack.

## Listing fields

- **Name:** Stageloom
- **Tagline:** Grasshopper definitions as stackable modifiers in Blender
- **Type:** Add-on
- **License:** GPL-3.0-or-later
- **Tags:** Geometry Nodes, Pipeline, Modeling
- **Website:** https://github.com/miscasquel/gh-blender-stack
- **Support:** https://github.com/miscasquel/gh-blender-stack/issues
- **Blender:** 5.2.0 or newer
- **Platforms:** all (the add-on is pure Python; the wheel is platform-neutral)

## Short description

Drive Grasshopper definitions from Blender as stackable modifiers. Geometry and
parameters flow both ways over a loopback connection; parameters can be
keyframed, driven and played back, and frames replay with Grasshopper closed.

## Long description

Stageloom puts Grasshopper where it belongs in a Blender stack: next to native
Geometry Nodes modifiers, in one visible object. A stage sees everything before
its own modifier, so `GH → Geometry Nodes → GH` chains evaluate as a prefix, and
a waiting stage pauses the ones after it. Duplicate a stack and the copy gets its
own stage key, cache, node group and helpers, with its parameters and animation.

- Meshes, curves, point clouds, SubD cages with crease tags, Breps (meshed, lossy)
- Named attributes in both directions, including `CURVE`/`POINT` curve attributes
- Grasshopper block definitions and nested instances, both ways
- Parameters with ranges, value lists, keyframes and drivers
- Pull, Freeze, Key all, Purge orphans, a bounded disk cache, and Diagnostics
- Loopback WebSocket only; no external service and no document writes at solve time

**Requirements.** Blender 5.2+ and Rhino 8 + Grasshopper for real definitions.
Enable Online Access in Blender (Preferences > System) so the add-on may listen
on localhost. The Grasshopper companion is built from the same GPL-3.0-or-later
repository (`grasshopper/`); the `.gha` is on the GitHub release.

**Without Rhino.** The Blender half still runs and is inspectable. Install the
extension, enable Online Access, add a stage, then run the bundled stand-in with
the stage key from the panel:

```sh
python3 tools/fake_gh/fake_gh.py --stage <stage key>
```

The stage goes from `disconnected` to `ok`, and `--report <path>` writes the
frames it received and sent to JSON. `scripts/test-blender` drives the same tool
across the engine surface.

**Known limitations.** Breps/surfaces arrive as meshes (lossy, warned); the frame
cache format is per machine and disposable; instance-heavy definitions still need
a live re-measure; Windows-native Rhino is the target but the tested build path
is wine. The add-on is a working demo, not the full v1 scope.

## Self-containment

The add-on is self-contained: it installs from the extension zip, checks
`bpy.app.online_access`, and reports a clear `disconnected` state when nothing is
connected. The Grasshopper companion is optional for inspecting the add-on and
is open source under GPL-3.0-or-later in the same repository. It communicates
only over `127.0.0.1` (localhost), never an external server. The repository
bundles `tools/fake_gh` so a reviewer can complete the loop without Rhino.

## Release notes (0.4.0)

Stageloom is the new name for GH Stack. This release carries curve and wire-edge
attributes, moves the frame cache to the extension's user data directory, and
prepares the add-on for a future extensions-platform release.

**Upgrade:** remove the old GH Stack add-on and `GhStack.gha` before installing
0.4.0. The extension id, Python package and C# assembly changed, and the old
`.gha` shares component GUIDs. Protocol v2 is unchanged; existing `.blend`
stages, parameters and animation are preserved; helper, scene and modifier names
migrate on load. The old frame cache is not read.

## Test evidence

```text
scripts/test-core        57 tests pass
scripts/test-blender     204 tests pass (rebrand migration + offline gate)
scripts/test-gha         12 fixtures + attribute store + link round trip + build guard
scripts/test-extension   installed extension round trip: pass
uvx ruff check --output-format=concise --ignore=E402,E741,E731 .
blender --command extension validate dist/stageloom-0.4.0.zip
```

The extension build validates the source and the artifact, pins the
`websockets` wheel digest, and refuses `vendor/`, `.so`, `.exe`, `.pyc`, `.pyo`,
`__pycache__` and stray zips. `scripts/test-extension` installs the built zip
into a throwaway Blender config and completes the round trip from the installed
location.

## Upload checklist (deferred)

- [ ] Platform upload resumed by the repository owner.
- [ ] `dist/stageloom-0.4.0.zip` built and validated.
- [ ] Icon and three screenshots uploaded (`docs/listing/`).
- [ ] Name, tagline, description, tags, support URL and release notes entered.
- [ ] First upload submitted; moderation questions answered.
- [ ] On acceptance, install from the platform in a clean config and repeat the
      `fake_gh` round trip.
