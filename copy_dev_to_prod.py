"""
Copy the 12 bot CONFIG tables from DEV to PROD, ids preserved.

    python copy_dev_to_prod.py            # dry run - shows what it would do
    python copy_dev_to_prod.py --apply    # actually copies

WHAT IT COPIES
    Only the 12 config tables. Never conversations, messages, tickets,
    sessions, guests, bookings or any log table.

SAFETY
    - Copies in foreign-key-safe order, so FK checks stay ON the whole time.
    - Primary keys are preserved exactly (dev hotel ids are 54,65,66,67,68,69,70
      - if they got renumbered, every child row would attach to the wrong hotel).
    - Refuses to touch a table that already has rows, unless you pass --force.
      So a re-run resumes where it stopped rather than duplicating.
    - Verifies the row count after every table and stops on the first mismatch.
    - Nothing is deleted. Ever.

CREDENTIALS
    Dev comes from .env (MYSQL_HOST / MYSQL_PORT / MYSQL_USER / MYSQL_PASSWORD).
    Prod uses the SAME user and password against PROD_HOST below.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

PROD_HOST = "10.100.5.153"
PROD_PORT = 3306
PROD_DB = "GHN_PROD_BAK"
DEV_DB = "GHN_PROD_BAK"

# foreign-key-safe order. do not reorder.
TABLES = [
    ("new_bot_hotels",             7),
    ("new_bot_properties",        74),
    ("new_bot_business_config",   65),
    ("new_bot_services",          26),
    ("new_bot_prompt_registry",   76),
    ("new_bot_kb_files",          73),
    ("new_bot_kb_books",        1557),
    ("new_bot_kb_pages",        2221),
    ("new_bot_kb_book_pages",   3521),
    ("bot_hotel_images_scraper",2547),
    ("new_bot_location_intros",   45),
    ("new_bot_widget_deployments", 2),
]

CHUNK = 200


def load_env() -> dict:
    env = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def main() -> int:
    apply = "--apply" in sys.argv
    force = "--force" in sys.argv

    env = load_env()
    import pymysql

    dev = pymysql.connect(
        host=env["MYSQL_HOST"], port=int(env["MYSQL_PORT"]),
        user=env["MYSQL_USER"], password=env["MYSQL_PASSWORD"],
        database=DEV_DB, connect_timeout=30, charset="utf8mb4")
    prod = pymysql.connect(
        host=PROD_HOST, port=PROD_PORT,
        user=env["MYSQL_USER"], password=env["MYSQL_PASSWORD"],
        database=PROD_DB, connect_timeout=30, charset="utf8mb4",
        autocommit=False)

    dc, pc = dev.cursor(), prod.cursor()

    dc.execute("SELECT @@hostname"); dev_host = dc.fetchone()[0]
    pc.execute("SELECT @@hostname"); prod_host = pc.fetchone()[0]
    print(f"  DEV  {env['MYSQL_HOST']}  ({dev_host})")
    print(f"  PROD {PROD_HOST}  ({prod_host})")
    if dev_host == prod_host:
        print("\n  ABORT: dev and prod resolve to the SAME server.")
        return 1
    print(f"\n  mode: {'APPLY' if apply else 'DRY RUN'}\n")
    print(f"  {'TABLE':<28}{'DEV':>7}{'PROD':>7}   ACTION")
    print("  " + "-" * 62)

    plan = []
    for name, expected in TABLES:
        dc.execute(f"SELECT COUNT(*) FROM `{name}`")
        n_dev = dc.fetchone()[0]
        pc.execute(f"SELECT COUNT(*) FROM `{name}`")
        n_prod = pc.fetchone()[0]

        if n_dev != expected:
            action = f"WARN dev has {n_dev}, expected {expected}"
        elif n_prod == n_dev:
            action = "already done - skip"
        elif n_prod > 0 and not force:
            action = f"SKIP - prod not empty ({n_prod}). use --force"
        else:
            action = f"copy {n_dev} rows"
            plan.append((name, n_dev))
        print(f"  {name:<28}{n_dev:>7}{n_prod:>7}   {action}")

    if not plan:
        print("\n  nothing to do.")
        return 0
    if not apply:
        print(f"\n  DRY RUN. {len(plan)} table(s) would be copied.")
        print("  re-run with --apply to do it.")
        return 0

    print()
    for name, n_dev in plan:
        dc.execute(f"SELECT * FROM `{name}`")
        cols = [d[0] for d in dc.description]
        collist = ", ".join(f"`{c}`" for c in cols)
        holders = ", ".join(["%s"] * len(cols))
        sql = f"INSERT INTO `{name}` ({collist}) VALUES ({holders})"

        done = 0
        while True:
            rows = dc.fetchmany(CHUNK)
            if not rows:
                break
            pc.executemany(sql, rows)
            done += len(rows)
            print(f"    {name:<28} {done}/{n_dev}", end="\r", flush=True)
        prod.commit()

        pc.execute(f"SELECT COUNT(*) FROM `{name}`")
        got = pc.fetchone()[0]
        ok = got == n_dev
        print(f"    {name:<28} {got}/{n_dev}   {'OK' if ok else '*** MISMATCH ***'}")
        if not ok:
            print("\n  STOPPING - count mismatch. Nothing further was copied.")
            return 1

    print("\n  all tables copied.\n")

    pc.execute("SELECT id, code, name FROM new_bot_hotels ORDER BY id")
    print("  hotel ids in prod (must be 54,65,66,67,68,69,70 - NOT 1-7):")
    for r in pc.fetchall():
        flag = "  <-- RENUMBERED" if r[0] < 50 else ""
        print(f"    {r[0]:>4}  {r[1]:<32} {r[2]}{flag}")

    print("\n  orphan check (every count must be 0):")
    checks = [
        ("properties with no hotel",
         "SELECT COUNT(*) FROM new_bot_properties p LEFT JOIN new_bot_hotels h"
         " ON h.id=p.hotel_id WHERE h.id IS NULL"),
        ("services with no hotel",
         "SELECT COUNT(*) FROM new_bot_services s LEFT JOIN new_bot_hotels h"
         " ON h.id=s.hotel_id WHERE h.id IS NULL"),
        ("kb_books with no hotel",
         "SELECT COUNT(*) FROM new_bot_kb_books b LEFT JOIN new_bot_hotels h"
         " ON h.id=b.hotel_id WHERE h.id IS NULL"),
        ("kb_pages with no hotel",
         "SELECT COUNT(*) FROM new_bot_kb_pages g LEFT JOIN new_bot_hotels h"
         " ON h.id=g.hotel_id WHERE h.id IS NULL"),
        ("book_pages with a dead book",
         "SELECT COUNT(*) FROM new_bot_kb_book_pages bp LEFT JOIN new_bot_kb_books b"
         " ON b.id=bp.book_id WHERE b.id IS NULL"),
        ("book_pages with a dead page",
         "SELECT COUNT(*) FROM new_bot_kb_book_pages bp LEFT JOIN new_bot_kb_pages g"
         " ON g.id=bp.page_id WHERE g.id IS NULL"),
        ("images with no hotel",
         "SELECT COUNT(*) FROM bot_hotel_images_scraper i LEFT JOIN new_bot_hotels h"
         " ON h.id=i.hotel_id WHERE h.id IS NULL"),
    ]
    bad = 0
    for label, q in checks:
        pc.execute(q)
        n = pc.fetchone()[0]
        bad += n
        print(f"    {label:<32} {n}")

    print("\n  prompt lock (both must be 1):")
    pc.execute("SELECT service_id, generated_system_prompt_override"
               " FROM new_bot_services WHERE hotel_id=67")
    for sid, ov in pc.fetchall():
        print(f"    {sid:<30} {ov}{'' if ov == 1 else '   <-- LOCK LOST'}")

    print("\n  " + ("ALL CHECKS PASSED" if bad == 0 else f"*** {bad} ORPHANS ***"))
    print("\n  NEXT: edit new_bot_widget_deployments.allowed_origins to prod domains.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
