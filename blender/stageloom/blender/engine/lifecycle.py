"""Stage/stack lookup, lifecycle entry points and register hooks."""

import time

import bpy

from .. import helpers, nodes, prefs, props, server
from .. import stack as stack_mod
from . import request as request_mod
from .request import _no_send, _set, _timeout
from .runtime import ENGINE
from .state import _set_state


CONFLICT_PREFIX = "duplicate stage key"


def _claims_for_key(key):
    """Every live stage record claiming ``key``, same-object duplicates included.

    The index names an object once per key, so iterating the record's stages is
    what turns a shared object entry into a real claim count.
    """
    for obj in list(ENGINE.stage_index.get(key, ())):
        try:
            record = stack_mod.stack_of(obj)
        except ReferenceError:
            ENGINE.index_dirty = True
            continue
        if record is None:
            continue
        for stage in record.stages:
            if stage.key == key:
                yield obj, record, stage


def _runtime_split_owner(key, previous_owners):
    """The previous owner when a key has exactly one old and some new claimants.

    Ownership is pointer identity, never a name: the original may have been
    renamed, and a duplicate may get any name. A same-object duplicate, or a
    key with no previous owner, is ambiguous and returns None.
    """
    owner = previous_owners.get(key)
    if owner is None:
        return None
    claims = list(_claims_for_key(key))
    if sum(1 for obj, _record, _stage in claims if obj is owner) != 1:
        return None
    others = []
    for obj, _record, _stage in claims:
        if obj is owner:
            continue
        if not any(obj is seen for seen in others):
            others.append(obj)
    if not others:
        return None
    if any(sum(1 for obj, _record, _stage in claims if obj is seen) != 1 for seen in others):
        return None
    return owner


def _owner_claims_key(owner, key):
    """Whether a recorded owner is still a live claimant of ``key``.

    A removed datablock can leave the last index stale when the scene's object
    count happens to end where it started (duplicate and delete in one edit).
    Reading the owner names that case: it raises, the index is rebuilt, and the
    surviving claimant keeps the key without a split.
    """
    try:
        record = stack_mod.stack_of(owner)
    except ReferenceError:
        ENGINE.index_dirty = True
        return False
    return record is not None and any(stage.key == key for stage in record.stages)


def _conflict_message(key, claims):
    """One message naming the key and every object/stage claiming it."""
    sources = []
    for obj, _record, _stage in claims:
        for source in sources:
            if source[0] is obj:
                source[1] += 1
                break
        else:
            sources.append([obj, 1])
    names = [
        "%s (%d stages)" % (obj.name, count) if count > 1 else obj.name
        for obj, count in sources
    ]
    return "%s %r: claimed by %s; resolve it from the Stageloom panel" % (
        CONFLICT_PREFIX,
        key,
        ", ".join(names),
    )


def _mark_key_conflict(key):
    """Put every stage claiming a conflicted key in the visible safe state.

    The state is ``conflict``; the error text names the claimants as detail.
    Nothing may be sent or applied for the key while it is ambiguous, so the
    transient per-stage state is cleared with it. The stage records stay
    untouched otherwise: resolving one claimant is what gives the others a
    proven owner again.
    """
    claims = list(_claims_for_key(key))
    if not claims:
        return
    message = _conflict_message(key, claims)
    for obj, record, stage in claims:
        stack_id = record.stack_id
        ENGINE.discard_runtime(stack_id, key)
        ENGINE.verify.discard((stack_id, key))
        _forget_prefix(obj, key)
        _set(stage, "rid", "")
        _set_state(stage, stack_mod.STATE_CONFLICT, error=message)


def _clear_conflict_error(stage):
    """Clear the derived error/state left by a conflict, if present."""
    if stage.state != stack_mod.STATE_CONFLICT and not stage.error.startswith(
        CONFLICT_PREFIX
    ):
        return False
    _set(stage, "error", "")
    # The key is no longer conflicted when this runs; the flags decide whether
    # the stage returns to idle, frozen or disabled.
    _set_state(stage, stack_mod.rest_state(stage))
    return True


def _clear_key_conflict(key):
    """Leave the safe conflict state once a key has a proven single owner."""
    for obj, record, stage in _claims_for_key(key):
        if not _clear_conflict_error(stage):
            continue
        if not record.stack_id:
            record.stack_id = stack_mod.ensure_stack(obj).stack_id
        # The stage was silent while ambiguous, and a load/undo may have changed
        # the session: ask once instead of trusting a frame or geometry hash.
        ENGINE.verify.add((record.stack_id, key))
        ENGINE.dirty.add(obj.name)


def _rebuild_index():
    """One full-scene scan: every object with stages, and every stage key.

    Lifecycle paths (register, load, undo, a stage being added) invalidate the
    index; message paths call :func:`_ensure_index` and then search only the
    matching stages. A key is owned either by its single claimant, or by the
    claimant the previous index already owned when every other claimant is new:
    that is a runtime duplication and the new one is split on its next pass.
    Any other shared key is a strict conflict.
    """
    ENGINE.stage_index.clear()
    del ENGINE.stack_objects[:]
    ENGINE.index_object_count = len(bpy.data.objects)
    claims = {}
    all_claims = []
    for obj in bpy.data.objects:
        record = stack_mod.stack_of(obj)
        if record is None or not len(record.stages):
            continue
        ENGINE.stack_objects.append(obj)
        for stage in record.stages:
            if not stage.key:
                continue
            objects = ENGINE.stage_index.setdefault(stage.key, [])
            if not any(other is obj for other in objects):
                objects.append(obj)
            claims[stage.key] = claims.get(stage.key, 0) + 1
            all_claims.append((obj, record, stage))
    previous_owners = dict(ENGINE.stage_owners)
    old_conflicts = set(ENGINE.stage_conflicts)
    ENGINE.stage_claims.clear()
    ENGINE.stage_claims.update(claims)
    owners = {}
    conflicts = set()
    for key, count in claims.items():
        if count == 1:
            owners[key] = ENGINE.stage_index[key][0]
            continue
        owner = _runtime_split_owner(key, previous_owners)
        if owner is not None:
            owners[key] = owner
        else:
            conflicts.add(key)
    ENGINE.stage_owners.clear()
    ENGINE.stage_owners.update(owners)
    ENGINE.stage_conflicts.clear()
    ENGINE.stage_conflicts.update(conflicts)
    ENGINE.index_dirty = False
    ENGINE.stats()["index_rebuilds"] += 1
    for key in old_conflicts - conflicts:
        _clear_key_conflict(key)
    for key in conflicts:
        _mark_key_conflict(key)
    # A file saved while conflicted can carry the derived error after the
    # duplicate was deleted. The conflict set is authoritative: clear stale
    # text on any key that now has a single owner.
    for obj, _record, stage in all_claims:
        if stage.key in conflicts:
            continue
        if _clear_conflict_error(stage):
            obj.update_tag()
    # A deleted stack object cannot run its own cleanup: its helpers,
    # definitions and clouds outlive the record until this sweep names the
    # stack ids that still exist. Only owned plumbing is removed.
    helpers.sweep_orphans()


def _index_has_stale_object():
    """Whether a removed datablock is still held by the index.

    The scene's object count is the cheap trigger, but a replace in one edit
    (remove one stack object, add another) can leave the count unchanged while
    every entry still names a dead pointer. Reading the name is what notices it.
    """
    for obj in ENGINE.stack_objects:
        try:
            obj.name
        except ReferenceError:
            return True
    return False


def _ensure_index():
    if (
        ENGINE.index_dirty
        or len(bpy.data.objects) != ENGINE.index_object_count
        or _index_has_stale_object()
    ):
        _rebuild_index()


def _refresh_key(key):
    """Recompute one key's claims, owner and conflict after an object change."""
    try:
        _ensure_index()
    except ReferenceError:
        ENGINE.index_dirty = True
        return
    claims = list(_claims_for_key(key))
    was_conflict = key in ENGINE.stage_conflicts
    if not claims:
        ENGINE.stage_claims.pop(key, None)
        ENGINE.stage_owners.pop(key, None)
        ENGINE.stage_conflicts.discard(key)
        ENGINE.stage_index.pop(key, None)
        return
    ENGINE.stage_claims[key] = len(claims)
    if len(claims) == 1:
        ENGINE.stage_owners[key] = claims[0][0]
        ENGINE.stage_conflicts.discard(key)
        if was_conflict:
            _clear_key_conflict(key)
        return
    owner = _runtime_split_owner(key, ENGINE.stage_owners)
    if owner is not None:
        ENGINE.stage_owners[key] = owner
        ENGINE.stage_conflicts.discard(key)
        if was_conflict:
            _clear_key_conflict(key)
        return
    ENGINE.stage_owners.pop(key, None)
    ENGINE.stage_conflicts.add(key)
    _mark_key_conflict(key)


def _reindex_object(obj):
    """Patch one object after its stage list or one of its keys changed.

    The index is first brought up to date, then this object's entries are
    replaced. Each touched key gets a real claim recount, including duplicate
    stages on this object that a per-object index entry cannot show.
    """
    try:
        record = stack_mod.stack_of(obj)
    except ReferenceError:
        ENGINE.index_dirty = True
        return
    _ensure_index()
    touched = set()
    if obj in ENGINE.stack_objects:
        del ENGINE.stack_objects[ENGINE.stack_objects.index(obj)]
    for key in list(ENGINE.stage_index):
        objects = ENGINE.stage_index[key]
        if not any(other is obj for other in objects):
            continue
        touched.add(key)
        objects[:] = [other for other in objects if other is not obj]
        if not objects:
            del ENGINE.stage_index[key]
    if record is not None and len(record.stages):
        ENGINE.stack_objects.append(obj)
        seen = set()
        for stage in record.stages:
            if not stage.key:
                continue
            touched.add(stage.key)
            if stage.key in seen:
                continue
            seen.add(stage.key)
            ENGINE.stage_index.setdefault(stage.key, []).append(obj)
    for key in touched:
        _refresh_key(key)


def _stage_key_conflicted(key):
    """Whether no stage may send or apply for this key until the user resolves it."""
    _ensure_index()
    return bool(key) and key in ENGINE.stage_conflicts


def _stage_views(key):
    """Yield ``(object, record, stage)`` for every live stage that may act on a key.

    A conflicted key has no views at all. A key with a proven owner yields only
    that object's stages, so a runtime duplicate cannot receive a message before
    its next pass splits it.
    """
    _ensure_index()
    if not key or key in ENGINE.stage_conflicts:
        return
    owner = ENGINE.stage_owners.get(key)
    if owner is None:
        return
    for obj, record, stage in _claims_for_key(key):
        if obj is owner:
            yield obj, record, stage


def _all_stack_objects():
    """Every object with a stage, refreshing the index if a datablock vanished."""
    _ensure_index()
    for obj in list(ENGINE.stack_objects):
        try:
            record = stack_mod.stack_of(obj)
        except ReferenceError:
            ENGINE.index_dirty = True
            continue
        if record is not None and len(record.stages):
            yield obj


def _mark_dirty_all():
    """Queue every stack object for a pass."""
    for obj in _all_stack_objects():
        ENGINE.dirty.add(obj.name)


def _scene_stack_objects(scene):
    """Every stack object linked into one scene, for a scene-scoped trigger."""
    for obj in _all_stack_objects():
        if scene is None or obj.name in scene.objects:
            yield obj


def _mark_dirty_scene(scene):
    """Queue only the stacks the triggering scene owns.

    A depsgraph or frame event carries its scene. Marking every stack made an
    edit in the active scene wake a stage in an unrelated one, which then asked
    for the active scene's frame. ``None`` keeps the old all-stacks behaviour
    for callers that have no scene (tests, a load-driven full pass).
    """
    for obj in _scene_stack_objects(scene):
        ENGINE.dirty.add(obj.name)


def _forget_prefix(obj, stage_key):
    """Drop one stage's memoized prefix so the next pass re-evaluates it.

    The memo keeps the evaluated *sections* under a revision, so anything that
    makes a revision untrustworthy has to retire the entry. Push in particular is
    the way to get past a stale read: the re-request must carry the geometry the
    scene is showing.
    """
    if obj is not None:
        ENGINE.prefix_memo.pop((obj.as_pointer(), stage_key), None)


def _split_stage(obj, stage, new_key=None):
    """Give one stage its own key and parameter namespace.

    The caller has already given a duplicated object a fresh ``stack_id``. The
    node group, helpers and Grasshopper wiring are rebuilt by the normal repair
    path on this same pass; here the stage record and its parameter properties
    are moved together, so values and animation survive the split.

    A runtime split keeps the deterministic key generator (stable across a redo
    of the same duplication); a user resolution passes a fresh random key
    instead. Either way the candidate is checked against the live claims.
    """
    old_key = stage.key
    if not new_key:
        new_key = stack_mod.duplicate_stage_key(old_key, obj.name)
        if new_key == old_key or ENGINE.stage_claims.get(new_key, 0):
            new_key = None
    while not new_key or new_key == old_key or ENGINE.stage_claims.get(new_key, 0):
        new_key = stack_mod.new_stage_key(stage.label or old_key)
    _forget_prefix(obj, old_key)
    _forget_prefix(obj, new_key)
    props.move_stage_params(obj, old_key, new_key)
    stage.key = new_key
    for entry in stage.params:
        entry.stage_key = new_key
    # A new key has no document that has answered it yet.
    _set_state(stage, stack_mod.STATE_UNBOUND)
    _set(stage, "rid", "")
    _set(stage, "geom_hash", "")
    _set(stage, "applied_out_hash", "")
    _set(stage, "applied_topology", "")
    _set(stage, "error", "")
    _set(stage, "warnings", "")
    _set(stage, "input_warnings", "")
    _set(stage, "nonce", 0)
    # The duplicate is a new claimant: the original's Clear Bake must not
    # unfreeze it, and its own stale token owns no upstream stages.
    _set(stage, "bake_token", "")
    _set(stage, "frozen_by_bake", "")
    return new_key


def _remove_stage_plumbing(obj, keys):
    """Remove this object's helpers and block definitions under ``keys``."""
    for key in keys:
        helpers.remove_helper(obj, key)
        for index in list(helpers.definition_collections(obj, key)):
            helpers.remove_definition(obj, key, index)


def _split_duplicate_stages(obj, record):
    """Split this object's stages off every key a proven owner already holds.

    Runs on each pass before the stages are processed (and from playback). A
    key with a proven owner and a new claimant is a runtime duplication: the
    new claimant gets a new key here, before it can send. A conflicted or
    unproven key is never split; it waits for the user's resolution. The shared
    group is rebuilt for its owner before the copy's old helpers are removed:
    the last pass may have pointed it at this object's helpers.
    Returns True when something split.
    """
    _ensure_index()
    keys = [stage.key for stage in record.stages if stage.key]
    if len(keys) != len(set(keys)):
        # Two stages on this object claim one key. `_stage_index` names the
        # object once, so refresh from the records: this is a real conflict,
        # not something to split by position.
        _reindex_object(obj)
        return False
    splits = []
    for stage in record.stages:
        if not stage.key or not _stage_has_modifier(obj, stage):
            continue
        if stage.key in ENGINE.stage_conflicts:
            continue
        owner = ENGINE.stage_owners.get(stage.key)
        if owner is None or owner is obj:
            continue
        if not _owner_claims_key(owner, stage.key):
            # The snapshot outlived its claimant and the object count hid the
            # change. Rebuild from the scene; the survivor owns the key now.
            _reindex_object(obj)
            return False
        splits.append((stage, owner))
    if not splits:
        return False
    record.stack_id = stack_mod.new_stack_id()
    if any(
        name.startswith(stack_mod.stage_prefix(stage.key))
        for stage, _owner in splits
        for name in obj.keys()
    ):
        # Only pay for (and decouple) the action when parameter paths move.
        props.detach_animation(obj)
    for stage, owner in splits:
        old_key = stage.key
        _split_stage(obj, stage)
        nodes.ensure_group(owner, old_key)
        # A reused object name can find helpers left behind by an earlier
        # incarnation of the copy; an unbound stage must start empty, not show
        # that output until Grasshopper answers.
        _remove_stage_plumbing(obj, (old_key, stage.key))
        ENGINE.trace("%s: split stage %s -> %s" % (obj.name, old_key, stage.key))
    _reindex_object(obj)
    obj.update_tag()
    return True


def resolve_duplicate_key(obj, record, stage):
    """Give one conflicted stage of ``obj`` a fresh key, at the user's request.

    The other claimant keeps the old key. The parameter namespace moves with
    the stage, its request/apply state is reset, and helpers/definitions left
    under either key for this object are removed; the next pass repairs the
    group and asks as usual. Returns the new key, or None when ``stage`` is not
    part of a conflict.
    """
    _ensure_index()
    old_key = stage.key
    if not old_key or old_key not in ENGINE.stage_conflicts:
        return None
    others = []
    for other, _other_record, _other_stage in _claims_for_key(old_key):
        if other is obj or any(other is seen for seen in others):
            continue
        others.append(other)
    # The old key stays with its other claimant(s); rebuild their group before
    # this object's old helpers go away, exactly like a runtime split.
    for other in others:
        nodes.ensure_group(other, old_key)
    if any(
        name.startswith(stack_mod.stage_prefix(old_key)) for name in obj.keys()
    ):
        props.detach_animation(obj)
    new_key = stack_mod.new_stage_key(stage.label or old_key)
    while new_key == old_key or ENGINE.stage_claims.get(new_key, 0):
        new_key = stack_mod.new_stage_key(stage.label or old_key)
    _split_stage(obj, stage, new_key)
    # A reused object name can find helpers left behind by an earlier
    # incarnation; the resolved stage must start empty, not show that output.
    _remove_stage_plumbing(obj, (old_key, new_key))
    _reindex_object(obj)
    ENGINE.trace(
        "%s: resolved duplicate stage key %s -> %s" % (obj.name, old_key, new_key)
    )
    obj.update_tag()
    request_again(obj, stage)
    return new_key


def _ordered_stages(obj, record):
    """Stages in modifier-stack order, with record order as the tie-breaker.

    Dragging a stage modifier is the authoritative reorder control, but the
    record keeps its creation order. Processing the record order made the
    downstream stage look upstream: its prefix was evaluated before the stage
    that actually sits above it in the stack. This is the one stage order: the
    N-panel, its operator indices and the diagnostics panel use it too, so the
    row the user clicks is the stage the modifier stack evaluates there.
    """
    positioned = []
    for record_index, stage in enumerate(record.stages):
        modifier = nodes.modifier_of(obj, stage)
        if modifier is None and not stage.modifier_uid:
            modifier = nodes.modifier_by_name(obj, stage)
        index = len(obj.modifiers)
        if modifier is not None:
            for i, candidate in enumerate(obj.modifiers):
                if candidate == modifier:
                    index = i
                    break
        positioned.append((index, record_index, stage))
    positioned.sort(key=lambda item: (item[0], item[1]))
    return [stage for _index, _record_index, stage in positioned]


def _process_object(obj):
    record = stack_mod.stack_of(obj)
    if record is None or not record.stages:
        return False
    if not record.stack_id:
        record.stack_id = stack_mod.ensure_stack(obj).stack_id
    busy = _drop_removed_stages(obj, record)
    if _split_duplicate_stages(obj, record):
        busy = True
    ENGINE.prefix_batch.clear()
    ENGINE.prefix_batch_built = False
    ENGINE.prefix_batching = True
    try:
        for stage in _ordered_stages(obj, record):
            if stage.key in ENGINE.stage_conflicts:
                # The panel already shows the conflict; nothing may ask or
                # apply for this key until the user resolves one claimant.
                continue
            outcome = request_mod._process_stage(obj, record, stage)
            if outcome == "waiting":
                # Later stages would run on a prefix that is not final yet (§9.1.5).
                busy = True
                break
            if outcome == "busy":
                busy = True
    finally:
        ENGINE.prefix_batching = False
        ENGINE.prefix_batch.clear()
        ENGINE.prefix_batch_built = False
    return busy


def _stage_has_modifier(obj, stage):
    """Whether a stage record still has its modifier (the stage is live)."""
    return nodes.modifier_of(obj, stage) is not None or (
        not stage.modifier_uid and nodes.modifier_by_name(obj, stage) is not None
    )


def _drop_removed_stages(obj, record):
    """Removing a stage is deleting its modifier in the modifiers panel (§9.8):
    the modifier is the stage's anchor in the stack, so a stage whose modifier is
    gone is removed here — helpers, block definitions and parameter properties
    included. Returns True when something was removed.

    A missing *node group* is not removal: ``modifier_of`` finds the modifier by
    its persistent uid and the group is rebuilt by ``_repair_plumbing``."""
    removed = False
    for index in range(len(record.stages) - 1, -1, -1):
        stage = record.stages[index]
        if nodes.modifier_of(obj, stage) is not None:
            continue
        if (
            stage.modifier_uid
            and nodes.modifier_uid_count(obj, stage.modifier_uid) > 1
        ):
            # The uid is shared by more than one modifier, so modifier_of()
            # refuses to guess. Keep the stage: deleting it would turn an
            # ambiguity into data loss.
            _no_send(record, stage, "ambiguous modifier uid")
            continue
        if not stage.modifier_uid:
            candidates = nodes.modifier_name_candidates(obj, stage)
            if candidates:
                # A record from before the uid existed, whose node group is
                # gone too: the modifier is there but has nothing to match on.
                # Keep the stage — removal is data loss, and the pass repairs
                # it. With more than one same-name candidate there is no safe
                # binding, so report the ambiguity instead of guessing.
                if len(candidates) > 1:
                    _set_state(
                        stage,
                        stack_mod.STATE_ERROR,
                        error="ambiguous modifier name: %d unattached modifiers match %r"
                        % (len(candidates), stage.label or stage.key),
                    )
                    _no_send(record, stage, "ambiguous modifier name")
                continue
        key = (record.stack_id, stage.key)
        ENGINE.discard_runtime(*key)
        ENGINE.verify.discard(key)
        _forget_prefix(obj, stage.key)
        helpers.remove_helper(obj, stage.key)
        for definition in list(helpers.definition_collections(obj, stage.key)):
            helpers.remove_definition(obj, stage.key, definition)
        stack_mod.clear_stage_params(obj, stage.key)
        nodes.remove_orphan_group(stage.key)
        record.stages.remove(index)
        removed = True
    if removed:
        _reindex_object(obj)
        obj.update_tag()
    return removed


def _repair_plumbing(obj, record, stage, modifier):
    """Rebuild plumbing a saved file (or the outliner) lost: the node group and
    the helper objects. A rebuilt stage is marked unbound until an answer
    arrives, and its applied hash is cleared so an answer with the same out_hash
    is re-applied instead of skipped over empty helpers.
    Returns True when something was rebuilt."""
    rebuilt = False
    if helpers.missing_kinds(obj, stage.key):
        helpers.ensure_helpers(obj, stage.key)
        modifier.node_group = nodes.ensure_group(obj, stage.key)
        rebuilt = True
    elif nodes.bind_modifier(obj, stage, modifier):
        rebuilt = True
    if stage.modifier_uid != modifier.persistent_uid:
        # Adopt the uid of the modifier actually found: an old record has none,
        # and a stale one (a record restored by undo, a duplicate's copied uid)
        # follows the modifier that is really there.
        stage.modifier_uid = modifier.persistent_uid
    if rebuilt:
        ENGINE.forget(record.stack_id, stage.key)
        _set(stage, "applied_out_hash", "")
        _set(stage, "applied_topology", "")
        # The helpers are empty again: ask for the answer even if the request
        # id and frame have not moved, or the same-frame gate treats the empty
        # display as current.
        ENGINE.verify.add((record.stack_id, stage.key))
        ENGINE.runtime(record.stack_id, stage.key).last_frame = None
        if stage.state != stack_mod.STATE_UNBOUND:
            _set_state(stage, stack_mod.STATE_UNBOUND)
        prefs.log("[stageloom] %s: plumbing rebuilt, marked unbound" % stage.key, flush=True)
    return rebuilt


def start_server(port=None):
    if port is None:
        port = int(prefs._pref("port", prefs.DEFAULT_PORT))
    return server.SERVER.start(port)


def request_again(obj, stage=None, skip_frozen=False):
    """Force a fresh request: the nonce changes so the request id cannot be
    mistaken for the answer to the previous one. Used when a stage is created or
    its identity changes, and by Pull. The memoized prefix is retired with it: a
    fresh request must carry what the scene shows.

    The target is flushed first when it is in Edit Mode: a request built from
    the stale datablock is a `ref` claiming Grasshopper's copy is current, which
    is how Push after a stroke on a non-active edit object recovered nothing.
    """
    record = stack_mod.stack_of(obj)
    if record is None:
        return
    from .triggers import _flush_object

    _flush_object(obj)
    if not record.stack_id:
        record.stack_id = stack_mod.ensure_stack(obj).stack_id
    stages = [stage] if stage is not None else list(record.stages)
    for item in stages:
        if skip_frozen and item.frozen:
            continue
        item.nonce += 1
        ENGINE.forget(record.stack_id, item.key)
        _forget_prefix(obj, item.key)
        if item.frozen:
            # Pull on a frozen stage: one request, then parked again.
            ENGINE.arm_pull(
                record.stack_id, item.key, time.monotonic() + _timeout()
            )
            _set_state(item, stack_mod.STATE_FROZEN)
        else:
            # A disabled stage never leaves disabled; unfreeze/Pull cannot make
            # it idle behind the flag.
            _set_state(item, stack_mod.STATE_IDLE)
    _reindex_object(obj)
    # A new stage (or a Pull) brings the engine back from dormancy.
    ENGINE.ensure_active()
    ENGINE.dirty.add(obj.name)
    ENGINE.mark_hot()


def freeze(obj, stage):
    """Freeze a stage: stop requests; the helpers keep their last output. An
    answer already in flight is forgotten; Pull refreshes on demand while
    frozen."""
    record = stack_mod.stack_of(obj)
    if record is None:
        return
    ENGINE.clear_pull(record.stack_id, stage.key)
    ENGINE.forget(record.stack_id, stage.key)
    _set(stage, "rid", "")
    _set(stage, "error", "")
    _set_state(stage, stack_mod.STATE_FROZEN)


def unfreeze(obj, stage):
    """Resume a frozen stage: the next pass asks Grasshopper again.

    Only the frozen flag changed; a disabled or conflicted stage settles back
    to its own state instead of pretending to be idle.
    """
    record = stack_mod.stack_of(obj)
    if record is not None:
        ENGINE.clear_pull(record.stack_id, stage.key)
    _set_state(stage, stack_mod.STATE_IDLE)
    ENGINE.ensure_active()
    ENGINE.dirty.add(obj.name)
    ENGINE.mark_hot()


def push(obj=None):
    """Force a fresh request for every live stage of a stack (increments the
    nonce). Frozen stages are parked and skipped entirely."""
    obj = obj or bpy.context.object
    if obj is None:
        return
    record = stack_mod.stack_of(obj)
    if record is None:
        return
    request_again(obj, skip_frozen=True)


def _mark_object_dirty(obj):
    ENGINE.ensure_active()
    ENGINE.dirty.add(obj.name)
    ENGINE.mark_hot()

