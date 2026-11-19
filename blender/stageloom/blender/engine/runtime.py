"""Session state: one explicit ``Engine`` object and one record per stage.

``ENGINE`` is the single mutable owner of the engine session. Submodules import
it directly; they never reach back through the package object. ``StageRuntime``
owns everything keyed by ``(stack_id, stage_key)``: the request in flight, what
the client was last sent, the frame it was sent at, the last latencies, the wait
clock, the one-shot Pull permit and the last "why no send" decision. One record
per stage means one accessor and one cleanup path.

Constants stay module-level: they are configuration, not session state.
"""

import time
from collections import deque

import bpy

WAIT_INTERVAL = 0.005
# The idle cadence: what a quiet session pays to notice an incoming message
# (a GH-side edit, a reconnect). 20 ms keeps those reactive without a tick
# budget worth measuring; the fast cadence below is what edits use.
IDLE_INTERVAL = 0.02
# While the scene is being edited the fast cadence applies instead of the idle
# one: an edit keeps the engine hot for HOT_SECONDS, so a reaction costs one tick
# rather than up to one idle interval. Playback fills frames this way — each
# answer re-dirties the stack and the next frame goes out at once — and a quiet
# scene costs nothing, because ``dirty`` is emptied on every pass.
HOT_INTERVAL = 0.005
HOT_SECONDS = 1.0
# A request that is still outstanding after this long may be superseded when the
# scene has moved on (the pass asks with the current state). Grasshopper
# coalesces solves, so the newer ask does not build a backlog, and the answer
# arrives for the value the user is looking at instead of one round trip behind
# it. The hard timeout still covers an answer that never comes.
SOFT_RETRY_SECONDS = 1.0


class StageRuntime:
    """Everything the engine remembers about one live stage for one session."""

    __slots__ = (
        "pending",
        "sent",
        "last_frame",
        "timings",
        "state_since",
        "pull_permit",
        "pull_deadline",
        "last_no_send",
    )

    def __init__(self):
        # {rid: monotonic seconds it was sent}. At most one entry: the
        # evaluation in flight. One at a time, deliberately — a frame that
        # arrives while an answer is outstanding is not queued behind it,
        # because Grasshopper solves in arrival order on one thread and a queue
        # of frames the user has already scrubbed past only keeps the client
        # busy after they have moved on.
        self.pending = {}
        # (rid, geometry hash) of the last request handed to a connected client.
        # This is connection state, never saved and never undoable.
        self.sent = None
        # Scene frame of the last delivered request. A frame change is one of
        # the two things that ask a live stage to solve again.
        self.last_frame = None
        # The last pass latencies in milliseconds: prefix_ms, encode_ms,
        # rtt_ms, apply_ms. Diagnostics-only; never saved with the .blend.
        self.timings = {}
        # Monotonic time the stage entered `waiting`, so a wait that is really
        # a fault is visible in the Diagnostics panel.
        self.state_since = None
        # A Pull is the one request a frozen stage may make. The permit and its
        # deadline settle back to frozen when the answer never lands.
        self.pull_permit = False
        self.pull_deadline = None
        # Why the last pass did not send: (reason, rid). Re-recorded only when
        # it changes, so a quiet pass does not flood the trace ring.
        self.last_no_send = None


class Engine:
    """The engine session: every mutable registry, trigger flag and counter."""

    def __init__(self):
        # Prefix evaluation memo/batch, one depsgraph generation and the
        # verification set (a request that must ask even when rid/frame match).
        self.prefix_memo = {}
        self.prefix_batch = {}
        self.prefix_batch_built = False
        self.prefix_batching = False
        self.prefix_epoch = 0
        self.verify = set()
        # What the engine acted on, for the Diagnostics panel.
        self.trace_lines = deque(maxlen=200)
        self.heartbeat = {
            "trigger_at": None,
            "trigger_count": 0,
            "trigger_updates": 0,
            "sent_at": None,
            "applied_at": None,
        }
        # Stage/stack lookup, rebuilt on load/undo and patched as stages change.
        self.stage_index = {}
        self.stage_claims = {}
        self.stage_owners = {}
        self.stage_conflicts = set()
        self.stack_objects = []
        self.index_dirty = True
        self.index_object_count = -1
        self.dirty = set()
        self.last_stall_check = 0.0
        # Registration/activity flags.
        self.registered = False
        self.active = False
        self.hot_until = 0.0
        # Prefix-evaluation guard. `evaluating` mutes the engine's own clone
        # events; `guard_owner`/`evaluating_since` let the watchdog name a pass
        # that died with it up, and `ticking` tells the depsgraph handler
        # whether the guard is expected.
        self.evaluating = False
        self.guard_owner = None
        self.evaluating_since = None
        self.ticking = False
        # Playback/bake gate: the scene that owns it, the flags the frame
        # handler reads, and the cursor-progress tuple while a bake runs.
        self.playback_sync = False
        self.bake_sync = False
        self.playback_scene = None
        self.bake_scene = None
        self.bake_progress = None
        # True while a prefix read temporarily activates the object's scene.
        self.switching_scene = False
        # Last failure reported per stage during playback/bake, so one
        # disconnected client does not print a line for every frame.
        self.playback_reports = {}
        # One runtime record per (stack_id, stage_key).
        self._runtimes = {}
        # Counters for tests and diagnostics.
        self._stats = {
            "passes": 0,
            "prefix_clones": 0,
            "prefix_memo_hits": 0,
            "applies": 0,
            "tags": 0,
            "sends": 0,
            "timeouts": 0,
            "undelivered": 0,
            "index_rebuilds": 0,
            "playback_frames": 0,
            "playback_synced": 0,
            "playback_failures": 0,
        }

    # ------------------------------------------------------------ runtime rows

    def runtime(self, stack_id, stage_key):
        """The record for one stage, created on first use."""
        key = (stack_id, stage_key)
        record = self._runtimes.get(key)
        if record is None:
            record = StageRuntime()
            self._runtimes[key] = record
        return record

    def peek_runtime(self, stack_id, stage_key):
        """The record for one stage, or None when it has never been used."""
        return self._runtimes.get((stack_id, stage_key))

    def pending_of(self, stack_id, stage_key):
        """The frame this stage is waiting on: ``{rid: monotonic}``, at most one."""
        return self.runtime(stack_id, stage_key).pending

    def is_pending(self, stack_id, stage_key):
        """Whether the stage has a request in flight."""
        record = self._runtimes.get((stack_id, stage_key))
        return bool(record is not None and record.pending)

    def forget(self, stack_id, stage_key):
        """Stop waiting for a stage's active frame; keep the rest of the row."""
        record = self._runtimes.get((stack_id, stage_key))
        if record is not None:
            record.pending.clear()

    def discard_runtime(self, stack_id, stage_key):
        """Drop every session fact about one stage (removal/reset only)."""
        self._runtimes.pop((stack_id, stage_key), None)

    def clear_runtimes(self):
        """Drop every per-stage session record."""
        self._runtimes.clear()

    def prune_session_state(self, live_keys, live_pointers):
        """Retire per-stage state whose stage or stack object no longer exists.

        A deleted stack object cannot run its own removal cleanup: its stage
        records leave the scene with it, so the full index rebuild is the only
        place that can see the new truth. ``live_keys`` are the current
        ``(stack_id, stage_key)`` claims and ``live_pointers`` the current
        ``(object pointer, stage_key)`` prefixes. A live stage keeps its
        record, including an in-flight request; only keys absent from those
        sets are dropped.
        """
        for key in list(self._runtimes):
            if key not in live_keys:
                del self._runtimes[key]
        self.verify.intersection_update(live_keys)
        for key in list(self.prefix_memo):
            if key not in live_pointers:
                del self.prefix_memo[key]
        stage_keys = {stage_key for _stack_id, stage_key in live_keys}
        for key in list(self.playback_reports):
            if key not in stage_keys:
                del self.playback_reports[key]
        self.dirty.intersection_update(
            {obj.name for obj in bpy.data.objects}
        )

    def clear_pull(self, stack_id, stage_key=None):
        """Drop one Pull permit and its deadline together."""
        if stage_key is None:
            stack_id, stage_key = stack_id
        record = self._runtimes.get((stack_id, stage_key))
        if record is not None:
            record.pull_permit = False
            record.pull_deadline = None

    def arm_pull(self, stack_id, stage_key, deadline):
        """Allow exactly one request while frozen, bounded by ``deadline``."""
        record = self.runtime(stack_id, stage_key)
        record.pull_permit = True
        record.pull_deadline = deadline

    # -------------------------------------------------------------- diagnostics

    def timed_since(self, started):
        """Milliseconds elapsed since a ``time.monotonic()`` stamp."""
        return (time.monotonic() - started) * 1000.0

    def record_timing(self, record, stage, field, value):
        """Keep the last measured latency of one stage field."""
        self.runtime(record.stack_id, stage.key).timings[field] = value

    def stage_timings(self, stack_id, stage_key):
        record = self._runtimes.get((stack_id, stage_key))
        return dict(record.timings) if record is not None and record.timings else {}

    def waiting_seconds(self, stack_id, stage_key):
        record = self._runtimes.get((stack_id, stage_key))
        if record is None or not record.pending or record.state_since is None:
            return 0.0
        return time.monotonic() - record.state_since

    def trigger_health(self):
        now = time.monotonic()
        heartbeat = self.heartbeat
        return {
            "trigger_age": _age(now, heartbeat["trigger_at"]),
            "trigger_count": heartbeat["trigger_count"],
            "trigger_updates": heartbeat["trigger_updates"],
            "send_age": _age(now, heartbeat["sent_at"]),
            "apply_age": _age(now, heartbeat["applied_at"]),
        }

    def recent_activity(self, count=8):
        return list(self.trace_lines)[-count:]

    def registration_report(self):
        from .diagnostics import registration_report

        return registration_report()

    def tick_rate(self, seconds=5.0):
        from .diagnostics import tick_rate

        return tick_rate(seconds)

    def stats(self):
        return self._stats

    def trace(self, line):
        self.trace_lines.append(line)

    # ------------------------------------------------------------- orchestration

    def stage_key_conflicted(self, key):
        from .lifecycle import _stage_key_conflicted

        return _stage_key_conflicted(key)

    def set_state(self, stage, state, error=None):
        from .state import _set_state

        return _set_state(stage, state, error=error)

    def ordered_stages(self, obj, record):
        from .lifecycle import _ordered_stages

        return _ordered_stages(obj, record)

    def ensure_index(self):
        from .lifecycle import _ensure_index

        _ensure_index()

    def mark_dirty_all(self):
        from .lifecycle import _mark_dirty_all

        _mark_dirty_all()

    def mark_dirty_scene(self, scene):
        from .lifecycle import _mark_dirty_scene

        _mark_dirty_scene(scene)

    def mark_object_dirty(self, obj):
        self.ensure_active()
        self.dirty.add(obj.name)
        self.mark_hot()

    def repair_plumbing(self, obj, record, stage, modifier):
        from .lifecycle import _repair_plumbing

        return _repair_plumbing(obj, record, stage, modifier)

    def mark_hot(self):
        self.hot_until = time.monotonic() + HOT_SECONDS

    # --------------------------------------------------------- public controls

    def start_server(self, port=None):
        from .lifecycle import start_server

        return start_server(port)

    def request_again(self, obj, stage=None, skip_frozen=False):
        from .lifecycle import request_again

        return request_again(obj, stage, skip_frozen=skip_frozen)

    def freeze(self, obj, stage):
        from .lifecycle import freeze

        return freeze(obj, stage)

    def unfreeze(self, obj, stage):
        from .lifecycle import unfreeze

        return unfreeze(obj, stage)

    def push(self, obj=None):
        from .lifecycle import push

        return push(obj)

    def resolve_duplicate_key(self, obj, record, stage):
        from .lifecycle import resolve_duplicate_key

        return resolve_duplicate_key(obj, record, stage)

    # -------------------------------------------------------------- playback/bake

    def reset_playback_reports(self):
        self.playback_reports.clear()

    def begin_bake(self, scene):
        """Arm the synchronous bake gate and reset the frame counters."""
        self.bake_sync = True
        self.bake_scene = scene.as_pointer() if scene is not None else None
        for counter in ("playback_frames", "playback_synced", "playback_failures"):
            self._stats[counter] = 0

    def set_bake_progress(self, window_manager, start, total):
        self.bake_progress = (window_manager, int(start), total)

    def end_bake(self):
        self.bake_progress = None
        self.bake_sync = False
        self.bake_scene = None

    # ---------------------------------------------------------- activation hooks

    def active_handlers(self):
        from . import playback, triggers

        return (
            (bpy.app.handlers.depsgraph_update_post, triggers._on_depsgraph),
            (bpy.app.handlers.frame_change_post, triggers._on_frame_change),
            (bpy.app.handlers.frame_change_post, playback._on_frame_change_sync),
            (bpy.app.handlers.animation_playback_pre, playback._on_playback_pre),
            (bpy.app.handlers.animation_playback_post, playback._on_playback_post),
        )

    def lifecycle_handlers(self):
        from . import triggers

        return (
            (bpy.app.handlers.load_post, triggers._on_load),
            (bpy.app.handlers.undo_post, triggers._on_undo),
            (bpy.app.handlers.redo_post, triggers._on_redo),
        )

    def activate(self):
        """Register the timer and the per-frame handlers (idempotent)."""
        if not self.registered:
            return False
        from .triggers import _timer

        if not bpy.app.timers.is_registered(_timer):
            bpy.app.timers.register(
                _timer, first_interval=IDLE_INTERVAL, persistent=True
            )
        for handler_list, handler in self.active_handlers():
            if handler not in handler_list:
                handler_list.append(handler)
        self.active = True
        return True

    def deactivate(self):
        """Go dormant: no timer and no per-update or per-frame handler."""

        from .triggers import _timer

        if bpy.app.timers.is_registered(_timer):
            bpy.app.timers.unregister(_timer)
        for handler_list, handler in self.active_handlers():
            if handler in handler_list:
                handler_list.remove(handler)
        self.active = False
        self.hot_until = 0.0
        self.playback_sync = False
        self.bake_sync = False
        self.bake_progress = None
        self.playback_scene = None
        self.bake_scene = None
        self.switching_scene = False
        self.dirty.clear()
        return False

    def ensure_active(self):
        if not self.registered:
            return False
        self.ensure_index()
        if self.stack_objects:
            return self.activate()
        return self.deactivate()

    def settle(self):
        if self.registered and self.ensure_active():
            self.mark_dirty_all()
            self.mark_hot()

    # ------------------------------------------------------------- registration

    def register(self):
        if self.registered:
            return
        from .. import bridge, prefs
        from .. import stack as stack_mod

        self.stage_index.clear()
        self.stage_claims.clear()
        self.stage_owners.clear()
        self.stage_conflicts.clear()
        del self.stack_objects[:]
        self.index_dirty = True
        # Everything the last registration learned belongs to the session that
        # just ended: a decoded frame, an evaluated prefix, a request in flight,
        # a counter. `dirty` is left alone — it discards nameless objects on its
        # own, and clearing it could skip the first pass a stack needs.
        bridge.clear_incoming()
        self.clear_runtimes()
        self.prefix_memo.clear()
        self.prefix_batch.clear()
        self.prefix_batch_built = False
        self.prefix_batching = False
        self.prefix_epoch = 0
        self.verify.clear()
        self.last_stall_check = 0.0
        self.reset_playback_reports()
        self.evaluating = False
        self.guard_owner = None
        self.evaluating_since = None
        self.ticking = False
        self.playback_sync = False
        self.bake_sync = False
        self.bake_progress = None
        self.playback_scene = None
        self.bake_scene = None
        self.switching_scene = False
        for field in self.heartbeat:
            self.heartbeat[field] = (
                0 if field in ("trigger_count", "trigger_updates") else None
            )
        self.hot_until = 0.0
        for counter in self._stats:
            self._stats[counter] = 0
        # A UI-driven parameter write (the choice dropdown) must reach the
        # engine.
        stack_mod.set_param_changed_hook(self.mark_object_dirty)
        # Lifecycle handlers are persistent and always present: Blender clears
        # non-persistent handlers and timers on every file load, and a dormant
        # engine still has to notice a loaded file that contains stages. The
        # membership checks make a second register() harmless.
        for handler_list, handler in self.lifecycle_handlers():
            if handler not in handler_list:
                handler_list.append(handler)
        self.registered = True
        try:
            self.start_server()
        except Exception as exc:
            # A socket that cannot start is a visible error state (the panel
            # shows it), not a reason to abort registration half-way.
            bridge.state["error"] = "server start failed: %r" % (exc,)
            prefs.log("[stageloom] server start failed: %r" % (exc,), flush=True)
        # Enable the per-update and per-frame work only when this file has
        # stages. Blender restricts datablock access while an add-on enables;
        # one shot on the event loop then settles the state, with the file
        # readable.
        if hasattr(bpy.data, "objects"):
            if self.ensure_active():
                self.mark_dirty_all()
                self.mark_hot()
        elif not bpy.app.timers.is_registered(_settle):
            bpy.app.timers.register(_settle, first_interval=0.0, persistent=True)

    def unregister(self):
        if not self.registered:
            return
        from .. import stack as stack_mod

        stack_mod.set_param_changed_hook(None)
        self.deactivate()
        if bpy.app.timers.is_registered(_settle):
            bpy.app.timers.unregister(_settle)
        for handler_list, handler in self.lifecycle_handlers():
            if handler in handler_list:
                handler_list.remove(handler)
        self.registered = False


def _age(now, then):
    return None if then is None else max(0.0, now - then)


def _settle():
    """Timer callback used while registration ran under data restriction."""
    ENGINE.settle()


ENGINE = Engine()
