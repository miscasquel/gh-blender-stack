"""Stage/stack lookup, lifecycle entry points and register hooks."""

import sys

engine = sys.modules[__package__]


def _rebuild_index():
    """One full-scene scan: every object with stages, and every stage key.

    Lifecycle paths (register, load, undo, a stage being added) invalidate the
    index; message paths call :func:`_ensure_index` and then search only the
    matching stages. The old handlers walked every object in the blend for each
    response and schema heartbeat.
    """
    engine._stage_index.clear()
    del engine._stack_objects[:]
    engine._index_object_count = len(engine.bpy.data.objects)
    for obj in engine.bpy.data.objects:
        record = engine.stack_mod.stack_of(obj)
        if record is None or not len(record.stages):
            continue
        engine._stack_objects.append(obj)
        for stage in record.stages:
            if not stage.key:
                continue
            objects = engine._stage_index.setdefault(stage.key, [])
            if not any(other is obj for other in objects):
                objects.append(obj)
    engine._index_dirty = False
    engine.stats["index_rebuilds"] += 1


def _ensure_index():
    if engine._index_dirty or len(engine.bpy.data.objects) != engine._index_object_count:
        engine._rebuild_index()


def _reindex_object(obj):
    """Patch one object after its stage list or one of its keys changed."""
    try:
        record = engine.stack_mod.stack_of(obj)
    except ReferenceError:
        engine._index_dirty = True
        return
    if obj in engine._stack_objects:
        del engine._stack_objects[engine._stack_objects.index(obj)]
    for key in list(engine._stage_index):
        objects = engine._stage_index[key]
        objects[:] = [other for other in objects if other is not obj]
        if not objects:
            del engine._stage_index[key]
    if record is None or not len(record.stages):
        return
    engine._stack_objects.append(obj)
    for stage in record.stages:
        if stage.key:
            engine._stage_index.setdefault(stage.key, []).append(obj)


def _stage_views(key):
    """Yield ``(object, record, stage)`` for every live stage with this key."""
    engine._ensure_index()
    for obj in list(engine._stage_index.get(key, ())):
        try:
            record = engine.stack_mod.stack_of(obj)
            stage = engine.stack_mod.stage_of(obj, key) if record is not None else None
        except ReferenceError:
            engine._index_dirty = True
            continue
        if record is not None and stage is not None:
            yield obj, record, stage


def _all_stack_objects():
    """Every object with a stage, refreshing the index if a datablock vanished."""
    engine._ensure_index()
    for obj in list(engine._stack_objects):
        try:
            record = engine.stack_mod.stack_of(obj)
        except ReferenceError:
            engine._index_dirty = True
            continue
        if record is not None and len(record.stages):
            yield obj


def _mark_dirty_all():
    for obj in engine._all_stack_objects():
        engine._dirty.add(obj.name)


def _forget_prefix(obj, stage_key):
    """Drop one stage's memoized prefix so the next pass re-evaluates it.

    The memo caches the evaluated *sections* under a revision, so anything that
    makes a revision untrustworthy has to retire the entry. Push in particular is
    documented as the way to get past anything cached: before this it reset only
    the frame store, so a stale prefix survived it and the re-request went out
    carrying the same geometry.
    """
    if obj is not None:
        engine._prefix_memo.pop((obj.as_pointer(), stage_key), None)


def _duplicate_owner(obj, stage_key):
    """The object that keeps a shared stage key, or None when this one owns it.

    A stage key is generated once per stage, so two objects carrying the same
    key are a duplicated stack (object duplication copies the record, the
    modifier and its group verbatim). One of them has to give up the key or both
    keep rebuilding the same node group and Grasshopper cannot tell them apart.

    The owner is the lexicographically first object name. Blender names the
    duplicate ``<original>.001``, so the original keeps its key and the copy gets
    a new one; the rule is deterministic across ticks and sessions, and does not
    depend on which object happened to be dirty first.
    """
    owner = obj
    for other, _record, other_stage in engine._stage_views(stage_key):
        if other is obj or other.name >= owner.name:
            continue
        if engine._stage_has_modifier(other, other_stage):
            owner = other
    return owner if owner is not obj else None


def _split_stage(obj, stage):
    """Give one stage of a duplicated object its own key and parameter namespace.

    The caller has already given the object a fresh ``stack_id``. The node group,
    helpers and Grasshopper wiring are rebuilt by the normal repair path on this
    same pass; here the stage record and its parameter properties are moved
    together, so values and animation survive the split.
    """
    old_key = stage.key
    new_key = engine.stack_mod.duplicate_stage_key(old_key, obj.name)
    engine._forget_prefix(obj, old_key)
    engine.props.move_stage_params(obj, old_key, new_key)
    stage.key = new_key
    for entry in stage.params:
        entry.stage_key = new_key
    # A new key has no document that has answered it yet (plan §9.2).
    engine._set(stage, "state", engine.stack_mod.STATE_UNBOUND)
    engine._set(stage, "rid", "")
    engine._set(stage, "geom_hash", "")
    engine._set(stage, "applied_out_hash", "")
    engine._set(stage, "error", "")
    engine._set(stage, "warnings", "")
    engine._set(stage, "nonce", 0)
    engine._set(stage, "generation", 0)


def _split_duplicate_stages(obj, record):
    """Split a duplicated stack off every stage key it shares with another object.

    Runs on each pass before the stages are processed, and on load before the
    repair loop. The duplicate gets a new stack namespace, its stages get new
    keys, and each property carrying a parameter moves with its stage. The
    shared group is rebuilt for its owner before the copy's old helpers are
    removed: the last pass may have pointed it at this object's helpers.
    Returns True when something split.
    """
    splits = []
    for stage in record.stages:
        if not stage.key or not engine._stage_has_modifier(obj, stage):
            continue
        owner = engine._duplicate_owner(obj, stage.key)
        if owner is not None:
            splits.append((stage, owner))
    if not splits:
        return False
    record.stack_id = engine.stack_mod.new_stack_id()
    if any(
        name.startswith(engine.stack_mod.stage_prefix(stage.key))
        for stage, _owner in splits
        for name in obj.keys()
    ):
        # Only pay for (and decouple) the action when parameter paths move.
        engine.props.detach_animation(obj)
    for stage, owner in splits:
        old_key = stage.key
        engine._split_stage(obj, stage)
        engine.nodes.ensure_group(owner, old_key)
        # A reused object name can find helpers left behind by an earlier
        # incarnation of the copy; an unbound stage must start empty, not show
        # that output until Grasshopper answers.
        for key in (old_key, stage.key):
            engine.helpers.remove_helper(obj, key)
            for index in list(engine.helpers.definition_collections(obj, key)):
                engine.helpers.remove_definition(obj, key, index)
        engine._trace.append("%s: split stage %s -> %s" % (obj.name, old_key, stage.key))
    engine._reindex_object(obj)
    obj.update_tag()
    return True


def _process_object(obj):
    record = engine.stack_mod.stack_of(obj)
    if record is None or not record.stages:
        return False
    if not record.stack_id:
        record.stack_id = engine.stack_mod.ensure_stack(obj).stack_id
    busy = engine._drop_removed_stages(obj, record)
    if engine._split_duplicate_stages(obj, record):
        busy = True
    engine._prefix_batch.clear()
    engine._prefix_batch_built = False
    engine._prefix_batching = True
    try:
        for stage in record.stages:
            outcome = engine._process_stage(obj, record, stage)
            if outcome == "waiting":
                # Later stages would run on a prefix that is not final yet (§9.1.5).
                busy = True
                break
            if outcome == "busy":
                busy = True
    finally:
        engine._prefix_batching = False
        engine._prefix_batch.clear()
        engine._prefix_batch_built = False
    return busy


def _drop_removed_stages(obj, record):
    """Removing a stage is deleting its modifier in the modifiers panel (§9.8):
    the modifier is the stage's anchor in the stack, so a stage whose modifier is
    gone is removed here — helpers, block definitions and parameter properties
    included. Cache entries are kept. Returns True when something was removed.

    A missing *node group* is not removal: ``modifier_of`` finds the modifier by
    its persistent uid and the group is rebuilt by ``_repair_plumbing``."""
    removed = False
    for index in range(len(record.stages) - 1, -1, -1):
        stage = record.stages[index]
        if engine.nodes.modifier_of(obj, stage) is not None:
            continue
        if not stage.modifier_uid and engine.nodes.modifier_by_name(obj, stage) is not None:
            # A record from before the uid existed, whose node group is gone
            # too: the modifier is there but has nothing to match on. Keep the
            # stage — removal is data loss, and the pass repairs it.
            continue
        key = (record.stack_id, stage.key)
        engine._forget(record.stack_id, stage.key)
        engine._sent.pop(key, None)
        engine._cache.pop(key, None)
        engine._cache_bytes.pop(key, None)
        engine._stores.pop(key, None)
        engine._state_since.pop(key, None)
        engine._timings.pop(key, None)
        engine._last_no_send.pop(key, None)
        engine._pull_permits.discard(key)
        engine._verify.discard(key)
        engine._forget_prefix(obj, stage.key)
        engine.helpers.remove_helper(obj, stage.key)
        for definition in list(engine.helpers.definition_collections(obj, stage.key)):
            engine.helpers.remove_definition(obj, stage.key, definition)
        engine.stack_mod.clear_stage_params(obj, stage.key)
        engine.nodes.remove_orphan_group(stage.key)
        record.stages.remove(index)
        removed = True
    if removed:
        engine._reindex_object(obj)
        obj.update_tag()
    return removed


def _repair_plumbing(obj, record, stage, modifier):
    """Rebuild plumbing a saved file (or the outliner) lost: the node group and
    the helper objects. A rebuilt stage is marked unbound until an answer
    arrives, and its applied hash is cleared so a cached frame with the same
    out_hash is re-applied instead of skipped over empty helpers (plan §9.7).
    Returns True when something was rebuilt."""
    rebuilt = False
    if engine.helpers.missing_kinds(obj, stage.key):
        engine.helpers.ensure_helpers(obj, stage.key)
        modifier.node_group = engine.nodes.ensure_group(obj, stage.key)
        rebuilt = True
    elif engine.nodes.bind_modifier(obj, stage, modifier):
        rebuilt = True
    if stage.modifier_uid != modifier.persistent_uid:
        # Adopt the uid of the modifier actually found: an old record has none,
        # and a stale one (a record restored by undo, a duplicate's copied uid)
        # follows the modifier that is really there.
        stage.modifier_uid = modifier.persistent_uid
    if rebuilt:
        engine._forget(record.stack_id, stage.key)
        engine._set(stage, "applied_out_hash", "")
        if stage.state != engine.stack_mod.STATE_UNBOUND:
            engine._set(stage, "state", engine.stack_mod.STATE_UNBOUND)
        engine.prefs.log("[stageloom] %s: plumbing rebuilt, marked unbound" % stage.key, flush=True)
    return rebuilt


def start_server(port=None):
    if port is None:
        port = int(engine.prefs._pref("port", engine.prefs.DEFAULT_PORT))
    return engine.server.SERVER.start(port)


def request_again(obj, stage=None, skip_frozen=False):
    """Force a fresh request without touching the frame cache: the nonce changes
    so the request id cannot collide with a cached one. Used when a stage is
    created or its identity changes, and by Pull. The memoized prefix is retired
    with it: a fresh request must carry what the scene shows.

    The target is flushed first when it is in Edit Mode: a request built from
    the stale datablock is a `ref` claiming Grasshopper's copy is current, which
    is how Push after a stroke on a non-active edit object recovered nothing.
    """
    record = engine.stack_mod.stack_of(obj)
    if record is None:
        return
    engine._flush_object(obj)
    if not record.stack_id:
        record.stack_id = engine.stack_mod.ensure_stack(obj).stack_id
    stages = [stage] if stage is not None else list(record.stages)
    for item in stages:
        if skip_frozen and item.frozen:
            continue
        item.nonce += 1
        engine._forget(record.stack_id, item.key)
        engine._forget_prefix(obj, item.key)
        if item.frozen:
            # Pull on a frozen stage: one request, then parked again.
            engine._pull_permits.add((record.stack_id, item.key))
            engine._set(item, "state", engine.stack_mod.STATE_FROZEN)
        else:
            engine._set(item, "state", engine.stack_mod.STATE_IDLE)
    engine._reindex_object(obj)
    engine._dirty.add(obj.name)
    engine._mark_hot()


def freeze(obj, stage):
    """Freeze a stage: stop requests and cache replay; the helpers keep their
    last output. An answer already in flight is forgotten (and still cached when
    it lands); Pull refreshes on demand while frozen."""
    record = engine.stack_mod.stack_of(obj)
    if record is None:
        return
    engine._pull_permits.discard((record.stack_id, stage.key))
    engine._forget(record.stack_id, stage.key)
    engine._set(stage, "rid", "")
    engine._set(stage, "error", "")
    engine._set(stage, "state", engine.stack_mod.STATE_FROZEN)


def unfreeze(obj, stage):
    """Resume a frozen stage: the next pass replays a cached frame or asks
    Grasshopper again, whichever the current request id resolves to."""
    record = engine.stack_mod.stack_of(obj)
    if record is not None:
        engine._pull_permits.discard((record.stack_id, stage.key))
    engine._set(stage, "state", engine.stack_mod.STATE_IDLE)
    engine._dirty.add(obj.name)
    engine._mark_hot()


def push(obj=None):
    """Force a fresh request for every live stage of a stack (increments the
    nonce) and reset its frame cache: Push exists to get past anything cached.
    Frozen stages are parked and skipped entirely (plan §9.8)."""
    obj = obj or engine.bpy.context.object
    if obj is None:
        return
    record = engine.stack_mod.stack_of(obj)
    if record is None:
        return
    if not record.stack_id:
        record.stack_id = engine.stack_mod.ensure_stack(obj).stack_id
    for stage in record.stages:
        if stage.frozen:
            continue
        engine._bump_generation(obj, record, stage, "Push")
    engine.request_again(obj, skip_frozen=True)


def _mark_object_dirty(obj):
    engine._dirty.add(obj.name)
    engine._mark_hot()

