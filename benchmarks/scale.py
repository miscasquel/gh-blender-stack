"""Scale verification for Stageloom's three 0.8.0 shapes.

    scripts/scale
    scripts/scale --quick
    scripts/scale --out benchmarks/results/2026-11-19-scale.json

The runner builds and measures the T10c scale shapes under ``blender -b
--factory-startup --online-mode`` from the working tree, exactly like
``benchmarks/bench.py``:

``stages``
    One mesh object carrying **200 stage records** (200 stages are one stack,
    not 200 objects). Builds every stage through the real operator, then forces
    a full stage-index rebuild, reads the ordered stage list, draws the real
    N-panel against a recording layout and runs one no-client engine pass.
    Structural checks pin the record order, unique keys, 200 index entries and
    that the panel/engine pass read the index without rebuilding it.
``triangles``
    One mesh of exactly **100,000 triangles** (250 x 200 quad grid = 50,000
    quads), read to sections, applied fresh and then applied again in place
    with the same topology token. The in-place call is made with
    ``_topology_matches`` patched to raise, so a topology re-read fails the run.
``instances``
    One small mesh definition placed on a **10,000-point cloud** through
    Instance on Points: prefix read, first apply, repeated apply with the same
    topology token and the evaluated instance count. Checks pin 10,000 row
    records, one definition, 10,000 top-level instances and one reference.

Timing is recorded, not gated: this runner adds no timing threshold. The 100k
in-place apply is already covered by ``scripts/bench``'s
``pipeline.100k.apply_inplace_ms`` metric and its 4 ms budget; the 13x instance
apply is covered by ``instances.13x.apply_repeat_ms``. The 200-stage index,
panel and no-client pass are not in the default benchmark matrix, so their
times are diagnostic evidence rather than a new gate. A structural failure, an
exception, or the wall-clock timeout exits non-zero. ``--quick`` uses 20 stages,
10,000 triangles and 100 instances for a fast local smoke; it is not the scale
claim.

Peak RSS is read from ``/proc/self/status`` where available. The full run is a
local/release check; it is deliberately not part of ``scripts/check-all``.
"""

import argparse
import contextlib
import json
import math
import os
import platform
import sys
import time
import traceback
import types
from pathlib import Path
from unittest import mock

import bpy
import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
WHEEL = REPO / "blender" / "stageloom" / "wheels" / "websockets-17.2-py3-none-any.whl"
BLENDER_PKG = REPO / "blender"

for path in (str(HERE), str(REPO), str(BLENDER_PKG), str(WHEEL)):
    if path not in sys.path:
        sys.path.insert(0, path)

import bench as harness  # noqa: E402
from stageloom.blender import (  # noqa: E402
    geometry_io,
    helpers,
    nodes,
    stack as stack_mod,
    ui,
)
from stageloom.blender.engine import ENGINE  # noqa: E402
from stageloom.blender.engine import (  # noqa: E402
    apply as apply_mod,
    lifecycle,
    request as request_mod,
)
from stageloom.blender.geometry_io import mesh as geometry_mesh  # noqa: E402

TOKEN = "sha256:" + "0" * 64
CHECKS = []
_DEADLINE = None


class ScaleError(RuntimeError):
    """A structural scale assertion or the wall-clock guard failed."""


# --------------------------------------------------------------------- helpers


def parse_args():
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    else:
        argv = []
    parser = argparse.ArgumentParser(prog="scripts/scale")
    parser.add_argument("--out", default="", help="write the JSON report here")
    parser.add_argument(
        "--quick",
        action="store_true",
        help="20 stages / 10k triangles / 100 instances (fast smoke, not the claim)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=600.0,
        help="wall-clock guard in seconds (default: 600)",
    )
    return parser.parse_args(argv)


def require(name, condition, detail=""):
    """Record one structural check and stop on failure."""
    entry = {"name": name, "ok": bool(condition), "detail": str(detail)}
    CHECKS.append(entry)
    if not condition:
        raise ScaleError("%s%s" % (name, ": " + str(detail) if detail else ""))
    return True


def guard(label):
    if _DEADLINE is not None and time.monotonic() > _DEADLINE:
        raise ScaleError("scale run exceeded its wall-clock guard before %s" % label)


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


@contextlib.contextmanager
def quiet_stdout():
    """Silence the operator's per-stage ``Info: Added stage ...`` report lines.

    Blender writes those reports to file descriptor 1, not through Python's
    buffered ``sys.stdout``. The test/runner report is on stderr, so a failure
    still reaches the caller.
    """
    try:
        sys.stdout.flush()
        saved = os.dup(1)
    except (AttributeError, OSError):
        yield
        return
    try:
        with open(os.devnull, "w") as sink:
            os.dup2(sink.fileno(), 1)
        yield
    finally:
        try:
            sys.stdout.flush()
        except (AttributeError, ValueError):
            pass
        os.dup2(saved, 1)
        os.close(saved)


def triangle_grid(triangles):
    """An exact quad grid of ``triangles`` triangles (the count is even)."""
    if triangles < 2 or triangles % 2:
        raise ScaleError("triangle count must be a positive even number")
    quads = triangles // 2
    columns = math.isqrt(quads)
    while columns > 1 and quads % columns:
        columns -= 1
    rows = quads // columns
    expected = columns * rows * 2
    if expected != triangles:
        raise ScaleError("cannot factor %d quads into a grid" % quads)
    xs = np.linspace(0.0, 10.0, columns + 1, dtype=np.float32)
    ys = np.linspace(0.0, 10.0, rows + 1, dtype=np.float32)
    gx, gy = np.meshgrid(xs, ys, indexing="ij")
    verts = np.stack(
        [gx.reshape(-1), gy.reshape(-1), np.zeros(gx.size, dtype=np.float32)], axis=1
    )
    i = np.arange(columns, dtype=np.int64)[:, None]
    j = np.arange(rows, dtype=np.int64)[None, :]
    a = (i * (rows + 1) + j).reshape(-1)
    quads_array = np.stack(
        [a, a + rows + 1, a + rows + 2, a + 1], axis=1
    ).astype(np.int32)
    return verts, quads_array, expected


class RecordingLayout:
    """The UI layout subset the real panel uses, with icon validation."""

    icon_names = frozenset()

    def __init__(self):
        self.labels = []
        self.icons = []
        self.operators = []
        self.props = []
        self.use_property_split = False
        self.use_property_decorate = False
        self.index = -1

    def _icon(self, icon):
        if icon is None:
            return
        self.icons.append(icon)
        if icon not in self.icon_names:
            raise ValueError("icon %r is not registered in this Blender build" % (icon,))

    def row(self):
        return self

    def box(self):
        return self

    def column(self):
        return self

    def label(self, text="", icon=None):
        self._icon(icon)
        self.labels.append(text)

    def operator(self, idname, text="", icon=None, **kwargs):
        self._icon(icon)
        self.operators.append((idname, text))
        return types.SimpleNamespace(index=self.index)

    def prop(self, holder, attr, text=None, **kwargs):
        if isinstance(attr, str) and not attr.startswith("["):
            getattr(holder, attr)
        self.props.append((holder, attr, text))


class PanelHarness:
    """Bind the real panel methods to a recording layout, without registering."""

    def __init__(self, layout, panel):
        self.layout = layout
        self.draw = panel.draw.__get__(self)
        self.draw_stage = panel.draw_stage.__get__(self)
        self.draw_param = panel.draw_param.__get__(self)


def draw_panel(obj):
    RecordingLayout.icon_names = ui._ICON_NAMES
    layout = RecordingLayout()
    PanelHarness(layout, ui.STAGELOOM_PT_panel).draw(types.SimpleNamespace(object=obj))
    return layout


# -------------------------------------------------------------------- scenarios


def scenario_stages(args, report):
    guard("stages")
    harness.reset_scene()
    count = 20 if args.quick else 200
    verts, quads, _actual = harness.grid_arrays(1000)
    obj = harness.mesh_object("scale-stages", verts, quads)
    rss_before = harness.rss_mb()

    labels = ["Scale %03d" % (index + 1) for index in range(count)]
    started = time.perf_counter()
    with quiet_stdout():
        for label in labels:
            harness.add_stage(obj, label)
    build_ms = (time.perf_counter() - started) * 1000.0

    record = stack_mod.stack_of(obj)
    require(
        "%d stage records" % count,
        record is not None and len(record.stages) == count,
        "found %s" % (len(record.stages) if record is not None else None),
    )
    require(
        "stage labels and creation order",
        [stage.label for stage in record.stages] == labels,
        "record labels differ from the creation order",
    )
    keys = [stage.key for stage in record.stages]
    require("stage keys are unique", len(set(keys)) == count, "%d unique" % len(set(keys)))
    require(
        "one modifier per stage",
        len(obj.modifiers) == count,
        "%d modifiers" % len(obj.modifiers),
    )

    def rebuild_index():
        ENGINE.index_dirty = True
        lifecycle._ensure_index()

    _, index_stats = harness.timed(rebuild_index, 5, warmup=True)
    require(
        "index contains every stage key",
        len(ENGINE.stage_index) == count,
        "%d keys" % len(ENGINE.stage_index),
    )
    require(
        "index has only the one stack object",
        len(ENGINE.stack_objects) == 1 and ENGINE.stack_objects[0] is obj,
        "%d stack objects" % len(ENGINE.stack_objects),
    )
    require(
        "each key has one owner and no conflict",
        len(ENGINE.stage_owners) == count
        and len(ENGINE.stage_claims) == count
        and not ENGINE.stage_conflicts
        and all(ENGINE.stage_owners[key] is obj for key in keys),
        "%d owners, %d claims, %d conflicts"
        % (len(ENGINE.stage_owners), len(ENGINE.stage_claims), len(ENGINE.stage_conflicts)),
    )

    ordered, ordered_stats = harness.timed(
        lambda: ENGINE.ordered_stages(obj, record), 5, warmup=True
    )
    require(
        "ordered stages follow the modifier/record order",
        [stage.key for stage in ordered] == keys,
        "ordered order differs from the record order",
    )

    rebuilds_before_panel = ENGINE.stats()["index_rebuilds"]
    layout, panel_stats = harness.timed(lambda: draw_panel(obj), 5, warmup=True)
    require(
        "panel draws every stage key",
        all(key in layout.labels for key in keys),
        "a stage key is missing from the panel labels",
    )
    require(
        "panel draws no failure row",
        "panel failed to draw" not in "\n".join(layout.labels),
        "the panel reported a failed stage",
    )
    pulls = sum(1 for idname, _text in layout.operators if idname == "stageloom.pull")
    require(
        "panel emits one Pull row per stage",
        pulls == count,
        "%d Pull rows" % pulls,
    )
    require(
        "panel draw does not rebuild the stage index",
        ENGINE.stats()["index_rebuilds"] == rebuilds_before_panel,
        "index rebuilds %d -> %d"
        % (rebuilds_before_panel, ENGINE.stats()["index_rebuilds"]),
    )

    ENGINE.dirty.clear()
    clones_before = ENGINE.stats()["prefix_clones"]
    started = time.perf_counter()
    busy = lifecycle._process_object(obj)
    first_pass_ms = (time.perf_counter() - started) * 1000.0
    require(
        "a no-client pass reports no work",
        busy is False,
        "the pass reported busy",
    )
    require(
        "a no-client pass reads no prefix",
        ENGINE.stats()["prefix_clones"] == clones_before,
        "%d prefix clones" % (ENGINE.stats()["prefix_clones"] - clones_before),
    )
    states = sorted({stage.state for stage in record.stages})
    require(
        "a no-client pass leaves every stage unbound/disconnected",
        set(states) <= {stack_mod.STATE_UNBOUND, stack_mod.STATE_DISCONNECTED},
        "states %r" % (states,),
    )
    _, pass_stats = harness.timed(
        lambda: lifecycle._process_object(obj), 5, warmup=True
    )

    report["stages"] = {
        "stage_count": count,
        "object_count": 1,
        "modifier_count": len(obj.modifiers),
        "label_order": labels,
        "build_ms": build_ms,
        "index_rebuild_ms": index_stats,
        "ordered_stages_ms": ordered_stats,
        "panel_draw_ms": panel_stats,
        "engine_pass_ms": pass_stats,
        "engine_pass_first_ms": first_pass_ms,
        "index": {
            "keys": len(ENGINE.stage_index),
            "stack_objects": len(ENGINE.stack_objects),
            "owners": len(ENGINE.stage_owners),
            "claims": len(ENGINE.stage_claims),
            "conflicts": len(ENGINE.stage_conflicts),
        },
        "panel": {
            "labels": len(layout.labels),
            "operators": len(layout.operators),
            "props": len(layout.props),
            "pull_rows": pulls,
        },
        "engine_pass": {
            "busy": busy,
            "prefix_clones_delta": ENGINE.stats()["prefix_clones"] - clones_before,
            "states": states,
        },
        "rss_mb_before": rss_before,
        "rss_mb_after": harness.rss_mb(),
    }
    print(
        "[scale] %d stages: build %.0f ms, index %.2f ms, order %.2f ms, "
        "panel %.2f ms, no-client pass %.2f ms (first %.2f ms)"
        % (
            count,
            build_ms,
            index_stats["p50"],
            ordered_stats["p50"],
            panel_stats["p50"],
            pass_stats["p50"],
            first_pass_ms,
        )
    )
    bpy.data.objects.remove(obj, do_unlink=True)


def scenario_triangles(args, report):
    guard("triangles")
    harness.reset_scene()
    triangles = 10_000 if args.quick else 100_000
    verts, quads, actual = triangle_grid(triangles)
    obj = harness.mesh_object("scale-triangles", verts, quads)
    mesh = obj.data
    mesh.calc_loop_triangles()
    rss_before = harness.rss_mb()

    require(
        "%d triangles in the source mesh" % triangles,
        actual == triangles and len(mesh.loop_triangles) == triangles,
        "%d grid triangles, %d mesh loop triangles"
        % (actual, len(mesh.loop_triangles)),
    )
    require(
        "exact quad grid shape",
        len(quads) == triangles // 2 and len(mesh.vertices) == len(verts),
        "%d quads, %d vertices" % (len(quads), len(mesh.vertices)),
    )

    sections, read_stats = harness.timed(
        lambda: geometry_io.mesh_to_sections(mesh), 5, warmup=True
    )
    by_name = harness.sections_dict(sections)
    require(
        "mesh read preserves the vertex table",
        by_name["mesh.verts"].shape == (len(verts), 3),
        "%r" % (by_name["mesh.verts"].shape,),
    )
    require(
        "mesh read preserves every quad",
        len(by_name["mesh.face_counts"]) == len(quads)
        and int(by_name["mesh.face_counts"].sum()) == len(quads) * 4,
        "%d faces, %d loop indices"
        % (len(by_name["mesh.face_counts"]), len(by_name["mesh.face_indices"])),
    )

    target = bpy.data.meshes.new("scale-triangles-applied")
    geometry_io.sections_to_mesh(target, sections)
    target.calc_loop_triangles()
    require(
        "fresh apply preserves the %d-triangle shape" % triangles,
        len(target.vertices) == len(verts)
        and len(target.polygons) == len(quads)
        and len(target.loop_triangles) == triangles,
        "%d vertices, %d polygons, %d loop triangles"
        % (len(target.vertices), len(target.polygons), len(target.loop_triangles)),
    )

    def apply_fresh():
        fresh = bpy.data.meshes.new("scale-apply-target")
        try:
            geometry_io.sections_to_mesh(fresh, sections)
        finally:
            bpy.data.meshes.remove(fresh)

    _, fresh_stats = harness.timed(apply_fresh, 5, warmup=True)

    moved_verts = np.array(by_name["mesh.verts"], dtype=np.float32, copy=True)
    moved_verts = moved_verts.reshape(-1, 3)
    moved_verts[:, 0] += 0.5
    moved = [("mesh.verts", "f32", moved_verts.reshape(-1))]
    moved.extend(
        (name, dtype, values)
        for name, dtype, values in sections
        if name != "mesh.verts"
    )
    before_x = float(target.vertices[0].co.x)
    with mock.patch.object(
        geometry_mesh,
        "_topology_matches",
        side_effect=AssertionError("the in-place scale path re-read topology"),
    ):
        geometry_io.sections_to_mesh(
            target, moved, topology=TOKEN, previous=TOKEN
        )
    target.calc_loop_triangles()
    require(
        "in-place apply reads no topology arrays",
        True,
        "the guarded call completed",
    )
    require(
        "in-place apply moves positions and keeps the faces",
        abs(float(target.vertices[0].co.x) - (before_x + 0.5)) < 1e-5
        and len(target.polygons) == len(quads)
        and len(target.loop_triangles) == triangles,
        "%d polygons, %d loop triangles, first x %.6f"
        % (len(target.polygons), len(target.loop_triangles), target.vertices[0].co.x),
    )
    _, inplace_stats = harness.timed(
        lambda: geometry_io.sections_to_mesh(
            target, moved, topology=TOKEN, previous=TOKEN
        ),
        25,
        warmup=True,
    )
    bpy.data.meshes.remove(target)

    report["triangles"] = {
        "requested": triangles,
        "actual": actual,
        "vertices": int(len(verts)),
        "polygons": int(len(quads)),
        "loop_triangles": int(triangles),
        "sections": [name for name, _dtype, _values in sections],
        "read_ms": read_stats,
        "apply_fresh_ms": fresh_stats,
        "apply_inplace_ms": inplace_stats,
        "rss_mb_before": rss_before,
        "rss_mb_after": harness.rss_mb(),
    }
    print(
        "[scale] %d triangles: read %.2f ms, fresh apply %.2f ms, "
        "in-place apply %.2f ms"
        % (
            triangles,
            read_stats["p50"],
            fresh_stats["p50"],
            inplace_stats["p50"],
        )
    )
    bpy.data.objects.remove(obj, do_unlink=True)


def scenario_instances(args, report):
    guard("instances")
    harness.reset_scene()
    count = 100 if args.quick else 10_000
    verts, quads, definition_triangles = harness.grid_arrays(4)
    source = harness.mesh_object("scale-definition", verts, quads)
    source.data.calc_loop_triangles()
    positions = np.array(
        [
            [float(index % 100) * 2.0, float(index // 100) * 2.0, 0.0]
            for index in range(count)
        ],
        dtype=np.float32,
    )
    points = harness.point_cloud_object("scale-points", positions)
    holder = harness.instance_holder("scale-instance-holder", source, points)
    stage = harness.add_stage(holder, "Instances")
    record = stack_mod.stack_of(holder)
    rss_before = harness.rss_mb()

    require(
        "definition is a two-triangle mesh",
        len(source.data.loop_triangles) == definition_triangles,
        "%d triangles" % len(source.data.loop_triangles),
    )

    def cold_prefix():
        lifecycle._forget_prefix(holder, stage.key)
        ENGINE.prefix_memo.pop((holder.as_pointer(), stage.key), None)
        modifier = nodes.modifier_of(holder, stage)
        index = request_mod._modifier_index(holder, modifier)
        return request_mod._clone_prefix(holder, index, bool(record.subd), stage.key)

    sections, prefix_stats = harness.timed(cold_prefix, 5)
    sections, attributes, definitions = cold_prefix()
    by_name = harness.sections_dict(sections)
    require(
        "one instance definition",
        definitions == [{"name": source.name}],
        "%r" % (definitions,),
    )
    require(
        "%d instance rows" % count,
        len(by_name.get("inst.ref_def", ())) == count,
        "%d rows" % len(by_name.get("inst.ref_def", ())),
    )
    require(
        "every row references definition 0",
        set(int(value) for value in by_name["inst.ref_def"]) == {0},
        "references %r" % sorted(set(int(value) for value in by_name["inst.ref_def"])),
    )
    matrices = np.asarray(by_name["inst.transforms"], dtype=np.float32).reshape(-1, 16)
    require(
        "one 4x4 transform per instance row",
        matrices.shape == (count, 16),
        "%r" % (matrices.shape,),
    )
    first = [float(matrices[0][3]), float(matrices[0][7]), float(matrices[0][11])]
    last = [float(matrices[-1][3]), float(matrices[-1][7]), float(matrices[-1][11])]
    require(
        "row transforms carry the point translations",
        first == [0.0, 0.0, 0.0] and last == [float(positions[-1][0]), float(positions[-1][1]), 0.0],
        "first %r last %r" % (first, last),
    )
    require(
        "definition sections are present",
        "idef.0.mesh.verts" in by_name
        and by_name["idef.0.mesh.verts"].shape == (4, 3)
        and len(by_name["idef.0.mesh.face_counts"]) == 1,
        "definition mesh sections are missing",
    )
    require(
        "single-part definitions have no nested row namespace",
        not any(name.startswith("idef.0.inst.") for name in by_name),
        "a nested row namespace appeared",
    )

    def apply_with(offset, token=TOKEN):
        payload = {}
        for name, _dtype, values in sections:
            if name == "inst.transforms":
                moved = np.array(values, dtype=np.float32, copy=True).reshape(-1, 16)
                moved[:, 3] += offset
                payload[name] = moved.reshape(-1)
            else:
                payload[name] = values
        return apply_mod._apply(
            holder, stage, payload, attributes, definitions=definitions, topology=token
        )

    stage.applied_topology = ""
    first_result, apply_first_stats = harness.timed(lambda: apply_with(0.0), 1, warmup=False)
    require(
        "first apply stores the topology token",
        first_result == TOKEN,
        "returned %r" % (first_result,),
    )
    stage.applied_topology = TOKEN
    group = bpy.data.node_groups[nodes.group_name(stage.key)]

    clouds = helpers.instance_clouds(holder, stage.key)
    require(
        "apply builds one %d-point row cloud" % count,
        sorted(clouds) == [0] and len(clouds[0].data.points) == count,
        "clouds %r" % ({key: len(value.data.points) for key, value in clouds.items()},),
    )
    definition_collections = helpers.definition_collections(holder, stage.key)
    require(
        "apply builds one definition collection",
        sorted(definition_collections) == [0],
        "definitions %r" % (sorted(definition_collections),),
    )
    definition_helper = bpy.data.objects.get(
        helpers.definition_name(holder, stage.key, 0, "mesh")
    )
    require(
        "definition helper keeps the mesh",
        definition_helper is not None and len(definition_helper.data.vertices) == 4,
        "definition helper is missing",
    )

    _, apply_repeat_stats = harness.timed(lambda: apply_with(1.0), 5, warmup=True)
    rebuilt = (
        bpy.data.node_groups[nodes.group_name(stage.key)].as_pointer()
        != group.as_pointer()
    )
    require(
        "repeated apply reuses the node group",
        rebuilt is False,
        "the node group was rebuilt",
    )
    cloud = clouds[0]
    co = np.empty(len(cloud.data.points) * 3, dtype=np.float32)
    cloud.data.points.foreach_get("co", co)
    co = co.reshape(-1, 3)
    require(
        "repeated apply moves the existing rows",
        abs(float(co[0][0]) - 1.0) < 1e-5,
        "first row x is %r" % (float(co[0][0]),),
    )

    bpy.context.view_layer.update()
    geometry = holder.evaluated_get(
        bpy.context.evaluated_depsgraph_get()
    ).evaluated_geometry()
    evaluated_points = geometry.instances_pointcloud()
    references = geometry.instance_references()
    require(
        "evaluation shows %d instances" % count,
        evaluated_points is not None and len(evaluated_points.points) == count,
        "%s points"
        % (len(evaluated_points.points) if evaluated_points is not None else None),
    )
    require(
        "evaluation shows one instance reference",
        len(references) == 1 and references[0].mesh is not None
        and len(references[0].mesh.vertices) == 4,
        "%d references" % len(references),
    )

    report["instances"] = {
        "instances": count,
        "definitions": len(definitions),
        "definition_name": source.name,
        "definition_triangles": len(source.data.loop_triangles),
        "sections": [name for name, _dtype, _values in sections],
        "prefix_ms": prefix_stats,
        "apply_first_ms": apply_first_stats,
        "apply_repeat_ms": apply_repeat_stats,
        "group_reused": not rebuilt,
        "evaluated_instances": len(evaluated_points.points),
        "evaluated_references": len(references),
        "rss_mb_before": rss_before,
        "rss_mb_after": harness.rss_mb(),
    }
    print(
        "[scale] %d instances: prefix %.2f ms, first apply %.2f ms, "
        "repeat apply %.2f ms, group reused %s"
        % (
            count,
            prefix_stats["p50"],
            apply_first_stats["p50"],
            apply_repeat_stats["p50"],
            not rebuilt,
        )
    )
    bpy.data.objects.remove(holder, do_unlink=True)


# ----------------------------------------------------------------------- main


def main():
    global _DEADLINE
    args = parse_args()
    _DEADLINE = time.monotonic() + args.timeout
    report = {
        "format": "stageloom-scale-report/1",
        "meta": {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "blender": bpy.app.version_string,
            "python": platform.python_version(),
            "os": platform.system(),
            "arch": platform.machine(),
            "platform": platform.platform(),
            "processor": harness.cpu_model(),
            "quick": bool(args.quick),
            "args": vars(args),
        },
        "shapes": {
            "stages": 20 if args.quick else 200,
            "triangles": 10_000 if args.quick else 100_000,
            "instances": 100 if args.quick else 10_000,
        },
        "ok": False,
    }
    started = time.perf_counter()
    print(
        "[scale] Blender %s on %s (quick=%s)"
        % (bpy.app.version_string, platform.platform(), args.quick)
    )
    try:
        harness.start_addon()
        scenario_stages(args, report)
        scenario_triangles(args, report)
        scenario_instances(args, report)
    except Exception as exc:  # noqa: BLE001 - the report records every failure
        report["error"] = "%s: %s" % (type(exc).__name__, exc)
        traceback.print_exc()
    finally:
        try:
            harness.stop_addon()
        except Exception as exc:  # noqa: BLE001 - cleanup must not hide the result
            report.setdefault("cleanup_errors", []).append(
                "%s: %s" % (type(exc).__name__, exc)
            )

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    failed = [item for item in CHECKS if not item["ok"]]
    report["checks"] = CHECKS
    report["failed_checks"] = [item["name"] for item in failed]
    report["elapsed_ms"] = elapsed_ms
    report["peak_rss_mb"] = peak_rss_mb()
    report["ok"] = not failed and not report.get("error") and elapsed_ms <= args.timeout * 1000.0
    if elapsed_ms > args.timeout * 1000.0:
        report["error"] = "scale run exceeded its %.0fs wall-clock guard" % args.timeout
        report["ok"] = False

    print("[scale] checks:")
    for item in CHECKS:
        print(
            "  %-4s %s%s"
            % (
                "ok" if item["ok"] else "FAIL",
                item["name"],
                " (%s)" % item["detail"] if item["detail"] and not item["ok"] else "",
            )
        )
    if args.out:
        try:
            with open(args.out, "w") as handle:
                json.dump(report, handle, indent=2)
            print("[scale] wrote %s" % args.out)
        except OSError as exc:
            print("[scale] cannot write %s: %s" % (args.out, exc), file=sys.stderr)
            report["ok"] = False
    if report["ok"]:
        print(
            "[scale] PASS %d checks in %.0f ms (peak RSS %s MiB)"
            % (
                len(CHECKS),
                elapsed_ms,
                "n/a" if report["peak_rss_mb"] is None else "%.1f" % report["peak_rss_mb"],
            )
        )
        return 0
    print(
        "[scale] FAIL: %s"
        % (
            report.get("error")
            or "structural check(s): %s" % ", ".join(report["failed_checks"])
        ),
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
