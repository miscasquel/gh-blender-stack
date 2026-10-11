"""S3 driver: geometry round trip against the C# side under wine.

    PYTHONPATH=blender/gh_stack/vendor python3 spikes/s3/driver.py [--check-s1]

- Generates grids at 10k and 100k triangles with numpy (the same generator the
  Blender tests use, so the sections are comparable).
- Sends each as a protocol `request` frame with `geom.mode = "full"` to the GH
  spike, which converts the sections into a Rhino Mesh and back.
- Decodes the reply, verifies the geometry matches, and reports the C# timings
  (from the reply header) plus the frame sizes and round-trip time.
- `--check-s1` first sends a section-less request and checks the S1 reply, so one
  session re-validates both paths.
- Writes the transcript to spikes/results/s3.txt.

The Blender-side numbers live in spikes/results/... (see the headless tests,
scripts/test-blender); this driver measures the C# conversion and the transport.
"""

import argparse
import asyncio
import json
import os
import struct
import sys
import time

import numpy as np

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
VENDOR = os.path.join(REPO_ROOT, "blender", "gh_stack", "vendor")
if VENDOR not in sys.path:
    sys.path.insert(0, VENDOR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import websockets  # noqa: E402
from blender.gh_stack.core import codec, hashing  # noqa: E402

RESULTS = os.path.join(REPO_ROOT, "spikes", "results")
TRANSCRIPT = []


def log(msg):
    print(f"[s3] {msg}", flush=True)
    TRANSCRIPT.append(msg)


def grid_arrays(nx, ny, size=10.0):
    """An (nx-1) x (ny-1) quad grid: (nx-1)*(ny-1)*2 triangles."""
    xs = np.linspace(0.0, size, nx, dtype=np.float32)
    ys = np.linspace(0.0, size, ny, dtype=np.float32)
    gx, gy = np.meshgrid(xs, ys, indexing="ij")
    verts = np.stack(
        [gx.reshape(-1), gy.reshape(-1), np.zeros(nx * ny, dtype=np.float32)], axis=1
    )
    faces = []
    for i in range(nx - 1):
        for j in range(ny - 1):
            a = i * ny + j
            b = a + 1
            c = a + ny
            d = c + 1
            faces.append((a, c, d, b))
    counts = np.full(len(faces), 4, dtype=np.uint32)
    indices = np.array([v for f in faces for v in f], dtype=np.uint32)
    return verts, counts, indices


def request_frame(stage, rid, verts, counts, indices, colors=None, as_subd=False):
    prefix = "subd" if as_subd else "mesh"
    sections = [
        (prefix + ".verts", "f32", verts),
        (prefix + ".face_counts", "u32", counts),
        (prefix + ".face_indices", "u32", indices),
    ]
    if colors is not None and not as_subd:
        sections.append(("mesh.colors", "u8", colors))
    header = {
        "type": "request",
        "stage": stage,
        "rid": rid,
        "nonce": 0,
        "params": {},
        "geom": {"mode": "full", "hash": "sha256:" + "0" * 64, "subd": as_subd},
        "units": "m",
        "space": "stack-local",
    }
    return codec.encode_frame(header, sections), sections


def sections_of(decoded):
    """Rebuild (name, dtype, array) tuples from decoded sections in header order."""
    out = []
    for entry in decoded[0].get("sections", []):
        out.append((entry["name"], entry["dtype"], decoded[1][entry["name"]]))
    return out


def as_arrays(sections):
    """Sections to numpy arrays with their protocol shapes (verts N x 3,
    colours N x 4, the rest flat). Decoded tuples arrive flat."""
    by = {}
    for name, dtype, values in sections:
        if dtype == "u8":
            arr = np.asarray(values, dtype=np.uint8)
            if name.endswith(".colors"):
                arr = arr.reshape(-1, 4)
        elif dtype == "u32":
            arr = np.asarray(values, dtype=np.uint32)
            if name.endswith(".crease_edges"):
                arr = arr.reshape(-1, 2)
        else:
            arr = np.asarray(values, dtype=np.float32)
            if name.endswith(".verts"):
                arr = arr.reshape(-1, 3)
        by[name] = arr
    return by


def verify(prefix, sent, got):
    """Compare the sent sections against the reply's. Returns a list of problems."""
    problems = []
    for key, dtype in (("verts", "f32"), ("face_counts", "u32"), ("face_indices", "u32")):
        name = f"{prefix}.{key}"
        if name not in got:
            problems.append(f"missing {name}")
            continue
        a = sent[name]
        b = got[name]
        if a.shape != b.shape:
            problems.append(f"{name}: shape {a.shape} -> {b.shape}")
            continue
        if dtype == "f32":
            max_diff = float(np.max(np.abs(a - b))) if a.size else 0.0
            if max_diff > 1e-4:
                problems.append(f"{name}: max diff {max_diff:g}")
        else:
            if not np.array_equal(a, b):
                problems.append(f"{name}: values differ")
    return problems


async def round_trip(ws, stage, frame, timeout=60.0):
    t0 = time.perf_counter()
    await ws.send(frame)
    reply = await asyncio.wait_for(ws.recv(), timeout=timeout)
    rtt_ms = (time.perf_counter() - t0) * 1000.0
    return reply, rtt_ms


async def check_s1(ws):
    log("S1 path check: section-less request")
    frame = codec.encode_frame({"type": "request", "n": 42, "text": "s3 driver s1 check"})
    reply, rtt_ms = await round_trip(ws, "s1-check", frame, timeout=40.0)
    header, sections = codec.decode_frame(reply)
    log(f"  reply in {rtt_ms:.1f} ms: {json.dumps(header, sort_keys=True)}")
    ok = (
        header.get("type") == "response"
        and header.get("n") == 42
        and isinstance(header.get("solves"), int)
    )
    log("  S1 path " + ("PASS" if ok else "FAIL"))
    return ok


async def run_case(ws, name, nx, ny, colors=False):
    verts, counts, indices = grid_arrays(nx, ny)
    tris = (nx - 1) * (ny - 1) * 2
    color_data = None
    if colors:
        color_data = np.random.randint(0, 256, size=(len(verts), 4), dtype=np.uint8)
    rid = "sha256:" + hashing.geometry_hash(
        {f"mesh.{k}": v for k, v in (("verts", verts), ("face_counts", counts), ("face_indices", indices))}
    )[7:39]
    frame, sent_sections = request_frame(f"grid-{name}", rid, verts, counts, indices, color_data)
    sent = as_arrays(sent_sections)
    log(f"{name}: {tris} tris, {len(verts)} verts, request frame {len(frame) / 1e6:.2f} MB")

    reply, rtt_ms = await round_trip(ws, name, frame)
    header, decoded = codec.decode_frame(reply)
    got = as_arrays(sections_of((header, decoded)))

    status = header.get("status")
    if status != "ok":
        log(f"  FAIL: status={status} error={header.get('error')}")
        return False

    counts_hdr = header.get("counts", {})
    timings = header.get("timings", {})
    log(
        "  reply %.1f ms rtt; C# in %.1f ms, out %.1f ms; %s verts, %s faces; "
        "reply %.2f MB; scale=%s"
        % (
            rtt_ms,
            timings.get("in_ms", -1),
            timings.get("out_ms", -1),
            counts_hdr.get("verts"),
            counts_hdr.get("faces"),
            len(reply) / 1e6,
            header.get("scale"),
        )
    )

    problems = verify("mesh", sent, got)
    if colors:
        if "mesh.colors" not in got:
            problems.append("colors missing in reply")
        elif not np.array_equal(sent["mesh.colors"], got["mesh.colors"]):
            diff = int(np.abs(sent["mesh.colors"].astype(int) - got["mesh.colors"].astype(int)).max())
            problems.append(f"colors differ (max {diff})")
    if problems:
        for p in problems:
            log("  FAIL: " + p)
        return False
    log("  PASS: geometry matches")
    return True


async def session(ws, args):
    """Runs on the connection from the GH spike (Blender is the server side)."""
    log(f"connection from {ws.remote_address}")
    ok = True
    if args.check_s1:
        ok = (await check_s1(ws)) and ok
    for case in [c.strip() for c in args.cases.split(",") if c.strip()]:
        if case not in CASES:
            log(f"unknown case {case}")
            continue
        nx, ny = CASES[case]
        ok = (await run_case(ws, case, nx, ny, colors=(case != "100k"))) and ok
    log("RESULT: " + ("PASS" if ok else "FAIL"))
    return ok


CASES = {
    "10k": (72, 72),
    "100k": (225, 225),
    "1k": (23, 23),
}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9131)
    ap.add_argument("--check-s1", action="store_true")
    ap.add_argument("--cases", default="10k,100k")
    args = ap.parse_args()

    outcome = {"ok": False, "done": False, "running": False}

    async def handler(ws):
        if outcome["running"]:
            log(f"extra connection from {ws.remote_address} ignored (one session at a time)")
            return
        outcome["running"] = True
        try:
            outcome["ok"] = await session(ws, args)
        except Exception as ex:
            log(f"session failed: {ex!r}")
        finally:
            outcome["running"] = False
            outcome["done"] = True

    async with websockets.serve(handler, "127.0.0.1", args.port, max_size=256 * 1024 * 1024):
        log(f"listening on ws://127.0.0.1:{args.port}/s1 (websockets {websockets.__version__})")
        log("waiting for the GH spike to connect; drop the components if nothing happens")
        deadline = time.time() + 300
        while not outcome["done"] and time.time() < deadline:
            await asyncio.sleep(0.2)
        if not outcome["done"]:
            log("RESULT: FAIL (no connection within 5 minutes)")

    os.makedirs(RESULTS, exist_ok=True)
    with open(os.path.join(RESULTS, "s3.txt"), "w") as f:
        f.write("\n".join(TRANSCRIPT) + "\n")
    log("results: spikes/results/s3.txt")
    return 0 if outcome["ok"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

