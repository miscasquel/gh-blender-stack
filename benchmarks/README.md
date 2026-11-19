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
scripts/bench-baseline            # gate: 3 sequential runs, best p50/p95 vs baseline
scripts/bench --check-baseline     # one-run comparison against the stored baseline
scripts/bench --require-baseline   # reference-machine mode: fail when not comparable
scripts/bench --check-baseline-only --report /tmp/bench.json  # compare an existing report
scripts/scale --out /tmp/scale.json  # T10c structural scale run
scripts/soak --out /tmp/soak.json    # T10d 1-hour churn soak
scripts/soak --quick                 # 20 s soak smoke
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

## Scale verification (T10c)

`scripts/scale` is the dedicated T10c scale runner. It is not one of the
scenarios above, is not in `scripts/check-all`, and is not part of the stored
baseline matrix; it is a local/release check with structural assertions.

| Shape | Construction | Structural assertions |
|---|---|---|
| 200 stages | one mesh object with **200 stage records** (one stack, not 200 objects) | 200 record labels in creation order, 200 unique keys, 200 modifiers, a forced index rebuild with 200 keys / 200 owners / no conflicts, `ordered_stages` in record order, a 200-row panel draw with one Pull per stage and no index rebuild, and a no-client engine pass with zero prefix clones |
| 100k triangles | one **exactly 100,000-triangle** mesh (250 × 200 quads = 50,000 quads, 50,451 vertices) | the source, fresh apply and in-place target all report 100,000 loop triangles; read keeps 50,000 faces and 200,000 loop indices; the in-place call runs with `_topology_matches` patched to raise and moves positions without changing the faces |
| 10k instances | one 2-triangle definition on a **10,000-point cloud** | one definition, 10,000 `inst.ref_def` rows all referencing definition 0, 10,000 4×4 transforms with the point translations, one 10,000-point row cloud, one definition collection, 10,000 evaluated instances, one instance reference, and group reuse on the repeated apply |

```bash
scripts/scale --out benchmarks/results/2026-11-19-scale.json
scripts/scale --quick        # 20 stages / 10k triangles / 100 instances smoke
```

The runner runs under `blender -b --factory-startup --online-mode` from the
working tree, records wall times and `VmHWM` RSS where available, and exits
non-zero on a structural failure, exception, crash or its wall-clock guard.
`--quick` is a fast local smoke, not the scale claim.

**2026-11-19 reference run** (`AMD Ryzen 7 7700X`, Blender 5.3.0, Python
3.13.13; 4.58 s total, peak RSS 524.0 MiB, 38/38 structural checks):

| Shape | Measurement | p50 / p95 |
|---|---|---|
| 200 stages | full scene build (200 real `add_stage` calls) | 2,957 ms (one sample) |
| 200 stages | forced 200-key index rebuild | 1.13 / 1.17 ms |
| 200 stages | `ordered_stages` over 200 stages/modifiers | 7.55 / 7.56 ms |
| 200 stages | real N-panel draw, 200 stage rows | 9.69 / 9.78 ms |
| 200 stages | no-client engine pass over 200 stages | 34.64 / 34.66 ms |
| 100k triangles | read (`mesh_to_sections`) | 4.93 / 4.95 ms |
| 100k triangles | fresh apply (`sections_to_mesh`) | 8.97 / 9.05 ms |
| 100k triangles | in-place apply (same topology token) | 2.17 / 2.22 ms |
| 10k instances | cold prefix | 13.63 / 13.99 ms |
| 10k instances | first apply (helpers + definition built) | 84.30 ms (one sample) |
| 10k instances | repeated apply (rows move, group reused) | 81.88 / 91.93 ms |

The timing rows are recorded diagnostics, not a new gate. **Existing baseline
coverage:** `pipeline.100k.apply_inplace_ms` gates the 100k in-place path
(4 ms budget), and `instances.13x.apply_repeat_ms` gates the repeated instance
apply at 13 rows. **Not in the default matrix:** the 200-stage index, panel and
no-client pass, and the 10,000-row instance apply. They are scale evidence; no
5% claim is made against the stored baseline, and this task adds no timing
threshold. The 200-stage no-client pass writes no client prefix (it reports
`prefix_clones` delta 0) and the panel draw is the real `STAGELOOM_PT_panel`
against a recording layout, not a live UI.

The JSON result is
[`results/2026-11-19-scale.json`](results/2026-11-19-scale.json); the same
shapes are asserted without timing in
`blender/tests/headless/test_scale_shapes.py` (part of `scripts/test-blender`).

## Churn soak (T10d)

`scripts/soak` is the dedicated 1-hour soak. It is not one of the scenarios
above, is not in `scripts/check-all`, and makes no timing claim; it proves
churn invariants, not latency.

```bash
scripts/soak --duration 3600 \
  --out benchmarks/results/2026-11-19-soak.json
scripts/soak --quick        # 20 s smoke with the same invariants
```

Each cycle adds a stage and a `fake_gh` client, sends a full and a
parameter-only request, renames the stage and the stack object, runs the
engine's undo/redo handler, disables/enables, freezes/unfreezes, seeds a
two-part definition plus both row-cloud families, and deletes the stage while
asserting that every helper, definition, cloud, parameter, runtime, verify,
prefix-memo and playback-report entry is gone. Every fifth cycle adds and
deletes a whole stack object. Every cycle runs three keyed playback frames;
scheduled work covers a native Geometry Nodes bake over three frames with an
offline output check and Clear Bake, and a full client stop/start reconnect with
a stale answer from the old client. The runner checks the stage registry,
owners/claims/conflicts, pending/sent/dirty/verify/memo maps, helpers and stage
groups, and exits non-zero on an invariant failure, an engine trace error, a
hang past the 60 s per-cycle deadline or the hard wall-clock guard.

**2026-11-19 reference run** (`AMD Ryzen 7 7700X`, Blender 5.3.0, Python
3.13.13; **10,238 cycles**, **3,600.2 s** churn, **0 invariant failures**,
**0 unclassified exceptions**, **30/30** native bakes, **15** reconnects, RSS
**368.6 → 399.0 MiB** peak 399.0, slowest cycle 0.64 s). The final live scene
had 3 stack objects, 4 stages, 12 helpers and 4 groups; teardown zeroed every
registry/helper/session count. The run exposed and fixed a whole-object
deletion session-state leak; see
[`../docs/performance-ledger.md`](../docs/performance-ledger.md) T10d and
[`results/2026-11-19-soak.json`](results/2026-11-19-soak.json). Blender's real
undo operator cannot run in `-b`; the engine's undo/redo handlers are
exercised. Native Windows/Rhino acceptance is deferred after 0.8.0 and is not
claimed.

## Stored baseline gate

The 0.8.0 reference baseline is `results/2026-11-17-linux.json`, built from
three solo runs on the reference machine: `results/2026-11-17-linux-run1.json`,
`…-run2.json` and `…-run3.json`. The raw reports and their SHA-256 are recorded
in the baseline, so every aggregate can be recomputed. The 2026-11-16 baseline
is historical; it was captured in a faster CPU power/core state and is no longer
the gate reference.

The documented best-of-three rule stores the fastest per-run p50/p95 for each
metric. CPU frequency/core scheduling on the development host moves a single
run by several percent even with unchanged code; the fastest run keeps that
ambient state from inflating the reference, while a sustained code regression
still moves every run. The per-run values remain in the baseline for audit.

```bash
# one baseline from three sequential runs (never run another gate alongside them)
scripts/bench --out benchmarks/results/2026-11-17-linux-run1.json
scripts/bench --out benchmarks/results/2026-11-17-linux-run2.json
scripts/bench --out benchmarks/results/2026-11-17-linux-run3.json
python3 benchmarks/baseline.py build \
  --run benchmarks/results/2026-11-17-linux-run1.json \
  --run benchmarks/results/2026-11-17-linux-run2.json \
  --run benchmarks/results/2026-11-17-linux-run3.json \
  --out benchmarks/results/2026-11-17-linux.json --created 2026-11-17
```

The gate is exactly the plan's rule: **a gated metric more than +5% slower than
its stored p50/p95 fails; exactly 5% passes.** A regression of 0–5% is allowed
only when the same task records a quantified offsetting gain (another benchmark,
production-code reduction, test/runtime improvement, or a documented
correctness/simplicity benefit). An unoffset regression is a failure.

`scripts/bench-baseline` is the gate command. It runs `scripts/bench` three
times, sequentially, and compares the fastest of the three per-run p50/p95 values
with the stored baseline (the baseline uses the same best-of-three rule). One
ambient-load outlier cannot inflate the result; a sustained code regression moves
all three runs. The per-run values are stored, so the aggregate is auditable.
`scripts/check-all` runs this command, so the full gate measures three times
before it decides.
`bench.py --check-baseline` remains the one-run comparison for a single report;
`--check-baseline-only --report` compares existing reports without measuring.

`benchmarks/baseline.py` is pure standard library and owns the statistics and
comparison; `bench.py` only measures. For each metric the baseline stores:

- `p50`/`p95`: the fastest of the three per-run percentiles;
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

`pipeline.100k.total_ms` is one timed operation that reads, hashes, encodes,
decodes and applies in sequence; it is not a sum of independently warmed phase
timings.

`pipeline.100k.apply_fresh_ms` measures the full topology build. T8 replaced the
per-face Python table plus `from_pydata` with one validated bulk
`vertices`/`loops`/`polygons` write; the metric fell from 77.95/87.13 ms to
8.96/9.10 ms p50/p95 with the same face order, n-gons and token path. The metric
set is unchanged, so the 2026-11-17 best-of-three baseline remains the gate
reference; no baseline rebuild was needed.

`playback.frame_ms` is the timer send-to-apply frame. T8 re-measured it without
editing the playback path: the stored baseline is 12.8405/13.3398 ms and the
accepted gate is 12.7994/13.2626 ms p50/p95. The 13.6–13.8 ms reading in the
2026-11-16 handoff does not reproduce; see the performance ledger's T8 section
for the three solo pre-edit runs and the host-state caveat.

The idle pass is not gated. It times 1000 timer ticks and divides, but even the
amortized value is about 3 µs; 5% of that is below the timer/scheduler noise
floor (a p95-only run produced a +13% swing with no code change). `bench.py`
keeps the hard 2 ms idle-pass budget. The value is still stored in the baseline
with that reason.

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
- `multistage.idle_pass_ms` is an amortized timer tick of about 3 µs; 5% is
  below the timer/scheduler noise floor and flaked +13% on an unrelated run.
  The hard 2 ms budget remains in `bench.py`.
- `roundtrip.*.prefix_ms/encode_ms/rtt_ms/apply_ms` are component diagnostics
  inside the full/ref settle gates. `rtt_ms` includes the fake client's own
  decode, scale, encode and hash.
- The 10k pipeline rows are setup measurements, not the hot path.

No metric is silently dropped: every ungated metric is stored in the baseline
with its reason, and `baseline.py` refuses a baseline whose ungated set disagrees
with the code's `METRICS`.

### Machine identity and CI

The stored numbers are from the development machine. The best-of-three rule
rejects a single background-load/CPU-state spike but cannot remove sustained
load: run the gate without another gate or benchmark, and only on a quiet
machine. If the host
is loaded, stop and defer. The gate compares
`processor`, OS (`os`), architecture (`arch`), `blender` and `python`. The full kernel/platform
string is printed for context but is not identity: a Linux kernel patch on the same CPU is not a different benchmark machine. On another machine it prints
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

The Blender matrix cannot see the C# parameter path or the frame decoder.
`scripts/bench-gha` builds `grasshopper/bench/ParamBench.cs` with the same
wine/Mono `csc` environment as the console suites and writes a JSON result under
`benchmarks/results/`:

```bash
scripts/bench-gha benchmarks/results/2026-11-17-linux-csharp-t8.json
```

It measures `Json.FormatDouble` over a representative double corpus
(`1e15`/`1e16`, `-0.0`, integers-as-doubles, shortest-repr values), every
`ParamValues.Cast`/`ParamValues.JsonValue` parameter type, the canonical
JSON/schema-frame encode for a 100-parameter stage, and the frame/section decode
path over a 100k-triangle mesh frame (`frame_decode`, `section_floats`,
`section_u32`, `frame_mesh_arrays`). It is separate from
`StageloomCoreTests.exe`, so normal test runs do not pay for it. The result
measures the pure managed layer only; it excludes RhinoCommon/Grasshopper object
allocation, Stage In conversion, publication and the solve.

The decode cases are the T8 benchmark: a decoded `Section` is a view into the
frame, `ByteLength` reads its extent without a copy, and `Raw` materialises one
copy on demand. `frame_decode` fell from 0.4556/0.5167 ms to 0.0050/0.0055 ms
p50/p95, and `frame_mesh_arrays` from 0.9210/1.0338 to 0.4434/0.4684 ms; the
remaining typed copy (`section_floats`/`section_u32`) is within sub-millisecond
noise. The result file is stored at
`results/2026-11-17-linux-csharp-t8.json`; the pre-change measurement of the
same new cases is `results/2026-11-17-linux-csharp-t8-before.json`.

**Rhino/GH-only skips.** `Conversion.BuildMesh`/`MeshSections` (RhinoCommon
geometry), `Publication.SendPublication` (a `GH_Document` and connected
`LinkConnection`) and `InstanceEmit`'s document lookup (a Rhino document) are
not measured by this bench. The local wine harness has no RhinoCore; the
console suites print their `SKIP native …` lines, and the T7 native probe is the
behaviour proof for emit/build. No microbench number is claimed for those
paths.


## Live Rhino measurement

A headless fake cannot measure Stage In conversion, the Grasshopper solve or
the definition's own work. The live half has two scripted entry points:

```bash
# Build the reference scenes and print their stage keys.
blender -b --factory-startup --online-mode \
  --python benchmarks/live_scene.py -- \
  --out /tmp/opencode/stageloom-live --kind both

# Measure one scene through a live Grasshopper connection. Run it with the
# installed 0.8.0 extension and an enabled Online Access preference.
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
