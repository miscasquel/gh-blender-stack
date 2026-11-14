# 0013 — Stage keys are unique; duplicates are a strict conflict

**Status:** Accepted, 2026-11-14.

## Context

A stage key names one Grasshopper stage. The add-on keeps it in the `.blend`
record, uses it for the parameter namespace (`gh/<key>/...`), the node-group and
helper names, the request id and Grasshopper's Stage In/Stage Out ownership.
Blender duplication copies all of that verbatim, so a duplicated stack starts
with two live claimants of one key. Grasshopper cannot tell them apart, and the
two objects then overwrite each other's groups and answers.

The previous policy split a shared key on every load, choosing the owner by the
lexicographically first object name. That silently rewrote a key the user may
already have wired into Grasshopper, and it was wrong whenever the original had
been renamed or the duplicate had a name that sorted first. It also only saw one
claim per object: two stage records with one key on the same object could not be
detected.

## Decision

- **Ownership is pointer identity, never a name.** `_stage_index` already holds
  object references. The engine keeps a per-key ownership snapshot from the
  previous index/pass plus a real claim count (same-object duplicates included).
  A runtime claimant whose key has one previously-known owner is split on its
  next pass; the owner keeps the key and its Grasshopper wiring even if it was
  renamed. If the owner is gone and one claimant remains, that claimant keeps the
  key without a split.
- **Ambiguity is a strict conflict.** A load, undo/redo or add-on enable cannot
  prove which claimant was the original. Two objects, or two stages on one
  object, sharing a key leave every conflicting stage in `error`, with a message
  naming the key and claimants. No request is sent and no response is applied or
  cached for that key; pending, sent, verify and Pull state is cleared. Undo/redo
  restoring a shared key enters the same safe state instead of auto-splitting.
- **Resolution is per stage and user-driven.** The N-panel shows **Resolve
  duplicate key** only on a conflicting row. It gives that stage a fresh random
  key, moves its parameter values and animation, resets its request/apply state,
  removes this object's stale helpers and block definitions under the old and new
  keys, and leaves the other claimant owning the old key. The next pass repairs
  the node group and the stage can connect. It is undoable with the other stage
  operators; there is no dialog.
- **One identity model.** The index, reindex/refresh path and split helper are
  reused. `duplicate_stage_key` remains only as the deterministic key generator
  for a runtime split; it never decides ownership. The name-based
  `_duplicate_owner` policy and its fallback path are removed.

## Consequences

- A `.blend` saved after duplicating but before a pass, or restored by undo, no
  longer opens silently split. It opens with the conflict visible and needs one
  click per duplicated stage. The previous name-derived key is not reused by the
  resolution, so a key already pasted into Grasshopper can still be identified
  from the old stage row.
- Runtime duplication (Shift+D, Alt+D, copy/paste, scripted `obj.copy()`,
  append/link while the original is live) keeps working without user action:
  the new object gets a new key and cache namespace before it can send.
- Conflicted stages keep their record and plumbing; only their transient request
  state is cleared. Deleting a conflicting stage or object lets the survivors
  leave the conflict and resume on the next pass.
- `blender/tests/headless/test_duplicates.py` covers renamed runtime copies, a
  vanished original, legacy load conflicts, same-object conflicts, the resolve
  operator, no sends/applies for a conflicted key, and a normal save/reload with
  distinct keys.
