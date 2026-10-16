# Sarovar during_stay — bugs, root causes, fixes
**18–19 Aug 2026.** Every claim below is backed by a log line, a DB row, or a
code line. Where I could not prove something, it says so.

Verification method: 50-turn conversations driven through the **real widget** by
a standalone Playwright script (`overnight/run_overnight_ui.py`) — typing into
the composer, clicking Send, filling forms. Not the HTTP API underneath.

---

## 1. Bot invented a red wine that is not on the menu

**Root cause.** `_resolve_main_route_with_llm` (`llm_orchestration_service.py`
~3211) returned its neutral fallback whenever `context.pending_action` was set.
That produced `route=auto` with **no** `service_id`, so the turn reached neither
the router nor any service agent, and the generic orchestrator answered with no
menu loaded. Proof: routing log shows `main_router {"route":"auto","confidence":0}`
and there is **no main-router LLM row** for that turn; `"wine"` appears **0 times**
in the 44 KB In-Room Dining prompt pack, which is why the grounded agent had
correctly declined the same request four minutes earlier.

**Fix.** When a flow is pending, route to the service that owns it
(`pending_data._last_service_id`), validated as active and in-phase. Guests are
not trapped — the agent still escapes via `relevant_to_service=false`.

---

## 2. Made-up restaurant ("Kadak") accepted and reused

**Root cause.** `_maybe_handle_ticket_edit` runs **before** the router. Its
classifier is shown only `{user_message, last_assistant_message, tickets}` — no
outlet list, no menu — so it cannot tell a real outlet from an invented one, and
approved "table at Kadak" as an ordinary detail edit, rewriting the booking.

**Fix.** The model reports `names_specific_place_or_item`; when the rewritten
request would introduce a name that is not already in the ticket, the turn is
handed back to the service that owns the real list. Verified: unknown outlet
hands back 5/5, while time changes, cancels and naming the ticket's own outlet
still edit normally.

---

## 3. "Yes" did nothing — THE ONE THAT KEPT COMING BACK

**Root cause (code, not the LLM).** A Sarovar-only *stickiness guard*
(`llm_orchestration_service.py` ~4113) coerced `route="book"` and wiped
`service_id` whenever `topic_relation=="new_topic"` while routing to the
already-active service. On the failing turn the router emitted
`route=service, service_id=in_room_dining, reason="the guest's 'yes' is a
confirmation to place the in-room dining order"` — **it understood perfectly** —
and the guard binned it because the model had also mislabelled `topic_relation`.
The guest's "yes" to "Shall I place this order?" fell into the book lane.

**Fix.** The guard stands down while that service has an open question. Staying
on the service is recoverable (the agent can still delegate); dropping to book
abandons the transaction. Verified: `yes` → **"Order placed — on its way."**

**Secondary cause, not fixed and deliberately so.** The bot sometimes asks two
things at once ("a drink instead, **or** shall I place your order?"), which makes
a bare "yes" ambiguous. Three separate prompt rules failed to stop this,
including moving the rule to the very end of the prompt. It is now harmless — the
bot names real alternatives and acts on the "yes" — so I did not add a
response-rewriting layer. **Product call if you want it enforced.**

---

## 4. Guest details asked repeatedly

Not reproducible after the room numbers were seeded — never asked once across
four 50-turn runs. See §8.

---

## 5. Latency ~25 s per reply

**Root cause: network, not code.** Measured outside the app: a **5-token** LLM
request takes **~919 ms median** (2.3 s cold), and `SELECT 1` against the prod DB
takes **320 ms** over the VPN. Prompt size is not the driver — between 02:00 and
08:00 the router's prompt got *smaller* (23k → 21k tokens) while latency rose
**65%**. Per turn there are 3–4 LLM calls plus many DB reads.

One outlier worth watching: a single **orchestrator call took 68 seconds**
(run 1, turn 5). The other calls that turn were 4.3 s and 5.8 s. Unknown whether
provider-side or payload-driven — **not diagnosed**.

**The only real lever is fewer calls per turn.** That is an architecture
decision, deliberately left for you.

**Measured properly over 484 calls in 90 minutes (19 Aug, per call site):**

| call site | n | avg | max | >45s |
|---|---|---|---|---|
| build summary | 179 | 3.1 s | 5.3 s | 0 |
| service agent | 61 | 6.2 s | 48.0 s | 1 |
| main router | 61 | 3.1 s | 5.9 s | 0 |
| orchestrator | 23 | 7.5 s | 17.7 s | 0 |
| ticket edit router | 65 | 2.2 s | 5.1 s | 0 |
| cancel target check *(new)* | 16 | 0.9 s | 1.1 s | 0 |
| ird order intent *(new)* | 9 | 1.0 s | 1.8 s | 0 |
| ird item provenance *(new)* | 6 | 1.2 s | 2.5 s | 0 |

Two conclusions:

1. **The 60-70 s outliers are provider-side stalls, not a bad code path.** Only
   three calls all day exceeded 45 s and each was on a DIFFERENT call site
   (orchestrator 68 s, build summary 65 s, service agent 48 s). One in 484 in the
   last 90 minutes. No correlation with payload size.
2. **`build summary` ran 179 times across ~150 turns** — the highest-volume call
   in the system, roughly 3 per turn at 3.1 s each. Nobody has asked why it runs
   that often; it looks like the cheapest available win on latency, ahead of
   touching the orchestrator.

The three judges added today cost **under 1.3 s each** and fire only when needed.
They are not the latency problem.

---

## 6. AC complaint answered "Your booking request has been received"

**Root cause.** `"request"` — the DEFAULT category every ordinary ticket gets —
was listed as a *booking* category in `chat_service.py` (~3696). The booking
branch **discards the model's reply** and substitutes a receipt built from room
type and dates. A maintenance fault has none of those, so the receipt was empty
*and* the good reply was thrown away. The sibling guard directly above (ambient
stay dates must not flag a towels/maintenance request as a booking) is the same
bug found earlier from the fields side; the category side was left in.

**Fix.** Removed `"request"` from that set. One word; affects every generic ticket.

---

## 7. Cancelling one thing cancelled another — WORST BUG OF THE NIGHT

**7a. "Cancel the spa booking" cancelled the AC repair.** The spa form was never
submitted, so no spa ticket existed. Rule 3 ("when only ONE open ticket could
plausibly be meant, that is the one") handed the classifier the only ticket in
sight, at **high** confidence. **Not caused by the day's changes** — reproduced
3/3 on the pre-change prompt.

**7b. "Cancel the tiramisu" cancelled the whole £/₹1,800 order.** This one **was**
caused by the day's prompt edits, and by their *interaction*: my rule 0c and a
colleague's locked-order block each added heavy "cancel" language. Removing
either restored `modify`; only both together flipped it. Proven by A/B, 3 runs
per variant.

**Fixes.**
- Code invariant: `action=cancel` arriving with a non-empty `revised_request` is
  a **partial** change mislabelled as total → treated as modify. Field-based, so
  it holds for any service, wording or language. Verified on a transport ticket
  with no food involved.
- Destructive cancels get a **second opinion**: one message, one ticket, one
  yes/no, run only on cancels (same pattern as the existing
  `_subject_is_in_stay_only`). 12/13 stable, including `"cancel it"` → *true* so
  bare contextual cancels still work.
- `locked_ticket_ref` must name a ticket the **code itself** computed as locked
  **and** pass the same judge — a spa cancel no longer draws
  "your order is already being prepared".

---

## 8. Room numbers invented / hotel code in the room field → TICKETS SILENTLY REJECTED

**Root cause.** All four Sarovar guests had `ROOM_NUMBER = NULL` in
`GHN_FF_PROD.GHN_FF_GUEST_INFO`. With nothing to use, the service agent filled
the field from the nearest identifier — first `508`, then the hotel code
`sarovar_portico_sriperumbudur`. **The external ticketing API rejects a ticket
whose room is not a room**, returning `id: 0`, and the code logged
`ticket_create_api_completed` and moved on. Two real guest requests (TV remote,
drinking water) were thrown away with nobody told.

The room-number bug and the rejected-tickets bug were **one bug**.

**Fix.** Seeded room numbers (Venkat 1400, Sana2 305, suhas1 712, suhas2 918).
Every ticket in every run since carries `room=1400` and a real external ref.

---

## 9. "What did I order" listed a Mojito that was never ordered

Initially looked like a recall bug. It was not — see §11; the cart was genuinely
wrong. Correct after the cart fix.

---

## 10. Outlet field accepted any text

**Root cause.** The Table Reservation `outlet` field was declared
`type: "select"` but carried **no `options`**, so the widget rendered a free-text
box. A guest could book a table at an outlet that does not exist — the Kadak hole
via the form rather than the chat. Pre-existing.

**Fix.** Real dropdown limited to `SPR Kitchen` / `Chola Lounge - The Bar`,
options read from the service description. Confirmed rendering in the widget.

---

## 11. A browsing question became an ₹4,350 order — FOUND ONLY BY RE-RUNNING

Cycle 3 passed. Cycle 4, same build and same script, did this:

```
turn 3  "whats good for dinner"
        → "Here's your dinner ORDER SUMMARY: Kalan Pattani Chettinadu ₹600,
           Avocado on Toast ₹350, Peri-Peri Prawn ₹900, Chicken Burger ₹750,
           Tandoori Murgh ₹850, Butter Chicken Bowl ₹900 — Total: ₹4350"
turn 4  "one tandoori murg and 2 garlic naan."   → appended to those six
turn 6  "yes"                                    → all 8 placed
ticket  10580 :: 1 Kalan Pattani Chettinadu, 1 Avocado on Toast, ... 8 items
```

**Root cause, exact.** The service agent fired twice on turn 3. The first call
was correct (`respond_only`, no items). The second wrote its own **suggestions**
into `pending_data_updates.order_items` — *which is the cart* — and set
`pending_action=confirm_booking`. `_ird_cart` is built from that field
(`llm_orchestration_grounding_mixin.py` ~3340-3407); every validation asks
**"is this a real menu item?"** (`_ird_item_is_real`, price, allergens) and none
asks **"did the guest ask for it?"** — a suggested dish passes them all. Downstream,
`chat_service.py` ~3421 then composes the kitchen ticket from that cart
*deliberately overriding* the model's own text, which gave the phantom order
authority.

**Fix.** An item may only JOIN the cart on a turn where the guest was actually
asking for something; items already in the cart are untouched. Decided by reading
the conversation (not word matching), so it works for any dish, any wording, any
language, and on uncertainty it **allows** the item — a silently dropped dish is
a lost order. Verified 9/9 stable: browsing questions → false, orders → true,
and a bare "yes" accepting an offer → true.

**Lesson: one clean run proves nothing.** This is the strongest argument for the
repeated runs — the model is non-deterministic and cycle 3 simply got lucky.

---

## THE PATTERN

Every fix enforced **in code, against a fact the code computed itself**, held
across full 50-turn conversations. Every **prompt-only** rule passed in isolation
and slipped inside a long conversation — three separate attempts, including
repositioning to the end of the prompt.

The LLM should judge; anything that must not fail has to be enforced in code.

---

## MISTAKES I MADE (so you can discount my claims appropriately)

1. **Reported failures that were my own test harness, three times** — a test
   context missing `pending_data` (swallowed by fail-open), a debug spy that
   broke the very judge it was watching, and "the AC handoff failed" when the
   Engineering ticket existed and only the reply was silent.
2. **Said "4 of 4 tickets landed"** when two had come back `id: 0`.
3. **Caused §7b** — the whole-order cancellation — with a prompt edit, then fixed it.
4. **My chain script silently skipped two of three runs**, reporting
   `all three runs complete` after doing one: it launched with an absolute path
   containing spaces, PowerShell mis-parsed it, and I only checked whether the
   process was gone. A second variant then failed the same way because
   `"overnight
un_overnight_ui.py"` in Python source contains a CARRIAGE
   RETURN, not a backslash. Both now use forward slashes and confirm the process
   actually started before waiting on it.
5. **I put a `NameError` into a live code path** — the fix for §11 logged a
   variable (`_last_guest`) I had just removed while switching the judge to take
   the whole conversation. It never crashed: the surrounding `try/except` caught
   it, logged a warning, and **aborted the entire In-Room Dining post-process**,
   which also carries the veg/non-veg safety net and the ticket rebuild. So a
   fix intended to protect the cart silently switched off protections that were
   already working, for roughly two full runs — and both of those runs *looked*
   healthy. Found only by calling the function directly:
   `NameError("name '_last_guest' is not defined")`. Fixed and re-verified 3/3
   (browsing refused, real order passes, bare "yes" accepting an offer passes).

   **The lesson worth keeping:** a clean 50-turn run is not evidence the code is
   sound. An exception swallowed by a fail-open `except` looks exactly like
   correct behaviour from the outside.

---

## §11b. RESIDUAL — the guest-facing order summary still lists refused items

The cart, the placed order and the kitchen ticket are all correct after the
per-item guard. But the model writes `response_text` independently of the cart,
so on the turn where it tries to add dishes the guest never asked for, the guest
still SEES them in an "updated order summary" before the correct order is placed.

Verified live (run of 06:43): guest-facing turn 4 listed Soup of the Day,
Balsamic Grilled Vegetable, Chicken Dum Biryani and Elaneer Payasam; the guard
refused all four; ticket `10600` reads exactly
`1 Tandoori Murgh, 2 Garlic Naan`, and the "Order placed" message was correct.

**Wrong words, right outcome** — the inverse of the original bug, and far less
harmful (nothing is cooked or billed). **Not fixed:** the proper fix is to render
that summary FROM the validated cart instead of from the model, which is the same
pattern already used for the ticket text (`chat_service.py` ~3421, "the validated
cart is the only reliable record"). Deliberately not attempted while runs were in
flight, on the highest-stakes path in the system.

**Cart-fix history, for context** — it took three attempts and the first two are
instructive:
1. Turn-level guard ("is the guest ordering on this turn?"). Looked right; the
   model re-declares its whole running list every turn, so all six phantom dishes
   sailed in on the NEXT turn, which was a genuine ordering turn.
2. Same guard carrying a `NameError` (see mistake 5) — the judge answered
   correctly and the log line then threw, aborting the whole post-process.
3. Per-ITEM provenance: "did the guest ask for THIS item?", judged from the
   conversation. Verified on the exact failing turn and live in a real run.

---

## STILL OPEN

- **Compound questions** ("X, or shall I place your order?") — harmless now, but
  unfixed. Product call.
- **68-second orchestrator call** — one occurrence, undiagnosed.
- **Latency** ~25 s median — network-bound; needs an architecture decision.
- **`"Please assist, thank you."`** appearing as a ticket subject is my test
  harness's filler text, not bot behaviour.

## STATE

Nothing committed. Code changes are in the working tree; every DB change has a
timestamped backup in `backups/`. Run artefacts are in `overnight/`
(`run1_*`, `run2_*`, `run3_*`, `cycle1..4_*`).

---

## 19 Aug morning — the three you asked to be fixed, plus yesterday's scenario re-verified

**§7c. Spa cancel drew a food-order message (3/3 runs).** Two distinct paths, both
now closed:
- The *model's own* `reply`, written under rule 3a's locked premise, was being
  passed through after the judge had already rejected that premise. It is now
  discarded and the turn handed back, so the service owning the subject answers.
- A bare **"yes"** answering a transport FORM was hijacked by the locked path
  (my code's reply, verbatim). Cause: the cancel judge deliberately treats a bare
  affirmative as "about this ticket" so `"cancel it"` keeps working — which also
  made `"yes"` qualify. The locked path now first requires that the guest is
  actually asking to change or cancel something, judged from the message together
  with what the bot had just asked. **8/8 stable**: `yes` to a form / to
  "shall I place this order?" / to "should I go ahead?" = NOT a cancel; `yes` to
  "shall I cancel your order?" = IS a cancel.

**§7d. `0 Tiramisu` on a kitchen ticket — worse than cosmetic.** The cart builder
did `qty = max(1, qty)`, so a cancelled item arriving as `qty: 0` was **rounded up
to 1** and would be cooked and billed. Zero-quantity lines are now dropped, with a
log line saying so.

**§11c. The guest-facing summary is now built from the validated cart.** Rather
than trying to make the model write accurate prose (three prompt attempts failed),
when the guard refuses items and the reply still names them, the reply is rebuilt
with `_render_ird_summary()` — the server-authoritative renderer that already
existed for checkout, whose docstring reads *"so what the guest sees always
matches what is actually ordered"*. Verified: phantom items shown to guest = NONE,
and it closes with ONE answerable question.

**Yesterday's reported scenario, replayed end to end: 5/5.**

| turn | yesterday | now |
|---|---|---|
| "yes add one red wine also" | declined ✓ | declined ✓ |
| **"yes"** | reprinted the order, nothing happened ✗ | **Order placed** ✓ |
| **"what about wine"** | *"You have added one red wine to your order"* ✗ | *"Wine isn't listed on the room service menu"* ✓ |
| ticket | — | `1 Tandoori Murgh, 2 Garlic Naan` ✓ |

(Run over HTTP, so that ticket is `LOCAL-*` with `room=None` — HTTP sessions do not
resolve the property. Ticket delivery is verified separately by the widget runs,
23/23 external with `room=1400`.)

**Still open and NOT fixed:** `"yes lets continue"` after a handoff re-entered the
parked order in only 1 of 3 runs. Latency (~24 s median, network-bound) deferred
by the user.

---

## BUG 12 - the order-provenance guard DELETED food the guest really ordered

**Symptom** (run `session_1787137551857_q38cdwn0`, turns 12-13)
Guest ordered *1 tandoori murgh + 2 garlic naan*, reported a broken AC, we handed
that off, then came back with "yes lets continue". The bot came back with a
**Tiramisu-only 500 order**. On "yes place it" it re-asked *"Shall I place this
order?"* instead of placing, and the food ticket only landed a turn later, off
the back of "how long will it take".

**Exact root cause** - mine, introduced with the fix for BUG 9:

    [SAROVAR_IRD] the guest never asked for ['Tandoori Murgh', 'Garlic Naan']
                  - refusing them; keeping ['Tiramisu']

`_ird_items_the_guest_asked_for` judges only items that are NEW to the cart,
against the **last 10 messages**. Both assumptions broke together on the way back
from the detour:

1. the detour messages pushed *"one tandoori murg and 2 garlic naan"* out of the
   10-message window, so the judge could not see the request; and
2. the resume rebuilds the cart from scratch, so those items arrived looking
   **new** and were re-judged.

The judge answered correctly for the evidence it was given - the guest had indeed
not asked for them *in anything it could see* - and the guard deleted them.
Everything downstream followed from a cart that no longer matched the
conversation: the wrong summary, the re-ask on "yes place it", the late ticket.

**Fix** (`services/llm_orchestration_grounding_mixin.py`)
- items that pass provenance are remembered in `pending_data["_ird_vetted_items"]`
  and are never re-judged - the decision is made once, on the turn where the
  evidence is actually present;
- the judge's window widened from 10 to 16 messages;
- the vetted set is cleared with the cart when the placed order stops being
  editable, so a closed order can never vouch for a later one.

**Lesson** - a guard that can delete real data must never depend on a *truncated*
view of the evidence. Judge once, where the proof is, and carry the verdict
forward.

## Model coverage of the prompt changes (checked 2026-08-19)

The bot runs two models: **gpt-4.1-mini** (routers + service agent) and
**gpt-5.4-mini** (book answerer, chips). Prompts are forked per model ONLY in
`new_bot_prompt_registry`, which has a `model_variant` column - for Sarovar
(hotel 68) the forked row is id 70, `orchestrator.book_answerer`,
variant `gpt-5.4-mini`.

Every prompt change in this batch was made to `new_bot_services`
(`generated_system_prompt` for IRD id 98, `form_config` for Table Reservation
id 86). **That table has no model column**, so those prompts are shared and
already apply to both models. No `new_bot_prompt_registry` row was touched in
this batch (0 rows updated in the last 3 days), so there is no 4.1/5.4 fork to
mirror.
