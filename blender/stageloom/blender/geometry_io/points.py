"""Point-cloud read/write: positions, radii and the native point colour."""

import numpy as np

from .common import (GeometryIOError, _active_color, _color_bytes,
                     sections_by_name, write_color_attribute)


# Blender's default point radius: absent ``pts.radii`` resets every point here.
POINT_RADIUS_DEFAULT = 0.01


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


def sections_to_points(cloud, sections):
    """Rebuild a point cloud from ``pts.*`` sections, in place.

    Absent ``pts.radii`` means "no per-point radii": every radius is reset to
    Blender's default rather than keeping a previous response's values.
    """
    by_name = sections_by_name(sections)

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
    write_color_attribute(cloud, colors, "pts.colors")
