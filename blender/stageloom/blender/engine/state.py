"""One owner for stage state writes: precedence, guards and the wait clock.

``stage.state`` is a projection of these facts, in this order:

* the stage key has no proven owner (``ENGINE.stage_conflicts``): ``conflict``;
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
``waiting_seconds`` honest: the runtime record owns ``state_since``, cleared on
every transition out of ``waiting``.

The target guards are the documented transition matrix. Every state may follow
any other state that its flags permit; the illegal edges are exactly the flag
contradictions above and ``waiting`` without an in-flight request.
"""

import time

from .. import stack as stack_mod
from .request import _set
from .runtime import ENGINE


def _wait_key(stage):
    """The ``(stack_id, stage_key)`` pair that owns the wait clock, or None."""
    try:
        obj = stage.id_data
        record = stack_mod.stack_of(obj)
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
    return ENGINE.is_pending(*key)


def _settled_state(stage):
    """``rest_state`` for a stage that is not showing a transient answer."""
    return stack_mod.rest_state(stage)


def _resolve(stage, requested):
    """The state actually written for ``requested`` (the transition matrix)."""
    if ENGINE.stage_key_conflicted(stage.key):
        return stack_mod.STATE_CONFLICT
    if not stage.enabled:
        # Disabled outranks frozen; both outrank transient activity.
        return stack_mod.STATE_DISABLED
    if stage.frozen:
        # A Pull is the one request a frozen stage may wait for.
        if requested == stack_mod.STATE_WAITING and _has_pending(stage):
            return stack_mod.STATE_WAITING
        return stack_mod.STATE_FROZEN
    if requested == stack_mod.STATE_WAITING:
        if _has_pending(stage):
            return stack_mod.STATE_WAITING
        if stage.state == stack_mod.STATE_WAITING:
            # A wait with nothing behind it is a diagnostic lie and a stall
            # forever. Settle it instead of leaving it.
            return _settled_state(stage)
        return stage.state
    if requested in stack_mod.FLAG_STATES:
        # A flag state without its flag. Clear a stale flag projection; for any
        # other current state, refuse the write rather than invent a flag.
        if stage.state in stack_mod.FLAG_STATES:
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
    if state not in stack_mod.STATE_VALUES:
        raise ValueError("unknown stage state %r" % (state,))
    actual = _resolve(stage, state)
    _set(stage, "state", actual)
    if error is not None:
        _set(stage, "error", error)
    key = _wait_key(stage)
    if key is not None:
        record = ENGINE.runtime(*key)
        if actual == stack_mod.STATE_WAITING:
            # request.py stamps the exact send time; this covers callers that
            # enter the state without one.
            if record.state_since is None:
                record.state_since = time.monotonic()
        else:
            record.state_since = None
    return actual
