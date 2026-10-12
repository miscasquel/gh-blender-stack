"""S1 pre-flight peer for MiniWsClient (spikes/s1/WsTest.exe).

    PYTHONPATH=blender/gh_stack/vendor python3 spikes/s1/wsecho.py [port]

Speaks the spike frame ([u32 little-endian length][JSON]) and checks the client
behaviours the live transport depends on:

1. It sends a WebSocket **ping** before anything else and requires a pong back.
   Blender's server pings every 2 s (protocol §2); a client that hands the ping
   payload to the frame decoder fails with "header length over the cap".
2. It sends a small frame and expects the echo.
3. It sends a frame over 64 KiB (the 127 length path) and expects the echo.
"""

import asyncio
import json
import struct
import sys

import websockets


def frame(obj):
    body = json.dumps(obj).encode("utf-8")
    return struct.pack("<I", len(body)) + body


async def handler(ws):
    print("[peer] connected from %s" % (ws.remote_address,), flush=True)

    pong = await ws.ping(b"gh-stack-ping")
    try:
        await asyncio.wait_for(pong, timeout=5)
        print("[peer] pong received", flush=True)
    except asyncio.TimeoutError:
        print("[peer] FAIL: no pong for the ping", flush=True)
        return

    await ws.send(frame({"type": "request", "n": 1, "text": "small"}))
    reply = await asyncio.wait_for(ws.recv(), timeout=10)
    print("[peer] small reply: %d bytes" % len(reply), flush=True)

    big = frame({"type": "request", "n": 2, "text": "big"}) + b"x" * 200000
    await ws.send(big)
    reply2 = await asyncio.wait_for(ws.recv(), timeout=10)
    print("[peer] big reply: %d bytes" % len(reply2), flush=True)

    print("[peer] RESULT: peer PASS", flush=True)


async def main(port):
    async with websockets.serve(handler, "127.0.0.1", port, max_size=8 * 1024 * 1024):
        print("[peer] listening on ws://127.0.0.1:%d/s1test" % port, flush=True)
        await asyncio.Future()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9132
    try:
        asyncio.run(main(port))
    except KeyboardInterrupt:
        pass
