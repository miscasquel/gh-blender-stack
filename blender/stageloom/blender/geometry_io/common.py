"""Shared geometry vocabulary: the error type, the note channels, the colour
transfer shared by meshes and point clouds, and the one ``crv.*`` builder."""

import numpy as np

CREASE_ATTRIBUTE = "crease_edge"
COLOR_ATTRIBUTE = "Col"


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


def _active_color(owner):
    """The active colour attribute (or the first one), or None. One source of
    truth for the native colour mapping and for the skip list of the generic
    attribute reader, so an attribute is never sent twice."""
    attrs = owner.color_attributes
    if not attrs:
        return None
    return attrs.active_color if attrs.active_color is not None else attrs[0]


def write_color_attribute(owner, colors, section_name):
    """Create or update ``owner``'s native byte-colour channel from u8 N×4.

    ``None`` removes the stage's own channel: absent means "no colour".
    """
    if colors is None:
        attr = owner.color_attributes.get(COLOR_ATTRIBUTE)
        if attr is not None and attr.name == COLOR_ATTRIBUTE:
            owner.color_attributes.remove(attr)
        return
    if colors.ndim != 2 or colors.shape[1] != 4:
        raise GeometryIOError("%s must be N x 4" % section_name)
    attr = owner.color_attributes.get(COLOR_ATTRIBUTE)
    if attr is None or attr.domain != "POINT" or attr.data_type != "BYTE_COLOR":
        # Keep one attribute of our own, so a user's attribute is never rewritten.
        if attr is not None and attr.name == COLOR_ATTRIBUTE:
            owner.color_attributes.remove(attr)
        attr = owner.color_attributes.new(COLOR_ATTRIBUTE, "BYTE_COLOR", "POINT")
    if len(attr.data) != len(colors):
        raise GeometryIOError(
            "colour attribute has %d entries, sections have %d" % (len(attr.data), len(colors))
        )
    attr.data.foreach_set("color_srgb", colors.reshape(-1).astype(np.float32) / 255.0)
    owner.color_attributes.active_color = attr


def sections_by_name(sections):
    """A section list as name -> values, the last definition winning."""
    return {name: values for name, _dtype, values in sections}


def crv_sections(prefix, points, counts, types, orders, modes, cyclic):
    """The six ``crv.*`` sections of one curve batch; ``points`` is float N×4."""
    name = prefix + "crv"
    return [
        (name + ".points", "f32", points),
        (name + ".counts", "u32", np.asarray(counts, dtype=np.uint32)),
        (name + ".types", "u8", np.asarray(types, dtype=np.uint8)),
        (name + ".orders", "u8", np.asarray(orders, dtype=np.uint8)),
        (name + ".knots_mode", "u8", np.asarray(modes, dtype=np.uint8)),
        (name + ".cyclic", "u8", np.asarray(cyclic, dtype=np.uint8)),
    ]
