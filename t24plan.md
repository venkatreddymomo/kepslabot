# T24 Hospitality — pre-booking, trained from the admin

> Tenant **`t24`** = `new_bot_hotels` **71**, two properties (372 Residency / 373 Retro), **pre_booking only**,
> web widget, **gpt-4.1-mini** for every role. Built on **DEV** (`.env` = dev copy, server on :8012).
> Source of truth: only the client's workbooks in `C:\Users\Hp\Desktop\t24\` — never the hotel website.
> Updated 2026-10-03.

## 0. The rule

1. Every pre-booking customisation lives in the DB and is edited in **Bot Training → Pre-booking** (plus the
   existing Forms, Phases, Prompts, Escalation and Widget pages). No `hotel_code == ...` checks and no per-hotel
   `.env` lists for new hotels.
2. A hotel that has saved the Pre-booking tab once is **admin-managed**: every switch below is read from its DB
   rows. A hotel that never saved it (Zone, Mango, Sarovar) keeps its old `.env`/code behaviour, untouched.
3. Code changes from here on are for bugs only.

## 1. How a guest moves through the chat (what the admin controls)

| Step | What the guest sees | Where it is set |
|---|---|---|
| Opens the chat | greeting + lead form (new visitor) | Setup Wizard (greeting), **Forms** (fields, title, subtitle) |
| Submits the form, or taps an opening button set to *Shows the saved message* | the saved message for their location | Pre-booking §1 (after-form choice, buttons) + §2 (messages) |
| Several hotels share the location | the comparison, **one button per hotel** | Pre-booking §2 |
| Taps a hotel | that hotel's saved message; the chat stays on that hotel | Pre-booking §2 |
| A service answers | that service's buttons: **link** (the chosen hotel's link, or one shared link) or **form** (raises a ticket for the chosen hotel) | Pre-booking §3 |
| Asks for the menu | the menu PDFs of the chosen hotel (+ any set for all hotels) | Pre-booking §4 |

**The hotel's shape is detected from the picker labels** (Pre-booking §6, *Picker label*):

| Shape | Example | Flow |
|---|---|---|
| One property | Mango | no location picker; one opening message |
| Several properties, **same picker label** | T24 (both "Mumbai") | no location picker; comparison + one button per hotel |
| Several properties, different labels | Zone | guest picks a location (lead form / dropdown); one message per location; a location with 2+ hotels shows hotel buttons |

The tab shows the detected shape at the top of §2.

## 2. What T24 has now (all set through the admin)

| Item | Value |
|---|---|
| Lead form (Forms) | full name, phone, email (city field removed — one location) |
| Opening buttons | **Book a Room** → shows the comparison; **Dining / Offers / Contact Us** → send a message |
| After the lead form | show the saved message (the comparison) |
| Picker labels | both "Mumbai" → one location, 2 hotels |
| Comparison + per-hotel messages | written from the KB (`new_bot_location_intros`: `mumbai`, `prop::t24_residency_andheri_east`, `prop::t24_retro_vile_parle_east`) |
| Services (Phases, prompts locked) | `room_booking_request` Room Booking (102, KB pulled), `support_request` Support Request (103, copy of Zone 82: callbacks, groups, events, booking problems), `guest_complaint` Complaint (104, past/current stay complaints) |
| Room Booking buttons | link **Book on website** → Residency `propertyId=9885`, Retro `propertyId=9956` (`chainId=9884`); form **Get help from our team** |
| Menus (S3, stable link `/api/widget/menu/t24/<id>`) | Residency: Toast food, Toast bar, In-room dining; Retro: Blend Kitchen & Bar (converted from the .docx) |
| Reply style | bullets •, no emoji, no "let me know if…" sign-off, no outside recommendations, chips on, form cancel on |
| Widget deployment | `wk_3aaaHPe7TIB2Vobu0Jb9n-pO`, pre_booking, Trisha, #E37533, model gpt-4.1-mini, origins `*` (restrict to the client's site before go-live) |

**Tested in the real widget (dev):** new visitor → lead form → comparison with 2 hotel buttons → tap Retro → Retro
message → "book a room" → Book on website (Retro link) + Get help from our team → form → ticket #11812 updated with
dates → returning visitor greeted by name with the 4 buttons → Book a Room → comparison → complaint → complaint form
→ ticket (Housekeeping) → callback/group request → support form → cancel works → "send me the menu" → 3 Residency
PDFs → link opens the PDF. Zone (Goa intro, booking link + team button) and Mango (gym, team-only booking) unchanged.

### 2 Oct — from the client's answers, and checks

- **Ticket entity: nothing exists for T24 in our GHN DB** (no FF entity / FMS id, no RI org, no FMS↔RI row, no
  BOT_IDENTIFIER_INFO widget, no GHN_USER for any t-24 / hoteltransit email). The harness test profile's 5703 is
  **ROHL** (another company): test tickets **#11812 and #11813 landed on ROHL's dashboard** (entity 6276 / FMS 5703)
  and should be closed. Fixed: an admin-managed hotel's ticket now goes out only to an FMS entity configured for it
  in the Pre-booking tab; otherwise it is kept LOCAL with a warning (verified: T24 booking with 5703 → LOCAL-148).
  The tab shows a red notice while any property has no FMS entity.
- **Departments the client gave** (OnBoarder + Gap Report), all already in DEPARTMENT_MASTER:
  reservations (43) → room booking request — reservation@t-24.in, 70455 93059; Sales (35) → support request —
  sales@t-24.in, 93248 02583; Front Office (5) — Residency reception@t-24.in 93249 72782, Retro
  reception@hoteltransit.in 82918 58469; complaints (44) → CRM + front desk — crm@t-24.in, 70459 02798; Banquets (1)
  and Food & Beverage (34) — fnbsales@t-24.in, 83559 43288; Housekeeping (8) — Residency housekeeping@t-24.in
  93249 72776, Retro housekeeping@hoteltransit.in 82918 58445; CRM gets all tickets. Dashboard login: Pawan Kumar,
  CRM executive, crm@t-24.in. Saved as T24's allowed departments (Pre-booking §7).
- **Emoji:** OnBoarder row 159 "No", row 179 "Limited" (= checkmark/arrow only) → no Zone-style emoji; set to none.
- **Languages:** English primary, Hindi secondary — tested: Hindi → Hindi, Hinglish → Hinglish.
- **Priority rules** (High complaint / urgent booking, Medium group & general enquiry, Low information) → new
  per-service "Ticket priority" in Pre-booking §3: Complaint High, Support Medium, Room Booking Medium (verified
  LOCAL-150 = high, department complaints).
- **KB** (DataSheet = newest answers): hotel phone +91 70455 93059 / landline 022 4667 8305 for both (onboarding
  numbers kept as "Other landline"), hotel email, main website, OTA 10% offer valid till 30 Sep, group bookings →
  Reservations, corporate → Sales (department contacts only — guardrail). Raw files + 13 books rebuilt; Room Booking
  and Support knowledge re-pulled.
- **Prompts:** T24 now has its own rows (hotel 71) copied from Zone's gpt-4.1 rows — book_answerer, main_router,
  service_router, next_suggestions, bot_briefing, instructions — with emoji and Zone names removed and the client's
  guardrails appended (no booking confirmation, rate caveat, no competitors, no medical advice, no staff personal
  mobiles).
- **Shared code fixes found by testing:** the scope judge refused "Tell me about T24 Retro" (it did not know the
  hotel's own property names — now it gets them); the reply de-duplicator emptied the second hotel's section when
  both share a fact (now dedupes per section).

### 3 Oct — DataSheet test, fixes, tester link

- **DataSheet test** (the client's 64 questions × 2 hotels, fresh session each, graded against the client's answer):
  **102 correct, 26 partial, 0 wrong.** Most partials are brevity or the sheet giving hotel totals ("Double
  occupancy: 110"). Real gaps, fixed and re-run:
  - Phone gave only the mobile → KB line now carries mobile + landline. (The first re-run still failed: the running
    server served the old Contact page from its 1-hour in-memory book cache. A KB rebuild needs a server restart.)
  - Retro "WhatsApp?" listed 4 numbers → the per-department numbers are the OnBoarder's *ticket-alert* WhatsApp
    numbers, not guest lines; relabelled "phone"; guest WhatsApp line made explicit (Residency also Front Desk).
  - Club Room bed "not confirmed, may be King or Queen" → queen (OnBoarder says Queen twice, King once).
  - Offer terms merged the direct-booking 10% with the OTA 10% → direct offer written out in words.
  - Retro "packages valid until 31 March 2027" → removed (the client replaced it in the Gap Report).
  - Room sizes held back ("If you want exact room area details, I can provide those") → T24 Room Booking prompt line
    (DB) + the shared answer-shape rule in `llm_orchestration_grounding_mixin._no_trailing_offer_directive` now names
    the wordings (how big / how spacious / size / area). Size asks: T24 6/12 → 12/12; Zone 3/4 → 4/4; Zone room
    overviews still never volunteer sq ft (0/4 before and after). Mango/Sarovar don't get this rule.
  - All in the DB, hotel 71 only: raw KB 576/577, the 13 books per hotel (JSON backups in `backups/kb_canonical/`),
    Room Booking + Support knowledge (prompts untouched, still locked).
- **Re-checked after the changes:** booking buttons (Retro 9956 / Residency 9885 + team help), complaint form, Hindi,
  group question → Reservations contact with no form.
- **Tester link** (dev): `tools/tester_tunnel_gateway.py` on :8020 serves a separate build of the /chat harness
  (`C:\Users\Hp\cloudflared\tester_chat_dist`, never `admin_ui/dist`) and forwards only the chat API/WebSocket, widget
  endpoints and three read-only admin GETs to :8012. Every admin write, the admin UI, the global model switch, Vite
  and the API docs get 403 (the admin API has no login). cloudflared quick tunnel → :8020; URL in
  `C:\Users\Hp\cloudflared\tester_tunnel.log`. All three run as detached processes. A cloudflared restart = new URL.
- Booking intent also files the existing global low-priority "room booking" lead ticket (Zone's June feature); for
  T24 it stays LOCAL like every T24 ticket.

### 5 Oct — tester fixes, forms, photos

- Fixed (code gated to admin-managed hotels = T24 + future; Zone/Mango/Sarovar unchanged): "Yes" to "Would
  you like our team to reach out?" opens the team form; a chip from a room reply follows the router (dining
  chip no longer answered "no dining"); a pre-cooked chip answer that opens a form runs live.
- T24 DB: plain "raise a complaint" → Complaint form; Support/Complaint forms no longer ask name/phone/email
  (the lead form has them; tickets take them from the session — verified LOCAL-162); Support's field =
  Issue Details (as Zone).
- **Photos** (client folders → web JPEGs ≤1600 px, uploaded via the admin upload endpoint to S3, categories
  set by script because the admin UI cannot set one yet): Residency 64 (8 `room` lead shots for the
  hotel-pick gallery, 26 `room_detail`, 17 lobby, 9 Toast Bistro & Bar, 4 rooftop pool); Retro 26 (8 `room`,
  11 `room_detail`, 5 Blend Kitchen & Bar, 2 Cafe Sidewalk). `room_detail` stays out of the pick carousel
  (it has no cap) but is matched mid-chat by title. Scripts: scratchpad `t24_img_prepare.py`,
  `t24_img_upload.py`.
- Photo gaps to ask the client: no exterior photo for either hotel (the gallery normally leads with one); no
  Residency Club Room; no Retro Cutting Cocktail or lobby. Retro photo files say "Queen" (Deluxe/Executive)
  and "Suite" while the workbook says King and Family Room — titles follow the workbook/KB.

### 6 Oct — "can you arrange a cab" (Suhas), tickets in Housekeeping

**The rule (user):** the pre-booking bot helps with room booking, meeting-hall / event enquiries and questions;
a question the KB cannot answer may go to the team. A request to ARRANGE a service (cab, airport pickup, table,
in-stay) is something it cannot do: no handoff, no ticket.

**What happened.** "can u arrange one cab" was answered by the book answerer. The shared guardrail tells it to
OFFER to pass a local service to staff; T24 had nothing overriding that. The "yes" opened the support form, and
ticket #11830 "arrange a cab" was raised.

**Fixed, T24 only, copied from Mango.** Mango has the same rule: its own prompt plus the late give-number switch,
and the client sheet's "cancel/modify: NO, give reservations phone and email".
- book_answerer row 77, v8: Mango's "only things you can do" block with T24's values (answer, room booking,
  meeting hall/event, complaint). The bot says it cannot arrange a cab / airport pickup / table / in-stay service
  / booking change, gives reservations **+91 70455 93059** (reservation@t-24.in), and stops. A shuttle, pickup or
  cab to the airport = the airport transfer in the KB, answered with its rates.
- main_router row 78, v4: Mango's refusal lines (a "yes" after a refusal is not an acceptance), "arrangements
  never go to support", and "meeting hall / event / group still go to the support form".
- support_request row 103: arrangement requests are not a support matter.
- Pre-booking → Reply style: "give the number when the bot cannot arrange something" ON.
- Backup: `backups/T24_cannot_arrange_before_20261006.json`.
- Lesson: extra lines of my own that mentioned "our team" (pass the question on, a follow-up rule, a yes rule)
  made bare yeses come back as the team-handoff line, which then opened a form. Reverted to Mango's wording.

**Housekeeping tickets #11827-11830 were the /chat test page, not the bot.** The page's per-hotel test profile
fell back to ROHL (entity 5703, ticket_source whatsapp_bot). The department step reads `entity_id` before
`organisation_id`, so it looked up ROHL's departments, where only Housekeeping has a manager. The embedded widget
never applies test profiles.
- Fix (code, admin-managed hotels only): `/api/chat/test-profiles` returns no profile for them. Zone, Mango and
  Sarovar keep theirs.
- Verified in the browser: the lead-form request carries `chat_test_profile_applied: false` and no entity.
- #11826 (a room-booking ticket raised from "Yes, please check for me") and #11827-11830 are tests to close.

**Replays** (WebSocket, no forms submitted; scratchpad `t24_cab_replay.py`, before/after/final JSON):
- cab → refusal + number, "yes" → no form.
- airport pickup → "arranged once your booking is confirmed… call reservations".
- shuttle question → transfer rates.
- pool and room booking: unchanged.
- meeting hall → form; complaint → form; "call customer care" → form.
- bare "yes" after an answer → "Sure - what would you like help with?".
- Browser on the tester link: cab → refusal with tap-to-call number; "Yes, please" → no form.

**Chips after a refusal (user: a "Yes, please" chip there is irrelevant).**
- Old "Yes, please" chips came from T24's chip prompt rule: a yes/no question gets "Yes, please" / "No, thanks".
  The old reply had asked "would you like me to?".
- After the fix the leftover chip was "Can you share the cab contact?". The shared grounding check keeps only chips
  grounded in the reply, which after a refusal is the number.
- Added to T24's `orchestrator.next_suggestions` row 79 (v2; backup `backups/T24_next_suggestions_before_20261006.json`):
  a refusal is not a question; no "Yes, please", no re-ask, no "share the number".
- Fetched the way the widget does, 4/4 cab runs showed only useful chips (meeting rooms, early check-in,
  wheelchair rooms, distance from T2).

**Open, a judgement call:** "Yes, please check for me" right after the transfer answer still opens the team form.
The bot had asked "which property… for your stay?", so it reads this as checking a stay. Treat it like the cab
only if the user says so.

### 7 Oct — booking buttons on "room service?", "pass it on" offers, "Yes, please pass it on" opened nothing

**Root cause: chip taps were pinned to the Room Booking agent.** The chip agent tags each chip with the service
that answered the turn before. After any room answer, every chip ("Do you have room service?", "Is there a shuttle
service to Terminal 2?", "Yes, please pass it on") carried target = room_booking_request. The chip lock in
`_apply_chip_click_route_lock` then overrode the router.
- The router had decided correctly each time: `book` for room service, transport and shuttle; `service /
  support_request` for "Yes, please pass it on".
- Room Booking's pulled KB is rooms only (13.7k characters, no dining or transfers). So it answered "no room service"
  and "no shuttle" (both wrong), offered "I can pass it to our team", and every reply carried the booking buttons.
- "Yes, please pass it on" got "I have noted your request…" with `ticket.required: false`: no form, no ticket.
- The same lock runs in the chip pre-cook (`prefetch::` sessions). Most of the tester's chip answers were served
  from that cache.
- Pull from KB was done: Room Booking has the rooms, and Support has meetings/events, contacts and group/corporate.
  There is no separate event service; hall and event enquiries go to Support.
- Zone keeps the pin harmlessly: its room agent's pulled KB holds almost everything (111k characters, including
  dining and airport).

**Fix (code, admin-managed hotels only; Zone, Mango and Sarovar keep the pin):** when the router actually decided
the turn (book/service with a confidence), its route stands for a chip tap too. Only an undecided turn keeps the
pin. One block in `services/llm_orchestration_chips_mixin.py`.

**Replays after the fix (WebSocket, tester's exact chip tags; scratchpad `t24_chip_lane_replay.py`,
`t24_passon_replay.py`):**
- room service chip → main answerer, "in-room dining 7:30 AM to 1:00 AM", no buttons.
- local transport chip → transfer rates, metro and bus stop, no team offer, no buttons.
- "Yes, please pass it on" chip → team form opens.
- booking chip → still Room Booking with the buttons.
- typed cab → "cannot arrange… +91 70455 93059", no buttons, no "Yes, please" chip.
- `is_admin_managed`: T24 True; Zone, Mango and Sarovar False.

**Seen, not fixed:**
- Room Booking answering a room detail it lacks ("grab bars?") says "I don't have specific details" with no number
  and no offer. The main answerer gives the number plus a team offer.
- The main answerer's offer line can end in "?." (a doubled final punctuation mark).

### 7-8 Oct — in-stay requests declined, wording in the DB, 4.1-mini, dev .env

**What the tester hit:** "i saw one rat in my room" and "my room view is not good" opened the Complaint form. "my
room door is locked" got "our team will assist shortly", and was written onto an open support ticket as an edit.
"i need knife now" offered "would you like me to put you in touch?". Past-stay complaints opened the form.

**Rules now (user):** the bot says plainly what it cannot help with and what it can. Complaints about a stay, current
or past, are not taken. All T24 wording lives in the DB, never in code.

**DB (dev), T24:**
- book_answerer row 77 v10:
  - QUESTIONS are always answered.
  - A request to do or arrange something, a room/stay problem, or a stay complaint gets exactly: "I'm sorry, I can't
    help with this request. I can help with information about T24 Residency and T24 Retro, room bookings, and
    meeting-hall or event enquiries. For this, please contact the hotel directly at **+91 70455 93059**." This line
    replaced the 14 "That's available only once you're checked in" lines.
  - Booking change or cancel: number + email.
  - Airport-transfer rates quoted exactly. The KB is right (T2 drop 350, T1 drop 450, pickups 750); the bot had
    merged them.
- Services 103 and 104 carry Zone's stay-stage paragraph verbatim (a stay "already underway or already finished" is
  out of scope for the form), and 104's prompt says the same. The Complaint form still opens for "I want to complain
  about the hotel" and for complaints about an enquiry or booking.
- Backups: `backups/T24_instay_decline_before_20261007.json`, `backups/T24_decline_pastStay_before_20261008.json`.

**DB (dev), defaults for every Pre-booking-tab hotel** (industry `hotel` rows, read only when `is_admin_managed`):
`reply_style.no_trailing_offer`, `reply_style.give_number_when_cannot_arrange`, `reply_style.emoji_none`,
`reply_style.emoji_limited`, `ticketing.edit_router_hotel_rules`, `scope.refusal_unsafe` ("I'm sorry, I can't help
with this request."). Code only fetches them, and Zone, Mango and Sarovar keep their code wording byte-identical. The
3 Oct size line in Zone's sentence was reverted; T24's copy keeps it.

**Model:** `.env` `CHAT_LLM_PREBOOKING_HOTELS_MODEL=gpt-4.1-mini` pins T24 and future Pre-booking-tab hotels to 4.1-mini
for every role (`model_override_service._prebooking_hotel_model`). Existing hotels keep prod's split (answers and chips
on 5.4-mini, Mango pinned to 4.1-mini).

**.env:** the prod values, with only the 17 environment keys from `.env.dev`: DB host, ports, `APP_ENV`/`DEBUG`,
secret keys, the dev ticket and handoff URLs, the dev entity/widget/department maps, and the log tag. The prod copy is
`.env.backup_prod_20261008_125651`.

**Verified in the browser (Playwright, 8 Oct):**
- Knife gets the refusal only. Cockroach, Wi-Fi, rat, burger, towels and the past-stay complaint get the decline
  line, with no form.
- Room service, dining, pool, gym and laundry are answered.
- Rates are exact. The cab gets "can't arrange + number". Cancel booking gets number + email.
- The Complaint form opens, and opens again after a cancel. Support opens for "call customer care". The hall offer
  followed by "yes" opens Support.
- Every T24 role ran on gpt-4.1-mini. Zone and Mango were unchanged in a smoke test.

**Open:**
- "Do you have housekeeping?" gets the decline: T24's KB has no housekeeping line (ask the client).
- In a long chat, "book your meeting hall" sometimes gets the offer instead of opening the form directly. This is the
  router's own hall rule; "yes" opens the form.

**Before prod:** create the industry rows above and copy T24's rows 77/81 and services 103/104 to prod, then add
`CHAT_LLM_PREBOOKING_HOTELS_MODEL` to the prod `.env`.

### 8 Oct (pm) — booking buttons vanished, "cancelled" was not true

**What the tester hit** (chat `session_1791449631876_cur8ssah`):
- After sending "Get help from our team" for Retro, later booking replies had no buttons: "book room here", "book a
  room at Residency", and "Done, I've cancelled… via the website link below".
- The Retro request (#11848) stayed open on the dashboard.

**Causes (logs):**
- The widget's "details already sent" flag hid both buttons for the rest of the chat. That is Mango's single-hotel
  rule.
- "id lik to book room hr" right after a venue talk was routed to the general answerer. Its reason: "meeting room
  booking is not handled by room_booking_request".
- The property-switch cancel sent `status: cancelled` with no reason. KeBo answered 400 "Cancellation Reason is
  required…", and the reply claimed success anyway.

**Fixes:**
- **Code, Pre-booking-tab hotels with 2+ properties (Zone's way):** an earlier submission no longer hides the
  buttons, so every booking reply gets "Book on website" for the hotel in focus and "Get help from our team".
  Single-property hotels keep Mango's rule.
- **DB, T24 router row 78 v5:** "a room" is a guest room, read from the conversation; it is the venue only when the
  guest names it or says "book that room" right after the venue answer.
- **Code, Pre-booking-tab hotels:**
  - The room-booking cancel sends KeBo's own value `cancel` (552 tickets use it; `cancelled` was last used on 8234)
    plus `cancelled_notes`, cleaned the same way as the ticket-edit cancel.
  - The reply says "cancelled" only when the cancel landed. Already cancelled by the edit check earlier in the same
    turn counts as landed.
- **Booking links checked against the client KB:** Residency `propertyId=9885`, Retro `9956`, `chainId=9884`.

**Verified (headless browser on the real widget page, plus KeBo's `GHN_OPERATION_TICKETS`):**
- Both buttons, with the right hotel's link, on all four booking replies.
- #11855 (edit-check cancel) and #11856 (the switch path itself) show `cancel` with the reason.

**Test tickets still open:** #11852 and #11854 (mine), plus #11848, #11849 and #11850 from the tester chat.

### 9 Oct — menus without being asked

**Asked:** when the guest talks about dining, show the menus of the hotel in focus without waiting for "send me the
menu", and let the admin choose which topic does it.

**Admin (no code per hotel):** Pre-booking §4 → "Show the menus without being asked" lists the hotel's KB topics
(active books, one per slug). Ticked topics are saved as `menu_topics` with "Save buttons and reply settings". T24:
**Dining & Restaurants** (`dining_restaurants`, both hotels).

**Rule (code, Pre-booking-tab hotels only):**
- The menus go out when the router's **first** book for the answer is a ticked topic. A dining question opens Dining
  first; "tell me about the hotel" or a typed hotel name also open it, but 3rd to 6th, so they get no menus. The
  retrieval set is never read (see the Aug menu misfire).
- Which menus: the hotel in focus plus the all-hotels ones; every hotel's when none or both are in focus. T1 = Blend;
  T2 = Toast food, Toast bar, in-room dining.
- Once per conversation per menu (`_menu_docs_sent`, cleared on a new conversation). "Send me the menu" always
  sends.
- Chip answers from the prefetch cache keep the router's books (`router_book_ids`), so they follow the same rule.

**Verified:** WS runs plus the real widget through the tester link: dining (both hotels) → 4 menus; Terminal 1 → Blend;
Terminal 2 → 3 Toast; breakfast after that → none; pool, transfer, rooms, hotel overviews → none; explicit ask → all
again; a prefetched chip carried the books. The chip/scope test suites have the same 10 failures with or without
this change.

**For the user to decide:** "Can I order food to my room now?" gets the in-stay decline **plus** the menus (the router
files it under Dining). On one explicit ask the answer text said "you can request the menu and we will share it"
right above the attached PDFs (KB/prompt wording; a rerun said "I can share the menus").

### 13 Oct — "i want to book room" in a new chat got a bare "Which property would you like to book?"

**Tester (Suhas, `session_1791865079744_a080zrsh`):** after the dustbin (new conversation, no hotel chosen) "i want
to book room" got only that line and "Get help from our team".

**Cause (logs):** the Room Booking agent had answered well - both hotels, their rooms, "which hotel?" (its DB prompt,
service 102, already says so). `_maybe_manage_room_booking_ticket` step (3) then swapped that answer for the fixed
`_rb_property_choice_reply` whenever no single hotel is in focus; in a fresh conversation it knows no hotel names, so
the line came out bare.

**Fix (code, Pre-booking-tab hotels only; Zone/Sarovar keep the fixed line):** keep the agent's answer when no hotel
is chosen. Still no ticket until a hotel is chosen.

**Verified (browser through the tester link):** dustbin -> "i want to book room" -> both hotels with room types ->
tap the Retro chip -> Retro rooms + Book on website (9956) + Get help from our team. No ticket from the test chats.

### 13 Oct (pm) — new chat "can i get food menu" answered for T24 Retro only

**Cause (router trace):** after the dustbin the router got `active_property_ids: []` and no history, but
`last_assistant_message` was the PREVIOUS chat's last reply (about Retro), read from the whole session; its reason:
"User specifically mentioned T24 Retro in last assistant message". `last_assistant_service_id` had the same leak.

**Fix (code, Pre-booking-tab hotels only):** both read the current conversation's messages
(`_current_thread_messages`, the same thread filter `recent_messages` already used). Verified in the browser: Retro
question -> Blend menu; dustbin; "can i get food menu" -> both hotels and all 4 menus; router given
`last_assistant_message ''`, chose both hotels.

### 14 Oct — T24 copied to the PROD DB (new_bot_* tables only); prod is now T24's home

User: allow all origins (UAT), prod only from now on (no more dev for T24), new_bot tables only, no ticket config.

**Copied (one transaction, ids kept, content checksums identical to dev):** new_bot_hotels 71 (T24), properties
372/373, kb_files 2, kb_books 63, kb_pages 69, kb_book_pages 92, services 102-104, business_config 11 (all but
`json_section.ticketing`), location_intros 3, widget_deployments id 3 (`wk_3aaaHPe7TIB2Vobu0Jb9n-pO`, origins `*`,
gpt-4.1-mini), prompt_registry 6 T24 rows + 6 shared rows (reply_style x4, scope.refusal_unsafe,
ticketing.edit_router_hotel_rules; read only for Pre-booking-tab hotels) as prod ids 78-89.
Script: scratchpad `t24_copy_dev_to_prod.py`. Dev rows: `backups/T24_dev_rows_copied_to_prod_20261014_134356.json`.
Undo: `backups/T24_prod_copy_UNDO_20261014_134356.sql`.

**Not copied (by request / not new_bot):**
- `json_section.ticketing`: dev FMS 6348/6349 are OTHER clients in prod. Prod T24 = Residency FMS 6622 / RI 10973,
  Retro FMS 6621 / RI 10972, group 7623; no prod widget row yet. Until routing is set, T24 tickets stay LOCAL.
- `bot_hotel_images_scraper` (90 photos; 17 dev ids taken in prod -> needs new ids).
- KeBo prod rows missing for T24: BOT_IDENTIFIER_INFO, DEPARTMENT_STAFF_CONFIG, GHN_DEPARTMENT_ALERT_MAPPINGS,
  GHN_FF_TICKET_NOTIFICATIONS, lead form GHN_CHATBOT_FORMS (Bot_Id 71).
- Test traffic (conversations, tickets, sessions, guests, logs).

**Before T24 can chat on prod:** deploy engagefeature code, add `CHAT_LLM_PREBOOKING_HOTELS_MODEL=gpt-4.1-mini` to
the prod .env, restart.

**Later 14 Oct:** photos (90, prod ids 4098-4187) and the lead form (GHN_CHATBOT_FORMS prod ID 69, Bot_Id 71) copied;
undo `backups/T24_prod_photos_leadform_UNDO_20261014_135542.sql`.

### 14 Oct — prod ticketing for T24, Suhas only (UAT), built like dev

User: use prod's departments (Reception Front Office = Front Office), create what is missing (Sales), tickets to
Suhas only, no hotel staff yet. Templates = Mango prod's own rows; dev's department ids could not be reused (dev 43 =
prod "Home Catering", 44 = "Corporate Booking", 34 = "Dessert Lab", 35 = "IRD").

| Prod row(s) | What |
|---|---|
| DEPARTMENT_MASTER **50** | new "Sales" |
| BOT_IDENTIFIER_INFO **11** | T24 widget: group 7623, org_id `10973,10972`, T24_1, Trisha, https://www.t-24.in/, bot_config = dev 62's |
| GHN_USER_ORGANIZATION_MAPPING 347074-347075 | Suhas (11860) sees 10972 + 10973 |
| DEPARTMENT_STAFF_CONFIG 2429-2442 | Suhas MANAGER, HIGH, 9:00:00-8:59:59 on 7 depts x 2 orgs |
| GHN_DEPARTMENT_ALERT_MAPPINGS 1344-1357 | auto-assign, escalation_1 Suhas, SLA 20 (Mango 1341 shape), 7 depts x FMS 6621/6622 |
| GHN_FF_TICKET_NOTIFICATIONS 1360-1381 | Mango's 11 templates per hotel |
| new_bot_business_config **6784** | T24 ticket routing: widget 11, group 7623, Residency FMS 6622, Retro FMS 6621 |

Departments (prod master): reservations 48, Sales 50, Reception Front Office 1, complaints 49, Banquet 38, Food and
Beverages 39, Housekeeping 3. Senders already in prod: WhatsApp 919324972779 on both FMS, email feedback@t-24.in
(verified). Verified with the bot's department lookup: both FMS ids resolve all 7 departments with Suhas as manager.
Undo: `backups/T24_prod_ticketing_setup_UNDO_20261014_154255.sql`.
**Test after the prod deploy:** one booking + one complaint per hotel -> T24 dashboard, right dept, auto-assigned to
Suhas, WhatsApp + email arrive, ref not LOCAL-*; then close them.

### 14 Oct (eve) — "i want to book room" with no hotel chosen, and cancels (Suhas on prod)

Not a prod data problem: every T24 table, prompt row, service, photo and form is identical on dev and prod, and the
dev server (`dev-environment`) gave the same replies in the browser. Same input, two lanes, both broken:
- Router -> general answerer: "You can complete your room booking on our website…" plus a button per hotel; with two
  hotels in focus the code removed every button, so there was nothing to click (Suhas's screenshots).
- Router -> room-booking agent: a good answer about both hotels, but its JSON picked T24 Residency on its own; that
  became the hotel in focus, a booking ticket was raised for Residency (prod #14405, dev #11871, from my test) and the
  reply became "Great choice! … T24 Residency".
- Cancel: "booking room cancel that ticket" matched #14404, a second check dropped it, and the bot said "has been
  noted" with nothing cancelled; answers to our own "Which one would you like me to cancel?" were not read as answers.
  Mango #14390 was cancelled on 9 Oct in Suhas's Mango chat, not by this one.

Fixed (code gated on admin-managed hotels; Zone, Mango, Sarovar unchanged):
- An agent may not narrow to one hotel when the router kept several on that turn.
- DB row `router.hotel_rules` (hotel default, read last by the router): book a room with no hotel -> room-booking agent.
- Two+ hotels in focus and the reply offers the booking link -> one "Book on website · <hotel>" button per hotel.
- The "which one?" list is kept one turn and the reply is read against it (DB row `ticketing.cancel_menu_pick_rules`);
  a dropped match asks instead of claiming; a cancelled booking leaves `_rb_bookings`.
- Web addresses and emails no longer get spaces ("https://www. t-24. in/").
Dev rows 89-90 (undo `backups/prebooking_rule_rows_dev_UNDO_20261014_204109.sql`). Prod needs the same two rows.

### 15 Oct — Suhas on prod: chips with no form, other cities filed to our hotels, venue on the wrong hotel

Prod runs `latency` 1df32d4, which has none of the 14 Oct (eve) fixes or these; T24's data is identical on dev and
prod. Root causes from the prod logs, fixed (code gated on admin-managed hotels; Zone, Mango, Sarovar unchanged) and
retested in the browser on the real UAT page and the harness (local code against the prod DB):
- "Yes, please have the team contact me" / "connect me to the front office" chips said "please provide details" with
  no form: the 5 Oct prefetch guard read a key chat_service never writes (`orchestration_action` lives under
  `metadata.entities`), so form answers were cached and replayed as text.
- Support/complaint with both hotels in focus was filed to Residency (#14414): the first hotel no longer counts as
  chosen; the gate asks "Which hotel is this about — …?" (DB row `ticketing.support_property_ask`, no "Sorry to hear
  that").
- A hotel tapped after "which hotel?" showed its intro, also through chip pre-cooks: it now continues the request,
  and chips are not pre-cooked while a hotel answer is awaited.
- Other cities (Bangalore, Mysore, Kerala) were filed to the hotel in focus (#14420, a Retro support ticket): a
  question without a form step no longer opens the form; an agent naming no hotel, or the router choosing none while
  one is in focus, makes the gate ask; router line "a hotel or place that is not ours is not the hotel in focus".
- "book one workhard venue" with Retro in focus was answered from Retro's Converge Hall: the router now also reads
  `router.hotel_facts` (T24 row: which hotel each venue, restaurant and room belongs to; checked against the KB).
- A lone "•" on one-line declines is dropped; the greeting is no longer stuck on "Hi! How can I help you today?".
Prod DB rows added 15 Oct (undo files in `backups/`): `ticketing.support_property_ask` #92, T24 complaint prompt +1
line (service row 104), `router.hotel_rules` #90 +1 line, T24 `scope.refusal_other_business` #93, T24
`router.hotel_facts` #94. Every future pre-booking hotel needs its own complaint line, refusal and facts rows.
Test tickets from these checks, left open (user): #14424 (complaint, Retro) and #14410.

### 16 Oct — UAT page: other-city complaints still reached our hotels, "Yes" chip missing

Suhas on the UAT page, 15 Oct evening (a new chat; T24 Residency in focus because he picked it). From the prod logs:
with one of our hotels in focus the router chooses no hotel only for a place that is not ours (all 29 such T24 turns,
13-15 Oct), and our code undid that choice:
- "mysore staff is too rude": the book lane put the hotel in focus back, the answerer got only Residency's contact
  page and offered our team, and "Yes, please" filed #14436 to Residency. The harness ran the same step and declined
  by chance, so this was not a UAT-page problem.
- "refund for my booking in a Jammu hotel": the support lane asked "which hotel — Residency or Retro?"; sent again,
  the message was read as answering that with the hotel in focus and a Residency form opened.
- "Yes, please have them contact me" was dropped by the chip evidence check ("them"/"contact" not in the question).
- Harness: "book one workhard venue" was refused as another business (the scope check knew only hotel names).
Fixed in code (admin-managed hotels only, no DB rows): the router's "no hotel" stands - every hotel is loaded for the
answer and a support/complaint request is answered, never sent to the form; a hotel tapped after our "which hotel?"
continues that request; a yes/no chip resting on the bot's own question is kept; the scope check reads
`router.hotel_facts`. Suhas's chat replayed locally against the prod DB (tickets stubbed): all of it now correct.

### 16 Oct (later) — one Support form like Zone; stay complaints no longer reach a form

UAT, 16 Oct morning: "room is not clean" opened the Complaint form, "i lost my room key" and lost luggage the
Support form, and "mysore staff is too rude" + "yes connect me" the Support form. The UAT page and the harness run
the same code (the embed loads `/chat`, same request, byte-identical router prompt); the harness chat was new, the UAT
chat had already filed three such forms and the router followed them. Cause: the router row's support examples
include stay problems ("room is dirty", "refund not received") in every phase, and the hotel's last rules said
nothing against it.
Prod DB, 16 Oct 11:53 IST (user OK'd; before-values in `backups/T24_support_only_BEFORE_20261016_115352.json`):
- service 104 Complaint switched off (Zone shape: Support + Room Booking only);
- service 103 description: the sentence sending complaints to the Complaint service removed;
- service 103 prompt: a complaint about how the hotel handled an enquiry or booking opens the form; a stay underway
  or finished gets the decline line (both lines copied from 104);
- `router.hotel_rules` #90 v3: + "A PROBLEM WITH A STAY NEVER GOES TO THE SUPPORT SERVICE ..." (only Pre-booking-tab
  hotels read it).
Code (8ae9e6a): router chose no hotel while one of ours was in focus -> the hotel's decline
(`scope.refusal_other_business`), nothing raised.
Logged router inputs replayed offline: stay problems declined 72/72, pre-booking complaints to the form 57/60. On the
UAT page (local code, prod DB, tickets stubbed): the three screenshots are declined with no form; "I want to raise a
complaint" -> which hotel -> Support form; meeting-hall "yes" -> form. Live needs the deploy + a restart (services
are cached per hotel).

## 3. Still open

1. **GHN provisioning for T24 (dev DB, someone else; blocks real tickets)** — see §5. Then enter the FMS ids (§6) and
   widget/group ids (§7). Until then every T24 ticket stays LOCAL — never routed to another hotel (user, 3 Oct).
2. Close the two test tickets on ROHL's dashboard (#11812, #11813).
3. **Escalation** (later, per the user): hours 9:30 AM–7:30 PM; handoff message from the Gap Report; triggers.
4. Prod is out of scope until dev training is complete (user, 2 Oct).
5. Client questions (workbook only):
   - **OTA 10% "valid until 30 September" has passed** — new validity, or remove?
   - Direct booking 10% vs Retro "12% on website bookings" (the two Gap Report copies say 10% and 12%).
   - Residency airport transfer: the DataSheet still has the template answer ("Rs 900 one-way OR NOT AVAILABLE"); we
     use the OnBoarder rates (750 / 350 / 450, larger car 1,000 / 500).
   - Club Room bed King vs Queen (we say queen); Retro Deluxe 180 (DataSheet Q9) vs 206 sq ft (everywhere else; we
     say 206).
   - Earlier: landline 8305 vs 8303/6693 0761; Retro EV; Retro–T2 distance; CRM number; group minimum; Residency
     outside food; emoji No vs Limited; "ATIKA"; Cutting Cocktail menu missing; 3 signature dishes not on the
     revised Blend menu; UAT URL.
6. Left for later (user, 16 Oct, not harmful): in a new chat "cancel my booking and refund" cancelled the previous
   support ticket (#14431, #14433); after the answerer's own "which hotel?" (meeting hall) a tapped hotel shows its
   intro instead of continuing.

## 4. Runbook for every future pre-booking hotel (admin only)

1. Setup Wizard → new code, business info, greeting, colours.
2. RAG → upload KB txt files → Reindex (then `tools/rebuild_kb_canonical_books.py` for the 13 standard books).
3. Pre-booking §6 Properties → names, **picker labels** (same label = one location), aliases.
4. Forms → lead form (drop the city field when there is only one location).
5. Phases → services (Room Booking / Support Request / Complaint like T24) → Pull from KB → lock the prompt.
6. Pre-booking §1 opening buttons + after-form choice; §2 messages; §3 service buttons and per-hotel links;
   §4 menus; §5 reply style. Save.
7. Pre-booking §7 + §6 FMS ids → ticket routing.
8. Escalation → hours, handoff message.
9. Widget → deployment (model, phase, allowed origins).
10. Test with the client's question sheet.

## 5. Dev DB provisioning for T24 tickets (to be done by someone else; nothing exists yet)

Template = **Mango Grove on dev** (FMS 6345 / RI 8725), whose tickets reach its dashboard and auto-assign. Every
table below holds Mango rows; copy their shape. Read-only checks, 3 Oct.

**Once for the group**

| Table | Rows | Mango example |
|---|---|---|
| `GHN_FF_PROD.GHN_FF_ENTITY_GROUP` | 1 — "Hotel Transit Private Limited" (OnBoarder "Hotel Brand / Group") | 5306 |
| `GHN_FF_PROD.GHN_FF_ENTITY_BRAND` | 1 (Entity_Group_Id = the group) | 5503 |
| `GHN_PROD_BAK.GHN_ORGANIZATION_GROUP` | 1 | 6744 |
| `GHN_PROD_BAK.GHN_ORGANIZATION_BRAND` | 1 (ORGANIZATION_GROUP_ID = the group) | 930006790 |

**Per hotel (× 2)** — names as the client's OnBoarder "Property Name" (typo "Andehri" fixed):
**T24 Residency, Andheri East, Mumbai** (Marol Church Road, Near Airport Road, Bori Colony, Andheri East, Mumbai,
Maharashtra 400059) and **T24 Retro, Vile Parle East, Mumbai** (Nehru Road, Near Terminal 1, Vile Parle East,
Mumbai, Maharashtra 400099); phone +91 70455 93059, email reservation@t-24.in for both. The GHN names do not have
to match the bot's display names: tickets route by the FMS id entered in Pre-booking §6.

| Table | What | Mango example |
|---|---|---|
| `GHN_FF_PROD.GHN_FF_ENTITY` | the **FMS id**: name, Entity_Type_Id 1, Industry_Id 1, brand + group, GEO_Area_Id (Mumbai), Status Active, phone, email | 6345 |
| `GHN_PROD_BAK.GHN_ORGANIZATION` | the **RI id**: full name, type 1, brand, GEO_AREA_ID, ENABLED 1, INDUSTRY_TYPE_ID 223, address | 8725 |
| `GHN_FEEDBACK_RESULTS.GHN_SURVEY_FMS_RI_ENTITY_MAPPING` | FMS ↔ RI — **the bot reads this** | 6345 ↔ 8725 |
| `GHN_FF_PROD.GHN_FF_CONTRACT` | Active contract, start/end dates | 5676 |
| `GHN_PROD_BAK.GHN_CLIENT` | ORGANIZATION_ID = RI, ENABLED 1 | 8252 |
| `GHN_FF_PROD.GHN_FF_DEPARTMENT` / `GHN_PROD_BAK.GHN_DEPARTMENT` | default department sets (Mango 58 / 22 rows, all created in the same second → by the onboarding tool; create the same way) | |

**Staff and ticket routing (both hotels)**

| Table | What |
|---|---|
| `GHN_PROD_BAK.GHN_USER` + `GHN_USER_ROLE_MAPPING` (role 3 GHN_CLIENT_ADMIN) + `GHN_ROLE_FEATURE` (like Mango's users, 43–49 features) | at least the dashboard login **Pawan Kumar, CRM, crm@t-24.in**; optionally one user per department mailbox below |
| `GHN_PROD_BAK.GHN_USER_ORGANIZATION_MAPPING` | each user → both RI orgs (dashboard visibility) |
| `GHN_PROD_BAK.DEPARTMENT_STAFF_CONFIG` | per RI org × department × staff: **MANAGER 1**, STATUS 1, ISAVAILABLE 1, TICKET_PRIORITY, shift times. **The bot reads this** (through the FMS↔RI row) to pick the department and agent — no MANAGER=1 row, no department |
| `GHN_FF_PROD.GHN_DEPARTMENT_ALERT_MAPPINGS` | per FMS × department: ticket_auto_assign 1, is_active 1, escalation_1/2/3 = a staff user id, escalation_interval 5, before_sla_default 70 (Mango). Without these, tickets do not auto-assign (Mango, 29 Sep) |
| `GHN_FF_PROD.GHN_FF_TICKET_NOTIFICATIONS` | 10 templates per hotel (ENTITY_ID = RI, FMS_ENTITY_ID = FMS): created (Dept Head, staff), assigned (staff), sla_breach (Dept Head), reminder_beforesla (staff), resolved (Dept Head, staff), cancelled (staff, Dept Head, guest) — copy Mango's ids 501–510. Without these, no alerts |

Departments (DEPARTMENT_MASTER id → client's contact): reservations **43** (reservation@t-24.in, 70455 93059) ·
Sales **35** (sales@t-24.in, 93248 02583) · Front Office **5** (Residency reception@t-24.in 93249 72782; Retro
reception@hoteltransit.in 82918 58469) · complaints **44** (crm@t-24.in, 70459 02798) · Banquets **1** and
Food & Beverage **34** (fnbsales@t-24.in, 83559 43288) · Housekeeping **8** (Residency housekeeping@t-24.in
93249 72776; Retro housekeeping@hoteltransit.in 82918 58445). The client wants CRM (70459 02798) to get every ticket.

**Widget row (checked 5 Oct against Mango prod bot 10 and Zone dev bot 61):** `GHN_PROD_BAK.BOT_IDENTIFIER_INFO`
one row: web_url https://www.t-24.in/, **group_id 6746** ("T24", the RI group of 8726 + 8727; Mango prod 7598 and
Zone dev 139 are likewise their hotels' RI group), org_id `8727,8726`, client_id `T24_1`, bot_name Trisha, idle
10/10 (Mango prod), web_search/hospitality_domain_search/group_kb 0/1/1 (dev-only columns, Zone dev values),
bot_config = Mango's JSON with Trisha's name. Next id on dev = 62. Its id + 6746 go into Pre-booking §7, which
switches on: every turn mirrored to KEBO_ENGAGE_SESSIONS (dashboard transcript), the guest row in
GHN_CHAT_BOT_CUSTOMER_DETAILS, widget_id/group_id on each ticket, live-agent takeover. Not needed: the bot's
GHN_CHATBOT_FORMS rows (Mango prod 1, Zone dev 4) and WIDGET_CTA (Zone dev 1) belong to Kepsla's own widget. Our
rows carry Form_Id NULL (47/47 on Mango prod), and our lead form is form 95 (Bot_Id 71 = our hotel id).

**DONE 5 Oct on dev (user OK'd): SuhasR@kepsla.ai, copied from his Mango PROD setup (prod user 11860).** Dev's
other Suhas (ratakondasuhass@gmail.com, 6425) is Mango dev's and is untouched.
- Account = **dev user 6440**: GHN_USER copy of 11860 (same password hash, mobile 918123242009, ROLE Admin); role 3;
  org access 8727 + 8726; 8 dashboard features mapped **by page**, because dev numbers the newer KeBo pages differently.
  Mapping: 1, 114, 115, 117, 118, 123 unchanged; prod 136 "Pre Booking Tickets" (/booking/ticket-list) → dev 143;
  prod 149 "Engage Active Tickets" → dev 146 "Pre Booking Tickets". Dev 149 is "Review Generator", a different page.
- DEPARTMENT_STAFF_CONFIG ×14 (2 orgs × 43/35/5/44/1/34/8), as prod 2423–2425: MANAGER 1, HIGH, 9:00:00–8:59:59.
- GHN_DEPARTMENT_ALERT_MAPPINGS ×14 (FMS 6349/6348), as prod 1341–1343: auto-assign, escalation_1 = 6440, SLA 20.
- GHN_FF_TICKET_NOTIFICATIONS ×22 = prod 1349–1359 per hotel (text identical).
- Our admin: FMS ids saved (Residency 6349, Retro 6348), so T24 tickets now go to T24 on dev, not LOCAL.
- Proof: test complaint **#11824** (session qa_t24_route_5ad357d4) → RI 8727 / FMS 6349, complaints (44), HIGH,
  auto-assigned to 6440. The opening lead form created no ticket.
- Undo: scratchpad `t24_suhas_dev_setup_undo_6440.sql` (7 DELETEs).
- **Widget done 6 Oct: BOT_IDENTIFIER_INFO id 62** (undo: `DELETE ... WHERE id=62`), made like Mango prod 10 / Zone
  prod 9: group 6746, org_id `8727,8726`, client_id T24_1, Trisha, idle 10/10. bot_config is Mango's with only the
  bot name changed; Zone's is Mango's too, apart from the name. Pre-booking §7 is saved: widget 62, group 6746.
- Proof, test **#11825** (session qa_t24_engage_6234c134), same shape as Mango/Zone prod tickets: group_id 6746,
  message_id = the guest's chat line, assigned to 6440. 8 chat-history rows (KEBO_ENGAGE_SESSIONS) with widget 62 /
  group 6746, and the guest row in GHN_CHAT_BOT_CUSTOMER_DETAILS. #11824 (5 Oct) predates the widget and has no group.
- Not made, because the user wants tickets and chat history only and our code never reads these:
  - Mango's widget form 14: a Sarovar "SIA" clone carrying an api_key and Kepsla staff emails. Zone prod has none.
  - Zone's GHN_CHATBOT_INFO row: Kepsla's own bot prompt per group, holding "test" text. Mango has none.
  - Zone dev's WIDGET_CTA row.
  - Mango's mail sender row (GHN_FF_ENTITY_EMAIL_MAP 695): T24 has no verified address.
- WhatsApp works with prod's alert shape: #11824 sent 2 "Remainder" messages to 6440 at 918123242009, both
  SUCCESS, about 6 and 8 minutes after creation (6:14 and 6:16 PM IST). Escalations follow after the 20-minute SLA
  while the ticket stays open. Email can only be confirmed in his inbox.

- **Ticket list showed only #11824/#11825 (fixed 6 Oct): the dashboard's hidden Type filter, not a user filter.**
  KeBo Engage → Tickets List (`/booking/ticket-list`) is feature **143** ("Tickets List" / "Pre Booking Tickets").
  Its DEFAULT TICKET_CARD config (DASHBOARD_FEATURE_CONFIG row 11) marks the Type column filterable with options
  complaint / request / upsell / enquiry. With nothing selected, the page still sends
  `categorization=complaint,request,upsell,enquiry`. Our bot files "room booking request" or a department name
  (Housekeeping), the same as Mango and Zone prod, so only the two "complaint" tickets came back. Kepsla's own KeBo bot
  files "Request" / "Complaint". Prod's older dashboard has no such filter, which is why Mango and Zone show
  everything there.
  - Fix: row **134** = TICKET_CARD for feature 143, scope GROUP 6746, a copy of row 11 with only
    `type.filterable=false`. `/dashboard/dashboard-config/143.htm?userId=6440` now reports TICKET_CARD: GROUP, and
    the list returns all 12. Other groups still use DEFAULT.
  - Undo: `DELETE FROM DASHBOARD_FEATURE_CONFIG WHERE ID=134`. Script: scratchpad
    `t24_ticket_list_type_filter_fix.py`.
  - The same default hides our bot's tickets on Mango dev (16 of 16) and Zone dev (19 of 21). It will hit prod too
    if this dashboard build ships there with that default.

**Not needed by the bot:** GHN_KPI_ORGANIZATION_MAPPING, GHN_ORGANIZATION_SOURCE_MAPPING, GHN_FF_ENTITY_SOCIAL_LINKS,
GHN_FF_ENTITY_COMMUNICATION_STATS (reviews / feedback product).

**Then on our side (admin, no code):** Pre-booking §6 → each property's FMS id; §7 → widget/group ids. From that
moment tickets go to T24's own dashboard instead of LOCAL.
