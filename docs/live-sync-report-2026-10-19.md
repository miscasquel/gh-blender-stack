# Live sync: findings and plan (2026-10-19)

Investigation report for the live report: *"times I edit the mesh and unless I
manually push, nothing happens."*

Scope: five parallel audits (trigger/wedge, mesh-edit matrix, GH plugin
lost-solve, deployed code, response/apply path), each reproduced headless where
possible, plus a latency measurement of the current pipeline. The repository was
not modified during the investigation. Line numbers refer to the working tree at
the time of writing (the uncommitted engine changes from 2026-10-18/19 are
present; the repo suite is 158/158 green).

---

## 0. The symptom is an asymmetry, not latency

Push works. A mesh edit does not. Push is the one path that:

1. writes the engine's dirty set **directly** (`request_again`, `push`,
   `engine.py:1662`), bypassing the depsgraph handler;
2. changes the stage **nonce**, so the request id changes even when geometry and
   parameters are unchanged;
3. resets GH's solve-retry budget on the plugin side (a request is the only
   thing that re-arms a stranded slot, `GhLink.cs:507`).

Every other path — a depsgraph tag, a frame change, a parameter commit — goes
through the trigger chain: *handler → dirty → pass → prefix read → new rid →
send*. Any link in that chain that dies silently looks exactly like "the addon
is dead until Push", and Push cannot even tell you why.

So the investigation looked for every way the chain can die without a visible
trace. It found three independent, reproduced paths, plus a set of smaller
apply-path traps and a latency budget that makes the live loop trail badly even
when everything works.

---

## 1. How the engine works today (working tree)

- `_on_depsgraph` (`engine.py:131`) bumps one global `_prefix_epoch` and marks
  every stack dirty. The prefix memo is keyed by that generation, the frame and
  the SubD flag (`_prefix_revision`): any tagged update retires every memo.
- `_timer` (`engine.py:265`) drains the depsgraph first (`_drain_depsgraph`,
  `engine.py:139`) so pending updates cannot be swallowed by the prefix clone's
  `_evaluating` mute, then flushes the active Edit Mode object
  (`_flush_edit_mode`, `engine.py:160`), then processes dirty stacks.
- The prefix is always read from an evaluated clone (`_prefix_sections`
  `engine.py:1353`, `_clone_prefix` `engine.py:1469`); in Edit Mode the clone
  gets a private copy of the flushed datablock.
- One request in flight per stage (`_pending`); a cached frame answers before
  the network; responses apply only when `rid == stage.rid`
  (`_handle_response` `engine.py:430`).
- `hello`/load put stages on `_verify` once, so a reconnect asks GH instead of
  replaying the cache.
- Diagnostics shows a bounded "Recent activity" ring (`_trace`).

This part is sound in the cases the repo suite covers. It is not sound in the
cases below.

---

## 2. Findings

### F1 — Edit Mode is flushed only for the *active* object (deterministic, no exception needed)

`_flush_edit_mode` (`engine.py:160`) reads `bpy.context.edit_object` and returns
unless **that** object is a stack object and is dirty. Blender supports
multiple objects in Edit Mode at once, and `bpy.context.edit_object` is only the
*active* one. Consequences, all reproduced headless:

- Two stack objects both in Edit Mode, stroke the non-active one: `_dirty`
  contains both names, but only the active object's datablock is flushed. The
  clone copies the stale mesh, the geometry hash does not change, and
  `_process_stage` silently replays the cached frame. `sends_delta = 0`.
- Stack object in Edit Mode while a non-stack object is active: the flush
  early-returns at the "not a stack object" check; the stroke never reaches GH.
- **Push alone does not recover this.** With the stale clone the hash is
  unchanged, so Push sends a `ref` request claiming GH's copy is still valid;
  the geometry never leaves Blender. The agent needed
  `obj.update_from_editmode()` plus Push. Tabbing out of Edit Mode flushes all
  objects and recovers, which is why the failure window is "inside the mode".

This is the most likely everyday cause of the report: no exception, no plugin
involvement, exactly "edit and nothing happens".

Evidence: `/tmp/opencode/out/result_14_multi_edit_non_active.json`,
`result_15_edit_stack_object_not_active.json`,
`result_16_leave_editmode_flushes_other.json`.

### F2 — The `_evaluating` guard can go permanently mute (exception path, reproduced)

`_clone_prefix` (`engine.py:1469`) sets `_evaluating = True` and clears it as the
**last statement of the `finally`**:

```python
finally:
    bpy.data.objects.remove(clone, do_unlink=True)   # raises?
    if owned_data is not None:
        _drop_owned_data(owned_data)                 # raises? (bl_rna touched outside its try)
    bpy.context.view_layer.update()                  # raises?
    _evaluating = False                              # never reached
```

A raise in any of the three steps above the reset leaves `_evaluating = True`
for the rest of the session. `_on_depsgraph` and `_drain_depsgraph` then return
early forever: no edit dirties anything, `_prefix_epoch` never moves, the memo
replays the pre-failure revision. `_timer` keeps ticking and does nothing.

Reproduced:

```
after raise in clone finally   evaluating=True  dirty=[]  sends=1
further edit                   dirty before=[] after=[]   sends 1->1   (ignored)
push (transient fault)         sent a request: True
persistent fault + push        evaluating=True  dirty=[]  sends=2      (push could not send)
_evaluating survives unregister/register: True
```

- Nothing resets or monitors the flag: it survives addon disable/enable and
  file load; the only writes are `True` at `engine.py:1497` and `False` at
  `:1512`.
- The same structure exists at HEAD and at `caad5a9` (where it was introduced);
  the working tree's `_drop_owned_data` merely added one more throwing statement
  before the reset.
- A *persistent* cleanup fault defeats Push too — the recovery everyone trusts
  is not universal.

### F3 — Edits are swallowed while GH is silent (up to 30 s; Push/Pull instant)

While a request is outstanding, `_process_stage` returns `waiting` for any
uncached frame (`engine.py:1275-1294`): no second send, no feedback. The GH side
can strand a request:

- `ScheduleSolution(10, delegate)` coalesces and can silently drop delegates
  (S1 evidence, `docs/plan.md` §19); the slot retry budget is 8 attempts at
  250 ms (~2 s, `GhLink.cs:736`), and **only a new request resets it**
  (`GhLink.cs:507`).
- After the budget is dry the slot sits `waiting` with no future trigger. The
  engine's only recovery is the 30 s timeout, which resends in full. Push/Pull
  short-circuit it by changing the rid and re-arming the plugin.

So even without F1/F2, a mesh edit can sit invisible for up to 30 seconds. Two
audits independently ranked this as a match for the report.

### F4 — Response/apply traps (smaller, same shape)

- **Batch abort.** `_handle_incoming` (`engine.py:335`) has no per-message
  guard, and `drain_incoming()` has already emptied the queue. A raise in
  `_apply`/`ensure_group` aborts the whole pass: later messages in the batch are
  lost, and because `_forget` runs before `_apply` (`engine.py:485`) the stage
  sits `waiting` with **no pending entry** — every later pass replays the same
  stored frame and re-raises. Only a rid change (Push) escapes. Reproduced.
- **Standalone errors do not clear pending.** `_handle_error`
  (`engine.py:534`) sets the stage's error text but never `_forget`s, so e.g.
  `unknown_stage` is followed by a 30 s timeout for an answer that will never
  come.
- **Hash trust.** `_handle_response` skips work whenever
  `out_hash == stage.applied_out_hash` (`engine.py:487`), and `_bump_generation`
  (`engine.py:998`) never clears that hash. If helper payloads are emptied while
  the hash survives, every response and replay is skipped — and **Push does not
  help either** (fresh rid, same hash). Only `_repair_plumbing`'s missing-helper
  detection or a different GH output escapes.
- **Parked `_verify`.** The permit is discarded only after a successful send
  (`engine.py:1331`); a frozen/disabled stage returns before that, and a
  disconnected one replays the cache while keeping the permit. Recovery is
  Pull/unfreeze, not Push.
- **Burned Pull permit.** `_pull_permits` is discarded at `engine.py:1223`
  before the prefix read and the send; a failed send or a pass exception loses
  the Pull silently.
- **`unknown_ref` resend swallowed by the cache.** `_resend_in_full`
  (`engine.py:570`) promises a full resend, but the cached frame for the
  unchanged rid answers first and returns `idle`. Liveness only: the next rid
  change sends in full because `geom_hash` was cleared.

### F5 — GH can answer a newer rid with older inputs (plugin; blocks supersede)

`GhLink.Handle` overwrites the slot unconditionally (`GhLink.cs:503`), but
Stage Out reads `slot.Rid` at the **end** of its solve (`Components.cs:294`,
error path `:335`). A solve that read geometry for rid1 can therefore answer
rid2. Blender accepts it (`rid == stage.rid`) and caches a frame that does not
correspond to the geometry it names. Today the window is narrow (one request in
flight); the moment superseding is enabled for responsiveness this becomes easy
to hit. The plugin must pin the rid when the solve starts and answer that rid.

### F6 — The live loop trails badly even when everything works (measured)

Headless measurement, 51 076-vertex grid (≈100k tris), one stage, 60 mesh edits
at ~60 Hz against a zero-latency fake client:

| measurement | value |
|---|---|
| prefix clone (the read the depsgraph forces) | **10.6 ms** |
| engine time during a 2.1 s drag | **0.96 s — 45 % of wall clock** |
| worst single tick | **40 ms** |
| prefix clones | **62** |
| requests actually sent | **13** |
| disk-cache write per response | **9.7 ms** |

Two structural reasons:

- the prefix is recomputed on every dirty pass even while a request is
  outstanding — 62 clones for 13 sends, i.e. most of the main-thread cost is
  thrown away;
- one request in flight means GH always solves a state one round trip old, and
  the displayed result trails by two round trips. On a heavy definition that is
  hundreds of milliseconds of lag per slider move.

The depsgraph itself cannot be made faster from Python: there is no dependency
query, no content digest, no rate control, and some writes (raw ID properties)
emit no update at all. The leverage is in *when and how often* the engine pays
the full re-evaluation, not in reading the graph harder.

### Coverage note

The repo suite (158 tests) passes because none of F1–F4's paths is covered:
tests drive `_timer()` directly and flush/raise nothing. F6 was measured ad hoc.
That gap is why the engine could be "correct" in CI and unreliable live.

---

## 3. Plan

Phases are ordered so the trigger can never go silent before anything is tuned.
Each phase ends with headless tests; the live acceptance list is at the end.

### Phase 0 — make silence explainable

- Trigger heartbeat: record the last depsgraph update time and the last pass
  that acted; Diagnostics shows `last trigger: 3.2 s ago`.
- "Why no send" reasons in the trace: `memo hit`, `cache hit`,
  `waiting on rid …`, `guard up`, `disabled`, `no stage`.
- Guard watchdog line: if `_evaluating` is set when a tick starts, log the
  traceback that left it, clear it, mark all dirty.
- Per-stage timing: prefix ms, encode ms, send→answer ms, apply ms, shown in
  Diagnostics.

**Acceptance:** a forced raise in the clone cleanup produces a console line and
a visible Diagnostics entry, and the next edit is still processed.

### Phase 1 — the trigger can never die

- Reset `_evaluating` in a nested `finally` that cannot be skipped; guard each
  cleanup step individually (`objects.remove`, `_drop_owned_data`,
  `view_layer.update`).
- Move `_drop_owned_data`'s `bl_rna` access inside its `try`.
- Watchdog at `_timer` start (belt and braces with the trace from Phase 0).
- Reset `_evaluating` in `register()` and `_on_load()`.
- Per-message `try/except` in `_handle_incoming`: one bad frame must not eat
  the rest of the batch; an apply failure clears pending and state so the next
  pass retries instead of replaying a poisoned frame.
- Make the timer's outer `except` log-safe (a closed stdout must not kill the
  timer) and move `_registered = True` after a successful `start_server()`.
- Fix the stall-retry rename hole (`_waiting_objects` keyed by name).

**Tests:** forced-raise wedge recovers; batch abort leaves later messages
intact; stage recovers after an apply raise; `_evaluating` cannot survive a
tick/register/load.

### Phase 2 — flush every Edit Mode object

- In `_timer`, flush **every** object in Edit Mode whose name is `_dirty` and
  which is a stack object, not just `bpy.context.edit_object`. Use
  `bpy.context.objects_in_mode` (fallback: scan `bpy.data.objects` for
  `mode == "EDIT"`).
- Do not early-return when the active object has no stack.
- `Push`/`Pull` flush the target object before forcing the request, so the
  never-Push-does-not-recover case (F1) disappears even without a pass.

**Tests:** multi-object Edit Mode, non-active stack object, stack object in
edit while a non-stack is active; in all three the full geometry reaches GH
without leaving the mode; Push after a stroke sends the changed geometry.

### Phase 3 — request self-healing (kills the 30 s window)

- Soft resend: while a request is outstanding, if the current state's rid
  differs and the pending send is older than ~1 s, forget it and send the
  current state (supersede). Keep the timeout as a hard fallback that resends
  in full (~5 s, configurable; 30 s is only useful for offline renders).
- Clear pending on any standalone error message.
- Consume Pull permits only after a successful send; re-arm `_verify` when a
  send is skipped (frozen/disconnected) so the stage still verifies later.
- On `unknown_ref`, bypass the cache for one pass so the promised full resend
  actually goes out.

**Tests:** stranded answer recovers in ≤ soft interval; Pull survives a failed
send; error clears pending; unknown_ref resends without a rid change.

### Phase 4 — plugin rid pinning (required before supersede ships)

- `GhLink`: snapshot `slot.Rid` when a solve is scheduled/started and expose it
  as the solve's rid; Stage Out answers that pinned rid (both ok and error
  paths). A mid-solve overwrite must not relabel an answer.
- Review `RetryStaleSolves`: keep the budget, but a new request already resets
  it; consider backing off 250 ms → 500 ms to reduce useless re-solves.
- Rebuild the `.gha` (`grasshopper/build`), reinstall, restart Rhino. Record the
  requirement in the changelog.

**Tests:** C# console tests for pinned-rid answering; live check that a
superseded request cannot produce a mismatched answer.

### Phase 5 — hash trust and repair

- Before skipping a response (or replay) because `out_hash` matches, verify the
  helpers actually hold payloads for that stage (cheap counts, not a re-read).
- If not, treat it as unbound: clear `applied_out_hash`, re-apply the stored
  frame.
- Clear `applied_out_hash` on `_bump_generation` where the cached output can no
  longer be trusted to be on screen.

**Tests:** empty helper + matching hash re-applies; Push recovers F4's mirror
case.

### Phase 6 — responsiveness (on a pipe that cannot disappear)

- Pending fast-path: check `_pending` before `_prefix_sections`; no clone while
  waiting (recovers roughly half of the F6 occupancy immediately).
- Adaptive coalescing: at most one snapshot per `max(30 ms, 3 × last prefix
  cost)`; always send the newest state; trailing edge sends the final state.
  Cheap `ref` sends (memo hit, no geometry tag) are exempt.
- Supersede within the coalescing interval (safe after Phase 4); bound
  in-flight sends by the interval.
- Move the 9.7 ms disk-store write off the critical path (writer thread or
  newest-per-burst).
- Idle tick 50 ms → 20 ms; treat incoming messages as hot.

**Acceptance:** during a 60-edit/s drag the engine's main-thread share drops
from ~45 % to ≤10 %; GH always holds a state at most one interval old; the
final value settles within one solve after the drag ends.

### Phase 7 — suite and live acceptance

Headless additions (one file per concern, following the existing pattern):
edit-mode multi-object matrix, wedge recovery, batch isolation, supersede,
error-clears-pending, hash trust, reconnect verify, coalescing bounds.

Live checklist under GH (restart Blender first; confirm the Diagnostics
"Recent activity" box is present):
1. Edit the stack object in Edit Mode, active and non-active; single and
   multi-object; strokes must reach GH without Push or tab-out.
2. Edit in object mode, sculpt, shape keys, prefix-modifier sliders.
3. Drag every parameter type; keyframes; drivers; a Python write from the
   console (documented as invisible — no depsgraph event; the panel tags).
4. Restart GH; reopen the file; verify one automatic request each.
5. Leave a drag idle and confirm the final value settles; check Diagnostics for
   the trigger/RTT lines.
6. Re-run the 60-edit drag and read the engine share from the trace.

---

## 4. Evidence appendix

- **Trigger/wedge audit** — reproductions in `/tmp/opencode/audit_wedge.py`,
  `audit_pending.py` (+ `audit_out.txt`, `audit_pending_out.txt`): wedge
  survives register/load; Push recovers a spent one-shot fault but not a
  persistent one; a response raise strands a stage with empty pending.
  Same structure at HEAD and `caad5a9`.
- **Mesh-edit matrix** — harness `/tmp/opencode/case_runner.py`,
  `run_case.sh`, results in `/tmp/opencode/out/result_*.json`. 16/17 cases pass
  (object-mode writes, edit strokes, strokes in one tick, pending strokes,
  shape keys, modifier properties, renames, two objects, hidden-in-edit-mode
  modifiers, mode exit). Failures: multi-object Edit Mode (case 14),
  stack-in-edit-while-other-active (case 15). Case 14 shows Push alone sending
  a `ref` with the stale hash.
- **GH plugin audit** — scenario table in the agent report; references
  `GhLink.cs:503/507/736/546`, `Components.cs:294/335`. Rows: coalesced/dropped
  solve with dry retry budget (best match), mid-solve rid overwrite (F5),
  dequeued-then-dropped on send failure (never answered; Push cannot cure),
  outbox eviction, reconnect clear (self-healing), wedged sender (self-healing
  via keepalive), no-document/exceptions, unclaimed keys (engine misses the
  error → timeout).
- **Response/apply audit** — ranked matrix; `engine.py:487-498` (hash trust),
  `:443-461` (errors), `:1247` (rid rewrite on replay, benign), `:1331`
  (verify consumed only on send), `:1223` (pull permit), `:570-580` +
  `:1239-1261` (unknown_ref resend swallowed).
- **Deployed code** — only one install: `~/.config/blender/5.2/scripts/addons/
  gh_stack`, symlinked to the working tree; no `bl_ext.*`; `userpref.blend` has
  the addon enabled; Python caches modules, so a **Blender restart** is required
  for code changes and the "Recent activity" box is the live tell.
- **Latency probe** — 51 076 verts, 60 edits at ~60 Hz: engine 0.96 s / 2.12 s
  wall (45 %), 62 clones for 13 sends; disk store 9.7 ms × 13 (zero-latency
  fake). Re-measured after Phase 6 with a 300 ms solve: 0.13 s / 1.30 s (10 %),
  6 clones for 4 sends, average tick 2.1 ms.

---

## 5. Implementation and acceptance status (2026-10-20)

Phases 0–6 are implemented, each as its own reviewed and tested commit:

| commit | phase |
|---|---|
| `2675672` | the depsgraph is the source of truth for the prefix (158 tests) |
| `7085324` | this report |
| `f67cb1f` | Phase 0 — a mute trigger is visible, not silent (162) |
| `481c2e0` | Phase 1 — the trigger can never die (167) |
| `a7b52c3` | Phase 2 — every Edit Mode object is flushed (171) |
| `17b63cb` | Phase 3 — a slow or lost request heals itself (175) |
| `db6380d` | Phase 4 — the plugin answers the rid its inputs were for (link tests) |
| `626e317` | Phase 5 — an emptied helper is re-applied (176) |
| `208fd0a` | Phase 6 — a wait coalesces instead of re-reading the scene (176) |

The adaptive snapshot throttle from Phase 6's plan is deliberately deferred:
the wait window is free now, and throttling cached replays would trade
playback smoothness for a cost the Diagnostics timings can prove first.

**Live acceptance passed 2026-10-19.** Blender and Rhino were restarted so the
add-on and the rebuilt `.gha` loaded, and the Diagnostics "Recent activity" box
was present. The Phase 7 checklist passed: Edit Mode (single, multi-object,
non-active), object-mode and sculpt edits, shape keys, modifier and parameter
drags, keyframes and drivers, GH-side edits and restarts, file reopen, and the
drag observed through the Recent activity ring. The choppy viewport recorded in
§6 after Phase 6 was the unconditional per-tick `_drain_depsgraph`; gating it on
work (`f69f5fc`) removed the cost and the viewport is smooth.

The S6 display half, S7 and acceptance A1–A10 passed 2026-10-20; A10 is also
covered headless by `blender/tests/headless/test_duplicates.py`. The extension
zip and `.gha` are built and verified: 0.3.0 is ready to tag and publish.

---

## 6. Open performance concerns (live, 2026-10-19)

Recorded from the owner's session after phases 0–6 landed. The viewport
choppiness reported in the same session was investigated and fixed (§5); the
instance latency below is the remaining concern and is accepted as a 0.3.0
known issue.

- **Stage In latency with instances.** One geometry instanced 13 times: the
  Stage In update in Rhino took ~300 ms typically and peaked at 800 ms, while a
  plain cube is instant. Turning preview off on every Grasshopper component
  lowers the delay. Re-testing the same file later in the session halved the
  worst case (800 → 300 ms); the session did not record whether the rebuilt
  `.gha` (Phase 4) had been loaded, so warm-up and deployed-version differences
  are uncontrolled. S3 rules out transport and conversion: 30 ms round trip and
  10 ms C# mesh build at 100k triangles.
- **Owner's read:** overall definition complexity, rather than one bridge cost.

Next measurement steps when this is picked up:

1. Env-gated timestamps through the plugin: receive → decode → scheduled →
   solve start → Stage In build (definitions vs rows) → Stage Out enqueue →
   write; Blender: request bytes and actual write completion.
2. Console benchmark of `Instances.Build` at 1 def × 1000 rows, 100 defs × 1
   row, 100 defs × 10 rows. It uses only managed Grasshopper/RhinoCommon types,
   so it runs in the console harness under wine without Rhino.
3. Preview on/off as an A/B, and the document solve time from the GH profiler.

Candidate fixes if the numbers match: skip stale-solve retries while a solve is
in flight; cache definition builds by content hash; make Stage In/Out preview
configurable (default off). Details in `docs/backlog.md` § Performance.
