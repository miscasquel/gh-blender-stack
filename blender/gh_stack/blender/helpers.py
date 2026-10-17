"""Helper objects, block definitions and row clouds in the GH Stage scene (plan §5).

Helpers are hidden objects that carry a stage's output geometry, one per
geometry kind: a mesh, a curve, and a point cloud. A node group reads them with
Object Info (Original transform space, so the data stays in stack-local
coordinates), which is what makes GH output ride along with native modifiers.

A SubD cage is a mesh, so it shares the mesh helper.

Instance definitions (§5.2) are the same idea one level up: each definition is
a small hidden collection holding its own per-kind helpers, read by the stage's
node group through Object Info, and each definition's rows are hidden point
clouds (position/rotation/scale) fed into Instance on Points: one cloud for the
definition's top-level rows, and one for the placements nested inside it.

All of it is owned by the dedicated `GH Stage` scene, not the user's: the
outliner stays theirs, while the node graph still resolves the datablocks
through Object Info (verified across scenes, render included).
"""

import bpy
import numpy as np

COLLECTION_NAME = "GH Stage Data"
SCENE_NAME = "GH Stage"
HELPER_PREFIX = "GHS "
KINDS = ("mesh", "curves", "points")


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
    for coll in list(bpy.data.collections):
        if coll.name == COLLECTION_NAME or coll.name.startswith(HELPER_PREFIX):
            _adopt(coll)


def helper_name(stack_obj, stage_key, kind="mesh"):
    base = "%s%s.%s" % (HELPER_PREFIX, stack_obj.name, stage_key)
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


def helper_for(stack_obj, stage_key, kind):
    return bpy.data.objects.get(helper_name(stack_obj, stage_key, kind))


# ----------------------------------------------------------------- definitions


def definition_collection_name(stack_obj, stage_key, index):
    return "%s%s.%s.def%d" % (HELPER_PREFIX, stack_obj.name, stage_key, index)


def definition_name(stack_obj, stage_key, index, kind="mesh"):
    """The per-kind helper inside a definition's collection."""
    base = definition_collection_name(stack_obj, stage_key, index)
    return base if kind == "mesh" else "%s.%s" % (base, kind)


def instance_name(stack_obj, stage_key, index):
    """The top-level row point cloud of one definition."""
    return "%s%s.%s.inst%d" % (HELPER_PREFIX, stack_obj.name, stage_key, index)


def nested_name(stack_obj, stage_key, index):
    """The nested placement point cloud of one definition: the placements of
    child definitions inside it (``idef.<i>.inst.*``)."""
    return "%s%s.%s.nest%d" % (HELPER_PREFIX, stack_obj.name, stage_key, index)


def ensure_definition(stack_obj, stage_key, index):
    """One definition's prototype collection plus its per-kind helper objects.
    Returns ``(collection, {kind: object})``.

    The collection lives in the stage scene (hidden by flag, never excluded
    from a view layer: an excluded collection is not evaluated, and Collection
    Info once read them as empty).
    """
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
    objects = {}
    for kind in KINDS:
        name = definition_name(stack_obj, stage_key, index, kind)
        obj = bpy.data.objects.get(name)
        if obj is None:
            obj = bpy.data.objects.new(name, _new_data(name, kind))
            coll.objects.link(obj)
        elif obj.name not in coll.objects:
            coll.objects.link(obj)
        _hide(obj)
        objects[kind] = obj
    return coll, objects


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
        for obj in list(coll.objects):
            data = obj.data
            bpy.data.collections[coll.name].objects.unlink(obj)
            bpy.data.objects.remove(obj, do_unlink=True)
            if data is not None and data.users == 0:
                _remove_data(data)
        # Unconditionally: the collection is linked in the scene, so a users
        # count never reaches zero, and remove() unlinks it as it goes.
        bpy.data.collections.remove(coll)

    for cloud in (
        bpy.data.objects.get(instance_name(stack_obj, stage_key, index)),
        bpy.data.objects.get(nested_name(stack_obj, stage_key, index)),
    ):
        if cloud is not None:
            remove_cloud(cloud)


def remove_helper(stack_obj, stage_key):
    """Remove every helper of one stage and its orphaned data."""
    for kind in KINDS:
        helper = helper_for(stack_obj, stage_key, kind)
        if helper is None:
            continue
        data = helper.data
        bpy.data.objects.remove(helper, do_unlink=True)
        if data is None or data.users != 0:
            continue
        if kind == "curves":
            bpy.data.curves.remove(data)
        elif kind == "points":
            bpy.data.pointclouds.remove(data)
        else:
            bpy.data.meshes.remove(data)


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
