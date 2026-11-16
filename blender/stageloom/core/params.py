"""Parameter schemas and value casting.

The schema a GH Param component publishes becomes Blender custom properties. This
module is the pure logic of that mapping: given a GH-ish schema, produce values that
are safe to put on the wire, and cast what comes back.
"""

import math

from . import codec

# The GH parameter types, as published in the schema (protocol §4.2).
PARAM_FLOAT = "float"
PARAM_INT = "int"
PARAM_BOOL = "bool"
PARAM_VECTOR3 = "vector3"
PARAM_COLOR = "color"
PARAM_VALUE_LIST = "value_list"
PARAM_TEXT = "text"


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


def _finite(value, label):
    """A number that JSON can carry. NaN and the infinities are not JSON."""
    number = float(value)
    if not math.isfinite(number):
        raise SchemaError("%s must be finite, got %r" % (label, number))
    return number


def cast(param_type, value):
    """Cast ``value`` to the schema type before it goes on the wire.

    The wire rules are protocol §4.3: floats as JSON numbers, ints as integers,
    bools as ``true``/``false``, vectors and colours as three numbers, value
    lists as integers (the index), text as strings. Non-finite numbers are
    rejected: they cannot be encoded as JSON and the C# peer rejects them.
    """
    if param_type == PARAM_FLOAT:
        return _finite(value, "float")
    if param_type == PARAM_INT:
        return int(round(_finite(value, "int")))
    if param_type == PARAM_BOOL:
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if param_type == PARAM_VALUE_LIST:
        return int(_finite(value, "value_list"))
    if param_type in (PARAM_VECTOR3, PARAM_COLOR):
        parts = list(value)
        if len(parts) != 3:
            raise SchemaError("%s needs three components, got %d" % (param_type, len(parts)))
        return tuple(_finite(p, param_type) for p in parts)
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


def canonical_param_json(schema, values):
    """Canonical parameter JSON, used verbatim in the request ID hash (§6.2)."""
    return codec.canonical_json(cast_params(schema, values))
