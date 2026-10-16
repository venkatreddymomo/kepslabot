"""
Smoke test for two fixes (run against live backend, zone hotel, pre_booking):

  FIX 1 — image property leak:
    A Pathankot conversation whose reply mentions "Jammu & Kashmir" must NOT
    surface a Jammu image. Image scope must come from the orchestrator's active
    property (FOCUS), not a reply-text scan. We assert:
      - smart_image_active_property_hint_source is a FOCUS source (not text scan)
      - any attached image belongs to the focused property, never another one.

  FIX 2 — Issue Details prefill:
    After a venue/birthday discussion + "yes pls", the support form's
    "Issue Details" textarea must be PRE-FILLED with an on-topic value
    (anchored to the prior assistant proposal, not the generic form-open line).

Does NOT submit the form, so no ticket is created.

    python tools/smoke_image_form_fixes.py
"""
from __future__ import annotations

import argparse
import json
import re
import uuid
from pathlib import Path
from typing import Any

import httpx

DEFAULT_BASE = "http://localhost:8011"
DEFAULT_HOTEL = "zone"
REPORT = Path(__file__).parent / "smoke_image_form_fixes_report.json"


def send(client, base, hotel, sid, msg):
    r = client.post(
        f"{base}/api/chat/message",
        json={
            "session_id": sid,
            "message": msg,
            "hotel_code": hotel,
            "channel": "web_widget",
            "metadata": {"phase": "pre_booking"},
        },
        timeout=180.0,
    )
    d = r.json()
    meta = d.get("metadata") or {}
    return {
        "msg": msg,
        "reply": (d.get("display_message") or d.get("message") or "").strip(),
        "meta": meta,
    }


def find_field(form_fields, *hints):
    for f in form_fields or []:
        blob = f"{f.get('id') or ''} {f.get('label') or ''}".lower()
        if any(h in blob for h in hints):
            return f
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--hotel", default=DEFAULT_HOTEL)
    args = ap.parse_args()

    failures: list[str] = []
    report: dict[str, Any] = {}

    with httpx.Client() as client:
        # ---- FIX 1: image scope (Pathankot reply mentioning Jammu) ----
        print("\n=== FIX 1: Pathankot reply (mentions Jammu & Kashmir) must not leak a Jammu image ===")
        sid = f"img-{uuid.uuid4().hex[:8]}"
        turns1 = []
        for m in [
            "I'm exploring Pathankot — tell me about the hotel there and what makes it special.",
            "tell me about the dining options there",
        ]:
            t = send(client, args.base, args.hotel, sid, m)
            turns1.append({
                "msg": m,
                "reply": t["reply"][:200],
                "hint_source": t["meta"].get("smart_image_active_property_hint_source"),
                "active_property_id": t["meta"].get("smart_image_active_property_id"),
                "active_property_ids": t["meta"].get("smart_image_active_property_ids"),
                "selection_reason": t["meta"].get("smart_image_selection_reason"),
                "images": [
                    {
                        "title": (im.get("title") or "")[:60],
                        "property_name": im.get("property_name"),
                        "property_id": im.get("property_id"),
                        "url": (im.get("image_url") or im.get("url") or "")[:80],
                    }
                    for im in (t["meta"].get("images") or [])
                ],
            })
            print(f"   >> {m}")
            print(f"      source={turns1[-1]['hint_source']} pid={turns1[-1]['active_property_id']} reason={turns1[-1]['selection_reason']}")
            for im in turns1[-1]["images"]:
                print(f"        IMG: {im['title']!r} prop={im['property_name']!r}({im['property_id']}) {im['url']}")
        report["fix1_image_scope"] = turns1

        # Assert: no image anywhere belongs to a NON-Pathankot property.
        for tr in turns1:
            for im in tr["images"]:
                pname = str(im.get("property_name") or "")
                purl = str(im.get("url") or "")
                blob = f"{pname} {purl} {im.get('title') or ''}".lower()
                if "jammu" in blob and "pathankot" not in blob:
                    failures.append(
                        f"FIX1: leaked a JAMMU image into a Pathankot turn: {im}"
                    )
                # Generic cross-property guard: property_name set but not Pathankot
                if pname and "pathankot" not in pname.lower():
                    failures.append(
                        f"FIX1: image from non-Pathankot property leaked: prop={pname!r}"
                    )
            # The reply-text scan must NOT be the source when a FOCUS exists.
            if tr["hint_source"] == "assistant_text_fallback_multi":
                failures.append(
                    f"FIX1: scope came from multi text-scan (leak path) source={tr['hint_source']}"
                )

        # ---- FIX 2: Issue Details prefill after birthday/venue + "yes pls" ----
        print("\n=== FIX 2: 'Issue Details' must be pre-filled from the prior topic (birthday/banquet) ===")
        sid2 = f"frm-{uuid.uuid4().hex[:8]}"
        flow = [
            "tell me about zone park pathankot",
            "do you have a banquet hall or venue for events?",
            "i want to host my friend's birthday party here, im confused what to do",
            "yes pls",
        ]
        turns2 = []
        issue_field = None
        for m in flow:
            t = send(client, args.base, args.hotel, sid2, m)
            ff = t["meta"].get("form_fields") or []
            rec = {
                "msg": m,
                "reply": t["reply"][:200],
                "form_trigger": bool(t["meta"].get("form_trigger")),
                "form_field_ids": [f.get("id") for f in ff],
                "issue_default": "",
            }
            f_issue = find_field(ff, "issue", "details", "description", "reason")
            if f_issue:
                rec["issue_default"] = str(f_issue.get("default_value") or "")
                issue_field = f_issue
            turns2.append(rec)
            print(f"   >> {m}")
            print(f"      form={rec['form_trigger']} fields={rec['form_field_ids']}")
            if rec["issue_default"]:
                print(f"      ISSUE_DETAILS default = {rec['issue_default']!r}")
        report["fix2_form_prefill"] = turns2

        last = turns2[-1]
        if not last["form_trigger"]:
            failures.append("FIX2: the form did not open on 'yes pls' — cannot evaluate prefill.")
        elif issue_field is None:
            failures.append("FIX2: no Issue Details field present on the opened form.")
        else:
            val = str(issue_field.get("default_value") or "").strip()
            if not val:
                failures.append("FIX2: Issue Details default_value is BLANK (not auto-filled).")
            else:
                # On-topic check: should reference the discussed subject.
                low = val.lower()
                if not any(k in low for k in ("birthday", "party", "banquet", "venue", "event", "celebrat", "townhall", "pathankot")):
                    failures.append(
                        f"FIX2: Issue Details filled but OFF-topic: {val!r}"
                    )

    REPORT.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\n[smoke] report -> {REPORT}")
    print("=" * 60)
    if failures:
        print("RESULT: FAIL")
        for f in failures:
            print("  -", f)
        return 1
    print("RESULT: PASS — image scope stays on the focused property; Issue Details is auto-filled on-topic.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
