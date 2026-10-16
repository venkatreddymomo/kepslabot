"""End-to-end, through the REAL widget: does the bot stop asking to place an order
it has already placed - and does it still ask for one it has not?

Reproduces the reported flow (order -> place -> cancel part of it -> "what did i
order"), then immediately checks the opposite direction, because the fix narrows
what the model is told is unplaced and the failure mode of over-narrowing is a
guest who can no longer order anything.

Idli is a BREAKFAST item (7:00-10:30 AM) so it is refused outside that window;
this runs on an all-day dish instead and tests the same code path.

    BASE_URL=http://localhost:8011 python overnight/test_e2e_no_reask_placed.py
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

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8011")
HOTEL = os.environ.get("HOTEL_CODE", "sarovar_portico_sriperumbudur")
PHASE = "during_stay"
HEADLESS = os.environ.get("UI_HEADLESS", "1") == "1"
OUT = ROOT / "overnight" / os.environ.get("RUN_TAG", "e2e_no_reask")
OUT.mkdir(parents=True, exist_ok=True)

TA = 'textarea[placeholder="Type a message..."]'
SEND = "button[aria-label='Send message']"

# A request for the guest to authorise PLACEMENT, however phrased. Deliberately
# an explicit list rather than a clever pattern: a loose one scored both ways on
# the first run - it read "shall I assist you further with your order?" as a
# placement ask (it is not; that turn passed) and missed "please confirm to place
# it" (which is one).
#
# "place A NEW order" is excluded throughout: once everything is with the kitchen
# the prompt tells the bot to close by offering something new, so "Would you like
# to place a new order for anything else?" is the CORRECT ending, not a re-ask.
# Reading it as one cost a false FAIL on the cancel turn.
_NEW = r"(?!\s*(a|another)\s+new\s+order)"
ASKS_TO_PLACE = re.compile(
    rf"shall i (go ahead and |now )?place{_NEW}"
    r"|shall i confirm"
    r"|should i place" + _NEW
    + rf"|would you like (me )?to place{_NEW}"
    r"|would you like me to go ahead"
    + rf"|do you want (me )?to place{_NEW}"
    + rf"|may i place{_NEW}"
    + rf"|can i place{_NEW}"
    r"|confirm to place"
    r"|and place it now"
    r"|place it now\?"
    # "Shall I add the Filter Coffee to your first order AND PLACE IT?" -
    # a placement ask with the verb at the end. Missed by patterns that
    # expect "place" to follow "shall I" directly.
    r"|\band place (it|them|this|that|your order)\b"
    r"|\badd .{0,60}? and place\b"
    r"|confirm (this|the|your) order"
    r"|let me know if (i should|you would like me to) place"
    r"|shall i send (it|this|the order|your order) to the kitchen",
    re.I,
)


def log(msg: str) -> None:
    print(msg, flush=True)


def bot_bubbles(page) -> list[str]:
    return [
        t.strip()
        for t in page.locator("div.flex.justify-start").all_inner_texts()
        if t.strip()
    ]


def msg_count(page) -> int:
    m = re.search(r"Messages:\s*(\d+)", page.inner_text("body"))
    return int(m.group(1)) if m else -1


def send(page, text: str) -> str:
    """Send one turn and return the bot's reply, waiting on the Messages counter
    rather than on bubble text (which changes while the reply streams)."""
    before = msg_count(page)
    page.fill(TA, text)
    page.click(SEND)
    for _ in range(90):
        page.wait_for_timeout(1000)
        if msg_count(page) > before + 1:
            break
    page.wait_for_timeout(2500)
    bubbles = bot_bubbles(page)
    return bubbles[-1] if bubbles else ""


def main() -> int:
    results: list[tuple[bool, str, str]] = []
    transcript: list[dict] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=HEADLESS)
        page = browser.new_page(viewport={"width": 1500, "height": 1000})
        page.set_default_timeout(60000)
        page.goto(f"{BASE_URL}/chat?hotel_code={HOTEL}&phase={PHASE}",
                  wait_until="domcontentloaded")

        # The mapped guest profile lands ~18s in; without it there is no room
        # number and the order cannot be placed at all.
        want = f"Mapped test profile active for {PHASE}"
        ok_profile = False
        for _ in range(50):
            page.wait_for_timeout(1000)
            try:
                if want in page.inner_text("body"):
                    ok_profile = True
                    break
            except Exception:
                pass
        log(f"mapped profile: {'active' if ok_profile else 'MISSING'}")

        page.click("button:has-text('New Session')")
        page.wait_for_timeout(2500)
        try:
            page.click("button[aria-label='Open Chat']")
        except Exception:
            page.click("text='Need help? Chat with Kai'")
        page.wait_for_selector(TA, timeout=45000)
        page.wait_for_timeout(2000)

        # (message, why, placement expectation, substring the reply MUST contain)
        # The last column is the guard against over-fixing: narrowing what the model
        # is told is unplaced must not cost the guest the ability to add a dish to an
        # order already open, which is what happened on the first run.
        TURNS = [
            ("order food - 2 chicken burger", "cart summary, SHOULD ask to place", "must_ask", "Chicken Burger"),
            ("yes place it", "placement confirmation", "no_ask", "Chicken Burger"),
            ("what did i order", "recall of a PLACED order", "no_ask", "Chicken Burger"),
            ("cancel one chicken burger", "ticket edit", "no_ask", ""),
            ("what did i order", "recall after the edit - THE REPORTED BUG", "no_ask", "Chicken Burger"),
            ("also add a filter coffee to that", "genuinely NEW item, SHOULD ask", "must_ask", "Filter Coffee"),
            ("yes", "placement of the coffee - IT MUST REACH THE ORDER", "no_ask", "Filter Coffee"),
            ("what did i order", "recall with everything placed", "no_ask", "Filter Coffee"),
        ]

        for text, why, expect, must_say in TURNS:
            reply = send(page, text)
            asks = bool(ASKS_TO_PLACE.search(reply))
            ask_ok = asks if expect == "must_ask" else not asks
            say_ok = (not must_say) or (must_say.lower() in reply.lower())
            ok = ask_ok and say_ok
            detail = ("asked to place" if asks else "did not ask")
            if not say_ok:
                detail += f"; MISSING {must_say!r}"
            results.append((ok, f"{text!r} - {why}", detail))
            transcript.append({"guest": text, "bot": reply, "expect": expect,
                               "asked_to_place": asks, "must_say": must_say,
                               "said_it": say_ok, "pass": ok})
            log(f"\n{'PASS' if ok else 'FAIL'}  {text!r}  ({why})")
            log(f"      expect={expect}  asked_to_place={asks}  mentions[{must_say}]={say_ok}")
            log("      " + reply.replace("\n", "\n      ")[:600])

        page.screenshot(path=str(OUT / "final.png"), full_page=True)
        (OUT / "transcript.json").write_text(
            json.dumps(transcript, indent=2, ensure_ascii=False), encoding="utf-8")
        browser.close()

    log("\n" + "=" * 70)
    fails = [r for r in results if not r[0]]
    for ok, label, got in results:
        log(f"{'PASS' if ok else 'FAIL'}  {label}  -> {got}")
    log(f"\n{len(results) - len(fails)}/{len(results)} passed"
        f"   (artifacts in {OUT})")
    log(f"ts: {datetime.datetime.now().isoformat(timespec='seconds')}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
