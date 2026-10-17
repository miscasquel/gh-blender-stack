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


def ensure_group(stack_obj, stage_key, nested_rows=None):
    """The node group for one stage, wired to all of its helpers and block
    definitions, nested placements included. Returns the group.

    ``nested_rows`` maps a definition index to ``[(target, matrix)]`` — the
    placements of other definitions inside it. Each definition's prototype
    chain is recursive: its own geometry joined with every nested child's
    chain through a Transform node, so a parent's rows instance the nested
    content with it.
    """
    helpers_map = helpers.ensure_helpers(stack_obj, stage_key)
    definitions = _definitions(stack_obj, stage_key)
    nested_rows = nested_rows or {}
    signature = _nested_signature(nested_rows)
    name = group_name(stage_key)
    group = bpy.data.node_groups.get(name)
    if group is not None and _group_matches(group, helpers_map, definitions, signature):
        return group
    if group is None:
        group = bpy.data.node_groups.new(name, "GeometryNodeTree")
    group.nodes.clear()
    group.interface.clear()

    join = group.nodes.new("GeometryNodeJoinGeometry")
    join.location = (0.0, 0.0)

    output = group.nodes.new("NodeGroupOutput")
    output.location = (400.0, 0.0)
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

    prototype_cache = {}
    building = set()

    def prototype_join(index):
        """The node chain for one definition's content: its own per-kind
        objects joined with every nested placement (recursively)."""
        cached = prototype_cache.get(index)
        if cached is not None:
            return cached
        proto = group.nodes.new("GeometryNodeJoinGeometry")
        proto.location = (-800.0, y)
        if index in building:
            print(
                "[gh-stack] %s: nested block %d is inside itself; the inner placement is skipped"
                % (stage_key, index),
                flush=True,
            )
            prototype_cache[index] = proto
            return proto
        building.add(index)
        objects = definitions.get(index, ({}, None))[0]
        for kind in helpers.KINDS:
            info = group.nodes.new("GeometryNodeObjectInfo")
            info.location = (-1000.0, y)
            info.inputs["Object"].default_value = objects[kind]
            info.transform_space = "ORIGINAL"
            group.links.new(info.outputs["Geometry"], proto.inputs["Geometry"])
        for target, matrix in nested_rows.get(index, []):
            if target not in definitions:
                continue
            child = prototype_join(target)
            transform = group.nodes.new("GeometryNodeTransform")
            transform.location = (-900.0, y - 100.0)
            local = helpers.matrix_of(matrix)
            position = local.to_translation()
            quaternion = local.to_quaternion()
            scale = local.to_scale()
            transform.inputs["Translation"].default_value = position
            transform.inputs["Rotation"].default_value = quaternion.to_euler()
            transform.inputs["Scale"].default_value = scale
            if not helpers.decomposes_cleanly(local, position, quaternion, scale):
                print(
                    "[gh-stack] %s: a nested transform in block %d has shear; "
                    "only position, rotation and scale can be carried" % (stage_key, index),
                    flush=True,
                )
            group.links.new(child.outputs["Geometry"], transform.inputs["Geometry"])
            group.links.new(transform.outputs["Geometry"], proto.inputs["Geometry"])
        building.discard(index)
        prototype_cache[index] = proto
        return proto

    for index in sorted(definitions):
        _objects, cloud = definitions[index]
        if cloud is None:
            # A definition only its parent instances: no own chain.
            continue
        iono = group.nodes.new("GeometryNodeInstanceOnPoints")
        iono.location = (0.0, y)
        group.links.new(prototype_join(index).outputs["Geometry"], iono.inputs["Instance"])

        pinfo = group.nodes.new("GeometryNodeObjectInfo")
        pinfo.location = (-200.0, y - 200.0)
        pinfo.inputs["Object"].default_value = cloud
        pinfo.transform_space = "ORIGINAL"
        group.links.new(pinfo.outputs["Geometry"], iono.inputs["Points"])

        # The implicit attribute fallback does not fire for datablock-backed
        # attributes (probed 2026-10-16), so rotation and scale are linked by
        # name explicitly. Both attributes are written with the rows, so a
        # missing-attribute default can never degenerate the transform.
        rotation = group.nodes.new("GeometryNodeInputNamedAttribute")
        rotation.location = (-100.0, y - 200.0)
        rotation.data_type = "QUATERNION"
        rotation.inputs["Name"].default_value = "rotation"
        group.links.new(rotation.outputs["Attribute"], iono.inputs["Rotation"])

        scale = group.nodes.new("GeometryNodeInputNamedAttribute")
        scale.location = (-100.0, y - 300.0)
        scale.data_type = "FLOAT_VECTOR"
        scale.inputs["Name"].default_value = "scale"
        group.links.new(scale.outputs["Attribute"], iono.inputs["Scale"])

        group.links.new(iono.outputs["Instances"], join.inputs["Geometry"])
        y -= 300.0

    group.links.new(join.outputs["Geometry"], output.inputs["Geometry"])
    group["gh_stack_nested"] = signature
    return group


def _nested_signature(nested_rows):
    """A deterministic string of the nested placements, stored on the group so
    a changed structure rebuilds it instead of leaving a stale chain."""
    parts = []
    for index in sorted(nested_rows):
        for target, matrix in nested_rows[index]:
            values = ",".join("%.9g" % float(v) for v in matrix)
            parts.append("%d>%d:%s" % (index, target, values))
    return ";".join(parts)


def _definitions(stack_obj, stage_key):
    """The stage's block definitions: index -> (prototype objects by kind, row
    cloud or None). A definition without rows is a prototype a parent nests."""
    clouds = helpers.instance_clouds(stack_obj, stage_key)
    definitions = {}
    for index in helpers.definition_collections(stack_obj, stage_key):
        _collection, objects = helpers.ensure_definition(stack_obj, stage_key, index)
        definitions[index] = (objects, clouds.get(index))
    return definitions


def _group_matches(group, helpers_map, definitions, signature=""):
    """Whether the group already reads exactly this set of objects and the
    same nested structure."""
    if group.get("gh_stack_nested") != signature:
        return False
    expected = set(helpers_map.values())
    for objects, cloud in definitions.values():
        expected.update(objects.values())
        if cloud is not None:
            expected.add(cloud)
    referenced = set()
    for node in group.nodes:
        if node.bl_idname != "GeometryNodeObjectInfo":
            continue
        obj = node.inputs["Object"].default_value
        if obj is not None:
            referenced.add(obj)
    return referenced == expected


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
