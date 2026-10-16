# ZONE go-live — running TODO

Living checklist. We add to this as we find things.
Only Zone matters right now (`hotel_id = 67`, code `ZONE`).

**Legend:** `[ ]` todo · `[x]` done · `[?]` waiting on info · `[!]` blocker

Last updated: 2026-09-09

---

## 1. Database tables

- [x] Work out every table the bot uses — **34 to create**, 9 already exist in prod
- [x] Build `prod_create_tables.sql` from dev's real DDL (verified 34/34 exact match)
- [ ] Run `prod_create_tables.sql` on prod
- [ ] Confirm 34 tables exist (`prod_verify_tables.sql`, or just count them)

---

## 2. Zone entity IDs + per-property readiness  <- **the master status table**

Dev points at **test** entities (`6313-6336`, all named "... test"). Prod has real ones with
**different numbers**. Verified against prod exports on 2026-09-09.

### 2a. Every Zone location - is it ready to raise a ticket?

**Right now: 0 of 26 can raise a ticket in prod.**

| Bot location key | dev id | **prod FMS id** | Prod entity name | RI id | FMS→RI mapping | Staff config | Ticketing |
|---|---:|---:|---|---:|---|---|---|
| `calangute` | 6314 | **6117** | Zone Connect Calangute, Goa | 8539 | ✅ yes | ❌ **none** | ❌ cannot ticket |
| `chennai` | 6316 | **6125** | Zone by The Park, Chennai | 8531 | ✅ yes | ❌ **none** | ❌ cannot ticket |
| `coimbatore` | 6317 | **6126** | Zone by The Park, Coimbatore | 8535 | ✅ yes | ❌ **none** | ❌ cannot ticket |
| `connect park coimbatore` | 6325 | **6124** | Zone Connect Hotel, Coimbatore | 8490 | ✅ yes | ❌ **none** | ❌ cannot ticket |
| `darjeeling` | 6329 | **6554** | Zone by The Park, Darjeeling | 10815 | ✅ yes | ❌ **none** | ❌ cannot ticket |
| `digha` | 6335 | **—** | — | — | ❌ NO ENTITY | ❌ | ❌ cannot ticket |
| `dimapur` | 6318 | **6151** | Zone Niathu By The Park, Dimapur | 9773 | ✅ yes | ❌ **none** | ❌ cannot ticket |
| `gangtok` | 6328 | **6539** | Zone Connect by The Park Mount Siniolchu, Gangtok | 10793 | ✅ yes | ❌ **none** | ❌ cannot ticket |
| `gopalpur` | 6313 | **6128** | Zone By The Park, Gopalpur | 8557 | ✅ yes | ❌ **none** | ❌ cannot ticket |
| `jaipur` | 6320 | **6123** | Zone by The Park Hotel, Jaipur | 7728 | ✅ yes | ❌ **none** | ❌ cannot ticket |
| `jaisalmer` | 6330 | **—** | — | — | ❌ NO ENTITY | ❌ | ❌ cannot ticket |
| `jammu` | 6321 | **6127** | Zone by The Park, Jammu | 8538 | ✅ yes | ❌ **none** | ❌ cannot ticket |
| `katra` | 6336 | **—** | — | — | ❌ NO ENTITY | ❌ | ❌ cannot ticket |
| `kolkata` | 6315 | **6121** | Zone By The Park Kolkata | 7256 | ✅ yes | ❌ **none** | ❌ cannot ticket |
| `lansdowne` | 6331 | **6130** | Zone Connect by The Park, Lansdowne | — | ❌ **missing** | ❌ **none** | ❌ cannot ticket |
| `mussoorie` | 6324 | **6129** | Zone Connect By The Park, Mussoorie | 8929 | ✅ yes | ❌ **none** | ❌ cannot ticket |
| `new market` | 6327 | **6329** | Zone by The Park New Market Kolkata | 10619 | ✅ yes | ❌ **none** | ❌ cannot ticket |
| `niathu` | 6318 | **6151** | Zone Niathu By The Park, Dimapur | 9773 | ✅ yes | ❌ **none** | ❌ cannot ticket |
| `parra` | 6332 | **6118** | Zone Connect By The Park, Parra Goa | — | ❌ **missing** | ❌ **none** | ❌ cannot ticket |
| `pathankot` | 6323 | **6254** | Zone by The Park, Pathankot | 8542 | ✅ yes | ❌ **none** | ❌ cannot ticket |
| `patna` | 6326 | **6279** | Zone Connect By The Park, Patna | — | ❌ **missing** | ❌ **none** | ❌ cannot ticket |
| `phalodi` | 6319 | **6180** | Zone Palace By The Park, Phalodi, Rajasthan | 10135 | ✅ yes | ❌ **none** | ❌ cannot ticket |
| `port blair` | 6333 | **—** | — | — | ❌ NO ENTITY | ❌ | ❌ cannot ticket |
| `saket` | 6322 | **6119** | Zone Connect By The Park, Saket Delhi | 8541 | ✅ yes | ❌ **none** | ❌ cannot ticket |
| `siniolchu` | 6328 | **6539** | Zone Connect by The Park Mount Siniolchu, Gangtok | 10793 | ✅ yes | ❌ **none** | ❌ cannot ticket |
| `udaipur` | 6334 | **—** | — | — | ❌ NO ENTITY | ❌ | ❌ cannot ticket |

### 2b. Summary of what is broken

| Problem | Count | Locations |
|---|---:|---|
| **No entity in prod at all** | 5 | `digha`, `jaisalmer`, `katra`, `port blair`, `udaipur` |
| **Entity exists, but no FMS->RI mapping** | 3 | `parra` (6118), `lansdowne` (6130), `patna` (6279) |
| **Mapped, but no staff configured** | 16 | everything else |
| **Fully working** | **0** | - |

Note: `dimapur`/`niathu` are the same hotel (FMS 6151) and `gangtok`/`siniolchu` are the same
hotel (FMS 6539) - the 26 location keys resolve to 21 distinct prod entities.

### 2c. Actions

- [ ] **5 locations with no prod entity** - create them in prod FMS, or accept that the bot
      cannot raise tickets for them. Blocking decision.
- [ ] **3 locations missing FMS->RI mapping** - add rows to
      `GHN_FEEDBACK_RESULTS.GHN_SURVEY_FMS_RI_ENTITY_MAPPING` for FMS `6118`, `6130`, `6279`
- [ ] **16 locations missing staff** - insert `DEPARTMENT_STAFF_CONFIG` rows (see section 3)
- [ ] Replace all ids in `.env` -> `TICKETING_PROPERTY_ENTITY_MAP_JSON` -> `"zone"`

> ### [!] TRAP - id 6329 means two different hotels
> `6329` exists in **both** environments but is a **different hotel** in each:
> - dev `6329` = **Darjeeling**
> - prod `6329` = **New Market Kolkata**
>
> If that number is left behind during the swap, Darjeeling complaints silently land on
> New Market Kolkata's staff. No error. Check this line specifically.

### 2d. Zone entities in prod the bot does not use

- [?] Should any of these be added? Currently ignored.

`6120` Zone By The Park Infantry, Bangalore * `6122` Zone by The Park Hotel, Jodhpur *
`6131` Zone Connect By The Park, Indore

---

## 3. Departments Zone needs

Zone is restricted to **4 departments** (set on 2026-07-17). The restriction **is** enforced in
code — `_filter_departments_to_allowlist` at `services/ticketing_service.py:1622` filters the
department list from the DB down to the allowed set, and if nothing survives, no department is
assigned at all.

### 3a. The 4 departments

| Department name | dev dept id | Zone tickets since the restriction |
|---|---:|---:|
| `reservations` | 43 | 208 (still active, latest 2026-09-08) |
| `complaints` | 44 | 35 |
| `Food & Beverage` | 34 | 2 |
| `Banquets` | 1 | 0 — allowed but never triggered |

> Older Zone tickets show many more departments (Reception Front Office 381, Guest Relations 245,
> Book a stay 168, Housekeeping 12 …). **All of that is pre-restriction history.** Those
> department names stop dead on 2026-07-20. Ignore them — they are not what the bot does now.

> The department stored as `" Food & Beverage"` has a **leading space** in `DEPARTMENT_MASTER`
> (dev id 34, length 16) while the config says `"Food & Beverage"`. This is **not** a problem:
> `_normalize_category_key` lowercases, strips, and converts `& - _ /` to spaces, so both sides
> reduce to `food beverage` and match. No action needed.

### 3b. What dev has configured — identical for all 24 hotels

`DEPARTMENT_STAFF_CONFIG`: **4 rows per hotel, one person, no variation.** 24 × 4 = 96 rows,
all with `MANAGER = 1`.

| DEPARTMENT_ID | Department | STAFF_ID | MANAGER | STATUS |
|---:|---|---:|---:|---:|
| 1 | Banquets | 6429 | 1 | 1 |
| 34 | ` Food & Beverage` | 6429 | 1 | 1 |
| 43 | reservations | 6429 | 1 | 1 |
| 44 | complaints | 6429 | 1 | 1 |

Column values on every one of those 96 rows:

| Column | Value |
|---|---|
| `MANAGER` | `1` ← the bot only reads rows where this is 1 |
| `STATUS` | `1` |
| `TICKET_PRIORITY` | `HIGH` |
| `SHIFT_START` / `SHIFT_END` | `09:00:00` / `18:00:00` |
| `ISAVAILABLE` | `'1'` |
| `OUTLET_ID`, `FLOOR_NO`, `GROUP_ID` | `NULL` |

Staff `6429` = `reservations@zonebythepark.com` (Nidhi Pandey, Sr. Marketing Manager).
She is the **only** person on all 4 departments.

### 3c. What prod needs

**16 orgs × 4 departments = 64 rows** in `DEPARTMENT_STAFF_CONFIG`. Same column values as above.

State of prod (checked 2026-09-09):

| Table | Prod state | Action |
|---|---|---|
| `GHN_USER` | 118 Zone users already exist | reuse, don't create |
| `GHN_USER_ORGANIZATION_MAPPING` | all 16 orgs already mapped | nothing to do |
| `DEPARTMENT_STAFF_CONFIG` | **empty for all 16 orgs** | **insert 64 rows** |
| `GHN_SURVEY_FMS_RI_ENTITY_MAPPING` | 3 of 19 missing | add 3 rows |

### 3d. The 16 target orgs (RI ids)

| FMS id | Hotel | RI id |
|---:|---|---:|
| 6121 | Zone By The Park Kolkata | 7256 |
| 6123 | Zone by The Park Hotel, Jaipur | 7728 |
| 6124 | Zone Connect Hotel, Coimbatore | 8490 |
| 6125 | Zone by The Park, Chennai | 8531 |
| 6126 | Zone by The Park, Coimbatore | 8535 |
| 6127 | Zone by The Park, Jammu | 8538 |
| 6117 | Zone Connect Calangute, Goa | 8539 |
| 6119 | Zone Connect By The Park, Saket Delhi | 8541 |
| 6254 | Zone by The Park, Pathankot | 8542 |
| 6128 | Zone By The Park, Gopalpur | 8557 |
| 6129 | Zone Connect By The Park, Mussoorie | 8929 |
| 6151 | Zone Niathu By The Park, Dimapur | 9773 |
| 6180 | Zone Palace By The Park, Phalodi | 10135 |
| 6329 | Zone by The Park New Market Kolkata | 10619 |
| 6539 | Zone Connect ... Siniolchu, Gangtok | 10793 |
| 6554 | Zone by The Park, Darjeeling | 10815 |

- [ ] Add FMS→RI mapping rows for `6118` Parra Goa, `6130` Lansdowne, `6279` Patna
      (then they need 4 staff rows each too — 3 × 4 = 12 more)

---

## 4. STILL NEEDED FROM PROD — 2 answers, then the INSERTs write themselves

### 4a. Prod department ids for the 4 names

- [ ] Run this. Dev uses 1 / 34 / 43 / 44 — prod may differ.

```sql
SELECT ID, CONCAT('[', DEPT_NAME, ']') AS name_delimited, CHAR_LENGTH(DEPT_NAME) AS len
FROM GHN_PROD_BAK.DEPARTMENT_MASTER
WHERE LOWER(TRIM(DEPT_NAME)) IN ('reservations','complaints','food & beverage','banquets')
ORDER BY ID;
```

If any of the 4 is missing from prod, it has to be created before the bot can use it.

### 4b. Which prod user replaces Nidhi

Dev's `reservations@zonebythepark.com` **does not exist in prod**.

- [!] **Decision needed.** Recommended: **`6480` `director.operations@zonebythepark.com`**
      — already mapped to all 16 Zone orgs, and is the same person dev uses as the secondary.

Other candidates already mapped to all 16 prod orgs:

| prod id | Login | Name |
|---:|---|---|
| **6480** | `director.operations@zonebythepark.com` | Opsdirectors ← recommended |
| 11529 | `director.foodproduction@zonebythepark.com` | Director F&B |
| 6585 | `training@zonebythepark.com` | TrainingCorp |
| 11387 | `training.div@zonebythepark.com` | Vijayashree |
| 6484 | `zonehotelsdemo@kepsla.com` | Demo (Kepsla) |
| 5344 | ? | [ ] look up |
| 11528 | ? | [ ] look up |

Also available: `digitalmarketing@zonebythepark.com` (prod `10145`, also a Nidhi, mapped to 14/16).

```sql
SELECT ID, USER_NAME, USER_FIRST_NAME, USER_LAST_NAME, DESIGNATION, ENABLED
FROM GHN_PROD_BAK.GHN_USER WHERE ID IN (5344, 11528);
```

### 4c. Confirm after the inserts

All 19 must say `ok`:

```sql
SELECT e.ID AS fms_entity_id, e.Entity_Name, m.RI_Entity_Id,
       COUNT(DISTINCT dsc.DEPARTMENT_ID) AS manager_depts,
       CASE WHEN m.RI_Entity_Id IS NULL              THEN 'NO MAPPING'
            WHEN COUNT(DISTINCT dsc.DEPARTMENT_ID)=0 THEN 'NO STAFF'
            ELSE 'ok' END AS verdict
FROM GHN_FF_PROD.GHN_FF_ENTITY e
LEFT JOIN GHN_FEEDBACK_RESULTS.GHN_SURVEY_FMS_RI_ENTITY_MAPPING m ON m.FMS_Entity_Id = e.ID
LEFT JOIN GHN_PROD_BAK.DEPARTMENT_STAFF_CONFIG dsc
       ON dsc.ORG_ID = m.RI_Entity_Id AND dsc.MANAGER = 1
WHERE e.ID IN (6117,6118,6119,6121,6123,6124,6125,6126,6127,6128,
               6129,6130,6151,6180,6254,6279,6329,6539,6554)
GROUP BY e.ID, e.Entity_Name, m.RI_Entity_Id ORDER BY e.ID;
```

---

## 5. Widget / group IDs

Dev Zone uses `widget_id = 61`, `group_id = 139`. Prod will differ.

- [ ] Find prod's values

```sql
SELECT widget_id, group_id, COUNT(*) AS rows_seen, MAX(timestamp) AS last_seen
FROM GHN_PROD_BAK.KEBO_ENGAGE_SESSIONS
WHERE timestamp > NOW() - INTERVAL 180 DAY
GROUP BY widget_id, group_id ORDER BY COUNT(*) DESC LIMIT 25;
```

- [ ] Put them in `.env` → `TICKETING_WIDGET_GROUP_MAP_JSON` → `"zone"`

---

## 6. Hotel configuration - what to copy (checklist item #4)

Zone is **pre-booking only**. Both its services are `pre_booking`; there is no during-stay or
post-stay config to carry.

### 6a. Row-by-row copy list (Zone = `hotel_id 67`)

| # | Table | Zone rows | Notes |
|---:|---|---:|---|
| 1 | `new_bot_hotels` | 1 | **id must stay 67** |
| 2 | `new_bot_properties` | 24 | ids must be preserved - KB books/pages point at them |
| 3 | `new_bot_business_config` | 10 | `json_section.knowledge_base` alone is 432,459 chars |
| 4 | `new_bot_services` | 2 | both `pre_booking`, both form-mode |
| 5 | `new_bot_prompt_registry` | 27 + **21 global** | see 6c - copy the global rows too |
| 6 | `new_bot_kb_files` | 24 | |
| 7 | `new_bot_kb_books` | 770 | FK to hotel + property |
| 8 | `new_bot_kb_pages` | 1,023 | what the answerer grounds on |
| 9 | `new_bot_kb_book_pages` | (joins 7 + 8) | composite PK |
| 10 | `bot_hotel_images_scraper` | 389 | |
| 11 | `new_bot_location_intros` | 45 | table is NOT auto-created - see section 1 |
| 12 | `new_bot_widget_deployments` | 1 | **edit `allowed_origins` after copying** |

Everything else stays empty (conversations, messages, tickets, sessions, guests, bookings, logs).

### 6b. The 2 services

| id | service_id | name | phase | ticketing_mode |
|---:|---|---|---|---|
| 82 | `support_request` | Support Request | pre_booking | form |
| 83 | `room_booking_request` | room booking request | pre_booking | form |

- [ ] Confirm both arrive with `service_prompt_pack`, `form_config` and
      `generated_system_prompt` intact (they are JSON / LONGTEXT columns)
- [ ] `generated_system_prompt_override = 1` on both - **this is a lock.** It stops the app
      regenerating the prompt on startup. If the flag is lost in the copy, the hand-tuned prompt
      is overwritten on first boot.

### 6c. Prompts - copy the global rows as well

`new_bot_prompt_registry` is 76 rows in dev:

| Scope | Rows | Copy? |
|---|---:|---|
| Zone (`hotel_id = 67`) | 27 | **yes** |
| Global (`hotel_id IS NULL`) | 21 | **yes** - fallback layer |
| Other hotels (66, 68, 69) | 28 | no, unless those hotels also go live |

Zone's 27 rows already override all 21 global keys, so Zone is self-sufficient - but copy the
globals anyway so nothing falls through if a new prompt key is added later.

The big ones, for sanity-checking the copy landed:

| Prompt key | Size |
|---|---:|
| `orchestrator.book_answerer` | 105,699 chars |
| `orchestrator.main_router` | 58,306 chars |
| `service_writer.bot_briefing` | 20,858 chars |

Several keys also have a `gpt-5.4-mini` variant row alongside the default - both must come.

### 6d. Widget

| Field | Dev value | Action |
|---|---|---|
| `widget_key` | `wk_kOKo8Vwt1rGBE7ZCSLhYaFeC` | keep |
| `hotel_code` | `zone` | keep |
| `bot_name` | `Zoogle` | keep |
| `model` | `gpt-4.1-mini` | confirm this is the intended prod model |
| `phase` | `pre_booking` | keep |
| `logo_url` | `/static/branding/zone-icon.png` | confirm the asset ships with the deploy |
| `allowed_origins` | `["*"]` | **[!] change to real Zone domains - see section 9b** |

### 6e. Verify after copying

- [ ] `SELECT id, code, name FROM new_bot_hotels WHERE id = 67;` returns Zone
- [ ] `SELECT COUNT(*) FROM new_bot_properties WHERE hotel_id = 67;` = 24
- [ ] `SELECT COUNT(*) FROM new_bot_kb_pages WHERE hotel_id = 67;` = 1023
- [ ] `SELECT COUNT(*) FROM new_bot_prompt_registry WHERE hotel_id = 67;` = 27
- [ ] `SELECT COUNT(*) FROM new_bot_prompt_registry WHERE hotel_id IS NULL;` = 21
- [ ] `SELECT service_id, generated_system_prompt_override FROM new_bot_services WHERE hotel_id = 67;`
      - both rows must show `1`
- [ ] No orphans - section 1's verify script check 7

### 6f. Files that ship with the code, not the database

- `config/properties/*.json` - the filename must match `business.id` or "pull from KB" is empty
- `config/business_config.json`, `config/ird_serving_windows.json`
- `config/knowledge_base/uploads/<tenant>/*.txt`
- `data/runtime/model_override.json`
- `static/branding/zone-icon.png` (the widget logo)

Do **not** carry `data/runtime/local_contexts.json`, `data/rag/local_index.json`,
`data/ticketing/local_tickets.*`.

---

## 7. `.env` changes for prod

- [ ] `DATABASE_URL` → prod IP (rest of the string unchanged)
- [ ] `TICKETING_BASE_URL` and `TICKET_API` → prod host (currently `dev-reviews.kepsla.com:18080`)
- [ ] `TICKETING_LOCAL_MODE` stays `false`
- [ ] `TICKETING_PROPERTY_ENTITY_MAP_JSON` → new Zone ids from §2a
- [ ] `TICKETING_WIDGET_GROUP_MAP_JSON` → new values from §5
- [ ] `TICKETING_ALLOWED_DEPARTMENTS_JSON` → confirm against §4
- [ ] `SECRET_KEY`, `API_GATEWAY_API_KEY` → rotate
- [ ] `APP_ENV` → prod, `DEBUG=false`
- [ ] `new_bot_widget_deployments.allowed_origins` → prod domain (this is in the DB, not `.env`)

---

## 8. After go-live

- [ ] Restart the app (services are cached per hotel until restart; KB cache is 1 hour)
- [ ] Raise **one real ticket** end-to-end and confirm the reference is **not** `LOCAL-*`
      (a `LOCAL-*` ref means it never reached the ticketing API)
- [ ] Set up a nightly delete on the 8 `new_bot_log_*` tables — the app only prunes at
      startup, which is why dev grew to 1.09 GB

---

## 9. Mapping to the official go-live checklist

| # | Task | Where it stands | Covered in |
|---:|---|---|---|
| 1 | Push code to Prod | not started — outside this doc | — |
| 2 | Separate Dev container | not started — outside this doc | — |
| 3 | **Validate Entity Configuration** | **done** — prod ids found, 5 missing, 1 id-collision trap | §2 |
| 4 | **Validate Hotel Configuration** | **done** — 2 services, widget, 28 prompts, 10 config keys catalogued | §6, §6a |
| 5 | **Department & Staff Configuration** | **blocked** — 4 departments identified, prod has 0 staff rows | §3, §4 |
| 6 | **Create tables in Prod DB** | **script ready & verified** — `prod_create_tables.sql` (34 tables) | §1 |
| 7 | Validate app after 1st deploy | pending deploy | §8 |
| 8 | **Notification & Alert Mapping** | **researched — see §9a** | §9a |
| 9 | **Script Security Review** | **finding raised — see §9b** | §9b |
| 10 | Production Smoke Testing | pending deploy — the `LOCAL-*` check is the key one | §8 |
| 11 | Production Logs Monitoring | note: log tables need a retention job or they hit 1 GB | §8 |
| 12 | Final Sign-off | after the above | — |

### 9a. Notification & Alert Mapping (checklist #8)

Notifications run on **two independent engines**, and they live in **different databases**:

| Channel | Table | Database | Dev rows |
|---|---|---|---:|
| Mail | `DEPARTMENT_STAFF_CONFIG` (`STATUS = 1` enables) | `GHN_PROD_BAK` | 96 Zone rows, all `STATUS=1` |
| WhatsApp | `GHN_DEPARTMENT_ALERT_MAPPINGS` | **`GHN_FF_PROD`** | 706 total |
| Ticket notifications | `GHN_FF_TICKET_NOTIFICATIONS` | **`GHN_FF_PROD`** | 387 |
| User notifications | `GHN_FF_USER_NOTIFICATION` | **`GHN_FF_PROD`** | 318 |

> These are **not** in the bot's database. Setting `DEPARTMENT_STAFF_CONFIG.STATUS = 0` turns
> mail off for a row without removing it. The 64 rows we insert in §3c will switch mail
> notifications **on** for whoever we assign — worth confirming that is wanted before inserting.

- [ ] Check WhatsApp alert rows for the 16 Zone RI orgs in prod

```sql
SELECT * FROM GHN_FF_PROD.GHN_DEPARTMENT_ALERT_MAPPINGS LIMIT 5;
```

(run this first to learn the column names, then filter by the Zone entity/org ids)

### 9b. Script Security Review (checklist #9 — marked Critical)

- [!] **The Zone widget is currently open to every website on the internet.**

`new_bot_widget_deployments.allowed_origins` for Zone is `["*"]`. Traced through the code:

| Check | File | Behaviour with `"*"` |
|---|---|---|
| Origin allow | `services/widget_deployment_service.py:662` | `if "*" in allowed: return True` — **any origin passes** |
| Iframe embedding | `api/middleware/widget_origin.py:77` | returns `frame-ancestors *` — **embeddable anywhere** |

The widget key (`wk_kOKo8Vwt1rGBE7ZCSLhYaFeC`) is public by design — it sits in the embed
`<script>` tag on the website. With `["*"]`, anyone who copies that tag can run Zone's bot on
their own site and spend Zone's LLM budget. There is no per-origin restriction stopping them.

The code's own comment says: *"Empty allowlist = wildcard (only useful for dev; admins should
set domains)."*

- [ ] Set `allowed_origins` to the real Zone domains before go-live, e.g.
      `["https://www.zonebythepark.com", "https://zonebythepark.com"]`
- [ ] Confirm rate limiting is on — dev has `API_GATEWAY_RATE_LIMIT_ENABLED=true`,
      80 requests / 60s. That is the only other brake.
- [ ] `API_GATEWAY_AUTH_ENABLED` is `false` in dev — decide whether prod needs it on
- [ ] `APP_ENV=development` and `DEBUG=true` in dev — **must** be `production` / `false` in prod

---

## Open questions

- [!] **Which prod user replaces Nidhi** for the 4 departments? (recommend `6480` Ops Director)
- [!] **The 5 locations missing from prod** — digha, jaisalmer, katra, port blair, udaipur.
      Create them in prod FMS, or disable ticketing for those 5?
- [ ] Who are prod users `5344` and `11528`?
- [ ] Do the 4 department names exist in prod `DEPARTMENT_MASTER`, and with which ids?
- [ ] Lock `allowed_origins` to real Zone domains, or leave the `["*"]` wildcard?
- [?] Should Bangalore Infantry / Jodhpur / Indore be added to the bot?
- [?] Which of the other 6 hotels go to prod later — copy them now or Zone only?
