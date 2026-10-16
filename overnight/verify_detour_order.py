"""The turn-12/13 bug, reproduced exactly.

The guest orders food, then a long detour (broken AC) pushes the original
order message out of the window the provenance judge can see. On the way
back the cart is rebuilt from scratch, so those items look brand new — and
the judge, answering honestly for what it could see, DELETED them. The
guest was left with a Tiramisu-only order and "yes place it" did nothing.

Pass conditions:
  1. after the detour the order still holds the murgh and the naan
  2. "yes place it" PLACES it on that turn, not one turn later
  3. the ticket the kitchen gets lists the real items
  4. nothing the guest never asked for is on it
"""
import io, sys, time, uuid, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import httpx
from sqlalchemy import create_engine, text

BASE, HOTEL = "http://localhost:8011", "sarovar_portico_sriperumbudur"
SID = "detour-" + uuid.uuid4().hex[:8]
PROFILE = {"entity_id": "6311", "organisation_id": "6311", "guest_id": "993543",
           "guest_name": "Venkat", "guest_phone": "917036718200", "room_number": "1400"}
URL = [l.split("=", 1)[1].strip() for l in open(".env", encoding="utf-8")
       if l.startswith("DATABASE_URL=")][0].replace("mysql+aiomysql", "mysql+pymysql")

TURNS = [
    "Order food",
    "one tandoori murg and 2 garlic naan.",
    # ---- the detour: long enough to push the order out of the judge's view ----
    "actually my ac is not working",
    "its blowing warm air",
    "since morning",
    "yes please send someone",
    "how long will they take",
    "ok thanks",
    "is there a fan i can use meanwhile",
    "alright",
    # ---- back to the food ----
    "yes lets continue",
    "yes place it",
]

replies = []
with httpx.Client() as c:
    for i, m in enumerate(TURNS, 1):
        t0 = time.time()
        r = c.post(BASE + "/api/chat/message", json={
            "session_id": SID, "message": m, "hotel_code": HOTEL, "channel": "web_widget",
            "metadata": {"phase": "during_stay", "chat_test_profile_phase": "during_stay",
                         "chat_test_profile": PROFILE}}, timeout=300.0)
        d = r.json() if r.status_code == 200 else {"_err": r.text[:200]}
        rep = str(d.get("display_message") or d.get("message") or d.get("_err") or "")
        replies.append(rep)
        print("\n[{:>2}] ({:>3.0f}s) GUEST: {}".format(i, time.time() - t0, m))
        print("        BOT  : " + rep[:400].replace("\n", "\n               "))

eng = create_engine(URL, connect_args={"connect_timeout": 8})
with eng.connect() as c:
    tk = [dict(r._mapping) for r in c.execute(text(
        "SELECT ticket_ref,status,room_number,category,issue FROM new_bot_tickets "
        "WHERE session_id=:s ORDER BY id"), {"s": SID})]

print("\n" + "=" * 70 + "\nTICKETS RAISED:")
for t in tk:
    print("  {} [{}] room={} cat={}\n      {}".format(
        t["ticket_ref"], t["status"], t["room_number"], t["category"], str(t["issue"])[:150]))
if not tk:
    print("  (none)")

low = lambda s: str(s).lower()
resume, place = replies[10], replies[11]
food_tk = [t for t in tk if "tandoori" in low(t["issue"]) or "naan" in low(t["issue"])
           or "dining" in low(t["category"]) or "food" in low(t["category"])]
food_txt = " ".join(str(t["issue"]) for t in food_tk).lower()

checks = [
    ("resume keeps the tandoori murgh",  "tandoori" in low(resume)),
    ("resume keeps the garlic naan",     "naan" in low(resume)),
    ("'yes place it' places it THAT turn",
     any(k in low(place) for k in ("order placed", "on its way", "on the way",
                                   "sent to", "kitchen", "confirmed"))
     and "shall i place" not in low(place)),
    ("a food ticket exists",             bool(food_tk)),
    ("ticket lists the tandoori murgh",  "tandoori" in food_txt),
    ("ticket lists the garlic naan",     "naan" in food_txt),
    ("nothing unordered on the ticket",  "tiramisu" not in food_txt and "wine" not in food_txt),
]
print("\nRESULTS")
for label, ok in checks:
    print("  {}  {}".format("PASS" if ok else "FAIL", label))
n = sum(1 for _, o in checks if o)
print("\n{}/{} passed  (session {})".format(n, len(checks), SID))
json.dump({"sid": SID, "replies": replies, "tickets": [
    {k: str(v) for k, v in t.items()} for t in tk]},
    open("overnight/detour_result.json", "w", encoding="utf-8"), indent=1)
