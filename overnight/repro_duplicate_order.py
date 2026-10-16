"""Reproduce the duplicate-food ticket in five turns.

The 50-turn run produced a second kitchen ticket carrying the FIRST order's
burgers. There is a guard for exactly that (`_ird_closed_items`), it did not drop
anything, and the log could not say whether it ran with an empty set or never ran
at all. This replays the shortest sequence that reaches it.

Judged on the TICKETS, not the prose: ticket 2 must not contain a dish from
ticket 1 that the guest did not ask for again.
"""
from __future__ import annotations

import datetime, io, json, os, re, sys
from pathlib import Path
from playwright.sync_api import sync_playwright
from sqlalchemy import create_engine, text

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8011")
HOTEL = "sarovar_portico_sriperumbudur"
HEADLESS = os.environ.get("UI_HEADLESS", "0") == "1"
TA = 'textarea[placeholder="Type a message..."]'
SEND = "button[aria-label='Send message']"

# The five-turn version did NOT reproduce: the model only re-declares its whole
# running order sometimes. This replays the 50-turn run's actual sequence, which
# includes the coffee that was offered-but-never-placed - the lingering unplaced
# line looks like the thing that gives the re-declaration something to carry.
TURNS = [
    "order 2 chicken burger",
    "yes place it",
    "what did i order",
    "is my food coming?",
    "add a filter coffee to that",
    "yes",
    "what did i order",
    "how much is the arrabbiata pasta",
    "does the arrabbiata have nuts?",
    "i'm allergic to dairy",
    "recommend something without dairy",
    "what did i order",
    "i want to place a new order - 2 arrabbiata pasta",
    # The verdict is the TICKET, so the second order has to actually be placed.
    # Without this the run ends on "shall I place it?" and proves nothing.
    "yes place it",
    "what did i order",
]


def msg_count(page):
    m = re.search(r"Messages:\s*(\d+)", page.inner_text("body"))
    return int(m.group(1)) if m else -1


def send(page, t):
    b = msg_count(page)
    page.fill(TA, t); page.click(SEND)
    for _ in range(100):
        page.wait_for_timeout(1000)
        if msg_count(page) > b + 1:
            break
    page.wait_for_timeout(2200)
    bub = [x.strip() for x in page.locator("div.flex.justify-start").all_inner_texts() if x.strip()]
    return bub[-1] if bub else ""


def main():
    sid = ""
    with sync_playwright() as pw:
        br = pw.chromium.launch(headless=HEADLESS, slow_mo=150)
        pg = br.new_page(viewport={"width": 1400, "height": 950})
        pg.set_default_timeout(60000)
        pg.goto(f"{BASE_URL}/chat?hotel_code={HOTEL}&phase=during_stay", wait_until="domcontentloaded")
        for _ in range(50):
            pg.wait_for_timeout(1000)
            try:
                if "Mapped test profile active for during_stay" in pg.inner_text("body"):
                    break
            except Exception:
                pass
        pg.click("button:has-text('New Session')"); pg.wait_for_timeout(2500)
        try:
            pg.click("button[aria-label='Open Chat']")
        except Exception:
            pg.click("text='Need help? Chat with Kai'")
        pg.wait_for_selector(TA, timeout=45000); pg.wait_for_timeout(2000)
        m = re.search(r"Session:\s*(\S+)", pg.inner_text("body"))
        sid = m.group(1) if m else ""
        print(f"session: {sid}\n", flush=True)
        for t in TURNS:
            r = send(pg, t)
            print(f"> {t}\n< {r[:320]}\n", flush=True)
        br.close()

    url = [l.split("=", 1)[1].strip() for l in open(ROOT / ".env", encoding="utf-8")
           if l.startswith("DATABASE_URL=")][0].replace("mysql+aiomysql", "mysql+pymysql")
    e = create_engine(url, connect_args={"connect_timeout": 10})
    with e.connect() as c:
        rows = [dict(r._mapping) for r in c.execute(text(
            "SELECT ticket_ref,issue FROM new_bot_tickets WHERE session_id=:s ORDER BY id"),
            {"s": sid})]
    print("=" * 66)
    for r in rows:
        print(f"  {r['ticket_ref']}  {r['issue']}")
    bad = False
    if len(rows) >= 2:
        second = str(rows[1]["issue"]).lower()
        if "chicken burger" in second:
            bad = True
            print("\nFAIL: ticket 2 carries the Chicken Burger from ticket 1")
    if not bad and len(rows) >= 2:
        print("\nPASS: ticket 2 carries only the new dish")
    elif len(rows) < 2:
        print(f"\nINCONCLUSIVE: expected 2 tickets, got {len(rows)}")
    print(f"\nsession: {sid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
