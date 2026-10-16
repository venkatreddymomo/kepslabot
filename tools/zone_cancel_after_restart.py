"""Reproduce (and verify the fix for) the cancel-after-restart hole.

THE BUG. `session_has_known_tickets` reads an in-memory set that a restart
wipes, but the tickets live in the DB keyed by session_id. The edit/cancel
interceptor only loaded the session's tickets when that in-memory set knew the
session, so after a deploy a guest cancelling was answered "I've noted that for
our team" while the ticket stayed OPEN — nothing cancelled, nobody told.

HOW THIS REPRODUCES IT WITHOUT A RESTART. "After a restart" is exactly the state
"the DB holds a ticket for this session, and the running process has never heard
of it". Inserting a ticket row directly puts the server in precisely that state,
so no deploy is needed and the test is deterministic.

    python tools/zone_cancel_after_restart.py --setup    # insert, print session id
    python tools/zone_cancel_after_restart.py --check    # read status back
    python tools/zone_cancel_after_restart.py --cleanup  # delete the row it made

Only ever touches the single row it created, by primary key.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import datetime, timezone

import pymysql

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

STATE = pathlib.Path(__file__).with_name("_cancel_restart_state.json")
DB = dict(host="172.16.5.32", port=3306, user="root", password="CHANGE_ME",
          database="GHN_PROD_BAK", connect_timeout=20)

# Must be a ticket Zone would really raise in PRE-BOOKING. An arrival/in-stay
# request (airport pickup, housekeeping) never exists in this phase, so testing
# with one proves the lookup works but says nothing about whether the phase gate
# lets the cancel through. Reservations is one of Zone's four live departments.
ISSUE = "Room booking assistance requested for Zone by The Park, Coimbatore"
REF = "TEST-RESTART-CANCEL"


def setup() -> int:
    sid = f"restartcancel_{int(datetime.now(timezone.utc).timestamp())}"
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    conn = pymysql.connect(**DB)
    try:
        with conn.cursor() as c:
            c.execute(
                """INSERT INTO new_bot_tickets
                   (ticket_ref, hotel_id, session_id, hotel_code, property_name,
                    source, issue, message, description, category, phase,
                    priority, status, is_escalated, created_at, updated_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (REF, 67, sid, "zone", "Zone by The Park, Coimbatore",
                 "test_harness", ISSUE, ISSUE, ISSUE, "Reservations",
                 "pre_booking", "medium", "open", 0, now, now),
            )
            row_id = c.lastrowid
        conn.commit()
    finally:
        conn.close()
    STATE.write_text(json.dumps({"session_id": sid, "row_id": row_id}), encoding="utf-8")
    print(f"session_id={sid}")
    print(f"row_id={row_id}  ref={REF}  status=open  issue={ISSUE!r}")
    print("\nThe server process has never seen this session -> the exact state a")
    print("restart leaves behind. Now send a cancel on this session_id.")
    return 0


def check() -> int:
    st = json.loads(STATE.read_text(encoding="utf-8"))
    conn = pymysql.connect(**DB)
    try:
        with conn.cursor() as c:
            c.execute("""SELECT status, manager_notes, issue FROM new_bot_tickets
                         WHERE id=%s""", (st["row_id"],))
            row = c.fetchone()
    finally:
        conn.close()
    if not row:
        print("row is gone")
        return 1
    status, notes, issue = row
    notes = str(notes or "")
    print(f"status        = {status!r}")
    print(f"manager_notes = {notes[:300]!r}")
    cancelled = str(status or "").lower().startswith("cancel")
    noted = "CANCELLED" in notes.upper()
    print()
    print(f"status flipped to cancel : {cancelled}")
    print(f"cancellation note added  : {noted}")
    print(f"VERDICT: {'FIXED' if (cancelled and noted) else 'STILL BROKEN'}")
    return 0 if (cancelled and noted) else 2


def cleanup() -> int:
    if not STATE.exists():
        print("no state file; nothing to clean")
        return 0
    st = json.loads(STATE.read_text(encoding="utf-8"))
    conn = pymysql.connect(**DB)
    try:
        with conn.cursor() as c:
            c.execute("DELETE FROM new_bot_tickets WHERE id=%s AND ticket_ref=%s",
                      (st["row_id"], REF))
            n = c.rowcount
        conn.commit()
    finally:
        conn.close()
    STATE.unlink(missing_ok=True)
    print(f"deleted {n} test row(s)")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--setup", action="store_true")
    p.add_argument("--check", action="store_true")
    p.add_argument("--cleanup", action="store_true")
    a = p.parse_args()
    if a.setup:
        raise SystemExit(setup())
    if a.check:
        raise SystemExit(check())
    if a.cleanup:
        raise SystemExit(cleanup())
    p.print_help()
