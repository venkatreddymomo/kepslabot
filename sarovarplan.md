# Sarovar Portico Sriperumbudur — WhatsApp Bot Implementation Plan

> Source: onboarding sheet received from Sarovar (client). This file maps every
> line of that sheet to **what's already built**, **what's pending**, and **who
> owns it** (Client vs KePSLA), plus the real staff / escalation / SLA data.

**Tenant:** `sarovar_portico_sriperumbudur` (hotel_id 68, `GHN_PROD_BAK`) · **Bot name:** Kai
**Type:** Single hotel, single property (NOT multi-property like Zone).
**Channel today:** Web widget / test harness. **Target channel:** WhatsApp (Cloud API).

Status legend: ✅ done · 🔧 in progress / config-only · ⏳ pending build · 👤 needs client input

---

## 0. How WhatsApp fits in (important context)

The bot's **logic, KB, routing, forms, and ticketing are channel-agnostic** — the same
orchestrator serves the web widget and WhatsApp. Connecting WhatsApp is **not** a rebuild;
it's an **integration + provisioning** task:

1. Provision a WhatsApp number + Meta Business (WABA) verification + (optional) Blue Tick.
2. Wire the WhatsApp Cloud API webhook → our `/api/chat` message pipeline (inbound) and the
   send API (outbound + template messages).
3. Register the message **templates** (Meta-approved) for the proactive/scheduled sends.

**Until WhatsApp is live we test all responses in the web widget / harness** — behaviour is
identical; only the transport changes. So build + validate the flows now, flip the channel later.

---

## 1. Onboarding Task Tracker (client sheet, 11 items)

| # | Task | Owner | Status | Notes |
|---|------|-------|--------|-------|
| 1 | Integration (PMS/CRM/reviews) | Client access → KePSLA integrates | ⏳ / 👤 | Need to know which PMS/CRM Sarovar runs + API access. Drives booking-confirmation / stay data. |
| 2 | WhatsApp onboarding (number, Blue Tick, profile) | Client provides → KePSLA activates | ⏳ / 👤 | See §2. |
| 3 | Guest journey & bot flow (stages, templates, ticketing, KB, escalation, SLA) | Client confirms → KePSLA configures | 🔧 | Core of this plan — see §3–§10. Partly built. |
| 4 | User & agent roles & privileges | Client confirms → KePSLA sets up | ⏳ | Viewer / Staff / HOD / GM — see §11. |
| 5 | Notification configuration | Client mapping → KePSLA configures | 🔧 | Server → HOD → GM — see §7. Staff data received. |
| 6 | SLA configuration (High/Med/Low) | Client thresholds → KePSLA | 🔧 | See §8. Client gave "5–10 min HOD, 15 min GM, All Case". |
| 7 | Escalation matrix | Client matrix → KePSLA triggers | 🔧 | See §9. Matrix received. |
| 8 | UAT 1 (initial testing) | Client validates | ⏳ | After flows + WhatsApp wired. |
| 9 | UAT 2 (post-change) | Client validates | ⏳ | If changes needed. |
| 10 | Training & handover | KePSLA session | ⏳ | SOPs + admin walkthrough. |
| 11 | Go live | Client confirms → KePSLA deploys | ⏳ | Production flip. |

---

## 2. WhatsApp On-Boarding, Registration & Commercials

### 2.1 Hotel contact person (👤 client to provide)
| Field | Value |
|-------|-------|
| Name | _____ |
| Mobile | _____ |
| Email | _____ |
| WhatsApp number | _____ |

### 2.2 Requirements to initiate (👤 client responsibility)
- [ ] **Active phone number** with **no existing WhatsApp** account; can receive OTP + calls (give to FB owner for registration).
- [ ] **Facebook ID + password** with admin access.
- [ ] **Approved Business Manager** (no restrictions).
- [ ] **Business website** with SSL + **T&C** + **Privacy Policy** pages.
- [ ] **GST number + GST certificate** (all 3 pages).
- [ ] **Business email** on the **same domain as the website** (receives verification OTP).
- [ ] **Display name** (WhatsApp sender name) shown clearly on the **website header**.
- [ ] **Full business legal name** (as per GST) shown in the **website footer**.
- [ ] **VISA / MasterCard** credit/debit card linked to FB (post-paid conversation billing).
- Accepted business docs: Certificate of Incorporation, GST registration certificate.

### 2.3 Blue Tick (Official Business Account) — best-effort, Meta-gated
- [ ] ≥ **5 recent (within 12 months) organic media mentions** from reputable sources (not paid, not social, not low-quality). KePSLA applies; **approval not guaranteed**.

### 2.4 KePSLA responsibilities
- Activate number, apply for Blue Tick, set profile name + display picture, integrate + test the Cloud API, register templates.

---

## 3. Guest Journey — Stages & Sub-Stages

Sarovar chose the full journey (all stages). Mapping to our phase engine:

| Client stage | Our phase | Built? |
|--------------|-----------|--------|
| Booking Confirmation | `pre_booking` (post-booking confirmation) | ⏳ |
| Pre-Checkin / During Checkin | `pre_checkin` | ✅ (Arrival Transport) |
| During Stay | `during_stay` | ✅ (5 services) |
| Post Checkout | `post_checkout` | ⏳ |

### 3.1 Post-booking sub-stages (proactive WhatsApp template sends)
Each = a scheduled **template message** (client provides copy, KePSLA configures the trigger).

| Sub-stage | Trigger | Content | Owner | Status |
|-----------|---------|---------|-------|--------|
| (a) Booking Confirmation | Immediately after booking | Thank-you + booking summary + hotel contact + policy link | 👤 template text | ⏳ |
| (b) Pre-Arrival | **T-3 days** | Reminder + check-in time + directions + add-on upsell (airport transfer, early check-in, special occasion) | 👤 template text | ⏳ |
| (c) Day-of-Arrival | **T-0 morning** | Welcome + check-in instructions + contact | 👤 template text | ⏳ |
| (d) During-Stay Check-in | **T+1 (day 1)** | "How's your stay?" + quick-action buttons (housekeeping, dining, spa, maintenance) | 👤 template text | 🔧 (services exist; needs the proactive template + buttons) |
| (e) Post-Checkout / Feedback | **T+1 after checkout** | Thank-you + review link + rebooking offer | 👤 template text + review link | ⏳ |

**Template assets needed per message (👤 client):** body text, logo, images, CTA (button text / call number), list messages, carousels (body, logo, images, 1 CTA).

### 3.2 Bot triggering criteria (👤 confirm)
- At what point after a template send should the interactive bot activate? (e.g., on any guest reply, or on button tap.) Default recommendation: bot engages on **any guest reply** to a template.

---

## 4. Services Enabled per Stage (matrix)

| Service (our id) | Dept it should route to | pre_booking | pre_checkin | during_stay | post_checkout |
|------------------|-------------------------|:-----------:|:-----------:|:-----------:|:-------------:|
| Arrival Transport `arrival_transport` ✅ | Front Office / Concierge | – | ✅ | – | – |
| Housekeeping `housekeeping` ✅ | House-Keeping | – | – | ✅ | – |
| Maintenance `maintenance` ✅ | Engineering | – | – | ✅ | – |
| Table Reservation `table_reservation` ✅ | Food & Beverages | – | – | ✅ | – |
| Spa `spa` ✅ | *no spa dept in staff sheet* → Front Office/GR (or add Spa) 👤 | – | – | ✅ | – |
| Meeting & Event `meeting_banquet` ✅ | *no banquet dept* → F&B/Front Office (or add) 👤 | – | – | ✅ | – |
| Booking Confirmation / support (mismatch, cancel, modify, payment) ⏳ | Front Office / Reservations | ✅ | – | – | – |
| Post-Checkout (invoice/GST, refund, lost & found, loyalty, rebooking) ⏳ | Front Office / Accounts / GM | – | – | – | ✅ |

> 👤 **Decision needed:** which dept owns **Spa** and **Meeting/Banquet** tickets (no dedicated
> dept in the sheet). Default: Spa → Front Office, Banquet → F&B or GM, until told otherwise.

---

## 5. Ticketing — Conditions per Stage

**Ticket creation logic (👤 confirm):** create a ticket for any concern the bot can't fully
resolve itself — complaints, service requests, and the stage conditions below. Info-only
questions answered from the KB do **not** raise a ticket.

| Stage | Auto-ticket conditions (from sheet) | Built? |
|-------|-------------------------------------|--------|
| (a) Booking Confirmation | booking mismatch, cancellation request, modification request, payment issue | ⏳ |
| (b) Pre-Arrival | early check-in, airport transfer, special requests, document issue | 🔧 (airport transfer ✅; others ⏳) |
| (c) During-Stay | complaints, housekeeping, maintenance, F&B, spa, late checkout | ✅ (housekeeping/maintenance/F&B/spa built; **late checkout** ⏳) |
| (d) Post-Checkout | invoice/GST, refund, lost & found, complaint, loyalty points, rebooking | ⏳ |

**Bot response handling (already implemented for during-stay, extend to others):**
- Housekeeping/maintenance/etc. → bot acknowledges, notes the request against the guest's
  **room number**, and says the team will attend shortly (with a "Shall I go ahead?" confirm).
- 👤 **Confirm:** should replies include an **estimated resolution time**? (We can add per-dept
  ETAs.) Routing to a human = the ticket + notification chain in §7.

---

## 6. Departments, Staff & Contacts (received from Sarovar)

| Name | Dept | Mobile | Email | Role | Timing |
|------|------|--------|-------|------|--------|
| Duty Manager | Front Office | 918925864054 | dm@SarovarPorticoSPR.com | Duty Manager | 24 h |
| Hemant Acharya | Front Office | 918925864052 | fom@SarovarPorticoSPR.com | Front Office Manager (HOD) | 09–22 |
| Shift Engineer | Engineering | 918925864057 | eng.desk@SarovarPorticoSPR.com | Shift Duty Engg | 24 h |
| Navanith Singh | Engineering | 918925864055 | ce@SarovarPorticoSPR.com | Chief Engineer (HOD) | 09–18 |
| House Keeping Shift Incharge | House-Keeping | 918925864068 | hkdesk@SarovarPorticoSPR.com | HK Shift Incharge | 24 h |
| SwarnaKumari | House-Keeping | 918925864069 | ehk@SarovarPorticoSPR.com | Executive House Keeper (HOD) | 09–18 |
| Riya | Food & Beverages | 918925864065 | fnb@SarovarPorticoSPR.com | Asst. Restaurant Manager | 06–23 |
| Ektej | Food & Beverages | 918925864063 | fnb@SarovarPorticoSPR.com | Restaurant Manager (HOD) | 09–23 |
| Amit Choudhury | Administration | 918925864059 | gmspspr@sarovarhotels.com | General Manager | 09–18 |

**Department → service mapping (routing):** Front Office ← arrival transport, general, front-desk ·
Engineering ← maintenance · House-Keeping ← housekeeping · Food & Beverages ← table reservation /
dining · Administration (GM) ← escalation only.

---

## 7. Notification Routing

**Chain (from sheet): `Server → HOD → GM`.** i.e. first the 24 h desk/shift person, then the
department head, then the GM if still unresolved.

| Dept | Level 1 — Server / desk (24 h) | Level 2 — HOD | Level 3 — GM |
|------|-------------------------------|---------------|--------------|
| Front Office | Duty Manager · 918925864054 | Hemant Acharya · 918925864052 | Amit Choudhury · 918925864059 |
| Engineering | Shift Engineer · 918925864057 | Navanith Singh · 918925864055 | Amit Choudhury · 918925864059 |
| House-Keeping | HK Shift Incharge · 918925864068 | SwarnaKumari · 918925864069 | Amit Choudhury · 918925864059 |
| Food & Beverages | Riya · 918925864065 | Ektej · 918925864063 | Amit Choudhury · 918925864059 |

**Rule (from sheet):** *"If any item is not available, a ticket is raised and the request goes to
the department head, who has to take care of it."*

**KePSLA config work (🔧):** the tenant's DEPARTMENT_STAFF_CONFIG in GHN currently has managers
only on **Front Office + Guest Relations**. To route Sarovar tickets correctly we must add the
above departments with a `MANAGER=1` staff row (the HODs) + their WhatsApp numbers — same
mechanism we mapped for Zone. **This is the key backend config gap.**

---

## 8. SLA Definition

Client input so far: **HOD response 5–10 min, GM 15 min, priority = "All Case".**
Recommend formalising three tiers (👤 confirm thresholds):

| Priority | Examples | Target response | Escalate to HOD after | Escalate to GM after |
|----------|----------|-----------------|-----------------------|----------------------|
| **High / Critical** | no water, no power, safety, complaint | ≤ 5 min | 5 min | +10 min |
| **Medium** | maintenance fault, housekeeping, F&B, spa | 5–10 min | 10 min | +15 min |
| **Low** | info requests, add-ons, add-on upsell | best-effort | — | — |

(Our maintenance service already flags no-water / no-power / safety as **high priority**.)

---

## 9. Escalation Matrix (received)

**Level 1 — Department Head, 5–10 min, All Case:**
| Dept | HOD | Timing | Mobile |
|------|-----|--------|--------|
| Front Office | Hemant Acharya | 5–10 min | 918925864052 |
| Engineering | Navanith Singh | 5–10 min | 918925864055 |
| House-Keeping | SwarnaKumari | 5–10 min | 918925864069 |
| Food & Beverages | Ektej | 5–10 min | 918925864063 |

**Level 2 — General Manager, 15 min, All Case:**
| Dept | Person | Timing | Mobile |
|------|--------|--------|--------|
| Administration | Amit Choudhury (GM) | 15 min | 918925864059 |

Trigger: unresolved ticket → HOD after 5–10 min → GM after 15 min.

---

## 10. KB Content per Stage (👤 client provides docs → KePSLA ingests)

Already ingested & grounded (verified): dining (SPR Kitchen 06:30–22:30 + Chola Lounge 11:00–23:00),
Aloe Spa (massage/steam/sauna; jacuzzi/facial "not available"), meeting venues (Summit 500 / Senate
170 / Cresant 20 / Board Room 12), amenities (outdoor pool, gym), airport transport (MAA ~32 km,
NH716). **Missing / to collect per stage:**
- Booking-confirmation: cancellation/modification/refund **policy** + payment terms.
- Pre-arrival: directions/map, early check-in policy, add-on price list.
- During-stay: **spa operating hours** (currently a placeholder 9 AM–8 PM — KB has none), late-checkout rules, full IRD/F&B menu.
- Post-checkout: invoice/GST process, lost & found SOP, loyalty program, rebooking offers, feedback/review link.

---

## 11. Roles & Privileges (👤 confirm)

| Role | Access |
|------|--------|
| Viewer | Read-only dashboards/tickets |
| Staff | Own-department tickets, respond/close |
| HOD (dept head) | Department tickets + reassignment + reports |
| GM | All departments, escalations, config sign-off |

Map the §6 people to these roles in Staff Management + Departments.

---

## 12. Feedback / NPS + Frequency Cap

- **Feedback / NPS (⏳):** post-checkout review link / native NPS flow — 👤 client provides link/format → KePSLA embeds in sub-stage (e).
- **Silence / frequency cap (👤 confirm → KePSLA enforces):** max messages per guest per stay + **quiet hours (no messages 10 PM – 8 AM)**.

---

## 13. UAT → Training → Go-Live

1. **UAT 1** — end-to-end sample cases in the widget/harness (all stages, ticketing, escalation, notifications). Client validates.
2. **UAT 2** — re-test after any changes.
3. **Training & handover** — admin walkthrough + SOPs (KePSLA).
4. **Go-Live** — client sign-off → production deploy (WhatsApp channel enabled).

---

## 14. DONE vs PENDING (current build → this plan)

### ✅ Already built & verified (in DB / code)
- During-stay services: Housekeeping, Maintenance, Table Reservation, Spa, Meeting & Event — active, KB-grounded, ticketing on.
- Pre-checkin: Arrival Transport (enquiry-vs-book, airport details grounded).
- Deterministic form UX: stay-bounded date picker, outlet-filtered **time-slot dropdowns**, closed-set selects, **room/name/phone prefill**, pickup-location capture.
- Grounding hardened (no fabrication); routing clauses so a named service isn't hijacked as a room booking.
- Leak fixes (confirmation recap + stored chat history) — no internal fields leak.
- Business name/city corrected (`Sarovar Portico Sriperumbudur` / `Sriperumbudur`).

### 🔧 / ⏳ Pending
1. **Departments + managers in GHN** for Front Office, Engineering, House-Keeping, F&B (+ decide Spa/Banquet) with the §6 HODs as `MANAGER=1` + WhatsApp numbers. **← highest priority; without it tickets can't route to the right team.**
2. **Notification chain** Server → HOD → GM wired to the §7 numbers.
3. **SLA tiers** (§8) + **escalation triggers** (§9) configured.
4. **Booking-Confirmation (pre_booking)** stage: template + ticketing (mismatch/cancel/modify/payment).
5. **Post-Checkout** stage: template + services (invoice/GST, refund, lost & found, loyalty, rebooking, feedback).
6. **Late-checkout** during-stay condition.
7. **WhatsApp provisioning** (§2) + template registration + webhook wiring.
8. **Proactive scheduled sends** (sub-stages a–e) with T-3 / T-0 / T+1 triggers.
9. **Feedback/NPS link** + **frequency cap / quiet hours**.
10. **PMS/CRM integration** (if in scope) for booking data.
11. **Deploy the updated code** (services already in DB; the slot/prefill/leak fixes are **code** — rebuild the Docker image from current code + `admin_ui` build, same DB, to make them live on bot.kepsla.ai).

---

## 15. Open decisions / client inputs needed (👤)

- [ ] Which **PMS/CRM** + API access (for booking/stay data)?
- [ ] **Template copy** for sub-stages a–e (+ logo/images/CTA/review link).
- [ ] **Dept owner for Spa and Meeting/Banquet** tickets.
- [ ] Confirm **ticket-creation conditions** per stage (§5) and whether replies include **ETAs**.
- [ ] **SLA thresholds** (High/Med/Low) + confirm escalation timings.
- [ ] **Roles** mapping (§11) + **notification recipients** confirmation (§7).
- [ ] **Frequency cap** + quiet-hours policy.
- [ ] **Feedback/NPS** link/format; **rebooking** offer content.
- [ ] WhatsApp: contact person + all §2 provisioning items + GST/website/FB assets.

---

## 16. Responsibility split (summary)

- **Client provides:** PMS access, WhatsApp/FB/GST/website assets, template copy + creatives,
  ticketing conditions, SLA thresholds, escalation matrix, roles mapping, KB docs per stage,
  feedback link, frequency policy, UAT validation, go-live sign-off.
- **KePSLA does:** integrate + test, activate WhatsApp + Blue Tick, configure all stages / bot
  flow / services / ticketing / KB ingestion, set up departments-managers-notifications-SLA-
  escalation, run UAT support, training & handover, production deploy.
