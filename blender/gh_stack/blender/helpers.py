"""Helper objects and the GH Stage Data collection (plan §5).

Helpers are hidden objects that carry a stage's output geometry, one per
geometry kind: a mesh, a curve, and a point cloud. A node group reads them with
Object Info (Original transform space, so the data stays in stack-local
coordinates), which is what makes GH output ride along with native modifiers.

A SubD cage is a mesh, so it shares the mesh helper.

Instance definitions (§5.2) are the same idea one level up: each definition is
a small hidden collection holding its own per-kind helpers, read by the stage's
node group through Collection Info, and each definition's rows are a hidden
point cloud (position/rotation/scale) fed into Instance on Points.
"""

import bpy
import numpy as np

COLLECTION_NAME = "GH Stage Data"
HELPER_PREFIX = "GHS "
KINDS = ("mesh", "curves", "points")


def collection(context=None):
    """The excluded helper collection, created on first use."""
    existing = bpy.data.collections.get(COLLECTION_NAME)
    if existing is not None:
        return existing
    coll = bpy.data.collections.new(COLLECTION_NAME)
    (context or bpy.context).scene.collection.children.link(coll)
    # Excluded from the view layer: helpers are plumbing, never visible.
    layer = (context or bpy.context).view_layer
    if coll.name in layer.layer_collection.children:
        layer.layer_collection.children[coll.name].exclude = True
    return coll


def helper_name(stack_obj, stage_key, kind="mesh"):
    base = "%s%s.%s" % (HELPER_PREFIX, stack_obj.name, stage_key)
    return base if kind == "mesh" else "%s.%s" % (base, kind)


def ensure_helper(stack_obj, stage_key, kind="mesh"):
    """The helper for one kind of one stage. Created hidden and identity-transformed."""
    name = helper_name(stack_obj, stage_key, kind)
    helper = bpy.data.objects.get(name)
    if helper is None:
        helper = bpy.data.objects.new(name, _new_data(name, kind))
        collection().objects.link(helper)
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
    """The row point cloud of one definition."""
    return "%s%s.%s.inst%d" % (HELPER_PREFIX, stack_obj.name, stage_key, index)


def ensure_definition(stack_obj, stage_key, index):
    """One definition's prototype collection plus its per-kind helper objects.
    Returns ``(collection, {kind: object})``.

    The collection is linked at the scene root and hidden by flag rather than
    excluded from the view layer: a collection inside the excluded helper
    collection is not evaluated, so Collection Info would read it as empty.
    """
    coll_name = definition_collection_name(stack_obj, stage_key, index)
    coll = bpy.data.collections.get(coll_name)
    if coll is None:
        coll = bpy.data.collections.new(coll_name)
        bpy.context.scene.collection.children.link(coll)
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
    """The row point cloud of one definition: one point per instance row, with
    ``rotation`` and ``scale`` attributes the node group reads by name."""
    name = instance_name(stack_obj, stage_key, index)
    cloud = bpy.data.objects.get(name)
    if cloud is None:
        cloud = bpy.data.objects.new(name, bpy.data.pointclouds.new(name))
        collection().objects.link(cloud)
    _hide(cloud)
    return cloud


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
    """Every row cloud this stage owns, keyed by index."""
    prefix = instance_name(stack_obj, stage_key, 0)[:-1]
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

    cloud = bpy.data.objects.get(instance_name(stack_obj, stage_key, index))
    if cloud is not None:
        data = cloud.data
        bpy.data.objects.remove(cloud, do_unlink=True)
        if data is not None and data.users == 0:
            bpy.data.pointclouds.remove(data)


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
    pass


def unregister():
    pass
