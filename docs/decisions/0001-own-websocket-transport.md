# 0001 — Own WebSocket protocol; Portal stays separate

**Status:** Accepted, 2026-10-10 (D1)

## Context

Driving Grasshopper definitions from Blender needs low latency, per-frame parameter changes for animation, binary geometry, and request/response correlation mid-stack. The old native track used files and polling, which cannot meet that.

Portal (`sean1832/Portal`, Apache-2.0) already provides WebSocket plumbing on both sides, and it is installed here. Measured against it:

- Portal's payload is gzip'd JSON with a CRC16 header. For a 100k-triangle mesh, encoding takes 74 ms and produces 3.8 MB. The binary path takes 15 ms to read and 1.8 MB.
- The local Portal.blender 0.3.0 defines curve enums but no curve serialization.
- Portal's message model has no parameter schema, no request IDs, and no stage semantics. Those are the core of this project.

## Decision

Build a small WebSocket-only protocol (`protocol.md`). Blender runs the server, GH runs the client. Portal stays installed and untouched for ad-hoc work. Its source may serve as a reference, with attribution if any code is copied.

## Consequences

- We own a protocol, a Blender server, and a GH client. Code size is modest and matches the data model.
- If spike S1 fails (the C# plugin cannot run under wine), the fallback is Portal's WebSocket components. The engine does not change; only the transport does.
- Portal and this project can run side by side on separate ports.
