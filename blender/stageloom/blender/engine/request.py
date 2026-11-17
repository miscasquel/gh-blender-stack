"""Prefix evaluation, request IDs, sends and the per-stage pass."""

import contextlib
import sys
import time

import bpy
import numpy as np

from ...core import CAP_BLOCKS_2
from ...core import attributes as attributes_mod, codec, hashing
from .. import (
    bridge,
    geometry_io,
    helpers,
    nodes,
    prefs,
    props,
    server,
    stack as stack_mod,
)
from .runtime import ENGINE, SOFT_RETRY_SECONDS


@contextlib.contextmanager
def _owned_scene(obj):
    """Make the object's own scene the evaluation scene for this read.

    ``bpy.context.evaluated_depsgraph_get()`` and the frame-dependent part of a
    prefix belong to the *active* scene. A stage on an object in a non-active
    scene therefore needs its scene activated for the clone evaluation, then
    restored. A normal single-scene session never pays for the switch (the
    context scene already is the object's scene).
    """
    scene = helpers.scene_of(obj)
    window = getattr(bpy.context, "window", None)
    current = getattr(window, "scene", None) if window is not None else None
    if scene is None or window is None or current == scene:
        yield scene
        return
    previous_layer = getattr(window, "view_layer", None)
    ENGINE.switching_scene = True
    try:
        window.scene = scene
        try:
            yield scene
        finally:
            window.scene = current
            if previous_layer is not None:
                try:
                    window.view_layer = previous_layer
                except (AttributeError, TypeError, ValueError):
                    pass
    finally:
        ENGINE.switching_scene = False


def _set(stage, field, value):
    """Assign a stage field only when it changes. An unchanged write would tag
    the depsgraph and restart Cycles accumulation for nothing."""
    if getattr(stage, field) != value:
        setattr(stage, field, value)
        return True
    return False


def _set_input_warnings(stage, notes):
    """Publish this prefix read's conversion notes when they changed.

    ``input_warnings`` is runtime display state, never part of the request or
    the hash. An unchanged string is not rewritten, so a quiet pass does not
    tag the UI.
    """
    text = "\n".join(note for note in (notes or []) if note)
    _set(stage, "input_warnings", text)


def _peer_blocks_2():
    """Whether the connected peer advertised ``blocks/2`` (protocol §8).

    An absent list means no capabilities. The sender must not emit a multi-part
    definition to a peer that cannot read one: it splits it into single-part
    definitions and leaves a visible note instead.
    """
    caps = bridge.state.get("caps") or ()
    if not isinstance(caps, (list, tuple, set, frozenset)):
        return False
    return CAP_BLOCKS_2 in caps


def _prefix_result(result):
    """``(sections, attributes, definitions, notes)`` from a prefix read.

    Batch/memo entries written before the notes channel may be three-tuples;
    treating them as note-less keeps old session state and test fixtures valid.
    """
    if len(result) >= 4:
        return result[0], result[1], result[2], result[3] or []
    if len(result) >= 3:
        return result[0], result[1], result[2], []
    return {}, [], [], []


_EMPTY_GEOMETRY_HASH = None

# The collections an Edit Mode clone's private copy can belong to. Named by the
# ID type's RNA identifier, which is what the copy reports.
_OWNED_COLLECTIONS = {
    "Mesh": "meshes",
    "Curve": "curves",
    "PointCloud": "pointclouds",
}


def _empty_geometry_hash():
    """The geometry hash of a generator stage: the hash of no sections.

    Computed once because every generator request names it, and it can never
    change: a generator has no Stage In, so the object's geometry must not move
    its request id.
    """
    global _EMPTY_GEOMETRY_HASH
    if _EMPTY_GEOMETRY_HASH is None:
        _EMPTY_GEOMETRY_HASH = hashing.geometry_hash({})
    return _EMPTY_GEOMETRY_HASH


def _arm_pull(key):
    """Allow one request while frozen, with a deadline.

    A Pull is a request like any other: if its answer never lands, waiting
    forever would leave the stall nudge dirtying the object once a second. The
    permit carries the same bound as a request timeout, so the stage settles
    back to frozen and keeps its last output.
    """
    ENGINE.arm_pull(key[0], key[1], time.monotonic() + _timeout())


def _clear_pull(key):
    """Drop one Pull permit and its deadline together."""
    ENGINE.clear_pull(key[0], key[1])


def _pull_expired(key, now=None):
    """Whether a Pull permit has outlived its bounded wait.

    A permit planted directly (a test, or state from before deadlines existed)
    gets the same bound from the first pass that sees it.
    """
    now = time.monotonic() if now is None else now
    record = ENGINE.runtime(key[0], key[1])
    deadline = record.pull_deadline
    if deadline is None:
        deadline = now + _timeout()
        record.pull_deadline = deadline
    return now >= deadline


def _expire_pull(record, stage):
    """Settle a Pull whose answer never came: frozen, with a visible error."""
    key = (record.stack_id, stage.key)
    timeout = _timeout()
    ENGINE.clear_pull(key[0], key[1])
    ENGINE.forget(record.stack_id, stage.key)
    _set(stage, "rid", "")
    message = "Pull timed out after %.0f s; keeping the last output" % timeout
    # A frozen stage stays frozen and keeps the reason; the guard also clamps a
    # disabled stage to disabled and a conflicted key to conflict.
    ENGINE.set_state(stage, stack_mod.STATE_ERROR, error=message)
    line = "%s: Pull timed out after %.0f s; keeping the last output" % (
        stage.key,
        timeout,
    )
    ENGINE.trace(line)
    prefs.log("[stageloom] " + line, flush=True)


def _no_send(record, stage, reason, rid=None):
    """Explain why a pass that saw the scene move sent nothing.

    One line per decision: repeating the same reason for the same state does
    not append again; a new reason or a moved request id rewrites it. `rid`
    equal to `stage.rid` means nothing moved, which is a no-op, not silence.
    """
    if rid is not None and rid == stage.rid:
        return
    key = (record.stack_id, stage.key)
    token = (reason, rid)
    record = ENGINE.runtime(*key)
    if record.last_no_send == token:
        return
    record.last_no_send = token
    ENGINE.trace("%s: no send (%s)" % (stage.key, reason))


def _process_stage(obj, record, stage):
    """One stage's pass. Returns "waiting" (request in flight; stop the stack),
    "busy" (work happened; keep the fast timer), or "idle".

    Every delivered request is a solver step. A pass asks when the stage's own
    request id or the scene frame moved, or when a reconnect, load, resend or
    timeout armed a new ask. Another stage merely dirtying the shared object is
    not one of those: it must not advance this stage.
    """
    if ENGINE.stage_key_conflicted(stage.key):
        # A shared key has no proven owner: sending would make one claimant
        # speak for both. The pass leaves it visibly errored until resolved.
        _no_send(record, stage, "duplicate stage key")
        return "idle"
    owner = ENGINE.stage_owners.get(stage.key)
    if owner is not None and owner is not obj:
        # A runtime duplicate that has not been split yet must not send under
        # its old key; its own pass splits it first.
        _no_send(record, stage, "stage key owned by %s" % owner.name)
        return "idle"
    modifier = nodes.modifier_of(obj, stage)
    if modifier is None and not stage.modifier_uid:
        # A record saved before the uid existed, whose group is gone: the name
        # fallback still finds it, and the repair below re-links it.
        modifier = nodes.modifier_by_name(obj, stage)
    if modifier is None:
        # `_drop_removed_stages` runs first; this is a defensive skip only.
        _no_send(record, stage, "no stage")
        return "idle"
    active = nodes.modifier_active(modifier)
    # The modifier's own toggles are the control: a stage is active
    # while visible in any context, and the record mirrors that for the state
    # row. Nothing here ever writes a modifier flag back — that was the bug
    # where a toggle in the modifiers panel was undone by the next pass.
    _set(stage, "enabled", active)
    if not active:
        # A request already in flight is no longer awaited: leaving it pending
        # keeps the stall nudge dirtying the object forever for an answer the
        # disabled stage will ignore. Drop the sent bookkeeping too, so
        # re-enabling starts from the scene rather than assuming the client
        # still holds geometry for a request the stage never saw answered.
        ENGINE.forget(record.stack_id, stage.key)
        ENGINE.runtime(record.stack_id, stage.key).sent = None
        ENGINE.runtime(record.stack_id, stage.key).last_frame = None
        _set(stage, "rid", "")
        ENGINE.set_state(stage, stack_mod.STATE_DISABLED)
        _no_send(record, stage, "disabled")
        return "idle"
    ENGINE.repair_plumbing(obj, record, stage, modifier)
    key = (record.stack_id, stage.key)
    if stage.frozen:
        if ENGINE.runtime(*key).pull_permit and _pull_expired(key):
            _expire_pull(record, stage)
            _no_send(record, stage, "Pull timed out")
            return "idle"
        if not ENGINE.runtime(*key).pull_permit:
            # Freeze: no requests; the helpers keep their last output. Pull
            # (engine.request_again) plants a one-shot permit above and works
            # while frozen; it is consumed here, or when its answer lands.
            if ENGINE.is_pending(*key):
                # A Pull whose permit was cleared (freeze, load, conflict) has
                # no answer to wait for. Leaving it pending is what made the
                # stall nudge dirty the object once a second forever.
                ENGINE.forget(record.stack_id, stage.key)
                _set(stage, "rid", "")
            ENGINE.set_state(stage, stack_mod.STATE_FROZEN)
            _no_send(record, stage, "frozen")
            return "idle"

    connected = bool(bridge.state["connected"])
    if not connected:
        # No client can answer a new request. Drop any wait: it belonged to a
        # client that is gone, and a reconnect's hello resets the rest. The
        # stage keeps its last output, so there is no prefix read to pay for.
        ENGINE.forget(record.stack_id, stage.key)
        ENGINE.set_state(
            stage,
            stack_mod.STATE_UNBOUND
            if not stage.applied_out_hash
            else stack_mod.STATE_DISCONNECTED,
        )
        _no_send(record, stage, "disconnected")
        return "idle"

    # While a request is young, a pass costs nothing: the prefix does not have
    # to be read to know nothing can go out before the soft retry. Without this
    # every edit during a slow solve re-evaluated the whole prefix and threw it
    # away (measured: 62 clones for 13 sends on a 51k-vertex drag).
    pending = ENGINE.pending_of(record.stack_id, stage.key)
    if pending:
        sent_rid = next(iter(pending))
        if time.monotonic() - pending[sent_rid] < SOFT_RETRY_SECONDS:
            _no_send(record, stage, "waiting on rid %s" % sent_rid[-8:])
            return "waiting"

    started = time.monotonic()
    sections, geom_hash, attributes, definitions = _prefix_sections(obj, record, stage)
    ENGINE.record_timing(record, stage, "prefix_ms", ENGINE.timed_since(started))
    params_json = props.canonical_json(obj, stage)
    rid = hashing.request_id(stage.key, params_json, geom_hash, "m", "stack-local", stage.nonce)
    # A client may have connected or disconnected while the prefix evaluated.
    # The decision below must use the state at decision time, as before; only
    # the fast path above uses the state at entry.
    connected = bool(bridge.state["connected"])

    # `stage.rid` is deliberately NOT set here. It names the request the stage is
    # waiting on — the answer it must recognise as its own — so it is written only
    # when a request actually goes out. While a reply is in flight the playhead
    # may have moved on, and updating `stage.rid` up front would make
    # `_handle_response` discard that reply as stale: during playback nothing
    # would ever be applied and the output would freeze for as long as the round
    # trip takes. The wanted frame is requested on the next pass after this
    # answer lands.

    verify_key = (record.stack_id, stage.key)
    one_way = not stage.has_stage_out
    if one_way:
        # No Stage Out answers this key. Drop any wait from when it was
        # two-way and deliver the prefix to its Stage In without arming one.
        ENGINE.forget(record.stack_id, stage.key)

    if not connected:
        # With Grasshopper away the stage keeps its last output. A stage no
        # document has ever answered reads unbound instead of disconnected:
        # disconnected means a client was there and left.
        _no_send(record, stage, "disconnected", rid)
        ENGINE.set_state(
            stage,
            stack_mod.STATE_UNBOUND
            if not stage.applied_out_hash
            else stack_mod.STATE_DISCONNECTED,
        )
        return "idle"

    pending = ENGINE.pending_of(record.stack_id, stage.key)
    retry_note = ""
    if pending:
        # One evaluation at a time. While an answer is outstanding the stage asks
        # for nothing else — not even the frame the playhead has moved to.
        # Grasshopper solves on one thread in the order it receives, so asking for
        # frames while it works only builds a backlog it grinds through after the
        # user has moved on: the client stops responding and every queued frame is
        # one nobody is looking at by the time it arrives. The frame the scene is
        # on is requested on the first pass after this answer lands.
        sent_rid = next(iter(pending))
        sent_at = pending[sent_rid]
        age = time.monotonic() - sent_at
        if age <= _timeout():
            # The state may move on while an answer is in flight. After
            # SOFT_RETRY_SECONDS a newer request supersedes the stale one: GH
            # coalesces solves, so the ask is not a backlog, and the answer is
            # for what the user is looking at. The same state waits: the hard
            # timeout below is its resend.
            if rid == sent_rid or age < SOFT_RETRY_SECONDS:
                _no_send(record, stage, "waiting on rid %s" % sent_rid[-8:], rid)
                return "waiting"
            ENGINE.forget(record.stack_id, stage.key)
            ENGINE.trace("%s: superseded %s" % (stage.key, sent_rid[-8:]))
        else:
            # The answer never came. We cannot know whether Grasshopper received
            # the geometry, so the retry goes out in full — the same reasoning as
            # the unknown_ref handler. And the failure stays visible: retrying
            # silently is what made a stage stuck in this loop look healthy.
            ENGINE.forget(record.stack_id, stage.key)
            ENGINE.runtime(*verify_key).sent = None
            _set(stage, "geom_hash", "")
            ENGINE.stats()["timeouts"] += 1
            retry_note = "timed out after %.0f s; requesting the geometry again" % _timeout()

    # Every delivered request is a solver step. A pass that was only dirtied by
    # another stage's apply sees the same request id and the same frame, and must
    # stay idle: sending there would step the solver twice per frame. A
    # verification (reconnect/load/unknown_ref) and the timeout retry are the
    # explicit permits that bypass this gate.
    last_frame = ENGINE.runtime(*verify_key).last_frame
    if (
        rid == stage.rid
        and last_frame is not None
        and last_frame == helpers.frame_of(obj)
        and verify_key not in ENGINE.verify
        and not retry_note
    ):
        # Nothing moved, so the displayed output is still current. Settle any
        # transient state (an unfreeze or a repair leaves `idle`/`unbound`).
        ENGINE.set_state(stage, stack_mod.STATE_OK)
        _no_send(record, stage, "unchanged")
        return "idle"

    # Use the session truth, not the saved stage record: undo/redo restores
    # `stage.geom_hash` from its step, while the client keeps the geometry it
    # was last sent. No session entry means no client has been told anything
    # this session, so the first request must carry the geometry in full (§8.8).
    held_record = ENGINE.peek_runtime(*verify_key)
    held_geom = held_record.sent[1] if held_record is not None and held_record.sent else ""
    mode = "full" if geom_hash != held_geom else "ref"
    header = {
        "type": "request",
        "stage": stage.key,
        "rid": rid,
        "nonce": stage.nonce,
        "params": props.cast_values(obj, stage),
        "geom": {"mode": mode, "hash": geom_hash, "subd": bool(record.subd)},
        "units": "m",
        "space": "stack-local",
        # This client reads multi-part definition entries. An old GH ignores
        # the unknown field; a GH that needs blocks/2 and does not see a peer
        # cap falls back to one definition per part with a warning.
        "caps": [CAP_BLOCKS_2],
    }
    # Descriptors describe the attribute sections, so they travel only with the
    # sections they describe (a `ref` request carries none of either). The
    # definition table is identity-relevant (it is hashed into the request), so
    # it follows the same rule.
    if mode == "full" and attributes:
        header["attributes"] = attributes
    if mode == "full" and definitions:
        header["definitions"] = definitions
    started = time.monotonic()
    frame = codec.encode_frame(header, sections if mode == "full" else [])
    ENGINE.record_timing(record, stage, "encode_ms", ENGINE.timed_since(started))
    if not server.SERVER.send(frame):
        # No client socket to carry it. Recording a pending request here would
        # make the stage wait out a whole timeout for an answer nobody was asked
        # for — and the retry would be gated on that same pending entry.
        ENGINE.stats()["undelivered"] += 1
        ENGINE.set_state(stage, stack_mod.STATE_DISCONNECTED)
        _no_send(record, stage, "undelivered", rid)
        return "idle"
    ENGINE.verify.discard(verify_key)
    # The client has now been told this request; `_sent` is the session truth for
    # the next full/ref decision (until undo or a reconnect says otherwise).
    sent_record = ENGINE.runtime(*verify_key)
    sent_record.sent = (rid, geom_hash)
    sent_record.last_frame = helpers.frame_of(obj)
    # A Pull permit is held until the request it caused settles: a send that
    # never left (disconnected, undelivered) leaves the Pull armed for the next
    # pass, and an answer or the permit deadline releases it. A one-way stage
    # never answers, so its delivery is the end of the Pull.
    if one_way or not stage.frozen:
        ENGINE.clear_pull(record.stack_id, stage.key)
    ENGINE.trace(
        "%s: sent %s %s" % (stage.key, mode, rid[-8:] if rid else "")
    )
    if retry_note:
        prefs.log("[stageloom] %s: %s" % (stage.key, retry_note), flush=True)
    ENGINE.stats()["sends"] += 1
    # A send re-arms the "why no send" reason: the next silence is a new
    # decision, not a repeat of the pass before this one.
    ENGINE.runtime(record.stack_id, stage.key).last_no_send = None

    now = time.monotonic()
    ENGINE.heartbeat["sent_at"] = now
    if one_way:
        # The request delivered the prefix to the stage's Stage In. Nothing will
        # answer it, so do not arm a pending wait: the stack continues to later
        # stages and the playback gate never blocks on this one.
        _set(stage, "rid", rid)
        _set(stage, "geom_hash", geom_hash)
        ENGINE.set_state(stage, stack_mod.STATE_OK)
        _set(stage, "error", retry_note)
        return "idle"
    ENGINE.pending_of(record.stack_id, stage.key)[rid] = now
    ENGINE.runtime(record.stack_id, stage.key).state_since = now
    _set(stage, "rid", rid)
    _set(stage, "geom_hash", geom_hash)
    # A delivered request is the solver step: the same state and frame do not ask
    # again until the playhead or the stage's own inputs move on. An undelivered
    # send returns above and leaves the frame for the retry.
    ENGINE.set_state(stage, stack_mod.STATE_WAITING)
    _set(stage, "error", retry_note)
    return "waiting"


def _prefix_sections(obj, record, stage):
    """The geometry a stage sees: everything before its own modifier.

    Read from the evaluated clone in :func:`_clone_prefix`, never from the base
    datablocks: shape keys, Edit Mode data and instances are all part of the
    prefix the depsgraph evaluates. Surfaces stay on the meshing path (no
    geometry set). The memo is keyed by the depsgraph generation, so a hit is
    only possible while nothing has been tagged since the read.

    A generator stage has no Stage In: it consumes only Params, so the read is
    skipped entirely and the request names the empty geometry hash. Base
    geometry edits therefore cannot move its request id.
    """
    if stack_mod.is_generator(stage):
        _set_input_warnings(stage, [])
        return [], _empty_geometry_hash(), [], []

    if obj.type == "SURFACE":
        subd = bool(record.subd)
        notes = []
        sections, attributes = geometry_io.object_to_sections(
            obj, as_subd=subd, notes=notes
        )
        _set_input_warnings(stage, notes)
        return sections, _sections_hash(sections, attributes), attributes, []

    modifier = nodes.modifier_of(obj, stage)
    index = _modifier_index(obj, modifier)
    revision = _prefix_revision(obj, record)
    # Keyed by identity, not by name: as_pointer() is stable for the session and
    # keeps no reference, so a deleted object does not live on through this dict.
    memo_key = (obj.as_pointer(), stage.key)
    blocks_2 = _peer_blocks_2()
    memo = ENGINE.prefix_memo.get(memo_key)
    if memo is not None and memo.get("blocks_2") != blocks_2:
        # A different peer capability changes the definition namespaces this
        # read must produce; the memoized frame belongs to the other peer.
        memo = None
    if memo is not None and memo["revision"] == revision:
        ENGINE.stats()["prefix_memo_hits"] += 1
        _set_input_warnings(stage, memo.get("notes"))
        return memo["sections"], memo["geom_hash"], memo["attributes"], memo["definitions"]

    subd = bool(record.subd)
    batched = None
    if ENGINE.prefix_batching:
        pointer = obj.as_pointer()
        batched = ENGINE.prefix_batch.get((pointer, stage.key))
        if batched is None:
            _build_prefix_batch(obj, record, stage)
            batched = ENGINE.prefix_batch.get((pointer, stage.key))
    if batched is not None:
        sections, attributes, definitions, notes = _prefix_result(batched)
    else:
        notes = []
        sections, attributes, definitions = _clone_prefix(
            obj, index, subd, stage.key, notes=notes
        )
        ENGINE.stats()["prefix_clones"] += 1

    # Blender evaluates modifier-generated attribute data (a Bevel's UVs, for
    # example) with last-bit float variation between evaluations. Hashing that
    # verbatim makes an idle scene look changed, so the engine re-requests and
    # re-applies forever. When the new read differs from the memoized one only in
    # attribute floats within a tolerance, the memo wins: the stored sections,
    # hash and revision are kept, and nothing is sent. Any user-sized edit is far
    # above the tolerance and takes the normal path.
    if (
        memo is not None
        and _same_prefix(
            memo["sections"], memo["attributes"], memo["definitions"],
            sections, attributes, definitions,
        )
    ):
        ENGINE.stats()["prefix_memo_hits"] += 1
        sections = memo["sections"]
        attributes = memo["attributes"]
        definitions = memo["definitions"]
        notes = memo.get("notes") or []
        geom_hash = memo["geom_hash"]
        memo["revision"] = revision
        _set_input_warnings(stage, notes)
        return sections, geom_hash, attributes, definitions

    geom_hash = _sections_hash(sections, attributes, definitions)
    ENGINE.prefix_memo[memo_key] = {
        "revision": revision,
        "blocks_2": blocks_2,
        "sections": sections,
        "geom_hash": geom_hash,
        "attributes": attributes,
        "definitions": definitions,
        "notes": notes,
    }
    _set_input_warnings(stage, notes)
    return sections, geom_hash, attributes, definitions


def _same_prefix(old_sections, old_attributes, old_definitions,
                 new_sections, new_attributes, new_definitions):
    """Whether two prefix reads are the same geometry.

    Exact, except that f32 attribute payloads may differ within float noise
    (``rtol=1e-5``, which is ~80× the last-bit variation Blender shows). The
    descriptors, the definition table and every non-attribute section must match
    exactly: positions, topology, colors and creases are authored data, and an
    edit to any of them is a real change.
    """
    if old_attributes != new_attributes or old_definitions != new_definitions:
        return False
    old_by = {name: (dtype, values) for name, dtype, values in old_sections}
    new_by = {name: (dtype, values) for name, dtype, values in new_sections}
    if set(old_by) != set(new_by):
        return False
    floating = {
        descriptor["section"]
        for descriptor in new_attributes
        if attributes_mod.wire_dtype(descriptor.get("type") or "FLOAT") == "f32"
    }
    for name, (dtype, values) in new_by.items():
        old_dtype, old_values = old_by[name]
        if dtype != old_dtype:
            return False
        if name in floating:
            if not _close_enough(old_values, values):
                return False
        elif codec.section_bytes(name, dtype, values) != codec.section_bytes(name, dtype, old_values):
            return False
    return True


def _close_enough(old_values, new_values):
    """Element-wise float comparison with the tolerance attribute noise needs."""
    old = np.asarray(old_values, dtype=np.float32).reshape(-1)
    new = np.asarray(new_values, dtype=np.float32).reshape(-1)
    if old.shape != new.shape:
        return False
    return bool(np.allclose(old, new, rtol=1e-5, atol=1e-9, equal_nan=True))


def _build_prefix_batch(obj, record, stage=None):
    """Evaluate stage prefixes in a single depsgraph update.

    Linking a clone and asking for `evaluated_depsgraph_get()` costs a full
    depsgraph update, ~17 ms on the FiraTower scene, and doing that once per
    stage made a three-stage object cost ~50 ms before any geometry was read.
    The clones are independent objects with different modifier subsets, so they
    can coexist for one update; their evaluated sections are identical to the
    per-stage reads (checked against the old hashes).

    The first build of a pass evaluates every stage. After an apply, only the
    prefix the pass is about to read can still be valid: `_invalidate_prefix_batch`
    removed every downstream entry, and pre-evaluating the rest would only be
    thrown away by the next apply. `stage` names the stage asking now, so those
    rebuilds evaluate one prefix instead of the whole stack.
    """
    if obj.type == "SURFACE" or len(record.stages) < 2:
        ENGINE.prefix_batch_built = True
        return
    rebuild_one = stage is not None and ENGINE.prefix_batch_built
    stages = []
    for candidate in record.stages:
        # A generator has no Stage In: there is no prefix to evaluate, and its
        # request never reads one. Cloning it would only cost a depsgraph update.
        if stack_mod.is_generator(candidate):
            continue
        if rebuild_one and candidate.key != stage.key:
            continue
        modifier = nodes.modifier_of(obj, candidate)
        if modifier is None and not candidate.modifier_uid:
            modifier = nodes.modifier_by_name(obj, candidate)
        stages.append((candidate, _modifier_index(obj, modifier)))
    if not stages:
        # Every stage in this rebuild is a generator: the pass has nothing to
        # evaluate, and the empty batch stays empty.
        ENGINE.prefix_batch_built = True
        return
    results = _clone_prefixes(obj, stages, bool(record.subd))
    pointer = obj.as_pointer()
    ENGINE.prefix_batch.update(
        {(pointer, key): value for key, value in results.items()}
    )
    ENGINE.prefix_batch_built = True
    ENGINE.stats()["prefix_clones"] += len(results)


def _clone_prefixes(obj, stages, subd):
    """One guarded depsgraph update for several prefix clones.

    Cleanup deliberately skips the per-clone `view_layer.update()`: each one
    forces another full evaluation, and the objects are removed from the data
    before the next pass reads anything. The next `evaluated_depsgraph_get()`
    sees the removals.
    """
    clones = []
    results = {}
    cleanup_failure = None
    primary_failed = False
    ENGINE.guard_owner = (obj.name, "prefix batch")
    ENGINE.evaluating_since = time.monotonic()
    ENGINE.evaluating = True
    try:
        # The clones live in, and are evaluated by, the object's own scene.
        # Evaluating them in the active scene read its frame for a stage that
        # belongs to another one.
        with _owned_scene(obj) as scene:
            target = scene.collection if scene is not None else bpy.context.scene.collection
            try:
                for stage, index in stages:
                    clone = obj.copy()
                    owned_data = obj.data.copy() if obj.mode == "EDIT" else None
                    if owned_data is not None:
                        clone.data = owned_data
                    # Register before linking: a link or modifier-removal failure must
                    # still remove the clone in the finally block.
                    clones.append((stage, clone, owned_data))
                    target.objects.link(clone)
                    for later in list(clone.modifiers)[index:]:
                        clone.modifiers.remove(later)
                depsgraph = bpy.context.evaluated_depsgraph_get()
                for stage, clone, _owned_data in clones:
                    geometry = clone.evaluated_get(depsgraph).evaluated_geometry()
                    notes = []
                    sections, attributes, definitions = (
                        geometry_io.geometry_set_to_sections(
                            geometry, as_subd=subd, notes=notes,
                            blocks_2=_peer_blocks_2(),
                        )
                    )
                    results[stage.key] = (sections, attributes, definitions, notes)
            finally:
                primary_failed = sys.exc_info()[0] is not None
                try:
                    for stage, clone, owned_data in clones:
                        failure = _cleanup_prefix_clone(
                            clone, owned_data, stage.key, update_view_layer=False
                        )
                        if cleanup_failure is None:
                            cleanup_failure = failure
                    try:
                        # Flush the removals while the guard is still up. Without this
                        # the depsgraph delivers the clone link/remove tags after the
                        # guard drops, marks every stack dirty, and the next pass builds
                        # another batch: a live-session feedback loop.
                        bpy.context.view_layer.update()
                    except Exception as exc:
                        if cleanup_failure is None:
                            cleanup_failure = exc
                        message = "prefix cleanup failed (update view layer): %r" % (exc,)
                        bridge.state["error"] = message
                        if not ENGINE.trace_lines or ENGINE.trace_lines[-1] != message:
                            ENGINE.trace(message)
                        import traceback

                        prefs.log(
                            "[stageloom] %s\n%s" % (message, traceback.format_exc()),
                            flush=True,
                        )
                finally:
                    ENGINE.evaluating = False
    finally:
        ENGINE.evaluating = False
    if cleanup_failure is not None and not primary_failed:
        raise cleanup_failure
    return results


def _clone_prefix(obj, index, subd, stage_key=None, notes=None):
    """The evaluated prefix of one stage: the object just before its modifier.

    The copy is linked, evaluated and removed inside the ``_evaluating`` guard:
    link/unlink tags the stack object like a real edit, and left unmuted it
    would retire the memo the engine is about to write, every pass. Because the
    guard drops everything, the timer drains the depsgraph first. In Edit Mode
    the clone gets a private copy of the flushed datablock — a shared copy
    evaluates empty. The cleanup resets the guard even if a step fails; the
    owner is recorded for the watchdog. ``notes`` collects outbound conversion
    losses without changing the returned section tuple.
    """
    clone = obj.copy()
    # A copy that shares an Edit Mode mesh evaluates empty: the depsgraph hands
    # the edit data to the object in Edit Mode and to nothing else. A private
    # copy of the (already flushed) datablock evaluates like any other object,
    # which is what keeps the prefix readable while the mode is open. The copy
    # is owned here and removed with the clone.
    owned_data = None
    if obj.mode == "EDIT":
        owned_data = obj.data.copy()
        clone.data = owned_data
    ENGINE.guard_owner = (obj.name, stage_key)
    ENGINE.evaluating_since = time.monotonic()
    ENGINE.evaluating = True
    sections = attributes = definitions = None
    try:
        # Link and evaluate in the object's own scene, not whichever scene is
        # active. The latter is the multi-scene frame bug: the clone was read
        # with another scene's playhead.
        with _owned_scene(obj) as scene:
            target = scene.collection if scene is not None else bpy.context.scene.collection
            try:
                target.objects.link(clone)
                for later in list(clone.modifiers)[index:]:
                    clone.modifiers.remove(later)
                depsgraph = bpy.context.evaluated_depsgraph_get()
                geometry = clone.evaluated_get(depsgraph).evaluated_geometry()
                sections, attributes, definitions = geometry_io.geometry_set_to_sections(
                    geometry, as_subd=subd, notes=notes, blocks_2=_peer_blocks_2()
                )
            finally:
                # The primary failure, if any, is captured before cleanup runs: a
                # cleanup failure must not mask it, and the guard reset must happen
                # either way. The reset is the inner `finally` of the two, so it runs
                # even if `_cleanup_prefix_clone` itself surprises us.
                primary_failed = sys.exc_info()[0] is not None
                try:
                    cleanup_failure = _cleanup_prefix_clone(clone, owned_data, stage_key)
                finally:
                    ENGINE.evaluating = False
                if cleanup_failure is not None and not primary_failed:
                    raise cleanup_failure
    finally:
        ENGINE.evaluating = False
    return sections, attributes, definitions


def _cleanup_prefix_clone(clone, owned_data, stage_key, update_view_layer=True):
    """Undo one prefix clone, one guarded step at a time.

    Removing the clone, dropping its private datablock and updating the view
    layer can each raise. One failing step must not skip the others, and must
    not skip the guard reset — which is why the caller owns that reset. The
    first failure is returned so the pass can report it. A batch skips the
    per-clone view-layer update; it runs one depsgraph evaluation for the whole
    batch and removes every clone in the same guarded region.
    """
    steps = (
        ("remove clone", lambda: bpy.data.objects.remove(clone, do_unlink=True)),
        (
            "drop private data",
            (lambda: _drop_owned_data(owned_data)) if owned_data is not None else None,
        ),
        (
            "update view layer",
            (lambda: bpy.context.view_layer.update())
            if update_view_layer
            else None,
        ),
    )
    failure = None
    for name, step in steps:
        if step is None:
            continue
        try:
            step()
        except Exception as exc:
            if failure is None:
                failure = exc
            message = "prefix cleanup failed (%s): %r" % (name, exc)
            bridge.state["error"] = message
            if not ENGINE.trace_lines or ENGINE.trace_lines[-1] != message:
                ENGINE.trace(message)
            import traceback

            prefs.log("[stageloom] %s\n%s" % (message, traceback.format_exc()), flush=True)
    return failure


def _drop_owned_data(id_block):
    """Remove the private datablock `_clone_prefix` made for an Edit Mode read.

    The `bl_rna` read is inside the guard on purpose: Blender frees the copy
    along with some object removals, and reading `bl_rna` on a freed ID raises
    `ReferenceError`. Outside the try that raise escaped into the clone's
    cleanup, which is how a best-effort delete could leave the prefix guard up.
    """
    try:
        collection = getattr(
            bpy.data, _OWNED_COLLECTIONS.get(id_block.bl_rna.identifier, ""), None
        )
        if collection is not None:
            collection.remove(id_block)
    except (ReferenceError, RuntimeError):
        pass


def _sections_hash(sections, attributes=None, definitions=None):
    """The request's geometry hash. Attribute *descriptors* are hashed as one
    extra entry, and the instance *definitions* table as another: the section
    names are index-based, so without them a renamed attribute or definition
    would produce the same hash and the same request id, and the stage would
    silently keep the old name."""
    raw = {name: codec.section_bytes(name, dtype, values) for name, dtype, values in sections}
    if attributes:
        raw["attr.descriptors"] = codec.canonical_json(attributes).encode("utf-8")
    if definitions:
        raw["definitions"] = codec.canonical_json(definitions).encode("utf-8")
    return hashing.geometry_hash(raw)


def _prefix_revision(obj, record):
    """What the memo compares: the depsgraph generation, the SubD cage flag and
    the frame.

    The generation moves on every update Blender reports and on every engine
    write a later stage must see; a memo is valid only while nothing has been
    tagged since it was read. One counter for the whole graph, deliberately:
    Python cannot ask a node tree what it depends on, and a hand-maintained
    list of references is silent stale geometry when it misses one. The frame
    (a frame change tags nothing) and the SubD cage flag are read directly.
    """
    return (ENGINE.prefix_epoch, bool(record.subd), helpers.frame_of(obj))


def _bump_epochs(depsgraph=None):
    """Move the generation for everything Blender tagged in this evaluation.

    Called with the depsgraph from `_on_depsgraph`, and with None from
    `_bump_id` for engine writes a later stage in the same pass must see. Every
    entry counts, including all-false ones (node tree edits, driver-driven ID
    properties): the flags say what kind of update it was, not whether an input
    moved. The engine's own clone events are excluded by the guard alone.
    Returns the update count for the heartbeat.
    """
    ENGINE.prefix_epoch += 1
    if depsgraph is None:
        return 0
    try:
        updates = list(depsgraph.updates)
    except (AttributeError, RuntimeError, TypeError):
        return 0
    if not updates:
        return 0
    names = []
    for update in updates[:3]:
        ident = getattr(update.id.bl_rna, "identifier", None) or "?"
        names.append("%s:%s" % (ident, update.id.name))
    line = "depsgraph %d update(s): %s" % (len(updates), ", ".join(names))
    # One rolling line, not one per event: a drag would flush everything else
    # out of the ring.
    if ENGINE.trace_lines and ENGINE.trace_lines[-1].startswith("depsgraph"):
        ENGINE.trace_lines[-1] = line
    else:
        ENGINE.trace(line)
    return len(updates)


def _bump_id(id_block):
    """Move the generation for the engine's own writes.

    The depsgraph has not seen a write the engine just made when the next stage
    in the same pass looks at its prefix, so the write is stamped here rather
    than waited for. The generation is global, so one bump covers every stage
    downstream of the written datablock.
    """
    _bump_epochs(None)


def _modifier_index(obj, modifier):
    if modifier is None:
        return 0
    for i, candidate in enumerate(obj.modifiers):
        if candidate == modifier:
            return i
    return 0


def _timeout():
    return float(prefs._pref("timeout", 30.0))

