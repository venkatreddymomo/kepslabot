"""
Smoke test: a GUEST-INITIATED request to be contacted by a human - "call me back",
"call back" (even misspelled "calll back"), "talk to staff" - must OPEN the support
intake form immediately (no "do you want to connect?" offer step), because the guest
already asked. Meanwhile the BOT-INITIATED handoff flow (bot hits a wall, offers
handoff, guest accepts) must stay intact, and the four guardrails must NOT regress:
  - in-stay execution ("book a spa") in pre-booking -> decline, no form
  - "tell me more" after a handoff offer -> answer, no form
  - personal/non-hotel crisis "I need someone to talk to" -> empathy, no form

Pins the fix for the reported bug: guest typed "Calll back", bot replied
"let me get someone from our team to help right away" and created NO ticket / form.

Drives the live /api/chat/message endpoint in pre_booking. Never SUBMITS a form,
so NO ticket is created - we only assert whether the form WOULD open.

Run from the project root with the backend up on localhost:8011:
    python tools/smoke_callback_request_form.py
    python tools/smoke_callback_request_form.py --hotel zone --base http://localhost:8011

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

REPORT_PATH = Path(__file__).parent / "smoke_callback_request_form_report.json"


def _session_id(tag: str) -> str:
    return f"smoke-cb-{tag}-{uuid.uuid4().hex[:10]}"


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


def _is_deadend_ack(text: str) -> bool:
    """The exact failing symptom: a terminal 'let me get someone' acknowledgement
    with no form behind it."""
    return _has(text, [
        "let me get someone from our team",
        "get someone from our team to help",
    ])


def run_turn(client, base, hotel, sid, msg) -> dict[str, Any]:
    reply = call_chat(client, base, hotel, sid, msg)
    if reply["status"] != 200:
        turn = {"message": msg, "reply": f"[HTTP {reply['status']}] {_message(reply)}", "meta": {}, "http": reply["status"]}
        print(f"   >> {msg}")
        print(f"   << [HTTP {reply['status']}]  {json.dumps(reply['data'])[:200]}")
        time.sleep(0.3)
        return turn
    text, meta = _message(reply), _meta(reply)
    turn = {"message": msg, "reply": text, "meta": meta, "http": 200}
    print(f"   >> {msg}")
    print(f"   << {text[:220]}")
    print(f"      action={meta.get('orchestration_action')!r} target={meta.get('target_service_id')!r} "
          f"render={meta.get('render_type')!r} form_trigger={meta.get('form_trigger')!r} "
          f"missing={meta.get('missing_fields')!r}  FORM={_is_form(text, meta)}")
    time.sleep(0.3)
    return turn


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--hotel", default=DEFAULT_HOTEL)
    args = parser.parse_args()

    failures: list[str] = []
    transcript: dict[str, Any] = {}

    try:
        with httpx.Client() as client:
            # ── CASE 1: guest-initiated cold contact request -> form MUST open ──
            print("\n=== CASE 1: cold guest-initiated contact request MUST open the form ===")
            cold_asks = ["call me back", "Calll back", "talk to staff",
                         "I need to talk to someone about my booking"]
            transcript["case1_cold_asks"] = []
            for ask in cold_asks:
                sid = _session_id("cold")
                t = run_turn(client, args.base, args.hotel, sid, ask)
                transcript["case1_cold_asks"].append({"session": sid, **t})
                if t.get("http") != 200:
                    failures.append(f"CASE1: '{ask}' -> HTTP {t.get('http')} (backend/DB error).")
                    continue
                if _is_deadend_ack(t["reply"]) and not _is_form(t["reply"], t["meta"]):
                    failures.append(
                        f"CASE1: '{ask}' produced the dead-end 'let me get someone' ack with NO form "
                        f"(the exact reported bug). target={t['meta'].get('target_service_id')!r}")
                elif not _is_form(t["reply"], t["meta"]):
                    failures.append(
                        f"CASE1: '{ask}' did NOT open the support form "
                        f"(target={t['meta'].get('target_service_id')!r}, action={t['meta'].get('orchestration_action')!r}).")

            # ── CASE 2: bot-initiated handoff (venue wall) still works ──
            print("\n=== CASE 2: bot offers handoff on a venue wall, guest accepts -> form opens ===")
            sid = _session_id("venue")
            t1 = run_turn(client, args.base, args.hotel, sid, "i want to book the banquet hall for a wedding reception")
            t2 = run_turn(client, args.base, args.hotel, sid, "yes please")
            transcript["case2_venue_handoff"] = {"session": sid, "turn1": t1, "turn2": t2}
            if t2.get("http") == 200 and not _is_form(t2["reply"], t2["meta"]):
                failures.append(
                    "CASE2: venue handoff acceptance 'yes please' did NOT open the form "
                    f"(target={t2['meta'].get('target_service_id')!r}). Case 2 regressed.")

            # ── GUARDRAIL A: 'tell me more' after a handoff offer -> NO form ──
            print("\n=== GUARDRAIL A: 'tell me more' after a handoff offer must NOT open a form ===")
            sid = _session_id("tellmore")
            g1 = run_turn(client, args.base, args.hotel, sid, "i want to book the banquet hall for a wedding reception")
            g2 = run_turn(client, args.base, args.hotel, sid, "tell me more")
            transcript["guardA_tell_me_more"] = {"session": sid, "turn1": g1, "turn2": g2}
            if g2.get("http") == 200 and _is_form(g2["reply"], g2["meta"]):
                failures.append(
                    "GUARDRAIL A: 'tell me more' after a handoff offer WRONGLY opened the form "
                    f"(target={g2['meta'].get('target_service_id')!r}). Info request must not open a form.")

            # ── GUARDRAIL B: in-stay 'book a spa' in pre-booking -> decline, NO form ──
            print("\n=== GUARDRAIL B: in-stay 'book a spa' in pre-booking must NOT open a form ===")
            sid = _session_id("spa")
            b = run_turn(client, args.base, args.hotel, sid, "i want to book a spa appointment")
            transcript["guardB_instay_spa"] = {"session": sid, **b}
            if b.get("http") == 200 and _is_form(b["reply"], b["meta"]):
                failures.append(
                    "GUARDRAIL B: in-stay 'book a spa' WRONGLY opened the support form in pre-booking "
                    f"(target={b['meta'].get('target_service_id')!r}).")

            # ── GUARDRAIL C: personal/non-hotel crisis -> empathy, NO form ──
            print("\n=== GUARDRAIL C: personal crisis 'I need someone to talk to' must NOT open a form ===")
            sid = _session_id("crisis")
            c = run_turn(client, args.base, args.hotel, sid,
                         "i'm feeling really hopeless and i want to hurt myself, i need someone to talk to")
            transcript["guardC_personal_crisis"] = {"session": sid, **c}
            if c.get("http") == 200 and _is_form(c["reply"], c["meta"]):
                failures.append(
                    "GUARDRAIL C: a personal-crisis message WRONGLY opened the support form "
                    f"(target={c['meta'].get('target_service_id')!r}). Sensitive precedence failed.")

    except httpx.HTTPError as exc:
        print(f"[smoke] HTTP error: {exc!r} - is the backend up on {args.base}?")
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
    print("  CASE1: cold 'call me back' / 'calll back' / 'talk to staff' opened the support form.")
    print("  CASE2: bot-initiated venue handoff acceptance still opened the form.")
    print("  GUARDRAILS: 'tell me more', in-stay spa, and personal crisis did NOT open a form.")
    print("  (No form submitted, so no ticket created.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
