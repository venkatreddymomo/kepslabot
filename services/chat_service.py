"""
Chat Service

Main orchestrator for handling chat messages.
Uses handler registry for structured intents, falls back to LLM for unhandled ones.
Now uses config_service for business configuration (synced with Admin Portal).
"""

import asyncio
import hashlib
import json
import logging
import re
import time
from datetime import UTC, date, datetime, timedelta
from difflib import SequenceMatcher
from typing import Any, Optional

from schemas.chat import (
    ChatRequest,
    ChatResponse,
    ConversationContext,
    ConversationState,
    IntentType,
    IntentResult,
    MessageRole,
)
from schemas.orchestration import OrchestrationDecision
from core.complexity_router import ProcessingPath, complexity_router
from core.context_manager import context_manager
from core.state_machine import state_machine
from llm.client import llm_client
from agents.complex_query_orchestrator import complex_query_orchestrator
from config.settings import settings
from services.booking_assistance_state import team_booking_enabled, details_submitted as booking_details_submitted
from services.config_service import config_service
from services.db_config_service import db_config_service
from services.conversation_memory_service import conversation_memory_service
from services import conversation_cache_service
from services.agent_plugin_service import agent_plugin_service
from services.response_validator import response_validator
from services.response_beautifier_service import response_beautifier_service
from services.ticketing_service import ticketing_service
from services.llm_orchestration_service import llm_orchestration_service
from services.orchestration_policy_service import orchestration_policy_service
from services.step_trace_service import step_trace_service
from services.prompt_registry_service import (
    PromptMissingError,
    prompt_registry,
)
from services.turn_diagnostics_service import turn_diagnostics_service
from services.backend_trace_service import backend_trace_service
from services.everything_trace_service import everything_trace_service
from integrations.lumira_ticketing_repository import lumira_ticketing_repository
from handlers import handler_registry
from handlers.base_handler import HandlerResult

logger = logging.getLogger(__name__)


def _script_mismatch_language_contract(
    *, user_message: str, reply_text: str
) -> dict[str, Any] | None:
    """A language contract derived from a VISIBLE script mismatch, or None.

    The router is supposed to name the turn's language, but it sometimes emits
    no `response_language` field at all. There is then no contract, the
    enforcement pass below is skipped, and whatever language the answer model
    drifted into is shipped. Caught live on 2026-09-28: after a Punjabi turn,
    "What time is check-in?" came back in Gurmukhi because that turn's router
    output simply had no language field on it.

    So when the router says nothing, fall back to what is objectively visible -
    the guest typed one script and the draft replies in another. Pure regex, no
    LLM, and it returns None unless the two genuinely disagree, so a turn that
    was already consistent is untouched. It cannot resolve ROMANIZED Hindi or
    Punjabi (Latin script by definition); that stays the router's job.
    """
    guest = llm_orchestration_service._script_language_hint(str(user_message or ""))
    reply = llm_orchestration_service._script_language_hint(str(reply_text or ""))
    guest_language = str(guest.get("language") or "")
    reply_language = str(reply.get("language") or "")
    if guest_language == reply_language:
        return None
    if guest_language:
        # The guest wrote a native script; answer in that language.
        return {
            "language": guest_language,
            "script": str(guest.get("script") or ""),
            "style": "native",
            "confidence": 0.6,
            "source": "script_mismatch_fallback",
        }
    # Latin letters do not identify a language. Leave this to the language router.
    return None


_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF"
    "\U0000FE0F\U0000200D\U000020E3\U0001F1E6-\U0001F1FF][ \t]?"
)


def _strip_emoji(text: Any) -> str:
    """Remove emoji (and the space after one) for hotels set to 'no emoji'.
    Leaves bullets, currency and dashes alone - they sit below U+2600."""
    cleaned = _EMOJI_RE.sub("", str(text or ""))
    return "\n".join(line.rstrip() for line in cleaned.split("\n"))


def _language_enforcement_for_hotel(hotel_code: Any) -> bool:
    """True when this hotel's reply is forced into the router's chosen language.

    Opt-IN list from settings.chat_language_enforcement_hotels_json, which
    DEFAULTS to Sarovar so the behaviour it already had survives an unset env.
    A hotel that is not named there skips the pass entirely — no extra LLM
    call and no added latency — so this can never change another tenant's turn.
    """
    code = str(hotel_code or "").strip().lower()
    if not code:
        return False
    from services import prebooking_settings_service

    admin = prebooking_settings_service.settings_for(code)
    if admin is not None:
        return bool(admin["reply_style"].get("enforce_language", False))
    raw = str(getattr(settings, "chat_language_enforcement_hotels_json", "") or "").strip()
    # EMPTY LIST MEANS EVERY HOTEL. A guest being answered in the language they
    # wrote in is not a per-tenant feature, it is what the bot is supposed to do,
    # so onboarding a hotel should need its KB and prompts and nothing else. The
    # list stays only as a narrowing escape hatch: name hotels in it and just
    # those run the pass. Costs nothing on an ordinary English turn - the pass
    # stands itself down when there is nothing to translate.
    if not raw or raw in ("[]", "null"):
        return True
    try:
        parsed = json.loads(raw)
    except Exception:
        # Malformed config must not silently answer guests in the wrong
        # language; fall back to the correct behaviour, not to nothing.
        logger.warning("chat_language_enforcement_hotels_json is not valid JSON; enforcing for all hotels")
        return True
    if not isinstance(parsed, list):
        return True
    if not parsed:
        return True
    return code in {str(entry or "").strip().lower() for entry in parsed}


# Shared with the ChatService domain mixins — housed in chat_service_shared
# to avoid an import cycle (the mixins import them from there, not from here).
from services.chat_service_shared import (
    CapabilityCheck,
    _coerce_optional_int,
    _PHASE_INTENT_SERVICE_MARKERS,
)


# CapabilityCheck moved to services/chat_service_shared.py (imported below).


# Map pending_action values to the IntentType whose handler should process
# contextual follow-ups.
_PENDING_ACTION_TO_INTENT = {
    "confirm_order": IntentType.ORDER_FOOD,
    "confirm_booking": IntentType.TABLE_BOOKING,
    "confirm_room_booking": IntentType.TABLE_BOOKING,
    "confirm_room_availability_check": IntentType.TABLE_BOOKING,
    "escalate_complaint": IntentType.COMPLAINT,
    "collect_ticket_room_number": IntentType.COMPLAINT,
    "collect_ticket_issue_details": IntentType.COMPLAINT,
    "collect_ticket_identity_details": IntentType.COMPLAINT,
    "confirm_ticket_creation": IntentType.COMPLAINT,
    "collect_ticket_update_note": IntentType.COMPLAINT,
    "confirm_ticket_escalation": IntentType.COMPLAINT,
    "show_menu": IntentType.FAQ,
    "select_service": IntentType.TABLE_BOOKING,
    "select_restaurant": IntentType.TABLE_BOOKING,
    "collect_booking_party_size": IntentType.TABLE_BOOKING,
    "collect_booking_time": IntentType.TABLE_BOOKING,
    "collect_room_booking_details": IntentType.TABLE_BOOKING,
    "select_room_type": IntentType.TABLE_BOOKING,
    "awaiting_request_detail": IntentType.ROOM_SERVICE,
    "awaiting_room_number": IntentType.ROOM_SERVICE,
    "collect_service_details": IntentType.FAQ,
}

# Detail-collection actions should continue deterministically even when
# the LLM classifies a short reply with low confidence.
_DETAIL_COLLECTION_PENDING_ACTIONS = {
    "select_service",
    "select_restaurant",
    "collect_booking_party_size",
    "collect_booking_time",
    "collect_room_booking_details",
    "select_room_type",
    "collect_ticket_room_number",
    "collect_ticket_issue_details",
    "collect_ticket_identity_details",
    "collect_ticket_update_note",
    "awaiting_request_detail",
    "awaiting_room_number",
    "collect_transport_details",
    "collect_service_details",
}


_INTERNAL_TASK_STATE_KEY = "_task_state"
_MAX_PARKED_TASKS = 5

_RETURN_TO_BOT_MARKERS = {
    "return to bot",
    "back to bot",
    "resume bot",
    "switch to bot",
    "continue with bot",
}

_RESUME_TASK_MARKERS = {
    "resume",
    "continue",
    "continue pending",
    "resume pending",
    "resume task",
    "continue task",
    "yes resume",
    "resume previous",
}

_CANCEL_TASK_MARKERS = {
    "cancel pending",
    "cancel task",
    "drop task",
    "cancel previous",
    "skip pending",
}

_INFORMATION_QUERY_PREFIXES = (
    "what",
    "when",
    "where",
    "which",
    "who",
    "how",
    "is",
    "are",
    "do",
    "can",
)

_INFORMATION_QUERY_IMPERATIVE_PREFIXES = (
    "tell me",
    "give me",
    "show me",
    "share",
    "explain",
    "describe",
)

_GENERIC_SERVICE_ALIASES = {
    "service",
    "services",
    "booking",
    "order",
    "support",
}

_BROAD_FIRST_ALIAS_TOKENS = {
    "hotel",
    "room",
    "service",
    "services",
    "support",
    "booking",
    "request",
    "enquiry",
    "sales",
    "status",
    "modification",
    "coordination",
    "help",
}

_SERVICE_DESCRIPTION_STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "this",
    "that",
    "your",
    "our",
    "can",
    "you",
    "are",
    "is",
    "of",
    "to",
    "in",
    "on",
    "at",
    "by",
    "or",
    "service",
    "services",
    "support",
    "assistance",
    "help",
}

# _PHASE_INTENT_SERVICE_MARKERS moved to services/chat_service_shared.py
# (imported at the top of this module; also imported by the phase mixin).



from services.chat_service_ticketing_mixin import ChatServiceTicketingMixin


from services.chat_service_booking_mixin import ChatServiceBookingMixin


from services.chat_service_phase_mixin import ChatServicePhaseMixin


from services.chat_service_ordermenu_mixin import ChatServiceOrderMenuMixin


from services.chat_service_response_mixin import ChatServiceResponseMixin


class ChatService(ChatServiceTicketingMixin, ChatServiceBookingMixin, ChatServicePhaseMixin, ChatServiceOrderMenuMixin, ChatServiceResponseMixin):
    """Handles chat message processing."""

    async def process_message(
        self,
        request: ChatRequest,
        db_session=None,
        prefetched_services: list[dict[str, Any]] | None = None,
        defer_summary_refresh: bool = False,
    ) -> ChatResponse:
        """Process an incoming chat message and generate response.

        Pushes the request's hotel_code into the db_config contextvar for the
        duration of this call so every downstream lookup (config files, DB
        service catalog, capability summary) is scoped to the correct hotel —
        regardless of whether the calling transport (HTTP, WebSocket, Lumira
        compat shim, etc.) remembered to push it. This makes the context
        bug structurally impossible to recur even if a new transport is
        added that forgets the explicit push.
        """
        _scope_hotel_code = str(getattr(request, "hotel_code", "") or "").strip()
        _scope_token = (
            db_config_service.set_hotel_context(_scope_hotel_code)
            if _scope_hotel_code
            else None
        )
        # Admin-managed pre-booking settings are read synchronously further
        # down; make sure this hotel's copy is current before the turn starts.
        try:
            from services import prebooking_settings_service

            await prebooking_settings_service.refresh_if_stale(_scope_hotel_code)
        except Exception as _settings_exc:
            logger.warning("pre-booking settings refresh failed (old copy kept): %r", _settings_exc)
        try:
            return await self._process_message_with_scope(
                request,
                db_session=db_session,
                prefetched_services=prefetched_services,
                defer_summary_refresh=defer_summary_refresh,
            )
        finally:
            if _scope_token is not None:
                db_config_service.reset_hotel_context(_scope_token)


    async def _maybe_handle_parked_task_command(
        self,
        *,
        effective_user_message: str,
        raw_user_message: str,
        context: "ConversationContext",
        capabilities_summary: "dict[str, Any]",
        request: "ChatRequest",
        db_session=None,
    ) -> "ChatResponse | None":
        """Handle an explicit resume/cancel command for a parked task.

        Returns a ChatResponse when the guest asked to resume or cancel the
        parked task; returns None to continue (e.g. auto-resume evaluation).
        """
        if self._is_resume_task_request(effective_user_message) or self._is_resume_task_request(raw_user_message):
            resumed_task = self._restore_parked_task(context)
            resumed_summary = str((resumed_task or {}).get("summary") or "your previous request").strip()
            response_text = (
                f"Resuming {resumed_summary}. "
                "Please continue with the remaining details."
            )
            response_text, _ = await self._maybe_llm_rewrite_response(
                response_text=response_text,
                user_message=effective_user_message,
                intent_result=IntentResult(intent=IntentType.FAQ, confidence=0.95, entities={}),
                context=context,
                capabilities_summary=capabilities_summary,
                capability_check_allowed=True,
                capability_reason="",
                response_source="parked_task_resume",
                validator_replaced=False,
            )
            assistant_metadata = {
                "intent": IntentType.FAQ.value,
                "confidence": 0.95,
                "channel": context.channel,
                "parked_task_resumed": True,
                "parked_task_summary": resumed_summary,
            }
            context.add_message(
                MessageRole.ASSISTANT,
                response_text,
                metadata=assistant_metadata,
            )
            conversation_memory_service.capture_assistant_message(
                context,
                response_text,
                metadata=assistant_metadata,
            )
            await self._refresh_summary_async(context)
            await context_manager.save_context(context, db_session=db_session)
            memory_snapshot = conversation_memory_service.get_snapshot(context)
            remaining_parked = self._ensure_internal_task_state(context).get("parked_tasks", [])
            remaining_count = len(remaining_parked) if isinstance(remaining_parked, list) else 0

            return ChatResponse(
                session_id=request.session_id,
                message=response_text,
                intent=IntentType.FAQ,
                confidence=0.95,
                state=context.state,
                suggested_actions=self._build_contextual_suggested_actions(
                    context.state,
                    IntentType.FAQ,
                    context.pending_action,
                    capabilities_summary,
                    context.pending_data,
                ),
                metadata={
                    "message_count": len(context.messages),
                    "routing_path": ProcessingPath.SIMPLE.value,
                    "routing_score": 1.0,
                    "routing_signals": {"path": "parked_task_resume"},
                    "response_source": "parked_task_resume",
                    "parked_task_resumed": True,
                    "parked_task_summary": resumed_summary,
                    "parked_tasks_remaining": remaining_count,
                    "memory_summary": memory_snapshot.get("summary", ""),
                    "memory_facts": memory_snapshot.get("facts", {}),
                },
            )

        if self._is_cancel_task_request(effective_user_message) or self._is_cancel_task_request(raw_user_message):
            canceled_task = self._cancel_parked_task(context)
            canceled_summary = str((canceled_task or {}).get("summary") or "that pending request").strip()
            response_text = (
                f"Cancelled {canceled_summary}. "
                "What would you like to do next?"
            )
            response_text, _ = await self._maybe_llm_rewrite_response(
                response_text=response_text,
                user_message=effective_user_message,
                intent_result=IntentResult(intent=IntentType.FAQ, confidence=0.95, entities={}),
                context=context,
                capabilities_summary=capabilities_summary,
                capability_check_allowed=True,
                capability_reason="",
                response_source="parked_task_cancel",
                validator_replaced=False,
            )
            context.state = ConversationState.IDLE
            assistant_metadata = {
                "intent": IntentType.FAQ.value,
                "confidence": 0.95,
                "channel": context.channel,
                "parked_task_cancelled": True,
                "parked_task_summary": canceled_summary,
            }
            context.add_message(
                MessageRole.ASSISTANT,
                response_text,
                metadata=assistant_metadata,
            )
            conversation_memory_service.capture_assistant_message(
                context,
                response_text,
                metadata=assistant_metadata,
            )
            await self._refresh_summary_async(context)
            await context_manager.save_context(context, db_session=db_session)
            memory_snapshot = conversation_memory_service.get_snapshot(context)
            remaining_parked = self._ensure_internal_task_state(context).get("parked_tasks", [])
            remaining_count = len(remaining_parked) if isinstance(remaining_parked, list) else 0

            return ChatResponse(
                session_id=request.session_id,
                message=response_text,
                intent=IntentType.FAQ,
                confidence=0.95,
                state=ConversationState.IDLE,
                suggested_actions=config_service.get_quick_actions(limit=4),
                metadata={
                    "message_count": len(context.messages),
                    "routing_path": ProcessingPath.SIMPLE.value,
                    "routing_score": 1.0,
                    "routing_signals": {"path": "parked_task_cancel"},
                    "response_source": "parked_task_cancel",
                    "parked_task_cancelled": True,
                    "parked_task_summary": canceled_summary,
                    "parked_tasks_remaining": remaining_count,
                    "memory_summary": memory_snapshot.get("summary", ""),
                    "memory_facts": memory_snapshot.get("facts", {}),
                },
            )

        return None

    async def _process_message_with_scope(
        self,
        request: ChatRequest,
        db_session=None,
        prefetched_services: list[dict[str, Any]] | None = None,
        defer_summary_refresh: bool = False,
    ) -> ChatResponse:
        """Inner body of process_message — runs with hotel context already pushed."""

        # Sub-step latency tracing. Each _pm_record call emits one event
        # immediately rather than buffering until the end — this survives
        # all of process_message's many return paths without needing a
        # try/finally wrap.
        import time as _pm_time
        _pm_session_id = str(getattr(request, "session_id", "") or "")
        _pm_hotel_code = str(getattr(request, "hotel_code", "") or "")
        def _pm_record(label: str, started: float) -> None:
            try:
                step_trace_service.log_event(
                    "chat_service_substep",
                    payload={
                        "duration_ms": round((_pm_time.perf_counter() - started) * 1000.0, 2),
                    },
                    session_id=_pm_session_id,
                    hotel_code=_pm_hotel_code,
                    component="services.chat_service",
                    step=f"chat_service.{label}",
                    stage="completed",
                )
            except Exception:
                pass

        _pm_step_started = _pm_time.perf_counter()

        # 1. Get or create conversation context
        request_channel = (
            request.channel
            or request.metadata.get("channel")
            or None
        )
        resolved_hotel_code = config_service.resolve_hotel_code(request.hotel_code)
        context = await context_manager.get_or_create_context(
            session_id=request.session_id,
            hotel_code=resolved_hotel_code,
            guest_phone=request.guest_phone,
            channel=request_channel,
            db_session=db_session,
        )
        _pm_record("get_or_create_context", _pm_step_started)
        _pm_step_started = _pm_time.perf_counter()
        # Keep existing sessions aligned with canonical tenant mapping.
        if context.hotel_code != resolved_hotel_code:
            context.hotel_code = resolved_hotel_code
        if request_channel:
            context.channel = request_channel
        if not isinstance(context.pending_data, dict):
            context.pending_data = {}
        # Track the rotating per-thread conversation id from the request so new
        # messages get tagged with it and the LLM history is scoped to the CURRENT
        # conversation only (a fresh thread starts clean; old threads never leak).
        _req_meta = request.metadata if isinstance(request.metadata, dict) else {}
        _conv_uid = str(
            _req_meta.get("conversation_id") or _req_meta.get("conversationId") or ""
        ).strip()
        # Stamp this turn's conversation thread id so every DB log stream (routing,
        # backend, llm, tickets) is traceable per conversation, not just messages.
        try:
            from services.db_log_sink import set_conversation_uid as _set_conv_uid_log
            _set_conv_uid_log(_conv_uid)
        except Exception:
            pass
        if _conv_uid:
            # A fresh conversation_id must start clean: wipe scope/memory/flow state
            # from any prior conversation so old threads never leak into the new one.
            _prev_conv_uid = str(
                (context.pending_data or {}).get("_active_conversation_uid") or ""
            ).strip()
            _rotated = conversation_memory_service.reset_pending_for_conversation_rotation(
                context, _conv_uid
            )
            context.pending_data["_active_conversation_uid"] = _conv_uid
            if _rotated:
                logger.info(
                    "[CONV_ROTATE] new conversation_id — wiped prior scope/memory. "
                    "session=%s prev_conv=%s new_conv=%s",
                    request.session_id, _prev_conv_uid or "(none)", _conv_uid,
                )
        else:
            # No conversation_id from the client → the rotation reset cannot fire and
            # scope will accumulate across conversations. This is exactly how a stale
            # widget bundle silently re-introduces cross-conversation leaks. Surface it
            # so it is greppable in prod (the router-side ISOLATION_SAFETYNET still
            # bounds the damage per turn).
            logger.warning(
                "[CONV_ROTATE] request carried NO conversation_id (session=%s) — "
                "per-conversation reset cannot run; check the widget is sending it.",
                request.session_id,
            )
        if defer_summary_refresh:
            # Phase-1 latency optimization: skip on-path summary LLM work and
            # refresh summary asynchronously after response emission.
            context.pending_data["_defer_summary_refresh"] = True
        else:
            context.pending_data.pop("_defer_summary_refresh", None)
        await self._ingest_request_metadata(context, request, db_session=db_session)
        _pm_record("ingest_request_metadata", _pm_step_started)
        _pm_step_started = _pm_time.perf_counter()
        selected_phase_context = self._get_selected_phase_context(
            context=context,
            pending_data=context.pending_data if isinstance(context.pending_data, dict) else {},
            entities={},
        )
        turn_diagnostics_service.update_turn_context(
            phase_id=str(selected_phase_context.get("selected_phase_id") or ""),
            phase_name=str(selected_phase_context.get("selected_phase_name") or ""),
        )
        raw_user_message = str(request.message or "")
        preprocessed_user_message = await self._preprocess_user_message_with_llm(
            raw_user_message,
            selected_phase_id=selected_phase_context.get("selected_phase_id", ""),
            selected_phase_name=selected_phase_context.get("selected_phase_name", ""),
            context=context,
        )
        _pm_record("preprocess_user_message", _pm_step_started)
        _pm_step_started = _pm_time.perf_counter()
        effective_user_message = preprocessed_user_message or raw_user_message
        request_for_processing = (
            request.model_copy(update={"message": effective_user_message})
            if effective_user_message != raw_user_message
            else request
        )
        self._sync_stay_window_from_messages(
            context=context,
            raw_message=raw_user_message,
            processed_message=effective_user_message,
        )
        self._log_processing_stage(
            "context_loaded",
            request=request,
            context=context,
            extra={
                "resolved_hotel_code": resolved_hotel_code,
                "request_channel": str(request_channel or ""),
                "selected_phase_id": str(selected_phase_context.get("selected_phase_id") or ""),
                "selected_phase_name": str(selected_phase_context.get("selected_phase_name") or ""),
                "db_session_available": db_session is not None,
                "message_preprocessed": effective_user_message != raw_user_message,
            },
        )

        # 2. Add user message to history
        user_metadata = {"channel": context.channel}
        if effective_user_message != raw_user_message:
            user_metadata["preprocessed_message"] = effective_user_message
        context.add_message(
            MessageRole.USER,
            raw_user_message,
            metadata=user_metadata,
        )
        conversation_memory_service.capture_user_message(context, raw_user_message)
        await self._refresh_summary_async(context)
        memory_snapshot = conversation_memory_service.get_snapshot(context)
        _pm_record("memory_summary_refresh", _pm_step_started)
        _pm_step_started = _pm_time.perf_counter()

        # 2a. Route complexity (initial hybrid architecture implementation)
        routing_decision = complexity_router.route(effective_user_message, context)
        history_window = self._resolve_history_window(context)

        # 3. Get hotel capabilities from config_service (synced with Admin Portal)
        capabilities_summary = config_service.get_capability_summary(context.hotel_code)
        capabilities_summary = await self._augment_capabilities_from_db(
            capabilities_summary,
            db_session,
            prefetched_services=prefetched_services,
            context=context,
        )
        _pm_record("augment_capabilities", _pm_step_started)
        _pm_step_started = _pm_time.perf_counter()

        self._sync_active_task_snapshot(
            context=context,
            selected_phase_context=selected_phase_context,
        )
        self._log_processing_stage(
            "routing_ready",
            request=request,
            context=context,
            extra={
                "routing_path": str(routing_decision.path.value),
                "routing_score": float(routing_decision.score),
                "history_window": int(history_window),
                "capability_intent_count": len(capabilities_summary.get("intents", [])),
                "service_catalog_count": len(capabilities_summary.get("service_catalog", [])),
                "faq_bank_count": len(capabilities_summary.get("faq_bank", [])),
            },
        )

        no_template_mode = bool(getattr(settings, "chat_no_template_response_mode", False))
        self._log_processing_stage(
            "mode_flags",
            request=request,
            context=context,
            extra={
                "chat_no_template_response_mode": no_template_mode,
                "chat_llm_orchestration_mode": bool(getattr(settings, "chat_llm_orchestration_mode", False)),
                "chat_pure_llm_mode": bool(getattr(settings, "chat_pure_llm_mode", False)),
                "chat_full_kb_llm_mode": bool(getattr(settings, "chat_full_kb_llm_mode", False)),
                "chat_kb_only_mode": bool(getattr(settings, "chat_kb_only_mode", False)),
                "chat_multi_ask_orchestration_enabled": bool(getattr(settings, "chat_multi_ask_orchestration_enabled", True)),
            },
        )
        # Deterministic identity answer ("who are you", "what's your name").
        # Must run BEFORE the no_template / llm-orchestration / multi-ask
        # dispatchers below: those field the message for multi-property
        # (zone-style) tenants and are NOT given the configured bot_name, so
        # identity questions were being deflected generically and never reached
        # the identity shortcut further down (dead code once these modes are on).
        # The _match_identity_response guards (idle state, no pending action)
        # keep this from hijacking an active service/booking flow. We keep the
        # deterministic text verbatim (no LLM surface rewrite) so the bot always
        # states its name exactly.
        # ONLY short-circuit for a genuine bot-name question ("who are you",
        # "what's your name"). _match_identity_response ALSO matches broad
        # business markers like "about ur hotel", which would wrongly steal
        # real KB questions ("what's special about your hotel") and answer them
        # with a terse "This is <hotel>." Those must fall through to the KB
        # answerer, so we deliberately ignore the "business_name" match type here.
        early_identity_match = self._match_identity_response(
            effective_user_message, capabilities_summary, context
        )
        _identity_shortcut_response = await self._maybe_identity_shortcut_response(
            early_identity_match=early_identity_match,
            capabilities_summary=capabilities_summary,
            context=context,
            request=request,
            db_session=db_session,
        )
        if _identity_shortcut_response is not None:
            return _identity_shortcut_response

        if no_template_mode:
            no_template_response = await self._process_policy_verified_llm_message(
                request=request_for_processing,
                context=context,
                capabilities_summary=capabilities_summary,
                routing_decision=routing_decision,
                memory_snapshot=memory_snapshot,
                selected_phase_context=selected_phase_context,
                db_session=db_session,
                prefetched_services=prefetched_services,
            )
            if no_template_response is not None:
                self._log_processing_stage(
                    "mode_return",
                    request=request,
                    context=context,
                    intent_result=IntentResult(intent=getattr(no_template_response, "intent", IntentType.FAQ), confidence=getattr(no_template_response, "confidence", 0.9), entities={}),
                    extra={
                        "mode": "chat_no_template_response_mode",
                        "response_source": str((no_template_response.metadata or {}).get("response_source") or "no_template_mode"),
                    },
                )
                return no_template_response

        parked_task = self._peek_parked_task(context)
        if context.pending_action is None and parked_task is not None:
            _parked_command_response = await self._maybe_handle_parked_task_command(
                effective_user_message=effective_user_message,
                raw_user_message=raw_user_message,
                context=context,
                capabilities_summary=capabilities_summary,
                request=request,
                db_session=db_session,
            )
            if _parked_command_response is not None:
                return _parked_command_response

            if self._should_auto_resume_parked_task(
                message=effective_user_message,
                parked_task=parked_task,
                capabilities_summary=capabilities_summary,
            ):
                self._restore_parked_task(context)
                selected_phase_context = self._get_selected_phase_context(
                    context=context,
                    pending_data=context.pending_data if isinstance(context.pending_data, dict) else {},
                    entities={},
                )
                turn_diagnostics_service.update_turn_context(
                    phase_id=str(selected_phase_context.get("selected_phase_id") or ""),
                    phase_name=str(selected_phase_context.get("selected_phase_name") or ""),
                )
                self._sync_active_task_snapshot(
                    context=context,
                    selected_phase_context=selected_phase_context,
                )

        # NOTE: previously this branch ran `_evaluate_ticketing_status_for_turn`
        # synchronously here, which fires an LLM-driven configured-case match
        # (~2-4s on gpt-4.1-mini) for telemetry-only metadata. The result was
        # never read between this point and the legacy-path re-evaluation at
        # the FAQ/handler branch — orchestration_mode and no_template_mode
        # both return a response before reaching that branch. Calling it here
        # added one full LLM hop to every turn for no observable effect on the
        # bot's reply. The legacy-path call below stays in place for the rare
        # turn that falls through to that handler.

        # 3.5 Service-agent plugin runtime (deterministic, low latency).
        plugin_runtime_enabled = bool(getattr(settings, "agent_plugin_runtime_enabled", False))
        if plugin_runtime_enabled:
            plugin_settings = config_service.get_agent_plugin_settings()
            if bool(plugin_settings.get("enabled", False)):
                plugin_result = agent_plugin_service.handle_message(
                    message=effective_user_message,
                    context=context,
                    channel=context.channel,
                )
                if plugin_result.handled:
                    internal_pending_entries = conversation_memory_service.internal_pending_entries(context.pending_data)
                    internal_pending_entries.pop("_agent_runtime", None)
                    context.state = plugin_result.next_state
                    context.pending_action = plugin_result.pending_action
                    context.pending_data = conversation_memory_service.merge_with_internal(
                        plugin_result.pending_data or {},
                        internal_pending_entries,
                    )
                    response_text = str(plugin_result.response_text or "").strip()
                    response_text, _ = await self._maybe_llm_rewrite_response(
                        response_text=response_text,
                        user_message=effective_user_message,
                        intent_result=IntentResult(intent=IntentType.FAQ, confidence=0.9, entities={}),
                        context=context,
                        capabilities_summary=capabilities_summary,
                        capability_check_allowed=True,
                        capability_reason="",
                        response_source="agent_plugin_runtime",
                        validator_replaced=False,
                    )

                    assistant_metadata = {
                        "intent": IntentType.FAQ.value,
                        "confidence": 0.9,
                        "channel": context.channel,
                        "agent_plugin_handled": True,
                    }
                    if plugin_result.metadata:
                        assistant_metadata.update(plugin_result.metadata)

                    context.add_message(
                        MessageRole.ASSISTANT,
                        response_text,
                        metadata=assistant_metadata,
                    )
                    conversation_memory_service.capture_assistant_message(
                        context,
                        response_text,
                        metadata=assistant_metadata,
                    )
                    await self._refresh_summary_async(context)
                    await context_manager.save_context(context, db_session=db_session)
                    memory_snapshot = conversation_memory_service.get_snapshot(context)
                    self._log_processing_stage(
                        "mode_return",
                        request=request,
                        context=context,
                        intent_result=IntentResult(intent=IntentType.FAQ, confidence=0.9, entities={}),
                        extra={
                            "mode": "agent_plugin_runtime",
                            "response_source": "agent_plugin_runtime",
                            "next_state": str(plugin_result.next_state.value),
                        },
                    )

                    return ChatResponse(
                        session_id=request.session_id,
                        message=response_text,
                        intent=IntentType.FAQ,
                        confidence=0.9,
                        state=plugin_result.next_state,
                        suggested_actions=plugin_result.suggested_actions or self._get_suggested_actions(
                            plugin_result.next_state,
                            IntentType.FAQ,
                            capabilities_summary,
                        ),
                        metadata={
                            "message_count": len(context.messages),
                            "routing_path": ProcessingPath.SIMPLE.value,
                            "routing_score": 1.0,
                            "routing_signals": {"path": "agent_plugin_runtime"},
                            "agent_plugin_handled": True,
                            "memory_summary": memory_snapshot.get("summary", ""),
                            "memory_facts": memory_snapshot.get("facts", {}),
                            **(plugin_result.metadata or {}),
                        },
                    )

        # Optional two-stage LLM orchestration mode:
        # 1) orchestrator LLM decides service/action/ticketing
        # 2) service LLM refines slot collection/response
        # Deterministic policy gate runs before any execution side-effect.
        if bool(getattr(settings, "chat_llm_orchestration_mode", False)):
            orchestration_response = await self._process_llm_orchestration_message(
                request=request_for_processing,
                context=context,
                capabilities_summary=capabilities_summary,
                routing_decision=routing_decision,
                memory_snapshot=memory_snapshot,
                selected_phase_context=selected_phase_context,
                db_session=db_session,
                prefetched_services=prefetched_services,
            )
            if orchestration_response is not None:
                # Surface the active property scope onto the response so the
                # API layer can decide booking-button display (e.g. show the
                # "Book on website" button only when exactly ONE property is in
                # focus). The scope lives on context.pending_data after routing.
                try:
                    if isinstance(orchestration_response.metadata, dict) and isinstance(context.pending_data, dict):
                        _act_ids = [
                            str(x).strip()
                            for x in (context.pending_data.get("_active_property_ids") or [])
                            if str(x).strip()
                        ]
                        _act_names = [
                            str(x).strip()
                            for x in (context.pending_data.get("_active_property_names") or [])
                            if str(x).strip()
                        ]
                        orchestration_response.metadata["active_property_ids"] = _act_ids
                        if _act_names:
                            orchestration_response.metadata["active_property_names"] = _act_names
                except Exception:
                    pass
                _orch_meta = orchestration_response.metadata if isinstance(orchestration_response.metadata, dict) else {}
                _orch_intent_raw = _orch_meta.get("intent") or _orch_meta.get("classified_intent") or "faq"
                try:
                    _orch_intent = IntentType(str(_orch_intent_raw))
                except Exception:
                    _orch_intent = IntentType.FAQ
                try:
                    _orch_conf = float(_orch_meta.get("confidence") or _orch_meta.get("classified_confidence") or 0.0)
                except Exception:
                    _orch_conf = 0.0
                self._log_processing_stage(
                    "mode_return",
                    request=request,
                    context=context,
                    intent_result=IntentResult(intent=_orch_intent, confidence=_orch_conf, entities={}),
                    extra={
                        "mode": "chat_llm_orchestration_mode",
                        "response_source": str((orchestration_response.metadata or {}).get("response_source") or "llm_orchestration_mode"),
                    },
                )
                return orchestration_response

        # Optional pure-LLM mode: full KB + full context with no deterministic
        # post-processing overrides in runtime.

        multi_ask_response = await self._maybe_handle_multi_ask_message(
            request=request_for_processing,
            context=context,
            capabilities_summary=capabilities_summary,
            routing_decision=routing_decision,
            memory_snapshot=memory_snapshot,
            history_window=history_window,
            db_session=db_session,
        )
        if multi_ask_response is not None:
            self._log_processing_stage(
                "mode_return",
                request=request,
                context=context,
                intent_result=IntentResult(intent=multi_ask_response.intent, confidence=multi_ask_response.confidence, entities={}),
                extra={
                    "mode": "chat_multi_ask_orchestration",
                    "response_source": str((multi_ask_response.metadata or {}).get("response_source") or "multi_ask_mode"),
                },
            )
            return multi_ask_response

        # 3a. Deterministic de-escalation from human handoff back to bot flow.
        if (
            context.state == ConversationState.ESCALATED
            and (
                self._is_return_to_bot_request(effective_user_message)
                or self._is_return_to_bot_request(raw_user_message)
            )
        ):
            internal_pending_entries = conversation_memory_service.internal_pending_entries(context.pending_data)
            context.pending_action = None
            context.pending_data = conversation_memory_service.retain_safe_internal_entries(
                context.pending_data,
            )
            context.pending_data.pop("_clarification_attempts", None)
            context.state = ConversationState.IDLE
            response_text = "You're back with the bot. How can I help you next?"
            response_text, _ = await self._maybe_llm_rewrite_response(
                response_text=response_text,
                user_message=effective_user_message,
                intent_result=IntentResult(intent=IntentType.HUMAN_REQUEST, confidence=1.0, entities={}),
                context=context,
                capabilities_summary=capabilities_summary,
                capability_check_allowed=True,
                capability_reason="",
                response_source="deescalation",
                validator_replaced=False,
            )
            assistant_metadata = {
                "intent": IntentType.HUMAN_REQUEST.value,
                "confidence": 1.0,
                "channel": context.channel,
                "system_action": "deescalated_to_bot",
            }
            context.add_message(
                MessageRole.ASSISTANT,
                response_text,
                metadata=assistant_metadata,
            )
            conversation_memory_service.capture_assistant_message(
                context,
                response_text,
                metadata=assistant_metadata,
            )
            await self._refresh_summary_async(context)
            await context_manager.save_context(context, db_session=db_session)
            memory_snapshot = conversation_memory_service.get_snapshot(context)

            return ChatResponse(
                session_id=request.session_id,
                message=response_text,
                intent=IntentType.HUMAN_REQUEST,
                confidence=1.0,
                state=ConversationState.IDLE,
                suggested_actions=config_service.get_quick_actions(limit=4),
                metadata={
                    "message_count": len(context.messages),
                    "routing_path": ProcessingPath.SIMPLE.value,
                    "routing_score": 1.0,
                    "routing_signals": {"path": "deescalation"},
                    "memory_summary": memory_snapshot.get("summary", ""),
                    "memory_facts": memory_snapshot.get("facts", {}),
                    "deescalated": True,
                },
            )

        # 3aa. Deterministic generic service detail collection flow.
        if context.pending_action == "collect_service_details":
            msg_lower = str(effective_user_message or "").strip().lower()
            should_consume_service_followup = (
                msg_lower in {"cancel", "stop", "no", "nope", "never mind", "nevermind"}
                or self._looks_like_service_detail_reply(msg_lower)
            )
            # Only consume detail-like/cancel replies here. Otherwise let normal
            # routing classify and interrupt pending flow when user switches topics.
            if should_consume_service_followup:
                followup_result = self._handle_service_detail_followup(effective_user_message, context, capabilities_summary)
                if followup_result is not None:
                    context.state = followup_result.next_state
                    context.pending_action = followup_result.pending_action
                    context.pending_data = conversation_memory_service.merge_with_internal(
                        followup_result.pending_data or {},
                        conversation_memory_service.internal_pending_entries(context.pending_data),
                    )
                    response_text = str(followup_result.response_text or "").strip()
                    response_text, _ = await self._maybe_llm_rewrite_response(
                        response_text=response_text,
                        user_message=effective_user_message,
                        intent_result=IntentResult(intent=IntentType.FAQ, confidence=0.88, entities={}),
                        context=context,
                        capabilities_summary=capabilities_summary,
                        capability_check_allowed=True,
                        capability_reason="",
                        response_source="service_detail_followup",
                        validator_replaced=False,
                    )
                    assistant_metadata = {
                        "intent": IntentType.FAQ.value,
                        "confidence": 0.88,
                        "channel": context.channel,
                        "service_followup": True,
                    }
                    if followup_result.metadata:
                        assistant_metadata.update(followup_result.metadata)
                    context.add_message(
                        MessageRole.ASSISTANT,
                        response_text,
                        metadata=assistant_metadata,
                    )
                    conversation_memory_service.capture_assistant_message(
                        context,
                        response_text,
                        metadata=assistant_metadata,
                    )
                    await self._refresh_summary_async(context)
                    await context_manager.save_context(context, db_session=db_session)
                    memory_snapshot = conversation_memory_service.get_snapshot(context)
                    return ChatResponse(
                        session_id=request.session_id,
                        message=response_text,
                        intent=IntentType.FAQ,
                        confidence=0.88,
                        state=followup_result.next_state,
                        suggested_actions=followup_result.suggested_actions or self._get_suggested_actions(
                            followup_result.next_state,
                            IntentType.FAQ,
                            capabilities_summary,
                        ),
                        metadata={
                            "message_count": len(context.messages),
                            "routing_path": ProcessingPath.SIMPLE.value,
                            "routing_score": 1.0,
                            "routing_signals": {"path": "service_detail_followup"},
                            "service_followup": True,
                            "memory_summary": memory_snapshot.get("summary", ""),
                            "memory_facts": memory_snapshot.get("facts", {}),
                        },
                    )

        # 3b. Deterministic personal-memory shortcut (latest reservation/order/departure facts).
        memory_match = self._match_memory_information_response(
            effective_user_message,
            context,
            memory_snapshot,
        )
        if memory_match is not None:
            intent_result = IntentResult(
                intent=IntentType.FAQ,
                confidence=0.9,
                entities={
                    "memory_match_type": memory_match.get("match_type"),
                },
            )
            response_text = memory_match["response_text"]
            response_text, _ = await self._maybe_llm_rewrite_response(
                response_text=response_text,
                user_message=effective_user_message,
                intent_result=intent_result,
                context=context,
                capabilities_summary=capabilities_summary,
                capability_check_allowed=True,
                capability_reason="",
                response_source="memory_shortcut",
                validator_replaced=False,
            )

            context.state = ConversationState.IDLE
            assistant_metadata = {
                "intent": intent_result.intent.value,
                "confidence": intent_result.confidence,
                "channel": context.channel,
                "memory_match_type": memory_match.get("match_type"),
            }
            context.add_message(
                MessageRole.ASSISTANT,
                response_text,
                metadata=assistant_metadata,
            )
            conversation_memory_service.capture_assistant_message(
                context,
                response_text,
                metadata=assistant_metadata,
            )
            await self._refresh_summary_async(context)
            await context_manager.save_context(context, db_session=db_session)
            memory_snapshot = conversation_memory_service.get_snapshot(context)

            return ChatResponse(
                session_id=request.session_id,
                message=response_text,
                intent=intent_result.intent,
                confidence=intent_result.confidence,
                state=ConversationState.IDLE,
                suggested_actions=self._get_suggested_actions(
                    ConversationState.IDLE,
                    IntentType.FAQ,
                    capabilities_summary,
                ),
                metadata={
                    "message_count": len(context.messages),
                    "entities": intent_result.entities,
                    "routing_path": ProcessingPath.SIMPLE.value,
                    "routing_score": 1.0,
                    "routing_signals": {"path": "memory_shortcut"},
                    "memory_shortcut_match": True,
                    "memory_match_type": memory_match.get("match_type"),
                    "memory_summary": memory_snapshot.get("summary", ""),
                    "memory_facts": memory_snapshot.get("facts", {}),
                },
            )

        # 3c. Deterministic identity/profile shortcut.
        identity_match = self._match_identity_response(effective_user_message, capabilities_summary, context)
        if identity_match is not None:
            intent_result = IntentResult(
                intent=IntentType.FAQ,
                confidence=0.92,
                entities={"identity_match_type": identity_match.get("match_type")},
            )
            response_text = identity_match["response_text"]
            response_text, _ = await self._maybe_llm_rewrite_response(
                response_text=response_text,
                user_message=effective_user_message,
                intent_result=intent_result,
                context=context,
                capabilities_summary=capabilities_summary,
                capability_check_allowed=True,
                capability_reason="",
                response_source="identity_shortcut",
                validator_replaced=False,
            )

            context.state = ConversationState.IDLE
            assistant_metadata = {
                "intent": intent_result.intent.value,
                "confidence": intent_result.confidence,
                "channel": context.channel,
                "identity_match_type": identity_match.get("match_type"),
            }
            context.add_message(
                MessageRole.ASSISTANT,
                response_text,
                metadata=assistant_metadata,
            )
            conversation_memory_service.capture_assistant_message(
                context,
                response_text,
                metadata=assistant_metadata,
            )
            await self._refresh_summary_async(context)
            await context_manager.save_context(context, db_session=db_session)
            memory_snapshot = conversation_memory_service.get_snapshot(context)

            return ChatResponse(
                session_id=request.session_id,
                message=response_text,
                intent=intent_result.intent,
                confidence=intent_result.confidence,
                state=ConversationState.IDLE,
                suggested_actions=self._get_suggested_actions(
                    ConversationState.IDLE,
                    IntentType.FAQ,
                    capabilities_summary,
                ),
                metadata={
                    "message_count": len(context.messages),
                    "entities": intent_result.entities,
                    "routing_path": ProcessingPath.SIMPLE.value,
                    "routing_score": 1.0,
                    "routing_signals": {"path": "identity_shortcut"},
                    "identity_shortcut_match": True,
                    "identity_match_type": identity_match.get("match_type"),
                    "memory_summary": memory_snapshot.get("summary", ""),
                    "memory_facts": memory_snapshot.get("facts", {}),
                },
            )

        # 3d. Deterministic service-overview shortcut.
        services_overview = self._match_service_overview_response(effective_user_message, capabilities_summary, context)
        if services_overview is not None:
            intent_result = IntentResult(
                intent=IntentType.FAQ,
                confidence=0.9,
                entities={"match_type": "service_overview"},
            )
            response_text = services_overview["response_text"]
            response_text, _ = await self._maybe_llm_rewrite_response(
                response_text=response_text,
                user_message=effective_user_message,
                intent_result=intent_result,
                context=context,
                capabilities_summary=capabilities_summary,
                capability_check_allowed=True,
                capability_reason="",
                response_source="service_overview",
                validator_replaced=False,
            )

            context.state = ConversationState.IDLE
            assistant_metadata = {
                "intent": intent_result.intent.value,
                "confidence": intent_result.confidence,
                "channel": context.channel,
                "service_overview": True,
            }
            context.add_message(
                MessageRole.ASSISTANT,
                response_text,
                metadata=assistant_metadata,
            )
            conversation_memory_service.capture_assistant_message(
                context,
                response_text,
                metadata=assistant_metadata,
            )
            await self._refresh_summary_async(context)
            await context_manager.save_context(context, db_session=db_session)
            memory_snapshot = conversation_memory_service.get_snapshot(context)

            return ChatResponse(
                session_id=request.session_id,
                message=response_text,
                intent=intent_result.intent,
                confidence=intent_result.confidence,
                state=ConversationState.IDLE,
                suggested_actions=self._get_suggested_actions(
                    ConversationState.IDLE,
                    IntentType.FAQ,
                    capabilities_summary,
                ),
                metadata={
                    "message_count": len(context.messages),
                    "entities": intent_result.entities,
                    "routing_path": ProcessingPath.SIMPLE.value,
                    "routing_score": 1.0,
                    "routing_signals": {"path": "service_overview"},
                    "memory_summary": memory_snapshot.get("summary", ""),
                    "memory_facts": memory_snapshot.get("facts", {}),
                },
            )

        # 3e. Deterministic profile update shortcut for room number capture.
        room_update = self._match_room_number_update_response(effective_user_message, context)
        if room_update is not None:
            context.room_number = room_update["room_number"]
            intent_result = IntentResult(
                intent=IntentType.FAQ,
                confidence=0.94,
                entities={
                    "match_type": "room_number_update",
                    "room_number": context.room_number,
                },
            )
            response_text = room_update["response_text"]
            response_text, _ = await self._maybe_llm_rewrite_response(
                response_text=response_text,
                user_message=effective_user_message,
                intent_result=intent_result,
                context=context,
                capabilities_summary=capabilities_summary,
                capability_check_allowed=True,
                capability_reason="",
                response_source="room_number_update",
                validator_replaced=False,
            )

            context.state = ConversationState.IDLE
            assistant_metadata = {
                "intent": intent_result.intent.value,
                "confidence": intent_result.confidence,
                "channel": context.channel,
                "room_number": context.room_number,
                "profile_update": True,
            }
            context.add_message(
                MessageRole.ASSISTANT,
                response_text,
                metadata=assistant_metadata,
            )
            conversation_memory_service.capture_assistant_message(
                context,
                response_text,
                metadata=assistant_metadata,
            )
            await self._refresh_summary_async(context)
            await context_manager.save_context(context, db_session=db_session)
            memory_snapshot = conversation_memory_service.get_snapshot(context)

            return ChatResponse(
                session_id=request.session_id,
                message=response_text,
                intent=intent_result.intent,
                confidence=intent_result.confidence,
                state=ConversationState.IDLE,
                suggested_actions=self._get_suggested_actions(
                    ConversationState.IDLE,
                    IntentType.FAQ,
                    capabilities_summary,
                ),
                metadata={
                    "message_count": len(context.messages),
                    "entities": intent_result.entities,
                    "routing_path": ProcessingPath.SIMPLE.value,
                    "routing_score": 1.0,
                    "routing_signals": {"path": "room_number_update"},
                    "memory_summary": memory_snapshot.get("summary", ""),
                    "memory_facts": memory_snapshot.get("facts", {}),
                },
            )

        # 3f. Deterministic room-number recall shortcut.
        room_lookup = self._match_room_number_lookup_response(effective_user_message, context, memory_snapshot)
        if room_lookup is not None:
            intent_result = IntentResult(
                intent=IntentType.FAQ,
                confidence=0.92,
                entities={"match_type": "room_number_lookup"},
            )
            response_text = room_lookup["response_text"]
            response_text, _ = await self._maybe_llm_rewrite_response(
                response_text=response_text,
                user_message=effective_user_message,
                intent_result=intent_result,
                context=context,
                capabilities_summary=capabilities_summary,
                capability_check_allowed=True,
                capability_reason="",
                response_source="room_number_lookup",
                validator_replaced=False,
            )

            context.state = ConversationState.IDLE
            assistant_metadata = {
                "intent": intent_result.intent.value,
                "confidence": intent_result.confidence,
                "channel": context.channel,
                "room_lookup": True,
            }
            context.add_message(
                MessageRole.ASSISTANT,
                response_text,
                metadata=assistant_metadata,
            )
            conversation_memory_service.capture_assistant_message(
                context,
                response_text,
                metadata=assistant_metadata,
            )
            await self._refresh_summary_async(context)
            await context_manager.save_context(context, db_session=db_session)
            memory_snapshot = conversation_memory_service.get_snapshot(context)

            return ChatResponse(
                session_id=request.session_id,
                message=response_text,
                intent=intent_result.intent,
                confidence=intent_result.confidence,
                state=ConversationState.IDLE,
                suggested_actions=self._get_suggested_actions(
                    ConversationState.IDLE,
                    IntentType.FAQ,
                    capabilities_summary,
                ),
                metadata={
                    "message_count": len(context.messages),
                    "entities": intent_result.entities,
                    "routing_path": ProcessingPath.SIMPLE.value,
                    "routing_score": 1.0,
                    "routing_signals": {"path": "room_number_lookup"},
                    "memory_summary": memory_snapshot.get("summary", ""),
                    "memory_facts": memory_snapshot.get("facts", {}),
                },
            )

        # 3d. (REMOVED) FAQ-bank shortcut deleted — FAQ entries are now injected
        # as PRIMARY KNOWLEDGE context into every answer-side LLM prompt. All
        # messages flow through the LLM, which sees FAQ as primary truth.

        # 3g. Deterministic menu recommendation shortcut.
        menu_recommendation = await self._match_menu_recommendation_response(
            message=effective_user_message,
            context=context,
            capabilities_summary=capabilities_summary,
            db_session=db_session,
        )
        if menu_recommendation is not None:
            intent_result = IntentResult(
                intent=IntentType.FAQ,
                confidence=0.88,
                entities={
                    "match_type": menu_recommendation.get("match_type", "menu_recommendation"),
                },
            )
            response_text = menu_recommendation["response_text"]
            response_text, _ = await self._maybe_llm_rewrite_response(
                response_text=response_text,
                user_message=effective_user_message,
                intent_result=intent_result,
                context=context,
                capabilities_summary=capabilities_summary,
                capability_check_allowed=True,
                capability_reason="",
                response_source="menu_recommendation",
                validator_replaced=False,
            )
            context.state = ConversationState.IDLE
            context.pending_action = None
            context.pending_data = conversation_memory_service.merge_with_internal(
                {},
                conversation_memory_service.internal_pending_entries(context.pending_data),
            )
            assistant_metadata = {
                "intent": intent_result.intent.value,
                "confidence": intent_result.confidence,
                "channel": context.channel,
                "menu_recommendation": True,
            }
            context.add_message(
                MessageRole.ASSISTANT,
                response_text,
                metadata=assistant_metadata,
            )
            conversation_memory_service.capture_assistant_message(
                context,
                response_text,
                metadata=assistant_metadata,
            )
            await self._refresh_summary_async(context)
            await context_manager.save_context(context, db_session=db_session)
            memory_snapshot = conversation_memory_service.get_snapshot(context)

            return ChatResponse(
                session_id=request.session_id,
                message=response_text,
                intent=intent_result.intent,
                confidence=intent_result.confidence,
                state=ConversationState.IDLE,
                suggested_actions=self._get_suggested_actions(
                    ConversationState.IDLE,
                    IntentType.FAQ,
                    capabilities_summary,
                ),
                metadata={
                    "message_count": len(context.messages),
                    "entities": intent_result.entities,
                    "routing_path": ProcessingPath.SIMPLE.value,
                    "routing_score": 1.0,
                    "routing_signals": {"path": "menu_recommendation"},
                    "memory_summary": memory_snapshot.get("summary", ""),
                    "memory_facts": memory_snapshot.get("facts", {}),
                },
            )

        # 3d. Deterministic service-aware shortcut (status/hours/description from admin services).
        service_match = self._match_service_information_response(effective_user_message, context, capabilities_summary)
        if service_match is not None:
            pending_interrupted = False
            if context.pending_action and self._should_interrupt_pending_flow_for_user_query(
                message=effective_user_message,
                context=context,
                capabilities_summary=capabilities_summary,
            ):
                parked = self._park_active_task(
                    context=context,
                    selected_phase_context=selected_phase_context,
                    reason="topic_diversion",
                    user_message=effective_user_message,
                )
                pending_interrupted = parked is not None
            service_match_type = str(service_match.get("match_type") or "")
            if service_match_type == "service_catalog_unavailable_handoff":
                handoff_response = await self._handle_service_catalog_unavailable_handoff(
                    request=request_for_processing,
                    context=context,
                    capabilities_summary=capabilities_summary,
                    service_match=service_match,
                    db_session=db_session,
                    routing_path_tag="service_catalog_unavailable_handoff",
                    pending_interrupted=pending_interrupted,
                )
                if handoff_response is not None:
                    return handoff_response

            intent_result = IntentResult(
                intent=IntentType.FAQ,
                confidence=0.86,
                entities={
                    "service_id": service_match.get("service_id"),
                    "service_name": service_match.get("service_name"),
                    "service_match_type": service_match.get("match_type"),
                },
            )
            response_text = str(service_match.get("response_text") or "")
            if service_match_type == "service_catalog_action":
                context.state = ConversationState.AWAITING_INFO
                internal_pending_entries = conversation_memory_service.internal_pending_entries(context.pending_data)
                context.pending_action = "collect_service_details"
                context.pending_data = conversation_memory_service.merge_with_internal(
                    {
                        "service_id": service_match.get("service_id"),
                        "service_name": service_match.get("service_name"),
                    },
                    internal_pending_entries,
                )
            else:
                context.state = ConversationState.IDLE
                context.pending_action = None
                context.pending_data = conversation_memory_service.merge_with_internal(
                    {},
                    conversation_memory_service.internal_pending_entries(context.pending_data),
                )
            response_text, resume_actions = self._append_resume_checkpoint(
                response_text=response_text,
                context=context,
                pending_interrupted=pending_interrupted,
            )
            response_text, _ = await self._maybe_llm_rewrite_response(
                response_text=response_text,
                user_message=effective_user_message,
                intent_result=intent_result,
                context=context,
                capabilities_summary=capabilities_summary,
                capability_check_allowed=True,
                capability_reason="",
                response_source="service_catalog",
                validator_replaced=False,
            )
            assistant_metadata = {
                "intent": intent_result.intent.value,
                "confidence": intent_result.confidence,
                "channel": context.channel,
                "service_id": service_match.get("service_id"),
                "service_match_type": service_match.get("match_type"),
            }
            context.add_message(
                MessageRole.ASSISTANT,
                response_text,
                metadata=assistant_metadata,
            )
            conversation_memory_service.capture_assistant_message(
                context,
                response_text,
                metadata=assistant_metadata,
            )
            await self._refresh_summary_async(context)
            await context_manager.save_context(context, db_session=db_session)
            memory_snapshot = conversation_memory_service.get_snapshot(context)

            return ChatResponse(
                session_id=request.session_id,
                message=response_text,
                intent=intent_result.intent,
                confidence=intent_result.confidence,
                state=context.state,
                suggested_actions=self._finalize_user_query_suggestions(
                    resume_actions + (
                        list(service_match.get("suggested_actions") or [])
                        if isinstance(service_match.get("suggested_actions"), list)
                        and service_match.get("suggested_actions")
                        else (
                            ["Share details", "Cancel"]
                            if service_match_type == "service_catalog_action"
                            else self._get_suggested_actions(
                                context.state,
                                IntentType.FAQ,
                                capabilities_summary,
                            )
                        )
                    ),
                    state=context.state,
                    intent=IntentType.FAQ,
                    pending_action=context.pending_action,
                    pending_data=context.pending_data,
                    capabilities_summary=capabilities_summary,
                ),
                metadata={
                    "message_count": len(context.messages),
                    "entities": intent_result.entities,
                    "routing_path": ProcessingPath.SIMPLE.value,
                    "routing_score": 1.0,
                    "routing_signals": {"path": "service_catalog"},
                    "pending_interrupted": pending_interrupted,
                    "resume_checkpoint": bool(resume_actions),
                    "parked_task_available": self._peek_parked_task(context) is not None,
                    "service_catalog_match": True,
                    "service_id": service_match.get("service_id"),
                    "service_match_type": service_match.get("match_type"),
                    "memory_summary": memory_snapshot.get("summary", ""),
                    "memory_facts": memory_snapshot.get("facts", {}),
                },
            )

        context_pack = self._build_llm_context_pack(
            context=context,
            capabilities_summary=capabilities_summary,
            selected_phase_context=selected_phase_context,
            memory_snapshot=memory_snapshot,
            user_message=effective_user_message,
        )

        # 4. Build context dict for LLM (with capabilities)
        llm_context = {
            "hotel_code": context.hotel_code,
            "hotel_name": capabilities_summary.get("hotel_name", context.hotel_code),
            "bot_name": capabilities_summary.get("bot_name", "Assistant"),
            "business_type": capabilities_summary.get("business_type", "hotel"),
            "selected_phase_id": selected_phase_context.get("selected_phase_id", ""),
            "selected_phase_name": selected_phase_context.get("selected_phase_name", ""),
            "enabled_intents": [
                intent.get("id")
                for intent in capabilities_summary.get("intents", [])
                if intent.get("enabled", False)
            ],
            "city": capabilities_summary.get("city", ""),
            "guest_name": context.guest_name or "Guest",
            "room_number": context.room_number,
            "state": context.state.value,
            "pending_action": context.pending_action,
            "pending_data": context.pending_data,
            "channel": context.channel,
            "capabilities": capabilities_summary,
            "intent_catalog": capabilities_summary.get("intents", []),
            "service_catalog": capabilities_summary.get("service_catalog", []),
            "faq_bank": capabilities_summary.get("faq_bank", []),
            "tools": capabilities_summary.get("tools", []),
            "nlu_policy": capabilities_summary.get("nlu_policy", {}),
            "prompts": capabilities_summary.get("prompts", {}),
            "conversation_summary": memory_snapshot.get("summary", ""),
            "memory_facts": memory_snapshot.get("facts", {}),
            "memory_recent_changes": memory_snapshot.get("recent_changes", []),
            "context_pack": context_pack,
            "current_phase": context_pack.get("current_phase", {}),
            "active_flow": context_pack.get("active_flow", {}),
            "missing_slots": context_pack.get("missing_slots", []),
            "phase_services": context_pack.get("phase_services", []),
            "ticketing_enabled_by_service": context_pack.get("ticketing_enabled_by_service", {}),
            "stay_window": context_pack.get("stay_window", {}),
            "current_time": context_pack.get("current_time", ""),
            "recent_user_goal": context_pack.get("recent_user_goal", ""),
        }
        self._log_processing_stage(
            "intent_classification_started",
            request=request,
            context=context,
            extra={
                "history_window": int(history_window),
                "llm_context_pending_action": str(context.pending_action or ""),
                "llm_context_missing_slots_count": len(context_pack.get("missing_slots", [])),
                "llm_context_phase_services_count": len(context_pack.get("phase_services", [])),
            },
        )

        # 5. Classify intent using LLM
        intent_result = await self._classify_intent(
            effective_user_message,
            context.to_llm_messages(count=history_window),
            llm_context,
        )
        self._log_processing_stage(
            "intent_classified",
            request=request,
            context=context,
            intent_result=intent_result,
            extra={
                "entity_keys": sorted([str(k) for k in (intent_result.entities or {}).keys()])[:80],
            },
        )

        pending_interrupted = False
        if context.pending_action and self._should_interrupt_pending_flow(
            message=effective_user_message,
            intent_result=intent_result,
            context=context,
            capabilities_summary=capabilities_summary,
        ):
            parked = self._park_active_task(
                context=context,
                selected_phase_context=selected_phase_context,
                reason="topic_diversion",
                user_message=effective_user_message,
            )
            pending_interrupted = parked is not None

        if pending_interrupted:
            identity_match = self._match_identity_response(effective_user_message, capabilities_summary, context)
            if identity_match is not None:
                response_text, resume_actions = self._append_resume_checkpoint(
                    response_text=str(identity_match["response_text"] or ""),
                    context=context,
                    pending_interrupted=pending_interrupted,
                )
                response_text, _ = await self._maybe_llm_rewrite_response(
                    response_text=response_text,
                    user_message=effective_user_message,
                    intent_result=IntentResult(intent=IntentType.FAQ, confidence=0.92, entities={}),
                    context=context,
                    capabilities_summary=capabilities_summary,
                    capability_check_allowed=True,
                    capability_reason="",
                    response_source="identity_shortcut_post_interrupt",
                    validator_replaced=False,
                )
                context.add_message(
                    MessageRole.ASSISTANT,
                    response_text,
                    metadata={
                        "intent": IntentType.FAQ.value,
                        "confidence": 0.92,
                        "channel": context.channel,
                        "identity_match_type": identity_match.get("match_type"),
                    },
                )
                conversation_memory_service.capture_assistant_message(
                    context,
                    response_text,
                    metadata={
                        "intent": IntentType.FAQ.value,
                        "confidence": 0.92,
                        "channel": context.channel,
                        "identity_match_type": identity_match.get("match_type"),
                    },
                )
                await self._refresh_summary_async(context)
                await context_manager.save_context(context, db_session=db_session)
                memory_snapshot = conversation_memory_service.get_snapshot(context)
                return ChatResponse(
                    session_id=request.session_id,
                    message=response_text,
                    intent=IntentType.FAQ,
                    confidence=0.92,
                    state=ConversationState.IDLE,
                    suggested_actions=self._finalize_user_query_suggestions(
                        resume_actions
                        + self._get_suggested_actions(
                            ConversationState.IDLE,
                            IntentType.FAQ,
                            capabilities_summary,
                        ),
                        state=ConversationState.IDLE,
                        intent=IntentType.FAQ,
                        pending_action=context.pending_action,
                        pending_data=context.pending_data,
                        capabilities_summary=capabilities_summary,
                    ),
                    metadata={
                        "message_count": len(context.messages),
                        "routing_path": ProcessingPath.SIMPLE.value,
                        "routing_score": 1.0,
                        "routing_signals": {"path": "identity_shortcut_post_interrupt"},
                        "pending_interrupted": True,
                        "resume_checkpoint": bool(resume_actions),
                        "parked_task_available": self._peek_parked_task(context) is not None,
                        "identity_shortcut_match": True,
                        "identity_match_type": identity_match.get("match_type"),
                        "memory_summary": memory_snapshot.get("summary", ""),
                        "memory_facts": memory_snapshot.get("facts", {}),
                    },
                )

            service_match = self._match_service_information_response(
                effective_user_message,
                context,
                capabilities_summary,
            )
            if service_match is not None:
                service_match_type = str(service_match.get("match_type") or "")
                if service_match_type == "service_catalog_unavailable_handoff":
                    handoff_response = await self._handle_service_catalog_unavailable_handoff(
                        request=request_for_processing,
                        context=context,
                        capabilities_summary=capabilities_summary,
                        service_match=service_match,
                        db_session=db_session,
                        routing_path_tag="service_catalog_unavailable_handoff_post_interrupt",
                        pending_interrupted=True,
                    )
                    if handoff_response is not None:
                        return handoff_response

                response_text, resume_actions = self._append_resume_checkpoint(
                    response_text=str(service_match["response_text"] or ""),
                    context=context,
                    pending_interrupted=pending_interrupted,
                )
                response_text, _ = await self._maybe_llm_rewrite_response(
                    response_text=response_text,
                    user_message=effective_user_message,
                    intent_result=IntentResult(intent=IntentType.FAQ, confidence=0.86, entities={}),
                    context=context,
                    capabilities_summary=capabilities_summary,
                    capability_check_allowed=True,
                    capability_reason="",
                    response_source="service_catalog_post_interrupt",
                    validator_replaced=False,
                )
                context.add_message(
                    MessageRole.ASSISTANT,
                    response_text,
                    metadata={
                        "intent": IntentType.FAQ.value,
                        "confidence": 0.86,
                        "channel": context.channel,
                        "service_id": service_match.get("service_id"),
                        "service_match_type": service_match.get("match_type"),
                    },
                )
                conversation_memory_service.capture_assistant_message(
                    context,
                    response_text,
                    metadata={
                        "intent": IntentType.FAQ.value,
                        "confidence": 0.86,
                        "channel": context.channel,
                        "service_id": service_match.get("service_id"),
                        "service_match_type": service_match.get("match_type"),
                    },
                )
                await self._refresh_summary_async(context)
                await context_manager.save_context(context, db_session=db_session)
                memory_snapshot = conversation_memory_service.get_snapshot(context)
                return ChatResponse(
                    session_id=request.session_id,
                    message=response_text,
                    intent=IntentType.FAQ,
                    confidence=0.86,
                    state=ConversationState.IDLE,
                    suggested_actions=self._finalize_user_query_suggestions(
                        resume_actions + (
                            list(service_match.get("suggested_actions") or [])
                            if isinstance(service_match.get("suggested_actions"), list)
                            and service_match.get("suggested_actions")
                            else self._get_suggested_actions(
                                ConversationState.IDLE,
                                IntentType.FAQ,
                                capabilities_summary,
                            )
                        ),
                        state=ConversationState.IDLE,
                        intent=IntentType.FAQ,
                        pending_action=context.pending_action,
                        pending_data=context.pending_data,
                        capabilities_summary=capabilities_summary,
                    ),
                    metadata={
                        "message_count": len(context.messages),
                        "routing_path": ProcessingPath.SIMPLE.value,
                        "routing_score": 1.0,
                        "routing_signals": {"path": "service_catalog_post_interrupt"},
                        "pending_interrupted": True,
                        "resume_checkpoint": bool(resume_actions),
                        "parked_task_available": self._peek_parked_task(context) is not None,
                        "service_catalog_match": True,
                        "service_id": service_match.get("service_id"),
                        "service_match_type": service_match.get("match_type"),
                        "memory_summary": memory_snapshot.get("summary", ""),
                        "memory_facts": memory_snapshot.get("facts", {}),
                    },
                )

        # 5b. Confidence-aware handling (clarification/escalation)
        low_confidence = self._handle_low_confidence(intent_result, context)
        if low_confidence is not None:
            response_text, next_state = low_confidence
            response_text, resume_actions = self._append_resume_checkpoint(
                response_text=response_text,
                context=context,
                pending_interrupted=pending_interrupted,
            )
            response_text, low_confidence_rewrite_metadata = await self._maybe_llm_rewrite_response(
                response_text=response_text,
                user_message=effective_user_message,
                intent_result=intent_result,
                context=context,
                capabilities_summary=capabilities_summary,
                capability_check_allowed=True,
                capability_reason="",
                response_source="low_confidence",
                validator_replaced=False,
            )
            context.state = next_state
            self._sync_active_task_snapshot(
                context=context,
                selected_phase_context=selected_phase_context,
            )
            assistant_metadata = {
                "intent": intent_result.intent.value,
                "confidence": intent_result.confidence,
                "channel": context.channel,
            }
            context.add_message(
                MessageRole.ASSISTANT,
                response_text,
                metadata=assistant_metadata,
            )
            conversation_memory_service.capture_assistant_message(
                context,
                response_text,
                metadata=assistant_metadata,
            )
            await self._refresh_summary_async(context)
            await context_manager.save_context(context, db_session=db_session)
            memory_snapshot = conversation_memory_service.get_snapshot(context)

            metadata = {
                "message_count": len(context.messages),
                "entities": intent_result.entities,
                "routing_path": routing_decision.path.value,
                "routing_score": routing_decision.score,
                "routing_signals": routing_decision.signals,
                "pending_interrupted": pending_interrupted,
                "resume_checkpoint": bool(resume_actions),
                "parked_task_available": self._peek_parked_task(context) is not None,
                "memory_summary": memory_snapshot.get("summary", ""),
                "memory_facts": memory_snapshot.get("facts", {}),
            }
            metadata.update(low_confidence_rewrite_metadata)
            self._log_processing_stage(
                "turn_completed",
                request=request,
                context=context,
                intent_result=intent_result,
                extra={
                    "response_source": "low_confidence",
                    "next_state": str(next_state.value),
                    "db_session_available": db_session is not None,
                },
            )
            return ChatResponse(
                session_id=request.session_id,
                message=response_text,
                intent=intent_result.intent,
                confidence=intent_result.confidence,
                state=next_state,
                suggested_actions=self._finalize_user_query_suggestions(
                    resume_actions
                    + self._get_suggested_actions(
                        next_state,
                        intent_result.intent,
                        capabilities_summary,
                    ),
                    state=next_state,
                    intent=intent_result.intent,
                    pending_action=context.pending_action,
                    pending_data=context.pending_data,
                    capabilities_summary=capabilities_summary,
                ),
                metadata=metadata,
            )
        context.pending_data.pop("_clarification_attempts", None)

        # 6. Check intent enablement and capability BEFORE generating response
        intent_enabled_check = self._check_intent_enabled(intent_result.intent)
        if intent_enabled_check.allowed:
            capability_check = self._check_capability_for_intent(
                context.hotel_code,
                intent_result,
                effective_user_message,
            )
        else:
            capability_check = intent_enabled_check

        # 7. Validate intent for current state
        is_valid, error_msg = state_machine.validate_intent_for_state(
            intent_result.intent, context.state
        )

        message_lower = str(effective_user_message or "").strip().lower()
        has_operational_ticket_signal = (
            self._looks_like_operational_issue_for_ticketing(message_lower)
            or self._looks_like_strong_ticket_issue_marker(message_lower)
        )
        phase_gate_intent = intent_result.intent
        if has_operational_ticket_signal and intent_result.intent in {
            IntentType.FAQ,
            IntentType.MENU_REQUEST,
            IntentType.HUMAN_REQUEST,
            IntentType.COMPLAINT,
        }:
            phase_gate_intent = IntentType.COMPLAINT

        contextual_sanity_issue = self._detect_contextual_sanity_issue(
            message=effective_user_message,
            intent=phase_gate_intent,
            context=context,
            pending_data=context.pending_data if isinstance(context.pending_data, dict) else {},
            entities=intent_result.entities if isinstance(intent_result.entities, dict) else {},
        )
        phase_gate = None
        if phase_gate_intent != IntentType.COMPLAINT:
            phase_gate = self._detect_ticketing_phase_service_mismatch(
                message=effective_user_message,
                context=context,
                pending_data=context.pending_data if isinstance(context.pending_data, dict) else {},
                entities=intent_result.entities if isinstance(intent_result.entities, dict) else {},
            )
            if phase_gate is None:
                phase_gate = self._detect_phase_service_unavailable_for_intent(
                    message=effective_user_message,
                    intent=phase_gate_intent,
                    context=context,
                    pending_data=context.pending_data if isinstance(context.pending_data, dict) else {},
                    entities=intent_result.entities if isinstance(intent_result.entities, dict) else {},
                )
        contextual_sanity_handler_result = (
            self._build_contextual_sanity_handler_result(contextual_sanity_issue)
            if contextual_sanity_issue is not None
            else None
        )
        phase_gate_handler_result = (
            await self._build_ticketing_phase_gate_handler_result(
                phase_gate,
                user_message=effective_user_message,
                intent=phase_gate_intent,
                context=context,
                capabilities_summary=capabilities_summary,
            )
            if phase_gate is not None
            else None
        )
        self._log_processing_stage(
            "policy_checks_completed",
            request=request,
            context=context,
            intent_result=intent_result,
            extra={
                "intent_enabled": bool(intent_enabled_check.allowed),
                "capability_allowed": bool(capability_check.allowed),
                "state_valid": bool(is_valid),
                "phase_gate_triggered": phase_gate is not None,
                "contextual_sanity_triggered": contextual_sanity_issue is not None,
            },
        )

        # 8. Generate response - try handler first, then complex agent team or LLM fallback
        handler_result: Optional[HandlerResult] = None
        agent_orchestrator_result = None
        response_source = "unknown"

        if contextual_sanity_handler_result is not None:
            handler_result = contextual_sanity_handler_result
            response_text = handler_result.response_text
            response_source = "contextual_sanity_gate"
        elif not capability_check.allowed:
            # Capability not available
            response_text = self._build_capability_denial_response(capability_check)
            response_source = "capability_denial"
        elif not is_valid:
            response_text = self._handle_unexpected_intent(context, intent_result, error_msg)
            response_source = "state_validation"
        else:
            # Try handler-based response
            handler_result = await self._dispatch_to_handler(
                effective_user_message, intent_result, context, capabilities_summary, db_session
            )

            if handler_result is not None:
                response_text = handler_result.response_text
                response_source = "handler"
            else:
                # No handler -> for COMPLEX route run agent team before generic LLM fallback
                if routing_decision.path == ProcessingPath.COMPLEX:
                    agent_orchestrator_result = await complex_query_orchestrator.handle(
                        message=effective_user_message,
                        intent_result=intent_result,
                        context=context,
                        capabilities_summary=capabilities_summary,
                        llm_context=llm_context,
                        routing_signals=routing_decision.signals,
                    )
                    if agent_orchestrator_result.handled:
                        response_text = agent_orchestrator_result.response_text
                        response_source = "agent_orchestrator"
                    else:
                        response_text = await self._generate_response(
                            effective_user_message,
                            intent_result,
                            context.to_llm_messages(count=history_window),
                            llm_context,
                            capability_check,
                        )
                        response_source = "llm_fallback"
                else:
                    # SIMPLE route fallback
                    response_text = await self._generate_response(
                        effective_user_message,
                        intent_result,
                        context.to_llm_messages(count=history_window),
                        llm_context,
                        capability_check,
                    )
                    response_source = "llm_fallback"

        self._log_processing_stage(
            "response_drafted",
            request=request,
            context=context,
            intent_result=intent_result,
            extra={
                "response_source": response_source,
                "handler_used": handler_result is not None,
                "agent_orchestrator_handled": bool(
                    agent_orchestrator_result and agent_orchestrator_result.handled
                ),
                "response_preview": self._trace_preview(response_text, 320),
            },
        )

        # 8b. Validate final response against capabilities/rules.
        validation = response_validator.validate(
            response_text=response_text,
            intent_result=intent_result,
            context=context,
            capabilities_summary=capabilities_summary,
            capability_check_allowed=capability_check.allowed,
            capability_reason=capability_check.reason,
        )
        validator_replaced = False
        if not validation.valid and validation.action == "replace" and validation.replacement_response:
            response_text = validation.replacement_response
            validator_replaced = True
        response_text = self._dedupe_response_sentences(response_text) or str(response_text or "").strip()
        state_basis_response_text = response_text

        # 9. Determine next state
        internal_pending_entries = conversation_memory_service.internal_pending_entries(context.pending_data)
        resume_actions: list[str] = []
        response_rewrite_metadata: dict[str, Any] = {}
        if handler_result is not None:
            next_state = handler_result.next_state
            # Apply pending action/data from handler
            if handler_result.pending_action is not None:
                context.pending_action = handler_result.pending_action
            if handler_result.pending_data is not None:
                context.pending_data = conversation_memory_service.merge_with_internal(
                    handler_result.pending_data,
                    internal_pending_entries,
                )
            # Clear pending if handler explicitly set to None and state is not awaiting
            if handler_result.pending_action is None and next_state not in (
                ConversationState.AWAITING_CONFIRMATION,
                ConversationState.AWAITING_INFO,
            ):
                context.pending_action = None
                context.pending_data = conversation_memory_service.retain_safe_internal_entries(
                    context.pending_data,
                )
                context.pending_data.pop("_clarification_attempts", None)
            if handler_result.metadata.get("room_number"):
                context.room_number = str(handler_result.metadata["room_number"])
        else:
            next_state = self._determine_next_state(context, intent_result, state_basis_response_text)

        response_text, response_rewrite_metadata = await self._maybe_llm_rewrite_response(
            response_text=response_text,
            user_message=effective_user_message,
            intent_result=intent_result,
            context=context,
            capabilities_summary=capabilities_summary,
            capability_check_allowed=capability_check.allowed,
            capability_reason=capability_check.reason,
            response_source=response_source,
            validator_replaced=validator_replaced,
        )

        response_text, resume_actions = self._append_resume_checkpoint(
            response_text=response_text,
            context=context,
            pending_interrupted=pending_interrupted,
        )

        turn_ticketing_status = await self._evaluate_ticketing_status_for_turn(
            message=effective_user_message,
            context=context,
            intent=intent_result.intent,
            response_text=response_text,
            current_pending_action=context.pending_action,
            pending_action_target=context.pending_action,
            capabilities_summary=capabilities_summary,
        )

        # 10. Update context
        context.state = next_state
        self._sync_active_task_snapshot(
            context=context,
            selected_phase_context=selected_phase_context,
        )
        assistant_metadata = {
            "intent": intent_result.intent.value,
            "confidence": intent_result.confidence,
            "channel": context.channel,
        }
        if handler_result is not None and handler_result.metadata:
            assistant_metadata.update(handler_result.metadata)
        context.add_message(
            MessageRole.ASSISTANT,
            response_text,
            metadata=assistant_metadata,
        )
        conversation_memory_service.capture_assistant_message(
            context,
            response_text,
            metadata=assistant_metadata,
        )
        await self._refresh_summary_async(context)
        await context_manager.save_context(context, db_session=db_session)
        memory_snapshot = conversation_memory_service.get_snapshot(context)

        # 11. Build suggested actions
        if handler_result is not None and handler_result.suggested_actions:
            suggested_actions = resume_actions + handler_result.suggested_actions
        else:
            suggested_actions = resume_actions + self._get_suggested_actions(
                next_state,
                intent_result.intent,
                capabilities_summary,
            )
        suggested_actions = self._finalize_user_query_suggestions(
            suggested_actions,
            state=next_state,
            intent=intent_result.intent,
            pending_action=context.pending_action,
            pending_data=context.pending_data,
            capabilities_summary=capabilities_summary,
        )

        # 12. Build metadata
        metadata = {
            "message_count": len(context.messages),
            "entities": intent_result.entities,
            "classified_intent": intent_result.intent.value,
            "classified_confidence": intent_result.confidence,
            "routing_path": routing_decision.path.value,
            "routing_score": routing_decision.score,
            "routing_signals": routing_decision.signals,
            "pending_interrupted": pending_interrupted,
            "resume_checkpoint": bool(resume_actions),
            "parked_task_available": self._peek_parked_task(context) is not None,
            "response_valid": validation.valid,
            "validation_issues": [issue.code for issue in validation.issues],
            "validator_replaced": validator_replaced,
            "response_source": response_source,
            "memory_summary": memory_snapshot.get("summary", ""),
            "memory_facts": memory_snapshot.get("facts", {}),
            "memory_recent_changes": memory_snapshot.get("recent_changes", []),
        }
        if handler_result is not None:
            metadata["routed_handler"] = intent_result.intent.value
        if agent_orchestrator_result is not None:
            metadata["agent_orchestration"] = {
                "handled": agent_orchestrator_result.handled,
                "reason": agent_orchestrator_result.reason,
            }
            metadata.update(agent_orchestrator_result.metadata)
        if handler_result is not None and handler_result.metadata:
            metadata.update(handler_result.metadata)
        metadata.update(response_rewrite_metadata)
        metadata.update(turn_ticketing_status)
        metadata = self._apply_ticket_metadata_contract(metadata)
        self._log_processing_stage(
            "turn_completed",
            request=request,
            context=context,
            intent_result=intent_result,
            extra={
                "response_source": str(response_source),
                "next_state": str(next_state.value),
                "validation_ok": bool(validation.valid),
                "validator_replaced": bool(validator_replaced),
                "ticket_created": bool(metadata.get("ticket_created", False)),
                "db_session_available": db_session is not None,
            },
        )

        # 13. Build response
        return ChatResponse(
            session_id=request.session_id,
            message=response_text,
            intent=intent_result.intent,
            confidence=intent_result.confidence,
            state=next_state,
            suggested_actions=suggested_actions,
            metadata=metadata,
        )

    @staticmethod
    def _trace_preview(value: Any, max_chars: int = 300) -> str:
        text = str(value or "").strip()
        if len(text) <= max_chars:
            return text
        omitted = len(text) - max_chars
        return f"{text[:max_chars]}...[TRUNCATED:{omitted}]"

    def _log_processing_stage(
        self,
        stage: str,
        *,
        request: ChatRequest | None = None,
        context: ConversationContext | None = None,
        intent_result: IntentResult | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        request_meta = request.metadata if request is not None and isinstance(request.metadata, dict) else {}
        trace_id = str(request_meta.get("trace_id") or "").strip()
        turn_trace_id = str(request_meta.get("turn_trace_id") or "").strip()
        session_id = str(request.session_id or "").strip() if request is not None else ""
        hotel_code = (
            str((context.hotel_code if context is not None else request.hotel_code) or "").strip()
            if request is not None or context is not None
            else ""
        )
        channel = (
            str(
                (
                    context.channel
                    if context is not None
                    else (request.channel or request_meta.get("channel") if request is not None else "")
                )
                or ""
            ).strip()
        )
        payload: dict[str, Any] = {
            "stage": str(stage or "").strip() or "unknown",
            "request_message_preview": self._trace_preview(
                request.message if request is not None else "",
                260,
            ),
            "state": str(getattr(context.state, "value", context.state) if context is not None else ""),
            "pending_action": str(context.pending_action or "") if context is not None else "",
            "intent": (
                str(getattr(intent_result.intent, "value", intent_result.intent) or "")
                if intent_result is not None
                else ""
            ),
            "intent_confidence": (
                float(intent_result.confidence)
                if intent_result is not None
                else None
            ),
        }
        if extra:
            payload["extra"] = extra

        backend_trace_service.log_event(
            "chat_service_stage",
            payload,
            trace_id=trace_id,
            turn_trace_id=turn_trace_id,
            session_id=session_id,
            hotel_code=hotel_code,
            channel=channel,
            endpoint="/api/chat/message",
            method="POST",
            component="services.chat_service",
        )
        everything_trace_service.log_event(
            "chat_service_stage",
            {
                **payload,
                "request_metadata": request_meta,
                "request_message_full": str(request.message or "") if request is not None else "",
                "context": {
                    "state": str(getattr(context.state, "value", context.state) if context is not None else ""),
                    "pending_action": str(context.pending_action or "") if context is not None else "",
                    "pending_data": dict(context.pending_data) if context is not None and isinstance(context.pending_data, dict) else {},
                    "message_count": len(context.messages) if context is not None and isinstance(context.messages, list) else 0,
                },
            },
            trace_id=trace_id,
            turn_trace_id=turn_trace_id,
            session_id=session_id,
            hotel_code=hotel_code,
            channel=channel,
            endpoint="/api/chat/message",
            method="POST",
            component="services.chat_service",
        )
        if turn_trace_id:
            turn_diagnostics_service.log_event(f"chat_service_{stage}", payload)

    def _normalize_service_identifier(self, value: Any) -> str:
        text = str(value or "").strip().lower()
        if not text:
            return ""
        text = re.sub(r"[^a-z0-9]+", "_", text)
        text = re.sub(r"_+", "_", text).strip("_")
        return text




    @staticmethod
    def _parse_orchestration_intent(raw_intent: str, fallback: IntentType = IntentType.FAQ) -> IntentType:
        normalized = str(raw_intent or "").strip().lower().replace(" ", "_")
        mapping = {
            "greeting": IntentType.GREETING,
            "faq": IntentType.FAQ,
            "order_food": IntentType.ORDER_FOOD,
            "food_order": IntentType.ORDER_FOOD,
            "table_booking": IntentType.TABLE_BOOKING,
            "room_booking": IntentType.GENERAL_SERVICE,
            "stay_booking": IntentType.GENERAL_SERVICE,
            "reservation": IntentType.GENERAL_SERVICE,
            "room_service": IntentType.ROOM_SERVICE,
            "complaint": IntentType.COMPLAINT,
            "human_request": IntentType.HUMAN_REQUEST,
            "confirmation_yes": IntentType.CONFIRMATION_YES,
            "confirmation_no": IntentType.CONFIRMATION_NO,
            "unclear": IntentType.UNCLEAR,
            "out_of_scope": IntentType.OUT_OF_SCOPE,
        }
        return mapping.get(normalized, fallback)

    def _intent_from_service_id(
        self,
        *,
        service_id: str,
        capabilities_summary: dict[str, Any],
        fallback: IntentType = IntentType.FAQ,
    ) -> IntentType:
        normalized = self._normalize_service_identifier(service_id)
        if not normalized:
            return fallback
        service_catalog = capabilities_summary.get("service_catalog", [])
        service_row = None
        if isinstance(service_catalog, list):
            for row in service_catalog:
                if not isinstance(row, dict):
                    continue
                if self._normalize_service_identifier(row.get("id")) == normalized:
                    service_row = row
                    break
        if not isinstance(service_row, dict):
            service_row = {"id": normalized, "name": normalized}

        prompt_pack = service_row.get("service_prompt_pack")
        if not isinstance(prompt_pack, dict):
            prompt_pack = config_service.get_service_prompt_pack(normalized)
        if not isinstance(prompt_pack, dict):
            prompt_pack = {}
        profile = str(prompt_pack.get("profile") or "").strip().lower()
        semantic_text = " ".join(
            [
                str(service_row.get("id") or ""),
                str(service_row.get("name") or ""),
                str(service_row.get("type") or ""),
                str(service_row.get("description") or ""),
                profile,
            ]
        ).strip().lower()

        if any(token in semantic_text for token in ("complaint", "maintenance", "housekeeping")):
            return IntentType.COMPLAINT
        if any(token in semantic_text for token in ("handoff", "human", "escalation", "staff")):
            return IntentType.HUMAN_REQUEST
        if any(token in semantic_text for token in ("food", "dining", "menu", "restaurant_order")):
            return IntentType.ORDER_FOOD
        if any(token in semantic_text for token in ("room_service", "housekeeping", "maintenance")):
            return IntentType.ROOM_SERVICE
        if any(token in semantic_text for token in ("table",)) and "room" not in semantic_text:
            return IntentType.TABLE_BOOKING
        if any(token in semantic_text for token in ("room_booking", "booking", "reservation", "room_discovery")):
            return IntentType.GENERAL_SERVICE
        return fallback

    def _service_name_from_id(
        self,
        *,
        service_id: str,
        capabilities_summary: dict[str, Any],
    ) -> str:
        normalized = self._normalize_service_identifier(service_id)
        if not normalized:
            return ""
        service_catalog = capabilities_summary.get("service_catalog", [])
        if not isinstance(service_catalog, list):
            service_catalog = []
        for row in service_catalog:
            if not isinstance(row, dict):
                continue
            if self._normalize_service_identifier(row.get("id")) != normalized:
                continue
            return str(row.get("name") or row.get("id") or normalized).strip()
        return normalized

    def _service_llm_label_for_service(
        self,
        *,
        service_id: str,
        capabilities_summary: dict[str, Any],
        effective_intent: IntentType | None = None,
        fallback: str = "main",
    ) -> str:
        normalized = self._normalize_service_identifier(service_id)
        if not normalized:
            return "complain" if effective_intent == IntentType.COMPLAINT else fallback

        # If the LLM hallucinated a complaint-flavored service ID for a non-complaint request,
        # treat it as main orchestrator rather than labeling the response as "complain agent".
        complaint_service_ids = {"complain", "complaint", "complaint_service", "complain_service"}
        if normalized in complaint_service_ids:
            if effective_intent == IntentType.COMPLAINT:
                return "complain"
            # Non-complaint effective_intent with complaint service ID → LLM hallucinated service ID
            return fallback

        # effective_intent is the LLM's authoritative decision — it is the only signal used here.
        # mapped_intent (keyword scan of service description) is NOT used because service descriptions
        # may contain words like "issue" or "maintenance" that have nothing to do with the guest's intent.
        if effective_intent == IntentType.COMPLAINT:
            return "complain"

        service_name = self._service_name_from_id(
            service_id=normalized,
            capabilities_summary=capabilities_summary,
        )
        return service_name or normalized

    @staticmethod
    def _order_text_covers(new_text: str, previous_text: str) -> bool:
        """Does this order line already include everything the previous one did?

        Compares two strings the BOT wrote about the same order, never guest
        language. When the agent resends the whole cart the new line already
        lists the earlier dishes and can replace the old one; when it sends only
        the dish just added, the caller merges instead so the kitchen does not
        lose what it is already cooking.
        """
        def _words(text: str) -> set[str]:
            return {
                w for w in re.findall(r"[a-z]{3,}", str(text or "").lower())
                if w not in {"room", "for", "order", "the", "and", "dining", "with"}
            }

        prev = _words(previous_text)
        if not prev:
            return True
        return prev.issubset(_words(new_text))

    @staticmethod
    def _is_waiting_state_for_user_input(state: ConversationState) -> bool:
        return state in {
            ConversationState.AWAITING_CONFIRMATION,
            ConversationState.AWAITING_INFO,
            ConversationState.AWAITING_SELECTION,
        }


    _RB_ROOM_TARGETS = {"room_booking_request", "room_stay_booking", "room_booking"}

    def _rb_active_property_ids(self, pd: dict | None) -> list[str]:
        if not isinstance(pd, dict):
            return []
        seen: set[str] = set()
        normalized: list[str] = []
        for item in list(pd.get("_active_property_ids") or pd.get("selected_property_ids") or []):
            key = self._normalize_service_identifier(item)
            if not key or key in seen:
                continue
            seen.add(key)
            normalized.append(key)
        return normalized

    def _rb_active_property_names(self, pd: dict | None) -> list[str]:
        if not isinstance(pd, dict):
            return []
        names = [
            str(name).strip()
            for name in list(pd.get("_active_property_names") or [])
            if str(name).strip()
        ]
        if names:
            return names
        name_map = pd.get("_property_name_map") if isinstance(pd.get("_property_name_map"), dict) else {}
        resolved: list[str] = []
        for property_id in self._rb_active_property_ids(pd):
            text = str(name_map.get(property_id) or "").strip()
            if text:
                resolved.append(text)
        return resolved

    def _rb_scope_selection(self, pd: dict | None) -> tuple[str, str, list[str]]:
        if not isinstance(pd, dict):
            return "", "", []
        selected_id = self._normalize_service_identifier(
            pd.get("_selected_property_scope_id") or pd.get("selected_property_scope_id")
        )
        selected_ids_raw = pd.get("_selected_property_scope_ids") or pd.get("selected_property_scope_ids") or []
        if isinstance(selected_ids_raw, str):
            selected_ids_raw = [selected_ids_raw]
        selected_ids: list[str] = []
        seen: set[str] = set()
        for item in list(selected_ids_raw or []):
            key = self._normalize_service_identifier(item)
            if not key or key in seen:
                continue
            seen.add(key)
            selected_ids.append(key)
        selected_name = str(
            pd.get("_selected_property_scope_name") or pd.get("selected_property_scope_name") or ""
        ).strip()
        if selected_id:
            return selected_name, selected_id, [selected_id]
        return selected_name, "", selected_ids

    def _rb_resolve_property_name(self, pd: dict | None, property_id: str, fallback: str = "") -> str:
        property_key = self._normalize_service_identifier(property_id)
        if property_key and isinstance(pd, dict):
            name_map = pd.get("_property_name_map") if isinstance(pd.get("_property_name_map"), dict) else {}
            text = str(name_map.get(property_key) or "").strip()
            if text:
                return text
        return str(fallback or "").strip()

    def _rb_needs_property_choice(self, pd: dict | None) -> bool:
        active_ids = self._rb_active_property_ids(pd)
        return len(active_ids) != 1

    @staticmethod
    def _rb_join_property_names(names: list[str]) -> str:
        clean = [str(name).strip() for name in names if str(name).strip()]
        if not clean:
            return ""
        if len(clean) == 1:
            return f"**{clean[0]}**"
        if len(clean) == 2:
            return f"**{clean[0]}** or **{clean[1]}**"
        return ", ".join(f"**{name}**" for name in clean[:-1]) + f", or **{clean[-1]}**"

    def _rb_property_choice_reply(self, pd: dict | None) -> str:
        names = self._rb_active_property_names(pd)
        if names:
            return f"Which property would you like to book: {self._rb_join_property_names(names)}?"
        return "Which property would you like to book?"

    def _rb_scope(self, context: ConversationContext, pd: dict | None) -> tuple[str, str, list[str]]:
        """(property_name, scope_key, scope_ids) for the property currently in focus."""
        active_ids = self._rb_active_property_ids(pd)
        active_names = self._rb_active_property_names(pd)
        selected_name, selected_id, selected_ids = self._rb_scope_selection(pd)
        scope_ids = list(active_ids)
        prop = ""
        # prop MUST reflect the CURRENT active scope, NOT the last-booked property.
        # context.booking_property_name lingers across turns/conversations, so reading
        # it first mislabels a freshly-explored property — e.g. exploring Digha but the
        # booking reply/ticket said "Lansdowne" from an earlier booking this session.
        if len(active_ids) == 1:
            scope_ids = list(active_ids)
            prop = self._rb_resolve_property_name(pd, active_ids[0], active_names[0] if active_names else selected_name)
        elif len(active_names) == 1 and not active_ids:
            prop = active_names[0]
            scope_ids = []
        elif selected_id and not active_ids:
            scope_ids = [selected_id]
            prop = self._rb_resolve_property_name(
                pd,
                selected_id,
                selected_name or (active_names[0] if len(active_names) == 1 else ""),
            )
        elif len(selected_ids) == 1 and not active_ids:
            scope_ids = [selected_ids[0]]
            prop = self._rb_resolve_property_name(
                pd,
                selected_ids[0],
                selected_name or (active_names[0] if len(active_names) == 1 else ""),
            )
        elif len(active_names) == 1:
            prop = active_names[0]
        else:
            prop = str(getattr(context, "booking_property_name", "") or "").strip() if not active_ids else ""
        scope_name = self._normalize_service_identifier(prop.lower())
        scope_key = ("ids:" + ",".join(scope_ids)) if scope_ids else (("nm:" + scope_name) if scope_name else "")
        return prop, scope_key, scope_ids

    @staticmethod
    def _rb_dates_phrase(check_in: str, check_out: str) -> str:
        ci, co = str(check_in or "").strip(), str(check_out or "").strip()
        if ci and co:
            return f" ({ci} - {co})"
        if ci:
            return f" (from {ci})"
        return ""

    @staticmethod
    def _rb_dates_span(check_in: str, check_out: str) -> str:
        """A clean date span for the 'updated your booking ... to {span}' line — no
        'from' prefix (the sentence already says 'to') and no phantom range when only
        one date is known. 'to 22 December 2026' / 'to 20 December to 25 December'."""
        ci, co = str(check_in or "").strip(), str(check_out or "").strip()
        if ci and co and ci != co:
            return f"{ci} to {co}"
        return ci or co or "the new dates"

    def _rb_make_issue(self, prop: str, room: str, check_in: str, check_out: str) -> str:
        return (
            f"Guest wants to book {('the ' + room) if room else 'a room'}"
            + (f" at {prop}" if prop else "")
            + self._rb_dates_phrase(check_in, check_out)
            + "."
        )


    async def _rb_classify_intent(self, user_message: str, reply: str) -> str:
        """Room-lane intent as ONE reliable token: 'book' | 'change_dates' | 'none'.

        Single-word classification (proven reliable) — a JSON variant misfired on
        clear 'book a room' turns. Date extraction is a separate call (_rb_extract_dates).
        """
        import logging as _lg
        # A TEAM-ONLY HOTEL'S ROOM REPLY ALWAYS SHOWS ROOMS AND THE BUTTON, so the
        # "'book it'/'go ahead' when the reply shows rooms" clause read any short
        # message as BOOK: Mango's "Premium Grove View" chip and a typed "Perium"
        # were BOOK 5/5 on the logged inputs (7 Oct). Hotels with a booking engine
        # get the prompt unchanged, byte for byte.
        _team_only_line = (
            "A room name, a single word, a typo or an 'ok' on its own is NONE - the guest is choosing what to read "
            "about, not asking to book; 'book it' / 'go ahead' are BOOK only when the guest's own message says so.\n"
            if self._rb_team_only_hotel() else ""
        )
        # PRICE only where the hotel's booking engine can show live rates (Mango):
        # the lane then answers with one "Check live rates" button. Replayed on a
        # logged Mango input: 7/7 price/availability asks PRICE, extra bed /
        # parking / hall price / room-name chip stay NONE, BOOK and CHANGEDATES
        # unchanged (4 runs each, 8 Oct).
        _answers = "BOOK, CHANGEDATES, or NONE."
        if _team_only_line and self._rb_live_rates_enabled():
            # "do you have connecting rooms" and "are accessible rooms available?"
            # read as room availability and got the live-rates reply (Mango, 9 Oct):
            # availability means free rooms for DATES, never which rooms exist.
            _team_only_line += (
                "PRICE - the guest asks what a ROOM costs - any question about room price, rate or tariff, even a "
                "single word ('what is the price', 'price?', 'tariff?', 'rates?', 'room rates for 10-12 Nov', 'how "
                "much for 2 nights') - or whether rooms are free for particular DATES or a stay ('is a room "
                "available on 15 Nov', 'do you have rooms this weekend'). For this hotel a room price or "
                "date-availability question is PRICE, not NONE. Asking whether the hotel HAS a kind of room or a "
                "room feature - connecting rooms, accessible rooms, twin beds, a view, a balcony, non-smoking - is "
                "NONE, even when it says 'available'. The price or availability of anything that is not a room "
                "(extra bed, parking, spa, dining, banquet hall, airport pickup) is NONE.\n"
            )
            _answers = "BOOK, CHANGEDATES, PRICE, or NONE."
        # ADDROOM only for a team-only hotel (Mango): its reply invites "add another
        # room, just tell me here", and that ask was read as BOOK, so the guest got
        # the same sentence back and the ticket never heard of the extra room.
        if _team_only_line:
            _team_only_line += (
                "ADDROOM - the guest wants one more room (or more rooms) added to the booking request they "
                "already made ('add another room', 'i need one more room', 'add 2 more rooms to my booking'). "
                "An extra BED, a different room type, or a first booking is not ADDROOM.\n"
            )
            _answers = _answers.replace(" or NONE.", " ADDROOM, or NONE.")
        try:
            raw = await llm_client.chat(
                messages=[
                    {"role": "system", "content": (
                        "Classify a hotel guest's latest message in the room-booking lane. Reply with EXACTLY one "
                        "word:\n"
                        "BOOK - an explicit request to book/reserve a room ('book a room', 'book the suite', 'i want "
                        "to book', 'reserve a room', or 'book it'/'go ahead' when the reply shows rooms or a booking "
                        "link). Booking a NON-room thing (shuttle, transfer, parking, spa, dining) is NOT BOOK. A "
                        "QUESTION that merely ASKS ABOUT offers/discounts/packages/prices is NOT BOOK even if it says "
                        "'book' - e.g. 'any discount if I book direct?', 'what offers do you have', 'is there a deal "
                        "for booking directly', 'what's the price' - that is an information question (NONE).\n"
                        "CHANGEDATES - the guest wants to change/update the dates of a booking ('change the dates to "
                        "2-3 July', 'make it the 5th instead', 'move it to next week').\n"
                        "NONE - a plain info / browsing question (including questions about offers, discounts, "
                        "packages, prices, amenities), greeting, or anything else.\n"
                    ) + _team_only_line + "Answer with ONE word: " + _answers},
                    {"role": "user", "content": f"Guest's latest message: \"{user_message}\"\n\nAssistant's reply this turn: \"{str(reply or '')[:500]}\""},
                ],
                temperature=0.0,
                max_tokens=4,
            )
            v = str(raw or "").strip().lower()
            if "changedate" in v or "change_date" in v or "change date" in v:
                return "change_dates"
            if v.startswith("addroom") and "ADDROOM" in _answers:
                return "add_room"
            if v.startswith("book"):
                return "book"
            if v.startswith("price") and _answers.startswith("BOOK, CHANGEDATES, PRICE"):
                return "price"
            return "none"
        except Exception as _e:
            _lg.getLogger(__name__).warning("RB intent classify failed: %s", _e)
            return "none"

    async def _rb_extract_stay_dates_iso(self, user_message: str, earlier: list[str]) -> tuple[str, str]:
        """Stay dates as ISO (check_in, check_out) for the live-rates link; "" when not stated.

        The guest's latest message first, else the most recent dates in their earlier
        messages. Replayed 8 cases (8 Oct): 7 exact, "this weekend" read as Fri-Sun."""
        import json as _json, logging as _lg
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo as _Zone

        today = _dt.now(_Zone("Asia/Kolkata")).date()
        try:
            raw = await llm_client.chat(
                messages=[
                    {"role": "system", "content": (
                        f"Work out the stay dates a hotel guest is asking about. Today is {today.isoformat()} "
                        f"({today.strftime('%A')}). Return STRICT JSON only: "
                        "{\"check_in\": \"YYYY-MM-DD\", \"check_out\": \"YYYY-MM-DD\"}. Use the guest's latest "
                        "message first; if it names no dates, use the most recent dates in their earlier messages. "
                        "A date without a year is the next one on or after today. Nights ('2 nights from the 20th') "
                        "set check_out. Use \"\" for anything not stated - never invent a date."
                    )},
                    {"role": "user", "content": _json.dumps(
                        {"latest_message": str(user_message or ""), "earlier_guest_messages": list(earlier or [])[-4:]},
                        ensure_ascii=False,
                    )},
                ],
                temperature=0.0,
                max_tokens=60,
            )
            txt = str(raw or "")
            data = _json.loads(txt[txt.find("{"): txt.rfind("}") + 1]) if "{" in txt and "}" in txt else {}
            return str(data.get("check_in") or "").strip(), str(data.get("check_out") or "").strip()
        except Exception as _e:
            _lg.getLogger(__name__).warning("RB live-rates date extract failed: %s", _e)
            return "", ""

    async def _rb_extract_dates(self, user_message: str) -> tuple[str, str]:
        """Extract stated stay dates as (check_in, check_out), keeping the guest's wording."""
        import json as _json, logging as _lg
        try:
            raw = await llm_client.chat(
                messages=[
                    {"role": "system", "content": (
                        "Extract any stay dates the guest stated. Return STRICT JSON only: "
                        "{\"check_in\": string, \"check_out\": string}. Keep the guest's wording (e.g. '1 July', "
                        "'2026-07-02'); use \"\" when a date is not mentioned."
                    )},
                    {"role": "user", "content": str(user_message or "")},
                ],
                temperature=0.0,
                max_tokens=40,
            )
            txt = str(raw or "")
            data = _json.loads(txt[txt.find("{"): txt.rfind("}") + 1]) if "{" in txt and "}" in txt else {}
            return str(data.get("check_in") or "").strip(), str(data.get("check_out") or "").strip()
        except Exception as _e:
            _lg.getLogger(__name__).warning("RB date extract failed: %s", _e)
            return "", ""

    async def _rb_classify_confirm(self, user_message: str) -> str:
        """Classify a yes/no answer to the cancel-previous question -> 'yes'|'no'|'unclear'."""
        import logging as _lg
        try:
            raw = await llm_client.chat(
                messages=[
                    {"role": "system", "content": (
                        "The assistant just asked the guest whether to CANCEL their previous room-booking request "
                        "before setting up a new one. Classify the guest's reply as exactly one word: 'yes' (cancel "
                        "the previous one), 'no' (keep it), or 'unclear'."
                    )},
                    {"role": "user", "content": str(user_message or "")},
                ],
                temperature=0.0,
                max_tokens=2,
            )
            v = str(raw or "").strip().lower()
            if v.startswith("y"):
                return "yes"
            if v.startswith("n"):
                return "no"
            return "unclear"
        except Exception as _e:
            _lg.getLogger(__name__).warning("RB confirm classify failed: %s", _e)
            return "unclear"




    @staticmethod
    def _should_hide_intent_in_no_template_mode() -> bool:
        return bool(getattr(settings, "chat_no_template_response_mode", False))

    @staticmethod
    def _derive_orchestration_state(
        *,
        decision: OrchestrationDecision,
        current_state: ConversationState,
    ) -> ConversationState:
        if bool(decision.requires_human_handoff):
            return ConversationState.ESCALATED
        action = str(decision.action or "").strip().lower()
        pending_action = str(decision.pending_action or "").strip().lower()
        missing_fields = getattr(decision, "missing_fields", None) or []
        if (
            action == "collect_info"
            or pending_action.startswith("collect_")
            or pending_action.startswith("select_")
            or (action == "respond_only" and bool(missing_fields))
        ):
            return ConversationState.AWAITING_INFO
        if pending_action.startswith("confirm_"):
            return ConversationState.AWAITING_CONFIRMATION
        if action == "dispatch_handler":
            return current_state
        return ConversationState.IDLE

    async def _process_policy_verified_llm_message(
        self,
        *,
        request: ChatRequest,
        context: ConversationContext,
        capabilities_summary: dict[str, Any],
        routing_decision: Any,
        memory_snapshot: dict[str, Any],
        selected_phase_context: dict[str, Any] | None = None,
        db_session=None,
        prefetched_services: list[dict[str, Any]] | None = None,
    ) -> ChatResponse | None:
        """
        Strict no-template response path:
        - prefer orchestration/service LLM responses
        - fallback to pure full-KB LLM runtime
        - deterministic policy/validator gates remain enforced
        """
        try:
            orchestration_response = await self._process_llm_orchestration_message(
                request=request,
                context=context,
                capabilities_summary=capabilities_summary,
                routing_decision=routing_decision,
                memory_snapshot=memory_snapshot,
                selected_phase_context=selected_phase_context,
                db_session=db_session,
                prefetched_services=prefetched_services,
            )
            if orchestration_response is not None:
                return orchestration_response
        except Exception:
            logger.exception("no_template_orchestration_path_failed")

        return await self._process_pure_llm_message(
            request=request,
            context=context,
            capabilities_summary=capabilities_summary,
            routing_decision=routing_decision,
            memory_snapshot=memory_snapshot,
            db_session=db_session,
        )

    async def _process_pure_llm_message(
        self,
        *,
        request: ChatRequest,
        context: ConversationContext,
        capabilities_summary: dict[str, Any],
        routing_decision: Any,
        memory_snapshot: dict[str, Any],
        db_session=None,
    ) -> "ChatResponse | None":
        """Fallback path when orchestration is unavailable — defers to main flow."""
        return None

    async def _process_llm_orchestration_message(
        self,
        *,
        request: ChatRequest,
        context: ConversationContext,
        capabilities_summary: dict[str, Any],
        routing_decision: Any,
        memory_snapshot: dict[str, Any],
        selected_phase_context: dict[str, Any] | None = None,
        db_session=None,
        prefetched_services: list[dict[str, Any]] | None = None,
    ) -> ChatResponse | None:
        """
        Two-stage LLM orchestration runtime:
        - orchestrator LLM decides service/action/ticket intent
        - service LLM refines slot collection and response
        - deterministic policy gate validates all action execution
        """
        effective_user_message = str(request.message or "").strip()
        # Detect chip-click turns and set a transient hint on the context so
        # the main router can prefer staying inside the previously active
        # service (advisory, not a hard route lock). The flag is cleared by
        # _run_main_router after it consumes it.
        request_meta = request.metadata if isinstance(request.metadata, dict) else {}
        source_type = str(
            request_meta.get("ui_source_type")
            or request_meta.get("source_type")
            or ""
        ).strip().lower()
        if source_type == "suggested_action" and isinstance(context.pending_data, dict):
            context.pending_data["_chip_click_hint"] = True
            context.pending_data["_chip_click_origin_responder"] = str(
                request_meta.get("chip_origin_responder") or ""
            ).strip()
            context.pending_data["_chip_click_origin_service_id"] = str(
                request_meta.get("chip_origin_service_id") or ""
            ).strip()
            context.pending_data["_chip_click_target_service_id"] = str(
                request_meta.get("chip_target_service_id") or ""
            ).strip()
            context.pending_data["_chip_click_active_property_ids"] = list(
                request_meta.get("chip_active_property_ids") or []
            )
            context.pending_data["_chip_click_selected_book_ids"] = list(
                request_meta.get("chip_selected_book_ids") or []
            )
            context.pending_data["_chip_click_turn_trace_id"] = str(
                request_meta.get("chip_turn_trace_id") or ""
            ).strip()
            context.pending_data["_chip_click_evidence_fingerprint"] = str(
                request_meta.get("chip_evidence_fingerprint") or ""
            ).strip()
        # ── SCOPE IS JUDGED ALONGSIDE THE ANSWER, NOT INSIDE IT ─────────────
        # Started here, before the slow part of the turn, and read once the reply
        # exists - so it costs wall-clock time only when the whole turn is
        # already faster than the judge, which it never is. Kept off the
        # answerer on purpose: the answerer is what produced "Ulaanbaatar won the
        # 2011 Cricket World Cup", so it does not get to be the thing that
        # decides whether it should have answered.
        _scope_task = None
        if bool(getattr(settings, "chat_scope_judge_enabled", True)):
            try:
                _scope_task = asyncio.create_task(
                    llm_orchestration_service._scope_verdict(
                        user_message=effective_user_message,
                        last_bot_message=str(
                            llm_orchestration_service._last_message_by_role(
                                context, "assistant") or ""),
                        hotel_code=str(getattr(context, "hotel_code", "") or ""),
                    )
                )
            except Exception as _sc_exc:
                logger.warning("scope judge could not start (non-fatal): %r", _sc_exc)
        decision = await llm_orchestration_service.orchestrate_turn(
            user_message=effective_user_message,
            context=context,
            capabilities_summary=capabilities_summary,
            memory_snapshot=memory_snapshot,
            selected_phase_context=selected_phase_context,
            prefetched_services=prefetched_services,
            assistant_turn_id=str(request_meta.get("turn_trace_id") or "").strip(),
        )
        if decision is None:
            return None
        no_template_mode = bool(getattr(settings, "chat_no_template_response_mode", False))
        hide_intent_in_response = self._should_hide_intent_in_no_template_mode()
        if not str(decision.target_service_id or "").strip() and isinstance(decision.metadata, dict):
            metadata_service_id = str(
                decision.metadata.get("target_service_id")
                or decision.metadata.get("service_id")
                or decision.metadata.get("resolved_service_id")
                or ""
            ).strip()
            if metadata_service_id:
                decision.target_service_id = metadata_service_id

        pending_action_active = str(context.pending_action or "").strip().lower()
        pending_public = context.pending_data if isinstance(context.pending_data, dict) else {}
        pending_service_id_hint = self._normalize_service_identifier(
            pending_public.get("service_id")
            or pending_public.get("target_service_id")
            or pending_public.get("resolved_service_id")
            or ""
        )
        # Check if the pending service uses form-based ticketing — if so, skip
        # the entire confirmation detection path because form submission IS the
        # confirmation.  This prevents the bot from asking for "yes confirm" on
        # services that use an inline form.
        _pending_service_is_form_mode = False
        if pending_action_active.startswith("confirm_") and pending_service_id_hint:
            try:
                _pending_svc = config_service.get_service(pending_service_id_hint)
                if _pending_svc and str(_pending_svc.get("ticketing_mode") or "").strip().lower() == "form":
                    _pending_service_is_form_mode = True
            except Exception:
                pass

        if _pending_service_is_form_mode and pending_action_active.startswith("confirm_"):
            # Form-mode service somehow ended up in a confirm_* state (LLM
            # ignored instructions).  Clear the stale state so the orchestrator
            # re-routes to the service agent cleanly on this turn.
            context.pending_action = "collect_form_details"
            decision.metadata["form_mode_cleared_stale_confirm"] = pending_action_active
            pending_action_active = "collect_form_details"

        if pending_action_active.startswith("confirm_") and not _pending_service_is_form_mode:
            confirmation_intent = self._detect_confirmation_intent(
                re.sub(r"\s+", " ", str(effective_user_message or "").strip().lower())
            )
            if pending_service_id_hint and not str(decision.target_service_id or "").strip():
                decision.target_service_id = pending_service_id_hint
            # In no_template/orchestration mode the service agent handles confirmations
            # and returns action="create_ticket" directly — do NOT override its decision.
            if not no_template_mode and confirmation_intent == IntentType.CONFIRMATION_YES:
                decision.action = "dispatch_handler"
                decision.use_handler = True
                decision.handler_intent = IntentType.CONFIRMATION_YES.value
                decision.intent = IntentType.CONFIRMATION_YES.value
                decision.interrupt_pending = False
                decision.resume_pending = False
                decision.cancel_pending = False
                decision.pending_action = context.pending_action
                decision.response_text = ""
                decision.pending_data_updates = (
                    dict(decision.pending_data_updates)
                    if isinstance(decision.pending_data_updates, dict)
                    else {}
                )
                if pending_service_id_hint:
                    decision.pending_data_updates.setdefault("service_id", pending_service_id_hint)
                decision.metadata.setdefault("confirmation_continuity_forced", True)

        pending_interrupted = False
        if context.pending_action and bool(decision.interrupt_pending):
            parked = self._park_active_task(
                context=context,
                selected_phase_context=selected_phase_context or {},
                reason="llm_orchestration_topic_diversion",
                user_message=effective_user_message,
            )
            pending_interrupted = parked is not None

        if bool(decision.resume_pending) or str(decision.action or "").strip().lower() == "resume_pending":
            resumed = self._restore_parked_task(context)
            if resumed is not None:
                resumed_summary = str(resumed.get("summary") or "your previous request").strip()
                response_text = str(decision.response_text or "").strip() or (
                    f"Resuming {resumed_summary}. Please continue with the remaining details."
                )
                response_text, _ = await self._maybe_llm_rewrite_response(
                    response_text=response_text,
                    user_message=effective_user_message,
                    intent_result=IntentResult(intent=IntentType.FAQ, confidence=max(0.6, float(decision.confidence or 0.0))),
                    context=context,
                    capabilities_summary=capabilities_summary,
                    capability_check_allowed=True,
                    capability_reason="",
                    response_source="llm_orchestration",
                    validator_replaced=False,
                )
                assistant_metadata = {
                    "intent": IntentType.FAQ.value,
                    "confidence": max(0.6, float(decision.confidence or 0.0)),
                    "service_llm_label": "main",
                    "service_llm_confidence": max(0.6, float(decision.confidence or 0.0)),
                    "channel": context.channel,
                    "orchestration_mode": True,
                    "orchestration_action": "resume_pending",
                }
                context.add_message(MessageRole.ASSISTANT, response_text, metadata=assistant_metadata)
                conversation_memory_service.capture_assistant_message(
                    context,
                    response_text,
                    metadata=assistant_metadata,
                )
                await self._refresh_summary_async(context)
                await context_manager.save_context(context, db_session=db_session)
                memory_snapshot = conversation_memory_service.get_snapshot(context)
                return ChatResponse(
                    session_id=request.session_id,
                    message=response_text,
                    intent=None if hide_intent_in_response else IntentType.FAQ,
                    confidence=None if hide_intent_in_response else max(0.6, float(decision.confidence or 0.0)),
                    service_llm_label="main",
                    service_llm_confidence=max(0.6, float(decision.confidence or 0.0)),
                    state=context.state,
                    suggested_actions=self._build_contextual_suggested_actions(
                        context.state,
                        IntentType.FAQ,
                        context.pending_action,
                        capabilities_summary,
                        context.pending_data,
                    ),
                    metadata={
                        "message_count": len(context.messages),
                        "routing_path": ProcessingPath.SIMPLE.value,
                        "routing_score": 1.0,
                        "routing_signals": {"path": "llm_orchestration_resume_pending"},
                        "response_source": "llm_orchestration",
                        "orchestration_mode": True,
                        "service_llm_label": "main",
                        "service_llm_confidence": max(0.6, float(decision.confidence or 0.0)),
                        "memory_summary": memory_snapshot.get("summary", ""),
                        "memory_facts": memory_snapshot.get("facts", {}),
                    },
                )

        if bool(decision.cancel_pending) or str(decision.action or "").strip().lower() == "cancel_pending":
            canceled = self._cancel_parked_task(context)
            canceled_summary = str((canceled or {}).get("summary") or "that pending request").strip()
            response_text = str(decision.response_text or "").strip() or (
                f"Cancelled {canceled_summary}. What would you like to do next?"
            )
            response_text, _ = await self._maybe_llm_rewrite_response(
                response_text=response_text,
                user_message=effective_user_message,
                intent_result=IntentResult(intent=IntentType.FAQ, confidence=max(0.6, float(decision.confidence or 0.0))),
                context=context,
                capabilities_summary=capabilities_summary,
                capability_check_allowed=True,
                capability_reason="",
                response_source="llm_orchestration",
                validator_replaced=False,
            )
            context.state = ConversationState.IDLE
            assistant_metadata = {
                "intent": IntentType.FAQ.value,
                "confidence": max(0.6, float(decision.confidence or 0.0)),
                "service_llm_label": "main",
                "service_llm_confidence": max(0.6, float(decision.confidence or 0.0)),
                "channel": context.channel,
                "orchestration_mode": True,
                "orchestration_action": "cancel_pending",
            }
            context.add_message(MessageRole.ASSISTANT, response_text, metadata=assistant_metadata)
            conversation_memory_service.capture_assistant_message(
                context,
                response_text,
                metadata=assistant_metadata,
            )
            await self._refresh_summary_async(context)
            await context_manager.save_context(context, db_session=db_session)
            memory_snapshot = conversation_memory_service.get_snapshot(context)
            return ChatResponse(
                session_id=request.session_id,
                message=response_text,
                intent=None if hide_intent_in_response else IntentType.FAQ,
                confidence=None if hide_intent_in_response else max(0.6, float(decision.confidence or 0.0)),
                service_llm_label="main",
                service_llm_confidence=max(0.6, float(decision.confidence or 0.0)),
                state=ConversationState.IDLE,
                suggested_actions=config_service.get_quick_actions(limit=4),
                metadata={
                    "message_count": len(context.messages),
                    "routing_path": ProcessingPath.SIMPLE.value,
                    "routing_score": 1.0,
                    "routing_signals": {"path": "llm_orchestration_cancel_pending"},
                    "response_source": "llm_orchestration",
                    "orchestration_mode": True,
                    "service_llm_label": "main",
                    "service_llm_confidence": max(0.6, float(decision.confidence or 0.0)),
                    "memory_summary": memory_snapshot.get("summary", ""),
                    "memory_facts": memory_snapshot.get("facts", {}),
                },
            )

        policy_result = orchestration_policy_service.evaluate(
            decision=decision,
            context=context,
            capabilities_summary=capabilities_summary,
            selected_phase_context=selected_phase_context,
        )

        policy_override_applied = False
        if policy_result.override_response or not policy_result.action_allowed:
            override_payload = {
                "service_id": str(
                    ((policy_result.target_service or {}).get("id") if isinstance(policy_result.target_service, dict) else "")
                    or decision.target_service_id
                    or ""
                ).strip(),
                "service_name": str(
                    ((policy_result.target_service or {}).get("name") if isinstance(policy_result.target_service, dict) else "")
                    or decision.target_service_id
                    or "This service"
                ).strip(),
                "current_phase_id": str(policy_result.current_phase_id or "").strip(),
                "current_phase_name": str(policy_result.current_phase_name or "").strip(),
                "service_phase_id": str(policy_result.service_phase_id or "").strip(),
                "service_phase_name": str(policy_result.service_phase_name or "").strip(),
                "phase_service_unavailable": bool(policy_result.out_of_phase),
                "policy_blocked_reason": str(policy_result.blocked_reason or "").strip(),
                "ticket_skip_reason": str(policy_result.ticket_skip_reason or "").strip(),
                "suggested_actions": self._current_phase_service_names(
                    context=context,
                    capabilities_summary=capabilities_summary,
                )[:3] + ["Show available services", "Ask another question"],
            }
            override_intent = self._parse_orchestration_intent(
                decision.handler_intent or decision.intent,
                fallback=IntentType.FAQ,
            )
            override_text = await self._compose_policy_guardrail_response(
                user_message=effective_user_message,
                context=context,
                capabilities_summary=capabilities_summary,
                intent=override_intent,
                gate_payload=override_payload,
                fallback_text="",
                response_source="llm_orchestration_policy_gate",
            )
            decision.response_text = str(override_text or decision.response_text or "").strip()
            decision.use_handler = False
            decision.pending_action = None
            decision.pending_data_updates = {}
            policy_override_applied = True
            if policy_result.out_of_phase:
                decision.ticket = decision.ticket.model_copy(
                    update={
                        "required": False,
                        "ready_to_create": False,
                    }
                )

        # In orchestration/no_template mode the LLM controls everything —
        # intent mapping only causes validator interference. Use GENERAL_SERVICE.
        effective_intent = IntentType.GENERAL_SERVICE
        if bool(decision.requires_human_handoff):
            effective_intent = IntentType.HUMAN_REQUEST
        else:
            # Preserve explicit yes/no confirmation intent for in-flight
            # transactional continuations so handler dispatch gets the right signal.
            parsed_intent = self._parse_orchestration_intent(
                str(decision.handler_intent or decision.intent or ""),
                fallback=IntentType.GENERAL_SERVICE,
            )
            if parsed_intent in {IntentType.CONFIRMATION_YES, IntentType.CONFIRMATION_NO}:
                effective_intent = parsed_intent
        # Rule-based complaint signal detection removed — LLM orchestrator owns all intent decisions.
        # (previously overrode LLM intent to COMPLAINT based on keyword matching)

        # Sanitize target_service_id: if the LLM returned a complaint-flavored service ID
        # for a non-complaint request, clear it so routing falls back to the LLM response text.
        _complaint_service_ids = {"complain", "complaint", "complaint_service", "complain_service"}
        _raw_target_sid = self._normalize_service_identifier(str(decision.target_service_id or ""))
        if _raw_target_sid in _complaint_service_ids and effective_intent != IntentType.COMPLAINT:
            decision.target_service_id = None

        entities = dict(decision.metadata) if isinstance(decision.metadata, dict) else {}
        if decision.target_service_id:
            entities.setdefault("service_id", str(decision.target_service_id))
        if decision.missing_fields:
            entities.setdefault("missing_fields", list(decision.missing_fields))
        if bool(decision.ticket.required):
            entities.setdefault("ticketing_flow", True)
            if decision.ticket.reason:
                entities.setdefault("ticket_reason", decision.ticket.reason)
        intent_result = IntentResult(
            intent=effective_intent,
            confidence=max(0.0, min(1.0, float(decision.confidence or 0.0))),
            entities=entities,
            requires_confirmation=effective_intent in {IntentType.ORDER_FOOD, IntentType.TABLE_BOOKING},
        )

        dispatch_handlers = bool(getattr(settings, "chat_llm_orchestration_dispatch_handlers", True))
        if no_template_mode:
            dispatch_handlers = False
        prefer_llm_response = bool(getattr(settings, "chat_llm_orchestration_prefer_llm_response", True))
        prefer_llm_suggested_actions = bool(getattr(settings, "chat_llm_orchestration_prefer_llm_suggested_actions", True))
        handler_result: HandlerResult | None = None
        if (
            policy_result.action_allowed
            and dispatch_handlers
            and bool(decision.use_handler or str(decision.action or "").strip().lower() == "dispatch_handler")
        ):
            handler_result = await self._dispatch_to_handler(
                effective_user_message,
                intent_result,
                context,
                capabilities_summary,
                db_session,
            )

        internal_pending_entries = conversation_memory_service.internal_pending_entries(context.pending_data)
        existing_public_pending = {
            key: value
            for key, value in (context.pending_data or {}).items()
            if isinstance(key, str) and not key.startswith("_")
        }

        response_text = str(decision.response_text or "").strip()
        next_state = self._derive_orchestration_state(decision=decision, current_state=context.state)
        if handler_result is not None:
            handler_text = str(handler_result.response_text or "").strip()
            handler_metadata = handler_result.metadata if isinstance(handler_result.metadata, dict) else {}
            handler_pending_action = str(handler_result.pending_action or "").strip().lower()
            if not prefer_llm_response or not response_text:
                response_text = handler_text or response_text
            next_state = handler_result.next_state
            if handler_result.pending_action is not None:
                context.pending_action = handler_result.pending_action
            if handler_result.pending_data is not None:
                context.pending_data = conversation_memory_service.merge_with_internal(
                    handler_result.pending_data,
                    internal_pending_entries,
                )
            if handler_result.pending_action is None and next_state not in (
                ConversationState.AWAITING_CONFIRMATION,
                ConversationState.AWAITING_INFO,
                ConversationState.AWAITING_SELECTION,
                ConversationState.PROCESSING_ORDER,
            ):
                context.pending_action = None
                context.pending_data = conversation_memory_service.retain_safe_internal_entries(
                    context.pending_data,
                )
                context.pending_data.pop("_clarification_attempts", None)
            if handler_result.metadata.get("room_number"):
                context.room_number = str(handler_result.metadata["room_number"])
        else:
            pending_action_target = decision.pending_action
            if not pending_action_target and decision.missing_fields:
                first_missing = str(decision.missing_fields[0] or "").strip().lower()
                if first_missing:
                    pending_action_target = f"collect_{first_missing}"

            context.pending_action = pending_action_target
            merged_public_pending = dict(existing_public_pending)
            if isinstance(decision.pending_data_updates, dict):
                merged_public_pending.update(decision.pending_data_updates)

            if context.pending_action or merged_public_pending:
                context.pending_data = conversation_memory_service.merge_with_internal(
                    merged_public_pending,
                    internal_pending_entries,
                )
            else:
                context.pending_data = conversation_memory_service.merge_with_internal(
                    {},
                    internal_pending_entries,
                )
                context.pending_data.pop("_clarification_attempts", None)

        if bool(decision.requires_human_handoff):
            next_state = ConversationState.ESCALATED
            context.pending_action = None
            context.pending_data = conversation_memory_service.merge_with_internal(
                {},
                internal_pending_entries,
            )

        if context.pending_action and isinstance(context.pending_data, dict):
            resolved_pending_service_id = self._normalize_service_identifier(
                context.pending_data.get("service_id")
                or decision.target_service_id
                or pending_service_id_hint
                or ""
            )
            if resolved_pending_service_id:
                context.pending_data.setdefault("service_id", resolved_pending_service_id)

        ticket_meta: dict[str, Any] = {}
        if handler_result is None or not bool((handler_result.metadata or {}).get("ticket_created")):
            ticket_required = bool(decision.ticket.required)
            if ticket_required:
                ticket_allowed_now, ticket_skip_reason = self._can_create_orchestration_ticket_now(
                    decision=decision,
                    policy_result=policy_result,
                    next_state=next_state,
                    pending_action=context.pending_action,
                )
                # ── THE SAME REQUEST, RAISED TWICE ──────────────────────────
                # "yes" raised an Engineering ticket for the AC; the very next
                # turn ("yes lets continue") raised a byte-identical second one,
                # 53 seconds later. Two crews get sent to the same room for the
                # same fault. Nothing downstream compares a new ticket against
                # what this session already has open, so this does.
                # Deliberately EXACT-match only — same issue text, same
                # department, still open. A guest who genuinely needs more
                # towels an hour later writes a different line and is unaffected.
                if ticket_allowed_now:
                    try:
                        _new_issue = " ".join(
                            str(decision.ticket.issue or "").lower().split()
                        )
                        _new_dept = str(
                            getattr(decision.ticket, "department", "") or ""
                        ).strip().lower()
                        for _t in (await ticketing_service.list_session_tickets(
                            str(getattr(context, "session_id", "") or "")
                        ) if _new_issue else []):
                            # list_session_tickets already excludes cancelled ones
                            # and does not return a status field at all, so every
                            # row here is live. (Reading a missing "status" as
                            # not-open would have skipped every row and this check
                            # would have quietly never fired.)
                            if " ".join(str(_t.get("issue") or "").lower().split()) != _new_issue:
                                continue
                            _old_dept = str(_t.get("department") or "").strip().lower()
                            if _new_dept and _old_dept and _new_dept != _old_dept:
                                continue
                            ticket_allowed_now = False
                            ticket_skip_reason = "duplicate_of_open_ticket"
                            logger.info(
                                "[TICKET] not raising a duplicate — %s is already open "
                                "for %r", _t.get("ticket_ref"), _new_issue[:60],
                            )
                            break
                    except Exception as _dup_exc:
                        logger.warning(
                            "[TICKET] duplicate check skipped (non-fatal): %r", _dup_exc
                        )
                # Room-booking tickets are handled by the dedicated SILENT path
                # (_maybe_create_silent_room_booking_ticket), keyed off the website
                # booking-button marker — so the website reply is never rewritten and
                # there is no double ticket. Skip the decision-ticket path for them.
                if ticket_allowed_now:
                    _is_room_booking_intent = (
                        self._normalize_service_identifier(str(decision.ticket.category or "")) == "room_booking"
                        or self._normalize_service_identifier(str(decision.ticket.sub_category or "")) == "room_booking"
                        or "<<SHOW_BOOKING_BUTTON" in str(response_text or "")
                    )
                    if _is_room_booking_intent:
                        ticket_allowed_now = False
                        ticket_skip_reason = "room_booking_silent_path"

                # ── ADDING TO AN ORDER ALREADY PLACED ────────────────────────
                # The in-room-dining cart is cumulative, so re-confirming after
                # adding a dish sent the WHOLE cart again as a SECOND ticket —
                # the kitchen received the first items twice — while the original
                # ticket kept the old, shorter list. Inside the edit window the
                # order can still be changed, so revise that ticket in place
                # rather than raising another one. Past the window it falls
                # through and becomes a new order, which is correct: the kitchen
                # has already started the first one.
                # ── THE KITCHEN'S COPY OF THE ORDER IS OWNED BY CODE ─────────
                # The model writes its own ticket.issue from whatever it recalls
                # of the conversation, and that text was reaching the kitchen: on
                # one turn it listed only the dish just added (the earlier food
                # silently dropped off the ticket), on another it re-listed food
                # already being cooked (a second ticket to make it again). The
                # validated cart is the only reliable record of the order, so the
                # item list is composed from it here — the one place that writes
                # to BOTH KePSLA and the external dashboard — and the model's
                # version of this field is overridden. The model still writes the
                # guest-facing message; it just no longer decides what is cooked.
                if ticket_allowed_now:
                    try:
                        _win = int(getattr(settings, "ird_edit_window_minutes", 5) or 0)
                        # ── WHOSE TURN IS THIS? ASK THE SERVICE, NOT THE WORDS ──
                        # This decided "is this a food turn" by looking for
                        # "dining"/"food"/"beverage" inside the model's own
                        # free-text category. On a HOUSEKEEPING turn - "yes and
                        # can i also get 2 towels" - that string mentioned food,
                        # so this block ran, UPDATED THE OPEN BURGER TICKET in
                        # place, and marked the turn as "ticket created". The
                        # towels were never raised, the accepted Tiramisu was
                        # never raised, and the safety net stood down because a
                        # ticket had supposedly been written. The guest was told
                        # both had gone to two teams. Neither had.
                        #
                        # The service that handled the turn is known for certain,
                        # so there is nothing to guess. The old text test is kept
                        # ONLY for turns where no service was resolved, so a
                        # genuine placement decided by the orchestrator alone
                        # still behaves exactly as before.
                        _svc_for_food = str(
                            getattr(decision, "target_service_id", "") or "").strip()
                        _is_food_turn = llm_orchestration_service._is_sarovar_ird(
                            context, _svc_for_food)
                        if not _svc_for_food:
                            _cat_blob = (
                                f"{decision.ticket.category} {decision.ticket.sub_category}"
                            ).lower()
                            _is_food_turn = any(
                                m in _cat_blob for m in ("dining", "food", "beverage"))
                        elif not _is_food_turn:
                            logger.info(
                                "[IRD] not touching the food order — this turn "
                                "belongs to %r", _svc_for_food)
                        if _is_food_turn:
                            _sid = str(getattr(context, "session_id", "") or "")
                            _pd = (
                                context.pending_data
                                if isinstance(context.pending_data, dict) else {}
                            )
                            # Is the order already placed still open to changes?
                            # An order the guest has explicitly moved on from is
                            # NOT a candidate to merge into, however recent and
                            # however open it still is. Without this the lookup
                            # finds the order they just closed and the "new order"
                            # they asked for lands on the old ticket - the exact
                            # bug this whole change exists to fix.
                            _closed_refs = [
                                str(r).strip()
                                for r in (_pd.get("_ird_closed_order_refs") or [])
                                if str(r or "").strip()
                            ]
                            _recent = (
                                await ticketing_service.find_recent_open_ticket(
                                    session_id=_sid,
                                    category_markers=("dining", "food", "beverage"),
                                    within_minutes=_win,
                                    exclude_refs=_closed_refs,
                                )
                                if _win > 0 else None
                            )
                            _ref = str((_recent or {}).get("ticket_ref") or "").strip()

                            # THE ORDER THIS TICKET CARRIES IS DECIDED HERE, from
                            # the validated cart — never from the model's own
                            # ticket.issue, which listed only the dish just added
                            # on one turn (dropping food already ordered) and
                            # re-listed food already cooking on another (a second
                            # ticket to make it twice). Joining an open order
                            # carries the running list forward; once that order
                            # locks, the list starts again from this turn's cart
                            # so the new ticket holds ONLY the new dish.
                            _cart_now = [
                                l for l in (_pd.get("_ird_cart") or [])
                                if isinstance(l, dict) and str(l.get("item") or "").strip()
                            ]
                            _carried = [
                                l for l in (_pd.get("_ird_open_order_items") or [])
                                if isinstance(l, dict) and str(l.get("item") or "").strip()
                            ] if _ref else []
                            _by_name: dict[str, dict[str, Any]] = {}
                            for _l in list(_carried) + list(_cart_now):
                                _k = str(_l.get("item", "")).strip().lower()
                                if not _k:
                                    continue
                                if _k in _by_name:
                                    try:
                                        _by_name[_k]["qty"] = max(
                                            int(_by_name[_k].get("qty") or 1),
                                            int(_l.get("qty") or 1),
                                        )
                                    except (TypeError, ValueError):
                                        pass
                                else:
                                    _by_name[_k] = dict(_l)
                            _full_items = list(_by_name.values())

                            # ── A NEW ORDER CARRIES ONLY WHAT IS NEW ─────────────
                            # The model re-declares the WHOLE running order every
                            # turn, so once the previous order locks (the window
                            # passed, or the team started it) that cumulative list
                            # went to the kitchen again as a fresh ticket: a guest
                            # adding one dessert had both earlier soups re-cooked and
                            # re-charged. What has already been sent is on record, so
                            # subtract it and keep only the genuinely new quantities.
                            # Ordering a second helping still works — the model
                            # declares qty 2, 2 minus the 1 already sent leaves 1, so
                            # exactly one more is made.
                            # A CART RESET FOR A FRESH ORDER HAS NOTHING STALE IN IT.
                            # The subtraction below exists because the model
                            # re-declares its whole running list every turn, so
                            # after an order locks that list would be cooked again.
                            # When the guest asked for a separate order the cart was
                            # emptied at that moment, so everything in it was asked
                            # for afterwards - and subtracting the previous order
                            # would silently delete a repeat ("another dosa") and
                            # send the guest nothing.
                            _fresh = bool(_pd.get("_ird_fresh_order"))
                            # ── THE BOOK CARRIES ONE ORDER, SO NONE OF THIS RUNS ──
                            # Every line of merging and subtracting below exists for
                            # one reason: the model re-declares its whole cumulative
                            # running order every turn, so the code has to work out
                            # which part of it is actually new. A slot in the book is
                            # ONE order and already holds exactly what belongs on this
                            # ticket. Subtracting a cumulative ledger from a
                            # non-cumulative list would delete real food - a guest
                            # ordering a second dosa would get none.
                            _book_on = bool(getattr(settings, "ird_order_book_enabled", False))
                            if _book_on:
                                logger.info(
                                    "[IRD_BOOK] ticket carries the slot as written: %s",
                                    [l.get("item") for l in _full_items])
                            if not _book_on and not _ref and not _fresh:
                                _already = _pd.get("_ird_placed_items")
                                if isinstance(_already, dict) and _already:
                                    _delta: list[dict[str, Any]] = []
                                    for _l in _full_items:
                                        _k = str(_l.get("item", "")).strip().lower()
                                        try:
                                            _want = max(1, int(_l.get("qty") or 1))
                                        except (TypeError, ValueError):
                                            _want = 1
                                        try:
                                            _have = int(_already.get(_k) or 0)
                                        except (TypeError, ValueError):
                                            _have = 0
                                        if _want - _have > 0:
                                            _line = dict(_l)
                                            _line["qty"] = _want - _have
                                            _delta.append(_line)
                                    if len(_delta) != len(_full_items):
                                        logger.info(
                                            "[IRD] earlier order can no longer be changed — "
                                            "this ticket carries only the new items %s "
                                            "(guest's running list was %s)",
                                            [l.get("item") for l in _delta],
                                            [l.get("item") for l in _full_items],
                                        )
                                    _full_items = _delta
                                    if not _full_items:
                                        # Everything the guest listed is already with
                                        # the kitchen. A ticket here would be a pure
                                        # duplicate of food being made right now.
                                        logger.info(
                                            "[IRD] nothing new in this order — "
                                            "no second ticket raised"
                                        )
                                        ticket_allowed_now = False
                                        ticket_skip_reason = "ird_nothing_new_to_order"

                            if _full_items:
                                _room_txt = str(
                                    _pd.get("room_number")
                                    or getattr(context, "room_number", "")
                                    or ""
                                ).strip()
                                _order_text = "In-room dining order{}: {}".format(
                                    f" for room {_room_txt}" if _room_txt else "",
                                    ", ".join(
                                        # The variant is the difference between
                                        # ghee phulka and plain phulka. Left out
                                        # here, the book said "Phulka (Ghee)" and
                                        # ticket 11179 said "2 Phulka" - the
                                        # kitchen makes the wrong bread.
                                        "%s %s%s" % (
                                            l.get("qty") or 1, l.get("item"),
                                            " (%s)" % l["variant"]
                                            if l.get("variant") else "")
                                        for l in _full_items
                                    ),
                                )
                                _note_txt = str(_pd.get("_ird_order_note") or "").strip()
                                if _note_txt:
                                    _order_text += ". Guest note: %s" % _note_txt
                                if _order_text != str(decision.ticket.issue or "").strip():
                                    logger.info(
                                        "[IRD] order text taken from the validated cart, "
                                        "not the model: %r (model had %r)",
                                        _order_text[:120],
                                        str(decision.ticket.issue or "")[:80],
                                    )
                                decision.ticket.issue = _order_text
                                # THE CONFIRMATION IS OWNED BY THE SAME LIST THE KITCHEN
                                # GETS. The order-state pass hands over a placement to
                                # announce, but it only runs on the service-agent lane —
                                # when the orchestrator is the one that decides the
                                # ticket, nothing was handed over, so a guest whose order
                                # had just gone to the kitchen was still being asked to
                                # confirm it. Registering the placement here means every
                                # written food ticket gets the real confirmation, from
                                # the exact items the ticket carries.
                                decision.metadata.setdefault("ird_placement", {
                                    "order_record": {
                                        "items": [
                                            {
                                                "name": l.get("item"),
                                                "qty": int(l.get("qty") or 1),
                                                "price": l.get("price"),
                                                "allergens": l.get("allergens"),
                                            }
                                            for l in _full_items
                                        ],
                                        "room": _room_txt,
                                        "status": "placed",
                                    },
                                    "summary": ", ".join(
                                        f"{l.get('qty') or 1} {l.get('item')}"
                                        for l in _full_items
                                    ),
                                    "room": _room_txt,
                                })
                                if isinstance(context.pending_data, dict):
                                    context.pending_data["_ird_open_order_items"] = _full_items
                                if isinstance(decision.pending_data_updates, dict):
                                    decision.pending_data_updates[
                                        "_ird_open_order_items"
                                    ] = _full_items
                            else:
                                _order_text = str(decision.ticket.issue or "").strip()

                            if _ref:
                                _res = await ticketing_service.append_ticket_comment(
                                    session_id=_sid,
                                    ticket_ref=_ref,
                                    note=f"ORDER UPDATED — now: {_order_text}",
                                    set_issue=_order_text,
                                    set_description=_order_text,
                                )
                                if getattr(_res, "success", False):
                                    logger.info(
                                        "[IRD] updated order ticket %s in place instead of "
                                        "raising a duplicate (window=%dm)", _ref, _win,
                                    )
                                    ticket_allowed_now = False
                                    ticket_skip_reason = "ird_existing_order_updated"
                                    ticket_meta = {
                                        "ticket_created": True,
                                        "ticket_id": _ref,
                                        "ticket_ref": _ref,
                                        "ticket_updated_existing": True,
                                        "ticket_status": "open",
                                        "ticket_summary": _order_text[:180],
                                    }
                                    decision.metadata["ticket_updated_existing"] = _ref
                                    if isinstance(context.pending_data, dict):
                                        context.pending_data["_ird_order_ticket_ref"] = _ref
                                    if isinstance(decision.pending_data_updates, dict):
                                        decision.pending_data_updates[
                                            "_ird_order_ticket_ref"
                                        ] = _ref
                    except Exception as _upd_exc:
                        logger.warning("IRD order-ticket update failed (non-fatal): %s", _upd_exc)

                if ticket_allowed_now:
                    generic_kb_request = bool(
                        getattr(decision.ticket, "generic_request", False)
                        or bool((decision.metadata or {}).get("generic_kb_request"))
                    )
                    issue_text = str(
                        decision.ticket.issue
                        or decision.ticket.reason
                        or response_text
                        or effective_user_message
                    ).strip()
                    # ── WHAT OF THIS DO STAFF ALREADY HAVE? ──────────────────
                    # The issue restates the whole conversation every turn, so
                    # adding one request re-raised every earlier one alongside
                    # it. Trimmed here, before the team split, so the split only
                    # ever divides work that is genuinely new.
                    _kept_new = await self._trim_already_raised(
                        context=context, issue_text=issue_text)
                    if _kept_new is not None and not _kept_new:
                        ticket_allowed_now = False
                        ticket_skip_reason = "work_already_raised_this_session"
                    elif _kept_new:
                        issue_text = _kept_new

                    # ── THE PLANNER DECIDES, IT NO LONGER MERELY ADVISES ──────
                    # It used to only refine a ticket the model had already
                    # decided to raise, so when it correctly found NOTHING the
                    # model's own ticket still went out - a club sandwich the bot
                    # had just called unavailable reached staff as ticket 11318,
                    # under a department left over from an earlier turn, and a
                    # room-cleaning request was raised twice on two turns because
                    # neither went past this check.
                    #
                    # An EMPTY plan now blocks the ticket. None does not: that
                    # means the planner could not decide (no departments loaded,
                    # a bad response, an exception) and the old behaviour stands,
                    # so a hiccup here can never swallow a real request.
                    _plan = await self._plan_tickets(
                        context=context,
                        current_reply=str(response_text or ""),
                        pre_arrival=(
                            str(getattr(policy_result, "current_phase_id", "") or "")
                            or str(policy_result.current_phase_name or "")
                        ).strip().lower().replace(" ", "_").replace("-", "_")
                        in {"pre_booking", "pre_checkin"},
                    )
                    if _plan is not None and not _plan:
                        logger.info(
                            "[TICKET_PLAN] nothing needs raising this turn — not "
                            "creating the ticket the model asked for (%r)",
                            str(issue_text)[:70])
                        ticket_allowed_now = False
                        ticket_skip_reason = "ticket_planner_found_no_work"

                if ticket_allowed_now:
                    category = str(
                        decision.ticket.category
                        or ("complaint" if effective_intent in {IntentType.COMPLAINT, IntentType.HUMAN_REQUEST} else "request")
                    ).strip().lower()
                    sub_category = str(
                        decision.ticket.sub_category
                        or self._normalize_service_identifier(decision.target_service_id)
                        or ("generic_kb_request" if generic_kb_request else "general_request")
                    ).strip().lower()
                    priority = str(decision.ticket.priority or "medium").strip().upper()
                    if priority not in {"LOW", "MEDIUM", "HIGH", "CRITICAL"}:
                        priority = "MEDIUM"
                    phase_text = str(
                        policy_result.current_phase_name
                        or (selected_phase_context or {}).get("selected_phase_name")
                        or "During Stay"
                    ).strip()
                    try:
                        _dept_id = str(decision.ticket.department_id or "").strip()
                        _dept_name = str(decision.ticket.department_name or "").strip()
                        ticket_message = str(effective_user_message or "").strip() or issue_text
                        if generic_kb_request:
                            ticket_message = issue_text or ticket_message
                        # ── DOES ANY OF THIS BELONG TO A SECOND TEAM? ──────────
                        # Asked BEFORE the first ticket is built, because the
                        # answer decides what the FIRST one says too. Asked after
                        # it, the first ticket had already gone out reading "Fix
                        # AC not cooling and provide 2 extra pillows" - Engineering
                        # holding Housekeeping's work, and both teams believing the
                        # pillows were theirs.
                        # ── ONE PLANNER DECIDES EVERY TICKET THIS TURN ────────
                        # It reads the conversation, the hotel's live teams and
                        # services and what is already open, and answers with one
                        # ticket per team that has real work. Replaces the old
                        # two-team split, which could only ever find a SECOND team
                        # and only ran once a ticket was already being raised.
                        # The first entry becomes this ticket; the rest are raised
                        # straight after. If it finds nothing, the existing
                        # behaviour stands untouched.
                        # Reuses the plan decided above - asking twice in one
                        # turn costs a second call and can answer differently.
                        _others: list[dict] = []
                        if _plan:
                            _want = str(_dept_name or "").strip().lower()
                            _mine = next(
                                (t for t in _plan
                                 if str(t.get("department") or "").strip().lower() == _want),
                                _plan[0])
                            _others = [t for t in _plan if t is not _mine]
                            if str(_mine.get("issue") or "").strip():
                                logger.info("[TICKET_PLAN] this ticket carries %r (was %r)",
                                            str(_mine["issue"])[:70], str(issue_text)[:70])
                                issue_text = str(_mine["issue"]).strip()
                            if str(_mine.get("department") or "").strip():
                                _dept_name = str(_mine["department"]).strip()
                                if str(_mine.get("department_id") or "").strip():
                                    _dept_id = str(_mine["department_id"]).strip()

                        # ── A FOOD TICKET IS PRINTED FROM THE ORDER, NOT THE CHAT ──
                        # The kitchen was being sent the guest-facing bubble:
                        # "**Your cart**\n• **Phulka** × 2 — ₹225 each\n\n**Total:
                        # ₹450**\n\nSending this to the kitchen now." - markdown,
                        # rupee totals and all - and the GHEE variant was missing
                        # from it, so the kitchen would have made plain phulka
                        # while the book correctly held Phulka (Ghee).
                        #
                        # The slot is the order. It was already being printed from
                        # the slot, but only AFTER a successful external write, so
                        # any turn the API refused kept the bubble. Do it here, before
                        # the payload is built, and the ticket is right whatever the
                        # API then does.
                        if (bool(getattr(settings, "ird_order_book_enabled", False))
                                and (decision.metadata or {}).get("ird_placement")):
                            try:
                                from services import ird_cart_book as _tbk
                                from services import ird_cart_store as _tbs

                                _thc = str(getattr(context, "hotel_code", "") or "")
                                _tsid = str(getattr(context, "session_id", "") or "")
                                _trow = (await _tbs.load(hotel_code=_thc, session_id=_tsid,
                                                         create=False)
                                         if _thc and _tsid else None)
                                if _trow:
                                    _tslots = [sl for sl in _tbk.slots_from_row(_trow)
                                               if sl.get("items")
                                               and not sl.get("ticket_ref")]
                                    if _tslots:
                                        _tslot = max(_tslots, key=lambda sl: sl["n"])
                                        _tdesc = _tbk.ticket_description(
                                            _tslot,
                                            room=str((context.pending_data or {}).get(
                                                "room_number") or ""))
                                        if _tdesc:
                                            logger.info(
                                                "[IRD_BOOK] food ticket printed from slot "
                                                "%d: %s", _tslot["n"], _tdesc[:90])
                                            issue_text = _tdesc
                            except Exception as _tx:
                                logger.warning("[IRD_BOOK] could not print the ticket "
                                               "from the order (non-fatal): %r", _tx)
                        payload = ticketing_service.build_lumira_ticket_payload(
                            context=context,
                            issue=issue_text,
                            message=ticket_message,
                            category=category,
                            sub_category=sub_category,
                            priority=priority,
                            phase=phase_text,
                            department_id=_dept_id,
                            department_manager=_dept_name,
                        )
                        if decision.target_service_id:
                            payload["service_id"] = str(decision.target_service_id).strip()
                        create_result = await ticketing_service.create_ticket(payload)
                        if create_result.success:
                            created_ticket_id = str(create_result.ticket_id or "").strip()
                            # ── THE OTHER TEAM GETS ITS OWN REQUEST, RIGHT AFTER ──
                            # A request reaches exactly one team, so a message
                            # covering two lost the second one silently: a broken
                            # AC and 2 extra pillows both went to Engineering and
                            # Housekeeping was never told, three times out of
                            # three. Telling the model not to bundle never held,
                            # because bundling was the only thing it could do.
                            # Now each remaining team is raised as its own ticket,
                            # one after the other - decided above, so that the
                            # first ticket could be trimmed to its own half before
                            # it went out. Sequential on purpose: the external API
                            # gives one ref per call, and a failure on the second
                            # must not cost us the first.
                            await self._raise_planned_tickets(
                                plan=_others, context=context,
                                guest_message=str(effective_user_message or ""),
                                sub_category=sub_category, priority=priority,
                                phase=phase_text)
                            # Remember a food ticket so the next dish the guest adds
                            # can be checked against it: still open and inside the
                            # edit window means it joins THIS order, otherwise the
                            # cart resets and it becomes a new one.
                            if isinstance(context.pending_data, dict) and any(
                                m in f"{category} {sub_category}"
                                for m in ("dining", "food", "beverage")
                            ):
                                context.pending_data["_ird_order_ticket_ref"] = created_ticket_id
                                # Also through pending_data_updates — that is the
                                # channel the IRD state actually persists on, and
                                # writing only to context.pending_data lost the ref
                                # between turns, so the next dish never knew which
                                # ticket to check and the lock never applied.
                                if isinstance(decision.pending_data_updates, dict):
                                    decision.pending_data_updates[
                                        "_ird_order_ticket_ref"
                                    ] = created_ticket_id
                            # Remember a room-booking ticket so a later "book a room" turn
                            # in the same conversation does not create a duplicate.
                            if isinstance(context.pending_data, dict) and (
                                self._normalize_service_identifier(category) == "room_booking"
                                or "<<SHOW_BOOKING_BUTTON" in str(response_text or "")
                            ):
                                context.pending_data["_room_booking_ticket_id"] = created_ticket_id
                            ticket_meta = {
                                "ticket_created": True,
                                "ticket_id": created_ticket_id,
                                "ticket_status": "open",
                                "ticket_category": category,
                                "ticket_sub_category": sub_category,
                                "ticket_priority": priority,
                                "ticket_source": "llm_orchestration",
                                "ticket_api_status_code": create_result.status_code,
                                "ticket_api_response": create_result.response,
                            }
                            # Update response_text after ticket creation.
                            # For booking-type tickets: build a summary confirmation.
                            # For all other tickets (complaints, lost items, etc.): use the LLM's own response_text
                            # or a generic fallback, but never expose internal ticket ids.
                            category_norm = self._normalize_service_identifier(category)
                            sub_category_norm = self._normalize_service_identifier(sub_category)
                            pending_public = (
                                context.pending_data if isinstance(context.pending_data, dict) else {}
                            )
                            # Only treat this as a booking when the guest DELIBERATELY
                            # chose a room or a booking date/time. Ambient stay dates
                            # (check_in / check_out / stay_*), which are auto-attached
                            # from a checked-in guest's booking, must NOT flag an
                            # ordinary during-stay request (towels, maintenance, etc.)
                            # as a "booking".
                            _has_booking_fields = bool(
                                pending_public.get("room_type")
                                or pending_public.get("room_name")
                                or pending_public.get("booking_date")
                                or pending_public.get("booking_time")
                            )
                            # "request" is NOT a booking — it is the DEFAULT category
                            # every ordinary ticket gets when nothing more specific
                            # applies. Listing it here sent housekeeping, maintenance
                            # and complaint tickets down the booking branch below,
                            # which DISCARDS the model's reply and substitutes the
                            # booking receipt. A guest reporting "the AC is not
                            # cooling" was answered "Your booking request has been
                            # received" — and because an AC fault carries no room
                            # type or dates, the receipt had no details either, so
                            # the good reply was traded for an empty wrong one.
                            # The sibling guard just above (ambient stay dates must
                            # not flag a towels/maintenance request as a booking) was
                            # this same bug found from the FIELDS side; the category
                            # side was left in place.
                            _is_booking_category = (
                                category_norm in {
                                    "room_booking",
                                    "table_booking",
                                    "spa_booking",
                                    "transport_booking",
                                }
                                or "booking" in category_norm
                                or "reservation" in category_norm
                            )
                            _is_booking_sub_category = (
                                "booking" in sub_category_norm
                                or "reservation" in sub_category_norm
                                or sub_category_norm in {
                                    "room_booking",
                                    "table_booking",
                                    "spa_booking",
                                    "transport_booking",
                                }
                            )
                            _is_booking_ticket = (
                                _is_booking_category
                                or _is_booking_sub_category
                                or _has_booking_fields
                            )
                            # Silent room-booking ticket: when the reply already carries the
                            # website booking-link marker, this is the "book on website" flow —
                            # the ticket is a backend-only side-effect, so KEEP the guest-visible
                            # reply (link + offers) exactly; never replace it with a summary.
                            _keep_website_reply = "<<SHOW_BOOKING_BUTTON" in str(response_text or "")
                            if _is_booking_ticket and not _keep_website_reply:
                                _pub = {k: v for k, v in (context.pending_data or {}).items() if not k.startswith("_")}
                                _parts = []
                                room_label = self._first_non_empty(
                                    _pub.get("room_type"),
                                    _pub.get("room_name"),
                                    _pub.get("service_name"),
                                )
                                check_in_value = self._first_non_empty(
                                    _pub.get("check_in"),
                                    _pub.get("stay_checkin_date"),
                                    _pub.get("check_in_date"),
                                    _pub.get("checkin_date"),
                                )
                                check_out_value = self._first_non_empty(
                                    _pub.get("check_out"),
                                    _pub.get("stay_checkout_date"),
                                    _pub.get("check_out_date"),
                                    _pub.get("checkout_date"),
                                )
                                guest_count_value = self._first_non_empty(
                                    _pub.get("guest_count"),
                                    _pub.get("party_size"),
                                    _pub.get("guests"),
                                )
                                if room_label:
                                    _parts.append(str(room_label))
                                if check_in_value:
                                    _parts.append(f"check-in: {check_in_value}")
                                if check_out_value:
                                    _parts.append(f"check-out: {check_out_value}")
                                if guest_count_value:
                                    _parts.append(f"{guest_count_value} guest(s)")
                                _summary = ", ".join(_parts)
                                response_text = (
                                    "Your booking request has been received. "
                                    + (f"Details: {_summary}. " if _summary else "")
                                    + "Our staff team will verify it and get back to you shortly."
                                )
                            else:
                                # Non-booking ticket: keep the LLM's response text when present.
                                _llm_response = response_text.strip()
                                if _llm_response:
                                    response_text = _llm_response
                                else:
                                    response_text = "Your request has been logged. Our team will follow up shortly."
                            # Booking/ticket is completed: clear active pending flow
                            # and remove stale service carry-over data.
                            completed_service_id = self._normalize_service_identifier(
                                decision.target_service_id
                                or (
                                    context.pending_data.get("service_id")
                                    if isinstance(context.pending_data, dict)
                                    else ""
                                )
                                or ""
                            )
                            context.pending_action = None
                            context.pending_data = conversation_memory_service.retain_safe_internal_entries(
                                context.pending_data,
                            )
                            context.pending_data.pop("_clarification_attempts", None)
                            if completed_service_id and isinstance(context.suspended_services, list):
                                context.suspended_services = [
                                    row
                                    for row in context.suspended_services
                                    if self._normalize_service_identifier((row or {}).get("service_id")) != completed_service_id
                                ]
                            if isinstance(context.suspended_services, list) and not context.suspended_services:
                                context.resume_prompt_sent = False
                            self._sync_active_task_snapshot(
                                context=context,
                                selected_phase_context=selected_phase_context or {},
                            )
                            next_state = ConversationState.IDLE
                        else:
                            ticket_meta = {
                                "ticket_created": False,
                                # THE HOTEL MAY STILL HAVE IT. The KePSLA row is
                                # written whether or not the external API answers,
                                # so an outage is not the same as a lost request -
                                # staff can see it on their dashboard. Carried
                                # separately from ticket_created so nothing that
                                # depends on a real external ref changes.
                                "ticket_on_dashboard": bool(
                                    getattr(create_result, "saved_to_dashboard", False)),
                                "ticket_create_failed": True,
                                "ticket_error": str(create_result.error or "ticket_create_failed"),
                                "ticket_source": "llm_orchestration",
                                "ticket_api_status_code": create_result.status_code,
                                "ticket_api_response": create_result.response,
                            }
                            # The ticket is persisted to the DB / KePSLA before the
                            # external push; an external-API failure (e.g. an
                            # unmapped property) must NOT surface an alarming
                            # "couldn't confirm" error to the guest. Acknowledge
                            # the request warmly — our team still receives it.
                            _llm_ack = str(response_text or "").strip()
                            response_text = _llm_ack or "Thanks — I've noted your request and our team will follow up with you shortly."
                            # ── THE SECOND TEAM STILL NEEDS TELLING ──────────
                            # Raising the other teams' tickets hung off
                            # create_result.success, i.e. off the EXTERNAL write.
                            # With that API down, a broken AC and 2 extra pillows
                            # split correctly into Engineering + Housekeeping, the
                            # Engineering ticket landed on the dashboard - and
                            # Housekeeping was silently dropped, which is the exact
                            # bug the split exists to prevent. If the first team's
                            # request reached staff, so must the rest.
                            if bool(getattr(create_result, "saved_to_dashboard", False)):
                                await self._raise_planned_tickets(
                                    plan=_others, context=context,
                                    guest_message=str(effective_user_message or ""),
                                    sub_category=sub_category, priority=priority,
                                    phase=phase_text)
                    except Exception as exc:
                        ticket_meta = {
                            "ticket_created": False,
                            "ticket_create_failed": True,
                            "ticket_error": str(exc),
                            "ticket_source": "llm_orchestration",
                        }
                elif not ticket_meta:
                    # Only when nothing was written at all. Updating an order already
                    # open takes the same "not allowed to create" exit as a genuine
                    # block — it deliberately raises no second ticket — so this used
                    # to overwrite that success with a skip, and every reader
                    # downstream was told no ticket existed when one had just been
                    # revised. `ticket_meta` being non-empty means a write happened.
                    ticket_meta = {
                        "ticket_created": False,
                        "ticket_create_skipped": True,
                        "ticket_create_skip_reason": ticket_skip_reason or policy_result.ticket_skip_reason or "ticket_policy_gate_blocked",
                        "ticket_source": "llm_orchestration",
                    }

        # ── A "YES" TO FOOD SURVIVES THE TURN GOING SOMEWHERE ELSE ───────────
        # "yes and can i also get 2 towels" carries two requests, and the router
        # must pick ONE lane. It picked Housekeeping - the towels won - so the
        # food lane never ran, nobody asked what the "yes" meant, and the
        # Tiramisu the guest had just accepted was lost. The same check inside
        # the food lane could not help: it was never reached.
        #
        # So it runs HERE, after whichever lane answered, and only when the
        # previous turn was the food lane - i.e. when an offer could plausibly
        # have been made. On any other turn it costs nothing.
        try:
            _pd_acc = context.pending_data if isinstance(context.pending_data, dict) else {}
            _svc_now = str(getattr(decision, "target_service_id", "") or "").strip().lower()
            # GATED ON "IS FOOD IN PLAY", not on the previous service. Keying it
            # to `_last_service_id == in_room_dining` was too narrow: the dessert
            # was offered TWO turns back, the turn between them was already
            # housekeeping, and the recovery never ran. A guest with an order in
            # the book is a guest who can be offered a dish, whichever lane
            # happens to answer next.
            _food_in_play = bool(
                (_pd_acc.get("_ird_cart") or [])
                or (_pd_acc.get("_ird_placed_orders") or [])
                or str(_pd_acc.get("_ird_order_ticket_ref") or "").strip()
            )
            if (bool(getattr(settings, "ird_order_book_enabled", False))
                    and _food_in_play and _svc_now != "in_room_dining"):
                from services import ird_cart_book as _abk
                from services import ird_cart_store as _abs

                _offer = llm_orchestration_service._last_message_by_role(context, "assistant")
                _took = await llm_orchestration_service._dishes_the_guest_just_accepted(
                    last_bot_message=str(_offer or ""),
                    guest_message=str(effective_user_message or ""))
                if _took:
                    _ahc = str(getattr(context, "hotel_code", "") or "")
                    _asid = str(getattr(context, "session_id", "") or "")
                    _arow = await _abs.load(hotel_code=_ahc, session_id=_asid,
                                            guest={"room_number": str(
                                                _pd_acc.get("room_number") or "")})
                    if _arow:
                        _aslots = _abk.slots_from_row(_arow)
                        # ── DO NOT ADD IT TWICE ──────────────────────────────
                        # This fires on the accept turn AND again on the turn
                        # that confirms it, and the model does not name the dish
                        # identically twice: an order ended up holding "Tiramisu"
                        # AND "Tiramisu dessert". Anything the order already has,
                        # under either name, is already handled.
                        _have_now = {
                            str(i.get("item", "")).strip().lower()
                            for sl in _aslots if sl.get("items")
                            for i in sl["items"]
                        }
                        _took = [
                            a for a in _took
                            if not any(
                                _h == a["item"].strip().lower()
                                or _h in a["item"].strip().lower()
                                or a["item"].strip().lower() in _h
                                for _h in _have_now)
                        ]
                        if not _took:
                            logger.info("[IRD_BOOK] accepted dish is already on the "
                                        "order — not adding it again")
                        _aslots, _achanged = _abk.apply_write(
                            _aslots, {"items": _took, "status": _abk.ONGOING} if _took else {},
                            now=_abk.now_local(),
                            window_minutes=int(getattr(
                                settings, "ird_edit_window_minutes", 5) or 5))
                        if _achanged:
                            await _abs.save_slots(
                                hotel_code=_ahc, session_id=_asid,
                                slots=[sl for sl in _aslots if sl["n"] in _achanged])
                            _abk.project_to_pending_data(_aslots, context.pending_data)
                            _names = ", ".join(
                                "%s%s" % (("%d x " % a["qty"]) if a["qty"] > 1 else "",
                                          a["item"]) for a in _took)
                            logger.info(
                                "[IRD_BOOK] the guest accepted %s on a turn the %r "
                                "lane answered — written to the cart so it is not "
                                "lost", _names, _svc_now or "other")
                            # And SAY so: the reply came from another lane and
                            # cannot know about it, so the guest would otherwise
                            # be offered the same dish again next turn.
                            response_text = (
                                "%s\n\nI've also added %s to your order — tell me "
                                "when you'd like it placed."
                                % (str(response_text or "").strip(), _names)
                            ).strip()
        except Exception as _acc2:
            logger.warning("[IRD_BOOK] cross-lane accepted-offer recovery skipped "
                           "(non-fatal): %r", _acc2)

        # ── IF IT TOLD THE GUEST STAFF WERE TOLD, STAFF GET TOLD ─────────────
        # The safety net behind ticket planning, and the answer to the worst
        # failure this system has had. A guest reported a leaking tap, an
        # uncleaned room and asked for a club sandwich; the router handed the
        # whole message to In-Room Dining, which has no tap and no mop, so it
        # emitted respond_only - no ticket path ran at all - and still replied
        # "I've passed this to Engineering and Housekeeping." Asked to confirm, it
        # said yes again. Nothing existed. Three teams' work was lost inside a
        # sentence.
        #
        # So the assistant's own words are treated as a request for a ticket: if
        # the reply asserts staff were told and nothing was raised this turn, the
        # planner reads the conversation and raises what is genuinely needed. It
        # costs one cheap check on the few turns that claim anything, and the
        # planner returns [] whenever the claim was about work already open.
        # ── THE SCOPE VERDICT IS ENFORCED HERE, IN CODE ─────────────────────
        # Read before the ticket net below, so a message the hotel should not be
        # answering cannot raise staff work on its way out either. A verdict of
        # None means the judge could not decide, and nothing changes.
        _scope_kind = ""
        if _scope_task is not None:
            try:
                _sv = await _scope_task
                _scope_kind = str((_sv or {}).get("kind") or "")
            except Exception as _sv_exc:
                logger.warning("scope judge failed (non-fatal): %r", _sv_exc)
        if _scope_kind and _scope_kind != "in_scope":
            _hotel_label = ""
            try:
                _hotel_label = str(
                    (capabilities_summary or {}).get("business_name") or ""
                ).strip()
            except Exception:
                _hotel_label = ""
            _refusal = llm_orchestration_service.scope_refusal_text(
                _scope_kind, hotel_name=_hotel_label,
                hotel_code=str(getattr(context, "hotel_code", "") or ""))
            # Hotels set up in the Pre-booking tab (T24 onward) word their own
            # refusals in the DB row `scope.refusal_<kind>`; with no row, the
            # sentence above stands. Zone, Mango and Sarovar are unchanged.
            from services import prebooking_settings_service as _pbs_scope

            if _pbs_scope.is_admin_managed(
                str(getattr(context, "hotel_code", "") or "").strip().lower()
            ):
                _db_refusal = await llm_orchestration_service._db_prompt_text(
                    f"scope.refusal_{_scope_kind}")
                if _db_refusal:
                    _refusal = _db_refusal
            logger.info(
                "[SCOPE_JUDGE] kind=%r subject=%r — replacing the reply and "
                "raising nothing (msg=%r)",
                _scope_kind, str((_sv or {}).get("subject") or "")[:40],
                str(effective_user_message or "")[:60],
            )
            response_text = _refusal
            # Nothing off-topic becomes staff work: skip the net entirely.
            _skip_ticket_net = True
        else:
            _skip_ticket_net = False

        try:
            _already = bool(
                _skip_ticket_net
                or (ticket_meta or {}).get("ticket_created")
                or (ticket_meta or {}).get("ticket_on_dashboard")
                or ((handler_result.metadata or {}) if handler_result is not None
                    else {}).get("ticket_created")
            )
            # ── THE PHASE GATE HAS TO HOLD HERE TOO ─────────────────────────
            # The router coerces an in-stay-only request to route=book before
            # arrival, and that worked - but the reply still said "the 5:00 AM
            # wake-up call is noted", this net read that claim, and raised the
            # in-stay tickets anyway. One turn, both lines in the log: the gate
            # fired, then the net walked straight round it and opened a wake-up
            # call and a laundry pickup for a stay a year away. A guard on one
            # path is not a guard.
            _net_phase = (
                str(getattr(policy_result, "current_phase_id", "") or "")
                or str(policy_result.current_phase_name or "")
            ).strip().lower().replace(" ", "_").replace("-", "_")
            _net_pre_arrival = _net_phase in {"pre_booking", "pre_checkin"}
            if (not _already and str(response_text or "").strip()
                    and self._is_ticketing_plugin_enabled()):
                if await llm_orchestration_service._reply_claims_staff_were_told(
                        reply_text=str(response_text or "")):
                    _net_plan = await self._plan_tickets(
                        context=context, pre_arrival=_net_pre_arrival,
                        current_reply=str(response_text or ""))
                    if _net_plan:
                        logger.warning(
                            "[TICKET_PLAN] the reply told the guest staff were "
                            "informed but no ticket was raised — raising %d now",
                            len(_net_plan))
                        _net_refs = await self._raise_planned_tickets(
                            plan=_net_plan, context=context,
                            guest_message=str(effective_user_message or ""),
                            phase=str(policy_result.current_phase_name or ""))
                        if _net_refs:
                            ticket_meta = dict(ticket_meta or {})
                            ticket_meta.update({
                                "ticket_created": True,
                                "ticket_id": _net_refs[0],
                                "ticket_status": "open",
                                "ticket_source": "ticket_planner_safety_net",
                            })
                    else:
                        logger.info(
                            "[TICKET_PLAN] reply claimed staff were told; planner "
                            "found nothing new to raise (already open or not a "
                            "request)")
        except Exception as _net_exc:
            logger.warning("[TICKET_PLAN] safety net skipped (non-fatal): %r", _net_exc)

        # ── A FOOD ORDER IS ONLY "ON ITS WAY" ONCE ITS TICKET EXISTS ─────────
        # This is the one place that knows whether the kitchen actually heard the
        # order — both the new-ticket path above and the update-an-open-order path
        # land here with ticket_meta filled in. The order-state pass deliberately
        # left the guest an accurate summary and a confirm request instead of a
        # confirmation, so if the ticket did NOT get written we simply say nothing
        # further and the guest is asked to confirm rather than thanked for food
        # nobody is cooking.
        _handler_meta = (handler_result.metadata or {}) if handler_result is not None else {}
        _ticket_was_written = bool(
            (ticket_meta or {}).get("ticket_created")
            or _handler_meta.get("ticket_created")
        )
        # ...BY THE KITCHEN, NOT BY WHOEVER HAPPENED TO GET A TICKET THIS TURN.
        # This used to fire on ANY ticket written. So on a turn that raised an
        # ENGINEERING ticket for the AC, the pending food cart was committed as
        # placed — against the Engineering ticket's ref — and the guest was told
        # "Order 3 placed — on its way. 3 x Vegetable Burger". No food ticket
        # existed; the kitchen never heard about it. A food order is only placed
        # when the ticket written this turn is the FOOD one.
        _ird_ticket_written = _ticket_was_written and llm_orchestration_service._is_sarovar_ird(
            context, str(getattr(decision, "target_service_id", "") or "").strip()
        )
        if _ticket_was_written and not _ird_ticket_written and (decision.metadata or {}).get("ird_placement"):
            logger.warning(
                "[IRD] a ticket was written this turn but it belongs to %r, not the "
                "kitchen — NOT committing the food order against it",
                str(getattr(decision, "target_service_id", "") or "") or "another team",
            )
        if _ird_ticket_written:
            _ird_done = llm_orchestration_service._ird_commit_placement(
                context=context,
                decision=decision,
                ticket_ref=str(
                    (ticket_meta or {}).get("ticket_ref")
                    or (ticket_meta or {}).get("ticket_id")
                    or _handler_meta.get("ticket_id")
                    or ""
                ).strip(),
            )
            if _ird_done:
                response_text = _ird_done

            # ── TELL THE BOOK WHICH TICKET THIS ORDER BECAME ─────────────────
            # Without the ref the book can never ask the dashboard whether the
            # kitchen has started, so an order would stay "placed" until the
            # five-minute clock ran out and a guest could still edit food that
            # was already being cooked. Written HERE because the ticket only
            # exists now, and _ird_commit_placement is sync so it cannot save.
            if bool(getattr(settings, "ird_order_book_enabled", False)):
                try:
                    from services import ird_cart_book as _bk
                    from services import ird_cart_store as _bs

                    _bref = str(
                        (ticket_meta or {}).get("ticket_ref")
                        or (ticket_meta or {}).get("ticket_id")
                        or _handler_meta.get("ticket_id") or ""
                    ).strip()
                    _bhc = str(getattr(context, "hotel_code", "") or "")
                    _bsid = str(getattr(context, "session_id", "") or "")
                    if _bref and _bhc and _bsid:
                        _brow = await _bs.load(hotel_code=_bhc, session_id=_bsid,
                                               create=False)
                        if _brow:
                            _bslots = _bk.slots_from_row(_brow)
                            # The order that just went out is the newest one the
                            # book has with no ticket against it yet.
                            _mine = [sl for sl in _bslots
                                     if sl.get("items") and not sl.get("ticket_ref")]
                            # ── ONE TICKET BELONGS TO ONE ORDER ──────────────
                            # This ref landed on TWO slots once (11329 on slot 2
                            # and slot 3), so two different orders both claimed
                            # the same kitchen ticket and the guest saw a third
                            # copy of an order they placed once. A ref already
                            # spoken for is never stamped again.
                            if any(str(sl.get("ticket_ref") or "").strip() == _bref
                                   for sl in _bslots):
                                logger.warning(
                                    "[IRD_BOOK] ticket %s is already on another "
                                    "order — not stamping it onto a second one",
                                    _bref)
                                _mine = []
                            if _mine:
                                _slot = max(_mine, key=lambda sl: sl["n"])
                                _slot["ticket_ref"] = _bref
                                _slot["status"] = _bk.PLACED
                                if not _slot.get("placed_at"):
                                    _slot["placed_at"] = _bk.now_local()
                                await _bs.save_slots(hotel_code=_bhc, session_id=_bsid,
                                                     slots=[_slot])
                                _bk.project_to_pending_data(_bslots, context.pending_data)
                                logger.info(
                                    "[IRD_BOOK] slot %d is now ticket %s",
                                    _slot["n"], _bref)
                                # ── AND THE KITCHEN READS THE SLOT, NOT THE CHAT ──
                                # The clean "In-room dining order for room X: ..."
                                # text is built earlier from the validated cart,
                                # and on a turn where the model emits no order
                                # items that cart is empty - so ticket 11183 went
                                # to the kitchen holding the guest-facing bubble,
                                # markdown stars and "Sending this to the kitchen
                                # now." included. The slot is the order; print the
                                # description from it every time.
                                try:
                                    from services.ticketing_service import (
                                        ticketing_service as _ts2,
                                    )

                                    _desc2 = _bk.ticket_description(
                                        _slot,
                                        room=str((context.pending_data or {}).get(
                                            "room_number") or ""))
                                    await _ts2.update_ticket_details(
                                        session_id=_bsid, ticket_ref=_bref,
                                        description=_desc2)
                                    logger.info(
                                        "[IRD_BOOK] ticket %s description set from "
                                        "slot %d: %s", _bref, _slot["n"], _desc2[:90])
                                except Exception as _d2:
                                    logger.warning(
                                        "[IRD_BOOK] could not set the ticket "
                                        "description from the slot (non-fatal): %r",
                                        _d2)
                            else:
                                logger.warning(
                                    "[IRD_BOOK] ticket %s has no slot waiting for it",
                                    _bref)
                except Exception as _bk_exc:
                    # The ticket is already out; a book that cannot record it is
                    # a stale status, never a lost order.
                    logger.warning("[IRD_BOOK] could not record ticket ref "
                                   "(non-fatal): %r", _bk_exc)
        elif not _ticket_was_written and (decision.metadata or {}).get("ird_placement"):
            logger.warning(
                "[IRD] order NOT placed — no ticket was written this turn (%s)",
                (ticket_meta or {}).get("ticket_create_skip_reason")
                or (ticket_meta or {}).get("ticket_error") or "unknown",
            )
            # ── AND THE BOOK AND THE REPLY MUST BOTH TAKE IT BACK ────────────
            # This branch used to only log, on the assumption that the guest had
            # been left an order summary. They had not. The order-state pass
            # writes the slot as PLACED and the reply says "Sending this to the
            # kitchen now" BEFORE the ticket is ever attempted, so a ticketing
            # outage left the book reading placed, a row carrying no external
            # reference, and a guest waiting for food nobody was cooking.
            # Verified by pointing the ticketing API at a blackhole: slot 1
            # [placed], TICKET None, "Sending this to the kitchen now."
            # The food goes back to being a cart - which is the truth, it is
            # still theirs to order - and the reply stops claiming the kitchen
            # has it. Nothing is thrown away.
            if bool(getattr(settings, "ird_order_book_enabled", False)):
                try:
                    from services import ird_cart_book as _bk
                    from services import ird_cart_store as _bs

                    _fhc = str(getattr(context, "hotel_code", "") or "")
                    _fsid = str(getattr(context, "session_id", "") or "")
                    _frow = await _bs.load(hotel_code=_fhc, session_id=_fsid,
                                           create=False) if _fhc and _fsid else None
                    if _frow:
                        _fslots = _bk.slots_from_row(_frow)
                        # The order that just went out is the newest one the book
                        # holds as placed with no ticket against it - the same
                        # slot the success path claims.
                        _stranded = [sl for sl in _fslots
                                     if sl.get("items")
                                     and not sl.get("ticket_ref")
                                     and sl.get("status") == _bk.PLACED]
                        if _stranded:
                            _fslot = max(_stranded, key=lambda sl: sl["n"])
                            # ── DID IT REACH STAFF ANY OTHER WAY? ────────────
                            # The external API and the hotel's dashboard are two
                            # different things. The KePSLA row is written even
                            # when the external push fails, so the kitchen team
                            # CAN see the order - and telling the guest "nothing
                            # has been sent" about a ticket already on staff's
                            # screen is its own kind of wrong. Only the case
                            # where nothing landed anywhere is a lost order.
                            if (ticket_meta or {}).get("ticket_on_dashboard"):
                                logger.info(
                                    "[IRD_BOOK] slot %d kept as placed — the external "
                                    "API failed but the order is on the hotel's "
                                    "dashboard", _fslot["n"])
                            else:
                                _fslot["status"] = _bk.ONGOING
                                _fslot["placed_at"] = None
                                await _bs.save_slots(hotel_code=_fhc, session_id=_fsid,
                                                     slots=[_fslot])
                                _bk.project_to_pending_data(_fslots, context.pending_data)
                                logger.warning(
                                    "[IRD_BOOK] slot %d rolled back to ongoing — its "
                                    "ticket never reached the hotel", _fslot["n"])
                                response_text = (
                                    "I'm sorry — I could not get that order through to "
                                    "the kitchen just now, so nothing has been sent. "
                                    "Your items are still in your cart. Shall I try "
                                    "placing it again?"
                                )
                except Exception as _fx:
                    logger.warning("[IRD_BOOK] could not roll back an unplaced "
                                   "order (non-fatal): %r", _fx)

        # Room-booking ticket manager: create on explicit booking intent (reply
        # offers website link OR team follow-up), dedup per property, ask to cancel
        # the most recent prior booking on a property switch, and update dates on a
        # change request. A "control" reply is authored by the manager (cancel
        # question / post-confirm / date confirmation) and must skip the LLM rewrite.
        response_text, _rb_control_response = await self._maybe_manage_room_booking_ticket(
            context=context,
            response_text=response_text,
            decision=decision,
            user_message=effective_user_message,
            selected_phase_context=selected_phase_context,
        )

        if not response_text:
            # Orchestrator/service agent returned empty response_text — generate a contextual reply
            # instead of the old hardcoded fallback so the guest gets a meaningful message.
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "Empty response_text after orchestration [session=%s action=%s pending=%s] — generating contextual fallback",
                getattr(context, "session_id", "?"),
                getattr(decision, "action", "?"),
                getattr(context, "pending_action", "?"),
            )
            _pending = str(context.pending_action or "").strip()
            _fallback_system = (
                "You are a hotel concierge assistant. The system failed to generate a response. "
                "Write a single short, warm, helpful reply that continues the conversation naturally. "
                "Do NOT mention any system error."
            )
            _fallback_user = (
                f"Guest said: \"{effective_user_message}\". "
                + (
                    f"The guest was mid-flow on: {_pending}. "
                    "Ask for the next piece of information needed in a natural, friendly way."
                    if _pending
                    else "Ask the guest what you can help them with today."
                )
            )
            try:
                response_text = await llm_client.chat(
                    messages=[
                        {"role": "system", "content": _fallback_system},
                        {"role": "user", "content": _fallback_user},
                    ],
                    temperature=0.4,
                    max_tokens=120,
                ) or ""
                response_text = response_text.strip()
            except Exception:
                response_text = ""
            if not response_text:
                response_text = "I'm here to help — could you let me know what you need?"

        if no_template_mode:
            capability_check = CapabilityCheck(allowed=True, reason="service_first_mode")
        else:
            intent_enabled_check = self._check_intent_enabled(effective_intent)
            capability_check = (
                intent_enabled_check
                if not intent_enabled_check.allowed
                else self._check_capability_for_intent(
                    context.hotel_code,
                    intent_result,
                    effective_user_message,
                )
            )
        # In orchestration mode the LLM/service-agent controls everything —
        # skip response_validator entirely to prevent canned responses overriding good LLM output.
        orchestration_validator_replaced = False
        if no_template_mode:
            from services.response_validator import ValidationResult as _VR
            orchestration_validation = _VR(valid=True, action="allow")
        else:
            orchestration_validation = response_validator.validate(
                response_text=response_text,
                intent_result=intent_result,
                context=context,
                capabilities_summary=capabilities_summary,
                capability_check_allowed=capability_check.allowed,
                capability_reason=capability_check.reason,
            )
            if (
                not orchestration_validation.valid
                and orchestration_validation.action == "replace"
                and orchestration_validation.replacement_response
            ):
                response_text = orchestration_validation.replacement_response
                orchestration_validator_replaced = True
        if _rb_control_response:
            # Backend-authored room-booking control reply (cancel question /
            # post-confirm / date confirmation) — keep it verbatim (and preserve its
            # <<SHOW_BOOKING_BUTTON>> marker); never send it through the LLM rewrite.
            orchestration_rewrite_metadata = {}
        else:
            response_text, orchestration_rewrite_metadata = await self._maybe_llm_rewrite_response(
                response_text=response_text,
                user_message=effective_user_message,
                intent_result=intent_result,
                context=context,
                capabilities_summary=capabilities_summary,
                capability_check_allowed=capability_check.allowed,
                capability_reason=capability_check.reason,
                response_source="llm_orchestration",
                validator_replaced=(orchestration_validator_replaced or policy_override_applied),
            )

        response_text, resume_actions = self._append_resume_checkpoint(
            response_text=response_text,
            context=context,
            pending_interrupted=pending_interrupted,
        )

        # Persist the router's language contract with this assistant turn. The
        # next turn can then identify an explicit language switch.
        turn_response_language = getattr(context, "_turn_response_language", None)
        # No contract from the router — fall back to a visible script mismatch,
        # so a reply that drifted into another script is still corrected. Only
        # for hotels that opted into enforcement; nobody else is touched.
        if not (
            isinstance(turn_response_language, dict)
            and str(turn_response_language.get("language") or "").strip()
            and str(turn_response_language.get("script") or "").strip()
        ) and _language_enforcement_for_hotel(
            str(context.hotel_code or request.hotel_code or "").strip().casefold()
        ):
            _fallback_language = await llm_orchestration_service._detect_response_language_with_llm(
                user_message=str(effective_user_message or ""), context=context,
            )
            if _fallback_language:
                logger.info(
                    "[LANG] router gave no contract; language router -> %s/%s",
                    _fallback_language.get("language"),
                    _fallback_language.get("script"),
                )
                turn_response_language = _fallback_language
        if isinstance(turn_response_language, dict) and (
            str(turn_response_language.get("language") or "").strip()
            and str(turn_response_language.get("script") or "").strip()
        ):
            orchestration_rewrite_metadata["response_language"] = dict(
                turn_response_language
            )

            # A final language-enforcement pass, because the answer model can
            # copy the previous turn's language even after the router supplied
            # the correct current-language contract. Reuse the existing display
            # beautifier/model so this is one additional call, not a separate
            # detector plus translator pipeline.
            #
            # This used to be `if hotel_key == "sarovar_portico_sriperumbudur"`.
            # That hardcode was the whole reason Mango drifted: its router named
            # the right language every turn and nothing made the answer obey, so
            # an English question came back in Hindi and a Hindi one in English.
            # Now an opt-IN list, defaulted to Sarovar so its behaviour is
            # unchanged even with no env set. A hotel that is not listed never
            # enters this block - no extra call, no added latency.
            hotel_key = str(context.hotel_code or request.hotel_code or "").strip().casefold()
            if _language_enforcement_for_hotel(hotel_key):
                language_switch = bool(
                    getattr(context, "_turn_language_switch", False)
                )
                try:
                    response_text, beautifier_metadata = (
                        await response_beautifier_service.beautify_display_text(
                            response_text,
                            state=str(getattr(next_state, "value", next_state) or ""),
                            metadata={
                                "response_source": "llm_orchestration",
                                "response_language": dict(turn_response_language),
                                "language_reference_text": str(
                                    effective_user_message or ""
                                ),
                                "language_switch": language_switch,
                                "strict_language_enforcement": team_booking_enabled(hotel_key),
                            },
                        )
                    )
                    logger.info(
                        "[LANG] enforcement target=%s/%s switch=%s applied=%s in %sms",
                        turn_response_language.get("language"),
                        turn_response_language.get("script"),
                        language_switch,
                        beautifier_metadata.get("display_beautifier_applied"),
                        beautifier_metadata.get("display_beautifier_latency_ms"),
                    )
                    if beautifier_metadata.get("display_beautifier_fallback_reason"):
                        logger.warning(
                            "[LANG] enforcement did NOT apply - guest keeps the "
                            "draft language: target=%s/%s switch=%s reason=%s",
                            turn_response_language.get("language"),
                            turn_response_language.get("script"),
                            language_switch,
                            beautifier_metadata.get(
                                "display_beautifier_fallback_reason"
                            ),
                        )
                    orchestration_rewrite_metadata.update(beautifier_metadata)
                    # VERIFIED MEANS "THE REPLY IS IN THE REQUIRED LANGUAGE",
                    # NOT "AN LLM WAS CALLED". The enforcement pass stands itself
                    # down when there is nothing to translate - the turn wants
                    # English and the draft is already plain English, no language
                    # switch in play. That turn is verified by construction; no
                    # model had to prove it. Reading only the LLM flags marked
                    # every ordinary English turn unverified, which made
                    # `_prefetch_contract_matches` reject every cached chip answer
                    # and silently disabled the whole prefetch cache for this
                    # tenant - answers stayed correct, but every chip paid full
                    # live latency.
                    orchestration_rewrite_metadata["language_contract_verified"] = bool(
                        (
                            beautifier_metadata.get("display_beautifier_language_enforced")
                            and beautifier_metadata.get("display_beautifier_llm_used")
                            and not beautifier_metadata.get("display_beautifier_fallback_reason")
                        )
                        or beautifier_metadata.get("display_beautifier_language_skipped")
                    )
                    orchestration_rewrite_metadata["response_language"] = dict(
                        turn_response_language
                    )
                except Exception:
                    logger.exception("language_beautifier_failed hotel=%s", hotel_key)
                    orchestration_rewrite_metadata.update(
                        {
                            "display_beautifier_language_enforced": True,
                            "display_beautifier_applied": False,
                            "display_beautifier_fallback_reason": "beautifier_exception",
                        }
                    )

        # MANGO: ONE BULLET CHARACTER. The widget prints list markers exactly as
        # written, and a Mango reply passes through two LLMs (the answerer, then
        # the language/beautifier pass) that each pick "•" or "-" on their own -
        # the beautifier even swapped a draft's "•" for "-". Guests saw both in
        # one chat. Line-leading "-"/"*" markers become "•" for Mango only; Zone
        # never runs the beautifier and is left exactly as it is.
        from services import prebooking_settings_service as _pbs

        _admin_settings = _pbs.settings_for(context.hotel_code)
        if _admin_settings is not None:
            # Admin-managed hotels: the bullet and emoji choices from the
            # Pre-booking tab, applied to the final text so every lane agrees.
            _style = _admin_settings.get("reply_style") or {}
            if _style.get("bullet", "•") == "•":
                response_text = re.sub(
                    r"(?m)^([ \t]*)[-*][ \t]+(?=\S)", r"\1• ", str(response_text or "")
                )
            else:
                response_text = re.sub(
                    r"(?m)^([ \t]*)[•*][ \t]+(?=\S)", r"\1- ", str(response_text or "")
                )
            # One point is not a list: a lone "• I'm sorry, I can't help with
            # this request..." read as a stray marker (T24, 15 Oct). Bullets stay
            # whenever the reply has two or more points.
            _bullet_line = r"(?m)^([ \t]*)[•*-][ \t]+(?=\S)"
            if len(re.findall(_bullet_line, response_text)) == 1:
                response_text = re.sub(_bullet_line, r"\1", response_text, count=1)
            if _style.get("emoji") == "none":
                response_text = _strip_emoji(response_text)
        elif str(context.hotel_code or "").strip().lower() == "mango_grove_chandigarh":
            response_text = re.sub(
                r"(?m)^([ \t]*)[-*][ \t]+(?=\S)", r"\1• ", str(response_text or "")
            )

        if team_booking_enabled(context.hotel_code):
            orchestration_rewrite_metadata["booking_details_submitted"] = booking_details_submitted(context)
        context.state = next_state
        self._sync_active_task_snapshot(
            context=context,
            selected_phase_context=selected_phase_context or {},
        )
        assistant_intent_value = (
            "service_routed"
            if hide_intent_in_response
            else effective_intent.value
        )
        assistant_confidence_value = (
            float(decision.confidence or 0.0)
            if hide_intent_in_response
            else intent_result.confidence
        )
        orchestration_source = str((decision.metadata or {}).get("source") or "orchestrator")
        source_key = self._normalize_service_identifier(orchestration_source)
        service_llm_label = "main"
        if source_key.startswith("service_agent"):
            service_llm_label = self._service_llm_label_for_service(
                service_id=str(decision.target_service_id or ""),
                capabilities_summary=capabilities_summary,
                effective_intent=effective_intent,
                fallback="main",
            )
        service_llm_confidence = max(
            0.0,
            min(1.0, float(decision.confidence or intent_result.confidence or 0.0)),
        )
        assistant_metadata = {
            "intent": assistant_intent_value,
            "confidence": assistant_confidence_value,
            "service_llm_label": service_llm_label,
            "service_llm_confidence": service_llm_confidence,
            # Active property scope for THIS turn — lets the API layer gate the
            # booking button (only show when exactly ONE property is in focus).
            "active_property_ids": [
                str(x).strip()
                for x in (
                    list(getattr(decision, "selected_property_ids", None) or [])
                    or (context.pending_data.get("_active_property_ids") if isinstance(context.pending_data, dict) else [])
                    or []
                )
                if str(x).strip()
            ],
            "channel": context.channel,
            "orchestration_mode": True,
            "orchestration_action": str(decision.action or ""),
            "orchestration_target_service_id": str(decision.target_service_id or ""),
            "orchestration_interrupt_pending": bool(decision.interrupt_pending),
            "orchestration_source": orchestration_source,
            "orchestration_trace_id": str((decision.metadata or {}).get("orchestration_trace_id") or ""),
            "policy_blocked_reason": str(policy_result.blocked_reason or ""),
            "policy_out_of_phase": bool(policy_result.out_of_phase),
            "policy_ticket_skip_reason": str(policy_result.ticket_skip_reason or ""),
            "orchestration_handler_executed": handler_result is not None,
            "orchestration_prefer_llm_response": prefer_llm_response,
            "intent_hidden_in_response": hide_intent_in_response,
            "response_validator_applied": True,
            "response_validator_replaced": orchestration_validator_replaced,
            "response_validator_issues": [
                str(issue.code)
                for issue in (orchestration_validation.issues or [])
                if getattr(issue, "code", None)
            ],
        }
        if handler_result is not None and handler_result.metadata:
            assistant_metadata.update(handler_result.metadata)
        if ticket_meta:
            assistant_metadata.update(ticket_meta)
        assistant_metadata.update(orchestration_rewrite_metadata)

        context.add_message(
            MessageRole.ASSISTANT,
            response_text,
            metadata=assistant_metadata,
        )
        conversation_memory_service.capture_assistant_message(
            context,
            response_text,
            metadata=assistant_metadata,
        )
        await self._refresh_summary_async(context)
        await context_manager.save_context(context, db_session=db_session)
        memory_snapshot = conversation_memory_service.get_snapshot(context)

        if prefer_llm_suggested_actions and decision.suggested_actions:
            suggested_actions = list(decision.suggested_actions)
        elif handler_result is not None and handler_result.suggested_actions:
            suggested_actions = list(handler_result.suggested_actions)
        elif decision.suggested_actions:
            suggested_actions = list(decision.suggested_actions)
        else:
            suggested_actions = self._get_suggested_actions(next_state, effective_intent, capabilities_summary)
        suggested_actions = resume_actions + suggested_actions
        suggested_actions = self._finalize_user_query_suggestions(
            suggested_actions,
            state=next_state,
            intent=effective_intent,
            pending_action=context.pending_action,
            pending_data=context.pending_data,
            capabilities_summary=capabilities_summary,
        )

        routing_path = getattr(
            getattr(routing_decision, "path", ProcessingPath.SIMPLE),
            "value",
            ProcessingPath.SIMPLE.value,
        )
        routing_score = float(getattr(routing_decision, "score", 1.0))
        routing_signals = list(getattr(routing_decision, "signals", []))

        metadata = {
            "message_count": len(context.messages),
            "entities": {
                "orchestration_action": str(decision.action or ""),
                "target_service_id": str(decision.target_service_id or ""),
                "missing_fields": list(decision.missing_fields),
                "pending_action": context.pending_action,
                "pending_data": {
                    k: v for k, v in (context.pending_data or {}).items()
                    if isinstance(k, str) and not k.startswith("_")
                } if isinstance(context.pending_data, dict) else {},
            },
            "classified_intent": assistant_intent_value,
            "classified_confidence": assistant_confidence_value,
            "routing_path": routing_path,
            "routing_score": routing_score,
            "routing_signals": routing_signals,
            "response_source": "llm_orchestration",
            "orchestration_mode": True,
            "service_llm_label": service_llm_label,
            "service_llm_confidence": service_llm_confidence,
            # The KB books the router chose for this answer (book route only);
            # the API layer sends an admin hotel's menus on its menu topics.
            "router_book_ids": [
                str(x) for x in list((decision.metadata or {}).get("router_book_ids") or [])[:20]
            ],
            # Active property scope for THIS turn — the API layer uses this to
            # show the booking button ONLY when exactly one property is in focus.
            "active_property_ids": [
                str(x).strip()
                for x in (
                    list(getattr(decision, "selected_property_ids", None) or [])
                    or (context.pending_data.get("_active_property_ids") if isinstance(context.pending_data, dict) else [])
                    or []
                )
                if str(x).strip()
            ],
            "active_property_names": [
                str(x).strip()
                for x in (context.pending_data.get("_active_property_names") or [])
                if isinstance(context.pending_data, dict) and str(x).strip()
            ],
            "orchestration_policy_blocked_reason": str(policy_result.blocked_reason or ""),
            "orchestration_policy_out_of_phase": bool(policy_result.out_of_phase),
            "orchestration_policy_ticket_skip_reason": str(policy_result.ticket_skip_reason or ""),
            "intent_hidden_in_response": hide_intent_in_response,
            "response_validator_applied": True,
            "response_validator_replaced": orchestration_validator_replaced,
            "response_validator_issues": [
                str(issue.code)
                for issue in (orchestration_validation.issues or [])
                if getattr(issue, "code", None)
            ],
            "memory_summary": memory_snapshot.get("summary", ""),
            "memory_facts": memory_snapshot.get("facts", {}),
            "memory_recent_changes": memory_snapshot.get("recent_changes", []),
        }
        if ticket_meta:
            metadata.update(self._apply_ticket_metadata_contract(ticket_meta))
        metadata.update(orchestration_rewrite_metadata)

        return ChatResponse(
            session_id=request.session_id,
            message=response_text,
            intent=None if hide_intent_in_response else effective_intent,
            confidence=None if hide_intent_in_response else intent_result.confidence,
            service_llm_label=service_llm_label,
            service_llm_confidence=service_llm_confidence,
            state=next_state,
            suggested_actions=suggested_actions,
            metadata=metadata,
        )













    @staticmethod
    def _extract_room_number_from_text(message: str) -> str:
        if not message:
            return ""
        match = re.search(
            r"\b(?:room(?:\s*(?:number|no\.?))?\s*(?:is|=|:)?\s*)?([A-Za-z0-9-]{2,10})\b",
            str(message),
            flags=re.IGNORECASE,
        )
        if not match:
            return ""
        candidate = str(match.group(1) or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9-]{2,10}", candidate):
            return ""
        if not any(ch.isdigit() for ch in candidate):
            return ""
        return candidate.upper()













    def _resolve_unified_complaint_routing_intent(
        self,
        *,
        message: str,
        effective_intent: IntentType,
        current_pending_action: str | None,
        pending_action_target: str | None,
        llm_response_text: str = "",
        llm_ticketing_preference: bool | None = None,
        next_state: ConversationState = ConversationState.IDLE,
    ) -> tuple[IntentType | None, str, str]:
        """
        Determine whether this turn should be handled by the complaint flow even
        when plugin takeover mode is disabled.
        """
        # LLM orchestrator is the single authority on routing.
        # Only route to complaint handler when the LLM explicitly decided it is a complaint.
        # All rule-based overrides removed:
        #   - _is_ticketing_pending_action: removed — orchestrator reads pending_action and decides
        #   - _should_route_full_kb_ticketing_handler: removed — orchestrator owns this decision
        #   - _llm_response_implies_staff_action: removed — scanning LLM output to second-guess LLM is wrong

        if effective_intent == IntentType.COMPLAINT:
            return IntentType.COMPLAINT, "complaint_intent", "intent"
        return None, "", ""

    @staticmethod
    def _extract_full_kb_entities_for_handler(llm_result: Any) -> dict[str, Any]:
        entities: dict[str, Any] = {}
        llm_output = llm_result.llm_output if isinstance(llm_result.llm_output, dict) else {}
        nested_entities = llm_output.get("entities")
        if isinstance(nested_entities, dict):
            entities.update(nested_entities)

        top_level_entity_keys = (
            "service_id",
            "service_name",
            "target_service_id",
            "target_service",
            "resolved_service_id",
            "resolved_service_name",
            "workflow_id",
            "booking_sub_category",
            "sub_category",
            "ticket_category",
            "ticket_sub_category",
            "ticket_priority",
            "ticket_issue",
            "ticket_reason",
            "requires_ticket",
        )
        for key in top_level_entity_keys:
            value = llm_output.get(key)
            if value in (None, ""):
                continue
            entities.setdefault(key, value)

        pending_updates = llm_output.get("pending_data_updates")
        if isinstance(pending_updates, dict):
            for key, value in pending_updates.items():
                if key not in entities and value not in (None, ""):
                    entities[key] = value

        pending_data = llm_result.pending_data if isinstance(llm_result.pending_data, dict) else {}
        for key, value in pending_data.items():
            if key not in entities and value not in (None, ""):
                entities[key] = value
        return entities





    @staticmethod
    def _coerce_optional_bool(value: Any) -> bool | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value or "").strip().lower()
        if not text:
            return None
        if text in {"true", "1", "yes", "y"}:
            return True
        if text in {"false", "0", "no", "n"}:
            return False
        return None


    @staticmethod
    def _resolve_history_window(context: ConversationContext) -> int:
        """
        Use full available session history for agent calls, bounded by the
        configured maximum conversation history.
        """
        configured_max = max(6, int(getattr(settings, "max_conversation_history", 20) or 20))
        available = len(context.messages or [])
        if available <= 0:
            return configured_max
        return min(available, configured_max)

















    @staticmethod
    def _looks_like_loss_or_security_issue(msg_lower: str) -> bool:
        text = str(msg_lower or "").strip().lower()
        if not text:
            return False
        loss_markers = (
            "lost",
            "missing",
            "misplaced",
            "stolen",
            "left behind",
            "cannot find",
            "can't find",
            "cant find",
            "belonging",
            "item",
            "luggage",
            "wallet",
            "keys",
            "earring",
            "jewellery",
            "jewelry",
        )
        return any(marker in text for marker in loss_markers)



    @staticmethod
    def _is_confirmation_prompt_text(message: str) -> bool:
        """
        Detect whether assistant text is explicitly asking for final confirmation.
        Kept generic so it works across workflows/domains.
        """
        msg = re.sub(r"\s+", " ", str(message or "").strip().lower())
        if not msg:
            return False
        if "type \"yes confirm\"" in msg or "type 'yes confirm'" in msg:
            return True
        if "yes/no" in msg or "(yes/no)" in msg:
            return True
        confirm_markers = (
            "shall i confirm",
            "would you like to confirm",
            "please confirm",
            "confirm this",
            "confirm your",
            "confirmation",
        )
        return any(marker in msg for marker in confirm_markers)

    @staticmethod
    def _contains_explicit_confirmation_instruction(
        response_text: str,
        confirmation_phrase: str,
    ) -> bool:
        msg = re.sub(r"\s+", " ", str(response_text or "").strip().lower())
        phrase = re.sub(r"\s+", " ", str(confirmation_phrase or "").strip().lower())
        if not msg or not phrase:
            return False
        if phrase not in msg:
            return False
        return any(token in msg for token in ("type", "reply", "confirm"))

    def _ensure_explicit_confirmation_instruction(
        self,
        response_text: str,
        confirmation_phrase: str,
    ) -> str:
        """
        Ensure confirmation prompts explicitly instruct user to type the
        required confirmation phrase.
        """
        text = str(response_text or "").strip()
        if not text:
            return f"To proceed, please type \"{confirmation_phrase}\". To cancel, type \"cancel\"."
        if self._contains_explicit_confirmation_instruction(text, confirmation_phrase):
            return text
        if self._is_confirmation_prompt_text(text):
            return (
                f"{text}\n\n"
                f'To proceed, please type "{confirmation_phrase}". '
                'If you want to cancel, type "cancel".'
            )
        return text

    def _normalize_confirm_pending_action(
        self,
        pending_action_target: str | None,
        intent: IntentType,
        room_booking_flow: bool,
    ) -> str:
        pending = str(pending_action_target or "").strip().lower()
        if pending.startswith("confirm_"):
            return pending
        if room_booking_flow:
            return "confirm_room_booking"
        if intent == IntentType.ORDER_FOOD:
            return "confirm_order"
        if intent == IntentType.TABLE_BOOKING:
            return "confirm_booking"
        return "confirm_action"

    def _requires_strict_confirmation(
        self,
        context: ConversationContext,
        current_pending_action: str | None,
    ) -> bool:
        """
        Enforce strict confirmation only for actual final-confirm steps, not
        generic selection/browsing follow-ups.
        """
        pending = str(current_pending_action or "").strip().lower()
        if pending.startswith("confirm_"):
            return True
        if context.state == ConversationState.AWAITING_CONFIRMATION:
            return True

        for msg in reversed(context.messages):
            if msg.role != MessageRole.ASSISTANT:
                continue
            return self._is_confirmation_prompt_text(str(msg.content or ""))
        return False

    @staticmethod
    def _detect_simple_binary_reply(message: str) -> Optional[IntentType]:
        """
        Lightweight yes/no detector used only for polarity correction in
        multi-step selection flows.
        """
        msg = re.sub(r"\s+", " ", str(message or "").strip().lower())
        if not msg:
            return None
        yes_tokens = {"yes", "yeah", "yep", "y", "ok", "okay", "sure"}
        no_tokens = {"no", "nope", "n", "nah", "cancel", "stop"}
        if msg in yes_tokens:
            return IntentType.CONFIRMATION_YES
        if msg in no_tokens:
            return IntentType.CONFIRMATION_NO
        return None


    @staticmethod
    def _looks_like_more_options_followup(message: str) -> bool:
        msg = re.sub(r"\s+", " ", str(message or "").strip().lower())
        if not msg:
            return False
        markers = (
            "show more",
            "more options",
            "any more",
            "do you have more",
            "do u have more",
            "do u hv more",
            "anything else",
            "more",
        )
        return any(marker in msg for marker in markers)




    @staticmethod
    def _strip_leading_yes(message: str) -> str:
        msg = str(message or "").strip()
        msg_lower = msg.lower()
        if msg_lower.startswith("yes "):
            return msg[4:].strip()
        if msg_lower.startswith("yes,"):
            return msg[4:].strip()
        return ""



    @staticmethod
    def _is_ambiguous_short_reference_phrase(text: str) -> bool:
        normalized = re.sub(r"\s+", " ", str(text or "").strip().lower())
        if not normalized:
            return False
        if "?" in normalized:
            return False
        if re.search(r"\d", normalized):
            return False
        tokens = re.findall(r"[a-z]+", normalized)
        if not tokens:
            return False
        return len(tokens) <= 2 and len(normalized) <= 20















    @staticmethod
    def _extract_option_entries_from_llm_json(llm_json: dict[str, Any]) -> list[tuple[str, str]]:
        if not isinstance(llm_json, dict):
            return []
        raw_options = llm_json.get("options")
        if not isinstance(raw_options, list):
            raw_options = llm_json.get("items")
        if not isinstance(raw_options, list):
            return []

        parsed: list[tuple[str, str]] = []
        for item in raw_options:
            if isinstance(item, str):
                name = re.sub(r"\s+", " ", item).strip()
                if name:
                    parsed.append((name, ""))
                continue
            if not isinstance(item, dict):
                continue
            name = re.sub(r"\s+", " ", str(item.get("name") or item.get("item_name") or "").strip())
            if not name:
                continue
            price_raw = str(item.get("price") or item.get("price_inr") or "").strip()
            price_text = ""
            if price_raw:
                price_compact = re.sub(r"\s+", "", price_raw)
                if not price_compact.lower().startswith(("rs.", "rs", "₹")):
                    price_text = f"Rs.{price_compact}"
                else:
                    price_text = price_raw.replace("₹", "Rs.")
            parsed.append((name, price_text))
        return parsed




    @staticmethod
    def _first_non_empty(*values: Any) -> str:
        for value in values:
            text = str(value or "").strip()
            if text:
                return text
        return ""

    def _enrich_pending_with_known_facts(
        self,
        *,
        pending_data: dict[str, Any] | None,
        context: ConversationContext,
        memory_snapshot: dict[str, Any] | None = None,
        entities: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        pending = dict(pending_data or {}) if isinstance(pending_data, dict) else {}
        entity_map = entities if isinstance(entities, dict) else {}
        integration = ticketing_service.get_integration_context(context)
        facts = {}
        if isinstance(memory_snapshot, dict):
            candidate_facts = memory_snapshot.get("facts", {})
            if isinstance(candidate_facts, dict):
                facts = candidate_facts
        if not facts:
            facts = self._memory_facts_from_context(context)

        known_values = {
            "guest_name": self._first_non_empty(
                pending.get("guest_name"),
                entity_map.get("guest_name"),
                context.guest_name,
                integration.get("guest_name"),
                facts.get("guest_name"),
            ),
            "guest_phone": self._first_non_empty(
                pending.get("guest_phone"),
                entity_map.get("guest_phone"),
                context.guest_phone,
                integration.get("guest_phone"),
                integration.get("wa_number"),
                facts.get("guest_phone"),
            ),
            "room_number": self._first_non_empty(
                pending.get("room_number"),
                entity_map.get("room_number"),
                context.room_number,
                integration.get("room_number"),
                facts.get("room_number"),
            ),
            "stay_checkin_date": self._first_non_empty(
                pending.get("stay_checkin_date"),
                pending.get("check_in"),
                entity_map.get("stay_checkin_date"),
                entity_map.get("check_in"),
                integration.get("stay_checkin_date"),
                integration.get("check_in"),
                facts.get("stay_checkin_date"),
                facts.get("check_in"),
                facts.get("checkin_date"),
            ),
            "stay_checkout_date": self._first_non_empty(
                pending.get("stay_checkout_date"),
                pending.get("check_out"),
                entity_map.get("stay_checkout_date"),
                entity_map.get("check_out"),
                integration.get("stay_checkout_date"),
                integration.get("check_out"),
                facts.get("stay_checkout_date"),
                facts.get("check_out"),
                facts.get("checkout_date"),
            ),
            "stay_date_range": self._first_non_empty(
                pending.get("stay_date_range"),
                entity_map.get("stay_date_range"),
                integration.get("stay_date_range"),
                facts.get("stay_date_range"),
            ),
            "booking_ref": self._first_non_empty(
                pending.get("booking_ref"),
                entity_map.get("booking_ref"),
                integration.get("booking_ref"),
                facts.get("last_booking_ref"),
            ),
        }

        for key, value in known_values.items():
            if value and not str(pending.get(key) or "").strip():
                pending[key] = value

        if str(pending.get("stay_checkin_date") or "").strip() and not str(pending.get("check_in") or "").strip():
            pending["check_in"] = str(pending.get("stay_checkin_date") or "").strip()
        if str(pending.get("stay_checkout_date") or "").strip() and not str(pending.get("check_out") or "").strip():
            pending["check_out"] = str(pending.get("stay_checkout_date") or "").strip()

        return pending




    @staticmethod
    def _format_calendar_date_full(value: date) -> str:
        try:
            return value.strftime("%B %d, %Y")
        except Exception:
            return str(value)



    @staticmethod
    def _looks_like_availability_request(message: str) -> bool:
        msg = re.sub(r"\s+", " ", str(message or "").strip().lower())
        if not msg:
            return False
        markers = (
            "availability",
            "available",
            "check availability",
            "check room availability",
            "room available",
            "is it available",
        )
        return any(marker in msg for marker in markers)

    @staticmethod
    def _looks_like_room_options_information_query(message: str) -> bool:
        msg = re.sub(r"\s+", " ", str(message or "").strip().lower())
        if not msg:
            return False
        ordinal_reference = bool(
            re.search(
                r"\b(?:the\s+)?(?:1st|2nd|3rd|[4-9]th|10th|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|last)\s+"
                r"(?:one|option|room|type|suite|villa|residence)\b",
                msg,
            )
        )
        room_markers = (
            "room",
            "rooms",
            "room type",
            "room types",
            "suite",
            "suites",
            "stay options",
        )
        info_markers = (
            "what",
            "which",
            "show",
            "list",
            "options",
            "types",
            "available",
            "have",
            "details",
            "tell me about",
        )
        if not any(marker in msg for marker in room_markers):
            if ordinal_reference and any(marker in msg for marker in ("tell me about", "more about", "details", "detail", "what about")):
                return True
            return False
        if "?" in msg:
            return True
        return any(marker in msg for marker in info_markers)

    @staticmethod
    def _extract_room_option_index_reference(message: str) -> int:
        msg = re.sub(r"\s+", " ", str(message or "").strip().lower())
        if not msg:
            return 0

        token_to_index = {
            "first": 1,
            "second": 2,
            "third": 3,
            "fourth": 4,
            "fifth": 5,
            "sixth": 6,
            "seventh": 7,
            "eighth": 8,
            "ninth": 9,
            "tenth": 10,
            "1st": 1,
            "2nd": 2,
            "3rd": 3,
            "4th": 4,
            "5th": 5,
            "6th": 6,
            "7th": 7,
            "8th": 8,
            "9th": 9,
            "10th": 10,
        }

        numbered = re.search(
            r"\b(?:the\s+)?(?P<idx>\d{1,2})(?:st|nd|rd|th)\s+(?:one|option|room|type|suite|villa|residence)\b",
            msg,
        )
        if numbered:
            try:
                index = int(numbered.group("idx"))
            except (TypeError, ValueError):
                index = 0
            return index if index > 0 else 0

        worded = re.search(
            r"\b(?:the\s+)?(?P<idx>first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)\s+"
            r"(?:one|option|room|type|suite|villa|residence)\b",
            msg,
        )
        if worded:
            return token_to_index.get(str(worded.group("idx") or "").strip().lower(), 0)

        return 0

    @classmethod
    def _extract_structured_room_option_entries(cls, content: str) -> list[dict[str, str]]:
        text = str(content or "")
        if not text:
            return []

        line_pattern = re.compile(
            r"^\s*(?:[-*]|\d+[.)])\s*(?:\*\*)?(?P<label>[a-z0-9&'/-][^:\n]{1,120}?(?:room|suite|villa|residence))"
            r"(?:\*\*)?\s*[:\-]\s*(?P<detail>[^\n]+?)\s*$",
            re.IGNORECASE | re.MULTILINE,
        )
        inline_pattern = re.compile(
            r"(?:^|\s)(?:\d+[.)])\s*(?:\*\*)?(?P<label>[a-z0-9&'/-][^:]{1,120}?(?:room|suite|villa|residence))"
            r"(?:\*\*)?\s*[:\-]\s*(?P<detail>.+?)(?=(?:\s+\d+[.)]\s+(?:\*\*)?[a-z0-9]|$))",
            re.IGNORECASE,
        )

        matches = list(line_pattern.finditer(text))
        if not matches:
            matches = list(inline_pattern.finditer(text))
        if not matches:
            return []

        entries: list[dict[str, str]] = []
        for match in matches:
            label = re.sub(r"\s+", " ", str(match.group("label") or "").strip(" .,:;!?*-"))
            detail = re.sub(r"\s+", " ", str(match.group("detail") or "").strip(" .,:;!?*-"))
            if not label or not detail:
                continue
            if cls._is_generic_room_reference(label):
                continue
            entries.append({"label": label, "detail": detail})
        return entries

    @classmethod
    def _find_room_option_entry(
        cls,
        entries: list[dict[str, str]],
        *,
        room_type: str = "",
        option_index: int = 0,
    ) -> dict[str, str]:
        if not isinstance(entries, list) or not entries:
            return {}

        if option_index > 0 and option_index <= len(entries):
            entry = entries[option_index - 1]
            if isinstance(entry, dict):
                return entry

        room_norm = cls._normalize_room_type_label(room_type)
        if room_norm:
            room_tokens = set(cls._room_type_tokens(room_norm))
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                label = str(entry.get("label") or "").strip()
                if not label:
                    continue
                label_norm = cls._normalize_room_type_label(label)
                if not label_norm:
                    continue
                if room_norm == label_norm:
                    return entry
                if room_norm in label_norm or label_norm in room_norm:
                    return entry
                label_tokens = set(cls._room_type_tokens(label_norm))
                if room_tokens and label_tokens and room_tokens.issubset(label_tokens):
                    return entry
        return {}

    def _resolve_room_reference_details(
        self,
        *,
        message: str,
        context: ConversationContext,
        pending_data: dict[str, Any],
    ) -> dict[str, Any]:
        persisted_candidates_raw = pending_data.get("room_type_candidates")
        persisted_candidates = [
            str(candidate or "").strip()
            for candidate in (persisted_candidates_raw if isinstance(persisted_candidates_raw, list) else [])
            if str(candidate or "").strip()
        ]
        room_candidates = self._merge_room_type_candidates(
            persisted_candidates,
            self._extract_room_type_candidates_from_context_messages(context),
        )
        msg_lower = str(message or "").strip().lower()
        room_type = self._match_room_type_candidate_from_message(msg_lower, room_candidates)
        option_index = self._extract_room_option_index_reference(msg_lower)
        if not room_type and option_index > 0 and option_index <= len(room_candidates):
            room_type = room_candidates[option_index - 1]

        detail_entry: dict[str, str] = {}
        for msg in reversed(context.messages):
            if msg.role != MessageRole.ASSISTANT:
                continue
            content = str(msg.content or "").strip()
            if not content:
                continue
            entries = self._extract_structured_room_option_entries(content)
            if not entries:
                continue
            detail_entry = self._find_room_option_entry(
                entries,
                room_type=room_type,
                option_index=option_index,
            )
            if detail_entry:
                break

        resolved_type = str(detail_entry.get("label") or room_type or "").strip()
        resolved_detail = str(detail_entry.get("detail") or "").strip()
        return {
            "room_type": resolved_type,
            "detail": resolved_detail,
            "option_index": option_index,
            "candidates": room_candidates,
        }


    def _build_room_availability_forward_prompt(self, pending_data: dict[str, Any]) -> str:
        check_in = self._first_non_empty(
            pending_data.get("check_in"),
            pending_data.get("stay_checkin_date"),
            pending_data.get("checkin_date"),
        )
        check_out = self._first_non_empty(
            pending_data.get("check_out"),
            pending_data.get("stay_checkout_date"),
            pending_data.get("checkout_date"),
        )
        guests = self._first_non_empty(
            pending_data.get("guest_count"),
            pending_data.get("party_size"),
            pending_data.get("guests"),
        )
        room_type = self._first_non_empty(
            pending_data.get("room_type"),
            pending_data.get("room_name"),
        )

        details: list[str] = []
        if room_type:
            details.append(f"{room_type}")
        if check_in and check_out:
            details.append(f"from {check_in} to {check_out}")
        if guests:
            details.append(f"for {guests} guests")
        detail_text = " ".join(details).strip()

        base = (
            "I can check availability by forwarding this request to our staff for confirmation."
        )
        if detail_text:
            base = f"{base} I have {detail_text}."
        return base + " Please type \"yes confirm\" to proceed."


    @staticmethod
    def _match_room_type_candidate_from_message(msg_lower: str, candidates: list[str]) -> str:
        text = str(msg_lower or "").strip().lower()
        if not text:
            return ""
        for candidate in candidates:
            cand_lower = str(candidate or "").strip().lower()
            if not cand_lower:
                continue
            if cand_lower in text:
                return candidate
            ratio = SequenceMatcher(a=text, b=cand_lower).ratio()
            if ratio >= 0.72:
                return candidate
        return ""

    @staticmethod
    def _normalize_room_type_label(value: str) -> str:
        text = str(value or "").strip().lower()
        if not text:
            return ""
        text = re.sub(r"[^a-z0-9\s/&'-]+", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @classmethod
    def _room_type_tokens(cls, value: str) -> list[str]:
        normalized = cls._normalize_room_type_label(value)
        tokens = re.findall(r"[a-z0-9]+", normalized)
        ignored = {
            "room",
            "rooms",
            "suite",
            "suites",
            "villa",
            "villas",
            "residence",
            "residences",
            "the",
            "a",
            "an",
        }
        return [token for token in tokens if token not in ignored]

    @classmethod
    def _is_generic_room_reference(cls, value: str) -> bool:
        normalized = cls._normalize_room_type_label(value)
        if not normalized:
            return True

        tokens = re.findall(r"[a-z0-9]+", normalized)
        if not tokens:
            return True

        accommodation_tokens = {
            "room",
            "rooms",
            "suite",
            "suites",
            "villa",
            "villas",
            "residence",
            "residences",
        }
        helper_tokens = {
            "i",
            "hotel",
            "hotels",
            "we",
            "our",
            "your",
            "my",
            "me",
            "us",
            "the",
            "a",
            "an",
            "of",
            "and",
            "or",
            "for",
            "to",
            "with",
            "from",
            "in",
            "at",
            "by",
            "this",
            "that",
            "these",
            "those",
            "is",
            "are",
            "was",
            "were",
            "be",
            "been",
            "being",
            "have",
            "has",
            "had",
            "do",
            "does",
            "did",
            "will",
            "would",
            "should",
            "may",
            "might",
            "must",
            "also",
            "just",
            "available",
            "availability",
            "offer",
            "offers",
            "offering",
            "variety",
            "type",
            "types",
            "option",
            "options",
            "booking",
            "book",
            "request",
            "requested",
            "selected",
            "select",
            "details",
            "detail",
            "help",
            "proceed",
            "share",
            "provide",
            "need",
            "want",
            "can",
            "could",
            "please",
            "about",
            "preferred",
            "specific",
            "any",
            "more",
            "here",
            "there",
            "are",
            "each",
            "on",
            "if",
            "you",
            "like",
            "let",
            "know",
            "which",
            "what",
            "show",
            "list",
            "tell",
            "explore",
        }
        phrase_markers = (
            "room booking",
            "book a room",
            "room request",
            "selected room",
            "room type",
            "room types",
            "room option",
            "room options",
            "variety of room",
            "preferred room",
            "specific room",
            "here are the room",
            "each room",
        )
        if any(marker in normalized for marker in phrase_markers):
            return True

        descriptor_tokens = [token for token in tokens if token not in accommodation_tokens]
        if not descriptor_tokens:
            return True

        informative = [token for token in descriptor_tokens if token not in helper_tokens]
        if not informative:
            return True
        if any(token in {"offer", "offers", "available", "availability", "variety", "booking"} for token in descriptor_tokens):
            return len(informative) < 2
        return False

    @classmethod
    def _resolve_room_type_candidate(cls, requested_label: str, candidates: list[str]) -> str:
        requested_norm = cls._normalize_room_type_label(requested_label)
        if not requested_norm or not candidates:
            return ""

        requested_tokens = set(cls._room_type_tokens(requested_norm))
        best_match = ""
        best_score = 0.0

        for candidate in candidates:
            candidate_text = str(candidate or "").strip()
            if not candidate_text:
                continue
            candidate_norm = cls._normalize_room_type_label(candidate_text)
            if not candidate_norm:
                continue

            if requested_norm == candidate_norm:
                return candidate_text
            if requested_norm in candidate_norm and len(requested_norm) >= max(6, int(len(candidate_norm) * 0.55)):
                return candidate_text
            if candidate_norm in requested_norm and len(candidate_norm) >= max(6, int(len(requested_norm) * 0.55)):
                return candidate_text

            ratio = SequenceMatcher(a=requested_norm, b=candidate_norm).ratio()
            candidate_tokens = set(cls._room_type_tokens(candidate_norm))
            if not requested_tokens:
                requested_tokens = set(re.findall(r"[a-z0-9]+", requested_norm))
            if not candidate_tokens:
                candidate_tokens = set(re.findall(r"[a-z0-9]+", candidate_norm))
            union = requested_tokens | candidate_tokens
            overlap = (len(requested_tokens & candidate_tokens) / len(union)) if union else 0.0

            score = 0.0
            if overlap >= 0.8:
                score = max(score, 0.84 + min(0.1, overlap * 0.1))
            if overlap >= 0.6 and ratio >= 0.86:
                score = max(score, ratio)
            if ratio >= 0.9:
                score = max(score, ratio)

            if score > best_score:
                best_score = score
                best_match = candidate_text

        return best_match if best_score >= 0.86 else ""

    @classmethod
    def _merge_room_type_candidates(cls, *candidate_groups: list[str]) -> list[str]:
        merged: list[str] = []
        seen: set[str] = set()
        for group in candidate_groups:
            if not isinstance(group, list):
                continue
            for raw in group:
                candidate = str(raw or "").strip()
                if not candidate:
                    continue
                if cls._is_generic_room_reference(candidate):
                    continue
                key = cls._normalize_room_type_label(candidate)
                if not key or key in seen:
                    continue
                seen.add(key)
                merged.append(candidate)
        return merged

    @classmethod
    def _select_room_type_from_preference(cls, preference: str, candidates: list[str]) -> str:
        if not candidates:
            return ""
        pref = str(preference or "").strip().lower()
        valid_candidates = [
            str(candidate or "").strip()
            for candidate in candidates
            if str(candidate or "").strip() and not cls._is_generic_room_reference(candidate)
        ]
        if not valid_candidates:
            return ""
        if pref == "cheapest":
            return valid_candidates[0]
        if pref == "luxury":
            premium_candidates = [
                candidate
                for candidate in valid_candidates
                if any(marker in cls._normalize_room_type_label(candidate) for marker in ("suite", "villa", "residence"))
            ]
            if premium_candidates:
                return premium_candidates[-1]
            return valid_candidates[-1]
        return ""

    def _extract_room_type_candidates_from_context_messages(
        self,
        conversation_context: ConversationContext | None,
    ) -> list[str]:
        if conversation_context is None:
            return []
        messages = getattr(conversation_context, "messages", None)
        if not isinstance(messages, list) or not messages:
            return []

        extracted: list[str] = []
        seen: set[str] = set()
        # Use recent assistant turns only. We only need a compact candidate set.
        for msg in reversed(messages[-12:]):
            if getattr(msg, "role", None) != MessageRole.ASSISTANT:
                continue
            content = str(getattr(msg, "content", "") or "").strip()
            if not content:
                continue
            entries = self._extract_structured_room_option_entries(content)
            if not entries:
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                label = str(entry.get("label") or "").strip()
                if not label or self._is_generic_room_reference(label):
                    continue
                key = self._normalize_room_type_label(label)
                if not key or key in seen:
                    continue
                seen.add(key)
                extracted.append(label)
            if extracted:
                # Most recent structured room list is usually the best context.
                break
        return extracted

    @staticmethod
    def _is_bot_instruction_style_action(action_text: str) -> bool:
        text = str(action_text or "").strip().lower()
        if not text:
            return True
        prefixes = (
            "provide ",
            "specify ",
            "share ",
            "enter ",
            "type ",
            "kindly provide",
            "please provide",
            "please specify",
            "request ",
        )
        return any(text.startswith(prefix) for prefix in prefixes)

    @staticmethod
    def _looks_like_bot_question_suggestion(action_text: str) -> bool:
        text = str(action_text or "").strip().lower()
        if not text:
            return True
        if not text.endswith("?"):
            return False

        bot_question_markers = (
            "would you like",
            "what time would you like",
            "are you interested",
            "do you have a preferred",
            "do you have any preferred",
            "please let me know",
            "could you please let me know",
            "kindly let me know",
            "what is your",
            "what's your",
            "may i know your",
            "how many guests",
            "which option would you like",
            "which spa treatment are you interested in",
            "which treatment are you interested in",
            "which package are you interested in",
        )
        if any(marker in text for marker in bot_question_markers):
            return True
        return bool(
            re.search(
                r"\bwhich\b.{0,40}\b(?:treatment|therap(?:y|ies)|package|option)\b.{0,20}\bare you interested in\b",
                text,
            )
        )



    def _finalize_user_query_suggestions(
        self,
        suggestions: list[str],
        state: ConversationState,
        intent: IntentType,
        pending_action: str | None,
        pending_data: dict[str, Any],
        capabilities_summary: dict[str, Any],
    ) -> list[str]:
        """
        Ensure UI bubbles are user-askable prompts, not bot-side instructions.
        """
        confirmation_phrase = str(
            getattr(settings, "chat_confirmation_phrase", "yes confirm")
        ).strip() or "yes confirm"
        if state == ConversationState.AWAITING_CONFIRMATION:
            return [confirmation_phrase, "cancel", "Talk to human"]

        cleaned: list[str] = []
        for item in (suggestions or []):
            text = str(item or "").strip()
            if not text:
                continue
            if self._is_bot_instruction_style_action(text):
                continue
            if self._looks_like_bot_question_suggestion(text):
                rewritten = self._rewrite_bot_question_to_user_query(text)
                if rewritten:
                    cleaned.append(rewritten)
                continue
            cleaned.append(text)

        if not cleaned:
            room_flow = self._is_room_booking_flow(
                raw_intent=intent.value,
                current_pending_action=pending_action,
                pending_action_target=pending_action,
                pending_data=pending_data if isinstance(pending_data, dict) else {},
                response_text="",
            )
            if room_flow:
                cleaned = [
                    "Show available room types",
                    "Show room amenities",
                    "Check availability for my dates",
                    "Talk to human",
                ]
            elif intent == IntentType.ORDER_FOOD:
                cleaned = [
                    "Show menu options",
                    "Suggest popular dishes",
                    "Show vegetarian options",
                    "Talk to human",
                ]
            else:
                cleaned = self._build_contextual_suggested_actions(
                    state,
                    intent,
                    pending_action,
                    capabilities_summary,
                    pending_data,
                )

        deduped: list[str] = []
        seen: set[str] = set()
        excluded_suggestion_keys = {
            "greeting",
            "knowledge query",
            "knowledge_query",
        }
        for action in cleaned:
            key = re.sub(r"\s+", " ", str(action).strip().lower())
            if not key or key in seen:
                continue
            if key in excluded_suggestion_keys:
                continue
            if not self._is_suggested_action_phase_allowed(
                action_text=str(action).strip(),
                pending_data=pending_data if isinstance(pending_data, dict) else {},
                capabilities_summary=capabilities_summary if isinstance(capabilities_summary, dict) else {},
            ):
                continue
            seen.add(key)
            deduped.append(str(action).strip())
            if len(deduped) >= 4:
                break
        # Return empty list so the frontend fallback endpoint generates contextual LLM suggestions.
        # Hardcoded generic chips removed — they were appearing regardless of conversation context.
        return deduped

    def _is_room_detail_collection_context(
        self,
        current_pending_action: str | None,
        state: ConversationState,
        pending_data: dict[str, Any],
        memory_facts: dict[str, Any],
        message: str,
    ) -> bool:
        pending = str(current_pending_action or "").strip().lower()
        if pending in {"collect_room_booking_details", "select_room_type", "confirm_room_booking"}:
            return True
        if state in {ConversationState.AWAITING_INFO, ConversationState.AWAITING_SELECTION, ConversationState.AWAITING_CONFIRMATION}:
            if any(
                key in (pending_data or {})
                for key in ("check_in", "check_out", "stay_checkin_date", "stay_checkout_date", "room_type", "guest_count")
            ):
                return True
            if any(
                key in (memory_facts or {})
                for key in ("check_in", "check_out", "stay_checkin_date", "stay_checkout_date", "party_size")
            ):
                return True
        room_markers = ("room", "stay", "check in", "check-out", "checkout", "checkin", "suite")
        return any(marker in message for marker in room_markers)

    def _handle_low_confidence(
        self,
        intent_result: IntentResult,
        context: ConversationContext,
    ) -> Optional[tuple[str, ConversationState]]:
        """
        Handle low-confidence intent predictions with clarify/escalate behavior.
        """
        if intent_result.intent in (IntentType.CONFIRMATION_YES, IntentType.CONFIRMATION_NO):
            return None

        if context.pending_action in _DETAIL_COLLECTION_PENDING_ACTIONS:
            # Slot-filling flows handle terse messages like "5", "202", "10 pm".
            return None

        if intent_result.confidence >= settings.intent_confidence_threshold:
            return None

        escalation_cfg = config_service.get_escalation_config()
        max_tries = int(escalation_cfg.get("max_clarification_attempts", 3))
        attempts = int(context.pending_data.get("_clarification_attempts", 0)) + 1
        context.pending_data["_clarification_attempts"] = attempts

        if attempts >= max_tries and config_service.is_capability_enabled("human_escalation"):
            escalate_msg = escalation_cfg.get(
                "escalation_message",
                "Let me connect you with our team for better assistance.",
            )
            return escalate_msg, ConversationState.ESCALATED

        return (
            "I want to make sure I understood correctly. Could you share a bit more detail so I can help you accurately?",
            ConversationState.AWAITING_INFO,
        )

    async def _dispatch_to_handler(
        self,
        message: str,
        intent_result: IntentResult,
        context: ConversationContext,
        capabilities: dict,
        db_session=None,
    ) -> Optional[HandlerResult]:
        """Route to the appropriate handler. Handles confirmation intents contextually."""
        intent = intent_result.intent
        pending_action = str(context.pending_action or "").strip().lower()
        unified_complaint_intent, unified_reason, unified_source = self._resolve_unified_complaint_routing_intent(
            message=message,
            effective_intent=intent,
            current_pending_action=context.pending_action,
            pending_action_target=None,
            llm_response_text="",
            llm_ticketing_preference=None,
            next_state=context.state,
        )

        if unified_complaint_intent == IntentType.COMPLAINT:
            complaint_handler = handler_registry.get_handler(IntentType.COMPLAINT)
            if complaint_handler is not None:
                complaint_entities = (
                    dict(intent_result.entities)
                    if isinstance(intent_result.entities, dict)
                    else {}
                )
                complaint_entities.setdefault("ticketing_flow", True)
                if unified_reason:
                    complaint_entities.setdefault("ticket_route_reason", unified_reason)
                if unified_source:
                    complaint_entities.setdefault("ticket_route_source", unified_source)
                complaint_intent_result = IntentResult(
                    intent=IntentType.COMPLAINT,
                    confidence=max(0.0, min(1.0, float(intent_result.confidence or 0.0))),
                    entities=complaint_entities,
                    requires_confirmation=False,
                )
                return await complaint_handler.handle(
                    message,
                    complaint_intent_result,
                    context,
                    capabilities,
                    db_session,
                )

        # Keep room-booking flow context while answering informational room
        # follow-ups (for example: "what rooms are there?").
        if (
            self._is_room_booking_pending_action(pending_action)
            and intent not in (IntentType.CONFIRMATION_YES, IntentType.CONFIRMATION_NO)
            and self._looks_like_room_options_information_query(message)
        ):
            pending_snapshot = context.pending_data if isinstance(context.pending_data, dict) else {}
            room_reference = self._resolve_room_reference_details(
                message=message,
                context=context,
                pending_data=pending_snapshot,
            )
            referenced_room_type = str(room_reference.get("room_type") or "").strip()
            referenced_room_detail = str(room_reference.get("detail") or "").strip()

            followup_queries: list[str] = [message]
            if referenced_room_type:
                followup_queries.insert(0, f"tell me about {referenced_room_type}")

            for followup_query in followup_queries:
                service_match = self._match_service_information_response(
                    followup_query,
                    context,
                    capabilities,
                    allow_pending=True,
                )
                if service_match is not None:
                    return HandlerResult(
                        response_text=str(service_match.get("response_text") or "").strip(),
                        next_state=context.state if context.state in {
                            ConversationState.AWAITING_INFO,
                            ConversationState.AWAITING_SELECTION,
                            ConversationState.AWAITING_CONFIRMATION,
                        } else ConversationState.AWAITING_INFO,
                        pending_action=context.pending_action,
                        pending_data=context.pending_data if isinstance(context.pending_data, dict) else {},
                        suggested_actions=list(service_match.get("suggested_actions") or ["Select room type", "Share number of guests"]),
                        metadata={
                            "room_booking_context_preserved": True,
                            "response_source": "service_catalog_room_followup",
                            "service_id": str(service_match.get("service_id") or ""),
                            "service_match_type": str(service_match.get("match_type") or ""),
                        },
                    )

            if referenced_room_type and referenced_room_detail:
                pending_data = dict(context.pending_data) if isinstance(context.pending_data, dict) else {}
                pending_data["requested_room_type"] = referenced_room_type
                if not self._is_generic_room_reference(referenced_room_type):
                    pending_data["room_type"] = referenced_room_type
                if isinstance(room_reference.get("candidates"), list):
                    pending_data["room_type_candidates"] = room_reference["candidates"]
                pending_data = self._enrich_pending_with_known_facts(
                    pending_data=pending_data,
                    context=context,
                    memory_snapshot={"facts": self._memory_facts_from_context(context)},
                    entities=intent_result.entities if isinstance(intent_result.entities, dict) else {},
                )
                missing_fields = self._missing_room_booking_fields(
                    pending_data=pending_data,
                    memory_facts=self._memory_facts_from_context(context),
                )
                if missing_fields:
                    booking_prompt = self._build_room_booking_missing_details_prompt(
                        missing_fields=missing_fields,
                        pending_data=pending_data,
                    )
                    suggested_actions = (
                        self._build_room_booking_missing_suggested_actions(missing_fields)
                        or ["Share check-in and check-out dates", "Share number of guests"]
                    )
                else:
                    booking_prompt = (
                        f"I can proceed with {referenced_room_type}. "
                        "If everything looks good, please type \"yes confirm\" to continue."
                    )
                    suggested_actions = ["yes confirm", "cancel"]
                response_text = f"{referenced_room_type}: {referenced_room_detail}\n\n{booking_prompt}".strip()
                return HandlerResult(
                    response_text=response_text,
                    next_state=context.state if context.state in {
                        ConversationState.AWAITING_INFO,
                        ConversationState.AWAITING_SELECTION,
                        ConversationState.AWAITING_CONFIRMATION,
                    } else ConversationState.AWAITING_INFO,
                    pending_action="collect_room_booking_details",
                    pending_data=pending_data,
                    suggested_actions=suggested_actions[:4],
                    metadata={
                        "room_booking_context_preserved": True,
                        "response_source": "room_option_reference_followup",
                        "room_type": referenced_room_type,
                        "room_booking_missing_fields": missing_fields,
                    },
                )

        room_intent_context = self._is_room_booking_intent_context(
            message=message,
            intent_result=intent_result,
            context=context,
        )
        if room_intent_context:
            return await self._handle_room_booking_intent_flow(
                message=message,
                intent_result=intent_result,
                context=context,
                capabilities=capabilities,
                db_session=db_session,
            )

        # For confirmation intents, route to the handler that owns the pending action
        if intent in (IntentType.CONFIRMATION_YES, IntentType.CONFIRMATION_NO):
            if context.pending_action == "confirm_service_request":
                service_name = str(context.pending_data.get("service_name") or "your request").strip()
                service_details = str(context.pending_data.get("service_details") or "").strip()
                if intent == IntentType.CONFIRMATION_YES:
                    details_text = f"\nDetails: {service_details}" if service_details else ""
                    return HandlerResult(
                        response_text=(
                            f"Confirmed. I've forwarded your {service_name} request to our team.{details_text}\n\n"
                            "You'll receive an update shortly."
                        ),
                        next_state=ConversationState.COMPLETED,
                        pending_action=None,
                        pending_data={},
                        suggested_actions=config_service.get_quick_actions(limit=4),
                        metadata={
                            "service_request_confirmed": True,
                            "service_name": service_name,
                            "service_details": service_details,
                        },
                    )
                return HandlerResult(
                    response_text="No problem, I won't proceed with that request.",
                    next_state=ConversationState.IDLE,
                    pending_action=None,
                    pending_data={},
                    suggested_actions=config_service.get_quick_actions(limit=4),
                    metadata={"service_request_confirmed": False, "service_name": service_name},
                )

            pending_intent = _PENDING_ACTION_TO_INTENT.get(context.pending_action)
            if pending_intent:
                handler = handler_registry.get_handler(pending_intent)
                if handler:
                    return await handler.handle(
                        message, intent_result, context, capabilities, db_session
                    )
            # No matching handler for the pending action - use simple defaults
            if intent == IntentType.CONFIRMATION_YES:
                return HandlerResult(
                    response_text=self._handle_confirmation_yes(
                        {"pending_action": context.pending_action, "pending_data": context.pending_data}
                    ),
                    next_state=ConversationState.COMPLETED,
                )
            else:
                return HandlerResult(
                    response_text="No problem, I've cancelled that. Is there anything else I can help you with?",
                    next_state=ConversationState.IDLE,
                )

        # Deterministic slot-filling follow-up routing based on pending action.
        if context.pending_action in _DETAIL_COLLECTION_PENDING_ACTIONS:
            pending_intent = _PENDING_ACTION_TO_INTENT.get(context.pending_action)
            if pending_intent:
                handler = handler_registry.get_handler(pending_intent)
                if handler:
                    return await handler.handle(
                        message, intent_result, context, capabilities, db_session
                    )

        # Transport follow-up flow (detail collection).
        msg_lower = message.lower()
        if context.pending_action == "collect_transport_details":
            from handlers.transport_handler import TransportHandler
            transport_handler = TransportHandler()
            return await transport_handler.handle(
                message, intent_result, context, capabilities, db_session
            )

        # Keyword transport routing for actionable requests only.
        if self._should_route_transport_action(msg_lower, intent_result.intent):
            from handlers.transport_handler import TransportHandler
            transport_handler = TransportHandler()
            return await transport_handler.handle(
                message, intent_result, context, capabilities, db_session
            )

        # Standard handler dispatch
        return await handler_registry.dispatch(
            intent_result, message, context, capabilities, db_session
        )


    @staticmethod
    def _looks_like_room_type_preference_reply(msg_lower: str) -> bool:
        text = str(msg_lower or "").strip().lower()
        if not text or "?" in text:
            return False
        preference_markers = (
            "cheapest",
            "budget",
            "affordable",
            "lowest",
            "luxury",
            "luxurious",
            "premium",
            "best",
            "king",
            "twin",
            "suite",
            "ultimate",
            "premier",
            "reserve",
            "prestige",
            "this one",
            "that one",
            "most luxurious",
        )
        if any(marker in text for marker in preference_markers):
            return True
        tokens = re.findall(r"[a-z0-9]+", text)
        if 0 < len(tokens) <= 4 and any(token in {"one", "room", "suite", "king", "twin"} for token in tokens):
            return True
        return False




    def _should_interrupt_pending_flow(
        self,
        message: str,
        intent_result: IntentResult,
        context: ConversationContext,
        capabilities_summary: dict,
    ) -> bool:
        """
        Allow user to switch topics while a slot-filling action is pending.
        """
        pending_action = str(context.pending_action or "").strip()
        if not pending_action:
            return False
        if pending_action not in _DETAIL_COLLECTION_PENDING_ACTIONS:
            return False
        if intent_result.intent in (IntentType.CONFIRMATION_YES, IntentType.CONFIRMATION_NO):
            return False

        msg_lower = str(message or "").strip().lower()
        if not msg_lower:
            return False
        if msg_lower in {"cancel", "stop", "never mind", "nevermind"}:
            return False

        if self._is_room_booking_pending_action(pending_action):
            # Keep room-booking slot context active for room-detail/room-option
            # follow-ups so the user doesn't lose collected booking data.
            if self._looks_like_problem_report(msg_lower):
                return True
            if self._looks_like_room_booking_detail_followup(msg_lower):
                return False
            if self._looks_like_room_options_information_query(msg_lower):
                return False
            if self._looks_like_room_type_preference_reply(msg_lower):
                return False
            if self._looks_like_room_stay_booking_request(msg_lower):
                return False

        if pending_action in {"select_service", "select_restaurant"}:
            if self._looks_like_service_selection(msg_lower, capabilities_summary):
                return False
        elif pending_action == "collect_booking_party_size":
            if self._looks_like_party_size_reply(msg_lower):
                return False
        elif pending_action == "collect_booking_time":
            if self._looks_like_time_reply(msg_lower):
                return False
        elif pending_action == "collect_ticket_room_number":
            if self._looks_like_room_number_reply(msg_lower):
                return False
        elif pending_action == "collect_ticket_issue_details":
            if self._looks_like_ticket_update_note(msg_lower):
                return False
        elif pending_action == "collect_ticket_identity_details":
            if self._looks_like_ticket_identity_details_reply(msg_lower):
                return False
        elif pending_action == "collect_ticket_update_note":
            if self._looks_like_ticket_update_note(msg_lower):
                return False
        elif pending_action == "awaiting_room_number":
            if self._looks_like_room_number_reply(msg_lower):
                return False
        elif pending_action == "awaiting_request_detail":
            if self._looks_like_room_service_detail(msg_lower):
                return False
        elif pending_action == "collect_transport_details":
            if self._looks_like_transport_detail(msg_lower):
                return False
        elif pending_action == "collect_service_details":
            if self._looks_like_service_detail_reply(msg_lower):
                return False

        pending_owner = _PENDING_ACTION_TO_INTENT.get(pending_action)
        if pending_owner and intent_result.intent not in {IntentType.UNCLEAR, pending_owner}:
            return True
        if self._looks_like_problem_report(msg_lower):
            return True
        if self._looks_like_information_query(msg_lower):
            return True
        return False

    def _should_interrupt_pending_flow_for_user_query(
        self,
        *,
        message: str,
        context: ConversationContext,
        capabilities_summary: dict,
    ) -> bool:
        """
        Lightweight interruption check for deterministic user-query shortcuts.
        Uses content heuristics without forcing intent mismatch interruption.
        """
        synthetic_intent = IntentResult(
            intent=IntentType.UNCLEAR,
            confidence=0.5,
            entities={},
        )
        return self._should_interrupt_pending_flow(
            message=message,
            intent_result=synthetic_intent,
            context=context,
            capabilities_summary=capabilities_summary,
        )

    @staticmethod
    def _looks_like_service_selection(msg_lower: str, capabilities_summary: dict) -> bool:
        service_catalog = capabilities_summary.get("service_catalog", [])
        for service in service_catalog:
            name = str(service.get("name") or "").strip().lower()
            if name and (name in msg_lower or msg_lower in name):
                return True
        return False

    @staticmethod
    def _looks_like_party_size_reply(msg_lower: str) -> bool:
        if re.fullmatch(r"\s*\d{1,2}\s*", msg_lower):
            return True
        return bool(re.search(r"\b(?:for|party of)\s+\d{1,2}\b", msg_lower)) or bool(
            re.search(r"\b\d{1,2}\s*(?:people|guests|persons|pax)\b", msg_lower)
        )

    @staticmethod
    def _looks_like_time_reply(msg_lower: str) -> bool:
        if any(token in msg_lower for token in ("today", "tomorrow", "tonight", "am", "pm")):
            return True
        return bool(
            re.search(
                r"\b((?:[01]?\d|2[0-3]):[0-5]\d(?:\s*(?:am|pm))?|(?:[1-9]|1[0-2])\s*(?:am|pm)|(?:[01]?\d|2[0-3])\s*(?:hrs|hours))\b",
                msg_lower,
            )
        )

    @staticmethod
    def _looks_like_room_number_reply(msg_lower: str) -> bool:
        return bool(re.fullmatch(r"\s*[a-z0-9-]{2,10}\s*", msg_lower))


    def _maybe_add_missing_room_number_prompt(
        self,
        *,
        response_text: str,
        ticket_meta: dict[str, Any] | None,
        context: ConversationContext,
        pending_data: dict[str, Any] | None = None,
        entities: dict[str, Any] | None = None,
    ) -> str:
        meta = ticket_meta if isinstance(ticket_meta, dict) else {}
        if str(
            meta.get("ticket_skip_reason")
            or meta.get("ticket_create_skip_reason")
            or meta.get("ticketing_skip_reason")
            or ""
        ).strip().lower() != "missing_required_details":
            return str(response_text or "").strip()

        resolved_phase = self._resolve_current_ticketing_phase(
            context=context,
            pending_data=pending_data if isinstance(pending_data, dict) else {},
            entities=entities if isinstance(entities, dict) else {},
        )
        if resolved_phase != "during_stay":
            return str(response_text or "").strip()

        room_number = self._first_non_empty(
            context.room_number,
            (pending_data or {}).get("room_number") if isinstance(pending_data, dict) else "",
        )
        if room_number:
            return str(response_text or "").strip()

        current = str(response_text or "").strip()
        if "room number" in current.lower():
            return current
        if current:
            return (
                f"{current} To create this service ticket, please share your room number."
            ).strip()
        return "Please share your room number so I can create this service ticket."



    @staticmethod
    def _looks_like_room_service_detail(msg_lower: str) -> bool:
        markers = (
            "clean",
            "cleaning",
            "towel",
            "amenit",
            "laundry",
            "maintenance",
            "ac",
            "room service",
            "housekeeping",
            "soap",
            "shampoo",
        )
        return any(marker in msg_lower for marker in markers)

    @staticmethod
    def _looks_like_transport_detail(msg_lower: str) -> bool:
        markers = ("flight", "terminal", "pickup", "drop", "airport")
        if any(marker in msg_lower for marker in markers):
            return True
        return bool(
            re.search(
                r"\b((?:[01]?\d|2[0-3]):[0-5]\d(?:\s*(?:am|pm))?|(?:[1-9]|1[0-2])\s*(?:am|pm)|(?:[01]?\d|2[0-3])\s*(?:hrs|hours))\b",
                msg_lower,
            )
        )

    @staticmethod
    def _looks_like_service_detail_reply(msg_lower: str) -> bool:
        if not msg_lower:
            return False
        if "?" in msg_lower:
            return False
        detail_markers = (
            "from",
            "to",
            "date",
            "dates",
            "night",
            "nights",
            "guest",
            "guests",
            "people",
            "person",
            "check in",
            "check out",
            "checkin",
            "checkout",
            "room",
            "stay",
            "arrival",
            "departure",
        )
        if any(marker in msg_lower for marker in detail_markers):
            return True
        if re.search(r"\b\d{1,2}(?:st|nd|rd|th)?\b", msg_lower):
            return True
        if re.search(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", msg_lower):
            return True
        if re.search(r"\b((?:[01]?\d|2[0-3]):[0-5]\d(?:\s*(?:am|pm))?|(?:[1-9]|1[0-2])\s*(?:am|pm))\b", msg_lower):
            return True
        return len(msg_lower.split()) >= 3





    def _handle_service_detail_followup(
        self,
        message: str,
        context: ConversationContext,
        capabilities_summary: dict,
    ) -> Optional[HandlerResult]:
        msg = str(message or "").strip()
        msg_lower = msg.lower()
        if not msg:
            return HandlerResult(
                response_text="Please share the details you'd like me to record for this request.",
                next_state=ConversationState.AWAITING_INFO,
                pending_action="collect_service_details",
                pending_data=context.pending_data,
                suggested_actions=["Share dates", "Share preferred time", "Cancel"],
            )

        if msg_lower in {"cancel", "stop", "no", "nope", "never mind", "nevermind"}:
            return HandlerResult(
                response_text="No problem. I've cancelled this request. Let me know if you need anything else.",
                next_state=ConversationState.IDLE,
                pending_action=None,
                pending_data={},
                suggested_actions=config_service.get_quick_actions(limit=4),
            )

        service_name = str(context.pending_data.get("service_name") or "this service").strip()
        service_id = str(context.pending_data.get("service_id") or "").strip()
        details = dict(context.pending_data or {})
        details["service_details"] = msg

        return HandlerResult(
            response_text=(
                f"Thanks, I've noted your {service_name} request details: {msg}. "
                "I can proceed with this request now. Shall I confirm and continue?"
            ),
            next_state=ConversationState.AWAITING_CONFIRMATION,
            pending_action="confirm_service_request",
            pending_data=details,
            suggested_actions=["Yes, confirm", "No, cancel"],
            metadata={"service_id": service_id, "service_name": service_name},
        )

    async def _handle_service_catalog_unavailable_handoff(
        self,
        *,
        request: ChatRequest,
        context: ConversationContext,
        capabilities_summary: dict[str, Any],
        service_match: dict[str, Any],
        db_session=None,
        routing_path_tag: str,
        pending_interrupted: bool,
    ) -> ChatResponse | None:
        """
        Auto-handoff flow for action requests against services that are not configured.
        """
        service_name = str(service_match.get("service_name") or "").strip()
        escalation_intent = IntentResult(
            intent=IntentType.HUMAN_REQUEST,
            confidence=0.88,
            entities={
                "requested_service": service_name,
                "service_match_type": str(service_match.get("match_type") or ""),
                "handoff_reason": "service_not_configured",
            },
        )
        effective_user_message = str(request.message or "").strip()
        handler_result = await self._dispatch_to_handler(
            effective_user_message,
            escalation_intent,
            context,
            capabilities_summary,
            db_session,
        )
        if handler_result is None:
            return None

        internal_pending_entries = conversation_memory_service.internal_pending_entries(context.pending_data)
        next_state = handler_result.next_state
        if handler_result.pending_action is not None:
            context.pending_action = handler_result.pending_action
        if handler_result.pending_data is not None:
            context.pending_data = conversation_memory_service.merge_with_internal(
                handler_result.pending_data,
                internal_pending_entries,
            )
        if handler_result.pending_action is None and next_state not in (
            ConversationState.AWAITING_CONFIRMATION,
            ConversationState.AWAITING_INFO,
        ):
            context.pending_action = None
            context.pending_data = conversation_memory_service.retain_safe_internal_entries(
                context.pending_data,
            )
            context.pending_data.pop("_clarification_attempts", None)
        if handler_result.metadata.get("room_number"):
            context.room_number = str(handler_result.metadata["room_number"])
        context.state = next_state

        fallback_prefix = str(service_match.get("response_text") or "").strip()
        selected_phase_context = self._get_selected_phase_context(
            context=context,
            pending_data=context.pending_data if isinstance(context.pending_data, dict) else {},
            entities=escalation_intent.entities if isinstance(escalation_intent.entities, dict) else {},
        )
        prefix = await self._compose_service_catalog_unavailable_preface(
            requested_service=service_name,
            user_message=effective_user_message,
            capabilities_summary=capabilities_summary,
            fallback_text=fallback_prefix,
            selected_phase_id=selected_phase_context.get("selected_phase_id", ""),
            selected_phase_name=selected_phase_context.get("selected_phase_name", ""),
        )
        if prefix:
            response_text = f"{prefix}\n\n{handler_result.response_text}"
        else:
            response_text = handler_result.response_text
        response_text, resume_actions = self._append_resume_checkpoint(
            response_text=response_text,
            context=context,
            pending_interrupted=pending_interrupted,
        )

        assistant_metadata = {
            "intent": escalation_intent.intent.value,
            "confidence": escalation_intent.confidence,
            "channel": context.channel,
            "service_id": service_match.get("service_id"),
            "service_name": service_name,
            "service_match_type": service_match.get("match_type"),
            "service_catalog_handoff": True,
        }
        if handler_result.metadata:
            assistant_metadata.update(handler_result.metadata)
        context.add_message(
            MessageRole.ASSISTANT,
            response_text,
            metadata=assistant_metadata,
        )
        conversation_memory_service.capture_assistant_message(
            context,
            response_text,
            metadata=assistant_metadata,
        )
        await self._refresh_summary_async(context)
        await context_manager.save_context(context, db_session=db_session)
        memory_snapshot = conversation_memory_service.get_snapshot(context)

        if handler_result.suggested_actions:
            suggested_actions = resume_actions + handler_result.suggested_actions
        else:
            suggested_actions = resume_actions + list(service_match.get("suggested_actions") or [])
        suggested_actions = self._finalize_user_query_suggestions(
            suggested_actions,
            state=next_state,
            intent=escalation_intent.intent,
            pending_action=context.pending_action,
            pending_data=context.pending_data,
            capabilities_summary=capabilities_summary,
        )

        metadata = {
            "message_count": len(context.messages),
            "entities": escalation_intent.entities,
            "routing_path": ProcessingPath.SIMPLE.value,
            "routing_score": 1.0,
            "routing_signals": {"path": routing_path_tag},
            "pending_interrupted": pending_interrupted,
            "resume_checkpoint": bool(resume_actions),
            "parked_task_available": self._peek_parked_task(context) is not None,
            "service_catalog_match": True,
            "service_id": service_match.get("service_id"),
            "service_match_type": service_match.get("match_type"),
            "service_catalog_handoff": True,
            "memory_summary": memory_snapshot.get("summary", ""),
            "memory_facts": memory_snapshot.get("facts", {}),
        }
        if handler_result.metadata:
            metadata.update(handler_result.metadata)

        return ChatResponse(
            session_id=request.session_id,
            message=response_text,
            intent=escalation_intent.intent,
            confidence=escalation_intent.confidence,
            state=next_state,
            suggested_actions=suggested_actions,
            metadata=metadata,
        )

    async def _compose_service_catalog_unavailable_preface(
        self,
        *,
        requested_service: str,
        user_message: str,
        capabilities_summary: dict[str, Any],
        fallback_text: str,
        selected_phase_id: str = "",
        selected_phase_name: str = "",
    ) -> str:
        """
        Build a natural handoff preface for unconfigured requests.
        Uses LLM when available; falls back to deterministic text.
        """
        fallback = str(fallback_text or "").strip()
        if not str(settings.openai_api_key or "").strip():
            return fallback

        service_label = str(requested_service or "").strip() or "that request"
        available_services: list[str] = []
        for service in capabilities_summary.get("service_catalog", []):
            if not isinstance(service, dict):
                continue
            if not bool(service.get("is_active", True)):
                continue
            name = str(service.get("name") or service.get("id") or "").strip()
            if not name:
                continue
            if name not in available_services:
                available_services.append(name)
            if len(available_services) >= 8:
                break
        available_text = ", ".join(available_services) if available_services else "configured hotel services"
        phase_label = str(selected_phase_name or "").strip() or (
            str(selected_phase_id or "").replace("_", " ").title()
        )
        phase_id = str(selected_phase_id or "").strip() or "unknown"

        prompt = (
            "Write one short guest-facing preface before human handoff.\n"
            "Context:\n"
            f"- Requested service: {service_label}\n"
            f"- User message: {str(user_message or '').strip()[:280] or '(none)'}\n"
            f"- Available services: {available_text}\n\n"
            f"- Selected phase: {phase_label} ({phase_id})\n\n"
            "Rules:\n"
            "1) Do not promise booking/order completion.\n"
            "2) Clearly say staff will assist manually.\n"
            "3) Keep it concise and natural (max 2 short sentences).\n"
            "4) Return plain text only."
        )
        try:
            rendered = await llm_client.chat(
                messages=[
                    {"role": "system", "content": "You write concise guest support responses."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
                max_tokens=120,
            )
        except Exception:
            return fallback

        text = " ".join(str(rendered or "").split()).strip()
        if not text:
            return fallback

        lower = text.lower()
        if any(
            marker in lower
            for marker in (
                "booked",
                "confirmed",
                "placed",
                "arranged",
                "i will arrange",
                "i'll arrange",
                "i can arrange",
            )
        ):
            return fallback
        if "staff" not in lower and "team" not in lower and "human" not in lower:
            return fallback
        return text

    @staticmethod
    def _looks_like_information_query(msg_lower: str) -> bool:
        if "?" in msg_lower:
            return True
        normalized = re.sub(r"\s+", " ", str(msg_lower or "").strip().lower())
        if not normalized:
            return False
        if ChatService._looks_like_imperative_information_request(normalized):
            return True
        tokens = re.findall(r"[a-z]+", normalized)
        if not tokens:
            return False
        if tokens[0] in _INFORMATION_QUERY_PREFIXES:
            return True
        return any(
            token in normalized
            for token in (
                "timing",
                "hours",
                "price",
                "cost",
                "available",
                "address",
                "location",
            )
        )

    @staticmethod
    def _looks_like_imperative_information_request(msg_lower: str) -> bool:
        normalized = re.sub(r"\s+", " ", str(msg_lower or "").strip().lower())
        if not normalized:
            return False
        if any(normalized.startswith(prefix) for prefix in _INFORMATION_QUERY_IMPERATIVE_PREFIXES):
            return True
        imperative_markers = (
            "tell me about",
            "more about",
            "give me the address",
            "share the address",
            "share details",
            "hotel address",
        )
        return any(marker in normalized for marker in imperative_markers)

    @staticmethod
    def _looks_like_problem_report(msg_lower: str) -> bool:
        normalized = re.sub(r"\s+", " ", str(msg_lower or "").strip().lower())
        if not normalized:
            return False
        direct_markers = (
            "booking system",
            "bookingsystem",
            "cannot access",
            "can't access",
            "cant access",
            "unable to access",
            "not able to access",
        )
        if any(marker in normalized for marker in direct_markers):
            return True

        issue_markers = (
            "not able",
            "unable",
            "can't",
            "cannot",
            "cant",
            "issue",
            "problem",
            "error",
            "failed",
            "not working",
        )
        system_markers = (
            "website",
            "site",
            "portal",
            "system",
            "app",
            "booking",
            "login",
            "sign in",
            "signin",
            "access",
        )
        return any(marker in normalized for marker in issue_markers) and any(
            marker in normalized for marker in system_markers
        )

    def _check_capability_for_intent(
        self,
        hotel_code: str,
        intent_result: IntentResult,
        message: str,
    ) -> CapabilityCheck:
        """Check if the detected intent is allowed by business capabilities from config_service."""
        intent = intent_result.intent
        entities = intent_result.entities

        # Map intents to capability checks using config_service
        if intent == IntentType.ORDER_FOOD:
            # Check if food ordering is enabled
            if not self._is_any_capability_enabled(
                "food_ordering",
                "room_service",
                "order_placement",
            ):
                return CapabilityCheck(
                    allowed=False,
                    reason="Food ordering is not available at this time.",
                    alternatives=config_service.get_quick_actions(limit=4),
                )
            # Check room-delivery restrictions only when user explicitly requests
            # room delivery and mentions a specific service.
            service_target = str(
                entities.get("service")
                or entities.get("restaurant")
                or entities.get("restaurant_name")
                or ""
            ).strip()
            message_service_target = self._extract_service_target_from_message(message)
            if message_service_target:
                service_target = message_service_target
            wants_room_delivery = bool(
                re.search(r"\b(room|to my room|in my room|room delivery|deliver to room)\b", message.lower())
            )
            explicitly_mentioned = self._entity_explicitly_mentioned_in_message(message, service_target)
            if service_target and wants_room_delivery and explicitly_mentioned:
                if config_service.can_deliver_to_room(service_target.lower()):
                    return CapabilityCheck(allowed=True, reason="Room delivery available")
                return CapabilityCheck(
                    allowed=False,
                    reason=f"{service_target} is dine-in only and does not currently support room delivery.",
                    alternatives=config_service.get_quick_actions(limit=4),
                )
            return CapabilityCheck(allowed=True, reason="Food ordering available")

        if intent == IntentType.TABLE_BOOKING:
            if not self._is_any_capability_enabled("table_booking", "appointment_booking"):
                return CapabilityCheck(
                    allowed=False,
                    reason="Booking is not available at this time.",
                    alternatives=config_service.get_quick_actions(limit=4),
                )
            return CapabilityCheck(allowed=True, reason="Table booking available")

        if intent == IntentType.MENU_REQUEST:
            # Keep this intent as a backward-compatible alias for knowledge lookup.
            if not config_service.is_menu_runtime_enabled():
                return CapabilityCheck(
                    allowed=True,
                    reason="Knowledge-base lookup allowed while menu runtime is disabled.",
                )
            return CapabilityCheck(allowed=True, reason="Menu capability available")

        if intent == IntentType.HUMAN_REQUEST:
            if not config_service.is_capability_enabled("human_escalation"):
                return CapabilityCheck(
                    allowed=False,
                    reason="Human support is temporarily unavailable. Please try again later.",
                )
            return CapabilityCheck(allowed=True, reason="Human escalation available")

        msg_lower = str(message or "").lower()

        # Check transport capability
        if "cab" in msg_lower or "taxi" in msg_lower or "transport" in msg_lower:
            asks_local_transport = any(
                marker in msg_lower
                for marker in ("local cab", "local taxi", "city cab", "within city", "around city")
            )
            asks_airport_transfer = any(
                marker in msg_lower
                for marker in ("airport", "terminal", "pickup", "drop", "transfer")
            )
            if asks_local_transport and not asks_airport_transfer:
                if not self._is_any_capability_enabled("local_transport", "local_cab"):
                    return CapabilityCheck(
                        allowed=False,
                        reason=(
                            "That specific transport request is not configured for instant booking here yet. "
                            "I can connect you with our staff team to assist manually."
                        ),
                        alternatives=["Connect to staff", "Transport details", "Need help"],
                    )
            if not self._is_any_capability_enabled(
                "transport",
                "local_transport",
                "airport_transfer",
            ):
                return CapabilityCheck(
                    allowed=False,
                    reason="Transport services are not available at this time.",
                )

        # Check spa booking
        if "spa" in message.lower() or "massage" in message.lower():
            if not config_service.is_capability_enabled("spa_booking"):
                return CapabilityCheck(
                    allowed=False,
                    reason="Spa booking is not available at this time.",
                )

        # Default: allow
        return CapabilityCheck(allowed=True, reason="Action allowed")

    @staticmethod
    def _is_any_capability_enabled(*capability_ids: str) -> bool:
        """Return True if at least one provided capability flag is enabled."""
        return any(config_service.is_capability_enabled(cap_id) for cap_id in capability_ids)

    @staticmethod
    def _entity_explicitly_mentioned_in_message(message: str, entity_value: str) -> bool:
        """Check whether an extracted entity text is explicitly present in the user message."""
        entity = str(entity_value or "").strip().lower()
        if not entity:
            return False

        entity_compact = re.sub(r"[^a-z0-9]+", " ", entity).strip()
        msg_compact = re.sub(r"[^a-z0-9]+", " ", str(message or "").lower()).strip()
        if not entity_compact or not msg_compact:
            return False
        if entity_compact in msg_compact:
            return True

        entity_tokens = [token for token in entity_compact.split() if token]
        msg_tokens = [token for token in msg_compact.split() if token]
        if not entity_tokens or not msg_tokens:
            return False

        for token in entity_tokens:
            if any(
                token == msg_token
                or (len(token) >= 5 and (token.startswith(msg_token[:4]) or msg_token.startswith(token[:4])))
                or (len(token) >= 5 and SequenceMatcher(a=token, b=msg_token).ratio() >= 0.86)
                for msg_token in msg_tokens
            ):
                continue
            return False
        return True

    @staticmethod
    def _extract_service_target_from_message(message: str) -> str:
        """
        Extract explicit service mentions from delivery phrasing like:
        "deliver X from kadak to my room".
        """
        msg_lower = str(message or "").strip().lower()
        if not msg_lower:
            return ""

        patterns = (
            r"\bfrom\s+(?:the\s+)?([a-z][a-z0-9&' -]{1,40}?)(?=\s+(?:to|for|in|at|with|now|please)\b|[?.!,]|$)",
            r"\bat\s+(?:the\s+)?([a-z][a-z0-9&' -]{1,40}?)(?=\s+(?:to|for|in|with|now|please)\b|[?.!,]|$)",
        )
        for pattern in patterns:
            match = re.search(pattern, msg_lower)
            if not match:
                continue
            candidate = re.sub(r"\s+", " ", match.group(1)).strip(" .,!?:;")
            if not candidate or candidate.isdigit():
                continue
            return candidate
        return ""

    @staticmethod
    def _is_transport_info_query(msg_lower: str) -> bool:
        markers = (
            "what is",
            "what are",
            "how much",
            "price",
            "cost",
            "charge",
            "charges",
            "rate",
            "rates",
            "timing",
            "timings",
            "do you provide",
            "can you provide",
            "available",
        )
        return any(marker in msg_lower for marker in markers)

    def _should_route_transport_action(self, msg_lower: str, intent: IntentType) -> bool:
        transport_keywords = ("cab", "taxi", "transport", "airport", "transfer", "pickup", "drop", "ride")
        if not any(kw in msg_lower for kw in transport_keywords):
            return False

        action_markers = (
            "book",
            "arrange",
            "need",
            "want",
            "send",
            "schedule",
            "confirm",
            "get me",
            "pick me",
            "drop me",
            "transfer me",
        )
        is_actionable = any(marker in msg_lower for marker in action_markers)

        # FAQ-like transport questions should stay on retrieval/FAQ path.
        if intent == IntentType.FAQ and not is_actionable:
            return False
        if self._is_transport_info_query(msg_lower) and not is_actionable:
            return False

        return is_actionable

    def _check_intent_enabled(self, intent: IntentType) -> CapabilityCheck:
        """
        Validate whether a classified intent is enabled in admin config.
        Keeps the bot industry-agnostic by honoring configured intent toggles.
        """
        # Internal/system intents are always allowed
        if intent in {
            IntentType.CONFIRMATION_YES,
            IntentType.CONFIRMATION_NO,
            IntentType.UNCLEAR,
            IntentType.OUT_OF_SCOPE,
        }:
            return CapabilityCheck(allowed=True, reason="System intent allowed")

        if any(config_service.is_intent_enabled(intent_id) for intent_id in self._intent_ids_for_enablement(intent)):
            return CapabilityCheck(allowed=True, reason="Intent enabled")

        return CapabilityCheck(
            allowed=False,
            reason="That workflow is currently disabled for this business configuration.",
            alternatives=config_service.get_quick_actions(limit=4),
        )







    @staticmethod
    def _service_aliases_for_matching(service: dict) -> list[str]:
        aliases: list[str] = []
        service_name = str(service.get("name") or "").strip().lower()
        service_id = str(service.get("id") or "").strip().lower().replace("_", " ")

        if service_name and (
            service_name not in _GENERIC_SERVICE_ALIASES or len(service_name.split()) > 1
        ):
            aliases.append(service_name)
        if (
            service_id
            and service_id != service_name
            and service_id not in _GENERIC_SERVICE_ALIASES
        ):
            aliases.append(service_id)

        # Add compact aliases by removing generic suffixes like "booking"/"service".
        compact_source_tokens = [
            token
            for token in re.findall(r"[a-z0-9]+", f"{service_name} {service_id}")
            if token
        ]
        removable_tokens = {
            "booking",
            "bookings",
            "service",
            "services",
            "request",
            "requests",
            "support",
            "desk",
            "assistance",
            "help",
        }
        compact_tokens = [token for token in compact_source_tokens if token not in removable_tokens]
        if compact_tokens:
            aliases.append(" ".join(compact_tokens[:3]))
            for token in compact_tokens[:3]:
                if (
                    len(token) >= 3
                    and token not in _GENERIC_SERVICE_ALIASES
                    and token not in _BROAD_FIRST_ALIAS_TOKENS
                    and token not in _SERVICE_DESCRIPTION_STOPWORDS
                ):
                    aliases.append(token)

        seen: set[str] = set()
        deduped: list[str] = []
        for alias in aliases:
            clean = alias.strip()
            if clean and clean not in seen:
                deduped.append(clean)
                seen.add(clean)
        return deduped

    @staticmethod
    def _is_spa_service_entry(service: dict[str, Any]) -> bool:
        text = " ".join(
            [
                str(service.get("id") or ""),
                str(service.get("name") or ""),
                str(service.get("type") or ""),
                str(service.get("description") or service.get("cuisine") or ""),
            ]
        ).strip().lower()
        if not text:
            return False
        return any(marker in text for marker in ("spa", "massage", "wellness", "therapy"))

    @staticmethod
    def _looks_like_spa_catalog_query(msg_lower: str) -> bool:
        text = str(msg_lower or "").strip().lower()
        if not text:
            return False
        if not any(marker in text for marker in ("spa", "massage", "wellness", "therapy")):
            return False
        return any(
            marker in text
            for marker in (
                "show",
                "list",
                "treatment",
                "treatments",
                "package",
                "packages",
                "therapist",
                "therapists",
                "menu",
                "options",
            )
        )

    @staticmethod
    def _extract_spa_option_hints(service: dict[str, Any]) -> list[str]:
        description = str(service.get("description") or service.get("cuisine") or "").strip()
        if not description:
            return []
        candidates = re.split(r"[,|;/\n]+", description)
        options: list[str] = []
        blocked_fragments = {
            "spa",
            "booking",
            "service",
            "services",
            "appointment",
            "appointments",
            "available",
            "timing",
            "hours",
            "assistance",
            "support",
        }
        for candidate in candidates:
            cleaned = re.sub(r"\s+", " ", str(candidate or "").strip(" .-"))
            if len(cleaned) < 4 or len(cleaned) > 40:
                continue
            lowered = cleaned.lower()
            if any(fragment in lowered for fragment in blocked_fragments):
                continue
            title = cleaned.title()
            if title not in options:
                options.append(title)
            if len(options) >= 8:
                break
        return options

    def _extract_unconfigured_service_request_label(
        self,
        msg_lower: str,
        service_catalog: list[dict[str, Any]],
    ) -> str:
        text = str(msg_lower or "").strip().lower()
        if not text:
            return ""

        action_markers = ("book", "reserve", "order", "arrange", "need", "want", "request", "schedule")
        if not any(marker in text for marker in action_markers):
            return ""

        known_tokens: set[str] = set()
        known_aliases: set[str] = set()
        for service in service_catalog:
            if not isinstance(service, dict):
                continue
            for alias in self._service_aliases_for_matching(service):
                alias_text = str(alias or "").strip().lower()
                if alias_text:
                    known_aliases.add(alias_text)
                known_tokens.update(re.findall(r"[a-z0-9]+", alias.lower()))

        match = re.search(
            r"\b(?:book|reserve|order|arrange|need|want|get|request|schedule)\b\s+(?:a|an|the|for)?\s*([a-z][a-z0-9&'/ -]{2,48})",
            text,
        )
        if not match:
            return ""

        candidate = re.split(r"\b(?:for|at|on|to|from|in|by|with|tomorrow|today|tonight)\b", match.group(1), maxsplit=1)[0]
        candidate = re.sub(r"\s+", " ", candidate).strip(" .,!?:;")
        if not candidate:
            return ""

        generic_tokens = {
            "booking",
            "reservation",
            "service",
            "services",
            "help",
            "assistance",
            "something",
            "anything",
        }
        supported_core_tokens = {
            "room",
            "table",
            "food",
            "menu",
            "spa",
            "massage",
            "restaurant",
            "dining",
        }
        candidate_tokens = set(re.findall(r"[a-z0-9]+", candidate))
        candidate_tokens -= generic_tokens
        if not candidate_tokens:
            return ""
        if candidate_tokens & supported_core_tokens:
            return ""
        if candidate_tokens & known_tokens:
            return ""

        return candidate.title()

    @staticmethod
    def _service_entry_supports_request_label(service: dict[str, Any], requested_label: str) -> bool:
        """
        Validate whether a matched service actually covers the requested label.
        Prevents broad aliases (for example generic transport) from auto-mapping
        to a different sub-service.
        """
        if not isinstance(service, dict):
            return False
        label_text = str(requested_label or "").strip().lower()
        if not label_text:
            return False

        service_text = " ".join(
            [
                str(service.get("id") or ""),
                str(service.get("name") or ""),
                str(service.get("type") or ""),
                str(service.get("description") or service.get("cuisine") or ""),
            ]
        ).strip().lower()
        if not service_text:
            return False

        ignored_tokens = {
            "booking",
            "service",
            "services",
            "request",
            "requests",
            "support",
            "assistance",
            "help",
            "instant",
            "manual",
        }
        label_tokens = [
            token
            for token in re.findall(r"[a-z0-9]+", label_text)
            if len(token) >= 3 and token not in ignored_tokens
        ]
        if not label_tokens:
            return False

        service_tokens = re.findall(r"[a-z0-9]+", service_text)
        matched_tokens = 0
        for token in label_tokens:
            if re.search(rf"\b{re.escape(token)}\b", service_text):
                matched_tokens += 1
                continue
            if len(token) >= 4 and any(
                (
                    len(service_token) >= 4
                    and (
                        service_token.startswith(token[:4])
                        or token.startswith(service_token[:4])
                    )
                )
                for service_token in service_tokens
            ):
                matched_tokens += 1

        required_matches = len(label_tokens) if len(label_tokens) <= 2 else max(2, len(label_tokens) - 1)
        return matched_tokens >= required_matches

    @staticmethod
    def _build_service_catalog_unavailable_match(requested_service_label: str) -> dict[str, Any]:
        label = str(requested_service_label or "").strip() or "This request"
        return {
            "service_id": "",
            "service_name": label,
            "match_type": "service_catalog_unavailable_handoff",
            "response_text": (
                f"{label} is not currently configured for instant bot fulfillment. "
                "I can connect you with our staff team to assist manually."
            ),
            "suggested_actions": ["Connect to staff", "Show available services", "Ask another question"],
        }








    @staticmethod
    def _memory_facts_from_context(context: ConversationContext) -> dict[str, Any]:
        pending = context.pending_data if isinstance(context.pending_data, dict) else {}
        memory = pending.get("_memory", {})
        if not isinstance(memory, dict):
            return {}
        facts = memory.get("facts", {})
        return facts if isinstance(facts, dict) else {}

    @staticmethod
    def _parse_compact_calendar_date(value: Any, *, reference_year: int | None = None) -> date | None:
        text = str(value or "").strip()
        if not text:
            return None
        text = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", text, flags=re.IGNORECASE)
        text = re.sub(r"\s+", " ", text).strip()

        iso_match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
        if iso_match:
            try:
                return date(int(iso_match.group(1)), int(iso_match.group(2)), int(iso_match.group(3)))
            except ValueError:
                return None

        month_first_match = re.fullmatch(
            r"([A-Za-z]{3,9})\s+(\d{1,2})(?:\s*,?\s*(\d{4}))?",
            text,
            flags=re.IGNORECASE,
        )
        day_first_match = re.fullmatch(
            r"(\d{1,2})\s+([A-Za-z]{3,9})(?:\s*,?\s*(\d{4}))?",
            text,
            flags=re.IGNORECASE,
        )

        parsed: date | None = None
        if month_first_match:
            month_token = month_first_match.group(1)
            day_token = month_first_match.group(2)
            year_token = month_first_match.group(3)
            fmt = "%b %d %Y"
            fmt_long = "%B %d %Y"
            year = int(year_token) if year_token else int(reference_year or date.today().year)
            compact = f"{month_token} {day_token} {year}"
            for parser in (fmt, fmt_long):
                try:
                    parsed = datetime.strptime(compact, parser).date()
                    break
                except ValueError:
                    continue
        elif day_first_match:
            day_token = day_first_match.group(1)
            month_token = day_first_match.group(2)
            year_token = day_first_match.group(3)
            fmt = "%d %b %Y"
            fmt_long = "%d %B %Y"
            year = int(year_token) if year_token else int(reference_year or date.today().year)
            compact = f"{day_token} {month_token} {year}"
            for parser in (fmt, fmt_long):
                try:
                    parsed = datetime.strptime(compact, parser).date()
                    break
                except ValueError:
                    continue

        if parsed is None:
            return None

        if reference_year is None and not re.search(r"\b\d{4}\b", text):
            today_local = date.today()
            if parsed < (today_local - timedelta(days=183)):
                try:
                    parsed = parsed.replace(year=parsed.year + 1)
                except ValueError:
                    pass
        return parsed

    @classmethod
    def _extract_explicit_dates_from_message(cls, message: str) -> list[date]:
        text = str(message or "").strip().lower()
        if not text:
            return []

        dates: list[date] = []
        seen: set[str] = set()
        month_token = (
            r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
            r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
        )

        patterns = (
            rf"\b({month_token})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:\s*,?\s*(\d{{4}}))?\b",
            rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({month_token})(?:\s*,?\s*(\d{{4}}))?\b",
        )
        for pattern in patterns:
            for match in re.finditer(pattern, text, flags=re.IGNORECASE):
                chunk = " ".join(part for part in match.groups() if part)
                parsed = cls._parse_compact_calendar_date(chunk)
                if parsed is None:
                    continue
                key = parsed.isoformat()
                if key in seen:
                    continue
                seen.add(key)
                dates.append(parsed)

        today_local = date.today()
        relative_markers = {
            "today": today_local,
            "tomorrow": today_local + timedelta(days=1),
            "day after tomorrow": today_local + timedelta(days=2),
        }
        for marker, parsed in relative_markers.items():
            if marker in text:
                key = parsed.isoformat()
                if key in seen:
                    continue
                seen.add(key)
                dates.append(parsed)

        return dates

    def _resolve_known_stay_window(
        self,
        *,
        context: ConversationContext,
        pending_data: dict[str, Any] | None = None,
        entities: dict[str, Any] | None = None,
    ) -> tuple[date, date] | None:
        pending = pending_data if isinstance(pending_data, dict) else {}
        entity_map = entities if isinstance(entities, dict) else {}
        integration = ticketing_service.get_integration_context(context)
        facts = self._memory_facts_from_context(context)

        check_in_text = self._first_non_empty(
            entity_map.get("check_in"),
            entity_map.get("stay_checkin_date"),
            pending.get("check_in"),
            pending.get("stay_checkin_date"),
            pending.get("checkin_date"),
            integration.get("check_in"),
            integration.get("stay_checkin_date"),
            facts.get("check_in"),
            facts.get("stay_checkin_date"),
            facts.get("checkin_date"),
        )
        check_out_text = self._first_non_empty(
            entity_map.get("check_out"),
            entity_map.get("stay_checkout_date"),
            pending.get("check_out"),
            pending.get("stay_checkout_date"),
            pending.get("checkout_date"),
            integration.get("check_out"),
            integration.get("stay_checkout_date"),
            facts.get("check_out"),
            facts.get("stay_checkout_date"),
            facts.get("checkout_date"),
        )
        if not check_in_text or not check_out_text:
            return None

        current_year = date.today().year
        parsed_check_in = self._parse_compact_calendar_date(check_in_text, reference_year=current_year)
        parsed_check_out = self._parse_compact_calendar_date(check_out_text, reference_year=current_year)
        if parsed_check_in is None or parsed_check_out is None:
            return None

        if parsed_check_out < parsed_check_in:
            try:
                parsed_check_out = parsed_check_out.replace(year=parsed_check_out.year + 1)
            except ValueError:
                return None
        if parsed_check_out < parsed_check_in:
            return None
        return (parsed_check_in, parsed_check_out)

    @classmethod
    def _format_calendar_date_label(cls, value: date) -> str:
        try:
            return value.strftime("%b %d")
        except Exception:
            return str(value)




    def _detect_contextual_sanity_issue(
        self,
        *,
        message: str,
        intent: IntentType,
        context: ConversationContext,
        pending_data: dict[str, Any] | None = None,
        entities: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        pending = pending_data if isinstance(pending_data, dict) else {}
        entity_map = entities if isinstance(entities, dict) else {}
        text = str(message or "").strip()
        msg_lower = text.lower()
        if not text:
            return None

        requested_dates = self._extract_explicit_dates_from_message(text)
        room_booking_context = (
            self._is_room_booking_pending_action(context.pending_action)
            or self._looks_like_room_stay_booking_request(text)
            or any(marker in msg_lower for marker in ("check in", "check-in", "check out", "check-out"))
        )
        if room_booking_context and requested_dates:
            today_local = date.today()
            contains_past_date = any(day < today_local for day in requested_dates)
            if contains_past_date:
                today_label = self._format_calendar_date_full(today_local)
                return {
                    "code": "room_booking_date_in_past",
                    "response_text": (
                        f"I noticed at least one stay date is in the past. "
                        f"Today's date is {today_label}. "
                        "Please share check-in and check-out dates from today onward."
                    ),
                    "suggested_actions": [
                        "Share check-in and check-out dates",
                        "Modify stay dates",
                    ],
                }

        actionable_intents = {
            IntentType.ORDER_FOOD,
            IntentType.TABLE_BOOKING,
            IntentType.ROOM_SERVICE,
            IntentType.COMPLAINT,
            IntentType.HUMAN_REQUEST,
        }
        is_action_like = (
            intent in actionable_intents
            or self._is_action_request_text(text)
            or self._looks_like_operational_issue_for_ticketing(msg_lower)
            or self._looks_like_booking_change_request(text)
        )

        if is_action_like:
            stay_window = self._resolve_known_stay_window(
                context=context,
                pending_data=pending,
                entities=entity_map,
            )
            if stay_window is not None and requested_dates:
                check_in, check_out = stay_window
                out_of_window = [day for day in requested_dates if day < check_in or day > check_out]
                if out_of_window:
                    check_in_label = self._format_calendar_date_label(check_in)
                    check_out_label = self._format_calendar_date_label(check_out)
                    return {
                        "code": "request_date_outside_stay_window",
                        "response_text": (
                            f"That date is outside your current stay window ({check_in_label} to {check_out_label}). "
                            "Please share a date within your stay, or tell me if you want to modify the stay dates first."
                        ),
                        "suggested_actions": [
                            "Share date within stay",
                            "Modify stay dates",
                            "Show available services",
                        ],
                    }

        complaint_like_intent = intent in {
            IntentType.COMPLAINT,
            IntentType.HUMAN_REQUEST,
            IntentType.FAQ,
            IntentType.MENU_REQUEST,
        }
        if complaint_like_intent and self._looks_like_operational_issue_for_ticketing(msg_lower):
            phase_context = self._get_selected_phase_context(
                context=context,
                pending_data=pending,
                entities=entity_map,
            )
            phase_id = self._normalize_phase_identifier(phase_context.get("selected_phase_id"))
            if phase_id in {"pre_booking", "pre_checkin"}:
                phase_name = str(phase_context.get("selected_phase_name") or self._phase_label(phase_id)).strip()
                return {
                    "code": "operational_issue_outside_stay_context",
                    "response_text": (
                        f"This sounds like an in-stay room support issue, but your current phase is {phase_name}. "
                        "If this is for an upcoming stay, please share booking details and room number (if assigned), "
                        "or tell me if you want pre-arrival support."
                    ),
                    "suggested_actions": [
                        "Share booking details",
                        "Need pre-arrival support",
                        "Talk to human",
                    ],
                }

        if self._looks_like_booking_change_request(text):
            target = self._resolve_booking_change_target(text)
            if not target:
                phase_context = self._get_selected_phase_context(
                    context=context,
                    pending_data=pending,
                    entities=entity_map,
                )
                phase_services = self._phase_service_names_for_context(
                    context=context,
                    pending_data=pending,
                    entities=entity_map,
                    limit=5,
                )
                response_text = (
                    "I can help with cancellation or modification. "
                    "Please tell me what you want to change: room booking, airport transfer, table reservation, or spa appointment."
                )
                if phase_services:
                    response_text = (
                        f"{response_text} In {phase_context.get('selected_phase_name') or 'this phase'}, "
                        f"available services include {', '.join(phase_services)}."
                    )
                return {
                    "code": "booking_change_target_ambiguous",
                    "response_text": response_text,
                    "suggested_actions": [
                        "Room booking",
                        "Airport transfer",
                        "Table reservation",
                        "Spa appointment",
                    ],
                }

        return None

    @classmethod
    def _is_action_request_text(cls, message: str) -> bool:
        text = cls._normalize_message_for_routing(message)
        if not text:
            return False
        action_markers = (
            "book",
            "reserve",
            "order",
            "arrange",
            "need",
            "request",
            "cancel",
            "modify",
            "status",
            "wanna",
            "want",
            "confirm",
            "schedule",
        )
        return any(marker in text for marker in action_markers)














    def _find_intent_compatible_service(
        self,
        *,
        intent: IntentType,
        services: list[dict[str, Any]],
        exclude_phase_id: str = "",
    ) -> dict[str, Any] | None:
        marker_map = _PHASE_INTENT_SERVICE_MARKERS
        markers = marker_map.get(intent, ())
        if not markers:
            return None
        excluded = self._normalize_phase_identifier(exclude_phase_id)
        best: dict[str, Any] | None = None
        best_score = 0

        for service in services:
            if not isinstance(service, dict):
                continue
            if not bool(service.get("is_active", True)):
                continue
            phase_id = self._normalize_phase_identifier(service.get("phase_id"))
            if excluded and phase_id == excluded:
                continue
            text = self._service_phase_gate_text_blob(service)
            if not text:
                continue
            score = 0
            for marker in markers:
                if re.search(rf"\b{re.escape(marker)}\b", text):
                    score += 1
            if score > best_score:
                best_score = score
                best = service
        if best is None or best_score <= 0:
            return None
        return best










    @staticmethod
    def _build_contextual_sanity_handler_result(sanity_issue: dict[str, Any]) -> HandlerResult:
        response_text = str(sanity_issue.get("response_text") or "").strip()
        if not response_text:
            response_text = "I need one clarification before I can proceed."
        response_text = ChatService._dedupe_response_sentences(response_text) or response_text
        raw_actions = sanity_issue.get("suggested_actions")
        suggestions: list[str] = []
        if isinstance(raw_actions, list):
            suggestions = [str(item).strip() for item in raw_actions if str(item).strip()]
        if not suggestions:
            suggestions = ["Show available services", "Ask another question"]
        return HandlerResult(
            response_text=response_text,
            next_state=ConversationState.IDLE,
            pending_action=None,
            pending_data={},
            suggested_actions=suggestions[:4],
            metadata={
                "response_source": "contextual_sanity_gate",
                "contextual_sanity_gate": True,
                "contextual_sanity_code": str(sanity_issue.get("code") or ""),
            },
        )

    @staticmethod
    def _service_description_keywords(service: dict) -> list[str]:
        description = str(service.get("description") or service.get("cuisine") or "").strip().lower()
        if not description:
            return []
        tokens = re.findall(r"[a-z0-9]+", description)
        keywords: list[str] = []
        for token in tokens:
            if len(token) < 4:
                continue
            if token in _SERVICE_DESCRIPTION_STOPWORDS:
                continue
            if token not in keywords:
                keywords.append(token)
        return keywords[:8]

    @staticmethod
    def _token_matches_message(token: str, msg_tokens: list[str]) -> bool:
        if not token:
            return False
        for msg_token in msg_tokens:
            if msg_token == token:
                return True
            if (
                len(token) >= 6
                and len(msg_token) >= 6
                and (msg_token.startswith(token[:4]) or token.startswith(msg_token[:4]))
            ):
                return True
            if len(token) >= 5 and len(msg_token) >= 4 and SequenceMatcher(a=token, b=msg_token).ratio() >= 0.86:
                return True
        return False

    def _service_alias_match_score(self, msg_lower: str, msg_tokens: list[str], alias: str) -> float:
        alias_clean = str(alias or "").strip().lower()
        if not alias_clean:
            return 0.0

        alias_tokens = [
            token
            for token in re.findall(r"[a-z0-9]+", alias_clean)
            if len(token) >= 3 and token not in _SERVICE_DESCRIPTION_STOPWORDS
        ]
        if not alias_tokens:
            return 0.0

        if len(alias_tokens) == 1:
            if re.search(rf"\b{re.escape(alias_tokens[0])}\b", msg_lower):
                return 0.95
        else:
            alias_phrase = " ".join(alias_tokens)
            if alias_phrase and re.search(rf"\b{re.escape(alias_phrase)}\b", msg_lower):
                return 0.98

        hits = sum(1 for token in alias_tokens if self._token_matches_message(token, msg_tokens))
        if hits == len(alias_tokens):
            return 0.9
        if len(alias_tokens) >= 3 and hits >= len(alias_tokens) - 1:
            return 0.8
        if len(alias_tokens) == 1 and hits == 1:
            return 0.84
        return 0.0

    @staticmethod
    def _service_action_should_route_to_transaction(service: dict, msg_lower: str) -> bool:
        """
        Keep only clearly-known transactional flows on dedicated handlers.
        """
        service_type = str(service.get("type") or "").strip().lower()
        service_id = str(service.get("id") or "").strip().lower()
        service_name = str(service.get("name") or "").strip().lower()
        combined = f"{service_type} {service_id} {service_name}"

        action_like = any(token in msg_lower for token in ("book", "reserve", "schedule", "order", "confirm", "arrange"))
        if not action_like:
            return False

        transport_markers = ("transport", "airport_transfer", "cab", "taxi", "shuttle")
        appointment_markers = ("appointment", "spa", "massage", "doctor")
        room_service_markers = ("room_service", "housekeeping", "laundry", "cleaning")
        restaurant_markers = ("restaurant", "outlet", "dining", "ird", "food")
        booking_markers = ("book", "reserve", "table", "guests", "party")

        if any(marker in combined for marker in transport_markers):
            return True
        if any(marker in combined for marker in appointment_markers):
            return True
        if any(marker in combined for marker in room_service_markers):
            return True
        if any(marker in combined for marker in restaurant_markers) and any(marker in msg_lower for marker in booking_markers):
            return True
        return False


    @staticmethod
    def _is_return_to_bot_request(message: str) -> bool:
        msg_lower = str(message or "").strip().lower()
        if not msg_lower:
            return False
        if msg_lower in _RETURN_TO_BOT_MARKERS:
            return True
        return msg_lower.startswith("return to bot")

    @staticmethod
    def _is_personal_reservation_query(msg_lower: str) -> bool:
        if "my" in msg_lower and ("reservation" in msg_lower or "booking" in msg_lower):
            return True
        return any(
            marker in msg_lower
            for marker in (
                "what time am i leaving",
                "when am i leaving",
                "my checkout",
                "my check out",
                "my departure",
            )
        )

    @staticmethod
    def _intent_ids_for_enablement(intent: IntentType) -> list[str]:
        """
        Intent aliases used across industry templates.
        Allows core intents to map onto industry-specific naming.
        """
        mapping = {
            IntentType.GREETING: ["greeting"],
            IntentType.MENU_REQUEST: ["menu_request", "faq", "product_search", "department_info", "doctor_info"],
            IntentType.ORDER_FOOD: ["order_food", "order_placement", "add_to_cart", "checkout"],
            IntentType.ORDER_STATUS: ["order_status", "refund_status", "report_request"],
            IntentType.TABLE_BOOKING: ["table_booking", "book_appointment", "appointment_booking"],
            IntentType.ROOM_SERVICE: ["room_service", "housekeeping", "prescription"],
            IntentType.COMPLAINT: ["complaint", "feedback", "support"],
            IntentType.FAQ: ["faq"],
            IntentType.HUMAN_REQUEST: ["human_request"],
        }
        return mapping.get(intent, [intent.value])

    @staticmethod
    def _detect_confirmation_intent(msg_lower: str) -> Optional[IntentType]:
        strict_mode = bool(getattr(settings, "chat_require_strict_confirmation_phrase", True))
        confirmation_phrase = str(getattr(settings, "chat_confirmation_phrase", "yes confirm")).strip().lower()
        compact_msg = re.sub(r"\s+", " ", str(msg_lower or "").strip().lower())
        yes_tokens = {"yes", "yeah", "yep", "sure", "ok", "okay", "confirm", "proceed", "y", "ys", "ye", "yess"}
        no_tokens = {"no", "nope", "cancel", "n", "stop", "dont", "don't", "nah"}
        # Always treat the configured phrase as a valid confirmation,
        # even when strict mode is disabled.
        if compact_msg == confirmation_phrase:
            return IntentType.CONFIRMATION_YES
        if strict_mode:
            if compact_msg in no_tokens:
                return IntentType.CONFIRMATION_NO
            return None

        if compact_msg in yes_tokens:
            return IntentType.CONFIRMATION_YES
        if compact_msg in no_tokens:
            return IntentType.CONFIRMATION_NO

        compact = re.sub(r"[^a-z]", "", compact_msg)
        if not compact:
            return None

        for token in yes_tokens:
            if SequenceMatcher(a=compact, b=token).ratio() >= 0.84:
                return IntentType.CONFIRMATION_YES
        for token in no_tokens:
            if SequenceMatcher(a=compact, b=token).ratio() >= 0.84:
                return IntentType.CONFIRMATION_NO
        return None


    async def _preprocess_user_message_with_llm(
        self,
        message: str,
        *,
        selected_phase_id: str = "",
        selected_phase_name: str = "",
        context: ConversationContext | None = None,
    ) -> str:
        """
        LLM-first user-message preprocessing layer.
        Correct typos/spelling and minor grammar while preserving intent.
        """
        original = re.sub(r"\s+", " ", str(message or "").strip())
        if not original:
            return ""
        if not bool(getattr(settings, "chat_llm_preprocess_enabled", True)):
            return original
        if not str(settings.openai_api_key or "").strip():
            return original
        # Skip LLM preprocess for messages that already look clean.
        _clean_threshold = max(
            1,
            int(getattr(settings, "chat_llm_preprocess_min_chars", 4) or 4),
        )
        if len(original) >= _clean_threshold:
            _non_ascii = sum(1 for c in original if ord(c) > 127)
            _has_obvious_issues = (
                (_non_ascii / max(1, len(original))) > 0.25
                or bool(re.search(r"[^\w\s'\"\-\.\,\!\?\(\)\:\/\@\#\&\+\=]", original))
                or (len(original.split()) < 2 and len(original) < 8)
            )
            if not _has_obvious_issues:
                return original

        phase_label = str(selected_phase_name or "").strip() or (
            str(selected_phase_id or "").replace("_", " ").title()
        )
        phase_id = str(selected_phase_id or "").strip() or "unknown"

        # Include the last assistant message so the LLM can use conversation
        # context to resolve ambiguous typos (e.g. "terminl1" → "terminal 1"
        # when the bot just asked "which terminal?").
        last_assistant_msg = ""
        if context and hasattr(context, "messages") and context.messages:
            for msg in reversed(context.messages):
                if msg.role == MessageRole.ASSISTANT:
                    last_assistant_msg = str(msg.content or "").strip()[:300]
                    break

        context_line = ""
        if last_assistant_msg:
            context_line = f"Last bot message: \"{last_assistant_msg}\"\n"

        try:
            prompt = await prompt_registry.get(
                "chat.preprocess",
                {
                    "phase_label": phase_label,
                    "phase_id": phase_id,
                    "context_line": context_line,
                },
            )
        except PromptMissingError:
            logger.exception("chat_preprocess_prompt_missing")
            return original
        preprocess_model = str(getattr(settings, "chat_llm_preprocess_model", "") or "").strip() or None
        preprocess_temp = float(getattr(settings, "chat_llm_preprocess_temperature", 0.0))
        preprocess_tokens = max(24, int(getattr(settings, "chat_llm_preprocess_max_tokens", 80)))

        try:
            rewritten = await llm_client.chat(
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": original},
                ],
                model=preprocess_model,
                temperature=preprocess_temp,
                max_tokens=preprocess_tokens,
            )
        except Exception:
            return original

        candidate = re.sub(r"\s+", " ", str(rewritten or "").strip().strip('"').strip("'"))
        if not candidate:
            return original
        if self._is_reasonable_message_preprocess_rewrite(original, candidate):
            return candidate
        return original

    @staticmethod
    def _normalize_message_for_routing(message: str) -> str:
        return re.sub(r"\s+", " ", str(message or "").strip().lower())

    @staticmethod
    def _normalize_alias_text(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


    @staticmethod
    def _has_hunger_signal(msg_lower: str) -> bool:
        """Detect hunger expressions with typo tolerance (e.g., 'hungfry')."""
        if not msg_lower:
            return False

        tokens = re.findall(r"[a-z]+", msg_lower.lower())
        if not tokens:
            return False

        explicit_markers = {"hungry", "starving", "famished"}
        for token in tokens:
            if token in explicit_markers:
                return True
            if len(token) >= 5 and SequenceMatcher(a=token, b="hungry").ratio() >= 0.72:
                return True
        return False

    @staticmethod
    def _looks_like_food_catalog_info_query(msg_lower: str) -> bool:
        """
        Detect natural-language food/menu information asks so they route to RAG FAQ
        instead of transactional handlers.
        """
        if not msg_lower:
            return False

        catalog_markers = (
            "menu",
            "menus",
            "catalog",
            "food",
            "dining",
            "ird",
            "in room dining",
            "in-room dining",
            "hungry",
            "eat",
        )
        info_markers = (
            "what",
            "which",
            "show",
            "list",
            "tell",
            "do you",
            "can you",
            "is there",
            "are there",
            "serve",
            "available",
            "options",
        )
        direct_order_markers = (
            "i want ",
            "order ",
            "book ",
            "confirm ",
            "place order",
            "send ",
        )

        has_hunger_signal = ChatService._has_hunger_signal(msg_lower)
        has_catalog_signal = any(marker in msg_lower for marker in catalog_markers) or has_hunger_signal
        has_info_signal = any(marker in msg_lower for marker in info_markers)
        if not has_catalog_signal:
            return False

        if has_info_signal:
            return True

        if has_hunger_signal and not any(marker in msg_lower for marker in direct_order_markers):
            return True

        return False



    @staticmethod
    def _looks_like_policy_or_timing_query(msg_lower: str) -> bool:
        """
        Detect policy/timing information asks and route to FAQ/RAG deterministically.
        """
        if not msg_lower:
            return False

        normalized = re.sub(r"\s+", " ", msg_lower.strip().lower())
        if not normalized:
            return False

        transactional_prefixes = (
            "book ",
            "reserve ",
            "order ",
            "arrange ",
            "send ",
            "cancel ",
            "confirm ",
            "i want ",
            "i need ",
            "please book",
            "please reserve",
            "please order",
        )
        if any(normalized.startswith(prefix) for prefix in transactional_prefixes):
            return False

        transactional_markers = (
            "talk to human",
            "connect me",
            "human agent",
            "manager",
            "my order",
            "my booking",
            "my reservation",
            "order status",
            "track my",
        )
        if any(marker in normalized for marker in transactional_markers):
            return False

        is_question_like = (
            "?" in normalized
            or normalized.startswith("what ")
            or normalized.startswith("when ")
            or normalized.startswith("where ")
            or normalized.startswith("which ")
            or normalized.startswith("who ")
            or normalized.startswith("how ")
            or normalized.startswith("is ")
            or normalized.startswith("are ")
            or normalized.startswith("do ")
            or normalized.startswith("can ")
            or normalized.startswith("what's ")
        )
        if not is_question_like:
            return False

        policy_markers = (
            "check in",
            "check-in",
            "checkin",
            "check out",
            "check-out",
            "checkout",
            "timing",
            "timings",
            "hours",
            "policy",
            "arrival",
            "departure",
            "late checkout",
            "early checkin",
        )
        return any(marker in normalized for marker in policy_markers)




    async def _classify_intent(
        self,
        message: str,
        conversation_history: list[dict],
        context: dict,
    ) -> IntentResult:
        """Classify user intent using LLM."""
        try:
            classification_message = re.sub(r"\s+", " ", str(message or "").strip())
            msg_lower = classification_message.lower()

            # Check for simple confirmation first (state-aware)
            if context.get("state") == "awaiting_confirmation":
                if context.get("pending_action") == "show_menu" and msg_lower in {"s", "show", "menu", "show menu", "full menu"}:
                    return IntentResult(intent=IntentType.CONFIRMATION_YES, confidence=0.9)
                confirmation_intent = self._detect_confirmation_intent(msg_lower)
                if confirmation_intent == IntentType.CONFIRMATION_YES:
                    return IntentResult(intent=IntentType.CONFIRMATION_YES, confidence=0.95)
                if confirmation_intent == IntentType.CONFIRMATION_NO:
                    return IntentResult(intent=IntentType.CONFIRMATION_NO, confidence=0.95)

            restaurant_alias = self._match_restaurant_alias_menu_phrase(msg_lower, context)
            if restaurant_alias:
                return IntentResult(
                    intent=IntentType.MENU_REQUEST,
                    confidence=0.9,
                    entities={"restaurant": restaurant_alias},
                )

            if self._looks_like_spa_booking_action(msg_lower):
                return IntentResult(
                    intent=IntentType.TABLE_BOOKING,
                    confidence=0.92,
                    entities={
                        "service_name": "Spa",
                        "booking_sub_category": "spa_booking",
                        "booking_type": "spa_booking",
                    },
                )

            if self._looks_like_room_stay_booking_request(classification_message):
                return IntentResult(
                    intent=IntentType.TABLE_BOOKING,
                    confidence=0.92,
                    entities={
                        "service_name": "Room Booking",
                        "booking_sub_category": "room_booking",
                        "booking_type": "room_booking",
                    },
                )

            if self._looks_like_food_delivery_order_request(msg_lower):
                return IntentResult(
                    intent=IntentType.ORDER_FOOD,
                    confidence=0.92,
                    entities={},
                )

            if self._looks_like_food_catalog_info_query(msg_lower):
                return IntentResult(
                    intent=IntentType.FAQ,
                    confidence=0.9,
                    entities={"requested_topic": "food_catalog"},
                )

            if self._looks_like_ticketing_request(msg_lower):
                return IntentResult(
                    intent=IntentType.COMPLAINT,
                    confidence=0.9,
                    entities={"ticketing_flow": True},
                )

            if self._looks_like_strong_menu_request(msg_lower):
                # In RAG-first mode, treat catalog/menu asks as FAQ-like retrieval.
                return IntentResult(
                    intent=IntentType.FAQ,
                    confidence=0.9,
                    entities={"requested_topic": "catalog"},
                )

            if self._looks_like_policy_or_timing_query(msg_lower):
                return IntentResult(
                    intent=IntentType.FAQ,
                    confidence=0.9,
                    entities={"requested_topic": "policy_timing"},
                )

            # Use LLM for classification
            result = await llm_client.classify_intent(classification_message, conversation_history, context)

            # Parse result
            raw_intent = result.get("intent", "unclear")
            intent_str = str(raw_intent).lower().replace(" ", "_")
            resolved_intent = config_service.resolve_intent_to_core(intent_str) or intent_str
            confidence = float(result.get("confidence", 0.5))
            entities = result.get("entities", {})
            if not isinstance(entities, dict):
                entities = {}

            if resolved_intent != intent_str:
                entities = dict(entities)
                entities.setdefault("custom_intent", intent_str)
                entities.setdefault("resolved_intent", resolved_intent)

            # Map to IntentType enum
            try:
                intent = IntentType(resolved_intent)
            except ValueError:
                intent = IntentType.UNCLEAR
                confidence = 0.3

            if intent == IntentType.MENU_REQUEST and not config_service.is_menu_runtime_enabled():
                intent = IntentType.FAQ
                confidence = max(confidence, 0.82)
                entities = dict(entities)
                entities.setdefault("requested_topic", "catalog")

            return IntentResult(
                intent=intent,
                confidence=confidence,
                entities=entities,
                requires_confirmation=intent in [IntentType.ORDER_FOOD, IntentType.TABLE_BOOKING],
            )

        except Exception as e:
            print(f"Intent classification error: {e}")
            # Fallback to unclear
            return IntentResult(intent=IntentType.UNCLEAR, confidence=0.3)


    def _handle_confirmation_yes(self, context: dict) -> str:
        """Handle yes confirmation based on pending action (LLM fallback path)."""
        action = context.get("pending_action")
        pending_data = context.get("pending_data", {})
        if not isinstance(pending_data, dict):
            pending_data = {}

        # ── THIS PATH MAY NOT CONFIRM ANYTHING. IT PLACES NOTHING. ───────────
        # It is the fallback taken when the normal flow has already failed, and
        # it used to answer a guest's "yes" with:
        #
        #   "Your order has been confirmed!
        #    Order ID: ORD-4821
        #    Estimated delivery: 25-30 minutes"
        #
        # The order id was `hash(str(items)) % 10000` - invented on the spot. The
        # delivery estimate was invented too. No ticket was created, nothing was
        # written to the order book, and the kitchen never heard a word. Same for
        # "Your table has been reserved!" - no reservation existed. That is the
        # source of the "it says placed but the book says ongoing" reports: a
        # confirmation with nothing whatsoever behind it.
        #
        # A fallback cannot place an order, so it must not say one was placed. It
        # asks the guest to say it again, which the real path can then handle.
        if action == "confirm_order":
            items = pending_data.get("items", [])
            if not items:
                return ("I don't have the order items yet. Please tell me what "
                        "you'd like to order first.")
            return ("Sorry - I couldn't get that order through just now, so "
                    "nothing has been sent to the kitchen yet. Could you tell me "
                    "the items once more and I'll put it straight through?")

        if action == "confirm_booking":
            return ("Sorry - I couldn't complete that booking just now, so it is "
                    "not reserved yet. Shall I try again, or would you like me to "
                    "pass it to our team?")

        if action == "show_menu":
            return "Knowledge browsing is handled through the retrieval layer now. Ask for a specific service/topic and I will help."

        return "Great! How can I help you further?"

    def _handle_unexpected_intent(
        self,
        context: ConversationContext,
        intent_result: IntentResult,
        error_msg: Optional[str],
    ) -> str:
        """Handle when intent doesn't match expected state."""
        if context.state == ConversationState.AWAITING_CONFIRMATION:
            return f"I'm waiting for your confirmation on the previous request. Please say 'Yes' to confirm or 'No' to cancel."
        return "I didn't quite understand. Could you please clarify what you'd like to do?"

    def _determine_next_state(
        self,
        context: ConversationContext,
        intent_result: IntentResult,
        response: str,
    ) -> ConversationState:
        """Determine the next conversation state (LLM fallback path)."""
        intent = intent_result.intent

        # Human escalation
        if intent == IntentType.HUMAN_REQUEST:
            return ConversationState.ESCALATED

        # Confirmation handling
        if intent == IntentType.CONFIRMATION_YES:
            pending_action = getattr(context, "pending_action", None)
            pending_data = getattr(context, "pending_data", {})
            if not isinstance(pending_data, dict):
                pending_data = {}
            if pending_action == "confirm_order" and not pending_data.get("items"):
                return ConversationState.AWAITING_INFO
            return ConversationState.COMPLETED

        if intent == IntentType.CONFIRMATION_NO:
            return ConversationState.IDLE

        # Order/booking flow - check if asking for confirmation
        if intent in [IntentType.ORDER_FOOD, IntentType.TABLE_BOOKING]:
            if "confirm" in response.lower() or "yes/no" in response.lower():
                internal_pending_entries = conversation_memory_service.internal_pending_entries(context.pending_data)
                context.pending_action = "confirm_order" if intent == IntentType.ORDER_FOOD else "confirm_booking"
                context.pending_data = conversation_memory_service.merge_with_internal(
                    {"items": intent_result.entities.get("items", [])},
                    internal_pending_entries,
                )
                return ConversationState.AWAITING_CONFIRMATION

        # Stay in current state if awaiting something
        if context.state in [ConversationState.AWAITING_CONFIRMATION, ConversationState.AWAITING_INFO]:
            return context.state

        return ConversationState.IDLE

    def _get_suggested_actions(
        self,
        state: ConversationState,
        intent: IntentType,
        capabilities_summary: dict,
    ) -> list[str]:
        """Get suggested quick actions for the user."""
        if state == ConversationState.AWAITING_CONFIRMATION:
            confirmation_phrase = str(
                getattr(settings, "chat_confirmation_phrase", "yes confirm")
            ).strip() or "yes confirm"
            return [confirmation_phrase, "cancel"]

        if state == ConversationState.ESCALATED:
            return ["Return to bot"]

        # All other states: return empty so LLM layers generate contextual suggestions
        # grounded in the actual bot message, history, phase and active service.
        return []

    def _build_contextual_suggested_actions(
        self,
        state: ConversationState,
        intent: IntentType,
        pending_action: str | None,
        capabilities_summary: dict,
        pending_data: dict[str, Any] | None = None,
    ) -> list[str]:
        """
        Returns deterministic chips only for states that require functional buttons
        (confirmation, escalation). All other cases return [] so the LLM layers
        generate contextual suggestions grounded in the actual bot message, history,
        phase and active service.
        """
        confirmation_phrase = str(
            getattr(settings, "chat_confirmation_phrase", "yes confirm")
        ).strip() or "yes confirm"

        if state == ConversationState.AWAITING_CONFIRMATION:
            return [confirmation_phrase, "cancel"]
        if state == ConversationState.ESCALATED:
            return ["Return to bot"]

        if pending_action in {
            "confirm_order",
            "confirm_booking",
            "confirm_service_request",
            "confirm_ticket_creation",
            "confirm_ticket_escalation",
        }:
            return [confirmation_phrase, "cancel"]

        # Everything else: return empty — LLM layers handle it
        return []

    def _ensure_internal_task_state(self, context: ConversationContext) -> dict[str, Any]:
        """Ensure diversion task-state payload exists in internal pending data."""
        if not isinstance(context.pending_data, dict):
            context.pending_data = {}
        state = context.pending_data.get(_INTERNAL_TASK_STATE_KEY)
        if not isinstance(state, dict):
            state = {}

        parked_tasks = state.get("parked_tasks")
        if not isinstance(parked_tasks, list):
            parked_tasks = []
        normalized_parked: list[dict[str, Any]] = []
        for item in parked_tasks:
            if isinstance(item, dict):
                normalized_parked.append(dict(item))

        active_task = state.get("active_task")
        if not isinstance(active_task, dict):
            active_task = None

        normalized = {
            "version": int(state.get("version") or 1),
            "active_task": dict(active_task) if isinstance(active_task, dict) else None,
            "parked_tasks": normalized_parked[:_MAX_PARKED_TASKS],
            "updated_at": str(state.get("updated_at") or datetime.now(UTC).isoformat()),
        }
        context.pending_data[_INTERNAL_TASK_STATE_KEY] = normalized
        return normalized

    @staticmethod
    def _public_pending_data(context: ConversationContext) -> dict[str, Any]:
        pending = context.pending_data if isinstance(context.pending_data, dict) else {}
        return {
            key: value
            for key, value in pending.items()
            if isinstance(key, str) and not key.startswith("_")
        }

    def _active_task_summary_text(
        self,
        *,
        pending_action: str | None,
        pending_data: dict[str, Any],
    ) -> str:
        service_name = str(
            pending_data.get("service_name")
            or pending_data.get("restaurant_name")
            or pending_data.get("service")
            or ""
        ).strip()
        issue_text = str(
            pending_data.get("issue")
            or pending_data.get("ticket_issue")
            or pending_data.get("request")
            or ""
        ).strip()
        item_name = str(
            pending_data.get("order_item")
            or pending_data.get("item_name")
            or ""
        ).strip()
        if service_name:
            return f"your {service_name} request"
        if issue_text:
            return issue_text[:120]
        if item_name:
            return f"your {item_name} request"
        action_label = str(pending_action or "").strip().replace("_", " ")
        if action_label:
            return f"your {action_label} flow"
        return "your previous request"

    def _sync_active_task_snapshot(
        self,
        *,
        context: ConversationContext,
        selected_phase_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Keep internal active_task aligned with current pending flow."""
        task_state = self._ensure_internal_task_state(context)
        if not str(context.pending_action or "").strip():
            task_state["active_task"] = None
            task_state["updated_at"] = datetime.now(UTC).isoformat()
            context.pending_data[_INTERNAL_TASK_STATE_KEY] = task_state
            return task_state

        pending_public = self._public_pending_data(context)
        phase_ctx = selected_phase_context if isinstance(selected_phase_context, dict) else {}
        task_state["active_task"] = {
            "pending_action": str(context.pending_action or "").strip(),
            "pending_data": dict(pending_public),
            "state": context.state.value,
            "phase_id": str(phase_ctx.get("selected_phase_id") or "").strip(),
            "phase_name": str(phase_ctx.get("selected_phase_name") or "").strip(),
            "summary": self._active_task_summary_text(
                pending_action=context.pending_action,
                pending_data=pending_public,
            ),
            "updated_at": datetime.now(UTC).isoformat(),
        }
        task_state["updated_at"] = datetime.now(UTC).isoformat()
        context.pending_data[_INTERNAL_TASK_STATE_KEY] = task_state
        return task_state

    def _park_active_task(
        self,
        *,
        context: ConversationContext,
        selected_phase_context: dict[str, Any] | None = None,
        reason: str = "topic_diversion",
        user_message: str = "",
    ) -> dict[str, Any] | None:
        """Suspend current pending flow into parked task queue."""
        task_state = self._sync_active_task_snapshot(
            context=context,
            selected_phase_context=selected_phase_context or {},
        )
        active_task = task_state.get("active_task")
        if not isinstance(active_task, dict):
            return None

        parked_task = dict(active_task)
        parked_task["reason"] = str(reason or "topic_diversion")
        parked_task["diversion_message"] = str(user_message or "").strip()[:280]
        parked_task["parked_at"] = datetime.now(UTC).isoformat()

        parked_tasks = [
            dict(item)
            for item in (task_state.get("parked_tasks") or [])
            if isinstance(item, dict)
        ]
        parked_tasks.insert(0, parked_task)
        deduped: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        for item in parked_tasks:
            key = json.dumps(
                {
                    "pending_action": str(item.get("pending_action") or ""),
                    "pending_data": item.get("pending_data") if isinstance(item.get("pending_data"), dict) else {},
                },
                sort_keys=True,
                ensure_ascii=False,
            )
            if key in seen_keys:
                continue
            seen_keys.add(key)
            deduped.append(item)
            if len(deduped) >= _MAX_PARKED_TASKS:
                break

        task_state["active_task"] = None
        task_state["parked_tasks"] = deduped
        task_state["updated_at"] = datetime.now(UTC).isoformat()

        internal_entries = conversation_memory_service.internal_pending_entries(context.pending_data)
        internal_entries[_INTERNAL_TASK_STATE_KEY] = task_state
        context.pending_action = None
        context.pending_data = conversation_memory_service.merge_with_internal({}, internal_entries)
        context.pending_data.pop("_clarification_attempts", None)
        context.state = ConversationState.IDLE
        return parked_task

    def _peek_parked_task(self, context: ConversationContext) -> dict[str, Any] | None:
        task_state = self._ensure_internal_task_state(context)
        for item in task_state.get("parked_tasks", []):
            if isinstance(item, dict):
                return dict(item)
        return None

    def _restore_parked_task(self, context: ConversationContext) -> dict[str, Any] | None:
        """Resume the most recently parked task."""
        task_state = self._ensure_internal_task_state(context)
        parked_tasks = [
            dict(item)
            for item in (task_state.get("parked_tasks") or [])
            if isinstance(item, dict)
        ]
        if not parked_tasks:
            return None

        task = parked_tasks.pop(0)
        pending_action = str(task.get("pending_action") or "").strip()
        pending_public = task.get("pending_data")
        if not isinstance(pending_public, dict):
            pending_public = {}

        internal_entries = conversation_memory_service.internal_pending_entries(context.pending_data)
        task_state["parked_tasks"] = parked_tasks
        task_state["active_task"] = dict(task)
        task_state["updated_at"] = datetime.now(UTC).isoformat()
        internal_entries[_INTERNAL_TASK_STATE_KEY] = task_state

        context.pending_action = pending_action or None
        context.pending_data = conversation_memory_service.merge_with_internal(
            dict(pending_public),
            internal_entries,
        )

        restored_state_raw = str(task.get("state") or "").strip().lower().replace("-", "_")
        try:
            restored_state = ConversationState(restored_state_raw)
        except Exception:
            restored_state = ConversationState.AWAITING_INFO
        context.state = restored_state
        return task

    def _cancel_parked_task(self, context: ConversationContext) -> dict[str, Any] | None:
        """Discard the most recently parked task."""
        task_state = self._ensure_internal_task_state(context)
        parked_tasks = [
            dict(item)
            for item in (task_state.get("parked_tasks") or [])
            if isinstance(item, dict)
        ]
        if not parked_tasks:
            return None
        task = parked_tasks.pop(0)
        task_state["parked_tasks"] = parked_tasks
        task_state["updated_at"] = datetime.now(UTC).isoformat()
        context.pending_data[_INTERNAL_TASK_STATE_KEY] = task_state
        return task

    @staticmethod
    def _is_resume_task_request(message: str) -> bool:
        compact = re.sub(r"\s+", " ", str(message or "").strip().lower())
        if not compact:
            return False
        if compact in _RESUME_TASK_MARKERS:
            return True
        return compact.startswith("resume ")

    @staticmethod
    def _is_cancel_task_request(message: str) -> bool:
        compact = re.sub(r"\s+", " ", str(message or "").strip().lower())
        if not compact:
            return False
        if compact in _CANCEL_TASK_MARKERS:
            return True
        return compact in {"cancel pending", "cancel pending task", "cancel"}

    def _should_auto_resume_parked_task(
        self,
        *,
        message: str,
        parked_task: dict[str, Any],
        capabilities_summary: dict[str, Any],
    ) -> bool:
        """
        Resume a parked task when user naturally returns with task-specific details.
        """
        msg_lower = re.sub(r"\s+", " ", str(message or "").strip().lower())
        if not msg_lower:
            return False
        if self._is_resume_task_request(msg_lower) or self._is_cancel_task_request(msg_lower):
            return False
        if self._looks_like_information_query(msg_lower):
            return False
        if self._looks_like_problem_report(msg_lower):
            return False

        pending_action = str(parked_task.get("pending_action") or "").strip().lower()
        if not pending_action:
            return False
        pending_data = parked_task.get("pending_data")
        if not isinstance(pending_data, dict):
            pending_data = {}

        if self._is_room_booking_pending_action(pending_action):
            if self._looks_like_room_booking_detail_followup(msg_lower):
                return True
            if self._looks_like_room_type_preference_reply(msg_lower):
                return True
            if self._looks_like_room_stay_booking_request(msg_lower):
                return True
            return bool(
                "?" not in msg_lower
                and re.search(r"\b(room|suite|stay|check in|check-in|check out|check-out|guest|guests)\b", msg_lower)
            )

        if pending_action in {"select_service", "select_restaurant"}:
            if self._looks_like_service_selection(msg_lower, capabilities_summary):
                return True
            known_service = str(
                pending_data.get("service_name")
                or pending_data.get("restaurant_name")
                or pending_data.get("service")
                or ""
            ).strip().lower()
            if known_service and (known_service in msg_lower or msg_lower in known_service):
                return True
            return False

        if pending_action == "collect_booking_party_size":
            return self._looks_like_party_size_reply(msg_lower)
        if pending_action == "collect_booking_time":
            return self._looks_like_time_reply(msg_lower)
        if pending_action in {"collect_ticket_room_number", "awaiting_room_number"}:
            return self._looks_like_room_number_reply(msg_lower)
        if pending_action in {"collect_ticket_issue_details", "collect_ticket_update_note"}:
            return self._looks_like_ticket_update_note(msg_lower)
        if pending_action == "collect_ticket_identity_details":
            return self._looks_like_ticket_identity_details_reply(msg_lower)
        if pending_action == "awaiting_request_detail":
            return self._looks_like_room_service_detail(msg_lower)
        if pending_action == "collect_transport_details":
            return self._looks_like_transport_detail(msg_lower)
        if pending_action == "collect_service_details":
            return self._looks_like_service_detail_reply(msg_lower)

        if pending_action.startswith("confirm_"):
            return msg_lower in {"yes", "yes confirm", "confirm", "no", "cancel"}

        return False

    def _append_resume_checkpoint(
        self,
        *,
        response_text: str,
        context: ConversationContext,
        pending_interrupted: bool,
    ) -> tuple[str, list[str]]:
        """Add resume/cancel checkpoint after a diversion answer."""
        if not pending_interrupted:
            return response_text, []
        parked_task = self._peek_parked_task(context)
        if parked_task is None:
            return response_text, []
        summary = str(
            parked_task.get("summary")
            or self._active_task_summary_text(
                pending_action=parked_task.get("pending_action"),
                pending_data=parked_task.get("pending_data")
                if isinstance(parked_task.get("pending_data"), dict)
                else {},
            )
        ).strip()
        checkpoint_line = (
            f"Shall we continue with {summary}? "
            "Reply 'resume' to continue or 'cancel pending' to drop it."
        )
        current_text = str(response_text or "").strip()
        if checkpoint_line.lower() in current_text.lower():
            return current_text, ["resume", "cancel pending"]
        if current_text:
            current_text = f"{current_text}\n\n{checkpoint_line}"
        else:
            current_text = checkpoint_line
        return current_text, ["resume", "cancel pending"]

    def _infer_context_missing_slots(
        self,
        *,
        context: ConversationContext,
        capabilities_summary: dict[str, Any],
    ) -> list[str]:
        pending_public = self._public_pending_data(context)
        missing: list[str] = []
        if context.state == ConversationState.AWAITING_INFO and context.pending_action:
            missing.append(str(context.pending_action))

        target_service_id = self._normalize_service_identifier(
            pending_public.get("service_id")
            or pending_public.get("target_service_id")
            or pending_public.get("resolved_service_id")
        )
        if not target_service_id:
            return missing

        service_catalog = capabilities_summary.get("service_catalog", [])
        if not isinstance(service_catalog, list):
            return missing
        target_service = next(
            (
                service
                for service in service_catalog
                if isinstance(service, dict)
                and self._normalize_service_identifier(service.get("id")) == target_service_id
            ),
            None,
        )
        if not isinstance(target_service, dict):
            return missing
        prompt_pack = target_service.get("service_prompt_pack")
        if not isinstance(prompt_pack, dict):
            return missing
        required_slots = prompt_pack.get("required_slots")
        if not isinstance(required_slots, list):
            return missing
        for slot in required_slots:
            if not isinstance(slot, dict):
                continue
            if not bool(slot.get("required", True)):
                continue
            slot_id = str(slot.get("id") or "").strip()
            if not slot_id:
                continue
            if str(pending_public.get(slot_id) or "").strip():
                continue
            missing.append(slot_id)
        deduped: list[str] = []
        seen: set[str] = set()
        for item in missing:
            key = str(item or "").strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(str(item))
        return deduped[:8]

    def _build_llm_context_pack(
        self,
        *,
        context: ConversationContext,
        capabilities_summary: dict[str, Any],
        selected_phase_context: dict[str, Any],
        memory_snapshot: dict[str, Any],
        user_message: str,
    ) -> dict[str, Any]:
        phase_id = self._normalize_phase_identifier(selected_phase_context.get("selected_phase_id"))
        phase_name = str(selected_phase_context.get("selected_phase_name") or "").strip()

        service_catalog = capabilities_summary.get("service_catalog", [])
        if not isinstance(service_catalog, list):
            service_catalog = []
        phase_services: list[dict[str, Any]] = []
        ticketing_enabled_by_service: dict[str, bool] = {}
        for service in service_catalog:
            if not isinstance(service, dict):
                continue
            service_id = self._normalize_service_identifier(service.get("id"))
            if not service_id:
                continue
            ticketing_enabled = bool(service.get("ticketing_enabled", True))
            ticketing_enabled_by_service[service_id] = ticketing_enabled
            service_phase_id = self._normalize_phase_identifier(service.get("phase_id"))
            if phase_id and service_phase_id and service_phase_id != phase_id:
                continue
            phase_services.append(
                {
                    "id": service_id,
                    "name": str(service.get("name") or service_id).strip(),
                    "type": str(service.get("type") or "service").strip(),
                    "ticketing_enabled": ticketing_enabled,
                }
            )

        stay_window = self._resolve_known_stay_window(
            context=context,
            pending_data=context.pending_data if isinstance(context.pending_data, dict) else {},
            entities={},
        )
        stay_window_payload: dict[str, Any] = {"available": False}
        if stay_window is not None:
            stay_window_payload = {
                "available": True,
                "check_in": self._format_calendar_date_label(stay_window[0]),
                "check_out": self._format_calendar_date_label(stay_window[1]),
            }

        memory_facts = memory_snapshot.get("facts", {}) if isinstance(memory_snapshot, dict) else {}
        if not isinstance(memory_facts, dict):
            memory_facts = {}
        recent_goal = str(
            memory_facts.get("last_user_topic")
            or memory_facts.get("latest_user_request")
            or user_message
            or ""
        ).strip()
        flow_type = "information"
        if context.pending_action or context.state in {
            ConversationState.AWAITING_CONFIRMATION,
            ConversationState.AWAITING_INFO,
            ConversationState.AWAITING_SELECTION,
            ConversationState.PROCESSING_ORDER,
        }:
            flow_type = "transactional"

        return {
            "current_phase": {"id": phase_id, "name": phase_name},
            "active_flow": {
                "state": context.state.value,
                "type": flow_type,
            },
            "pending_action": str(context.pending_action or "").strip(),
            "missing_slots": self._infer_context_missing_slots(
                context=context,
                capabilities_summary=capabilities_summary,
            ),
            "recent_user_goal": recent_goal[:180],
            "phase_services": phase_services[:30],
            "ticketing_enabled_by_service": ticketing_enabled_by_service,
            "stay_window": stay_window_payload,
            "current_time": datetime.now(UTC).isoformat(),
        }


    @staticmethod
    def _normalize_flow_identifier(value: Any) -> str:
        return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")

    @staticmethod
    def _build_generated_guest_id(seed: str) -> str:
        text = str(seed or "").strip()
        if not text:
            return ""
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()
        try:
            numeric_value = int(digest[:15], 16) % 900000000 + 100000000
        except (TypeError, ValueError):
            return ""
        return str(numeric_value)

    @classmethod
    def _should_auto_generate_guest_id(cls, *, phase_hint: Any, flow_hint: Any) -> bool:
        phase_norm = cls._normalize_phase_identifier(phase_hint)
        if phase_norm == "pre_booking":
            return True
        flow_norm = cls._normalize_flow_identifier(flow_hint)
        return flow_norm in {"engage", "booking", "booking_bot", "pre_booking", "prebooking"}

    def _derive_context_guest_id(
        self,
        *,
        context: ConversationContext,
        integration: dict[str, Any],
    ) -> str:
        seed_parts = [
            str(context.session_id or "").strip(),
            str(context.hotel_code or "").strip(),
            str(integration.get("conversation_id") or "").strip(),
            str(integration.get("group_id") or "").strip(),
            str(integration.get("guest_phone") or integration.get("wa_number") or context.guest_phone or "").strip(),
            str(integration.get("room_number") or context.room_number or "").strip(),
        ]
        seed = "|".join(part for part in seed_parts if part)
        return self._build_generated_guest_id(seed or str(context.session_id or "").strip())

    @classmethod
    def _extract_stay_window_updates_from_message(cls, message: str) -> dict[str, str]:
        text = str(message or "").strip()
        if not text:
            return {}
        normalized = re.sub(r"\s+", " ", text).strip()
        lowered = normalized.lower()

        month_token = (
            r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
            r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
        )
        date_token = (
            rf"(?:\d{{4}}-\d{{1,2}}-\d{{1,2}}|"
            rf"{month_token}\s+\d{{1,2}}(?:st|nd|rd|th)?(?:\s*,?\s*\d{{4}})?|"
            rf"\d{{1,2}}(?:st|nd|rd|th)?\s+{month_token}(?:\s*,?\s*\d{{4}})?)"
        )

        def _parse_iso(value: str) -> str:
            parsed = cls._parse_compact_calendar_date(
                value,
                reference_year=date.today().year,
            )
            return parsed.isoformat() if parsed is not None else ""

        updates: dict[str, str] = {}

        check_in_patterns = (
            rf"\b(?:check[\s-]?in|arrival|arrive(?:\s+on)?)\b[^.!?\n\r]*?(?P<date>{date_token})",
        )
        check_out_patterns = (
            rf"\b(?:check[\s-]?out|departure|depart(?:\s+on)?)\b[^.!?\n\r]*?(?P<date>{date_token})",
        )

        for pattern in check_in_patterns:
            match = re.search(pattern, lowered, flags=re.IGNORECASE)
            if not match:
                continue
            parsed = _parse_iso(str(match.group("date") or "").strip())
            if parsed:
                updates["check_in"] = parsed
                break

        for pattern in check_out_patterns:
            match = re.search(pattern, lowered, flags=re.IGNORECASE)
            if not match:
                continue
            parsed = _parse_iso(str(match.group("date") or "").strip())
            if parsed:
                updates["check_out"] = parsed
                break

        stay_markers = (
            "check in",
            "check-in",
            "check out",
            "check-out",
            "stay",
            "arrival",
            "departure",
            "arrive",
            "depart",
        )
        has_stay_marker = any(marker in lowered for marker in stay_markers)
        if has_stay_marker and ("check_in" not in updates or "check_out" not in updates):
            range_patterns = (
                rf"\bfrom\s+(?P<start>{date_token})\s*(?:to|till|until|-)\s*(?P<end>{date_token})",
                rf"\bbetween\s+(?P<start>{date_token})\s+and\s+(?P<end>{date_token})",
                rf"\b(?P<start>{date_token})\s*(?:to|till|until|-)\s*(?P<end>{date_token})\b",
            )
            for pattern in range_patterns:
                match = re.search(pattern, lowered, flags=re.IGNORECASE)
                if not match:
                    continue
                start_value = _parse_iso(str(match.group("start") or "").strip())
                end_value = _parse_iso(str(match.group("end") or "").strip())
                if start_value and end_value:
                    updates.setdefault("check_in", start_value)
                    updates.setdefault("check_out", end_value)
                    break

        return updates

    @staticmethod
    def _extract_guest_phone_candidate(message: str) -> str:
        text = str(message or "").strip()
        if not text:
            return ""
        compact = re.sub(r"[\s\-\(\)]", "", text)
        if re.fullmatch(r"\+?\d{7,15}", compact):
            return compact

        lower = text.lower()
        has_phone_marker = any(
            marker in lower
            for marker in ("phone", "mobile", "contact", "number", "whatsapp", "wa ")
        )
        for match in re.finditer(r"(?<!\d)\+?\d[\d\s\-\(\)]{8,20}\d(?!\d)", text):
            candidate = str(match.group(0) or "").strip()
            digits = re.sub(r"\D", "", candidate)
            if len(digits) < 7 or len(digits) > 15:
                continue
            if has_phone_marker or len(digits) >= 11:
                return f"+{digits}" if candidate.startswith("+") else digits
        return ""

    @staticmethod
    def _extract_guest_name_candidate(message: str) -> str:
        text = str(message or "").strip()
        if not text:
            return ""
        patterns = (
            r"\bmy name is\s+(?P<name>[A-Za-z][A-Za-z'\- ]{1,60})",
            r"\bname is\s+(?P<name>[A-Za-z][A-Za-z'\- ]{1,60})",
            r"\bname\s*:\s*(?P<name>[A-Za-z][A-Za-z'\- ]{1,60})",
            r"\bthis is\s+(?P<name>[A-Za-z][A-Za-z'\- ]{1,60})",
        )
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            candidate = str(match.group("name") or "").strip()
            if not candidate:
                continue
            candidate = re.split(r"[,.!?;\n\r]", candidate, maxsplit=1)[0].strip()
            candidate = re.split(
                r"\b(?:and|phone|mobile|contact|number|whatsapp|wa)\b",
                candidate,
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0].strip()
            tokens = [tok for tok in re.findall(r"[A-Za-z']+", candidate) if len(tok) >= 2]
            if not tokens or len(tokens) > 4:
                continue
            return " ".join(token[:1].upper() + token[1:] for token in tokens)
        return ""

    def _sync_stay_window_from_messages(
        self,
        *,
        context: ConversationContext,
        raw_message: str,
        processed_message: str,
    ) -> None:
        if not isinstance(context.pending_data, dict):
            context.pending_data = {}
        pending = context.pending_data
        integration_raw = pending.get("_integration", {})
        integration: dict[str, Any] = dict(integration_raw) if isinstance(integration_raw, dict) else {}

        merged_updates: dict[str, str] = {}
        for candidate in (processed_message, raw_message):
            extracted = self._extract_stay_window_updates_from_message(candidate)
            if not extracted:
                continue
            for key, value in extracted.items():
                if value and not merged_updates.get(key):
                    merged_updates[key] = value
        if not merged_updates:
            return

        check_in_text = str(merged_updates.get("check_in") or "").strip()
        check_out_text = str(merged_updates.get("check_out") or "").strip()

        if check_in_text:
            pending["check_in"] = check_in_text
            pending["stay_checkin_date"] = check_in_text
            integration["check_in"] = check_in_text
            integration["stay_checkin_date"] = check_in_text
        if check_out_text:
            pending["check_out"] = check_out_text
            pending["stay_checkout_date"] = check_out_text
            integration["check_out"] = check_out_text
            integration["stay_checkout_date"] = check_out_text

        pending["_integration"] = integration
        stay_window = self._resolve_known_stay_window(
            context=context,
            pending_data=pending,
            entities={},
        )
        if stay_window is not None:
            normalized_check_in = stay_window[0].isoformat()
            normalized_check_out = stay_window[1].isoformat()
            pending["check_in"] = normalized_check_in
            pending["check_out"] = normalized_check_out
            pending["stay_checkin_date"] = normalized_check_in
            pending["stay_checkout_date"] = normalized_check_out
            pending["stay_date_range"] = f"{normalized_check_in} to {normalized_check_out}"
            integration["check_in"] = normalized_check_in
            integration["check_out"] = normalized_check_out
            integration["stay_checkin_date"] = normalized_check_in
            integration["stay_checkout_date"] = normalized_check_out

        pending["_integration"] = integration


    @staticmethod
    async def _resolve_guest_stay_from_ghn(
        db_session: Any,
        *,
        entity_id: Any = None,
        guest_id: Any = None,
        guest_phone: Any = None,
    ) -> dict[str, str]:
        """Direct GHN_FF_GUEST_INFO lookup for the guest's stay (name, room,
        check-in/out). Prefers an explicit guest_id; else matches by phone within
        the entity, preferring the booking that is active TODAY (so a guest with
        several bookings resolves to the current stay). Not gated by the Lumira
        feature flag — mirrors the test harness's real-profile query so it works
        the same way for real WhatsApp guests.
        """
        if db_session is None:
            return {}
        from sqlalchemy import text as _text

        eid = str(entity_id or "").strip() or None
        gid = str(guest_id or "").strip() or None
        digits = re.sub(r"\D+", "", str(guest_phone or "").strip())
        last10 = digits[-10:] if len(digits) >= 10 else digits

        def _fmt(value: Any) -> str:
            if value in (None, ""):
                return ""
            try:
                base = value.date() if hasattr(value, "date") else value
                return base.strftime("%Y-%m-%d")
            except Exception:
                return str(value)[:10]

        row = None
        try:
            if gid:
                row = (await db_session.execute(_text(
                    """
                    SELECT ID, GUEST_NAME, ROOM_NUMBER, CHECK_IN_DATE, CHECK_OUT_DATE
                    FROM GHN_FF_PROD.GHN_FF_GUEST_INFO
                    WHERE ID = :gid AND (:eid IS NULL OR ENTITY_ID = :eid)
                    LIMIT 1
                    """
                ), {"gid": gid, "eid": eid})).mappings().first()
            if row is None and eid and last10:
                row = (await db_session.execute(_text(
                    """
                    SELECT ID, GUEST_NAME, ROOM_NUMBER, CHECK_IN_DATE, CHECK_OUT_DATE
                    FROM GHN_FF_PROD.GHN_FF_GUEST_INFO
                    WHERE ENTITY_ID = :eid
                      AND (
                        REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(MOBILE_NUMBER_1,'+',''),'-',''),' ',''),'(',''),')','') LIKE CONCAT('%', :last10)
                        OR REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(MOBILE_NUMBER_2,'+',''),'-',''),' ',''),'(',''),')','') LIKE CONCAT('%', :last10)
                      )
                    ORDER BY
                        CASE WHEN NOW() BETWEEN CHECK_IN_DATE AND CHECK_OUT_DATE THEN 0 ELSE 1 END,
                        CHECK_IN_DATE DESC
                    LIMIT 1
                    """
                ), {"eid": eid, "last10": last10})).mappings().first()
        except Exception:
            logger.debug("GHN stay lookup failed (entity=%s gid=%s)", eid, gid, exc_info=True)
            return {}

        if not row:
            return {}
        rec = dict(row)
        return {
            "guest_id": str(rec.get("ID") or "").strip(),
            "guest_name": str(rec.get("GUEST_NAME") or "").strip(),
            "room_number": str(rec.get("ROOM_NUMBER") or "").strip(),
            "check_in_date": _fmt(rec.get("CHECK_IN_DATE")),
            "check_out_date": _fmt(rec.get("CHECK_OUT_DATE")),
        }

    async def _ingest_request_metadata(
        self,
        context: ConversationContext,
        request: ChatRequest,
        *,
        db_session: Any = None,
    ) -> None:
        """
        Persist integration identifiers from ChatRequest metadata into context.
        Stored under pending_data['_integration'] so handlers can build external payloads.
        """
        if not isinstance(context.pending_data, dict):
            context.pending_data = {}

        existing = context.pending_data.get("_integration", {})
        integration: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
        metadata = request.metadata if isinstance(request.metadata, dict) else {}

        canonical_map: dict[str, tuple[str, ...]] = {
            "guest_id": ("guest_id", "guestId"),
            "user_id": ("user_id", "userId"),
            "wa_number": ("wa_number", "waNumber"),
            "guest_phone": (
                "guest_phone",
                "guestPhone",
                "phone",
                "phone_number",
                "contact_number",
            ),
            "guest_name": ("guest_name", "name", "user_name"),
            "room_number": ("room_number", "room"),
            "organisation_id": ("organisation_id", "organization_id", "org_id", "entity_id"),
            "entity_id": ("entity_id",),
            "group_id": ("group_id",),
            "conversation_id": ("conversation_id",),
            "agent_id": ("agent_id", "staff_id"),
            "phase": ("phase", "phase_id"),
            "check_in": ("check_in", "stay_checkin_date", "checkin_date"),
            "check_out": ("check_out", "stay_checkout_date", "checkout_date"),
            "message_id": ("message_id",),
            "ticket_source": ("ticket_source", "source"),
            "flow": ("flow", "bot_mode"),
        }

        for canonical, aliases in canonical_map.items():
            for alias in aliases:
                value = metadata.get(alias)
                if value in (None, ""):
                    continue
                integration[canonical] = value
                break

        # An explicitly SELECTED phase (harness/admin "Phase" control → metadata
        # `selected_phase_id`) is an authoritative, sticky choice for the whole
        # conversation. Persist it as the `_selected_phase_id` marker that
        # _resolve_current_ticketing_phase honors first. The prefetch path
        # (api/routes/chat.py) already did this, but the MAIN message path did
        # not — so a selected phase was silently dropped and the resolver fell
        # back to "during_stay", which then zeroed out the phase-scoped services.
        explicit_selected_phase = self._normalize_phase_identifier(
            metadata.get("selected_phase_id") or metadata.get("selectedPhaseId")
        )
        if explicit_selected_phase:
            context.pending_data["_selected_phase_id"] = explicit_selected_phase

        if request.guest_phone:
            integration["guest_phone"] = request.guest_phone
        if not str(integration.get("guest_phone") or "").strip() and context.guest_phone:
            integration["guest_phone"] = context.guest_phone
        if context.channel:
            integration["channel"] = context.channel
        if context.hotel_code:
            integration.setdefault("organisation_id", context.hotel_code)

        guest_id_text = str(integration.get("guest_id") or "").strip()
        user_id_text = str(integration.get("user_id") or "").strip()
        if guest_id_text and user_id_text and guest_id_text != user_id_text:
            logger.warning(
                "Identity mismatch detected: guest_id and user_id differ (session_id=%s guest_id=%s user_id=%s)",
                str(context.session_id or "").strip(),
                guest_id_text,
                user_id_text,
            )

        if not context.guest_name:
            guest_name = str(integration.get("guest_name") or "").strip()
            if guest_name:
                context.guest_name = guest_name
        if not context.room_number:
            room_number = str(integration.get("room_number") or "").strip()
            if room_number:
                context.room_number = room_number

        # Hydrate the AUTHORITATIVE booking context from THIS turn's metadata so the
        # answerer's "ACTIVE BOOKING CONTEXT" block stays populated + stable for the
        # whole conversation (the guest's own stay dates, name, room type). Without
        # this, booking_* is only ever set on form-submit, so on a plain Q&A turn the
        # block is empty and the model invents the guest's stay dates. Authoritative:
        # UPDATE whenever metadata supplies a value; never wipe an existing value with
        # a blank. The widget re-sends the guest's booking context each turn.
        def _meta_first(*keys: str) -> str:
            for k in keys:
                v = metadata.get(k)
                if v not in (None, ""):
                    return str(v).strip()
            return ""

        _bk_ci = _meta_first("booking_check_in_date", "check_in_date", "stay_checkin_date", "checkin_date")
        _bk_co = _meta_first("booking_check_out_date", "check_out_date", "stay_checkout_date", "checkout_date")
        _bk_name = _meta_first("booking_guest_name")
        _bk_rt = _meta_first("booking_room_type")
        _bk_prop = _meta_first("booking_property_name")
        _bk_conf = _meta_first("booking_confirmation_code", "confirmation_code")
        if _bk_ci:
            context.booking_check_in_date = _bk_ci
        if _bk_co:
            context.booking_check_out_date = _bk_co
        if _bk_name:
            context.booking_guest_name = _bk_name
        if _bk_rt:
            context.booking_room_type = _bk_rt
        if _bk_prop:
            context.booking_property_name = _bk_prop
        if _bk_conf:
            context.booking_confirmation_code = _bk_conf

        # ── Web-widget guard on guest-identity / stay-record lookups ──────────
        # The phone/entity guest-stay lookups below (identity enrichment, GHN
        # stay-date resolution, local Guest hydration) exist for WhatsApp/Lumira
        # guests who arrive with a real identity. A WEBSITE widget visitor is a
        # fresh PRE-BOOKING lead — they must NEVER be matched to a stranger's (or
        # their own OLD) stay record by phone/entity, because that silently:
        #   • flips their journey phase to post_checkout (past stay dates), and
        #   • loads someone else's guest_id/property scope.
        # That corruption is exactly what made a "pick Jammu" visitor get answered
        # for Goa/Mussoorie or asked "which property?" (a leaked test-profile
        # entity_id + a reused phone triggered the lookup). Booking context that a
        # web guest legitimately provides still flows in via the metadata
        # hydration ABOVE (the widget re-sends it every turn), so gating these
        # lookups costs the web path nothing. WhatsApp/Lumira are unaffected.
        _channel_norm = str(getattr(context, "channel", "") or "").strip().lower()
        _req_channel = str(
            (metadata.get("channel") if isinstance(metadata, dict) else "")
            or getattr(request, "channel", "")
            or ""
        ).strip().lower()
        _WEB_CHANNELS = {"web", "web_widget", "widget", "chat_widget"}
        _is_web_widget = (_channel_norm in _WEB_CHANNELS) or (_req_channel in _WEB_CHANNELS)

        if not guest_id_text and db_session is not None and not _is_web_widget:
            entity_id = (
                integration.get("entity_id")
                or integration.get("organisation_id")
                or metadata.get("entity_id")
                or metadata.get("organisation_id")
                or metadata.get("organization_id")
                or metadata.get("org_id")
            )
            guest_phone = (
                integration.get("guest_phone")
                or integration.get("wa_number")
                or context.guest_phone
                or metadata.get("guest_phone")
                or metadata.get("wa_number")
            )
            if entity_id not in (None, "") and str(guest_phone or "").strip():
                try:
                    profile = await lumira_ticketing_repository.fetch_guest_profile(
                        db_session,
                        entity_id=entity_id,
                        guest_phone=str(guest_phone or "").strip(),
                    )
                except Exception:
                    logger.exception(
                        "Identity enrichment failed (session_id=%s entity_id=%s)",
                        str(context.session_id or "").strip(),
                        str(entity_id or "").strip(),
                    )
                    profile = {}
                if profile:
                    resolved_guest_id = str(profile.get("guest_id") or "").strip()
                    resolved_guest_name = str(profile.get("guest_name") or "").strip()
                    resolved_room_number = str(profile.get("room_number") or "").strip()
                    resolved_entity_id = str(profile.get("entity_id") or "").strip()
                    if resolved_guest_id:
                        integration["guest_id"] = resolved_guest_id
                        context.pending_data.setdefault("guest_id", resolved_guest_id)
                        logger.info(
                            "Identity enriched via guest profile lookup (session_id=%s entity_id=%s guest_id=%s)",
                            str(context.session_id or "").strip(),
                            str(entity_id or "").strip(),
                            resolved_guest_id,
                        )
                    if resolved_guest_name and not context.guest_name:
                        context.guest_name = resolved_guest_name
                    if resolved_guest_name and not str(integration.get("guest_name") or "").strip():
                        integration["guest_name"] = resolved_guest_name
                    if resolved_room_number and not context.room_number:
                        context.room_number = resolved_room_number
                    if resolved_room_number and not str(integration.get("room_number") or "").strip():
                        integration["room_number"] = resolved_room_number
                    if resolved_entity_id and not str(integration.get("entity_id") or "").strip():
                        integration["entity_id"] = resolved_entity_id

        if str(integration.get("guest_id") or "").strip():
            context.pending_data.setdefault("guest_id", str(integration.get("guest_id") or "").strip())

        # --- Server-side stay-date + phase resolution (production WhatsApp) ---
        # When the caller (WhatsApp/Lumira) sends only the guest's phone/id and NOT
        # their stay dates, resolve the dates from the guest's booking in
        # GHN_FF_GUEST_INFO, then DERIVE the journey phase from TODAY vs those dates
        # (pre_checkin / during_stay / post_checkout). This makes the phase
        # data-driven instead of trusting whatever phase the caller sent. If the
        # caller already provided stay dates, we keep them and just derive the phase.
        _have_ci = str(getattr(context, "booking_check_in_date", "") or "").strip()
        _have_co = str(getattr(context, "booking_check_out_date", "") or "").strip()
        if (not _have_ci or not _have_co) and db_session is not None and not _is_web_widget:
            _rg_id = str(integration.get("guest_id") or metadata.get("guest_id") or "").strip()
            _rg_entity = (
                integration.get("entity_id")
                or integration.get("organisation_id")
                or metadata.get("entity_id")
                or metadata.get("organisation_id")
            )
            _rg_phone = (
                str(integration.get("guest_phone") or "").strip()
                or str(context.guest_phone or "").strip()
                or str(metadata.get("guest_phone") or metadata.get("wa_number") or "").strip()
            )
            if _rg_id or (_rg_entity not in (None, "") and _rg_phone):
                try:
                    _stay_prof = await self._resolve_guest_stay_from_ghn(
                        db_session,
                        entity_id=_rg_entity,
                        guest_id=_rg_id or None,
                        guest_phone=_rg_phone or None,
                    )
                except Exception:
                    logger.exception(
                        "Stay-date resolution failed (session_id=%s)",
                        str(context.session_id or "").strip(),
                    )
                    _stay_prof = {}
                _rci = str((_stay_prof or {}).get("check_in_date") or "").strip()
                _rco = str((_stay_prof or {}).get("check_out_date") or "").strip()
                if _rci and not _have_ci:
                    context.booking_check_in_date = _rci
                    _have_ci = _rci
                if _rco and not _have_co:
                    context.booking_check_out_date = _rco
                    _have_co = _rco
                # Also fill guest identity when the caller only sent a phone
                # (Lumira repo is flag-disabled, so this direct lookup is the
                # only thing that resolves the guest for WhatsApp visitors).
                _rgn = str((_stay_prof or {}).get("guest_id") or "").strip()
                _rgname = str((_stay_prof or {}).get("guest_name") or "").strip()
                _rgroom = str((_stay_prof or {}).get("room_number") or "").strip()
                if _rgn and not str(integration.get("guest_id") or "").strip():
                    integration["guest_id"] = _rgn
                    context.pending_data.setdefault("guest_id", _rgn)
                if _rgname:
                    if not context.guest_name:
                        context.guest_name = _rgname
                    if not str(context.booking_guest_name or "").strip():
                        context.booking_guest_name = _rgname
                if _rgroom and not context.room_number:
                    context.room_number = _rgroom

        # Derive the phase from the (now-known) stay dates vs today — authoritative.
        _derived_phase = self._derive_phase_from_dates(_have_ci, _have_co)
        if _derived_phase:
            context.booking_phase = _derived_phase
            integration["phase"] = _derived_phase
            context.pending_data["_integration"] = integration
            context.pending_data["_derived_phase"] = _derived_phase

        check_in_text = str(integration.get("check_in") or "").strip()
        check_out_text = str(integration.get("check_out") or "").strip()

        # Fallback: hydrate stay context from the local Guest row when the
        # caller didn't pass dates in metadata. The form-submit flow writes
        # check_in/out/room_type/property onto new_bot_guests, so subsequent
        # chat turns can recover that without requiring the widget to keep
        # passing them every turn.
        #
        # Latency guard: this runs at most ONCE per (session, guest_phone).
        # Without the guard it added 2 DB roundtrips × VPN latency (~6-10s)
        # to every chat turn. The flag lives in pending_data so it survives
        # context save/load. Cleared when guest_phone changes.
        guest_phone_for_lookup = str(
            integration.get("guest_phone")
            or context.guest_phone
            or ""
        ).strip()
        hotel_code_for_lookup = str(context.hotel_code or "").strip()
        _guest_lookup_cache_key = f"{hotel_code_for_lookup}::{guest_phone_for_lookup}"
        _guest_lookup_done_for = str(
            context.pending_data.get("_guest_stay_lookup_done_for") or ""
        ).strip()
        _need_guest_lookup = (
            db_session is not None
            and not _is_web_widget
            and guest_phone_for_lookup
            and hotel_code_for_lookup
            and _guest_lookup_done_for != _guest_lookup_cache_key
            and (
                not check_in_text
                or not check_out_text
                or not metadata.get("booking_room_type")
                or not metadata.get("booking_property_name")
            )
        )
        if _need_guest_lookup:
            try:
                from sqlalchemy import select as _select
                from models.database import Guest as _Guest, Hotel as _Hotel

                # Single query, joined — saves one VPN roundtrip vs two
                # sequential selects.
                _row = (await db_session.execute(
                    _select(_Guest, _Hotel)
                    .join(_Hotel, _Guest.hotel_id == _Hotel.id)
                    .where(
                        _Hotel.code == hotel_code_for_lookup,
                        _Guest.phone_number == guest_phone_for_lookup,
                    )
                )).first()
                _guest_row = _row[0] if _row else None
                if _guest_row is not None:
                    if not check_in_text and getattr(_guest_row, "check_in_date", None):
                        check_in_text = _guest_row.check_in_date.isoformat()
                        integration["check_in"] = check_in_text
                        integration["stay_checkin_date"] = check_in_text
                    if not check_out_text and getattr(_guest_row, "check_out_date", None):
                        check_out_text = _guest_row.check_out_date.isoformat()
                        integration["check_out"] = check_out_text
                        integration["stay_checkout_date"] = check_out_text
                    _g_rt = str(getattr(_guest_row, "room_type", "") or "").strip()
                    if _g_rt and not str(metadata.get("booking_room_type") or "").strip():
                        metadata = {**metadata, "booking_room_type": _g_rt}
                        request.metadata = metadata
                    _g_prop = str(getattr(_guest_row, "property_name", "") or "").strip()
                    if _g_prop and not str(metadata.get("booking_property_name") or "").strip():
                        metadata = {**metadata, "booking_property_name": _g_prop}
                        request.metadata = metadata
                    _g_prop_scope = str(getattr(_guest_row, "property_scope_id", "") or "").strip()
                    if _g_prop_scope and not str(metadata.get("booking_property_scope_id") or "").strip():
                        metadata = {**metadata, "booking_property_scope_id": _g_prop_scope}
                        request.metadata = metadata
                    _g_room = str(getattr(_guest_row, "room_number", "") or "").strip()
                    if _g_room and not context.room_number:
                        context.room_number = _g_room
                        integration.setdefault("room_number", _g_room)
                    if not context.guest_name:
                        _g_name = str(getattr(_guest_row, "name", "") or "").strip()
                        if _g_name:
                            context.guest_name = _g_name
                # Mark done regardless of whether a row was found, so a
                # phone with no Guest row doesn't re-query every turn.
                context.pending_data["_guest_stay_lookup_done_for"] = _guest_lookup_cache_key
            except Exception:
                logger.exception(
                    "Guest stay-context hydration from Guest table failed (session_id=%s)",
                    str(context.session_id or "").strip(),
                )

        if check_in_text:
            context.pending_data["check_in"] = check_in_text
            context.pending_data["stay_checkin_date"] = check_in_text
            integration["stay_checkin_date"] = check_in_text
        if check_out_text:
            context.pending_data["check_out"] = check_out_text
            context.pending_data["stay_checkout_date"] = check_out_text
            integration["stay_checkout_date"] = check_out_text

        context.pending_data["_integration"] = integration
        stay_window = self._resolve_known_stay_window(
            context=context,
            pending_data=context.pending_data,
            entities={},
        )
        if stay_window is not None:
            normalized_check_in = stay_window[0].isoformat()
            normalized_check_out = stay_window[1].isoformat()
            context.pending_data["check_in"] = normalized_check_in
            context.pending_data["check_out"] = normalized_check_out
            context.pending_data["stay_checkin_date"] = normalized_check_in
            context.pending_data["stay_checkout_date"] = normalized_check_out
            context.pending_data["stay_date_range"] = f"{normalized_check_in} to {normalized_check_out}"
            integration["check_in"] = normalized_check_in
            integration["check_out"] = normalized_check_out
            integration["stay_checkin_date"] = normalized_check_in
            integration["stay_checkout_date"] = normalized_check_out
            context.pending_data["_integration"] = integration

        # Ingest booking context from metadata (sent by Chat UI booking picker)
        _booking_id_raw = metadata.get("booking_id")
        if _booking_id_raw:
            try:
                context.booking_id = int(_booking_id_raw)
            except (TypeError, ValueError):
                pass
        _bk_code = str(metadata.get("booking_confirmation_code") or "").strip()
        if _bk_code:
            context.booking_confirmation_code = _bk_code
        _bk_phase = str(metadata.get("booking_phase") or "").strip()
        if _bk_phase:
            context.booking_phase = _bk_phase
        _bk_prop = str(metadata.get("booking_property_name") or "").strip()
        _bk_prop_id = str(
            metadata.get("booking_property_scope_id")
            or metadata.get("booking_property_id")
            or ""
        ).strip()
        if _bk_prop:
            context.booking_property_name = _bk_prop
            context.pending_data["_selected_property_scope_name"] = _bk_prop
        if _bk_prop_id:
            context.booking_property_scope_id = _bk_prop_id

        if _bk_prop or _bk_prop_id:
            resolved_property_id = ""
            resolved_property_name = _bk_prop
            unresolved_hints: list[str] = []
            property_hints = [
                value
                for value in [_bk_prop_id, _bk_prop]
                if str(value or "").strip()
            ]
            try:
                property_resolution = await config_service.resolve_property_scope_ids(
                    property_hints=property_hints,
                    max_sources=200,
                    limit=1,
                )
            except Exception:
                logger.exception(
                    "Booking property resolution failed (session_id=%s property=%s property_scope_id=%s)",
                    str(context.session_id or "").strip(),
                    _bk_prop,
                    _bk_prop_id,
                )
                property_resolution = {}

            resolved_rows = property_resolution.get("resolved", []) if isinstance(property_resolution, dict) else []
            if isinstance(resolved_rows, list) and resolved_rows:
                resolved_first = resolved_rows[0] if isinstance(resolved_rows[0], dict) else {}
                resolved_property_id = str(resolved_first.get("id") or _bk_prop_id).strip()
                resolved_name_raw = str(resolved_first.get("name") or "").strip()
                if resolved_name_raw:
                    resolved_property_name = resolved_name_raw
            unresolved_hints = (
                property_resolution.get("unresolved_hints", [])
                if isinstance(property_resolution, dict)
                and isinstance(property_resolution.get("unresolved_hints"), list)
                else []
            )
            if unresolved_hints:
                logger.warning(
                    "Booking property could not be resolved to canonical ID (session_id=%s property=%s property_scope_id=%s unresolved=%s)",
                    str(context.session_id or "").strip(),
                    _bk_prop,
                    _bk_prop_id,
                    unresolved_hints,
                )

            existing_stay_property_id = str(context.pending_data.get("_stay_property_id") or "").strip()
            existing_stay_property_name = str(context.pending_data.get("_stay_property_name") or "").strip()
            stay_property_id = resolved_property_id or _bk_prop_id or existing_stay_property_id
            stay_property_name = (
                resolved_property_name
                or existing_stay_property_name
                or _bk_prop
                or _bk_prop_id
            )
            if stay_property_name and not context.booking_property_name:
                context.booking_property_name = stay_property_name
            if stay_property_id:
                context.booking_property_scope_id = stay_property_id
            if stay_property_id:
                context.pending_data["_stay_property_id"] = stay_property_id
            context.pending_data["_stay_property_name"] = stay_property_name

            phase_for_scope = self._resolve_current_ticketing_phase(
                context=context,
                pending_data=context.pending_data,
                entities={},
            )
            stay_pin_phases = {"pre_checkin", "during_stay", "post_checkout"}
            if phase_for_scope in stay_pin_phases:
                scoped_property_id = stay_property_id or _bk_prop_id or _bk_prop
                if scoped_property_id:
                    llm_orchestration_service._sync_property_scope_pending(
                        pending_data=context.pending_data,
                        property_ids=[scoped_property_id],
                        active_property_names=[stay_property_name] if stay_property_name else [],
                        selected_property_name=stay_property_name,
                    )
                context.pending_data["_stay_property_pin_enabled"] = True
            else:
                # Preserve pre-booking behavior exactly as-is.
                prebooking_active_id = _bk_prop or _bk_prop_id
                if prebooking_active_id:
                    llm_orchestration_service._sync_property_scope_pending(
                        pending_data=context.pending_data,
                        property_ids=[prebooking_active_id],
                        active_property_names=[_bk_prop or stay_property_name] if (_bk_prop or stay_property_name) else [],
                        selected_property_name=_bk_prop or stay_property_name,
                    )
                elif _bk_prop or stay_property_name:
                    context.pending_data["_selected_property_scope_name"] = _bk_prop or stay_property_name
                    context.pending_data["selected_property_scope_name"] = _bk_prop or stay_property_name
                context.pending_data["_stay_property_pin_enabled"] = False
        _bk_room = str(metadata.get("booking_room_number") or "").strip()
        if _bk_room and not context.room_number:
            context.room_number = _bk_room
        _bk_rt = str(metadata.get("booking_room_type") or "").strip()
        if _bk_rt:
            context.booking_room_type = _bk_rt
        _bk_ci = str(metadata.get("booking_check_in_date") or "").strip()
        if _bk_ci:
            context.booking_check_in_date = _bk_ci
            if not check_in_text:
                context.pending_data["check_in"] = _bk_ci
                context.pending_data["stay_checkin_date"] = _bk_ci
                integration["check_in"] = _bk_ci
                integration["stay_checkin_date"] = _bk_ci
        _bk_co = str(metadata.get("booking_check_out_date") or "").strip()
        if _bk_co:
            context.booking_check_out_date = _bk_co
            if not check_out_text:
                context.pending_data["check_out"] = _bk_co
                context.pending_data["stay_checkout_date"] = _bk_co
                integration["check_out"] = _bk_co
                integration["stay_checkout_date"] = _bk_co
        _bk_gname = str(metadata.get("booking_guest_name") or "").strip()
        if _bk_gname:
            context.booking_guest_name = _bk_gname
            if not context.guest_name:
                context.guest_name = _bk_gname
        context.pending_data["_integration"] = integration

    @staticmethod
    def _normalize_runtime_service_id(value: Any) -> str:
        return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")

    @classmethod
    def _merge_service_catalog_from_db(
        cls,
        *,
        base_catalog: Any,
        db_services: Any,
    ) -> list[dict[str, Any]]:
        base_rows = [dict(row) for row in (base_catalog or []) if isinstance(row, dict)]
        db_rows = [dict(row) for row in (db_services or []) if isinstance(row, dict)]

        base_by_id: dict[str, dict[str, Any]] = {}
        passthrough_rows: list[dict[str, Any]] = []
        for row in base_rows:
            sid = cls._normalize_runtime_service_id(row.get("id"))
            if sid:
                base_by_id[sid] = row
            else:
                passthrough_rows.append(row)

        merged_rows: list[dict[str, Any]] = []
        overlay_fields = (
            "id",
            "name",
            "type",
            "description",
            "cuisine",
            "hours",
            "delivery_zones",
            "is_active",
            "is_builtin",
            "phase_id",
            "ticketing_enabled",
            "ticketing_policy",
            "ticketing_mode",
            "form_config",
            "service_prompt_pack",
            "service_prompt_pack_custom",
            "generated_system_prompt",
        )

        for db_row in db_rows:
            sid = cls._normalize_runtime_service_id(db_row.get("id"))
            if not sid:
                continue
            merged = dict(base_by_id.pop(sid, {}))
            for field in overlay_fields:
                if field in db_row:
                    merged[field] = db_row.get(field)
            merged["id"] = str(merged.get("id") or sid).strip() or sid
            merged_rows.append(merged)

        merged_rows.extend(base_by_id.values())
        merged_rows.extend(passthrough_rows)
        return merged_rows

    @staticmethod
    def _build_restaurants_from_service_catalog(service_catalog: Any) -> list[dict[str, Any]]:
        rows = [dict(item) for item in (service_catalog or []) if isinstance(item, dict)]
        restaurants: list[dict[str, Any]] = []
        for service in rows:
            service_type = str(service.get("type") or "").strip().lower()
            if not any(token in service_type for token in ("restaurant", "dining", "food", "outlet")):
                continue

            hours_value = service.get("hours")
            if not isinstance(hours_value, dict):
                hours_value = {}

            delivery_zones = service.get("delivery_zones")
            if not isinstance(delivery_zones, list):
                delivery_zones = []
            normalized_zones = [str(zone).strip().lower() for zone in delivery_zones if str(zone).strip()]
            delivers_to_room = any(zone in {"room", "room_delivery", "in_room"} for zone in normalized_zones)
            dine_in_only = "dine_in_only" in normalized_zones or (bool(normalized_zones) and not delivers_to_room)

            restaurants.append(
                {
                    "id": service.get("id"),
                    "name": service.get("name"),
                    "cuisine": str(service.get("cuisine") or service.get("description") or "").strip(),
                    "description": service.get("description", ""),
                    "hours": hours_value,
                    "is_active": bool(service.get("is_active", True)),
                    "delivers_to_room": delivers_to_room,
                    "dine_in_only": dine_in_only,
                }
            )
        return restaurants

    async def _refresh_summary_async(self, context: ConversationContext) -> None:
        """Fire-and-forget summary refresh when chat_async_summary_enabled is set;
        otherwise await synchronously. Deferring lets the response return first
        and the next turn picks up the updated summary on read."""
        if bool(getattr(settings, "chat_async_summary_enabled", False)):
            try:
                asyncio.create_task(
                    conversation_memory_service.maybe_refresh_summary(context)
                )
                return
            except RuntimeError:
                pass
        await conversation_memory_service.maybe_refresh_summary(context)

    async def _overlay_capabilities_from_db_config(
        self,
        capabilities_summary: dict[str, Any],
        *,
        prefetched_services: list[dict[str, Any]] | None = None,
        context: ConversationContext | None = None,
    ) -> dict[str, Any]:
        merged = dict(capabilities_summary or {})

        # Per-conversation cache (shared by hotel_code) for fetches that don't
        # change mid-conversation. Each fetch checks cache first; misses fetch
        # and write back. TTL-bounded so admin updates eventually propagate.
        # CRITICAL: defaults to True. With it False (the prior default) every
        # chat turn paid 5-6 sequential VPN-to-MySQL roundtrips inside this
        # one function — measured at 17.3s on iconiqa cold turn. The cache
        # is invalidated automatically on admin write paths via TTL; admins
        # who want stricter freshness can set the env var to false.
        _cache_enabled = bool(getattr(settings, "chat_conversation_cache_enabled", True))
        _cache_ttl = conversation_cache_service.session_ttl_seconds(
            getattr(settings, "chat_conversation_cache_ttl_seconds", 1800)
        )
        _cache_scope = ""
        if _cache_enabled and context is not None:
            _cache_scope = str(getattr(context, "hotel_code", "") or "").strip() or str(
                getattr(context, "session_id", "") or ""
            ).strip()

        def _cache_get(key: str) -> Any:
            if not _cache_enabled or not _cache_scope:
                return None
            return conversation_cache_service.get(_cache_scope, key, _cache_ttl)

        def _cache_set(key: str, value: Any) -> None:
            if not _cache_enabled or not _cache_scope:
                return
            conversation_cache_service.set(_cache_scope, key, value)

        # Parallelize the 6 independent DB fetches. Each one was hitting
        # MySQL sequentially over VPN; with each roundtrip ~3s, that summed
        # to 15-18s of avoidable serialization. Running them concurrently
        # with asyncio.gather collapses the wall-clock to roughly the
        # slowest single fetch (~3-5s). Cache hits return immediately.
        async def _fetch_business():
            cached = _cache_get("business_info")
            if cached is not None:
                return cached
            try:
                value = await db_config_service.get_business_info()
                if isinstance(value, dict) and value:
                    _cache_set("business_info", value)
                return value
            except Exception:
                return None

        async def _fetch_capabilities():
            cached = _cache_get("capabilities")
            if cached is not None:
                return cached
            try:
                value = await db_config_service.get_capabilities()
                if isinstance(value, dict):
                    _cache_set("capabilities", value)
                return value
            except Exception:
                return None

        async def _fetch_intents():
            cached = _cache_get("intents")
            if cached is not None:
                return cached
            try:
                value = await db_config_service.get_intents()
                if isinstance(value, list):
                    _cache_set("intents", value)
                return value
            except Exception:
                return None

        async def _fetch_services():
            if isinstance(prefetched_services, list) and prefetched_services:
                return [dict(item) for item in prefetched_services if isinstance(item, dict)]
            cached = _cache_get("services")
            if cached is not None:
                return cached
            try:
                value = await db_config_service.get_services()
                if isinstance(value, list):
                    _cache_set("services", value)
                return value
            except Exception:
                return None

        async def _fetch_knowledge():
            cached = _cache_get("knowledge_config")
            if cached is not None:
                return cached
            try:
                value = await db_config_service.get_knowledge_config()
                if isinstance(value, dict):
                    _cache_set("knowledge_config", value)
                return value
            except Exception:
                return None

        async def _fetch_service_kb():
            _pending_cache = (
                context.pending_data
                if context is not None and isinstance(context.pending_data, dict)
                else None
            )
            _service_kb_cache_ttl_seconds = conversation_cache_service.session_ttl_seconds(300.0)
            if _pending_cache is not None:
                _cached_records = _pending_cache.get("_cached_service_kb_records")
                _cached_ts_raw = _pending_cache.get("_cached_service_kb_ts")
                _cached_ts = 0.0
                try:
                    _cached_ts = float(_cached_ts_raw or 0.0)
                except (TypeError, ValueError):
                    _cached_ts = 0.0
                if (
                    isinstance(_cached_records, list)
                    and _cached_records
                    and (time.time() - _cached_ts) <= _service_kb_cache_ttl_seconds
                ):
                    return _cached_records
            cached = _cache_get("service_kb_records_summary")
            if cached is not None:
                return cached
            try:
                rows = await db_config_service.get_service_kb_records(active_only=True)
                if isinstance(rows, list):
                    summarized = config_service.summarize_service_kb_records(rows, limit=120)
                    _cache_set("service_kb_records_summary", summarized)
                    if _pending_cache is not None:
                        _pending_cache["_cached_service_kb_records"] = summarized
                        _pending_cache["_cached_service_kb_ts"] = time.time()
                    return summarized
            except Exception:
                pass
            return None

        (
            db_business,
            db_capabilities,
            db_intents,
            db_services,
            db_knowledge,
            summarized_kb_records,
        ) = await asyncio.gather(
            _fetch_business(),
            _fetch_capabilities(),
            _fetch_intents(),
            _fetch_services(),
            _fetch_knowledge(),
            _fetch_service_kb(),
        )

        try:
            if isinstance(db_business, dict) and db_business:
                merged["business_id"] = str(
                    db_business.get("id") or merged.get("business_id") or ""
                ).strip()
                business_name = str(
                    db_business.get("name")
                    or merged.get("business_name")
                    or merged.get("hotel_name")
                    or ""
                ).strip()
                if business_name:
                    merged["business_name"] = business_name
                    merged["hotel_name"] = business_name
                for key in (
                    "hotel_id",
                    "city",
                    "timezone",
                    "currency",
                    "language",
                    "welcome_message",
                    "bot_name",
                    "resolved_welcome_message",
                    "multi_property_welcome_message",
                    "multi_property_location_manifest",
                    "property_count",
                    "location_count",
                ):
                    value = db_business.get(key)
                    if value in (None, ""):
                        continue
                    if key == "hotel_id":
                        coerced = _coerce_optional_int(value)
                        if coerced is None:
                            continue
                        merged[key] = coerced
                    else:
                        merged[key] = value
        except Exception:
            pass

        try:
            if isinstance(db_capabilities, dict):
                merged["capabilities"] = db_capabilities
                services_summary: dict[str, Any] = {}
                for cap_id, cap_data in db_capabilities.items():
                    if not isinstance(cap_data, dict):
                        continue
                    cap_key = str(cap_id).strip()
                    if not cap_key:
                        continue
                    services_summary[cap_key] = bool(cap_data.get("enabled", False))
                    if cap_data.get("hours"):
                        services_summary[f"{cap_key}_hours"] = cap_data.get("hours")
                merged["services"] = services_summary
        except Exception:
            pass

        try:
            if isinstance(db_intents, list):
                merged["intents"] = db_intents
        except Exception:
            pass

        try:
            if isinstance(db_services, list):
                merged_catalog = self._merge_service_catalog_from_db(
                    base_catalog=merged.get("service_catalog", []),
                    db_services=db_services,
                )
                merged["service_catalog"] = merged_catalog
                merged["restaurants"] = self._build_restaurants_from_service_catalog(merged_catalog)
        except Exception:
            pass

        try:
            if isinstance(db_knowledge, dict):
                db_sources = db_knowledge.get("sources", [])
                if isinstance(db_sources, list):
                    merged["knowledge_sources"] = db_sources
                if "notes" in db_knowledge:
                    merged["knowledge_notes"] = str(db_knowledge.get("notes") or "").strip()
                db_nlu_policy = db_knowledge.get("nlu_policy", {})
                if isinstance(db_nlu_policy, dict):
                    merged["nlu_policy"] = db_nlu_policy
        except Exception:
            pass

        try:
            if isinstance(summarized_kb_records, list):
                merged["service_kb_records"] = summarized_kb_records
        except Exception:
            pass

        return merged

    @staticmethod
    def _display_kb_source_name(value: str) -> str:
        name = str(value or "").strip()
        if not name:
            return "kb_source"
        name = name.replace("\\", "/").rsplit("/", 1)[-1]
        cleaned = re.sub(r"^[0-9a-f]{8}_", "", name, flags=re.IGNORECASE)
        return cleaned or name

    async def _load_latest_kb_text_from_db(
        self,
        *,
        db_session,
        hotel_code: str = "",
        max_chars: int = 90000,
    ) -> str:
        """Best-effort DB fallback for combined KB text when file sources are stale/empty."""
        if db_session is None:
            return ""

        from sqlalchemy import select
        from models.database import Hotel, KBFile

        code = config_service.resolve_hotel_code(hotel_code)
        hotel_id = None
        try:
            result = await db_session.execute(
                select(Hotel.id).where(Hotel.code.ilike(code)).limit(1)
            )
            hotel_id = result.scalar_one_or_none()
            if hotel_id is None:
                return ""

            row = await db_session.execute(
                select(KBFile.stored_name, KBFile.content)
                .where(KBFile.hotel_id == hotel_id)
                .order_by(KBFile.id.asc())
            )
            blocks: list[str] = []
            for stored_name, content in row.all():
                body = str(content or "").strip()
                if not body:
                    continue
                source_name = self._display_kb_source_name(str(stored_name or "kb_file"))
                blocks.append(f"=== SOURCE: {source_name} (db) ===\n{body}")
            combined = "\n\n".join(blocks).strip()
            if not combined:
                return ""
            if max_chars and max_chars > 0:
                return combined[:max_chars]
            return combined
        except Exception:
            return ""

    async def _load_kb_text_with_db_preference(
        self,
        *,
        db_session,
        hotel_code: str = "",
        max_chars: int = 90000,
    ) -> str:
        kb_text = str(
            config_service.get_full_kb_text_with_sources(
                max_chars=max_chars,
                max_sources=200,
            ) or ""
        ).strip()
        db_kb_text = await self._load_latest_kb_text_from_db(
            db_session=db_session,
            hotel_code=hotel_code,
            max_chars=max_chars,
        )
        prefer_db_kb = bool(getattr(settings, "chat_db_prefer_kb_content", False))
        if prefer_db_kb and db_kb_text:
            return db_kb_text
        if not kb_text and db_kb_text:
            return db_kb_text
        if db_kb_text and len(kb_text) < 2500:
            return (kb_text + "\n\n" + db_kb_text).strip()[:max_chars]
        return kb_text

    async def _augment_capabilities_from_db(
        self,
        capabilities_summary: dict,
        db_session=None,
        *,
        prefetched_services: list[dict[str, Any]] | None = None,
        context: ConversationContext | None = None,
    ) -> dict:
        """
        Overlay DB runtime configuration and merge legacy restaurant rows so
        prompt inputs stay DB-aware without breaking existing compatibility.
        """
        if not bool(getattr(settings, "chat_db_overlay_runtime_config", False)):
            return capabilities_summary

        if db_session is None:
            # NO DB SESSION, BUT THE SERVICES WERE HANDED TO US. This is the
            # suggestion-prefetch path: it cooks a chip answer on a throwaway
            # session with `db_session=None` and passes the batch's own
            # `services_allowed_in_phase`. Returning here threw those away, so
            # the router was handed `in_phase_services: []` and routed the turn
            # knowing NOTHING about the hotel's services — measured empty on
            # 10/10 prefetch calls across tenants. A guest accepting a staff
            # handoff on a chip was answered by a router that had just decided
            # "no support service is configured", and the form never opened.
            # The overlay itself needs no session (it short-circuits to the
            # prefetched list), so run it and skip only the Hotel/Restaurant
            # lookups below, which genuinely do need one.
            if not prefetched_services:
                return capabilities_summary
            merged = await self._overlay_capabilities_from_db_config(
                dict(capabilities_summary or {}),
                prefetched_services=prefetched_services,
                context=context,
            )
            if not isinstance(merged.get("restaurants"), list):
                merged["restaurants"] = self._build_restaurants_from_service_catalog(
                    list(merged.get("service_catalog", []) or [])
                )
            return merged

        from sqlalchemy import select
        from models.database import Hotel, Restaurant

        merged = await self._overlay_capabilities_from_db_config(
            dict(capabilities_summary or {}),
            prefetched_services=prefetched_services,
            context=context,
        )
        service_catalog = list(merged.get("service_catalog", []) or [])

        existing_service_ids = {
            self._normalize_runtime_service_id(service.get("id"))
            for service in service_catalog
            if isinstance(service, dict)
        }
        existing_service_ids.discard("")

        hotel_id = _coerce_optional_int(merged.get("hotel_id"))
        if hotel_id is None:
            try:
                db_business = await db_config_service.get_business_info()
                candidate_hotel_id = db_business.get("hotel_id") if isinstance(db_business, dict) else None
                hotel_id = _coerce_optional_int(candidate_hotel_id)
            except Exception:
                hotel_id = None
        if hotel_id is None:
            hotel_name = str(merged.get("hotel_name") or "").strip()
            if hotel_name:
                hotel_row = (
                    await db_session.execute(
                        select(Hotel).where(Hotel.name.ilike(hotel_name)).limit(1)
                    )
                ).scalars().first()
                if hotel_row is not None:
                    hotel_id = hotel_row.id
        if hotel_id is None:
            merged["service_catalog"] = service_catalog
            if not isinstance(merged.get("restaurants"), list):
                merged["restaurants"] = self._build_restaurants_from_service_catalog(service_catalog)
            return merged

        rows = (
            await db_session.execute(
                select(Restaurant).where(Restaurant.hotel_id == hotel_id)
            )
        ).scalars().all()

        for row in rows:
            service_id = self._normalize_runtime_service_id(row.code)
            if not service_id:
                continue
            if service_id not in existing_service_ids and not config_service.is_menu_runtime_enabled():
                # In RAG-first mode, keep admin services as authoritative.
                continue

            hours_open = row.opens_at.strftime("%H:%M:%S") if row.opens_at else "00:00:00"
            hours_close = row.closes_at.strftime("%H:%M:%S") if row.closes_at else "23:59:00"
            delivery_zones = ["room"] if row.delivers_to_room else ["dine_in_only"]

            if service_id not in existing_service_ids:
                service_catalog.append(
                    {
                        "id": service_id,
                        "name": row.name,
                        "type": "service",
                        "description": row.cuisine or "",
                        "cuisine": row.cuisine or "",
                        "hours": {"open": hours_open, "close": hours_close},
                        "delivery_zones": delivery_zones,
                        "is_active": bool(row.is_active),
                    }
                )
                existing_service_ids.add(service_id)

        merged["hotel_id"] = hotel_id
        merged["service_catalog"] = service_catalog
        if not isinstance(merged.get("restaurants"), list) or not merged.get("restaurants"):
            merged["restaurants"] = self._build_restaurants_from_service_catalog(service_catalog)
        return merged


# Global instance
# A few extracted-mixin staticmethods call sibling staticmethods by class name.
# Expose ChatService in those modules now that the class exists — it can't be a
# module-level import there (that would be a circular import).
from services import (
    chat_service_ticketing_mixin as _csm_ticketing,
    chat_service_booking_mixin as _csm_booking,
    chat_service_phase_mixin as _csm_phase,
    chat_service_ordermenu_mixin as _csm_ordermenu,
    chat_service_response_mixin as _csm_response,
)
for _csm in (_csm_ticketing, _csm_booking, _csm_phase, _csm_ordermenu, _csm_response):
    _csm.ChatService = ChatService


chat_service = ChatService()
