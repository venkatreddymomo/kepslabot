"""
Quick read-only inspector for the Phase 2-4 KB tables.

Prints, per hotel: count of properties, books (with descriptions), pages, joins.
Plus a sample of the books and the first few pages of one book.

Usage:
    python scripts/inspect_kb_tables.py              # all hotels
    python scripts/inspect_kb_tables.py --hotel iconiqa
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from sqlalchemy import func, select

from models.database import (
    AsyncSessionLocal,
    Hotel,
    KBBook,
    KBBookPage,
    KBPage,
    Property,
)


async def _inspect(hotel_code: str | None) -> int:
    async with AsyncSessionLocal() as session:
        stmt = select(Hotel).order_by(Hotel.code)
        if hotel_code:
            stmt = stmt.where(Hotel.code == hotel_code)
        hotels = (await session.execute(stmt)).scalars().all()
        if not hotels:
            print(f"No hotels found (filter={hotel_code!r}).")
            return 1

        for hotel in hotels:
            print("\n" + "=" * 70)
            print(f"Hotel: id={hotel.id}  code={hotel.code}  name={hotel.name}")
            print("=" * 70)

            # Counts
            prop_count = (
                await session.execute(
                    select(func.count(Property.id)).where(Property.hotel_id == hotel.id)
                )
            ).scalar_one()
            book_count = (
                await session.execute(
                    select(func.count(KBBook.id)).where(KBBook.hotel_id == hotel.id)
                )
            ).scalar_one()
            page_count = (
                await session.execute(
                    select(func.count(KBPage.id)).where(KBPage.hotel_id == hotel.id)
                )
            ).scalar_one()
            books_with_desc = (
                await session.execute(
                    select(func.count(KBBook.id)).where(
                        KBBook.hotel_id == hotel.id,
                        KBBook.description.isnot(None),
                        func.length(KBBook.description) > 0,
                    )
                )
            ).scalar_one()

            print(f"  Properties        : {prop_count}")
            print(f"  Books             : {book_count}")
            print(f"  Pages             : {page_count}")
            print(f"  Books with desc.  : {books_with_desc} / {book_count}")

            if not (prop_count or book_count or page_count):
                print("\n  WARNING: tables empty for this hotel.")
                print("  Possible causes:")
                print("   - dual-write didn't fire (reindex not run since the new code, OR error during write)")
                print("   - hotel.code mismatch with db_config_service.get_current_hotel_code()")
                print("   - app started but reindex failed silently")
                continue

            # Properties listing
            props = (
                await session.execute(
                    select(Property)
                    .where(Property.hotel_id == hotel.id)
                    .order_by(Property.is_default.desc(), Property.name)
                )
            ).scalars().all()
            if props:
                print("\n  Properties:")
                for p in props:
                    default_marker = " [DEFAULT]" if p.is_default else ""
                    print(f"    - slug={p.property_slug!r:24} name={p.name!r}{default_marker}")

            # Sample of books (max 10)
            books = (
                await session.execute(
                    select(KBBook)
                    .where(KBBook.hotel_id == hotel.id)
                    .order_by(KBBook.title)
                    .limit(10)
                )
            ).scalars().all()
            if books:
                print(f"\n  Books (showing up to 10):")
                for b in books:
                    desc = (b.description or "(no description)")[:90]
                    print(f"    - slug={b.book_slug!r:30} pages={b.page_count}")
                    print(f"      title: {b.title}")
                    print(f"      desc:  {desc}")

            # First book's pages
            if books:
                first_book = books[0]
                join_rows = (
                    await session.execute(
                        select(KBPage.page_slug, KBPage.title, KBBookPage.ordinal)
                        .join(KBBookPage, KBBookPage.page_id == KBPage.id)
                        .where(KBBookPage.book_id == first_book.id)
                        .order_by(KBBookPage.ordinal)
                        .limit(5)
                    )
                ).all()
                if join_rows:
                    print(f"\n  First 5 pages of book {first_book.book_slug!r}:")
                    for slug, title, ordinal in join_rows:
                        print(f"    [{ordinal}] {slug}  -  {title or '(untitled)'}")

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hotel", help="Filter to a single hotel.code")
    args = parser.parse_args()
    sys.exit(asyncio.run(_inspect(args.hotel)))
