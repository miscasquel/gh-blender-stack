"""Diagnostics: trace, heartbeat, per-stage timings and wait ages."""

import sys

engine = sys.modules[__package__]


def _timed_since(started):
    """Milliseconds elapsed since a `time.monotonic()` stamp."""
    return (engine.time.monotonic() - started) * 1000.0


def _record_timing(record, stage, field, value):
    """Keep the last measured latency of one stage field."""
    engine._timings.setdefault((record.stack_id, stage.key), {})[field] = value


def stage_timings(stack_id, stage_key):
    """The last measured pass latencies of one stage, in milliseconds.

    Read by the Diagnostics panel ("clone 11 ms · encode 2 ms · answer 68 ms ·
    apply 3 ms"), so a slow round trip names its slowest part instead of being a
    black box. A copy, not the live mapping: a pass may write while the panel
    draws, and the panel must never be able to change engine state. Fields that
    have not been measured yet are absent.
    """
    values = engine._timings.get((stack_id, stage_key))
    return dict(values) if values else {}


def _age(now, then):
    return None if then is None else max(0.0, now - then)


def trigger_health():
    """Ages of the last trigger, send and apply, for the Diagnostics panel.

    A frozen trigger age while the scene is edited is the tell for a mute; the
    ages are seconds, or None before the event has happened this session.
    """
    now = engine.time.monotonic()
    return {
        "trigger_age": engine._age(now, engine._heartbeat["trigger_at"]),
        "trigger_count": engine._heartbeat["trigger_count"],
        "trigger_updates": engine._heartbeat["trigger_updates"],
        "send_age": engine._age(now, engine._heartbeat["sent_at"]),
        "apply_age": engine._age(now, engine._heartbeat["applied_at"]),
    }


def waiting_seconds(stack_id, stage_key):
    """How long a stage has been waiting for an answer, or 0.0 when it is not.

    Read by the diagnostics panel: an answer outstanding for a second is where a
    sync fault is indistinguishable from a stage that is merely thinking.
    """
    since = engine._state_since.get((stack_id, stage_key))
    if since is None:
        return 0.0
    return engine.time.monotonic() - since


def recent_activity(count=8):
    """The last events the engine acted on, oldest first.

    The Diagnostics panel shows them so a sync fault is a visible chain
    (trigger -> decision -> request -> answer) instead of a guess.
    """
    return list(engine._trace)[-count:]


_HANDLER_NAMES = (
    "depsgraph_update_post",
    "frame_change_post",
    "load_post",
    "undo_post",
    "redo_post",
)
# The engine package is loaded as `gh_stack.blender.engine` by the development
# install and as `bl_ext.<repo>.gh_stack.blender.engine` by an extension. Both
# can exist on disk at once, so a registration check must see both. A filter on
# `startswith("gh_stack")` misses every extension handler.
_ENGINE_MODULE_TAIL = "gh_stack.blender.engine"


def _is_engine_module(module):
    """Whether a module name belongs to any GH Stack engine generation."""
    if not module:
        return False
    if module == _ENGINE_MODULE_TAIL or module.startswith(_ENGINE_MODULE_TAIL + "."):
        return True
    return module.endswith("." + _ENGINE_MODULE_TAIL) or (
        "." + _ENGINE_MODULE_TAIL + "."
    ) in module


def _enabled_gh_stack_addons():
    """The enabled add-on modules that look like a GH Stack install."""
    try:
        addons = engine.bpy.context.preferences.addons
    except AttributeError:
        return []
    return sorted(item.module for item in addons if "gh_stack" in item.module)


def registration_report():
    """A read-only snapshot of who is registered, for the reload investigation.

    The handoff's first check counted handlers by module name; this reports the
    same thing plus the pieces that decide whether the engine can be trusted:
    the module path actually loaded, the timer in this generation, the stack
    index, the hot deadline and the dirty set. It never writes engine state.

    Handler counts are the visible part of a leaked generation. Timers cannot be
    enumerated through `bpy.app.timers`; `tick_rate` measures this generation
    only, so a stale timer with its own module state is invisible there. A
    leaked generation with handlers still attached is the reliable signal.
    """
    now = engine.time.monotonic()
    handlers = {}
    counts = {}
    for name in _HANDLER_NAMES:
        entries = []
        for handler in getattr(engine.bpy.app.handlers, name):
            module = getattr(handler, "__module__", "")
            if not _is_engine_module(module):
                continue
            timer = getattr(handler, "_gh_stack_engine_timer", None)
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
        "engine_file": getattr(engine, "__file__", ""),
        "engine_package": __package__,
        "timer_module": getattr(engine._timer, "__module__", ""),
        "timer_id": id(engine._timer),
        "timer_registered": bool(engine.bpy.app.timers.is_registered(engine._timer)),
        "registered": bool(engine._registered),
        "enabled_addons": _enabled_gh_stack_addons(),
        "handler_counts": counts,
        "handlers": handlers,
        "stack_objects": len(engine._stack_objects),
        "index_dirty": bool(engine._index_dirty),
        "dirty": sorted(engine._dirty),
        "hot_seconds": max(0.0, engine._hot_until - now),
        "passes": engine.stats["passes"],
    }


def tick_rate(seconds=5.0):
    """Timer passes per second, sampled by blocking the caller.

    `bpy.app.timers` has no public callback list, and a stale timer keeps its
    own module's `stats`; this measures the current generation only. It is still
    the way to tell an idle cadence from a hot one. Call it from Blender's
    Python console; it sleeps, so the window is unresponsive for ``seconds``.
    In a background session the timer is inert and the result is zero.
    """
    seconds = max(0.1, float(seconds))
    before = engine.stats["passes"]
    started = engine.time.monotonic()
    engine.time.sleep(seconds)
    elapsed = engine.time.monotonic() - started
    return (engine.stats["passes"] - before) / elapsed if elapsed else 0.0

