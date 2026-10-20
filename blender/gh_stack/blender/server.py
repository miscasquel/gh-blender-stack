"""WebSocket server on a background thread (plan §8.1, S2).

The dependency is the official pure-Python `websockets` wheel the extension
declares (Blender installs it into the extension's `_libs` on `sys.path`; the
development install places the same package where Blender already looks). No
code here manipulates `sys.path` — the Add-on Guidelines forbid it. The server
runs its own asyncio loop on a plain thread; the main thread talks to it only
through the bridge queues. The socket thread never touches bpy: the one bpy
read, the online-access gate, happens in `start()` on the caller's thread.
"""

import asyncio
import threading

import bpy

from ..core import codec
from . import bridge, prefs


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
        self._thread = threading.Thread(target=self._run, name="gh-stack-server", daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        # The serve loop polls the stop event every 100 ms and then closes its
        # connections on the live loop. Stopping the loop directly here would
        # make websockets schedule its close on a dead loop (noisy RuntimeError
        # at interpreter shutdown).
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        self._thread = None
        self._loop = None
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
        """Queue a frame from the main thread. False when there is no client to
        receive it: a dropped request must not be recorded as sent, or the caller
        waits out a whole timeout for an answer nobody was ever asked for."""
        if not bridge.state["clients"]:
            return False
        loop = self._loop
        if loop is None:
            return False
        loop.call_soon_threadsafe(self._enqueue, frame)
        return True

    def _enqueue(self, frame):
        if self._send_queue is not None:
            self._send_queue.put_nowait(frame)

    # ------------------------------------------------------------------ loop
    def _run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._serve())
        except Exception as exc:  # pragma: no cover - surfaced in the UI
            bridge.state["error"] = "server error: %r" % (exc,)
        finally:
            bridge.set_listening(False)
            bridge.set_disconnected("server stopped")
            loop.close()

    async def _serve(self):
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
                    while not self._stop.is_set():
                        await asyncio.sleep(0.1)
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
                    # (returning from this handler closes it).
                    code = codec.code_of(exc)
                    await _refuse(ws, code, str(exc))
                    if code == "proto_mismatch":
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
                prefs.log("[gh-stack] client disconnected: %r" % (exc,), flush=True)
            else:
                import traceback

                bridge.state["error"] = "handler error: %r" % (exc,)
                prefs.log("[gh-stack] handler error: %r\n%s" % (exc, traceback.format_exc()), flush=True)
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
