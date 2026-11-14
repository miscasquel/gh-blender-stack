"""Build the reference `.blend` scenes for a live Grasshopper measurement.

The benchmark matrix runs entirely inside headless Blender against `fake_gh`;
the live half needs a saved scene a person (or a scripted Rhino) can open. This
script builds the same shapes and writes them to disk, printing the stage keys
so the Grasshopper definition can use them.

    blender -b --factory-startup --online-mode \
        --python benchmarks/live_scene.py -- \
        --out /tmp/opencode/stageloom-live --kind both

Scenes:

``cube``
    One small mesh with a stage: the fixed-overhead baseline.
``instances-13``
    One 100k-triangle definition placed 13x through Instance-on-Points, with
    the stage after the instancing modifier: the instance-heavy case.

Like ``bench.py``, this builder uses the working tree; the saved file is then
opened by the installed extension for the live measurement.
"""

import argparse
import json
import sys
from pathlib import Path

import bpy
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import bench  # noqa: E402


def parse_args():
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    parser = argparse.ArgumentParser(prog="benchmarks/live_scene.py")
    parser.add_argument("--out", required=True, help="directory for the .blend files")
    parser.add_argument(
        "--kind",
        choices=("cube", "instances", "both"),
        default="both",
        help="which reference scene(s) to build",
    )
    parser.add_argument("--triangles", type=int, default=100_000)
    parser.add_argument("--instances", type=int, default=13)
    return parser.parse_args(argv)


def save_scene(path, kind, stage, note):
    bpy.ops.wm.save_as_mainfile(filepath=str(path))
    return {
        "kind": kind,
        "path": str(path),
        "stage_key": stage.key,
        "label": stage.label,
        "note": note,
    }


def build_cube(out_dir):
    bench.reset_scene()
    bpy.ops.mesh.primitive_cube_add(size=2.0)
    obj = bpy.context.object
    obj.name = "LiveCube"
    stage = bench.add_stage(obj, "LiveCube")
    return save_scene(out_dir / "cube.blend", "cube", stage, "fixed-overhead baseline")


def build_instances(out_dir, args):
    bench.reset_scene()
    verts, quads, actual = bench.grid_arrays(args.triangles)
    source = bench.mesh_object("live-instance-source", verts, quads)
    positions = np.array(
        [[float(i % args.instances) * 5.0, 0.0, 0.0] for i in range(args.instances)],
        dtype=np.float32,
    )
    points = bench.point_cloud_object("live-instance-points", positions)
    holder = bench.instance_holder("live-instance-holder", source, points)
    stage = bench.add_stage(holder, "LiveInstances")
    return save_scene(
        out_dir / "instances-13.blend",
        "instances-13",
        stage,
        "%d tris x %d instances" % (actual, args.instances),
    )


def main():
    args = parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    bench.start_addon()
    scenes = []
    try:
        if args.kind in ("cube", "both"):
            scenes.append(build_cube(out_dir))
        if args.kind in ("instances", "both"):
            scenes.append(build_instances(out_dir, args))
    finally:
        bench.stop_addon()
    print("[live-scene] " + json.dumps({"scenes": scenes}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
