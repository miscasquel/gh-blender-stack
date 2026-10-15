"""WebSocket server on a background thread (plan §8.1, S2).

`websockets` 17.2 is vendored at ``gh_stack/vendor``. The server runs its own
asyncio loop on a plain thread; the main thread talks to it only through the
bridge queues. The socket thread never touches bpy (plan §3, principle 7).
"""

import asyncio
import os
import sys
import threading

from .. import core  # noqa: F401
from ..core import codec
from . import bridge

VENDOR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vendor")


def _load_websockets():
    """Import websockets, preferring an installed copy.

    The extension ships the official pure-Python wheel, which Blender installs
    into the extension's _libs directory (on sys.path while the extension is
    enabled). The vendored copy under vendor/ is the fallback for development
    installs (scripts/install-addon), which have no wheel.
    """
    try:
        import websockets as ws
        from websockets.asyncio.server import serve as ws_serve

        return ws, ws_serve, None
    except ImportError as first_error:
        if VENDOR not in sys.path:
            sys.path.insert(0, VENDOR)
        try:
            import websockets as ws
            from websockets.asyncio.server import serve as ws_serve

            return ws, ws_serve, None
        except ImportError as exc:  # pragma: no cover - reported in the UI
            return None, None, exc or first_error


websockets, serve, IMPORT_ERROR = _load_websockets()


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
                print("[gh-stack] client disconnected: %r" % (exc,), flush=True)
            else:
                import traceback

                bridge.state["error"] = "handler error: %r" % (exc,)
                print("[gh-stack] handler error: %r\n%s" % (exc, traceback.format_exc()), flush=True)
        finally:
            try:
                self._clients.remove(ws)
            except ValueError:
                pass
            bridge.set_clients(len(self._clients))
            if not self._clients:
                bridge.set_disconnected("client disconnected")


SERVER = GhServer()


def register():
    pass


def unregister():
    SERVER.stop()
