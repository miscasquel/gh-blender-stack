# Demo v0 — the working loop

What this shows: a mesh object in Blender driven by a Grasshopper definition,
as a stage in its modifier stack, with a parameter you can edit in Blender.

```
Blender                                     Rhino / Grasshopper (wine)
┌───────────────────────────┐   WebSocket   ┌───────────────────────────────┐
│ Cube                      │◄─────────────►│ Stage In ──► Scale ──► Stage Out│
│   GH · Demo (stage)       │   9130        │                 ▲              │
│   Bevel (native, optional)│               │               Param            │
│ GH Stage Data/helper      │               │                                │
└───────────────────────────┘               └───────────────────────────────┘
```

## What is deliberately not here yet

- One mesh stack object, one GH stage; mid-stack composition (`GH → GN → GH`) is
  not evaluated as a prefix yet (the stage always reads the base mesh).
- Meshes only. Curves, points and SubD arrive with S4/S6.
- No bake, freeze, preview, orphan list, or disk cache (the cache is in memory).
- Undo is not specially handled; the stack records are ordinary properties.
- The `Param` component takes a **Key** input (a demo deviation from the plan's
  signature; the plan assumes one stage per document).
- Text parameters have a plain field (not animatable, by design). A value-list
  parameter (Type 5) draws a dropdown; its output is the selected index.

## Setup

```
scripts/install-addon          # symlinks the add-on into Blender's user addons
grasshopper/build/build-gha.sh # builds GhStack.gha
grasshopper/deploy/deploy.sh   # installs it into the Grasshopper Libraries
```

Enable **GH Stack** in Blender's Preferences > Add-ons (search `gh_stack`). The
panel is in the 3D viewport sidebar (**N**) under the **GH Stack** tab. The server
starts with the add-on on port 9130 (add-on preferences).

## The loop

1. **Blender.** Select the default cube. In the GH Stack panel press **Add Stage**.
   A stage appears with a key like `stage-3f9a2c`, a hidden helper object
   (`GH Stage Data` collection) and a Geometry Nodes modifier named `GH · Stage`.

2. **Grasshopper.** In Rhino, open Grasshopper. From the **GH Stack** tab place:
   - one **Stage In**
   - one **Stage Out**
   - one **Param**

   Wire them:
   - `Stage In.Geometry` → `Scale.Geometry`
   - `Scale.Geometry` → `Stage Out.Geometry`
   - `Param.Value` → `Scale.Factor`

   Put a **Panel** on the canvas containing the stage key text, and connect that
   Panel to `Stage In.Key`, `Stage Out.Key` and `Param.Key`. The Param defaults
   are fine as they are: label `parameter`, type 0 (float), default 1.0, and no
   min/max until you give one.

3. **Watch Blender.** Within a tick the stage goes to `ok`, the object shows the
   scaled mesh, and the panel gains a **Scale** slider. Drag it: the mesh follows.
   Edit the base mesh (in Edit Mode): the new geometry goes over on the next tick.

4. **Native modifiers still compose.** Add a Bevel modifier after `GH · Stage`:
   it acts on the GH output.

## What to watch for (the point of the demo)

- Does the panel tell you what you need? (state, key, where to copy it, errors)
- Is the parameter slider where you expect it, and does dragging feel immediate?
- Does the stage behave like a modifier (enable/disable, order, delete)?
- What is confusing or missing for a real workflow?
