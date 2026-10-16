"""
One-time migration: rewrite every `new_bot_services.form_config` row into the
GHN-aligned shape.

WHY:
    Older rows store fields as `{ "validation_prompt": "..." }` and lack the
    top-level `type` / `title` keys. New rows (saved through the updated
    admin UI) use `{ "validation": { "message": "..." } }` plus
    `{ "type": "form", "title": ... }`. The runtime accepts both shapes, but
    we want every row to look identical in the DB so a future "read forms
    straight from GHN_CHATBOT_FORMS" plan is a drop-in.

WHAT IT DOES:
    For each service with form_config NOT NULL:
      1. Parses the JSON
      2. Rewrites every field: copies `validation_prompt` -> `validation.message`
         (drops the old key). Other field keys (id, label, type, required,
         placeholder, options) are preserved.
      3. Adds top-level `type: "form"` (idempotent — sets only if missing)
      4. Adds top-level `title` from the service name (idempotent — sets only
         if missing or blank)
      5. Leaves Kebo-only keys alone (`lumira_form_key`, `lumira_entity_id`,
         `trigger_field`, `pre_form_instructions`)
    Idempotent: re-running it on an already-migrated row is a no-op and
    nothing is rewritten. The script reports which rows were touched.

USAGE:
    DRY RUN (default — prints what would change, writes nothing):
        python scripts/migrate_form_config_to_ghn_shape.py

    APPLY (actually writes the changes):
        python scripts/migrate_form_config_to_ghn_shape.py --apply

REQUIREMENTS:
    Same DB env as the running bot (reads `database_url` from settings).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

# Allow running from repo root: `python scripts/...py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from models.database import AsyncSessionLocal  # noqa: E402


def _migrate_field(field: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Return (new_field, changed)."""
    if not isinstance(field, dict):
        return field, False
    changed = False
    new_field: dict[str, Any] = {}
    # Copy through every existing key except the legacy one we're rewriting.
    for k, v in field.items():
        if k == "validation_prompt":
            continue
        new_field[k] = v
    # Rewrite validation_prompt → validation.message (only if not already in new shape).
    legacy_msg = str(field.get("validation_prompt") or "").strip()
    existing_validation = field.get("validation") if isinstance(field.get("validation"), dict) else None
    existing_msg = ""
    if existing_validation:
        existing_msg = str(existing_validation.get("message") or "").strip()
    if existing_msg:
        # Already in new shape — nothing to do.
        new_field["validation"] = {"message": existing_msg}
        if "validation_prompt" in field:
            changed = True  # we dropped the dead legacy key
    elif legacy_msg:
        new_field["validation"] = {"message": legacy_msg}
        changed = True
    return new_field, changed


def _migrate_form_config(cfg: dict[str, Any], service_name: str) -> tuple[dict[str, Any], bool]:
    """Return (new_form_config, changed)."""
    if not isinstance(cfg, dict):
        return cfg, False
    changed = False
    new_cfg = dict(cfg)

    # Top-level: type/title.
    if str(new_cfg.get("type") or "").strip().lower() != "form":
        new_cfg["type"] = "form"
        changed = True
    current_title = str(new_cfg.get("title") or "").strip()
    if not current_title:
        new_cfg["title"] = str(service_name or "").strip()
        changed = True

    # Field-level: validation rewrite.
    fields = new_cfg.get("fields")
    if isinstance(fields, list):
        rewritten: list[dict[str, Any]] = []
        any_field_changed = False
        for f in fields:
            nf, fc = _migrate_field(f)
            rewritten.append(nf)
            any_field_changed = any_field_changed or fc
        if any_field_changed:
            new_cfg["fields"] = rewritten
            changed = True

    return new_cfg, changed


async def main(apply: bool) -> int:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                "SELECT id, name, form_config "
                "FROM new_bot_services "
                "WHERE form_config IS NOT NULL"
            )
        )
        rows = result.mappings().fetchall()

        touched = 0
        skipped_already_new = 0
        skipped_unparseable = 0
        updates: list[tuple[int, str]] = []
        for row in rows:
            row_id = int(row["id"])
            name = str(row["name"] or "")
            raw = row["form_config"]
            try:
                cfg = json.loads(raw) if isinstance(raw, str) else raw
            except Exception as exc:
                print(f"[SKIP] id={row_id} name={name!r}: unparseable form_config ({exc})")
                skipped_unparseable += 1
                continue
            if not isinstance(cfg, dict):
                skipped_unparseable += 1
                continue
            new_cfg, changed = _migrate_form_config(cfg, name)
            if not changed:
                skipped_already_new += 1
                continue
            touched += 1
            updates.append((row_id, json.dumps(new_cfg, ensure_ascii=False)))
            print(f"[CHANGE] id={row_id} name={name!r}")

        if apply and updates:
            for row_id, new_json in updates:
                await session.execute(
                    text("UPDATE new_bot_services SET form_config = :cfg WHERE id = :id"),
                    {"cfg": new_json, "id": row_id},
                )
            await session.commit()

        print()
        print(f"summary: changed={touched}, already_new={skipped_already_new}, unparseable={skipped_unparseable}, total_scanned={len(rows)}")
        print("mode: APPLY (wrote to DB)" if apply else "mode: DRY RUN (no writes)")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Actually write changes to the DB. Default is a dry run.")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(apply=args.apply)))
