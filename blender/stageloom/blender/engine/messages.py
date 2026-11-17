"""Incoming protocol messages: schema, responses, errors and hello."""

import time
import traceback

from .. import bridge, geometry_io, nodes, prefs, props
from .. import stack as stack_mod
from . import apply as apply_mod, lifecycle as lifecycle_mod
from .apply import _nested_rows_of, _payload_missing, _warnings_text
from .request import _set
from .runtime import ENGINE
from .state import _set_state


def _topology_token(header):
    """The response's optional mesh-topology token (protocol §4.4).

    Absent, wrong-shaped or malformed values are no token, never an error: a
    peer that does not send one is answered by the verified path, and a bad
    field must not cost the frame. Only the producer's exact ``sha256:`` +
    64 lowercase hex shape is trusted for the fast path.
    """
    geom = header.get("geom")
    if not isinstance(geom, dict):
        return None
    token = geom.get("topology")
    if not isinstance(token, str) or len(token) != 71 or not token.startswith("sha256:"):
        return None
    if any(char not in "0123456789abcdef" for char in token[7:]):
        return None
    return token


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
                ENGINE.trace("incoming message failed and could not be reported")


def _pending_at(key):
    """One stage's pending map, without creating a runtime record."""
    row = ENGINE.peek_runtime(*key)
    return row.pending if row is not None else {}


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
    if not ENGINE.trace_lines or ENGINE.trace_lines[-1] != message:
        ENGINE.trace(message)

    prefs.log("[stageloom] incoming %s failed:\n%s" % (where, traceback.format_exc()), flush=True)
    if mtype == "response" and stage_key:
        try:
            _clear_failed_response(stage_key, header.get("rid"))
        except Exception as recovery:
            # Best-effort, like the cleanup: a stage that cannot be recovered
            # must not cost the *other* messages their chance to be handled.
            prefs.log(
                "[stageloom] could not recover the failed response for %s: %r"
                % (stage_key, recovery),
                flush=True,
            )


def _clear_failed_response(stage_key, rid=None):
    """Put the stage of a failed response back in a retryable state.

    Only the request the failed message names is consumed. A message for an
    older rid is stale: the stage is waiting on a newer request, and clearing
    that wait here would make the newer answer look unsolicited. Without a rid
    the failed response cannot be attributed to any request, so a pending wait
    is left untouched for the same reason.

    The consumed answer may already have removed its pending entry before the
    handler raised; ``rid == stage.rid`` still identifies that current request.
    The failed answer consumed its permit, so arm the same one a reconnect uses:
    the next pass must ask again instead of treating the unchanged request id as
    already delivered. Pending is cleared (idempotent) and the state leaves
    `waiting`, where an empty pending map would look like an answer on its way.
    """
    for obj, record, stage in lifecycle_mod._stage_views(stage_key):
        key = (record.stack_id, stage.key)
        pending = _pending_at(key)
        if rid:
            if rid in pending:
                pending.pop(rid, None)
                if not pending:
                    ENGINE.forget(*key)
            elif rid != stage.rid:
                ENGINE.trace(
                    "%s: ignored a stale failed response" % stage.key
                )
                continue
        elif pending:
            ENGINE.trace(
                "%s: ignored an uncorrelated failed response" % stage.key
            )
            continue
        ENGINE.clear_pull(*key)
        ENGINE.verify.add(key)
        _set_state(
            stage,
            stack_mod.STATE_IDLE,
        )
        ENGINE.dirty.add(obj.name)
        ENGINE.mark_hot()


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
    caps = (header or {}).get("caps")
    bridge.state["caps"] = (
        [cap for cap in caps if isinstance(cap, str)]
        if isinstance(caps, list)
        else []
    )
    claims = {}
    for entry in (header or {}).get("stages") or []:
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        if key:
            claims[key] = entry
    for obj in lifecycle_mod._all_stack_objects():
        record = stack_mod.stack_of(obj)
        if not record.stack_id:
            record.stack_id = stack_mod.ensure_stack(obj).stack_id
        for stage in record.stages:
            if stage.key in ENGINE.stage_conflicts:
                # An ambiguous key has no owner to adopt a claim for, and no
                # request may be verified or sent until the user resolves it.
                continue
            entry = claims.get(stage.key)
            if entry is not None:
                _apply_input_mode(stage, entry)
                _apply_output_presence(stage, entry)
            if stage.state == stack_mod.STATE_WAITING:
                ENGINE.forget(record.stack_id, stage.key)
                _set_state(stage, stack_mod.STATE_IDLE)
            # A request id names a request this client session was asked. The
            # next client may be a different document: a late response carrying
            # the old id must not apply before the verify request goes out.
            _set(stage, "rid", "")
            _set(stage, "geom_hash", "")
            row = ENGINE.runtime(record.stack_id, stage.key)
            row.sent = None
            row.last_frame = None
            ENGINE.verify.add((record.stack_id, stage.key))
        ENGINE.dirty.add(obj.name)
    ENGINE.trace("client hello: %s" % (doc or "unnamed"))
    ENGINE.mark_hot()


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
        stack_mod.INPUT_PREFIX
        if has_stage_in
        else stack_mod.INPUT_GENERATOR
    )
    if _set(stage, "input_mode", mode):
        ENGINE.trace("%s: input mode %s" % (stage.key, mode))
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
    if _set(stage, "has_stage_out", has_stage_out):
        ENGINE.trace(
            "%s: Stage Out %s" % (stage.key, "present" if has_stage_out else "missing")
        )
        return True
    return False


def _handle_schema(header):
    key = header.get("stage")
    if ENGINE.stage_key_conflicted(key):
        prefs.log(
            "[stageloom] schema for duplicate stage key %r ignored" % (key,),
            flush=True,
        )
        return
    params = header.get("params") or []
    matched = False
    for obj, record, stage in lifecycle_mod._stage_views(key):
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

            prefs.log("[stageloom] schema error:\n%s" % traceback.format_exc(), flush=True)
            continue
        # A changed schema is a changed definition: ask for a new solve even if
        # the request id and frame have not moved.
        ENGINE.verify.add((record.stack_id, stage.key))
        ENGINE.dirty.add(obj.name)
        obj.update_tag()
    if not matched:
        # A schema for a stage no object has: usually a key pasted into GH that
        # does not belong to any stack, or a stage removed in Blender.
        prefs.log("[stageloom] schema for unknown stage %r ignored" % (key,), flush=True)


def _answer_pending(record, stage, rid):
    """Consume the pending entry one answer names, if it is still the one held.

    Returns the monotonic send time, or None. Only the named rid is removed: a
    stale answer for an older request must not clear a newer pending request
    (the error path has always had this rule; the ok path now matches it).
    """
    key = (record.stack_id, stage.key)
    pending = _pending_at(key)
    if not pending:
        return None
    sent_at = pending.pop(rid, None)
    if not pending:
        ENGINE.forget(*key)
    return sent_at


def _handle_response(header, sections):
    key = header.get("stage")
    rid = header.get("rid")
    status = header.get("status")
    if not isinstance(key, str) or not key:
        prefs.log("[stageloom] response without a stage key ignored", flush=True)
        return
    if ENGINE.stage_key_conflicted(key):
        prefs.log(
            "[stageloom] response for duplicate stage key %r ignored" % (key,),
            flush=True,
        )
        return
    if not isinstance(rid, str) or not rid:
        # A Stage Out solve that was triggered by another stage has no request
        # to answer. It must not be applied as an empty success.
        prefs.log(
            "[stageloom] response for %r without a request id ignored" % (key,),
            flush=True,
        )
        return
    if status not in ("ok", "error"):
        prefs.log(
            "[stageloom] response for %r has invalid status %r ignored"
            % (key, status),
            flush=True,
        )
        return
    if status == "ok":
        out_hash = header.get("out_hash")
        if not isinstance(out_hash, str) or not out_hash:
            prefs.log(
                "[stageloom] ok response for %r has no output hash ignored" % (key,),
                flush=True,
            )
            return

    for obj, record, stage in lifecycle_mod._stage_views(key):
        if not record.stack_id:
            record.stack_id = stack_mod.ensure_stack(obj).stack_id

        if not stage.enabled:
            ENGINE.forget(record.stack_id, stage.key)
            ENGINE.clear_pull(record.stack_id, stage.key)
            continue

        pending_key = (record.stack_id, stage.key)
        pending = _pending_at(pending_key)
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
                _resend_in_full(obj, record, stage, "unknown_ref response")
                continue
            if was_pending:
                # Remove only this request. A different pending entry belongs
                # to a newer request and must survive a late error.
                _answer_pending(record, stage, rid)
                # The error ends a Pull as surely as an answer does: without
                # this the permit would arm one more request while frozen.
                ENGINE.clear_pull(*pending_key)
            # The guard keeps a frozen stage frozen and records the reason; a
            # disabled stage stays disabled.
            _set_state(
                stage,
                stack_mod.STATE_ERROR,
                error=error.get("message") if isinstance(error, dict) else str(error),
            )
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
            ENGINE.trace(
                "%s: dropped stale answer %s" % (stage.key, rid[-8:])
            )
            continue

        # Consume only the request this answer names: a stale ok must not clear
        # a newer pending request (the rule the error path has always had).
        sent_at = _answer_pending(record, stage, rid) if was_pending else None
        if sent_at is not None:
            ENGINE.record_timing(record, stage, "rtt_ms", ENGINE.timed_since(sent_at))
        applied = False
        # A frozen stage is parked with rid="" and must ignore unsolicited
        # answers. Pull deliberately sends one request while frozen; its answer
        # carries the stage's current rid, so rid equality is the permission.
        if out_hash != stage.applied_out_hash or _payload_missing(obj, stage, sections):
            started = time.monotonic()
            try:
                applied_topology = apply_mod._apply(
                    obj, stage, sections, header.get("attributes"),
                    header.get("definitions", []),
                    topology=_topology_token(header),
                )
            except geometry_io.GeometryIOError as exc:
                # A malformed or internally inconsistent section group is a
                # classified stage error, not an anonymous incoming-frame crash.
                _set(stage, "error", "geometry: %s" % exc)
                raise
            ENGINE.record_timing(record, stage, "apply_ms", ENGINE.timed_since(started))
            ENGINE.stats()["applies"] += 1
            _set(stage, "applied_out_hash", out_hash)
            # A frame without a mesh group (or from an old peer) clears the
            # claim: the helper no longer holds a topology this token names.
            _set(stage, "applied_topology", applied_topology or "")
            applied = True
            ENGINE.trace("%s: applied %s" % (stage.key, (out_hash or "")[-8:]))
        else:
            # The displayed answer is already this one, but the node group may
            # predate the current format (a group written by an older build, or
            # a session that has not applied since nesting landed). Rebuilding
            # is cheap and is the only way a same-hash answer can repair it.
            nodes.ensure_group(obj, stage.key, _nested_rows_of(sections))
        _set(stage, "warnings", _warnings_text(header))
        # An unchanged output is still the solver's current state: it settles
        # normally; a frozen or disabled stage keeps its flag state.
        _set_state(stage, stack_mod.STATE_OK)
        _set(stage, "error", "")
        # A Pull is one request: the answer that names its rid ends the permit,
        # so a frozen stage parks again instead of asking forever.
        ENGINE.clear_pull(*pending_key)
        if applied:
            ENGINE.stats()["tags"] += 1
            obj.update_tag()
        if applied or was_pending:
            ENGINE.dirty.add(obj.name)
            ENGINE.mark_hot()


def _handle_error(header):
    """A standalone `error` message (protocol §4.6).

    `unknown_ref` is the one that needs action: Grasshopper does not hold the
    geometry the last request named. Everything else is reported on the stages it
    names so a protocol mismatch is not silent.

    An error with a `rid` belongs to exactly that request: a late one must not
    cancel the newer request the stage is now waiting on. Without a rid the
    message cannot be attributed, so it never consumes a pending wait; it only
    surfaces the error when there is no wait that could be the newer one.
    """
    code = header.get("code")
    message = header.get("message") or code or "protocol error"
    rid = header.get("rid")
    if ENGINE.stage_key_conflicted(header.get("stage")):
        prefs.log(
            "[stageloom] error for duplicate stage key %r ignored"
            % (header.get("stage"),),
            flush=True,
        )
        return
    if code == "unknown_ref":
        matched = False
        for obj, record, stage in lifecycle_mod._stage_views(header.get("stage")):
            matched = True
            key = (record.stack_id, stage.key)
            pending = _pending_at(key)
            if rid:
                if not _request_is_current(record, stage, rid):
                    ENGINE.trace(
                        "%s: ignored a stale unknown_ref" % stage.key
                    )
                    continue
            elif pending:
                # No rid: this could answer a request that has already been
                # superseded. A full resend forgets the pending map, which
                # would cancel the newer request.
                ENGINE.trace(
                    "%s: ignored an uncorrelated unknown_ref" % stage.key
                )
                continue
            _resend_in_full(obj, record, stage, "unknown_ref")
        if not matched:
            prefs.log("[stageloom] unknown_ref for unknown stage %r" % (header.get("stage"),), flush=True)
        return

    prefs.log("[stageloom] protocol error %s: %s" % (code, message), flush=True)
    for obj, record, stage in lifecycle_mod._stage_views(header.get("stage")):
        # The request this error answers is over: without this the stage would
        # sit `waiting` for the full timeout for an answer that is already here.
        # The error is left on display and the stage is not dirtied — retrying
        # here would ask a component that just failed, in a loop; the next
        # change or Push retries (protocol §7).
        key = (record.stack_id, stage.key)
        pending = _pending_at(key)
        if rid:
            if not _request_is_current(record, stage, rid):
                ENGINE.trace("%s: ignored a stale protocol error" % stage.key)
                continue
            if rid in pending:
                _answer_pending(record, stage, rid)
            ENGINE.clear_pull(*key)
        elif pending:
            ENGINE.trace(
                "%s: ignored an uncorrelated protocol error" % stage.key
            )
            continue
        _set(stage, "error", "%s: %s" % (code, message))
        _set_state(stage, stack_mod.STATE_ERROR)


def _request_is_current(record, stage, rid):
    """Whether ``rid`` names the request this stage is waiting on now."""
    if not rid:
        return False
    pending = _pending_at((record.stack_id, stage.key))
    return rid in pending or rid == stage.rid


def _resend_in_full(obj, record, stage, reason):
    """Grasshopper no longer holds this stage's geometry, so the next request
    must carry it. Shared by the `unknown_ref` error message, the response-shaped
    `unknown_ref` and the request timeout, because all three leave the same
    uncertainty behind (protocol §4.3, §8.8)."""
    ENGINE.forget(record.stack_id, stage.key)
    ENGINE.runtime(record.stack_id, stage.key).sent = None
    # Ask even if the request id and frame have not moved: the whole point of
    # the resend is to tell Grasshopper again.
    ENGINE.verify.add((record.stack_id, stage.key))
    _set(stage, "geom_hash", "")
    _set_state(stage, stack_mod.STATE_IDLE)
    _set(stage, "error", "")
    ENGINE.dirty.add(obj.name)
    prefs.log("[stageloom] %s: resending geometry (%s)" % (stage.key, reason), flush=True)

