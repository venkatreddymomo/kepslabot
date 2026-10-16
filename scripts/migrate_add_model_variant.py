#!/usr/bin/env python
"""
MIGRATION (DB WRITE): add `model_variant` to new_bot_prompt_registry.

Idempotent and safe to re-run:
  - Adds the column only if it does not already exist.
  - Rebuilds the unique scope key to include model_variant only if needed.

This is the ONLY schema change the per-model prompt system needs. Run it BEFORE
restarting the backend with the model-aware loader. Until it runs, the loader
degrades gracefully to the shared/default rows (bot keeps working on 4.1 prompts).

Creds are read from .env (MYSQL_* / DATABASE_URL). Nothing else is modified.

Usage:
    python scripts/migrate_add_model_variant.py            # apply
    python scripts/migrate_add_model_variant.py --dry-run  # show plan only
"""
from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

import pymysql

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
TABLE = "new_bot_prompt_registry"
COLUMN = "model_variant"
UNIQUE_KEY = "uq_prompt_scope"


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
    host = env.get("MYSQL_HOST"); port = int(env.get("MYSQL_PORT", "3306") or 3306)
    user = env.get("MYSQL_USER"); password = env.get("MYSQL_PASSWORD")
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


def _column_exists(cur, db) -> bool:
    cur.execute(
        """SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_NAME=%s""",
        (db, TABLE, COLUMN),
    )
    return int(cur.fetchone()[0]) > 0


def _unique_key_columns(cur, db) -> list[str]:
    cur.execute(
        """SELECT COLUMN_NAME FROM information_schema.STATISTICS
           WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND INDEX_NAME=%s
           ORDER BY SEQ_IN_INDEX""",
        (db, TABLE, UNIQUE_KEY),
    )
    return [r[0] for r in cur.fetchall()]


def main() -> None:
    dry = "--dry-run" in sys.argv
    env = _load_env(ENV_FILE)
    db = env.get("MYSQL_DATABASE") or (
        urlparse(env.get("DATABASE_URL", "").replace("mysql+aiomysql", "mysql")).path.lstrip("/")
    )
    conn = _conn(env); cur = conn.cursor()
    try:
        has_col = _column_exists(cur, db)
        key_cols = _unique_key_columns(cur, db)
        print(f"DB={db} table={TABLE}")
        print(f"  column {COLUMN!r} exists: {has_col}")
        print(f"  {UNIQUE_KEY} columns: {key_cols}")

        plan: list[str] = []
        if not has_col:
            plan.append(
                f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} VARCHAR(64) NULL AFTER hotel_id"
            )
        if key_cols and COLUMN not in key_cols:
            plan.append(f"ALTER TABLE {TABLE} DROP INDEX {UNIQUE_KEY}")
            plan.append(
                f"ALTER TABLE {TABLE} ADD UNIQUE KEY {UNIQUE_KEY} "
                f"(prompt_key, industry, hotel_id, {COLUMN})"
            )
        elif not key_cols:
            plan.append(
                f"ALTER TABLE {TABLE} ADD UNIQUE KEY {UNIQUE_KEY} "
                f"(prompt_key, industry, hotel_id, {COLUMN})"
            )

        if not plan:
            print("\nNothing to do — schema already migrated. [OK]")
            return

        print("\nPlanned statements:")
        for s in plan:
            print("  " + s + ";")

        if dry:
            print("\n--dry-run: no changes applied.")
            return

        for s in plan:
            cur.execute(s)
        conn.commit()
        print("\nMigration applied and committed. [OK]")

        # Verify
        assert _column_exists(cur, db), "post-check: column still missing"
        assert COLUMN in _unique_key_columns(cur, db), "post-check: unique key not updated"
        print("Post-migration verification passed.")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
