"""
Surgically remove ONE Zone sub-property (default: Katra) and nothing else.

Targets the property by EXACT slug match (+ its own source file name), so no other
property that merely mentions the place in its text is affected.

Removes, for that one property:
  - new_bot_kb_book_pages / new_bot_kb_pages / new_bot_kb_books (property-scoped)
  - bot_hotel_images_scraper (property-scoped)
  - the new_bot_properties row
  - its raw new_bot_kb_files source(s)  [so a future reindex can't recreate it]
  - its entries in json_section.knowledge_base.library_index (by source_name)
  - its location entry in json_section.business.multi_property_location_manifest
    and recomputes property_count / location_count

Leaves every OTHER property + all services/widget/prompts/config untouched.

    python tools/remove_zone_property.py            # DRY RUN
    python tools/remove_zone_property.py --apply
"""
from __future__ import annotations

import asyncio
import json
import sys

from sqlalchemy import delete, func, select, text

from models.database import (
    AsyncSessionLocal, Hotel, ImageAsset, KBBook, KBFile, KBPage, Property, engine,
)

HOTEL_CODE = "zone"
TARGET_SLUG = "zone_connect_park_katra"   # EXACT slug of the property to remove


async def _scalar(s, q, p=None):
    return int((await s.execute(text(q), p or {})).scalar() or 0)


def _recompute_business(biz: dict, target_slug: str) -> dict:
    man = biz.get("multi_property_location_manifest") or []
    new_man = []
    for loc in man:
        props = [p for p in (loc.get("properties") or []) if p.get("id") != target_slug]
        if not props:
            continue
        loc["properties"] = props
        loc["property_ids"] = [p.get("id") for p in props]
        loc["property_names"] = [p.get("name") for p in props]
        loc["property_count"] = len(props)
        new_man.append(loc)
    biz["multi_property_location_manifest"] = new_man
    biz["property_count"] = sum(l["property_count"] for l in new_man)
    biz["location_count"] = len(new_man)
    return biz


def _scrub_library(kb: dict, source_names: set[str]) -> dict:
    li = kb.get("library_index") or {}
    removed_page_ids = {p.get("id") for p in (li.get("pages") or []) if p.get("source_name") in source_names}
    li["documents"] = [d for d in (li.get("documents") or []) if d.get("source_name") not in source_names]
    li["pages"] = [p for p in (li.get("pages") or []) if p.get("source_name") not in source_names]
    new_books = []
    for bk in (li.get("books") or []):
        bk["page_ids"] = [pid for pid in (bk.get("page_ids") or []) if pid not in removed_page_ids]
        bk["sources"] = [s for s in (bk.get("sources") or []) if s not in source_names]
        bk["page_count"] = len(bk["page_ids"])
        if bk["page_count"] > 0:
            new_books.append(bk)
    li["books"] = new_books
    li["source_count"] = len(li["documents"])
    cov = li.get("coverage")
    if isinstance(cov, dict):
        total = len(li["pages"])
        covered = len({pid for bk in new_books for pid in (bk.get("page_ids") or [])})
        cov.update(total_pages=total, covered_pages=covered,
                   uncovered_pages=max(total - covered, 0),
                   coverage_ratio=round(covered / total, 4) if total else 0)
    kb["library_index"] = li
    return kb


async def _blob(s, hid, key):
    row = (await s.execute(text(
        "SELECT config_value FROM new_bot_business_config WHERE hotel_id=:h AND config_key=:k"
    ), {"h": hid, "k": key})).first()
    return json.loads(row[0]) if row and row[0] else None


async def _save(s, hid, key, obj):
    await s.execute(text(
        "UPDATE new_bot_business_config SET config_value=:v WHERE hotel_id=:h AND config_key=:k"
    ), {"v": json.dumps(obj, ensure_ascii=False), "h": hid, "k": key})


async def main(apply: bool) -> int:
    print("Mode:", "APPLY" if apply else "DRY RUN", "| target slug:", TARGET_SLUG)
    async with AsyncSessionLocal() as s:
        hotel = (await s.execute(select(Hotel).where(func.lower(Hotel.code) == HOTEL_CODE))).scalar_one_or_none()
        hid = hotel.id
        prop = (await s.execute(
            select(Property).where(Property.hotel_id == hid, Property.property_slug == TARGET_SLUG)
        )).scalar_one_or_none()
        if prop is None:
            print(f"No property with slug {TARGET_SLUG!r} under hotel {hid}. Aborting.")
            return 1
        pid = prop.id
        total_props = await _scalar(s, "SELECT COUNT(*) FROM new_bot_properties WHERE hotel_id=:h", {"h": hid})

        # source file names that this property's pages reference (for library_index scrub)
        src_rows = (await s.execute(text(
            "SELECT DISTINCT source_name FROM new_bot_kb_pages WHERE property_id=:p AND source_name IS NOT NULL"
        ), {"p": pid})).all()
        source_names = {r[0] for r in src_rows if r[0]}
        # matching raw kb_files (single-property files named for this property)
        if source_names:
            kbfiles = (await s.execute(
                select(KBFile.id, KBFile.original_name).where(
                    KBFile.hotel_id == hid, KBFile.original_name.in_(list(source_names))
                )
            )).all()
        else:
            kbfiles = (await s.execute(
                select(KBFile.id, KBFile.original_name).where(
                    KBFile.hotel_id == hid, func.lower(KBFile.original_name).like("%katra%")
                )
            )).all()

        nbooks = await _scalar(s, "SELECT COUNT(*) FROM new_bot_kb_books WHERE property_id=:p", {"p": pid})
        npages = await _scalar(s, "SELECT COUNT(*) FROM new_bot_kb_pages WHERE property_id=:p", {"p": pid})
        nimg = await _scalar(s, "SELECT COUNT(*) FROM bot_hotel_images_scraper WHERE property_id=:p", {"p": pid})

        print(f"\nProperty: id={pid} slug={prop.property_slug!r} name={prop.name!r}")
        print(f"  hotel has {total_props} properties -> will become {total_props - 1}")
        print(f"  delete: books={nbooks} pages={npages} images={nimg}")
        print(f"  source_names: {source_names}")
        print(f"  raw kb_files to delete: {[(f[0], f[1]) for f in kbfiles]}")

        if not apply:
            print("\nDRY RUN — re-run with --apply.")
            return 0

        await s.execute(text(
            "DELETE FROM new_bot_kb_book_pages WHERE book_id IN (SELECT id FROM new_bot_kb_books WHERE property_id=:p) "
            "OR page_id IN (SELECT id FROM new_bot_kb_pages WHERE property_id=:p)"), {"p": pid})
        await s.execute(delete(KBPage).where(KBPage.property_id == pid))
        await s.execute(delete(KBBook).where(KBBook.property_id == pid))
        await s.execute(delete(ImageAsset).where(ImageAsset.property_id == pid))
        await s.execute(delete(Property).where(Property.id == pid))
        if kbfiles:
            await s.execute(delete(KBFile).where(KBFile.id.in_([f[0] for f in kbfiles])))

        biz = await _blob(s, hid, "json_section.business")
        if biz:
            await _save(s, hid, "json_section.business", _recompute_business(biz, TARGET_SLUG))
        kb = await _blob(s, hid, "json_section.knowledge_base")
        if kb and source_names:
            await _save(s, hid, "json_section.knowledge_base", _scrub_library(kb, source_names))
        await s.commit()

        left = await _scalar(s, "SELECT COUNT(*) FROM new_bot_properties WHERE hotel_id=:h", {"h": hid})
        biz2 = await _blob(s, hid, "json_section.business")
        print(f"\nDONE. properties now={left}, business.property_count={biz2.get('property_count') if biz2 else '?'}")
    await engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(apply="--apply" in sys.argv[1:])))
