"""Every ticket a session raised, judged on the HOTEL's side of the wire.

Our own new_bot_tickets row proves only that we tried. The question that matters
is whether the ticket reached the dashboard the staff actually work from, with
the right department, category and words on it - so this reads
GHN_OPERATION_TICKETS (read-only) and reports anything our row claims that the
dashboard does not confirm.

    python overnight/check_external.py <session_id>
"""
import io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from sqlalchemy import create_engine, text

sid = sys.argv[1]
u = [l.split("=", 1)[1].strip() for l in open(".env", encoding="utf-8")
     if l.startswith("DATABASE_URL=")][0].replace("mysql+aiomysql", "mysql+pymysql")
e = create_engine(u, connect_args={"connect_timeout": 20})

with e.connect() as c:
    ours = [dict(r._mapping) for r in c.execute(text(
        "SELECT id, ticket_ref, category, description, created_at "
        "FROM new_bot_tickets WHERE session_id=:s ORDER BY id"), {"s": sid})]

    print("session %s - %d ticket(s) on our side" % (sid, len(ours)))
    problems = 0
    for r in ours:
        ref = str(r["ticket_ref"] or "").strip()
        if not ref or ref.upper().startswith("LOCAL-"):
            problems += 1
            print("\n  [INVALID] ref=%r - never reached the external API" % ref)
            print("      %s" % str(r["description"])[:150])
            continue
        ext = c.execute(text(
            "SELECT id, department_alloc, category, sub_category, status, "
            "issue, room_number, created_at FROM GHN_OPERATION_TICKETS "
            "WHERE id=:t"), {"t": ref}).mappings().first()
        if not ext:
            problems += 1
            print("\n  [MISSING] %s is in our table but NOT on the dashboard" % ref)
            print("      %s" % str(r["description"])[:150])
            continue
        print("\n  [ON DASHBOARD] %s  status=%s  dept=%s" % (
            ref, ext["status"], ext["department_alloc"]))
        print("      cat=%s / sub=%s  room=%s" % (
            ext["category"], ext["sub_category"], ext["room_number"]))
        print("      %s" % str(ext["issue"])[:170])

    print("\n%s" % ("=" * 60))
    print("%d confirmed on the dashboard, %d problem(s)"
          % (len(ours) - problems, problems))
