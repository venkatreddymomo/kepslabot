"""
Side-by-side comparison: same 10 khil pre-booking questions sent to
- KePSLA endpoint  ->POST /api/chat/message
- Lumira  endpoint  ->POST /engage-bot/message

Captures the bot's reply for each question and writes a markdown report so
we can see whether the two paths produce the same answers.
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

BOT_BASE = "http://localhost:8011"
HOTEL_CODE = "khil"
QUESTIONS = [
    "tell me about your properties",
    "tell me about goa properties",
    "tell me about ira porvorim",
    "does porvorim have a pool",
    "what amenities does it have",
    "what room types are there",
    "tell me about heritage property",
    "do you have venues for wedding",
    "i want to book a banquet hall",
    "yes please",
]

REPORT_PATH = Path(__file__).parent / "comparison_report.md"
RAW_PATH = Path(__file__).parent / "comparison_raw.json"


def _kepsla_session_id() -> str:
    return f"cmp-nx-{uuid.uuid4().hex[:12]}"


def _lumira_session_id() -> str:
    return f"cmp-lm-{uuid.uuid4().hex[:12]}"


def call_kepsla(client: httpx.Client, session_id: str, message: str) -> dict[str, Any]:
    body = {
        "session_id": session_id,
        "message": message,
        "hotel_code": HOTEL_CODE,
        "channel": "web_widget",
        "metadata": {},
    }
    started = time.perf_counter()
    resp = client.post(f"{BOT_BASE}/api/chat/message", json=body, timeout=120.0)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    try:
        data = resp.json()
    except Exception:
        data = {"_parse_error": resp.text[:500]}
    return {
        "status": resp.status_code,
        "elapsed_ms": elapsed_ms,
        "data": data,
    }


def call_lumira(client: httpx.Client, session_id: str, message: str) -> dict[str, Any]:
    body = {
        "session_id": session_id,
        "message": message,
        "Bot_Id": 57,
        "widget_id": 57,
        "group_id": 611,
        "entity_id": 611,
        "hotel_code": HOTEL_CODE,
        "phase": "pre_booking",
        "channel": "web_widget",
        "conversation_id": f"conv-{session_id}",
        "metadata": {"phase": "pre_booking"},
    }
    headers = {"session_id": session_id}
    started = time.perf_counter()
    resp = client.post(
        f"{BOT_BASE}/engage-bot/message",
        json=body,
        headers=headers,
        timeout=120.0,
    )
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    try:
        data = resp.json()
    except Exception:
        data = {"_parse_error": resp.text[:500]}
    return {
        "status": resp.status_code,
        "elapsed_ms": elapsed_ms,
        "data": data,
    }


def _extract_nx_message(reply: dict[str, Any]) -> str:
    d = reply.get("data") or {}
    return str(d.get("display_message") or d.get("message") or "").strip()


def _extract_lm_message(reply: dict[str, Any]) -> str:
    d = reply.get("data") or {}
    return str(d.get("message") or "").strip()


def _extract_meta_summary_nx(reply: dict[str, Any]) -> dict[str, Any]:
    d = reply.get("data") or {}
    meta = d.get("metadata") or {}
    return {
        "service_llm_label": d.get("service_llm_label"),
        "service_llm_confidence": d.get("service_llm_confidence"),
        "state": d.get("state"),
        "phase": meta.get("phase"),
        "flow": meta.get("flow"),
        "ticket_source": meta.get("ticket_source"),
        "form_trigger": meta.get("form_trigger"),
        "render": meta.get("render"),
        "render_type": meta.get("render_type"),
        "orchestration_action": (meta.get("entities") or {}).get("orchestration_action"),
        "target_service_id": (meta.get("entities") or {}).get("target_service_id"),
        "missing_fields": (meta.get("entities") or {}).get("missing_fields"),
    }


def _extract_meta_summary_lm(reply: dict[str, Any]) -> dict[str, Any]:
    d = reply.get("data") or {}
    return {
        "render": d.get("render"),
        "render_type": d.get("render_type"),
        "is_ticket_intent": d.get("is_ticket_intent"),
    }


def main() -> int:
    nx_session = _kepsla_session_id()
    lm_session = _lumira_session_id()
    print(f"KePSLA session: {nx_session}", flush=True)
    print(f"Lumira  session: {lm_session}", flush=True)

    results: list[dict[str, Any]] = []

    with httpx.Client() as client:
        for i, q in enumerate(QUESTIONS, 1):
            print(f"\n[{i}/{len(QUESTIONS)}] Q: {q}", flush=True)
            print("  ->KePSLA...", end="", flush=True)
            try:
                nx = call_kepsla(client, nx_session, q)
                print(f" {nx['status']} ({nx['elapsed_ms']}ms)", flush=True)
            except Exception as exc:
                print(f" ERROR: {exc!r}", flush=True)
                nx = {"status": -1, "elapsed_ms": 0, "data": {"_error": str(exc)}}

            print("  ->Lumira ...", end="", flush=True)
            try:
                lm = call_lumira(client, lm_session, q)
                print(f" {lm['status']} ({lm['elapsed_ms']}ms)", flush=True)
            except Exception as exc:
                print(f" ERROR: {exc!r}", flush=True)
                lm = {"status": -1, "elapsed_ms": 0, "data": {"_error": str(exc)}}

            results.append({
                "turn": i,
                "question": q,
                "kepsla": nx,
                "lumira": lm,
            })
            time.sleep(0.5)  # be gentle on the LLM

    # Persist raw
    RAW_PATH.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"\nRaw → {RAW_PATH}", flush=True)

    # Markdown report
    lines: list[str] = []
    lines.append("# KePSLA vs Lumira — khil pre_booking comparison")
    lines.append("")
    lines.append(f"- KePSLA session: `{nx_session}`")
    lines.append(f"- Lumira  session: `{lm_session}`")
    lines.append(f"- Hotel: `{HOTEL_CODE}`")
    lines.append(f"- Run at: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")

    for r in results:
        nx_msg = _extract_nx_message(r["kepsla"])
        lm_msg = _extract_lm_message(r["lumira"])
        nx_meta = _extract_meta_summary_nx(r["kepsla"])
        lm_meta = _extract_meta_summary_lm(r["lumira"])

        lines.append("---")
        lines.append("")
        lines.append(f"## Turn {r['turn']} — Q: *{r['question']}*")
        lines.append("")
        lines.append(f"- KePSLA  HTTP {r['kepsla']['status']} · {r['kepsla']['elapsed_ms']} ms")
        lines.append(f"- Lumira   HTTP {r['lumira']['status']} · {r['lumira']['elapsed_ms']} ms")
        lines.append("")
        lines.append("### KePSLA reply")
        lines.append("```")
        lines.append(nx_msg or "(empty)")
        lines.append("```")
        lines.append("")
        lines.append("### Lumira reply")
        lines.append("```")
        lines.append(lm_msg or "(empty)")
        lines.append("```")
        lines.append("")
        lines.append("### KePSLA metadata")
        lines.append("```json")
        lines.append(json.dumps(nx_meta, indent=2, default=str))
        lines.append("```")
        lines.append("")
        lines.append("### Lumira metadata")
        lines.append("```json")
        lines.append(json.dumps(lm_meta, indent=2, default=str))
        lines.append("```")
        lines.append("")

    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"Report → {REPORT_PATH}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
