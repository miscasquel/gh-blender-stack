"""Playback frame gate: Play Every Frame waits for Stageloom.

Blender's playback has two behaviours (`Scene.sync_mode`):

* ``NONE`` — Play Every Frame: the playhead advances one frame at a time and
  waits for the dependency graph. A Grasshopper stage is outside that graph,
  so playback used to advance without waiting for its answer.
* ``FRAME_DROP`` — Frame Drop: playback keeps real time and shows whatever the
  dependency graph had time to compute. Stageloom keeps its existing
  latest-wins, skip-what-you-cannot-do behaviour here.

This module makes ``NONE`` respect the Grasshopper stage: ``frame_change_post``
runs after the frame's dependency graph evaluation, asks for the current
frame's output, and blocks on the socket queue until it has been applied. The
playback timer cannot advance until the handler returns, so the playhead waits
exactly like it waits for a slow native modifier. ``FRAME_DROP`` is untouched.

The gate is the normal engine pass made synchronous; it uses the same prefix,
request IDs, cache and apply path as the timer. The engine timer is suppressed
while ``_playback_sync`` is set, so an asynchronous pass for the live playhead
cannot race the gate.
"""

import queue
import sys
import time

engine = sys.modules[__package__]

# Last failure reported per stage, so a disconnected client does not print a
# line for every frame of a playback. Cleared when the stage succeeds and at
# the start/end of a synced playback.
_reported = {}


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


def _playback_deadline():
    """When the current stage wait must give up, or None to wait indefinitely.

    The preference default is 0: Play Every Frame and a stage bake wait until
    the answer or a disconnect, which is what "play every frame" means for an
    external solver. A positive value caps a single stage's wait and lets the
    frame continue with the last output.
    """
    try:
        timeout = float(engine.prefs._pref("playback_timeout", 0.0))
    except (TypeError, ValueError):
        timeout = 0.0
    return time.monotonic() + timeout if timeout > 0.0 else None


def _scene_stacks(scene):
    """The stack objects the playback gate is responsible for in this scene."""
    return [obj for obj in engine._all_stack_objects() if obj.name in scene.objects]


def _dispatch(item):
    """Apply one queued message with the same isolation as the timer drain."""
    try:
        engine._handle_message(*item)
    except Exception as exc:
        try:
            engine._report_message_failure(item[0], exc)
        except Exception:
            engine._trace.append("incoming message failed and could not be reported")


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
    frame cannot be made ready: timeout, protocol error, or a disconnected
    client. It deliberately does not use the pending map as the completion
    signal: an unsolicited answer for another frame clears it, and returning
    then made the gate ask a second time for the same frame.
    """
    answered = False
    while not answered:
        if deadline is None:
            wait = 0.05
        else:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                engine._forget(record.stack_id, stage.key)
                try:
                    timeout = float(engine.prefs._pref("playback_timeout", 0.0))
                except (TypeError, ValueError):
                    timeout = 0.0
                _report(
                    stage,
                    "no answer within %.1f s; keeping the last output" % timeout,
                    error=True,
                )
                return False
            wait = min(0.05, remaining)
        if not engine.bridge.state["clients"]:
            engine._forget(record.stack_id, stage.key)
            _report(stage, "Grasshopper disconnected; keeping the last output")
            return False
        try:
            item = engine.bridge.INCOMING.get(timeout=wait)
        except queue.Empty:
            continue
        if _is_answer(item, stage, rid):
            answered = True
        _dispatch(item)
        # Handle anything else already queued before deciding the frame
        # settled, and keep looking for our own answer in that batch.
        while True:
            try:
                item = engine.bridge.INCOMING.get_nowait()
            except queue.Empty:
                break
            if _is_answer(item, stage, rid):
                answered = True
            _dispatch(item)
    return True


def _sync_stage(obj, record, stage, deadline):
    """Make one stage ready for the current playback frame."""
    # A request left in flight before playback began belongs to the live
    # session, not to this frame. Its answer is cached when it lands.
    engine._forget(record.stack_id, stage.key)
    while True:
        outcome = engine._process_stage(obj, record, stage)
        if outcome == "idle":
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


def _sync_playback_frame(scene):
    """Run one synchronous pass for the current playback frame."""
    stacks = _scene_stacks(scene)
    if not stacks:
        return True
    engine._handle_incoming()
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
                if not _sync_stage(obj, record, stage, _playback_deadline()):
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


@engine.bpy.app.handlers.persistent
def _on_frame_change_sync(scene, depsgraph=None):
    """One gated frame: block until the current frame's stages are ready."""
    if engine._bake_sync:
        # A native Bake job steps frames and fires this handler. The stage
        # outputs it caches must be the ones for *this* frame.
        _sync_playback_frame(scene)
        return
    if not engine._playback_sync:
        return
    if scene.sync_mode != "NONE":
        # The mode changed while playing; hand the frame back to the timer.
        engine._playback_sync = False
        return
    _sync_playback_frame(scene)
