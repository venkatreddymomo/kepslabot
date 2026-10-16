# Multi-property logic

This file captures the current multi-property implementation so it can be rebuilt after reverting the repo.

## Goal

The current implementation does **not** treat property focus as a one-off choice. It keeps a live property scope in conversation state, updates it on every turn, and reuses it in:

- main-router book/service routing
- book KB loading
- book-answerer replies
- service-agent KB scoping
- post-response suggestion chips
- suggestion prefetch / chip-click cache hits
- booking/form fallbacks that still need a property name

If you re-implement only one piece and miss the others, the flow breaks on the next turn.

## 1. Core state that must exist

Main storage is `context.pending_data`.

Required private keys:

- `_active_property_ids`
  - Canonical list of properties currently in scope for the conversation.
  - This is the main source of truth after turn 1.
- `_active_property_names`
  - Best-effort names for `_active_property_ids`.
  - Can temporarily contain ids as fallback until KB scoping refreshes names.
- `_past_property_context`
  - Map of properties that were previously active but were dropped.
  - Used so the router can add them back when the guest references an older property again.
- `_selected_property_scope_ids`
  - Legacy mirror of `_active_property_ids`.
- `_selected_property_scope_id`
  - Legacy single-property mirror when only one property is active.
- `_selected_property_scope_name`
  - Legacy single-property name mirror.
- `_known_property_scope_count`
  - Number of properties detected for the tenant.
- `_available_property_manifest`
  - Property list cached from the hotel book bundle.
- `_stay_property_id`
  - Canonical booked/stay property id.
- `_stay_property_name`
  - Human-readable booked/stay property name.
- `_stay_property_pin_enabled`
  - Whether stay-property pinning is active.
- `_stay_property_pin_last_source`
  - Debug/tracing field for where pin re-application happened.
- `_last_service_id`
  - Sticky active service id.
- `_last_service_name`
  - Sticky active service name.
- `_selected_phase_id`
  - Selected journey phase id.
- `_selected_phase`
  - Object with `id` and `name`.

Public mirrors that must be exposed for suggestions/prefetch:

- `active_property_ids`
- `active_property_names`
- `selected_property_scope_ids`
- `selected_property_scope_id`
- `selected_property_scope_name`
- `known_property_scope_count`
- `last_service_id`
- `last_service_name`

These public mirrors are produced in `api/routes/chat.py::_extract_prefetch_pending_data_public()` and rehydrated back to private `_...` keys in `services/suggestion_prefetch_service.py::_build_runtime_context()`.

## 2. Where active property ids come from

There are multiple entry points. All of them matter.

### 2.1 Booking metadata seed

File: `services/chat_service.py`, method `_ingest_request_metadata()`

When request metadata contains `booking_property_name`:

1. `context.booking_property_name` is set.
2. `_selected_property_scope_name` is immediately set to the raw property label.
3. `config_service.resolve_property_scope_ids(property_hints=[booking_property_name])` tries to resolve the raw label to a canonical property id.
4. `_stay_property_id` / `_stay_property_name` are stored.
5. Phase decides how focus is seeded:
   - `pre_checkin`, `during_stay`, `post_checkout`
     - `_active_property_ids = [resolved_property_id or raw_label]`
     - `_active_property_names = [resolved_property_name]`
     - `_selected_property_scope_id = resolved_property_id or raw_label`
     - `_selected_property_scope_name = resolved_property_name`
     - `_stay_property_pin_enabled = true`
   - `pre_booking`
     - preserve old behavior:
     - `_active_property_ids = [raw booking_property_name]`
     - `_selected_property_scope_name = raw booking_property_name`
     - `_stay_property_pin_enabled = false`

This path is important because post-booking phases should default to the guest's own property even before any LLM routing happens.

### 2.2 First-turn property-scope detection from the KB

File: `services/config_service.py`, method `build_property_scoped_kb_context()`

Used by `services/llm_orchestration_service.py::_prepare_full_kb_context_for_message()` when there is no cached active property set fast path.

Inputs:

- `user_message`
- `history_text`
- `seed_property_id`
- property index from `ensure_property_scope_index()`

How it decides `selected_property_ids`:

1. `current_matches = _match_property_scope_ids_from_text(user_message, property_index, ...)`
2. `history_matches = _match_property_scope_ids_from_text(history_text, property_index, ...)`
3. `seeded_property_id` comes from existing `_selected_property_scope_id`
4. Detect special modes:
   - listing request
   - compare request
   - property-sensitive ask
5. Decision order for multi-property tenants:
   - compare request with 2+ ids from current/history -> `mode=compare`, use those ids
   - exactly 1 current match -> `mode=single`, use it
   - seeded property id and not listing -> `mode=single`, use it
   - exactly 1 history match and not listing -> `mode=single`, use it
   - listing request -> `mode=listing`
   - property-sensitive + property-specific ask with no clear match -> `mode=clarify`
   - else `mode=common`
6. It returns:
   - `active_property_id`
   - `active_property_name`
   - `selected_property_ids`
   - `matched_property_ids`
   - `history_property_ids`
   - `property_manifest`
   - `requires_clarification`
   - `clarification_question`
   - `scoped_full_kb_text`

This is the first-turn/non-cached property identification path.

### 2.3 Main-router property focus

File: `services/llm_orchestration_service.py`, main-router logic around `_run_main_router()`

The main router is a separate LLM pass. It receives:

- `available_properties`
- `active_property_ids`
- `past_property_context`
- `book_catalog`
- `in_phase_services`
- recent history

Important implementation details:

- `book_catalog` is fetched for the whole hotel, not SQL-filtered to the active property.
- If there is an active property set, books outside that set are still shown to the router, but in a slimmed-down shape.
- This is deliberate. Filtering the router catalog too early prevents property switching.

The router must output:

```json
{
  "route": "service|book|clarify|auto",
  "service_id": "",
  "selected_property_ids": ["property_id_1"],
  "book_ids": ["book_1"],
  "clarification_question": "",
  "reason": "",
  "confidence": 0.0
}
```

Prompt source:

- `prompts/defaults/hotel/orchestrator.main_router.md`

Runtime code appends stronger property rules on top of the prompt. Key rules:

- multi-property hotels must keep `selected_property_ids` filled
- city mention -> include all matching properties in that city
- compare/between them -> resolve from current `active_property_ids` first
- clean continuation with no new property name -> keep current `active_property_ids`
- generic ask with no active set -> use all available properties
- add/remove/switch properties explicitly

Router result parsing:

- `selected_property_ids` are normalized and validated against `available_properties`
- invalid ids are dropped
- `book_ids` are validated against the catalog

Very important fallback:

- if route is `book` and the guest is still broad across many properties, `_expand_book_ids_for_unresolved_multi_property()` expands thin/narrow book picks so missing properties do not disappear from coverage

## 3. Property resolution order inside `orchestrate_turn()`

File: `services/llm_orchestration_service.py`, method `orchestrate_turn()`

After the main-router returns, property focus is resolved in this order:

1. `main_router_hint["selected_property_ids"]`
2. existing `context.pending_data["_active_property_ids"]`
3. derive property ids from chosen `book_ids` via `_derive_property_ids_from_book_catalog()`
4. for `book` / `auto` with no scope -> all properties from the hotel book catalog

Then:

1. `_persist_book_answerer_focus()` is called with the resolved ids
2. catalog is filtered to that property scope using `_filter_catalog_to_property_scope()`
3. chosen `book_ids` are reduced to that scope using `_filter_book_ids_to_property_scope()`
4. if there are no chosen books but there is a property scope, all books under that property scope are loaded

This order is what keeps the scope sticky across turns.

## 4. Stay-property pin

File: `services/llm_orchestration_service.py`

- `_is_stay_property_pin_phase()` returns true for:
  - `pre_checkin`
  - `during_stay`
  - `post_checkout`
- `_apply_stay_property_pin()` prepends `_stay_property_id` into the active property list if:
  - current phase is one of the above
  - `_stay_property_id` exists
  - that id is not already in the list

This function is called in multiple places:

- after book-answerer focus persistence
- after full orchestrator property output
- before KB fast-path loading
- during speculation / cache-hydration flows

If this pin is missing in the reimplementation, in-stay users can drift to another property.

## 5. Book-answerer path

File: `services/llm_orchestration_service.py`, method `_run_book_answerer_llm()`

### Inputs sent to the book-answerer

The book-answerer payload includes:

- `active_property_ids`
- `book_properties`
- `past_property_context`
- `services_available_now`
- `services_unavailable_now`
- history and memory summary

`book_properties` is built from `book_route_context["books"]` using each book's:

- `property_slug`
- `property_id`
- `property_name`

Stub/fake property rows are filtered out:

- `default`
- `shared`
- `common`
- `all_properties`
- related variants

### Multi-property answerer contract

If there are multiple `book_properties` and no single active property id, the answerer gets an extra mandatory contract:

- cover every property
- do not silently drop any property
- reduce depth per property instead of omitting properties
- end by asking which property the guest wants to continue with

### Mandatory `FOCUS:` line

The answerer must end with a trailing line:

```text
FOCUS: property_id_1 | property_id_2
```

Rules:

- ids must come from `book_properties[].id`
- no `all`, `none`, or `-`
- if the answer covered all properties, list all ids
- if the guest narrowed to one/subset, emit only that subset

Parsing:

- `_split_answer_chips_and_focus()` strips `FOCUS:` and optional `SUGGESTIONS:` suffixes
- if `FOCUS:` is missing, focus defaults to all `book_properties` ids

Persistence:

- `_persist_book_answerer_focus(context, focus_ids, selected_phase_id)` writes the next-turn active scope

### `_persist_book_answerer_focus()` behavior

This helper is the shared book/service persistence function for focus updates.

Rules:

- empty `focus_ids` -> leave current scope untouched
- non-empty `focus_ids`:
  - normalize/dedupe
  - reapply stay-property pin
  - move dropped old ids into `_past_property_context`
  - set `_active_property_ids`
  - set `_active_property_names` to ids as fallback
  - set `_selected_property_scope_ids`
  - set `_selected_property_scope_id` if only one id

This helper is used by:

- book route short-circuit
- auto route short-circuit
- service route merge
- prefetch cached-response hydration

## 6. Service-agent path

File: `services/llm_orchestration_service.py`, method `_run_service_agent()`

### How property scope reaches service agents

Service agents do **not** rely on raw `_active_property_ids` being passed in the JSON payload.

The main property isolation is done before the LLM call:

1. `service["extracted_knowledge"]` is loaded
2. `_scope_service_knowledge_to_runtime_property()` scopes it to the current runtime property set

Scope resolution order inside `_scope_service_knowledge_to_runtime_property()`:

1. `_active_property_ids`
2. `_selected_property_scope_ids`
3. `_selected_property_scope_name`
4. `_stay_property_name` / `_stay_property_id`
5. if still empty -> do not scope, return full service KB

Behavior:

- if the scoped KB contains matching `=== PROPERTY: ... ===` sections, only matching property sections plus common knowledge are kept
- if active scope does not match any property section, the function returns only common knowledge
- this is a deliberate anti-leak rule so one property's service facts do not spill into another property's answer

### Additional service-agent safety rule

`_build_service_system_prompt()` injects a multi-property guard when service knowledge contains `=== PROPERTY:` sections:

- use only the clear property section plus shared/common knowledge
- if property is unclear for a property-specific transactional ask, ask briefly
- never mix sections
- never continue transactionally until property is clear

There is also a standalone prompt block in:

- `prompts/defaults/hotel/service_writer.multi_property_rule.md`

### Service route short-circuit

When main-router returns `route=service`:

1. `_service_focus_ids` is built from:
   - main-router `selected_property_ids`
   - else `property_scope["selected_property_ids"]`
   - else current `_active_property_ids`
2. `base_decision.selected_property_ids` is set to that list
3. service agent runs
4. `_merge_decisions(base_decision, service_decision)` preserves service agent property updates
5. if merged decision still has `selected_property_ids`, `_persist_book_answerer_focus()` is called
6. if there is a single selected property and a name, `_selected_property_scope_name` is refreshed

This is how service turns keep property focus sticky for the next turn.

## 7. Heavy orchestrator property persistence

File: `services/llm_orchestration_service.py`, still in `orchestrate_turn()`

If the request goes through the heavy orchestrator instead of the short-circuit book/service paths, the returned `OrchestrationDecision` must contain:

- `selected_property_name`
- `selected_property_ids`

The runtime then:

1. normalizes ids
2. reapplies stay-property pin
3. moves dropped old ids into `_past_property_context`
4. updates:
   - `_active_property_ids`
   - `_active_property_names`
   - `_past_property_context`
   - `_selected_property_scope_ids`
   - `_selected_property_scope_name`
   - `_selected_property_scope_id` when exactly one id

If the LLM only returned `selected_property_name` and no ids, only `_selected_property_scope_name` is updated.

## 8. Chip generation and suggestion flows

There are three chip-related paths. All need the same property scope continuity.

### 8.1 Book-answerer quick chips

File: `services/llm_orchestration_service.py`, method `_run_quick_chips_llm()`

Payload includes:

- `active_property_ids`
- `book_properties`
- `services_available_now`
- `services_unavailable_now`
- `knowledge_base_excerpt`

This is the fast parallel chip generator used with the book-answerer.

### 8.2 Post-response chip job

File: `services/llm_orchestration_service.py`, methods:

- `schedule_post_response_chip_job()`
- `_run_next_action_suggestion_agent()`

Behavior:

- if service agent already emitted `decision.suggested_actions`, reuse them directly
- otherwise run `orchestrator.next_suggestions`

Important property/service continuity details:

- the next-suggestions payload includes `pending_data_public`
- if the answering LLM was a service agent, `allowed_services` is restricted to only that active service
- chips are published into `_post_response_chip_jobs[session_id]["chips"]`

Prompt source:

- `prompts/defaults/hotel/orchestrator.next_suggestions.md`

### 8.3 Universal speculation for chip clicks

File: `services/llm_orchestration_service.py`, method `_speculate_for_decision_universal()`

Used after a live answer to precompute answers for the suggested chips.

Critical multi-property behavior:

- if there is no existing `book_route_context` (common on service-route turns), speculation rebuilds one from the current `_active_property_ids`
- it fetches the hotel book bundle
- scopes catalog to `active_props`
- builds a speculation-only book context from those properties

Without this, service-route chip clicks lose KB grounding and drift off-property.

## 9. Prefetch / chip-click cache hit path

This is the easiest part to break on reimplementation.

### 9.1 Export public mirrors

File: `api/routes/chat.py`, function `_extract_prefetch_pending_data_public()`

Normal `pending_data_public` strips `_...` keys, so multi-property state would be lost unless mirrored back out.

The code explicitly mirrors:

- `active_property_ids <- _active_property_ids`
- `active_property_names <- _active_property_names`
- `selected_property_scope_ids <- _selected_property_scope_ids`
- `selected_property_scope_id <- _selected_property_scope_id`
- `selected_property_scope_name <- _selected_property_scope_name`
- `known_property_scope_count <- _known_property_scope_count`
- `last_service_id <- _last_service_id`
- `last_service_name <- _last_service_name`

### 9.2 Rehydrate private keys in prefetch runtime context

File: `services/suggestion_prefetch_service.py`, method `_build_runtime_context()`

It takes `pending_data_public` and rebuilds private state:

- `_active_property_ids`
- `_active_property_names`
- `_selected_property_scope_ids`
- `_selected_property_scope_id`
- `_selected_property_scope_name`
- `_known_property_scope_count`
- `_last_service_id`
- `_last_service_name`

Fallback rules inside rehydration:

- if names are missing but ids exist -> `_active_property_names = active_property_ids`
- if scope ids are missing but active ids exist -> `_selected_property_scope_ids = active_property_ids`
- if single active property and no `_selected_property_scope_id` -> use that id

This exact rehydration is required so speculative orchestration sees the same property/service scope as the live turn.

### 9.3 Store scope in cached chip answers

File: `services/suggestion_prefetch_service.py`, method `_generate_answer_for_entry()`

When prefetching an answer, the cached payload metadata stores:

- `service_llm_label`
- `service_llm_confidence`
- `target_service_id`
- `orchestration_target_service_id`
- `prefetch_followup_chips`
- `selected_property_name`
- `selected_property_ids`
- `selected_phase_id`
- `selected_phase_name`

These fields are not optional. They are what make a cache hit behave like the live orchestration path.

### 9.4 Cache-hit response must re-persist property scope

File: `api/routes/chat.py`, function `_build_prefetch_cached_response()`

On cache hit:

1. read `selected_property_name` and `selected_property_ids` from cached metadata
2. persist sticky service:
   - `_last_service_id`
   - `_last_service_name`
3. call `llm_orchestration_service._persist_book_answerer_focus(...)`
4. if one property name is present:
   - `_selected_property_scope_name = selected_property_name`
   - `_active_property_names = [selected_property_name]`
5. restore phase fields
6. publish cached follow-up chips via `publish_post_response_chips()`

If this step is skipped, a chip-click answer may display correctly once, but the next turn loses both property scope and follow-up chips.

## 10. Other downstream consumers that still depend on legacy mirrors

Do not remove the legacy `_selected_property_scope_*` / `_active_property_names` mirrors unless you also rewrite these consumers.

Examples:

- `api/routes/chat.py::_extract_prebooking_booking_payload()`
  - falls back to `_selected_property_scope_name`
  - then to `_active_property_names` when only one is present
- `api/routes/chat.py` form ticket description builder
  - uses `_selected_property_scope_name`
  - also `_stay_property_name`

So the legacy mirrors are still operationally required, not just debug fields.

## 11. Important helper functions to preserve

Files and methods worth recreating exactly:

- `services/chat_service.py::_ingest_request_metadata`
- `services/config_service.py::_match_property_scope_ids_from_text`
- `services/config_service.py::resolve_property_scope_ids`
- `services/config_service.py::build_property_scoped_kb_context`
- `services/llm_orchestration_service.py::_run_main_router`
- `services/llm_orchestration_service.py::_expand_book_ids_for_unresolved_multi_property`
- `services/llm_orchestration_service.py::_derive_property_ids_from_book_catalog`
- `services/llm_orchestration_service.py::_filter_book_ids_to_property_scope`
- `services/llm_orchestration_service.py::_filter_catalog_to_property_scope`
- `services/llm_orchestration_service.py::_scope_service_knowledge_to_runtime_property`
- `services/llm_orchestration_service.py::_persist_book_answerer_focus`
- `services/llm_orchestration_service.py::_split_answer_chips_and_focus`
- `services/llm_orchestration_service.py::_apply_stay_property_pin`
- `services/llm_orchestration_service.py::schedule_post_response_chip_job`
- `services/llm_orchestration_service.py::_speculate_for_decision_universal`
- `services/suggestion_prefetch_service.py::_build_runtime_context`
- `api/routes/chat.py::_extract_prefetch_pending_data_public`
- `api/routes/chat.py::_build_prefetch_cached_response`

## 12. Current end-to-end lifecycle

Use this as the rebuild order.

1. Request metadata may seed booked/stay property focus in `chat_service`.
2. Main-router sees:
   - all available properties
   - active property ids
   - past property context
   - full hotel book catalog
3. Main-router returns `selected_property_ids`.
4. `orchestrate_turn()` resolves final property scope using:
   - router explicit ids
   - existing active ids
   - derived ids from books
   - all properties fallback
5. Focus is persisted via `_persist_book_answerer_focus()`.
6. Book ids are scoped to those properties.
7. Route-specific behavior:
   - `book` -> book-answerer gets scoped books + emits `FOCUS:`
   - `service` -> service KB is scoped before prompt build
   - `auto` -> book-answerer fallback still persists focus
   - heavy orchestrator -> decision writes `selected_property_ids` back to context
8. Post-response chips are generated or reused.
9. Suggestions endpoint mirrors private property/service fields into `pending_data_public`.
10. Prefetch service rehydrates them into private runtime state.
11. Cached chip-click answers re-persist property scope and republish chips.

## 13. Tests that protect this logic

These tests encode the current behavior and should be restored too:

- `tests/test_identity_resolution.py`
  - booking metadata seeds stay-property pin
  - pre-booking keeps old behavior
  - stay-property pin re-applies
  - duplicate book slug derives all matching properties
  - book ids are filtered to active property scope
  - `_persist_book_answerer_focus()` moves dropped ids into `_past_property_context`
  - service KB scoping does not leak other properties
- `tests/test_suggestion_prefetch_runtime_context.py`
  - private property/service scope is mirrored out to public pending data
  - public pending data is rehydrated back to private runtime context
- `tests/test_llm_orchestration_room_booking.py`
  - `_merge_decisions()` preserves `selected_property_name` and `selected_property_ids`

## 14. Minimum rebuild checklist

If you are re-adding this after reset, do these in order:

1. Add `selected_property_name` and `selected_property_ids` to `OrchestrationDecision`.
2. Restore all pending-data keys listed in section 1.
3. Restore booking metadata seeding in `chat_service`.
4. Restore property index matching / property-scoped KB builder in `config_service`.
5. Restore main-router payload + prompt contract for `selected_property_ids`.
6. Restore router fallback order inside `orchestrate_turn()`.
7. Restore `_persist_book_answerer_focus()` and stay-property pinning.
8. Restore book-answerer `FOCUS:` contract and parser.
9. Restore service KB runtime scoping.
10. Restore public/private pending-data mirroring for suggestions/prefetch.
11. Restore cached chip-hit hydration of property ids + follow-up chips.
12. Restore the tests above.

If any one of steps 8, 10, or 11 is missed, the first answer may look correct but the property scope will break on the next turn.
