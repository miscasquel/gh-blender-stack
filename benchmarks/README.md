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
scripts/bench-baseline            # gate: 3 sequential runs, median p50/p95 vs baseline
scripts/bench --check-baseline     # one-run comparison against the stored baseline
scripts/bench --require-baseline   # reference-machine mode: fail when not comparable
scripts/bench --check-baseline-only --report /tmp/bench.json  # compare an existing report
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

## Stored baseline gate

The 0.8.0 baseline is `results/2026-11-16-linux.json`, built from three solo
runs on the quiet reference machine: `results/2026-11-16-linux-run1.json`,
`…-run2.json` and `…-run3.json`. The raw reports and their SHA-256 are recorded
in the baseline, so every aggregate can be recomputed.

Runs 1 and 3 were tight; run 2 was a global outlier, roughly 20–30% slower on
CPU-bound metrics (for example, `pipeline.100k.extract_ms` p50 8.19 ms against
4.97/4.96 ms). The documented median-of-three rule stores the median of each
metric's three per-run percentiles, so the ambient-load/frequency episode cannot
become the baseline and an optimistic p95 from the fastest run cannot either.
The median baseline was rebuilt from the same three source runs; the fresh
three-run gate and its measured values are recorded in the performance ledger.

```bash
# one baseline from three sequential runs (never run another gate alongside them)
scripts/bench --out benchmarks/results/2026-11-16-linux-run1.json
scripts/bench --out benchmarks/results/2026-11-16-linux-run2.json
scripts/bench --out benchmarks/results/2026-11-16-linux-run3.json
python3 benchmarks/baseline.py build \
  --run benchmarks/results/2026-11-16-linux-run1.json \
  --run benchmarks/results/2026-11-16-linux-run2.json \
  --run benchmarks/results/2026-11-16-linux-run3.json \
  --out benchmarks/results/2026-11-16-linux.json --created 2026-11-16
```

The gate is exactly the plan's rule: **a gated metric more than +5% slower than
its stored p50/p95 fails; exactly 5% passes.** A regression of 0–5% is allowed
only when the same task records a quantified offsetting gain (another benchmark,
production-code reduction, test/runtime improvement, or a documented
correctness/simplicity benefit). An unoffset regression is a failure.

`scripts/bench-baseline` is the gate command. It runs `scripts/bench` three
times, sequentially, and compares the median of the three per-run p50/p95 values
with the stored baseline (the baseline uses the same median-of-three rule). One
ambient-load outlier cannot move the result; a sustained code regression moves
all three runs. The per-run values are stored, so the aggregate is auditable.
`scripts/check-all` runs this command, so the full gate measures three times
before it decides.
`bench.py --check-baseline` remains the one-run comparison for a single report;
`--check-baseline-only --report` compares existing reports without measuring.

`benchmarks/baseline.py` is pure standard library and owns the statistics and
comparison; `bench.py` only measures. For each metric the baseline stores:

- `p50`/`p95`: the median of the three per-run percentiles;
- `p50_runs`/`p95_runs`: the three per-run values;
- `samples`: the pooled raw samples from all three runs.

`percentile()` is linear interpolation (Hyndman–Fan type 7), used identically on
the baseline and the current run. The comparison treats a value within `1e-12`
relative of the limit as exactly at it, so a mathematical +5% does not fail on
IEEE-754 rounding.

### Gated metrics

| Metric | Why it is gated |
|---|---|
| `pipeline.100k.extract_ms` | 100k mesh read |
| `pipeline.100k.decode_ms` | 100k frame decode |
| `pipeline.100k.apply_fresh_ms` | full 100k topology build |
| `pipeline.100k.apply_inplace_ms` | token fast path, 100k |
| `pipeline.100k.total_ms` | read + hash + encode + decode + in-place apply |
| `pipeline.1m.apply_inplace_ms` | token fast path, 1M |
| `instances.13x.prefix_ms` | prefix read for 13 definitions |
| `instances.13x.apply_repeat_ms` | repeated instance apply |
| `playback.frame_ms` | live send-to-apply, p50 and p95 |
| `roundtrip.full.settle_ms` | full request through the real socket |
| `roundtrip.ref.settle_ms` | parameter-only (`ref`) request |
| `multistage.dirty_pass_ms` | dirty engine pass |
| `multistage.settle_ms` | full three-stage chain settle |
| `multistage.idle_pass_ms` | idle timer pass (amortized over 1000 ticks) |

`pipeline.100k.total_ms` is one timed operation that reads, hashes, encodes,
decodes and applies in sequence; it is not a sum of independently warmed phase
timings. The idle pass times 1000 ticks and divides, because one tick is about
three microseconds and a single-tick timer cannot resolve 5%.

The baseline also records the remaining diagnostics, but does not gate them:

- `pipeline.100k.hash_ms`, `pipeline.100k.encode_ms`,
  `instances.13x.encode_ms`, and the round-trip `encode_ms` components are
  sub-millisecond; a 5% change is inside the timer/allocation noise floor and
  cannot be resolved reliably run-to-run.
- `pipeline.1m.extract_ms/hash_ms/encode_ms/decode_ms/apply_fresh_ms` are the
  1M scale case. The plan gives it no full-pipeline budget; the repeat hot path
  is `pipeline.1m.apply_inplace_ms`, which is gated.
- `instances.13x.apply_first_ms` has one sample per run (first definition
  build); the repeated apply is the gated path.
- `roundtrip.*.prefix_ms/encode_ms/rtt_ms/apply_ms` are component diagnostics
  inside the full/ref settle gates. `rtt_ms` includes the fake client's own
  decode, scale, encode and hash.
- The 10k pipeline rows are setup measurements, not the hot path.

No metric is silently dropped: every ungated metric is stored in the baseline
with its reason, and `baseline.py` refuses a baseline whose ungated set disagrees
with the code's `METRICS`.

### Machine identity and CI

The stored numbers are from the development machine. The median-of-three rule
rejects a single background-load spike but cannot remove sustained load: run the
gate without another gate or benchmark, and only on a quiet machine. If the host
is loaded, stop and defer. The gate compares
`processor`, `platform`, `blender` and `python`. On another machine it prints
`[baseline] SKIP: NOT COMPARABLE on this machine` and exits 0; the check-all
summary records “baseline skipped (machine differs)”. `--require-baseline` turns
that into exit 2, which is the mode for the reference machine. CI invokes the
same check through `scripts/check-all --ci`; it honestly reports the skip
instead of claiming a green baseline it cannot measure.

Exit codes: `0` pass or explicit not-comparable skip; `1` gated regression;
`2` not comparable under `--require-baseline`; `3` malformed/missing baseline,
report or gated metric.

### Test the gate itself

`blender/tests/core/test_bench_baseline.py` is part of `scripts/test-core` and
covers identical, +4%, exactly +5%, +6%, p95-only regression, missing/extra
metrics, malformed baselines, machine mismatch and a widened threshold.
`scripts/test-bench-baseline` runs the real `scripts/bench --check-baseline-only`
CLI under Blender and proves the non-zero exit on a temporary +6% baseline
mutation; it is part of the full `scripts/check-all` gate.

## C# parameter/schema microbenchmark

The Blender matrix cannot see the C# parameter path. `scripts/bench-gha` builds
`grasshopper/bench/ParamBench.cs` with the same wine/Mono `csc` environment as
the console suites and writes a JSON result under `benchmarks/results/`:

```bash
scripts/bench-gha benchmarks/results/2026-11-16-linux-csharp.json
```

It measures `Json.FormatDouble` over a representative double corpus
(`1e15`/`1e16`, `-0.0`, integers-as-doubles, shortest-repr values), every
`ParamValues.Cast`/`ParamValues.JsonValue` parameter type, and the canonical
JSON/schema-frame encode for a 100-parameter stage. It is separate from
`StageloomCoreTests.exe`, so normal test runs do not pay for it. The result
measures the pure JSON/parameter layer only; it excludes RhinoCommon/Grasshopper
object allocation, Stage In conversion and the solve.

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
