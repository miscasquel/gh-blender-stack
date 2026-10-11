"""Spike S2b: what cadence can ``bpy.app.timers`` actually hold?

Run in the GUI (needs a window; quits Blender when done):

    blender --python spikes/s2_timer_cadence.py

Question: plan §9.4 wants incoming messages drained every 5 ms while any stage is
waiting. Blender's timer interval is a minimum; the real cadence is set by the main
event loop. This measures the achievable cadence while a background thread streams
2 MB frames through a websockets server, so the engine's latency budget uses a real
number.

The script registers timers and returns: timers only fire while the main thread is
back in the event loop (S2's first GUI attempt blocked it and measured 0 ticks).
"""

import asyncio
import os
import statistics
import sys
import threading
import time

import bpy

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "blender", "gh_stack", "vendor"))

GUI = not bpy.app.background
RUN_SECONDS = 3.0

LINES = []


def log(msg):
    print(f"[s2-timer] {msg}", flush=True)
    LINES.append(msg)


class Cadence:
    def __init__(self, name, interval):
        self.name = name
        self.interval = interval
        self.gaps = []
        self.last = None
        self.ticks = 0

    def tick(self):
        now = time.perf_counter()
        if self.last is not None:
            self.gaps.append((now - self.last) * 1000.0)
        self.last = now
        self.ticks += 1
        return self.interval

    def report(self):
        if not self.gaps:
            log(f"{self.name}: never ticked (0 gaps)")
            return
        # Drop the first gap: it spans the registration moment (window startup).
        steady = self.gaps[1:]
        over = [g for g in steady if g > 100.0]
        log(
            "%s: %d ticks over %.2f s, median %.2f ms, p95 %.2f ms, max %.2f ms, "
            "gaps > 100 ms after registration: %d (requested %.1f ms)"
            % (
                self.name,
                self.ticks,
                sum(self.gaps) / 1000.0,
                statistics.median(self.gaps),
                sorted(self.gaps)[int(len(self.gaps) * 0.95)],
                max(self.gaps),
                len(over),
                self.interval * 1000.0,
            )
        )
        for g in over[:5]:
            log(f"    {self.name}: outlier gap {g:.1f} ms")


async def server_main(port_holder, stop):
    from websockets.asyncio.server import serve

    async def handler(ws):
        try:
            while True:
                await ws.recv()
                await ws.send(b"ok")
        except Exception:
            pass

    async with serve(handler, "127.0.0.1", 0, max_size=None) as server:
        port_holder["port"] = server.sockets[0].getsockname()[1]
        while not stop["stop"]:
            await asyncio.sleep(0.02)


async def load_main(port_holder, stop, sent):
    import websockets

    big = os.urandom(2_000_000)
    while not stop["stop"]:
        try:
            async with websockets.connect(
                f"ws://127.0.0.1:{port_holder['port']}", max_size=None
            ) as ws:
                while not stop["stop"]:
                    await ws.send(big)
                    await ws.recv()
                    sent["count"] += 1
        except Exception:
            await asyncio.sleep(0.05)


def main():
    if not GUI:
        log("background mode: bpy.app.timers is inert under -b; nothing to measure here")
        log("(see s2_server_thread.py for that finding)")
        write = os.path.join(os.path.dirname(__file__), "results")
        os.makedirs(write, exist_ok=True)
        with open(os.path.join(write, "s2_timer.txt"), "w") as f:
            f.write("\n".join(LINES) + "\n")
        return

    fast = Cadence("timer@4ms", 0.004)
    slow = Cadence("timer@50ms", 0.05)
    counter = {"n": 0, "stop": False}
    sent = {"count": 0}
    port_holder = {}

    def fast_cb():
        return fast.tick()

    def slow_cb():
        return slow.tick()

    def stopper():
        # Fired once after RUN_SECONDS by the timer loop itself.
        if time.perf_counter() - START >= RUN_SECONDS:
            counter["stop"] = True
            fast.report()
            slow.report()
            log(f"background load: {sent['count']} 2 MB round trips during the measurement")
            for cb in (fast_cb, slow_cb, stopper):
                if bpy.app.timers.is_registered(cb):
                    bpy.app.timers.unregister(cb)
            out = os.path.join(os.path.dirname(__file__), "results")
            os.makedirs(out, exist_ok=True)
            with open(os.path.join(out, "s2_timer.txt"), "w") as f:
                f.write("\n".join(LINES) + "\n")
            log("results: spikes/results/s2_timer.txt")
            bpy.ops.wm.quit_blender()
            return None
        return 0.05

    START = time.perf_counter()

    threading.Thread(target=lambda: asyncio.run(server_main(port_holder, counter)), daemon=True).start()
    while "port" not in port_holder:
        time.sleep(0.005)
    threading.Thread(
        target=lambda: asyncio.run(load_main(port_holder, counter, sent)), daemon=True
    ).start()

    log(f"background load running: loopback client streaming 2 MB frames on port {port_holder['port']}")
    log(f"measuring timer cadence for {RUN_SECONDS:.0f} s")

    bpy.app.timers.register(fast_cb)
    bpy.app.timers.register(slow_cb)
    bpy.app.timers.register(stopper)
    log("registered; returning to Blender's event loop")


main()
