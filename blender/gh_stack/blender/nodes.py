"""Node groups and modifier wiring (plan §5).

Each stage owns a node group: one Object Info (Original transform space, per the
plan's evidence) per helper kind feeding a Join Geometry and the group output.
The stack object gets a Geometry Nodes modifier built from that group, so the GH
stage sits in the modifier stack like any other modifier and native modifiers
still compose around it.

The group is rebuilt only when its shape changes — the helper set, the block
definitions, or the nested structure (not placement transforms, which are
cloud data) — never per tick: node graph churn would dirty the depsgraph every
pass.

Modifier inputs are never set from Python: the wiring is through node values
inside the group (plan §5).
"""

import bpy

from . import helpers, prefs

GROUP_PREFIX = "GHS "
MODIFIER_PREFIX = "GH · "
# The node group's format. Stamped on every group the builder writes, and
# required by _group_matches: a group from an older build (no stamp, no nested
# chain) is stale even when the object set matches, which is what lets a
# same-hash frame repair a session that has not applied since the format
# changed. Version 3: nested placements are instances (one Instance on Points
# per child, fed by the placement clouds), not baked Transform copies.
GROUP_FORMAT = 3


def group_name(stage_key):
    return "%s%s" % (GROUP_PREFIX, stage_key)


def ensure_group(stack_obj, stage_key, nested_rows=None):
    """The node group for one stage, wired to all of its helpers and block
    definitions, nested placements included. Returns the group.

    ``nested_rows`` maps a definition index to ``[(target, matrix)]`` — the
    placements of other definitions inside it. Each definition's prototype
    chain is recursive: its own geometry joined with one Instance on Points per
    child definition, fed by the child's chain and the parent's placement cloud
    (its points carry the child index in a ``child`` attribute). Child chains
    are shared, so a nested placement is one instance per row and the graph
    grows with definitions and distinct placements, never with depth.
    """
    helpers_map = helpers.ensure_helpers(stack_obj, stage_key)
    nested_rows = nested_rows or {}
    definitions = _definitions(stack_obj, stage_key, nested_rows)
    signature = _nested_signature(nested_rows)
    name = group_name(stage_key)
    group = bpy.data.node_groups.get(name)
    if group is not None and _group_matches(
        group, helpers_map, definitions, signature, nested_rows
    ):
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

    y = [0.0]

    def next_row():
        """The next free row of the layout. Node positions are cosmetic; this
        only keeps the builder's output readable."""
        y[0] -= 60.0
        return y[0]

    for kind in helpers.KINDS:
        info = group.nodes.new("GeometryNodeObjectInfo")
        info.location = (-200.0, next_row())
        info.inputs["Object"].default_value = helpers_map[kind]
        # Original space: the helper's data is already stack-local, and the
        # plan's evidence shows Relative gives wrong coordinates.
        info.transform_space = "ORIGINAL"
        group.links.new(info.outputs["Geometry"], join.inputs["Geometry"])

    wired = _wired_edges(definitions, nested_rows)
    _note_skipped(stage_key, definitions, nested_rows, wired)

    def rotation_scale(iono):
        """Wire a placement cloud's rotation and scale by name. The implicit
        attribute fallback does not fire for datablock-backed attributes
        (probed 2026-10-16), so both are linked explicitly. The write path
        always puts both attributes on a cloud with rows, so a
        missing-attribute default cannot degenerate a transform."""
        rotation = group.nodes.new("GeometryNodeInputNamedAttribute")
        rotation.location = (-100.0, next_row())
        rotation.data_type = "QUATERNION"
        rotation.inputs["Name"].default_value = "rotation"
        group.links.new(rotation.outputs["Attribute"], iono.inputs["Rotation"])

        scale = group.nodes.new("GeometryNodeInputNamedAttribute")
        scale.location = (-100.0, next_row())
        scale.data_type = "FLOAT_VECTOR"
        scale.inputs["Name"].default_value = "scale"
        group.links.new(scale.outputs["Attribute"], iono.inputs["Scale"])

    prototype_cache = {}

    def prototype_join(index):
        """The node chain for one definition's content: its own per-kind
        objects joined with one Instance-on-Points per wired child, each fed by
        the placement points that name it. Memoized, so a DAG shares one chain
        per definition (the graph does not grow per level), and a child's
        geometry stays a reference (no baked copies)."""
        cached = prototype_cache.get(index)
        if cached is not None:
            return cached
        proto = group.nodes.new("GeometryNodeJoinGeometry")
        proto.location = (-800.0, next_row())
        prototype_cache[index] = proto
        objects, _cloud, nested = definitions.get(index, ({}, None, None))
        for kind in helpers.KINDS:
            info = group.nodes.new("GeometryNodeObjectInfo")
            info.location = (-1200.0, next_row())
            info.inputs["Object"].default_value = objects[kind]
            info.transform_space = "ORIGINAL"
            group.links.new(info.outputs["Geometry"], proto.inputs["Geometry"])
        for target in wired.get(index, []):
            child = prototype_join(target)
            points = group.nodes.new("GeometryNodeObjectInfo")
            points.location = (-1400.0, next_row())
            points.inputs["Object"].default_value = nested
            points.transform_space = "ORIGINAL"
            # Select this child's placements: the cloud's ``child`` attribute
            # names the definition each point places.
            selector = group.nodes.new("FunctionNodeCompare")
            selector.location = (-1100.0, next_row())
            selector.data_type = "INT"
            selector.operation = "EQUAL"
            selector.inputs["B"].default_value = target
            child_attr = group.nodes.new("GeometryNodeInputNamedAttribute")
            child_attr.location = (-1200.0, next_row())
            child_attr.data_type = "INT"
            child_attr.inputs["Name"].default_value = "child"
            group.links.new(child_attr.outputs["Attribute"], selector.inputs["A"])
            iono = group.nodes.new("GeometryNodeInstanceOnPoints")
            iono.location = (-1000.0, next_row())
            group.links.new(points.outputs["Geometry"], iono.inputs["Points"])
            group.links.new(child.outputs["Geometry"], iono.inputs["Instance"])
            group.links.new(selector.outputs["Result"], iono.inputs["Selection"])
            rotation_scale(iono)
            group.links.new(iono.outputs["Instances"], proto.inputs["Geometry"])
        return proto

    for index in sorted(definitions):
        _objects, cloud, _nested = definitions[index]
        if cloud is None:
            # No top-level rows: the chain is only reached through a parent's
            # placements.
            continue
        iono = group.nodes.new("GeometryNodeInstanceOnPoints")
        iono.location = (0.0, next_row())
        group.links.new(prototype_join(index).outputs["Geometry"], iono.inputs["Instance"])

        pinfo = group.nodes.new("GeometryNodeObjectInfo")
        pinfo.location = (-200.0, next_row())
        pinfo.inputs["Object"].default_value = cloud
        pinfo.transform_space = "ORIGINAL"
        group.links.new(pinfo.outputs["Geometry"], iono.inputs["Points"])

        rotation_scale(iono)

        group.links.new(iono.outputs["Instances"], join.inputs["Geometry"])

    group.links.new(join.outputs["Geometry"], output.inputs["Geometry"])
    group["gh_stack_nested"] = signature
    group["gh_stack_format"] = GROUP_FORMAT
    return group


def _nested_signature(nested_rows):
    """A deterministic string of the nested structure — which definition nests
    which — stored on the group so a changed structure rebuilds it instead of
    leaving a stale chain. Transform values are deliberately not part of it:
    the placement clouds carry them, so moving a nested placement updates the
    drawing without rebuilding the graph."""
    parts = []
    for index in sorted(nested_rows or {}):
        targets = sorted(set(target for target, _matrix in nested_rows[index]))
        if targets:
            parts.append("%d>%s" % (index, ",".join(str(target) for target in targets)))
    return ";".join(parts)


def _wired_edges(definitions, nested_rows):
    """The nested placements the builder wires: ``{index: [target, ...]}`` with
    duplicate targets collapsed, targets the response does not carry dropped,
    and edges that would close a cycle skipped. One source of truth for the
    builder and for :func:`_group_matches`, so a chain that dropped an edge is
    not taken for a stale group on every pass."""
    edges = []
    for index in sorted(nested_rows or {}):
        if index not in definitions:
            continue
        for target in sorted(set(target for target, _matrix in nested_rows[index])):
            if target in definitions:
                edges.append((index, target))
    wired = {}
    for source, target in edges:
        if _has_path(wired, target, source):
            continue
        wired.setdefault(source, []).append(target)
    return wired


def _has_path(wired, start, goal):
    """Whether ``start`` reaches ``goal`` over already-wired edges. A self edge
    counts: start == goal."""
    stack = [start]
    seen = set()
    while stack:
        node = stack.pop()
        if node == goal:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(wired.get(node, ()))
    return False


def _note_skipped(stage_key, definitions, nested_rows, wired):
    """Report the nested placements the wiring drops: a definition placed
    inside itself, or an edge that would close a cycle. A target the response
    does not carry is left to the apply path, which already reports it."""
    for index in sorted(nested_rows or {}):
        accepted = set(wired.get(index, []))
        for target in sorted(set(t for t, _matrix in nested_rows[index])):
            if target in accepted or target not in definitions:
                continue
            if target == index:
                prefs.log(
                    "[gh-stack] %s: nested block %d is inside itself; the placement is skipped"
                    % (stage_key, index),
                    flush=True,
                )
            else:
                prefs.log(
                    "[gh-stack] %s: the placement of block %d inside block %d would close "
                    "a cycle; the placement is skipped" % (stage_key, target, index),
                    flush=True,
                )


def _definitions(stack_obj, stage_key, nested_rows=None):
    """The stage's block definitions: index -> (prototype objects by kind,
    top-level row cloud or None, nested placement cloud or None). A definition
    only a parent instances has no top cloud, so no chain of its own is drawn;
    one that nests children gets a nested cloud even before the first apply
    fills it, so a chain rebuilt from a cached frame still wires."""
    top = helpers.instance_clouds(stack_obj, stage_key)
    nested = helpers.nested_clouds(stack_obj, stage_key)
    rows = nested_rows or {}
    definitions = {}
    for index in sorted(helpers.definition_collections(stack_obj, stage_key)):
        _collection, objects = helpers.ensure_definition(stack_obj, stage_key, index)
        if index not in nested and rows.get(index):
            # The apply path normally wrote it already; ensuring it here keeps
            # a rebuild from a cached frame wired (empty until the next apply).
            nested[index] = helpers.ensure_nested_cloud(stack_obj, stage_key, index)
        definitions[index] = (objects, top.get(index), nested.get(index))
    return definitions


def _reachable(definitions, wired):
    """The definitions the builder actually wires: those with a top-level row
    cloud, plus everything their nested placements reference, transitively. A
    definition no row reaches gets no chain, so it must not be expected in the
    group either (otherwise every apply would see a mismatch and rebuild)."""
    reachable = set()
    stack = [
        index
        for index, (_objects, cloud, _nested) in definitions.items()
        if cloud is not None
    ]
    while stack:
        index = stack.pop()
        if index in reachable or index not in definitions:
            continue
        reachable.add(index)
        for target in (wired or {}).get(index, []):
            if target not in reachable:
                stack.append(target)
    return reachable


def _group_matches(group, helpers_map, definitions, signature="", nested_rows=None):
    """Whether the group already reads exactly this set of objects, the same
    nested structure, and the current format."""
    if group.get("gh_stack_format") != GROUP_FORMAT:
        return False
    if group.get("gh_stack_nested") != signature:
        return False
    wired = _wired_edges(definitions, nested_rows)
    expected = set(helpers_map.values())
    for index in _reachable(definitions, wired):
        objects, cloud, nested = definitions[index]
        expected.update(objects.values())
        if cloud is not None:
            expected.add(cloud)
        if nested is not None and wired.get(index):
            expected.add(nested)
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
