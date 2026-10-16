"""Reader for pre-stored location-intro answers (new_bot_location_intros).

The dropdown short-circuit serves these curated bodies straight from the DB
instead of calling the LLM, so a location pick renders instantly. Content is
authored/edited directly in the DB row (see tools/seed_location_intros.py to
seed it from intromessage.txt). Rarely changes at runtime, so a short TTL cache
keeps the VPN→MySQL latency off the hot path.

Deliberately dependency-light: raw text() selects over the shared async engine,
no ORM model, so adding this table needs no models/database.py change.
"""

from __future__ import annotations

import time
from typing import Optional

from sqlalchemy import text

from models.database import AsyncSessionLocal
from services.kb_table_reader import _resolve_hotel_id

# location_id -> {"body", "label"} per hotel_code, TTL-cached.
_CACHE_TTL_SECONDS = 300.0
_CACHE: dict[str, tuple[float, dict[str, dict[str, str]]]] = {}


async def _load_all(hotel_code: str) -> dict[str, dict[str, str]]:
    """Return {location_id: {"body", "label"}} for every active intro row."""
    async with AsyncSessionLocal() as session:
        hotel_id = await _resolve_hotel_id(session, hotel_code)
        if not hotel_id:
            return {}
        result = await session.execute(
            text(
                "SELECT location_id, location_label, body "
                "FROM new_bot_location_intros "
                "WHERE hotel_id = :hid AND is_active = 1"
            ),
            {"hid": hotel_id},
        )
        out: dict[str, dict[str, str]] = {}
        for row in result.mappings():
            lid = str(row.get("location_id") or "").strip()
            body = str(row.get("body") or "").strip()
            if not lid or not body:
                continue
            out[lid] = {"body": body, "label": str(row.get("location_label") or "").strip()}
        return out


async def _get_map(hotel_code: str) -> dict[str, dict[str, str]]:
    code = str(hotel_code or "").strip()
    if not code:
        return {}
    now = time.monotonic()
    cached = _CACHE.get(code)
    if cached and (now - cached[0]) <= _CACHE_TTL_SECONDS:
        return cached[1]
    try:
        data = await _load_all(code)
    except Exception:
        # Table missing or DB hiccup → behave as "no intros" so the caller
        # falls through to the normal LLM pipeline. Never raise onto the hot path.
        data = {}
    _CACHE[code] = (now, data)
    return data


async def get_intro(*, hotel_code: str, location_id: str) -> Optional[dict[str, str]]:
    """Return {"body", "label"} for a location, or None if there's no active row."""
    lid = str(location_id or "").strip()
    if not lid:
        return None
    data = await _get_map(hotel_code)
    return data.get(lid)


def _flat(s: str) -> str:
    import re as _re

    return _re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


async def find_property_intro_by_name(
    *, hotel_code: str, text: str
) -> Optional[dict[str, str]]:
    """Match a message that IS a property name to its curated pick message.

    Matched on the name rather than on the click's source_type because the WS
    transport rewrites ui_source_type to "typed_input" (see the note above
    _INTRO_PHRASE_RE in api/routes/chat.py) — a source_type gate misses every
    WebSocket pick, which is every pick a guest actually makes. The existing
    city-intro short-circuit works around it the same way.

    Safe to key on text: the WHOLE message must equal a live property name, so
    "is Parra Goa pet friendly?" does not match and still reaches the pipeline.
    Reads the same TTL-cached map as the intros, so it costs no query.
    """
    body_key = _flat(text)
    if not body_key:
        return None
    data = await _get_map(hotel_code)
    for key, row in data.items():
        if not key.startswith("prop::"):
            continue
        if _flat(row.get("label")) == body_key:
            return {
                "slug": key[len("prop::"):],
                "label": str(row.get("label") or ""),
                "body": str(row.get("body") or ""),
            }
    return None


async def get_property_intro(*, hotel_code: str, property_slug: str) -> Optional[dict[str, str]]:
    """Return {"body", "label"} for the message shown when a guest TAPS a hotel.

    Stored in this same table under "prop::<property_slug>" rather than a table
    of its own: the content, the editing workflow and the cache are identical to
    a city intro, and only the key differs. Seeded by
    tools/seed_property_intros.py.
    """
    slug = str(property_slug or "").strip()
    if not slug:
        return None
    data = await _get_map(hotel_code)
    return data.get(f"prop::{slug}")


def invalidate(hotel_code: str | None = None) -> None:
    """Drop the cache (all hotels, or one) so the next read re-hits the DB."""
    if hotel_code is None:
        _CACHE.clear()
    else:
        _CACHE.pop(str(hotel_code).strip(), None)
