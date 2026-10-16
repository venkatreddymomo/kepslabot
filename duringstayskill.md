# During‑Stay Training Skill

> **How to use this file:** When you need to train a property's **during‑stay** services, tell Claude:
> *"Refer to `duringstayskill.md` and set up during‑stay services for <property>."*
> This document is self‑contained — a future session can reproduce the whole setup from here alone.
> Last verified against the codebase: 2026‑07‑03.
>
> **Part of post‑booking training.** See `postbookingskill.md` (the master) which covers all three
> post‑booking phases — **pre‑checkin, during‑stay, post‑checkout**. This file is the during‑stay chapter
> (full field‑by‑field detail); the master reuses the same mechanism, routing, and design rules.

---

## 0. What "during‑stay" training means here

Give an **in‑house guest** the ability to ask for operational help (housekeeping, dining, maintenance, spa, transport, etc.) in chat, and have the bot **answer from the KB** and, when the guest actually wants an action, **raise a staff ticket**. Each of those capabilities is a **Service** attached to the `during_stay` phase.

You are done when: every intended service exists under `during_stay`, is grounded in that property's KB, and produces a correctly‑categorised ticket when the guest asks for the action.

---

## 1. The mechanism (how services actually work in this bot)

- **No prebuilt during‑stay catalog exists** — `_DURINGSTAY_PREBUILT_SERVICES = []` in [services/config_service.py:224](services/config_service.py#L224). **Every during‑stay service is custom‑created per property.**
- Services are created/edited in the admin UI: **Bot Training → Phases tab** ([admin_ui/src/components/bot-training/PhasesTab.tsx](admin_ui/src/components/bot-training/PhasesTab.tsx)) → set **Active Phase = During Stay** → *"Create New Service For This Phase"*.
- Under the hood that calls the admin API (all require the `x-hotel-code` header):
  - `POST   /api/config/services` — create
  - `PUT    /api/config/services/{id}` — update / map to a phase (`{ "phase_id": "during_stay" }`)
  - `DELETE /api/config/services/{id}` — delete
  - `POST   /api/config/service-kb/preview-extract` — "Pull from KB" (grounds the service in KB)
  - `POST   /api/config/services/{id}/regenerate-prompt` — build the agent system prompt
  - `PUT    /api/config/services/{id}/prompt` — save an edited prompt
- **The `description` field is a routing mini‑prompt.** It overrides router rules — write it as an instruction to the bot, not marketing copy. (This bot treats a service's DB description as a prompt.)

### Service payload schema (verified from `savePhaseService`)

```jsonc
{
  "id": "housekeeping",                 // kebab/snake, unique per property
  "name": "Housekeeping",
  "type": "service",                    // service | department | workflow | plugin
  "description": "…routing mini-prompt…",
  "phase_id": "during_stay",
  "is_active": true,
  "is_builtin": false,
  "ticketing_enabled": true,            // = (ticketing_mode != "none")
  "ticketing_mode": "text",             // none | text | form
  "ticketing_policy": "…when to raise a ticket…",
  "service_prompt_pack": {              // optional; grounding + conditions
    "source": "manual_override",
    "generator": "admin_ui",
    "version": 1,
    "extracted_knowledge": "…pulled from KB…",
    "ticketing_conditions": "…same as ticketing_policy…"
  },
  "form_config": {                      // ONLY when ticketing_mode == "form"
    "trigger_field": { "id": "…", "label": "…", "description": "…" },
    "fields": [
      { "id": "room_number", "label": "Room Number", "type": "text",
        "required": true, "validation_prompt": "" }
    ],
    "pre_form_instructions": "…when to show the form…"
  }
}
```

- **`ticketing_mode`**: `none` (info only), `text` (bot collects details in chat, then tickets), `form` (renders a structured form).
- **Form field `type`**: one of `text | date | time | number | tel | email | textarea`.

---

## 2. Departments & ticket routing (READ THIS — it's non‑obvious)

**Services do NOT have a department field.** Departments exist only at the *ticket* layer. A ticket's category is coerced onto a **fixed accepted set** ([services/ticketing_service.py:1816](services/ticketing_service.py#L1816)); anything outside it gets remapped or the ticket is rejected (a real past bug: `"Reception Front Office"` was rejected 9/9 times → use `"Front Office"`).

**Accepted ticket categories:** `Housekeeping`, `In-Room Dining`, `Front Office`, `Cashiering Billing`, `spa booking`, `airport transfer`, `Business Centre`, `Guest Relations`, `complaint`, `technical_support`, `support request`, `room booking request`.

**KePSLA page collapses to 4 departments** ([ticketing_service.py:3316](services/ticketing_service.py#L3316)): `Reception Front Office`, `Guest Relations`, `Housekeeping`, `In-Room Dining`.

**Map any real-world department → an accepted category:**

| Real‑world dept | Use accepted category |
|---|---|
| Housekeeping / Linen / Laundry | `Housekeeping` |
| Maintenance / Engineering | `technical_support` |
| In‑Room Dining / Room Service / Restaurant / F&B | `In-Room Dining` |
| Spa / Wellness | `spa booking` |
| Transport / Airport / Railway / Local pick‑drop | `airport transfer` |
| Concierge / Front desk / Wake‑up / Luggage / Parking / Valet | `Front Office` |
| Meeting / Banquet / Business services | `Business Centre` |
| General complaint / dissatisfaction | `complaint` |
| Billing / Cashiering | `Cashiering Billing` |

**The during‑stay classifier is already primed.** `_DURING_STAY_TICKETING_CASES` ([config_service.py:141](services/config_service.py#L141)) already enumerates housekeeping, maintenance, in‑room dining, restaurant reservation, spa/recreation, transport, complaint, late‑checkout, front‑desk, and emergency. You are giving those intents dedicated services + forms + KB, not teaching them from scratch.

---

## 3. Design rules (apply every time)

1. **Hybrid capture mode.** Use `text` for "just do it" asks (housekeeping, laundry, maintenance, concierge, parking); use `form` where structured data matters (dining, spa, transport, restaurant, banquet).
2. **Room number is mandatory** on every during‑stay service (guest is in‑house). In `form` mode add a required `room_number` field; in `text` mode force it in the Ticketing Conditions.
3. **Ground everything in the KB — never fabricate.** After creating a service, click **Pull from KB** so `extracted_knowledge` comes from *that property's* blocks. If the KB doesn't list an amenity/outlet/treatment, the service must say "not available", not invent it.
4. **Encode closed sets in the `description`.** If the KB says some sub‑items are unavailable (e.g. spa offers massage/steam/sauna but NOT jacuzzi/facial/mani‑pedi), write that exclusion into the description so the bot refuses them.
5. **Info vs. action split.** Some things stay **KB answers with NO service and NO ticket**: check‑in/out timings, smoking policy, pool/gym info, Wi‑Fi *availability*, nearby attractions, room amenities/types, children policy. Note the split — "Wi‑Fi availability" is info; "Wi‑Fi not working" is a Maintenance ticket.
6. **`description` = instruction, not blurb.** Tell the bot what it handles, to answer info from KB first, and when to raise a ticket.

---

## 4. Reusable service template (fill in the blanks)

```
Name:                <human name>
ID:                  <snake_case_id>            # unique for this property
Type:                service | department
Ticketing Mode:      text | form
Ticketing Enabled:   on

Description:
"Handles <what> for in-house guests. Answer <info type> from the KB.
 When the guest wants <action>, collect <key data> and raise a ticket.
 <Closed-set exclusions if any>."

Ticketing Conditions:
"Raise a ticket when <trigger>. Always capture the room number first.
 <Priority note if safety/no-water/no-power>."

Form fields (form mode only):
  room_number (text, required)
  <field_id> (<type>, required|optional)
  ...
Pre-form instructions:
  "Answer questions from KB first. Only show this form once the guest confirms <intent>."

KB to pull:  <which KB section(s)>
Routes to:   <accepted ticket category from §2>
```

---

## 5. Step‑by‑step procedure

**Prerequisites**
- [ ] KB for the property is **reindexed** (source of truth is the DB, not local files).
- [ ] You know the property's **`hotel_code`** (the `x-hotel-code` value) and its **name** as configured.
- [ ] You know the property's **FMS entity id** for `ticketing_property_entity_map_json` — *without it, tickets still save to DB + KePSLA, but the external Kepsla API stays unmapped.*
- [ ] Fresh chat sessions **default to `during_stay`**, so you can test each service immediately — just confirm the routing‑log phase reads `during_stay`.

**For each service**
1. Bot Training → Phases → **Active Phase = During Stay** → *Create New Service For This Phase*.
2. Fill Name, ID, Type, Ticketing Mode, Description, Ticketing Conditions (and Form Builder fields if `form`).
3. **Save Service**.
4. **Knowledge Input → Pull from KB** (grounds `extracted_knowledge`). Optionally upload a menu (Menu OCR) for dining.
5. **Regenerate → Save Prompt** (bakes description + conditions + form into the agent system prompt).
6. Verify it appears under **Services In This Phase** with *Ticketing On* and the right mode.

**After all services**
- If prompt changes don't take effect, **restart the backend** (WatchFiles can be unreliable) or hit the dev prompt‑reload endpoint.
- Add the property's FMS entity id to `ticketing_property_entity_map_json` so tickets route externally.

---

## 6. Verification checklist (per service)

- [ ] Service shows under *Services In This Phase* for `during_stay`, **Active + Ticketing On**, correct mode.
- [ ] Info question → answered **from KB** (no fabrication), **no ticket**.
- [ ] Action request → bot captures **room number** + required data → **ticket created**.
- [ ] Ticket lands in the **expected accepted category** (§2) and is visible via `GET /api/admin/api/tickets` with `x-hotel-code`.
- [ ] Closed‑set exclusions are refused (e.g. spa jacuzzi → "not available").
- [ ] Verify routing‑log **phase = during_stay** for the test session.

---

## 7. Gotchas / lessons carried forward

- **Category rejection:** never send a department name as a category. Coerce to the accepted set (§2). `"Reception Front Office"` → `"Front Office"`.
- **FMS mapping:** ticket may show on KePSLA but be absent from the external API page if the property's entity isn't mapped. Add it.
- **Restart to load:** service prompt / description edits sometimes need a backend restart to take effect.
- **Don't hand‑type KB facts:** always Pull from KB so grounding stays truthful and updates when the KB changes.
- **Prompt rules are recency‑sensitive:** override rules placed late in a prompt win; if a rule "isn't working", check placement before rewriting.

---

## 8. Worked reference — Sarovar Portico (during‑stay, hybrid, 10 services)

Built from a real KB read. Reuse the shapes; re‑verify each against the actual property KB before enabling.

| # | Service (`id`) | Type | Mode | Routes to |
|---|---|---|---|---|
| 1 | In‑Room Dining (`in_room_dining`) | service | form | In‑Room Dining |
| 2 | Housekeeping (`housekeeping`) — cleaning+linen+bath amenities | department | text | Housekeeping |
| 3 | Laundry (`laundry`) | service | text | Housekeeping |
| 4 | Maintenance/Engineering (`maintenance`) | department | text | technical_support |
| 5 | Spa Booking (`spa_booking`) ⚠️ closed‑set | service | form | spa booking |
| 6 | Transportation (`transport`) | service | form | airport transfer |
| 7 | Restaurant Reservation (`restaurant_reservation`) | service | form | In‑Room Dining |
| 8 | Concierge/Front Office (`concierge`) | department | text | Front Office |
| 9 | Meeting & Event Assistance (`meeting_banquet`) | service | form | Business Centre |
| 10 | Parking/Valet (`parking_valet`) | service | text | Front Office |

**Closed‑set / grounding notes for this property:**
- **Spa (Aloe Spa):** offer **massage / steam / sauna only**; refuse jacuzzi, facial, hair, mani/pedi (KB says unavailable).
- **Housekeeping amenities:** toiletries kit, dental kit, toilet paper, slippers, shower cap, towels, bedsheet, pillow, blanket. No evening turndown (not in KB).
- **Maintenance items:** AC, TV, Wi‑Fi, telephone, hot/cold water, shower, mini fridge, electric kettle, in‑room safe, charging socket, lighting.
- **Restaurant outlets:** SPR Kitchen, Chola Lounge.
- **Parking/Valet:** KB‑CONFIRMED for this property — parking (42 cars) + valet + EV charging station all present.

Full field‑by‑field spec (descriptions, ticketing conditions, form fields) for these 10 was produced in‑chat and can be regenerated from §4 + this table.

---

## 9. Convention for future skills

When we train something new and it works well, capture it the same way: one `*.md` playbook, self‑contained, grounded in real code/endpoint references, with a reusable template + a worked example + gotchas. Keep them together so "refer to `<x>skill.md` and do it" always works.
