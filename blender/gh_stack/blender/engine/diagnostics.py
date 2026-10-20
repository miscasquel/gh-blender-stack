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

