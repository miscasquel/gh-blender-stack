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

from ..core import attributes as attributes_mod
from ..core import cache as frame_cache
from ..core import codec, hashing
from . import bridge, geometry_io, helpers, nodes, prefs, props, server, stack as stack_mod

WAIT_INTERVAL = 0.005
IDLE_INTERVAL = 0.05
# While the scene is being edited the fast cadence applies instead of the idle
# one: an edit keeps the engine hot for HOT_SECONDS, so a reaction costs one tick
# rather than up to one idle interval. Playback fills frames this way — each
# answer re-dirties the stack and the next frame goes out at once — and a quiet
# scene costs nothing, because `_dirty` is emptied on every pass.
HOT_INTERVAL = 0.005
HOT_SECONDS = 1.0

# How long a stage may wait for its outstanding answer before the request is given
# up on and retried in full. See `_timeout()`.

_cache = {}  # (stack_id, stage_key) -> {rid: (header, sections)}; the hot path
_stores = {}  # (stack_id, stage_key) -> ResponseStore; the backing store
_prefix_memo = {}  # (obj.as_pointer(), stage key) -> {"revision": tuple, "sections": list}
# Blender's own invalidation: one counter per datablock, (type, name) -> int, moved
# by `_bump_epochs` for everything the depsgraph tags and by `_bump_id` for the
# engine's own writes. The prefix revision reads the counters of the names it
# consults, so a changed input retires the memo without anyone maintaining a
# description of what that input *is*. Names, not pointers, because the update
# list resolves to evaluated IDs whose addresses are not the originals'.
_epochs = {}
# (stack_id, stage_key) -> {rid: monotonic seconds it was sent}. At most one entry:
# the evaluation in flight. One at a time, deliberately — a frame that arrives while
# an answer is outstanding is not queued behind it, because Grasshopper solves in
# arrival order on one thread and a queue of frames the user has already scrubbed
# past only keeps the client busy after they have moved on. The frame the playhead
# is on is asked for on the first pass after the answer lands, and every answer is
# stored either way, so nothing evaluated is wasted.
_pending = {}
_dirty = set()  # object names that need a pass
_waiting_objects = set()  # object names with a request in flight (stall retry)
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
        return
    _bump_epochs(depsgraph)
    _mark_dirty_all()
    _mark_hot()


@bpy.app.handlers.persistent
def _on_frame_change(scene, depsgraph=None):
    # A frame change tags nothing, which is why the frame is part of the prefix
    # revision and this handler exists separately from the depsgraph one.
    _mark_dirty_all()
    _mark_hot()


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
    _state_since.clear()
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
    _mark_hot()


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

                print("[gh-stack] engine error:\n%s" % traceback.format_exc(), flush=True)
            _dirty.discard(name)
        if busy:
            return WAIT_INTERVAL
        if time.monotonic() < _hot_until:
            return HOT_INTERVAL
        return IDLE_INTERVAL
    except Exception as exc:
        bridge.state["error"] = "engine error: %r" % (exc,)
        import traceback

        print("[gh-stack] engine error:\n%s" % traceback.format_exc(), flush=True)
        return IDLE_INTERVAL


def _nudge_stalled_waits():
    """Mark a stack object dirty when one of its stages has waited past the
    timeout, so the retry path in `_process_stage` runs without the user
    touching anything.

    The timeout itself lives in the per-stage pass, which only runs for dirty
    objects; a lost answer (a disconnect, a dropped frame) dirties nothing, so
    a waiting stage could sit invisible until the next edit. Checked at most
    once a second.
    """
    global _last_stall_check
    now = time.monotonic()
    if now - _last_stall_check < 1.0:
        return
    _last_stall_check = now
    for name in list(_waiting_objects):
        obj = bpy.data.objects.get(name)
        if obj is None:
            _waiting_objects.discard(name)
            continue
        record = stack_mod.stack_of(obj)
        if record is None or not record.stages:
            _waiting_objects.discard(name)
            continue
        waiting = False
        for stage in record.stages:
            pending = _pending.get((record.stack_id, stage.key))
            if not pending:
                continue
            waiting = True
            if now - min(pending.values()) > _timeout():
                _dirty.add(name)
                _mark_hot()
        if not waiting:
            _waiting_objects.discard(name)


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
                        _bump_generation(obj, record, stage, "document changed")
            if record.connected_doc != doc:
                record.connected_doc = doc
        for stage in record.stages:
            if stage.state == stack_mod.STATE_WAITING:
                _forget(record.stack_id, stage.key)
                _set(stage, "state", stack_mod.STATE_IDLE)
            _set(stage, "geom_hash", "")
        _dirty.add(obj.name)
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

                print("[gh-stack] schema error:\n%s" % traceback.format_exc(), flush=True)
                continue
            # A changed schema is a changed definition: frames cached against
            # the old one must not replay.
            _bump_generation(obj, record, stage, "schema changed")
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
            _bump_generation(obj, record, stage, "Grasshopper's answer changed")
        _remember(record, stage, rid, header, sections)
        _store_frame(record, stage, rid, header, sections)

        was_pending = rid in _pending_of(record.stack_id, key)
        _forget(record.stack_id, stage.key)
        applied = False
        if rid == stage.rid and out_hash != stage.applied_out_hash:
            _apply(obj, stage, sections, header.get("attributes"))
            stats["applies"] += 1
            _set(stage, "applied_out_hash", out_hash)
            applied = True
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
                print(
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
        print(
            "[gh-stack] %sinst.* rows are incomplete (ref_def=%s, transforms=%s); skipped"
            % (prefix, refs is not None, mats is not None),
            flush=True,
        )
        return []
    matrices = np.asarray(mats, dtype=np.float32).reshape(-1, 16)
    if len(refs) != len(matrices):
        print(
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
            print(
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
            print(
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
            print("[gh-stack] %s: %s" % (stage.key, note), flush=True)
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

    sections, geom_hash, attributes, definitions = _prefix_sections(obj, record, stage)
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
            _apply(obj, stage, cached_sections, header.get("attributes"))
            stats["applies"] += 1
            _set(stage, "applied_out_hash", out_hash)
            _set(stage, "warnings", _warnings_text(header))
            _set(stage, "state", stack_mod.STATE_OK)
            _set(stage, "error", "")
            return "busy"
        # The cached frame is the displayed one, but the group may predate the
        # current format: repairing it here is what lets a replay fix a stale
        # session (see _handle_response).
        nodes.ensure_group(obj, stage.key, _nested_rows_of(cached_sections))
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
    # Descriptors describe the attribute sections, so they travel only with the
    # sections they describe (a `ref` request carries none of either). The
    # definition table is identity-relevant (it is hashed into the request), so
    # it follows the same rule.
    if mode == "full" and attributes:
        header["attributes"] = attributes
    if mode == "full" and definitions:
        header["definitions"] = definitions
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

    now = time.monotonic()
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
    """The geometry a stage sees: everything before its own modifier in the
    stack (plan §9.1), so preceding native modifiers and earlier GH stages are
    part of the input. Returns ``(sections, geometry hash, attribute
    descriptors, definitions)`` — the descriptors and the definition table
    belong to the sections and travel with them, never on their own.

    Both shapes are memoized against a revision of the datablocks they read (see
    :func:`_prefix_revision`): with nothing before the stage the input is the base
    datablocks, read directly and no clone evaluation; otherwise it is the
    evaluated prefix, and the clone that produces it would tag the depsgraph and
    wake every idle stack. When the revision is unchanged the read and the clone
    are both skipped, and the stored hash means the pass does not pay for
    re-serializing either.
    """
    modifier = nodes.modifier_for(obj, stage.key)
    index = _modifier_index(obj, modifier)
    revision = _prefix_revision(obj, record, index)
    # Keyed by identity, not by name: an object renamed to a name another object
    # used to hold would otherwise inherit what that object had evaluated.
    # as_pointer() is stable for the session and keeps no reference to the object,
    # so a deleted object does not live on through this dict.
    memo_key = (obj.as_pointer(), stage.key)
    memo = _prefix_memo.get(memo_key)
    if revision is not None and memo is not None and memo["revision"] == revision:
        stats["prefix_memo_hits"] += 1
        return memo["sections"], memo["geom_hash"], memo["attributes"], memo["definitions"]

    subd = bool(record.subd)
    if index <= 0:
        # Nothing before the stage: the base datablocks are the input.
        sections, attributes = geometry_io.object_to_sections(obj, as_subd=subd)
        definitions = []
    else:
        sections, attributes, definitions = _clone_prefix(obj, index, subd)
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
        if revision is not None:
            memo["revision"] = revision
        return sections, geom_hash, attributes, definitions

    geom_hash = _sections_hash(sections, attributes, definitions)
    if revision is not None:
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


def _clone_prefix(obj, index, subd):
    """The evaluated prefix of one stage: the object as it is just before the
    stage's own modifier, which is what everything before the stage has made.

    The copy is linked, evaluated and removed, all inside the ``_evaluating``
    guard so the engine ignores the depsgraph events it causes — including the
    one that arrives *after* the removal: linking and unlinking a copy is our
    change, not the scene's, and Blender reports it as a geometry update for the
    stack object, indistinguishable from a real edit. That report is consumed
    here, while the guard is still up; left alone it retires the memo the engine
    is about to write, once per pass, forever.
    """
    global _evaluating
    clone = obj.copy()
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
        bpy.data.objects.remove(clone, do_unlink=True)
        bpy.context.view_layer.update()
        _evaluating = False
    return sections, attributes, definitions


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


def _prefix_revision(obj, record, index):
    """What the memo compares: the epochs of every datablock the prefix reads.

    Blender's dependency graph already knows what an input is, so this revision
    holds *names* and one counter per name (see :func:`_bump_epochs`) instead of
    a frozen copy of every value, and it reads no geometry at all. The
    difference that matters: a frozen value cannot follow a reference. A node
    tree that reads another object — an Object Info node, a modifier input of
    type Object or Collection — kept sending the geometry from before that object
    moved, because the stored name never changed; an epoch does, because Blender
    tagged the object that moved.

    Three things a counter cannot cover, and are read directly instead:

    - **the frame**, because a frame change tags nothing at all: the one case the
      depsgraph is silent about, and the reason anything that reads the timeline
      (a Scene Time node, a simulation zone, a driver) still moves the revision;
    - **the SubD cage flag**, a stack property that tags nothing (it changes how
      the sections are cut, so it has to be in the key);
    - **the shape keys**, a handful of scalars, cheaper to read than to route
      through a tag.

    ``None`` means "do not memoize": surfaces are meshed on the fly and rarely
    used.
    """
    if obj.type == "SURFACE":
        return None
    return (
        tuple((key, _epochs.get(key, 0)) for key in _prefix_references(obj, index)),
        bool(record.subd),
        _shapekey_state(obj),
        bpy.context.scene.frame_float,
    )


def _prefix_references(obj, index):
    """Every datablock the evaluated prefix reads, as sorted ``(type, name)``.

    **This list must name every reference the prefix can read** — the same oath
    the content revision carried, for the same reason: a reference nobody
    consults is a reference whose changes go unnoticed. What shrank is that only
    the *references* are enumerated now: no geometry is read, no value frozen.
    """
    references = set()
    _add_reference(references, obj)
    for modifier in list(obj.modifiers)[:index]:
        references.update(_modifier_references(modifier))
    return tuple(sorted(references))


def _modifier_references(modifier):
    """The datablocks one modifier reads: its ID pointers, the stored datablock
    values of its group inputs, and the datablocks its node graph names."""
    found = set()
    for prop in modifier.bl_rna.properties:
        if prop.type != "POINTER":
            continue
        try:
            value = getattr(modifier, prop.identifier)
        except (AttributeError, TypeError, RuntimeError):
            continue
        _add_reference(found, value)
    inputs = getattr(getattr(modifier, "properties", None), "inputs", None)
    if inputs is not None:
        for identifier in inputs.keys():
            try:
                value = inputs[identifier].to_dict().get("value")
            except (AttributeError, TypeError, RuntimeError):
                continue
            _add_reference(found, value)
    group = getattr(modifier, "node_group", None)
    if group is not None:
        _node_tree_references(group, found, set())
    return found


def _node_tree_references(tree, found, visited):
    """One node tree's ID-valued socket defaults, recursively into nested groups.

    A group node keeps its body in another tree, so the walk follows it; the
    visited set is what makes a recursive group safe. There is no depth cap: the
    walk collects every reference and is bounded by the graph, and a cap that
    truncated it would silently drop references instead of guarding anything.
    """
    visited.add(tree.as_pointer())
    for node in tree.nodes:
        for socket in node.inputs:
            try:
                value = socket.default_value
            except (AttributeError, RuntimeError):
                continue
            _add_reference(found, value)
        try:
            nested = node.node_tree
        except (AttributeError, RuntimeError):
            nested = None
        if nested is not None and nested.as_pointer() not in visited:
            _node_tree_references(nested, found, visited)
    return found


def _add_reference(found, value, visited=None):
    """One referenced datablock, plus everything whose change alters what it
    contributes: an object's data, and a collection's members (an instanced
    collection *is* its objects — editing one of them must move the revision).

    ``visited`` guards the recursion: a collection can nest another, and an
    object can name a collection (object-level instancing), so a walk without
    it would not terminate on a cycle.
    """
    if not isinstance(value, bpy.types.ID):
        return
    if visited is None:
        visited = set()
    key = _id_key(value)
    if key in visited:
        return
    visited.add(key)
    found.add(key)
    data = getattr(value, "data", None)
    if isinstance(data, bpy.types.ID):
        _add_reference(found, data, visited)
    if isinstance(value, bpy.types.Collection):
        for member in value.all_objects:
            _add_reference(found, member, visited)
    instance_collection = getattr(value, "instance_collection", None)
    if isinstance(instance_collection, bpy.types.ID):
        _add_reference(found, instance_collection, visited)


def _id_key(id_block):
    """The identity the depsgraph speaks in: its type and its name.

    Names, not pointers: the update list resolves to *evaluated* IDs, whose
    addresses are not the originals' — a tagged mesh and the mesh it was copied
    from sit at different addresses — so a pointer match never matches.
    """
    return (id_block.bl_rna.identifier, id_block.name)


def _bump_epochs(depsgraph):
    """Move the counter of every datablock Blender tagged, geometry and
    transform updates only.

    A shading-only entry is bookkeeping, not a change, and the engine's own
    prefix clone produces exactly one for the stack object on the evaluation
    after it runs: linking and unlinking the copy re-syncs the object without
    moving it. Counting that would retire the memo the engine just filled, once
    per pass, and the memo would never hold. Blender states which of the three
    kinds of update each entry is; this reads it rather than guessing.
    """
    try:
        updates = list(depsgraph.updates)
    except (AttributeError, RuntimeError, TypeError):
        return
    for update in updates:
        if not (update.is_updated_geometry or update.is_updated_transform):
            continue
        ident = getattr(update.id.bl_rna, "identifier", None)
        if ident is not None:
            key = (ident, update.id.name)
            _epochs[key] = _epochs.get(key, 0) + 1


def _bump_id(id_block):
    """Move a datablock's counter for the engine's own writes.

    The depsgraph has not seen a write the engine just made when the next stage
    in the same pass looks at its prefix, so the write is stamped here rather
    than waited for. This is what the upstream-output hashes used to do.
    """
    for target in (id_block, getattr(id_block, "data", None)):
        if target is not None:
            key = _id_key(target)
            _epochs[key] = _epochs.get(key, 0) + 1


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


def waiting_seconds(stack_id, stage_key):
    """How long a stage has been waiting for an answer, or 0.0 when it is not.

    Read by the diagnostics panel: an answer outstanding for a second is where a
    sync fault is indistinguishable from a stage that is merely thinking.
    """
    since = _state_since.get((stack_id, stage_key))
    if since is None:
        return 0.0
    return time.monotonic() - since


# ------------------------------------------------------------------- lifecycle


def start_server(port=None):
    if port is None:
        port = int(prefs._pref("port", 9130))
    return server.SERVER.start(port)


def request_again(obj, stage=None):
    """Force a fresh request without touching the frame cache: the nonce changes
    so the request id cannot collide with a cached one. Used when a stage is
    created or its identity changes. The memoized prefix is retired with it: a
    fresh request must carry what the scene shows."""
    record = stack_mod.stack_of(obj)
    if record is None:
        return
    if not record.stack_id:
        record.stack_id = stack_mod.ensure_stack(obj).stack_id
    stages = [stage] if stage is not None else list(record.stages)
    for item in stages:
        item.nonce += 1
        _forget(record.stack_id, item.key)
        _forget_prefix(obj, item.key)
        _set(item, "state", stack_mod.STATE_IDLE)
    _dirty.add(obj.name)
    _mark_hot()


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
        _bump_generation(obj, record, stage, "Push")
    request_again(obj)


def _mark_object_dirty(obj):
    _dirty.add(obj.name)
    _mark_hot()


def register():
    global _registered, _hot_until
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
    _state_since.clear()
    _epochs.clear()
    _hot_until = 0.0
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
