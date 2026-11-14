# 0010 — Selective stage solves and live generator stages

**Status:** Proposed, 2026-11-12. The live Rhino/Grasshopper acceptance run is
still pending; this record is promoted to Accepted when the reference file
shows one Kangaroo step per Blender frame and no cross-stage answers.

## Context

Stageloom currently defeats Grasshopper's native dependency-driven update: every
request schedules a document solution and then expires **every** registered
Stage In, Stage Out and Param component
(`grasshopper/src/gh/GhLink.cs`, `RequestSolve`). In native GH a changed source
expires only that object and its dependents; unrelated branches keep their
cached outputs. The current loop therefore:

- recomputes every stage on every request;
- lets every Stage Out with an old rid answer a request aimed at another stage;
- pins `SolveRid` on every slot;
- can produce more than one solve pass per request.

The live reference file exposed all three consequences. It has one first-stage
generator (no Stage In, driven entirely by a Param reading Blender's frame) and
one second stage with a Stage In. The generator output changed on every solve,
even when the second stage's input geometry was emptied. One request produced
two responses per stage with one Stage Out per stage. The generator advanced
twice per Blender frame; the plugin treated the second answer as a definition
change, bumped the generation and pruned the cache. This is independent of any
Grasshopper branch wiring: the stage keys and geometry branches are independent;
the coupling is the document-wide expiry and answer path.

The generator is a **stateful simulation**, not a pure function: Kangaroo
advances one step per solve and resets natively when the frame reaches 0. Its
frame cache cannot be replayed in arbitrary order without advancing the solver.

## Decision

### 1. Stage claims carry a role and publish Stage In presence

`GhLink.Claims` gains a role (`Input`, `Output`, `Param`). `PublishStageIn`
registers a Stage In; `PublishStageOut` (with `PublishStage` kept as a
compatibility alias) registers a Stage Out. `ReconcileOwnership` already rebuilds
ownership from claims; it also derives whether each stage key has a Stage In.

`hello.stages[]` gains an optional `has_stage_in` boolean. Blender uses it to
mark a stage as a generator. Absence of the field means "unknown"; old clients
keep the saved mode. The field is protocol-compatible: readers that ignore it
behave as before.

### 2. One queued, request-driven solve at a time

Replace the no-argument `RequestSolve()` with a request queue:

- inbound `request` calls `RequestStage(key, rid)`;
- a pending map keeps the latest rid per stage key;
- if no solve is queued, schedule one document solution; otherwise the request
  joins the next queued solve;
- the scheduled delegate copies the queued map into an active solve context,
  pins `SolveRid` only for those keys, marks `SolveInFlight` only for those
  slots, and expires only that stage's components;
- `OnSolutionEnd` clears only the active keys, then schedules the next queued
  batch if one arrived during the solve.

There is at most one scheduled request solve in flight. `RetryStaleSolves` queues
the waiting `(key, rid)` pairs instead of requesting a global solve. The 30 s
watchdog still recovers a lost `SolutionEnd`.

### 3. Selective expiry, not a global loop

Inside the already-scheduled solution, expire only the requested stage's
components with `ExpireSolution(false)`: the Stage In and Params are sources;
the Stage Out is explicitly included so a disconnected branch still answers.
`false` expires the object and downstream dependents without asking the
document for an immediate extra solution; the scheduled solution is the one
that recomputes. This is the native GH model and removes the second solve pass.

Components belonging to other stage keys are not expired and keep their cached
outputs.

### 4. Request-driven solves answer the requested stages and changed dependents

The active solve context exposes `ShouldAnswer(key)`:

- **request-driven solve:** true for keys in the active context, and for a
  dependent Stage Out that a shared source ran through its branch when it has no
  newer request queued;
- **document/user solve:** true for any stage with a non-empty rid, preserving
  the existing GH-side push when the user edits the canvas.

`AnswerRid`/`SolveRid` are pinned only for the requested keys. A dependent Stage
Out answers with its own slot rid, so a changed output still reaches the Blender
object whose inputs did not change. An unchanged dependent stays quiet
(`LastSentRid`/`LastSentHash`), and a stage with a newer request queued does not
answer: that solve reads its inputs and answers the newer id. A Stage Out that
is not part of the solve does not clear another stage's status. A user-driven
answer that is byte-identical to the last send for the same rid and output hash
is skipped (the slot remembers `LastSentRid` and `LastSentHash`).

### 5. Generator stages skip the Blender prefix

A stage whose latest `hello` says `has_stage_in: false` is an `input_mode ==
"generator"` stage. The engine:

- does not evaluate or hash the object prefix;
- uses the empty geometry hash and empty sections for the request;
- does not include generator stages in the prefix batch;
- keeps the request id a function of the stage params and nonce only.

The generator still has Params and a Stage Out. Parameters stay independent of
Stage In; Stage In remains geometry-only.

### 6. Live stages bypass the frame cache

A stage gets an explicit `live` flag (an N-panel checkbox, saved with the
stage). It is **on by default**: a new stage cannot know whether its graph is
deterministic, and replaying a solver frame is worse than a missing cache hit.
A deterministic stage is the opt-out (untick Live) and keeps the frame cache.
Live means "stateful output":

- the engine never replays a cached response for the stage;
- a live request is sent when its own inputs changed: its request id differs, or
  the scene frame differs from `live_frame`, or a verification/resend permit is
  armed; a dirty event caused by another stage does not advance it;
- a successful send stores `live_frame`;
- responses are applied when their rid is current, but are not stored in the
  frame cache, do not bump the generation on a same-rid output change, and do
  not prune the cache;
- the normal frame cache remains for deterministic stages.

This preserves native Kangaroo behaviour: one request per frame advances the
solver once, and scrubbing to frame 0 lets the solver reset as it normally does.
A live stage is not replayed from an old frame after the solver state has moved
on.

### 7. Stale answers cannot reset a stage

In `_handle_response`:

- a same-rid output change bumps the generation only for the current rid or a
  pending request, never for a stale rid;
- only a pending request is cleared when an `ok` answer arrives;
- a live stage skips frame storage and generation pruning entirely;
- applying the current answer still tags the object, so a downstream stage sees
  the new prefix on the next pass.

### 8. Protocol

`hello.stages[]` adds optional `has_stage_in` (bool). No request/response schema
changes are needed. `docs/protocol.md` documents the field and the generator
semantics. The `hello` fixture remains a valid old-version frame; no golden
fixture is regenerated for an optional additive field.

### 9. Stages without a Stage Out are one-way

`hello.stages[]` also publishes `has_stage_out`, derived from the live Stage Out
claims. A key with a Stage In but no Stage Out is a **one-way consumer**:

- Blender still evaluates the prefix and delivers it to the Stage In;
- no response is ever expected, so the stage never arms a pending wait, never
  applies or caches a response, and a late answer is dropped;
- the playback gate treats it as ready after the delivery instead of waiting
  for an answer that cannot come.

An absent field means an older client, whose claim is unknown; the saved two-way
path stays. A stage with neither a Stage In nor a Stage Out is not published.

## Consequences

- A request for stage B recomputes only B's affected branch; an independent A
  does not advance and does not answer. A dependent A that a shared source
  recomputed answers only when its output changed.
- A request for stage A recomputes only A; B waits for A's applied output and is
  requested by the next pass, as the stack requires. If a shared source also
  changes B during A's solve, B answers with its own rid so its object follows;
  an independent B stays quiet.
- One request produces one solve and one Stage Out answer per requested stage,
  plus one answer for a dependent stage whose output changed with no newer
  request queued.
- The generator advances exactly once per Blender frame. The frame-0 reset is
  Grasshopper's, not the plugin's.
- A full frame where both stages change still costs two serial solves, because
  B's prefix contains A's applied output. This patch removes redundant solves,
  unrelated branch work and cross-stage answers; it does not make a dependent
  stack parallel.
- The generator is first-class: no dummy Stage In, no parameters on Stage In,
  no inversion of the parameter schema. Grasshopper remains the declaration
  authority; Blender owns values, keyframes and drivers.
- The frame cache remains exact for deterministic stages. Live stages trade
  replay for native solver state.
- New stages start live, so a stateful graph is correct without a manual mode
  switch; a deterministic graph opts out of the cache with one checkbox.
- A Stage In without a Stage Out no longer blocks timeline playback or times
  out against a document that cannot answer; it is a one-way geometry feed.

## Out of scope

- Stage In geometry-build reuse for repeated refs (a separate conversion
  optimisation).
- Blender-declared parameter types/schemas. The current split is retained.
- Multi-stage batch solves that would remove the dependent-stack round trip.
- A `geom.mode = "none"` wire value; generator requests keep the v2 full/ref
  envelope with an empty geometry hash.

## Verification

C# console tests:

- hello reports `has_stage_in` for Stage In and Stage Out-only keys;
- hello reports `has_stage_out` and republishes when either presence flips;
- a request pins/answers only its requested key;
- a dependent Stage Out with no queued request answers its own rid when a
  shared source changed its output, and stays quiet when it has a newer request
  queued;
- a user-driven solve can still answer an affected Stage Out;
- queued requests coalesce into one scheduled solve and retry queues waiting
  keys;
- an unchanged user-driven answer is not re-sent.

Blender headless tests:

- a generator stage skips prefix evaluation and base-geometry edits do not
  change its request id;
- a live stage sends again on a frame change even when the rid is cached, does
  not send when another stage's apply merely dirties the object, and does not
  write frame-cache entries or bump its generation on same-rid changes;
- a Stage In-only stage delivers each frame without arming a wait or blocking
  the playback gate, and an absent `has_stage_out` keeps the saved two-way mode;
- a stale `ok` does not bump a generation or clear a newer pending request;
- the existing deterministic cache/replay tests stay green.

Live Rhino/Grasshopper acceptance (user):

- the reference CatenaryMST file advances one Kangaroo step per Blender frame;
- a request for the second stage does not advance the first;
- each request returns one response per requested stage;
- scrub to 0 resets Kangaroo as native GH does;
- non-live stages still replay from the warm frame cache.
