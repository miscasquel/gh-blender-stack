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
if VENDOR not in sys.path:
    sys.path.insert(0, VENDOR)

try:
    import websockets
    from websockets.asyncio.server import serve
except ImportError as exc:  # pragma: no cover - reported in the UI
    websockets = None
    serve = None
    IMPORT_ERROR = exc
else:
    IMPORT_ERROR = None


class GhServer(object):
    def __init__(self):
        self._thread = None
        self._loop = None
        self._stop = threading.Event()
        self._port = None
        self._send_queue = None  # asyncio.Queue, created on the loop

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
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
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
        """Queue a frame from the main thread. The loop drains it."""
        loop = self._loop
        if loop is None:
            return
        loop.call_soon_threadsafe(self._enqueue, frame)

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
            async with serve(self._on_connect, "127.0.0.1", self._port, max_size=None):
                bridge.set_listening(True)
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
            for ws in list(getattr(self, "_clients", ())):
                try:
                    await ws.send(frame)
                except Exception:
                    pass

    async def _on_connect(self, ws):
        if not hasattr(self, "_clients"):
            self._clients = []
        self._clients.append(ws)
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
            # Never swallow silently: a handler bug closes the connection, and
            # that is exactly the kind of thing that is invisible from the UI.
            import traceback

            bridge.state["error"] = "handler error: %r" % (exc,)
            print("[gh-stack] handler error: %r\n%s" % (exc, traceback.format_exc()), flush=True)
        finally:
            try:
                self._clients.remove(ws)
            except ValueError:
                pass
            if not self._clients:
                bridge.set_disconnected("client disconnected")


SERVER = GhServer()


def register():
    pass


def unregister():
    SERVER.stop()
