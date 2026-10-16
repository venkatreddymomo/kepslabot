"""Drop the few legacy service_kb facts that name a removed Zone property
(json_section.service_kb, hotel_id=67). Dead in DB-first mode; cleanup only.

    python tools/scrub_zone_service_kb.py            # DRY RUN (lists facts)
    python tools/scrub_zone_service_kb.py --apply
"""
from __future__ import annotations
import asyncio, json, sys
from sqlalchemy import text
from models.database import AsyncSessionLocal, engine

HID = 67
REMOVED = ["jaisalmer", "lansdowne", "parra", "port blair", "port_blair", "udaipur", "digha", "patna"]


def _mentions_removed(obj) -> bool:
    s = json.dumps(obj, ensure_ascii=False).lower()
    return any(r in s for r in REMOVED)


async def main(apply: bool) -> int:
    print("Mode:", "APPLY" if apply else "DRY RUN")
    async with AsyncSessionLocal() as s:
        row = (await s.execute(text(
            "SELECT config_value FROM new_bot_business_config WHERE hotel_id=:h AND config_key='json_section.service_kb'"
        ), {"h": HID})).first()
        sk = json.loads(row[0])
        total_dropped = 0
        for rec in sk.get("records", []):
            facts = rec.get("facts") or []
            keep, drop = [], []
            for f in facts:
                (drop if _mentions_removed(f) else keep).append(f)
            for f in drop:
                print(f"  DROP fact {f.get('id')}: {str(f.get('text'))[:120]!r}")
            rec["facts"] = keep
            total_dropped += len(drop)
        print(f"\nTotal facts to drop: {total_dropped}")
        if not apply:
            print("DRY RUN — re-run with --apply to write.")
            return 0
        await s.execute(text(
            "UPDATE new_bot_business_config SET config_value=:v WHERE hotel_id=:h AND config_key='json_section.service_kb'"
        ), {"v": json.dumps(sk, ensure_ascii=False), "h": HID})
        await s.commit()
        print("DONE — service_kb updated.")
    await engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(apply="--apply" in sys.argv[1:])))
