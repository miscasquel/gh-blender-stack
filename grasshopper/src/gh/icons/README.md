# Component icons

Rasterised from the official Blender icon set, so the Grasshopper components and
the Blender panel share one visual language.

- Source: `release/datafiles/icons_svg/<name>.svg` in the Blender repository
  (<https://github.com/blender/blender>), the same set published at
  <https://ui.blender.org/icons>.
- License: the icons are part of Blender (GPL-2.0-or-later); this project is
  GPL-3.0, and the icon set is published for reuse with attribution. The SVG
  sources are kept here alongside the PNGs.
- Recolour: the glyphs are white for Blender's dark theme; GH's canvas is light,
  so they are recoloured to `#2f3338`.

Regenerate:

```
rsvg-convert -w 48 -h 48 <name>.svg -o tmp.png
magick tmp.png -fill '#2f3338' -colorize 100 +dither -colors 64 <name>.png
magick <name>.png -resize 24x24 <name>.png
```

Used by:

| Icon | Component |
|---|---|
| `import` | Stage In |
| `export` | Stage Out |
| `driver` | Param |
| `geometry_nodes` | the spike geometry component |
