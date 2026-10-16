# The order book

**Status: built, wired, and switched OFF** (`ird_order_book_enabled = False`). 76/76 offline. Nothing in the live path behaves differently until the flag is turned on.

## The problem it solves

The order lived in two places at once:

1. the sentence the bot wrote for the guest
2. a JSON field (`pending_data_updates.order_items`) it was asked to re-type in full every turn

Nothing reconciled them. On the 72-turn run of 2026-08-27, **3 of the 6 replies that said "I've added X" carried no field at all**. The cart post-process is gated on that field being present ([llm_orchestration_grounding_mixin.py:3991](services/llm_orchestration_grounding_mixin.py#L3991)), so on those turns the cart kept its old contents while the guest was told otherwise.

Ticket **11135** went to the kitchen as Filter Coffee + Tandoori Murgh after the guest was shown *"You've added 2 x Chicken Burger, total ₹3,375"*.

The book makes it one copy. The guest summary and the ticket description are both printed from the same row, so they cannot disagree.

## The table

`new_bot_ird_guest_orders` — one row per stay-session, 50 columns.

```
hotel_code, session_id, conversation_id        UNIQUE(hotel_code, session_id)
guest_name, room_number, check_in, check_out   copied from GHN, read-only there
cart_1   status_1   placed_at_1   ticket_ref_1
cart_2   status_2   placed_at_2   ticket_ref_2
...  ten slots
created_at, updated_at                          the 24h sweep keys
```

`cart_N`:

```json
{"items": [{"item": "Chicken Burger", "qty": 2, "price": 750,
            "allergens": "gluten, dairy"}],
 "note": "no onions, ring twice - baby sleeping"}
```

`status_N`: `ongoing` → `placed` → `processing` → `delivered`, plus `cancelled`.

Ten slots, not a list, because the row is cleared after 24h so slots recycle. When all ten are live, the **oldest `delivered`** is reused. If every slot is a live order, no slot is stolen — the bot says so instead.

## Who does what

| The LLM | The code |
|---|---|
| Writes dishes + note into a slot | Prints the guest summary from it |
| Sets the status when the guest acts | Prints the ticket description from it |
| Edits a slot when the guest asks | Runs the timer, decides if editing is allowed |
| | Advances status from the real ticket state |

The LLM is never asked to do clock arithmetic. Code compares `placed_at_N` and hands it words: *"about 3 minute(s) left to change it"*. Same reason serving times moved into code — it got the arithmetic wrong, and got it 13/13 right once told the answer.

## The lock rule

| Situation | Editable |
|---|---|
| `ongoing` — not sent | yes, freely |
| `placed`, under 5 min, ticket still open | yes — add, remove, change qty |
| 5 min elapsed **or** ticket In Progress | **no** |
| `processing` / `delivered` / `cancelled` | **no** |

Status advances by itself — window lapse or the ticket's own status. Nothing waits for the bot to notice.

## What the model writes

Today it re-declares the **cumulative running order across all orders**, every turn. That is why the ticket builder has to merge `_ird_cart` with `_ird_open_order_items`, subtract `_ird_placed_items`, skip that subtraction when `_ird_fresh_order`, and exclude `_ird_closed_order_refs`.

Under the book it writes **one slot at a time**:

```json
"pending_data_updates": {
  "cart_write": {"slot": 2, "items": [...], "note": "...", "status": "ongoing"}
}
```

`items` is the complete list **for that one order** — not a running total. All the merge/subtract arithmetic disappears.

## What this replaces

| Key today | Sites | Under the book |
|---|---|---|
| `_ird_cart` | 22 | `cart_N` where status = ongoing |
| `_ird_placed_orders` | 21 | rows with status placed/processing/delivered |
| `_ird_order_ticket_ref` | 18 | `ticket_ref_N` |
| `_ird_open_order_items` | 16 | `cart_N` — it *is* the order |
| `_ird_order_placed_at` | 13 | `placed_at_N` |
| `_ird_fresh_order` | 11 | **gone** — a new order is a new slot |
| `_ird_closed_order_refs` | 7 | **gone** — status says so |
| `_ird_placed_items` | 4 | **gone** — no subtraction |

## Rollout — table first, keys derived

~100 touch points. Repointing all of them in one go and hoping is not a plan.

1. **The book becomes the source of truth.** Writes go to the table.
2. **`pending_data` keys are regenerated FROM the table each turn**, so the ~100 existing readers keep working unchanged. They stop being independently maintained copies and become a projection — which is what kills the drift.
3. **Summary and ticket description repoint to the book first**, because that is where the bug actually bites.
4. Readers migrate off the derived keys afterwards, at leisure, with tests green throughout.

## Rules held

- **GHN tables are read-only.** Guest identity is copied out, never written back. All writes go to `new_bot_*`.
- **Recall reads the book first**, falls back to tickets if the row is gone — so the 24h wipe costs nothing.

## Built and proven so far

Everything below is committed and **wired but switched off** (`ird_order_book_enabled = False`).
With the flag off, not one thing about the live order path behaves differently.

| | |
|---|---|
| `models/database.py` | `IRDGuestOrders` — 50 columns |
| `scripts/migrate_create_ird_guest_orders.py` | idempotent, `--dry-run`, verifies after applying |
| `services/ird_cart_book.py` | slot rules, lock, status advance, write handler, both renderers, projection |
| `services/ird_cart_store.py` | load / save / sweep — two short DB visits per turn, never one held across an LLM call |
| `overnight/test_ird_cart_book.py` | **76/76** — the rules, no database |
| `overnight/test_ird_cart_store_sqlite.py` | **27/27** — the plumbing, against a real (SQLite) database |
| payload read | `order_book` replaces `order_lifecycle` when on — never both |
| cart write | after every existing validation, so the book stores validated lines only |
| ticket ref | written back into the slot the moment the ticket exists |
| ticket text | dishes printed from the slot, guest note carried through |
| subtraction | bypassed under the book — a slot is one order, nothing to subtract |
| sweep | hourly background task, only started when the flag is on |
| `overnight/patch_98_order_book.py` | prompt rule, anchor verified offline against the live text |
| `overnight/test_order_book_e2e.py` | judges the table and the ticket, not the bot's words |

Also fixed on the way: `_hotel_now()` is timezone-aware but MySQL `DATETIME` is naive, and
`aware - naive` raises `TypeError` — which the surrounding `try/except` would have swallowed as
"book unreadable", silently dropping every order back to the old path. Normalised and pinned.

## Runbook — when the DB is back

Read-only first, then one step at a time. Stop at the first thing that disagrees.

```bash
# 1. verify assumptions (READ ONLY)
python overnight/db_up.py && echo up
#    - which GHN table holds venkat / sana / suhas, and its room/stay fields
#    - GHN_OPERATION_TICKETS has id / status / assigned_id / ticket_auto_assign

# 2. create the table
python scripts/migrate_create_ird_guest_orders.py --dry-run
python scripts/migrate_create_ird_guest_orders.py

# 3. prompt rule
python overnight/patch_98_order_book.py --dry-run
python overnight/patch_98_order_book.py

# 4. turn it on, restart (services are cached per hotel_code)
#    ird_order_book_enabled = True in config/settings.py

# 5. prove it
python overnight/test_order_book_e2e.py        # table + ticket, not words
python overnight/test_full_ird_sweep.py        # 72 turns, nothing regressed
python overnight/check_tickets.py <session>    # external API
```

**Roll back**: set the flag to `False` and restart. The table can stay — nothing reads it when
the flag is off, and the old `pending_data` path is untouched.

## Still open, honestly

- Nothing has run against **MySQL** yet. The SQLite pass proves the round trip, the slot
  isolation and the sweep, but MySQL JSON is not SQLite JSON — the prod run still decides.
- The model still emits the flat `order_items` shape. That is deliberate — the book is proven
  against today's output first. Moving the output contract too would be two changes at once
  with no way to attribute a failure.
- The ~100 readers still read the derived keys. They work because the book regenerates them,
  but they should migrate off afterwards, with tests green throughout.
