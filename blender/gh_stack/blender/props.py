"""Parameter custom properties and UI ranges (plan §7).

Values live as custom properties named ``gh/<stage>/<param>`` so they can be
keyframed and driven. Ranges go through ``id_properties_ui``. The schema itself
(types, labels, choices) is kept in the stage's parameter collection for the UI.
"""

import bpy

from ..core import params as core_params
from . import stack as stack_mod

TYPE_DEFAULTS = {
    "float": 0.0,
    "int": 0,
    "bool": False,
    "vector3": (0.0, 0.0, 0.0),
    "color": (0.0, 0.0, 0.0),
    "choice": 0,
    "text": "",
}


def schema_of(stage):
    """The stage's schema as the plain dicts the core expects."""
    out = []
    for entry in stage.params:
        out.append(
            {
                "id": entry.param_id,
                "label": entry.label,
                "type": entry.type,
                "group": entry.group,
                "default": _typed_default(entry),
                "min": entry.min if entry.has_range else None,
                "max": entry.max if entry.has_range else None,
                "step": entry.step,
                "choices": [c for c in entry.choices.split("\n") if c] if entry.choices else [],
            }
        )
    return out


def _typed_default(entry):
    import json

    raw = entry.default
    if entry.type in ("vector3", "color"):
        try:
            return json.loads(raw) if raw else (0.0, 0.0, 0.0)
        except ValueError:
            return (0.0, 0.0, 0.0)
    if entry.type == "int" or entry.type == "choice":
        try:
            return int(float(raw)) if raw else 0
        except ValueError:
            return 0
    if entry.type == "bool":
        return str(raw).lower() in ("1", "true", "yes", "on")
    if entry.type == "float":
        try:
            return float(raw) if raw else 0.0
        except ValueError:
            return 0.0
    return raw or ""


def apply_schema(obj, stage, params):
    """Create or refresh the custom properties for one stage from a schema
    message. Existing values are kept, so keyframes survive a schema refresh —
    except when a parameter's type changed, where the old value cannot be
    reinterpreted and the property is recreated from the new default."""
    stage.params.clear()
    for param in params:
        entry = stage.params.add()
        entry.param_id = param.get("id", "")
        entry.stage_key = stage.key
        entry.label = param.get("label") or entry.param_id
        entry.type = param.get("type", "float")
        entry.group = param.get("group") or ""
        entry.default = _default_text(param)
        entry.has_range = param.get("min") is not None or param.get("max") is not None
        entry.min = float(param.get("min") or 0.0)
        entry.max = float(param.get("max") or 1.0)
        entry.step = float(param.get("step") or 0.01)
        choices = param.get("choices") or []
        entry.choices = "\n".join(str(c) for c in choices)

        name = stack_mod.param_prop_name(stage.key, entry.param_id)
        if name in obj and not _matches_type(obj[name], entry.type):
            # The GH side changed the parameter's type; the old value cannot be
            # reinterpreted (a float is not a vector), so start from the default.
            del obj[name]
            print(
                "[gh-stack] %s changed type to %s; its value was reset"
                % (name, entry.type),
                flush=True,
            )
        if name not in obj:
            obj[name] = _initial_value(entry)
        _set_ui(obj, name, entry)


def _matches_type(value, param_type):
    """Whether an existing custom property can hold this parameter type."""
    if param_type in ("vector3", "color"):
        try:
            return len(value) == 3
        except TypeError:
            return False
    if param_type == "text":
        return isinstance(value, str)
    if param_type == "bool":
        return isinstance(value, bool)
    if param_type in ("int", "choice"):
        return isinstance(value, int) and not isinstance(value, bool)
    if param_type == "float":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return False


def _default_text(param):
    import json

    value = param.get("default")
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return json.dumps(list(value))
    return str(value)


def _initial_value(entry):
    value = _typed_default(entry)
    if entry.type in ("vector3", "color"):
        return list(value)
    return value


def _set_ui(obj, name, entry):
    try:
        ui = obj.id_properties_ui(name)
    except TypeError:
        return
    try:
        kwargs = {"description": entry.label}
        if entry.has_range:
            if entry.type == "int":
                kwargs["min"] = int(entry.min)
                kwargs["max"] = int(entry.max)
            else:
                kwargs["min"] = float(entry.min)
                kwargs["max"] = float(entry.max)
        else:
            # No range declared: clear any range a previous schema set.
            # update(min=None) does not clear it; the type's extremes do.
            if entry.type in ("int", "choice"):
                kwargs["min"] = -(2 ** 31)
                kwargs["max"] = 2 ** 31 - 1
            elif entry.type in ("float", "vector3", "color"):
                kwargs["min"] = -3.4e38
                kwargs["max"] = 3.4e38
        if entry.type in ("float", "vector3", "color"):
            kwargs["step"] = float(entry.step or 0.01)
        if entry.type == "color":
            kwargs["subtype"] = "COLOR"
        elif entry.type == "vector3":
            kwargs["subtype"] = "XYZ"
        ui.update(**kwargs)
    except (TypeError, ValueError):
        pass


def read_values(obj, stage):
    """Current values for the stage's parameters, as the core's params dict."""
    out = {}
    for entry in stage.params:
        name = stack_mod.param_prop_name(stage.key, entry.param_id)
        if name in obj:
            out[entry.param_id] = obj[name]
    return out


def _safe_values(obj, stage):
    """The stage's current values, dropping any that cannot be cast to their
    schema type. A stale value (say a keyframed float on a parameter that is now
    a vector) falls back to the schema default instead of stalling the engine."""
    schema = schema_of(stage)
    values = read_values(obj, stage)
    safe = {}
    for param in schema:
        pid = param["id"]
        if pid not in values:
            continue
        try:
            core_params.cast(param["type"], values[pid])
            safe[pid] = values[pid]
        except (core_params.SchemaError, TypeError, ValueError):
            print(
                "[gh-stack] value for %s/%s does not fit type %s; using the default"
                % (stage.key, pid, param["type"]),
                flush=True,
            )
    return safe


def canonical_json(obj, stage):
    """Canonical parameter JSON for the request ID (protocol §6.2)."""
    return core_params.canonical_param_json(schema_of(stage), _safe_values(obj, stage))


def cast_values(obj, stage):
    return core_params.cast_params(schema_of(stage), _safe_values(obj, stage))


def register():
    pass


def unregister():
    pass
