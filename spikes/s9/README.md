# S9: attributes on the geometry, and blocks without a document

**Question (asked 2026-10-15, before the attributes and instances work):**

1. **Attributes.** If named per-element values ride on a geometry object's
   `UserDictionary` (the native RhinoCommon store), do they survive the
   operations Grasshopper components actually perform — duplicate, transform,
   GH goo duplication, bake — for every geometry kind (mesh, curve, point
   cloud, SubD)? If they do not, the whole "attributes travel with the geometry"
   design is wrong and a fallback (custom `UserData`, or a side channel) has to
   be chosen before the product code is written.
2. **Blocks.** Can a block definition be built graph-side
   (`ModelObject` → `ModelInstanceDefinition` → `GH_InstanceReference`) and
   previewed without a document definition, so Stage In can hand Blender's
   instances to Grasshopper as real blocks without writing to the document? And
   what does `BakeGeometry` do when the definition is not in the document?

## How it runs

The checks execute **inside Rhino** (`spikes/s9/S9Checks.cs`, bundled into the
spike `.gha` built by `spikes/s1/build.sh`), because RhinoCommon's native half
only loads under Rhino. Everything runs on the UI thread
(`RhinoApp.InvokeAndWait`); the bake probe records what the document gained and
deletes it again.

```
# build the spike .gha with the S9 checks (idempotent)
spikes/s1/build.sh
# install it into the Grasshopper Libraries folder
spikes/s1/deploy.sh
# restart Rhino (Grasshopper scans Libraries only on load)

# terminal 1: the driver listens on the spike endpoint
PYTHONPATH=blender/gh_stack/vendor python3 spikes/s9/driver.py
# then drop the "S1 Spike" component on the canvas (or toggle Enabled),
# as in S6: http://localhost spikes/s1/README.md
```

Raw transcript: `spikes/results/s9-run.txt`. The curated report is
`spikes/results/s9.txt` (a timed-out run must not overwrite it).

## Status

**Round 1 ran 2026-10-16 and passed.** Attributes: the `UserDictionary` survives
every operation GH performs (duplicate, transform, goo duplication, bake) for
mesh, curve, point cloud and SubD — the design's premise holds. Blocks: a
graph-side definition built from `Attributes` is invalid without an identity,
does not preview, and cannot be baked (Rhino raises its "invalid block instance"
warning from the canvas) — the public constructor path cannot supply an Id or
content, so round 2 tries `ModelInstanceDefinition.Cast` and verifies the
document-definition fallback. Full report: `spikes/results/s9.txt`.

The checks are in `spikes/s9/S9Checks.cs`; the deployed spike `.gha` carries
round 2's probes (`definition_cast`, `doc_reference_valid`, `doc_reference_bbox`,
`doc_reference_bake`). To re-run: restart Rhino (Libraries load at start), then
the driver, then drop the **S1 Spike** component.
