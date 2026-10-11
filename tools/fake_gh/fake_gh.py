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


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9130)
    ap.add_argument("--stage", required=True)
    ap.add_argument("--scale-param", default=PARAM_ID)
    ap.add_argument("--report", default="")
    ap.add_argument("--host", default="127.0.0.1")
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
        if args.report:
            with open(args.report, "w") as f:
                json.dump(
                    {"full": state.full, "ref": state.ref, "responses": state.responses},
                    f,
                    indent=2,
                )
    return 0


async def session(ws, args, state):
    print("[fake-gh] connected to blender", flush=True)
    hello = {
        "type": "hello",
        "client": "fake-gh 0.1",
        "doc": "fake.gh",
        "doc_units": "Meters",
        "stages": [{"key": args.stage, "enabled": True}],
    }
    await ws.send(codec.encode_frame(hello))
    schema = {
        "type": "schema",
        "stage": args.stage,
        "params": [
            {
                "id": args.scale_param,
                "label": "Scale",
                "type": "float",
                "default": 1.0,
                "min": 0.1,
                "max": 10.0,
                "step": 0.1,
                "choices": [],
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
    await ws.send(codec.encode_frame(schema))

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
        out_sections = _scaled_sections(state.sections, scale)
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
            "counts": {"meshes": 1, "subd": 0, "curves": 0, "points": 0, "approximated_curves": 0},
        }
        state.responses += 1
        await ws.send(codec.encode_frame(reply, out_sections))
        print("[fake-gh] response %d (scale %.3f, %s)" % (state.responses, scale, mode), flush=True)


def _np_dtype(name):
    if name.endswith(".colors"):
        return np.uint8
    if name.endswith(".verts") or name.endswith(".crease_values"):
        return np.float32
    return np.uint32


def _proto_dtype(name):
    if name.endswith(".colors"):
        return "u8"
    if name.endswith(".verts") or name.endswith(".crease_values"):
        return "f32"
    return "u32"


def _scaled_sections(sections, scale):
    out = []
    for name, values in sections.items():
        if name.endswith(".verts"):
            out.append((name, "f32", (values.astype(np.float32) * scale).reshape(-1, 3)))
        elif name.endswith(".colors"):
            out.append((name, "u8", values.reshape(-1, 4)))
        elif name.endswith(".crease_edges"):
            out.append((name, "u32", values.reshape(-1, 2)))
        elif name.endswith(".crease_values"):
            out.append((name, "f32", values))
        else:
            out.append((name, "u32", values))
    return out


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
