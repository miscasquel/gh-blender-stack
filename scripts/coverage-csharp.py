#!/usr/bin/env python3
"""Measure pure-core C# line coverage with Mono's coverage profiler.

The Grasshopper core has two halves:

* pure C# (Json, the generated protocol tables, the frame codec, parameter
  values and the endpoint constants) which builds and runs with Mono alone;
* RhinoCommon-dependent code (``Attributes.cs`` and ``Conversion.cs``) which is
  exercised only by the wine/Rhino suites and is explicitly outside the pure
  core denominator.

This tool builds the pure half from ``grasshopper/src/core`` with ``mcs
-debug``, runs ``StageloomCoreTests.exe`` and ``StageloomFuzzTests.exe`` under
``mono --profile=coverage``, and reports unique source-line coverage for that
denominator. It is local-only: CI has no Mono/Rhino toolchain.

A normal Linux Mono install (``mono`` and ``mcs`` on PATH) works directly. A
relocated Mono prefix can be selected with ``MONO_HOME`` (the directory holding
``bin/mono`` and ``lib/mono/4.5/mcs.exe``); ``MONO`` and ``MCS`` override the
two executables individually. Mono 6.12's built-in ``coverage`` profiler writes
``coverage.xml`` in the working directory; no external coverage package is
needed.

Usage:
    scripts/coverage-csharp.py [--fail-under=80] [--work-dir=DIR]

``--work-dir`` keeps the debug builds and raw coverage XML for inspection.
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "grasshopper" / "src" / "core"
CONSOLE = REPO / "grasshopper" / "tests" / "console"
FIXTURES = REPO / "protocol" / "fixtures"

# Every file under grasshopper/src/core must either be measured by a suite
# below or be named here with a reason. A new core file fails the run until it
# is accounted for, so the exclude list cannot silently grow.
EXCLUDED = {
    "Attributes.cs": (
        "references RhinoCommon's ArchivableDictionary; exercised by "
        "StageloomAttrTests.exe (43 checks) under wine, not by pure C#"
    ),
    "Conversion.cs": (
        "references RhinoCommon Mesh/Curve/Point types; exercised by "
        "StageloomConversionTests.exe (37 checks) under wine, not by pure C#"
    ),
}

SUITES = (
    {
        "name": "core",
        "exe": "StageloomCoreTests.exe",
        "sources": (
            CORE / "Json.cs",
            CORE / "ProtocolTables.generated.cs",
            CORE / "Frame.cs",
            CORE / "ParamValues.cs",
            CORE / "Endpoint.cs",
            CONSOLE / "Program.cs",
        ),
        "args": (str(FIXTURES),),
    },
    {
        "name": "fuzz",
        "exe": "StageloomFuzzTests.exe",
        "sources": (
            CORE / "Json.cs",
            CORE / "ProtocolTables.generated.cs",
            CORE / "Frame.cs",
            CONSOLE / "FuzzTests.cs",
        ),
        "args": (),
    },
)


def _mono_environment():
    """Return (env, mono, mcs) for the selected Mono installation."""
    env = dict(os.environ)
    home = os.environ.get("MONO_HOME")
    if home:
        home_path = Path(home).resolve()
        prefix = home_path.parent if home_path.name == "usr" else home_path
        config = prefix / "etc" / "mono" / "config"
        if "MONO_CONFIG" not in env and config.is_file():
            env["MONO_CONFIG"] = str(config)
        env.setdefault("MONO_PATH", str(home_path / "lib" / "mono" / "4.5"))
        env.setdefault("MONO_GAC_PREFIX", str(home_path))
        libs = str(home_path / "lib")
        inherited = env.get("LD_LIBRARY_PATH")
        env["LD_LIBRARY_PATH"] = libs + (":" + inherited if inherited else "")
    else:
        home_path = None

    mono = os.environ.get("MONO")
    if not mono and home_path is not None:
        candidate = home_path / "bin" / "mono"
        if candidate.is_file():
            mono = str(candidate)
    if not mono:
        mono = shutil.which("mono")
    if not mono:
        raise SystemExit(
            "coverage-csharp: mono not found. Set MONO_HOME to a Mono prefix "
            "(bin/mono and lib/mono/4.5/mcs.exe) or put mono on PATH."
        )

    mcs = os.environ.get("MCS")
    if not mcs and home_path is not None:
        for candidate in (
            home_path / "lib" / "mono" / "4.5" / "mcs.exe",
            home_path / "bin" / "mcs.exe",
        ):
            if candidate.is_file():
                mcs = str(candidate)
                break
    if not mcs:
        mcs = shutil.which("mcs")
    if not mcs:
        raise SystemExit(
            "coverage-csharp: mcs not found. Set MONO_HOME or MCS, or put mcs "
            "on PATH."
        )
    return env, mono, mcs


def _run(command, cwd, env, label):
    started = time.monotonic()
    result = subprocess.run(
        command,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    elapsed = time.monotonic() - started
    if result.returncode != 0:
        sys.stderr.write("coverage-csharp: %s failed (exit %d)\n" % (label, result.returncode))
        sys.stderr.write("command: %s\n" % " ".join(command))
        tail = result.stdout.splitlines()[-40:]
        if tail:
            sys.stderr.write("\n".join(tail) + "\n")
        raise SystemExit(result.returncode)
    return elapsed


def _read_coverage(path, covered, seen):
    """Merge one coverage.xml into {relative file: {line: hit}}."""
    core_prefix = str(CORE.resolve()) + os.sep
    context = ET.iterparse(str(path), events=("start", "end"))
    for event, element in context:
        if event != "end" or element.tag != "method":
            continue
        filename = element.get("filename")
        if filename and filename.startswith(core_prefix):
            relative = filename[len(core_prefix):].replace(os.sep, "/")
            seen.add(relative)
            lines = covered[relative]
            for statement in element.findall("statement"):
                line = int(statement.get("line"))
                lines[line] = lines.get(line, False) or (
                    int(statement.get("counter", "0")) > 0
                )
        element.clear()


def _check_denominator(seen):
    actual = {path.name for path in CORE.glob("*.cs")}
    declared = {path.name for suite in SUITES for path in suite["sources"] if path.parent == CORE}
    uncovered = actual - declared - set(EXCLUDED)
    if uncovered:
        raise SystemExit(
            "coverage-csharp: core file(s) neither measured nor excluded: %s\n"
            "Add each to a suite in this script or to EXCLUDED with a reason."
            % ", ".join(sorted(uncovered))
        )
    empty = sorted(name for name in declared if name not in seen)
    if empty:
        raise SystemExit(
            "coverage-csharp: suite build did not emit statement data for: %s"
            % ", ".join(empty)
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--fail-under",
        type=float,
        default=80.0,
        help="minimum pure-core line coverage percent (default: 80)",
    )
    parser.add_argument(
        "--work-dir",
        help="keep the debug builds and coverage XML in this directory",
    )
    options = parser.parse_args()

    env, mono, mcs = _mono_environment()
    if options.work_dir:
        work = Path(options.work_dir).resolve()
        work.mkdir(parents=True, exist_ok=True)
        cleanup = False
    else:
        work = Path(tempfile.mkdtemp(prefix="stageloom-csharp-coverage."))
        cleanup = True

    compiler = [mono, mcs] if mcs.endswith(".exe") else [mcs]
    covered = defaultdict(dict)
    seen = set()
    timings = {}
    total_start = time.monotonic()
    try:
        for suite in SUITES:
            suite_dir = work / suite["name"]
            suite_dir.mkdir(parents=True, exist_ok=True)
            exe = suite_dir / suite["exe"]
            sources = [str(path) for path in suite["sources"]]
            build = compiler + [
                "-debug",
                "-out:%s" % exe,
                "-r:System.Numerics.dll",
            ] + sources
            timings["build " + suite["name"]] = _run(
                build, suite_dir, env, "build " + suite["name"]
            )
            run = [mono, "--profile=coverage", str(exe)] + list(suite["args"])
            timings["run " + suite["name"]] = _run(
                run, suite_dir, env, "run " + suite["name"]
            )
            coverage_file = suite_dir / "coverage.xml"
            if not coverage_file.is_file():
                raise SystemExit(
                    "coverage-csharp: %s produced no coverage.xml" % suite["name"]
                )
            _read_coverage(coverage_file, covered, seen)

        _check_denominator(seen)

        total_lines = 0
        hit_lines = 0
        print("%-40s %10s %8s" % ("file", "covered", "cover"))
        print("-" * 60)
        for relative in sorted(covered):
            lines = covered[relative]
            hits = sum(1 for hit in lines.values() if hit)
            total = len(lines)
            total_lines += total
            hit_lines += hits
            percent = 100.0 * hits / total if total else 0.0
            print("%-40s %4d/%-5d %7.2f%%" % (relative, hits, total, percent))
        print("-" * 60)
        total_percent = 100.0 * hit_lines / total_lines if total_lines else 0.0
        print("%-40s %4d/%-5d %7.2f%%" % ("TOTAL pure core", hit_lines, total_lines, total_percent))
        print()
        print("Excluded from the pure-core denominator (RhinoCommon-dependent):")
        for name in sorted(EXCLUDED):
            print("  %-24s %s" % (name, EXCLUDED[name]))
        print()
        status = "PASS" if total_percent >= options.fail_under else "FAIL"
        print(
            "threshold %.2f%%: %s (%.2f%%)"
            % (options.fail_under, status, total_percent)
        )
        elapsed = time.monotonic() - total_start
        print(
            "runtime %.1fs (%s)"
            % (
                elapsed,
                ", ".join("%s %.1fs" % item for item in timings.items()),
            )
        )
        if status == "FAIL":
            return 1
        return 0
    finally:
        if cleanup:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
