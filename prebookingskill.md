# Pre‑Booking Training Skill (master)

> **How to use this file:** When you need to train a property for **pre‑booking** — everything before
> a reservation exists — tell Claude:
> *"Refer to `prebookingskill.md` and set up pre‑booking for <property>."*
>
> **Pre‑booking is the one phase `postbookingskill.md` deliberately excludes.** It is not a smaller
> version of during‑stay — it inverts the mechanism. Read §1 before creating any service.
>
> Companion files: `postbookingskill.md` (pre_checkin → during_stay → post_checkout),
> `duringstayskill.md` (field‑by‑field during‑stay), `multiptorpty logic.md` (property scope state).
> Reference tenant: **Zone Hotels** (24 properties, live). Worked example: **Mango Grove Chandigarh** (§9).
> Last verified against the codebase: 2026‑09‑04.

---

## 1. The one thing that makes pre‑booking different

**In post‑booking phases, almost every guest ask is a staff task → you build a service for it.**
During‑stay has 10 services, post‑checkout has 8.

**In pre‑booking, almost every guest ask is a question → it is answered from the KB with no service
and no ticket.** Zone runs the whole pre‑booking phase on **two or three** services.

Routing is decided by the main router
([prompts/models/gpt-5.4-mini/orchestrator.main_router.md](prompts/models/gpt-5.4-mini/orchestrator.main_router.md)),
which picks exactly one route:

- **`book`** = answer from hotel knowledge/policy → [orchestrator.book_answerer.md](prompts/models/gpt-5.4-mini/orchestrator.book_answerer.md)
- **`service`** = hand to an active service agent

> Router rule 1: *"`book` = answer from hotel knowledge/policy."*
> Router rule 4: *"Keep the pre‑booking boundary strict… Route `book` so the answerer can decline naturally."*

**So the first design question is never "what service do I build?" — it is "is this a question or a
staff task?"** Build a service only for the second. Over‑servicing pre‑booking is the classic
mistake: it turns informational turns into tickets and buries the hotel in noise.

### The four pre‑booking‑specific consequences

1. **No room anchor.** The guest has no room number and usually no booking reference. The anchor is **name + phone/email** — i.e. the lead‑capture fields. Every pre‑booking form must capture them.
2. **Booking itself is out of scope.** The bot never completes a reservation. It deflects to the booking engine with the `<<SHOW_BOOKING_BUTTON: <property>>>` marker ([chat_service_booking_mixin.py:37](services/chat_service_booking_mixin.py#L37)) and raises a silent `room_booking` ticket in the background (`_create_silent_room_booking_ticket_bg`).
3. **Lead capture is a goal, not a failure.** In during‑stay a ticket means the bot could not finish. In pre‑booking a captured lead *is* the product.
4. **The phase boundary is the main guardrail** and is enforced **twice** — router rule 4 *and* book_answerer CORE RULE 6. Both must hold; fixing one alone leaks (same shape as the HOSPITALS and outside‑dining bugs).

---

## 2. Scope of work — what pre‑booking does and does not do

From the signed Zone SOW (*KeBo Engage BOT — Scope of Work*, May 2026). Reuse this table as the
default contract for any new pre‑booking tenant; it is what the prompts already encode.

**In scope**

| Capability | Notes |
|---|---|
| Room queries | Room types, configurations, descriptions |
| Amenities & property details | Features, dining outlets, facility timings |
| Policy inquiries | Cancellation, pets, check‑in/out, mandatory ID |
| Location & directions | Exact navigation routed via Google Maps |
| Multi‑property routing | Rule‑based city selection → hotel list (skip entirely for single‑property) |
| Call deflection | The commercial point: cut pre‑booking calls to the front desk |
| Agent notifications | Ticket pushed to the property's own agents (WhatsApp on Zone; **channel is per-tenant** — web-only is a valid phase 1) |
| Human handoff | Escalate when context exceeds bot scope |
| Lead capture | Contact data + after‑hours SLA follow‑up promise |

**Out of scope — hard guardrails**

| Excluded | Behaviour |
|---|---|
| Booking / reservation execution | Deflect to booking engine; never "I've booked it" |
| In‑stay requests | Decline as an in‑stay thing (towels, room service) |
| Reservation alterations | Cannot modify or cancel an existing booking |
| Guest complaints | Immediate human handoff; the bot does not resolve |
| Financial / comp discretion | Never offer unverified discounts, upgrades or freebies |
| External commitments | Never stray from or except published policy |
| Competitor property talk | Deflect |
| Specialized advice | No medical, legal or financial advice |
| Loyalty & voice channels | Deferred |

The last four are already carried by `_out_of_scope_guardrail_directive()` plus the book_answerer's
CORE RULES 8 and 9. **Verify the guardrail is actually injected** — on Sarovar it existed in code
and was never injected at all for weeks (`project_sarovar_fake_confirmation_and_dead_guardrail_sep01`).

---

## 3. The mechanism (same plumbing as post‑booking)

- **No prebuilt catalog** — `_PREBOOKING_PREBUILT_SERVICES = []` ([config_service.py:219](services/config_service.py#L219)). Every service is custom‑created per property.
- Admin UI: **Bot Training → Phases tab** → Active Phase **`pre_booking`** → *Create New Service For This Phase*.
- Admin API (all need `x-hotel-code`): `POST/PUT/DELETE /api/config/services` · `POST /api/config/service-kb/preview-extract` ("Pull from KB") · `POST …/{id}/regenerate-prompt` · `PUT …/{id}/prompt`.
- **Service payload schema, `ticketing_mode` values, and form field types are identical to `postbookingskill.md §1`** — not repeated here.
- **A service's `description` is a routing mini‑prompt**, not marketing copy. It is what the router reads.
- The service's real prompt is the DB column `new_bot_services.generated_system_prompt`, **not** `_build_service_system_prompt`. Writing the override column **locks** auto‑regeneration — lock deliberately, and lock *first*, then save.

**Ticketing categories** are the same fixed accepted set as post‑booking
([ticketing_service.py:1816](services/ticketing_service.py#L1816)) — see `postbookingskill.md §2`.
The pre‑booking‑relevant ones: `room booking request`, `support request`, `Business Centre`,
`In-Room Dining`, `Cashiering Billing`, `Front Office`. **Never send a department name as a
category** (`"Reception Front Office"` was rejected 9/9 in a real incident).

The classifier is already primed: `_PREBOOKING_TICKETING_CASES`
([config_service.py:120‑129](services/config_service.py#L120)) enumerates all eight pre‑booking
ticket intents — sales callback, custom room arrangement, custom/corporate/group quotation, policy
exception, website booking failure, payment issue, and group/corporate proposals. You are giving
them services and KB, not teaching them from scratch.

---

## 4. The canonical pre‑booking service set

Start from these. Add only what the property's own collateral justifies.

| # | Service (`id`) | Type | Mode | Accepted category |
|---|---|---|---|---|
| 1 | Room Booking / Accommodation lane (`room_booking`) | service | text + booking button | `room booking request` |
| 2 | Support Intake / Callback (`support_intake`) | service | form | `support request` |
| 3 | Events, Banquet & Wedding Enquiry (`banquet_event_enquiry`) ⚠️ only if the property sells events | service | form | `Business Centre` |

**That is usually the whole set.** Optional fourth: **Booking / Payment Issue**
(`booking_payment_issue`, form → `Cashiering Billing`) where the property has a real IBE that fails
often enough to matter. Book_answerer CORE RULE 10 already handles the conversational side.

**Everything below stays on the `book` lane — no service, no ticket:** room types and sizes, in‑room
amenities, dining outlets and cuisine, policies, check‑in/out, ID, payment methods, distances,
nearby attractions, parking, gym, whether a spa/pool exists, and the *existence* of offers.

### 4.1 The room lane is modular — and it is the one that goes wrong

Router rule 2, verbatim:

> If an active room‑booking service exists, route all room / suite / accommodation / room‑offer /
> room‑rate / room‑booking questions to that service, **whether the guest is only exploring or
> trying to book**.
> `room booking` is about accommodation. `room service` is an in‑stay operational request.
> **The word `room` alone does not decide the route.**
> If the object is spa, dining, transport, towels, maintenance, laundry, hall or venue, **never fall
> back to room booking just because it is the only active booking lane.**

That last sentence is a scar. With only one active service, the router will try to funnel unrelated
asks into it. Encode the closed set in the service `description`.

**Spec — Room Booking:** *Desc:* "Handles room, suite and accommodation questions and booking
intent, whether the guest is exploring or ready to book. Answer room types, sizes, occupancy and
inclusions from the KB. Never quote a rate that is not in the KB and never state live availability.
Do not take or confirm the booking in chat — share the booking link and capture the guest's contact
and stay dates. This lane does NOT handle dining, spa, transport, events or any in‑stay request."
*Captures:* `full_name` · `phone` · `email` · `check_in` · `check_out` · `guests`.

### 4.2 Support intake is the escalation destination

**Spec — Support Intake:** *Desc:* "Handles guests who explicitly ask to speak to a person, be
called back, or be contacted by the team, and guests whose question the bot could not answer.
Capture name and contact and raise a callback ticket. Do NOT open this service for a pre‑checkin,
during‑stay or post‑checkout operational request — those are declined on the book lane."
*Form:* `full_name`(text,req) · `phone`(tel,req) · `email`(email,opt) · `requirement`(textarea,req).

The exclusion in that last sentence mirrors router rule 3's exception and is load‑bearing — without
it, "my AC is broken" opens a support ticket on a bot that cannot help.

---

## 5. Design rules

1. **Question or staff task?** Build a service only for the second. (§1)
2. **Anchor = name + contact**, never a room number. There is no room yet.
3. **Ground in the KB — never fabricate.** Pull from KB after creating each service. Missing info → "I don't have that", never invented.
4. **Pair every gap with a handoff offer.** Book_answerer's D1 rubric: when a KB gap plausibly affects the booking decision (rate, offer terms, hall names, hours), say you don't have it **and offer the team**. Dead‑ending on "I don't have it" three times was a real Zone defect. A property with many KB gaps needs this most.
5. **Encode closed sets in the `description`** — what this lane does *and does not* handle (§4.1).
6. **Never fix behaviour with keyword lists.** LLM judges, code enforces its own emitted field. Keyword guards scored 5/7; deleting them scored 7/7 (`feedback_no_deterministic_keyword_guards`).
7. **Router emits, code enforces.** Prompt‑only rules slip; CODE‑enforced fixes held across three 50‑turn runs (`project_sarovar_yes_bug_stickiness_guard_and_cancel_judge_aug18`).
8. **Phase gates must fail OPEN on unknown.** A gate that closes on an unrecognised phase silently kills valid turns.

---

## 6. Single‑property vs multi‑property

Most of the pre‑booking machinery — and most of its historical bugs — exist only to answer *"which
property does the guest mean?"* See `multiptorpty logic.md` for the full scope‑state contract.

**Multi‑property (Zone):** `_active_property_ids` scope state maintained every turn across router,
KB load, answerer, service scoping, chips and prefetch. City‑selection intro, curated
`new_bot_location_intros` rows, property narrowing and pinning. *If you re‑implement one piece and
miss the others, the flow breaks on the next turn.*

**Single property: switch none of it on.** Set `expected_property_count = 1` and confirm
`property_count = 1` — [chat_service_response_mixin.py:1544‑1560](services/chat_service_response_mixin.py#L1544-L1560)
then takes the single‑city branch (*"…at <hotel> in <city>"*) instead of the group phrasing. No
location picker, no narrowing, no curated intro rows. Every cross‑property leak bug becomes
structurally impossible.

⚠️ **`business.city` must be a real city.** The code treats "group", "hotels", "properties",
"chain", "collection" as group‑ish labels and falls back to the group phrasing.

---

## 7. Procedure & verification

**Prerequisites**
- [ ] `hotel_code` decided and **not a prefix‑family relative** of an existing code.
- [ ] `config/properties/<hotel_code>.json` exists with **filename == `business.id` == `business.code` == the `new_bot_hotels` code.** No short alias file. This is the tenant‑identity split trap — see §8.
- [ ] KB loaded and scoped to the property.
- [ ] FMS entity id in `TICKETING_PROPERTY_ENTITY_MAP_JSON` (keyed by **location**, not hotel_code), else tickets stay internal.

**Per service:** Phases tab → Active Phase `pre_booking` → Create → fill (+ Form Builder if `form`)
→ **Save** → **Pull from KB** → **Regenerate → Save Prompt** → confirm it appears under *Services In
This Phase* with the right mode.

**Testing:** force the phase without a widget —
`POST /api/chat/message`, header `x-hotel-code: <CODE>`, body
`{"session_id":"<new>","hotel_code":"<CODE>","channel":"web","message":"<ask>","metadata":{"phase":"pre_booking"}}`.
Use `-m 200`; LLM turns are slow. **Fresh sessions default to `during_stay`** — always confirm the
routing‑log phase before trusting a result.

**Verification checklist**
- Info question → KB answer, **no ticket**, route `book`
- Room/accommodation ask (exploring *or* booking) → route `service`, room lane
- Booking intent → booking button, **no claim that a booking was made**
- Explicit "talk to someone" → support intake, ticket in `support request`
- In‑stay ask ("AC broken", "send towels") → declined on `book`, **no support ticket opened**
- Unrelated ask (jokes, maths, competitor, medical) → deflected per §2
- KB gap on a decision‑affecting fact → "I don't have that" **+ handoff offer**
- Non‑room object (spa/dining/venue) → **not** swallowed by the room lane
- Ticket lands in the expected accepted category, visible via `GET /api/admin/api/tickets`

Run a **50‑turn unattended pass** through the real widget (`reference_standalone_playwright_overnight_harness.md`):
wait on the `Messages: N` counter, not panel text; flush every turn to disk. Verify in both the
harness page and the `?embed=1` iframe at phone and laptop widths — they are the same component.

---

## 8. Gotchas

- **Tenant‑identity split.** "Pull from KB" returns `no_kb_content` despite a good KB = `resolve_hotel_code()` collapsed the long code onto a stray short one via prefix‑family match ([config_service.py:2368](services/config_service.py#L2368)). `_discover_known_hotel_codes()` reads `config/properties/*.json`, **not** the DB. Diagnose in `logs/pull_from_kb_debug.txt` (`source_count=0` = wrong scope). Services created under the wrong scope never persist — re‑create them after fixing.
- **Pull from KB can take ~276 s** and the button stays disabled the whole time. It is not hung.
- **`pending_action` switches the router OFF** → the ungrounded fallback hallucinates (it once invented "red wine added"). Fingerprint: no router LLM row + `main_router.confidence = 0`.
- **"Guest says yes and nothing happens"** = the stickiness guard binning a correct `route=service`.
- **The opening lead form raises a spurious `room_booking` ticket** unless it carries the `lead_source` marker.
- **Chip clicks shipped as `typed_input`** (`ui_source_type` clobbered) → prefetch cache never hit, every click a live LLM call. Needs `vite build`; `dist/` is what's served.
- **New services are invisible to a running chat** until restart (stale per‑`hotel_code` service cache). No `--reload`.
- **Prompt rules are recency‑sensitive** — a rule placed late wins. Check placement before rewriting.
- **The bot's spoken property name comes from `new_bot_properties.name`**, not the KB file.
- **Never hardcode property names, lists or counts** in prompts — count from live data.
- **Tickets are judged against the external API.** A `LOCAL-*` ref means the test was invalid.
- **Multi‑property only:** the COMPARE rule ("which is best for X") drags a past city back in; an isolated prompt‑block A/B said 0/60 while real end‑to‑end still leaked 1/3. **Never trust an isolated block A/B as verification.**

---

## 9. Worked example — Mango Grove Hotel, Chandigarh

First genuinely single‑property tenant. Build plan in [mangoplan.md](mangoplan.md); live status in
[MANGO_CHECKLIST.md](MANGO_CHECKLIST.md).

- **Shape:** 1 property, 97 rooms, pre_booking only. §6 single‑property path — no scope state, no picker.
- **KB:** `mango_grove_hotel_chandigarh_kb.txt`, **15 books over 18 pages**, built from the client's onboarding workbooks.
- **Services:** the §4 canonical three. Banquets is justified — 12,000 sq ft, up to 400 guests, and the client named banquets as a secondary bot objective. A fourth (payment issue) is deferred until there is a real IBE.
- **Rule 4 matters more than usual here:** the client returned the gap report with 10 of 12 gaps filled, but restaurant hours and all four offers' terms are still missing. Every one of those must produce a handoff offer, not a dead end.
- **Client asked for real‑time rate & availability** (Onboarder sheet 5). That is **out of scope per §2** and unbuilt — it needs an IBE integration and an explicit commercial decision. Do not let it leak into a service description.

### 9.1 What this build taught us

Seven things that were not in this file before, each one paid for:

1. **Book count is not a health metric.** Mango's 15 books index only 18 pages — several books point at
   the *same* page id. Count `new_bot_kb_book_pages` links, not books, before calling a KB "duplicated".
2. **`conversation_audit.jsonl` lies about services, phase and tickets.** Its `services_enabled`,
   `phase` and `ticket_ids` blocks are empty even when all three work. Read `flow.log` payloads,
   `chat_turn_llm_summary.jsonl` and `ticketing_debug.jsonl` instead. Two wrong conclusions came from this.
3. **The opening lead form already exists for every tenant.** `introFormRequired`
   ([ChatHarness.tsx:3499](admin_ui/src/pages/ChatHarness.tsx#L3499)) fires for any `pre_booking`
   tenant on a new session and locks the input until name/phone/email are submitted. Do not build a
   second one; only the *copy* is per‑hotel (`ui_settings.widget.lead_form`).
4. **A gap rule can exist and still not fire.** Mango's book_answerer said "always offer the handoff
   for a rate" in one place and "only if it's a dealbreaker" in another. The model followed the
   narrower rule and dead‑ended on "how much is a room?". When behaviour is wrong, look for a
   *second, conflicting* instruction before adding a third.
5. **Never edit `prompts/**/*.md` to change one tenant.** Those files seed the industry‑default rows
   shared by every hotel. Write a hotel override row (`new_bot_prompt_registry.hotel_id`) instead —
   resolution is hotel → industry, so the override is tenant‑local by construction.
6. **`sq. ft.` breaks the output formatter.** `_dedupe_response_sentences`
   ([chat_service_response_mixin.py:387](services/chat_service_response_mixin.py#L387)) splits on
   `(?<=[.!?])\s+`, so "sq. ft." becomes two sentences and the orphan "ft." can be deduped away.
   Write **`sq ft`** in KB text. (A shared fix would be `(?<=[.!?])\s+(?![a-z])`.)
7. **FMS entity ids are NOT the same in dev and prod.** Dev 6345 = Mango Grove; prod 6345 = an
   unrelated German company, and prod's Mango is **6586**. Same for Sarovar and Zone. Always resolve
   the entity id *per environment* by name before pointing tickets at it — and check what else uses
   it: prod 6586 is a live FMS/RI customer with real guest data, so test tickets would land in the
   client's live dashboard.

### 9.2 A tenant with a dedicated service must own its own handoff acceptance

Zone has only TWO services, so every "yes, have the team contact me" funnels to `support_request` —
and Zone's description says so explicitly ("agreement to a staff handoff that you yourself offered in
your own immediately preceding message").

**Copying that shape blindly onto a tenant that has a THIRD service routes the acceptance to the
wrong form.** On Mango the bot answered a banquet question, offered "shall our team send details and
a quotation?", and the guest's "Yes, please" opened **nothing** — and when first patched, it opened the
*generic support form*, which collects only name/phone/email. The Banquets team cannot quote without
**event type, event date and guest count**, which only the banquet form has.

Rule: whichever service owns the TOPIC must also own the ACCEPTANCE of a handoff it offered. Put that
in the service's own `description` (it is a routing mini-prompt), and tell it to carry over what the
guest already said rather than re-asking:

> "THIS SERVICE ALSO OWNS THE FOLLOW-UP. If your own immediately preceding message was about an event
> … and offered to have the team reach out … and the guest then agrees … that agreement belongs HERE,
> not to the general support form."

Then scope the support service so it does not steal those ("…venue, event, group or custom booking
enquiries **that the Banquet service has not already taken**").

**Debugging note:** a turn where the acceptance silently does nothing shows `policy_reason_code:
"unknown_target_service"` and `requested_service.id: ""` in the ticketing payload, and **no
`main_router` call at all** in `flow.log`. Do not read `last_assistant_message` from that payload —
the ticketing payload does not carry it, and it will read empty even when context is fine.

### 9.2 When the client returns a gap report

- **Reconcile, don't append.** Their new answers will contradict the KB you already built (Mango had
  8 conflicts: breakfast time, distances, payment methods, room amenities, hall count, WhatsApp number).
  Appending leaves the bot able to state both. Agree the rule up front — Mango's is *newest wins*.
- **Absence is not a contradiction.** An outlet the new sheet simply doesn't mention (Alfresco) is an
  omission. Keep it and ask; don't delete a real fact on silence.
- **Watch for data‑entry slips.** Mango's returned sheet carried the extra‑bed price in the bar row
  ("Is there a bar? → yes at cost 1000+taxes"). Applying it verbatim would have priced the bar.
- **Service descriptions go stale too.** The banquet service still told the model "hall names are NOT
  in the knowledge base" after the client supplied them. After any KB update, re‑read every service
  description for facts that moved, then Pull from KB and regenerate.
- **Lock the service prompts only when the descriptions are final** — `generated_system_prompt_override = 1`.
  Zone's two services are locked in both environments; that is the end state to match.

---

## 10. Convention

Keep these together (`prebookingskill.md`, `postbookingskill.md`, `duringstayskill.md`) so
*"refer to `<x>skill.md` and do it"* always works. When a phase is trained well, fold what was
learned back into its skill file — shared mechanism, routing, design rules, service specs, a worked
example, and gotchas, all grounded in real code references.
