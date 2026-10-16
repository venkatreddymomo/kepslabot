"""Overnight job that drives the REAL widget UI with Playwright.

Waits for the prod DB, applies the Table Reservation outlet fix, restarts the
backend, then behaves like an actual guest: opens the chat, types into the box,
clicks Send, and fills in any form that appears - exactly the path a real user
takes, not the HTTP API underneath it.

Standalone on purpose. It keeps running after the Claude session ends, and every
turn is flushed to disk the moment it completes, because the previous UI run's
18 turns were lost when they only lived in browser memory.
"""
import datetime
import json
import os
import re
import statistics
import subprocess
import sys
import time

import httpx
from playwright.sync_api import sync_playwright
from sqlalchemy import create_engine, text

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "overnight")
BASE = "http://127.0.0.1:8011"
CHAT_URL = BASE + "/chat?hotel_code=sarovar_portico_sriperumbudur&phase=during_stay"
URL = [
    l.split("=", 1)[1].strip()
    for l in open(os.path.join(ROOT, ".env"), encoding="utf-8")
    if l.startswith("DATABASE_URL=")
][0].replace("mysql+aiomysql", "mysql+pymysql")

TURNS = [
    "hi", "Order food", "whats good for dinner", "one tandoori murg and 2 garlic naan.",
    "can i get a mojito as well?", "yes", "actually add a lobster thermidor too",
    "ok what desserts do you have", "add tiramisu",
    "the AC in my room is not cooling properly", "yes", "yes lets continue", "yes place it",
    "how long will it take", "what did i order", "actually cancel the tiramisu",
    "book a table at kadak for 4 people at 8pm tonight",
    "ok spr kitchen then, 4 people at 8pm tonight",
    "actually make it 9pm", "actually for table at kadak... 4 people at 9pm",
    "i need extra towels", "yes", "and 2 extra pillows", "is there a bathtub in the suites?",
    "what time is breakfast", "is the spa open now", "book me a spa session tomorrow 5pm",
    "what treatments do you have", "actually cancel the spa booking",
    "can you arrange a cab to the airport tomorrow 6am", "yes", "whats the wifi password",
    "do you allow pets", "can i get a late checkout", "tell me a joke", "whats 25 times 4",
    "book me a room at your delhi hotel", "whats my name and room number?",
    "whats my phone number", "i want to talk to your team about my bill",
    "actually i dont want to fill that form", "the tv remote is not working", "yes",
    "order water to my room", "yes", "mujhe hindi mein baat karni hai",
    "kya aap mujhe menu bata sakte hain", "ok english please",
    "anything else i should know about the hotel", "thanks thats all",
]

# A focused scenario can be driven through this same real-widget path by
# pointing UI_TURNS_JSON at a JSON list of messages.
_turns_file = os.environ.get("UI_TURNS_JSON", "").strip()
if _turns_file:
    import json as _json
    TURNS = _json.load(open(_turns_file, encoding="utf-8"))

TA = 'textarea[placeholder="Type a message..."]'


def log(msg):
    line = "[{:%Y-%m-%d %H:%M:%S}] {}".format(datetime.datetime.now(), msg)
    print(line, flush=True)
    with open(os.path.join(OUT, "progress.log"), "a", encoding="utf-8") as f:
        f.write(line + "\n")


def db_up():
    try:
        with create_engine(URL, connect_args={"connect_timeout": 6}).connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def apply_outlet_fix():
    """`outlet` is declared type=select but has no options, so the widget draws a
    free-text box that accepts any outlet name - the Kadak hole via the form."""
    eng = create_engine(URL, connect_args={"connect_timeout": 8})
    with eng.begin() as c:
        row = c.execute(
            text("SELECT form_config, description FROM new_bot_services WHERE id=86")
        ).fetchone()
        raw, desc = row[0], str(row[1] or "")
        d = json.loads(raw) if isinstance(raw, str) else raw
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        bp = os.path.join(ROOT, "backups",
                          "SAROVAR_TABLE_RESERVATION_FORM_BACKUP_{}.json".format(stamp))
        with open(bp, "w", encoding="utf-8") as f:
            f.write(json.dumps(d, indent=1, ensure_ascii=False))
        log("outlet fix: backup -> {}".format(bp))
        outlets = ["SPR Kitchen", "Chola Lounge - The Bar"]
        for o in outlets:
            if o.split(" - ")[0].lower() not in desc.lower():
                log("  WARNING: '{}' not named in the service description".format(o))
        done = False
        for f in d.get("fields", []):
            if str(f.get("id", "")).lower() == "outlet":
                f["type"] = "select"
                f["options"] = [{"label": o, "value": o} for o in outlets]
                f["required"] = True
                done = True
        if done:
            c.execute(text("UPDATE new_bot_services SET form_config=:f WHERE id=86"),
                      {"f": json.dumps(d, ensure_ascii=False)})
            log("outlet fix: applied -> {}".format(outlets))
        else:
            log("outlet fix: no outlet field found, nothing changed")


def restart_backend():
    subprocess.run(
        ["powershell", "-Command",
         "$c=Get-NetTCPConnection -LocalPort 8011 -State Listen -ErrorAction SilentlyContinue;"
         "foreach($x in $c){Stop-Process -Id $x.OwningProcess -Force -ErrorAction SilentlyContinue}"],
        capture_output=True,
    )
    time.sleep(4)
    py = r"C:\Users\Hp\AppData\Local\Programs\Python\Python313\python.exe"
    cmd = (
        "Start-Process -FilePath '{py}' "
        "-ArgumentList '-m','uvicorn','main:app','--host','0.0.0.0','--port','8011' "
        "-WorkingDirectory '{root}' "
        "-RedirectStandardOutput '{out}\\srv_out.log' "
        "-RedirectStandardError '{out}\\srv_err.log' -WindowStyle Hidden"
    ).format(py=py, root=ROOT, out=OUT)
    subprocess.Popen(["powershell", "-Command", cmd])
    log("backend restarting ...")
    for _ in range(120):
        time.sleep(5)
        try:
            if httpx.get(BASE + "/docs", timeout=5).status_code < 500:
                log("backend is up")
                return True
        except Exception:
            pass
    log("backend never came up")
    return False


def msg_count(page):
    m = re.search(r"Messages:\s*(\d+)", page.inner_text("body"))
    return int(m.group(1)) if m else 0


def value_for(label, kind):
    lab = (label or "").lower()
    if kind == "date":
        return (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
    if kind == "time":
        return "17:00"
    if kind == "number":
        return "4"
    if re.search(r"outlet|restaurant|venue", lab):
        return "SPR Kitchen"
    if re.search(r"treatment|session", lab):
        return "Massage"
    if re.search(r"guest|people|party|headcount", lab):
        return "4"
    if re.search(r"location|pickup|drop", lab):
        return "Chennai International Airport"
    if re.search(r"trip", lab):
        return "Drop"
    if re.search(r"email", lab):
        return "venkat@example.com"
    if re.search(r"occasion", lab):
        return "Anniversary"
    if re.search(r"equipment", lab):
        return "Projector"
    if re.search(r"event", lab):
        return "Meeting"
    if re.search(r"flight|train", lab):
        return "AI440"
    return "Please assist, thank you."


def fill_form(page, note):
    """Fill and submit an in-chat form the way a guest would. Returns a summary
    of what was on it, so the report shows which fields the guest was asked for."""
    forms = page.locator("form").all()
    target = None
    for f in forms:
        try:
            if "submit" in (f.inner_text() or "").lower() and f.locator("input, select").count():
                target = f
                break
        except Exception:
            continue
    if target is None:
        return None
    # A form already in flight shows a disabled "Submitting..." button. Clicking
    # it again just blocks for the full timeout and risks a double submission,
    # so treat an in-flight or already-sent form as nothing to do.
    try:
        btn = target.locator("button[type=submit]").first
        if btn.count() and not btn.is_enabled():
            return None
    except Exception:
        pass
    seen = []
    for el in target.locator("input, select, textarea").all():
        try:
            tag = el.evaluate("e => e.tagName")
            kind = el.evaluate("e => e.type || ''")
            label = el.evaluate(
                "e => (e.labels && e.labels[0] && e.labels[0].innerText) || e.placeholder || ''"
            )
            if tag == "SELECT":
                opts = el.evaluate("e => [...e.options].map(o => o.value).filter(Boolean)")
                seen.append({"label": label, "type": "select", "options": opts})
                if opts:
                    el.select_option(opts[0])
            else:
                v = value_for(label, kind)
                seen.append({"label": label, "type": kind})
                el.fill(v)
        except Exception as ex:
            seen.append({"label": "<error>", "type": str(ex)[:60]})
    try:
        target.locator("button", has_text=re.compile("submit", re.I)).first.click()
        log("  {} form submitted, fields={}".format(note, [s.get("label") for s in seen]))
    except Exception as ex:
        log("  {} form submit FAILED: {!r}".format(note, ex))
    with open(os.path.join(OUT, "forms.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": note, "fields": seen}, ensure_ascii=False) + "\n")
    return seen


def run_ui():
    rows = []
    with sync_playwright() as pw:
        # Visible and human-paced so it can be watched: slow_mo spaces the
        # actions out, and each message is TYPED character by character into
        # the real composer rather than pasted in.
        _headless = os.environ.get("UI_HEADLESS", "0") == "1"
        browser = pw.chromium.launch(headless=_headless, slow_mo=250)
        page = browser.new_page(viewport={"width": 1500, "height": 1000})
        page.set_default_timeout(30000)
        page.goto(CHAT_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        try:
            page.click("button:has-text('New Session')")
            page.wait_for_timeout(2500)
        except Exception:
            log("New Session button not found (continuing)")
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
        log("chat open, session={}".format(sid))

        for i, msg in enumerate(TURNS, 1):
            # A form can hold the composer. Try to clear it; if it is wedged (a
            # submit stuck on "Submitting...", say), reload the page and reopen
            # the chat rather than burning the whole turn budget on a dead DOM.
            # The session lives server-side, so a reload resumes the conversation.
            cleared = False
            for attempt in range(12):
                if page.locator(TA).count() and page.locator(TA).is_enabled():
                    cleared = True
                    break
                fill_form(page, "before turn {}".format(i))
                page.wait_for_timeout(4000)
            if not cleared:
                log("  turn {}: composer wedged - reloading the page to recover".format(i))
                try:
                    page.goto(CHAT_URL, wait_until="domcontentloaded")
                    page.wait_for_timeout(4000)
                    page.evaluate(
                        "() => {const b=[...document.querySelectorAll('button')]"
                        ".find(x=>/open chat/i.test(x.getAttribute('aria-label')||'')); if(b) b.click();}"
                    )
                    page.wait_for_selector(TA, timeout=30000)
                    log("  turn {}: recovered after reload".format(i))
                except Exception as ex:
                    log("  turn {}: reload recovery failed: {!r}".format(i, ex))
            before = msg_count(page)
            t0 = time.time()
            try:
                page.click(TA)
                page.fill(TA, "")
                page.type(TA, msg, delay=45)      # keystroke by keystroke, like a guest
                page.wait_for_timeout(400)
                page.click("button[aria-label='Send message']")
            except Exception as ex:
                log("  turn {} send failed: {!r}".format(i, ex))
            for _ in range(150):
                page.wait_for_timeout(1000)
                if msg_count(page) >= before + 2:
                    break
            page.wait_for_timeout(1500)
            secs = round(time.time() - t0, 1)
            bubbles = page.evaluate(
                "() => [...document.querySelectorAll('p')].map(p=>p.innerText.trim())"
                ".filter(Boolean).slice(-5).join(' | ')"
            )
            row = {"turn": i, "guest": msg, "secs": secs, "tail": bubbles[:500]}
            rows.append(row)
            with open(os.path.join(OUT, "turns.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            log("  turn {:02d}/{} ({:5.1f}s) :: {}".format(i, len(TURNS), secs, msg[:44]))
            fill_form(page, "after turn {}".format(i))
            page.wait_for_timeout(800)

        page.screenshot(path=os.path.join(OUT, "final_screen.png"), full_page=True)
        browser.close()
    return sid, rows


def collect(sid, rows):
    eng = create_engine(URL, connect_args={"connect_timeout": 8})
    tickets, events, transcript = [], [], []
    with eng.connect() as c:
        for r in c.execute(text(
            "SELECT ticket_ref,status,category,department,room_number,guest_name,issue,"
            "manager_notes FROM new_bot_tickets WHERE session_id=:s ORDER BY id"), {"s": sid}):
            tickets.append(dict(r._mapping))
        events = [r[0] for r in c.execute(text(
            "SELECT event FROM new_bot_log_tickets WHERE session_id=:s ORDER BY ts"), {"s": sid})]
        for r in c.execute(text(
            "SELECT user_text,bot_text FROM new_bot_log_messages WHERE session_id=:s ORDER BY ts"),
            {"s": sid}):
            transcript.append({"guest": r[0], "bot": r[1]})
    lat = [r["secs"] for r in rows]
    out = {
        "session": sid, "turns": rows, "transcript": transcript,
        "tickets": tickets, "ticket_events": events,
        "external_tickets": events.count("ticket_create_api_completed"),
        "local_tickets": events.count("local_ticket_create_completed"),
        "latency": {"min": min(lat), "median": statistics.median(lat),
                    "max": max(lat), "over_30s": sum(1 for x in lat if x > 30)},
    }
    with open(os.path.join(OUT, "result.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, ensure_ascii=False, default=str)
    log("tickets={} external={} local={}".format(
        len(tickets), out["external_tickets"], out["local_tickets"]))
    for t in tickets:
        log("   ref={} status={} room={} :: {}".format(
            t.get("ticket_ref"), t.get("status"), t.get("room_number"),
            str(t.get("issue"))[:60]))
    log("latency min={} median={} max={} over30s={}".format(
        out["latency"]["min"], out["latency"]["median"],
        out["latency"]["max"], out["latency"]["over_30s"]))
    log("RESULT -> overnight/result.json")


if __name__ == "__main__":
    log("=== overnight UI job started, waiting for the DB ===")
    deadline = time.time() + 13 * 3600
    ok = False
    while time.time() < deadline:
        if db_up():
            log("DB IS UP")
            ok = True
            break
        time.sleep(600)
    if not ok:
        log("DB never came back inside the window, aborting")
        sys.exit(1)
    try:
        apply_outlet_fix()
        if restart_backend():
            sid, rows = run_ui()
            collect(sid, rows)
            log("=== DONE ===")
        else:
            log("=== ABORTED: backend never started ===")
    except Exception as ex:
        log("=== FAILED: {!r} ===".format(ex))
        raise
