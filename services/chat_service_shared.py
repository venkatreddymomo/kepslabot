"""Shared module-level helpers/constants for ChatService and its mixins.

These are the only module-level names in chat_service used by more than one
method cluster (core + a domain cluster). Housing them here lets the domain
mixins import them without importing chat_service (no cycle). Moved verbatim.
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from schemas.chat import IntentType


class CapabilityCheck(BaseModel):
    """Result of a capability check."""
    allowed: bool
    reason: str
    alternatives: list[str] = Field(default=[])
    constraints: Optional[dict] = None


def _coerce_optional_int(value: Any) -> Optional[int]:
    """Normalize dict-sourced ids to int (or None).

    Several JSON-facing dicts (e.g. ``db_config_service.get_business_info()``)
    serialize ``hotel_id`` as a string for the admin API. Postgres rejects
    ``integer = varchar`` comparisons, so any value that originates from such
    a dict must be coerced before it reaches an ORM query.
    """
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


_PHASE_INTENT_SERVICE_MARKERS = {
    IntentType.ORDER_FOOD: (
        "food",
        "dining",
        "restaurant",
        "menu",
        "meal",
        "in-room dining",
        "room dining",
        "aviator",
        "kadak",
        "swim club",
    ),
    IntentType.TABLE_BOOKING: (
        "booking",
        "book",
        "reservation",
        "appointment",
        "checkin",
        "checkout",
        "check in",
        "check out",
        "status",
        "modification",
        "cancellation",
        "transfer",
        "transport",
        "cab",
        "taxi",
        "pickup",
        "drop",
        "table",
        "restaurant",
        "spa",
        "recreation",
    ),
    IntentType.ROOM_SERVICE: (
        "room service",
        "housekeeping",
        "maintenance",
        "laundry",
        "amenity",
        "front desk",
        "support",
    ),
}
