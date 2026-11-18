#!/usr/bin/env python3
"""Run the Blender headless suite under coverage inside Blender's Python.

`scripts/coverage engine` starts Blender with this file after installing the
pinned coverage.py into a temporary target. The shipped add-on does not touch
``sys.path``; this wrapper (test/tooling only) puts the coverage target first so
Blender's bundled Python 3.13 can import it.

Environment:
    STAGELOOM_COVERAGE_PATH  directory containing the coverage package
    STAGELOOM_COVERAGE_DATA  path for the coverage data file
    STAGELOOM_REPO           repository root (defaults to this file's parent)
"""

import atexit
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.environ.get("STAGELOOM_REPO") or os.path.dirname(HERE))
TARGET = os.environ.get("STAGELOOM_COVERAGE_PATH")
DATA = os.environ.get("STAGELOOM_COVERAGE_DATA")
HEADLESS = os.path.join(REPO, "blender", "tests", "headless", "run.py")
ENGINE = os.path.join(REPO, "blender", "stageloom", "blender", "engine")
GEOMETRY = os.path.join(REPO, "blender", "stageloom", "blender", "geometry_io")

if not TARGET or not DATA:
    sys.stderr.write(
        "coverage: STAGELOOM_COVERAGE_PATH and STAGELOOM_COVERAGE_DATA are "
        "required (run this through scripts/coverage engine)\n"
    )
    raise SystemExit(2)
if not os.path.isdir(TARGET):
    sys.stderr.write("coverage: no coverage package at %s\n" % TARGET)
    raise SystemExit(2)

sys.path.insert(0, TARGET)
import coverage  # noqa: E402

cov = coverage.Coverage(data_file=DATA, source=[ENGINE, GEOMETRY])
cov.start()

_saved = False


def _save():
    global _saved
    if _saved:
        return
    _saved = True
    cov.stop()
    cov.save()


# A normal return, a test exception and sys.exit all pass through `finally`;
# the atexit registration is the backstop for an exception raised by the
# interpreter shutdown path itself.
atexit.register(_save)

status = 1
try:
    spec = importlib.util.spec_from_file_location("stageloom_headless_runner", HEADLESS)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    status = runner.main()
finally:
    _save()

raise SystemExit(status)
