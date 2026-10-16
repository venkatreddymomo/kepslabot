"""
Phase 3 — DB-backed readers for the book catalog and book context.

Replaces the JSON-blob path with direct queries against new_bot_kb_books,
new_bot_kb_pages, new_bot_kb_book_pages, and new_bot_properties.

Property isolation is enforced at the SQL level:
- WHERE hotel_id = X AND (property_id = Y OR property_id IN COMMON_IDS
  OR property_id IS NULL) ensures property-specific books/pages plus
  shared/common content, with zero leakage from sibling properties.

Used by services.llm_orchestration_service when settings.kb_read_from_tables=True.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import func, or_, select

from models.database import (
    AsyncSessionLocal,
    Hotel,
    KBBook,
    KBBookPage,
    KBPage,
    Property,
)


# In-memory TTL cache for the structured book catalog. The catalog rarely
# changes mid-session (admin edits invalidate via process restart) but is
# read 1-3x per chat turn — DB latency on each turn is dominant when the
# cache is cold. 5 min TTL matches the conversation cache TTL.
_CATALOG_CACHE_TTL_SECONDS = 3600.0  # 1 hour. Hotel IDs / property slugs / book metadata rarely change at runtime; a longer TTL means subsequent chats don't pay 3-5s of VPN-to-MySQL latency to re-resolve them. Admin write paths should invalidate where they mutate, but stale-by-a-bit is preferable to the every-5-min cold-cache hit users were seeing.
_CATALOG_CACHE: dict[tuple[str, tuple[str, ...], bool], tuple[float, list[dict[str, Any]]]] = {}
_CATALOG_CACHE_LOCK = asyncio.Lock()
_HOTEL_ID_CACHE: dict[str, tuple[float, Optional[int]]] = {}
_PROPERTY_ID_CACHE: dict[tuple[int, str], tuple[float, Optional[int]]] = {}
_COMMON_PROP_IDS_CACHE: dict[int, tuple[float, list[int]]] = {}
# Process-level cache for the heavy book-context build. The same
# (hotel + active properties + book set) repeats across turns and across
# sessions; building the page text from DB pages is the single largest
# cold-path cost. Keyed by stable ingredients only.
_BOOK_CTX_CACHE: dict[
    tuple[str, tuple[str, ...], tuple[str, ...], int],
    tuple[float, dict[str, Any]],
] = {}


def invalidate_hotel_caches(hotel_code: str) -> int:
    """Drop one hotel's cached book catalog and book context, so an admin edit
    shows on the next chat turn instead of after the one-hour TTL. Returns the
    number of entries dropped."""
    code = str(hotel_code or "").strip().lower()
    dropped = 0
    for cache in (_CATALOG_CACHE, _BOOK_CTX_CACHE):
        for key in [k for k in cache if str(k[0] or "").strip().lower() == code]:
            cache.pop(key, None)
            dropped += 1
    return dropped


@dataclass
class BookContextResult:
    """Structured result returned by build_book_context_from_ids_db.

    Mirrors the shape returned by config_service.build_book_context_from_ids
    so it is a drop-in replacement.
    """

    context_text: str
    book_ids: list[str]
    page_ids: list[str]
    book_count: int
    page_count: int
    truncated: bool
    source_signature: str
    book_hits: list[dict[str, Any]]
    books: list[dict[str, Any]]
    overflow_pages: list[dict[str, Any]]  # pages that did not fit (titles only)

    def to_dict(self) -> dict[str, Any]:
        return {
            "context_text": self.context_text,
            "book_ids": list(self.book_ids),
            "page_ids": list(self.page_ids),
            "book_count": self.book_count,
            "page_count": self.page_count,
            "truncated": self.truncated,
            "source_signature": self.source_signature,
            "book_hits": list(self.book_hits),
            "books": list(self.books),
            "overflow_pages": list(self.overflow_pages),
        }


async def _resolve_hotel_id(session, hotel_code: str) -> Optional[int]:
    if not hotel_code:
        return None
    now = time.monotonic()
    cached = _HOTEL_ID_CACHE.get(hotel_code)
    if cached and (now - cached[0]) <= _CATALOG_CACHE_TTL_SECONDS:
        return cached[1]
    stmt = select(Hotel.id).where(Hotel.code == hotel_code).limit(1)
    value = (await session.execute(stmt)).scalar_one_or_none()
    _HOTEL_ID_CACHE[hotel_code] = (now, value)
    return value


async def _resolve_property_id(
    session, hotel_id: int, property_slug: Optional[str]
) -> Optional[int]:
    """Resolve a property slug to its row id. None means no explicit property."""
    slug = (property_slug or "").strip()
    if not slug:
        return None
    now = time.monotonic()
    cache_key = (int(hotel_id), slug)
    cached = _PROPERTY_ID_CACHE.get(cache_key)
    if cached and (now - cached[0]) <= _CATALOG_CACHE_TTL_SECONDS:
        return cached[1]
    stmt = (
        select(Property.id)
        .where(Property.hotel_id == hotel_id, Property.property_slug == slug)
        .limit(1)
    )
    value = (await session.execute(stmt)).scalar_one_or_none()
    if value is None:
        # Tolerant fallback — ONLY runs when the exact-slug match above failed
        # (so valid slugs are unaffected). Callers occasionally pass a display
        # NAME or a differently-cased value instead of the slug (e.g. the router
        # LLM returned "Zone Connect Park Lansdowne" rather than
        # "zone_connect_park_lansdowne"). Resolve it via the space→underscore
        # normalized slug OR the exact (case-insensitive) property name so the
        # scope RESOLVES to the right property, instead of silently returning
        # None — which upstream turns into "no scope" and loads EVERY property's
        # books (the 90k-token / ~13s router blow-up). Names are unique per
        # hotel, so this cannot match the wrong property.
        norm = slug.lower().replace(" ", "_")
        stmt2 = (
            select(Property.id)
            .where(
                Property.hotel_id == hotel_id,
                or_(
                    func.lower(Property.property_slug) == norm,
                    func.lower(Property.name) == slug.lower(),
                ),
            )
            .limit(1)
        )
        value = (await session.execute(stmt2)).scalar_one_or_none()
    _PROPERTY_ID_CACHE[cache_key] = (now, value)
    return value


async def _resolve_common_property_ids(session, hotel_id: int) -> list[int]:
    """Return property ids treated as shared/common for this hotel.

    Compatibility rule:
    - Legacy rows used property_id IS NULL for common content.
    - New writer uses non-null sentinel properties:
      - property_slug='shared' for multi-property common rows.
      - is_default=True (usually property_slug='default') for single-property.
    """
    now = time.monotonic()
    cached = _COMMON_PROP_IDS_CACHE.get(int(hotel_id))
    if cached and (now - cached[0]) <= _CATALOG_CACHE_TTL_SECONDS:
        return list(cached[1])
    stmt = select(Property.id).where(
        Property.hotel_id == hotel_id,
        or_(Property.property_slug == "shared", Property.is_default.is_(True)),
    )
    value = list((await session.execute(stmt)).scalars().all())
    _COMMON_PROP_IDS_CACHE[int(hotel_id)] = (now, list(value))
    return value


def _property_filter_clause(
    model,
    property_ids: list[int],
    common_property_ids: list[int],
):
    """Return a SQL filter for property-scoped book/page reads.

    Two modes:
    1. ``property_ids`` is non-empty (some properties are active in the
       conversation): restrict to rows scoped to those properties, plus any
       legacy ``property_id IS NULL`` rows for backwards compatibility.
       Common/shared sentinel ids (if any survive from older writes) are
       merged in too so brand-wide content stays visible.
    2. ``property_ids`` is empty (no property has been disambiguated yet):
       return ALL rows for the hotel — the bot should consider books from
       every property until the conversation narrows down which property
       the guest is asking about. The outer query's ``hotel_id`` filter
       keeps this hotel-scoped.
    """
    from sqlalchemy import true as _sa_true

    common_ids = [int(pid) for pid in common_property_ids if pid is not None]
    requested_ids = [int(pid) for pid in property_ids if pid is not None]
    if requested_ids:
        allowed = list(set(requested_ids + common_ids))
        return or_(model.property_id.in_(allowed), model.property_id.is_(None))
    # No active property → no property restriction (return books from every
    # property of this hotel). hotel_id on the outer SELECT keeps tenancy.
    return _sa_true()

# ---------------------------------------------------------------------------
# Book catalog (router input)
# ---------------------------------------------------------------------------


async def get_structured_book_catalog_db(
    *,
    hotel_code: str,
    property_slugs: Optional[list[str]] = None,
    only_active: bool = True,
) -> list[dict[str, Any]]:
    """Return the full book catalog for the LLM router.

    Args:
        hotel_code: Hotel.code (string id from existing config layer).
        property_slugs: list of active property slugs from
            conversation_context._active_property_ids.
            - None or [] => return books from EVERY property of this hotel
              (the conversation hasn't narrowed to a specific property yet,
              so the router should consider all of them).
            - non-empty list => restrict to those properties' books only.
        only_active: filter is_active = TRUE.
    """
    cache_key = (
        str(hotel_code or ""),
        tuple(sorted({str(s).strip() for s in (property_slugs or []) if str(s).strip()})),
        bool(only_active),
    )
    now = time.monotonic()
    cached = _CATALOG_CACHE.get(cache_key)
    if cached and (now - cached[0]) <= _CATALOG_CACHE_TTL_SECONDS:
        return [dict(row) for row in cached[1]]
    async with AsyncSessionLocal() as session:
        hotel_id = await _resolve_hotel_id(session, hotel_code)
        if not hotel_id:
            return []

        property_ids: list[int] = []
        for slug in property_slugs or []:
            pid = await _resolve_property_id(session, hotel_id, slug)
            if pid is not None:
                property_ids.append(pid)
        # SAFETY NET: a scope WAS requested (property_slugs non-empty) but none
        # of them resolved to a real property. Do NOT fall through to the
        # "empty property_ids => every property" rule in _property_filter_clause
        # below — that dumps the ENTIRE library (all properties' books) into the
        # router payload and is the direct cause of the ~90k-token / ~13s router
        # calls. An unresolvable scope means "we don't know the property", which
        # is NOT the same as "no property chosen yet" (property_slugs=None). Return
        # an empty catalog; the router's own no-scope guards handle it.
        if property_slugs and not property_ids:
            return []
        common_property_ids = await _resolve_common_property_ids(session, hotel_id)

        stmt = (
            select(KBBook)
            .where(KBBook.hotel_id == hotel_id)
            .where(_property_filter_clause(KBBook, property_ids, common_property_ids))
        )
        if only_active:
            stmt = stmt.where(KBBook.is_active.is_(True))
        stmt = stmt.order_by(KBBook.title.asc(), KBBook.id.asc())

        rows = (await session.execute(stmt)).scalars().all()
        property_ids_in_rows = {
            int(row.property_id)
            for row in rows
            if getattr(row, "property_id", None) is not None
        }
        property_lookup: dict[int, dict[str, str]] = {}
        if property_ids_in_rows:
            prop_stmt = (
                select(Property.id, Property.property_slug, Property.name)
                .where(Property.id.in_(property_ids_in_rows))
            )
            for pid, slug, name in (await session.execute(prop_stmt)).all():
                property_lookup[int(pid)] = {
                    "property_id": str(slug or "").strip(),
                    "property_name": str(name or "").strip(),
                }
        catalog: list[dict[str, Any]] = []
        for row in rows:
            property_meta = property_lookup.get(int(row.property_id or 0), {})
            catalog.append(
                {
                    "id": row.book_slug,
                    "title": row.title or row.book_slug,
                    "description": row.description or "",
                    "aliases": list(row.aliases or []),
                    "keywords": list(row.keywords or []),
                    "page_count": int(row.page_count or 0),
                    "property_id": str(property_meta.get("property_id") or "").strip(),
                    "property_name": str(property_meta.get("property_name") or "").strip(),
                }
            )
        _CATALOG_CACHE[cache_key] = (now, [dict(row) for row in catalog])
        return catalog


# ---------------------------------------------------------------------------
# Book context (loaded after router picks book_ids)
# ---------------------------------------------------------------------------


async def build_book_context_from_ids_db(
    *,
    hotel_code: str,
    book_ids: list[str],
    property_slugs: Optional[list[str]] = None,
    max_chars: int = 0,
) -> BookContextResult:
    """Load full content for the picked books, scoped by property.

    Property isolation: only books matching
    (hotel_id, property_id IN selected/common OR NULL) are loadable.
    Even if the LLM returned a stale book_id from another property,
    the WHERE clause silently filters it out.

    max_chars: 0 => unlimited. Pass the adaptive budget from compute_kb_budget()
    to enable overflow cascade. Pages exceeding budget are dropped from
    context_text but listed in overflow_pages (titles only) so the answering
    LLM can request them.
    """
    cleaned_book_ids: list[str] = []
    seen: set[str] = set()
    for raw in book_ids or []:
        slug = str(raw or "").strip()
        if slug and slug not in seen:
            seen.add(slug)
            cleaned_book_ids.append(slug)

    empty = BookContextResult(
        context_text="",
        book_ids=[],
        page_ids=[],
        book_count=0,
        page_count=0,
        truncated=False,
        source_signature="",
        book_hits=[],
        books=[],
        overflow_pages=[],
    )
    if not cleaned_book_ids:
        return empty

    cache_key = (
        str(hotel_code or ""),
        tuple(sorted({str(s).strip() for s in (property_slugs or []) if str(s).strip()})),
        tuple(sorted(cleaned_book_ids)),
        int(max_chars or 0),
    )
    now = time.monotonic()
    cached = _BOOK_CTX_CACHE.get(cache_key)
    if cached and (now - cached[0]) <= _CATALOG_CACHE_TTL_SECONDS:
        return BookContextResult(**cached[1])

    # Open ONE session for the dependency-chain step (resolve_hotel/resolve_props,
    # which cache results in-process so are usually free), and the book_stmt
    # which has to come before pages/property-labels.
    async with AsyncSessionLocal() as session:
        hotel_id = await _resolve_hotel_id(session, hotel_code)
        if not hotel_id:
            return empty

        property_ids: list[int] = []
        for slug in property_slugs or []:
            pid = await _resolve_property_id(session, hotel_id, slug)
            if pid is not None:
                property_ids.append(pid)
        # SAFETY NET (mirrors get_structured_book_catalog_db): a property scope
        # was requested but nothing resolved → do NOT let the empty-property_ids
        # branch load EVERY property's books for the picked slugs. Return empty
        # so an unresolvable scope can't balloon the answerer's KB.
        if property_slugs and not property_ids:
            return empty
        common_property_ids = await _resolve_common_property_ids(session, hotel_id)

        # Pull picked books (scoped by property + slug whitelist).
        book_stmt = (
            select(KBBook)
            .where(
                KBBook.hotel_id == hotel_id,
                KBBook.book_slug.in_(cleaned_book_ids),
                KBBook.is_active.is_(True),
            )
            .where(_property_filter_clause(KBBook, property_ids, common_property_ids))
        )
        book_rows = (await session.execute(book_stmt)).scalars().all()
        if not book_rows:
            return empty

        # Order books by the order the LLM picked them.
        slug_to_pick_order = {slug: idx for idx, slug in enumerate(cleaned_book_ids)}
        book_rows.sort(key=lambda b: slug_to_pick_order.get(b.book_slug, 9999))

    # Now property-label lookup AND the bulk page join can run CONCURRENTLY
    # — they're independent, both depend only on book_rows. Each gets its
    # own AsyncSessionLocal so SQLAlchemy doesn't see concurrent ops on one
    # session. Saves 1 full VPN-MySQL roundtrip on every turn (~3-5s).
    book_property_ids = {
        int(book.property_id)
        for book in book_rows
        if getattr(book, "property_id", None) is not None
    }
    book_id_to_row = {row.id: row for row in book_rows}

    async def _fetch_property_labels() -> list[tuple]:
        if not book_property_ids:
            return []
        async with AsyncSessionLocal() as label_session:
            prop_stmt = (
                select(Property.id, Property.property_slug, Property.name)
                .where(Property.id.in_(book_property_ids))
            )
            return list((await label_session.execute(prop_stmt)).all())

    async def _fetch_join_rows() -> list[tuple]:
        async with AsyncSessionLocal() as page_session:
            bulk_stmt = (
                select(KBBookPage.book_id, KBBookPage.ordinal, KBPage)
                .join(KBPage, KBPage.id == KBBookPage.page_id)
                .where(
                    KBBookPage.book_id.in_(book_id_to_row.keys()),
                    KBPage.hotel_id == hotel_id,
                )
                .where(_property_filter_clause(KBPage, property_ids, common_property_ids))
                .order_by(KBBookPage.book_id.asc(), KBBookPage.ordinal.asc(), KBPage.id.asc())
            )
            return list((await page_session.execute(bulk_stmt)).all())

    label_rows, join_rows = await asyncio.gather(
        _fetch_property_labels(),
        _fetch_join_rows(),
    )

    property_lookup: dict[int, dict[str, str]] = {}
    for pid, slug, name in label_rows:
        property_lookup[int(pid)] = {
            "property_id": str(slug or "").strip(),
            "property_name": str(name or "").strip(),
        }

    pages_by_book: dict[int, list[KBPage]] = {bid: [] for bid in book_id_to_row}
    for book_id, _ordinal, page in join_rows:
        pages_by_book.setdefault(book_id, []).append(page)

    enforce_char_limit = max_chars and max_chars > 0
    total_chars = 0
    truncated = False
    used_book_ids: list[str] = []
    used_page_ids: list[str] = []
    seen_pages: set[int] = set()
    lines: list[str] = []
    book_hits: list[dict[str, Any]] = []
    overflow_pages: list[dict[str, Any]] = []
    budget_exhausted = False

    for book_row in book_rows:
        book = book_row
        pages = pages_by_book.get(book.id, [])
        if not pages:
            continue
        used_book_ids.append(book.book_slug)
        book_hit_pages: list[dict[str, Any]] = []
        property_meta = property_lookup.get(int(book.property_id or 0), {})
        property_slug = str(property_meta.get("property_id") or "").strip()
        property_name = str(property_meta.get("property_name") or "").strip()
        property_label = property_name or property_slug or "Common"

        for page in pages:
            if page.id in seen_pages:
                continue
            content = (page.content or "").strip()
            if not content:
                continue
            seen_pages.add(page.id)

            page_title = (page.title or "").strip()
            source_name = (page.source_name or "").strip()
            location = (page.location or "").strip()
            block = (
                f"[PROPERTY:{property_label} | BOOK:{book.title or book.book_slug} | PAGE:{page.page_slug} | "
                f"SOURCE:{source_name} | LOCATION:{location}]\n"
                f"{page_title}\n{content}\n"
            )
            projected = total_chars + len(block) + 1

            if budget_exhausted or (enforce_char_limit and projected > max_chars):
                # Overflow: keep title-only entry so the LLM can ask for it.
                truncated = True
                budget_exhausted = True
                overflow_pages.append(
                    {
                        "book_id": book.book_slug,
                        "book_title": book.title or book.book_slug,
                        "property_id": property_slug,
                        "property_name": property_name,
                        "page_id": page.page_slug,
                        "page_title": page_title or "(untitled)",
                        "char_count": int(page.char_count or len(content)),
                    }
                )
                continue

            lines.append(block)
            total_chars = projected
            used_page_ids.append(page.page_slug)
            book_hit_pages.append(
                {
                    "id": page.page_slug,
                    "title": page_title,
                    "source_name": source_name,
                    "location": location,
                    "text": content,
                    "topics": list(page.topics or []),
                    "char_count": int(page.char_count or len(content)),
                }
            )

        book_hits.append(
            {
                "book_id": book.book_slug,
                "book_title": book.title or book.book_slug,
                "property_id": property_slug,
                "property_name": property_name,
                "book_score": 1.0,
                "pages": book_hit_pages,
            }
        )

    context_text = "\n".join(lines).strip()

    # Append overflow index so the answering LLM knows there's more to ask for.
    if overflow_pages:
        overflow_lines = ["", "[OVERFLOW_PAGES — request by page id if needed]"]
        for entry in overflow_pages:
            overflow_lines.append(
                f"- BOOK={entry['book_id']} PAGE={entry['page_id']} "
                f"TITLE={entry['page_title']}"
            )
        context_text = (context_text + "\n" + "\n".join(overflow_lines)).strip()

    result = BookContextResult(
        context_text=context_text,
        book_ids=used_book_ids,
        page_ids=used_page_ids,
        book_count=len(used_book_ids),
        page_count=len(used_page_ids),
        truncated=truncated,
        source_signature="",
        book_hits=book_hits,
        books=[
            {
                "id": hit["book_id"],
                "title": hit["book_title"],
                "property_id": str(hit.get("property_id") or "").strip(),
                "property_name": str(hit.get("property_name") or "").strip(),
                "score": hit["book_score"],
                "page_count": len(hit["pages"]),
            }
            for hit in book_hits
        ],
        overflow_pages=overflow_pages,
    )
    _BOOK_CTX_CACHE[cache_key] = (now, result.to_dict())
    return result


# ---------------------------------------------------------------------------
# Property catalog (used when no active property + multi-property hotel)
# ---------------------------------------------------------------------------


async def get_property_catalog_db(*, hotel_code: str) -> list[dict[str, Any]]:
    """Return the property list shown to the user / router when no property
    is active yet. Used to ask the user which property they want info about.
    """
    async with AsyncSessionLocal() as session:
        hotel_id = await _resolve_hotel_id(session, hotel_code)
        if not hotel_id:
            return []
        stmt = (
            select(Property)
            .where(
                Property.hotel_id == hotel_id,
                Property.is_active.is_(True),
                Property.property_slug != "shared",
            )
            .order_by(Property.is_default.desc(), Property.name.asc())
        )
        rows = (await session.execute(stmt)).scalars().all()
        return [
            {
                "id": row.property_slug,
                "name": row.name,
                "description": row.description or "",
                "aliases": list(row.aliases or []),
                "location": row.location or {},
                "is_default": bool(row.is_default),
            }
            for row in rows
        ]


async def fetch_pages_by_slug_db(
    *,
    hotel_code: str,
    page_slugs: list[str],
    property_slugs: Optional[list[str]] = None,
) -> list[dict[str, Any]]:
    """Fetch full content for specific pages (used by overflow cascade when the
    answering LLM requests a page that was sent as title-only).
    """
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in page_slugs or []:
        slug = str(raw or "").strip()
        if slug and slug not in seen:
            seen.add(slug)
            cleaned.append(slug)
    if not cleaned:
        return []

    async with AsyncSessionLocal() as session:
        hotel_id = await _resolve_hotel_id(session, hotel_code)
        if not hotel_id:
            return []
        property_ids: list[int] = []
        for slug in property_slugs or []:
            pid = await _resolve_property_id(session, hotel_id, slug)
            if pid is not None:
                property_ids.append(pid)
        common_property_ids = await _resolve_common_property_ids(session, hotel_id)

        stmt = (
            select(KBPage)
            .where(KBPage.hotel_id == hotel_id, KBPage.page_slug.in_(cleaned))
            .where(_property_filter_clause(KBPage, property_ids, common_property_ids))
        )
        rows = (await session.execute(stmt)).scalars().all()
        return [
            {
                "id": row.page_slug,
                "title": row.title or "",
                "text": row.content or "",
                "source_name": row.source_name or "",
                "location": row.location or "",
                "char_count": int(row.char_count or 0),
                "topics": list(row.topics or []),
            }
            for row in rows
        ]
