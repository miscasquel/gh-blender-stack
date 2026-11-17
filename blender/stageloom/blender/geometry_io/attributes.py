"""The attribute wire vocabulary and its Blender mapping.

One ``_FOREACH`` table owns the Blender type -> foreach property/dtype mapping
in both directions; ``WIRE_TYPES`` (in core) owns which types travel.
"""

import numpy as np

from ...core import attributes as attr_mod

from .common import (COLOR_ATTRIBUTE, CREASE_ATTRIBUTE, GeometryIOError,
                     _active_color, _count_note)

# Blender attribute type -> (foreach property, dtype). The dtype matters:
# Blender refuses float buffers for ints; STRING is element-wise (no row).
_FOREACH = {
    "FLOAT": ("value", np.float32, np.float32),
    "INT": ("value", np.int32, np.int32),
    "BOOLEAN": ("value", np.bool_, np.bool_),
    "FLOAT_VECTOR": ("vector", np.float32, np.float32),
    "FLOAT2": ("vector", np.float32, np.float32),
    "FLOAT4": ("vector", np.float32, np.float32),
    "FLOAT_COLOR": ("color", np.float32, np.float32),
    "BYTE_COLOR": ("color_srgb", np.float32, np.float32),
    "QUATERNION": ("value", np.float32, np.float32),
    "FLOAT4X4": ("value", np.float32, np.float32),
    "INT8": ("value", np.int8, np.int8),
    "INT16_2D": ("value", np.int32, np.int32),
    "INT32_2D": ("value", np.int32, np.int32),
}


def _attribute_values(attr):
    """One attribute's values in wire form.

    Non-STRING attributes come back as a flat numpy array in the wire dtype
    (u8 for BOOLEAN/BYTE_COLOR, i32 for INT8, f32 otherwise); STRING comes back
    as a list of byte strings. Keeping this in one place lets the mesh and curve
    readers reindex values without duplicating the Blender type handling.
    """
    type_name = attr.data_type
    if type_name == "STRING":
        # Strings have no foreach path; element access is the only way and
        # the counts are small by nature.
        return [bytes(element.value) for element in attr.data]

    prop, buf_dtype, _write_dtype = _FOREACH[type_name]
    comps = attr_mod.components(type_name)
    buf = np.zeros(len(attr.data) * comps, dtype=buf_dtype)
    attr.data.foreach_get(prop, buf)
    if type_name == "BYTE_COLOR":
        return np.clip(np.rint(buf * 255.0), 0.0, 255.0).astype(np.uint8)
    if type_name == "BOOLEAN":
        return buf.astype(np.uint8)
    if type_name == "INT8":
        return buf.astype(np.int32)
    return np.ascontiguousarray(buf)


def _take_attribute_values(values, indices, comps):
    """The values at ``indices``, flattened in that order.

    ``values`` is either the numpy array or string list ``_attribute_values``
    returned. This is how a mesh attribute is reindexed onto the curves and
    points the exporter emits.
    """
    if isinstance(values, list):
        return [values[int(index)] for index in indices]
    if len(indices) == 0:
        return values[:0]
    return np.ascontiguousarray(values.reshape(-1, comps)[indices].reshape(-1))


def _emit_attribute(group, name, domain, type_name, values, index, prefix="",
                    definition=None, part=None):
    """Sections plus descriptor for one already-read attribute value array."""
    value_section, offsets_section = attr_mod.section_names(index, prefix)
    if type_name == "STRING":
        data = values
        raw = np.frombuffer(b"".join(data), dtype=np.uint8) if data else np.zeros(0, np.uint8)
        sections = [
            (value_section, "u8", raw),
            (offsets_section, "u32", np.asarray([len(item) for item in data], dtype=np.uint32)),
        ]
        count = len(data)
    else:
        sections = [(value_section, attr_mod.wire_dtype(type_name), values)]
        count = len(values) // attr_mod.components(type_name)

    descriptor = attr_mod.descriptor(
        group, name, domain, type_name, count, index,
        prefix=prefix, definition=definition, part=part,
    )
    return sections, descriptor


def _emit_mapped(sections, descriptors, index0, group, name, domain, type_name,
                 values, indices, comps, prefix="", definition=None, part=None):
    """Reindex one attribute onto ``indices`` and append its sections/descriptor."""
    mapped = _take_attribute_values(values, indices, comps)
    index = index0 + len(descriptors)
    attribute_sections, descriptor = _emit_attribute(
        group, name, domain, type_name, mapped, index,
        prefix=prefix, definition=definition, part=part,
    )
    sections += attribute_sections
    descriptors.append(descriptor)


def read_attributes(owner, group, index0=0, skip=(), prefix="", definition=None,
                    part=None):
    """Every transportable attribute of one geometry data owner, as
    ``(sections, descriptors)``.

    ``owner`` is anything with Blender's attribute API (Mesh, PointCloud,
    Curves component). Internal attributes (leading dot) and the geometry
    itself (``position``) are skipped, as is anything in ``skip`` — the names
    the caller already carries through a native channel (``mesh.colors``,
    ``crease_edge``), so nothing travels twice. ``index0`` is the running
    attribute count of the frame: the sections are named from it. ``prefix``
    and ``definition`` namespace a definition's attributes (§5.2).
    """
    sections = []
    descriptors = []
    attrs = getattr(owner, "attributes", None)
    if attrs is None:
        return sections, descriptors

    for attr in attrs:
        name = attr.name
        if name.startswith(".") or name in attr_mod.SKIP_NAMES or name in skip:
            continue
        type_name = attr.data_type
        if type_name not in attr_mod.WIRE_TYPES:
            continue
        index = index0 + len(descriptors)
        attribute_sections, descriptor = _emit_attribute(
            group, name, attr.domain, type_name, _attribute_values(attr), index,
            prefix=prefix, definition=definition, part=part,
        )
        sections += attribute_sections
        descriptors.append(descriptor)
    return sections, descriptors


def _native_skip(owner):
    """The colour attribute name the native channel already carries."""
    color = _active_color(owner)
    return [] if color is None else [color.name]


def mesh_attributes(mesh, as_subd=False, index0=0, prefix="", definition=None,
                    part=None):
    """The generic attributes of a mesh (or SubD cage). The active colour
    attribute is excluded only when the native ``mesh.colors`` channel carries
    it, so a SubD cage's colours travel as an ordinary attribute."""
    skip = [CREASE_ATTRIBUTE]
    if not as_subd:
        skip += _native_skip(mesh)
    return read_attributes(
        mesh, "subd" if as_subd else "mesh", index0=index0, skip=skip,
        prefix=prefix, definition=definition, part=part,
    )


def points_attributes(cloud, index0=0, prefix="", definition=None, part=None,
                      notes=None):
    """The generic attributes of a point cloud. The native ``pts.colors``
    channel carries the active colour attribute; everything else travels."""
    return read_attributes(
        cloud, "pts", index0=index0, skip=_native_skip(cloud),
        prefix=prefix, definition=definition, part=part,
    )


def wire_attributes(mesh, loose, edge_verts, isolated, index0=0, prefix="", definition=None,
                    part=None, notes=None):
    """Reindex a mesh's attributes onto its emitted curves and points.

    EDGE attributes become one value per curve (domain ``CURVE``). POINT
    attributes become two values per curve, in edge order ``(a, b)``, and one
    value per isolated vertex in the ``pts`` group. FACE and CORNER attributes
    have no wire/point target and are skipped.
    """
    sections = []
    descriptors = []
    if not len(loose) and not len(isolated):
        return sections, descriptors

    curve_point_indices = edge_verts[loose].reshape(-1) if len(loose) else None
    unsupported_type = 0
    unsupported_domain = 0
    for attr in mesh.attributes:
        name = attr.name
        if name.startswith(".") or name in attr_mod.SKIP_NAMES or name == CREASE_ATTRIBUTE:
            continue
        type_name = attr.data_type
        if type_name not in attr_mod.WIRE_TYPES:
            unsupported_type += 1
            continue
        values = _attribute_values(attr)
        comps = attr_mod.components(type_name)

        if attr.domain == "EDGE" and len(loose):
            _emit_mapped(
                sections, descriptors, index0, "crv", name, "CURVE", type_name,
                values, loose, comps, prefix=prefix, definition=definition, part=part,
            )
            continue

        if attr.domain != "POINT":
            unsupported_domain += 1
            continue

        if len(loose):
            _emit_mapped(
                sections, descriptors, index0, "crv", name, "POINT", type_name,
                values, curve_point_indices, comps,
                prefix=prefix, definition=definition, part=part,
            )
        if len(isolated):
            _emit_mapped(
                sections, descriptors, index0, "pts", name, "POINT", type_name,
                values, isolated, comps, prefix=prefix, definition=definition, part=part,
            )

    _count_note(notes, unsupported_domain,
                "wire/point attribute had a domain with no wire target and was skipped",
                "wire/point attributes had a domain with no wire target and were skipped")
    _count_note(notes, unsupported_type,
                "wire/point attribute had an unsupported type and was skipped",
                "wire/point attributes had an unsupported type and were skipped")
    return sections, descriptors


def write_attributes(owner, entries, prune=False):
    """Create or update attributes from decoded wire sections.

    ``entries`` is ``(descriptor, values, offsets)`` per attribute, straight
    from a decoded frame. Values that do not fit the target domain are dropped
    with a note in the returned list — a wrong count is a topology mismatch, and
    writing it anyway would be silent corruption.

    ``prune`` removes stage-owned generic attributes the frame no longer
    carries. The native colour channel and the crease attribute are managed by
    their own paths and are never removed here; leading-dot names are Blender's
    internals.
    """
    notes = []
    written = set()
    for descriptor, values, offsets in entries:
        name = descriptor.get("name") or "?"
        domain = descriptor.get("domain") or "POINT"
        type_name = descriptor.get("type")
        spec = attr_mod.WIRE_TYPES.get(type_name)
        if spec is None:
            notes.append("attribute %r: unknown type %r" % (name, type_name))
            continue
        target = _domain_length(owner, domain)
        if target is None:
            notes.append("attribute %r: %s has no %s domain" % (name, describe(owner), domain))
            continue

        if type_name == "STRING":
            try:
                data = _decode_strings(values, offsets)
            except GeometryIOError as exc:
                # One malformed STRING must not take the whole frame down.
                notes.append("attribute %r: %s" % (name, exc))
                continue
            if len(data) != target:
                notes.append(
                    "attribute %r: %d strings for %d %s elements"
                    % (name, len(data), target, domain.lower())
                )
                continue
            attr = _attribute_for(owner, name, domain, type_name)
            for i, raw in enumerate(data):
                attr.data[i].value = raw
            written.add(name)
            continue

        prop, _read_dtype, write_dtype = _FOREACH[type_name]
        comps = spec[1]
        arr = np.asarray(values)
        count = arr.size // comps
        if count != target:
            notes.append(
                "attribute %r: %d values for %d %s elements"
                % (name, count, target, domain.lower())
            )
            continue
        attr = _attribute_for(owner, name, domain, type_name)
        out = arr.astype(np.float32) / 255.0 if type_name == "BYTE_COLOR" else arr.reshape(-1).astype(write_dtype)
        attr.data.foreach_set(prop, out)
        written.add(name)

    if prune:
        keep = written
        for attr in list(owner.attributes):
            name = attr.name
            if (name in keep or name.startswith(".") or name in attr_mod.SKIP_NAMES
                    or name in (COLOR_ATTRIBUTE, CREASE_ATTRIBUTE)):
                continue
            try:
                owner.attributes.remove(attr)
            except RuntimeError:
                # Blender marks some channels required (for example a point
                # cloud's position). They are internal and cannot be removed.
                pass
    return notes


def describe(owner):
    """A short name for an attribute owner, for notes."""
    if hasattr(owner, "vertices"):
        return "mesh"
    if hasattr(owner, "points"):
        return "point cloud"
    return "geometry"


def _attribute_for(owner, name, domain, type_name):
    """One attribute of the owner, created or replaced when its shape changed."""
    attr = owner.attributes.get(name)
    if attr is not None and (attr.domain != domain or attr.data_type != type_name):
        owner.attributes.remove(attr)
        attr = None
    if attr is None:
        attr = owner.attributes.new(name, type_name, domain)
    return attr


def _domain_length(owner, domain):
    """Element count of one domain, or None when the owner has no such domain."""
    if hasattr(owner, "vertices"):
        counts = {
            "POINT": len(owner.vertices),
            "EDGE": len(owner.edges),
            "FACE": len(owner.polygons),
            "CORNER": len(owner.loops),
        }
        return counts.get(domain)
    if hasattr(owner, "points"):
        return len(owner.points) if domain == "POINT" else None
    return None


def _decode_strings(values, offsets):
    """The wire's concatenated UTF-8 bytes plus per-string lengths, as bytes.

    A short slice or sum mismatch is a malformed STRING: raise, and let the
    caller skip just that attribute.
    """
    raw = (
        np.asarray(values, dtype=np.uint8).tobytes()
        if not isinstance(values, (bytes, bytearray))
        else bytes(values)
    )
    if offsets is None:
        if raw:
            raise GeometryIOError("STRING values have no offsets section")
        return []
    lengths = [int(length) for length in offsets]
    data = []
    at = 0
    for length in lengths:
        if length < 0 or at + length > len(raw):
            raise GeometryIOError(
                "length %d at offset %d runs past the %d value bytes"
                % (length, at, len(raw))
            )
        data.append(raw[at : at + length])
        at += length
    if at != len(raw):
        raise GeometryIOError(
            "offsets cover %d of %d value bytes" % (at, len(raw))
        )
    return data
