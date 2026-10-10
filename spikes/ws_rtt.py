import asyncio, time, statistics, os
import websockets
async def echo(ws):
    async for msg in ws:
        await ws.send(msg)
async def main():
    async with websockets.serve(echo, "127.0.0.1", 0, max_size=None) as server:
        port = list(server.sockets)[0].getsockname()[1]
        async with websockets.connect(f"ws://127.0.0.1:{port}", max_size=None) as ws:
            small = b"x" * 120
            rts = []
            for _ in range(300):
                t = time.perf_counter(); await ws.send(small); await ws.recv(); rts.append((time.perf_counter() - t) * 1000)
            print(f"W1 small msg RTT ms: median {statistics.median(rts):.2f}, p95 {sorted(rts)[int(len(rts)*0.95)]:.2f}", flush=True)
            big = os.urandom(2_000_000)
            rts = []
            for _ in range(20):
                t = time.perf_counter(); await ws.send(big); await ws.recv(); rts.append((time.perf_counter() - t) * 1000)
            print(f"W2 2MB binary RTT ms: median {statistics.median(rts):.1f}", flush=True)
asyncio.run(main())
