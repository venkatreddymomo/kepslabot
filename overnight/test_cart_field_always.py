"""When the bot says it added something, did it actually update the cart?

The cart is `pending_data_updates.order_items`, and the model must emit it.
Measured before this test: of 6 replies that claimed an item was added, 3 carried
no field at all - and the cart block is gated on the field being present
(llm_orchestration_grounding_mixin.py:3991), so on those turns the cart kept its
old contents while the guest was told otherwise. Ticket 11135 went to the kitchen
as Filter Coffee + Tandoori Murgh after the guest was shown "You've added 2 x
Chicken Burger".

This drives adds, removes and quantity changes through the real widget, then
reads back what the model emitted on each turn from new_bot_log_llm and reports,
per turn: did the reply claim a change, and was the cart updated to match.

    BASE_URL=http://localhost:8011 python overnight/test_cart_field_always.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "no_reask", str(ROOT / "overnight" / "test_e2e_no_reask_placed.py"))
_m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_m)          # this also wraps stdout for UTF-8
send, TA = _m.send, _m.TA

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8011")
HOTEL = "sarovar_portico_sriperumbudur"
OUT = ROOT / "overnight" / os.environ.get("RUN_TAG", "cart_field")
OUT.mkdir(parents=True, exist_ok=True)

# The reply asserts the order changed or states its contents.
CLAIMS_CHANGE = re.compile(
    r"you'?ve added|i'?ve added|added \d|added the|added to your (cart|order)"
    r"|to your cart|in your cart|your cart (now )?(contains|has)|removed|cancelled"
    r"|updated your order|order total|cart total|changed .{0,20}quantity", re.I)

TURNS = [
    "hi",
    "can i get 2 chicken burgers",
    "add a filter coffee also",
    "and a tiramisu",
    "what's in my cart",
    "actually make the burgers 3",
    "remove the tiramisu",
    "what's in my cart now",
    "add a garlic naan too",
    "ok place the order",
    "oh also add a phulka",
    "what did i order",
]


def log(m: str) -> None:
    print(m, flush=True)


def main() -> int:
    session_id = ""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=os.environ.get("UI_HEADLESS", "1") == "1")
        page = browser.new_page(viewport={"width": 1500, "height": 1000})
        page.set_default_timeout(60000)
        page.goto(f"{BASE_URL}/chat?hotel_code={HOTEL}&phase=during_stay",
                  wait_until="domcontentloaded")
        for _ in range(50):
            page.wait_for_timeout(1000)
            try:
                if "Mapped test profile active for during_stay" in page.inner_text("body"):
                    break
            except Exception:
                pass
        page.click("button:has-text('New Session')")
        page.wait_for_timeout(2500)
        try:
            page.click("button[aria-label='Open Chat']")
        except Exception:
            page.click("text='Need help? Chat with Kai'")
        page.wait_for_selector(TA, timeout=45000)
        page.wait_for_timeout(2000)
        try:
            m = re.search(r"Session:\s*(\S+)", page.inner_text("body"))
            session_id = m.group(1) if m else ""
        except Exception:
            pass
        log(f"session: {session_id}\n")

        replies = []
        for i, msg in enumerate(TURNS, 1):
            reply = send(page, msg)
            replies.append({"turn": i, "guest": msg, "bot": reply})
            log(f"[{i:02d}] > {msg}")
            log("     < " + reply.replace("\n", "\n       ")[:220] + "\n")
        page.screenshot(path=str(OUT / "final.png"), full_page=True)
        browser.close()

    # ── what did the model actually emit on each of those turns? ────────────
    from sqlalchemy import create_engine, text
    url = [l.split("=", 1)[1].strip() for l in open(ROOT / ".env", encoding="utf-8")
           if l.startswith("DATABASE_URL=")][0].replace("mysql+aiomysql", "mysql+pymysql")
    eng = create_engine(url, connect_args={"connect_timeout": 15})
    with eng.connect() as c:
        rows = c.execute(text(
            "SELECT id, output_text FROM new_bot_log_llm WHERE session_id=:s "
            "AND input_text LIKE '%%order_lifecycle%%' ORDER BY id ASC"), {"s": session_id}).fetchall()
    emitted = [("order_items" in (r.output_text or "")) for r in rows]

    log("=" * 74)
    log(f"{'TURN':<5}{'CLAIMS A CHANGE':<18}{'CART UPDATED':<15}RESULT")
    log("-" * 74)
    claims = bad = 0
    detail = []
    for i, rep in enumerate(replies):
        claimed = bool(CLAIMS_CHANGE.search(rep["bot"]))
        got = emitted[i] if i < len(emitted) else None
        if not claimed:
            verdict = "-"
        else:
            claims += 1
            verdict = "ok" if got else "LOST"
            bad += 0 if got else 1
        detail.append({**rep, "claimed_change": claimed, "cart_emitted": got,
                       "verdict": verdict})
        log(f"{rep['turn']:<5}{('yes' if claimed else 'no'):<18}"
            f"{(str(got) if got is not None else '?'):<15}{verdict}")
    log("-" * 74)
    log(f"replies claiming the order changed: {claims}")
    log(f"of those, cart NOT updated:         {bad}")
    log(f"\n{'PASS - the cart matched the words every time' if not bad else 'FAIL'}")
    (OUT / "detail.json").write_text(json.dumps(detail, indent=2, ensure_ascii=False),
                                     encoding="utf-8")
    log(f"session: {session_id}\nartifacts: {OUT}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
