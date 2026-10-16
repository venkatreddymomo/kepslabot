"""
Scrub the 7 deleted Zone properties out of the legacy json_section.* config blobs
in new_bot_business_config (hotel_id=67). These blobs are NOT read by the bot in
DB-first mode, but they still list the removed properties + a stale property_count.

Surgical, deterministic, no LLM:
  - json_section.business: filter multi_property_location_manifest (drop removed
    properties; drop locations that become empty), recompute property_count +
    location_count + each location's property_count.
  - json_section.knowledge_base.library_index: drop documents/pages whose
    source_name is a removed file; drop removed page_ids + removed sources from
    books (drop now-empty books); strip removed city words from book keywords/
    aliases; recompute source_count + coverage.

Dry-run by default.
    python tools/scrub_zone_config_blobs.py            # DRY RUN
    python tools/scrub_zone_config_blobs.py --apply    # write
"""
from __future__ import annotations

import asyncio
import json
import sys

from sqlalchemy import text

from models.database import AsyncSessionLocal, engine

HID = 67
REMOVED_SLUGS = {
    "zone_connect_park_jaisalmer", "zone_connect_park_lansdowne",
    "zone_connect_park_parra_goa", "zone_connect_park_patna",
    "zone_connect_park_port_blair", "zone_connect_park_udaipur", "zone_park_digha",
}
REMOVED_FILES = {
    "zone_by_the_park_digha_kb.txt", "zone_connect_by_the_park_jaisalmer_kb.txt",
    "zone_connect_by_the_park_lansdowne_kb.txt", "zone_connect_by_the_park_parra_goa_kb.txt",
    "zone_connect_by_the_park_patna_kb.txt", "zone_connect_by_the_park_port_blair_kb.txt",
    "zone_connect_by_the_park_udaipur_kb.txt",
}
# words safe to strip from keyword bags (NOT 'goa'/'calangute' — those survive)
REMOVED_WORDS = ["jaisalmer", "lansdowne", "parra", "port blair", "port_blair", "udaipur", "digha", "patna", "blair"]


def _removed_hits(obj) -> int:
    s = json.dumps(obj, ensure_ascii=False).lower()
    return sum(s.count(w) for w in REMOVED_WORDS)


def scrub_business(b: dict) -> dict:
    manifest = b.get("multi_property_location_manifest") or []
    new_manifest = []
    for loc in manifest:
        props = [p for p in (loc.get("properties") or []) if p.get("id") not in REMOVED_SLUGS]
        if not props:
            continue
        loc["properties"] = props
        loc["property_ids"] = [p.get("id") for p in props]
        loc["property_names"] = [p.get("name") for p in props]
        loc["property_count"] = len(props)
        new_manifest.append(loc)
    b["multi_property_location_manifest"] = new_manifest
    b["property_count"] = sum(l["property_count"] for l in new_manifest)
    b["location_count"] = len(new_manifest)
    return b


def scrub_kb(kb: dict) -> dict:
    li = kb.get("library_index") or {}
    removed_page_ids = {p.get("id") for p in (li.get("pages") or []) if p.get("source_name") in REMOVED_FILES}
    li["documents"] = [d for d in (li.get("documents") or []) if d.get("source_name") not in REMOVED_FILES]
    li["pages"] = [p for p in (li.get("pages") or []) if p.get("source_name") not in REMOVED_FILES]

    new_books = []
    for bk in (li.get("books") or []):
        bk["page_ids"] = [pid for pid in (bk.get("page_ids") or []) if pid not in removed_page_ids]
        bk["sources"] = [s for s in (bk.get("sources") or []) if s not in REMOVED_FILES]
        bk["keywords"] = [k for k in (bk.get("keywords") or []) if str(k).lower() not in REMOVED_WORDS]
        bk["aliases"] = [a for a in (bk.get("aliases") or []) if str(a).lower() not in REMOVED_WORDS]
        bk["page_count"] = len(bk["page_ids"])
        if bk["page_count"] > 0:
            new_books.append(bk)
    li["books"] = new_books
    li["source_count"] = len(li["documents"])

    cov = li.get("coverage")
    if isinstance(cov, dict):
        total = len(li["pages"])
        covered_ids = set()
        for bk in new_books:
            covered_ids.update(bk.get("page_ids") or [])
        covered = len([p for p in li["pages"] if p.get("id") in covered_ids])
        cov.update(total_pages=total, covered_pages=covered, uncovered_pages=total - covered,
                   coverage_ratio=round(covered / total, 4) if total else 0)
    kb["library_index"] = li
    return kb


async def _load(s, key):
    row = (await s.execute(text(
        "SELECT config_value FROM new_bot_business_config WHERE hotel_id=:h AND config_key=:k"
    ), {"h": HID, "k": key})).first()
    return json.loads(row[0]) if row and row[0] else None


async def _save(s, key, obj):
    await s.execute(text(
        "UPDATE new_bot_business_config SET config_value=:v WHERE hotel_id=:h AND config_key=:k"
    ), {"v": json.dumps(obj, ensure_ascii=False), "h": HID, "k": key})


async def main(apply: bool) -> int:
    print("Mode:", "APPLY" if apply else "DRY RUN")
    async with AsyncSessionLocal() as s:
        biz = await _load(s, "json_section.business")
        kb = await _load(s, "json_section.knowledge_base")

        print("\n[business] BEFORE: property_count=%s location_count=%s removed-hits=%s"
              % (biz.get("property_count"), biz.get("location_count"), _removed_hits(biz)))
        print("  multi_property_welcome_message:", repr((biz.get("multi_property_welcome_message") or "")[:160]))
        scrub_business(biz)
        print("[business] AFTER:  property_count=%s location_count=%s removed-hits=%s"
              % (biz.get("property_count"), biz.get("location_count"), _removed_hits(biz)))

        li0 = kb.get("library_index", {})
        print("\n[knowledge_base] BEFORE: docs=%s pages=%s books=%s source_count=%s removed-hits=%s"
              % (len(li0.get("documents") or []), len(li0.get("pages") or []), len(li0.get("books") or []),
                 li0.get("source_count"), _removed_hits(kb)))
        scrub_kb(kb)
        li1 = kb["library_index"]
        print("[knowledge_base] AFTER:  docs=%s pages=%s books=%s source_count=%s removed-hits=%s"
              % (len(li1.get("documents") or []), len(li1.get("pages") or []), len(li1.get("books") or []),
                 li1.get("source_count"), _removed_hits(kb)))

        if not apply:
            print("\nDRY RUN — re-run with --apply to write.")
            return 0
        await _save(s, "json_section.business", biz)
        await _save(s, "json_section.knowledge_base", kb)
        await s.commit()
        print("\nDONE — both blobs updated.")
    await engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(apply="--apply" in sys.argv[1:])))
