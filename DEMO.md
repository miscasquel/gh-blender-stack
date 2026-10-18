# Demo v0 — the working loop

What this shows: a mesh object in Blender driven by a Grasshopper definition,
as a stage in its modifier stack, with a parameter you can edit in Blender.

```
Blender                                     Rhino / Grasshopper (wine)
┌───────────────────────────┐   WebSocket   ┌───────────────────────────────┐
│ Cube                      │◄─────────────►│ Stage In ──► Scale ──► Stage Out│
│   GH · Demo (stage)       │   59130       │                 ▲              │
│   Bevel (native, optional)│               │               Param            │
│ (GH Stage scene: helpers) │               │                                │
└───────────────────────────┘               └───────────────────────────────┘
```

## What is deliberately not here yet

- A Rhino Brep or surface is meshed on the way out (warned); Blender has no
  B-rep. A Blender surface object is meshed on the way in, the same loss in the
  other direction.
- No freeze, preview, or orphan list yet. The frame cache writes to disk
  (`~/.local/share/gh-stack/`) and fills as you play and scrub; there is no bake
  operator.
- Undo is not specially handled; the stack records are ordinary properties.
- The `Param` component takes a **Key** input (a demo deviation from the plan's
  signature; the plan assumes one stage per document).
- Text parameters have a plain field (not animatable, by design). A value-list
  parameter (Type 5) draws a dropdown; its output is the selected index.

## Geometry kinds

- **Stage In** outputs one generic `Geometry` list: the mesh, the SubD cage,
  curves, and points, in that order. One wire carries everything the stage
  returns, mixed kinds included.
- **Stage Out** takes one generic `Geometry` input: mesh, curve, point cloud, SubD,
  or Brep. Everything is converted to the protocol's sections.
- **SubD** is a control cage with edge-crease tags. Flag a mesh stack object
  with **Base is a SubD cage** in the panel and Blender sends `subd.*`; the GH
  side builds a SubD from the cage and applies the tags. Semi-sharp and vertex
  creases are not available from RhinoCommon, and the stage says so.
- **Curves** are exact by construction: `MakePiecewiseBezier` splits them into
  single rational Bezier spans, each of which Blender holds exactly as a NURBS
  spline with its weights. Poly, NURBS of any order 2–6, Blender Bezier splines,
  circles, ellipses, and arcs of any angle all round-trip exactly. Only curves
  the split cannot carry (degree above 5, periodic) are fitted with cubic
  Beziers and warned with the measured deviation (D3).
- **Points** arrive as one point cloud per stage, which is the geometry that can
  carry point colours and attributes. Use **Point Cloud Points** (GH Stack tab)
  to explode it into a point list.
- **Attributes** are Blender's named attributes (any type, any mesh domain:
  floats, ints, booleans, strings, vectors, colours, quaternions, matrices).
  They travel with the geometry both ways: put one on a base mesh (Mesh
  properties → Attributes) and Stage In lists its name on the `Attributes`
  output, **Attribute Get** reads its values in Grasshopper; **Attribute Set**
  attaches one the graph computed, and Stage Out writes it back into the helper
  mesh. The active colour attribute and the edge creases keep their native
  Blender channels. If `Attribute Get` cannot find a name it lists what the
  geometry does carry.

## Setup

```
scripts/build-extension        # builds dist/gh_stack-<version>.zip
scripts/install-extension      # installs and enables the extension in Blender

grasshopper/build/build-gha.sh # builds GhStack.gha
grasshopper/deploy/deploy.sh   # installs it into the Grasshopper Libraries
```

For development, `scripts/install-addon` symlinks the working tree instead (no
wheel; the vendored websockets copy is used). The add-on appears as **GH Stack**
in Blender's Preferences > Add-ons. The panel is in the 3D viewport sidebar
(**N**) under the **GH Stack** tab. The server starts with the add-on on port
59130 (add-on preferences; 9130 through 0.2.2, where BlenderKit sits).

## The loop

1. **Blender.** Select the default cube. In the GH Stack panel press **Add Stage**.
   A stage appears with a key like `stage-3f9a2c`, a hidden helper object (in the
   dedicated **GH Stage** scene's `GH Stage Data` collection, so your own scene
   stays clean) and a Geometry Nodes modifier named `GH · Stage`.

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
