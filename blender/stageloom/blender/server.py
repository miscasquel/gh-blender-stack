"""WebSocket server on a background thread.

The dependency is the official pure-Python `websockets` wheel the extension
declares (Blender installs it into the extension's `_libs` on `sys.path`; the
development install places the same package where Blender already looks). No
code here manipulates `sys.path` — the Add-on Guidelines forbid it. The server
runs its own asyncio loop on a plain thread; the main thread talks to it only
through the bridge queues. The socket thread never touches bpy: the one bpy
read, the online-access gate, happens in `start()` on the caller's thread.
"""

import asyncio
import concurrent.futures
import threading

import bpy

from ..core import codec
from . import bridge, prefs

# How long the main thread waits for the loop to make the enqueue decision.
# It is a round trip to a thread that is already running, not a network wait;
# the cap only keeps a wedged loop from freezing the frame handler.
SEND_DECISION_TIMEOUT = 0.2
# How long stop() waits for the socket thread. The thread is daemon and gets a
# wake-up on its own loop, so a healthy stop finishes in milliseconds. The cap
# is only the fallback for a loop wedged in code the stop event cannot reach;
# the old unconditional 3 s join blocked the UI for the full cap in that case.
STOP_JOIN_TIMEOUT = 1.0


def _load_websockets():
    """Import websockets, the one declared dependency."""
    try:
        import websockets as ws
        from websockets.asyncio.server import serve as ws_serve

        return ws, ws_serve, None
    except ImportError as exc:  # pragma: no cover - reported in the UI
        return None, None, exc


websockets, serve, IMPORT_ERROR = _load_websockets()


async def _refuse(ws, code, message):
    """Answer an unusable frame with a protocol `error` frame (§4.6). Never
    raises into the read loop: a peer that cannot be told can only be dropped."""
    try:
        await ws.send(codec.encode_frame({"type": "error", "code": code, "message": message}))
    except Exception:
        pass


class GhServer(object):
    def __init__(self):
        self._thread = None
        self._loop = None
        self._stop = threading.Event()
        self._stop_event = None  # asyncio.Event, woken from stop()
        self._port = None
        self._send_queue = None  # asyncio.Queue, created on the loop
        self._clients = []  # live sockets; only touched on the loop thread

    # ------------------------------------------------------------------ start
    def start(self, port):
        if not websockets:
            bridge.state["error"] = "websockets missing: %s" % (IMPORT_ERROR,)
            return False
        if not bpy.app.online_access:
            # The `network` permission contract: the listening socket is the
            # add-on's only network use, and Blender's switch is the user's
            # consent. Checked on every start, so flipping the preference and
            # pressing Start works without a restart. A notice, not an error —
            # an offline session is a legitimate state.
            bridge.state["notice"] = (
                "Online access is disabled — enable it in Preferences > System"
                + (" (locked by the command line)" if bpy.app.online_access_override else "")
            )
            return False
        bridge.state["notice"] = ""
        if self._thread and self._thread.is_alive() and self._port == port:
            return True
        self.stop()
        self._port = port
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="stageloom-server", daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        # Wake the serve loop instead of waiting out its poll interval. The
        # serve task still closes its connections on the live loop; stopping
        # the loop directly here would make websockets schedule its close on a
        # dead loop (noisy RuntimeError at interpreter shutdown).
        loop = self._loop
        stop_event = self._stop_event
        if loop is not None and stop_event is not None:
            try:
                loop.call_soon_threadsafe(stop_event.set)
            except RuntimeError:
                # The loop closed between the read and the scheduling.
                pass
        if self._thread is not None:
            self._thread.join(timeout=STOP_JOIN_TIMEOUT)
        self._thread = None
        self._loop = None
        self._stop_event = None
        bridge.set_listening(False)
        bridge.set_disconnected("server stopped")

    @property
    def running(self):
        return bool(self._thread and self._thread.is_alive())

    @property
    def port(self):
        """The port actually bound, or None when nothing is listening. The panel
        reports this rather than the preference: the two disagreed once."""
        return self._port if self.running else None

    # ------------------------------------------------------------------- send
    def send(self, frame):
        """Queue a frame from the main thread, returning whether it was queued.

        The observation of ``_clients`` and the enqueue happen in one callback
        on the loop thread: ``_clients`` is owned there, so a disconnect cannot
        land between the check and the put. The caller records the request as
        sent only when this returns True; a False leaves the stage free to ask
        again instead of waiting out a timeout for a frame nobody carried.
        """
        if not bridge.state["clients"]:
            # Fast negative only: a stale positive is resolved by the atomic
            # decision below, and this avoids a loop round trip when there is
            # provably no socket to carry a frame.
            return False
        loop = self._loop
        if loop is None:
            return False
        decision = concurrent.futures.Future()

        def enqueue_if_connected():
            # A timed-out caller cancelled the decision; do not enqueue after
            # saying the send failed.
            if not decision.set_running_or_notify_cancel():
                return
            if not self._clients or self._send_queue is None:
                decision.set_result(False)
                return
            try:
                self._send_queue.put_nowait(frame)
            except Exception as exc:  # pragma: no cover - loop teardown
                decision.set_exception(exc)
            else:
                decision.set_result(True)

        try:
            loop.call_soon_threadsafe(enqueue_if_connected)
        except RuntimeError:
            # The loop closed between the read above and the scheduling.
            return False
        try:
            return bool(decision.result(timeout=SEND_DECISION_TIMEOUT))
        except concurrent.futures.TimeoutError:
            decision.cancel()
            return False
        except Exception:
            return False

    # ------------------------------------------------------------------ loop
    def _run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        stop_event = asyncio.Event()
        self._loop = loop
        self._stop_event = stop_event
        try:
            loop.run_until_complete(self._serve(stop_event))
        except Exception as exc:  # pragma: no cover - surfaced in the UI
            bridge.state["error"] = "server error: %r" % (exc,)
        finally:
            bridge.set_listening(False)
            bridge.set_disconnected("server stopped")
            # A stop that timed out can leave this thread finishing after a
            # restart already installed the next loop. Only clear our own.
            if self._loop is loop:
                self._loop = None
            if self._stop_event is stop_event:
                self._stop_event = None
            loop.close()

    async def _serve(self, stop_event):
        self._send_queue = asyncio.Queue()
        try:
            # The keepalive closes a peer that stops answering: with the
            # library defaults (20 s ping, 20 s timeout) a half-open socket
            # costs a whole request timeout (30 s) to notice, which is exactly
            # the window protocol §2 meant to close. The Grasshopper client
            # answers pings from its socket thread; SendFrame holds a lock, so
            # a pong never interleaves with a frame being written.
            async with serve(
                self._on_connect,
                "127.0.0.1",
                self._port,
                max_size=None,
                ping_interval=5.0,
                ping_timeout=20.0,
            ):
                bridge.set_listening(True)
                # Bind succeeded, so anything a previous bind or a previous
                # shutdown wrote here is stale: leaving it up shows a running
                # server as stopped, which is how a restart looks broken.
                bridge.state["error"] = ""
                sender = asyncio.create_task(self._sender())
                try:
                    # stop() wakes this immediately. A start/stop race before
                    # the loop was ready leaves `_stop` set; honour it here.
                    if not self._stop.is_set():
                        await stop_event.wait()
                finally:
                    sender.cancel()
        except OSError as exc:
            bridge.state["error"] = "cannot listen on %s: %s" % (self._port, exc)

    async def _sender(self):
        while True:
            frame = await self._send_queue.get()
            for ws in list(self._clients):
                try:
                    await ws.send(frame)
                except Exception:
                    pass

    async def _on_connect(self, ws):
        self._clients.append(ws)
        bridge.set_clients(len(self._clients))
        try:
            async for message in ws:
                if isinstance(message, str):
                    message = message.encode("utf-8")
                try:
                    header, sections = codec.decode_frame(message)
                except codec.CodecError as exc:
                    bridge.state["error"] = "bad frame: %s" % (exc,)
                    # Answer with the protocol's own code (§4.6) instead of
                    # silence: a wrong version additionally ends the connection
                    # (returning from this handler closes it). The codec carries
                    # the code on the exception; never infer it from text.
                    await _refuse(ws, exc.code, str(exc))
                    if exc.code == "proto_mismatch":
                        break
                    continue
                if header.get("type") == "hello":
                    bridge.set_connected(header.get("doc", ""))
                bridge.post_incoming(header, sections)
        except Exception as exc:
            # An abrupt disconnect is normal (GH closed, Blender quit); a handler
            # bug is not. Only the latter deserves a traceback in the log.
            closed = False
            try:
                import websockets.exceptions

                closed = isinstance(exc, websockets.exceptions.ConnectionClosed)
            except Exception:
                pass
            if closed:
                prefs.log("[stageloom] client disconnected: %r" % (exc,), flush=True)
            else:
                import traceback

                bridge.state["error"] = "handler error: %r" % (exc,)
                prefs.log("[stageloom] handler error: %r\n%s" % (exc, traceback.format_exc()), flush=True)
        finally:
            try:
                self._clients.remove(ws)
            except ValueError:
                pass
            bridge.set_clients(len(self._clients))
            if not self._clients:
                bridge.set_disconnected("client disconnected")


SERVER = GhServer()


def unregister():
    SERVER.stop()
