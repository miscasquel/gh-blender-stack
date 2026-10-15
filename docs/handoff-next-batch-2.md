# Handoff — the decoder boundary, the pass's cost, and failures nobody can see

**Status:** proposed, not started. Written 2026-10-13 from a full read of the tree
at `6cc4cb1` (v0.1.2), and from a verification pass rather than an inference one:
all four layers were re-run, and the three claims this document leans on hardest
were **reproduced**, not read off the code. Those three carry their evidence inline,
with the conditions to re-run them.

**Relation to the other documents.** `docs/polish-plan.md` remains the full backlog
with every item's evidence. `docs/handoff-next-batch.md` is the previous slice and
its outcome is recorded there. This is the next slice; findings that are not already
in the plan are tagged **`N1`–`N9`** and are not yet folded into the plan below.

**Baseline, all green at `6cc4cb1`, re-run 2026-10-13 on this machine:**

| layer | command | result |
|---|---|---|
| A, pure core | `scripts/test-core` | 60 tests, OK (49 after the `rules.py` removal, below) |
| B, Blender headless | `scripts/test-blender` | 86 tests, OK (35 s) |
| C, C# under wine | `scripts/test-gha` | 10 fixtures PASS, `param-values` PASS, `decoder-boundary` PASS |
| D-lite, extension | `scripts/test-extension` | PASS (websockets from the wheel, round trip from the install location) |
| deployment | `scripts/check-deployed` | deployed `.gha` matches the build |

The share of that which matters: the previous batch's five items are all genuinely
in, with negative controls, and nothing regressed.

---

## The batch, and why this order

| # | Item | Why here |
|---|---|---|
| 1 | **N1** `Decode`'s contract is still not total | It is the one-layer-deeper twin of the bug `decoder-boundary` was written to pin. The test group claims the contract; three inputs break it. Same file, same build cycle, S. |
| 2 | **N8** a SubD crease that cannot be mapped is dropped silently | **Inserted** relative to the review's order. It shares item 1's `.gha` build + deploy + Rhino restart, and in this project that cycle is the expensive part. It is also a silent loss, which is the tier this repo treats as unforgivable (D2, principle 6). |
| 3 | **N3** a truncated node-tree signature silently pays the full clone every pass | An internal condition with no surface: a cost the user is paying and nobody can see. S, and independent of everything else. **N7 is closed as decided-against** — see item 3. |
| 4 | **the prefs/engine registration order** | One line, and it is a *verified*, fully deterministic user-visible bug: a saved port is ignored and the panel reports the wrong one. |
| 5 | **N4** one object-pointer property costs more than the whole documented memo floor | Measured, and the number is large enough to change P4-1's conclusion. It is a *recording* task first: measure, write the number where the profile lives, then decide. |
| 6 | The hygiene tier, with the counts fixed for the last time | N2, N5, N6, N9, dead code, `spikes/`. No behaviour change; the suites staying green is the whole verification. Best done when someone is already in those files — which this batch puts them in for items 1, 2 and 4. |

Items 1 and 2 land in one `.gha`; do them in one edit–build–deploy–restart cycle.
Items 3 and 4 are pure Blender and fully testable in layer B. Item 5 is a recording
task before it is a change. Item 6 is last because it touches the most files for the
least risk reduction.

---

## Deliberately not in this batch

- **P8-3 · `core/rules.py` — resolved before this batch started.** Deleted, with its
  11 layer-A tests (layer A: 60 → 49). The record of why is in
  `docs/polish-plan.md` P8-3; the short version: adopting it changes nothing a user
  can see, and it had already drifted from the engine it described — 8 parameters
  with 3 unused, and no `unknown_ref` (`engine.py:276`) or generation counter
  (`:310`). Documenting a drifted module as the spec would enshrine the wrong rules,
  and a careless adoption would drop the `unknown_ref` recovery P0-1 fixed. The
  apply rules live in `docs/protocol.md` §6.4 and the request side in `plan.md` §9.1.
  **One consequence to not lose:** `rules.py` was also the more complete of the two
  copies of the stage-state vocabulary (it had `disabled`, `frozen`, `unbound`;
  `stack.py:25-29` has only five states, and the engine still writes the bare literal
  `"disabled"` at `engine.py:654` and `ui.py:177`). That inconsistency is now
  entirely P4-6's third bullet, which should be done while someone is next in
  `stack.py` — not as part of this batch.
- **P0-9 piece 3 (units).** Still the largest untested axis, still latent: the
  development document is in metres, and the conversion is silent in both
  directions. It needs a non-metres Rhino document to test honestly, which means a
  live session, not a suite. Keep it as backlog rather than fake it.
- **P5-1 (`int` truncates in C# where Python rounds).** Cheap, but it changes a
  value on the wire and therefore a request ID, and it wants its own negative
  control. The next time the plugin is opened for something else, not now.
- **P6-12, P6-14, P6-17 and the rest of the mechanical C# tier.** Real, low risk,
  and each one is a chance to break a conversion that has no automated test. Fold
  them into whichever item next touches `Conversion.cs` — which is item 2.
- **Any change to the one-in-flight rule.** The previous batch wrote down why it
  exists and why a limit above one was refused. Do not reopen it without a reason.
- **N9 as an implementation.** The decision is what item 6 needs, not the code; see
  the hygiene tier.

---

## Standing constraints from the owner

Quoted in `handoff-next-batch.md`; the ones that bear on this batch:

- *"Feel free to make cleanups."* — item 6 is authorised.
- *"The solution MUST be clean and minimal… if we need to simplify the logic… I
  would consider it."* — item 6 is cleanup, not redesign: no abstraction is added,
  nothing is restructured. The same constraint is why `core/rules.py` is dropped
  rather than refactored into the pass (see "Deliberately not in this batch").
- *"One evaluation at a time for now."* — untouched here.

---

## Where things stand

`handoff-next-batch.md` §"Where things stand" and §"Three invariants" are current
and should be read before touching the pass. The short version: one pass per stage
(prefix → hash → rid → cache → send), `_pending` holds at most one evaluation,
the cache lookup runs *before* the gate, and `_prefix_revision` must name every
input the evaluated prefix reads.

Item 5 touches that area, so budget a re-read. Nothing in this batch
changes the invariants themselves.

---

## Item 1 — N1: `Decode`'s contract is still not total

**Where:** `grasshopper/src/core/Json.cs:337-347` (`GetString`, `GetLong`), called
from `Frame.cs:220`, `:223` and `:227`.

**Failure, reproduced 2026-10-13.** The core compiled exactly as
`grasshopper/build/build.sh` compiles it, decoding three hand-built frames (the same
way the new tests build them: header text, length prefix, no payload accounting):

```
dtype is a number:  InvalidCastException  <-- RAW, a caller cannot catch this
bytes is a string:  FormatException       <-- RAW, a caller cannot catch this
name is a number:   InvalidCastException  <-- RAW, a caller cannot catch this
```

Frames: `{"type":"ping","v":2,"sections":[{"name":"mesh.verts","dtype":5,…}]}`,
the same with `"bytes":"x"`, and the same with `"name":7`.

**Why it matters.** `Decode` documents `FrameException` as its failure mode, and
`decoder-boundary` was written to pin exactly that — but every case in it is a frame
the *encoder* can produce, where `dtype` and `bytes` are always the right JSON type.
The byte-level path (`FrameWithHeader`) is used once, for truncated JSON, so the
group tests the contract one layer shallower than it claims.

The blast radius is contained, and that half is genuinely fixed: `ReceiveLoop`'s
last-resort catch notes the frame and keeps reading, so the connection survives. What
is lost is the *diagnosis* — protocol §4.6 says a malformed frame gets `bad_frame`,
and today it gets silence and a `Note` with a .NET type name in it.

**Design.** Both converters are three lines.
- `GetString` → `v as string`. A non-string returns `null`, which `Frame.cs:220`'s
  existing empty-name check then converts into a proper `bad_frame`, and which
  `GhLink.Handle`'s `type`/`stage`/`rid` reads already tolerate as "absent".
- `GetLong` → guard on `Json.IsNumber` (it exists, `Json.cs:322`) and throw
  `FrameException("bad_frame", …)`, exactly as the version check at `Frame.cs:199`
  now does.

**Tests.** Three cases in `CheckDecoderBoundary`, built with `FrameWithHeader` —
which already exists for precisely this. Assert the code is `bad_frame`, not merely
"an exception": `CheckFrameProblem` already distinguishes those.

**Verify:** `scripts/test-gha`. Against the old code the three cases report raw
exception names; the negative control is the current output above.

**Effort:** S.

---

## Item 2 — N8: a SubD crease that cannot be mapped is dropped silently

**Where:** `grasshopper/src/core/Conversion.cs:617-658` (`SubDFromSections`); the
silent `continue` is `:647`.

**What the code assumes.** The crease section carries *Blender's cage vertex
indices*. `byPair` at `:630-637` is keyed by the *SubD's* vertex indices, built from
`subd.Edges`. The two sets are matched by index, on the assumption that
`SubD.CreateFromMesh(cage)` preserves vertex order and count.

**Why it matters.** That assumption is unverified, and the plan's own evidence log
cautions about it: `Mesh.CreateFromSubDControlNet` — the *outgoing* direction —
"returns seam-duplicated control points (19 for a 16-point open cage) and vertex
order is not stable across SubD round trips". So the repo already knows vertex
identity is not preserved in one direction of this round trip and relies on it in
the other, with nothing to say so. If it is wrong, creases silently do not transfer
— and D2 plus principle 6 both say every such loss is reported.

This is deliberately framed as *unverified*, not as a bug: the *silent* part is the
defect regardless of whether the mapping is right, and it is 3 lines to fix either
way.

**Design.**
1. Count the misses. Today `if (!byPair.TryGetValue(…)) continue;` — increment a
   counter there.
2. Warn when it is non-zero, with the count, in the same `warnings` list the
   fractional-crease path already uses at `:654`.
3. Then verify the assumption once, with a cage whose creases are asymmetric (a
   crease on one edge only), through a live round trip. If the mapping *is* wrong,
   the fix is to match by position instead: for each incoming creased pair, take the
   SubD edge whose two endpoints are nearest that pair's cage vertex positions (the
   cage is the incoming mesh, so both sides have positions), and warn when a pair
   matches nothing inside a tolerance.

Record the outcome in the plan's §19 evidence log either way — "verified, order is
preserved" is as useful to the next person as a fix.

**Tests.** The warning is reachable without Rhino only by construction, so this is
inspection plus the live session. The live session needs one cage with a single
creased edge; the warning should be absent, and if it appears, the assumption is
dead.

**Verify:** `grasshopper/build/build-gha.sh` (which compiles `Conversion.cs` against
RhinoCommon — `scripts/test-gha` builds only the codec), then
`grasshopper/deploy/deploy.sh`, `scripts/check-deployed`, and a Rhino restart.

**Effort:** S for the warning; M if the mapping turns out to be wrong.

---

## Item 3 — N3: a truncated node-tree signature silently pays the full clone every pass

**Where:** `engine.py:65` (the counter) and `:923` (the increment).

`grep -rn signature_truncated blender/gh_stack blender/tests tools` matches exactly
those two lines: it is incremented and never read, not even by a test. The depth cap
itself is right — hitting it returns `None`, which propagates as "do not memoize" —
but the consequence is the *most expensive* path in the addon: a full clone on every
pass — 45 ms at 50k triangles by the plan's own P4-1 measurement — with nothing
anywhere saying why. That is the "why is it slow" report nobody can answer from the
UI.

**Done, one deviation.** The counter is shown in the Diagnostics panel (`ui.py`,
`GHSTACK_PT_diagnostics`), which is the proposed surface and is opt-in by being a
collapsed child panel, so nothing is added to the panel users read every day. The
deviation: no `_warned_truncated` per-stack print-once set. The panel line is the
surface, the console already prints the fallback's cause where it decides not to
memoize, and a second warning channel was one more thing to keep in step.

**Test.** `TruncatedSignatureTests` (layer B) builds a chain `_MAX_NODE_TREE_DEPTH`
deep and pins both halves: `_node_tree_signature` returns `None` rather than a
truncated signature, and `_prefix_revision` returns `None` with it, so the prefix is
re-evaluated every pass — the fallback is a cost, not a failure, and the stage
settles anyway. The counter assertion in it fails if the counter is ever dropped
again.

### N7 — the only way to retry a stage discards its cache

**Where:** `ops.py:62` (`gh_stack.push`) → `engine.push` (`engine.py:1069-1082`) →
`_bump_generation` for every stage (`:535`), which prunes every stored frame.

After an error the stage is deliberately not re-asked — the last batch removed the
dirty-add for exactly the right reason (protocol §7: retry on the next change or
Push). But the only manual recovery is Push, and Push's stated purpose is "get past
anything cached", so a user whose solve failed once has to choose between touching
the scene to force a change and destroying their frame cache.

**Built, then removed on request.** `gh_stack.retry` landed in `cfcab50`: operator,
panel button, and a layer-B test asserting the stage recovers *and* `_cache` is
unchanged, the second assertion being the one that distinguishes it from Push. It
was then reverted at the user's request: the panel already carried
`Add Stage | Push`, and a third button plus a second recovery path was judged UI
surface for a condition a user hits rarely. `engine.request_again` remains, is still
what `add_stage` calls, and now also retires the stage's memoized prefix — the part
of this item that was worth keeping.

If a stuck stage needs one, it is a ten-line re-add: an operator calling
`engine.request_again(context.object)`, a row button, and a test that asserts the
frame count survives. The record of why it is not there is this paragraph, so the
next reader does not re-derive it or assume it was forgotten.

**Effort:** S, and already spent once.

---

## Item 4 — the server starts before the preferences exist (verified)

**Where:** `blender/gh_stack/__init__.py:37` (`"prefs"` is last in `MODULE_ORDER`);
`engine.register()` → `start_server()` at `engine.py:1113` → `engine.py:1048`
(`port = int(prefs._pref("port", 9130))`).

**Failure, reproduced 2026-10-13 across two Blender launches** on a throwaway
config (the `scripts/test-extension` pattern: `BLENDER_USER_CONFIG` +
`BLENDER_USER_SCRIPTS` with the working tree symlinked as the add-on):

```
run 1:  SET:   prefs.port=9141   server._port=9130
run 2:  CHECK: prefs.port=9141   server._port=9130   running=True
run 2:  CHECK: after an explicit start_server(): server._port=9141
```

Blender reads the saved preference correctly — the preference is fine. The server
does not, because `GhStackPreferences` is registered *after* `engine.register()`
starts it, so `_pref` falls back to the default. This is deterministic, not
intermittent: `unregister()` runs the modules in reverse, so every enable has the
same ordering, and the only path that reads the right port is the **Start server**
button (`ops.py:91`), which runs after registration has finished.

**The part that makes it worse than a wrong port.** `ui._port()` (`ui.py:181`) reads
the *preference*, and the panel prints it while listening. So with port 9141 saved,
the panel says "Listening on port 9141" while the socket is on 9130. A user
debugging a connection sees two numbers that disagree and neither is obviously
wrong.

**Design.** Move `"prefs"` to the head of `MODULE_ORDER`. That is the whole fix —
the comment above the tuple already says registration order matters, it just has
`prefs` in the wrong place.

While there: have `_port()` report `server.SERVER._port` when the server is running,
so the panel can never disagree with the socket again.

**Test:** layer B can assert it directly, and this is the kind of test the suite has
no coverage for today: set `preferences.port` to a non-default, disable and re-enable
the add-on (or call `engine.start_server()` on a fresh registration) and assert
`server.SERVER._port` matches the preference. Note the shape of the test that would
*have* caught it: it must save the preference and reload, not set it after enabling —
setting it afterwards already works, which is why nobody noticed.

**Verify:** `scripts/test-blender`; a manual check that the panel and the socket
agree with a non-default port.

**Effort:** S.

---

## Item 5 — N4: one object-pointer property costs more than the whole documented memo floor (resolved)

**Where:** `engine._modifier_descriptor` → `_pointer_state` → `_object_geometry_hash`
(`engine.py:837-878`).

**Measured 2026-10-13** (Blender 5.2.2; a prefix `ARRAY` modifier with
`offset_object` pointed at a dense ico-sphere; `_modifier_descriptor` timed over 5
runs and the minimum reported):

| target | descriptor, no pointer | with one object pointer | the referenced read + hash alone |
|---|---:|---:|---:|
| 5,120 tris | 0.020 ms | 0.648 ms (+0.628 ms) | 0.623 ms |
| 81,920 tris | 0.019 ms | **10.004 ms** (+9.985 ms) | 9.973 ms |

**Why it matters.** P4-1 concluded "89% of the memo-hit floor is the geometry read,
and gating the descriptor walks is 0.41 ms of 8.3 — so do not optimise it." That
profile was taken on a prefix with **no pointer properties**. One object-pointer
property — `Array.offset_object`, `Mirror.mirror_object`, `Shrinkwrap.target`,
`Boolean.object`, `SimpleDeform.origin` — adds a full serialize + SHA-256 of a
*second* datablock on **every pass**, additive per pointer, and unbounded by the
prefix's own geometry. It is paid on the memo *hit* path: the cheap path.

Put in P4-1's terms, and with the caveat that the densities are not identical:
- at **5k triangles** one pointer adds 0.63 ms, against P4-1's 0.99 ms memo-hit floor
  at 4k — so a single pointer property is about **60% of the entire floor**, and it
  is not the 0.41 ms the profile measured;
- at **82k triangles** one pointer adds 10.0 ms, the same order as P4-1's 8.3 ms
  floor at 50k. That is why the density caveat matters: the ratio between the two
  rows is the signal, not the absolute.

**Resolved.** The sound content signal P4-1 could not find does not exist, so the
fix is not to find one but to stop asking: a referenced target's geometry hash is
cached across passes and retired when the depsgraph tags that target. The update
list resolves to *evaluated* IDs, whose pointers are not the originals' — a tagged
mesh and the mesh it was copied from sit at different addresses, so a pointer match
never matches — and the cache is keyed by `(as_pointer, object name, data name)`:
names are unique for a type, and the pointer keeps two objects that swapped names
apart. `_apply` clears the cache outright, because the engine's own writes are the
one geometry change that never comes through the depsgraph.

Measured, `Array.offset_object` -> a grid mesh, minimum of 5 (the table's own method):

| target | hash, cold | hash, warm (a memo-hit pass) | saved per pass |
|---|---:|---:|---:|
| 9,522 tris | 0.789 ms | 0.020 ms | 0.769 ms |
| 38,642 tris | 3.167 ms | 0.019 ms | 3.147 ms |
| 114,242 tris | 9.284 ms | 0.020 ms | 9.264 ms |

Per pointer, additive, on the memo hit path — the reproduction of Item 5's own
10.0 ms at 82k that settles its "decide" as implemented. Layer B pins the soundness
side, not the speed: a target vertex move must move the prefix revision (with the
retirement broken it does not), and the hash is computed once until something
changes.

The remaining question this does not answer, and neither does the old enumeration:
the memo *hit* floor is now dominated by the stack object's own base read, which
only the epoch design removes.
Blender gives no content counter for a datablock, which is the whole difficulty in
P4-1; the target object has exactly the same problem as the prefix. So:

1. Put these numbers in `docs/polish-plan.md` P4-1, with the conditions.
2. Then apply P4-1's own framework. Option 1 ("accept it and write the number down")
   was justified by a 0.41 ms descriptor + signature walk; that figure did not include
   a pointer property's geometry hash, which is 0.63 ms at 5k triangles and 10.0 ms at
   82k. Option 2 (buffer reuse) shares its fix with the prefix read and is the
   cheapest sound win. Option 3 (a sound content signal) is where this really lands,
   and it is design-first for both.
3. **Do not** add a heuristic. The trap is identical to the one P0-8 was: a vertex
   moved with no count change. A cheap gate here reintroduces silent stale geometry
   with a *second* datablock to blame.

**One observation that is not a fix.** `_pointer_state` already hashes
`matrix_world` alongside the geometry, and for some pointer properties the geometry
is irrelevant — an Array's offset object contributes a transform. For others it is
the whole point — a Shrinkwrap target. Encoding that distinction means per-modifier
knowledge, which is precisely the fragile design P4-1 rejected for the descriptor
walk. Note it and move on.

**Verify:** re-run the measurement at two target densities; the ratio is the signal,
not the absolute. `_pointer_state` is reachable with any modifier carrying an object
pointer placed before the stage.

**Effort:** S to record and re-scope; the underlying cost is P4-1's effort.

---

## Item 6 — the hygiene tier, and the counts for the last time

No behaviour change intended anywhere below. The suites staying green is the whole
verification.

**N2 · the test counts — half done, and the half that is left is the interesting one.**
The README's counts (60 / 69, both wrong) were removed in the same commit as the
`rules.py` deletion, which is the fix the plan itself recommends: *"a count in a README
is stale the moment a test is added."* What remains:
- `docs/polish-plan.md:41` "72 headless" is P8-1's original baseline at `ea0f651`,
  not drift: P8-1 was closed by *removing* the counts, so the baseline sentence is
  history, and the runner now prints **96**. Nothing to do; recorded here because
  this item looked like it said otherwise.
- The open question is whether to *derive* the numbers instead. `run.py` already
  prints them and CI already runs layer A, so an assertion or a badge is nearly free;
  what must not happen is a fourth hand-edit.

**N5 · `--coalesce` is harsher than the client, and the help text does not say so.**
The window is fixed at the first request's arrival, so a request arriving a
millisecond before it closes is dropped; the real `ScheduleSolution` path re-arms on
each request. Being harsher is defensible for a double — arguably better — but a
future reader could draw the wrong conclusion from a green test. One sentence in
`--coalesce`'s help.

**N6 · the version string is in six places and only two are checked.**
`blender_manifest.toml:5`, `blender/gh_stack/__init__.py:17` (cross-referenced, per
P8-4), `GhLink.cs:538` (`hello["client"]`, *no* cross-reference),
`docs/protocol.md:47` (the §4.1 example), `README.md:8`, `CHANGELOG.md`. The manifest
is the only one the release path enforces. Add the pointer comment to `GhLink.cs` and
the protocol example, or have `check-deployed` echo the client string it expects.

**N9 · decide `ping`/`pong`, because the spec currently promises it.**
`protocol.md` §2 specifies a 2 s ping from Blender and a 6 s `disconnected` verdict;
§4.5 defines the messages; a `ping` fixture exists and both codecs test it. Nothing
in the product sends either. The transport does have `websockets`' keepalive
(`ping_interval`, 20 s), so TCP liveness is covered — but a half-open peer costs the
full request timeout (30 s) to notice, which is exactly the window §2 was written to
close. Either implement the application-level ping or amend §2 and say the keepalive
is the transport's. Do not leave a spec that lies. S either way, and the decision
should be recorded as an ADR if §2 changes.

**The mechanical tier, still counted and still there.** `bridge.post_outgoing` /
`take_outgoing` (`bridge.py:44,48`); `geometry_io.evaluated_mesh` (`:737`) and the
four `*_SECTION_ORDER` tuples, which actively mislead because the live table is
`hashing.HASH_SECTION_ORDER`; `props.TYPE_DEFAULTS` (`:13`); `GhLink.Republish`
(`:578`); `Conversion.meshedBreps` (`:711,720`); four no-op `register`/`unregister`
pairs (`helpers.py:90`, `nodes.py:131`, `props.py:311`, `server.py:187` — keep
`server.unregister`, it calls `SERVER.stop()`). All verified still present at
`6cc4cb1`; line counts are in the plan's P6 table.

**`spikes/`.** 71 tracked files, of which `docs/plan.md` §19 cites about twelve.
`spikes/s6/probe_api.exe` and
`blender/gh_stack/vendor/websockets/speedups.cpython-314-x86_64-linux-gnu.so` are
still tracked (`git ls-files`); `.gitignore` already ignores the `s1` twins. Keep
what §19 cites, delete the rest, add the two ignore rules. The `.so` is not in the
declared wheel and `frames.py` falls back to pure Python without it.

**Still open from the smaller plan items, deliberately not reordered here:**
P0-6 (units read off the UI thread), P1-2 (`Stage In` has no exception guard — note
it is the *incoming* half of item 2's file, so it is nearly free while there),
P3-5 (the plugin's registries grow without bound), P4-4 (quadratic eviction in
`engine.py:585` and `cache.py:108`), P5-1, P6-1, P6-12, P6-15, P6-17. Each has its
own evidence in the plan.

---

## Working rules this batch should keep

All seven in `handoff-next-batch.md` §"Working rules" still apply and none is
repeated here. The ones this batch exercises specifically:

- **Rule 1 (verify call sites)** — this document's items 1, 2 and 4 exist because
  the claims in them were *run*, not inferred. Item 4 is the one that would have been
  filed wrongly: reading the code suggests an ordering hazard; measuring it shows a
  deterministic wrong port and a panel that reports a different one. Different item,
  different priority.
- **Rule 2 (a negative control per fix)** — items 1, 3 and 4 each have one, and item
  1's is already written down here as the "against the old code" output.
- **Rule 3 (check the deployment)** — items 1 and 2 land in the `.gha`;
  `scripts/check-deployed` is the gate, and a Rhino restart is still part of
  deploying.

---

## Verification

```
scripts/test-core         # layer A: pure Python core, no Blender
scripts/test-blender      # layer B: Blender headless against tools/fake_gh
scripts/test-gha          # layer C: the C# codec under wine
scripts/test-extension    # layer D-lite: build, install, round trip
scripts/check-deployed    # after any .gha change
```

Baseline to beat: **49 / 86 / 10 fixtures + 2 groups / PASS**, all verified at
`6cc4cb1` on 2026-10-13 (layer A was 60 there; `core/rules.py` and its 11 tests
were removed before this batch started — see "Deliberately not in this batch").
Items 1 and 2 also want `grasshopper/build/build-gha.sh`
(which compiles `GhLink.cs` and `Components.cs` against RhinoCommon —
`scripts/test-gha` builds only the codec) and then `deploy.sh` plus a Rhino restart.

For items 1, 3 and 4, add the failing case to the suite *before* the fix and keep
the negative control in the commit message — the same discipline as the last batch,
which is why those items are findable at all.

---

## Open questions

1. **Does `SubD.CreateFromMesh` preserve vertex order and count?** Item 2 only makes
   the failure visible; it does not answer this. One live cage with one creased edge
   answers it.
2. **Is `_pointer_state`'s geometry hash ever *necessary*, for any modifier whose
   pointer property this protocol carries?** An Array's offset object contributes a
   transform; a Shrinkwrap target contributes its geometry. If a subset are
   transform-only, their cost could be cut — but only by naming the modifiers, which
   is the per-modifier knowledge P4-1 rejected for the descriptor walk, and which
   would silently rot when Rhino or Blender changes a modifier. Worth one look to
   size the win; the likely answer is "no action, and the measurement stands as the
   record".
3. **Should the panel ever report the preference rather than the running port?**
   Item 4 fixes the disagreement by making them equal; a server that fails to bind
   (port already in use) still leaves the panel optimistic. `server.py:130-131`
   records the bind failure in `bridge.state["error"]`, which the panel shows — so
   maybe the answer is "no, the error line is enough". Confirm.
4. **Was the previous batch's `_handle_response` change (no dirty-add on error) ever
   exercised against a client that recovers on its own?** `SolveFailureTests` covers
   recovery via a scene change; a GH-side fix that re-sends for the same rid is a
   different path through the same code and is only covered incidentally by
   `GhEditResetTests`. Cheap to close while item 3 is in that file.

---

## Outcome

*(to be filled when carried out: what shipped, where it deviated from this plan, and
what the live session found — the format used by `handoff-next-batch.md`.)*
