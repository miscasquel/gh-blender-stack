"""The N-panel: connection status, stages, parameters (plan §11, demo v0/S5).

Icons are Blender's own icon names, the same glyphs the Grasshopper components
carry (see grasshopper/src/gh/icons). Value rows follow Blender's own layout:
a split label/widget column, no icons on the values themselves, and the standard
property decorators.
"""

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
        row.operator("gh_stack.push", icon="FILE_REFRESH")
        row.operator("gh_stack.retry", icon="LOOP_FORWARDS")

        if obj.type == "MESH":
            row = layout.row()
            row.use_property_split = True
            row.prop(record, "subd", text="Base is a SubD cage")

        for index, stage in enumerate(record.stages):
            self.draw_stage(layout, obj, record, stage, index)

    def draw_stage(self, layout, obj, record, stage, index):
        box = layout.box()
        row = box.row()
        row.prop(stage, "enabled", text="")
        row.label(text=stage.label or stage.key, icon="GEOMETRY_NODES")
        row.label(text=_state_text(stage), icon=_state_icon(stage))
        row.operator("gh_stack.copy_key", text="", icon="COPYDOWN").index = index
        row.operator("gh_stack.remove_stage", text="", icon="TRASH").index = index

        row = box.row()
        row.label(text="Key")
        row.label(text=stage.key)
        if bridge.state.get("notice"):
            box.label(text=bridge.state["notice"], icon="INFO")
        if stage.error:
            box.label(text=stage.error, icon="ERROR")
        for line in (stage.warnings or "").split("\n"):
            if line:
                box.label(text=line, icon="INFO")

        if stage.params:
            params_box = box.box()
            params_box.label(text="Parameters", icon="DRIVER")
            # Group labels come from the Param component's Group input; parameters
            # with no group sit directly under the header, in schema order.
            groups = []
            for entry in stage.params:
                group = entry.group or ""
                if group not in groups:
                    groups.append(group)
            for group in groups:
                if group:
                    group_box = params_box.box()
                    group_box.label(text=group)
                    for entry in stage.params:
                        if (entry.group or "") == group:
                            self.draw_param(group_box, obj, stage, entry)
                else:
                    for entry in stage.params:
                        if not entry.group:
                            self.draw_param(params_box, obj, stage, entry)

    def draw_param(self, layout, obj, stage, entry):
        name = stack_mod.param_prop_name(stage.key, entry.param_id)
        if name not in obj:
            return
        # Blender's own layout: a label column and a widget column, aligned, with
        # the property decorators (keyframe diamond, driver badge) on the widget.
        col = layout.column()
        col.use_property_split = True
        col.use_property_decorate = True
        if entry.type == "value_list" and entry.values:
            # A dropdown over the custom property, which stays the real value
            # (keyframable, and what the engine sends). The index is clamped:
            # assigning an out-of-range identifier raises inside the draw.
            labels = [c for c in entry.values.split("\n") if c]
            index = int(obj[name])
            if index < 0 or index >= len(labels):
                index = 0
                obj[name] = 0
            current = str(index)
            if entry.value_index != current:
                try:
                    entry.value_index = current
                except TypeError:
                    pass
            col.prop(entry, "value_index", text=entry.label or entry.param_id)
        else:
            # Custom properties are addressed with the bracket syntax in the UI;
            # a bare name (or a slash path) does not resolve.
            col.prop(obj, '["%s"]' % name, text=entry.label or entry.param_id)


class GHSTACK_PT_diagnostics(bpy.types.Panel):
    """What the add-on actually received, for when the panel and GH disagree.
    Collapsed by default; Blender-style child panel."""

    bl_label = "Diagnostics"
    bl_idname = "GHSTACK_PT_diagnostics"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "GH Stack"
    bl_parent_id = "GHSTACK_PT_panel"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        state = bridge.state
        layout.label(text="connected: %s" % ("yes" if state["connected"] else "no"))
        layout.label(text="doc: %s" % (state["doc"] or "-"))
        if state.get("error"):
            layout.label(text=state["error"], icon="ERROR")

        obj = context.object
        record = stack_mod.stack_of(obj) if obj is not None else None
        if record is None or not record.stages:
            layout.label(text="no stage on this object")
            return
        for stage in record.stages:
            box = layout.box()
            box.label(text="%s (%s)" % (stage.key, stage.state))
            box.label(text="rid: %s" % (stage.rid[-16:] or "-"))
            if stage.error:
                box.label(text=stage.error, icon="ERROR")
            for entry in stage.params:
                detail = entry.type
                if entry.type == "value_list":
                    detail += " (%d values)" % len([c for c in entry.values.split("\n") if c])
                box.label(text="%s: %s" % (entry.label or entry.param_id, detail))


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


def _safe_unregister(cls):
    """Unregister a class that may already be gone.

    Disabling an add-on can race a partially registered state (for example when
    an extension replaces a development install); an unregister that raises
    leaves the rest of the module registered.
    """
    try:
        bpy.utils.unregister_class(cls)
    except (RuntimeError, ValueError):
        pass


def register():
    bpy.utils.register_class(GHSTACK_PT_panel)
    bpy.utils.register_class(GHSTACK_PT_diagnostics)


def unregister():
    _safe_unregister(GHSTACK_PT_diagnostics)
    _safe_unregister(GHSTACK_PT_panel)
