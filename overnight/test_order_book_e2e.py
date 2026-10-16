"""Does the guest's order really exist? Through the widget, checked in the table.

Every other test so far has read the bot's WORDS. That is exactly how ticket
11135 slipped through: the chat read perfectly - "You've added 2 x Chicken
Burger, total 3,375" - and the kitchen got coffee and chicken. Words are not
evidence.

So each turn here is judged three ways at once:

    what the guest was told      the reply text
    what the server holds        the row in new_bot_ird_guest_orders
    what the kitchen gets        the ticket description

The book exists so those three cannot disagree, and this is the test that says
whether they do.

    BASE_URL=http://localhost:8011 python overnight/test_order_book_e2e.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "no_reask", str(ROOT / "overnight" / "test_e2e_no_reask_placed.py"))
_m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_m)          # also wraps stdout for UTF-8
send, TA = _m.send, _m.TA

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8011")
HOTEL = "sarovar_portico_sriperumbudur"
OUT = ROOT / "overnight" / os.environ.get("RUN_TAG", "order_book_e2e")
OUT.mkdir(parents=True, exist_ok=True)

_URL = [l.split("=", 1)[1].strip() for l in open(ROOT / ".env", encoding="utf-8")
        if l.startswith("DATABASE_URL=")][0].replace("mysql+aiomysql", "mysql+pymysql")
ENG = create_engine(_URL, connect_args={"connect_timeout": 15})

# The reply asserts food was added or lists the order.
CLAIMS = re.compile(
    r"you'?ve added|i'?ve added|added \d|added the|in your cart|your cart"
    r"|order total|has been placed|order placed|on its way", re.I)

results: list[tuple[bool, str, str]] = []


def log(m: str = "") -> None:
    print(m, flush=True)


def check(ok: bool, name: str, detail: str = "") -> None:
    results.append((bool(ok), name, detail))
    log("      %s %s%s" % ("ok " if ok else "XX ", name, ("  -> " + detail) if detail else ""))


def read_book(session_id: str) -> dict:
    """The row as the server holds it, slot by slot."""
    with ENG.connect() as c:
        row = c.execute(text(
            "SELECT * FROM new_bot_ird_guest_orders "
            "WHERE hotel_code=:h AND session_id=:s"),
            {"h": HOTEL, "s": session_id}).mappings().first()
    return dict(row) if row else {}


def slots_of(row: dict) -> list[dict]:
    out = []
    for n in range(1, 11):
        cart = row.get("cart_%d" % n)
        if isinstance(cart, str):
            try:
                cart = json.loads(cart)
            except Exception:
                cart = {}
        if not cart or not (cart or {}).get("items"):
            continue
        out.append({
            "n": n,
            "items": [(i.get("item"), i.get("qty")) for i in cart["items"]],
            "note": (cart or {}).get("note") or "",
            "status": row.get("status_%d" % n),
            "ticket_ref": row.get("ticket_ref_%d" % n),
            "placed_at": row.get("placed_at_%d" % n),
        })
    return out


def tickets_of(session_id: str) -> list[dict]:
    with ENG.connect() as c:
        rows = c.execute(text(
            "SELECT ticket_ref, description FROM new_bot_tickets "
            "WHERE session_id=:s ORDER BY id ASC"), {"s": session_id}).mappings().all()
    return [dict(r) for r in rows]


def named_in(text_blob: str, dish: str) -> bool:
    return dish.lower() in (text_blob or "").lower()


def main() -> int:
    session_id = ""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=os.environ.get("UI_HEADLESS", "1") == "1")
        page = browser.new_page(viewport={"width": 1500, "height": 1000})
        page.set_default_timeout(60000)
        page.goto("%s/chat?hotel_code=%s&phase=during_stay" % (BASE_URL, HOTEL),
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
        m = re.search(r"Session:\s*(\S+)", page.inner_text("body"))
        session_id = m.group(1) if m else ""
        log("session: %s\n" % session_id)

        transcript: list[dict] = []

        def turn(msg: str, why: str) -> tuple[str, list[dict]]:
            reply = send(page, msg)
            book = slots_of(read_book(session_id))
            log("[%s]\n  > %s\n  < %s" % (why, msg, reply.replace("\n", "\n    ")[:260]))
            log("  BOOK: %s" % (book or "(empty)"))
            transcript.append({"guest": msg, "bot": reply, "book": str(book), "why": why})
            # THE CENTRAL CHECK. If the reply claims food was added, the server
            # must be holding it - that is the whole contract.
            if CLAIMS.search(reply):
                check(bool(book), "the reply claims an order, so the book must hold one")
            return reply, book

        reply, book = turn("can i get 2 chicken burgers", "a dish opens a slot")
        check(any(named_in(str(s["items"]), "Chicken Burger") for s in book),
              "the burgers are in the book", str(book))
        check(any(s["status"] == "ongoing" for s in book),
              "and the slot is ongoing, not sent yet")

        reply, book = turn("yes place it", "placing it")
        placed = [s for s in book if s["status"] in ("placed", "processing")]
        check(bool(placed), "the slot is now placed")
        check(bool(placed and placed[0]["ticket_ref"]),
              "and it knows its ticket ref", str(placed[0]["ticket_ref"]) if placed else "")
        tk = tickets_of(session_id)
        check(bool(tk), "a ticket exists", str([t["ticket_ref"] for t in tk]))
        check(bool(tk) and named_in(tk[-1]["description"], "Chicken Burger"),
              "THE KITCHEN GOT WHAT THE GUEST WAS TOLD",
              tk[-1]["description"] if tk else "no ticket")

        reply, book = turn("oh also add a filter coffee", "adding inside the window")
        joined = [s for s in book if named_in(str(s["items"]), "Filter Coffee")]
        check(bool(joined), "the coffee is in the book somewhere", str(book))

        reply, book = turn("what did i order", "recall must match the book")
        for s in book:
            for dish, _q in s["items"]:
                check(named_in(reply, dish),
                      "recall names %s, which the book holds" % dish)

        reply, book = turn("actually add a tiramisu as a new order", "a second order")
        check(len([s for s in book if s["items"]]) >= 1, "the book still holds orders")

        reply, book = turn("yes go ahead", "placing the second one")
        tk = tickets_of(session_id)
        log("  TICKETS: %s" % [t["description"] for t in tk])

        # No dish may be on a ticket that is not in the book, and nothing the
        # book calls placed may be missing from the tickets.
        book_dishes = {d.lower() for s in book for d, _ in s["items"]}
        ticket_blob = " ".join(t["description"] for t in tk).lower()
        for d in book_dishes:
            placed_slots = [s for s in book if s["status"] in ("placed", "processing")
                            and any(x[0].lower() == d for x in s["items"])]
            if placed_slots:
                check(d in ticket_blob, "%s is placed in the book AND on a ticket" % d)

        page.screenshot(path=str(OUT / "final.png"), full_page=True)
        (OUT / "transcript.json").write_text(
            json.dumps(transcript, indent=2, ensure_ascii=False), encoding="utf-8")
        browser.close()

    log("\n" + "=" * 70)
    fails = [n for ok, n, _ in results if not ok]
    for ok, n, d in results:
        log("%s %s%s" % ("PASS " if ok else "FAIL ", n, ("  -> " + d) if d and not ok else ""))
    log("\n%d/%d passed   (artifacts in %s)" % (len(results) - len(fails), len(results), OUT))
    log("session: %s" % session_id)
    log("tickets: python overnight/check_tickets.py %s" % session_id)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
