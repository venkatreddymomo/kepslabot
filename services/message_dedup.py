"""In-process idempotency store for chat messages.

Keyed by ``(session_id, message_id)``. Lets the WebSocket and HTTP chat entry
points dedupe a *retried* message within a short window so it is never processed
(answered / ticketed) twice. This is what makes the client's "no delivery ack in
15s -> resend over HTTP" path safe: if the original WS message actually did reach
the server, the HTTP retry returns the cached answer instead of running the whole
pipeline again.

Three outcomes from :meth:`claim`:
  * ``("owner", None)``      -> first time seen; caller must process and then
                               call :meth:`complete` (or :meth:`release_if_pending`
                               on failure).
  * ``("duplicate", result)`` -> a retry whose original already completed; caller
                               should return ``result`` without reprocessing.
  * ``("duplicate", None)``  -> a retry whose original is still in flight and did
                               not finish within the wait window; caller may
                               process (rare) — the dedupe key is not owned.

NOTE: single-worker / single-event-loop only. The store is a per-process dict, so
with multiple uvicorn workers each worker has its own copy. For a multi-worker
deployment this must be backed by a shared store (e.g. Redis). Documented here on
purpose; not implemented because the current deployment runs a single worker.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

# How long a record (in-flight or completed) is retained. Covers the client's
# delivery-ack (15s) + answer (45s, with patient re-waits) windows with margin.
_TTL_SECONDS = 300.0
# How long a duplicate waits for an in-flight owner to finish before giving up.
# Must comfortably exceed a slow LOCAL turn (~15-30s+) so a same-id retry returns
# the owner's cached answer instead of timing out and reprocessing.
_DUP_WAIT_SECONDS = 60.0
# Only take over an in-flight record as a NEW owner once it is this old — i.e.
# the original owner is presumed dead (it would normally release on error). This
# is deliberately far longer than any real turn so a slow-but-alive turn is
# WAITED on, never reprocessed (reprocessing double-runs the LLM and competes for
# the event loop, making the live turn even slower).
_TAKEOVER_SECONDS = 150.0

Key = tuple[str, str]


@dataclass
class _Entry:
    state: str  # "in_flight" | "completed"
    ts: float
    result: Any = None
    event: asyncio.Event = field(default_factory=asyncio.Event)


class MessageDedup:
    def __init__(self) -> None:
        self._store: dict[Key, _Entry] = {}
        self._lock = asyncio.Lock()

    def _evict_expired(self, now: float) -> None:
        stale = [k for k, e in self._store.items() if (now - e.ts) > _TTL_SECONDS]
        for k in stale:
            self._store.pop(k, None)

    async def claim(self, key: Key) -> tuple[str, Any]:
        """Atomically register ``key`` or detect that it's a retry.

        Returns ``("owner", None)`` if the caller should process the message, or
        ``("duplicate", result_or_None)`` if it's a retry.
        """
        now = time.monotonic()
        async with self._lock:
            self._evict_expired(now)
            entry = self._store.get(key)
            # Fresh, or a presumed-dead in-flight record we are allowed to take
            # over. A slow-but-alive turn (in-flight, younger than the takeover
            # threshold) is NOT taken over here — the duplicate falls through to
            # wait for it below and returns its cached answer.
            if entry is None or (
                entry.state == "in_flight" and (now - entry.ts) > _TAKEOVER_SECONDS
            ):
                self._store[key] = _Entry(state="in_flight", ts=now)
                return ("owner", None)

        # A duplicate. If the original already completed, hand back its result.
        if entry.state == "completed":
            return ("duplicate", entry.result)

        # Still in flight elsewhere — wait briefly for the owner to finish so the
        # retry can return the same answer instead of reprocessing.
        try:
            await asyncio.wait_for(entry.event.wait(), timeout=_DUP_WAIT_SECONDS)
        except Exception:
            return ("duplicate", None)
        return ("duplicate", entry.result if entry.state == "completed" else None)

    async def complete(self, key: Key, result: Any) -> None:
        """Mark ``key`` completed and cache its result; wakes any waiters."""
        now = time.monotonic()
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                entry = _Entry(state="completed", ts=now)
                self._store[key] = entry
            entry.state = "completed"
            entry.result = result
            entry.ts = now
            entry.event.set()
            self._evict_expired(now)

    async def release_if_pending(self, key: Key) -> None:
        """Drop an in-flight record (owner errored before completing) so a later
        retry can be processed. No-op if it already completed."""
        async with self._lock:
            entry = self._store.get(key)
            if entry is not None and entry.state == "in_flight":
                entry.event.set()
                self._store.pop(key, None)

    def stats(self) -> dict[str, int]:
        in_flight = sum(1 for e in self._store.values() if e.state == "in_flight")
        completed = sum(1 for e in self._store.values() if e.state == "completed")
        return {"in_flight": in_flight, "completed": completed, "total": len(self._store)}


# Module-level singleton shared by the WS handler and the HTTP endpoint (same
# process / event loop).
message_dedup = MessageDedup()
