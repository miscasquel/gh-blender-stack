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

**All rounds ran 2026-10-16.** Round 1: attributes pass — the store survives
every operation GH performs. Round 2: no graph-side identity through the
constructor or `Cast` — which round 3d traced to the missing **Name**. Round 3
(3–3f): the instances path live, first through document definitions, then, on
the owner's pointer to the Model Block Definition component, as **named
graph-side definitions**: valid, previewing and baking with no document write at
solve time, nested blocks included; Rhino materializes the blocks at bake and
reuses them. The report (`spikes/results/s9.txt` §D) records the two product
bugs the rounds caught (attribute attach order against the document copy; a
block-instance `ModelObject` refusing the geometry cast) and the probe bug (a
matrix written in the wrong layout, which twice looked like a bbox limitation).

The dialog that appeared during every run was the probe's own bake of the
round-1 invalid reference; those bakes are now skipped for invalid references,
so a warning-free run is the control that nothing else raises it.

The checks are in `spikes/s9/S9Checks.cs`; the deployed spike `.gha` carries
rounds 3 through 3f (`checks_revision: 3f …`). To re-run: restart Rhino
(Libraries load at start), then the driver, then drop the **S1 Spike**
component.
