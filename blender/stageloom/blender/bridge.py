"""Queues between the server thread and the main thread (plan §11).

The socket thread never touches bpy. It decodes frames and puts them here; the
main thread drains them from a timer (5 ms while waiting or hot, 20 ms otherwise).
Outgoing frames go the other way. `state` is written by the socket thread and
read by the UI; the values are simple enough that no lock is needed.
"""

import queue
import time

INCOMING = queue.Queue()  # (header dict, sections dict, raw frame or None)

state = {
    "listening": False,
    "connected": False,
    # Live client sockets. `connected` says a `hello` has been seen; this says how
    # many sockets would actually receive a frame, which is what decides whether a
    # request can be sent at all.
    "clients": 0,
    "doc": "",
    "connected_at": 0.0,
    "error": "",
    "notice": "",
}


def post_incoming(header, sections, raw=None):
    """Queue one decoded frame with the bytes it arrived as, when available.

    The raw frame is the hot cache's storage form: decoding is deferred until a
    replay, and the disk store writes the same bytes instead of re-encoding a
    response the socket thread already had."""
    INCOMING.put((header, sections, raw))


def drain_incoming():
    """All queued (header, sections, raw) triples, oldest first."""
    items = []
    while True:
        try:
            items.append(INCOMING.get_nowait())
        except queue.Empty:
            break
    return items


def set_connected(doc):
    state["connected"] = True
    state["doc"] = doc or ""
    state["connected_at"] = time.time()
    state["error"] = ""


def set_disconnected(reason=""):
    state["connected"] = False
    state["doc"] = ""
    if reason:
        state["error"] = reason


def set_listening(listening):
    state["listening"] = bool(listening)


def set_clients(count):
    """How many client sockets the server is holding. A frame can only be
    delivered to a non-zero number of them."""
    state["clients"] = int(count)
