"""Live web search for questions about the AREA around a hotel.

Why web search and not a maps API: guests do not name places. They ask "is there
a vet nearby", "where can I eat", "anything to do around here". Measured against
OpenStreetMap, those questions are answered either too slowly to use (Overpass:
22-46s) or with big gaps (Nominatim found pharmacies and ATMs but no vet at all).
Web search answered the same questions in about 4 seconds, including for tier-3
towns like Phalodi and Lansdowne.

The hotel's OWN facts — rooms, dining, rates, policies, its published distances —
never come from here. Those stay KB-only. This service exists purely for the
surrounding neighbourhood.

Three fences, all learned from probing this before wiring it up:

1. GEOGRAPHIC. Every answer is pinned to the property's own city. Unfenced, a
   question about a tea stall near Lansdowne came back describing a shop in
   Richmond, Canada — confidently, with opening hours.
2. NO HOTEL COMMERCIALS. Room rates, availability, offers and competitor
   comparisons are refused outright. Verified: three such probes were refused
   with zero searches fired.
3. NOT OUR OWN HOTEL. Asked for nearby restaurants, the model recommended the
   hotel's own in-house restaurant using details scraped off the web. Prompting
   did not stop it, so it is filtered in code below.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Optional

from config.settings import settings

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 6 * 60 * 60
_cache: dict[str, tuple[float, str]] = {}
_CACHE_MAX = 500

# The model is told to emit this when the question is not one it should answer.
NO_ANSWER = "NONE"

INSTRUCTIONS = """You answer a hotel guest's question about the AREA AROUND their hotel, using web search.

THE HOTEL: {hotel}
ITS CITY / AREA: {city}

YOUR JOB: search the web and name real, independent places near the hotel.
This includes parks, shops, sweet shops, repair shops, clinics, vets, ATMs,
markets, attractions, places to eat — anything a guest could walk or drive to.
DEFAULT TO ANSWERING. If the search turns up real named places in {city},
list them. Only use {none} for the narrow exceptions below.

GEOGRAPHIC FENCE (critical): every place you mention MUST be in or near {city}.
If a search result is anywhere else, it is WRONG — discard it. Never present a
place from another city or country.

Reply with exactly {none} ONLY when:
- they are asking about room rates, availability, offers or booking (any hotel)
- they are asking about {hotel}'s OWN rooms, restaurants, facilities or policies
  (the hotel's own records cover those, and the web gets them wrong)
- they want a comparison with, or a recommendation of, another hotel
- the search genuinely finds nothing real in {city}

Do NOT return {none} because the hotel's own material mentions something
similar. A guest asking for parks or shops wants INDEPENDENT nearby places.
Verified: passing the hotel's own text in here made the model answer {none} for
"any parks nearby" even though Eco Park — one of India's largest urban parks —
is in this very neighbourhood. So the hotel's text is deliberately NOT supplied.

OUTPUT FORMAT (important): one place per line, starting with "- ", as
NAME | ADDRESS | short description
The ADDRESS matters as much as the name: it is what lets us measure how far the
place is. Give the fullest street address the search shows, ending with the
locality and city. Write "unknown" only if the search truly gives no address.
Example:
- Eco Park | Action Area II, New Town, Kolkata | large urban park with boating
- Annapurna Sweets | AC Block, Action Area 1A, New Town, Kolkata | Bengali sweets

DISTANCE: if the search shows roughly how far a place is from the hotel or its
area, add it as a fourth field: NAME | ADDRESS | DISTANCE | description.
Write it as an approximation ("about 2 km", "roughly 10 min drive"). Leave the
field empty when the search does not show one — never estimate it yourself.
These figures are treated as ROUGH: measured against real routing, web distances
were out by 31% and 64% on two of five places, so they are always presented to
the guest with a hedge, never as an exact measurement.

Do NOT include links: those are built separately."""


def _cache_key(*parts: Any) -> str:
    return "|".join(re.sub(r"\s+", " ", str(p or "").strip().lower()) for p in parts)


class WebLookupService:
    """One bounded web-search call, fenced to a property's neighbourhood."""

    def __init__(self) -> None:
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            from openai import AsyncOpenAI

            key = str(getattr(settings, "openai_api_key", "") or "").strip()
            if not key:
                return None
            self._client = AsyncOpenAI(api_key=key)
        return self._client

    @staticmethod
    def is_enabled() -> bool:
        return bool(getattr(settings, "chat_web_lookup_enabled", False))

    @staticmethod
    def _strip_own_hotel(text: str, hotel_name: str) -> str:
        """Drop lines naming the hotel itself, or its brand.

        Prompting alone did not hold: asked for nearby Bengali restaurants the
        model listed the hotel's own in-house restaurant, described from web
        pages rather than the hotel's records. Filtering here is the only
        reliable stop, and it operates on OUR OWN output, not on guest input.
        """
        # Match the brand as a PHRASE, never as separate words. Splitting
        # "Zone by The Park" into tokens meant every line containing "Park" was
        # deleted — so "Eco Park", the biggest park in this neighbourhood, could
        # never be recommended. Single common words are far too broad to filter.
        name = re.sub(r"\s+", " ", str(hotel_name or "").strip())
        if not name:
            return text
        words = [w for w in re.split(r"[^A-Za-z]+", name) if w]
        if len(words) < 2:
            return text
        # "Zone by The Park" -> allow any whitespace/punctuation between words.
        phrase = r"[^A-Za-z]+".join(re.escape(w) for w in words[:4])
        pattern = re.compile(phrase, re.IGNORECASE)
        kept = [line for line in str(text or "").splitlines() if not pattern.search(line)]
        return "\n".join(kept).strip()

    async def lookup(
        self,
        *,
        question: str,
        hotel_name: str,
        city: str,
        kb_excerpt: str = "",  # accepted for compatibility; intentionally unused
        # 30s. This has been raised twice because the tail kept being cut:
        # 9s lost one hotel on a two-hotel city, then 15s killed a sweet-shop
        # search that was simply taking longer to gather results. Observed times
        # run 4.2-10.9s normally but some queries need well past 15s. A slow
        # answer is far better than a wrong "I don't have that", which is what a
        # cut-off search produces, so the budget is generous and the cache means
        # the cost is paid once per question.
        budget_seconds: float = 30.0,
    ) -> str:
        """Return a short grounded answer about the area, or "" for nothing.

        "" means the answerer should carry on as if this never ran — it must
        never be turned into a guess.
        """
        if not self.is_enabled():
            return ""
        question_clean = re.sub(r"\s+", " ", str(question or "").strip())
        if len(question_clean) < 5:
            return ""

        key = _cache_key("web", hotel_name, city, question_clean)
        cached = _cache.get(key)
        if cached and (time.monotonic() - cached[0]) <= _CACHE_TTL_SECONDS:
            return cached[1]

        client = self._get_client()
        if client is None:
            return ""

        instructions = INSTRUCTIONS.format(
            hotel=hotel_name or "the hotel",
            city=city or "the hotel's city",
            none=NO_ANSWER,
        )

        started = time.monotonic()
        try:
            response = await asyncio.wait_for(
                client.responses.create(
                    model=str(
                        getattr(settings, "chat_web_lookup_model", "") or "gpt-4.1-mini"
                    ),
                    tools=[{"type": "web_search"}],
                    instructions=instructions,
                    input=question_clean[:400],
                    max_output_tokens=700,
                ),
                timeout=budget_seconds,
            )
            text = str(getattr(response, "output_text", "") or "").strip()
        except asyncio.TimeoutError:
            logger.warning("web lookup timed out after %.1fs", budget_seconds)
            return ""
        except Exception as exc:
            logger.warning("web lookup failed: %s: %s", type(exc).__name__, exc)
            return ""

        elapsed = time.monotonic() - started
        if not text or NO_ANSWER in text.upper()[:40]:
            answer = ""
        else:
            answer = self._strip_own_hotel(text, hotel_name)
            # Citation markdown is noise once the answerer rewrites this.
            answer = re.sub(r"\(\[[^\]]*\]\([^)]*\)\)", "", answer)
            answer = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", answer).strip()

        logger.info(
            "[WEB] lookup %.1fs answered=%s q=%r",
            elapsed, bool(answer), question_clean[:60],
        )
        # Only cache a real answer. Caching "" meant one timeout locked that
        # question into "I don't have that" for the next six hours.
        if answer:
            if len(_cache) >= _CACHE_MAX:
                _cache.clear()
            _cache[key] = (time.monotonic(), answer)
        return answer

    @staticmethod
    def extract_places(answer: str) -> list[tuple[str, str, str]]:
        """Pull (name, address) out of the "- Name | Address | desc" lines.

        The address is the important half: geocoding a business NAME fails for
        small local places (OpenStreetMap has never heard of "Moitri Veterinary
        Clinic"), but its street and locality resolve fine — which is what makes
        a distance possible at all.
        """
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        for line in str(answer or "").splitlines():
            line = line.strip()
            if not line.startswith(("-", "*", "•")):
                continue
            line = re.sub(r"^[-*•]+\s*", "", line)
            parts = [re.sub(r"[*_`]", "", p).strip() for p in line.split("|")]
            name = parts[0].strip(" .") if parts else ""
            address = parts[1].strip(" .") if len(parts) > 1 else ""
            distance = parts[2].strip(" .") if len(parts) > 2 else ""
            if address.lower() in {"unknown", "n/a", "na", "-"}:
                address = ""
            if not (3 <= len(name) <= 70) or name.lower() in seen:
                continue
            seen.add(name.lower())
            out.append((name, address, distance))
        return out[:6]

    @staticmethod
    def as_kb_fact(answer: str, hotel_name: str) -> str:
        """Wrap the answer so the answerer treats it as sourced, not as hotel fact."""
        if not answer:
            return ""
        return "\n".join(
            [
                f"[PROPERTY:{hotel_name} | BOOK:Location & Transport | PAGE:web_area_lookup]",
                "=== Nearby area information (from a public web search) ===",
                answer,
                "- These are independent places found online, not run by the hotel. "
                "Mention them as nearby options and make clear they are not hotel "
                "facilities.",
                "- ANY distance above is a ROUGH figure from web listings, not a "
                "measured one. Always hedge it — say 'roughly', 'about' or "
                "'approximately' — and never present it as exact. Do not state a "
                "distance the text above does not give.",
            ]
        )


web_lookup_service = WebLookupService()
