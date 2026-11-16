"""Operators: Add Stage, Push, Pull, Resolve duplicate key, Freeze, Key all,
Purge orphans, server control.

Stage removal and enable/disable are native: deleting the stage's modifier
removes the stage, and the modifier's own visibility toggles are the switch. The add-on never writes those toggles after creation.
"""

import secrets

import bpy
from bpy.props import IntProperty, StringProperty

from . import bridge, engine, helpers, nodes, props, stack as stack_mod
from .engine import bake as bake_mod
from .engine.dependencies import stage_dependencies, stage_dependents
from .registration import register_classes, unregister_classes

STACK_TYPES = {"MESH", "CURVE", "POINTCLOUD"}
# States a bake may start from for the selected stage and its upstream chain.
# Idle settles during the bake gate and frozen/disabled stages are static by
# design; anything else cannot give the native bake a stable input.
BAKE_READY_STATES = frozenset(
    (
        stack_mod.STATE_IDLE,
        stack_mod.STATE_OK,
        stack_mod.STATE_FROZEN,
        stack_mod.STATE_DISABLED,
    )
)
SURFACE_MESSAGE = (
    "surface objects cannot hold a Stageloom stage in Blender 5.2 "
    "(a surface object cannot take a Geometry Nodes modifier); "
    "convert it to a mesh first (Object > Convert > Mesh)"
)
LINKED_MESSAGE = (
    "this object is linked from a library; edit the original .blend to change "
    "its Stageloom stages"
)
STAGE_MESSAGE = "select an object with a Stageloom stage"


def _is_stack_object(obj):
    return obj is not None and getattr(obj, "type", None) in STACK_TYPES


def _linked(obj):
    return getattr(obj, "library", None) is not None


def _editable_stack(context):
    """The context object with a non-empty stack, or None.

    Polls use this so F3 and a stale panel row report a clear refusal instead
    of executing against a missing stage or an object with no stack.
    """
    obj = getattr(context, "object", None)
    if not _is_stack_object(obj):
        return None
    record = stack_mod.stack_of(obj)
    if record is None or not len(record.stages):
        return None
    return obj


class StageOperatorMixin:
    """Poll for operators that address a row in the panel's stage list."""

    @classmethod
    def poll(cls, context):
        if _editable_stack(context) is None:
            cls.poll_message_set(STAGE_MESSAGE)
            return False
        return True


class EditableStageOperatorMixin(StageOperatorMixin):
    """A stage operator that writes: refuse a linked-library object.

    Its stage record belongs to the library, so any write would be lost or
    rejected by Blender; the panel's action must be disabled instead.
    """

    @classmethod
    def poll(cls, context):
        if not super().poll(context):
            return False
        if _linked(context.object):
            cls.poll_message_set(LINKED_MESSAGE)
            return False
        return True


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

    label: StringProperty(name="Label", default="")

    @classmethod
    def poll(cls, context):
        obj = getattr(context, "object", None)
        if obj is None:
            cls.poll_message_set("select a mesh, curve or point cloud first")
            return False
        if _linked(obj):
            cls.poll_message_set(LINKED_MESSAGE)
            return False
        if getattr(obj, "type", None) == "SURFACE":
            cls.poll_message_set(SURFACE_MESSAGE)
            return False
        if getattr(obj, "type", None) not in STACK_TYPES:
            cls.poll_message_set("select a mesh, curve or point cloud first")
            return False
        return True

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
        if _linked(obj):
            self.report({"ERROR"}, LINKED_MESSAGE)
            return {"CANCELLED"}
        record = stack_mod.ensure_stack(obj)
        if record is None:
            self.report({"ERROR"}, "this object cannot hold a stack")
            return {"CANCELLED"}
        label = (self.label or "").strip() or stack_mod.unique_stage_label(obj)
        stage = record.stages.add()
        stage.key = stack_mod.new_stage_key(label)
        stage.label = label
        helpers.ensure_helpers(obj, stage.key)
        nodes.ensure_modifier(obj, stage)
        obj.update_tag()
        engine.request_again(obj, stage)
        self.report({"INFO"}, "Added stage %s" % stage.key)
        return {"FINISHED"}


class STAGELOOM_OT_push(EditableStageOperatorMixin, bpy.types.Operator):
    bl_idname = "stageloom.push"
    bl_label = "Push"
    bl_description = "Send the current state to Grasshopper again"
    # The engine's request/freeze bookkeeping is not part of Blender's undo
    # snapshot, so an undo of this operator would not put the engine back.
    bl_options = {"REGISTER"}

    def execute(self, context):
        engine.push(context.object)
        return {"FINISHED"}


class STAGELOOM_OT_pull(EditableStageOperatorMixin, bpy.types.Operator):
    bl_idname = "stageloom.pull"
    bl_label = "Pull"
    bl_description = "Ask Grasshopper for this stage's output again"
    # Engine state (pending request, pull permit) is not undoable.
    bl_options = {"REGISTER"}

    index: IntProperty(name="Index", default=-1)

    def execute(self, context):
        obj, _record, stage = _stage(context, self.index)
        if stage is None:
            return {"CANCELLED"}
        # A fresh request: this is also how a frozen stage is refreshed once,
        # on demand.
        engine.request_again(obj, stage)
        self.report({"INFO"}, "Pulling %s" % stage.key)
        return {"FINISHED"}


class STAGELOOM_OT_resolve_duplicate_key(
    EditableStageOperatorMixin, bpy.types.Operator
):
    bl_idname = "stageloom.resolve_duplicate_key"
    bl_label = "Resolve duplicate key"
    bl_description = (
        "Give this stage a new key, leaving the other claimant on the old one. "
        "Parameters and their animation move with the stage"
    )
    bl_options = {"REGISTER", "UNDO"}

    index: IntProperty(name="Index", default=-1)

    def execute(self, context):
        obj, record, stage = _stage(context, self.index)
        if stage is None:
            return {"CANCELLED"}
        old_key = stage.key
        new_key = engine.resolve_duplicate_key(obj, record, stage)
        if new_key is None:
            self.report({"WARNING"}, "this stage key is not in conflict")
            return {"CANCELLED"}
        self.report({"INFO"}, "Resolved %s as %s" % (old_key, new_key))
        return {"FINISHED"}


class STAGELOOM_OT_freeze(EditableStageOperatorMixin, bpy.types.Operator):
    bl_idname = "stageloom.freeze"
    bl_label = "Freeze"
    bl_description = "Freeze this stage: no requests until unfrozen; the output stays"
    # Freezing also parks engine session state that undo cannot restore.
    bl_options = {"REGISTER"}

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


class STAGELOOM_OT_key_all(EditableStageOperatorMixin, bpy.types.Operator):
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


class STAGELOOM_OT_purge_orphans(EditableStageOperatorMixin, bpy.types.Operator):
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


def _stage_pending(obj, stage):
    """Whether the stage is waiting for an answer to its current request."""
    record = stack_mod.stack_of(obj)
    if record is None:
        return False
    return bool(engine._pending.get((record.stack_id, stage.key)))


def _bake_blocker(obj, stage):
    """Why a bake cannot start from this stage, or None when it is ready."""
    if stage.state not in BAKE_READY_STATES:
        return stage.state
    if _stage_pending(obj, stage):
        return "request pending"
    return None


def _unfreeze_bake_token(token):
    """Unfreeze exactly the stages this bake token froze; return the count.

    The marker is the only proof that the bake froze a stage: a stage the user
    had frozen before the bake carries no marker and is left alone. A stage
    that has since gained its own bake keeps that bake and stays frozen; only
    the marker from this token is dropped.
    """
    return bake_mod.release_token(token)


def _delete_native_bake(context, obj, modifier, entry):
    """Delete a modifier's native bake; return an error string, or None."""
    if modifier is None or entry is None:
        return None
    context.view_layer.objects.active = obj
    obj.select_set(True)
    try:
        result = bpy.ops.object.geometry_node_bake_delete_single(
            session_uid=getattr(obj, "session_uid", 0),
            modifier_name=modifier.name,
            bake_id=entry.bake_id,
        )
    except Exception as exc:
        return repr(exc)
    if "FINISHED" not in result:
        return "Blender did not delete it"
    return None


def _dependent_skip_reason(obj, stage, token):
    """Why a downstream stage cannot be triggered, or None when it can.

    A baked or disabled stage is already settled by something else, and a
    one-way stage (no Stage Out) has no answer to wait for. A stage frozen by
    another bake belongs to that bake: triggering it would change an output
    that bake promised to hold. A stage the user froze manually is not skipped:
    its Pull refreshes one frame and its freeze, without a marker, is kept.
    """
    if stage.baked:
        return "already baked"
    if not stage.enabled:
        return "disabled"
    modifier = nodes.modifier_of(obj, stage)
    if modifier is None or not nodes.modifier_active(modifier):
        return "disabled"
    if not stage.has_stage_out:
        return "one-way (no Stage Out)"
    marker = stage.frozen_by_bake
    if marker and marker != token:
        return "frozen by another bake"
    return None


def _trigger_dependents(pairs, token, frame):
    """Ask each downstream stage once, in dependency order, and freeze it.

    Returns ``(triggered, newly_frozen, skipped, failure)``. ``skipped`` is a
    list of ``(object, stage, reason)`` strings for the report and trace;
    ``failure`` is ``(object, stage, reason, frame)`` for the first dependent
    that could not answer, which the caller turns into a rollback. A
    user-frozen stage stays frozen and gets no marker; it is still asked so the
    frozen frame it keeps is the one that follows the target bake.
    """
    triggered = 0
    newly_frozen = 0
    skipped = []
    for dep_obj, dep_stage in pairs:
        reason = _dependent_skip_reason(dep_obj, dep_stage, token)
        if reason is not None:
            skipped.append((dep_obj, dep_stage, reason))
            line = "bake: skipped dependent %s/%s (%s)" % (
                dep_obj.name,
                dep_stage.key,
                reason,
            )
            if not engine._trace or engine._trace[-1] != line:
                engine._trace.append(line)
            continue
        user_frozen = bool(dep_stage.frozen)
        ok, why = bake_mod.trigger_once(dep_obj, dep_stage)
        if not ok:
            reason_text = why or "no answer"
            record = stack_mod.stack_of(dep_obj)
            if record is not None:
                engine._forget(record.stack_id, dep_stage.key)
                engine._clear_pull((record.stack_id, dep_stage.key))
            # A user-frozen stage stays frozen with the failure visible; any
            # other dependent lands in error. Rollback below unfreezes the
            # stages this bake froze, so no dependent is left half-asked.
            engine._set(
                dep_stage,
                "state",
                engine.stack_mod.STATE_FROZEN
                if dep_stage.frozen
                else engine.stack_mod.STATE_ERROR,
            )
            engine._set(dep_stage, "error", reason_text)
            line = "bake: dependent %s/%s failed at frame %s: %s" % (
                dep_obj.name,
                dep_stage.key,
                frame,
                reason_text,
            )
            engine._trace.append(line)
            return triggered, newly_frozen, skipped, (
                dep_obj,
                dep_stage,
                reason_text,
                frame,
            )
        bake_mod.freeze_stage(dep_obj, dep_stage, token, mark=not user_frozen)
        if not user_frozen:
            newly_frozen += 1
        triggered += 1
        engine._trace.append(
            "bake: triggered dependent %s/%s at frame %s%s"
            % (
                dep_obj.name,
                dep_stage.key,
                frame,
                " (kept the user's freeze)" if user_frozen else "",
            )
        )
    return triggered, newly_frozen, skipped, None


class STAGELOOM_OT_bake(EditableStageOperatorMixin, bpy.types.Operator):
    bl_idname = "stageloom.bake"
    bl_label = "Bake Stage"
    bl_description = (
        "Bake this stage's output into its native Geometry Nodes Bake node over "
        "the scene frame range, freeze the stage and every upstream stage it "
        "depends on, then trigger each downstream dependent once and freeze "
        "its single frame. Only this stage gets bake data; a dependent that "
        "needs offline animation is baked in turn. The per-frame gate waits "
        "for Grasshopper during the bake, so a stateful definition is stepped "
        "in order; the frozen chain renders without a Grasshopper request"
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

        # Compute the freeze set before the bake, and refuse to start unless
        # every upstream stage can give it a stable input. An idle stage is
        # settled by the bake gate itself; waiting/errored/unbound stages cannot
        # be.
        upstream = stage_dependencies(obj, stage)
        blocked = []
        for candidate_obj, candidate_stage in [(obj, stage)] + upstream:
            reason = _bake_blocker(candidate_obj, candidate_stage)
            if reason is not None:
                blocked.append(
                    "%s/%s (%s)"
                    % (candidate_obj.name, candidate_stage.key, reason)
                )
        if blocked:
            self.report(
                {"ERROR"},
                "bake needs settled stages; not ready: %s" % ", ".join(blocked),
            )
            return {"CANCELLED"}

        entry.bake_mode = "ANIMATION"
        entry.use_custom_simulation_frame_range = True
        entry.frame_start = int(scene.frame_start)
        entry.frame_end = int(scene.frame_end)

        # A rebake replaces this stage's freeze set. Release the old set first:
        # those stages must be live again for this bake, and must not stay
        # frozen under a token nothing owns. The marker on this stage belongs to
        # another stage's bake; it is dropped only once this bake succeeds,
        # because until then that other bake still owns the freeze.
        was_frozen = bool(stage.frozen)
        prior_marker = stage.frozen_by_bake
        if stage.bake_token:
            _unfreeze_bake_token(stage.bake_token)
            stage.bake_token = ""
        # A frozen stage would skip the per-frame gate, so a rebake has to thaw
        # it for the duration and freeze it again on success.
        if was_frozen:
            stage.frozen = False
            engine.unfreeze(obj, stage)

        # The native bake steps frames and fires frame_change_post. Arm the same
        # synchronous stage pass Play Every Frame uses; the Bake node then
        # stores the output of each frame, in order. While that job owns the
        # graph, do not build or inspect node groups from the per-frame apply:
        # nested Instance-on-Points chains crash Blender 5.3 if we do.
        nodes.hold_group_updates(True)
        engine._bake_sync = True
        engine._bake_scene = scene.as_pointer()
        for counter in ("playback_frames", "playback_synced", "playback_failures"):
            engine.stats[counter] = 0
        original = scene.frame_current
        context.view_layer.objects.active = obj
        obj.select_set(True)
        wm = context.window_manager
        total = int(scene.frame_end - scene.frame_start + 1)
        failure = None
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
            failure = repr(exc)
        finally:
            engine._bake_progress = None
            if wm is not None:
                wm.progress_end()
            engine._bake_sync = False
            engine._bake_scene = None
            nodes.hold_group_updates(False)
            try:
                scene.frame_set(original)
            except Exception:
                pass
        stats = engine.stats
        if failure is None and "FINISHED" not in result:
            failure = "Blender did not finish the native bake"
        if failure is None and stats["playback_failures"]:
            # The operator can return FINISHED after a frame the gate could not
            # make ready; that bake is incomplete, not a success.
            failure = "%d frame(s) did not settle" % stats["playback_failures"]
        if failure is not None:
            # Leave neither a baked stage nor a frozen chain behind. The native
            # bake may have written some frames before it failed; the old bake
            # is not recoverable either way, so delete what is there.
            stage.baked = False
            stage.bake_token = ""
            stage.frozen = False
            if was_frozen:
                stage.frozen = True
                engine.freeze(obj, stage)
            deleted = _delete_native_bake(context, obj, modifier, entry)
            if deleted is not None:
                self.report(
                    {"ERROR"},
                    "bake failed: %s; no upstream stage was frozen; the partial "
                    "native bake could not be deleted: %s" % (failure, deleted),
                )
            else:
                self.report(
                    {"ERROR"},
                    "bake failed: %s; no upstream stage was frozen" % failure,
                )
            return {"CANCELLED"}
        # The native bake is now the render source: stop asking Grasshopper
        # for every frame until the user clears the bake. Upstream stages join
        # the freeze only now, after a complete bake; a stage the user had
        # already frozen keeps its own freeze and gets no marker. This stage now
        # owns its bake, so a marker from another bake is dropped: Clear Bake on
        # that other stage must not unfreeze this one.
        token = secrets.token_hex(8)
        stage.baked = True
        stage.frozen = True
        stage.bake_token = token
        stage.frozen_by_bake = ""
        engine.freeze(obj, stage)
        frozen_upstream = 0
        for upstream_obj, upstream_stage in upstream:
            if upstream_stage.frozen:
                continue
            bake_mod.freeze_stage(upstream_obj, upstream_stage, token)
            frozen_upstream += 1

        # Only this stage was baked. Every downstream consumer is asked once,
        # in dependency order, so it sees the baked output, then frozen on its
        # one-frame answer. It never gets native bake data of its own.
        dependents = stage_dependents(obj, stage)
        triggered, _frozen_downstream, skipped, dependent_failure = (
            _trigger_dependents(dependents, token, scene.frame_current)
        )
        if dependent_failure is not None:
            failure_obj, failure_stage, reason, frame = dependent_failure
            released = _unfreeze_bake_token(token)
            stage.baked = False
            stage.bake_token = ""
            stage.frozen = False
            if was_frozen:
                stage.frozen = True
                stage.frozen_by_bake = prior_marker
                engine.freeze(obj, stage)
            deleted = _delete_native_bake(context, obj, modifier, entry)
            tail = (
                "; the partial native bake could not be deleted: %s" % deleted
                if deleted is not None
                else "; the partial native bake was deleted"
            )
            self.report(
                {"ERROR"},
                "bake failed: dependent %s/%s did not answer at frame %s: %s; "
                "unfroze the %d stage(s) this bake froze%s"
                % (failure_obj.name, failure_stage.key, frame, reason, released, tail),
            )
            return {"CANCELLED"}

        target = entry.bake_target
        if target == "INHERIT":
            target = modifier.bake_target
        keep = "; save the .blend to keep the packed bake" if target == "PACKED" else ""
        skipped_note = ""
        if skipped:
            skipped_note = "; skipped %s" % ", ".join(
                "%s/%s (%s)" % (skip_obj.name, skip_stage.key, why)
                for skip_obj, skip_stage, why in skipped
            )
        one_way = [
            "%s/%s" % (skip_obj.name, skip_stage.key)
            for skip_obj, skip_stage, why in skipped
            if why.startswith("one-way")
        ]
        if one_way:
            self.report(
                {"WARNING"},
                "skipped one-way dependent(s) with no Stage Out: %s" % ", ".join(one_way),
            )
        self.report(
            {"INFO"},
            "Baked %d frame(s): %d ready, %d failure(s); stage and %d upstream "
            "stage(s) frozen; triggered/frozen %d/%d dependent(s)%s%s"
            % (
                stats["playback_frames"],
                stats["playback_synced"],
                stats["playback_failures"],
                frozen_upstream,
                triggered,
                len(dependents),
                skipped_note,
                keep,
            ),
        )
        return {"FINISHED"}


# Clear Bake deletes data Blender's undo does not restore. Keep the warning in
# one place so the panel and the operator tooltip cannot drift from the action.
CLEAR_BAKE_WARNING = "Bake data cannot be undone"
CLEAR_BAKE_CONFIRM_TITLE = "Clear Bake"
CLEAR_BAKE_CONFIRM_MESSAGE = (
    "Delete this stage's native bake data? It cannot be recovered; this stage "
    "and the stages this bake froze (upstream or downstream) resume live. "
    "Another stage's own bake is not touched"
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


class STAGELOOM_OT_clear_bake(EditableStageOperatorMixin, bpy.types.Operator):
    bl_idname = "stageloom.clear_bake"
    bl_label = "Clear Bake"
    bl_description = (
        "Delete only this stage's native Bake data and resume live Grasshopper. "
        "The freeze markers this bake created are released (upstream and "
        "downstream); another stage's own bake is never touched. The Bake node "
        "then passes the live stage output through again. "
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
        token = stage.bake_token
        had_bake = bool(stage.baked or token or _native_bake_has_data(entry))
        error = _delete_native_bake(context, obj, modifier, entry)
        if error is not None:
            self.report({"ERROR"}, "could not delete the native bake: %s" % error)
            return {"CANCELLED"}

        stage.baked = False
        stage.bake_token = ""
        # A stage the user froze by hand has no marker and no bake: this action
        # must not quietly thaw it. A stage with bake data heats up again, and
        # only this action's own freeze markers are released below.
        if had_bake:
            stage.frozen = False
            engine.unfreeze(obj, stage)
        unfrozen = _unfreeze_bake_token(token)
        self.report(
            {"INFO"},
            "Cleared the stage bake; %s%d stage(s) tagged by this bake unfrozen; "
            "live Grasshopper resumed; save the .blend to keep the cleared state"
            % ("stage resumed live and " if had_bake else "", unfrozen),
        )
        return {"FINISHED"}


class STAGELOOM_OT_copy_key(StageOperatorMixin, bpy.types.Operator):
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


_OPERATOR_CLASSES = (
    STAGELOOM_OT_add_stage,
    STAGELOOM_OT_push,
    STAGELOOM_OT_pull,
    STAGELOOM_OT_resolve_duplicate_key,
    STAGELOOM_OT_freeze,
    STAGELOOM_OT_bake,
    STAGELOOM_OT_clear_bake,
    STAGELOOM_OT_key_all,
    STAGELOOM_OT_purge_orphans,
    STAGELOOM_OT_copy_key,
    STAGELOOM_OT_start_server,
)


def register():
    register_classes(_OPERATOR_CLASSES)


def unregister():
    unregister_classes(reversed(_OPERATOR_CLASSES))
