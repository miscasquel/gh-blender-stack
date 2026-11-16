"""Shared scaffolding for the Blender headless suite.

The headless runner (``run.py``) owns ``sys.path``; a test module imports the
add-on directly. A test class allocates its own port and data root in
``setUpClass``, so no module can depend on another's leftovers or on a fixed
port number. The only module state is the registry of live fake processes,
which the interpreter exit drains as a last resort.

Run with ``scripts/test-blender``; a single module is selected with
``STAGELOOM_TEST_PATTERN=test_stage_index.py``.
"""

import atexit
import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import bpy
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
FAKE_GH = os.path.join(REPO, "tools", "fake_gh", "fake_gh.py")
# The float parameter fake_gh publishes and scales by: one definition for the
# suites that keyframe it.
SCALE = "00000000-0000-4000-8000-0000000000aa"


def free_port():
    """An unused loopback port: bind zero, read the kernel's pick, close."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def pump(seconds=8.0, until=None, step=0.01):
    """Drive the engine timer directly: bpy.app.timers is inert under -b.

    ``step`` is explicit because one suite polls at half the usual interval to
    catch a short-lived state; every other caller keeps the 10 ms cadence.
    """
    import stageloom.blender.engine as engine

    deadline = time.time() + seconds
    while time.time() < deadline:
        engine._timer()
        if until is not None and until():
            return True
        time.sleep(step)
    return until is None


def pump_dirty(obj, seconds=8.0, until=None, step=0.01):
    """Like pump, but keeps the object dirty. The request timeout is only
    noticed on a pass, so a test waiting for a retry has to ask for passes (a
    scene change would do the same in a session)."""
    import stageloom.blender.engine as engine

    deadline = time.time() + seconds
    while time.time() < deadline:
        engine._mark_object_dirty(obj)
        engine._timer()
        if until is not None and until():
            return True
        time.sleep(step)
    return until is None


class FakeGh:
    """One fake Grasshopper process, bound to the port the test allocated."""

    def __init__(self, process, port, report):
        self.process = process
        self.port = port
        self.report = report


# A class fixture that raises still runs its cleanups, but a fake started
# outside one (or before its class registers the teardown) would outlive the
# suite. The interpreter exit is the last owner.
_FAKES = set()


@atexit.register
def _stop_remaining_fakes():
    for fake in list(_FAKES):
        stop_fake(fake)


def start_fake(stage_key, report, *extra, port=None):
    """Start fake_gh for one stage; without a port it allocates a free one.

    The caller normally passes the add-on server's port so both ends agree.
    ``extra`` is passed through to fake_gh (``--delay-ms``, ``--extras``, ...).
    """
    if port is None:
        port = free_port()
    process = subprocess.Popen(
        [
            sys.executable,
            FAKE_GH,
            "--port",
            str(port),
            "--stage",
            stage_key,
            "--report",
            report,
            *extra,
        ],
        env=dict(
            os.environ,
            PYTHONPATH=os.pathsep.join(
                [
                    os.path.join(
                        REPO,
                        "blender",
                        "stageloom",
                        "wheels",
                        "websockets-17.2-py3-none-any.whl",
                    ),
                    REPO,
                ]
            ),
        ),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    fake = FakeGh(process, port, report)
    _FAKES.add(fake)
    return fake


def stop_fake(fake):
    """Terminate a FakeGh (or nothing) and reap it; never raise on a dead one."""
    if fake is None:
        return
    _FAKES.discard(fake)
    process = fake.process
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _addon_preferences():
    """The add-on preferences, or None while the add-on is disabled."""
    try:
        return bpy.context.preferences.addons["stageloom"].preferences
    except (KeyError, AttributeError):
        return None


@contextlib.contextmanager
def isolate_data_root(prefix="stageloom-test-"):
    """Point the add-on data root at a temp dir and remove it afterwards.

    Enter after ``addon_enable``: the preference object only exists while the
    add-on is registered. Leaving tolerates the add-on being disabled, which is
    the usual ``tearDownClass`` order, and restores the previous value when the
    preference still exists.
    """
    root = tempfile.mkdtemp(prefix=prefix)
    prefs = _addon_preferences()
    previous = prefs.data_root if prefs is not None else None
    if prefs is not None:
        prefs.data_root = root
    try:
        yield root
    finally:
        prefs = _addon_preferences()
        if prefs is not None and previous is not None:
            prefs.data_root = previous
        shutil.rmtree(root, ignore_errors=True)


def report_of(path):
    """A fake's JSON report, or {} while it is missing or half-written."""
    try:
        with open(path) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def requests_of(path):
    report = report_of(path)
    return report.get("full", 0) + report.get("ref", 0)


def helper_max(obj, stage_key):
    """The largest absolute coordinate in a stage's mesh helper: what actually
    landed on screen (the fake scales the stored geometry by the parameter)."""
    import stageloom.blender.helpers as helpers

    helper = helpers.helper_for(obj, stage_key, "mesh")
    if helper is None or not len(helper.data.vertices):
        return 0.0
    count = len(helper.data.vertices)
    co = np.empty(count * 3, dtype=np.float32)
    helper.data.vertices.foreach_get("co", co)
    return float(np.abs(co.reshape(-1, 3)).max())


def keyframe_scale(obj, stage, values):
    """Keyframe the stage's float parameter at frames 1..len(values)."""
    import stageloom.blender.stack as stack_mod

    name = stack_mod.param_prop_name(stage.key, SCALE)
    if name not in obj:
        obj[name] = 1.0
    for frame, value in enumerate(values, start=1):
        obj[name] = float(value)
        obj.keyframe_insert(data_path='["%s"]' % name, frame=frame)


def settle(
    engine,
    stack_mod,
    objects,
    settled,
    total=40.0,
    rearm_quiet=6.0,
    include_error=False,
):
    """Pump until settled, re-arming requests only when they have gone quiet.

    A class transition can leave the server holding a dead socket, so a lost
    first request is normal. Re-arming a *young* request is not the fix: under a
    slow fake it starves the answer that was about to land. A request is
    replaced only after ``rearm_quiet`` seconds with nothing else in flight.
    ``include_error`` adds the stage error to the failure, for the suite whose
    fakes fail on purpose.
    """
    deadline = time.time() + total
    while time.time() < deadline and not settled():
        quiet = True
        for obj in objects:
            record = stack_mod.stack_of(obj)
            if record is None:
                continue
            for stage in record.stages:
                age = engine.waiting_seconds(record.stack_id, stage.key)
                if age and age < rearm_quiet:
                    quiet = False
        if quiet:
            for obj in objects:
                engine.request_again(obj)
        pump(seconds=2.0, until=settled)
    if not settled():
        states = []
        for obj in objects:
            record = stack_mod.stack_of(obj)
            if record is None:
                continue
            for stage in record.stages:
                state = (obj.name, stage.key, stage.state)
                states.append(state + (stage.error,) if include_error else state)
        raise AssertionError("the fake never settled: %s" % (states,))
