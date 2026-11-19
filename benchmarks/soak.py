"""One-hour churn soak for Stageloom's 0.8.0 invariants (T10d).

    scripts/soak --duration 3600 --out benchmarks/results/2026-11-19-soak.json
    scripts/soak --quick
    scripts/soak --duration 600 --out /tmp/soak.json

The runner drives the real add-on under ``blender -b --factory-startup
--online-mode`` from the working tree, with one real WebSocket server and
``tools/fake_gh`` clients. Each cycle adds a stage, starts its fake, sends a
full and a parameter-only request, renames the stage and the object, exercises
the engine's undo/redo handler, freezes/unfreezes, disables/enables, seeds a
two-part definition and both row-cloud families, deletes the stage and asserts
that every helper, definition, cloud, parameter, runtime and registry entry is
gone. Periodically it deletes a whole stack object, reconnects every client
with a stale-answer check, runs a playback frame, and runs a real native
Geometry Nodes bake plus Clear Bake (or the bake-marker orchestration when the
native operator is unavailable).

The runner records wall time, cycles, operations, recoveries, RSS and final
registry/helper counts in JSON and exits non-zero on:

- an unclassified exception or an engine error in the trace;
- a wait past its per-cycle deadline or the hard wall-clock guard;
- a leaked helper/definition/cloud/parameter/runtime/registry entry;
- an engine collection that grows past the live stages it describes;
- a missing or malformed classified-recovery state (stale answer, reconnect,
  native bake markers, playback frame).

``--quick`` is a short local smoke (20 s), not the 1-hour claim. The full run is
a local/release check; it is deliberately not part of ``scripts/check-all`` or
the benchmark matrix. Blender's real undo/redo operator cannot run in
background mode (its poll fails), so the engine's undo/redo handlers — the code
a real history step runs — are exercised and the operator limitation is
recorded. The native bake is Blender's own Geometry Nodes bake, not a native
Windows/Rhino acceptance run; that remains explicitly deferred.
"""

import argparse
import hashlib
import json
import os
import platform
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

import bpy
import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
WHEEL = REPO / "blender" / "stageloom" / "wheels" / "websockets-17.2-py3-none-any.whl"
BLENDER_PKG = REPO / "blender"
FAKE_GH = REPO / "tools" / "fake_gh" / "fake_gh.py"

for path in (str(HERE), str(REPO), str(BLENDER_PKG), str(WHEEL)):
    if path not in sys.path:
        sys.path.insert(0, path)

import bench as harness  # noqa: E402
from stageloom.blender import bridge, helpers, nodes, ops, prefs  # noqa: E402
from stageloom.blender import stack as stack_mod  # noqa: E402
from stageloom.blender.engine import ENGINE  # noqa: E402
from stageloom.blender.engine import bake as bake_mod  # noqa: E402
from stageloom.blender.engine import lifecycle, messages, playback, triggers  # noqa: E402

SCALE = "00000000-0000-4000-8000-0000000000aa"
HELPER_NAME = re.compile(r"^SL ([0-9a-f]{16})\.")
DATA_ROOT = tempfile.mkdtemp(prefix="stageloom-soak-")
BLEND_PATH = os.path.join(DATA_ROOT, "soak.blend")
ERROR_MARKERS = (
    "engine error",
    "playback gate failed",
    "guard watchdog",
    "incoming message failed",
)

CHECKS = {}
OPERATIONS = {}
RECOVERIES = {}
RECOVERY_SAMPLES = []
REPORT = {}
POOL = None
_CYCLE_DEADLINE = None
_HARD_DEADLINE = None
_CURRENT_CYCLE = 0


class SoakError(RuntimeError):
    """A soak precondition or classified state failed."""


class SoakInvariantError(SoakError):
    """A soak invariant failed."""


class SoakTimeout(SoakError):
    """The per-cycle deadline or the hard wall-clock guard fired."""


# --------------------------------------------------------------------- helpers


def parse_args():
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    else:
        argv = []
    parser = argparse.ArgumentParser(prog="scripts/soak")
    parser.add_argument(
        "--duration",
        type=int,
        default=None,
        help="churn seconds (default: 3600, or 20 with --quick)",
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="20-second smoke with the same invariants (not the 1-hour claim)",
    )
    parser.add_argument("--out", default="", help="write the JSON report here")
    return parser.parse_args(argv)


def rss_mb():
    """Resident set size in MiB, or None when the platform cannot report it."""
    try:
        with open("/proc/self/status", "r") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except OSError:
        pass
    try:
        import resource

        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return value / (1024.0 * 1024.0) if sys.platform == "darwin" else value / 1024.0
    except (ImportError, OSError):
        return None


def peak_rss_mb():
    """Peak resident set size in MiB, or None when the platform cannot report it."""
    try:
        with open("/proc/self/status", "r") as handle:
            for line in handle:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1]) / 1024.0
    except OSError:
        pass
    try:
        import resource

        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return value / (1024.0 * 1024.0) if sys.platform == "darwin" else value / 1024.0
    except (ImportError, OSError):
        return None


def free_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def increment(table, key, amount=1):
    table[key] = int(table.get(key, 0)) + int(amount)


def require(name, condition, detail=""):
    """Record one invariant run and stop on failure."""
    entry = CHECKS.setdefault(
        name, {"name": name, "runs": 0, "failures": 0, "last_detail": ""}
    )
    entry["runs"] += 1
    if condition:
        return True
    entry["failures"] += 1
    entry["last_detail"] = str(detail)
    raise SoakInvariantError(
        "%s%s" % (name, ": " + str(detail) if detail else "")
    )


def recovery(kind, detail):
    increment(RECOVERIES, kind)
    if len(RECOVERY_SAMPLES) < 20:
        RECOVERY_SAMPLES.append(
            {"cycle": _CURRENT_CYCLE, "kind": kind, "detail": str(detail)}
        )


def pump(seconds, until, label="wait"):
    """Drive the engine timer until ``until`` or the wait/cycle/global deadline.

    The engine timer is inert under ``-b``; calling it here is the same pass a
    live session receives. A per-cycle or hard wall-clock overrun raises, so a
    wedged operation cannot be recorded as a slow success.
    """
    deadline = time.monotonic() + max(0.0, float(seconds))
    if _CYCLE_DEADLINE is not None:
        deadline = min(deadline, _CYCLE_DEADLINE)
    while True:
        triggers._timer()
        if until():
            return True
        now = time.monotonic()
        if _CYCLE_DEADLINE is not None and now >= _CYCLE_DEADLINE:
            raise SoakTimeout("per-cycle deadline exceeded while %s" % label)
        if now >= _HARD_DEADLINE:
            raise SoakTimeout("hard wall-clock guard exceeded while %s" % label)
        if now >= deadline:
            return False
        time.sleep(0.005)


def live_stacks():
    """Every object with a live stage record, with reference errors skipped."""
    found = []
    for obj in list(bpy.data.objects):
        try:
            record = stack_mod.stack_of(obj)
        except ReferenceError:
            continue
        if record is not None and len(record.stages):
            found.append((obj, record))
    return found


def live_stages():
    return [
        (obj, record, stage)
        for obj, record in live_stacks()
        for stage in record.stages
    ]


def all_settled():
    for _obj, record, stage in live_stages():
        if stage.state != "ok":
            return False
        if ENGINE.is_pending(record.stack_id, stage.key):
            return False
    return not ENGINE.dirty


def stage_states():
    return [
        (obj.name, stage.key, stage.state, stage.error)
        for obj, _record, stage in live_stages()
    ]


def settle(label, seconds=20.0):
    if not pump(seconds, all_settled, label):
        raise SoakInvariantError(
            "%s did not settle: %r" % (label, stage_states())
        )


def tick(times=3):
    """Run a few synchronous engine passes without waiting for a clock."""
    for _ in range(times):
        triggers._timer()
        if not ENGINE.active:
            break


def wait_clients(expected, seconds=15.0):
    if not pump(
        seconds,
        lambda: bridge.state["clients"] == expected,
        "clients == %d" % expected,
    ):
        raise SoakInvariantError(
            "expected %d client(s), found %d"
            % (expected, bridge.state["clients"])
        )


def mesh_geometry(mesh):
    """A stable (vertex count, coordinate hash, max coordinate) for a mesh."""
    count = len(mesh.vertices)
    if not count:
        return (0, "", 0.0)
    co = np.empty(count * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", co)
    return (
        count,
        hashlib.sha256(co.tobytes()).hexdigest(),
        float(np.abs(co).max()),
    )


def evaluated_geometry(obj):
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    if mesh is None:
        return (0, "", 0.0)
    try:
        return mesh_geometry(mesh)
    finally:
        evaluated.to_mesh_clear()


def helper_max(obj, stage_key):
    helper = helpers.helper_for(obj, stage_key, "mesh")
    if helper is None:
        return 0.0
    return mesh_geometry(helper.data)[2]


def keyframe_scale(obj, stage, values):
    name = stack_mod.param_prop_name(stage.key, SCALE)
    if name not in obj:
        obj[name] = 1.0
    for frame, value in enumerate(values, start=1):
        obj[name] = float(value)
        obj.keyframe_insert(data_path='["%s"]' % name, frame=frame)


def remove_object_data(obj):
    """Remove one user object and its mesh/curve/pointcloud data in the soak.

    This is harness cleanup, not add-on behavior: Blender leaves the data
    datablock orphaned when the object is removed, and a one-hour churn loop
    must not grow that table just to create a fresh object each cycle.
    """
    data = getattr(obj, "data", None)
    bpy.data.objects.remove(obj, do_unlink=True)
    if data is None or data.users:
        return
    if isinstance(data, bpy.types.Mesh):
        bpy.data.meshes.remove(data)
    elif isinstance(data, bpy.types.Curve):
        bpy.data.curves.remove(data)
    elif isinstance(data, bpy.types.PointCloud):
        bpy.data.pointclouds.remove(data)


# ---------------------------------------------------------------- fake clients


class FakeClient:
    __slots__ = ("process", "report")

    def __init__(self, process, report):
        self.process = process
        self.report = report


class FakePool:
    """The fake_gh processes for the live stage keys, one per key."""

    def __init__(self, port):
        self.port = port
        self.directory = os.path.join(DATA_ROOT, "fakes")
        os.makedirs(self.directory, exist_ok=True)
        self.clients = {}

    def start(self, stage_key, *extra):
        if stage_key in self.clients:
            return
        report = os.path.join(self.directory, "%s.json" % stage_key)
        try:
            os.remove(report)
        except OSError:
            pass
        env = dict(
            os.environ,
            PYTHONPATH=os.pathsep.join([str(WHEEL), str(REPO)]),
        )
        command = [
            sys.executable,
            str(FAKE_GH),
            "--port",
            str(self.port),
            "--stage",
            stage_key,
            "--report",
            report,
            *extra,
        ]
        process = subprocess.Popen(
            command,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
        self.clients[stage_key] = FakeClient(process, report)

    def stop(self, stage_key):
        client = self.clients.pop(stage_key, None)
        if client is None:
            return
        process = client.process
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        try:
            os.remove(client.report)
        except OSError:
            pass

    def stop_all(self):
        for key in list(self.clients):
            self.stop(key)

    def report(self, stage_key):
        client = self.clients.get(stage_key)
        if client is None:
            return {}
        try:
            with open(client.report) as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return {}

    def requests(self, stage_key):
        report = self.report(stage_key)
        return int(report.get("full") or 0) + int(report.get("ref") or 0)

    def check_alive(self):
        for key, client in self.clients.items():
            code = client.process.poll()
            if code is not None:
                raise SoakInvariantError(
                    "fake_gh for %s exited with code %s" % (key, code)
                )

    def expected(self):
        return len(self.clients)


# ----------------------------------------------------------------------- rigs


class Rigs:
    def __init__(self):
        self.churn = None
        self.churn_stage = None
        self.playback = None
        self.playback_stage = None
        self.bake = None
        self.bake_a = None
        self.bake_b = None
        self.scene = None


def setup_rigs():
    global POOL
    harness.start_addon()
    prefs._log_info = False
    # Bounded playback/bake waits: the gate's default hard cap is ten minutes,
    # which would outlive the per-cycle guard if a fake wedged. Ten seconds is
    # far beyond a local answer and keeps a bad frame a classified failure.
    # The add-on is registered directly (not through add-on preferences), so
    # this is the module constant the preference fallback reads.
    playback.PLAYBACK_HARD_CAP_SECONDS = 5.0
    # Blender's undo operator cannot run in background mode. Disabling the
    # global undo stack also keeps the harness from accumulating one step per
    # added stage; the engine's undo/redo handler is exercised instead.
    bpy.context.preferences.edit.use_global_undo = False
    port = free_port()
    if not ENGINE.start_server(port):
        raise SoakError("the add-on server did not start on port %d" % port)
    if not pump(10.0, lambda: bridge.state["listening"], "server listening"):
        raise SoakError("the add-on server never listened")
    POOL = FakePool(port)

    rigs = Rigs()
    verts, quads, _actual = harness.grid_arrays(400)
    rigs.churn = harness.mesh_object("Soak Churn", verts, quads)
    rigs.churn_stage = harness.add_stage(rigs.churn, "Soak Base")

    bpy.ops.mesh.primitive_cube_add(size=2.0, calc_uvs=False)
    rigs.playback = bpy.context.object
    rigs.playback.name = "Soak Playback"
    rigs.playback_stage = harness.add_stage(rigs.playback, "Soak Playback")

    bpy.ops.mesh.primitive_cube_add(size=2.0, calc_uvs=False)
    rigs.bake = bpy.context.object
    rigs.bake.name = "Soak Bake"
    rigs.bake_a = harness.add_stage(rigs.bake, "Bake A")
    bevel = rigs.bake.modifiers.new("Soak Bevel", "BEVEL")
    bevel.width = 0.1
    rigs.bake.modifiers.active = bevel
    rigs.bake_b = harness.add_stage(rigs.bake, "Bake B")

    rigs.scene = bpy.context.scene
    rigs.scene.frame_start = 1
    rigs.scene.frame_end = 3
    rigs.scene.frame_set(1)
    rigs.scene.sync_mode = "NONE"

    for stage in (
        rigs.churn_stage,
        rigs.playback_stage,
        rigs.bake_a,
        rigs.bake_b,
    ):
        POOL.start(stage.key, "--delay-ms", "3")
    wait_clients(4)
    for stage in (rigs.playback_stage, rigs.bake_b):
        if not pump(
            15.0,
            lambda stage=stage: any(entry.param_id == SCALE for entry in stage.params),
            "schema for %s" % stage.key,
        ):
            raise SoakInvariantError(
                "the fake never published the float param for %s" % stage.key
            )
        keyframe_scale(
            rigs.playback if stage is rigs.playback_stage else rigs.bake,
            stage,
            (1.0, 2.0, 3.0),
        )
    rigs.scene.frame_set(1)
    ENGINE.mark_dirty_all()
    settle("setup settle")

    bpy.ops.wm.save_as_mainfile(filepath=BLEND_PATH)
    return rigs


def probe_undo():
    try:
        available = bool(bpy.ops.ed.undo.poll())
        error = ""
    except Exception as exc:  # noqa: BLE001 - the probe records the limitation
        available = False
        error = "%s: %s" % (type(exc).__name__, exc)
    return available, error


# ------------------------------------------------------------ invariant checks


def helper_bases():
    bases = {}
    for obj, record in live_stacks():
        for stage in record.stages:
            bases[helpers.helper_name(obj, stage.key)] = (obj, record, stage)
    return bases


def plumbing_names():
    names = []
    for table in (
        bpy.data.objects,
        bpy.data.collections,
        bpy.data.meshes,
        bpy.data.curves,
        bpy.data.pointclouds,
    ):
        for datablock in list(table):
            if HELPER_NAME.match(datablock.name):
                names.append(datablock.name)
    return names


def orphan_plumbing(bases):
    bases = list(bases)
    return [
        name
        for name in plumbing_names()
        if not any(name == base or name.startswith(base + ".") for base in bases)
    ]


def orphan_groups(live_keys):
    return [
        group.name
        for group in bpy.data.node_groups
        if group.get("gh_stack_format") is not None
        and group.name.startswith(nodes.GROUP_PREFIX)
        and group.name[len(nodes.GROUP_PREFIX):] not in live_keys
    ]


def engine_snapshot():
    stacks = live_stacks()
    stages = live_stages()
    live_keys = {
        (record.stack_id, stage.key)
        for _obj, record, stage in stages
        if record.stack_id and stage.key
    }
    bases = helper_bases()
    helper_objects = 0
    for obj, record in stacks:
        for stage in record.stages:
            for kind in helpers.KINDS:
                if helpers.helper_for(obj, stage.key, kind) is not None:
                    helper_objects += 1
    definition_collections = 0
    instance_clouds = 0
    nested_clouds = 0
    for obj, record in stacks:
        for stage in record.stages:
            definition_collections += len(
                helpers.definition_collections(obj, stage.key)
            )
            instance_clouds += len(helpers.instance_clouds(obj, stage.key))
            nested_clouds += len(helpers.nested_clouds(obj, stage.key))
    return {
        "stack_objects": len(stacks),
        "stages": len(stages),
        "stage_index_keys": len(ENGINE.stage_index),
        "stage_owners": len(ENGINE.stage_owners),
        "stage_claims": sum(ENGINE.stage_claims.values()),
        "stage_conflicts": len(ENGINE.stage_conflicts),
        "runtimes": len(ENGINE._runtimes),
        "pending_stages": sum(
            1 for runtime in ENGINE._runtimes.values() if runtime.pending
        ),
        "dirty": len(ENGINE.dirty),
        "verify": len(ENGINE.verify),
        "prefix_memo": len(ENGINE.prefix_memo),
        "playback_reports": len(ENGINE.playback_reports),
        "helper_objects": helper_objects,
        "definition_collections": definition_collections,
        "instance_clouds": instance_clouds,
        "nested_clouds": nested_clouds,
        "node_groups": sum(
            1
            for group in bpy.data.node_groups
            if group.get("gh_stack_format") is not None
        ),
        "orphan_plumbing": len(orphan_plumbing(bases)),
        "orphan_groups": len(orphan_groups({key for _sid, key in live_keys})),
        "bpy_objects": len(bpy.data.objects),
        "bpy_meshes": len(bpy.data.meshes),
        "bpy_curves": len(bpy.data.curves),
        "bpy_pointclouds": len(bpy.data.pointclouds),
        "bpy_collections": len(bpy.data.collections),
        "bpy_node_groups": len(bpy.data.node_groups),
    }


def scan_engine_trace(label):
    lines = ENGINE.recent_activity(200)
    for marker in ERROR_MARKERS:
        for line in lines:
            if marker in line:
                require(
                    "no engine failure trace",
                    False,
                    "%s: %s" % (label, line),
                )


def check_engine_bookkeeping(label):
    """The periodic registry/helper/session invariant for the live scene."""
    lifecycle._ensure_index()
    stacks = live_stacks()
    stages = live_stages()
    stack_ids = {obj.as_pointer() for obj, _record in stacks}
    live_keys = {
        (record.stack_id, stage.key)
        for _obj, record, stage in stages
        if record.stack_id and stage.key
    }
    stage_keys = {stage.key for _obj, _record, stage in stages}
    bases = helper_bases()

    require(
        "registry: stack objects match the scene",
        {obj.as_pointer() for obj in ENGINE.stack_objects} == stack_ids,
        "index=%d scene=%d" % (len(ENGINE.stack_objects), len(stack_ids)),
    )
    require(
        "registry: stage keys match the scene",
        set(ENGINE.stage_index) == stage_keys,
        "index=%r scene=%r"
        % (sorted(ENGINE.stage_index), sorted(stage_keys)),
    )
    require(
        "registry: every key has one owner",
        set(ENGINE.stage_owners) == stage_keys
        and all(ENGINE.stage_claims.get(key) == 1 for key in stage_keys),
        "owners=%d claims=%r"
        % (len(ENGINE.stage_owners), dict(ENGINE.stage_claims)),
    )
    require(
        "registry: no conflicts",
        not ENGINE.stage_conflicts,
        "conflicts=%r" % sorted(ENGINE.stage_conflicts),
    )
    require(
        "engine: runtimes describe live stages only",
        set(ENGINE._runtimes) <= live_keys,
        "%d runtime(s), %d live key(s)"
        % (len(ENGINE._runtimes), len(live_keys)),
    )
    require(
        "engine: no pending at rest",
        all(not runtime.pending for runtime in ENGINE._runtimes.values()),
        "pending=%r"
        % [
            key
            for key, runtime in ENGINE._runtimes.items()
            if runtime.pending
        ],
    )
    require(
        "engine: sent rows are live and well shaped",
        all(
            runtime.sent is None
            or (
                key in live_keys
                and isinstance(runtime.sent, tuple)
                and len(runtime.sent) == 2
            )
            for key, runtime in ENGINE._runtimes.items()
        ),
        "%r"
        % {
            key: runtime.sent
            for key, runtime in ENGINE._runtimes.items()
            if runtime.sent is not None
        },
    )
    require("engine: dirty empty at rest", not ENGINE.dirty, repr(ENGINE.dirty))
    require("engine: verify empty at rest", not ENGINE.verify, repr(ENGINE.verify))
    live_pointers = {
        (obj.as_pointer(), stage.key)
        for obj, _record, stage in stages
    }
    require(
        "engine: prefix memo bounded",
        len(ENGINE.prefix_memo) <= len(stages)
        and set(ENGINE.prefix_memo) <= live_pointers,
        "%d memo(s), %d stage(s)" % (len(ENGINE.prefix_memo), len(stages)),
    )
    require(
        "engine: no playback reports at rest",
        not ENGINE.playback_reports,
        repr(ENGINE.playback_reports),
    )
    require(
        "engine: trace ring bounded",
        len(ENGINE.trace_lines) <= 200,
        "%d lines" % len(ENGINE.trace_lines),
    )
    require(
        "engine: flags and escrow clear at rest",
        not ENGINE.evaluating
        and not ENGINE.ticking
        and not ENGINE.playback_sync
        and not ENGINE.bake_sync
        and ENGINE.bake_progress is None
        and ENGINE.playback_scene is None
        and ENGINE.bake_scene is None
        and not ENGINE.switching_scene,
        "evaluating=%r ticking=%r playback=%r bake=%r"
        % (
            ENGINE.evaluating,
            ENGINE.ticking,
            ENGINE.playback_sync,
            ENGINE.bake_sync,
        ),
    )
    require(
        "mode: object mode at rest",
        bpy.context.mode == "OBJECT",
        bpy.context.mode,
    )
    for obj, record in stacks:
        for stage in record.stages:
            missing = helpers.missing_kinds(obj, stage.key)
            require(
                "helpers: every kind exists",
                not missing,
                "%s/%s missing %r" % (obj.name, stage.key, missing),
            )
    require(
        "plumbing: no orphan helper/definition/cloud",
        not orphan_plumbing(bases),
        "%r" % orphan_plumbing(bases),
    )
    require(
        "plumbing: no orphan stage group",
        not orphan_groups(stage_keys),
        "%r" % orphan_groups(stage_keys),
    )
    require(
        "bridge: no unexpected error at rest",
        not bridge.state["error"],
        bridge.state["error"],
    )
    scan_engine_trace(label)
    increment(OPERATIONS, "bookkeeping_checks")


def check_deleted_stage(obj, stage_key, stack_id, params, group):
    """Every per-stage datablock and session entry is gone after a deletion."""
    base = helpers.helper_name(obj, stage_key)
    residual = [
        item.name
        for item in list(bpy.data.objects)
        + list(bpy.data.collections)
        + list(bpy.data.meshes)
        + list(bpy.data.curves)
        + list(bpy.data.pointclouds)
        if item.name == base or item.name.startswith(base + ".")
    ]
    require(
        "delete: helper/definition/cloud names removed",
        not residual,
        "%r" % residual,
    )
    key = (stack_id, stage_key)
    require(
        "delete: runtime and pending removed",
        ENGINE.peek_runtime(*key) is None,
        repr(ENGINE.peek_runtime(*key)),
    )
    require(
        "delete: verify removed",
        key not in ENGINE.verify,
        repr(ENGINE.verify),
    )
    require(
        "delete: playback report removed",
        stage_key not in ENGINE.playback_reports,
        repr(ENGINE.playback_reports),
    )
    require(
        "delete: node group removed",
        bpy.data.node_groups.get(group) is None,
        group,
    )
    require(
        "delete: stage parameters removed",
        not any(name.startswith(stack_mod.stage_prefix(stage_key)) for name in obj.keys()),
        "%r" % params,
    )


# ------------------------------------------------------------------- operations


def request_full(obj, stage):
    record = stack_mod.stack_of(obj)
    ENGINE.runtime(record.stack_id, stage.key).sent = None
    ENGINE.request_again(obj, stage)
    settle("full request for %s" % stage.key)
    return record


def request_ref(obj, stage, record):
    entry = next(
        (param for param in stage.params if param.param_id == SCALE), None
    )
    require("request: schema published a float param", entry is not None)
    prop = stack_mod.param_prop_name(stage.key, entry.param_id)
    report = POOL.report(stage.key)
    before = int(report.get("ref") or 0)
    obj[prop] = float(obj.get(prop, 1.0)) + 0.1
    ENGINE.mark_object_dirty(obj)
    if not pump(
        10.0,
        lambda: int(POOL.report(stage.key).get("ref") or 0) > before,
        "ref request for %s" % stage.key,
    ):
        raise SoakInvariantError(
            "no ref request reached the fake for %s" % stage.key
        )
    settle("ref request for %s" % stage.key)
    increment(OPERATIONS, "request_ref")


def stale_answer(obj, stage, old_rid, old_hash, label):
    """Post a well-formed answer for a superseded request; it must be dropped."""
    applies = ENGINE.stats()["applies"]
    sections = {
        "mesh.verts": np.array(
            [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
            dtype=np.float32,
        ),
        "mesh.face_counts": np.array([3], dtype=np.uint32),
        "mesh.face_indices": np.array([0, 1, 2], dtype=np.uint32),
    }
    header = {
        "type": "response",
        "stage": stage.key,
        "rid": old_rid or "sha256:soak-stale",
        "out_hash": "sha256:soak-stale",
        "status": "ok",
        "warnings": [],
        "counts": {"meshes": 1, "subd": 0, "curves": 0, "points": 0},
    }
    bridge.post_incoming(header, sections)
    messages._handle_incoming()
    require(
        "%s: stale answer dropped" % label,
        ENGINE.stats()["applies"] == applies
        and stage.applied_out_hash == old_hash,
        "applies %d -> %d, hash %r -> %r"
        % (
            applies,
            ENGINE.stats()["applies"],
            old_hash,
            stage.applied_out_hash,
        ),
    )
    recovery("stale-answer", label)


def playback_cycle(rigs):
    settle("playback pre-settle")
    scene = rigs.scene
    engine_playback = playback
    engine_playback._on_playback_pre(scene)
    try:
        for frame, expected in ((1, 1.0), (2, 2.0), (3, 3.0)):
            scene.frame_set(frame)
            ready = engine_playback._sync_playback_frame(scene)
            require(
                "playback: frame %d ready" % frame,
                ready,
                "states=%r" % (stage_states(),),
            )
            require(
                "playback: frame %d applied" % frame,
                abs(
                    helper_max(rigs.playback, rigs.playback_stage.key)
                    - expected
                )
                < 0.02,
                "helper max %r, expected %r"
                % (
                    helper_max(rigs.playback, rigs.playback_stage.key),
                    expected,
                ),
            )
            increment(OPERATIONS, "playback_frames")
    finally:
        engine_playback._on_playback_post(scene)
    stats = ENGINE.stats()
    require(
        "playback: every frame synced",
        stats["playback_frames"] >= 3
        and stats["playback_failures"] == 0
        and stats["playback_synced"] == stats["playback_frames"],
        "frames=%d synced=%d failures=%d"
        % (
            stats["playback_frames"],
            stats["playback_synced"],
            stats["playback_failures"],
        ),
    )
    increment(OPERATIONS, "playback_cycles")
    settlement = pump(5.0, all_settled, "playback post-settle")
    require("playback: stage settled after stop", settlement, repr(stage_states()))


def freeze_cycle(obj, stage):
    settle("freeze pre-settle")
    sends = ENGINE.stats()["sends"]
    stage.frozen = True
    ENGINE.freeze(obj, stage)
    require(
        "freeze: flag state is frozen",
        stage.state == "frozen",
        stage.state,
    )
    ENGINE.mark_object_dirty(obj)
    tick()
    require(
        "freeze: frozen stage sends nothing",
        ENGINE.stats()["sends"] == sends,
        "sends %d -> %d" % (sends, ENGINE.stats()["sends"]),
    )
    stage.frozen = False
    ENGINE.unfreeze(obj, stage)
    settle("freeze unfreeze")
    increment(OPERATIONS, "freeze_cycles")


def disabled_cycle(obj, stage):
    modifier = nodes.modifier_of(obj, stage)
    require("disable: modifier exists", modifier is not None)
    settle("disable pre-settle")
    sends = ENGINE.stats()["sends"]
    for flag in ("show_viewport", "show_render", "show_in_editmode"):
        setattr(modifier, flag, False)
    ENGINE.mark_object_dirty(obj)
    if not pump(
        5.0, lambda: stage.state == "disabled", "disable transition"
    ):
        raise SoakInvariantError(
            "the stage did not become disabled: %s" % stage.state
        )
    tick()
    require(
        "disable: disabled stage sends nothing",
        ENGINE.stats()["sends"] == sends,
        "sends %d -> %d" % (sends, ENGINE.stats()["sends"]),
    )
    for flag in ("show_viewport", "show_render", "show_in_editmode"):
        setattr(modifier, flag, True)
    ENGINE.mark_object_dirty(obj)
    settle("enable transition")
    require(
        "disable: enabling asks again",
        ENGINE.stats()["sends"] > sends,
        "sends %d -> %d" % (sends, ENGINE.stats()["sends"]),
    )
    increment(OPERATIONS, "disabled_cycles")


def marker_fallback_cycle(rigs):
    """Exercise the bake-token bookkeeping when the native operator is absent."""
    settle("marker bake pre-settle")
    token = "soak" + secrets.token_hex(6)
    bake_mod.freeze_stage(rigs.bake, rigs.bake_a, token)
    rigs.bake_b.baked = True
    rigs.bake_b.frozen = True
    rigs.bake_b.bake_token = token
    try:
        require(
            "marker bake: token names the upstream stage",
            [
                (obj.name, stage.key)
                for obj, stage in bake_mod.token_stages(token)
            ]
            == [(rigs.bake.name, rigs.bake_a.key)],
            "token set differs",
        )
        released = bake_mod.release_token(token)
        require(
            "marker bake: release unfreezes only the token's upstream",
            released == 1
            and not rigs.bake_a.frozen
            and rigs.bake_a.frozen_by_bake == ""
            and rigs.bake_b.baked
            and rigs.bake_b.frozen,
            "released=%d" % released,
        )
    finally:
        rigs.bake_a.frozen = False
        rigs.bake_a.frozen_by_bake = ""
        rigs.bake_b.baked = False
        rigs.bake_b.frozen = False
        rigs.bake_b.bake_token = ""
        ENGINE.unfreeze(rigs.bake, rigs.bake_a)
        ENGINE.unfreeze(rigs.bake, rigs.bake_b)
    settle("marker bake after-clear")
    increment(OPERATIONS, "marker_bake_cycles")


def native_bake_cycle(rigs):
    settle("bake pre-settle")
    obj = rigs.bake
    stage_a = rigs.bake_a
    stage_b = rigs.bake_b
    modifier = nodes.modifier_of(obj, stage_b)
    require("bake: stage modifier exists", modifier is not None)
    require("bake: file is saved", bool(bpy.data.is_saved))
    modifier.bake_target = "PACKED"
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    result = bpy.ops.stageloom.bake(index=1)
    require("bake: operator finished", "FINISHED" in result, repr(result))
    token = stage_b.bake_token
    require(
        "bake: token and markers",
        len(token) == 16
        and stage_b.baked
        and stage_b.frozen
        and stage_a.frozen
        and stage_a.frozen_by_bake == token,
        "token=%r baked=%r frozen=%r marker=%r"
        % (
            token,
            stage_b.baked,
            stage_b.frozen,
            stage_a.frozen_by_bake,
        ),
    )
    entry = ops._native_bake_entry(modifier)
    require("bake: native entry present", entry is not None)
    require(
        "bake: packed target",
        getattr(entry, "directory", "") == "",
        getattr(entry, "directory", ""),
    )

    reports = {
        stage_a.key: POOL.requests(stage_a.key),
        stage_b.key: POOL.requests(stage_b.key),
    }
    baked = {}
    original = rigs.scene.frame_current
    try:
        for frame, expected in ((1, 1.0), (2, 2.0), (3, 3.0)):
            rigs.scene.frame_set(frame)
            bpy.context.view_layer.update()
            baked[frame] = evaluated_geometry(obj)
            for _ in range(2):
                triggers._timer()
            require(
                "bake: frozen chain sends nothing on a scrub",
                POOL.requests(stage_a.key) == reports[stage_a.key]
                and POOL.requests(stage_b.key) == reports[stage_b.key],
                "fake request counts changed while frozen",
            )
            require(
                "bake: frame %d scale" % frame,
                abs(baked[frame][2] - expected) < 0.05,
                "max %r, expected %r" % (baked[frame][2], expected),
            )
    finally:
        rigs.scene.frame_set(original)
    require(
        "bake: three distinct per-frame outputs",
        len({baked[frame][1] for frame in (1, 2, 3)}) == 3,
        "%r" % {frame: baked[frame][1] for frame in (1, 2, 3)},
    )

    result = bpy.ops.stageloom.clear_bake(index=1)
    require("bake: clear finished", "FINISHED" in result, repr(result))
    require(
        "bake: clear released the chain",
        not stage_b.baked
        and not stage_b.frozen
        and not stage_a.frozen
        and stage_b.bake_token == ""
        and stage_a.frozen_by_bake == "",
        "baked=%r frozen=%r marker=%r"
        % (stage_b.baked, stage_b.frozen, stage_a.frozen_by_bake),
    )
    settle("bake clear resume")
    increment(OPERATIONS, "native_bake_cycles")
    increment(OPERATIONS, "clear_bake_cycles")


def reconnect_cycle(rigs):
    settle("reconnect pre-settle")
    witness = rigs.churn_stage
    old_rid = witness.rid
    old_hash = witness.applied_out_hash
    POOL.stop_all()
    require(
        "reconnect: clients stopped",
        pump(10.0, lambda: bridge.state["clients"] == 0, "clients stop"),
        "clients=%d" % bridge.state["clients"],
    )
    # A disconnect alone does not tag an object; the next scene event is what
    # notices it. Mark the stacks dirty the way an edit would, then require the
    # pass to land in the classified disconnected state.
    ENGINE.mark_dirty_all()
    require(
        "reconnect: stages become disconnected",
        pump(
            10.0,
            lambda: all(
                stage.state in ("disconnected", "unbound")
                for _obj, _record, stage in live_stages()
            ),
            "disconnected states",
        ),
        repr(stage_states()),
    )
    require(
        "reconnect: pending waits dropped",
        all(
            not ENGINE.is_pending(record.stack_id, stage.key)
            for _obj, record, stage in live_stages()
        ),
        repr(stage_states()),
    )

    live = live_stages()
    for _obj, _record, stage in live:
        POOL.start(stage.key, "--delay-ms", "3")
    wait_clients(len(live))
    settle("reconnect recovery")
    for _obj, _record, stage in live:
        require(
            "reconnect: every stage asked again",
            POOL.requests(stage.key) >= 1,
            "%s requests=%d" % (stage.key, POOL.requests(stage.key)),
        )
        require(
            "reconnect: every stage is ok",
            stage.state == "ok",
            "%s state=%s error=%s" % (stage.key, stage.state, stage.error),
        )
    # The reconnect's re-ask is deterministic and may reuse the old rid; force
    # a fresh nonce so the answer from the previous client is truly stale.
    ENGINE.request_again(rigs.churn, witness)
    settle("reconnect stale witness")
    stale_answer(
        rigs.churn, witness, old_rid, old_hash, "reconnect stale answer"
    )
    increment(OPERATIONS, "reconnects")
    recovery("reconnect", "stopped and restarted %d client(s)" % len(live))


def object_delete_cycle(cycle):
    """Add a whole stack object, delete it, and prove its session state died."""
    verts, quads, _actual = harness.grid_arrays(200)
    obj = harness.mesh_object("Soak Temp %04d" % cycle, verts, quads)
    stage = harness.add_stage(obj, "Soak Temp Stage %04d" % cycle)
    record = stack_mod.stack_of(obj)
    stack_id = record.stack_id
    key = stage.key
    group = nodes.group_name(key)
    base = helpers.helper_name(obj, key)
    POOL.start(key, "--delay-ms", "3")
    wait_clients(POOL.expected())
    settle("temporary object settle")

    # Seed the session state a live stage may carry, including a wait and a
    # report, so the deletion test covers every per-stage owner.
    ENGINE.runtime(stack_id, key).pending = {"sha256:soak-temp": time.monotonic()}
    ENGINE.verify.add((stack_id, key))
    ENGINE.playback_reports[key] = "soak temp report"
    ENGINE.dirty.add(obj.name)
    obj_name = obj.name
    params = [
        name for name in obj.keys() if name.startswith(stack_mod.stage_prefix(key))
    ]

    remove_object_data(obj)
    if not pump(
        10.0,
        lambda: bpy.data.objects.get(obj_name) is None
        and all(
            stage.key != key
            for _obj, _record, stage in live_stages()
        ),
        "temporary object removal",
    ):
        raise SoakInvariantError("the temporary object survived removal")
    require(
        "object delete: stack removed from the registry",
        stack_id not in {
            record.stack_id for _obj, record in live_stacks()
        },
        stack_id,
    )
    residual = [
        item.name
        for table in (
            bpy.data.objects,
            bpy.data.collections,
            bpy.data.meshes,
            bpy.data.curves,
            bpy.data.pointclouds,
        )
        for item in list(table)
        if item.name == base or item.name.startswith(base + ".")
    ]
    require(
        "object delete: helper plumbing swept",
        not residual,
        "%r" % residual,
    )
    require(
        "object delete: node group swept",
        bpy.data.node_groups.get(group) is None,
        group,
    )
    require(
        "object delete: session state pruned",
        ENGINE.peek_runtime(stack_id, key) is None
        and (stack_id, key) not in ENGINE.verify
        and key not in ENGINE.playback_reports
        and not any(
            stage_key == key for _sid, stage_key in ENGINE.prefix_memo
        ),
        "runtime=%r verify=%r reports=%r"
        % (
            ENGINE.peek_runtime(stack_id, key),
            (stack_id, key) in ENGINE.verify,
            key in ENGINE.playback_reports,
        ),
    )
    require(
        "object delete: parameter namespace removed with the object",
        bpy.data.objects.get(obj_name) is None and bool(params),
        "%r" % params,
    )
    POOL.stop(key)
    wait_clients(POOL.expected())
    increment(OPERATIONS, "object_delete_cycles")


def churn_cycle(cycle):
    """One add/request/rename/undo/freeze/disable/seed/delete cycle."""
    global _CURRENT_CYCLE
    _CURRENT_CYCLE = cycle
    obj = RIGS.churn
    stage = harness.add_stage(obj, "Soak B %04d" % cycle)
    key = stage.key
    increment(OPERATIONS, "stage_add")
    POOL.start(key, "--delay-ms", "3")
    wait_clients(POOL.expected())
    settle("new stage settle")

    request_full(obj, stage)
    increment(OPERATIONS, "request_full")
    request_ref(obj, stage, stack_mod.stack_of(obj))

    helper_before = helpers.helper_for(obj, key, "mesh")
    group_before = nodes.group_name(key)
    stage.label = "Soak B renamed %04d" % cycle
    ENGINE.mark_object_dirty(obj)
    settle("stage rename")
    increment(OPERATIONS, "stage_rename")
    require(
        "rename: stage keeps its helper and group",
        helpers.helper_for(obj, key, "mesh") is helper_before
        and bpy.data.node_groups.get(group_before) is not None,
        "helper or group moved with the label",
    )

    object_before = obj.name
    obj.name = "Soak Churn %04d" % cycle
    ENGINE.mark_object_dirty(obj)
    settle("object rename")
    increment(OPERATIONS, "object_rename")
    require(
        "rename: object keeps its helper set",
        helpers.helper_for(obj, key, "mesh") is helper_before
        and helper_before.name.startswith(
            "SL %s.%s" % (stack_mod.stack_of(obj).stack_id, key)
        ),
        "the helper changed with the object name",
    )
    require(
        "rename: no old-name plumbing",
        not any(
            item.name.startswith("SL %s." % object_before)
            for item in list(bpy.data.objects) + list(bpy.data.collections)
        ),
        "an old-name helper set was left behind",
    )

    old_rid = stage.rid
    old_hash = stage.applied_out_hash
    triggers._on_undo()
    triggers._on_redo()
    increment(OPERATIONS, "undo_handlers")
    increment(OPERATIONS, "redo_handlers")
    # A history step re-asks, but the deterministic request id can be the same
    # when the state did not change. Force a fresh nonce so the old answer is
    # genuinely superseded, which is the stale-answer case under test.
    ENGINE.request_again(obj, stage)
    settle("undo/redo handler")
    stale_answer(obj, stage, old_rid, old_hash, "undo stale answer")
    playback_cycle(RIGS)
    disabled_cycle(obj, stage)
    freeze_cycle(obj, stage)

    _collection, parts = helpers.ensure_definition_parts(obj, key, 0, 2)
    for part in parts:
        part["mesh"].data.from_pydata(
            [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)], [], [(0, 1, 2)]
        )
        part["mesh"].data.update()
    instance = helpers.ensure_instance_cloud(obj, key, 0)
    instance.data.resize(1)
    nested = helpers.ensure_nested_cloud(obj, key, 0)
    nested.data.resize(1)
    increment(OPERATIONS, "definition_seeds")
    check_engine_bookkeeping("seeded definition")

    params = [
        name for name in obj.keys() if name.startswith(stack_mod.stage_prefix(key))
    ]
    stack_id = stack_mod.stack_of(obj).stack_id
    group = nodes.group_name(key)
    modifier = nodes.modifier_of(obj, stage)
    require("delete: modifier exists", modifier is not None)
    obj.modifiers.remove(modifier)
    ENGINE.mark_object_dirty(obj)
    if not pump(
        15.0,
        lambda: len(stack_mod.stack_of(obj).stages) == 1,
        "stage removal",
    ):
        raise SoakInvariantError(
            "the stage survived its deleted modifier: %r"
            % (stage_states(),)
        )
    check_deleted_stage(obj, key, stack_id, params, group)
    POOL.stop(key)
    wait_clients(POOL.expected())
    increment(OPERATIONS, "stage_delete")

    if cycle % 5 == 0:
        object_delete_cycle(cycle)
    check_engine_bookkeeping("churn cycle %d" % cycle)


def run_soak(rigs, duration, quick):
    global _CYCLE_DEADLINE
    cycle = 0
    loop_started = time.monotonic()
    next_bake = loop_started + (4.0 if quick else 60.0)
    next_reconnect = loop_started + (6.0 if quick else 120.0)
    bake_interval = 4.0 if quick else 120.0
    reconnect_interval = 6.0 if quick else 240.0
    cycle_cap = 20.0 if quick else 60.0
    native_bake = "geometry_node_bake_single" in dir(bpy.ops.object)
    REPORT["bake"]["native_operator"] = native_bake
    if not native_bake:
        REPORT["bake"]["limitation"] = (
            "Blender did not expose object.geometry_node_bake_single; the soak "
            "ran the freeze-token orchestration instead and does not claim "
            "native bake data coverage"
        )
    while time.monotonic() - loop_started < duration:
        cycle += 1
        REPORT["cycles"] = cycle
        started = time.monotonic()
        _CYCLE_DEADLINE = started + cycle_cap
        try:
            POOL.check_alive()
            churn_cycle(cycle)
            now = time.monotonic()
            if now >= next_bake:
                if native_bake:
                    REPORT["bake"]["native_attempts"] += 1
                    native_bake_cycle(rigs)
                    REPORT["bake"]["native_successes"] += 1
                else:
                    REPORT["bake"]["marker_fallback_runs"] += 1
                    marker_fallback_cycle(rigs)
                next_bake = now + bake_interval
            now = time.monotonic()
            if now >= next_reconnect:
                reconnect_cycle(rigs)
                next_reconnect = now + reconnect_interval
            check_engine_bookkeeping("cycle %d end" % cycle)
            REPORT["loop_seconds"] = time.monotonic() - loop_started
        finally:
            elapsed = time.monotonic() - started
            REPORT["cycle_seconds_max"] = max(
                REPORT["cycle_seconds_max"], elapsed
            )
            _CYCLE_DEADLINE = None
        if elapsed > cycle_cap:
            raise SoakTimeout(
                "cycle %d took %.1f s, past the %.0f s per-cycle guard"
                % (cycle, elapsed, cycle_cap)
            )
        if time.monotonic() - loop_started > duration + cycle_cap:
            # The loop condition is sufficient; this is a readable guard for a
            # cycle that overran the requested duration without hitting a wait.
            raise SoakTimeout(
                "the churn loop overran its requested duration by more than "
                "%.0f s" % cycle_cap
            )
        current = rss_mb()
        if current is not None:
            REPORT["rss_mb_max_cycle"] = max(
                REPORT["rss_mb_max_cycle"] or 0.0, current
            )
        if cycle % 10 == 0 or cycle == 1:
            print(
                "[soak] cycle %d, %.0f s elapsed, %d stage(s), RSS %.1f MiB"
                % (
                    cycle,
                    time.monotonic() - loop_started,
                    len(live_stages()),
                    current or 0.0,
                ),
                flush=True,
            )
    REPORT["cycles"] = cycle
    REPORT["loop_seconds"] = time.monotonic() - loop_started


# -------------------------------------------------------------------- teardown


def teardown(rigs, report):
    POOL.stop_all()
    wait_clients(0)
    for obj, record in list(live_stacks()):
        remove_object_data(obj)
    lifecycle._rebuild_index()
    triggers._timer()
    if not pump(5.0, lambda: not ENGINE.stack_objects, "final teardown"):
        raise SoakInvariantError("the engine still has stack objects after teardown")
    snapshot = engine_snapshot()
    report["after_teardown"] = snapshot
    require(
        "teardown: registry empty",
        not ENGINE.stage_index
        and not ENGINE.stage_owners
        and not ENGINE.stage_claims
        and not ENGINE.stage_conflicts
        and not ENGINE.stack_objects,
        "%r" % snapshot,
    )
    require(
        "teardown: session state empty",
        not ENGINE._runtimes
        and not ENGINE.verify
        and not ENGINE.prefix_memo
        and not ENGINE.playback_reports
        and not ENGINE.dirty,
        "%r" % snapshot,
    )
    require(
        "teardown: no helper plumbing or groups",
        snapshot["helper_objects"] == 0
        and snapshot["definition_collections"] == 0
        and snapshot["instance_clouds"] == 0
        and snapshot["nested_clouds"] == 0
        and snapshot["orphan_plumbing"] == 0
        and snapshot["orphan_groups"] == 0
        and snapshot["node_groups"] == 0,
        "%r" % snapshot,
    )
    require(
        "teardown: engine dormant",
        not ENGINE.active
        and not bpy.app.timers.is_registered(triggers._timer),
        "active=%r timer=%r"
        % (
            ENGINE.active,
            bpy.app.timers.is_registered(triggers._timer),
        ),
    )


# ------------------------------------------------------------------------ main


def main():
    global _HARD_DEADLINE, REPORT, RIGS
    args = parse_args()
    duration = args.duration
    if duration is None:
        duration = 20 if args.quick else 3600
    if duration <= 0:
        print("soak: --duration must be positive", file=sys.stderr)
        return 2
    started = time.monotonic()
    _HARD_DEADLINE = started + duration + (180 if not args.quick else 120)
    REPORT = {
        "format": "stageloom-soak-report/1",
        "meta": {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "blender": bpy.app.version_string,
            "python": platform.python_version(),
            "os": platform.system(),
            "arch": platform.machine(),
            "platform": platform.platform(),
            "processor": harness.cpu_model(),
            "quick": bool(args.quick),
            "duration_requested": duration,
            "command": " ".join(sys.argv),
            "data_root": DATA_ROOT,
            "undo_operator": "probed after setup",
            "native_bake": "Blender Geometry Nodes bake (headless)",
            "native_windows_rhino": "deferred until after 0.8.0; not run",
        },
        "ok": False,
        "cycles": 0,
        "loop_seconds": 0.0,
        "operations": OPERATIONS,
        "recoveries": RECOVERIES,
        "recovery_samples": RECOVERY_SAMPLES,
        "checks": [],
        "failed_checks": [],
        "bake": {
            "native_operator": False,
            "native_attempts": 0,
            "native_successes": 0,
            "marker_fallback_runs": 0,
            "limitation": "",
        },
        "undo": {
            "operator_available": False,
            "operator_error": "",
            "engine_handler_cycles": 0,
            "global_undo_disabled": True,
            "limitation": (
                "bpy.ops.ed.undo()/redo() poll fails in background mode; the "
                "engine's undo/redo handlers (the code a real history step "
                "runs) are exercised instead"
            ),
        },
        "rss_mb_start": rss_mb(),
        "rss_mb_end": None,
        "rss_mb_peak": peak_rss_mb(),
        "rss_mb_max_cycle": None,
        "cycle_seconds_max": 0.0,
        "cycle_limit_seconds": 20.0 if args.quick else 60.0,
        "final": {},
        "after_teardown": {},
        "failures": [],
        "unclassified_exceptions": [],
    }
    try:
        RIGS = setup_rigs()
        undo_available, undo_error = probe_undo()
        REPORT["undo"]["operator_available"] = undo_available
        REPORT["undo"]["operator_error"] = undo_error or (
            "" if undo_available else "poll returned False in background mode"
        )
        REPORT["rss_mb_start"] = rss_mb()
        REPORT["rss_mb_peak"] = peak_rss_mb()
        REPORT["bake"]["native_operator"] = (
            "geometry_node_bake_single" in dir(bpy.ops.object)
        )
        check_engine_bookkeeping("setup")
        print(
            "[soak] Blender %s, %ds%s; native bake operator=%s"
            % (
                bpy.app.version_string,
                duration,
                " quick" if args.quick else "",
                REPORT["bake"]["native_operator"],
            ),
            flush=True,
        )
        run_soak(RIGS, duration, args.quick)
        settle("final settle")
        REPORT["final"] = engine_snapshot()
        check_engine_bookkeeping("final")
        REPORT["rss_mb_end"] = rss_mb()
        teardown(RIGS, REPORT)
        REPORT["rss_mb_peak"] = peak_rss_mb()
    except Exception as exc:  # noqa: BLE001 - the report records every failure
        REPORT["error"] = "%s: %s" % (type(exc).__name__, exc)
        REPORT["traceback"] = traceback.format_exc()
        REPORT["failed_at_cycle"] = _CURRENT_CYCLE
        if isinstance(exc, (SoakInvariantError, SoakTimeout)):
            REPORT["failures"].append(
                {"type": type(exc).__name__, "message": str(exc)}
            )
        else:
            REPORT["unclassified_exceptions"].append(
                {"type": type(exc).__name__, "message": str(exc)}
            )
        if RIGS is not None:
            try:
                POOL.stop_all()
            except Exception:  # noqa: BLE001 - cleanup must not hide the failure
                pass
            try:
                REPORT["final"] = engine_snapshot()
            except Exception:  # noqa: BLE001 - best-effort state for the report
                pass
        REPORT["rss_mb_peak"] = peak_rss_mb()
    finally:
        try:
            harness.stop_addon()
        except Exception as exc:  # noqa: BLE001 - cleanup must not hide the result
            REPORT.setdefault("cleanup_errors", []).append(
                "%s: %s" % (type(exc).__name__, exc)
            )

    REPORT["wall_seconds"] = time.monotonic() - started
    REPORT["undo"]["engine_handler_cycles"] = int(
        OPERATIONS.get("undo_handlers", 0)
    )
    REPORT["operations"] = OPERATIONS
    REPORT["recoveries"] = RECOVERIES
    REPORT["recovery_samples"] = RECOVERY_SAMPLES
    REPORT["checks"] = sorted(CHECKS.values(), key=lambda item: item["name"])
    REPORT["failed_checks"] = [
        item["name"] for item in REPORT["checks"] if item["failures"]
    ]
    REPORT["rss_mb_end"] = REPORT["rss_mb_end"] if REPORT["rss_mb_end"] is not None else rss_mb()
    REPORT["rss_mb_peak"] = peak_rss_mb()
    REPORT["ok"] = (
        not REPORT.get("error")
        and not REPORT["failed_checks"]
        and not REPORT["unclassified_exceptions"]
        and REPORT["cycles"] > 0
        and REPORT["wall_seconds"] <= duration + (180 if not args.quick else 120)
    )

    print("[soak] invariants:")
    for item in REPORT["checks"]:
        state = "ok" if not item["failures"] else "FAIL"
        print(
            "  %-4s %-52s %6d run(s)%s"
            % (
                state,
                item["name"],
                item["runs"],
                " - " + item["last_detail"] if item["failures"] else "",
            )
        )
    if args.out:
        try:
            with open(args.out, "w") as handle:
                json.dump(REPORT, handle, indent=2)
            print("[soak] wrote %s" % args.out)
        except OSError as exc:
            print("[soak] cannot write %s: %s" % (args.out, exc), file=sys.stderr)
            REPORT["ok"] = False
    if REPORT["ok"]:
        print(
            "[soak] PASS %d cycles in %.0f s (%.0f s churn, peak RSS %s MiB)"
            % (
                REPORT["cycles"],
                REPORT["wall_seconds"],
                REPORT["loop_seconds"],
                "n/a"
                if REPORT["rss_mb_peak"] is None
                else "%.1f" % REPORT["rss_mb_peak"],
            )
        )
        return 0
    print(
        "[soak] FAIL: %s"
        % (
            REPORT.get("error")
            or "invariant(s): %s" % ", ".join(REPORT["failed_checks"])
            or "no complete cycle",
        ),
        file=sys.stderr,
    )
    return 1


RIGS = None

if __name__ == "__main__":
    raise SystemExit(main())
