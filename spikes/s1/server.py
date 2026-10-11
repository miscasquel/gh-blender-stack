"""S1 server: the Blender side of the spike (the transport gate).

Stands in for the real add-on (milestone M3). Speaks the spike frame:
[u32 little-endian JSON length][UTF-8 JSON], the same header shape as the protocol
frame (docs/protocol.md §3).

    PYTHONPATH=blender/gh_stack/vendor python3 spikes/s1/server.py [--rounds N] [--port 9131]

- Waits for the GhStack spike .gha to connect.
- On each connection, sends a trigger and waits for the reply, then reports the
  round-trip time. The reply proves the whole chain: background thread received a
  message, ScheduleSolution ran the solution on the UI thread, and the component
  replied from the solve.
- Keeps running so the save-and-reopen check is just: save the document, close it,
  reopen it. The server logs the reconnect; nothing else is needed.
- `--selftest` runs a loopback client that mimics the GH side, so the Python peer
  can be verified before the live Rhino session. Use it before every live run.
"""

import argparse
import asyncio
import json
import os
import struct
import sys
import time

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
VENDOR = os.path.join(REPO_ROOT, "blender", "gh_stack", "vendor")
if VENDOR not in sys.path:
    sys.path.insert(0, VENDOR)

import websockets  # noqa: E402

RESULTS = os.path.join(REPO_ROOT, "spikes", "results")
TRANSCRIPT = []


def log(msg):
    print(f"[s1] {msg}", flush=True)
    TRANSCRIPT.append(msg)


def frame(obj):
    body = json.dumps(obj).encode("utf-8")
    return struct.pack("<I", len(body)) + body


def unframe(data):
    (n,) = struct.unpack_from("<I", data, 0)
    body = data[4:4 + n]
    return json.loads(body.decode("utf-8"))


async def handle(ws, rounds):
    log(f"connection from {ws.remote_address}")
    rtts = []
    for i in range(1, rounds + 1):
        payload = frame({"type": "request", "n": i, "text": "hello from Blender"})
        t0 = time.perf_counter()
        await ws.send(payload)
        try:
            data = await asyncio.wait_for(ws.recv(), timeout=30)
        except asyncio.TimeoutError:
            log(f"  round {i}: no reply in 30 s (GH side unresponsive?)")
            continue
        dt = (time.perf_counter() - t0) * 1000.0
        reply = unframe(data) if isinstance(data, (bytes, bytearray)) else unframe(data.encode())
        log(f"  round {i}: reply in {dt:.1f} ms: {json.dumps(reply, sort_keys=True)}")
        rtts.append(dt)

    # Hold the connection open so save-and-reopen stays observable; the GH side closes first.
    try:
        async for _ in ws:
            pass
    except Exception:
        pass
    return rtts


async def client_mimic(port, rounds):
    """Mimics the GH side: read a trigger, answer it. Used by --selftest."""
    async with websockets.connect(f"ws://127.0.0.1:{port}/s1", max_size=8 * 1024 * 1024) as ws:
        for i in range(rounds):
            data = await asyncio.wait_for(ws.recv(), timeout=10)
            trigger = unframe(data)
            reply = frame(
                {
                    "type": "response",
                    "n": trigger["n"],
                    "solves": i + 1,
                    "msg": "selftest echo of " + data.decode("utf-8", "ignore"),
                }
            )
            await ws.send(reply)
        await asyncio.sleep(0.2)


async def serve(rounds, port, stop_after):
    rtts = []

    async def on_connect(ws):
        return await handle(ws, rounds)

    async with websockets.serve(on_connect, "127.0.0.1", port, max_size=8 * 1024 * 1024):
        log(f"listening on ws://127.0.0.1:{port}/s1 (websockets {websockets.__version__})")
        try:
            if stop_after is None:
                await asyncio.Event().wait()
            else:
                await asyncio.sleep(stop_after)
        except (KeyboardInterrupt, asyncio.CancelledError):
            log("shutting down")
    return rtts


async def run(args):
    if args.selftest:
        log("selftest: starting the peer with a loopback client")
        t0 = time.perf_counter()
        try:
            async with websockets.serve(
                lambda ws: handle(ws, args.rounds), "127.0.0.1", args.port, max_size=8 * 1024 * 1024
            ):
                log(f"selftest listening on ws://127.0.0.1:{args.port}/s1")
                await client_mimic(args.port, args.rounds)
        except Exception as ex:
            log(f"selftest failed: {ex!r}")
            write_results()
            return 1
        took = (time.perf_counter() - t0) * 1000.0
        log(f"selftest: {args.rounds} round trips in {took:.1f} ms")
        ok = any("reply in" in line for line in TRANSCRIPT)
        log("RESULT: selftest " + ("PASS" if ok else "FAIL"))
        write_results()
        return 0 if ok else 1

    await serve(args.rounds, args.port, None)
    return 0


def write_results():
    os.makedirs(RESULTS, exist_ok=True)
    path = os.path.join(RESULTS, "s1.txt")
    with open(path, "w") as f:
        f.write("\n".join(TRANSCRIPT) + "\n")
    log(f"results: {path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--port", type=int, default=9131)
    ap.add_argument("--selftest", action="store_true",
                    help="verify the peer against a loopback client that mimics the GH side")
    args = ap.parse_args()

    try:
        code = asyncio.run(run(args))
    except KeyboardInterrupt:
        code = 0
    sys.exit(code)


if __name__ == "__main__":
    main()
