"""Blender handlers, the timer and the depsgraph/edit-mode triggers."""

import sys

engine = sys.modules[__package__]


@engine.bpy.app.handlers.persistent
def _on_depsgraph(scene, depsgraph=None):
    if engine._evaluating:
        engine._trace_guard_drop()
        return
    engine._heartbeat["trigger_at"] = engine.time.monotonic()
    engine._heartbeat["trigger_count"] += 1
    engine._heartbeat["trigger_updates"] = engine._bump_epochs(depsgraph)
    engine._mark_dirty_all()
    engine._mark_hot()


def _trace_guard_drop():
    """Note a depsgraph update the guard swallowed, but only when it is a symptom.

    Guard events during a pass are the engine's own clone; an update with no
    pass running means the guard was left up by a pass that died — the mute the
    watchdog exists for — and only that one is named.
    """
    if engine._ticking:
        return
    where = "%s (stage %s)" % engine._guard_owner if engine._guard_owner else "unknown"
    line = "depsgraph update dropped: guard up by %s" % where
    if not engine._trace or engine._trace[-1] != line:
        engine._trace.append(line)


def _drain_depsgraph():
    """Force any evaluation Blender still owes *before* this pass can clone.

    The clone mutes the depsgraph handler while it evaluates, so an update
    delivered there would be dropped with nothing left to mark the stack dirty
    — the "random, needs Push" failure. Draining first delivers every pending
    update while the guard is down; it is free when nothing is tagged.
    """
    if engine._evaluating:
        return
    try:
        engine.bpy.context.evaluated_depsgraph_get().update()
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
        candidates = list(engine.bpy.context.objects_in_mode)
    except AttributeError:
        candidates = list(engine.bpy.data.objects)
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
    engine._trace.append("flushed Edit Mode data for %s" % obj.name)
    return True


def _flush_edit_mode():
    """Flush every dirty stack object that is in Edit Mode, not only the active.

    A referenced object needs no flush (its edits reach the evaluated prefix
    through the graph), and the dirty gate keeps an idle session from copying
    meshes for nothing. Iterating every edit object is what makes a stroke on a
    non-active object — or behind a non-stack active object — reach Grasshopper
    while the mode is open.
    """
    if not engine._dirty:
        # Nothing is waiting for a flush; skip the mode scan on every idle tick.
        return
    for obj in engine._edit_mode_objects():
        if engine.stack_mod.stack_of(obj) is None:
            continue
        if obj.name not in engine._dirty:
            continue
        engine._flush_object(obj)


@engine.bpy.app.handlers.persistent
def _on_frame_change(scene, depsgraph=None):
    # A frame change tags nothing, which is why the frame is part of the prefix
    # revision and this handler exists separately from the depsgraph one.
    engine._mark_dirty_all()
    engine._mark_hot()


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
    engine._rebuild_index()
    for obj in list(engine._stack_objects):
        record = engine.stack_mod.stack_of(obj)
        if not record.stack_id:
            record.stack_id = engine.stack_mod.ensure_stack(obj).stack_id
        for stage in record.stages:
            sent = engine._sent.get((record.stack_id, stage.key))
            engine._set(stage, "geom_hash", sent[1] if sent is not None else "")
    engine._prefix_memo.clear()
    engine._mark_dirty_all()
    engine._mark_hot()


@engine.bpy.app.handlers.persistent
def _on_undo(scene=None):
    engine._after_history_step()


@engine.bpy.app.handlers.persistent
def _on_redo(scene=None):
    engine._after_history_step()


@engine.bpy.app.handlers.persistent
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
    if not engine.bpy.app.timers.is_registered(engine._timer):
        engine.bpy.app.timers.register(engine._timer, first_interval=engine.IDLE_INTERVAL, persistent=True)
    # A file from before the stage scene still has its plumbing in the user's
    # scene: move it now, so the outliner is clean before the first apply.
    engine.helpers.adopt_existing()
    engine._pending.clear()
    engine._sent.clear()
    engine._prefix_memo.clear()
    engine._state_since.clear()
    engine._last_no_send.clear()
    engine._timings.clear()
    # The guard belongs to the session that just ended. A pass can only be
    # running on the main thread, but a load (a new file, a revert) must not
    # inherit the previous file's wedge: with `_evaluating` set the depsgraph
    # handler would drop every edit in the new file until the watchdog fired.
    engine._evaluating = False
    engine._guard_owner = None
    engine._evaluating_since = None
    engine._ticking = False
    for obj in engine.bpy.data.objects:
        record = engine.stack_mod.stack_of(obj)
        if record is None:
            continue
        # A file saved after a duplication but before a pass still carries the
        # copy's shared key: split it before the repair loop rebuilds anything
        # under that key.
        engine._split_duplicate_stages(obj, record)
        for stage in record.stages:
            if stage.state == engine.stack_mod.STATE_WAITING:
                engine._set(stage, "state", engine.stack_mod.STATE_IDLE)
            if stage.geom_hash:
                # We cannot know that GH kept the geometry: ask again in full.
                stage.geom_hash = ""
            # A saved file may meet a document that changed in between: the
            # cache must not answer without asking once (see _verify).
            if not record.stack_id:
                record.stack_id = engine.stack_mod.ensure_stack(obj).stack_id
            engine._verify.add((record.stack_id, stage.key))
            # Plumbing lost with the file (helpers or the node group deleted
            # before the save) is rebuilt here and the stage marked unbound
            # until an answer arrives (plan §9.7). A missing *modifier* is not
            # repaired: deleting it is how a stage is removed (§9.8).
            modifier = engine.nodes.modifier_of(obj, stage)
            if modifier is not None:
                engine._repair_plumbing(obj, record, stage, modifier)
    engine._rebuild_index()
    engine._mark_dirty_all()
    engine._mark_hot()


def _recover_stuck_guard():
    """Clear `_evaluating` when a previous pass left it set, and say so.

    A tick that starts with the guard up cannot belong to that pass: the owner
    has ended. While it is set every update is dropped and nothing marks a
    stack dirty again, so the tick says who left it, clears it, and marks every
    stack dirty to pick up the edits missed meanwhile.
    """
    age = engine.time.monotonic() - engine._evaluating_since if engine._evaluating_since is not None else 0.0
    where = "%s (stage %s)" % engine._guard_owner if engine._guard_owner else "unknown"
    engine._evaluating = False
    # Every depsgraph update the guard swallowed was dropped *before* it could
    # move the generation, so a memo written before the leak still looks
    # current even though the scene moved. Clearing them is what makes the
    # "mark dirty" below read the edited scene instead of replaying the stale
    # prefix (and the old rid) one more time.
    engine._prefix_memo.clear()
    engine._mark_dirty_all()
    line = (
        "guard watchdog: prefix guard left up by %s %.2f s ago; cleared and all stacks marked dirty"
        % (where, age)
    )
    engine._trace.append(line)
    engine.prefs.log("[stageloom] " + line, flush=True)


def _timer():
    """bpy.app.timers callback. Returns the next interval.

    A timer that raises is unregistered by Blender, which would silently stop the
    engine; every part of the pass is therefore guarded, and failures land in the
    panel state instead.
    """
    try:
        engine.stats["passes"] += 1
        engine._ticking = True
        # Before anything else: a guard already up at the start of a tick cannot
        # belong to this pass, so it is the wedge the watchdog exists for.
        if engine._evaluating:
            engine._recover_stuck_guard()
        # Before anything in this pass can evaluate a clone (and mute the
        # handler), let Blender deliver whatever is already tagged. Every
        # trigger the add-on has funnels through here, which is what makes the
        # depsgraph authoritative rather than best-effort.
        #
        # Only pay for it when there is work: a tick with nothing to process
        # must not force an evaluation. Forcing one every idle interval is a
        # main-thread cost the viewport feels as choppiness, and there is
        # nothing a clone could swallow if no clone is going to run.
        dirty_before = bool(engine._dirty)
        # In the GUI Blender's own event loop delivers a tagged update to
        # `_on_depsgraph`; draining with nothing to process only forces an
        # evaluation the viewport feels as choppiness. Background sessions have
        # no such loop (and the tests drive this timer directly), so they keep
        # the unconditional drain that makes a pending edit visible.
        if dirty_before or engine.bpy.app.background:
            engine._drain_depsgraph()
        engine._handle_incoming()
        engine._nudge_stalled_waits()
        if engine._dirty and not dirty_before:
            # Handling a message or the stall scan can produce work after the
            # gate above. Drain before the clone this pass is now about to run.
            engine._drain_depsgraph()
        engine._flush_edit_mode()
        busy = False
        for name in list(engine._dirty):
            obj = engine.bpy.data.objects.get(name)
            if obj is None:
                engine._dirty.discard(name)
                continue
            try:
                if engine._process_object(obj):
                    busy = True
            except Exception as exc:
                engine.bridge.state["error"] = "engine error on %s: %r" % (name, exc)
                import traceback

                engine.prefs.log("[stageloom] engine error:\n%s" % traceback.format_exc(), flush=True)
                # The pass died before it could decide anything for this
                # object: say so in the panel, where the silence is what a user
                # is staring at. One line per consecutive identical failure.
                line = "engine error on %s: %r" % (name, exc)
                if not engine._trace or engine._trace[-1] != line:
                    engine._trace.append(line)
            engine._dirty.discard(name)
        if busy:
            return engine.WAIT_INTERVAL
        if engine.time.monotonic() < engine._hot_until:
            return engine.HOT_INTERVAL
        return engine.IDLE_INTERVAL
    except Exception as exc:
        # The last-resort handler must not raise itself: Blender unregisters a
        # timer whose callback raises, and then the engine is gone for the rest
        # of the session. `prefs.log` is already print-safe; this guard also
        # covers the state write and anything a future edit adds here.
        try:
            engine.bridge.state["error"] = "engine error: %r" % (exc,)
            import traceback

            engine.prefs.log("[stageloom] engine error:\n%s" % traceback.format_exc(), flush=True)
        except Exception:
            pass
        return engine.IDLE_INTERVAL
    finally:
        engine._ticking = False


def _nudge_stalled_waits():
    """Mark a stack object dirty when a stage has waited past the soft retry.

    A waiting stage dirties nothing on its own, and the pass is what supersedes
    a stale request with the current state (or resends at the hard timeout);
    without the nudge a supersede would wait for an unrelated edit. The scan
    walks the scene's stack objects — not names captured at send time, which a
    rename invalidates — and runs at most once a second.
    """
    now = engine.time.monotonic()
    if now - engine._last_stall_check < 1.0:
        return
    engine._last_stall_check = now
    waiting = set()
    for obj in engine._all_stack_objects():
        record = engine.stack_mod.stack_of(obj)
        for stage in record.stages:
            pending = engine._pending.get((record.stack_id, stage.key))
            if not pending:
                continue
            waiting.add(obj.name)
            if now - min(pending.values()) > engine.SOFT_RETRY_SECONDS:
                engine._dirty.add(obj.name)
                engine._mark_hot()
    engine._waiting_objects.clear()
    engine._waiting_objects.update(waiting)

