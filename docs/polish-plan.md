# Polish and cleanup plan

> **Historical record.** This is one review, kept for its measured profiles and
> the reasoning behind the items that were deferred. It is not the plan: the live
> list is `docs/backlog.md`, and resolved items are in the commits and the
> CHANGELOG. Line numbers and function names below refer to the tree as it was
> when the review was written.


**Status:** in progress. Written 2026-10-12 from a full read of the tree at
`ea0f651`, after the W2 frame cache (`b3305cf`) and the reopen fix (`ea0f651`);
items carried out since carry a **Done** banner with commit and evidence.

**Next slice:** `docs/backlog.md`. (The handoffs this plan fed — `handoff-next-batch.md`
and `handoff-next-batch-2.md` — were folded into it once their items were done or
re-stated, 2026-10-15.)
Its findings that are not yet items below are tagged **`N1`–`N9`** there — notably
`N4` (a measured per-tick cost that re-scopes **P4-1**, see item 5 of that handoff)
and `N1` (a gap in the decode contract that `P2-1`/`P2-3` left one layer deeper).
Fold those in here when this plan is next touched.

**Scope:** correctness fixes, hardening, dead-code removal, and documentation
drift. No new features and no protocol changes. Every item either removes a silent
failure, removes a stall, or removes code that is not reachable.

**Not in scope:** the roadmap in `docs/plan.md` (W3 onward, the S6 display half,
S7). This document does not touch it.

---

## 0. How this is ordered

Ordered by **silent failure × blast radius ÷ cost**, not by code smell.

| Tier | What it is | Behaviour change? |
|---|---|---|
| P0 | Produces wrong output without saying so | yes, needs a test |
| P1 | Stalls the product on a path that already fails | yes, needs a test |
| P2 | Decode boundary: a malformed frame must not win | yes, needs a test |
| P3 | Grasshopper concurrency and resource bugs | yes, needs a test |
| P4 | Engine cost paid on every idle tick | yes, needs a test |
| P5 | The two codecs disagreeing | yes, needs a test |
| P6 | Mechanical cleanup | **no** — suites stay green |
| P7 | Repo hygiene | no |
| P8 | Documentation drift | no |

Items are tagged `P<tier>-<n>`. Each carries an effort estimate (**S** ≈ under an
hour, **M** ≈ half a day, **L** ≈ a day or more) and how it is proved done.

Baseline at `ea0f651`, all green: **60 core, 72 headless**, 10 golden fixtures,
installed-extension round trip.

```
scripts/test-core         # layer A: pure Python core, no Blender
scripts/test-blender      # layer B: Blender headless against tools/fake_gh
scripts/test-gha          # layer C: the C# codec under wine
scripts/test-extension    # layer D-lite: build, install, round trip
```

---

## P0 — Silent failures

Nothing here raises, logs, or turns a test red. That is the whole problem.

`P0-1`..`P0-3` are one bug seen from three sides, confirmed by reproduction on
2026-10-12 (see "Reproduction" below). A `.blend` that is reopened can enter a
state where **the stage never receives geometry again and never reports an
error**; the only escape is deleting and re-creating the stage. Fix all three:
each is independently sufficient to keep the loop broken.

> **Done 2026-10-12.** `P0-1`..`P0-8` are fixed, with layer B coverage. `P0-9`'s
> deployment check and coalescing fake landed the same day; its plugin-contract test
> and units path stay open. It is the finding that made half of this tier possible
> to miss.
> **`P0-8` is the one reported from a live scene** (a prefix modifier reading
> scene time); the rest were found while investigating it or during the review.
> Read `P0-8` first if you are looking for why a timeline-driven stage did not
> move.
>
> A fourth hole surfaced while fixing `P0-1`: **Blender had no dispatcher for the
> standalone `error` message at all** (`protocol.md` §4.6) — it only understood
> the error-*response* shape that `fake_gh` happened to send, which is why the
> suites never noticed. `_handle_incoming` now routes `type == "error"` to
> `_handle_error`, and `fake_gh` gained `--refusal` so both shapes are covered.
> Two more came out of playback testing: `P0-4` (the frame-evaluation design) and
> `P0-5` (a request with no client stalled a timeout). See `CHANGELOG.md`
> (Unreleased → Fixed).

### P0-1 · The GH side never answers `unknown_ref` — the blocker
**Where:** `grasshopper/src/gh/GhLink.cs:236-258`. `mode` is read at `:244` and used
only at `:258` (`if (mode == "full") slot.Sections = decoded.Sections`). A `ref`
for geometry the slot does not hold falls straight through and is dropped: no
reply, no note.
**Why it matters:** `unknown_ref` is the protocol's *only* recovery path for a
`ref` (`docs/protocol.md:127`; Blender's half is implemented at
`engine.py:238-248`). Without it, a `ref` that GH cannot serve is unrecoverable —
the request is lost, Blender waits out its timeout, and the retry is another
`ref`. Confirmed: 8 consecutive `mode=ref held=False` requests, every one
dropped, `state` stuck at `waiting` with `error` empty for the whole run.
**Fix:** track the last `geom.hash` received per slot. On a `ref` with no matching
sections, enqueue an `error` frame with `code: "unknown_ref"` — the shape
`protocol/fixtures/error-unknown-ref/` already pins. Add a `type == "error"`
branch in `Handle` so inbound `proto_mismatch` / `bad_frame` / `too_large` /
`duplicate_stage` / `unknown_stage` surface in `slot.Message` and `Note` instead
of being logged as `"ignoring message type"`.
**Verify:** `scripts/test-gha`. Add a "forget the geometry" mode to
`tools/fake_gh` so layer B can assert the whole round trip.
**Effort:** M.

### P0-2 · `stage.geom_hash` survives a file load — the trigger
**Where:** `blender/gh_stack/blender/engine.py:78-99` (`_on_load`). It resets
`_waiting_since`, clears `_prefix_memo`, and resets `waiting` stages to `idle` —
but leaves `stage.geom_hash` intact. `_process_stage` then picks its mode at
`:608`: `mode = "full" if geom_hash != stage.geom_hash else "ref"`.
**Why it matters:** `geom_hash` is persisted in the `.blend` and means "the last
geometry I sent to GH". After a reopen, Grasshopper may be a different process, a
different document, or simply have lost its slots — so that claim is no longer
safe. The very first request after a load is therefore a `ref`, i.e. exactly the
mode GH cannot serve (P0-1).
`docs/protocol.md:207` already states the rule for the analogous case — "Blender
sends the current `request` for each stage, with `geom.mode = "full"` for the
first request after a connection (GH may have lost its geometry)". A file load has
the same property and is not treated the same way. Note `_on_client_hello`
(`:150-181`) also does not clear it.
**Fix:** in `_on_load`, clear `stage.geom_hash` for every stage, exactly as the
`unknown_ref` handler already does at `:242-243`. The first post-load request then
carries `geom.mode = "full"` and the whole scenario disappears.
**Verify:** with the fix simulated, one `full` request, `held=True`, `state=ok`,
`sends=1`, no retry loop. See "Reproduction".
**Effort:** S.

### P0-3 · The timeout sets `error` and the next pass silently overwrites it
**Where:** `blender/gh_stack/blender/engine.py:597-606` (timeout branch) and
`:608-627` (send block).
**Why it matters:** when the timeout fires it sets `STATE_ERROR` and an
`error` string. On the very next pass the stage is no longer `waiting`, so the
timeout branch is skipped, control falls through to the send block, and the state
is set back to `STATE_WAITING` (`:624`) with `error` cleared (`:625`). The user
therefore **never sees the failure**: the stage flickers to `error` for under one
tick and reads `waiting` afterwards, forever. Worse, the retry recomputes the
same `rid` and the same mode, so it retries the identical request that just
failed — an infinite loop at one request per timeout, with no diagnostic.
Confirmed: with the timeout shortened to 5 s, `state` read `waiting` at every one
of 8 retries and `error` stayed empty throughout.
**Fix:** on timeout, remember the mode/rid that failed for that stage and force
the next request to be `full` (a `ref` that timed out must never be retried as a
`ref`). Additionally, keep the failure visible: either leave the stage in `error`
until something changes, or surface the retry count so the panel does not claim
health. A retry loop that reports `waiting` is the reason this bug went unnoticed.
**Test:** a layer B test that sends a request, never answers it, and asserts that
(a) the stage reaches `error` and stays there, and (b) the retry is `full`, not
`ref`.
**Effort:** M.

### Why the suites did not catch this
`tools/fake_gh/fake_gh.py` implements `unknown_ref` **correctly** — if it holds no
sections it refuses, and Blender's half of the recovery is exercised on every
reopen test. So layer B's `test_reload.py` passed from the start: Blender sends
the `ref`, the fake refuses, Blender clears `geom_hash` and resends `full`, and the
test asserts `state == "ok"`. **The fake was more correct than the real plugin**,
so the suite proved the engine survives a load while the shipped product could
not. Two things fixed that: the fake's refusal is now switchable
(`--refusal message|response`) and the *plugin* is what the C# side must match;
and the standalone `error` dispatcher Blender was missing is now covered, so a
spec-shaped refusal can no longer pass unnoticed.

### Reproduction (2026-10-12)
Two processes, `blender -b --factory-startup`, real `tools/fake_gh` for phase 1
and a variant that mimics `GhLink.Handle` (drops an unservable `ref`) for phase 2:

| Step | Result |
|---|---|
| Phase 1: cube + stage, reach `ok`, save `.blend` | `state=ok`, `helper_max=1.0`, 1 `full` request |
| Phase 2, warm disk cache, reopen | **`sends: 0`** — the cache replays the frame and GH is never contacted at all |
| Phase 2, cold disk cache, reopen | 1 `ref` → dropped → stuck `waiting`, `sends: 1` |
| Phase 2, cold, `timeout=5s`, 40 s of pumping | 8 × `mode=ref held=False`, all dropped; `state=waiting` throughout, `error` empty |
| Phase 2, cold, with `geom_hash` cleared on load | **1 × `mode=full held=True` → `state=ok`, `sends=1`** |

The warm-cache row matters on its own: a reopen with an intact cache sends
nothing, so the session is never re-established and the *first* traffic after a
load is the `ref` from P0-2. Replay-with-GH-closed is the intended design; what is
missing is the re-sync that should accompany it.

### P0-4 · Playback: one evaluation at a time, and nothing queued
**Status:** found and fixed 2026-10-12, after W2. Superseded once: see the note.
**Where:** `blender/gh_stack/blender/engine.py` — the per-stage in-flight gate
(`_pending`) and the per-frame evaluation path in `_process_stage`.
**Why it matters:** two defects with one cause — the engine tracked "a request is
in flight" but not what that meant for the frames that followed.
1. **Most frames were never evaluated.** With one request in flight and a gate that
   returned `waiting`, a frame that arrived while an earlier request was
   outstanding was not requested at all: the pass returned `waiting` and the frame
   was lost. Measured: a pass over 10 frames sent 3 requests. Because the frame
   cache is filled by evaluating frames, playback could never get smoother.
2. **`stage.rid` was written on every pass** rather than when a request went out,
   so a reply still in flight no longer matched the stage and was discarded as
   stale. With a round trip slower than the frame interval no reply ever matched
   and the display never changed.
**Superseded:** the first fix removed the gate and asked for each frame as it
arrived, abandoning the request in hand. That made the engine request every frame
of a scrub (24 of 24, measured), which **queued a backlog at the client**:
Grasshopper solves on one thread in arrival order, so it ground through frames the
user had already scrubbed past and stopped responding to them. The owner's call,
2026-10-12: *"If an evaluation did not finish do not put anything into a queue of
evaluations. One evaluation at a time for now."*
**Design as shipped:** `_pending[(stack_id, stage_key)]` holds at most one entry —
the rid being waited for, with the time it was sent. While it is there the stage
requests nothing else, not even the frame the playhead has moved to; it asks for
whatever frame the playhead is on when the answer lands. Every answer is stored
whether or not it is still wanted, so an evaluation is never wasted — a frame
evaluated once replays from the cache for free. A cached frame never reaches the
gate, because the cache lookup runs first, so a replay is not throttled by an
outstanding request.
**Third defect, found while testing that one:** the guarantee was not actually
held. A cache hit answered while a request was in flight cleared that request's
bookkeeping, so the next pass saw an open gate and sent a second request. At the
client that is the same backlog by another route, and because the plugin
coalesces its solves it can reply for a request the engine has stopped waiting
for — an answer with no matching rid, swallowed without starting the next request,
which is how a stage came to look stuck until a manual **Push**. Measured by
instrumenting the engine's send and response paths while scrubbing over a mix of
cached and uncached frames: a peak of **two requests in flight**, 11 requests
where 7 do the same work, and a convergence after stopping that varied between
2.7 s and 4.2 s for the same work. The in-flight entry now survives a cache hit:
it goes when the answer arrives, when the timeout gives up on it, or on an
explicit reset (Push, a client `hello`, a load) — the three cases where the request
genuinely no longer matters. A swallowed answer for a frame the playhead has left
also marks the stage for a pass now, so the chain that asks for the current frame
cannot stop.
**Result:** 24 frames scrubbed at 20 fps → **3 requests** at a 400 ms round trip,
**2** at 800 ms, against 24 before; peak concurrency **1**; convergence after
stopping **deterministic at 2.7 s** (a 1.5 s solve) where it previously varied
between 2.7 s and 4.2 s. The residual two solves are inherent to one-at-a-time:
the request already in flight when the playhead stops must finish before the
stopped frame can be asked for.
**Not done, deliberately:** no configuration knob. A limit above one needs a rule
for *which* frame is dropped when the queue is full, and any such rule is a guess
about what the user will look at. Revisit only with a reason.
**Tests:** `test_frame_cache.OneEvaluationAtATimeTests` (a frame while one is
outstanding is not queued; the answer lands and the current frame is asked for
next), `PlaybackTests` (a scrub does not ask for every frame it passes; the frame
it stopped on lands and then replays) and `OneRequestOutstandingTests` (the
concurrency invariant, which fails with the one line restored: `up to 2 requests
were outstanding at once`).
**Effort:** M.

### P0-5 · A request with no client socket stalled the stage for a whole timeout
**Status:** found and fixed 2026-10-12, while testing P0-4.
**Where:** `blender/gh_stack/blender/server.py` (`GhServer.send`) and the engine's
send path.
**Why it matters:** `send` returned silently when there was no client, and
`bridge.state["connected"]` is set from `hello` and can lag the socket. The engine
therefore recorded a pending request that nothing would ever deliver, and waited
the full timeout for an answer nobody had been asked for — then the retry was
gated on that same pending entry. Found because a test class ticked the engine
once before its fake connected and lost 30 s per stage.
**Fix:** `send` returns whether at least one client took the frame; the engine
counts it under `stats["undelivered"]` and leaves the stage `disconnected` rather
than pending. `bridge.state["clients"]` now carries the socket count, which is the
accurate "can a request go out" signal (`connected` only means a `hello` was seen).
`GhServer._clients` is also created in `__init__` now rather than by `hasattr`.
**Tests:** covered by the waiting/late-client tests in `test_prefix.py` and by
`FrameCacheTestCase.await_client`, which waits on `clients`, not `connected`.
**Effort:** S.

### P0-6 · `RhinoDoc.ActiveDoc` read from the socket thread
**Where:** `grasshopper/src/gh/GhLink.cs:530-538` (`UnitsName`), reached from
`SendPublication` (`:487`), which runs on the socket thread (`:176`), the sender
thread (`:311`), and the UI thread (via `Publish`, `:372`).

> **Corrected 2026-10-12, after checking the call sites instead of inferring them.**
> This item previously named `DocumentScale()` as a second offender and claimed the
> consequence was *"every mesh in the session silently scaled wrong"*. Both were
> wrong:
>
> - **`DocumentScale()` is not read off the UI thread.** It has exactly two call
>   sites, `Components.cs:92` and `:216`, both inside `SolveInstance` — the UI
>   thread. Only `UnitsName()` breaks the threading rule.
> - **`UnitsName()`'s output is consumed by nobody.** It feeds
>   `hello["doc_units"]`, and no reader exists: not Blender (`_on_client_hello`
>   reads `doc` and `stages` and nothing else) and not `fake_gh`'s tests. Nothing
>   scales geometry from it.
>
> The threading violation is real and worth fixing on principle — the file states
> the invariant in its own header — but it is **hygiene, not a correctness bug**,
> and nothing in a live session can currently observe it. The severity below is
> what was originally written; take the correction as the accurate one.

**Why it matters:** `GhLink.cs:7-12` states the invariant "GH objects only on the
UI thread", and `UnitsName` breaks it. `RhinoDoc.ActiveDoc` and `ModelUnitSystem`
are not safe off the UI thread, and the read is wrapped in `catch (Exception) { }`
falling back to `"Meters"`, so a failure is invisible.
**Fix:** resolve the units on the UI thread and cache them in a `volatile` field,
publishing from `Publish(doc)` (`:372`), which already runs there. Replace the bare
catch with `Note(...)` so a failure is visible.
**Verify:** `scripts/test-gha`; a live session to confirm `hello` still reports the
document's units.
**Effort:** S.

### P0-7 · `return` inside the loop over objects in `_handle_response`
**Where:** `blender/gh_stack/blender/engine.py:248`, `:252`, `:285`, `:300` — all
inside `for obj in bpy.data.objects:` which opens at `:223`.
**Why it matters:** the first object that owns a matching stage key ends the whole
loop. A second stack object claiming the same stage key **never receives the
response**: it waits out its timeout if the answer was an error or `unknown_ref`
(`:248`, `:252`), and silently keeps stale geometry if the answer was a normal
result (`:285`). Blender permits the duplicate, so the failure is invisible and
intermittent.
**Fix:** `continue` at all four. They are per-stage outcomes, not
per-object ones. Note `_handle_schema` (`:183`) and `_on_client_hello` (`:150`)
already use `continue` correctly, so this is an inconsistency, not a
misunderstanding.
**Test:** a layer B test with two stack objects sharing one stage key, asserting
both reach `ok`. No such test exists today.
**Verify:** `scripts/test-blender`.
**Effort:** S (fix), M (test).

---

### P0-8 · The prefix memo ignored the frame — one frame's geometry answered them all
**Status:** found and fixed 2026-10-12, reported from a live scene.
**Where:** `blender/gh_stack/blender/engine.py`, `_prefix_revision` — the tuple it
returns feeds the memo in `_prefix_sections`.
**Why it matters:** this made the whole frame cache inert for any scene whose stage
depends on the timeline. The user's setup: a native Geometry Nodes modifier before
the GH stage reads a **Scene Time** node, so the geometry fed to Grasshopper moves
with the frame. The revision listed base geometry, modifier properties, node-graph
wiring, upstream outputs and shape keys — every one of them identical across
frames, because a Scene Time node is read at *solve* time rather than stored on the
modifier. So:

    frame change → revision identical → memo hit → same prefix
                 → same geometry hash → same request ID → cache hit
                 → Grasshopper never asked → the stage never moves

Measured: a prefix offset by 1.0 at frame 24 still served the frame-1 geometry, and
`stage.rid` was byte-identical at frames 1 and 24. This is the bug that survived
four rounds of cache work, and it is why nothing else that was fixed appeared to
change anything: the engine never reached a new request ID, so the request path,
the in-flight rules and the cache were never exercised.
**Fix:** `bpy.context.scene.frame_float` is part of the revision, and the
docstring now states the invariant the function has to hold — **every input the
evaluated prefix reads belongs in that list** — naming the two that are easy to
miss (the frame, and the object identity, which lives in the memo key). A missing
input is not a performance problem, it is wrong geometry with nothing to show for
it.
**Two holes fixed with it:** the memo was keyed by object *name*, so an object
could in principle inherit what a different object of that name had evaluated (now
`as_pointer()`, which holds no reference); and `register()` cleared only the disk
stores, so a decoded frame, an evaluated prefix, a pending request and the counters
survived a disable/enable.
**Cost, measured:** the revision is built on every pass, not only on a clone, so a
memo hit costs **0.99 ms at 4k triangles and 8.7 ms at 50k**, against 4.1 ms and
45 ms for the clone. During playback the clone is paid per frame — unavoidable
while the request ID is content-addressed over the *input* geometry, since the
input has to be evaluated to know which frame's answer to replay. See P4-1.
**Tests:** `test_frame_cache.TimeDrivenPrefixTests` (3 tests). Negative control:
removing the one line makes all three fail with `frame 24 never reached the helper:
max x 1.042, expected 2.000` — frame 24 receiving frame 1's geometry.
**Effort:** S.

### P0-9 · The real plugin has no automated coverage at all
**Where:** every layer B and layer D test drives `tools/fake_gh`
(`test_engine_demo`, `test_geometry_kinds`, `test_prefix`, `test_frame_cache`,
`test_reload`, `tests/installed/run.py`). `scripts/test-gha` compiles the *codec*
console exe, which references no Rhino and no Grasshopper types — so
`GhLink.cs` and `Components.cs` are never compiled against a test and never run
under one. CI runs layer A only.
**Why it matters:** this is the root of the whole P0-1 family. The fake was *more
correct than the plugin* — it answered `unknown_ref` while the plugin dropped the
request — so `test_reload.py` passed from the day it was written while the shipped
product hung on a reopen. The lesson is not "the fake is wrong" (a test double is
the right tool for the Blender side); it is that **the plugin needs a contract test
of what it sends**, and that a suite which passes with a *more compliant* peer
proves less than it appears to.
**What the fake cannot cover, and therefore has never been tested end to end:**
- **units** — the plugin converts document units ↔ metres via `DocumentScale()`;
  the fake's "scale" is the *parameter* value and it never converts. A GH document
  that is not in metres has never had its geometry scale exercised. *(The
  development document is in metres — confirmed 2026-10-12 — so this is a latent
  gap rather than something currently biting. It is still the largest untested
  axis, because the conversion is silent in both directions.)*
- **solver coalescing** — the plugin collapses `ScheduleSolution` calls and may
  answer only the most recent request; the fake answers every request. This is what
  hid the swallowed-answer defect in P0-4: the fake could not reproduce it, and it
  was found by reading the C# and instrumenting Blender's own concurrency.
- **`hello`'s `doc`** — the plugin sends a *file path*, so saving an unsaved GH
  document changes it and a later reconnect bumps the generation, dropping the
  frame cache once. The fake's `--doc` is fixed.
- **self-healing** — `RetryStaleSolves` re-asks up to 8 times, so the plugin
  recovers from a lost solve where the fake hangs. The fake is the stricter peer.
- **geometry conversion** — Brep meshing, curve fitting and SubD tag transfer are
  covered only by `spikes/s6` inside Rhino, not by any Blender test.
**Fix, in order of value:** a contract test for what the plugin sends (its frames
are just bytes — a console harness can assert them without Rhino for the parts that
do not touch GH types); then a `fake_gh` mode that coalesces, so P0-4's defect is
reproducible in the suite rather than only by instrumentation; then the units path.
**Effort:** L.

> **Two of three done 2026-10-12** (`97f1425`, `74df6fb`): `scripts/check-deployed`
> compares the deployed `.gha` against the build (warned from `scripts/test-gha`,
> documented in the README), and `fake_gh --coalesce` models the collapsed
> `ScheduleSolution` solves — later requests supersede the pending one and the
> report counts them as `dropped` — exercised by `CoalescedAnswerTests`, which
> converges only because the engine's own timeout retry handles the dropped answer.
> The plugin-sends contract test and the units path remain open; units is
> deliberately deferred as the latent one.

**State of the deployment, 2026-10-12:** `grasshopper/deploy/deploy.sh` copies the
built `.gha` into the wine Grasshopper Libraries folder, and **Grasshopper only
scans Libraries on load** — so the plugin in a running Rhino is the one loaded at
startup. Nothing in this repository detects that. Every C# fix is therefore
invisible until Rhino restarts, which is how the `unknown_ref` refusal sat
unexercised for a day while the suites were green: the layer B tests drive
`fake_gh`, `scripts/test-gha` builds the codec only, and neither can see whether
the running client is current. When debugging anything protocol-shaped, check
`stat` on the deployed `.gha` against the build before trusting a negative result.

## P1 — Failure paths that stall

Each of these turns a fast, diagnosable failure into a 30-second timeout or a
lost connection.

### P1-1 · `Stage Out` never answers with an error

> **Done 2026-10-12** (`34499a0`). The catch enqueues the protocol's error response
> (`gh_solve_failed`, empty hash, zeroed counts) when nothing was sent, and records
> `slot.Status`/`Message` under `Gate` through the new `GhLink.SetSlotStatus`, which
> also settles `RetryStaleSolves`. `fake_gh --fail-after` plus `SolveFailureTests`
> pin the fast error from the Blender side; the `Components.cs` half is compiled and
> deployed, and the 2026-10-12 live pass found no practical way to trigger the catch
> (the outgoing conversion warns, it does not throw), so it stands as inspection plus
> deployment. The new test also
> surfaced a second bug: the engine's generic error branch marked the object dirty,
> re-sending on the same tick — with a solve that just failed, a request loop. It now
> follows protocol §7 and retries on the next change instead.

**Where:** `grasshopper/src/gh/Components.cs:244-248`.
**Why it matters:** the `catch` sets a local `status` and adds a runtime message,
then falls through to `da.SetData(...)`. **No response frame is enqueued**, and
`slot.Status` is never set to `"error"` either — only `"ok"` is, at `:240`. So
Blender's stage sits in `waiting` for the full timeout (30 s by default) instead
of getting an immediate `status: "error"` with `gh_solve_failed`, which protocol
§4.4 defines, `protocol/fixtures/response-error/` pins, and
`engine.py:234-252` already handles. The Blender side is ready for this answer;
the GH side never sends it.
**Fix:** in the `catch`, build and enqueue a response with `out_hash: ""`,
`status: "error"`, `error: {code: "gh_solve_failed", message: ex.Message}`,
empty `warnings`, zeroed `counts`, reusing `slot.Rid` and `key`. Set
`slot.Status = "error"` under `Gate`.
**Test:** `grasshopper/tests/console/Program.cs` cannot reach this (no GH types).
Cover it in layer B by having `tools/fake_gh` return a `status:"error"` response
and asserting the Blender stage lands in `error` without waiting for the timeout.
**Verify:** `scripts/test-gha`, `scripts/test-blender`.
**Effort:** M.

### P1-2 · `Stage In` has no exception guard
**Where:** `grasshopper/src/gh/Components.cs:62-117` — no `try`/`catch`, unlike
`StageOutComponent` (`:200-248`).
**Why it matters:** every conversion it calls can throw — `AddFaces` on a
malformed section (`Conversion.cs:62`), `SetMeshColors` (`:102`), and an
`IndexOutOfRangeException` for a `mesh.verts` length that is not a multiple of 3.
The exception escapes `SolveInstance`, red-flags the component with a raw
stack-less message, and **skips `GhLink.Publish(OnPingDocument())` at `:116`**,
so the schema and `hello` refresh is lost for that solve. Inconsistent with the
sibling component, which does guard.
**Fix:** wrap the conversion block the way `StageOutComponent` does, report
through `AddRuntimeMessage`, and set `slot.Message` so the stage shows a readable
failure.
**Verify:** `scripts/test-gha`.
**Effort:** S.

---

## P2 — Decode hardening

The frame decoder is the boundary. Today several malformed inputs throw what the
callers do not catch, and one path tears down the connection.

### P2-1 · `Json`'s stated contract is not total

> **Done 2026-10-12** (`34499a0`). Every malformed path in `Json` raises
> `JsonException` (`int.TryParse` for `\uZZZZ`, `double.TryParse` for numerics), and
> `ReceiveLoop` gained a last-resort `catch (Exception)` that notes and continues, so
> no single frame can tear down a healthy connection. The console suite's new
> `decoder-boundary` group covers the escapes and numerics; against the old code
> they throw raw .NET exceptions.

**Where:** `grasshopper/src/core/Json.cs:119`
(`Convert.ToInt32(s.Substring(pos, 4), 16)`, on a `\uZZZZ` escape) and
`:147-154` (`double.Parse` on `1.2.3`, `1e`, `--5`).
**Why it matters:** the class documents `JsonException` as its failure mode, but
these throw `FormatException` / `ArgumentException` / `OverflowException`.
`GhLink.ReceiveLoop` (`:218-227`) catches only `FrameException` and
`JsonException`, so anything else escapes into `Loop`'s catch-all (`:187-190`),
which reports "connect failed", disposes the client, and reconnects. **One
malformed frame tears down a healthy connection.**
**Fix, both halves:** (a) wrap every numeric and unicode conversion in the parser
so every malformed-input path raises `JsonException`; (b) add a last-resort
`catch (Exception)` in `ReceiveLoop` that notes and `continue`s, so no single
frame can kill the loop.
**Verify:** add malformed-header cases to `grasshopper/tests/console/Program.cs`.
It runs without Rhino, so this is cheap.
**Effort:** M.

### P2-2 · The `proto_mismatch` path itself throws

> **Done 2026-10-12** (`34499a0`). `Frame.Decode` requires a numeric `v`
> (`Json.IsNumber`), compares numerically so `2.0 == 2` and `"2"` is rejected as in
> Python (which settles P5-2), and builds the message with `Convert.ToString`. The
> console case pins `"v":"x"` as a `proto_mismatch`, not a converter crash.

**Where:** `grasshopper/src/core/Frame.cs:195-198`.
**Why it matters:** `Convert.ToInt64(v, ...)` throws `FormatException` for
`"v": "x"`, and the message builder throws again through `Convert.ToDouble` and
`Json.FormatDouble`. So the one error the protocol mandates for a version
mismatch becomes an unclassified exception, which P2-1 then escalates into a
reconnect.
**Fix:** require an integral JSON type before converting (`Json.IsNumber` exists
at `Json.cs:307`), and build the message with `Convert.ToString(v)` rather than
the double formatter. This also settles P5-2 (below): Python's `check_version`
(`codec.py:112-117`) rejects a string `"2"` with `!=`, so C# should too.
**Verify:** a console case with `"v": "x"` asserting `FrameException` with code
`proto_mismatch`.
**Effort:** S.

### P2-3 · `Decode` does not validate `dtype`, and `name` may be null

> **Done 2026-10-12** (`34499a0`). `Decode` rejects dtypes outside `{f32,u32,u8}`
> and nameless sections with `bad_frame`; the console cases cover both. The "reaches
> `UpdateSection` and throws an NRE at hash time" sentence below was wrong, found
> while implementing: nothing in the product hashes a decoded section, and
> `Encoding.UTF8.GetBytes(null)` would throw `ArgumentNullException` if it did. The
> validation stands on the real consequence — a null name matches nothing, so the
> frame is accepted and its geometry is silently unusable.

**Where:** `grasshopper/src/core/Frame.cs:219-227` accepts any dtype other than
the `"u8"` special case. `:214` reads `name` through `Json.GetString`, which
returns `null` for a missing key (`Json.cs:322-326`).
**Why it matters:** `codec.py:88` and `:105` reject anything outside
`{f32, u32, u8}` with `SectionError`. C# therefore **accepts frames Python
rejects** — a real divergence, not a style difference. `Section.ElementCount`
(`Frame.cs:74`) then reports `Raw.Length / 4` for a dtype that is not four bytes
wide. Separately, `new Section(null, ...)` reaches `UpdateSection` (`:285`) and
throws an NRE at hash time.
**Fix:** validate `dtype` against a `{"f32","u32","u8"}` set, throwing
`FrameException("bad_frame", ...)`; require `name` to be a non-null, non-empty
string.
**Verify:** console cases mirroring the malformed-frame coverage already in
`blender/tests/core/test_core.py`. Compare the two suites for gaps while here.
**Effort:** S.

---

## P3 — Grasshopper concurrency and resource bugs

These are separate from P1 because none of them stalls a request on the happy
path — they corrupt state, leak, or race.

### P3-1 · `GhLink.Stop()` is dead *and* ineffective
**Where:** `grasshopper/src/gh/GhLink.cs:153-157`.
**Why it matters:** nothing calls it, and it only flips `_running`. The socket
thread is blocked inside `client.ReceiveBinary()` (`:211`), so the thread and the
socket outlive the call by an unbounded time.
**Fix:** both halves or neither. Either dispose the client to unblock the read and
`Join` with a timeout, and call it from a document-close hook — or delete it.
**Effort:** S.

### P3-2 · `Outbox` is unbounded, never cleared, and can jam permanently

> **Done 2026-10-12** (`0283021`). `SendFrames` dequeues before sending, so a failed
> send drops just that frame — no frame is ever left at the head; `Enqueue` keeps the
> queue under 64 MB (dropping the oldest first, but never the newest, so one
> oversized frame can still go out); `Loop`'s `finally` clears the queue on
> disconnect. Deliberately no priority queue: frames are redundant by design. The
> plugin has no automated suite, so verification is inspection plus the deployed
> build (the 2026-10-12 live pass confirmed the healthy path and the reconnect; a
> mid-send failure is not producible in a session).

**Where:** `grasshopper/src/gh/GhLink.cs:45`, `:361-365`, `:191-196`, `:338-359`.
**Why it matters:** two distinct problems.
1. **A live stall.** `SendFrames` (`:348-356`) catches a `SendBinary` failure and
   `return`s **without dequeuing** — `:357` is skipped. The failed frame stays at
   the head and every later frame queues behind it, forever.
2. **Unbounded growth.** Nothing caps the queue and nothing clears it on
   disconnect (`:191-196`), so a stalled link accumulates a full frame per solve,
   and a reconnect then delivers the whole backlog first-class.
**Fix:** the dequeue-on-failure bug first — that is a stall, not a leak. Then bound
the queue (newest-per-stage, or a byte cap) and `lock (Gate) Outbox.Clear()` in
`Loop`'s `finally`.
**Effort:** M.

### P3-3 · Torn read of `slot.Params`
**Where:** `grasshopper/src/gh/GhLink.cs:254-257` assigns and fills `slot.Params`
under `Gate`; `grasshopper/src/gh/Components.cs:347-348` reads it **without the
lock** from the UI thread.
**Why it matters:** a solve landing mid-write sees an incomplete parameter set, so
a `Param` component outputs its default for a frame instead of the driven value.
**Fix:** build a local dictionary and assign the field once at the end of the
lock, and take the lock for the read.
**Effort:** S.

### P3-4 · `_lastPublishAt` is not volatile, and the `hello` flag races
**Where:** `grasshopper/src/gh/GhLink.cs:379` (non-`volatile` `DateTime`) written
at `:310` and `:385`, read at `:308`. `_publishedThisConnection` (`:51`) read at
`:396`, written at `:415`, with no lock.
**Why it matters:** a `DateTime` wraps a single `ulong`, which is not atomic on
the 32-bit Mono runtime, so the heartbeat comparison can observe a torn value.
The unlocked read-modify-write on `_publishedThisConnection` lets two threads both
send `hello`. Blender tolerates the duplicate (it only resets waiting stages and
re-sends), so this is a wasted round trip rather than corruption — but it is a
data race.
**Fix:** `Interlocked.Read`/`Exchange` on `UtcTicks` for the timestamp; move the
flag's read-modify-write under `Gate`. `Republish()` (`:450-453`, dead) becomes
unnecessary either way.
**Effort:** S.

### P3-5 · Registries grow without bound
**Where:** `grasshopper/src/gh/GhLink.cs:42` (`Slots`), `:55-57` (`StageEnabled`,
`StageParams`).
**Why it matters:** every key ever seen is kept. A deleted or mistyped stage stays
in `hello` forever, so Blender logs "schema for unknown stage" every heartbeat
(`engine.py:214-217`), and memory grows with each edit.
**Fix:** prune on `RemovedFromDocument`, or reconcile against live components on
`hello`. Pairs with P6-12 (log once per key, not once per heartbeat).
**Effort:** S.

### P3-6 · A schema frame per stage on every solve
**Where:** `grasshopper/src/gh/Components.cs:116`, `:254`, `:380` all call
`GhLink.Publish(OnPingDocument())` unconditionally; `SendPublication`
(`GhLink.cs:419-428`) then emits one schema frame per stage.
**Why it matters:** protocol §4.2 says schema is sent "whenever a declaration
changes". Blender de-duplicates by signature (`engine.py:199-201`), so this is
waste rather than a defect — but it is also why the 2 s heartbeat at `:308` only
ever fires when the canvas is idle, which is a useful property to understand
before changing it.
**Fix:** compare against the last declaration sent per stage and skip when
identical.
**Effort:** M.

---

## P4 — Engine hot path and idle stability

W2 established *that* writes should be change-gated, and got that right. What is
left is the cost of *deciding*.

### P4-1 · `_prefix_revision` runs on every tick — **re-scoped, see the profile**
**Where:** `blender/gh_stack/blender/engine.py:801-830`, called from
`_prefix_sections` (`:771`).
**Why it matters:** a *memo hit* — the cheap path, the one that is supposed to avoid
work — costs **0.99 ms at 4k triangles and 8.3 ms at 50k**, while the clone it
avoids costs 4.1 ms and 45 ms. So the memo is worth having, but its floor is high
for a stage with anything before it in the stack.
**Profiled 2026-10-12** (50,625 triangles, a 42-node prefix graph), timing the
parts of one revision:

| part | ms | share |
|---|---:|---:|
| `geometry_io.object_to_sections` — the geometry read | **7.35** | 89% |
| `hashing.geometry_hash` | 0.60 | 7% |
| `_modifier_descriptor` — the `bl_rna` property walk | **0.21** | 3% |
| `_node_tree_signature` — nodes, sockets, links | **0.20** | 2% |
| `codec.section_bytes` | 0.03 | <1% |
| whole `_prefix_revision` | 8.23 | |

> **This re-scopes the item.** The original fix here was to gate
> `_modifier_descriptor` and `_node_tree_signature` behind cheap counters. Those
> two are **0.41 ms combined — 5% of the cost** — so gating them is complexity for
> almost nothing. The cost is the geometry read, and the read is what makes the
> change signal *sound*: a vertex moved without changing any count is exactly the
> edit a cheap gate would miss, which is the class of bug P0-8 was.
**The real question is whether the read can be skipped at all.** Blender exposes no
per-datablock content counter to lean on: `Mesh.version` does not exist in 5.2, and
`NodeTree` offers only `tag`, `session_uid`, `update_tag` and `interface_update` —
none of them a count of content changes. So:
1. **Accept it.** 8.3 ms at 50k, paid once per pass per stage-with-a-prefix; a stage
   at the head of the stack reads the base datablocks directly and pays nothing
   extra. Do nothing and write the number down.
2. **Cheapen the read**, not the decision: reuse preallocated numpy buffers across
   passes instead of allocating fresh arrays per read. Modest (allocation is part
   of the 7.35 ms), sound, and independent of everything else.
3. **Skip the read only with a sound signal.** A "the depsgraph updated" flag is
   not one: our own passes tag the depsgraph constantly (helper writes, modifier
   visibility, the prefix clone's link/unlink). Finding a signal that distinguishes
   input changes from our own writes is the whole problem, and a heuristic here
   reintroduces silent stale geometry. If someone wants this, it needs a design
   note first, not a patch.
**Do not** do the original item 1/3: gating the descriptor walks.
**Tests:** `test_frame_cache.py::NoOpQuietTests.test_02_event_loop_idle_is_quiet`
already asserts the quiet path, and
`TimeDrivenPrefixTests.test_03_an_unchanged_frame_still_hits_the_memo` asserts the
memo still hits at an unchanged frame. Any change here must keep both green, and
must keep the frame in whatever tuple gets compared.
**Resolved 2026-10-15.** The floor is gone, and option 3's design note was written:
the revision is built from epochs the depsgraph moves, so no geometry is read or
hashed on the memo hit path. Measured at 50k triangles: **0.010 ms** for a prefixed
stage (was 8.1 ms) and **0.006 ms** for a head-of-stack stage, which had no memo at
all and re-read its base every pass (was 8.0 ms). What remains is the clone a real
change needs — 51.9 ms at 50k, unchanged — which is P4-1's other half and the
subject of the note below.

**Update 2026-10-14.** Option 1's "8.3 ms at 50k" is still the floor and is now
understood as two different costs: the *stack object's own base read* (the 89% the
profile named) and *referenced targets* (`_pointer_state` hashing a second datablock,
per pointer, additive — measured at 0.6 ms/5k and
10 ms/82k). The second is resolved: the target hash is cached across passes and
retired when the depsgraph tags that target, which takes 9.3 ms off one pointer at
114k triangles on the memo hit path. The first is untouched and remains the
dominant per-pass cost for a heavy base mesh; it needs the epoch design (see
the backlog's epoch note), and still option 3's sound signal
problem, not a patch.

**Effort:** S for option 2; L and design-first for option 3.

### P4-2 · Nested node groups are not walked

> **Done 2026-10-12** (`d045b42`). `_tree_signature` (`engine.py:921`) recurses into
> every node with a `node_tree` — Blender 5.2 exposes one exactly on the group node
> types — visits each tree once keyed by `as_pointer()` so a recursive group cannot
> loop, and turns the depth cap into "do not memoize" rather than a silently
> truncated signature. `NestedGroupPrefixTests` builds `Outer → Inner`, edits a node
> inside `Inner`, and asserts the stage requests again; with the old walk it fails
> (the negative control), with the new one the suite is green.

**Where:** `blender/gh_stack/blender/engine.py:734-737` — the docstring says so
outright.
**Why it matters:** a prefix modifier whose node group contains a *nested* group —
which is how anyone organises a real GN graph — will not invalidate the memo when
the nested group is edited. The result is a **silently stale prefix**: the stage
sends old geometry and nothing says why. This is the most likely cause of a "why
is it not updating" report.
**Fix:** recurse into referenced `node_group`s with a depth cap and a visited set
(cycles are possible), or widen the memo to hash every referenced `node_groups`
datablock.
**Test:** a layer B test that edits a node inside a nested group and asserts the
prefix changes.
**Effort:** M.

### P4-3 · `_modifier_descriptor` skips collections — **not a defect, close it**
**Where:** `blender/gh_stack/blender/engine.py:854-855` — `continue` for
`prop.type == "COLLECTION"`.
**Why it was filed:** presumed to be the same silent-staleness class as P4-2, "for
any modifier whose behaviour depends on a collection".
**Checked 2026-10-12 across every registered modifier type.** Exactly four have a
COLLECTION-typed property, and none of them is an input this protocol carries:

| modifier | collections | matters? |
|---|---|---|
| `NodesModifier` | `bakes`, `panels`, `node_warnings` | no — a bake cache, node-editor layout, and diagnostics |
| `UVProjectModifier` | `projectors` | no — changes UVs, and the wire carries vertex colours, not UVs |
| `GreasePencilDashModifierData` | `segments` | no — not one of the supported object types |
| `GreasePencilTimeModifier` | `segments` | no |

So skipping by type is **correct as written**, and freezing those collections would
add cost for no signal. **Close the item** — but leave a comment in place saying
which four were checked and why none matters, so it is not re-filed.
**One corner worth a note, not a fix:** `NodesModifier.bakes` is a *bake cache* — if
a prefix graph contains a Bake node, recomputing that bake changes the evaluated
geometry with no property and no node-graph change, so the revision would not see
it and the memo would serve a stale prefix. Narrow (a baked prefix modifier), and
unverified — confirm whether a re-bake is observable before treating it as real.
**Effort:** S (the comment), or zero if the checked table above is enough.

### P4-4 · Quadratic eviction in two places
**Where:** `engine.py:488-492` (recomputes `_memory_usage` on every loop
iteration) and `core/cache.py:104-113` (`min()` plus a recomputed `total_size()`
per eviction).
**Why it matters:** both are O(n²) in the number of cached entries. With a 256 MB
budget and many small frames that is thousands of entries, and the stall lands on
the frame that crosses the limit — exactly during playback, which is when the
cache matters.
**Fix:** in `_remember`, compute the total once and subtract each evicted entry's
size. In `CacheIndex`, keep a running total and exploit the existing monotonic
`seq` — a deque, or a `heapq` keyed by `seq` with lazy deletion, replaces the
repeated `min()`.
**Test:** `test_core.ResponseStoreTests` and `test_frame_cache.EvictionTests`
already cover the behaviour; add a large-n timing assertion.
**Effort:** M.

### P4-5 · `register()` does not clear the rest of the module state
**Where:** `blender/gh_stack/blender/engine.py:889-895` — clears only `_stores`.
**Why it matters:** `_cache`, `_prefix_memo`, `_waiting_since` and `_dirty` hold
`obj.name` keys and decoded frames across a disable/enable cycle. `_prefix_memo` is
keyed by object **name**, so a re-register can serve a memo entry that belongs to a
different object that has since taken the name.
**Fix:** clear all five in `register()`, alongside `_stores`.
**Effort:** S.

### P4-6 · A dead setting, a duplicated lookup, and an undefined literal
- `blender/gh_stack/blender/prefs.py:40-43` exposes `log_level` (drawn at `:51`).
  **Nothing reads it.** Wire it to the `print("[gh-stack] ...")` calls or remove
  it. A settings field that does nothing is worse than no field.
- `blender/gh_stack/blender/ui.py:181-186` (`_port`) re-implements the prefs
  lookup that `prefs._pref()` (`:54-61`) now provides. Use `_pref("port", 9130)`.
- `"disabled"` is a bare literal at `engine.py:654` and `ui.py:177`.
  `stack.py:25-29` defines `STATE_IDLE`..`STATE_DISCONNECTED` but no
  `STATE_DISABLED`. Add it and use it in both places.
  **Updated 2026-10-13:** this bullet is now the *only* record of that
  inconsistency. `core/rules.py` had a second, more complete state list
  (`disabled`, `frozen`, `unbound`) and was deleted (P8-3), so nothing else defines
  the missing three. Worth doing while someone is next in `stack.py`.
- `blender/gh_stack/blender/bridge.py:15-21` initialises `state` without a
  `"notice"` key, but `props.py:158` writes it and `ui.py:68` reads it. Add
  `"notice": ""` so the channel is declared.
**Effort:** S.

---

## P5 — Codec parity

Small items, but each is a case where the two implementations disagree about the
same wire bytes.

### P5-1 · `int` casting: C# truncates, Python rounds
**Where:** `grasshopper/src/core/ParamValues.cs:92`
(`(int)Convert.ToInt64(...)`) versus `blender/gh_stack/core/params.py:64`
(`int(round(float(value)))`).
**Why it matters:** `2.7` at an `int` parameter becomes 2 on the GH side and 3 on
the Blender side. Beyond the value, the two sides compute **different request
IDs** for the same input, so the content-addressed cache and the "did the
definition change?" comparison can disagree.
**Fix:** `(int)Math.Round(Convert.ToDouble(value, CultureInfo.InvariantCulture))`,
matching Python. Add a console case pinning `2.7 → 3`.
**Verify:** `scripts/test-gha`, `scripts/test-core`.
**Effort:** S.

### P5-2 · `v` accepts a string
**Where:** `grasshopper/src/core/Frame.cs:195` — `Convert.ToInt64` parses `"2"`.
Python's `check_version` (`codec.py:112-117`) compares with `!=` and rejects it.
**Fix:** folded into P2-2. Make the two agree and state which way.
**Effort:** S.

### P5-3 · `FormatDouble`'s documented claim is false
**Where:** `grasshopper/src/core/Json.cs:176-178` claims byte equality with
`json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)`;
`:260-268` implements it with `.ToString("R", ...)`.
**Why it matters:** `"R"` emits `1E+21` where Python emits `1e+21`, and Mono's `R`
is a 15-then-17-digit algorithm, so `Math.PI` may render as
`3.1415926535897931` against Python's `3.141592653589793`. **Impact today is
nil** — GH never hashes a header and Blender parses JSON numerically — so this is
a comment that will mislead a future maintainer into trusting byte equality they
do not have.
**Fix:** lowercase the exponent, and soften the comment to name the values that
*are* exact (integers and short decimals). Add a console case recording where the
boundary is, so it is documented rather than assumed.
**Effort:** S.

### P5-4 · `too_large` and `proto_mismatch` are never actually sent
**Where:** `blender/gh_stack/core/codec.py:155` raises `too_large` locally;
`blender/gh_stack/blender/server.py:146-148` catches `CodecError` and only writes
`bridge.state["error"]`. `docs/protocol.md:40` says a version mismatch is
"answered with `error` code `proto_mismatch` and the connection is closed".
**Why it matters:** the specification promises a reply the implementation does not
send, so a peer that misbehaves gets silence instead of a diagnosis. Either the
behaviour or the specification is wrong, and right now that is ambiguous.
**Fix:** decide. Sending the `error` frame before closing is more useful and
matches the spec; updating `protocol.md` is cheaper. Record the choice in an ADR if
the spec changes.
**Effort:** S.

---

## P6 — Mechanical cleanup

**No behaviour change anywhere in this tier.** The existing suites staying green
is the whole verification.

### Dead code

| Where | What | Lines |
|---|---|---:|
| `blender/gh_stack/blender/bridge.py:39-48` | `post_outgoing` / `take_outgoing` — `server._sender` uses its own asyncio queue | ~10 |
| `blender/gh_stack/blender/geometry_io.py:25-55` | `MESH_`/`SUBD_`/`CURVE_`/`POINT_SECTION_ORDER` — labelled "per protocol §5" but unused. The live table is `hashing.HASH_SECTION_ORDER`, so these actively mislead | ~31 |
| `blender/gh_stack/blender/geometry_io.py:737-746` | `evaluated_mesh` — never called | ~10 |
| `blender/gh_stack/blender/props.py:13-21` | `TYPE_DEFAULTS` — never referenced; duplicates `params.default_for()` | ~9 |
| `blender/gh_stack/blender/props.py:311-316` | `register()` / `unregister()` both `pass` | 6 |
| `blender/gh_stack/blender/helpers.py:90-95` | both `pass` | 6 |
| `blender/gh_stack/blender/nodes.py:131-136` | both `pass` | 6 |
| `blender/gh_stack/blender/server.py:181-182` | `register()` is `pass`. **Keep `unregister()`** — it calls `SERVER.stop()` | 2 |
| `grasshopper/src/gh/GhLink.cs:450-453` | `Republish` | 4 |
| `grasshopper/src/gh/MiniWsClient.cs:65-67` | `Connected` (and `TcpClient.Connected` is unreliable anyway) | 3 |
| `grasshopper/src/core/Conversion.cs:711, 720` | local `meshedBreps` — incremented, never read | 2 |
| `grasshopper/src/gh/GhLink.cs:20` | unused `using Rhino.Geometry` — no geometry type appears in the file | 1 |
| `grasshopper/tests/console/Program.cs:17` | unused `using System.Globalization` | 1 |

### P6-1 · `_handle_schema` logs "unknown stage" every heartbeat
`blender/gh_stack/blender/engine.py:214-217`, once per 2 s per unknown key. Log
once per key. Pairs with P3-5.

### P6-2 · Inconsistent access modifiers
`grasshopper/src/gh/GhLink.cs:104` `Note` is public but only used internally;
`Json.cs:307` `IsNumber`, `ParamValues.cs:117` `Neutral`. Make them
`internal`/`private` so the public surface is honest.

### P6-3 · Function-local imports to hoist
`blender/gh_stack/blender/geometry_io.py:717, 730, 742` (`import bpy` ×3),
`blender/gh_stack/blender/props.py:45, 107, 213` (`json`, `hashlib`, `json`),
`blender/gh_stack/core/params.py:120` (`json`),
`blender/gh_stack/blender/engine.py:126, 133, 205` (`import traceback` ×3).

### P6-4 · `codec.py:197` re-derives what `_DTYPE_FMT` holds
`item = 1 if dtype == "u8" else 4`. Use `_DTYPE_FMT[dtype][1]`.

### P6-5 · `params.py:118-124` re-implements `codec.canonical_json`
The same `sort_keys` / `separators` / `ensure_ascii` call, duplicated. Import and
reuse it.

### P6-6 · Magic numbers that want names
`grasshopper/src/gh/GhLink.cs:161, 172, 198-203` (backoff 500/5000/100), `:282`
(`ScheduleSolution(10, ...)` — load-bearing per the comment at `:8-10`), `:301`
(250 ms poll), `:109` (note cap 50), `:308` (2 s heartbeat), `:329` (8 resolve
attempts), `grasshopper/src/core/Conversion.cs:362` (`Math.Max(8, SpanCount*8)`),
`:272` (24 samples), `:390` (`1e-3`). `Conversion.cs:378-381` already shows the
intended pattern.

### P6-7 · Error-code and prefix literals duplicated
`"bad_frame"` appears ten times in `Frame.cs`, `"sha256:"` three. Python has single
sources (`codec.py`, `hashing.B_PREFIX`). A `static class ProtocolCodes` plus a
`HashPrefix` const collapses them.

### P6-8 · `for (int i = 1; i < 9; i++)`
`grasshopper/src/gh/Components.cs:304`. A literal bound that silently breaks the
moment a tenth input is added. Derive it from the manager's parameter count.

### P6-9 · Every warning carries the code `"conversion"`
`grasshopper/src/gh/Components.cs:223`. Protocol §4.4 and
`protocol/fixtures/response-mixed` use per-cause codes (`curve_approximated`,
`subd_creases`). Blender renders only the text (`engine.py:303-313`), so this is
cosmetic today, but the codes are part of the documented message.

### P6-10 · The SubD crease warning fires even with no creases
`grasshopper/src/core/Conversion.cs:686` sits outside the
`if (creaseEdges.Count > 0)` block at `:679`, so it is emitted for every SubD and
re-emitted on every solve. Move it inside, or emit once per stage.

### P6-11 · `counts["curves"]` over-reports
`grasshopper/src/core/Conversion.cs:764` publishes `curves.Count` (the input goos)
rather than the number of curves actually emitted; `SectionsFromCurves` can drop
one. The fixture's `counts.curves` is the emitted count.

### P6-12 · The SubD cage is converted twice per solve
`grasshopper/src/gh/Components.cs:93` builds it via `MeshFromSections`, then `:99`
`SubDFromSections` (`Conversion.cs:619`) calls `MeshFromSections` again. Roughly
double the Stage In cost for a cage, against the §13 budget. Have
`SubDFromSections` accept the mesh already built.

### P6-13 · A hand-built `DecodedFrame` used as an adapter
`grasshopper/src/gh/Components.cs:91` constructs
`new DecodedFrame(new Dictionary<string, object>(), slot.Sections)` purely to
reuse the section converters. A signal that `Conversion` wants a natural
`FromSections(IList<Section>, double scale)` entry point.

### P6-14 · Point-scaling loops duplicated four times
`grasshopper/src/core/Conversion.cs:131-138`, `:467-471`, `:590-592`, `:601-606`.
One `FloatsToPoint3d` / `VecToFloats` pair removes all four — and this is exactly
the duplication where a units bug hides.

### P6-15 · Silent data drops with no warning
`grasshopper/src/core/Conversion.cs:462`
(`if (n < 2 || at + n > raw.Length / 4) break;`) and `:591` both truncate
malformed input wordlessly, where `geometry_io.py:574-577` and `:468-471` raise.
Plan principle 6 says every loss is reported. Either raise or warn.

### P6-16 · `MeshSections` hardcodes `"mesh.colors"` while parameterising the prefix
`grasshopper/src/core/Conversion.cs:177`. Harmless today because `includeColors`
is false for `subd`, but a trap for the next caller.

### P6-17 · `Icons.Get` disposes the stream behind the `Bitmap`
`grasshopper/src/gh/Icons.cs:41-45`. GDI+ requires the stream to stay open for the
bitmap's lifetime, and the cached icon is drawn on every canvas repaint. Decode a
copy (`new Bitmap(Image.FromStream(stream))`) so the stream can be closed safely.

### P6-18 · `Program.cs` unguarded dereference
`grasshopper/tests/console/Program.cs:145-146`:
`decoded.Find("mesh.verts")` then `.ToFloats()`, no null check. Fine for today's
fixture; an NRE the moment it is regenerated.

### P6-19 · Small Python polish
- `props.py:194-209` `_matches_type` is a long if-chain; a dict of type→predicate
  would read better and pairs with the `TYPE_DEFAULTS` removal.
- `props.py:265-266` swallows `TypeError`/`ValueError` in `_set_ui` with no
  diagnostic, so a schema whose range cannot be applied is silently unconstrained.
- `ops.py:65` `Push` is marked `UNDO` for an action whose purpose is to bypass the
  cache. Consider `{"REGISTER"}` only.
- `ui.py:167-168` `_state_text` is a one-line wrapper around `stage.state`.
- `helpers.py:55-57` `ensure_helpers` always builds all three kinds — cheap, but
  it is why every stage has three helper objects.
- `nodes.py:89-95` `ensure_modifier` ordering looks modifiers up by name twice.
  Fine at these sizes; note it if a stack ever gets long.

### P6-20 · Loose style
- `grasshopper/src/gh/Components.cs:46, 162, 307-310, 441` use fully-qualified
  type names where the file already uses `GH_Boolean`/`GH_Integer` elsewhere.
- `grasshopper/src/gh/Components.cs:409` empty `catch (Exception) { }` around the
  param GUID parse — catch `FormatException` and note it so a corrupted id is
  visible.
- Missing XML docs on `GhLink`'s public surface: `Endpoint`, `Connected`,
  `Register`, `Unregister`, `PublishStage`, `PublishParam`, `Slot`, `Enqueue`,
  `Start`, `Stop`, `Publish`, plus `StageSlot`'s mutable public fields. The file's
  internal comments are excellent; the public surface is the gap.
- `GhLink.cs:39` `Endpoint` is a mutable static captured by a running thread at
  `:167`. Changing it at runtime has no effect and there is no way to force a
  restart. Make it read-only-with-restart, or document that it must be set before
  the first solve.
- `blender/tests/core/` has no `__init__.py` while `blender/tests/headless/` does,
  and `scripts/test-core` passes a slightly surprising `-t blender/tests/core`. It
  works; make the two layers consistent.

---

## P7 — Repo hygiene

### P7-1 · Untrack `spikes/s6/probe_api.exe`
13.8 KB, a build artefact. `.gitignore` already ignores its `s1` twins
(`spikes/s1/WsTest.exe`, `spikes/s1/GhStackSpike.gha`, `spikes/s1/MkDoc.exe`). Add
an ignore rule and `git rm --cached`.

### P7-2 · Decide the fate of the vendored speedups build
`blender/gh_stack/vendor/websockets/speedups.cpython-314-x86_64-linux-gnu.so`
(38 KB) is **not** in `wheels/websockets-17.2-py3-none-any.whl`; it was compiled
locally from the wheel's `speedups.c` and committed. It is a platform-specific,
unverifiable binary in VCS, useful only on CPython 3.14 / linux x86-64.
`websockets/frames.py:18` falls back to pure Python without it. Options, best
first: (a) build it in `scripts/install-addon` and gitignore it; (b) keep it with a
comment recording its provenance; (c) drop it and accept the pure-Python mask path
on the dev install only.

### P7-3 · Three build scripts duplicate the same boilerplate
`grasshopper/build/build.sh:18-26`, `build-core.sh:19-28`, `build-gha.sh:12-19`
each repeat the `CSC_PATH` find, the wine-prefix defaults, and `win_path()`. A
shared `grasshopper/build/_common.sh` sourced by all three removes ~30 lines and
one class of drift.

### P7-4 · Two copies of websockets
`vendor/` (2.0 MB, 64 tracked files) and the wheel (212 KB). Documented and
deliberate (`blender_manifest.toml:14-17`); `vendor/` could in principle be
populated from the committed wheel by `scripts/install-addon` rather than tracked.
Low priority — noted for completeness.

---

## P8 — Documentation drift

### P8-1 · README test counts
**Resolved 2026-10-13, by removal.** The README's counts were dropped rather than
corrected, which is what this item recommended ("a count in a README is stale the
moment a test is added"). They had drifted twice — the text here said 69/72 when the
runner printed 86 — and the `rules.py` removal changed layer A again (60 → 49), which
is exactly the treadmill. `run.py` still prints each layer's count, and CI runs layer
A, so a derived number or a badge remains available if one is ever wanted; a
hand-written one should not come back.
The history is below.

### P8-2 · `docs/handoff-frame-cache.md` contradicts itself
The status line says "implemented 2026-10-12 (W2)", but §"Where things stand"
still describes the pre-W2 tree:
- `:46` — "**Unused by the engine today**" for `CacheIndex`. It is wired, via
  `engine._store_for` / `_stores`.
- `:47-48` — prefs described as "port, timeout, log level only". `data_root` and
  `cache_size_mb` exist (`prefs.py:28-39`).
- `:49` — "50 core, 54 headless". Now 60 and 72.
- `:108-117` — the "Idle stability" checklist reads as TODO; items 1 and 2 are
  done (`_set()` change-gating, `nodes.set_modifier_visible`), and item 3 is
  partly done (the prefix memo exists — but see P4-1/P4-2/P4-3).
**Resolved 2026-10-12:** the file was deleted rather than bannered — it described a
branch of history that had already been superseded, and ADR 0008 plus this plan's
P4 items now carry everything it established. Nothing referenced it except ADR
0008, whose link was updated.

### P8-3 · `core/rules.py` has no consumer

> **Done 2026-10-13.** Deleted: `core/rules.py` and its 11 layer-A tests (layer A
> 60 → 49), with the four references it invalidated — the test module's import, the
> `core/__init__.py` docstring, the §11 layout listing in `plan.md`, and the README's
> test counts (removed rather than re-numbered; see P8-1).
> Removed rather than adopted after a user-value review: adopting it changes nothing
> a user can see — it routes two inline comparisons through pure functions with the
> same semantics — and the module **had already drifted from the engine it would
> describe**: `decide_response` took 8 parameters and used 5 (`stage_key`,
> `cached_rids`, `rid_nonce` unused), and it modelled neither `unknown_ref`
> (`engine.py:276`) nor the generation counter (`engine.py:310`). So option (b) below
> was unsound while it was stale — documenting it would have enshrined the wrong rules
> — and option (a) was a refactor with a sharp edge: a careless adoption would have
> silently dropped the `unknown_ref` recovery that P0-1 fixed. The live option was
> (c) delete: `docs/protocol.md` §6.4 already carries the five apply rules and
> `plan.md` §9.1 the request side.
> **One consequence, deliberately left to P4-6:** `rules.py` was also the more
> complete of the two copies of the stage-state vocabulary (it had `disabled`,
> `frozen`, `unbound`; `stack.py:25-29` has five states, and the engine still writes
> the bare literal `"disabled"` at `engine.py:654` and `ui.py:177`). Deleting the
> module removed the more complete copy, so P4-6's third bullet is now the only place
> that inconsistency lives. It should be done while someone is next in `stack.py`.
> **On the score:** this module was a 3 on *module health* and a 1 on *risk to the
> product*. The two axes are not the same thing, and grading it on the first while
> scheduling it like the second is what put it in a batch it did not belong in.

`blender/gh_stack/core/rules.py` (116 lines) was well-documented and exhaustively
unit-tested, but it is **not imported by the add-on at all** — `engine.py:27-29`
imports `cache`, `codec` and `hashing`, not `rules`. `_process_stage` and
`_handle_response` re-implement its decisions inline. It also restates the stage
states that `stack.py:25-29` owns.
**Fix:** pick one.
- **(a) Adopt it.** Route `_process_stage`'s mode choice and `_handle_response`'s
  apply decision through `rules.decide_request` / `rules.decide_response`. Real
  value: those decisions become testable in layer A without Blender, and the W2
  generation logic gets a pure home.
- **(b) Document it as a spec.** Say so in the module docstring and in
  `docs/protocol.md`, and have `rules.py` import the state constants from
  `stack.py` rather than restating them.

**Superseded the same day — see the banner above.** (a) is more work and better;
(b) is honest and cheap *only once the module is correct*; (c) delete is now the
live option.

### P8-4 · `bl_info` versus the manifest version

> **Done 2026-10-12** with the v0.1.2 release: both files say `0.1.2` and each
> carries a comment pointing at the other, so the next bump cannot miss one.
> (Blender ignores `bl_info` for extension installs; only the development symlink
> install reads it.) The history is below.

`blender/gh_stack/__init__.py:14` (`"version": (0, 1, 1)`) and
`blender_manifest.toml:4` (`version = "0.1.1"`) must be bumped together. For an
extension Blender ignores `bl_info` — it is required only by the development
symlink install (`scripts/install-addon`). Add a comment saying so, so the next
version bump does not miss one.

---

## Deferred

Deliberately **not** in this plan, with reasons.

- **Layers B and C in CI.** `README.md:101-103` already explains that they need
  Blender and a wine prefix with Rhino 8, which GitHub runners do not have.
  Unchanged.
- **A shared test helper for `pump()` / `start_fake()`.** Each layer B test file
  re-implements them. Real duplication, but extracting it touches six test files
  and risks destabilising a suite that currently passes 72/72. Do it after P0–P5,
  as its own change.
- **`CacheEntry`'s field threading.** Seven fields, five of them threaded through
  `store()`/`add()` positionally. A dataclass-style refactor is cleaner but is
  churn against a module P4-4 already has to touch. Fold it into P4-4 or skip.
- **`_dtype_of`'s name-suffix heuristic** (`engine.py:395-406`) — a second source
  of truth for name→dtype beside the protocol. Real fragility (a new section name
  silently defaults to `u32`), but fixing it properly means `codec.decode_frame`
  returning dtypes alongside values, which is a protocol-layer change. Out of
  scope; noted for the next protocol revision.
- **`docs/plan.md` §7/§9.8/§13** still describe the bake operator that ADR 0008
  supersedes. That is a roadmap edit, not cleanup, and belongs with the next
  planning pass.

---

## Verification

| Tier | Layers | Additional |
|---|---|---|
| P0 | core, blender, gha | a fake that mimics `GhLink.Handle`; a two-objects-one-key test (P0-7) |
| P1 | gha, blender | a `fake_gh` mode that errors (P1-1) |
| P2 | gha | malformed-frame cases in `console/Program.cs`, mirroring `blender/tests/core/test_core.py` |
| P3 | gha, blender | concurrency cases are hard to test; rely on review plus the existing suite |
| P4 | blender | `stats["prefix_clones"]` assertions; a 100k prefix timing; a nested-group test |
| P5 | core, gha | the same value through both codecs, byte-compared |
| P6 | all four | |
| P7 | all four | |
| P8 | none | read the diff |

Close with `scripts/test-core`, `scripts/test-blender`, `scripts/test-gha`,
`scripts/test-extension` all green, and the counts in the README matching what
they print.

---

## Sequencing

Two independent tracks — the Python and C# halves never touch each other. If one
person is doing this, still do the tracks in this order: the P0 items are small
and their value is immediate.

**Track A — Blender / Python** *(P0-2, P0-3, P0-4, P0-5 and P0-7 are done)*
P4-5, P4-6 → P4-2 → P4-3 → P4-1 → P4-4 → P5-4 → P6 (Python items) → P8-1, P8-3,
P8-4.

**Track B — Grasshopper / C#** *(P0-1 is done)*
P1-1 → P2-1, P2-2, P2-3, P5-2 → P1-2 → P3-2 → P3-3, P3-4, P3-1, P3-5 → P3-6 →
P5-1, P5-3 → P6 (C# items) → P7-3.

**Both:** P7-1 and P7-2 last, since they touch git history and the packaging path.

**All of P0 is done** (2026-10-12). What remains below is P1 onward, in the order
given.

The P0 order was: **P0-1 and P0-2** first — the two halves of the reopened-file
bug, in different codebases so they could go in parallel, and each independent.
P0-2 (`engine.py`, one line) stops the `ref` from being sent at all; P0-1 (GH)
makes a `ref` recoverable when it is. Do both: P0-2 alone leaves the `ref` path
broken for every other cause (a fresh Rhino, a reopened document, a stage whose
slot was cleared), and P0-1 alone leaves the first request after every load
depending on an error round trip. **P0-3** is the same bug's third face and went in
the same pass, because a retry loop that reports `waiting` is why it went
unnoticed for so long. **P0-4 and P0-5** then came out of playback testing — the
frame-evaluation design and the undelivered-request stall, both in
`_process_stage`'s send path. **P0-6 and P0-7** are small and independent: P0-7 is
two characters and removes an intermittent, invisible correctness bug, and P0-6
removes the one failure that corrupts geometry without leaving a trace.
