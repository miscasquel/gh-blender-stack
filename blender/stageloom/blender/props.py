"""Parameter custom properties and UI ranges.

Values live as custom properties named ``gh/<stage>/<param>`` so they can be
keyframed and driven. Ranges go through ``id_properties_ui``. The schema itself
(types, labels, value lists) is kept in the stage's parameter collection for the UI.
"""

import json
import math

from ..core import params as core_params
from . import bridge, prefs, stack as stack_mod

# `id_properties_ui` clears a range with the type's own extremes; passing None
# does not clear it. The int bounds are Blender's ID-property int32 limits.
INT_MIN = -(2 ** 31)
INT_MAX = 2 ** 31 - 1
FLOAT_MIN = -3.4e38
FLOAT_MAX = 3.4e38
INT_TYPES = ("int", "value_list")
FLOAT_TYPES = ("float", "vector3", "color")
RANGE_TYPES = INT_TYPES + FLOAT_TYPES


def _schema_bound(param, key):
    """A finite declared bound, or ``(None, True)`` when it is unusable."""
    raw = param.get(key)
    if raw is None:
        return None, False
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None, True
    if not math.isfinite(value):
        return None, True
    return value, False


def _signature_bound(param, key):
    value, _bad = _schema_bound(param, key)
    if value is None:
        return None
    return round(value, 6)


def _schema_step(param):
    raw = param.get("step")
    if raw is None:
        return 0.01
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.01
    if not math.isfinite(value):
        return 0.01
    return value


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
                "min": entry.min if entry.has_min else None,
                "max": entry.max if entry.has_max else None,
                "step": entry.step,
                "values": [c for c in entry.values.split("\n") if c] if entry.values else [],
            }
        )
    return out


def _typed_default(entry):
    raw = entry.default
    if entry.type in ("vector3", "color"):
        try:
            value = json.loads(raw) if raw else (0.0, 0.0, 0.0)
            parts = tuple(float(c) for c in value)
        except (TypeError, ValueError):
            return (0.0, 0.0, 0.0)
        if len(parts) != 3 or not all(math.isfinite(c) for c in parts):
            return (0.0, 0.0, 0.0)
        return parts
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
    """What the stage currently holds, for comparison with an incoming schema.

    The declared *default* is deliberately not in here. It is only the fallback
    shown when no value has been sent; a definition that drives the Param's
    Default input (a frame value, for example) republishes a different default
    every solve. Treating that as a declaration change recreated properties on
    every playback step. Labels, types, ranges and value lists still compare,
    because those are real declaration changes.
    """
    return [
        (
            e.param_id,
            e.label,
            e.type,
            e.group,
            e.has_min,
            round(e.min, 6) if e.has_min else None,
            e.has_max,
            round(e.max, 6) if e.has_max else None,
            round(e.step, 6),
            e.values,
        )
        for e in stage.params
    ]


def _unique_params(params):
    """One declaration per id, keeping the first position and the last value.

    A duplicate id names one property, so the schema must contain one entry for
    it. Last declaration wins, matching a dict of the message's entries.
    """
    by_id = {}
    order = []
    for param in params:
        pid = param.get("id", "")
        if pid not in by_id:
            order.append(pid)
        by_id[pid] = param
    return [by_id[pid] for pid in order]


def incoming_signature(params):
    """The same shape, built from a schema message, without the default."""
    out = []
    for param in _unique_params(params):
        values = param.get("values") or []
        min_value = _signature_bound(param, "min")
        max_value = _signature_bound(param, "max")
        out.append(
            (
                param.get("id", ""),
                param.get("label") or param.get("id", ""),
                param.get("type", "float"),
                param.get("group") or "",
                min_value is not None,
                min_value,
                max_value is not None,
                max_value,
                round(_schema_step(param), 6),
                "\n".join(str(c) for c in values),
            )
        )
    return out


def _apply_bounds(entry, stage_key, param):
    """Store each declared side independently; an absent side stays absent."""
    for key in ("min", "max"):
        value, malformed = _schema_bound(param, key)
        if malformed:
            prefs.log(
                "[stageloom] schema %s/%s has an unusable %s %r; ignoring it"
                % (stage_key, param.get("id", ""), key, param.get(key)),
                flush=True,
            )
        setattr(entry, "has_" + key, value is not None)
        if value is None:
            setattr(entry, key, 0.0)
            continue
        stored = value
        if value < FLOAT_MIN or value > FLOAT_MAX:
            stored = max(FLOAT_MIN, min(FLOAT_MAX, value))
            prefs.log(
                "[stageloom] schema %s/%s %s %r is outside Blender's float range; using %r"
                % (stage_key, param.get("id", ""), key, value, stored),
                flush=True,
            )
        setattr(entry, key, stored)


def apply_schema(obj, stage, params):
    """Create or refresh the custom properties for one stage from a schema
    message. Existing values are kept, so keyframes survive a schema refresh —
    except when a parameter's type changed, where the old value cannot be
    reinterpreted and the property is recreated from the new default.

    Entries are reconciled in place rather than cleared and rebuilt: the plugin
    re-publishes its schema on a heartbeat, and rebuilding the collection under
    an open UI (a dropdown, a dragged slider) is what made the panel feel
    unstable."""
    params = _unique_params(params)
    wanted = {param.get("id", "") for param in params}
    seen = set()
    for index in range(len(stage.params) - 1, -1, -1):
        entry = stage.params[index]
        if entry.param_id not in wanted or entry.param_id in seen:
            stage.params.remove(index)
        else:
            seen.add(entry.param_id)
    by_id = {entry.param_id: entry for entry in stage.params}

    for param in params:
        pid = param.get("id", "")
        entry = by_id.get(pid)
        if entry is None:
            entry = stage.params.add()
            # Index only after the id is set: a new entry starts with an empty
            # id, and a duplicate id must resolve to this same entry, not add a
            # second one that targets the same custom property.
            entry.param_id = pid
            by_id[pid] = entry
        entry.param_id = pid
        entry.stage_key = stage.key
        entry.label = param.get("label") or entry.param_id
        entry.type = param.get("type", "float")
        entry.group = param.get("group") or ""
        entry.default = _default_text(param)
        _apply_bounds(entry, stage.key, param)
        entry.step = _schema_step(param)
        values = param.get("values") or []
        entry.values = "\n".join(str(c) for c in values)

        name = stack_mod.param_prop_name(stage.key, entry.param_id)
        _migrate_property_type(obj, name, entry)
        if entry.type == "value_list":
            # A stale index (from a changed value list) would break the dropdown
            # and send an index GH cannot use; clamp it.
            labels = [c for c in entry.values.split("\n") if c]
            if labels:
                index = int(obj[name])
                if index < 0 or index >= len(labels):
                    obj[name] = max(0, min(index, len(labels) - 1))
        _set_ui(obj, name, entry)


def _migrate_property_type(obj, name, entry):
    """Replace a property whose stored type cannot hold the declared type.

    Blender would coerce an int into a float (or a bool into an int) silently,
    which leaves a stale declaration and, worse, a property the request caster
    still accepts as the old type. A real storage-type change removes the old
    property with its keyframes and drivers and starts from the new default;
    the user is told because that animation cannot be reinterpreted.
    """
    if name not in obj:
        obj[name] = _initial_value(entry)
        return
    if _matches_type(obj[name], entry.type):
        return
    keys = _remove_animation(obj, name)
    del obj[name]
    notice = "%s changed type to %s; its value was reset" % (entry.label, entry.type)
    if keys:
        notice += " and %d keyframe(s)/driver(s) were removed" % keys
    bridge.state["notice"] = notice
    prefs.log("[stageloom] " + notice, flush=True)
    obj[name] = _initial_value(entry)


def _fcurve_slots(obj):
    """Yield ``(channelbag, fcurve)`` for every fcurve of the object's action.

    Blender 5.2 has no ``action.fcurves``; the layered API is the only path. A generator, so callers can remove while walking."""
    animation = getattr(obj, "animation_data", None)
    if animation is None or animation.action is None:
        return
    for layer in animation.action.layers:
        for strip in layer.strips:
            for bag in strip.channelbags:
                for fcurve in bag.fcurves:
                    yield bag, fcurve


def _remove_animation(obj, name):
    """Drop the keyframes and drivers of one custom property. Returns how many
    fcurves were removed."""
    path = '["%s"]' % name
    removed = 0
    for bag, fcurve in list(_fcurve_slots(obj)):
        if fcurve.data_path == path:
            bag.fcurves.remove(fcurve)
            removed += 1
    try:
        obj.driver_remove(path)
    except (TypeError, RuntimeError):
        pass
    return removed


def detach_animation(obj):
    """Give the object its own action before its parameter paths are renamed.

    Object duplication shares the action between the original and the copy
    (``obj.copy()`` leaves ``users == 2``); rewriting an fcurve path in place
    would then rename the original's keyframes too. Called once per split, not
    per property.
    """
    animation = getattr(obj, "animation_data", None)
    if animation is None or animation.action is None:
        return
    if animation.action.users > 1:
        animation.action = animation.action.copy()


def move_stage_params(obj, old_key, new_key):
    """Move one stage's parameter properties to a new stage key, values, UI,
    keyframes and drivers included.

    A duplicated object carries the stage record and its ``gh/<key>/<param>``
    custom properties verbatim. Splitting the stage onto its own key has to move
    the properties with it, or the copy silently loses its values and its
    animation (the old names become orphans under a key no stage owns).
    """
    old_prefix = stack_mod.stage_prefix(old_key)
    new_prefix = stack_mod.stage_prefix(new_key)
    for name in list(obj.keys()):
        if name.startswith(old_prefix):
            _rename_property(obj, name, new_prefix + name[len(old_prefix):])


def _rename_property(obj, old_name, new_name):
    """Rename one custom property, keeping its value, UI settings and animation."""
    if old_name not in obj or old_name == new_name:
        return
    value = obj[old_name]
    if hasattr(value, "to_list"):
        value = value.to_list()
    settings = None
    try:
        settings = obj.id_properties_ui(old_name).as_dict()
    except (KeyError, TypeError):
        pass
    if new_name in obj:
        # A re-split after undo/redo, or a name collision: the destination is
        # the stage's own property now. Do not overwrite it with the stale copy.
        _remove_animation(obj, old_name)
        del obj[old_name]
        return
    obj[new_name] = value
    if settings:
        try:
            obj.id_properties_ui(new_name).update(**settings)
        except (TypeError, ValueError):
            pass
    _rename_animation(obj, old_name, new_name)
    del obj[old_name]


def _rename_animation(obj, old_name, new_name):
    """Point one property's fcurves and drivers at its new name.

    The action was detached first (see :func:`detach_animation`), so this touches
    only the duplicate's animation. Drivers live on the object's animation data,
    not in the action, and are copied by ``obj.copy()``.
    """
    old_path = '["%s"]' % old_name
    new_path = '["%s"]' % new_name
    for _bag, fcurve in _fcurve_slots(obj):
        if fcurve.data_path == old_path:
            fcurve.data_path = new_path
    animation = getattr(obj, "animation_data", None)
    if animation is not None:
        for driver in animation.drivers:
            if driver.data_path == old_path:
                driver.data_path = new_path


# Parameters whose keys must hold, not interpolate: an interpolated int, bool or
# choice index is not a value Grasshopper can use.
CONSTANT_TYPES = ("int", "bool", "value_list")


def key_all(obj, stage, frame):
    """Key every parameter of a stage at ``frame``. Text parameters are skipped
    (not animatable), and int, bool and value list keys are set to constant
    interpolation. Value list parameters are keyed on the real custom property —
    the dropdown is a proxy, so this is their only keying path. Returns how many
    properties were keyed."""
    keyed = 0
    for entry in stage.params:
        if entry.type == "text":
            continue
        name = stack_mod.param_prop_name(stage.key, entry.param_id)
        if name not in obj:
            continue
        path = '["%s"]' % name
        obj.keyframe_insert(data_path=path, frame=frame)
        keyed += 1
        if entry.type in CONSTANT_TYPES:
            for _bag, fcurve in _fcurve_slots(obj):
                if fcurve.data_path == path:
                    for point in fcurve.keyframe_points:
                        point.interpolation = "CONSTANT"
    return keyed


def orphans(obj, stage):
    """The custom properties of one stage whose parameter left the schema: the
    orphan list of protocol §4.2. Their values and keyframes are kept, never
    silently deleted; Purge orphans removes them explicitly."""
    known = {stack_mod.param_prop_name(stage.key, entry.param_id) for entry in stage.params}
    prefix = stack_mod.stage_prefix(stage.key)
    return [name for name in obj.keys() if name.startswith(prefix) and name not in known]


def purge_orphans(obj, stage):
    """Remove every orphaned property of one stage, with its animation. Returns
    how many properties were removed."""
    removed = 0
    for name in orphans(obj, stage):
        _remove_animation(obj, name)
        del obj[name]
        removed += 1
    return removed


def _matches_type(value, param_type):
    """Whether an existing custom property can hold this parameter type.

    The check is on the stored type, not on what Blender can coerce. An int
    value accepted as a float would look compatible forever and turn a real
    int→float declaration change into a silent no-op.
    """
    if param_type in ("vector3", "color"):
        try:
            parts = list(value)
        except TypeError:
            return False
        return len(parts) == 3 and all(isinstance(part, float) for part in parts)
    if param_type == "text":
        return isinstance(value, str)
    if param_type == "bool":
        return isinstance(value, bool)
    if param_type in ("int", "value_list"):
        return isinstance(value, int) and not isinstance(value, bool)
    if param_type == "float":
        return isinstance(value, float)
    return False


def _default_text(param):
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
        # Force float components: an IDPropertyArray built from ints is an int
        # array, which would look like a different storage type on the next
        # schema heartbeat.
        return [float(part) for part in value]
    return value


def _type_extremes(param_type):
    if param_type in INT_TYPES:
        return INT_MIN, INT_MAX
    return FLOAT_MIN, FLOAT_MAX


def _ui_bound(value, param_type, lower):
    """A declared bound in the numeric domain of the property's UI."""
    number = float(value)
    if not math.isfinite(number):
        extremes = _type_extremes(param_type)
        return extremes[0 if lower else 1]
    if param_type in INT_TYPES:
        number = math.ceil(number) if lower else math.floor(number)
        number = max(INT_MIN, min(INT_MAX, number))
        return int(number)
    return max(FLOAT_MIN, min(FLOAT_MAX, number))


def _ui_bounds(entry):
    """The two UI limits: declared sides, type extremes for absent ones."""
    low, high = _type_extremes(entry.type)
    if entry.has_min:
        low = _ui_bound(entry.min, entry.type, True)
    if entry.has_max:
        high = _ui_bound(entry.max, entry.type, False)
    if entry.has_min and entry.has_max and low > high:
        prefs.log(
            "[stageloom] %s declares min %r above max %r; leaving it unconstrained"
            % (entry.param_id, entry.min, entry.max),
            flush=True,
        )
        return _type_extremes(entry.type)
    return low, high


def _ui_step(entry):
    try:
        step = float(entry.step)
    except (TypeError, ValueError):
        return 0.01
    if not math.isfinite(step) or step <= 0.0:
        return 0.01
    return step


def _update_ui(ui, name, kwargs, fallback=None):
    """Apply one UI group, reporting a rejection instead of swallowing it."""
    try:
        ui.update(**kwargs)
        return True
    except (TypeError, ValueError, OverflowError) as exc:
        prefs.log(
            "[stageloom] UI settings for %s were rejected (%s: %s)"
            % (name, type(exc).__name__, exc),
            flush=True,
        )
        if fallback is not None:
            try:
                ui.update(**fallback)
            except (TypeError, ValueError, OverflowError) as fallback_exc:
                prefs.log(
                    "[stageloom] could not reset the UI for %s (%s: %s)"
                    % (name, type(fallback_exc).__name__, fallback_exc),
                    flush=True,
                )
        return False


def _set_ui(obj, name, entry):
    try:
        ui = obj.id_properties_ui(name)
    except (KeyError, TypeError) as exc:
        prefs.log(
            "[stageloom] no UI settings for %s (%s: %s)"
            % (name, type(exc).__name__, exc),
            flush=True,
        )
        return
    if entry.type in RANGE_TYPES:
        low, high = _ui_bounds(entry)
        low_default, high_default = _type_extremes(entry.type)
        # Both sides are always written: the declared one, and the type extreme
        # that clears a side a previous schema set. update(min=None) does not.
        _update_ui(
            ui,
            name,
            {"description": entry.label, "min": low, "max": high},
            fallback={
                "description": entry.label,
                "min": low_default,
                "max": high_default,
            },
        )
    else:
        _update_ui(ui, name, {"description": entry.label})
    if entry.type in FLOAT_TYPES:
        _update_ui(ui, name, {"step": _ui_step(entry)})
    if entry.type == "color":
        _update_ui(ui, name, {"subtype": "COLOR"})
    elif entry.type == "vector3":
        _update_ui(ui, name, {"subtype": "XYZ"})


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
            prefs.log(
                "[stageloom] value for %s/%s does not fit type %s; using the default"
                % (stage.key, pid, param["type"]),
                flush=True,
            )
    return safe


def canonical_json(obj, stage):
    """Canonical parameter JSON for the request ID (protocol §6.2)."""
    return core_params.canonical_param_json(schema_of(stage), _safe_values(obj, stage))


def cast_values(obj, stage):
    return core_params.cast_params(schema_of(stage), _safe_values(obj, stage))
