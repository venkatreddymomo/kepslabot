#!/usr/bin/env python
"""
MIGRATION (DB WRITE): add `model` to new_bot_widget_deployments.

Purpose: pin a customer embed to one chat model. Once a deployment row carries a
model id, every guest turn on that embed runs that model AND that model's prompt
variant, and the /chat harness dropdown can no longer move it. NULL = unpinned
(the historical behaviour: follow the harness override / env default).

Idempotent and safe to re-run: adds the column only if it does not already exist.
Purely additive — no existing column, row or value is touched, and every row
starts NULL, i.e. exactly today's behaviour. The running backend ignores an
unknown column, so this can be applied BEFORE the restart (and should be:
widget_deployment_service degrades to "nobody is pinned" if the app comes up
first, but that means customers stay exposed to the dropdown until the restart).

Creds are read from .env (MYSQL_* / DATABASE_URL). Nothing else is modified.

Usage:
    python scripts/migrate_add_widget_model.py --dry-run  # show plan only
    python scripts/migrate_add_widget_model.py            # apply

Rollback:
    ALTER TABLE new_bot_widget_deployments DROP COLUMN model;
  (or just clear it: UPDATE new_bot_widget_deployments SET model=NULL;
   the column is inert when no row is pinned)
"""
from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

import pymysql

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
TABLE = "new_bot_widget_deployments"
COLUMN = "model"
# Mirrors models.database.WidgetDeployment.model
COLUMN_DDL = "VARCHAR(64) NULL DEFAULT NULL"
AFTER_COLUMN = "idle_timeout_minutes"


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


def main() -> None:
    dry = "--dry-run" in sys.argv
    env = _load_env(ENV_FILE)
    db = env.get("MYSQL_DATABASE") or (
        urlparse(env.get("DATABASE_URL", "").replace("mysql+aiomysql", "mysql")).path.lstrip("/")
    )
    conn = _conn(env); cur = conn.cursor()
    try:
        has_col = _column_exists(cur, db)
        print(f"DB={db} table={TABLE}")
        print(f"  column {COLUMN!r} exists: {has_col}")

        cur.execute(f"SELECT COUNT(*) FROM {TABLE}")
        print(f"  existing deployment rows: {int(cur.fetchone()[0])}")

        if has_col:
            cur.execute(
                f"SELECT widget_key, hotel_code, status, {COLUMN} FROM {TABLE} ORDER BY id"
            )
            print("  current pins:")
            for wk, hc, st, model in cur.fetchall():
                print(f"    {wk}  hotel={hc}  status={st}  model={model or '(unpinned)'}")

        plan: list[str] = []
        if not has_col:
            plan.append(
                f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} {COLUMN_DDL} AFTER {AFTER_COLUMN}"
            )

        if not plan:
            print("\nNothing to do — schema already up to date.")
            return

        print("\nPlan:")
        for stmt in plan:
            print(f"  {stmt};")

        if dry:
            print("\n--dry-run: nothing applied.")
            return

        for stmt in plan:
            cur.execute(stmt)
        conn.commit()
        print("\nApplied. Every row starts model=NULL (unpinned) = today's behaviour.")
        print("Next: restart the backend, then set the pin per deployment in Admin > Widget.")
    finally:
        cur.close(); conn.close()


if __name__ == "__main__":
    main()
