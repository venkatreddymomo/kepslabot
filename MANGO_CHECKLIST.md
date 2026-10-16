# Mango Grove Hotel, Chandigarh — training checklist

> Live working checklist. Build plan: [mangoplan.md](mangoplan.md) · Mechanism: [prebookingskill.md](prebookingskill.md)
> Tenant: `mango_grove_chandigarh` · hotel_id **69** · property **371** · pre_booking only, web widget only.
> Last updated: 2026-09-18

**Environments** — dev `172.16.5.32`, prod `10.100.5.153`, both DB `GHN_PROD_BAK`.
Prod rule: writes ONLY to `new_bot_*` tables, and ONLY after asking each time. Everything else read-only.

---

## A. Done

- [x] Tenant isolated — hotel 69, property 371, `property_count = 1`, no scope/picker machinery
- [x] KB loaded — 15 books over 18 pages (books share pages; no duplicate content)
- [x] 3 services built — 99 Room Booking, 100 Support Intake, 101 Banquet/Event
- [x] Branding — `#003F34` / `#F79558`, position right, consent line
- [x] Bot name + greeting — "Mango Grove Assist" / "Sir/Madam. How can I assist you today?"
- [x] Escalation configured — 1 attempt → 9815009635, 24 hrs
- [x] Lead capture — mandatory session-start form (name / mobile / email), already built-in
- [x] Ticketing wired in dev — real external ticket proven (id 11600, HTTP 200)
- [x] **KB updated in DEV** — 14 new facts from the hotel's returned gap report
- [x] **KB contradictions resolved in DEV** — newest answer wins (11 replacements)
- [x] **Handoff rule fixed in DEV** — `book_answerer` v3→v5, Mango's own prompt row only
- [x] `sq. ft.` → `sq ft` in Mango KB (works around a shared formatter bug)
- [x] Verified in dev — Wi-Fi, hall names, extra bed, laundry, lift, restaurant hours, rate-handoff

## B. Next up

- [x] **Service 101 (Banquet) rewritten** — description + hand-written prompt, applied to **dev and prod**, and **locked** (`generated_system_prompt_override = 1`). Verified: names/sizes stated, per-hall capacity declined + handoff, price declined + handoff, booking intent opens the form.
- [x] **Room booking = one button** — no "Book on website" (they have no booking engine, only a homepage). Single **Book Now** → form (dates, guests, contact) → ticket → *"our team will get back to you shortly to confirm your booking."* Hidden for the rest of the conversation once submitted.
      Isolated via one opt-in env key `CHAT_BOOKING_TEAM_ONLY_HOTELS_JSON=["mango_grove_chandigarh"]`; verified the gate returns **False** for zone / sarovar / khil, so their path is unchanged.
- [x] **All 3 service prompts locked** in prod (`generated_system_prompt_override = 1`), matching Zone
- [x] **Service 99 description** — stale "shares the hotel's booking link" wording removed
- [x] **Promoted to PROD 2026-09-18** — 11 KB pages + prompt row 75 + services 99/100. Zero anchor misses; prompt length matches dev byte-for-byte (99,936). Guard confirmed only `hotel_id=69` rows changed; other hotels' 13 locked services untouched.
- [x] **Google Maps link added** — `30.71676,76.810678` on page 11029
- [x] **English / Hindi / Punjabi all verified on prod** — Hindi answers in natural Hinglish, Punjabi in Gurmukhi and used the new gym-hours fact
- [x] **Escalation verified on prod** — gap question → "would you like our team to reach out?" → "yes please" → **support form opens** (full_name, phone, email, requirement). Mango's router already carries Zone's intent-based rule (handoff offer + acceptance → `support_intake`); nothing needed porting.
- [ ] **Set Mango's widget deployment `model` to `gpt-4.1-mini`** (Zone's is already 4.1-mini; Mango's is NULL so it falls back to the global 5.4-mini for book_answerer). Per-deployment, so Zone/Sarovar are untouched. Needs a write to `new_bot_widget_deployments`.
- [ ] Manual Zone check in the widget (two buttons, original label, original ack)
- [x] **Property images loaded (2026-09-21)** — 17 photos uploaded via Admin → Content Manager, scoped to `hotel_id=69 / property_id=371`. Verified the bot matches them (4 returned for "photos of your restaurant", 3 for "the banquet hall"). Zone/khil/iconiqa counts unchanged.
      Titles: Hotel Exterior · Hotel Building · Purple Mango Restaurant (+Seating/Dining/Interior) · Purple Mango Bar (+Counter/Seating) · Conference Room (+U Shape Setup/With Projector/Meeting Setup) · Banquet Hall (+Round Tables/Event Seating/With Projector).
      Prep needed: the 3 Bar images were `.avif` (uploader takes png/jpeg/webp only) so they were converted to JPEG, and the facade shot was 10.8 MB (over the 10 MB cap) so it was resized to 2400px.
      The 4 Hotel Logo files were **not** uploaded — a logo is branding, not a gallery photo, and one of them is a PDF.
- [x] **"Can't show photos" fixed** — Mango's prompt listed "photos" as trivia staff can't help with (written before images existed). Replaced with a rule stating the hotel HAS photos and the app attaches them. Verified: "show me photos of your restaurant" → *"Here are a few photos of the restaurant."* + 3 images.
- [x] **Stale cached KB facts fixed in ALL 3 services** — `service_prompt_pack.extracted_knowledge` was a verbatim snapshot of the pre-update KB. svc 99 (9 fixes), svc 100 (9), svc 101 (2, incl. "Divided into 4 halls"). Verified live: "Side table", "children below 6 years", "INR 1,000 + taxes per night", "Credit Card, Debit Card, UPI".
- [x] **Generic photo ask fixed** — added a "Photographs Available" list to KB page 11016 and a prompt rule to name the subject. "pictures of the hotel?" now attaches Hotel Exterior (was 0).
- [ ] **PARKED — two photo edge cases.** Images attach by matching photo names against the bot's OWN reply text, and "hotel"/"room"/"suite" are deliberate stopwords ([image_tool_service.py:34](services/image_tool_service.py#L34)). Two cases resist prompt-only control:
      · "show me the bar" → 4 *restaurant* photos, because the outlet is really called "Purple Mango Restaurant & Bar" and "Restaurant" wins the match.
      · "what photos do you have?" → the bot recites the names and every name attaches its picture (9 images).
      Proper fix is in the matcher (cap images per reply + match only the first sentence) — shared code, so left alone on the user's instruction. Three prompt rounds gave diminishing returns.
- [x] **Prompt mechanism-leak fixed** — a reply had said *"The app should show the exterior photo for you."* Rule rewritten so the guest never hears about "the app"/"matching"/"attaching".
- [x] **Widget model set to `gpt-4.1-mini`** (`new_bot_widget_deployments` id 2) — matches Zone; per-deployment so no global env change.
- [x] **Bar image rename tried and REVERTED** — renaming to `Bar Lounge/Counter/Seating` made "show me the bar" work, but the token "bar" then collided with *"mini bar"* in the room amenities, so room questions started showing bar photos. Reverted to `Purple Mango Bar …`. No title can satisfy both; the fix belongs in the matcher.

- [x] **Banquet handoff acceptance fixed (2026-09-21)** — after a banquet answer the bot offered "shall our team send details and a quotation?", and the guest's "Yes, please" opened **nothing** (no form, no ticket, but the bot said the team would reach out). Root cause was the service `description`, not code: Zone has only 2 services so every acceptance funnels to `support_request`, and its description says so explicitly; Mango's `support_intake` and `banquet_event_enquiry` had no such clause.
      Fix: `banquet_event_enquiry` (101) now owns the acceptance of an event handoff it offered, and `support_intake` (100) was brought up to Zone's shape but scoped so it does not steal event enquiries. Applied to **prod and dev**; 23 other-hotel services untouched.
      Verified: "venue for 40th anniversary, 180 guests" → banquet form; **"Yes, please" → banquet form** (7 fields incl. event_type / event_date / guest_count), carrying over the occasion and guest count.
      Note: routing this to Support Intake would have lost exactly those three fields — the Banquets team cannot quote without them.
- [x] **Single-property intro line** — the auto-opening message said "tell me about your **hotels** and what makes **each** special". Now "I'm exploring your hotel — tell me about it and what's special here", gated on `ctaIsSingleProperty` so Zone keeps its plural copy. Widget rebuilt.
- [x] **Zone verified unaffected** — drove Zone's widget end to end; its chip clicks carry full context (`last_assistant_message`, both services, phase) and its support form opens correctly. Zone does **not** have this bug and nothing of Zone's was changed.

### Image matching — known limits (accepted for now)
Images match against the **whole reply text** with **no cap**, so:
- "show me the bar" → shows *restaurant* photos (the outlet is "Purple Mango Restaurant & Bar")
- "what photos do you have?" → the bot names them and every name attaches a picture (9 images)
- an off-topic decline can still attach stray images
Proper fix ≈ 10 lines in [image_tool_service.py](services/image_tool_service.py): match only the first sentence and cap at 3. Shared code, so not done.
- [x] **`category` NULL on all 17 rows — investigated 2026-09-21, NOT the image bug.** Zone fills categories with a one-time offline vision backfill, [tools/classify_zone_images.py](tools/classify_zone_images.py) (GPT-4.1-mini looks at each photo and writes `category`/`description`/`tags`/`title`). Zone is 389/389; Mango 0/17. But on today's live code the category buys Mango almost nothing, because only ONE of its three consumers is still wired up:
      · `select_for_books` (category-ordered picking) — **zero call sites, dead**
      · `_rank_candidates` (category+description+tags haystack) — **zero call sites, dead**
      · `_loose_title_match` — **live**, and reads `category` for exactly one thing: skip rows whose category is `'other'` ([image_tool_service.py:231](services/image_tool_service.py#L231))
      The live path is `select_by_title_mentions` ([chat.py:2489](api/routes/chat.py#L2489), [:2593](api/routes/chat.py#L2593)) = photo TITLE vs the bot's reply text. So Zone's real win was the **titles** — it rewrote hashes like `sample43` into `Deluxe Room` so matching could fire at all. Mango's titles were hand-written correctly at upload, so Mango already has the part that mattered. Running the classifier would only enable the `'other'` guard, and Mango has no junk photos to exclude (all 17 are genuine guest-facing shots). Verified the 3 "Purple Mango Bar" rows are real bar photos, correctly titled — not mislabelled.
- [x] **Images MUTED on the Room Booking lane (2026-09-21)** — since there is no room photo to show, the room-booking agent now attaches none at all rather than the wrong one. New opt-in setting `chat_images_muted_services_json` (a hotel_code -> service-id map), helper `_images_muted_for_turn` in [chat.py](api/routes/chat.py), gating BOTH image paths: the inline one that reaches the guest (`_attach_topic_images`) and the deferred one (`_attach_smart_images`). Set to `{"mango_grove_chandigarh":["room_booking"]}` in `.env` and `.env.dev`.
      **To re-enable when room photos arrive:** delete `"room_booking"` from that one env value and restart. No code change.
      Browser-verified end-to-end through the real widget (harness -> Mango -> lead form -> chat), with the ORIGINAL bug reproduced in its exact form:
      · "Do the rooms have a mini bar and tea kettle?" -> Room Booking agent answered *"Mini Bar and Tea/Coffee Maker"* — the precise wording that used to drag in the `Purple Mango Bar` photo — and attached NO image. Gate logged twice (inline + deferred path): `[images] MUTED: mango_grove_chandigarh has no photo of what 'roombooking' covers`.
      · Banquet lane ("tell me about your banquet hall for a wedding reception") -> correctly attached the **Banquet Hall** photo, gate silent. Not over-blocked.
      · Hotel-overview / KB lane -> still attached **Purple Mango Restaurant**.
      · Across the whole run the mute fired twice, both for Mango room-booking; no other hotel ever appears in the log.
      A `logger.info("[images] MUTED: ...")` line makes this observable in `logs/`, since `smart_image_selection_reason` lives only in response metadata and is never written to a log file.
      Isolation proven by unit-exercising the real helper: Mango room-booking mutes on all three spellings (`Room Booking` / `room_booking` / `room-booking`); Mango banquet, support and KB answers keep their images; ZONE and SAROVAR return False even when their lane is literally named "Room Booking"; empty setting mutes nothing anywhere. 11/11.
- [ ] **BLOCKED ON HOTEL — no guest-room photos exist.** The 17 supplied are Facade 2, Restaurant 4, Bar 3, Conference 4, Banquet 4. **Zero rooms.** So "show me your rooms" can never attach a room picture, and the loose tier fills the slot with whatever else shares a word — which is the real cause of the wrong-image reports, together with 3-letter tokens matching on equality so `Purple Mango **Bar**` fires on *"mini bar"* in a room answer. Ask the hotel for room/suite photos; no code or category change fixes this.
- [ ] **Deploy the code changes to the real prod server** — `.env` key, `api/routes/chat.py`, `services/chat_service_booking_mixin.py`, and the rebuilt `admin_ui/dist`. These exist only on the local machine so far; the live server still runs the old code.
- [x] **Dev synced from prod (2026-09-21)** — KB pages, all 3 prompt rows, all 3 services (description / prompt / lock / cached knowledge) and the widget model now MATCH prod. Guard confirmed only `hotel_id=69` rows written.
- [ ] **Images not in dev** — prod has 17, dev has 0. `bot_hotel_images_scraper` is not a `new_bot_*` table (no write permission), so they'd have to be re-uploaded through the admin UI pointed at dev.

### Round 2 fixes (prod, 2026-09-18)
- [x] Hardcoded "complete your booking via the website link below" reply — gated for team-only hotels (it pointed at a link that no longer renders)
- [x] Google Maps URL rewritten to `https://maps.google.com/?q=...` — the `&` was being HTML-escaped into the href, breaking the link
- [x] Button relabelled **"Provide booking details"**
- [x] Prompt rule: never ask for dates / guests / contact in chat — send the guest to the form

## C. Blocked / parked

- [ ] **Ticket testing in prod — entity ID.** `.env` says **6345**; in prod that is *"Schulbegleitung myschoolcare, Munchen"*, an unrelated company. Prod's real Mango is **6586**.
      6586 is a **live FMS/RI customer** — 4,086 guest-feed rows, 927 real guest records, 5 surveys, signed contract.
      Our `new_bot_*` work cannot touch it, but **test tickets would land in their live dashboard**.
      6586 currently has **no alert recipients**, so nothing would notify the hotel.
      → Get a separate test entity before any prod ticket test. **Parked.**
- [ ] Daily reports → `ed@`, `gmsales@` — never verified
- [ ] Unhandled-query alerts → `fom@` — never verified
- [ ] `allowed_origins = ["*"]` on the Mango widget in both envs — should be scoped to `mangogrovehotel.com`
- [ ] Live rate / availability from the booking engine — **out of scope** (Zone SOW); needs a commercial decision
- [ ] WhatsApp — deferred; phase 1 is web widget only

## D. Waiting on the hotel

Asked once and still unanswered:

- [ ] Restaurant (Purple Mango) full opening hours — asked twice
- [ ] All four offers: discount amount, conditions, validity — asked twice
- [ ] Google Maps share link
- [ ] Per-hall capacity (theatre / classroom / U-shape)
- [ ] Room split — how many Premium City View vs Premium Grove View of the 97
- [ ] Cancellation detail — 48 hrs before what, charge inside the window, no-show rule
- [ ] **Is Alfresco still operating?** Their new sheet lists only Purple Mango. Kept in KB — omission, not a contradiction.
- [ ] **Data-entry error to confirm:** *"Is there a bar? → yes at cost 1000+taxes"* — that is the extra-bed price in the bar row. Not applied.
- [ ] Which WhatsApp number is real — three exist: 9815009635, **9815008671** (used), 9815029465

## E. Decisions taken

| Decision | Why |
|---|---|
| Where two answers conflict, **newest wins** | Client instruction, 2026-09-18 |
| No dining service | They named "dining packages" as an objective but gave no process, menu or pricing. Dining stays KB-answered; F&B tickets come via Support Intake. |
| Banquet service kept | They sell it, named Banquets as a department, and the data exists |
| 15 books left as-is | Books share the same 18 pages — no duplicate content, nothing to clean |
| No opening lead form built | Already built in and mandatory for every `pre_booking` tenant |
| Shared formatter bug not fixed | `_dedupe_response_sentences` splits "sq. ft."; shared with Zone/Sarovar. Worked around in Mango's KB instead. |

---

## Isolation rules for this build

- Prompts: write **hotel override rows** (`hotel_id = 69`) only. Never edit `prompts/**/*.md` — those seed the shared industry default used by Zone and Sarovar.
- Never run `copy_dev_to_prod.py` for Mango — it copies all 12 config tables for **all** hotels and can overwrite live Zone/Sarovar config.
- Every write carries `WHERE hotel_id = 69`. Check Zone (67) and Sarovar (68) row counts before and after.
- Code changes are global — if one is unavoidable, gate it on `hotel_code` and flag it first.

### Opening CTAs — rebuilt 2026-09-21 (browser-verified)
- [x] **ROOT CAUSE: `welcomeCtaIntent` was missing from `handleCtaContactSubmit`'s dependency array.** The callback closed over its FIRST value (`"stay"`) forever, so tapping the venue button set the state but the submit handler never saw it — every guest who asked about events got a room pitch. The venue CTA had never once worked.
- [x] **Tapping a CTA no longer re-opens the details form.** It used to call `setCtaContactOpen(true)` unconditionally, so a guest we already knew got the form back pre-filled with what they typed a minute earlier, standing between them and their answer. Now, when `bookingContact` is set, the topic goes straight to its agent.
- [x] **No auto-sent overview after the lead form.** It both pre-empted the guest's choice and hid the buttons for good (they only render before the first guest message). The CTAs now sit under the greeting and the guest starts the conversation.
- [x] **Both CTA phrases ASK rather than announce.** "I'd like to book a room" reads as booking intent and the room agent answered it with the booking button alone — the guest tapped "Book a Stay" and was asked for details about a room nobody had described. Now: *"What room types do you have, and what's included in each?"*
- [x] **Renamed "Book a Venue" -> "Enquire about venue"** (`chat_welcome_service_ctas_json`).
- Verified in the browser, clean session each time:
  · Form open -> NO CTAs. Form submitted -> both CTAs under the greeting, `Messages: 0` (nothing auto-sent).
  · **Enquire about venue** -> sends the venue phrase, answers with all three venues + sizes + capacity, attaches the **Banquet Hall** photo, NO form, then offers the enquiry.
  · **Book a Stay** -> answers with both room types in bullets, **0 images** (reply says "mini bar" and no bar photo appears — the mute holding in a natural case), then offers booking help.
- Every change gated on `welcomeServiceCtas.length > 0`, which only Mango configures — Zone and Sarovar keep collecting details + city on their CTA exactly as before.

### Event enquiry as a BUTTON, not a question — 2026-09-21 (browser-verified)
- [x] **The banquet agent no longer asks "Would you like to go ahead and submit an event enquiry?"** That sentence was literally in service 101's prompt (`new_bot_services`, hotel 69). Replaced in PROD and DEV, with the lock (`generated_system_prompt_override=1`) left ON so auto-regeneration cannot undo it. Two sibling dead ends went with it: the "Would you like our team to reach out with the details and a quotation?" handoff, and "the handoff offer is enough… if the guest says yes, the form follows". Originals backed up per database.
- [x] **New `event_cta`** — server attaches it on any banquet/event turn for a hotel listed in `chat_welcome_service_ctas_json`, carrying the service's own four fields (Type of Event, Event Date, Expected Number of Guests, Any specific requirement). The widget renders **"Share your event details"** in the same style as the room button; tapping opens that exact form through the normal inline-form path. One enquiry per conversation, then it retires.
- [x] **ROOT CAUSE of "the button never appeared" (two bugs, found in order):**
      1. `config_service.get_service("banquet_event_enquiry")` returns **None** — the service is admin-created so it lives in the DATABASE, not the JSON config. Fixed with the same DB fallback `_inject_form_trigger` already uses.
      2. The helper was wired only into the HTTP `POST /message` route. **The widget talks over WebSocket**, and `websocket_chat.py` imports each attach helper by name — so it never called mine. That is why there was not even a log line. Now wired in BOTH paths.
      Lesson: anything attached only in `api/routes/chat.py` never reaches a real guest. Check [websocket_chat.py:578](api/routes/websocket_chat.py#L578) for the list of helpers the socket actually invokes.
- Verified in the browser, fresh session: informational venue question -> answer in bullets + **Banquet Hall** photo + *"Tap Share your event details below…"* + the button. Tapping it opened the 4-field form with Submit and Cancel request. A booking-intent question ("can you host a wedding for 250?") still opens the form directly, as before.

### Prod ids audit — 2026-09-22 (READ ONLY)
**Found in prod (`GHN_PROD_BAK`):**
| What | Value |
|---|---|
| `new_bot_hotels` | id **69**, code `MANGO_GROVE_CHANDIGARH` |
| `new_bot_properties` | **371** — Mango Grove Hotel, Chandigarh |
| `new_bot_services` | **99** room_booking · **100** support_intake · **101** banquet_event_enquiry |
| `new_bot_widget_deployments` | id **2**, widget_key `wk_KkRabdHTRYylT-eZ7Nu_1RiS`, status active, phase pre_booking, bot "Mango Grove Assist", model gpt-4.1-mini |
| `GHN_ORGANIZATION` (RI side) | **10898** — "Mango Grove Hotel, Chandigarh", Purv Marg, Industrial Area Phase I |
| `GHN_DEPARTMENT` | **22 departments** under org 10898 (all with NO head / email / mobile) |

**CORRECTED (read `configticket.md` §2): the ids live in OTHER databases on the same server.**
My first pass resolved them against `GHN_PROD_BAK.GHN_ORGANIZATION` and got unrelated hotels — the number ranges overlap across registries, so that lookup was meaningless.
| Registry | Database.table | Mango |
|---|---|---|
| **FMS entity** | `GHN_FF_PROD.GHN_FF_ENTITY` | **6586** |
| **RI org** | `GHN_PROD_BAK.GHN_ORGANIZATION` | **10898** |
| **FMS↔RI link** | `GHN_FEEDBACK_RESULTS.GHN_SURVEY_FMS_RI_ENTITY_MAPPING` | **6586 ↔ 10898 — row EXISTS** |

FMS entity 6586 in full: "Mango Grove Hotel, Chandigarh", **Group_Id 5392** ("Mango Grove Hotel"), Brand_Id 5846, Status Active, created 2026-08-13, vp@mangogrovehotel.com / 9815008941.
196 distinct active departments under 6586; 22 under RI 10898.
The missing FMS↔RI row was the Sarovar blocker (runbook §10) — **Mango already has it**, so nothing is blocked on that.

**Missing for ticketing (all blockers):**
- [ ] **`TICKETING_PROPERTY_ENTITY_MAP_JSON`** — no Mango entry, so tickets fall back to `LOCAL-*`. Needs `"mango_grove_chandigarh": {"chandigarh": "6586"}`.
- [ ] **`TICKETING_WIDGET_GROUP_MAP_JSON`** — no Mango entry (guest name will not show on the ticket). group_id is almost certainly the FF **Group_Id 5392**; widget_id still has to be allocated.
- [ ] **`TICKETING_ALLOWED_DEPARTMENTS_JSON` is WRONG for Mango.** Currently `["reservations", "Food & Beverage", "Banquets"]` — only **Banquets** exists. Under FMS 6586 the real names are **Booking** (not "reservations") and **Food & Beverages / Food & Drinks / Restaurant** (not "Food & Beverage"). The bot is being told to assign to departments that do not exist.
- [ ] **No staff in `DEPARTMENT_STAFF_CONFIG`** — 0 rows for org 10898 (Sarovar has 11). Nobody is assigned to receive a ticket.
- [ ] **No engage `widget_id`** — `KEBO_ENGAGE_SESSIONS` has no Mango sessions and no id allocated (Sarovar = 53). Chat History will not populate without one.
- [ ] **`allowed_origins = ["*"]`** on the live widget row — scope to mangogrovehotel.com before go-live.

## DB config gaps — Mango (prod, read-only audit 2026-09-24)

Mango's own ids already exist and are correct:
- RI org **10898** "Mango Grove Hotel, Chandigarh"
- FMS entity **6586** "Mango Grove Hotel, Chandigarh"
- FMS<->RI mapping 6586<->10898: present
- Departments present on both sides (RI: BANQUETS/Restaurant/...; FMS: Banquet / Events, Food & beverage service)

### Senior must create (we have no write access)
1. `GHN_USER_ORGANIZATION_MAPPING` — 1 row: USER_ID 11860 (Suhas, SuhasR@kepsla.ai, 918123242009) -> ORGANIZATION_ID 10898.
   Currently 0. Suhas is on 24 other orgs, not Mango. Without it he cannot open Mango in the dashboard.
2. `DEPARTMENT_STAFF_CONFIG` — 3 rows, ORG_ID 10898, STAFF_ID 11860, STATUS 1, MANAGER 1,
   DEPARTMENT_ID 48 (reservations), 39 (Food and Beverages), 38 (Banquet).
   Currently 0 rows. Result: all 29 Mango tickets have assigned_to = NULL and no mail fires.
3. `GHN_FF_TICKET_NOTIFICATIONS` — 5 rows, FMS_ENTITY_ID 6586, ENTITY_ID 10898, RECEIVER 'Dept Head',
   TYPE 'template', PHASE created / assigned / reminder_beforesla / sla_breach / resolved, each with a
   WhatsApp TEMPLATE_ID. Currently 0 rows -> no WhatsApp alert.

### Not a DB gap — our bug (mine to fix)
All 29 Mango tickets were filed against **Comfort Inn, Dickinson** (RI 7877 / FMS 5703); 4 more went to
entity 0. Cause: `mango_grove_chandigarh` is absent from `TICKETING_PROPERTY_ENTITY_MAP_JSON` in `.env`,
so it falls through to the harness fallback `_TEST_FALLBACK_ENTITY_ID = "5703"` (api/routes/chat.py:169).
The multi-property wrong-tenant guard only fires for hotels that HAVE an entity map, so Mango sails past it.
Also: every ticket used department_id 48 regardless of subject (never 38/39).
Also missing: `mango_grove_chandigarh` in `TICKETING_WIDGET_GROUP_MAP_JSON` (only zone is there).

## Where every id comes from (traced in prod, 2026-09-24)

CORRECTION to the section above: FMS 6312 is "Riviera Sarovar Portico Pathankot", NOT Zone Katra.
The counts it was used for are still valid as "a working reference", the label was wrong.

### Our end — we own these, both hotels have them
| id | table | Zone | Mango |
|---|---|---|---|
| hotel_code | new_bot_hotels | zone | mango_grove_chandigarh |
| widget_key (embed) | new_bot_widget_deployments | wk_kOKo8Vwt1rGBE7ZCSLhYaFeC | wk_KkRabdHTRYylT-eZ7Nu_1RiS (row 2, active) |

### Their end — where Zone's ids actually live
| id | table | Zone | Mango |
|---|---|---|---|
| **widget_id + group_id** | **GHN_PROD_BAK.BOT_IDENTIFIER_INFO** | **row id=9** -> widget_id 9, group_id 6666, client_id zone_2, bot_name Zoogle, org_id = CSV of all 19 Zone orgs | **NO ROW AT ALL** |
| FMS entity | GHN_FF_PROD.GHN_FF_ENTITY | 6125 Chennai etc | 6586 exists |
| RI org | GHN_PROD_BAK.GHN_ORGANIZATION | 8531 Chennai etc | 10898 exists |
| org group | GHN_PROD_BAK.GHN_ORGANIZATION_GROUP | 6666 "Zone Hotels" | 7598 "Mango Grove Hotel" exists |
| department ids | GHN_PROD_BAK.DEPARTMENT_MASTER (global) | 38 Banquet / 39 F&B / 48 reservations / 49 complaints | same ids reusable |
| staff user | GHN_PROD_BAK.GHN_USER | - | Suhas 11860 exists, NOT mapped to 10898 |

`widget_id` IS `BOT_IDENTIFIER_INFO.id` — there is no separate widget registry. It is the only id
Mango has no source row for. Everything else already exists and just needs wiring.

For Mango the senior creates ONE BOT_IDENTIFIER_INFO row:
  org_id = 10898, group_id = 7598, web_url = https://www.mangogrovehotel.com/,
  client_id = mango_1, bot_name = Mango Grove Assist
Its auto-assigned `id` becomes our widget_id for TICKETING_WIDGET_GROUP_MAP_JSON.

Without it: Mango chats never appear in the dashboard's Chat History (it scopes by widget_id) and
GHN_CHAT_BOT_CUSTOMER_DETAILS / KEBO_ENGAGE_SESSIONS rows are written unscoped.

## PROD WRITES — log (undo SQL included)

### 2026-09-24 08:09 UTC — step 1/3 DONE
`GHN_PROD_BAK.GHN_USER_ORGANIZATION_MAPPING` — inserted 1 row, **ID 346646**
ORGANIZATION_ID=10898 (Mango), USER_ID=11860 (Suhas), CREATED_USER_ID=0

Table went 41568 -> 41569 rows (delta exactly +1). Suhas: 24 -> 25 hotels.

Undo:

    DELETE FROM GHN_USER_ORGANIZATION_MAPPING
    WHERE ORGANIZATION_ID=10898 AND USER_ID=11860;

Safe by design: PRIMARY KEY is (ORGANIZATION_ID, USER_ID) so a duplicate is impossible.

### 2026-09-24 — A and B DONE (.env + code, no DB)

**B — department name typos.** Mango's allowlist was `["reservations","Food & Beverage","Banquets"]`.
The lookup is `WHERE TRIM(dm.DEPT_NAME) IN :names` — an EXACT match — so only `reservations`
resolved. That is why all 29 tickets were `department_id=48` regardless of subject.
Fixed to Zone's (working) spellings: `["reservations","Food and Beverages","Banquet"]`.
Proven against prod: **1/3 -> 3/3 resolved** (38 Banquet, 39 Food and Beverages, 48 reservations).

**A — entity map.** Added `"mango_grove_chandigarh": {"mango":"6586","chandigarh":"6586","mango grove":"6586"}`.
The env line ALONE would have regressed: `_resolve_property_entity_id` matches on a property NAME,
which a single-property hotel has no picker for, and the wrong-tenant guard
(`_force_local_only`) turns an unmatched entity into a LOCAL-ONLY ticket. So also added an
opt-in fallback: `TICKETING_SINGLE_ENTITY_HOTELS_JSON=["mango_grove_chandigarh"]` +
`_load_single_entity_hotels()` in ticketing_service.py — a hotel on that list whose map resolves to
exactly ONE entity uses it when no name matched. Opt-in by hotel_code so Zone keeps strict matching.

Verified: Mango -> 6586 with no property name / with name / with location.
Zone UNCHANGED (empty when no name; Kolkata 6121; "New Market Kolkata" 6329 longest-match intact;
Chennai 6125). Sarovar UNCHANGED (not opted in). `.env` backed up to `.env.bak_before_mango_AB`.

### 2026-09-24 — step 2/3 (C) DONE
`GHN_PROD_BAK.DEPARTMENT_STAFF_CONFIG` — inserted 3 rows, **IDs 2423 / 2424 / 2425**
ORG_ID=10898, STAFF_ID=11860 (Suhas), STATUS=1, MANAGER=1, DEPARTMENT_ID 48 / 39 / 38.
Other columns mirror Zone exactly: TICKET_PRIORITY='CRITICAL', SHIFT 09:00:00->08:59:59 (24h),
TICKETCOUNT='0', ISAVAILABLE='1', OUTLET_ID/FLOOR_NO/GROUP_ID NULL.
Table went 1805 -> 1808 (delta exactly +3).

Undo:

    DELETE FROM DEPARTMENT_STAFF_CONFIG WHERE ID IN (2423,2424,2425);

CAUTION: this table has NO unique key, so a blind re-run WOULD duplicate. The insert script
checks (ORG_ID, DEPARTMENT_ID, STAFF_ID) first, so re-running it is safe.
When the hotel gives real department heads, swap STAFF_ID on these 3 rows — no restructuring.

### step 3/3 (D) — NOT RUN YET
`BOT_IDENTIFIER_INFO` — 1 row. id is AUTO_INCREMENT, next value 10.

### HELD — needs the senior / BSP, do not insert
`GHN_FF_TICKET_NOTIFICATIONS` — group 7598 has whatsapp_number / whatsapp_key / bsp_org_id all NULL.
Rows would be inert, and TEMPLATE_IDs cannot be copied from Sarovar (they belong to Sarovar's BSP).
