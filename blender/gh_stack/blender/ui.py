"""The N-panel: connection status, stages, parameters (plan §11, demo v0)."""

import bpy

from . import bridge, engine, stack as stack_mod


class GHSTACK_PT_panel(bpy.types.Panel):
    bl_label = "GH Stack"
    bl_idname = "GHSTACK_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "GH Stack"

    def draw(self, context):
        layout = self.layout
        state = bridge.state

        box = layout.box()
        row = box.row()
        if state["connected"]:
            row.label(text="Connected: %s" % (state["doc"] or "unnamed"), icon="LINKED")
        elif state["listening"]:
            row.label(text="Listening on port", icon="PLUGIN")
            row.label(text=str(_port()))
        else:
            row.label(text="Server not running", icon="UNLINKED")
        if state.get("error"):
            box.label(text=state["error"], icon="ERROR")

        obj = context.object
        record = stack_mod.stack_of(obj) if obj is not None else None
        if obj is None or record is None or not record.stages:
            layout.operator("gh_stack.add_stage", icon="ADD")
            layout.label(text="Select a mesh and add a stage", icon="INFO")
            return

        row = layout.row()
        row.operator("gh_stack.add_stage", icon="ADD")
        row.operator("gh_stack.push", icon="PLAY")

        for index, stage in enumerate(record.stages):
            self.draw_stage(layout, obj, record, stage, index)

    def draw_stage(self, layout, obj, record, stage, index):
        box = layout.box()
        row = box.row()
        row.prop(stage, "enabled", text="")
        row.label(text=stage.label or stage.key)
        row.label(text=_state_text(stage), icon=_state_icon(stage))
        row.operator("gh_stack.remove_stage", text="", icon="X").index = index

        row = box.row()
        row.label(text="Key")
        row.label(text=stage.key)
        if stage.error:
            box.label(text=stage.error, icon="ERROR")

        if stage.params:
            params_box = box.box()
            params_box.label(text="Parameters")
            for entry in stage.params:
                self.draw_param(params_box, obj, stage, entry)

    def draw_param(self, layout, obj, stage, entry):
        name = stack_mod.param_prop_name(stage.key, entry.param_id)
        if name not in obj:
            return
        row = layout.row()
        row.label(text=entry.label or entry.param_id)
        if entry.type == "choice" and entry.choices:
            row.label(text="(choice list: %s)" % entry.choices.replace("\n", ", "))
        else:
            # Custom properties are addressed with the bracket syntax in the UI;
            # a bare name (or a slash path) does not resolve.
            row.prop(obj, '["%s"]' % name, text="")


def _state_text(stage):
    return stage.state


def _state_icon(stage):
    return {
        stack_mod.STATE_OK: "CHECKMARK",
        stack_mod.STATE_WAITING: "TIME",
        stack_mod.STATE_ERROR: "ERROR",
        stack_mod.STATE_DISCONNECTED: "UNLINKED",
        "disabled": "CHECKBOX_DEHLT",
    }.get(stage.state, "DOT")


def _port():
    try:
        prefs = bpy.context.preferences.addons[__package__.split(".")[0]].preferences
        return prefs.port
    except (KeyError, AttributeError):
        return 9130


def register():
    bpy.utils.register_class(GHSTACK_PT_panel)


def unregister():
    bpy.utils.unregister_class(GHSTACK_PT_panel)
