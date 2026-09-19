"""The Kill-Feed - a loopback WebSocket that streams operations in real time.

Streamlit redraws on its own schedule. A fragment polling at one second is fine
for a progress bar and useless for a log: by the time the rerun lands, the
handshake it describes is already over. This module opens a WebSocket beside
the Streamlit server and pushes each line the instant it happens, so the HUD
terminal scrolls at the speed of the engine rather than the speed of a rerun.

Four decisions that keep it from becoming a liability:

* **Loopback only.** The socket binds ``127.0.0.1``. Never ``0.0.0.0``: the feed
  narrates campaigns and can carry addresses, and a machine on the same coffee
  shop network has no business reading it.
* **Token gated.** A random token is minted per process and handed to the page
  through the DOM. A connection without it is closed before the first frame.
  Any local process can reach a loopback port; the token is what makes reaching
  it useless.
* **One server per process.** A module-level guard, because Streamlit re-runs
  this file's importers constantly and a server per rerun would exhaust the
  port range in a minute.
* **Nothing blocks and nothing raises.** ``push()`` is called from campaign and
  scraper worker threads. It hands the line to the server's event loop and
  returns; if the loop is gone or no one is listening, the line goes into the
  ring buffer and that is the end of it.
"""

from __future__ import annotations

import asyncio
import secrets
import threading
from collections import deque
from datetime import datetime
from typing import Any

DEFAULT_PORT = 8765
PORT_ATTEMPTS = 12        # 8765..8776 before giving up
BACKLOG = 400             # lines replayed to a client that connects late

# Levels the HUD styles differently. Kept short: they travel on every line.
INFO = "info"
OK = "ok"
WARN = "warn"
FAIL = "fail"
FIRE = "fire"


class KillFeed:
    """A WebSocket broadcaster living on its own event loop in a daemon thread."""

    def __init__(self) -> None:
        self.port: int | None = None
        self.token: str = ""
        self.started = False
        self.error: str = ""
        self.pushed = 0
        self._clients: set[Any] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._backlog: deque[str] = deque(maxlen=BACKLOG)
        self._lock = threading.Lock()

    # -- lifecycle -----------------------------------------------------------
    def start(self, port: int = DEFAULT_PORT) -> bool:
        """Bring the server up once. Safe to call on every Streamlit rerun."""
        with self._lock:
            if self.started:
                return True
            try:
                import websockets  # noqa: F401
            except Exception as exc:
                self.error = f"websockets unavailable: {type(exc).__name__}"
                return False

            self.token = secrets.token_urlsafe(18)
            ready = threading.Event()
            self._thread = threading.Thread(
                target=self._run, args=(port, ready), name="killfeed",
                daemon=True)
            self._thread.start()
            # Wait for the bind to resolve so the caller can publish a real
            # port rather than a guess.
            ready.wait(timeout=8)
            self.started = self.port is not None
            return self.started

    def _run(self, first_port: int, ready: threading.Event) -> None:
        try:
            asyncio.run(self._serve(first_port, ready))
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            ready.set()

    async def _serve(self, first_port: int, ready: threading.Event) -> None:
        import websockets

        self._loop = asyncio.get_running_loop()
        server = None
        for offset in range(PORT_ATTEMPTS):
            candidate = first_port + offset
            try:
                server = await websockets.serve(
                    self._handler, "127.0.0.1", candidate,
                    ping_interval=20, ping_timeout=20, max_queue=64)
                self.port = candidate
                break
            except OSError:
                continue          # port taken; try the next one
        if server is None:
            self.error = (f"no free port in {first_port}-"
                          f"{first_port + PORT_ATTEMPTS - 1}")
            ready.set()
            return

        ready.set()
        try:
            await asyncio.Future()      # serve until the process exits
        finally:
            server.close()

    async def _handler(self, ws: Any) -> None:
        # The token rides in the query string. websockets exposes the raw
        # request path, which is all that is needed to check it.
        path = getattr(ws, "request", None)
        raw = getattr(path, "path", "") if path is not None else ""
        if self.token and f"token={self.token}" not in (raw or ""):
            await ws.close(code=4401, reason="unauthorised")
            return

        self._clients.add(ws)
        try:
            for line in list(self._backlog):
                await ws.send(line)
            async for _ in ws:
                pass                     # the feed is one-way; ignore input
        except Exception:
            pass
        finally:
            self._clients.discard(ws)

    # -- broadcasting --------------------------------------------------------
    def push(self, text: str, level: str = INFO) -> None:
        """Send one line. Called from worker threads; never blocks or raises."""
        if not text:
            return
        import json

        line = json.dumps({
            "t": datetime.now().strftime("%H:%M:%S"),
            "level": level,
            "text": str(text)[:400],
        })
        self._backlog.append(line)
        self.pushed += 1

        loop = self._loop
        if loop is None or loop.is_closed() or not self._clients:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._broadcast(line), loop)
        except Exception:
            pass

    async def _broadcast(self, line: str) -> None:
        for ws in list(self._clients):
            try:
                await ws.send(line)
            except Exception:
                self._clients.discard(ws)

    def stats(self) -> dict:
        return {"started": self.started, "port": self.port,
                "clients": len(self._clients), "pushed": self.pushed,
                "buffered": len(self._backlog), "error": self.error}


_feed = KillFeed()


def feed() -> KillFeed:
    return _feed


def start(port: int = DEFAULT_PORT) -> bool:
    return _feed.start(port)


def push(text: str, level: str = INFO) -> None:
    _feed.push(text, level)
