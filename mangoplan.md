# Mango Grove Hotel, Chandigarh — pre-booking onboarding plan

> Single-property tenant. Pre-booking phase only.
> Written 2026-09-04 against the Zone (multi-property) and Sarovar (single-code) precedents.
> **Nothing implemented yet — this is the plan.**
> Mechanism, routing and gotchas live in [prebookingskill.md](prebookingskill.md); this file is the property-specific build.

Source collateral lives in `D:\Sana Hp Laptop Data\D Drive Data\kepslasw\mango\`:
- `mango_grove_hotel_chandigarh_kb.txt` — the KB, built from their onboarding workbooks
- `mango_grove_hotel_chandigarh_kb_gap_report.xlsx` — 62/75 data points present, 13 gaps

---

## 0. What Zone does, and why Mango is not Zone

Zone is a **24-property** tenant under one `hotel_code`. Most of its complexity exists purely to
answer *"which property is the guest talking about?"* — and most of the Zone bugs in
`zoneplan.md` and the memory log are that machinery misfiring (Lansdowne-lock, Coimbatore
single-property lock, Mussoorie-under-Coimbatore, the COMPARE-rule city leak, chip cross-conversation
scope leak).

Mango Grove is **one property**. Every one of those failure modes is structurally impossible here,
and the work is correspondingly smaller — *provided we do not inherit the machinery*.

**Zone machinery to deliberately NOT switch on** (§2 has the specifics):

| Zone feature | Why Mango must not have it |
|---|---|
| Property scope state (`_active_property_ids`, `_past_property_context`, narrowing, pinning — `multiptorpty logic.md`) | Nothing to narrow between. Scope state that can drift is pure risk. |
| Location dropdown + 21 curated `new_bot_location_intros` rows | One location. The picker is a dead click. |
| Multi-property welcome message / intro location form | Guest already knows which hotel they are on. |
| Multi-property image facade (hardcoded) | Only relevant when choosing between properties. |
| COMPARE / PRIOR-NARROW / LOCATION GUARD router rules | These exist to stop cross-city leaks. No second city. |

**What Mango gets instead:** `property_count = 1`, so
[chat_service_response_mixin.py:1544-1560](services/chat_service_response_mixin.py#L1544-L1560)
takes the single-city branch and the bot introduces itself as *"…at Mango Grove Hotel in Chandigarh"*
rather than the group phrasing. That is the correct, simpler path — we just have to make sure the
tenant is genuinely configured as one property so the branch is reached.

**Sarovar is the closer precedent** for mechanism (single `hotel_code`, phase-gated, chips off,
services built per property) — but note Sarovar's `hotel_code` covers ~150 KB property files, so its
*scoping* behaviour is still multi-property. Mango is the first genuinely single-property tenant.
Follow Sarovar for **how**, not for **scope**.

---

## 1. Tenant isolation — do this first, and get it exactly right

This is the step that silently breaks everything downstream if it is wrong.

**Decision: `hotel_code = MANGO_GROVE_CHANDIGARH`** (the `x-hotel-code` header value).

Rationale: it must be unique and must not be a **prefix-family relative** of any existing code.
`resolve_hotel_code()` ([config_service.py:2368](services/config_service.py#L2368)) collapses a long
code onto a shorter "known" one when they share a prefix family. A bare `mango` is fine today, but
`MANGO_GROVE_CHANDIGARH` is safer if a second Mango Grove property is ever added.

### 1.1 The tenant-identity split trap (read this before creating anything)

The single most expensive bug in the Sarovar onboarding, per `postbookingskill.md §8`:

> "Pull from KB" extracts nothing (`no_kb_content`) even though the KB reindexed = tenant-identity
> split. `_discover_known_hotel_codes()` reads local `config/properties/*.json`, **not**
> `new_bot_hotels` — so a DB-only hotel is not "known" until a matching file exists, and a stray
> short-name file hijacks the long code.

Sarovar still carries the scar: `config/properties/sarovar_portico_sriperumbudur.json` has
`business.id = "sarovar_portico_sriperumbudur"` but `business.code = "sarovar"`, and there is a
`sarovar.json.bak` sitting next to it from when this was untangled.

**Therefore, for Mango, all four of these must be the same string:**

1. the file name — `config/properties/mango_grove_chandigarh.json`
2. `business.id` inside it
3. `business.code` inside it
4. the `hotel_code` on the `new_bot_hotels` row

**Do not create any short alias file** (no `mango.json`).

### 1.2 Setup checklist

- [ ] `new_bot_hotels` row: `code = MANGO_GROVE_CHANDIGARH`, name "Mango Grove Hotel, Chandigarh". Note the assigned `id` (Zone 67, Sarovar 68 — Mango will be the next).
- [ ] `config/properties/mango_grove_chandigarh.json` — copy the Sarovar file's shape, then set:
  - `business.id` / `business.code` / filename all `mango_grove_chandigarh`
  - `business.name` — "Mango Grove Hotel, Chandigarh"
  - `business.city` — "Chandigarh" (**a real city, not a group label** — this is what makes the single-city identity line fire)
  - `business.bot_name` — "Mango Grove Assist" (their onboarder answer)
  - `business.contact_phone` — `9815009635`, `contact_email` — `reservations@mangogrovehotel.com`
  - `business.website` — `www.mangogrovehotel.com`
  - `business.channels` — `{"web_widget": true, "whatsapp": false}`. **Phase 1 is web only** — the Sarovar template ships `whatsapp: true`, so this must be changed, not copied.
  - `knowledge_base.expected_property_count` — **1**
- [ ] `new_bot_properties` row: one property, name **"Mango Grove Hotel, Chandigarh"**. This is the name the bot *speaks* — it comes from this table, not the KB file.
- [ ] Restart backend (service/property caches are per-`hotel_code` and stale until restart — no `--reload`).

**Verify before moving on:** `GET /api/config/...` with `x-hotel-code: MANGO_GROVE_CHANDIGARH`
returns `property_count = 1` and the right business name. If `property_count` is 0 or 2+, stop and
fix — every later step inherits it.

---

## 2. Knowledge base

**Load the KB directly from the txt — do not run the LLM reindex.**

Per the Sarovar rebuild (2026-08-11): the reindex path runs an LLM book-renamer that produces
duplicate/renamed books (Sarovar went 24 → 20 books only after a direct rebuild). For a single
clean file we want deterministic books.

- [ ] Ingest `mango_grove_hotel_chandigarh_kb.txt` into `new_bot_kb_books` → `new_bot_kb_pages` →
      `new_bot_kb_book_pages` ([models/database.py:759-851](models/database.py#L759)) via ORM, one
      book per `=== Section ===`. Expect **10 books**: Property Overview, Rooms & Suites,
      Dining & Restaurants, Amenities & Facilities, Spa & Wellness, Meetings & Events,
      Policies & Rules, Location & Transport, Contact Information, Special Offers & Packages.
- [ ] Scope every book to the single property id.
- [ ] Restart (KB is DB-live but cached ~1h).

**Verify:** ask the bot one fact from each of the 10 books and confirm it answers from KB, not from
the model's general knowledge — particularly *"is there a pool?"* and *"do you have a spa?"*, where
the correct answer is a clean **No** (the hotel has neither).

---

## 3. Pre-booking service set

All `_PHASE_PREBUILT_SERVICES` lists are **empty** ([config_service.py:219-235](services/config_service.py#L219))
— every service is custom-created per property. Nothing is inherited; we build these three.

Grounded in `_PREBOOKING_TICKETING_CASES` ([config_service.py:120-129](services/config_service.py#L120))
and mapped onto the **three departments the hotel actually asked to be tagged** (Onboarder sheet 6:
*"Reservations, F&B, Banquets"*).

**Corrected after reading how Zone is actually built** (see [prebookingskill.md](prebookingskill.md)
§1 and §4). An earlier draft of this plan proposed six services. That was wrong: Zone runs its entire
pre-booking phase on **two or three**, because in pre-booking most guest asks are *questions* that the
`book` lane answers from the KB with no service and no ticket. Over-servicing pre-booking turns
informational turns into tickets and buries the hotel in noise.

| # | Service (`id`) | Mode | Accepted category | Their dept |
|---|---|---|---|---|
| 1 | Room Booking / Accommodation lane (`room_booking`) | text + booking button | `room booking request` | Reservations |
| 2 | Support Intake / Callback (`support_intake`) | form | `support request` | Reservations |
| 3 | Banquet, Event & Wedding Enquiry (`banquet_event_enquiry`) | form | `Business Centre` | Banquets |

Deferred: **Booking / Payment Issue** — book_answerer CORE RULE 10 already handles the
conversational side, and there is no real IBE yet (§7.4). Revisit once there is one.

Dropped from the earlier draft: *Rate / Quotation* folds into the room lane (router rule 2 sends all
room-rate asks there anyway); *Dining Enquiry* is a `book`-lane question, not a staff task — the hotel
has no pre-booking table-reservation process in their collateral, and F&B tagging can come from the
ticket category when one is genuinely raised.

**Never send a department name as a ticket category.** Categories are coerced onto a fixed accepted
set ([ticketing_service.py:1816](services/ticketing_service.py#L1816)); `"Reception Front Office"`
was rejected 9/9 in a real incident. Use the middle column above.

**Info-only — no service, no ticket** (KB answers): room types and sizes, in-room amenities,
policies, check-in/out, ID, payment methods, distances, nearby attractions, parking, gym,
"do you have a spa/pool" (No), and the *existence* of offers.

### Service specs

1. **Room Booking / Accommodation lane** — *Desc:* "Handles room, suite and accommodation questions and booking intent, whether the guest is exploring or ready to book. Answer room types (Premium City View, Premium Grove View), 190 sq. ft., 2+1 occupancy, king-or-twin, and in-room amenities from the KB. There is no rate in the KB and no live availability feed — never quote or estimate a price and never claim to check availability. Do not take or confirm the booking in chat: share the booking link and capture the guest's contact and stay dates. This lane does NOT handle dining, banquets, transport or any in-stay request." *Captures:* `full_name` · `phone` · `email` · `check_in` · `check_out` · `guests`.
   ⚠️ The closed-set sentence is load-bearing — router rule 2 warns that with few active services the router funnels unrelated asks into the only booking lane.
2. **Support Intake / Callback** — the onboarder's explicit escalation destination (9815009635, 24 hrs). *Desc:* "Handles guests who ask to speak to a person or be called back, and guests whose question the bot could not answer. Capture name and contact and raise a callback ticket. Support is available 24 hours. Do NOT open this service for a pre-checkin, during-stay or post-checkout operational request — those are declined on the book lane." *Form:* `full_name`(text,req) · `phone`(tel,req) · `email`(email,opt) · `requirement`(textarea,req).
3. **Banquet, Event & Wedding Enquiry** — *the highest-value service for this property.* 12,000 sq. ft. across 4 halls plus an open lawn, up to 400 guests, and the hotel named banquets as a secondary bot objective. *Desc:* "Handles banquet, conference, wedding and event enquiries. State from the KB what exists — 12,000 sq. ft. of banqueting space divided into 4 halls and an open lawn, up to 400 guests, projector/AV support, in-house catering, weddings hosted. Individual hall names, per-hall capacities and pricing are NOT in the KB — never invent them; capture the requirement and raise a Banquets ticket." *Form:* `full_name`(text,req) · `phone`(tel,req) · `email`(email,req) · `event_type`(text,req) · `event_date`(date,req) · `guest_count`(number,req) · `requirement`(textarea,opt).

**Per service:** Phases tab → Active Phase `pre_booking` → Create → fill → Save → **Pull from KB** →
**Regenerate → Save Prompt**.

- The `description` is a **routing mini-prompt**, not marketing copy — it decides routing.
- Always **Pull from KB**; never hand-type facts into `extracted_knowledge`.
- **Pull-from-KB can take ~276 s** and the button stays disabled throughout — it is not hung. Watch `logs/pull_from_kb_debug.txt`; `source_count=0` means wrong scope, go back to §1.
- The service's real prompt is the DB column `new_bot_services.generated_system_prompt`, **not** `_build_service_system_prompt`. Editing the override column **locks** auto-regeneration — lock deliberately, and lock *first*, then save.

---

## 4. Bot persona and widget config (all from the onboarder)

| Field | Value | Sheet |
|---|---|---|
| Bot name | Mango Grove Assist | 4. Functional Preferences |
| Tone | Formal and polite | 4 |
| Greeting | "Sir/Madam. How can I assist you today?" | 4 |
| Lead capture | Name, Mobile Number, Email | 4 |
| Languages | English, Hindi, Punjabi | 4 |
| Support hours | 24 hrs | 4 |
| Consent | "By continuing, you agree to our Privacy Policy." | 4 |
| Fallback | "I'm not sure about that — let me connect you to our team." | 4 |
| Escalation | After 1 failed response → 9815009635 | 2. BOT Goals |
| Primary colour | `#003F34` | 1. Basic Info |
| Accent colour | `#F79558` | 1 |
| Placement | Right-hand side, centre | 1 |

**Three things to raise with the hotel before go-live** (§7).

**Lead form gotcha:** the opening lead form must carry the `lead_source` marker, or it creates a
spurious `room_booking` ticket on open — this was a real bug (`project_opening_lead_form_no_ticket`).
Separately, explicit room-booking intent raises a **silent** background ticket
(`_create_silent_room_booking_ticket_bg`, `zoneplan.md:312`) gated on `target=room_booking_request`
plus a yes/no micro-classifier — confirm that fires once, not per turn, for Mango.

**Suggestion chips:** Sarovar has them off
(`CHAT_SUGGESTION_CHIPS_DISABLED_HOTELS_JSON=["sarovar_portico_sriperumbudur"]`). For a
single-property pre-booking bot chips are genuinely useful (room types, banquets, offers, location)
and carry none of the cross-property leak risk that made them a problem on Zone. **Recommend: leave
chips ON**, and confirm with the hotel.

---

## 5. Phase gating — pre-booking only

Only `pre_booking` is in scope. The other three phases must be **inactive** for this tenant, so an
in-stay ask is declined rather than half-served.

Follow `PREBOOKING_PHASE_FIXES_PLAN.md`'s approach, which is also the standing rule
(`feedback_no_deterministic_keyword_guards`: keyword guards scored 5/7, deleting them scored 7/7):

> Do NOT hardcode word lists. Give the model the **identity + the phase principle + ONE worked
> example per case**, and let it reason.

The `=== WHO YOU ARE (pre-booking) ===` block for Mango:

- **Identity:** you help guests explore Mango Grove Hotel, Chandigarh and book a room, a table, or an event. You cannot perform any in-stay or pre-arrival service yourself.
- **During-stay ask** (housekeeping, room service delivery, "my AC is broken"): decline as an in-stay thing — the guest has not checked in.
- **Pre-arrival ask** (airport pickup, early check-in): the KB says both exist; answer *that they exist*, then "that's arranged once your booking is confirmed."
- **Decision-affecting gap** (rate, offer terms, hall names, restaurant hours): say you don't have it **and offer the team handoff** — do not dead-end. This is the D1 rubric, and with 13 KB gaps it will fire often.
- **No HTML tags** in output — `<br>` leakage was a real 5.4 defect.

Also carries over from Sarovar: the universal out-of-scope guardrail (jokes/math decline,
self-harm → professional help) and the "no emoji" styling contract if the hotel wants the formal
tone they asked for.

---

## 6. Ticketing

- [ ] Tickets must be judged **against the external ticketing API** — a `LOCAL-*` ref means the test was invalid (`feedback_tickets_always_external_api`).
- [ ] The property needs an **FMS entity id** provisioned by GHN ops (5 tables: `GHN_ORGANIZATION`/`GHN_FF_ENTITY`, `GHN_SURVEY_FMS_RI_ENTITY_MAPPING`, `DEPARTMENT_STAFF_CONFIG`, `GHN_FF_TICKET_NOTIFICATIONS`, `GHN_DEPARTMENT_ALERT_MAPPINGS`). **This cannot be done from the bot side and the entity must exist first.** Sarovar shipped without it and its tickets never reached the external dashboard.
- [ ] Then add to `.env` `TICKETING_PROPERTY_ENTITY_MAP_JSON` — the map is keyed by **location**, not hotel_code:
  ```json
  {"zone": { …existing… },
   "sarovar_portico_sriperumbudur": {"sriperumbudur": "6311"},
   "mango_grove_chandigarh": {"chandigarh": "<FMS_ID>"}}
  ```
  Restart to load. Verify via `GET /api/admin/api/tickets` with `x-hotel-code`.
- [ ] Guest name on the ticket comes from PMS room→name lookup; with no PMS integration at pre-booking, the **lead form is the only name source** — so the forms in §3 must all capture `full_name` (they do).

---

## 6a. Client requirements found in the onboarding pack

A sweep of every mango-folder file specifically for *requirements* (not KB facts) turned up five
things that are asked for but not currently planned or possible.

1. **"Booking Engine Integration: YES — Real time"** (Onboarder sheet 5). The client is asking for **live rate and availability lookup**. This is **out of scope** per the signed Zone SOW (*"Booking / Reservation services — ✘ Out of Scope"*) and there is no IBE integration in the product. It also contradicts §3's room lane, which must never quote a rate. **This needs an explicit commercial decision before go-live** — either the client accepts booking-engine deflection (the Zone model), or real-time lookup becomes a scoped integration project. Do not let it leak into a service description in the meantime.
2. **WhatsApp — DEFERRED, not in this phase.** Phase 1 is the **web widget only**. `KePSLA - WhatsApp - On-Boarding.docx` is already fully filled (API number `9815029465`, Meta Business Manager under `mangogrove2023@gmail.com`, GST and incorporation certificates attached, display name "Mango grove hotel", Blue Tick requested), so nothing is lost by parking it — recorded here only so it is not re-collected later. Set `channels.whatsapp = false` in the property config (§1.2) so the tenant does not half-enable a channel we are not building. When it is picked up: it carries its own Meta dependencies, and Blue Tick is **not guaranteed** (Meta requires 5 organic media mentions in 12 months).
3. **Languages: English, Hindi and Punjabi** (sheet 4). The book_answerer already mirrors the guest's language per CORE RULE 2, but **Punjabi has never been tested on this stack.** Add Hindi and Punjabi turns to the 50-turn run rather than assuming.
4. **Daily reporting and daily unhandled-query review** (sheets 6 and 7) — email summary + dashboard to `ed@` and `gmsales@`; unhandled queries to `fom@`. Confirm the reporting job can run daily for this tenant; the reviewer cadence is also the mechanism that will close the 13 KB gaps.
5. **Two claimed attachments are missing from the folder.**
   - **Department Contact Sheet** — sheet 8 marks it "Attached / Received", but there is **no such file**. This is the sheet that maps departments to people, phones and emails for escalation, so ticket routing has no named owners. Less urgent with WhatsApp deferred, but still needed before tickets mean anything. **Request it.**
   - **Brochures** — sheet 3 names *"Brochures and website"* as knowledge sources. No brochure was sent. If brochures exist they likely close several of the 13 gaps (hours, Wi-Fi, hall names).

Also noted: **Department-wise FAQs** and **Special/Seasonal FAQs** are both blank/Pending on sheet 3,
and **Website Script Placement** and **IT POC email** are Pending on sheet 5 — the last two block
deployment, not training, but block it hard.

---

## 7. Open items to settle with the hotel

Blocking-ish, in priority order:

1. **The 13 KB gaps** (gap report, red rows). The ones that will actually hurt a pre-booking bot: **Wi-Fi (does it exist / is it free)**, **restaurant and bar operating hours**, **laundry**, **lift**, **doctor on call**, **24-hr room service**, and the **Google Maps link**. Wi-Fi is the worst — a business hotel bot that cannot answer "do you have Wi-Fi?" looks broken.
2. **Offer terms and validity.** All four offers (direct-booking discount, early-bird, Mango Grove Rewards Program, seasonal/festive) are in the KB as *"yes, this exists"* with **no discount amount, terms or dates** — that cell is blank in their FAQ. A bot that says "yes we have a discount" and cannot say what it is generates a callback ticket every time.
3. **Escalation after 1 failed response is too aggressive.** With 13 gaps, a 1-strike rule routes a large share of conversations to a human immediately. Recommend 2, or fill the gaps first.
4. **Booking Engine URL is the website homepage**, not an IBE deep link. If there is a real booking engine, we want that URL so the bot can hand off with dates pre-filled.
5. **46 of 64 FAQ rows are still marked "Pending - confirm with hotel"** — including check-out time, cancellation policy, extra bed cost, child age and the ID list. These are load-bearing policy answers.
6. **Star rating, room rates, individual banquet hall names, Alfresco timings, reception hours** — the 5 extra gaps beyond the Zone template.
7. Spelling confirmations: their sheet says **"Sukna lake"** and **"Elante Hall"** (likely Sukhna Lake / Elante Mall). Kept verbatim in the KB pending their answer.

---

## 8. Sequence

| Step | Depends on | Verify by |
|---|---|---|
| 1. Tenant + property + properties.json (§1) | — | `property_count = 1`, correct business name |
| 2. KB direct-load, 10 books (§2) | 1 | One fact per book answered from KB; pool/spa answer "No" |
| 3. Phase config: `pre_booking` active only (§5) | 1 | In-stay ask declined with the identity line |
| 4. Six services + Pull from KB + Regenerate (§3) | 2 | `source_count > 0` in `pull_from_kb_debug.txt` |
| 5. Persona, colours, greeting, lead form (§4) | 1 | Widget renders `#003F34`, greeting matches |
| 6. FMS entity id + env map (§6) | GHN ops | Ticket visible in external API, ref is not `LOCAL-*` |
| 7. 50-turn unattended run | 4, 5 | §9 |

Steps 1-5 are ours. **Step 6 is blocked on GHN ops** and should be requested now, in parallel — it
was the long pole on Sarovar.

---

## 9. Verification

Use the standalone Playwright harness (`reference_standalone_playwright_overnight_harness.md`):
drive the real widget, wait on the `Messages: N` counter (not panel text), flush every turn to disk.

Per-service checklist: info question → KB answer, **no ticket** · action → anchor captured → ticket
created in the expected accepted category · gap question → "I don't have that" **plus** a team-handoff
offer, never an invention · wrong-phase ask → declined with the identity line.

**Single-property regression set** (cheap, and it is the whole point of the isolation work):

- "which is your best property" / "do you have a hotel in Delhi" → must answer about Mango Grove only, never enumerate or invent siblings
- No location picker, no property-choice chips, no "please choose a location" welcome
- Bot self-intro says *"at Mango Grove Hotel in Chandigarh"* (single-city branch), not the group phrasing

**Pin the grounding traps** — every one of these has burned us before:

- "how much is a room?" → no invented rate
- "what are your banquet halls called?" → no invented hall names (the KB has the 12,000 sq. ft./4 halls/400 pax figures but **no names**)
- "what time does the restaurant open?" → no invented hours (breakfast 7:00–10:30 is the *only* timing in the KB)
- "do you have Wi-Fi?" → currently a gap; must handoff, not assume
- "what's the discount?" → offers exist, terms do not; must handoff

Verify in **both** the harness page and the `?embed=1` iframe, at phone and laptop widths — they are
the same component (`admin_ui/src/pages/ChatHarness.tsx`), so a change lands in both and must be
checked in both.

---

## 10. `prebookingskill.md` — written

`duringstayskill.md` and `postbookingskill.md` existed; **`prebookingskill.md` did not** — the one
phase Mango actually needs. `postbookingskill.md` explicitly scopes pre-booking out.

It has now been written from the Zone implementation: [prebookingskill.md](prebookingskill.md).
It carries the mechanism, the `book` vs `service` routing model, the SOW scope table, the canonical
service set, design rules, the single- vs multi-property split, verification and gotchas — with
Mango as its worked example (§9). Keep it updated as this build teaches us more, per the §10
convention in that file.
