"""End-to-end, through the REAL widget: serving times, and the one sentence that
must never come out.

The rule under test is not "refuse closed dishes". It is:

    a dish we serve, asked for at the wrong hour, must be answered with its HOURS
    -- never with "we don't have that"

so every case below checks the wording as well as the outcome. It also checks the
opposite direction: all-day dishes must stay orderable at the same moment, because
the failure mode of a timing feature is refusing food the kitchen would cook.

Which dishes are closed depends on the clock, so the expectations are built from
the same table the bot reads and the run reports which side of the windows it ran
on.

    BASE_URL=http://localhost:8011 python overnight/test_e2e_serving_times.py
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
sys.path.insert(0, ".")

from services import ird_availability as A

ROOT = Path(__file__).resolve().parent.parent
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8011")
HOTEL = os.environ.get("HOTEL_CODE", "sarovar_portico_sriperumbudur")
PHASE = "during_stay"
HEADLESS = os.environ.get("UI_HEADLESS", "1") == "1"
OUT = ROOT / "overnight" / os.environ.get("RUN_TAG", "e2e_serving_times")
OUT.mkdir(parents=True, exist_ok=True)

TA = 'textarea[placeholder="Type a message..."]'
SEND = "button[aria-label='Send message']"

# The forbidden answer for a dish that IS on the menu.
DENIES_EXISTENCE = re.compile(
    r"don.?t have|do not have|not on (the|our) menu|we don.?t serve|do not serve"
    r"|isn.?t on the menu|no longer (serve|offer)"
    # Phrasings that deny a real dish without using any of the words above.
    # "Phulka is not currently available as a menu item at SPR Kitchen" slipped
    # straight through and was scored a PASS - Phulka is on the menu.
    r"|not (currently )?(available|listed) as a (menu item|dish)"
    r"|isn.?t (listed|available) as a (specific )?(dish|menu item)"
    r"|not a (dish|item) (on|in) (the|our) menu", re.I)
# Turning an AVAILABLE dish away. This is the failure the first run missed: the bot
# told a guest at 13:34 that Filter Coffee - an all-day drink - was "served only
# from 7:00 AM to 10:30 AM". It denied nothing and quoted hours, so a check for
# those two things alone passed it. Refusing food the kitchen would happily cook is
# the whole failure mode of a timing feature, so it gets its own assertion.
REFUSES = re.compile(
    r"(is|are|it'?s)\s+(currently\s+|now\s+)?not available"
    r"|not available (right now|at (the |this )?moment|until|now)"
    r"|only available (from|between)"
    r"|(is |are )?served only"
    r"|available only (from|between)"
    r"|can'?t (be )?(order|serve|make|prepare)"
    r"|cannot (be )?(order|serve|make|prepare)"
    r"|will be (available|back)\b[^.]{0,20}\b(tomorrow|today|at \d)"
    r"|comes? back\b[^.]{0,20}\b(tomorrow|today|at \d)"
    r"|(back|available) (later|again) (today|tomorrow)",
    re.I,
)


def log(m: str) -> None:
    print(m, flush=True)


def bot_bubbles(page):
    return [t.strip() for t in page.locator("div.flex.justify-start").all_inner_texts() if t.strip()]


def msg_count(page) -> int:
    m = re.search(r"Messages:\s*(\d+)", page.inner_text("body"))
    return int(m.group(1)) if m else -1


def send(page, text: str) -> str:
    before = msg_count(page)
    page.fill(TA, text)
    page.click(SEND)
    for _ in range(90):
        page.wait_for_timeout(1000)
        if msg_count(page) > before + 1:
            break
    page.wait_for_timeout(2500)
    b = bot_bubbles(page)
    return b[-1] if b else ""


def main() -> int:
    now = datetime.datetime.now()
    log(f"running at {now:%H:%M} — availability resolved from the live table")
    blk = A.build_availability_block(HOTEL, now)
    shut = {r["item"] for r in (blk.get("items") or [])} if blk.get("mode", "").startswith("these") else set()
    log(f"mode={blk.get('mode')}  closed now: {len(shut)}")

    def state(dish: str) -> bool:
        return A.item_status(HOTEL, dish, now)["available"]

    # (message, dish it is about, why)
    CASES = [
        ("i want to order 2 idli", "Idli", "breakfast dish"),
        ("do you have dosa?", "Dosa (Plain or Masala)", "asking, not ordering"),
        ("can i get tandoori murgh", "Tandoori Murgh", "tandoor kebab"),
        ("i'd like a naan", "Naan", "tandoor bread"),
        ("order me a chicken burger", "Chicken Burger", "ALL-DAY — must never be refused"),
        ("can i get a filter coffee", "Filter Coffee (South Indian Style)", "ALL-DAY — must never be refused"),
        ("i want kadak", None, "NOT a dish — the off-menu decline must still fire"),
    ]

    results, transcript = [], []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=HEADLESS)
        page = browser.new_page(viewport={"width": 1500, "height": 1000})
        page.set_default_timeout(60000)
        page.goto(f"{BASE_URL}/chat?hotel_code={HOTEL}&phase={PHASE}", wait_until="domcontentloaded")

        want = f"Mapped test profile active for {PHASE}"
        for _ in range(50):
            page.wait_for_timeout(1000)
            try:
                if want in page.inner_text("body"):
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

        for msg, dish, why in CASES:
            reply = send(page, msg)
            denies = bool(DENIES_EXISTENCE.search(reply))
            hours = bool(GIVES_HOURS.search(reply))
            refuses = bool(REFUSES.search(reply))

            if dish is None:                       # genuine off-menu
                ok = denies
                verdict = "off-menu decline fired" if ok else "off-menu decline MISSING"
            elif state(dish):                      # open right now
                ok = (not denies) and (not refuses)
                verdict = ("served without denial" if ok
                           else ("DENIED an available dish" if denies
                                 else "REFUSED an available dish on invented hours"))
            else:                                  # on the menu, closed now
                ok = (not denies) and hours
                verdict = ("explained with hours" if ok
                           else ("said we DON'T HAVE IT" if denies else "no hours given"))

            results.append((ok, f"{msg!r} ({why})", verdict))
            transcript.append({"guest": msg, "dish": dish, "bot": reply,
                               "open_now": None if dish is None else state(dish),
                               "denies_existence": denies, "gives_hours": hours,
                               "refuses": refuses, "pass": ok})
            log(f"\n{'PASS' if ok else 'FAIL'}  {msg!r}  [{why}]")
            log(f"      open_now={None if dish is None else state(dish)}  denies={denies}"
                f"  hours={hours}  refuses={refuses}")
            log("      " + reply.replace("\n", "\n      ")[:520])

        page.screenshot(path=str(OUT / "final.png"), full_page=True)
        (OUT / "transcript.json").write_text(
            json.dumps(transcript, indent=2, ensure_ascii=False), encoding="utf-8")
        browser.close()

    log("\n" + "=" * 70)
    for ok, label, verdict in results:
        log(f"{'PASS' if ok else 'FAIL'}  {label}  -> {verdict}")
    bad = [r for r in results if not r[0]]
    log(f"\n{len(results) - len(bad)}/{len(results)} passed   (artifacts in {OUT})")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
