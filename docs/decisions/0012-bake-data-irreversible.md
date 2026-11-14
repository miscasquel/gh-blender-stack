# 0012 — Bake data is irreversible; Clear Bake confirms

**Status:** Accepted, 2026-11-14.

## Context

The 0.6 robustness work requires Bake stage and Clear Bake to be either
undo-safe or explicitly irreversible with a confirmation before bake data is
deleted. Clear Bake calls Blender's
`OBJECT_OT_geometry_node_bake_delete_single`, then clears the stage's `baked`
and `frozen` flags. Before this record it had `bl_options = {"REGISTER"}` and no
confirmation.

A headless probe saved a temporary `.blend` with one Stageloom stage, baked two
frames, pushed an explicit "baked" undo step, ran `stageloom.clear_bake`, and
called `bpy.ops.ed.undo()`. The bake and clear both finished. The undo did not
restore the bake: it landed on the previous "stage" step, with the Bake entry's
mode back at `STILL` and `stage.baked` false. Blender's delete operator also has
no undo flag in its registration, so it does not record the pre-delete data.
The experiment therefore gives no reliable undo path, and claiming undo safety
would hide a real data-loss path.

The probe also showed that the native `modifier.bakes` entry is metadata: it
exists as soon as the depsgraph sees a Bake node, before any bake, and remains
after Clear Bake. Entry presence alone cannot decide whether a confirmation is
needed.

## Decision

- **Clear Bake is documented as irreversible.** Its tooltip and the panel row
  say the bake cannot be undone. The README known-limitations list and the
  changelog carry the same note.
- **Delete prompts only when there is data to lose.** `invoke()` calls
  `WindowManager.invoke_confirm` when the stage's `baked` flag is set, or when a
  native entry points at data the add-on did not write (a disk bake directory,
  or referenced bake data blocks). A bare metadata entry and an already-empty
  stage clear without a prompt.
- **The confirmation is the only dialog.** Title **Clear Bake**, a message that
  the data cannot be recovered, and **Delete Bake** as the confirm button. There
  is no second panel, toggle, or preference.
- **No `UNDO` flag is added.** `Bake stage` stays `REGISTER` too. The UI hides
  Bake stage while a stage is baked, so the normal panel path cannot replace an
  add-on bake without first clearing it; no extra confirmation is added there.
- **The decision is a testable helper.**
  `clear_bake_needs_confirmation(stage, entry)` owns the decision, and the
  tests pin it without opening a modal.

## Consequences

- Clearing a bake is immediate and permanent. Recovering it means baking the
  scene range again; a packed bake that was never saved was already at risk and
  is now explicitly presented that way.
- A native packed bake made outside the add-on exposes no `is_baked` flag. The
  panel does not offer Clear Bake unless `stage.baked` is set, so that bake only
  reaches this path through a programmatic call; the add-on's own bakes are
  covered by the stage flag.
- `blender/tests/headless/test_bake_safety.py` covers the helper, the no-op
  clear, the invoke decision, the warning text, and the absence of `UNDO`.
  `test_ui_panel.py` covers the panel warning. The actual popup is left to
  Blender and is not exercised headless.
