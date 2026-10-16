"""
Smoke test: conversation_uid history isolation.

Same stable visitor session, two conversation threads (A then B):

  Thread A:
    1. "remember my reference code is 7421" -> ack
    2. "what reference code did I just give you?" -> MUST recall 7421
       (proves same-thread history is intact)
  Thread B (new conversation_id, SAME session):
    3. "what is my reference code?" -> MUST NOT know 7421
       (proves a new conversation starts the LLM with a clean slate)

Run with the backend up on localhost:8011:
    python tools/smoke_conv_isolation.py
"""

from __future__ import annotations

import sys
import uuid
from typing import Any

import httpx

BASE = "http://localhost:8011"
HOTEL = "zone"
SESSION = f"smoke-iso-{uuid.uuid4().hex[:10]}"
CONV_A = f"conv-A-{uuid.uuid4().hex[:8]}"
CONV_B = f"conv-B-{uuid.uuid4().hex[:8]}"
SECRET = "7421"


def call(client: httpx.Client, conv: str, message: str) -> str:
    body = {
        "session_id": SESSION,
        "message": message,
        "hotel_code": HOTEL,
        "channel": "web_widget",
        "metadata": {
            "phase": "pre_booking",
            "chat_test_profile_phase": "pre_booking",
            "conversation_id": conv,
            "guest_name": "Iso Test",
            "guest_phone": "9000000007",
            "guest_email": "iso.test@example.com",
        },
    }
    resp = client.post(f"{BASE}/api/chat/message", json=body, timeout=180.0)
    data: dict[str, Any] = {}
    try:
        data = resp.json()
    except Exception:
        return f"<HTTP {resp.status_code}: {resp.text[:200]}>"
    return str(data.get("display_message") or data.get("message") or "")


def main() -> int:
    print(f"session={SESSION}\n  CONV_A={CONV_A}\n  CONV_B={CONV_B}\n")
    with httpx.Client() as client:
        print("[A1] tell secret ...")
        r = call(client, CONV_A, f"Please remember a detail for me: my reference code is {SECRET}. Just acknowledge it briefly.")
        print("   ->", r[:200], "\n")

        print("[A2] recall within SAME thread ...")
        r2 = call(client, CONV_A, "What reference code did I just give you? Reply with just the code.")
        print("   ->", r2[:200], "\n")

        print("[B3] ask in a NEW thread (must NOT know) ...")
        r3 = call(client, CONV_B, "What is my reference code? If you don't know it, say you don't have it.")
        print("   ->", r3[:200], "\n")

    a2_recalls = SECRET in r2
    b3_leaks = SECRET in r3

    print("=" * 60)
    print(f"A2 recalls {SECRET} (same-thread history intact): {'PASS' if a2_recalls else 'FAIL'}  -> {a2_recalls}")
    print(f"B3 does NOT leak {SECRET} (cross-thread isolation): {'PASS' if not b3_leaks else 'FAIL'}  -> leak={b3_leaks}")
    ok = a2_recalls and not b3_leaks
    print("RESULT:", "ALL PASS" if ok else "FAILURE")
    print(f"(verify DB tagging for session_id={SESSION})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
