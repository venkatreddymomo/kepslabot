"""
Delete the 7 raw Zone KB source files (new_bot_kb_files) left behind after the
property delete. Each was confirmed to carry exactly ONE 'Property:' header, so
they are single-property files and safe to remove.

Scoped to hotel_id=67 (code 'zone') as a guard. Dry-run by default.

    python tools/remove_zone_kbfiles.py            # DRY RUN
    python tools/remove_zone_kbfiles.py --apply    # delete
"""
from __future__ import annotations

import asyncio
import sys

from sqlalchemy import delete, select

from models.database import AsyncSessionLocal, KBFile, engine

ZONE_HOTEL_ID = 67
FILE_IDS = [480, 481, 472, 483, 484, 485, 487]


async def main(apply: bool) -> int:
    print(f"DB: {engine.url.render_as_string(hide_password=True)}")
    print("Mode: " + ("APPLY (will DELETE)" if apply else "DRY RUN (no writes)"))
    print(f"Target kb_file ids: {FILE_IDS} (guard hotel_id={ZONE_HOTEL_ID})")
    print("=" * 70)

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(KBFile).where(
                    KBFile.id.in_(FILE_IDS), KBFile.hotel_id == ZONE_HOTEL_ID
                )
            )
        ).scalars().all()

        if not rows:
            print("No matching kb_files found (already deleted?). Nothing to do.")
            return 0

        for f in rows:
            print(f"  - id={f.id} name={f.original_name!r} ({len(f.content or '')} chars)")
        found_ids = {f.id for f in rows}
        missing = [i for i in FILE_IDS if i not in found_ids]
        if missing:
            print(f"  (note: ids not found / not under this hotel: {missing})")

        if not apply:
            print("\nDRY RUN complete. Re-run with --apply to delete.")
            return 0

        await session.execute(
            delete(KBFile).where(
                KBFile.id.in_(list(found_ids)), KBFile.hotel_id == ZONE_HOTEL_ID
            )
        )
        await session.commit()
        print(f"\nDONE. Deleted {len(found_ids)} kb_file row(s).")
    await engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(apply="--apply" in sys.argv[1:])))
