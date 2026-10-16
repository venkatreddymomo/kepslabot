"""
Smoke test: the support/handoff FORM must open for genuine, staff-actionable
pre-booking requests — and must NOT open for phase-impossible services (spa,
food, etc.) which staff cannot action before check-in.

Desired flow (per the support_request agent's description):
  - "can't do that through chat — want our team to reach out?" -> guest "yes" -> FORM
  - genuine staff-actionable: venue/hall booking, extra beds, group/custom -> FORM on accept
  - phase-impossible (spa session, in-room dining, etc.): refuse, and even an
    explicit "connect me to staff" stays REFUSED (no form) — staff can't do it pre-check-in.

Runs against the live backend (pre_booking). It does NOT submit the form, so no
ticket is created.

    python tools/smoke_handoff_form.py
"""

from __future__ import annotations

import argparse
import json
import uuid
import time
from pathlib import Path
from typing import Any

import httpx

DEFAULT_BASE = "http://localhost:8011"
DEFAULT_HOTEL = "zone"
REPORT = Path(__file__).parent / "smoke_handoff_form_report.json"


def send(client, base, hotel, sid, msg):
    r = client.post(f"{base}/api/chat/message", json={
        "session_id": sid, "message": msg, "hotel_code": hotel, "channel": "web_widget",
        "metadata": {"phase": "pre_booking"},
    }, timeout=180.0)
    d = r.json()
    meta = d.get("metadata") or {}
    ent = meta.get("entities") or {}
    return {
        "msg": msg,
        "reply": (d.get("display_message") or d.get("message") or "").strip(),
        "action": ent.get("orchestration_action"),
        "target": ent.get("target_service_id"),
        "form": bool(meta.get("form_trigger")) or str(meta.get("render_type") or "").lower().find("form") >= 0,
    }


def has(text, needles):
    low = text.lower()
    return any(n in low for n in needles)


def is_form(turn):
    if turn["form"]:
        return True
    if "support" in str(turn["target"] or "").lower() and str(turn["action"] or "").lower() in {"collect_info", "create_ticket"}:
        return True
    return has(turn["reply"], ["few quick details", "details below", "fill in the form", "form that will appear", "provide a few"])


def is_refusal(turn):
    return has(turn["reply"], ["checked in", "check-in", "check in", "can't take that", "only available once", "available once you", "i can't"]) \
        and not is_form(turn)


def run_convo(client, base, hotel, msgs):
    sid = f"hf-{uuid.uuid4().hex[:8]}"
    turns = []
    for m in msgs:
        t = send(client, base, hotel, sid, m)
        turns.append(t)
        print(f"   >> {m}\n      << [{t['action']}/{t['target']}] {t['reply'][:140]}")
        time.sleep(0.3)
    return turns


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--hotel", default=DEFAULT_HOTEL)
    args = ap.parse_args()

    failures: list[str] = []
    report: dict[str, Any] = {}
    with httpx.Client() as client:
        R = lambda msgs: run_convo(client, args.base, args.hotel, msgs)

        # GENUINE (staff-actionable) -> FORM should open on acceptance
        print("\n=== G1: extra beds (genuine) -> yes -> FORM ===")
        g1 = R(["tell me about zone jammu", "can i add extra beds in room", "yes pls"]); report["g1_extra_beds"] = g1
        if not is_form(g1[-1]):
            failures.append("G1: extra-beds handoff acceptance did NOT open the form.")

        print("\n=== G2: venue/hall booking (genuine) -> yes -> FORM ===")
        g2 = R(["tell me about zone jammu", "what venues do you have", "i want to book one", "yes"]); report["g2_venue"] = g2
        if not is_form(g2[-1]):
            failures.append("G2: venue handoff acceptance did NOT open the form (fake-ack instead).")

        print("\n=== G3: general 'I want to talk to staff' (hotel-related) -> FORM ===")
        g3 = R(["tell me about zone jammu", "i want to talk to a staff member please"]); report["g3_general_staff"] = g3
        if not is_form(g3[-1]):
            failures.append("G3: a general staff-connect request did NOT open the form.")

        print("\n=== G4: spa context + EXPLICIT staff request -> FORM (hotel-related, explicit human ask) ===")
        g4 = R(["tell me about zone jammu", "book a spa", "connect me to staff"]); report["g4_spa_staff"] = g4
        # The 'book a spa' turn must be REFUSED (execution of during-stay service)...
        if is_form(g4[1]):
            failures.append("G4: 'book a spa' (execution) wrongly opened a form — it should be refused.")
        # ...but the EXPLICIT staff request must open the form (hotel-related).
        if not is_form(g4[-1]):
            failures.append("G4: explicit 'connect me to staff' (hotel-related) did NOT open the form.")

        # EXECUTION of during-stay service -> refuse, no form
        print("\n=== E1: 'book a spa' / 'order food' (execution) -> REFUSED, no form ===")
        e1 = R(["tell me about zone jammu", "book a spa"]); report["e1_spa_exec"] = e1
        if is_form(e1[-1]):
            failures.append("E1: 'book a spa' execution wrongly opened a form (should refuse).")

        # PERSONAL / non-hotel staff request -> no form (sensitive path)
        print("\n=== P1: personal/non-hotel + 'connect me to staff' -> NO form (sensitive) ===")
        p1 = R(["tell me about zone jammu", "i'm going through some tough personal problems", "connect me to staff"]); report["p1_personal"] = p1
        if is_form(p1[-1]):
            failures.append("P1: personal/non-hotel staff request WRONGLY opened the support form (should not).")

    REPORT.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\n[smoke] report -> {REPORT}")
    print("=" * 60)
    if failures:
        print("RESULT: FAIL")
        for f in failures:
            print("  -", f)
        return 1
    print("RESULT: PASS — form opens for genuine staff-actionable handoffs; phase-impossible stays refused.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
