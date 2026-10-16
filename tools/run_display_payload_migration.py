"""
Idempotent migration runner: add new_bot_messages.display_payload (LONGTEXT NULL).

Adds the column used to replay rich chat-history bubbles (beautified text,
image cards, submitted form values). Safe to run repeatedly. Uses the app's
configured engine, so it targets whatever DB the app talks to.

Run:  python tools/run_display_payload_migration.py
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from models.database import engine

COL_CHECK = text(
    "SELECT COUNT(*) FROM information_schema.COLUMNS "
    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'new_bot_messages' "
    "AND COLUMN_NAME = 'display_payload'"
)
# Append at the end (no positional AFTER) so MySQL 8 can use ALGORITHM=INSTANT.
ALTER_SQL = text(
    "ALTER TABLE new_bot_messages ADD COLUMN display_payload LONGTEXT NULL"
)


async def main() -> None:
    print(f"[migration] dialect={engine.dialect.name}  url={engine.url.render_as_string(hide_password=True)}")

    async with engine.connect() as conn:
        db_name = (await conn.execute(text("SELECT DATABASE()"))).scalar()
        print(f"[migration] database = {db_name}")
        already = bool((await conn.execute(COL_CHECK)).scalar())

    if already:
        print("[migration] display_payload already exists — nothing to do.")
        return

    async with engine.begin() as conn:
        print("[migration] adding column display_payload ...")
        await conn.execute(ALTER_SQL)

    async with engine.connect() as conn:
        now = bool((await conn.execute(COL_CHECK)).scalar())
    print(f"[migration] done. display_payload present = {now}")


if __name__ == "__main__":
    asyncio.run(main())
