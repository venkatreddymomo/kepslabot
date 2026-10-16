# Plan: (1) Guest chat history via the external API, (2) WhatsApp as a channel

Reference material read for this plan:
- `C:\Users\Hp\Downloads\kebo_fe-main\kebo_fe-main` — the KePSLA/Kebo React widget (`kebo_fe`), which talks to the **Java** `kepsla-rep-dashboard-svc`.
- `C:\Users\Hp\Downloads\lumira-main (1)\lumira-main` — Lumira (Flask), the WhatsApp guest-journey + engage bot.
- Our repo: `api/routes/lumira_compat.py`, `services/lumira_compat_adapter.py`, `services/engage_session_service.py`, `services/kebo_agent_bridge.py`, `core/context_manager.py`, `models/database.py`.

No code changed. This is scope, sequencing and the decisions that need to be made.

---

# PART 1 — Guest chat history

## 1.1 There are TWO external history systems, not one

### A. Kebo Java dashboard service — the one `kebo_fe` uses

Transport ([src/api/urlConstants.ts](../../../../../C:/Users/Hp/Downloads/kebo_fe-main/kebo_fe-main/src/api/urlConstants.ts)):

| Purpose | Call |
|---|---|
| Base REST | `https://reviews.kepsla.com/kepsla-rep-dashboard-svc` (axios, JSON, 30s timeout) |
| **History read** | `POST /insert/get-latest-conversations.htm` `{sessionId, botId}` |
| **Thread reset** | `POST /insert/clear-chat.htm` `{sessionId, botId}` → new `conversationId` |
| Send (HTTP) | `POST /insert/reply-v2.htm` |
| Send (WS) | STOMP over `wss://…/ws/socket`, publish `/app/kebo-chat.sendMessage` `{content:{…}, roomId: sessionId}`, subscribe `/topic/room/{sessionId}` |

History response shape (`src/api/conversations.ts`):

```ts
{
  messages: [{
    id, sessionId, entityId, sender,        // "user" | "bot" | "agent" | "system"
    text, widgetId, render,                 // "show_country_dropdown" | "show_city_dropdown" | form_key
    messageType,                            // "text" | "form" | "handover" | null
    media: [{ type[], url[] }],
    metadata,                               // CTAButton[] | FormSubmission | Dropdown
    createdAt
  }],
  sessionId, conversationId,
  currentResponder: "BOT" | "AGENT",
  isChatClosed
}
```

**Identity model** (`src/store/SessionUtil.ts`) — this is exactly the model we already run:
- `sessionId` — client-minted UUID in `localStorage["engagebot_session_id"]`, stable forever.
- `conversationId` — **server-issued**, returned by `get-latest-conversations`, cached in `localStorage["engagebot_conversation_id"]`, rotated only by `clear-chat`. Ours is client-minted and rotates on the dustbin button — same lifecycle, different issuer.

**Rehydration is a replay, not a text dump** (`src/provider/ChatProvider.tsx` ~1940–2400). Worth copying:
- `messageType === "handover" && sender === "system"` → system bubble.
- `render === show_country_dropdown|show_city_dropdown` → the dropdown is re-rendered **only if `isDropdownAnswered()` finds no later answer**; otherwise only its text is replayed. This is what stops a restored chat from showing five dead dropdowns.
- `media[0].type[]` / `media[0].url[]` are parallel arrays → attachments reconstructed with derived filenames.
- `currentResponder` drives `isAgent` (mutes the bot in the UI).
- Paging via `config.messagesPerBatch`; `MessageTimestampTracker.extractFromConversationHistory` recovers the last-user-message timestamp.
- `StorageUtil` (`localStorage["engagebot_messages"]`) is only a local cache — the server is the source of truth.

**The physical store behind it is `KEBO_ENGAGE_SESSIONS`** — the same table we already write to.

### B. Lumira — does **not** own history at all

Lumira is a stateless HTTP callee. History is read straight out of the platform's WhatsApp tables (`app/extensions/db/guest_journey_sql.py`):
- `fetch_last_n_messages(wa_number, entity_id, n=10, session_window_minutes=30)` on `GHN_FF_PROD.GHN_FF_WHATSAPP_MESSAGES`, matching `WA_NUMBER OR RECIPIENT_WA_NUMBER`, chronological, and **truncated at the last `"conversation transferred to bot"` marker** so post-handover context doesn't leak back into the bot.
- `fetch_last_few_messages(name, user_id)` on `GHN_PROD_BAK.WHATSAPP_CHATBOT_MESSAGES` + `insert_message_into_db(...)` for the configurator flow (Lumira writes only here).
- Plus a working-memory layer of its own: `app/guest_journey/utils/session_storage.py` (local JSON → `push_session_to_mysql`) and Redis (`session_memory`, sent-image dedupe).

So Lumira runs three overlapping history layers. **Do not copy that.** One display store + one model-memory store is the right shape.

## 1.2 Where we already are

| Piece | Status |
|---|---|
| Write turns to `KEBO_ENGAGE_SESSIONS` | ✅ `services/engage_session_service.py` — one row per turn, FIFO single-worker queue so ordering matches conversation order, fire-and-forget |
| KES ORM model | ✅ `models/database.py:920` |
| Read KES for **agent** messages | ✅ `services/kebo_agent_bridge.py` — polls every 3s per connected session, pushes `agent_message` / `system_handover` frames, tracks agent-active to mute the bot |
| Read KES for **history** | ❌ nothing |
| Server-issued `conversationId` | ❌ client-minted in the loader |
| `clear-chat` equivalent | Partial — `POST /api/chat/session/{id}/reset` exists but doesn't mint/return a conversation id |
| Our own history read | `GET /api/chat/session/{session_id}` returns `context.messages` (LLM memory shape: role/content/timestamp/metadata) |

## 1.3 Gaps that actually matter

1. **KES is write-only for us.** Nothing can rehydrate a chat from it — not our widget, not kebo_fe.
2. **`conversationId` issuer mismatch.** kebo_fe expects the server to hand it out on first history fetch. Ours is minted browser-side.
3. **Our KES rows are lossy for replay.** We set `render`, `message_type` ("text"/"form"), `metadata`, `total_cost/tokens` — but `media_type`/`url` stay NULL, `metadata` is our internal orchestration blob (not kebo's `CTAButton[] | FormSubmission | Dropdown` shape), and we never write `system`/`handover` rows from our side. Anything replayed from these rows today loses chips, forms, carousels and images.
4. **No `currentResponder` / `isChatClosed` surface**, even though `kebo_agent_bridge.is_agent_active()` already knows the answer.
5. **Two histories with no stated contract.** `new_bot_messages` + `context_manager` (model memory, trimmed, phase-scoped) vs KES (display/audit). Nobody has decided which is authoritative for what.
6. **Cold-context problem.** After a restart / on another worker, `context_manager` has no memory of the thread; KES does. We don't use it.

## 1.4 Recommended decision

> **KES = display + audit truth. `new_bot_messages`/`context_manager` = model memory. Never merge them; hydrate memory *from* KES only when memory is cold.**

Rationale: the kebo dashboard and any future FE already read KES; model memory needs trimming/phase-scoping/summarisation that would corrupt a display log.

## 1.5 Phased plan

### Phase 1 — history read API (the unlock)
- New module `services/chat_history_service.py`: `fetch_history(session_id, conversation_id=None, limit=N)` reading KES ordered by `id ASC`, filtered on `is_conversation_active`.
- Two surfaces over it:
  - `GET /api/chat/history?session_id=&bot_id=&limit=` — clean native contract.
  - `POST /insert/get-latest-conversations.htm` — byte-compatible with kebo_fe, so their widget can point at us with **zero** frontend change. Put it in `api/routes/lumira_compat.py` (that file is already our "speak someone else's dialect" layer).
- Mapping: `sender` USER→`user`, BOT→`bot`, agent→`agent`, system→`system`; `timestamp`→`createdAt` epoch ms; `metadata` JSON-parsed; `message_type`→`messageType`.
- `currentResponder` = `AGENT` if `kebo_agent_bridge.is_agent_active(session_id)` or the newest row has `to_responder='agent'`, else `BOT`.
- `conversationId` = newest row's `conversation_id`; if absent, mint one, persist it, return it.
- **Cost check first**: KES lives on the prod MySQL behind the VPN, and we already know DB-over-VPN round trips run ~seconds locally. Measure before putting this on the widget's open path; add a short-TTL per-session cache if needed.

### Phase 2 — server-issued conversation id + clear-chat
- `POST /insert/clear-chat.htm` (+ native `POST /api/chat/conversation/rotate`): mint a new `conversation_id`, write a `system` marker row, call the existing session reset, return the new id.
- Move minting out of the widget loader into the first history fetch. **This touches the dustbin-rotation behaviour we deliberately locked down** — keep "rotate only on dustbin", just change *who* generates the id.

### Phase 3 — make the write path replay-lossless
Today's writer drops everything visual. Extend `engage_session_service.write_turn` to also persist, from the display payload `_build_turn_display_payload` / `_attach_smart_images` / `_attach_menu_document` already produce:
- `media_type` / `url` (parallel arrays, kebo's format) for images, menus, carousels;
- `metadata` in kebo's discriminated shape — `CTAButton[]` for suggestion chips, `FormSubmission` for forms, `Dropdown` for city/property pickers — keeping our internal blob under a nested key so we don't lose telemetry;
- `message_type` values kebo understands (`text` | `form` | `handover` | `dropdown`);
- explicit `system` rows on handover/reset.

Note the ordering trap this table already bit us with: writes must keep going through the single FIFO worker, or the dashboard renders turns scrambled.

### Phase 4 — widget rehydration
Switch ChatHarness restore from `GET /api/chat/session/{id}` + localStorage to the Phase 1 endpoint, and port kebo's replay rules: unanswered-dropdown suppression, handover system bubbles, media reconstruction, batch paging. Keep localStorage as an offline-first cache that the server response overwrites.

### Phase 5 — cold-context hydration
When `context_manager.get_context()` misses, backfill the last N `use_for_context=1` KES rows into the context before the LLM runs. Bounded, feature-flagged, and off the hot path when memory is warm.

### Risks
- Latency on the widget-open path (VPN/DB) — flag + cache + measure.
- Double-render: our optimistic client bubbles vs replayed server rows → dedupe on stable ids (KES `id`, or a client-supplied idempotency key echoed back).
- PII: KES holds full guest text; a history endpoint is a new read surface — it must be scoped to `session_id` + `bot_id`/`entity_id` and never enumerable.

---

# PART 2 — WhatsApp

## 2.1 How WhatsApp actually works in the Lumira stack

**Lumira never touches WhatsApp.** The Java/GHN platform owns the channel end to end:

```
Guest WhatsApp
      │
      ▼
Meta WA Business API  ──►  GHN platform webhook
                              │  writes inbound row → GHN_FF_PROD.GHN_FF_WHATSAPP_MESSAGES
                              ▼
                    POST /guest-journey/message
                    { message, waNumber, phase, guest_id, guest_name,
                      room_number, entity_id, interactiveId, language }
                              │
                              ▼
                        Lumira (Flask)
                        · reads its own history from GHN_FF_WHATSAPP_MESSAGES
                        · runs scope-guard → classifiers → agents → ticketing
                              │
                              ▼
                    { response, media, url, name_of_file, is_ticket_intent,
                      needs_escalation, ticket_summary, category, sub_category,
                      room_number, outlet_id, input/output/total_tokens, cost }
                              │
                              ▼
                    platform sends via WA API + writes the bot row
```

Identity comes from the PMS, not from the phone number alone: `fetch_guest_info(guest_id)` → `GHN_FF_GUEST_INFO` (name, room, check-in/out, `MOBILE_NUMBER_1`, preferences).

**Implication: the entire WhatsApp channel is a webhook URL swap, not a build.**

## 2.2 What we already have

- `POST /guest-journey/message` — already live in `api/routes/lumira_compat.py:295`, Lumira-shaped.
- `services/lumira_compat_adapter.py:build_guest_journey_chat_request` — maps `waNumber` → `guest_phone`, sets `channel="whatsapp"`, `ticket_source="whatsapp_bot"`, carries phase/guest_id/room/entity.
- Phone → guest resolution against `GHN_FF_GUEST_INFO` (`services/chat_service.py:8456`) — matches on last-10 digits of `MOBILE_NUMBER_1/2`, prefers the booking active **today**, and derives the journey phase from check-in/out dates.
- Ticketing already routes with `ticket_source="whatsapp_bot"`.
- `config/settings.py:400-402` has `whatsapp_api_url` / `whatsapp_access_token` / `whatsapp_phone_number_id` — **declared and never read anywhere**. Dead config, ready to be adopted or deleted.

## 2.3 Option A (recommended first) — reuse the platform's WhatsApp bridge

KePSLA repoints the guest-journey webhook from Lumira to our FastAPI. No Meta app, no number provisioning, no send-side code, ticketing/escalation already wired.

**Work items**

1. **Response contract parity.** `build_guest_journey_response` currently returns `is_ticket_intent, response, category, sub_category, room_number, ticket_summary, outlet_id, tokens, cost, media, url` — but is **missing `needs_escalation` and `name_of_file`**, both of which the platform consumes (they're in Lumira's `success_schema`). Fill those, then diff field-by-field against `app/guest_journey/api/swagger_schema.py`.
2. **Idempotency.** The platform retries. Dedupe on `interactiveId` (already mapped to `metadata.message_id`) — we have `services/message_dedup.py` to hang this on.
3. **Response time.** Our median turn is ~15–25s. Confirm the platform's webhook timeout; if it's short, agree an async ack + callback with them rather than blocking.
4. **`phase` trust.** Lumira takes `phase` from the payload; we derive it from PMS dates. Decide precedence — recommend: **PMS dates win, payload `phase` is a fallback** (it's what our current code effectively does and it's more accurate).
5. **History source.** Under Option A the platform still writes `GHN_FF_WHATSAPP_MESSAGES`. Either read from it for continuity with the dashboard (Lumira's 30-min window + `"conversation transferred to bot"` truncation rules), or keep our own via `session_id = wa_{entity_id}_{waNumber}` and write KES rows. **Recommend writing KES** (so Part 1 works for WhatsApp too) and **reading the WA table only** when our context is cold.
6. **Handover.** Lumira has `handle_agent_handoff_guest_journey`; we have `kebo_agent_bridge`. Confirm which side mutes the bot for WhatsApp threads.
7. **Response surface.** Our replies are tuned for the web widget (`•` bullets + `**bold**`, no emoji). WhatsApp uses `*bold*` / `_italic_`, no markdown links, ~4096 char limit. Needs a `whatsapp` `response_surface` variant — prompt-side, not code-side.

**Effort: small.** Mostly contract diffing plus a channel-specific response surface.

## 2.4 Option B — own the number ourselves (Meta WhatsApp Cloud API)

Needed if we want to test/demo without the Java platform, or run a number KePSLA doesn't operate.

**Build**
1. `api/routes/whatsapp.py`
   - `GET /api/whatsapp/webhook` — hub-challenge verification with a verify token.
   - `POST /api/whatsapp/webhook` — **validate `X-Hub-Signature-256` HMAC**, dedupe on `wamid`, enqueue, **return 200 immediately** (Meta retries aggressively on slow/non-200; our 15–25s turn cannot run inline).
2. `integrations/whatsapp_client.py` — `send_text`, `send_image`, `send_document`, `send_interactive_buttons`, `mark_as_read`, using the three settings that already exist.
3. Async worker → build a `ChatRequest` (reuse `build_guest_journey_chat_request`; `session_id = wa_{phone_number_id}_{wa_id}`) → `chat_service.process_message` → send the reply → write KES rows.
4. Identity: `wa_id` → `GHN_FF_GUEST_INFO` via the existing last-10-digit lookup; unresolved numbers fall back to a pre-booking/anonymous phase.
5. **UX translation layer** — the real work:
   | Web widget | WhatsApp |
   |---|---|
   | Suggestion chips | Interactive reply buttons (**max 3**, 20 chars) or a list (10 rows) |
   | Forms / booking flow | Sequential Q&A turns, or WhatsApp Flows |
   | Carousels | Numbered text + images, or a catalog message |
   | Images / menu PDFs | Media messages (media id or public URL) |
   | `**bold**` | `*bold*` |
6. **24-hour customer-service window**: outside it, only approved templates can be sent. Any proactive/re-engagement message needs templates submitted and approved.
7. Ops: per-number rate limits, media upload/expiry, opt-in records, template approval lead time.

**Effort: medium-to-large**, mostly channel plumbing and UX translation, not bot logic.

## 2.5 Decision (2026-08-04): **Option A — drop-in replacement for Lumira**

We do **not** touch the Java service and we do **not** run our own WhatsApp number. Java keeps owning WhatsApp entirely; we take the seat Lumira currently occupies. Full contract in **Part 4** below. Option B (own number, Meta Cloud API) is parked — keep §2.4 for reference only.

Consequence worth stating: **no phone number, no Meta business verification, no template approvals are needed.** The hotel's existing number on the Java platform is the number.

**Hosting is already solved.** Verified on 2026-08-04:

| Check | Result |
|---|---|
| `https://bot.kepsla.ai/health` | 200, ~2 ms, `x-trace-id` present — our FastAPI |
| TLS certificate | valid (`ssl_verify_result=0`) — Meta requires this |
| `/api/*` reachable from public internet | ✅ reaches the app |
| `/api/whatsapp/webhook` | FastAPI 404 → **build here, no infra change needed** |

Remaining external dependency is Meta business verification + number registration.

**Cross-cutting caution:** WhatsApp fan-in is not rate-limited by a browser widget. Our known concurrency ceiling (~15 simultaneous turns, default SQLAlchemy pool + a DB connection held across the LLM call) will be hit far sooner on WhatsApp than on the web. Load-test before any real rollout.

---

# Suggested sequencing

| # | Item | Depends on | Size |
|---|---|---|---|
| 1 | KES history read service + `GET /api/chat/history` | — | S |
| 2 | `get-latest-conversations.htm` compat surface | 1 | S |
| 3 | Guest-journey response parity (`needs_escalation`, `name_of_file`) + `interactiveId` dedupe | — | S |
| 4 | `whatsapp` response surface (prompt-side) | — | S |
| 5 | **Option A go-live**: platform repoints webhook to us | 3, 4 | S (theirs) |
| 6 | Server-issued conversationId + clear-chat | 1 | M |
| 7 | Replay-lossless KES writes (media/CTA/form metadata) | — | M |
| 8 | Widget rehydration from history API | 2, 7 | M |
| 9 | Cold-context hydration from KES | 1 | M |
| 10 | Option B: Meta Cloud API webhook + sender + UX translation | 3, 4 | L |

# Open questions for you

1. Is KePSLA willing to repoint the guest-journey webhook at us (Option A), or do we need our own WhatsApp number (Option B)?
2. For WhatsApp history — keep the platform's `GHN_FF_WHATSAPP_MESSAGES` as the record, or move to KES so web + WhatsApp share one store?
3. Does kebo_fe need to run against our backend unchanged? That's the only reason to build the `.htm` compat surfaces rather than a clean native API.
4. Server-issued `conversationId` changes the rotation we deliberately locked to dustbin-only. OK to change the issuer while keeping the rotation trigger identical?

---

# PART 3 — Guest identity: "who is this phone number?" (and how to test)

Added 2026-08-04 after auditing the live GHN tables.

## 3.1 How identity works today

A WhatsApp message carries only two useful facts: **the guest's number** (`wa_id`) and **which of our numbers they messaged** (`phone_number_id`). Everything else — name, room, check-in/out, journey phase — is looked up from the PMS table `GHN_FF_PROD.GHN_FF_GUEST_INFO`.

Two code paths do this today, and both already work:

| Path | Where | Behaviour |
|---|---|---|
| `fetch_guest_profile` | `integrations/lumira_ticketing_repository.py:354` | `guest_id` first; else phone. **`entity_id` is optional** — with it NULL the search spans every hotel. Orders active-stay-first, then `UPDATED_DATE DESC`, `LIMIT 1` |
| `_resolve_guest_stay_from_ghn` | `services/chat_service.py:8467` | `guest_id` first; else phone **within a known entity**. Same active-first ordering, `LIMIT 1` |

Matching is on the **last 10 digits**, punctuation-insensitive, across `MOBILE_NUMBER_1` and `MOBILE_NUMBER_2`.

Phase is then derived from the dates, not trusted from the payload (`_phase_from_dates`, `api/routes/chat.py:246`):

```
check_in  > today                → pre_checkin
check_in <= today <= check_out   → during_stay
check_out < today                → post_checkout
no match at all                  → pre_booking (treat as a new lead)
```

There is also a deliberate guard (`chat_service.py` ~8733): these phone lookups are **skipped for web-widget visitors**, so a website lead is never silently matched to a stranger's stay. WhatsApp is exactly the channel they were built for.

## 3.2 The collision problem — measured, not hypothetical

Query run against live `GHN_FF_GUEST_INFO`, restricted to stays active right now:

```
same MOBILE_NUMBER_1 on >1 currently-active stay → 3 groups
  · 2 stays, 1 hotel, rooms 101 and 803
  · 2 stays, 1 hotel, room 801
  · 2 stays, 2 DIFFERENT hotels, rooms 110 and 702
```

So all three collision types are already present in production data. Each behaves differently:

**(a) Two bookings, same guest, different dates** — *already handled.* Active-today sorts first, so the current stay wins.

**(b) Two people sharing one phone, both in-house** (couple, family, colleague booking for others) — **broken.** `LIMIT 1` picks one arbitrarily. A towel request could be raised against room 101 when the guest is in 803. WhatsApp gives us one thread and one `wa_id`; there is no signal that distinguishes the two humans.

**(c) Same number in-house at two different hotels** — **broken, and worse.** With one shared WhatsApp number we cannot tell which property they're asking about, so we may load the wrong hotel's knowledge base entirely.

**(d) Number matches nothing** — fine. Falls through to `pre_booking`, treated as a new lead.

## 3.3 Fixes

**Fix 1 (the big one): one WhatsApp number per hotel.** Meta allows several numbers under one WhatsApp Business Account. Map `phone_number_id → hotel_code → guest_info_entity_id` in config. This makes `entity_id` known before any lookup, which **eliminates case (c) entirely** and shrinks (b) to a single-hotel problem. Cheap, deterministic, no LLM involved. Do this first.

**Fix 2: stop hiding ambiguity behind `LIMIT 1`.** Return all candidates. Auto-select only when exactly one matches. This is a small, contained change to the two functions above.

**Fix 3: ask when ambiguous.** More than one candidate → one clarifying turn ("Is this about room 101 or room 803?"), then **pin the chosen `guest_id` onto the session** so we never re-resolve mid-conversation. Pinning matters as much as asking — re-resolving each turn could silently switch identity halfway through.

**Fix 4: never invent a room number.** Both Sarovar test rows currently have `ROOM_NUMBER = NULL`. If the resolved guest has no room, the bot must ask before raising a ticket rather than filing it blank. (Consistent with the earlier PMS-room gap noted for entity 6311.)

**Accept and document:** two people genuinely sharing one handset share one conversation, one memory, and one food/preference profile. WhatsApp offers no way around this. It should be a stated limitation, not a bug to chase.

## 3.4 How to create a test guest (do this now — no WhatsApp needed)

Test data lives in **`GHN_FF_PROD.GHN_FF_GUEST_INFO`**, one row per booking.

Current state for Sarovar (`ENTITY_ID = 6311`) — note both rows already share one phone, i.e. collision case (b) is sitting in our own test data:

| ID | Name | Check-in | Check-out | Phase today | Room |
|---|---|---|---|---|---|
| 991475 | Sana1 | 2027-07-20 | 2027-07-30 | `pre_checkin` | **NULL** |
| 991476 | Sana2 | 2026-07-07 | 2027-07-01 | `during_stay` | **NULL** |

**Which entity id to use:** it comes from the hotel's service `form_config` —

| Hotel | code | id | guest-info entity |
|---|---|---|---|
| Sarovar Portico Sriperumbudur | `SAROVAR_PORTICO_SRIPERUMBUDUR` | 68 | **6311** (`guest_info_entity_id`) |
| Iconiqa | `ICONIQA` | 54 | 54 (`lumira_entity_id`) |
| Zone / KHIL / NewKhil | — | 67 / 66 / 65 | none configured → harness placeholder `5703` |

**To make your WhatsApp number a guest:** update one of the existing rows (safest) or insert a new one.

- `ID` — auto-increment, do **not** set it
- `ENTITY_ID` — 6311 for Sarovar
- `MOBILE_NUMBER_1` — your WhatsApp number (last 10 digits are what match; `+91`/spaces are fine)
- `CHECK_IN_DATE` / `CHECK_OUT_DATE` — **this is the phase dial**: future = pre-check-in, spanning today = in-stay, past = post-checkout
- `ROOM_NUMBER` — set it, or tickets will have no room
- `GUEST_NAME`, `EMAIL_ID_1` — `EMAIL_ID_1` is `NOT NULL`

To deliberately reproduce the collision, give two rows the same number with overlapping current dates and different rooms.

**Caution:** this is the production MySQL and our connection user is `root`. Only touch rows for test entities (6311), never real guest data.

## 3.5 Test ladder — each rung is usable before the next exists

1. **Chat harness, today.** `/api/chat/test-profiles` builds phase profiles straight from these rows (`_fetch_real_test_profiles`). Edit the dates, reload, and the widget greets you by name with the right phase. Tests the brain, not the transport.
2. **Simulate WhatsApp locally.** `POST /guest-journey/message` with `{message, waNumber, entity_id}` — the exact shape the Lumira/WhatsApp path uses (`api/routes/lumira_compat.py:295`). This exercises the real identity resolution, ticketing and `ticket_source="whatsapp_bot"` with no Meta account. **Best pre-Meta test.** Runs locally; not exposed publicly (nginx blocks that path).
3. **Collision tests.** Two active rows, same number, different rooms → confirm the bot asks instead of guessing. Then two rows across two entities → confirm hotel routing.
4. **Real WhatsApp**, once Meta approves — same brain, new transport.

## 3.6 Where the WhatsApp work actually lands

| Concern | Status |
|---|---|
| Public HTTPS endpoint | ✅ done — `/api/*` is live and proxied |
| Phone → guest → name / room / dates | ✅ exists, works |
| Phase from stay dates | ✅ exists |
| Ticketing with `whatsapp_bot` source | ✅ exists |
| `phone_number_id` → hotel mapping | ❌ build (Fix 1) |
| Ambiguous-match handling | ❌ build (Fixes 2–4) |
| Webhook + signature + async reply + sender | ❌ build |
| Chips → buttons, forms → Q&A, `*bold*` | ❌ build |
| Meta verification, number, token | ⏳ external |

The bot logic is largely done. What's left is the transport and the identity edge cases.

---

# PART 4 — The actual integration: become a drop-in replacement for Lumira

Decided 2026-08-04. **We do not touch the Java service and we do not run our own number.** Java keeps owning WhatsApp; we take Lumira's seat. Everything below is verified against Lumira source, not inferred.

## 4.1 Lumira's complete integration surface

Grepped every outbound HTTP call in `app/` and `worker/`. The full list is three calls, all to the Java platform:

| Direction | Call | Source |
|---|---|---|
| **In** ← Java | `POST /guest-journey/message` (and bare `POST /message` — the no-prefix blueprint registers both) | `guest_journey_bot_api.py:826` |
| **Out** → Java | `POST {TICKETING_BASE_URL}/insert/ticket.htm` | `ticket_creator.py:60` |
| **Out** → Java | `PATCH /insert/ticket/{id}.htm` (cancel/update) | `ticket_cancel.py` |
| **Out** → Java | `POST {AGENT_HANDOFF_API_URL_GJ}` | `worker/ticketing_tasks.py:350` |

**Zero WhatsApp API calls.** No `graph.facebook.com` anywhere. Lumira never sends a message and never writes the guest-journey message rows — Java does both. Lumira only *reads* `GHN_FF_WHATSAPP_MESSAGES`.

Note `/insert/ticket.htm` is the same `/insert/*.htm` family as kebo_fe's `reply-v2.htm` / `get-latest-conversations.htm` — one Java service is the hub for WhatsApp send, chat history, tickets and handoff.

## 4.2 Request contract (Java → us)

```json
{ "message": "...", "waNumber": "+91...", "phase": "During Stay",
  "guest_id": 101, "guest_name": "John Doe", "room_number": "101",
  "entity_id": 6289, "interactiveId": 0, "language": "english" }
```

`message` and `waNumber` are required. **Java supplies the identity** — `guest_id`, `guest_name`, `room_number`, `entity_id`. Lumira only falls back to a DB lookup **by `guest_id`** when `guest_name` and `room_number` aren't both present (`get_guest_info`, line 686). It never resolves a guest from the phone number.

> **This retires most of Part 3's collision problem.** Deciding *which* guest a shared number belongs to is Java's job in this model. Our phone-based resolution stays as a fallback only. Fix 1 (number-per-hotel) is likewise moot — `entity_id` arrives in the payload.

## 4.3 Response contract (us → Java)

From `swagger_schema.py::success_schema`:

`is_ticket_intent`, `response`, `media`, `url`, `name_of_file`, `needs_escalation`, `ticket_summary[]`, `category`, `sub_category`, `room_number`, `outlet_id`, `input_tokens`, `output_tokens`, `total_tokens`, `cost`, `error`

Status codes Lumira returns, which Java is coded against:

| Code | When |
|---|---|
| 200 | normal; also **duplicate suppressed** — `{is_ticket_intent:false, response:"", media:null}` (empty `response` = send nothing) |
| 400 | empty body (`invalid_payload`) or missing `message`/`waNumber` |
| 422 | `entity_not_found` |
| 429 | rate limit — returns a polite "Too many messages" string |
| 502 | `guest_info_failed` |

## 4.4 Behaviours we must copy (from `handle_direct_message`)

1. **Dedupe** — Redis `SET NX EX 2` on `md5(waNumber + message + unix_second)`. Duplicate → 200 with empty `response`.
2. **Rate limit** — 20 msgs/min per `(entity_id, waNumber)` → 429.
3. **Session window** — `get_active_or_new_session(wa_number, entity_id)`, 30-min idle.
4. **History** — `fetch_last_n_messages(wa_number, entity_id, n=20, session_start=…)`, falling back to a 30-min window. Read from `GHN_FF_WHATSAPP_MESSAGES`; **do not write to it**.
5. **First-message welcome prefix** — `maybe_prepend_welcome` prepends the "Hello! I'm your AI assistant…" paragraph when it's the guest's first message.

## 4.5 Gap list

| Item | Status |
|---|---|
| `POST /guest-journey/message` exists | ✅ `api/routes/lumira_compat.py:295` |
| Request field mapping | ✅ `build_guest_journey_chat_request` |
| Ticket create/cancel to the same Java paths | ✅ `services/ticketing_service.py` |
| `fetch_agent_for_handover` (manager staff id) | ✅ `integrations/lumira_ticketing_repository.py:255` — **but** it filters `dsc.ENTITY_ID` directly, whereas Lumira maps FMS→RI via `GHN_SURVEY_FMS_RI_ENTITY_MAPPING` and filters `dsc.ORG_ID`. **Verify ours returns a row before trusting it.** |
| `needs_escalation`, `name_of_file` in response | ❌ missing from `build_guest_journey_response` |
| Lumira's status-code semantics (400/422/429/502, empty-response dedupe) | ❌ |
| 2-second dedupe + 20/min rate limit | ❌ |
| First-message welcome prefix | ❌ |
| History from `GHN_FF_WHATSAPP_MESSAGES` | ❌ we use our own context store |
| Bare `POST /message` alias | ❌ only the prefixed path exists |
| **nginx exposes `/guest-journey/message`** | ❌ **returns nginx 404 on bot.kepsla.ai — the one infra task** |
| Java repoints its bot URL at us | ⏳ their change |

## 4.6 Guest-journey agent handoff

`.env` updated 2026-08-04: `AGENT_HANDOFF_API_URL_GJ` uncommented.

**Uncommenting alone does nothing** — `config/settings.py` has only `agent_handoff_api_url` (line 446); there is no `_gj` field, so nothing reads the variable. Three steps to actually wire it:

1. Add `agent_handoff_api_url_gj: str = ""` to `config/settings.py`.
2. Branch in `ticketing_service.handoff_to_agent` on channel/flow — WhatsApp/guest-journey uses the GJ URL, web/engage keeps the existing one. **The payloads differ**:
   - engage (ours today): `{from_responder, to_responder, conversation_id, session_id, to_agent_id, widget_id, reason}`
   - guest journey (Lumira): `{wa_number, from_responder, to_responder, to_agent_id, reason, entity_id}` — no conversation/session/widget
3. Resolve `to_agent_id` via `fetch_agent_for_handover(department_id, entity_id)` — confirm the ORG_ID/ENTITY_ID discrepancy above first, or it returns empty and the handoff posts a blank agent.

Also note the two handoffs live on **different services**: `kepsla-rep-dashboard-svc/insert/transfer.htm` (engage) vs `kepsla-fbm-api-svc/guestjourney/transfer` (guest journey).

## 4.7 Order of work

1. Response parity — `needs_escalation`, `name_of_file`, Lumira's status codes
2. Dedupe + rate limit + welcome prefix
3. Bare `/message` alias
4. **nginx: expose `/guest-journey/message`** (ask infra)
5. GJ handoff wiring (§4.6, three steps)
6. History source decision — Java's WA table vs our own KES rows
7. WhatsApp reply styling (`*bold*`, chips → plain text, no forms/carousels)
8. Java repoints to us on dev → test with a real WhatsApp number → prod

---

# PART 5 — Sarovar WhatsApp setup reference (verified against dev DB, 2026-08-04)

DB: `172.16.5.32` (dev, VPN). All values below were read from the live tables, not assumed.

## 5.1 Sarovar's ID map — memorise these

| What | Value |
|---|---|
| Our hotel code | `SAROVAR_PORTICO_SRIPERUMBUDUR` |
| Our hotel id (`new_bot_hotels`) | 68 |
| **FMS entity id** — guest info, WhatsApp config | **6311** |
| **RI entity id / org_id** — ticketing, departments, outlets | **7408** |
| WhatsApp number | **919187016262** (shared by 50 hotels) |
| Configured in our repo as | `guest_info_entity_id` in a service `form_config` |

The FMS↔RI split is the thing that trips people up. Guest and WhatsApp data key on **6311**; departments, outlets and media key on **7408**.

## 5.2 Already configured — do not touch

**`GHN_FF_PROD.GHN_FF_ENTITY`** (row ID 6311)

| Column | Value |
|---|---|
| `WA_Number` | 919187016262 |
| `AIBot_Interaction` | 1 |
| `WHATSAPP_AGENT_ENABLED` | 1 |
| `is_agent_handoff` | 1 |
| `Whatsapp_Message_Identifier` | 3 |
| `Status` | Active |

**`GHN_FEEDBACK_RESULTS.GHN_SURVEY_FMS_RI_ENTITY_MAPPING`** — `FMS_Entity_Id 6311 → RI_Entity_Id 7408`. Exists.

**Departments** — 6 wired under `org_id 7408`:

| dept_id | Name |
|---|---|
| 1 | Banquets |
| 5 | Front Office |
| 8 | Housekeeping |
| 34 | Food & Beverage |
| 41 | Engineering |
| 42 | Administration |

## 5.3 TABLE 1 — `GHN_FF_PROD.GHN_FF_GUEST_INFO` (the one that matters)

No row = the bot does not know the guest = no reply. This table **is** guest identity.

**Required (NOT NULL):** `ENTITY_ID`, `CHECK_IN_DATE`, `CHECK_OUT_DATE`, `EMAIL_ID_1`
**Auto:** `ID` (auto_increment), `CREATED_DATE`, `UPDATED_DATE`, `IS_EMAIL_SCHEDULED` (default 0)
**Should fill:** `GUEST_NAME`, `MOBILE_NUMBER_1`, `ROOM_NUMBER`, `ROOM_TYPE`
**Bot writes:** `GUEST_PREFERENCES` (JSON) — Lumira does this; we don't yet

**Phase comes from the dates, nothing else:**

| Dates vs today | Phase |
|---|---|
| check-in in the future | `pre_checkin` |
| check-in ≤ today ≤ check-out | `during_stay` |
| check-out in the past | `post_checkout` |
| no matching row | `pre_booking` (new lead) |

Phone matching is on the **last 10 digits**, punctuation-insensitive, against `MOBILE_NUMBER_1` and `MOBILE_NUMBER_2`.

**Existing rows — both broken:**

| ID | Name | Phone | Room | Problem |
|---|---|---|---|---|
| 991475 | Sana1 | 8123884969 | NULL | shares phone, no room |
| 991476 | Sana2 | 8123884969 | NULL | shares phone, no room |

That phone also receives staff SLA alerts — use a different number for testing.

**Insert a clean test guest (in-stay):**

```sql
INSERT INTO GHN_FF_PROD.GHN_FF_GUEST_INFO
  (ENTITY_ID, GUEST_NAME, MOBILE_NUMBER_1, EMAIL_ID_1, ROOM_NUMBER, ROOM_TYPE,
   CHECK_IN_DATE, CHECK_OUT_DATE, IS_EMAIL_SCHEDULED)
VALUES
  (6311, '<name>', '<your whatsapp number>', '<email>', '305', 'Deluxe',
   DATE_SUB(NOW(), INTERVAL 1 DAY), DATE_ADD(NOW(), INTERVAL 3 DAY), 0);
```

Change the dates to move the guest between phases. One row per booking.

## 5.4 TABLE 2 — `GHN_PROD_BAK.ORGANIZATION_OUTLETS` (0 rows for Sarovar)

Restaurants / bars / IRD, so food orders tag to the right outlet.

| Column | Value for Sarovar |
|---|---|
| `entity_id` | **7408** (RI id) |
| `fms_entity_id` | **6311** |
| `outlet_name` | e.g. `IRD`, restaurant name |
| `department_id` | **34** (Food & Beverage) |
| `group_id` | NULL |

Working example from hotel 6280: `(28, 8490, 6280, 'Aviator', 34, NULL)` — note `entity_id` there is *its* RI id.

## 5.5 TABLE 3 — `GHN_PROD_BAK.GHN_CHATBOT_MEDIA` (0 rows for Sarovar)

Menus, brochures, images the bot can send.

| Column | Value |
|---|---|
| `org_id` | **7408** |
| `fms_id` | **6311** |
| `header` | slug, e.g. `ird_menu_001` |
| `media_type` | `image` / `video` / `pdf` |
| `media_url` | S3 URL |

Hotel 6280 has 30 rows; Sarovar has none.

## 5.6 Not needed

`GHN_PROD_BAK.GHN_CHATBOT_INFO` — holds `hotel_data_json` / `prompt_data_json` for Lumira. Hotel 2637 gets 85 bot replies with **zero** rows here, so it is not the bot switch. We have our own KB. Skip it.

## 5.7 Why Sarovar has never actually been tested

The 17 inbound "Hello" messages on 6311 came from `…7975321645` and `…9302937780`. **Both are registered guests of hotel 2637**, not Sarovar. The platform resolved them to their real booking and answered them as 2637 guests — which is where 2637's 85 bot replies came from.

So Sarovar's bot slot has never received a message from an actual Sarovar guest. Its state is **unknown, not broken**.

**First real test:** insert one guest row per §5.3 with a clean phone → message 919187016262 → observe whether anything replies. That single test tells us whether the slot is live or empty.

## 5.8 Order of work

1. Insert one clean guest row (§5.3); fix or delete Sana1/Sana2
2. Message 919187016262 from that phone — does anything answer?
3. Add outlets (§5.4) so F&B orders route correctly
4. Add media (§5.5) if the bot should send menus
5. Only then chase Java routing — step 2 determines whether that's even needed
