"""Helper objects, block definitions and row clouds in the Stageloom Stage scene.

Helpers are hidden objects per geometry kind, read by the stage's node group in
Original transform space so their data stays stack-local. A SubD cage shares the
mesh helper. Instance definitions hold their own per-kind helpers and placement
clouds. Everything lives in the dedicated `Stageloom Stage` scene, never the
user's.
"""

import bpy
import numpy as np

from . import stack as stack_mod

COLLECTION_NAME = "Stageloom Stage Data"
SCENE_NAME = "Stageloom Stage"
HELPER_PREFIX = "SL "
OLD_COLLECTION_NAME = "GH Stage Data"
OLD_SCENE_NAME = "GH Stage"
OLD_HELPER_PREFIX = "GHS "
KINDS = ("mesh", "curves", "points")


def identity(stack_obj):
    """The stable root of every plumbing datablock name: the saved stack id.

    The object name is *not* part of it. Growing/shrinking a name or renaming
    the object in the outliner used to address a different helper set, which
    created a second set and orphaned the first. The stack id travels with the
    .blend and is independent of every user-facing name.
    """
    record = stack_mod.ensure_stack(stack_obj)
    if record is None:
        return ""
    return record.stack_id


def scene_of(obj):
    """The scene whose frame and playback own ``obj``.

    An object can be linked into several scenes; the active one wins when it
    is among them (that is the scene the user is looking at), otherwise the
    first link is its home. Never read the active scene for an object that is
    not in it: that is the multi-scene bug.
    """
    scenes = getattr(obj, "users_scene", None)
    if scenes:
        active = getattr(bpy.context, "scene", None)
        for scene in scenes:
            if scene == active:
                return scene
        return scenes[0]
    return getattr(bpy.context, "scene", None)


def frame_of(obj):
    """The current frame of the scene that owns ``obj``."""
    scene = scene_of(obj)
    if scene is None:
        return 0.0
    return float(scene.frame_float)


def stage_scene():
    """The scene that owns the plumbing: helpers, definitions and row clouds.

    One scene per file keeps the user's own scene and outliner clean while the
    objects stay linked — an unlinked datablock would not survive a save — and
    the node graph reads them across scenes (verified with a render). Created
    on first use; deleting it is repaired on the next use.
    """
    scene = bpy.data.scenes.get(SCENE_NAME)
    if scene is None:
        scene = bpy.data.scenes.new(SCENE_NAME)
    return scene


def _adopt(coll):
    """Own ``coll``: linked into the stage scene, and nowhere else. Sessions
    from before the stage scene have their plumbing linked into the user's
    scene; the next use moves it."""
    scene = stage_scene()
    if coll.name not in scene.collection.children:
        scene.collection.children.link(coll)
    for other in bpy.data.scenes:
        if other is not scene and coll.name in other.collection.children:
            other.collection.children.unlink(coll)


def collection():
    """The helper collection, created on first use, owned by the stage scene."""
    existing = bpy.data.collections.get(COLLECTION_NAME)
    if existing is not None:
        _adopt(existing)
        return existing
    coll = bpy.data.collections.new(COLLECTION_NAME)
    stage_scene().collection.children.link(coll)
    return coll


def adopt_existing():
    """Move plumbing linked into the user's scene (sessions from before the
    stage scene) into the stage scene. Called on register and on file load, so
    an old file tidies up even before its first apply."""
    if not hasattr(bpy.data, "collections"):
        # register() also runs while a file is being read, and data access is
        # restricted there; the load_post hook adopts once the file is in.
        return
    _migrate_old_names()
    _migrate_identity_names()
    for coll in list(bpy.data.collections):
        if coll.name == COLLECTION_NAME or coll.name.startswith(HELPER_PREFIX):
            _adopt(coll)


def _migrate_old_names():
    """Rename pre-Stageloom scene, collection, helper and node names in place."""
    old_scene = bpy.data.scenes.get(OLD_SCENE_NAME)
    if old_scene is not None and bpy.data.scenes.get(SCENE_NAME) is None:
        old_scene.name = SCENE_NAME
    old_coll = bpy.data.collections.get(OLD_COLLECTION_NAME)
    if old_coll is not None and bpy.data.collections.get(COLLECTION_NAME) is None:
        old_coll.name = COLLECTION_NAME
    for coll in list(bpy.data.collections):
        if coll.name.startswith(OLD_HELPER_PREFIX):
            coll.name = HELPER_PREFIX + coll.name[len(OLD_HELPER_PREFIX):]
    for obj in list(bpy.data.objects):
        if obj.name.startswith(OLD_HELPER_PREFIX):
            obj.name = HELPER_PREFIX + obj.name[len(OLD_HELPER_PREFIX):]
        data = obj.data
        if data is not None and data.name.startswith(OLD_HELPER_PREFIX):
            data.name = HELPER_PREFIX + data.name[len(OLD_HELPER_PREFIX):]
    # Node groups and modifiers live in nodes.py; import lazily to avoid the
    # helpers <-> nodes cycle.
    from . import nodes
    nodes.migrate_names()


def _object_has_data(obj):
    """Whether an owned plumbing object already displays geometry.

    Used only when a migrated name collides with an existing datablock: keeping
    the populated one preserves what the user was looking at.
    """
    data = obj.data
    if data is None:
        return False
    if isinstance(data, bpy.types.Mesh):
        return bool(len(data.vertices) or len(data.polygons))
    if isinstance(data, bpy.types.Curve):
        return bool(len(data.splines))
    if isinstance(data, bpy.types.PointCloud):
        return bool(len(data.points))
    return False


def _remove_object(obj):
    """Remove one owned object and its data when nothing else uses it."""
    data = obj.data
    bpy.data.objects.remove(obj, do_unlink=True)
    if data is not None and data.users == 0:
        _remove_data(data)


def _rename_object(old_name, new_name):
    """Rename one owned plumbing object, keeping the populated datablock.

    A session interrupted between the old name and the stable name can hold
    both. Prefer the one with geometry (the other is a freshly created empty
    helper) so a migration never discards output.
    """
    obj = bpy.data.objects.get(old_name)
    if obj is None:
        return
    existing = bpy.data.objects.get(new_name)
    if existing is not None and existing != obj:
        if _object_has_data(existing) and not _object_has_data(obj):
            _remove_object(obj)
            return
        _remove_object(existing)
    obj.name = new_name
    data = obj.data
    if data is not None and data.name == old_name:
        data.name = new_name


def _rename_prefix(old, new):
    """Rename every owned object/collection under one plumbing name prefix."""
    if old == new:
        return
    for obj in list(bpy.data.objects):
        if obj.name != old and not obj.name.startswith(old + "."):
            continue
        _rename_object(obj.name, new + obj.name[len(old):])
    for coll in list(bpy.data.collections):
        if coll.name != old and not coll.name.startswith(old + "."):
            continue
        target = new + coll.name[len(old):]
        existing = bpy.data.collections.get(target)
        if existing is not None and existing != coll:
            # Same interrupted migration as an object: fold the old collection
            # into the stable one instead of leaving two owners.
            for child in list(coll.objects):
                child_target = target + child.name[len(coll.name):]
                _rename_object(child.name, child_target)
                existing.objects.link(child)
                coll.objects.unlink(child)
            bpy.data.collections.remove(coll)
            continue
        coll.name = target


def _live_records():
    """Yield every ``(object, record)`` a saved stack still claims."""
    for obj in bpy.data.objects:
        record = getattr(obj, "gh_stack", None)
        if record is not None:
            yield obj, record


def _migrate_identity_names():
    """Move object-name-based plumbing to the stable stack-id identity.

    0.6/early-0.7 files named helpers ``SL <object name>.<stage key>``. A
    rename then addressed a second, empty set. Every live stage's old names are
    renamed in place, which keeps the data (and the node-group references) and
    leaves no old set behind. Files already using the stable name are a no-op.

    A stage key is globally unique outside a duplicate conflict, so its old
    prefix is found even when the object was renamed *before* this build first
    saw the file. Conflicts keep the current-object-name lookup: two claimants
    of one key cannot prove which old helper set is whose.
    """
    records = list(_live_records())
    claims = {}
    for owner, record in records:
        for stage in record.stages:
            if stage.key:
                claims.setdefault(stage.key, []).append(owner)

    for obj, record in records:
        if not record.stack_id:
            record.stack_id = stack_mod.new_stack_id()
        for stage in record.stages:
            if not stage.key:
                continue
            new = helper_name(obj, stage.key)
            if len(claims.get(stage.key, ())) == 1:
                for old in _stage_name_candidates(stage.key):
                    if old != new:
                        _rename_prefix(old, new)
            else:
                old = "%s%s.%s" % (HELPER_PREFIX, obj.name, stage.key)
                if old != new:
                    _rename_prefix(old, new)


def _stage_name_candidates(stage_key):
    """Every owned-name prefix that ends in one stage key, stable one excluded.

    The object-name part can contain dots, so the stage key is matched as the
    suffix component(s) rather than by splitting the name: ``SL any.name.key``
    and ``SL any.name.key.def0`` both yield ``SL any.name.key``.
    """
    marker = "." + stage_key
    found = set()
    for item in list(bpy.data.objects) + list(bpy.data.collections):
        name = item.name
        if not name.startswith(HELPER_PREFIX):
            continue
        start = len(HELPER_PREFIX)
        last = None
        while True:
            at = name.find(marker, start)
            if at < 0:
                break
            end = at + len(marker)
            if end == len(name) or name[end] == ".":
                # The last qualifying occurrence is the stage-key component:
                # an object name may itself contain dots (and even look like a
                # key), but the plumbing suffix is what follows it.
                last = at
            start = at + 1
        if last is not None:
            found.add(name[: last + len(marker)])
    return sorted(found)


def _remove_collection(coll):
    """Remove one owned definition collection and everything inside it."""
    for obj in list(coll.objects):
        _remove_object(obj)
    bpy.data.collections.remove(coll)


def _plumbing_is_live(name, live_ids, live_names):
    """Whether a named datablock still belongs to a live stack.

    Stable names carry the stack id; an old object-name-based name is kept when
    a live stack object of that name exists, even if its stage record is
    temporarily missing (a load, undo or duplicate split in progress). Only a
    stack whose id is gone is swept, so plumbing is never discarded merely
    because a stage is between records.
    """
    if not name.startswith(HELPER_PREFIX):
        return True
    rest = name[len(HELPER_PREFIX):]
    if rest.split(".", 1)[0] in live_ids:
        return True
    return any(name.startswith(HELPER_PREFIX + object_name + ".") for object_name in live_names)


def sweep_orphans():
    """Remove plumbing whose stack id no longer exists.

    Called after an index rebuild (a delete, a load, an undo), when the set of
    live stacks is authoritative. Only Stageloom's own collections and node
    groups are touched; user objects are never candidates.
    """
    if not hasattr(bpy.data, "collections"):
        return
    live_ids = set()
    live_names = set()
    live_keys = set()
    for obj, record in _live_records():
        live_names.add(obj.name)
        if record.stack_id:
            live_ids.add(record.stack_id)
        for stage in record.stages:
            if stage.key:
                live_keys.add(stage.key)

    helper_coll = bpy.data.collections.get(COLLECTION_NAME)
    if helper_coll is not None:
        for obj in list(helper_coll.objects):
            if not _plumbing_is_live(obj.name, live_ids, live_names):
                _remove_object(obj)

    stage = bpy.data.scenes.get(SCENE_NAME)
    if stage is not None:
        for coll in list(stage.collection.children):
            if coll.name == COLLECTION_NAME or not coll.name.startswith(HELPER_PREFIX):
                continue
            if not _plumbing_is_live(coll.name, live_ids, live_names):
                _remove_collection(coll)

    # A deleted stack object leaves its node group with no user once the
    # modifier went with it. Only groups this builder stamped are candidates;
    # a live key (even a conflicted one) is never removed while its record
    # exists.
    for group in list(bpy.data.node_groups):
        if group.get("gh_stack_format") is None:
            continue
        if not group.name.startswith(HELPER_PREFIX):
            continue
        if group.name[len(HELPER_PREFIX):] in live_keys:
            continue
        if group.users == 0:
            bpy.data.node_groups.remove(group)


def helper_name(stack_obj, stage_key, kind="mesh"):
    base = "%s%s.%s" % (HELPER_PREFIX, identity(stack_obj), stage_key)
    return base if kind == "mesh" else "%s.%s" % (base, kind)


def ensure_helper(stack_obj, stage_key, kind="mesh"):
    """The helper for one kind of one stage. Created hidden and identity-transformed."""
    name = helper_name(stack_obj, stage_key, kind)
    coll = collection()
    helper = bpy.data.objects.get(name)
    if helper is None:
        helper = bpy.data.objects.new(name, _new_data(name, kind))
        coll.objects.link(helper)
    elif helper.name not in coll.objects:
        # The stage scene was deleted: its datablocks survive unlinked, and
        # this keeps them owned (and saved) again.
        coll.objects.link(helper)
    # Only write on change, like every engine-side write: an unchanged write
    # tags the depsgraph and restarts Cycles accumulation for nothing.
    _hide(helper)
    return helper


def ensure_helpers(stack_obj, stage_key):
    """All helpers of a stage, by kind."""
    return {kind: ensure_helper(stack_obj, stage_key, kind) for kind in KINDS}


def missing_kinds(stack_obj, stage_key):
    """Which per-kind helpers of a stage are gone (deleted datablocks)."""
    return [kind for kind in KINDS if helper_for(stack_obj, stage_key, kind) is None]


def helper_for(stack_obj, stage_key, kind):
    return bpy.data.objects.get(helper_name(stack_obj, stage_key, kind))


# ----------------------------------------------------------------- definitions


def definition_collection_name(stack_obj, stage_key, index):
    return "%s%s.%s.def%d" % (HELPER_PREFIX, identity(stack_obj), stage_key, index)


# The number of parts a definition's collection holds (protocol §5.2). Written
# by the apply path and read back by the node builder; a collection from an
# older session has no value and is one part.
DEFINITION_PARTS_KEY = "gh_stack_parts"


def definition_name(stack_obj, stage_key, index, kind="mesh", part=None):
    """The per-kind helper inside a definition's collection.

    ``part`` selects one part of a multi-part definition: part helpers carry a
    ``.part<p>`` suffix (``...def3.part1`` for mesh, ``...def3.part1.curves``
    for the rest). A single-part definition keeps its original names.
    """
    base = definition_collection_name(stack_obj, stage_key, index)
    if part is not None:
        base = "%s.part%d" % (base, int(part))
    return base if kind == "mesh" else "%s.%s" % (base, kind)


def instance_name(stack_obj, stage_key, index):
    """The top-level row point cloud of one definition."""
    return "%s%s.%s.inst%d" % (HELPER_PREFIX, identity(stack_obj), stage_key, index)


def nested_name(stack_obj, stage_key, index):
    """The nested placement point cloud of one definition: the placements of
    child definitions inside it (``idef.<i>.inst.*``)."""
    return "%s%s.%s.nest%d" % (HELPER_PREFIX, identity(stack_obj), stage_key, index)


def _definition_collection(stack_obj, stage_key, index):
    """One definition's prototype collection, created and adopted on first use."""
    coll_name = definition_collection_name(stack_obj, stage_key, index)
    coll = bpy.data.collections.get(coll_name)
    if coll is None:
        coll = bpy.data.collections.new(coll_name)
        stage_scene().collection.children.link(coll)
    else:
        _adopt(coll)
    if not coll.hide_viewport:
        coll.hide_viewport = True
    if not coll.hide_render:
        coll.hide_render = True
    return coll


def ensure_definition(stack_obj, stage_key, index):
    """One definition's prototype collection plus its per-kind helper objects.
    Returns ``(collection, {kind: object})`` — the original single-part shape.

    The collection lives in the stage scene (hidden by flag, never excluded
    from a view layer: an excluded collection is not evaluated, and Collection
    Info once read them as empty).
    """
    coll, parts = ensure_definition_parts(stack_obj, stage_key, index, 1)
    return coll, parts[0]


def ensure_definition_parts(stack_obj, stage_key, index, parts):
    """One definition's collection plus one per-kind helper per part.

    Returns ``(collection, [{kind: object}, ...])`` in part order. A one-part
    definition keeps the original helper names; a multi-part one uses the
    ``.part<p>`` names. Helpers left over from another shape (a definition that
    was single-part and is now multi-part, or a part that went away) are removed,
    so a namespace change never leaves ghost geometry in the prototype.
    """
    parts = max(1, int(parts))
    coll = _definition_collection(stack_obj, stage_key, index)
    if coll.get(DEFINITION_PARTS_KEY) != parts:
        coll[DEFINITION_PARTS_KEY] = parts
    objects = []
    expected = set()
    for part in range(parts):
        part_index = part if parts > 1 else None
        part_objects = {}
        for kind in KINDS:
            name = definition_name(stack_obj, stage_key, index, kind, part=part_index)
            expected.add(name)
            obj = bpy.data.objects.get(name)
            if obj is None:
                obj = bpy.data.objects.new(name, _new_data(name, kind))
                coll.objects.link(obj)
            elif obj.name not in coll.objects:
                coll.objects.link(obj)
            _hide(obj)
            part_objects[kind] = obj
        objects.append(part_objects)
    _remove_stale_definition_helpers(coll, expected)
    return coll, objects


def _remove_stale_definition_helpers(coll, expected):
    """Remove helper objects this definition no longer has a name for.

    Only objects under the definition's own helper base are touched: the
    collection is owned plumbing, and an unexpected object of another kind is
    left alone.
    """
    base = coll.name
    for obj in list(coll.objects):
        if obj.name in expected or not obj.name.startswith(base):
            continue
        data = obj.data
        coll.objects.unlink(obj)
        bpy.data.objects.remove(obj, do_unlink=True)
        if data is not None and data.users == 0:
            _remove_data(data)


def definition_parts_count(stack_obj, stage_key, index):
    """How many parts a definition's collection holds, at least 1."""
    coll = bpy.data.collections.get(definition_collection_name(stack_obj, stage_key, index))
    if coll is None:
        return 1
    parts = coll.get(DEFINITION_PARTS_KEY, 1)
    if isinstance(parts, bool) or not isinstance(parts, int) or parts < 1:
        return 1
    return parts


def ensure_definition_layout(stack_obj, stage_key, index):
    """Ensure a definition's collection has the parts it was last given.

    The apply path writes the part count; the node builder reads it back (a
    group rebuild after a load must wire the same helpers). Returns
    ``(collection, [{kind: object}, ...])``.
    """
    return ensure_definition_parts(
        stack_obj, stage_key, index,
        definition_parts_count(stack_obj, stage_key, index),
    )


def ensure_instance_cloud(stack_obj, stage_key, index):
    """The top-level row point cloud of one definition: one point per instance
    row, with ``rotation`` and ``scale`` attributes the node group reads by
    name."""
    return _ensure_cloud(instance_name(stack_obj, stage_key, index))


def ensure_nested_cloud(stack_obj, stage_key, index):
    """The nested placement cloud of one definition: one point per placement of
    a child definition inside it, with the same transforms as a top-level cloud
    plus an INT ``child`` attribute naming the child each point places."""
    return _ensure_cloud(nested_name(stack_obj, stage_key, index))


def _ensure_cloud(name):
    coll = collection()
    cloud = bpy.data.objects.get(name)
    if cloud is None:
        cloud = bpy.data.objects.new(name, bpy.data.pointclouds.new(name))
        coll.objects.link(cloud)
    elif cloud.name not in coll.objects:
        # The stage scene was deleted: its datablocks survive unlinked, and
        # this keeps them owned (and saved) again.
        coll.objects.link(cloud)
    _hide(cloud)
    return cloud


def remove_cloud(cloud):
    """Remove a row point cloud object and its data when orphaned."""
    data = cloud.data
    bpy.data.objects.remove(cloud, do_unlink=True)
    if data is not None and data.users == 0:
        bpy.data.pointclouds.remove(data)


def definition_collections(stack_obj, stage_key):
    """Every definition collection this stage owns, keyed by index."""
    prefix = definition_collection_name(stack_obj, stage_key, 0)[:-1]
    found = {}
    for coll in bpy.data.collections:
        if not coll.name.startswith(prefix):
            continue
        suffix = coll.name[len(prefix):]
        if suffix.isdigit():
            found[int(suffix)] = coll
    return found


def instance_clouds(stack_obj, stage_key):
    """Every top-level row cloud this stage owns, keyed by index."""
    return _clouds(instance_name(stack_obj, stage_key, 0)[:-1])


def nested_clouds(stack_obj, stage_key):
    """Every nested placement cloud this stage owns, keyed by index."""
    return _clouds(nested_name(stack_obj, stage_key, 0)[:-1])


def _clouds(prefix):
    found = {}
    for obj in bpy.data.objects:
        if not obj.name.startswith(prefix) or obj.type != "POINTCLOUD":
            continue
        suffix = obj.name[len(prefix):]
        if suffix.isdigit():
            found[int(suffix)] = obj
    return found


def remove_definition(stack_obj, stage_key, index):
    """Remove one definition: its collection and the helper, nested and row
    objects it owns. Collections a nested empty points at belong to other
    definitions and are left alone."""
    coll = bpy.data.collections.get(definition_collection_name(stack_obj, stage_key, index))
    if coll is not None:
        _remove_collection(coll)

    for cloud in (
        bpy.data.objects.get(instance_name(stack_obj, stage_key, index)),
        bpy.data.objects.get(nested_name(stack_obj, stage_key, index)),
    ):
        if cloud is not None:
            remove_cloud(cloud)


def remove_helper(stack_obj, stage_key):
    """Remove one stage's entire plumbing: every helper kind, every definition
    part collection, and both row-cloud families.

    One owner for the stage's namespace, so removing a stage (or dropping a
    duplicate's old key) cannot leave a part, collection or cloud behind. The
    identity is the stack id, never the object name.
    """
    for kind in KINDS:
        helper = helper_for(stack_obj, stage_key, kind)
        if helper is None:
            continue
        _remove_object(helper)
    for index in list(definition_collections(stack_obj, stage_key)):
        remove_definition(stack_obj, stage_key, index)
    for index in list(instance_clouds(stack_obj, stage_key)):
        remove_cloud(bpy.data.objects[instance_name(stack_obj, stage_key, index)])
    for index in list(nested_clouds(stack_obj, stage_key)):
        remove_cloud(bpy.data.objects[nested_name(stack_obj, stage_key, index)])


def _new_data(name, kind):
    if kind == "curves":
        return bpy.data.curves.new(name, "CURVE")
    if kind == "points":
        return bpy.data.pointclouds.new(name)
    return bpy.data.meshes.new(name)


def _remove_data(data):
    if isinstance(data, bpy.types.Mesh):
        bpy.data.meshes.remove(data)
    elif isinstance(data, bpy.types.Curve):
        bpy.data.curves.remove(data)
    elif isinstance(data, bpy.types.PointCloud):
        bpy.data.pointclouds.remove(data)


def _hide(obj):
    if not obj.hide_viewport:
        obj.hide_viewport = True
    if not obj.hide_render:
        obj.hide_render = True
    if obj.display_type != "WIRE":
        obj.display_type = "WIRE"


def matrix_of(matrix):
    """A mathutils Matrix from the wire's row-major 16 floats (translation in
    the fourth column, protocol §5.2)."""
    import mathutils

    m = np.asarray(matrix, dtype=np.float32).reshape(4, 4)
    return mathutils.Matrix((tuple(m[0]), tuple(m[1]), tuple(m[2]), tuple(m[3])))


def decomposes_cleanly(matrix, position, quaternion, scale):
    """Whether position/rotation/scale rebuild the matrix. A shear is the one
    shape the point-cloud channel and the Transform node cannot carry."""
    import mathutils

    rebuilt = mathutils.Matrix.LocRotScale(position, quaternion, scale)
    difference = np.abs(
        np.asarray(rebuilt, dtype=np.float32) - np.asarray(matrix, dtype=np.float32)
    )
    return float(difference.max()) < 1e-4


def register():
    adopt_existing()
