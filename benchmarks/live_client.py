"""Measure one live Grasshopper stage from the installed Blender extension.

Run inside Blender with a reference `.blend` and the 0.7.0 extension enabled:

    blender -b /tmp/opencode/stageloom-live/instances-13.blend --online-mode \
        --python benchmarks/live_client.py -- \
        --out /tmp/opencode/stageloom-live/instances-preview-on.json \
        --label "instances-13, preview on" --samples 5

The script starts the add-on's server when it is not running, waits for the
Grasshopper client, then forces a full request per sample and records the
engine's own `prefix`, `encode`, `rtt` (the panel's "answer") and `apply`
timings. It does not guess the Grasshopper side; the split is the difference
between `rtt` and Blender's own work.
"""

import argparse
import importlib
import json
import sys
import time

import bpy


def parse_args():
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    parser = argparse.ArgumentParser(prog="benchmarks/live_client.py")
    parser.add_argument("--out", required=True, help="JSON report path")
    parser.add_argument("--label", default="live", help="human label for this run")
    parser.add_argument("--port", type=int, default=59130)
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=120.0)
    return parser.parse_args(argv)


def addon_modules():
    names = [
        name for name in bpy.context.preferences.addons.keys() if "stageloom" in name
    ]
    if not names:
        raise RuntimeError("the Stageloom extension is not enabled in this config")
    package = names[0]
    return (
        package,
        importlib.import_module(package + ".blender.engine"),
        importlib.import_module(package + ".blender.stack"),
        importlib.import_module(package + ".blender.bridge"),
    )


def locate_stage(stack_mod):
    for obj in bpy.data.objects:
        if not hasattr(obj, "gh_stack"):
            continue
        record = stack_mod.stack_of(obj)
        if record is None or not len(record.stages):
            continue
        return obj, record, record.stages[0]
    raise RuntimeError("the reference scene has no Stageloom stage")


def pump(engine, seconds, until):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        engine._timer()
        if until():
            return True
        time.sleep(0.002)
    return bool(until())


def pending(engine, record, stage):
    return engine._pending.get((record.stack_id, stage.key)) or {}


def wait_settled(engine, record, stage, timeout):
    return pump(
        engine,
        timeout,
        lambda: stage.state == "ok" and not pending(engine, record, stage),
    )


def main():
    args = parse_args()
    package, engine, stack_mod, bridge = addon_modules()
    obj, record, stage = locate_stage(stack_mod)

    if not engine.server.SERVER.running:
        engine.start_server(args.port)
    if not engine.server.SERVER.running:
        raise RuntimeError("the Stageloom server did not start on %d" % args.port)

    connected = pump(engine, args.timeout, lambda: bridge.state["clients"] >= 1)
    if not connected:
        raise RuntimeError("no Grasshopper client connected")
    if not wait_settled(engine, record, stage, args.timeout):
        raise RuntimeError(
            "the first stage solve did not settle: %s / %s"
            % (stage.state, stage.error)
        )

    samples = []
    for index in range(args.samples):
        key = (record.stack_id, stage.key)
        engine._timings.pop(key, None)
        engine._sent.pop(key, None)
        engine._prefix_memo.pop((obj.as_pointer(), stage.key), None)
        engine._prefix_batch.clear()
        engine._prefix_batch_built = False
        stats_before = dict(engine.stats)
        started = time.monotonic()
        engine.request_again(obj, stage)
        if not wait_settled(engine, record, stage, args.timeout):
            samples.append(
                {
                    "sample": index + 1,
                    "ok": False,
                    "state": stage.state,
                    "error": stage.error,
                    "total_ms": (time.monotonic() - started) * 1000.0,
                    "timings": engine.stage_timings(record.stack_id, stage.key),
                    "stats": {
                        name: engine.stats[name] - stats_before[name]
                        for name in stats_before
                    },
                }
            )
            break
        samples.append(
            {
                "sample": index + 1,
                "ok": True,
                "state": stage.state,
                "error": stage.error,
                "total_ms": (time.monotonic() - started) * 1000.0,
                "timings": engine.stage_timings(record.stack_id, stage.key),
                "passes": engine.stats["passes"],
                "stats": {
                    name: engine.stats[name] - stats_before[name]
                    for name in stats_before
                },
            }
        )

    report = {
        "label": args.label,
        "scene": bpy.data.filepath,
        "addon": package,
        "stage_key": stage.key,
        "object": obj.name,
        "port": engine.server.SERVER.port,
        "clients": bridge.state["clients"],
        "samples": samples,
        "meta": {
            "blender": bpy.app.version_string,
            "python": sys.version.split()[0],
        },
    }
    with open(args.out, "w") as handle:
        json.dump(report, handle, indent=2)
    print("[live-client] wrote %s" % args.out)
    return 0 if all(sample.get("ok") for sample in samples) else 1


if __name__ == "__main__":
    raise SystemExit(main())
