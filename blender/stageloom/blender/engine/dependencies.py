"""Stage dependencies: what a stage reads, and what reads it.

The upstream walker answers what a stage needs before it runs: the stages
before it on its own object and, transitively, the stages of any object or
collection its prefix reads. The downstream walker is its reverse: the later
stages on the same object and the stages of objects whose prefix reads this
object, transitively. Both are read-only graphs; they build no memo, freeze
nothing and do not run a pass.

The Bake operator uses both once per bake: upstream for the stable input set,
downstream for the dependents it must trigger and freeze (W5, ADR 0015).
"""

import bpy

from .. import helpers, nodes, stack as stack_mod
from .lifecycle import _all_stack_objects, _ordered_stages
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


def stage_dependents(obj, stage):
    """The transitive downstream stages that read ``stage`` on ``obj``.

    Returns ``[(object, stage)]`` in dependency order: a stage appears after
    every other returned stage it reads, so triggering and freezing the list in
    order means each dependent sees the previous dependent's answer. A stage
    the selected stage does not feed is absent, as is the selected stage
    itself. Stages are deduplicated by object pointer and stage key, and a
    cycle is visited once rather than hanging the walk.

    The graph is built by reversing :func:`stage_dependencies` over every stack
    object. That is intentionally O(S²) for S stages: this walk runs once per
    bake, not per pass, so the simple definition is worth more than an index
    that would have to stay in step with prefix evaluation.
    """
    nodes = _all_nodes()
    dependencies = {}
    reverse = {}
    for owner, candidate in nodes:
        key = _node_key(owner, candidate)
        deps = []
        for dep_owner, dep_stage in stage_dependencies(owner, candidate):
            dep_key = _node_key(dep_owner, dep_stage)
            deps.append(dep_key)
            reverse.setdefault(dep_key, []).append((owner, candidate))
        dependencies[key] = deps

    root = _node_key(obj, stage)
    closure = []
    seen = {root}
    pending = [root]
    while pending:
        for dependent in reverse.get(pending.pop(), ()):
            key = _node_key(*dependent)
            if key in seen:
                continue
            seen.add(key)
            closure.append(dependent)
            pending.append(key)

    # Kahn's algorithm over the closure, with the walk order as the stable
    # tie-break. A cycle has no dependency order; take the first remaining
    # stage so the walk still terminates and returns every stage once.
    remaining = list(closure)
    ordered = []
    emitted = {root}
    while remaining:
        chosen = None
        for position, (owner, candidate) in enumerate(remaining):
            deps = dependencies.get(_node_key(owner, candidate), ())
            if all(dep in emitted or dep not in seen for dep in deps):
                chosen = position
                break
        if chosen is None:
            chosen = 0
        owner, candidate = remaining.pop(chosen)
        emitted.add(_node_key(owner, candidate))
        ordered.append((owner, candidate))
    return ordered


def _all_nodes():
    """Every stage of every stack object, in deterministic walk order."""
    nodes = []
    for owner in _all_stack_objects():
        record = stack_mod.stack_of(owner)
        if record is None or not record.stages:
            continue
        for candidate in _ordered_stages(owner, record):
            nodes.append((owner, candidate))
    return nodes


def _node_key(owner, stage):
    return (owner.as_pointer(), stage.key)


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
