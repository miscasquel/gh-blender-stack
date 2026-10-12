"""Helper objects and the GH Stage Data collection (plan §5).

Helpers are hidden objects that carry a stage's output geometry, one per
geometry kind: a mesh, a curve, and a point cloud. A node group reads them with
Object Info (Original transform space, so the data stays in stack-local
coordinates), which is what makes GH output ride along with native modifiers.

A SubD cage is a mesh, so it shares the mesh helper.
"""

import bpy

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
    helper.hide_viewport = True
    helper.hide_render = True
    helper.display_type = "WIRE"
    return helper


def ensure_helpers(stack_obj, stage_key):
    """All helpers of a stage, by kind."""
    return {kind: ensure_helper(stack_obj, stage_key, kind) for kind in KINDS}


def helper_for(stack_obj, stage_key, kind):
    return bpy.data.objects.get(helper_name(stack_obj, stage_key, kind))


def _new_data(name, kind):
    if kind == "curves":
        return bpy.data.curves.new(name, "CURVE")
    if kind == "points":
        return bpy.data.pointclouds.new(name)
    return bpy.data.meshes.new(name)


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


def register():
    pass


def unregister():
    pass
