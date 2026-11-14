"""Operators: Add Stage, Push, Pull, Freeze, Key all, Purge orphans, server
control.

Stage removal and enable/disable are native: deleting the stage's modifier
removes the stage, and the modifier's own visibility toggles are the switch. The add-on never writes those toggles after creation.
"""

import bpy
from bpy.props import IntProperty, StringProperty

from . import bridge, engine, helpers, nodes, props, stack as stack_mod

STACK_TYPES = {"MESH", "CURVE", "POINTCLOUD"}
SURFACE_MESSAGE = (
    "surface objects cannot hold a Stageloom stage in Blender 5.2 "
    "(a surface object cannot take a Geometry Nodes modifier); "
    "convert it to a mesh first (Object > Convert > Mesh)"
)


def _stage(context, index):
    """The (object, record, stage) the operator was pointed at, or Nones.

    The index is the row number in the panel, which lists stages in modifier
    stack order — the same order evaluation uses. The record keeps its own
    creation order, so an index into ``record.stages`` would address the wrong
    stage after a modifier drag.
    """
    obj = context.object
    record = stack_mod.stack_of(obj) if obj is not None else None
    stages = engine._ordered_stages(obj, record) if record is not None else []
    if not (0 <= index < len(stages)):
        return None, None, None
    return obj, record, stages[index]


class STAGELOOM_OT_add_stage(bpy.types.Operator):
    bl_idname = "stageloom.add_stage"
    bl_label = "Add Stageloom Stage"
    bl_description = "Add a Grasshopper stage to the active object"
    bl_options = {"REGISTER", "UNDO"}

    label: StringProperty(name="Label", default="Stage")

    def execute(self, context):
        obj = context.object
        if obj is not None and obj.type == "SURFACE":
            # Blender 5.2 surfaces cannot take a Geometry Nodes modifier, so a
            # stage has no anchor. Refuse before creating anything.
            self.report({"ERROR"}, SURFACE_MESSAGE)
            return {"CANCELLED"}
        if obj is None or obj.type not in STACK_TYPES:
            self.report({"ERROR"}, "select a mesh, curve or point cloud first")
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


class STAGELOOM_OT_bake(bpy.types.Operator):
    bl_idname = "stageloom.bake"
    bl_label = "Bake Stage"
    bl_description = (
        "Bake this stage's output into its native Geometry Nodes Bake node over "
        "the scene frame range. The per-frame gate waits for Grasshopper, so a "
        "stateful definition is stepped in order; renders then read the bake"
    )
    bl_options = {"REGISTER"}

    index: IntProperty(name="Index", default=-1)

    def execute(self, context):
        obj, _record, stage = _stage(context, self.index)
        if stage is None:
            return {"CANCELLED"}
        if not bpy.data.is_saved:
            self.report(
                {"ERROR"},
                "save the .blend before baking; Blender requires a saved file for bakes",
            )
            return {"CANCELLED"}
        scene = context.scene
        if scene.frame_start > scene.frame_end:
            self.report({"ERROR"}, "the scene frame range is reversed")
            return {"CANCELLED"}
        group = bpy.data.node_groups.get(nodes.group_name(stage.key))
        if group is None:
            self.report(
                {"ERROR"},
                "the stage's node group is missing; let the stage evaluate once "
                "before baking",
            )
            return {"CANCELLED"}
        modifier = nodes.modifier_of(obj, stage)
        if modifier is None:
            self.report({"ERROR"}, "this stage has no modifier")
            return {"CANCELLED"}
        if not nodes.modifier_active(modifier):
            self.report({"ERROR"}, "enable the stage modifier before baking")
            return {"CANCELLED"}
        modifier.node_group = group
        # The depsgraph creates the modifier's bake entry when it sees the Bake
        # node; force that before looking it up.
        context.view_layer.update()
        bake_node = next(
            (node for node in group.nodes if node.bl_idname == "GeometryNodeBake"),
            None,
        )
        entry = None
        if bake_node is not None:
            entry = next((item for item in modifier.bakes if item.node == bake_node), None)
        if entry is None:
            self.report({"ERROR"}, "the stage's Bake node is missing; rebuild the stage group")
            return {"CANCELLED"}

        entry.bake_mode = "ANIMATION"
        entry.use_custom_simulation_frame_range = True
        entry.frame_start = int(scene.frame_start)
        entry.frame_end = int(scene.frame_end)

        # A frozen stage would skip the per-frame gate, so a rebake has to thaw
        # it for the duration and freeze it again on success.
        was_frozen = bool(stage.frozen)
        if was_frozen:
            stage.frozen = False
            engine.unfreeze(obj, stage)

        # The native bake steps frames and fires frame_change_post. Arm the same
        # synchronous stage pass Play Every Frame uses; the Bake node then
        # caches the output of each frame, in order. While that job owns the
        # graph, do not build or inspect node groups from the per-frame apply:
        # nested Instance-on-Points chains crash Blender 5.3 if we do.
        nodes.hold_group_updates(True)
        engine._bake_sync = True
        for counter in ("playback_frames", "playback_synced", "playback_failures"):
            engine.stats[counter] = 0
        original = scene.frame_current
        context.view_layer.objects.active = obj
        obj.select_set(True)
        wm = context.window_manager
        total = int(scene.frame_end - scene.frame_start + 1)
        try:
            if wm is not None:
                # Blender's cursor progress number updates even while the
                # synchronous bake blocks the event loop.
                wm.progress_begin(0, total)
                engine._bake_progress = (wm, int(scene.frame_start), total)
            result = bpy.ops.object.geometry_node_bake_single(
                session_uid=getattr(obj, "session_uid", 0),
                modifier_name=modifier.name,
                bake_id=entry.bake_id,
            )
        except Exception as exc:
            result = {"CANCELLED"}
            self.report({"ERROR"}, "bake failed: %r" % (exc,))
        finally:
            engine._bake_progress = None
            if wm is not None:
                wm.progress_end()
            engine._bake_sync = False
            nodes.hold_group_updates(False)
            try:
                scene.frame_set(original)
            except Exception:
                pass
        if "FINISHED" not in result:
            if was_frozen:
                stage.frozen = True
                engine.freeze(obj, stage)
            return {"CANCELLED"}
        # The native cache is now the render source: stop asking Grasshopper
        # for every frame until the user clears the bake.
        stage.baked = True
        stage.frozen = True
        engine.freeze(obj, stage)
        stats = engine.stats
        target = entry.bake_target
        if target == "INHERIT":
            target = modifier.bake_target
        keep = "; save the .blend to keep the packed bake" if target == "PACKED" else ""
        self.report(
            {"INFO"},
            "Baked %d frame(s): %d ready, %d failure(s); stage frozen%s"
            % (
                stats["playback_frames"],
                stats["playback_synced"],
                stats["playback_failures"],
                keep,
            ),
        )
        return {"FINISHED"}


# Clear Bake deletes data Blender's undo does not restore. Keep the warning in
# one place so the panel and the operator tooltip cannot drift from the action.
CLEAR_BAKE_WARNING = "Bake data cannot be undone"
CLEAR_BAKE_CONFIRM_TITLE = "Clear Bake"
CLEAR_BAKE_CONFIRM_MESSAGE = (
    "Delete this stage's native bake data? It cannot be recovered; the stage "
    "will resume live Grasshopper output"
)
CLEAR_BAKE_CONFIRM_TEXT = "Delete Bake"


def _native_bake_entry(modifier):
    """The modifier's native bake record for its Bake node, if it has one.

    Blender creates this entry for every Bake node as soon as the depsgraph sees
    the group, so finding one is not proof that it holds baked data. The
    add-on's own bakes are recorded on ``stage.baked``; this entry is also what
    the delete operator addresses.
    """
    group = modifier.node_group if modifier is not None else None
    if group is None:
        return None
    bake_node = next(
        (node for node in group.nodes if node.bl_idname == "GeometryNodeBake"),
        None,
    )
    if bake_node is None:
        return None
    return next((item for item in modifier.bakes if item.node == bake_node), None)


def _native_bake_has_data(entry):
    """Whether a native bake entry points at data the add-on did not write.

    A disk bake records its directory on the entry; a packed bake exposes no
    "is baked" flag. The add-on's own packed bakes are covered by
    ``stage.baked``, which the Bake operator sets.
    """
    if entry is None:
        return False
    if getattr(entry, "directory", ""):
        return True
    return len(getattr(entry, "data_blocks", ())) > 0


def clear_bake_needs_confirmation(stage, entry):
    """Whether Clear Bake has bake data to delete, and so must confirm first.

    A bare native entry is metadata, not data: every stage with a Bake node has
    one, baked or not. Prompting on its presence alone would put a dialog in
    front of every no-op clear, which is why the add-on's ``baked`` flag (and a
    native disk bake) decide instead.
    """
    if stage is not None and stage.baked:
        return True
    return _native_bake_has_data(entry)


class STAGELOOM_OT_clear_bake(bpy.types.Operator):
    bl_idname = "stageloom.clear_bake"
    bl_label = "Clear Bake"
    bl_description = (
        "Delete this stage's native Bake data and resume live Grasshopper. "
        "The Bake node then passes the live stage output through again. "
        + CLEAR_BAKE_WARNING + "."
    )
    bl_options = {"REGISTER"}

    index: IntProperty(name="Index", default=-1)

    def invoke(self, context, event):
        obj, _record, stage = _stage(context, self.index)
        entry = None
        if stage is not None and not stage.baked:
            modifier = nodes.modifier_of(obj, stage)
            if modifier is not None:
                # The depsgraph creates the modifier's bake entry when it sees
                # the Bake node; force that before deciding.
                context.view_layer.update()
                entry = _native_bake_entry(modifier)
        if not clear_bake_needs_confirmation(stage, entry):
            # No bake to lose: a no-op clear must not ask a question.
            return self.execute(context)
        return context.window_manager.invoke_confirm(
            self,
            event,
            title=CLEAR_BAKE_CONFIRM_TITLE,
            message=CLEAR_BAKE_CONFIRM_MESSAGE,
            confirm_text=CLEAR_BAKE_CONFIRM_TEXT,
        )

    def execute(self, context):
        obj, _record, stage = _stage(context, self.index)
        if stage is None:
            return {"CANCELLED"}
        modifier = nodes.modifier_of(obj, stage)
        entry = None
        if modifier is not None:
            # The depsgraph creates the modifier's bake entry when it sees the
            # Bake node; force that before looking it up.
            context.view_layer.update()
            entry = _native_bake_entry(modifier)
        if modifier is not None and entry is not None:
            context.view_layer.objects.active = obj
            obj.select_set(True)
            try:
                result = bpy.ops.object.geometry_node_bake_delete_single(
                    session_uid=getattr(obj, "session_uid", 0),
                    modifier_name=modifier.name,
                    bake_id=entry.bake_id,
                )
            except Exception as exc:
                self.report({"ERROR"}, "could not delete the native bake: %r" % (exc,))
                return {"CANCELLED"}
            if "FINISHED" not in result:
                self.report({"ERROR"}, "Blender did not delete the native bake")
                return {"CANCELLED"}

        stage.baked = False
        stage.frozen = False
        engine.unfreeze(obj, stage)
        self.report(
            {"INFO"},
            "Cleared the stage bake; live Grasshopper resumed; save the .blend "
            "to keep the cleared state",
        )
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
        STAGELOOM_OT_bake,
        STAGELOOM_OT_clear_bake,
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
        STAGELOOM_OT_bake,
        STAGELOOM_OT_clear_bake,
        STAGELOOM_OT_freeze,
        STAGELOOM_OT_pull,
        STAGELOOM_OT_push,
        STAGELOOM_OT_add_stage,
    ):
        _safe_unregister(cls)
