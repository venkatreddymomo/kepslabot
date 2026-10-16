import os
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, field_validator, model_validator


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        # WHICH ENV FILE THIS PROCESS READS. Defaults to `.env`, so every
        # existing way of starting the app is unchanged. Set BOT_ENV_FILE to
        # run a second environment WITHOUT swapping files around:
        #
        #     BOT_ENV_FILE=.env.dev python main.py     # dev DB, port 8012
        #     python main.py                           # .env as before
        #
        # Swapping `.env` in place is how dev used to be run, which is why the
        # repo carries half a dozen `.env.backup_*` files and why a restore that
        # was forgotten left the local server pointed at prod.
        env_file=os.getenv("BOT_ENV_FILE", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Application
    app_name: str = "KePSLA"
    app_env: str = "development"
    debug: bool = True
    secret_key: str = "change-me-in-production"

    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    kb_scraper_auto_start: bool = False

    # API Gateway
    api_gateway_auth_enabled: bool = False
    api_gateway_api_key: str = ""
    api_gateway_rate_limit_enabled: bool = True
    api_gateway_rate_limit_requests: int = 80
    api_gateway_rate_limit_window_seconds: int = 60

    # Observability / Evaluation
    observability_enabled: bool = True
    observability_log_file: str = "./logs/observability.log"
    backend_trace_enabled: bool = True
    backend_trace_log_file: str = "./logs/backend_trace.jsonl"
    backend_trace_max_chars: int = 2000
    evaluation_metrics_enabled: bool = True
    conversation_audit_enabled: bool = True
    conversation_audit_log_file: str = "./logs/conversation_audit.jsonl"
    turn_diagnostics_enabled: bool = True
    turn_diagnostics_log_file: str = "./logs/turn_diagnostics.jsonl"
    turn_diagnostics_max_chars: int = 250000
    turn_llm_summary_enabled: bool = True
    turn_llm_summary_log_file: str = "./logs/chat_turn_llm_summary.jsonl"
    chat_step_timing_enabled: bool = True
    chat_step_timing_log_file: str = "./logs/chat_step_timing.jsonl"
    chat_step_timing_max_chars: int = 250000
    everything_trace_enabled: bool = True
    everything_trace_log_file: str = "./logs/everything_backend_trace.jsonl"
    everything_trace_max_chars: int = 400000
    step_trace_logging_enabled: bool = True
    step_trace_log_file: str = "./logs/step_trace.jsonl"
    step_trace_max_chars: int = 25000
    log_retention_enabled: bool = True
    log_retention_days: int = 7
    log_retention_cleanup_on_startup: bool = True
    log_retention_cleanup_interval_minutes: int = 360
    log_retention_include_root_temp_logs: bool = True

    # ── DB log sink ──────────────────────────────────────────────────────────
    # Mirror the file-based loggers into the prod DB so logs are queryable from
    # any environment (local or deployed). Files keep writing regardless; the DB
    # sink is an additional, best-effort target that never blocks/raises.
    db_logging_enabled: bool = True
    # Per-stream allowlist. Empty list = ALL known streams are mirrored to DB.
    db_logging_streams: list[str] = []
    # 'local' | 'deployed' | 'auto' (auto-detects from env vars / falls back to local).
    db_logging_source_env: str = "auto"
    # Background writer batching.
    db_logging_queue_maxsize: int = 20000      # bounded; drop-on-full (best effort)
    db_logging_batch_size: int = 200           # rows per bulk insert
    db_logging_flush_interval_seconds: float = 0.5
    # DB-side retention (mirrors the 7-day file policy). Pruned by the existing job.
    db_logging_retention_days: int = 7
    # Store full untrimmed raw LLM input/output + full backend payload in DB.
    db_logging_store_raw_llm_io: bool = True
    # Daily LLM cost/token rollup aggregate table.
    db_logging_cost_rollup_enabled: bool = True
    # Bounded FIFO for KEBO_ENGAGE_SESSIONS writes. Drops on full rather than
    # letting background logging grow memory without bound.
    engage_session_queue_maxsize: int = 5000

    # Database — toggle DB_BACKEND=mysql or DB_BACKEND=postgres in .env
    db_backend: str = "mysql"
    database_url: str = "sqlite+aiosqlite:///./kepsla_bot.db"
    pg_database_url: str = "postgresql+asyncpg://kepsla:CHANGE_ME@35.154.99.170:5433/bsp_platform"
    database_echo: bool = False

    @model_validator(mode="after")
    def _apply_db_backend(self) -> "Settings":
        backend = str(self.db_backend or "").strip().lower()
        if backend in {"postgres", "postgresql"}:
            self.db_backend = "postgres"
            self.database_url = self.pg_database_url
        elif backend == "mysql":
            self.db_backend = "mysql"
        return self
    admin_db_fast_fallback_timeout_seconds: float = 30.0
    admin_db_unavailable_backoff_seconds: float = 5.0

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # OpenAI
    openai_api_key: str = ""
    # The clock the GUEST is on. The service agent used to be handed UTC and told
    # it was UTC, so at 8:27 PM in Sriperumbudur it opened with "It's 2:51 PM now,
    # so breakfast items are not available" - right conclusion, wrong reason, and
    # near midnight it would name the wrong day and the wrong menu window.
    # Ask how many portions when a dish arrives without a number. OFF: the
    # judge behind it still flags a dish the guest DID give a count for
    # ("2 garlic naan"), and asking again about something already answered
    # on every order turn is worse than the assumption it was meant to fix.
    # Let the provenance judge DELETE items from the cart.
    #
    # ON since 2026-08-27, after the question it asks was inverted. It used to
    # ask "which items did the guest ask for?" and delete the remainder, so an
    # item whose ordering message had scrolled out of the judge's window was
    # thrown away - it removed dishes guests had explicitly ordered three times
    # in live testing, which is why this stayed off. It now asks "which did the
    # ASSISTANT introduce, and the guest never take up?" and deletes only those.
    # Deletion has to be positively established; uncertainty keeps the dish.
    #
    # What it is for: the model re-declares its whole running order every turn,
    # so a dish it merely OFFERED gets carried forward as though ordered. That
    # is how "I'm allergic to dairy" put an Arrabbiata Pasta on a kitchen ticket.
    # OFF. The judge deletes food guests really ordered: it refused a Tiramisu
    # after the guest typed 'order me a tiramisu then', and a Filter Coffee
    # after they typed 'oh also add a filter coffee'. That is the same failure
    # that had it switched off before, and inverting the question did not fix
    # it. A stray line is visible on the confirmation and the guest can correct
    # it; food deleted here simply never arrives.
    ird_provenance_refusal_enabled: bool = False
    ird_portion_check_enabled: bool = False
    hotel_timezone: str = "Asia/Kolkata"
    openai_model: str = "gpt-4.1-mini"
    # The small single-purpose judges (offer acceptance, ticket-edit routing,
    # separate-order detection, ticket summaries, reply-claim checks). They were
    # each hardcoded to gpt-4o-mini in twelve places, so the model they ran on
    # could not be changed without editing code. Empty means "use openai_model".
    llm_judge_model: str = ""
    openai_embedding_model: str = "text-embedding-3-small"
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    # LLM Settings
    llm_max_tokens: int = 4096
    llm_temperature: float = 0.7
    llm_timeout: int = 30
    llm_history_max_messages: int = 0
    llm_history_max_chars: int = 12000
    llm_io_trace_enabled: bool = True
    llm_io_trace_file: str = "./logs/llm_io_trace.jsonl"
    llm_io_trace_max_chars: int = 300000
    llm_call_simple_logging_enabled: bool = True
    llm_call_simple_log_file: str = "./logs/llm_call_metrics.jsonl"
    llm_call_simple_text_max_chars: int = 1200
    llm_orchestration_input_log_enabled: bool = False
    llm_model_pricing_json: str = ""

    # RAG Settings
    rag_backend: str = "local"  # local | qdrant
    rag_chunk_size: int = 220
    rag_chunk_overlap: int = 40
    rag_top_k: int = 4
    rag_min_retrieval_score: float = 0.18
    rag_enable_rerank: bool = True
    rag_enable_mmr: bool = True
    rag_mmr_lambda: float = 0.7
    rag_candidate_pool_min: int = 20
    rag_candidate_pool_max: int = 40
    rag_enable_llm_query_rewrite: bool = True
    rag_llm_query_rewrite_max_tokens: int = 64
    rag_enable_llm_rerank: bool = False
    rag_step_logs_enabled: bool = True
    rag_step_log_file: str = "./logs/detailedsteps.log"
    rag_step_log_preview_chars: int = 260
    rag_local_index_file: str = "./data/rag/local_index.json"
    kb_db_only_mode: bool = True
    kb_direct_lookup_enabled: bool = True
    kb_direct_lookup_min_score: float = 0.34
    kb_direct_lookup_max_answer_chars: int = 600
    kb_direct_lookup_step_logs_enabled: bool = True
    kb_direct_lookup_enable_llm_rewrite: bool = True
    kb_direct_lookup_llm_rewrite_max_tokens: int = 64
    kb_direct_disable_rag_fallback: bool = True
    # FAQ Bank — set FAQ_BANK_ENABLED=false in .env to disable the entire FAQ
    # system globally. When disabled, build_faq_block() returns "" so no FAQ
    # context is injected into any LLM call and all FAQ admin endpoints return
    # empty results without touching the database.
    faq_bank_enabled: bool = True

    agent_plugin_runtime_enabled: bool = False
    chat_pure_llm_mode: bool = False
    chat_full_kb_llm_mode: bool = False
    full_kb_llm_max_kb_chars: int = 180000
    full_kb_llm_max_history_messages: int = 0
    full_kb_llm_history_max_chars: int = 12000
    full_kb_llm_memory_summary_chars: int = 2200
    full_kb_llm_force_summary_refresh: bool = True
    full_kb_llm_step_logs_enabled: bool = True
    full_kb_llm_temperature: float = 0.1
    full_kb_llm_passthrough_mode: bool = False
    full_kb_llm_chunk_scan_enabled: bool = False
    full_kb_llm_chunk_chars: int = 18000
    full_kb_llm_chunk_max_chunks: int = 100
    full_kb_llm_chunk_result_chars: int = 64000
    full_kb_llm_pre_shortcuts_enabled: bool = False
    chat_llm_preprocess_enabled: bool = False
    chat_llm_preprocess_model: str = ""
    chat_llm_preprocess_temperature: float = 0.0
    chat_llm_preprocess_max_tokens: int = 80
    chat_llm_orchestration_mode: bool = False
    chat_no_template_response_mode: bool = True
    # Latency: per-conversation cache for shared per-hotel data (services,
    # service_kb_records, full_kb_text, capabilities overlay). Survives
    # across turns of the same conversation; TTL guards against stale data
    # if admin updates config mid-conversation.
    chat_conversation_cache_enabled: bool = True
    chat_conversation_cache_ttl_seconds: int = 1800
    # Latency: defer maybe_refresh_summary as a background task so the
    # response is sent first, summary updates after.
    chat_async_summary_enabled: bool = True
    # Latency: when a live turn marks summary refresh as deferred, keep the
    # updated context hot in memory for the next turn and let the background
    # flush persist it to DB after the response is returned.
    chat_response_first_persistence_enabled: bool = True
    # Latency: persist the conversation snapshot to the DB OFF the critical
    # path (a background task with its own session) so the reply returns before
    # the over-VPN write completes. In-memory context is still saved first, so
    # the next turn sees the latest state; only durable DB write is deferred.
    chat_background_context_save_enabled: bool = False
    # Latency: run the display-beautifier + smart-image attach as a background
    # task instead of blocking the reply on them. Trade-off: the first reply may
    # arrive before images/beautified display are attached (they persist to the
    # turn row afterwards). Off by default — quality-sensitive.
    chat_defer_display_enrichment: bool = False
    # Latency: only run the answer-first guard LLM when it can actually
    # validate something (KB-factual answer, mid-flow collection,
    # ambiguous dispatch). Skip clarify, first-turn intros, ticket create.
    chat_smart_guard_skip_enabled: bool = True
    chat_llm_service_agent_enabled: bool = True
    chat_llm_main_router_enabled: bool = True
    chat_llm_book_routing_enabled: bool = True
    # Phase 5: zero/negative = no soft cap. Adaptive budget computed from the
    # active LLM's context window minus history+system+response overhead.
    chat_llm_book_route_max_books: int = 0
    chat_llm_book_route_max_pages_per_book: int = 0
    chat_llm_book_route_max_chars: int = 0
    # Phase 3: read book catalog and book context from new_bot_kb_books /
    # new_bot_kb_pages tables instead of the JSON blob in business_config.
    # Flip to True after running scripts/backfill_kb_tables.py.
    kb_read_from_tables: bool = False
    # Reserved tokens not available for KB content (system prompt + history +
    # response reservation + safety margin). Used only when chat_llm_book_route_max_chars=0.
    llm_context_reserved_tokens: int = 19000
    llm_orchestration_model: str = ""
    llm_service_agent_model: str = ""
    llm_service_agent_max_tokens: int = 2000
    chat_llm_main_router_model: str = ""
    chat_llm_service_router_model: str = ""
    chat_llm_continuity_router_model: str = ""
    chat_llm_book_answerer_model: str = ""
    chat_llm_next_suggestions_model: str = ""
    # MULTI-MODEL: run each orchestration role on the model it's best at
    # (routing on 4.1, answering + chips on 5.4), each loading THAT model's
    # prompt variant. When True the /chat harness model dropdown is DISABLED —
    # it becomes a no-op so it can't collapse the turn back onto one model.
    # A deployment pin (customer embed) still wins, so customers are unaffected.
    multi_model_enabled: bool = True
    # Per-hotel model, e.g. {"mango_grove_chandigarh": "gpt-4.1-mini"}: every
    # LLM role for a listed hotel runs on that model (and loads its prompts);
    # hotels not listed are untouched. Empty = off.
    chat_llm_model_by_hotel_json: str = ""
    # One model for every role of every hotel set up in the Bot Training ->
    # Pre-booking tab (T24 onward) that is not listed above. Empty = those
    # hotels resolve like any other hotel.
    chat_llm_prebooking_hotels_model: str = ""
    chat_llm_service_continuity_router_enabled: bool = False
    # Latency gate: when the main router returns route=book with at least one
    # selected book and confidence at or above this threshold, skip the
    # service-continuity LLM check. Structural-only — uses the router LLM's
    # own confidence + book selection, not any keyword analysis.
    chat_llm_continuity_skip_confidence_threshold: float = 0.85
    # Form-submit ticket description: by default we use the deterministic
    # fallback (built from form fields) — fast, no LLM call. Enable polish
    # to have an LLM rewrite the issue/message into prettier prose for the
    # operations staff at the cost of 5-20s extra per form submission.
    chat_form_ticket_llm_polish_enabled: bool = False
    chat_form_ticket_llm_polish_timeout_ms: int = 4000
    chat_llm_main_router_full_catalog_without_active_scope: bool = False
    chat_llm_main_router_max_tokens: int = 700
    chat_llm_service_router_max_tokens: int = 320
    chat_llm_continuity_router_max_tokens: int = 220
    chat_llm_book_answerer_max_tokens: int = 700
    chat_main_router_book_desc_chars: int = 120
    chat_main_router_book_max_aliases: int = 2
    chat_main_router_book_max_keywords: int = 4
    chat_llm_book_answerer_history_messages: int = 6
    chat_llm_book_answerer_history_chars: int = 2600
    # Raised for large-context models (gpt-4.1/gpt-5 ~1M window): a cross-hotel
    # question loads one book per hotel × ~24 hotels, so the KB budget must hold
    # them all or the answer silently drops hotels. 500k chars ≈ 125k tokens —
    # well within the window. Single-hotel turns load little, so they're unaffected.
    chat_llm_book_answerer_max_kb_chars: int = 500000
    chat_llm_book_answerer_memory_summary_chars: int = 1400
    chat_llm_book_route_max_selected_books: int = 12
    chat_llm_book_route_auto_char_cap: int = 500000
    chat_book_route_context_cache_ttl_seconds: int = 300
    llm_orchestration_history_messages: int = 12
    llm_orchestration_history_chars: int = 8000
    chat_llm_orchestration_dispatch_handlers: bool = True
    chat_llm_orchestration_prefer_llm_response: bool = True
    chat_llm_orchestration_prefer_llm_suggested_actions: bool = True
    chat_llm_answer_first_guard_enabled: bool = False
    chat_llm_answer_first_guard_model: str = ""
    chat_llm_answer_first_guard_temperature: float = 0.0
    chat_multi_ask_orchestration_enabled: bool = True
    chat_multi_ask_min_items: int = 2
    chat_multi_ask_max_items: int = 6
    chat_multi_ask_decompose_model: str = ""
    chat_multi_ask_compose_model: str = ""
    chat_llm_next_suggestions_enabled: bool = True
    # How long after an in-room dining ticket is raised the guest may still
    # change or cancel it. Past this the kitchen has started, so the bot says it
    # can no longer be updated. Minutes; 0 disables the window (always editable).
    ird_edit_window_minutes: int = 5
    # Read the guest's order from new_bot_ird_guest_orders (the order book)
    # instead of rebuilding it from a field the model re-types every turn.
    # OFF until the book has been proven end-to-end: with it off, nothing
    # about the live order path changes. See irdcartbook.md.
    ird_order_book_enabled: bool = True
    # Books untouched for this long are swept so the ten slots recycle. Every
    # placed order is on record as an external ticket, so nothing is lost.
    ird_order_book_sweep_hours: int = 24
    # JSON list of hotel_codes that must never be shown suggestion chips, e.g.
    # ["sarovar_portico_sriperumbudur"]. Per-tenant opt-OUT: every hotel not
    # listed keeps chips exactly as before.
    chat_suggestion_chips_disabled_hotels_json: str = ""
    # JSON list of hotel_codes whose in-chat service forms carry a "Cancel
    # request" button, e.g. ["sarovar_portico_sriperumbudur"]. Per-tenant
    # opt-IN: a hotel that is not listed keeps the form exactly as it was
    # (submit-or-nothing), so this can never change another tenant's flow.
    chat_form_cancel_enabled_hotels_json: str = ""
    # JSON list of hotel_codes whose room-booking turn offers ONLY the team
    # button, e.g. ["mango_grove_chandigarh"] — no "Book on website" link, the
    # button reads "Book Now", and the form's acknowledgement says the team will
    # confirm the booking. For a property with no real booking engine. Per-tenant
    # opt-IN: a hotel that is not listed keeps both buttons exactly as before.
    chat_booking_team_only_hotels_json: str = ""
    # JSON list of hotel_codes where a suggestion-chip PRE-COOK is forbidden from
    # filing a ticket. The prefetch service replays every chip the bot wrote
    # through the real pipeline on a synthetic `prefetch::` session so a click can
    # be served from cache; that replay was filing real tickets for chips nobody
    # clicked. Opt-IN per tenant: a hotel not listed here keeps the old behaviour
    # exactly, phantoms included.
    # This gate gets read TWICE, and both readers must agree — suppressing the
    # ticket without also dropping the cached answer is WORSE than doing neither,
    # because the guest then clicks the chip, is told the request is with the
    # team, and no ticket exists anywhere. Never enable one half alone.
    chat_prefetch_ticket_guard_hotels_json: str = '["mango_grove_chandigarh"]'
    # JSON list of hotel_codes whose final reply is run through the beautifier's
    # LANGUAGE-ENFORCEMENT pass, e.g. ["sarovar_portico_sriperumbudur"].
    #
    # The router already decides the visible language per turn, but that verdict
    # is only ADVISORY: the answering model writes the reply and can quietly
    # keep the language of an earlier turn. This pass is what makes the router's
    # verdict binding. It was hardcoded to Sarovar, so Mango's guests were asking
    # in English and being answered in Hindi (and vice versa) with nothing to
    # catch it.
    #
    # Per-tenant opt-IN, and Sarovar is the DEFAULT so its behaviour cannot be
    # lost by leaving the env unset. A hotel not listed skips the pass entirely -
    # no extra LLM call, no latency, no change of any kind.
    chat_language_enforcement_hotels_json: str = '["sarovar_portico_sriperumbudur"]'
    # Per-hotel CTA buttons shown under the opening bot message (and under the
    # greeting of a fresh conversation in the same session), e.g.
    # {"mango_grove_chandigarh": ["Enquire about room booking",
    #                             "Enquire about banquet booking"]}
    # Tapping one sends that text, so the router hands it to the matching
    # service agent. A hotel that is not listed gets NO buttons — unchanged.
    chat_welcome_service_ctas_json: str = ""

    # ── OTP delivery ────────────────────────────────────────────────────────
    # "log" (default) writes the code to the application log instead of sending
    # it, so the whole verification flow can be exercised before any account
    # exists. "auto" routes email->smtp and phone->sms. See otp_delivery.py.
    otp_delivery_provider: str = "log"
    otp_smtp_host: str = ""
    otp_smtp_port: int = 587
    otp_smtp_user: str = ""
    otp_smtp_password: str = ""
    otp_smtp_from: str = ""
    otp_sms_endpoint: str = ""
    otp_sms_api_key: str = ""
    otp_sms_sender_id: str = ""
    # Per-hotel map of service identifiers whose replies must carry NO photos,
    # e.g. {"mango_grove_chandigarh": ["room_booking"]}. That hotel supplied no
    # guest-room photos at all — only facade, restaurant, bar, conference and
    # banquet — so a room answer could never attach a room, and the one-word
    # title matcher filled the slot with whatever else shared a word ("mini bar"
    # pulling up the bar counter). Muting the lane is honest: no picture beats
    # the wrong picture. DELETE the service from this list the moment real room
    # photos are uploaded, and pictures come back on their own with no code
    # change. Per-tenant opt-IN: a hotel that is not listed is untouched.
    chat_images_muted_services_json: str = ""
    chat_require_strict_confirmation_phrase: bool = False
    chat_confirmation_phrase: str = "yes confirm"
    chat_llm_generic_kb_ticketing_enabled: bool = True
    kb_indexing_enabled: bool = False
    chat_kb_only_mode: bool = False
    chat_phase_gate_llm_only: bool = True
    chat_llm_response_surface_enabled: bool = False
    chat_llm_response_surface_model: str = ""
    chat_llm_response_surface_temperature: float = 0.35
    chat_llm_response_surface_max_tokens: int = 420
    chat_llm_response_surface_rewrite_replacements: bool = True
    chat_llm_response_surface_rewrite_llm_outputs: bool = False
    chat_display_beautifier_enabled: bool = True
    chat_display_beautifier_use_llm: bool = True
    chat_display_beautifier_model: str = ""
    chat_summary_model: str = ""
    chat_display_beautifier_temperature: float = 0.2
    chat_display_beautifier_max_tokens: int = 420
    chat_display_beautifier_min_similarity: float = 0.25
    chat_display_beautifier_timeout_ms: int = 900
    # Live web search for neighbourhood questions the KB cannot answer
    # ("is there a vet nearby", "where can I eat"). Off unless enabled: it is a
    # billed call and adds a few seconds, so it is opt-in per deployment.
    # OFF: the public OpenStreetMap geocoder rate-limits us (429) and each
    # rejection costs ~3s, so distances cost 20s+ and returned nothing. Turn on
    # only with a geocoder we control (self-hosted Nominatim or a paid one).
    # Google Maps: nearby discovery (places:searchText) + real road distance
    # (computeRouteMatrix). Replaces web-search discovery, which was
    # inconsistent, and OSM geocoding, which was rate-limited to 429s.
    google_maps_api_key: str = ""
    chat_google_places_enabled: bool = False
    chat_geo_distance_enabled: bool = False
    chat_web_lookup_enabled: bool = False
    chat_web_lookup_model: str = "gpt-4.1-mini"
    chat_smart_images_enabled: bool = True
    chat_smart_images_attach_timeout_ms: int = 1800
    chat_smart_images_attach_hard_cap_ms: int = 3000
    # Default True: strict verbatim-title match — fast (~50ms), no LLM call.
    # LLM-mode (False) gives better semantic matches but the 1.8s timeout
    # budget isn't enough — every attach times out and no images appear.
    # If image quality on compare turns becomes a priority later, raise
    # `chat_smart_images_attach_timeout_ms` + `chat_smart_images_attach_hard_cap_ms`
    # first, then flip this to False.
    chat_smart_images_fast_title_match_only: bool = True
    chat_smart_images_min_match_confidence: float = 0.74
    # Cold-start fallback: when the orchestrator hasn't yet emitted a FOCUS
    # line (first turn), scan the bot's reply text for a single property name
    # mention and scope images to it. Avoids the cold-start image leak where
    # a fresh session attached an unrelated property's image.
    # Revert via CHAT_SMART_IMAGES_TEXT_SCOPE_FALLBACK=false in .env.
    chat_smart_images_text_scope_fallback: bool = True
    chat_live_chip_budget_ms: int = 1500
    chat_smart_images_max_candidates: int = 18
    # When True, freeform textarea form-prefill (Issue Details, etc.) receives
    # a rolled-up excerpt of recent conversation turns so the LLM can infer
    # a value even when the triggering user message is too thin ("yes please").
    # Revert via CHAT_FORM_PREFILL_USE_HISTORY=false in .env.
    chat_form_prefill_use_history: bool = True
    # When True, prefetch-cache-hit turns that carry form_fields will RE-RUN
    # apply_prefill against the current session's context. This prevents stale
    # `default_value` strings from a different session (e.g. an earlier guest's
    # cached form payload) from leaking into the current turn's form. The
    # display-message + smart-images paths remain skipped for latency.
    # Revert via CHAT_PREFETCH_REPREFILL_FORM_FIELDS=false in .env.
    chat_prefetch_reprefill_form_fields: bool = True
    chat_suggestions_llm_model: str = ""
    chat_suggestions_llm_max_tokens: int = 160
    chat_suggestions_timeout_ms: int = 1800
    chat_post_response_chip_wait_ms: int = 2200
    chat_suggestions_fast_path_enabled: bool = True
    chat_chip_speculation_click_wait_ms: int = 8000
    chat_suggestions_max_kb_chars: int = 14000
    chat_suggestions_max_service_kb_chars: int = 8000
    smart_image_kb_caption_enabled: bool = False
    smart_image_kb_caption_model: str = ""
    smart_image_kb_caption_max_tokens: int = 420
    smart_image_kb_context_max_chars: int = 14000
    qdrant_url: str = ""
    qdrant_api_key: str = ""
    qdrant_collection: str = "kepsla_kb_chunks"
    qdrant_vector_size: int = 1536

    # Context Settings
    max_conversation_history: int = 20
    context_window_tokens: int = 8000
    session_ttl_hours: int = 24
    # Production / deployment defaults: the bot reads AND writes through the
    # database. Local JSON config files are a development convenience only —
    # in production they may not exist or may be on a read-only filesystem.
    # Leave these True unless you're explicitly working on a single-machine
    # local-only setup.
    chat_db_first_mode: bool = True
    chat_db_allow_inmemory_fallback: bool = True
    chat_db_overlay_runtime_config: bool = True
    chat_db_prefer_kb_content: bool = True
    # When True, _save_json_config / file writes for runtime config are
    # skipped silently. The DB is the only writable source of truth.
    chat_skip_json_persist: bool = True
    conversation_local_store_enabled: bool = False
    chat_test_phase_profile_auto_apply: bool = True
    chat_test_phase_profiles_json: str = ""
    chat_suggestion_prefetch_enabled: bool = True
    chat_suggestion_prefetch_count: int = 3
    chat_suggestion_prefetch_ttl_seconds: int = 300
    chat_suggestion_prefetch_parallelism: int = 6
    chat_suggestion_prefetch_similarity_threshold: float = 0.90
    chat_suggestion_prefetch_max_batches_per_session: int = 6
    chat_suggestion_prefetch_model: str = ""
    chat_suggestion_prefetch_temperature: float = 0.2
    chat_suggestion_prefetch_max_tokens: int = 420
    chat_suggestion_prefetch_max_kb_chars: int = 12000
    chat_suggestion_prefetch_wait_ms: int = 220
    conversation_local_store_file: str = Field(
        default_factory=lambda: (
            "/tmp/kepsla_local_contexts.json"
            if os.name != "nt"
            else "./data/runtime/local_contexts.json"
        )
    )

    # Confidence Thresholds
    intent_confidence_threshold: float = 0.7
    response_confidence_threshold: float = 0.6
    escalation_threshold: float = 0.4

    # WhatsApp
    whatsapp_api_url: str = ""
    whatsapp_access_token: str = ""
    whatsapp_phone_number_id: str = ""

    # S3 media uploads (Content Manager image upload → S3)
    # When s3_bucket is empty the upload endpoint is disabled and the UI keeps
    # paste-URL-only behavior. Credentials fall back to the standard AWS
    # environment / instance-profile chain when left blank.
    s3_bucket: str = ""
    s3_region: str = "us-east-1"
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    # Folder prefix inside the bucket for Content Manager uploads.
    s3_uploads_prefix: str = "hotel-images"
    # Base URL used to build the public image_url stored in the DB. Leave empty
    # to derive the direct S3 URL (https://{bucket}.s3.{region}.amazonaws.com).
    # Set to a CloudFront / custom-domain origin (no trailing slash) to serve
    # through a CDN instead. The public/private (signed-URL) decision is
    # deferred — only this prefix changes when that's settled.
    s3_public_url_prefix: str = ""
    s3_max_upload_mb: float = 10.0
    s3_allowed_mime_types: str = "image/png,image/jpeg,image/webp"
    # Private bucket: uploaded objects are stored as s3://bucket/key in the DB
    # and a time-limited presigned GET URL is minted on every read (admin list
    # + chat surfacing). SigV4 caps the lifetime at 7 days (604800s).
    s3_presign_expiry_seconds: int = 604800

    # External ticketing/handoff integrations (Lumira-compatible)
    ticketing_base_url: str = ""
    ticketing_create_path: str = "/insert/ticket.htm"
    ticketing_update_path_template: str = "/insert/ticket/{ticket_id}.htm"
    # A ticket that is not raised is a guest waiting for something nobody is
    # bringing, so the client is tuned to survive a slow spell rather than to
    # fail fast. Measured 2026-09-02: the API's healthy response is 2.8-6.8s
    # (mean 4.0s), so a 10s ceiling left barely 2.5x headroom - when it
    # degraded past 10s for ~20 minutes, all 7 requests in that window were
    # lost permanently because nothing retried them.
    # A small judge that decides, in parallel with the answer, whether this
    # message is something a hotel assistant may answer at all. Code enforces
    # its verdict. Off restores the old prompt-only behaviour.
    chat_scope_judge_enabled: bool = True
    # 20s, not 30: the guest waits on this inside their turn. Healthy responses
    # measured 2.8-6.8s and the failures that lost six tickets were drifting at
    # 11-12s, so 20s catches those with margin while capping what a genuinely
    # hung API costs the person watching the typing dots.
    ticketing_timeout_seconds: float = 20.0
    # Total attempts per request, including the first. 1 restores the old
    # no-retry behaviour.
    ticketing_max_attempts: int = 3
    # First backoff pause; doubles each attempt, with jitter.
    ticketing_retry_backoff_seconds: float = 1.5
    # Hard ceiling on ALL attempts combined. A guest is waiting on this turn, so
    # retrying must never turn a slow API into a minute of silence: once the
    # budget is spent the request fails and the caller falls back.
    ticketing_total_budget_seconds: float = 45.0
    # Connecting is either fast or never - a dead host should not burn the whole
    # read timeout before the next attempt.
    ticketing_connect_timeout_seconds: float = 5.0
    ticketing_local_mode: bool = False
    ticketing_api_failure_local_fallback_enabled: bool = False
    ticketing_local_store_file: str = Field(
        default_factory=lambda: (
            "/tmp/local_tickets.json"
            if os.name != "nt"
            else "./data/ticketing/local_tickets.json"
        )
    )
    ticketing_local_csv_file: str = ""
    agent_handoff_api_url: str = ""
    agent_handoff_api_url_gj: str = ""
    # Per-property entity (organisation_id) routing for multi-property tenants.
    # The Kepsla ticket API expects the FMS entity id in `organisation_id`, and
    # resolves it to the org (RI id) the dashboard filters by. Multi-property
    # tenants (e.g. Zone: one hotel_code, many properties) need a *different*
    # FMS id per property, which the single hotel-wide lumira_entity_id cannot
    # express. Map: { hotel_code: { property_match: fms_entity_id } } where
    # property_match is a lowercase substring tested against the selected
    # property name. Example:
    #   {"zone": {"kolkata": "6290", "jaipur": "6295"}}
    ticketing_property_entity_map_json: str = ""
    # Single-property tenants opt in here. The entity map above resolves by
    # matching a property NAME, which a multi-property tenant always has (the
    # guest picked one). A single-property hotel has no picker, so that name is
    # often absent and nothing matches — and because the hotel now HAS an entity
    # map, the wrong-tenant guard turns the ticket LOCAL-ONLY instead of sending
    # it. For a hotel listed here, a map that resolves to exactly ONE entity is
    # unambiguous, so that entity is used when no name matched. Opt-in by
    # hotel_code so multi-property tenants keep strict name matching.
    #   ["mango_grove_chandigarh"]
    ticketing_single_entity_hotels_json: str = ""
    # Closed-set department allowlist for ticket assignment, per hotel.
    # Shape: { hotel_code: [department_name, ...] }. When set for a hotel, the
    # bot may assign a ticket ONLY to a department whose name is in this list
    # (matched case/punctuation-insensitively) — every other department is
    # filtered out of the candidate list before the LLM/heuristic picks, even if
    # it has a manager configured. Absent hotel_code => no restriction (default).
    #   {"zone": ["Front Office","Guest Relations","Book a stay","Book a venue","MICE/ corporate bookings","Call us/contact us/request a call"]}
    ticketing_allowed_departments_json: str = ""
    # Per-property booking website URLs for the "Book on website" button. Same
    # shape/matching as the entity map: { hotel_code: { property_match: url } },
    # lowercase substring matched against the selected property name/slug,
    # longest match wins. When set, the booking CTA points at the real per-
    # property page instead of a placeholder.
    chat_property_booking_url_map_json: str = ""
    # Live rates for team-only hotels: { hotel_code: url } where the url carries
    # {arr} / {dep} (dd/mm/yyyy) for the hotel's own booking engine search. A
    # room price/availability question then gets one "Check live rates" button
    # that opens that search with the guest's dates. Unset = feature off.
    chat_live_rates_url_by_hotel_json: str = ""
    # When False (default), a KB reindex/upload does NOT auto-regenerate service
    # system prompts — they regenerate ONLY when a service is explicitly edited/
    # saved in admin. Prevents reindex from clobbering hand-tuned service prompts.
    reindex_regenerate_service_prompts_enabled: bool = False
    # Enable ONLY the live department lookup (GHN DEPARTMENT_MASTER via the
    # FMS->RI mapping) so the LLM can assign a department to each ticket and it
    # shows on the dashboard. Scoped flag — does NOT enable the other Lumira DB
    # queries (guest profile / agent handoff / ticket dedup). Requires the app
    # DB to be the GHN MySQL with access to GHN_FEEDBACK_RESULTS + GHN_PROD_BAK.
    lumira_departments_enabled: bool = False
    # Whether to override the LLM-assigned priority and force pre-booking (lead)
    # tickets to LOW. Default False: the LLM decides priority per case (an upset
    # guest / urgent blocker can be HIGH even pre-arrival). Set True only if you
    # want every pre-booking ticket pinned to LOW regardless of what the LLM
    # judged. CRITICAL is never downgraded either way.
    ticketing_prebooking_force_low_priority: bool = False
    # Room/stay bookings are completed on the hotel website, not via an in-chat
    # lead/contact form. When True (default), a router decision that would open
    # the room-booking service's form is redirected to the book answerer so it
    # surfaces the "Book on website" button instead. Set False to restore the
    # old in-chat room-booking lead form. Only the room-booking service is
    # affected — support / venue / spa / dining intakes keep their forms.
    ticketing_room_booking_website_only: bool = True
    # When True, an explicit ROOM booking intent (the room agent surfaces the
    # website link) ALSO creates a SILENT backend ticket so staff know the guest
    # wants to book — no form, the guest-visible reply is unchanged. Only fires on
    # a genuine room booking (never on info questions or non-room "book" requests
    # like shuttle/parking/spa). One ticket per conversation (deduped).
    ticketing_room_booking_silent_ticket_enabled: bool = True
    # Push the SAME one-line description shown on the KePSLA ticket dashboard
    # to the external ticketing API. When enabled, the KePSLA LLM summary is
    # generated up-front (once) and used as the ticket `issue` we send, so the
    # external dashboard and the KePSLA page show identical descriptions
    # instead of generic "<service> request" boilerplate. The same summary is
    # written to new_bot_tickets.description (no second LLM call). Falls back to
    # the original issue text when the LLM is unavailable or this is disabled.
    ticketing_kepsla_description_to_api_enabled: bool = True
    # Send the (sanitized, bounded) conversation transcript of the SINGLE chat
    # thread that raised the ticket as the external API `message`, so the Kepsla
    # "API page" shows the actual messages — not just the one-line summary. The
    # transcript is scoped to the active conversation_uid and heavily sanitized
    # (markdown/emoji/`<<...>>`/control chars stripped) to avoid the id:0
    # rejection that the old raw-transcript approach caused. Set false to revert
    # to summary-only.
    ticketing_external_transcript_enabled: bool = True
    # Hard cap on the transcript length pushed to the external API. Kept modest
    # so an unusually long thread can never balloon the payload past what the
    # external dashboard accepts.
    ticketing_external_transcript_max_chars: int = 4000
    # Mirror chat turns / form submissions into KEBO_ENGAGE_SESSIONS so the
    # Kepsla dashboard can show the guest name + conversation against a ticket.
    # (The Lumira-compat path already does this; this also covers /api/chat.)
    engage_session_logging_enabled: bool = True
    # Store the guest's identity in GHN_CHAT_BOT_CUSTOMER_DETAILS on form submit
    # (keyed by Session_Id) — the table the dashboard / Lumira read the guest
    # name from. Processing_Status is set to PROCESSED so no email is triggered.
    # Bridge kebo-dashboard agent replies (written to KEBO_ENGAGE_SESSIONS) to
    # our guest widget over /ws/chat, and mute the bot while a kebo agent is
    # handling the session. Set False to disable the KES poller.
    kebo_agent_bridge_enabled: bool = True
    customer_details_logging_enabled: bool = True
    # Real working rows use Form_Type='personal_information' (verified in DB).
    customer_details_form_type: str = "personal_information"
    # Real Kepsla widget_id/group_id per hotel — the dashboard scopes the
    # guest-name lookup by these, so a null widget/group row is dropped.
    # Verified: Zone = Sarovar group, widget 53 / group 244.
    #   {"zone": {"widget_id": "53", "group_id": "244"}}
    ticketing_widget_group_map_json: str = ""
    # Row lifecycle state. The closed Java pipeline inserts these as 'PENDING'
    # and a job processes them. We default to 'PROCESSED' to avoid triggering
    # that job's email — but if the dashboard only surfaces rows the pipeline
    # touched, flip this to 'PENDING' (emails only fire if the form itself has
    # notification emails configured, which our ad-hoc rows do not).
    customer_details_processing_status: str = "PROCESSED"
    ticketing_smart_routing_enabled: bool = True
    ticketing_smart_routing_use_llm: bool = True
    ticketing_router_model: str = "gpt-4o-mini"
    ticketing_router_ack_similarity: float = 0.88
    ticketing_router_update_similarity: float = 0.55
    ticketing_enrichment_enabled: bool = True
    ticketing_auto_create_on_actionable: bool = True
    ticketing_plugin_enabled: bool = True
    ticketing_plugin_takeover_mode: bool = False
    ticketing_agent_llm_only: bool = True
    ticketing_case_match_use_llm: bool = True
    ticketing_case_match_model: str = "gpt-4o-mini"
    ticketing_case_match_fallback_enabled: bool = False
    ticketing_case_match_context_chars: int = 5000
    ticketing_subcategory_llm_enabled: bool = False
    ticketing_subcategory_model: str = "gpt-4o-mini"
    ticketing_stale_reconfirm_enabled: bool = False
    ticketing_stale_reconfirm_minutes: int = 30
    ticketing_guest_preferences_enabled: bool = False
    ticketing_guest_preferences_use_llm: bool = False
    ticketing_guest_preferences_model: str = "gpt-4o-mini"
    ticketing_identity_gate_enabled: bool = False
    ticketing_identity_gate_prebooking_only: bool = True
    ticketing_identity_require_name: bool = True
    ticketing_identity_require_phone: bool = True
    ticketing_update_window_minutes: int = 2
    ticketing_update_window_llm_assessment_enabled: bool = True
    ticketing_debug_log_enabled: bool = True
    ticketing_debug_log_file: str = "./logs/ticketing_debug.jsonl"

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def is_development(self) -> bool:
        return self.app_env == "development"

    @field_validator("debug", mode="before")
    @classmethod
    def _coerce_debug_flag(cls, value):
        """Accept deployment-style strings like DEBUG=release without crashing startup."""
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value or "").strip().lower()
        if text in {"1", "true", "yes", "y", "on", "debug", "dev", "development"}:
            return True
        if text in {"0", "false", "no", "n", "off", "release", "prod", "production"}:
            return False
        return value


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()


settings = get_settings()
