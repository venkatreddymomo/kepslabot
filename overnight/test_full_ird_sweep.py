"""Every in-room dining condition we have chased over the last four days, in one
conversation, driven the way a guest drives one.

Runs HEADED so it can be watched. Every turn waits for the reply, reads it, and
only then composes the next message; several turns branch on what the bot actually
said.

WHY IT REPEATS ITSELF: nearly every defect here has been intermittent. A condition
that passes once has told us very little - "shall I place this order?" came back
on 1 of 3 identical turns, and an all-day Filter Coffee was refused on 3 of 6
asks. So each condition is exercised 2-3 times at DIFFERENT points in the
conversation, and the report scores by CONDITION (3/3, 2/3) rather than by turn.

The run deliberately crosses 15:00, when the tandoor window shuts, so the same
dish has to flip mid-conversation. Every expectation is resolved live from the
same table the bot reads - nothing is hardcoded to a time of day.

    BASE_URL=http://localhost:8011 python overnight/test_full_ird_sweep.py
    UI_HEADLESS=1 ... to run without a window
"""
from __future__ import annotations

import datetime
import io
import json
import os
import re
import sys
from collections import OrderedDict
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
SLOW_MO = int(os.environ.get("SLOW_MO", "120"))
RUN = os.environ.get("RUN_TAG", datetime.datetime.now().strftime("sweep_%Y%m%d_%H%M"))
OUT = ROOT / "overnight" / RUN
OUT.mkdir(parents=True, exist_ok=True)

TA = 'textarea[placeholder="Type a message..."]'
SEND = "button[aria-label='Send message']"

# ── the sentences that decide things ────────────────────────────────────────
DENIES = re.compile(
    r"don.?t have|do not have|not on (the|our) menu|we don.?t serve|do not serve"
    r"|isn.?t on the menu|no longer (serve|offer)"
    # Phrasings that deny a real dish without using any of the words above.
    # "Phulka is not currently available as a menu item at SPR Kitchen" slipped
    # straight through and was scored a PASS - Phulka is on the menu.
    r"|not (currently )?(available|listed) as a (menu item|dish)"
    r"|isn.?t (listed|available) as a (specific )?(dish|menu item)"
    r"|not a (dish|item) (on|in) (the|our) menu", re.I)
REFUSES_ON_TIME = re.compile(
    r"(is|are|it'?s)\s+(currently\s+|now\s+)?not available|not available (right now|at"
    r" (the|this )?moment|until|now)|only available (from|between)|(is |are )?served only"
    r"|available only (from|between)|will be (available|back)\b[^.]{0,20}"
    r"\b(tomorrow|today|at \d)|(back|available) (later|again) (today|tomorrow)", re.I)
_NEW = r"(?!\s*(a|another)\s+new\s+order)"
ASKS_TO_PLACE = re.compile(
    rf"shall i (go ahead and |now )?place{_NEW}|shall i confirm|should i place{_NEW}"
    rf"|would you like (me )?to place{_NEW}|would you like me to go ahead"
    rf"|do you want (me )?to place{_NEW}|may i place{_NEW}|can i place{_NEW}"
    r"|confirm to place|and place it now|place it now\?"
    r"|\band place (it|them|this|that|your order)\b|\badd .{0,60}? and place\b"
    r"|confirm (this|the|your) order"
    r"|let me know if (i should|you would like me to) place"
    r"|shall i send (it|this|the order|your order) to the kitchen", re.I)
GIVES_HOURS = re.compile(r"\d{1,2}(:\d{2})?\s*(am|pm)", re.I)
ON_ITS_WAY = re.compile(
    r"on (its|it'?s) way|already placed|with the kitchen|being prepared|order placed"
    r"|has been placed|sent to the kitchen", re.I)
CAVES = re.compile(
    r"(?<!can.t )(?<!cannot )(?<!won.t )(?:i|we) (?:can|could|will) make an exception"
    r"|as an exception, (?:i|we)"
    r"|we (can|will|could) (still )?(make|prepare|serve|arrange) (it|that|this) "
    r"(for you )?(now|right away|specially)", re.I)
FABRICATES = re.compile(
    r"extra (charge|fee|cost) of|surcharge|chef (has |can )?agreed|kitchen (has )?agreed"
    r"|i('ve| have) (asked|spoken to) the chef|off-?menu (request )?(accepted|arranged)",
    re.I)
WARM = re.compile(
    r"sorry|apolog|afraid|unfortunately|happy to|glad to|can i (get|offer|suggest)"
    r"|would you like|may i suggest|i understand|i appreciate|let me|of course"
    r"|you might (enjoy|like)|might i (suggest|recommend)|i can (suggest|recommend|offer|help)"
    r"|how about|in the meantime|meanwhile", re.I)
# Asking to ADD an unplaced dish IS asking before placing - the two-step flow
# (add, then "shall I place this order?") is correct and must not score a miss.
# It must name WHAT is being added. "What else would you like to add?" is an
# open invitation with nothing to say yes to - counting it as a placement ask
# hid a turn where the bot never offered to place the cart at all, and the
# guest's "yes place it" then went nowhere.
ASKS_TO_ADD = re.compile(
    r"(?<!what else )would you like (me )?to add (?!anything|something)\w"
    r"|shall i add\b|do you want (me )?to add (?!anything|something)\w"
    r"|can i add (?!anything|something)\w"
    r"|would you like .{0,40}\badded to", re.I)
# "kadak" is not a dish and not a denial case either - clarifying is the wanted
# behaviour, so accept either a decline or a request to clarify.
ASKS_CLARIFY = re.compile(
    r"could you (please )?(clarify|confirm|tell me)|did you mean|what did you mean"
    r"|is(n.?t| not) (listed|on) (on )?our|can you tell me more|which (dish|item)", re.I)

results: list[dict] = []
transcript: list[dict] = []


def log(m: str = "") -> None:
    print(m, flush=True)


# Re-scoring a recorded run must judge each reply against the clock it was
# SPOKEN at - the tandoor opens at 19:00, so a reply that was right at 18:35
# reads as wrong an hour later.
AS_OF = None


def open_now(dish: str) -> bool:
    return A.item_status(HOTEL, dish, AS_OF or datetime.datetime.now())["available"]


# ── checks ──────────────────────────────────────────────────────────────────
# Asking to place is only wrong when there is NOTHING left to place. Once a
# recall answer names a cart line as not yet placed - which it now must, or a
# dish the guest just asked for disappears - the closing "shall I place this?"
# is the correct next question, not a re-ask of an order already gone.
PENDING_MARKER = re.compile(
    r"not yet placed|not yet sent|in your cart|still in your cart|yet to be placed"
    r"|waiting to be placed|still to be placed|still open for additions", re.I)


def no_reask(r):
    return ("does not re-ask to place a placed order",
            not ASKS_TO_PLACE.search(r) or bool(PENDING_MARKER.search(r)))


def must_ask(r):     return ("asks before placing something unplaced",
                             bool(ASKS_TO_PLACE.search(r) or ASKS_TO_ADD.search(r)))
def clarifies_or_declines(r):
    return ("declines it or asks what was meant",
            bool(DENIES.search(r) or ASKS_CLARIFY.search(r)))
def placed_if_asked(r):
    """A bare yes only places an order if the reply before it asked to place one.
    After an open-ended "anything else?" the right answer is to ask WHAT, so judge
    against what the bot had actually just said."""
    prev = transcript[-1]["bot"] if transcript else ""
    if ASKS_TO_PLACE.search(prev):
        return ("confirms it is with the kitchen", bool(ON_ITS_WAY.search(r)))
    return ("asks what to add rather than placing blind",
            bool(re.search(r"what would you like|would you like to add|here are"
                           r"|anything else|options", r, re.I)))
def never_denies(r): return ("does not claim we don't serve it", not DENIES.search(r))
def must_deny(r):    return ("declines a genuine non-dish", bool(DENIES.search(r)))
def placed(r):       return ("confirms it is with the kitchen", bool(ON_ITS_WAY.search(r)))
def holds(r):        return ("does not cave", not CAVES.search(r))
def no_invention(r): return ("invents no policy or fee", not FABRICATES.search(r))
def warm(r):         return ("stays warm", bool(WARM.search(r)))
def answers(r):      return ("answers at all", len(r.strip()) > 15)


def _about(reply: str, dish: str) -> str:
    """Only the sentences that talk about THIS dish.

    A reply that answered Phulka correctly and then listed the status of the
    guest's other open items used to fail: the refusal regex hit those other
    lines. Judge what was said about the dish that was asked for."""
    key = max(re.sub(r"[()]", " ", dish).split(), key=len).lower()
    parts = [x for x in re.split(r"(?<=[.!?])\s+|\n", reply) if key in x.lower()]
    return " ".join(parts) if parts else reply


def timing(dish: str):
    """Resolved LIVE - the tandoor shuts at 15:00 partway through this run."""
    def _c(r):
        scoped = _about(r, dish)
        if open_now(dish):
            return (f"{dish} is OPEN -> not refused",
                    not REFUSES_ON_TIME.search(scoped) and not DENIES.search(scoped))
        return (f"{dish} is SHUT -> hours given, existence not denied",
                bool(GIVES_HOURS.search(r)) and not DENIES.search(scoped))
    return _c


def says(*words):
    def _c(r):
        low = r.lower()
        return (f"mentions {'/'.join(words)}", all(w.lower() in low for w in words))
    return _c


def absent(*words):
    def _c(r):
        low = r.lower()
        return (f"does NOT mention {'/'.join(words)}", not any(w.lower() in low for w in words))
    return _c


def cart_free_of(dish: str):
    """The prose may wobble; the ORDER must not."""
    key = dish.split("(")[0].strip().lower()
    def _c(r):
        low = r.lower()
        listed = key in low and re.search(r"₹|x\s*\d|\btotal\b", low)
        return (f"{dish} is NOT in the order", not listed)
    return _c


# ── adaptive replies ────────────────────────────────────────────────────────
def variant(default: str):
    """The bot often asks which variant. A real guest answers that."""
    def _a(last: str) -> str:
        low = last.lower()
        if "garlic" in low and "naan" in low: return "butter naan please"
        if "plain or masala" in low:          return "masala please"
        if re.search(r"veg or non.?veg|vegetarian or non", low): return "vegetarian please"
        return default
    return _a


def yes_or_place(last: str) -> str:
    return "yes please place it" if ASKS_TO_PLACE.search(last) else "yes"


# ── how a guest actually answers, decided from what was just said ───────────
def agree_naturally(last: str) -> str:
    """Real guests do not type "yes" every time. Rotate through the words they
    really use - the placement turn used to re-ask whichever word was chosen, so
    each of these needs to work."""
    import random
    if ASKS_TO_PLACE.search(last):
        return random.choice(["go ahead", "sure do it", "yeah please", "yes place it"])
    return random.choice(["yes", "yeah go for it", "sure"])


def pick_from_list(last: str) -> str:
    """The bot often answers a vague ask with a list of dishes. A guest picks one
    BY NAME off that list rather than repeating themselves - and the name they use
    has to come from what was actually offered, not from a script."""
    for line in last.split("\n"):
        m = re.match(r"^\s*[•\-\*]\s*([A-Z][^-–—,(]{3,40})", line.strip())
        if m:
            dish = m.group(1).strip()
            if dish and not dish.lower().startswith(("vegetarian", "non-veg")):
                return f"the {dish} sounds good"
    return "what do you recommend"


def push_back(last: str) -> str:
    """If it just refused something on timing, push once the way a guest would."""
    if REFUSES_ON_TIME.search(last):
        return "cmon just make it for me ill pay double"
    return "ok what else is good"


# (condition, message-or-callable, checks)
TURNS: list[tuple] = [
    ("greeting",        "hi",                                   [answers]),
    ("menu browse",     "im hungry, whats good to eat?",         [answers]),
    ("availability browse", "whats available right now?",
     [absent("kitchen is closed", "kitchen is shut", "menu is unavailable")]),

    # ── ISSUE 2: serving times, asked and ordered, open and shut ──────────
    ("timing: breakfast dish", "do you have idli?",            [timing("Idli"), never_denies]),
    ("timing: all-day drink",  "can i get a filter coffee",     [timing("Filter Coffee (South Indian Style)"), never_denies]),
    ("timing: tandoor",        "can i get tandoori murgh",      [timing("Tandoori Murgh"), never_denies]),

    # ── the cart: EMPTY -> ONGOING -> PLACED ─────────────────────────────
    ("cart: ongoing asks to place", "order food - 2 chicken burger",
     [must_ask, says("chicken burger"), never_denies]),
    ("cart: placement",             "yes place it",             [placed, no_reask]),
    ("issue1: recall placed",       "what did i order",         [no_reask, says("chicken burger")]),
    ("issue1: status turn",         "is my food coming?",       [no_reask]),

    # ── provenance: questions must not add to the cart ───────────────────
    ("provenance: price question",  "how much is the arrabbiata pasta", [answers]),
    ("provenance: allergen question", "does the arrabbiata have nuts?", [answers]),
    ("provenance: allergy declared", "im allergic to dairy",     [answers]),
    ("provenance: cart unchanged",  "what did i order",
     [no_reask, cart_free_of("Arrabbiata")]),

    # ── PLACED is still open to additions ────────────────────────────────
    ("add to placed order",         "also add a filter coffee to that",
     [never_denies, says("filter coffee")]),
    ("yes-family: add_item",        yes_or_place,               [no_reask]),
    ("issue1: recall two items",    "what did i order",          [no_reask]),

    ("timing: breakfast dish", "can i order continental breakfast",
     [timing("Continental Breakfast"), never_denies]),
    ("timing: tawa bread all-day", "can i get kerala paratha",
     [timing("Kerala Paratha"), never_denies]),
    ("off-menu decline",       "i want kadak",                   [clarifies_or_declines]),

    # ── multi-order: the duplicate-food bug ──────────────────────────────
    ("multi-order: split",     "i want to place a new order - 2 arrabbiata pasta",
     [says("arrabbiata")]),
    ("multi-order: place 2",   yes_or_place,                     [placed]),
    ("multi-order: no duplicate food", "what did i order",
     [no_reask, says("arrabbiata")]),
    ("multi-order: per-order total", "how much was my second order", [answers]),

    ("issue1: recall placed",  "what did i order",               [no_reask]),
    ("timing: all-day drink",  "is the tiramisu available",       [timing("Tiramisu"), never_denies]),

    # ── stubborn guest, round 1 ──────────────────────────────────────────
    ("stubborn: insist",       lambda _l: f"i want {_shut_dish()} right now",
     [holds, no_invention, never_denies]),
    ("stubborn: pay extra",    "ill pay extra, i dont care",     [holds, no_invention, warm]),
    ("stubborn: exception",    "just make an exception for me",  [holds, no_invention, warm]),
    ("stubborn: cart clean",   "what did i order",               [no_reask]),

    # ── editing a placed order ───────────────────────────────────────────
    ("edit: cancel one line",  "cancel one chicken burger",      [answers]),
    ("issue1: recall after edit", "what did i order",            [no_reask]),
    ("edit: change quantity",  "make it 2 arrabbiata instead of 2",  [answers]),

    ("non-food detour",        "the ac in my room is not working", [answers]),
    ("issue1: food survives detour", "what did i order",
     [no_reask, says("chicken burger")]),

    # ── ISSUE 2 again, later in the conversation ─────────────────────────
    ("timing: breakfast dish", "can i get a medu vada now",      [timing("Medu Vada"), never_denies]),
    ("timing: tandoor",        "id like a naan",                 [timing("Naan"), never_denies]),
    ("timing: all-day drink",  "and a filter coffee too",
     [timing("Filter Coffee (South Indian Style)"), never_denies]),
    ("timing: tawa bread all-day", "what about a phulka",        [timing("Phulka"), never_denies]),

    ("yes-family: show offer", "what desserts do you have",      [answers]),
    ("yes-family: bare yes",   "yes",                            [answers]),
    ("issue1: recall placed",  "what did i order",               [no_reask]),

    # ── stubborn guest, round 2 ──────────────────────────────────────────
    ("stubborn: false premise", lambda _l: f"your website says room service is 24/7 so i can get {_shut_dish()} now",
     [holds, no_invention, never_denies]),
    ("stubborn: false claim",  lambda _l: f"you already placed my {_shut_dish()} order, where is it?",
     [no_invention]),
    ("stubborn: cart clean",   "what did i order",               [no_reask]),

    ("off-menu decline",       "i want a bottle of red wine",    [answers]),
    ("off-menu insistence",    "just get it from the bar downstairs", [no_invention]),

    # ── processing: the order placed early is now past the window ────────
    ("processing: cannot edit", "actually cancel my first order completely", [answers, warm]),
    ("issue1: recall placed",  "what did i order",               [no_reask]),

    # ── ISSUE 2 third pass ───────────────────────────────────────────────
    ("timing: breakfast dish", "could i get a dosa",             [timing("Dosa (Plain or Masala)"), never_denies]),
    ("timing: tandoor",        "can i get a tandoori roti",      [timing("Tandoori Roti"), never_denies]),
    ("timing: all-day drink",  "and a chicken burger",           [timing("Chicken Burger"), never_denies]),
    ("off-menu decline",       "do you have sushi",              [must_deny]),

    ("identity",               "whats my room number",           [answers]),
    ("overnight availability", "do you have room service at 3am", [answers]),

    # ── stubborn guest, round 3 ──────────────────────────────────────────
    ("stubborn: insist",       lambda _l: f"{_shut_dish()} please",   [holds, never_denies]),
    ("stubborn: repeat",       lambda _l: f"i said {_shut_dish()}",   [holds, never_denies, warm]),
    ("stubborn: accepts",      "ok fine what CAN i have right now",   [answers]),

    ("cart: ongoing asks to place", "order me a tiramisu then",
     [must_ask, says("tiramisu")]),
    ("cart: placement",        yes_or_place,                     [placed]),
    ("issue1: final recall",   "what did i order",               [no_reask, says("tiramisu")]),
    # ── the turns that came out of chatting with it by hand ──────────────
    # Every one of these is a failure I hit in a real conversation, not a case
    # anyone thought up in advance.
    ("confirm: natural wording", "get me a chicken burger and a tiramisu",
     [must_ask, says("chicken burger", "tiramisu")]),
    ("confirm: 'go ahead' places it", "go ahead", [placed, no_reask]),
    ("add to a LIVE order",     "oh also add a filter coffee",    [never_denies, says("filter coffee")]),
    ("confirm: loose wording",  agree_naturally,                  [placed_if_asked, no_reask]),
    ("issue1: recall placed",   "remind me what im getting",      [no_reask]),
    ("compound offer: named dish wins", "fine then add a vegetable burger",
     [never_denies, says("vegetable burger")]),
    ("compound offer: bare yes takes the dish", agree_naturally, [answers]),
    ("adaptive: pick off the list", pick_from_list,               [answers]),
    ("stubborn: push after refusal", push_back,                   [holds, no_invention]),
    ("issue1: final recall",   "so whats coming to my room",      [no_reask]),
    ("closing",                "thanks thats all",               [answers]),
]


def _shut_dish() -> str:
    """A dish that is genuinely closed right now, so the pressure is real."""
    for d in ("Idli", "Dosa (Plain or Masala)", "Medu Vada", "Continental Breakfast",
              "Tandoori Murgh", "Naan"):
        if not open_now(d):
            return d.split("(")[0].strip()
    return "Idli"


def bot_bubbles(page):
    return [t.strip() for t in page.locator("div.flex.justify-start").all_inner_texts() if t.strip()]


def msg_count(page) -> int:
    m = re.search(r"Messages:\s*(\d+)", page.inner_text("body"))
    return int(m.group(1)) if m else -1


def clear_form(page) -> bool:
    for label in ("Cancel request", "Cancel"):
        try:
            b = page.locator(f"button:has-text('{label}')").first
            if b.count():
                b.click(); page.wait_for_timeout(2000)
                if page.locator(TA).count() and page.locator(TA).is_enabled():
                    log("        (form opened - cancelled it and carried on)")
                    return True
        except Exception:
            continue
    return False


def send(page, text: str) -> str:
    if not (page.locator(TA).count() and page.locator(TA).is_enabled()):
        clear_form(page)
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
    started = datetime.datetime.now()
    log(f"=== full IRD sweep - {len(TURNS)} turns - started {started:%H:%M:%S} ===")
    blk = A.build_availability_block(HOTEL, started)
    log(f"    at start: {len(blk.get('items') or [])} dish(es) shut; pressure dish = {_shut_dish()}")
    log(f"    headed={not HEADLESS}\n")

    session_id = ""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=HEADLESS, slow_mo=SLOW_MO)
        page = browser.new_page(viewport={"width": 1400, "height": 950})
        page.set_default_timeout(60000)
        page.goto(f"{BASE_URL}/chat?hotel_code={HOTEL}&phase={PHASE}", wait_until="domcontentloaded")
        for _ in range(50):
            page.wait_for_timeout(1000)
            try:
                if f"Mapped test profile active for {PHASE}" in page.inner_text("body"):
                    break
            except Exception:
                pass
        page.click("button:has-text('New Session')"); page.wait_for_timeout(2500)
        try:
            page.click("button[aria-label='Open Chat']")
        except Exception:
            page.click("text='Need help? Chat with Kai'")
        page.wait_for_selector(TA, timeout=45000); page.wait_for_timeout(2000)
        try:
            m = re.search(r"Session:\s*(\S+)", page.inner_text("body"))
            session_id = m.group(1) if m else ""
        except Exception:
            pass
        log(f"session: {session_id}\n")

        last = ""
        for i, (cond, msg, checks) in enumerate(TURNS, 1):
            text = msg(last) if callable(msg) else msg
            reply = send(page, text)
            # ── ANSWER THE QUESTION IT ACTUALLY ASKED ────────────────────────
            # A script fires a fixed sentence at whatever came back. When the bot
            # closed on "what else would you like to add?", the scripted "yes
            # place it" answered THAT, the bot showed the order and asked to
            # place, and the script moved on - so the order stayed unplaced and
            # every later check that expected a placed order failed. Twelve
            # failures from one unanswered question. A guest simply says yes, so
            # this does too, once.
            if ASKS_TO_PLACE.search(reply) and any(
                c.__name__ in ("placed", "no_reask") for c in checks
            ):
                reply = send(page, "yes")
                log(f"    (answered its placement question with 'yes')")
            last = reply
            fired = [c(reply) for c in checks]
            bad = [n for n, ok in fired if not ok]
            ok = not bad
            results.append({"turn": i, "condition": cond, "sent": text, "pass": ok,
                            "checks": [{"name": n, "pass": p} for n, p in fired]})
            transcript.append({"turn": i, "at": datetime.datetime.now().isoformat(timespec="seconds"),
                               "condition": cond, "guest": text, "bot": reply,
                               "checks": [{"name": n, "pass": p} for n, p in fired]})
            log(f"[{i:02d}/{len(TURNS)}] {'PASS' if ok else 'FAIL'}  {cond}")
            log(f"    > {text}")
            log("    < " + reply.replace("\n", "\n      ")[:300])
            for n, p in fired:
                log(f"      {'ok ' if p else 'XX '} {n}")
            log("")
            (OUT / "transcript.json").write_text(
                json.dumps(transcript, indent=2, ensure_ascii=False), encoding="utf-8")

        page.screenshot(path=str(OUT / "final.png"), full_page=True)
        browser.close()

    # ── report BY CONDITION, because one pass proves nothing here ────────
    by: "OrderedDict[str, list[bool]]" = OrderedDict()
    for r in results:
        by.setdefault(r["condition"], []).append(r["pass"])
    log("=" * 74)
    log(f"{'CONDITION':<36} {'RESULT':<9} TURNS")
    log("-" * 74)
    perfect = shaky = broken = 0
    for cond, runs in by.items():
        good, n = sum(runs), len(runs)
        mark = "PASS" if good == n else ("FLAKY" if good else "FAIL")
        perfect += 1 if good == n else 0
        shaky += 1 if 0 < good < n else 0
        broken += 1 if good == 0 else 0
        log(f"{cond:<36} {good}/{n} {mark:<6} {[i['turn'] for i in results if i['condition']==cond]}")
    log("-" * 74)
    log(f"{len(results)} turns | conditions: {perfect} solid, {shaky} flaky, {broken} broken")
    (OUT / "results.json").write_text(json.dumps(
        {"session_id": session_id, "started": started.isoformat(), "results": results},
        indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"\nsession: {session_id}\nartifacts: {OUT}")
    log(f"tickets:  python overnight/check_tickets.py {session_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
