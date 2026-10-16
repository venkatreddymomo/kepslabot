"""The 50-turn Sarovar in-room-dining run, driven through the REAL widget.

Why the widget and not the API: an earlier API harness produced LOCAL-125 ticket
refs, which means the external ticketing call never landed and the run proved
nothing. The widget path is the one a guest actually takes and it produces real
external refs.

Rules this runner obeys, each of them learned the hard way:
  * Wait for "Mapped test profile active" BEFORE clicking New Session. It can take
    ~18s. Click early and the guest profile is missing - the greeting says "Hi!"
    instead of "Hi Venkat!" and there is no room number.
  * NEVER reload the page mid-run. A reload rotates conversation_id and the cart
    lives per-conversation: the chat history survives, the order does not, and any
    result after that point proves nothing. A wedged composer is reported, not
    "recovered".
  * Read a reply by waiting for a NEW assistant bubble whose text has STOPPED
    changing, expanding "Read more" first, since that toggle re-renders bubbles.
  * Flush every turn to disk the moment it lands.

Item choice: the multi-order turns deliberately use all-day dishes. Idli, dosa and
the rest of the spec's original items live inside the BREAKFAST section, which is
served 7:00-10:30 AM, so in an evening run the serving-time rule now refuses them -
correctly - and the add-vs-new-order behaviour underneath would never be exercised.
Breakfast items are instead used ON PURPOSE in the timing turns, where a refusal is
the pass condition.
"""
import datetime
import io
import json
import os
import re
import statistics
import sys
import time

# The replies carry emoji and rupee signs; a cp1252 console kills the run mid-turn.
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from playwright.sync_api import sync_playwright
from sqlalchemy import create_engine, text

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "overnight")
TAG = os.environ.get("RUN_TAG", "run50")
BASE = "http://127.0.0.1:8011"
CHAT_URL = BASE + "/chat?hotel_code=sarovar_portico_sriperumbudur&phase=during_stay"
URL = [l.split("=", 1)[1].strip()
       for l in open(os.path.join(ROOT, ".env"), encoding="utf-8")
       if l.startswith("DATABASE_URL=")][0].replace("mysql+aiomysql", "mysql+pymysql")

TA = 'textarea[placeholder="Type a message..."]'
SEND = "button[aria-label='Send message']"
BUBBLE = "div.flex.justify-start"

# (message, form action, what this turn is proving)
# form action: "submit" fills and sends the form, "cancel" clicks Cancel request,
# None leaves it alone (a form that blocks the next turn is still filled, and logged).
TURNS = [
    # --- ordering basics ---
    ("hi", None, "greeting names Venkat, room 1400"),
    ("Order food", None, "menu shows, veg/non-veg split, PDF attached, correct IST"),
    ("whats good for dinner", None, "browsing only, nothing added"),
    ("no thanks", None, "decline path, no order"),
    ("order food - 2 chicken burger and one filter coffee", None, "items + prices added"),
    ("yes place it", None, "TICKET 1 created, external ref, placed on THIS turn (4.1)"),
    ("what did i order", None, "prices AND a total"),

    # --- serving times (4.2) ---
    ("can i get a continental breakfast", None,
     "OUT of window -> refuse + give 7:00 AM-10:30 AM"),
    ("is the chocolate walnut brownie available right now", None,
     "no stated timing -> available"),
    ("whats available right now", None, "only in-window items, no breakfast dishes"),

    # --- add vs new order: both must pass ---
    ("oh i forgot, also add a filter coffee to that", None,
     "SAME ticket, bot says added to the existing order"),
    ("i want to place a new order - 2 arrabbiata pasta", None,
     "NEW order, total for the pasta only"),
    ("yes place it", None, "TICKET 2, pasta only"),
    ("what did i order", None, "both orders separate, never a combined total"),
    ("i want to place a new order", None, "two-turn phrasing, part 1"),
    ("2 vegetable burger", None, "two-turn phrasing, part 2 -> must also split"),

    # --- off-menu and browsing ---
    ("can i get a red wine as well?", None, "declined, alternatives named"),
    ("yes", None, "LISTS alternatives, does NOT place an order"),
    ("ok what desserts do you have", None, "browsing"),
    ("add tiramisu", None, "only tiramisu enters the cart"),

    # --- cancel and edit ---
    ("actually cancel the tiramisu", None, "that item only, rest survives"),
    ("what did i order", None, "guest total matches the ticket"),
    ("make it 3 vegetable burgers instead of 2", None, "quantity change"),

    # --- another team mid-order (this used to destroy the food order) ---
    ("the AC in my room is not cooling properly", None, "Engineering, food order untouched"),
    ("yes", None, "Engineering ticket raised"),
    ("yes lets continue", None, "back to food"),
    ("what did i order", None, "food order INTACT, no invented items, prices present"),
    ("cancel my second order", None, "that order only, the other stays open"),
    ("actually cancel my whole order", None, "ticket status cancelled"),

    # --- table reservation ---
    ("book a table at kadak for 4 at 8pm", None,
     "refused, only SPR Kitchen + Chola Lounge offered"),
    ("spr kitchen, 4 people at 8pm tonight", "submit",
     "form opens, outlet dropdown shows 2 clean names, ticket created"),
    ("change the venue to chola lounge", None, "SAME ticket updated, no form, no duplicate"),
    ("change it back to spr", None, "same again"),
    ("change kadak hotel timings to 9pm", None, "refused, booking untouched"),
    ("actually make it 9pm", None, "ticket updated"),
    ("can you arrange a cab to the airport tomorrow at 6am", "cancel",
     "form opens then Cancel request -> bot confirms, NO ticket"),

    # --- housekeeping / spa ---
    ("i need extra towels", None, "housekeeping intent"),
    ("yes", None, "Housekeeping ticket"),
    ("book me a spa session tomorrow at 5pm", "submit",
     "form; ticket carries treatment + date + time"),
    ("actually cancel the spa booking", None, "spa cancelled, towels ticket UNTOUCHED"),

    # --- identity, scope, language ---
    ("whats my name and room number", None, "Venkat / 1400, nothing invented"),
    ("whats my phone number", None, "own contact, not the hotel's"),
    ("book me a room at your delhi hotel", None, "phase decline"),
    ("can you book me a flight", None, "out of scope"),
    ("whats the wifi password", None, "KB answer"),
    ("do you allow pets", None, "KB answer"),
    ("mujhe apna order dubara batao", None, "Hindi, correct order"),
    ("ok english please", None, "language switch back"),
    ("did my ac get fixed", None, "engineering recall"),
    ("whats the checkout time", None, "KB answer"),
    ("thanks thats all", None, "closing, no spurious ticket"),
]


# A smoke run drives the same real-widget path over the first N turns, so the
# harness and the two new fixes are proven before spending an hour on the full set.
_limit = int(os.environ.get("MAX_TURNS", "0") or 0)
if _limit > 0:
    TURNS = TURNS[:_limit]

# A focused scenario runs through this same real-widget path. The file holds a JSON
# list of [message, form_action, intent] - form_action is "submit", "cancel" or null.
_turns_file = os.environ.get("UI_TURNS_JSON", "").strip()
if _turns_file:
    TURNS = [tuple(t) if isinstance(t, list) else (t, None, "")
             for t in json.load(open(_turns_file, encoding="utf-8"))]


def log(msg):
    line = "[{:%H:%M:%S}] {}".format(datetime.datetime.now(), msg)
    print(line, flush=True)
    with open(os.path.join(OUT, TAG + "_progress.log"), "a", encoding="utf-8") as f:
        f.write(line + "\n")


ENG = create_engine(URL, connect_args={"connect_timeout": 8}, pool_pre_ping=True)


def tickets_for(sid):
    if not sid:
        return []
    try:
        with ENG.connect() as c:
            return [dict(r._mapping) for r in c.execute(text(
                "SELECT id,ticket_ref,status,department,category,room_number,issue "
                "FROM new_bot_tickets WHERE session_id=:s ORDER BY id"), {"s": sid})]
    except Exception as ex:
        log("  ticket query failed: {!r}".format(ex))
        return []


def msg_count(page):
    m = re.search(r"Messages:\s*(\d+)", page.inner_text("body"))
    return int(m.group(1)) if m else 0


def expand_all(page):
    """"Read more" collapses long replies; expand before reading or the captured
    text is a truncated prefix."""
    try:
        page.evaluate(
            "() => {[...document.querySelectorAll('button')]"
            ".filter(b => /read more/i.test(b.innerText||'')).forEach(b => b.click());}"
        )
    except Exception:
        pass


def bot_bubbles(page):
    try:
        raw = page.evaluate(
            "() => [...document.querySelectorAll('%s')]"
            ".map(d => (d.innerText||'').trim()).filter(Boolean)" % BUBBLE
        )
    except Exception:
        return []
    out = []
    for t in raw:
        t = re.sub(r"\s*(Read more|Show less)\s*$", "", t, flags=re.I).strip()
        if t:
            out.append(t)
    return out


def wait_for_reply(page, before_count, timeout_s=180):
    """A new assistant bubble whose text has stopped changing. The Messages counter
    moves first; the text then streams in, so settling on the text is what matters."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        page.wait_for_timeout(1000)
        if msg_count(page) >= before_count + 2:
            break
    last, stable = None, 0
    while time.time() < deadline:
        expand_all(page)
        bubbles = bot_bubbles(page)
        cur = bubbles[-1] if bubbles else ""
        if cur and cur == last:
            stable += 1
            if stable >= 3:            # ~3s unchanged
                return cur
        else:
            stable = 0
        last = cur
        page.wait_for_timeout(1000)
    return last or ""


def find_form(page):
    for f in page.locator("form").all():
        try:
            if f.locator("input, select, textarea").count():
                return f
        except Exception:
            continue
    return None


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
    if re.search(r"treatment|session|service", lab):
        return "Massage"
    if re.search(r"guest|people|party|headcount|pax", lab):
        return "4"
    if re.search(r"location|pickup|drop|airport", lab):
        return "Chennai International Airport"
    if re.search(r"trip", lab):
        return "Drop"
    if re.search(r"email", lab):
        return "venkat@example.com"
    if re.search(r"flight|train", lab):
        return "AI440"
    return "Please assist, thank you."


def submit_form(page, note):
    form = find_form(page)
    if form is None:
        return None
    try:
        btn = form.locator("button[type=submit]").first
        if btn.count() and not btn.is_enabled():
            return None                      # already in flight
    except Exception:
        pass
    seen = []
    for el in form.locator("input, select, textarea").all():
        try:
            tag = el.evaluate("e => e.tagName")
            kind = el.evaluate("e => e.type || ''")
            label = el.evaluate(
                "e => (e.labels && e.labels[0] && e.labels[0].innerText) || e.placeholder || ''")
            if tag == "SELECT":
                opts = el.evaluate("e => [...e.options].map(o => o.value).filter(Boolean)")
                labels = el.evaluate("e => [...e.options].map(o => o.label).filter(Boolean)")
                seen.append({"label": label, "type": "select", "options": opts, "labels": labels})
                if opts:
                    el.select_option(opts[0])
            else:
                seen.append({"label": label, "type": kind})
                el.fill(value_for(label, kind))
        except Exception as ex:
            seen.append({"label": "<error>", "type": str(ex)[:60]})
    try:
        form.locator("button", has_text=re.compile("submit", re.I)).first.click()
        log("  {}: form SUBMITTED fields={}".format(note, [s.get("label") for s in seen]))
    except Exception as ex:
        log("  {}: form submit FAILED {!r}".format(note, ex))
    with open(os.path.join(OUT, TAG + "_forms.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": note, "action": "submit", "fields": seen},
                           ensure_ascii=False) + "\n")
    return seen


def cancel_form(page, note):
    """The Cancel request button. Closing the form in React is only half of it -
    the server must clear pending_action too, so the reply is checked as well."""
    try:
        btn = page.locator("button", has_text=re.compile(r"cancel request", re.I)).first
        if not btn.count():
            log("  {}: NO Cancel request button found".format(note))
            return False
        fields = []
        form = find_form(page)
        if form is not None:
            for el in form.locator("input, select, textarea").all():
                try:
                    fields.append(el.evaluate(
                        "e => (e.labels && e.labels[0] && e.labels[0].innerText) "
                        "|| e.placeholder || e.name || ''"))
                except Exception:
                    pass
        btn.click()
        log("  {}: Cancel request CLICKED (form had {})".format(note, fields))
        with open(os.path.join(OUT, TAG + "_forms.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": note, "action": "cancel", "fields": fields},
                               ensure_ascii=False) + "\n")
        return True
    except Exception as ex:
        log("  {}: Cancel request FAILED {!r}".format(note, ex))
        return False


def run():
    rows = []
    sid = ""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=os.environ.get("UI_HEADLESS", "0") == "1",
                                     slow_mo=120)
        page = browser.new_page(viewport={"width": 1500, "height": 1000})
        page.set_default_timeout(45000)
        page.goto(CHAT_URL, wait_until="domcontentloaded")

        # The guest profile (name + room) is attached only once the mapped test
        # profile resolves. Clicking New Session before that yields a nameless,
        # roomless guest and every identity turn below becomes meaningless.
        got_profile = False
        for _ in range(40):
            page.wait_for_timeout(1000)
            if "Mapped test profile active" in page.inner_text("body"):
                got_profile = True
                break
        log("mapped test profile active" if got_profile
            else "WARNING: mapped profile never appeared - identity turns are invalid")

        page.click("button:has-text('New Session')")
        page.wait_for_timeout(3000)
        try:
            page.click("button[aria-label='Open Chat']")
        except Exception:
            page.evaluate(
                "() => {const b=[...document.querySelectorAll('button')]"
                ".find(x=>/open chat/i.test(x.getAttribute('aria-label')||'')); if(b) b.click();}")
        page.wait_for_selector(TA, timeout=45000)
        page.wait_for_timeout(3000)

        m = re.search(r"Session:\s*(\S+)", page.inner_text("body"))
        sid = m.group(1) if m else ""
        greeting = (bot_bubbles(page) or [""])[0]
        log("chat open session={}".format(sid))
        log("greeting: {}".format(greeting[:160].replace("\n", " ")))

        seen_tickets = {}
        for i, (msg, form_action, intent) in enumerate(TURNS, 1):
            # Clear a form that is holding the composer. No reloads: a reload would
            # rotate conversation_id and silently void every later turn.
            if not (page.locator(TA).count() and page.locator(TA).is_enabled()):
                for _ in range(10):
                    submit_form(page, "unblocking before turn {}".format(i))
                    page.wait_for_timeout(3000)
                    if page.locator(TA).count() and page.locator(TA).is_enabled():
                        break
            if not (page.locator(TA).count() and page.locator(TA).is_enabled()):
                log("  turn {:02d}: COMPOSER WEDGED - stopping rather than reloading".format(i))
                break

            before = msg_count(page)
            t0 = time.time()
            try:
                page.click(TA)
                page.fill(TA, "")
                page.type(TA, msg, delay=35)
                page.wait_for_timeout(300)
                page.click(SEND)
            except Exception as ex:
                log("  turn {:02d} send failed: {!r}".format(i, ex))

            reply = wait_for_reply(page, before)
            secs = round(time.time() - t0, 1)

            # A form may need handling before the reply is judged complete.
            form_note = None
            if form_action == "submit":
                page.wait_for_timeout(2500)
                if submit_form(page, "turn {}".format(i)) is not None:
                    form_note = "submitted"
                    b2 = msg_count(page)
                    reply = wait_for_reply(page, b2 - 2) or reply
            elif form_action == "cancel":
                page.wait_for_timeout(2500)
                if cancel_form(page, "turn {}".format(i)):
                    form_note = "cancelled"
                    page.wait_for_timeout(4000)
                    expand_all(page)
                    bl = bot_bubbles(page)
                    reply = bl[-1] if bl else reply

            now_tickets = tickets_for(sid)
            changed = []
            for t in now_tickets:
                key = t["id"]
                sig = (t.get("ticket_ref"), t.get("status"), str(t.get("issue"))[:200])
                if seen_tickets.get(key) != sig:
                    changed.append(t)
                    seen_tickets[key] = sig

            row = {"turn": i, "guest": msg, "intent": intent, "secs": secs,
                   "bot": reply, "form": form_note,
                   "tickets_total": len(now_tickets),
                   "tickets_changed": [
                       {"ref": t.get("ticket_ref"), "status": t.get("status"),
                        "dept": t.get("department"), "issue": str(t.get("issue"))[:200]}
                       for t in changed]}
            rows.append(row)
            with open(os.path.join(OUT, TAG + "_turns.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

            log("  {:02d}/{} ({:5.1f}s) > {}".format(i, len(TURNS), secs, msg[:46]))
            log("        bot: {}".format(reply[:200].replace("\n", " ")))
            for t in changed:
                log("        TICKET {} [{}] {} :: {}".format(
                    t.get("ticket_ref"), t.get("status"), t.get("department"),
                    str(t.get("issue"))[:90]))
            page.wait_for_timeout(700)

        try:
            page.screenshot(path=os.path.join(OUT, TAG + "_final.png"), full_page=True)
        except Exception:
            pass
        browser.close()
    return sid, rows


def report(sid, rows):
    tickets = tickets_for(sid)
    local = [t for t in tickets if str(t.get("ticket_ref") or "").upper().startswith("LOCAL-")]
    lat = [r["secs"] for r in rows] or [0]
    out = {"session": sid, "turns": rows, "tickets": tickets,
           "local_only": len(local),
           "latency": {"min": min(lat), "median": statistics.median(lat), "max": max(lat)}}
    with open(os.path.join(OUT, TAG + "_result.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, ensure_ascii=False, default=str)

    log("\n===== TICKETS ({}) =====".format(len(tickets)))
    for t in tickets:
        ref = str(t.get("ticket_ref") or "")
        mark = "INVALID local-only" if ref.upper().startswith("LOCAL-") else "external OK"
        log("  [{}] {} status={} dept={} room={}".format(
            mark, ref, t.get("status"), t.get("department"), t.get("room_number")))
        log("        {}".format(str(t.get("issue"))[:160]))
    log("\nturns={} tickets={} local-only={} latency med={}s max={}s".format(
        len(rows), len(tickets), len(local), out["latency"]["median"], out["latency"]["max"]))
    log("RESULT -> overnight/{}_result.json".format(TAG))
    return len(local) == 0


if __name__ == "__main__":
    log("=== 50-turn Sarovar run start ({}) ===".format(TAG))
    sid, rows = run()
    ok = report(sid, rows)
    log("=== DONE {} ===".format("(no local-only tickets)" if ok else "(LOCAL TICKETS PRESENT)"))
    sys.exit(0 if ok else 1)
