"""
Read-only: dump recent GHN_CHAT_BOT_CUSTOMER_DETAILS rows so we can compare a
WORKING row (one whose ticket shows a guest name on the dashboard, written by
the real Java widget) against the rows OUR bot writes.

The difference (Widget_Id / Group_Id / Form_Id / Form_Type / Processing_Status /
Conversation_Id) is what the dashboard's join requires.

Run on a machine with DB/VPN access:
    python tools/diagnose_customer_details.py
Then paste the output back.
"""
from __future__ import annotations

import asyncio

from sqlalchemy import text

from models.database import AsyncSessionLocal, engine

SQL = text(
    """
    SELECT ID, Session_Id, Conversation_Id, Widget_Id, Group_Id, Form_Id,
           Form_Type, Guest_Name, Email_Id, Mobile_number, Property_Name,
           Consent_Given, Processing_Status, Created_Date
    FROM GHN_CHAT_BOT_CUSTOMER_DETAILS
    ORDER BY Created_Date DESC
    LIMIT 25
    """
)


async def main() -> int:
    print(f"DB: {engine.url.render_as_string(hide_password=True)}\n")
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(SQL)).mappings().fetchall()
    if not rows:
        print("No rows in GHN_CHAT_BOT_CUSTOMER_DETAILS.")
        return 0
    cols = ["ID", "Session_Id", "Conversation_Id", "Widget_Id", "Group_Id",
            "Form_Id", "Form_Type", "Guest_Name", "Property_Name",
            "Consent_Given", "Processing_Status"]
    for r in rows:
        print("-" * 70)
        for c in cols:
            print(f"  {c:18}= {r.get(c)}")
    print("-" * 70)
    print("\nCompare a WORKING row (ticket shows a name) vs an OURS row "
          "(Form_Type likely 'ci_form'): which columns differ?")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
