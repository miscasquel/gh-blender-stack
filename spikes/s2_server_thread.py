"""Spike S2: does ``websockets`` run in a Blender background thread?

Run headless:

    blender -b --factory-startup --python-exit-code 1 --python spikes/s2_server_thread.py

Run in the GUI (needs a window; the spike quits Blender when done):

    blender --python spikes/s2_server_thread.py

What is checked
1. ``websockets`` 17.2 (vendored at blender/gh_stack/vendor) imports and runs on Blender 5.2's Python 3.14.7.
2. An asyncio WebSocket server runs on a plain background thread with its own event loop.
3. A loopback client (own thread) round-trips small and large binary frames through it.
4. The server background thread hands work to the main thread through a thread-safe queue.
5. In the GUI, ``bpy.app.timers`` drains that queue on the main thread and holds a steady
   cadence while a 2 MB frame is in flight on the background thread, i.e. no UI stall.
6. Everything shuts down cleanly, so Blender exits on its own.

Findings recorded here, because they shape the engine (docs/plan.md §9.4, §14):

- ``bpy.app.timers`` does not tick while a Python script is blocking the main thread. The
  GUI run must register a timer and return; the timer then drives the wait. Blocking the
  main thread starves the event loop: this script's first GUI attempt measured 0 ticks.
- ``bpy.app.timers`` is inert in ``blender -b`` background mode. Layer B tests must call the
  same drain function the timer calls, directly, rather than waiting on timers.

Results are written to spikes/results/s2.txt.
"""

import asyncio
import os
import queue
import statistics
import sys
import threading
import time

import bpy

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "blender", "gh_stack", "vendor"))

import websockets  # noqa: E402
from websockets.asyncio.server import serve  # noqa: E402

GUI = not bpy.app.background

PORT_HOLDER = {"port": None}
SERVER_READY = threading.Event()
STOP_SERVER = threading.Event()

INBOX = queue.Queue()
INBOX_STATS = {"count": 0, "max_handoff_ms": 0.0}

TIMER_STATS = {"ticks": 0, "max_gap_ms": 0.0, "last": None, "gaps": []}

EXPECTED_MESSAGES = 401  # 200 small + 200 stress small + 1 big
TIMER_INTERVAL = 0.004
TIMER_GIVEUP_TICKS = 10000  # 40 s at 4 ms


def log(msg):
    print(f"[s2] {msg}", flush=True)


async def server_main():
    async with serve(on_connect, "127.0.0.1", 0, max_size=None) as server:
        PORT_HOLDER["port"] = server.sockets[0].getsockname()[1]
        SERVER_READY.set()
        log(f"server listening on 127.0.0.1:{PORT_HOLDER['port']} (websockets {websockets.__version__})")
        while not STOP_SERVER.is_set():
            await asyncio.sleep(0.02)


async def on_connect(ws):
    """Echo server: every message is queued for the main thread, then echoed."""
    try:
        while True:
            msg = await ws.recv()
            enqueued_at = time.perf_counter()
            INBOX.put((enqueued_at, len(msg)))
            await ws.send(b"ack:%d" % len(msg))
    except websockets.exceptions.ConnectionClosed:
        pass  # client closed; nothing to report


def server_thread_main():
    asyncio.run(server_main())


def drain_inbox():
    """Main-thread queue drain. Timer callback in the GUI, direct call in the tests."""
    now = time.perf_counter()
    while True:
        try:
            enqueued_at, size = INBOX.get_nowait()
        except queue.Empty:
            break
        handoff_ms = (now - enqueued_at) * 1000.0
        INBOX_STATS["count"] += 1
        INBOX_STATS["max_handoff_ms"] = max(INBOX_STATS["max_handoff_ms"], handoff_ms)


def note_tick(now):
    last = TIMER_STATS["last"]
    if last is not None:
        gap_ms = (now - last) * 1000.0
        TIMER_STATS["gaps"].append(gap_ms)
        TIMER_STATS["max_gap_ms"] = max(TIMER_STATS["max_gap_ms"], gap_ms)
    TIMER_STATS["last"] = now
    TIMER_STATS["ticks"] += 1


async def client_main(rtts, stress_result):
    async with websockets.connect(f"ws://127.0.0.1:{PORT_HOLDER['port']}", max_size=None) as ws:
        # Phase 1: 200 small round trips.
        for i in range(200):
            t0 = time.perf_counter()
            await ws.send(b"m%d" % i)
            await ws.recv()
            rtts.append(time.perf_counter() - t0)

        # Phase 2: one 2 MB frame back-to-back with 200 small frames, so the
        # background server thread is busy while the main thread keeps running.
        gap_before_stress = TIMER_STATS["max_gap_ms"]
        big = os.urandom(2_000_000)
        t0 = time.perf_counter()

        async def send_big():
            await ws.send(big)

        async def send_small():
            for i in range(200):
                await ws.send(b"s%d" % i)

        async def recv_all():
            for _ in range(EXPECTED_MESSAGES - 200):
                await ws.recv()

        await asyncio.gather(send_big(), send_small(), recv_all())
        stress_ms = (time.perf_counter() - t0) * 1000.0
        stress_result.update(
            {
                "ms": stress_ms,
                "max_gap_before_stress_ms": gap_before_stress,
                "max_gap_during_stress_ms": TIMER_STATS["max_gap_ms"],
            }
        )
        await asyncio.sleep(1.0)  # let the cadence accumulate and the queue drain


def client_thread_main(rtts, stress_result):
    asyncio.run(client_main(rtts, stress_result))


LINES = []
FAILURES = []


def record(msg, fail=False):
    log(msg)
    LINES.append(msg)
    if fail:
        FAILURES.append(msg)


def write_results():
    out = os.path.join(os.path.dirname(__file__), "results")
    os.makedirs(out, exist_ok=True)
    name = "s2-gui.txt" if GUI else "s2-background.txt"
    path = os.path.join(out, name)
    with open(path, "w") as f:
        f.write("\n".join(LINES) + "\n")
    return path


def report_transport(rtts, stress_result):
    if rtts:
        rtt_ms = [r * 1000.0 for r in rtts]
        record(
            "small-frame RTT ms: median %.2f, mean %.2f, p95 %.2f, max %.2f (n=%d)"
            % (
                statistics.median(rtt_ms),
                statistics.fmean(rtt_ms),
                sorted(rtt_ms)[int(len(rtt_ms) * 0.95)],
                max(rtt_ms),
                len(rtt_ms),
            )
        )
        if statistics.median(rtt_ms) > 5.0:
            record("FAIL: median small-frame RTT above 5 ms", fail=True)
    else:
        record("FAIL: no small-frame round trips completed", fail=True)

    if stress_result:
        record("2 MB frame + 200 small back-to-back: %.1f ms wall" % stress_result["ms"])
        if stress_result["ms"] > 5000.0:
            record("FAIL: 2 MB stress phase took over 5 s", fail=True)
    else:
        record("FAIL: stress phase did not run", fail=True)

    record(
        "queue handoff: %d messages drained, max enqueue-to-apply %.2f ms"
        % (INBOX_STATS["count"], INBOX_STATS["max_handoff_ms"])
    )
    if INBOX_STATS["count"] != EXPECTED_MESSAGES:
        record(f"FAIL: expected {EXPECTED_MESSAGES} messages, drained {INBOX_STATS['count']}", fail=True)


def report_timer_cadence(stress_result):
    if TIMER_STATS["ticks"] > 20:
        gaps = TIMER_STATS["gaps"]
        record(
            "bpy.app.timers cadence (4 ms requested): %d ticks, median gap %.2f ms, "
            "p95 %.2f ms, max %.2f ms"
            % (
                TIMER_STATS["ticks"],
                statistics.median(gaps),
                sorted(gaps)[int(len(gaps) * 0.95)],
                TIMER_STATS["max_gap_ms"],
            )
        )
        stress_gap = stress_result.get("max_gap_during_stress_ms", 0.0)
        record(
            "timer max gap during 2 MB stress: %.2f ms (before stress: %.2f ms)"
            % (stress_gap, stress_result.get("max_gap_before_stress_ms", 0.0))
        )
        if stress_gap > 500.0:
            record("FAIL: main thread stalled over 500 ms during the 2 MB transfer", fail=True)
    else:
        record(
            f"FAIL: timer only ticked {TIMER_STATS['ticks']} times; main-thread event loop "
            "was not free to run timers",
            fail=True,
        )


def finalize(rtts, stress_result):
    report_transport(rtts, stress_result)
    if GUI:
        report_timer_cadence(stress_result)
    else:
        record("timer cadence not measurable under -b; covered by the GUI run of this spike")

    if GUI:
        bpy.app.timers.unregister(timer_tick)
    STOP_SERVER.set()
    SERVER_THREAD.join(timeout=5.0)
    if SERVER_THREAD.is_alive():
        record("FAIL: server thread did not stop", fail=True)
    else:
        record("server thread stopped cleanly; Blender can exit")

    if FAILURES:
        record(f"RESULT: S2 FAIL ({len(FAILURES)} check(s))")
    else:
        record("RESULT: S2 PASS")
    path = write_results()
    log(f"results: {path}")


def pump_until_done():
    """Background-mode path: drain from the main thread while the client runs."""
    deadline = time.time() + TIMER_GIVEUP_TICKS * TIMER_INTERVAL
    while time.time() < deadline:
        drain_inbox()
        if INBOX_STATS["count"] >= EXPECTED_MESSAGES and stress_result and not CLIENT_THREAD.is_alive():
            break
        time.sleep(0.002)
    CLIENT_THREAD.join(timeout=5.0)
    drain_inbox()
    finalize(rtts, stress_result)


def timer_tick():
    """GUI path. Registered on bpy.app.timers, so Blender's event loop calls it."""
    drain_inbox()
    note_tick(time.perf_counter())
    if TIMER_STATS["ticks"] >= TIMER_GIVEUP_TICKS:
        record("FAIL: GUI timer give-up reached before the client finished", fail=True)
        finalize(rtts, stress_result)
        bpy.ops.wm.quit_blender()
        return None
    if (
        INBOX_STATS["count"] >= EXPECTED_MESSAGES
        and stress_result
        and not CLIENT_THREAD.is_alive()
    ):
        finalize(rtts, stress_result)
        bpy.ops.wm.quit_blender()
        return None
    return TIMER_INTERVAL


def main():
    global SERVER_THREAD, CLIENT_THREAD, rtts, stress_result

    mode = "GUI" if GUI else "background (-b)"
    record(f"mode: {mode}; Blender {bpy.app.version_string}; websockets {websockets.__version__}")

    SERVER_THREAD = threading.Thread(target=server_thread_main, name="s2-server", daemon=True)
    SERVER_THREAD.start()
    if not SERVER_READY.wait(10.0):
        record("FAIL: server did not start in 10 s", fail=True)
        write_results()
        sys.exit(1)
    record("server thread started; queue.Queue created")

    rtts = []
    stress_result = {}
    CLIENT_THREAD = threading.Thread(target=client_thread_main, args=(rtts, stress_result), name="s2-client")
    CLIENT_THREAD.start()

    if GUI:
        record("registering bpy.app.timers drain (4 ms); returning to Blender's event loop")
        bpy.app.timers.register(timer_tick)
        # Return here: timers only fire when the main thread is back in the event loop.
    else:
        record("bpy.app.timers inert under -b; draining from the main thread directly")
        record("FINDING: layer B tests call drain_inbox() directly instead of waiting on timers")
        pump_until_done()
        if FAILURES:
            sys.exit(1)


main()
