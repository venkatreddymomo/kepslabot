"""What state is each food order in, and what may the bot say about it?

Every confusing thing the in-room dining bot has done about orders came from the
same place: "is this food placed or not?" was answered by inference, from a cart
that survives placement, a timestamp, a ticket ref, and three separate stores that
could disagree. The model was asked to work it out and, given contradictory
inputs, sometimes worked it out wrong - offering to place food already cooking,
or re-sending an order the kitchen had.

So the question is answered ONCE, here, as a state, and each state carries the one
thing the bot is allowed to say about it. Nothing is left to infer.

    EMPTY       nothing in the cart                    -> invite an order
    ONGOING     items in the cart, not placed          -> the ONLY state where
                                                          "shall I place this
                                                          order?" is allowed
    PLACED      with the kitchen, still changeable      -> "placed, on its way";
                                                          edits allowed
    PROCESSING  the kitchen has started                -> "being prepared";
                                                          NO edits, no cancels
    DELIVERED   the ticket is closed                    -> a past order

PLACED becomes PROCESSING on whichever comes first:
  * `ird_edit_window_minutes` (default 5) elapses since it was placed, or
  * the staff dashboard moves the ticket off `open` - a real person picking it up
    beats any clock, and the dashboard's own table is in the database we are
    already connected to.

A conversation holds ONE cart and MANY orders, each with its own state. That is
what makes multiple orders tractable: "your second order" is a row with a state,
not something reconstructed from chat.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

EMPTY = "empty"
ONGOING = "ongoing"
PLACED = "placed"
PROCESSING = "processing"
DELIVERED = "delivered"

# What the bot may say in each state. Written as instructions because they end up
# in the payload verbatim - the model should never have to derive these.
_SAY: dict[str, str] = {
    EMPTY: (
        "The cart was empty when this message arrived. Do NOT claim the guest has "
        "an order already in progress. BUT THIS IS A SNAPSHOT FROM BEFORE YOUR "
        "TURN, NOT A VERDICT ON IT: if the guest has just asked for a dish, or "
        "just said yes to one you offered, that dish goes INTO the cart now and "
        "you carry on as normal. Never answer a request for food by saying the "
        "cart is empty and there is nothing to place - the thing they just asked "
        "for is what fills it."
    ),
    ONGOING: (
        "These items are NOT with the kitchen yet. THIS IS THE ONLY STATE IN WHICH "
        "YOU MAY ASK TO PLACE AN ORDER - show the items with their prices and the "
        "total, and close with one question meaning 'shall I place this order?'. "
        "The guest can still add, remove or change anything."
    ),
    PLACED: (
        "This order IS with the kitchen. Say it is placed and on its way, and NEVER "
        "ask the guest to place, confirm or approve it again - it has already gone. "
        "BUT IT IS STILL OPEN. The kitchen has not started it, so a further dish "
        "CAN still be added to this very order, and it can still be changed or "
        "cancelled. If the guest asks for another dish, ADD IT TO THIS ORDER - do "
        "not tell them it is too late and do not start a separate order for it. "
        "Saying 'it is already on its way so I cannot add to it' is WRONG in this "
        "state and has cost guests the food they asked for."
    ),
    PROCESSING: (
        "The kitchen has STARTED on this order. Say it is being prepared. NEVER ask "
        "to place or confirm it. It can NO LONGER be changed or cancelled - if the "
        "guest asks, say so plainly and kindly, and offer to place anything new as "
        "a separate order rather than pretending the change was made."
    ),
    DELIVERED: (
        "This order has been delivered. Refer to it in the past tense. It cannot be "
        "changed. If the guest wants the same again, that is a NEW order."
    ),
}


def _num(v: Any) -> "float | None":
    try:
        return float(str(v).replace("₹", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _qty(row: dict[str, Any]) -> int:
    try:
        return max(1, int(row.get("qty") or 1))
    except (TypeError, ValueError):
        return 1


def _age_minutes(stamp: str) -> "float | None":
    raw = str(stamp or "").strip()
    if not raw:
        return None
    try:
        t = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds() / 60.0
    except (TypeError, ValueError):
        return None


def _lines(items: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i in items or []:
        if not isinstance(i, dict):
            continue
        name = str(i.get("item") or i.get("name") or "").strip()
        if name:
            out.append({"item": name, "qty": _qty(i), "price": i.get("price")})
    return out


def _total(lines: list[dict[str, Any]]) -> "int | None":
    vals = [
        (_num(l.get("price")) * l["qty"]) if _num(l.get("price")) is not None else None
        for l in lines
    ]
    return int(sum(vals)) if vals and all(v is not None for v in vals) else None


async def _order_state(
    *, ticket_ref: str, placed_at: str, window_minutes: int
) -> tuple[str, str]:
    """(state, why) for one placed order.

    The dashboard's verdict beats the clock in BOTH directions: a ticket a person
    has picked up is processing however recent, and a ticket still untouched stays
    editable right up to the window. Only when the dashboard has no opinion does
    the clock decide alone - which is exactly how it behaved before states existed.
    """
    ref = str(ticket_ref or "").strip()
    picked: "bool | None" = None
    closed = False
    if ref:
        try:
            from services.ticketing_service import ticketing_service as _ts

            picked = await _ts._staff_have_picked_up(ref)
            closed = bool(await _ticket_is_closed(ref))
        except Exception as exc:
            logger.warning("[IRD_STATE] dashboard lookup failed for %s: %r", ref, exc)
            picked = None

    if closed:
        return DELIVERED, "the ticket has been closed on the staff dashboard"
    if picked is True:
        return PROCESSING, "the team has picked this up on the staff dashboard"

    age = _age_minutes(placed_at)
    win = int(window_minutes or 0)
    if win > 0 and age is not None and age > win:
        return PROCESSING, f"more than {win} minutes have passed since it was placed"
    return PLACED, "it is with the kitchen and still inside the change window"


async def _ticket_is_closed(ticket_ref: str) -> "bool | None":
    """Has the dashboard closed this ticket? None when we cannot tell, which is
    treated as not closed - claiming food was delivered when it was not is the
    worse mistake."""
    ref = str(ticket_ref or "").strip()
    if not ref:
        return None
    try:
        from models.database import AsyncSessionLocal
        from sqlalchemy import text as _text

        async with AsyncSessionLocal() as db:
            row = (
                await db.execute(
                    _text("SELECT status FROM GHN_OPERATION_TICKETS WHERE id = :t LIMIT 1"),
                    {"t": ref},
                )
            ).mappings().first()
        if not row:
            return None
        return str(row.get("status") or "").strip().lower() in {
            "closed", "close", "completed", "complete", "resolved", "delivered"
        }
    except Exception:
        return None


async def build_lifecycle(
    pending_data: Any, *, window_minutes: int = 5
) -> dict[str, Any]:
    """The whole food picture for this conversation: one cart, many orders, each
    with a state and the one thing that may be said about it.

    Never raises. An empty dict means "say nothing new", which leaves every
    existing behaviour exactly as it was.
    """
    pd = pending_data if isinstance(pending_data, dict) else {}
    try:
        cart_lines = _lines(pd.get("_ird_cart") or [])
        ledger = [r for r in (pd.get("_ird_placed_orders") or []) if isinstance(r, dict)]

        orders: list[dict[str, Any]] = []
        for row in ledger:
            ref = str(row.get("ticket_ref") or "").strip()
            state, why = await _order_state(
                ticket_ref=ref,
                placed_at=str(row.get("placed_at") or pd.get("_ird_order_placed_at") or ""),
                window_minutes=window_minutes,
            )
            lines = _lines(row.get("items") or [])
            orders.append({
                "order_no": int(row.get("n") or 0) or (len(orders) + 1),
                "ticket_ref": ref,
                "state": state,
                "can_be_changed": state == PLACED,
                "why": why,
                "items": [f"{l['qty']} x {l['item']}" for l in lines][:20],
                "total": row.get("total") if row.get("total") is not None else _total(lines),
                "what_you_may_say": _SAY[state],
            })

        # THE CART IS ONLY WHAT IS NOT YET WITH THE KITCHEN. A cart line that is
        # already on a placed order is not "awaiting placement" - describing it
        # that way is what had the bot offer to place food it had just sent.
        held: dict[str, int] = {}
        for o, row in zip(orders, ledger):
            if o["state"] == DELIVERED:
                continue
            for l in _lines(row.get("items") or []):
                k = l["item"].strip().lower()
                held[k] = max(held.get(k, 0), l["qty"])
        # A cart deliberately reset for a fresh order has nothing stale in it.
        if not pd.get("_ird_fresh_order") and held:
            trimmed: list[dict[str, Any]] = []
            for l in cart_lines:
                left = l["qty"] - int(held.get(l["item"].strip().lower(), 0))
                if left > 0:
                    trimmed.append({**l, "qty": left})
            cart_lines = trimmed

        cart_state = ONGOING if cart_lines else EMPTY
        return {
            "cart": {
                "state": cart_state,
                "items": [f"{l['qty']} x {l['item']}" for l in cart_lines][:20],
                "total": _total(cart_lines),
                "what_you_may_say": _SAY[cart_state],
            },
            "orders": orders,
            "rules": (
                "THIS DESCRIBES THE MOMENT BEFORE THIS MESSAGE WAS HANDLED. It is a "
                "starting point, not the answer: whatever the guest asks for on this "
                "turn still gets added, and `cart` will hold it by the time you reply. "
                "Reading `cart.state: empty` as 'there is nothing to place' while the "
                "guest is in the middle of asking for a dish lost a guest their "
                "Tiramisu - they said yes to it and were told the cart was empty.\n"
                "This is the complete and authoritative picture of the guest's food. "
                "Do NOT work any of it out from the conversation - the states below "
                "are already decided. `cart` is what is NOT yet with the kitchen and "
                "is the ONLY thing you may ever offer to place. Every entry in "
                "`orders` has already gone to the kitchen: never ask the guest to "
                "place, confirm or approve any of them again. Follow each item's "
                "`what_you_may_say` exactly. When there is more than one order, refer "
                "to them by `order_no` and give each its own total - never add them "
                "together, or the guest is charged again for food already served.\n"
                "PLACED IS NOT CLOSED. An order in state `placed` is still open to "
                "additions - `can_be_changed` says so. A guest asking for another "
                "dish while an order is `placed` gets it added to THAT order. Only "
                "`processing` and `delivered` are beyond changing, and only those "
                "two may ever be given as a reason not to add something."
            ),
        }
    except Exception as exc:
        logger.warning("[IRD_STATE] lifecycle build failed (non-fatal): %r", exc)
        return {}
