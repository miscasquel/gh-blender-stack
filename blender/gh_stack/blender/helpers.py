"""Helper objects and the GH Stage Data collection (plan §5).

Helpers are hidden objects that carry a stage's output geometry. A node group
reads them with Object Info (Original transform space, so the data stays in
stack-local coordinates), which is what makes GH output ride along with native
modifiers.
"""

import bpy

COLLECTION_NAME = "GH Stage Data"
HELPER_PREFIX = "GHS "


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


def helper_name(stack_obj, stage_key):
    return "%s%s.%s" % (HELPER_PREFIX, stack_obj.name, stage_key)


def ensure_helper(stack_obj, stage_key):
    """The mesh helper for one stage. Created hidden and identity-transformed."""
    name = helper_name(stack_obj, stage_key)
    helper = bpy.data.objects.get(name)
    if helper is None:
        mesh = bpy.data.meshes.new(name)
        helper = bpy.data.objects.new(name, mesh)
        collection().objects.link(helper)
    helper.hide_viewport = True
    helper.hide_render = True
    helper.display_type = "WIRE"
    return helper


def remove_helper(stack_obj, stage_key):
    helper = bpy.data.objects.get(helper_name(stack_obj, stage_key))
    if helper is None:
        return
    mesh = helper.data
    bpy.data.objects.remove(helper, do_unlink=True)
    if mesh is not None and mesh.users == 0:
        bpy.data.meshes.remove(mesh)


def register():
    pass


def unregister():
    pass
