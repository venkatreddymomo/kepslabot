"""Does what the model is shown actually identify each ticket?

Rebuilds the `category` value the ticket-edit classifier now sees, from the REAL
rows of the last 50-turn run, and checks that a table booking and a spa booking no
longer both read as "In-Room Dining". No LLM involved - this is about whether the
data tells the truth.
"""
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from sqlalchemy import create_engine, text

SESSION = sys.argv[1] if len(sys.argv) > 1 else "session_1787291825925_1z95ar4k"

url = [l.split("=", 1)[1].strip() for l in open(".env", encoding="utf-8")
       if l.startswith("DATABASE_URL=")][0].replace("mysql+aiomysql", "mysql+pymysql")
eng = create_engine(url, connect_args={"connect_timeout": 10})

with eng.connect() as c:
    rows = [dict(r._mapping) for r in c.execute(text(
        "SELECT ticket_ref, category, sub_category, department, issue "
        "FROM new_bot_tickets WHERE session_id=:s ORDER BY id"), {"s": SESSION})]

if not rows:
    print("no tickets for {}".format(SESSION))
    raise SystemExit(0)


def shown_before(t):
    return str(t.get("category") or "").strip()


def shown_now(t):
    return (str(t.get("sub_category") or "").strip()
            or str(t.get("department") or "").strip())


print("{:<8} {:<26} {:<26} {}".format("ref", "WAS shown (API label)", "NOW shown (what it is)", "issue"))
print("-" * 100)
for t in rows:
    print("{:<8} {:<26} {:<26} {}".format(
        str(t["ticket_ref"]), shown_before(t)[:26], shown_now(t)[:26],
        str(t["issue"])[:40]))

fails = 0

# Every ticket must be distinguishable from the food orders by what is shown.
food = [t for t in rows if "dining" in shown_now(t).lower()]
nonfood = [t for t in rows if "dining" not in shown_now(t).lower()]

print("\nreads as food now : {}".format([t["ticket_ref"] for t in food]))
print("reads as non-food : {}".format([t["ticket_ref"] for t in nonfood]))

for t in rows:
    is_really_food = str(t["issue"] or "").lower().startswith("in-room dining order")
    reads_food = "dining" in shown_now(t).lower()
    if is_really_food != reads_food:
        print("\nFAIL {}: really_food={} but reads_food={} (shown {!r})".format(
            t["ticket_ref"], is_really_food, reads_food, shown_now(t)))
        fails += 1

# and the old label must have been genuinely ambiguous, or this fix is pointless
old_food_labelled = [t["ticket_ref"] for t in rows if "dining" in shown_before(t).lower()]
print("\nunder the OLD label these all read as In-Room Dining: {}".format(old_food_labelled))

print("\n{}".format("ALL PASS - every ticket now says what it is"
                    if not fails else "{} FAILURE(S)".format(fails)))
raise SystemExit(1 if fails else 0)
