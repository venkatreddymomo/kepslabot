"""
Smoke test: venue-booking handoff must work whether or not the guest looked at
rooms first. Drives the live /api/chat/message endpoint in pre_booking.

Two scenarios:

  A. POLLUTED session (the reported bug): explore a room and start a booking
     lead, THEN switch to the venue and say a bare "book it". The bare verb must
     NOT be hijacked back into the room-booking template — it must land on the
     venue handoff (decline + "want our team to reach out?" / support form).

  B. CLEAN session: a first-attempt "book the townhall" right after pure venue
     INFO must do the two-step — decline + staff-handoff offer first, then open
     the support form only after the guest accepts.

Properties are pinned by name so the book answerer doesn't fan out / pick a
detail-less property (which would itself trigger a conversion-blocker offer and
confound the test).

It never submits a form, so NO ticket is created.

Run from the project root with the backend up on localhost:8011:
    python tools/smoke_venue_handoff.py
    python tools/smoke_venue_handoff.py --hotel zone --base http://localhost:8011

Exit 0 = all assertions passed, 1 = a failure, 2 = setup/HTTP error.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

DEFAULT_BASE = "http://localhost:8011"
DEFAULT_HOTEL = "zone"

REPORT_PATH = Path(__file__).parent / "smoke_venue_handoff_report.json"


def _session_id(tag: str) -> str:
    return f"smoke-{tag}-{uuid.uuid4().hex[:10]}"


def call_chat(client: httpx.Client, base: str, hotel: str, session_id: str, message: str) -> dict[str, Any]:
    body = {
        "session_id": session_id,
        "message": message,
        "hotel_code": hotel,
        "channel": "web_widget",
        # /api/chat/message reads the phase from metadata["phase"] (the exact key
        # the admin ChatHarness/widget sends). selected_phase_id is IGNORED here
        # and silently falls back to during_stay.
        # Carry guest identity the way the real widget's CTA "Book a Stay" does, so
        # "book this room" completes as a booking LEAD (website link) instead of
        # opening a details form — this matches the reported conversation.
        "metadata": {
            "phase": "pre_booking",
            "chat_test_profile_phase": "pre_booking",
            "guest_name": "Sana Test",
            "guest_phone": "9000000001",
            "guest_email": "sana.test@example.com",
        },
    }
    resp = client.post(f"{base}/api/chat/message", json=body, timeout=180.0)
    try:
        data = resp.json()
    except Exception:
        data = {"_parse_error": resp.text[:500]}
    return {"status": resp.status_code, "data": data}


def _message(reply: dict[str, Any]) -> str:
    d = reply.get("data") or {}
    return str(d.get("display_message") or d.get("message") or "").strip()


def _meta(reply: dict[str, Any]) -> dict[str, Any]:
    d = reply.get("data") or {}
    meta = d.get("metadata") or {}
    entities = meta.get("entities") or {}
    return {
        "orchestration_action": entities.get("orchestration_action"),
        "target_service_id": entities.get("target_service_id"),
        "render_type": meta.get("render_type"),
        "form_trigger": meta.get("form_trigger"),
        "missing_fields": entities.get("missing_fields"),
        "pending_action": entities.get("pending_action"),
    }


def _has(text: str, needles: list[str]) -> bool:
    low = text.lower()
    return any(n in low for n in needles)


def _is_room_booking(text: str, meta: dict[str, Any]) -> bool:
    tsid = str(meta.get("target_service_id") or "").lower()
    if tsid.startswith("room_booking"):
        return True
    return _has(text, ["complete your booking on our website", "20% off on direct", "book on our website"])


def _is_support(meta: dict[str, Any]) -> bool:
    return "support" in str(meta.get("target_service_id") or "").lower()


def _has_decline(text: str) -> bool:
    return _has(text, ["can't", "cannot", "can not", "not able", "unable", "directly through", "through the chat"])


def _has_offer(text: str) -> bool:
    return _has(text, ["our team", "reach out", "connect you", "get in touch", "someone from our team", "have our team"])


def _is_form(text: str, meta: dict[str, Any]) -> bool:
    if _has(str(meta.get("render_type") or ""), ["form"]):
        return True
    if meta.get("form_trigger") or meta.get("missing_fields"):
        return True
    if _is_support(meta) and str(meta.get("orchestration_action") or "").lower() in {
        "collect_info", "create_ticket", "dispatch_handler",
    }:
        return True
    return _has(text, ["few quick details", "details below", "fill in", "provide a few"])


def run_turns(client, base, hotel, session_id, messages) -> list[dict[str, Any]]:
    out = []
    for msg in messages:
        reply = call_chat(client, base, hotel, session_id, msg)
        text, meta = _message(reply), _meta(reply)
        out.append({"message": msg, "reply": text, "meta": meta})
        print(f"   >> {msg}")
        print(f"   << {text[:220]}")
        print(f"      action={meta.get('orchestration_action')!r} target={meta.get('target_service_id')!r}")
        time.sleep(0.4)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--hotel", default=DEFAULT_HOTEL)
    args = parser.parse_args()

    failures: list[str] = []
    transcript: dict[str, Any] = {}

    try:
        with httpx.Client() as client:
            # ---- Scenario A: polluted (room first), then venue + bare "book it" ----
            print("\n=== Scenario A: room-first, then venue 'book it' (the reported bug) ===")
            sid_a = _session_id("polA")
            turns_a = run_turns(client, args.base, args.hotel, sid_a, [
                "tell me about zone connect park coimbatore",
                "tell me about the connect room",
                "book this room",
                "do you have venues at coimbatore",
                "book it",
            ])
            transcript["scenario_a"] = {"session": sid_a, "turns": turns_a}

            book_room_turn = turns_a[2]   # "book this room" — SHOULD be room booking
            venue_turn = turns_a[3]       # "do you have venues" — book route, clears lead
            book_it_turn = turns_a[4]     # "book it" — must NOT be room booking

            if not _is_room_booking(book_room_turn["reply"], book_room_turn["meta"]):
                failures.append("A: 'book this room' did NOT engage room booking (regression in the room path).")
            if _is_room_booking(book_it_turn["reply"], book_it_turn["meta"]):
                failures.append(
                    "A: bare 'book it' after the venue turn was HIJACKED back to room booking "
                    f"(target={book_it_turn['meta'].get('target_service_id')!r}). This is the reported bug."
                )
            elif not (_has_offer(book_it_turn["reply"]) or _is_support(book_it_turn["meta"]) or _is_form(book_it_turn["reply"], book_it_turn["meta"])):
                failures.append(
                    "A: bare 'book it' did not land on a venue handoff/support path "
                    f"(reply={book_it_turn['reply'][:120]!r})."
                )

            # ---- Scenario B: clean, first-attempt 'book the townhall' two-step ----
            print("\n=== Scenario B: clean venue 'book the townhall' -> decline+offer -> yes -> form ===")
            sid_b = _session_id("cleanB")
            turns_b = run_turns(client, args.base, args.hotel, sid_b, [
                "tell me about the townhall at zone connect park coimbatore",
                "book the townhall",
                "yes",
            ])
            transcript["scenario_b"] = {"session": sid_b, "turns": turns_b}

            info_turn = turns_b[0]
            book_hall_turn = turns_b[1]
            yes_turn = turns_b[2]

            if _is_room_booking(book_hall_turn["reply"], book_hall_turn["meta"]):
                failures.append("B: 'book the townhall' was hijacked to room booking.")
            else:
                # Accept the two-step (decline+offer on the book-hall turn, form on yes)
                # OR a direct-to-form, AS LONG AS it's the support/venue lane — but we
                # PREFER the two-step. Only flag if the info turn did NOT pre-offer and
                # the book-hall turn skipped straight to the form.
                info_pre_offered = _has_offer(info_turn["reply"])
                book_hall_is_form = _is_form(book_hall_turn["reply"], book_hall_turn["meta"])
                book_hall_is_decline_offer = _has_decline(book_hall_turn["reply"]) and _has_offer(book_hall_turn["reply"])
                if not info_pre_offered and book_hall_is_form and not book_hall_is_decline_offer:
                    failures.append(
                        "B: first 'book the townhall' skipped the consent step and jumped straight to the form "
                        "(should decline + offer staff first)."
                    )
                # After 'yes', we should be in the support form / ticket-intake lane.
                if not (_is_form(yes_turn["reply"], yes_turn["meta"]) or _is_support(yes_turn["meta"])):
                    failures.append(
                        "B: 'yes' did not reach the support form / ticket intake "
                        f"(target={yes_turn['meta'].get('target_service_id')!r})."
                    )
    except httpx.HTTPError as exc:
        print(f"[smoke] HTTP error: {exc!r} — is the backend up on {args.base}?")
        return 2

    REPORT_PATH.write_text(json.dumps(transcript, indent=2, default=str), encoding="utf-8")
    print(f"\n[smoke] transcript -> {REPORT_PATH}")
    print("=" * 64)
    if failures:
        print("RESULT: FAIL")
        for f in failures:
            print("  -", f)
        return 1
    print("RESULT: PASS")
    print("  A: room lead cleared on venue topic — bare 'book it' went to venue handoff, not room booking.")
    print("  B: clean 'book the townhall' reached the staff-handoff / support form path.")
    print("  (No form submitted, so no ticket created.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
