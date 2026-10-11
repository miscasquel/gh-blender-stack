"""S4 driver: ask the spike .gha to run the SubD and curve checks, record results.

    PYTHONPATH=blender/gh_stack/vendor python3 spikes/s4/driver.py

Blender's role here is just a socket peer: the checks run inside Rhino (they need
RhinoCommon). Start this driver first, then drop any spike component on the
Grasshopper canvas (it starts the link); the driver sends one request, prints the
results and writes spikes/results/s4.txt.
"""

import asyncio
import json
import os
import sys
import time

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
for path in (os.path.join(REPO_ROOT, "blender", "gh_stack", "vendor"), REPO_ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)

import websockets  # noqa: E402
from blender.gh_stack.core import codec  # noqa: E402

RESULTS = os.path.join(REPO_ROOT, "spikes", "results")
TRANSCRIPT = []


def log(msg):
    print(f"[s4] {msg}", flush=True)
    TRANSCRIPT.append(msg)


async def session(ws):
    log(f"connection from {ws.remote_address}")
    request = codec.encode_frame(
        {"type": "request", "check": "s4", "stage": "s4-check", "rid": "sha256:s4"}
    )
    t0 = time.perf_counter()
    await ws.send(request)
    reply = await asyncio.wait_for(ws.recv(), timeout=120)
    rtt_ms = (time.perf_counter() - t0) * 1000.0
    header, _ = codec.decode_frame(reply)
    log(f"reply in {rtt_ms:.1f} ms, status={header.get('status')}")
    if header.get("status") != "ok":
        log(f"error: {header.get('error')}")
        return False
    results = header.get("results", {})
    for key in sorted(results):
        value = results[key]
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value)
        log(f"  {key}: {value}")
    return True


async def main():
    outcome = {"ok": False, "done": False, "running": False}

    async def handler(ws):
        if outcome["running"]:
            log("extra connection ignored")
            return
        outcome["running"] = True
        try:
            outcome["ok"] = await session(ws)
        except Exception as ex:
            log(f"session failed: {ex!r}")
        finally:
            outcome["running"] = False
            outcome["done"] = True

    async with websockets.serve(handler, "127.0.0.1", 9131, max_size=None):
        log("listening on ws://127.0.0.1:9131/s1 (the spike's endpoint)")
        log("waiting for the spike .gha; drop any spike component on the canvas")
        deadline = time.time() + 300
        while not outcome["done"] and time.time() < deadline:
            await asyncio.sleep(0.2)
        if not outcome["done"]:
            log("RESULT: FAIL (no connection within 5 minutes)")

    log("RESULT: " + ("PASS" if outcome["ok"] else "FAIL"))
    os.makedirs(RESULTS, exist_ok=True)
    with open(os.path.join(RESULTS, "s4.txt"), "w") as f:
        f.write("\n".join(TRANSCRIPT) + "\n")
    log("results: spikes/results/s4.txt")
    return 0 if outcome["ok"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
