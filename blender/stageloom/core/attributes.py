"""The attribute section format (protocol §5 addition, 2026-10-15).

An attribute is a per-element named array attached to one geometry pool:
``mesh``, ``subd``, ``crv`` or ``pts`` — the protocol's own group names. One
attribute is one ``attr.<i>.values`` section plus one descriptor in the frame
header, because a section name alone cannot carry the attribute's name, domain
and type:

    {"group": "mesh", "name": "panel_id", "domain": "FACE", "type": "INT",
     "components": 1, "count": 3, "section": "attr.0.values"}

``type`` is Blender's attribute data type (the vocabulary both platforms share);
the wire dtype and the components per element follow from it. ``STRING`` adds an
``offsets`` section holding each string's byte length; the values section is the
concatenated UTF-8 bytes.

This module is pure Python: the codec, the engine, the fake GH peer and the
tests all build the same descriptors from it.
"""

# Blender data type -> (wire dtype, components per element).
WIRE_TYPES = {
    "FLOAT": ("f32", 1),
    "INT": ("i32", 1),
    "BOOLEAN": ("u8", 1),
    "FLOAT_VECTOR": ("f32", 3),
    "FLOAT2": ("f32", 2),
    "FLOAT4": ("f32", 4),
    "FLOAT_COLOR": ("f32", 4),
    "BYTE_COLOR": ("u8", 4),
    "QUATERNION": ("f32", 4),
    "FLOAT4X4": ("f32", 16),
    "INT8": ("i32", 1),
    "INT16_2D": ("i32", 2),
    "INT32_2D": ("i32", 2),
    "STRING": ("u8", 1),
}

# The geometry pools attributes can belong to (protocol §5 group names).
GROUPS = ("mesh", "subd", "crv", "pts")

# Attribute names that are not data: the position is the geometry itself, and
# leading-dot names are Blender's internal attributes.
SKIP_NAMES = ("position",)


def wire_dtype(type_name):
    return WIRE_TYPES[type_name][0]


def components(type_name):
    return WIRE_TYPES[type_name][1]


def section_names(index, prefix=""):
    """(values section, offsets section or None) for attribute ``index``.

    ``prefix`` namespaces the sections of one instance definition
    (``idef.0.``), protocol §5.2.
    """
    return prefix + "attr.%d.values" % index, prefix + "attr.%d.offsets" % index


def descriptor(group, name, domain, type_name, count, index, prefix="", definition=None):
    """The header entry for one attribute section. ``definition`` is the
    instance-definition index the attribute belongs to, when it is not the
    frame's own geometry (§5.2)."""
    section, offsets = section_names(index, prefix)
    entry = {
        "group": group,
        "name": name,
        "domain": domain,
        "type": type_name,
        "components": components(type_name),
        "count": int(count),
        "section": section,
    }
    if definition is not None:
        entry["definition"] = int(definition)
    if type_name == "STRING":
        entry["offsets"] = offsets
    return entry
