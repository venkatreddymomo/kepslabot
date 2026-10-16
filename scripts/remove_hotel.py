"""
Remove a hotel from the dropdown.

Default behaviour is a soft-delete (is_active=False) so the row is hidden from
both /api/chat/properties and /admin/api/properties without cascading destroying
related conversations, services, KB pages, etc. Pass --hard to delete the row
outright (cascades).

Usage:
    python scripts/remove_hotel.py --id 57            # soft delete
    python scripts/remove_hotel.py --id 57 --hard     # hard delete (cascades)
    python scripts/remove_hotel.py --code my_property # by code
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from sqlalchemy import delete, select, update

from models.database import AsyncSessionLocal, Hotel


async def _remove(hotel_id: int | None, hotel_code: str | None, hard: bool) -> int:
    async with AsyncSessionLocal() as session:
        stmt = select(Hotel)
        if hotel_id is not None:
            stmt = stmt.where(Hotel.id == hotel_id)
        elif hotel_code:
            stmt = stmt.where(Hotel.code == hotel_code)
        else:
            print("Provide --id or --code.")
            return 2

        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None:
            print(f"No hotel found for id={hotel_id} code={hotel_code}")
            return 1

        print(f"Found: id={row.id} code={row.code!r} name={row.name!r} city={row.city!r} is_active={row.is_active}")

        if hard:
            await session.execute(delete(Hotel).where(Hotel.id == row.id))
            await session.commit()
            print(f"HARD DELETED hotel id={row.id} (cascade applied).")
        else:
            await session.execute(
                update(Hotel).where(Hotel.id == row.id).values(is_active=False)
            )
            await session.commit()
            print(f"SOFT DELETED hotel id={row.id} (is_active=False, row preserved).")
    return 0


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--id", type=int, default=None, help="Hotel id to remove.")
    parser.add_argument("--code", type=str, default=None, help="Hotel code to remove (alternative to --id).")
    parser.add_argument("--hard", action="store_true", help="Hard delete the row instead of soft-deleting.")
    args = parser.parse_args()
    return asyncio.run(_remove(args.id, args.code, args.hard))


if __name__ == "__main__":
    raise SystemExit(_main())
