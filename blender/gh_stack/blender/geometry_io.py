"""Geometry <-> protocol sections, on the Blender side (plan §6, §11).

Reads an evaluated mesh into protocol v2 sections and writes sections back into a
mesh in place where the topology allows, rebuilding when it does not. Numpy through
``foreach_get``/``foreach_set``; ``from_pydata`` only on the rebuild path, which is
the slow path and the one scripted changes take.

Section names follow protocol §5: ``mesh.*`` for plain meshes, ``subd.*`` for a
mesh flagged as a SubD control cage. The geometry hash order is the same order the
sections are returned in.

This module is the bpy layer: core stays pure (see blender/gh_stack/core). Curve
and point extraction arrive with the curve/point work; mesh and SubD are what the
geometry round trip needs first.
"""

import numpy as np

# Section orders per protocol §5. mesh.colors exists only for plain meshes;
# subd.colors does not exist in v2.
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

CREASE_ATTRIBUTE = "crease_edge"
COLOR_ATTRIBUTE = "Col"


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
    attrs = mesh.color_attributes
    if not attrs:
        return None
    attr = attrs.active_color if attrs.active_color is not None else attrs[0]
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


# ------------------------------------------------------------------ helpers


def evaluated_mesh(obj, depsgraph=None):
    """The evaluated mesh of ``obj``. The caller does not free anything: with no
    modifiers this is ``obj.data``, otherwise the evaluated mesh is owned by the
    evaluated object and stays valid until the depsgraph updates."""
    if depsgraph is None:
        import bpy

        depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(depsgraph)
    return evaluated.data if evaluated.type == "MESH" else None
