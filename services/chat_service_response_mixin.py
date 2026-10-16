"""ChatServiceResponseMixin: methods extracted from chat_service.py (behavior-preserving).

Moved verbatim (31 methods). ChatService inherits this mixin, so ``self.*``
resolution is unchanged. This module never imports chat_service — no cycle.
"""
import json
import logging
import re
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
from core.complexity_router import ProcessingPath
from core.context_manager import context_manager
from llm.client import llm_client
from config.settings import settings
from services.config_service import config_service
from services.conversation_memory_service import conversation_memory_service
from services.response_validator import response_validator
from services.prompt_registry_service import (
    PromptMissingError,
    prompt_registry,
)
from services.faq_context_service import build_faq_block, faq_property_scope
from handlers import handler_registry

logger = logging.getLogger(__name__)


# Shared with the ChatService domain mixins — housed in chat_service_shared
# to avoid an import cycle (the mixins import them from there, not from here).
from services.chat_service_shared import (
    CapabilityCheck,
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










logger = logging.getLogger("services.chat_service")




class ChatServiceResponseMixin:
    async def _maybe_identity_shortcut_response(
        self,
        *,
        early_identity_match: "dict[str, Any] | None",
        capabilities_summary: "dict[str, Any]",
        context: "ConversationContext",
        request: "ChatRequest",
        db_session=None,
    ) -> "ChatResponse | None":
        """Bot-name identity shortcut: answer directly, skipping routing.

        Returns a ChatResponse when the message was a recognised bot-name identity
        query; returns None to let normal processing continue.
        """
        if not (
            early_identity_match is not None
            and early_identity_match.get("match_type") == "bot_name"
        ):
            return None

        intent_result = IntentResult(
            intent=IntentType.FAQ,
            confidence=0.92,
            entities={"identity_match_type": early_identity_match.get("match_type")},
        )
        response_text = early_identity_match["response_text"]
        context.state = ConversationState.IDLE
        assistant_metadata = {
            "intent": intent_result.intent.value,
            "confidence": intent_result.confidence,
            "channel": context.channel,
            "identity_match_type": early_identity_match.get("match_type"),
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
        self._log_processing_stage(
            "mode_return",
            request=request,
            context=context,
            intent_result=intent_result,
            extra={
                "mode": "identity_shortcut",
                "response_source": "identity_shortcut",
            },
        )
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
                "identity_match_type": early_identity_match.get("match_type"),
                "response_source": "identity_shortcut",
                "memory_summary": memory_snapshot.get("summary", ""),
                "memory_facts": memory_snapshot.get("facts", {}),
            },
        )

    def _match_greeting_response(
        self,
        message: str,
        capabilities_summary: dict[str, Any],
        context: ConversationContext,
    ) -> Optional[dict]:
        """
        Deterministically handle short greeting turns in full-KB mode so they
        do not depend on model variability.
        """
        if context.pending_action:
            return None
        if context.state not in {ConversationState.IDLE, ConversationState.COMPLETED, ConversationState.ESCALATED}:
            return None

        msg = str(message or "").strip().lower()
        if not msg:
            return None
        compact = re.sub(r"[^a-z0-9 ]+", " ", msg)
        compact = re.sub(r"\s+", " ", compact).strip()
        tokens = compact.split()
        if not tokens:
            return None
        if len(tokens) > 3:
            return None

        greeting_tokens = {
            "hi",
            "hii",
            "hiii",
            "hello",
            "hey",
            "heyy",
            "yo",
            "hola",
            "namaste",
            "good morning",
            "good afternoon",
            "good evening",
        }
        is_greeting = compact in greeting_tokens or any(token in {"hi", "hello", "hey", "hola", "namaste"} for token in tokens)
        if not is_greeting:
            return None

        welcome_message = str(
            capabilities_summary.get("resolved_welcome_message")
            or capabilities_summary.get("multi_property_welcome_message")
            or capabilities_summary.get("welcome_message")
            or config_service.get_welcome_message()
            or "Welcome. How may I assist you today?"
        ).strip()
        return {
            "match_type": "greeting",
            "response_text": welcome_message,
        }

    @staticmethod
    def _normalize_multi_ask_query_text(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "").strip())

    @staticmethod
    def _response_sentence_key(value: str) -> str:
        text = re.sub(r"\s+", " ", str(value or "").strip().lower())
        return text.strip(" .,!?:;\"'")

    @classmethod
    def _dedupe_response_sentences(cls, response_text: str) -> str:
        text = str(response_text or "").strip()
        if not text:
            return ""

        normalized = re.sub(r"\r\n?", "\n", text).strip()
        if not normalized:
            return ""

        lines = [line.strip() for line in normalized.split("\n") if line.strip()]
        if not lines:
            return ""

        seen: set[str] = set()
        compact_lines: list[str] = []
        for line in lines:
            # A heading starts a new section (one per property in a
            # multi-property answer). Two hotels that share a fact - the same
            # phone number, the same check-in time - must each keep that line;
            # deduping across sections emptied the second hotel's section.
            if re.match(r"^(#{1,6}\s+\S.*|\*\*[^*\n]+\*\*:?)$", line):
                seen = set()
                compact_lines.append(line)
                continue
            if re.match(r"^\d+\.\s+", line) or line.startswith("- "):
                key = cls._response_sentence_key(line)
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                compact_lines.append(line)
                continue

            fragments = re.split(r"(?<=[.!?])\s+", line)
            kept: list[str] = []
            for fragment in fragments:
                sentence = cls._normalize_multi_ask_query_text(fragment)
                if len(sentence) < 2:
                    continue
                key = cls._response_sentence_key(sentence)
                if not key or key in seen:
                    continue
                seen.add(key)
                kept.append(sentence)
            if kept:
                compact_lines.append(" ".join(kept))

        cleaned = "\n".join(compact_lines).strip()
        if cleaned:
            return cleaned
        return cls._normalize_multi_ask_query_text(normalized)

    @staticmethod
    def _response_surface_key(text: str) -> str:
        normalized = re.sub(r"\s+", " ", str(text or "").strip().lower())
        return normalized.strip(" \n\t\r.,!?:;\"'")

    async def _maybe_llm_rewrite_response(
        self,
        *,
        response_text: str,
        user_message: str,
        intent_result: IntentResult,
        context: ConversationContext,
        capabilities_summary: dict[str, Any],
        capability_check_allowed: bool = True,
        capability_reason: str = "",
        response_source: str = "",
        validator_replaced: bool = False,
    ) -> tuple[str, dict[str, Any]]:
        base_text = self._dedupe_response_sentences(response_text) or str(response_text or "").strip()
        metadata = {
            "llm_surface_rewrite_attempted": False,
            "llm_surface_rewrite_applied": False,
            "llm_surface_rewrite_validator_replaced": False,
            "llm_surface_rewrite_validation_issues": [],
            "llm_surface_rewrite_source": str(response_source or ""),
        }
        if not base_text:
            return "", metadata

        response_surface_enabled = bool(getattr(settings, "chat_llm_response_surface_enabled", True))
        no_template_mode = bool(getattr(settings, "chat_no_template_response_mode", False))
        # In orchestration/no_template mode the service agent and ticket confirmation logic
        # already produce the correct response — never let a secondary LLM rewrite it.
        if no_template_mode:
            return base_text, metadata
        if not response_surface_enabled:
            return base_text, metadata
        if not str(getattr(settings, "openai_api_key", "") or "").strip():
            return base_text, metadata

        control_sources = {
            "deescalation",
            "parked_task_resume",
            "parked_task_cancel",
            "low_confidence",
        }
        if str(response_source or "").strip().lower() in control_sources:
            return base_text, metadata

        llm_generated_sources = {
            "llm_fallback",
            "agent_orchestrator",
            "full_kb_llm",
            "full_kb_llm_handler",
            "full_kb_shortcut",
            "pure_llm",
            "llm_orchestration",
            "multi_ask_orchestrator",
        }
        rewrite_llm_outputs = bool(getattr(settings, "chat_llm_response_surface_rewrite_llm_outputs", False))
        if (
            str(response_source or "").strip().lower() in llm_generated_sources
            and not validator_replaced
            and not rewrite_llm_outputs
            and not no_template_mode
        ):
            return base_text, metadata

        metadata["llm_surface_rewrite_attempted"] = True
        selected_phase = self._get_selected_phase_context(
            context=context,
            pending_data=context.pending_data if isinstance(context.pending_data, dict) else {},
            entities=intent_result.entities if isinstance(intent_result.entities, dict) else {},
        )
        phase_name = str(selected_phase.get("selected_phase_name") or "").strip()
        phase_id = str(selected_phase.get("selected_phase_id") or "").strip()
        phase_label = phase_name or (phase_id.replace("_", " ").title() if phase_id else "Current")
        phase_services = self._current_phase_service_names(
            context=context,
            capabilities_summary=capabilities_summary,
        )
        phase_services_text = ", ".join(phase_services) if phase_services else "Not specified"
        nlu_policy = capabilities_summary.get("nlu_policy", {}) if isinstance(capabilities_summary, dict) else {}
        dos = nlu_policy.get("dos", []) if isinstance(nlu_policy, dict) else []
        donts = nlu_policy.get("donts", []) if isinstance(nlu_policy, dict) else []
        dos_text = "; ".join(str(item).strip() for item in dos if str(item).strip()) or "None"
        donts_text = "; ".join(str(item).strip() for item in donts if str(item).strip()) or "None"
        # Multi-model: the response surface is guest-facing prose -> 5.4, and it
        # loads 5.4's surface prompt.
        try:
            from services.model_override_service import resolve_role_model
            model = resolve_role_model("response_surface") or None
        except Exception:
            model = str(getattr(settings, "chat_llm_response_surface_model", "") or "").strip() or None
        temperature = float(getattr(settings, "chat_llm_response_surface_temperature", 0.35) or 0.35)
        max_tokens = max(120, int(getattr(settings, "chat_llm_response_surface_max_tokens", 420) or 420))
        confirmation_phrase = str(getattr(settings, "chat_confirmation_phrase", "yes confirm") or "yes confirm").strip()

        try:
            system_prompt = await prompt_registry.get("chat.response_surface", model=model)
        except PromptMissingError:
            logger.exception("chat_response_surface_prompt_missing")
            return base_text, metadata
        _faq_block_surface = build_faq_block(
            (context.pending_data or {}).get("_active_property_ids")
            if isinstance(context.pending_data, dict) else None
        )
        if _faq_block_surface:
            system_prompt = _faq_block_surface + "\n\n" + system_prompt
        user_payload = (
            f"User message: {str(user_message or '').strip()}\n"
            f"Intent: {intent_result.intent.value}\n"
            f"Current phase: {phase_label} ({phase_id or 'unknown'})\n"
            f"Current phase services: {phase_services_text}\n"
            f"Capability allowed: {bool(capability_check_allowed)}\n"
            f"Capability reason (if blocked): {str(capability_reason or '').strip() or 'N/A'}\n"
            f"Policy DO rules: {dos_text}\n"
            f"Policy DONT rules: {donts_text}\n"
            f"Confirmation phrase: {confirmation_phrase}\n\n"
            f"Draft response:\n{base_text}"
        )
        try:
            rewritten = await llm_client.chat(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_payload},
                ],
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except Exception:
            rewritten = ""

        candidate = self._dedupe_response_sentences(rewritten) or str(rewritten or "").strip()
        if not candidate or len(candidate) < 3:
            return base_text, metadata

        validation = response_validator.validate(
            response_text=candidate,
            intent_result=intent_result,
            context=context,
            capabilities_summary=capabilities_summary,
            capability_check_allowed=capability_check_allowed,
            capability_reason=capability_reason,
        )
        issue_codes = [str(issue.code) for issue in (validation.issues or []) if getattr(issue, "code", None)]
        metadata["llm_surface_rewrite_validation_issues"] = issue_codes
        if validation.valid:
            metadata["llm_surface_rewrite_applied"] = (
                self._response_surface_key(candidate) != self._response_surface_key(base_text)
            )
            return candidate, metadata

        if validation.action == "replace" and validation.replacement_response:
            safe_text = self._dedupe_response_sentences(validation.replacement_response) or str(
                validation.replacement_response or ""
            ).strip()
            metadata["llm_surface_rewrite_validator_replaced"] = True

            # First try an issue-aware LLM repair instead of surfacing deterministic replacement text.
            repair_issue_text = ", ".join(issue_codes) if issue_codes else "policy_violation"
            try:
                repair_prompt = await prompt_registry.get(
                    "chat.response_repair",
                    {
                        "user_message": str(user_message or "").strip(),
                        "intent_value": intent_result.intent.value,
                        "repair_issue_text": repair_issue_text,
                        "phase_label": phase_label,
                        "phase_id": phase_id or "unknown",
                        "phase_services_text": phase_services_text,
                        "capability_check_allowed": bool(capability_check_allowed),
                        "capability_reason": str(capability_reason or "").strip() or "N/A",
                        "base_text": base_text,
                        "candidate": candidate,
                        "safe_text": safe_text,
                    },
                )
                repair_system_prompt = await prompt_registry.get("chat.response_repair_system")
            except PromptMissingError:
                logger.exception("chat_response_repair_prompt_missing")
                return safe_text, metadata
            _faq_block_repair = build_faq_block(
                (context.pending_data or {}).get("_active_property_ids")
                if isinstance(context.pending_data, dict) else None
            )
            if _faq_block_repair:
                repair_system_prompt = _faq_block_repair + "\n\n" + repair_system_prompt
            try:
                repaired = await llm_client.chat(
                    messages=[
                        {"role": "system", "content": repair_system_prompt},
                        {"role": "user", "content": repair_prompt},
                    ],
                    model=model,
                    temperature=max(0.1, min(temperature, 0.45)),
                    max_tokens=max_tokens,
                )
            except Exception:
                repaired = ""
            repaired_candidate = self._dedupe_response_sentences(repaired) or str(repaired or "").strip()
            if repaired_candidate:
                repaired_validation = response_validator.validate(
                    response_text=repaired_candidate,
                    intent_result=intent_result,
                    context=context,
                    capabilities_summary=capabilities_summary,
                    capability_check_allowed=capability_check_allowed,
                    capability_reason=capability_reason,
                )
                if repaired_validation.valid:
                    metadata["llm_surface_rewrite_applied"] = (
                        self._response_surface_key(repaired_candidate) != self._response_surface_key(base_text)
                    )
                    return repaired_candidate
                if repaired_validation.action == "replace" and repaired_validation.replacement_response:
                    safe_text = self._dedupe_response_sentences(repaired_validation.replacement_response) or str(
                        repaired_validation.replacement_response or ""
                    ).strip()

            if bool(getattr(settings, "chat_llm_response_surface_rewrite_replacements", True)) and safe_text:
                try:
                    soft_rewrite = await llm_client.chat(
                        messages=[
                            {
                                "role": "system",
                                "content": (
                                    "Paraphrase the response with the same policy constraints and meaning. "
                                    "Do not relax restrictions. Return plain text only."
                                ),
                            },
                            {"role": "user", "content": safe_text},
                        ],
                        model=model,
                        temperature=max(0.0, min(temperature, 0.25)),
                        max_tokens=max_tokens,
                    )
                except Exception:
                    soft_rewrite = ""
                soft_candidate = self._dedupe_response_sentences(soft_rewrite) or str(soft_rewrite or "").strip()
                if soft_candidate:
                    soft_validation = response_validator.validate(
                        response_text=soft_candidate,
                        intent_result=intent_result,
                        context=context,
                        capabilities_summary=capabilities_summary,
                        capability_check_allowed=capability_check_allowed,
                        capability_reason=capability_reason,
                    )
                    if soft_validation.valid:
                        safe_text = soft_candidate
                    elif (
                        soft_validation.action == "replace"
                        and soft_validation.replacement_response
                    ):
                        safe_text = self._dedupe_response_sentences(soft_validation.replacement_response) or str(
                            soft_validation.replacement_response or ""
                        ).strip()

            metadata["llm_surface_rewrite_applied"] = (
                self._response_surface_key(safe_text) != self._response_surface_key(base_text)
            )
            return safe_text, metadata

        return base_text, metadata

    @classmethod
    def _split_stacked_multi_ask_part(cls, part: str) -> list[str]:
        text = cls._normalize_multi_ask_query_text(part)
        if not text:
            return []

        markers = list(
            re.finditer(r"\b(?:what|when|where|which|who|how|is|are|do|can|will)\b", text, flags=re.IGNORECASE)
        )
        if len(markers) <= 1:
            return [text]

        stop_prev_tokens = {
            "i",
            "you",
            "u",
            "me",
            "to",
            "the",
            "a",
            "an",
            "and",
            "or",
            "if",
            "please",
            "tell",
            "show",
            "explain",
            "know",
            "need",
            "want",
            "can",
            "do",
            "is",
            "are",
            "will",
        }
        split_points = [0]
        for marker in markers[1:]:
            prefix_tokens = re.findall(r"[a-z0-9']+", text[: marker.start()].lower())
            if len(prefix_tokens) < 4:
                continue
            prev_token = prefix_tokens[-1]
            if len(prev_token) < 3 or prev_token in stop_prev_tokens:
                continue
            split_points.append(marker.start())

        if len(split_points) == 1:
            return [text]

        split_points.append(len(text))
        chunks: list[str] = []
        for idx in range(len(split_points) - 1):
            chunk = cls._normalize_multi_ask_query_text(text[split_points[idx] : split_points[idx + 1]])
            if chunk:
                chunks.append(chunk)
        return chunks or [text]

    @classmethod
    def _fallback_split_multi_ask_queries(cls, message: str, max_items: int) -> list[str]:
        text = cls._normalize_multi_ask_query_text(message)
        if not text:
            return []

        parts = re.split(
            r"[?.!]+\s+|[;]+\s+|,\s+(?=(?:what|when|where|which|who|how|is|are|do|can|will|please|also)\b)|\s+(?:and|also|plus|then)\s+",
            text,
            flags=re.IGNORECASE,
        )
        expanded_parts: list[str] = []
        for part in parts:
            expanded_parts.extend(cls._split_stacked_multi_ask_part(part))

        cleaned: list[str] = []
        seen: set[str] = set()
        for part in expanded_parts:
            query = cls._normalize_multi_ask_query_text(part)
            if len(query) < 3:
                continue
            if len(re.findall(r"[a-z0-9]+", query.lower())) < 2:
                continue
            key = query.lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(query)
            if len(cleaned) >= max_items:
                break
        return cleaned

    async def _decompose_multi_ask_queries(
        self,
        message: str,
        *,
        selected_phase_id: str = "",
        selected_phase_name: str = "",
    ) -> list[str]:
        normalized_message = self._normalize_multi_ask_query_text(message)
        if not normalized_message:
            return []

        min_items = max(2, int(getattr(settings, "chat_multi_ask_min_items", 2) or 2))
        max_items = max(min_items, int(getattr(settings, "chat_multi_ask_max_items", 6) or 6))
        max_items = min(max_items, 12)

        if str(getattr(settings, "openai_api_key", "") or "").strip():
            decompose_model = str(getattr(settings, "chat_multi_ask_decompose_model", "") or "").strip() or None
            phase_label = str(selected_phase_name or "").strip() or (
                str(selected_phase_id or "").replace("_", " ").title()
            )
            phase_id = str(selected_phase_id or "").strip() or "unknown"
            prompt = (
                "You are a query decomposition assistant.\n"
                "Task: decide if the user message contains multiple independent asks.\n"
                f"Selected user journey phase: {phase_label} ({phase_id}).\n"
                "Return strict JSON with keys:\n"
                "is_multi_ask: boolean\n"
                "sub_requests: array of rewritten standalone asks preserving original meaning.\n"
                "Rules:\n"
                "- Keep wording concise and faithful.\n"
                "- Do not add/remove asks.\n"
                "- If single ask, return is_multi_ask=false and sub_requests as empty array.\n"
                f"- Output at most {max_items} sub_requests."
            )
            try:
                parsed = await llm_client.chat_with_json(
                    messages=[
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": normalized_message},
                    ],
                    model=decompose_model,
                    temperature=0.0,
                )
            except Exception:
                parsed = {}

            llm_multi = self._coerce_optional_bool(
                parsed.get("is_multi_ask") if isinstance(parsed, dict) else None
            )
            raw_items = []
            if isinstance(parsed, dict):
                raw_items = parsed.get("sub_requests", [])
            if not isinstance(raw_items, list):
                raw_items = []

            llm_queries: list[str] = []
            seen_queries: set[str] = set()
            for item in raw_items:
                query = self._normalize_multi_ask_query_text(item)
                if len(query) < 3:
                    continue
                key = query.lower()
                if key in seen_queries:
                    continue
                seen_queries.add(key)
                llm_queries.append(query)
                if len(llm_queries) >= max_items:
                    break

            if llm_multi is True and len(llm_queries) >= min_items:
                return llm_queries
            if len(llm_queries) >= min_items:
                return llm_queries

        fallback = self._fallback_split_multi_ask_queries(normalized_message, max_items)
        if len(fallback) >= min_items:
            return fallback
        return []

    @staticmethod
    def _build_multi_ask_fallback_response(sub_results: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for index, row in enumerate(sub_results, start=1):
            response_text = str(row.get("response_text") or "").strip()
            if not response_text:
                continue
            lines.append(f"{index}. {response_text}")
        if not lines:
            return (
                "I could not find this in the current knowledge base for this property. "
                "If you want, I can connect you with our team."
            )
        return "\n".join(lines)

    async def _compose_multi_ask_response_with_llm(
        self,
        *,
        original_message: str,
        sub_results: list[dict[str, Any]],
        selected_phase_id: str = "",
        selected_phase_name: str = "",
    ) -> str:
        fallback = self._build_multi_ask_fallback_response(sub_results)
        if not str(getattr(settings, "openai_api_key", "") or "").strip():
            return fallback

        compose_model = str(getattr(settings, "chat_multi_ask_compose_model", "") or "").strip() or None
        evidence_lines: list[str] = []
        for index, row in enumerate(sub_results, start=1):
            question = str(row.get("query") or "").strip()
            answer = str(row.get("response_text") or "").strip()
            if not question and not answer:
                continue
            evidence_lines.append(f"Q{index}: {question}")
            evidence_lines.append(f"A{index}: {answer}")
        evidence_blob = "\n".join(evidence_lines).strip()
        if not evidence_blob:
            return fallback

        phase_label = str(selected_phase_name or "").strip() or (
            str(selected_phase_id or "").replace("_", " ").title()
        )
        phase_id = str(selected_phase_id or "").strip() or "unknown"
        prompt = (
            "You are a response composer.\n"
            "Compose one final assistant reply from the provided sub-answer evidence.\n"
            f"Selected user journey phase: {phase_label} ({phase_id}).\n"
            "Rules:\n"
            "- Use only provided evidence; do not add new facts.\n"
            "- Cover each answered sub-question.\n"
            "- Keep tone concise and service-assistant friendly.\n"
            "- If evidence conflicts, prefer safer and more restrictive statements.\n"
            "- Return plain text only."
        )
        try:
            rendered = await llm_client.chat(
                messages=[
                    {"role": "system", "content": prompt},
                    {
                        "role": "user",
                        "content": (
                            f"Original user message:\n{original_message}\n\n"
                            f"Sub-answer evidence:\n{evidence_blob}"
                        ),
                    },
                ],
                model=compose_model,
                temperature=0.1,
                max_tokens=380,
            )
        except Exception:
            rendered = ""

        candidate = self._normalize_multi_ask_query_text(rendered).strip()
        if not candidate:
            return fallback
        if len(candidate) < 12:
            return fallback
        return candidate

    async def _run_multi_ask_kb_answer(
        self,
        *,
        message: str,
        context: ConversationContext,
        capabilities_summary: dict[str, Any],
        db_session=None,
    ) -> str:
        forced_intent = IntentResult(
            intent=IntentType.FAQ,
            confidence=1.0,
            entities={"forced_mode": "multi_ask_subquery"},
        )
        sub_context = context.model_copy(deep=True)
        internal_pending_entries = conversation_memory_service.internal_pending_entries(context.pending_data)
        sub_context.state = ConversationState.IDLE
        sub_context.pending_action = None
        sub_context.pending_data = conversation_memory_service.merge_with_internal(
            {},
            internal_pending_entries,
        )
        sub_context.pending_data.pop("_clarification_attempts", None)

        try:
            handler_result = await handler_registry.dispatch(
                forced_intent,
                message,
                sub_context,
                capabilities_summary,
                db_session,
            )
        except Exception:
            handler_result = None
        if handler_result is None:
            return ""
        return str(handler_result.response_text or "").strip()

    async def _run_multi_ask_subquery_agent(
        self,
        *,
        query: str,
        context: ConversationContext,
        capabilities_summary: dict[str, Any],
        llm_context: dict[str, Any],
        history_window: int,
        db_session=None,
    ) -> dict[str, Any]:
        normalized_query = self._normalize_multi_ask_query_text(query)
        if not normalized_query:
            return {
                "query": "",
                "response_text": "",
                "response_source": "multi_ask_empty_query",
                "intent": IntentType.UNCLEAR.value,
                "confidence": 0.0,
                "entities": {},
                "ticketing": {},
            }

        sub_llm_context = dict(llm_context)
        sub_llm_context["state"] = ConversationState.IDLE.value
        sub_llm_context["pending_action"] = None
        sub_llm_context["pending_data"] = {}
        intent_result = await self._classify_intent(
            normalized_query,
            context.to_llm_messages(count=history_window),
            sub_llm_context,
        )

        phase_gate = self._detect_ticketing_phase_service_mismatch(
            message=normalized_query,
            context=context,
            pending_data=context.pending_data if isinstance(context.pending_data, dict) else {},
            entities=intent_result.entities if isinstance(intent_result.entities, dict) else {},
        )
        if phase_gate is None:
            phase_gate = self._detect_phase_service_unavailable_for_intent(
                message=normalized_query,
                intent=intent_result.intent,
                context=context,
                pending_data=context.pending_data if isinstance(context.pending_data, dict) else {},
                entities=intent_result.entities if isinstance(intent_result.entities, dict) else {},
            )

        if phase_gate is not None:
            response_text = await self._compose_policy_guardrail_response(
                user_message=normalized_query,
                context=context,
                capabilities_summary=capabilities_summary,
                intent=intent_result.intent,
                gate_payload=phase_gate,
                fallback_text="",
                response_source="multi_ask_phase_gate",
            )
            if not response_text:
                response_text = "I can help with services available in your current phase."
            response_source = "multi_ask_phase_gate"
        else:
            intent_enabled_check = self._check_intent_enabled(intent_result.intent)
            capability_check = (
                intent_enabled_check
                if not intent_enabled_check.allowed
                else self._check_capability_for_intent(
                    context.hotel_code,
                    intent_result,
                    normalized_query,
                )
            )
            if not capability_check.allowed:
                response_text = self._build_capability_denial_response(capability_check)
                response_source = "multi_ask_capability_denial"
            else:
                sub_context = context.model_copy(deep=True)
                internal_pending_entries = conversation_memory_service.internal_pending_entries(context.pending_data)
                sub_context.state = ConversationState.IDLE
                sub_context.pending_action = None
                sub_context.pending_data = conversation_memory_service.merge_with_internal(
                    {},
                    internal_pending_entries,
                )
                sub_context.pending_data.pop("_clarification_attempts", None)

                identity_match = self._match_identity_response(
                    normalized_query,
                    capabilities_summary,
                    sub_context,
                )
                if identity_match is not None:
                    response_text = str(identity_match.get("response_text") or "").strip()
                    response_source = "multi_ask_identity_shortcut"
                else:
                    service_overview = self._match_service_overview_response(
                        normalized_query,
                        capabilities_summary,
                        sub_context,
                    )
                    if service_overview is not None:
                        response_text = str(service_overview.get("response_text") or "").strip()
                        response_source = "multi_ask_service_overview_shortcut"
                    else:
                        service_match = self._match_service_information_response(
                            normalized_query,
                            sub_context,
                            capabilities_summary,
                        )
                        if service_match is not None:
                            response_text = str(service_match.get("response_text") or "").strip()
                            response_source = "multi_ask_service_catalog_shortcut"
                        else:
                            kb_answer = await self._run_multi_ask_kb_answer(
                                message=normalized_query,
                                context=sub_context,
                                capabilities_summary=capabilities_summary,
                                db_session=db_session,
                            )
                            if kb_answer:
                                response_text = kb_answer
                                response_source = "multi_ask_kb_handler"
                            else:
                                response_text = (
                                    "I could not find this in the current knowledge base for this property. "
                                    "If you want, I can connect you with our team."
                                )
                                response_source = "multi_ask_kb_miss"

        response_text = self._dedupe_response_sentences(response_text) or str(response_text or "").strip()

        ticketing_status = await self._evaluate_ticketing_status_for_turn(
            message=normalized_query,
            context=context,
            intent=intent_result.intent,
            response_text=response_text,
            current_pending_action=context.pending_action,
            pending_action_target=None,
            capabilities_summary=capabilities_summary,
        )

        return {
            "query": normalized_query,
            "response_text": response_text,
            "response_source": response_source,
            "intent": intent_result.intent.value,
            "confidence": float(intent_result.confidence),
            "entities": intent_result.entities if isinstance(intent_result.entities, dict) else {},
            "ticketing": ticketing_status,
        }

    async def _maybe_handle_multi_ask_message(
        self,
        *,
        request: ChatRequest,
        context: ConversationContext,
        capabilities_summary: dict[str, Any],
        routing_decision: Any,
        memory_snapshot: dict[str, Any],
        history_window: int,
        db_session=None,
    ) -> ChatResponse | None:
        if not bool(getattr(settings, "chat_multi_ask_orchestration_enabled", True)):
            return None
        if context.pending_action:
            return None
        if context.state not in {ConversationState.IDLE, ConversationState.COMPLETED, ConversationState.ESCALATED}:
            return None

        effective_user_message = self._normalize_multi_ask_query_text(request.message)
        if not effective_user_message:
            return None
        if context.state == ConversationState.ESCALATED and self._is_return_to_bot_request(effective_user_message):
            return None

        msg_lower = effective_user_message.lower()
        token_count = len(re.findall(r"[a-z0-9]+", msg_lower))
        if token_count < 4:
            return None
        has_connector_marker = any(
            marker in msg_lower
            for marker in (" and ", ",", " also ", " plus ", " then ", "?", " & ")
        )
        if not has_connector_marker and msg_lower.count("?") < 2:
            return None

        selected_phase_context = self._get_selected_phase_context(
            context=context,
            pending_data=context.pending_data if isinstance(context.pending_data, dict) else {},
            entities={},
        )

        sub_queries = await self._decompose_multi_ask_queries(
            effective_user_message,
            selected_phase_id=selected_phase_context.get("selected_phase_id", ""),
            selected_phase_name=selected_phase_context.get("selected_phase_name", ""),
        )
        min_items = max(2, int(getattr(settings, "chat_multi_ask_min_items", 2) or 2))
        if len(sub_queries) < min_items:
            return None

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
        }

        sub_results: list[dict[str, Any]] = []
        for query in sub_queries:
            sub_result = await self._run_multi_ask_subquery_agent(
                query=query,
                context=context,
                capabilities_summary=capabilities_summary,
                llm_context=llm_context,
                history_window=history_window,
                db_session=db_session,
            )
            sub_results.append(sub_result)

        if not sub_results:
            return None

        response_text = await self._compose_multi_ask_response_with_llm(
            original_message=effective_user_message,
            sub_results=sub_results,
            selected_phase_id=selected_phase_context.get("selected_phase_id", ""),
            selected_phase_name=selected_phase_context.get("selected_phase_name", ""),
        )
        response_text = self._dedupe_response_sentences(response_text) or str(response_text or "").strip()

        internal_pending_entries = conversation_memory_service.internal_pending_entries(context.pending_data)
        context.pending_action = None
        context.pending_data = conversation_memory_service.merge_with_internal(
            {},
            internal_pending_entries,
        )
        context.pending_data.pop("_clarification_attempts", None)
        context.state = ConversationState.IDLE

        ticketing_statuses = [
            item.get("ticketing")
            for item in sub_results
            if isinstance(item.get("ticketing"), dict)
        ]
        ticketing_required = any(
            bool(status.get("ticketing_required"))
            for status in ticketing_statuses
        )
        ticketing_create_allowed = any(
            bool(status.get("ticketing_create_allowed"))
            for status in ticketing_statuses
        )

        assistant_metadata = {
            "intent": IntentType.FAQ.value,
            "confidence": 0.9,
            "channel": context.channel,
            "response_source": "multi_ask_orchestrator",
            "multi_ask_orchestrated": True,
            "multi_ask_sub_count": len(sub_results),
            "ticketing_required": ticketing_required,
            "ticketing_create_allowed": ticketing_create_allowed,
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

        suggested_actions = self._finalize_user_query_suggestions(
            self._get_suggested_actions(
                ConversationState.IDLE,
                IntentType.FAQ,
                capabilities_summary,
            ),
            state=ConversationState.IDLE,
            intent=IntentType.FAQ,
            pending_action=context.pending_action,
            pending_data=context.pending_data,
            capabilities_summary=capabilities_summary,
        )

        metadata = {
            "message_count": len(context.messages),
            "entities": {"multi_ask_queries": [item.get("query") for item in sub_results]},
            "classified_intent": IntentType.FAQ.value,
            "classified_confidence": 0.9,
            "routing_path": getattr(getattr(routing_decision, "path", ProcessingPath.COMPLEX), "value", "complex"),
            "routing_score": float(getattr(routing_decision, "score", 0.0)),
            "routing_signals": list(getattr(routing_decision, "signals", [])),
            "response_source": "multi_ask_orchestrator",
            "multi_ask_orchestrated": True,
            "multi_ask_sub_count": len(sub_results),
            "multi_ask_sub_results": sub_results,
            "ticketing_status_checked": True,
            "ticketing_required": ticketing_required,
            "ticketing_create_allowed": ticketing_create_allowed,
            "ticketing_statuses": ticketing_statuses,
            "memory_summary": memory_snapshot.get("summary", ""),
            "memory_facts": memory_snapshot.get("facts", {}),
            "memory_recent_changes": memory_snapshot.get("recent_changes", []),
        }
        return ChatResponse(
            session_id=request.session_id,
            message=response_text,
            intent=IntentType.FAQ,
            confidence=0.9,
            state=ConversationState.IDLE,
            suggested_actions=suggested_actions,
            metadata=metadata,
        )

    @staticmethod
    def _llm_response_implies_staff_action(response_text: str) -> bool:
        text = str(response_text or "").strip().lower()
        if not text:
            return False
        markers = (
            "i will escalate",
            "i'll escalate",
            "escalate this",
            "our staff",
            "our team",
            "team will",
            "someone will contact",
            "will be addressed",
            "will arrange",
            "priority follow-up",
        )
        return any(marker in text for marker in markers)

    def _rewrite_affirmative_selection_reply(
        self,
        context: ConversationContext,
        message: str,
    ) -> str:
        """
        When user replies with bare 'yes' in an awaiting-selection step, map it
        to the first offered option from the previous assistant question.
        This avoids losing conversational intent due to under-specified affirmatives.
        """
        if context.state != ConversationState.AWAITING_SELECTION:
            return message
        if self._detect_simple_binary_reply(message) != IntentType.CONFIRMATION_YES:
            return message

        last_assistant = ""
        for msg in reversed(context.messages):
            if msg.role == MessageRole.ASSISTANT:
                last_assistant = str(msg.content or "").strip()
                break
        if not last_assistant:
            return message

        text = re.sub(r"\s+", " ", last_assistant).strip()
        lower = text.lower()
        if "would you like to" not in lower or " or " not in lower:
            return message

        match = re.search(r"would you like to\s+(.+?)\s+or\s+(.+?)(?:\?|$)", lower)
        if not match:
            return message

        first_option = match.group(1).strip(" .")
        if not first_option:
            return message
        # Use readable text for the LLM, preserving original semantics.
        return first_option

    def _rewrite_more_options_reply(
        self,
        context: ConversationContext,
        message: str,
    ) -> str:
        """
        Generic follow-up rewrite for "show more" requests.
        Adds a topic and explicit non-repeat instruction so the LLM returns only
        additional options, or clearly states no additional options remain.
        """
        if not self._looks_like_more_options_followup(message):
            return message

        stopwords = {
            "show",
            "more",
            "option",
            "options",
            "any",
            "do",
            "you",
            "u",
            "hv",
            "have",
            "please",
            "list",
            "tell",
            "me",
            "about",
            "the",
            "a",
            "an",
            "for",
            "to",
            "with",
            "and",
            "else",
            "anything",
        }

        def topic_tokens(text: str) -> list[str]:
            tokens = re.findall(r"[a-z0-9]+", str(text or "").lower())
            return [token for token in tokens if len(token) >= 3 and token not in stopwords]

        current_tokens = topic_tokens(message)
        topic = " ".join(current_tokens[:2]).strip()

        if not topic:
            for msg in reversed(context.messages[:-1]):
                if msg.role != MessageRole.USER:
                    continue
                previous_tokens = topic_tokens(str(msg.content or ""))
                if previous_tokens:
                    topic = " ".join(previous_tokens[:2]).strip()
                    break

        if not topic:
            return message

        return (
            f"Show more options for {topic}. "
            "Return only additional options not already listed in this conversation. "
            "If there are no additional options, clearly say there are no more options."
        )

    @staticmethod
    def _is_room_choice_loop_response(response_text: str) -> bool:
        msg = re.sub(r"\s+", " ", str(response_text or "").strip().lower())
        if not msg:
            return False
        return (
            "would you like to view" in msg
            and "check" in msg
            and "availability" in msg
        )

    @staticmethod
    def _rewrite_bot_question_to_user_query(action_text: str) -> str:
        text = str(action_text or "").strip().lower()
        if not text:
            return ""
        if "are you interested in" in text and any(
            marker in text for marker in ("treatment", "treatments", "therapy", "therapies")
        ):
            return "Show spa treatments"
        if "are you interested in" in text and any(
            marker in text for marker in ("package", "packages")
        ):
            return "Show spa packages"
        if any(marker in text for marker in ("spa", "massage")) and any(
            marker in text for marker in ("treatment", "treatments", "therapy", "therapies")
        ):
            return "Show spa treatments"
        if any(marker in text for marker in ("spa", "massage")) and any(
            marker in text for marker in ("package", "packages")
        ):
            return "Show spa packages"
        if "therapist" in text:
            return "Show available therapists"
        if "time" in text or "preferred time" in text:
            return "Book at 6 PM"
        if "room number" in text:
            return "My room number is 305"
        if any(marker in text for marker in ("how many guests", "party size", "number of guests")):
            return "Book for 2 guests"
        if any(marker in text for marker in ("check in", "check-in", "check out", "check-out", "dates")):
            return "Share booking dates"
        return ""

    def _match_identity_response(
        self,
        message: str,
        capabilities_summary: dict,
        context: ConversationContext,
    ) -> Optional[dict]:
        """Handle bot/business identity questions deterministically."""
        if context.pending_action:
            return None
        if context.state not in {ConversationState.IDLE, ConversationState.COMPLETED, ConversationState.ESCALATED}:
            return None

        msg_lower = str(message or "").strip().lower()
        if not msg_lower:
            return None

        compact = re.sub(r"[^a-z0-9 ]+", " ", msg_lower)
        compact = re.sub(r"\s+", " ", compact).strip()
        tokens = re.findall(r"[a-z0-9]+", compact)
        token_set = set(tokens)
        joined = "".join(tokens)

        direct_identity_markers = (
            "your name",
            "ur name",
            "who are you",
            "who r u",
            "bot name",
            "what is your name",
            "what's your name",
            "whats your name",
            "who are u",
            "who u",
            "who ru",
            "who r you",
        )
        asks_bot_name = any(marker in compact for marker in direct_identity_markers)
        if not asks_bot_name and token_set:
            who_like = any(SequenceMatcher(a=tok, b="who").ratio() >= 0.75 for tok in token_set)
            are_like = any(
                tok in {"are", "r"}
                or SequenceMatcher(a=tok, b="are").ratio() >= 0.66
                for tok in token_set
            )
            you_like = any(tok in {"you", "u", "ur"} for tok in token_set)
            name_like = "name" in token_set
            bot_like = any(tok in {"bot", "assistant"} for tok in token_set)

            if who_like and you_like and (are_like or len(tokens) <= 3):
                asks_bot_name = True
            elif name_like and (you_like or bot_like):
                asks_bot_name = True
            elif joined in {"whoru", "whoareu", "whoareyou", "whatsyourname"}:
                asks_bot_name = True

        if asks_bot_name:
            bot_name = str(capabilities_summary.get("bot_name") or "Assistant").strip()
            business_name = str(
                capabilities_summary.get("hotel_name")
                or capabilities_summary.get("business_name")
                or "our business"
            ).strip()
            city = str(capabilities_summary.get("city") or "").strip()
            # A multi-property group's "city" field is a placeholder like
            # "Group of Hotels" — rendering "<name> in <that>" reads as the wrong
            # "Zone in Group of Hotels". Only append "in <city>" for a genuine single
            # city; for a group (group-ish label or 2+ properties) say "for <name>".
            city_lower = city.lower()
            city_is_group = (not city) or any(
                tok in city_lower
                for tok in ("group", "hotels", "properties", "chain", "collection")
            )
            try:
                _prop_count = int(capabilities_summary.get("property_count") or 0)
            except (TypeError, ValueError):
                _prop_count = 0
            if _prop_count >= 2:
                city_is_group = True
            if city and not city_is_group:
                response_text = (
                    f"I am {bot_name}, your concierge assistant at {business_name} in {city}. "
                    "How may I assist you today?"
                )
            else:
                response_text = (
                    f"I am {bot_name}, your concierge assistant for {business_name}. "
                    "How may I assist you today?"
                )
            return {
                "match_type": "bot_name",
                "response_text": response_text,
            }

        asks_business_name = any(
            marker in msg_lower
            for marker in (
                "which hotel is this",
                "hotel name",
                "which business is this",
                "where am i",
                "tell me about your hotel",
                "tell me about ur hotel",
                "about your hotel",
                "about ur hotel",
                "about this hotel",
            )
        )
        if asks_business_name:
            business_name = str(capabilities_summary.get("hotel_name") or "our business").strip()
            city = str(capabilities_summary.get("city") or "").strip()
            location = f" in {city}" if city else ""
            return {
                "match_type": "business_name",
                "response_text": f"This is {business_name}{location}.",
            }

        return None

    def _match_service_overview_response(
        self,
        message: str,
        capabilities_summary: dict,
        context: ConversationContext,
    ) -> Optional[dict]:
        """
        Deterministically answer "what services are available" style requests
        so they do not fall through to FAQ/RAG when service metadata is local.
        """
        if context.pending_action:
            return None
        if context.state not in {ConversationState.IDLE, ConversationState.COMPLETED, ConversationState.ESCALATED}:
            return None

        msg_lower = str(message or "").strip().lower()
        if not msg_lower:
            return None

        tokens = re.findall(r"[a-z]+", msg_lower)
        if not tokens:
            return None

        has_service_signal = any(
            token.startswith("serv")
            or SequenceMatcher(a=token, b="services").ratio() >= 0.78
            for token in tokens
        )
        has_availability_signal = any(
            token in {"available", "provide", "provides", "offered", "offer", "offers", "show", "list", "all", "options"}
            or SequenceMatcher(a=token, b="available").ratio() >= 0.74
            for token in tokens
        )
        normalized_message = re.sub(r"\s+", " ", msg_lower).strip()
        is_overview_query = (
            "what services" in msg_lower
            or "which services" in msg_lower
            or "services available" in msg_lower
            or "what can you" in msg_lower
            or "what do you offer" in msg_lower
            or "what are available" in msg_lower
            or "all services" in msg_lower
            or "list services" in msg_lower
            or "show services" in msg_lower
            or normalized_message in {"services", "service list", "available services"}
        )
        broad_query_markers = {"what", "which", "list", "show", "all", "available", "options"}
        has_broad_query_marker = any(token in broad_query_markers for token in tokens)
        if not is_overview_query and has_service_signal and has_availability_signal and has_broad_query_marker:
            is_overview_query = True
        if not is_overview_query:
            return None

        service_catalog = capabilities_summary.get("service_catalog", [])
        active_services = []
        if isinstance(service_catalog, list):
            active_services = [
                svc
                for svc in service_catalog
                if isinstance(svc, dict) and bool(svc.get("is_active", True))
            ]

        if active_services:
            names = [str(svc.get("name") or svc.get("id") or "").strip() for svc in active_services]
            names = [name for name in names if name]
            names = names[:8]
            if names:
                return {
                    "response_text": "Here are the currently available services: " + ", ".join(names) + ".",
                }

        services_flags = capabilities_summary.get("services", {})
        if isinstance(services_flags, dict):
            labels: list[str] = []
            for capability_id, enabled in services_flags.items():
                if capability_id.endswith("_hours") or not enabled:
                    continue
                label = str(capability_id).replace("_", " ").strip().title()
                if label and label not in labels:
                    labels.append(label)
            if labels:
                return {
                    "response_text": "Here are the currently enabled services: " + ", ".join(labels[:8]) + ".",
                }

        return {
            "response_text": "I don't see any configured services right now. I can connect you with our team to assist further.",
        }

    def _match_room_number_update_response(
        self,
        message: str,
        context: ConversationContext,
    ) -> Optional[dict]:
        """
        Detect profile-style room number updates and acknowledge them deterministically.
        """
        if context.pending_action:
            return None
        if context.state not in {ConversationState.IDLE, ConversationState.COMPLETED, ConversationState.ESCALATED}:
            return None

        msg_lower = str(message or "").strip().lower()
        if not msg_lower:
            return None

        if "room" not in msg_lower:
            return None
        if "?" in msg_lower:
            return None
        if msg_lower.startswith(("what", "which", "where", "is my room", "what is my room")):
            return None

        match = re.search(r"\bmy\s+room(?:\s*number)?\s*(?:is|=|:)\s*([a-z0-9-]{2,10})\b", msg_lower)
        if not match:
            return None

        room_number = match.group(1).upper()
        if not any(ch.isdigit() for ch in room_number):
            return None
        return {
            "room_number": room_number,
            "response_text": f"Got it, I've updated your room number to {room_number}.",
        }

    def _match_room_number_lookup_response(
        self,
        message: str,
        context: ConversationContext,
        memory_snapshot: dict,
    ) -> Optional[dict]:
        """Answer room-number recall questions from context/memory."""
        if context.pending_action:
            return None
        if context.state not in {ConversationState.IDLE, ConversationState.COMPLETED, ConversationState.ESCALATED}:
            return None

        msg_lower = str(message or "").strip().lower()
        if not msg_lower:
            return None

        asks_room = (
            "my room number" in msg_lower
            or "what is my room" in msg_lower
            or "which room" in msg_lower
        )
        if not asks_room:
            return None

        room_number = str(context.room_number or "").strip().upper()
        if not room_number:
            facts = memory_snapshot.get("facts", {}) if isinstance(memory_snapshot, dict) else {}
            if isinstance(facts, dict):
                room_number = str(facts.get("room_number") or "").strip().upper()

        if room_number:
            return {"response_text": f"Your room number is {room_number}."}
        return {"response_text": "I don't have your room number yet. Please share it in this format: 'My room number is 202'."}

    def _match_service_information_response(
        self,
        message: str,
        context: ConversationContext,
        capabilities_summary: dict,
        allow_pending: bool = False,
    ) -> Optional[dict]:
        """
        Match user queries against configured services so service metadata is always considered.
        """
        if not allow_pending and context.pending_action:
            return None
        if not allow_pending and context.state not in {ConversationState.IDLE, ConversationState.COMPLETED}:
            return None

        service_catalog = capabilities_summary.get("service_catalog", [])
        if not isinstance(service_catalog, list) or not service_catalog:
            return None

        msg_lower = self._normalize_message_for_routing(message)
        if not msg_lower:
            return None
        if self._is_personal_reservation_query(msg_lower):
            return None

        action_query = any(
            marker in msg_lower
            for marker in ("book", "reserve", "order", "arrange", "need", "send", "request", "confirm", "want", "wanna")
        )
        info_query = any(
            marker in msg_lower
            for marker in (
                "what",
                "when",
                "where",
                "show",
                "list",
                "timing",
                "hours",
                "open",
                "close",
                "available",
                "provide",
                "provides",
                "offered",
                "offer",
                "offers",
                "tell me about",
                "details",
                "catalog",
                "treatment",
                "treatments",
                "package",
                "packages",
                "therapist",
                "therapists",
                "service",
            )
        )
        if not action_query and not info_query:
            return None

        msg_tokens = re.findall(r"[a-z0-9]+", msg_lower)
        matched_service: Optional[dict] = None
        best_score = 0.0
        for service in service_catalog:
            if not isinstance(service, dict):
                continue
            score = 0.0
            for alias in self._service_aliases_for_matching(service):
                score = max(score, self._service_alias_match_score(msg_lower, msg_tokens, alias))

            description_keywords = self._service_description_keywords(service)
            if description_keywords:
                desc_hits = sum(1 for token in description_keywords if self._token_matches_message(token, msg_tokens))
                if desc_hits >= 2:
                    score = max(score, 0.72 + min(0.2, desc_hits * 0.04))

            if score > best_score:
                best_score = score
                matched_service = service

        unsupported_label = self._extract_unconfigured_service_request_label(msg_lower, service_catalog)
        if matched_service is None or best_score < 0.72:
            if unsupported_label:
                return self._build_service_catalog_unavailable_match(unsupported_label)
            return None

        if unsupported_label and not self._service_entry_supports_request_label(matched_service, unsupported_label):
            return self._build_service_catalog_unavailable_match(unsupported_label)

        service_name = str(matched_service.get("name") or matched_service.get("id") or "This service").strip()
        service_id = str(matched_service.get("id") or "").strip()
        service_type = str(matched_service.get("type") or "").strip().lower()
        active = bool(matched_service.get("is_active", True))
        description = str(
            matched_service.get("description")
            or matched_service.get("cuisine")
            or ""
        ).strip()
        hours = matched_service.get("hours")
        if isinstance(hours, dict):
            open_time = str(hours.get("open") or "").strip()
            close_time = str(hours.get("close") or "").strip()
            hours_text = f"{open_time} - {close_time}".strip(" -") if (open_time or close_time) else ""
        else:
            hours_text = str(hours or "").strip()
        is_spa_service = self._is_spa_service_entry(matched_service)
        spa_catalog_query = self._looks_like_spa_catalog_query(msg_lower)
        spa_option_hints = self._extract_spa_option_hints(matched_service)
        details: list[str] = []
        if description:
            details.append(description)
        if hours_text:
            details.append(f"Hours: {hours_text}")
        details_text = " ".join(details).strip()

        if not active:
            alternatives = []
            for service in service_catalog:
                if not isinstance(service, dict):
                    continue
                if not bool(service.get("is_active", True)):
                    continue
                alt_type = str(service.get("type") or "").strip().lower()
                if service_type and alt_type and alt_type != service_type:
                    continue
                alt_name = str(service.get("name") or "").strip()
                if alt_name and alt_name.lower() != service_name.lower():
                    alternatives.append(alt_name)
                if len(alternatives) >= 3:
                    break
            alt_text = ""
            if alternatives:
                alt_text = f" Available alternatives: {', '.join(alternatives)}."
            return {
                "service_id": service_id,
                "service_name": service_name,
                "match_type": "inactive_service",
                "response_text": f"{service_name} is currently unavailable.{alt_text}",
                "suggested_actions": ["Show available services", "Talk to human"],
            }

        # Keep known transactional requests on normal handler path even if the
        # message also contains informational words (for example "book spa treatment").
        if action_query and self._service_action_should_route_to_transaction(matched_service, msg_lower):
            return None
        if action_query and not info_query:
            response_text = f"{service_name} is available."
            if details_text:
                response_text += f" {details_text}"
            response_text += " Please share your preferred time and required details so I can help next."
            return {
                "service_id": service_id,
                "service_name": service_name,
                "match_type": "service_catalog_action",
                "response_text": response_text,
                "suggested_actions": ["Share details", "Cancel"],
            }

        if is_spa_service and spa_catalog_query:
            spa_response = f"{service_name} is available."
            if details_text:
                spa_response += f" {details_text}"
            if spa_option_hints:
                spa_response += f"\n\nAvailable spa options: {', '.join(spa_option_hints[:8])}."
            spa_response += (
                "\n\nYou can ask for spa treatments or packages, or share a direct request like "
                "\"Book Swedish massage at 6 PM\"."
            )
            return {
                "service_id": service_id,
                "service_name": service_name,
                "match_type": "service_catalog_spa_info",
                "response_text": spa_response,
                "suggested_actions": [
                    "Show spa treatments",
                    "Show spa packages",
                    "Book spa at 6 PM",
                    "Talk to human",
                ],
            }
        if details_text:
            response_text = f"{service_name} is available. {details_text}"
        else:
            response_text = f"{service_name} is available."

        if action_query:
            response_text += " Please share your preferred time and any specific requirement so I can help next."

        return {
            "service_id": service_id,
            "service_name": service_name,
            "match_type": "service_catalog_info",
            "response_text": response_text,
            "suggested_actions": ["Show available services", "Ask another question", "Talk to human"],
        }

    async def _compose_policy_guardrail_response(
        self,
        *,
        user_message: str,
        context: ConversationContext,
        capabilities_summary: dict[str, Any],
        intent: IntentType,
        gate_payload: dict[str, Any] | None = None,
        fallback_text: str = "",
        response_source: str = "policy_guardrail",
    ) -> str:
        gate = gate_payload if isinstance(gate_payload, dict) else {}
        fallback = self._dedupe_response_sentences(fallback_text) or str(fallback_text or "").strip()
        if not fallback:
            fallback = "I can help with services available in your current phase."

        selected_phase = self._get_selected_phase_context(
            context=context,
            pending_data=context.pending_data if isinstance(context.pending_data, dict) else {},
            entities={},
        )
        current_phase_id = self._normalize_phase_identifier(
            gate.get("current_phase_id") or selected_phase.get("selected_phase_id")
        )
        current_phase_name = str(gate.get("current_phase_name") or selected_phase.get("selected_phase_name") or "").strip()
        if not current_phase_name and current_phase_id:
            current_phase_name = self._phase_label(current_phase_id)

        service_phase_id = self._normalize_phase_identifier(gate.get("service_phase_id"))
        service_phase_name = str(gate.get("service_phase_name") or "").strip()
        if service_phase_id and not service_phase_name:
            service_phase_name = self._phase_label(service_phase_id)
        service_name = str(gate.get("service_name") or gate.get("requested_service") or "This service").strip()
        timing_hint = self._phase_transition_timing_hint(
            current_phase_id=current_phase_id,
            service_phase_id=service_phase_id,
        )

        available_now = self._phase_services_for_id(
            capabilities_summary=capabilities_summary,
            phase_id=current_phase_id,
            limit=6,
        )
        raw_actions = gate.get("suggested_actions")
        suggested_actions: list[str] = []
        if isinstance(raw_actions, list):
            for action in raw_actions:
                label = str(action or "").strip()
                if not label:
                    continue
                if label.lower() in {"show available services", "ask another question"}:
                    continue
                if label not in suggested_actions:
                    suggested_actions.append(label)
                if len(suggested_actions) >= 4:
                    break

        service_catalog_context: list[dict[str, Any]] = []
        catalog_rows = capabilities_summary.get("service_catalog", [])
        if isinstance(catalog_rows, list):
            for service in catalog_rows:
                if not isinstance(service, dict):
                    continue
                if not bool(service.get("is_active", True)):
                    continue
                service_id = str(service.get("id") or "").strip()
                service_name = str(service.get("name") or "").strip()
                if not service_id and not service_name:
                    continue
                phase_row_id = self._normalize_phase_identifier(service.get("phase_id"))
                phase_row_name = self._phase_label(phase_row_id) if phase_row_id else ""
                prompt_pack = service.get("service_prompt_pack")
                if not isinstance(prompt_pack, dict):
                    prompt_pack = {}
                knowledge_hint = str(prompt_pack.get("extracted_knowledge") or "").strip()
                if not knowledge_hint:
                    knowledge_hint = str(service.get("description") or service.get("cuisine") or "").strip()
                service_catalog_context.append(
                    {
                        "id": service_id,
                        "name": service_name or service_id,
                        "phase_id": phase_row_id,
                        "phase_name": phase_row_name,
                        "description": str(service.get("description") or "").strip(),
                        "ticketing_enabled": bool(service.get("ticketing_enabled", True)),
                        "knowledge_hint": knowledge_hint[:500],
                    }
                )
                if len(service_catalog_context) >= 80:
                    break

        prompt_payload = {
            "intent": intent.value if isinstance(intent, IntentType) else str(intent),
            "user_message": str(user_message or "").strip(),
            "policy_reason_code": str(
                gate.get("policy_blocked_reason")
                or gate.get("ticket_skip_reason")
                or gate.get("code")
                or ""
            ).strip(),
            "current_phase": {
                "id": current_phase_id,
                "name": current_phase_name,
            },
            "requested_service": {
                "id": str(gate.get("service_id") or "").strip(),
                "name": service_name,
                "target_phase_id": service_phase_id,
                "target_phase_name": service_phase_name,
                "timing_hint": timing_hint,
            },
            "phase_service_unavailable": bool(gate.get("phase_service_unavailable")),
            "available_services_now": available_now,
            "suggested_services_now": suggested_actions,
            "service_catalog_context": service_catalog_context,
        }

        if not str(getattr(settings, "openai_api_key", "") or "").strip():
            return fallback

        system_prompt = (
            "You are a hotel concierge assistant composing a policy-guarded reply.\n"
            "Return plain text only.\n"
            "Use only the supplied facts.\n"
            "Do not promise immediate booking/order/ticket actions when blocked.\n"
            "If service is out of phase, mention current phase and when it becomes available.\n"
            "If requested_service.id is empty, infer likely service from user_message using service_catalog_context.\n"
            "When a likely match exists in another phase, include brief details from that service's description/knowledge_hint.\n"
            "For informational topics where execution is blocked, provide info-only response and clearly avoid transactional promises.\n"
            "Keep response concise, natural, and guest-friendly (2-4 short sentences).\n"
        )
        user_prompt = (
            "Generate the final user response from this JSON policy payload:\n"
            f"{json.dumps(prompt_payload, ensure_ascii=False)}"
        )
        # Multi-model: this policy-guarded reply composer is guest-facing prose,
        # so it runs on the answering model (5.4). Prompt is inline (no DB variant).
        try:
            from services.model_override_service import resolve_role_model
            model = resolve_role_model("response_surface") or None
        except Exception:
            model = str(getattr(settings, "chat_llm_response_surface_model", "") or "").strip() or None
        base_temperature = float(getattr(settings, "chat_llm_response_surface_temperature", 0.35) or 0.35)
        temperature = max(0.2, min(0.8, base_temperature + 0.1))
        max_tokens = max(120, int(getattr(settings, "chat_llm_response_surface_max_tokens", 420) or 420))

        try:
            rendered = await llm_client.chat(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                model=model,
                temperature=temperature,
                max_tokens=min(max_tokens, 260),
            )
        except Exception:
            return fallback

        candidate = self._dedupe_response_sentences(rendered) or str(rendered or "").strip()
        if not candidate:
            return fallback

        validation_intent = intent if isinstance(intent, IntentType) else IntentType.FAQ
        validation = response_validator.validate(
            response_text=candidate,
            intent_result=IntentResult(intent=validation_intent, confidence=0.82, entities={}),
            context=context,
            capabilities_summary=capabilities_summary,
            capability_check_allowed=True,
            capability_reason="",
        )
        if validation.valid:
            return candidate

        if validation.action == "replace" and validation.replacement_response:
            safe = self._dedupe_response_sentences(validation.replacement_response) or str(
                validation.replacement_response or ""
            ).strip()
            if safe and bool(getattr(settings, "chat_no_template_response_mode", False)):
                rewritten_safe, _ = await self._maybe_llm_rewrite_response(
                    response_text=safe,
                    user_message=user_message,
                    intent_result=IntentResult(intent=validation_intent, confidence=0.82, entities={}),
                    context=context,
                    capabilities_summary=capabilities_summary,
                    capability_check_allowed=True,
                    capability_reason="",
                    response_source=response_source,
                    validator_replaced=True,
                )
                safe = self._dedupe_response_sentences(rewritten_safe) or str(rewritten_safe or "").strip() or safe
            return safe or fallback

        return fallback

    def _match_memory_information_response(
        self,
        message: str,
        context: ConversationContext,
        memory_snapshot: dict,
    ) -> Optional[dict]:
        """
        Deterministic personal-memory lookup for "my booking/order/departure" questions.
        """
        if context.pending_action:
            return None
        if context.state not in {ConversationState.IDLE, ConversationState.COMPLETED, ConversationState.ESCALATED}:
            return None

        msg_lower = str(message or "").strip().lower()
        if not msg_lower:
            return None

        facts = memory_snapshot.get("facts", {}) if isinstance(memory_snapshot, dict) else {}
        if not isinstance(facts, dict) or not facts:
            return None

        asks_personal_booking = (
            "my" in msg_lower
            and any(token in msg_lower for token in ("reservation", "booking", "table"))
        ) or any(
            marker in msg_lower
            for marker in (
                "reservation time",
                "booking time",
                "what time is my reservation",
                "what time is my booking",
            )
        )
        if asks_personal_booking:
            latest_booking = facts.get("latest_booking", {})
            if isinstance(latest_booking, dict) and latest_booking:
                restaurant = str(latest_booking.get("restaurant") or "the restaurant").strip()
                booking_time = str(latest_booking.get("time") or "").strip()
                booking_date = str(latest_booking.get("date") or "").strip()
                details = [f"Your latest reservation is for {restaurant}"]
                if booking_time:
                    details.append(f"at {booking_time}")
                if booking_date and booking_date.lower() not in {"today", ""}:
                    details.append(f"on {booking_date}")
                details_text = " ".join(details).strip() + "."

                return {
                    "match_type": "memory_booking_lookup",
                    "response_text": details_text,
                }
            return {
                "match_type": "memory_booking_lookup_missing",
                "response_text": "I don't see a confirmed reservation in this chat yet. Would you like me to book one now?",
            }

        asks_departure = (
            any(marker in msg_lower for marker in ("what time am i leaving", "when am i leaving", "my departure"))
            or ("my checkout" in msg_lower)
            or (("leave" in msg_lower or "leaving" in msg_lower or "checkout" in msg_lower) and "my" in msg_lower)
        )
        if asks_departure:
            departure_time = str(facts.get("departure_time") or "").strip()
            departure_day = str(facts.get("departure_day") or "").strip()
            if departure_time:
                day_text = f" {departure_day}" if departure_day else ""
                return {
                    "match_type": "memory_departure_lookup",
                    "response_text": f"You told me your departure is at {departure_time}{day_text}.",
                }

        asks_order = (
            "my order" in msg_lower
            and any(token in msg_lower for token in ("id", "status", "details", "what", "which"))
        ) or ("order id" in msg_lower)
        if asks_order:
            latest_order = facts.get("latest_order", {})
            if isinstance(latest_order, dict) and latest_order:
                order_id = str(latest_order.get("id") or "").strip()
                items = latest_order.get("items") if isinstance(latest_order.get("items"), list) else []
                total = latest_order.get("total")

                parts = []
                if order_id:
                    parts.append(f"Your latest order ID is {order_id}.")
                if items:
                    parts.append(f"Items: {', '.join(str(i) for i in items[:5])}.")
                if total is not None and str(total) != "":
                    try:
                        parts.append(f"Total: Rs.{float(total):.0f}.")
                    except (TypeError, ValueError):
                        pass

                if parts:
                    return {
                        "match_type": "memory_order_lookup",
                        "response_text": " ".join(parts),
                    }
                return {
                    "match_type": "memory_order_lookup_missing",
                    "response_text": "I can see your recent order, but I don't have full details. I can connect you with staff for exact status.",
                }

        return None

    @staticmethod
    def _is_reasonable_message_preprocess_rewrite(original: str, rewritten: str) -> bool:
        base = re.sub(r"\s+", " ", str(original or "").strip())
        candidate = re.sub(r"\s+", " ", str(rewritten or "").strip())
        if not base or not candidate:
            return False
        if candidate == base:
            return True
        if len(candidate) > max(2600, len(base) * 3):
            return False

        base_tokens = set(re.findall(r"[a-z0-9]+", base.lower()))
        candidate_tokens = set(re.findall(r"[a-z0-9]+", candidate.lower()))
        if not base_tokens or not candidate_tokens:
            return False

        if len(base_tokens) == 1 and len(candidate_tokens) == 1:
            base_token = next(iter(base_tokens))
            candidate_token = next(iter(candidate_tokens))
            return SequenceMatcher(a=base_token, b=candidate_token).ratio() >= 0.45

        overlap = len(base_tokens & candidate_tokens) / max(1, len(base_tokens))
        return overlap >= 0.35

    def _build_capability_denial_response(self, capability_check: CapabilityCheck) -> str:
        """Build a helpful response when capability is not available."""
        response = capability_check.reason

        if capability_check.alternatives:
            alternatives_text = ", ".join(capability_check.alternatives)
            response += f"\n\nAlternatively, I can help you with: {alternatives_text}"

        return response

    async def _apply_full_kb_response_validation(
        self,
        *,
        response_text: str,
        effective_intent: IntentType,
        effective_confidence: float,
        context: ConversationContext,
        capabilities_summary: dict[str, Any],
        request_message: str,
    ) -> tuple[str, bool, list[str], bool]:
        """
        Run the same runtime validator used by classic flow for full-KB responses.
        Returns: (final_response_text, replaced, issue_codes, capability_allowed)
        """
        validation_intent = IntentResult(
            intent=effective_intent,
            confidence=max(0.0, min(1.0, float(effective_confidence or 0.0))),
            entities={},
        )
        intent_enabled_check = self._check_intent_enabled(effective_intent)
        capability_check = (
            intent_enabled_check
            if not intent_enabled_check.allowed
            else self._check_capability_for_intent(
                context.hotel_code,
                validation_intent,
                request_message,
            )
        )
        validation = response_validator.validate(
            response_text=response_text,
            intent_result=validation_intent,
            context=context,
            capabilities_summary=capabilities_summary,
            capability_check_allowed=capability_check.allowed,
            capability_reason=capability_check.reason,
        )
        issue_codes = [str(issue.code) for issue in (validation.issues or []) if getattr(issue, "code", None)]
        validator_replaced = False
        final_response_text = str(response_text or "").strip()
        if not validation.valid and validation.action == "replace" and validation.replacement_response:
            final_response_text = str(validation.replacement_response or "").strip()
            validator_replaced = True
        final_response_text, rewrite_meta = await self._maybe_llm_rewrite_response(
            response_text=final_response_text,
            user_message=request_message,
            intent_result=validation_intent,
            context=context,
            capabilities_summary=capabilities_summary,
            capability_check_allowed=capability_check.allowed,
            capability_reason=capability_check.reason,
            response_source="full_kb_llm",
            validator_replaced=validator_replaced,
        )
        if bool(rewrite_meta.get("llm_surface_rewrite_validator_replaced")):
            validator_replaced = True
        rewrite_issue_codes = [
            str(code).strip()
            for code in (rewrite_meta.get("llm_surface_rewrite_validation_issues") or [])
            if str(code).strip()
        ]
        combined_issue_codes: list[str] = []
        for code in [*issue_codes, *rewrite_issue_codes]:
            if code and code not in combined_issue_codes:
                combined_issue_codes.append(code)
        return final_response_text, validator_replaced, combined_issue_codes, capability_check.allowed

    async def _generate_response(
        self,
        user_message: str,
        intent_result: IntentResult,
        conversation_history: list[dict],
        context: dict,
        capability_check: Optional[CapabilityCheck] = None,
    ) -> str:
        """Generate response using LLM (fallback when no handler matched)."""
        try:
            if intent_result.intent == IntentType.CONFIRMATION_YES:
                return self._handle_confirmation_yes(context)

            if intent_result.intent == IntentType.CONFIRMATION_NO:
                return "No problem, I've cancelled that. Is there anything else I can help you with?"

            # Use LLM for response generation. Scope the FAQ block (injected
            # globally inside generate_response) to the focused property so this
            # fallback path respects property-scoped FAQs too. Absent scope ->
            # None -> no filtering (unchanged behavior).
            _fb_scope = None
            if isinstance(context, dict):
                _fb_scope = (
                    (context.get("pending_data") or {}).get("_active_property_ids")
                    if isinstance(context.get("pending_data"), dict)
                    else context.get("active_property_ids")
                )
            with faq_property_scope(_fb_scope):
                response = await llm_client.generate_response(
                    user_message,
                    intent_result.intent.value,
                    intent_result.entities,
                    conversation_history,
                    context,
                )

            # Add confirmation prompt for orders
            if intent_result.intent == IntentType.ORDER_FOOD and "confirm" not in response.lower():
                if intent_result.entities.get("items"):
                    response += "\n\nShall I confirm this order? (Yes/No)"

            return response

        except Exception as e:
            print(f"Response generation error: {e}")
            return "I apologize, but I'm having trouble processing your request right now. Could you please try again or let me connect you with our staff?"
