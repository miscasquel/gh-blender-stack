"""Blender handlers, the timer and the depsgraph/edit-mode triggers."""

import time

import bpy

from .. import bridge, helpers, nodes, prefs
from .. import stack as stack_mod
from .lifecycle import (
    _all_stack_objects,
    _ensure_index,
    _mark_dirty_all,
    _mark_dirty_scene,
    _process_object,
    _rebuild_index,
    _repair_plumbing,
    _stage_key_conflicted,
)
from .messages import _handle_incoming
from .state import _set_state
from .request import _bump_epochs, _expire_pull, _pull_expired, _set
from .runtime import (
    ENGINE,
    HOT_INTERVAL,
    IDLE_INTERVAL,
    SOFT_RETRY_SECONDS,
    WAIT_INTERVAL,
)


@bpy.app.handlers.persistent
def _on_depsgraph(scene, depsgraph=None):
    # By design, every depsgraph update re-reads every stage prefix: Python
    # cannot ask a node tree which datablocks it depends on, and a narrower
    # filter is silent stale geometry when it misses one. The pass is coalesced
    # to one per tick; only the redundant evaluation before it was removed.
    if ENGINE.evaluating:
        _trace_guard_drop()
        return
    ENGINE.heartbeat["trigger_at"] = time.monotonic()
    ENGINE.heartbeat["trigger_count"] += 1
    ENGINE.heartbeat["trigger_updates"] = _bump_epochs(depsgraph)
    # A depsgraph event belongs to one scene. Marking every stack made an
    # unrelated scene's stage ask again, with the wrong scene's frame.
    _mark_dirty_scene(scene)
    ENGINE.mark_hot()


def _trace_guard_drop():
    """Note a depsgraph update the guard swallowed, but only when it is a symptom.

    Guard events during a pass are the engine's own clone; an update with no
    pass running means the guard was left up by a pass that died — the mute the
    watchdog exists for — and only that one is named.
    """
    if ENGINE.ticking:
        return
    where = "%s (stage %s)" % ENGINE.guard_owner if ENGINE.guard_owner else "unknown"
    line = "depsgraph update dropped: guard up by %s" % where
    if not ENGINE.trace_lines or ENGINE.trace_lines[-1] != line:
        ENGINE.trace(line)


def _drain_depsgraph():
    """Force any evaluation Blender still owes *before* this pass can clone.

    The clone mutes the depsgraph handler while it evaluates, so an update
    delivered there would be dropped with nothing left to mark the stack dirty
    — the "random, needs Push" failure. Draining first delivers every pending
    update while the guard is down; it is free when nothing is tagged.
    """
    if ENGINE.evaluating:
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
    prefix clone copies the pre-edit cage and the unchanged hash looks like no
    edit at all. A no-op outside Edit Mode, so Push/Pull can call it directly;
    a RuntimeError leaves the datablock as it was.
    """
    if obj is None or obj.mode != "EDIT":
        return False
    try:
        obj.update_from_editmode()
    except RuntimeError:
        return False
    ENGINE.trace("flushed Edit Mode data for %s" % obj.name)
    return True


def _flush_edit_mode():
    """Flush every dirty stack object that is in Edit Mode, not only the active.

    A referenced object needs no flush (its edits reach the evaluated prefix
    through the graph), and the dirty gate keeps an idle session from copying
    meshes for nothing. Iterating every edit object is what makes a stroke on a
    non-active object — or behind a non-stack active object — reach Grasshopper
    while the mode is open.
    """
    if not ENGINE.dirty:
        # Nothing is waiting for a flush; skip the mode scan on every idle tick.
        return
    for obj in _edit_mode_objects():
        if stack_mod.stack_of(obj) is None:
            continue
        if obj.name not in ENGINE.dirty:
            continue
        _flush_object(obj)


@bpy.app.handlers.persistent
def _on_frame_change(scene, depsgraph=None):
    # A frame change tags nothing (the frame is part of the prefix revision).
    # It queues the stacks of *this* scene: a stage on an object in another
    # scene keeps that scene's frame and is not woken by this one.
    if ENGINE.switching_scene:
        # Fired by the prefix read temporarily activating the object's scene,
        # not by the user or by playback.
        return
    _mark_dirty_scene(scene)
    ENGINE.mark_hot()


def _after_history_step():
    """Re-assert the client-side state after an undo or redo.

    The stage record is part of the .blend, so undo/redo restores its saved
    `geom_hash`, `rid`, `applied_out_hash` and `applied_topology` to the values
    captured in the step. A topology token names the helper data of that step,
    not the session's next apply, so it is cleared here. What the connected
    client holds did not travel with that step: after undoing a move, the client
    still has the moved geometry. `_sent` is the session truth, so the saved
    geometry field is put back from it (or cleared when nothing was sent, which
    asks in full) and the next pass decides full/ref correctly instead of
    sending a ref the client must refuse.

    Marking every stack dirty also gives the step a pass even when its
    depsgraph delivery is missed; the memo is cleared for the same reason — an
    undo restores datablocks the memo may name.

    The step cannot prove which of two claimants was the original: a restored
    shared key is a strict conflict, never a name-based split.
    """
    ENGINE.stage_owners.clear()
    _rebuild_index()
    for obj in list(ENGINE.stack_objects):
        record = stack_mod.stack_of(obj)
        if not record.stack_id:
            record.stack_id = stack_mod.ensure_stack(obj).stack_id
        for stage in record.stages:
            row = ENGINE.peek_runtime(record.stack_id, stage.key)
            sent = row.sent if row is not None else None
            _set(stage, "geom_hash", sent[1] if sent is not None else "")
            # A topology token restored by the step describes the helper data
            # saved in that step, not what the session will apply next. Drop it
            # so the next apply rebuilds or verifies instead of trusting it.
            _set(stage, "applied_topology", "")
            # The solver state is not part of Blender's undo history: ask for
            # the current state instead of assuming the restored rid was
            # delivered.
            ENGINE.verify.add((record.stack_id, stage.key))
            ENGINE.runtime(record.stack_id, stage.key).last_frame = None
    ENGINE.prefix_memo.clear()
    if ENGINE.ensure_active():
        _mark_dirty_all()
        ENGINE.mark_hot()


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
    evaluated against Grasshopper again.

    A load is also a *new session from Grasshopper's point of view*: the client
    may be a fresh process or a different document, so the geometry hash — a
    claim about what GH still holds — does not survive it. Clearing it makes the
    next request go out with `geom.mode = "full"`, which is what protocol §8.8
    already prescribes for the first request after a connection.
    """
    # A file from before the stage scene still has its plumbing in the user's
    # scene: move it now, so the outliner is clean before the first apply.
    # This also renames object-name-based helpers to the stable stack-id
    # identity, so a file saved under the old scheme keeps its geometry.
    helpers.adopt_existing()
    # A decoded frame belongs to the session that just ended. A response or
    # schema queued before the load must not be applied to the new file (the
    # stage keys may even coincide).
    bridge.clear_incoming()
    ENGINE.clear_runtimes()
    ENGINE.prefix_memo.clear()
    ENGINE.prefix_batch.clear()
    ENGINE.prefix_batch_built = False
    ENGINE.prefix_batching = False
    ENGINE.prefix_epoch = 0
    ENGINE.verify.clear()
    ENGINE.last_stall_check = 0.0
    ENGINE.reset_playback_reports()
    ENGINE.dirty.clear()
    # The guard belongs to the session that just ended. A pass can only be
    # running on the main thread, but a load (a new file, a revert) must not
    # inherit the previous file's wedge: with `_evaluating` set the depsgraph
    # handler would drop every edit in the new file until the watchdog fired.
    ENGINE.evaluating = False
    ENGINE.guard_owner = None
    ENGINE.evaluating_since = None
    ENGINE.ticking = False
    ENGINE.playback_sync = False
    ENGINE.bake_sync = False
    ENGINE.bake_progress = None
    ENGINE.playback_scene = None
    ENGINE.bake_scene = None
    ENGINE.switching_scene = False
    # A load cannot prove which of two objects sharing a key was the original.
    # Build the index without trusting the previous session's owners: a shared
    # key becomes a visible conflict rather than a name-based silent split.
    ENGINE.stage_owners.clear()
    ENGINE.stage_conflicts.clear()
    ENGINE.stage_claims.clear()
    _rebuild_index()
    for obj in list(ENGINE.stack_objects):
        record = stack_mod.stack_of(obj)
        if record is None:
            continue
        for stage in record.stages:
            if stage.state == stack_mod.STATE_WAITING:
                _set_state(stage, stack_mod.STATE_IDLE)
            # A saved request id names a request from the session that just
            # ended. A response queued at load must not apply it before
            # `_verify` has asked Grasshopper again, so clear it on every stage;
            # for a frozen stage it also keeps a late answer from unparking the
            # output. Pull permits belong to that old session too: an armed Pull
            # must not fire into a stage the file saved frozen.
            _set(stage, "rid", "")
            # The token belongs to the helper data this file saved; a new
            # session proves nothing about it (and an old producer may not
            # have sent one), so the next apply verifies or rebuilds.
            _set(stage, "applied_topology", "")
            if stage.geom_hash:
                # We cannot know that GH kept the geometry: ask again in full.
                stage.geom_hash = ""
            if not record.stack_id:
                record.stack_id = stack_mod.ensure_stack(obj).stack_id
            if _stage_key_conflicted(stage.key):
                # The conflict state names the claimants and blocks both the
                # request and the answer path; do not repair or verify it.
                continue
            # A saved file may meet a document that changed in between: ask
            # once even if the saved request id and frame still match.
            ENGINE.verify.add((record.stack_id, stage.key))
            # Plumbing lost with the file (helpers or the node group deleted
            # before the save) is rebuilt here and the stage marked unbound
            # until an answer arrives. A missing *modifier* is not
            # repaired: deleting it is how a stage is removed (§9.8).
            modifier = nodes.modifier_of(obj, stage)
            if modifier is not None:
                _repair_plumbing(obj, record, stage, modifier)
    _rebuild_index()
    # Activate only when this file has stages. A load into a stage-less file
    # must leave Blender with no Stageloom timer or per-frame handler at all.
    if ENGINE.ensure_active():
        _mark_dirty_all()
        ENGINE.mark_hot()


def _recover_stuck_guard():
    """Clear `_evaluating` when a previous pass left it set, and say so.

    A tick that starts with the guard up cannot belong to that pass: the owner
    has ended. While it is set every update is dropped and nothing marks a
    stack dirty again, so the tick says who left it, clears it, and marks every
    stack dirty to pick up the edits missed meanwhile.
    """
    age = time.monotonic() - ENGINE.evaluating_since if ENGINE.evaluating_since is not None else 0.0
    where = "%s (stage %s)" % ENGINE.guard_owner if ENGINE.guard_owner else "unknown"
    ENGINE.evaluating = False
    # Every depsgraph update the guard swallowed was dropped *before* it could
    # move the generation, so a memo written before the leak still looks
    # current even though the scene moved. Clearing them is what makes the
    # "mark dirty" below read the edited scene instead of replaying the stale
    # prefix (and the old rid) one more time.
    ENGINE.prefix_memo.clear()
    _mark_dirty_all()
    line = (
        "guard watchdog: prefix guard left up by %s %.2f s ago; cleared and all stacks marked dirty"
        % (where, age)
    )
    ENGINE.trace(line)
    prefs.log("[stageloom] " + line, flush=True)


def _in_background():
    """Whether this Blender has no GUI event loop (tests, headless renders)."""
    return bool(bpy.app.background)


def _all_stacks_dirty():
    """Whether every stack object in this file is already queued for a pass."""
    _ensure_index()
    names = {obj.name for obj in ENGINE.stack_objects}
    return bool(names) and names <= ENGINE.dirty


def _needs_drain(dirty_before):
    """Whether this pass must deliver pending depsgraph updates before it clones.

    A pass that clones every queued stack does not need a separate evaluation:
    the clone sees the pending edit, and bumping the depsgraph generation makes
    it read the scene instead of a memo. A narrower pass drains, because the
    pending update may name a stack that is not queued. Background sessions have no
    event loop delivering updates, so they always drain.
    """
    if _in_background():
        return True
    return bool(dirty_before) and not _all_stacks_dirty()


def _timer():
    """bpy.app.timers callback. Returns the next interval.

    A timer that raises is unregistered by Blender, which would silently stop the
    engine; every part of the pass is therefore guarded, and failures land in the
    panel state instead.
    """
    try:
        if not ENGINE.active:
            # A stale tick after deactivation (or a direct test call): there is
            # no stage to evaluate, so do not touch the scene.
            return None
        if ENGINE.playback_sync or ENGINE.bake_sync:
            # Play Every Frame or a native Bake is driving the stage pass
            # synchronously from frame_change_post; an asynchronous pass for
            # the live playhead must not race it. The playback/bake end hooks
            # clear their flag, so the timer resumes afterwards.
            return IDLE_INTERVAL
        _ensure_index()
        if not ENGINE.stack_objects:
            ENGINE.deactivate()
            return None
        ENGINE.stats()["passes"] += 1
        ENGINE.ticking = True
        # Before anything else: a guard already up at the start of a tick cannot
        # belong to this pass, so it is the wedge the watchdog exists for.
        if ENGINE.evaluating:
            _recover_stuck_guard()
        # Drain only when the pass is narrower than the queues: an all-dirty
        # pass clones every prefix anyway, and the clone sees the pending edit.
        dirty_before = bool(ENGINE.dirty)
        if _needs_drain(dirty_before):
            _drain_depsgraph()
        elif dirty_before:
            _bump_epochs(None)
        _handle_incoming()
        _nudge_stalled_waits()
        if ENGINE.dirty and not dirty_before:
            # Handling a message or the stall scan can produce work after the
            # gate above. Drain before the clone this pass is now about to run.
            if _needs_drain(True):
                _drain_depsgraph()
            else:
                _bump_epochs(None)
        _flush_edit_mode()
        busy = False
        for name in list(ENGINE.dirty):
            obj = bpy.data.objects.get(name)
            if obj is None:
                ENGINE.dirty.discard(name)
                continue
            try:
                if _process_object(obj):
                    busy = True
            except Exception as exc:
                bridge.state["error"] = "engine error on %s: %r" % (name, exc)
                import traceback

                prefs.log("[stageloom] engine error:\n%s" % traceback.format_exc(), flush=True)
                # The pass died before it could decide anything for this
                # object: say so in the panel, where the silence is what a user
                # is staring at. One line per consecutive identical failure.
                line = "engine error on %s: %r" % (name, exc)
                if not ENGINE.trace_lines or ENGINE.trace_lines[-1] != line:
                    ENGINE.trace(line)
            ENGINE.dirty.discard(name)
        if not ENGINE.stack_objects:
            # The pass removed the last stage: stop the timer and the per-frame
            # handlers until a load/undo/add brings one back.
            ENGINE.deactivate()
            return None
        if busy:
            return WAIT_INTERVAL
        if time.monotonic() < ENGINE.hot_until:
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

            prefs.log("[stageloom] engine error:\n%s" % traceback.format_exc(), flush=True)
        except Exception:
            pass
        return IDLE_INTERVAL
    finally:
        ENGINE.ticking = False


def _nudge_stalled_waits():
    """Mark a stack object dirty when a stage has waited past the soft retry.

    A waiting stage dirties nothing on its own, and the pass is what supersedes
    a stale request with the current state (or resends at the hard timeout);
    without the nudge a supersede would wait for an unrelated edit. The scan
    walks the scene's stack objects — not names captured at send time, which a
    rename invalidates — and runs at most once a second.

    A frozen Pull is different: it has one permit with a deadline and no newer
    request to supersede it. The nudge expires it instead of poking the object
    forever, and otherwise leaves the object alone until its answer lands.
    """
    now = time.monotonic()
    if now - ENGINE.last_stall_check < 1.0:
        return
    ENGINE.last_stall_check = now
    for obj in _all_stack_objects():
        record = stack_mod.stack_of(obj)
        for stage in record.stages:
            key = (record.stack_id, stage.key)
            if ENGINE.runtime(*key).pull_permit:
                if _pull_expired(key, now):
                    _expire_pull(record, stage)
                continue
            row = ENGINE.peek_runtime(*key)
            pending = row.pending if row is not None else {}
            if not pending:
                continue
            if now - min(pending.values()) > SOFT_RETRY_SECONDS:
                ENGINE.dirty.add(obj.name)
                ENGINE.mark_hot()

