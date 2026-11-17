"""Geometry sets: the frame's own mesh/curves/points and the flat definition
table for instances. One owner for the duplicate-section guard and
``MAX_INSTANCE_DEPTH``.
"""

import numpy as np

from .. import prefs
from .attributes import points_attributes
from .common import GeometryIOError, _count_note, _note
from .curves import curves_component_to_sections
from .mesh import mesh_with_attributes
from .points import points_to_sections


# The recursion guard for instance nesting. Blender cannot evaluate a cyclic
# instance graph, but an eagerly built GeometrySet could still arrive deep or
# malformed; a cap keeps a broken scene from hanging the engine.
MAX_INSTANCE_DEPTH = 32

_IDENTITY = (1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0,
             0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0)


def geometry_set_to_sections(geometry, as_subd=False, notes=None, blocks_2=True):
    """Protocol sections and attribute descriptors for an evaluated
    ``GeometrySet`` (a stage's prefix). Returns ``(sections, attributes,
    definitions)``. ``notes`` collects user-visible conversion losses.

    A stage's prefix is everything before its own modifier in the stack, so it
    can hold a mesh, curves, points, and instances at once. The geometry set must
    stay referenced while the components are read: its datablocks die with it.

    Instances become a definition table (protocol §5.2): each referenced
    geometry set is serialized once, prefixed ``idef.<i>.``, and the instance
    rows become ``inst.ref_def`` / ``inst.transforms``. A referenced geometry set
    that itself holds instances recurses into the same flat table.

    A definition with more than one part is emitted as ``parts: N`` and one
    ``idef.<i>.<part>.`` namespace per part. ``blocks_2`` is the peer's
    ``blocks/2`` capability (§8): without it the definition is split into one
    single-part definition per part and ``notes`` names the fallback, because an
    old peer ignores both the `parts` field and the part namespaces and would
    otherwise lose every part but one.

    Curves components carry their ``CURVE`` and ``POINT`` attributes (the same
    per-curve/per-point mapping used for wire-mesh edges). A Curve datablock
    used as a stage helper cannot store attributes on the way back; that return
    path is separate.
    """
    sections = []
    attributes = []
    definitions = []
    _serialize_geometry(
        geometry, sections, attributes, prefix="", as_subd=as_subd, notes=notes
    )
    rows = _serialize_instances(
        geometry, sections, attributes, definitions, depth=0, notes=notes,
        blocks_2=blocks_2,
    )
    if rows is not None:
        ref_def, transforms = rows
        sections.append(("inst.ref_def", "u32", np.asarray(ref_def, dtype=np.uint32)))
        sections.append(
            ("inst.transforms", "f32", np.asarray(transforms, dtype=np.float32).reshape(-1))
        )
    _reject_duplicate_sections(sections)
    return sections, attributes, definitions


def _reject_duplicate_sections(sections):
    """Fail visibly on two sections with one name.

    A duplicate name means two geometry pools were written into one namespace:
    a codec keeps only one of them, so the other is silent loss. Instances give
    every part and definition its own prefix; this guard is what keeps a future
    serialization change from reintroducing the collapse.
    """
    seen = set()
    for name, _dtype, _values in sections:
        if name in seen:
            raise GeometryIOError("duplicate section name %r" % name)
        seen.add(name)


def _mesh_part(mesh, sections, attributes, prefix, as_subd, definition, part, notes):
    """One GeometrySet's mesh component, including its loose wire remainder."""
    if mesh is None or not len(mesh.vertices):
        return
    mesh_sections, mesh_attrs = mesh_with_attributes(
        mesh, as_subd=as_subd, index0=len(attributes), prefix=prefix,
        definition=definition, part=part, notes=notes,
    )
    sections += mesh_sections
    attributes += mesh_attrs


def _curves_part(curves, sections, attributes, prefix, as_subd, definition, part, notes):
    """One GeometrySet's curves component."""
    if curves is None or not len(curves.points):
        return
    curve_sections, curve_attrs = curves_component_to_sections(
        curves, index0=len(attributes), prefix=prefix, definition=definition,
        part=part, notes=notes,
    )
    sections += curve_sections
    attributes += curve_attrs


def _points_part(cloud, sections, attributes, prefix, as_subd, definition, part, notes):
    """One GeometrySet's point-cloud component."""
    if cloud is None or not len(cloud.points):
        return
    sections += points_to_sections(cloud, prefix=prefix, notes=notes)
    attribute_sections, point_attributes_ = points_attributes(
        cloud, index0=len(attributes), prefix=prefix, definition=definition,
        part=part, notes=notes,
    )
    sections += attribute_sections
    attributes += point_attributes_


# One row per geometry component a GeometrySet can carry, in wire order.
_GEOMETRY_PARTS = (("mesh", _mesh_part), ("curves", _curves_part),
                   ("pointcloud", _points_part))


def _serialize_geometry(geometry, sections, attributes, prefix="", as_subd=False,
                        definition=None, part=None, notes=None):
    """Append one GeometrySet's geometry (mesh, curves, point cloud) to
    ``sections`` / ``attributes``, under an optional definition prefix.

    ``part`` is the part index of a multi-part definition (§5.2); it is ``None``
    for the frame's own geometry and for a single-part definition."""
    for member, read in _GEOMETRY_PARTS:
        read(
            getattr(geometry, member), sections, attributes, prefix, as_subd,
            definition, part, notes,
        )


def _has_components(geometry):
    """Whether a GeometrySet carries geometry of its own (as opposed to only
    instance references)."""
    mesh, curves, cloud = geometry.mesh, geometry.curves, geometry.pointcloud
    return bool(
        (mesh is not None and len(mesh.vertices))
        or (curves is not None and len(curves.points))
        or (cloud is not None and len(cloud.points))
    )


def _instance_rows(geometry, notes=None):
    """The rows of a GeometrySet's instances component as
    ``[(reference, 16 floats)]``, or None when it has none.

    ``reference`` may be a GeometrySet (node-built geometry), a Collection or an
    Object (the shapes Blender's instance references take).
    """
    try:
        refs = list(geometry.instance_references())
    except AttributeError:
        return None
    if not refs:
        return None
    cloud = geometry.instances_pointcloud()
    if cloud is None or not len(cloud.points):
        return None
    index_attr = cloud.attributes.get(".reference_index")
    transform_attr = cloud.attributes.get("instance_transform")
    if index_attr is None or transform_attr is None:
        message = "instance rows have no reference_index/instance_transform and were skipped"
        prefs.log("[stageloom] " + message, flush=True)
        _note(notes, message)
        return None

    n = len(cloud.points)
    indices = np.empty(n, dtype=np.int32)
    index_attr.data.foreach_get("value", indices)
    # instance_transform reads column-major (Blender's memory layout); the wire
    # is row-major with the translation in the fourth column (protocol §5.2).
    memory = np.empty(n * 16, dtype=np.float32)
    transform_attr.data.foreach_get("value", memory)
    matrices = memory.reshape(n, 4, 4).transpose(0, 2, 1).reshape(-1)

    rows = []
    skipped = 0
    for row in range(n):
        slot = int(indices[row])
        if 0 <= slot < len(refs):
            rows.append((refs[slot], matrices[row * 16:(row + 1) * 16]))
        else:
            skipped += 1
            prefs.log(
                "[stageloom] instance row %d skipped: reference index %d out of range" % (row, slot),
                flush=True,
            )
    _count_note(notes, skipped,
                "instance row referenced a missing definition and was skipped",
                "instance rows referenced a missing definition and were skipped")
    return rows or None


_GEOMETRY_MEMBER_TYPES = frozenset(
    ("MESH", "CURVE", "SURFACE", "POINTCLOUD", "CURVES", "FONT", "META",
     "GREASEPENCIL", "VOLUME")
)


def _depth_exceeded(notes):
    """The one visible warning and log line for the nesting cap."""
    message = "instance nesting deeper than %d was skipped" % MAX_INSTANCE_DEPTH
    prefs.log("[stageloom] " + message, flush=True)
    _note(notes, message)


def _reference_parts(reference, depth, notes=None):
    """A raw Collection or Object reference as ``(units, rows)``.

    A **collection** contributes one placement row per member — Blender applies
    a member's own transform when a collection is instanced — plus its child
    collections, recursively. An **object** contributes its evaluated geometry;
    an empty that instances a collection contributes that collection's content
    (the row already carries the empty's placement, so it is not applied
    twice)."""
    import bpy

    if reference is None:
        return [], []
    if depth >= MAX_INSTANCE_DEPTH:
        _depth_exceeded(notes)
        return [], []
    try:
        depsgraph = bpy.context.evaluated_depsgraph_get()
        evaluated = reference.evaluated_get(depsgraph)
    except Exception as ex:
        message = "1 instance reference could not be evaluated and was skipped"
        prefs.log("[stageloom] instance reference not evaluable: %r" % (ex,), flush=True)
        _note(notes, message)
        return [], []

    if isinstance(reference, bpy.types.Collection):
        units, rows = [], []
        skipped = 0
        for member in evaluated.objects:
            member_units, member_rows = _member_parts(member, depth, notes=notes)
            if not member_units and not member_rows and member.type != "EMPTY":
                skipped += 1
            units += member_units
            rows += member_rows
        _count_note(notes, skipped,
                    "collection member had no usable geometry and was skipped",
                    "collection members had no usable geometry and were skipped")
        for child in getattr(evaluated, "children", None) or ():
            child_units, child_rows = _reference_parts(child, depth + 1, notes=notes)
            units += child_units
            rows += child_rows
        return units, rows
    return _object_parts(evaluated, depth, notes=notes)


def _member_parts(obj, depth, notes=None):
    """One collection member as ``(units, rows)``: a placement row to the
    member, whose own transform Blender applies. Non-geometry members (cameras,
    lights) and bare empties contribute nothing."""
    if obj is None:
        return [], []
    if obj.type == "EMPTY":
        if _instances_a_collection(obj):
            return [], [(obj, _matrix_wire(obj.matrix_world))]
        return [], []
    if obj.type not in _GEOMETRY_MEMBER_TYPES:
        return [], []
    return [], [(obj, _matrix_wire(obj.matrix_world))]


def _object_parts(obj, depth, notes=None):
    """A direct object reference as ``(units, rows)``: its evaluated geometry,
    or the content of the collection an empty instances."""
    import bpy

    if obj.type == "EMPTY":
        if _instances_a_collection(obj):
            return _reference_parts(obj.instance_collection, depth + 1, notes=notes)
        return [], []
    try:
        return [bpy.types.GeometrySet.from_evaluated_object(obj)], []
    except Exception as ex:
        message = "1 instance reference object could not be read and was skipped"
        prefs.log(
            "[stageloom] instance reference %r not readable: %r" % (obj.name, ex),
            flush=True,
        )
        _note(notes, message)
        return [], []


def _instances_a_collection(obj):
    return (getattr(obj, "instance_type", "NONE") == "COLLECTION"
            and getattr(obj, "instance_collection", None) is not None)


def _matrix_wire(matrix):
    """A mathutils matrix as the wire's 16 floats: row-major, translation in
    the fourth column (protocol §5.2) — the Python row view is that order."""
    return np.asarray([list(row) for row in matrix], dtype=np.float32).reshape(-1)


def _definition_parts(reference, depth, notes=None):
    """Split one instance reference into ``(units, nested_rows)``.

    ``units`` are GeometrySet sources whose geometry belongs to the definition
    itself; ``nested_rows`` are ``(reference, 16 floats)`` placements that become
    the definition's own instance rows (nested blocks).

    A GeometrySet contributes its own components as a unit — the unit's own
    instance rows are resolved by the caller from the unit, so they are not
    returned here (that would double them). A set with no components at all
    whose rows are all identity placements — the Collection Info shape — is
    *hoisted*: its references become this definition's content instead of an
    extra nesting level. Anything else keeps its rows as nested placements.
    """
    if not hasattr(reference, "instance_references"):
        return _reference_parts(reference, depth, notes=notes)

    if _has_components(reference):
        return [reference], []
    rows = _instance_rows(reference, notes=notes) or []
    if not rows:
        return [], []
    if depth < MAX_INSTANCE_DEPTH and all(_is_identity(matrix) for _ref, matrix in rows):
        units = []
        nested = []
        for sub_reference, _matrix in rows:
            sub_units, sub_rows = _definition_parts(sub_reference, depth + 1, notes=notes)
            units.extend(sub_units)
            nested.extend(sub_rows)
        return units, nested
    return [], rows


def _is_identity(matrix):
    return all(float(matrix[i]) == _IDENTITY[i] for i in range(16))


def _definition_prefix(index, part=None):
    """The §5.2 namespace of one definition or one of its parts."""
    return "idef.%d." % index if part is None else "idef.%d.%d." % (index, part)


def _append_definition_rows(sections, prefix, ref_def, transforms):
    """Append one definition's ``inst.*`` row pair, all-or-nothing."""
    if not ref_def:
        return
    sections.append((prefix + "inst.ref_def", "u32", np.asarray(ref_def, dtype=np.uint32)))
    sections.append(
        (prefix + "inst.transforms", "f32",
         np.asarray(transforms, dtype=np.float32).reshape(-1))
    )


def _serialize_definition(reference, sections, attributes, definitions, depth,
                          notes=None, blocks_2=True):
    """Serialize one definition into the flat table. Returns its indices.

    A definition with one unit (or none) keeps the original single-part
    namespace ``idef.<i>.*`` and no ``parts`` field. A definition with more than
    one unit emits ``parts: N`` and one ``idef.<i>.<part>.`` namespace per part.
    When the peer cannot read multi-part definitions (``blocks_2`` is false),
    each part becomes its own single-part definition and the returned list names
    all of them in part order, so every row that referenced the original one
    fans out to the same placements instead of losing a part.

    Rows come from two sources, resolved through one dictionary so a child
    reached twice lands once: the definition's nested placements, and every
    unit's own instances — an object member whose geometry carries instances of
    its own (a cube whose modifier instances a sphere, say). Nested rows stay at
    definition level (``idef.<i>.inst.*``) in every form. In a fallback split
    they belong to the first part, which keeps the nested content drawn once.
    """
    name = _reference_name(reference)
    if depth >= MAX_INSTANCE_DEPTH:
        index = len(definitions)
        definitions.append({"name": name})
        _depth_exceeded(notes)
        return [index]

    units, nested = _definition_parts(reference, depth, notes=notes)
    parts = len(units)
    split = parts > 1 and not blocks_2
    resolved = {}

    def resolve(child_reference, matrix):
        children = resolved.get(id(child_reference))
        if children is None:
            children = _serialize_definition(
                child_reference, sections, attributes, definitions, depth + 1,
                notes=notes, blocks_2=blocks_2,
            )
            resolved[id(child_reference)] = children
        for child in children:
            ref_def.append(child)
            transforms.extend(matrix)

    if not split:
        index = len(definitions)
        definitions.append(
            {"name": name, "parts": parts} if parts > 1 else {"name": name}
        )

    indices = []
    ref_def = []
    transforms = []
    for part, unit in enumerate(units):
        if split:
            # No `blocks/2` on the peer: one single-part definition per part. An
            # old peer ignores `parts` and the part namespaces, so emitting them
            # anyway would collapse the definition silently (§8).
            index = len(definitions)
            definitions.append({"name": _split_part_name(name, part, parts)})
            indices.append(index)
            ref_def = []
            transforms = []
            unit_part = None
        else:
            unit_part = part if parts > 1 else None
        _serialize_geometry(
            unit, sections, attributes, prefix=_definition_prefix(index, unit_part),
            definition=index, part=unit_part, notes=notes,
        )
        for child_reference, matrix in (_instance_rows(unit, notes=notes) or []):
            resolve(child_reference, matrix)
        if split:
            if part == 0:
                for child_reference, matrix in nested:
                    resolve(child_reference, matrix)
            _append_definition_rows(
                sections, _definition_prefix(index), ref_def, transforms
            )

    if not split:
        for child_reference, matrix in nested:
            resolve(child_reference, matrix)
        _append_definition_rows(
            sections, _definition_prefix(index), ref_def, transforms
        )
        return [index]

    _note(
        notes,
        "definition %r has %d parts; Grasshopper does not advertise blocks/2, "
        "so it was split into %d single-part definitions" % (name or "?", parts, parts),
    )
    return indices


def _split_part_name(name, part, parts):
    """The display name of one part of a fallback split.

    Names are display-only (§5.2), but distinct names keep an old peer's block
    table from colliding two parts into one block name. An unnamed definition
    stays unnamed rather than inventing a name.
    """
    if not name:
        return ""
    return "%s [part %d/%d]" % (name, part, parts)


def _serialize_instances(geometry, sections, attributes, definitions, depth,
                         notes=None, blocks_2=True):
    """Serialize a GeometrySet's instances component.

    Returns ``(ref_def, transforms)`` — the definition index per row and the
    flat row-major matrices — or None when the set has no instances. The
    definitions the rows reference are appended to the shared flat table
    (``definitions``); a definition that holds instances of its own recurses.
    A fallback-split definition returns several indices, so its row fans out to
    one row per part at the same placement.
    """
    rows = _instance_rows(geometry, notes=notes)
    if not rows:
        return None
    if depth >= MAX_INSTANCE_DEPTH:
        _depth_exceeded(notes)
        return None

    local = {}
    ref_def = []
    transforms = []
    for reference, matrix in rows:
        indices = local.get(id(reference))
        if indices is None:
            indices = _serialize_definition(
                reference, sections, attributes, definitions, depth + 1,
                notes=notes, blocks_2=blocks_2,
            )
            local[id(reference)] = indices
        for index in indices:
            ref_def.append(index)
            transforms.extend(matrix)
    return ref_def, transforms


def _reference_name(reference):
    """The display name of one instance reference (a GeometrySet, an Object or a
    Collection), or ""."""
    name = getattr(reference, "name", None)
    return name if isinstance(name, str) else ""
