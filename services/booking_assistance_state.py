"""Shared booking-form state for team-assisted booking tenants.

The durable ledger is authoritative; browser flags and speculative replies are
only views of it. Pure helpers also let cache readers check before any mutation.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from config.settings import settings


def team_booking_enabled(hotel_code: Any) -> bool:
    from services import prebooking_settings_service

    admin_team_only = prebooking_settings_service.team_only_booking(hotel_code)
    if admin_team_only is not None:
        # Room Booking has a form button and no link button = team-only booking.
        return admin_team_only
    try:
        hotels = json.loads(settings.chat_booking_team_only_hotels_json or "[]")
    except (TypeError, ValueError):
        return False
    return isinstance(hotels, list) and str(hotel_code or "").strip().casefold() in {
        str(value).strip().casefold() for value in hotels
    }


def live_rates_url_template(hotel_code: Any) -> str:
    """The hotel's booking-engine search url with {arr}/{dep}, or "" when unset."""
    try:
        mapping = json.loads(settings.chat_live_rates_url_by_hotel_json or "{}")
    except (TypeError, ValueError):
        return ""
    if not isinstance(mapping, dict):
        return ""
    code = str(hotel_code or "").strip().casefold()
    for key, url in mapping.items():
        if str(key or "").strip().casefold() == code and "{arr}" in str(url) and "{dep}" in str(url):
            return str(url)
    return ""


def live_rates_url(hotel_code: Any, check_in_iso: str, check_out_iso: str) -> str:
    """Fill the template with dd/mm/yyyy dates; "" when unset or a date is bad."""
    from datetime import date
    from urllib.parse import quote

    template = live_rates_url_template(hotel_code)
    if not template:
        return ""
    try:
        arr, dep = date.fromisoformat(check_in_iso), date.fromisoformat(check_out_iso)
    except (TypeError, ValueError):
        return ""
    if dep <= arr:
        return ""
    fmt = lambda d: quote(d.strftime("%d/%m/%Y"), safe="")
    return template.replace("{arr}", fmt(arr)).replace("{dep}", fmt(dep))


def booking_ledger(context: Any, conversation_id: str = "") -> list[dict[str, Any]]:
    pending = getattr(context, "pending_data", None) or {}
    active = str(pending.get("_active_conversation_uid") or "").strip()
    conversation_id = str(conversation_id or active).strip()
    if active and conversation_id and active != conversation_id:
        return []
    return [
        dict(row) for row in (pending.get("_rb_bookings") or [])
        if isinstance(row, dict) and (
            not row.get("conversation_id")
            or str(row["conversation_id"]) == conversation_id
        )
    ]


def details_submitted(context: Any, conversation_id: str = "") -> bool:
    # Silent booking-intent tickets also live in this ledger. Only a successful
    # form submission spends the form, not merely the existence of a ticket.
    return any(row.get("details_submitted") is True for row in booking_ledger(context, conversation_id))


def last_response_language(context: Any, conversation_id: str = "") -> dict[str, Any]:
    pending = getattr(context, "pending_data", None) or {}
    conversation_id = str(conversation_id or pending.get("_active_conversation_uid") or "")
    for message in reversed(getattr(context, "messages", []) or []):
        meta = getattr(message, "metadata", None) or {}
        if conversation_id and str(meta.get("conversation_uid") or "") != conversation_id:
            continue
        role = getattr(message, "role", None)
        if str(getattr(role, "value", role)) != "assistant":
            continue
        language = meta.get("response_language")
        if isinstance(language, dict) and language.get("language") and language.get("script"):
            return dict(language)
    return {}


def prefetch_revision(context: Any, conversation_id: str = "") -> str:
    """Revision of the facts that change what a speculative reply may say."""
    pending = getattr(context, "pending_data", None) or {}
    language = last_response_language(context, conversation_id)
    state = {
        "conversation_id": str(conversation_id or pending.get("_active_conversation_uid") or ""),
        "bookings": booking_ledger(context, conversation_id),
        "language": {key: str(language.get(key) or "").casefold() for key in ("language", "script", "style")},
        "properties": sorted(str(value) for value in (pending.get("_active_property_ids") or [])),
    }
    return hashlib.sha256(json.dumps(state, sort_keys=True, default=str).encode()).hexdigest()
