# Per-model prompt sets

The bot reads its prompts from the DB (`new_bot_prompt_registry`). This folder is
the **git-versioned mirror + editing source** for the per-model prompt system.

## How model → prompt selection works (in code)

`prompt_registry.get(key)` resolves a row for the **active model** in this order:

```
hotel + model  →  hotel + default(NULL)  →  industry + model  →  industry + default(NULL)
```

- The **active model** is whatever the harness dropdown selects (the runtime
  model override), or the env default when no override is set — the exact same
  signal `llm/client.py` uses to route the call. So: **pick 4.1 in the harness →
  4.1 prompts; pick 5.4 → 5.4 prompts.** No code change per call site — the
  loader detects it.
- `model_variant = NULL` rows are the **shared default** = today's 4.1 set.
- A `model_variant = 'gpt-5.4-mini'` row applies **only** when 5.4 is active.
- Hotel rows outrank industry rows, so Zone's customizations are never shadowed
  by a generic per-model prompt.

## Folders

| folder | what it is | seeded to DB? |
|---|---|---|
| `gpt-4.1-mini/` | Exact export of Zone's **current live** rows (the known-good 4.1 set). | **No** — pure safekeeping. 4.1 keeps reading the existing NULL/hotel rows untouched. |
| `gpt-5.4-mini/` | The 5.4 variant set. Starts as baseline copies of 4.1, then tuned. | **Yes** — seeded as `hotel=ZONE, model_variant='gpt-5.4-mini'` rows via `scripts/sync_model_variants.py`. |

`gpt-4.1-mini/` and the 5.4 baseline are produced by `scripts/export_model_prompts.py`
(read-only) once the DB/VPN is reachable. See `../../PER_MODEL_PROMPTS_RUNBOOK.md`.

## The guarantee for 4.1

4.1's rows are never edited or moved — only **read-copied** (export) and
**shadowed by a new sibling row** (the 5.4 variant). Select 4.1 → the loader finds
no 4.1 variant → falls through to the exact row 4.1 uses today. Structural, not
hopeful. Verified offline in the loader logic test.
