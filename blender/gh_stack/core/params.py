"""Parameter schemas and value casting (plan §7).

The schema a GH Param component publishes becomes Blender custom properties. This
module is the pure logic of that mapping: given a GH-ish schema, produce values that
are safe to put on the wire, and cast what comes back.
"""

from . import codec

# The GH parameter types, as published in the schema (protocol §4.2).
PARAM_FLOAT = "float"
PARAM_INT = "int"
PARAM_BOOL = "bool"
PARAM_VECTOR3 = "vector3"
PARAM_COLOR = "color"
PARAM_VALUE_LIST = "value_list"
PARAM_TEXT = "text"

PARAM_TYPES = (
    PARAM_FLOAT,
    PARAM_INT,
    PARAM_BOOL,
    PARAM_VECTOR3,
    PARAM_COLOR,
    PARAM_VALUE_LIST,
    PARAM_TEXT,
)

# Which of them may be keyframed (plan §7). Text is documented as not animatable.
ANIMATABLE = frozenset(
    (PARAM_FLOAT, PARAM_INT, PARAM_BOOL, PARAM_VECTOR3, PARAM_COLOR, PARAM_VALUE_LIST)
)


class SchemaError(Exception):
    """A schema is malformed."""


def default_for(param_type):
    """The neutral default for a type, used when a schema omits one."""
    if param_type == PARAM_FLOAT:
        return 0.0
    if param_type == PARAM_INT:
        return 0
    if param_type == PARAM_BOOL:
        return False
    if param_type in (PARAM_VECTOR3, PARAM_COLOR):
        return (0.0, 0.0, 0.0)
    if param_type == PARAM_VALUE_LIST:
        return 0
    if param_type == PARAM_TEXT:
        return ""
    raise SchemaError("unknown parameter type %r" % (param_type,))


def cast(param_type, value):
    """Cast ``value`` to the schema type before it goes on the wire.

    The wire rules are protocol §4.3: floats as JSON numbers, ints as integers,
    bools as ``true``/``false``, vectors and colours as three numbers, value
    lists as integers (the index), text as strings.
    """
    if param_type == PARAM_FLOAT:
        return float(value)
    if param_type == PARAM_INT:
        return int(round(float(value)))
    if param_type == PARAM_BOOL:
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if param_type == PARAM_VALUE_LIST:
        return int(value)
    if param_type in (PARAM_VECTOR3, PARAM_COLOR):
        parts = list(value)
        if len(parts) != 3:
            raise SchemaError("%s needs three components, got %d" % (param_type, len(parts)))
        return tuple(float(p) for p in parts)
    if param_type == PARAM_TEXT:
        return str(value)
    raise SchemaError("unknown parameter type %r" % (param_type,))


def cast_params(schema, values):
    """Cast a whole parameter dict. ``values`` is ``{param_id: value}``."""
    out = {}
    for param in schema:
        pid = param["id"]
        ptype = param["type"]
        if pid in values:
            out[pid] = cast(ptype, values[pid])
        elif "default" in param and param["default"] is not None:
            out[pid] = cast(ptype, param["default"])
        else:
            out[pid] = default_for(ptype)
    return out


def clamp(param, value):
    """Clamp a cast value to the schema range, if one is declared.

    Vectors and colours are clamped component-wise. Returns the value unchanged
    when the schema declares no range.
    """
    if param.get("min") is None and param.get("max") is None:
        return value
    lo, hi = param.get("min"), param.get("max")
    if param["type"] in (PARAM_VECTOR3, PARAM_COLOR):
        return tuple(_clamp_one(v, lo, hi) for v in value)
    return _clamp_one(value, lo, hi)


def _clamp_one(v, lo, hi):
    if lo is not None and v < lo:
        return float(lo)
    if hi is not None and v > hi:
        return float(hi)
    return v


def canonical_param_json(schema, values):
    """Canonical parameter JSON, used verbatim in the request ID hash (§6.2)."""
    return codec.canonical_json(cast_params(schema, values))
