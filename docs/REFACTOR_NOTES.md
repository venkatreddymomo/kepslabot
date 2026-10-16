# Structural refactor — 2026-07-31

Behavior-preserving cleanup of the bot/admin app. **No functionality was
changed.** Every phase was verified against a locked test baseline (207 pass /
26 pre-existing fails) — the same set must stay green, and no new failures may
appear. Nothing was committed to git; all changes are local.

Out of scope (left untouched, by request): the KB scraper
(`Kepsla-hotal-kb-scraper-main/`), the OCR pipeline (`ocrbadfinalfr/`), and all
prompt content / routing logic.

## Verification method

- **Test baseline**: `pytest tests/` → 207 pass / 26 fail. The 26 failures are
  pre-existing (prompt-content and live-LLM/DB-dependent tests that had drifted).
  After every phase the failing set was diffed against the baseline and came
  back **identical** — no regressions.
- **Golden output diffs** (Phase 2): the trace loggers' JSONL output was captured
  on a fixed payload before and after, and compared byte-for-byte (identical).
- **Import smoke tests**: `import main` + the edited modules, after every phase.
- **End-to-end**: fresh server boot + a real chat turn via the widget.

## What changed, phase by phase

### Phase 0 — safety net + git hygiene
- Established the test baseline above.
- Untracked generated junk that had been committed (files kept on disk, **not**
  deleted, changes staged but **not committed**): the OCR `output/` tree (~2,778
  files / 76 MB), `**/graphify-out/cache/`, and a stray `node_modules` file.
  `.gitignore` updated to keep them out. **Tracked files: 3,811 → 693 (-82%).**

### Phase 1 — `main.py` slimmed 1,732 → 260 lines
Pure relocation into focused modules (no logic changed):
- `bootstrap/console_logging.py` — Windows WMI-hang fix, safe stdout/stderr,
  console logging config, API log line.
- `bootstrap/scraper_proxy.py` — KB-scraper subprocess mgmt + reverse proxy.
- `bootstrap/dev_processes.py` — admin_ui build + Vite dev server.
- `bootstrap/lifespan.py` — startup/shutdown handler (DB init, prewarm, seeding).
- `api/middleware/gateway.py` — gateway middleware + global exception handler.

Middleware registration order (behavior-critical, Starlette is LIFO) was
preserved exactly: `WidgetOrigin → CORS → gateway`.

### Phase 2 — logging de-duplication
- `services/log_common.py` — shared `sanitize` / `truncate_text` / `append_jsonl`
  / `now_iso` / `clean`. The ~50-line sanitize+append block that was copy-pasted
  across `backend_trace_service`, `everything_trace_service`, and
  `step_trace_service` now lives once, parameterized by the per-service caps.
- Output verified byte-identical via golden diff. The other loggers were left
  alone — each is single-responsibility and lower-duplication.

### Phase 3 — shared helpers (narrow, safe subset)
- `core/text_utils.py` — canonical `normalize_identifier`.
- The 5 **byte-identical** copies of `_normalize_identifier` now delegate to it
  (call sites unchanged).
- **Deliberately left alone**: `_normalize_phase_identifier` (7 distinct
  implementations across 9 copies), `_first_non_empty` (3 distinct, one is a
  classmethod using `cls._safe_str`), and the 2 divergent `_normalize_identifier`
  variants. These "duplicates" had drifted into genuinely different behavior —
  merging them would change functionality, so they stay.

### Phase 4 — god-function decomposition (in progress + roadmap)
Extractions completed so far (each an early-return handler with a narrow in/out
surface; each verified: import + AST + full suite identical + live chat turn):

| Extracted method | Out of | Lines moved | Live-verified |
|---|---|---|---|
| `_maybe_serve_chip_click_fast_path` | `orchestrate_turn` (2,921→2,865) | 58 | chip fast-path |
| `_maybe_identity_shortcut_response` | `_process_message_with_scope` (2,037→1,862) | 63 | identity turn → `response_source: identity_shortcut` |
| `_maybe_handle_parked_task_command` | `_process_message_with_scope` | ~130 | resume/cancel handler |

`_process_message_with_scope`: **2,037 → 1,862 lines** so far.

**File-size reduction via mixin splits** (the big lever — whole cohesive method
clusters moved out of `chat_service.py` into base classes it inherits, verbatim,
no logic change; the mixin modules never import chat_service, so no cycle):

| Mixin file | Cluster | Methods | Verified |
|---|---|---|---|
| `services/chat_service_ticketing_mixin.py` | ticketing + 2 constants | 44 | tests + 70 ticketing tests + live complaint → Maintenance |
| `services/chat_service_booking_mixin.py` | room/spa/transport booking | 27 | tests + 13 booking tests |

**`chat_service.py`: 18,535 → 13,967 lines (−4,568, −25%).** More clusters remain
(phase ~1,225, orders ~708, forms ~390, menu ~329 — each verified separable),
and the two mega-methods (`_process_message_with_scope` 1,862,
`_process_llm_orchestration_message` ~1,000) need in-place decomposition to get
the file fully under 10k. `llm_orchestration_service.py` (15,413) has a chip
cluster (~2,374 lines) that is separable once its shared `_log_llm_call` helper
is lifted to a small shared module.

- **Roadmap for the rest** (needs a dedicated, incremental effort against the
  golden harness — not a rushed pass, since these are the live chat pipeline):

  | Function | File | Lines | Approach |
  |---|---|---|---|
  | `orchestrate_turn` | llm_orchestration_service | ~2,865 | Extract remaining top guard blocks (resume-prompt, router short-circuits) one at a time; then split the routing core into `_route`, `_ground`, `_answer`, `_post_process` steps. |
  | `_process_message_with_scope` | chat_service | ~2,040 | Same guard-block-first strategy. |
  | `_process_llm_orchestration_message` | chat_service | ~1,001 | Extract the pre/post-orchestration blocks. |
  | `_resolve_main_route_with_llm` | llm_orchestration_service | ~1,027 | Separate prompt-build / LLM-call / result-parse. |
  | `_run_service_agent` | llm_orchestration_service | ~982 | Separate prompt-build / call / parse. |

  Method for each extraction: identify the block's read-set (params) and
  write-set (returns), move it verbatim, re-run tests + golden transcript, and
  revert on any diff.

### Phase 6 — god-files under 10k via mixin splits (2026-07-31)
Every cohesive method cluster was moved verbatim into a mixin the class inherits.
Two shared modules hold names used by both the base class and the mixins so the
mixins never import the base module (no cycle):
`services/chat_service_shared.py` (constants + `CapabilityCheck`) and
`services/llm_call_logging.py` (`_log_llm_call`).

| God-file | Before | After | Mixins |
|---|---|---|---|
| `chat_service.py` | 18,535 | 9,617 | ticketing, booking, phase, ordermenu, response |
| `llm_orchestration_service.py` | 15,413 | 9,734 | chips, grounding, directives |
| `config_service.py` | 10,326 | 9,429 | normalize |

**No first-party file exceeds 10k lines anymore.**

Gotcha found + fixed: a handful of moved **staticmethods** called sibling
staticmethods by class name (`ChatService._foo(...)`), which fails in the mixin
module. Fixed by exposing the class name in each mixin module after the class is
defined (`for _m in (...): _m.ChatService = ChatService`) — a runtime injection
that avoids the circular import. A whole-repo static pass now confirms every
global reference in every mixin resolves. Unused imports (278, mostly from the
copied import blocks) were removed with `ruff --select F401 --fix`.

Verified: full suite identical to baseline after every split; live chat turns
across all mixin paths (info, food-order, ticketing, multi-property) and all
admin `/admin/api/config/*` endpoints return correct data.

### Phase 5 — tooling (no mass reformat)
- `pyproject.toml` — ruff + black + pytest config, scoped to first-party code
  (vendored sub-apps excluded). Conservative ruff rule set so adoption doesn't
  bury the team in pre-existing findings. **No repo-wide reformat was run** —
  that would be a huge, review-hostile diff.
- `scripts/check_file_length.py` + `scripts/file_length_baseline.json` — a
  **ratchet**: existing large files are frozen at their current size (may shrink,
  may not grow); new first-party files must be < 800 lines.
- `.pre-commit-config.yaml` — opt-in (`pre-commit install`); runs ruff + the
  ratchet.

## Handy commands

```bash
pytest tests/                              # 207 pass / 26 pre-existing fail
python scripts/check_file_length.py        # ratchet gate (CI)
python scripts/check_file_length.py --update-baseline   # after an intentional split
pip install ruff black pre-commit && pre-commit install # enable local gate
ruff check .                               # lint first-party code
```

## Git state note
The artifact-untracking is **staged but not committed** (3,118 staged deletions).
To undo without losing files: `git reset` (files remain on disk). To keep the
cleanup, commit at your discretion.
