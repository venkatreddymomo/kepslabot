"""
List every hotel in new_bot_hotels (active + inactive) for quick auditing.

Usage:
    python scripts/list_hotels.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from sqlalchemy import select

from models.database import AsyncSessionLocal, Hotel


async def _list() -> int:
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(select(Hotel).order_by(Hotel.id))).scalars().all()
    if not rows:
        print("(no hotels)")
        return 0
    print(f"{'id':>4}  {'code':<24} {'name':<30} {'city':<20} active")
    print("-" * 90)
    for row in rows:
        print(
            f"{row.id:>4}  {str(row.code or ''):<24} "
            f"{str(row.name or ''):<30} {str(row.city or ''):<20} {bool(row.is_active)}"
        )
    print(f"\nTotal rows: {len(rows)}  Active: {sum(1 for r in rows if r.is_active)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_list()))
