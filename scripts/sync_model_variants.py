#!/usr/bin/env python
"""
SEED / SYNC (DB WRITE): push the gpt-5.4-mini prompt files into the registry as
per-model variant rows.

Reads:   prompts/models/gpt-5.4-mini/*.md   (frontmatter + body)
Writes:  new_bot_prompt_registry rows scoped to
           hotel_id = <ZONE>, model_variant = 'gpt-5.4-mini', industry = NULL

Behaviour:
  - Idempotent: matches an existing (prompt_key, hotel_id, model_variant) row by
    id and UPDATEs only when the body changed (version bumps); otherwise INSERTs.
  - Never touches the shared/default rows (model_variant IS NULL) — so 4.1 is
    untouched.
  - Files without valid frontmatter `key:` (README, _TUNING_SPEC, …) are skipped.

REQUIRES the model_variant column (run migrate_add_model_variant.py first).
Creds from .env. DB WRITE — run only with permission.

Usage:
    python scripts/sync_model_variants.py                 # apply to Zone (default)
    python scripts/sync_model_variants.py --dry-run
    python scripts/sync_model_variants.py --model gpt-5.4-mini --hotel ZONE
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

import pymysql

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


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
    return pymysql.connect(host=host, port=port, user=user, password=password,
                           database=database, connect_timeout=15, read_timeout=60,
                           autocommit=False)


def _parse(path: Path):
    text = path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None
    fm, body = m.group(1), m.group(2).lstrip("\n")
    if body.endswith("\n"):
        body = body[:-1]
    meta: dict[str, object] = {}
    for line in fm.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, v = line.split(":", 1)
        v = v.strip()
        if v.startswith("[") and v.endswith("]"):
            inner = v[1:-1].strip()
            meta[k.strip().lower()] = [p.strip().strip("'\"") for p in inner.split(",") if p.strip()]
        else:
            meta[k.strip().lower()] = v
    key = str(meta.get("key") or "").strip()
    if not key:
        return None
    return key, body, meta.get("description") or "", meta.get("variables") or []


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="gpt-5.4-mini")
    ap.add_argument("--hotel", default="ZONE")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    folder = ROOT / "prompts" / "models" / args.model
    if not folder.exists():
        raise SystemExit(f"folder not found: {folder}")
    # Baseline = the shared/default (4.1) set. A 5.4 file byte-identical to its 4.1
    # counterpart needs NO variant row — 5.4 falls through to the shared row via the
    # loader's fallback. So we only ever seed the keys that genuinely differ.
    baseline_dir = ROOT / "prompts" / "models" / "gpt-4.1-mini"

    env = _load_env(ENV_FILE)
    conn = _conn(env); cur = conn.cursor()
    try:
        cur.execute("SELECT id FROM new_bot_hotels WHERE code=%s", (args.hotel,))
        r = cur.fetchone()
        if not r:
            raise SystemExit(f"hotel {args.hotel!r} not found")
        hotel_id = int(r[0])
        print(f"target: hotel={args.hotel}(id={hotel_id}) model_variant={args.model!r}\n")

        inserted = updated = unchanged = skipped = 0
        for md in sorted(folder.glob("*.md")):
            parsed = _parse(md)
            if not parsed:
                print(f"  skip (no frontmatter): {md.name}")
                skipped += 1
                continue
            key, body, desc, variables = parsed
            # Skip keys identical to the 4.1 baseline — no variant row needed.
            base_file = baseline_dir / md.name
            if base_file.exists():
                base_parsed = _parse(base_file)
                if base_parsed and base_parsed[1] == body:
                    unchanged += 1
                    print(f"  = same-as-4.1 (no variant needed): {key}")
                    continue
            body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
            cur.execute(
                """SELECT id, content FROM new_bot_prompt_registry
                   WHERE prompt_key=%s AND hotel_id=%s AND model_variant=%s
                     AND industry IS NULL AND is_active=1
                   LIMIT 1""",
                (key, hotel_id, args.model),
            )
            existing = cur.fetchone()
            vars_json = json.dumps(list(variables))
            if existing:
                row_id, cur_content = int(existing[0]), (existing[1] or "")
                if cur_content == body:
                    unchanged += 1
                    print(f"  = unchanged: {key}")
                    continue
                if not args.dry_run:
                    cur.execute(
                        """UPDATE new_bot_prompt_registry
                           SET content=%s, variables=%s, description=%s,
                               version=version+1, updated_by='sync_5.4',
                               seeded_from_file_hash=%s, updated_at=NOW()
                           WHERE id=%s""",
                        (body, vars_json, str(desc), body_hash, row_id),
                    )
                updated += 1
                print(f"  ~ update:    {key}  (id={row_id}, {len(body)} ch)")
            else:
                if not args.dry_run:
                    cur.execute(
                        """INSERT INTO new_bot_prompt_registry
                           (prompt_key, industry, hotel_id, model_variant, content,
                            variables, version, description, updated_by,
                            seeded_from_file_hash, is_active, created_at, updated_at)
                           VALUES (%s, NULL, %s, %s, %s, %s, 1, %s, 'sync_5.4', %s, 1, NOW(), NOW())""",
                        (key, hotel_id, args.model, body, vars_json, str(desc), body_hash),
                    )
                inserted += 1
                print(f"  + insert:    {key}  ({len(body)} ch)")

        if args.dry_run:
            conn.rollback()
            print(f"\n--dry-run (no writes): +{inserted} ~{updated} ={unchanged} skip {skipped}")
        else:
            conn.commit()
            print(f"\ncommitted: +{inserted} inserted, ~{updated} updated, ={unchanged} unchanged, skip {skipped} [OK]")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
