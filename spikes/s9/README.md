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

**Checks written and compiled; the live run is pending** (2026-10-15). The
results decide one fallback, so treat the attributes and blocks designs as
API-backed but not yet behaviour-confirmed until `s9.txt` exists.
