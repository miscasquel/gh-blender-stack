# 0016 — Live-only stages; native bake is the offline path

**Status:** Accepted, 2026-11-16. Supersedes
[0008](0008-simulation-style-frame-cache.md) and amends
[0009](0009-playback-sync-and-native-stage-bake.md),
[0010](0010-selective-stage-solves-and-live-generators.md) and
[0014](0014-bake-freezes-upstream.md): the custom frame cache and the `Live`
toggle are removed.

## Context

ADR 0008 made the content-addressed frame cache the playback path: a response
was stored by request id and replayed later, including with Grasshopper closed.
ADR 0009 added Play Every Frame and a native Bake node, but kept the frame cache
as the default path. ADR 0010 then added a per-stage `Live` flag so a stateful
solver could bypass the cache.

That left two overlapping offline mechanisms and a stage-level mode whose only
job was to choose between them. The cache carried its own generation counters,
definition fingerprints, disk store, eviction, reset triggers, hot-cache
accounting and replay failure modes. In practice the flag was almost always on:
a new stage cannot know whether its graph is deterministic, and replaying a
solver step is wrong. The cache's complexity outweighed its value.

## Decision

- **Every stage is live.** A delivered request is a solver step. A pass asks
  again when the stage's own request id or the scene frame moves, or when a
  reconnect, load, `unknown_ref` or timeout arms a new ask. A dirty event caused
  by another stage does not advance it.
- **One request in flight.** The engine keeps one outstanding request per stage,
  coalesces edits while it waits, and supersedes it after the soft retry or the
  hard timeout. Latest-wins remains the rule.
- **Late answers are dropped.** A response is applied only when its `rid` is the
  stage's current request. An answer for a superseded request is neither applied
  nor stored anywhere; it may only consume the pending entry it names.
- **Freeze is the manual hold.** Freeze stops requests and keeps the last
  applied output; Pull asks once while frozen. Unfreezing asks again.
- **The native Bake node is the offline path.** Bake stage steps the scene range
  through the same gate, writes the stage's Geometry Nodes Bake node and freezes
  the stage plus its upstream chain. Clear Bake removes the bake and resumes live
  output. There is no custom cache or replay.
- **No second cache.** A simulation-zone cache remains deferred R&D; if adopted
  it must replace the native bake rather than run beside it.
- **Storage and identity.** The data root keeps logs only. `stack_id` remains the
  stage's session identity for pending/sent/verify bookkeeping, not a cache
  namespace. `geom_hash` still drives full/ref requests and the prefix memo;
  `applied_out_hash` still skips an identical apply.

## Consequences

- A delivered request is the only way a stage advances. Scrubbing with
  Grasshopper disconnected keeps the last output; use Freeze or Bake for offline
  frames and renders.
- No disk cache means no cache corruption, eviction, sidecar, generation,
  definition-fingerprint or replay-failure class of bug.
- A stateful stage no longer needs a mode switch: every stage behaves like the
  former live mode. A deterministic stage also asks per frame, which is the cost
  of removing the mode.
- Opening a 0.6 project drops the removed `live`, `live_frame` and `generation`
  RNA fields; Blender ignores unknown properties. The old cache directory is
  disposable and can be left in place.
- `docs/protocol.md`'s request/response semantics are unchanged. Only the local
  decision to store or replay a response is gone.
