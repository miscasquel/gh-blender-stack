"""Playback gate: Play Every Frame waits for each stage.

While playback is armed, ``frame_change_post`` runs the normal stage pass
synchronously, so the playhead cannot advance before the frame is applied.
Frame Drop keeps the asynchronous latest-wins path. The engine timer is
suppressed while the gate owns the frame.
"""

import queue
import sys
import time

engine = sys.modules[__package__]

# Last failure reported per stage, so a disconnected client does not print a
# line for every frame of a playback. Cleared when the stage succeeds and at
# the start/end of a synced playback.
_reported = {}

# A finite backstop for the default "no timeout" preference. Ten minutes is
# far longer than any interactive solve, and short enough that a wedged client
# cannot pin the playhead forever. Tests and embedded callers assign it; a
# `playback_hard_cap` preference may override it when one exists.
PLAYBACK_HARD_CAP_SECONDS = 600.0


def _report(stage, text, error=False):
    """Record one playback failure once per stage, not once per frame."""
    engine.stats["playback_failures"] += 1
    if error:
        engine._set(stage, "state", engine.stack_mod.STATE_ERROR)
        engine._set(stage, "error", text)
    if _reported.get(stage.key) == text:
        return
    _reported[stage.key] = text
    mode = "bake" if engine._bake_sync else "playback"
    line = "%s: %s: %s" % (mode, stage.key, text)
    if not engine._trace or engine._trace[-1] != line:
        engine._trace.append(line)
    engine.prefs.log("[stageloom] " + line, flush=True)


def _clear_report(stage):
    _reported.pop(stage.key, None)


def _playback_hard_cap():
    """The finite cap that applies even when the preference says "no timeout"."""
    try:
        cap = float(engine.prefs._pref("playback_hard_cap", PLAYBACK_HARD_CAP_SECONDS))
    except (TypeError, ValueError):
        cap = PLAYBACK_HARD_CAP_SECONDS
    return cap if cap > 0.0 else PLAYBACK_HARD_CAP_SECONDS


def _playback_deadline():
    """When the current stage wait must give up.

    The preference default is 0: Play Every Frame and a stage bake wait for the
    answer or a disconnect, which is what "play every frame" means for an
    external solver. A positive value caps a single stage's wait and lets the
    frame continue with the last output. Either way a finite hard cap applies,
    so a superseding hello that is never delivered and a client that never
    answers both end in a reported timeout rather than a pinned main thread.
    """
    try:
        timeout = float(engine.prefs._pref("playback_timeout", 0.0))
    except (TypeError, ValueError):
        timeout = 0.0
    cap = _playback_hard_cap()
    if timeout <= 0.0 or timeout > cap:
        timeout = cap
    return time.monotonic() + timeout


def _client_token():
    """What identifies the client session a wait belongs to."""
    state = engine.bridge.state
    return (state.get("connected_at"), state.get("doc"), state.get("clients"))


def _superseded(record, stage, rid, token):
    """Why the wait for ``rid`` no longer describes the current work, or None.

    The pending map alone is not the signal: an unsolicited answer for another
    frame can empty it. The awaited rid is. A reconnect/hello changes the
    session and clears the wait; the playhead must move on with the last output
    instead of reading an answer the new client was never asked for.
    """
    state = engine.bridge.state
    if not state["clients"]:
        return "Grasshopper disconnected; keeping the last output"
    if _client_token() != token:
        return "the Grasshopper session changed; keeping the last output"
    pending = engine._pending.get((record.stack_id, stage.key)) or {}
    if not rid or stage.rid != rid:
        return "the request was superseded; keeping the last output"
    if rid not in pending:
        return "the request is no longer pending; keeping the last output"
    if stage.state != engine.stack_mod.STATE_WAITING:
        return "the stage left waiting; keeping the last output"
    return None


def _scene_stacks(scene):
    """The stack objects the playback gate is responsible for in this scene."""
    return [obj for obj in engine._all_stack_objects() if obj.name in scene.objects]


def _dispatch(item):
    """Apply one queued message with the same isolation as the timer drain.

    Returns the failure, if any, so a playback wait can stop on the failure of
    its own answer instead of trying the same broken answer again.
    """
    try:
        engine._handle_message(*item)
    except Exception as exc:
        try:
            engine._report_message_failure(item[0], exc)
        except Exception:
            engine._trace.append("incoming message failed and could not be reported")
        return exc
    return None


def _is_answer(item, stage, rid):
    """Whether one incoming message is this stage's answer for this frame."""
    header = item[0] if item else None
    if not isinstance(header, dict) or not rid:
        return False
    if header.get("stage") != stage.key or header.get("rid") != rid:
        return False
    return header.get("type") in ("response", "error")


def _pump_until_settled(record, stage, rid, deadline):
    """Wait for the stage's answer to *this* request.

    Returns True when an answer for ``rid`` has been handled, False when the
    frame cannot be made ready: timeout, protocol error, a disconnected client,
    or a wait the scene has superseded. It deliberately does not use the pending
    map as the completion signal: an unsolicited answer for another frame clears
    it, and returning then made the gate ask a second time for the same frame.

    The awaited rid and the client session are re-checked after every dispatch.
    A hello, reconnect, document replacement, a newer request or a stage that
    left `waiting` makes this wait obsolete; returning keeps the last output and
    lets the playhead move instead of blocking the main thread on an answer that
    can no longer arrive.
    """
    started = time.monotonic()
    token = _client_token()
    while True:
        reason = _superseded(record, stage, rid, token)
        if reason:
            _report(stage, reason)
            return False
        if deadline is None:
            wait = 0.05
        else:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                engine._forget(record.stack_id, stage.key)
                _report(
                    stage,
                    "no answer within %.1f s; keeping the last output"
                    % (time.monotonic() - started),
                    error=True,
                )
                return False
            wait = min(0.05, remaining)
        try:
            item = engine.bridge.INCOMING.get(timeout=wait)
        except queue.Empty:
            continue
        answered = _is_answer(item, stage, rid)
        failed = _dispatch(item)
        if answered:
            if failed is not None:
                _report(
                    stage,
                    "the answer failed: %r; keeping the last output" % (failed,),
                    error=True,
                )
                return False
            return True


def _sync_stage(obj, record, stage, deadline):
    """Make one stage ready for the current playback frame."""
    # A request left in flight before playback began belongs to the session
    # before this frame. Its answer is dropped if it supersedes.
    engine._forget(record.stack_id, stage.key)
    while True:
        outcome = engine._process_stage(obj, record, stage)
        if outcome == "idle":
            if not stage.has_stage_out:
                # One-way stage: the pass delivered its prefix to Stage In and
                # there is nothing to wait for. Never let it block playback.
                _clear_report(stage)
                return True
            if stage.state == engine.stack_mod.STATE_DISCONNECTED:
                _report(stage, "Grasshopper disconnected; keeping the last output")
                return False
            if stage.state == engine.stack_mod.STATE_UNBOUND:
                _report(stage, "no Grasshopper document answered this stage")
                return False
            # Disabled, frozen or already on screen: nothing to wait for.
            _clear_report(stage)
            return True
        if outcome == "busy":
            _clear_report(stage)
            return True
        # "waiting": a request went out. Wait for *that* rid, not the pending
        # map: a stale answer for another frame clears it too.
        rid = stage.rid
        if not rid:
            _report(stage, "the request has no id", error=True)
            return False
        if not _pump_until_settled(record, stage, rid, deadline):
            # The wait can end because the stage left `waiting` with a visible
            # error (a protocol error for this request); report that too, so a
            # failed frame is not only a silent counter.
            if stage.state == engine.stack_mod.STATE_ERROR and stage.error:
                _report(stage, stage.error)
            return False
        if stage.state == engine.stack_mod.STATE_ERROR:
            _report(stage, stage.error or "Grasshopper error")
            return False
        if stage.state in (engine.stack_mod.STATE_WAITING, engine.stack_mod.STATE_IDLE):
            # A stale answer won the pending race (waiting), or Grasshopper
            # asked for the geometry again (unknown_ref): ask for this frame
            # once more, still inside the same deadline.
            continue
        _clear_report(stage)
        return True


def _flush_playback_edits(scene):
    """Flush Edit Mode data before the gate reads any prefix.

    The timer path flushes its dirty objects; a playback/bake frame does not
    pass through that path, and the prefix clone would otherwise read the
    pre-edit cage. Only objects actually in Edit Mode pay for this.
    """
    for obj in engine._edit_mode_objects():
        if obj.name not in scene.objects:
            continue
        if engine.stack_mod.stack_of(obj) is None:
            continue
        engine._flush_object(obj)


def _report_stage_failure(stage, exc):
    """Turn one stage's exception into its error row, never the frame's death."""
    message = "stage pass failed: %r" % (exc,)
    try:
        engine.bridge.state["error"] = message
        import traceback

        engine.prefs.log("[stageloom] " + message + "\n" + traceback.format_exc(), flush=True)
        _report(stage, message, error=True)
    except Exception:
        # Reporting is the last line of defence: a stage whose record cannot be
        # written must not cost the next stage its pass.
        engine._trace.append(message)


def _sync_playback_frame(scene):
    """Run one synchronous pass for the current playback frame."""
    stacks = _scene_stacks(scene)
    if not stacks:
        return True
    engine._handle_incoming()
    engine._flush_playback_edits(scene)
    engine.stats["playback_frames"] += 1
    ready = True
    for obj in stacks:
        record = engine.stack_mod.stack_of(obj)
        if record is None or not record.stages:
            continue
        if not record.stack_id:
            record.stack_id = engine.stack_mod.ensure_stack(obj).stack_id
        engine._drop_removed_stages(obj, record)
        engine._split_duplicate_stages(obj, record)
        # One depsgraph evaluation serves every stage prefix on this object,
        # the same batching the timer pass uses.
        engine._prefix_batch.clear()
        engine._prefix_batch_built = False
        engine._prefix_batching = True
        try:
            for stage in engine._ordered_stages(obj, record):
                try:
                    stage_ready = _sync_stage(obj, record, stage, _playback_deadline())
                except Exception as exc:
                    # A raise in one stage's pass is that stage's error. It must
                    # not escape a frame handler while `_playback_sync` is armed
                    # and leave the rest of the chain unprocessed. The failed
                    # pass will not wait for anything it asked for.
                    engine._forget(record.stack_id, stage.key)
                    engine._clear_pull((record.stack_id, stage.key))
                    _report_stage_failure(stage, exc)
                    stage_ready = False
                if not stage_ready:
                    ready = False
        finally:
            engine._prefix_batching = False
            engine._prefix_batch.clear()
            engine._prefix_batch_built = False
    if ready:
        engine.stats["playback_synced"] += 1
    return ready


@engine.bpy.app.handlers.persistent
def _on_playback_pre(scene, depsgraph=None):
    """Arm the gate when Blender starts playback in Play Every Frame."""
    _reported.clear()
    for counter in ("playback_frames", "playback_synced", "playback_failures"):
        engine.stats[counter] = 0
    engine._playback_sync = bool(_scene_stacks(scene)) and scene.sync_mode == "NONE"
    if engine._playback_sync:
        engine._trace.append("playback sync: Play Every Frame waits for Grasshopper")
        engine.prefs.log(
            "[stageloom] playback sync: Play Every Frame waits for Grasshopper",
            flush=True,
        )


@engine.bpy.app.handlers.persistent
def _on_playback_post(scene, depsgraph=None):
    """Disarm the gate when playback stops."""
    was_synced = engine._playback_sync
    engine._playback_sync = False
    _reported.clear()
    if was_synced:
        stats = engine.stats
        line = "playback sync: %d frame(s), %d ready, %d failure(s)" % (
            stats["playback_frames"],
            stats["playback_synced"],
            stats["playback_failures"],
        )
        engine._trace.append(line)
        engine.prefs.log("[stageloom] " + line, flush=True)


def _update_bake_progress(scene):
    """Move Blender's cursor progress number with the baked frame."""
    progress = engine._bake_progress
    if progress is None:
        return
    wm, start, total = progress
    wm.progress_update(max(0, min(total, scene.frame_current - start + 1)))


def _run_gate(scene):
    """Call the gate without letting a structural failure escape the handler."""
    try:
        _sync_playback_frame(scene)
    except Exception as exc:
        try:
            message = "playback gate failed: %r" % (exc,)
            engine.bridge.state["error"] = message
            import traceback

            engine.prefs.log("[stageloom] " + message + "\n" + traceback.format_exc(), flush=True)
            if not engine._trace or engine._trace[-1] != message:
                engine._trace.append(message)
        except Exception:
            pass


@engine.bpy.app.handlers.persistent
def _on_frame_change_sync(scene, depsgraph=None):
    """One gated frame: block until the current frame's stages are ready."""
    if engine._bake_sync:
        # A native Bake job steps frames and fires this handler. The stage
        # outputs it stores must be the ones for *this* frame.
        _update_bake_progress(scene)
        _run_gate(scene)
        return
    if not engine._playback_sync:
        return
    if scene.sync_mode != "NONE":
        # The mode changed while playing; hand the frame back to the timer.
        engine._playback_sync = False
        return
    _run_gate(scene)
