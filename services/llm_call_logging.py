"""Shared LLM-input logger for the orchestrator and its mixins.

Writes full LLM call input (and optional response) to logs/llm_inputs.log and
mirrors an event to the everything-trace stream. Lifted verbatim out of
llm_orchestration_service so the extracted mixins can call ``_log_llm_call``
without importing the orchestrator (no cycle).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

from config.settings import settings
from services.everything_trace_service import everything_trace_service

# ── LLM Input Logger ─────────────────────────────────────────────────────────
_log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
os.makedirs(_log_dir, exist_ok=True)
_llm_input_log_enabled = bool(getattr(settings, "llm_orchestration_input_log_enabled", False))
_llm_logger: "logging.Logger | None" = None
if _llm_input_log_enabled:
    _llm_logger = logging.getLogger("llm_inputs")
    if not _llm_logger.handlers:
        _fh = logging.FileHandler(os.path.join(_log_dir, "llm_inputs.log"), encoding="utf-8")
        _fh.setFormatter(logging.Formatter("%(message)s"))
        _llm_logger.addHandler(_fh)
        _llm_logger.setLevel(logging.DEBUG)
        _llm_logger.propagate = False


def _log_llm_call(label: str, session_id: str, user_message: str, system_prompt: str, payload: Any, response: Any = None) -> None:
    """Write full LLM input (and optional response) to logs/llm_inputs.log."""
    try:
        if _llm_input_log_enabled and _llm_logger is not None:
            sep = "=" * 80
            lines = [
                f"\n{sep}",
                f"[{datetime.now(UTC).isoformat()}] {label}",
                f"SESSION : {session_id}",
                f"USER    : {user_message}",
                "--- SYSTEM PROMPT ---",
                str(system_prompt or ""),
                "--- PAYLOAD ---",
                json.dumps(payload, ensure_ascii=False, indent=2) if not isinstance(payload, str) else payload,
            ]
            if response is not None:
                lines += ["--- LLM RESPONSE ---", json.dumps(response, ensure_ascii=False, indent=2) if not isinstance(response, str) else str(response)]
            lines.append(sep)
            _llm_logger.debug("\n".join(lines))
        everything_trace_service.log_event(
            "llm_orchestration_call",
            {
                "label": str(label or "").strip(),
                "user_message": str(user_message or ""),
                "system_prompt": str(system_prompt or ""),
                "payload": payload,
                "response": response,
            },
            session_id=str(session_id or ""),
            component="services.llm_orchestration_service",
        )
    except Exception:
        pass
