# new_bot — prod tables & data transfer

Everything the bot touches in MySQL, what to create, and what data to carry over.

## Verification record (2026-09-09)

Nothing here was written from memory. Every check below was executed.

| # | Check | Result |
|---:|---|---|
| 1 | Every DDL statement dumped from dev with `SHOW CREATE TABLE` (not hand-written) | 34 tables |
| 2 | **Character-for-character diff** of each statement in `prod_create_tables.sql` vs dev's live DDL, after only the two documented edits (`IF NOT EXISTS`, strip `AUTO_INCREMENT=<n>`) | **34/34 exact** |
| 3 | FK ordering — every `REFERENCES` target is created earlier in the file | PASS |
| 4 | No leftover `AUTO_INCREMENT=<n>` counters | PASS |
| 5 | Every statement properly terminated | PASS |
| 6 | Column sets match live dev | PASS (34/34) |
| 7 | `prod_verify_tables.sql` executed against dev (read-only) | All 16 statements ran clean, 8/8 checks returned expected results |
| 8 | `prod_copy_config_data.sh` shell syntax (`bash -n`) | PASS |
| 9 | **Code → DB:** every `__tablename__` and every raw-SQL `FROM`/`JOIN`/`INTO`/`UPDATE` target across `.py`/`.js`/`.jsx`/`.ts`/`.sql` | 41 identifiers, **all accounted for**, 0 unaccounted |
| 10 | **DB → code:** asked `information_schema` for every bot-shaped table and diffed against the code list | Only 3 extras, all Java-side (see §B) |

Two bugs were found and fixed by these checks: a semicolon inside a string literal in
`prod_verify_tables.sql` (the `mysql` client splits on it — would have failed in prod), and a dead
`HOTEL_IDS` variable in the copy script that did nothing.

**Dev and prod differ only by IP.** Same database names on both (`GHN_PROD_BAK`, `GHN_FF_PROD`,
`GHN_FEEDBACK_RESULTS`), different data. No code change is needed for the database names.

---

## Files in this set

| File | What it does | Safe to re-run? |
|---|---|---|
| `prod_create_tables.sql` | Creates all **34** bot tables. Every statement is `CREATE TABLE IF NOT EXISTS`. | Yes — skips anything that exists |
| `prod_copy_config_data.sh` | Dumps the **12** config tables from dev and loads them into prod, ids preserved. | Yes — it replaces the config rows |
| `prod_verify_tables.sql` | 8 read-only checks. Confirms tables, legacy deps, ids, orphans. | Yes — read-only |

---

## Totals

| | Count |
|---|---:|
| Bot tables to create in prod | **34** |
| — of those, tables whose **data must be copied** | **12** |
| — tables created but left **empty** | **22** |
| Legacy GHN tables that **already exist in prod** | **9** |
| **Total tables the bot touches** | **43** |

---

## A. Tables to CREATE — with data verdict

All 34 are in `prod_create_tables.sql`, already in foreign-key-safe order.

### A1 · COPY THE DATA (12)

This is the whole configured product. Without these rows the bot knows nothing.
Total ~8 MB.

| Table | Dev rows | What it holds |
|---|---:|---|
| `new_bot_hotels` | 7 | The tenants. Every other table points at its `id`. **Ids must be preserved.** |
| `new_bot_properties` | 74 | Individual properties. The names the bot speaks come from `name`. **Ids must be preserved.** |
| `new_bot_business_config` | 65 | Per-hotel settings, key/value. |
| `new_bot_services` | 26 | **The live service-agent prompts.** `generated_system_prompt`, `service_prompt_pack` (IRD menu), `form_config`. |
| `new_bot_prompt_registry` | 76 | Router and answerer prompts, including per-hotel overrides. |
| `new_bot_kb_files` | 73 | The uploaded KB source files. |
| `new_bot_kb_books` | 1,557 | Those files organised into books per property. |
| `new_bot_kb_pages` | 2,221 | The page bodies — **what the bot actually reads to answer**. |
| `new_bot_kb_book_pages` | 3,521 | Which page belongs to which book. Composite PK. |
| `bot_hotel_images_scraper` | 2,547 | Photos shown in chat. `category` drives the property gallery. |
| `new_bot_location_intros` | 45 | Instant intro text when a guest picks a city from the dropdown. |
| `new_bot_widget_deployments` | 2 | Widget config. **After copying, edit `allowed_origins` to prod domains.** |

### A2 · CREATE BUT LEAVE EMPTY (22)

The table must exist — the app queries it. The dev rows are test traffic and debug logs.

**Config tables that are already empty in dev (4)** — nothing exists to copy:

`new_bot_capabilities` · `new_bot_intents` · `new_bot_restaurants` · `new_bot_menu_items`

> `restaurants` / `menu_items` are a legacy IRD model. The live menu is in
> `new_bot_services.service_prompt_pack`, not here.

**Runtime — fills itself as real guests chat (9):**

| Table | Dev rows | Why not to copy |
|---|---:|---|
| `new_bot_conversations` | 3,834 | 228 MB of dev conversation context |
| `new_bot_messages` | 65,289 | Dev chat transcripts |
| `new_bot_tickets` | 2,337 | Dev test tickets — these would appear as real prod history |
| `new_bot_sessions` | 1,029 | Dev widget sessions |
| `new_bot_guests` | 209 | Test guests |
| `new_bot_bookings` | 45 | Test bookings |
| `new_bot_ird_guest_orders` | 7 | Dev in-room-dining carts |
| `new_bot_orders` | 0 | Legacy, empty |
| `new_bot_order_items` | 0 | Legacy, empty |

**Logs — 1.09 GB in dev (8):**

| Table | Dev rows | Size |
|---|---:|---:|
| `new_bot_log_backend` | 129,246 | 601.1 MB |
| `new_bot_log_llm` | 11,171 | 467.2 MB |
| `new_bot_log_routing` | 2,667 | 8.4 MB |
| `new_bot_log_tickets` | 1,198 | 7.4 MB |
| `new_bot_log_messages` | 1,413 | 3.4 MB |
| `new_bot_log_errors` | 113 | 1.1 MB |
| `new_bot_log_llm_daily` | 186 | 0.1 MB |
| `new_bot_log_chips` | 40 | 0.1 MB |

> **Set up log retention before go-live.** The app's own `db_logging_retention_days` (default 7)
> is only applied by `prune()` **at startup**, so a prod process that stays up for weeks never
> trims. Add a nightly job:
> ```sql
> DELETE FROM new_bot_log_backend WHERE ts < NOW() - INTERVAL 7 DAY LIMIT 50000;
> -- repeat for the other 7 new_bot_log_* tables
> ```

**KB scraper (1):** `scrape_jobs` — 33 rows / 35 MB of job history. Only needed if you deploy the
KB scraper alongside the bot.

---

## B. Legacy tables that ALREADY EXIST in prod

The bot does not own or create these. They belong to the GHN / Lumira application and prod
already has them with prod's own data. **Nothing to create, nothing to copy** — you only need
grants. `prod_verify_tables.sql` Check 3 confirms all 9 resolve.

### In the same database as the bot tables (7)

| Table | Bot's access | Use case |
|---|---|---|
| `KEBO_ENGAGE_SESSIONS` | **read + write** | The bot writes every chat turn here. This is what the Kepsla dashboard's Chat History reads. Also carries agent-handoff ownership — a handoff row here mutes the bot. |
| `GHN_CHAT_BOT_CUSTOMER_DETAILS` | **read + write** | On form submit the bot writes guest name / email / phone keyed by `Session_Id`, so the dashboard shows a name instead of a blank. Normally the Java widget backend fills this; our bot bypasses that, so it writes the row itself. |
| `GHN_CHATBOT_FORMS` | **read + write** | The bot upserts its in-chat form definitions here from `new_bot_services.form_config`, so the Lumira frontend always renders the current fields. |
| `GHN_OPERATION_TICKETS` | read | Looks up a guest's existing **open** tickets so a follow-up edits the existing ticket instead of raising a duplicate. |
| `DEPARTMENT_STAFF_CONFIG` | read | Which departments exist for an entity, and which manager to assign a ticket to. |
| `DEPARTMENT_MASTER` | read | Department id → department name. |
| `ORGANIZATION_OUTLETS` | read | Outlet list per FMS entity. |

### In other databases — cross-database SELECT (2)

| Table | Bot's access | Use case |
|---|---|---|
| `GHN_FF_PROD.GHN_FF_GUEST_INFO` | read | Guest name, room number and stay dates. **The journey phase (pre-arrival / in-stay / post-stay) is derived from these dates**, so this drives most routing. |
| `GHN_FEEDBACK_RESULTS.GHN_SURVEY_FMS_RI_ENTITY_MAPPING` | read | FMS entity id → RI entity id. Every department lookup joins through this table. |

### Grants needed

```sql
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, INDEX, ALTER ON GHN_PROD_BAK.* TO '<user>'@'%';
GRANT SELECT ON GHN_FF_PROD.GHN_FF_GUEST_INFO                         TO '<user>'@'%';
GRANT SELECT ON GHN_FEEDBACK_RESULTS.GHN_SURVEY_FMS_RI_ENTITY_MAPPING TO '<user>'@'%';
```

### Not ours — bot-shaped names that belong to the Java side

Asking the database for every table matching `new_bot%` / `bot_%` / `KEBO%` / `%scrape%` and
diffing against the codebase turned up three more. **Zero references anywhere in our code** —
Python, JS, SQL or JSON. They belong to the Java widget backend, already exist in prod, nothing
to do:

`BOT_IDENTIFIER_INFO` (31 rows) · `KEBO_CONVERSATION_AUDIT_LOGS` (1,347) · `KEBO_DEVICE_DETAILS` (1,486)

---

## C. How to run it

### Step 1 — create the tables

```bash
mysql -h <PROD_IP> -u <user> -p GHN_PROD_BAK < prod_create_tables.sql
```

Creates all 34. Every statement is `CREATE TABLE IF NOT EXISTS`, ordered so foreign keys always
point at a table that already exists. Safe to run again if it stops halfway.

### Step 2 — copy the config data

```bash
./prod_copy_config_data.sh
```

Edit the connection variables at the top of the script first. It dumps the 12 config tables from
dev with primary keys preserved and loads them into prod.

### Step 3 — verify

```bash
mysql -h <PROD_IP> -u <user> -p GHN_PROD_BAK -t < prod_verify_tables.sql
```

The `-t` flag prints boxed tables. Eight checks:

| Check | Passes when |
|---|---|
| 1 · Bot tables | 34 rows, every one says `present` |
| 2 · Count | `34` and `OK` |
| 3 · Legacy deps | 9 rows, every one says `present` |
| 4 · `KEBO_ENGAGE_SESSIONS` | `4` and `OK - real GHN table` |
| 5 · Row counts | The 12 config tables match the dev numbers |
| 6 · Hotel ids | `54, 65, 66, 67, 68, 69, 70` — **not** 1–7 |
| 7 · Orphans | Every count is `0` |
| 8 · Widget origins | Prod domains, not dev |

---

## D. Two things that will bite you

### 1. Hotel ids must be preserved

The live ids are **non-contiguous and do not start at 1** — rows were deleted along the way:

| id | code | name |
|---:|---|---|
| 54 | `ICONIQA` | iconiqa |
| 65 | `NEWKHIL` | newkhil |
| 66 | `KHIL` | khil |
| 67 | `ZONE` | Zone |
| 68 | `SAROVAR_PORTICO_SRIPERUMBUDUR` | Sarovar Portico Sriperumbudur |
| 69 | `MANGO_GROVE_CHANDIGARH` | Mango Grove Hotel |
| 70 | `DEFAULT` | DEFAULT |

A plain insert into an empty prod table would renumber them 1–7 and silently detach every
property, prompt, service and KB page from its hotel. `prod_copy_config_data.sh` preserves them;
Check 6 and Check 7 catch it if anything goes wrong.

`ICONIQA`, `NEWKHIL`, `KHIL` and `DEFAULT` look like test/scaffold tenants. If they should not
exist in prod, set `DROP_HOTEL_IDS="54,65,66,70"` at the top of `prod_copy_config_data.sh`. It
copies all 7 and then deletes those from prod, letting the `ON DELETE CASCADE` foreign keys
remove their properties, config, services, prompts, KB books/pages/joins and images correctly.

> Filtering at dump time is **not** safe here: `new_bot_kb_book_pages` has no `hotel_id` column
> at all, so a `--where` filter cannot reach it and you would import join rows pointing at books
> that were never copied. The script deletes after load for exactly this reason.
>
> Two tables have **no** FK to `new_bot_hotels` and so are not cascaded — the script deletes
> `new_bot_location_intros` explicitly, and `new_bot_widget_deployments` keys off `hotel_code`
> (not `hotel_id`), so if you drop a tenant, remove its widget row by hand.

### 2. `KEBO_ENGAGE_SESSIONS` — the one landmine

This table **is** in the app's ORM, so the app will create it if it is missing. But the ORM
definition is **4 columns short** of the real GHN table: `CACHED_TOKENS`, `INPUT_TOKENS`,
`OUTPUT_TOKENS`, `auto_load_form`.

In prod it already exists, so nothing happens. But if the app is ever pointed at a database where
it is absent, you get a brand-new crippled empty copy **instead of an error**, and the Java side
then writes into it. Check 4 in the verify script guards against exactly this.

---

## E. Things that are not database changes

| Where | What | Action |
|---|---|---|
| `.env` `DATABASE_URL` | Dev IP | **Prod IP.** Everything after the host is unchanged. |
| `.env` `TICKETING_BASE_URL` + `TICKET_API` | `dev-reviews.kepsla.com:18080` | Prod dashboard service. **Both** are set. |
| `.env` `TICKETING_LOCAL_MODE` | `false` | Must stay `false`. A `LOCAL-*` ticket ref means nothing reached the real API. |
| `.env` `TICKETING_PROPERTY_ENTITY_MAP_JSON` | Dev FMS entity ids, keyed by **location** | Re-issue from prod FMS |
| `.env` `TICKETING_WIDGET_GROUP_MAP_JSON` | zone → widget 61 / group 139; sarovar → widget 53 / group 244 | Re-issue from prod |
| `.env` `TICKETING_ALLOWED_DEPARTMENTS_JSON` | Per-brand department whitelist | Confirm the names exist in prod `DEPARTMENT_MASTER` |
| `.env` `SECRET_KEY`, `API_GATEWAY_API_KEY` | Dev secrets | **Rotate** |
| `.env` `APP_ENV`, `DEBUG` | Dev values | Prod values, `DEBUG=false` |

**Ships with the code checkout, not the DB:** `config/properties/*.json` (the filename must match
`business.id` or "pull from KB" comes back empty), `config/business_config.json`,
`config/ird_serving_windows.json`, `config/knowledge_base/uploads/<tenant>/*.txt`,
`data/runtime/model_override.json`.

**Do not carry:** `data/runtime/local_contexts.json` (3.5 MB dev session state),
`data/rag/local_index.json` (rebuilt from the KB tables), `data/ticketing/local_tickets.*`.

**Nothing else to migrate:** `QDRANT_URL` is empty and `KB_READ_FROM_TABLES=true`, so retrieval
reads the KB straight from `new_bot_kb_*` — there is no vector store. Redis is a per-instance
cache. `PG_DATABASE_URL` is a dormant fallback that only applies when `DATABASE_URL` is blank.

---

## F. After the data is in

1. **Edit `new_bot_widget_deployments.allowed_origins`** to your prod domains, or the embed is
   blocked. Check `new_bot_business_config` for any dev URL too.
2. **Restart the app.** Services are cached per hotel_code and are invisible to a running process
   until restart; the KB has a 1-hour cache.
3. **Raise one real ticket end-to-end** and confirm the reference is **not** `LOCAL-*`. That
   prefix means the external ticketing API was never reached and the test proves nothing.
4. **Schedule the log retention job** from §A2.
