"""
Backfill the new first-class KB tables from the existing JSON blob.

Reads the structured library currently stored in:
    new_bot_business_config.config_value  (JSON, keyed by "knowledge_base")

and populates:
    - new_bot_properties
    - new_bot_kb_books
    - new_bot_kb_pages
    - new_bot_kb_book_pages

Safe to run multiple times (upserts on slug-based unique keys).
Does NOT delete the existing JSON blob — readers continue to use it until
Phase 3 flips them over to the tables.

Usage:
    python scripts/backfill_kb_tables.py              # all hotels
    python scripts/backfill_kb_tables.py --hotel CODE # one hotel
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

# Add project root to sys.path so `models`, `config` imports work.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from sqlalchemy import select, delete
from sqlalchemy.dialects.postgresql import insert as pg_insert

from models.database import (
    AsyncSessionLocal,
    Hotel,
    Property,
    KBBook,
    KBPage,
    KBBookPage,
    BusinessConfig,
    init_db,
)


def _coerce_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return []


async def _load_library(session, hotel_id: int) -> dict[str, Any]:
    """Pull the JSON knowledge_base section for one hotel."""
    stmt = select(BusinessConfig.config_value).where(
        BusinessConfig.hotel_id == hotel_id,
        BusinessConfig.config_key == "knowledge_base",
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if not row:
        return {}
    try:
        payload = json.loads(row) if isinstance(row, str) else row
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


async def _backfill_hotel(session, hotel: Hotel) -> dict[str, int]:
    """Populate the new tables for one hotel. Returns per-entity counts."""
    knowledge = await _load_library(session, hotel.id)
    library = knowledge.get("library_index") or {}
    scope = knowledge.get("property_scope_index") or {}

    books = _coerce_list(library.get("books"))
    pages = _coerce_list(library.get("pages"))
    properties_raw = _coerce_list(scope.get("properties"))
    common_page_ids = set(str(pid) for pid in _coerce_list(scope.get("common_page_ids")))

    counts = {"properties": 0, "pages": 0, "books": 0, "book_pages": 0}

    # --- 1) upsert properties ------------------------------------------------
    page_to_property: dict[str, int | None] = {}
    prop_slug_to_id: dict[str, int] = {}

    for row in properties_raw:
        if not isinstance(row, dict):
            continue
        slug = str(row.get("id") or "").strip()
        if not slug:
            continue
        name = str(row.get("name") or slug.replace("_", " ").title()).strip()
        aliases = [str(a).strip() for a in _coerce_list(row.get("aliases")) if str(a).strip()]
        source_names = [
            str(s).strip() for s in _coerce_list(row.get("source_names")) if str(s).strip()
        ]

        stmt = (
            pg_insert(Property)
            .values(
                hotel_id=hotel.id,
                property_slug=slug,
                name=name,
                aliases=aliases or None,
                source_names=source_names or None,
                is_active=True,
                is_default=False,
            )
            .on_conflict_do_update(
                index_elements=[Property.hotel_id, Property.property_slug],
                set_={
                    "name": name,
                    "aliases": aliases or None,
                    "source_names": source_names or None,
                },
            )
            .returning(Property.id)
        )
        result = await session.execute(stmt)
        prop_id = result.scalar_one()
        prop_slug_to_id[slug] = prop_id
        counts["properties"] += 1

        # Map each page owned by this property → property_id
        for page_id in _coerce_list(row.get("page_ids")):
            page_to_property[str(page_id)] = prop_id

    # If hotel has zero properties in scope but has pages, create a synthetic
    # "default" property row so the book/page FKs have somewhere to land.
    # (Single-property hotels.)
    if not prop_slug_to_id and (pages or books):
        default_slug = "default"
        stmt = (
            pg_insert(Property)
            .values(
                hotel_id=hotel.id,
                property_slug=default_slug,
                name=hotel.name or default_slug.title(),
                is_active=True,
                is_default=True,
            )
            .on_conflict_do_update(
                index_elements=[Property.hotel_id, Property.property_slug],
                set_={"name": hotel.name or default_slug.title(), "is_default": True},
            )
            .returning(Property.id)
        )
        result = await session.execute(stmt)
        prop_slug_to_id[default_slug] = result.scalar_one()
        counts["properties"] += 1

    # --- 2) upsert pages -----------------------------------------------------
    page_slug_to_id: dict[str, int] = {}

    for page in pages:
        if not isinstance(page, dict):
            continue
        slug = str(page.get("id") or "").strip()
        if not slug:
            continue

        resolved_property_id = page_to_property.get(slug)
        # For common pages (marked in scope) or unassigned: property_id = NULL.
        if slug in common_page_ids:
            resolved_property_id = None

        topics = [str(t).strip() for t in _coerce_list(page.get("topics")) if str(t).strip()]

        stmt = (
            pg_insert(KBPage)
            .values(
                hotel_id=hotel.id,
                property_id=resolved_property_id,
                document_id=str(page.get("document_id") or "") or None,
                page_slug=slug,
                title=str(page.get("title") or "") or None,
                content=str(page.get("text") or ""),
                source_name=str(page.get("source_name") or "") or None,
                source_path=str(page.get("source_path") or "") or None,
                location=str(page.get("location") or "") or None,
                topics=topics or None,
                char_count=int(page.get("char_count") or 0),
            )
            .on_conflict_do_update(
                index_elements=[KBPage.hotel_id, KBPage.page_slug],
                set_={
                    "property_id": resolved_property_id,
                    "title": str(page.get("title") or "") or None,
                    "content": str(page.get("text") or ""),
                    "source_name": str(page.get("source_name") or "") or None,
                    "source_path": str(page.get("source_path") or "") or None,
                    "location": str(page.get("location") or "") or None,
                    "topics": topics or None,
                    "char_count": int(page.get("char_count") or 0),
                },
            )
            .returning(KBPage.id)
        )
        result = await session.execute(stmt)
        page_slug_to_id[slug] = result.scalar_one()
        counts["pages"] += 1

    # --- 3) upsert books + rebuild join rows --------------------------------
    for book in books:
        if not isinstance(book, dict):
            continue
        slug = str(book.get("id") or "").strip()
        if not slug:
            continue

        book_page_slugs = [str(p) for p in _coerce_list(book.get("page_ids")) if str(p)]

        # Determine property_id for this book: use the property_id shared by its
        # pages (majority wins). If mixed or unknown, leave NULL (= common).
        prop_tally: dict[int | None, int] = {}
        for page_slug in book_page_slugs:
            prop_id = page_to_property.get(page_slug)
            if page_slug in common_page_ids:
                prop_id = None
            prop_tally[prop_id] = prop_tally.get(prop_id, 0) + 1
        resolved_book_property_id = None
        if prop_tally:
            resolved_book_property_id = max(prop_tally.items(), key=lambda kv: kv[1])[0]

        aliases = [str(a).strip() for a in _coerce_list(book.get("aliases")) if str(a).strip()]
        keywords = [str(k).strip() for k in _coerce_list(book.get("keywords")) if str(k).strip()]
        source_names = [
            str(s).strip() for s in _coerce_list(book.get("sources")) if str(s).strip()
        ]

        stmt = (
            pg_insert(KBBook)
            .values(
                hotel_id=hotel.id,
                property_id=resolved_book_property_id,
                book_slug=slug,
                title=str(book.get("title") or slug).strip() or slug,
                description=str(book.get("description") or "").strip() or None,
                aliases=aliases or None,
                keywords=keywords or None,
                page_count=int(book.get("page_count") or len(book_page_slugs)),
                char_count=int(book.get("char_count") or 0),
                source_names=source_names or None,
                is_active=True,
            )
            .on_conflict_do_update(
                index_elements=[KBBook.hotel_id, KBBook.property_id, KBBook.book_slug],
                set_={
                    "title": str(book.get("title") or slug).strip() or slug,
                    "description": str(book.get("description") or "").strip() or None,
                    "aliases": aliases or None,
                    "keywords": keywords or None,
                    "page_count": int(book.get("page_count") or len(book_page_slugs)),
                    "char_count": int(book.get("char_count") or 0),
                    "source_names": source_names or None,
                    "is_active": True,
                },
            )
            .returning(KBBook.id)
        )
        result = await session.execute(stmt)
        book_id = result.scalar_one()
        counts["books"] += 1

        # Rebuild join rows: drop old, insert fresh
        await session.execute(delete(KBBookPage).where(KBBookPage.book_id == book_id))
        for ordinal, page_slug in enumerate(book_page_slugs):
            page_id = page_slug_to_id.get(page_slug)
            if not page_id:
                continue
            await session.execute(
                pg_insert(KBBookPage)
                .values(book_id=book_id, page_id=page_id, ordinal=ordinal)
                .on_conflict_do_nothing(index_elements=[KBBookPage.book_id, KBBookPage.page_id])
            )
            counts["book_pages"] += 1

    await session.commit()
    return counts


async def _main(hotel_code: str | None) -> None:
    # Make sure new tables exist (no-op if already present).
    await init_db()

    async with AsyncSessionLocal() as session:
        stmt = select(Hotel)
        if hotel_code:
            stmt = stmt.where(Hotel.code == hotel_code)
        hotels = (await session.execute(stmt)).scalars().all()

        if not hotels:
            print(f"No hotels matched (hotel_code={hotel_code!r})")
            return

        total = {"properties": 0, "pages": 0, "books": 0, "book_pages": 0}
        for hotel in hotels:
            print(f"\n=== Backfilling hotel id={hotel.id} code={hotel.code} name={hotel.name} ===")
            try:
                counts = await _backfill_hotel(session, hotel)
            except Exception as exc:
                print(f"  ERROR: {exc!r}")
                await session.rollback()
                continue
            for k, v in counts.items():
                total[k] += v
                print(f"  {k}: {v}")

        print("\n--- Totals ---")
        for k, v in total.items():
            print(f"  {k}: {v}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hotel", help="Backfill only this hotel code")
    args = parser.parse_args()
    asyncio.run(_main(args.hotel))
