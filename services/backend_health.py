"""Can the bot actually answer right now?

The widget's Online dot used to mean "the WebSocket handshake succeeded", which
is nearly worthless: with the database down the socket still opens and still
pongs, so the badge stayed green through an entire outage while every turn
failed. This module answers the question the guest actually cares about.

Three independent signals, because no single one is sufficient:

  1. DATABASE   — a periodic `SELECT 1` on the engine the chat turn uses. Every
                  turn loads and saves context, so a dead DB means no answers.
  2. LLM        — the outcome of the REAL model calls live traffic is already
                  making. Free, and it is ground truth: no synthetic probe tells
                  you as much as the calls you are genuinely making. A dead API
                  key, an exhausted quota or an unreachable provider shows up
                  here and NOWHERE in a database check.
  3. TURN       — a chat turn that raised. The most direct evidence of all.

Any one of them failing means degraded. Each is deliberately slow to condemn
(consecutive strikes, never a single blip) because a badge that cries wolf at a
working bot is the same bug pointing the other way.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# ── Tunables ──────────────────────────────────────────────────────────────
DB_TTL_SECONDS = 20.0
DB_PROBE_TIMEOUT_S = 10.0  # a DB that can't answer SELECT 1 in 10s can't serve a turn
DB_STRIKES_TO_DEGRADE = 2

# One failed model call is noise — a timeout, a rate limit, one bad prompt. A run
# of them with nothing succeeding in between is an outage.
LLM_STRIKES_TO_DEGRADE = 3
# ...but only while the failures are RECENT. Three failures spread over an hour,
# with the bot answering fine in between, must not leave the badge stuck red.
LLM_FAILURE_WINDOW_S = 120.0

# Same reasoning for whole turns, with a lower bar: a turn raising is a much
# stronger signal than one model call failing.
TURN_STRIKES_TO_DEGRADE = 2
TURN_FAILURE_WINDOW_S = 120.0


class BackendHealth:
    """Shared, process-wide verdict on whether the bot can serve a turn."""

    def __init__(self) -> None:
        self._db_ok = True
        self._db_detail = ""
        self._db_checked_at = 0.0
        self._db_strikes = 0
        self._db_task: asyncio.Task | None = None

        self._llm_strikes = 0
        self._llm_last_failure = 0.0

        self._turn_strikes = 0
        self._turn_last_failure = 0.0

    # ── 1. Database ──────────────────────────────────────────────────────
    async def _probe_db(self) -> None:
        """Refresh the DB verdict with a real `SELECT 1`. Never raises."""
        loop = asyncio.get_running_loop()
        started = loop.time()
        ok = True
        detail = ""
        try:
            from sqlalchemy import text

            from models.database import AsyncSessionLocal

            async def _ping() -> None:
                async with AsyncSessionLocal() as session:
                    await session.execute(text("SELECT 1"))

            await asyncio.wait_for(_ping(), timeout=DB_PROBE_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 — any DB failure means degraded
            ok = False
            # Classify by the CLOCK, not the exception type: wait_for cancels the
            # driver mid-query and aiomysql surfaces that cancellation as its own
            # InterfaceError long before TimeoutError ever reaches us.
            timed_out = (loop.time() - started) >= (DB_PROBE_TIMEOUT_S - 0.5)
            detail = "database_timeout" if timed_out else "database_unreachable"
            logger.warning("[health] db probe failed (%s): %r", detail, exc)

        if ok:
            self._db_strikes = 0
        else:
            self._db_strikes += 1
            if self._db_strikes < DB_STRIKES_TO_DEGRADE:
                logger.info(
                    "[health] db probe failed (%s) strike %d/%d — holding %s",
                    detail, self._db_strikes, DB_STRIKES_TO_DEGRADE, self._db_ok,
                )
                self._db_checked_at = loop.time()
                return

        if self._db_ok != ok:
            logger.warning("[health] database %s -> %s (%s)", self._db_ok, ok, detail or "ok")
        self._db_ok = ok
        self._db_detail = detail
        self._db_checked_at = loop.time()

    def _refresh_db_if_stale(self) -> None:
        """Kick off a background refresh when the DB verdict has gone stale.

        Deliberately NOT awaited by callers. A cold `SELECT 1` against a remote
        database can take seconds, and the widget closes a socket that fails to
        pong within 10s — blocking the beat on the probe would kill healthy
        connections in order to ask whether they were healthy.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop (sync caller) — serve the cached verdict as-is
        if (loop.time() - self._db_checked_at) < DB_TTL_SECONDS:
            return
        if self._db_task is not None and not self._db_task.done():
            return  # single-flight: one probe per TTL for the whole process
        self._db_task = loop.create_task(self._probe_db())

    # ── 2. LLM ───────────────────────────────────────────────────────────
    def record_llm_call(self, *, ok: bool) -> None:
        """Called on every real model call. Cheap enough to be unconditional."""
        if ok:
            self._llm_strikes = 0
            return
        now = time.monotonic()
        if (now - self._llm_last_failure) > LLM_FAILURE_WINDOW_S:
            self._llm_strikes = 0  # the previous run has aged out
        self._llm_last_failure = now
        self._llm_strikes += 1
        if self._llm_strikes == LLM_STRIKES_TO_DEGRADE:
            logger.warning(
                "[health] %d consecutive LLM failures — reporting degraded",
                self._llm_strikes,
            )

    def _llm_degraded(self) -> bool:
        if self._llm_strikes < LLM_STRIKES_TO_DEGRADE:
            return False
        # Stale run of failures with nothing since → assume recovered rather than
        # leaving the badge stuck red on a bot that is answering.
        return (time.monotonic() - self._llm_last_failure) <= LLM_FAILURE_WINDOW_S

    # ── 3. Whole turns ───────────────────────────────────────────────────
    def record_turn(self, *, ok: bool, detail: str = "") -> None:
        """Called when a chat turn completes or blows up."""
        if ok:
            self._turn_strikes = 0
            return
        now = time.monotonic()
        if (now - self._turn_last_failure) > TURN_FAILURE_WINDOW_S:
            self._turn_strikes = 0
        self._turn_last_failure = now
        self._turn_strikes += 1
        if self._turn_strikes >= TURN_STRIKES_TO_DEGRADE:
            logger.warning("[health] %d consecutive failed turns (%s)", self._turn_strikes, detail)

    def _turn_degraded(self) -> bool:
        if self._turn_strikes < TURN_STRIKES_TO_DEGRADE:
            return False
        return (time.monotonic() - self._turn_last_failure) <= TURN_FAILURE_WINDOW_S

    # ── The verdict ──────────────────────────────────────────────────────
    def snapshot(self) -> dict[str, Any]:
        """The current verdict. Safe to call on a hot path — never blocks."""
        self._refresh_db_if_stale()

        failing: list[str] = []
        if not self._db_ok:
            failing.append(self._db_detail or "database_unreachable")
        if self._llm_degraded():
            failing.append("llm_failing")
        if self._turn_degraded():
            failing.append("turns_failing")

        return {
            "ok": not failing,
            "detail": failing[0] if failing else "",
            "failing": failing,
            "checks": {
                "database": bool(self._db_ok),
                "llm": not self._llm_degraded(),
                "turns": not self._turn_degraded(),
            },
        }


backend_health = BackendHealth()
