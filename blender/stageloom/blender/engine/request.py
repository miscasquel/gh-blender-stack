"""Prefix evaluation, request IDs, sends and the per-stage pass."""

import sys

engine = sys.modules[__package__]


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
    return engine.stack_mod.STATE_FROZEN if stage.frozen else engine.stack_mod.STATE_OK


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
    if engine._last_no_send.get(key) == token:
        return
    engine._last_no_send[key] = token
    engine._trace.append("%s: no send (%s)" % (stage.key, reason))


def _process_stage(obj, record, stage):
    """One stage's pass. Returns "waiting" (request in flight; stop the stack),
    "busy" (work happened; keep the fast timer), or "idle"."""
    modifier = engine.nodes.modifier_of(obj, stage)
    if modifier is None and not stage.modifier_uid:
        # A record saved before the uid existed, whose group is gone: the name
        # fallback still finds it, and the repair below re-links it.
        modifier = engine.nodes.modifier_by_name(obj, stage)
    if modifier is None:
        # `_drop_removed_stages` runs first; this is a defensive skip only.
        engine._no_send(record, stage, "no stage")
        return "idle"
    active = engine.nodes.modifier_active(modifier)
    # The modifier's own toggles are the control (plan §11): a stage is active
    # while visible in any context, and the record mirrors that for the state
    # row. Nothing here ever writes a modifier flag back — that was the bug
    # where a toggle in the modifiers panel was undone by the next pass.
    engine._set(stage, "enabled", active)
    if not active:
        engine._set(stage, "state", engine.stack_mod.STATE_DISABLED)
        engine._no_send(record, stage, "disabled")
        return "idle"
    engine._repair_plumbing(obj, record, stage, modifier)
    if stage.frozen and (record.stack_id, stage.key) not in engine._pull_permits:
        # Freeze: no requests and no cache replay; the helpers keep their last
        # output. Pull (engine.request_again) plants a one-shot permit above and
        # works while frozen; it is consumed here.
        engine._set(stage, "state", engine.stack_mod.STATE_FROZEN)
        engine._no_send(record, stage, "frozen")
        return "idle"

    # While a request is young, a pass costs nothing: the prefix does not have
    # to be read to know nothing can go out before the soft retry. Without this
    # every edit during a slow solve re-evaluated the whole prefix and threw it
    # away (measured: 62 clones for 13 sends on a 51k-vertex drag).
    pending = engine._pending_of(record.stack_id, stage.key)
    if pending:
        sent_rid = next(iter(pending))
        if engine.time.monotonic() - pending[sent_rid] < engine.SOFT_RETRY_SECONDS:
            engine._no_send(record, stage, "waiting on rid %s" % sent_rid[-8:])
            return "waiting"

    started = engine.time.monotonic()
    sections, geom_hash, attributes, definitions = engine._prefix_sections(obj, record, stage)
    engine._record_timing(record, stage, "prefix_ms", engine._timed_since(started))
    params_json = engine.props.canonical_json(obj, stage)
    rid = engine.hashing.request_id(stage.key, params_json, geom_hash, "m", "stack-local", stage.nonce)

    # `stage.rid` is deliberately NOT set here. It names the request the stage is
    # waiting on — the answer it must recognise as its own — so it is written only
    # when a request actually goes out (or when a cached frame is applied). While
    # a reply is in flight the playhead may have moved on, and updating `stage.rid`
    # up front would make `_handle_response` discard that reply as stale: during
    # playback nothing would ever be applied and the output would freeze for as
    # long as the round trip takes. The wanted frame is fetched on the next pass,
    # from the frame cache when it is warm.

    verify_key = (record.stack_id, stage.key)
    cached = engine._cached_response(record, stage, rid)
    connected = bool(engine.bridge.state["connected"])
    sent = engine._sent.get(verify_key)
    # Whether the client has been told *this* request. A frame in the cache is
    # Blender's answer, not the client's state: cancelling a transform (or an
    # undo, or a reverted slider) returns the rid to an earlier, cached frame
    # while the client still holds the intermediate one.
    client_has = sent is not None and sent[0] == rid
    # A reconnect or a file load puts the stage on `_verify`: the definition may
    # have changed while no one was listening, and a cached frame cannot know
    # it. With a client there, ask; with none, replay the cache as usual and
    # keep the permit for when one connects.
    if cached is not None and (verify_key not in engine._verify or not connected):
        header, cached_sections = cached
        out_hash = header.get("out_hash") or ""
        engine._set(stage, "rid", rid)
        applied = False
        if out_hash != stage.applied_out_hash or engine._payload_missing(
            obj, stage, cached_sections
        ):
            started = engine.time.monotonic()
            engine._apply(obj, stage, cached_sections, header.get("attributes"))
            engine._record_timing(record, stage, "apply_ms", engine._timed_since(started))
            engine.stats["applies"] += 1
            engine._set(stage, "applied_out_hash", out_hash)
            engine._set(stage, "warnings", engine._warnings_text(header))
            engine._set(stage, "error", "")
            applied = True
        else:
            # The cached frame is the displayed one, but the group may predate
            # the current format: repairing it here is what lets a replay fix a
            # stale session (see _handle_response).
            engine.nodes.ensure_group(obj, stage.key, engine._nested_rows_of(cached_sections))
        if not connected or client_has:
            engine._no_send(record, stage, "cache hit", rid)
            engine._set(stage, "state", engine._rest_state(stage))
            return "busy" if applied else "idle"
        # The frame is right for Blender, but the connected client holds some
        # other request. Applying it is not enough: the client must also be told
        # the inputs for this rid, or its viewport keeps the cancelled edit (and
        # the next edit starts from geometry Blender no longer shows). Fall
        # through and send; the answer carries the same output and is not
        # re-applied.

    if not engine.bridge.state["connected"]:
        # A miss with Grasshopper away keeps the last good output; only cached
        # frames replay (renders use the cache). A stage no document has ever
        # answered reads unbound instead of disconnected: disconnected means a
        # client was there and left (plan §9.2).
        engine._no_send(record, stage, "disconnected", rid)
        engine._set(
            stage,
            "state",
            engine.stack_mod.STATE_UNBOUND if not stage.applied_out_hash else engine.stack_mod.STATE_DISCONNECTED,
        )
        return "idle"

    pending = engine._pending_of(record.stack_id, stage.key)
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
        age = engine.time.monotonic() - sent_at
        if age <= engine._timeout():
            # The state may move on while an answer is in flight. After
            # SOFT_RETRY_SECONDS a newer request supersedes the stale one: GH
            # coalesces solves, so the ask is not a backlog, and the answer is
            # for what the user is looking at. The same state waits: the hard
            # timeout below is its resend.
            if rid == sent_rid or age < engine.SOFT_RETRY_SECONDS:
                engine._no_send(record, stage, "waiting on rid %s" % sent_rid[-8:], rid)
                return "waiting"
            engine._forget(record.stack_id, stage.key)
            engine._trace.append("%s: superseded %s" % (stage.key, sent_rid[-8:]))
        else:
            # The answer never came. We cannot know whether Grasshopper received
            # the geometry, so the retry goes out in full — the same reasoning as
            # the unknown_ref handler. And the failure stays visible: retrying
            # silently is what made a stage stuck in this loop look healthy.
            engine._forget(record.stack_id, stage.key)
            engine._sent.pop(verify_key, None)
            engine._set(stage, "geom_hash", "")
            engine.stats["timeouts"] += 1
            retry_note = "timed out after %.0f s; requesting the geometry again" % engine._timeout()

    # Use the session truth, not the saved stage record: undo/redo restores
    # `stage.geom_hash` from its step, while the client keeps the geometry it
    # was last sent. No session entry means no client has been told anything
    # this session, so the first request must carry the geometry in full (§8.8).
    held_geom = engine._sent[verify_key][1] if verify_key in engine._sent else ""
    mode = "full" if geom_hash != held_geom else "ref"
    header = {
        "type": "request",
        "stage": stage.key,
        "rid": rid,
        "nonce": stage.nonce,
        "params": engine.props.cast_values(obj, stage),
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
    started = engine.time.monotonic()
    frame = engine.codec.encode_frame(header, sections if mode == "full" else [])
    engine._record_timing(record, stage, "encode_ms", engine._timed_since(started))
    if not engine.server.SERVER.send(frame):
        # No client socket to carry it. Recording a pending request here would
        # make the stage wait out a whole timeout for an answer nobody was asked
        # for — and the retry would be gated on that same pending entry.
        engine.stats["undelivered"] += 1
        engine._set(stage, "state", engine.stack_mod.STATE_DISCONNECTED)
        engine._no_send(record, stage, "undelivered", rid)
        return "idle"
    engine._verify.discard(verify_key)
    # The client has now been told this request; from here the frame cache may
    # answer it without a send (until a reconnect/load/refusal says otherwise).
    engine._sent[verify_key] = (rid, geom_hash)
    # A Pull permit is spent by the send it caused, not before it: a send that
    # never left (disconnected, undelivered) leaves the Pull armed for the next
    # pass instead of losing it silently.
    engine._pull_permits.discard((record.stack_id, stage.key))
    engine._trace.append(
        "%s: sent %s %s" % (stage.key, mode, rid[-8:] if rid else "")
    )
    if retry_note:
        engine.prefs.log("[stageloom] %s: %s" % (stage.key, retry_note), flush=True)
    engine.stats["sends"] += 1
    # A send re-arms the "why no send" reason: the next silence is a new
    # decision, not a repeat of the pass before this one.
    engine._last_no_send.pop((record.stack_id, stage.key), None)

    now = engine.time.monotonic()
    engine._heartbeat["sent_at"] = now
    engine._pending_of(record.stack_id, stage.key)[rid] = now
    engine._waiting_objects.add(obj.name)
    engine._state_since[(record.stack_id, stage.key)] = now
    engine._set(stage, "rid", rid)
    engine._set(stage, "geom_hash", geom_hash)
    engine._set(stage, "state", engine.stack_mod.STATE_WAITING)
    engine._set(stage, "error", retry_note)
    return "waiting"


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
        sections, attributes = engine.geometry_io.object_to_sections(obj, as_subd=subd)
        return sections, engine._sections_hash(sections, attributes), attributes, []

    modifier = engine.nodes.modifier_of(obj, stage)
    index = engine._modifier_index(obj, modifier)
    revision = engine._prefix_revision(obj, record)
    # Keyed by identity, not by name: an object renamed to a name another object
    # used to hold would otherwise inherit what that object had evaluated.
    # as_pointer() is stable for the session and keeps no reference to the object,
    # so a deleted object does not live on through this dict.
    memo_key = (obj.as_pointer(), stage.key)
    memo = engine._prefix_memo.get(memo_key)
    if memo is not None and memo["revision"] == revision:
        engine.stats["prefix_memo_hits"] += 1
        return memo["sections"], memo["geom_hash"], memo["attributes"], memo["definitions"]

    subd = bool(record.subd)
    batched = None
    if engine._prefix_batching:
        if not engine._prefix_batch_built:
            engine._build_prefix_batch(obj, record)
        batched = engine._prefix_batch.get((obj.as_pointer(), stage.key))
    if batched is not None:
        sections, attributes, definitions = batched
    else:
        sections, attributes, definitions = engine._clone_prefix(
            obj, index, subd, stage.key
        )
        engine.stats["prefix_clones"] += 1

    # Blender evaluates modifier-generated attribute data (a Bevel's UVs, for
    # example) with last-bit float variation between evaluations. Hashing that
    # verbatim makes an idle scene look changed, so the engine re-requests and
    # re-applies forever. When the new read differs from the memoized one only in
    # attribute floats within a tolerance, the memo wins: the stored sections,
    # hash and revision are kept, and nothing is sent. Any user-sized edit is far
    # above the tolerance and takes the normal path.
    if (
        memo is not None
        and engine._same_prefix(
            memo["sections"], memo["attributes"], memo["definitions"],
            sections, attributes, definitions,
        )
    ):
        engine.stats["prefix_memo_hits"] += 1
        sections = memo["sections"]
        attributes = memo["attributes"]
        definitions = memo["definitions"]
        geom_hash = memo["geom_hash"]
        memo["revision"] = revision
        return sections, geom_hash, attributes, definitions

    geom_hash = engine._sections_hash(sections, attributes, definitions)
    engine._prefix_memo[memo_key] = {
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
        if engine.attributes_mod.wire_dtype(descriptor.get("type") or "FLOAT") == "f32"
    }
    for name, (dtype, values) in new_by.items():
        old_dtype, old_values = old_by[name]
        if dtype != old_dtype:
            return False
        if name in floating:
            if not engine._close_enough(old_values, values):
                return False
        elif engine.codec.section_bytes(name, dtype, values) != engine.codec.section_bytes(name, dtype, old_values):
            return False
    return True


def _close_enough(old_values, new_values):
    """Element-wise float comparison with the tolerance attribute noise needs."""
    old = engine.np.asarray(old_values, dtype=engine.np.float32).reshape(-1)
    new = engine.np.asarray(new_values, dtype=engine.np.float32).reshape(-1)
    if old.shape != new.shape:
        return False
    return bool(engine.np.allclose(old, new, rtol=1e-5, atol=1e-9, equal_nan=True))


def _build_prefix_batch(obj, record):
    """Evaluate every stage prefix of one object in a single depsgraph update.

    Linking a clone and asking for `evaluated_depsgraph_get()` costs a full
    depsgraph update, ~17 ms on the FiraTower scene, and doing that once per
    stage made a three-stage object cost ~50 ms before any geometry was read.
    The clones are independent objects with different modifier subsets, so they
    can coexist for one update; their evaluated sections are identical to the
    per-stage reads (checked against the old hashes).
    """
    if obj.type == "SURFACE" or len(record.stages) < 2:
        engine._prefix_batch_built = True
        return
    stages = []
    for stage in record.stages:
        modifier = engine.nodes.modifier_of(obj, stage)
        if modifier is None and not stage.modifier_uid:
            modifier = engine.nodes.modifier_by_name(obj, stage)
        stages.append((stage, engine._modifier_index(obj, modifier)))
    results = engine._clone_prefixes(obj, stages, bool(record.subd))
    pointer = obj.as_pointer()
    engine._prefix_batch.update(
        {(pointer, key): value for key, value in results.items()}
    )
    engine._prefix_batch_built = True
    engine.stats["prefix_clones"] += len(results)


def _clone_prefixes(obj, stages, subd):
    """One guarded depsgraph update for several prefix clones.

    Cleanup deliberately skips the per-clone `view_layer.update()`: each one
    forces another full evaluation, and the objects are removed from the data
    before the next pass reads anything. The next `evaluated_depsgraph_get()`
    sees the removals.
    """
    clones = []
    engine._guard_owner = (obj.name, "prefix batch")
    engine._evaluating_since = engine.time.monotonic()
    engine._evaluating = True
    results = {}
    try:
        for stage, index in stages:
            clone = obj.copy()
            owned_data = obj.data.copy() if obj.mode == "EDIT" else None
            if owned_data is not None:
                clone.data = owned_data
            # Register before linking: a link or modifier-removal failure must
            # still remove the clone in the finally block.
            clones.append((stage, clone, owned_data))
            engine.bpy.context.scene.collection.objects.link(clone)
            for later in list(clone.modifiers)[index:]:
                clone.modifiers.remove(later)
        depsgraph = engine.bpy.context.evaluated_depsgraph_get()
        for stage, clone, _owned_data in clones:
            geometry = clone.evaluated_get(depsgraph).evaluated_geometry()
            sections, attributes, definitions = (
                engine.geometry_io.geometry_set_to_sections(
                    geometry, as_subd=subd
                )
            )
            results[stage.key] = (sections, attributes, definitions)
    finally:
        primary_failed = engine.sys.exc_info()[0] is not None
        cleanup_failure = None
        try:
            for stage, clone, owned_data in clones:
                failure = engine._cleanup_prefix_clone(
                    clone, owned_data, stage.key, update_view_layer=False
                )
                if cleanup_failure is None:
                    cleanup_failure = failure
            try:
                # Flush the removals while the guard is still up. Without this
                # the depsgraph delivers the clone link/remove tags after the
                # guard drops, marks every stack dirty, and the next pass builds
                # another batch: a live-session feedback loop.
                engine.bpy.context.view_layer.update()
            except Exception as exc:
                if cleanup_failure is None:
                    cleanup_failure = exc
                message = "prefix cleanup failed (update view layer): %r" % (exc,)
                engine.bridge.state["error"] = message
                if not engine._trace or engine._trace[-1] != message:
                    engine._trace.append(message)
                import traceback

                engine.prefs.log(
                    "[stageloom] %s\n%s" % (message, traceback.format_exc()),
                    flush=True,
                )
        finally:
            engine._evaluating = False
        if cleanup_failure is not None and not primary_failed:
            raise cleanup_failure
    return results


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
    engine._guard_owner = (obj.name, stage_key)
    engine._evaluating_since = engine.time.monotonic()
    engine._evaluating = True
    try:
        engine.bpy.context.scene.collection.objects.link(clone)
        for later in list(clone.modifiers)[index:]:
            clone.modifiers.remove(later)
        depsgraph = engine.bpy.context.evaluated_depsgraph_get()
        geometry = clone.evaluated_get(depsgraph).evaluated_geometry()
        sections, attributes, definitions = engine.geometry_io.geometry_set_to_sections(
            geometry, as_subd=subd
        )
    finally:
        # The primary failure, if any, is captured before cleanup runs: a
        # cleanup failure must not mask it, and the guard reset must happen
        # either way. The reset is the inner `finally` of the two, so it runs
        # even if `_cleanup_prefix_clone` itself surprises us.
        primary_failed = engine.sys.exc_info()[0] is not None
        try:
            cleanup_failure = engine._cleanup_prefix_clone(clone, owned_data, stage_key)
        finally:
            engine._evaluating = False
        if cleanup_failure is not None and not primary_failed:
            raise cleanup_failure
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
        ("remove clone", lambda: engine.bpy.data.objects.remove(clone, do_unlink=True)),
        (
            "drop private data",
            (lambda: engine._drop_owned_data(owned_data)) if owned_data is not None else None,
        ),
        (
            "update view layer",
            (lambda: engine.bpy.context.view_layer.update())
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
            engine.bridge.state["error"] = message
            if not engine._trace or engine._trace[-1] != message:
                engine._trace.append(message)
            import traceback

            engine.prefs.log("[stageloom] %s\n%s" % (message, traceback.format_exc()), flush=True)
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
            engine.bpy.data, engine._OWNED_COLLECTIONS.get(id_block.bl_rna.identifier, ""), None
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
    raw = {name: engine.codec.section_bytes(name, dtype, values) for name, dtype, values in sections}
    if attributes:
        raw["attr.descriptors"] = engine.codec.canonical_json(attributes).encode("utf-8")
    if definitions:
        raw["definitions"] = engine.codec.canonical_json(definitions).encode("utf-8")
    return engine.hashing.geometry_hash(raw)


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
    return (engine._prefix_epoch, bool(record.subd), engine.bpy.context.scene.frame_float)


def _bump_epochs(depsgraph=None):
    """Move the generation for everything Blender tagged in this evaluation.

    Called with the depsgraph from `_on_depsgraph`, and with None from
    `_bump_id` for engine writes a later stage in the same pass must see. Every
    entry counts, including all-false ones (node tree edits, driver-driven ID
    properties): the flags say what kind of update it was, not whether an input
    moved. The engine's own clone events are excluded by the guard alone.
    Returns the update count for the heartbeat.
    """
    engine._prefix_epoch += 1
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
    if engine._trace and engine._trace[-1].startswith("depsgraph"):
        engine._trace[-1] = line
    else:
        engine._trace.append(line)
    return len(updates)


def _bump_id(id_block):
    """Move the generation for the engine's own writes.

    The depsgraph has not seen a write the engine just made when the next stage
    in the same pass looks at its prefix, so the write is stamped here rather
    than waited for. The generation is global, so one bump covers every stage
    downstream of the written datablock.
    """
    engine._bump_epochs(None)


def _modifier_index(obj, modifier):
    if modifier is None:
        return 0
    for i, candidate in enumerate(obj.modifiers):
        if candidate == modifier:
            return i
    return 0


def _timeout():
    return float(engine.prefs._pref("timeout", 30.0))

