"""
Weather Service

Resolves the right city/coords for a guest's weather ask, then fetches current
conditions from Open-Meteo (free, no API key).

Resolution order in `resolve_and_fetch_weather`:
    1. Asked city — LLM extracts a city/place from the user's message.
    2. Active property — first id in context.selected_property_ids; reads
       new_bot_properties.location.{coordinates,city}.
    3. Hotel default — business.city from capability summary.

Coordinates are preferred over city strings whenever present (skips the
geocoding hop).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Optional

import httpx

from llm.client import llm_client
from services.config_service import config_service

logger = logging.getLogger(__name__)

_CACHE_TTL_MINUTES = 30
_CITY_CACHE: dict[str, dict[str, Any]] = {}
_COORDS_CACHE: dict[str, dict[str, Any]] = {}


def clear_weather_caches() -> None:
    """Drop both city and coordinate caches. Used after geocoding-table updates."""
    _CITY_CACHE.clear()
    _COORDS_CACHE.clear()

WEATHER_CODE_MAPPING = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snow fall",
    73: "Moderate snow fall",
    75: "Heavy snow fall",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}


# ── Open-Meteo primitives ──────────────────────────────────────────────────────

# Hard-coded coords for ambiguous Indian regions where Open-Meteo's free-text
# geocoding misfires. "Goa" returns Genoa, Italy. "South Goa" returns nothing.
# "North Goa" returns nothing. Map these to actual Indian coordinates so the
# property fallback chain succeeds when only a region label is stored.
_INDIAN_REGION_COORDS: dict[str, tuple[float, float]] = {
    "goa": (15.4909, 73.8278),               # Panaji (Goa capital)
    "south goa": (15.2832, 73.9862),         # Margao
    "north goa": (15.5933, 73.7385),         # Mapusa
    "porvorim": (15.5093, 73.8278),
    "benaulim": (15.2643, 73.9281),
    "calangute": (15.5450, 73.7551),
    "anjuna": (15.5736, 73.7401),
    "manali": (32.2396, 77.1887),
    "dapoli": (17.7593, 73.1832),
    "murud": (17.9143, 73.1437),
    "konark": (19.8876, 86.0945),
    "puri": (19.8135, 85.8312),
    "rishikesh": (30.0869, 78.2676),
    "lonavala": (18.7546, 73.4062),
    "panchgani": (17.9249, 73.7997),
    "shimla": (31.1048, 77.1734),
    "jamnagar": (22.4707, 70.0577),
    "ayodhya": (26.7922, 82.1998),
    "sambhajinagar": (19.8762, 75.3433),
    "aurangabad": (19.8762, 75.3433),
    "bhubaneswar": (20.2961, 85.8245),
    "bhubaneshwar": (20.2961, 85.8245),
    "ira_orchid_porvorim": (15.5093, 73.8278),
    "orchid_passaros_goa_south_goa": (15.2643, 73.9281),
    "lotus_eco_beach_resort_murud_dapoli": (17.7593, 73.1832),
    "lotus_eco_beach_resort_konark": (19.8876, 86.0945),
    "fort_jadhavgadh": (18.4523, 74.0269),
}


async def _geocode_city(city: str, country_code: str = "IN") -> Optional[tuple[float, float]]:  # noqa: D401
    """Geocode a city to (lat, lon). Defaults to country_code='IN' so ambiguous
    Indian city names (Goa, Pune, etc.) don't lose to European/American matches.
    Falls back to a hard-coded Indian-region table for terms Open-Meteo's
    free-text search misses ('South Goa', 'North Goa', 'Porvorim', etc.).
    """
    if not city:
        return None
    key = city.strip().lower()
    # Fast hard-coded path for known-ambiguous Indian regions / property slugs.
    if key in _INDIAN_REGION_COORDS:
        return _INDIAN_REGION_COORDS[key]

    url = "https://geocoding-api.open-meteo.com/v1/search"
    params = {"name": city, "count": 5, "language": "en", "format": "json"}
    if country_code:
        params["country_code"] = country_code
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
            results = data.get("results") or []
            # Prefer a result whose country_code matches what we asked for.
            for r in results:
                if not country_code or str(r.get("country_code") or "").upper() == country_code.upper():
                    return float(r.get("latitude")), float(r.get("longitude"))
            if results:
                loc = results[0]
                return float(loc.get("latitude")), float(loc.get("longitude"))
    except Exception as e:
        logger.warning("[WeatherService] geocode failed for %r: %s", city, e)
    return None


async def _fetch_current(lat: float, lon: float) -> Optional[dict]:
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "current_weather": "true",
        "timezone": "auto",
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json().get("current_weather")
    except Exception as e:
        logger.warning("[WeatherService] forecast fetch failed for %s,%s: %s", lat, lon, e)
    return None


def _format_summary(weather: dict) -> str:
    temp = weather.get("temperature", "unknown")
    code = weather.get("weathercode", -1)
    condition = WEATHER_CODE_MAPPING.get(code, "Unknown condition")
    return f"{condition}, {temp}°C"


def _cache_get(bucket: dict[str, dict[str, Any]], key: str) -> Optional[str]:
    entry = bucket.get(key)
    if not entry:
        return None
    if datetime.now() - entry["timestamp"] >= timedelta(minutes=_CACHE_TTL_MINUTES):
        bucket.pop(key, None)
        return None
    return entry["data"]


def _cache_put(bucket: dict[str, dict[str, Any]], key: str, value: str) -> None:
    bucket[key] = {"timestamp": datetime.now(), "data": value}


# ── Public fetch helpers ───────────────────────────────────────────────────────

async def get_weather_for_coords(lat: float, lon: float, label: str = "") -> str:
    """Fetch current weather for explicit coordinates. `label` is the city name
    used in the formatted output (and as the cache discriminator)."""
    cache_key = f"{round(lat, 2)},{round(lon, 2)}"
    cached = _cache_get(_COORDS_CACHE, cache_key)
    if cached:
        return cached

    weather = await _fetch_current(lat, lon)
    if not weather:
        return ""

    city_label = (label or "").strip() or "this location"
    summary = f"{city_label}: {_format_summary(weather)}"
    _cache_put(_COORDS_CACHE, cache_key, summary)
    return summary


async def get_weather_for_city(city: str) -> str:
    """Fetch current weather for a city by name. Geocodes via Open-Meteo."""
    city_clean = (city or "").strip()
    if not city_clean:
        return ""

    cache_key = city_clean.lower()
    cached = _cache_get(_CITY_CACHE, cache_key)
    # Sanity check: if a known-ambiguous Indian region is cached with the
    # Europe-mapped Genoa temperature signature (18°C overcast), it's the old
    # broken geocoding result — invalidate and refetch.
    if cached and cache_key in _INDIAN_REGION_COORDS:
        if "18.0°C" in cached or "18°C" in cached:
            _CITY_CACHE.pop(cache_key, None)
            cached = None
    if cached:
        return cached

    coords = await _geocode_city(city_clean)
    if not coords:
        return ""

    weather = await _fetch_current(*coords)
    if not weather:
        return ""

    summary = f"{city_clean}: {_format_summary(weather)}"
    _cache_put(_CITY_CACHE, cache_key, summary)
    return summary


# ── Property-aware resolution ──────────────────────────────────────────────────

async def _lookup_property_location(hotel_code: str, property_slug: str) -> dict[str, Any]:
    """Return the `location` JSON for a property (city/coordinates), or empty dict."""
    if not hotel_code or not property_slug:
        return {}
    try:
        from sqlalchemy import func, select
        from models.database import AsyncSessionLocal, Hotel, Property

        async with AsyncSessionLocal() as session:
            hotel = (
                await session.execute(
                    select(Hotel).where(func.lower(Hotel.code) == str(hotel_code).strip().lower())
                )
            ).scalar_one_or_none()
            if hotel is None:
                return {}

            row = (
                await session.execute(
                    select(Property).where(
                        Property.hotel_id == hotel.id,
                        Property.property_slug == str(property_slug).strip(),
                    )
                )
            ).scalar_one_or_none()
            if row is None or not isinstance(row.location, dict):
                return {}
            return dict(row.location)
    except Exception as e:
        logger.warning("[WeatherService] property location lookup failed: %s", e)
        return {}


async def _lookup_hotel_city(hotel_code: str) -> str:
    """The hotel's own city from `new_bot_hotels.city` (the per-hotel source of
    truth), or "" when unset. Used as the location fallback so weather resolves
    to the hotel's ACTUAL city for any tenant — never a hard-coded default."""
    if not hotel_code:
        return ""
    try:
        from sqlalchemy import func, select
        from models.database import AsyncSessionLocal, Hotel

        async with AsyncSessionLocal() as session:
            hotel = (
                await session.execute(
                    select(Hotel).where(func.lower(Hotel.code) == str(hotel_code).strip().lower())
                )
            ).scalar_one_or_none()
            if hotel is None:
                return ""
            return str(getattr(hotel, "city", "") or "").strip()
    except Exception as e:
        logger.warning("[WeatherService] hotel city lookup failed: %s", e)
        return ""


def _coords_from_location(loc: dict[str, Any]) -> Optional[tuple[float, float]]:
    coords = loc.get("coordinates") if isinstance(loc, dict) else None
    if not isinstance(coords, dict):
        return None
    lat = coords.get("lat") if "lat" in coords else coords.get("latitude")
    lon = coords.get("lon") if "lon" in coords else coords.get("longitude")
    try:
        if lat is None or lon is None:
            return None
        return float(lat), float(lon)
    except (TypeError, ValueError):
        return None


# ── LLM city extraction ────────────────────────────────────────────────────────

_CITY_EXTRACTOR_SYSTEM = (
    "You extract a single city or place name from a hotel-guest message about "
    "weather. Return STRICT JSON only: {\"city\": \"<name or empty string>\"}.\n"
    "Rules:\n"
    "- If the guest names a city, town, neighborhood, or country, return it.\n"
    "- If the guest references the property/hotel itself ('here', 'there', "
    "'at the hotel', 'in the area'), return empty string — the caller will use "
    "the property's location.\n"
    "- If the guest does not name any place, return empty string.\n"
    "- Do not guess. Do not add commentary. JSON only."
)


async def _extract_city_via_llm(message: str) -> str:
    text = (message or "").strip()
    if not text:
        return ""
    try:
        raw = await llm_client.chat(
            messages=[
                {"role": "system", "content": _CITY_EXTRACTOR_SYSTEM},
                {"role": "user", "content": text},
            ],
            temperature=0.0,
            max_tokens=40,
            trace_context={"agent": "weather_city_extractor"},
        )
        cleaned = (raw or "").strip()
        if cleaned.startswith("```"):
            parts = cleaned.split("```")
            if len(parts) >= 2:
                cleaned = parts[1]
                if cleaned.lower().startswith("json"):
                    cleaned = cleaned[4:]
                cleaned = cleaned.strip()
        data = json.loads(cleaned) if cleaned else {}
        city = str(data.get("city") or "").strip()
        return city
    except Exception as e:
        logger.warning("[WeatherService] city extraction failed: %s", e)
        return ""


# ── Main entry point used by orchestration ────────────────────────────────────

async def resolve_and_fetch_weather(
    context: Any,
    user_message: str,
    pre_extracted_city: Optional[str] = None,
    property_ids_override: Optional[list[str]] = None,
) -> Optional[dict[str, str]]:
    """
    Returns a dict ready to inject under memory_facts.live_weather, or None.

    Shape:
        {"city": "Bandra", "summary": "Light rain, 26°C", "source": "..."}
    `source` is one of: "asked_city", "active_property", "hotel_default".

    `pre_extracted_city` lets upstream callers (e.g. the main router which
    already classified weather intent and named the city) skip the internal
    LLM city extractor. Pass an empty string to force the property/default
    fallback chain; pass None to run the internal extractor; pass a city name
    to use it directly.

    `property_ids_override` lets the orchestrator pass the property scope it
    just resolved on THIS turn (e.g. from the router's selected_property_ids)
    instead of reading from `context.pending_data._active_property_ids` —
    which lags one turn behind because it's persisted AFTER the weather
    fetch fires.
    """
    hotel_code = str(getattr(context, "hotel_code", "") or "DEFAULT")

    # 1) asked city — either supplied by the caller or extracted via LLM.
    if pre_extracted_city is None:
        asked = await _extract_city_via_llm(user_message)
    else:
        asked = (pre_extracted_city or "").strip()
    if asked:
        summary = await get_weather_for_city(asked)
        if summary:
            return {"city": asked, "summary": summary, "source": "asked_city"}

    # 2) active property → coords preferred, else city.
    # Property scope lives in `pending_data`, not on a top-level context attr.
    # Both the underscore-prefixed (`_active_property_ids`) and bare keys are
    # used in different parts of the codebase, so check all of them.
    property_ids: list[str] = []
    if isinstance(property_ids_override, list) and property_ids_override:
        property_ids = [str(s).strip() for s in property_ids_override if str(s).strip()]
    if not property_ids:
        pending = getattr(context, "pending_data", None) or {}
        if not isinstance(pending, dict):
            pending = {}
        for key in (
            "_active_property_ids",
            "active_property_ids",
            "_selected_property_scope_ids",
            "selected_property_scope_ids",
        ):
            raw = pending.get(key)
            if isinstance(raw, list) and raw:
                property_ids = [str(s).strip() for s in raw if str(s).strip()]
                break
            if isinstance(raw, str) and raw.strip():
                property_ids = [raw.strip()]
                break
    if not property_ids:
        # Last-resort backwards compat: some callers do attach the field.
        legacy = getattr(context, "selected_property_ids", None)
        if isinstance(legacy, list):
            property_ids = [str(s).strip() for s in legacy if str(s).strip()]
    for slug in property_ids:
        loc = await _lookup_property_location(hotel_code, slug)
        if not loc:
            continue
        # Coords first if available — most reliable.
        coords = _coords_from_location(loc)
        primary_label = str(loc.get("city") or loc.get("label") or slug).strip()
        if coords:
            summary = await get_weather_for_coords(*coords, label=primary_label)
            if summary:
                return {"city": primary_label, "summary": summary, "source": "active_property"}
        # Try city, label, then slug (city alone may be a sub-area like
        # "South Goa" the weather API doesn't recognise; the broader
        # `label` typically does).
        candidate_labels: list[str] = []
        for raw in (loc.get("city"), loc.get("label"), slug):
            value = str(raw or "").strip()
            if value and value not in candidate_labels:
                candidate_labels.append(value)
        for label in candidate_labels:
            summary = await get_weather_for_city(label)
            if summary:
                return {"city": label, "summary": summary, "source": "active_property"}

    # 3) hotel default — the hotel's OWN configured city. Try the capability
    # summary's business.city first, then the authoritative `new_bot_hotels.city`.
    # NO hard-coded fallback city: if the hotel has no resolvable/geocodable city
    # we return None so the answerer honestly says it can't fetch weather, rather
    # than reporting some unrelated city's weather as if it were the hotel's.
    default_candidates: list[str] = []
    try:
        cap = config_service.get_capability_summary(hotel_code)
        biz_city = str((cap.get("business") or {}).get("city") or "").strip()
        if biz_city:
            default_candidates.append(biz_city)
    except Exception as e:
        logger.warning("[WeatherService] capability summary failed: %s", e)
    hotel_city = await _lookup_hotel_city(hotel_code)
    if hotel_city and hotel_city not in default_candidates:
        default_candidates.append(hotel_city)
    for default_city in default_candidates:
        summary = await get_weather_for_city(default_city)
        if summary:
            return {"city": default_city, "summary": summary, "source": "hotel_default"}

    return None


# ── Backwards-compat wrapper (legacy callers) ─────────────────────────────────

async def get_current_weather(hotel_code: str = "DEFAULT") -> str:
    """Legacy entry point. Prefer `resolve_and_fetch_weather` for new callers."""
    try:
        cap = config_service.get_capability_summary(hotel_code)
        city = str((cap.get("business") or {}).get("city") or "Mumbai").strip()
    except Exception as e:
        logger.warning("[WeatherService] legacy hotel city resolve failed: %s", e)
        city = "Mumbai"
    return await get_weather_for_city(city)
