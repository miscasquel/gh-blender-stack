"""Operators: Add Stage, Remove Stage, Push, server control (plan §9.8, demo v0)."""

import bpy
from bpy.props import IntProperty, StringProperty

from . import engine, helpers, nodes, stack as stack_mod

STACK_TYPES = {"MESH", "CURVE", "POINTCLOUD", "SURFACE"}


class GHSTACK_OT_add_stage(bpy.types.Operator):
    bl_idname = "gh_stack.add_stage"
    bl_label = "Add GH Stage"
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
        engine.push(obj)
        self.report({"INFO"}, "Added stage %s" % stage.key)
        return {"FINISHED"}


class GHSTACK_OT_remove_stage(bpy.types.Operator):
    bl_idname = "gh_stack.remove_stage"
    bl_label = "Remove GH Stage"
    bl_description = "Remove this Grasshopper stage, its helper and its parameters"
    bl_options = {"REGISTER", "UNDO"}

    index: IntProperty(name="Index", default=-1)

    def execute(self, context):
        obj = context.object
        record = stack_mod.stack_of(obj) if obj is not None else None
        if record is None or not (0 <= self.index < len(record.stages)):
            return {"CANCELLED"}
        stage = record.stages[self.index]
        nodes.remove_modifier(obj, stage.key)
        helpers.remove_helper(obj, stage.key)
        stack_mod.clear_stage_params(obj, stage.key)
        record.stages.remove(self.index)
        obj.update_tag()
        return {"FINISHED"}


class GHSTACK_OT_push(bpy.types.Operator):
    bl_idname = "gh_stack.push"
    bl_label = "Push"
    bl_description = "Send the current state to Grasshopper again"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        engine.push(context.object)
        return {"FINISHED"}


class GHSTACK_OT_copy_key(bpy.types.Operator):
    bl_idname = "gh_stack.copy_key"
    bl_label = "Copy stage key"
    bl_description = "Copy this stage's key, to paste into the Grasshopper components"

    index: IntProperty(name="Index", default=-1)

    def execute(self, context):
        obj = context.object
        record = stack_mod.stack_of(obj) if obj is not None else None
        if record is None or not (0 <= self.index < len(record.stages)):
            return {"CANCELLED"}
        key = record.stages[self.index].key
        context.window_manager.clipboard = key
        self.report({"INFO"}, "Copied %s" % key)
        return {"FINISHED"}


class GHSTACK_OT_start_server(bpy.types.Operator):
    bl_idname = "gh_stack.start_server"
    bl_label = "Start server"
    bl_description = "Start (or restart) the WebSocket server"

    def execute(self, context):
        ok = engine.start_server()
        self.report({"INFO"} if ok else {"ERROR"}, "GH Stack server " + ("started" if ok else "failed"))
        return {"FINISHED"} if ok else {"CANCELLED"}


def _safe_unregister(cls):
    try:
        bpy.utils.unregister_class(cls)
    except (RuntimeError, ValueError):
        pass


def register():
    for cls in (
        GHSTACK_OT_add_stage,
        GHSTACK_OT_remove_stage,
        GHSTACK_OT_push,
        GHSTACK_OT_copy_key,
        GHSTACK_OT_start_server,
    ):
        bpy.utils.register_class(cls)


def unregister():
    for cls in (
        GHSTACK_OT_start_server,
        GHSTACK_OT_copy_key,
        GHSTACK_OT_push,
        GHSTACK_OT_remove_stage,
        GHSTACK_OT_add_stage,
    ):
        _safe_unregister(cls)
