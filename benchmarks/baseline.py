"""Stored-baseline comparison for the Stageloom benchmark matrix.

This module is deliberately pure standard library. ``bench.py`` imports bpy to
measure; the comparison and statistics live here so they can be tested without
Blender.

    python3 benchmarks/baseline.py build --run run1.json --run run2.json \
        --run run3.json --out benchmarks/results/2026-11-16-linux.json
    python3 benchmarks/baseline.py check --baseline <baseline> --report <run>
    python3 benchmarks/baseline.py summary --baseline <baseline>

The gate is exactly the plan's: a gated metric more than 5% slower than the
stored baseline fails. A delta of exactly 5% passes. The baseline is
machine-specific; a report from another processor, platform, Blender or Python
is reported as not comparable instead of silently passing or failing.

Gated metrics are the ones the hot path actually pays for. The full list and
the reason for each ungated diagnostic is in ``METRICS`` below, and is copied
into the baseline file so an audit can see what was and was not enforced.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
import time
from pathlib import Path

FORMAT = "stageloom-bench-baseline/1"
THRESHOLD_PCT = 5.0
DEFAULT_BASELINE = "benchmarks/results/2026-11-16-linux.json"
MACHINE_FIELDS = ("processor", "platform", "blender", "python")
# The comparison treats values within this relative distance of the limit as
# exactly at it, so a mathematically exact +5% does not fail on binary rounding
# (105.0 / 100.0 - 1 is 5.000000000000004 in IEEE 754).
COMPARE_EPSILON = 1e-12


class BaselineError(Exception):
    """The baseline, the report or the metric set cannot be compared."""


class MetricMissing(BaselineError):
    """A required metric is absent from a report."""


class Metric:
    """One measured series and the rule for extracting it from a run report."""

    def __init__(self, metric_id, scenario, unit, gated, reason, extract):
        self.id = metric_id
        self.scenario = scenario
        self.unit = unit
        self.gated = gated
        self.reason = reason
        self.extract = extract


def _float_samples(values):
    return [float(value) for value in values]


def _samples(stats, label):
    if not isinstance(stats, dict):
        raise MetricMissing("%s is not a statistics object" % label)
    samples = stats.get("samples")
    if not samples:
        raise MetricMissing("%s has no samples" % label)
    return _float_samples(samples)


def _pipeline_row(report, requested):
    rows = report.get("pipeline")
    if not isinstance(rows, list):
        raise MetricMissing("report has no pipeline rows")
    for row in rows:
        if int(row.get("requested_triangles", -1)) == requested:
            return row
    raise MetricMissing("report has no pipeline row for %d requested triangles" % requested)


def _pipeline_samples(report, requested, field):
    row = _pipeline_row(report, requested)
    return _samples(row.get(field), "pipeline.%dk.%s" % (requested // 1000, field))


def _pipeline_total_samples(report, requested):
    row = _pipeline_row(report, requested)
    if "total_ms" in row:
        return _samples(row["total_ms"], "pipeline.%dk.total_ms" % (requested // 1000))
    names = ("extract_ms", "hash_ms", "encode_ms", "decode_ms", "apply_inplace_ms")
    series = [_pipeline_samples(report, requested, name) for name in names]
    lengths = {len(values) for values in series}
    if len(lengths) != 1:
        raise MetricMissing(
            "pipeline.%dk phase sample counts differ (%s); cannot derive total"
            % (requested // 1000, sorted(lengths))
        )
    return [sum(values[index] for values in series) for index in range(len(series[0]))]


def _scenario_samples(report, scenario, field):
    block = report.get(scenario)
    if not isinstance(block, dict):
        raise MetricMissing("report has no %s block" % scenario)
    return _samples(block.get(field), "%s.%s" % (scenario, field))


def _roundtrip_samples(report, kind, field):
    block = report.get("roundtrip")
    if not isinstance(block, dict) or not isinstance(block.get(kind), dict):
        raise MetricMissing("report has no roundtrip.%s block" % kind)
    samples = block[kind].get("samples")
    if not samples:
        raise MetricMissing("roundtrip.%s has no samples" % kind)
    values = []
    for sample in samples:
        value = sample.get(field) if isinstance(sample, dict) else None
        if value is None:
            raise MetricMissing("roundtrip.%s sample has no %s" % (kind, field))
        values.append(float(value))
    return values


def _playback_samples(report):
    block = report.get("playback")
    if not isinstance(block, dict):
        raise MetricMissing("report has no playback block")
    return _samples(block.get("per_frame_ms"), "playback.per_frame_ms")


def _m(metric_id, scenario, unit, gated, reason, extract):
    return Metric(metric_id, scenario, unit, gated, reason, extract)


# The gated set is the hot path: the 100k pipeline phases that dominate a frame,
# the same-topology applies the token fast path protects, the repeated instance
# apply, playback, both round-trip settles, and the engine passes.
#
# The numbers are wall-clock p50/p95: playback and round-trip settles include
# the wire wait, and the CPU-bound paths are what the user waits for. Run the
# gate on a quiet machine; `scripts/bench-baseline` stores and compares the
# median of three sequential runs, so one ambient-load outlier is visible in the
# per-run values but does not become the baseline.
#
# The ungated entries are still measured and stored. They are ungated for a
# stated reason, never silently dropped: sub-millisecond timer noise cannot
# resolve a 5% change, one-shot setup costs have no meaningful p95 in one run,
# and the round-trip component timings are diagnostics inside the settle gate
# (rtt includes the fake client's own decode/encode).
METRICS = (
    _m("pipeline.100k.extract_ms", "pipeline", "ms", True, "",
         lambda report: _pipeline_samples(report, 100_000, "extract_ms")),
    _m("pipeline.100k.decode_ms", "pipeline", "ms", True, "",
         lambda report: _pipeline_samples(report, 100_000, "decode_ms")),
    _m("pipeline.100k.apply_fresh_ms", "pipeline", "ms", True, "",
         lambda report: _pipeline_samples(report, 100_000, "apply_fresh_ms")),
    _m("pipeline.100k.apply_inplace_ms", "pipeline", "ms", True, "",
         lambda report: _pipeline_samples(report, 100_000, "apply_inplace_ms")),
    _m("pipeline.100k.total_ms", "pipeline", "ms", True, "",
         lambda report: _pipeline_total_samples(report, 100_000)),
    _m("pipeline.1m.apply_inplace_ms", "pipeline", "ms", True, "",
         lambda report: _pipeline_samples(report, 1_000_000, "apply_inplace_ms")),
    _m("instances.13x.prefix_ms", "instances", "ms", True, "",
         lambda report: _scenario_samples(report, "instances", "prefix_ms")),
    _m("instances.13x.apply_repeat_ms", "instances", "ms", True, "",
         lambda report: _scenario_samples(report, "instances", "apply_repeat_ms")),
    _m("playback.frame_ms", "playback", "ms", True, "",
       _playback_samples),
    _m("roundtrip.full.settle_ms", "roundtrip", "ms", True, "",
       lambda report: _roundtrip_samples(report, "full", "settle_ms")),
    _m("roundtrip.ref.settle_ms", "roundtrip", "ms", True, "",
       lambda report: _roundtrip_samples(report, "ref", "settle_ms")),
    _m("multistage.dirty_pass_ms", "multistage", "ms", True, "",
         lambda report: _scenario_samples(report, "multistage", "dirty_pass_ms")),
    _m("multistage.settle_ms", "multistage", "ms", True, "",
       lambda report: _scenario_samples(report, "multistage", "settle_ms")),
    _m("multistage.idle_pass_ms", "multistage", "ms", False,
         "amortized idle tick (~0.003 ms); 5% is below the timer/scheduler noise floor; "
         "bench.py keeps a hard 2 ms budget",
         lambda report: _scenario_samples(report, "multistage", "idle_pass_ms")),

    _m("pipeline.100k.hash_ms", "pipeline", "ms", False,
         "sub-millisecond diagnostic (~0.6 ms); 5% is at the timer/allocation noise floor",
         lambda report: _pipeline_samples(report, 100_000, "hash_ms")),
    _m("pipeline.100k.encode_ms", "pipeline", "ms", False,
         "sub-millisecond diagnostic (~0.1 ms); 5% is at the timer/allocation noise floor",
         lambda report: _pipeline_samples(report, 100_000, "encode_ms")),
    _m("pipeline.1m.extract_ms", "pipeline", "ms", False,
         "1M scale case; the repeat hot path is apply_inplace and is gated",
         lambda report: _pipeline_samples(report, 1_000_000, "extract_ms")),
    _m("pipeline.1m.hash_ms", "pipeline", "ms", False,
         "1M scale case recorded for memory/scale comparison; not a per-frame path",
         lambda report: _pipeline_samples(report, 1_000_000, "hash_ms")),
    _m("pipeline.1m.encode_ms", "pipeline", "ms", False,
         "1M scale case recorded for frame-size comparison; not a per-frame path",
         lambda report: _pipeline_samples(report, 1_000_000, "encode_ms")),
    _m("pipeline.1m.decode_ms", "pipeline", "ms", False,
         "1M scale case; the repeat hot path is apply_inplace and is gated",
         lambda report: _pipeline_samples(report, 1_000_000, "decode_ms")),
    _m("pipeline.1m.apply_fresh_ms", "pipeline", "ms", False,
         "1M full build is a scale case with no budget; apply_inplace is the repeat guard",
         lambda report: _pipeline_samples(report, 1_000_000, "apply_fresh_ms")),
    _m("instances.13x.encode_ms", "instances", "ms", False,
         "sub-millisecond diagnostic (~0.2 ms); 5% is at the timer/allocation noise floor",
         lambda report: _scenario_samples(report, "instances", "encode_ms")),
    _m("instances.13x.apply_first_ms", "instances", "ms", False,
         "one sample per run (first definition build); repeated apply is the gated path",
         lambda report: _scenario_samples(report, "instances", "apply_first_ms")),
    _m("roundtrip.full.prefix_ms", "roundtrip", "ms", False,
       "component diagnostic inside the settle gate; the full prefix is cached",
       lambda report: _roundtrip_samples(report, "full", "prefix_ms")),
    _m("roundtrip.full.encode_ms", "roundtrip", "ms", False,
       "sub-millisecond component diagnostic inside the settle gate",
       lambda report: _roundtrip_samples(report, "full", "encode_ms")),
    _m("roundtrip.full.rtt_ms", "roundtrip", "ms", False,
       "includes the fake client's own decode/scale/encode; the settle gate covers it",
       lambda report: _roundtrip_samples(report, "full", "rtt_ms")),
    _m("roundtrip.full.apply_ms", "roundtrip", "ms", False,
       "component diagnostic inside the settle gate; the first apply is one-shot",
       lambda report: _roundtrip_samples(report, "full", "apply_ms")),
    _m("roundtrip.ref.prefix_ms", "roundtrip", "ms", False,
       "component diagnostic inside the ref settle gate",
       lambda report: _roundtrip_samples(report, "ref", "prefix_ms")),
    _m("roundtrip.ref.encode_ms", "roundtrip", "ms", False,
       "sub-millisecond component diagnostic inside the ref settle gate",
       lambda report: _roundtrip_samples(report, "ref", "encode_ms")),
    _m("roundtrip.ref.rtt_ms", "roundtrip", "ms", False,
       "includes the fake client's own work; the ref settle gate covers it",
       lambda report: _roundtrip_samples(report, "ref", "rtt_ms")),
    _m("roundtrip.ref.apply_ms", "roundtrip", "ms", False,
       "component diagnostic inside the ref settle gate; the apply is the same cached write",
       lambda report: _roundtrip_samples(report, "ref", "apply_ms")),
)

METRICS_BY_ID = {metric.id: metric for metric in METRICS}
GATED_IDS = tuple(metric.id for metric in METRICS if metric.gated)


def percentile(values, fraction):
    """Linear-interpolation percentile (Hyndman-Fan type 7).

    The same definition is used for the stored baseline and the current run;
    for one sample it is that sample. This is the numpy/scipy default and
    avoids the nearest-rank discontinuity at small sample counts.
    """
    if not values:
        raise ValueError("percentile of an empty series")
    if not 0.0 <= fraction <= 1.0:
        raise ValueError("percentile fraction outside 0..1")
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * fraction
    lower = int(math.floor(rank))
    upper = int(math.ceil(rank))
    if lower == upper:
        return ordered[lower]
    weight = rank - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight


def statistics_for(samples):
    """The p50/p95 summary stored for one metric."""
    values = _float_samples(samples)
    if not values:
        raise ValueError("statistics of an empty series")
    return {
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
        "min": min(values),
        "max": max(values),
        "count": len(values),
    }


def _load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise BaselineError("cannot read %s: file not found" % path)
    except OSError as exc:
        raise BaselineError("cannot read %s: %s" % (path, exc))
    except ValueError as exc:
        raise BaselineError("cannot parse %s as JSON: %s" % (path, exc))


def load_report(path):
    """Load a raw benchmark report (no baseline validation)."""
    return _load_json(path)


def load_baseline(path):
    document = _load_json(path)
    validate_baseline(document)
    return document


def _machine_of(report, label):
    if not isinstance(report, dict):
        raise BaselineError("%s is not a JSON object" % label)
    meta = report.get("meta")
    if not isinstance(meta, dict):
        raise BaselineError("%s has no meta object" % label)
    machine = {}
    for field in MACHINE_FIELDS:
        value = meta.get(field)
        if not isinstance(value, str) or not value:
            raise BaselineError("%s meta has no %s" % (label, field))
        machine[field] = value
    return machine


def _machine_note(baseline_machine, current_machine):
    differences = []
    for field in MACHINE_FIELDS:
        if baseline_machine.get(field) != current_machine.get(field):
            differences.append(
                "%s: baseline %r, current %r"
                % (field, baseline_machine.get(field), current_machine.get(field))
            )
    return "; ".join(differences)


def _source_run_record(path, root=None):
    file_path = Path(path)
    record = {"path": str(file_path)}
    if root is not None:
        try:
            record["path"] = str(file_path.resolve().relative_to(Path(root).resolve()))
        except ValueError:
            pass
    try:
        record["sha256"] = hashlib.sha256(file_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise BaselineError("cannot hash source run %s: %s" % (path, exc))
    return record


def build_baseline(reports, source_paths=(), created=None, root=None):
    """Combine clean run reports into the stored baseline.

    Every metric keeps its per-run p50/p95, the median of the three per-run
    p50/p95 becomes the stored value, and the pooled samples are kept. The
    median is the standard three-run aggregate: it rejects one ambient-load
    outlier without cherry-picking an optimistic p95, and a sustained regression
    moves all three runs. The per-run values make the aggregate auditable.
    """
    if not reports:
        raise BaselineError("no benchmark runs to build a baseline from")
    machines = [_machine_of(report, "run %d" % (index + 1)) for index, report in enumerate(reports)]
    machine = machines[0]
    for index, other in enumerate(machines[1:], 2):
        note = _machine_note(machine, other)
        if note:
            raise BaselineError("run %d was measured on a different machine: %s" % (index, note))

    metrics = {}
    for metric in METRICS:
        run_samples = []
        for index, report in enumerate(reports, 1):
            try:
                samples = metric.extract(report)
            except MetricMissing as exc:
                raise BaselineError("run %d is missing %s: %s" % (index, metric.id, exc))
            if not samples:
                raise BaselineError("run %d has no samples for %s" % (index, metric.id))
            run_samples.append(samples)
        p50_runs = [percentile(samples, 0.50) for samples in run_samples]
        p95_runs = [percentile(samples, 0.95) for samples in run_samples]
        pooled = [value for samples in run_samples for value in samples]
        metrics[metric.id] = {
            "scenario": metric.scenario,
            "unit": metric.unit,
            "gated": metric.gated,
            "reason": metric.reason,
            "p50": statistics.median(p50_runs),
            "p95": statistics.median(p95_runs),
            "p50_runs": p50_runs,
            "p95_runs": p95_runs,
            "samples": pooled,
            "sample_count": len(pooled),
        }

    document = {
        "format": FORMAT,
        "created": created or time.strftime("%Y-%m-%d"),
        "threshold_pct": THRESHOLD_PCT,
        "aggregate": "median run",
        "machine": machine,
        "run_count": len(reports),
        "source_runs": [_source_run_record(path, root) for path in source_paths],
        "metrics": metrics,
    }
    return document


def validate_baseline(document):
    if not isinstance(document, dict):
        raise BaselineError("baseline is not a JSON object")
    if document.get("format") != FORMAT:
        raise BaselineError(
            "baseline format is %r; expected %r" % (document.get("format"), FORMAT)
        )
    threshold = document.get("threshold_pct")
    if not isinstance(threshold, (int, float)) or float(threshold) != THRESHOLD_PCT:
        raise BaselineError(
            "baseline threshold is %r; the gate is exactly %.1f%%"
            % (threshold, THRESHOLD_PCT)
        )
    if document.get("aggregate") != "median run":
        raise BaselineError(
            "baseline aggregate is %r; expected 'median run'"
            % document.get("aggregate")
        )
    machine = document.get("machine")
    if not isinstance(machine, dict):
        raise BaselineError("baseline has no machine object")
    for field in MACHINE_FIELDS:
        if not isinstance(machine.get(field), str) or not machine[field]:
            raise BaselineError("baseline machine has no %s" % field)
    metrics = document.get("metrics")
    if not isinstance(metrics, dict) or not metrics:
        raise BaselineError("baseline has no metrics object")

    for metric_id, entry in metrics.items():
        spec = METRICS_BY_ID.get(metric_id)
        if spec is None:
            raise BaselineError("baseline contains unknown metric %r" % metric_id)
        if not isinstance(entry, dict):
            raise BaselineError("baseline metric %r is not an object" % metric_id)
        if bool(entry.get("gated")) != spec.gated:
            raise BaselineError(
                "baseline gated flag for %r disagrees with the code's gated metric set"
                % metric_id
            )
        for field in ("p50", "p95"):
            value = entry.get(field)
            if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise BaselineError("baseline metric %r has no finite %s" % (metric_id, field))
            if float(value) <= 0.0:
                raise BaselineError("baseline metric %r has a non-positive %s" % (metric_id, field))
        samples = entry.get("samples")
        if not isinstance(samples, list) or not samples:
            raise BaselineError("baseline metric %r has no raw samples" % metric_id)
        for value in samples:
            if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise BaselineError("baseline metric %r has a non-finite sample" % metric_id)

    missing = [metric.id for metric in METRICS if metric.gated and metric.id not in metrics]
    if missing:
        raise BaselineError("baseline is missing gated metric(s): " + ", ".join(missing))
    return document


class Row:
    def __init__(self, metric, stat, unit, baseline_value, current_value):
        self.metric = metric
        self.stat = stat
        self.unit = unit
        self.baseline = float(baseline_value)
        self.current = float(current_value)
        self.delta_pct = (self.current - self.baseline) / self.baseline * 100.0
        limit = self.baseline * (1.0 + THRESHOLD_PCT / 100.0)
        at_limit = math.isclose(self.current, limit, rel_tol=COMPARE_EPSILON, abs_tol=0.0)
        self.ok = self.current <= limit or at_limit


class Comparison:
    def __init__(self, rows, comparable=True, reason="", baseline_machine=None,
                 current_machine=None, run_count=1):
        self.rows = rows
        self.comparable = comparable
        self.reason = reason
        self.baseline_machine = baseline_machine or {}
        self.current_machine = current_machine or {}
        self.run_count = run_count

    @property
    def failed(self):
        return [row for row in self.rows if not row.ok]

    def exit_code(self, require_baseline=False):
        if not self.comparable:
            return 2 if require_baseline else 0
        return 1 if self.failed else 0


def compare_reports(reports, baseline_document):
    """Compare one or more measured reports against a validated baseline.

    With several reports (the gate uses three sequential runs), each metric's
    current p50/p95 is the median of the per-run p50/p95. This mirrors the
    baseline's aggregate: one ambient-load outlier cannot move the result, while
    a sustained code regression moves all three runs. The reports must all be
    from the same machine.
    """
    validate_baseline(baseline_document)
    if not reports:
        raise BaselineError("no current reports to compare")
    current_machines = [
        _machine_of(report, "current report %d" % (index + 1))
        for index, report in enumerate(reports)
    ]
    baseline_machine = baseline_document["machine"]
    notes = []
    for index, current_machine in enumerate(current_machines, 1):
        note = _machine_note(baseline_machine, current_machine)
        if note:
            notes.append("report %d: %s" % (index, note))
    if notes:
        return Comparison(
            [],
            comparable=False,
            reason="; ".join(notes),
            baseline_machine=baseline_machine,
            current_machine=current_machines[0],
            run_count=len(reports),
        )

    baseline_metrics = baseline_document["metrics"]
    unknown = sorted(set(baseline_metrics) - set(METRICS_BY_ID))
    if unknown:
        raise BaselineError("baseline contains unknown metric(s): " + ", ".join(unknown))
    missing = sorted(
        metric.id for metric in METRICS if metric.gated and metric.id not in baseline_metrics
    )
    if missing:
        raise BaselineError("baseline is missing gated metric(s): " + ", ".join(missing))

    rows = []
    for metric in METRICS:
        if not metric.gated:
            continue
        entry = baseline_metrics[metric.id]
        p50s = []
        p95s = []
        for index, report in enumerate(reports, 1):
            try:
                samples = metric.extract(report)
            except MetricMissing as exc:
                raise BaselineError(
                    "current report %d is missing gated metric %s: %s"
                    % (index, metric.id, exc)
                )
            if not samples:
                raise BaselineError(
                    "current report %d has no samples for %s" % (index, metric.id)
                )
            current = statistics_for(samples)
            p50s.append(current["p50"])
            p95s.append(current["p95"])
        for stat, values in (("p50", p50s), ("p95", p95s)):
            rows.append(
                Row(metric.id, stat, metric.unit, entry[stat], statistics.median(values))
            )
    return Comparison(
        rows,
        comparable=True,
        baseline_machine=baseline_machine,
        current_machine=current_machines[0],
        run_count=len(reports),
    )


def compare_report(report, baseline_document):
    """Compare one measured report; a convenience over ``compare_reports``."""
    return compare_reports([report], baseline_document)


def format_machine(machine):
    return ", ".join("%s=%s" % (field, machine.get(field, "?")) for field in MACHINE_FIELDS)


def format_table(comparison):
    lines = []
    if not comparison.comparable:
        lines.append("[baseline] SKIP: NOT COMPARABLE on this machine")
        lines.append("[baseline]   baseline: " + format_machine(comparison.baseline_machine))
        lines.append("[baseline]   current:  " + format_machine(comparison.current_machine))
        lines.append("[baseline]   " + comparison.reason)
        lines.append(
            "[baseline]   the stored baseline is machine-specific; "
            "run scripts/bench --require-baseline on the reference machine"
        )
        return "\n".join(lines)

    lines.append(
        "[baseline] %-34s %-4s %-4s %11s %11s %9s  %s"
        % ("metric", "stat", "unit", "baseline", "current", "delta", "result")
    )
    for row in comparison.rows:
        lines.append(
            "[baseline] %-34s %-4s %-4s %11.4f %11.4f %+8.2f%%  %s"
            % (
                row.metric,
                row.stat,
                row.unit,
                row.baseline,
                row.current,
                row.delta_pct,
                "PASS" if row.ok else "FAIL",
            )
        )
    failed = comparison.failed
    if failed:
        lines.append(
            "[baseline] FAIL: %d of %d checks over +%.1f%%: %s"
            % (
                len(failed),
                len(comparison.rows),
                THRESHOLD_PCT,
                ", ".join(sorted({row.metric for row in failed})),
            )
        )
    else:
        lines.append(
            "[baseline] PASS: %d checks across %d gated metrics (median of %d run%s) within +%.1f%%"
            % (
                len(comparison.rows),
                len(GATED_IDS),
                comparison.run_count,
                "" if comparison.run_count == 1 else "s",
                THRESHOLD_PCT,
            )
        )
    return "\n".join(lines)


def summarize_baseline(document):
    validate_baseline(document)
    lines = [
        "%-34s %-4s %-6s %11s %11s %s"
        % ("metric", "unit", "gated", "p50", "p95", "reason / scenario"),
    ]
    for metric in METRICS:
        entry = document["metrics"].get(metric.id)
        if entry is None:
            continue
        note = entry.get("reason") or entry.get("scenario", "")
        lines.append(
            "%-34s %-4s %-6s %11.4f %11.4f %s"
            % (
                metric.id,
                metric.unit,
                "yes" if entry["gated"] else "no",
                entry["p50"],
                entry["p95"],
                note,
            )
        )
    return "\n".join(lines)


def write_baseline(path, document):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2, sort_keys=False)
        handle.write("\n")


def _cli(argv):
    parser = argparse.ArgumentParser(prog="benchmarks/baseline.py")
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="combine clean run reports into a baseline")
    build.add_argument("--run", action="append", required=True, help="run report (repeat for 3)")
    build.add_argument("--out", required=True, help="baseline JSON path")
    build.add_argument("--created", default="", help="baseline date (default: today)")
    build.add_argument(
        "--root",
        default=str(Path(__file__).resolve().parent.parent),
        help="repository root for relative source-run paths",
    )

    check = sub.add_parser("check", help="compare run report(s) against a baseline")
    check.add_argument("--baseline", default=DEFAULT_BASELINE)
    check.add_argument(
        "--report",
        action="append",
        required=True,
        help="run report (repeat for the 3-run median; the gate uses 3)",
    )
    check.add_argument(
        "--require-baseline",
        action="store_true",
        help="fail (exit 2) when the report was measured on another machine",
    )

    summary = sub.add_parser("summary", help="print the stored p50/p95 table")
    summary.add_argument("--baseline", default=DEFAULT_BASELINE)

    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            reports = [_load_json(path) for path in args.run]
            document = build_baseline(
                reports,
                source_paths=args.run,
                created=args.created or None,
                root=args.root,
            )
            write_baseline(args.out, document)
            print(
                "baseline: wrote %s (%d metrics, %d gated, %d runs)"
                % (args.out, len(document["metrics"]), len(GATED_IDS), len(reports))
            )
            return 0
        if args.command == "check":
            document = load_baseline(args.baseline)
            reports = [_load_json(path) for path in args.report]
            comparison = compare_reports(reports, document)
            print(format_table(comparison))
            return comparison.exit_code(args.require_baseline)
        if args.command == "summary":
            document = load_baseline(args.baseline)
            print(summarize_baseline(document))
            return 0
    except BaselineError as exc:
        print("baseline: ERROR: %s" % exc, file=sys.stderr)
        return 3
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
