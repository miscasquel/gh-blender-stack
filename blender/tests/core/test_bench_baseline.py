"""Unit tests for the pure benchmark-baseline comparison.

The gate's statistics and failure rules must be testable without Blender. The
reports here are synthetic but have the same shape as benchmarks/bench.py's
JSON; the deliberate 6% mutation is the same one scripts/test-bench-baseline
runs through the real CLI.
"""

import copy
import json
import os
import statistics
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "benchmarks"))

import baseline  # noqa: E402

MACHINE = {
    "processor": "Test CPU 8-Core",
    "platform": "Linux-test-x86_64",
    "blender": "5.3.0",
    "python": "3.13.13",
}


def _stat(values):
    return {
        "min": min(values),
        "p50": baseline.percentile(values, 0.50),
        "median": statistics.median(values),
        "p95": baseline.percentile(values, 0.95),
        "max": max(values),
        "samples": list(values),
    }


def _roundtrip(kind):
    return {
        "count": 3,
        "samples": [
            {
                "settle_ms": value,
                "prefix_ms": 0.01,
                "encode_ms": 0.17,
                "rtt_ms": value - 10.0,
                "apply_ms": 11.0,
            }
            for value in (120.0, 121.0, 122.0)
        ],
    }


def make_report():
    phase = {
        "extract_ms": _stat([5.0, 5.1, 5.2]),
        "hash_ms": _stat([0.60, 0.61, 0.62]),
        "encode_ms": _stat([0.10, 0.11, 0.10]),
        "decode_ms": _stat([5.5, 5.6, 5.7]),
        "apply_fresh_ms": _stat([80.0, 81.0, 82.0]),
        "apply_inplace_ms": _stat([2.3, 2.4, 2.5]),
    }
    total = [
        sum(phase[name]["samples"][index] for name in phase)
        for index in range(3)
    ]
    pipeline = [
        {
            "triangles": 10_000,
            "requested_triangles": 10_000,
            "extract_ms": _stat([0.5, 0.51, 0.52]),
            "hash_ms": _stat([0.07, 0.071, 0.072]),
            "encode_ms": _stat([0.02, 0.021, 0.022]),
            "decode_ms": _stat([0.5, 0.51, 0.52]),
            "apply_fresh_ms": _stat([9.0, 9.1, 9.2]),
            "apply_inplace_ms": _stat([0.23, 0.24, 0.25]),
            "total_ms": _stat([10.3, 10.4, 10.5]),
        },
        dict({"triangles": 99_458, "requested_triangles": 100_000}, **phase),
        {
            "triangles": 994_582,
            "requested_triangles": 1_000_000,
            "extract_ms": _stat([80.0, 81.0, 82.0]),
            "hash_ms": _stat([6.0, 6.1, 6.2]),
            "encode_ms": _stat([1.0, 1.1, 1.0]),
            "decode_ms": _stat([60.0, 61.0, 62.0]),
            "apply_fresh_ms": _stat([800.0, 810.0, 820.0]),
            "apply_inplace_ms": _stat([23.0, 24.0, 25.0]),
            "total_ms": _stat([970.0, 983.0, 996.0]),
        },
    ]
    pipeline[1]["total_ms"] = _stat(total)
    return {
        "format": "stageloom-bench-report/1",
        "meta": dict(MACHINE),
        "scenarios": ["pipeline", "instances", "roundtrip", "multistage", "playback"],
        "pipeline": pipeline,
        "instances": {
            "triangles_in_definition": 99_458,
            "instances": 13,
            "prefix_ms": _stat([10.0, 10.2, 10.4]),
            "encode_ms": _stat([0.20, 0.21, 0.22]),
            "apply_first_ms": _stat([87.0]),
            "apply_repeat_ms": _stat([2.4, 2.5, 2.6]),
            "group_reused": True,
        },
        "roundtrip": {
            "triangles": 99_458,
            "full": _roundtrip("full"),
            "ref": _roundtrip("ref"),
        },
        "multistage": {
            "triangles": 9_800,
            "stages": 3,
            "dirty_pass_ms": _stat([6.0, 6.1, 6.2]),
            "settle_ms": _stat([80.0, 81.0, 82.0]),
            "idle_pass_ms": _stat([0.0055] * 200),
        },
        "playback": {
            "triangles": 9_800,
            "frames": 30,
            "per_frame_ms": _stat([12.0] * 30),
            "responses": 30,
            "applies": 30,
        },
    }


def scale_playback(report, factor):
    samples = [value * factor for value in report["playback"]["per_frame_ms"]["samples"]]
    report["playback"]["per_frame_ms"] = _stat(samples)


class BaselineGateTests(unittest.TestCase):
    def setUp(self):
        self.baseline = baseline.build_baseline(
            [make_report(), make_report(), make_report()],
            created="2026-11-16",
        )

    def test_gated_set_covers_the_required_paths(self):
        required = {
            "pipeline.100k.total_ms",
            "pipeline.100k.apply_inplace_ms",
            "pipeline.1m.apply_inplace_ms",
            "instances.13x.apply_repeat_ms",
            "playback.frame_ms",
            "roundtrip.full.settle_ms",
            "roundtrip.ref.settle_ms",
            "multistage.dirty_pass_ms",
            "multistage.idle_pass_ms",
        }
        self.assertLessEqual(required, set(baseline.GATED_IDS))
        for metric in baseline.METRICS:
            if not metric.gated:
                self.assertTrue(
                    metric.reason,
                    "ungated metric %s must state why" % metric.id,
                )

    def test_identical_report_passes(self):
        result = baseline.compare_report(make_report(), self.baseline)
        self.assertTrue(result.comparable)
        self.assertEqual(result.failed, [])
        self.assertEqual(result.exit_code(), 0)
        self.assertEqual(len(result.rows), 2 * len(baseline.GATED_IDS))

    def test_four_percent_passes(self):
        report = make_report()
        scale_playback(report, 1.04)
        result = baseline.compare_report(report, self.baseline)
        self.assertEqual(result.failed, [])
        self.assertEqual(result.exit_code(), 0)

    def test_exactly_five_percent_passes(self):
        report = make_report()
        scale_playback(report, 1.05)
        result = baseline.compare_report(report, self.baseline)
        self.assertEqual(result.failed, [])
        self.assertEqual(result.exit_code(), 0)

    def test_six_percent_fails_with_the_metric_name(self):
        report = make_report()
        scale_playback(report, 1.06)
        result = baseline.compare_report(report, self.baseline)
        self.assertTrue(result.comparable)
        self.assertEqual(result.exit_code(), 1)
        failed = {row.metric for row in result.failed}
        self.assertIn("playback.frame_ms", failed)
        table = baseline.format_table(result)
        self.assertIn("playback.frame_ms", table)
        self.assertIn("FAIL", table)

    def test_baseline_is_median_of_three_runs(self):
        good = make_report()
        bad = make_report()
        scale_playback(bad, 1.20)
        document = baseline.build_baseline(
            [good, bad, good], created="2026-11-16"
        )
        entry = document["metrics"]["playback.frame_ms"]
        self.assertAlmostEqual(entry["p50"], 12.0, places=6)
        self.assertAlmostEqual(entry["p95"], 12.0, places=6)
        self.assertEqual(document["aggregate"], "median run")

    def test_baseline_aggregate_must_be_median(self):
        document = copy.deepcopy(self.baseline)
        document["aggregate"] = "best run"
        with self.assertRaises(baseline.BaselineError) as caught:
            baseline.validate_baseline(document)
        self.assertIn("median run", str(caught.exception))

    def test_one_outlier_is_ignored_by_median(self):
        good = make_report()
        bad = make_report()
        scale_playback(bad, 1.20)
        result = baseline.compare_reports([good, bad, good], self.baseline)
        self.assertEqual(result.run_count, 3)
        self.assertEqual(result.failed, [])

    def test_systematic_regression_fails(self):
        reports = [make_report() for _ in range(3)]
        for report in reports:
            scale_playback(report, 1.06)
        result = baseline.compare_reports(reports, self.baseline)
        self.assertEqual(result.exit_code(), 1)
        self.assertIn("playback.frame_ms", {row.metric for row in result.failed})

    def test_p95_fails_even_when_p50_passes(self):
        report = make_report()
        # 27 values at the baseline plus 3 at +6%: p50 is unchanged, p95 moves.
        report["playback"]["per_frame_ms"] = _stat([12.0] * 27 + [12.72] * 3)
        result = baseline.compare_report(report, self.baseline)
        rows = {(row.metric, row.stat): row for row in result.rows}
        self.assertTrue(rows[("playback.frame_ms", "p50")].ok)
        self.assertFalse(rows[("playback.frame_ms", "p95")].ok)
        self.assertEqual(result.exit_code(), 1)

    def test_missing_metric_is_a_clear_error(self):
        report = make_report()
        del report["playback"]
        with self.assertRaises(baseline.BaselineError) as caught:
            baseline.compare_report(report, self.baseline)
        self.assertIn("playback.frame_ms", str(caught.exception))

    def test_extra_metric_is_a_clear_error(self):
        document = copy.deepcopy(self.baseline)
        document["metrics"]["extra.metric"] = {
            "gated": True,
            "p50": 1.0,
            "p95": 1.0,
            "samples": [1.0],
        }
        with self.assertRaises(baseline.BaselineError) as caught:
            baseline.compare_report(make_report(), document)
        self.assertIn("extra.metric", str(caught.exception))

    def test_malformed_baseline_is_a_clear_error(self):
        with self.assertRaises(baseline.BaselineError) as caught:
            baseline.validate_baseline({"format": "not-a-baseline"})
        self.assertIn("format", str(caught.exception))

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump({"format": "not-a-baseline"}, handle)
            path = handle.name
        try:
            with self.assertRaises(baseline.BaselineError) as caught:
                baseline.load_baseline(path)
            self.assertIn("format", str(caught.exception))
        finally:
            os.unlink(path)

    def test_machine_mismatch_skips_and_require_fails(self):
        report = make_report()
        report["meta"]["processor"] = "Some Other CPU"
        result = baseline.compare_report(report, self.baseline)
        self.assertFalse(result.comparable)
        self.assertEqual(result.rows, [])
        self.assertEqual(result.exit_code(), 0)
        self.assertEqual(result.exit_code(require_baseline=True), 2)
        self.assertIn("NOT COMPARABLE", baseline.format_table(result))
        self.assertIn("Some Other CPU", baseline.format_table(result))

    def test_threshold_cannot_be_widened(self):
        document = copy.deepcopy(self.baseline)
        document["threshold_pct"] = 6.0
        with self.assertRaises(baseline.BaselineError) as caught:
            baseline.validate_baseline(document)
        self.assertIn("exactly", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
