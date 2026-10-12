# Handoff — stale inputs and truthful failures (next batch)

**Status:** implemented 2026-10-12. Written after the frame-cache and protocol
work landed (`0a2bf22` plugin, `c1d19d7` engine, `a8054ab` plan); revised the same
day after a code-level review, then carried out. What shipped, where it deviated
from the plan, and what is still open are at the end, under "Outcome".

**Supersedes:** nothing. `docs/polish-plan.md` remains the full backlog, with every
item's evidence; this is the next slice of it, chosen and re-justified against what
the last batch taught us.

**Baseline, all green at `a8054ab` (re-run 2026-10-12):** 60 core, 80 headless,
10 golden fixtures, installed-extension round trip. The deployed plugin is
byte-identical to the build (`grasshopper/build/out/GhStack.gha`); re-verified the
same day.

---

## The batch, and why this order

| # | Item | Why here |
|---|---|---|
| 1 | **P4-2** nested node groups are not walked | The same failure class as the bug that just cost days: a prefix edit the engine does not notice delivers stale geometry, silently. The docstring admits it. A complex GH tree is exactly the case. |
| 2 | **P3-2** the client's outbox jams on a failed send | A live stall in the client: one failed write leaves the frame at the head of the queue forever. Small, and it is the kind of thing that looks like "it randomly stopped". |
| 3 | **P1-1** `Stage Out` never answers with an error | A failing stage sits for the full 30 s timeout instead of answering `gh_solve_failed`. The protocol defines it, the fixture pins it, the engine already handles it. Only the client is missing. |
| 4 | **P2-1, P2-2, P2-3** the decoder's malformed-input contract, `proto_mismatch`, and `dtype` validation | Same build cycle as item 3, cheap, and the plugin has just been rebuilt anyway. A malformed frame currently kills a healthy connection. |
| 5 | **P0-9** coverage and deployment discipline | The reason items 1–4 are findable at all. Two concrete pieces: a `fake_gh` mode that coalesces, and a script that says whether the deployed plugin is current. |

Items 1 and 2 are independent of everything else and can go in either order. Items
3 and 4 both land in the same `.gha` and want one rebuild-and-deploy cycle. Item 5 is
the only one that is pure infrastructure, and it is what makes the next handoff
cheaper.

**Deliberately not in this batch:**
- **P4-1** (revision cost) — re-scoped after profiling: 89% of the memo-hit cost is
  the geometry read, and the two things the original item wanted to gate are 0.41 ms
  combined. See the plan for the numbers. Only "reuse buffers" is worth doing, and
  it is not urgent.
- **P4-3** (collections) — checked against every modifier type: none of the four
  that have a collection is an input this protocol carries. Closed, not fixed.
- **P0-6** (units off-thread) — real but unobservable: its output feeds a `hello`
  field nobody reads. Hygiene.
- **P5** (codec parity), **P6/P7/P8** (mechanical cleanup) — real, low risk, and
  better done when someone is already in those files.

---

## Standing constraints from the owner

Quoted, because they decide several design questions below:

- *"The solution MUST be clean and minimal, if we need to simplify the logic and
  make the bake a static toggle or ditch it all together I would consider it."*
- *"I really want to pursue the blender-esque approach to simulation nodes — to bake
  on already evaluated frames as one passes through them."*
- *"If an evaluation did not finish do not put anything into a queue of evaluations.
  One evaluation at a time for now."*
- *"Feel free to make cleanups."*

The second and third together are the tension to hold: the cache should fill by
dwelling on frames, but must never build a queue at the client. The current design
resolves it with one in-flight request and a cache lookup *before* the gate, so
replays are never throttled.

---

## Where things stand

### The engine (`blender/gh_stack/blender/engine.py`)

One pass per stage: prefix sections → geometry hash → request id → cache → send.
The pieces an implementer needs to know:

- **`_prefix_sections`** returns what a stage sees: the base datablocks when nothing
  precedes the stage's modifier, otherwise a clone evaluated with the stage's
  modifier and every later one removed. The result is memoized in `_prefix_memo`,
  keyed by `(obj.as_pointer(), stage.key)`, against a `_prefix_revision` tuple.
- **`_pending[(stack_id, stage_key)]`** holds at most one entry: the evaluation in
  flight. The pass returns `waiting` while it is there — it does not queue the frame
  the playhead has moved to.
- The **cache lookup runs before the gate**, so a cached frame applies immediately
  even with a request outstanding. This is what makes a replay smooth.
- **`stats`** carries `timeouts` and `undelivered` for diagnostics;
  `undelivered` counts requests no client socket could carry.

### Three invariants that must not be broken

Each was a shipped bug within the last day. Any change to the pass should be checked
against all three.

1. **`_prefix_revision` must name every input the evaluated prefix reads.** An input
   missing from it is not a performance problem, it is wrong geometry: the memo
   returns an earlier evaluation and nothing reports it. The frame
   (`scene.frame_float`) and the object identity are the two that were missing; the
   docstring now says so.
2. **`stage.rid` names the request the stage last asked for** — written when a
   request goes out or when a cached frame is applied, never for a frame the scene
   has merely moved to. Writing it on every pass is what made playback freeze.
3. **At most one request outstanding per stage.** A cache hit must not clear the
   in-flight entry: we may no longer want the answer, but the client is still
   solving it, and opening the gate early let two requests out at once.

### Numbers worth not re-deriving

| measurement | value |
|---|---|
| scrub of 24 frames, 400 ms round trip | 3 requests (was 24) |
| same, 800 ms round trip | 2 requests |
| peak concurrent requests | 1 (was 2) |
| convergence after stopping, 1.5 s solve | 2.7 s, deterministic (was 2.7–4.2 s) |
| prefix: memo hit vs clone, 4k tris | 0.99 ms vs 4.1 ms |
| prefix: memo hit vs clone, 50k tris | 8.3 ms vs 45 ms |
| of the 8.3 ms memo hit, the geometry read | 7.35 ms (89%) |

---

## Item 1 — P4-2: nested node groups are not walked

**Where:** `engine.py:881-884`, `_node_tree_signature`. Its own docstring says
"Nested groups are not walked."

**Failure:** a prefix modifier whose node group contains a *nested* group — the
normal way to organise a real graph — does not invalidate the memo when the nested
group is edited. The stage then sends the geometry from before the edit, and nothing
reports it. Verify the failure first with a test before designing the fix; the same
class of bug was misdiagnosed three times in the last batch, so the reproduction is
worth more than the design.

**Design:**
- Recurse into every node that has a `node_tree` (a Group node, and the geometry-node
  variants that hold sub-graphs — check which node types expose one rather than
  assuming).
- Carry a **visited set keyed by identity** (`as_pointer()`), because a recursive
  group is legal in Blender and a naive walk will not terminate.
- Keep a **depth cap** as a backstop, and record when it is hit so a truncated
  signature is not silently accepted.
- Reuse the existing per-tree logic (nodes, input socket defaults, links, interface)
  for each tree rather than writing a second walker.
- Keep the walk **deterministic**: sort links as the current code does, and sort
  nested trees by something stable (`session_uid` or name) so two runs agree.

**Cost:** `_node_tree_signature` is 0.20 ms for a 42-node graph. Even with recursion
it stays far below the geometry read, so do not optimise it.

**Tests (layer B, `scripts/test-blender`):** build a prefix group containing a nested
group, reach `ok`, then edit a node *inside the nested group* and assert the stage
requests again and the geometry follows. That is the regression test for the user's
exact workflow.

**Effort:** M.

---

## Item 2 — P3-2: the client's outbox jams on a failed send

**Where:** `grasshopper/src/gh/GhLink.cs`, `SendFrames` and `Enqueue`.

**Failure:** two problems in one path.
1. **A live stall.** `SendFrames` peeks the head, sends outside the lock, and on an
   exception `return`s **without dequeuing**. That frame stays at the head and every
   later frame queues behind it, permanently.
2. **Unbounded growth.** Nothing caps the queue and nothing clears it on disconnect,
   so a stalled link accumulates a frame per solve and a reconnect delivers the whole
   backlog.

**Fix:** dequeue-on-failure first — that is the stall. Then bound the queue and
`lock (Gate) Outbox.Clear()` in `Loop`'s `finally`. Follow the owner's constraint:
**one frame at a time, nothing queued.** The simplest honest bound is to keep only
the newest frame per stage, or to drop the frame and let the next pass re-derive it
from the cache — do not build a priority queue.

**Tests:** the plugin has no automated coverage, so this is inspection plus a live
session. If item 5's contract test lands first, the queue's behaviour can be asserted
without Rhino by driving the send path with a fake client — consider ordering item 5
ahead of this one if that turns out to be cheap.

**Effort:** M.

---

## Item 3 — P1-1: `Stage Out` never answers with an error

**Where:** `grasshopper/src/gh/Components.cs:244-248`. The `catch` sets a local
`status` and adds a runtime message, then falls through to `da.SetData(...)`. **No
response frame is enqueued**, and `slot.Status` is never set to `"error"` — only
`"ok"` is, at `:240`.

**Failure:** Blender waits out the full timeout for a stage that already failed. It
should get an immediate answer.

**The answer already exists in the protocol**, and everything on the Blender side is
written and tested: `engine.py:272` handles `status == "error"`, the fixture
`protocol/fixtures/response-error` pins the shape:

```json
{"type":"response","status":"error","out_hash":"",
 "error":{"code":"gh_solve_failed","message":"Component 'Loft' failed: no input"},
 "warnings":[],"counts":{"meshes":0,"subd":0,"curves":0,"points":0,"approximated_curves":0}}
```

**Fix:** in the `catch`, build and enqueue exactly that, reusing `slot.Rid` and the
stage key. Set `slot.Status = "error"` under `Gate` — note that `RetryStaleSolves`
treats anything other than `"waiting"` as settled, so this also stops the retry loop.
Error responses are not cached (`_store_frame` returns early on a non-`ok` status),
so a retry is a fresh attempt, which is what you want.

**Tests:** `tools/fake_gh` can already send `status: "error"` (it does, for
`unknown_ref`). Add a mode that fails a solve — `--fail-after N` — and assert in
layer B that the Blender stage reaches `error` **without waiting for the timeout**
(assert on elapsed time, not just on the state, or the test passes for the wrong
reason). Note the layer boundary: layer B drives the fake, so this pins the
*engine's* error path (only the `unknown_ref` flavor is covered today); the
`Components.cs` half cannot be reached by any automated suite and needs inspection
plus a live session.

**Effort:** M.

---

## Item 4 — P2-1, P2-2, P2-3: the decoder's contract

Different files from item 3 (`Json.cs`, plus a few lines each in `Frame.cs` and
`GhLink.cs`), but one edit–build–deploy cycle: all of it lands in the same `.gha`.

- **P2-1** — `Json.cs:119` and `:147-154`: the parser's contract is `JsonException`
  (the one exception `Frame.Decode` catches) but it throws `FormatException` /
  `ArgumentException` / `OverflowException` for `\uZZZZ`, `1.2.3`, `1e`. `GhLink.ReceiveLoop` catches only
  `FrameException` and `JsonException`, so anything else escapes into `Loop`'s
  catch-all and **tears down a healthy connection**. Both halves needed: make the
  conversions raise `JsonException`, *and* add a last-resort `catch (Exception)` in
  `ReceiveLoop` that notes and continues.
- **P2-2** — `Frame.cs:195-198`: `"v": "x"` makes `Convert.ToInt64` throw
  `FormatException`, and the message builder throws again through `Convert.ToDouble`
  and `Json.FormatDouble`, so the one error the protocol mandates for a version
  mismatch is an unclassified exception. After P2-1 it would merely be swallowed
  instead of answered. Require an integral JSON type (`Json.IsNumber` already
  exists) before converting, and build the message with `Convert.ToString` rather
  than the double formatter. This also settles P5-2.
- **P2-3** — `Frame.cs:219-227` accepts any `dtype` other than the `"u8"` special
  case, so C# accepts frames Python rejects (`codec.py:88`, `:105`); and `:211` reads
  `name` through `Json.GetString`, which returns `null` — a name `DecodedFrame.Find`
  can never match, so the frame is accepted and its geometry is silently unusable.
  Validate `dtype` against `{f32,u32,u8}` and require a non-empty `name`.

**Tests:** `grasshopper/tests/console/Program.cs` runs without Rhino, so these *are*
testable — add malformed-frame cases there, mirroring the coverage
`blender/tests/core/test_core.py` already has. That file is the one place the plugin
side can be tested without wine's Rhino, and it is under-used.

**Effort:** M.

---

## Item 5 — P0-9: coverage and deployment discipline

The last batch's costliest lesson: **the suites were green while the shipped product
was broken**, because every layer B test drives `tools/fake_gh`, and the fake was
*more protocol-correct than the client*. The fake answered `unknown_ref`; the plugin
dropped the request. `test_reload.py` therefore proved the engine survives a reopen
while the product hung.

Three concrete pieces, in order of value:

1. **A deployment check.** Nothing detects that Rhino is running an older `.gha`
   than the build. A `scripts/check-deployed` that compares
   `grasshopper/build/out/GhStack.gha` against the copy under
   `$WINEPREFIX/drive_c/users/*/AppData/Roaming/Grasshopper/Libraries/` and warns on
   a mismatch would have saved a day. Call it from `scripts/test-gha` and mention it
   in the README's install section.
2. **A `fake_gh` mode that coalesces.** The plugin collapses `ScheduleSolution`
   calls and may answer only the most recent request. That behaviour is what hid the
   swallowed-answer defect in P0-4 — the fake could not reproduce it, and it was
   found by reading the C# and instrumenting Blender's own concurrency. A
   `--coalesce` mode (answer only the newest request, drop the rest) makes that class
   of defect reproducible in the suite. **Consider doing this before item 1**: it
   makes the highest-risk bugs in this batch testable.
3. **Units.** The plugin converts document units ↔ metres through
   `DocumentScale()`; the fake never converts. The development document is in metres
   (confirmed 2026-10-12), so this is latent — but the conversion is silent in both
   directions and the first non-metres document would be unverified. Lowest priority
   of the three.

**Effort:** M for pieces 1 and 2; L for piece 3.

---

## Working rules this batch should keep

Learned the hard way in the last one; they cost hours each.

1. **Instrument before theorising.** Three attempts were spent on wrong theories
   about the playback bug before wrapping `_forget` and printing its caller — which
   found it in one run. `engine.stats` exists for this; add a counter rather than
   reasoning about a call graph. Likewise, verify call sites before claiming an
   invariant is broken (P0-6 was filed wrongly by inferring a call graph from a
   call-graph description).
2. **Every fix gets a negative control.** Remove the fix, watch the new test fail,
   put it back. Both P0-8 and P0-4's concurrency fix have one, and both were needed.
3. **Check the deployment before trusting a negative result.** A C# fix is invisible
   until Rhino restarts, and `grasshopper/deploy/deploy.sh` reminds you but nothing
   enforces it. `stat` the deployed `.gha` against the build.
4. **The fake is the stricter peer in some ways and the weaker one in others.** It
   answers every request and implements `unknown_ref` (stricter than the pre-fix
   plugin); it never coalesces and never converts units (weaker). Know which way
   round a given test is.
5. **`Blender`'s `_timer` only processes objects something marked dirty.** A test
   that calls `_timer()` in a loop with an empty `_dirty` set does nothing — mark
   dirty explicitly, or drive the depsgraph. Several tests are written this way for
   exactly this reason.
6. **`frame_change_post` and friends need `@bpy.app.handlers.persistent`**, or a file
   load silently removes them. The timer needs `persistent=True` at registration for
   the same reason.
7. **Keep it minimal and knobless.** Where a knob was tempting (an in-flight limit
   above one), the reason it was refused is in the code: a limit above one needs a
   rule for which frame gets dropped, and any such rule guesses what the user will
   look at.

---

## Verification

```
scripts/test-core         # layer A: pure Python core, no Blender
scripts/test-blender      # layer B: Blender headless against tools/fake_gh
scripts/test-gha          # layer C: the C# codec under wine
scripts/test-extension    # layer D-lite: build, install, round trip
```

Baseline to beat: **60 / 80 / 10 fixtures / PASS**. Items 3 and 4 also want
`grasshopper/build/build-gha.sh` (which compiles `GhLink.cs` and `Components.cs`
against RhinoCommon — `scripts/test-gha` builds only the codec) and then
`grasshopper/deploy/deploy.sh` plus a Rhino restart.

For items 1 and 2, add the failing case to the suite *before* the fix and keep the
negative control in the commit message.

---

## Open questions

1. **Does a re-bake change a prefix's evaluated geometry with no property or
   node-graph change?** If it does, `NodesModifier.bakes` is a staleness source the
   revision cannot see — narrow, but the same class as everything in this batch.
   Confirm before treating it as real.
2. **Should `Publish` refresh `hello` when the document is saved?** The plugin sends
   `hello` only when `!_publishedThisConnection || stagesChanged`, and `doc` is a file
   path — so saving an unsaved GH document can make a later reconnect look like a
   document change and drop the frame cache. Harmless, surprising, and untested.
3. **Is one evaluation at a time still right once the cache is warm?** The cache
   lookup runs before the gate, so a warm replay is not throttled. If a slow first
   pass over a large range is ever a problem, the answer is the per-frame *prefix*
   cache (plan P4-1 option 3), not a wider in-flight limit.
4. **When is the plugin's `Slots` registry pruned?** It grows without bound (P3-5),
   and it is never cleared. If a stage key is reused after a document change, a stale
   slot could serve geometry for the wrong definition. Worth a look while in that
   file for item 2.

---

## Outcome (2026-10-12)

All five items landed, with three deviations from the plan above:

1. **P2-2 joined item 4** after review — same teardown class, same files, S —
   `34499a0`.
2. **The engine half of item 3 changed behavior.** `_handle_response`'s generic
   error branch marked the object dirty, re-sending the request on the same tick;
   with a solve that just failed, that is a request loop against the client.
   Protocol §7 says an errored stage retries on the next change or Push, so the
   dirty-add is gone. `SolveFailureTests` pins both the fast error and the
   no-immediate-retry.
3. **`SchemaResetTests` was racy**, and the new class exposed it: the schema landed
   while frame 1 was the playhead, so the engine re-evaluated and re-cached frame 1
   under the new generation, and the "requested again" assertion passed only when
   the frame-2 request count happened to land after the budget was recorded. Its
   script now visits both frames before the change — `74df6fb`.

Also: `P8-2` was resolved by deleting the superseded frame-cache handoff and
updating ADR 0008; `P0-9` pieces 1 and 2 landed (check-deployed, `--coalesce`);
piece 3 (units) stays backlog along with the plugin-contract test.

Still open from this batch:

- **The two plugin halves — P3-2's outbox and P1-1's error response — are compiled
  and deployed but not exercised by any suite.** They need a Rhino restart and a
  live session: a failing `Stage Out` should answer immediately (not after 30 s),
  and a failed send should not jam later frames. `scripts/check-deployed` confirms
  the deployed `.gha` is current (`c9001aca…`).
- The live session is also where P2-1..P2-3's runtime effect is seen (a malformed
  frame no longer drops the link).
- Open questions 1–4 above remain open as written.
