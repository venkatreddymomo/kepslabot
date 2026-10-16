"""Seed the pre-stored location-intro answers used by the dropdown short-circuit.

What this does (idempotent, reversible):
  1. CREATE TABLE IF NOT EXISTS new_bot_location_intros
  2. Parse intromessage.txt into one curated "# Bot" body per city
  3. Resolve each city -> location_id (same slug the runtime manifest uses)
     and cross-check it against the live property catalog (so a typo can't
     silently seed an intro no dropdown entry will ever match)
  4. UPSERT one row per (hotel_id, location_id)

Credentials are read from .env (DATABASE_URL) — nothing hardcoded here, so the
file is safe to keep in the tree. Never committed by this script.

Usage:
    python tools/seed_location_intros.py            # create + seed (default)
    python tools/seed_location_intros.py --dry-run  # parse + validate only
    python tools/seed_location_intros.py --drop      # DROP the table (rollback)

Reversible: --drop removes the table entirely; re-run without it to rebuild.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import os
import re
import sys
from pathlib import Path

import aiomysql

ROOT = Path(__file__).resolve().parents[1]
INTRO_FILE = ROOT / "intromessage.txt"


def _normalize_slug(value: str) -> str:
    """Mirror config_service.ConfigService._normalize_slug exactly."""
    lowered = str(value or "").strip().lower()
    return re.sub(r"[^a-z0-9]+", "_", lowered).strip("_")


def _load_db_dsn() -> dict:
    """Parse DATABASE_URL from .env into aiomysql connect kwargs."""
    env_path = ROOT / ".env"
    dsn = ""
    if env_path.exists():
        for line in io.open(env_path, encoding="utf-8"):
            line = line.strip()
            if line.startswith("DATABASE_URL=") and "mysql" in line:
                dsn = line.split("=", 1)[1].strip()
                break
    dsn = os.environ.get("DATABASE_URL", dsn)
    # mysql+aiomysql://user:pass@host:port/db
    m = re.match(
        r"^\w+\+\w+://(?P<user>[^:]+):(?P<pw>[^@]+)@(?P<host>[^:/]+):(?P<port>\d+)/(?P<db>[^?]+)",
        dsn,
    )
    if not m:
        print("ERROR: could not parse a mysql DATABASE_URL from .env", file=sys.stderr)
        sys.exit(2)
    return {
        "host": m.group("host"),
        "port": int(m.group("port")),
        "user": m.group("user"),
        "password": m.group("pw"),
        "db": m.group("db"),
    }


def parse_intro_sections(text: str) -> list[tuple[str, str]]:
    """Return [(city, body), ...] in file order.

    Each block is:  '# User\\n\\nI'm exploring {City}. ...\\n\\n# Bot\\n\\n{body}'
    Body runs until the next '# User' header (or EOF).
    """
    # Split on top-level headers while keeping them.
    parts = re.split(r"(?m)^# (User|Bot)\s*$", text)
    # parts = [pre, 'User', userblock, 'Bot', botblock, 'User', ...]
    sections: list[tuple[str, str]] = []
    i = 1
    pending_city = ""
    while i < len(parts) - 1:
        header = parts[i].strip()
        block = parts[i + 1]
        if header == "User":
            m = re.search(r"I'm exploring\s+(.+?)\.", block)
            pending_city = m.group(1).strip() if m else ""
        elif header == "Bot" and pending_city:
            body = block.strip()
            sections.append((pending_city, body))
            pending_city = ""
        i += 2
    return sections


CREATE_SQL = """
CREATE TABLE IF NOT EXISTS new_bot_location_intros (
    id             INT AUTO_INCREMENT PRIMARY KEY,
    hotel_id       INT NOT NULL,
    location_id    VARCHAR(100) NOT NULL,
    location_label VARCHAR(255) NOT NULL,
    body           MEDIUMTEXT NOT NULL,
    is_active      TINYINT(1) NOT NULL DEFAULT 1,
    created_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_hotel_location (hotel_id, location_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

UPSERT_SQL = """
INSERT INTO new_bot_location_intros (hotel_id, location_id, location_label, body, is_active)
VALUES (%s, %s, %s, %s, 1)
ON DUPLICATE KEY UPDATE
    location_label = VALUES(location_label),
    body           = VALUES(body),
    is_active      = 1,
    updated_at     = CURRENT_TIMESTAMP
"""


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="parse + validate only, no writes")
    ap.add_argument("--drop", action="store_true", help="DROP the intros table and exit")
    ap.add_argument("--hotel-id", type=int, default=67, help="Zone hotel_id (default 67)")
    args = ap.parse_args()

    cfg = _load_db_dsn()
    conn = await aiomysql.connect(charset="utf8mb4", autocommit=True, connect_timeout=20, **cfg)
    cur = await conn.cursor(aiomysql.DictCursor)

    if args.drop:
        await cur.execute("DROP TABLE IF EXISTS new_bot_location_intros")
        print("DROPPED table new_bot_location_intros")
        await cur.close(); conn.close()
        return

    # 1) live property catalog for this hotel -> city -> location_id + property slugs
    await cur.execute(
        "SELECT property_slug, name, location FROM new_bot_properties "
        "WHERE hotel_id=%s AND is_active=1",
        (args.hotel_id,),
    )
    import json
    live_cities: dict[str, dict] = {}
    for r in await cur.fetchall():
        loc = r["location"]
        loc = json.loads(loc) if isinstance(loc, str) else (loc or {})
        city = (loc or {}).get("city") if isinstance(loc, dict) else ""
        if not city:
            continue
        lid = _normalize_slug(city)
        live_cities.setdefault(lid, {"label": city, "slugs": []})
        live_cities[lid]["slugs"].append(r["property_slug"])
    print(f"live cities for hotel_id={args.hotel_id}: {len(live_cities)}")

    # 2) parse the file
    text = io.open(INTRO_FILE, encoding="utf-8").read()
    sections = parse_intro_sections(text)
    print(f"parsed intro sections from file: {len(sections)}")

    # 3) validate + prepare rows
    rows: list[tuple[int, str, str, str]] = []
    problems: list[str] = []
    for city, body in sections:
        lid = _normalize_slug(city)
        if lid not in live_cities:
            problems.append(f"  ! '{city}' (id={lid}) has NO matching live property city")
        if not body.strip():
            problems.append(f"  ! '{city}' has empty body")
            continue
        rows.append((args.hotel_id, lid, city, body))

    print("\n{:<16} {:<16} {:>6} {}".format("city", "location_id", "chars", "property_slugs"))
    for _, lid, label, body in rows:
        slugs = ",".join(live_cities.get(lid, {}).get("slugs", [])) or "-"
        print("{:<16} {:<16} {:>6} {}".format(label, lid, len(body), slugs))

    if problems:
        print("\nVALIDATION ISSUES:")
        print("\n".join(problems))

    # every live city should have an intro
    missing = sorted(set(live_cities) - {lid for _, lid, _, _ in rows})
    if missing:
        print(f"\nWARNING: live cities with NO intro: {missing}")

    if args.dry_run:
        print("\n--dry-run: no writes performed.")
        await cur.close(); conn.close()
        return

    # 4) create + upsert
    await cur.execute(CREATE_SQL)
    print("\nensured table new_bot_location_intros")
    n = 0
    for row in rows:
        await cur.execute(UPSERT_SQL, row)
        n += 1
    print(f"upserted {n} intro rows for hotel_id={args.hotel_id}")

    await cur.execute(
        "SELECT COUNT(*) AS c FROM new_bot_location_intros WHERE hotel_id=%s AND is_active=1",
        (args.hotel_id,),
    )
    print("active rows now:", (await cur.fetchone())["c"])
    await cur.close(); conn.close()


if __name__ == "__main__":
    asyncio.run(main())
