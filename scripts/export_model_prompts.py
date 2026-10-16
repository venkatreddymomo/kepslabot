#!/usr/bin/env python
"""
Export the prompt registry to versioned local files (READ-ONLY on the DB).

Purpose
-------
Safekeeping + the source-of-truth files for the per-model prompt system.
This script NEVER writes to the database. It only SELECTs and writes local files.

What it produces
----------------
1. backups/model_prompts/registry_full_<date>.json
   A complete snapshot of EVERY active row (industry defaults + all hotel
   overrides) — the exact restore point for the whole registry.

2. prompts/models/gpt-4.1-mini/<key>.md
   Zone's live effective rows (hotel_id = ZONE) written as re-seedable
   markdown (frontmatter + body). This is the "4.1 folder": the known-good
   prompt set the bot uses today. These files are for git safekeeping —
   they are NOT auto-seeded (4.1 keeps reading the existing NULL-variant /
   hotel rows untouched).

3. prompts/models/gpt-5.4-mini/<key>.md
   Baseline COPIES of the same Zone rows, so that when the 5.4 variant rows
   are seeded they start identical to 4.1 (zero behaviour change), and can
   then be edited independently for the 5.4 dialect.

Credentials are read from .env (MYSQL_* / DATABASE_URL). No creds are
hard-coded in this file.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path
from urllib.parse import unquote, urlparse

import pymysql

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"

# The hotel whose effective prompts we snapshot as the "live" set.
ZONE_HOTEL_CODE = "ZONE"

# Only these keys are behaviour-critical enough to keep as per-model files.
# Everything else is still captured in the full JSON snapshot (restore point).
MODEL_PROMPT_KEYS = [
    "orchestrator.main_router",
    "orchestrator.book_answerer",
    "orchestrator.next_suggestions",
    "orchestrator.service_router",
    "orchestrator.continuity_router",
    "orchestrator.answer_first_guard",
    "orchestrator.kb_chunk_scan",
]


def _load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _db_conn(env: dict[str, str]) -> pymysql.connections.Connection:
    host = env.get("MYSQL_HOST")
    port = int(env.get("MYSQL_PORT", "3306") or 3306)
    user = env.get("MYSQL_USER")
    password = env.get("MYSQL_PASSWORD")
    database = env.get("MYSQL_DATABASE")
    # Fall back to DATABASE_URL if MYSQL_* not present.
    if not host and env.get("DATABASE_URL"):
        u = urlparse(env["DATABASE_URL"].replace("mysql+aiomysql", "mysql"))
        host, port = u.hostname, (u.port or 3306)
        user, password = unquote(u.username or ""), unquote(u.password or "")
        database = (u.path or "/").lstrip("/")
    if not all([host, user, database]):
        raise SystemExit("Could not resolve DB creds from .env (MYSQL_* or DATABASE_URL).")
    return pymysql.connect(
        host=host, port=port, user=user, password=password, database=database,
        connect_timeout=15, read_timeout=120, charset="utf8mb4",
    )


def _slug_safe(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", key)


def _frontmatter(key: str, description: str | None, variables: list[str] | None) -> str:
    vars_str = "[" + ", ".join(variables or []) + "]"
    desc = (description or "").replace("\n", " ").strip()
    return f"---\nkey: {key}\ndescription: {desc}\nvariables: {vars_str}\n---\n"


def main() -> None:
    env = _load_env(ENV_FILE)
    conn = _db_conn(env)
    cur = conn.cursor(pymysql.cursors.DictCursor)

    # Resolve Zone hotel id.
    cur.execute("SELECT id, code FROM new_bot_hotels WHERE code = %s", (ZONE_HOTEL_CODE,))
    zone_row = cur.fetchone()
    if not zone_row:
        raise SystemExit(f"Hotel code {ZONE_HOTEL_CODE!r} not found.")
    zone_id = int(zone_row["id"])
    print(f"Zone hotel id = {zone_id}")

    # ---- 1. Full snapshot (every active row) ----
    cur.execute(
        """SELECT id, prompt_key, industry, hotel_id, content, variables, version,
                  description, updated_by, seeded_from_file_hash, is_active,
                  created_at, updated_at
           FROM new_bot_prompt_registry
           WHERE is_active = 1
           ORDER BY prompt_key, hotel_id"""
    )
    all_rows = cur.fetchall()
    for r in all_rows:
        for k in ("created_at", "updated_at"):
            if r.get(k) is not None:
                r[k] = str(r[k])

    today = date.today().isoformat()
    backup_dir = ROOT / "backups" / "model_prompts"
    backup_dir.mkdir(parents=True, exist_ok=True)
    snap_path = backup_dir / f"registry_full_{today}.json"
    snap_path.write_text(
        json.dumps({"exported_at": today, "zone_hotel_id": zone_id, "rows": all_rows},
                   ensure_ascii=False, indent=1, default=str),
        encoding="utf-8",
    )
    print(f"[1] full snapshot: {len(all_rows)} rows -> {snap_path.relative_to(ROOT)}")

    # ---- 2 + 3. Per-key files: Zone's effective row for each key ----
    # Effective = hotel-67 override if present, else industry default (hotel NULL).
    by_key_zone: dict[str, dict] = {}
    by_key_industry: dict[str, dict] = {}
    for r in all_rows:
        if r["hotel_id"] == zone_id:
            by_key_zone[r["prompt_key"]] = r
        elif r["hotel_id"] is None:
            by_key_industry.setdefault(r["prompt_key"], r)

    dir_41 = ROOT / "prompts" / "models" / "gpt-4.1-mini"
    dir_54 = ROOT / "prompts" / "models" / "gpt-5.4-mini"
    dir_41.mkdir(parents=True, exist_ok=True)
    dir_54.mkdir(parents=True, exist_ok=True)

    written = 0
    for key in MODEL_PROMPT_KEYS:
        row = by_key_zone.get(key) or by_key_industry.get(key)
        if not row:
            print(f"    - {key}: no row found, skipped")
            continue
        variables = row.get("variables")
        if isinstance(variables, str):
            try:
                variables = json.loads(variables)
            except Exception:
                variables = []
        if not isinstance(variables, list):
            variables = []
        content = str(row.get("content") or "")
        source = "hotel_zone" if row.get("hotel_id") == zone_id else "industry_default"
        body = _frontmatter(key, row.get("description"), variables) + content + "\n"

        # 4.1 folder is pure safekeeping — always refresh it.
        (dir_41 / f"{_slug_safe(key)}.md").write_text(body, encoding="utf-8")
        # 5.4 folder: only seed a baseline copy when no (possibly tuned) file exists,
        # so re-running export never clobbers hand-tuned 5.4 prompts.
        f54 = dir_54 / f"{_slug_safe(key)}.md"
        note54 = "5.4 baseline written"
        if f54.exists():
            note54 = "5.4 exists — kept (not overwritten)"
        else:
            f54.write_text(body, encoding="utf-8")
        written += 1
        print(f"    - {key}: {len(content):>6} ch  (source={source})  | 4.1 refreshed | {note54}")

    conn.close()
    print(f"[2] 4.1 folder: {written} files -> {dir_41.relative_to(ROOT)}")
    print(f"[3] 5.4 folder (baseline copies): {written} files -> {dir_54.relative_to(ROOT)}")
    print("\nDONE. No database writes were performed (SELECT-only).")


if __name__ == "__main__":
    sys.exit(main())
