"""
DB Log Sink
===========

A single, best-effort sink that mirrors the file-based loggers into the prod DB
so logs are queryable from ANY environment (local or deployed). Files keep
writing on their own; this is an ADDITIONAL target.

Design rules (match the existing file loggers' philosophy):
- `emit(...)` is synchronous, non-blocking, and NEVER raises. It only pushes a
  row onto a bounded in-memory queue (drop-on-full) — safe to call from request
  handlers and from background threads alike.
- A background asyncio worker (started at app startup) drains the queue, groups
  rows by table, and bulk-inserts them on its own DB session every ~0.5s.
- If the DB is down or slow, chat is unaffected — rows are simply dropped.

Routing: each file logger calls `db_log_sink.emit("<stream>", record)` once after
its file write. The sink maps the stream → one of the purpose tables and pulls
typed columns out of the record. Any `error`-level row is ALSO fanned out into
new_bot_log_errors so every failure is searchable in one place.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
from datetime import UTC, datetime, time, timedelta
from typing import Any

from config.settings import settings

logger = logging.getLogger(__name__)

# Per-request conversation thread id (the frontend's rotating conversation_id).
# Set once at the start of each chat turn; read at emit() time (which runs in the
# request's async context) so EVERY log stream — routing, backend, llm, tickets —
# is stamped with it, even when the emitting record itself doesn't carry it.
import contextvars as _contextvars

_conversation_uid_ctx: _contextvars.ContextVar[str] = _contextvars.ContextVar(
    "db_log_conversation_uid", default=""
)


def set_conversation_uid(uid: Any) -> None:
    """Stamp the active conversation thread id for subsequent log emits this turn."""
    try:
        _conversation_uid_ctx.set(str(uid or "").strip())
    except Exception:
        pass


def _current_conversation_uid() -> str:
    try:
        return str(_conversation_uid_ctx.get("") or "").strip()
    except Exception:
        return ""


# ── stream → table routing ───────────────────────────────────────────────────
# table keys map to ORM models lazily (imported inside the worker to avoid any
# import-time cycles with models.database).
STREAM_TABLE: dict[str, str] = {
    # conversation / messages
    "conversation_audit": "messages",
    "chat_turn_llm_summary": "messages",
    # suggestion chips
    "chips": "chips",
    # routing
    "book_routing": "routing",
    "routing": "routing",
    # full backend path / deep trace
    "step_trace": "backend",
    "backend_trace": "backend",
    "everything_trace": "backend",
    "turn_diagnostics": "backend",
    "chat_step_timing": "backend",
    "flow": "backend",
    "observability": "backend",
    "processing_debug": "backend",
    "service_runtime": "backend",
    "service_config": "backend",
    "kb_pipeline": "backend",
    "new_detailed": "backend",
    "rag_step": "backend",
    # llm calls
    "llm_call": "llm",
    "llm_io": "llm",
    # ticketing
    "ticketing": "tickets",
    # crashes / explicit errors
    "gateway_crash": "errors",
    "error": "errors",
}

# For these tables, don't duplicate huge raw text inside `payload` (it already
# lives in dedicated columns).
_PAYLOAD_EXCLUDE: dict[str, set[str]] = {
    "llm": {"input_text", "output_text", "input", "output", "messages"},
}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _first(record: dict, *keys: str) -> Any:
    for k in keys:
        if k in record and record.get(k) not in (None, ""):
            return record.get(k)
    return None


def _iter_nested_candidates(value: Any, *, max_depth: int = 4):
    stack: list[tuple[Any, int]] = [(value, 0)]
    seen: set[int] = set()
    while stack:
        current, depth = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, dict):
            yield current
            if depth >= max_depth:
                continue
            for nested in current.values():
                if isinstance(nested, (dict, list, tuple)):
                    stack.append((nested, depth + 1))
        elif isinstance(current, (list, tuple)) and depth < max_depth:
            for nested in current[:20]:
                if isinstance(nested, (dict, list, tuple)):
                    stack.append((nested, depth + 1))


def _deep_first(record: dict, *keys: str) -> Any:
    for candidate in _iter_nested_candidates(record):
        got = _first(candidate, *keys)
        if got not in (None, ""):
            return got
    return None


def _as_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "y"):
        return True
    if s in ("0", "false", "no", "n"):
        return False
    return None


def _resolve_source_env() -> str:
    configured = _clean(getattr(settings, "db_logging_source_env", "auto")).lower()
    if configured in ("local", "deployed"):
        return configured

    def _classify(ev: str) -> str | None:
        ev = _clean(ev).lower()
        if not ev:
            return None
        if ev in ("prod", "production", "deployed", "staging", "stage", "live"):
            return "deployed"
        if ev in ("local", "dev", "development", "test"):
            return "local"
        return ev

    for key in ("DB_LOGGING_SOURCE_ENV", "DEPLOY_ENV", "APP_ENV", "ENV", "ENVIRONMENT", "NODE_ENV"):
        got = _classify(os.environ.get(key, ""))
        if got:
            return got
    # Fall back to the app's own env flag (defaults to "development" => local).
    got = _classify(getattr(settings, "app_env", ""))
    return got or "local"


class DBLogSink:
    def __init__(self) -> None:
        self.enabled = bool(getattr(settings, "db_logging_enabled", True))
        self.source_env = _resolve_source_env()
        self._allow = {s for s in (getattr(settings, "db_logging_streams", []) or []) if _clean(s)}
        self._maxsize = max(1000, int(getattr(settings, "db_logging_queue_maxsize", 20000) or 20000))
        self._batch = max(1, int(getattr(settings, "db_logging_batch_size", 200) or 200))
        self._flush = float(getattr(settings, "db_logging_flush_interval_seconds", 0.5) or 0.5)
        self._q: queue.Queue[tuple[str, dict]] = queue.Queue(maxsize=self._maxsize)
        self._task = None
        self._stop = False
        self._dropped = 0
        self._dropped_queue_full = 0
        self._dropped_flush_error = 0
        self._written = 0
        self._flush_failures = 0
        self._last_flush_error = ""
        self._last_flush_at = ""
        self._started_at = ""
        self._lock = threading.Lock()

    # ── public, sync, never-raises ───────────────────────────────────────────
    def emit(self, stream: str, record: dict | None = None, /, **extra: Any) -> None:
        """Queue one log row. Best-effort: returns immediately, never raises."""
        if not self.enabled:
            return
        try:
            stream = _clean(stream) or "backend"
            if self._allow and stream not in self._allow:
                return
            rec: dict = {}
            if isinstance(record, dict):
                rec.update(record)
            if extra:
                rec.update(extra)
            # Stamp the per-request conversation thread id onto every stream so
            # routing/backend/llm/tickets rows are traceable per conversation even
            # when their own record doesn't include it.
            if not _clean(rec.get("conversation_uid")):
                _cuid = _current_conversation_uid()
                if _cuid:
                    rec["conversation_uid"] = _cuid

            table = STREAM_TABLE.get(stream, "backend")
            common = self._common(stream, rec)
            typed = self._typed(table, rec)
            payload = self._payload(table, rec)
            row = {**common, **typed, "payload": payload}
            self._put((table, row))

            # Fan out any error into the errors table too.
            if common.get("level") == "error" and table != "errors":
                err_row = {
                    **common,
                    "origin_stream": stream,
                    "error_type": _clean(_deep_first(rec, "error_type", "exc_type"))[:160] or None,
                    "error_message": _deep_first(rec, "error", "error_message", "message"),
                    "stack": _deep_first(rec, "stack", "traceback", "stacktrace"),
                    "payload": payload,
                }
                self._put(("errors", err_row))
        except Exception:
            return

    # ── row building ─────────────────────────────────────────────────────────
    def _common(self, stream: str, rec: dict) -> dict:
        ts_raw = _deep_first(rec, "timestamp", "ts")
        ts = self._parse_ts(ts_raw)
        level = _clean(_deep_first(rec, "level")) or (
            "error" if _deep_first(rec, "error", "error_message") else "info"
        )
        return {
            "ts": ts,
            "source_env": self.source_env,
            "stream": stream,
            "session_id": _clean(_deep_first(rec, "session_id", "sid"))[:100] or None,
            # The frontend's conversation id is a STRING thread id (e.g. "conv_..."),
            # not the legacy numeric FK. Store it as-is so logs are traceable per
            # conversation. `_as_int` on a string thread id would silently null it.
            "conversation_id": _as_int(_deep_first(rec, "conversation_row_id", "conv_row_id")),
            "conversation_uid": _clean(
                _deep_first(rec, "conversation_uid", "conversation_id", "conv_id")
            )[:100] or None,
            "hotel_code": _clean(
                _deep_first(rec, "hotel_code", "hotel", "tenant_id")
            )[:50] or None,
            "trace_id": _clean(_deep_first(rec, "trace_id", "api_trace_id"))[:100] or None,
            "turn_trace_id": _clean(_deep_first(rec, "turn_trace_id"))[:100] or None,
            "level": level[:16],
            "component": _clean(_deep_first(rec, "component"))[:120] or None,
            "event": _clean(_deep_first(rec, "event", "step", "name", "event_type"))[:120] or None,
            "message": _to_text(_first(rec, "message", "msg", "text", "line")),
        }

    def _typed(self, table: str, rec: dict) -> dict:
        if table == "messages":
            return {
                "sender": _clean(_first(rec, "sender"))[:10] or None,
                "user_text": _to_text(_first(rec, "user_message", "user_text", "user")),
                "bot_text": _to_text(_first(rec, "bot_message", "bot_text", "bot", "assistant")),
                "phase": self._phase_text(rec),
                "service_id": _clean(_deep_first(rec, "service_id"))[:100] or None,
                "intent": _clean(_deep_first(rec, "intent"))[:120] or None,
            }
        if table == "chips":
            return {
                "chips_shown": _first(rec, "chips", "chips_shown", "suggested_actions"),
                "chosen_chip": _to_text(_first(rec, "chosen_chip", "chosen")),
                "chip_source": _clean(
                    _deep_first(rec, "chip_source", "source", "suggested_actions_source")
                )[:60] or None,
                "kb_verified": _as_bool(_deep_first(rec, "kb_verified")),
                "phase": self._phase_text(rec),
                "service_id": _clean(_deep_first(rec, "service_id"))[:100] or None,
            }
        if table == "routing":
            return {
                "route": _clean(_deep_first(rec, "route", "main_router_route"))[:60] or None,
                "from_topic": _clean(_deep_first(rec, "from_topic", "from"))[:120] or None,
                "to_topic": _clean(_deep_first(rec, "to_topic", "to"))[:120] or None,
                "decision": _clean(_deep_first(rec, "decision"))[:60] or None,
                "service_id": _clean(_deep_first(rec, "service_id"))[:100] or None,
                "phase": self._phase_text(rec),
                "reason": _to_text(_deep_first(rec, "reason")),
            }
        if table == "backend":
            return {
                "step": _clean(_deep_first(rec, "step", "event", "event_type"))[:120] or None,
                "stage": _clean(_deep_first(rec, "stage"))[:60] or None,
                "endpoint": _clean(_deep_first(rec, "endpoint"))[:255] or None,
                "method": _clean(_deep_first(rec, "method"))[:10] or None,
                "status_code": _as_int(_deep_first(rec, "status_code")),
                "duration_ms": _as_int(
                    _deep_first(rec, "duration_ms", "duration", "elapsed_ms", "took_ms")
                ),
            }
        if table == "llm":
            return {
                "model": _clean(_deep_first(rec, "model"))[:80] or None,
                "call_site": _clean(_deep_first(rec, "call_site", "purpose", "component"))[:120] or None,
                "input_tokens": _as_int(_deep_first(rec, "input_tokens", "prompt_tokens")),
                "output_tokens": _as_int(_deep_first(rec, "output_tokens", "completion_tokens")),
                "total_tokens": _as_int(_deep_first(rec, "total_tokens")),
                "input_rate": _as_float(_deep_first(rec, "input_rate")),
                "output_rate": _as_float(_deep_first(rec, "output_rate")),
                "input_cost_usd": _as_float(_deep_first(rec, "input_cost_usd", "input_cost")),
                "output_cost_usd": _as_float(_deep_first(rec, "output_cost_usd", "output_cost")),
                "total_cost_usd": _as_float(_deep_first(rec, "total_cost_usd", "total_cost", "cost")),
                "currency": _clean(_deep_first(rec, "currency")) or "USD",
                "pricing_source": _clean(_deep_first(rec, "pricing_source"))[:20] or None,
                "latency_ms": _as_int(_deep_first(rec, "latency_ms", "time_taken_ms", "duration_ms")),
                "input_text": _to_text(_first(rec, "input_text", "input")),
                "output_text": _to_text(_first(rec, "output_text", "output")),
            }
        if table == "tickets":
            return {
                "ticket_ref": _clean(
                    _deep_first(rec, "ticket_ref", "ticket_id", "assigned_id", "id")
                )[:100] or None,
                "service_id": _clean(_deep_first(rec, "service_id"))[:100] or None,
                "status": _clean(
                    _deep_first(rec, "status", "ticket_status", "api_status")
                )[:40] or None,
                "stage": _clean(_deep_first(rec, "stage", "event"))[:60] or None,
            }
        if table == "errors":
            return {
                "origin_stream": _clean(_deep_first(rec, "origin_stream", "stream"))[:60] or None,
                "error_type": _clean(_deep_first(rec, "error_type", "exc_type"))[:160] or None,
                "error_message": _to_text(_deep_first(rec, "error", "error_message", "message")),
                "stack": _to_text(_deep_first(rec, "stack", "traceback", "stacktrace")),
            }
        return {}

    def _phase_text(self, rec: dict) -> str | None:
        phase = _deep_first(
            rec,
            "phase_id",
            "selected_phase_id",
            "current_phase_id",
            "resolved_phase_id",
            "phase_gate_current_phase_id",
            "response_phase_id",
            "requested_phase_id",
            "phase",
            "id",
        )
        if isinstance(phase, dict):
            phase = _first(
                phase,
                "phase_id",
                "selected_phase_id",
                "current_phase_id",
                "resolved_phase_id",
                "phase_gate_current_phase_id",
                "response_phase_id",
                "requested_phase_id",
                "id",
            )
        text = _clean(phase)[:50]
        return text or None

    def _payload(self, table: str, rec: dict) -> Any:
        exclude = _PAYLOAD_EXCLUDE.get(table, set())
        try:
            return {k: v for k, v in rec.items() if k not in exclude}
        except Exception:
            return None

    def _put(self, item: tuple[str, dict]) -> None:
        try:
            self._q.put_nowait(item)
        except queue.Full:
            with self._lock:
                self._dropped += 1
                self._dropped_queue_full += 1
                dropped = self._dropped_queue_full
            if dropped in {1, 10} or dropped % 100 == 0:
                logger.warning(
                    "db_log_sink queue full; dropping log rows (dropped_queue_full=%s maxsize=%s)",
                    dropped,
                    self._maxsize,
                )

    @staticmethod
    def _parse_ts(value: Any) -> datetime:
        if isinstance(value, datetime):
            return value
        s = _clean(value)
        if s:
            try:
                return datetime.fromisoformat(s.replace("Z", "+00:00"))
            except Exception:
                pass
        return datetime.now(UTC)

    # ── background worker ────────────────────────────────────────────────────
    def start(self) -> None:
        """Launch the async drain worker on the running event loop."""
        if not self.enabled or self._task is not None:
            return
        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._stop = False
        with self._lock:
            self._started_at = datetime.now(UTC).isoformat()
        self._task = loop.create_task(self._run(), name="db_log_sink_worker")

    async def stop(self) -> None:
        self._stop = True
        if self._task is not None:
            try:
                await self._task
            except Exception:
                pass
            self._task = None

    async def _run(self) -> None:
        import asyncio

        while not self._stop:
            batch = self._drain()
            if batch:
                await self._flush_batch(batch)
            else:
                await asyncio.sleep(self._flush)
        # Final drain on shutdown.
        leftover = self._drain(drain_all=True)
        if leftover:
            await self._flush_batch(leftover)

    def _drain(self, drain_all: bool = False) -> list[tuple[str, dict]]:
        out: list[tuple[str, dict]] = []
        limit = self._maxsize if drain_all else self._batch
        for _ in range(limit):
            try:
                out.append(self._q.get_nowait())
            except queue.Empty:
                break
        return out

    async def _flush_batch(self, batch: list[tuple[str, dict]]) -> None:
        from models.database import (
            AsyncSessionLocal,
            LogBackend,
            LogChip,
            LogError,
            LogLLM,
            LogMessage,
            LogRouting,
            LogTicket,
        )

        models = {
            "messages": LogMessage,
            "chips": LogChip,
            "routing": LogRouting,
            "backend": LogBackend,
            "llm": LogLLM,
            "tickets": LogTicket,
            "errors": LogError,
        }
        grouped: dict[str, list[dict]] = {}
        for table, row in batch:
            grouped.setdefault(table, []).append(row)

        try:
            async with AsyncSessionLocal() as session:
                for table, rows in grouped.items():
                    model = models.get(table)
                    if model is None:
                        continue
                    cols = {c.name for c in model.__table__.columns}
                    session.add_all([model(**{k: v for k, v in r.items() if k in cols}) for r in rows])
                await session.commit()
            with self._lock:
                self._written += len(batch)
                self._last_flush_at = datetime.now(UTC).isoformat()
                self._last_flush_error = ""
        except Exception:
            # Best-effort: drop this batch rather than blocking or raising.
            with self._lock:
                self._dropped += len(batch)
                self._dropped_flush_error += len(batch)
                self._flush_failures += 1
                self._last_flush_error = "flush_batch_failed"
            logger.warning("db_log_sink flush failed; dropped batch rows=%s", len(batch), exc_info=True)

    # ── retention ────────────────────────────────────────────────────────────
    async def prune(self, days: int | None = None) -> dict[str, int]:
        """Delete log rows older than `days` from every log table (batched)."""
        from sqlalchemy import text as _text

        from models.database import AsyncSessionLocal

        retention = int(days if days is not None else getattr(settings, "db_logging_retention_days", 7) or 7)
        cutoff = datetime.now(UTC) - timedelta(days=retention)
        tables = [
            "new_bot_log_messages",
            "new_bot_log_chips",
            "new_bot_log_routing",
            "new_bot_log_backend",
            "new_bot_log_llm",
            "new_bot_log_tickets",
            "new_bot_log_errors",
        ]
        deleted: dict[str, int] = {}
        for tbl in tables:
            total = 0
            try:
                while True:
                    async with AsyncSessionLocal() as session:
                        res = await session.execute(
                            _text(f"DELETE FROM {tbl} WHERE ts < :cutoff LIMIT 5000"),
                            {"cutoff": cutoff},
                        )
                        await session.commit()
                        n = int(res.rowcount or 0)
                    total += n
                    if n < 5000:
                        break
            except Exception:
                # Some backends (sqlite) reject DELETE ... LIMIT — fall back.
                try:
                    async with AsyncSessionLocal() as session:
                        res = await session.execute(
                            _text(f"DELETE FROM {tbl} WHERE ts < :cutoff"),
                            {"cutoff": cutoff},
                        )
                        await session.commit()
                        total += int(res.rowcount or 0)
                except Exception:
                    pass
            deleted[tbl] = total
        return deleted

    async def rollup_llm_daily(self, days: int | None = None) -> int:
        """Recompute the daily LLM cost/token aggregate for the recent window.

        Delete-then-reinsert the covered days so it is idempotent and always
        reflects current rows in new_bot_log_llm.
        """
        if not bool(getattr(settings, "db_logging_cost_rollup_enabled", True)):
            return 0
        from sqlalchemy import text as _text

        from models.database import AsyncSessionLocal

        window = int(days if days is not None else getattr(settings, "db_logging_retention_days", 7) or 7)
        cutoff = datetime.now(UTC) - timedelta(days=window)
        # Recompute only the days the raw table still holds IN FULL. The cutoff
        # lands mid-day, so the day it falls on is already partly pruned: pick
        # it up and we would delete that day's whole rollup row and rebuild it
        # from the hours that happen to survive, quietly shrinking a day's
        # tokens on every run. That day's row was written when the day was
        # complete, so leave it alone. Deleting and selecting from the same
        # whole-day boundary also keeps the two halves of this rollup in step.
        recompute_from_date = (cutoff + timedelta(days=1)).date()
        recompute_from = datetime.combine(
            recompute_from_date, time.min, tzinfo=UTC
        )
        inserted = 0
        try:
            async with AsyncSessionLocal() as session:
                rows = (
                    await session.execute(
                        _text(
                            """
                            SELECT DATE(ts) AS d,
                                   COALESCE(source_env,'') AS env,
                                   COALESCE(hotel_code,'') AS hc,
                                   COALESCE(model,'')      AS mdl,
                                   COUNT(*)                AS calls,
                                   COALESCE(SUM(input_tokens),0)  AS itok,
                                   COALESCE(SUM(output_tokens),0) AS otok,
                                   COALESCE(SUM(total_tokens),0)  AS ttok,
                                   COALESCE(SUM(total_cost_usd),0) AS cost
                            FROM new_bot_log_llm
                            WHERE ts >= :cutoff
                            GROUP BY DATE(ts), COALESCE(source_env,''), COALESCE(hotel_code,''), COALESCE(model,'')
                            """
                        ),
                        {"cutoff": recompute_from},
                    )
                ).all()
                await session.execute(
                    _text("DELETE FROM new_bot_log_llm_daily WHERE day >= :d"),
                    {"d": recompute_from_date},
                )
                for r in rows:
                    await session.execute(
                        _text(
                            """
                            INSERT INTO new_bot_log_llm_daily
                                (day, source_env, hotel_code, model, calls,
                                 input_tokens, output_tokens, total_tokens, total_cost_usd, updated_at)
                            VALUES (:day,:env,:hc,:mdl,:calls,:itok,:otok,:ttok,:cost, CURRENT_TIMESTAMP)
                            """
                        ),
                        {
                            "day": r.d, "env": r.env, "hc": r.hc, "mdl": r.mdl,
                            "calls": int(r.calls or 0), "itok": int(r.itok or 0),
                            "otok": int(r.otok or 0), "ttok": int(r.ttok or 0),
                            "cost": float(r.cost or 0),
                        },
                    )
                    inserted += 1
                await session.commit()
        except Exception:
            return inserted
        return inserted

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "written": self._written,
                "dropped": self._dropped,
                "dropped_queue_full": self._dropped_queue_full,
                "dropped_flush_error": self._dropped_flush_error,
                "flush_failures": self._flush_failures,
                "queued": self._q.qsize(),
                "maxsize": self._maxsize,
                "worker_running": 1 if self._task is not None and not self._task.done() else 0,
                "started_at": self._started_at,
                "last_flush_at": self._last_flush_at,
                "last_flush_error": self._last_flush_error,
            }


def _to_text(value: Any) -> str | None:
    """Stringify a value for a text column without truncating (full untrimmed)."""
    if value is None:
        return None
    if isinstance(value, str):
        return value if value.strip() else None
    try:
        import json

        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


db_log_sink = DBLogSink()
