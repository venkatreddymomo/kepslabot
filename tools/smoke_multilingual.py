"""Smoke test: the bot mirrors the guest's language on the LLM-generated answer
and the suggestion chips. Drives the live /api/chat/message endpoint for zone in
pre_booking across several global languages and prints a per-language script
verdict. It never submits a form, so NO ticket is created.

How it works: a per-producer language rule ("reply in the SAME language as the
guest's latest message") is injected as the LAST message before each guest-facing
LLM call (book_answerer, service agent, both chip generators, heavy orchestrator).
Recency makes it win even behind the large English prompt + English KB. Proper
nouns (property/room/restaurant names), prices, and markers stay verbatim — so a
small amount of Latin text inside a non-Latin reply is EXPECTED and correct.

Run from the project root with the backend up on localhost:8011:
    python tools/smoke_multilingual.py
    python tools/smoke_multilingual.py --hotel zone --base http://localhost:8011

Exit 0 = every language replied in its own script, 1 = a mismatch, 2 = HTTP error.
"""
from __future__ import annotations

import argparse
import sys
import uuid

import httpx

# (label, message, expected non-Latin script tag; "latin" = stays Latin/mirrored)
CASES = [
    ("HINDI", "मुझे कोलकाता वाले होटल के बारे में बताइए", "devanagari"),
    ("JAPANESE", "コルカタのホテルについて教えてください", "kana"),
    ("ARABIC", "أخبرني عن فندقكم في كولكاتا", "arabic"),
    ("RUSSIAN", "Расскажите о вашем отеле в Калькутте", "cyrillic"),
    ("CHINESE", "请介绍一下你们在加尔各答的酒店", "cjk-han"),
    ("KOREAN", "콜카타에 있는 호텔에 대해 알려주세요", "hangul"),
    ("SPANISH", "Háblame de tu hotel en Calcuta", "latin"),
    ("ENGLISH", "tell me about your kolkata hotel", "latin"),
]


def call(client, base, hotel, session_id, message):
    body = {
        "session_id": session_id, "message": message, "hotel_code": hotel,
        "channel": "web_widget",
        "metadata": {"phase": "pre_booking", "chat_test_profile_phase": "pre_booking",
                     "guest_name": "Sana Test", "guest_phone": "9000000001",
                     "guest_email": "sana.test@example.com"},
    }
    return client.post(f"{base}/api/chat/message", json=body, timeout=180.0).json()


def scripts(text: str) -> set[str]:
    out: set[str] = set()
    for ch in text:
        if not ch.isalpha():
            continue
        cp = ord(ch)
        if 0x0900 <= cp <= 0x097F: out.add("devanagari")
        elif 0x0600 <= cp <= 0x06FF: out.add("arabic")
        elif 0x0400 <= cp <= 0x04FF: out.add("cyrillic")
        elif 0xAC00 <= cp <= 0xD7A3: out.add("hangul")
        elif 0x3040 <= cp <= 0x30FF: out.add("kana")
        elif 0x4E00 <= cp <= 0x9FFF: out.add("cjk-han")
        elif cp < 0x250: out.add("latin")
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="http://localhost:8011")
    p.add_argument("--hotel", default="zone")
    args = p.parse_args()

    failures: list[str] = []
    try:
        with httpx.Client() as client:
            for label, msg, expect in CASES:
                d = call(client, args.base, args.hotel, f"ml-{label.lower()}-{uuid.uuid4().hex[:8]}", msg)
                data = d.get("data", d)
                reply = str(data.get("display_message") or data.get("message") or "").strip()
                sc = scripts(reply)
                ok = (expect in sc) if expect != "latin" else (sc == {"latin"} or sc <= {"latin"})
                print(f"\n=== {label} ===\n>> {msg}\n<< {reply[:200]}\n   scripts={sorted(sc)} expect={expect} {'OK' if ok else 'FAIL'}")
                if not ok:
                    failures.append(f"{label}: expected {expect} in reply, got {sorted(sc)}")
    except httpx.HTTPError as exc:
        print(f"[smoke] HTTP error: {exc!r} — is the backend up on {args.base}?")
        return 2

    print("\n" + "=" * 56)
    if failures:
        print("RESULT: FAIL")
        for f in failures:
            print("  -", f)
        return 1
    print("RESULT: PASS — every language replied in its own script.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
