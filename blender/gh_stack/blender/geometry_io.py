"""Geometry <-> protocol sections, on the Blender side (plan §6, §11).

Reads an evaluated mesh into protocol v2 sections and writes sections back into a
mesh in place where the topology allows, rebuilding when it does not. Numpy through
``foreach_get``/``foreach_set``; ``from_pydata`` only on the rebuild path, which is
the slow path and the one scripted changes take.

Section names follow protocol §5: ``mesh.*`` for plain meshes, ``subd.*`` for a
mesh flagged as a SubD control cage, ``crv.*`` for curves, ``pts.*`` for point
clouds. The geometry hash order is the same order the sections are returned in.

Curves: poly, Bezier, and NURBS splines map to the protocol's three curve types.
A Blender Bezier spline travels as a degree-3 NURBS in Bezier knot mode (exact),
and comes back as Bezier splines. NURBS splines keep order, weights, and the
endpoint/uniform flag. Gaps in what Blender can express (arbitrary interior
knots) are the GH side's job to approximate and warn about (D3).

This module is the bpy layer: core stays pure (see blender/gh_stack/core).
"""

import numpy as np

from ..core import attributes as attr_mod

# Section orders per protocol §5. mesh.colors exists only for plain meshes;
# subd.colors does not exist in v2. Attribute sections (``attr.<i>.values``,
# ``attr.<i>.offsets``) are described by the frame header, not by a fixed order.
MESH_SECTION_ORDER = (
    "mesh.verts",
    "mesh.face_counts",
    "mesh.face_indices",
    "mesh.colors",
    "mesh.crease_edges",
    "mesh.crease_values",
)

SUBD_SECTION_ORDER = (
    "subd.verts",
    "subd.face_counts",
    "subd.face_indices",
    "subd.crease_edges",
    "subd.crease_values",
)

CURVE_SECTION_ORDER = (
    "crv.points",
    "crv.counts",
    "crv.types",
    "crv.orders",
    "crv.knots_mode",
    "crv.cyclic",
)

POINT_SECTION_ORDER = (
    "pts.positions",
    "pts.radii",
    "pts.colors",
)

CREASE_ATTRIBUTE = "crease_edge"
COLOR_ATTRIBUTE = "Col"

CURVE_POLY = 0
CURVE_BEZIER = 1
CURVE_NURBS = 2

KNOTS_NORMAL = 0
KNOTS_ENDPOINT = 1
KNOTS_BEZIER = 2


class GeometryIOError(Exception):
    pass


# --------------------------------------------------------------------- read


def mesh_to_sections(mesh, as_subd=False):
    """Read ``mesh`` into a list of ``(name, dtype, numpy array)`` sections.

    ``as_subd`` selects the ``subd.*`` names and omits colours (protocol §5).
    """
    prefix = "subd" if as_subd else "mesh"
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
        (prefix + ".verts", "f32", verts.reshape(-1, 3)),
        (prefix + ".face_counts", "u32", counts),
        (prefix + ".face_indices", "u32", indices),
    ]

    if not as_subd:
        colors = read_colors(mesh)
        if colors is not None:
            sections.append(("mesh.colors", "u8", colors))

    creases = read_creases(mesh)
    if creases is not None:
        edges, values = creases
        sections.append((prefix + ".crease_edges", "u32", edges.reshape(-1, 2)))
        sections.append((prefix + ".crease_values", "f32", values))

    return sections


def read_colors(mesh):
    """The active colour attribute as u8 N×4, or None.

    BYTE_COLOR stores bytes; ``color_srgb`` reads them back as floats in 0..1, so
    they are scaled and rounded to bytes. FLOAT_COLOR stores linear floats, so
    ``color`` is quantised directly. (Reading into a u8 buffer truncates the float
    representation instead of converting, which is why both paths read floats.)

    A CORNER-domain attribute is folded to one entry per vertex; the last loop that
    references a vertex wins, which is deterministic for Blender's loop order.
    """
    attr = _active_color(mesh)
    if attr is None:
        return None
    n = len(mesh.vertices) if attr.domain == "POINT" else len(mesh.loops)

    floats = np.empty(n * 4, dtype=np.float32)
    if attr.data_type == "BYTE_COLOR":
        attr.data.foreach_get("color_srgb", floats)
    else:
        attr.data.foreach_get("color", floats)
    out = np.clip(np.rint(floats.reshape(n, 4) * 255.0), 0.0, 255.0).astype(np.uint8)

    if attr.domain == "POINT":
        return out

    vertex_of_loop = np.empty(len(mesh.loops), dtype=np.uint32)
    mesh.loops.foreach_get("vertex_index", vertex_of_loop)
    folded = np.zeros((len(mesh.vertices), 4), dtype=np.uint8)
    folded[vertex_of_loop] = out
    return folded


def read_creases(mesh):
    """Edges with a non-zero crease as ``(edges N×2 u32, values N f32)``, or None.

    Fractional values are passed through; the GH side rounds them to the nearest
    tag and warns (D2). Edges at exactly zero are omitted: absent means smooth.
    """
    attr = mesh.attributes.get(CREASE_ATTRIBUTE)
    if attr is None:
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


def read_attributes(owner, group, index0=0, skip=()):
    """Every transportable attribute of one geometry data owner, as
    ``(sections, descriptors)``.

    ``owner`` is anything with Blender's attribute API (Mesh, PointCloud,
    Curves component). Internal attributes (leading dot) and the geometry
    itself (``position``) are skipped, as is anything in ``skip`` — the names
    the caller already carries through a native channel (``mesh.colors``,
    ``crease_edge``), so nothing travels twice. ``index0`` is the running
    attribute count of the frame: the sections are named from it.
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
        value_section, offsets_section = attr_mod.section_names(index)
        n = len(attr.data)
        comps = attr_mod.components(type_name)

        if type_name == "STRING":
            # Strings have no foreach path; element access is the only way and
            # the counts are small by nature.
            data = [bytes(element.value) for element in attr.data]
            raw = np.frombuffer(b"".join(data), dtype=np.uint8) if data else np.zeros(0, np.uint8)
            sections.append((value_section, "u8", raw))
            sections.append(
                (offsets_section, "u32", np.asarray([len(item) for item in data], dtype=np.uint32))
            )
        else:
            prop, buf_dtype = _FOREACH[type_name]
            buf = np.zeros(n * comps, dtype=buf_dtype)
            attr.data.foreach_get(prop, buf)
            if type_name == "BYTE_COLOR":
                values = np.clip(np.rint(buf * 255.0), 0.0, 255.0).astype(np.uint8)
            elif type_name == "BOOLEAN":
                values = buf.astype(np.uint8)
            elif type_name == "INT8":
                values = buf.astype(np.int32)
            else:
                values = np.ascontiguousarray(buf)
            sections.append((value_section, attr_mod.wire_dtype(type_name), values))

        descriptors.append(
            attr_mod.descriptor(group, name, attr.domain, type_name, n, index)
        )
    return sections, descriptors


def mesh_attributes(mesh, as_subd=False, index0=0):
    """The generic attributes of a mesh (or SubD cage). The active colour
    attribute is excluded only when the native ``mesh.colors`` channel carries
    it, so a SubD cage's colours travel as an ordinary attribute."""
    skip = [CREASE_ATTRIBUTE]
    if not as_subd:
        color = _active_color(mesh)
        if color is not None:
            skip.append(color.name)
    return read_attributes(mesh, "subd" if as_subd else "mesh", index0=index0, skip=skip)


def points_attributes(cloud, index0=0):
    """The generic attributes of a point cloud. The native ``pts.colors``
    channel carries the active colour attribute; everything else travels."""
    skip = []
    color = _active_color(cloud)
    if color is not None:
        skip.append(color.name)
    return read_attributes(cloud, "pts", index0=index0, skip=skip)


def write_attributes(owner, entries):
    """Create or update attributes from decoded wire sections.

    ``entries`` is ``(descriptor, values, offsets)`` per attribute, straight
    from a decoded frame. Values that do not fit the target domain are dropped
    with a note in the returned list — a wrong count is a topology mismatch, and
    writing it anyway would be silent corruption.
    """
    notes = []
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
            data, lengths = _decode_strings(values, offsets)
            if len(data) != target:
                notes.append(
                    "attribute %r: %d strings for %d %s elements"
                    % (name, len(data), target, domain.lower())
                )
                continue
            attr = _attribute_for(owner, name, domain, type_name)
            for i, raw in enumerate(data):
                attr.data[i].value = raw
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
    """The wire's concatenated UTF-8 bytes plus per-string lengths, as bytes."""
    raw = (
        np.asarray(values, dtype=np.uint8).tobytes()
        if not isinstance(values, (bytes, bytearray))
        else bytes(values)
    )
    lengths = () if offsets is None else [int(length) for length in offsets]
    data = []
    at = 0
    for length in lengths:
        data.append(raw[at : at + length])
        at += length
    return data, lengths


# ------------------------------------------------------------ curves and points


def curve_to_sections(curve):
    """Read a Blender Curve datablock into ``crv.*`` sections (protocol §5).

    A Bezier spline travels as a degree-3 NURBS in Bezier knot mode, using the
    control net co0, handle_right0, handle_left1, co1, ... — the representation
    the GH side rebuilds exactly. A cyclic Bezier spline wraps the closing
    segment, so its net ends where it starts instead of carrying the cyclic flag.
    """
    points = []
    counts = []
    types = []
    orders = []
    knots_mode = []
    cyclic = []

    for spline in curve.splines:
        if spline.type == "BEZIER":
            net = _bezier_net(spline)
            if net is None:
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
            if len(p) < 2:
                continue
            points.extend(p)
            counts.append(len(p))
            types.append(CURVE_NURBS)
            orders.append(int(spline.order_u))
            knots_mode.append(KNOTS_ENDPOINT if spline.use_endpoint_u else KNOTS_NORMAL)
            cyclic.append(1 if spline.use_cyclic_u else 0)
            continue
        # POLY and anything else Blender has: a polyline through the points.
        p = [(pt.co[0], pt.co[1], pt.co[2], 1.0) for pt in spline.points]
        if len(p) < 2:
            continue
        points.extend(p)
        counts.append(len(p))
        types.append(CURVE_POLY)
        orders.append(0)
        knots_mode.append(0)
        cyclic.append(1 if spline.use_cyclic_u else 0)

    if not counts:
        return []
    flat = np.asarray(points, dtype=np.float32).reshape(-1, 4)
    return [
        ("crv.points", "f32", flat),
        ("crv.counts", "u32", np.asarray(counts, dtype=np.uint32)),
        ("crv.types", "u8", np.asarray(types, dtype=np.uint8)),
        ("crv.orders", "u8", np.asarray(orders, dtype=np.uint8)),
        ("crv.knots_mode", "u8", np.asarray(knots_mode, dtype=np.uint8)),
        ("crv.cyclic", "u8", np.asarray(cyclic, dtype=np.uint8)),
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


def points_to_sections(cloud):
    """Read a point cloud into ``pts.*`` sections."""
    n = len(cloud.points)
    if n == 0:
        return []
    positions = np.empty(n * 3, dtype=np.float32)
    cloud.points.foreach_get("co", positions)
    sections = [("pts.positions", "f32", positions.reshape(-1, 3))]

    radii = np.empty(n, dtype=np.float32)
    cloud.points.foreach_get("radius", radii)
    if radii.any():
        sections.append(("pts.radii", "f32", radii))

    colors = read_point_colors(cloud)
    if colors is not None:
        sections.append(("pts.colors", "u8", colors))
    return sections


def read_point_colors(cloud):
    """The active point-domain colour attribute as u8 N×4, or None."""
    attrs = cloud.color_attributes
    if not attrs:
        return None
    attr = attrs.active_color if attrs.active_color is not None else attrs[0]
    if attr.domain != "POINT":
        return None
    floats = np.empty(len(cloud.points) * 4, dtype=np.float32)
    if attr.data_type == "BYTE_COLOR":
        attr.data.foreach_get("color_srgb", floats)
    else:
        attr.data.foreach_get("color", floats)
    return np.clip(np.rint(floats.reshape(-1, 4) * 255.0), 0.0, 255.0).astype(np.uint8)


# ------------------------------------------------- evaluated geometry (prefixes)


# Blender's CurvesType enum, as stored in the curve_type attribute.
CURVE_TYPE_CATMULL_ROM = 0
CURVE_TYPE_POLY = 1
CURVE_TYPE_BEZIER = 2
CURVE_TYPE_NURBS = 3


def geometry_set_to_sections(geometry, as_subd=False):
    """Protocol sections and attribute descriptors for an evaluated
    ``GeometrySet`` (a stage's prefix). Returns ``(sections, attributes)``.

    A stage's prefix is everything before its own modifier in the stack, so it
    can hold a mesh, curves, and points at once. The geometry set must stay
    referenced while the components are read: its datablocks die with it.

    Curves carry no attributes yet: Blender's curve helper is a legacy Curve
    datablock, which cannot store attributes, so a read-only channel would be
    lost on the way back anyway. Mesh, SubD cage and point attributes are
    supported end to end.
    """
    sections = []
    attributes = []
    mesh = geometry.mesh
    if mesh is not None and len(mesh.vertices):
        sections += mesh_to_sections(mesh, as_subd=as_subd)
        attribute_sections, attributes = mesh_attributes(mesh, as_subd=as_subd)
        sections += attribute_sections
    curves = geometry.curves
    if curves is not None and len(curves.points):
        sections += curves_component_to_sections(curves)
    cloud = geometry.pointcloud
    if cloud is not None and len(cloud.points):
        sections += points_to_sections(cloud)
        attribute_sections, point_attributes = points_attributes(cloud, index0=len(attributes))
        sections += attribute_sections
        attributes += point_attributes
    return sections, attributes


def curves_component_to_sections(curves):
    """Read a GeometrySet's curve component into ``crv.*`` sections.

    The component is the new Curves type: per-curve attributes carry the kind
    (``curve_type``), the NURBS order and knots mode, and the cyclic flag;
    per-point attributes carry the position, the NURBS weight, and the Bezier
    handles. NURBS and Bezier curves map to the same wire forms the legacy
    datablock reader produces, so a prefix curve is as exact as a base one.

    Catmull-Rom components have no wire representation (our protocol has poly,
    Bezier, and NURBS); they are skipped with a log line. They can only come
    from hand-built Geometry Nodes graphs, never from the helpers.
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

    for curve in curves.curves:
        first = curve.first_point_index
        length = curve.points_length
        if length < 2:
            continue
        index = curve.index
        ctype = int(type_attr.data[index].value) if type_attr is not None else CURVE_TYPE_POLY
        is_cyclic = bool(cyclic_attr.data[index].value) if cyclic_attr is not None else False

        if ctype == CURVE_TYPE_CATMULL_ROM:
            print(
                "[gh-stack] Catmull-Rom curve component skipped: no wire representation",
                flush=True,
            )
            continue

        if ctype == CURVE_TYPE_BEZIER:
            net = _component_bezier_net(positions, handle_left, handle_right, first, length, is_cyclic)
            if net is None:
                continue
            points.extend(net)
            counts.append(len(net))
            types.append(CURVE_NURBS)
            orders.append(4)
            modes.append(KNOTS_BEZIER)
            cyclic.append(0)
            continue

        chunk = []
        for i in range(first, first + length):
            p = positions[i].vector
            w = float(weights.data[i].value) if weights is not None else 1.0
            chunk.append((p[0], p[1], p[2], w))

        if ctype == CURVE_TYPE_NURBS:
            order = int(order_attr.data[index].value) if order_attr is not None else 4
            mode = int(knots_attr.data[index].value) if knots_attr is not None else KNOTS_NORMAL
            points.extend(chunk)
            counts.append(length)
            types.append(CURVE_NURBS)
            orders.append(order)
            modes.append(mode)
            cyclic.append(1 if is_cyclic else 0)
        else:  # CURVE_TYPE_POLY
            points.extend(chunk)
            counts.append(length)
            types.append(CURVE_POLY)
            orders.append(0)
            modes.append(0)
            cyclic.append(1 if is_cyclic else 0)

    if not counts:
        return []
    flat = np.asarray(points, dtype=np.float32).reshape(-1, 4)
    return [
        ("crv.points", "f32", flat),
        ("crv.counts", "u32", np.asarray(counts, dtype=np.uint32)),
        ("crv.types", "u8", np.asarray(types, dtype=np.uint8)),
        ("crv.orders", "u8", np.asarray(orders, dtype=np.uint8)),
        ("crv.knots_mode", "u8", np.asarray(modes, dtype=np.uint8)),
        ("crv.cyclic", "u8", np.asarray(cyclic, dtype=np.uint8)),
    ]


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


def sections_to_mesh(mesh, sections):
    """Update ``mesh`` from sections, in place.

    Positions are written with ``foreach_set`` when the vertex count matches;
    otherwise the mesh is rebuilt with ``clear_geometry`` + ``from_pydata``.
    Colours and creases are written to attributes. Extra sections (curves, points)
    are ignored here; they belong to other components.
    """
    by_name = {}
    for name, dtype, values in sections:
        by_name[name] = values

    verts = by_name.get("mesh.verts", by_name.get("subd.verts"))
    counts = by_name.get("mesh.face_counts", by_name.get("subd.face_counts"))
    indices = by_name.get("mesh.face_indices", by_name.get("subd.face_indices"))
    if verts is None or counts is None or indices is None:
        raise GeometryIOError("sections need verts, face_counts and face_indices")

    verts = np.ascontiguousarray(verts, dtype=np.float32)
    counts = np.ascontiguousarray(counts, dtype=np.uint32)
    indices = np.ascontiguousarray(indices, dtype=np.uint32)
    if len(indices) != int(counts.sum()):
        raise GeometryIOError(
            "face_indices length %d != sum(face_counts) %d" % (len(indices), int(counts.sum()))
        )

    same_topology = (
        len(mesh.vertices) == len(verts)
        and len(mesh.polygons) == len(counts)
        and len(mesh.loops) == len(indices)
    )

    if same_topology:
        mesh.vertices.foreach_set("co", verts.reshape(-1))
    else:
        mesh.clear_geometry()
        faces = _faces_from(counts, indices)
        mesh.from_pydata(verts.tolist(), [], faces)

    if "mesh.colors" in by_name:
        write_colors(mesh, np.asarray(by_name["mesh.colors"], dtype=np.uint8))

    creases = None
    if "subd.crease_edges" in by_name and "subd.crease_values" in by_name:
        creases = (by_name["subd.crease_edges"], by_name["subd.crease_values"])
    elif "mesh.crease_edges" in by_name and "mesh.crease_values" in by_name:
        creases = (by_name["mesh.crease_edges"], by_name["mesh.crease_values"])
    write_creases(mesh, creases)

    mesh.update()
    return mesh


def _faces_from(counts, indices):
    faces = []
    at = 0
    for count in counts.tolist():
        faces.append(tuple(int(i) for i in indices[at : at + count]))
        at += count
    return faces


def write_colors(mesh, colors):
    """Create or update the byte-colour attribute from u8 N×4 data."""
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


def write_creases(mesh, creases):
    """Create or update the crease attribute. ``creases`` is (edges, values) or None,
    in which case a zeroed attribute is still created so the mesh is explicitly
    uncreased rather than silently stale."""
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
        for k in range(len(edges)):
            a, b = int(edges[k][0]), int(edges[k][1])
            idx = lookup.get((min(a, b), max(a, b)))
            if idx is not None:
                values[idx] = cv[k]
    attr.data.foreach_set("value", values)


# ----------------------------------------------------------- curve and point write


def sections_to_curve(curve, sections):
    """Rebuild a Blender Curve datablock from ``crv.*`` sections, in place."""
    by_name = {}
    for name, dtype, values in sections:
        by_name[name] = values

    points = by_name.get("crv.points")
    counts = by_name.get("crv.counts")
    if points is None or counts is None:
        raise GeometryIOError("curve sections need crv.points and crv.counts")
    points = np.asarray(points, dtype=np.float32).reshape(-1, 4)
    counts = np.asarray(counts, dtype=np.uint32).reshape(-1)
    if int(counts.sum()) != len(points):
        raise GeometryIOError(
            "crv.points length %d != sum(crv.counts) %d" % (len(points), int(counts.sum()))
        )
    n_curves = len(counts)
    types = _u8(by_name.get("crv.types"), n_curves)
    orders = _u8(by_name.get("crv.orders"), n_curves)
    modes = _u8(by_name.get("crv.knots_mode"), n_curves)
    cyclic = _u8(by_name.get("crv.cyclic"), n_curves)

    curve.splines.clear()
    at = 0
    for i in range(n_curves):
        n = int(counts[i])
        chunk = points[at : at + n]
        at += n
        if n < 2:
            continue
        if types[i] == CURVE_POLY:
            _poly_spline(curve, chunk, bool(cyclic[i]))
        elif modes[i] == KNOTS_BEZIER or types[i] == CURVE_BEZIER:
            _bezier_spline(curve, chunk)
        else:
            _nurbs_spline(curve, chunk, int(orders[i]), int(modes[i]), bool(cyclic[i]))
    curve.update_tag()
    return curve


def _u8(values, count):
    if values is None:
        return np.zeros(count, dtype=np.uint8)
    return np.asarray(values, dtype=np.uint8).reshape(-1)


def _poly_spline(curve, chunk, cyclic):
    spline = curve.splines.new("POLY")
    spline.points.add(len(chunk) - 1)
    spline.points.foreach_set("co", chunk.reshape(-1))
    spline.use_cyclic_u = cyclic


def _nurbs_spline(curve, chunk, order, mode, cyclic):
    spline = curve.splines.new("NURBS")
    spline.points.add(len(chunk) - 1)
    spline.order_u = max(2, min(6, int(order)))
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
    """Rebuild a point cloud from ``pts.*`` sections, in place."""
    by_name = {}
    for name, dtype, values in sections:
        by_name[name] = values

    positions = by_name.get("pts.positions")
    if positions is None:
        cloud.resize(0)
        return cloud
    positions = np.asarray(positions, dtype=np.float32).reshape(-1, 3)
    n = len(positions)
    cloud.resize(n)
    cloud.points.foreach_set("co", positions.reshape(-1))

    radii = by_name.get("pts.radii")
    if radii is not None:
        radii = np.asarray(radii, dtype=np.float32).reshape(-1)
        if len(radii) == n:
            cloud.points.foreach_set("radius", radii)
    cloud.points.update()

    if "pts.colors" in by_name:
        write_point_colors(cloud, np.asarray(by_name["pts.colors"], dtype=np.uint8))
    return cloud


def write_point_colors(cloud, colors):
    """Create or update the byte-colour attribute of a point cloud."""
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


def object_to_sections(obj, as_subd=False):
    """Protocol sections and attribute descriptors for an object's base
    geometry, by object type. Returns ``(sections, attributes)``.

    Meshes travel as ``mesh.*`` (or ``subd.*`` when the stack is flagged as a
    SubD cage), curves as ``crv.*``, point clouds as ``pts.*``. A Blender
    surface is meshed first: Blender has no B-rep to send, so the surface
    arrives on the GH side as a mesh (lossy, like a Rhino Brep going the other
    way).
    """
    if obj.type == "MESH":
        attribute_sections, attributes = mesh_attributes(obj.data, as_subd=as_subd)
        return mesh_to_sections(obj.data, as_subd=as_subd) + attribute_sections, attributes
    if obj.type == "CURVE":
        return curve_to_sections(obj.data), []
    if obj.type == "POINTCLOUD":
        attribute_sections, attributes = points_attributes(obj.data)
        return points_to_sections(obj.data) + attribute_sections, attributes
    if obj.type == "SURFACE":
        import bpy

        mesh = surface_to_mesh(obj)
        try:
            attribute_sections, attributes = mesh_attributes(mesh, as_subd=False)
            return mesh_to_sections(mesh, as_subd=False) + attribute_sections, attributes
        finally:
            bpy.data.meshes.remove(mesh)
    return [], []


def surface_to_mesh(obj):
    """A new mesh datablock for a surface object's evaluated geometry. The
    caller owns it and removes it after use."""
    import bpy

    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(depsgraph)
    return bpy.data.meshes.new_from_object(evaluated)


def evaluated_mesh(obj, depsgraph=None):
    """The evaluated mesh of ``obj``. The caller does not free anything: with no
    modifiers this is ``obj.data``, otherwise the evaluated mesh is owned by the
    evaluated object and stays valid until the depsgraph updates."""
    if depsgraph is None:
        import bpy

        depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(depsgraph)
    return evaluated.data if evaluated.type == "MESH" else None
