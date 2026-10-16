"""Vision-classify Zone's image gallery so images can be picked without keywords.

Why: all 714 rows in bot_hotel_images_scraper for Zone (hotel 67) have
category=NULL, tags=NULL, description='' and machine-generated titles
("sample43", "Zone Connect Calangute Goa a8f18d7eef"). The chat image picker
only attaches an image when its TITLE appears verbatim in the bot's reply, so
with titles like these it almost never fires — and there is no category data to
order by either.

This is a ONE-TIME OFFLINE backfill: it looks at each image and writes a real
`category`, `description` and `tags`. After it runs, image selection at chat
time is a plain ordered DB read (facade first, then rooms) — no keyword
matching against the reply text, and no vision calls on the hot path.

Usage:
    python tools/classify_zone_images.py --dry-run --limit 8
    python tools/classify_zone_images.py --limit 50
    python tools/classify_zone_images.py                 # everything unclassified
    python tools/classify_zone_images.py --reset         # clear categories again
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
from datetime import datetime

import httpx
import pymysql

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HOTEL_ID = 67
BATCH_SIZE = 6
# The hotel's own image host is slow (8-70s per file), so the bottleneck is
# downloading, not the model. Run plenty of batches in parallel.
CONCURRENCY = 10

# Ordered by how useful each is as the FIRST image a guest sees for a property.
CATEGORIES = [
    "facade",       # exterior / building / entrance — the establishing shot
    "room",         # guest rooms and suites
    "dining",       # restaurants, bars, cafes, food
    "pool",
    "spa",
    "fitness",
    "banquet",      # event spaces, halls, lawns, meeting rooms
    "lobby",        # lobby, reception, common areas
    "kids",
    "other",        # logos, collateral, anything not guest-facing
]

DB = dict(
    host=os.getenv("MYSQL_HOST", "172.16.5.32"),
    port=int(os.getenv("MYSQL_PORT", "3306")),
    user=os.getenv("MYSQL_USER", "root"),
    password=os.getenv("MYSQL_PASSWORD", "CHANGE_ME"),
    database=os.getenv("MYSQL_DATABASE", "GHN_PROD_BAK"),
    charset="utf8mb4",
    connect_timeout=20,
)

SYSTEM_PROMPT = (
    "You label hotel photographs for a guest-facing chat widget.\n"
    "For EACH image you are given, return one object with:\n"
    '  "id": the id you were given for that image\n'
    f'  "category": exactly one of {CATEGORIES}\n'
    '  "title": a short human title a guest would recognise, 2-5 words '
    '(e.g. "Deluxe Room", "Rooftop Pool", "Hotel Exterior")\n'
    '  "description": one plain sentence describing what is visible, 8-20 words\n'
    '  "room_type": for category="room" only, the room name if it is legible in '
    "the image, else empty string\n"
    "Rules:\n"
    "- Judge ONLY what is visible. Never guess a facility from the filename.\n"
    "- 'facade' means the building seen from outside, the entrance or the "
    "driveway — not an interior.\n"
    "- Logos, banners, marketing collateral, maps, screenshots and blank or "
    "broken images are category 'other'.\n"
    "- If an image could be two things, pick the one a guest would search for.\n"
    'Return STRICT JSON only: {"images": [ ... ]}'
)


def fetch_rows(cur, *, limit: int, redo: bool) -> list[dict]:
    where = "hotel_id=%s AND is_active=1"
    if not redo:
        where += " AND (category IS NULL OR category='')"
    sql = f"SELECT id, title, image_url FROM bot_hotel_images_scraper WHERE {where} ORDER BY id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    cur.execute(sql, (HOTEL_ID,))
    return list(cur.fetchall())


def _display_url(stored: str) -> str:
    """Stored image_url may be an `s3://` pointer, not a fetchable URL.

    The chat UI never sees the raw value — it goes through
    s3_storage_service.resolve_display_url(), which presigns s3:// and passes
    http(s) through. Using the raw column here meant every S3-backed image
    failed with UnsupportedProtocol (that was all 7 images for New Market
    Kolkata, so that property showed none).
    """
    try:
        from services.s3_storage_service import s3_storage_service

        return s3_storage_service.resolve_display_url(stored) or stored
    except Exception:
        return stored


async def _fetch_data_url(http, url: str) -> str:
    """Download an image and return it as a base64 data URL.

    We hand OpenAI the BYTES rather than the URL: zonebythepark.com is slow
    enough that OpenAI's own fetcher times out ("Timeout while downloading"),
    which failed every batch. Fetching here also lets us skip dead links
    cleanly instead of losing the whole batch to one bad URL.
    """
    try:
        response = await http.get(url, timeout=75.0, follow_redirects=True)
        response.raise_for_status()
        blob = response.content
    except Exception:
        return ""
    if not blob or len(blob) > 12_000_000:
        return ""
    mime = (response.headers.get("content-type") or "image/jpeg").split(";")[0].strip()
    if not mime.startswith("image/"):
        mime = "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(blob).decode('ascii')}"


async def classify_batch(client, http, rows: list[dict]) -> list[dict]:
    usable: list[tuple[dict, str]] = []
    for row in rows:
        data_url = await _fetch_data_url(http, _display_url(str(row["image_url"] or "")))
        if data_url:
            usable.append((row, data_url))
    if not usable:
        print(f"    -- batch {rows[0]['id']}-{rows[-1]['id']}: no downloadable images, skipped")
        return []

    content: list[dict] = [
        {
            "type": "text",
            "text": "Label these images. Ids in order: "
            + ", ".join(str(r["id"]) for r, _ in usable),
        }
    ]
    for row, data_url in usable:
        content.append({"type": "text", "text": f'id={row["id"]}'})
        content.append({"type": "image_url", "image_url": {"url": data_url, "detail": "low"}})
    try:
        response = await client.chat.completions.create(
            model=os.getenv("VISION_MODEL", "gpt-4.1-mini"),
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
            temperature=0.0,
            max_tokens=900,
            response_format={"type": "json_object"},
        )
        payload = json.loads(response.choices[0].message.content or "{}")
    except Exception as exc:
        print(f"    !! batch {rows[0]['id']}-{rows[-1]['id']} failed: {type(exc).__name__}: {exc}")
        return []
    images = payload.get("images")
    return images if isinstance(images, list) else []


async def run(rows: list[dict], *, dry_run: bool) -> list[dict]:
    from openai import AsyncOpenAI

    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        for line in open(
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"),
            encoding="utf-8",
        ):
            if line.startswith("OPENAI_API_KEY="):
                api_key = line.split("=", 1)[1].strip()
                break
    if not api_key:
        print("!! no OPENAI_API_KEY")
        return []

    client = AsyncOpenAI(api_key=api_key)
    batches = [rows[i : i + BATCH_SIZE] for i in range(0, len(rows), BATCH_SIZE)]
    semaphore = asyncio.Semaphore(CONCURRENCY)
    results: list[dict] = []
    done = 0

    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0 (compatible; kepsla-image-labeller/1.0)"}
    ) as http:

        async def worker(batch: list[dict]) -> None:
            nonlocal done
            async with semaphore:
                out = await classify_batch(client, http, batch)
                results.extend(out)
                done += 1
                if done % 5 == 0 or done == len(batches):
                    print(f"    {done}/{len(batches)} batches, {len(results)} labels")

        await asyncio.gather(*(worker(b) for b in batches))
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--redo", action="store_true", help="re-label already-labelled rows")
    ap.add_argument("--reset", action="store_true", help="clear category/description and exit")
    ap.add_argument(
        "--keep-titles",
        action="store_true",
        help="never overwrite the existing title (for rows whose titles were authored by hand)",
    )
    args = ap.parse_args()

    conn = pymysql.connect(**DB)
    cur = conn.cursor(pymysql.cursors.DictCursor)

    if args.reset:
        cur.execute(
            "UPDATE bot_hotel_images_scraper SET category=NULL, tags=NULL "
            "WHERE hotel_id=%s",
            (HOTEL_ID,),
        )
        conn.commit()
        print(f"cleared categories for {cur.rowcount} rows")
        return 0

    rows = fetch_rows(cur, limit=args.limit, redo=args.redo)
    print(f"{len(rows)} images to classify  (started {datetime.now():%H:%M:%S})")
    if not rows:
        return 0

    labels = asyncio.run(run(rows, dry_run=args.dry_run))
    by_id = {int(r["id"]): r for r in rows}
    applied = 0
    counts: dict[str, int] = {}

    for label in labels:
        try:
            image_id = int(label.get("id"))
        except Exception:
            continue
        if image_id not in by_id:
            continue
        category = str(label.get("category") or "").strip().lower()
        if category not in CATEGORIES:
            category = "other"
        title = str(label.get("title") or "").strip()[:250]
        description = str(label.get("description") or "").strip()[:900]
        room_type = str(label.get("room_type") or "").strip()
        tags = [t for t in [category, room_type.lower()] if t]
        counts[category] = counts.get(category, 0) + 1

        if args.dry_run:
            print(f"    {image_id:6} {category:9} {title[:32]:34} {description[:60]}")
            continue

        # Keep the original scraped title when it is meaningful; the generated
        # ones are hashes, so prefer the vision title for those.
        old_title = str(by_id[image_id].get("title") or "")
        # --keep-titles: rows added through the Content Manager already carry a
        # hand-written title ("Junior Suite Balcony", "Vitalia Spa"). Those are
        # better than anything the model invents, and the title-matching image
        # path keys on them, so overwriting would lose real signal.
        use_title = old_title if args.keep_titles else (title or old_title)
        cur.execute(
            "UPDATE bot_hotel_images_scraper "
            "SET category=%s, description=%s, tags=%s, title=%s WHERE id=%s",
            (category, description, json.dumps(tags), use_title, image_id),
        )
        applied += 1

    if not args.dry_run:
        conn.commit()
    print(f"\ncategories: {dict(sorted(counts.items(), key=lambda kv: -kv[1]))}")
    print(f"{'would apply' if args.dry_run else 'applied'} {applied or len(labels)} labels  ({datetime.now():%H:%M:%S})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
