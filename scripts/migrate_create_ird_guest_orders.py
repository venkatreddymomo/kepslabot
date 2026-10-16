#!/usr/bin/env python
"""
MIGRATION (DB WRITE): create `new_bot_ird_guest_orders` - the guest's order book.

Ten order slots per stay-session, each with its own status, timestamp and ticket
ref. This becomes the ONE copy of an in-room dining order: both the guest summary
and the kitchen ticket are printed from it, so they can no longer disagree. See
services/ird_cart_book.py for the rules and why this exists.

Idempotent and safe to re-run:
  - Creates the table only if it does not already exist.
  - Adds any missing slot column to an existing table (so the slot count can be
    raised later without a rebuild).
  - Never drops or rewrites a column, and never touches another table.

WRITES ONLY TO new_bot_*. The GHN tables are read-only to this application; guest
identity is COPIED out of them and never written back.

Creds are read from .env (MYSQL_* / DATABASE_URL).

Usage:
    python scripts/migrate_create_ird_guest_orders.py --dry-run   # show the plan
    python scripts/migrate_create_ird_guest_orders.py             # apply
"""
from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

import pymysql

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
TABLE = "new_bot_ird_guest_orders"
SLOT_COUNT = 10

# The four columns each slot needs. Kept here rather than imported so the
# migration can run against a checkout whose app code has moved on.
SLOT_COLUMNS = {
    "cart_%d": "JSON NULL",
    "status_%d": "VARCHAR(20) NULL",
    "placed_at_%d": "DATETIME NULL",
    "ticket_ref_%d": "VARCHAR(50) NULL",
}

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS {table} (
  id              INT NOT NULL AUTO_INCREMENT,
  hotel_code      VARCHAR(50)  NOT NULL,
  session_id      VARCHAR(100) NOT NULL,
  conversation_id VARCHAR(100) NULL,
  guest_name      VARCHAR(255) NULL,
  room_number     VARCHAR(20)  NULL,
  check_in        DATE NULL,
  check_out       DATE NULL,
{slots}
  created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                  ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_ird_orders_session (hotel_code, session_id),
  KEY idx_ird_orders_room (hotel_code, room_number),
  KEY idx_ird_orders_updated (updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
""".strip()


def _load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _conn(env: dict[str, str]):
    host = env.get("MYSQL_HOST")
    port = int(env.get("MYSQL_PORT", "3306") or 3306)
    user = env.get("MYSQL_USER")
    password = env.get("MYSQL_PASSWORD")
    database = env.get("MYSQL_DATABASE")
    if not host and env.get("DATABASE_URL"):
        u = urlparse(env["DATABASE_URL"].replace("mysql+aiomysql", "mysql"))
        host, port = u.hostname, (u.port or 3306)
        user, password = unquote(u.username or ""), unquote(u.password or "")
        database = (u.path or "/").lstrip("/")
    if not all([host, user, database]):
        raise SystemExit("Could not resolve DB creds from .env")
    return pymysql.connect(host=host, port=port, user=user, password=password,
                           database=database, connect_timeout=15, read_timeout=60,
                           autocommit=False)


def _slot_ddl() -> str:
    lines: list[str] = []
    for n in range(1, SLOT_COUNT + 1):
        for tmpl, coltype in SLOT_COLUMNS.items():
            lines.append("  %-16s %s," % (tmpl % n, coltype))
    return "\n".join(lines)


def _table_exists(cur, db: str) -> bool:
    cur.execute(
        "SELECT COUNT(*) FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s", (db, TABLE))
    return int(cur.fetchone()[0]) > 0


def _existing_columns(cur, db: str) -> set[str]:
    cur.execute(
        "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s", (db, TABLE))
    return {r[0] for r in cur.fetchall()}


def main() -> int:
    dry = "--dry-run" in sys.argv
    env = _load_env(ENV_FILE)
    db = env.get("MYSQL_DATABASE") or urlparse(
        env.get("DATABASE_URL", "").replace("mysql+aiomysql", "mysql")).path.lstrip("/")

    conn = _conn(env)
    cur = conn.cursor()
    try:
        exists = _table_exists(cur, db)
        print("DB=%s table=%s exists=%s" % (db, TABLE, exists))

        plan: list[str] = []
        if not exists:
            plan.append(CREATE_SQL.format(table=TABLE, slots=_slot_ddl()))
        else:
            have = _existing_columns(cur, db)
            for n in range(1, SLOT_COUNT + 1):
                for tmpl, coltype in SLOT_COLUMNS.items():
                    col = tmpl % n
                    if col not in have:
                        plan.append("ALTER TABLE %s ADD COLUMN %s %s"
                                    % (TABLE, col, coltype))
            print("  existing columns: %d" % len(have))

        if not plan:
            print("nothing to do - already up to date")
            return 0

        print("\nPLAN (%d statement(s)):" % len(plan))
        for sql in plan:
            print("  " + sql.replace("\n", "\n  "))

        if dry:
            print("\n--dry-run: nothing was written")
            return 0

        for sql in plan:
            cur.execute(sql)
        conn.commit()

        # Prove it landed rather than trusting the commit.
        if not _table_exists(cur, db):
            print("VERIFY FAILED: table still missing")
            return 1
        have = _existing_columns(cur, db)
        missing = [tmpl % n for n in range(1, SLOT_COUNT + 1)
                   for tmpl in SLOT_COLUMNS if (tmpl % n) not in have]
        if missing:
            print("VERIFY FAILED: missing %s" % missing)
            return 1
        print("\napplied. %s now has %d columns (%d slots)."
              % (TABLE, len(have), SLOT_COUNT))
        print("VERIFIED OK")
        return 0
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
