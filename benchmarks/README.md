# Benchmarks

The reference performance matrix for the Blender side of Stageloom. It is
separate from `blender/tests/`: the unit suite checks correctness, this matrix
checks latency and memory. Runs take minutes and their numbers are
machine-specific, so they are not part of `scripts/test-blender`.

```bash
scripts/bench                      # all scenarios, budget gate on
scripts/bench --quick              # skip the 1M-triangle case
scripts/bench --scenario instances # one scenario (repeatable)
scripts/bench --out /tmp/bench.json
scripts/bench --no-budget          # measure without failing on a budget
```

The harness runs inside `blender -b --factory-startup --online-mode` and uses
the working tree through `sys.path`; it does not need an installed add-on. For
the wire scenarios it starts `tools/fake_gh/fake_gh.py` on a free loopback port,
so the WebSocket server, codec, bridge and timer are the real ones.

## What is measured

| Scenario | Shape | What it separates |
|---|---|---|
| `pipeline` | 10k, 100k and 1M triangle quad grids | mesh read, geometry hash, frame encode, decode, topology rebuild, in-place update |
| `instances` | one 100k-triangle definition placed 13× through Instance-on-Points | prefix read, encode, first apply, repeated apply, node-group reuse |
| `roundtrip` | one 100k stage through a real socket to fake-gh | full and parameter-only (`ref`) request latency, with `prefix_ms`, `encode_ms`, `rtt_ms` and `apply_ms` |
| `multistage` | three 10k stages on one object | one dirty engine pass, full chain settle, idle pass |
| `playback` | one stage, 30 frames, stateful fake output | per-frame send-to-apply latency and one response per frame |

`rtt_ms` includes the fake client's own Python decode, scale, encode and hash.
It is a regression number, not a substitute for a live Rhino solve: the
Grasshopper conversion and definition solve are only present in a live run.

## Budgets

These are the plan's §5A targets, enforced by `scripts/bench` unless
`--no-budget` is passed:

| Check | Budget |
|---|---|
| 100k triangle Blender round trip (read, hash, encode, decode, in-place apply) | ≤ 50 ms |
| 100k triangle in-place apply (same topology token) | ≤ 4 ms |
| 1M triangle in-place apply (same topology token) | ≤ 30 ms |
| 13× instance prefix read + encode | ≤ 100 ms |
| 13× instance repeated apply (same topology token) | ≤ 4 ms |
| 13× instance repeated apply reuses the node group | required |
| Multi-stage dirty engine pass | ≤ 100 ms |
| Idle engine pass | ≤ 2 ms |
| Live playback frame (no plan budget yet) | ≤ 250 ms |

The in-place budgets exist because the token fast path is what keeps a repeat
apply from reading every polygon and loop: the Q9 verified path measured 6.4 ms
at 100k and 69 ms at 1M on the reference machine, while the token path returns
to the v0.6 2.15 ms / 22.3 ms baseline. The 4 ms and 30 ms ceilings are guards
with headroom, not targets. The response token covers definition parts too, so
the instance repeated-apply budget is the same 4 ms guard around the 2.45 ms
v0.6 baseline; the Q9 verified path measured 6.98 ms there.

**0.8 regression policy:** the baseline gate fails on any metric more than
**5% slower** than its stored p50/p95. A regression of 0–5% is allowed only
when the same task records a quantified offsetting gain (another benchmark,
production-code reduction, test/runtime improvement, or an accepted
correctness/simplicity benefit). An unoffset regression is a failure.

The 1M case has an in-place-apply guard but no full-pipeline latency budget:
the plan asks that it evaluate without a crash or an unbounded memory increase.
The report records timings, frame size and RSS before/after; compare those
across runs.

## Live Rhino measurement

A headless fake cannot measure Stage In conversion, the Grasshopper solve or
the definition's own work. The live half has two scripted entry points:

```bash
# Build the reference scenes and print their stage keys.
blender -b --factory-startup --online-mode \
  --python benchmarks/live_scene.py -- \
  --out /tmp/opencode/stageloom-live --kind both

# Measure one scene through a live Grasshopper connection. Run it with the
# installed 0.7.0 extension and an enabled Online Access preference.
blender -b /tmp/opencode/stageloom-live/instances-13.blend --online-mode \
  --python benchmarks/live_client.py -- \
  --out /tmp/opencode/stageloom-live/instances-preview-on.json \
  --label "instances-13 preview-on" --samples 10
```

Build a minimal `Stage In.Geometry -> Stage Out.Geometry` definition in Rhino
with the scene's stage key, then run the client. `total_ms` is the stage update
from the forced request to the applied answer; `rtt_ms` is send-to-answer, the
panel's "answer" field. Add a real definition's components only when measuring
the solve itself.

The first live reference run is in
[`results/2026-11-14-live-rhino.md`](results/2026-11-14-live-rhino.md): 13 ×
99,458 triangles cost 60.6 ms median end to end with preview on, inside the
100 ms target. The manual panel path is still useful for a GUI session:

1. Open a generated `.blend`, confirm both stages are unfrozen and the
   `Stageloom` panel shows the stage keys.
2. Grasshopper must report connected. Use **Push** once to deliver the current
   stage state.
3. Set **Play Every Frame**, or scrub, and read the panel after each event.
4. Record the four numbers plus the scene's triangle count, instance count and
   definition triangle count in the performance report.

The important split is `answer`: a large `answer` with a small prefix and apply
is Grasshopper's solve or Stage In conversion; a large `prefix` is Blender's
copy and read; a large `apply` is Blender's write.
