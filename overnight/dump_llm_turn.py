"""What the model actually received and actually emitted for one turn.

Settles "did it even see this?" instead of guessing. Usage:
    python overnight/dump_llm_turn.py <session_id> "<text the guest typed>"
"""
import io
import json
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from sqlalchemy import create_engine, text

sid = sys.argv[1]
needle = sys.argv[2] if len(sys.argv) > 2 else ""
want = sys.argv[3] if len(sys.argv) > 3 else ""

url = [l.split("=", 1)[1].strip() for l in open(".env", encoding="utf-8")
       if l.startswith("DATABASE_URL=")][0].replace("mysql+aiomysql", "mysql+pymysql")
eng = create_engine(url, connect_args={"connect_timeout": 10})

with eng.connect() as c:
    rows = [dict(r._mapping) for r in c.execute(text(
        "SELECT id, call_site, input_text, output_text, ts "
        "FROM new_bot_log_llm WHERE session_id=:s ORDER BY id"), {"s": sid})]

print("LLM calls for {}: {}".format(sid, len(rows)))
hits = [r for r in rows if needle.lower() in str(r["input_text"] or "").lower()] if needle else rows
print("matching {!r}: {}".format(needle, len(hits)))

for r in hits:
    role = str(r["call_site"] or "")
    if want and want.lower() not in role.lower():
        continue
    inp = str(r["input_text"] or "")
    out = str(r["output_text"] or "")
    print("\n" + "=" * 78)
    print("id={} role={} at={}".format(r["id"], role, r["ts"]))

    # what the model was told about the order state
    for key in ("menu_serving_windows", "food_order_status", "current_confirmed_order",
                "not_yet_placed", "is_a_new_separate_order"):
        m = re.search(re.escape(key) + r'"?\s*:\s*', inp)
        print("  input has {:28s} {}".format(key + ":", "YES" if m else "no"))

    print("\n  --- OUTPUT ---")
    try:
        o = json.loads(out)
        tk = o.get("ticket") or {}
        print("  response_text : {!r}".format(str(o.get("response_text"))[:220]))
        print("  pending_action: {!r}".format(o.get("pending_action")))
        print("  ticket.required     = {}".format(tk.get("required")))
        print("  ticket.ready_to_create = {}".format(tk.get("ready_to_create")))
        print("  ticket.category     = {!r}".format(tk.get("category")))
        for k in ("confirmation_pending", "context_switched", "action"):
            if k in o:
                print("  {:19s} = {!r}".format(k, o[k]))
    except ValueError:
        print("  (not JSON) " + out[:600])
