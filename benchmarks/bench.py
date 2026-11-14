"""Reference benchmark matrix for Stageloom's Blender side.

    scripts/bench                     # all scenarios, budget gate on
    scripts/bench --scenario pipeline # one scenario
    scripts/bench --no-budget         # measure only

The harness runs under ``blender -b`` (see ``scripts/bench``). It drives the
real engine and, when a scenario needs the wire, the real WebSocket transport
against ``tools/fake_gh``. It is deliberately separate from the unit suite:
these runs take minutes, and their budgets are about latency, not correctness.

Scenarios
---------
``pipeline``
    A plain quad grid at 10k, 100k and 1M triangles: read, hash, encode, decode
    and apply, with a fresh mesh and then with an in-place position update.
``instances``
    One 100k-triangle definition placed 13 times through Instance-on-Points:
    prefix read, first apply and repeated apply (the row data moves, the node
    group must not be rebuilt).
``roundtrip``
    A 100k-triangle stage through the socket and fake_gh: full request and a
    parameter-only (ref) request, with the engine's own prefix / encode / rtt /
    apply timings.
``multistage``
    A three-stage chain at 10k triangles: one dirty pass, the full chain settle
    and the idle pass, all against three fake clients.
``playback``
    A live stage over 30 frames: per-frame send-to-apply latency, one request
    per frame, no cache replay.

The reported numbers are engine pass times and protocol work. They do not
include Grasshopper's own solve (a live Rhino run measures that); the fake's
work is included in ``rtt_ms`` and labelled ``fake-gh`` in the output.
"""

import argparse
import json
import math
import os
import platform
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import bpy
import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
VENDOR = REPO / "blender" / "stageloom" / "vendor"
BLENDER_PKG = REPO / "blender"

for path in (str(REPO), str(BLENDER_PKG), str(VENDOR)):
    if path not in sys.path:
        sys.path.insert(0, path)

import stageloom  # noqa: E402
from stageloom.blender import (  # noqa: E402
    bridge,
    engine,
    geometry_io,
    nodes,
    prefs,
    stack as stack_mod,
)
from stageloom.core import codec, hashing  # noqa: E402

DATA_ROOT = tempfile.mkdtemp(prefix="stageloom-bench-")
_FAKES = []


# --------------------------------------------------------------------- setup


def parse_args():
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    else:
        argv = []
    parser = argparse.ArgumentParser(prog="scripts/bench")
    parser.add_argument(
        "--scenario",
        action="append",
        choices=("pipeline", "instances", "roundtrip", "multistage", "playback"),
        help="run only this scenario (repeatable); default is all",
    )
    parser.add_argument("--out", default="", help="write the JSON report here")
    parser.add_argument("--repeats", type=int, default=3, help="timed repeats per measurement")
    parser.add_argument("--no-budget", action="store_true", help="measure without failing on a budget")
    parser.add_argument("--quick", action="store_true", help="skip the 1M-triangle case")
    return parser.parse_args(argv)


def start_addon():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    prefs.DEFAULT_DATA_ROOT = DATA_ROOT
    stageloom.register()
    # Benchmarks want numbers, not a stream of [stageloom] diagnostics.
    prefs._log_info = False


def stop_addon():
    for fake in _FAKES:
        stop_fake(fake)
    _FAKES.clear()
    try:
        engine.server.SERVER.stop()
    except Exception:
        pass
    try:
        stageloom.unregister()
    except Exception:
        pass


def reset_scene():
    """A clean file between scenarios, with the engine's transient state gone."""
    engine._dirty.clear()
    engine._force_read.clear()
    engine._pending.clear()
    engine._sent.clear()
    engine._verify.clear()
    engine._pull_permits.clear()
    engine._timings.clear()
    bpy.ops.wm.read_factory_settings(use_empty=True)


# ----------------------------------------------------------------- utilities


def cpu_model():
    """The processor name Linux exposes, for a comparable result note."""
    try:
        with open("/proc/cpuinfo", "r") as handle:
            for line in handle:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or platform.machine()


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


def timed(fn, repeats=3, warmup=True):
    """Run ``fn`` and return its last result plus min/median/max milliseconds."""
    if warmup:
        fn()
    samples = []
    result = None
    for _ in range(max(1, repeats)):
        started = time.perf_counter()
        result = fn()
        samples.append((time.perf_counter() - started) * 1000.0)
    return result, {
        "min": min(samples),
        "median": statistics.median(samples),
        "max": max(samples),
        "samples": samples,
    }


def grid_arrays(triangles, size=10.0):
    """A square quad grid of about ``triangles`` triangles (2 per quad)."""
    n = max(2, int(math.sqrt(max(1, triangles) / 2.0)) + 1)
    xs = np.linspace(0.0, size, n, dtype=np.float32)
    ys = np.linspace(0.0, size, n, dtype=np.float32)
    gx, gy = np.meshgrid(xs, ys, indexing="ij")
    verts = np.stack(
        [gx.reshape(-1), gy.reshape(-1), np.zeros(n * n, dtype=np.float32)], axis=1
    )
    i = np.arange(n - 1, dtype=np.int64)[:, None]
    j = np.arange(n - 1, dtype=np.int64)[None, :]
    a = (i * n + j).reshape(-1)
    quads = np.stack([a, a + n, a + n + 1, a + 1], axis=1).astype(np.int32)
    return verts, quads, (n - 1) * (n - 1) * 2


def mesh_object(name, verts, quads):
    """A mesh built with foreach_set: fast enough for the 1M-triangle case."""
    mesh = bpy.data.meshes.new(name)
    mesh.vertices.add(len(verts))
    mesh.vertices.foreach_set("co", np.ascontiguousarray(verts, dtype=np.float32).reshape(-1))
    mesh.loops.add(quads.size)
    mesh.loops.foreach_set(
        "vertex_index", np.ascontiguousarray(quads, dtype=np.int32).reshape(-1)
    )
    mesh.polygons.add(len(quads))
    mesh.polygons.foreach_set(
        "loop_start", np.arange(0, quads.size, 4, dtype=np.int32)
    )
    mesh.update(calc_edges=True)
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def point_cloud_object(name, positions):
    cloud = bpy.data.pointclouds.new(name)
    cloud.resize(len(positions))
    cloud.points.foreach_set(
        "co", np.ascontiguousarray(positions, dtype=np.float32).reshape(-1)
    )
    cloud.points.update()
    obj = bpy.data.objects.new(name, cloud)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def instance_holder(name, source, points):
    """An object whose GN modifier instances ``source`` at the ``points`` cloud."""
    group = bpy.data.node_groups.new(name, "GeometryNodeTree")
    group.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    output = group.nodes.new("NodeGroupOutput")
    instance = group.nodes.new("GeometryNodeInstanceOnPoints")
    source_info = group.nodes.new("GeometryNodeObjectInfo")
    source_info.inputs["Object"].default_value = source
    source_info.transform_space = "ORIGINAL"
    points_info = group.nodes.new("GeometryNodeObjectInfo")
    points_info.inputs["Object"].default_value = points
    points_info.transform_space = "ORIGINAL"
    group.links.new(source_info.outputs["Geometry"], instance.inputs["Instance"])
    group.links.new(points_info.outputs["Geometry"], instance.inputs["Points"])
    group.links.new(instance.outputs["Instances"], output.inputs["Geometry"])
    # A mesh object, not an Empty: add_stage accepts mesh/curve/point-cloud
    # stacks, and the GN modifier replaces the empty base mesh with instances.
    holder = bpy.data.objects.new(name, bpy.data.meshes.new(name + "-base"))
    bpy.context.scene.collection.objects.link(holder)
    holder.modifiers.new("Instances", "NODES").node_group = group
    return holder


def add_stage(obj, label):
    for other in bpy.context.selected_objects:
        other.select_set(False)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.stageloom.add_stage(label=label)
    return obj.gh_stack.stages[-1]


def sections_dict(sections):
    return {name: values for name, dtype, values in sections}


def sections_hash(sections):
    raw = {name: codec.section_bytes(name, dtype, values) for name, dtype, values in sections}
    return hashing.geometry_hash(raw)


# --------------------------------------------------------------- fake client


def free_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def start_fake(stage_key, port, report, *extra):
    env = dict(
        os.environ,
        PYTHONPATH=os.pathsep.join([str(VENDOR), str(REPO)]),
    )
    command = [
        sys.executable,
        str(REPO / "tools" / "fake_gh" / "fake_gh.py"),
        "--port",
        str(port),
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
    _FAKES.append(process)
    return process


def stop_fake(process):
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


def read_report(path):
    """The fake client's counters, or {} while it has not written them yet."""
    try:
        with open(path) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def pump(seconds, until):
    """Drive the engine timer until ``until()`` or the deadline."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        engine._timer()
        if until():
            return True
        time.sleep(0.002)
    return bool(until())


def pending_of(record, stage):
    return engine._pending.get((record.stack_id, stage.key)) or {}


def settled(record):
    """Every stage has applied its answer and nothing is still in flight."""
    for stage in record.stages:
        if stage.state != stack_mod.STATE_OK:
            return False
        if pending_of(record, stage):
            return False
    return True


def wait_for_stages(record, seconds=20.0):
    return pump(seconds, lambda: settled(record))


def wait_for_clients(count, seconds=15.0):
    return pump(seconds, lambda: bridge.state["clients"] >= count)


def request_full(obj, stage):
    """Ask for a fresh full request: a new nonce and no remembered client geometry."""
    engine._sent.pop((stack_mod.stack_of(obj).stack_id, stage.key), None)
    engine.request_again(obj, stage)


# ------------------------------------------------------------------ scenarios


def scenario_pipeline(args, report):
    reset_scene()
    rows = []
    for triangles in (10_000, 100_000, 1_000_000):
        if args.quick and triangles >= 1_000_000:
            continue
        repeats = 1 if triangles >= 1_000_000 else max(2, args.repeats)
        warmup = triangles < 1_000_000
        before = rss_mb()
        verts, quads, actual = grid_arrays(triangles)
        obj = mesh_object("pipeline-%d" % triangles, verts, quads)
        mesh = obj.data

        def extract():
            return geometry_io.mesh_to_sections(mesh)

        sections, extract_stats = timed(extract, repeats, warmup=warmup)
        _, hash_stats = timed(lambda: sections_hash(sections), repeats, warmup=warmup)
        frame, encode_stats = timed(
            lambda: codec.encode_frame({"type": "request", "stage": "bench"}, sections),
            repeats,
            warmup=warmup,
        )
        _, decode_stats = timed(lambda: codec.decode_frame(frame), repeats, warmup=warmup)

        def apply_fresh():
            target = bpy.data.meshes.new("apply-target")
            try:
                geometry_io.sections_to_mesh(target, sections)
            finally:
                bpy.data.meshes.remove(target)

        _, apply_fresh_stats = timed(apply_fresh, repeats, warmup=warmup)

        target = bpy.data.meshes.new("apply-in-place")
        geometry_io.sections_to_mesh(target, sections)
        by_name = sections_dict(sections)
        verts_array = np.array(by_name["mesh.verts"], copy=True)
        moved = [("mesh.verts", "f32", verts_array)]
        moved.extend(
            (name, dtype, values)
            for name, dtype, values in sections
            if name != "mesh.verts"
        )
        _, apply_inplace_stats = timed(
            lambda: geometry_io.sections_to_mesh(target, moved), repeats, warmup=warmup
        )
        bpy.data.meshes.remove(target)

        after = rss_mb()
        row = {
            "triangles": actual,
            "requested_triangles": triangles,
            "vertices": int(len(verts)),
            "frame_mb": len(frame) / 1e6,
            "extract_ms": extract_stats,
            "hash_ms": hash_stats,
            "encode_ms": encode_stats,
            "decode_ms": decode_stats,
            "apply_fresh_ms": apply_fresh_stats,
            "apply_inplace_ms": apply_inplace_stats,
            "rss_mb_before": before,
            "rss_mb_after": after,
        }
        rows.append(row)
        print(
            "[pipeline] %7d tris: read %.1f ms, hash %.1f ms, encode %.1f ms, "
            "decode %.1f ms, apply(fresh) %.1f ms, apply(in place) %.1f ms, frame %.1f MB"
            % (
                actual,
                extract_stats["median"],
                hash_stats["median"],
                encode_stats["median"],
                decode_stats["median"],
                apply_fresh_stats["median"],
                apply_inplace_stats["median"],
                row["frame_mb"],
            )
        )
        # The arrays are large at 1M; drop them before the next size. Assigning
        # None releases them without pyflakes treating the names as unbound
        # for the closures that used them above.
        sections = frame = by_name = verts_array = moved = None
        bpy.data.objects.remove(obj, do_unlink=True)
        verts = quads = None
    report["pipeline"] = rows


def scenario_instances(args, report):
    reset_scene()
    verts, quads, actual = grid_arrays(100_000)
    source = mesh_object("instance-source", verts, quads)
    positions = np.array(
        [[float(i % 13) * 5.0, 0.0, float(i // 13) * 5.0] for i in range(13)],
        dtype=np.float32,
    )
    points = point_cloud_object("instance-points", positions)
    holder = instance_holder("instance-holder", source, points)
    stage = add_stage(holder, "Instances")
    record = stack_mod.stack_of(holder)

    def cold_prefix():
        engine._forget_prefix(holder, stage.key)
        engine._prefix_memo.pop((holder.as_pointer(), stage.key), None)
        modifier = nodes.modifier_of(holder, stage)
        index = engine._modifier_index(holder, modifier)
        return engine._clone_prefix(holder, index, bool(record.subd), stage.key)

    (sections, attributes, definitions), prefix_stats = timed(
        cold_prefix, max(2, args.repeats)
    )
    by_name = sections_dict(sections)
    frame, encode_stats = timed(
        lambda: codec.encode_frame(
            {
                "type": "request",
                "stage": stage.key,
                "definitions": definitions,
                "attributes": attributes,
            },
            sections,
        ),
        max(2, args.repeats),
    )
    rows = len(by_name.get("inst.ref_def", ()))
    group = bpy.data.node_groups[nodes.group_name(stage.key)]
    group_id = group.as_pointer()

    def apply_with(offset):
        payload = {}
        for name, dtype, values in sections:
            if name == "inst.transforms":
                matrices = np.array(values, dtype=np.float32, copy=True).reshape(-1, 16)
                matrices[:, 3] += offset
                payload[name] = matrices.reshape(-1)
            else:
                payload[name] = values
        engine._apply(holder, stage, payload, attributes)

    _, apply_first_stats = timed(lambda: apply_with(0.0), 1, warmup=False)
    _, apply_repeat_stats = timed(
        lambda: apply_with(1.0), max(2, args.repeats), warmup=True
    )
    rebuilt = bpy.data.node_groups[nodes.group_name(stage.key)].as_pointer() != group_id
    row = {
        "triangles_in_definition": actual,
        "instances": rows,
        "definitions": len(definitions),
        "frame_mb": len(frame) / 1e6,
        "prefix_ms": prefix_stats,
        "encode_ms": encode_stats,
        "apply_first_ms": apply_first_stats,
        "apply_repeat_ms": apply_repeat_stats,
        "group_reused": not rebuilt,
    }
    report["instances"] = row
    print(
        "[instances] %d x %d tris, %d definition(s): prefix %.1f ms, encode %.1f ms, "
        "apply first %.1f ms, apply repeat %.1f ms, group reused %s"
        % (
            rows,
            actual,
            len(definitions),
            prefix_stats["median"],
            encode_stats["median"],
            apply_first_stats["median"],
            apply_repeat_stats["median"],
            not rebuilt,
        )
    )
    bpy.data.objects.remove(holder, do_unlink=True)


def scenario_roundtrip(args, report):
    reset_scene()
    verts, quads, actual = grid_arrays(100_000)
    obj = mesh_object("roundtrip", verts, quads)
    stage = add_stage(obj, "Roundtrip")
    record = stack_mod.stack_of(obj)
    port = free_port()
    report_path = os.path.join(DATA_ROOT, "roundtrip-report.json")
    if not engine.start_server(port):
        raise RuntimeError("server did not start: %s" % bridge.state.get("notice", ""))
    start_fake(stage.key, port, report_path)
    if not wait_for_clients(1, 15.0):
        raise RuntimeError("fake-gh did not connect")
    pump(10.0, lambda: len(stage.params) > 0)

    full_samples = []
    ref_samples = []
    for attempt in range(max(1, args.repeats)):
        request_full(obj, stage)
        started = time.monotonic()
        if not wait_for_stages(record, 30.0):
            raise RuntimeError("round trip did not settle: %s" % stage.state)
        elapsed = (time.monotonic() - started) * 1000.0
        timings = engine.stage_timings(record.stack_id, stage.key)
        full_samples.append(
            {
                "settle_ms": elapsed,
                "prefix_ms": timings.get("prefix_ms"),
                "encode_ms": timings.get("encode_ms"),
                "rtt_ms": timings.get("rtt_ms"),
                "apply_ms": timings.get("apply_ms"),
            }
        )
        if attempt == 0:
            # A parameter-only change is a ref request: no geometry sections.
            entry = stage.params[0]
            prop = stack_mod.param_prop_name(stage.key, entry.param_id)
            obj[prop] = float(obj.get(prop, 1.0)) + 0.1
            engine.request_again(obj, stage)
            started = time.monotonic()
            if not wait_for_stages(record, 30.0):
                raise RuntimeError("ref round trip did not settle: %s" % stage.state)
            elapsed = (time.monotonic() - started) * 1000.0
            timings = engine.stage_timings(record.stack_id, stage.key)
            ref_samples.append(
                {
                    "settle_ms": elapsed,
                    "prefix_ms": timings.get("prefix_ms"),
                    "encode_ms": timings.get("encode_ms"),
                    "rtt_ms": timings.get("rtt_ms"),
                    "apply_ms": timings.get("apply_ms"),
                }
            )

    def summary(samples, field):
        values = [sample[field] for sample in samples if sample.get(field) is not None]
        return statistics.median(values) if values else None

    row = {
        "triangles": actual,
        "full": {
            "count": len(full_samples),
            "settle_ms": summary(full_samples, "settle_ms"),
            "prefix_ms": summary(full_samples, "prefix_ms"),
            "encode_ms": summary(full_samples, "encode_ms"),
            "rtt_ms": summary(full_samples, "rtt_ms"),
            "apply_ms": summary(full_samples, "apply_ms"),
            "samples": full_samples,
        },
        "ref": {
            "count": len(ref_samples),
            "settle_ms": summary(ref_samples, "settle_ms"),
            "prefix_ms": summary(ref_samples, "prefix_ms"),
            "encode_ms": summary(ref_samples, "encode_ms"),
            "rtt_ms": summary(ref_samples, "rtt_ms"),
            "apply_ms": summary(ref_samples, "apply_ms"),
            "samples": ref_samples,
        },
    }
    # The fake's report may not be flushed yet; it rewrites on every response.
    time.sleep(0.05)
    fake_report = read_report(report_path)
    row["fake"] = {
        "full": fake_report.get("full"),
        "ref": fake_report.get("ref"),
        "responses": fake_report.get("responses"),
        "received_verts": fake_report.get("received_verts"),
    }
    report["roundtrip"] = row
    print(
        "[roundtrip] %d tris: full settle %.1f ms (prefix %.1f, encode %.1f, "
        "rtt %.1f, apply %.1f), ref settle %.1f ms"
        % (
            actual,
            row["full"]["settle_ms"],
            row["full"]["prefix_ms"] or 0.0,
            row["full"]["encode_ms"] or 0.0,
            row["full"]["rtt_ms"] or 0.0,
            row["full"]["apply_ms"] or 0.0,
            row["ref"]["settle_ms"] or 0.0,
        )
    )


def scenario_multistage(args, report):
    reset_scene()
    verts, quads, actual = grid_arrays(10_000)
    obj = mesh_object("multistage", verts, quads)
    stages = [add_stage(obj, "Chain %d" % (index + 1)) for index in range(3)]
    record = stack_mod.stack_of(obj)
    port = free_port()
    if not engine.start_server(port):
        raise RuntimeError("server did not start: %s" % bridge.state.get("notice", ""))
    for index, stage in enumerate(stages):
        path = os.path.join(DATA_ROOT, "multistage-%d.json" % index)
        start_fake(stage.key, port, path)
    if not wait_for_clients(len(stages), 20.0):
        raise RuntimeError("fake-gh clients did not connect: %s" % bridge.state["clients"])
    pump(10.0, lambda: all(len(stage.params) > 0 for stage in stages))

    dirty_samples = []
    settle_samples = []
    for _ in range(max(2, args.repeats)):
        arr = np.empty(len(obj.data.vertices) * 3, dtype=np.float32)
        obj.data.vertices.foreach_get("co", arr)
        arr[0] += 1.0
        obj.data.vertices.foreach_set("co", arr)
        obj.update_tag()
        bpy.context.view_layer.update()
        engine._mark_dirty_all(force=True)
        started = time.monotonic()
        engine._timer()
        dirty_samples.append((time.monotonic() - started) * 1000.0)
        started = time.monotonic()
        if not wait_for_stages(record, 30.0):
            raise RuntimeError("multi-stage chain did not settle: %s" % [s.state for s in stages])
        settle_samples.append((time.monotonic() - started) * 1000.0)

    # The idle pass: nothing dirty, no request in flight, the timer still ticks.
    engine._dirty.clear()
    engine._force_read.clear()
    idle_samples = []
    for _ in range(20):
        started = time.monotonic()
        engine._timer()
        idle_samples.append((time.monotonic() - started) * 1000.0)

    row = {
        "triangles": actual,
        "stages": len(stages),
        "dirty_pass_ms": {
            "median": statistics.median(dirty_samples),
            "max": max(dirty_samples),
            "samples": dirty_samples,
        },
        "settle_ms": {
            "median": statistics.median(settle_samples),
            "max": max(settle_samples),
            "samples": settle_samples,
        },
        "idle_pass_ms": {
            "median": statistics.median(idle_samples),
            "max": max(idle_samples),
            "samples": idle_samples,
        },
        "stage_timings": {
            stage.key: engine.stage_timings(record.stack_id, stage.key)
            for stage in stages
        },
    }
    report["multistage"] = row
    print(
        "[multistage] %d stages: dirty pass %.1f ms (max %.1f), chain settle %.1f ms, "
        "idle pass %.3f ms"
        % (
            len(stages),
            row["dirty_pass_ms"]["median"],
            row["dirty_pass_ms"]["max"],
            row["settle_ms"]["median"],
            row["idle_pass_ms"]["median"],
        )
    )


def scenario_playback(args, report):
    reset_scene()
    verts, quads, actual = grid_arrays(10_000)
    obj = mesh_object("playback", verts, quads)
    stage = add_stage(obj, "Playback")
    stage.live = True
    record = stack_mod.stack_of(obj)
    port = free_port()
    report_path = os.path.join(DATA_ROOT, "playback-report.json")
    if not engine.start_server(port):
        raise RuntimeError("server did not start: %s" % bridge.state.get("notice", ""))
    # A stateful solver: each answer is a different output for the same inputs.
    start_fake(stage.key, port, report_path, "--stateful")
    if not wait_for_clients(1, 15.0):
        raise RuntimeError("fake-gh did not connect")
    pump(10.0, lambda: len(stage.params) > 0)
    # Park the playhead before frame 1 and let the setup request settle there:
    # the connect-time answer is setup, and frame 1 must be a real step.
    bpy.context.scene.frame_set(bpy.context.scene.frame_start - 1)
    engine._mark_dirty_all(force=True)
    if not wait_for_stages(record, 30.0):
        raise RuntimeError("playback setup did not settle: %s" % stage.state)
    time.sleep(0.05)
    responses_before = read_report(report_path).get("responses") or 0
    applies_before = engine.stats["applies"]

    frames = 30
    per_frame = []
    for frame in range(1, frames + 1):
        bpy.context.scene.frame_set(frame)
        engine._mark_dirty_all(force=True)
        started = time.monotonic()
        if not wait_for_stages(record, 30.0):
            raise RuntimeError("playback frame did not settle: %s" % stage.state)
        per_frame.append((time.monotonic() - started) * 1000.0)

    time.sleep(0.05)
    fake_report = read_report(report_path)
    responses = (fake_report.get("responses") or 0) - responses_before
    applies = engine.stats["applies"] - applies_before
    row = {
        "triangles": actual,
        "frames": frames,
        "per_frame_ms": {
            "median": statistics.median(per_frame),
            "p95": sorted(per_frame)[max(0, int(len(per_frame) * 0.95) - 1)],
            "max": max(per_frame),
            "samples": per_frame,
        },
        "responses": responses,
        "echoes": fake_report.get("echoes"),
        "applies": applies,
        "stage_timings": engine.stage_timings(record.stack_id, stage.key),
    }
    report["playback"] = row
    print(
        "[playback] %d frames: median %.1f ms, p95 %.1f ms, max %.1f ms, "
        "%s responses, %s applies"
        % (
            frames,
            row["per_frame_ms"]["median"],
            row["per_frame_ms"]["p95"],
            row["per_frame_ms"]["max"],
            row["responses"],
            applies,
        )
    )


# -------------------------------------------------------------------- budgets


def check(report, checks):
    pipeline = {row["requested_triangles"]: row for row in report.get("pipeline", [])}
    row = pipeline.get(100_000)
    if row is not None:
        total = (
            row["extract_ms"]["median"]
            + row["hash_ms"]["median"]
            + row["encode_ms"]["median"]
            + row["decode_ms"]["median"]
            + row["apply_inplace_ms"]["median"]
        )
        checks.append(
            {
                "name": "100k triangle Blender round trip",
                "value": total,
                "unit": "ms",
                "budget": 50.0,
                "ok": total <= 50.0,
            }
        )
    instances = report.get("instances")
    if instances is not None:
        total = instances["prefix_ms"]["median"] + instances["encode_ms"]["median"]
        checks.append(
            {
                "name": "13x instance prefix read + encode",
                "value": total,
                "unit": "ms",
                "budget": 100.0,
                "ok": total <= 100.0,
            }
        )
        checks.append(
            {
                "name": "13x instance apply group reuse",
                "value": 1.0 if instances["group_reused"] else 0.0,
                "unit": "bool",
                "budget": 1.0,
                "ok": bool(instances["group_reused"]),
            }
        )
    multi = report.get("multistage")
    if multi is not None:
        checks.append(
            {
                "name": "multi-stage dirty pass",
                "value": multi["dirty_pass_ms"]["median"],
                "unit": "ms",
                "budget": 100.0,
                "ok": multi["dirty_pass_ms"]["median"] <= 100.0,
            }
        )
        checks.append(
            {
                "name": "idle engine pass",
                "value": multi["idle_pass_ms"]["median"],
                "unit": "ms",
                "budget": 2.0,
                "ok": multi["idle_pass_ms"]["median"] <= 2.0,
            }
        )
    playback = report.get("playback")
    if playback is not None:
        # No plan budget yet: record it, and fail only on a gross regression.
        checks.append(
            {
                "name": "live playback frame",
                "value": playback["per_frame_ms"]["median"],
                "unit": "ms",
                "budget": 250.0,
                "ok": playback["per_frame_ms"]["median"] <= 250.0,
            }
        )
        checks.append(
            {
                "name": "live playback one response per frame",
                "value": playback["responses"],
                "unit": "frames",
                "budget": playback["frames"],
                "ok": playback["responses"] == playback["frames"],
            }
        )
        checks.append(
            {
                "name": "live playback applies one state per frame",
                "value": playback["applies"],
                "unit": "frames",
                "budget": playback["frames"],
                "ok": playback["applies"] == playback["frames"],
            }
        )


# ----------------------------------------------------------------------- main


def main():
    args = parse_args()
    scenarios = args.scenario or ["pipeline", "instances", "roundtrip", "multistage", "playback"]
    report = {
        "meta": {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "blender": bpy.app.version_string,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "processor": cpu_model(),
            "data_root": DATA_ROOT,
            "args": vars(args),
        },
        "scenarios": [],
    }
    print("[bench] Blender %s on %s" % (bpy.app.version_string, platform.platform()))
    start_addon()
    try:
        for name in scenarios:
            report["scenarios"].append(name)
            globals()["scenario_" + name](args, report)
    finally:
        stop_addon()

    checks = []
    check(report, checks)
    report["checks"] = checks
    failed = [item for item in checks if not item["ok"]]

    print("[bench] budgets:")
    for item in checks:
        state = "ok" if item["ok"] else "OVER"
        print(
            "  %-38s %9.2f %-4s (budget %.2f) %s"
            % (item["name"], item["value"], item["unit"], item["budget"], state)
        )
    if args.out:
        with open(args.out, "w") as handle:
            json.dump(report, handle, indent=2)
        print("[bench] wrote %s" % args.out)
    if failed and not args.no_budget:
        print("[bench] %d budget(s) over" % len(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
