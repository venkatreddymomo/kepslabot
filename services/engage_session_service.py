"""
Engage session service — writes chat turns to KEBO_ENGAGE_SESSIONS.

Used by Lumira-compatible endpoints to log every guest/bot message so
Lumira frontend can read conversation history and analytics.

Design:
- write_turn() is always fire-and-forget — it NEVER blocks the chat response.
- Errors are logged silently; a failed write must never break the chat flow.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any

from config.settings import settings
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

_INSERT_SQL = text(
    """
    INSERT INTO KEBO_ENGAGE_SESSIONS
        (session_id, entity_id, group_id, widget_id, conversation_id,
         sender, message, render, media_type, url, message_type,
         total_cost, total_tokens, metadata, timestamp,
         from_responder, to_responder, from_agent_id, to_agent_id,
         is_conversation_active, use_for_context)
    VALUES
        (:session_id, :entity_id, :group_id, :widget_id, :conversation_id,
         :sender, :message, :render, :media_type, :url, :message_type,
         :total_cost, :total_tokens, :metadata, :ts,
         NULL, NULL, NULL, NULL, 1, 1)
    """
)

# The newest timestamp already used by this session, so the next row can be
# placed strictly after it.
_LAST_TS_SQL = text(
    """
    SELECT MAX(timestamp) AS last_ts
    FROM KEBO_ENGAGE_SESSIONS
    WHERE session_id = :session_id
    """
)


def _to_int_or_none(value: Any) -> int | None:
    """Convert string/int to int, return None if empty or not numeric."""
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return None


class EngageSessionService:
    """Writes one row per chat turn to KEBO_ENGAGE_SESSIONS."""

    def __init__(self) -> None:
        # Single FIFO worker so KES rows are inserted (and thus get their
        # auto-increment id / created_at) in the exact order write_turn_bg was
        # called — i.e. conversation order. Without this each turn was its own
        # asyncio.create_task racing to commit on its own connection, so the
        # kebo dashboard (which orders by id/created_at) rendered turns
        # scrambled. Bound to the running loop; rebuilt if the loop changes
        # (e.g. uvicorn --reload).
        self._maxsize = max(
            1,
            int(getattr(settings, "engage_session_queue_maxsize", 5000) or 5000),
        )
        self._queue: asyncio.Queue[dict[str, Any]] | None = None
        self._worker: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queued = 0
        self._written = 0
        self._dropped = 0

    @staticmethod
    async def _next_timestamp(session: AsyncSession, session_id: str) -> datetime:
        """A timestamp strictly later than every row this session already has.

        The column is a plain `timestamp` — whole seconds, no fractions — and a
        reply lands in the same second as its question about 85% of the time.
        Two rows sharing a second are indistinguishable to `ORDER BY timestamp`,
        which is what the dashboard runs (its index is widget_id + timestamp,
        with no id), so the reply could be drawn above the question that
        prompted it.

        Nudging the second row to the next whole second costs up to a second of
        precision on a gap that averages 0.21s, and buys an order that is right
        under ANY sort the dashboard applies, ascending or descending, with or
        without a tiebreaker. Real per-call latency is measured in
        new_bot_log_llm.latency_ms and is unaffected by this.
        """
        now = datetime.now()
        try:
            row = (await session.execute(_LAST_TS_SQL, {"session_id": session_id})).first()
            last = row[0] if row else None
            if last is not None:
                nxt = last + timedelta(seconds=1)
                if nxt > now:
                    return nxt
        except Exception:
            pass  # never block a chat write on this
        return now

    async def write_turn(
        self,
        *,
        session_id: str,
        sender: str,                     # "USER" or "BOT"
        message: str,
        entity_id: str = "",
        group_id: str = "",
        widget_id: str = "",
        conversation_id: str = "",
        render: str | None = None,       # lumira form_key if form was triggered
        message_type: str = "text",      # "text" or "form"
        total_cost: float = 0.0,
        total_tokens: int = 0,
        metadata: dict[str, Any] | None = None,
        db_session: AsyncSession | None = None,
    ) -> int | None:
        """
        Insert one row into KEBO_ENGAGE_SESSIONS. Returns the new row's
        auto-increment id, or None if the write failed.

        That id is what a ticket must carry as `message_id` so the dashboard
        can draw it inline against this exact turn. Nothing on the turn path
        waits for it: ticketing_service resolves the row at POST time instead
        (see TicketingService._resolve_engage_message_id), because this write
        runs ~1.5s against the kebo DB and the guest is waiting on the reply.

        Call via write_turn_bg() when you only need the row written and do not
        need its id, so the main response path is never blocked.
        """
        try:
            params = {
                "session_id": str(session_id or ""),
                # entity_id / group_id / widget_id are INT columns in the real table
                "entity_id": _to_int_or_none(entity_id),
                "group_id": _to_int_or_none(group_id),
                "widget_id": _to_int_or_none(widget_id),
                "conversation_id": str(conversation_id or "") or None,
                "sender": str(sender or "BOT").upper(),
                "message": str(message or ""),
                "render": str(render).strip() if render else None,
                "media_type": "text",
                "url": None,
                "message_type": str(message_type or "text"),
                "total_cost": float(total_cost or 0.0),
                "total_tokens": int(total_tokens or 0),
                "metadata": (
                    json.dumps(metadata, ensure_ascii=False, default=str)
                    if metadata
                    else None
                ),
            }

            if db_session is not None:
                params["ts"] = await self._next_timestamp(db_session, params["session_id"])
                result = await db_session.execute(_INSERT_SQL, params)
                # Don't commit here — caller owns the transaction.
                # Use a separate session for truly fire-and-forget writes.
                return getattr(result, "lastrowid", None)

            from models.database import AsyncSessionLocal
            async with AsyncSessionLocal() as session:
                params["ts"] = await self._next_timestamp(session, params["session_id"])
                result = await session.execute(_INSERT_SQL, params)
                # Read lastrowid BEFORE the commit closes the cursor.
                new_id = getattr(result, "lastrowid", None)
                await session.commit()
                return new_id

        except Exception as exc:
            logger.warning(
                "engage_session write_turn failed (silent): session_id=%s sender=%s error=%r",
                session_id,
                sender,
                exc,
            )
            return None

    def write_turn_bg(self, **kwargs: Any) -> None:
        """
        Enqueue write_turn onto the single FIFO worker.

        Call this inside a running async request handler so the DB write never
        delays the response being sent to the guest. Unlike a bare
        ``asyncio.create_task`` per turn, every enqueued write is committed
        sequentially in call order, so each KES row's auto-increment id (and
        created_at) reflects conversation order — which is what the kebo
        dashboard sorts by.

        Example:
            engage_session_service.write_turn_bg(
                session_id=session_id,
                sender="USER",
                message=request.message,
                entity_id=entity_id,
            )
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No event loop (e.g. during tests) — skip silently.
            return

        self._ensure_worker(loop)

        try:
            self._queue.put_nowait(kwargs)
            self._queued += 1
        except asyncio.QueueFull:
            self._dropped += 1
            if self._dropped in {1, 10} or self._dropped % 100 == 0:
                logger.warning(
                    "engage_session queue full; dropping turn writes (dropped=%s maxsize=%s)",
                    self._dropped,
                    self._maxsize,
                )
        except Exception:
            logger.debug("engage_session enqueue skipped", exc_info=True)

    def _ensure_worker(self, loop: asyncio.AbstractEventLoop) -> None:
        """(Re)build the queue + worker if missing or bound to a stale loop."""
        if self._queue is None or self._loop is not loop:
            self._queue = asyncio.Queue(maxsize=self._maxsize)
            self._loop = loop
            self._worker = loop.create_task(self._drain())
        elif self._worker is None or self._worker.done():
            self._worker = loop.create_task(self._drain())

    async def _drain(self) -> None:
        """Serially commit queued turns in FIFO (conversation) order."""
        assert self._queue is not None
        while True:
            kwargs = await self._queue.get()
            try:
                await self.write_turn(**kwargs)
                self._written += 1
            except Exception as exc:
                # write_turn already swallows its own errors; this is a
                # last-resort guard so one bad item never kills the worker.
                logger.warning("engage_session drain item failed (silent): %r", exc)
            finally:
                self._queue.task_done()

    def stats(self) -> dict[str, int]:
        return {
            "queued_total": self._queued,
            "written": self._written,
            "dropped": self._dropped,
            "maxsize": self._maxsize,
            "queued_now": self._queue.qsize() if self._queue is not None else 0,
            "worker_running": 1 if self._worker is not None and not self._worker.done() else 0,
        }


engage_session_service = EngageSessionService()
