"""Per-kind object dispatch; a type outside the table is a noted skip."""

from .attributes import mesh_attributes, points_attributes
from .common import _note
from .curves import curve_to_sections
from .mesh import mesh_to_sections, mesh_with_attributes
from .points import points_to_sections


def _mesh_object(obj, as_subd, notes):
    """A MESH object: faces as ``mesh.*``/``subd.*``, loose remainder as wire."""
    return mesh_with_attributes(obj.data, as_subd=as_subd, notes=notes)


def _curve_object(obj, as_subd, notes):
    return curve_to_sections(obj.data, notes=notes), []


def _point_cloud_object(obj, as_subd, notes):
    attribute_sections, attributes = points_attributes(obj.data)
    return points_to_sections(obj.data, notes=notes) + attribute_sections, attributes


def _surface_object(obj, as_subd, notes):
    import bpy

    _note(notes, "surface was meshed; Blender has no B-rep to send exactly")
    mesh = surface_to_mesh(obj)
    try:
        attribute_sections, attributes = mesh_attributes(mesh, as_subd=False)
        return mesh_to_sections(mesh, as_subd=False) + attribute_sections, attributes
    finally:
        bpy.data.meshes.remove(mesh)


# Object type -> reader; the one vocabulary of object kinds this side reads.
_OBJECT_READERS = {
    "MESH": _mesh_object,
    "CURVE": _curve_object,
    "POINTCLOUD": _point_cloud_object,
    "SURFACE": _surface_object,
}


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
    reader = _OBJECT_READERS.get(obj.type)
    if reader is None:
        _note(notes, "%s object has no wire geometry and was skipped" % obj.type)
        return [], []
    return reader(obj, as_subd, notes)


def surface_to_mesh(obj):
    """A new mesh datablock for a surface object's evaluated geometry. The
    caller owns it and removes it after use."""
    import bpy

    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(depsgraph)
    return bpy.data.meshes.new_from_object(evaluated)
