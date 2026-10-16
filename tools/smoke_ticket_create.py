"""
Smoke test: create a real ticket via the dev rep-dashboard API.

Run from the new_bot project root:
    python tools/smoke_ticket_create.py

Uses a payload modeled on a known-good production ticket (organisation_id=5703)
with a clearly identifiable issue string so the result is easy to find in the
agent dashboard. Reports the returned ticket_id on success — search the dashboard
for that ID to confirm it lands in the ticket page.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import settings  # noqa: E402
from services.ticketing_service import ticketing_service  # noqa: E402


async def main() -> int:
    base = (settings.ticketing_base_url or "").strip()
    print(f"[smoke] TICKETING_BASE_URL = {base!r}")
    if not base:
        print("[smoke] FAIL: TICKETING_BASE_URL not configured")
        return 2

    stamp = datetime.now(UTC).strftime("%H:%M:%S %d-%m-%Y")
    tag = datetime.now(UTC).strftime("%H%M%S")

    payload: dict = {
        "guest_id": "263496787",
        "room_number": "",
        "organisation_id": "5703",
        "issue": f"[SMOKE-TEST {tag}] new_bot ticketing smoke test — please ignore",
        "message": (
            f"This ticket was created by tools/smoke_ticket_create.py at {stamp} UTC "
            "to verify the new_bot → kepsla-rep-dashboard-svc /insert/ticket.htm "
            "integration after wiring widget_id into the handoff payload. "
            "Safe to close."
        ),
        "sentiment_score": "",
        "department_allocated": "",
        "department_manager": "",
        "assigned_to": "",
        "priority": "LOW",
        "categorization": "room booking support",
        "sub_categorization": "smoke_test",
        "manager_notes": "",
        "created_at": stamp,
        "session_id": f"smoke-ticket-{tag}",
        "phase": "Pre Booking",
        "ticket_status": "open",
        "chatBot": "yes",
        "sla_due_time": "",
        "outlet_id": "",
        "source": "whatsapp_bot",
        "guest_name": "Smoke Test",
        "property_name": "Fort Jadhavgadh",
        "room_type": "Museum Suite",
        "department_id": "",
        "category": "room booking support",
        "sub_category": "smoke_test",
        "guestName": "Smoke Test",
        "propertyName": "Fort Jadhavgadh",
        "roomType": "Museum Suite",
        "entity_id": "5703",
    }

    print(f"[smoke] Creating ticket with issue tag SMOKE-TEST {tag} ...")
    result = await ticketing_service.create_ticket(payload)

    print("[smoke] success      :", result.success)
    print("[smoke] status_code  :", result.status_code)
    print("[smoke] ticket_id    :", result.ticket_id)
    print("[smoke] assigned_id  :", result.assigned_id)
    print("[smoke] error        :", result.error)
    print("[smoke] response body:", json.dumps(result.response, indent=2, default=str)[:1500])
    if result.success and result.ticket_id:
        print()
        print(f"[smoke] PASS: ticket {result.ticket_id} created. Search the dashboard")
        print(f"        for the tag 'SMOKE-TEST {tag}' or ticket ID {result.ticket_id}.")
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
