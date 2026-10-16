"""A dish the guest just asked for must not vanish from "what did I order".

Turn 61 of the 72-turn sweep: two orders were with the kitchen and a Tiramisu was
sitting in a fresh cart, one turn old. Asked "what did i order", the bot named the
two placed orders and said nothing about the Tiramisu.

The cause was in the service prompt, not the model: ORDER RECALL said to answer
"STRICTLY from food_profile.latest_order ... NEVER include an item that is not in
the profile", and food_profile is built from PLACED orders only, so a cart line
can never appear in it. The rule required the omission.

This drives the same sequence through the real widget and checks both directions:
the pending dish must be named AND marked as not yet sent, and once everything is
placed the bot must stop claiming anything is still waiting - the failure mode of
over-fixing is a bot that forever says an order needs placing.

    BASE_URL=http://localhost:8011 python overnight/test_e2e_recall_shows_cart.py
"""
from __future__ import annotations

import datetime
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
_spec.loader.exec_module(_m)
ASKS_TO_PLACE, send, TA = _m.ASKS_TO_PLACE, _m.send, _m.TA

# NOTE: the import above already wrapped stdout for UTF-8. Wrapping it again
# orphans that wrapper, and its finalizer closes the underlying buffer.


BASE_URL = os.environ.get("BASE_URL", "http://localhost:8011")
HOTEL = os.environ.get("HOTEL_CODE", "sarovar_portico_sriperumbudur")
PHASE = "during_stay"
HEADLESS = os.environ.get("UI_HEADLESS", "1") == "1"
OUT = ROOT / "overnight" / os.environ.get("RUN_TAG", "e2e_recall_cart")
OUT.mkdir(parents=True, exist_ok=True)

# However the bot phrases "this one has not gone to the kitchen yet".
PENDING = re.compile(
    "still waiting to be placed|waiting to be placed|not yet placed|yet to be placed"
    "|not been placed|still to be placed|not yet sent|yet to be sent|still open"
    "|awaiting placement|once you confirm|shall i place|would you like me to place",
    re.I)


def log(m: str) -> None:
    print(m, flush=True)


def main() -> int:
    results: list[tuple[bool, str, str]] = []
    transcript: list[dict] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=HEADLESS)
        page = browser.new_page(viewport={"width": 1500, "height": 1000})
        page.set_default_timeout(60000)
        page.goto(f"{BASE_URL}/chat?hotel_code={HOTEL}&phase={PHASE}",
                  wait_until="domcontentloaded")
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

        # (message, why, must contain, must NOT be missing, pending expectation)
        # pending: "yes"  the reply must flag something as not yet placed
        #          "no"   everything is with the kitchen, nothing may be flagged
        #          ""     not judged
        TURNS = [
            ("order food - 2 chicken burger", "opens a cart", ["Chicken Burger"], ""),
            ("yes place it", "sends it to the kitchen", ["Chicken Burger"], "no"),
            ("order me a tiramisu then", "second dish, order 1 is closed to edits",
             ["Tiramisu"], ""),
            ("yes", "Tiramisu lands in a NEW cart, still unplaced", ["Tiramisu"], ""),
            ("what did i order", "THE REPORTED BUG - the cart line must be named",
             ["Chicken Burger", "Tiramisu"], "yes"),
            ("yes please place it", "no-op if it already went; must not double-order", [], ""),
            ("what did i order", "everything placed - nothing may be called pending",
             ["Chicken Burger", "Tiramisu"], "no"),
        ]

        prev = ""
        for text, why, must_say, pending in TURNS:
            reply = send(page, text)
            # The dish sometimes goes straight to the kitchen instead of waiting
            # in a cart. Both are correct, so the expectation has to come from
            # what the bot just said - if placement was already confirmed,
            # "still waiting to be placed" would be a lie, not a pass.
            if pending == "yes" and re.search("placed|on its way|on their way",
                                              prev, re.I):
                pending = "no"
                why += " (placed outright - nothing should be pending)"
            missing = [w for w in must_say if w.lower() not in reply.lower()]
            flagged = bool(PENDING.search(reply))
            pend_ok = True if not pending else (flagged if pending == "yes" else not flagged)
            ok = not missing and pend_ok
            detail = ("names everything" if not missing else f"MISSING {missing}")
            if pending:
                detail += f"; pending_flagged={flagged} (want {pending})"
            results.append((ok, f"{text!r} - {why}", detail))
            transcript.append({"guest": text, "bot": reply, "why": why,
                               "missing": missing, "pending_expected": pending,
                               "pending_flagged": flagged, "pass": ok})
            log(f"\n{'PASS' if ok else 'FAIL'}  {text!r}  ({why})")
            log(f"      {detail}")
            log("      " + reply.replace("\n", "\n      ")[:600])
            prev = reply

        page.screenshot(path=str(OUT / "final.png"), full_page=True)
        (OUT / "transcript.json").write_text(
            json.dumps(transcript, indent=2, ensure_ascii=False), encoding="utf-8")
        browser.close()

    log("\n" + "=" * 70)
    fails = [r for r in results if not r[0]]
    for ok, label, got in results:
        log(f"{'PASS' if ok else 'FAIL'}  {label}  -> {got}")
    log(f"\n{len(results) - len(fails)}/{len(results)} passed   (artifacts in {OUT})")
    log(f"ts: {datetime.datetime.now().isoformat(timespec='seconds')}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
