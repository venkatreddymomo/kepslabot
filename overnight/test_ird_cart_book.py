"""The order book's rules, without a database.

Everything here is decided by code, not by the model, so it can be proved
outright: which slot a new order takes, when an order locks, what the bot is
allowed to say about it. These are the rules that used to be spread across
`_ird_cart`, `_ird_placed_orders`, `_ird_order_placed_at`, `_ird_fresh_order`,
`_ird_placed_items` and `_ird_closed_order_refs` and had to agree with each
other by luck.

    python overnight/test_ird_cart_book.py
"""
from __future__ import annotations

import datetime
import io
import json
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

from services import ird_cart_book as B

NOW = datetime.datetime(2026, 8, 28, 13, 0, 0)
WINDOW = 5

results: list[tuple[bool, str]] = []


def check(name: str, got, want) -> None:
    ok = got == want
    results.append((ok, name))
    print(("PASS  " if ok else "FAIL  ") + name)
    if not ok:
        print("        got  %r\n        want %r" % (got, want))


def row(**kw) -> dict:
    """A wide DB row with only the slots the test cares about filled in."""
    base: dict = {}
    for n in range(1, B.SLOT_COUNT + 1):
        base["cart_%d" % n] = None
        base["status_%d" % n] = None
        base["placed_at_%d" % n] = None
        base["ticket_ref_%d" % n] = None
    base.update(kw)
    return base


def cart(*names, note: str = "") -> dict:
    c: dict = {"items": [{"item": n, "qty": 1, "price": 100} for n in names]}
    if note:
        c["note"] = note
    return c


# ── reading a row ───────────────────────────────────────────────────────────
r = row(cart_1=cart("Chicken Burger", note="no onions"), status_1="ongoing")
slots = B.slots_from_row(r)
check("ten slots always come back", len(slots), 10)
check("slot 1 has the dish", [i["item"] for i in slots[0]["items"]], ["Chicken Burger"])
check("slot 1 keeps the note", slots[0]["note"], "no onions")
check("slot 2 reads as empty", B.is_empty(slots[1]), True)
check("slot 1 is not empty", B.is_empty(slots[0]), False)

# A blank name would reach the kitchen as an empty line on the ticket.
r2 = row(cart_1={"items": [{"item": "  ", "qty": 1}, {"item": "Naan", "qty": 2}]},
         status_1="ongoing")
check("blank item names are dropped",
      [i["item"] for i in B.slots_from_row(r2)[0]["items"]], ["Naan"])

# ── which slot does a new order take? ───────────────────────────────────────
check("first order takes slot 1", B.pick_slot(B.slots_from_row(row())), 1)
check("next order takes the first free slot",
      B.pick_slot(B.slots_from_row(row(cart_1=cart("A"), status_1="placed"))), 2)

full_live = row(**{k: v for n in range(1, 11) for k, v in
                   (("cart_%d" % n, cart("Dish%d" % n)), ("status_%d" % n, "placed"))})
check("no slot is stolen from a live order", B.pick_slot(B.slots_from_row(full_live)), 0)

full_mixed = dict(full_live)
full_mixed["status_4"] = "delivered"
full_mixed["placed_at_4"] = NOW - datetime.timedelta(hours=6)
full_mixed["status_9"] = "delivered"
full_mixed["placed_at_9"] = NOW - datetime.timedelta(hours=2)
check("the OLDEST delivered slot is the one reused",
      B.pick_slot(B.slots_from_row(full_mixed)), 4)

# ── may this order still be changed? ────────────────────────────────────────
def edit(status, placed_min_ago=None, ticket_status=None, items=("Burger",)):
    s = {"n": 1, "items": [{"item": i, "qty": 1, "price": 100} for i in items],
         "note": "", "status": status, "ticket_ref": "T1",
         "placed_at": (NOW - datetime.timedelta(minutes=placed_min_ago)
                       if placed_min_ago is not None else None)}
    return B.editability(s, now=NOW, window_minutes=WINDOW,
                         ticket_status=ticket_status)["editable"]


check("ongoing is editable", edit("ongoing"), True)
check("placed 1 min ago is editable", edit("placed", 1), True)
check("placed 4 min ago is still editable", edit("placed", 4), True)
check("placed exactly 5 min ago is LOCKED", edit("placed", 5), False)
check("placed 20 min ago is LOCKED", edit("placed", 20), False)
check("placed 1 min ago but kitchen started it is LOCKED",
      edit("placed", 1, ticket_status="In Progress"), False)
check("processing is LOCKED", edit("processing", 1), False)
check("delivered is LOCKED", edit("delivered", 30), False)
check("cancelled is LOCKED", edit("cancelled", 30), False)
check("an empty slot is not editable", edit("ongoing", items=()), False)

# The bot is told the answer in words, never given arithmetic to do.
why = B.editability(
    {"n": 1, "items": [{"item": "Burger", "qty": 1}], "note": "", "status": "placed",
     "placed_at": NOW - datetime.timedelta(minutes=2), "ticket_ref": "T1"},
    now=NOW, window_minutes=WINDOW)["why"]
check("it is told how long is left, not the timestamp",
      ("minute(s) left to change it" in why), True)

# ── statuses move on by themselves ──────────────────────────────────────────
def refreshed(status, placed_min_ago, ticket_status=None):
    s = [{"n": 1, "items": [{"item": "Burger", "qty": 1}], "note": "", "status": status,
          "placed_at": NOW - datetime.timedelta(minutes=placed_min_ago),
          "ticket_ref": "T1"}]
    B.refresh(s, now=NOW, window_minutes=WINDOW,
              ticket_status_by_ref={"T1": ticket_status} if ticket_status else None)
    return s[0]["status"]


check("placed -> processing once the window lapses", refreshed("placed", 6), "processing")
check("placed stays placed inside the window", refreshed("placed", 2), "placed")
check("placed -> processing when the team picks it up",
      refreshed("placed", 1, "In Progress"), "processing")
check("placed -> delivered when the ticket closes",
      refreshed("placed", 1, "Closed"), "delivered")
check("placed -> cancelled when the ticket is cancelled",
      refreshed("placed", 1, "Cancelled"), "cancelled")
check("an ongoing order is never advanced by the clock",
      refreshed("ongoing", 60), "ongoing")

# ── one slot, printed two ways - this is the whole point ────────────────────
slot = B.slots_from_row(row(
    cart_1={"items": [{"item": "Chicken Burger", "qty": 2, "price": 750},
                      {"item": "Filter Coffee", "qty": 1, "price": 325}],
            "note": "no onions, ring twice - baby sleeping"},
    status_1="ongoing"))[0]
check("dishes render once", B.render_dishes(slot), "2 x Chicken Burger, 1 x Filter Coffee")
check("total is computed, not typed", B.total_of(slot), 2 * 750 + 325)
check("the ticket carries the dishes AND the note",
      B.ticket_description(slot, room="1400"),
      "In-room dining order for room 1400: 2 x Chicken Burger, 1 x Filter Coffee. "
      "Guest note: no onions, ring twice - baby sleeping")

# ── what the model is shown ─────────────────────────────────────────────────
r3 = row(cart_1=cart("Burger"), status_1="placed", ticket_ref_1="11135",
         placed_at_1=NOW - datetime.timedelta(minutes=1),
         cart_2=cart("Tiramisu"), status_2="ongoing")
view = B.for_the_model(B.slots_from_row(r3), now=NOW, window_minutes=WINDOW)
check("only the used slots are shown", len(view["orders"]), 2)
# The book now carries FACTS about each order; the RULES live in the ordering
# directive. They used to be duplicated in both, in wording that had drifted
# apart - the model was reconciling two versions of the same instruction.
check("a placed order is stated as PLACED with its ticket",
      "PLACED - ticket 11135" in view["orders"][0]["what_you_may_say"], True)
check("and as still updatable inside the window",
      "still be added to or removed from it" in view["orders"][0]["what_you_may_say"], True)
check("an ongoing order is stated as NOT PLACED",
      "NOT PLACED" in view["orders"][1]["what_you_may_say"], True)
# The lie this exists to stop: it must be told outright not to call an
# unplaced order placed. Left implicit, it said "your separate order has been
# placed and is on its way" for a slot with no ticket.
check("and forbidden from calling it placed",
      "must NOT say this is placed" in view["orders"][1]["what_you_may_say"], True)
_locked_v = B.for_the_model(
    B.slots_from_row(row(cart_1=cart("Burger"), status_1="placed", ticket_ref_1="X",
                         placed_at_1=NOW - datetime.timedelta(minutes=30))),
    now=NOW, window_minutes=WINDOW)
check("a locked order says a further dish is a NEW order",
      "NEW order" in _locked_v["orders"][0]["what_you_may_say"], True)

locked = B.for_the_model(
    B.slots_from_row(row(cart_1=cart("Burger"), status_1="placed", ticket_ref_1="X",
                         placed_at_1=NOW - datetime.timedelta(minutes=30))),
    now=NOW, window_minutes=WINDOW)
check("a lapsed order is described as unchangeable",
      "no longer be changed" in locked["orders"][0]["what_you_may_say"], True)

check("the book names the field that records a change",
      "order_items" in view["rule"], True)
check("and says an unemitted change did not happen",
      "nothing changed" in view["rule"], True)

# The rules themselves must be in the directive, exactly once.
from services.llm_orchestration_service import llm_orchestration_service as _S
_d = _S._ird_order_flow_directive()
check("the directive covers every status",
      all(w in _d for w in ("ongoing", "placed", "processing", "delivered")), True)
check("it no longer contradicts itself about saying an order is on its way",
      "NEVER SAY AN ORDER IS PLACED" in _d, False)
check("it does not reference fields that no longer exist",
      ("order_lifecycle.cart" in _d) or ("not_yet_placed" in _d), False)
check("it says a repeat order is real",
      "not a duplicate" in _d, True)
check("and that a request for food is never refused",
      "never answer a request for food by refusing" in _d, True)

# ── round trip back to columns ──────────────────────────────────────────────
s1 = B.slots_from_row(row(cart_1=cart("Burger", note="hot"), status_1="ongoing"))[0]
up = B.row_updates_for_slot(s1)
check("write-back names the right columns", sorted(up),
      ["cart_1", "placed_at_1", "status_1", "ticket_ref_1"])
check("write-back keeps the note", up["cart_1"]["note"], "hot")
check("an emptied slot writes NULL, not an empty shell",
      B.row_updates_for_slot({"n": 3, "items": [], "note": "", "status": "",
                              "placed_at": None, "ticket_ref": ""})["cart_3"], None)

# ── the model writing into the book ─────────────────────────────────────────
def write_into(row_kw, write, minutes_ago=None, ticket_status=None,
               prefer_new=False):
    r = row(**row_kw)
    slots = B.slots_from_row(r)
    if minutes_ago is not None:
        for sl in slots:
            if sl.get("status") == "placed":
                sl["placed_at"] = NOW - datetime.timedelta(minutes=minutes_ago)
    return B.apply_write(
        slots, write, now=NOW, window_minutes=WINDOW, prefer_new=prefer_new,
        ticket_status_by_ref={"T1": ticket_status} if ticket_status else None)


# The old shape - a flat list with no slot number - still lands correctly, so
# the book can be proven before the prompt is changed.
slots, changed = write_into({}, {"items": [{"item": "Chicken Burger", "qty": 2}]})
check("a list with no slot opens slot 1", changed, [1])
check("and it holds the dish", [i["item"] for i in slots[0]["items"]], ["Chicken Burger"])
check("and it starts as ongoing", slots[0]["status"], "ongoing")

slots, changed = write_into(
    {"cart_1": cart("Burger"), "status_1": "ongoing"},
    {"items": [{"item": "Burger", "qty": 1}, {"item": "Tiramisu", "qty": 1}]})
check("more dishes land in the SAME ongoing order", changed, [1])
check("both dishes are there", len(slots[0]["items"]), 2)

# THE 11135 CASE. Order 1 is with the kitchen and past its window; a new dish
# must open a new order rather than being lost or joining a locked one.
slots, changed = write_into(
    {"cart_1": cart("Burger"), "status_1": "placed", "ticket_ref_1": "T1"},
    {"items": [{"item": "Tiramisu", "qty": 1}]}, minutes_ago=30)
check("a dish added after the window opens a NEW slot", changed, [2])
check("the locked order is untouched",
      [i["item"] for i in slots[0]["items"]], ["Burger"])
check("the new dish is really in slot 2",
      [i["item"] for i in slots[1]["items"]], ["Tiramisu"])

# Inside the window it joins the open order instead.
slots, changed = write_into(
    {"cart_1": cart("Burger"), "status_1": "placed", "ticket_ref_1": "T1"},
    {"slot": 1, "items": [{"item": "Burger", "qty": 1}, {"item": "Coffee", "qty": 1}]},
    minutes_ago=1)
check("inside the window a dish joins the placed order", changed, [1])
check("the placed order now has both", len(slots[0]["items"]), 2)

# A write aimed at an order the kitchen has started is REFUSED outright, never
# quietly redirected - the guest's dish must not land on the wrong order.
slots, changed = write_into(
    {"cart_1": cart("Burger"), "status_1": "processing"},
    {"slot": 1, "items": [{"item": "Coffee", "qty": 1}]})
check("a write into a cooking order is refused", changed, [])
check("and that order is unchanged",
      [i["item"] for i in slots[0]["items"]], ["Burger"])

slots, changed = write_into(
    {"cart_1": cart("Burger"), "status_1": "placed", "ticket_ref_1": "T1"},
    {"slot": 1, "items": [{"item": "Coffee", "qty": 1}]},
    minutes_ago=1, ticket_status="In Progress")
check("a write is refused once the team picks the ticket up", changed, [])

# Placing stamps the clock once, and a later edit must not restart the window.
slots, changed = write_into(
    {"cart_1": cart("Burger"), "status_1": "ongoing"},
    {"slot": 1, "items": [{"item": "Burger", "qty": 1}], "status": "placed"})
check("placing sets the status", slots[0]["status"], "placed")
check("placing stamps the clock", slots[0]["placed_at"], NOW)

r = row(cart_1=cart("Burger"), status_1="placed", ticket_ref_1="T1")
sl = B.slots_from_row(r)
stamped = NOW - datetime.timedelta(minutes=3)
sl[0]["placed_at"] = stamped
B.apply_write(sl, {"slot": 1, "items": [{"item": "Burger", "qty": 1},
                                        {"item": "Coffee", "qty": 1}],
                   "status": "placed"},
              now=NOW, window_minutes=WINDOW)
check("editing a placed order does NOT restart the window", sl[0]["placed_at"], stamped)

# The note is the model's words and must survive.
slots, changed = write_into({}, {"items": [{"item": "Burger", "qty": 1}],
                                 "note": "no onions, ring twice"})
check("the note is kept", slots[0]["note"], "no onions, ring twice")

check("an empty write changes nothing", write_into({}, {"items": []})[1], [])
check("a non-dict write changes nothing", write_into({}, "nonsense")[1], [])

# ── the book projected back into the old keys ───────────────────────────────
# The ~100 existing readers keep working because these are REGENERATED from the
# book each turn instead of being maintained separately. That is what stops them
# drifting - they are a view of one record, not a second copy of it.
r4 = row(cart_1=cart("Burger"), status_1="placed", ticket_ref_1="11135",
         placed_at_1=NOW - datetime.timedelta(minutes=2),
         cart_2=cart("Tiramisu"), status_2="ongoing",
         cart_3=cart("Soup"), status_3="delivered",
         placed_at_3=NOW - datetime.timedelta(hours=3))
pd: dict = {"room_number": "1400", "something_else": "left alone"}
B.project_to_pending_data(B.slots_from_row(r4), pd)

check("the cart is ONLY the unplaced order",
      [i["item"] for i in pd["_ird_cart"]], ["Tiramisu"])
check("placed orders include everything with the kitchen",
      [o["ticket_ref"] for o in pd["_ird_placed_orders"]], ["11135", ""])
check("a delivered order is still on the ledger", len(pd["_ird_placed_orders"]), 2)
check("the open order is the one still joinable",
      [i["item"] for i in pd["_ird_open_order_items"]], ["Burger"])
check("its ticket ref is carried", pd["_ird_order_ticket_ref"], "11135")
check("its placed_at is carried", bool(pd["_ird_order_placed_at"]), True)
check("unrelated pending_data is untouched", pd["something_else"], "left alone")

# Nothing ordered at all: every key must be emptied, not left stale. A stale
# cart here is what had the bot offer to place an order that no longer existed.
pd2: dict = {"_ird_cart": [{"item": "Ghost", "qty": 1}],
             "_ird_order_ticket_ref": "9999"}
B.project_to_pending_data(B.slots_from_row(row()), pd2)
check("an empty book empties the cart", pd2["_ird_cart"], [])
check("an empty book clears the ticket ref", pd2["_ird_order_ticket_ref"], "")
check("an empty book clears the placed ledger", pd2["_ird_placed_orders"], [])

# Once the kitchen starts the order it is no longer joinable, so nothing may
# still point at it as the open one.
r5 = row(cart_1=cart("Burger"), status_1="processing", ticket_ref_1="T9",
         placed_at_1=NOW - datetime.timedelta(minutes=30))
pd3: dict = {}
B.project_to_pending_data(B.slots_from_row(r5), pd3)
check("a cooking order is not offered as joinable", pd3["_ird_open_order_items"], [])
check("and no ticket ref is left pointing at it", pd3["_ird_order_ticket_ref"], "")
check("but it is still on the placed ledger", len(pd3["_ird_placed_orders"]), 1)

# ── an aware clock against a naive column ───────────────────────────────────
# MySQL DATETIME has no offset so placed_at comes back naive, while the caller's
# clock (_hotel_now) is timezone-aware. Subtracting one from the other raises
# TypeError, and the try/except around the book would have swallowed that as
# "unreadable" - silently dropping every order back to the old path, which is
# exactly the failure this whole change exists to remove.
from zoneinfo import ZoneInfo

AWARE = NOW.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
tz_slot = {"n": 1, "items": [{"item": "Burger", "qty": 1}], "note": "",
           "status": "placed", "ticket_ref": "T1",
           "placed_at": NOW - datetime.timedelta(minutes=2)}
check("an aware clock still reads a naive placed_at",
      B.editability(tz_slot, now=AWARE, window_minutes=WINDOW)["editable"], True)
check("and still locks once the window has passed",
      B.editability({**tz_slot, "placed_at": NOW - datetime.timedelta(minutes=9)},
                    now=AWARE, window_minutes=WINDOW)["editable"], False)
tz_list = [dict(tz_slot, placed_at=NOW - datetime.timedelta(minutes=9))]
B.refresh(tz_list, now=AWARE, window_minutes=WINDOW)
check("refresh advances the status with an aware clock too",
      tz_list[0]["status"], "processing")
_, tz_changed = B.apply_write(
    B.slots_from_row(row(cart_1=cart("Burger"), status_1="ongoing")),
    {"slot": 1, "items": [{"item": "Burger", "qty": 1}], "status": "placed"},
    now=AWARE, window_minutes=WINDOW)
check("placing with an aware clock works", tz_changed, [1])

# ── an empty dish list must never wipe an order ─────────────────────────────
# The model blanking `items` is the exact failure the book exists to remove - it
# did it on 3 of the 6 turns that claimed food had been added. Silence about the
# dishes means "nothing to say", never "the guest has no order".
slots, changed = write_into({"cart_1": cart("Burger"), "status_1": "ongoing"},
                            {"items": []})
check("an empty list does not wipe the order", changed, [])
check("and the dish is still there",
      [i["item"] for i in slots[0]["items"]], ["Burger"])

# Cancelling IS how an order is cleared, and it is a status move - so it must be
# allowed even on an order the kitchen already has, or a cancelled ticket could
# never be reflected back and the guest would keep being told food was coming.
slots, changed = write_into({"cart_1": cart("Burger"), "status_1": "placed",
                             "ticket_ref_1": "T1"},
                            {"slot": 1, "status": "cancelled"}, minutes_ago=30)
check("a cancellation lands on a locked order", changed, [1])
check("and the status really changed", slots[0]["status"], "cancelled")

slots, changed = write_into({"cart_1": cart("Burger"), "status_1": "placed",
                             "ticket_ref_1": "T1"},
                            {"slot": 1, "status": "delivered"}, minutes_ago=30)
check("delivered lands too", slots[0]["status"], "delivered")

# But a status-only write still must not invent an order out of nothing.
slots, changed = write_into({}, {"slot": 1, "status": "cancelled"})
check("cancelling an empty slot does nothing", changed, [])

# ── a dish joins the order the kitchen has not started ──────────────────────
# The first live run opened a NEW slot for a coffee added to an order still
# inside its window, so the second order carried the coffee AND the two burgers
# already being cooked. A dish joins the open order; that is what the window is.
slots, changed = write_into(
    {"cart_1": cart("Chicken Burger"), "status_1": "placed", "ticket_ref_1": "T1"},
    {"items": [{"item": "Chicken Burger", "qty": 2},
               {"item": "Filter Coffee", "qty": 1}]}, minutes_ago=1)
check("a dish joins the placed order still in its window", changed, [1])
check("and no second order is opened",
      len([s for s in slots if s["items"]]), 1)
check("the joined order holds both dishes",
      sorted(i["item"] for i in slots[0]["items"]), ["Chicken Burger", "Filter Coffee"])

# Once that order locks, the same dish must start a new one instead.
slots, changed = write_into(
    {"cart_1": cart("Chicken Burger"), "status_1": "placed", "ticket_ref_1": "T1"},
    {"items": [{"item": "Filter Coffee", "qty": 1}]}, minutes_ago=30)
check("but a locked order is not joined - a new slot opens", changed, [2])

# And an explicit request for a separate order is honoured even inside the window.
r_join = row(cart_1=cart("Chicken Burger"), status_1="placed", ticket_ref_1="T1")
sl = B.slots_from_row(r_join)
sl[0]["placed_at"] = NOW - datetime.timedelta(minutes=1)
sl, changed = B.apply_write(sl, {"items": [{"item": "Tiramisu", "qty": 1}]},
                            now=NOW, window_minutes=WINDOW, prefer_new=True)
check("asking for a SEPARATE order opens a new slot", changed, [2])

# ── a status never goes backwards ───────────────────────────────────────────
# Adding a dish to an order already with the kitchen arrives with status
# `ongoing`, because nothing is being PLACED on that turn. Taking that literally
# demoted a placed order back to a cart: the bot would then offer to place food
# already on its way, and a ticket ref would sit on a slot claiming to be
# unplaced. Seen live - slot 1 ended up `ongoing` holding ticket 11143.
slots, changed = write_into(
    {"cart_1": cart("Chicken Burger"), "status_1": "placed", "ticket_ref_1": "T1"},
    {"slot": 1, "items": [{"item": "Chicken Burger", "qty": 2},
                          {"item": "Tiramisu", "qty": 1}], "status": "ongoing"},
    minutes_ago=1)
check("adding a dish does NOT demote a placed order", slots[0]["status"], "placed")
check("and the dish is still added", len(slots[0]["items"]), 2)
check("the ticket ref stays put", slots[0]["ticket_ref"], "T1")

# Forwards is always allowed.
slots, changed = write_into({"cart_1": cart("Burger"), "status_1": "ongoing"},
                            {"slot": 1, "items": [{"item": "Burger", "qty": 1}],
                             "status": "placed"})
check("ongoing -> placed still works", slots[0]["status"], "placed")

# Cancelling outranks the ordering - it must land from any state.
slots, changed = write_into({"cart_1": cart("Burger"), "status_1": "processing"},
                            {"slot": 1, "status": "cancelled"})
check("cancelled lands even from processing", slots[0]["status"], "cancelled")

# ── a different set of dishes must not overwrite a waiting order ────────────
# Seen live: the guest had 2 Chicken Burgers waiting, asked for a separate order
# of 2 Arrabbiata Pasta, and the pasta REPLACED the burgers in the same slot.
# They were never placed and never mentioned again.
slots, changed = write_into(
    {"cart_1": cart("Chicken Burger"), "status_1": "ongoing"},
    {"items": [{"item": "Arrabbiata Pasta", "qty": 2}]})
check("a wholly different order opens a new slot", changed, [2])
check("and the waiting order is untouched",
      [i["item"] for i in slots[0]["items"]], ["Chicken Burger"])
check("the new dishes are in the new slot",
      [i["item"] for i in slots[1]["items"]], ["Arrabbiata Pasta"])

# An EDIT keeps at least one dish, so it must still edit in place.
slots, changed = write_into(
    {"cart_1": cart("Chicken Burger", "Filter Coffee"), "status_1": "ongoing"},
    {"items": [{"item": "Chicken Burger", "qty": 1}]})
check("dropping a line from an order still edits that order", changed, [1])
check("and really drops it", len(slots[0]["items"]), 1)

# When the model names the slot, it is obeyed even if the dishes differ.
slots, changed = write_into(
    {"cart_1": cart("Chicken Burger"), "status_1": "ongoing"},
    {"slot": 1, "items": [{"item": "Arrabbiata Pasta", "qty": 2}]})
check("an explicitly named slot is still honoured", changed, [1])

# ── a write can never remove food from an order with the kitchen ────────────
# Live: ticket 11173 held 1 Tandoori Murgh + 2 Butter Naan. The guest asked to
# add a filter coffee, the model sent ONLY the coffee as "the complete list",
# and the murgh and naan were wiped from the book AND from the ticket.
slots, changed = write_into(
    {"cart_1": cart("Tandoori Murgh", "Butter Naan"), "status_1": "placed",
     "ticket_ref_1": "T1"},
    {"slot": 1, "items": [{"item": "Filter Coffee", "qty": 1}]}, minutes_ago=1)
check("adding to a placed order keeps what was already on it", changed, [1])
check("all three dishes survive",
      sorted(i["item"] for i in slots[0]["items"]),
      ["Butter Naan", "Filter Coffee", "Tandoori Murgh"])

# A quantity change on a placed order still lands.
slots, changed = write_into(
    {"cart_1": cart("Chicken Burger"), "status_1": "placed", "ticket_ref_1": "T1"},
    {"slot": 1, "items": [{"item": "Chicken Burger", "qty": 5}]}, minutes_ago=1)
check("a quantity change on a placed order still applies",
      slots[0]["items"][0]["qty"], 5)

# An ongoing cart is still fully managed by the model, so removing works there.
slots, changed = write_into(
    {"cart_1": cart("Chicken Burger", "Filter Coffee"), "status_1": "ongoing"},
    {"slot": 1, "items": [{"item": "Chicken Burger", "qty": 1}]})
check("removing a line from an UNPLACED cart still works", len(slots[0]["items"]), 1)

# ── the edit window must actually open ──────────────────────────────────────
# QA found the 5-minute window never opened once: an order was "already being
# prepared" ~20 seconds after placing. placed_at was stamped with the PROCESS
# clock (the server runs UTC) and compared against the HOTEL clock (IST), so
# every order read as 5h30m old the moment it was created.
_fresh = {"n": 1, "items": [{"item": "Burger", "qty": 1}], "note": "",
          "status": "placed", "ticket_ref": "T1", "placed_at": B.now_local()}
check("an order placed just now IS still editable",
      B.editability(_fresh, now=B.now_local(), window_minutes=WINDOW)["editable"], True)
check("and it is described as still changeable",
      "left to change it" in B.editability(_fresh, now=B.now_local(),
                                           window_minutes=WINDOW)["why"], True)
# The two clocks must agree, or the skew comes straight back.
_skew = abs((B.now_local() - datetime.datetime.now()).total_seconds())
check("the book clock matches the hotel clock, whatever the server runs on",
      _skew < 90, True)

# ── TWO PEOPLE, TWO ORDERS, TWO COLUMNS ────────────────────────────────────
# A guest ordered for herself, then asked for a separate order for her
# vegetarian husband while her own cart was still OPEN (its ticket had failed,
# so it never left `ongoing`). Both landed in slot 1: her Nilgiri Tonic and
# Elaneer Payasam sitting inside "your husband's order" beside his Paneer Handi
# and Kulcha, read back to her that way, on one bill. prefer_new was being
# tested AFTER the ongoing-cart branch, so it only applied when no cart was
# open - the one case that never needed it.
slots, changed = write_into(
    {"cart_1": cart("Chicken Burger", "Nilgiri Tonic"), "status_1": "ongoing"},
    {"items": [{"item": "Paneer Handi", "qty": 1}]}, prefer_new=True)
check("a separate order opens a NEW slot even while a cart is open", changed, [2])
check("  and the open cart is left alone",
      [i["item"] for i in slots[0]["items"]], ["Chicken Burger", "Nilgiri Tonic"])
check("  and the new order holds only its own food",
      [i["item"] for i in slots[1]["items"]], ["Paneer Handi"])

# They must reach the DB as DIFFERENT COLUMNS, not one merged cart.
_u1 = B.row_updates_for_slot(slots[0])
_u2 = B.row_updates_for_slot(slots[1])
check("  the two orders write to different columns",
      ("cart_1" in _u1) and ("cart_2" in _u2), True)
check("  and nothing of order 2 leaks into order 1's column",
      "Paneer Handi" in json.dumps(_u1), False)

# Without the explicit ask, adding a dish still joins the open cart. The model
# re-sends the COMPLETE list for that order, so the write overlaps what is there;
# a wholly disjoint list is deliberately treated as a different order (see the
# pasta-overwrote-the-burgers rule above), which is why the burger is repeated.
slots, changed = write_into(
    {"cart_1": cart("Chicken Burger"), "status_1": "ongoing"},
    {"items": [{"item": "Chicken Burger", "qty": 1},
               {"item": "Filter Coffee", "qty": 1}]})
check("but an ordinary add still joins the open cart", changed, [1])
check("  and that cart now holds both dishes",
      sorted(i["item"] for i in slots[0]["items"]),
      ["Chicken Burger", "Filter Coffee"])

# The model is actually TOLD that orders are separate.
_ctx = B.for_the_model([{"n": 1, "items": [{"item": "Burger", "qty": 1}],
                         "note": "", "status": "ongoing", "ticket_ref": "",
                         "placed_at": None}],
                       now=NOW, window_minutes=WINDOW)
check("the model is told several orders can be open and must not mix",
      "SEVERAL ORDERS" in _ctx.get("orders_are_separate", ""), True)

fails = [n for ok, n in results if not ok]
print("\n" + "=" * 66)
print("%d/%d passed" % (len(results) - len(fails), len(results)))
for n in fails:
    print("  FAILED: " + n)
sys.exit(1 if fails else 0)
