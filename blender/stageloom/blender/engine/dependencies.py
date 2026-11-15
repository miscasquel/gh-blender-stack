"""Upstream stage dependencies: what a stage reads before it runs.

The graph direction is upstream only. A stage depends on the stages before it
on its own object and, transitively, on the stages of any object or collection
its prefix reads. Downstream consumers are never part of the answer: they read
the selected stage, so they cannot change what it evaluated.

This is a read-only walk. It builds no cache, freezes nothing and does not run a
pass; the Bake operator computes the set once per bake (W2.1).
"""

import bpy

from .. import helpers, nodes, stack as stack_mod
from .lifecycle import _ordered_stages
from .request import _modifier_index


def stage_dependencies(obj, stage):
    """The transitive upstream stages of ``stage`` on ``obj``.

    Returns ``[(object, stage)]`` in deterministic walk order: same-object
    stages before the selected stage in modifier-stack order, then stages
    reached through its prefix, depth first. Stages are deduplicated by object
    pointer and stage key, and an object is visited at most once, so a cycle
    terminates. Internal Stageloom plumbing is skipped.

    A generator stage has no Stage In: its prefix is not part of its request,
    so it has no upstream stages at all.
    """
    if stack_mod.is_generator(stage):
        return []

    found = []
    seen_stages = set()
    # The selected object is visited before the walk: a prefix may read it
    # (directly or through a cycle), and that reference is not an upstream
    # dependency of something already on it.
    seen_objects = {obj.as_pointer()}

    def remember(owner, candidate):
        key = (owner.as_pointer(), candidate.key)
        if key in seen_stages:
            return
        seen_stages.add(key)
        found.append((owner, candidate))

    def collect(reference):
        if isinstance(reference, bpy.types.Collection):
            if _is_plumbing_collection(reference):
                return
            for member in reference.all_objects:
                visit_object(member)
        else:
            visit_object(reference)

    def visit_object(candidate_obj):
        if candidate_obj is None or _is_plumbing_object(candidate_obj):
            return
        pointer = candidate_obj.as_pointer()
        if pointer in seen_objects:
            return
        seen_objects.add(pointer)
        record = stack_mod.stack_of(candidate_obj)
        if record is not None and record.stages:
            for candidate in _ordered_stages(candidate_obj, record):
                remember(candidate_obj, candidate)
        # A reference reads the object's fully evaluated result, so every
        # modifier can contribute a reference, including one after its last
        # stage. Walk them once rather than once per stage prefix.
        _collect_references(list(candidate_obj.modifiers))

    def _collect_references(modifiers):
        for reference in _references(modifiers):
            collect(reference)

    record = stack_mod.stack_of(obj)
    if record is not None and record.stages:
        stages = _ordered_stages(obj, record)
        pointer = stage.as_pointer()
        for position, candidate in enumerate(stages):
            if candidate.as_pointer() == pointer:
                for earlier in stages[:position]:
                    remember(obj, earlier)
                break
    _collect_references(_prefix_modifiers(obj, stage))
    return found


def _prefix_modifiers(obj, stage):
    """The modifiers evaluated before a stage's own modifier, in stack order."""
    modifier = nodes.modifier_of(obj, stage)
    if modifier is None and not getattr(stage, "modifier_uid", 0):
        modifier = nodes.modifier_by_name(obj, stage)
    if modifier is None:
        return list(obj.modifiers)
    return list(obj.modifiers)[:_modifier_index(obj, modifier)]


def _references(modifiers):
    """Every Object/Collection datablock the modifiers read, deduplicated."""
    found = []
    seen = set()
    trees = set()

    def add(reference):
        if not isinstance(reference, (bpy.types.Object, bpy.types.Collection)):
            return
        pointer = reference.as_pointer()
        if pointer in seen:
            return
        seen.add(pointer)
        found.append(reference)

    for modifier in modifiers:
        group = getattr(modifier, "node_group", None)
        if group is not None:
            for reference in _node_tree_references(group, trees):
                add(reference)
        for reference in _rna_references(modifier):
            add(reference)
        for reference in _input_references(modifier):
            add(reference)
    return found


def _node_tree_references(tree, seen):
    """Object/Collection defaults in a node tree and its nested groups."""
    if tree is None:
        return
    pointer = tree.as_pointer()
    if pointer in seen:
        return
    seen.add(pointer)
    for node in tree.nodes:
        for socket in getattr(node, "inputs", ()):
            if getattr(socket, "type", None) not in ("OBJECT", "COLLECTION"):
                continue
            try:
                value = socket.default_value
            except (AttributeError, ReferenceError, TypeError):
                continue
            if isinstance(value, (bpy.types.Object, bpy.types.Collection)):
                yield value
        nested = getattr(node, "node_tree", None)
        if nested is not None:
            for reference in _node_tree_references(nested, seen):
                yield reference


def _rna_references(modifier):
    """Object/Collection RNA properties of a native modifier (Boolean, ...)."""
    for prop in modifier.bl_rna.properties:
        if prop.type != "POINTER":
            continue
        fixed = getattr(prop, "fixed_type", None)
        if getattr(fixed, "identifier", None) not in ("Object", "Collection"):
            continue
        try:
            value = getattr(modifier, prop.identifier)
        except (AttributeError, ReferenceError, TypeError):
            continue
        if isinstance(value, (bpy.types.Object, bpy.types.Collection)):
            yield value


def _input_references(modifier):
    """Object/Collection values stored on a Geometry Nodes modifier's inputs."""
    properties = getattr(modifier, "properties", None)
    inputs = getattr(properties, "inputs", None)
    if inputs is None:
        return
    try:
        keys = list(inputs.keys())
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return
    for key in keys:
        try:
            value = inputs[key]["value"]
        except (KeyError, ReferenceError, RuntimeError, TypeError):
            continue
        if isinstance(value, (bpy.types.Object, bpy.types.Collection)):
            yield value


def _is_plumbing_object(obj):
    """Whether an object is a Stageloom helper, prototype or row cloud."""
    if obj is None:
        return True
    stage_scene = bpy.data.scenes.get(helpers.SCENE_NAME)
    if stage_scene is None:
        # The scene can be missing on stale, unlinked plumbing datablocks.
        if obj.name.startswith(helpers.HELPER_PREFIX):
            return True
    elif stage_scene in obj.users_scene:
        return True
    for collection in obj.users_collection:
        if _is_plumbing_collection(collection):
            return True
    return False


def _is_plumbing_collection(collection):
    if collection is None:
        return True
    stage_scene = bpy.data.scenes.get(helpers.SCENE_NAME)
    if stage_scene is None:
        return collection.name == helpers.COLLECTION_NAME or collection.name.startswith(
            helpers.HELPER_PREFIX
        )
    if collection == stage_scene.collection:
        return True
    return collection.name in stage_scene.collection.children
