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

import time

import bpy
import numpy as np

from ..core import cache as frame_cache
from ..core import codec, hashing
from . import bridge, geometry_io, helpers, nodes, prefs, props, server, stack as stack_mod

WAIT_INTERVAL = 0.005
IDLE_INTERVAL = 0.05

# How long a stage may wait for its outstanding answer before the request is given
# up on and retried in full. See `_timeout()`.

_cache = {}  # (stack_id, stage_key) -> {rid: (header, sections)}; the hot path
_stores = {}  # (stack_id, stage_key) -> ResponseStore; the backing store
_prefix_memo = {}  # (obj.as_pointer(), stage key) -> {"revision": tuple, "sections": list}
# (stack_id, stage_key) -> {rid: monotonic seconds it was sent}. At most one entry:
# the evaluation in flight. One at a time, deliberately — a frame that arrives while
# an answer is outstanding is not queued behind it, because Grasshopper solves in
# arrival order on one thread and a queue of frames the user has already scrubbed
# past only keeps the client busy after they have moved on. The frame the playhead
# is on is asked for on the first pass after the answer lands, and every answer is
# stored either way, so nothing evaluated is wasted.
_pending = {}
_dirty = set()  # object names that need a pass
_registered = False
# True while the engine evaluates a prefix clone. Linking and removing that
# clone tags the depsgraph; without the guard every pass would re-mark every
# stack dirty and the engine would never idle.
_evaluating = False
# Counters for tests and diagnostics: what the engine actually did, so a quiet
# idle can be asserted without guessing at Blender internals.
stats = {
    "passes": 0,
    "prefix_clones": 0,  # prefix evaluations that needed a clone
    "prefix_memo_hits": 0,  # prefix evaluations served from the revision memo
    "applies": 0,  # responses written into helper objects
    "tags": 0,  # obj.update_tag() calls
    "sends": 0,  # requests actually sent
    "timeouts": 0,  # requests given up on after the timeout and retried in full
    "undelivered": 0,  # requests not even sent: no client socket to carry them
    "signature_truncated": 0,  # node-tree walks cut at the depth cap (memo disabled)
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


@bpy.app.handlers.persistent
def _on_depsgraph(scene, depsgraph=None):
    if _evaluating:
        return
    _mark_dirty_all()


@bpy.app.handlers.persistent
def _on_frame_change(scene, depsgraph=None):
    _mark_dirty_all()


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
    if not bpy.app.timers.is_registered(_timer):
        bpy.app.timers.register(_timer, first_interval=IDLE_INTERVAL, persistent=True)
    _pending.clear()
    _prefix_memo.clear()
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
    _mark_dirty_all()


# ------------------------------------------------------------------------ tick


def _timer():
    """bpy.app.timers callback. Returns the next interval.

    A timer that raises is unregistered by Blender, which would silently stop the
    engine; every part of the pass is therefore guarded, and failures land in the
    panel state instead.
    """
    try:
        stats["passes"] += 1
        _handle_incoming()
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

                print("[gh-stack] engine error:\n%s" % traceback.format_exc(), flush=True)
            _dirty.discard(name)
        return WAIT_INTERVAL if busy else IDLE_INTERVAL
    except Exception as exc:
        bridge.state["error"] = "engine error: %r" % (exc,)
        import traceback

        print("[gh-stack] engine error:\n%s" % traceback.format_exc(), flush=True)
        return IDLE_INTERVAL


def _handle_incoming():
    for header, sections in bridge.drain_incoming():
        mtype = header.get("type")
        if mtype == "schema":
            _handle_schema(header)
        elif mtype == "response":
            _handle_response(header, sections)
        elif mtype == "error":
            _handle_error(header)
        elif mtype == "hello":
            _on_client_hello(header)


def _on_client_hello(header):
    """A GH client connected (or reconnected).

    A request sent while no client was connected is lost, and a stage left
    waiting on it would sit until its timeout. Reset waiting stages and mark
    every stack dirty so the current request goes out again (protocol §8.8).
    A reconnect with a *different* document resets the stages it claims: frames
    cached against the old document must not replay (handoff reset rules).

    Every claimed stage also forgets its geometry hash: a `hello` is a new client
    session, so the geometry it holds is unknown, and protocol §8.8 wants the
    next request in full.
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
        if doc and claimed and any(stage.key in claimed for stage in record.stages):
            if record.connected_doc and record.connected_doc != doc:
                for stage in record.stages:
                    if stage.key in claimed:
                        _bump_generation(record, stage, "document changed")
            if record.connected_doc != doc:
                record.connected_doc = doc
        for stage in record.stages:
            if stage.state == stack_mod.STATE_WAITING:
                _forget(record.stack_id, stage.key)
                _set(stage, "state", stack_mod.STATE_IDLE)
            _set(stage, "geom_hash", "")
        _dirty.add(obj.name)


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

                print("[gh-stack] schema error:\n%s" % traceback.format_exc(), flush=True)
                continue
            # A changed schema is a changed definition: frames cached against
            # the old one must not replay.
            _bump_generation(record, stage, "schema changed")
            _dirty.add(obj.name)
            obj.update_tag()
    if not matched:
        # A schema for a stage no object has: usually a key pasted into GH that
        # does not belong to any stack, or a stage removed in Blender.
        print("[gh-stack] schema for unknown stage %r ignored" % (key,), flush=True)


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
            _bump_generation(record, stage, "Grasshopper's answer changed")
        _remember(record, stage, rid, header, sections)
        _store_frame(record, stage, rid, header, sections)

        was_pending = rid in _pending_of(record.stack_id, key)
        _forget(record.stack_id, stage.key)
        applied = False
        if rid == stage.rid and out_hash != stage.applied_out_hash:
            _apply(obj, stage, sections)
            stats["applies"] += 1
            _set(stage, "applied_out_hash", out_hash)
            applied = True
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
            # evaluate the frame the scene is actually on.
            _set(stage, "state", stack_mod.STATE_IDLE)
        else:
            _set(stage, "state", stack_mod.STATE_OK)
        _set(stage, "error", "")
        if applied:
            stats["tags"] += 1
            obj.update_tag()
        if applied or was_pending:
            _dirty.add(obj.name)


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
            print("[gh-stack] unknown_ref for unknown stage %r" % (header.get("stage"),), flush=True)
        return

    print("[gh-stack] protocol error %s: %s" % (code, message), flush=True)
    for obj in bpy.data.objects:
        record = stack_mod.stack_of(obj)
        if record is None or not record.stages:
            continue
        stage = stack_mod.stage_of(obj, header.get("stage"))
        if stage is None:
            continue
        _set(stage, "error", "%s: %s" % (code, message))
        _dirty.add(obj.name)


def _resend_in_full(obj, record, stage, reason):
    """Grasshopper no longer holds this stage's geometry, so the next request
    must carry it. Shared by the `unknown_ref` error message, the response-shaped
    `unknown_ref` and the request timeout, because all three leave the same
    uncertainty behind (protocol §4.3, §8.8)."""
    _forget(record.stack_id, stage.key)
    _set(stage, "geom_hash", "")
    _set(stage, "state", stack_mod.STATE_IDLE)
    _set(stage, "error", "")
    _dirty.add(obj.name)
    print("[gh-stack] %s: resending geometry (%s)" % (stage.key, reason), flush=True)


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


def _apply(obj, stage, sections):
    """Write the response geometry into the stage's helpers, one per kind.

    A section group that is absent means "none": that helper is cleared, so a
    definition that stops returning curves stops showing them.
    """
    nodes.ensure_group(obj, stage.key)
    groups = {"mesh": [], "curves": [], "points": []}
    for name, values in sections.items():
        if name.startswith("mesh.") or name.startswith("subd."):
            groups["mesh"].append((name, values))
        elif name.startswith("crv."):
            groups["curves"].append((name, values))
        elif name.startswith("pts."):
            groups["points"].append((name, values))

    _apply_mesh(obj, stage, groups["mesh"])
    _apply_curves(obj, stage, groups["curves"])
    _apply_points(obj, stage, groups["points"])


def _apply_mesh(obj, stage, entries):
    helper = helpers.ensure_helper(obj, stage.key, "mesh")
    mesh = helper.data
    if not entries:
        if len(mesh.vertices) or len(mesh.polygons):
            mesh.clear_geometry()
            helper.update_tag()
        return
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


def _apply_curves(obj, stage, entries):
    helper = helpers.ensure_helper(obj, stage.key, "curves")
    if not entries:
        if len(helper.data.splines):
            helper.data.splines.clear()
            helper.update_tag()
        return
    prepared = []
    for name, values in entries:
        arr = np.asarray(values)
        if name == "crv.points":
            arr = arr.reshape(-1, 4)
        prepared.append((name, _dtype_of(name), arr))
    geometry_io.sections_to_curve(helper.data, prepared)
    helper.update_tag()


def _apply_points(obj, stage, entries):
    helper = helpers.ensure_helper(obj, stage.key, "points")
    if not entries:
        if len(helper.data.points):
            helper.data.resize(0)
            helper.update_tag()
        return
    prepared = []
    for name, values in entries:
        arr = np.asarray(values)
        if name == "pts.positions":
            arr = arr.reshape(-1, 3)
        elif name == "pts.colors":
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


def _bump_generation(record, stage, reason=""):
    """A reset: old frames describe the wrong definition and are deleted."""
    stage.generation += 1
    _cache.pop((record.stack_id, stage.key), None)
    try:
        removed = _store_for(record, stage).prune(generation=stage.generation)
    except OSError as exc:
        removed = []
        print("[gh-stack] cache reset failed for %s: %r" % (stage.key, exc), flush=True)
    if removed:
        print(
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
        print("[gh-stack] could not store a frame for %s: %r" % (stage.key, exc), flush=True)
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
        print("[gh-stack] cache write failed for %s: %r" % (stage.key, exc), flush=True)


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
    total = 0
    for _header, sections in cache.values():
        for name, values in sections.items():
            try:
                total += len(values) * (1 if _dtype_of(name) == "u8" else 4)
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
        print("[gh-stack] cache read failed for %s: %r" % (stage.key, exc), flush=True)
        return None
    if raw is None:
        return None
    try:
        header, sections = codec.decode_frame(raw)
    except codec.CodecError as exc:
        print("[gh-stack] dropping a corrupt cached frame for %s: %r" % (stage.key, exc), flush=True)
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
    busy = False
    for stage in record.stages:
        outcome = _process_stage(obj, record, stage)
        if outcome == "waiting":
            # Later stages would run on a prefix that is not final yet (§9.1.5).
            busy = True
            break
        if outcome == "busy":
            busy = True
    return busy


def _process_stage(obj, record, stage):
    """One stage's pass. Returns "waiting" (request in flight; stop the stack),
    "busy" (work happened; keep the fast timer), or "idle"."""
    if not stage.enabled:
        _set(stage, "state", "disabled")
        nodes.set_modifier_visible(obj, stage.key, False)
        return "idle"
    nodes.set_modifier_visible(obj, stage.key, True)

    sections = _prefix_sections(obj, record, stage)
    raw = {name: codec.section_bytes(name, dtype, values) for name, dtype, values in sections}
    geom_hash = hashing.geometry_hash(raw)
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

    cached = _cached_response(record, stage, rid)
    if cached is not None:
        header, cached_sections = cached
        out_hash = header.get("out_hash") or ""
        _set(stage, "rid", rid)
        if out_hash != stage.applied_out_hash:
            _apply(obj, stage, cached_sections)
            stats["applies"] += 1
            _set(stage, "applied_out_hash", out_hash)
            _set(stage, "warnings", _warnings_text(header))
            _set(stage, "state", stack_mod.STATE_OK)
            _set(stage, "error", "")
            return "busy"
        _set(stage, "state", stack_mod.STATE_OK)
        return "idle"

    if not bridge.state["connected"]:
        # A miss with Grasshopper away keeps the last good output; only cached
        # frames replay (renders use the cache).
        _set(stage, "state", stack_mod.STATE_DISCONNECTED)
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
        # A cached frame never reaches here: the lookup above answers it first, so
        # a replay is not throttled by whatever is outstanding.
        sent_at = next(iter(pending.values()))
        if time.monotonic() - sent_at <= _timeout():
            return "waiting"
        # The answer never came. We cannot know whether Grasshopper received the
        # geometry, so the retry goes out in full — the same reasoning as the
        # unknown_ref handler. And the failure stays visible: retrying silently is
        # what made a stage stuck in this loop look healthy.
        _forget(record.stack_id, stage.key)
        _set(stage, "geom_hash", "")
        stats["timeouts"] += 1
        retry_note = "timed out after %.0f s; requesting the geometry again" % _timeout()

    mode = "full" if geom_hash != stage.geom_hash else "ref"
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
    frame = codec.encode_frame(header, sections if mode == "full" else [])
    if not server.SERVER.send(frame):
        # No client socket to carry it. Recording a pending request here would
        # make the stage wait out a whole timeout for an answer nobody was asked
        # for — and the retry would be gated on that same pending entry.
        stats["undelivered"] += 1
        _set(stage, "state", stack_mod.STATE_DISCONNECTED)
        return "idle"
    if retry_note:
        print("[gh-stack] %s: %s" % (stage.key, retry_note), flush=True)
    stats["sends"] += 1

    _pending_of(record.stack_id, stage.key)[rid] = time.monotonic()
    _set(stage, "rid", rid)
    _set(stage, "geom_hash", geom_hash)
    _set(stage, "state", stack_mod.STATE_WAITING)
    _set(stage, "error", retry_note)
    return "waiting"


# ------------------------------------------------------------------ prefix cache


def _prefix_sections(obj, record, stage):
    """The geometry a stage sees: everything before its own modifier in the
    stack (plan §9.1), so preceding native modifiers and earlier GH stages are
    part of the input.

    With nothing before the stage, the base datablocks are read directly: exact,
    and no clone evaluation. Otherwise the evaluated prefix is memoized against
    a revision of everything it depends on; when the revision is unchanged the
    clone (which would tag the depsgraph and wake every idle stack) is skipped.
    """
    modifier = nodes.modifier_for(obj, stage.key)
    index = _modifier_index(obj, modifier)
    if index <= 0:
        return geometry_io.object_to_sections(obj, as_subd=bool(record.subd))

    revision = _prefix_revision(obj, record, stage, index)
    # Keyed by identity, not by name: an object renamed to a name another object
    # used to hold would otherwise inherit what that object had evaluated.
    # as_pointer() is stable for the session and keeps no reference to the object,
    # so a deleted object does not live on through this dict.
    memo_key = (obj.as_pointer(), stage.key)
    memo = _prefix_memo.get(memo_key)
    if revision is not None and memo is not None and memo["revision"] == revision:
        stats["prefix_memo_hits"] += 1
        return memo["sections"]

    global _evaluating
    clone = obj.copy()
    _evaluating = True
    try:
        bpy.context.scene.collection.objects.link(clone)
        for later in list(clone.modifiers)[index:]:
            clone.modifiers.remove(later)
        depsgraph = bpy.context.evaluated_depsgraph_get()
        geometry = clone.evaluated_get(depsgraph).evaluated_geometry()
        sections = geometry_io.geometry_set_to_sections(geometry, as_subd=bool(record.subd))
    finally:
        _evaluating = False
        bpy.data.objects.remove(clone, do_unlink=True)
    stats["prefix_clones"] += 1
    if revision is not None:
        _prefix_memo[memo_key] = {"revision": revision, "sections": sections}
    return sections


def _prefix_revision(obj, record, stage, index):
    """Everything the evaluated prefix depends on, cheaply comparable.

    **This list must name every input the evaluated prefix reads.** The memo in
    :func:`_prefix_sections` trusts it, so an input that is missing here is an
    input whose change goes unnoticed: the stage then sends Grasshopper geometry
    that no longer matches the scene, with nothing to show for it. Two of them are
    read at solve time rather than stored on the modifier, which makes them easy
    to miss:

    - **the frame** (``scene.frame_float``), because anything in the prefix that
      reads the timeline — a Scene Time node, a simulation zone, a driver
      expression — moves with it. A prefix that moves has to produce a new
      geometry hash, so the frame cache can key each frame separately; without
      this the same request ID came back for every frame and the cache answered
      them all with one frame's geometry;
    - **the object identity**, which is in the memo key rather than the tuple
      (see :func:`_prefix_sections`).

    ``None`` means "do not memoize": surfaces are meshed on the fly and rarely
    used, and a truncated node-tree signature (see :func:`_node_tree_signature`)
    must not be trusted.
    """
    if obj.type == "SURFACE":
        return None
    base = geometry_io.object_to_sections(obj, as_subd=False)
    raw = {name: codec.section_bytes(name, dtype, values) for name, dtype, values in base}
    descriptor = _modifier_descriptor(obj, index)
    if descriptor is None:
        return None
    return (
        hashing.geometry_hash(raw),
        descriptor,
        _upstream_hashes(obj, record, stage, index),
        bool(record.subd),
        _shapekey_state(obj),
        bpy.context.scene.frame_float,
    )


def _modifier_descriptor(obj, index):
    """Settings of every modifier before the stage, plus the objects they
    reference. Any edit that matters changes this tuple; UI-only state is left
    out so selecting a modifier does not invalidate the memo.

    ``None`` means one of them could not be described completely (a truncated
    node-tree walk); the caller must not memoize against it."""
    items = []
    for modifier in list(obj.modifiers)[:index]:
        props_state = []
        for prop in modifier.bl_rna.properties:
            identifier = prop.identifier
            if prop.is_readonly or identifier in _SKIP_MODIFIER_PROPS:
                continue
            try:
                value = getattr(modifier, identifier)
            except (AttributeError, TypeError, RuntimeError):
                continue
            if prop.type == "POINTER":
                props_state.append((identifier, _pointer_state(value)))
            elif prop.type == "COLLECTION":
                continue
            else:
                props_state.append((identifier, _freeze(value)))
        if modifier.type == "NODES" and modifier.node_group is not None:
            signature = _node_tree_signature(modifier.node_group)
            if signature is None:
                # A truncated signature cannot be trusted to catch every edit;
                # `None` bubbles up as "do not memoize" (see `_prefix_sections`).
                return None
            props_state.append(("node_tree", signature))
        items.append((modifier.name, modifier.type, tuple(props_state)))
    return tuple(items)


def _pointer_state(value):
    if value is None:
        return None
    if getattr(value, "bl_rna", None) is not None and value.bl_rna.identifier == "Object":
        return ("object", value.name, tuple(value.matrix_world), _object_geometry_hash(value))
    return ("id", getattr(value, "name", None))


def _object_geometry_hash(obj):
    try:
        sections = geometry_io.object_to_sections(obj, as_subd=False)
    except Exception:
        return ""
    raw = {name: codec.section_bytes(name, dtype, values) for name, dtype, values in sections}
    return hashing.geometry_hash(raw)


_MAX_NODE_TREE_DEPTH = 32  # backstop for a pathological graph; a hit disables the memo


def _node_tree_signature(tree):
    """Node names, wiring, and every socket default reachable from one node
    group, so an edit inside a prefix modifier's graph invalidates the memo.

    Group-type nodes are walked recursively: a nested group is how a real graph
    is organised, and the node that holds it does not change when the group's
    body does. Blender 5.2 exposes ``node_tree`` on exactly the group node types
    (``NodeGroup``, ``GeometryNodeGroup``, the custom variants, and the shader/
    compositor/texture equivalents); a simulation zone keeps its body in the
    same tree. A recursive group is legal, so every tree is visited once, keyed
    by identity; a repeat is recorded as a reference, not re-walked.

    The depth cap is a backstop: when it is hit the signature is ``None``,
    which the revision treats as "do not memoize". A silently truncated
    signature is the stale-geometry bug this walk exists to prevent.
    """
    return _tree_signature(tree, set(), 0)


def _tree_signature(tree, visited, depth):
    if depth >= _MAX_NODE_TREE_DEPTH:
        stats["signature_truncated"] += 1
        return None
    visited.add(tree.as_pointer())
    nodes_state = []
    for node in tree.nodes:
        sockets = []
        for socket in node.inputs:
            try:
                value = socket.default_value
            except (AttributeError, RuntimeError):
                continue
            if value is None:
                continue
            sockets.append((socket.identifier, _freeze(value)))
        try:
            nested = node.node_tree
        except (AttributeError, RuntimeError):
            nested = None
        subtree = None
        if nested is not None:
            if nested.as_pointer() in visited:
                subtree = ("visited", nested.name)
            else:
                subtree = _tree_signature(nested, visited, depth + 1)
                if subtree is None:
                    return None
        nodes_state.append((node.bl_idname, node.name, tuple(sockets), subtree))
    links = []
    for link in tree.links:
        try:
            links.append(
                (link.from_node.name, link.from_socket.identifier, link.to_node.name, link.to_socket.identifier)
            )
        except AttributeError:
            continue
    links.sort()
    interface = []
    try:
        for item in tree.interface.items_tree:
            if getattr(item, "item_type", "") != "SOCKET":
                continue
            try:
                value = item.default_value
            except AttributeError:
                continue
            if value is None:
                continue
            interface.append((item.identifier, _freeze(value)))
    except (AttributeError, RuntimeError):
        pass
    return (tree.name, tuple(nodes_state), tuple(links), tuple(interface))


def _freeze(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    try:
        if hasattr(value, "__len__") and hasattr(value, "__getitem__"):
            return tuple(_freeze(value[i]) for i in range(len(value)))
    except (TypeError, IndexError, RuntimeError):
        pass
    name = getattr(value, "name", None)
    if name is not None:
        return ("#", str(name))
    return repr(value)


_SKIP_MODIFIER_PROPS = frozenset(
    (
        "rna_type",
        "name",
        "show_expanded",
        "is_active",
        "is_override_data",
        "use_pin_to_last",
        "execution_time",
        "persistent_uid",
    )
)


def _upstream_hashes(obj, record, stage, index):
    """The applied outputs of GH stages whose modifiers precede this stage, in
    stack order: their helpers are what the clone evaluation reads, so a new
    upstream output invalidates the memo."""
    by_group = {}
    for other in record.stages:
        if other.key != stage.key:
            by_group[nodes.group_name(other.key)] = other
    hashes = []
    for modifier in list(obj.modifiers)[:index]:
        if modifier.type != "NODES" or modifier.node_group is None:
            continue
        other = by_group.get(modifier.node_group.name)
        if other is not None:
            hashes.append((other.key, other.applied_out_hash))
    return tuple(hashes)


def _shapekey_state(obj):
    keys = getattr(obj.data, "shape_keys", None)
    blocks = getattr(keys, "key_blocks", None) if keys is not None else None
    if blocks is None:
        return ()
    return tuple((block.name, float(block.value)) for block in blocks)


def _modifier_index(obj, modifier):
    if modifier is None:
        return 0
    for i, candidate in enumerate(obj.modifiers):
        if candidate == modifier:
            return i
    return 0


def _timeout():
    return float(prefs._pref("timeout", 30.0))


# ------------------------------------------------------------------- lifecycle


def start_server(port=None):
    if port is None:
        port = int(prefs._pref("port", 9130))
    return server.SERVER.start(port)


def request_again(obj, stage=None):
    """Force a fresh request without touching the frame cache: the nonce changes
    so the request id cannot collide with a cached one. Used when a stage is
    created or its identity changes."""
    record = stack_mod.stack_of(obj)
    if record is None:
        return
    if not record.stack_id:
        record.stack_id = stack_mod.ensure_stack(obj).stack_id
    stages = [stage] if stage is not None else list(record.stages)
    for item in stages:
        item.nonce += 1
        _forget(record.stack_id, item.key)
        _set(item, "state", stack_mod.STATE_IDLE)
    _dirty.add(obj.name)


def push(obj=None):
    """Force a fresh request for every stage of a stack (increments the nonce)
    and reset its frame cache: Push exists to get past anything cached."""
    obj = obj or bpy.context.object
    if obj is None:
        return
    record = stack_mod.stack_of(obj)
    if record is None:
        return
    if not record.stack_id:
        record.stack_id = stack_mod.ensure_stack(obj).stack_id
    for stage in record.stages:
        _bump_generation(record, stage, "Push")
    request_again(obj)


def _mark_object_dirty(obj):
    _dirty.add(obj.name)


def register():
    global _registered
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
    _prefix_memo.clear()
    for counter in stats:
        stats[counter] = 0
    # A UI-driven parameter write (the choice dropdown) must reach the engine.
    stack_mod.set_param_changed_hook(_mark_object_dirty)
    # Persistent: Blender clears non-persistent timers and handlers on every
    # file load, which would otherwise silence the engine on reopen.
    bpy.app.timers.register(_timer, first_interval=IDLE_INTERVAL, persistent=True)
    bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph)
    bpy.app.handlers.frame_change_post.append(_on_frame_change)
    bpy.app.handlers.load_post.append(_on_load)
    _registered = True
    start_server()


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
    ):
        if handler in handler_list:
            handler_list.remove(handler)
    _registered = False
