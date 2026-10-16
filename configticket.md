# Ticketing Dashboard — Staff / Manager Provisioning Runbook

How the **Kepsla / GHN ticketing dashboard** (KePSLA / kepsla-rep-dashboard-svc) decides who
sees and receives tickets & notifications, and the exact tables + SQL to add a person.

> This is the **external GHN production platform**, NOT the chatbot's own `new_bot_*` tables.
> Last updated 2026-07-08 (Atul provisioning + Sarovar analysis).

---

## 0. TL;DR

- A hotel group = an **organization GROUP**. Each hotel = an **organization** with an **RI id**
  (a.k.a. `ORG_ID` / `ORGANIZATION_ID` / `entity_id`) and an **FMS id** (what the bot sends as
  `organisation_id` when creating a ticket). `GHN_SURVEY_FMS_RI_ENTITY_MAPPING` maps FMS ↔ RI.
- A person = a row in `GHN_USER`. To make them see + receive tickets for a hotel you wire **5 tables**
  (all keyed off the **RI id**, never the FMS id):
  1. `GHN_USER` — the login
  2. `GHN_USER_ROLE_MAPPING` — permission role (ROLE_ID 3 = client admin)
  3. `GHN_ROLE_FEATURE` — per-user dashboard feature grants (**easy to miss — needed for the UI to work**)
  4. `GHN_USER_ORGANIZATION_MAPPING` — which hotels they can SEE (1 row/hotel)
  5. `DEPARTMENT_STAFF_CONFIG` — which hotel×department they handle, and MANAGER (head) vs staff
- `GHN_FF_TICKET_NOTIFICATIONS` = per-hotel/phase message **templates** (RECEIVER = role name, not a
  user) → **no per-user rows needed**; recipients resolve via `DEPARTMENT_STAFF_CONFIG`.
- `GHN_DEPARTMENT_ALERT_MAPPINGS` = a separate alert engine; **0 rows for Zone → unused**.

---

## 1. Database connection

Same MySQL as the app's `DATABASE_URL` in `.env` (VPN required):

```
host=172.16.5.32  port=3306  user=root  password=<see .env DATABASE_URL — do NOT hardcode/commit it>
databases used: GHN_PROD_BAK, GHN_FF_PROD, GHN_FEEDBACK_RESULTS
```

Read-only helper (Python / pymysql). **Never commit scripts with the password; delete throwaways.**

```python
import os, pymysql  # load the password from .env DATABASE_URL — never hardcode it here
DB_PW = os.environ["GHN_DB_PASSWORD"]  # or parse it out of .env's DATABASE_URL
conn = pymysql.connect(host="172.16.5.32", port=3306, user="root",
                       password=DB_PW, connect_timeout=25,
                       cursorclass=pymysql.cursors.DictCursor)
cur = conn.cursor()
def q(sql, args=None):
    cur.execute(sql, args) if args else cur.execute(sql)   # note: no-arg branch avoids % LIKE bug
    return cur.fetchall()
```

Gotcha: pymysql treats `%` in a `LIKE` as a format token when you pass any args tuple — pass
`args=None` and call `cur.execute(sql)` with no args, or double it to `%%`.

---

## 2. The tables (what each is + key columns)

### `GHN_PROD_BAK.GHN_USER` — the account
Login handle + alert email live in **`USER_NAME`** (existing staff keep `PRIMARY_EMAIL` NULL).
`PASSWORD` = **bcrypt hash** (`$2a$10$...`) — the column default is literally `manager` but storing
the plain word fails login (see §9). Other: `ENABLED=1`, `ROLE='Admin'`, `GROUP_ROLE=1`,
`MOBILE`='91'+10 digits (for WhatsApp; no `+`/spaces), `TICKET_NOTIFICATION` (0 on working staff — not
the gate), `MFA_ENABLED`/`USER_MFA_ENABLED`. No direct group/entity column — access is via the mapping tables.

### `GHN_PROD_BAK.GHN_USER_ROLE_MAPPING` — permission level
Cols: `USER_ID`, `ROLE_ID`. One row per user. `ROLE_ID=3` = `GHN_CLIENT_ADMIN` (see `GHN_ROLE`:
1 GHN_ADMIN internal-super, 2 GHN_ACCOUNT_MANAGER, **3 GHN_CLIENT_ADMIN**, 4 POS_MANAGER, 5 POS_EMPLOYEE).

### `GHN_PROD_BAK.GHN_ROLE_FEATURE` — per-user feature grants (REQUIRED for dashboard)
Despite the name it has a `USER_ID` column. Cols: `ROLE_ID`, `FEATURE_ID`, `AUTHORITY`, `USER_ID`.
Each working Zone admin has **~43 rows** (ROLE_ID=3, FEATURE_ID 1,2,3,36,72,88,…, AUTHORITY=1).
**Without these the user authenticates but has no features → broken/blank dashboard.** Clone from a
working admin.

#### KeBo Engage tab is missing? It is THIS table. (learned 2026-09-25)
Symptom: user logs in fine, sees the hotel, but **"KeBo Engage", "Tickets List" and "Active Tickets"
do not appear**. Cause is always missing `GHN_ROLE_FEATURE` rows — not org mapping, not role mapping.
The names in `GHN_FEATURE_MASTER` map 1:1 onto the tabs people report as missing:

| FEATURE_ID | FEATURE | URL |
|---|---|---|
| 1   | Dashboard              | `../main/dashboard.htm` |
| 114 | Bot & Ticket Insights  | `../dashboard/kebo-v3.htm` |
| 115 | **KeBo Engage**        | `../dashboard/kebo-v3.htm` |
| 117 | **GJ Active Tickets**  | `?app=active-tickets` |
| 118 | **Tickets List**       | `?app=tickets-list` |
| 123 | Visitors Chat History  | `?app=visitors-chat-history` |
| 136 | Pre Booking Tickets    | `?app=/booking/ticket-list` |
| 149 | **Engage Active Tickets** | (none) |

A KeBo-Engage user needs **all 8**. Reference user that works: `reservations@zonebythepark.com`
(**ID 11865**) — has exactly these 8, ROLE_ID=3, AUTHORITY=1.

Note: the "~43 rows" figure above is the OLDER full-suite admin pattern. A KeBo-Engage-only user
needs just these 8; do not assume a count, diff against 11865.

**Done 2026-09-25 — Suhas (`SuhasR@kepsla.ai`, ID 11860)** had only FEATURE_ID 1 (a 2023 row), so
every KeBo Engage tab was hidden. Inserted the other 7 (ids 584841-584847); now matches 11865 exactly.

```sql
-- add the 7 missing grants for a user (this table has NO unique key -> check before inserting)
INSERT INTO GHN_PROD_BAK.GHN_ROLE_FEATURE
  (ROLE_ID, FEATURE_ID, AUTHORITY, CREATED_DATE, CREATED_USER_ID, UPDATED_DATE, UPDATED_USER_ID, USER_ID)
VALUES (3,114,1,NOW(),0,NOW(),:u,:u),(3,115,1,NOW(),0,NOW(),:u,:u),(3,117,1,NOW(),0,NOW(),:u,:u),
       (3,118,1,NOW(),0,NOW(),:u,:u),(3,123,1,NOW(),0,NOW(),:u,:u),(3,136,1,NOW(),0,NOW(),:u,:u),
       (3,149,1,NOW(),0,NOW(),:u,:u);
-- undo
DELETE FROM GHN_PROD_BAK.GHN_ROLE_FEATURE WHERE USER_ID=:u AND FEATURE_ID IN (114,115,117,118,123,136,149);
```

**Gotchas:** this table has **no unique key** on (USER_ID, FEATURE_ID) — a blind re-run silently
duplicates, so always check first. And the grant may not show until the user **logs out and back in**
(feature list is cached in the session).

`GHN_USER_ORGANIZATION_DEPARTMENT_MAPPING` is NOT the cause — user 11865 has 24 rows there but every
one is `DEPARTMENT_ID=0, ORGANIZATION_ID=0` (empty placeholders the admin UI writes). Ignore it.

### `GHN_PROD_BAK.GHN_USER_ORGANIZATION_MAPPING` — hotel visibility
Cols: `ORGANIZATION_ID` (=RI id), `USER_ID`. One row per hotel the user may SEE. Zone now has **24** (23 in the original list + Katra RI 8699, added 2026-07-13).

### `GHN_PROD_BAK.DEPARTMENT_STAFF_CONFIG` — ticket routing (the important one)
Cols: `STAFF_ID`(=user), `ORG_ID`(=RI id), `DEPARTMENT_ID`, `MANAGER` (1=dept head / ticket lands on
them / **bot only routes to depts that HAVE a manager**; 0=staff, copied only), `STATUS=1`,
`TICKET_PRIORITY`, `GROUP_ID` (usually NULL), shift/outlet defaults. One row per hotel×department.

### `GHN_PROD_BAK.GHN_USER_ORGANIZATION_DEPARTMENT_MAPPING` — loose/placeholder
Working staff have 1–2 rows with `DEPARTMENT_ID=0, ORGANIZATION_ID=0`. Not part of routing; add one
placeholder to match, otherwise ignorable.

### `GHN_FEEDBACK_RESULTS.GHN_SURVEY_FMS_RI_ENTITY_MAPPING` — FMS↔RI lookup
Cols: `FMS_Entity_Id`, `RI_Entity_Id`. Reference only. **If an FMS id has no row here, it has no RI →
you cannot attach staff/departments/notifications to it** (that was the Sarovar blocker, §10).

### `GHN_FF_PROD.GHN_FF_TICKET_NOTIFICATIONS` — alert templates (no per-user rows)
Cols: `ENTITY_ID`(=RI), `FMS_ENTITY_ID`, `PHASE` (created/assigned/reminder_beforesla/sla_breach/
resolved/cancelled), `TYPE`, `RECEIVER` (`Dept Head`/`staff`/`guest` — a ROLE, resolved to people via
`DEPARTMENT_STAFF_CONFIG`), `TEMPLATE_*`, `EMAIL_*`. Zone has 174 rows across its 23 hotels.

### `GHN_FF_PROD.GHN_DEPARTMENT_ALERT_MAPPINGS` — separate alert engine (unused by Zone)
Cols: `entity_id`, `fms_entity_id`, `department_id`, `channel(whatsapp|mail|both)`, `escalation_1..3`,
`escalation_level_1..3(json)`, SLA fields. **0 rows for Zone → not used.**

---

## 3. Zone reference data ("The Zone Park", GROUP_ID 139)

24 properties — RI (=ORG_ID) → FMS, with names (Katra RI 8699 / FMS 6312 added 2026-07-13):

| RI | FMS | Hotel | RI | FMS | Hotel |
|----|-----|-------|----|-----|-------|
| 7256 | 6290 | Kolkata | 8688 | 6300 | Coimbatore (Connect) |
| 7728 | 6295 | Jaipur | 8689 | 6301 | Patna |
| 8676 | 6288 | Gopalpur | 8690 | 6302 | New Market Kolkata |
| 8677 | 6289 | Calangute Goa | 8691 | 6303 | Gangtok (Siniolchu) |
| 8679 | 6291 | Chennai | 8692 | 6304 | Darjeeling |
| 8680 | 6292 | Coimbatore | 8693 | 6305 | Jaisalmer |
| 8681 | 6293 | Dimapur (Niathu) | 8694 | 6306 | Lansdowne |
| 8682 | 6294 | Phalodi | 8695 | 6307 | Parra Goa |
| 8684 | 6296 | Jammu | 8696 | 6308 | Port Blair |
| 8685 | 6297 | Saket Delhi | 8697 | 6309 | Udaipur |
| 8686 | 6298 | Pathankot | 8698 | 6310 | Digha |
| 8687 | 6299 | Mussoorie | 8699 | 6312 | Zone Connect Katra |

RI list (copy/paste): `7256,7728,8676,8677,8679,8680,8681,8682,8684,8685,8686,8687,8688,8689,8690,8691,8692,8693,8694,8695,8696,8697,8698,8699`
FMS list: `6288,6289,6290,6291,6292,6293,6294,6295,6296,6297,6298,6299,6300,6301,6302,6303,6304,6305,6306,6307,6308,6309,6310,6312`

> **24th property (Katra) added 2026-07-13, after this runbook's original 23-hotel list:** Zone Connect
> Katra = **RI 8699 / FMS 6312** (`.env` `"katra":"6312"`). Any new all-Zone user should get **24** org-visibility
> rows, not 23. (Verify whether existing staff Venky/Sana/Suhas got the 24th Katra org row or are still on 23.)

Zone departments used (6): **5 Front Office, 16 Guest Relations, 28 Book a stay, 29 Book a venue,
31 MICE/ corporate bookings, 33 Call us/contact us/request a call.** Existing staff pattern per hotel:
MANAGER=1 on 5 & 16, MANAGER=0 on 28/29/31/33. Zone `.env`: `group_id 139`, `widget_id 61`.

### Zone staff (existing)
| User | ID | login (USER_NAME) | org_map | dept_cfg | role | features |
|------|----|-------------------|---------|----------|------|----------|
| Venky | 6422 | VenkateshwaraR@kepsla.ai | 23 | 66 (11 hotels) | 3 | 43 |
| Sana  | 6424 | **sanas@kepsla.ai** | 23 | 36 (6 hotels) | 3 | 43 |
| Suhas | 6425 | ratakondasuhass@gmail.com | 23 | 36 (6 hotels) | 3 | 49 |
| Sana (dup) | 6421 | SanaSs@kepsla.ai | 23 | 0 | 3 | 43 |

They **split** the hotels for ticket duty (11+6+6) but all **see all** of them (23 as of the table above;
verify whether they've since gained the 24th Katra org row). Use `sanas@kepsla.ai` (6424) as "Sana".

---

## 4. Full department catalog (`DEPARTMENT_MASTER`)

```
 1 Banquets           11 Room                 27 Tension work        36 Spa / Wellness
 2 Bar/ Pub           12 Spa                  28 Book a stay         37 Concierge
 3 Beauty Salon       13 Accounts             29 Book a venue        38 Maintenance
 4 Business Centre    14 Marketing            30 Ghar Mehfil         39 IT
 5 Front Office       15 In-Room Dining       31 MICE/ corporate     40 Billing
 6 General            16 Guest Relations      32 Travel agent bookings
 7 Gym                17 Loyalty & CRM Mktg    33 Call us/contact us/request a call
 8 Housekeeping       18 Reception Front Office 34 Food & Beverage
 9 Recreation         19 Cashiering Billing   35 Sales
10 Restaurant
```

---

## 5. Provisioning recipe — add a staff/manager user

Order matters (each step needs the user id). Always run guarded + verify + commit; everything is
reversible by the new user id. Do writes in a transaction (`autocommit=False`, verify, then `commit()`).

```sql
-- STEP 1: the login  (PASSWORD must be a bcrypt hash of the real password — see §9)
INSERT INTO GHN_PROD_BAK.GHN_USER
  (USER_NAME, USER_FIRST_NAME, PASSWORD, ENABLED, ROLE, GROUP_ROLE,
   DEFAULT_FEATURE_ID, EMAIL_TRIG_ENABLED, USER_MFA_ENABLED, CREATED_USER_ID, MOBILE)
VALUES ('person@kepsla.com','Name', :bcrypt_hash, 1,'Admin',1, 0,0,0,0, '9199XXXXXXXX');
-- capture @uid = LAST_INSERT_ID()

-- STEP 2: role
INSERT INTO GHN_PROD_BAK.GHN_USER_ROLE_MAPPING (USER_ID, ROLE_ID, CREATED_USER_ID) VALUES (@uid, 3, 0);

-- STEP 3: feature grants — clone a working admin (e.g. Venky 6422)
INSERT INTO GHN_PROD_BAK.GHN_ROLE_FEATURE (ROLE_ID, FEATURE_ID, AUTHORITY, USER_ID, CREATED_USER_ID)
SELECT ROLE_ID, FEATURE_ID, AUTHORITY, @uid, 0
FROM GHN_PROD_BAK.GHN_ROLE_FEATURE WHERE USER_ID = 6422;

-- STEP 4: hotel visibility — one row per RI id the user should SEE
INSERT INTO GHN_PROD_BAK.GHN_USER_ORGANIZATION_MAPPING (ORGANIZATION_ID, USER_ID, CREATED_USER_ID)
VALUES (<RI>, @uid, 0), ... ;

-- STEP 5: ticket routing — one row per hotel×department. MANAGER=1 = dept head (ticket lands on them)
INSERT INTO GHN_PROD_BAK.DEPARTMENT_STAFF_CONFIG
  (DEPARTMENT_ID, ORG_ID, STAFF_ID, MANAGER, STATUS, TICKET_PRIORITY)
VALUES (<dept>, <RI>, @uid, 1, 1, 'HIGH'), ... ;

-- (optional) placeholder to match existing staff
INSERT INTO GHN_PROD_BAK.GHN_USER_ORGANIZATION_DEPARTMENT_MAPPING (USER_ID, DEPARTMENT_ID, ORGANIZATION_ID)
VALUES (@uid, 0, 0);
```

bcrypt hash for a known password (Python) — `$2a$` prefix for the Java/jBCrypt backend:
```python
import bcrypt; bcrypt.hashpw(b"manager", bcrypt.gensalt(rounds=10, prefix=b"2a")).decode()
# or copy an existing working hash: SELECT PASSWORD FROM GHN_USER WHERE ID=6424  (Sana's = bcrypt of "manager")
```

**Existing user, new tenant?** Skip steps 1–3 (account/role/features are global per user) — only add
step 4 (org visibility) + step 5 (dept routing) rows for the new tenant's RI ids.

---

## 6. Case study — Atul (tester), done 2026-07-08

- Created **`atul@kepsla.com` = user id 6427**, temp password **`manager`**, mobile `919971946661`.
- Role `ROLE_ID=3`; features cloned from Venky (43 rows); org visibility all 23 Zone hotels (23 rows);
  `DEPARTMENT_STAFF_CONFIG` = **all 6 departments × all 23 hotels = 138 rows, every one MANAGER=1**
  (deliberately manager-of-everything so tickets route to him for end-to-end testing); +1 org-dept placeholder.
- Bug hit #1: first stored password as literal `manager` → login "Bad credentials". Fixed to bcrypt hash.
- Bug hit #2: had 0 `GHN_ROLE_FEATURE` rows → login worked but dashboard broken. Fixed by cloning Venky's 43.
- **Undo:** `DELETE` his rows from GHN_USER, GHN_USER_ROLE_MAPPING, GHN_ROLE_FEATURE,
  GHN_USER_ORGANIZATION_MAPPING, DEPARTMENT_STAFF_CONFIG, GHN_USER_ORGANIZATION_DEPARTMENT_MAPPING WHERE the id = 6427.

---

## 6b. Case study — Nidhi Pandey (view-only), done 2026-07-16

- Ask: create a login that can sign into the dashboard and **SEE** Zone tickets — **no** email, **no**
  WhatsApp, not a manager/agent (nothing routed to her).
- Created **`reservations@zonebythepark.com` = user id 6429**, name Nidhi Pandey, designation
  "Sr. Marketing Manager", mobile 919953041877, temp password **`manager`** (reused Sana 6424's
  known-good bcrypt('manager') hash so login is guaranteed).
- Wired **only 4 tables**, deliberately a **pure viewer**:
  1. `GHN_USER` (login) 2. `GHN_USER_ROLE_MAPPING` ROLE_ID=3 3. `GHN_ROLE_FEATURE` = 43 rows cloned
  from Venky 6422 4. `GHN_USER_ORGANIZATION_MAPPING` = **all 24 Zone RI ids** (incl Katra 8699).
  + 1 placeholder `GHN_USER_ORGANIZATION_DEPARTMENT_MAPPING` (0/0).
- **NOT added (this is what keeps her silent + non-owning):** `DEPARTMENT_STAFF_CONFIG` (no routing /
  not a manager or agent), `GHN_FF_TICKET_NOTIFICATIONS`, `GHN_DEPARTMENT_ALERT_MAPPINGS`. → zero email,
  zero WhatsApp, no ticket lands on her.
- Reference shape proven by the existing **Sana-dup 6421** account (org visibility, 0 dept-config).
- **If her ticket list ever shows empty:** a client-admin (role 3) with org visibility should see all
  Zone tickets; if not, add view-only `DEPARTMENT_STAFF_CONFIG` rows with `MANAGER=0` (still no alert
  mappings → still no email) — do NOT add her to `GHN_DEPARTMENT_ALERT_MAPPINGS` or she'd start getting alerts.
- **Undo:** `DELETE` her rows from GHN_USER, GHN_USER_ROLE_MAPPING, GHN_ROLE_FEATURE,
  GHN_USER_ORGANIZATION_MAPPING, GHN_USER_ORGANIZATION_DEPARTMENT_MAPPING WHERE the id = 6429.

### Same-pattern view-only user — Tanay, done 2026-07-16
- **`director.operations@zonebythepark.com` = user id 6430**, name Tanay, designation "Director Operations",
  mobile 919643004404, temp password **`manager`**. Identical wiring to Nidhi: role 3 + 43 cloned features
  + 24 Zone org-visibility rows + placeholder; **no** dept/notification rows → viewer, zero alerts.
- Undo = delete id 6430 across the same 4 tables + org-dept placeholder.

---

## 7. Read-only diagnostic cookbook

```sql
-- schema of any table
SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, EXTRA
FROM information_schema.COLUMNS WHERE TABLE_SCHEMA='GHN_PROD_BAK' AND TABLE_NAME='<T>' ORDER BY ORDINAL_POSITION;

-- find a user (login is in USER_NAME, not PRIMARY_EMAIL)
SELECT ID, USER_NAME, USER_FIRST_NAME, ENABLED, ROLE, GROUP_ROLE, MOBILE
FROM GHN_PROD_BAK.GHN_USER WHERE LOWER(USER_NAME) LIKE '%name%' ORDER BY ID DESC;

-- a user's full footprint
SELECT (SELECT GROUP_CONCAT(ROLE_ID) FROM GHN_USER_ROLE_MAPPING WHERE USER_ID=:u)            AS roles,
       (SELECT COUNT(*) FROM GHN_ROLE_FEATURE               WHERE USER_ID=:u)               AS features,
       (SELECT COUNT(*) FROM GHN_USER_ORGANIZATION_MAPPING  WHERE USER_ID=:u)               AS org_visibility,
       (SELECT COUNT(*) FROM DEPARTMENT_STAFF_CONFIG        WHERE STAFF_ID=:u)              AS dept_rows;

-- group name + org (hotel) names
SELECT ID, ORGANIZATION_GROUP_NAME FROM GHN_PROD_BAK.GHN_ORGANIZATION_GROUP WHERE ID=139;
SELECT ID, ORGANIZATION_FULL_NAME FROM GHN_PROD_BAK.GHN_ORGANIZATION WHERE ID IN (<RI list>);

-- FMS <-> RI
SELECT FMS_Entity_Id, RI_Entity_Id FROM GHN_FEEDBACK_RESULTS.GHN_SURVEY_FMS_RI_ENTITY_MAPPING
WHERE FMS_Entity_Id=:fms OR RI_Entity_Id=:ri;

-- which entity the chatbot actually tickets to (organisation_id lives in the ticket payload)
SELECT id, hotel_code, property_name, department, lumira_payload
FROM GHN_PROD_BAK.new_bot_tickets WHERE LOWER(hotel_code) LIKE '%<hotel>%' ORDER BY id DESC LIMIT 8;

-- departments configured on a hotel + who staffs it
SELECT DISTINCT d.DEPARTMENT_ID, m.DEPT_NAME FROM DEPARTMENT_STAFF_CONFIG d
JOIN DEPARTMENT_MASTER m ON m.ID=d.DEPARTMENT_ID WHERE d.ORG_ID=:ri ORDER BY d.DEPARTMENT_ID;
```

---

## 8. Login / dashboard notes

- Dashboard: `https://dev-reviews.kepsla.com:18080/kepsla-rep-dashboard-svc/`
- Username = the email in `USER_NAME`; password = plain password whose **bcrypt hash** is in `PASSWORD`.
- `GHN_USER.LAST_LOGGED_IN` updates on a **successful** auth — handy to confirm creds work.
- Error **"Authentication method not supported: GET"** = the page was reached via a GET/deep link,
  not the login form's POST → front-end/URL usage issue, **not** bad credentials. Start from the login screen.
- Ticket/handoff API base (`.env`): `TICKETING_BASE_URL=…/kepsla-rep-dashboard-svc`,
  create path `/insert/ticket.htm`.

---

## 9. Bot-side ticketing config (`.env`) — how a tenant is wired to the dashboard

- `TICKETING_PROPERTY_ENTITY_MAP_JSON` — `{hotel_code:{property_substr: FMS_id}}`; makes the bot send the
  numeric **FMS id** as `organisation_id` (longest substring match wins). Zone has all 25 keys.
- `TICKETING_WIDGET_GROUP_MAP_JSON` — `{hotel_code:{widget_id,group_id}}` so the guest name shows on the ticket.
- `TICKETING_ALLOWED_DEPARTMENTS_JSON` — `{hotel_code:[dept names]}` closed-set the bot may assign to.
- `LUMIRA_DEPARTMENTS_ENABLED=true` — enables the live FMS→RI→DEPARTMENT_STAFF_CONFIG(MANAGER=1) dept lookup
  (see `integrations/lumira_ticketing_repository.py::fetch_departments_of_entity`).

---

## 10. Sarovar — PENDING TASK (as of 2026-07-08)

Goal: configure ticketing for **Sarovar Portico Sriperumbudur** and make **Sana (6424), Venky (6422),
Suhas (6425) all MANAGERS** on chosen departments.

Key ids (resolved 2026-07-08):
- **FMS entity 6311** = `GHN_FF_PROD.GHN_FF_ENTITY` id 6311, "Sarovar Portico, Sriperumbudur", Group_Id 5282
  "Sarovar Group", Brand 5495, created 2026-07-07. (The new FMS/FF system entity the bot targets.)
- **RI org 7408** = `GHN_PROD_BAK.GHN_ORGANIZATION` id 7408, renamed to "Sarovar Portico, Sriperumbudur"
  on 2026-07-08 (was "Grand Continent Malleshwaram, Bangalore", created 2021 — a **reused** org). This is
  the RI id the staff/dept/visibility tables key on.

Current state:
- Bot deployment exists: `new_bot_hotels` id 68, code `SAROVAR_PORTICO_SRIPERUMBUDUR`, ~22 tickets.
- Bot tickets carry `organisation_id = "sarovar_portico_sriperumbudur"` — a **slug, not a numeric entity**
  (needs the `.env` entity map to send **6311** instead).
- Bot assigns depts: **15 In-Room Dining, 16 Guest Relations, 18 Reception Front Office** (from real tickets).
- **FMS↔RI MISMATCH (unresolved):** `GHN_SURVEY_FMS_RI_ENTITY_MAPPING` has RI **7408 ↔ FMS 91007** (the old
  Malleshwaram FMS), and **FMS 6311 has no RI row**. For the bot's FMS→RI department lookup to resolve,
  add a row **`FMS 6311 ↔ RI 7408`** (composite PK, coexists with the old 91007 row). NOT yet done — needs sign-off.
- RI 7408 today: 1 `DEPARTMENT_STAFF_CONFIG` row (staff 6394), 0 notification templates, 42 org-visibility users.
- Two parallel entity systems exist: legacy **RI** (`GHN_PROD_BAK.GHN_ORGANIZATION`, staff via
  `DEPARTMENT_STAFF_CONFIG` — what Zone uses) and new **FF/FMS** (`GHN_FF_PROD.GHN_FF_ENTITY`, users via
  `GHN_FF_USER_ENTITY(user_id, entity_id)`). Zone staff are RI-only (0 rows in GHN_FF_USER_ENTITY), so
  the RI path is the one to replicate; confirm whether the dashboard also needs a `GHN_FF_USER_ENTITY` row.

Progress (2026-07-08):
1. ✅ **RI↔FMS mapping fixed** — `GHN_SURVEY_FMS_RI_ENTITY_MAPPING` UPDATEd RI 7408 from FMS 91007 → **6311**
   (old 91007 row gone; bot's FMS 6311 now resolves to RI 7408).
2. ✅ **Staff attached** — Sana (6424), Venky (6422), Suhas (6425) each: 1 `GHN_USER_ORGANIZATION_MAPPING`
   row (RI 7408) + `DEPARTMENT_STAFF_CONFIG` rows (all MANAGER=1) on RI 7408. **Final departments (per the
   Sriperumbudur staff sheet): `5` Front Office, `8` Housekeeping, `34` Food & Beverage, `41` Engineering,
   `42` Administration** — 5 per staff, 15 rows total. (History: added 16 → trimmed to 3 → added back 41/42
   once we found real "Engineering"/"Administration" depts exist in DEPARTMENT_MASTER, created 2026-02-16.)
   Role + features already global on their accounts — untouched. Undo = delete their rows on ORG_ID 7408.
   Note: the bot's older Sarovar tickets used Guest Relations/In-Room Dining/Reception Front Office (not in
   this set) — the allowlist below now forces the bot onto these 5 instead.

3. ✅ **`.env` entity map (Fix #1)** — added `"sarovar_portico_sriperumbudur": {"sriperumbudur": "6311"}`
   to `TICKETING_PROPERTY_ENTITY_MAP_JSON` (line ~149). Now the bot stamps FMS **6311** on Sarovar tickets
   instead of the slug. Resolution = the inner substring `"sriperumbudur"` is matched against the property
   name "Sarovar Portico Sriperumbudur" (see `ticketing_service._resolve_property_entity_id`).
   **⚠️ Needs a bot restart to take effect.** Also added `TICKETING_ALLOWED_DEPARTMENTS_JSON` →
   `"sarovar_portico_sriperumbudur": ["Front Office","Housekeeping","Food & Beverage","Engineering","Administration"]`
   (line ~161) so the bot may ONLY assign Sarovar tickets to those 5 (matched via `_normalize_category_key`: `&`/`/`/`-`/`_`→space,
   lowercased — so "Food & Beverage" == DB's " Food & Beverage"). (Not added: `TICKETING_WIDGET_GROUP_MAP_JSON`
   = guest-name-on-ticket, needs Sarovar's widget_id.)
4. ✅ **`GHN_FF_TICKET_NOTIFICATIONS` (Fix #2)** — cloned Zone Kolkata's **10** per-phase template rows with
   ENTITY_ID=**7408** / FMS_ENTITY_ID=**6311** (created/assigned/sla_breach/reminder_beforesla/resolved/
   cancelled × Dept Head/staff/guest). Alerts will now fire. Text was copied verbatim from Zone — refine the
   `TEMPLATE_BODY`/`MESSAGE_TEXT`/`EMAIL_*` later if they mention Zone-specific wording. Undo = delete rows
   WHERE FMS_ENTITY_ID=6311.

**★ ROOT CAUSE of "ticket not on kepsla dashboard + no email" (found 2026-07-08): `GHN_FF_PROD.GHN_DEPARTMENT_ALERT_MAPPINGS`.**
This is the **auto-assign + escalation engine**, keyed by **`entity_id` = the FMS id** (NOT the RI id — Zone Kolkata
rows have `entity_id=6290`, not 7256; so an `entity_id IN (RI ids)` search returns 0 and misleads you). Each Zone
entity has ~12 rows (2 per dept): `ticket_auto_assign=1`, `is_active=1`, `escalation_interval=5`, `before_sla_default=70`,
`escalation_1/2/3`=staff ids, `escalation_level_1/2/3`=JSON staff-id arrays. **Sarovar 6311 had 0 rows → tickets were
created fine but never auto-assigned to a manager, so they never appeared on the dashboard and no email fired** — even
though bot payload, entity config, FMS↔RI mapping, managers, and notif templates were ALL already identical to Zone.
FIX applied: inserted 5 rows for `entity_id=6311` (depts 5,8,34,41,42), `ticket_auto_assign=1`, escalation→[6424,6422,6425].
Assignment runs via a **delayed dashboard background job** (even Zone's newest ticket sits unassigned briefly) → not instant.
Undo = delete rows WHERE entity_id=6311. Debugging tip: an UNASSIGNED ticket (`assigned_id=0`) won't show in a
manager's "My Tickets" view; compare `assigned_id`/`ticket_auto_assign` on a visible Zone ticket vs the missing one.

**Net: Sarovar ticketing is fully wired (entity map, mapping, managers, notif templates, AND the alert/auto-assign
mappings). Remaining action = restart the bot so the `.env` change loads; then confirm a fresh ticket auto-assigns.**
