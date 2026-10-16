"""Process-wide TTL cache for shared per-hotel/per-session data.

Used to skip redundant DB/file fetches across turns of a conversation.
Scope keys:
- hotel_code  → cache shared across all sessions of that hotel
- session_id  → cache scoped to a single conversation
"""
from __future__ import annotations

import time
from typing import Any

from config.settings import settings

_CACHE: dict[str, dict[str, tuple[float, Any]]] = {}


def session_ttl_seconds(default_seconds: float | int | None = None) -> float:
    """Return a cache TTL that lasts at least as long as the chat session."""
    fallback = 0.0
    try:
        fallback = float(default_seconds or 0.0)
    except (TypeError, ValueError):
        fallback = 0.0

    try:
        session_hours = float(getattr(settings, "session_ttl_hours", 24) or 0.0)
    except (TypeError, ValueError):
        session_hours = 0.0

    session_seconds = session_hours * 3600.0 if session_hours > 0 else 0.0
    ttl = max(fallback, session_seconds, 30.0)
    return ttl


def get(scope: str, key: str, ttl_seconds: float) -> Any | None:
    if not scope:
        scope = "_global"
    bucket = _CACHE.get(scope)
    if not bucket:
        return None
    entry = bucket.get(key)
    if not entry:
        return None
    cached_at, value = entry
    if (time.time() - cached_at) > float(ttl_seconds):
        bucket.pop(key, None)
        return None
    return value


def set(scope: str, key: str, value: Any) -> None:  # noqa: A001 - mirror dict-like API
    if not scope:
        scope = "_global"
    _CACHE.setdefault(scope, {})[key] = (time.time(), value)


def clear_scope(scope: str) -> None:
    if not scope:
        scope = "_global"
    _CACHE.pop(scope, None)


def clear_key(scope: str, key: str) -> None:
    if not scope:
        scope = "_global"
    bucket = _CACHE.get(scope)
    if bucket:
        bucket.pop(key, None)
