"""
Hard-delete a set of Zone sub-properties and ALL their KB content from the DB,
as if they never existed.

DESTRUCTIVE. Dry-run by default — nothing is written until you pass --apply.

Scope: hotel code == "zone" ONLY. For each MATCHED property it deletes, inside a
single transaction:
  - new_bot_kb_book_pages  links (book side or page side)
  - new_bot_kb_pages       (rows with property_id == that property)
  - new_bot_kb_books       (rows with property_id == that property)
  - bot_hotel_images_scraper (rows with property_id == that property)
  - the new_bot_properties row itself

Common / shared rows (property_id IS NULL) are NEVER touched — they belong to
every property under the hotel.

It does NOT delete new_bot_kb_files (the raw uploaded source text): those are
hotel-scoped and one file can bundle several properties' content, so deleting a
file could wipe a property you are KEEPING. Instead it REPORTS which files
mention a removed property so you can prune them by hand (and re-index after).

Run on a machine with DB/VPN access:

    python tools/remove_zone_properties.py            # DRY RUN (read-only)
    python tools/remove_zone_properties.py --apply    # actually delete

Optional:
    --force           proceed even if a matched property is is_default
"""
from __future__ import annotations

import asyncio
import sys
from typing import Any

from sqlalchemy import delete, func, or_, select

from models.database import (
    AsyncSessionLocal,
    Hotel,
    ImageAsset,
    KBBook,
    KBBookPage,
    KBFile,
    KBPage,
    Property,
    engine,
)

HOTEL_CODE = "zone"

# Display name -> match tokens (lowercase substrings tested against the
# property name + aliases + slug). "Parra Goa" uses "parra" so it does NOT
# also catch the surviving Calangute Goa property.
TARGETS: dict[str, list[str]] = {
    "Jaisalmer": ["jaisalmer"],
    "Lansdowne": ["lansdowne"],
    "Parra Goa": ["parra"],
    "Port Blair": ["port blair", "portblair", "port-blair"],
    "Udaipur": ["udaipur"],
    "Digha": ["digha"],
    "Patna": ["patna"],
}


def _haystack(prop: Property) -> str:
    parts: list[str] = [str(prop.name or ""), str(prop.property_slug or "")]
    aliases = prop.aliases if isinstance(prop.aliases, list) else []
    parts.extend(str(a or "") for a in aliases)
    return " | ".join(parts).lower()


def _match_targets(prop: Property) -> list[str]:
    hay = _haystack(prop)
    hit: list[str] = []
    for label, tokens in TARGETS.items():
        if any(tok in hay for tok in tokens):
            hit.append(label)
    return hit


async def _count(session, model, prop_ids: list[int]) -> int:
    res = await session.execute(
        select(func.count()).select_from(model).where(model.property_id.in_(prop_ids))
    )
    return int(res.scalar() or 0)


async def _report_kb_files(session, hotel_id: int) -> None:
    """Read-only: which raw KB files mention a removed property's tokens."""
    files = (
        await session.execute(select(KBFile).where(KBFile.hotel_id == hotel_id))
    ).scalars().all()
    if not files:
        print("  (no new_bot_kb_files rows for this hotel)")
        return

    all_removed_tokens = [t for toks in TARGETS.values() for t in toks]
    surviving_hint = ("calangute", "goa", "coimbatore", "kolkata", "jaipur", "mussoorie")

    flagged = False
    for f in files:
        content = (f.content or "").lower()
        name = (f.original_name or "").lower()
        hit_labels = [
            label
            for label, toks in TARGETS.items()
            if any(t in content or t in name for t in toks)
        ]
        if not hit_labels:
            continue
        flagged = True
        bundled = any(s in content for s in surviving_hint)
        tag = "  <-- BUNDLED (also contains a surviving property; edit, do NOT delete whole file)" if bundled else "  (single-property file; safe to delete)"
        print(f"  - kb_file id={f.id} name={f.original_name!r} mentions: {', '.join(hit_labels)}{tag}")
    if not flagged:
        print("  (no raw KB file mentions any removed property — nothing to prune)")
    _ = all_removed_tokens  # documented intent; tokens already used above


async def main(apply: bool, force: bool) -> int:
    print(f"DB: {engine.url.render_as_string(hide_password=True)}")
    print(f"Target hotel_code: {HOTEL_CODE!r}")
    print("Mode: " + ("APPLY (will DELETE)" if apply else "DRY RUN (no writes)"))
    print("Targets: " + ", ".join(TARGETS.keys()))
    print("=" * 78)

    async with AsyncSessionLocal() as session:
        hotel = (
            await session.execute(select(Hotel).where(Hotel.code == HOTEL_CODE))
        ).scalar_one_or_none()
        if hotel is None:
            print(f"No hotel with code={HOTEL_CODE!r}. Aborting.")
            return 1
        print(f"HOTEL id={hotel.id} code={hotel.code!r} name={hotel.name!r}")

        props = (
            await session.execute(select(Property).where(Property.hotel_id == hotel.id))
        ).scalars().all()
        print(f"Properties under hotel: {len(props)}\n")

        matched: dict[int, tuple[Property, list[str]]] = {}
        target_hits: dict[str, list[str]] = {label: [] for label in TARGETS}
        for p in props:
            labels = _match_targets(p)
            if labels:
                matched[p.id] = (p, labels)
                for label in labels:
                    target_hits[label].append(p.name or p.property_slug or str(p.id))

        # Unmatched / ambiguous targets
        problems = False
        for label, names in target_hits.items():
            if not names:
                print(f"  [WARN] target {label!r} matched NO property (already gone or named differently?)")
                problems = True
            elif len(names) > 1:
                print(f"  [WARN] target {label!r} matched MULTIPLE properties: {names} — review before applying")
                problems = True
        if problems:
            print()

        if not matched:
            print("Nothing to delete. Aborting.")
            return 1

        # Per-property impact
        grand = {"books": 0, "pages": 0, "images": 0}
        default_blocked = False
        print("WILL REMOVE these properties (and their scoped KB):")
        for pid, (p, labels) in sorted(matched.items()):
            books = await _count(session, KBBook, [pid])
            pages = await _count(session, KBPage, [pid])
            images = await _count(session, ImageAsset, [pid])
            grand["books"] += books
            grand["pages"] += pages
            grand["images"] += images
            flags = []
            if p.is_default:
                flags.append("IS_DEFAULT")
                if not force:
                    default_blocked = True
            if not p.is_active:
                flags.append("already inactive")
            flag_s = f"  [{', '.join(flags)}]" if flags else ""
            print(
                f"  - id={pid} slug={p.property_slug!r} name={p.name!r} "
                f"(matched: {', '.join(labels)}) -> books={books} pages={pages} images={images}{flag_s}"
            )
        print(
            f"\n  TOTAL KB rows to delete: books={grand['books']} pages={grand['pages']} "
            f"images={grand['images']} + {len(matched)} property rows"
        )

        print("\nRaw KB source files (new_bot_kb_files) — REVIEW & prune by hand:")
        await _report_kb_files(session, hotel.id)

        if default_blocked:
            print("\n[ABORT] A matched property is is_default. Re-run with --force only if you are sure.")
            return 2

        if not apply:
            print("\nDRY RUN complete. Re-run with --apply to delete.")
            return 0

        # ---- APPLY ----
        ids = list(matched.keys())
        book_ids_subq = select(KBBook.id).where(KBBook.property_id.in_(ids)).scalar_subquery()
        page_ids_subq = select(KBPage.id).where(KBPage.property_id.in_(ids)).scalar_subquery()

        await session.execute(
            delete(KBBookPage).where(
                or_(KBBookPage.book_id.in_(book_ids_subq), KBBookPage.page_id.in_(page_ids_subq))
            )
        )
        await session.execute(delete(KBPage).where(KBPage.property_id.in_(ids)))
        await session.execute(delete(KBBook).where(KBBook.property_id.in_(ids)))
        await session.execute(delete(ImageAsset).where(ImageAsset.property_id.in_(ids)))
        await session.execute(delete(Property).where(Property.id.in_(ids)))
        await session.commit()

        print(f"\nDONE. Deleted {len(ids)} property rows and their scoped KB content.")
        print("NEXT: prune the bundled kb_files above, then RAG-reindex + refresh the")
        print("multi-property welcome message so caches drop the removed properties.")
        return 0


if __name__ == "__main__":
    argv = sys.argv[1:]
    raise SystemExit(asyncio.run(main(apply="--apply" in argv, force="--force" in argv)))
