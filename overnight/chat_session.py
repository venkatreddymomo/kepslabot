"""Hold one widget conversation open so it can be talked to, a turn at a time.

Scripted sweeps decide every message before the first reply arrives, so they only
ever find what was already imagined. This keeps a real browser session alive and
reads messages from a mailbox file, writing each reply back - so the next thing
said can depend on what the bot actually just said.

    python overnight/chat_session.py            # start it, leave it running
    echo "can i get 2 chicken burgers" > overnight/_chat/in.txt
    cat overnight/_chat/out.txt                 # the reply, plus the book state

`out.txt` carries the book row alongside the reply, because what the bot SAYS is
not evidence - the whole point is whether the server holds what the guest was
told.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sys
import time
from pathlib import Path

import io

from playwright.sync_api import sync_playwright
from sqlalchemy import create_engine, text

# The console here is cp1252 and the menu is full of emoji - printing a reply
# killed the session mid-conversation.
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

ROOT = Path(__file__).resolve().parent.parent
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8011")
HOTEL = os.environ.get("HOTEL_CODE", "sarovar_portico_sriperumbudur")
BOX = ROOT / "overnight" / "_chat"
BOX.mkdir(parents=True, exist_ok=True)
IN, OUT, LOG = BOX / "in.txt", BOX / "out.txt", BOX / "log.txt"

TA = 'textarea[placeholder="Type a message..."]'
SEND = "button[aria-label='Send message']"

_URL = [l.split("=", 1)[1].strip() for l in open(ROOT / ".env", encoding="utf-8")
        if l.startswith("DATABASE_URL=")][0].replace("mysql+aiomysql", "mysql+pymysql")
ENG = create_engine(_URL, connect_args={"connect_timeout": 15}, pool_pre_ping=True)


def book_state(session_id: str) -> str:
    """What the server actually holds, so a reply can be checked against it."""
    try:
        with ENG.connect() as c:
            row = c.execute(text(
                "SELECT * FROM new_bot_ird_guest_orders WHERE hotel_code=:h "
                "AND session_id=:s"), {"h": HOTEL, "s": session_id}).mappings().first()
            tks = c.execute(text(
                "SELECT ticket_ref, description FROM new_bot_tickets "
                "WHERE session_id=:s ORDER BY id"), {"s": session_id}).mappings().all()
    except Exception as exc:
        return "  (book unreadable: %r)" % exc
    out = []
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
            out.append("  slot %d [%s] %s%s%s" % (
                n, row.get("status_%d" % n), items,
                ("  note=%r" % cart.get("note")) if cart.get("note") else "",
                ("  ticket=%s" % row.get("ticket_ref_%d" % n))
                if row.get("ticket_ref_%d" % n) else ""))
    if not out:
        out.append("  (book empty)")
    for t in tks:
        out.append("  TICKET %s: %s" % (t["ticket_ref"], t["description"]))
    return "\n".join(out)


def bot_bubbles(page) -> list[str]:
    return [t.strip() for t in page.locator("div.flex.justify-start").all_inner_texts()
            if t.strip()]


def msg_count(page) -> int:
    m = re.search(r"Messages:\s*(\d+)", page.inner_text("body"))
    return int(m.group(1)) if m else -1


def handle_form(page, msg: str) -> str:
    """Fill and submit the open in-chat form, or cancel it.

    Submitting is a path no scripted sweep had ever exercised: the harness only
    ever typed, so every form in every run was abandoned. Whether a submitted
    form actually reaches the hotel with the right fields was therefore never
    tested at all.

    Fields are matched on their visible label, loosely, because the labels carry
    hints ("Destination (e.g. Chennai Airport)") that no caller wants to retype.
    """
    if msg == "__cancel__":
        try:
            page.click("button:has-text('Cancel request')", timeout=10000)
            page.wait_for_timeout(2500)
            return "FORM: cancelled\n"
        except Exception as exc:
            return "FORM: could not cancel: %r\n" % exc

    pairs = []
    for chunk in msg[len("__form__"):].split(";"):
        if "=" in chunk:
            k, v = chunk.split("=", 1)
            pairs.append((k.strip().lower(), v.strip()))

    filled, missed = [], []
    for key, value in pairs:
        done = False
        # A <select> first - its options are the only valid values.
        for sel in page.locator("select").all():
            try:
                label = sel.evaluate(
                    "e => (e.closest('label')||e.parentElement).innerText || ''")
            except Exception:
                label = ""
            if key in label.strip().lower():
                # The label is not always in the caller's words: a time slot
                # offered as "9:00 PM" will never match "21:00", and the whole
                # field then reads as missing when it is really only spelled
                # differently. Try exact, then a loose contains, then - when the
                # caller passed "any" - whatever the field actually offers.
                opts = []
                try:
                    opts = [o.strip() for o in sel.evaluate(
                        "e => Array.from(e.options).map(o => o.label || o.text)")
                        if o and o.strip()]
                except Exception:
                    pass
                real = [o for o in opts
                        if o.lower() not in ("select...", "select outlet first", "")]
                want = None
                for cand in real:
                    if cand.lower() == value.lower():
                        want = cand
                        break
                if want is None:
                    for cand in real:
                        if value.lower() in cand.lower() or cand.lower() in value.lower():
                            want = cand
                            break
                if want is None and value.strip().lower() == "any" and real:
                    want = real[0]
                try:
                    sel.select_option(label=want if want else value)
                    filled.append("%s=%s" % (key, want or value))
                    done = True
                    break
                except Exception:
                    if real:
                        missed.append("%s (offered: %s)" % (key, real[:6]))
                        done = True
                        break
        if done:
            page.wait_for_timeout(600)
            continue
        for inp in page.locator("input, textarea").all():
            try:
                label = inp.evaluate(
                    "e => (e.closest('label')||e.parentElement).innerText || ''")
            except Exception:
                label = ""
            if key in label.strip().lower():
                try:
                    inp.fill(value)
                    filled.append("%s=%s" % (key, value))
                    done = True
                    break
                except Exception:
                    pass
        if not done:
            missed.append(key)

    page.wait_for_timeout(500)
    before = msg_count(page)
    try:
        page.click("button:has-text('Submit')", timeout=10000)
    except Exception as exc:
        return "FORM: filled %s, but Submit failed: %r\n" % (filled, exc)
    for _ in range(90):
        page.wait_for_timeout(1000)
        if msg_count(page) > before:
            break
    page.wait_for_timeout(3000)
    bubbles = bot_bubbles(page)
    return "FORM SUBMITTED: filled=%s missed=%s\nBOT:\n%s\n" % (
        filled, missed, bubbles[-1] if bubbles else "(no reply)")


LOCK = BOX / "session.lock"


def _claim_the_mailbox() -> None:
    """Refuse to start if another session is already reading in.txt.

    Two of these running at once both consume the same mailbox, so messages meant
    for a fresh guest were answered by the previous one - and the transcript then
    shows another session's order, which reads exactly like a product bug. It is
    not; it is two browsers fighting over one file.
    """
    import os

    if LOCK.exists():
        try:
            pid = int(LOCK.read_text(encoding="utf-8").strip() or 0)
        except (ValueError, OSError):
            pid = 0
        alive = False
        if pid:
            try:
                os.kill(pid, 0)
                alive = True
            except OSError:
                alive = False
        if alive:
            raise SystemExit(
                "ANOTHER CHAT SESSION IS ALREADY RUNNING (pid %d). Stop it first, "
                "or two browsers will share this mailbox." % pid)
    LOCK.write_text(str(os.getpid()), encoding="utf-8")


def main() -> int:
    _claim_the_mailbox()
    for f in (IN, OUT):
        f.write_text("", encoding="utf-8")
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
        OUT.write_text("READY session=%s\n" % session_id, encoding="utf-8")
        print("READY session=%s" % session_id, flush=True)

        seen = ""
        while True:
            try:
                msg = IN.read_text(encoding="utf-8").strip()
            except OSError:
                msg = ""
            if not msg or msg == seen:
                time.sleep(1.0)
                continue
            seen = msg
            if msg == "__quit__":
                break
            before = msg_count(page)
            # An open in-chat form REPLACES the composer: there is no textarea to
            # type into, so the only two moves a guest has are to SUBMIT it or to
            # cancel it. Two special messages drive that:
            #   __form__ k=v; k=v      fill those fields and submit
            #   __cancel__             click "Cancel request"
            # Anything else, with a form open, cancels first and then types - a
            # form left open used to kill the whole session on a fill() timeout.
            if msg.startswith("__form__") or msg == "__cancel__":
                block = handle_form(page, msg)
                OUT.write_text(block, encoding="utf-8")
                with LOG.open("a", encoding="utf-8") as fh:
                    fh.write("[%s]\n%s\n" % (datetime.datetime.now().isoformat(
                        timespec="seconds"), block))
                print(block, flush=True)
                continue
            if page.locator(TA).count() == 0:
                try:
                    page.click("button:has-text('Cancel request')", timeout=8000)
                    page.wait_for_selector(TA, timeout=20000)
                    print("[harness] dismissed an open form to keep typing",
                          flush=True)
                except Exception as exc:
                    print("[harness] could not dismiss the form: %r" % exc,
                          flush=True)
            page.fill(TA, msg)
            page.click(SEND)
            for _ in range(90):
                page.wait_for_timeout(1000)
                if msg_count(page) > before + 1:
                    break
            page.wait_for_timeout(2500)
            bubbles = bot_bubbles(page)
            reply = bubbles[-1] if bubbles else "(no reply)"
            block = ("GUEST: %s\nBOT:\n%s\nBOOK:\n%s\n" %
                     (msg, reply, book_state(session_id)))
            OUT.write_text(block, encoding="utf-8")
            with LOG.open("a", encoding="utf-8") as fh:
                fh.write("[%s]\n%s\n" % (datetime.datetime.now().isoformat(
                    timespec="seconds"), block))
            print(block, flush=True)
        browser.close()
    LOCK.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
