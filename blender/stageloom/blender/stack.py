"""Stack records: what lives on a stack object.

The stack object is any object the user points the add-on at. Its stage records
and parameter schema live in property groups so they save with the .blend;
parameter *values* are custom properties (``gh/<stage>/<param>``) so they can be
keyframed and driven.
"""

import hashlib
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

# What a stage consumes from Blender. A prefix stage reads the evaluated
# geometry before its modifier (its Stage In); a generator stage has no Stage In
# at all — its GH definition is driven entirely by Params and a Stage Out.
INPUT_PREFIX = "prefix"
INPUT_GENERATOR = "generator"

STATE_IDLE = "idle"
STATE_WAITING = "waiting"
STATE_OK = "ok"
STATE_ERROR = "error"
STATE_DISCONNECTED = "disconnected"
STATE_DISABLED = "disabled"
STATE_FROZEN = "frozen"
STATE_UNBOUND = "unbound"


def new_stage_key(label="stage"):
    slug = "".join(c for c in label.lower().replace(" ", "-") if c in string.ascii_lowercase + "-0123456789")
    slug = (slug or "stage")[:12].strip("-") or "stage"
    return "%s-%s" % (slug, "".join(secrets.choice("0123456789abcdef") for _ in range(STAGE_KEY_LENGTH)))


def duplicate_stage_key(stage_key, object_name):
    """A deterministic key for a stage split out of a runtime duplicate.

    Duplication copies the stage key verbatim, so both objects claim the same
    node group and Grasshopper wiring. A runtime split gives the new claimant a
    fresh key; deriving it from the source key and the object name keeps that
    split stable for the same duplication. Ownership is never inferred from
    this name: a load or undo that cannot prove the original is a strict
    conflict, and the user's resolution uses a random key instead.
    """
    slug = stage_key.rsplit("-", 1)[0] if "-" in stage_key else stage_key
    slug = (slug or "stage")[:12].strip("-") or "stage"
    digest = hashlib.sha256(("%s|%s" % (stage_key, object_name)).encode("utf-8")).hexdigest()
    return "%s-%s" % (slug, digest[:STAGE_KEY_LENGTH])


def new_stack_id():
    """A fresh session identity for one object's stack."""
    return secrets.token_hex(8)


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


def _stage_changed(self, context):
    """A stage-level control changed: queue a pass so the engine acts on it.

    A PropertyGroup write does not tag the depsgraph, so without this a mode
    change made outside `hello` (a saved file, a test) would wait for an
    unrelated edit before the engine noticed it.
    """
    obj = getattr(self, "id_data", None)
    if obj is not None:
        notify_param_changed(obj)


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


class StageloomParam(bpy.types.PropertyGroup):
    """One parameter declaration, from the GH schema message."""

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
    # A view of the custom property, so the panel can draw a dropdown.
    value_index: EnumProperty(name="Value", items=_value_items, update=_value_changed)


class StageloomStage(bpy.types.PropertyGroup):
    """One GH stage on a stack object."""

    key: StringProperty(name="Key", default="")
    label: StringProperty(name="Label", default="")
    # Derived from the modifier's own visibility flags on every pass: a stage is
    # active while its modifier is visible in any context (viewport, render or
    # edit mode), and disabled only when all three are off. Kept as a field so
    # saved files and readers of the record stay coherent; the toggles in the
    # modifiers panel are the control, not a checkbox in the panel.
    enabled: BoolProperty(name="Enabled", default=True)
    # Freeze: no requests — the helpers keep their last output. Pull still
    # refreshes on demand.
    frozen: BoolProperty(name="Frozen", default=False)
    # What the stage consumes. Hello publishes `has_stage_in`; an absent field
    # means an older client, whose claim is unknown, so the saved mode stays.
    input_mode: EnumProperty(
        name="Input",
        items=(
            (INPUT_PREFIX, "Prefix", "The stage consumes the geometry before its modifier"),
            (
                INPUT_GENERATOR,
                "Generator",
                "The stage has no Stage In and consumes only its parameters",
            ),
        ),
        default=INPUT_PREFIX,
        update=_stage_changed,
    )
    # Whether Grasshopper has a Stage Out for this key. False is a one-way
    # stage: Blender still delivers geometry to its Stage In, but there is no
    # answer to wait for or apply. The field is unknown on older clients, so the
    # default keeps the two-way path.
    has_stage_out: BoolProperty(name="Has Stage Out", default=True)
    # True while the stage's native Bake node holds render data. Baking freezes
    # the stage automatically; Clear Bake removes the data and unfreezes it.
    baked: BoolProperty(name="Baked", default=False)
    # The freeze set one bake owns: the baked stage carries the token, and every
    # upstream stage it froze carries the same token here. Clear Bake matches
    # them, so a stage the user had frozen before the bake is left frozen.
    bake_token: StringProperty(name="Bake token", default="")
    frozen_by_bake: StringProperty(name="Frozen by bake", default="")
    # The modifier's persistent_uid, written when the modifier is created or
    # first seen. It is what tells "the modifier was deleted" (the stage goes
    # with it) apart from "the node group was deleted" (the group is rebuilt),
    # which the group-name link alone cannot.
    modifier_uid: IntProperty(name="Modifier", default=0)
    state: StringProperty(name="State", default=STATE_IDLE)
    # The request the stage last asked for: set when a request goes out, never
    # for a frame the scene has merely moved to. `_handle_response` compares an
    # incoming rid against this to recognise the answer the stage is waiting
    # for; a reply for any other request is dropped.
    rid: StringProperty(name="Request ID", default="")
    nonce: IntProperty(name="Nonce", default=0)
    geom_hash: StringProperty(name="Geometry hash", default="")
    applied_out_hash: StringProperty(name="Applied output hash", default="")
    error: StringProperty(name="Last error", default="")
    warnings: StringProperty(name="Warnings", default="")
    params: CollectionProperty(type=StageloomParam)


class StageloomStack(bpy.types.PropertyGroup):
    """Per-object stack record. `stack_id` scopes the stage's session state."""

    stack_id: StringProperty(name="Stack ID", default="")
    # A mesh stack flagged as a SubD control cage travels as subd.* sections
    # (protocol §5); the GH side builds a SubD from it and tags the creases.
    subd: BoolProperty(name="SubD cage", default=False)
    stages: CollectionProperty(type=StageloomStage)


def stack_of(obj):
    """The Stageloom stack record for an object, or None when it has no stages."""
    return getattr(obj, "gh_stack", None)


def ensure_stack(obj):
    record = stack_of(obj)
    if record is None:
        return None
    if not record.stack_id:
        record.stack_id = new_stack_id()
    return record


def stage_of(obj, key):
    record = stack_of(obj)
    if record is None:
        return None
    for stage in record.stages:
        if stage.key == key:
            return stage
    return None


def is_generator(stage):
    """Whether the stage's GH definition has no Stage In.

    A generator consumes only Params: the Blender-side prefix is not part of its
    request, so its request id does not depend on the object's geometry.
    """
    return stage.input_mode == INPUT_GENERATOR


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
    bpy.utils.register_class(StageloomParam)
    bpy.utils.register_class(StageloomStage)
    bpy.utils.register_class(StageloomStack)
    bpy.types.Object.gh_stack = bpy.props.PointerProperty(type=StageloomStack)


def unregister():
    try:
        del bpy.types.Object.gh_stack
    except AttributeError:
        pass
    for cls in (StageloomStack, StageloomStage, StageloomParam):
        try:
            bpy.utils.unregister_class(cls)
        except (RuntimeError, ValueError):
            pass
