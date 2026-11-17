"""Geometry <-> protocol sections, on the Blender side.

Reads evaluated geometry into protocol v2 sections and writes sections back in
place where topology allows. Section names follow protocol §5. Curves keep
order, weights and endpoint/uniform modes; what Blender cannot express exactly
is returned as a counted note on the stage's input warnings (and warned about
on the GH side where that side owns the approximation). This is the bpy layer;
core stays pure.

The implementation is split by responsibility underneath this facade:
``common``, ``attributes``, ``mesh``, ``curves``, ``points``, ``instances`` and
``dispatch``.
"""

from .attributes import (describe, mesh_attributes, points_attributes,  # noqa: F401
                         read_attributes, wire_attributes, write_attributes)
from .common import COLOR_ATTRIBUTE, CREASE_ATTRIBUTE, GeometryIOError  # noqa: F401
from .curves import (CURVE_BEZIER, CURVE_INTERNAL_ATTRIBUTES, CURVE_NURBS,  # noqa: F401
                     CURVE_POLY, CURVE_TYPE_BEZIER, CURVE_TYPE_CATMULL_ROM,
                     CURVE_TYPE_NURBS, CURVE_TYPE_POLY, KNOTS_BEZIER,
                     KNOTS_ENDPOINT, KNOTS_NORMAL, curve_to_sections,
                     curves_component_attributes, curves_component_to_sections,
                     sections_to_curve)
from .dispatch import object_to_sections, surface_to_mesh  # noqa: F401
from .instances import MAX_INSTANCE_DEPTH, geometry_set_to_sections  # noqa: F401
from .mesh import (isolated_vertex_indices, loose_edge_indices, mesh_to_sections,  # noqa: F401
                   read_colors, read_creases, sections_to_mesh, select_mesh_group,
                   wire_mesh_to_sections, write_colors, write_creases)
from .points import (POINT_RADIUS_DEFAULT, points_to_sections, read_point_colors,  # noqa: F401
                     sections_to_points, write_point_colors)
