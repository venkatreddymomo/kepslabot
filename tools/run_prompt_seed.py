"""Seed prompt .md defaults into the DB (same sync the backend runs at startup).

Hash-guarded: only rewrites industry-default rows whose .md changed. Never
touches hotel overrides. Prints which prompt_keys changed, then verifies the
book_answerer fixes landed and flags any hotel override that would shadow them.
"""
from __future__ import annotations
import asyncio, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select
from services.prompt_seed_service import seed_prompts_from_files
from models.database import AsyncSessionLocal, PromptRegistry, engine

KEY = "orchestrator.book_answerer"
MARKERS = ("HARD EXCLUSION", "VENUE / HALL / banquet / event-space bare-book")


async def main() -> None:
    ins, upd, unch, keys = await seed_prompts_from_files()
    print(f"[seed] inserted={ins}  updated={upd}  unchanged={unch}")
    print(f"[seed] changed keys: {keys or '(none)'}")

    async with AsyncSessionLocal() as s:
        rows = (await s.execute(
            select(PromptRegistry).where(PromptRegistry.prompt_key == KEY)
        )).scalars().all()

    print(f"\n[verify] rows for {KEY}: {len(rows)}")
    for r in rows:
        content = str(getattr(r, "content", "") or "")
        scope = "INDUSTRY DEFAULT" if getattr(r, "hotel_id", None) is None else f"HOTEL OVERRIDE (hotel_id={r.hotel_id})"
        has = all(m in content for m in MARKERS)
        print(f"  - {scope}  industry={getattr(r,'industry',None)}  len={len(content)}  fixes_present={has}")
        if getattr(r, "hotel_id", None) is not None:
            print("    ^^ WARNING: a hotel override exists — the seed does NOT update it; "
                  "this hotel will keep the old behavior until the override is reset in admin Prompts.")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
