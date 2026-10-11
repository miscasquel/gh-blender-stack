"""The engine: one tick per timer pass, per stack object (plan §9).

Demo v0 scope: the prefix geometry of a stage is the stack object's base mesh
(mid-stack composition and clone-based prefix evaluation come later). Requests,
responses, the in-memory cache, states and timeouts follow the protocol's apply
rules.

Nothing here touches the socket directly: it posts frames through the bridge and
drains what the server thread decoded.
"""

import time

import bpy

from ..core import codec, hashing, rules
from . import bridge, geometry_io, helpers, nodes, props, server, stack as stack_mod

WAIT_INTERVAL = 0.005
IDLE_INTERVAL = 0.05

_cache = {}  # (stack_id, stage_key) -> {rid: (header, sections)}
_waiting_since = {}  # (stack_id, stage_key) -> monotonic seconds
_dirty = set()  # object names that need a pass
_registered = False


# --------------------------------------------------------------------- triggers


def _mark_dirty_all():
    for obj in bpy.data.objects:
        if stack_mod.stack_of(obj) is not None and len(obj.gh_stack.stages):
            _dirty.add(obj.name)


def _on_depsgraph(scene, depsgraph=None):
    _mark_dirty_all()


def _on_frame_change(scene, depsgraph=None):
    _mark_dirty_all()


# ------------------------------------------------------------------------ tick


def _timer():
    """bpy.app.timers callback. Returns the next interval.

    A timer that raises is unregistered by Blender, which would silently stop the
    engine; every part of the pass is therefore guarded, and failures land in the
    panel state instead.
    """
    try:
        _handle_incoming()
        busy = False
        for name in list(_dirty):
            obj = bpy.data.objects.get(name)
            if obj is None:
                _dirty.discard(name)
                continue
            try:
                if _process_object(obj):
                    busy = True
            except Exception as exc:
                bridge.state["error"] = "engine error on %s: %r" % (name, exc)
                import traceback

                print("[gh-stack] engine error:\n%s" % traceback.format_exc(), flush=True)
            _dirty.discard(name)
        return WAIT_INTERVAL if busy else IDLE_INTERVAL
    except Exception as exc:
        bridge.state["error"] = "engine error: %r" % (exc,)
        import traceback

        print("[gh-stack] engine error:\n%s" % traceback.format_exc(), flush=True)
        return IDLE_INTERVAL


def _handle_incoming():
    for header, sections in bridge.drain_incoming():
        mtype = header.get("type")
        if mtype == "schema":
            _handle_schema(header)
        elif mtype == "response":
            _handle_response(header, sections)
        elif mtype == "hello":
            pass  # connection state already set by the server thread


def _handle_schema(header):
    key = header.get("stage")
    params = header.get("params") or []
    matched = False
    for obj in bpy.data.objects:
        record = stack_mod.stack_of(obj)
        if record is None:
            continue
        stage = stack_mod.stage_of(obj, key)
        if stage is not None:
            matched = True
            try:
                # The plugin re-publishes on a heartbeat; only touch the UI when
                # the schema actually changed.
                if props.schema_signature(stage) == props.incoming_signature(params):
                    continue
                props.apply_schema(obj, stage, params)
            except Exception as exc:
                # One bad schema must not take the engine down.
                bridge.state["error"] = "schema for %s failed: %r" % (key, exc)
                import traceback

                print("[gh-stack] schema error:\n%s" % traceback.format_exc(), flush=True)
                continue
            _dirty.add(obj.name)
            obj.update_tag()
    if not matched:
        # A schema for a stage no object has: usually a key pasted into GH that
        # does not belong to any stack, or a stage removed in Blender.
        print("[gh-stack] schema for unknown stage %r ignored" % (key,), flush=True)


def _handle_response(header, sections):
    key = header.get("stage")
    rid = header.get("rid")
    for obj in bpy.data.objects:
        record = stack_mod.stack_of(obj)
        if record is None:
            continue
        stage = stack_mod.stage_of(obj, key)
        if stage is None:
            continue
        cache = _cache.setdefault((record.stack_id, key), {})
        cache[rid] = (header, sections)

        if header.get("status") == "error":
            stage.state = stack_mod.STATE_ERROR
            error = header.get("error") or {}
            stage.error = error.get("message") if isinstance(error, dict) else str(error)
            _waiting_since.pop((record.stack_id, key), None)
            obj.update_tag()
            return

        if rid != stage.rid:
            # Stale response: cached under its own rid, not applied (protocol §6.4).
            return
        out_hash = header.get("out_hash") or ""
        if out_hash == stage.applied_out_hash:
            stage.state = stack_mod.STATE_OK
            stage.error = ""
            _waiting_since.pop((record.stack_id, key), None)
            return
        _apply(obj, stage, sections)
        stage.applied_out_hash = out_hash
        stage.state = stack_mod.STATE_OK
        stage.error = ""
        _waiting_since.pop((record.stack_id, key), None)
        obj.update_tag()
        return


def _apply(obj, stage, sections):
    """Write the response geometry into the stage's helper mesh."""
    import numpy as np

    helper = helpers.ensure_helper(obj, stage.key)
    mesh = helper.data
    mesh_sections = [s for s in sections.items() if s[0].startswith("mesh.")]
    if not mesh_sections:
        mesh.clear_geometry()
        helper.update_tag()
        return
    prepared = []
    for name, values in sections.items():
        arr = np.asarray(values)
        if name.endswith(".verts"):
            arr = arr.reshape(-1, 3)
        elif name.endswith(".colors"):
            arr = arr.reshape(-1, 4)
        elif name.endswith(".crease_edges"):
            arr = arr.reshape(-1, 2)
        prepared.append((name, _dtype_of(name), arr))
    geometry_io.sections_to_mesh(mesh, prepared)
    helper.update_tag()


def _dtype_of(name):
    if name.endswith(".colors"):
        return "u8"
    if name.endswith(".verts") or name.endswith(".crease_values"):
        return "f32"
    return "u32"


# --------------------------------------------------------------------- process


def _process_object(obj):
    record = stack_mod.stack_of(obj)
    if record is None or not record.stages:
        return False
    if not record.stack_id:
        record.stack_id = stack_mod.ensure_stack(obj).stack_id
    busy = False
    for stage in record.stages:
        if _process_stage(obj, record, stage):
            busy = True
    return busy


def _process_stage(obj, record, stage):
    if not stage.enabled:
        stage.state = "disabled"
        nodes.set_modifier_visible(obj, stage.key, False)
        return False
    nodes.set_modifier_visible(obj, stage.key, True)

    if not bridge.state["connected"]:
        stage.state = stack_mod.STATE_DISCONNECTED
        return False

    sections = geometry_io.mesh_to_sections(obj.data)
    raw = {name: codec.section_bytes(name, dtype, values) for name, dtype, values in sections}
    geom_hash = hashing.geometry_hash(raw)
    params_json = props.canonical_json(obj, stage)
    rid = hashing.request_id(stage.key, params_json, geom_hash, "m", "stack-local", stage.nonce)

    cache = _cache.setdefault((record.stack_id, stage.key), {})
    if rid in cache:
        header, cached_sections = cache[rid]
        if (header.get("out_hash") or "") != stage.applied_out_hash:
            _apply(obj, stage, cached_sections)
            stage.applied_out_hash = header.get("out_hash") or ""
        stage.rid = rid
        stage.state = stack_mod.STATE_OK
        return False

    waiting_key = (record.stack_id, stage.key)
    since = _waiting_since.get(waiting_key)
    if stage.state == stack_mod.STATE_WAITING and since is not None:
        timeout = _timeout()
        if time.monotonic() - since > timeout:
            stage.state = stack_mod.STATE_ERROR
            stage.error = "timed out after %.0f s" % timeout
            return False
        return True  # still waiting; keep the fast timer

    mode = "full" if geom_hash != stage.geom_hash else "ref"
    header = {
        "type": "request",
        "stage": stage.key,
        "rid": rid,
        "nonce": stage.nonce,
        "params": props.cast_values(obj, stage),
        "geom": {"mode": mode, "hash": geom_hash, "subd": False},
        "units": "m",
        "space": "stack-local",
    }
    frame = codec.encode_frame(header, sections if mode == "full" else [])
    server.SERVER.send(frame)

    stage.rid = rid
    stage.geom_hash = geom_hash
    stage.state = stack_mod.STATE_WAITING
    stage.error = ""
    _waiting_since[waiting_key] = time.monotonic()
    return True


def _timeout():
    try:
        prefs = bpy.context.preferences.addons[__package__.split(".")[0]].preferences
        return float(prefs.timeout)
    except (KeyError, AttributeError):
        return 30.0


# ------------------------------------------------------------------- lifecycle


def start_server(port=None):
    if port is None:
        try:
            prefs = bpy.context.preferences.addons[__package__.split(".")[0]].preferences
            port = int(prefs.port)
        except (KeyError, AttributeError):
            port = 9130
    return server.SERVER.start(port)


def push(obj=None):
    """Force a new request for every stage of a stack (increments the nonce)."""
    obj = obj or bpy.context.object
    if obj is None:
        return
    record = stack_mod.stack_of(obj)
    if record is None:
        return
    for stage in record.stages:
        stage.nonce += 1
        _waiting_since.pop((record.stack_id, stage.key), None)
        stage.state = stack_mod.STATE_IDLE
    _dirty.add(obj.name)


def _mark_object_dirty(obj):
    _dirty.add(obj.name)


def register():
    global _registered
    if _registered:
        return
    # A UI-driven parameter write (the choice dropdown) must reach the engine.
    stack_mod.set_param_changed_hook(_mark_object_dirty)
    bpy.app.timers.register(_timer, first_interval=IDLE_INTERVAL)
    bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph)
    bpy.app.handlers.frame_change_post.append(_on_frame_change)
    _registered = True
    start_server()


def unregister():
    global _registered
    if not _registered:
        return
    stack_mod.set_param_changed_hook(None)
    if bpy.app.timers.is_registered(_timer):
        bpy.app.timers.unregister(_timer)
    for handler_list, handler in (
        (bpy.app.handlers.depsgraph_update_post, _on_depsgraph),
        (bpy.app.handlers.frame_change_post, _on_frame_change),
    ):
        if handler in handler_list:
            handler_list.remove(handler)
    _registered = False
