"""A Python stand-in for the Grasshopper side, for layer B tests (plan §14).

    PYTHONPATH=blender/gh_stack/vendor python3 tools/fake_gh/fake_gh.py \
        --port 9130 --stage <key> [--scale 2.0] [--report /tmp/report.json]

It speaks protocol v2: sends `hello` and one `schema` (a float parameter), then
answers every `request`. The "definition" is deliberately trivial and visible:
the input mesh is scaled by the parameter value, so a test can assert the
round trip and the parameter flow. It never draws a dialog and never blocks.

Full requests replace the stored geometry; ref requests reuse it. The report
counts requests by mode, which is how the tests check that parameter-only
changes stay `ref`.
"""

import argparse
import asyncio
import json
import os
import struct
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
for path in (os.path.join(REPO_ROOT, "blender", "gh_stack", "vendor"), REPO_ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)

import numpy as np  # noqa: E402
import websockets  # noqa: E402
from blender.gh_stack.core import codec, hashing  # noqa: E402

PARAM_ID = "00000000-0000-4000-8000-0000000000aa"


class StageState(object):
    def __init__(self):
        self.sections = None  # stored input geometry
        self.full = 0
        self.ref = 0
        self.responses = 0
        self.received_verts = 0
        self.received_faces = 0
        self.received_max = 0.0
        self.received_curve_points = 0
        self.received_first = None
        self.echoes = 0  # identical responses re-sent
        self.edits = 0  # same-rid responses with a changed output (GH-side edit)
        self.schema_updates = 0  # changed schemas re-sent


def _summarise(state):
    """What the last full request carried, for tests that assert on a prefix."""
    verts = state.sections.get("mesh.verts") if state.sections else None
    if verts is None and state.sections:
        verts = state.sections.get("subd.verts")
    faces = state.sections.get("mesh.face_counts") if state.sections else None
    state.received_verts = int(len(verts)) if verts is not None else 0
    state.received_faces = int(len(faces)) if faces is not None else 0
    if verts is not None and len(verts):
        state.received_max = float(np.abs(np.asarray(verts, dtype=np.float32)).max())
    else:
        state.received_max = 0.0

    counts = state.sections.get("crv.counts") if state.sections else None
    state.received_curve_points = int(np.asarray(counts, dtype=np.uint32).sum()) if counts is not None else 0

    first = None
    if verts is not None and len(verts):
        first = np.asarray(verts, dtype=np.float32).reshape(-1, 3)[0]
    elif state.sections and state.sections.get("crv.points") is not None:
        first = np.asarray(state.sections["crv.points"], dtype=np.float32).reshape(-1, 4)[0][:3]
    elif state.sections and state.sections.get("pts.positions") is not None:
        first = np.asarray(state.sections["pts.positions"], dtype=np.float32).reshape(-1, 3)[0]
    state.received_first = [float(v) for v in first] if first is not None else None


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9130)
    ap.add_argument("--stage", required=True)
    ap.add_argument("--scale-param", default=PARAM_ID)
    ap.add_argument("--report", default="")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--doc", default="fake.gh",
                    help="document name reported in hello, to test document resets")
    ap.add_argument("--extras", action="store_true",
                    help="also answer with a polyline and points built from the mesh")
    ap.add_argument("--delay-ms", type=int, default=0,
                    help="wait this long before answering, to test waiting stages")
    ap.add_argument("--echo-after", type=int, default=0,
                    help="after this many responses, re-send the identical frame")
    ap.add_argument("--gh-edit-after", type=int, default=0,
                    help="after this many responses, send a same-rid response with a changed output")
    ap.add_argument("--schema-after", type=int, default=0,
                    help="after this many responses, send a changed schema")
    args = ap.parse_args()

    state = StageState()
    endpoint = "ws://%s:%d/" % (args.host, args.port)

    # Blender is the server; the GH side connects, with retries, like the real
    # plugin does with its backoff.
    ws = None
    for attempt in range(50):
        try:
            ws = await websockets.connect(endpoint, max_size=None)
            break
        except OSError:
            await asyncio.sleep(0.2)
    if ws is None:
        print("[fake-gh] could not connect to %s" % endpoint, flush=True)
        return 1

    try:
        await session(ws, args, state)
    finally:
        _write_report(args, state)
    return 0


def _write_report(args, state):
    """Write the counters, refreshed on every response so tests can poll it."""
    if not args.report:
        return
    with open(args.report, "w") as f:
        json.dump(
            {
                "full": state.full,
                "ref": state.ref,
                "responses": state.responses,
                "echoes": state.echoes,
                "edits": state.edits,
                "schema_updates": state.schema_updates,
                "sections": sorted(state.sections.keys()) if state.sections else [],
                "received_verts": state.received_verts,
                "received_faces": state.received_faces,
                "received_max": state.received_max,
                "received_curve_points": state.received_curve_points,
                "received_first": state.received_first,
            },
            f,
            indent=2,
        )


def _schema_message(stage, scale_param, suffix=""):
    return {
        "type": "schema",
        "stage": stage,
        "params": [
            {
                "id": scale_param,
                "label": "Scale" + suffix,
                "type": "float",
                "default": 1.0,
                "min": 0.1,
                "max": 10.0,
                "step": 0.1,
                "choices": [],
                "group": "Shape",
            },
            # A value list parameter, so the dropdown path is exercised end to end.
            {
                "id": "00000000-0000-4000-8000-0000000000cc",
                "label": "Mode",
                "type": "value_list",
                "default": 1,
                "min": None,
                "max": None,
                "step": None,
                "values": ["Ribbon", "Panel", "Grid"],
                "group": "Shape",
            },
            # A second parameter, of a different type, so the schema path is
            # exercised for arrays as well as scalars.
            {
                "id": "00000000-0000-4000-8000-0000000000bb",
                "label": "Translation",
                "type": "vector3",
                "default": [0.0, 1.0, 2.0],
                "min": None,
                "max": None,
                "step": 0.1,
                "choices": [],
                "group": "Shape",
            },
        ],
    }


async def session(ws, args, state):
    print("[fake-gh] connected to blender", flush=True)
    hello = {
        "type": "hello",
        "client": "fake-gh 0.1",
        "doc": args.doc,
        "doc_units": "Meters",
        "stages": [{"key": args.stage, "enabled": True}],
    }
    await ws.send(codec.encode_frame(hello))
    await ws.send(codec.encode_frame(_schema_message(args.stage, args.scale_param)))

    async for message in ws:
        header, sections = codec.decode_frame(message)
        if header.get("type") != "request":
            continue
        if header.get("stage") != args.stage:
            continue
        mode = (header.get("geom") or {}).get("mode")
        if mode == "full":
            state.full += 1
            state.sections = {
                name: np.asarray(values, dtype=_np_dtype(name)) for name, values in sections.items()
            }
            _summarise(state)
        else:
            state.ref += 1
        if state.sections is None:
            reply = {
                "type": "response",
                "stage": args.stage,
                "rid": header.get("rid"),
                "out_hash": "",
                "status": "error",
                "error": {"code": "unknown_ref", "message": "no geometry stored"},
                "warnings": [],
            }
            await ws.send(codec.encode_frame(reply))
            continue

        scale = float((header.get("params") or {}).get(args.scale_param, 1.0))
        if args.delay_ms:
            await asyncio.sleep(args.delay_ms / 1000.0)
        out_sections = _scaled_sections(state.sections, scale)
        counts = {"meshes": 1, "subd": 0, "curves": 0, "points": 0, "approximated_curves": 0}
        if args.extras:
            out_sections = _with_extras(out_sections, scale)
            counts = {"meshes": 1, "subd": 0, "curves": 1, "points": 1, "approximated_curves": 0}
        out_hash = hashing.output_hash(
            [(name, codec.section_bytes(name, _proto_dtype(name), values)) for name, dtype, values in out_sections]
        )
        reply = {
            "type": "response",
            "stage": args.stage,
            "rid": header.get("rid"),
            "out_hash": out_hash,
            "status": "ok",
            "error": None,
            "warnings": [],
            "counts": counts,
        }
        state.responses += 1
        frame = codec.encode_frame(reply, out_sections)
        await ws.send(frame)
        _write_report(args, state)
        print("[fake-gh] response %d (scale %.3f, %s)" % (state.responses, scale, mode), flush=True)

        # Optional scripted events, one response count each, for the frame-cache
        # tests (they drive the engine's reset and quiet rules deterministically).
        if args.echo_after and state.responses == args.echo_after:
            await ws.send(frame)  # byte-for-byte identical
            state.echoes += 1
            _write_report(args, state)
        if args.gh_edit_after and state.responses == args.gh_edit_after:
            # A GH-side definition change: same request, different answer.
            edited_sections = _scaled_sections(state.sections, 42.0)
            edited_hash = hashing.output_hash(
                [(name, codec.section_bytes(name, _proto_dtype(name), values)) for name, dtype, values in edited_sections]
            )
            edited_reply = {
                "type": "response",
                "stage": args.stage,
                "rid": header.get("rid"),
                "out_hash": edited_hash,
                "status": "ok",
                "error": None,
                "warnings": [],
                "counts": counts,
            }
            await ws.send(codec.encode_frame(edited_reply, edited_sections))
            state.edits += 1
            _write_report(args, state)
            print("[fake-gh] gh-edit response for rid %s" % (header.get("rid"),), flush=True)
        if args.schema_after and state.responses == args.schema_after:
            await ws.send(codec.encode_frame(_schema_message(args.stage, args.scale_param, suffix=" v2")))
            state.schema_updates += 1
            _write_report(args, state)
            print("[fake-gh] schema v2 sent", flush=True)


def _is_u8(name):
    return name.endswith(".colors") or name in (
        "crv.types",
        "crv.orders",
        "crv.knots_mode",
        "crv.cyclic",
    )


def _is_f32(name):
    return (
        name.endswith(".verts")
        or name.endswith(".crease_values")
        or name.endswith(".points")
        or name.endswith(".positions")
        or name.endswith(".radii")
    )


def _np_dtype(name):
    if _is_u8(name):
        return np.uint8
    if _is_f32(name):
        return np.float32
    return np.uint32


def _proto_dtype(name):
    if _is_u8(name):
        return "u8"
    if _is_f32(name):
        return "f32"
    return "u32"


def _scaled_sections(sections, scale):
    """Echo the stored geometry with every position scaled, keeping protocol
    dtypes per section name."""
    out = []
    for name, values in sections.items():
        if name.endswith(".verts") or name == "pts.positions":
            arr = values.astype(np.float32).reshape(-1, 3).copy()
            arr *= scale
            out.append((name, "f32", arr))
        elif name == "crv.points":
            arr = values.astype(np.float32).reshape(-1, 4).copy()
            arr[:, :3] *= scale
            out.append((name, "f32", arr))
        elif name.endswith(".colors"):
            out.append((name, "u8", values.reshape(-1, 4)))
        elif name.endswith(".crease_edges"):
            out.append((name, "u32", values.reshape(-1, 2)))
        elif _is_f32(name):
            out.append((name, "f32", values.reshape(-1)))
        else:
            out.append((name, _proto_dtype(name), values.reshape(-1)))
    return out


def _with_extras(sections, scale):
    """Append a polyline through the first vertices and a point at every vertex,
    so tests can check the curve and point paths against the mesh they came from."""
    verts = None
    for name, dtype, values in sections:
        if name in ("mesh.verts", "subd.verts"):
            verts = values.reshape(-1, 3)
            break
    if verts is None or len(verts) == 0:
        return sections
    n = min(5, len(verts))
    poly = np.ascontiguousarray(verts[:n], dtype=np.float32)
    extras = [
        ("crv.points", "f32", np.hstack([poly, np.ones((n, 1), dtype=np.float32)])),
        ("crv.counts", "u32", np.array([n], dtype=np.uint32)),
        ("crv.types", "u8", np.array([0], dtype=np.uint8)),
        ("crv.orders", "u8", np.array([0], dtype=np.uint8)),
        ("crv.knots_mode", "u8", np.array([0], dtype=np.uint8)),
        ("crv.cyclic", "u8", np.array([0], dtype=np.uint8)),
        ("pts.positions", "f32", np.ascontiguousarray(verts, dtype=np.float32)),
        ("pts.radii", "f32", np.full(len(verts), 0.02, dtype=np.float32)),
    ]
    return list(sections) + extras


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
