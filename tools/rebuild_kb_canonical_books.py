"""
Rebuild one property's KB into the 13 canonical books Zone uses.

The admin reindex lets an LLM name the books. It files the same page under
several names, invents per-room and per-outlet books, and turns the file's
header banner into a book of its own. Zone was rebuilt into 13 fixed books per
property in July 2026 so that a question like "which hotel has a pool?" finds
the same book in every property. This is that rebuild as a reusable tool.

Input: the onboarding KB txt with the 10 standard `=== Section ===` headers.
  - Each section becomes its book, line for line (`## ` sub-headings kept).
    The header banner above the first section is dropped.
  - Pet, child and extra-bed / occupancy lines also get their own book. They
    are MOVED out of Policies (Zone's rule) and COPIED from any other section
    together with their `## <heading>` (an onboarding file may keep child policy
    and extra beds under Rooms). A blank line ends a `## ` block.

Writes, only with --apply, one property at a time after a JSON backup:
  - one page per book, slug `canon__<property_slug>__<book_slug>`
  - the 13 books (titles and descriptions copied from Zone), each linked to its
    one page
  - every other book of that property set to is_active=0 (kept, not deleted)

The bot caches the book catalog for an hour, so restart the server afterwards.
A later admin reindex rebuilds the LLM books and undoes this.

Run from the project root (the database URL comes from .env):
  python tools/rebuild_kb_canonical_books.py --hotel t24 \
      --property t24_residency_andheri_east=T24_Residency_Andheri_East_KB.txt \
      --property t24_retro_vile_parle_east=T24_Retro_Vile_Parle_East_KB.txt \
      [--apply]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy import delete, func, select, update

from config.settings import settings
from models.database import AsyncSessionLocal, Hotel, KBBook, KBBookPage, KBPage, Property, engine
from services.kb_table_writer import _upsert_and_get_id

BACKUP_DIR = ROOT_DIR / "backups" / "kb_canonical"

# Copied verbatim from Zone's 24 properties (hotel 67), where every property
# carries these exact titles and descriptions.
CANONICAL_BOOKS: list[tuple[str, str, str]] = [
    ("hotel_overview", "Hotel Overview",
     "Snapshot of the property — type, star rating, brand, total rooms, general location and vibe. For broad 'tell me about this hotel' / overview questions."),
    ("rooms_suites", "Rooms & Suites",
     "Room & suite categories — names, sizes, bed types, unit counts, in-room features (balcony, view, TV, wifi, minibar, safe), connecting/accessible rooms. For room types, sizes, 'which room has a balcony/view', suites, family rooms."),
    ("dining_restaurants", "Dining & Restaurants",
     "Restaurants, bars & in-room dining — names, cuisines, meal timings, breakfast details/inclusions, room service, veg/dietary options. For dining, restaurants, bar, breakfast, food, cuisines, room service."),
    ("amenities_facilities", "Amenities & Facilities",
     "Hotel-wide facilities — pool (+timings), gym, parking, wifi, kids play area, lift/elevator, business centre, laundry, airport shuttle. For 'do you have a pool/gym/parking/wifi', facilities, leisure."),
    ("spa_wellness", "Spa & Wellness",
     "Spa name, treatments/massages, pricing, spa timings, wellness/fitness. For spa, massage, treatments, wellness."),
    ("meetings_events", "Meetings & Events",
     "Meeting rooms, conference & banquet halls, indoor/outdoor event spaces, auditoriums, lawns, wedding facilities, capacities. For meetings, conferences, banquets, events, weddings, halls, corporate events."),
    ("location_transport", "Location & Transport",
     "Address, distances to airport/railway/bus, directions, maps/GPS, nearby attractions, parking access, transfers. For address, 'how far from airport', directions, nearby places."),
    ("contact_information", "Contact Information",
     "Phone, toll-free, email, WhatsApp, website, reservations & sales contacts. For contact number, phone, email, 'how do I reach you'."),
    ("policies_rules", "Policies & Rules",
     "Check-in/check-out times, cancellation, payment methods, ID/documents, smoking, couple/local-ID, early check-in/late check-out. For check-in/out time, cancellation, house rules. (Pet, child & extra-bed are their own books.)"),
    ("pet_policy", "Pet Policy",
     "Pet rules only — whether pets are allowed, room types, charges/deposit, disclaimer. For 'are pets allowed', 'pet friendly', 'can I bring my dog'."),
    ("child_policy", "Child Policy",
     "Children only — child age definition and complimentary-stay age. For 'kids policy', 'do children stay free', 'child age limit'."),
    ("room_occupancy_extra_bed", "Room Occupancy & Extra Bed",
     "Max occupancy per room, extra bed / additional mattress availability and charges, bedding. For 'extra bed', 'how many people per room', occupancy."),
    ("special_offers_packages", "Special Offers & Packages",
     "All deals — direct-booking, seasonal/festive, early-bird, long-stay/weekend, credit-card, bar/dining offers, memberships. For offers, discounts, deals, packages, promotions."),
]
CANONICAL_SLUGS = [slug for slug, _title, _desc in CANONICAL_BOOKS]

SECTION_TO_BOOK: dict[str, str] = {
    "property overview": "hotel_overview",
    "rooms & suites": "rooms_suites",
    "dining & restaurants": "dining_restaurants",
    "amenities & facilities": "amenities_facilities",
    "spa & wellness": "spa_wellness",
    "meetings & events": "meetings_events",
    "policies & rules": "policies_rules",
    "location & transport": "location_transport",
    "contact information": "contact_information",
    "special offers & packages": "special_offers_packages",
}
POLICIES_BOOK = "policies_rules"

# Matched against the label of a top-level "- Label: value" line; first match wins.
SPLIT_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("pet_policy", re.compile(r"\bpet", re.IGNORECASE)),
    ("child_policy", re.compile(r"^(child|baby)|\bbaby\b", re.IGNORECASE)),
    ("room_occupancy_extra_bed", re.compile(r"extra bed|occupancy", re.IGNORECASE)),
]

_SECTION_RE = re.compile(r"^===\s*([^=\s].*?)\s*===$")
_LABEL_RE = re.compile(r"^-\s+([^:]+):")


def _split_target(line: str) -> str | None:
    match = _LABEL_RE.match(line)
    if not match:
        return None
    label = match.group(1).strip()
    for slug, pattern in SPLIT_RULES:
        if pattern.search(label):
            return slug
    return None


def _tidy(lines: list[str]) -> str:
    out: list[str] = []
    for line in lines:
        if not line.strip() and (not out or not out[-1].strip()):
            continue  # no leading or doubled blank lines
        out.append(line.rstrip())
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out)


def parse_sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        match = _SECTION_RE.match(line.strip())
        if match:
            name = match.group(1).strip().lower()
            if name not in SECTION_TO_BOOK:
                raise SystemExit(f"Unknown section header: {match.group(1)!r}")
            if name in sections:
                raise SystemExit(f"Section appears twice: {match.group(1)!r}")
            current = name
            sections[current] = []
            continue
        if current is None:
            continue  # header banner above the first section
        sections[current].append(line)
    missing = [name for name in SECTION_TO_BOOK if name not in sections]
    if missing:
        raise SystemExit(f"Missing sections: {missing}")
    return sections


def build_pages(text: str) -> tuple[dict[str, str], list[str]]:
    """Return {book_slug: page content} and a report of every moved/copied line."""
    own: dict[str, list[str]] = {slug: [] for slug in CANONICAL_SLUGS}
    split_items: dict[str, list[tuple[str | None, tuple[str, ...]]]] = {
        slug: [] for slug, _pattern in SPLIT_RULES
    }
    report: list[str] = []

    for section, lines in parse_sections(text).items():
        book = SECTION_TO_BOOK[section]
        heading: str | None = None
        i = 0
        while i < len(lines):
            line = lines[i]
            if not line.strip():
                heading = None
                own[book].append("")
                i += 1
                continue
            if line.startswith("## "):
                heading = line
                own[book].append(line)
                i += 1
                continue
            item = [line]
            i += 1
            while i < len(lines) and lines[i].strip() and lines[i][:1] in (" ", "\t"):
                item.append(lines[i])
                i += 1
            target = _split_target(line)
            if target and book == POLICIES_BOOK:
                split_items[target].append((heading, tuple(item)))
                report.append(f"  MOVE {book} -> {target}: {line[:100]}")
            else:
                own[book].extend(item)
                if target:
                    split_items[target].append((heading, tuple(item)))
                    report.append(f"  COPY {book} -> {target}: {(heading + ' / ') if heading else ''}{line[:100]}")

    for slug, items in split_items.items():
        seen: set[tuple[str | None, tuple[str, ...]]] = set()
        last_heading: str | None = None
        for heading, item in items:
            if (heading, item) in seen:
                continue
            seen.add((heading, item))
            if heading != last_heading:
                own[slug].append("")
                if heading:
                    own[slug].append(heading)
                last_heading = heading
            own[slug].extend(item)

    pages = {slug: _tidy(lines) for slug, lines in own.items()}
    empty = [slug for slug, content in pages.items() if not content.strip()]
    if empty:
        raise SystemExit(f"These books would be empty, refusing to build them: {empty}")
    return pages, report


def _row_dict(row) -> dict:
    out = {}
    for column in row.__table__.columns:
        value = getattr(row, column.key)
        out[column.key] = value.isoformat() if isinstance(value, datetime) else value
    return out


async def _backup(session, hotel_id: int, property_id: int, path: Path) -> None:
    books = (await session.execute(
        select(KBBook).where(KBBook.hotel_id == hotel_id, KBBook.property_id == property_id)
    )).scalars().all()
    pages = (await session.execute(
        select(KBPage).where(KBPage.hotel_id == hotel_id, KBPage.property_id == property_id)
    )).scalars().all()
    book_ids = [b.id for b in books]
    links = (await session.execute(
        select(KBBookPage).where(KBBookPage.book_id.in_(book_ids))
    )).scalars().all() if book_ids else []
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "hotel_id": hotel_id,
        "property_id": property_id,
        "books": [_row_dict(b) for b in books],
        "pages": [_row_dict(p) for p in pages],
        "book_pages": [_row_dict(link) for link in links],
    }, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


async def rebuild_property(hotel_code: str, property_slug: str, source: Path, apply: bool) -> None:
    pages, report = build_pages(source.read_text(encoding="utf-8"))
    print(f"\n=== {property_slug}  <-  {source.name}")
    for slug in CANONICAL_SLUGS:
        content = pages[slug]
        print(f"  {slug:<26} {content.count(chr(10)) + 1:>3} lines {len(content):>5} chars")
    print("\n".join(report) or "  (no lines moved or copied)")

    async with AsyncSessionLocal() as session:
        hotel = (await session.execute(
            select(Hotel).where(func.lower(Hotel.code) == hotel_code.lower())
        )).scalar_one_or_none()
        if hotel is None:
            raise SystemExit(f"No hotel with code {hotel_code!r}")
        prop = (await session.execute(
            select(Property).where(Property.hotel_id == hotel.id, Property.property_slug == property_slug)
        )).scalar_one_or_none()
        if prop is None:
            raise SystemExit(f"No property {property_slug!r} under hotel {hotel.code} (id {hotel.id})")
        others = (await session.execute(
            select(KBBook.book_slug).where(
                KBBook.hotel_id == hotel.id,
                KBBook.property_id == prop.id,
                KBBook.is_active.is_(True),
                KBBook.book_slug.notin_(CANONICAL_SLUGS),
            )
        )).scalars().all()
        print(f"  hotel {hotel.code} id={hotel.id}, property id={prop.id}; "
              f"{len(others)} other active book(s) to deactivate: {sorted(others)}")
        if not apply:
            print("  DRY RUN - nothing written. Re-run with --apply.")
            return

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = BACKUP_DIR / f"{hotel.code.lower()}_{property_slug}_{stamp}.json"
        await _backup(session, hotel.id, prop.id, backup)
        print(f"  backup: {backup}")

        for slug, title, description in CANONICAL_BOOKS:
            content = pages[slug]
            page_id = await _upsert_and_get_id(
                session, KBPage,
                values={
                    "hotel_id": hotel.id,
                    "property_id": prop.id,
                    "page_slug": f"canon__{property_slug}__{slug}",
                    "title": title,
                    "content": content,
                    "topics": [slug],
                    "char_count": len(content),
                },
                conflict_cols=[KBPage.hotel_id, KBPage.page_slug],
                update_keys=["property_id", "title", "content", "topics", "char_count"],
                id_col=KBPage.id,
            )
            book_id = await _upsert_and_get_id(
                session, KBBook,
                values={
                    "hotel_id": hotel.id,
                    "property_id": prop.id,
                    "book_slug": slug,
                    "title": title,
                    "description": description,
                    "aliases": None,
                    "keywords": None,
                    "page_count": 1,
                    "char_count": len(content),
                    "source_names": [source.name],
                    "content_hash": None,
                    "is_active": True,
                },
                conflict_cols=[KBBook.hotel_id, KBBook.property_id, KBBook.book_slug],
                update_keys=["title", "description", "aliases", "keywords", "page_count",
                             "char_count", "source_names", "content_hash", "is_active"],
                id_col=KBBook.id,
            )
            await session.execute(delete(KBBookPage).where(KBBookPage.book_id == book_id))
            session.add(KBBookPage(book_id=book_id, page_id=page_id, ordinal=0))

        await session.execute(
            update(KBBook)
            .where(
                KBBook.hotel_id == hotel.id,
                KBBook.property_id == prop.id,
                KBBook.book_slug.notin_(CANONICAL_SLUGS),
            )
            .values(is_active=False)
        )
        await session.commit()

        active = (await session.execute(
            select(KBBook.book_slug, KBPage.content)
            .join(KBBookPage, KBBookPage.book_id == KBBook.id)
            .join(KBPage, KBPage.id == KBBookPage.page_id)
            .where(KBBook.hotel_id == hotel.id, KBBook.property_id == prop.id, KBBook.is_active.is_(True))
        )).all()
        got = {slug: content for slug, content in active}
        problems = [slug for slug in CANONICAL_SLUGS if got.get(slug) != pages[slug]]
        if len(active) != len(CANONICAL_SLUGS) or problems:
            raise SystemExit(f"  VERIFY FAILED: {len(active)} active links, mismatched books {problems}")
        print(f"  verified: {len(active)} active books, each linked to its canonical page")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hotel", required=True, help="hotel code, e.g. t24")
    parser.add_argument("--property", action="append", required=True, metavar="SLUG=FILE",
                        help="property_slug=path/to/kb.txt (repeat per property)")
    parser.add_argument("--apply", action="store_true", help="write to the database (default: dry run)")
    args = parser.parse_args()

    print(f"database: {str(settings.database_url).split('@')[-1]}")
    try:
        for spec in args.property:
            slug, _, file_name = spec.partition("=")
            source = Path(file_name)
            if not source.is_absolute():
                source = ROOT_DIR / source
            if not slug or not source.is_file():
                raise SystemExit(f"Bad --property {spec!r} (expected slug=existing_file)")
            await rebuild_property(args.hotel, slug.strip(), source, args.apply)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
