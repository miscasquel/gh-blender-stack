# 0008 — Simulation-style frame cache, no bake operator

**Status:** Accepted, 2026-10-12. Supersedes the plan's bake operator
(`docs/plan.md` rev 2, §7 "Bake", §9.8 "Bake range").

## Context

The plan carried an explicit **Bake** operator: a frame range, a progress
dialog, and a cache filled ahead of playback. The 2026-10-12 review asked for
Geometry-Nodes-simulation behavior instead: the cache fills as the user plays
and scrubs, with no bake setup and no start/end frame fields; it resets when
Grasshopper sends an update; idle must not tag the depsgraph (Cycles could not
accumulate more than one viewport sample); and renders use cached frames.
The design was worked out in the frame-cache handoff (deleted 2026-10-12; its
substance lives in this ADR and in the polish plan's P4 items).

## Decision

- **The content-addressed response store is the frame cache.** Keyframed
  parameters produce a different request id (`rid`) per frame, so no
  frame-to-rid map exists; frames replay in any order, and Blender's own frame
  range is the only notion of range.
- **Two layers.** The memory dict is the hot path (bounded by the same byte
  budget); the disk store under the data root is the backing store. A frame is
  the raw protocol frame as received plus a JSON sidecar (generation,
  definition fingerprint, out hash), written through a temp file and a rename.
- **Per-stage generation resets.** A same-`rid` answer with a different
  `out_hash` (a Grasshopper-side edit), a schema change, a reconnect with a
  different document, or a manual Push bumps the stage's generation and deletes
  its frames. Frames are valid only under the current generation *and* the
  current definition fingerprint (protocol version + schema signature). The
  document name is deliberately not in the fingerprint so offline replay works;
  a document change is caught by the generation bump on `hello`.
- **Reconnect and reload keep frames when the fingerprint matches** — the
  handoff's open question 2, answered yes: reopening a `.blend` reuses the disk
  cache and deletes only what mismatches.
- **Playback never blocks.** Uncached frames keep the last good output; one
  request is in flight per stage (Blender-like coalescing to the playhead). A
  response for a rid the scene has moved past is cached, never displayed.
- **Cached frames replay with Grasshopper closed**, so renders work from the
  cache. The cache lookup happens before the connection check.
- **Idle stability.** Engine writes are change-gated; the prefix evaluation is
  memoized against a revision (base geometry hash, modifier settings, upstream
  applied outputs, shape keys), so a pass that changes nothing does not clone,
  write, or tag the depsgraph. This is what lets Cycles accumulate samples.
- **Preferences:** data root (`~/.local/share/gh-stack/`) and cache size limit
  (256 MB, per stack object and stage) become real.
- Push resets the stage's cache (handoff open question 1, answered yes).

Out: the Bake operator, manual frame ranges, cache management UI, the
per-stage cache readout (the state row already reads "waiting"). The disk format
is private and has no stability promise.

## Consequences

- Acceptance A1 changes from "after a bake, playback is served from the cache"
  to "after the cache is warm, playback is served from the cache".
- Frame-keyed bake output no longer exists; content addressing makes stale
  frames stop matching instead of being invalidated explicitly.
- A frame that Grasshopper changed without touching Blender's inputs is only
  detected when the same rid is answered twice with different output. A
  definition change that is never re-answered for a cached rid can leave stale
  frames until the next schema/document/reset event (documented limitation).
- The prefix memo walks node groups recursively (identity-keyed, depth-capped;
  hitting the cap disables the memo rather than guessing), so an edit inside a
  *nested* group invalidates the revision like any other.
- Layer B grows the frame-cache suite (progressive fill, replay, both reset
  paths, quiet no-op and idle, eviction, uncached-frame behavior).
