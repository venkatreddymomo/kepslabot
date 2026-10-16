"""Loading and saving the guest's order book. The thin half.

All the rules live in services/ird_cart_book.py, which knows nothing about a
database so the slot logic can be tested without one. This module is only the
trip to MySQL and back.

TWO SHORT VISITS PER TURN, NEVER ONE LONG ONE. Load at the start of a turn,
close, run the LLM, then open again to save. A session held across an LLM call
pins a pooled connection for the whole of it, which is exactly what put the
concurrency ceiling at ~15 turns before. Nothing here is awaited while a model
is thinking.

FAILS OPEN. Every entry point returns None or False rather than raising: a
database hiccup must degrade the order book, not take the guest's conversation
down with it. Callers treat None as "no book this turn" and fall back to the
existing pending_data keys.

GHN IS READ-ONLY. Guest identity is copied in when the row is created and never
written back.
"""
from __future__ import annotations

import datetime
import logging
from typing import Any

from sqlalchemy import delete, select, update

from models.database import AsyncSessionLocal, IRDGuestOrders
from services import ird_cart_book as book

logger = logging.getLogger(__name__)

# Columns that make up the identity half of the row, so a refresh can update
# them without naming each one at every call site.
_IDENTITY = ("guest_name", "room_number", "check_in", "check_out", "conversation_id")


def _row_to_dict(row: IRDGuestOrders) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": row.id,
        "hotel_code": row.hotel_code,
        "session_id": row.session_id,
        "conversation_id": row.conversation_id,
        "guest_name": row.guest_name,
        "room_number": row.room_number,
        "check_in": row.check_in,
        "check_out": row.check_out,
    }
    for n in range(1, book.SLOT_COUNT + 1):
        for stem in ("cart", "status", "placed_at", "ticket_ref"):
            key = "%s_%d" % (stem, n)
            out[key] = getattr(row, key, None)
    return out


async def load(
    *,
    hotel_code: str,
    session_id: str,
    guest: dict[str, Any] | None = None,
    create: bool = True,
) -> dict[str, Any] | None:
    """The guest's book, created on first use.

    `guest` is whatever identity we already resolved for this turn (name, room,
    stay dates) - copied in on creation and refreshed when it was previously
    blank. It is only ever copied FROM GHN; nothing goes back the other way.
    """
    hotel_code = str(hotel_code or "").strip()
    session_id = str(session_id or "").strip()
    if not hotel_code or not session_id:
        return None
    try:
        async with AsyncSessionLocal() as s:
            row = (await s.execute(
                select(IRDGuestOrders).where(
                    IRDGuestOrders.hotel_code == hotel_code,
                    IRDGuestOrders.session_id == session_id,
                )
            )).scalar_one_or_none()

            if row is None:
                if not create:
                    return None
                row = IRDGuestOrders(hotel_code=hotel_code, session_id=session_id)
                for k in _IDENTITY:
                    v = (guest or {}).get(k)
                    if v not in (None, ""):
                        setattr(row, k, v)
                s.add(row)
                await s.commit()
                await s.refresh(row)
                logger.info("[IRD_BOOK] opened a book for session=%s room=%s",
                            session_id, getattr(row, "room_number", None))
            elif guest:
                # Fill in anything that was blank when the row was created - the
                # room number often arrives a turn or two after the session does,
                # and a ticket without it cannot be routed.
                changed = {k: guest[k] for k in _IDENTITY
                           if guest.get(k) not in (None, "")
                           and getattr(row, k, None) in (None, "")}
                if changed:
                    for k, v in changed.items():
                        setattr(row, k, v)
                    await s.commit()
                    await s.refresh(row)
                    logger.info("[IRD_BOOK] filled in %s for session=%s",
                                sorted(changed), session_id)
            return _row_to_dict(row)
    except Exception as exc:
        logger.warning("[IRD_BOOK] could not load the book (non-fatal): %r", exc)
        return None


async def save_slots(
    *,
    hotel_code: str,
    session_id: str,
    slots: list[dict[str, Any]],
) -> bool:
    """Write the given slots back. Only the slots passed in are touched.

    Deliberately not a whole-row overwrite: two things can move in one turn (a
    new order opens while an older one flips to processing) and rewriting all ten
    slots from a snapshot read before the LLM ran would undo whichever one
    changed underneath.
    """
    hotel_code = str(hotel_code or "").strip()
    session_id = str(session_id or "").strip()
    if not hotel_code or not session_id or not slots:
        return False
    values: dict[str, Any] = {}
    for slot in slots:
        values.update(book.row_updates_for_slot(slot))
    try:
        async with AsyncSessionLocal() as s:
            res = await s.execute(
                update(IRDGuestOrders)
                .where(IRDGuestOrders.hotel_code == hotel_code,
                       IRDGuestOrders.session_id == session_id)
                .values(**values)
            )
            await s.commit()
            if not res.rowcount:
                logger.warning("[IRD_BOOK] nothing written - no book for session=%s",
                               session_id)
                return False
        logger.info("[IRD_BOOK] wrote slot(s) %s for session=%s",
                    [sl["n"] for sl in slots], session_id)
        return True
    except Exception as exc:
        logger.warning("[IRD_BOOK] could not save slots %s (non-fatal): %r",
                       [sl.get("n") for sl in slots], exc)
        return False


async def sweep(*, older_than_hours: int = 24) -> int:
    """Drop books nobody has touched for a day, so the slots recycle.

    Safe to lose: every placed order is on record as an external ticket, and
    recall falls back to those when the book has no row.
    """
    cutoff = datetime.datetime.now() - datetime.timedelta(hours=max(1, older_than_hours))
    try:
        async with AsyncSessionLocal() as s:
            res = await s.execute(
                delete(IRDGuestOrders).where(IRDGuestOrders.updated_at < cutoff))
            await s.commit()
            n = int(res.rowcount or 0)
        if n:
            logger.info("[IRD_BOOK] swept %d book(s) older than %dh", n, older_than_hours)
        return n
    except Exception as exc:
        logger.warning("[IRD_BOOK] sweep failed (non-fatal): %r", exc)
        return 0
