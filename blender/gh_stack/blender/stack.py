"""Stack records: what lives on a stack object (plan §5).

The stack object is any object the user points the add-on at. Its stage records
and parameter schema live in property groups so they save with the .blend;
parameter *values* are custom properties (``gh/<stage>/<param>``) so they can be
keyframed and driven.
"""

import secrets
import string

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    StringProperty,
)

PARAM_PREFIX = "gh"
STAGE_KEY_LENGTH = 6

STATE_IDLE = "idle"
STATE_WAITING = "waiting"
STATE_OK = "ok"
STATE_ERROR = "error"
STATE_DISCONNECTED = "disconnected"


def new_stage_key(label="stage"):
    slug = "".join(c for c in label.lower().replace(" ", "-") if c in string.ascii_lowercase + "-0123456789")
    slug = (slug or "stage")[:12].strip("-") or "stage"
    return "%s-%s" % (slug, "".join(secrets.choice("0123456789abcdef") for _ in range(STAGE_KEY_LENGTH)))


def _value_items(self, context):
    """Dropdown entries for a value list parameter, from its schema values."""
    labels = [c for c in (self.values or "").split("\n") if c]
    if not labels:
        return [("0", "(no values)", "")]
    return [(str(i), label, "") for i, label in enumerate(labels)]


# The engine registers a hook so a UI-driven write can mark the object dirty.
# A raw ID-property write from Python does not tag the depsgraph, so without this
# a dropdown pick would sit unnoticed until something else triggered a tick.
_param_changed = None


def set_param_changed_hook(hook):
    global _param_changed
    _param_changed = hook


def notify_param_changed(obj):
    try:
        obj.update_tag()
    except (AttributeError, ReferenceError):
        pass
    if _param_changed is not None:
        _param_changed(obj)


def _value_changed(self, context):
    """Write the picked index to the parameter's custom property, which is the
    value the engine sends and the one keyframes live on."""
    obj = self.id_data  # the owning object, not context: works headless too
    if obj is None or not self.stage_key or not self.param_id:
        return
    name = param_prop_name(self.stage_key, self.param_id)
    if name in obj:
        obj[name] = int(self.value_index)
        notify_param_changed(obj)


class GhParam(bpy.types.PropertyGroup):
    """One parameter declaration, from the GH schema message (plan §7)."""

    param_id: StringProperty(name="ID", default="")
    stage_key: StringProperty(name="Stage", default="")
    label: StringProperty(name="Label", default="")
    type: StringProperty(name="Type", default="float")
    group: StringProperty(name="Group", default="")
    default: StringProperty(name="Default", default="")
    min: FloatProperty(name="Min", default=0.0)
    max: FloatProperty(name="Max", default=1.0)
    has_range: BoolProperty(name="Has range", default=False)
    step: FloatProperty(name="Step", default=0.01)
    values: StringProperty(name="Values", default="")  # newline separated
    # A view of the custom property, so the panel can draw a dropdown (plan §7).
    value_index: EnumProperty(name="Value", items=_value_items, update=_value_changed)


class GhStage(bpy.types.PropertyGroup):
    """One GH stage on a stack object."""

    key: StringProperty(name="Key", default="")
    label: StringProperty(name="Label", default="")
    enabled: BoolProperty(name="Enabled", default=True)
    state: StringProperty(name="State", default=STATE_IDLE)
    rid: StringProperty(name="Request ID", default="")
    nonce: IntProperty(name="Nonce", default=0)
    geom_hash: StringProperty(name="Geometry hash", default="")
    applied_out_hash: StringProperty(name="Applied output hash", default="")
    error: StringProperty(name="Last error", default="")
    warnings: StringProperty(name="Warnings", default="")
    params: CollectionProperty(type=GhParam)


class GhStack(bpy.types.PropertyGroup):
    """Per-object stack record. `gh_stack_id` doubles as the cache namespace."""

    stack_id: StringProperty(name="Stack ID", default="")
    connected_doc: StringProperty(name="GH document", default="")
    # A mesh stack flagged as a SubD control cage travels as subd.* sections
    # (protocol §5); the GH side builds a SubD from it and tags the creases.
    subd: BoolProperty(name="SubD cage", default=False)
    stages: CollectionProperty(type=GhStage)


def stack_of(obj):
    """The GhStack record for an object, or None when it has no stages."""
    return getattr(obj, "gh_stack", None)


def ensure_stack(obj):
    record = stack_of(obj)
    if record is None:
        return None
    if not record.stack_id:
        record.stack_id = secrets.token_hex(8)
    return record


def stage_of(obj, key):
    record = stack_of(obj)
    if record is None:
        return None
    for stage in record.stages:
        if stage.key == key:
            return stage
    return None


def param_prop_name(stage_key, param_id):
    return "%s/%s/%s" % (PARAM_PREFIX, stage_key, param_id)


def stage_prefix(stage_key):
    return "%s/%s/" % (PARAM_PREFIX, stage_key)


def clear_stage_params(obj, stage_key):
    """Remove the custom properties of one stage (values, not the schema)."""
    prefix = stage_prefix(stage_key)
    for name in list(obj.keys()):
        if name.startswith(prefix):
            del obj[name]


def register():
    bpy.utils.register_class(GhParam)
    bpy.utils.register_class(GhStage)
    bpy.utils.register_class(GhStack)
    bpy.types.Object.gh_stack = bpy.props.PointerProperty(type=GhStack)


def unregister():
    del bpy.types.Object.gh_stack
    bpy.utils.unregister_class(GhStack)
    bpy.utils.unregister_class(GhStage)
    bpy.utils.unregister_class(GhParam)
