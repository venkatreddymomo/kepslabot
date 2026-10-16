# Suggestion chips logic

This file captures the current suggestion-chip implementation so it can be rebuilt after reverting the repo.

This is separate from `multiptorpty logic.md`, but the two systems are linked. Suggestion chips reuse the same active-property and sticky-service state, and the prefetch/cache-hit path must preserve those fields or the chip flow breaks on the next turn.

## Goal

The current chip system is built around four rules:

- live answers must stay non-blocking
- visible chips should come from one canonical runtime slot
- chips must be KB-grounded and filtered hard
- chip clicks must preserve the same service/property/phase scope as the live turn

If you only restore chip generation and miss prefetch hydration or sticky-state replay, the first chip render may work but the next turn will drift.

## 1. What is the real source of truth

There are multiple chip-related fields, but they are not all equal.

### 1.1 Live response object

`OrchestrationDecision.suggested_actions` exists, but the live `/messages` response does **not** always wait for it.

- service-agent turns may already carry inline `suggested_actions`
- book-answerer live turns intentionally discard inline chips
- `_ensure_suggested_actions()` does not synchronously generate chips on the request path

So `ChatResponse.suggested_actions` is not the canonical chip source for the frontend.

### 1.2 Canonical runtime slot

The main user-visible chip source is:

- `LLMOrchestrationService._post_response_chip_jobs[session_id]["chips"]`

That slot is written by:

- `schedule_post_response_chip_job()`
- `publish_post_response_chips()` on cache/speculation hits

And read by:

- `await_post_response_chips()`
- `/api/chat/suggestions`

### 1.3 Separate caches that also matter

Do not confuse these:

- post-response chip job cache
  - holds chips for `/suggestions`
- suggestion prefetch cache
  - holds fully generated speculative answers for clicked chips
- chip speculation cache inside `LLMOrchestrationService`
  - holds speculative `OrchestrationDecision` objects for instant chip-click turn reuse

All three are active in the current implementation.

## 2. Core quality filters

Two layers exist.

### 2.1 API-side sanitize/filter helpers

File: `api/routes/chat.py`

Helpers:

- `_sanitize_suggestions(raw, limit=4)`
- `_filter_chip_quality(raw, limit=3)`
- `_chip_is_informational_positive(text)`

Behavior:

- trim and collapse whitespace
- drop empty labels
- drop labels longer than 80 chars
- require 2 to 12 words
- dedupe case-insensitively
- filter out chips containing banned phrases
- cap final result

Current banned phrases:

- `availability`
- `rates`
- `rate`
- `price`
- `cost`
- `breakfast`
- `photo`
- `photos`
- `picture`
- `pictures`
- `image`
- `images`
- `see room`
- `show me photo`

Important: booking/reserve verbs are intentionally **not** blocked here. The generator prompt is expected to only propose those when the active service/phase allows them.

### 2.2 Orchestration-side canonical filter

File: `services/llm_orchestration_service.py`

Helpers:

- `_normalize_suggested_actions()`
- `_filter_chip_quality()`

Behavior:

- normalize/dedupe chips
- truncate each label to 72 chars
- apply the same banned-phrase belt-and-suspenders filter before publishing into the post-response slot

This is why `/api/chat/suggestions` does **not** re-filter ready chips from `await_post_response_chips()`; it treats that slot as already canonical.

## 3. Live turn behavior

### 3.1 `_ensure_suggested_actions()` is intentionally non-blocking

File: `services/llm_orchestration_service.py`

`_ensure_suggested_actions()` only does this:

- if `decision.suggested_actions` already exists, keep it
- if chip feature is disabled, set `decision.metadata["suggested_actions_source"] = "chat_service_fallback"`
- otherwise set `decision.metadata["suggested_actions_source"] = "post_response_chip_job"`

It does **not** call the chip generator inline. This is deliberate.

### 3.2 Book-answerer live path

File: `services/llm_orchestration_service.py`, book-route short-circuit

Current live behavior:

1. `_run_book_answerer_llm()` returns `(answer_text, _ignored_inline_chips, focus_ids)`
2. `_persist_book_answerer_focus()` writes the next-turn property scope
3. book decision is created with:
   - `suggested_actions=[]`
   - `metadata["suggested_actions_source"] = "post_response_generator"`
4. any inline chips the book-answerer may have emitted are discarded
5. the actual visible chips come later through the post-response chip job

This single-source rule is important. If you restore inline book chips and also keep the post-response generator, the UI can diverge.

### 3.3 Service-agent live path

Service-agent turns can be different.

- if the service agent already emitted `decision.suggested_actions`, the post-response job reuses them
- this is treated as `chip_source = "service_agent_inline"`
- those chips are already grounded in the active service KB

So service turns may have live inline chips, but they are still funneled into the same post-response slot for `/suggestions`.

## 4. Post-response chip job

File: `services/llm_orchestration_service.py`

Main entry point:

- `schedule_post_response_chip_job(...)`

Supporting reader:

- `await_post_response_chips(session_id, wait_ms=4000)`

### 4.1 When the job is skipped

The job is cancelled and skipped only for form-render turns:

- `decision.pending_action == "collect_form_details"`
- or `context.pending_action == "collect_form_details"`
- or `decision.action == "collect_info"`

Everything else still gets chips.

### 4.2 Job lifecycle

For each session it stores:

```python
{
    "chips": [],
    "task": <asyncio task or None>,
    "ts": time.time(),
}
```

Flow:

1. cancel any old chip job for that session
2. resolve the target service for followups
3. create a background runner
4. runner chooses chip source:
   - `service_agent_inline` if `decision.suggested_actions` already exists
   - otherwise `post_response_agent` via `_run_next_action_suggestion_agent()`
5. normalize/filter/cap chips
6. write final chips into `job["chips"]`
7. log `post_response_chip_job_done ... source=... raw=... kept=...`
8. if chips exist and chip speculation is enabled, start detached background speculation for chip-click answers

### 4.3 Read behavior

`await_post_response_chips()` does:

- immediate return if `job["chips"]` already exists
- otherwise waits up to the passed timeout on the background task
- returns normalized chips or `[]`

`/api/chat/suggestions` uses this to avoid blocking too long.

## 5. Next-action suggestion agent

File: `services/llm_orchestration_service.py`

Main method:

- `_run_next_action_suggestion_agent(...)`

Prompt source:

- `prompts/defaults/hotel/orchestrator.next_suggestions.md`

### 5.1 Hard gates

The agent returns `[]` if:

- `chat_llm_next_suggestions_enabled` is off
- OpenAI key is missing
- `decision.suggested_actions` already exists

### 5.2 Who answered this turn

The method determines:

- `answering_llm = "service_agent"` if a target service answered
- otherwise `answering_llm = "main_orchestrator"`

If a service agent answered, it also builds:

- `service`
  - `id`
  - `name`
  - `profile`
- `answering_service_kb`
  - `extracted_knowledge`
  - `required_slots`

### 5.3 Delivery boundary and service scoping

The method builds `bot_delivery_boundary` from capability summary:

- enabled capabilities
- property-specific `donts`
- medium restrictions like text-only and no external real-time data

Then it builds:

- `allowed_services`
- `blocked_service_names`

Important rule:

- when `answering_llm == "service_agent"`, `allowed_services` is restricted to **only** the active service

This is the main reason service-agent turns do not leak chips for unrelated services.

### 5.4 Payload sent to the prompt

The payload includes:

- `user_message`
- `assistant_response`
- `answering_llm`
- `selected_phase`
- `current_state`
- `pending_action`
- `pending_data_public`
- stripped `decision`
- `service`
- `answering_service_kb`
- `history`
- `bot_delivery_boundary`
- `allowed_services`
- `blocked_service_names`
- `response_contract`

The method also prepends/appends optional blocks when available:

- FAQ context
- booking context prompt block

### 5.5 Prompt contract

The prompt in `orchestrator.next_suggestions.md` is strict. Key rules:

- output strict JSON only
- return 1 to 3 suggestions
- read `bot_delivery_boundary` first
- do not re-ask what the assistant just answered
- use conversation history
- respect `decision.missing_fields`
- KB grounding is mandatory
- if `service_agent`, suggestions must stay inside `answering_service_kb.extracted_knowledge`
- blocked services must never be suggested
- return zero suggestions instead of weak/unsupported ones
- use first-person guest phrasing
- no unique/personal info
- no dates/times/quantities/money
- keep each suggestion 2 to 8 words
- treat PRIMARY KNOWLEDGE as authoritative over steady-state knowledge

This prompt is a major part of the logic, not just wording.

## 6. Book-answerer quick chips helper

File: `services/llm_orchestration_service.py`

Helpers:

- `_run_quick_chips_llm()`
- `_split_answer_chips_and_focus()`

### 6.1 What the helper does

`_run_quick_chips_llm()` is a small KB-grounded chip LLM with payload:

- `user_message`
- `selected_phase`
- `active_property_ids`
- `book_properties`
- `services_available_now`
- `services_unavailable_now`
- `history_last_5`
- `knowledge_base_excerpt`

Prompt rules include:

- exactly 3 short chips
- informational only
- stay on active property
- no booking/reserve actions
- no price/breakfast/photo/availability topics
- only emit chips whose supporting fact exists in the KB excerpt

### 6.2 Current live usage

Important detail: the current live book-route path does **not** rely on this helper for user-visible chips.

- `_run_book_answerer_llm()` has `wait_for_chips=False` by default
- the helper only runs if some caller explicitly enables `wait_for_chips`
- current live book/auto short-circuits ignore inline chips and use the post-response generator instead

`_split_answer_chips_and_focus()` still parses trailing:

- `SUGGESTIONS: chip1 | chip2 | chip3`
- `FOCUS: property_id_1 | property_id_2`

But today the `FOCUS:` part matters much more than the inline chip part.

## 7. `/api/chat/suggestions` endpoint

File: `api/routes/chat.py`

Endpoint:

- `POST /api/chat/suggestions`
- handler `get_contextual_suggestions(...)`

### 7.1 Request inputs

`SuggestionsRequest` currently carries:

- `last_bot_message`
- `user_message`
- `hotel_code`
- `current_phase`
- `session_id`
- `assistant_turn_id`
- `fallback_suggestions`

### 7.2 Decision order

The endpoint comment is accurate. Current flow is:

1. wait briefly for post-response chips prepared by orchestration
2. if ready chips exist, return them
3. otherwise, if runtime fallback suggestions exist, return those
4. otherwise, if deep-path suggestions are enabled, run the contextual KB-LLM
5. otherwise return `[]`

### 7.3 Ready-chip path

If `session_id` exists:

1. call `await_post_response_chips()` with wait budget derived from `chat_post_response_chip_wait_ms`
2. if chips are ready:
   - return first 3
   - do not re-filter them
   - optionally schedule suggestion prefetch and return `prefetch_batch_id`

### 7.4 Runtime fallback path

If the caller supplied `fallback_suggestions`:

- sanitize/filter them first
- if non-empty, return them
- optionally schedule prefetch for those fallback chips too

This is still "generator-only" in the sense that the endpoint is not inventing new chips on its own here; it is forwarding already supplied runtime candidates.

### 7.5 Deep-path contextual chip generation

If no ready chips and no usable fallback chips exist, and `chat_suggestions_deep_path_enabled` is on, the endpoint builds a full runtime context.

It loads:

- conversation context
- conversation state
- pending action
- `pending_data_public`
- known guest context
- memory snapshot
- recent conversation history
- active service
- selected phase
- in-phase services
- out-of-phase services
- full hotel KB text
- service knowledge corpus
- capability summary and NLU policy

It then builds `context_payload` with:

- `last_bot_message`
- `user_message`
- `conversation_history`
- `conversation_state`
- `pending_data_public`
- `journey_phase`
- `active_service`
- `services_allowed_in_phase`
- `services_outside_phase`
- `knowledge_base`
- `candidate_suggestions_from_runtime`
- `bot_delivery_boundary`

The endpoint uses its own inline system prompt, not `orchestrator.next_suggestions.md`.

Deep-path prompt rules:

- exactly 3 suggestions
- informational only
- no booking/reserving
- no rates/prices/cost
- no breakfast-included checks
- no availability checks
- no photo/image requests
- avoid re-asking what the assistant just answered
- use first-person guest wording

Then it:

- extracts candidates
- sanitizes and quality-filters them
- falls back to sanitized runtime candidates if needed
- optionally schedules prefetch

### 7.6 Failure behavior

If the endpoint throws:

- return sanitized/filtered `fallback_suggestions`
- otherwise return `[]`

## 8. Suggestion prefetch service

File: `services/suggestion_prefetch_service.py`

This is the answer-prefetch layer behind clicked chips.

### 8.1 What gets scheduled

`schedule_batch(...)` is called from `/api/chat/suggestions` for:

- ready post-response chips
- fallback chips
- deep-path generated chips

Each batch stores:

- `session_id`
- `assistant_turn_id`
- `phase_id`
- `conversation_state`
- last bot message / triggering user message
- recent history
- active service
- in-phase services
- KB excerpt
- capabilities summary
- memory snapshot
- selected phase context
- pending action
- `pending_data_public`
- known context

Important batch controls:

- dedupe suggestions before storing them
- TTL via `chat_suggestion_prefetch_ttl_seconds`
- max batches per session via `chat_suggestion_prefetch_max_batches_per_session`
- parallel generation via `chat_suggestion_prefetch_parallelism`

Each entry gets its own background task.

### 8.2 Fingerprints that prevent stale hits

Each batch stores:

- `config_fingerprint`
- `context_fingerprint`

These are later checked during cache lookup.

Current behavior:

- if the latest session context fingerprint changed, older prefetch entries are treated as stale
- if capability/config fingerprint changed, old prefetch entries are rejected

This is important. Otherwise chips from an older turn can answer against the wrong context.

### 8.3 Rehydrating runtime context

Method:

- `_build_runtime_context(batch)`

This is where suggestion-prefetch reconnects to sticky service/property state.

`pending_data_public` is converted back into private `context.pending_data` keys:

- `_active_property_ids`
- `_active_property_names`
- `_selected_property_scope_ids`
- `_selected_property_scope_id`
- `_selected_property_scope_name`
- `_known_property_scope_count`
- `_last_service_id`
- `_last_service_name`

There are fallback rules:

- if active ids exist but names do not, names fall back to ids
- if selected scope ids are missing but active ids exist, scope ids fall back to active ids
- if exactly one active property exists and no single scope id exists, use that property id
- if `active_service` exists and `_last_service_id` is missing, re-inject it

This rehydration is required because `/suggestions` only exposes public mirror fields, not `_...` private keys.

### 8.4 How answers are generated

Method:

- `_generate_answer_for_entry(batch_id, entry_id)`

Generation order:

1. embed chip text
2. try `_generate_with_orchestration_flow(...)`
   - this calls `llm_orchestration_service.orchestrate_turn(...)`
   - this is the preferred path because it preserves routing, service labels, follow-up chips, property ids, and confidence
3. if orchestration returns no answer, fall back to a plain-text concierge prompt

The orchestration-first step is critical. It is what makes a chip click behave like a real typed turn instead of a separate side channel.

### 8.5 Metadata captured in cached payloads

When orchestration returns a decision, the cached payload stores:

- `response_source = "suggestion_prefetch_cache"`
- `prefetch_batch_id`
- `prefetch_entry_id`
- `prefetch_chip_text`
- `prefetch_speculative = True`
- `prefetch_config_fingerprint`
- `prefetch_context_fingerprint`
- `service_llm_label`
- `service_llm_confidence`
- `target_service_id`
- `orchestration_target_service_id`
- `prefetch_followup_chips`
- `selected_property_name`
- `selected_property_ids`
- `selected_phase_id`
- `selected_phase_name`

Do not drop these. Cache-hit replay depends on them.

### 8.6 How lookup works on the next `/messages` call

Inbound message path:

- `api/routes/chat.py`

Current frontend/request contract:

- chip click must send `prefetch_batch_id` or `ui_prefetch_batch_id`
- chip click must also send `ui_source_type` or `source_type = "suggested_action"`

If the new input is **not** a suggestion-chip click:

- `cancel_session_prefetch()` is called
- stale prefetch work for that session is cancelled

If it **is** a chip click:

- `/messages` calls `suggestion_prefetch_service.resolve_cached_answer(...)`

### 8.7 Exact-click priority path

`resolve_cached_answer()` has a special fast path for real chip clicks:

- `_try_resolve_exact_chip_priority(session_id, user_message, batch_id)`

Behavior:

1. find the exact matching chip entry in the preferred batch/session
2. if it is already ready, consume it immediately
3. if it is still running:
   - cancel sibling prefetch tasks for the session
   - wait for the exact target task up to `chat_chip_speculation_click_wait_ms`
4. if it finishes ready, consume that payload as an exact hit

This is separate from semantic matching and is the main "chip click should feel instant" path.

### 8.8 Semantic fallback

If no exact ready hit is available:

- embed the new message
- compare against ready chip embeddings
- use cosine similarity
- apply `chat_suggestion_prefetch_similarity_threshold`

If similarity is high enough and context/config fingerprints still match:

- consume that cached payload
- annotate metadata with:
  - `prefetch_similarity`
  - `prefetch_match_mode`
  - `prefetch_threshold`

## 9. Cache-hit replay into the normal chat flow

File: `api/routes/chat.py`

Method:

- `_build_prefetch_cached_response(...)`

This is the most important replay step.

### 9.1 What it does

On a prefetch cache hit it:

1. loads or creates the real conversation context
2. writes the user message and assistant message into history with `prefetch_cache_hit=True`
3. restores sticky service info:
   - `_last_service_id`
   - `_last_service_name`
4. restores property focus:
   - call `llm_orchestration_service._persist_book_answerer_focus(...)` with cached `selected_property_ids`
   - if one property name exists, refresh `_selected_property_scope_name`
   - refresh `_active_property_names`
5. restores selected phase:
   - `_selected_phase_id`
   - `_selected_phase = {"id", "name"}`
6. saves context
7. republishes cached follow-up chips through `publish_post_response_chips()`
8. returns `ChatResponse` with the cached answer and cached follow-up chips

### 9.2 Why republishing chips matters

The frontend still fetches chips through `/api/chat/suggestions`.

If `_build_prefetch_cached_response()` does not call `publish_post_response_chips()`, a chip-click cache hit answers correctly once but shows no follow-up chips afterward.

## 10. Chip speculation inside `LLMOrchestrationService`

This is separate from suggestion prefetch service, but they work together.

### 10.1 Current behavior

Inside `orchestrate_turn()`:

- at the start of a new live turn, old background speculation is cancelled
- if the new user message exactly matches a previously speculated chip, the cached speculative decision can be returned immediately
- on that hit, sticky service/property scope is persisted back into `context.pending_data`
- cached chips are republished via `publish_post_response_chips()`
- next-level speculation is kicked off for the newly returned chips

This creates a cascading tree-of-clicks effect.

### 10.2 Why this exists in addition to suggestion prefetch

Suggestion prefetch cache stores speculative answer payloads and is used from the API layer.

Chip speculation inside orchestration stores speculative `OrchestrationDecision` objects and is used from the orchestration layer.

Both are current behavior. Do not assume one replaced the other.

## 11. State and metadata that must round-trip

### 11.1 Pending-data public mirrors

File: `api/routes/chat.py`, `_extract_prefetch_pending_data_public()`

These private fields are mirrored out for suggestions/prefetch:

- `active_property_ids <- _active_property_ids`
- `active_property_names <- _active_property_names`
- `selected_property_scope_ids <- _selected_property_scope_ids`
- `selected_property_scope_id <- _selected_property_scope_id`
- `selected_property_scope_name <- _selected_property_scope_name`
- `known_property_scope_count <- _known_property_scope_count`
- `last_service_id <- _last_service_id`
- `last_service_name <- _last_service_name`

These exact mirrors are what let prefetch and chip clicks rebuild the same scope.

### 11.2 Important decision/response metadata

Values currently used downstream include:

- `suggested_actions_source`
  - `post_response_chip_job`
  - `chat_service_fallback`
  - `post_response_generator`
- `service_llm_label`
- `service_llm_confidence`
- `target_service_id`
- `orchestration_target_service_id`
- `prefetch_followup_chips`
- `selected_property_name`
- `selected_property_ids`
- `selected_phase_id`
- `selected_phase_name`
- `prefetch_batch_id`
- `prefetch_entry_id`
- `prefetch_similarity`
- `prefetch_match_mode`
- `prefetch_threshold`

## 12. Current end-to-end lifecycle

Use this as the rebuild order.

1. `/messages` produces the live answer without blocking on chip generation.
2. `_ensure_suggested_actions()` marks chip source but does not generate chips inline.
3. `schedule_post_response_chip_job()` starts background chip generation.
4. Job either:
   - reuses service-agent inline chips
   - or runs `_run_next_action_suggestion_agent()`
5. Filtered chips are published into `_post_response_chip_jobs[session_id]["chips"]`.
6. Frontend calls `/api/chat/suggestions`.
7. `/suggestions` first waits briefly for that post-response chip slot.
8. If ready chips exist, it returns them and optionally schedules answer prefetch.
9. If no ready chips exist, it can return sanitized fallback chips or run the deep-path KB chip generator.
10. Prefetch batches generate speculative answers in the background, capturing service/property/phase metadata.
11. When the guest clicks a chip, `/messages` uses `prefetch_batch_id` plus `source_type="suggested_action"` to look for a cached answer.
12. On cache hit, `_build_prefetch_cached_response()` restores sticky state, republishes follow-up chips, and returns the cached answer.
13. In parallel, orchestration-level chip speculation can also short-circuit future chip clicks with speculative decisions.

## 13. Files and functions to restore

The main moving parts are:

- `api/routes/chat.py`
  - `_extract_prefetch_pending_data_public`
  - `_build_suggestion_prefetch_snapshot`
  - `_sanitize_suggestions`
  - `_filter_chip_quality`
  - `_build_prefetch_cached_response`
  - `get_contextual_suggestions`
  - inbound `/messages` prefetch lookup logic
- `services/llm_orchestration_service.py`
  - `publish_post_response_chips`
  - `schedule_post_response_chip_job`
  - `await_post_response_chips`
  - `_run_next_action_suggestion_agent`
  - `_ensure_suggested_actions`
  - `_run_quick_chips_llm`
  - `_split_answer_chips_and_focus`
  - `_persist_book_answerer_focus`
  - chip speculation handling in `orchestrate_turn()`
- `services/suggestion_prefetch_service.py`
  - `_build_runtime_context`
  - `_generate_with_orchestration_flow`
  - `_generate_answer_for_entry`
  - `schedule_batch`
  - `resolve_cached_answer`
  - `_try_resolve_exact_chip_priority`
  - `cancel_session_prefetch`
- `prompts/defaults/hotel/orchestrator.next_suggestions.md`

## 14. Minimum rebuild checklist

If you re-add this after reset, restore it in this order:

1. Restore the post-response chip job store and reader.
2. Restore `_ensure_suggested_actions()` as a non-blocking marker, not a synchronous generator.
3. Restore `_run_next_action_suggestion_agent()` and its prompt contract.
4. Restore the canonical chip quality filters.
5. Restore book-route behavior that discards inline chips and uses the post-response generator.
6. Restore service-agent inline-chip reuse inside the post-response job.
7. Restore `/api/chat/suggestions` wait order:
   - ready chips
   - fallback chips
   - deep-path LLM
8. Restore suggestion-prefetch batch scheduling from `/suggestions`.
9. Restore public/private pending-data mirroring for active property/service scope.
10. Restore orchestration-first prefetch answer generation and cached metadata fields.
11. Restore chip-click request metadata handling:
   - `prefetch_batch_id`
   - `ui_prefetch_batch_id`
   - `ui_source_type`
   - `source_type`
12. Restore cache-hit hydration in `_build_prefetch_cached_response()`.
13. Restore prefetch cancellation on typed non-chip turns.
14. Restore orchestration-level chip speculation and chip republishing on hit.

If steps 9, 10, or 12 are missed, chip clicks will answer once but the next turn will lose routing continuity.

## 15. Existing tests and current gaps

Direct current test coverage is thin.

Existing explicit test file:

- `tests/test_suggestion_prefetch_runtime_context.py`
  - verifies `_extract_prefetch_pending_data_public()` mirrors property/service scope
  - verifies `suggestion_prefetch_service._build_runtime_context()` rehydrates private scope keys

There is currently much less direct automated coverage for:

- post-response chip job behavior
- `/suggestions` ready-vs-fallback-vs-deep-path order
- exact-chip prefetch priority behavior
- cache-hit chip republishing
- service-agent inline chip reuse

If you rebuild this after a reset, these are the first missing tests worth restoring.
