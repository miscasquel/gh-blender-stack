"""The engine: one tick per timer pass, per stack object (plan §9, §7 frame cache).

One pass per stage: compute the prefix geometry, hash it, build the request id,
then serve the frame from the memory cache, the disk store, or Grasshopper. The
response store *is* the frame cache: keyframed parameters produce a different rid
per frame, so frames cached under their rids replay in any order, with no bake
operator and no frame range (the handoff, 2026-10-12).

A per-stage generation resets the cache when Grasshopper sends something that
makes old frames describe the wrong definition: a changed schema, a response that
contradicts the stored one for the same rid, a reconnect with a different
document, or a manual Push.

Cached frames replay with Grasshopper closed (renders use the cache). A frame
that is not cached keeps the last good output; a pass that changes nothing writes
nothing and tags nothing, so Cycles can accumulate samples while idle.

Nothing here touches the socket directly: it posts frames through the bridge and
drains what the server thread decoded.
"""

import sys
import time
from collections import deque

import bpy
import numpy as np

from ..core import attributes as attributes_mod
from ..core import cache as frame_cache
from ..core import codec, hashing
from . import bridge, geometry_io, helpers, nodes, prefs, props, server, stack as stack_mod

WAIT_INTERVAL = 0.005
# The idle cadence: what a quiet session pays to notice an incoming message
# (a GH-side edit, a reconnect). 20 ms keeps those reactive without a tick
# budget worth measuring; the fast cadence below is what edits use.
IDLE_INTERVAL = 0.02
# While the scene is being edited the fast cadence applies instead of the idle
# one: an edit keeps the engine hot for HOT_SECONDS, so a reaction costs one tick
# rather than up to one idle interval. Playback fills frames this way — each
# answer re-dirties the stack and the next frame goes out at once — and a quiet
# scene costs nothing, because `_dirty` is emptied on every pass.
HOT_INTERVAL = 0.005
HOT_SECONDS = 1.0
# A request that is still outstanding after this long may be superseded when the
# scene has moved on (the pass asks with the current state). Grasshopper
# coalesces solves, so the newer ask does not build a backlog, and the answer
# arrives for the value the user is looking at instead of one round trip behind
# it. The hard `_timeout()` still covers an answer that never comes.
SOFT_RETRY_SECONDS = 1.0

# How long a stage may wait for its outstanding answer before the request is given
# up on and retried in full. See `_timeout()`.

_cache = {}  # (stack_id, stage_key) -> {rid: (header, sections)}; the hot path
_stores = {}  # (stack_id, stage_key) -> ResponseStore; the backing store
_prefix_memo = {}  # (obj.as_pointer(), stage key) -> {"revision": tuple, "sections": list}
# (stack_id, stage_key) pairs allowed exactly one request while frozen: Pull.
# Transient session state, like `_pending` — never saved, cleared on register.
_pull_permits = set()
# The depsgraph generation: one counter, moved by every update Blender reports
# and by every engine write. A memoized prefix is only valid within the
# generation that produced it, so anything the depsgraph tags retires every
# memo. This is deliberately *one* counter and not a per-reference list: the
# Python API has no way to ask a node tree which datablocks it depends on, so a
# list of "what a prefix reads" is a hand-maintained description of the
# depsgraph, and a missing entry is silent stale geometry. Tagged is tagged.
_prefix_epoch = 0
# (stack_id, stage_key) pairs that must ask Grasshopper even when a cached frame
# answers the request id: a reconnect or a file load means the definition may
# have changed while the add-on was away, and the frame cache cannot know it.
# Consumed when the verification request is actually sent.
_verify = set()
# The last events the engine acted on, for the Diagnostics panel: a bounded ring
# of "what did it see, what did it do" lines. Never saved.
_trace = deque(maxlen=200)
# Trigger health, for the Diagnostics panel: when the depsgraph last reached
# `_on_depsgraph` (and how many entries that delivery carried), when a pass last
# sent a request, and when a response was last applied. Monotonic and
# session-only: the panel renders the ages ("trigger 0.4 s ago"), which is the
# one line that tells a user whether the engine heard Blender at all.
_heartbeat = {
    "trigger_at": None,
    "trigger_count": 0,
    "trigger_updates": 0,
    "sent_at": None,
    "applied_at": None,
}
# Why a stage's last pass did not send: (stack_id, stage_key) -> (reason, rid).
# A reason is re-recorded only when it changes or when the scene state moves on,
# so a pass that stays quiet for the same reason does not flood the ring.
_last_no_send = {}
# The last pass latencies per stage, in milliseconds:
# (stack_id, stage_key) -> {"prefix_ms", "encode_ms", "rtt_ms", "apply_ms"}.
# Diagnostics-only, deliberately not on the saveable stage record: one
# machine's timings must not travel inside a .blend.
_timings = {}
# (stack_id, stage_key) -> {rid: monotonic seconds it was sent}. At most one entry:
# the evaluation in flight. One at a time, deliberately — a frame that arrives while
# an answer is outstanding is not queued behind it, because Grasshopper solves in
# arrival order on one thread and a queue of frames the user has already scrubbed
# past only keeps the client busy after they have moved on. The frame the playhead
# is on is asked for on the first pass after the answer lands, and every answer is
# stored either way, so nothing evaluated is wasted.
_pending = {}
# (stack_id, stage_key) -> (rid, geometry hash) of the last request actually
# handed to a connected client. The stage record is saved *and undoable*: an
# undo restores `geom_hash`/`rid` from the undo step, which says nothing about
# what the client currently holds. This map is connection state, never saved:
# the cache may answer a request without asking only while the client has been
# told that exact request (see `_process_stage`), and the request mode is
# decided against the geometry named here, not against a rolled-back field.
_sent = {}
_dirty = set()  # object names that need a pass
# Object names with a request in flight, for the Diagnostics summary. Refreshed
# from `_pending` by the once-a-second stall scan; the scan itself walks the
# scene rather than trusting this set, because a rename between send and check
# would leave the old name in here (see `_nudge_stalled_waits`).
_waiting_objects = set()
_last_stall_check = 0.0
_registered = False
# Monotonic deadline until which the engine runs at the fast cadence; see
# HOT_INTERVAL. Advanced by every edit, response and frame change.
_hot_until = 0.0
# When each stage last asked Grasshopper something, so a wait that is really a
# fault is visible in the diagnostics panel: (stack_id, stage_key) -> monotonic.
_state_since = {}
# True while the engine evaluates a prefix clone. Linking and removing that
# clone tags the depsgraph; without the guard every pass would re-mark every
# stack dirty and the engine would never idle.
_evaluating = False
# Who set the guard, and when: written once per clone, so the watchdog at the
# top of `_timer` can name the object and stage that left it up without
# formatting a traceback per clone. `_ticking` tells the depsgraph handler
# whether the guard it sees belongs to the engine's own clone (a pass is
# running, the event is ours and expected to be eaten) or to a pass that died
# before it could clear it (the event is a real edit being dropped).
_guard_owner = None
_evaluating_since = None
_ticking = False
# Counters for tests and diagnostics: what the engine actually did, so a quiet
# idle can be asserted without guessing at Blender internals.
stats = {
    "passes": 0,
    "prefix_clones": 0,  # prefix evaluations on a clone (every non-surface read)
    "prefix_memo_hits": 0,  # prefix evaluations served from the generation memo
    "applies": 0,  # responses written into helper objects
    "tags": 0,  # obj.update_tag() calls
    "sends": 0,  # requests actually sent
    "timeouts": 0,  # requests given up on after the timeout and retried in full
    "undelivered": 0,  # requests not even sent: no client socket to carry them
}


def _pending_of(stack_id, stage_key):
    """The frame this stage is waiting on: ``{rid: monotonic}``, at most one entry."""
    return _pending.setdefault((stack_id, stage_key), {})


def _forget(stack_id, stage_key):
    """Stop waiting for a stage's active frame."""
    _pending.pop((stack_id, stage_key), None)



# --------------------------------------------------------------------- triggers


def _mark_dirty_all():
    for obj in bpy.data.objects:
        if stack_mod.stack_of(obj) is not None and len(obj.gh_stack.stages):
            _dirty.add(obj.name)


def _mark_hot():
    """Run the fast cadence for another HOT_SECONDS (see HOT_INTERVAL)."""
    global _hot_until
    _hot_until = time.monotonic() + HOT_SECONDS


@bpy.app.handlers.persistent
def _on_depsgraph(scene, depsgraph=None):
    if _evaluating:
        _trace_guard_drop()
        return
    _heartbeat["trigger_at"] = time.monotonic()
    _heartbeat["trigger_count"] += 1
    _heartbeat["trigger_updates"] = _bump_epochs(depsgraph)
    _mark_dirty_all()
    _mark_hot()


def _trace_guard_drop():
    """Note a depsgraph update the guard swallowed, but only when it is a symptom.

    Guard events during a pass are the engine's own clone; an update with no
    pass running means the guard was left up by a pass that died — the mute the
    watchdog exists for — and only that one is named.
    """
    if _ticking:
        return
    where = "%s (stage %s)" % _guard_owner if _guard_owner else "unknown"
    line = "depsgraph update dropped: guard up by %s" % where
    if not _trace or _trace[-1] != line:
        _trace.append(line)


def _drain_depsgraph():
    """Force any evaluation Blender still owes *before* this pass can clone.

    The clone mutes the depsgraph handler while it evaluates, so an update
    delivered there would be dropped with nothing left to mark the stack dirty
    — the "random, needs Push" failure. Draining first delivers every pending
    update while the guard is down; it is free when nothing is tagged.
    """
    if _evaluating:
        return
    try:
        bpy.context.evaluated_depsgraph_get().update()
    except (AttributeError, RuntimeError):
        pass


def _edit_mode_objects():
    """Every object currently in Edit Mode, active or not.

    `bpy.context.edit_object` names only the *active* one; multi-object editing
    gives every selected object its own BMesh edits. The context list is walked
    when present, the scene otherwise; filtering on the object's own mode keeps
    sculpt and pose sessions out.
    """
    try:
        candidates = list(bpy.context.objects_in_mode)
    except AttributeError:
        candidates = list(bpy.data.objects)
    return [obj for obj in candidates if obj.mode == "EDIT"]


def _flush_object(obj):
    """Copy one Edit Mode object's edits into its datablock.

    Edit data lives in the BMesh, not in `obj.data`, until Blender flushes it:
    the one script-facing way is `update_from_editmode()`. Without the flush the
    prefix clone copies the pre-edit cage and the unchanged hash replays the
    cached frame. A no-op outside Edit Mode, so Push/Pull can call it directly;
    a RuntimeError leaves the datablock as it was.
    """
    if obj is None or obj.mode != "EDIT":
        return False
    try:
        obj.update_from_editmode()
    except RuntimeError:
        return False
    _trace.append("flushed Edit Mode data for %s" % obj.name)
    return True


def _flush_edit_mode():
    """Flush every dirty stack object that is in Edit Mode, not only the active.

    A referenced object needs no flush (its edits reach the evaluated prefix
    through the graph), and the dirty gate keeps an idle session from copying
    meshes for nothing. Iterating every edit object is what makes a stroke on a
    non-active object — or behind a non-stack active object — reach Grasshopper
    while the mode is open.
    """
    for obj in _edit_mode_objects():
        if stack_mod.stack_of(obj) is None:
            continue
        if obj.name not in _dirty:
            continue
        _flush_object(obj)


@bpy.app.handlers.persistent
def _on_frame_change(scene, depsgraph=None):
    # A frame change tags nothing, which is why the frame is part of the prefix
    # revision and this handler exists separately from the depsgraph one.
    _mark_dirty_all()
    _mark_hot()


def _after_history_step():
    """Re-assert the client-side state after an undo or redo.

    The stage record is part of the .blend, so undo/redo restores its saved
    `geom_hash`, `rid` and `applied_out_hash` to the values captured in the
    step. What the connected client holds did not travel with that step: after
    undoing a move, the client still has the moved geometry. `_sent` is the
    session truth, so the saved geometry field is put back from it (or cleared
    when nothing was sent, which asks in full) and the next pass decides
    full/ref correctly instead of sending a ref the client must refuse.

    Marking every stack dirty also gives the step a pass even when its
    depsgraph delivery is missed; the memo is cleared for the same reason — an
    undo restores datablocks the memo may name.
    """
    for obj in bpy.data.objects:
        record = stack_mod.stack_of(obj)
        if record is None or not record.stages:
            continue
        if not record.stack_id:
            record.stack_id = stack_mod.ensure_stack(obj).stack_id
        for stage in record.stages:
            sent = _sent.get((record.stack_id, stage.key))
            _set(stage, "geom_hash", sent[1] if sent is not None else "")
    _prefix_memo.clear()
    _mark_dirty_all()
    _mark_hot()


@bpy.app.handlers.persistent
def _on_undo(scene=None):
    _after_history_step()


@bpy.app.handlers.persistent
def _on_redo(scene=None):
    _after_history_step()


@bpy.app.handlers.persistent
def _on_load(scene=None):
    """A file was opened (or reverted, or Blender started with one).

    Blender clears non-persistent handlers and timers on every file load, so
    these hooks are registered as persistent: without that the engine goes
    silent on reopen and never talks to Grasshopper again. A freshly loaded
    file must behave like a reconnect: an in-flight request belonged to the old
    session, a stage saved mid-request must not wait for it, and every stack is
    evaluated against the cache and Grasshopper (plan §9.7).

    A load is also a *new session from Grasshopper's point of view*: the client
    may be a fresh process or a different document, so the geometry hash — a
    claim about what GH still holds — does not survive it. Clearing it makes the
    next request go out with `geom.mode = "full"`, which is what protocol §8.8
    already prescribes for the first request after a connection.
    """
    global _evaluating, _guard_owner, _evaluating_since, _ticking
    if not bpy.app.timers.is_registered(_timer):
        bpy.app.timers.register(_timer, first_interval=IDLE_INTERVAL, persistent=True)
    # A file from before the stage scene still has its plumbing in the user's
    # scene: move it now, so the outliner is clean before the first apply.
    helpers.adopt_existing()
    _pending.clear()
    _sent.clear()
    _prefix_memo.clear()
    _state_since.clear()
    _last_no_send.clear()
    _timings.clear()
    # The guard belongs to the session that just ended. A pass can only be
    # running on the main thread, but a load (a new file, a revert) must not
    # inherit the previous file's wedge: with `_evaluating` set the depsgraph
    # handler would drop every edit in the new file until the watchdog fired.
    _evaluating = False
    _guard_owner = None
    _evaluating_since = None
    _ticking = False
    for obj in bpy.data.objects:
        record = stack_mod.stack_of(obj)
        if record is None:
            continue
        for stage in record.stages:
            if stage.state == stack_mod.STATE_WAITING:
                _set(stage, "state", stack_mod.STATE_IDLE)
            if stage.geom_hash:
                # We cannot know that GH kept the geometry: ask again in full.
                stage.geom_hash = ""
            # A saved file may meet a document that changed in between: the
            # cache must not answer without asking once (see _verify).
            if not record.stack_id:
                record.stack_id = stack_mod.ensure_stack(obj).stack_id
            _verify.add((record.stack_id, stage.key))
            # Plumbing lost with the file (helpers or the node group deleted
            # before the save) is rebuilt here and the stage marked unbound
            # until an answer arrives (plan §9.7). A missing *modifier* is not
            # repaired: deleting it is how a stage is removed (§9.8).
            modifier = nodes.modifier_of(obj, stage)
            if modifier is not None:
                _repair_plumbing(obj, record, stage, modifier)
    _mark_dirty_all()
    _mark_hot()


# ------------------------------------------------------------------------ tick


def _recover_stuck_guard():
    """Clear `_evaluating` when a previous pass left it set, and say so.

    A tick that starts with the guard up cannot belong to that pass: the owner
    has ended. While it is set every update is dropped and nothing marks a
    stack dirty again, so the tick says who left it, clears it, and marks every
    stack dirty to pick up the edits missed meanwhile.
    """
    global _evaluating
    age = time.monotonic() - _evaluating_since if _evaluating_since is not None else 0.0
    where = "%s (stage %s)" % _guard_owner if _guard_owner else "unknown"
    _evaluating = False
    # Every depsgraph update the guard swallowed was dropped *before* it could
    # move the generation, so a memo written before the leak still looks
    # current even though the scene moved. Clearing them is what makes the
    # "mark dirty" below read the edited scene instead of replaying the stale
    # prefix (and the old rid) one more time.
    _prefix_memo.clear()
    _mark_dirty_all()
    line = (
        "guard watchdog: prefix guard left up by %s %.2f s ago; cleared and all stacks marked dirty"
        % (where, age)
    )
    _trace.append(line)
    prefs.log("[gh-stack] " + line, flush=True)


def _timer():
    """bpy.app.timers callback. Returns the next interval.

    A timer that raises is unregistered by Blender, which would silently stop the
    engine; every part of the pass is therefore guarded, and failures land in the
    panel state instead.
    """
    global _ticking
    try:
        stats["passes"] += 1
        _ticking = True
        # Before anything else: a guard already up at the start of a tick cannot
        # belong to this pass, so it is the wedge the watchdog exists for.
        if _evaluating:
            _recover_stuck_guard()
        # Before anything in this pass can evaluate a clone (and mute the
        # handler), let Blender deliver whatever is already tagged. Every
        # trigger the add-on has funnels through here, which is what makes the
        # depsgraph authoritative rather than best-effort.
        _drain_depsgraph()
        _flush_edit_mode()
        _handle_incoming()
        _nudge_stalled_waits()
        busy = False
        for name in list(_dirty):
            obj = bpy.data.objects.get(name)
            if obj is None:
                _dirty.discard(name)
                continue
            try:
                if _process_object(obj):
                    busy = True
            except Exception as exc:
                bridge.state["error"] = "engine error on %s: %r" % (name, exc)
                import traceback

                prefs.log("[gh-stack] engine error:\n%s" % traceback.format_exc(), flush=True)
                # The pass died before it could decide anything for this
                # object: say so in the panel, where the silence is what a user
                # is staring at. One line per consecutive identical failure.
                line = "engine error on %s: %r" % (name, exc)
                if not _trace or _trace[-1] != line:
                    _trace.append(line)
            _dirty.discard(name)
        if busy:
            return WAIT_INTERVAL
        if time.monotonic() < _hot_until:
            return HOT_INTERVAL
        return IDLE_INTERVAL
    except Exception as exc:
        # The last-resort handler must not raise itself: Blender unregisters a
        # timer whose callback raises, and then the engine is gone for the rest
        # of the session. `prefs.log` is already print-safe; this guard also
        # covers the state write and anything a future edit adds here.
        try:
            bridge.state["error"] = "engine error: %r" % (exc,)
            import traceback

            prefs.log("[gh-stack] engine error:\n%s" % traceback.format_exc(), flush=True)
        except Exception:
            pass
        return IDLE_INTERVAL
    finally:
        _ticking = False


def _nudge_stalled_waits():
    """Mark a stack object dirty when a stage has waited past the soft retry.

    A waiting stage dirties nothing on its own, and the pass is what supersedes
    a stale request with the current state (or resends at the hard timeout);
    without the nudge a supersede would wait for an unrelated edit. The scan
    walks the scene's stack objects — not names captured at send time, which a
    rename invalidates — and runs at most once a second.
    """
    global _last_stall_check
    now = time.monotonic()
    if now - _last_stall_check < 1.0:
        return
    _last_stall_check = now
    waiting = set()
    for obj in bpy.data.objects:
        record = stack_mod.stack_of(obj)
        if record is None or not record.stages:
            continue
        for stage in record.stages:
            pending = _pending.get((record.stack_id, stage.key))
            if not pending:
                continue
            waiting.add(obj.name)
            if now - min(pending.values()) > SOFT_RETRY_SECONDS:
                _dirty.add(obj.name)
                _mark_hot()
    _waiting_objects.clear()
    _waiting_objects.update(waiting)


def _handle_incoming():
    """Apply every queued message, isolating one bad frame from the rest.

    `drain_incoming()` empties the queue into a list, so without a per-message
    guard a raise in one handler aborted the pass and every message behind it
    was lost.
    """
    for header, sections in bridge.drain_incoming():
        try:
            _handle_message(header, sections)
        except Exception as exc:
            try:
                _report_message_failure(header, exc)
            except Exception:
                # The reporter is the last thing between one bad message and
                # the next one: if it fails too, the rest of the batch still
                # matters more than the report.
                _trace.append("incoming message failed and could not be reported")


def _handle_message(header, sections):
    mtype = header.get("type")
    if mtype == "schema":
        _handle_schema(header)
    elif mtype == "response":
        _handle_response(header, sections)
    elif mtype == "error":
        _handle_error(header)
    elif mtype == "hello":
        _on_client_hello(header)


def _report_message_failure(header, exc):
    """Record one failed incoming message and recover what it left behind.

    The panel, the trace and the console get the failure; for a response the
    stage is put back in a retryable state through `_clear_failed_response`, so
    one bad frame costs that frame instead of the messages behind it.
    """
    mtype = header.get("type") or "?"
    stage_key = header.get("stage")
    where = "%s %s" % (mtype, stage_key or "?")
    message = "incoming %s failed: %r" % (where, exc)
    bridge.state["error"] = message
    if not _trace or _trace[-1] != message:
        _trace.append(message)
    import traceback

    prefs.log("[gh-stack] incoming %s failed:\n%s" % (where, traceback.format_exc()), flush=True)
    if mtype == "response" and stage_key:
        try:
            _clear_failed_response(stage_key, header.get("rid"))
        except Exception as recovery:
            # Best-effort, like the cleanup: a stage that cannot be recovered
            # must not cost the *other* messages their chance to be handled.
            prefs.log(
                "[gh-stack] could not recover the failed response for %s: %r"
                % (stage_key, recovery),
                flush=True,
            )


def _clear_failed_response(stage_key, rid):
    """Put the stage of a failed response back in a retryable state.

    The frame is removed first (see `_forget_frame`): a frame whose apply raised
    would answer the next pass and raise again. Pending is cleared (idempotent)
    and the state leaves `waiting`, where an empty pending map would look like
    an answer still on its way.
    """
    for obj in bpy.data.objects:
        record = stack_mod.stack_of(obj)
        if record is None or not record.stages:
            continue
        stage = stack_mod.stage_of(obj, stage_key)
        if stage is None:
            continue
        _forget(record.stack_id, stage.key)
        if rid:
            _forget_frame(record, stage, rid)
        _set(
            stage,
            "state",
            stack_mod.STATE_FROZEN if stage.frozen else stack_mod.STATE_IDLE,
        )
        _dirty.add(obj.name)
        _mark_hot()


def _forget_frame(record, stage, rid):
    """Drop one stored frame — hot cache and disk — so it cannot be replayed.

    `_handle_response` stores the payload before it applies it, exactly so a
    stale rid's answer is still cached. When the apply fails, that order means
    the frame is a trap: the next pass for the same rid finds it, applies it and
    raises again, forever. Removing it turns the retry into a real request,
    which is the only thing that can heal a transient apply fault.
    """
    cache = _cache.get((record.stack_id, stage.key))
    if cache is not None:
        cache.pop(rid, None)
    try:
        _store_for(record, stage).remove(rid)
    except OSError as exc:
        prefs.log(
            "[gh-stack] could not drop a failed frame for %s: %r" % (stage.key, exc),
            flush=True,
        )


def _on_client_hello(header):
    """A GH client connected (or reconnected).

    A request sent while no client was connected is lost, and a stage left
    waiting on it would sit until its timeout. Reset waiting stages and mark
    every stack dirty so the current request goes out again (protocol §8.8).
    A reconnect with a *different* document resets the stages it claims: frames
    cached against the old document must not replay (handoff reset rules).

    Every claimed stage also forgets its geometry hash: a `hello` is a new client
    session, so the geometry it holds is unknown, and protocol §8.8 wants the
    next request in full. The stage is also put on `_verify`: a fresh connection
    is the one moment the document may have changed without Blender hearing
    about it, so the next pass must ask instead of replaying a cached frame.
    """
    doc = (header or {}).get("doc") or ""
    claimed = set()
    for entry in (header or {}).get("stages") or []:
        key = entry.get("key") if isinstance(entry, dict) else None
        if key:
            claimed.add(key)
    for obj in bpy.data.objects:
        record = stack_mod.stack_of(obj)
        if record is None or not record.stages:
            continue
        if not record.stack_id:
            record.stack_id = stack_mod.ensure_stack(obj).stack_id
        if doc and claimed and any(stage.key in claimed for stage in record.stages):
            if record.connected_doc and record.connected_doc != doc:
                for stage in record.stages:
                    if stage.key in claimed:
                        _bump_generation(obj, record, stage, "document changed")
            if record.connected_doc != doc:
                record.connected_doc = doc
        for stage in record.stages:
            if stage.state == stack_mod.STATE_WAITING:
                _forget(record.stack_id, stage.key)
                _set(stage, "state", stack_mod.STATE_IDLE)
            _set(stage, "geom_hash", "")
            _sent.pop((record.stack_id, stage.key), None)
            _verify.add((record.stack_id, stage.key))
        _dirty.add(obj.name)
    _trace.append("client hello: %s" % (doc or "unnamed"))
    _mark_hot()


def _handle_schema(header):
    key = header.get("stage")
    params = header.get("params") or []
    matched = False
    for obj in bpy.data.objects:
        record = stack_mod.stack_of(obj)
        if record is None:
            continue
        stage = stack_mod.stage_of(obj, key)
        if stage is not None:
            matched = True
            if not record.stack_id:
                record.stack_id = stack_mod.ensure_stack(obj).stack_id
            try:
                # The plugin re-publishes on a heartbeat; only touch the UI when
                # the schema actually changed.
                if props.schema_signature(stage) == props.incoming_signature(params):
                    continue
                props.apply_schema(obj, stage, params)
            except Exception as exc:
                # One bad schema must not take the engine down.
                bridge.state["error"] = "schema for %s failed: %r" % (key, exc)
                import traceback

                prefs.log("[gh-stack] schema error:\n%s" % traceback.format_exc(), flush=True)
                continue
            # A changed schema is a changed definition: frames cached against
            # the old one must not replay.
            _bump_generation(obj, record, stage, "schema changed")
            _dirty.add(obj.name)
            obj.update_tag()
    if not matched:
        # A schema for a stage no object has: usually a key pasted into GH that
        # does not belong to any stack, or a stage removed in Blender.
        prefs.log("[gh-stack] schema for unknown stage %r ignored" % (key,), flush=True)


def _handle_response(header, sections):
    key = header.get("stage")
    rid = header.get("rid")
    for obj in bpy.data.objects:
        record = stack_mod.stack_of(obj)
        if record is None or not record.stages:
            continue
        stage = stack_mod.stage_of(obj, key)
        if stage is None:
            continue
        if not record.stack_id:
            record.stack_id = stack_mod.ensure_stack(obj).stack_id

        if header.get("status") == "error":
            error = header.get("error") or {}
            code = error.get("code") if isinstance(error, dict) else None
            if code == "unknown_ref":
                # GH lost the geometry a ref named (reconnect, restarted
                # document): send it again in full (protocol §4.3). The error is
                # not a result and must not be cached.
                _resend_in_full(obj, record, stage, "unknown_ref response")
                continue
            _forget(record.stack_id, stage.key)
            _set(stage, "state", stack_mod.STATE_ERROR)
            _set(stage, "error", error.get("message") if isinstance(error, dict) else str(error))
            # Deliberately no retry here: a solve that just failed would fail
            # again the moment it was asked, turning one broken component into
            # a request loop against the client. Protocol §7: an errored stage
            # retries on the next change or Push; `_set` above has already
            # surfaced the state, and the change that fixes the graph marks the
            # object dirty like any other.
            continue

        out_hash = header.get("out_hash") or ""
        stored_out = None
        memory = _cache.get((record.stack_id, key))
        if memory is not None and rid in memory:
            stored_out = memory[rid][0].get("out_hash") or ""
        else:
            try:
                entry = _store_for(record, stage).scan().index.get(rid)
            except OSError:
                entry = None
            if entry is not None:
                stored_out = entry.out_hash or ""
        if stored_out is not None and stored_out != out_hash:
            # The same request was answered differently: Grasshopper's
            # definition changed (a slider upstream of Stage Out, a graph edit
            # that does not touch Blender's inputs). Everything cached for this
            # stage is suspect.
            _bump_generation(obj, record, stage, "Grasshopper's answer changed")
        _remember(record, stage, rid, header, sections)
        _store_frame(record, stage, rid, header, sections)

        was_pending = rid in _pending_of(record.stack_id, key)
        sent_at = _pending_of(record.stack_id, key).get(rid) if was_pending else None
        _forget(record.stack_id, stage.key)
        if sent_at is not None:
            _record_timing(record, stage, "rtt_ms", _timed_since(sent_at))
        applied = False
        if rid == stage.rid and (
            out_hash != stage.applied_out_hash or _payload_missing(obj, stage, sections)
        ):
            started = time.monotonic()
            _apply(obj, stage, sections, header.get("attributes"))
            _record_timing(record, stage, "apply_ms", _timed_since(started))
            stats["applies"] += 1
            _set(stage, "applied_out_hash", out_hash)
            applied = True
            _trace.append("%s: applied %s" % (stage.key, (out_hash or "")[-8:]))
        if rid == stage.rid and not applied:
            # The displayed answer is already this one, but the node group may
            # predate the current format (a group written by an older build, or
            # a session that has not applied since nesting landed). Rebuilding
            # is cheap and is the only way a same-hash frame can repair it.
            nodes.ensure_group(obj, stage.key, _nested_rows_of(sections))
        if not applied and not was_pending:
            # Nothing to apply and nothing was being waited for: the answer is
            # stored under its own rid, which is what makes a later pass over that
            # frame a replay rather than a request (protocol §6.4).
            #
            # If it was for a frame the playhead has moved past, the stage still
            # gets a pass: that pass is what asks for the frame the scene is on, and
            # dropping it is how a stage ended up looking stuck until a manual Push.
            # If it was for the frame already displayed, it is a duplicate and stays
            # a complete no-op, which is what keeps an idle scene quiet.
            if rid != stage.rid:
                _dirty.add(obj.name)
            continue
        _set(stage, "warnings", _warnings_text(header))
        if was_pending and not applied:
            # The active frame's answer arrived, but the playhead moved on while
            # it was in flight: keep the last good output and let the next pass
            # evaluate the frame the scene is actually on. A frozen stage stays
            # frozen — Pull asked for the answer, freeze still owns the state.
            _set(
                stage,
                "state",
                stack_mod.STATE_FROZEN if stage.frozen else stack_mod.STATE_IDLE,
            )
        else:
            _set(stage, "state", _rest_state(stage))
        _set(stage, "error", "")
        if applied:
            stats["tags"] += 1
            obj.update_tag()
        if applied or was_pending:
            _dirty.add(obj.name)
            _mark_hot()


def _handle_error(header):
    """A standalone `error` message (protocol §4.6).

    `unknown_ref` is the one that needs action: Grasshopper does not hold the
    geometry the last request named. Everything else is reported on the stages it
    names so a protocol mismatch is not silent.
    """
    code = header.get("code")
    message = header.get("message") or code or "protocol error"
    if code == "unknown_ref":
        matched = False
        for obj in bpy.data.objects:
            record = stack_mod.stack_of(obj)
            if record is None or not record.stages:
                continue
            stage = stack_mod.stage_of(obj, header.get("stage"))
            if stage is None:
                continue
            matched = True
            _resend_in_full(obj, record, stage, "unknown_ref")
        if not matched:
            prefs.log("[gh-stack] unknown_ref for unknown stage %r" % (header.get("stage"),), flush=True)
        return

    prefs.log("[gh-stack] protocol error %s: %s" % (code, message), flush=True)
    for obj in bpy.data.objects:
        record = stack_mod.stack_of(obj)
        if record is None or not record.stages:
            continue
        stage = stack_mod.stage_of(obj, header.get("stage"))
        if stage is None:
            continue
        # The request this error answers is over: without this the stage would
        # sit `waiting` for the full timeout for an answer that is already here.
        # The error is left on display and the stage is not dirtied — retrying
        # here would ask a component that just failed, in a loop; the next
        # change or Push retries (protocol §7).
        _forget(record.stack_id, stage.key)
        _set(stage, "error", "%s: %s" % (code, message))
        if stage.state == stack_mod.STATE_WAITING:
            _set(stage, "state", stack_mod.STATE_ERROR)


def _resend_in_full(obj, record, stage, reason):
    """Grasshopper no longer holds this stage's geometry, so the next request
    must carry it. Shared by the `unknown_ref` error message, the response-shaped
    `unknown_ref` and the request timeout, because all three leave the same
    uncertainty behind (protocol §4.3, §8.8)."""
    _forget(record.stack_id, stage.key)
    _sent.pop((record.stack_id, stage.key), None)
    # Bypass the frame cache once: the whole point of the resend is to ask
    # Grasshopper, and a cached frame for the same rid would answer the pass and
    # swallow it.
    _verify.add((record.stack_id, stage.key))
    _set(stage, "geom_hash", "")
    _set(stage, "state", stack_mod.STATE_IDLE)
    _set(stage, "error", "")
    _dirty.add(obj.name)
    prefs.log("[gh-stack] %s: resending geometry (%s)" % (stage.key, reason), flush=True)


def _warnings_text(header):
    """The response's warnings as one line each (protocol §4.4)."""
    lines = []
    for warning in header.get("warnings") or []:
        if isinstance(warning, dict):
            text = warning.get("message") or warning.get("code")
        else:
            text = str(warning)
        if text:
            lines.append(text)
    return "\n".join(lines)


def _sections_have_geometry(sections):
    """Whether a frame's sections name geometry a helper should hold."""
    return any(
        name.startswith(("mesh.", "subd.", "crv.", "pts.", "inst.", "idef."))
        for name in sections
    )


def _helpers_empty(obj, stage):
    """Whether every top-level helper of a stage holds nothing.

    A cheap guard against an out-of-band clear (a script, an undo): the applied
    hash still names an output that is no longer on screen, and without this
    every response and replay would be skipped — Push included.
    """
    for kind in helpers.KINDS:
        helper = helpers.helper_for(obj, stage.key, kind)
        if helper is None:
            continue
        data = helper.data
        if kind == "mesh" and len(data.vertices):
            return False
        if kind == "curves" and len(data.splines):
            return False
        if kind == "points" and len(data.points):
            return False
    return True


def _payload_missing(obj, stage, sections):
    """Whether a frame that names geometry is displayed over empty helpers.

    The one case the applied hash cannot see: the helpers were emptied while
    the hash stayed, so the skip rule would refuse every response — and Push
    too. A frame that carries geometry over empty helpers is re-applied
    instead, which is idempotent when the output really is empty.
    """
    return _sections_have_geometry(sections) and _helpers_empty(obj, stage)


def _apply(obj, stage, sections, attributes=None):
    """Write the response geometry into the stage's helpers, one per kind, then
    its attributes into the helpers that can hold them.

    A section group that is absent means "none": that helper is cleared, so a
    definition that stops returning curves stops showing them. Attributes are
    written after the geometry, because the geometry write is what fixes the
    element counts they have to match.

    Instance definitions (protocol §5.2) are the same rule one level up: each
    `idef.<i>.*` group fills definition i's prototype collection (created on
    first sight, removed when the response stops carrying it), the top-level
    `inst.*` rows fill each definition's row cloud, and a definition's own
    `idef.<i>.inst.*` rows fill its nested placement cloud, which the node
    group instances from.
    """
    base = {"mesh": [], "curves": [], "points": []}
    definitions = {}

    for name, values in sections.items():
        index, rest = _definition_split(name)
        if index is not None:
            if rest.startswith("inst."):
                continue  # handled with the other row pairs below
            bucket = _kind_of(rest)
            if bucket is not None:
                entry = definitions.setdefault(index, {"mesh": [], "curves": [], "points": []})
                entry[bucket].append((rest, values))
            continue
        if name.startswith("inst."):
            continue
        bucket = _kind_of(name)
        if bucket is not None:
            base[bucket].append((name, values))

    top_rows = _rows_of(sections, "")
    nested_rows = _nested_rows_of(sections)
    keep = set(definitions) | set(nested_rows)
    for index, _matrix in top_rows:
        keep.add(index)
    for index in sorted(nested_rows):
        for target, _matrix in nested_rows[index]:
            if target in definitions or target in nested_rows:
                keep.add(target)
            else:
                # The response names a nested placement whose definition group
                # is absent: keep it visible instead of building an empty block.
                prefs.log(
                    "[gh-stack] %s: block %d nests definition %d, which the response does not carry"
                    % (stage.key, index, target),
                    flush=True,
                )
    for index in keep:
        definitions.setdefault(index, {"mesh": [], "curves": [], "points": []})

    # Definitions the response no longer carries go away whole: collection,
    # helpers, nested empties and row cloud.
    for index in list(helpers.definition_collections(obj, stage.key)):
        if index not in keep:
            helpers.remove_definition(obj, stage.key, index)

    # Data first, wiring after: the node group reads the final definition set.
    for index in sorted(definitions):
        _collection, objects = helpers.ensure_definition(obj, stage.key, index)
        _apply_mesh(objects["mesh"], definitions[index]["mesh"])
        _apply_curves(objects["curves"], definitions[index]["curves"])
        _apply_points(objects["points"], definitions[index]["points"])

    _apply_mesh(helpers.ensure_helper(obj, stage.key, "mesh"), base["mesh"])
    _apply_curves(helpers.ensure_helper(obj, stage.key, "curves"), base["curves"])
    _apply_points(helpers.ensure_helper(obj, stage.key, "points"), base["points"])
    _apply_attributes(obj, stage, sections, attributes)
    _apply_instance_rows(obj, stage, definitions, top_rows, nested_rows)

    nodes.ensure_group(obj, stage.key, nested_rows)
    # The frame is fully on screen: the heartbeat's "applied" stamp. Written
    # here rather than at the callers so every apply path is counted once.
    _heartbeat["applied_at"] = time.monotonic()


def _definition_split(name):
    """``(index, rest)`` when a section name is inside a definition group
    (``idef.<i>.mesh.verts``), else ``(None, name)``."""
    if not name.startswith("idef."):
        return None, name
    parts = name.split(".", 2)
    if len(parts) != 3 or not parts[1].isdigit():
        return None, name
    return int(parts[1]), parts[2]


def _nested_rows_of(sections):
    """Every definition's nested placements, ``{index: [(target, matrix)]}``.

    One source of truth for the apply path and for the same-hash repair path
    (a frame already applied still has to be able to rebuild the node group)."""
    scopes = {}
    for name in sections:
        index, rest = _definition_split(name)
        if index is not None and rest.startswith("inst."):
            scopes.setdefault(index, None)
    return {index: _rows_of(sections, "idef.%d." % index) for index in scopes}


def _kind_of(name):
    if name.startswith("mesh.") or name.startswith("subd."):
        return "mesh"
    if name.startswith("crv."):
        return "curves"
    if name.startswith("pts."):
        return "points"
    return None


def _rows_of(sections, prefix):
    """The decoded row pairs of one scope as ``[(definition index, matrix 4x4)]``.

    Both halves are all-or-nothing (protocol §5.2). A missing or mismatched
    pair is reported rather than silently degraded: a truncated pair used to
    keep the shorter side and leave no trace.
    """
    refs = sections.get(prefix + "inst.ref_def")
    mats = sections.get(prefix + "inst.transforms")
    if refs is None and mats is None:
        return []
    if refs is None or mats is None or len(mats) < 16:
        prefs.log(
            "[gh-stack] %sinst.* rows are incomplete (ref_def=%s, transforms=%s); skipped"
            % (prefix, refs is not None, mats is not None),
            flush=True,
        )
        return []
    matrices = np.asarray(mats, dtype=np.float32).reshape(-1, 16)
    if len(refs) != len(matrices):
        prefs.log(
            "[gh-stack] %sinst.* row pair lengths differ (%d rows vs %d matrices); the shorter side is used"
            % (prefix, len(refs), len(matrices)),
            flush=True,
        )
    count = min(len(refs), len(matrices))
    return [(int(refs[row]), matrices[row]) for row in range(count)]


def _apply_instance_rows(obj, stage, definitions, top_rows, nested_rows):
    """The row pairs: each definition's top-level rows fill its row cloud, and
    its own rows fill its nested placement cloud (position/rotation/scale, read
    by the node group). Both are the same channel; nested clouds carry the row
    target as an INT ``child`` attribute, which the group's per-child instance
    selections compare against. Nothing bakes: the node group instances the
    child's prototype chain at these points (see nodes.ensure_group)."""
    # Legacy leftovers: earlier builds represented a nested placement as an
    # Empty with a collection instance; the node graph carries them now, so any
    # such object is removed on sight.
    for index in sorted(definitions):
        collection, _objects = helpers.ensure_definition(obj, stage.key, index)
        for child in list(collection.objects):
            if child.type == "EMPTY":
                bpy.data.collections[collection.name].objects.unlink(child)
                bpy.data.objects.remove(child, do_unlink=True)
    for index, rows in sorted(nested_rows.items()):
        if not rows:
            continue
        known = helpers.definition_collections(obj, stage.key)
        missing = [target for target, _matrix in rows if target not in known]
        if missing:
            prefs.log(
                "[gh-stack] %s: block %d nests definition %d, which the response does not carry"
                % (stage.key, index, missing[0]),
                flush=True,
            )

    # Top-level rows: one cloud per definition, or none when it has no rows.
    clouds = {}
    for target, matrix in top_rows:
        clouds.setdefault(target, []).append((target, matrix))
    for index, existing in sorted(helpers.instance_clouds(obj, stage.key).items()):
        if not clouds.get(index):
            helpers.remove_cloud(existing)
    for index, rows in sorted(clouds.items()):
        if rows:
            _write_rows_cloud(
                helpers.ensure_instance_cloud(obj, stage.key, index), rows, stage.key
            )

    # Nested placements: one cloud per definition that nests something, or
    # none when its own rows are gone.
    for index, existing in sorted(helpers.nested_clouds(obj, stage.key).items()):
        if not nested_rows.get(index):
            helpers.remove_cloud(existing)
    for index, rows in sorted(nested_rows.items()):
        if rows:
            _write_rows_cloud(
                helpers.ensure_nested_cloud(obj, stage.key, index),
                rows,
                stage.key,
                with_child=True,
            )


def _write_rows_cloud(cloud, rows, stage_key, with_child=False):
    """One row cloud's data: a point per ``(target, matrix)`` row with
    ``rotation`` / ``scale`` attributes the node group reads by name. A nested
    cloud also carries the target definition index per point, so one
    Instance-on-Points per child can select its own placements."""
    data = cloud.data
    n = len(rows)
    if len(data.points) != n:
        data.resize(n)
    positions = np.empty(n * 3, dtype=np.float32)
    rotation = data.attributes.get("rotation")
    if rotation is None:
        rotation = data.attributes.new("rotation", "QUATERNION", "POINT")
    scale = data.attributes.get("scale")
    if scale is None:
        scale = data.attributes.new("scale", "FLOAT_VECTOR", "POINT")
    child = None
    if with_child:
        child = data.attributes.get("child")
        if child is None:
            child = data.attributes.new("child", "INT", "POINT")
    warned_shear = False
    for row, (target, matrix) in enumerate(rows):
        local = helpers.matrix_of(matrix)
        position = local.to_translation()
        positions[row * 3:row * 3 + 3] = position
        quaternion = local.to_quaternion()
        scale_value = local.to_scale()
        rotation.data[row].value = quaternion
        scale.data[row].vector = scale_value
        if child is not None:
            child.data[row].value = int(target)
        if not warned_shear and not helpers.decomposes_cleanly(
            local, position, quaternion, scale_value
        ):
            # A shear (or a non-uniform basis Blender cannot rebuild from
            # position/rotation/scale) is the one shape the point-cloud
            # channel cannot carry; the transform is applied lossily.
            prefs.log(
                "[gh-stack] %s: instance transform %d has shear; "
                "Blender can only carry position, rotation and scale" % (stage_key, row),
                flush=True,
            )
            warned_shear = True
    data.points.foreach_set("co", positions)
    data.points.update()
    cloud.update_tag()


def _apply_attributes(obj, stage, sections, attributes):
    """Write the frame's attribute sections into the helper they belong to.

    mesh and subd share the mesh helper (a SubD cage is a mesh); points go to
    the point cloud helper. A descriptor with a ``definition`` index targets
    that definition's helper instead (protocol §5.2). Curve attributes are not
    transferred yet — Blender's curve helper is a legacy Curve datablock, which
    cannot store attributes.
    """
    if not attributes:
        return
    kinds = {"mesh": "mesh", "subd": "mesh", "pts": "points"}
    grouped = {}
    for descriptor in attributes:
        kind = kinds.get(descriptor.get("group"))
        if kind is None:
            continue
        values = sections.get(descriptor.get("section"))
        if values is None:
            continue
        offsets = sections.get(descriptor.get("offsets")) if descriptor.get("offsets") else None
        definition = descriptor.get("definition")
        grouped.setdefault((definition, kind), []).append((descriptor, values, offsets))

    for (definition, kind), entries in grouped.items():
        if definition is None:
            helper = helpers.ensure_helper(obj, stage.key, kind)
        else:
            _collection, objects = helpers.ensure_definition(obj, stage.key, int(definition))
            helper = objects[kind]
        _bump_id(helper)
        for note in geometry_io.write_attributes(helper.data, entries):
            prefs.log("[gh-stack] %s: %s" % (stage.key, note), flush=True)
        helper.update_tag()


def _apply_mesh(helper, entries):
    mesh = helper.data
    if not entries:
        if len(mesh.vertices) or len(mesh.polygons):
            mesh.clear_geometry()
            helper.update_tag()
        return
    _bump_id(helper)
    prepared = []
    for name, values in entries:
        arr = np.asarray(values)
        if name.endswith(".verts"):
            arr = arr.reshape(-1, 3)
        elif name.endswith(".colors"):
            arr = arr.reshape(-1, 4)
        elif name.endswith(".crease_edges"):
            arr = arr.reshape(-1, 2)
        prepared.append((name, _dtype_of(name), arr))
    geometry_io.sections_to_mesh(mesh, prepared)
    helper.update_tag()


def _apply_curves(helper, entries):
    if not entries:
        if len(helper.data.splines):
            helper.data.splines.clear()
            helper.update_tag()
        return
    _bump_id(helper)
    prepared = []
    for name, values in entries:
        arr = np.asarray(values)
        if name.endswith(".points"):
            arr = arr.reshape(-1, 4)
        prepared.append((name, _dtype_of(name), arr))
    geometry_io.sections_to_curve(helper.data, prepared)
    helper.update_tag()


def _apply_points(helper, entries):
    if not entries:
        if len(helper.data.points):
            helper.data.resize(0)
            helper.update_tag()
        return
    _bump_id(helper)
    prepared = []
    for name, values in entries:
        arr = np.asarray(values)
        if name.endswith(".positions"):
            arr = arr.reshape(-1, 3)
        elif name.endswith(".colors"):
            arr = arr.reshape(-1, 4)
        prepared.append((name, _dtype_of(name), arr))
    geometry_io.sections_to_points(helper.data, prepared)
    helper.update_tag()


def _dtype_of(name):
    if name.endswith(".colors") or name in ("crv.types", "crv.orders", "crv.knots_mode", "crv.cyclic"):
        return "u8"
    if (
        name.endswith(".verts")
        or name.endswith(".crease_values")
        or name.endswith(".points")
        or name.endswith(".positions")
        or name.endswith(".radii")
        or name.endswith("inst.transforms")
    ):
        return "f32"
    return "u32"


# ------------------------------------------------------------------- cache/store


def _set(stage, field, value):
    """Assign a stage field only when it changes. An unchanged write would tag
    the depsgraph and restart Cycles accumulation for nothing."""
    if getattr(stage, field) != value:
        setattr(stage, field, value)
        return True
    return False


def _rest_state(stage):
    """The state a stage settles into after an answer. A frozen stage stays
    frozen through a Pull; it owns the state (plan §9.2)."""
    return stack_mod.STATE_FROZEN if stage.frozen else stack_mod.STATE_OK


def _store_for(record, stage):
    key = (record.stack_id, stage.key)
    root = prefs.data_root()
    store = _stores.get(key)
    limit = prefs.cache_size_bytes()
    if store is None or store.index.base_dir != root:
        store = frame_cache.ResponseStore(root, record.stack_id, stage.key, limit)
        _stores[key] = store
    else:
        store.index.size_limit = limit
    return store


def _definition(stage):
    return props.definition_fingerprint(stage)


def _forget_prefix(obj, stage_key):
    """Drop one stage's memoized prefix so the next pass re-evaluates it.

    The memo caches the evaluated *sections* under a revision, so anything that
    makes a revision untrustworthy has to retire the entry. Push in particular is
    documented as the way to get past anything cached: before this it reset only
    the frame store, so a stale prefix survived it and the re-request went out
    carrying the same geometry.
    """
    if obj is not None:
        _prefix_memo.pop((obj.as_pointer(), stage_key), None)


def _bump_generation(obj, record, stage, reason=""):
    """A reset: old frames describe the wrong definition and are deleted."""
    _forget_prefix(obj, stage.key)
    stage.generation += 1
    _cache.pop((record.stack_id, stage.key), None)
    _trace.append("%s: reset (%s)" % (stage.key, reason or "definition changed"))
    try:
        removed = _store_for(record, stage).prune(generation=stage.generation)
    except OSError as exc:
        removed = []
        prefs.log("[gh-stack] cache reset failed for %s: %r" % (stage.key, exc), flush=True)
    if removed:
        prefs.log(
            "[gh-stack] %s: cache reset (%s), %d frame(s) dropped"
            % (stage.key, reason or "definition changed", len(removed)),
            flush=True,
        )


def _store_frame(record, stage, rid, header, sections):
    """Persist a response as the raw protocol frame plus a metadata sidecar."""
    if header.get("status") != "ok":
        return
    entries = []
    for entry in header.get("sections") or []:
        name = entry.get("name")
        if name in sections:
            entries.append((name, entry.get("dtype"), sections[name]))
    try:
        raw = codec.encode_frame(header, entries)
    except (codec.CodecError, KeyError, TypeError) as exc:
        prefs.log("[gh-stack] could not store a frame for %s: %r" % (stage.key, exc), flush=True)
        return
    try:
        _store_for(record, stage).store(
            rid,
            raw,
            generation=stage.generation,
            definition=_definition(stage),
            out_hash=header.get("out_hash") or "",
        )
    except OSError as exc:
        prefs.log("[gh-stack] cache write failed for %s: %r" % (stage.key, exc), flush=True)


def _remember(record, stage, rid, header, sections):
    """Put a decoded response in the hot memory cache, bounded by the same byte
    budget as the disk store (FIFO over insertion order; the disk is the
    backing store, so an evicted frame just loads again)."""
    cache = _cache.setdefault((record.stack_id, stage.key), {})
    cache[rid] = (header, sections)
    limit = prefs.cache_size_bytes()
    while len(cache) > 1 and _memory_usage(cache) > limit:
        oldest = next(iter(cache))
        if oldest == rid:
            break
        del cache[oldest]


def _memory_usage(cache):
    """Approximate bytes of a memory-cache entry set. Attribute sections get
    their real dtype from the frame's descriptors; everything else from its
    name. The number only drives eviction, so a small overestimate is fine —
    a wrong *underestimate* would let the cache grow past its bound."""
    total = 0
    for header, sections in cache.values():
        dtypes = {}
        for descriptor in header.get("attributes") or []:
            name = descriptor.get("section")
            if name:
                spec = attributes_mod.WIRE_TYPES.get(descriptor.get("type"), ("f32",))
                dtypes[name] = spec[0]
            offsets = descriptor.get("offsets")
            if offsets:
                dtypes[offsets] = "u32"
        for name, values in sections.items():
            dtype = dtypes.get(name) or _dtype_of(name)
            try:
                total += len(values) * (1 if dtype == "u8" else 4)
            except TypeError:
                continue
    return total


def _cached_response(record, stage, rid):
    """A stored response for this rid: memory first, then disk. Entries that do
    not match the current generation or definition are dropped on the way."""
    memory = _cache.get((record.stack_id, stage.key))
    if memory is not None:
        hit = memory.get(rid)
        if hit is not None:
            return hit
    try:
        store = _store_for(record, stage)
        entry = store.valid(rid, generation=stage.generation, definition=_definition(stage))
        if entry is None:
            return None
        raw = store.load(rid)
    except OSError as exc:
        prefs.log("[gh-stack] cache read failed for %s: %r" % (stage.key, exc), flush=True)
        return None
    if raw is None:
        return None
    try:
        header, sections = codec.decode_frame(raw)
    except codec.CodecError as exc:
        prefs.log("[gh-stack] dropping a corrupt cached frame for %s: %r" % (stage.key, exc), flush=True)
        store.remove(rid)
        return None
    if header.get("status") != "ok" or header.get("stage") != stage.key:
        store.remove(rid)
        return None
    _remember(record, stage, rid, header, sections)
    return header, sections


# --------------------------------------------------------------------- process


def _process_object(obj):
    record = stack_mod.stack_of(obj)
    if record is None or not record.stages:
        return False
    if not record.stack_id:
        record.stack_id = stack_mod.ensure_stack(obj).stack_id
    busy = _drop_removed_stages(obj, record)
    for stage in record.stages:
        outcome = _process_stage(obj, record, stage)
        if outcome == "waiting":
            # Later stages would run on a prefix that is not final yet (§9.1.5).
            busy = True
            break
        if outcome == "busy":
            busy = True
    return busy


def _drop_removed_stages(obj, record):
    """Removing a stage is deleting its modifier in the modifiers panel (§9.8):
    the modifier is the stage's anchor in the stack, so a stage whose modifier is
    gone is removed here — helpers, block definitions and parameter properties
    included. Cache entries are kept. Returns True when something was removed.

    A missing *node group* is not removal: ``modifier_of`` finds the modifier by
    its persistent uid and the group is rebuilt by ``_repair_plumbing``."""
    removed = False
    for index in range(len(record.stages) - 1, -1, -1):
        stage = record.stages[index]
        if nodes.modifier_of(obj, stage) is not None:
            continue
        if not stage.modifier_uid and nodes.modifier_by_name(obj, stage) is not None:
            # A record from before the uid existed, whose node group is gone
            # too: the modifier is there but has nothing to match on. Keep the
            # stage — removal is data loss, and the pass repairs it.
            continue
        _forget(record.stack_id, stage.key)
        _sent.pop((record.stack_id, stage.key), None)
        _pull_permits.discard((record.stack_id, stage.key))
        _verify.discard((record.stack_id, stage.key))
        _forget_prefix(obj, stage.key)
        helpers.remove_helper(obj, stage.key)
        for definition in list(helpers.definition_collections(obj, stage.key)):
            helpers.remove_definition(obj, stage.key, definition)
        stack_mod.clear_stage_params(obj, stage.key)
        nodes.remove_orphan_group(stage.key)
        record.stages.remove(index)
        removed = True
    if removed:
        obj.update_tag()
    return removed


def _repair_plumbing(obj, record, stage, modifier):
    """Rebuild plumbing a saved file (or the outliner) lost: the node group and
    the helper objects. A rebuilt stage is marked unbound until an answer
    arrives, and its applied hash is cleared so a cached frame with the same
    out_hash is re-applied instead of skipped over empty helpers (plan §9.7).
    Returns True when something was rebuilt."""
    rebuilt = False
    if helpers.missing_kinds(obj, stage.key):
        helpers.ensure_helpers(obj, stage.key)
        modifier.node_group = nodes.ensure_group(obj, stage.key)
        rebuilt = True
    elif nodes.bind_modifier(obj, stage, modifier):
        rebuilt = True
    if stage.modifier_uid != modifier.persistent_uid:
        # Adopt the uid of the modifier actually found: an old record has none,
        # and a stale one (a record restored by undo, a duplicate's copied uid)
        # follows the modifier that is really there.
        stage.modifier_uid = modifier.persistent_uid
    if rebuilt:
        _forget(record.stack_id, stage.key)
        _set(stage, "applied_out_hash", "")
        if stage.state != stack_mod.STATE_UNBOUND:
            _set(stage, "state", stack_mod.STATE_UNBOUND)
        prefs.log("[gh-stack] %s: plumbing rebuilt, marked unbound" % stage.key, flush=True)
    return rebuilt


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
    if _last_no_send.get(key) == token:
        return
    _last_no_send[key] = token
    _trace.append("%s: no send (%s)" % (stage.key, reason))


def _process_stage(obj, record, stage):
    """One stage's pass. Returns "waiting" (request in flight; stop the stack),
    "busy" (work happened; keep the fast timer), or "idle"."""
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
    # The modifier's own toggles are the control (plan §11): a stage is active
    # while visible in any context, and the record mirrors that for the state
    # row. Nothing here ever writes a modifier flag back — that was the bug
    # where a toggle in the modifiers panel was undone by the next pass.
    _set(stage, "enabled", active)
    if not active:
        _set(stage, "state", stack_mod.STATE_DISABLED)
        _no_send(record, stage, "disabled")
        return "idle"
    _repair_plumbing(obj, record, stage, modifier)
    if stage.frozen and (record.stack_id, stage.key) not in _pull_permits:
        # Freeze: no requests and no cache replay; the helpers keep their last
        # output. Pull (engine.request_again) plants a one-shot permit above and
        # works while frozen; it is consumed here.
        _set(stage, "state", stack_mod.STATE_FROZEN)
        _no_send(record, stage, "frozen")
        return "idle"

    # While a request is young, a pass costs nothing: the prefix does not have
    # to be read to know nothing can go out before the soft retry. Without this
    # every edit during a slow solve re-evaluated the whole prefix and threw it
    # away (measured: 62 clones for 13 sends on a 51k-vertex drag).
    pending = _pending_of(record.stack_id, stage.key)
    if pending:
        sent_rid = next(iter(pending))
        if time.monotonic() - pending[sent_rid] < SOFT_RETRY_SECONDS:
            _no_send(record, stage, "waiting on rid %s" % sent_rid[-8:])
            return "waiting"

    started = time.monotonic()
    sections, geom_hash, attributes, definitions = _prefix_sections(obj, record, stage)
    _record_timing(record, stage, "prefix_ms", _timed_since(started))
    params_json = props.canonical_json(obj, stage)
    rid = hashing.request_id(stage.key, params_json, geom_hash, "m", "stack-local", stage.nonce)

    # `stage.rid` is deliberately NOT set here. It names the request the stage is
    # waiting on — the answer it must recognise as its own — so it is written only
    # when a request actually goes out (or when a cached frame is applied). While
    # a reply is in flight the playhead may have moved on, and updating `stage.rid`
    # up front would make `_handle_response` discard that reply as stale: during
    # playback nothing would ever be applied and the output would freeze for as
    # long as the round trip takes. The wanted frame is fetched on the next pass,
    # from the frame cache when it is warm.

    verify_key = (record.stack_id, stage.key)
    cached = _cached_response(record, stage, rid)
    connected = bool(bridge.state["connected"])
    sent = _sent.get(verify_key)
    # Whether the client has been told *this* request. A frame in the cache is
    # Blender's answer, not the client's state: cancelling a transform (or an
    # undo, or a reverted slider) returns the rid to an earlier, cached frame
    # while the client still holds the intermediate one.
    client_has = sent is not None and sent[0] == rid
    # A reconnect or a file load puts the stage on `_verify`: the definition may
    # have changed while no one was listening, and a cached frame cannot know
    # it. With a client there, ask; with none, replay the cache as usual and
    # keep the permit for when one connects.
    if cached is not None and (verify_key not in _verify or not connected):
        header, cached_sections = cached
        out_hash = header.get("out_hash") or ""
        _set(stage, "rid", rid)
        applied = False
        if out_hash != stage.applied_out_hash or _payload_missing(
            obj, stage, cached_sections
        ):
            started = time.monotonic()
            _apply(obj, stage, cached_sections, header.get("attributes"))
            _record_timing(record, stage, "apply_ms", _timed_since(started))
            stats["applies"] += 1
            _set(stage, "applied_out_hash", out_hash)
            _set(stage, "warnings", _warnings_text(header))
            _set(stage, "error", "")
            applied = True
        else:
            # The cached frame is the displayed one, but the group may predate
            # the current format: repairing it here is what lets a replay fix a
            # stale session (see _handle_response).
            nodes.ensure_group(obj, stage.key, _nested_rows_of(cached_sections))
        if not connected or client_has:
            _no_send(record, stage, "cache hit", rid)
            _set(stage, "state", _rest_state(stage))
            return "busy" if applied else "idle"
        # The frame is right for Blender, but the connected client holds some
        # other request. Applying it is not enough: the client must also be told
        # the inputs for this rid, or its viewport keeps the cancelled edit (and
        # the next edit starts from geometry Blender no longer shows). Fall
        # through and send; the answer carries the same output and is not
        # re-applied.

    if not bridge.state["connected"]:
        # A miss with Grasshopper away keeps the last good output; only cached
        # frames replay (renders use the cache). A stage no document has ever
        # answered reads unbound instead of disconnected: disconnected means a
        # client was there and left (plan §9.2).
        _no_send(record, stage, "disconnected", rid)
        _set(
            stage,
            "state",
            stack_mod.STATE_UNBOUND if not stage.applied_out_hash else stack_mod.STATE_DISCONNECTED,
        )
        return "idle"

    pending = _pending_of(record.stack_id, stage.key)
    retry_note = ""
    if pending:
        # One evaluation at a time. While an answer is outstanding the stage asks
        # for nothing else — not even the frame the playhead has moved to.
        # Grasshopper solves on one thread in the order it receives, so asking for
        # frames while it works only builds a backlog it grinds through after the
        # user has moved on: the client stops responding and every queued frame is
        # one nobody is looking at by the time it arrives. The frame the scene is
        # on is requested on the first pass after this answer lands, and the answer
        # itself is stored either way, so an evaluation is never wasted.
        #
        # A replay never reaches here — the lookup above answers it first — so a
        # cached frame is not throttled by whatever is outstanding. The one
        # frame that can arrive with a request in flight is a verification
        # (reconnect or load): by then the previous request belonged to the
        # session that just ended, and `hello`/`load` cleared it.
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
            _forget(record.stack_id, stage.key)
            _trace.append("%s: superseded %s" % (stage.key, sent_rid[-8:]))
        else:
            # The answer never came. We cannot know whether Grasshopper received
            # the geometry, so the retry goes out in full — the same reasoning as
            # the unknown_ref handler. And the failure stays visible: retrying
            # silently is what made a stage stuck in this loop look healthy.
            _forget(record.stack_id, stage.key)
            _sent.pop(verify_key, None)
            _set(stage, "geom_hash", "")
            stats["timeouts"] += 1
            retry_note = "timed out after %.0f s; requesting the geometry again" % _timeout()

    # Use the session truth, not the saved stage record: undo/redo restores
    # `stage.geom_hash` from its step, while the client keeps the geometry it
    # was last sent. No session entry means no client has been told anything
    # this session, so the first request must carry the geometry in full (§8.8).
    held_geom = _sent[verify_key][1] if verify_key in _sent else ""
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
    _record_timing(record, stage, "encode_ms", _timed_since(started))
    if not server.SERVER.send(frame):
        # No client socket to carry it. Recording a pending request here would
        # make the stage wait out a whole timeout for an answer nobody was asked
        # for — and the retry would be gated on that same pending entry.
        stats["undelivered"] += 1
        _set(stage, "state", stack_mod.STATE_DISCONNECTED)
        _no_send(record, stage, "undelivered", rid)
        return "idle"
    _verify.discard(verify_key)
    # The client has now been told this request; from here the frame cache may
    # answer it without a send (until a reconnect/load/refusal says otherwise).
    _sent[verify_key] = (rid, geom_hash)
    # A Pull permit is spent by the send it caused, not before it: a send that
    # never left (disconnected, undelivered) leaves the Pull armed for the next
    # pass instead of losing it silently.
    _pull_permits.discard((record.stack_id, stage.key))
    _trace.append(
        "%s: sent %s %s" % (stage.key, mode, rid[-8:] if rid else "")
    )
    if retry_note:
        prefs.log("[gh-stack] %s: %s" % (stage.key, retry_note), flush=True)
    stats["sends"] += 1
    # A send re-arms the "why no send" reason: the next silence is a new
    # decision, not a repeat of the pass before this one.
    _last_no_send.pop((record.stack_id, stage.key), None)

    now = time.monotonic()
    _heartbeat["sent_at"] = now
    _pending_of(record.stack_id, stage.key)[rid] = now
    _waiting_objects.add(obj.name)
    _state_since[(record.stack_id, stage.key)] = now
    _set(stage, "rid", rid)
    _set(stage, "geom_hash", geom_hash)
    _set(stage, "state", stack_mod.STATE_WAITING)
    _set(stage, "error", retry_note)
    return "waiting"


# ------------------------------------------------------------------ prefix cache


def _prefix_sections(obj, record, stage):
    """The geometry a stage sees: everything before its own modifier.

    Read from the evaluated clone in :func:`_clone_prefix`, never from the base
    datablocks: shape keys, Edit Mode data and instances are all part of the
    prefix the depsgraph evaluates. Surfaces stay on the meshing path (no
    geometry set). The memo is keyed by the depsgraph generation, so a hit is
    only possible while nothing has been tagged since the read.
    """
    if obj.type == "SURFACE":
        subd = bool(record.subd)
        sections, attributes = geometry_io.object_to_sections(obj, as_subd=subd)
        return sections, _sections_hash(sections, attributes), attributes, []

    modifier = nodes.modifier_of(obj, stage)
    index = _modifier_index(obj, modifier)
    revision = _prefix_revision(obj, record)
    # Keyed by identity, not by name: an object renamed to a name another object
    # used to hold would otherwise inherit what that object had evaluated.
    # as_pointer() is stable for the session and keeps no reference to the object,
    # so a deleted object does not live on through this dict.
    memo_key = (obj.as_pointer(), stage.key)
    memo = _prefix_memo.get(memo_key)
    if memo is not None and memo["revision"] == revision:
        stats["prefix_memo_hits"] += 1
        return memo["sections"], memo["geom_hash"], memo["attributes"], memo["definitions"]

    subd = bool(record.subd)
    sections, attributes, definitions = _clone_prefix(obj, index, subd, stage.key)
    stats["prefix_clones"] += 1

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
        stats["prefix_memo_hits"] += 1
        sections = memo["sections"]
        attributes = memo["attributes"]
        definitions = memo["definitions"]
        geom_hash = memo["geom_hash"]
        memo["revision"] = revision
        return sections, geom_hash, attributes, definitions

    geom_hash = _sections_hash(sections, attributes, definitions)
    _prefix_memo[memo_key] = {
        "revision": revision,
        "sections": sections,
        "geom_hash": geom_hash,
        "attributes": attributes,
        "definitions": definitions,
    }
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


def _clone_prefix(obj, index, subd, stage_key=None):
    """The evaluated prefix of one stage: the object just before its modifier.

    The copy is linked, evaluated and removed inside the ``_evaluating`` guard:
    link/unlink tags the stack object like a real edit, and left unmuted it
    would retire the memo the engine is about to write, every pass. Because the
    guard drops everything, the timer drains the depsgraph first. In Edit Mode
    the clone gets a private copy of the flushed datablock — a shared copy
    evaluates empty. The cleanup resets the guard even if a step fails; the
    owner is recorded for the watchdog.
    """
    global _evaluating, _guard_owner, _evaluating_since
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
    _guard_owner = (obj.name, stage_key)
    _evaluating_since = time.monotonic()
    _evaluating = True
    try:
        bpy.context.scene.collection.objects.link(clone)
        for later in list(clone.modifiers)[index:]:
            clone.modifiers.remove(later)
        depsgraph = bpy.context.evaluated_depsgraph_get()
        geometry = clone.evaluated_get(depsgraph).evaluated_geometry()
        sections, attributes, definitions = geometry_io.geometry_set_to_sections(
            geometry, as_subd=subd
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
            _evaluating = False
        if cleanup_failure is not None and not primary_failed:
            raise cleanup_failure
    return sections, attributes, definitions


def _cleanup_prefix_clone(clone, owned_data, stage_key):
    """Undo one prefix clone, one guarded step at a time.

    Removing the clone, dropping its private datablock and updating the view
    layer can each raise. One failing step must not skip the others, and must
    not skip the guard reset — which is why the caller owns that reset. The
    first failure is returned so the pass can report it.
    """
    steps = (
        ("remove clone", lambda: bpy.data.objects.remove(clone, do_unlink=True)),
        (
            "drop private data",
            (lambda: _drop_owned_data(owned_data)) if owned_data is not None else None,
        ),
        ("update view layer", lambda: bpy.context.view_layer.update()),
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
            if not _trace or _trace[-1] != message:
                _trace.append(message)
            import traceback

            prefs.log("[gh-stack] %s\n%s" % (message, traceback.format_exc()), flush=True)
    return failure


# The collections an Edit Mode clone's private copy can belong to. Named by the
# ID type's RNA identifier, which is what the copy reports.
_OWNED_COLLECTIONS = {
    "Mesh": "meshes",
    "Curve": "curves",
    "PointCloud": "pointclouds",
}


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
    would produce the same hash, the same request id, and a cache hit that
    silently keeps the old name."""
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
    return (_prefix_epoch, bool(record.subd), bpy.context.scene.frame_float)


def _bump_epochs(depsgraph=None):
    """Move the generation for everything Blender tagged in this evaluation.

    Called with the depsgraph from `_on_depsgraph`, and with None from
    `_bump_id` for engine writes a later stage in the same pass must see. Every
    entry counts, including all-false ones (node tree edits, driver-driven ID
    properties): the flags say what kind of update it was, not whether an input
    moved. The engine's own clone events are excluded by the guard alone.
    Returns the update count for the heartbeat.
    """
    global _prefix_epoch
    _prefix_epoch += 1
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
    if _trace and _trace[-1].startswith("depsgraph"):
        _trace[-1] = line
    else:
        _trace.append(line)
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


def _timed_since(started):
    """Milliseconds elapsed since a `time.monotonic()` stamp."""
    return (time.monotonic() - started) * 1000.0


def _record_timing(record, stage, field, value):
    """Keep the last measured latency of one stage field."""
    _timings.setdefault((record.stack_id, stage.key), {})[field] = value


def stage_timings(stack_id, stage_key):
    """The last measured pass latencies of one stage, in milliseconds.

    Read by the Diagnostics panel ("clone 11 ms · encode 2 ms · answer 68 ms ·
    apply 3 ms"), so a slow round trip names its slowest part instead of being a
    black box. A copy, not the live mapping: a pass may write while the panel
    draws, and the panel must never be able to change engine state. Fields that
    have not been measured yet are absent.
    """
    values = _timings.get((stack_id, stage_key))
    return dict(values) if values else {}


def _age(now, then):
    return None if then is None else max(0.0, now - then)


def trigger_health():
    """Ages of the last trigger, send and apply, for the Diagnostics panel.

    A frozen trigger age while the scene is edited is the tell for a mute; the
    ages are seconds, or None before the event has happened this session.
    """
    now = time.monotonic()
    return {
        "trigger_age": _age(now, _heartbeat["trigger_at"]),
        "trigger_count": _heartbeat["trigger_count"],
        "trigger_updates": _heartbeat["trigger_updates"],
        "send_age": _age(now, _heartbeat["sent_at"]),
        "apply_age": _age(now, _heartbeat["applied_at"]),
    }


def waiting_seconds(stack_id, stage_key):
    """How long a stage has been waiting for an answer, or 0.0 when it is not.

    Read by the diagnostics panel: an answer outstanding for a second is where a
    sync fault is indistinguishable from a stage that is merely thinking.
    """
    since = _state_since.get((stack_id, stage_key))
    if since is None:
        return 0.0
    return time.monotonic() - since


def recent_activity(count=8):
    """The last events the engine acted on, oldest first.

    The Diagnostics panel shows them so a sync fault is a visible chain
    (trigger -> decision -> request -> answer) instead of a guess.
    """
    return list(_trace)[-count:]


# ------------------------------------------------------------------- lifecycle


def start_server(port=None):
    if port is None:
        port = int(prefs._pref("port", prefs.DEFAULT_PORT))
    return server.SERVER.start(port)


def request_again(obj, stage=None, skip_frozen=False):
    """Force a fresh request without touching the frame cache: the nonce changes
    so the request id cannot collide with a cached one. Used when a stage is
    created or its identity changes, and by Pull. The memoized prefix is retired
    with it: a fresh request must carry what the scene shows.

    The target is flushed first when it is in Edit Mode: a request built from
    the stale datablock is a `ref` claiming Grasshopper's copy is current, which
    is how Push after a stroke on a non-active edit object recovered nothing.
    """
    record = stack_mod.stack_of(obj)
    if record is None:
        return
    _flush_object(obj)
    if not record.stack_id:
        record.stack_id = stack_mod.ensure_stack(obj).stack_id
    stages = [stage] if stage is not None else list(record.stages)
    for item in stages:
        if skip_frozen and item.frozen:
            continue
        item.nonce += 1
        _forget(record.stack_id, item.key)
        _forget_prefix(obj, item.key)
        if item.frozen:
            # Pull on a frozen stage: one request, then parked again.
            _pull_permits.add((record.stack_id, item.key))
            _set(item, "state", stack_mod.STATE_FROZEN)
        else:
            _set(item, "state", stack_mod.STATE_IDLE)
    _dirty.add(obj.name)
    _mark_hot()


def freeze(obj, stage):
    """Freeze a stage: stop requests and cache replay; the helpers keep their
    last output. An answer already in flight is forgotten (and still cached when
    it lands); Pull refreshes on demand while frozen."""
    record = stack_mod.stack_of(obj)
    if record is None:
        return
    _pull_permits.discard((record.stack_id, stage.key))
    _forget(record.stack_id, stage.key)
    _set(stage, "rid", "")
    _set(stage, "error", "")
    _set(stage, "state", stack_mod.STATE_FROZEN)


def unfreeze(obj, stage):
    """Resume a frozen stage: the next pass replays a cached frame or asks
    Grasshopper again, whichever the current request id resolves to."""
    record = stack_mod.stack_of(obj)
    if record is not None:
        _pull_permits.discard((record.stack_id, stage.key))
    _set(stage, "state", stack_mod.STATE_IDLE)
    _dirty.add(obj.name)
    _mark_hot()


def push(obj=None):
    """Force a fresh request for every live stage of a stack (increments the
    nonce) and reset its frame cache: Push exists to get past anything cached.
    Frozen stages are parked and skipped entirely (plan §9.8)."""
    obj = obj or bpy.context.object
    if obj is None:
        return
    record = stack_mod.stack_of(obj)
    if record is None:
        return
    if not record.stack_id:
        record.stack_id = stack_mod.ensure_stack(obj).stack_id
    for stage in record.stages:
        if stage.frozen:
            continue
        _bump_generation(obj, record, stage, "Push")
    request_again(obj, skip_frozen=True)


def _mark_object_dirty(obj):
    _dirty.add(obj.name)
    _mark_hot()


def register():
    global _registered, _hot_until
    global _evaluating, _guard_owner, _evaluating_since, _ticking
    if _registered:
        return
    # Everything the last registration learned belongs to the session that just
    # ended: a decoded frame, an evaluated prefix, a request in flight, a counter.
    # Leaving them behind served a stale prefix and stale counters across a
    # disable/enable. `_dirty` is left alone — it discards nameless objects on its
    # own, and clearing it could skip the first pass a stack needs.
    _stores.clear()
    _cache.clear()
    _pending.clear()
    _sent.clear()
    _prefix_memo.clear()
    _state_since.clear()
    _verify.clear()
    _pull_permits.clear()
    _last_no_send.clear()
    _timings.clear()
    # The prefix guard and the tick flag are session state too: a wedge used to
    # survive disable/enable, so re-enabling the add-on inherited a mute that
    # dropped every edit until the watchdog fired. Registering is a clean start;
    # the watchdog stays as the belt-and-braces for a leak within a session.
    _evaluating = False
    _guard_owner = None
    _evaluating_since = None
    _ticking = False
    for field in _heartbeat:
        _heartbeat[field] = 0 if field in ("trigger_count", "trigger_updates") else None
    _hot_until = 0.0
    for counter in stats:
        stats[counter] = 0
    # A UI-driven parameter write (the choice dropdown) must reach the engine.
    stack_mod.set_param_changed_hook(_mark_object_dirty)
    # Persistent: Blender clears non-persistent timers and handlers on every
    # file load, which would otherwise silence the engine on reopen. The
    # membership checks make a second register() harmless: appending the same
    # handler twice would run the depsgraph path twice per update, and a leaked
    # duplicate outlives the next disable.
    if not bpy.app.timers.is_registered(_timer):
        bpy.app.timers.register(_timer, first_interval=IDLE_INTERVAL, persistent=True)
    for handler_list, handler in (
        (bpy.app.handlers.depsgraph_update_post, _on_depsgraph),
        (bpy.app.handlers.frame_change_post, _on_frame_change),
        (bpy.app.handlers.load_post, _on_load),
        (bpy.app.handlers.undo_post, _on_undo),
        (bpy.app.handlers.redo_post, _on_redo),
    ):
        if handler not in handler_list:
            handler_list.append(handler)
    _registered = True
    try:
        start_server()
    except Exception as exc:
        # A socket that cannot start is a visible error state (the panel shows
        # it), not a reason to abort registration half-way: the timer, handlers
        # and `_registered` must stay paired so unregister can tear them down.
        # The user's Start/retry goes through the same `start_server`.
        bridge.state["error"] = "server start failed: %r" % (exc,)
        prefs.log("[gh-stack] server start failed: %r" % (exc,), flush=True)


def unregister():
    global _registered
    if not _registered:
        return
    stack_mod.set_param_changed_hook(None)
    if bpy.app.timers.is_registered(_timer):
        bpy.app.timers.unregister(_timer)
    for handler_list, handler in (
        (bpy.app.handlers.depsgraph_update_post, _on_depsgraph),
        (bpy.app.handlers.frame_change_post, _on_frame_change),
        (bpy.app.handlers.load_post, _on_load),
        (bpy.app.handlers.undo_post, _on_undo),
        (bpy.app.handlers.redo_post, _on_redo),
    ):
        if handler in handler_list:
            handler_list.remove(handler)
    _registered = False
