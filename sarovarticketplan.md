# Sarovar (SPR) — Ticketing Config Plan

Built on the **SPR Onboarding Guide v3.12** structure (its 6 activities), instantiated with
**our** real ids, verified live against the DB on **2026-07-17**.

> The guide is a **process reference**. Its worked-example ids are from another environment and
> do **not** resolve in our `GHN_PROD_BAK` / `GHN_FF_PROD` — substitute the values in §1 below.
> Companion docs: [`configticket.md`](configticket.md) (mechanism + Zone patterns) ·
> [`sarovarplan.md`](sarovarplan.md) (client sheet, staff, escalation, SLA).

---

## 1. Key substitutions — guide's placeholder → our real value

| Guide says | Our actual value | Note |
|---|---|---|
| Org / `entity_id` = **10477** | **RI 7408** (`Sarovar Portico, Sriperumbudur`) | 10477 does not exist here |
| FMS entity | **6311** | FMS↔RI `6311 ↔ 7408` ✅ already fixed |
| Reception Front Office = 1 | **5** = Front Office | id 1 here = *Banquets* |
| Housekeeping = 3 | **8** = Housekeeping | id 3 here = *Beauty Salon* |
| Maintenance = 29 | **41** = Engineering | id 29 here = *Book a venue*; 38 = Maintenance |
| Food and Beverages = 39 | **34** = `" Food & Beverage"` | id 39 here = *IT*. **Leading space in the name** |
| Guest Relations (confirm) | **42** = Administration (for the GM) | 16 = Guest Relations if ever needed |
| Notif template source FMS **6269** | **6290** (Zone Kolkata, ENTITY 7256) | 6269 has 0 notification rows here |
| Feature set = 5 ids (114,135,117,119,108) | **clone all 43** from Venky **6422** | 135 isn't even on Venky |
| `PASSWORD` defaults to `'manager'` | must be a **bcrypt `$2a$` hash** | plain text ⇒ "Bad credentials" |
| (not mentioned) | **`GHN_USER_ROLE_MAPPING`** ROLE_ID=**3** required | guide omits this table |
| Escalation `entity_id` = org (RI) | **`entity_id` = the FMS id (6311)** | ⚠️ see §5 — guide has this backwards |

**Sarovar departments in use (5):** `5` Front Office · `8` Housekeeping · `34` Food & Beverage ·
`41` Engineering · `42` Administration.

---

## 2. Status at a glance

| # | Activity (guide's order) | Table | Status |
|---|---|---|---|
| 0 | FMS↔RI mapping | `GHN_SURVEY_FMS_RI_ENTITY_MAPPING` | ✅ `6311 ↔ 7408` |
| 1 | Users / agents | `GHN_USER` | ⏳ **0 of 9 real staff exist** |
| 2 | Organization mapping | `GHN_USER_ORGANIZATION_MAPPING` | 🔧 testers only |
| 3 | Feature / menu access | `GHN_ROLE_FEATURE` | 🔧 testers only (43 rows each) |
| 4 | Users → departments | `DEPARTMENT_STAFF_CONFIG` | 🔧 15 rows, **testers as HODs** |
| 5 | Escalation ladder | `GHN_DEPARTMENT_ALERT_MAPPINGS` | 🔧 5 rows, **flat ladder + no SLA** |
| 6 | Notification templates | `GHN_FF_TICKET_NOTIFICATIONS` | ✅ 10 rows @ 7408/6311 |
| — | Bot `.env` (entity map + allowlist) | `.env` | ✅ written · ⏳ **needs restart** |

**Net:** the pipe is wired end-to-end and works — but it routes to **us** (Sana/Venky/Suhas), not
to the hotel. Steps 1–5 are about handing it over to the real staff.

---

## 3. What's already done (verified live 2026-07-17)

- **`DEPARTMENT_STAFF_CONFIG`** on ORG_ID 7408 — 15 rows: depts 5/8/34/41/42 × staff
  6422 (Venky) / 6424 (Sana) / 6425 (Suhas), **all `MANAGER=1`**, `STATUS=1`, `TICKET_PRIORITY=HIGH`.
  - ⚠️ Plus one **stray legacy row**: dept `1` Banquets, staff `6394` — inherited from the reused
    Malleshwaram org. Harmless but should be removed (backup first).
- **`GHN_DEPARTMENT_ALERT_MAPPINGS`** — 5 rows on `entity_id=6311` (depts 5/8/34/41/42),
  `ticket_auto_assign=1`, `is_active=1`, `escalation_interval=5`, `before_sla_default=70`, `group_id=0`.
- **`GHN_FF_TICKET_NOTIFICATIONS`** — 10 rows `ENTITY_ID=7408 / FMS_ENTITY_ID=6311`, cloned from Zone
  Kolkata. Phases: created / assigned / sla_breach / reminder_beforesla / resolved / cancelled ×
  Dept Head / staff / guest. Text is verbatim Zone — **review for Zone-specific wording**.
- **Role + features** already global on 6422/6424/6425 (ROLE_ID 3, 43 features) — untouched.

---

## 4. ⚠️ Two defects found while planning

### 4.1 SLA thresholds are NULL on our escalation rows
Our `entity_id=6311` rows have `sla_data = NULL` **and** `before_sla_data = NULL`.
Zone Kolkata (the pattern we cloned from) has both populated:

```
sla_data        = {"low": 30, "high": 20, "medium": 25, "critical": 10}
before_sla_data = {"low": 75, "high": 25, "medium": 50, "critical": 10}
```

We cloned the **notification templates** for `sla_breach` and `reminder_beforesla`, but never gave
the engine the thresholds that fire them → **those two phases likely never trigger.** The guide
flags `sla_data` as a required column (its §8) — this is the one place following it more literally
would have saved us. Fix = populate both JSONs per §8 of `sarovarplan.md` (High 5min / Med 10min / GM 15min).

*(`sla_duration_minutes` is NULL on Zone too — leave it; the guide's "10" doesn't match live practice.)*

### 4.2 Bot still emits departments that have no manager
Sarovar ticket history by department:

| Department on ticket | n | Has a manager on 7408? |
|---|---:|---|
| Reception Front Office | 20 | ❌ (dept 18 — not configured) |
| Guest Relations | 20 | ❌ (dept 16 — not configured) |
| Housekeeping | 15 | ✅ dept 8 |
| Front Office | 12 | ✅ dept 5 |
| Engineering | 10 | ✅ dept 41 |
| In-Room Dining | 5 | ❌ (dept 15 — not configured) |
| Food & Beverage | 3 | ✅ dept 34 |

~45 tickets went to departments with **no manager → no auto-assign → invisible on the dashboard**.
The `.env` `TICKETING_ALLOWED_DEPARTMENTS_JSON` allowlist forces the bot onto the correct 5 — **but
it only takes effect after the restart.** This makes the restart the single highest-value action.

---

## 5. The plan — remaining work, in dependency order

### Step 0 — Restart the bot ⏳ *(do this first; unblocks §4.2)*
Loads the `.env` entity map (`"sriperumbudur": "6311"`) + the 5-department allowlist.
Then raise one test ticket and confirm `organisation_id=6311` and the dept is one of the 5.
Hard restart (no `--reload`) — kill port owners, not just named processes.

### Step 1 — Create the 9 real staff (`GHN_USER`)
All 9 confirmed **NOT FOUND** — every one needs creating. Clone-insert per guide §4.2.1, **but with
our corrections**: bcrypt `PASSWORD`, `ROLE='Admin'`, `HASH_TAG=MD5(RAND())`, template = an existing
Zone/Sarovar admin.

| Agent | USER_NAME (email) | Mobile | Dept | Role | Hours |
|---|---|---|---|---|---|
| Duty Manager | dm@SarovarPorticoSPR.com | 918925864054 | Front Office | desk | 24 h |
| Hemant Acharya | fom@SarovarPorticoSPR.com | 918925864052 | Front Office | **HOD** | 09–22 |
| Shift Engineer | eng.desk@SarovarPorticoSPR.com | 918925864057 | Engineering | desk | 24 h |
| Navanith Singh | ce@SarovarPorticoSPR.com | 918925864055 | Engineering | **HOD** | 09–18 |
| HK Shift Incharge | hkdesk@SarovarPorticoSPR.com | 918925864068 | Housekeeping | desk | 24 h |
| SwarnaKumari | ehk@SarovarPorticoSPR.com | 918925864069 | Housekeeping | **HOD** | 09–18 |
| Riya | fnb@SarovarPorticoSPR.com | 918925864065 | F&B | staff | 06–23 |
| Ektej | fnb@SarovarPorticoSPR.com | 918925864063 | F&B | **HOD** | 09–23 |
| Amit Choudhury | gmspspr@sarovarhotels.com | 918925864059 | Administration | **GM** | 09–18 |

👤 **Blocker — `fnb@` is shared by Riya AND Ektej.** `USER_NAME` is the login and each needs their own
mobile for WhatsApp. Either get distinct emails, or create **one** F&B account (Ektej, the HOD) and
drop Riya. **Ask the client before running Step 1.**

👤 GM name mismatch to confirm: guide's register shows `gmspspr@` under *"Richard Rajesh"*, our sheet says *Amit Choudhury*.

Then: **capture every new ID into a register** — everything downstream references users by id only.

### Step 2 — Role + features + org visibility (per new user)
1. `GHN_USER_ROLE_MAPPING` → `ROLE_ID=3` (guide omits this — **required**).
2. `GHN_ROLE_FEATURE` → clone all **43** rows from Venky 6422 (not the guide's 5).
3. `GHN_USER_ORGANIZATION_MAPPING` → one row, `ORGANIZATION_ID=7408`.
4. (optional) `GHN_USER_ORGANIZATION_DEPARTMENT_MAPPING` placeholder `(uid, 0, 0)` to match existing staff.

### Step 3 — Rebuild `DEPARTMENT_STAFF_CONFIG` on 7408
**Backup the 15 existing rows first.** Then one row per user×dept, real shift hours,
`MANAGER=1` on the HOD only, `MANAGER=0` on the 24 h desks:

| Dept | `MANAGER=1` (HOD) | `MANAGER=0` (desk) | Desk hours |
|---|---|---|---|
| 5 Front Office | Hemant | Duty Manager | 00:00:00–23:59:59 |
| 41 Engineering | Navanith | Shift Engineer | 00:00:00–23:59:59 |
| 8 Housekeeping | SwarnaKumari | HK Shift Incharge | 00:00:00–23:59:59 |
| 34 Food & Beverage | Ektej | (Riya — pending §5 Step 1 blocker) | 06:00–23:00 |
| 42 Administration | Amit (GM) | — | 09:00–18:00 |

Also: drop the stray dept-1 Banquets / staff 6394 row (backup first).
👤 **Decision:** keep Sana/Venky/Suhas as a `MANAGER=0` fallback tier during UAT so nothing goes
unanswered — **recommended** — or remove them entirely at go-live.

### Step 4 — Rewrite the escalation ladder (`entity_id = 6311`, **FMS not RI**)
Today all three levels are the identical array `[6424, 6422, 6425]` — no real ladder.
Target, per `sarovarplan.md` §7/§9 (**Server → HOD → GM**), cumulative like Zone:

| Dept | `escalation_level_1` | `escalation_level_2` | `escalation_level_3` |
|---|---|---|---|
| 5 Front Office | `[dm]` | `[dm, hemant]` | `[dm, hemant, amit]` |
| 41 Engineering | `[eng]` | `[eng, navanith]` | `[eng, navanith, amit]` |
| 8 Housekeeping | `[hk]` | `[hk, swarna]` | `[hk, swarna, amit]` |
| 34 F&B | `[ektej]` | `[ektej, amit]` | `[ektej, amit]` |
| 42 Administration | `[amit]` | `[amit]` | `[amit]` |

Also on the same rows: populate **`sla_data`** + **`before_sla_data`** (fixes §4.1), keep
`escalation_interval=5`, `before_sla_default=70`, `ticket_auto_assign=1`, `is_active=1`, `group_id=0`.
Keep the int cols `escalation_1/2/3` populated (Zone does, despite the guide saying leave NULL).
**Backup the 5 rows before UPDATE.**

### Step 5 — Notification templates
Already ✅ (10 rows). Remaining: scrub Zone-specific wording from `TEMPLATE_BODY` / `MESSAGE_TEXT` /
`EMAIL_*`. Guide's §9.3 post-copy tweak pattern applies. Also 👤 `TICKETING_WIDGET_GROUP_MAP_JSON`
was never added for Sarovar (guest name on ticket) — needs Sarovar's `widget_id`.

### Step 6 — Validate (guide §10)
- Escalation coverage check returns **empty** — every dept in `DEPARTMENT_STAFF_CONFIG` (ORG 7408) has
  a row in `GHN_DEPARTMENT_ALERT_MAPPINGS` (`entity_id=6311`). ⚠️ Note the join is **RI on one side,
  FMS on the other** — the guide's query compares org-to-org and won't work as written here.
- Exactly **one** `MANAGER=1` per department.
- All mobiles E.164 digits only (`91` + 10, no `+`/spaces).
- One test ticket per department → confirm it **auto-assigns** (`assigned_id != 0`) and the alert fires.
  Auto-assign runs on a **delayed background job** — not instant, even on Zone.

---

## 6. Open decisions (👤 client / Sana)

- [ ] **`fnb@` shared by Riya + Ektej** — distinct emails, or one HOD account? *(blocks Step 1)*
- [ ] **GM identity** — Amit Choudhury vs "Richard Rajesh"?
- [ ] **Spa** and **Meeting/Banquet** ticket ownership — no dedicated dept in the sheet.
      Candidates: `12` Spa · `36` Spa / Wellness · `1` Banquets · `10` Restaurant · `15` In-Room Dining.
      Default until told otherwise: Spa → Front Office, Banquet → F&B.
- [ ] **Keep Sana/Venky/Suhas as fallback tier** during UAT? *(recommended)*
- [ ] **Confirm SLA thresholds** for §4.1 — sheet says HOD 5–10 min, GM 15 min, "All Case".

---

## 7. Rollback

Every step is reversible by id / entity:
- Steps 1–2: `DELETE` the new user ids from `GHN_USER`, `GHN_USER_ROLE_MAPPING`, `GHN_ROLE_FEATURE`,
  `GHN_USER_ORGANIZATION_MAPPING`, `GHN_USER_ORGANIZATION_DEPARTMENT_MAPPING`.
- Step 3: restore the `DEPARTMENT_STAFF_CONFIG` backup `WHERE ORG_ID = 7408`.
- Step 4: restore the `GHN_DEPARTMENT_ALERT_MAPPINGS` backup `WHERE entity_id = 6311`.
- Step 5: `DELETE ... WHERE FMS_ENTITY_ID = 6311`.

**Backup before every DELETE/UPDATE** (guide §2, and our own rule) — dated backup table or JSON export.
