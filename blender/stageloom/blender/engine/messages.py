"""Incoming protocol messages: schema, responses, errors and hello."""

import sys

engine = sys.modules[__package__]


def _handle_incoming():
    """Apply every queued message, isolating one bad frame from the rest.

    `drain_incoming()` empties the queue into a list, so without a per-message
    guard a raise in one handler aborted the pass and every message behind it
    was lost.
    """
    for header, sections in engine.bridge.drain_incoming():
        try:
            engine._handle_message(header, sections)
        except Exception as exc:
            try:
                engine._report_message_failure(header, exc)
            except Exception:
                # The reporter is the last thing between one bad message and
                # the next one: if it fails too, the rest of the batch still
                # matters more than the report.
                engine._trace.append("incoming message failed and could not be reported")


def _handle_message(header, sections):
    mtype = header.get("type")
    if mtype == "schema":
        engine._handle_schema(header)
    elif mtype == "response":
        engine._handle_response(header, sections)
    elif mtype == "error":
        engine._handle_error(header)
    elif mtype == "hello":
        engine._on_client_hello(header)


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
    engine.bridge.state["error"] = message
    if not engine._trace or engine._trace[-1] != message:
        engine._trace.append(message)
    import traceback

    engine.prefs.log("[stageloom] incoming %s failed:\n%s" % (where, traceback.format_exc()), flush=True)
    if mtype == "response" and stage_key:
        try:
            engine._clear_failed_response(stage_key)
        except Exception as recovery:
            # Best-effort, like the cleanup: a stage that cannot be recovered
            # must not cost the *other* messages their chance to be handled.
            engine.prefs.log(
                "[stageloom] could not recover the failed response for %s: %r"
                % (stage_key, recovery),
                flush=True,
            )


def _clear_failed_response(stage_key):
    """Put the stage of a failed response back in a retryable state.

    The failed answer consumed its pending entry, so arm the same permit a
    reconnect uses: the next pass must ask again instead of treating the
    unchanged request id as already delivered. Pending is cleared
    (idempotent) and the state leaves `waiting`, where an empty pending map
    would look like an answer still on its way.
    """
    for obj, record, stage in engine._stage_views(stage_key):
        engine._forget(record.stack_id, stage.key)
        engine._verify.add((record.stack_id, stage.key))
        engine._set(
            stage,
            "state",
            engine.stack_mod.STATE_FROZEN if stage.frozen else engine.stack_mod.STATE_IDLE,
        )
        engine._dirty.add(obj.name)
        engine._mark_hot()


def _on_client_hello(header):
    """A GH client connected (or reconnected).

    A request sent while no client was connected is lost, and a stage left
    waiting on it would sit until its timeout. Reset waiting stages and mark
    every stack dirty so the current request goes out again (protocol §8.8).

    Every claimed stage forgets its geometry hash: a `hello` is a new client
    session, so the geometry it holds is unknown, and protocol §8.8 wants the
    next request in full. The stage is also put on `_verify`: a fresh connection
    is the one moment the document may have changed without Blender hearing
    about it, so the next pass must ask even when its request id and frame have
    not moved.

    A stage entry may also publish `has_stage_in` and `has_stage_out`. False
    `has_stage_in` turns the stage into a generator (no Stage In, so no prefix).
    False `has_stage_out` makes it one-way: the prefix is still delivered to its
    Stage In, but nothing answers, so the stage must not wait or apply. Absent
    fields mean an older client whose claims are unknown, and the saved modes
    stay.
    """
    doc = (header or {}).get("doc") or ""
    claims = {}
    for entry in (header or {}).get("stages") or []:
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        if key:
            claims[key] = entry
    for obj in engine._all_stack_objects():
        record = engine.stack_mod.stack_of(obj)
        if not record.stack_id:
            record.stack_id = engine.stack_mod.ensure_stack(obj).stack_id
        for stage in record.stages:
            if stage.key in engine._stage_conflicts:
                # An ambiguous key has no owner to adopt a claim for, and no
                # request may be verified or sent until the user resolves it.
                continue
            entry = claims.get(stage.key)
            if entry is not None:
                _apply_input_mode(stage, entry)
                _apply_output_presence(stage, entry)
            if stage.state == engine.stack_mod.STATE_WAITING:
                engine._forget(record.stack_id, stage.key)
                engine._set(stage, "state", engine.stack_mod.STATE_IDLE)
            engine._set(stage, "geom_hash", "")
            engine._sent.pop((record.stack_id, stage.key), None)
            engine._last_frame.pop((record.stack_id, stage.key), None)
            engine._verify.add((record.stack_id, stage.key))
        engine._dirty.add(obj.name)
    engine._trace.append("client hello: %s" % (doc or "unnamed"))
    engine._mark_hot()


def _apply_input_mode(stage, entry):
    """Adopt one hello entry's `has_stage_in` claim, if it carries one.

    A stage whose GH definition has no Stage In is a generator: the Blender
    prefix is not part of its request, so its request id depends only on its
    params and nonce. An absent or non-boolean field is "unknown" and must not
    overwrite the mode saved with the file (old clients keep working).
    """
    has_stage_in = entry.get("has_stage_in")
    if not isinstance(has_stage_in, bool):
        return False
    mode = (
        engine.stack_mod.INPUT_PREFIX
        if has_stage_in
        else engine.stack_mod.INPUT_GENERATOR
    )
    if engine._set(stage, "input_mode", mode):
        engine._trace.append("%s: input mode %s" % (stage.key, mode))
        return True
    return False


def _apply_output_presence(stage, entry):
    """Adopt one hello entry's `has_stage_out` claim, if it carries one.

    No Stage Out means nothing will answer this stage's requests. It stays
    useful as a one-way consumer (a Stage In still receives the prefix), but
    Blender must never wait on it. An absent or non-boolean field is "unknown",
    so an old plugin keeps the saved two-way behavior.
    """
    has_stage_out = entry.get("has_stage_out")
    if not isinstance(has_stage_out, bool):
        return False
    if engine._set(stage, "has_stage_out", has_stage_out):
        engine._trace.append(
            "%s: Stage Out %s" % (stage.key, "present" if has_stage_out else "missing")
        )
        return True
    return False


def _handle_schema(header):
    key = header.get("stage")
    if engine._stage_key_conflicted(key):
        engine.prefs.log(
            "[stageloom] schema for duplicate stage key %r ignored" % (key,),
            flush=True,
        )
        return
    params = header.get("params") or []
    matched = False
    for obj, record, stage in engine._stage_views(key):
        matched = True
        if not record.stack_id:
            record.stack_id = engine.stack_mod.ensure_stack(obj).stack_id
        try:
            # The plugin re-publishes on a heartbeat; only touch the UI when
            # the schema actually changed.
            if engine.props.schema_signature(stage) == engine.props.incoming_signature(params):
                continue
            engine.props.apply_schema(obj, stage, params)
        except Exception as exc:
            # One bad schema must not take the engine down.
            engine.bridge.state["error"] = "schema for %s failed: %r" % (key, exc)
            import traceback

            engine.prefs.log("[stageloom] schema error:\n%s" % traceback.format_exc(), flush=True)
            continue
        # A changed schema is a changed definition: ask for a new solve even if
        # the request id and frame have not moved.
        engine._verify.add((record.stack_id, stage.key))
        engine._dirty.add(obj.name)
        obj.update_tag()
    if not matched:
        # A schema for a stage no object has: usually a key pasted into GH that
        # does not belong to any stack, or a stage removed in Blender.
        engine.prefs.log("[stageloom] schema for unknown stage %r ignored" % (key,), flush=True)


def _answer_pending(record, stage, rid):
    """Consume the pending entry one answer names, if it is still the one held.

    Returns the monotonic send time, or None. Only the named rid is removed: a
    stale answer for an older request must not clear a newer pending request
    (the error path has always had this rule; the ok path now matches it).
    """
    pending = engine._pending.get((record.stack_id, stage.key))
    if not pending:
        return None
    sent_at = pending.pop(rid, None)
    if not pending:
        engine._pending.pop((record.stack_id, stage.key), None)
    return sent_at


def _handle_response(header, sections):
    key = header.get("stage")
    rid = header.get("rid")
    status = header.get("status")
    if not isinstance(key, str) or not key:
        engine.prefs.log("[stageloom] response without a stage key ignored", flush=True)
        return
    if engine._stage_key_conflicted(key):
        engine.prefs.log(
            "[stageloom] response for duplicate stage key %r ignored" % (key,),
            flush=True,
        )
        return
    if not isinstance(rid, str) or not rid:
        # A Stage Out solve that was triggered by another stage has no request
        # to answer. It must not be applied as an empty success.
        engine.prefs.log(
            "[stageloom] response for %r without a request id ignored" % (key,),
            flush=True,
        )
        return
    if status not in ("ok", "error"):
        engine.prefs.log(
            "[stageloom] response for %r has invalid status %r ignored"
            % (key, status),
            flush=True,
        )
        return
    if status == "ok":
        out_hash = header.get("out_hash")
        if not isinstance(out_hash, str) or not out_hash:
            engine.prefs.log(
                "[stageloom] ok response for %r has no output hash ignored" % (key,),
                flush=True,
            )
            return

    for obj, record, stage in engine._stage_views(key):
        if not record.stack_id:
            record.stack_id = engine.stack_mod.ensure_stack(obj).stack_id

        if not stage.enabled:
            engine._forget(record.stack_id, stage.key)
            continue

        pending_key = (record.stack_id, stage.key)
        pending = engine._pending.get(pending_key) or {}
        was_pending = rid in pending
        if not stage.has_stage_out:
            # No Stage Out answers this stage: a response left over from when it
            # had one is not a sync to apply. Consume only its own pending entry
            # if the mode change raced the answer.
            if was_pending:
                _answer_pending(record, stage, rid)
            continue
        if header.get("status") == "error" and not was_pending:
            # An error answers a request we are waiting for. If this rid is not
            # pending, the error is stale or unsolicited — often a document-wide
            # solve answering with the stage's last rid. Acting on it could
            # abandon a newer request.
            continue

        if header.get("status") == "error":
            error = header.get("error") or {}
            code = error.get("code") if isinstance(error, dict) else None
            if code == "unknown_ref":
                # GH lost the geometry a ref named (reconnect, restarted
                # document): send it again in full (protocol §4.3). The error is
                # not a result.
                engine._resend_in_full(obj, record, stage, "unknown_ref response")
                continue
            if was_pending:
                # Remove only this request. A different pending entry belongs
                # to a newer request and must survive a late error.
                _answer_pending(record, stage, rid)
            if not stage.frozen:
                engine._set(stage, "state", engine.stack_mod.STATE_ERROR)
            engine._set(stage, "error", error.get("message") if isinstance(error, dict) else str(error))
            # Deliberately no retry here: a solve that just failed would fail
            # again the moment it was asked, turning one broken component into
            # a request loop against the client. Protocol §7: an errored stage
            # retries on the next change or Push; `_set` above has already
            # surfaced the state, and the change that fixes the graph marks the
            # object dirty like any other.
            continue

        out_hash = header.get("out_hash") or ""
        if rid != stage.rid:
            # The answer belongs to a request the scene has superseded. It is
            # not the solver state the user is looking at, and there is no frame
            # store to replay it from.
            if was_pending:
                _answer_pending(record, stage, rid)
            engine._trace.append(
                "%s: dropped stale answer %s" % (stage.key, rid[-8:])
            )
            continue

        # Consume only the request this answer names: a stale ok must not clear
        # a newer pending request (the rule the error path has always had).
        sent_at = _answer_pending(record, stage, rid) if was_pending else None
        if sent_at is not None:
            engine._record_timing(record, stage, "rtt_ms", engine._timed_since(sent_at))
        applied = False
        # A frozen stage is parked with rid="" and must ignore unsolicited
        # answers. Pull deliberately sends one request while frozen; its answer
        # carries the stage's current rid, so rid equality is the permission.
        if out_hash != stage.applied_out_hash or engine._payload_missing(obj, stage, sections):
            started = engine.time.monotonic()
            engine._apply(obj, stage, sections, header.get("attributes"))
            engine._record_timing(record, stage, "apply_ms", engine._timed_since(started))
            engine.stats["applies"] += 1
            engine._set(stage, "applied_out_hash", out_hash)
            applied = True
            engine._trace.append("%s: applied %s" % (stage.key, (out_hash or "")[-8:]))
        else:
            # The displayed answer is already this one, but the node group may
            # predate the current format (a group written by an older build, or
            # a session that has not applied since nesting landed). Rebuilding
            # is cheap and is the only way a same-hash answer can repair it.
            engine.nodes.ensure_group(obj, stage.key, engine._nested_rows_of(sections))
        engine._set(stage, "warnings", engine._warnings_text(header))
        # An unchanged output is still the solver's current state: it settles
        # normally, and a frozen stage stays frozen because freeze owns state.
        engine._set(stage, "state", engine._rest_state(stage))
        engine._set(stage, "error", "")
        if applied:
            engine.stats["tags"] += 1
            obj.update_tag()
        if applied or was_pending:
            engine._dirty.add(obj.name)
            engine._mark_hot()


def _handle_error(header):
    """A standalone `error` message (protocol §4.6).

    `unknown_ref` is the one that needs action: Grasshopper does not hold the
    geometry the last request named. Everything else is reported on the stages it
    names so a protocol mismatch is not silent.
    """
    code = header.get("code")
    message = header.get("message") or code or "protocol error"
    if engine._stage_key_conflicted(header.get("stage")):
        engine.prefs.log(
            "[stageloom] error for duplicate stage key %r ignored"
            % (header.get("stage"),),
            flush=True,
        )
        return
    if code == "unknown_ref":
        matched = False
        for obj, record, stage in engine._stage_views(header.get("stage")):
            matched = True
            engine._resend_in_full(obj, record, stage, "unknown_ref")
        if not matched:
            engine.prefs.log("[stageloom] unknown_ref for unknown stage %r" % (header.get("stage"),), flush=True)
        return

    engine.prefs.log("[stageloom] protocol error %s: %s" % (code, message), flush=True)
    for obj, record, stage in engine._stage_views(header.get("stage")):
        # The request this error answers is over: without this the stage would
        # sit `waiting` for the full timeout for an answer that is already here.
        # The error is left on display and the stage is not dirtied — retrying
        # here would ask a component that just failed, in a loop; the next
        # change or Push retries (protocol §7).
        engine._forget(record.stack_id, stage.key)
        engine._set(stage, "error", "%s: %s" % (code, message))
        if stage.state == engine.stack_mod.STATE_WAITING:
            engine._set(stage, "state", engine.stack_mod.STATE_ERROR)


def _resend_in_full(obj, record, stage, reason):
    """Grasshopper no longer holds this stage's geometry, so the next request
    must carry it. Shared by the `unknown_ref` error message, the response-shaped
    `unknown_ref` and the request timeout, because all three leave the same
    uncertainty behind (protocol §4.3, §8.8)."""
    engine._forget(record.stack_id, stage.key)
    engine._sent.pop((record.stack_id, stage.key), None)
    # Ask even if the request id and frame have not moved: the whole point of
    # the resend is to tell Grasshopper again.
    engine._verify.add((record.stack_id, stage.key))
    engine._set(stage, "geom_hash", "")
    engine._set(stage, "state", engine.stack_mod.STATE_IDLE)
    engine._set(stage, "error", "")
    engine._dirty.add(obj.name)
    engine.prefs.log("[stageloom] %s: resending geometry (%s)" % (stage.key, reason), flush=True)

