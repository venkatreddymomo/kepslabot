"""Accurate distance from a hotel to a named place, via OpenStreetMap.

Split of responsibilities, decided from measurement rather than preference:

  * Web search is good at DISCOVERY ("is there a vet nearby") and bad at
    distance — tested against real routing it was out by 31% and 64% on two of
    five places, and invented a fake Google Maps URL every single time.
  * OpenStreetMap is the opposite: it cannot answer "is there a vet nearby"
    quickly (Overpass took 22-46s), but geocoding one NAMED place and routing to
    it is both fast and exact.

So this module does one job only: given a place the guest named, return the real
road distance and drive time from that property. Discovery stays with web search
in services/web_lookup_service.py.

It also produces the maps link, because the model cannot be trusted to: every
URL it generated in testing was the same fabricated placeholder.

Never raises, never blocks a reply: any failure returns None and the answerer
simply says it does not have that detail.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
import urllib.parse
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OSRM_URL = "https://router.project-osrm.org/route/v1/driving"
USER_AGENT = "kepsla-hotel-bot/1.0 (guest distance lookup; contact: ops@kepsla.com)"

# Product rule: never recommend or measure anything beyond this from the hotel.
# It doubles as the safety fence — an unfenced geocode returns same-named places
# on other continents (a Lansdowne query once resolved to Richmond, Canada).
MAX_RADIUS_KM = 100.0

_CACHE_TTL_SECONDS = 24 * 60 * 60
_CACHE_MAX = 2000
_cache: dict[str, tuple[float, Any]] = {}


def _key(*parts: Any) -> str:
    return "|".join(re.sub(r"\s+", " ", str(p or "").strip().lower()) for p in parts)


def _cache_get(key: str) -> tuple[bool, Any]:
    hit = _cache.get(key)
    if not hit:
        return False, None
    stored_at, value = hit
    if (time.monotonic() - stored_at) > _CACHE_TTL_SECONDS:
        _cache.pop(key, None)
        return False, None
    return True, value


def _cache_put(key: str, value: Any) -> None:
    if len(_cache) >= _CACHE_MAX:
        _cache.clear()
    _cache[key] = (time.monotonic(), value)


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371.0088 * 2 * math.asin(math.sqrt(h))


def maps_url(place: str, city: str = "") -> str:
    """A Google Maps search link the guest can tap.

    Built here, never taken from the model: asked for links, the model returned
    `https://goo.gl/maps/abc123` — the same fabricated placeholder every time.
    A search URL always resolves and needs no API key.
    """
    query = ", ".join(part for part in [str(place or "").strip(), str(city or "").strip()] if part)
    if not query:
        return ""
    return "https://www.google.com/maps/search/?api=1&query=" + urllib.parse.quote(query)


class GeoDistanceService:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            async with self._lock:
                if self._client is None:
                    self._client = httpx.AsyncClient(
                        headers={"User-Agent": USER_AGENT, "Accept-Language": "en"},
                        timeout=httpx.Timeout(6.0, connect=3.0),
                    )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def property_origin(self, property_id: int) -> Optional[tuple[float, float]]:
        """Coordinates for a property, from new_bot_properties.location."""
        key = _key("origin", property_id)
        found, cached = _cache_get(key)
        if found:
            return tuple(cached) if cached else None  # type: ignore[return-value]

        origin: Optional[tuple[float, float]] = None
        try:
            import json as _json

            from sqlalchemy import select as _select

            from models.database import AsyncSessionLocal, Property

            async with AsyncSessionLocal() as session:
                row = (
                    await session.execute(_select(Property).where(Property.id == int(property_id)))
                ).scalars().first()
            location = getattr(row, "location", None) if row is not None else None
            if isinstance(location, str):
                try:
                    location = _json.loads(location)
                except Exception:
                    location = None
            coords = (location or {}).get("coordinates") if isinstance(location, dict) else None
            if isinstance(coords, dict) and coords.get("lat") and coords.get("lon"):
                origin = (float(coords["lat"]), float(coords["lon"]))
        except Exception as exc:
            logger.warning("geo: origin lookup failed for %s: %s", property_id, exc)

        _cache_put(key, list(origin) if origin else None)
        return origin

    async def distance_to(
        self,
        *,
        property_id: int,
        property_name: str,
        city: str,
        place: str,
        budget_seconds: float = 7.0,
    ) -> Optional[dict[str, Any]]:
        """Real road distance to a named place, or None.

        None is a real answer: it means we could not establish this, and the
        caller must let the bot say so rather than estimate.
        """
        place_clean = re.sub(r"\s+", " ", str(place or "").strip())
        if len(place_clean) < 3:
            return None

        cache_key = _key("dist", property_id, place_clean)
        found, cached = _cache_get(cache_key)
        if found:
            return cached

        async def _work() -> Optional[dict[str, Any]]:
            origin = await self.property_origin(int(property_id))
            if origin is None:
                return None
            client = await self._http()

            # Geocode, fenced to the property's city and then to MAX_RADIUS_KM.
            best: Optional[tuple[float, float, str]] = None
            best_km = MAX_RADIUS_KM
            for query in (
                f"{place_clean}, {city}, India" if city else f"{place_clean}, India",
                f"{place_clean}, India",
            ):
                try:
                    response = await client.get(
                        NOMINATIM_URL,
                        params={"q": query, "format": "json", "limit": 5, "countrycodes": "in"},
                    )
                    response.raise_for_status()
                    rows = response.json()
                except Exception as exc:
                    logger.warning("geo: geocode failed for %r: %s", query, exc)
                    continue
                for row in rows if isinstance(rows, list) else []:
                    try:
                        point = (float(row["lat"]), float(row["lon"]))
                    except Exception:
                        continue
                    km = haversine_km(origin, point)
                    if km < best_km:
                        best_km = km
                        best = (point[0], point[1], str(row.get("display_name") or "").strip())
                if best:
                    break
            if best is None:
                return None

            route = None
            try:
                url = f"{OSRM_URL}/{origin[1]},{origin[0]};{best[1]},{best[0]}"
                response = await client.get(
                    url, params={"overview": "false", "alternatives": "false"}
                )
                response.raise_for_status()
                routes = (response.json() or {}).get("routes") or []
                if routes:
                    route = {
                        "km": round(float(routes[0]["distance"]) / 1000.0, 1),
                        "minutes": int(round(float(routes[0]["duration"]) / 60.0)),
                    }
            except Exception as exc:
                logger.warning("geo: routing failed: %s", exc)

            # The 100 km rule applies to the ROAD distance too when we have it.
            if route and route["km"] > MAX_RADIUS_KM:
                return None

            name = best[2].split(",")[0].strip() or place_clean
            return {
                "place": place_clean,
                "matched_name": name,
                "full_name": best[2],
                "property_name": property_name,
                "straight_km": round(best_km, 1),
                "road_km": (route or {}).get("km"),
                "drive_minutes": (route or {}).get("minutes"),
                "maps_url": maps_url(name, city),
            }

        try:
            result = await asyncio.wait_for(_work(), timeout=budget_seconds)
        except asyncio.TimeoutError:
            logger.warning("geo: timed out for %r", place_clean)
            result = None
        except Exception as exc:
            logger.warning("geo: failed for %r: %s", place_clean, exc)
            result = None

        _cache_put(cache_key, result)
        return result

    async def _geocode_best(self, queries: list[str], origin: tuple[float, float]):
        """First query that resolves within MAX_RADIUS_KM. Returns (lat, lon, label, index)."""
        client = await self._http()
        for index, query in enumerate(queries):
            if not query:
                continue
            try:
                response = await client.get(
                    NOMINATIM_URL,
                    params={"q": query + ", India", "format": "json",
                            "limit": 5, "countrycodes": "in"},
                )
                response.raise_for_status()
                rows = response.json()
            except Exception:
                continue
            best, best_km = None, MAX_RADIUS_KM
            for row in rows if isinstance(rows, list) else []:
                try:
                    point = (float(row["lat"]), float(row["lon"]))
                except Exception:
                    continue
                km = haversine_km(origin, point)
                if km < best_km:
                    best_km = km
                    best = (point[0], point[1], str(row.get("display_name") or "")[:80])
            if best:
                return best[0], best[1], best[2], index
        return None

    async def verify_places(
        self,
        *,
        property_id: int,
        property_name: str,
        city: str,
        places: list[tuple[str, str]],
        budget_seconds: float = 12.0,
    ) -> list[dict[str, Any]]:
        """Confirm web-found places and measure each, using name THEN address.

        Geocoding the business name alone fails for small local places —
        OpenStreetMap has no record of "Moitri Veterinary Clinic". Its address
        does resolve, and if the full street address misses we drop the most
        specific parts until the locality matches. That took coverage from 0/4
        to 4/4 on a real set of vet clinics.

        Each result is tagged `precision`:
          exact       - we pinned the place itself; distance is to the door
          approximate - we could only pin its street or locality, so the
                        distance is to that area and MUST be said as "about"
        """
        wanted = [
            (re.sub(r"\s+", " ", str(n or "").strip()), re.sub(r"\s+", " ", str(a or "").strip()))
            for n, a in (places or [])
            if len(str(n or "").strip()) >= 3
        ][:3]  # 3, not 6: each place needs up to 4 geocodes and Nominatim
        #        allows ~1 request/second. Firing 24 in parallel got them all
        #        rate-limited, which is why every distance vanished.
        if not wanted:
            return []

        origin = await self.property_origin(int(property_id))
        if origin is None:
            return []

        async def one(name: str, address: str) -> Optional[dict[str, Any]]:
            # Most specific first: the place itself, then its full address, then
            # progressively broader parts of that address.
            queries = [f"{name}, {city}" if city else name]
            if address:
                queries.append(address)
                parts = [p.strip() for p in address.split(",") if p.strip()]
                # One broadening step only, to keep the request count sane.
                if len(parts) > 2:
                    queries.append(", ".join(parts[-3:]))
            hit = await self._geocode_best(queries, origin)
            if hit is None:
                return None
            lat, lon, label, index = hit
            route = None
            try:
                client = await self._http()
                url = f"{OSRM_URL}/{origin[1]},{origin[0]};{lon},{lat}"
                response = await client.get(
                    url, params={"overview": "false", "alternatives": "false"}
                )
                response.raise_for_status()
                routes = (response.json() or {}).get("routes") or []
                if routes:
                    route = {
                        "km": round(float(routes[0]["distance"]) / 1000.0, 1),
                        "minutes": int(round(float(routes[0]["duration"]) / 60.0)),
                    }
            except Exception:
                route = None
            if route and route["km"] > MAX_RADIUS_KM:
                return None
            return {
                "matched_name": name,
                "full_name": label,
                "property_name": property_name,
                "straight_km": round(haversine_km(origin, (lat, lon)), 1),
                "road_km": (route or {}).get("km"),
                "drive_minutes": (route or {}).get("minutes"),
                # index 0 == matched the business itself
                "precision": "exact" if index == 0 else "approximate",
                "maps_url": maps_url(name, city),
            }

        # Sequential with spacing, and keep whatever resolved when time runs
        # out — a partial list of real distances beats none at all.
        results: list[Any] = []
        deadline = time.monotonic() + budget_seconds
        for name, address in wanted:
            if time.monotonic() >= deadline:
                results.append(None)
                continue
            try:
                results.append(await one(name, address))
            except Exception:
                results.append(None)
            await asyncio.sleep(0.4)

        out: list[dict[str, Any]] = []
        for (name, _addr), result in zip(wanted, results):
            if isinstance(result, dict) and result:
                out.append(result)
            else:
                logger.info("geo: could not place %r near %s — dropped", name, city)
        out.sort(key=lambda r: r["road_km"] if r.get("road_km") is not None else r["straight_km"])
        return out

    @staticmethod
    def verified_places_as_kb_fact(
        property_name: str, results: list[dict[str, Any]]
    ) -> str:
        """Verified nearby places, nearest first, each with a real map link."""
        if not results:
            return ""
        lines = [
            f"[PROPERTY:{property_name} | BOOK:Location & Transport | PAGE:verified_nearby]",
            "=== Nearby places (found online, distances confirmed by road routing) ===",
        ]
        for item in results:
            if item.get("road_km") is not None:
                drive = (
                    f", {item['drive_minutes']} min by car" if item.get("drive_minutes") else ""
                )
                distance = f"{item['road_km']} km by road{drive}"
            else:
                distance = f"about {item['straight_km']} km"
            qualifier = "about " if item.get("precision") == "approximate" else ""
            lines.append(
                f"  - {item['matched_name']}: {qualifier}{distance}"
                f" — [View on Google Maps]({item['maps_url']})"
            )
        lines.append(
            "- These are independent places, not hotel facilities. Quote each "
            "distance exactly as written, keep the word 'about' where it appears "
            "(those are measured to the locality, not the exact door), and "
            "include each map link."
        )
        return "\n".join(lines)

    @staticmethod
    def as_kb_fact(result: dict[str, Any]) -> str:
        if not result:
            return ""
        lines = [
            f"[PROPERTY:{result.get('property_name') or ''} | BOOK:Location & Transport"
            " | PAGE:live_distance]",
            "=== Distance lookup (OpenStreetMap road routing) ===",
            f"- Place: {result.get('matched_name')}",
        ]
        if result.get("road_km") is not None:
            drive = (
                f", about {result['drive_minutes']} minutes by car"
                if result.get("drive_minutes")
                else ""
            )
            lines.append(f"- Distance by road from the hotel: {result['road_km']} km{drive}")
        else:
            lines.append(
                f"- Distance from the hotel: about {result['straight_km']} km straight-line "
                "(road distance unavailable — say 'about' if you use it)"
            )
        if result.get("maps_url"):
            # Spelled out as a ready-made markdown link and marked REQUIRED:
            # given only the bare URL the answerer left it out of the reply.
            lines.append(
                f"- REQUIRED: end the reply with this exact markdown link so the "
                f"guest can tap through to the map — "
                f"[View on Google Maps]({result['maps_url']})"
            )
        lines.append(
            "- Quote the distance exactly as given above. Do not round it into a "
            "different figure and do not add a walking time."
        )
        return "\n".join(lines)


geo_distance_service = GeoDistanceService()
