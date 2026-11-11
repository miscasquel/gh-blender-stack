# 0009 — Play Every Frame sync and opt-in native stage bake

**Status:** Accepted, 2026-11-08. Amends [0008](0008-simulation-style-frame-cache.md):
the frame cache remains the default path; a native bake is an explicit render
path, not the playback path.

## Context

ADR 0008 removed the explicit bake operator in favour of a simulation-style
frame cache, and made "playback never blocks" a rule: uncached frames kept the
last output while the playhead kept moving. Two gaps remained.

- **Play Every Frame did not wait for Grasshopper.** Blender's Play Every Frame
  advances one frame at a time and waits for the dependency graph, but a
  Grasshopper stage is outside that graph. The playhead could run ahead of the
  stage, so most frames were never shown even though the user asked for every
  frame.
- **Renders relied on a warm cache.** The frame cache made interactive playback
  and offline replay work, but a final render had no explicit, self-contained
  baked state. A native Geometry Nodes bake gives the render path a stable
  per-frame output and removes Grasshopper from it entirely.

## Decision

- **Play Every Frame waits for each stage.** When playback starts with
  `Scene.sync_mode == "NONE"`, `animation_playback_pre` arms a gate and
  `frame_change_post` runs the normal stage pass synchronously. The playback
  timer cannot advance until the handler returns, so the playhead waits for the
  current frame's Grasshopper output exactly as it waits for a slow native
  modifier. `FRAME_DROP` keeps the existing asynchronous latest-wins behaviour.
- **The sync pass is the normal engine path.** It uses the same prefix,
  request IDs, cache and apply path as the timer. Warm frames replay, a
  disconnected client keeps the last output, and the timer is suppressed while
  the gate owns the frame so the two cannot race.
- **A configurable sync wait limit.** `playback_timeout` caps one stage's wait
  per frame. The default is 0: wait until the answer or a disconnect, which is
  what "Play Every Frame" means for an external solver. A positive value keeps
  the last output and continues after that many seconds.
- **Each stage group carries a native Bake node.** `GeometryNodeBake` sits
  between Join Geometry and the Group Output, with one Geometry bake item. It
  passes the live stage output through until the stage is baked.
- **Bake stage is explicit and freezes the stage.** It requires a saved
  `.blend`, an active stage modifier and a forward scene frame range. It sets
  an ANIMATION bake over the scene range, arms the same synchronous gate while
  Blender's bake job steps frames, then freezes the stage so no further
  Grasshopper requests are made. Renders read the baked data directly; there is
  no Grasshopper code in the render path.
- **Clear Bake returns to live.** It deletes the native bake data, clears the
  baked flag, unfreezes the stage and resumes live Grasshopper. The bake uses
  Blender's own bake target, so a packed bake is only written when the `.blend`
  is saved.

## Consequences

- ADR 0008's "no bake operator" is no longer a blanket rule. The frame cache is
  still the default live and replay path; the native bake is opt-in and exists
  for rendering and frozen hand-off.
- ADR 0008's "playback never blocks" applies to Frame Drop and the asynchronous
  timer. Play Every Frame and a stage bake intentionally block per frame.
- The stage group format moves to 4. Existing groups are rebuilt on the next
  ensure pass because an older group has no Bake node.
- New surface: the `playback_timeout` preference, **Bake stage** / **Clear
  Bake** in the panel, the `baked` stage flag, and the `playback_frames`,
  `playback_synced` and `playback_failures` counters.
- Headless coverage lives in `blender/tests/headless/test_playback_sync.py`
  (mode selection, one request per frame, warm-cache replay, disconnect, timeout
  preference, driven default, Bake node). A live Rhino playback/bake pass is
  still pending.
