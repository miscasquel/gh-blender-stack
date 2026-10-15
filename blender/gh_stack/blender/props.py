"""Parameter custom properties and UI ranges (plan §7).

Values live as custom properties named ``gh/<stage>/<param>`` so they can be
keyframed and driven. Ranges go through ``id_properties_ui``. The schema itself
(types, labels, value lists) is kept in the stage's parameter collection for the UI.
"""

from ..core import codec, params as core_params
from . import bridge, stack as stack_mod

TYPE_DEFAULTS = {
    "float": 0.0,
    "int": 0,
    "bool": False,
    "vector3": (0.0, 0.0, 0.0),
    "color": (0.0, 0.0, 0.0),
    "value_list": 0,
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
                "values": [c for c in entry.values.split("\n") if c] if entry.values else [],
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
    if entry.type in ("int", "value_list"):
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


def schema_signature(stage):
    """What the stage currently holds, for comparison with an incoming schema."""
    return [
        (e.param_id, e.label, e.type, e.group, e.default, e.has_range,
         round(e.min, 6), round(e.max, 6), round(e.step, 6), e.values)
        for e in stage.params
    ]


def incoming_signature(params):
    """The same shape, built from a schema message."""
    out = []
    for param in params:
        values = param.get("values") or []
        out.append(
            (
                param.get("id", ""),
                param.get("label") or param.get("id", ""),
                param.get("type", "float"),
                param.get("group") or "",
                _default_text(param),
                param.get("min") is not None or param.get("max") is not None,
                round(float(param.get("min") or 0.0), 6),
                round(float(param.get("max") or 1.0), 6),
                round(float(param.get("step") or 0.01), 6),
                "\n".join(str(c) for c in values),
            )
        )
    return out


def definition_fingerprint(stage):
    """What a cached frame was produced from, as far as Blender can know it: the
    protocol version and the stage's schema signature.

    The document name is *not* in here: it is only known while a client is
    connected, and a fingerprint that changes when Grasshopper is closed would
    defeat offline replay. A reconnected document is handled by the generation
    bump instead (the stack records which document it is bound to)."""
    import hashlib

    payload = repr((codec.PROTOCOL_VERSION, schema_signature(stage))).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def apply_schema(obj, stage, params):
    """Create or refresh the custom properties for one stage from a schema
    message. Existing values are kept, so keyframes survive a schema refresh —
    except when a parameter's type changed, where the old value cannot be
    reinterpreted and the property is recreated from the new default.

    Entries are reconciled in place rather than cleared and rebuilt: the plugin
    re-publishes its schema on a heartbeat, and rebuilding the collection under
    an open UI (a dropdown, a dragged slider) is what made the panel feel
    unstable."""
    wanted = [param.get("id", "") for param in params]
    for index in range(len(stage.params) - 1, -1, -1):
        if stage.params[index].param_id not in wanted:
            stage.params.remove(index)
    by_id = {entry.param_id: entry for entry in stage.params}

    for param in params:
        entry = by_id.get(param.get("id", ""))
        if entry is None:
            entry = stage.params.add()
            by_id[entry.param_id] = entry
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
        values = param.get("values") or []
        entry.values = "\n".join(str(c) for c in values)

        name = stack_mod.param_prop_name(stage.key, entry.param_id)
        if name in obj and not _matches_type(obj[name], entry.type):
            # The GH side changed the parameter's type. The old value cannot be
            # reinterpreted (a float is not a vector), and neither can its
            # keyframes or drivers, so they are removed with the property and the
            # user is told. A rename does not do this: the GUID keeps the keys.
            keys = _remove_animation(obj, name)
            del obj[name]
            notice = "%s changed type to %s; its value was reset" % (entry.label, entry.type)
            if keys:
                notice += " and %d keyframe(s)/driver(s) were removed" % keys
            bridge.state["notice"] = notice
            print("[gh-stack] " + notice, flush=True)
        if name not in obj:
            obj[name] = _initial_value(entry)
        elif entry.type == "value_list":
            # A stale index (from a changed value list) would break the dropdown
            # and send an index GH cannot use; clamp it.
            labels = [c for c in entry.values.split("\n") if c]
            if labels:
                index = int(obj[name])
                if index < 0 or index >= len(labels):
                    obj[name] = max(0, min(index, len(labels) - 1))
        _set_ui(obj, name, entry)


def _remove_animation(obj, name):
    """Drop the keyframes and drivers of one custom property. Returns how many
    fcurves were removed."""
    path = '["%s"]' % name
    removed = 0
    animation = getattr(obj, "animation_data", None)
    if animation is not None and animation.action is not None:
        for layer in list(animation.action.layers):
            for strip in list(layer.strips):
                for bag in list(strip.channelbags):
                    for fcurve in list(bag.fcurves):
                        if fcurve.data_path == path:
                            bag.fcurves.remove(fcurve)
                            removed += 1
    try:
        obj.driver_remove(path)
    except (TypeError, RuntimeError):
        pass
    return removed


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
    if param_type in ("int", "value_list"):
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
        # Only a vector or colour parameter carries an array default; for any
        # other type a list is a leftover and must not become a string like
        # "[0.0, 0.0, 0.0]" that later fails to parse.
        if param.get("type") in ("vector3", "color"):
            return json.dumps(list(value))
        return ""
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
            if entry.type in ("int", "value_list"):
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
