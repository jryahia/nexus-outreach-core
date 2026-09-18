"""Background job machinery.

Streamlit re-runs the whole script on every interaction. Anything that blocks -
a scrape, or a campaign that waits minutes between sends - must therefore live
in a worker thread, or the UI freezes and the STOP button becomes unclickable.

Two rules that this module exists to enforce:

1. Worker threads have no ScriptRunContext, so they must never call ``st.*``.
   They report progress into a plain dict guarded by a lock; the UI polls it.
2. Waiting is done with ``stop_event.wait(seconds)``, never ``time.sleep``.
   ``wait`` returns the instant the flag is set, so STOP is immediate instead
   of being honoured only after the current delay has run out.
"""

from __future__ import annotations

import threading
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

import streamlit as st

IDLE = "idle"
RUNNING = "running"
DONE = "done"
STOPPED = "stopped"
ERROR = "error"

_TERMINAL = {DONE, STOPPED, ERROR}


@dataclass
class Job:
    """Thread-safe handle for one long-running operation."""

    key: str
    status: str = IDLE
    current: int = 0
    total: int = 0
    message: str = ""
    log: list[str] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)
    state: dict = field(default_factory=dict)
    result: Any = None
    error: str = ""
    stop_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # -- called from the worker thread -------------------------------------
    def report(
        self,
        message: str | None = None,
        current: int | None = None,
        total: int | None = None,
        log: str | None = None,
        row: dict | None = None,
        state: dict | None = None,
    ) -> None:
        with self._lock:
            if message is not None:
                self.message = message
            if current is not None:
                self.current = current
            if total is not None:
                self.total = total
            if log is not None:
                self.log.append(log)
                del self.log[:-400]
            if row is not None:
                # Structured twin of the text log - what the Analytics table reads.
                self.rows.append(row)
                del self.rows[:-2000]
            if state is not None:
                # What the worker is doing right now, as data rather than prose,
                # so the UI can draw a countdown instead of a frozen-looking bar.
                self.state = dict(state)

    @property
    def cancelled(self) -> bool:
        return self.stop_event.is_set()

    def wait(self, seconds: float) -> bool:
        """Interruptible sleep. Returns True if STOP was pressed while waiting."""
        return self.stop_event.wait(seconds)

    # -- called from the Streamlit script thread ---------------------------
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "status": self.status,
                "current": self.current,
                "total": self.total,
                "message": self.message,
                "log": list(self.log),
                "rows": list(self.rows),
                "state": dict(self.state),
                "result": self.result,
                "error": self.error,
            }

    @property
    def is_running(self) -> bool:
        return self.status == RUNNING

    @property
    def fraction(self) -> float:
        with self._lock:
            if self.total <= 0:
                return 0.0
            return min(1.0, self.current / self.total)


def get_job(key: str) -> Job:
    """Fetch (or create) the Job stored under ``key`` in session state."""
    jobs: dict[str, Job] = st.session_state.setdefault("_jobs", {})
    if key not in jobs:
        jobs[key] = Job(key=key)
    return jobs[key]


def start_job(key: str, fn: Callable[..., Any], /, **kwargs: Any) -> Job:
    """Run ``fn(job=job, **kwargs)`` on a daemon thread.

    ``fn`` receives the Job as its ``job`` keyword argument and should use
    ``job.report(...)`` for progress and ``job.cancelled`` / ``job.wait(...)``
    for cancellation. Its return value lands in ``job.result``.

    Both parameters are positional-only, and that slash is load-bearing. The
    scrapers take a keyword called ``target``; while this function's own second
    parameter was also named ``target``, every call that forwarded one -
    Instagram, TikTok, Reddit, Discord - died on the spot with
    "start_job() got multiple values for argument 'target'" before the worker
    thread was ever created. Positional-only makes that class of collision
    impossible for any keyword a scraper might take, now or later.
    """
    job = get_job(key)
    if job.is_running:
        return job

    # Inside the lock: a STOP that lands between the fresh Event and the status
    # change would otherwise set the discarded Event and be lost.
    with job._lock:
        job.stop_event = threading.Event()
        job.status = RUNNING
        job.current = 0
        job.total = 0
        job.message = "Starting"
        job.log = []
        job.rows = []
        job.state = {}
        job.result = None
        job.error = ""

    def _runner() -> None:
        try:
            value = fn(job=job, **kwargs)
            with job._lock:
                job.result = value
                job.status = STOPPED if job.stop_event.is_set() else DONE
                if job.status == STOPPED:
                    job.message = "Stopped"
        except Exception as exc:  # surfaced in the UI, not swallowed
            with job._lock:
                job.status = ERROR
                job.error = f"{type(exc).__name__}: {exc}"
                job.log.append(traceback.format_exc(limit=6))

    job.thread = threading.Thread(target=_runner, name=f"job-{key}", daemon=True)
    job.thread.start()
    return job


def stop_job(key: str) -> None:
    """Signal cancellation. The worker unblocks from its wait immediately."""
    job = get_job(key)
    with job._lock:  # read the current Event, never one start_job just replaced
        event = job.stop_event
    event.set()  # outside the lock: report() takes the same non-reentrant lock
    job.report(message="Stop requested - finishing current step")


def reset_job(key: str) -> None:
    job = get_job(key)
    if job.is_running:
        return
    st.session_state["_jobs"][key] = Job(key=key)
