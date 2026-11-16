"""Queues between the server thread and the main thread.

The socket thread never touches bpy. It decodes frames and puts them here; while
the engine is active, the main thread drains them from a timer (5 ms while
waiting or hot, 20 ms otherwise). A file with no stages has no timer at all.
Outgoing frames go the other way. `state` is written by the socket thread and
read by the UI; the values are simple enough that no lock is needed.
"""

import queue
import time

INCOMING = queue.Queue()  # (header dict, sections dict)

state = {
    "listening": False,
    "connected": False,
    # Live client sockets. `connected` says a `hello` has been seen; this says how
    # many sockets would actually receive a frame, which is what decides whether a
    # request can be sent at all.
    "clients": 0,
    "doc": "",
    "connected_at": 0.0,
    # The `caps` list from the peer's last `hello` (protocol §8). Absent means
    # no capabilities, never the newest; the sender falls back to one
    # single-part definition per part rather than emitting a multi-part frame
    # an old peer cannot read.
    "caps": [],
    "error": "",
    "notice": "",
}


def post_incoming(header, sections):
    """Queue one decoded frame for the main thread."""
    INCOMING.put((header, sections))


def drain_incoming():
    """All queued (header, sections) pairs, oldest first."""
    items = []
    while True:
        try:
            items.append(INCOMING.get_nowait())
        except queue.Empty:
            break
    return items


def clear_incoming():
    """Discard every queued frame.

    A file load or a fresh registration starts a new session: a decoded frame
    from the previous document must not be applied to the new one, even when a
    stage key happens to coincide. Returns how many frames were dropped.
    """
    return len(drain_incoming())


def set_connected(doc):
    state["connected"] = True
    state["doc"] = doc or ""
    state["connected_at"] = time.time()
    state["error"] = ""


def set_disconnected(reason=""):
    state["connected"] = False
    state["doc"] = ""
    # The next peer negotiates its own capabilities; keeping this client's
    # would let a later send claim a capability nobody advertised.
    state["caps"] = []
    if reason:
        state["error"] = reason


def set_listening(listening):
    state["listening"] = bool(listening)


def set_clients(count):
    """How many client sockets the server is holding. A frame can only be
    delivered to a non-zero number of them."""
    state["clients"] = int(count)
