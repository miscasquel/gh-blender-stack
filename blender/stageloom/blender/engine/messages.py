"""Incoming protocol messages: schema, responses, errors and hello."""

import sys

engine = sys.modules[__package__]


def _handle_incoming():
    """Apply every queued message, isolating one bad frame from the rest.

    `drain_incoming()` empties the queue into a list, so without a per-message
    guard a raise in one handler aborted the pass and every message behind it
    was lost.
    """
    for header, sections, raw in engine.bridge.drain_incoming():
        try:
            engine._handle_message(header, sections, raw)
        except Exception as exc:
            try:
                engine._report_message_failure(header, exc)
            except Exception:
                # The reporter is the last thing between one bad message and
                # the next one: if it fails too, the rest of the batch still
                # matters more than the report.
                engine._trace.append("incoming message failed and could not be reported")


def _handle_message(header, sections, raw=None):
    mtype = header.get("type")
    if mtype == "schema":
        engine._handle_schema(header)
    elif mtype == "response":
        engine._handle_response(header, sections, raw)
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
            engine._clear_failed_response(stage_key, header.get("rid"))
        except Exception as recovery:
            # Best-effort, like the cleanup: a stage that cannot be recovered
            # must not cost the *other* messages their chance to be handled.
            engine.prefs.log(
                "[stageloom] could not recover the failed response for %s: %r"
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
    for obj, record, stage in engine._stage_views(stage_key):
        engine._forget(record.stack_id, stage.key)
        if rid:
            engine._forget_frame(record, stage, rid)
        if stage.live:
            # A live stage has no frame-cache miss to drive its retry, and the
            # failed answer consumed its pending entry: arm the same permit a
            # reconnect uses so the next pass asks again.
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
    A reconnect with a *different* document resets the stages it claims: frames
    cached against the old document must not replay.

    Every claimed stage also forgets its geometry hash: a `hello` is a new client
    session, so the geometry it holds is unknown, and protocol §8.8 wants the
    next request in full. The stage is also put on `_verify`: a fresh connection
    is the one moment the document may have changed without Blender hearing
    about it, so the next pass must ask instead of replaying a cached frame.

    A stage entry may also publish `has_stage_in` and `has_stage_out`. False
    `has_stage_in` turns the stage into a generator (no Stage In, so no prefix).
    False `has_stage_out` makes it one-way: the prefix is still delivered to its
    Stage In, but nothing answers, so the stage must not wait, apply or cache.
    Absent fields mean an older client whose claims are unknown, and the saved
    modes stay.
    """
    doc = (header or {}).get("doc") or ""
    claims = {}
    for entry in (header or {}).get("stages") or []:
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        if key:
            claims[key] = entry
    claimed = set(claims)
    for obj in engine._all_stack_objects():
        record = engine.stack_mod.stack_of(obj)
        if not record.stack_id:
            record.stack_id = engine.stack_mod.ensure_stack(obj).stack_id
        if doc and claimed and any(stage.key in claimed for stage in record.stages):
            if record.connected_doc and record.connected_doc != doc:
                for stage in record.stages:
                    if stage.key in claimed:
                        engine._bump_generation(obj, record, stage, "document changed")
            if record.connected_doc != doc:
                record.connected_doc = doc
        for stage in record.stages:
            entry = claims.get(stage.key)
            if entry is not None:
                _apply_input_mode(stage, entry)
                _apply_output_presence(stage, entry)
            if stage.state == engine.stack_mod.STATE_WAITING:
                engine._forget(record.stack_id, stage.key)
                engine._set(stage, "state", engine.stack_mod.STATE_IDLE)
            engine._set(stage, "geom_hash", "")
            engine._sent.pop((record.stack_id, stage.key), None)
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
        # A changed schema is a changed definition: frames cached against
        # the old one must not replay.
        engine._bump_generation(obj, record, stage, "schema changed")
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


def _handle_response(header, sections, raw=None):
    key = header.get("stage")
    rid = header.get("rid")
    status = header.get("status")
    if not isinstance(key, str) or not key:
        engine.prefs.log("[stageloom] response without a stage key ignored", flush=True)
        return
    if not isinstance(rid, str) or not rid:
        # A Stage Out solve that was triggered by another stage has no request
        # to answer. It must not be cached or applied as an empty success.
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
            # had one is not a sync to apply or cache. Consume only its own
            # pending entry if the mode change raced the answer.
            if was_pending:
                _answer_pending(record, stage, rid)
            continue
        if header.get("status") == "error" and not was_pending:
            # An error answers a request we are waiting for. If this rid is not
            # pending, the error is stale or unsolicited — often a
            # document-wide solve answering with the stage's last rid. Acting
            # on it would evict a good cached frame and could abandon a newer
            # request.
            continue

        if header.get("status") == "error":
            error = header.get("error") or {}
            code = error.get("code") if isinstance(error, dict) else None
            if code == "unknown_ref":
                # GH lost the geometry a ref named (reconnect, restarted
                # document): send it again in full (protocol §4.3). The error is
                # not a result and must not be cached.
                engine._resend_in_full(obj, record, stage, "unknown_ref response")
                continue
            if was_pending:
                # Remove only this request. A different pending entry belongs
                # to a newer request and must survive a late error.
                _answer_pending(record, stage, rid)
            engine._forget_frame(record, stage, rid)
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
        current = rid == stage.rid
        if stage.live:
            # A live stage is stateful: its answer is the solver's new state,
            # not a frame that can be replayed. A stale answer belongs to a
            # state the solver has already left; it is neither applied nor
            # cached, and it may only consume the pending entry it names.
            if not current:
                if was_pending:
                    _answer_pending(record, stage, rid)
                continue
        else:
            stored_out = None
            memory = engine._cache.get((record.stack_id, key))
            if memory is not None and rid in memory:
                stored_out = memory[rid][1] or ""
            else:
                try:
                    entry = engine._store_for(record, stage).entry(rid)
                except OSError:
                    entry = None
                if entry is not None:
                    stored_out = entry.out_hash or ""
            if (current or was_pending) and stored_out is not None and stored_out != out_hash:
                # The same request was answered differently: Grasshopper's
                # definition changed (a slider upstream of Stage Out, a graph
                # edit that does not touch Blender's inputs). Everything cached
                # for this stage is suspect. A stale answer, however, describes
                # a state the playhead has left; it must not prune the frames
                # the current state uses.
                engine._bump_generation(obj, record, stage, "Grasshopper's answer changed")
            engine._remember(record, stage, rid, header, sections, raw)
            engine._store_frame(record, stage, rid, header, sections, raw)

        # Consume only the request this answer names: a stale ok must not clear
        # a newer pending request (the rule the error path has always had).
        sent_at = _answer_pending(record, stage, rid) if was_pending else None
        if sent_at is not None:
            engine._record_timing(record, stage, "rtt_ms", engine._timed_since(sent_at))
        applied = False
        # A frozen stage is parked with rid="" and must ignore unsolicited
        # answers. Pull deliberately sends one request while frozen; its answer
        # carries the stage's current rid, so rid equality is the permission.
        if rid == stage.rid and (
            out_hash != stage.applied_out_hash or engine._payload_missing(obj, stage, sections)
        ):
            started = engine.time.monotonic()
            engine._apply(obj, stage, sections, header.get("attributes"))
            engine._record_timing(record, stage, "apply_ms", engine._timed_since(started))
            engine.stats["applies"] += 1
            engine._set(stage, "applied_out_hash", out_hash)
            applied = True
            engine._trace.append("%s: applied %s" % (stage.key, (out_hash or "")[-8:]))
        if rid == stage.rid and not applied:
            # The displayed answer is already this one, but the node group may
            # predate the current format (a group written by an older build, or
            # a session that has not applied since nesting landed). Rebuilding
            # is cheap and is the only way a same-hash frame can repair it.
            engine.nodes.ensure_group(obj, stage.key, engine._nested_rows_of(sections))
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
                engine._dirty.add(obj.name)
            continue
        engine._set(stage, "warnings", engine._warnings_text(header))
        if was_pending and not applied and not stage.live:
            # The active frame's answer arrived, but the playhead moved on while
            # it was in flight: keep the last good output and let the next pass
            # evaluate the frame the scene is actually on. A frozen stage stays
            # frozen — Pull asked for the answer, freeze still owns the state.
            # A live stage is not a frame: an unchanged output is still the
            # solver's current state, so it settles normally.
            engine._set(
                stage,
                "state",
                engine.stack_mod.STATE_FROZEN if stage.frozen else engine.stack_mod.STATE_IDLE,
            )
        else:
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
    # Bypass the frame cache once: the whole point of the resend is to ask
    # Grasshopper, and a cached frame for the same rid would answer the pass and
    # swallow it.
    engine._verify.add((record.stack_id, stage.key))
    engine._set(stage, "geom_hash", "")
    engine._set(stage, "state", engine.stack_mod.STATE_IDLE)
    engine._set(stage, "error", "")
    engine._dirty.add(obj.name)
    engine.prefs.log("[stageloom] %s: resending geometry (%s)" % (stage.key, reason), flush=True)

