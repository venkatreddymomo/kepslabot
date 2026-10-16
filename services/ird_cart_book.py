"""The guest's order book: ten slots, and the rules for reading them.

WHY THIS EXISTS. The order used to live in two places at once - the sentence the
bot wrote for the guest, and a JSON field it was asked to re-type in full every
single turn. Nothing reconciled them, so they drifted: a guest was shown "You've
added 2 x Chicken Burger, total 3,375" on a turn where the field was absent
entirely, the cart kept its old contents, and ticket 11135 went to the kitchen as
Filter Coffee and Tandoori Murgh. The burgers existed only in a sentence.

The book is the one copy. Both the guest summary and the ticket description are
printed from the same slot, so they cannot disagree. The model's job shrinks to
writing dishes and a note into a slot and moving its status; it never has to
remember an order across turns, because the order is on the page in front of it.

WHAT THIS MODULE DOES NOT DO: touch the database. Everything here is a pure
function over plain dicts so the slot rules - which slot a new order takes, when
an order locks, what the model may say about it - can be tested without a
connection. services/ird_cart_store.py is the thin layer that loads and saves.

Ten slots rather than a list because the row is cleared after 24h, so slots
recycle; no guest needs an eleventh order in one day. When all ten are in use the
oldest `delivered` one is reused.
"""
from __future__ import annotations

import datetime
import re
from typing import Any

SLOT_COUNT = 10

# The five states an order can be in. `ongoing` is the ONLY one that may be
# offered for placing, and `processing` may not be touched at any cost.
ONGOING = "ongoing"
PLACED = "placed"
PROCESSING = "processing"
DELIVERED = "delivered"
CANCELLED = "cancelled"

OPEN_STATES = (ONGOING, PLACED)
CLOSED_STATES = (PROCESSING, DELIVERED, CANCELLED)

# An external ticket in any of these has been picked up by the kitchen. Whatever
# the clock says, the food is being cooked and the order is no longer ours to
# change.
TICKET_STARTED = ("in progress", "in-progress", "inprogress", "processing",
                  "closed", "completed", "resolved", "cancelled", "canceled")


def now_local() -> datetime.datetime:
    """The guest's wall clock, naive. THE ONLY clock the book may use.

    `datetime.now()` is the process clock, and the server runs on UTC while the
    hotel is Asia/Kolkata. Orders were stamped placed_at on the UTC clock and
    then compared against the hotel clock, so every order read as five and a half
    hours old the instant it was placed - the five-minute edit window never once
    opened, and a guest adding a dish twenty seconds later was told the kitchen
    had already started.
    """
    try:
        from zoneinfo import ZoneInfo

        from config.settings import settings

        tz = str(getattr(settings, "hotel_timezone", "") or "Asia/Kolkata").strip()
        return datetime.datetime.now(ZoneInfo(tz or "Asia/Kolkata")).replace(tzinfo=None)
    except Exception:
        return datetime.datetime.now()


def _naive(dt: Any) -> Any:
    """Drop the timezone so two datetimes can actually be subtracted.

    MySQL DATETIME has no offset, so everything in the book comes back naive
    local. The caller's clock does not: _hotel_now() is timezone-aware, and
    `aware - naive` raises TypeError, which the surrounding try/except would have
    swallowed as "the book is unreadable" - silently dropping every order back to
    the old path. Same wall clock either way; only the label differs.
    """
    if isinstance(dt, datetime.datetime) and dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


def _int(v: Any, default: int = 0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _clean_items(raw: Any) -> list[dict[str, Any]]:
    """Item lines, keeping only what has a real name.

    Tolerant on purpose: the model writes this, and a line with a blank name is a
    line that would reach the kitchen as an empty row on the ticket.
    """
    out: list[dict[str, Any]] = []
    for it in (raw or []):
        if not isinstance(it, dict):
            continue
        name = str(it.get("item") or it.get("name") or "").strip()
        if not name:
            continue
        line: dict[str, Any] = {"item": name, "qty": max(1, _int(it.get("qty"), 1))}
        if it.get("price") is not None:
            line["price"] = _int(it.get("price"))
        allergens = str(it.get("allergens") or "").strip()
        if allergens:
            line["allergens"] = allergens
        # WHICH ONE THEY ASKED FOR. Menu entries like "Phulka (Plain or Ghee)" or
        # "Naan (Plain, Butter, Garlic)" are one item with options, so the model
        # writes the canonical name and the choice was simply lost: the reply said
        # "2 x Phulka (Ghee)" and the kitchen ticket said "Phulka". The guest gets
        # the wrong bread. There was nowhere to record it - now there is.
        variant = str(it.get("variant") or it.get("choice") or "").strip()
        if variant:
            line["variant"] = variant
        out.append(line)
    return out


def slot_from_row(row: dict[str, Any], n: int) -> dict[str, Any]:
    """One slot, read out of the wide row."""
    cart = row.get("cart_%d" % n) or {}
    if not isinstance(cart, dict):
        cart = {}
    return {
        "n": n,
        "items": _clean_items(cart.get("items")),
        "note": str(cart.get("note") or "").strip(),
        "status": str(row.get("status_%d" % n) or "").strip().lower(),
        "placed_at": row.get("placed_at_%d" % n),
        "ticket_ref": str(row.get("ticket_ref_%d" % n) or "").strip(),
    }


def slots_from_row(row: dict[str, Any]) -> list[dict[str, Any]]:
    """All ten slots. Empty ones are included so slot numbers stay stable."""
    return [slot_from_row(row, n) for n in range(1, SLOT_COUNT + 1)]


def is_empty(slot: dict[str, Any]) -> bool:
    return not slot.get("items") and not slot.get("status")


def row_updates_for_slot(slot: dict[str, Any]) -> dict[str, Any]:
    """The four columns for one slot, ready to write back."""
    n = slot["n"]
    cart: dict[str, Any] | None = None
    if slot.get("items") or slot.get("note"):
        cart = {"items": slot.get("items") or []}
        if slot.get("note"):
            cart["note"] = slot["note"]
    return {
        "cart_%d" % n: cart,
        "status_%d" % n: (slot.get("status") or None) or None,
        "placed_at_%d" % n: slot.get("placed_at"),
        "ticket_ref_%d" % n: (slot.get("ticket_ref") or None) or None,
    }


def pick_slot(slots: list[dict[str, Any]]) -> int:
    """Which slot does a NEW order take?

    First an empty one. If all ten are in use, the oldest `delivered` - that food
    has been eaten and its ticket is on record externally. Never reuse a slot
    that is still ongoing, placed or processing: those are live orders and
    overwriting one loses food someone is waiting for. Returns 0 when there is
    genuinely nowhere to put it, so the caller can say so rather than destroy
    something.
    """
    for s in slots:
        if is_empty(s):
            return s["n"]
    for state in (DELIVERED, CANCELLED):
        done = [s for s in slots if s.get("status") == state]
        if done:
            return min(done, key=lambda s: s.get("placed_at") or datetime.datetime.min)["n"]
    return 0


def ticket_has_started(ticket_status: str | None) -> bool:
    return str(ticket_status or "").strip().lower() in TICKET_STARTED


def editability(
    slot: dict[str, Any],
    *,
    now: datetime.datetime,
    window_minutes: int,
    ticket_status: str | None = None,
) -> dict[str, Any]:
    """May this order still be changed, and what should the bot be told?

    Code answers this, never the model. It is not asked whether 14:32 minus 14:28
    is more than five minutes - the same reason serving times moved into code:
    it is arithmetic against a clock, and it got it wrong.
    """
    status = slot.get("status") or ""
    if not slot.get("items"):
        return {"editable": False, "why": "there is nothing in this slot"}
    if status == ONGOING:
        return {"editable": True,
                "why": "not sent to the kitchen yet - add, remove or change freely"}
    if status in CLOSED_STATES:
        why = {
            PROCESSING: "the kitchen has started cooking it",
            DELIVERED: "it has already been delivered",
            CANCELLED: "it was cancelled",
        }[status]
        return {"editable": False, "why": why}
    if status == PLACED:
        if ticket_has_started(ticket_status):
            return {"editable": False,
                    "why": "the kitchen has picked it up and started cooking it"}
        placed_at = _naive(slot.get("placed_at"))
        now = _naive(now)
        if isinstance(placed_at, datetime.datetime) and window_minutes > 0:
            elapsed = (now - placed_at).total_seconds()
            if elapsed >= window_minutes * 60:
                return {"editable": False,
                        "why": "more than %d minutes have passed since it went to "
                               "the kitchen" % window_minutes}
            left = int((window_minutes * 60 - elapsed) // 60) + 1
            return {"editable": True,
                    "why": "it is with the kitchen but they have not started it - "
                           "about %d minute(s) left to change it" % left}
        return {"editable": True,
                "why": "it is with the kitchen but they have not started it"}
    return {"editable": False, "why": "unknown state %r" % status}


def refresh(
    slots: list[dict[str, Any]],
    *,
    now: datetime.datetime,
    window_minutes: int,
    ticket_status_by_ref: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Move statuses on by themselves.

    `placed` becomes `processing` the moment the change window lapses OR the real
    ticket says the team picked it up, and `delivered` when the ticket closes.
    This is the part that updates itself: nothing waits for the bot to notice,
    because the bot only ever sees a snapshot and would have to be told anyway.
    """
    refs = {str(k).strip(): str(v or "").strip().lower()
            for k, v in (ticket_status_by_ref or {}).items() if str(k or "").strip()}
    for s in slots:
        if s.get("status") != PLACED:
            continue
        tstatus = refs.get(s.get("ticket_ref") or "")
        if tstatus in ("closed", "completed", "resolved"):
            s["status"] = DELIVERED
            continue
        # PREFIX, NOT AN EXACT LIST. The cancel path writes status "cancel"
        # (ticketing_service normalises on startswith("cancel")), while this
        # matched only "cancelled"/"canceled" - so a genuinely cancelled order
        # never moved off PLACED, and _say() then told the model "the kitchen
        # HAS this and you may say it is on its way". The guest was told their
        # cancelled tea was being prepared. The model was reading the book
        # correctly; the book was wrong.
        if tstatus.startswith("cancel"):
            s["status"] = CANCELLED
            continue
        if ticket_has_started(tstatus):
            s["status"] = PROCESSING
            continue
        placed_at = _naive(s.get("placed_at"))
        if (isinstance(placed_at, datetime.datetime) and window_minutes > 0
                and (_naive(now) - placed_at).total_seconds() >= window_minutes * 60):
            s["status"] = PROCESSING
    return slots


def render_dishes(slot: dict[str, Any]) -> str:
    """The dish lines. The SAME text feeds the guest summary and the ticket."""
    return ", ".join(
        "%d x %s%s" % (line["qty"], line["item"],
                       " (%s)" % line["variant"] if line.get("variant") else "")
        for line in slot.get("items") or []
    )


def total_of(slot: dict[str, Any]) -> int:
    return sum(_int(l.get("price")) * _int(l.get("qty"), 1)
               for l in slot.get("items") or [])


_TOTAL_IN_TEXT = re.compile(r"(?i)(total[^\n₹]{0,24}?)(₹\s*\d[\d,]*)")


def fix_totals(reply: str, slots: list[dict[str, Any]]) -> "str | None":
    """Rewrite any money total in the reply to the book's own figure.

    The model was handed every line total and the order total and told plainly
    not to add anything up, and it still announced ₹2,825 for an order of
    ₹2,325 - the tiramisu counted twice. Intermittently, which is worse than
    always: the next total in the same conversation was right. Arithmetic is not
    something to ask for and hope for, so the number is no longer the model's to
    write. It says "Total: ₹x" and the book decides what x is.

    Only runs when exactly ONE order is live, because with two open orders a
    total in the text may legitimately belong to either. Returns None when
    there is nothing to change, so a correct reply is never rewritten.
    """
    text = str(reply or "")
    if not text or "₹" not in text:
        return None
    # AN ORDER THAT IS OVER IS NOT AN ORDER. This counted any slot still holding
    # items, so a CANCELLED order left behind made it look like the guest had two
    # live orders and the correction switched itself off - silently, exactly when
    # a second order existed to be confused about. A 3-line cart was then
    # announced at Rs4,175 instead of Rs3,400 while an earlier 2-line one had
    # been right, which is what made it look like a "multi-line" problem.
    live = [s for s in (slots or [])
            if s.get("items") and s.get("status") not in (CANCELLED, DELIVERED)]
    if len(live) != 1:
        return None
    right = "₹%s" % format(total_of(live[0]), ",")

    changed = False

    def _swap(m: "re.Match[str]") -> str:
        nonlocal changed
        if m.group(2).replace(" ", "") == right:
            return m.group(0)
        changed = True
        return m.group(1) + right

    out = _TOTAL_IN_TEXT.sub(_swap, text)
    return out if changed else None


def ticket_description(slot: dict[str, Any], *, room: str = "") -> str:
    """What the kitchen reads. Dishes printed from the slot, note carried through.

    The dish list is never re-typed by the model - that is the one job it kept
    dropping. The note is its words, untouched, because "no onions, ring twice"
    is real language and there is nothing to check it against.
    """
    where = " for room %s" % room if str(room or "").strip() else ""
    text = "In-room dining order%s: %s" % (where, render_dishes(slot))
    if slot.get("note"):
        text += ". Guest note: %s" % slot["note"]
    return text


_HOW = (" TO ACTUALLY ADD A DISH HERE, emit order_items on this turn with "
        "this order's COMPLETE list including the new dish. Without that "
        "field nothing is added, whatever your reply says.")


def _say(entry: dict[str, Any]) -> str:
    """What is TRUE of this order, and what may therefore be said about it.

    Trimmed once to a single fact per order ("Not sent to the kitchen yet.") and
    that was too far: nothing told the model it must not CALL that order placed,
    and it told a guest "your separate order for 2 Arrabbiata Pasta has been
    placed and is on its way" while the slot was still `ongoing` with no ticket.
    The state and its consequence travel together now.
    """
    status = entry["status"]
    if status == ONGOING:
        return ("NOT PLACED. No ticket exists and the kitchen has NOT been told. "
                "You must NOT say this is placed, confirmed, ordered, on its way or "
                "being prepared - none of that is true yet. This is the order that "
                "still needs placing: show it and ask to place it.")
    if status == PLACED and entry["can_be_changed"]:
        return ("PLACED - ticket %s. The kitchen HAS this and you may say it is on "
                "its way. Do not ask to place or confirm it again. It has not been "
                "started, so dishes can still be added to or removed from it - that "
                "is updating it, not placing it."
                % (entry.get("ticket_ref") or "raised"))
    # ── A LOCKED ORDER IS A FACT, NOT AN ANSWER TO GIVE ──────────────────────
    # These two used to LEAD with "Say it is on its way" / "Say it is being
    # prepared". That reads as an instruction, and an instruction the model can
    # see wins any tie: offered two sandwiches and told "yes", it replied "your
    # order is being prepared and cannot be changed" - answering about an order
    # nobody had asked about, and dropping the rest of the guest's message. The
    # second half of the line was the right answer all along. So the consequence
    # leads now, and the line says plainly that it is not a reply to reach for.
    if status == PLACED:
        return ("PLACED - ticket %s, and the change window has closed. This order "
                "is settled: anything the guest asks for now is a NEW order - open "
                "one and add it there. This is background about an order already "
                "handled, NOT an answer to give: only mention it if the guest asks "
                "about THIS order. Never answer a request for something else by "
                "describing it, and never refuse a new request because of it. If "
                "they do ask, it is on its way and can no longer be changed."
                % (entry.get("ticket_ref") or "raised"))
    if status == PROCESSING:
        return ("The kitchen has STARTED cooking this. It is settled: anything the "
                "guest asks for now is a NEW order - open one and add it there. "
                "This is background about an order already handled, NOT an answer "
                "to give: only mention it if the guest asks about THIS order. Never "
                "answer a request for something else by describing it, and never "
                "refuse a new request because of it. If they do ask, it is being "
                "prepared and cannot be changed or cancelled.")
    if status == DELIVERED:
        return "Already DELIVERED. Mention it only if the guest asks."
    return "CANCELLED. It is not coming, and no food will be made for it."


def for_the_model(
    slots: list[dict[str, Any]],
    *,
    now: datetime.datetime,
    window_minutes: int,
    ticket_status_by_ref: dict[str, str] | None = None,
) -> dict[str, Any]:
    """What the bot is shown: every live order, already judged.

    It receives conclusions, not raw material - "you CAN still add to this one",
    not a timestamp to subtract. Empty slots are left out entirely; a list of ten
    mostly-blank rows invites the model to describe blanks to the guest.
    """
    out: list[dict[str, Any]] = []
    for s in slots:
        if is_empty(s):
            continue
        e = editability(
            s, now=now, window_minutes=window_minutes,
            ticket_status=(ticket_status_by_ref or {}).get(s.get("ticket_ref") or ""),
        )
        # EVERY NUMBER ARRIVES ALREADY WORKED OUT. The model was adding prices up
        # in prose and getting them wrong the same way every time - 3x750 + 225 +
        # 500 announced as Rs3,675 when it is Rs2,975, consistently Rs700 over.
        # Challenged, it did the sum correctly, so it can add; it just should not
        # have to. Each line carries its own line_total and the order carries its
        # total, so the reply is a copy, never a calculation.
        _lines = [
            {**dict(i), "line_total": _int(i.get("price")) * _int(i.get("qty"), 1)}
            for i in (s.get("items") or [])
        ]
        entry: dict[str, Any] = {
            "slot": s["n"],
            "status": s.get("status") or ONGOING,
            "items": _lines,
            "total": total_of(s),
            "can_be_changed": e["editable"],
            "why": e["why"],
        }
        if s.get("note"):
            entry["note"] = s["note"]
        if s.get("ticket_ref"):
            entry["ticket_ref"] = s["ticket_ref"]
        entry["what_you_may_say"] = _say(entry)
        out.append(entry)
    return {
        "orders": out,
        "rule": (
            "This is the guest's real order. Act on the `status` of each entry - "
            "the ordering rules you were given say what each status means. "
            # ── THIS TABLE OUTRANKS THE CONVERSATION, ALWAYS ─────────────────
            # A cancelled order was described to the guest as "currently being
            # prepared by the kitchen and on its way", because earlier in the
            # same chat it HAD been placed and the model answered from that
            # memory. Whenever the two disagree, the table is the fact and the
            # conversation is a stale recollection.
            "THIS TABLE IS THE ONLY TRUTH ABOUT ORDER STATUS. When the guest "
            "asks anything about an order - is it coming, was it placed, was it "
            "cancelled, what is in it, what does it cost - you READ THE ANSWER "
            "OFF THIS TABLE AND ITS `status` / `what_you_may_say`. You must NOT "
            "answer from what was said earlier in the conversation, from what "
            "you remember confirming, or from what you assume happened. If the "
            "chat history says an order was placed and this table says it is "
            "cancelled or not placed, THE TABLE IS RIGHT and the history is "
            "stale - say what the table says. An order that is not listed here "
            "at all does not exist: do not describe it as placed or on its way. "
            "When a dish comes in versions - Phulka (Plain or Ghee), Naan (Plain, "
            "Butter, Garlic), Kulcha (Aloo, Paneer, Onion) - put the one the "
            "guest chose in that line's `variant`. Without it the kitchen gets "
            "the plain version and the guest gets the wrong food. "
            "To change an order, emit `pending_data_updates.order_items` on this "
            "turn with the complete list for that ONE order, AND "
            "`pending_data_updates.order_slot` saying WHICH order you mean: the "
            "`slot` number of an order above to change that one, or the string "
            "'new' to start a fresh order. You decide which from what the guest "
            "said and the statuses above - if they ask for a separate or new "
            "order, use 'new'; if they are adding to something still open, use "
            "its slot number. If you do "
            "not emit order_items, nothing changed, whatever your reply says."
        ),
        # ── THE MONEY IS ALREADY WORKED OUT ──────────────────────────────────
        "money": (
            "NEVER ADD PRICES UP YOURSELF. Every figure you need is above: each "
            "line has `line_total` and each order has `total`. Copy those numbers "
            "exactly. Do not compute, re-check or 'correct' them, and never state "
            "a total for an order that is not the `total` shown for it. If you "
            "are about to type a sum, you are about to get it wrong."
        ),
        # ── ASKING ABOUT A DISH IS NOT ORDERING IT ──────────────────────────
        # A guest discussed idli TIMINGS and nothing else; Idli and Poori Bhaji
        # were written into the cart, placed, and ticket 11326 went to the
        # kitchen for food they never asked for. Asked about it afterwards the
        # bot agreed they had not ordered it - while the kitchen was cooking it.
        "only_add_what_they_ASKED_for": (
            "PUT A DISH IN THE ORDER ONLY WHEN THE GUEST ASKS FOR IT. Wanting it "
            "ordered is the test, and nothing else is.\n"
            "NOT an order: asking what a dish is, what it costs, whether you have "
            "it, when it is served, what is available now, or anything you "
            "suggested and they have not accepted. Listing dishes, recommending "
            "dishes and answering questions about dishes all leave the order "
            "exactly as it was.\n"
            "AN ORDER: 'I'll have the...', 'send up a...', 'add a...', 'get me...', "
            "or a clear yes to a dish you offered.\n"
            "If you are not sure whether they were asking or ordering, ASK - do "
            "not add it. Food on a ticket is food someone cooks and the guest is "
            "charged for."
        ),
        # ── CHANGING AN ORDER IS A CART EDIT, NOT A MESSAGE TO STAFF ─────────
        "how_to_change_an_order": (
            "Adding, removing or changing a dish is something YOU do to the order "
            "above. It is never a request to pass to staff. Answering 'I've noted "
            "that, the team will take care of it' changes nothing - the dish stays "
            "on the order and the guest is charged for it.\n"
            "TO REMOVE A DISH, NAME IT IN `pending_data_updates.order_remove` - a "
            "list of the dish names to take off that order, e.g. "
            "`\"order_remove\": [\"Medu Vada\"]`, together with `order_slot`. "
            "LEAVING A DISH OUT OF `order_items` DOES NOT REMOVE IT: absence is "
            "ignored on purpose, because a list that arrives short would otherwise "
            "wipe food the kitchen is already cooking. `order_remove` is the ONLY "
            "way a dish comes off an order - if the guest asks you to drop "
            "something and you do not emit it, the dish stays and they are "
            "charged for it.\n"
            "IF THE DISH IS NOT ON ANY ORDER ABOVE, THERE IS NOTHING TO CHANGE. "
            "Say so plainly - 'you don't have a Medu Vada on an order at the "
            "moment' - and stop. Do NOT answer 'the team will take care of it': "
            "there is no team, nothing was ordered, and that sentence is read "
            "elsewhere as a promise that staff were told, which has raised real "
            "kitchen tickets for food the guest never ordered.\n"
            "`order_items` is always the COMPLETE list for that one order after "
            "your change - every dish that should remain, with its quantity and "
            "variant. Anything you leave out is removed. Never send only the dish "
            "being added.\n"
            "NEVER OFFER TO PLACE AN ORDER YOU HAVE NOT WRITTEN DOWN. If you are "
            "showing the guest a summary and asking whether to place it, emit "
            "`order_items` for that order on the SAME turn. An order that exists "
            "only in your reply is not an order: the guest's 'yes' then arrives "
            "at an empty cart, nothing can be placed, and they are asked the very "
            "same question again. That loop has cost a guest their whole second "
            "order - it sat unplaced while they believed it was coming."
        ),
        # ── A BARE "YES" ANSWERS THE QUESTION YOU JUST ASKED ────────────────
        # The guest was offered two sandwiches and said "yes ... and some
        # towels". The reply was "your order is being prepared and cannot be
        # changed" - about an order they had not mentioned - and the towels were
        # never answered at all. The offer was one line above in the same
        # conversation. A short answer belongs to the most recent question,
        # always; an older order is not a candidate unless they name it.
        "what_a_bare_yes_means": (
            "A SHORT ANSWER - 'yes', 'sure', 'go ahead', 'ok', 'that one' - "
            "belongs to the LAST THING YOU ASKED OR OFFERED. Read your own "
            "previous message and answer THAT. If you offered dishes, 'yes' "
            "accepts those dishes: add them. If you asked whether to place the "
            "order, 'yes' places it.\n"
            "An order already with the kitchen is NEVER what a bare 'yes' means, "
            "unless the guest names it. Accepting a dish you offered is a NEW "
            "order (or an addition to one still open) - it is never a request to "
            "change a settled one, so a locked order can never be the reason to "
            "refuse it.\n"
            "AND ANSWER THE WHOLE MESSAGE. If they said yes AND asked for "
            "something else in the same breath - 'yes, and can i get some "
            "towels' - both halves are real. Handle the food yourself and pass "
            "the rest to the team that owns it. Answering only one half and "
            "leaving the other unmentioned makes the guest ask twice."
        ),
        # ── WHEN IT IS NOT CLEAR, ASK ───────────────────────────────────────
        "when_unsure": (
            "If you cannot tell WHICH order the guest means, WHICH dish they mean, "
            "or whether they want something added, removed or replaced - ASK THEM, "
            "in one short question, and change nothing that turn. A guess that "
            "goes the wrong way costs the guest real food and real money; a "
            "question costs one turn. Only act when you are sure."
        ),
        # ── SEVERAL ORDERS CAN BE OPEN AT ONCE, AND THEY DO NOT MIX ──────────
        # A guest ordered for herself, then asked for a second order for her
        # vegetarian husband. Both ended up in ONE order: her Nilgiri Tonic and
        # Elaneer Payasam sitting in "your husband's order" beside his Paneer
        # Handi and Kulcha, read back to her that way, on one bill. The model was
        # restating every dish in the session on every turn, so each write
        # dragged the other person's food along with it.
        "orders_are_separate": (
            "THE GUEST MAY HAVE SEVERAL ORDERS AT ONCE and they are completely "
            "separate - different people, different food, different bills, placed "
            "and delivered on their own. `orders` above lists each one under its "
            "own `slot`.\n"
            "- `order_items` describes ONE order only: the one named by "
            "`order_slot`. List exactly the dishes belonging to THAT order and "
            "nothing else.\n"
            "- NEVER copy a dish from one order into another. A dish already "
            "sitting in another slot above is not part of the order you are "
            "writing now, even if the guest asked for it earlier in this chat.\n"
            "- A second person's food ('for my husband', 'for my daughter', 'a "
            "separate order') is ALWAYS order_slot 'new' - never added to an "
            "order that is already open.\n"
            "- When you tell the guest what is in an order, read back ONLY that "
            "order's own slot from `orders` above. Do not total two orders "
            "together, and do not describe one order's dishes as another's."
        ),
    }


def apply_write(
    slots: list[dict[str, Any]],
    write: dict[str, Any],
    *,
    now: datetime.datetime,
    window_minutes: int,
    ticket_status_by_ref: dict[str, str] | None = None,
    prefer_new: bool = False,
) -> tuple[list[dict[str, Any]], list[int]]:
    """Put what the model wrote into a slot. Returns the slots and which changed.

    `prefer_new` is the guest asking for a SEPARATE order. Without it a dish joins
    whatever is still open, which is what they mean the rest of the time.

    ACCEPTS THE OLD SHAPE TOO. The model currently emits `order_items` - a flat
    list with no slot number - and will keep doing so until its prompt is
    changed. A list with no slot lands in the one `ongoing` order if there is
    one, and opens a new slot if there is not, which is what the guest meant
    either way. That means the book can be proven end-to-end before the prompt
    moves, instead of both changing at once and neither being trustworthy.

    A LOCKED SLOT IS NEVER OVERWRITTEN. If the model aims a write at an order the
    kitchen has started, the write is refused and reported - it does not silently
    land somewhere else, because food already being cooked cannot be changed and
    quietly redirecting it would put the guest's dish on the wrong order.
    """
    if not isinstance(write, dict):
        return slots, []
    items = _clean_items(write.get("items"))
    note = str(write.get("note") or "").strip()
    _status_in = str(write.get("status") or "").strip().lower()
    # Dishes the model says the guest asked to drop, by name. Only these are ever
    # taken off an order the kitchen already has - see the merge below.
    _removals = {
        str(x).strip().lower()
        for x in (write.get("remove") if isinstance(write.get("remove"), list) else [])
        if str(x or "").strip()
    }
    # AN EMPTY DISH LIST NEVER WIPES AN ORDER. The model omitting or blanking
    # `items` is the exact failure this whole change exists to remove - it did it
    # on 3 of 6 turns that claimed food had been added - so silence here means
    # "nothing to say about the dishes", never "the guest has no order". Clearing
    # an order is done by cancelling it, which is a status, not an empty list.
    if (not items and not note and not _removals
            and _status_in not in (CANCELLED, DELIVERED, PROCESSING, PLACED)):
        return slots, []

    by_n = {s["n"]: s for s in slots}
    want = _int(write.get("slot"), 0)
    _explicit_slot = want in by_n          # the model named the order itself
    if want not in by_n:
        ongoing = [s for s in slots if s.get("status") == ONGOING and s.get("items")]
        # ── "A SEPARATE ORDER" OUTRANKS "THERE IS A CART OPEN" ────────────────
        # This used to be tested AFTER the ongoing-cart branch, so prefer_new only
        # ever applied when no cart was open - which is the one case that does not
        # need it. Asked for "another order for my husband, he's vegetarian" while
        # the guest's own cart was still open, the second order was merged into the
        # first: one slot holding her Nilgiri Tonic and Elaneer Payasam alongside
        # his Paneer Handi, Kulcha and Latte, and the bot then read that single
        # mixed cart back to her as "your husband's order". Two people's food, one
        # row, one bill, and no way to place either separately.
        if prefer_new:
            want = pick_slot(slots)
        elif ongoing:
            want = ongoing[0]["n"]
        else:
            # NO CART OPEN, BUT AN ORDER THE KITCHEN HAS NOT STARTED. A further
            # dish joins THAT order - it is the whole point of the change window,
            # and what the book tells the model it may do. Opening a new slot
            # instead duplicated the food already on the first order: a guest who
            # added a coffee got a second order carrying the coffee AND the two
            # burgers that were already being made.
            joinable = [
                s for s in slots
                if s.get("status") == PLACED and s.get("items")
                and editability(
                    s, now=now, window_minutes=window_minutes,
                    ticket_status=(ticket_status_by_ref or {}).get(s.get("ticket_ref") or ""),
                )["editable"]
            ]
            want = (max(joinable, key=lambda s: s.get("placed_at")
                        or datetime.datetime.min)["n"]
                    if joinable else pick_slot(slots))
    if want not in by_n:
        return slots, []           # every slot is a live order - caller must say so

    target = by_n[want]
    # Cancelling or marking an order delivered is a STATUS move, not an edit, so
    # it is allowed on an order the kitchen already has - otherwise a cancelled
    # ticket could never be reflected back into the book and the guest would keep
    # being told food was coming.
    _status_only = not items and not note and not _removals and _status_in
    # An order that does not exist cannot be cancelled or delivered. Without this
    # a stray status write would stamp a state onto an empty slot, and the book
    # would start reporting an order the guest never placed.
    if _status_only and not target.get("items"):
        return slots, []
    lock = editability(
        target, now=now, window_minutes=window_minutes,
        ticket_status=(ticket_status_by_ref or {}).get(target.get("ticket_ref") or ""),
    )
    if target.get("items") and not lock["editable"] and not _status_only:
        return slots, []

    # ── A COMPLETELY DIFFERENT SET OF DISHES IS A NEW ORDER, NOT AN EDIT ──
    # Writing to a slot replaces its contents, which is right for "make it 1
    # burger" and catastrophic for a new order: the guest had 2 Chicken Burgers
    # waiting, asked for a separate order of 2 Arrabbiata Pasta, and the pasta
    # overwrote the burgers - they were never placed and never mentioned again.
    # An edit always keeps at least one dish from the order it is editing; a list
    # that shares nothing with it is a different order.
    if target.get("items") and items:
        _old = {str(i.get("item", "")).strip().lower() for i in target["items"]}
        _new = {str(i.get("item", "")).strip().lower() for i in items}
        _disjoint = bool(_old and _new and not (_old & _new))
        if target.get("status") in (PLACED, PROCESSING):
            # ── AN ORDER WITH THE KITCHEN IS ONLY EVER ADDED TO HERE ─────────
            # Replacing its contents cost a guest their food: 1 Tandoori Murgh
            # and 2 Butter Naan were on ticket 11173, the guest asked to add a
            # filter coffee, the model sent ONLY the coffee as "the complete
            # list", and the murgh and naan were wiped from the book AND from the
            # ticket. Removing a line from a placed order goes through the ticket
            # edit path, which knows what it is doing; a write here can only add.
            _have = {str(i.get("item", "")).strip().lower(): dict(i)
                     for i in target["items"]}
            for line in items:
                _have[str(line.get("item", "")).strip().lower()] = dict(line)
            items = list(_have.values())
        elif _disjoint and not _explicit_slot:
            # A wholly different set of dishes in a cart that is still being
            # built is a NEW order, not a replacement of the waiting one.
            _fresh = pick_slot(slots)
            if _fresh and _fresh in by_n:
                target = by_n[_fresh]
                want = _fresh
    # ── A DISH THE GUEST ASKED TO DROP MUST ACTUALLY GO ──────────────────────
    # Everything above protects an order from DISAPPEARING: absence never
    # removes anything, because a model that sends only the new dish as "the
    # complete list" once wiped a Tandoori Murgh and 2 Butter Naan off ticket
    # 11173. That protection also made removal impossible - the model did
    # exactly the right thing, resent the list without the Medu Vada, and the
    # merge put the Medu Vada straight back. The guest was told "the team will
    # take care of it" while the dish stayed on their order and their bill. It
    # read as the model ignoring them; it was this function.
    #
    # So absence still means nothing, and removal is something the model SAYS.
    # Only the lines it names go, and they go whether the order is a cart or
    # already with the kitchen.
    if _removals:
        _base = items or list(target.get("items") or [])
        items = [i for i in _base
                 if str(i.get("item", "")).strip().lower() not in _removals]
        target["items"] = items
    else:
        target["items"] = items or target.get("items") or []
    if note:
        target["note"] = note
    status = _status_in
    # A STATUS NEVER GOES BACKWARDS. Adding a dish to an order already with the
    # kitchen arrives with status `ongoing` (nothing is being placed on that
    # turn), and taking it literally demoted a placed order back to a cart - so
    # the bot would offer to place food that was already on its way, and the
    # ticket ref would sit on a slot claiming to be unplaced.
    _RANK = {ONGOING: 0, PLACED: 1, PROCESSING: 2, DELIVERED: 3}
    if (status in _RANK and target.get("status") in _RANK
            and _RANK[status] < _RANK[str(target.get("status"))]):
        status = ""
    if status in (ONGOING, PLACED, PROCESSING, DELIVERED, CANCELLED):
        # Stamp the clock at the moment it goes to the kitchen, and only then -
        # a placed_at rewritten on a later edit would silently restart the
        # five-minute window and keep an order editable forever.
        if status == PLACED and target.get("status") != PLACED:
            target["placed_at"] = _naive(now)
        target["status"] = status
    elif not target.get("status"):
        target["status"] = ONGOING
    return slots, [want]


def project_to_pending_data(
    slots: list[dict[str, Any]], pending_data: dict[str, Any]
) -> dict[str, Any]:
    """Write the book back out into the old pending_data keys.

    THIS IS WHAT MAKES THE ROLLOUT SAFE. Around a hundred places still read
    `_ird_cart`, `_ird_placed_orders`, `_ird_order_ticket_ref`,
    `_ird_open_order_items` and `_ird_order_placed_at`. Repointing all of them in
    one step and hoping is not a plan, so instead they are REGENERATED from the
    book on every turn. They stop being copies that are independently maintained
    and drift, and become a view of the one record - which is the entire point of
    the book. Readers migrate off them afterwards, at leisure, with tests green
    the whole way.

    Only the keys the book genuinely owns are touched. Anything else in
    pending_data is left exactly as it was.
    """
    if not isinstance(pending_data, dict):
        return {}

    live = [s for s in slots if s.get("items")]
    ongoing = [s for s in live if s.get("status") == ONGOING]
    with_kitchen = [s for s in live if s.get("status") in (PLACED, PROCESSING, DELIVERED)]

    # The cart is ONLY what has not gone to the kitchen. A line already on a
    # placed order is not awaiting placement, and describing it that way is what
    # had the bot offer to place food it had just sent.
    pending_data["_ird_cart"] = list(ongoing[0]["items"]) if ongoing else []

    pending_data["_ird_placed_orders"] = [
        {
            "n": s["n"],
            "ticket_ref": s.get("ticket_ref") or "",
            "items": list(s.get("items") or []),
            "total": total_of(s),
            "placed_at": (s["placed_at"].isoformat()
                          if isinstance(s.get("placed_at"), datetime.datetime) else ""),
        }
        for s in with_kitchen
    ]

    # The one order a further dish may still join: with the kitchen, not started.
    still_open = [s for s in live if s.get("status") == PLACED]
    newest = max(still_open, key=lambda s: s.get("placed_at") or datetime.datetime.min,
                 default=None) if still_open else None
    pending_data["_ird_open_order_items"] = list(newest["items"]) if newest else []
    pending_data["_ird_order_ticket_ref"] = (newest or {}).get("ticket_ref") or ""
    _at = (newest or {}).get("placed_at")
    pending_data["_ird_order_placed_at"] = (
        _at.isoformat() if isinstance(_at, datetime.datetime) else "")

    # The guest's own words about the order they are building - "no onions",
    # "ring twice, baby sleeping". This is the half of the ticket the model
    # SHOULD write, because it is real language with nothing to check it
    # against; the dish list is the half it must not, because that is the part
    # it kept dropping.
    _noted = (ongoing[0] if ongoing else (newest if newest else None))
    pending_data["_ird_order_note"] = (_noted or {}).get("note") or ""

    return pending_data


# A closing question that asks the guest to authorise something the book says is
# already done. Deliberately narrow: it must be a QUESTION, and it must be about
# placing/adding/confirming - the bot is free to ask anything else.
_ASK_TO_PLACE = re.compile(
    r"\b(?:shall i|should i|would you like me to|do you want me to"
    r"|may i|can i|shall we)\b[^?\n]*\b(?:place|confirm|send|proceed)\b[^?\n]*\?",
    re.I)
# "shall I add X?" is only stale when X is ALREADY in the order. Offering
# something new - "would you like me to add a dessert?" - is a normal offer
# and must survive untouched.
_ASK_TO_ADD = re.compile(
    r"\b(?:shall i|should i|would you like me to|do you want me to"
    r"|may i|can i|shall we)\b[^?\n]*\badd\b([^?\n]*)\?",
    re.I)


def fix_stale_closing(reply: str, slots: list[dict[str, Any]]) -> "str | None":
    """Drop a closing question the book has already answered.

    The model kept ending on "shall I place this updated order?" for an order the
    book says is `placed`, and "shall I add the Filter Coffee?" for a dish it had
    just written into that order. Both ask the guest to authorise something that
    has already happened, which is how a guest ends up saying yes three times and
    doubting whether the food is coming.

    Only fires when NOTHING is `ongoing` - if a cart really is waiting, the
    placement question is correct and is left alone. Returns the corrected reply,
    or None when there is nothing to correct.
    """
    text = str(reply or "")
    if not text.strip():
        return None
    live = [s for s in slots if s.get("items")]
    if any(s.get("status") == ONGOING for s in live):
        return None                      # something IS waiting - the ask is right
    with_kitchen = [s for s in live if s.get("status") in (PLACED, PROCESSING)]
    if not with_kitchen:
        return None
    m = _ASK_TO_PLACE.search(text)
    if not m:
        m = _ASK_TO_ADD.search(text)
        if not m:
            return None
        # Only stale if the dish it offers to add is already on the order.
        offered = (m.group(1) or "").lower()
        # The book stores the full menu name - "Filter Coffee (South Indian
        # Style)" - and the bot writes the short one. Compare without the
        # parenthetical, or the check never matches the thing it is guarding.
        held = []
        for s2 in with_kitchen:
            for i in (s2.get("items") or []):
                nm = str(i.get("item", "")).strip().lower()
                nm = re.sub(r"\s*\([^)]*\)", "", nm).strip()
                if nm:
                    held.append(nm)
        if not any(h in offered for h in held):
            return None
    kept = (text[:m.start()] + text[m.end():]).rstrip()
    tail = ("Your order is on its way to your room."
            if len(with_kitchen) == 1 else
            "Your orders are on their way to your room.")
    return (kept + ("\n\n" if kept else "") + tail).strip()
