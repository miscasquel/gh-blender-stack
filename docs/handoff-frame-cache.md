# Handoff — simulation-style frame cache (W2, redefined)

**Status:** implemented 2026-10-12 (W2). Supersedes the plan's explicit bake
operator (`docs/plan.md` §7 "Bake", §9.8 "Bake range", §13). Written 2026-10-12
after the W1 prefix-evaluation work landed (`40f68bc`); the decisions it left
open are settled in ADR 0008 and the deviations are recorded there: no UI cache
readout, the document is handled by the generation bump rather than the
fingerprint, the memory cache is bounded by the same budget, and the prefix is
memoized against a content revision instead of a scratch object.

**Owner's requirements (from the 2026-10-12 review):**

1. The cache must behave like **Geometry Nodes simulation caches in the timeline**:
   it fills *as the user plays and scrubs*, with no bake setup, no start/end frame
   fields. Blender's own frame range is the only notion of range.
2. The cache **resets when Grasshopper sends an update** — a changed definition,
   a slider moved upstream of Stage Out, a schema change, a reconnected document.
   Frames cached against the old definition must not replay.
3. **Idle stability:** when nothing changed, the add-on must not write to the scene
   or tag the depsgraph. Today Cycles cannot accumulate more than one viewport
   sample because something keeps updating, even with no playback. A cached frame
   that is already applied must be a complete no-op.
4. Renders use cached frames; uncached frames keep the last good output (playback
   never blocks). Filling the cache is done by playing/scrubbing first, exactly
   like a simulation.

**Non-goals:** no bake operator UI, no manual frame ranges, no cache management
window, no cross-machine sharing, no promise about disk format stability yet.

---

## Where things stand (context for the implementer)

- **Engine** (`blender/gh_stack/blender/engine.py`):
  - Per stage: prefix sections (W1: clone evaluation when anything precedes the
    stage's modifier; base datablocks otherwise) → geometry hash → `rid`
    (`hashing.request_id`, content-addressed over params + geometry + nonce) →
    in-memory cache lookup → apply or send.
  - `_cache = {(stack_id, stage_key): {rid: (header, sections)}}`, session-only.
  - Responses: `_handle_response` applies, caches, and sets state/warnings;
    `unknown_ref` triggers a full resend (§4.3); a client `hello` re-sends waiting
    requests (§8.8). Both landed with W1.
  - Stop-on-waiting: a stage in `waiting` breaks the pass, so later stages never
    run on a stale prefix.
  - The depsgraph handler marks every stack dirty on any update; an `_evaluating`
    flag suppresses marking during prefix clone evaluation (clone link/unlink tags
    the depsgraph).
- **Pure cache index** (`blender/gh_stack/core/cache.py`): `CacheIndex` with the
  disk layout `cache/<stack_id>/<stage_key>/<rid>.bin`, byte budget, FIFO eviction
  with `touch`. Tested in layer A. **Unused by the engine today.**
- **Prefs** (`blender/gh_stack/blender/prefs.py`): port, timeout, log level only.
  The plan wants data root, cache size limit, curve deviation threshold (§11).
- **Tests:** 50 core, 54 headless Blender (`blender/tests/headless/`, incl.
  `test_prefix.py`), 10 golden fixtures, installed-extension round trip.
- **Numbers:** prefix evaluation 4.3 ms at 10k triangles, 50.9 ms at 100k.

---

## Design

### Two layers, one key

1. **Response store (rid → response).** Content-addressed, exactly what the engine
   already uses in memory. Because keyframed parameters produce a different `rid`
   per frame, **the rid store *is* the frame cache** — no separate frame→rid map is
   needed. A frame whose rid is present applies instantly; a frame that maps to the
   same rid as another frame (no animation) shares one entry.
2. **Generation per stage.** A counter (plus a definition fingerprint) that is
   bumped whenever Grasshopper sends something that makes previously cached frames
   describe the wrong definition. Entries from older generations are ignored and
   deleted. This is the "simulation cache resets when the node tree changes" half.

### What counts as a Grasshopper update (generation bump)

- A response whose `rid` equals the stage's current `rid` but whose `out_hash`
  differs from what was last applied/stored for that rid — a GH-side change
  (slider upstream of Stage Out, graph edit that does not touch Blender's inputs).
- A `schema` message whose signature differs (param declarations changed). The
  engine already compares signatures before touching the UI; extend that to a
  generation bump.
- A new `hello` / reconnect where the document may have lost state. Treat any
  client (re)connect as a reset for stages it claims (conservative; the plan's
  §8.8 already re-sends requests on hello).
- Manual **Push** (owner decision pending): simplest is to bump generations too —
  Push exists to force a fresh GH answer.

Blender-side changes (parameter edits, base geometry edits, upstream stage output
changes) need **no reset**: they change `rid`s, so old entries simply stop being
looked up. They age out through eviction.

### Persistence and validity across sessions

Store each response as the **raw protocol frame as received** (the codec
round-trips frames byte-for-byte; decode is ~7 ms at 100k triangles, only on a
disk hit). Each entry also carries a small header file (or a JSON sidecar) with:
- `definition`: a fingerprint of the GH-side definition that produced it — the
  stage's schema signature + the document name + protocol version. On load, an
  entry is only valid if the fingerprint matches the current one; otherwise it is
  deleted. This makes a re-opened session reuse frames when the definition is
  unchanged, and drop them when it is not.

### Progressive filling during playback

Nothing new is needed in the playback path: frame change → engine pass → compute
`rid` → memory hit → disk hit → apply; miss → send request (latest-wins still
holds) → store on arrival. The disk write happens off the critical path (same
place the response is cached today). The UI can show a per-stage readout like
"cache 42 frames" if useful; optional.

### Idle stability (the Cycles half)

Every write below must become change-gated; today several happen on every pass and
each one tags the depsgraph, which restarts Cycles accumulation:

1. `nodes.set_modifier_visible` is called for every enabled stage on every pass and
   writes `show_viewport`/`show_render` even when unchanged → only write when the
   value differs.
2. `_handle_response` writes `stage.warnings`, `stage.state`, `stage.error` and
   calls `obj.update_tag()` even when `out_hash == applied_out_hash` (a no-op
   response, which a heartbeat or a repeated GH solve produces) → only assign
   changed values and never `update_tag()` when nothing was applied.
3. Prefix clone link/unlink tags the depsgraph on every pass that has a stage with
   predecessors. Add a **stack revision** per object (modifier list + enabled
   flags + base data version + upstream `applied_out_hash`es + current params) and
   cache the prefix sections per stage; when the revision is unchanged, skip the
   clone entirely. A persistent scratch object linked once (never linked/unlinked
   per pass) is the fallback if a clone is unavoidable.
4. Verify in the GUI with Cycles: no viewport redraws while idle, samples
   accumulate. This is an acceptance check, not just a test.

### Eviction and prefs

- Add prefs: **data root** (default `~/.local/share/gh-stack/`) and **cache size
  limit** (default 256 MB; `core/cache.py` already uses that number).
- `CacheIndex` handles the byte budget and eviction; wire it in (memory dict stays
  the hot path, disk is the backing store). Keep FIFO-with-touch unless strict LRU
  is wanted.
- Prune a stage's entries on generation bump.

---

## Files to touch

| File | Change |
|---|---|
| `blender/gh_stack/blender/engine.py` | disk read-through, generation logic, change-gated writes, stack revision/prefix caching, eviction wiring |
| `blender/gh_stack/blender/prefs.py` | data root, cache size limit |
| `blender/gh_stack/core/cache.py` | generation/fingerprint fields if they belong in the pure index; keep it bpy-free |
| `blender/gh_stack/blender/ui.py` | optional cache readout; "updating" indicator for uncached frames |
| `blender/tests/headless/test_frame_cache.py` (new) | see below |
| `blender/tests/core/test_core.py` | index/eviction/generation unit tests |
| `docs/plan.md` | §7/§9.8/§13 rewritten around the simulation-style cache (and an ADR for the decision) |

## Tests

Layer B (fake_gh, headless):

1. **Progressive fill:** scrub frames 1–5 with a keyframed parameter; assert five
   distinct rids were requested and five entries exist (memory + disk).
2. **Replay:** stop the fake (or disconnect), clear the memory dict, scrub again;
   every frame applies from disk with no request sent.
3. **Reset on GH update:** send a response with the same rid but a different
   `out_hash` (a fake mode), then assert the stage's entries were pruned and the
   next scrub re-requests instead of replaying.
4. **Schema change resets** the same way.
5. **No-op quiet:** after settle, a repeated identical response must not tag the
   object or write properties (watch a depsgraph counter / data version).
6. **Idle quiet:** a pass with an unchanged stack revision performs no clone
   evaluation and no writes.
7. **Eviction:** a tiny limit evicts oldest-first and playback still works.
8. **Uncached frame does not block:** apply last-good and keep state `waiting`
   without stalling the timer.

Layer A: `CacheIndex` generation/fingerprint matching and eviction.

## Acceptance mapping

- **A1** keyframes: playback drives GH output per frame; replayed frames come from
  the cache.
- **A9** playback: measured fps at 10k/100k after the cache is warm.
- **New:** idle scene is static (Cycles accumulates samples), and a GH-side edit
  visibly resets the cache.

## Open questions

1. Does **Push** bump generations? (Suggested: yes.)
2. Are entries kept across a Blender reload when the definition fingerprint
   matches? (Suggested: yes; delete on mismatch.)
3. What is shown for an uncached frame during playback — last-good plus a small
   "updating" tag in the panel (suggested) or a viewport overlay?
4. Should the cache be per stack object or global across stacks? (Suggested:
   global under the data root, keyed by `stack_id`; the same stage key copied to a
   new stack gets new entries.)
