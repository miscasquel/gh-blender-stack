"""The per-stage decision function (plan §9).

Given the current request ID for a stage, what a fresh response says, and the
stage's state, decide what the engine should do. This is the heart of the apply
rules in protocol §6.4, kept pure so it can be exhaustively tested in layer A.
"""

# Stage states (plan §9.2).
IDLE = "idle"
WAITING = "waiting"
OK = "ok"
ERROR = "error"
DISCONNECTED = "disconnected"
DISABLED = "disabled"
FROZEN = "frozen"
UNBOUND = "unbound"

# What the engine does with an incoming response.
APPLY = "apply"          # set the helper data from this response
CACHE_ONLY = "cache"     # store under its rid, do not apply
IGNORE = "ignore"        # duplicate, nothing to do
ERROR_OUT = "error"      # status is error

# Request decisions.
SEND_FULL = "send_full"      # send a request with the geometry sections
SEND_REF = "send_ref"        # send only the hash
USE_CACHE = "use_cache"      # a cached result satisfies this rid
NOTHING = "nothing"          # stage is disabled, frozen, or unbound


class Decision(object):
    """The result of one engine step for one stage."""

    def __init__(self, action, **kwargs):
        self.action = action
        self.reason = kwargs.pop("reason", None)
        self.rid = kwargs.pop("rid", None)
        self.geom_mode = kwargs.pop("geom_mode", None)
        self.out_hash = kwargs.pop("out_hash", None)
        self.__dict__.update(kwargs)

    def __repr__(self):
        return "Decision(%r, rid=%r, geom_mode=%r, reason=%r)" % (
            self.action, self.rid, self.geom_mode, self.reason,
        )

    def __eq__(self, other):
        return isinstance(other, Decision) and vars(self) == vars(other)


def decide_response(stage_key, response_rid, response_out_hash, current_rid, applied_out_hash,
                    status=OK, cached_rids=None, rid_nonce=None):
    """Decide what to do with a response (protocol §6.4).

    ``cached_rids`` is a set of rids already stored; the caller puts the result
    away afterwards. ``status`` is the response's status field.
    """
    if response_rid is None:
        return Decision(IGNORE, reason="response has no rid")

    if status == ERROR:
        # Rule 5: an error keeps the last good output and sets error.
        return Decision(ERROR_OUT, rid=response_rid)

    if response_rid != current_rid:
        # Rule 2: a response for another rid is cached, never applied.
        return Decision(CACHE_ONLY, rid=response_rid, reason="other rid")

    if response_out_hash == applied_out_hash:
        # Already applied: this is the echo or a repeat.
        return Decision(IGNORE, rid=response_rid, reason="already applied")

    # Rule 1 and rule 3: matches the current rid and has a new hash.
    return Decision(APPLY, rid=response_rid, out_hash=response_out_hash)


def decide_request(stage_key, current_rid, cache, geom_changed, connected=True,
                   state=WAITING, gh_holds_geom=True):
    """Decide what to send for a stage on this tick (plan §9.1).

    ``cache`` maps rid to a response, so a cache hit needs no message at all.
    ``geom_changed`` says whether the geometry hash moved since the last send.
    ``gh_holds_geom`` is false when GH has lost the geometry for this stage (the
    first request after a connection, when ``ref`` is not safe).
    """
    if not connected:
        return Decision(NOTHING, reason="disconnected")
    if state in (DISABLED, FROZEN, UNBOUND):
        return Decision(NOTHING, reason=state)
    if current_rid is None:
        return Decision(NOTHING, reason="no rid yet")

    if current_rid in (cache or {}):
        # Rule 4 of the apply rules: a cached result for the current rid is applied
        # without contacting GH.
        return Decision(USE_CACHE, rid=current_rid, reason="cache hit")

    if geom_changed or not gh_holds_geom:
        # ``full`` sends the sections; also used when GH may have lost the geometry.
        return Decision(SEND_FULL, rid=current_rid, geom_mode="full")
    # ``ref`` sends only the hash: parameter-only changes carry no geometry.
    return Decision(SEND_REF, rid=current_rid, geom_mode="ref")


def next_stage_state(state, *, connected=False, has_gh_document=False, frozen=False,
                     disabled=False):
    """State transitions the engine makes between ticks (plan §9.2)."""
    if disabled:
        return DISABLED
    if frozen:
        return FROZEN
    if not connected:
        return DISCONNECTED
    if not has_gh_document:
        return UNBOUND
    return state if state in (WAITING, OK, ERROR) else WAITING
