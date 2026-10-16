# Mango Grove — pre-check-in, during-stay and post-checkout bot (plan v3, full check)

> 12 Oct 2026. Nothing built yet. Work happens on **DEV** (`.env` = dev DB 172.16.5.32, port 8012, checked). Prod was
> only read. The dev DB stopped answering during the last check (VPN), so the dev department/staff check in §6 is
> still pending.
> Source: the client's files in `kepslasw\mango\`; KB = `Mango_Grove_Chandigarh_Stay_KB.txt`.
> Rule: everything is trained from the admin, the way T24 was. Services are made in the **Phases tab** (pick a
> phase → create its services). No new tab, no per-hotel code.

## 0. A separate bot, not stay services on the live Mango

If stay services went into the live Mango (hotel 69): services of every phase are shown to the bot on every message,
so the website bot's answers change; Mango's prompt says "WHO YOU ARE (pre-booking)" and declines housekeeping, room
service and AC problems, so the stay bot would refuse stay requests; and the stay KB and Mango-only `.env` switches
would apply too. A new hotel leaves hotel 69 untouched.

## 1. How a guest goes through it

1. Booking confirmed → the KePSLA platform sends Mango's WhatsApp messages at each stage (pre-check-in "tell us your
   special requests", during stay "reply with any request", post-checkout survey + call us).
2. The guest replies → our bot answers. It knows the phase from the guest's stay dates in GHN. The PMS feed is
   live on prod: 1,680 Mango guests (19 arriving, 94 in house).
3. Questions → answered from the KB. Requests → a service (form or chat) → ticket to the right department → staff
   alert → escalation if it stays open.
4. **If the guest's dates are not found**, today the bot falls back to `pre_booking`. The stay hotel has no
   pre-booking services, so it would refuse every request → a per-hotel **default phase** is needed (§4).

## 2. What is set where (admin)

| Area | Admin page | Mango value | Today |
|---|---|---|---|
| Hotel, bot name, greeting, colours | Setup Wizard | "Mango Grove Assist"; "Sir/Madam. How can I assist you today?"; #003F34 / #F79558 (as Mango 69) | exists |
| Image gallery tool | Setup / Tools | on, web + WhatsApp (as Mango 69) | exists |
| KB | RAG tab → upload → Reindex → 13 standard books | the stay KB | exists |
| Prompts | Prompts tab (hotel scope) | copies of Sarovar's stay prompts (main_router, book_answerer, response_surface, answer_first_guard), Sarovar names/facts removed; chips prompt from T24 | exists |
| **Default phase when dates are unknown** | Phases tab, top card | During Stay (to confirm) | **new** |
| **Opening buttons + opening message, per phase** | Phases tab, phase card | §5 | **new** (today one list for the whole hotel) |
| Services per phase: name, description, text / form, ticket rules, form fields, knowledge (Pull from KB + menu), agent prompt + lock | Phases tab → service editor | §5 | exists |
| **Per service: department, priority, "takes food orders from its menu", buttons (form / link / call)** | Phases tab → service editor | §5 | **new** |
| **Ready-made stay services** | Phases tab → Prebuilt Services | — | card exists, **lists empty** |
| Menu PDFs | Pre-booking §4 (whole hotel) | room service menu | exists |
| Reply style, chips, form cancel, languages | Pre-booking §5 | formal; English / Hindi / Punjabi; "give number when the bot can't arrange" **off** | exists |
| FMS id | Pre-booking §6 | dev 6345 / prod 6586 | exists |
| Widget/group ids, allowed departments | Pre-booking §7 | departments in §5 | exists |
| Escalation | Escalation tab | live chat + ticket; 24 hours; after 1 failed answer; "I'm not sure about that - let me connect you to our team." | exists |
| Widget deployment | Widget page | only if guests also use the web chat (§9) | exists |
| Photos | image upload | restaurant, bar, facade, conference room (client folders) | exists |

No lead form: the guest is known, and forms fill in name, phone, email and room by themselves
(`form_fields_service.apply_prefill`).

## 3. Buttons, forms, menus on each channel

- **Web chat** (our widget, the test page): opening buttons, service buttons, forms, chips, menu PDFs all work
  today, as for T24.
- **WhatsApp**: today our reply is **text plus one file or link only** (`build_guest_journey_response`). Forms,
  buttons and chips are dropped, and nothing sends the menu PDF. For WhatsApp:
  1. our side: the reply also carries buttons (WhatsApp: up to 3, or a list of up to 10), a link or call button,
     and a document (menu PDF); a form becomes the bot asking its questions in the chat;
  2. the KePSLA platform: show those as WhatsApp buttons/lists/documents. A tapped button already comes back to us
     (`interactiveId`).
- **Mango prod has no WhatsApp sender at all yet**: FMS 6586 has no WhatsApp number, API key or BSP set. The
  OnBoarder gives 9815029465 as the WhatsApp Business number. Platform team.
- Hand-off to a person on WhatsApp goes through the platform. Our reply sets `needs_escalation`.

## 4. Code changes (once, for every future stay hotel)

All gated so **Zone, Mango, Sarovar and T24 stay exactly as they are**, proved by hashing their prompts before and
after plus a smoke test.

1. **Service editor**: department (from the hotel's departments), priority, "takes food orders from its menu",
   buttons (form / link / call, up to 3). Saved with the service (its settings JSON already keeps any field; saving
   refreshes the cache: no DB change, no restart). Empty = today's behaviour. The chat code for service buttons
   already works for any service (`_attach_admin_service_button`). Only the Pre-booking tab limited it to
   pre-booking services.
2. **Phases tab cards**: opening buttons + opening message per phase; default phase when dates are unknown.
3. **Prebuilt stay services**: fill the empty lists (`config_service._PRECHECKIN/_DURINGSTAY/_POSTCHECKOUT_PREBUILT_SERVICES`)
   with Sarovar's tested services, written without any hotel's facts. They only appear in the admin.
4. **Sarovar-only stay logic** → also on for an admin-set hotel that **has services in a stay phase** (T24 has
   none, so it doesn't change). This covers: the food order flow (per service, from 1), keeping a request in
   progress from being lost (`topic_relation` guards), the "can this only be done in the hotel?" check before
   arrival, and the no-guessing check. Sarovar keeps its own branch.
5. **Test page**: test guests from the hotel's own FMS id and its real GHN guests by stay dates; never another
   company's entity (today admin-set hotels get none).
6. **WhatsApp**: a message for an FMS id goes to the stay hotel that owns it; reply fields for buttons / list / link
   / call / document and forms asked as questions (shown once the platform supports them).
7. **Check, fix only if a test fails** (scoped to these hotels): a bare "yes" to "Shall I place the order?"; the
   global keyword rule that declines in-room-sounding requests before arrival ("need" + "pillow"); the shared admin
   wording rows that T24-type hotels read.

## 5. Mango content (all from the client's files)

**Services** (client's SLA categories, pre-check-in sheet and stage messages; Sarovar's services as the template):

| Phase | Service | Mode | Ticket to | Buttons |
|---|---|---|---|---|
| Pre Checkin | Pre-arrival requests: arrival time, early check-in, extra bed, infant, wheelchair, veg / non-veg / diet, occasion, sightseeing, dining wishes | form | Front Office | — |
| Pre Checkin | Airport pickup (INR 1,500 + taxes one way) | form | Front Office | Book pickup (form) |
| Pre Checkin | Change my booking (dates, room) | form | reservations | — |
| Pre Checkin | Talk to our team | form | Front Office | Call us |
| During Stay | Housekeeping, incl. laundry (INR 200 + taxes per piece) | text | Housekeeping | — |
| During Stay | Maintenance | text | Maintenance | — |
| During Stay | In-room dining: takes orders from its menu | text | Food & Beverage | View menu |
| During Stay | Front desk: late check-out, airport drop, doctor, bill | form | Front Office | Call us |
| During Stay | Extend my stay | form | reservations | — |
| During Stay | Complaint / talk to our team | form | Front Office (L2 GM-Operation) | Call us |
| Post Checkout | Post-stay: lost and found, invoice, feedback | form | Front Office | Call us |
| (optional) | Event / banquet enquiry (OnBoarder: "banquet and dining packages") | form | Banquet | — |

Priority **High** for every category (client's SLA sheet). "Call us" = +91 98150 09635.

**Opening buttons** (labels to confirm, taken from the client's own items):
- Pre Checkin: Arrival time · Airport pickup · Special request
- During Stay: Housekeeping · Room service · Front desk
- Post Checkout: Lost & found · Invoice · Call us

**Departments** (DEPARTMENT_MASTER ids differ between dev and prod):

| Department | Prod id | Dev id |
|---|---|---|
| Reception Front Office | 1 | 5 (Front Office) |
| Housekeeping | 3 | 8 |
| Maintenance / Engineering | 29 or 41 | 41 (Engineering) |
| Food and Beverages | 39 | 34 |
| reservations | 48 | 43 |
| Banquet (optional) | 38 | 1 |

**Room service menu**: not in the client's folder. The order flow needs it as text in Sarovar's format (dish,
price, veg / non-veg, serving times) in the service's Knowledge Input, plus the PDF for "send me the menu".

## 6. GHN side

**Dev** (with your OK; dev check of what exists is pending until the VPN is back):
- 3 test guests on FMS 6345 (it has none): one arriving later, one in house, one checked out.
- Staff rows (MANAGER=1) and alert rows for Front Office, Housekeeping, Maintenance, F&B and reservations on
  RI 8725 / FMS 6345: escalation L1 department head 10 min → L2 GM-Operation 15 min → L3 Executive Director 20 min,
  SLA per category (Journey OnBoarder tabs 4–7). Staff names and numbers go only here, never in the KB.
- Dashboard Type filter row for Mango's dev group (like T24's row 134). Without it, stay tickets are hidden in the
  dev ticket list.

**Prod** (later):
- Staff and alert rows for Front Office, Housekeeping and Maintenance. Prod has only Banquet / F&B / reservations,
  each with one manager (Suhas).
- Notification templates (Mango has none).
- WhatsApp sender on FMS 6586.

## 7. Testing

- The test page with each phase's test guest; WebSocket replays; Playwright on the real page.
- KB facts: Wi-Fi, gym, breakfast, laundry, doctor, check-out.
- Every service end to end: ticket on Mango's dashboard, right department and priority, staff alert.
- Phase rules:
  - pre-arrival "AC not working" → no ticket;
  - pre-arrival requests that mention the room still open the form;
  - check-in day and check-out day;
  - guest with unknown dates.
- One WhatsApp thread runs from pre-check-in to post-checkout: old requests must not leak into the next phase.
- Food order on gpt-4.1-mini.
- Hindi and Punjabi.
- Zone, Mango, Sarovar and T24 unchanged.

## 8. Later: prod

Create the hotel on prod and copy its rows (KB, services, prompts, settings); add `CHAT_LLM_PREBOOKING_HOTELS_MODEL`
to the prod `.env`; deploy the code; do the prod GHN items in §6.

## 9. Not our bot (tell the client)

Daily performance reports, unhandled-query emails, the FMS surveys (pre-check-in, during stay, post stay), review
replies: those are KePSLA's other products.

## 10. Needed from you

1. Hotel code `mango_grove_journey`?
2. **Where will guests chat: WhatsApp, the web chat, or both?**
3. OK to test on gpt-4.1-mini first? Sarovar's food ordering was tuned on gpt-5.4-mini.
4. The room service menu.
5. The client's answers on the KB conflicts.
6. Button labels OK?
7. Add "Change my booking", "Extend my stay" (reservations) and the optional event enquiry?
8. Default phase when a guest's dates are unknown: During Stay?
9. Commit the 9 files of uncommitted menu-topics work first?
10. OK for the dev GHN rows (test guests, staff/alerts, dashboard filter row)?
