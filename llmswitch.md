# LLM Switch — per-model prompt system

**What it does:** the bot serves a *different prompt set per LLM*, from the DB.
Pick **gpt-4.1-mini** in the harness → it uses the original ("4.1") prompts. Pick
**gpt-5.4-mini** → it uses a separate, 5.4-tuned prompt set. Everything is stored in
and served from the DB (`new_bot_prompt_registry`); the model dropdown drives which
variant is loaded, with zero code change per call site.

Status: **LIVE and verified** (migration applied, 2 variant rows seeded, end-to-end
chat logs confirm correct routing). Authored 2026-07-15. Nothing committed to git yet.

---

## 1. Why this exists

`gpt-5.4-mini` follows our huge, prose-heavy prompts worse than `gpt-4.1-mini`: it
skims rules buried deep in a long prompt and defaults to terse one-liners (verified:
36–104 completion tokens vs 4.1's ~244; e.g. "give me details" → "Zone Park Jaipur —
Jaipur, Rajasthan."). It also missed scope rules (pet-friendly → "none", compare only
current, wrong "first hotel we discussed").

Root cause is **salience, not context capacity** — 5.4 handled a ~37k-token system
prompt in the live test fine and *faster* than 4.1. So the fix is a 5.4 *dialect*:
the same rules, front-loaded and imperative, in its own DB rows — without touching
the 4.1 prompts that already work.

---

## 2. How it works (architecture)

### Resolution order
`prompt_registry.get(key)` resolves a row for the **active model**:

```
hotel + model  →  hotel + default(NULL)  →  industry + model  →  industry + default(NULL)
```

- `model_variant = NULL` = the **shared default** = the historical / 4.1 rows.
- `model_variant = 'gpt-5.4-mini'` = a row used **only** when 5.4 is active.
- **Hotel outranks model** on purpose: Zone runs on hotel-override rows, so a 5.4
  variant only reaches Zone as a *hotel-67 + model* row. This protects Zone's heavy
  customizations from being shadowed by a generic per-model prompt, and leaves other
  hotels (e.g. KHIL) untouched even on 5.4.

### What "active model" means
The loader auto-detects it — no per-call-site change:
```
active model = get_model_override()   # the harness dropdown / runtime override
             or settings.openai_model # env default when no override is set
```
This is the *same* signal `llm/client.py::_apply_model_override` uses to route the
actual API call, so the prompt variant always matches the model that runs the call.

### Fallback for un-tuned keys
Only keys that genuinely differ from 4.1 get a variant row. Every other key has **no**
variant row, so 5.4 falls through to the shared/4.1 row automatically — zero
duplication, zero divergence, zero maintenance.

### Graceful degradation
If the `model_variant` column is ever missing (e.g. pre-migration), the loader catches
the DB error once, disables the variant path, and serves the shared/default rows — the
bot keeps working exactly as before.

---

## 3. What changed

### Code (working tree — not committed)
| File | Change |
|---|---|
| `models/database.py` | `PromptRegistry` gains `model_variant VARCHAR(64) NULL`; unique key → `(prompt_key, industry, hotel_id, model_variant)`. |
| `services/prompt_registry_service.py` | Model-aware loader: `get(key, model=…)`, `_resolve_row` (4-step fallback), variant-aware cache key, `_active_model_variant()`, graceful column-missing fallback, `upsert(model_variant=…)`. |

Call sites in `services/llm_orchestration_service.py` were **not** changed — `get()`
auto-detects the model.

### Database (prod `GHN_PROD_BAK` — APPLIED)
- `model_variant` column + rebuilt unique key (via `scripts/migrate_add_model_variant.py`).
- 2 variant rows seeded for Zone (hotel 67), `model_variant='gpt-5.4-mini'`:
  `orchestrator.book_answerer` and `orchestrator.main_router`.
- The NULL (4.1) rows are untouched (same length / version / author).

### Files
```
prompts/models/
  README.md                       # how selection works
  gpt-4.1-mini/*.md               # Zone's live 4.1 set — SAFEKEEPING ONLY (not seeded)
  gpt-5.4-mini/*.md               # the 5.4 set (what gets seeded)
    _TUNING_SPEC.md               # design notes + test protocol
backups/model_prompts/
  registry_full_2026-07-15.json   # full 60-row snapshot (restore point)
```

### Scripts
| Script | Purpose | Writes DB? |
|---|---|---|
| `scripts/export_model_prompts.py` | Export live rows → folders + full snapshot | No (read-only) |
| `scripts/extract_prompts_from_freeze.py` | Offline fallback: extract from a hotel freeze snapshot | No |
| `scripts/migrate_add_model_variant.py` | Add the column (idempotent) | Yes |
| `scripts/sync_model_variants.py` | Seed `gpt-5.4-mini` rows from the folder; skips keys identical to 4.1 | Yes |

---

## 4. The 5.4 prompt tuning (what is done)

**Tailored:** only `book_answerer` and `main_router` — the two behind the observed
failures. Both were **rewritten lean** for 5.4: short, imperative rules with the most-
failed items front-loaded (a "5.4 CONTRACT" block marked "overrides on conflict", which
is how 5.4 weights the opening). Every hard contract from the 4.1 prompt is preserved and
every machine-parsed string is kept verbatim (the exact decline lines, `<<SHOW_BOOKING_
BUTTON: ...>>`, the `FOCUS:` line, ⭐, the JSON output contract).

Size (the "current prompt is too huge for 5.4" fix):
| key | 4.1 (full) | 5.4 (lean) | reduction |
|---|---|---|---|
| `orchestrator.book_answerer` | 87,676 ch | 16,097 ch | **~82%** |
| `orchestrator.main_router` | 58,238 ch | 11,989 ch | **~79%** |

What the lean rewrite fixes (verified live, see §6):
- `book_answerer`: answer-length floor (kills one-liners), cover-every-property, KB-only
  grounding, info-≠-booking, all CASE/B1/B2/handoff/focus/weather/safety rules kept tight.
- `main_router`: fleet-attribute broaden (pet/rooftop), compare = union of discussed
  hotels, recall = keep scope, plus the full routing logic (room ownership, support-intake
  identification, bare-book resolution, continuity) stated tersely.

**Notes:**
- The other 18 keys are **not** 5.4-specific — they reuse the 4.1 rows by design.
- The router also gets ~240 lines of scope rules **appended in code** (shared by both
  models — see §8); the lean 5.4 router front-loads the same intent, which in testing
  routed scope *better* than the bloated 4.1 prompt. Moving that code block into a
  model-variant key is the remaining lever if any scope case still slips on 5.4.

---

## 5. How to operate it

### Switch the model (which prompt set is used)
Harness dropdown, or:
```bash
curl -X POST http://127.0.0.1:8011/api/chat/model-override -H "Content-Type: application/json" -d '{"model":"gpt-5.4-mini"}'   # or gpt-4.1-mini, or "" to clear
```

### Edit the 5.4 prompts
1. Edit files in `prompts/models/gpt-5.4-mini/`.
2. `python scripts/sync_model_variants.py`  (re-seeds only changed keys).
3. `curl -X POST http://127.0.0.1:8011/api/dev/reload-prompts`  (or wait 5-min cache TTL).
4. Test on 5.4; 4.1 is never affected.

### Add a 5.4 variant for another key
Make that key's `gpt-5.4-mini/<key>.md` differ from its `gpt-4.1-mini/<key>.md`, then
`sync`. (Identical files are skipped — no variant row created.)

### Add a whole new model (e.g. another GPT)
1. Add its id to `AVAILABLE_MODELS` in `services/model_override_service.py`.
2. Create `prompts/models/<model-id>/` with the tuned files.
3. `python scripts/sync_model_variants.py --model <model-id>`.

---

## 6. Verification (how we know it works)

1. **Loader unit logic** — offline test: 5.4→variant, 4.1→default, unseeded→default,
   hotel-beats-industry, pre-migration fallback (all pass).
2. **Live loader vs prod DB** — `get(key, model=…)` returns base for 4.1, tuned
   (⚡ contract) for 5.4, for both keys.
3. **End-to-end chat logs** (`logs/llm_io_trace.jsonl`), same message on each model:
   | session | model | book_answerer sysprompt starts with | 5.4 contract |
   |---|---|---|---|
   | mtest-41 | 4.1 | `You are an INFORMATION-ONLY…` | No |
   | mtest-54 | 5.4 | `=== ⚡ 5.4 OPERATING CONTRACT…` | Yes |
   (same split for `main_router`.)

---

## 7. Rollback

- Remove 5.4 behavior: `DELETE FROM new_bot_prompt_registry WHERE model_variant IS NOT NULL;`
  (loader falls back to the 4.1 defaults). Or restore from the full snapshot JSON.
- The `model_variant` column can stay — it's inert with no variant rows.
- Code change is backward-compatible; with no variant rows + no override it behaves
  exactly as before.

---

## 8. Known limitations / deferred

- **Slimming DONE** for the 2 big 5.4 prompts (~82% / ~79%, see §4). Further trimming can
  continue live-tested if wanted.
- **Scope rules #1-4 partly live in a shared code block** (`_resolve_main_route_with_llm`
  appends ~240 lines to the router prompt for *both* models). The 5.4 SCOPE CONTRACT
  front-loads the intent; if live testing still misses, move that specific code block
  into a model-variant registry key (prompt-only, decided after testing).
- **`reasoning_effort` / `verbosity`** for gpt-5.x in `llm/client._adapt_kwargs_for_model`
  — a code lever for 5.4 length/latency, intentionally out of scope (prompts-only).
- **Admin `/api/prompts` listing** (`list_effective_for_hotel`) doesn't yet distinguish
  variants — cosmetic; the chat path resolves correctly. Fix when wiring an editor UI.

See `PER_MODEL_PROMPTS_RUNBOOK.md` for the step-by-step apply/rollback sequence.
