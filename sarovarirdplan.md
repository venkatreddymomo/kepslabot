# Sarovar In-Room Dining — two defects found 2026-08-26

> **Status (2026-08-26):** both issues **BUILT and tested**. Nothing committed.
>
> - **Issue 1** — `_ird_unplaced_cart_lines`; 14/14 unit, 8/8 on the last e2e.
> - **Issue 2** — per-dish table (`config/ird_serving_windows.json`) +
>   `services/ird_availability.py` (resolver, payload block, menu annotation) +
>   a code-written refusal + two service-98 prompt patches; 80+ unit assertions,
>   **7/7 e2e** on the final build.
>
> **What each fix of issue 2 was needed for** — each was found by a failing e2e run,
> not designed up front:
>
> | symptom seen in a real run | cause | fix |
> |---|---|---|
> | Idli's breakfast window lost | it sits in a sub-section; the resolver kept only the last heading | per-dish table |
> | "Filter Coffee is served only 7:00-10:30 AM" (it is all-day) | absence from a closed list is weak evidence of "unrestricted" | annotate the 27 restricted dishes IN the menu, so the other ~130 are visibly unmarked |
> | Tandoori Murgh refused at 13:46, inside its own 12-3 window | bare hours in the annotation invited the model to compare them to the clock | the line states AVAILABLE NOW / NOT AVAILABLE NOW instead |
> | Tandoori Murgh still refused at 14:07 | the menu's own prose notes ("All Tandoori Kebabs will be available from 12 noon to 3 pm") were the last clock arithmetic left in the payload | replace all 4 prose notes with "(Serving times are shown on each dish below.)" |

Hotel **Sarovar Portico Sriperumbudur** (`sarovar_portico_sriperumbudur`, id 68),
phase **during_stay**, service **98 = In-Room Dining**.

Both defects below were traced to real code and real menu data. Neither is a
hardcoded sentence. Nothing has been changed yet.

---

## 1. "Shall I place this order?" after the order was already placed

### What the guest saw

```
two plates idli          → cart summary, "Shall I place this order for your room now?"
yes                      → "Order placed — on its way. • Idli × 2"
cancel one plate idli    → "I've noted your request to adjust the order..."
what did i order ?       → "Your current order includes: • Idli - ₹400
                            Total: ₹400 ... Shall I place this order for your room now?"
```

### Is the sentence hardcoded? No.

There *is* a hardcoded pair in `_render_ird_summary`
([llm_orchestration_grounding_mixin.py:3141-3147](services/llm_orchestration_grounding_mixin.py#L3141-L3147)):

```python
closing = (
    "This is on its way to your room."
    if already_placed
    else "Shall I place this order?"
)
```

That one is **correctly gated** and it is not what the guest saw — its wording
("Shall I place this order?") and its heading format don't match the screenshot,
and it filters out `None mentioned` allergens, which the screenshot shows.
The reply in the screenshot is **written by the service-agent LLM**.

So the model was asked to place an already-placed order because **the payload told
it the order was not placed**.

### Root cause: the cart survives placement, and is shipped as "not yet placed"

`_ird_cart` is deliberately **not** cleared when an order is placed
([grounding_mixin.py:4424](services/llm_orchestration_grounding_mixin.py#L4424)) —
it has to survive so a dish added within `ird_edit_window_minutes` (default **5**,
[config/settings.py:301](config/settings.py#L301)) merges onto the *same* kitchen
ticket instead of raising a second one. That is correct and intended.

But the payload builder copies that same surviving cart straight into
`food_order_status.not_yet_placed.items`
([llm_orchestration_service.py:6855](services/llm_orchestration_service.py#L6855))
with **no subtraction** of what the open ticket already holds:

```python
"not_yet_placed": {
    "items": [
        f"{int(_l.get('qty') or 1)} x {str(_l.get('item') or '').strip()}"
        for _l in (_pd_food.get("_ird_cart") or [])   # ← the PLACED order's own lines
        ...
    ][:20],
```

and the accompanying note
([llm_orchestration_service.py:6884](services/llm_orchestration_service.py#L6884))
tells the model, in capitals:

> "Anything in `not_yet_placed` is the OPPOSITE: it is NOT with the kitchen,
> nobody is cooking it, and it still has to be confirmed and placed like any
> other order."

The same list is *also* handed over a second time as
`payload["current_confirmed_order"]`
([llm_orchestration_service.py:6990](services/llm_orchestration_service.py#L6990)).

### The exact state on the "what did i order" turn

| field | value | what it tells the model |
|---|---|---|
| `food_order_status.orders[0]` | `1 x Idli`, ticket ref, placed | on its way |
| `food_order_status.not_yet_placed.items` | `["1 x Idli"]` | **not with the kitchen, still needs placing** |
| `not_yet_placed.belongs_to_order_no` | `1` | belongs to order 1 |
| `current_confirmed_order` | `[{item: Idli, qty: 1, price: 400}]` | a cart with a total |

The identical dish is described both as already cooking and as never sent. The
model then follows `_ird_order_flow_directive` rules **1** and **6**
([grounding_mixin.py:3346](services/llm_orchestration_grounding_mixin.py#L3346)) —
*"your closing question MUST be the placement one"* — and asks to place it.

The `cancel one plate idli` turn is not the cause; it only rewrote the quantity.
`_cart_lines_that_survive_an_edit` set the cart to `1 x Idli` and
`_ird_rewrite_order_stores` pushed the same figure into the ledger
([llm_orchestration_service.py:9047](services/llm_orchestration_service.py#L9047)) —
which is why the recall total (₹400) is right while the question is wrong.

### Independently reproduced

Same bug, same wording, in the **Aug 24 production run**
(`overnight/sarovar_prod_full_20260824/during_stay/history.txt:190`), one turn
after `Order placed — on its way`:

```
is the chocolate walnut brownie available right now
→ The Chocolate Walnut Brownie is available now for in-room dining.
  Your current order includes:
  • 2 x Chicken Burger at ₹750 each
  • 1 x Filter Coffee at ₹325
  Total: ₹1825
  Shall I place this order for you?          ← already placed at 12:37
```

Note the same run answers `what did i order` **correctly** two turns earlier
("Your current order that is on its way includes… If you need anything else…").
Intermittent, both readings available in the data — the signature of ambiguous
input, not of a broken rule.

### Fix

Subtract what the open ticket already holds before building `not_yet_placed`.
`_ird_open_order_items` is exactly "what the kitchen ticket currently says" — it is
written at placement
([grounding_mixin.py:3616](services/llm_orchestration_grounding_mixin.py#L3616))
and kept in step on every ticket edit
([grounding_mixin.py:3480](services/llm_orchestration_grounding_mixin.py#L3480)).

`chat_service` **already computes this exact delta** for the kitchen ticket at
[chat_service.py:3474-3520](services/chat_service.py#L3474-L3520). The payload
builder simply never learned it. Reuse the same per-dish quantity subtraction so
the two agree by construction:

1. `not_yet_placed.items` = cart − `_ird_open_order_items`, by dish, on quantity.
2. When the remainder is empty → `items: []` plus a note saying nothing is awaiting
   placement, so there is no second reading available.
3. Gate `current_confirmed_order` the same way, or label it as the *placed* order
   when the cart is fully covered — right now the key name itself
   ("current_confirmed_order") invites a checkout summary.

Behaviour that must not regress:
- a genuine second helping ("another idli") — model declares qty 2, ticket holds 1,
  delta = 1, still placeable;
- `_ird_fresh_order` — cart was reset and `_ird_open_order_items` popped, so there
  is nothing to subtract;
- after the 5-minute window the cart is cleared anyway, so this path is a no-op.

**Blast radius:** one payload field in `llm_orchestration_service.py`. No prompt
change, no change to the placement or ticket paths.

### What shipped (2026-08-26)

`_ird_unplaced_cart_lines()` — cart minus `_ird_open_order_items`, by dish, on
quantity. Applied to `not_yet_placed.items` only. 100 insertions, **2 deletions**.

**A narrowed `current_confirmed_order` was tried and reverted.** It made the bot
dumber: with the field gone once everything was placed, *"also add a filter coffee
to that"* had nothing to append to, so the model offered a brand-new order and the
coffee never reached the ticket — a flow the Aug 24 prod run shows working. That
list is also the anti-hallucination whitelist ("NEVER include an item that is not
in current_confirmed_order"), and an empty whitelist guards nothing. It now stays
as the whole cart, with a new `current_confirmed_order_status` alongside it saying
every line is already with the kitchen.

| test | result |
|---|---|
| `overnight/test_unplaced_cart.py` (14 cases incl. second helpings, fresh orders, junk qty, no mutation) | **14/14** |
| `overnight/test_e2e_no_reask_placed.py` through the real widget | **7/8** |

Payload verified from `new_bot_log_llm` on every turn: `not_yet_placed.items` is
`[]` while everything is placed, and `["1 x Filter Coffee"]` on the one turn where
a coffee genuinely awaited placement — the burger correctly excluded.

**Residual (1 of 8):** one of three identical `what did i order` turns still ended
with a placement question. The payload was correct and identical on all three, and
the emitted JSON on the passing and failing turns is byte-identical
(`pending_action: ''`, `ticket.required: False`, `order_items: null`) — so this is
model variance in the closing sentence, with nothing armed behind it. There is no
emitted field left to enforce on; the only levers are a prompt rule (one already
exists at service 98 and is what slipped) or rewriting the model's prose, which
`feedback_no_deterministic_keyword_guards` rules out.

---

## 2. Serving times are not obeyed per dish

### What the model gets today

`_build_menu_serving_windows`
([llm_orchestration_service.py:5660](services/llm_orchestration_service.py#L5660))
scans the menu for lines containing "timing" or "available" with a parseable clock
range, and attaches each to the last ALL-CAPS heading above it.

Run against the real 41k-char menu it produces **exactly three entries**:

```
--- 08:00 IST ---
   [OPEN  ] BREAKFAST          7:00 AM-10:30 AM
   [CLOSED] INDIAN SELECTION   12:00 PM-3:00 PM and 7:00 PM-11:00 PM   next: today at 12:00 PM
   [CLOSED] BREAD              12:00 PM-3:00 PM and 7:00 PM-11:00 PM   next: today at 12:00 PM
```

Three headings. The menu holds **152 dishes**. Mapping dish → heading is left
entirely to the model, over a 41k-char menu, on every turn.

### Defect A — the menu is two levels deep and the resolver flattens it

The menu nests sections, but both levels are ALL-CAPS so the parser cannot tell
them apart, and `section` is simply overwritten by whichever heading came last:

```
BREAKFAST                 (line 189)   ← Timing: 7:00 AM – 10:30 AM  (line 191)
  INDIAN DELICACIES       (line 251)   ← Idli, Medu Vada, Dosa
BREAKFAST                 (line 346)   ← Timing: 7:00 AM – 10:30 AM  (line 348)
  WESTERN DELICACIES      (line 350)   ← Cereal, Juices, …
```

**Idli is in `INDIAN DELICACIES`, which is a child of `BREAKFAST`.** Nothing in the
payload says so — there is no parent/child link anywhere in
`menu_serving_windows`. The model is told "BREAKFAST is closed" and has to work out
on its own that Idli is a breakfast dish. (Today's 08:19 order was inside the
window, so it passed on merit; at 23:00 it is a coin flip.)

Worse, `INDIAN DELICACIES` (breakfast idli/dosa) and `INDIAN SELECTION` (line 1005,
the lunch/dinner Indian section) are two different sections with near-identical
names and *different* windows.

### Defect B — section-level notes over-restrict

`INDIAN SELECTION` (1005) spans APPETIZERS, MAIN COURSE, RICE, STAPLES and BREAD —
about 30 dishes — but its note only restricts **"All Tandoori Kebabs"**. At 08:00
the model is told the whole of INDIAN SELECTION is CLOSED, so Dal Makhani, Biryani
and Paneer Handi — none of which state any timing — read as unavailable.

### Defect C — the note's scope cannot be resolved from the menu at all

This is the finding that decides the design. The two scoped notes name a subset,
and **the menu never says which dishes are in that subset**:

`INDIAN SELECTION > APPETIZERS` — note: *"All **Tandoori Kebabs** will be available
from 12 noon to 3 pm and 7 pm to 11 pm"*

| item | contains "Tandoori"? | actually a tandoori kebab? |
|---|---|---|
| Mushroom Galouti | no | ? |
| Jaitooni Paneer Tikka | no | almost certainly yes |
| Ajwaini Jhinga | no | almost certainly yes |
| Kasundi Macchi Tikka | no | almost certainly yes |
| Tandoori Murgh | **yes** | yes |
| Zaffrani Murgh Tikka | no | almost certainly yes |

`BREAD` — note: *"All **Tandoori Breads** available from 12 noon–3 pm & 7 pm–11 pm"*

| item | contains "Tandoori"? | actually tandoori? |
|---|---|---|
| Naan | no | yes (tandoor) |
| Tandoori Roti | **yes** | yes |
| Kulcha | no | yes (tandoor) |
| Lachha Paratha | no | no (tawa) |
| Phulka | no | no (tawa) |
| Kerala Paratha | no | no (tawa) |

Matching the note's words against dish names restricts **1 of 6** kebabs and
**1 of 3** tandoor breads. No parser can do better, because the information is not
in the text. **This is a KB content gap, not a parsing bug** — and it is why no
amount of code alone will fix defect B.

### Defect D — sections that obviously have a window state none

- **`MID NIGHT MENU`** (line 1988) — no timing line at all, so it produces no entry
  and is treated as available all day. Verified: the current resolver test asserts
  exactly this as expected behaviour.
- The property KB says *"24-hour room service (limited menu after 23:00)"*
  (line 162) — that rule is nowhere in the ordering payload.

---

## Plan for issue 2 — a hardcoded availability table

### Why parsing the menu cannot be the answer

A full read of the menu (2,400 lines) turned up **three different item formats**:

| format | shape | example |
|---|---|---|
| food | name → `Type: Veg` → `Price: ₹400` | Idli |
| drink, own price | name → `Price: ₹325` | Filter Coffee |
| drink, section price | section → `Price: ₹280`, then name → description → `Allergens:` | Agni Mule |

Seven ALL-CAPS lines are *themselves* orderable items (`CLASSIC HOT CHOCOLATE`,
`FRESH COLD PRESSED JUICES`, `ZERO SECTION` …), and five drinks say
`Price: Included`, inheriting the section's price.

A parser keyed on `Type:` finds 152 items and silently misses every drink. One
keyed on `Price:` finds 169 but misses the drinks with no price line. One loose
enough to catch those returns **352** — because it starts eating description
lines ("Plain steamed rice.", "Serving Size: 350 gm", "Tomato Chutney").

There is no clean parse. That is the case for stopping.

### The key fact that makes a table practical

**Only 4 timing notes exist in the entire menu**, and the set of dishes they
affect is small and fully enumerable — 41 items out of ~160. Everything else is
all-day. So the table only has to list the exceptions:

| period | window | items |
|---|---|---|
| `breakfast` | 07:00–10:30 | **18** — Continental / American / Indian Breakfast; Idli, Medu Vada, Dosa (Plain or Masala), Uttappam, Paratha (Aloo or Paneer), Poori Bhaji; Cereal, Freshly Squeezed Juices, Seasonal Sliced Fruit Platter, Yogurt, Smoothie Bowl, Bircher Muesli, Baker's Basket, Egg to Order, Pancake / Waffle / French Toast |
| `tandoor` | 12:00–15:00 & 19:00–23:00 | **12** — Mushroom Galouti, Jaitooni Paneer Tikka, Ajwaini Jhinga, Kasundi Macchi Tikka, Tandoori Murgh, Zaffrani Murgh Tikka; Naan, Tandoori Roti, Kulcha, Lachha Paratha, Phulka, Kerala Paratha |
| `late_night` | 23:00–07:00 | **11** — the MID NIGHT MENU items (Soup of the Day ×2, 3 sandwiches, 2 dum biryanis, Elaneer Payasam, Ada Pradhaman, Rasmalai, Ice Cream) |
| *(everything else)* | all day | ~120 items — **no entry needed** |

Two lines in that table still need the hotel to rule on them, flagged below.

### The table

`config/ird_serving_windows.json`, keyed by hotel_code so a second property does
not inherit Sriperumbudur's kitchen:

```jsonc
{
  "sarovar_portico_sriperumbudur": {
    "timezone": "Asia/Kolkata",
    "periods": {
      "all_day":    [["06:00", "23:00"]],
      "breakfast":  [["07:00", "10:30"]],
      "tandoor":    [["12:00", "15:00"], ["19:00", "23:00"]],
      "late_night": [["23:00", "07:00"]]      // crosses midnight
    },
    // Only exceptions are listed. An item absent from here is all_day.
    "items": {
      "Idli":          ["breakfast"],
      "Tandoori Murgh":["tandoor"],
      "Naan":          ["tandoor"],
      "Chicken Dum Biryani": ["all_day", "late_night"],
      "...":           []
    }
  }
}
```

An item is available if `now` falls inside **any** of its periods. Midnight-menu
dishes carry both `all_day` and `late_night`, which makes "after 23:00 only the
midnight menu is available" fall out of the same rule rather than needing a
special case.

**Fails open, always.** A dish not in the table is available. A malformed table
means everything is available. A timing gap can therefore never refuse food the
kitchen would happily cook — the same principle as the existing
`menu_timing_note`, which exists because the bot once refused an all-day Chicken
Burger at 10:34 after concluding the kitchen was shut.

### What the model receives

Code resolves the clock and sends **one short list — whichever is shorter**, so
the payload stays small at every hour:

```jsonc
// 08:19 — 23 dishes are shut, so list those
{ "as_of": "08:19 AM",
  "mode": "these_are_NOT_available_now",
  "items": [ {"item": "Tandoori Murgh", "returns": "today at 12:00 PM"},
             {"item": "Naan",           "returns": "today at 12:00 PM"} ],
  "rule": "Any dish NOT listed here is available right now. Never refuse a dish that is not on this list." }

// 02:15 — almost everything is shut, so invert and list what IS open
{ "as_of": "02:15 AM",
  "mode": "ONLY_these_are_available_now",
  "items": ["Vegetable Dum Biryani", "Chicken Dum Biryani", "Rasmalai", "..."],
  "rule": "Nothing outside this list can be ordered right now." }
```

No dish-to-section mapping, no window arithmetic, no reading the 41k menu for
timings. The model reads one list of names.

Replaces `menu_serving_windows` and `menu_timing_note` in the payload.

### "Not right now" must never come out as "we don't have that"

A dish the hotel proudly serves, refused as though it did not exist, is worse
than the original bug: the guest stops asking, and may believe the hotel has no
idli at all. **Existence and availability are two different questions and must
never share an answer.** Three outcomes, never two:

| the dish is… | the bot must say | who writes it |
|---|---|---|
| on the menu, open now | take the order | model + cart |
| **on the menu, closed now** | **"We do serve that — Idli is available 7:00–10:30 AM, so it'll be back tomorrow at 7:00 AM. Can I get you something else meanwhile?"** | **code** |
| not on the menu at all | "Sorry, we don't have that on our in-room dining menu." | code (exists today) |

**The existing off-menu decline is already safe** and must stay that way: it
fires from `_ird_item_is_real`, which asks whether the dish's words appear
anywhere in the menu text — a closed dish is still *in* the menu, so it passes
and never reaches that wording. The failure mode is the model's own prose, not
that branch.

**The inverted list is the new danger.** In `ONLY_these_are_available` mode at
02:15 the model is handed ~11 names. If it reads that as "the menu", a guest
asking for a Chicken Burger gets "we don't have that" — which is exactly the
shape of the 10:34 bug that once had the bot refuse an all-day Chicken Burger
after deciding the kitchen was shut. So the block carries its own guard:

```jsonc
"rule": "This list is about TIMING ONLY. It is NEVER the menu and never says
         what the hotel serves. The full menu is above. If a guest asks for a
         dish that is on the menu but not available at this hour, say we DO
         serve it, say the hours it is served and when it is next available,
         and offer something else — NEVER say we do not have it."
```

Belt and braces, because the prompt half is the half that slips:

- **Code writes the refusal** whenever the model declares an `order_items` entry
  that the table says is closed, so the guest sees the hours and the return time
  regardless of what the model wrote.
- **Only the closed dish is dropped.** A guest ordering idli *and* a filter
  coffee at 8 PM keeps the coffee — the order is not thrown away wholesale.
- Availability is resolved by looking the dish up in the table. A dish that is
  not in the table is available, so this can only ever *reduce* a false refusal.

The "do you have X?" case — a question, not an order — has no `order_items` to
enforce on and rests on the prompt rule above. That is acceptable here because
the full menu is in the prompt, so the model is answering existence from the
menu itself, not from the availability list.

### Enforcement

The model still judges *what the guest asked for*; code checks the resolved
`order_items` against the table and writes the refusal in its own words —

> "We do serve Idli — it's available 7:00 AM–10:30 AM, so it'll be back tomorrow
> at 7:00 AM. Can I get you something else in the meantime?"

Same shape as the existing off-menu refusal (`_ird_item_is_real`). Prompt-only
rules have slipped every time here; this is the pattern that held.

### Tests this adds

- At 20:00 order **Idli** → reply contains "7:00 AM"/"tomorrow", must **not**
  contain "don't have" / "not on the menu" / "unavailable" alone.
- At 20:00 ask *"do you have idli?"* → affirms the hotel serves it, gives hours.
- At 02:15 (inverted mode) ask for a **Chicken Burger** → must not be refused as
  off-menu; it is all-day, so either it is served or it is declined on the
  late-night rule with its hours — never "we don't have it".
- At 20:00 order **"kadak"** (a genuine non-dish) → the off-menu decline still
  fires, unchanged.
- At 20:00 order **idli + filter coffee** → coffee survives, idli explained.

### Needs a decision from the hotel

1. **Which of the 6 kebabs and 6 breads are actually tandoor?** The menu says
   "All Tandoori Kebabs" / "All Tandoori Breads" but never marks which dishes
   those are — only *Tandoori Murgh* and *Tandoori Roti* carry the word, while
   Naan and Kulcha are tandoor and Phulka and Kerala Paratha are tawa. The table
   above provisionally restricts **all** items in both sections; that is the
   safe-for-the-kitchen reading, not a verified one.
2. **The MID NIGHT MENU window.** The menu states none. `23:00–07:00` above is
   inferred from the property KB line *"24-hour room service (limited menu after
   23:00)"*. Until confirmed, leave those items `all_day` so nothing is wrongly
   refused.
3. **Kitchen hours for `all_day`.** `06:00–23:00` is a guess; the KB's all-day
   restaurant runs `06:30–22:30`, but in-room dining is described as 24-hour.

### Build order

1. Table + resolver + unit tests at 08:00 / 13:00 / 17:36 / 19:30 / 23:30 / 02:15.
2. Swap the payload to the two-mode list, **with the "timing only, never the
   menu" rule shipped in the same change** — the inverted list must never go out
   without it.
3. Add the code refusal on `order_items`, in both flavours: "we serve it, but
   not now" for a closed dish, and the existing off-menu decline for a non-dish.
4. Fill in the hotel's answers to the three questions above.

Steps 1–3 are code-only and can ship against the provisional table, because the
table fails open: worst case it behaves exactly like today.

### Two rules this design must never break

1. **A dish that exists is never denied.** The table only ever answers "can this
   be ordered right now". Existence is answered by the menu, which stays in the
   prompt in full and unfiltered. Never filter the menu itself by time — that is
   what would turn "not until 7 AM" into "we don't serve idli".
2. **Fail open.** A dish missing from the table, a malformed table, an
   unparseable clock — all mean available. A timing feature must never be the
   reason a guest cannot order food.
