# Queue for when the DB / VPN is back (expected shortly after 08:00)

A watcher (`overnight/wait_for_db.py`) is polling and will announce the moment a
real `SELECT 1` succeeds. Nothing is committed.

## 0. Before anything
- Restart the backend — `services/llm_orchestration_service.py` changed after the
  last restart, so the running build is stale.
- Check backend start time against source mtime before trusting any result.

## 1. Verify the two fixes written blind (highest priority)

**a. Table booking no longer claimed by the kitchen lock (§3.3)**
The lock used to decide "is this food?" from the ticket's category TEXT, and a
Table Reservation was filed as category `In-Room Dining`. It now decides from
`_order_no_by_ref` (the ledger of refs recorded as each food order was placed).
Scenario: place a food order → book a table → `actually make it 9pm` → the TABLE
ticket must update; the reply must NOT be "that order is already being prepared".

**b. A cancelled order stops being reported as with the kitchen**
Cancel emptied the cart but left `_ird_placed_orders`, so recall still read the
food back as `already_sent_to_the_kitchen` with `can_still_be_changed: true`.
Scenario: `overnight/turns_cancel_recall.json` — the last turn must NOT list the
cancelled food. (The run that would have proven this died when the DB dropped.)

## 2. Diagnose §3.4 — "whats my phone number"
Reply was "I don't have your phone number listed" while `known_context.guest_phone`
= 917036718200 and the own-contact directive is correct. NOT yet root-caused —
needs `new_bot_log_llm.input_text` for that turn to see WHICH agent answered and
whether it was given the profile at all. Do not guess; the previous two root causes
both came from reading the actual input.
- Session: `session_1787237608853_hwan7usn`, turn 42.
- Tool: `python overnight/dump_llm_turn.py <session> "whats my phone number"`

## 3. Placement flake
One run in six dropped the order with an EMPTY cart at the confirm gate while the
model had emitted `create_ticket` + `ready_to_create=true`. `[IRD_CONFIRM_GATE]`
now logs the cart size at exactly that moment. Repeat
`overnight/turns_place3.json` several times and read the gate line on any failure.

## 4. Veg keyword list (rule-1 violation)
`_sarovar_ird_fix_veg_grouping` uses a hardcoded NONVEG token list
("chicken", "murgh", "fish", …). Removing it needs a menu-render test to confirm
veg/non-veg grouping does not regress.

## 5. Re-run the full 50-turn suite
`RUN_TAG=run51 UI_HEADLESS=1 python overnight/sarovar_50_turn.py`
then `python overnight/grade_run.py run51`.
Judge bot words AND tickets. Zero `LOCAL-*` or the run proved nothing.

## Parked by the user
- Language items: the hardcoded English locked-order sentence, and
  "ok english please" answering with a menu upsell.

## Tools built this session
| file | purpose |
|---|---|
| `sarovar_50_turn.py` | real-widget runner; `UI_TURNS_JSON`, `MAX_TURNS`, `RUN_TAG` |
| `grade_run.py` | grades a run against the pass criteria |
| `dump_llm_turn.py` | what the model actually received / emitted |
| `latency_breakdown.py` | LLM calls + ms per turn, per call-site |
| `step_hotspots.py` | non-LLM substep timings |
| `compare_service_input.py` | which part of the prompt grew, old vs new session |
| `probe_reentry.py` | asks the re-entry judge the real messages |
| `test_serving_windows.py` | serving-window parser unit tests (no DB needed) |
| `wait_for_db.py` | the watcher |
