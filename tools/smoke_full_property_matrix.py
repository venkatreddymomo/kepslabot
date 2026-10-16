"""
Full property matrix smoke test — exercises EVERY discussed behavior across many
Zone properties, one session per property, 20 confusing turns each.

Per turn it captures: reply, target service, action, active property scope,
booking_cta url, suggestion chips — and flags:
  - canned   : the hardcoded "I need one clarification" line appeared (BAD)
  - instay_leak : an in-stay action in pre-booking opened a form / routed to
                  support instead of being declined (BAD)
  - weather_staff : a weather question offered a staff callback (BAD)
  - link_ok  : a booking turn produced a booking_cta (and, when the property is
               pinned, the url matches that property's real page)
  - chips    : suggestion chips were returned

Run with the backend up on localhost:8011:
    python tools/smoke_full_property_matrix.py
    python tools/smoke_full_property_matrix.py --base http://localhost:8011 --concurrency 3

Writes tools/smoke_full_property_matrix_report.json + .md. NEVER submits a form
(no tickets created).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import uuid
from pathlib import Path
from typing import Any

import httpx

DEFAULT_BASE = "http://localhost:8011"
HOTEL = "zone"
REPORT_JSON = Path(__file__).parent / "smoke_full_property_matrix_report.json"
REPORT_MD = Path(__file__).parent / "smoke_full_property_matrix_report.md"

# (label, pin phrase used to scope the bot, expected booking-url slug substring)
PROPERTIES: list[tuple[str, str, str]] = [
    ("Kolkata", "Zone by The Park Kolkata", "zone-by-the-park-kolkata"),
    ("New Market Kolkata", "Zone by The Park New Market Kolkata", "new-market"),
    ("Jaipur", "Zone by The Park Jaipur", "zone-by-the-park-jaipur"),
    ("Calangute Goa", "Zone Connect Park Calangute Goa", "calangute"),
    ("Parra Goa", "Zone Connect Park Parra Goa", "parra"),
    ("Coimbatore (Park)", "Zone by The Park Coimbatore", "zone-by-the-park-coimbatore"),
    ("Coimbatore (Connect)", "Zone Connect Park Coimbatore", "zone-connect-by-the-park-coimbatore"),
    ("Jammu", "Zone by The Park Jammu", "zone-by-the-park-jammu"),
    ("Pathankot", "Zone by The Park Pathankot", "zone-by-the-park-pathankot"),
    ("Patna", "Zone Connect Park Patna", "zone-connect-by-the-park-patna"),
    ("Mussoorie", "Zone Connect Park Mussoorie", "zone-connect-by-the-park-mussoorie"),
    ("Saket Delhi", "Zone Connect Park Saket Delhi", "saket-delhi"),
    ("Chennai", "Zone by The Park Chennai", "zone-by-the-park-chennai"),
    ("Gopalpur", "Zone by The Park Gopalpur", "zone-by-the-park-gopalpur"),
    ("Dimapur", "Zone Niathu Park Dimapur", "dimapur"),
    ("Phalodi", "Zone Palace Park Phalodi", "phalodi"),
    ("Darjeeling", "Zone by The Park Darjeeling", "darjeeling"),
    ("Gangtok", "Zone Connect Park Gangtok", "gangtok"),
    ("Jaisalmer", "Zone Connect Park Jaisalmer", "jaisalmer"),
    ("Udaipur", "Zone Connect Park Udaipur", "udaipur"),
]

# 20-turn confusing script. {prop} = the pin phrase. Tags drive the per-turn checks.
TURNS: list[tuple[str, str]] = [
    ("hi", "greet"),
    ("tell me about {prop}", "info"),
    ("what room types do you have", "info"),
    ("what offers are available", "info"),
    ("i want to book a room", "book"),
    ("which room has a balcony", "info"),
    ("i need to clean my room", "instay"),
    ("book a spa appointment", "instay"),
    ("i want room service", "instay"),
    ("i have an issue with my bed", "instay"),
    ("book a table for dinner tonight", "instay"),
    ("ok i want to book the room now", "book"),
    ("give me the booking link", "book"),
    ("what's the weather like there", "weather"),
    ("can you book me a cab to the airport", "oos"),
    ("i want to talk to someone about my booking", "support"),
    ("tell me about all your other properties", "multi"),
    ("asdf book book pool spa now???", "confuse"),
    ("actually just the room booking link please", "book"),
    ("thanks, bye", "bye"),
]

CANNED = "one clarification to proceed"


def _reply(d: dict[str, Any]) -> str:
    data = d if isinstance(d, dict) else {}
    return str(data.get("display_message") or data.get("message") or "")


def _meta(d: dict[str, Any]) -> dict[str, Any]:
    data = d if isinstance(d, dict) else {}
    m = data.get("metadata") or {}
    e = m.get("entities") or {}
    cta = m.get("booking_cta")
    cta_urls: list[str] = []
    if isinstance(cta, dict):
        cta_urls = [str(cta.get("url") or "")]
    elif isinstance(cta, list):
        cta_urls = [str(c.get("url") or "") for c in cta if isinstance(c, dict)]
    chips = (
        data.get("suggestions")
        or data.get("chips")
        or m.get("suggestions")
        or m.get("suggested_actions")
        or e.get("suggested_actions")
        or []
    )
    chip_texts = [
        (c.get("text") if isinstance(c, dict) else str(c))
        for c in (chips if isinstance(chips, list) else [])
    ]
    return {
        "target": e.get("target_service_id") or "",
        "action": e.get("orchestration_action") or "",
        "active_ids": m.get("active_property_ids") or e.get("active_property_ids") or [],
        "form": bool(m.get("form_trigger")) or bool(e.get("missing_fields")) or (str(m.get("render_type") or "").lower() == "form"),
        "cta_urls": [u for u in cta_urls if u],
        "chips": [t for t in chip_texts if t],
    }


def _is_support(target: str) -> bool:
    return "support" in str(target or "").lower()


def _instay_declined(reply: str, m: dict[str, Any]) -> bool:
    low = reply.lower()
    during = any(p in low for p in (
        "during your stay", "once you", "after you arrive", "when you arrive",
        "after you check in", "once you're checked in", "once you have checked in",
        "after check-in", "while you're staying", "when you check in",
    ))
    refuse = any(p in low for p in ("can't", "cannot", "not able", "unable"))
    # declined = during-stay framing AND no form opened AND not routed to support
    return during and refuse and not m["form"] and not _is_support(m["target"])


async def run_property(client: httpx.AsyncClient, base: str, label: str, prop: str,
                       expect_slug: str, sem: asyncio.Semaphore) -> dict[str, Any]:
    async with sem:
        sid = f"matrix-{re.sub(r'[^a-z0-9]+','-',label.lower())}-{uuid.uuid4().hex[:6]}"
        turns_out: list[dict[str, Any]] = []
        flags = {"canned": 0, "instay_total": 0, "instay_leak": 0, "weather_staff": 0,
                 "link_turns": 0, "link_ok": 0, "link_wrong_prop": 0, "chips_turns": 0,
                 "http_err": 0}
        for msg_t, tag in TURNS:
            msg = msg_t.replace("{prop}", prop)
            body = {"session_id": sid, "message": msg, "hotel_code": HOTEL,
                    "channel": "web_widget",
                    "metadata": {"phase": "pre_booking", "chat_test_profile_phase": "pre_booking"}}
            try:
                r = await client.post(f"{base}/api/chat/message", json=body, timeout=180.0)
                d = r.json()
            except Exception as exc:
                flags["http_err"] += 1
                turns_out.append({"msg": msg, "tag": tag, "error": str(exc)[:120]})
                continue
            reply = _reply(d)
            m = _meta(d)
            low = reply.lower()
            rec = {"msg": msg, "tag": tag, "reply": reply[:200], "target": m["target"],
                   "action": m["action"], "active_ids": m["active_ids"],
                   "cta": m["cta_urls"], "chips": len(m["chips"])}
            if CANNED in low:
                flags["canned"] += 1
                rec["FLAG_canned"] = True
            if m["chips"]:
                flags["chips_turns"] += 1
            if tag == "instay":
                flags["instay_total"] += 1
                if not _instay_declined(reply, m):
                    flags["instay_leak"] += 1
                    rec["FLAG_instay_leak"] = True
            if tag == "weather":
                if any(p in low for p in ("team to reach out", "our team will", "connect you", "have our team", "someone reach out")):
                    flags["weather_staff"] += 1
                    rec["FLAG_weather_staff"] = True
            if tag == "book":
                flags["link_turns"] += 1
                if m["cta_urls"]:
                    flags["link_ok"] += 1
                    # When scope is the single pinned property, url should match it
                    if len(m["active_ids"]) == 1 and expect_slug and not any(expect_slug in u for u in m["cta_urls"]):
                        flags["link_wrong_prop"] += 1
                        rec["FLAG_link_wrong_prop"] = True
            turns_out.append(rec)
        return {"label": label, "prop": prop, "session": sid, "flags": flags, "turns": turns_out}


async def main_async(base: str, concurrency: int) -> int:
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient() as client:
        tasks = [run_property(client, base, lbl, prop, slug, sem) for (lbl, prop, slug) in PROPERTIES]
        results = []
        for i, coro in enumerate(asyncio.as_completed(tasks), 1):
            res = await coro
            results.append(res)
            f = res["flags"]
            print(f"[{i}/{len(PROPERTIES)}] {res['label']:<22} canned={f['canned']} "
                  f"instay_leak={f['instay_leak']}/{f['instay_total']} weather_staff={f['weather_staff']} "
                  f"link_ok={f['link_ok']}/{f['link_turns']} wrong_prop={f['link_wrong_prop']} "
                  f"chips_turns={f['chips_turns']}/20 http_err={f['http_err']}")
    results.sort(key=lambda r: r["label"])
    REPORT_JSON.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")

    # Aggregate
    agg = {k: 0 for k in ("canned", "instay_total", "instay_leak", "weather_staff",
                          "link_turns", "link_ok", "link_wrong_prop", "chips_turns", "http_err")}
    for r in results:
        for k in agg:
            agg[k] += r["flags"][k]
    total_turns = len(PROPERTIES) * len(TURNS)
    lines = ["# Full property matrix report", "",
             f"- properties: {len(PROPERTIES)} · turns/session: {len(TURNS)} · total turns: {total_turns}",
             f"- **canned 'clarification' lines:** {agg['canned']}  (target 0)",
             f"- **in-stay leaks (form/support instead of decline):** {agg['instay_leak']}/{agg['instay_total']}  (target 0)",
             f"- **weather staff-offer leaks:** {agg['weather_staff']}  (target 0)",
             f"- **booking link shown on booking turns:** {agg['link_ok']}/{agg['link_turns']}",
             f"- **booking link WRONG property:** {agg['link_wrong_prop']}  (target 0)",
             f"- **turns with suggestion chips:** {agg['chips_turns']}/{total_turns}",
             f"- **HTTP/transport errors:** {agg['http_err']}", "",
             "## Per property", "",
             "| Property | canned | instay leak | weather | link ok | wrong prop | chips turns | http err |",
             "|---|---|---|---|---|---|---|---|"]
    for r in results:
        f = r["flags"]
        lines.append(f"| {r['label']} | {f['canned']} | {f['instay_leak']}/{f['instay_total']} | "
                     f"{f['weather_staff']} | {f['link_ok']}/{f['link_turns']} | {f['link_wrong_prop']} | "
                     f"{f['chips_turns']}/20 | {f['http_err']} |")
    REPORT_MD.write_text("\n".join(lines), encoding="utf-8")
    print("\n" + "\n".join(lines[:9]))
    print(f"\n[matrix] json -> {REPORT_JSON}\n[matrix] md   -> {REPORT_MD}")
    bad = agg["canned"] + agg["instay_leak"] + agg["weather_staff"] + agg["link_wrong_prop"]
    print("RESULT:", "PASS" if bad == 0 else f"REVIEW ({bad} flagged behaviors)")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base", default=DEFAULT_BASE)
    p.add_argument("--concurrency", type=int, default=3)
    args = p.parse_args()
    return asyncio.run(main_async(args.base, args.concurrency))


if __name__ == "__main__":
    raise SystemExit(main())
