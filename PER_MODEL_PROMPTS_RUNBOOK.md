# Per-model prompts — runbook

Goal: the bot uses **4.1 prompts when 4.1 is selected** and **5.4 prompts when 5.4
is selected**, all stored in and served from the DB. 4.1 stays byte-identical to today.

Everything prompt/code-side is already prepared. The steps below are the parts that
need a reachable DB (VPN up) + your go-ahead, because they **write to prod
(`GHN_PROD_BAK`)** or restart the backend.

---

## What's already done (no DB writes, safe, in the working tree)

- `models/database.py` — `PromptRegistry` gains a `model_variant` column + 4-col unique key.
- `services/prompt_registry_service.py` — model-aware loader:
  `get()` resolves **hotel+model → hotel+default → industry+model → industry+default**,
  auto-detecting the active model from the harness override (same signal as `llm/client`).
  Includes a **graceful fallback**: if the column isn't there yet, it serves the
  existing default rows (bot keeps working). Logic verified offline (4 scenarios pass).
- `scripts/export_model_prompts.py` — READ-ONLY export of prompts to files.
- `scripts/migrate_add_model_variant.py` — idempotent schema migration (DB write).
- `scripts/sync_model_variants.py` — seeds the 5.4 variant rows (DB write).
- `prompts/models/README.md`, `prompts/models/gpt-5.4-mini/_TUNING_SPEC.md` — the 5.4 dialect edits.

No production data has been changed. The running backend still executes the OLD code
until it's restarted (step 5).

---

## Wake-up sequence (VPN up + your approval)

### 1. Refresh the baseline from the LIVE DB (READ-ONLY — safe, no approval needed)
```bash
python scripts/export_model_prompts.py
```
Produces: `backups/model_prompts/registry_full_<date>.json` (full restore point) and
refreshes `prompts/models/gpt-4.1-mini/*.md` (4.1 safekeeping). It will NOT overwrite the
already-authored 5.4 files (it skips existing ones).

> The 5.4 set was authored offline from the 2026-06-30 Zone freeze snapshot
> (`scripts/extract_prompts_from_freeze.py`) because the DB was down. That snapshot is
> ~97% identical to the live rows. Running this export confirms the current baseline; if
> the live 4.1 `main_router` / `book_answerer` differ materially from the freeze, re-apply
> the 5.4 operating-contract header (top of each file) onto the fresh baseline before
> seeding. The header is clearly delimited (`=== ⚡ 5.4 ... CONTRACT ===` … `=== END ... ===`).

### 2. Migrate the schema  ← FIRST DB WRITE
```bash
python scripts/migrate_add_model_variant.py --dry-run   # review the plan
python scripts/migrate_add_model_variant.py             # apply
```
Adds `model_variant` + rebuilds the unique key. Idempotent. Existing rows get
`model_variant = NULL` (= the 4.1 default). Nothing else changes.

### 3. Restart the backend so the model-aware loader is live  ← ORDER MATTERS
Kill the uvicorn port owner and start it (no `--reload`). Do this **before seeding**:
the migration added the column (safe for old code, which ignores it), and restarting
now brings up the model-aware loader while **no variant rows exist yet** — so every
model still resolves to the shared/default rows (4.1 behavior for everyone). Seeding
before this restart would let the OLD loader (which does not filter `model_variant`)
occasionally serve a 5.4 row to a 4.1 session.

### 4. Seed the 5.4 variant rows (the authored 5.4 set)  ← DB WRITE
```bash
python scripts/sync_model_variants.py --dry-run          # review (only 2 keys differ)
python scripts/sync_model_variants.py                    # apply
```
Writes exactly 2 rows: `hotel=ZONE, model_variant='gpt-5.4-mini'` for `book_answerer`
and `main_router` (the only keys that differ from 4.1). The other 18 keys get no
variant row — 5.4 falls through to the live 4.1 row. 4.1 rows are never touched. The
now-running model-aware loader routes the 5.4 rows only to 5.4 sessions.

### 5. Verify (harness)
- Select **4.1** → answers identical to today (the real safety proof — 4.1 reads the
  untouched shared rows).
- Select **5.4** → the 5.4 set is active. Run the test transcript in
  `prompts/models/gpt-5.4-mini/_TUNING_SPEC.md` (details/compare/first-hotel/pet-friendly/
  rooftop-pool). Confirm both models resolve prompts fine in `logs/llm_call_metrics.jsonl`.

### 6. Iterate on 5.4 (optional, after it's live and testable)
- Edit only the files in `prompts/models/gpt-5.4-mini/`, re-run
  `python scripts/sync_model_variants.py`, clear the cache (`POST /api/dev/reload-prompts`
  or wait 5 min TTL), re-test. 4.1 is never affected.
- This is where the "slim the bloated prompt" work happens — now safe because you can
  A/B it live against 4.1.

---

## Rollback

- Undo 5.4 behavior: `DELETE FROM new_bot_prompt_registry WHERE model_variant IS NOT NULL;`
  (removes every 5.4 variant row; loader falls back to the 4.1 defaults). Or restore
  from `backups/model_prompts/registry_full_<date>.json`.
- The `model_variant` column can stay — it's inert when no variant rows exist.
- Revert code: the loader change is backward-compatible; with no variant rows and no
  override it behaves exactly as before.

---

## Deferred (NOT in this batch — flagged so it isn't lost)

- **Scope rules for #1-4** (fleet-attribute broaden / compare-union / recall) live in a
  code-appended block shared by both models. If live 5.4 testing still shows those
  failing after the prompt front-loading, the durable fix is to move that specific
  block into a model-variant registry key. Prompt-only; done with testing, not before.
- **`reasoning_effort` / `verbosity`** for gpt-5.x in `llm/client._adapt_kwargs_for_model`
  — a code change (latency + length lever). Deferred per "prompts-only for now".
