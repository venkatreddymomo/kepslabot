"""Nearby places and real driving distances, via Google Maps.

Replaces the two things the previous approach could not do reliably:

  * DISCOVERY. Web search found places but was inconsistent — measured on the
    live bot, "any hospitals nearby?" returned nothing for Calangute while
    "where can I get medicines?" answered fine seconds later in the same
    session. Google answers both every time.
  * DISTANCE. Web search was out by 31% and 64% on two of five test places and
    fabricated a maps URL every single time, so distances were suppressed
    entirely. Routes gives the real road distance.

Two HTTP calls per lookup, no more:
  1. places:searchText  — the guest's OWN wording is the query. No keyword
     table mapping "medicines" to a pharmacy type; Google resolves the intent,
     which also means new phrasings need no code change.
  2. computeRouteMatrix — every result priced in one request, not one each.

Never raises and never blocks a reply: any failure returns an empty list and
the caller falls back to web search exactly as before.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Optional

import httpx

from config.settings import settings

logger = logging.getLogger(__name__)

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
MATRIX_URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"

# Product rule: never present anything beyond 100 km from the hotel. Same fence
# the OSM path used (MAX_RADIUS_KM there), and it doubles as the safety net for
# a query that resolves to a same-named place in another state.
MAX_RADIUS_M = 100_000

# The SEARCH is biased far tighter than the fence, because "nearby" should mean
# nearby: Google ranks within this circle, and the 100 km fence only removes
# anything that still slipped through. Widened only when a tight search returns
# nothing at all — see nearby().
DEFAULT_BIAS_M = 8000
WIDE_BIAS_M = 50000
MAX_RESULTS = 4

_CACHE_TTL_SECONDS = 6 * 60 * 60
_CACHE_MAX = 500
_cache: dict[str, tuple[float, Any]] = {}


def _cache_get(key: str) -> tuple[bool, Any]:
    hit = _cache.get(key)
    if not hit:
        return False, None
    at, value = hit
    if (time.monotonic() - at) > _CACHE_TTL_SECONDS:
        _cache.pop(key, None)
        return False, None
    return True, value


def _cache_put(key: str, value: Any) -> None:
    if len(_cache) >= _CACHE_MAX:
        _cache.clear()
    _cache[key] = (time.monotonic(), value)


class GooglePlacesService:
    def __init__(self) -> None:
        self._client: Optional[httpx.AsyncClient] = None

    @staticmethod
    def is_enabled() -> bool:
        return bool(
            str(getattr(settings, "google_maps_api_key", "") or "").strip()
            and getattr(settings, "chat_google_places_enabled", False)
        )

    @staticmethod
    def _key() -> str:
        return str(getattr(settings, "google_maps_api_key", "") or "").strip()

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(12.0))
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def _post(self, url: str, body: dict, field_mask: str) -> Optional[Any]:
        try:
            client = await self._http()
            r = await client.post(
                url,
                json=body,
                headers={
                    "X-Goog-Api-Key": self._key(),
                    "X-Goog-FieldMask": field_mask,
                    "Content-Type": "application/json",
                },
            )
            if r.status_code != 200:
                logger.warning("google places %s -> %s %s", url.rsplit("/", 1)[-1],
                               r.status_code, r.text[:160])
                return None
            return r.json()
        except Exception as exc:
            logger.warning("google places call failed: %s: %s", type(exc).__name__, exc)
            return None

    async def nearby(
        self,
        *,
        question: str,
        origin: tuple[float, float],
        radius_m: int = DEFAULT_BIAS_M,
        limit: int = MAX_RESULTS,
    ) -> list[dict[str, Any]]:
        """Places matching the guest's question, nearest first, with real distances.

        Searches the tight radius first and only widens if that finds nothing —
        a hill property can have its closest hospital 20 km away, and answering
        "there are none" there would be wrong. Nothing beyond MAX_RADIUS_M is
        ever returned either way.

        Returns [] on any failure, so the caller can fall back to web search.
        """
        q = re.sub(r"\s+", " ", str(question or "")).strip()
        if not self.is_enabled() or len(q) < 3 or not origin:
            return []
        # Google itself is capped at MAX_RESULTS — this is a paid API and an
        # unbounded nearby search is not useful to a guest anyway. The gallery
        # caps are the ones that were removed, not this.
        lat, lng = float(origin[0]), float(origin[1])
        radius = float(min(max(int(radius_m), 500), MAX_RADIUS_M))

        cache_key = f"{q.lower()}|{lat:.4f},{lng:.4f}|{int(radius)}"
        found, cached = _cache_get(cache_key)
        if found:
            return list(cached or [])

        started = time.monotonic()
        rows: list[dict[str, Any]] = []
        for attempt_radius in (radius, float(WIDE_BIAS_M)):
            data = await self._post(
                SEARCH_URL,
                {
                    "textQuery": q,
                    "maxResultCount": max(1, min(int(limit), MAX_RESULTS)),
                    "locationBias": {
                        "circle": {
                            "center": {"latitude": lat, "longitude": lng},
                            "radius": attempt_radius,
                        }
                    },
                },
                # Open-now, hours, rating and phone ride on the SAME request, so
                # they cost nothing extra — and they are what a guest actually
                # needs when they ask for a pharmacy at 10pm. A list of shut
                # shops is worse than no list.
                "places.displayName,places.formattedAddress,places.location,"
                "places.types,"
                "places.rating,places.userRatingCount,places.nationalPhoneNumber,"
                "places.currentOpeningHours.openNow,"
                "places.regularOpeningHours.weekdayDescriptions",
            )
            for p in ((data or {}).get("places") or []):
                name = ((p.get("displayName") or {}).get("text") or "").strip()
                loc = p.get("location") or {}
                if not name or "latitude" not in loc:
                    continue
                # NEVER return another place to stay. Asked "which hotel is
                # closer to a beach", this returned a Hilton in Huntington
                # Beach, California; asked "any hotel in coimbatore" it listed
                # Treebo, IVY Residency and Hotel O. Recommending a competitor
                # is the worst thing this feature can do, and nothing anywhere
                # prevented it.
                #
                # Every hotel and resort Google returns carries "lodging" in its
                # types; pharmacies and restaurants do not. Checking that is
                # safer than the primary type, because a resort is also typed
                # "restaurant" and would otherwise slip through on a dining ask.
                types = {str(t).lower() for t in (p.get("types") or [])}
                if types & {"lodging", "hotel", "resort_hotel", "motel", "hostel",
                            "inn", "guest_house", "bed_and_breakfast",
                            "extended_stay_hotel", "apartment_hotel", "campground"}:
                    continue

                hours = (p.get("regularOpeningHours") or {}).get("weekdayDescriptions") or []
                rows.append({
                    "name": name,
                    "address": str(p.get("formattedAddress") or "").strip(),
                    "lat": float(loc["latitude"]),
                    "lng": float(loc["longitude"]),
                    "open_now": (p.get("currentOpeningHours") or {}).get("openNow"),
                    "hours": hours,
                    "rating": p.get("rating"),
                    "ratings_count": p.get("userRatingCount"),
                    "phone": str(p.get("nationalPhoneNumber") or "").strip(),
                })
            if rows or attempt_radius >= WIDE_BIAS_M:
                break
            logger.info("[GMAPS] nothing within %.0fm for %r — widening", attempt_radius, q[:40])
        if not rows:
            logger.info("[GMAPS] no places for %r (%.1fs)", q[:50], time.monotonic() - started)
            return []

        await self._attach_distances(origin=(lat, lng), rows=rows)
        # Beyond the fence, or no road route at all, is OUT.
        #
        # This previously kept rows whose distance could not be resolved, on the
        # reasoning that a missing number should not lose a good result. That is
        # backwards for a safety fence, and it showed: asked "which hotel is
        # closer to a beach" from Goa, the reply included The Waterfront Beach
        # Resort in Huntington Beach, California. Nothing can be driven from Goa
        # to California, so Routes returned no route, meters stayed None, and
        # None sailed through the 100 km check. Unroutable now means out.
        rows = [
            r for r in rows
            if r.get("meters") is not None and r["meters"] <= MAX_RADIUS_M
        ]
        rows.sort(key=lambda r: r["meters"])
        logger.info("[GMAPS] %d places for %r in %.1fs", len(rows), q[:50], time.monotonic() - started)
        _cache_put(cache_key, rows)
        return rows

    async def _attach_distances(self, *, origin: tuple[float, float], rows: list[dict[str, Any]]) -> None:
        """Real driving distance for every row, in a single request."""
        if not rows:
            return
        body = {
            "origins": [{"waypoint": {"location": {"latLng": {
                "latitude": origin[0], "longitude": origin[1]}}}}],
            "destinations": [
                {"waypoint": {"location": {"latLng": {"latitude": r["lat"], "longitude": r["lng"]}}}}
                for r in rows
            ],
            "travelMode": "DRIVE",
        }
        data = await self._post(
            MATRIX_URL, body,
            "originIndex,destinationIndex,distanceMeters,duration,condition",
        )
        if not isinstance(data, list):
            return
        for cell in data:
            try:
                if str(cell.get("condition") or "") != "ROUTE_EXISTS":
                    continue
                idx = int(cell.get("destinationIndex"))
                rows[idx]["meters"] = int(cell.get("distanceMeters") or 0)
                secs = str(cell.get("duration") or "").rstrip("s")
                rows[idx]["minutes"] = int(round(float(secs) / 60.0)) if secs else None
            except Exception:
                continue

    async def distance_to_place(
        self,
        *,
        origin: tuple[float, float],
        place: str,
        city: str = "",
    ) -> Optional[dict[str, Any]]:
        """Real driving (and walking) time from a property to ONE named place.

        For the places the hotel already publishes a distance to — airport,
        station, a landmark the guest names. The KB gives kilometres and a guest
        thinks in minutes: "40 km from Dabolim" means little, "about an hour's
        drive" decides a booking.

        Walking is only measured, and only reported, when the place is close
        enough for it to be a real option — the airport is a 497-minute walk and
        nobody needs to be told that.

        Cached for 6 hours per property+place. Returns None on any failure, so
        the answerer just falls back to the KB text.
        """
        p = re.sub(r"\s+", " ", str(place or "")).strip()
        if not self.is_enabled() or len(p) < 3 or not origin:
            return None
        dest = f"{p}, {city}".strip(", ") if city else p
        key = f"dist|{origin[0]:.4f},{origin[1]:.4f}|{dest.lower()}"
        found, cached = _cache_get(key)
        if found:
            return cached

        async def one(mode: str) -> Optional[tuple[int, int]]:
            data = await self._post(
                "https://routes.googleapis.com/directions/v2:computeRoutes",
                {"origin": {"location": {"latLng": {"latitude": origin[0], "longitude": origin[1]}}},
                 "destination": {"address": dest},
                 "travelMode": mode},
                "routes.distanceMeters,routes.duration",
            )
            try:
                r = (data or {}).get("routes") or []
                if not r:
                    return None
                secs = str(r[0].get("duration") or "").rstrip("s")
                return int(r[0].get("distanceMeters") or 0), int(round(float(secs) / 60.0))
            except Exception:
                return None

        drive = await one("DRIVE")
        if not drive or drive[0] > MAX_RADIUS_M:
            _cache_put(key, None)
            return None
        meters, drive_min = drive

        walk_min = None
        if meters <= 3000:                       # only worth measuring if plausibly walkable
            walked = await one("WALK")
            if walked and walked[1] <= 30:
                walk_min = walked[1]

        out = {"place": p, "meters": meters, "drive_min": drive_min, "walk_min": walk_min}
        _cache_put(key, out)
        logger.info("[GMAPS] %s -> %.1f km / %s min drive%s",
                    p[:34], meters / 1000, drive_min,
                    f" / {walk_min} min walk" if walk_min else "")
        return out

    @staticmethod
    def distance_as_kb_fact(d: dict[str, Any], hotel_name: str = "") -> str:
        if not d:
            return ""
        km = d["meters"] / 1000.0
        dist = f"{km:.1f} km" if km >= 1 else f"{int(d['meters'])} m"
        line = f"- {d['place']}: {dist} by road, about {d['drive_min']} minutes by car"
        who = f" from {hotel_name}" if hotel_name else ""
        out = [
            f"VERIFIED DISTANCE (Google Maps, real road route{who}) — prefer these "
            f"numbers over any distance in the knowledge base:",
            line,
        ]
        # The walking figure kept getting dropped: the block carried "roughly a
        # 24 minute walk" for Calangute Beach and the reply mentioned only the
        # drive, twice out of two. It is the more useful half for a guest deciding
        # whether to bother with a taxi, so it gets its own instruction rather
        # than sitting in brackets where it reads as optional colour.
        if d.get("walk_min"):
            out.append(
                f"- It is also about a {d['walk_min']} minute WALK. Say this too — "
                f"a guest deciding between walking and a taxi needs both numbers."
            )
        return "\n".join(out)

    @staticmethod
    def as_kb_fact(rows: list[dict[str, Any]], hotel_name: str = "") -> str:
        """Format for injection as a grounded fact block for the answerer."""
        if not rows:
            return ""
        who = f" from {hotel_name}" if hotel_name else ""
        # The scope gate declines "where can I get medicines?" some of the time,
        # answering "that isn't covered in the information I have" even with
        # four verified pharmacies sitting right here. Same question answers
        # correctly on another run, so it is the scope judgement wavering rather
        # than missing data. Stating in-scope inside the block settles it, since
        # this text arrives with the KB the answerer is told to trust.
        lines = [
            f"NEARBY PLACES (verified by Google Maps, road distance{who}):",
            "READ THIS BEFORE ANSWERING. These are real, verified places around "
            "this hotel, looked up moments ago for THIS guest. You DO have this "
            "information — it is directly below.",
            "- Helping a guest find what is near the hotel is IN SCOPE. Never "
            "reply that this is outside what you help with.",
            "- Never reply that you do not have the information, cannot arrange "
            "it, or that it is not in the hotel's details. That would be false: "
            "the answer is in this list.",
            "- Give the names WITH their distances. Include the opening status "
            "and phone number when one is shown for that place.",
            "- CHOOSE, do not dump. Pick the few that genuinely answer what was "
            "asked and write them in your own words. This is a list of facts for "
            "you to use, not a reply to paste.",
            "- NEVER suggest another hotel, resort, guest house or anywhere else "
            "to stay, whatever the guest asks. Recommending somewhere else to "
            "sleep is never acceptable.",
        ]
        for r in rows:
            bits = [f"- {r['name']}"]
            if r.get("meters") is not None:
                km = r["meters"] / 1000.0
                bits.append(f"{km:.1f} km" if km >= 1 else f"{int(r['meters'])} m")
            if r.get("minutes"):
                bits.append(f"about {r['minutes']} min by car")
            # open/closed decides whether a place is any use right now, so it
            # goes near the front rather than trailing after the address.
            if r.get("open_now") is True:
                bits.append("OPEN NOW")
            elif r.get("open_now") is False:
                bits.append("CLOSED RIGHT NOW")
            hours = r.get("hours") or []
            if any("24 hours" in str(h) for h in hours):
                bits.append("open 24 hours")
            if r.get("rating"):
                count = f" from {r['ratings_count']} reviews" if r.get("ratings_count") else ""
                bits.append(f"rated {r['rating']}/5{count}")
            if r.get("phone"):
                bits.append(f"phone {r['phone']}")
            if r.get("address"):
                bits.append(r["address"])
            lines.append(" | ".join(bits))
        return "\n".join(lines)


google_places_service = GooglePlacesService()
