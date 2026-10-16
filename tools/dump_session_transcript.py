"""Dump full transcript + tickets for one session. Read-only. UTF-8 output."""
from __future__ import annotations

import asyncio
import io
import sys

from sqlalchemy import text

from models.database import AsyncSessionLocal


async def main(sid: str) -> None:
    lines: list[str] = []

    def p(s: str = "") -> None:
        lines.append(s)

    async with AsyncSessionLocal() as s:
        # --- messages ---
        rows = (await s.execute(text(
            "SELECT ts, sender, user_text, bot_text, phase, service_id, intent, conversation_id "
            "FROM new_bot_log_messages WHERE session_id=:sid ORDER BY ts ASC, id ASC"
        ), {"sid": sid})).mappings().all()
        p(f"=== TRANSCRIPT ({len(rows)} rows) session={sid} ===")
        n = 0
        for r in rows:
            ut = (r.get("user_text") or "").strip()
            bt = (r.get("bot_text") or "").strip()
            meta = f"[phase={r.get('phase')} svc={r.get('service_id')} intent={r.get('intent')}]"
            if ut:
                n += 1
                p(f"\n[{n:02d}] USER ({r.get('ts')}) {meta}:")
                p(ut)
            if bt:
                p(f"     BOT:")
                p(bt)

        # --- tickets ---
        p("\n\n=== TICKETS (new_bot_log_tickets) ===")
        try:
            trows = (await s.execute(text(
                "SELECT ts, session_id, ticket_ref, service_id, status, stage, event, message "
                "FROM new_bot_log_tickets WHERE session_id=:sid ORDER BY ts ASC"
            ), {"sid": sid})).mappings().all()
            p(f"count={len(trows)}")
            for t in trows:
                p(f"  - ref={t.get('ticket_ref')} svc={t.get('service_id')} "
                  f"status={t.get('status')} stage={t.get('stage')} event={t.get('event')} "
                  f":: {(t.get('message') or '')[:140]}")
        except Exception as e:
            p(f"  tickets query error: {e}")

        # --- customer details (external/kepsla tickets) ---
        p("\n=== GHN_CHAT_BOT_CUSTOMER_DETAILS (this session) ===")
        try:
            ccols = (await s.execute(text(
                "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_NAME='GHN_CHAT_BOT_CUSTOMER_DETAILS'"))).scalars().all()
            has_sid = any(c.lower() == "session_id" for c in ccols)
            if has_sid:
                crows = (await s.execute(text(
                    "SELECT * FROM GHN_CHAT_BOT_CUSTOMER_DETAILS WHERE session_id=:sid "
                    "ORDER BY 1 DESC LIMIT 30"), {"sid": sid})).mappings().all()
                p(f"count={len(crows)}")
                for c in crows:
                    keep = {k: v for k, v in c.items() if k.lower() in (
                        "id", "customer_name", "customer_phone", "customer_email",
                        "categorization", "issue", "created_date", "entity_id",
                        "hotel_code", "session_id")}
                    p(f"  {keep}")
            else:
                p(f"  (no session_id column) cols={ccols}")
        except Exception as e:
            p(f"  customer details query error: {e}")

    out = "\n".join(lines)
    path = sys.argv[2] if len(sys.argv) > 2 else "transcript_out.txt"
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(out)
    print(f"wrote {path} ({len(rows)} msg rows)")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
