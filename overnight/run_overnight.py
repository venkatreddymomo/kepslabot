"""Self-contained overnight job.

Waits for the prod DB to come back, applies the Table Reservation outlet fix,
restarts the backend, runs the 50-turn during-stay conversation, and writes
everything to overnight/. Deliberately standalone: it keeps running even if the
Claude session ends, and every turn is flushed to disk as it completes so a
crash or another DB blip cannot erase the evidence (which is exactly how the
last 18-turn run was lost).
"""
import datetime
import json
import os
import statistics
import subprocess
import sys
import time
import uuid

import httpx
from sqlalchemy import create_engine, text

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "overnight")
BASE = "http://localhost:8011"
HOTEL = "sarovar_portico_sriperumbudur"
PROFILE = {
    "entity_id": "6311", "organisation_id": "6311", "guest_id": "993543",
    "guest_name": "Venkat", "guest_phone": "917036718200", "room_number": "1400",
}
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
    """outlet is declared type=select but carries no options, so the widget draws
    a free-text box and accepts any outlet name - the Kadak hole via the form."""
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
                log("  WARNING: '{}' is not named in the service description".format(o))

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


def run_conversation():
    sid = "overnight-" + uuid.uuid4().hex[:8]
    log("conversation start session={}".format(sid))
    rows = []
    with httpx.Client() as client:
        for i, msg in enumerate(TURNS, 1):
            t0 = time.time()
            try:
                r = client.post(
                    BASE + "/api/chat/message",
                    json={
                        "session_id": sid, "message": msg, "hotel_code": HOTEL,
                        "channel": "web_widget",
                        "metadata": {
                            "phase": "during_stay",
                            "chat_test_profile_phase": "during_stay",
                            "chat_test_profile": PROFILE,
                        },
                    },
                    timeout=240.0,
                )
                d = r.json() if r.status_code == 200 else {"_err": r.text[:200]}
            except Exception as ex:
                d = {"_err": str(ex)[:200]}
            secs = round(time.time() - t0, 1)
            meta = d.get("metadata") or {}
            ent = meta.get("entities") or {}
            row = {
                "turn": i, "guest": msg, "secs": secs,
                "svc": ent.get("target_service_id") or "",
                "action": ent.get("orchestration_action") or "",
                "render": meta.get("render_type") or "",
                "bot": str(d.get("display_message") or d.get("message") or d.get("_err") or "")[:700],
            }
            rows.append(row)
            with open(os.path.join(OUT, "turns.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            log("  turn {:02d}/{} ({:5.1f}s) svc={:<18} :: {}".format(
                i, len(TURNS), secs, row["svc"] or "-", msg[:40]))
    return sid, rows


def collect(sid, rows):
    eng = create_engine(URL, connect_args={"connect_timeout": 8})
    tickets, events = [], []
    with eng.connect() as c:
        for r in c.execute(text(
            "SELECT ticket_ref,status,category,department,room_number,guest_name,issue,"
            "manager_notes FROM new_bot_tickets WHERE session_id=:s ORDER BY id"), {"s": sid}):
            tickets.append(dict(r._mapping))
        events = [r[0] for r in c.execute(text(
            "SELECT event FROM new_bot_log_tickets WHERE session_id=:s ORDER BY ts"), {"s": sid})]
    lat = [r["secs"] for r in rows]
    out = {
        "session": sid, "turns": rows, "tickets": tickets, "ticket_events": events,
        "latency": {"min": min(lat), "median": statistics.median(lat),
                    "max": max(lat), "over_30s": sum(1 for x in lat if x > 30)},
        "external_tickets": events.count("ticket_create_api_completed"),
        "local_tickets": events.count("local_ticket_create_completed"),
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
    log("=== overnight job started, waiting for the DB ===")
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
            sid, rows = run_conversation()
            collect(sid, rows)
            log("=== DONE ===")
        else:
            log("=== ABORTED: backend never started ===")
    except Exception as ex:
        log("=== FAILED: {!r} ===".format(ex))
        raise
