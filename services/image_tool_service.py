from __future__ import annotations

import json
from collections import OrderedDict
import re
from difflib import SequenceMatcher
from typing import Any, Optional

from sqlalchemy import delete, or_, select

from config.settings import settings
from llm.client import llm_client
from models.database import AsyncSessionLocal, ImageAsset, Property
from services.config_service import config_service
from services.db_config_service import db_config_service
from services.s3_storage_service import s3_storage_service


SMART_IMAGE_TOOL_ID = "smart_image_gallery"
SMART_IMAGE_TOOL_HANDLER = "smart_image_selector"


class ImageToolService:
    _TITLE_MATCH_STOPWORDS = {
        "the",
        "and",
        "for",
        "with",
        "from",
        "this",
        "that",
        "your",
        "our",
        "hotel",
        "iconiqa",
        "image",
        "gallery",
        "available",
        "includes",
        "include",
        "features",
        "feature",
        "room",
        "rooms",
        "suite",
        "suites",
        "property",
        "resort",
        "stay",
    }

    @staticmethod
    def _normalize_identifier(value: Any) -> str:
        return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")

    @staticmethod
    def _normalize_text(value: Any) -> str:
        text = re.sub(r"\s+", " ", str(value or "").strip())
        if text.lower() in {"none", "nan", "null", "n/a", "na"}:
            return ""
        return text

    @staticmethod
    def _normalize_url(value: Any) -> str:
        url = str(value or "").strip()
        if not url:
            return ""
        if url.lower().startswith(("http://", "https://")):
            return url
        return ""

    @staticmethod
    def _rank_candidate_assets(
        *,
        candidates: list[dict[str, Any]],
        user_message: str,
        assistant_message: str,
        intent: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        if not isinstance(candidates, list) or not candidates:
            return []
        text = " ".join(
            part for part in [
                str(user_message or "").strip().lower(),
                str(assistant_message or "").strip().lower(),
                str(intent or "").strip().lower(),
            ]
            if part
        )
        tokens = {
            token
            for token in re.findall(r"[a-z0-9]{3,}", text)
            if token not in {"with", "from", "your", "have", "what", "tell", "about", "this", "that", "room", "hotel"}
        }
        if not tokens:
            return candidates[:limit] if limit > 0 else list(candidates)

        scored: list[tuple[int, int, dict[str, Any]]] = []
        for row in candidates:
            if not isinstance(row, dict):
                continue
            haystack = " ".join(
                [
                    str(row.get("title") or "").lower(),
                    str(row.get("description") or "").lower(),
                    str(row.get("category") or "").lower(),
                    " ".join(str(tag or "").lower() for tag in (row.get("tags") or []) if str(tag or "").strip()),
                ]
            )
            matches = sum(1 for token in tokens if token in haystack)
            priority = int(row.get("priority") or 0)
            scored.append((matches, priority, row))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        ranked = [row for _, _, row in scored]
        return ranked[:limit] if limit > 0 else ranked

    @staticmethod
    def _title_match_tokens(value: str) -> list[str]:
        tokens = re.findall(r"[a-z0-9]+", str(value or "").lower())
        return [tok for tok in tokens if len(tok) >= 3 and tok not in ImageToolService._TITLE_MATCH_STOPWORDS]

    @classmethod
    def _tokenize_for_title_match(cls, value: str) -> list[tuple[str, int]]:
        out: list[tuple[str, int]] = []
        for match in re.finditer(r"[a-z0-9]+", str(value or "").lower()):
            token = str(match.group(0) or "").strip()
            if len(token) < 3 or token in cls._TITLE_MATCH_STOPWORDS:
                continue
            out.append((token, int(match.start())))
        return out

    @staticmethod
    def _token_similarity(expected: str, actual: str) -> float:
        if expected == actual:
            return 1.0
        if not expected or not actual:
            return 0.0
        if len(expected) <= 3 or len(actual) <= 3:
            return 0.0
        return float(SequenceMatcher(None, expected, actual).ratio())

    @staticmethod
    def _is_short_strict_token(token: str) -> bool:
        # Very short titles like "spa"/"lux"/"gym" must match exactly.
        # Fuzzy matching here creates false positives ("spa" ~= "space").
        return len(str(token or "").strip()) <= 3

    # Most pictures a single loose word may add to one reply. Pass 1 stays
    # uncapped — a reply that names five photos outright should show five.
    _LOOSE_TIER_MAX = 3

    # Extra words the LOOSE tier ignores, on top of the strict stopwords. These
    # are photo-caption filler that carries no subject: matching on them meant
    # "The pool area includes…" pulled up "Junior Suite living AREA", and every
    # "…view" title fired on any reply containing the word view. Kept separate
    # from _TITLE_MATCH_STOPWORDS so the strict tier's behaviour is untouched.
    _LOOSE_MATCH_STOPWORDS = _TITLE_MATCH_STOPWORDS | {
        "area",
        "areas",
        "view",
        "views",
        "shot",
        "shots",
        "photo",
        "photos",
        "space",
        "spaces",
        # "150 theatre-style SEATING" in a banquet answer pulled up both
        # "Restaurant Seating" and "Hotel Lobby Seating".
        "seating",
        "interior",
        "wide",
    }

    @staticmethod
    def _property_name_tokens(candidates: list[dict[str, Any]]) -> set[str]:
        """Words from the hotel's OWN name, which are never evidence.

        Every reply repeats the property name, so those words match constantly
        and mean nothing about what a picture shows: asked about the pool at
        "Zone Connect by The Park Parra Goa", the reply's own "Connect" pulled
        up the photo titled "Connect room".
        """
        banned: set[str] = set()
        for row in candidates:
            if not isinstance(row, dict):
                continue
            for token in re.findall(r"[a-z0-9]+", str(row.get("property_name") or "").lower()):
                if len(token) >= 3:
                    banned.add(token)
        return banned

    @staticmethod
    def _same_word(expected: str, actual: str) -> bool:
        """"pool" == "poolside" == "pools".

        A guest asking about the pool gets a reply saying "pool", "poolside" or
        "the pools"; the picture is titled "Poolside day view". Prefix agreement
        on 4+ characters covers that whole family with no synonym list to keep
        up to date. Below 4 characters it stays exact, so "spa" != "space".
        """
        if expected == actual:
            return True
        if len(expected) >= 4 and len(actual) >= 4 and (
            expected.startswith(actual) or actual.startswith(expected)
        ):
            return True
        return SequenceMatcher(None, expected, actual).ratio() >= 0.86

    @classmethod
    def _loose_title_match(
        cls,
        *,
        title: str,
        reply_tokens: list[tuple[str, int]],
        banned: set[str],
        category: str,
    ) -> tuple[int, float]:
        """ONE distinctive word of the title is enough — the fallback tier.

        The strict tier needs the WHOLE title present ("poolside day view" as
        three consecutive words), which a real reply essentially never does, so
        a hotel that hands us descriptive titles gets no pictures at all. Here a
        single distinctive word carries it.

        `other` is skipped: banners, corridors, landmarks and driveways are not
        what a guest asked to see, and they are the rows whose titles collide
        with ordinary prose ("90 minute drive" pulling up "DRive way").
        """
        if str(category or "").strip().lower() == "other":
            return -1, 0.0
        for token in re.findall(r"[a-z0-9]+", str(title or "").lower()):
            if len(token) < 3 or token in cls._LOOSE_MATCH_STOPWORDS or token in banned:
                continue
            for actual, pos in reply_tokens:
                if len(actual) < 3 or actual in banned:
                    continue
                # Three-letter words are real subjects here — gym, spa, bar —
                # and excluding them meant "do you have a gym?" found nothing
                # while "Vitalia Gym image" sat in the gallery. They match on
                # equality only, never by prefix, so "spa" cannot take "space".
                if len(token) <= 3 or len(actual) <= 3:
                    if token != actual:
                        continue
                elif not cls._same_word(token, actual):
                    continue
                return int(pos), (1.0 if token == actual else 0.85)
        return -1, 0.0

    @staticmethod
    def _tokens_similar(expected: str, actual: str) -> bool:
        return ImageToolService._token_similarity(expected, actual) >= 0.70

    @classmethod
    def _find_contiguous_token_sequence_pos(
        cls,
        *,
        assistant_tokens: list[tuple[str, int]],
        title_tokens: list[str],
    ) -> tuple[int, float]:
        if not assistant_tokens or not title_tokens:
            return -1, 0.0
        needed = len(title_tokens)
        available = len(assistant_tokens)
        if needed > available:
            return -1, 0.0
        best_pos = -1
        best_score = 0.0
        for start_idx in range(0, available - needed + 1):
            ok = True
            sims: list[float] = []
            for rel_idx, expected in enumerate(title_tokens):
                actual = assistant_tokens[start_idx + rel_idx][0]
                if cls._is_short_strict_token(expected):
                    if expected != actual:
                        ok = False
                        break
                    sim = 1.0
                else:
                    sim = cls._token_similarity(expected, actual)
                sims.append(sim)
                if sim < 0.70:
                    ok = False
                    break
            if ok:
                score = sum(sims) / max(1, len(sims))
                pos = int(assistant_tokens[start_idx][1])
                if score > best_score:
                    best_score = score
                    best_pos = pos
        return best_pos, best_score

    @classmethod
    def _select_candidates_by_title_mentions(
        cls,
        *,
        candidates: list[dict[str, Any]],
        assistant_message: str,
    ) -> list[dict[str, Any]]:
        if not isinstance(candidates, list) or not candidates:
            return []
        assistant_text = re.sub(r"\s+", " ", str(assistant_message or "").strip().lower())
        if not assistant_text:
            return []
        assistant_token_positions = cls._tokenize_for_title_match(assistant_text)
        assistant_tokens = [tok for tok, _ in assistant_token_positions]
        assistant_token_set = set(assistant_tokens)
        if not assistant_token_set:
            return []
        min_conf = float(getattr(settings, "chat_smart_images_min_match_confidence", 0.74) or 0.74)

        banned = cls._property_name_tokens(candidates)

        # (tier, pos, -score, -priority, row). Tier 0 = the reply named the whole
        # title; tier 1 = it named one distinctive word of it. Sorting on tier
        # first keeps every existing exact match ahead of the new loose ones, so
        # this only ever adds pictures to turns that previously had none.
        scored: list[tuple[int, int, int, int, dict[str, Any]]] = []
        for row in candidates:
            if not isinstance(row, dict):
                continue
            title = re.sub(r"\s+", " ", str(row.get("title") or "").strip().lower())
            if not title:
                continue

            title_tokens = cls._title_match_tokens(title)
            if not title_tokens:
                continue
            tier = 0
            overlap = [token for token in title_tokens if token in assistant_token_set]
            overlap_count = len(overlap)

            # Strict whole-phrase first: "spa" won't match "spacious".
            phrase_regex = r"\b" + r"\s+".join(re.escape(tok) for tok in re.findall(r"[a-z0-9]+", title)) + r"\b"
            phrase_match = re.search(phrase_regex, assistant_text)
            if phrase_match:
                pos = int(phrase_match.start())
                exact_hit = True
                match_confidence = 1.0
            else:
                exact_hit = False
                pos, match_confidence = cls._find_contiguous_token_sequence_pos(
                    assistant_tokens=assistant_token_positions,
                    title_tokens=title_tokens,
                )
                if pos < 0 or match_confidence < min_conf:
                    # Nothing named the whole title. Fall back to one
                    # distinctive word of it before giving up entirely.
                    tier = 1
                    pos, match_confidence = cls._loose_title_match(
                        title=title,
                        reply_tokens=assistant_token_positions,
                        banned=banned,
                        category=str(row.get("category") or ""),
                    )
                    if pos < 0:
                        continue

            score = (100 + overlap_count) if exact_hit else overlap_count
            priority = int(row.get("priority") or 0)
            row_with_meta = dict(row)
            row_with_meta["_title_match_confidence"] = float(match_confidence)
            row_with_meta["_title_match_tier"] = int(tier)
            scored.append((tier, pos, -score, -priority, row_with_meta))

        if not scored:
            return []

        scored.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
        ordered: list[dict[str, Any]] = []
        seen_ids: set[int] = set()
        for _, _, _, _, row in scored:
            try:
                image_id = int(row.get("id") or 0)
            except (TypeError, ValueError):
                image_id = 0
            if image_id > 0 and image_id in seen_ids:
                continue
            if image_id > 0:
                seen_ids.add(image_id)
            ordered.append(row)
        return ordered

    @staticmethod
    def _build_image_cards(rows: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
        selected: list[dict[str, Any]] = []
        for match in rows[: max(0, int(limit or 0))]:
            description_text = str(match.get("description") or "").strip()
            category_text = str(match.get("category") or "").strip()
            tags_list = match.get("tags") if isinstance(match.get("tags"), list) else []
            if not description_text:
                if category_text:
                    description_text = f"{category_text.title()} highlight from hotel gallery."
                elif tags_list:
                    description_text = "Related to: " + ", ".join(
                        str(tag).strip() for tag in tags_list[:3] if str(tag).strip()
                    )
                else:
                    description_text = f"{str(match.get('title') or '').strip()} image from hotel gallery."
            property_name = str(match.get("property_name") or "").strip()
            property_id = match.get("property_id")
            selected.append(
                {
                    "id": int(match.get("id") or 0),
                    "title": str(match.get("title") or "").strip(),
                    "description": description_text,
                    "url": s3_storage_service.resolve_display_url(match.get("image_url")),
                    "category": category_text,
                    "tags": tags_list,
                    "property_id": int(property_id) if property_id is not None else None,
                    "property_name": property_name or None,
                }
            )
        return selected

    @staticmethod
    def _derive_tags_from_title(title: str) -> list[str]:
        lowered = str(title or "").strip().lower()
        if not lowered:
            return []
        tokens = re.findall(r"[a-z0-9]+", lowered)
        stop = {
            "the",
            "and",
            "for",
            "with",
            "from",
            "this",
            "that",
            "your",
            "room",
            "suite",
            "hotel",
            "iconiqa",
        }
        tags = [token for token in tokens if token not in stop and len(token) > 2]
        deduped: list[str] = []
        seen: set[str] = set()
        for tag in tags:
            if tag in seen:
                continue
            seen.add(tag)
            deduped.append(tag)
            if len(deduped) >= 12:
                break
        return deduped

    @staticmethod
    def _title_keywords(title: str) -> list[str]:
        base = str(title or "").strip().lower()
        if not base:
            return []
        base = re.sub(r"iconiqa", " ", base)
        tokens = [tok for tok in re.findall(r"[a-z0-9]+", base) if len(tok) > 2]
        keywords: list[str] = []
        for tok in tokens:
            if tok not in keywords:
                keywords.append(tok)

        joined = "_".join(tokens)
        if joined and joined not in keywords:
            keywords.append(joined)
        if tokens:
            spaced = " ".join(tokens)
            if spaced not in keywords:
                keywords.append(spaced)

        mapping = {
            "lux": ["lux_suite", "lux suite"],
            "prestige": ["prestige_suite", "prestige suite"],
            "reserve": ["reserve_suite", "reserve suite"],
            "premier": ["premier_king", "premier_twin", "premier king", "premier twin"],
            "pool": ["swim club", "poolside", "bombay swim club"],
            "swim": ["swim club", "poolside", "bombay swim club"],
            "spa": ["spa", "wellness"],
            "suite": ["suite", "suites"],
        }
        expanded: list[str] = []
        for key in list(keywords):
            if key in mapping:
                expanded.extend(mapping[key])
        for term in expanded:
            if term not in keywords:
                keywords.append(term)
        return keywords[:14]

    @classmethod
    def _kb_snippet_map_for_images(
        cls,
        *,
        kb_text: str,
        selected_images: list[dict[str, Any]],
        max_chars_per_image: int = 950,
    ) -> dict[int, str]:
        normalized = str(kb_text or "").strip()
        if not normalized:
            return {}
        chunks = [re.sub(r"\s+", " ", part).strip() for part in re.split(r"\n\s*\n+", normalized) if str(part).strip()]
        if not chunks:
            return {}
        lower_chunks = [chunk.lower() for chunk in chunks]

        snippet_by_id: dict[int, str] = {}
        for image in selected_images:
            try:
                image_id = int(image.get("id") or 0)
            except (TypeError, ValueError):
                continue
            if image_id <= 0:
                continue
            keywords = cls._title_keywords(str(image.get("title") or ""))
            if not keywords:
                continue
            scored: list[tuple[int, int]] = []
            for idx, chunk_lower in enumerate(lower_chunks):
                score = 0
                for kw in keywords:
                    if kw and kw in chunk_lower:
                        score += 1
                if score > 0:
                    scored.append((score, idx))
            if not scored:
                continue
            scored.sort(key=lambda row: (-row[0], row[1]))
            picked_parts: list[str] = []
            consumed = 0
            for _, idx in scored[:3]:
                text = chunks[idx]
                if not text:
                    continue
                remaining = max_chars_per_image - consumed
                if remaining <= 0:
                    break
                segment = text[:remaining].strip()
                if not segment:
                    continue
                picked_parts.append(segment)
                consumed += len(segment) + 1
            snippet = "\n".join(picked_parts).strip()
            if snippet:
                snippet_by_id[image_id] = snippet
        return snippet_by_id

    @staticmethod
    def _extract_tool_enabled(tools: Any) -> bool:
        if not isinstance(tools, list):
            return False
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            tool_id = ImageToolService._normalize_identifier(tool.get("id"))
            handler = ImageToolService._normalize_identifier(tool.get("handler"))
            if tool_id == SMART_IMAGE_TOOL_ID or handler == SMART_IMAGE_TOOL_HANDLER:
                return bool(tool.get("enabled", True))
        return False

    # In-process cache for is_enabled_for_hotel. Was hitting MySQL every
    # turn — multi-second VPN cost just to read a tool flag that almost
    # never changes.
    _enabled_cache: "dict[str, tuple[float, bool]]" = {}
    _ENABLED_CACHE_TTL_SECONDS = 600.0

    @classmethod
    def invalidate_enabled_cache(cls, hotel_code: str | None = None) -> None:
        if hotel_code:
            cls._enabled_cache.pop(str(hotel_code).strip().upper(), None)
        else:
            cls._enabled_cache.clear()

    async def is_enabled_for_hotel(self, hotel_code: str) -> bool:
        import time as _time
        cache_key = str(hotel_code or "").strip().upper()
        cached = self._enabled_cache.get(cache_key)
        if cached and (_time.monotonic() - cached[0]) <= self._ENABLED_CACHE_TTL_SECONDS:
            return cached[1]

        with db_config_service.scoped_hotel(hotel_code):
            tools = await db_config_service.get_tools()
        if self._extract_tool_enabled(tools):
            self._enabled_cache[cache_key] = (_time.monotonic(), True)
            return True

        # Backfill: if the admin uploaded images via Content Manager but the
        # smart_image_gallery tool was never registered, treat the presence
        # of active images as opt-in.
        try:
            with db_config_service.scoped_hotel(hotel_code):
                hotel_id = await db_config_service.get_current_hotel_id()
            if not hotel_id:
                self._enabled_cache[cache_key] = (_time.monotonic(), False)
                return False
            async with AsyncSessionLocal() as session:
                row = (
                    await session.execute(
                        select(ImageAsset)
                        .where(
                            ImageAsset.hotel_id == hotel_id,
                            ImageAsset.is_active == True,  # noqa: E712
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
            result = row is not None
            self._enabled_cache[cache_key] = (_time.monotonic(), result)
            return result
        except Exception:
            return False

    async def ensure_tool_registered(self, hotel_code: str, *, enabled: bool = True) -> bool:
        payload = {
            "id": SMART_IMAGE_TOOL_ID,
            "name": "Smart Image Gallery",
            "description": "LLM selects relevant hotel images to enrich assistant responses.",
            "type": "workflow",
            "handler": SMART_IMAGE_TOOL_HANDLER,
            "channels": ["web_widget", "whatsapp"],
            "enabled": bool(enabled),
            "requires_confirmation": False,
        }
        with db_config_service.scoped_hotel(hotel_code):
            return await db_config_service.add_tool(payload)

    async def upsert_assets(
        self,
        *,
        hotel_code: str,
        rows: list[dict[str, Any]],
        source_label: str = "",
        replace_existing: bool = True,
    ) -> dict[str, int]:
        normalized_rows: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        for row in rows:
            title = self._normalize_text(row.get("title"))
            image_url = self._normalize_url(row.get("image_url"))
            if not title or not image_url:
                continue
            if image_url.lower() in seen_urls:
                continue
            seen_urls.add(image_url.lower())
            description = self._normalize_text(row.get("description"))
            category = self._normalize_text(row.get("category"))
            raw_tags = row.get("tags")
            tags: list[str] = []
            if isinstance(raw_tags, list):
                tags = [self._normalize_identifier(tag) for tag in raw_tags if self._normalize_identifier(tag)]
            if not tags:
                tags = self._derive_tags_from_title(f"{title} {description}".strip())
            priority = row.get("priority")
            try:
                priority_value = int(priority) if priority not in (None, "") else 0
            except (TypeError, ValueError):
                priority_value = 0
            normalized_rows.append(
                {
                    "title": title,
                    "description": description or None,
                    "image_url": image_url,
                    "category": category or None,
                    "tags": tags,
                    "priority": priority_value,
                }
            )

        if not normalized_rows:
            return {"inserted": 0, "updated": 0, "skipped": len(rows)}

        with db_config_service.scoped_hotel(hotel_code):
            hotel_id = await db_config_service.get_current_hotel_id()

        inserted = 0
        updated = 0
        async with AsyncSessionLocal() as session:
            if replace_existing:
                await session.execute(
                    delete(ImageAsset).where(ImageAsset.hotel_id == hotel_id)
                )
                for item in normalized_rows:
                    row = ImageAsset(
                        hotel_id=hotel_id,
                        title=item["title"],
                        description=item["description"],
                        image_url=item["image_url"],
                        category=item["category"],
                        tags=item["tags"],
                        source_label=source_label or None,
                        is_active=True,
                        priority=item["priority"],
                    )
                    session.add(row)
                    inserted += 1
                await session.commit()
                return {"inserted": inserted, "updated": 0, "skipped": max(0, len(rows) - len(normalized_rows))}

            for item in normalized_rows:
                existing = await session.execute(
                    select(ImageAsset).where(
                        ImageAsset.hotel_id == hotel_id,
                        ImageAsset.image_url == item["image_url"],
                    )
                )
                row = existing.scalar_one_or_none()
                if row is None:
                    row = ImageAsset(
                        hotel_id=hotel_id,
                        title=item["title"],
                        description=item["description"],
                        image_url=item["image_url"],
                        category=item["category"],
                        tags=item["tags"],
                        source_label=source_label or None,
                        is_active=True,
                        priority=item["priority"],
                    )
                    session.add(row)
                    inserted += 1
                else:
                    row.title = item["title"]
                    row.description = item["description"]
                    row.category = item["category"]
                    row.tags = item["tags"]
                    row.source_label = source_label or row.source_label
                    row.is_active = True
                    row.priority = item["priority"]
                    updated += 1
            await session.commit()

        return {"inserted": inserted, "updated": updated, "skipped": max(0, len(rows) - len(normalized_rows))}

    # In-process cache for image candidates per (hotel, property-scope).
    # Without this, every chat turn was paying 2 VPN-MySQL roundtrips here
    # (~6s) just to list the same image rows. TTL = 10 minutes; admin
    # upload/delete paths could invalidate, but stale-by-10-min is cheap
    # compared to the latency hit of every-turn lookup.
    #
    # Cache key shape: "<HOTEL_CODE>::<scope>" where scope is one of:
    #   "all"        → no property filter (caller didn't supply one)
    #   "p:<int>"    → scoped to that specific sub-property
    # Invalidating by hotel_code drops every scope variant for that hotel.
    _candidate_cache: "dict[str, tuple[float, list[dict[str, Any]]]]" = {}
    _CANDIDATE_CACHE_TTL_SECONDS = 600.0

    # Cache for resolving (hotel_code, property_hint) → new_bot_properties.id.
    # Keyed by (HOTEL_CODE_UPPER, normalized_hint). 5-min TTL — long enough to
    # save the DB hit on hot paths, short enough that admin renames flow
    # through without an explicit invalidation.
    _property_id_resolver_cache: "dict[tuple[str, str], tuple[float, Optional[int]]]" = {}
    _PROPERTY_ID_RESOLVER_TTL_SECONDS = 300.0

    @classmethod
    def invalidate_candidate_cache(cls, hotel_code: str | None = None) -> None:
        if hotel_code:
            prefix = str(hotel_code).strip().upper() + "::"
            for key in list(cls._candidate_cache.keys()):
                if key.startswith(prefix):
                    cls._candidate_cache.pop(key, None)
        else:
            cls._candidate_cache.clear()

    @staticmethod
    def _scope_cache_suffix(active_property_ids) -> str:
        """Build a stable cache-key suffix from the active-property scope.

        Accepts either a single int (legacy single-property scope) or a list
        of ints (multi-property scope used when a reply summarizes several
        properties). None / empty / falsy → "all" (no scope).
        """
        if active_property_ids is None:
            return "all"
        if isinstance(active_property_ids, (list, tuple, set)):
            try:
                sorted_ids = sorted({int(pid) for pid in active_property_ids if pid is not None})
            except (TypeError, ValueError):
                return "all"
            if not sorted_ids:
                return "all"
            return "p:" + ",".join(str(pid) for pid in sorted_ids)
        try:
            return f"p:{int(active_property_ids)}"
        except (TypeError, ValueError):
            return "all"

    @classmethod
    def _normalize_property_hint(cls, value: Any) -> str:
        text = re.sub(r"\s+", " ", str(value or "").strip()).lower()
        return text

    async def resolve_active_property_id(
        self,
        *,
        hotel_code: str,
        property_hint: Any,
    ) -> Optional[int]:
        """Map a free-form property name/slug/id (as known to the orchestrator)
        to a row in new_bot_properties. Returns None when there's no match —
        callers treat None as "no property filter" so chat keeps showing all
        images (matches single-property hotels and pre-feature deployments).
        """
        normalized = self._normalize_property_hint(property_hint)
        if not normalized:
            return None
        cache_key = (str(hotel_code or "").strip().upper(), normalized)
        cached = self._property_id_resolver_cache.get(cache_key)
        import time as _time
        if cached and (_time.monotonic() - cached[0]) <= self._PROPERTY_ID_RESOLVER_TTL_SECONDS:
            return cached[1]

        with db_config_service.scoped_hotel(hotel_code):
            hotel_id = await db_config_service.get_current_hotel_id()
        if not hotel_id:
            self._property_id_resolver_cache[cache_key] = (_time.monotonic(), None)
            return None

        async with AsyncSessionLocal() as session:
            rows = (
                await session.execute(
                    select(Property).where(
                        Property.hotel_id == hotel_id,
                        Property.is_active == True,  # noqa: E712
                    )
                )
            ).scalars().all()

        # Build a label → property_id map (slug, name, aliases). Last match
        # wins on ambiguity; that's rare, and resolve_property_scope_ids does
        # the heavy lifting in the KB layer anyway.
        slug_norm = re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")
        match_id: Optional[int] = None
        for row in rows:
            candidates = [
                str(row.property_slug or "").strip().lower(),
                str(row.name or "").strip().lower(),
            ]
            if isinstance(row.aliases, list):
                candidates.extend(str(alias or "").strip().lower() for alias in row.aliases)
            candidates = [c for c in candidates if c]
            slug_candidate = re.sub(r"[^a-z0-9]+", "_", str(row.property_slug or "").lower()).strip("_")
            if normalized in candidates or (slug_candidate and slug_norm == slug_candidate):
                match_id = int(row.id)
                break
            # Fuzzy: exact substring on name, both directions, to absorb
            # "Lotus Eco Beach Resort Dapoli" vs KB scope name "lotus_eco_beach_resort".
            name_lower = str(row.name or "").strip().lower()
            if name_lower and (name_lower in normalized or normalized in name_lower):
                match_id = int(row.id)
                break

        self._property_id_resolver_cache[cache_key] = (_time.monotonic(), match_id)
        return match_id

    async def list_candidate_assets(
        self,
        *,
        hotel_code: str,
        limit: int = 0,
        active_property_id: Optional[int] = None,
        active_property_ids: Optional[list[int]] = None,
    ) -> list[dict[str, Any]]:
        """List image candidates for a hotel, optionally scoped to one or more sub-properties.

        Accepts either a single `active_property_id` (legacy) or a list of
        `active_property_ids`. When any scope is provided, the result includes
        images for those properties PLUS shared images with property_id IS NULL.
        When both are None, returns every image (single-property / pre-feature
        behavior).

        Multi-property scope is critical for replies that summarize several
        properties at once — without it the image picker pulls from every
        property in the hotel and leaks unrelated images (e.g. a pool from
        Porvorim into a reply discussing only Pune properties).
        """
        scope_ids: list[int] = []
        if active_property_ids:
            for pid in active_property_ids:
                if pid is None:
                    continue
                try:
                    scope_ids.append(int(pid))
                except (TypeError, ValueError):
                    continue
        if active_property_id is not None:
            try:
                _single = int(active_property_id)
                if _single not in scope_ids:
                    scope_ids.append(_single)
            except (TypeError, ValueError):
                pass

        import time as _time
        scope_suffix = self._scope_cache_suffix(scope_ids or None)
        cache_key = f"{str(hotel_code or '').strip().upper()}::{scope_suffix}"
        cached = self._candidate_cache.get(cache_key)
        if cached and (_time.monotonic() - cached[0]) <= self._CANDIDATE_CACHE_TTL_SECONDS:
            assets = [dict(item) for item in cached[1]]
            parsed_limit = int(limit or 0)
            return assets[:parsed_limit] if parsed_limit > 0 else assets

        with db_config_service.scoped_hotel(hotel_code):
            hotel_id = await db_config_service.get_current_hotel_id()
        async with AsyncSessionLocal() as session:
            # LEFT JOIN Property so each asset carries its sub-property name
            # in one trip — the chat carousel renders this below the title to
            # disambiguate when two properties are compared side-by-side.
            query = (
                select(ImageAsset, Property.name)
                .outerjoin(Property, ImageAsset.property_id == Property.id)
                .where(
                    ImageAsset.hotel_id == hotel_id,
                    ImageAsset.is_active == True,  # noqa: E712
                )
                .order_by(ImageAsset.priority.desc(), ImageAsset.id.asc())
            )
            if scope_ids:
                query = query.where(
                    or_(
                        ImageAsset.property_id.in_(scope_ids),
                        ImageAsset.property_id.is_(None),
                    )
                )
            result = await session.execute(query)
            rows = result.all()
        assets: list[dict[str, Any]] = []
        for row, property_name in rows:
            assets.append(
                {
                    "id": int(row.id),
                    "property_id": int(row.property_id) if row.property_id is not None else None,
                    "property_name": str(property_name or "").strip() or None,
                    "title": str(row.title or "").strip(),
                    "description": str(row.description or "").strip(),
                    "image_url": str(row.image_url or "").strip(),
                    "category": str(row.category or "").strip(),
                    "tags": row.tags if isinstance(row.tags, list) else [],
                    "priority": int(row.priority or 0),
                }
            )
        self._candidate_cache[cache_key] = (_time.monotonic(), [dict(a) for a in assets])
        parsed_limit = int(limit or 0)
        return assets[:parsed_limit] if parsed_limit > 0 else assets

    # Order a property's gallery is shown in when we are showing it because the
    # guest just picked that property — not because the reply happened to name a
    # picture. Facade first (the establishing shot), then rooms, then the rest.
    GALLERY_CATEGORY_ORDER = (
        "facade",
        "room",
        "dining",
        "pool",
        "spa",
        "banquet",
        "lobby",
        "fitness",
        "kids",
    )

    # Which image categories a KB book is about. The router already decided
    # which books to open for the turn, so this translates that decision into
    # pictures — no extra LLM call, no extra tokens, no extra latency.
    # Books absent from this map (policies, contact, offers, pet policy…) are
    # deliberately image-less: nothing visual to show, so we show nothing.
    BOOK_IMAGE_CATEGORIES: dict[str, tuple[str, ...]] = {
        "roomssuites": ("room",),
        "roomoccupancyextrabed": ("room",),
        "diningrestaurants": ("dining",),
        "spawellness": ("spa", "pool"),
        "meetingsevents": ("banquet",),
        "amenitiesfacilities": ("pool", "fitness", "lobby"),
        "hoteloverview": ("facade", "room"),
        "childpolicy": ("kids",),
        # "How far is the airport?" is a Location & Transport question. With no
        # entry here it fell through to whatever the previous turn had loaded —
        # observed on the live bot answering an airport question with a picture
        # of the swimming pool. The building is the right thing to show when the
        # guest is asking where the hotel is.
        "locationtransport": ("facade",),
        # An offers answer is about the stay itself, so show the hotel and a room
        # rather than nothing at all.
        "specialofferspackages": ("facade", "room"),
    }

    # session_id -> image ids already shown, so the same photo doesn't reappear
    # on every turn. Best-effort and in-process: losing it on restart just means
    # a repeat, never a failure, so it isn't worth a DB round-trip.
    _shown_images: "OrderedDict[str, list[int]]" = OrderedDict()
    _SHOWN_MAX_SESSIONS = 500
    _SHOWN_MAX_PER_SESSION = 60

    # session_id -> KB book titles opened on the most recent answered turn.
    # Recorded where the answer is produced and read where images are attached,
    # instead of threading the value through several layers of decision/response
    # metadata (which is filtered along the way, so it never arrived).
    _last_books: "OrderedDict[str, list[str]]" = OrderedDict()

    @classmethod
    def note_books(cls, session_id: str, book_labels: list[str]) -> None:
        key = str(session_id or "").strip()
        if not key:
            return
        cls._last_books[key] = [str(b).strip() for b in (book_labels or []) if str(b).strip()]
        cls._last_books.move_to_end(key)
        while len(cls._last_books) > cls._SHOWN_MAX_SESSIONS:
            cls._last_books.popitem(last=False)

    @classmethod
    def books_for(cls, session_id: str, *, consume: bool = False) -> list[str]:
        """Book titles the BOOK ANSWERER opened, for this turn only.

        `consume=True` clears them on read. Only the book answerer records here,
        so on a service-agent turn the entry left over from an earlier turn is
        stale — and it showed: after a run of location questions, "what rooms do
        you have?" was answered by the service agent and still picked up
        `Location & Transport`, so the guest got a photo of the building instead
        of a room. Consuming the labels means a lane that did not set them gets
        nothing and falls through to its own title-matching selector, which is
        how the service lane is meant to choose pictures anyway.
        """
        key = str(session_id or "").strip()
        labels = list(cls._last_books.get(key, []))
        if consume:
            cls._last_books.pop(key, None)
        return labels

    @classmethod
    def shown_for(cls, session_id: str) -> list[int]:
        return list(cls._shown_images.get(str(session_id or "").strip(), []))

    @classmethod
    def note_shown(cls, session_id: str, image_ids: list[int]) -> None:
        key = str(session_id or "").strip()
        if not key or not image_ids:
            return
        current = cls._shown_images.get(key, [])
        for image_id in image_ids:
            try:
                value = int(image_id)
            except (TypeError, ValueError):
                continue
            if value and value not in current:
                current.append(value)
        cls._shown_images[key] = current[-cls._SHOWN_MAX_PER_SESSION :]
        cls._shown_images.move_to_end(key)
        while len(cls._shown_images) > cls._SHOWN_MAX_SESSIONS:
            cls._shown_images.popitem(last=False)

    @classmethod
    def reset_shown(cls, session_id: str) -> None:
        cls._shown_images.pop(str(session_id or "").strip(), None)

    # Books that answer one narrow topic outrank broad ones. The router usually
    # opens "Hotel Overview" alongside the specific book, and overview leads with
    # a facade shot — so a question about ROOMS was showing the building. Sorting
    # by specificity puts the room photo first and keeps overview as a filler.
    BOOK_SPECIFICITY: dict[str, int] = {
        "roomssuites": 0,
        "diningrestaurants": 0,
        "spawellness": 0,
        "meetingsevents": 0,
        "childpolicy": 0,
        "roomoccupancyextrabed": 1,
        # Specific topic books, like the 0s above but one step back so a turn
        # that also opens Rooms & Suites or Dining still leads with those.
        #
        # These sat at 3 first, BELOW amenitiesfacilities, and it showed: asked
        # "how far is the airport?", the router opened location_transport AND
        # amenities_facilities, amenities won on rank, and the guest got a photo
        # of the lobby (this property has no pool image, so pool/fitness/lobby
        # fell through to lobby). Location is what the question was about.
        "locationtransport": 1,
        "specialofferspackages": 1,
        # Broad "about the whole hotel" books rank last.
        "amenitiesfacilities": 2,
        "hoteloverview": 3,
    }

    @classmethod
    def categories_for_books(cls, book_labels: list[str]) -> list[str]:
        """Ordered, de-duplicated image categories for the books opened.

        Ordering matters: the first category supplies the first picture the
        guest sees, so it must match what they actually asked about.
        """
        keys = [
            re.sub(r"[^a-z0-9]+", "", str(label or "").strip().lower())
            for label in (book_labels or [])
        ]
        keys = [k for k in keys if k in cls.BOOK_IMAGE_CATEGORIES]
        if not keys:
            return []
        keys.sort(key=lambda k: cls.BOOK_SPECIFICITY.get(k, 9))

        # When a narrow book is open, use ONLY its categories. The router often
        # opens several books at once, and blending them in meant a question
        # about ROOMS could lead with a banquet hall, and one about DINING with
        # the building exterior. The most specific book is what the guest asked
        # about; the rest are background for the text, not for the pictures.
        # Exactly ONE book drives the pictures. Two equally-specific books (Rooms
        # & Suites plus Meetings & Events, both opened for one question) still
        # produced a banquet hall in answer to "what rooms do you have". The sort
        # is stable, so this keeps the first book as it appeared in the KB
        # context — the one the router led with.
        primary = keys[0]

        out: list[str] = []
        for category in cls.BOOK_IMAGE_CATEGORIES.get(primary, ()):
            if category not in out:
                out.append(category)
        return out

    async def select_for_books(
        self,
        *,
        hotel_code: str,
        property_ids: list[int],
        book_labels: list[str],
        limit: int = 4,
        exclude_ids: Optional[list[int]] = None,
    ) -> list[dict[str, Any]]:
        """Images matching the topic of the books the router opened this turn.

        Replaces title-matching for mid-conversation turns: that only fired when
        an image's title appeared word-for-word in the reply, so in practice
        replies about rooms/dining/pool showed no pictures at all.

        `exclude_ids` carries the images already shown in this conversation, so
        the guest doesn't get the same photo on every turn.
        """
        categories = self.categories_for_books(book_labels)
        if not categories:
            return []

        wanted: list[int] = []
        for pid in property_ids or []:
            try:
                wanted.append(int(pid))
            except (TypeError, ValueError):
                continue
        if not wanted:
            return []

        candidates = await self.list_candidate_assets(
            hotel_code=hotel_code, limit=0, active_property_ids=wanted
        )
        if not candidates:
            return []

        already = {int(x) for x in (exclude_ids or []) if str(x).strip().isdigit()}
        rank = {name: index for index, name in enumerate(categories)}
        in_scope = [
            row
            for row in candidates
            if str(row.get("category") or "").strip().lower() in rank
        ]
        rows = [row for row in in_scope if int(row.get("id") or 0) not in already]
        if not rows:
            # Every picture for this topic has already been shown. Repeating one
            # beats showing none: measured on a Kolkata conversation, the welcome
            # and two nearby-place questions consumed both facade photos, so
            # "how far is the airport?" — the question most in need of a picture
            # of the building — got nothing at all. A property will never have
            # enough photos to keep a long chat unique, so exhaustion is the
            # normal end state, not an edge case.
            rows = in_scope
        if not rows:
            return []
        rows.sort(
            key=lambda r: (
                rank.get(str(r.get("category") or "").strip().lower(), 99),
                -int(r.get("priority") or 0),
                int(r.get("id") or 0),
            )
        )
        return self._build_image_cards(rows, limit=max(1, int(limit)))

    async def select_by_title_mentions(
        self,
        *,
        hotel_code: str,
        property_ids: list[int],
        text: str,
        limit: int = 4,
        exclude_ids: Optional[list[int]] = None,
    ) -> list[dict[str, Any]]:
        """Images whose title the reply actually names.

        For the SERVICE lane. The service agent never records which KB books it
        opened, so the book→category path has nothing to work from and room
        answers came back with no pictures at all despite the property having
        eight room photos. This matches on what the agent wrote instead: it lists
        "Connect Room", "Junior Suite", "Cafe C" by name, and the gallery titles
        are those same names.

        Deterministic on purpose — no LLM, so it can run inline on the critical
        path without costing the turn any time. The LLM title-matcher stays on
        the deferred path where it cannot reach the guest anyway.

        Longest title first, so "Junior Suite" wins over a bare "Suite" when the
        reply mentions both.
        """
        wanted: list[int] = []
        for pid in property_ids or []:
            try:
                wanted.append(int(pid))
            except (TypeError, ValueError):
                continue
        body = str(text or "").strip().lower()
        if not wanted or len(body) < 3:
            return []

        candidates = await self.list_candidate_assets(
            hotel_code=hotel_code, limit=0, active_property_ids=wanted
        )
        if not candidates:
            return []

        already = {int(x) for x in (exclude_ids or []) if str(x).strip().isdigit()}
        banned = self._property_name_tokens(candidates)
        body_tokens = self._tokenize_for_title_match(body)
        ordered = sorted(
            candidates,
            key=lambda r: (-len(str(r.get("title") or "")), int(r.get("id") or 0)),
        )
        hits: list[dict[str, Any]] = []
        seen_titles: set[str] = set()

        def _take(row: dict[str, Any], needle: str) -> None:
            if int(row.get("id") or 0) in already and needle in seen_titles:
                return
            seen_titles.add(needle)
            hits.append(row)

        # PASS 1 — the reply names the WHOLE title. Unchanged, and taken first
        # so an exact mention always outranks a one-word match.
        for row in ordered:
            title = str(row.get("title") or "").strip()
            if len(title) < 3:
                continue
            needle = title.lower()
            # Word-boundary match so "Pool" does not fire on "Liverpool" and a
            # partial word never counts.
            if not re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", body):
                continue
            _take(row, needle)

        # PASS 2 — one distinctive word of the title is enough.
        #
        # Pass 1 needs the title verbatim, which descriptive hotel-supplied
        # titles never are: asked "do you have a pool?" the reply says "pool"
        # and "poolside", while the photos are called "Poolside day view" and
        # "Poolside night view", so the guest got no picture at all. We cannot
        # rename them — the titles come from the hotel.
        #
        # Ordered by WHERE the match falls in the reply, not by title length:
        # one loose word is weak evidence, so the thing the reply mentions
        # first is the likeliest subject. Capped, because a single word is weak
        # enough that a long answer can brush past a dozen titles — a Jaipur
        # dining reply matched 17 photos, including a guest room and the pool.
        loose: list[tuple[int, int, dict[str, Any]]] = []
        for row in ordered:
            title = str(row.get("title") or "").strip()
            if len(title) < 3:
                continue
            needle = title.lower()
            if needle in seen_titles:
                continue
            pos, _conf = self._loose_title_match(
                title=needle,
                reply_tokens=body_tokens,
                banned=banned,
                category=str(row.get("category") or ""),
            )
            if pos < 0:
                continue
            loose.append((pos, -int(row.get("priority") or 0), row))
        loose.sort(key=lambda item: (item[0], item[1], int(item[2].get("id") or 0)))
        taken = 0
        for _pos, _prio, row in loose:
            if taken >= self._LOOSE_TIER_MAX:
                break
            needle = str(row.get("title") or "").strip().lower()
            # Several rows can share a title (a property holds four "Rooftop
            # Pool" shots). One weak word must not spend the whole budget on
            # copies of the same picture.
            if needle in seen_titles:
                continue
            _take(row, needle)
            taken += 1

        if not hits:
            return []
        # 0 (or less) means no cap.
        capped = hits[: int(limit)] if int(limit) > 0 else hits
        return self._build_image_cards(capped, limit=len(capped))

    async def select_gallery_for_properties(
        self,
        *,
        hotel_code: str,
        property_ids: list[int],
        per_property_limit: int = 4,
        category_quota: tuple[tuple[str, int], ...] | None = None,
    ) -> list[dict[str, Any]]:
        """Pick a showcase set of images for one or more properties.

        Unlike `llm_select_images`, this does NOT require the assistant's reply
        to mention an image title. It is used when the guest has just chosen a
        property (or a city), where showing the place is the point — the reply
        text is irrelevant to which pictures are right.

        Ordering per property: facade, then rooms, then other facilities, each
        group by `priority` then id. Every property in `property_ids` gets its
        own slice, so a two-hotel city shows both rather than whichever happened
        to rank highest overall.

        Returns [] when the gallery has no usable categories yet (i.e. before
        tools/classify_zone_images.py has labelled the rows), so the caller
        silently falls back to attaching nothing rather than showing junk.
        """
        wanted_ids = []
        for pid in property_ids or []:
            try:
                wanted_ids.append(int(pid))
            except (TypeError, ValueError):
                continue
        if not wanted_ids:
            return []

        candidates = await self.list_candidate_assets(
            hotel_code=hotel_code,
            limit=0,
            active_property_ids=wanted_ids,
        )
        if not candidates:
            return []

        rank = {name: index for index, name in enumerate(self.GALLERY_CATEGORY_ORDER)}
        selected: list[dict[str, Any]] = []
        for pid in wanted_ids:
            rows = [
                row
                for row in candidates
                if row.get("property_id") == pid
                and str(row.get("category") or "").strip().lower() in rank
            ]
            if not rows:
                continue
            rows.sort(
                key=lambda r: (
                    rank.get(str(r.get("category") or "").strip().lower(), 99),
                    -int(r.get("priority") or 0),
                    int(r.get("id") or 0),
                )
            )
            # 0 (or less) means no cap — show every photo that qualifies.
            cap = int(per_property_limit) if int(per_property_limit) > 0 else len(rows)
            picked: list[dict[str, Any]] = []

            if category_quota:
                # Caller asked for a specific mix — the welcome message wants the
                # building and its rooms, not one-of-everything. Take up to N per
                # named category, in the order given, then backfill from whatever
                # is left in those same categories so a property with no facade
                # still fills its slots with rooms instead of showing fewer
                # pictures.
                by_category: dict[str, list[dict[str, Any]]] = {}
                for row in rows:
                    by_category.setdefault(
                        str(row.get("category") or "").strip().lower(), []
                    ).append(row)
                for wanted, want_n in category_quota:
                    _bucket = by_category.get(str(wanted).strip().lower(), [])
                    # want_n of 0 means every photo in that category.
                    _slice = _bucket if int(want_n) <= 0 else _bucket[: int(want_n)]
                    for row in _slice:
                        if len(picked) >= cap:
                            break
                        picked.append(row)
                if len(picked) < cap:
                    chosen_ids = {id(r) for r in picked}
                    for wanted, _n in category_quota:
                        for row in by_category.get(str(wanted).strip().lower(), []):
                            if len(picked) >= cap:
                                break
                            if id(row) not in chosen_ids:
                                picked.append(row)
                                chosen_ids.add(id(row))
            else:
                # One image per category first, so the guest sees variety (exterior,
                # room, restaurant) instead of four near-identical room shots.
                seen_categories: set[str] = set()
                for row in rows:
                    category = str(row.get("category") or "").strip().lower()
                    if category in seen_categories:
                        continue
                    seen_categories.add(category)
                    picked.append(row)
                    if len(picked) >= cap:
                        break
            selected.extend(picked)

        if not selected:
            return []
        return self._build_image_cards(selected, limit=len(selected))

    async def select_top_ranked_fallback(
        self,
        *,
        hotel_code: str,
        user_message: str,
        assistant_message: str,
        intent: str = "",
        max_images: int = 3,
        active_property_id: Optional[int] = None,
        active_property_ids: Optional[list[int]] = None,
    ) -> dict[str, Any]:
        # Deterministic fallback for when the LLM image selector is too slow or
        # errors out: rank candidates by keyword overlap (the same pre-ranker the
        # LLM path uses) and return the top-N. No LLM, no KB enrichment — uses
        # the cached candidate list, so it completes in ~50 ms.
        try:
            candidates = await self.list_candidate_assets(
                hotel_code=hotel_code,
                limit=0,
                active_property_id=active_property_id,
                active_property_ids=active_property_ids,
            )
        except Exception:
            return {"images": [], "selection_reason": "fallback_candidates_failed", "tool_used": False}
        if not candidates:
            return {"images": [], "selection_reason": "no_candidates", "tool_used": False}
        limit = max(1, int(max_images or 3))
        ranked = self._rank_candidate_assets(
            candidates=candidates,
            user_message=user_message,
            assistant_message=assistant_message,
            intent=intent,
            limit=limit,
        )
        if not ranked:
            return {"images": [], "selection_reason": "no_relevant_candidates", "tool_used": False}
        selected: list[dict[str, Any]] = []
        for match in ranked[:limit]:
            description_text = str(match.get("description") or "").strip()
            category_text = str(match.get("category") or "").strip()
            tags_list = match.get("tags") if isinstance(match.get("tags"), list) else []
            if not description_text:
                if category_text:
                    description_text = f"{category_text.title()} highlight from hotel gallery."
                elif tags_list:
                    description_text = "Related to: " + ", ".join(
                        str(tag).strip() for tag in tags_list[:3] if str(tag).strip()
                    )
                else:
                    description_text = f"{str(match.get('title') or '').strip()} image from hotel gallery."
            property_name = str(match.get("property_name") or "").strip()
            property_id = match.get("property_id")
            selected.append(
                {
                    "id": int(match.get("id") or 0),
                    "title": str(match.get("title") or "").strip(),
                    "description": description_text,
                    "url": s3_storage_service.resolve_display_url(match.get("image_url")),
                    "category": category_text,
                    "tags": tags_list,
                    "property_id": int(property_id) if property_id is not None else None,
                    "property_name": property_name or None,
                }
            )
        return {
            "images": selected,
            "selection_reason": "deterministic_fallback",
            "tool_used": True,
            "candidate_count": len(candidates),
        }

    async def llm_select_images(
        self,
        *,
        hotel_code: str,
        user_message: str,
        assistant_message: str,
        intent: str = "",
        max_images: int = 0,
        trace_context: Optional[dict[str, Any]] = None,
        active_property_id: Optional[int] = None,
        active_property_ids: Optional[list[int]] = None,
    ) -> dict[str, Any]:
        if not str(getattr(settings, "openai_api_key", "") or "").strip():
            return {"images": [], "selection_reason": "openai_key_missing", "tool_used": False}

        candidates = await self.list_candidate_assets(
            hotel_code=hotel_code,
            limit=0,
            active_property_id=active_property_id,
            active_property_ids=active_property_ids,
        )
        if not candidates:
            return {"images": [], "selection_reason": "no_candidates", "tool_used": False}

        requested_max = int(max_images or 0)
        title_matched_candidates = self._select_candidates_by_title_mentions(
            candidates=candidates,
            assistant_message=assistant_message,
        )
        if not title_matched_candidates:
            return {
                "images": [],
                "selection_reason": "no_title_mentions_in_assistant_response",
                "tool_used": True,
                "candidate_count": len(candidates),
            }

        limit = len(title_matched_candidates) if requested_max <= 0 else min(requested_max, len(title_matched_candidates))
        if limit <= 0:
            return {"images": [], "selection_reason": "no_candidates", "tool_used": False}

        # Fast deterministic mode: no LLM inference, only title-grounded match
        # from the assistant response in mention order.
        if bool(getattr(settings, "chat_smart_images_fast_title_match_only", True)):
            selected = self._build_image_cards(title_matched_candidates, limit=limit)
            selected = await self._enrich_descriptions_from_kb(
                hotel_code=hotel_code,
                selected_images=selected,
                user_message=user_message,
                assistant_message=assistant_message,
                trace_context=trace_context,
            )
            return {
                "images": selected,
                "selection_reason": "title_mention_match_fast",
                "tool_used": True,
                "candidate_count": len(title_matched_candidates),
            }

        candidates = title_matched_candidates
        candidate_payload = [
            {
                "id": row["id"],
                "title": row["title"],
                "description": str(row["description"] or "").strip()[:140],
                "category": row["category"],
                "tags": (row["tags"] if isinstance(row.get("tags"), list) else [])[:8],
            }
            for row in candidates
        ]
        system_prompt = (
            "You are a FAST image selector for hotel chat.\n"
            "Select only candidates whose title is clearly mentioned in assistant_message.\n"
            "Do not infer from user intent. Do not guess. Do not add unrelated images.\n"
            "When multiple room titles are mentioned, include each mentioned room once.\n"
            "Order selected_ids by mention order in assistant_message.\n"
            "If no candidate title is mentioned, return empty list.\n"
            "Return valid JSON object only with keys:\n"
            "{"
            "\"selected_ids\": [int], "
            "\"reason\": \"short reason\""
            "}"
        )
        user_prompt = {
            "hotel_code": str(hotel_code or "").strip(),
            "intent": str(intent or "").strip(),
            "max_images": "all" if requested_max <= 0 else limit,
            "ordering_rule": "match topic order in assistant_message",
            "user_message": str(user_message or "").strip(),
            "assistant_message": str(assistant_message or "").strip(),
            "candidates": candidate_payload,
        }
        token_budget = max(140, min(420, 120 + (limit * 18)))
        raw = await llm_client.chat_with_json(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_prompt, ensure_ascii=False)},
            ],
            model=(
                str(getattr(settings, "chat_suggestions_llm_model", "") or "").strip()
                or str(getattr(settings, "chat_display_beautifier_model", "") or "").strip()
                or None
            ),
            temperature=0.0,
            max_tokens=token_budget,
            trace_context={
                "component": "services.image_tool_service",
                "what_this_call_is_for": "Select image IDs for chat UI carousel",
                "hotel_code": str(hotel_code or "").strip(),
                **(trace_context or {}),
            },
        )
        selected_ids_raw = raw.get("selected_ids") if isinstance(raw, dict) else []
        selected_ids: list[int] = []
        if isinstance(selected_ids_raw, list):
            for item in selected_ids_raw:
                try:
                    image_id = int(item)
                except (TypeError, ValueError):
                    continue
                if image_id not in selected_ids:
                    selected_ids.append(image_id)
                if len(selected_ids) >= limit:
                    break

        by_id = {int(item["id"]): item for item in candidates}
        selected_rows = []
        for image_id in selected_ids:
            match = by_id.get(image_id)
            if not match:
                continue
            selected_rows.append(match)
            if len(selected_rows) >= limit:
                break
        selected = self._build_image_cards(selected_rows, limit=limit)

        selected = await self._enrich_descriptions_from_kb(
            hotel_code=hotel_code,
            selected_images=selected,
            user_message=user_message,
            assistant_message=assistant_message,
            trace_context=trace_context,
        )

        return {
            "images": selected,
            "selection_reason": str(raw.get("reason") or "").strip() if isinstance(raw, dict) else "",
            "tool_used": True,
            "candidate_count": len(candidates),
        }

    async def _enrich_descriptions_from_kb(
        self,
        *,
        hotel_code: str,
        selected_images: list[dict[str, Any]],
        user_message: str,
        assistant_message: str,
        trace_context: Optional[dict[str, Any]] = None,
    ) -> list[dict[str, Any]]:
        if not selected_images:
            return selected_images
        if not bool(getattr(settings, "smart_image_kb_caption_enabled", True)):
            return selected_images

        kb_max_chars = max(3000, int(getattr(settings, "smart_image_kb_context_max_chars", 14000) or 14000))
        with db_config_service.scoped_hotel(hotel_code):
            kb_text = str(
                config_service.get_full_kb_text_with_sources(
                    max_chars=kb_max_chars,
                    max_sources=120,
                )
                or ""
            ).strip()
        if not kb_text:
            return selected_images

        snippet_by_id = self._kb_snippet_map_for_images(
            kb_text=kb_text,
            selected_images=selected_images,
            max_chars_per_image=950,
        )

        system_prompt = (
            "You write short image card descriptions for a hotel chat UI.\n"
            "Use only facts grounded in the provided KB context.\n"
            "For each image id, return one concise description (8-24 words).\n"
            "If KB has no relevant fact for an image title, keep the existing description unchanged.\n"
            "Return valid JSON only with this shape:\n"
            "{"
            "\"images\": ["
            "{\"id\": 1, \"description\": \"...\"}"
            "]"
            "}"
        )
        user_payload = {
            "hotel_code": str(hotel_code or "").strip(),
            "user_message": str(user_message or "").strip(),
            "assistant_message": str(assistant_message or "").strip(),
            "selected_images": [
                {
                    "id": int(item.get("id") or 0),
                    "title": str(item.get("title") or "").strip(),
                    "existing_description": str(item.get("description") or "").strip(),
                    "kb_snippet": str(snippet_by_id.get(int(item.get("id") or 0)) or "").strip(),
                }
                for item in selected_images
            ],
            "kb_context": kb_text,
        }
        caption_model = str(getattr(settings, "smart_image_kb_caption_model", "") or "").strip() or None
        caption_tokens = max(180, int(getattr(settings, "smart_image_kb_caption_max_tokens", 420) or 420))
        raw = await llm_client.chat_with_json(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            model=caption_model,
            temperature=0.1,
            max_tokens=caption_tokens,
            trace_context={
                "component": "services.image_tool_service",
                "what_this_call_is_for": "Generate KB-grounded image descriptions",
                "hotel_code": str(hotel_code or "").strip(),
                **(trace_context or {}),
            },
        )
        image_rows = []
        if isinstance(raw, dict):
            rows_any = raw.get("images")
            if isinstance(rows_any, list):
                image_rows = rows_any
            elif isinstance(raw.get("descriptions"), list):
                image_rows = raw.get("descriptions")

        descriptions_by_id: dict[int, str] = {}
        for row in image_rows:
            if not isinstance(row, dict):
                continue
            try:
                image_id = int(row.get("id"))
            except (TypeError, ValueError):
                continue
            description = re.sub(r"\s+", " ", str(row.get("description") or "").strip())
            if not description:
                continue
            descriptions_by_id[image_id] = description[:220]

        if not descriptions_by_id:
            return selected_images

        enriched: list[dict[str, Any]] = []
        for item in selected_images:
            image_id = int(item.get("id") or 0)
            updated = dict(item)
            replacement = descriptions_by_id.get(image_id)
            if replacement:
                updated["description"] = replacement
            enriched.append(updated)
        return enriched


image_tool_service = ImageToolService()
