"""A stand-in for the C# side, for pre-flighting the S3 driver without Rhino.

Connects to the driver's server like the GH spike does, decodes each frame, and
echoes the sections back with fake timings. Verifies the driver's framing,
verification and reporting; it does not test the conversion (that is what the
live Rhino run is for).

    PYTHONPATH=blender/gh_stack/vendor python3 spikes/s3/fake_gh.py [--port 9131]
"""

import argparse
import asyncio
import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
for path in (os.path.join(REPO_ROOT, "blender", "gh_stack", "vendor"), REPO_ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)

import websockets  # noqa: E402
from blender.gh_stack.core import codec  # noqa: E402


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9131)
    args = ap.parse_args()

    async with websockets.connect(
        f"ws://127.0.0.1:{args.port}/s1", max_size=256 * 1024 * 1024
    ) as ws:
        print("[fake-gh] connected", flush=True)
        async for msg in ws:
            header, sections = codec.decode_frame(msg)
            mtype = header.get("type")
            if mtype == "request" and "sections" in header:
                out = [(e["name"], e["dtype"], sections[e["name"]]) for e in header["sections"]]
                reply = {
                    "type": "response",
                    "stage": header.get("stage"),
                    "rid": header.get("rid"),
                    "status": "ok",
                    "scale": 1.0,
                    "counts": {
                        "verts": len(sections.get("mesh.verts", ())) // 3,
                        "faces": len(sections.get("mesh.face_counts", ())),
                    },
                    "timings": {"in_ms": 1.5, "out_ms": 2.5},
                }
                await ws.send(codec.encode_frame(reply, out))
                print(
                    "[fake-gh] echoed %d section(s) for %s"
                    % (len(out), header.get("stage")),
                    flush=True,
                )
            else:
                reply = {
                    "type": "response",
                    "n": header.get("n"),
                    "solves": 7,
                    "msg": "fake-gh",
                }
                await ws.send(codec.encode_frame(reply, None))
                print("[fake-gh] answered a simple request", flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
