"""Mesh read/write: sections, native colour/crease channels, group selection,
the loose-edge wire mapping and the topology-token fast path. The verified
``_topology_matches`` comparison is the only polygon/loop read on the write
side.
"""

import numpy as np

from .common import (CREASE_ATTRIBUTE, GeometryIOError, _active_color,
                     _color_bytes, _count_note, _note, crv_sections,
                     sections_by_name, write_color_attribute)
from .attributes import mesh_attributes, wire_attributes


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
        zero = np.zeros(len(loose), dtype=np.uint8)
        sections += crv_sections(
            prefix, points,
            np.full(len(loose), 2, dtype=np.uint32),
            zero, zero, zero, zero,
        )

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


def mesh_with_attributes(mesh, as_subd=False, index0=0, prefix="", definition=None,
                         part=None, notes=None):
    """One mesh's geometry and generic attributes, including its loose remainder.

    The single reader shared by object dispatch and the definition serializer.
    ``index0`` is the running attribute index of the frame; the loose remainder
    continues after this mesh's generic attributes.
    """
    if not as_subd and len(mesh.polygons) == 0:
        return wire_mesh_to_sections(
            mesh, index0=index0, prefix=prefix, definition=definition,
            part=part, notes=notes,
        )
    sections = list(mesh_to_sections(mesh, as_subd=as_subd, prefix=prefix, notes=notes))
    attribute_sections, attributes = mesh_attributes(
        mesh, as_subd=as_subd, index0=index0, prefix=prefix,
        definition=definition, part=part,
    )
    sections += attribute_sections
    if not as_subd:
        wire_sections, wire_attrs = wire_mesh_to_sections(
            mesh, index0=index0 + len(attributes), prefix=prefix,
            definition=definition, part=part, notes=notes,
        )
        sections += wire_sections
        attributes += wire_attrs
    return sections, attributes


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
    otherwise the validated table is written in bulk with
    ``vertices.add``/``loops.add``/``polygons.add`` and ``foreach_set``.

    ``topology`` is the producer's additive mesh-topology token (protocol §4.4)
    and ``previous`` is the token stored with the helper's displayed geometry.
    When the two match and the cheap element counts agree, positions, colours,
    creases and attributes are written without reading ``mesh.polygons`` or
    ``mesh.loops`` at all. A present-but-different token rebuilds directly (the
    token already proves the topology differs), and an absent token keeps the
    verified comparison for old peers. Extra sections (curves, points) are
    ignored here; they belong to other components.
    """
    by_name = sections_by_name(sections)

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
        _rebuild_topology(mesh, verts, counts, indices)

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


def _rebuild_topology(mesh, verts, counts, indices):
    """Replace the mesh topology with one bulk array write.

    The whole group is already validated (contiguous N×3 float positions, face
    counts with every entry ≥ 3, and a flat vertex-index array that sums to the
    counts). ``vertices.add``/``loops.add``/``polygons.add`` plus
    ``foreach_set`` writes that validated table in place, which avoids building
    a Python tuple per face and the ``from_pydata`` conversion. Face order,
    n-gons and derived edges are identical to the old builder.
    """
    mesh.clear_geometry()
    if len(verts):
        mesh.vertices.add(len(verts))
        mesh.vertices.foreach_set("co", verts.reshape(-1))
    if len(indices):
        mesh.loops.add(len(indices))
        mesh.loops.foreach_set("vertex_index", indices)
    if len(counts):
        mesh.polygons.add(len(counts))
        starts = np.empty(len(counts), dtype=np.int32)
        starts[0] = 0
        if len(counts) > 1:
            starts[1:] = np.cumsum(counts[:-1], dtype=np.int32)
        mesh.polygons.foreach_set("loop_start", starts)
    # Edges are derived data: the crease writer and the wire reader both need
    # them immediately, so build them in this update rather than leaving them
    # to a later lazy evaluation.
    mesh.update(calc_edges=True)


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
    return bool(np.array_equal(current_counts, counts)
                and np.array_equal(current_indices, indices))


def write_colors(mesh, colors):
    """Create or update the byte-colour attribute from u8 N×4 data.

    ``None`` removes the stage's native colour channel: an absent
    ``mesh.colors`` section means "no colour", not "keep the previous one".
    """
    write_color_attribute(mesh, colors, "mesh.colors")


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
