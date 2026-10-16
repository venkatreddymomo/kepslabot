# Sarovar in-room dining — handover (round 2)

Paste this whole file as the opening prompt of a fresh session.

Hotel **Sarovar Portico Sriperumbudur** (`sarovar_portico_sriperumbudur`, id 68),
phase **during_stay**. Backend on **:8011**, no `--reload` — restart after any code
or DB-prompt change.

A 50-turn Playwright run has just been completed. Its results are below: six
issues are open, two of them serious. **Nothing is committed.**

---

## 1. Hard rules

1. **Never hardcode bot behaviour** — no keyword or phrase matching to decide
   intent. The LLM judges; code enforces the model's own emitted fields.
   **Only exception, granted explicitly: menu serving times.**
2. **Never make the bot dumber.** When it gets something wrong, first ask what
   information it was given. Nearly every failure so far has been the bot
   reasoning correctly over wrong, missing or ambiguous data.
3. **Tickets are judged against the EXTERNAL ticketing API.** A ref starting
   `LOCAL-` means the external call never landed and **the test proved nothing**.
4. **The bot's words and the ticket must MATCH.** A correct ticket with a reply
   saying something different is still a failure.
5. **Verify, don't assume.** Read the log or the DB before naming a cause.
6. **Never commit or push unless asked.**

---

## 2. Verified working (50-turn run, all tickets external, zero `LOCAL-*`)

- **Double-confirm fixed** — "yes place it" places on the same turn; ticket 10838
  raised immediately, no re-ask.
- **Serving times fixed** — Continental Breakfast refused with *"served from 7:00 AM
  to 10:30 AM… available again tomorrow starting at 7:00 AM"*; an untimed brownie
  accepted; browsing turns list only in-window sections.
- **Multi-order, both directions** — "also add a filter coffee to that" → same
  ticket 10838; "new order – 2 arrabbiata" → separate ticket 10839, pasta only;
  two-turn phrasing splits correctly; orders always shown separately with their own
  totals, never combined.
- Off-menu red wine declined, and "yes" to the follow-up listed alternatives without
  ordering · add/remove/quantity edits with correct totals · AC complaint mid-order
  raised Engineering 10840 and left the food orders intact · "kadak" refused with
  only the 2 real dining outlets · form dropdown clean · venue change updated the
  same ticket 10841 twice, no duplicate · fake "Kadak Hotel" edit refused, booking
  untouched · Cancel request closed the form with no ticket · towels 10842 · spa
  10843 carried treatment+date+time and cancelled without touching towels · identity
  Venkat/1400 · flight declined · checkout time correct.

---

## 3. Open issues, in priority order

### 3.1 Food-order cancel fails — WORST
- `"cancel my second order"` → *"I can't cancel orders directly from this chat"*
- `"cancel my whole order"` → *"already being prepared, can no longer be cancelled"*
- **Both food tickets stayed `open`.**

Note the *spa* cancel works and cancels its ticket, so the cancel machinery itself
functions — this is specific to food orders. Worth checking whether the food path
is being treated as locked by the kitchen-edit window
(`ird_edit_window_minutes`, default 5) and whether that lock is being reported as
"cannot cancel" rather than actually consulted.

### 3.2 `"what did i order"` is unreliable — 2 of 4 attempts ungrounded
It routes to the **KB book answerer** instead of in-room dining, then answers from
chat history with no prices. Once it invented the order: listed **"1 red wine"** —
which the bot had explicitly refused — plus wrong quantities.

**A declined item appearing as ordered breaks a stated pass criterion.**

There is an existing re-entry guard (`[IRD_REENTRY]`, `_guest_is_talking_about_their_food_order`)
meant to pull order questions back into the dining lane. Check whether it is firing
and why the router reaches the book answerer instead. **Diagnostic:** a recall reply
with **no prices and no total** is always the ungrounded path — a real cart render
always carries money.

### 3.3 `"actually make it 9pm"` hit the wrong lane
The guest was editing the **table booking**; the **food-order** refusal answered.
The booking time never updated. Two open contexts, wrong one claimed the turn.

### 3.4 `"whats my phone number"` → *"I don't have your phone number listed"*
`known_context.guest_phone` was populated. The data is there and the answer says it
isn't.

### 3.5 `"ok english please"` → replied with a menu upsell
Should acknowledge the language switch.

### 3.6 Wi-Fi password missing from the KB
Pre-existing content gap, not a code bug.

---

## 4. Two free efficiency wins (found, not yet done)

1. **The provenance judge pays for a call it throws away.** It makes its
   `gpt-4o-mini` call at ~line 4002 and only checks whether the feature is enabled
   at ~line 4014 — *after*. The feature is **off**, so every one of those calls is
   billed and discarded. Move the flag check above the call.
   (It also "would have dropped" Arrabbiata Pasta and Vegetable Burger — items the
   guest really ordered — confirming why it stays off.)
2. **The IRD judges are invisible to cost tracking.** `ird_item_provenance` and
   `ird_separate_order` are not logged to `new_bot_log_llm` at all.
   `_ird_guest_is_asking_to_order` is **dead code** and can go.

---

## 5. Latency — measured, no regression on the main path

| | Aug 19 | now |
|---|---|---|
| LLM calls/turn | 2.0–2.1 | 2.0 |
| router avg | 4,169–4,235 ms | **3,816 ms** (faster) |
| service agent avg | 4,692–4,932 ms | 6,360 ms |
| service agent input | ~19.8k tok | 25.6k tok |

Only the **service agent** got slower, from a bigger payload. Like-for-like on the
first call of a session the growth is **+1,008 tokens (+4%)**, of which
`menu_serving_windows` is 561 chars; the rest is `food_order_status` expanding as
multiple orders stay open — the cost of the multi-order feature itself.

Wall-clock caveats: the Aug 19 UI runs already recorded medians of **23.4s and
28.2s** before any of this work, and the current harness adds ~6–8s per turn of
measurement overhead (3s settle wait + `slow_mo` polling). Server-side is ~9.4s of
LLM + ~1.7s traced work. **Do not chase the UI number as if it were server latency.**

**Biggest single per-call cost:** the service agent's ~98k-char system prompt — the
**41k-char menu ships on every call**. That is the real optimisation if latency
becomes the priority.

---

## 6. Deliberately OFF — do not enable without re-testing

- `IRD_PROVENANCE_REFUSAL_ENABLED` — deleted food guests had explicitly ordered
  (three separate occasions). Observation-only now; it logs what it *would* have
  dropped.
- `IRD_PORTION_CHECK_ENABLED` — asked "how many portions?" right after the guest
  said "2 garlic naan".

---

## 7. Traps that have cost real time

- **Restart after any DB prompt edit** — services are cached per hotel_code.
- **Check backend start time against source mtime.** A "verified fix" was once
  tested against a build started 60s before the edit was saved.
- **Back up prompt rows before editing.** `backups/` is **gitignored** — those
  backups exist only on this machine. Service **98** = In-Room Dining,
  **86** = Table Reservation. `generated_system_prompt_override = 1` is a LOCK that
  stops auto-regeneration; keep it set.
- **A page reload rotates `conversation_id`** and the cart lives per-conversation.
  Chat history survives, the cart does not. Never reload mid-scenario.
- **Bash heredocs mangle `\n` and `\"`** — two patches silently failed this way, one
  printing "COMPILES" while changing nothing. Use the Write tool for patch scripts
  and always assert the anchor was found.
- **Wait for "Mapped test profile active"** (~18s) before New Session, or the guest
  profile is missing (greeting "Hi!" not "Hi Venkat!", no room number).
- Multi-order **merge and split pull against each other** through the same field —
  always test BOTH after touching either.
- DB timestamps are UTC; logs are IST. `new_bot_hotels` uses column **`code`**.
- `new_bot_log_llm.input_text` shows what the model *actually* received — use it to
  settle "did it even see this?" rather than guessing.

---

## 8. Suggested order of work

1. **Food-order cancel** (§3.1) — a guest told their order can't be cancelled while
   the kitchen ticket stays open is the most damaging failure here.
2. **`"what did i order"` misroute** (§3.2) — it invents orders, including items the
   bot itself refused.
3. The two efficiency wins (§4) — small and safe.
4. §3.3 wrong-lane edit, then §3.4/§3.5.
5. Re-run the 50-turn suite. Full spec is in git history of this file
   (commit before this one) if needed; the run above followed it.

**Test through the real widget, not the API** — an earlier API harness produced
`LOCAL-*` tickets and proved nothing.
