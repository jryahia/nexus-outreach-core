"""The Outpost - fire-and-forget webhooks to an external automation endpoint.

NEXUS runs on one machine. This module is the single, deliberate exception:
when ``NEXUS_WEBHOOK_URL`` is set, operational events are posted to it so an
n8n / GoHighLevel / WhatsApp flow can relay a live feed to a phone.

Three rules hold this together, and all three exist because the alternative
breaks a campaign that is halfway through a list:

* **Nothing blocks.** ``fire()`` drops a payload on a queue and returns. The
  POST happens on a daemon worker. A webhook that takes four seconds to answer
  must not add four seconds to the gap between two emails.
* **Nothing raises.** A refused connection, a DNS failure, a 500, a timeout:
  every one of them is counted and dropped. The campaign never learns.
* **Nothing personal leaves by default.** The payload carries the event, the
  timestamp and counters. Addresses, names and message bodies are only
  included when ``NEXUS_WEBHOOK_DETAIL`` is switched on, because this is the
  one code path that sends scraped third-party data off the machine.

The URL itself is treated as a credential. n8n and GoHighLevel both embed an
auth token in the path, so it is never logged, never shown in the UI and never
returned by the diagnostics - only ``redact()`` of it is.
"""

from __future__ import annotations

import json
import queue
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

# The queue is bounded. An unreachable endpoint with an unbounded queue is a
# slow memory leak that only shows up on the longest campaigns.
MAX_QUEUED = 500
DEFAULT_TIMEOUT = 5.0
USER_AGENT = "NEXUS-Outreach-Core/1.0"

# Events the engine emits. Named here so a caller cannot invent a string that
# the receiving automation has never heard of.
CAMPAIGN_STARTED = "campaign.started"
EMAIL_SENT = "email.sent"
EMAIL_FAILED = "email.failed"
EMAIL_SKIPPED = "email.skipped"
CAMPAIGN_FINISHED = "campaign.finished"
HUNT_FINISHED = "hunt.finished"
TARGET_ACQUIRED = "target.acquired"

EVENTS = (CAMPAIGN_STARTED, EMAIL_SENT, EMAIL_FAILED, EMAIL_SKIPPED,
          CAMPAIGN_FINISHED, HUNT_FINISHED, TARGET_ACQUIRED)

# Fields that identify a scraped third party. Held back unless detail is on.
PERSONAL_FIELDS = ("email", "name", "website", "phone", "handle", "subject",
                   "detail", "location")


def redact(url: str) -> str:
    """A webhook URL reduced to something safe to show.

    Keeps the scheme and host so the user can confirm which service is
    configured, and drops the path, which is where the token lives.
    """
    if not url:
        return "not set"
    try:
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        if not parts.hostname:
            return "set (unparsable)"
        return f"{parts.scheme}://{parts.hostname}/***"
    except Exception:
        return "set"


class WebhookOutpost:
    """A queue and one daemon worker that POSTs JSON to a single endpoint.

    Built on ``urllib`` from the standard library rather than requests: this
    runs inside a campaign worker thread, and the fewer third-party layers
    between a send loop and a socket, the fewer ways a dependency upgrade can
    stall one.
    """

    def __init__(self, url: str = "", *, timeout: float = DEFAULT_TIMEOUT,
                 include_personal: bool = False, source: str = "nexus") -> None:
        self.url = (url or "").strip()
        self.timeout = timeout
        self.include_personal = include_personal
        self.source = source
        self.sent = 0
        self.failed = 0
        self.dropped = 0
        self.last_error = ""
        self._queue: queue.Queue[dict | None] = queue.Queue(maxsize=MAX_QUEUED)
        self._worker: threading.Thread | None = None
        self._lock = threading.Lock()

    # -- state ---------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(self.url)

    @property
    def target(self) -> str:
        return redact(self.url)

    def stats(self) -> dict:
        return {"enabled": self.enabled, "target": self.target,
                "sent": self.sent, "failed": self.failed,
                "dropped": self.dropped, "queued": self._queue.qsize(),
                "last_error": self.last_error}

    # -- sending -------------------------------------------------------------
    def build_payload(self, event: str, fields: dict[str, Any]) -> dict:
        """The JSON body. Personal fields are stripped unless switched on."""
        body = {
            "source": self.source,
            "event": event,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "data": {},
        }
        for key, value in fields.items():
            if key in PERSONAL_FIELDS and not self.include_personal:
                continue
            body["data"][key] = value
        if not self.include_personal:
            # Say so in the payload itself, so a flow that expected an address
            # and found none knows why rather than treating it as a bug.
            body["redacted"] = True
        return body

    def fire(self, event: str, **fields: Any) -> bool:
        """Queue one event. Returns False when it was not accepted.

        Never blocks and never raises. A full queue means the endpoint is not
        keeping up, and the right answer there is to drop the newest event and
        carry on rather than to slow the campaign down to the webhook's speed.
        """
        if not self.enabled:
            return False
        payload = self.build_payload(event, fields)
        self._ensure_worker()
        try:
            self._queue.put_nowait(payload)
            return True
        except queue.Full:
            self.dropped += 1
            return False

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(target=self._drain, name="outpost",
                                            daemon=True)
            self._worker.start()

    def _drain(self) -> None:
        while True:
            payload = self._queue.get()
            if payload is None:
                self._queue.task_done()
                return
            self._post(payload)
            self._queue.task_done()

    def _post(self, payload: dict) -> bool:
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.url, data=data, method="POST",
            headers={"Content-Type": "application/json",
                     "User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                if 200 <= response.status < 300:
                    self.sent += 1
                    return True
                self.failed += 1
                self.last_error = f"HTTP {response.status}"
                return False
        except urllib.error.HTTPError as exc:
            self.failed += 1
            self.last_error = f"HTTP {exc.code}"
        except urllib.error.URLError as exc:
            self.failed += 1
            # str(exc) can carry the URL, which carries the token.
            self.last_error = f"{type(exc.reason).__name__ if exc.reason else 'URLError'}"
        except Exception as exc:
            self.failed += 1
            self.last_error = type(exc).__name__
        return False

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait for the queue to empty. Used by tests, not by the send path."""
        if not self.enabled:
            return True
        deadline = threading.Event()
        waiter = threading.Thread(target=lambda: (self._queue.join(),
                                                  deadline.set()), daemon=True)
        waiter.start()
        return deadline.wait(timeout)

    def close(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass


# A module-level instance so every caller shares one queue and one worker.
# Rebuilt by ``configure`` whenever the .env changes underneath a rerun.
_outpost = WebhookOutpost()


def configure(url: str, *, include_personal: bool = False,
              timeout: float = DEFAULT_TIMEOUT) -> WebhookOutpost:
    """Point the shared outpost at an endpoint. Safe on every rerun."""
    global _outpost
    url = (url or "").strip()
    if (url != _outpost.url or include_personal != _outpost.include_personal):
        _outpost.close()
        _outpost = WebhookOutpost(url, timeout=timeout,
                                  include_personal=include_personal)
    return _outpost


def outpost() -> WebhookOutpost:
    return _outpost


def fire(event: str, **fields: Any) -> bool:
    """Fire on the shared outpost. A no-op when no URL is configured."""
    return _outpost.fire(event, **fields)
