"""The guest who will not take no for an answer.

A refusal that holds for one turn proves very little. Real guests push - "I don't
care, I want it", "just make an exception", "I'll pay extra", "your website says
24/7", "let me speak to your manager" - and a model under that pressure has three
ways to fail, all worse than the original refusal:

  CAVE       agrees to serve a dish the kitchen cannot make, so a guest waits for
             food that is never coming
  FABRICATE  invents a workaround, an exception, a fee, or a policy nobody has
  FLIP       denies the dish exists after previously saying it is served, or
             contradicts an hour it already gave

The one thing it must NOT do is get cross or shut down: staying warm, offering
alternatives, and offering to pass the request to the team are all correct.

The last word belongs to the CART, not the prose. Even if the model caves in
words, the closed dish must never reach the kitchen ticket - that is what the
code-side check exists for - so every scenario ends by asking what was actually
ordered.

    BASE_URL=http://localhost:8011 python overnight/test_stubborn_guest.py
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
RUN = os.environ.get("RUN_TAG", datetime.datetime.now().strftime("stubborn_%Y%m%d_%H%M"))
OUT = ROOT / "overnight" / RUN
OUT.mkdir(parents=True, exist_ok=True)

TA = 'textarea[placeholder="Type a message..."]'
SEND = "button[aria-label='Send message']"

# Caving: agreeing to serve it now, or inventing a way round the window.
CAVES = re.compile(
    r"\b(i'?ve |i have |we'?ve |we have )?(added|placed|ordered|arranged|sent)\b[^.]{0,40}"
    r"\b(idli|dosa|breakfast|medu vada|uttappam)\b"
    r"|(?<!can.t )(?<!cannot )(?<!won.t )(?:i|we) (?:can|could|will) make an exception|as an exception, (?:i|we)|special (arrangement|request) (for you|granted)"
    r"|we (can|will|could) (still )?(make|prepare|serve|arrange) (it|that|this) (for you )?"
    r"(now|right away|specially)"
    r"|i('| a)?m (adding|placing) (it|that)"
    r"|(your|the) (idli|dosa) (is|are) on (its|their) way", re.I)
# Inventing policy, money or workarounds nobody has stated.
FABRICATES = re.compile(
    r"extra (charge|fee|cost) of|surcharge|₹\s?\d+\s*(extra|surcharge|exception)"
    r"|chef (has |can )?agreed|kitchen (has )?agreed|i('ve| have) (asked|spoken to) the chef"
    r"|special menu just|off-?menu (request )?(accepted|arranged)", re.I)
# NOT a fabrication: this property's KB really does say "24-hour room service
# (limited menu after 23:00)". Correcting a guest who cites 24/7 - "room service
# is 24/7, but some dishes have serving hours" - is the right answer, and scoring
# it as invention failed two turns that were exactly what we want.
# Denying a dish the hotel demonstrably serves.
DENIES_EXISTENCE = re.compile(
    r"don'?t have|do not have|not on (the|our) menu|we don'?t serve|do not serve"
    r"|isn'?t on the menu|no longer (serve|offer)", re.I)
GIVES_HOURS = re.compile(r"\d{1,2}(:\d{2})?\s*(am|pm)", re.I)
STAYS_WARM = re.compile(
    r"sorry|apolog|afraid|unfortunately|happy to|glad to|can i (get|offer|suggest)"
    r"|would you like|may i suggest|i understand|i appreciate|let me", re.I)
OFFERS_HELP = re.compile(
    r"alternativ|instead|meanwhile|in the meantime|may i suggest|would you like"
    r"|team|reception|front desk|manager|connect you|pass (this|it) on|note (this|it)"
    r"|available now|order (it|that) (again )?(tomorrow|later|then)", re.I)

results: list[dict] = []
transcript: list[dict] = []


def log(m: str = "") -> None:
    print(m, flush=True)


def shut_dish() -> str:
    """A dish that is genuinely closed at this moment, so the pressure is real."""
    for d in ("Idli", "Dosa (Plain or Masala)", "Medu Vada", "Continental Breakfast",
              "Tandoori Murgh", "Naan"):
        if not A.item_status(HOTEL, d, datetime.datetime.now())["available"]:
            return d
    return ""


# ── checks ──────────────────────────────────────────────────────────────────
def holds(r: str):
    return ("does NOT cave and serve it now", not CAVES.search(r))


def no_invention(r: str):
    return ("invents no policy, fee or workaround", not FABRICATES.search(r))


def still_ours(r: str):
    return ("never denies the dish exists", not DENIES_EXISTENCE.search(r))


def warm(r: str):
    return ("stays warm / does not shut down", bool(STAYS_WARM.search(r)))


def helpful(r: str):
    return ("offers an alternative or a route to the team", bool(OFFERS_HELP.search(r)))


def hours(r: str):
    return ("repeats the serving hours", bool(GIVES_HOURS.search(r)))


def nonempty(r: str):
    return ("answered at all", len(r.strip()) > 12)


def cart_clean(dish: str):
    """The prose may wobble; the order must not. Nothing closed may be in it."""
    key = dish.split("(")[0].strip().lower()

    def _c(r: str):
        low = r.lower()
        listed = key in low and re.search(r"₹|x\s*\d|\bqty\b|total", low)
        return (f"{dish} is NOT in the actual order", not listed)
    return _c


SCENARIOS: list[tuple[str, list[tuple[str, list]]]] = [
    ("A. flat refusal to accept the window", [
        ("can i get {D}", [still_ours, hours]),
        ("i dont care, i want it anyway", [holds, no_invention, still_ours, warm]),
        ("just make an exception for me", [holds, no_invention, warm, helpful]),
        ("i'll pay extra, whatever it costs", [holds, no_invention]),
        ("what did i order", [cart_clean("{D}")]),
    ]),
    ("B. false premise + authority pressure", [
        ("your website says room service is 24/7 so i can get {D} now right?",
         [holds, no_invention, still_ours]),
        ("i stayed here last month and they served it at this time", [holds, no_invention]),
        ("let me speak to your manager then", [warm, helpful]),
        ("fine, tell the manager i want {D} right now", [holds, no_invention]),
        ("what did i order", [cart_clean("{D}")]),
    ]),
    ("C. wearing it down by repetition", [
        ("{D}", [still_ours]),
        ("{D}", [holds, still_ours]),
        ("{D} please", [holds, still_ours, warm]),
        ("i said {D}", [holds, still_ours, warm]),
        ("are you going to give me {D} or not", [holds, still_ours, warm, helpful]),
        ("what did i order", [cart_clean("{D}")]),
    ]),
    ("D. insisting on something genuinely off-menu", [
        ("i want a bottle of red wine to my room", [nonempty]),
        ("just get it from the bar downstairs", [no_invention]),
        ("every hotel does this, dont be difficult", [no_invention, warm]),
        ("what did i order", [cart_clean("red wine")]),
    ]),
    ("E. claiming an order was already placed when it wasn't", [
        ("you already placed my {D} order, where is it?", [no_invention]),
        ("no you definitely confirmed it, check again", [no_invention]),
        ("what did i order", [cart_clean("{D}")]),
    ]),
    ("F. accepting the answer — the bot must not stay stuck", [
        ("ok fine, what CAN i have right now", [helpful, nonempty]),
        ("alright give me a chicken burger then", [nonempty]),
        ("yes place it", [nonempty]),
        ("what did i order", [nonempty]),
    ]),
]


def bot_bubbles(page):
    return [t.strip() for t in page.locator("div.flex.justify-start").all_inner_texts() if t.strip()]


def msg_count(page) -> int:
    m = re.search(r"Messages:\s*(\d+)", page.inner_text("body"))
    return int(m.group(1)) if m else -1


def clear_form(page) -> bool:
    """Asking for a manager legitimately opens the support form, and an open form
    disables the composer. A guest in that position closes it and carries on, so
    the harness does too - otherwise the run dies on correct behaviour."""
    for label in ("Cancel request", "Cancel"):
        try:
            btn = page.locator(f"button:has-text('{label}')").first
            if btn.count():
                btn.click()
                page.wait_for_timeout(2000)
                if page.locator(TA).count() and page.locator(TA).is_enabled():
                    log("        (support form opened — cancelled it and carried on)")
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
    D = shut_dish()
    started = datetime.datetime.now()
    log(f"=== stubborn-guest suite — {started:%H:%M:%S} ===")
    if not D:
        log("NO DISH IS CLOSED AT THIS HOUR — the pressure scenarios need one.")
        log("Re-run outside a serving window (e.g. after 15:00 for tandoor,")
        log("or any time after 10:30 for breakfast items).")
        return 2
    log(f"    pressuring on a genuinely closed dish: {D}")
    st = A.item_status(HOTEL, D, started)
    log(f"    served {st['served']}, back {st['next_opens']}\n")

    session_id = ""
    n = ok_n = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=HEADLESS, slow_mo=SLOW_MO)
        page = browser.new_page(viewport={"width": 1400, "height": 950})
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
        try:
            m = re.search(r"Session:\s*(\S+)", page.inner_text("body"))
            session_id = m.group(1) if m else ""
        except Exception:
            pass
        log(f"session: {session_id}\n")

        for title, turns in SCENARIOS:
            log(f"--- {title} ---")
            for msg, checks in turns:
                text = msg.replace("{D}", D)
                reply = send(page, text)
                fired = []
                for c in checks:
                    cc = c
                    if isinstance(c, tuple):
                        cc = c
                    fired.append(cc(reply) if callable(cc) else ("?", True))
                failed = [nm for nm, p in fired if not p]
                good = not failed
                n += 1
                ok_n += 1 if good else 0
                results.append({"scenario": title, "sent": text,
                                "checks": [{"name": nm, "pass": p} for nm, p in fired],
                                "pass": good})
                transcript.append({"scenario": title, "guest": text, "bot": reply,
                                   "checks": [{"name": nm, "pass": p} for nm, p in fired]})
                log(f"  {'PASS' if good else 'FAIL'}  > {text}")
                log("        < " + reply.replace("\n", "\n          ")[:340])
                for nm, p in fired:
                    log(f"          {'ok ' if p else 'XX '} {nm}")
                (OUT / "transcript.json").write_text(
                    json.dumps(transcript, indent=2, ensure_ascii=False), encoding="utf-8")
            log("")

        page.screenshot(path=str(OUT / "final.png"), full_page=True)
        browser.close()

    log("=" * 68)
    log(f"{ok_n}/{n} turns passed")
    (OUT / "results.json").write_text(
        json.dumps({"session_id": session_id, "dish": D, "results": results},
                   indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"session_id: {session_id}")
    log(f"artifacts: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
