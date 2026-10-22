"""The engine: one tick per timer pass, per stack object (plan §9, §7 frame cache).

One pass per stage: compute the prefix geometry, hash it, build the request id,
then serve the frame from the memory cache, the disk store, or Grasshopper. The
response store *is* the frame cache: keyframed parameters produce a different rid
per frame, so frames cached under their rids replay in any order, with no bake
operator and no frame range (the handoff, 2026-10-12).

A per-stage generation resets the cache when Grasshopper sends something that
makes old frames describe the wrong definition: a changed schema, a response that
contradicts the stored one for the same rid, a reconnect with a different
document, or a manual Push.

Cached frames replay with Grasshopper closed (renders use the cache). A frame
that is not cached keeps the last good output; a pass that changes nothing writes
nothing and tags nothing, so Cycles can accumulate samples while idle.

Nothing here touches the socket directly: it posts frames through the bridge and
drains what the server thread decoded.
"""

import sys
import time
from collections import deque

import bpy
import numpy as np

from ...core import attributes as attributes_mod
from ...core import cache as frame_cache
from ...core import codec, hashing
from .. import bridge, geometry_io, helpers, nodes, prefs, props, server, stack as stack_mod

WAIT_INTERVAL = 0.005
# The idle cadence: what a quiet session pays to notice an incoming message
# (a GH-side edit, a reconnect). 20 ms keeps those reactive without a tick
# budget worth measuring; the fast cadence below is what edits use.
IDLE_INTERVAL = 0.02
# While the scene is being edited the fast cadence applies instead of the idle
# one: an edit keeps the engine hot for HOT_SECONDS, so a reaction costs one tick
# rather than up to one idle interval. Playback fills frames this way — each
# answer re-dirties the stack and the next frame goes out at once — and a quiet
# scene costs nothing, because `_dirty` is emptied on every pass.
HOT_INTERVAL = 0.005
HOT_SECONDS = 1.0
# A request that is still outstanding after this long may be superseded when the
# scene has moved on (the pass asks with the current state). Grasshopper
# coalesces solves, so the newer ask does not build a backlog, and the answer
# arrives for the value the user is looking at instead of one round trip behind
# it. The hard `_timeout()` still covers an answer that never comes.
SOFT_RETRY_SECONDS = 1.0

_cache = {}  # (stack_id, stage_key) -> {rid: (raw frame, out_hash)}; the hot path
_cache_bytes = {}  # (stack_id, stage_key) -> bytes currently held by its cache
_stores = {}  # (stack_id, stage_key) -> ResponseStore; the backing store
_prefix_memo = {}  # (obj.as_pointer(), stage key) -> {"revision": tuple, "sections": list}
# One depsgraph evaluation can serve every stage prefix on an object. The batch
# is built on the first cache miss in a pass and cleared when that pass ends.
_prefix_batch = {}  # (obj.as_pointer(), stage key) -> (sections, attributes, definitions)
_prefix_batch_built = False
_prefix_batching = False
# (stack_id, stage_key) pairs allowed exactly one request while frozen: Pull.
# Transient session state, like `_pending` — never saved, cleared on register.
_pull_permits = set()
# The depsgraph generation: one counter, moved by every update Blender reports
# and by every engine write. A memoized prefix is only valid within the
# generation that produced it, so anything the depsgraph tags retires every
# memo. This is deliberately *one* counter and not a per-reference list: the
# Python API has no way to ask a node tree which datablocks it depends on, so a
# list of "what a prefix reads" is a hand-maintained description of the
# depsgraph, and a missing entry is silent stale geometry. Tagged is tagged.
_prefix_epoch = 0
# (stack_id, stage_key) pairs that must ask Grasshopper even when a cached frame
# answers the request id: a reconnect or a file load means the definition may
# have changed while the add-on was away, and the frame cache cannot know it.
# Consumed when the verification request is actually sent.
_verify = set()
# The last events the engine acted on, for the Diagnostics panel: a bounded ring
# of "what did it see, what did it do" lines. Never saved.
_trace = deque(maxlen=200)
# Trigger health, for the Diagnostics panel: when the depsgraph last reached
# `_on_depsgraph` (and how many entries that delivery carried), when a pass last
# sent a request, and when a response was last applied. Monotonic and
# session-only: the panel renders the ages ("trigger 0.4 s ago"), which is the
# one line that tells a user whether the engine heard Blender at all.
_heartbeat = {
    "trigger_at": None,
    "trigger_count": 0,
    "trigger_updates": 0,
    "sent_at": None,
    "applied_at": None,
}
# Why a stage's last pass did not send: (stack_id, stage_key) -> (reason, rid).
# A reason is re-recorded only when it changes or when the scene state moves on,
# so a pass that stays quiet for the same reason does not flood the ring.
_last_no_send = {}
# The last pass latencies per stage, in milliseconds:
# (stack_id, stage_key) -> {"prefix_ms", "encode_ms", "rtt_ms", "apply_ms"}.
# Diagnostics-only, deliberately not on the saveable stage record: one
# machine's timings must not travel inside a .blend.
_timings = {}
# (stack_id, stage_key) -> {rid: monotonic seconds it was sent}. At most one entry:
# the evaluation in flight. One at a time, deliberately — a frame that arrives while
# an answer is outstanding is not queued behind it, because Grasshopper solves in
# arrival order on one thread and a queue of frames the user has already scrubbed
# past only keeps the client busy after they have moved on. The frame the playhead
# is on is asked for on the first pass after the answer lands, and every answer is
# stored either way, so nothing evaluated is wasted.
_pending = {}
# (stack_id, stage_key) -> (rid, geometry hash) of the last request actually
# handed to a connected client. The stage record is saved *and undoable*: an
# undo restores `geom_hash`/`rid` from the undo step, which says nothing about
# what the client currently holds. This map is connection state, never saved:
# the cache may answer a request without asking only while the client has been
# told that exact request (see `_process_stage`), and the request mode is
# decided against the geometry named here, not against a rolled-back field.
_sent = {}
_dirty = set()  # object names that need a pass
# Stage and stack lookup, rebuilt on load/undo and patched as stages change.
# The handlers run on the main thread, so direct object references are safe;
# a rename follows the reference, unlike a name-keyed index.
_stage_index = {}  # stage_key -> [object]
_stack_objects = []  # every object with at least one stage
_index_dirty = True
_index_object_count = -1
# Object names with a request in flight, for the Diagnostics summary. Refreshed
# from `_pending` by the once-a-second stall scan; the scan itself walks the
# scene rather than trusting this set, because a rename between send and check
# would leave the old name in here (see `_nudge_stalled_waits`).
_waiting_objects = set()
_last_stall_check = 0.0
_registered = False
# Monotonic deadline until which the engine runs at the fast cadence; see
# HOT_INTERVAL. Advanced by every edit, response and frame change.
_hot_until = 0.0
# When each stage last asked Grasshopper something, so a wait that is really a
# fault is visible in the diagnostics panel: (stack_id, stage_key) -> monotonic.
_state_since = {}
# True while the engine evaluates a prefix clone. Linking and removing that
# clone tags the depsgraph; without the guard every pass would re-mark every
# stack dirty and the engine would never idle.
_evaluating = False
# Who set the guard, and when: written once per clone, so the watchdog at the
# top of `_timer` can name the object and stage that left it up without
# formatting a traceback per clone. `_ticking` tells the depsgraph handler
# whether the guard it sees belongs to the engine's own clone (a pass is
# running, the event is ours and expected to be eaten) or to a pass that died
# before it could clear it (the event is a real edit being dropped).
_guard_owner = None
_evaluating_since = None
_ticking = False
# Counters for tests and diagnostics: what the engine actually did, so a quiet
# idle can be asserted without guessing at Blender internals.
stats = {
    "passes": 0,
    "prefix_clones": 0,  # prefix evaluations on a clone (every non-surface read)
    "prefix_memo_hits": 0,  # prefix evaluations served from the generation memo
    "applies": 0,  # responses written into helper objects
    "tags": 0,  # obj.update_tag() calls
    "sends": 0,  # requests actually sent
    "timeouts": 0,  # requests given up on after the timeout and retried in full
    "undelivered": 0,  # requests not even sent: no client socket to carry them
    "index_rebuilds": 0,  # full-scene index scans (diagnostics/tests)
}


def _pending_of(stack_id, stage_key):
    """The frame this stage is waiting on: ``{rid: monotonic}``, at most one entry."""
    return _pending.setdefault((stack_id, stage_key), {})


def _forget(stack_id, stage_key):
    """Stop waiting for a stage's active frame."""
    _pending.pop((stack_id, stage_key), None)



# --------------------------------------------------------------------- triggers














def _mark_hot():
    """Run the fast cadence for another HOT_SECONDS (see HOT_INTERVAL)."""
    global _hot_until
    _hot_until = time.monotonic() + HOT_SECONDS
























# ------------------------------------------------------------------------ tick


































































# ------------------------------------------------------------------- cache/store
























# --------------------------------------------------------------------- process


def _stage_has_modifier(obj, stage):
    """Whether a stage record still has its modifier (the stage is live)."""
    return nodes.modifier_of(obj, stage) is not None or (
        not stage.modifier_uid and nodes.modifier_by_name(obj, stage) is not None
    )


















# ------------------------------------------------------------------ prefix cache












# The collections an Edit Mode clone's private copy can belong to. Named by the
# ID type's RNA identifier, which is what the copy reports.
_OWNED_COLLECTIONS = {
    "Mesh": "meshes",
    "Curve": "curves",
    "PointCloud": "pointclouds",
}






























# ------------------------------------------------------------------- lifecycle














def register():
    global _registered, _hot_until, _index_dirty
    global _evaluating, _guard_owner, _evaluating_since, _ticking
    global _prefix_batch_built, _prefix_batching
    if _registered:
        return
    _stage_index.clear()
    del _stack_objects[:]
    _index_dirty = True
    # Everything the last registration learned belongs to the session that just
    # ended: a decoded frame, an evaluated prefix, a request in flight, a counter.
    # Leaving them behind served a stale prefix and stale counters across a
    # disable/enable. `_dirty` is left alone — it discards nameless objects on its
    # own, and clearing it could skip the first pass a stack needs.
    _stores.clear()
    _cache.clear()
    _cache_bytes.clear()
    _pending.clear()
    _sent.clear()
    _prefix_memo.clear()
    _prefix_batch.clear()
    _prefix_batch_built = False
    _prefix_batching = False
    _state_since.clear()
    _verify.clear()
    _pull_permits.clear()
    _last_no_send.clear()
    _timings.clear()
    # The prefix guard and the tick flag are session state too: a wedge used to
    # survive disable/enable, so re-enabling the add-on inherited a mute that
    # dropped every edit until the watchdog fired. Registering is a clean start;
    # the watchdog stays as the belt-and-braces for a leak within a session.
    _evaluating = False
    _guard_owner = None
    _evaluating_since = None
    _ticking = False
    for field in _heartbeat:
        _heartbeat[field] = 0 if field in ("trigger_count", "trigger_updates") else None
    _hot_until = 0.0
    for counter in stats:
        stats[counter] = 0
    # A UI-driven parameter write (the choice dropdown) must reach the engine.
    stack_mod.set_param_changed_hook(_mark_object_dirty)
    # Persistent: Blender clears non-persistent timers and handlers on every
    # file load, which would otherwise silence the engine on reopen. The
    # membership checks make a second register() harmless: appending the same
    # handler twice would run the depsgraph path twice per update, and a leaked
    # duplicate outlives the next disable.
    if not bpy.app.timers.is_registered(_timer):
        bpy.app.timers.register(_timer, first_interval=IDLE_INTERVAL, persistent=True)
    for handler_list, handler in (
        (bpy.app.handlers.depsgraph_update_post, _on_depsgraph),
        (bpy.app.handlers.frame_change_post, _on_frame_change),
        (bpy.app.handlers.load_post, _on_load),
        (bpy.app.handlers.undo_post, _on_undo),
        (bpy.app.handlers.redo_post, _on_redo),
    ):
        if handler not in handler_list:
            handler_list.append(handler)
    _registered = True
    try:
        start_server()
    except Exception as exc:
        # A socket that cannot start is a visible error state (the panel shows
        # it), not a reason to abort registration half-way: the timer, handlers
        # and `_registered` must stay paired so unregister can tear them down.
        # The user's Start/retry goes through the same `start_server`.
        bridge.state["error"] = "server start failed: %r" % (exc,)
        prefs.log("[stageloom] server start failed: %r" % (exc,), flush=True)


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
        (bpy.app.handlers.load_post, _on_load),
        (bpy.app.handlers.undo_post, _on_undo),
        (bpy.app.handlers.redo_post, _on_redo),
    ):
        if handler in handler_list:
            handler_list.remove(handler)
    _registered = False

from .diagnostics import (
    _age,
    _is_engine_module,
    _record_timing,
    _timed_since,
    recent_activity,
    registration_report,
    stage_timings,
    tick_rate,
    trigger_health,
    waiting_seconds,
)
from .cache_ops import (
    _bump_generation,
    _cached_response,
    _definition,
    _drop_hot,
    _forget_frame,
    _frame_bytes,
    _remember,
    _store_for,
    _store_frame,
)
from .apply import (
    _apply,
    _apply_attributes,
    _apply_curves,
    _apply_instance_rows,
    _apply_mesh,
    _apply_points,
    _cloud_has_rows,
    _definition_helper,
    _definition_split,
    _dtype_of,
    _group_has_geometry,
    _has_generic_attributes,
    _helper_has_data,
    _kind_of,
    _nested_rows_of,
    _payload_missing,
    _rows_of,
    _warnings_text,
    _write_rows_cloud,
)
from .request import (
    _build_prefix_batch,
    _bump_epochs,
    _bump_id,
    _cleanup_prefix_clone,
    _clone_prefix,
    _clone_prefixes,
    _close_enough,
    _drop_owned_data,
    _modifier_index,
    _no_send,
    _prefix_revision,
    _prefix_sections,
    _process_stage,
    _rest_state,
    _same_prefix,
    _sections_hash,
    _set,
    _timeout,
)
from .lifecycle import (
    _all_stack_objects,
    _drop_removed_stages,
    _duplicate_owner,
    _ensure_index,
    _forget_prefix,
    _mark_dirty_all,
    _mark_object_dirty,
    _process_object,
    _rebuild_index,
    _reindex_object,
    _repair_plumbing,
    _split_duplicate_stages,
    _split_stage,
    _stage_views,
    freeze,
    push,
    request_again,
    start_server,
    unfreeze,
)
from .triggers import (
    _after_history_step,
    _drain_depsgraph,
    _edit_mode_objects,
    _flush_edit_mode,
    _flush_object,
    _nudge_stalled_waits,
    _on_depsgraph,
    _on_frame_change,
    _on_load,
    _on_redo,
    _on_undo,
    _recover_stuck_guard,
    _timer,
    _trace_guard_drop,
)
from .messages import (
    _clear_failed_response,
    _handle_error,
    _handle_incoming,
    _handle_message,
    _handle_response,
    _handle_schema,
    _on_client_hello,
    _report_message_failure,
    _resend_in_full,
)

__all__ = [
    "sys",
    "np",
    "attributes_mod",
    "frame_cache",
    "codec",
    "hashing",
    "bridge",
    "geometry_io",
    "helpers",
    "nodes",
    "prefs",
    "props",
    "server",
    "stack_mod",
    "_age",
    "_is_engine_module",
    "_record_timing",
    "_timed_since",
    "recent_activity",
    "registration_report",
    "stage_timings",
    "tick_rate",
    "trigger_health",
    "waiting_seconds",
    "_bump_generation",
    "_cached_response",
    "_definition",
    "_drop_hot",
    "_forget_frame",
    "_frame_bytes",
    "_remember",
    "_store_for",
    "_store_frame",
    "_apply",
    "_apply_attributes",
    "_apply_curves",
    "_apply_instance_rows",
    "_apply_mesh",
    "_apply_points",
    "_cloud_has_rows",
    "_definition_helper",
    "_definition_split",
    "_dtype_of",
    "_group_has_geometry",
    "_has_generic_attributes",
    "_helper_has_data",
    "_kind_of",
    "_nested_rows_of",
    "_payload_missing",
    "_rows_of",
    "_warnings_text",
    "_write_rows_cloud",
    "_build_prefix_batch",
    "_bump_epochs",
    "_bump_id",
    "_cleanup_prefix_clone",
    "_clone_prefix",
    "_clone_prefixes",
    "_close_enough",
    "_drop_owned_data",
    "_modifier_index",
    "_no_send",
    "_prefix_revision",
    "_prefix_sections",
    "_process_stage",
    "_rest_state",
    "_same_prefix",
    "_sections_hash",
    "_set",
    "_timeout",
    "_all_stack_objects",
    "_drop_removed_stages",
    "_duplicate_owner",
    "_ensure_index",
    "_forget_prefix",
    "_mark_dirty_all",
    "_mark_object_dirty",
    "_process_object",
    "_rebuild_index",
    "_reindex_object",
    "_repair_plumbing",
    "_split_duplicate_stages",
    "_split_stage",
    "_stage_views",
    "freeze",
    "push",
    "request_again",
    "start_server",
    "unfreeze",
    "_after_history_step",
    "_drain_depsgraph",
    "_edit_mode_objects",
    "_flush_edit_mode",
    "_flush_object",
    "_nudge_stalled_waits",
    "_on_depsgraph",
    "_on_frame_change",
    "_on_load",
    "_on_redo",
    "_on_undo",
    "_recover_stuck_guard",
    "_timer",
    "_trace_guard_drop",
    "_clear_failed_response",
    "_handle_error",
    "_handle_incoming",
    "_handle_message",
    "_handle_response",
    "_handle_schema",
    "_on_client_hello",
    "_report_message_failure",
    "_resend_in_full",
]
