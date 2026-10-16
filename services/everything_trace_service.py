"""
Everything Trace Service

Single append-only JSONL log for deep backend debugging across
HTTP, chat pipeline, orchestration, and LLM calls.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from config.settings import settings
from services import log_common


class EverythingTraceService:
    """Best-effort write-only trace stream for full backend events."""

    def __init__(self) -> None:
        self.enabled = bool(getattr(settings, "everything_trace_enabled", True))
        self.log_file = Path(
            str(
                getattr(
                    settings,
                    "everything_trace_log_file",
                    "./logs/everything_backend_trace.jsonl",
                )
            )
        )
        self.max_chars = max(
            1000,
            int(getattr(settings, "everything_trace_max_chars", 400000) or 400000),
        )
        self._lock = threading.Lock()

    @staticmethod
    def _clean(value: Any) -> str:
        return log_common.clean(value)

    def _active_turn_context(self) -> dict[str, Any]:
        try:
            from services.turn_diagnostics_service import turn_diagnostics_service

            ctx = turn_diagnostics_service.get_turn_context()
            return ctx if isinstance(ctx, dict) else {}
        except Exception:
            return {}

    def log_event(
        self,
        event: str,
        payload: dict[str, Any] | None = None,
        *,
        trace_id: str = "",
        turn_trace_id: str = "",
        session_id: str = "",
        hotel_code: str = "",
        channel: str = "",
        endpoint: str = "",
        method: str = "",
        status_code: int | None = None,
        component: str = "",
        error: str = "",
    ) -> None:
        if not self.enabled:
            return

        turn_ctx = self._active_turn_context()
        resolved_turn_trace_id = self._clean(turn_trace_id) or self._clean(turn_ctx.get("turn_trace_id"))
        resolved_trace_id = self._clean(trace_id) or self._clean(turn_ctx.get("api_trace_id"))
        resolved_session_id = self._clean(session_id) or self._clean(turn_ctx.get("session_id"))
        resolved_hotel_code = self._clean(hotel_code) or self._clean(turn_ctx.get("hotel_code"))
        resolved_channel = self._clean(channel) or self._clean(turn_ctx.get("channel"))

        record = {
            "timestamp": log_common.now_iso(),
            "event": self._clean(event) or "backend_event",
            "trace_id": resolved_trace_id,
            "turn_trace_id": resolved_turn_trace_id,
            "session_id": resolved_session_id,
            "hotel_code": resolved_hotel_code,
            "channel": resolved_channel,
            "endpoint": self._clean(endpoint),
            "method": self._clean(method),
            "status_code": status_code,
            "component": self._clean(component),
            "error": log_common.truncate_text(self._clean(error), self.max_chars),
            "payload": log_common.sanitize(payload or {}, max_chars=self.max_chars, depth_cap=10, item_cap=400),
        }
        log_common.append_jsonl(self.log_file, self._lock, record, default_str=True)

        try:
            from services.db_log_sink import db_log_sink
            db_log_sink.emit("everything_trace", record)
        except Exception:
            pass


everything_trace_service = EverythingTraceService()

