# Plan — pre-booking phase behavior + chat bugs (gpt-5.4-mini)

Source: Saket Delhi chat, 2026-07-15 ~14:00. Scope: 5.4 prompt + a little shared code.
4.1 is NOT touched. Guiding rule below is mandatory.

## GUIDING PRINCIPLE (do NOT repeat the 4.1 mistake)
Do NOT hardcode word lists or "if the guest says X, decline with line Y" conditions. Give the
model the **bot's identity + the phase principle + ONE worked example per case**, and let IT
reason: *"which phase does this request belong to, and which phase are we in?"* The bot is in
**pre-booking**; its whole job is to help the guest **explore the hotel and book a room**. Every
decline/handoff decision flows from that, judged per message — never from a keyword table.

---

## Issue 1 — Phase-aware handling (the main ask)

**What we want (pre-booking):**
- **Identity:** the bot can ONLY help explore the hotel and book a room. It cannot itself provide
  any in-stay or pre-arrival service.
- **During-stay request** (food/room-service delivery, spa/treatment booking, housekeeping/towels,
  "my AC isn't working", anything consumed on-site): politely decline as a during-stay thing —
  *"I can't help with that — I'm here to help you explore the hotel and book a room; that's taken
  care of once you've checked in."* AC-broken example: *"I'm sorry, I can't assist with that — you
  haven't checked in yet; I'm here to help you explore and book a room."*
- **Pre-arrival request** (airport shuttle/transfer/cab, early check-in): *"that's arranged once
  your booking is confirmed."*
- **Decision-affecting info the KB lacks** (pet cost, an exact price/capacity that matters to the
  booking): after "I don't have that", OFFER *"want me to connect you with our team for that?"*
- **Genuine pre-booking support** (event/venue booking, a real request that only staff can do):
  offer the team handoff / support form.

**Root cause (verified in transcript):** the lean 5.4 `book_answerer` already carries B1
(during-stay decline), the pre-arrival line, CASE C/D, and the D1/D3 handoff rubric — but they read
as isolated CASE rules with (a) NO bot-identity framing and (b) no "judge which phase" instruction.
On 5.4 that produced: "order biryani" → flat "we don't offer room service" (no identity, no
phase framing); pet-cost asked 3× → "I don't have the amount" every time with NO handoff offer
(D1 didn't fire).

**Fix (5.4 prompt — LLM-reasoned, no word lists):** add a short, front-loaded block
`=== WHO YOU ARE (pre-booking) ===`:
- State the identity + the single principle (judge each request's phase; you're pre-booking; you
  only explore + book).
- ONE worked example each: during-stay decline (food/AC), pre-arrival ("once booking confirmed"),
  decision-info handoff (pet cost → offer team). Tell the model to GENERALIZE from these, not
  pattern-match them.
- Reinforce the existing D1 rubric: when a KB gap plausibly affects the booking decision, OFFER the
  team handoff instead of dead-ending on "I don't have it".
Keep the existing CASE/B1 lines as the mechanics; this block gives them the "why/identity" so 5.4
applies them consistently.

---

## Issue 2 — `<br>` (raw HTML) leaking into replies

**Root cause (verified):** 0 KB pages contain `<br>` — the **model** is emitting it. The prompt says
"plain Markdown only" but never bans HTML tags, so 5.4 sometimes adds `<br>` for a line break.

**Fix:**
- **5.4 prompt:** one line — *"NEVER output HTML tags (`<br>`, `<b>`, `<p>`, …). Plain Markdown
  only; for a break, start a new bullet."*
- **Optional code backstop (shared, defensive):** strip stray `<br>`/HTML tags in the
  response-surface/cleanup step so a stray tag can never reach the widget. Low-risk.

---

## Issue 3 — "list all hotels I showed interest in" lists hotels never discussed

**Root cause (verified, code):** `_reconcile_discussed_property_state`
(`services/llm_orchestration_service.py:1622-1625`) appends **every** `active_property_ids` entry to
the "discussed" order. When "which hotel is pet friendly" broadened active to all 24, all 24 (capped
to the last 12) were recorded as "discussed" — so the recall listed hotels the guest never engaged
with (Coimbatore, Digha, Gopalpur, Jammu, …). It conflates *"in scope for a fleet answer"* with
*"the guest showed interest."*

**Fix (code — memory bookkeeping, NOT a behavior guard):** only record a property as "discussed"
when it was a genuine **focus** (the guest named/narrowed to it — a small active set), NOT when it's
part of a broad fleet/broaden scope. Concretely: when the active set spans (nearly) the whole
catalog or came from a fleet-attribute broaden, do not fold those ids into `_discussed_property_order`.
Track genuine engagement, not fleet scope. (This is the same class as the `[:5]` fix — stop letting
transient scope pollute persisted memory.)

---

## Issue 4 (minor) — geographic guessing

"which one is closer to Bangalore" → bot invented *"Mussoorie is more southerly"* (both are in
Uttarakhand; not in KB). **Root cause:** comparative-geography reasoning not grounded in KB.
**Fix:** reinforce the grounding rule for comparative distance/geography — if the KB has no distance
data, say so and don't rank by guessed geography. (One line in the 5.4 grounding section.)

---

## Rollout
1. **5.4 prompt** (Issues 1, 2, 4): edit `prompts/models/gpt-5.4-mini/orchestrator.book_answerer.md`
   → `python scripts/sync_model_variants.py` → reload cache → test. 4.1 untouched.
2. **Code** (Issue 2 backstop + Issue 3): shared code, low-risk plumbing → restart to load.
3. **Test transcript:** order biryani / "AC not working" (→ identity decline), book shuttle
   (→ once booking confirmed), pet cost (→ offer team handoff), a `<br>`-prone answer, and
   "list hotels I discussed" after a fleet broaden (→ only genuinely-discussed ones).

## Honest caveats
- Phase handling on 5.4 is judgment — the identity block makes it *usually* right; it won't be 100%
  without word-lists (which you've ruled out, correctly).
- Issue 3's fix is memory plumbing; the "what counts as genuine interest" line is a heuristic, kept
  minimal and out of the answer path.
