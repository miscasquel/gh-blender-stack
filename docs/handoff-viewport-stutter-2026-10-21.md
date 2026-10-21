# Handoff: viewport stutter (2026-10-21)

**Status:** parked 2026-10-21. The evidence gathered after this handoff does not
support the duplicate-generation theory, and the stutter could not be tied to
engine work. It is treated as external/system-side for now. Re-open only if it
reproduces with both GH Stack installs disabled and the host otherwise quiet.

**Current tree:** v0.3.1, which carries the Phase 0 diagnostics described below.

## Phase 0 outcome (2026-10-21)

- A clean session with only the development add-on enabled reported one
  handler per list, one registered timer, no duplicate module generation, and
  two stack objects.
- A 60-second panning trace with the engine smooth showed 34–50 timer passes
  per second, **zero prefix clones, zero applies and zero sends**, `hot 0` and
  `dirty 0` throughout. The trace ring held only the startup/load triggers,
  the initial `hello` and four full sends.
- Two isolated four-clone bursts appeared in an earlier 15-second sample, with
  no sends or applies and no reproducible stutter around them.
- With both the development add-on and the extension disabled, `enabled_addons`
  and `sys.modules` were empty of GH Stack, no process listened on port 59130,
  and no stutter appeared during the observation.
- At the time of the comparison, the installed extension and the development
  tree differed only in the Phase 0 diagnostics; their runtime engine code was
  identical. The development tree has since gained the load-time timer fallback
  fix below, which only affects a timer that was already lost. Switching between
  the two installs therefore cannot explain a behavioural difference.
- The host was busy throughout: Rhino and Wine together used roughly one core,
  a browser GPU process held a large share of the GPU, the GPU sat around
  30–40% at low clocks, and Sunshine was running. That leaves a credible
  external cause for an intermittent viewport hitch.
- A dual-install state was observed once: the extension owned port 59130 with a
  connected client while the development package was loaded but disabled.
  Stopping the development server could not close the extension's socket. A
  clean restart cleared it, but it did not reproduce as the stutter cause.

Separately, the review found and fixed a load-time regression in
`engine/triggers.py`: the missing-timer fallback called
`bpy.app.timers.engine.register`, which does not exist. A file load that lost the
persistent timer would have raised instead of re-registering it. That fix is
covered by `test_registration.py`; it is unrelated to the stutter.

**Related work already landed:**

- `43ea08a` — Stage In/GhLink review hardening.
- `bea6e10` — definition-only outputs are no longer re-applied forever.
- `fc0d5c6` — attribute reconciliation, bounded hot cache, stage index.
- `a2d61e2` — engine split into `engine/` modules.
- `d27dd19` — stage prefixes share one depsgraph update.
- `ee5ed15` — prefix clones are flushed before the guard drops (a live
  feedback-loop fix).

The stutter discussed here is **not** the prefix-pass cost. FiraTower measures
~46 ms for a dirty Cube pass and ~1.5 ms idle after `ee5ed15`; that work only
happens when a stage exists and the scene is dirty.

## Symptom

- Viewport panning stuttered after the add-on had been reloaded/reinstalled
  while Blender was running.
- It happened in every file, including files with **no GH stage**.
- Disabling and re-enabling GH Stack made the stutter disappear.
- A full Blender restart also made it disappear.
- Vanilla Blender (or GH Stack disabled) is smooth.

## What this points to

The engine is not doing heavy work with no stages. A headless no-stage
measurement gave:

```text
_timer()          ~1.0 µs per call
_on_depsgraph()   ~0.7 µs per call with no updates
```

So the listener logic itself is cheap. The likely problem is that more than one
generation of the listener was alive:

- `register()` guards handlers by function identity:
  `if handler not in handler_list`.
- `register()` guards the timer with
  `bpy.app.timers.is_registered(_timer)`.
- A module reload (`Reload Scripts`, `scripts/install-addon`, or a reinstall
  while Blender is open) creates **new function objects**. The old timer and
  old handlers are not the same objects, so they can survive and a second set
  is added.
- Two timer/handler sets mean twice the main-thread wakeups, twice the hot
  cadence (`_mark_hot()` extends 5 ms ticks), and more Python/GIL contention.
- Disabling/re-enabling tears down the current generation and re-registers one
  set; a restart clears everything. That matches the observed fix.

`bpy.app.timers` has no public way to enumerate registered callbacks, so timer
duplicates are not directly visible. Handler duplicates are.

## First checks for the next session

### 1. Confirm duplicate handlers after a reload

In Blender's Python console:

```python
import bpy

for name in (
    "depsgraph_update_post",
    "frame_change_post",
    "load_post",
    "undo_post",
    "redo_post",
):
    handlers = getattr(bpy.app.handlers, name)
    mine = [
        (getattr(h, "__module__", ""), getattr(h, "__name__", ""))
        for h in handlers
        if getattr(h, "__module__", "").startswith("gh_stack")
    ]
    print(name, mine)
```

Expected healthy result: one `gh_stack.blender.engine.triggers` `_on_depsgraph`,
one `_on_frame_change`, one `_on_load`, one `_on_undo`, one `_on_redo`.

More than one entry confirms stale registration.

### 2. Reproduce the transition

1. Full restart Blender; open any file, with or without a stage; pan. Smooth.
2. Run `scripts/install-addon` while Blender is open, or use Reload Scripts.
3. Disable/enable GH Stack if needed and pan again. Stutter returns.
4. Disable/enable once more, or restart Blender. Smooth again.

Record whether handler duplicates are present at each step.

### 3. Distinguish timer from handlers (optional)

If needed, remove the timer only in the console and pan:

```python
import bpy
from gh_stack.blender import engine
bpy.app.timers.unregister(engine._timer)
```

Then remove the depsgraph handler only. This separates timer cadence from
handler work. Do not leave the add-on in that state for normal use.

### 4. Registration report (Phase 0 helper)

Run this in Blender's Python console at each step of the reproduction:

```python
import pprint
from gh_stack.blender import engine

pprint.pp(engine.registration_report(), sort_dicts=False)
print("passes/s:", engine.tick_rate(5.0))
```

What to read:

- `handler_counts`: all five should be 1. The report matches both
  `gh_stack.blender.engine` and `bl_ext.*.gh_stack.blender.engine`, so a
  development install and an extension install cannot hide each other.
- `timer_registered`: should be True.
- `engine_file`: should end in `engine/__init__.py`; if it points at an old
  `engine.py`, the running session is on pre-split code and needs a restart.
- `enabled_addons`: should name exactly one GH Stack install.
- `tick_rate`: the idle cadence is 20 ms, about 50 passes/s. A hot engine is
  5 ms, about 200/s. It measures **this** generation only; a stale timer with
  its own module state is invisible to it, which is why `handler_counts` is the
  primary signal. `tick_rate` blocks the console for its duration; in
  `blender -b` it is 0 because timers are inert.
- `hot_seconds`: a positive value in an empty file means something is still
  calling `_mark_hot()` with no stages.
- `index_dirty` may be True until the first tick; it is not a fault by itself.

Record the numbers after a fresh restart, after `scripts/install-addon` plus
Reload Scripts, after an in-place extension reinstall, and after
disable/enable. Do not write the fix until that comparison says whether the
problem is handler generations, timer cadence, or neither.

## Proposed changes (preserved, not pursued while parked)

None of the changes below was implemented for this investigation. They remain a
starting point if the stutter re-opens with a clean disabled baseline showing
engine involvement.

### A. Reload-safe registration

Implement in `blender/gh_stack/blender/engine/__init__.py` and
`blender/gh_stack/blender/engine/triggers.py`.

1. Before appending handlers, remove any handler whose `__module__` starts with
   `gh_stack.blender.engine`, not just handlers that compare identical to the
   new function. This clears old generations from a module reload without
   touching other add-ons.
2. Give the timer an ownership record that survives a module reload. Two viable
   approaches:
   - Store the previous timer callable and handler list in
     `bpy.app.driver_namespace` under a private key, and unregister/remove them
     from the new `register()` before installing the new set.
   - Store a session token in `bpy.app.driver_namespace`; the old timer checks
     the token at entry and unregisters itself when it no longer owns it.
     Simpler, but it leaves the old timer alive for one tick.
3. Make `unregister()` clear the same record and remain idempotent.
4. Do not use a broad `bpy.app.handlers.*.clear()`; only remove handlers that
   belong to this engine's module prefix.

Potential pitfall: the old timer function's module is still in `sys.modules`
or has been replaced; test by actually reloading the engine module/add-on, not
only by calling `register()` twice in one process.

### B. Dormant engine when no stages exist

This is worth doing independently of the reload bug.

- Do not register `_timer` or the depsgraph/frame handlers when no stack has
  stages.
- Reactivate on:
  - first stage added (`request_again` from `Add Stage`);
  - file load with stages;
  - undo/redo restoring a stage.
- Deactivate when the last stage is removed.
- Keep `load_post`, `undo_post`, `redo_post` registered so restoration can be
  noticed; the expensive per-frame work is the timer and the depsgraph/frame
  handlers.
- Keep the WebSocket server available; network work is irrelevant until a stage
  exists. A low-frequency pump is still needed while stages exist and a client
  is connected because responses can arrive without a Blender edit.

Suggested split: an explicit `engine.activate()` / `engine.deactivate()` (or
`_ensure_active()`) called from `register`, `_on_load`, `_after_history_step`,
`request_again`, and `_drop_removed_stages`.

### C. Cadence (follow-up, not required for this bug)

Even with one clean generation, `_mark_hot()` keeps a 5 ms timer for a second
after every depsgraph trigger. Consider replacing the persistent hot cadence
with a one-shot/settling pass while work exists, then decay to a slow idle pump.
Do not do this before the reload-safe registration and dormancy, and measure
with Diagnostics (`passes`, `clones`, `applies`, `prefix_ms`).

## Tests to add

In `blender/tests/headless/test_registration.py` (or a new file):

- Calling `register()` twice does not add a second engine handler.
- A handler from a foreign module is left alone while engine-module handlers
  are swept.
- A no-stage file has no engine timer registered and no depsgraph/frame engine
  handlers; adding a stage activates them; removing the last stage deactivates
  them.
- File load with stages activates the engine; file load without stages leaves
  it dormant.
- Undo restoring a deleted stage activates it again.
- The stage still round-trips through `fake_gh` after activation.

Timer reload behavior may need a manual test because the headless timer is
inert under `blender -b`; `bpy.app.timers` registration itself can still be
inspected.

## Acceptance

- After `scripts/install-addon` plus Reload Scripts/disable-enable, the handler
  lists show exactly one engine generation.
- Panning is smooth in an empty file with GH Stack enabled.
- Panning is smooth in FiraTower, and a dirty pass still settles to ~1.5 ms.
- Adding/removing stages activates/deactivates the engine without a restart.

## Do not

- Do not chase the 46 ms prefix pass for this symptom; the stutter was gone
  after a full restart and occurred even with no stage.
- Do not remove `bpy.app.handlers.load_post`/undo/redo; they are needed to
  reactivate stages restored by a file load or undo.
- Do not assume `bpy.app.timers.is_registered(_timer)` proves there is only one
  timer; after a reload, `_timer` is a new function object.
