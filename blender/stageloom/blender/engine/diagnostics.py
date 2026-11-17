"""Diagnostics: trace, heartbeat, per-stage timings and wait ages."""

import os
import time

import bpy

from .runtime import ENGINE


def _timed_since(started):
    """Milliseconds elapsed since a `time.monotonic()` stamp."""
    return ENGINE.timed_since(started)


def _record_timing(record, stage, field, value):
    """Keep the last measured latency of one stage field."""
    ENGINE.record_timing(record, stage, field, value)


def stage_timings(stack_id, stage_key):
    """The last measured pass latencies of one stage, in milliseconds.

    Read by the Diagnostics panel ("clone 11 ms · encode 2 ms · answer 68 ms ·
    apply 3 ms"), so a slow round trip names its slowest part instead of being a
    black box. A copy, not the live mapping: a pass may write while the panel
    draws, and the panel must never be able to change engine state. Fields that
    have not been measured yet are absent.
    """
    return ENGINE.stage_timings(stack_id, stage_key)


def _age(now, then):
    return None if then is None else max(0.0, now - then)


def trigger_health():
    """Ages of the last trigger, send and apply, for the Diagnostics panel.

    A frozen trigger age while the scene is edited is the tell for a mute; the
    ages are seconds, or None before the event has happened this session.
    """
    return ENGINE.trigger_health()


def waiting_seconds(stack_id, stage_key):
    """How long a stage has been waiting for an answer, or 0.0 when it is not.

    Read by the diagnostics panel: an answer outstanding for a second is where a
    sync fault is indistinguishable from a stage that is merely thinking. The
    pending request is the truth: a stage that settled to ok/error/idle may still
    have an old send stamp until it is cleared, and an idle stage must never
    report a wait.
    """
    return ENGINE.waiting_seconds(stack_id, stage_key)


def recent_activity(count=8):
    """The last events the engine acted on, oldest first.

    The Diagnostics panel shows them so a sync fault is a visible chain
    (trigger -> decision -> request -> answer) instead of a guess.
    """
    return ENGINE.recent_activity(count)


_HANDLER_NAMES = (
    "depsgraph_update_post",
    "frame_change_post",
    "animation_playback_pre",
    "animation_playback_post",
    "load_post",
    "undo_post",
    "redo_post",
)
# The engine package is loaded as `stageloom.blender.engine` by the development
# install and as `bl_ext.<repo>.stageloom.blender.engine` by an extension. Both
# can exist on disk at once, so a registration check must see both. A filter on
# `startswith("stageloom")` misses every extension handler.
_ENGINE_MODULE_TAIL = "stageloom.blender.engine"


def _is_engine_module(module):
    """Whether a module name belongs to any Stageloom engine generation."""
    if not module:
        return False
    if module == _ENGINE_MODULE_TAIL or module.startswith(_ENGINE_MODULE_TAIL + "."):
        return True
    return module.endswith("." + _ENGINE_MODULE_TAIL) or (
        "." + _ENGINE_MODULE_TAIL + "."
    ) in module


def _enabled_stageloom_addons():
    """The enabled add-on modules that look like a Stageloom install."""
    try:
        addons = bpy.context.preferences.addons
    except AttributeError:
        return []
    return sorted(item.module for item in addons if "stageloom" in item.module)


def registration_report():
    """A read-only snapshot of who is registered.

    Handler counts reveal a leaked generation: timers cannot be enumerated
    through `bpy.app.timers`, but a stale generation with handlers still
    attached is visible here. Never writes engine state.
    """
    from .triggers import _timer

    now = time.monotonic()
    handlers = {}
    counts = {}
    for name in _HANDLER_NAMES:
        entries = []
        for handler in getattr(bpy.app.handlers, name):
            module = getattr(handler, "__module__", "")
            if not _is_engine_module(module):
                continue
            timer = getattr(handler, "_stageloom_engine_timer", None)
            entries.append(
                {
                    "module": module,
                    "name": getattr(handler, "__name__", repr(handler)),
                    "id": id(handler),
                    "timer_id": id(timer) if timer is not None else None,
                }
            )
        handlers[name] = entries
        counts[name] = len(entries)
    return {
        # The facade file names the engine generation, as it always has.
        "engine_file": os.path.join(os.path.dirname(__file__), "__init__.py"),
        "engine_package": __package__,
        "timer_module": getattr(_timer, "__module__", ""),
        "timer_id": id(_timer),
        "timer_registered": bool(bpy.app.timers.is_registered(_timer)),
        "registered": bool(ENGINE.registered),
        "active": bool(ENGINE.active),
        "enabled_addons": _enabled_stageloom_addons(),
        "handler_counts": counts,
        "handlers": handlers,
        "stack_objects": len(ENGINE.stack_objects),
        "index_dirty": bool(ENGINE.index_dirty),
        "dirty": sorted(ENGINE.dirty),
        "hot_seconds": max(0.0, ENGINE.hot_until - now),
        "passes": ENGINE.stats()["passes"],
    }


def tick_rate(seconds=5.0):
    """Timer passes per second, sampled by blocking the caller.

    `bpy.app.timers` has no public callback list, and a stale timer keeps its
    own module's counters; this measures the current generation only. It is
    still the way to tell an idle cadence from a hot one. Call it from Blender's
    Python console; it sleeps, so the window is unresponsive for ``seconds``.
    A dormant engine (a file with no stages) has no timer, so the result is
    zero. In a background session the timer is inert and the result is zero too.
    """
    seconds = max(0.1, float(seconds))
    before = ENGINE.stats()["passes"]
    started = time.monotonic()
    time.sleep(seconds)
    elapsed = time.monotonic() - started
    return (ENGINE.stats()["passes"] - before) / elapsed if elapsed else 0.0
