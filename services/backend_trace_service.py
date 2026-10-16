"""
Backend Trace Service

Writes structured JSONL events for request and turn-level backend debugging.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from config.settings import settings
from services import log_common


class BackendTraceService:
    """Best-effort append-only logger for backend runtime tracing."""

    def __init__(self) -> None:
        self.enabled = bool(getattr(settings, "backend_trace_enabled", True))
        self.log_file = Path(
            str(getattr(settings, "backend_trace_log_file", "./logs/backend_trace.jsonl"))
        )
        self.max_chars = max(200, int(getattr(settings, "backend_trace_max_chars", 2000) or 2000))
        self._lock = threading.Lock()

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

        record = {
            "timestamp": log_common.now_iso(),
            "event": log_common.clean(event) or "backend_event",
            "trace_id": log_common.clean(trace_id),
            "turn_trace_id": log_common.clean(turn_trace_id),
            "session_id": log_common.clean(session_id),
            "hotel_code": log_common.clean(hotel_code),
            "channel": log_common.clean(channel),
            "endpoint": log_common.clean(endpoint),
            "method": log_common.clean(method),
            "status_code": status_code,
            "component": log_common.clean(component),
            "error": log_common.truncate_text(log_common.clean(error), self.max_chars),
            "payload": log_common.sanitize(payload or {}, max_chars=self.max_chars, depth_cap=8, item_cap=300),
        }
        log_common.append_jsonl(self.log_file, self._lock, record, default_str=False)

        try:
            from services.db_log_sink import db_log_sink
            db_log_sink.emit("backend_trace", record)
        except Exception:
            pass


backend_trace_service = BackendTraceService()
