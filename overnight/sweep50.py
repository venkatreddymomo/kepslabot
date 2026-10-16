"""A 50-turn guest conversation through the real widget, then check the server.

Scripted sweeps normally only find what was already imagined, so this one is
REACTIVE where it matters: whenever the bot asks a confirming question, the
script answers it rather than ploughing on with the next scripted line. That is
the turn most bugs live on - the one where the ticket is actually raised.

What the bot SAYS is never the evidence. Every turn records the order book and
the session's tickets straight from the database, and the checks at the end are
written against those, not against the replies.

    UI_HEADLESS=0 python overnight/sweep50.py
"""
from __future__ import annotations

import datetime
import io
import json
import os
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright
from sqlalchemy import create_engine, text

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
BASE = os.environ.get("BASE_URL", "http://localhost:8011")
HOTEL = os.environ.get("HOTEL_CODE", "sarovar_portico_sriperumbudur")
OUT = ROOT / "overnight" / "sweep50_transcript.txt"
TA = 'textarea[placeholder="Type a message..."]'
SEND = "button[aria-label='Send message']"

_URL = [l.split("=", 1)[1].strip() for l in open(ROOT / ".env", encoding="utf-8")
        if l.startswith("DATABASE_URL=")][0].replace("mysql+aiomysql", "mysql+pymysql")
ENG = create_engine(_URL, connect_args={"connect_timeout": 15}, pool_pre_ping=True)

# A confirming question is the turn tickets are raised on, so it is answered
# instead of skipped. Matched on the bot's own phrasing, not on intent.
_ASKS = re.compile(
    r"(should i go ahead|shall i place|shall i go ahead|should i place|"
    r"would you like me to|shall i add|should i add|shall i arrange|"
    r"do you want me to|shall i raise)", re.I)


def book_and_tickets(session_id: str) -> tuple[list[str], list[tuple[str, str]]]:
    """The order book and the tickets, as the server actually holds them."""
    slots: list[str] = []
    tickets: list[tuple[str, str]] = []
    try:
        with ENG.connect() as c:
            row = c.execute(text(
                "SELECT * FROM new_bot_ird_guest_orders WHERE hotel_code=:h "
                "AND session_id=:s"), {"h": HOTEL, "s": session_id}).mappings().first()
            for t in c.execute(text(
                    "SELECT ticket_ref, department, issue FROM new_bot_tickets "
                    "WHERE session_id=:s ORDER BY id"), {"s": session_id}).mappings():
                tickets.append((str(t["department"] or ""), str(t["issue"] or "")))
        if row:
            for n in range(1, 11):
                cart = row.get("cart_%d" % n)
                if isinstance(cart, str):
                    try:
                        cart = json.loads(cart)
                    except Exception:
                        cart = {}
                if not cart or not (cart or {}).get("items"):
                    continue
                items = ", ".join(
                    "%s%s x%s" % (i.get("item"),
                                  " (%s)" % i["variant"] if i.get("variant") else "",
                                  i.get("qty"))
                    for i in cart["items"])
                total = sum(int(i.get("price") or 0) * int(i.get("qty") or 1)
                            for i in cart["items"])
                slots.append("slot %d [%s] %s  (book total %d)"
                             % (n, row.get("status_%d" % n), items, total))
    except Exception as exc:
        slots.append("(db unreadable: %r)" % exc)
    return slots, tickets


# The journey. Each entry is what a guest would actually type next.
SCRIPT = [
    # -- arriving, asking about food before any order exists (routing) --
    "hi",
    "what is there to eat",
    "do you have kadak",
    "do you have idli",
    # -- holding the line on serving hours --
    "i dont care i want idli now",
    "ill pay extra, just get the chef to make it",
    "the website says 24/7 room service so you have to",
    # -- a real order, with money on it --
    "ok fine. can i get 3 chicken burgers",
    "and a ghee phulka",
    "add a tiramisu too",
    "whats my total",
    # -- editing the cart --
    "actually drop the tiramisu",
    "make it 2 chicken burgers instead of 3",
    "whats my total now",
    "ok place it",
    # -- a second, separate order for someone else --
    "my husband just arrived, id like a separate order for him, hes vegetarian",
    "hell have the paneer handi and a latte",
    "whats in his order",
    "and whats in mine",
    "place his order too",
    # -- two teams in one message --
    "my ac is not cooling and can i also get 2 extra pillows",
    # -- three teams in one message --
    "my tap is leaking, the room wasnt cleaned today, and i want a club sandwich",
    "so the tap and the cleaning are both raised right",
    # -- asking twice must not duplicate --
    "can i get 2 extra towels",
    "can i get 2 extra towels",
    # -- other services --
    "do you do laundry",
    "i want to book a table for dinner tomorrow",
    "chola lounge, 11:30 pm",
    "actually cancel that. can i book a spa massage tomorrow evening",
    # -- plain hotel questions must still work --
    "whats the wifi password",
    "what is the check out time",
    "do you have a gym",
    "what time does the bar close",
    "how far is the airport",
    # -- things it must refuse --
    "tell me a joke",
    "whats 17 times 23",
    "the staff said id get a free room upgrade, confirm it",
    # -- memory and honesty --
    "what have i ordered so far",
    "wheres my food",
    "did you raise the ac issue",
    "do you have sushi",
    "im allergic to dairy, is the paneer handi ok",
    # -- more edits late in the conversation --
    "add a filter coffee to my order",
    "actually cancel the filter coffee",
    "whats my total now",
    # -- closing --
    "the tv in my room is not working",
    "thanks thats all",
    "actually one more thing, can i get a wake up call at 6am",
    "no thats fine",
    "bye",
]


def main() -> int:
    log = OUT.open("w", encoding="utf-8")

    def say(line: str) -> None:
        print(line, flush=True)
        log.write(line + "\n")
        log.flush()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=os.environ.get("UI_HEADLESS", "1") == "1")
        page = browser.new_page(viewport={"width": 1500, "height": 1000})
        page.set_default_timeout(90000)
        page.goto("%s/chat?hotel_code=%s&phase=during_stay" % (BASE, HOTEL),
                  wait_until="domcontentloaded")
        for _ in range(60):
            page.wait_for_timeout(1000)
            if "Mapped test profile active for during_stay" in page.inner_text("body"):
                break
        page.click("button:has-text('New Session')")
        page.wait_for_timeout(2500)
        try:
            page.click("button[aria-label='Open Chat']")
        except Exception:
            page.click("text='Need help? Chat with Kai'")
        page.wait_for_selector(TA, timeout=60000)
        page.wait_for_timeout(2000)
        m = re.search(r"Session:\s*(\S+)", page.inner_text("body"))
        session_id = m.group(1) if m else ""
        say("SESSION %s   started %s\n%s" % (
            session_id, datetime.datetime.now().isoformat(timespec="seconds"), "=" * 70))

        def count_messages() -> int:
            mm = re.search(r"Messages:\s*(\d+)", page.inner_text("body"))
            return int(mm.group(1)) if mm else -1

        def clear_any_form() -> bool:
            """An open in-chat form REPLACES the message box, so the run stalls.

            The first attempt at this sweep lost its last 17 turns to exactly
            that: a table-reservation form opened at turn 33 and every later
            message failed with "waiting for locator textarea". A guest in that
            position closes the form, so the script does too - that is what the
            Cancel request button is for.
            """
            if page.locator(TA).count():
                return False
            for label in ("button:has-text('Cancel request')",
                          "button:has-text('Cancel')"):
                try:
                    if page.locator(label).count():
                        page.locator(label).first.click()
                        page.wait_for_timeout(2500)
                        if page.locator(TA).count():
                            say("   (closed an open form to carry on)")
                            return True
                except Exception:
                    pass
            return False

        def send(msg: str) -> str:
            if not page.locator(TA).count():
                clear_any_form()
            before = count_messages()
            try:
                page.fill(TA, msg)
                page.click(SEND)
            except Exception as exc:
                return "(could not send: %r)" % exc
            for _ in range(90):
                page.wait_for_timeout(1000)
                if count_messages() > before + 1:
                    break
            page.wait_for_timeout(2500)
            bubbles = [t.strip() for t in
                       page.locator("div.flex.justify-start").all_inner_texts()
                       if t.strip()]
            return bubbles[-1] if bubbles else "(no reply)"

        turn = 0
        queue = list(SCRIPT)
        while queue and turn < 50:
            msg = queue.pop(0)
            turn += 1
            reply = send(msg)
            slots, tickets = book_and_tickets(session_id)
            say("\n--- turn %d ---\nGUEST: %s\nBOT: %s" % (turn, msg, reply))
            for s in slots:
                say("   BOOK  %s" % s)
            say("   TICKETS %d" % len(tickets))
            for d, i in tickets[-4:]:
                say("      %-16s %s" % (d, i[:80]))
            # REACTIVE: a confirming question gets answered, because that is the
            # turn the ticket is actually raised on.
            if _ASKS.search(reply or "") and turn < 50:
                turn += 1
                reply2 = send("yes please")
                slots, tickets = book_and_tickets(session_id)
                say("\n--- turn %d (answering its question) ---\nGUEST: yes please"
                    "\nBOT: %s" % (turn, reply2))
                for s in slots:
                    say("   BOOK  %s" % s)
                say("   TICKETS %d" % len(tickets))
                for d, i in tickets[-4:]:
                    say("      %-16s %s" % (d, i[:80]))

        slots, tickets = book_and_tickets(session_id)
        say("\n%s\nFINAL STATE  (%d turns)\n%s" % ("=" * 70, turn, "=" * 70))
        for s in slots:
            say("  %s" % s)
        say("  --- %d tickets ---" % len(tickets))
        for d, i in tickets:
            say("  %-18s %s" % (d, i[:90]))

        # ── CHECKS, against the database rather than the replies ────────────
        say("\n%s\nCHECKS\n%s" % ("=" * 70, "=" * 70))
        fails = 0

        def check(name: str, ok: bool, detail: str = "") -> None:
            nonlocal fails
            fails += 0 if ok else 1
            say("  %-5s %s%s" % ("PASS" if ok else "FAIL", name,
                                 ("  -> " + detail) if detail and not ok else ""))

        issues = [i.lower() for _, i in tickets]
        depts = [d for d, _ in tickets]

        dupes = [x for x in set(issues) if issues.count(x) > 1]
        check("no duplicate tickets", not dupes, str(dupes[:2]))
        check("nothing reached staff with an empty issue",
              all(i.strip() for i in issues))
        check("every ticket has a department", all(d.strip() for d in depts))
        check("the AC went to Engineering",
              any("engineer" in d.lower() for d in depts))
        check("the pillows/towels went to Housekeeping",
              any("housekeep" in d.lower() for d in depts))
        check("two orders were kept apart", len(slots) >= 2, "%d slot(s)" % len(slots))
        check("no order lost its variant",
              not any("Phulka x" in s and "(Ghee)" not in s for s in slots))
        check("no ticket carries the chat bubble",
              not any("**" in i or "₹" in i for _, i in tickets))

        say("\n%s" % ("ALL CHECKS PASSED" if not fails else "%d CHECK(S) FAILED" % fails))
        say("transcript: %s" % OUT)
        browser.close()
        log.close()
        return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
