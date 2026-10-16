"""Every ticket raised in a session, judged against the EXTERNAL ticketing API.

A ticket_ref starting with LOCAL- means the external call never landed and the
test proved nothing, so it is reported as INVALID rather than as a pass.
"""
import io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from sqlalchemy import create_engine, text

sid = sys.argv[1]
u = [l.split("=", 1)[1].strip() for l in open(".env", encoding="utf-8")
     if l.startswith("DATABASE_URL=")][0].replace("mysql+aiomysql", "mysql+pymysql")
e = create_engine(u, connect_args={"connect_timeout": 10})
with e.connect() as c:
    rows = [dict(r._mapping) for r in c.execute(text(
        "SELECT id,ticket_ref,status,room_number,category,department,issue,created_at "
        "FROM new_bot_tickets WHERE session_id=:s ORDER BY id"), {"s": sid})]

print("TICKETS for {}: {}".format(sid, len(rows)))
bad = 0
for r in rows:
    ref = str(r["ticket_ref"] or "")
    local = ref.upper().startswith("LOCAL-")
    if local:
        bad += 1
    print("\n  [{}] {}  {}".format(
        "INVALID - local only" if local else "external OK", ref, r["created_at"]))
    print("      status={} room={} dept={} cat={}".format(
        r["status"], r["room_number"], r["department"], r["category"]))
    print("      {}".format(str(r["issue"])[:190]))
print("\n{} ticket(s), {} local-only (invalid)".format(len(rows), bad))
