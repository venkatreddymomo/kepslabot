"""Restore Zone's real identity fields in json_section.business (clobbered to the
hotel template — bot_name 'Assistant Name', name 'Your Hotel Name' — by a config
refresh). SAFE MERGE: only sets the identity fields, preserves everything else.

    python tools/restore_zone_identity.py            # DRY RUN
    python tools/restore_zone_identity.py --apply
"""
from __future__ import annotations
import asyncio, json, sys
from sqlalchemy import text
from models.database import AsyncSessionLocal, engine

HID = 67
RESTORE = {
    "bot_name": "Zoogle",
    "name": "Zone by The Park Hotels",
    "welcome_message": "Hi there! How can I assist you today?",
}


async def main(apply: bool) -> int:
    async with AsyncSessionLocal() as s:
        row = (await s.execute(text(
            "SELECT config_value FROM new_bot_business_config WHERE hotel_id=:h AND config_key='json_section.business'"
        ), {"h": HID})).first()
        if not row or not row[0]:
            print("No json_section.business found. Aborting.")
            return 1
        b = json.loads(row[0])
        print("BEFORE:", {k: b.get(k) for k in RESTORE})
        for k, v in RESTORE.items():
            b[k] = v
        print("AFTER :", {k: b.get(k) for k in RESTORE})
        # also confirm standalone authoritative key
        std = (await s.execute(text(
            "SELECT config_value FROM new_bot_business_config WHERE hotel_id=:h AND config_key='business.bot_name'"), {"h": HID})).scalar()
        print("standalone business.bot_name =", repr(std))
        if not apply:
            print("\nDRY RUN — re-run with --apply to write.")
            return 0
        await s.execute(text(
            "UPDATE new_bot_business_config SET config_value=:v WHERE hotel_id=:h AND config_key='json_section.business'"
        ), {"v": json.dumps(b, ensure_ascii=False), "h": HID})
        await s.commit()
        print("\nDONE — identity restored in json_section.business.")
    await engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(apply="--apply" in sys.argv[1:])))
