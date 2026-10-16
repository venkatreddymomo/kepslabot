"""
Read-only: list the departments the bot would offer the LLM for each Zone
property, using the SAME lookup the ticket flow uses
(lumira_ticketing_repository.fetch_departments_of_entity).

Run on a machine with DB/VPN access:
    python tools/diagnose_zone_departments.py

Requires LUMIRA_DEPARTMENTS_ENABLED=true in .env (already set). If a property
returns an empty list, the GHN FMS->RI->DEPARTMENT_STAFF_CONFIG(MANAGER=1) join
has no rows for that entity — tell me and we adjust the query.
"""
from __future__ import annotations

import asyncio

from integrations.lumira_ticketing_repository import (
    _departments_enabled,
    lumira_ticketing_repository,
)
from models.database import AsyncSessionLocal, engine

# FMS entity ids (what goes in organisation_id) -> label
ENTITIES = [
    ("6290", "Zone Kolkata (FMS 6290 / RI 7256)"),
    ("6295", "Zone Jaipur (FMS 6295 / RI 7728)"),
]


async def main() -> int:
    print(f"DB: {engine.url.render_as_string(hide_password=True)}")
    print(f"lumira_departments_enabled = {_departments_enabled()}")
    if not _departments_enabled():
        print("Flag is OFF — set LUMIRA_DEPARTMENTS_ENABLED=true in .env and re-run.")
        return 1
    print("=" * 70)
    async with AsyncSessionLocal() as session:
        for fms_id, label in ENTITIES:
            depts = await lumira_ticketing_repository.fetch_departments_of_entity(
                session, entity_id=fms_id
            )
            print(f"\n{label}: {len(depts)} department(s)")
            for d in depts:
                print(f"    id={d.get('department_id'):>6}  {d.get('department_name')}")
            if not depts:
                print("    (none — join returned no rows for this entity)")
    print("\n" + "=" * 70)
    print("If lists look right, the LLM will pick from these and set department_allocated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
