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
| `playback` | a live stage, 30 frames, stateful fake output | per-frame send-to-apply latency and one response per frame |

`rtt_ms` includes the fake client's own Python decode, scale, encode and hash.
It is a regression number, not a substitute for a live Rhino solve: the
Grasshopper conversion and definition solve are only present in a live run.

## Budgets

These are the plan's §5A targets, enforced by `scripts/bench` unless
`--no-budget` is passed:

| Check | Budget |
|---|---|
| 100k triangle Blender round trip (read, hash, encode, decode, in-place apply) | ≤ 50 ms |
| 13× instance prefix read + encode | ≤ 100 ms |
| 13× instance repeated apply reuses the node group | required |
| Multi-stage dirty engine pass | ≤ 100 ms |
| Idle engine pass | ≤ 2 ms |
| Live playback frame (no plan budget yet) | ≤ 250 ms |

The 1M case has no latency budget: the plan asks that it evaluate without a
crash or an unbounded memory increase. The report records timings, frame size
and RSS before/after; compare those across runs.

## Live Rhino measurement

A headless fake cannot measure Stage In conversion, the Grasshopper solve or
the definition's own work. For the instance-latency investigation, run the same
reference scene against a live Rhino, with the panel's Diagnostics timing rows
(`prefix · encode · answer · apply`) and one stage at a time:

1. Open the reference `.blend`, confirm both stages are unfrozen and the
   `Stageloom` panel shows the stage keys.
2. Grasshopper must report connected. Use **Push** once to fill the cache.
3. Set **Play Every Frame**, or scrub, and read the panel after each event.
4. Record the four numbers plus the scene's triangle count, instance count and
   definition triangle count in the performance report.

The important split is `answer`: a large `answer` with a small prefix and apply
is Grasshopper's solve or Stage In conversion; a large `prefix` is Blender's
copy and read; a large `apply` is Blender's write.
