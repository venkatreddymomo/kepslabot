"""
Diagnose why Zone (Kolkata / Jaipur) tickets do not show up for the property
manager — i.e. the entity / organisation_id routing for ticket creation.

READ ONLY. Makes no writes. Run on a machine with DB/VPN access:

    python tools/diagnose_zone_ticketing_entity.py

What it shows (for the `zone` tenant and anything matching kolkata/jaipur):
  1. The hotel row(s)  -> id / code / name
  2. Properties under each hotel (new_bot_properties): slug, name, aliases, scope
  3. Services under each hotel (new_bot_services): service_id, ticketing_enabled,
     and whether form_config carries a `lumira_entity_id` (+ its value)
  4. The EXACT lookup the Admin Chat Harness runs (api/routes/chat.py
     _fetch_real_test_profiles) and the entity_id it currently resolves to.
     If that is empty, the harness falls back to the TEST entity 5703 ->
     every Zone ticket lands under 5703, invisible to the real property.

Expected real entity ids (from Kebo config): 7256 and 7728 (one per property).
This script does NOT know which is Kolkata vs Jaipur — that mapping is what we
need to wire next.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from sqlalchemy import select, text

from models.database import AsyncSessionLocal, BotService, Hotel, Property, engine

# api/routes/chat.py fallback used when no lumira_entity_id is found.
TEST_FALLBACK_ENTITY_ID = "5703"
EXPECTED_REAL_ENTITY_IDS = ("7256", "7728")

# Tenants / properties we care about.
HOTEL_CODE_HINTS = ("zone",)
NAME_HINTS = ("zone", "kolkata", "jaipur")


def _as_form_config(raw: Any) -> dict[str, Any]:
    """form_config may come back as a dict (ORM JSON col) or a JSON string."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _lumira_entity_id_of(form_config: dict[str, Any]) -> str:
    return str(form_config.get("lumira_entity_id") or "").strip()


async def _harness_resolved_entity_id(session, hotel_code: str) -> tuple[str, bool]:
    """Replicate api/routes/chat.py::_fetch_real_test_profiles entity lookup.

    Returns (resolved_entity_id, used_fallback).
    """
    entity_id = ""
    try:
        result = await session.execute(
            text(
                """
                SELECT s.form_config
                FROM new_bot_services s
                JOIN new_bot_hotels h ON h.id = s.hotel_id
                WHERE h.code = :code
                  AND s.form_config IS NOT NULL
                  AND s.form_config LIKE '%lumira_entity_id%'
                LIMIT 1
                """
            ),
            {"code": hotel_code},
        )
        row = result.first()
        if row and row[0]:
            fc = _as_form_config(row[0])
            entity_id = _lumira_entity_id_of(fc)
    except Exception as exc:  # pragma: no cover - diagnostic
        print(f"    (harness lookup raised: {exc})")

    if not entity_id:
        return TEST_FALLBACK_ENTITY_ID, True
    return entity_id, False


async def _find_hotels(session) -> list[Hotel]:
    seen: dict[int, Hotel] = {}

    # by code
    for code_hint in HOTEL_CODE_HINTS:
        res = await session.execute(
            select(Hotel).where(Hotel.code == code_hint)
        )
        for h in res.scalars():
            seen[h.id] = h

    # by name / code substring (catches per-property hotels if that's the shape)
    for hint in NAME_HINTS:
        like = f"%{hint}%"
        res = await session.execute(
            select(Hotel).where((Hotel.name.ilike(like)) | (Hotel.code.ilike(like)))
        )
        for h in res.scalars():
            seen[h.id] = h

    return sorted(seen.values(), key=lambda h: (h.code or "", h.id))


async def main() -> int:
    print(f"DB: {engine.url.render_as_string(hide_password=True)}")
    print(f"Fallback test entity (api/routes/chat.py): {TEST_FALLBACK_ENTITY_ID}")
    print(f"Expected real Zone entity ids (Kebo): {', '.join(EXPECTED_REAL_ENTITY_IDS)}")
    print("=" * 78)

    async with AsyncSessionLocal() as session:
        hotels = await _find_hotels(session)
        if not hotels:
            print("No hotels matched code='zone' or name/code ~ zone/kolkata/jaipur.")
            print("If Zone uses a different code, tell me and I'll widen the search.")
            return 1

        for h in hotels:
            print(f"\nHOTEL  id={h.id}  code={h.code!r}  name={h.name!r}  city={h.city!r}  active={bool(h.is_active)}")

            # Properties
            props = (
                await session.execute(
                    select(Property).where(Property.hotel_id == h.id)
                )
            ).scalars().all()
            if props:
                print(f"  Properties ({len(props)}):")
                for p in props:
                    aliases = p.aliases if isinstance(p.aliases, list) else []
                    print(
                        f"    - slug={p.property_slug!r}  name={p.name!r}  "
                        f"default={bool(p.is_default)} active={bool(p.is_active)}  "
                        f"aliases={aliases}"
                    )
            else:
                print("  Properties: (none in new_bot_properties)")

            # Services + lumira_entity_id presence
            svcs = (
                await session.execute(
                    select(BotService).where(BotService.hotel_id == h.id)
                )
            ).scalars().all()
            print(f"  Services ({len(svcs)}):")
            any_entity = False
            for s in svcs:
                fc = _as_form_config(s.form_config)
                eid = _lumira_entity_id_of(fc)
                if eid:
                    any_entity = True
                fc_keys = ",".join(sorted(fc.keys())) if fc else "-"
                print(
                    f"    - service_id={s.service_id!r}  ticketing_enabled={bool(s.ticketing_enabled)}  "
                    f"lumira_entity_id={eid or '(none)'}"
                )
                # surface any per-property entity-ish keys already present
                extra = {
                    k: v for k, v in (fc or {}).items()
                    if "entity" in k.lower() or "property" in k.lower()
                }
                if extra:
                    print(f"        form_config entity/property keys: {extra}")

            # Harness resolution
            resolved, fallback = await _harness_resolved_entity_id(session, h.code)
            verdict = (
                f"FALLBACK -> {resolved} (NO lumira_entity_id found; tickets land under TEST entity)"
                if fallback
                else f"{resolved} (from form_config.lumira_entity_id)"
            )
            print(f"  >>> Chat Harness would file tickets under organisation_id = {verdict}")
            if not any_entity:
                print("  >>> ROOT CAUSE: no service has lumira_entity_id -> harness uses 5703.")

        print("\n" + "=" * 78)
        print("NEXT: tell me which entity id is which property:")
        print("   Zone Park Kolkata -> 7256 or 7728 ?")
        print("   Zone <Jaipur prop> -> the other one")
        print("Plus paste the property slugs printed above so I can wire per-property routing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
