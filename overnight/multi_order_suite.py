"""Nine scenarios covering how a guest builds, adds to, and cancels food orders.

The one that matters is S3: a guest who says "new order" should get a SECOND
ticket, not have the food quietly added to the one already with the kitchen.
Everything either side of it is here so a fix for S3 cannot pass by breaking
something that already worked - the forgotten-item merge in S2 especially, which
is the behaviour S3 must NOT destroy.

Judged on the tickets, not the wording: what the kitchen receives is the only
record that decides whether a guest gets fed correctly.
"""
import io, sys, time, uuid, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import httpx
from sqlalchemy import create_engine, text

BASE, HOTEL = "http://localhost:8011", "sarovar_portico_sriperumbudur"
PROFILE = {"entity_id": "6311", "organisation_id": "6311", "guest_id": "993543",
           "guest_name": "Venkat", "guest_phone": "917036718200", "room_number": "1400"}
URL = [l.split("=", 1)[1].strip() for l in open(".env", encoding="utf-8")
       if l.startswith("DATABASE_URL=")][0].replace("mysql+aiomysql", "mysql+pymysql")
ENG = create_engine(URL, connect_args={"connect_timeout": 10})

# Breakfast-safe items so the run does not fail on time-of-day availability.
A1, A2 = "2 masala dosa", "one filter coffee"
B1 = "2 idli"


def say(client, sid, msg):
    r = client.post(BASE + "/api/chat/message", json={
        "session_id": sid, "message": msg, "hotel_code": HOTEL, "channel": "web_widget",
        "metadata": {"phase": "during_stay", "chat_test_profile_phase": "during_stay",
                     "chat_test_profile": PROFILE}}, timeout=300.0)
    d = r.json() if r.status_code == 200 else {"_err": r.text[:200]}
    return str(d.get("display_message") or d.get("message") or d.get("_err") or "")


def tickets(sid):
    with ENG.connect() as c:
        return [dict(r._mapping) for r in c.execute(text(
            "SELECT ticket_ref,status,category,issue FROM new_bot_tickets "
            "WHERE session_id=:s ORDER BY id"), {"s": sid})]


def food(sid):
    return [t for t in tickets(sid)
            if "dining" in str(t["category"]).lower() or "food" in str(t["category"]).lower()]


def scenario(name, turns, check):
    sid = "mo-{}-{}".format(name.split(":")[0].lower(), uuid.uuid4().hex[:6])
    replies = []
    t0 = time.time()
    with httpx.Client() as c:
        for m in turns:
            replies.append(say(c, sid, m))
    tk = tickets(sid)
    ok, detail = check(replies, tk, sid)
    print("\n{}  {}  ({:.0f}s)".format("PASS" if ok else "FAIL", name, time.time() - t0))
    print("   {}".format(detail))
    for t in tk:
        print("     [{}] {} :: {}".format(t["status"], t["ticket_ref"], str(t["issue"])[:95]))
    return ok, name, detail, sid


RESULTS = []
low = lambda s: str(s).lower()

# S1 - one order, one ticket
RESULTS.append(scenario(
    "S1: place one order",
    ["order food - {} and {}".format(A1, A2), "yes place it"],
    lambda r, tk, s: (len(food(s)) == 1, "food tickets={} (want 1)".format(len(food(s))))))

# S2 - forgot an item: must JOIN the existing order, not start a new one
RESULTS.append(scenario(
    "S2: forgot an item -> merges",
    ["order food - {}".format(A1), "yes place it", "oh i forgot, add {} to that".format(A2),
     "yes"],
    lambda r, tk, s: (len(food(s)) == 1 and "coffee" in low(food(s)[0]["issue"]),
                      "food tickets={} (want 1 containing coffee)".format(len(food(s))))))

# S3 - THE BUG: an explicit new order must be its own ticket
RESULTS.append(scenario(
    "S3: new order -> separate ticket",
    ["order food - {}".format(A1), "yes place it", "i want to place a new order",
     B1, "yes place it"],
    lambda r, tk, s: (len(food(s)) == 2,
                      "food tickets={} (want 2 - separate order)".format(len(food(s))))))

# S4 - cancelling the whole order must actually cancel it
RESULTS.append(scenario(
    "S4: cancel the order",
    ["order food - {} and {}".format(A1, A2), "yes place it", "cancel my order", "yes"],
    lambda r, tk, s: (any(low(t["status"]).startswith("cancel") for t in food(s)),
                      "statuses={}".format([t["status"] for t in food(s)]))))

# S5 - removing ONE dish must leave the rest of the order standing
RESULTS.append(scenario(
    "S5: partial cancel",
    ["order food - {} and {}".format(A1, A2), "yes place it", "actually remove the coffee"],
    lambda r, tk, s: (bool(food(s)) and "coffee" not in low(food(s)[0]["issue"])
                      and "dosa" in low(food(s)[0]["issue"]),
                      "ticket now: {}".format(str(food(s)[0]["issue"])[:80] if food(s) else "none"))))

# S6 - the guest can be told what they ordered, with money
RESULTS.append(scenario(
    "S6: recall",
    ["order food - {}".format(A1), "yes place it", "what did i order"],
    lambda r, tk, s: ("dosa" in low(r[-1]) and any(x in r[-1] for x in ("₹", "Rs", "total", "Total")),
                      "recall reply: {}".format(r[-1][:90].replace("\n", " ")))))

# S7 - another team's request must not disturb the order
RESULTS.append(scenario(
    "S7: topic switch",
    ["order food - {}".format(A1), "yes place it", "the AC in my room is not cooling", "yes",
     "what did i order"],
    lambda r, tk, s: ("dosa" in low(r[-1]) and len(food(s)) == 1,
                      "food tickets={} recall={}".format(len(food(s)), r[-1][:70].replace("\n", " ")))))

# S8 - two orders, then cancel the SECOND one by number
RESULTS.append(scenario(
    "S8: cancel the second order",
    ["order food - {}".format(A1), "yes place it", "i want to place a new order", B1,
     "yes place it", "cancel my second order", "yes"],
    lambda r, tk, s: (
        len(food(s)) == 2
        and any(low(t["status"]).startswith("cancel") for t in food(s))
        and any(not low(t["status"]).startswith("cancel") for t in food(s)),
        "statuses={} (want one cancelled, one open)".format([t["status"] for t in food(s)]))))

# S9 - a second order must not silently absorb the first one's items
RESULTS.append(scenario(
    "S9: orders stay separate",
    ["order food - {}".format(A1), "yes place it", "i want to place a new order", B1,
     "yes place it"],
    lambda r, tk, s: (
        len(food(s)) == 2 and not all("dosa" in low(t["issue"]) for t in food(s)),
        "issues={}".format([str(t["issue"])[:45] for t in food(s)]))))

print("\n" + "=" * 72)
n = sum(1 for ok, *_ in RESULTS if ok)
for ok, name, detail, sid in RESULTS:
    print("{}  {:<34} {}".format("PASS" if ok else "FAIL", name.split(":")[0], detail[:70]))
print("\n{}/{} passed".format(n, len(RESULTS)))
json.dump([{"ok": ok, "name": nm, "detail": d, "session": s} for ok, nm, d, s in RESULTS],
          open("overnight/multi_order_result.json", "w", encoding="utf-8"), indent=1)
