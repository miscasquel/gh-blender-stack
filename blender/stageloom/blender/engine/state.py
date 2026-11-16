"""One owner for stage state writes: precedence, guards and the wait clock.

``stage.state`` is a projection of these facts, in this order:

* the stage key has no proven owner (``_stage_conflicts``): ``conflict``;
* the stage's modifier is inactive: ``disabled``;
* the stage is frozen: ``frozen`` (a Pull may still be ``waiting``);
* otherwise the last activity: ``idle``/``waiting``/``ok``/``error``/
  ``disconnected``/``unbound``.

A request for a state that contradicts those facts is clamped, never written:
a disabled stage cannot report ``ok``, an unfrozen stage cannot report
``frozen``, and a key with one owner cannot report ``conflict``. A flag state
that is already displayed while its flag has gone (an undo, a record written by
an older build) is settled back to ``rest_state``; other contradictory targets
are refused so a stale caller cannot move a healthy stage.

``waiting`` is special: it is legal only while a request is actually pending
(or a frozen Pull is in flight). Without one it is refused, or settled back to
``rest_state`` if the stage was already stuck there. This is also what keeps
``waiting_seconds`` honest: the helper owns ``_state_since``, clearing it on
every transition out of ``waiting``.

The target guards are the documented transition matrix. Every state may follow
any other state that its flags permit; the illegal edges are exactly the flag
contradictions above and ``waiting`` without an in-flight request.
"""

import sys

engine = sys.modules[__package__]


def _wait_key(stage):
    """The ``(stack_id, stage_key)`` pair that owns the wait clock, or None."""
    try:
        obj = stage.id_data
        record = engine.stack_mod.stack_of(obj)
    except (AttributeError, ReferenceError):
        return None
    if record is None:
        return None
    return (record.stack_id, stage.key)


def _has_pending(stage):
    """Whether this stage has a request in flight (an answer is awaited)."""
    key = _wait_key(stage)
    if key is None:
        return False
    return bool(engine._pending.get(key))


def _settled_state(stage):
    """``rest_state`` for a stage that is not showing a transient answer."""
    return engine.stack_mod.rest_state(stage)


def _resolve(stage, requested):
    """The state actually written for ``requested`` (the transition matrix)."""
    if engine._stage_key_conflicted(stage.key):
        return engine.stack_mod.STATE_CONFLICT
    if not stage.enabled:
        # Disabled outranks frozen; both outrank transient activity.
        return engine.stack_mod.STATE_DISABLED
    if stage.frozen:
        # A Pull is the one request a frozen stage may wait for.
        if requested == engine.stack_mod.STATE_WAITING and _has_pending(stage):
            return engine.stack_mod.STATE_WAITING
        return engine.stack_mod.STATE_FROZEN
    if requested == engine.stack_mod.STATE_WAITING:
        if _has_pending(stage):
            return engine.stack_mod.STATE_WAITING
        if stage.state == engine.stack_mod.STATE_WAITING:
            # A wait with nothing behind it is a diagnostic lie and a stall
            # forever. Settle it instead of leaving it.
            return _settled_state(stage)
        return stage.state
    if requested in engine.stack_mod.FLAG_STATES:
        # A flag state without its flag. Clear a stale flag projection; for any
        # other current state, refuse the write rather than invent a flag.
        if stage.state in engine.stack_mod.FLAG_STATES:
            return _settled_state(stage)
        return stage.state
    return requested


def _set_state(stage, state, error=None):
    """Write one stage state through the guard; return the state written.

    ``error`` is an optional detail written in the same call, so a frozen error
    keeps ``frozen`` while still recording why: the panel shows the state icon
    and the error text independently. A state that the guard refuses returns the
    unchanged current state.
    """
    if state not in engine.stack_mod.STATE_VALUES:
        raise ValueError("unknown stage state %r" % (state,))
    actual = _resolve(stage, state)
    engine._set(stage, "state", actual)
    if error is not None:
        engine._set(stage, "error", error)
    key = _wait_key(stage)
    if key is not None:
        if actual == engine.stack_mod.STATE_WAITING:
            # request.py stamps the exact send time; this covers callers that
            # enter the state without one.
            engine._state_since.setdefault(key, engine.time.monotonic())
        else:
            engine._state_since.pop(key, None)
    return actual
