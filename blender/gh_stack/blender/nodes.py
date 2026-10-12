"""Node groups and modifier wiring (plan §5).

Each stage owns a node group: one Object Info (Original transform space, per the
plan's evidence) per helper kind feeding a Join Geometry and the group output.
The stack object gets a Geometry Nodes modifier built from that group, so the GH
stage sits in the modifier stack like any other modifier and native modifiers
still compose around it.

The group is rebuilt only when the helper set changes (a stage that starts
returning curves or points), never per tick: node graph churn would dirty the
depsgraph every pass.

Modifier inputs are never set from Python: the wiring is through node values
inside the group (plan §5).
"""

import bpy

from . import helpers

GROUP_PREFIX = "GHS "
MODIFIER_PREFIX = "GH · "


def group_name(stage_key):
    return "%s%s" % (GROUP_PREFIX, stage_key)


def ensure_group(stack_obj, stage_key):
    """The node group for one stage, wired to all of its helpers. Returns the group."""
    helpers_map = helpers.ensure_helpers(stack_obj, stage_key)
    name = group_name(stage_key)
    group = bpy.data.node_groups.get(name)
    if group is not None and _group_matches(group, helpers_map):
        return group
    if group is None:
        group = bpy.data.node_groups.new(name, "GeometryNodeTree")
    group.nodes.clear()
    group.interface.clear()

    join = group.nodes.new("GeometryNodeJoinGeometry")
    join.location = (0.0, 0.0)

    output = group.nodes.new("NodeGroupOutput")
    output.location = (200.0, 0.0)
    group.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")

    y = -100.0
    for kind in helpers.KINDS:
        info = group.nodes.new("GeometryNodeObjectInfo")
        info.location = (-200.0, y)
        info.inputs["Object"].default_value = helpers_map[kind]
        # Original space: the helper's data is already stack-local, and the
        # plan's evidence shows Relative gives wrong coordinates.
        info.transform_space = "ORIGINAL"
        group.links.new(info.outputs["Geometry"], join.inputs["Geometry"])
        y += 100.0

    group.links.new(join.outputs["Geometry"], output.inputs["Geometry"])
    return group


def _group_matches(group, helpers_map):
    """Whether the group already reads exactly this helper set."""
    wanted = list(helpers_map.values())
    seen = 0
    for node in group.nodes:
        if node.bl_idname != "GeometryNodeObjectInfo":
            continue
        for helper in wanted:
            if node.inputs["Object"].default_value == helper:
                seen += 1
                break
    return seen == len(wanted)


def ensure_modifier(stack_obj, stage):
    """The GN modifier on the stack object for one stage, inserted after the
    active modifier, or after the last GH stage when there is no active one."""
    group = ensure_group(stack_obj, stage.key)
    modifier = modifier_for(stack_obj, stage.key)
    if modifier is None:
        modifier = stack_obj.modifiers.new(name=MODIFIER_PREFIX + (stage.label or stage.key), type="NODES")
    modifier.node_group = group
    modifier.show_viewport = stage.enabled
    modifier.show_render = stage.enabled

    # Position: right after the active modifier, else at the end.
    order = [m.name for m in stack_obj.modifiers]
    if order and stack_obj.modifiers.active is not None and stack_obj.modifiers.active.name in order:
        at = order.index(stack_obj.modifiers.active.name) + 1
    else:
        at = len(order)
    if order.index(modifier.name) != at:
        stack_obj.modifiers.move(order.index(modifier.name), min(at, len(order) - 1))
    return modifier


def modifier_for(stack_obj, stage_key):
    """The stage's modifier, found through its node group name. Modifiers do not
    accept ID properties in Blender 5.2, so the group name is the link."""
    wanted = group_name(stage_key)
    for mod in stack_obj.modifiers:
        if mod.type == "NODES" and mod.node_group is not None and mod.node_group.name == wanted:
            return mod
    return None


def remove_modifier(stack_obj, stage_key):
    modifier = modifier_for(stack_obj, stage_key)
    if modifier is None:
        return
    group = modifier.node_group
    stack_obj.modifiers.remove(modifier)
    if group is not None and group.users == 0:
        bpy.data.node_groups.remove(group)


def set_modifier_visible(stack_obj, stage_key, visible):
    modifier = modifier_for(stack_obj, stage_key)
    if modifier is None:
        return
    # Only write on change: an unchanged write tags the depsgraph and restarts
    # Cycles accumulation for nothing.
    if modifier.show_viewport != visible:
        modifier.show_viewport = visible
    if modifier.show_render != visible:
        modifier.show_render = visible


def register():
    pass


def unregister():
    pass
