"""Shared primitives for the append-only JSONL trace loggers.

Extracted verbatim (behavior-preserving) from the per-service copies in
backend_trace_service / everything_trace_service / step_trace_service. Each
service kept its own bespoke ``log_event`` (they differ in fields, method
casing, and turn-context resolution) but the sanitize / truncate / JSONL-append
mechanics were byte-for-byte duplicated with only numeric caps differing — so
those are parameterized here and shared.
"""
from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def now_iso() -> str:
    """UTC ISO-8601 timestamp, matching ``datetime.now(UTC).isoformat()``."""
    return datetime.now(UTC).isoformat()


def clean(value: Any) -> str:
    """``str(value or "").strip()`` — the ubiquitous field normalizer."""
    return str(value or "").strip()


def truncate_text(value: Any, max_chars: int) -> str:
    """Clamp a string to ``max_chars``, appending a truncation marker."""
    text = str(value or "")
    if len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    return f"{text[:max_chars]}...[TRUNCATED:{omitted}]"


def sanitize(
    value: Any,
    *,
    max_chars: int,
    depth_cap: int,
    item_cap: int,
    depth: int = 0,
) -> Any:
    """Recursively JSON-safe a value: truncate strings, cap depth and breadth.

    Mirrors the original per-service ``_sanitize`` exactly; ``depth_cap`` and
    ``item_cap`` capture the only numbers that differed between services.
    """
    if depth > depth_cap:
        return truncate_text(str(value), max_chars)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return truncate_text(value, max_chars)
    if isinstance(value, dict):
        return {
            str(k): sanitize(v, max_chars=max_chars, depth_cap=depth_cap, item_cap=item_cap, depth=depth + 1)
            for k, v in list(value.items())[:item_cap]
        }
    if isinstance(value, list):
        return [
            sanitize(v, max_chars=max_chars, depth_cap=depth_cap, item_cap=item_cap, depth=depth + 1)
            for v in value[:item_cap]
        ]
    if isinstance(value, tuple):
        return [
            sanitize(v, max_chars=max_chars, depth_cap=depth_cap, item_cap=item_cap, depth=depth + 1)
            for v in value[:item_cap]
        ]
    return truncate_text(str(value), max_chars)


def append_jsonl(
    log_file: Path,
    lock: threading.Lock,
    record: dict[str, Any],
    *,
    default_str: bool = False,
) -> None:
    """Append one JSON record as a line to ``log_file`` under ``lock``.

    Best-effort: never raises. ``default_str=True`` mirrors the services that
    pass ``json.dumps(..., default=str)``.
    """
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        if default_str:
            encoded = json.dumps(record, ensure_ascii=False, default=str)
        else:
            encoded = json.dumps(record, ensure_ascii=False)
        with lock:
            with log_file.open("a", encoding="utf-8") as fh:
                fh.write(encoded + "\n")
    except Exception:
        pass
