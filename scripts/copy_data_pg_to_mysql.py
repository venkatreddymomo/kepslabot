"""
Script: Copy data from PostgreSQL → MySQL for all bot tables.

Reverse of scripts/copy_data.py. Use this when migrating from PG back to MySQL.

Run AFTER scripts/create_mysql_tables.py (so target tables exist).

Behaviour:
- Copies in FK dependency order (hotels first, leaf tables last).
- Skips new_bot_messages by design (table exists, runtime fills it).
- Converts PG JSONB → MySQL JSON string (json.dumps).
- Converts PG TIMESTAMPTZ → naive UTC for MySQL DATETIME.
- Idempotent at row level: uses INSERT IGNORE so reruns don't create dupes.
- After insert, runs ALTER TABLE AUTO_INCREMENT = max(id)+1 so future inserts
  don't collide with copied IDs.

Usage:
    python scripts/copy_data_pg_to_mysql.py
    python scripts/copy_data_pg_to_mysql.py --tables new_bot_hotels new_bot_services
    python scripts/copy_data_pg_to_mysql.py --truncate     # wipe target rows first (DANGEROUS)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def load_env() -> None:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        print(f"ERROR: .env file not found at {env_path}")
        sys.exit(1)
    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env()

try:
    import pymysql
    import pymysql.cursors
except ImportError:
    print("ERROR: pymysql not installed. Run: pip install pymysql")
    sys.exit(1)

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    print("ERROR: psycopg2 not installed. Run: pip install psycopg2-binary")
    sys.exit(1)


# ── Credentials ──────────────────────────────────────────────────────────────
MY_HOST     = os.environ.get("MYSQL_HOST", "127.0.0.1")
MY_PORT     = int(os.environ.get("MYSQL_PORT", "3306"))
MY_USER     = os.environ.get("MYSQL_USER", "root")
MY_PASSWORD = os.environ.get("MYSQL_PASSWORD", "")
MY_DATABASE = os.environ.get("MYSQL_DATABASE", "")

PG_HOST     = os.environ.get("PG_HOST", "127.0.0.1")
PG_PORT     = int(os.environ.get("PG_PORT", "5432"))
PG_DATABASE = os.environ.get("PG_DATABASE", "")
PG_USER     = os.environ.get("PG_USER", "")
PG_PASSWORD = os.environ.get("PG_PASSWORD", "")
PG_SCHEMA   = os.environ.get("PG_SCHEMA", "guest_chatbot")

if not (MY_DATABASE and PG_DATABASE):
    print("ERROR: MYSQL_DATABASE and PG_DATABASE must both be set in .env")
    sys.exit(1)

BATCH_SIZE = 500


# ── Tables to copy, in FK dependency order ──────────────────────────────────
# new_bot_messages is intentionally excluded (table created in DDL, runtime fills).
TABLES_TO_COPY = [
    "new_bot_hotels",
    "new_bot_restaurants",
    "new_bot_menu_items",
    "new_bot_guests",
    "new_bot_bookings",
    "new_bot_orders",
    "new_bot_order_items",
    "new_bot_conversations",
    "new_bot_business_config",
    "new_bot_capabilities",
    "new_bot_intents",
    "new_bot_services",
    "new_bot_prompt_registry",
    "new_bot_kb_files",
    "bot_hotel_images_scraper",
    # Page indexing tables (preserve them so book routing keeps working) ───
    "new_bot_properties",
    "new_bot_kb_books",
    "new_bot_kb_pages",
    "new_bot_kb_book_pages",
    "scrape_jobs",
]

# Columns whose Python value (dict/list) needs to be JSON-encoded for MySQL.
JSON_COLUMNS = {
    "new_bot_conversations":     {"pending_data"},
    "new_bot_services":          {"form_config", "service_prompt_pack"},
    "new_bot_prompt_registry":   {"variables"},
    "bot_hotel_images_scraper":  {"tags"},
    "new_bot_properties":        {"aliases", "location", "source_names"},
    "new_bot_kb_books":          {"aliases", "keywords", "source_names"},
    "new_bot_kb_pages":          {"topics"},
}

# Tables whose primary key is NOT 'id' or that don't use AUTO_INCREMENT —
# skip the AUTO_INCREMENT realignment step for these.
NON_AUTOINCREMENT_TABLES = {
    "new_bot_kb_book_pages",  # composite PK (book_id, page_id)
    "scrape_jobs",            # VARCHAR(36) PK
}


def fix_value(table: str, col: str, val):
    """Convert a PG cell value into something pymysql will accept for MySQL."""
    if val is None:
        return None

    # JSONB → JSON string. psycopg2 returns dict/list directly; MySQL JSON
    # column accepts a JSON-encoded string.
    if col in JSON_COLUMNS.get(table, set()):
        if isinstance(val, (dict, list)):
            return json.dumps(val, ensure_ascii=False)
        if isinstance(val, str):
            # Already a JSON string — validate it parses, then pass through.
            try:
                json.loads(val)
                return val
            except (json.JSONDecodeError, ValueError):
                return None
        return json.dumps(val, ensure_ascii=False)

    # PG TIMESTAMPTZ → MySQL DATETIME (no tz). Convert to UTC then drop tz.
    if isinstance(val, datetime):
        if val.tzinfo is not None:
            val = val.astimezone(timezone.utc).replace(tzinfo=None)
        return val

    # Postgres bool → MySQL TINYINT (Python bool works fine via pymysql).
    if isinstance(val, bool):
        return 1 if val else 0

    # Memoryview / bytes from PG bytea → str.
    if isinstance(val, (bytes, bytearray, memoryview)):
        try:
            return bytes(val).decode("utf-8", errors="replace")
        except Exception:
            return str(val)

    return val


def fetch_columns(pg_cur, table: str) -> list[str]:
    """Return ordered column list for a PG table inside the configured schema."""
    pg_cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = %s
          AND table_name = %s
        ORDER BY ordinal_position
        """,
        (PG_SCHEMA, table),
    )
    return [row[0] for row in pg_cur.fetchall()]


def copy_table(pg_cur, my_conn, my_cur, table: str) -> tuple[int, int]:
    """Copy all rows from one PG table into MySQL. Returns (pg_count, inserted)."""
    pg_cur.execute(f'SELECT COUNT(*) FROM {PG_SCHEMA}."{table}"')
    total = int(pg_cur.fetchone()[0] or 0)
    if total == 0:
        return 0, 0

    columns = fetch_columns(pg_cur, table)
    if not columns:
        print(f"    no columns found for {table} — skipping")
        return total, 0

    backtick_cols = ", ".join(f"`{c}`" for c in columns)
    placeholders = ", ".join(["%s"] * len(columns))
    insert_sql = (
        f"INSERT IGNORE INTO `{table}` ({backtick_cols}) "
        f"VALUES ({placeholders})"
    )

    inserted = 0
    offset = 0
    errors = 0

    while offset < total:
        col_select = ", ".join(f'"{c}"' for c in columns)
        pg_cur.execute(
            f'SELECT {col_select} FROM {PG_SCHEMA}."{table}" '
            f'ORDER BY 1 LIMIT {BATCH_SIZE} OFFSET {offset}'
        )
        rows = pg_cur.fetchall()
        if not rows:
            break

        batch = [
            tuple(fix_value(table, columns[i], row[i]) for i in range(len(columns)))
            for row in rows
        ]

        try:
            my_cur.executemany(insert_sql, batch)
            my_conn.commit()
            inserted += my_cur.rowcount  # MySQL reports actually-inserted rows
        except Exception as e:
            my_conn.rollback()
            print(f"\n    Batch error at offset {offset}: {e}")
            # Row-by-row fallback so one bad row doesn't abort the whole table.
            for row_values in batch:
                try:
                    my_cur.execute(insert_sql, row_values)
                    my_conn.commit()
                    inserted += my_cur.rowcount
                except Exception as row_err:
                    my_conn.rollback()
                    errors += 1
                    if errors <= 3:
                        print(f"    Row error: {row_err} | {str(row_values)[:120]}")

        offset += BATCH_SIZE
        pct = min(100, int(offset / total * 100))
        print(f"\r    {min(offset, total)}/{total} rows ({pct}%)", end="", flush=True)

    print()
    return total, inserted


def realign_autoincrement(my_cur, my_conn, table: str) -> None:
    """Set AUTO_INCREMENT past max(id) so future inserts don't collide."""
    if table in NON_AUTOINCREMENT_TABLES:
        return
    try:
        my_cur.execute(f"SELECT COALESCE(MAX(id), 0) + 1 FROM `{table}`")
        next_id = int(my_cur.fetchone()[0] or 1)
        my_cur.execute(f"ALTER TABLE `{table}` AUTO_INCREMENT = {next_id}")
        my_conn.commit()
    except Exception as e:
        print(f"    auto_increment realign failed for {table}: {e}")


def truncate_target(my_cur, my_conn, tables: list[str]) -> None:
    """DESTRUCTIVE: delete all rows from given tables (FK-safe order)."""
    print("WARNING: --truncate requested. Wiping target tables first.")
    try:
        my_cur.execute("SET FOREIGN_KEY_CHECKS=0")
        for table in reversed(tables):  # leaf-first
            try:
                my_cur.execute(f"TRUNCATE TABLE `{table}`")
                print(f"  truncated {table}")
            except Exception as e:
                print(f"  truncate failed for {table}: {e}")
        my_cur.execute("SET FOREIGN_KEY_CHECKS=1")
        my_conn.commit()
    except Exception as e:
        print(f"truncate setup failed: {e}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tables", nargs="*", default=None, help="Only copy these tables (subset of TABLES_TO_COPY).")
    parser.add_argument("--truncate", action="store_true", help="DESTRUCTIVE: wipe target tables before copying.")
    args = parser.parse_args()

    selected = args.tables or TABLES_TO_COPY
    unknown = [t for t in selected if t not in TABLES_TO_COPY]
    if unknown:
        print(f"ERROR: unknown table names: {unknown}")
        print(f"Allowed: {TABLES_TO_COPY}")
        return 2

    print("=" * 64)
    print("COPY DATA: PostgreSQL → MySQL")
    print(f"Postgres:  {PG_HOST}:{PG_PORT}/{PG_DATABASE} (schema={PG_SCHEMA})")
    print(f"MySQL:     {MY_HOST}:{MY_PORT}/{MY_DATABASE}")
    print(f"Tables:    {len(selected)} of {len(TABLES_TO_COPY)}")
    print(f"Skipping:  new_bot_messages (table exists, runtime fills it)")
    print("=" * 64)

    # ── Connect Postgres ───────────────────────────────────────────────────
    try:
        pg_conn = psycopg2.connect(
            host=PG_HOST, port=PG_PORT, dbname=PG_DATABASE,
            user=PG_USER, password=PG_PASSWORD,
            connect_timeout=15,
            options=f"-c search_path={PG_SCHEMA}",
        )
        pg_cur = pg_conn.cursor()
        print("Connected to PostgreSQL OK")
    except Exception as e:
        print(f"ERROR: cannot connect to PostgreSQL — {e}")
        return 1

    # ── Connect MySQL ──────────────────────────────────────────────────────
    try:
        my_conn = pymysql.connect(
            host=MY_HOST, port=MY_PORT,
            user=MY_USER, password=MY_PASSWORD,
            database=MY_DATABASE,
            charset="utf8mb4",
            connect_timeout=15,
        )
        my_cur = my_conn.cursor()
        print("Connected to MySQL OK\n")
    except Exception as e:
        print(f"ERROR: cannot connect to MySQL — {e}")
        pg_conn.close()
        return 1

    if args.truncate:
        truncate_target(my_cur, my_conn, selected)

    # ── Copy each table ────────────────────────────────────────────────────
    results: list[tuple[str, int, int, str | None]] = []
    for i, table in enumerate(selected, 1):
        label = f"[{i:02d}/{len(selected)}]"
        print(f"{label} {table}")
        try:
            pg_count, my_inserted = copy_table(pg_cur, my_conn, my_cur, table)
            realign_autoincrement(my_cur, my_conn, table)
            results.append((table, pg_count, my_inserted, None))
            print(f"    Done — {my_inserted} inserted (PG had: {pg_count})\n")
        except Exception as e:
            try:
                my_conn.rollback()
            except Exception:
                pass
            results.append((table, 0, 0, str(e)))
            print(f"    FAILED — {e}\n")

    pg_cur.close()
    pg_conn.close()
    my_cur.close()
    my_conn.close()

    # ── Summary ────────────────────────────────────────────────────────────
    print("=" * 64)
    print("MIGRATION SUMMARY")
    print("=" * 64)
    print(f"{'Table':<32} {'PG':>8} {'Inserted':>10}  Status")
    print("-" * 64)
    total_pg = total_inserted = 0
    errors: list[str] = []
    for table, pc, mi, err in results:
        status = "OK" if err is None else "ERROR"
        print(f"{table:<32} {pc:>8} {mi:>10}  {status}")
        if err:
            errors.append(f"  {table}: {err}")
        total_pg += pc
        total_inserted += mi
    print("-" * 64)
    print(f"{'TOTAL':<32} {total_pg:>8} {total_inserted:>10}")
    if errors:
        print("\nErrors:")
        for line in errors:
            print(line)
        return 1
    print("\nAll tables copied successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
