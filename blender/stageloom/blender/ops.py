"""Operators: Add Stage, Push, Pull, Freeze, Key all, Purge orphans, server
control (plan §9.8).

Stage removal and enable/disable are native: deleting the stage's modifier
removes the stage, and the modifier's own visibility toggles are the switch
(plan §11). The add-on never writes those toggles after creation.
"""

import bpy
from bpy.props import IntProperty, StringProperty

from . import bridge, engine, helpers, nodes, props, stack as stack_mod

STACK_TYPES = {"MESH", "CURVE", "POINTCLOUD", "SURFACE"}


def _stage(context, index):
    """The (object, record, stage) the operator was pointed at, or Nones."""
    obj = context.object
    record = stack_mod.stack_of(obj) if obj is not None else None
    if record is None or not (0 <= index < len(record.stages)):
        return None, None, None
    return obj, record, record.stages[index]


class STAGELOOM_OT_add_stage(bpy.types.Operator):
    bl_idname = "stageloom.add_stage"
    bl_label = "Add Stageloom Stage"
    bl_description = "Add a Grasshopper stage to the active object"
    bl_options = {"REGISTER", "UNDO"}

    label: StringProperty(name="Label", default="Stage")

    def execute(self, context):
        obj = context.object
        if obj is None or obj.type not in STACK_TYPES:
            self.report({"ERROR"}, "select a mesh, curve, point cloud or surface first")
            return {"CANCELLED"}
        record = stack_mod.ensure_stack(obj)
        if record is None:
            self.report({"ERROR"}, "this object cannot hold a stack")
            return {"CANCELLED"}
        stage = record.stages.add()
        stage.key = stack_mod.new_stage_key(self.label)
        stage.label = self.label
        helpers.ensure_helpers(obj, stage.key)
        nodes.ensure_modifier(obj, stage)
        obj.update_tag()
        engine.request_again(obj, stage)
        self.report({"INFO"}, "Added stage %s" % stage.key)
        return {"FINISHED"}


class STAGELOOM_OT_push(bpy.types.Operator):
    bl_idname = "stageloom.push"
    bl_label = "Push"
    bl_description = "Send the current state to Grasshopper again, and reset this stack's frame cache"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        engine.push(context.object)
        return {"FINISHED"}


class STAGELOOM_OT_pull(bpy.types.Operator):
    bl_idname = "stageloom.pull"
    bl_label = "Pull"
    bl_description = "Ask Grasshopper for this stage's output again; cached frames are kept"
    bl_options = {"REGISTER", "UNDO"}

    index: IntProperty(name="Index", default=-1)

    def execute(self, context):
        obj, _record, stage = _stage(context, self.index)
        if stage is None:
            return {"CANCELLED"}
        # A fresh request without a cache reset: the frame cache stays valid, so
        # this is also how a frozen stage is refreshed once, on demand.
        engine.request_again(obj, stage)
        self.report({"INFO"}, "Pulling %s" % stage.key)
        return {"FINISHED"}


class STAGELOOM_OT_freeze(bpy.types.Operator):
    bl_idname = "stageloom.freeze"
    bl_label = "Freeze"
    bl_description = "Freeze this stage: no requests until unfrozen; the output stays"
    bl_options = {"REGISTER", "UNDO"}

    index: IntProperty(name="Index", default=-1)

    def execute(self, context):
        obj, _record, stage = _stage(context, self.index)
        if stage is None:
            return {"CANCELLED"}
        if stage.frozen:
            stage.frozen = False
            engine.unfreeze(obj, stage)
            self.report({"INFO"}, "Unfroze %s" % stage.key)
        else:
            stage.frozen = True
            engine.freeze(obj, stage)
            self.report({"INFO"}, "Froze %s" % stage.key)
        return {"FINISHED"}


class STAGELOOM_OT_key_all(bpy.types.Operator):
    bl_idname = "stageloom.key_all"
    bl_label = "Key all"
    bl_description = "Key every parameter of this stage at the current frame"
    bl_options = {"REGISTER", "UNDO"}

    index: IntProperty(name="Index", default=-1)

    def execute(self, context):
        obj, _record, stage = _stage(context, self.index)
        if stage is None:
            return {"CANCELLED"}
        keyed = props.key_all(obj, stage, context.scene.frame_current)
        if not keyed:
            self.report({"WARNING"}, "nothing animatable to key")
            return {"CANCELLED"}
        self.report({"INFO"}, "Keyed %d parameter(s)" % keyed)
        return {"FINISHED"}


class STAGELOOM_OT_purge_orphans(bpy.types.Operator):
    bl_idname = "stageloom.purge_orphans"
    bl_label = "Purge orphans"
    bl_description = "Delete parameter properties Grasshopper no longer declares, with their keyframes"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        obj = context.object
        record = stack_mod.stack_of(obj) if obj is not None else None
        if record is None:
            return {"CANCELLED"}
        removed = 0
        for stage in record.stages:
            removed += props.purge_orphans(obj, stage)
        if not removed:
            return {"CANCELLED"}
        self.report({"INFO"}, "Purged %d orphan(s)" % removed)
        return {"FINISHED"}


class STAGELOOM_OT_copy_key(bpy.types.Operator):
    bl_idname = "stageloom.copy_key"
    bl_label = "Copy stage key"
    bl_description = "Copy this stage's key, to paste into the Grasshopper components"

    index: IntProperty(name="Index", default=-1)

    def execute(self, context):
        _obj, _record, stage = _stage(context, self.index)
        if stage is None:
            return {"CANCELLED"}
        context.window_manager.clipboard = stage.key
        self.report({"INFO"}, "Copied %s" % stage.key)
        return {"FINISHED"}


class STAGELOOM_OT_start_server(bpy.types.Operator):
    bl_idname = "stageloom.start_server"
    bl_label = "Start server"
    bl_description = "Start (or restart) the WebSocket server"

    def execute(self, context):
        ok = engine.start_server()
        if ok:
            self.report({"INFO"}, "Stageloom server started")
            return {"FINISHED"}
        why = bridge.state.get("notice") or bridge.state.get("error") or "failed"
        self.report({"WARNING"}, "Stageloom server not started: %s" % why)
        return {"CANCELLED"}


def _safe_unregister(cls):
    try:
        bpy.utils.unregister_class(cls)
    except (RuntimeError, ValueError):
        pass


def register():
    for cls in (
        STAGELOOM_OT_add_stage,
        STAGELOOM_OT_push,
        STAGELOOM_OT_pull,
        STAGELOOM_OT_freeze,
        STAGELOOM_OT_key_all,
        STAGELOOM_OT_purge_orphans,
        STAGELOOM_OT_copy_key,
        STAGELOOM_OT_start_server,
    ):
        bpy.utils.register_class(cls)


def unregister():
    for cls in (
        STAGELOOM_OT_start_server,
        STAGELOOM_OT_copy_key,
        STAGELOOM_OT_purge_orphans,
        STAGELOOM_OT_key_all,
        STAGELOOM_OT_freeze,
        STAGELOOM_OT_pull,
        STAGELOOM_OT_push,
        STAGELOOM_OT_add_stage,
    ):
        _safe_unregister(cls)
