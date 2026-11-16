# 0006 — Carried decisions

**Status:** Accepted, 2026-10-10. Data-root scope amended by
[0016](0016-live-only-stages.md), 2026-11-16: the custom frame cache is gone.

These decisions were settled earlier in the planning discussion. They are recorded here so the reasons travel with the code.

## No file protocol

The earlier design exchanged `.3dm` files and polled them. Polling and atomic file exchange cannot meet the latency target. The live WebSocket protocol (ADR 0001) replaces it. Persistence is the `.blend` plus an optional native bake.

## No rhino3dm in Blender

All Rhino work happens on the Grasshopper side, which has RhinoCommon. Geometry travels as arrays. This avoids vendoring platform-specific wheels in the Blender extension. The one remaining rhino3dm route, for SubD sharpness, is ruled out by ADR 0002.

## No GhPython

GhPython and hand-built `.ghx` documents failed under wine. The plugin is compiled C# (`csc.exe`, C# 5).

## GPL-3.0

The project is licensed under GPL-3.0. Portal's Apache-2.0 code is compatible with that, and Portal is not copied into this repository.

## Blender extension packaging

The add-on ships as a Blender extension (`blender_manifest.toml`). `websockets` is pure Python, so one package covers Linux and Windows. Fallback: a plain `vendor/` directory, as Portal does.

## Data root

Logs live under the add-on's data root (`~/.local/share/stageloom/` for the
development install; the extension's user directory for the packaged add-on).
In wine the legacy path was `Z:\home\<user>\.local\share\gh-stack\`. Native bake
data belongs to Blender, not the add-on data root.
