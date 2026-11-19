"""Bake freeze bookkeeping and the downstream trigger pass.

The native Animation bake itself stays in the operator: it needs a saved file,
a window manager and Blender's own bake operator. Everything around it lives
here, so the freeze token rules and the one-request dependent pass have one
definition:

- a bake token names exactly the stages that bake froze;
- Clear Bake releases only those stages, and never a stage that owns a bake;
- a downstream dependent is asked exactly once, at the scene's current frame,
  and is frozen only after its answer lands.

The dependent wait drives ``_timer``, the same pass an ordinary scene
event uses. It deliberately does not arm the playback gate: a bake is not
playback, and moving the playhead would make the single retained frame differ
from the frame the target was baked at.
"""

import time

import bpy

from .. import bridge
from .. import stack as stack_mod
from .lifecycle import freeze, request_again, unfreeze
from .messages import _handle_incoming
from .request import _set, _timeout
from .triggers import _timer


def token_stages(token):
    """Yield ``(object, stage)`` records carrying ``token`` in their marker.

    The marker is the only proof that a bake froze a stage. A user's manual
    freeze has no marker, so it is never returned here. Objects without a stack
    or stages removed since the bake simply contribute nothing.

    ``release_token`` may call ``unfreeze`` on a match, which can rebuild the
    index and sweep helper objects. It consumes this generator into a list
    before mutating, so the walk over ``bpy.data.objects`` is complete first.
    """
    if not token:
        return
    for candidate in bpy.data.objects:
        try:
            record = stack_mod.stack_of(candidate)
        except ReferenceError:
            continue
        if record is None or not record.stages:
            continue
        for stage in record.stages:
            if stage.frozen_by_bake == token:
                yield candidate, stage


def release_token(token, skip_baked=True):
    """Drop one bake's freeze markers and unfreeze the stages it owns.

    Returns the number of stages actually unfrozen. A stage with its own bake
    is left frozen and baked: its native data is not this token's to clear
    (D4 scopes Clear Bake to the selected stage). Its marker is still dropped,
    because the freeze it described now belongs to its own bake.
    """
    # Unfreezing calls ENGINE.ensure_active(), which rebuilds the index and
    # sweeps helper objects after a whole stack was deleted. Finish the walk
    # over bpy.data.objects before any of that mutation starts.
    matches = list(token_stages(token))
    count = 0
    for obj, stage in matches:
        stage.frozen_by_bake = ""
        if skip_baked and stage.baked:
            continue
        if not stage.frozen:
            continue
        stage.frozen = False
        unfreeze(obj, stage)
        count += 1
    return count


def freeze_stage(obj, stage, token=None, mark=True):
    """Freeze one stage, optionally recording the bake token that did it."""
    stage.frozen = True
    if mark:
        stage.frozen_by_bake = token or ""
    freeze(obj, stage)


def trigger_once(obj, stage, timeout=None):
    """Force one request and wait for its answer through the engine timer.

    Returns ``(ok, reason)``. ``ok`` is True only when the stage settled with
    the answer to this request (``ok``, or ``frozen`` for a stage the user had
    frozen: its Pull refreshes the output without changing its freeze). A
    disconnect, a solve error and the timeout all return False and a reason for
    the bake report.

    The caller owns the freeze state around this call. The stage must be
    unfrozen already, or frozen deliberately: ``request_again`` arms the
    one-shot Pull permit for the frozen case, so both paths ask exactly once.
    """
    if timeout is None:
        timeout = _timeout()
    started = time.monotonic()
    deadline = started + max(float(timeout), 0.05)
    # A stale error must not be read as this request's failure. The answer
    # path clears the error on success; the frozen error path keeps the state
    # frozen and writes the error here.
    _set(stage, "error", "")
    request_again(obj, stage)
    seen_waiting = False

    def outcome():
        """Where the forced request stands, or None while it is still out."""
        if not bridge.state["clients"]:
            return False, "Grasshopper disconnected"
        state = stage.state
        if state == stack_mod.STATE_ERROR:
            return False, stage.error or "Grasshopper error"
        if state == stack_mod.STATE_WAITING:
            # A frozen stage's error answer keeps `frozen` (and was left in
            # `waiting` by the message handler), so the error field is the
            # signal there. The request started with a cleared error, which
            # makes any error here this request's.
            if stage.error:
                return False, stage.error
            return None
        if seen_waiting and state in (
            stack_mod.STATE_OK,
            stack_mod.STATE_FROZEN,
        ):
            if stage.error:
                return False, stage.error
            return True, ""
        if state in (
            stack_mod.STATE_DISCONNECTED,
            stack_mod.STATE_UNBOUND,
        ):
            return False, state
        return None

    while time.monotonic() < deadline:
        # Apply any answer first: a settled answer must not let the same timer
        # pass continue into the next live dependent. The timer below is what
        # asks and what notices a superseded or timed-out request.
        _handle_incoming()
        settled = outcome()
        if settled is not None:
            return settled
        _timer()
        if stage.state == stack_mod.STATE_WAITING:
            seen_waiting = True
        settled = outcome()
        if settled is not None:
            return settled
        time.sleep(0.005)
    return False, "no answer within %.1f s" % (time.monotonic() - started)
