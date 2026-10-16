# Post‑Booking Training Skill (master)

> **How to use this file:** When you need to train a property for **post‑booking** — everything after a
> booking is confirmed — tell Claude:
> *"Refer to `postbookingskill.md` and set up post‑booking services for <property>."*
>
> **Post‑booking = 3 phases:** `pre_checkin` → `during_stay` → `post_checkout`.
> (`pre_booking` is the only *pre*‑booking phase and is out of scope here.)
>
> The **during‑stay** chapter has extra field‑by‑field detail in `duringstayskill.md`. This master reuses
> the same mechanism, routing, and design rules for all three phases.
> Last verified against the codebase: 2026‑07‑03.

---

## 1. The mechanism (shared across all phases)

- **No prebuilt catalog exists** for any phase — `_PREBOOKING/_PRECHECKIN/_DURINGSTAY/_POSTCHECKOUT_PREBUILT_SERVICES = []` in [services/config_service.py:218‑235](services/config_service.py#L218). **Every service is custom‑created per property.**
- Create/edit in the admin UI: **Bot Training → Phases tab** ([admin_ui/src/components/bot-training/PhasesTab.tsx](admin_ui/src/components/bot-training/PhasesTab.tsx)) → set **Active Phase** to the target phase → *"Create New Service For This Phase"*.
- Admin API (all need the `x-hotel-code` header):
  - `POST /api/config/services` (create) · `PUT /api/config/services/{id}` (update / map to phase via `{"phase_id":"<phase>"}`) · `DELETE …/{id}`
  - `POST /api/config/service-kb/preview-extract` ("Pull from KB") · `POST …/{id}/regenerate-prompt` · `PUT …/{id}/prompt`
- **A service's `description` is a routing mini‑prompt** — write it as an instruction to the bot, not marketing copy.

### Service payload schema

```jsonc
{
  "id": "booking_modification",
  "name": "Booking Modification",
  "type": "service",                    // service | department | workflow | plugin
  "description": "…routing mini-prompt…",
  "phase_id": "pre_checkin",            // pre_checkin | during_stay | post_checkout
  "is_active": true,
  "is_builtin": false,
  "ticketing_enabled": true,            // = (ticketing_mode != "none")
  "ticketing_mode": "form",             // none | text | form
  "ticketing_policy": "…when to raise a ticket…",
  "service_prompt_pack": {
    "source": "manual_override", "generator": "admin_ui", "version": 1,
    "extracted_knowledge": "…pulled from KB…",
    "ticketing_conditions": "…same as ticketing_policy…"
  },
  "form_config": {                      // ONLY when ticketing_mode == "form"
    "trigger_field": { "id": "…", "label": "…", "description": "…" },
    "fields": [ { "id": "…", "label": "…", "type": "text",
                  "required": true, "validation_prompt": "" } ],
    "pre_form_instructions": "…when to show the form…"
  }
}
```

- `ticketing_mode`: `none` (info only) · `text` (bot collects details in chat, then tickets) · `form` (structured form).
- Form field `type`: `text | date | time | number | tel | email | textarea`.

---

## 2. Departments & ticket routing (non‑obvious — read once)

Services have **no department field**. Departments live only on the *ticket*. A ticket's category is coerced onto a **fixed accepted set** ([services/ticketing_service.py:1816](services/ticketing_service.py#L1816)); outside values are remapped or the ticket is rejected (real bug: `"Reception Front Office"` rejected 9/9 → use `"Front Office"`).

**Accepted categories:** `Housekeeping`, `In-Room Dining`, `Front Office`, `Cashiering Billing`, `spa booking`, `airport transfer`, `Business Centre`, `Guest Relations`, `complaint`, `technical_support`, `support request`, `room booking request`.
**KePSLA's 4 departments:** `Reception Front Office`, `Guest Relations`, `Housekeeping`, `In-Room Dining`.

| Real‑world dept | Accepted category |
|---|---|
| Housekeeping / Linen / Laundry | `Housekeeping` |
| Maintenance / Engineering | `technical_support` |
| In‑Room Dining / Room Service / Restaurant / F&B | `In-Room Dining` |
| Spa / Wellness | `spa booking` |
| Transport / Airport / Railway / Local | `airport transfer` |
| Concierge / Front desk / Wake‑up / Luggage / Parking / Valet | `Front Office` |
| Meeting / Banquet / Business services | `Business Centre` |
| Reservations changes / status / early check‑in | `Front Office` |
| Billing / Invoice / Refund / Deposit / GST | `Cashiering Billing` |
| Complaint / dissatisfaction | `complaint` |
| Loyalty / membership / misc follow‑up | `support request` |
| Rebooking / new booking callback | `room booking request` |

**The classifier is already primed for all 4 phases** — `_PRECHECKIN/_DURING_STAY/_POSTCHECKOUT_TICKETING_CASES` ([config_service.py:130‑163](services/config_service.py#L130)) already enumerate these intents. You're giving them dedicated services + forms + KB, not teaching from scratch.

---

## 3. Design rules (apply every phase)

1. **Hybrid capture mode.** `text` for quick asks; `form` where structured data matters (transport, setup, refund, GST, lost‑and‑found, etc.).
2. **Anchor identity per phase.** During‑stay → **room number** mandatory. Pre‑checkin / post‑checkout → **booking reference (or name + dates)** mandatory (guest isn't in a room). Force it in Ticketing Conditions (text) or as a required field (form).
3. **Ground in the KB — never fabricate.** After creating each service, **Pull from KB** so `extracted_knowledge` is real. Missing info → "not available", not invented.
4. **Encode closed sets** in the `description` (e.g. cancellation policy, what pre‑arrival setups are actually offered, spa closed‑set).
5. **Info vs. action split.** Policies/timings/FAQ stay **KB answers, no service, no ticket**. Only create a service where the ask becomes a **staff task**.
6. **`description` = instruction to the bot**, telling it what it handles, to answer from KB first, and when to raise a ticket.

---

## 4. Phase A — PRE‑CHECKIN  (`phase_id: pre_checkin`)

> "Guest booking is confirmed and needs support before arrival." Anchor = **booking reference / name + dates**.
> Grounded in `_PRECHECKIN_TICKETING_CASES` ([config_service.py:130](services/config_service.py#L130)).

| # | Service (`id`) | Type | Mode | Routes to |
|---|---|---|---|---|
| 1 | Booking Status / Confirmation (`booking_status`) | service | text | Front Office |
| 2 | Booking Modification (`booking_modification`) | service | form | Front Office |
| 3 | Booking Cancellation (`booking_cancellation`) | service | form | Cashiering Billing |
| 4 | Early Check‑in Request (`early_checkin`) | service | text | Front Office |
| 5 | Arrival Transport / Airport Transfer (`arrival_transport`) | service | form | airport transfer |
| 6 | Pre‑arrival / Special Occasion Setup (`prearrival_setup`) | service | form | Guest Relations |
| 7 | Document / ID Verification (`document_verification`) ⚠️ only if hotel collects pre‑arrival docs | service | text | Front Office |

**Specs**

1. **Booking Status** — *Desc:* "Handles booking status/confirmation lookups for guests who have already booked. Confirm details from booking context/KB. If the booking can't be found or details mismatch, capture name + booking reference and raise a Front Office ticket for manual verification." *Conditions:* "Raise a ticket when a booking cannot be verified or details mismatch. Capture guest name and booking reference/dates."
2. **Booking Modification** — *Desc:* "Handles changes to a confirmed booking (dates, room type, guest count, name). Requires staff/PMS update; collect the change and raise a ticket." *Form:* `full_name`(text,req) · `booking_reference`(text,req) · `requested_change`(textarea,req) · `phone`(tel,req).
3. **Booking Cancellation** — *Desc:* "Handles cancellation of a confirmed booking and refund follow‑up. State the cancellation policy from the KB first, then collect details and raise a ticket for manual cancellation/refund." *Form:* `full_name`(text,req) · `booking_reference`(text,req) · `reason`(textarea,opt) · `phone`(tel,req). *KB:* cancellation/refund policy.
4. **Early Check‑in** — *Desc:* "Handles early check‑in requests. Requires availability check + approval; capture booking reference and expected arrival time and raise a Front Office ticket." *Conditions:* capture booking ref + arrival time.
5. **Arrival Transport** — *Desc:* "Arranges airport/railway pickup before arrival; requires transport coordination." *Form:* `full_name`(text,req) · `booking_reference`(text,req) · `arrival_date`(date,req) · `flight_train_no`(text,opt) · `pickup_location`(text,req) · `arrival_time`(time,req). *KB:* transport availability/charges.
6. **Pre‑arrival / Occasion Setup** — *Desc:* "Handles pre‑arrival special requests and occasion setups (anniversary/birthday decoration, cake, room preferences, amenity setup). Only offer what the KB/hotel supports; collect details and raise a Guest Relations ticket for operations." *Form:* `full_name`(text,req) · `booking_reference`(text,req) · `occasion_type`(text,opt) · `requests`(textarea,req) · `arrival_date`(date,req). *KB:* what setups/amenities are offered.
7. **Document Verification** *(optional)* — *Desc:* "Handles pre‑arrival ID/document submission or validation issues. If validation fails or needs override, raise a Front Office ticket."

---

## 5. Phase B — DURING‑STAY  (`phase_id: during_stay`)

> "Guest is in‑house." Anchor = **room number (mandatory everywhere)**.
> Full field‑by‑field spec lives in **`duringstayskill.md`**. Summary:

| # | Service (`id`) | Type | Mode | Routes to |
|---|---|---|---|---|
| 1 | In‑Room Dining (`in_room_dining`) | service | form | In‑Room Dining |
| 2 | Housekeeping (`housekeeping`) — cleaning+linen+bath amenities | department | text | Housekeeping |
| 3 | Laundry (`laundry`) | service | text | Housekeeping |
| 4 | Maintenance / Engineering (`maintenance`) | department | text | technical_support |
| 5 | Spa Booking (`spa_booking`) ⚠️ closed‑set | service | form | spa booking |
| 6 | Transportation (`transport`) | service | form | airport transfer |
| 7 | Restaurant Reservation (`restaurant_reservation`) | service | form | In‑Room Dining |
| 8 | Concierge / Front Office (`concierge`) | department | text | Front Office |
| 9 | Meeting & Event Assistance (`meeting_banquet`) | service | form | Business Centre |
| 10 | Parking / Valet (`parking_valet`) | service | text | Front Office |

Closed‑set / grounding notes (Spa = massage/steam/sauna only; housekeeping amenity list; maintenance items; outlets SPR Kitchen / Chola Lounge; verify valet+EV) → see `duringstayskill.md §8`.

---

## 6. Phase C — POST‑CHECKOUT  (`phase_id: post_checkout`)

> "Guest has checked out and needs follow‑up." Anchor = **booking reference / folio + contact (phone/email)**.
> Grounded in `_POSTCHECKOUT_TICKETING_CASES` ([config_service.py:154](services/config_service.py#L154)).

| # | Service (`id`) | Type | Mode | Routes to |
|---|---|---|---|---|
| 1 | Invoice / Billing Clarification (`billing_clarification`) | service | text | Cashiering Billing |
| 2 | Refund / Deposit / Charge Reversal (`refund_request`) | service | form | Cashiering Billing |
| 3 | Lost & Found (`lost_and_found`) | service | form | Front Office |
| 4 | Post‑stay Complaint / Feedback (`post_stay_feedback`) | service | text | complaint → Guest Relations |
| 5 | Tax Invoice / GST Correction (`gst_invoice`) | service | form | Cashiering Billing |
| 6 | Loyalty / Membership Support (`loyalty_support`) | service | text | support request |
| 7 | Folio / Receipt Resend (`folio_resend`) | service | text | Cashiering Billing |
| 8 | Rebooking Callback (`rebooking_callback`) | service | text | room booking request |

**Specs**

1. **Billing Clarification** — *Desc:* "Handles questions about the final invoice/bill after checkout. Explain charges from available data; if it needs finance/front‑office follow‑up, capture folio/booking reference and raise a Cashiering Billing ticket."
2. **Refund / Deposit / Reversal** — *Desc:* "Handles refund, security‑deposit return, or charge‑reversal requests after checkout; requires manual verification." *Form:* `full_name`(text,req) · `booking_reference`(text,req) · `amount`(number,opt) · `reason`(textarea,req) · `phone`(tel,req) · `email`(email,opt).
3. **Lost & Found** — *Desc:* "Handles lost‑item reports after checkout; collect item, where/when, and return contact, then raise a ticket for item search and courier coordination." *Form:* `full_name`(text,req) · `item_description`(textarea,req) · `room_or_area`(text,opt) · `stay_dates`(text,opt) · `contact_phone`(tel,req) · `return_address`(textarea,opt).
4. **Post‑stay Complaint / Feedback** — *Desc:* "Handles complaints or negative feedback after checkout for service recovery. Acknowledge, capture details, and raise a complaint ticket to Guest Relations." *Conditions:* capture booking ref + issue.
5. **GST / Tax Invoice Correction** — *Desc:* "Handles GST/tax‑invoice correction or reissue after checkout." *Form:* `full_name`(text,req) · `booking_reference`(text,req) · `gst_number`(text,req) · `company_name`(text,req) · `correction_details`(textarea,req).
6. **Loyalty / Membership Support** — *Desc:* "Handles loyalty‑points or membership‑benefit corrections after stay. Capture membership id and the issue; raise a support ticket."
7. **Folio / Receipt Resend** — *Desc:* "Handles requests to resend stay confirmation, folio copy, or receipt. If auto‑resend isn't possible, capture email and raise a ticket." *Conditions:* capture booking ref + email.
8. **Rebooking Callback** — *Desc:* "Handles guests who want to book again after checkout. Capture preferred dates and contact, and raise a sales/reservations callback ticket."

---

## 7. Procedure & verification (all phases)

**Prerequisites**
- [ ] KB reindexed (DB is source of truth). ✅ done for the current property.
- [ ] Property **`hotel_code`** (the `x-hotel-code`) and configured name known.
- [ ] Property **FMS entity id** in `ticketing_property_entity_map_json` — else tickets save to DB + KePSLA but the external Kepsla API stays unmapped.

### Current property (resolved 2026‑07‑03)
- **new_bot hotel** `id=68`, `code=SAROVAR_PORTICO_SRIPERUMBUDUR` → **`x-hotel-code` = `SAROVAR_PORTICO_SRIPERUMBUDUR`**.
- **new_bot property** `id=343`, slug `sarovar_portico_sriperumbudur`, name "Sarovar Portico Sriperumbudur" (Kanchipuram / Sriperumbudur, TN), KB `sarovar_portico_sriperumbudur_kb.txt`.
- **FMS entity id: DOES NOT EXIST YET.** This property is **not provisioned** in the GHN FMS/RI ticketing system. Verified read‑only:
  - No `Sarovar Portico Sriperumbudur` in `GHN_ORGANIZATION` / `GHN_FF_ENTITY`. The only Sriperumbudur orgs (Citrus=7, Sathyam Grand Resort=5994, Fairfield=7568) have **no** `GHN_SURVEY_FMS_RI_ENTITY_MAPPING` row and **no** `DEPARTMENT_STAFF_CONFIG` staff — so there is nothing to borrow.
  - Method sanity‑checked against Zone (FMS `6290`↔RI `7256` = Kolkata w/ 5 managers; FMS `6295`↔RI `7728` = Jaipur).
- **Consequence:** during‑stay/pre‑checkin/post‑checkout tickets will save to `new_bot_tickets` + KePSLA (internal) but **cannot route to the external Kepsla FMS dashboard** per‑property, and the `LUMIRA_DEPARTMENTS_ENABLED` live‑department lookup returns empty → the bot falls back to the accepted‑category set (fine).

### KB‑grounded service set for this property (23 total) — verified against `sarovar_portico_sriperumbudur_kb.txt`
- **During‑stay: all 10 confirmed.** Parking/Valet/EV all present (earlier ⚠️ resolved). Spa closed‑set confirmed: massage/steam/sauna/Asian therapies only — jacuzzi/facial/hair/mani‑pedi explicitly NOT available. Outlets: SPR Kitchen (06:30–22:30), Chola Lounge bar (11:00–23:00).
- **Pre‑checkin: create 6, drop Document Verification** (no KB support).
- **Post‑checkout: create 7, drop Loyalty/Membership** (no loyalty program in KB).
- **Policy‑gap rule:** the factsheet has `# TODO` for cancellation, deposit, payment types, pets, early check‑in/late check‑out, extra bed. Services touching these (Booking Cancellation, Early Check‑in, Refund/Deposit) must **capture + ticket and never quote a policy** ("our team will confirm"). Ideal fix: hotel fills the KB TODOs.
- **No priced menu in KB:** In‑Room Dining / Restaurant answer generally + take the request; upload the real menu via Menu OCR for true ordering.

### Build log (live) — pre‑checkin
- **`early_checkin` (Early Check‑in Request) — BUILT + LIVE‑VERIFIED 2026‑07‑06.** service · **text** mode · Front Office routing · created via admin UI (Playwright). KB‑grounded via Pull‑from‑KB (the only relevant fact is **Check‑in 15:00**; early‑check‑in policy/charge is a KB `# TODO`, so it is **capture‑and‑ticket, never quotes a policy/fee**). Includes the exact front‑desk fallback line: *"I don't have the pricing information right now, but our front desk team can help with that. Shall I connect you with them?"* Verified with two live turns (see testing note below): (1) "can I check in early at 11am?" → captures booking ref/name+date + expected arrival time, states 3 PM, "Front Office will confirm and get back to you", no invented policy; (2) "how much for early check‑in?" → the fallback line, no fabricated price.
- **GOTCHA — Pull‑from‑KB can be very slow.** On 2026‑07‑06 the `preview-extract` LLM call took **~276 s** (returned 200; the button stays disabled the whole time — it is NOT hung). Regenerate was ~30 s. Be patient / poll the Knowledge Input textarea; confirm completion in `logs/pull_from_kb_debug.txt` and the `new_detailed.log` HTTP duration.
- Still pending in pre‑checkin per the client sheet: special requests / occasion setup, booking status/mismatch, modification, cancellation, payment issue (all capture‑and‑ticket; policy answers blocked on the KB `# TODO`s).

### How to map the FMS id once it's created (GHN/Kepsla ops task)
1. GHN ops creates the entity → it gets an **FMS_Entity_Id** (Zone's are `62xx–63xx`) + an **RI** `GHN_ORGANIZATION` id, a row in `GHN_FEEDBACK_RESULTS.GHN_SURVEY_FMS_RI_ENTITY_MAPPING (FMS_Entity_Id, RI_Entity_Id)`, `DEPARTMENT_STAFF_CONFIG` managers (`ORG_ID = RI id`, `MANAGER=1`), and the alert rows (`GHN_FF_TICKET_NOTIFICATIONS`, `GHN_DEPARTMENT_ALERT_MAPPINGS` keyed by `FMS_ENTITY_ID`). *(These 5 tables are the ones to fill — they cannot be written from the bot side and the entity must exist first.)*
2. Then add the tenant to **`TICKETING_PROPERTY_ENTITY_MAP_JSON`** in `.env` (key = `hotel_code` lowercased; inner key = a lowercase substring of the property name, longest match wins). Merge alongside the existing `zone` entry:
   ```json
   {"zone": { …existing… }, "sarovar_portico_sriperumbudur": {"sriperumbudur": "<FMS_ID>"}}
   ```
   Restart the backend to load. Verify a ticket routes via `GET /api/admin/api/tickets` with `x-hotel-code: SAROVAR_PORTICO_SRIPERUMBUDUR`.

**Per service:** Phases tab → pick phase → Create → fill fields (+ Form Builder if `form`) → **Save** → **Pull from KB** → **Regenerate → Save Prompt** → confirm it shows under *Services In This Phase* with *Ticketing On* and the right mode.

**Testing a phase:** the session must be in that phase — check the routing‑log phase. Fresh sessions default to `during_stay`; for `pre_checkin` / `post_checkout` drive the session into the right phase (booking dates resolve the phase) and verify the log before trusting results. **Reproducible HTTP test** (no widget needed): `POST /api/chat/message` with header `x-hotel-code: <CODE>` and body `{"session_id":"<new>","hotel_code":"<CODE>","channel":"web","message":"<ask>","metadata":{"phase":"pre_checkin"}}` — `metadata.phase` forces the phase. Use `-m 200` (LLM turns can be slow).

**Verification checklist (per service):** info Q → KB answer, no ticket · action → anchor (room# or booking ref) captured → ticket created · ticket lands in expected **accepted category** (§2), visible via `GET /api/admin/api/tickets` with `x-hotel-code` · closed‑set exclusions refused · routing‑log phase correct.

---

## 8. Gotchas / lessons

- **Never send a department name as a ticket category** — coerce to the accepted set (§2). `"Reception Front Office"` → `"Front Office"`.
- **FMS mapping:** a ticket can show on KePSLA yet be missing from the external API page if the property's entity isn't mapped — add it.
- **Restart to load:** service prompt/description edits sometimes need a backend restart to take effect.
- **Don't hand‑type KB facts** — always Pull from KB so grounding stays truthful and tracks KB changes.
- **Prompt rules are recency‑sensitive** — a rule placed late in a prompt wins; check placement before rewriting.
- **Wrong phase = false decline** — an action asked in the wrong phase gets declined; always confirm the routing‑log phase when testing.
- **"Pull from KB" extracts nothing (`no_kb_content`) even though the KB reindexed** = tenant‑identity split. The KB is stored in the DB under the *full* hotel code (e.g. `sarovar_portico_sriperumbudur`, hotel 68), but a stray `config/properties/<short>.json` (e.g. `sarovar.json`, business named just "sarovar") makes `<short>` a "known code". `resolve_hotel_code()` then collapses the full code onto the short one via a prefix‑family match ([config_service.py:2368](services/config_service.py#L2368)), and the short scope has no KB. **Diagnose** via `logs/pull_from_kb_debug.txt` — look at `active_property=` and `source_count=` (0 = wrong scope). **Fix**: make the local property file name AND its internal `business.id` equal the real hotel code, and retire the stray short‑name file (rename to `.bak`). Note: `_discover_known_hotel_codes()` reads local `config/properties/*.json`, NOT `new_bot_hotels`, so a DB‑only hotel isn't "known" until a matching file/business.id exists. Also: services created under the wrong scope never persist to the real hotel — re‑create them after fixing the scope.

---

## 9. Convention for future skills

When we train something well, capture it as one self‑contained `*skill.md`: shared mechanism + routing + design rules, then phase/topic service specs with a reusable template, a worked example, and gotchas — all grounded in real code/endpoint references. Keep them together (`duringstayskill.md`, `postbookingskill.md`, …) so *"refer to `<x>skill.md` and do it"* always works.
