# Handoff — selective stage solves, generator/live stages, and the N-panel regression

**Date:** 2026-11-13
**Status:** committed 2026-11-13. The Blender 5.3 UI blocker is fixed and
covered by `blender/tests/headless/test_ui_panel.py`; the stage loop is
resilient. Live now defaults on for new stages, with a hover description.
Live Rhino/GH acceptance of the solver scheduling is still in progress. The
GH-side Stage In geometry-loss report is unconfirmed and tracked below.
**Authoritative design:** [0010-selective-stage-solves-and-live-generators.md](decisions/0010-selective-stage-solves-and-live-generators.md)

---

## 1. Current working-tree state

The patch implements:

- **GH native selective update.** `GhLink.RequestSolve()` no longer expires every
  Stageloom component. Requests are queued per stage key; only the requested
  stage's Stage In, Stage Out and Params are expired with `ExpireSolution(false)`
  inside the one scheduled solution.
- **Request-only answers.** During a request-driven solve, only the requested
  stage's Stage Out answers. A user-driven GH solve can still push affected
  stages. `LastSentRid/Hash` suppresses byte-identical user-driven re-sends and
  is cleared on a new connection.
- **`hello.stages[].has_stage_in`.** Optional additive bool. `false` marks a
  generator stage (no Stage In). Blender stores `input_mode = prefix | generator`.
- **Generator stages.** No prefix evaluation; request id depends only on Params,
  nonce and the empty geometry hash. Excluded from prefix batches.
- **Live stages.** Avoid the frame cache and generation reset for a stateful
  output (Kangaroo). `live` is a stage checkbox; `live_frame` records the last
  delivered frame. A live stage sends only when its own rid changes, the scene
  frame changes, a verification/resend permit is armed, or the hard timeout
  retries. It is not advanced by another stage's apply. Live answers apply but
  are not cached and do not bump/prune the generation.
- **Stale-answer safety.** A stale `ok` does not clear a newer pending request or
  bump a generation. Deterministic stale answers are cached under their own rid;
  live stale answers are dropped.
- **Live toggle cache reset and undo/redo verification.** Toggling Live bumps the
  stage generation, clears hot/disk frames and resets `live_frame`; undo/redo
  resets `live_frame` for live stages.

### Changed files

- `grasshopper/src/gh/GhLink.cs` — claims/roles, `PublishStageIn/Out`,
  `has_stage_in`, request queue, active request context, selective expiry,
  `ShouldAnswer`, `LastSent*`, reconnect clearing.
- `grasshopper/src/gh/Components.cs` — Stage In/Out publish roles; Stage Out
  answer gating and unchanged-answer skip.
- `grasshopper/tests/console/LinkTests.cs` — new hello/queue/context tests.
- `blender/stageloom/blender/stack.py` — `input_mode`, `live`, `live_frame`,
  hooks for cache reset and dirtying.
- `blender/stageloom/blender/engine/request.py` — generator prefix skip, live
  send gate, live frame recording.
- `blender/stageloom/blender/engine/messages.py` — hello `has_stage_in`, stale
  answer ordering, live no-cache path.
- `blender/stageloom/blender/engine/lifecycle.py` — `_reset_stage_cache`.
- `blender/stageloom/blender/engine/triggers.py` — undo/redo live reset.
- `blender/stageloom/blender/engine/__init__.py` — cache-reset hook wiring.
- `blender/stageloom/blender/ui.py` — Live checkbox and generator label;
  `_GENERATOR_ICON`, state-icon table, and per-stage draw resilience.
- `blender/tests/headless/test_live_generators.py` — new coverage, including
  the live-by-default and tooltip assertions.
- `blender/tests/headless/test_ui_panel.py` — icon smoke checks and a recording
  layout draw, including the one-stage-fails path.
- Existing deterministic cache/replay tests set `live = False` explicitly now
  that the stage default is live.
- `docs/protocol.md` — `has_stage_in` documentation.
- `docs/decisions/0010-...md` and the decision index.

No commit was made. Pre-existing edits to `README.md`, `DEMO.md`,
`docs/plan.md` and `docs/extensions-compliance.md` were left alone.

---

## 2. Installed artifacts

### Rhino 8 / Grasshopper

- Installed at:
  `~/.local/share/wineprefixes/rhino8/drive_c/users/miquel/AppData/Roaming/Grasshopper/Libraries/Stageloom/Stageloom.gha`
- Hash matches the build:
  `ac6f695940ec552ab5fdcc35205e70b505475db0f2464dbba8c0371e61a841c8`
- `scripts/check-deployed` reports current.
- One `.gha` exists under the Rhino 8 prefix.

### Blender 5.3

- Installed extension:
  `~/.config/blender/5.3/extensions/user_default/stageloom`
- `blender_manifest.toml` version `0.5.0`.
- Enabled as `bl_ext.user_default.stageloom`.
- Installed `stack.py`/`messages.py`/`request.py` contain the new code.
- `.user/user_default/stageloom` is only the frame-cache data root (~257 MB),
  not a second code install.
- Old `__pycache__` was removed after install.

### Separate installations left untouched

- Blender 5.2 extension `0.4.1` under `~/.config/blender/5.2/`.
- Old `Stageloom.gha` under the `rhino9-fork-test` Wine prefix.

These are not loaded by the requested Blender 5.3 / Rhino 8 targets.

---

## 3. Live reference-file state (2026-11-13)

File: `S1-FF-Catenary.blend`
GH document: `CatenaryMST_Init.gh`
Object: `Cube`
Modifiers: `Stageloom · Stage`, `GeometryNodes`, `Stageloom · Stage.001`

| Stage | Mode | State | Frozen | Live | Generation |
|---|---|---|---|---|---|
| `stage-81c35a` | generator | frozen | **yes** | **no** | 24866 |
| `stage-5b4402` | prefix | ok | no | no | 1 |

- Stage A is the GH generator. It has no Stage In and is currently **frozen**, so
  it sends nothing. `live=false`.
- Stage B is the prefix stage and is working.
- The user has deleted the temporary third stage they created.

Recent timings on the live file after the GH patch:

```text
stage-81c35a: prefix 0.003 ms · encode 0.017 ms · answer 139 ms · apply 1.9 ms
stage-5b4402: prefix 13.4 ms · encode 0.81 ms · answer 155 ms · apply 18.3 ms
```

The user reports that answer times differ per stage and only the needed stage is
computing. The selective-update part appears to be working.

---

## 4. The immediate Blender UI blocker

### Symptom

The Stageloom N-panel shows only one stage. Creating a stage or baking throws the
same exception on redraw:

```text
File ".../stageloom/blender/ui.py", line 74, in draw
    self.draw_stage(layout, obj, record, stage, index)
File ".../stageloom/blender/ui.py", line 97, in draw_stage
    row.label(text="Generator (no Stage In)", icon="NODES")
TypeError: UILayout.label(): error with keyword argument "icon" —
  enum "NODES" not found ...
```

### Cause

`NODES` is not a valid icon in Blender 5.3. The enum list includes
`GEOMETRY_NODES`, `NODE`, and `NODETREE`, but not `NODES`.

`STAGELOOM_PT_panel.draw()` loops over stages and calls `draw_stage()`. Stage A is
a generator, so it reaches the invalid-label line; the exception aborts the rest
of the panel draw. Stage B is therefore never drawn. The stage data is intact
(the record has both stages); this is a rendering abort, not a lost stage.

### Intended fix

Use the existing `_ICON_NAMES` convention at the top of `ui.py` (the same pattern
as `_STAGE_ICON`):

```python
_GENERATOR_ICON = (
    "GEOMETRY_NODES" if "GEOMETRY_NODES" in _ICON_NAMES else "NODE"
)
...
row.label(text="Generator (no Stage In)", icon=_GENERATOR_ICON)
```

Add a UI smoke check that every icon constant used by the panel is in
`_ICON_NAMES` (or at least that `_GENERATOR_ICON` is). The current headless tests
do not draw the panel, which is why this escaped.

A second robustness improvement: wrap one `draw_stage()` call in a try/except
that draws a readable error box and continues with the next stage. One bad stage
should not hide every later stage. This is a UI robustness change, not a
substitute for the icon fix.

### Why creating a stage / baking triggers the same error

Those actions redraw the N-panel. The existing generator stage A is always in the
object, so the same `draw_stage()` line throws on every redraw. The new stage and
bake themselves are not the cause.

---

## 5. What the Live toggle is and why it exists

### The design reason

The normal frame cache assumes a **deterministic stage**: the same request id
(geometry + params + nonce) produces the same output hash. It stores frames by
`rid` and replays them on scrub/playback.

A Kangaroo generator is **stateful**: each solve advances the simulation one
step. The same `rid` can legitimately produce a different output hash, and
replaying an old frame does not reconstruct the solver state. Without special
handling, the plugin sees “same rid, different output” and treats it as a GH
definition change: it bumps the generation, prunes the cache, applies again, and
can enter a reset/request loop.

`live` tells the stage:

- do not consult or store the frame cache;
- do not bump the generation or prune frames on a same-rid output change;
- send only when its own inputs changed or the scene frame changed;
- let an applied answer dirty the object for the **downstream** stage, but do
  not re-send itself merely because a downstream apply dirtied the object.

### Why it is a toggle instead of automatic

A generator stage is not necessarily stateful. A deterministic generator (for
example, a pattern driven by a frame parameter) can and should use the frame
cache. `input_mode = generator` only says “no Stage In”; `live` says “the output
is solver state and cannot replay”. They are orthogonal.

The user is questioning whether the toggle is worth the UX complexity. Options
for the follow-up session:

1. **Keep it explicit**, but rename it to something clearer such as
   **Stateful / Live output**, add a tooltip and a warning when a generator stage
   is likely stateful (for example, same-rid output changes repeatedly).
2. **Auto-detect**: when a stage answers the same rid with a different out_hash
   more than once, mark it live and stop resetting its generation. This is magic
   and may mask a real GH edit; needs a clear diagnostic if chosen.
3. **Default generator stages to live**, with a “deterministic generator” opt-out.
   Simpler for this workflow, but fails for frame-driven deterministic generators.
4. **Split the flag from input mode**: keep `generator` for prefix semantics and
   use `cache = deterministic | live` for caching semantics. This is what the ADR
   describes; the UI can expose one “Stateful output” checkbox.

Recommendation for the next session: keep the explicit flag but improve naming,
tooltip, and validation; do not auto-detect silently.

### Current file gotcha

Stage A is currently **frozen**. `_process_stage()` returns before the live gate
when frozen, so ticking Live while frozen does nothing. To test:

1. Unfreeze stage A.
2. Tick Live.
3. Connect GH.
4. Play/scrub.

The generation value 24866 is historical; with Live on, it should stop climbing.
It does not need to be reset manually.

---

## 6. Other observations / follow-ups

- **New-stage error was expected.** A stage created in Blender with no matching
  Stage Out/Stage In in GH goes to `error`/`unbound`; the user deleted the test
  stage. If recreated, wire a Stage Out with the same key. The panel should show
  the key and error once the icon exception is fixed.
- **Only one stage displayed is not a stage-record bug.** The live query shows
  both stages; the panel draw aborts at stage A.
- **Generation 24866 is pre-existing state.** It came from the earlier
  same-rid reset loop. Live prevents new resets; the number itself is harmless.
- **`has_stage_in` is claim-derived.** A just-added Stage In can report false
  until its first solve; the next `hello` corrects it. If that is visible during
  wiring, it is transient.
- **Mixed user edit during a request solve.** A canvas edit coalesced into a
  request-driven solve may have its non-requested stage push deferred to the next
  solve. This is a known risk of the answer gate; watch it in live use.
- **Full dependent stack still serial.** A and B both changing remains two serial
  solves, because B's prefix contains A's applied output. The patch removes
  redundant solves and unrelated branch work, not the dependency round trip.
- **Stage In build reuse is not in this patch.** Repeated refs still rebuild the
  Stage In geometry in GH. A separate conversion optimisation.
- **Unconfirmed GH-side Stage In geometry loss.** The user reported that
  changes which should not update the Stage In seemed to clear its staged
  geometry, then questioned that diagnosis. The only known path that clears a
  held frame is a `full` request with zero sections (protocol: `full` replaces
  the held geometry). A transient `has_stage_in: false` or an empty Blender
  prefix could send one. Reproduce and instrument before changing the GH
  semantics.

---

## 7. Verification already run

- `grasshopper/build/build.sh` — all five console executables built.
- `grasshopper/build/build-gha.sh` — built `grasshopper/build/out/Stageloom.gha`.
- `scripts/test-gha` — PASS:
  - 12 core fixtures
  - attribute store
  - link round-trip (new `has_stage_in`, request context, retry, unchanged-answer checks)
  - stage component status
  - stage-in build guard
- `scripts/test-blender` — **249 tests, OK**; offline gate PASS.
- `scripts/test-core` — **57 tests, OK**.
- `scripts/test-extension` — PASS.
- `scripts/build-extension` — exit 0; `dist/stageloom-0.5.0.zip`.
- `git diff --check` and Python compilation clean.

**Not run:** real Rhino/Grasshopper UI acceptance for one-step-per-frame, no
cross-stage answers, and the frame-0 reset. The user is testing that live.

---

## 8. Suggested next-session order

1. **Fix the UI icon** with `_ICON_NAMES` fallback; add an icon smoke test.
   **Done:** `_GENERATOR_ICON`; the smoke test covers constants, state icons
   and literal `icon=` arguments.
2. **Make the stage loop resilient**: one failing stage should render an error
   row and continue, not abort the panel. **Done.**
3. Rebuild/reinstall the Blender extension; restart Blender. **Done.**
4. Unfreeze stage A, enable Live, and rerun the live test.
5. Decide the Live UX (explicit renamed flag vs auto-default vs auto-detect).
   **Done:** new stages default live with a hover description; a deterministic
   stage opts out by unticking Live.
6. Re-check the reference file:
   - both stages visible;
   - generator label visible;
   - stage A advances one Kangaroo step per frame;
   - a B request does not advance A;
   - one response per requested stage per request;
   - generation stops climbing;
   - scrub to 0 resets Kangaroo natively;
   - deterministic stages still replay from the cache.
7. If the double-answer or double-step survives, instrument the GH solve path
   (`ExpireSolution(false)` vs the scheduled delegate) before changing more
   semantics.

---

## 9. Commands

Build/deploy:

```bash
grasshopper/build/build-gha.sh
grasshopper/deploy/deploy.sh          # then restart Rhino/GH

scripts/build-extension
scripts/install-extension             # then restart Blender
```

Exact UI repro:

```text
Open S1-FF-Catenary.blend
Select Cube
The panel draws stage A up to the generator label, throws on icon="NODES",
and stops before stage B.
Creating a stage or baking redraws the panel and throws the same error.
```

Key reference: `blender/stageloom/blender/ui.py`, `STAGELOOM_PT_panel.draw()` and
`draw_stage()`.
