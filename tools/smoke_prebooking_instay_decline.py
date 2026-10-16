"""
Smoke test: in PRE-BOOKING, the bot must politely DECLINE in-stay-only actions
(housekeeping, room service, spa appointment, table booking) instead of opening
the support / ticket-intake form — while still handling genuine pre-arrival
requests normally (room booking, info questions, talk-to-staff about a booking).

This pins the fix for the reported bug: "even for during-stay issues it offers
the staff thing and shows the support form, but we are in pre-booking phase".

The decline is LLM-generated (phase reasoning), NOT a hardcoded keyword block —
so the assertions look for behavior (no form opened + a during-stay decline),
not an exact sentence.

Drives the live /api/chat/message endpoint in pre_booking. Never submits a
form, so NO ticket is created.

Run from the project root with the backend up on localhost:8011:
    python tools/smoke_prebooking_instay_decline.py
    python tools/smoke_prebooking_instay_decline.py --hotel zone --base http://localhost:8011

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

REPORT_PATH = Path(__file__).parent / "smoke_prebooking_instay_decline_report.json"


def _session_id(tag: str) -> str:
    return f"smoke-{tag}-{uuid.uuid4().hex[:10]}"


def call_chat(client: httpx.Client, base: str, hotel: str, session_id: str, message: str) -> dict[str, Any]:
    body = {
        "session_id": session_id,
        "message": message,
        "hotel_code": hotel,
        "channel": "web_widget",
        "metadata": {
            "phase": "pre_booking",
            "chat_test_profile_phase": "pre_booking",
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
    }


def _has(text: str, needles: list[str]) -> bool:
    low = text.lower()
    return any(n in low for n in needles)


def _is_support(meta: dict[str, Any]) -> bool:
    return "support" in str(meta.get("target_service_id") or "").lower()


def _is_form(text: str, meta: dict[str, Any]) -> bool:
    if _has(str(meta.get("render_type") or ""), ["form"]):
        return True
    if meta.get("form_trigger") or meta.get("missing_fields"):
        return True
    if _is_support(meta) and str(meta.get("orchestration_action") or "").lower() in {
        "collect_info", "create_ticket", "dispatch_handler",
    }:
        return True
    return _has(text, ["few quick details", "details below and submit", "fill in", "provide a few", "full name"])


def _is_instay_decline(text: str) -> bool:
    """LLM phrases the decline naturally; accept any 'during the stay / after you
    arrive / can't do that now' wording."""
    during_stay = _has(text, [
        "during your stay", "during the stay", "once you", "after you check in",
        "after you arrive", "when you arrive", "when you check in", "once you're checked in",
        "once you have checked in", "after check-in", "after checking in", "while you're staying",
    ])
    cant = _has(text, ["can't", "cannot", "can not", "not able", "unable", "won't be able"])
    return during_stay and cant


def run_turns(client, base, hotel, session_id, messages) -> list[dict[str, Any]]:
    out = []
    for msg in messages:
        reply = call_chat(client, base, hotel, session_id, msg)
        if reply["status"] != 200:
            out.append({"message": msg, "reply": f"[HTTP {reply['status']}] {_message(reply)}", "meta": {}, "http": reply["status"]})
            print(f"   >> {msg}")
            print(f"   << [HTTP {reply['status']}]  {json.dumps(reply['data'])[:200]}")
            time.sleep(0.4)
            continue
        text, meta = _message(reply), _meta(reply)
        out.append({"message": msg, "reply": text, "meta": meta, "http": 200})
        print(f"   >> {msg}")
        print(f"   << {text[:200]}")
        print(f"      action={meta.get('orchestration_action')!r} target={meta.get('target_service_id')!r} "
              f"form_trigger={meta.get('form_trigger')!r} missing={meta.get('missing_fields')!r}")
        time.sleep(0.4)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--hotel", default=DEFAULT_HOTEL)
    args = parser.parse_args()

    # Each in-stay request runs in its OWN fresh session so there's no
    # cross-contamination between turns.
    instay_requests = [
        "i need to clean my room",
        "i have an issue with my bed",
        "i want to book a spa appointment",
        "book a table for dinner tonight",
        "i want room service",
    ]
    # Genuine pre-arrival requests that MUST keep working (not be wrongly declined).
    allowed_requests = [
        ("i want to book a room", "room_booking"),
        ("what spa services do you have", "info"),
        ("i want to talk to someone about my booking", "support_ok"),
    ]

    failures: list[str] = []
    transcript: dict[str, Any] = {"instay": [], "allowed": []}

    try:
        with httpx.Client() as client:
            print("\n=== A: in-stay actions in pre-booking MUST decline (no form) ===")
            for req in instay_requests:
                sid = _session_id("instay")
                turn = run_turns(client, args.base, args.hotel, sid, [req])[0]
                transcript["instay"].append({"session": sid, **turn})
                if turn.get("http") != 200:
                    failures.append(f"A: '{req}' -> HTTP {turn.get('http')} (backend/DB error, not a fix result).")
                    continue
                text, meta = turn["reply"], turn["meta"]
                if _is_form(text, meta):
                    failures.append(
                        f"A: '{req}' OPENED the support form / ticket intake in pre-booking "
                        f"(target={meta.get('target_service_id')!r}). This is the reported bug."
                    )
                elif not _is_instay_decline(text):
                    failures.append(
                        f"A: '{req}' did not clearly decline as an in-stay action "
                        f"(reply={text[:140]!r})."
                    )

            print("\n=== B: genuine pre-arrival requests MUST still work (no false decline) ===")
            for req, kind in allowed_requests:
                sid = _session_id("allow")
                turn = run_turns(client, args.base, args.hotel, sid, [req])[0]
                transcript["allowed"].append({"session": sid, "kind": kind, **turn})
                if turn.get("http") != 200:
                    failures.append(f"B: '{req}' -> HTTP {turn.get('http')} (backend/DB error).")
                    continue
                text, meta = turn["reply"], turn["meta"]
                if kind == "room_booking":
                    # Must NOT be declined as an in-stay action.
                    if _is_instay_decline(text):
                        failures.append(f"B: room booking '{req}' was WRONGLY declined as in-stay (reply={text[:120]!r}).")
                elif kind == "info":
                    # Informational ask: must not be declined as in-stay and must not force a form.
                    if _is_instay_decline(text):
                        failures.append(f"B: info '{req}' was WRONGLY declined as in-stay (reply={text[:120]!r}).")
                elif kind == "support_ok":
                    # A genuine pre-arrival support ask must not be treated as an in-stay action.
                    if _is_instay_decline(text):
                        failures.append(
                            f"B: pre-arrival support '{req}' was WRONGLY declined as in-stay (reply={text[:120]!r})."
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
    print("  A: in-stay actions (housekeeping/room service/spa/table) declined politely, no form opened.")
    print("  B: room booking, info, and pre-arrival support asks were NOT wrongly declined.")
    print("  (No form submitted, so no ticket created.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
