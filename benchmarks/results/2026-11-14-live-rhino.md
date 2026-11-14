# 2026-11-14 — Live Rhino / Blender 5.3.0 instance baseline

First live measurement of the instance-heavy case with the 0.6.0 add-on and
`Stageloom.gha` 0.6.0. Raw report:
[`2026-11-14-live-rhino.json`](2026-11-14-live-rhino.json).

Environment: Blender 5.3.0, Stageloom 0.6.0 extension, `Stageloom.gha` 0.6.0 in
Rhino 8 under wine, Linux 7.3.4 (CachyOS), AMD Ryzen 7 7700X. The Grasshopper
document was the minimal passthrough `Stage In.Geometry -> Stage Out.Geometry`,
with no Param or other definition work, so the numbers isolate the bridge, the
Stage In/Out conversions and the block round trip.

Scenes:

- `cube.blend`: one small mesh with a stage, the fixed-overhead baseline.
- `instances-13.blend`: one 99,458-triangle definition placed 13× through
  Instance-on-Points, with the stage after the instancing modifier.

## Method

`benchmarks/live_scene.py` built and saved the scenes. `benchmarks/live_client.py`
ran inside Blender 5.3 with the installed 0.6.0 extension, started the server,
waited for the Rhino client, and forced a fresh full request per sample. Rhino
and the Grasshopper graph were driven through the MCP.

Each sample records:

- **`total_ms`** — wall clock from `request_again()` to the applied answer. This
  is the stage update, comparable to the 100 ms target.
- **`rtt_ms`** — send to answer, the panel's "answer" timing.
- **`prefix_ms` / `encode_ms`** — the engine's own fields.

`prefix_ms` only covers the timed `_prefix_sections()` call; the clone cleanup
and its view-layer update happen after the read, so it is near zero here. The
benchmark matrix's cold-prefix number (10.5 ms read + encode) remains the
Blender-side reference.

## Results

Ten samples per configuration.

| Run | total min / median / p95 / max (ms) | rtt median (ms) |
|---|---|---|
| cube, preview on | 17.9 / **31.4** / 34.4 / 55.3 | 30.0 |
| instances-13, preview on | 53.4 / **60.6** / 64.5 / 68.6 | 47.4 |
| instances-13, preview off | 57.9 / **62.8** / 72.2 / 74.7 | 49.4 |

**Split.** With the cube baseline subtracted:

- Blender-side remainder: ~1.4 ms for the cube, ~13.2 ms for the 13× instance
  case, which matches the benchmark's ~10.5 ms prefix+encode plus ~2.4 ms apply.
- Grasshopper round trip: ~30.0 ms for the cube, ~47.4 ms for the 13× instance
  case. The instance-specific part is therefore roughly 17 ms in Grasshopper and
  12 ms in Blender.

## What this says about the 300–800 ms report

The reference scene does **not** reproduce the old 300–800 ms instance latency.
With preview on, the whole stage update is **60.6 ms median** (64.5 ms p95,
68.6 ms max), inside the plan's 100 ms target. Turning Stage In preview off does
not help; it is slightly slower within run-to-run noise (62.8 ms median). The
remaining time is the fixed Grasshopper scheduling/answer cost plus the 13×
block conversion, not the Rhino display.

The original 300–800 ms report came from a different, heavier scene
(FiraTower). That file is not in this repository, so it could not be re-run. The
reference case is now measured and inside budget; a re-run against the original
file is the only remaining way to explain that specific report.

## Caveats

- The Grasshopper definition is a minimal passthrough. A real definition's solve
  time is additional.
- The cube baseline still pays the plugin's fixed scheduling cost (~30 ms), so
  it is an overhead baseline, not a zero-cost point.
- Numbers are machine-specific. Compare runs on the same machine and Blender
  build.
- The live `prefix_ms` field under-reports the cold prefix, as described above;
  use `total_ms` for the stage update and the benchmark matrix for the
  Blender-side split.

## Reproduce

```bash
# scenes
blender -b --factory-startup --online-mode \
  --python benchmarks/live_scene.py -- \
  --out /tmp/opencode/stageloom-live --kind both

# start Rhino, load Stageloom.gha, build Stage In -> Stage Out with the scene key
# then run one configuration:
blender -b /tmp/opencode/stageloom-live/instances-13.blend --online-mode \
  --python benchmarks/live_client.py -- \
  --out /tmp/opencode/stageloom-live/instances-preview-on.json \
  --label "instances-13 preview-on" --samples 10
```
