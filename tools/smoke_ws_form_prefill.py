"""
Smoke test over the REAL widget transport (WebSocket /ws/chat).

Reproduces the reported failure: a missing-info handoff ("do u have doctors")
-> "yes" opens the support form, and the "Issue Details" textarea must be
PRE-FILLED from the conversation (it was blank because the WS path passed no
context to the form-trigger prefill).

Also re-checks the image scope fix over WS (Mussoorie turn stays on its own
property, no cross-property image).

    python tools/smoke_ws_form_prefill.py
"""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import websockets

WS_URL = "ws://localhost:8011/ws/chat"
HOTEL = "zone"
REPORT = Path(__file__).parent / "smoke_ws_form_prefill_report.json"


async def recv_response(ws, timeout=180.0) -> dict[str, Any]:
    """Read frames until the 'response' frame arrives; ignore typing/connected/etc."""
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        try:
            frame = json.loads(raw)
        except Exception:
            continue
        if frame.get("type") == "response":
            return frame.get("data") or {}
        # ignore: connected, typing, pong, session_*, system_handover, error(log)
        if frame.get("type") == "error":
            return {"_error": frame.get("detail")}


async def send(ws, sid, msg) -> dict[str, Any]:
    await ws.send(json.dumps({
        "type": "message",
        "message": msg,
        "session_id": sid,
        "hotel_code": HOTEL,
        "channel": "web_widget",
        "metadata": {"phase": "pre_booking"},
    }))
    data = await recv_response(ws)
    meta = data.get("metadata") or {}
    return {
        "reply": (data.get("display_message") or data.get("message") or "").strip(),
        "meta": meta,
    }


def find_field(form_fields, *hints):
    for f in form_fields or []:
        blob = f"{f.get('id') or ''} {f.get('label') or ''}".lower()
        if any(h in blob for h in hints):
            return f
    return None


async def main() -> int:
    failures: list[str] = []
    report: dict[str, Any] = {}

    sid = f"ws-{uuid.uuid4().hex[:8]}"
    url = f"{WS_URL}?session_id={sid}&hotel_code={HOTEL}&channel=web_widget"
    async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
        # drain the initial 'connected' frame
        try:
            await asyncio.wait_for(ws.recv(), timeout=10)
        except Exception:
            pass

        flow = [
            "I'm exploring Mussoorie — tell me about the hotel there and what makes it special.",
            "do you have doctors on call?",
            "yes",
        ]
        turns = []
        issue_field = None
        for m in flow:
            t = await send(ws, sid, m)
            ff = t["meta"].get("form_fields") or []
            f_issue = find_field(ff, "issue", "details", "description", "reason")
            rec = {
                "msg": m,
                "reply": t["reply"][:220],
                "form_trigger": bool(t["meta"].get("form_trigger")),
                "form_field_ids": [f.get("id") for f in ff],
                "issue_default": str(f_issue.get("default_value") or "") if f_issue else "",
                # image scope diagnostics
                "img_hint_source": t["meta"].get("smart_image_active_property_hint_source"),
                "img_pid": t["meta"].get("smart_image_active_property_id"),
                "images": [
                    {"title": (im.get("title") or "")[:50], "prop": im.get("property_name")}
                    for im in (t["meta"].get("images") or [])
                ],
            }
            if f_issue:
                issue_field = f_issue
            turns.append(rec)
            print(f">> {m}")
            print(f"   form={rec['form_trigger']} fields={rec['form_field_ids']} img_src={rec['img_hint_source']} pid={rec['img_pid']}")
            for im in rec["images"]:
                print(f"     IMG {im['title']!r} prop={im['prop']!r}")
            if rec["issue_default"]:
                print(f"   ISSUE_DETAILS = {rec['issue_default']!r}")
        report["ws_doctor_flow"] = turns

        # image scope: nothing from a non-Mussoorie property
        for tr in turns:
            for im in tr["images"]:
                p = str(im.get("prop") or "")
                if p and "mussoorie" not in p.lower():
                    failures.append(f"IMG: cross-property image leaked: prop={p!r}")

        last = turns[-1]
        if not last["form_trigger"]:
            failures.append("FORM: 'yes' did not open the form over WS — cannot evaluate prefill.")
        elif issue_field is None:
            failures.append("FORM: no Issue Details field on the opened form.")
        else:
            val = str(issue_field.get("default_value") or "").strip()
            if not val:
                failures.append("FORM: Issue Details is BLANK over WS (prefill still not working).")
            elif "dighashree" in val.lower() or "auditorium" in val.lower():
                failures.append(f"FORM: Issue Details is the hallucinated prompt-example value: {val!r}")
            else:
                low = val.lower()
                if not any(k in low for k in ("doctor", "mussoorie", "medical", "physician")):
                    failures.append(f"FORM: Issue Details filled but OFF-topic (expected doctor/Mussoorie): {val!r}")

    REPORT.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\n[smoke] report -> {REPORT}")
    print("=" * 60)
    if failures:
        print("RESULT: FAIL")
        for f in failures:
            print("  -", f)
        return 1
    print("RESULT: PASS — over the real WS path, Issue Details prefills on-topic and image scope stays correct.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
