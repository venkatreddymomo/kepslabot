"""
Step Trace Service

Append-only structured step logger used to trace request execution paths
across gateway, admin, chat, and ticketing flows.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from config.settings import settings
from services import log_common


class StepTraceService:
    """Best-effort structured step logger."""

    def __init__(self) -> None:
        self.enabled = bool(getattr(settings, "step_trace_logging_enabled", True))
        self.log_file = Path(str(getattr(settings, "step_trace_log_file", "./logs/step_trace.jsonl")))
        self.max_chars = max(1000, int(getattr(settings, "step_trace_max_chars", 25000) or 25000))
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
        step: str = "",
        stage: str = "",
        error: str = "",
    ) -> None:
        if not self.enabled:
            return

        record = {
            "timestamp": log_common.now_iso(),
            "event": log_common.clean(event) or "step_event",
            "trace_id": log_common.clean(trace_id),
            "turn_trace_id": log_common.clean(turn_trace_id),
            "session_id": log_common.clean(session_id),
            "hotel_code": log_common.clean(hotel_code),
            "channel": log_common.clean(channel),
            "endpoint": log_common.clean(endpoint),
            "method": log_common.clean(method).upper(),
            "status_code": status_code,
            "component": log_common.clean(component),
            "step": log_common.clean(step),
            "stage": log_common.clean(stage),
            "error": log_common.truncate_text(log_common.clean(error), self.max_chars),
            "payload": log_common.sanitize(payload or {}, max_chars=self.max_chars, depth_cap=8, item_cap=400),
        }
        log_common.append_jsonl(self.log_file, self._lock, record, default_str=True)

        try:
            from services.db_log_sink import db_log_sink
            db_log_sink.emit("step_trace", record)
        except Exception:
            pass


step_trace_service = StepTraceService()

