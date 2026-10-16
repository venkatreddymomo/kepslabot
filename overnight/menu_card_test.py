"""Does the In-Room Dining menu card appear only when it should?

Drives the REAL widget the way a guest does - types into the composer, clicks
Send - and after every turn counts the menu cards actually rendered in the
transcript (`a[href*="sarovar_ird.pdf"]`). What the guest can see is the thing
under test, so the assertion is made against the DOM, not against metadata.

Each scenario runs in its own fresh session, because the "menu already sent"
flag is per session and would otherwise leak between cases.

Expectations are CUMULATIVE card counts after each turn.
"""
import datetime
import json
import os
import re
import subprocess
import sys
import time

import httpx
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "overnight")
BASE = "http://127.0.0.1:8011"
HOTEL = "sarovar_portico_sriperumbudur"
TA = 'textarea[placeholder="Type a message..."]'
CARD = 'a[href*="sarovar_ird.pdf"]'
LOG = os.path.join(OUT, "menu_card_test.log")
RESULT = os.path.join(OUT, "menu_card_test.json")

SCENARIOS = [
    {
        "name": "S1  REPORTED BUG: pre-check-in Wi-Fi complaint then a bare 'yes'",
        "phase": "pre_checkin",
        "why": "the screenshot. Nothing about food; the menu must never appear.",
        "turns": [("wifi is slow", 0), ("yes", 0)],
    },
    {
        "name": "S2  in-stay non-food complaint then a bare 'yes'",
        "phase": "during_stay",
        "why": "same vague-'yes' shape as the bug, but in the phase where the "
               "menu IS otherwise allowed - so it isolates the trigger from the phase gate.",
        "turns": [("the AC in my room is not cooling properly", 0), ("yes", 0)],
    },
    {
        "name": "S3  ordering intent -> menu once, and only once",
        "phase": "during_stay",
        "why": "the case the feature exists for, plus the anti-annoyance rule.",
        "turns": [
            ("i want to order some food", 1),
            ("what desserts do you have", 1),
            ("add tiramisu", 1),
        ],
    },
    {
        "name": "S4  explicit ask re-sends even after it was already sent",
        "phase": "during_stay",
        "why": "a direct request is never refused on the grounds that we sent it earlier.",
        "turns": [
            ("i want to order some food", 1),
            ("can you send me the menu pdf", 2),
        ],
    },
    {
        "name": "S5  browsing food, no ordering intent (DELIBERATELY DROPPED)",
        "phase": "during_stay",
        "why": "used to attach via the book-list trigger. Removed on purpose - "
               "recording it so the change is visible if anyone wants it back.",
        "turns": [("whats there to eat here?", 0)],
    },
    {
        "name": "S6  explicit ask before check-in",
        "phase": "pre_checkin",
        "why": "the phase gate must stop the UNPROMPTED push only - a guest who "
               "asks in words still gets it.",
        "turns": [("send me the menu", 1)],
    },
]



# ── Channels that do not stamp a phase ───────────────────────────────────────
# The web widget always sends metadata.phase, so the UI scenarios above can
# never exercise a turn where the phase is unknown. WhatsApp and any raw API
# caller can. These go straight at /api/chat/message with NO phase, to prove
# the gate degrades to the routing check instead of silently swallowing the menu.
API_SCENARIOS = [
    {
        "name": "S7  no phase stamped, ordering intent",
        "why": "unknown phase must NOT disable the feature - fail open.",
        "turns": [("i want to order some food", 1)],
    },
    {
        "name": "S8  no phase stamped, non-food complaint",
        "why": "with the phase gate inert, the routing check alone must hold.",
        "turns": [("wifi is slow", 0), ("yes", 0)],
    },
]


def docs_in(payload):
    d = ((payload or {}).get("metadata") or {}).get("documents") or []
    return [x for x in d if isinstance(x, dict) and "sarovar_ird" in str(x.get("url") or "")]


def run_api():
    results = []
    for sc in API_SCENARIOS:
        log("")
        log("=" * 78)
        log(sc["name"])
        log("  channel=whatsapp, NO metadata.phase  ({})".format(sc["why"]))
        sid = "menucard-api-{}".format(int(time.time() * 1000))
        log("  session={}".format(sid))
        turns, total = [], 0
        for msg, expected in sc["turns"]:
            t0 = time.time()
            r = httpx.post(
                BASE + "/api/chat/message",
                json={"session_id": sid, "message": msg, "hotel_code": HOTEL,
                      "channel": "whatsapp", "metadata": {}},
                timeout=180,
            )
            secs = round(time.time() - t0, 1)
            body = r.json() if r.status_code == 200 else {}
            total += len(docs_in(body))
            ok = total == expected
            turns.append({"guest": msg, "cards_after": total, "expected": expected,
                          "pass": ok, "secs": secs, "http": r.status_code,
                          "tail": str(body.get("message") or "")[:200]})
            log("  {} '{}'  -> cards={} expected={} (http={} {:.1f}s)".format(
                "PASS " if ok else "FAIL ", msg[:46], total, expected, r.status_code, secs))
            if not ok:
                log("        reply: {}".format(turns[-1]["tail"][:180]))
        passed = all(t["pass"] for t in turns)
        log("  {}".format("SCENARIO PASS" if passed else "SCENARIO FAIL"))
        results.append({"name": sc["name"], "phase": "<unstamped>", "session": sid,
                        "pass": passed, "turns": turns})
    return results


def log(msg):
    line = "[{:%H:%M:%S}] {}".format(datetime.datetime.now(), msg)
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def restart_backend():
    """Kill whatever OWNS the port (not just 'python'), then relaunch and wait."""
    subprocess.run(
        ["powershell", "-Command",
         "$c=Get-NetTCPConnection -LocalPort 8011 -State Listen -ErrorAction SilentlyContinue;"
         "foreach($x in $c){taskkill /F /T /PID $($x.OwningProcess) 2>$null}"],
        capture_output=True,
    )
    time.sleep(5)
    still = subprocess.run(
        ["powershell", "-Command",
         "(Get-NetTCPConnection -LocalPort 8011 -State Listen -ErrorAction SilentlyContinue"
         " | Measure-Object).Count"],
        capture_output=True, text=True,
    ).stdout.strip()
    log("port 8011 listeners after kill: {}".format(still or "?"))
    if still not in ("0", ""):
        log("PORT STILL HELD - a zombie would serve stale code. Aborting.")
        return False
    py = r"C:\Users\Hp\AppData\Local\Programs\Python\Python313\python.exe"
    cmd = (
        "Start-Process -FilePath '{py}' "
        "-ArgumentList '-m','uvicorn','main:app','--host','0.0.0.0','--port','8011' "
        "-WorkingDirectory '{root}' "
        "-RedirectStandardOutput '{out}\\menu_srv_out.log' "
        "-RedirectStandardError '{out}\\menu_srv_err.log' -WindowStyle Hidden"
    ).format(py=py, root=ROOT, out=OUT)
    subprocess.Popen(["powershell", "-Command", cmd])
    log("backend restarting ...")
    for _ in range(120):
        time.sleep(5)
        try:
            if httpx.get(BASE + "/docs", timeout=5).status_code < 500:
                break
        except Exception:
            pass
    else:
        log("backend never came up")
        return False
    # A 200 is not proof the NEW code loaded - the bind error is.
    err = os.path.join(OUT, "menu_srv_err.log")
    txt = open(err, encoding="utf-8", errors="ignore").read() if os.path.exists(err) else ""
    if "10048" in txt:
        log("Errno 10048 in the server log - the old instance is still serving. Aborting.")
        return False
    log("backend is up, no bind error")
    return True


def msg_count(page):
    m = re.search(r"Messages:\s*(\d+)", page.inner_text("body"))
    return int(m.group(1)) if m else 0


def open_fresh_chat(page, phase):
    page.goto("{}/chat?hotel_code={}&phase={}".format(BASE, HOTEL, phase),
              wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    try:
        page.click("button:has-text('New Session')")
        page.wait_for_timeout(2500)
    except Exception:
        log("  (New Session button not found)")
    try:
        page.click("button[aria-label='Open Chat']")
    except Exception:
        page.evaluate(
            "() => {const b=[...document.querySelectorAll('button')]"
            ".find(x=>/open chat/i.test(x.getAttribute('aria-label')||'')); if(b) b.click();}"
        )
    page.wait_for_selector(TA, timeout=30000)
    sid = ""
    m = re.search(r"Session:\s*(\S+)", page.inner_text("body"))
    if m:
        sid = m.group(1)
    return sid


def say(page, msg):
    before = msg_count(page)
    t0 = time.time()
    page.click(TA)
    page.fill(TA, "")
    page.type(TA, msg, delay=25)
    page.wait_for_timeout(300)
    page.click("button[aria-label='Send message']")
    # Wait on the harness's own counter: the echoed USER bubble satisfies any
    # "panel text changed" check instantly and would race us past the reply.
    for _ in range(150):
        page.wait_for_timeout(1000)
        if msg_count(page) >= before + 2:
            break
    page.wait_for_timeout(2000)
    return round(time.time() - t0, 1)


def reply_tail(page):
    return page.evaluate(
        "() => [...document.querySelectorAll('p')].map(p=>p.innerText.trim())"
        ".filter(Boolean).slice(-3).join(' | ')"
    )[:300]


def run():
    results = []
    headless = os.environ.get("UI_HEADLESS", "1") == "1"
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless, slow_mo=120)
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        page.set_default_timeout(30000)
        for sc in SCENARIOS:
            log("")
            log("=" * 78)
            log(sc["name"])
            log("  phase={}  ({})".format(sc["phase"], sc["why"]))
            sid = open_fresh_chat(page, sc["phase"])
            log("  session={}".format(sid))
            turns = []
            for msg, expected in sc["turns"]:
                secs = say(page, msg)
                cards = page.locator(CARD).count()
                ok = cards == expected
                turns.append({
                    "guest": msg, "cards_after": cards, "expected": expected,
                    "pass": ok, "secs": secs, "tail": reply_tail(page),
                })
                log("  {} '{}'  -> cards={} expected={} ({:.1f}s)".format(
                    "PASS " if ok else "FAIL ", msg[:46], cards, expected, secs))
                if not ok:
                    log("        reply tail: {}".format(turns[-1]["tail"][:200]))
                with open(os.path.join(OUT, "menu_card_turns.jsonl"), "a",
                          encoding="utf-8") as f:
                    f.write(json.dumps(
                        {"scenario": sc["name"], "session": sid, **turns[-1]},
                        ensure_ascii=False) + "\n")
            passed = all(t["pass"] for t in turns)
            log("  {}".format("SCENARIO PASS" if passed else "SCENARIO FAIL"))
            results.append({"name": sc["name"], "phase": sc["phase"],
                            "session": sid, "pass": passed, "turns": turns})
        page.screenshot(path=os.path.join(OUT, "menu_card_final.png"), full_page=True)
        browser.close()
    return results


if __name__ == "__main__":
    open(LOG, "w", encoding="utf-8").close()
    open(os.path.join(OUT, "menu_card_turns.jsonl"), "w", encoding="utf-8").close()
    log("=== menu card test ===")
    if not restart_backend():
        sys.exit(2)
    res = run() + run_api()
    with open(RESULT, "w", encoding="utf-8") as f:
        json.dump(res, f, indent=1, ensure_ascii=False)
    log("")
    log("=" * 78)
    for r in res:
        log("{}  {}".format("PASS" if r["pass"] else "FAIL", r["name"]))
    bad = [r for r in res if not r["pass"]]
    log("{}/{} scenarios passed".format(len(res) - len(bad), len(res)))
    log("RESULT -> overnight/menu_card_test.json")
    sys.exit(1 if bad else 0)
