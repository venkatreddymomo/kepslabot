"""The store against a real database - a throwaway SQLite one.

test_ird_cart_book.py proves the RULES. It does not prove the plumbing: that the
JSON column round-trips a dict, that save_slots writes only the slots it was
given, that the unique key does what it should, that the sweep deletes by age.
Those need a database, and waiting for the prod one to come back to discover a
typo in a column name is a poor trade when SQLite will answer the same questions
in a second.

Not a substitute for the real thing - MySQL JSON and SQLite JSON are not
identical, and the prod run still has to happen. It just means the first time the
store meets MySQL, the obvious things are already known to work.

    python overnight/test_ird_cart_store_sqlite.py
"""
from __future__ import annotations

import asyncio
import datetime
import io
import sys
import tempfile
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    results.append((bool(ok), name, detail))
    print("%s %s%s" % ("PASS " if ok else "FAIL ", name,
                       ("  -> " + detail) if detail and not ok else ""))


async def main() -> int:
    tmp = Path(tempfile.mkdtemp()) / "book.sqlite3"
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from models.database import Base, IRDGuestOrders
    from services import ird_cart_book as B
    import services.ird_cart_store as store

    engine = create_async_engine("sqlite+aiosqlite:///%s" % tmp.as_posix())
    async with engine.begin() as conn:
        # Only our table, so a mismatch elsewhere in the schema cannot mask a
        # problem in this one.
        await conn.run_sync(Base.metadata.create_all,
                            tables=[IRDGuestOrders.__table__])
    check(True, "the table is creatable from the model as written")

    # Point the store at the throwaway database.
    store.AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    HC, SID = "sarovar_portico_sriperumbudur", "session_test_book"

    row = await store.load(hotel_code=HC, session_id=SID,
                           guest={"room_number": "1400", "guest_name": "Venkat"})
    check(bool(row), "a book is opened on first use")
    check((row or {}).get("room_number") == "1400", "and it copies the room number")
    check(all(("cart_%d" % n) in (row or {}) for n in range(1, 11)),
          "all ten slots come back")

    # Same session twice must not make a second book.
    again = await store.load(hotel_code=HC, session_id=SID)
    check((again or {}).get("id") == (row or {}).get("id"),
          "loading again returns the SAME book, not a new one")

    # A detail that arrives late must fill in, not be ignored - the room number
    # often lands a turn or two after the session does, and a ticket without one
    # cannot be routed.
    await store.load(hotel_code=HC, session_id="session_late")
    late = await store.load(hotel_code=HC, session_id="session_late",
                            guest={"room_number": "1501"})
    check((late or {}).get("room_number") == "1501",
          "a room number arriving later is filled in")

    # ── write a slot and read it back ───────────────────────────────────────
    slots = B.slots_from_row(row)
    now = datetime.datetime.now().replace(microsecond=0)
    slots, changed = B.apply_write(
        slots,
        {"items": [{"item": "Chicken Burger", "qty": 2, "price": 750}],
         "note": "no onions"},
        now=now, window_minutes=5)
    ok = await store.save_slots(hotel_code=HC, session_id=SID,
                                slots=[s for s in slots if s["n"] in changed])
    check(ok, "saving a slot reports success")

    back = await store.load(hotel_code=HC, session_id=SID)
    got = B.slots_from_row(back or {})[0]
    check([i["item"] for i in got["items"]] == ["Chicken Burger"],
          "THE DISH SURVIVES THE ROUND TRIP", str(got["items"]))
    check(got["items"][0]["qty"] == 2, "with its quantity")
    check(got["items"][0].get("price") == 750, "and its price")
    check(got["note"] == "no onions", "the guest note survives too", got["note"])
    check(got["status"] == "ongoing", "and the status", str(got["status"]))

    # ── placing it ──────────────────────────────────────────────────────────
    slots = B.slots_from_row(back or {})
    slots, changed = B.apply_write(
        slots, {"slot": 1, "items": [{"item": "Chicken Burger", "qty": 2, "price": 750}],
                "status": "placed"},
        now=now, window_minutes=5)
    slots[0]["ticket_ref"] = "11135"
    await store.save_slots(hotel_code=HC, session_id=SID, slots=[slots[0]])
    back = await store.load(hotel_code=HC, session_id=SID)
    got = B.slots_from_row(back or {})[0]
    check(got["status"] == "placed", "placing persists")
    check(got["ticket_ref"] == "11135", "the ticket ref persists", str(got["ticket_ref"]))
    check(isinstance(got["placed_at"], datetime.datetime), "the timestamp persists",
          repr(got["placed_at"]))

    # ── only the named slot is touched ──────────────────────────────────────
    slots = B.slots_from_row(back or {})
    slots[1]["items"] = [{"item": "Tiramisu", "qty": 1, "price": 500}]
    slots[1]["status"] = "ongoing"
    await store.save_slots(hotel_code=HC, session_id=SID, slots=[slots[1]])
    back = await store.load(hotel_code=HC, session_id=SID)
    fresh = B.slots_from_row(back or {})
    check([i["item"] for i in fresh[0]["items"]] == ["Chicken Burger"],
          "writing slot 2 leaves slot 1 ALONE", str(fresh[0]["items"]))
    check(fresh[0]["ticket_ref"] == "11135", "and leaves its ticket ref alone")
    check([i["item"] for i in fresh[1]["items"]] == ["Tiramisu"], "slot 2 holds its dish")

    # ── the projection, end to end from a real row ──────────────────────────
    pd: dict = {}
    B.project_to_pending_data(fresh, pd)
    check([i["item"] for i in pd["_ird_cart"]] == ["Tiramisu"],
          "the cart derived from a real row is only the unplaced order",
          str(pd["_ird_cart"]))
    check(pd["_ird_order_ticket_ref"] == "11135", "and the open ticket ref comes through")

    # ── emptying a slot really clears the columns ───────────────────────────
    fresh[1]["items"] = []
    fresh[1]["status"] = ""
    await store.save_slots(hotel_code=HC, session_id=SID, slots=[fresh[1]])
    back = await store.load(hotel_code=HC, session_id=SID)
    check((back or {}).get("cart_2") is None, "an emptied slot writes NULL",
          repr((back or {}).get("cart_2")))

    # ── saving into a book that does not exist must not create one ──────────
    ok = await store.save_slots(hotel_code=HC, session_id="never_opened",
                                slots=[{"n": 1, "items": [{"item": "X", "qty": 1}],
                                        "note": "", "status": "ongoing",
                                        "placed_at": None, "ticket_ref": ""}])
    check(ok is False, "saving to a book that was never opened reports failure")
    check(await store.load(hotel_code=HC, session_id="never_opened",
                           create=False) is None,
          "and does not conjure one into existence")

    # ── the sweep ───────────────────────────────────────────────────────────
    swept = await store.sweep(older_than_hours=24)
    check(swept == 0, "nothing fresh is swept", str(swept))

    from sqlalchemy import text as _t
    async with store.AsyncSessionLocal() as s:
        await s.execute(_t("UPDATE new_bot_ird_guest_orders SET updated_at=:old "
                           "WHERE session_id=:sid"),
                        {"old": datetime.datetime.now() - datetime.timedelta(hours=30),
                         "sid": SID})
        await s.commit()
    swept = await store.sweep(older_than_hours=24)
    check(swept == 1, "a stale book IS swept", str(swept))
    check(await store.load(hotel_code=HC, session_id=SID, create=False) is None,
          "and it is really gone")
    check(await store.load(hotel_code=HC, session_id="session_late",
                           create=False) is not None,
          "while a fresh one is untouched")

    await engine.dispose()
    try:
        tmp.unlink()
    except OSError:
        pass

    fails = [n for ok_, n, _ in results if not ok_]
    print("\n" + "=" * 66)
    print("%d/%d passed" % (len(results) - len(fails), len(results)))
    for n in fails:
        print("  FAILED: " + n)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
