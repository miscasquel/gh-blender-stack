"""Geometry <-> protocol sections, on the Blender side.

Reads evaluated geometry into protocol v2 sections and writes sections back in
place where topology allows. Section names follow protocol §5. Curves keep
order, weights and endpoint/uniform modes; what Blender cannot express exactly
is returned as a counted note on the stage's input warnings (and warned about
on the GH side where that side owns the approximation). This is the bpy layer;
core stays pure.
"""

import numpy as np

from ..core import attributes as attr_mod

from . import prefs

# The serialization order per protocol §5 lives in hashing.HASH_SECTION_ORDER;
# this module reads and writes sections by name.
CREASE_ATTRIBUTE = "crease_edge"
COLOR_ATTRIBUTE = "Col"

CURVE_POLY = 0
CURVE_BEZIER = 1
CURVE_NURBS = 2

# The Curves component's own channels are the geometry, not user attributes.
# They are already carried by crv.* and must not be duplicated as generic ones.
CURVE_INTERNAL_ATTRIBUTES = (
    "curve_type",
    "nurbs_weight",
    "nurbs_order",
    "knots_mode",
    "cyclic",
    "handle_left",
    "handle_right",
)

KNOTS_NORMAL = 0
KNOTS_ENDPOINT = 1
KNOTS_BEZIER = 2

# Blender's default point radius. ``pts.radii`` absent means "no per-point
# radii", so the write path resets every point to this value instead of leaving
# a previous response's radii behind.
POINT_RADIUS_DEFAULT = 0.01


class GeometryIOError(Exception):
    pass


def _note(notes, text):
    """Append one user-visible conversion note, when a channel was supplied."""
    if notes is not None:
        notes.append(text)


def _count_note(notes, count, singular, plural):
    """Append a counted note, so a loss is visible without a line per item."""
    if notes is not None and count:
        notes.append("%d %s" % (count, singular if count == 1 else plural))


def _linear_to_srgb(values):
    """Scene-linear floats to sRGB floats, the transfer Blender's native
    FLOAT_COLOR channel needs before the wire's u8 sRGB quantisation."""
    linear = np.clip(np.asarray(values, dtype=np.float32), 0.0, 1.0)
    return np.where(
        linear <= 0.0031308,
        linear * 12.92,
        1.055 * np.power(linear, 1.0 / 2.4) - 0.055,
    ).astype(np.float32)


def _color_bytes(attr, count):
    """One colour attribute as wire u8 RGBA.

    BYTE_COLOR already stores display-referred sRGB, so ``color_srgb`` is read
    directly. FLOAT_COLOR stores scene-linear values in ``color``; those are
    converted to sRGB before quantisation. The alpha channel is linear in both
    representations (it is opacity, not colour) and is never transfer-encoded.
    """
    floats = np.empty(count * 4, dtype=np.float32)
    if attr.data_type == "BYTE_COLOR":
        attr.data.foreach_get("color_srgb", floats)
    else:
        attr.data.foreach_get("color", floats)
        rgba = floats.reshape(count, 4)
        rgba[:, :3] = _linear_to_srgb(rgba[:, :3])
    return np.clip(np.rint(floats.reshape(count, 4) * 255.0), 0.0, 255.0).astype(np.uint8)


# --------------------------------------------------------------------- read


def mesh_to_sections(mesh, as_subd=False, prefix="", notes=None):
    """Read ``mesh`` into a list of ``(name, dtype, numpy array)`` sections.

    ``as_subd`` selects the ``subd.*`` names and omits colours (protocol §5).
    ``prefix`` namespaces the sections of one instance definition (§5.2),
    e.g. ``"idef.0."``. ``notes`` collects user-visible conversion losses.
    """
    group = "subd" if as_subd else "mesh"
    name = prefix + group
    n_verts = len(mesh.vertices)
    n_loops = len(mesh.loops)
    n_polys = len(mesh.polygons)

    verts = np.empty(n_verts * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", verts)

    counts = np.empty(n_polys, dtype=np.uint32)
    mesh.polygons.foreach_get("loop_total", counts)

    indices = np.empty(n_loops, dtype=np.uint32)
    mesh.loops.foreach_get("vertex_index", indices)

    sections = [
        (name + ".verts", "f32", verts.reshape(-1, 3)),
        (name + ".face_counts", "u32", counts),
        (name + ".face_indices", "u32", indices),
    ]

    if not as_subd:
        colors = read_colors(mesh)
        if colors is not None:
            sections.append((name + ".colors", "u8", colors))

    creases = read_creases(mesh, notes=notes)
    if creases is not None:
        edges, values = creases
        sections.append((name + ".crease_edges", "u32", edges.reshape(-1, 2)))
        sections.append((name + ".crease_values", "f32", values))

    return sections


def read_colors(mesh):
    """The active colour attribute as wire u8 N×4 sRGB, or None.

    BYTE_COLOR already stores display-referred sRGB, so ``color_srgb`` is read
    directly. FLOAT_COLOR stores scene-linear floats; ``color`` is converted to
    sRGB before quantisation. Both paths read floats because reading into a u8
    buffer truncates the float representation instead of converting.

    A CORNER-domain attribute is folded to one entry per vertex; the last loop that
    references a vertex wins, which is deterministic for Blender's loop order.
    """
    attr = _active_color(mesh)
    if attr is None:
        return None
    n = len(mesh.vertices) if attr.domain == "POINT" else len(mesh.loops)
    out = _color_bytes(attr, n)

    if attr.domain == "POINT":
        return out

    vertex_of_loop = np.empty(len(mesh.loops), dtype=np.uint32)
    mesh.loops.foreach_get("vertex_index", vertex_of_loop)
    folded = np.zeros((len(mesh.vertices), 4), dtype=np.uint8)
    folded[vertex_of_loop] = out
    return folded


def read_creases(mesh, notes=None):
    """Edges with a non-zero crease as ``(edges N×2 u32, values N f32)``, or None.

    Fractional values are passed through; the GH side rounds them to the nearest
    tag and warns (D2). Edges at exactly zero are omitted: absent means smooth.
    A crease channel Blender cannot read as EDGE/FLOAT is skipped and noted.
    """
    attr = mesh.attributes.get(CREASE_ATTRIBUTE)
    if attr is None:
        return None
    if attr.domain != "EDGE" or attr.data_type != "FLOAT":
        _note(
            notes,
            "crease_edge is %s/%s, not EDGE/FLOAT; creases were skipped"
            % (attr.domain, attr.data_type),
        )
        return None
    if len(attr.data) != len(mesh.edges):
        _note(
            notes,
            "crease_edge has %d values for %d edges; creases were skipped"
            % (len(attr.data), len(mesh.edges)),
        )
        return None
    values = np.empty(len(mesh.edges), dtype=np.float32)
    attr.data.foreach_get("value", values)
    mask = values > 0.0
    if not mask.any():
        return None
    edge_verts = np.empty(len(mesh.edges) * 2, dtype=np.uint32)
    mesh.edges.foreach_get("vertices", edge_verts)
    return edge_verts.reshape(-1, 2)[mask], values[mask]


def _active_color(owner):
    """The active colour attribute (or the first one), or None. One source of
    truth for the native colour mapping and for the skip list of the generic
    attribute reader, so an attribute is never sent twice."""
    attrs = owner.color_attributes
    if not attrs:
        return None
    return attrs.active_color if attrs.active_color is not None else attrs[0]


# ------------------------------------------------------------------ attributes


# Blender attribute data type -> (foreach property, buffer dtype). The buffer
# dtype matters: Blender refuses float buffers for integer-typed attributes.
_FOREACH = {
    "FLOAT": ("value", np.float32),
    "INT": ("value", np.int32),
    "BOOLEAN": ("value", np.bool_),
    "FLOAT_VECTOR": ("vector", np.float32),
    "FLOAT2": ("vector", np.float32),
    "FLOAT4": ("vector", np.float32),
    "FLOAT_COLOR": ("color", np.float32),
    "BYTE_COLOR": ("color_srgb", np.float32),
    "QUATERNION": ("value", np.float32),
    "FLOAT4X4": ("value", np.float32),
    "INT8": ("value", np.int8),
    "INT16_2D": ("value", np.int32),
    "INT32_2D": ("value", np.int32),
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

    prop, buf_dtype = _FOREACH[type_name]
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


def mesh_attributes(mesh, as_subd=False, index0=0, prefix="", definition=None,
                    part=None):
    """The generic attributes of a mesh (or SubD cage). The active colour
    attribute is excluded only when the native ``mesh.colors`` channel carries
    it, so a SubD cage's colours travel as an ordinary attribute."""
    skip = [CREASE_ATTRIBUTE]
    if not as_subd:
        color = _active_color(mesh)
        if color is not None:
            skip.append(color.name)
    return read_attributes(
        mesh, "subd" if as_subd else "mesh", index0=index0, skip=skip,
        prefix=prefix, definition=definition, part=part,
    )


def loose_edge_indices(mesh):
    """The mesh edges that belong to no face, as a u32 array.

    A face-less mesh is all wire. For a mesh with faces, ``is_loose`` is read
    through ``foreach_get``: the per-edge Python property costs a round trip for
    every edge of every face mesh, which is measurable on large stages.
    """
    n = len(mesh.edges)
    if n == 0:
        return np.zeros(0, dtype=np.uint32)
    if len(mesh.polygons) == 0:
        return np.arange(n, dtype=np.uint32)
    mask = np.empty(n, dtype=bool)
    mesh.edges.foreach_get("is_loose", mask)
    return np.nonzero(mask)[0].astype(np.uint32)


def isolated_vertex_indices(mesh):
    """Vertices referenced by no edge, as a u32 array."""
    n = len(mesh.vertices)
    if n == 0:
        return np.zeros(0, dtype=np.uint32)
    if len(mesh.edges) == 0:
        return np.arange(n, dtype=np.uint32)
    edge_verts = np.empty(len(mesh.edges) * 2, dtype=np.uint32)
    mesh.edges.foreach_get("vertices", edge_verts)
    degree = np.bincount(edge_verts, minlength=n)
    return np.nonzero(degree == 0)[0].astype(np.uint32)


def wire_mesh_to_sections(mesh, index0=0, prefix="", definition=None, part=None,
                          notes=None):
    """Loose mesh edges and vertices as curves and points, plus their attributes.

    Each loose edge becomes one two-point polyline. That is the only lossless
    mapping for an EDGE attribute: the value belongs to exactly one curve, so
    ``Top``/``Diag``-style booleans stay one-per-curve instead of being averaged
    or folded into a merged chain. Vertices with no edge become a point cloud.

    Returns ``(sections, attributes)``. A mesh with faces keeps its ``mesh.*``
    sections elsewhere; this function only carries the loose remainder.
    """
    loose = loose_edge_indices(mesh)
    isolated = isolated_vertex_indices(mesh)
    if not len(loose) and not len(isolated):
        return [], []

    n_verts = len(mesh.vertices)
    edge_verts = np.empty(len(mesh.edges) * 2, dtype=np.uint32)
    mesh.edges.foreach_get("vertices", edge_verts)
    edge_verts = edge_verts.reshape(-1, 2)

    verts = np.empty(n_verts * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", verts)
    verts = verts.reshape(-1, 3)

    sections = []
    if len(loose):
        pairs = edge_verts[loose]
        points = np.empty((len(loose) * 2, 4), dtype=np.float32)
        points[:, :3] = verts[pairs.reshape(-1)]
        points[:, 3] = 1.0
        name = prefix + "crv"
        sections += [
            (name + ".points", "f32", points),
            (name + ".counts", "u32", np.full(len(loose), 2, dtype=np.uint32)),
            (name + ".types", "u8", np.zeros(len(loose), dtype=np.uint8)),
            (name + ".orders", "u8", np.zeros(len(loose), dtype=np.uint8)),
            (name + ".knots_mode", "u8", np.zeros(len(loose), dtype=np.uint8)),
            (name + ".cyclic", "u8", np.zeros(len(loose), dtype=np.uint8)),
        ]

    if len(isolated):
        sections.append(
            (prefix + "pts.positions", "f32", np.ascontiguousarray(verts[isolated]))
        )

    if len(mesh.polygons) == 0:
        # A face-less mesh travels entirely as crv/pts, neither of which carries
        # the crease channel; say how many authored crease edges were dropped.
        creases = read_creases(mesh, notes=notes)
        if creases is not None:
            _count_note(
                notes, len(creases[0]),
                "crease edge on a wire mesh had no wire representation and was dropped",
                "crease edges on a wire mesh had no wire representation and were dropped",
            )

    attribute_sections, attributes = wire_attributes(
        mesh, loose, edge_verts, isolated, index0=index0,
        prefix=prefix, definition=definition, part=part, notes=notes,
    )
    return sections + attribute_sections, attributes


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
            mapped = _take_attribute_values(values, loose, comps)
            index = index0 + len(descriptors)
            attribute_sections, descriptor = _emit_attribute(
                "crv", name, "CURVE", type_name, mapped, index,
                prefix=prefix, definition=definition, part=part,
            )
            sections += attribute_sections
            descriptors.append(descriptor)
            continue

        if attr.domain != "POINT":
            unsupported_domain += 1
            continue

        if len(loose):
            point_indices = edge_verts[loose].reshape(-1)
            mapped = _take_attribute_values(values, point_indices, comps)
            index = index0 + len(descriptors)
            attribute_sections, descriptor = _emit_attribute(
                "crv", name, "POINT", type_name, mapped, index,
                prefix=prefix, definition=definition, part=part,
            )
            sections += attribute_sections
            descriptors.append(descriptor)

        if len(isolated):
            mapped = _take_attribute_values(values, isolated, comps)
            index = index0 + len(descriptors)
            attribute_sections, descriptor = _emit_attribute(
                "pts", name, "POINT", type_name, mapped, index,
                prefix=prefix, definition=definition, part=part,
            )
            sections += attribute_sections
            descriptors.append(descriptor)

    _count_note(notes, unsupported_domain,
                "wire/point attribute had a domain with no wire target and was skipped",
                "wire/point attributes had a domain with no wire target and were skipped")
    _count_note(notes, unsupported_type,
                "wire/point attribute had an unsupported type and was skipped",
                "wire/point attributes had an unsupported type and were skipped")
    return sections, descriptors


def points_attributes(cloud, index0=0, prefix="", definition=None, part=None,
                      notes=None):
    """The generic attributes of a point cloud. The native ``pts.colors``
    channel carries the active colour attribute; everything else travels."""
    skip = []
    color = _active_color(cloud)
    if color is not None:
        skip.append(color.name)
    return read_attributes(
        cloud, "pts", index0=index0, skip=skip,
        prefix=prefix, definition=definition, part=part,
    )


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
                data, lengths = _decode_strings(values, offsets)
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
        if type_name == "BYTE_COLOR":
            attr.data.foreach_set("color_srgb", arr.astype(np.float32) / 255.0)
        elif type_name == "BOOLEAN":
            attr.data.foreach_set("value", arr.astype(bool))
        elif type_name == "INT8":
            attr.data.foreach_set("value", arr.astype(np.int8))
        elif type_name in ("FLOAT_VECTOR", "FLOAT2", "FLOAT4"):
            attr.data.foreach_set("vector", arr.reshape(-1).astype(np.float32))
        elif type_name == "FLOAT_COLOR":
            attr.data.foreach_set("color", arr.reshape(-1).astype(np.float32))
        elif type_name == "INT":
            attr.data.foreach_set("value", arr.reshape(-1).astype(np.int32))
        elif type_name in ("INT16_2D", "INT32_2D"):
            attr.data.foreach_set("value", arr.reshape(-1).astype(np.int32))
        else:  # FLOAT, QUATERNION, FLOAT4X4
            attr.data.foreach_set("value", arr.reshape(-1).astype(np.float32))
        written.add(name)

    if prune:
        keep = written
        for attr in list(owner.attributes):
            name = attr.name
            if name in keep:
                continue
            if name.startswith(".") or name in attr_mod.SKIP_NAMES:
                continue
            if name in (COLOR_ATTRIBUTE, CREASE_ATTRIBUTE):
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
        return [], []
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
    return data, lengths


# ------------------------------------------------------------ curves and points


def curve_to_sections(curve, prefix="", notes=None):
    """Read a Blender Curve datablock into ``crv.*`` sections (protocol §5).

    A Bezier spline travels as a degree-3 NURBS in Bezier knot mode, using the
    control net co0, handle_right0, handle_left1, co1, ... — the representation
    the GH side rebuilds exactly. A cyclic Bezier spline wraps the closing
    segment, so its net ends where it starts instead of carrying the cyclic flag.
    ``prefix`` namespaces a definition's sections (§5.2). ``notes`` collects
    splines this wire form cannot carry.
    """
    points = []
    counts = []
    types = []
    orders = []
    knots_mode = []
    cyclic = []
    empty_bezier = 0
    short_splines = 0
    bad_nurbs = 0

    for spline in curve.splines:
        if spline.type == "BEZIER":
            net = _bezier_net(spline)
            if net is None:
                empty_bezier += 1
                continue
            points.extend(net)
            counts.append(len(net))
            types.append(CURVE_NURBS)
            orders.append(4)
            knots_mode.append(KNOTS_BEZIER)
            cyclic.append(0)
            continue
        if spline.type == "NURBS":
            p = [(pt.co[0], pt.co[1], pt.co[2], float(pt.weight)) for pt in spline.points]
            order = int(spline.order_u)
            weights = [point[3] for point in p]
            if (
                len(p) < 2
                or order < 2
                or order > 6
                or order > len(p)
                or any(not np.isfinite(weight) or weight <= 0.0 for weight in weights)
            ):
                bad_nurbs += 1
                continue
            points.extend(p)
            counts.append(len(p))
            types.append(CURVE_NURBS)
            orders.append(order)
            knots_mode.append(KNOTS_ENDPOINT if spline.use_endpoint_u else KNOTS_NORMAL)
            cyclic.append(1 if spline.use_cyclic_u else 0)
            continue
        # POLY and anything else Blender has: a polyline through the points.
        p = [(pt.co[0], pt.co[1], pt.co[2], 1.0) for pt in spline.points]
        if len(p) < 2:
            short_splines += 1
            continue
        points.extend(p)
        counts.append(len(p))
        types.append(CURVE_POLY)
        orders.append(0)
        knots_mode.append(0)
        cyclic.append(1 if spline.use_cyclic_u else 0)

    _count_note(notes, empty_bezier,
                "Bezier spline had no span and was skipped",
                "Bezier splines had no span and were skipped")
    _count_note(notes, short_splines,
                "spline had fewer than two points and was skipped",
                "splines had fewer than two points and were skipped")
    _count_note(notes, bad_nurbs,
                "NURBS spline has an unsupported order or weight and was skipped",
                "NURBS splines have an unsupported order or weight and were skipped")

    if not counts:
        return []
    flat = np.asarray(points, dtype=np.float32).reshape(-1, 4)
    name = prefix + "crv"
    return [
        (name + ".points", "f32", flat),
        (name + ".counts", "u32", np.asarray(counts, dtype=np.uint32)),
        (name + ".types", "u8", np.asarray(types, dtype=np.uint8)),
        (name + ".orders", "u8", np.asarray(orders, dtype=np.uint8)),
        (name + ".knots_mode", "u8", np.asarray(knots_mode, dtype=np.uint8)),
        (name + ".cyclic", "u8", np.asarray(cyclic, dtype=np.uint8)),
    ]


def _bezier_net(spline):
    """The NURBS control net of a Bezier spline, or None when it has no span."""
    bp = list(spline.bezier_points)
    n = len(bp)
    if n < 2:
        return None

    def p(v):
        return (float(v[0]), float(v[1]), float(v[2]), 1.0)

    net = [p(bp[0].co)]
    for i in range(n - 1):
        net.append(p(bp[i].handle_right))
        net.append(p(bp[i + 1].handle_left))
        net.append(p(bp[i + 1].co))
    if spline.use_cyclic_u:
        net.append(p(bp[n - 1].handle_right))
        net.append(p(bp[0].handle_left))
        net.append(p(bp[0].co))
    return net


def points_to_sections(cloud, prefix="", notes=None):
    """Read a point cloud into ``pts.*`` sections. ``prefix`` namespaces a
    definition's sections (§5.2). ``notes`` collects conversion losses."""
    n = len(cloud.points)
    if n == 0:
        return []
    name = prefix + "pts"
    positions = np.empty(n * 3, dtype=np.float32)
    cloud.points.foreach_get("co", positions)
    sections = [(name + ".positions", "f32", positions.reshape(-1, 3))]

    radii = np.empty(n, dtype=np.float32)
    cloud.points.foreach_get("radius", radii)
    if radii.any():
        sections.append((name + ".radii", "f32", radii))

    colors = read_point_colors(cloud)
    if colors is not None:
        sections.append((name + ".colors", "u8", colors))
    return sections


def read_point_colors(cloud):
    """The active point-domain colour attribute as wire u8 N×4 sRGB, or None.

    The same colour-space mapping as ``read_colors``: BYTE_COLOR is direct,
    FLOAT_COLOR is linear and converted to sRGB before quantisation.
    """
    attr = _active_color(cloud)
    if attr is None or attr.domain != "POINT":
        return None
    return _color_bytes(attr, len(cloud.points))


# ------------------------------------------------- evaluated geometry (prefixes)


# Blender's CurvesType enum, as stored in the curve_type attribute.
CURVE_TYPE_CATMULL_ROM = 0
CURVE_TYPE_POLY = 1
CURVE_TYPE_BEZIER = 2
CURVE_TYPE_NURBS = 3


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


# The recursion guard for instance nesting. Blender cannot evaluate a cyclic
# instance graph, but an eagerly built GeometrySet could still arrive deep or
# malformed; a cap keeps a broken scene from hanging the engine.
MAX_INSTANCE_DEPTH = 32

_IDENTITY = (
    1.0, 0.0, 0.0, 0.0,
    0.0, 1.0, 0.0, 0.0,
    0.0, 0.0, 1.0, 0.0,
    0.0, 0.0, 0.0, 1.0,
)


def _serialize_geometry(geometry, sections, attributes, prefix="", as_subd=False,
                        definition=None, part=None, notes=None):
    """Append one GeometrySet's geometry (mesh, curves, point cloud) to
    ``sections`` / ``attributes``, under an optional definition prefix.

    ``part`` is the part index of a multi-part definition (§5.2); it is ``None``
    for the frame's own geometry and for a single-part definition."""
    mesh = geometry.mesh
    if mesh is not None and len(mesh.vertices):
        if not as_subd and len(mesh.polygons) == 0:
            # A wire mesh has no faces to carry; send its edges as curves and
            # its isolated vertices as points instead of an empty mesh.
            wire_sections, wire_attrs = wire_mesh_to_sections(
                mesh, index0=len(attributes), prefix=prefix, definition=definition,
                part=part, notes=notes,
            )
            sections += wire_sections
            attributes += wire_attrs
        else:
            sections += mesh_to_sections(
                mesh, as_subd=as_subd, prefix=prefix, notes=notes
            )
            attribute_sections, mesh_attributes_ = mesh_attributes(
                mesh, as_subd=as_subd, index0=len(attributes),
                prefix=prefix, definition=definition, part=part,
            )
            sections += attribute_sections
            attributes += mesh_attributes_
            if not as_subd:
                # Loose edges and vertices beside a face mesh travel as their
                # own curves/points so they are not silently dropped.
                wire_sections, wire_attrs = wire_mesh_to_sections(
                    mesh, index0=len(attributes), prefix=prefix, definition=definition,
                    part=part, notes=notes,
                )
                sections += wire_sections
                attributes += wire_attrs
    curves = geometry.curves
    if curves is not None and len(curves.points):
        curve_sections, curve_attrs = curves_component_to_sections(
            curves, index0=len(attributes), prefix=prefix, definition=definition,
            part=part, notes=notes,
        )
        sections += curve_sections
        attributes += curve_attrs
    cloud = geometry.pointcloud
    if cloud is not None and len(cloud.points):
        sections += points_to_sections(cloud, prefix=prefix, notes=notes)
        attribute_sections, point_attributes_ = points_attributes(
            cloud, index0=len(attributes), prefix=prefix, definition=definition,
            part=part, notes=notes,
        )
        sections += attribute_sections
        attributes += point_attributes_


def _has_components(geometry):
    """Whether a GeometrySet carries geometry of its own (as opposed to only
    instance references)."""
    mesh = geometry.mesh
    if mesh is not None and len(mesh.vertices):
        return True
    curves = geometry.curves
    if curves is not None and len(curves.points):
        return True
    cloud = geometry.pointcloud
    if cloud is not None and len(cloud.points):
        return True
    return False


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
        message = (
            "instance nesting deeper than %d was skipped" % MAX_INSTANCE_DEPTH
        )
        prefs.log("[stageloom] " + message, flush=True)
        _note(notes, message)
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
    if part is None:
        return "idef.%d." % index
    return "idef.%d.%d." % (index, part)


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
        message = "instance nesting deeper than %d was skipped" % MAX_INSTANCE_DEPTH
        prefs.log("[stageloom] " + message, flush=True)
        _note(notes, message)
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

    if parts <= 1:
        index = len(definitions)
        definitions.append({"name": name})
        ref_def = []
        transforms = []
        for unit in units:
            _serialize_geometry(
                unit, sections, attributes, prefix=_definition_prefix(index),
                definition=index, notes=notes,
            )
            for child_reference, matrix in (_instance_rows(unit, notes=notes) or []):
                resolve(child_reference, matrix)
        for child_reference, matrix in nested:
            resolve(child_reference, matrix)
        _append_definition_rows(
            sections, _definition_prefix(index), ref_def, transforms
        )
        return [index]

    indices = []
    if not split:
        index = len(definitions)
        definitions.append({"name": name, "parts": parts})
        indices.append(index)
        ref_def = []
        transforms = []
        for part, unit in enumerate(units):
            _serialize_geometry(
                unit, sections, attributes, prefix=_definition_prefix(index, part),
                definition=index, part=part, notes=notes,
            )
            for child_reference, matrix in (_instance_rows(unit, notes=notes) or []):
                resolve(child_reference, matrix)
        for child_reference, matrix in nested:
            resolve(child_reference, matrix)
        _append_definition_rows(
            sections, _definition_prefix(index), ref_def, transforms
        )
        return indices

    # No `blocks/2` on the peer: one single-part definition per part. An old
    # peer ignores `parts` and the part namespaces, so emitting them anyway
    # would collapse the definition silently (§8).
    for part, unit in enumerate(units):
        index = len(definitions)
        definitions.append({"name": _split_part_name(name, part, parts)})
        indices.append(index)
        ref_def = []
        transforms = []
        _serialize_geometry(
            unit, sections, attributes, prefix=_definition_prefix(index),
            definition=index, notes=notes,
        )
        for child_reference, matrix in (_instance_rows(unit, notes=notes) or []):
            resolve(child_reference, matrix)
        if part == 0:
            for child_reference, matrix in nested:
                resolve(child_reference, matrix)
        _append_definition_rows(
            sections, _definition_prefix(index), ref_def, transforms
        )
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
        message = "instance nesting deeper than %d was skipped" % MAX_INSTANCE_DEPTH
        prefs.log("[stageloom] " + message, flush=True)
        _note(notes, message)
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


def curves_component_to_sections(curves, index0=0, prefix="", definition=None,
                                 part=None, notes=None):
    """Read a GeometrySet's curve component into ``crv.*`` sections and attributes.

    The component is the new Curves type: per-curve attributes carry the kind
    (``curve_type``), the NURBS order and knots mode, and the cyclic flag;
    per-point attributes carry the position, the NURBS weight, and the Bezier
    handles. Those internal channels stay in ``crv.*``; user attributes are
    reindexed onto the curves that actually reach the wire, with per-curve
    values in domain ``CURVE`` and per-point values in domain ``POINT``. This is
    the same attribute logic the loose-edge exporter uses.

    ``prefix`` namespaces a definition's sections (§5.2). Returns
    ``(sections, attributes)``.

    Catmull-Rom components have no wire representation (our protocol has poly,
    Bezier, and NURBS); they are skipped and ``notes`` names the count. A Bezier
    curve's wire points are its control net, not its original points, so
    per-point attributes are skipped for the whole component when a Bezier curve
    is present; a 1:1 point mapping does not exist.
    """
    attrs = curves.attributes
    positions = attrs["position"].data
    weights = attrs.get("nurbs_weight")
    cyclic_attr = attrs.get("cyclic")
    type_attr = attrs.get("curve_type")
    order_attr = attrs.get("nurbs_order")
    knots_attr = attrs.get("knots_mode")
    handle_left = attrs.get("handle_left")
    handle_right = attrs.get("handle_right")

    points = []
    counts = []
    types = []
    orders = []
    modes = []
    cyclic = []
    curve_indices = []
    point_indices = []
    has_bezier = False
    catmull = 0
    missing_handles = 0
    short_curves = 0
    unsupported_nurbs = 0
    unknown_types = 0

    for curve in curves.curves:
        first = curve.first_point_index
        length = curve.points_length
        index = curve.index
        if length < 2:
            short_curves += 1
            continue
        ctype = int(type_attr.data[index].value) if type_attr is not None else CURVE_TYPE_POLY
        is_cyclic = bool(cyclic_attr.data[index].value) if cyclic_attr is not None else False

        if ctype == CURVE_TYPE_CATMULL_ROM:
            catmull += 1
            prefs.log(
                "[stageloom] Catmull-Rom curve component skipped: no wire representation",
                flush=True,
            )
            continue

        if ctype == CURVE_TYPE_BEZIER:
            net = _component_bezier_net(positions, handle_left, handle_right, first, length, is_cyclic)
            if net is None:
                missing_handles += 1
                prefs.log(
                    "[stageloom] Bezier curve component skipped: no handles",
                    flush=True,
                )
                continue
            points.extend(net)
            counts.append(len(net))
            types.append(CURVE_NURBS)
            orders.append(4)
            modes.append(KNOTS_BEZIER)
            cyclic.append(0)
            curve_indices.append(index)
            has_bezier = True
            continue

        chunk = []
        for i in range(first, first + length):
            p = positions[i].vector
            w = float(weights.data[i].value) if weights is not None else 1.0
            chunk.append((p[0], p[1], p[2], w))

        if ctype == CURVE_TYPE_NURBS:
            order = int(order_attr.data[index].value) if order_attr is not None else 4
            mode = int(knots_attr.data[index].value) if knots_attr is not None else KNOTS_NORMAL
            weights_valid = all(
                np.isfinite(point[3]) and point[3] > 0.0 for point in chunk
            )
            if order < 2 or order > 6 or order > length or mode > 2 or not weights_valid:
                unsupported_nurbs += 1
                prefs.log(
                    "[stageloom] NURBS curve component skipped: order %d, mode %d, "
                    "or a non-positive weight is not representable" % (order, mode),
                    flush=True,
                )
                continue
            points.extend(chunk)
            counts.append(length)
            types.append(CURVE_NURBS)
            orders.append(order)
            modes.append(mode)
            cyclic.append(1 if is_cyclic else 0)
        elif ctype == CURVE_TYPE_POLY:
            points.extend(chunk)
            counts.append(length)
            types.append(CURVE_POLY)
            orders.append(0)
            modes.append(0)
            cyclic.append(1 if is_cyclic else 0)
        else:
            unknown_types += 1
            prefs.log(
                "[stageloom] curve component type %d skipped: not a wire curve type" % ctype,
                flush=True,
            )
            continue

        curve_indices.append(index)
        point_indices.extend(range(first, first + length))

    _count_note(notes, catmull,
                "Catmull-Rom curve component has no wire representation and was skipped",
                "Catmull-Rom curve components have no wire representation and were skipped")
    _count_note(notes, missing_handles,
                "Bezier curve component had no handles and was skipped",
                "Bezier curve components had no handles and were skipped")
    _count_note(notes, short_curves,
                "curve component had fewer than two points and was skipped",
                "curve components had fewer than two points and were skipped")
    _count_note(notes, unsupported_nurbs,
                "NURBS curve component had an unsupported order, mode or weight and was skipped",
                "NURBS curve components had an unsupported order, mode or weight and were skipped")
    _count_note(notes, unknown_types,
                "curve component had an unknown type and was skipped",
                "curve components had an unknown type and were skipped")

    if not counts:
        return [], []
    flat = np.asarray(points, dtype=np.float32).reshape(-1, 4)
    name = prefix + "crv"
    sections = [
        (name + ".points", "f32", flat),
        (name + ".counts", "u32", np.asarray(counts, dtype=np.uint32)),
        (name + ".types", "u8", np.asarray(types, dtype=np.uint8)),
        (name + ".orders", "u8", np.asarray(orders, dtype=np.uint8)),
        (name + ".knots_mode", "u8", np.asarray(modes, dtype=np.uint8)),
        (name + ".cyclic", "u8", np.asarray(cyclic, dtype=np.uint8)),
    ]
    attribute_sections, attributes = curves_component_attributes(
        curves, curve_indices, point_indices, has_bezier,
        index0=index0, prefix=prefix, definition=definition, part=part,
        notes=notes,
    )
    return sections + attribute_sections, attributes


def curves_component_attributes(curves, curve_indices, point_indices, has_bezier,
                                index0=0, prefix="", definition=None, part=None,
                                notes=None):
    """Reindex a Curves component's user attributes onto the emitted curves.

    Per-curve values become domain ``CURVE``; per-point values become domain
    ``POINT`` and are flattened in curve order. The internal channels already
    carried by ``crv.*`` are skipped.
    """
    sections = []
    descriptors = []
    if not curve_indices:
        return sections, descriptors

    for attr in curves.attributes:
        name = attr.name
        if (
            name.startswith(".")
            or name in attr_mod.SKIP_NAMES
            or name in CURVE_INTERNAL_ATTRIBUTES
        ):
            continue
        type_name = attr.data_type
        if type_name not in attr_mod.WIRE_TYPES:
            continue
        values = _attribute_values(attr)
        comps = attr_mod.components(type_name)

        if attr.domain == "CURVE":
            mapped = _take_attribute_values(values, curve_indices, comps)
            index = index0 + len(descriptors)
            attribute_sections, descriptor = _emit_attribute(
                "crv", name, "CURVE", type_name, mapped, index,
                prefix=prefix, definition=definition, part=part,
            )
            sections += attribute_sections
            descriptors.append(descriptor)
        elif attr.domain == "POINT":
            if has_bezier:
                prefs.log(
                    "[stageloom] curve point attribute %r skipped: a Bezier control "
                    "net has no 1:1 point mapping" % name,
                    flush=True,
                )
                _note(
                    notes,
                    "curve point attribute %r was skipped: a Bezier control net "
                    "has no 1:1 point mapping" % name,
                )
                continue
            mapped = _take_attribute_values(values, point_indices, comps)
            index = index0 + len(descriptors)
            attribute_sections, descriptor = _emit_attribute(
                "crv", name, "POINT", type_name, mapped, index,
                prefix=prefix, definition=definition, part=part,
            )
            sections += attribute_sections
            descriptors.append(descriptor)

    return sections, descriptors


def _component_bezier_net(positions, handle_left, handle_right, first, length, is_cyclic):
    """The degree-3 Bezier net of a component's Bezier curve, or None."""
    if handle_left is None or handle_right is None:
        return None

    def pos(i):
        v = positions[i].vector
        return (v[0], v[1], v[2], 1.0)

    def left(i):
        v = handle_left.data[i].vector
        return (v[0], v[1], v[2], 1.0)

    def right(i):
        v = handle_right.data[i].vector
        return (v[0], v[1], v[2], 1.0)

    net = [pos(first)]
    for i in range(length - 1):
        net.append(right(first + i))
        net.append(left(first + i + 1))
        net.append(pos(first + i + 1))
    if is_cyclic:
        net.append(right(first + length - 1))
        net.append(left(first))
        net.append(pos(first))
    return net


# -------------------------------------------------------------------- write


def select_mesh_group(by_name):
    """One complete ``mesh.*`` or ``subd.*`` group, already chosen as a unit.

    This is the Blender twin of the C# Q7b1 selector: both complete groups mean
    the plain mesh wins; a partial group is an error even when the other group
    is complete. The two groups describe different topology sets, so vertices
    and faces are never paired across them.
    """
    mesh_names = ("mesh.verts", "mesh.face_counts", "mesh.face_indices")
    subd_names = ("subd.verts", "subd.face_counts", "subd.face_indices")
    mesh_any = any(
        name in by_name
        for name in mesh_names + ("mesh.colors",)
    )
    mesh_complete = all(name in by_name for name in mesh_names)
    subd_any = any(name in by_name for name in subd_names)
    subd_complete = all(name in by_name for name in subd_names)

    if mesh_any and not mesh_complete:
        missing = ", ".join(name for name in mesh_names if name not in by_name)
        raise GeometryIOError("mesh group is incomplete: %s missing" % missing)
    if subd_any and not subd_complete:
        missing = ", ".join(name for name in subd_names if name not in by_name)
        raise GeometryIOError("subd group is incomplete: %s missing" % missing)

    if mesh_complete:
        return "mesh"
    if subd_complete:
        return "subd"
    return None


def sections_to_mesh(mesh, sections, notes=None, topology=None, previous=None):
    """Update ``mesh`` from sections, in place.

    Exactly one complete group is selected first (``select_mesh_group``). Its
    positions are written with ``foreach_set`` only when the topology matches;
    otherwise the mesh is rebuilt with ``clear_geometry`` + ``from_pydata``.

    ``topology`` is the producer's additive mesh-topology token (protocol §4.4)
    and ``previous`` is the token stored with the helper's displayed geometry.
    When the two match and the cheap element counts agree, positions, colours,
    creases and attributes are written without reading ``mesh.polygons`` or
    ``mesh.loops`` at all. A present-but-different token rebuilds directly (the
    token already proves the topology differs), and an absent token keeps the
    verified comparison for old peers. Extra sections (curves, points) are
    ignored here; they belong to other components.
    """
    by_name = {}
    for name, dtype, values in sections:
        by_name[name] = values

    group = select_mesh_group(by_name)
    if group is None:
        raise GeometryIOError("sections need verts, face_counts and face_indices")
    prefix = group + "."

    try:
        verts = np.ascontiguousarray(by_name[prefix + "verts"], dtype=np.float32).reshape(-1, 3)
        counts = np.ascontiguousarray(by_name[prefix + "face_counts"], dtype=np.uint32).reshape(-1)
        indices = np.ascontiguousarray(by_name[prefix + "face_indices"], dtype=np.uint32).reshape(-1)
    except (TypeError, ValueError):
        raise GeometryIOError(
            "%sverts must be N x 3 floats and face arrays flat integers" % prefix
        )
    total = int(counts.sum())
    if len(indices) != total:
        raise GeometryIOError(
            "face_indices length %d != sum(face_counts) %d" % (len(indices), total)
        )
    if len(counts) and int(counts.min()) < 3:
        raise GeometryIOError("face_counts must be at least 3 vertices per face")
    if len(indices) and int(indices.max()) >= len(verts):
        raise GeometryIOError(
            "face_indices refer to vertex %d, but only %d vertices were sent"
            % (int(indices.max()), len(verts))
        )

    lengths_match = (
        len(mesh.vertices) == len(verts)
        and len(mesh.polygons) == len(counts)
        and len(mesh.loops) == len(indices)
    )
    if topology is not None:
        # A token is a producer claim about the source topology. With the same
        # token and the same cheap counts, the displayed topology is the one
        # this frame describes; a different token proves a rebuild is needed.
        # Either way the polygon/loop arrays are not read.
        same_topology = previous == topology and lengths_match
    else:
        # No token (old peer or a producer that could not name the group):
        # verify the arrays, exactly as before.
        same_topology = lengths_match and _topology_matches(mesh, counts, indices)

    if same_topology:
        mesh.vertices.foreach_set("co", verts.reshape(-1))
    else:
        mesh.clear_geometry()
        faces = _faces_from(counts, indices)
        mesh.from_pydata(verts.tolist(), [], faces)

    colors = by_name.get("mesh.colors") if group == "mesh" else None
    write_colors(
        mesh,
        None if colors is None else np.asarray(colors, dtype=np.uint8).reshape(-1, 4),
    )

    creases = None
    edge_name = prefix + "crease_edges"
    value_name = prefix + "crease_values"
    if edge_name in by_name or value_name in by_name:
        if edge_name not in by_name or value_name not in by_name:
            raise GeometryIOError(
                "%s and %s must be sent together" % (edge_name, value_name)
            )
        creases = (by_name[edge_name], by_name[value_name])
    write_creases(mesh, creases, notes=notes)

    mesh.update()
    return mesh


def _topology_matches(mesh, counts, indices):
    """The verified-path comparison: read the mesh's loops and compare them.

    The token path never calls this; it is the only place ``sections_to_mesh``
    reads ``mesh.polygons``/``mesh.loops``. Kept as its own function so a test
    can guard the fast path without patching Blender's RNA collections.
    """
    current_counts = np.empty(len(mesh.polygons), dtype=np.uint32)
    mesh.polygons.foreach_get("loop_total", current_counts)
    current_indices = np.empty(len(mesh.loops), dtype=np.uint32)
    mesh.loops.foreach_get("vertex_index", current_indices)
    return bool(
        np.array_equal(current_counts, counts)
        and np.array_equal(current_indices, indices)
    )


def _faces_from(counts, indices):
    faces = []
    at = 0
    for count in counts.tolist():
        faces.append(tuple(int(i) for i in indices[at : at + count]))
        at += count
    return faces


def write_colors(mesh, colors):
    """Create or update the byte-colour attribute from u8 N×4 data.

    ``None`` removes the stage's native colour channel: an absent
    ``mesh.colors`` section means "no colour", not "keep the previous one".
    """
    if colors is None:
        attr = mesh.color_attributes.get(COLOR_ATTRIBUTE)
        if attr is not None and attr.name == COLOR_ATTRIBUTE:
            mesh.color_attributes.remove(attr)
        return
    if colors.ndim != 2 or colors.shape[1] != 4:
        raise GeometryIOError("mesh.colors must be N x 4")
    attr = mesh.color_attributes.get(COLOR_ATTRIBUTE)
    if attr is None or attr.domain != "POINT" or attr.data_type != "BYTE_COLOR":
        # Keep one attribute of our own, so a user's attribute is never rewritten.
        if attr is not None and attr.name == COLOR_ATTRIBUTE:
            mesh.color_attributes.remove(attr)
        attr = mesh.color_attributes.new(COLOR_ATTRIBUTE, "BYTE_COLOR", "POINT")
    if len(attr.data) != len(colors):
        raise GeometryIOError(
            "colour attribute has %d entries, sections have %d" % (len(attr.data), len(colors))
        )
    # color_srgb takes floats 0..1 in both directions; a u8 write truncates.
    attr.data.foreach_set("color_srgb", colors.reshape(-1).astype(np.float32) / 255.0)
    mesh.color_attributes.active_color = attr


def write_creases(mesh, creases, notes=None):
    """Create or update the crease attribute. ``creases`` is (edges, values) or None,
    in which case a zeroed attribute is still created so the mesh is explicitly
    uncreased rather than silently stale.

    Edges named by the frame that no longer exist on the rebuilt mesh are
    dropped; ``notes`` receives their count instead of hiding the loss in the
    console.
    """
    attr = mesh.attributes.get(CREASE_ATTRIBUTE)
    if attr is None or attr.domain != "EDGE" or attr.data_type != "FLOAT":
        if attr is not None and attr.name == CREASE_ATTRIBUTE:
            mesh.attributes.remove(attr)
        attr = mesh.attributes.new(CREASE_ATTRIBUTE, "FLOAT", "EDGE")
    values = np.zeros(len(mesh.edges), dtype=np.float32)
    if creases is not None:
        edges = np.asarray(creases[0], dtype=np.uint32).reshape(-1, 2)
        cv = np.asarray(creases[1], dtype=np.float32).reshape(-1)
        if len(edges) != len(cv):
            raise GeometryIOError("crease_edges and crease_values differ in length")
        # Map the section's vertex pairs to edge indices.
        lookup = {}
        edge_verts = np.empty(len(mesh.edges) * 2, dtype=np.uint32)
        mesh.edges.foreach_get("vertices", edge_verts)
        edge_verts = edge_verts.reshape(-1, 2)
        for i in range(len(edge_verts)):
            a, b = int(edge_verts[i][0]), int(edge_verts[i][1])
            lookup[(min(a, b), max(a, b))] = i
        missing = 0
        for k in range(len(edges)):
            a, b = int(edges[k][0]), int(edges[k][1])
            idx = lookup.get((min(a, b), max(a, b)))
            if idx is not None:
                values[idx] = cv[k]
            else:
                missing += 1
        _count_note(notes, missing,
                    "crease edge did not match the rebuilt mesh and was dropped",
                    "crease edges did not match the rebuilt mesh and were dropped")
    attr.data.foreach_set("value", values)


# ----------------------------------------------------------- curve and point write


def sections_to_curve(curve, sections):
    """Rebuild a Blender Curve datablock from ``crv.*`` sections, in place.

    The whole group is validated before ``curve`` is touched: section lengths,
    the points/counts sum, per-curve type, order, knot mode, cyclic flag and
    weight. A malformed group raises ``GeometryIOError``; it is never truncated
    or completed with an invented zero weight.
    """
    by_name = {}
    for name, dtype, values in sections:
        by_name[name] = values

    names = (
        "crv.points", "crv.counts", "crv.types", "crv.orders",
        "crv.knots_mode", "crv.cyclic",
    )
    if not any(name in by_name for name in names):
        raise GeometryIOError("curve sections need crv.points and crv.counts")
    if "crv.points" not in by_name or "crv.counts" not in by_name:
        raise GeometryIOError("curve group is incomplete: crv.points/crv.counts missing")

    try:
        points = np.asarray(by_name["crv.points"], dtype=np.float32)
        if points.size % 4:
            raise ValueError
        points = points.reshape(-1, 4)
        counts = np.asarray(by_name["crv.counts"], dtype=np.uint32).reshape(-1)
    except (TypeError, ValueError):
        raise GeometryIOError("crv.points must be N x 4 floats and crv.counts a u32 list")

    n_curves = len(counts)
    arrays = {}
    for name in names[2:]:
        if name not in by_name:
            if n_curves == 0:
                arrays[name] = np.zeros(0, dtype=np.uint8)
                continue
            raise GeometryIOError("%s is missing for %d curves" % (name, n_curves))
        try:
            values = np.asarray(by_name[name]).reshape(-1)
            if len(values) != n_curves:
                raise GeometryIOError(
                    "%s has %d entries for %d curves" % (name, len(values), n_curves)
                )
            if len(values) and (values.min() < 0 or values.max() > 255):
                raise GeometryIOError("%s has a value outside u8 range" % name)
            arrays[name] = values.astype(np.uint8)
        except GeometryIOError:
            raise
        except (TypeError, ValueError) as exc:
            raise GeometryIOError("%s is not a u8 list: %s" % (name, exc))

    types = arrays["crv.types"]
    orders = arrays["crv.orders"]
    modes = arrays["crv.knots_mode"]
    cyclic = arrays["crv.cyclic"]

    total = int(counts.sum())
    if total != len(points):
        raise GeometryIOError(
            "crv.points length %d != sum(crv.counts) %d" % (len(points), total)
        )

    at = 0
    for i in range(n_curves):
        raw_count = int(counts[i])
        if raw_count < 2:
            raise GeometryIOError(
                "curve %d has %d points; at least 2 are required" % (i, raw_count)
            )
        chunk = points[at : at + raw_count]
        at += raw_count
        type_value = int(types[i])
        mode = int(modes[i])
        order = int(orders[i])
        if type_value > CURVE_NURBS:
            raise GeometryIOError("curve %d has type %d" % (i, type_value))
        if mode > KNOTS_BEZIER:
            raise GeometryIOError("curve %d has knot mode %d" % (i, mode))
        if cyclic[i] > 1:
            raise GeometryIOError("curve %d cyclic flag is %d" % (i, cyclic[i]))

        bezier = type_value == CURVE_BEZIER or (
            type_value == CURVE_NURBS and mode == KNOTS_BEZIER
        )
        if bezier:
            if raw_count < 4 or (raw_count - 1) % 3:
                raise GeometryIOError(
                    "curve %d is a Bezier net with %d points; expected 3k+1"
                    % (i, raw_count)
                )
        elif type_value == CURVE_NURBS:
            if order < 2 or order > 6:
                raise GeometryIOError(
                    "curve %d NURBS order is %d; expected 2..6" % (i, order)
                )
            if order > raw_count:
                raise GeometryIOError(
                    "curve %d NURBS order %d exceeds its %d control points"
                    % (i, order, raw_count)
                )

        weights = chunk[:, 3]
        if not np.all(np.isfinite(weights)) or not np.all(weights > 0.0):
            bad = int(np.nonzero(~np.isfinite(weights) | (weights <= 0.0))[0][0])
            raise GeometryIOError(
                "curve %d point %d has weight %r; weights must be finite and positive"
                % (i, bad, float(weights[bad]))
            )

    curve.splines.clear()
    at = 0
    for i in range(n_curves):
        n = int(counts[i])
        chunk = points[at : at + n]
        at += n
        if types[i] == CURVE_POLY:
            _poly_spline(curve, chunk, bool(cyclic[i]))
        elif modes[i] == KNOTS_BEZIER or types[i] == CURVE_BEZIER:
            _bezier_spline(curve, chunk)
        else:
            _nurbs_spline(curve, chunk, int(orders[i]), int(modes[i]), bool(cyclic[i]))
    curve.update_tag()
    return curve


def _poly_spline(curve, chunk, cyclic):
    spline = curve.splines.new("POLY")
    spline.points.add(len(chunk) - 1)
    spline.points.foreach_set("co", chunk.reshape(-1))
    spline.use_cyclic_u = cyclic


def _nurbs_spline(curve, chunk, order, mode, cyclic):
    """One NURBS spline. ``order`` is validated by ``sections_to_curve``; it is
    deliberately not clamped here, so a bad frame cannot silently change shape."""
    spline = curve.splines.new("NURBS")
    spline.points.add(len(chunk) - 1)
    spline.order_u = int(order)
    spline.use_endpoint_u = mode == KNOTS_ENDPOINT
    spline.use_cyclic_u = cyclic
    spline.points.foreach_set("co", chunk.reshape(-1))


def _bezier_spline(curve, chunk):
    """A Bezier net is ``co0, hr0, hl1, co1, ...``: 3k+1 points for k spans.

    Closed nets (from a cyclic Blender spline or a closed Rhino curve) end where
    they start; the open Bezier spline with coincident ends is the same shape.
    """
    n = len(chunk)
    if n < 4 or (n - 1) % 3 != 0:
        # Not a Bezier net. A NURBS spline keeps the control polygon shape, at
        # the cost of exactness; the GH side is where approximations are warned.
        _nurbs_spline(curve, chunk, 4, KNOTS_ENDPOINT, False)
        return
    k = (n - 1) // 3
    spline = curve.splines.new("BEZIER")
    spline.bezier_points.add(k)
    for i in range(k + 1):
        bp = spline.bezier_points[i]
        co = chunk[3 * i]
        bp.co = (float(co[0]), float(co[1]), float(co[2]))
        bp.handle_left_type = "FREE"
        bp.handle_right_type = "FREE"
        if i > 0:
            hl = chunk[3 * i - 1]
            bp.handle_left = (float(hl[0]), float(hl[1]), float(hl[2]))
        if i < k:
            hr = chunk[3 * i + 1]
            bp.handle_right = (float(hr[0]), float(hr[1]), float(hr[2]))
    spline.use_cyclic_u = False


def sections_to_points(cloud, sections):
    """Rebuild a point cloud from ``pts.*`` sections, in place.

    Absent ``pts.radii`` means "no per-point radii": every radius is reset to
    Blender's default rather than keeping a previous response's values.
    """
    by_name = {}
    for name, dtype, values in sections:
        by_name[name] = values

    positions = by_name.get("pts.positions")
    if positions is None:
        if any(name.startswith("pts.") for name in by_name):
            raise GeometryIOError("point group is incomplete: pts.positions is missing")
        cloud.resize(0)
        return cloud
    try:
        positions = np.asarray(positions, dtype=np.float32).reshape(-1, 3)
    except (TypeError, ValueError):
        raise GeometryIOError("pts.positions must be N x 3 floats")
    n = len(positions)
    cloud.resize(n)
    cloud.points.foreach_set("co", positions.reshape(-1))

    radii = by_name.get("pts.radii")
    if radii is None:
        cloud.points.foreach_set(
            "radius", np.full(n, POINT_RADIUS_DEFAULT, dtype=np.float32)
        )
    else:
        radii = np.asarray(radii, dtype=np.float32).reshape(-1)
        if len(radii) != n:
            raise GeometryIOError(
                "pts.radii has %d entries for %d points" % (len(radii), n)
            )
        cloud.points.foreach_set("radius", radii)
    cloud.points.update()

    colors = by_name.get("pts.colors")
    write_point_colors(
        cloud,
        None if colors is None else np.asarray(colors, dtype=np.uint8).reshape(-1, 4),
    )
    return cloud


def write_point_colors(cloud, colors):
    """Create or update the byte-colour attribute of a point cloud.

    ``None`` removes the native colour channel when the frame no longer
    carries ``pts.colors``.
    """
    if colors is None:
        attr = cloud.color_attributes.get(COLOR_ATTRIBUTE)
        if attr is not None and attr.name == COLOR_ATTRIBUTE:
            cloud.color_attributes.remove(attr)
        return
    if colors.ndim != 2 or colors.shape[1] != 4:
        raise GeometryIOError("pts.colors must be N x 4")
    attr = cloud.color_attributes.get(COLOR_ATTRIBUTE)
    if attr is None or attr.domain != "POINT" or attr.data_type != "BYTE_COLOR":
        if attr is not None and attr.name == COLOR_ATTRIBUTE:
            cloud.color_attributes.remove(attr)
        attr = cloud.color_attributes.new(COLOR_ATTRIBUTE, "BYTE_COLOR", "POINT")
    if len(attr.data) != len(colors):
        raise GeometryIOError(
            "colour attribute has %d entries, sections have %d" % (len(attr.data), len(colors))
        )
    attr.data.foreach_set("color_srgb", colors.reshape(-1).astype(np.float32) / 255.0)
    cloud.color_attributes.active_color = attr


# ------------------------------------------------------------------ helpers


def object_to_sections(obj, as_subd=False, notes=None):
    """Protocol sections and attribute descriptors for an object's base
    geometry, by object type. Returns ``(sections, attributes)``.

    Meshes travel as ``mesh.*`` (or ``subd.*`` when the stack is flagged as a
    SubD cage), curves as ``crv.*``, point clouds as ``pts.*``. A Blender
    surface is meshed first: Blender has no B-rep to send, so the surface
    arrives on the GH side as a mesh (lossy, like a Rhino Brep going the other
    way); ``notes`` names that conversion. ``notes`` also collects curve and
    attribute losses from the readers.
    """
    if obj.type == "MESH":
        mesh = obj.data
        if not as_subd and len(mesh.polygons) == 0:
            return wire_mesh_to_sections(mesh, notes=notes)
        attribute_sections, attributes = mesh_attributes(mesh, as_subd=as_subd)
        sections = mesh_to_sections(mesh, as_subd=as_subd, notes=notes) + attribute_sections
        if not as_subd:
            wire_sections, wire_attrs = wire_mesh_to_sections(
                mesh, index0=len(attributes), notes=notes,
            )
            sections += wire_sections
            attributes += wire_attrs
        return sections, attributes
    if obj.type == "CURVE":
        return curve_to_sections(obj.data, notes=notes), []
    if obj.type == "POINTCLOUD":
        attribute_sections, attributes = points_attributes(obj.data)
        return points_to_sections(obj.data, notes=notes) + attribute_sections, attributes
    if obj.type == "SURFACE":
        import bpy

        _note(notes, "surface was meshed; Blender has no B-rep to send exactly")
        mesh = surface_to_mesh(obj)
        try:
            attribute_sections, attributes = mesh_attributes(mesh, as_subd=False)
            return mesh_to_sections(mesh, as_subd=False) + attribute_sections, attributes
        finally:
            bpy.data.meshes.remove(mesh)
    _note(notes, "%s object has no wire geometry and was skipped" % obj.type)
    return [], []


def surface_to_mesh(obj):
    """A new mesh datablock for a surface object's evaluated geometry. The
    caller owns it and removes it after use."""
    import bpy

    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(depsgraph)
    return bpy.data.meshes.new_from_object(evaluated)



