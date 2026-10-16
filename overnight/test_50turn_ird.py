"""A 50-turn in-room dining conversation, driven the way a guest drives one.

Runs HEADED so it can be watched live. Every turn waits for the bot's reply, reads
it, and only then composes the next message - several turns branch on what the bot
actually said, because a script that fires blind stops being a test of a
conversation and becomes a test of a transcript.

Two defects are re-tested throughout rather than once:

  ISSUE 1  a placed order must never be offered for placing again
  ISSUE 2  a dish outside its serving window must be answered with its HOURS,
           never as something the hotel does not have - and an all-day dish must
           never be refused on timing at all

Expectations are resolved LIVE from the same table the bot reads, because a run of
this length crosses serving-window boundaries (the tandoor shuts at 15:00) and a
dish that was orderable at turn 15 is legitimately not at turn 40.

    BASE_URL=http://localhost:8011 python overnight/test_50turn_ird.py
    UI_HEADLESS=1 ... to run it without a window
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
HEADLESS = os.environ.get("UI_HEADLESS", "0") == "1"
SLOW_MO = int(os.environ.get("SLOW_MO", "180"))
RUN = os.environ.get("RUN_TAG", datetime.datetime.now().strftime("run50_%Y%m%d_%H%M"))
OUT = ROOT / "overnight" / RUN
OUT.mkdir(parents=True, exist_ok=True)

TA = 'textarea[placeholder="Type a message..."]'
SEND = "button[aria-label='Send message']"

# ── the two sentences that decide the two issues ────────────────────────────
DENIES_EXISTENCE = re.compile(
    r"don'?t have|do not have|not on (the|our) menu|we don'?t serve|do not serve"
    r"|isn'?t on the menu|no longer (serve|offer)", re.I)
REFUSES_ON_TIME = re.compile(
    r"(is|are|it'?s)\s+(currently\s+|now\s+)?not available|not available (right now|at"
    r" (the|this )?moment|until|now)|only available (from|between)|(is |are )?served only"
    r"|available only (from|between)|can'?t (be )?(order|serve|make|prepare)"
    r"|cannot (be )?(order|serve|make|prepare)|will be (available|back)\b[^.]{0,20}"
    r"\b(tomorrow|today|at \d)|(back|available) (later|again) (today|tomorrow)", re.I)
_NEW = r"(?!\s*(a|another)\s+new\s+order)"
ASKS_TO_PLACE = re.compile(
    rf"shall i (go ahead and |now )?place{_NEW}|shall i confirm|should i place{_NEW}"
    rf"|would you like (me )?to place{_NEW}|would you like me to go ahead"
    rf"|do you want (me )?to place{_NEW}|may i place{_NEW}|can i place{_NEW}"
    r"|confirm to place|and place it now|place it now\?|confirm (this|the|your) order"
    r"|shall i send (it|this|the order|your order) to the kitchen", re.I)
GIVES_HOURS = re.compile(r"\d{1,2}(:\d{2})?\s*(am|pm)", re.I)
SAYS_ON_ITS_WAY = re.compile(
    r"on (its|it'?s) way|already placed|with the kitchen|being prepared|order placed"
    r"|已|being made", re.I)

results: list[dict] = []
transcript: list[dict] = []


def log(m: str = "") -> None:
    print(m, flush=True)


def now() -> datetime.datetime:
    return datetime.datetime.now()


def open_now(dish: str) -> bool:
    return A.item_status(HOTEL, dish, now())["available"]


# ── checks ──────────────────────────────────────────────────────────────────
def no_reask(r: str):
    """ISSUE 1 - nothing already with the kitchen may be offered for placing."""
    return ("no re-ask of a placed order", not ASKS_TO_PLACE.search(r))


def must_ask(r: str):
    return ("asks before placing something unplaced", bool(ASKS_TO_PLACE.search(r)))


def timing_answer(dish: str):
    """ISSUE 2 - resolved live: open -> must not refuse; shut -> hours, not denial."""
    def _c(r: str):
        if open_now(dish):
            return (f"{dish} is OPEN now -> not refused",
                    not REFUSES_ON_TIME.search(r) and not DENIES_EXISTENCE.search(r))
        return (f"{dish} is SHUT now -> hours given, existence not denied",
                bool(GIVES_HOURS.search(r)) and not DENIES_EXISTENCE.search(r))
    return _c


def never_denies(r: str):
    return ("does not claim we don't serve it", not DENIES_EXISTENCE.search(r))


def must_deny(r: str):
    return ("genuine off-menu item declined", bool(DENIES_EXISTENCE.search(r)))


def mentions(*words: str):
    def _c(r: str):
        low = r.lower()
        return (f"mentions {'/'.join(words)}", all(w.lower() in low for w in words))
    return _c


def absent(*words: str):
    def _c(r: str):
        low = r.lower()
        return (f"does NOT mention {'/'.join(words)}", not any(w.lower() in low for w in words))
    return _c


def says_placed(r: str):
    return ("confirms the order is with the kitchen", bool(SAYS_ON_ITS_WAY.search(r)))


def nonempty(r: str):
    return ("answered at all", len(r.strip()) > 15)


# ── adaptive follow-ups: read the reply, then decide what a guest would say ──
def answer_variant(default: str):
    """The bot often asks which variant (Plain/Butter/Garlic naan, veg/non-veg).
    A real guest answers that question rather than repeating themselves."""
    def _a(last: str) -> str:
        low = last.lower()
        if "garlic" in low and "naan" in low:
            return "butter naan please"
        if "plain or masala" in low:
            return "masala please"
        if re.search(r"vegetarian or non.?vegetarian|veg or non.?veg", low):
            return "vegetarian please"
        return default
    return _a


def yes_or_place(last: str) -> str:
    return "yes please place it" if ASKS_TO_PLACE.search(last) else "yes"


# (message | callable(last_reply)->message,  label,  [checks])
TURNS: list[tuple] = [
    ("hi", "greeting", [nonempty]),
    ("im hungry, what's good to eat?", "menu browse", [nonempty]),
    ("what's available right now?", "availability browse — must not claim kitchen shut",
     [absent("kitchen is closed", "kitchen is shut", "menu is unavailable")]),
    ("do you have idli?", "ISSUE 2 — breakfast dish, asking", [timing_answer("Idli"), never_denies]),
    ("what about dosa?", "ISSUE 2 — breakfast dish, asking",
     [timing_answer("Dosa (Plain or Masala)"), never_denies]),
    ("ok what can i get right now then", "available-now list", [nonempty]),

    ("order 2 chicken burger", "order an all-day dish",
     [must_ask, mentions("chicken burger"), never_denies]),
    ("yes place it", "PLACE order 1", [says_placed, no_reask]),
    ("what did i order", "ISSUE 1 #1 — recall right after placing",
     [no_reask, mentions("chicken burger")]),
    ("is my food coming?", "ISSUE 1 #2 — status turn", [no_reask]),

    ("add a filter coffee to that", "add an all-day drink to an open order",
     [never_denies, mentions("filter coffee")]),
    (yes_or_place, "PLACE the coffee", [no_reask]),
    ("what did i order", "ISSUE 1 #3 — recall with two items", [no_reask]),
    ("can i get tandoori murgh", "ISSUE 2 — tandoor kebab",
     [timing_answer("Tandoori Murgh"), never_denies]),
    ("i want naan", "ISSUE 2 — tandoor bread", [timing_answer("Naan"), never_denies]),
    (answer_variant("what about kerala paratha"), "adaptive: answer the variant question", [nonempty]),
    ("can i get kerala paratha", "ISSUE 2 — tawa bread, all-day, must NOT be refused",
     [timing_answer("Kerala Paratha"), never_denies]),
    ("can i order continental breakfast", "ISSUE 2 — breakfast combo",
     [timing_answer("Continental Breakfast"), never_denies]),
    ("when do you serve breakfast", "hours question", [nonempty]),
    ("ok give me a tiramisu", "all-day dessert must never be refused",
     [timing_answer("Tiramisu"), never_denies]),

    ("what did i order", "ISSUE 1 #4 — recall mid-flow", [no_reask]),
    ("do you have kadak", "genuine off-menu must still be declined", [must_deny]),
    ("i want a red wine", "off-menu / not served in IRD", [nonempty]),
    ("how much is the arrabbiata pasta", "price check", [nonempty]),
    ("does the arrabbiata have nuts?", "allergen question", [nonempty]),
    ("i'm allergic to dairy", "declare an allergy", [nonempty]),
    ("recommend something without dairy", "allergy-aware recommendation", [nonempty]),
    ("what did i order", "ISSUE 1 #5 — recall after a detour", [no_reask]),

    ("i want to place a new order - 2 arrabbiata pasta", "multi-order split",
     [must_ask, mentions("arrabbiata")]),
    (yes_or_place, "PLACE order 2", [says_placed]),
    ("what did i order", "ISSUE 1 #6 — recall with two orders", [no_reask]),
    ("how much was my second order", "per-order total, not combined", [nonempty]),
    ("is my first order still coming", "status of order 1", [no_reask]),
    ("what did i order", "ISSUE 1 #7", [no_reask]),

    ("the ac in my room is not working", "non-food ticket mid-conversation", [nonempty]),
    ("what did i order", "ISSUE 1 #8 — food survives a non-food detour",
     [no_reask, mentions("chicken burger")]),
    ("can i get idli now", "ISSUE 2 — breakfast dish, ordering not asking",
     [timing_answer("Idli"), never_denies]),
    ("what time does the tandoori kebab section open", "hours question", [nonempty]),
    ("can i get a medu vada", "ISSUE 2 — breakfast sub-section dish",
     [timing_answer("Medu Vada"), never_denies]),
    ("and a chicken burger too", "all-day dish alongside a refused one",
     [never_denies, timing_answer("Chicken Burger")]),

    ("what did i order", "ISSUE 1 #9", [no_reask]),
    ("cancel the tiramisu", "cancel one line", [nonempty]),
    ("what did i order", "ISSUE 1 #10 — recall after a cancel", [no_reask]),
    ("what's my room number", "identity", [nonempty]),
    ("do you have room service at 3am", "overnight availability question", [nonempty]),
    ("could i get a dosa at 3am", "ISSUE 2 — breakfast dish at a stated late hour",
     [never_denies]),
    ("what's on the midnight menu", "midnight menu browse", [nonempty]),
    ("ok one last thing - a filter coffee", "final add",
     [never_denies, timing_answer("Filter Coffee (South Indian Style)")]),
    (yes_or_place, "PLACE the final coffee", [no_reask]),
    ("what did i order", "ISSUE 1 #11 — final recall", [no_reask]),
]


def bot_bubbles(page):
    return [t.strip() for t in page.locator("div.flex.justify-start").all_inner_texts() if t.strip()]


def msg_count(page) -> int:
    m = re.search(r"Messages:\s*(\d+)", page.inner_text("body"))
    return int(m.group(1)) if m else -1


def send(page, text: str) -> str:
    before = msg_count(page)
    page.fill(TA, text)
    page.click(SEND)
    for _ in range(100):
        page.wait_for_timeout(1000)
        if msg_count(page) > before + 1:
            break
    page.wait_for_timeout(2200)
    b = bot_bubbles(page)
    return b[-1] if b else ""


def main() -> int:
    started = now()
    log(f"=== 50-turn IRD conversation — started {started:%H:%M:%S} ===")
    log(f"    headed={not HEADLESS}   base={BASE_URL}")
    blk = A.build_availability_block(HOTEL, started)
    log(f"    availability at start: {blk.get('mode')} — {len(blk.get('items') or [])} dish(es) shut\n")

    session_id = ""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=HEADLESS, slow_mo=SLOW_MO)
        page = browser.new_page(viewport={"width": 1400, "height": 950})
        page.set_default_timeout(60000)
        page.goto(f"{BASE_URL}/chat?hotel_code={HOTEL}&phase={PHASE}", wait_until="domcontentloaded")

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
        log(f"mapped guest profile: {'active' if ok_profile else 'MISSING'}")

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

        last = ""
        for i, (msg, label, checks) in enumerate(TURNS, 1):
            text = msg(last) if callable(msg) else msg
            reply = send(page, text)
            last = reply

            fired = [c(reply) for c in checks]
            failed = [n for n, ok in fired if not ok]
            ok = not failed
            results.append({"turn": i, "label": label, "sent": text,
                            "checks": [{"name": n, "pass": p} for n, p in fired],
                            "pass": ok})
            transcript.append({"turn": i, "at": now().isoformat(timespec="seconds"),
                               "label": label, "guest": text, "bot": reply,
                               "checks": [{"name": n, "pass": p} for n, p in fired]})
            log(f"[{i:02d}/50] {'PASS' if ok else 'FAIL'}  {label}")
            log(f"    > {text}")
            log("    < " + reply.replace("\n", "\n      ")[:380])
            for n, p in fired:
                log(f"      {'ok ' if p else 'XX '} {n}")
            log("")

            (OUT / "transcript.json").write_text(
                json.dumps(transcript, indent=2, ensure_ascii=False), encoding="utf-8")

        page.screenshot(path=str(OUT / "final.png"), full_page=True)
        browser.close()

    bad = [r for r in results if not r["pass"]]
    log("=" * 72)
    log(f"{len(results) - len(bad)}/{len(results)} turns passed")
    (OUT / "results.json").write_text(
        json.dumps({"session_id": session_id, "started": started.isoformat(),
                    "results": results}, indent=2, ensure_ascii=False), encoding="utf-8")
    if session_id:
        log(f"\nsession_id for ticket check: {session_id}")
        log(f"  python overnight/check_tickets.py {session_id}")
    log(f"artifacts: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
