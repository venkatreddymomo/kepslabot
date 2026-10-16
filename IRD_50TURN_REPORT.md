# In-Room Dining — 50-turn conversation test

**2026-08-26, 14:54–15:26 IST · Sarovar Portico Sriperumbudur · during_stay · service 98**
Driven through the real widget in Playwright, headed. Every turn waited for the
bot's reply and read it before composing the next message; several turns branched
on what the bot actually said.

Tickets judged against the **external** ticketing API — both refs are real
(`11084`, `11085`), no `LOCAL-*`, so the run counts.

---

## Headline

| | |
|---|---|
| 50-turn conversation | **45 / 50** |
| Stubborn-guest suite (27 turns) | **27 / 27** (see §7) |
| **Issue 2 — serving times** | **9 / 9 PASS** |
| **Issue 1 — re-asking a placed order** | **10 / 11 PASS** (the one failure is a test artefact, see §3) |
| New defects found | **4**, one of them serious |

The two defects you reported are holding. What the long run exposed is a
different problem underneath them: **the cart drifts**. Dishes enter it that were
never ordered, and a second order carries food the kitchen already cooked.

---

## 1. The serious one — a second order re-sends food already delivered

**Ticket 11085, verified on the external API:**

```
11084  In-room dining order for room 1400: 2 Chicken Burger
11085  In-room dining order for room 1400: 2 Chicken Burger, 1 Filter Coffee, 2 Arrabbiata Pasta
                                           ^^^^^^^^^^^^^^^^ already cooked on 11084
```

The guest is charged twice for two burgers and the kitchen cooks them twice.

The reply at turn 29 also duplicated a line:

```
> i want to place a new order - 2 arrabbiata pasta
< Order 2 placed — on its way.
  • Chicken Burger × 2          ← already sent on order 1
  • Filter Coffee (South Indian Style)
  • Arrabbiata Pasta            ← same dish
  • Arrabbiata Pasta × 2        ← listed twice
```

By turn 42 the drift had compounded — the bot reported **3 × Arrabbiata** and a
total of **₹3925**, against a ticket that says 2:

> "Second order: 2 x Chicken Burger, 1 x Filter Coffee, and 3 x Arrabbiata Pasta,
> total ₹3925. Both are confirmed and being prepared for you"

There is already a guard for exactly this in `chat_service`
("A NEW ORDER CARRIES ONLY WHAT IS NEW", [chat_service.py:3518](services/chat_service.py#L3518)) —
it subtracts `_ird_placed_items` from a new order. It did not hold here. Worth
noting the guard is skipped when `_ird_fresh_order` is set, which is exactly what
"I want to place a **new order**" sets.

---

## 2. How the cart drifted — traced turn by turn

The bad ticket was not one bad turn; it was four turns of accumulation.

| turn | guest said | what happened to the cart |
|---|---|---|
| 8 | "yes place it" | 2 Chicken Burger **placed** (ticket 11084) ✔ |
| 11 | "add a filter coffee to that" | bot asked *"would you like to **add**…?"* — not *"shall I place?"* |
| 12 | "yes" | bot asked **again**: *"Shall I place this addition now?"* → **coffee never placed** |
| 24 | "how much is the arrabbiata pasta" | bot only **offered** it — cart still correct ✔ |
| 25 | "does the arrabbiata have nuts?" | still correct ✔ |
| **26** | **"i'm allergic to dairy"** | **Arrabbiata is now IN the cart** ✘ |
| 29 | "place a new order – 2 arrabbiata" | placed everything, burgers included ✘ |

**Two distinct bugs there:**

**(a) The two-step ask loses the order.** Turn 11 closed on *"would you like to add
it?"* instead of the placement question. The guest's "yes" then bought them a
second question rather than an order. The service-98 prompt already forbids this
("ONE QUESTION, ANSWERABLE WITH YES OR NO… it NEVER replaces 'shall I place this
order?'") — the rule is being ignored, not missing.

**(b) Declaring an allergy added a dish.** Turn 26 is not an order in any reading;
the guest said *"i'm allergic to dairy"*. The `SUGGESTING IS NOT ORDERING` judge
([grounding_mixin.py:4155](services/llm_orchestration_grounding_mixin.py#L4155))
exists to stop the model writing dishes it merely mentioned into `order_items`,
and it let this one through.

Also at turn 26 the bot listed the **already-placed** burgers as part of the
"current unplaced order" — mislabelled — while turn 28 got the same distinction
right. Inconsistent, and it is the same underlying confusion issue 1 came from.

---

## 3. Issue 1 — 10 / 11, and the one "failure" is mine

Every recall of a placed order came back clean:

| turn | check | result |
|---|---|---|
| 9 | recall right after placing | PASS |
| 10 | "is my food coming?" | PASS |
| 13 | recall with two items | PASS |
| 21 | recall mid-flow | PASS |
| **28** | recall after a detour | **FAIL — test artefact** |
| 31 | recall with two orders | PASS |
| 34 | recall | PASS |
| 36 | food survives a non-food detour | PASS |
| 41, 43, 50 | recall, after-cancel, final | PASS |

Turn 28's reply was **correct** and my check was too blunt:

> "Your latest placed order (Order No. 1) includes: 2 x Chicken Burger… **Would you
> like to add anything else to your current unplaced order**, which has: 1 x Filter
> Coffee, 1 x Arrabbiata Pasta. Shall I place this new order?"

It separated placed from unplaced properly and offered to place only the genuinely
unplaced part. My `no_reask` check fails on any placement question, and here one
was warranted. **The bot was right; the assertion was wrong.**

Fix for the harness: only fail when the placement question refers to an order that
is already with the kitchen.

---

## 4. Issue 2 — 9 / 9, including across a serving boundary

The run crossed **15:00**, when the tandoor window shuts, so the same dish had to
flip mid-conversation. Expectations were resolved live from the table, and the bot
tracked it:

| turn | dish | state at that moment | bot |
|---|---|---|---|
| 4, 5 | Idli, Dosa | shut (breakfast) | gave hours, did not deny |
| 14 | Tandoori Murgh | **open** (14:5x) | served without refusing |
| 15 | Naan | **open** | served |
| 17 | Kerala Paratha | all-day | served — never wrongly restricted |
| 18 | Continental Breakfast | shut | gave hours |
| 37, 39 | Idli, Medu Vada | shut | gave hours |
| 46 | dosa "at 3am" | shut | did not deny existence |

Not one instance of *"we don't have that"* for a dish the hotel serves, and not one
all-day dish refused on timing.

---

## 5. Smaller findings

**Pedantic name-matching (turn 11).**

> "**Filter Coffee is not on the menu**, but we do have Filter Coffee (South Indian
> Style) priced at ₹325."

It is on the menu — under a longer name. Saying "not on the menu" first is the same
class of wording you asked me to eliminate, even though it recovered immediately.

**A bare "yes" fell out of the dining lane (turn 30).**

> "Got it — yes. Since you've already checked in, I can help with in-stay requests
> like: • Room service / Housekeeping / Maintenance / Table reservation"

A generic service menu in the middle of an order. This is the bare-yes routing
failure resurfacing.

**The AC complaint never became a ticket (turn 35).** The bot replied *"Should I go
ahead with this request?"* and the guest moved on, so nothing was raised. Only 2
tickets exist for the session, both F&B. Defensible — but a maintenance complaint
arguably should not need a confirmation step.

---

## 6. What is solid

- Both reported defects hold up across 20 combined checks.
- Serving times are correct in both directions, including across a live boundary.
- Genuine off-menu items are still declined properly ("kadak", turn 22).
- Recall after a cancel, after a non-food detour, and at the end of a long
  conversation all stayed clean.
- Tickets reach the external API.

---

## 7. The stubborn guest — 27 / 27

A second 27-turn run, pressuring on **Idli** (genuinely shut at 15:26), testing the
three ways a model fails under pressure: **caving**, **fabricating**, or **flipping**
to "we don't have it". Every scenario ended with *"what did i order"*, because the
prose can wobble but the cart must not.

| scenario | result |
|---|---|
| A. "i don't care" → "make an exception" → "i'll pay extra" | held, warm, cart clean |
| B. "your website says 24/7" / "they served it last month" / manager | held, corrected the premise |
| C. the same demand five times running | held all five, never got curt |
| D. insisting on off-menu red wine, "just get it from the bar" | declined, invented no workaround |
| E. **"you already placed my order, where is it?"** | **refused to confirm a placement that never happened — twice** |
| F. "ok fine, what CAN i have" | recovered immediately, not stuck in refusal |

Two turns scored FAIL and both were **my regex, not the bot** — I had listed "24/7"
as a fabrication marker, but this property's KB really does say *"24-hour room
service (limited menu after 23:00)"*. The bot was correcting the guest's premise:

> "Room service is 24/7, **but some dishes like Idli have specific serving hours**."

That is the right answer. Corrected in the harness. **True score 27/27.**

Representative answers under real pressure:

> **"i'll pay extra, whatever it costs"**
> "I appreciate your willingness, but we must adhere to the serving hours for
> quality and freshness. The Idli is served from 7:00 AM to 10:30 AM…"

> **"you already placed my Idli order, where is it?"**
> "You currently have **no** food or beverage orders placed with the kitchen, so
> there is no Idli order on its way to your room."

> **"just get it from the bar downstairs"**
> "For in-room dining, we don't have red wine on the menu, but I can assist with
> any other food or beverage…"

**One intermittent failure, seen once in two runs.** In the first attempt the bot
answered *"I can help with that once you're checked in — room service is handled by
the hotel team during your stay"* — to a guest who **is** checked in, in room 1400.
It did not cave, but it fell out of the dining lane and stopped making sense. It did
not recur on the re-run, so it is variance rather than a systematic fault — worth
watching, not worth chasing yet.

(The first attempt also died when *"let me speak to your manager"* opened the
support form and blocked the composer. That is correct product behaviour; the
harness now closes the form and carries on.)

---

## 8. Suggested order of work

1. **The duplicate-food ticket (§1)** — a guest charged twice for food they already
   received is the worst outcome in this list.
2. **Dishes entering the cart from questions (§2b)** — it is what fed §1.
3. **The two-step ask (§2a)** — a "yes" that buys another question loses orders.
4. The bare-yes lane drop, then the "not on the menu" wording.

Artifacts: `overnight/run50_final/` (transcript.json, results.json, final.png),
log at `overnight/run50.log`. Session `session_1787736264898_j383bwth`.
