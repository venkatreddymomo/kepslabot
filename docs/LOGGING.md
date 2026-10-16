# Logging Guide — where to look when something breaks

This bot logs to **two places at once**:

1. **Files** in `logs/*.jsonl` / `logs/*.log` — full detail, but **local to the machine** and auto-deleted after **7 days**. Useless for the deployed server (its filesystem is wiped).
2. **Database tables** (`new_bot_log_*` in MySQL `GHN_PROD_BAK`) — the same logs mirrored into the DB so you can query them **from anywhere, local or deployed**, also kept **7 days**.

> When you hit an error, query the **DB tables** below. Every row is tagged with
> `source_env` (`local` vs `deployed`), `session_id`, and `trace_id`, so you can
> pull the *complete* story of one chat — across both environments — in one query.

---

## Quick "what broke? → which table" map

| Symptom | Look here first | Then |
|---|---|---|
| **Any crash / exception / 500** | `new_bot_log_errors` | the `origin_stream` tells you which flow; join `trace_id` to `new_bot_log_backend` |
| Bot gave a **wrong / weird answer** | `new_bot_log_messages` (what it said) | `new_bot_log_llm` (exact prompt+response) + `new_bot_log_routing` (how it got there) |
| **Wrong suggestion chips** shown | `new_bot_log_chips` | `new_bot_log_messages` for the turn context |
| Message went to the **wrong flow / service** | `new_bot_log_routing` | `new_bot_log_backend` for the full step path |
| **Ticket not created / wrong** | `new_bot_log_tickets` | `new_bot_log_errors` (same session_id) |
| **Slow** response | `new_bot_log_backend` (`duration_ms`) | `new_bot_log_llm` (`latency_ms`) |
| **LLM cost / token** questions | `new_bot_log_llm_daily` (rollup) | `new_bot_log_llm` (per call) |
| "What exactly happened in this chat?" | `new_bot_log_backend` filtered by `session_id` ordered by `ts` | everything else by `session_id` |

---

## Shared columns (on every `new_bot_log_*` table)

These let you correlate and filter no matter which table you're in:

| Column | Meaning |
|---|---|
| `ts` | when the event happened (UTC) |
| `source_env` | **`local`** (a dev machine) or **`deployed`** (the server) — filter on this to separate environments, or omit it to see both |
| `session_id` | the chat session — **the main key to reconstruct one conversation** |
| `trace_id` / `turn_trace_id` | per-request / per-turn correlation IDs (join across tables) |
| `hotel_code` | which tenant/property |
| `level` | `info` / `debug` / `error` — anything `error` is **also** copied into `new_bot_log_errors` |
| `component` / `event` | where in the code / what happened |
| `stream` | the original file logger this came from |
| `message` | human-readable line (when applicable) |
| `payload` | the full structured record (JSON) — everything, untrimmed |

---

## The tables, in detail

### 1. `new_bot_log_messages` — the conversation
**What:** one row per chat turn: the user's message and the bot's reply, with phase/service/intent.
**Check it when:** you want to see *what was actually said*.
**Key columns:** `user_text`, `bot_text`, `phase`, `service_id`, `intent`.
**Fed by:** `conversation_audit` logger (`logs/conversation_audit.jsonl`).

### 2. `new_bot_log_chips` — suggestion chips
**What:** which chips were shown to the guest, the source that produced them, and (when known) whether they were KB-verified.
**Check it when:** the chips are wrong, irrelevant, or missing.
**Key columns:** `chips_shown` (JSON array), `chosen_chip`, `chip_source`, `kb_verified`, `phase`, `service_id`.
**Fed by:** chip finalization in the chat turn (`api/routes/chat.py`).

### 3. `new_bot_log_routing` — how the message was routed
**What:** every routing decision — which route/topic/service the turn was sent to and why.
**Check it when:** the bot answered as the wrong service, or a booking verb went to the wrong place.
**Key columns:** `route`, `from_topic`, `to_topic`, `decision`, `service_id`, `phase`, `reason`.
**Fed by:** `book_routing` logger (`logs/book_routing.log`) + orchestration router.

### 4. `new_bot_log_backend` — the full backend path (deep trace)
**What:** the firehose — every internal step, stage, endpoint, timing. This is the "what happened, step by step" timeline.
**Check it when:** you need the complete execution path of a request, or to find *where* a turn went wrong/slow.
**Key columns:** `step`, `stage`, `endpoint`, `method`, `status_code`, `duration_ms`, plus full `payload`.
**Fed by:** `step_trace`, `backend_trace`, `everything_trace`, `turn_diagnostics`, `chat_step_timing`, `observability`, `processing_debug`. (This is the biggest table.)

### 5. `new_bot_log_llm` — every AI call (with cost)
**What:** one row per LLM call: model, tokens, **computed cost (USD)**, latency, and the **full untrimmed prompt and response**.
**Check it when:** the AI output was wrong (read the exact `input_text`/`output_text`), or for token/cost analysis.
**Key columns:** `model`, `call_site`, `input_tokens`, `output_tokens`, `total_tokens`, `input_cost_usd`, `output_cost_usd`, `total_cost_usd`, `latency_ms`, `input_text`, `output_text`.
**Fed by:** the central LLM client (`llm/client.py`) — covers chat, JSON chat, raw completions, embeddings.
**Note:** the KB scraper (`Kepsla-hotal-kb-scraper-main/...`) uses its own OpenAI client and is **not** tracked here yet.

### 6. `new_bot_log_tickets` — ticketing pipeline
**What:** ticket create attempts, payloads, and status transitions.
**Check it when:** a support/booking ticket didn't get created or has wrong data.
**Key columns:** `ticket_ref`, `service_id`, `status`, `stage`, full `payload`.
**Fed by:** `ticketing_service` debug logger (`logs/ticketing_debug.jsonl`).

### 7. `new_bot_log_errors` — every error in one place
**What:** every `error`-level event from *any* of the above streams, copied here so failures are searchable in one table.
**Check it when:** anything failed — **start here**.
**Key columns:** `origin_stream` (which flow it came from), `error_type`, `error_message`, `stack`, full `payload`.

### 8. `new_bot_log_llm_daily` — cost rollup
**What:** daily aggregate of LLM usage for fast dashboards.
**Check it when:** "what did we spend yesterday / this week, by model / hotel?"
**Key columns:** `day`, `source_env`, `hotel_code`, `model`, `calls`, `input_tokens`, `output_tokens`, `total_tokens`, `total_cost_usd`. Refreshed automatically (startup + every 6h).

---

## Handy queries

**Reconstruct one whole chat (across all environments):**
```sql
SELECT ts, source_env, level, component, event, message
FROM new_bot_log_backend
WHERE session_id = 'THE_SESSION_ID'
ORDER BY ts;
```

**See the user/bot turns of a session:**
```sql
SELECT ts, user_text, bot_text, phase, intent
FROM new_bot_log_messages WHERE session_id = 'THE_SESSION_ID' ORDER BY ts;
```

**Recent errors (last hour) and where they came from:**
```sql
SELECT ts, source_env, hotel_code, origin_stream, error_type, error_message
FROM new_bot_log_errors WHERE ts >= NOW() - INTERVAL 1 HOUR ORDER BY ts DESC;
```

**The exact prompt/response for a bad answer:**
```sql
SELECT ts, model, total_cost_usd, input_text, output_text
FROM new_bot_log_llm WHERE session_id = 'THE_SESSION_ID' ORDER BY ts;
```

**Cost this week by model:**
```sql
SELECT model, SUM(calls) calls, SUM(total_tokens) tokens, ROUND(SUM(total_cost_usd),4) usd
FROM new_bot_log_llm_daily WHERE day >= CURDATE() - INTERVAL 7 DAY
GROUP BY model ORDER BY usd DESC;
```

**Only deployed-server logs (ignore local dev):**
```sql
... WHERE source_env = 'deployed' ...
```

---

## Quick health check

After the backend has been running and a few chats happened, run (on a machine with DB/VPN access):
```
python tools/check_db_logging.py        # last 30 min
python tools/check_db_logging.py 120    # last 120 min
```
It prints per-table counts, the local-vs-deployed split, a sample row per table, and the cost rollup.

---

## Settings & retention (`config/settings.py`)

| Setting | Default | Purpose |
|---|---|---|
| `db_logging_enabled` | `True` | master on/off for DB logging |
| `db_logging_streams` | `[]` (all) | allowlist; empty means mirror every stream |
| `db_logging_source_env` | `auto` | `local`/`deployed`/`auto` (auto reads env vars / falls back to `local`) |
| `db_logging_retention_days` | `7` | DB rows older than this are pruned (matches file retention) |
| `db_logging_store_raw_llm_io` | `True` | store full prompt/response text in `new_bot_log_llm` |
| `db_logging_cost_rollup_enabled` | `True` | maintain `new_bot_log_llm_daily` |

Writing is **best-effort and asynchronous** (a background batched writer): it never blocks or breaks a chat, and under extreme load it drops log rows rather than slowing the bot. Files always keep their own copy.
