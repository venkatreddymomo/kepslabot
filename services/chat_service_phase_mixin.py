"""ChatServicePhaseMixin: methods extracted from chat_service.py (behavior-preserving).

Moved verbatim (24 methods). ChatService inherits this mixin, so ``self.*``
resolution is unchanged. This module never imports chat_service — no cycle.
"""
import logging
import re
from datetime import date
from typing import Any, Optional
from pydantic import BaseModel, Field

from schemas.chat import (
    ConversationContext,
    IntentType,
)
from config.settings import settings
from services.config_service import config_service

logger = logging.getLogger(__name__)


# Shared with the ChatService domain mixins — housed in chat_service_shared
# to avoid an import cycle (the mixins import them from there, not from here).
from services.chat_service_shared import (
    _PHASE_INTENT_SERVICE_MARKERS,
)


class CapabilityCheck(BaseModel):
    """Result of a capability check."""
    allowed: bool
    reason: str
    alternatives: list[str] = Field(default=[])
    constraints: Optional[dict] = None


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

_PHASE_INTENT_SERVICE_MARKERS_STRICT = {
    IntentType.ORDER_FOOD: _PHASE_INTENT_SERVICE_MARKERS[IntentType.ORDER_FOOD],
    IntentType.TABLE_BOOKING: (
        "spa",
        "treatment",
        "table",
        "reservation",
        "restaurant",
        "recreation",
        "appointment",
        "transport",
        "cab",
        "taxi",
        "pickup",
        "drop",
        "airport transfer",
    ),
    IntentType.ROOM_SERVICE: _PHASE_INTENT_SERVICE_MARKERS[IntentType.ROOM_SERVICE],
}





logger = logging.getLogger("services.chat_service")

_PHASE_INTENT_SERVICE_MARKERS_STRICT = {
    IntentType.ORDER_FOOD: _PHASE_INTENT_SERVICE_MARKERS[IntentType.ORDER_FOOD],
    IntentType.TABLE_BOOKING: (
        "spa",
        "treatment",
        "table",
        "reservation",
        "restaurant",
        "recreation",
        "appointment",
        "transport",
        "cab",
        "taxi",
        "pickup",
        "drop",
        "airport transfer",
    ),
    IntentType.ROOM_SERVICE: _PHASE_INTENT_SERVICE_MARKERS[IntentType.ROOM_SERVICE],
}


class ChatServicePhaseMixin:
    def _resolve_service_from_phase_signals(
        self,
        *,
        services: list[dict[str, Any]],
        entities: dict[str, Any] | None = None,
        pending_data: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if not isinstance(services, list) or not services:
            return None

        id_index: dict[str, dict[str, Any]] = {}
        name_index: dict[str, dict[str, Any]] = {}
        for service in services:
            if not isinstance(service, dict):
                continue
            if not bool(service.get("is_active", True)):
                continue
            sid = self._normalize_service_identifier(service.get("id"))
            if sid and sid not in id_index:
                id_index[sid] = service
            sname = self._normalize_service_identifier(service.get("name"))
            if sname and sname not in name_index:
                name_index[sname] = service

        signal_map: dict[str, Any] = {}
        if isinstance(pending_data, dict):
            signal_map.update(pending_data)
        if isinstance(entities, dict):
            signal_map.update(entities)

        id_candidates = (
            signal_map.get("service_id"),
            signal_map.get("target_service_id"),
            signal_map.get("resolved_service_id"),
            signal_map.get("workflow_id"),
            signal_map.get("booking_sub_category"),
            signal_map.get("sub_category"),
            signal_map.get("ticket_sub_category"),
        )
        for candidate in id_candidates:
            key = self._normalize_service_identifier(candidate)
            if key and key in id_index:
                service = id_index[key]
                result = {
                    "service_id": str(service.get("id") or "").strip(),
                    "service_name": str(service.get("name") or service.get("id") or "This service").strip(),
                    "phase_id": self._normalize_phase_identifier(service.get("phase_id")),
                    "score": 1.0,
                    "matched_by_signal": True,
                }
                if "ticketing_enabled" in service:
                    result["ticketing_enabled"] = service.get("ticketing_enabled")
                return result

        name_candidates = (
            signal_map.get("service_name"),
            signal_map.get("target_service"),
            signal_map.get("resolved_service_name"),
        )
        for candidate in name_candidates:
            key = self._normalize_service_identifier(candidate)
            if key and key in name_index:
                service = name_index[key]
                result = {
                    "service_id": str(service.get("id") or "").strip(),
                    "service_name": str(service.get("name") or service.get("id") or "This service").strip(),
                    "phase_id": self._normalize_phase_identifier(service.get("phase_id")),
                    "score": 0.95,
                    "matched_by_signal": True,
                }
                if "ticketing_enabled" in service:
                    result["ticketing_enabled"] = service.get("ticketing_enabled")
                return result
        return None

    def _current_phase_service_names(
        self,
        *,
        context: ConversationContext,
        capabilities_summary: dict[str, Any],
    ) -> list[str]:
        selected_phase = self._get_selected_phase_context(
            context=context,
            pending_data=context.pending_data if isinstance(context.pending_data, dict) else {},
            entities={},
        )
        phase_id = self._normalize_phase_identifier(selected_phase.get("selected_phase_id"))
        if not phase_id:
            return []
        service_catalog = capabilities_summary.get("service_catalog", [])
        if not isinstance(service_catalog, list):
            return []
        names: list[str] = []
        for service in service_catalog:
            if not isinstance(service, dict):
                continue
            if not bool(service.get("is_active", True)):
                continue
            service_phase_id = self._normalize_phase_identifier(service.get("phase_id"))
            if service_phase_id != phase_id:
                continue
            name = str(service.get("name") or service.get("id") or "").strip()
            if not name:
                continue
            names.append(name)
            if len(names) >= 6:
                break
        return names

    @staticmethod
    def _looks_like_phase_unavailable_response(response_text: str) -> bool:
        text = re.sub(r"\s+", " ", str(response_text or "").strip().lower())
        if not text:
            return False
        markers = (
            "not available for",
            "available in during stay phase",
            "available in pre checkin phase",
            "available in post checkout phase",
            "available after check-in",
            "available after check in",
            "cannot be pre-booked",
            "can't be pre-booked",
            "can only be requested after check-in",
            "action requests are available only for",
        )
        return any(marker in text for marker in markers)

    def _is_suggested_action_phase_allowed(
        self,
        *,
        action_text: str,
        pending_data: dict[str, Any],
        capabilities_summary: dict[str, Any],
    ) -> bool:
        phase_id = self._extract_phase_from_pending_data(pending_data)
        if not phase_id:
            return True
        inferred_intent = self._infer_phase_gate_transactional_intent(action_text)
        if inferred_intent is None:
            return True

        service_catalog = capabilities_summary.get("service_catalog", [])
        if not isinstance(service_catalog, list):
            return True
        phase_services = [
            service
            for service in service_catalog
            if isinstance(service, dict)
            and bool(service.get("is_active", True))
            and self._normalize_phase_identifier(service.get("phase_id")) == phase_id
        ]
        if not phase_services:
            return True
        requested_label = self._extract_requested_service_label_for_phase_gate(action_text)
        if requested_label:
            return self._phase_services_include_requested_label(
                requested_label=requested_label,
                phase_services=phase_services,
            )
        has_spa_marker = self._has_spa_booking_marker(action_text)
        has_transport_marker = self._has_transport_booking_marker(action_text)
        room_only_booking = (
            self._looks_like_room_stay_booking_request(action_text)
            and not has_spa_marker
            and not has_transport_marker
        )
        if (
            inferred_intent == IntentType.TABLE_BOOKING
            and room_only_booking
        ):
            return self._phase_has_room_booking_support(phase_services)
        return self._phase_has_intent_compatible_service(
            intent=inferred_intent,
            phase_services=phase_services,
            strict=True,
        )

    @staticmethod
    def _normalize_phase_identifier(value: Any) -> str:
        normalized = str(value or "").strip().lower().replace(" ", "_")
        aliases = {
            "prebooking": "pre_booking",
            "booking": "pre_checkin",
            "precheckin": "pre_checkin",
            "duringstay": "during_stay",
            "instay": "during_stay",
            "in_stay": "during_stay",
            "postcheckout": "post_checkout",
        }
        return aliases.get(normalized, normalized)

    @classmethod
    def _phase_label(cls, phase_id: str) -> str:
        normalized = cls._normalize_phase_identifier(phase_id)
        if not normalized:
            return "Current"
        try:
            phases = config_service.get_journey_phases()
        except Exception:
            phases = []
        if isinstance(phases, list):
            for phase in phases:
                if not isinstance(phase, dict):
                    continue
                candidate_id = cls._normalize_phase_identifier(phase.get("id"))
                if candidate_id == normalized:
                    name = str(phase.get("name") or "").strip()
                    if name:
                        return name
        return normalized.replace("_", " ").title()

    @classmethod
    def _phase_transition_timing_hint(
        cls,
        *,
        current_phase_id: str,
        service_phase_id: str,
    ) -> str:
        current_norm = cls._normalize_phase_identifier(current_phase_id)
        service_norm = cls._normalize_phase_identifier(service_phase_id)
        if not current_norm or not service_norm or current_norm == service_norm:
            return ""

        if current_norm == "pre_booking":
            if service_norm == "pre_checkin":
                return "after your booking is confirmed"
            if service_norm in {"during_stay", "post_checkout"}:
                return "after check-in"
        if current_norm == "pre_checkin":
            if service_norm == "during_stay":
                return "once you check in"
            if service_norm == "post_checkout":
                return "after checkout"
        if current_norm == "during_stay" and service_norm == "post_checkout":
            return "after checkout"
        return ""

    @classmethod
    def _build_phase_gate_sales_response(
        cls,
        *,
        service_name: str,
        current_phase_id: str,
        current_phase_name: str,
        service_phase_id: str = "",
        service_phase_name: str = "",
        available_names: list[str] | None = None,
    ) -> str:
        service_label = str(service_name or "").strip() or "This service"
        response_parts = [f"{service_label} is not available for {current_phase_name} phase."]

        if service_phase_name:
            response_parts.append(f"It is available in {service_phase_name} phase.")
            timing_hint = cls._phase_transition_timing_hint(
                current_phase_id=current_phase_id,
                service_phase_id=service_phase_id,
            )
            if timing_hint:
                response_parts.append(
                    f"Great news: we do offer {service_label} at the hotel, and you can request it {timing_hint}."
                )
            else:
                response_parts.append(f"Great news: we do offer {service_label} at the hotel.")

        curated_names = [str(name).strip() for name in (available_names or []) if str(name).strip()]
        curated_names = curated_names[:4]
        if curated_names:
            response_parts.append(f"Right now, I can help you with {', '.join(curated_names)}.")
        return " ".join(response_parts).strip()

    def _extract_phase_from_pending_data(self, pending_data: dict[str, Any] | None) -> str:
        pending = pending_data if isinstance(pending_data, dict) else {}
        integration = pending.get("_integration", {})
        integration_phase = ""
        if isinstance(integration, dict):
            integration_phase = self._normalize_phase_identifier(integration.get("phase"))
        if integration_phase:
            return integration_phase
        return self._normalize_phase_identifier(pending.get("phase"))

    def _get_selected_phase_context(
        self,
        *,
        context: ConversationContext,
        pending_data: dict[str, Any] | None = None,
        entities: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        phase_id = self._resolve_current_ticketing_phase(
            context=context,
            pending_data=pending_data,
            entities=entities,
        )
        if not phase_id:
            return {"selected_phase_id": "", "selected_phase_name": ""}
        return {
            "selected_phase_id": phase_id,
            "selected_phase_name": self._phase_label(phase_id),
        }

    def _phase_service_names_for_context(
        self,
        *,
        context: ConversationContext,
        pending_data: dict[str, Any] | None = None,
        entities: dict[str, Any] | None = None,
        limit: int = 5,
    ) -> list[str]:
        try:
            services = config_service.get_services()
        except Exception:
            services = []
        if not isinstance(services, list):
            return []
        current_phase = self._resolve_current_ticketing_phase(
            context=context,
            pending_data=pending_data,
            entities=entities,
        )
        names: list[str] = []
        seen_names: set[str] = set()
        for service in services:
            if not isinstance(service, dict):
                continue
            if not bool(service.get("is_active", True)):
                continue
            if self._normalize_phase_identifier(service.get("phase_id")) != current_phase:
                continue
            name = str(service.get("name") or service.get("id") or "").strip()
            if not name:
                continue
            key = name.lower()
            if key in seen_names:
                continue
            seen_names.add(key)
            names.append(name)
            if len(names) >= max(1, int(limit)):
                break
        return names

    def _match_phase_service_from_rows(
        self,
        *,
        message: str,
        services: list[dict[str, Any]],
        min_score: float = 0.72,
    ) -> dict[str, Any] | None:
        text = self._normalize_message_for_routing(message)
        if not text or not isinstance(services, list):
            return None
        tokens = re.findall(r"[a-z0-9]+", text)
        if not tokens:
            return None

        best_service: dict[str, Any] | None = None
        best_score = 0.0
        for service in services:
            if not isinstance(service, dict):
                continue
            if not bool(service.get("is_active", True)):
                continue
            phase_id = self._normalize_phase_identifier(service.get("phase_id"))
            if not phase_id:
                continue
            score = 0.0
            for alias in self._service_aliases_for_matching(service):
                score = max(score, self._service_alias_match_score(text, tokens, alias))
            if score > best_score:
                best_score = score
                best_service = service

        if best_service is None or best_score < float(min_score):
            return None

        result = {
            "service_id": str(best_service.get("id") or "").strip(),
            "service_name": str(best_service.get("name") or best_service.get("id") or "This service").strip(),
            "phase_id": self._normalize_phase_identifier(best_service.get("phase_id")),
            "score": best_score,
        }
        if "ticketing_enabled" in best_service:
            result["ticketing_enabled"] = best_service.get("ticketing_enabled")
        return result

    @staticmethod
    def _extract_requested_service_label_for_phase_gate(message: str) -> str:
        text = str(message or "").strip().lower()
        if not text:
            return ""

        at_match = re.search(r"\b(?:table|booking|reservation|book)\b[^.?!]{0,40}\bat\s+([a-z][a-z0-9&' -]{2,40})", text)
        if at_match:
            candidate = at_match.group(1)
        else:
            candidate = ""

        if not candidate:
            request_match = re.search(
                r"\b(?:book|reserve|order|arrange|need|want|request|schedule)\b\s+(?:a|an|the|for)?\s*([a-z][a-z0-9&'/-]{2,48})",
                text,
            )
            if request_match:
                candidate = request_match.group(1)

        if not candidate:
            return ""

        candidate = re.split(
            r"\b(?:for|on|to|from|in|by|with|today|tomorrow|tonight|pm|am)\b",
            candidate,
            maxsplit=1,
        )[0]
        candidate = re.sub(r"\s+", " ", candidate).strip(" .,!?:;")
        if not candidate:
            return ""

        generic = {
            "table",
            "booking",
            "reservation",
            "service",
            "services",
            "food",
            "room",
            "restaurant",
            "transport",
        }
        tokens = [token for token in re.findall(r"[a-z0-9]+", candidate) if token]
        tokens = [token for token in tokens if token not in generic]
        if not tokens:
            return ""

        return " ".join(tokens).title()

    @staticmethod
    def _phase_gate_intent_label(intent: IntentType) -> str:
        mapping = {
            IntentType.ORDER_FOOD: "Food ordering",
            IntentType.TABLE_BOOKING: "Booking",
            IntentType.ROOM_SERVICE: "Room service",
        }
        return mapping.get(intent, "This service")

    @staticmethod
    def _infer_phase_gate_transactional_intent(message: str) -> IntentType | None:
        text = str(message or "").strip().lower()
        if not text:
            return None

        def _has_marker(markers: tuple[str, ...]) -> bool:
            for marker in markers:
                pattern = rf"\b{re.escape(marker)}\b"
                if re.search(pattern, text):
                    return True
            return False

        if ChatService._looks_like_room_stay_booking_request(text):
            return IntentType.TABLE_BOOKING

        room_markers = ("room service", "housekeeping", "maintenance", "laundry", "amenity")
        if _has_marker(room_markers):
            return IntentType.ROOM_SERVICE

        food_markers = ("hungry", "food", "dining", "menu", "meal", "eat", "in-room dining")
        if _has_marker(food_markers):
            return IntentType.ORDER_FOOD

        booking_markers = (
            "new booking",
            "pre booking",
            "pre-booking",
            "spa",
            "treatment",
            "table",
            "reservation",
            "restaurant",
            "recreation",
            "room booking",
            "room discovery",
            "stay booking",
            "book a room",
            "book room",
            "transport",
            "cab",
            "taxi",
            "pickup",
            "drop",
            "airport transfer",
            "ride",
            "shuttle",
        )
        if _has_marker(booking_markers):
            return IntentType.TABLE_BOOKING
        return None

    @staticmethod
    def _infer_all_phase_gate_transactional_intents(message: str) -> list[IntentType]:
        text = str(message or "").strip().lower()
        if not text:
            return []

        def _has_marker(markers: tuple[str, ...]) -> bool:
            for marker in markers:
                pattern = rf"\b{re.escape(marker)}\b"
                if re.search(pattern, text):
                    return True
            return False

        intents: list[IntentType] = []
        if ChatService._looks_like_room_stay_booking_request(text):
            intents.append(IntentType.TABLE_BOOKING)
        room_markers = ("room service", "housekeeping", "maintenance", "laundry", "amenity")
        food_markers = ("hungry", "food", "dining", "menu", "meal", "eat", "in-room dining")
        booking_markers = (
            "new booking",
            "pre booking",
            "pre-booking",
            "spa",
            "treatment",
            "table",
            "reservation",
            "restaurant",
            "recreation",
            "room booking",
            "room discovery",
            "stay booking",
            "book a room",
            "book room",
            "transport",
            "cab",
            "taxi",
            "pickup",
            "drop",
            "airport transfer",
            "ride",
            "shuttle",
        )

        if _has_marker(booking_markers):
            intents.append(IntentType.TABLE_BOOKING)
        if _has_marker(food_markers):
            intents.append(IntentType.ORDER_FOOD)
        if _has_marker(room_markers):
            intents.append(IntentType.ROOM_SERVICE)
        return intents

    @staticmethod
    def _service_phase_gate_text_blob(service: dict[str, Any]) -> str:
        if not isinstance(service, dict):
            return ""
        fields = (
            service.get("id"),
            service.get("name"),
            service.get("type"),
            service.get("description"),
            service.get("cuisine"),
        )
        return " ".join(str(field or "").strip().lower() for field in fields if str(field or "").strip())

    def _phase_has_intent_compatible_service(
        self,
        *,
        intent: IntentType,
        phase_services: list[dict[str, Any]],
        strict: bool = False,
    ) -> bool:
        marker_map = _PHASE_INTENT_SERVICE_MARKERS_STRICT if strict else _PHASE_INTENT_SERVICE_MARKERS
        markers = marker_map.get(intent, ())
        if not markers:
            return False
        for service in phase_services:
            text = self._service_phase_gate_text_blob(service)
            if not text:
                continue
            for marker in markers:
                if re.search(rf"\b{re.escape(marker)}\b", text):
                    return True
        return False

    def _phase_services_include_requested_label(
        self,
        *,
        requested_label: str,
        phase_services: list[dict[str, Any]],
    ) -> bool:
        label = str(requested_label or "").strip().lower()
        if not label:
            return False
        label_tokens = [token for token in re.findall(r"[a-z0-9]+", label) if len(token) >= 3]
        for service in phase_services:
            text = self._service_phase_gate_text_blob(service)
            if not text:
                continue
            if label in text:
                return True
            if label_tokens and any(token in text for token in label_tokens):
                return True
        return False

    def _phase_gate_additional_unavailable_lines(
        self,
        *,
        message: str,
        current_phase_id: str,
        all_phase_services: list[dict[str, Any]],
        phase_services: list[dict[str, Any]],
        primary_service_name: str = "",
    ) -> list[str]:
        inferred_intents = self._infer_all_phase_gate_transactional_intents(message)
        if not inferred_intents:
            return []

        current_phase_name = self._phase_label(current_phase_id)
        primary_norm = str(primary_service_name or "").strip().lower()
        has_spa_marker = self._has_spa_booking_marker(message)
        has_transport_marker = self._has_transport_booking_marker(message)
        room_only_booking = (
            self._looks_like_room_stay_booking_request(message)
            and not has_spa_marker
            and not has_transport_marker
        )
        lines: list[str] = []
        seen_keys: set[str] = set()
        for inferred_intent in inferred_intents:
            if inferred_intent == IntentType.TABLE_BOOKING:
                if has_spa_marker:
                    if self._phase_has_booking_subtype_support(
                        phase_services=phase_services,
                        subtype="spa",
                    ):
                        continue
                elif has_transport_marker:
                    if self._phase_has_booking_subtype_support(
                        phase_services=phase_services,
                        subtype="transport",
                    ):
                        continue
                elif room_only_booking:
                    if self._phase_has_room_booking_support(phase_services):
                        continue
                elif self._phase_has_intent_compatible_service(
                    intent=inferred_intent,
                    phase_services=phase_services,
                ):
                    continue
            elif self._phase_has_intent_compatible_service(
                intent=inferred_intent,
                phase_services=phase_services,
            ):
                continue
            fallback_service = self._find_intent_compatible_service(
                intent=inferred_intent,
                services=all_phase_services,
                exclude_phase_id=current_phase_id,
            )
            label = self._phase_gate_intent_label(inferred_intent)
            if fallback_service is None:
                key = f"{label.lower()}::{current_phase_id}"
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                lines.append(f"{label} is not available for {current_phase_name} phase.")
                continue

            service_name = str(fallback_service.get("name") or fallback_service.get("id") or label).strip() or label
            if primary_norm and service_name.lower() == primary_norm:
                continue
            service_phase_id = self._normalize_phase_identifier(fallback_service.get("phase_id"))
            service_phase_name = self._phase_label(service_phase_id)
            timing_hint = self._phase_transition_timing_hint(
                current_phase_id=current_phase_id,
                service_phase_id=service_phase_id,
            )
            unavailable_key = f"{service_name.lower()}::not_available::{current_phase_id}"
            if unavailable_key not in seen_keys:
                seen_keys.add(unavailable_key)
                lines.append(f"{service_name} is not available for {current_phase_name} phase.")
            key = f"{service_name.lower()}::{service_phase_id}"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            if timing_hint:
                lines.append(
                    f"{service_name} is available in {service_phase_name} phase, and you can request it {timing_hint}."
                )
            else:
                lines.append(f"{service_name} is available in {service_phase_name} phase.")
        return lines

    def _match_phase_managed_service_request(self, message: str) -> dict[str, Any] | None:
        text = self._normalize_message_for_routing(message)
        if not text:
            return None

        query_markers = (
            "book",
            "reserve",
            "order",
            "arrange",
            "need",
            "request",
            "want",
            "wanna",
            "cancel",
            "modify",
            "status",
            "what",
            "when",
            "where",
            "show",
            "list",
            "details",
            "available",
            "have",
            "provide",
            "offer",
            "services",
            "support",
            "help",
        )
        if not any(marker in text for marker in query_markers):
            return None

        tokens = re.findall(r"[a-z0-9]+", text)
        if not tokens:
            return None

        try:
            services = config_service.get_services()
        except Exception:
            services = []
        if not isinstance(services, list):
            return None

        return self._match_phase_service_from_rows(
            message=message,
            services=services,
            min_score=0.82,
        )

    def _detect_phase_service_unavailable_for_intent(
        self,
        *,
        message: str,
        intent: IntentType,
        context: ConversationContext,
        pending_data: dict[str, Any] | None = None,
        entities: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """
        Enforce phase-specific service availability for transactional requests.
        If no matching active service exists in current phase, decline politely.
        """
        transactional_intents = {
            IntentType.TABLE_BOOKING,
            IntentType.ORDER_FOOD,
            IntentType.ROOM_SERVICE,
        }
        is_transactional = intent in transactional_intents
        is_action_request = self._is_action_request_text(message)
        is_faq_action = intent in {IntentType.FAQ, IntentType.MENU_REQUEST} and is_action_request
        is_faq_operational_issue = (
            intent in {IntentType.FAQ, IntentType.MENU_REQUEST}
            and self._looks_like_operational_issue_for_ticketing(message)
        )
        is_ticketing_like = intent in {IntentType.COMPLAINT, IntentType.HUMAN_REQUEST}
        ticketing_intent_hint = self._infer_phase_gate_ticketing_intent(
            message=message,
            intent=intent,
        )
        if not is_transactional and not is_faq_action and not is_faq_operational_issue and not (
            is_ticketing_like and ticketing_intent_hint is not None
        ):
            return None

        if context.pending_action in {
            "confirm_order",
            "confirm_booking",
            "confirm_room_booking",
            "confirm_room_availability_check",
            "confirm_service_request",
            "select_service",
            "select_restaurant",
            "collect_booking_party_size",
            "collect_booking_time",
            "collect_room_booking_details",
            "select_room_type",
            "collect_transport_details",
        }:
            return None

        try:
            services = config_service.get_services()
        except Exception:
            services = []
        if not isinstance(services, list):
            services = []

        all_phase_services = [
            service
            for service in services
            if isinstance(service, dict)
            and bool(service.get("is_active", True))
            and self._normalize_phase_identifier(service.get("phase_id"))
        ]
        if not all_phase_services:
            return None

        current_phase_id = self._resolve_current_ticketing_phase(
            context=context,
            pending_data=pending_data,
            entities=entities,
        )
        if not current_phase_id:
            return None

        phase_services = [
            service
            for service in all_phase_services
            if self._normalize_phase_identifier(service.get("phase_id")) == current_phase_id
        ]
        is_room_booking_request = self._looks_like_room_stay_booking_request(message)
        has_spa_marker = self._has_spa_booking_marker(message)
        has_transport_marker = self._has_transport_booking_marker(message)
        room_only_booking = (
            is_room_booking_request
            and not has_spa_marker
            and not has_transport_marker
        )
        phase_supports_room_booking = self._phase_has_room_booking_support(phase_services)

        resolved_signal_service = self._resolve_service_from_phase_signals(
            services=all_phase_services,
            entities=entities if isinstance(entities, dict) else {},
            pending_data=pending_data if isinstance(pending_data, dict) else {},
        )
        llm_only_phase_gate = bool(getattr(settings, "chat_phase_gate_llm_only", True))
        if llm_only_phase_gate and resolved_signal_service is not None:
            signal_service_phase = self._normalize_phase_identifier(resolved_signal_service.get("phase_id"))
            if not signal_service_phase:
                return None
            if signal_service_phase == current_phase_id:
                additional_lines = self._phase_gate_additional_unavailable_lines(
                    message=message,
                    current_phase_id=current_phase_id,
                    all_phase_services=all_phase_services,
                    phase_services=phase_services,
                    primary_service_name=str(resolved_signal_service.get("service_name") or ""),
                )
                if not additional_lines:
                    return None
                current_phase_name = self._phase_label(current_phase_id)
                available_names = [
                    str(service.get("name") or "").strip()
                    for service in phase_services
                    if isinstance(service, dict) and str(service.get("name") or "").strip()
                ]
                response_parts = list(additional_lines)
                curated_names = [name for name in available_names if name][:4]
                if curated_names:
                    response_parts.append(f"Right now, I can help you with {', '.join(curated_names)}.")
                response_text = self._dedupe_response_sentences(
                    " ".join(part for part in response_parts if str(part or "").strip()).strip()
                )
                return {
                    "service_id": str(resolved_signal_service.get("service_id") or "").strip(),
                    "service_name": str(resolved_signal_service.get("service_name") or "").strip(),
                    "current_phase_id": current_phase_id,
                    "current_phase_name": current_phase_name,
                    "service_phase_id": current_phase_id,
                    "service_phase_name": current_phase_name,
                    "response_text": response_text,
                    "suggested_actions": list(curated_names[:3]) + ["Show available services", "Ask another question"],
                    "phase_service_unavailable": True,
                }
            if intent == IntentType.TABLE_BOOKING and room_only_booking and phase_supports_room_booking:
                return None
            service_name = str(resolved_signal_service.get("service_name") or "This service").strip()
            current_phase_name = self._phase_label(current_phase_id)
            service_phase_name = self._phase_label(signal_service_phase)
            available_names = [
                str(service.get("name") or "").strip()
                for service in phase_services
                if isinstance(service, dict) and str(service.get("name") or "").strip()
            ]
            return {
                "service_id": str(resolved_signal_service.get("service_id") or "").strip(),
                "service_name": service_name,
                "current_phase_id": current_phase_id,
                "current_phase_name": current_phase_name,
                "service_phase_id": signal_service_phase,
                "service_phase_name": service_phase_name,
                "response_text": self._build_phase_gate_sales_response(
                    service_name=service_name,
                    current_phase_id=current_phase_id,
                    current_phase_name=current_phase_name,
                    service_phase_id=signal_service_phase,
                    service_phase_name=service_phase_name,
                    available_names=available_names,
                ),
                "suggested_actions": available_names[:3] + ["Show available services", "Ask another question"],
                "phase_service_unavailable": True,
            }
        if resolved_signal_service is not None:
            signal_service_phase = self._normalize_phase_identifier(resolved_signal_service.get("phase_id"))
            if signal_service_phase == current_phase_id:
                return None
            if intent == IntentType.TABLE_BOOKING and room_only_booking and phase_supports_room_booking:
                return None
            service_name = str(resolved_signal_service.get("service_name") or "This service").strip()
            current_phase_name = self._phase_label(current_phase_id)
            service_phase_name = self._phase_label(signal_service_phase)
            available_names = [
                str(service.get("name") or "").strip()
                for service in phase_services
                if isinstance(service, dict) and str(service.get("name") or "").strip()
            ]
            result = {
                "service_id": str(resolved_signal_service.get("service_id") or "").strip(),
                "service_name": service_name,
                "current_phase_id": current_phase_id,
                "current_phase_name": current_phase_name,
                "service_phase_id": signal_service_phase,
                "service_phase_name": service_phase_name,
                "response_text": self._build_phase_gate_sales_response(
                    service_name=service_name,
                    current_phase_id=current_phase_id,
                    current_phase_name=current_phase_name,
                    service_phase_id=signal_service_phase,
                    service_phase_name=service_phase_name,
                    available_names=available_names,
                ),
                "suggested_actions": available_names[:3] + ["Show available services", "Ask another question"],
            }
            return result

        if (
            intent == IntentType.TABLE_BOOKING
            and self._looks_like_room_booking_detail_followup(message)
            and self._recent_user_turn_mentions_room_booking(context, message)
            and self._phase_has_room_booking_support(phase_services)
        ):
            return None

        matched_current_phase_service = None
        if not (is_ticketing_like and ticketing_intent_hint is not None):
            matched_current_phase_service = self._match_phase_service_from_rows(
                message=message,
                services=phase_services,
                min_score=0.70,
            )
        if matched_current_phase_service is not None:
            additional_lines = self._phase_gate_additional_unavailable_lines(
                message=message,
                current_phase_id=current_phase_id,
                all_phase_services=all_phase_services,
                phase_services=phase_services,
                primary_service_name=str(matched_current_phase_service.get("service_name") or ""),
            )
            if additional_lines:
                current_phase_name = self._phase_label(current_phase_id)
                available_names = [
                    str(service.get("name") or "").strip()
                    for service in phase_services
                    if isinstance(service, dict) and str(service.get("name") or "").strip()
                ]
                response_parts = list(additional_lines)
                curated_names = [name for name in available_names if name][:4]
                if curated_names:
                    response_parts.append(f"Right now, I can help you with {', '.join(curated_names)}.")
                return {
                    "service_id": str(matched_current_phase_service.get("service_id") or "").strip(),
                    "service_name": str(matched_current_phase_service.get("service_name") or "").strip(),
                    "current_phase_id": current_phase_id,
                    "current_phase_name": current_phase_name,
                    "service_phase_id": current_phase_id,
                    "service_phase_name": current_phase_name,
                    "response_text": self._dedupe_response_sentences(
                        " ".join(part for part in response_parts if str(part or "").strip()).strip()
                    ),
                    "suggested_actions": list(curated_names[:3]) + ["Show available services", "Ask another question"],
                    "phase_service_unavailable": True,
                }
            return None

        matched_any_phase_service = None
        if not (is_ticketing_like and ticketing_intent_hint is not None):
            matched_any_phase_service = self._match_phase_service_from_rows(
                message=message,
                services=all_phase_services,
                min_score=0.70,
            )
        if (
            matched_any_phase_service is not None
            and self._normalize_phase_identifier(matched_any_phase_service.get("phase_id")) != current_phase_id
        ):
            if intent == IntentType.TABLE_BOOKING and room_only_booking and phase_supports_room_booking:
                return None
            service_name = str(matched_any_phase_service.get("service_name") or "This service").strip()
            current_phase_name = self._phase_label(current_phase_id)
            service_phase_id = self._normalize_phase_identifier(matched_any_phase_service.get("phase_id"))
            service_phase_name = self._phase_label(service_phase_id)
            available_names = [
                str(service.get("name") or "").strip()
                for service in phase_services
                if isinstance(service, dict) and str(service.get("name") or "").strip()
            ]
            result = {
                "service_id": str(matched_any_phase_service.get("service_id") or "").strip(),
                "service_name": service_name,
                "current_phase_id": current_phase_id,
                "current_phase_name": current_phase_name,
                "service_phase_id": service_phase_id,
                "service_phase_name": service_phase_name,
                "response_text": self._build_phase_gate_sales_response(
                    service_name=service_name,
                    current_phase_id=current_phase_id,
                    current_phase_name=current_phase_name,
                    service_phase_id=service_phase_id,
                    service_phase_name=service_phase_name,
                    available_names=available_names,
                ),
                "suggested_actions": [
                    str(service.get("name") or "").strip()
                    for service in phase_services
                    if isinstance(service, dict) and str(service.get("name") or "").strip()
                ][:3] + ["Show available services", "Ask another question"],
            }
            additional_lines = self._phase_gate_additional_unavailable_lines(
                message=message,
                current_phase_id=current_phase_id,
                all_phase_services=all_phase_services,
                phase_services=phase_services,
                primary_service_name=service_name,
            )
            if additional_lines:
                result["response_text"] = (
                    f"{str(result.get('response_text') or '').strip()} {' '.join(additional_lines)}"
                ).strip()
            result["response_text"] = self._dedupe_response_sentences(
                str(result.get("response_text") or "").strip()
            )
            return result

        inferred_transactional_intent = ticketing_intent_hint or intent
        requested_label = self._extract_requested_service_label_for_phase_gate(message)
        if not is_transactional:
            inferred = ticketing_intent_hint or self._infer_phase_gate_transactional_intent(message)
            if inferred is None:
                # For FAQ/MENU action-like text, if no known transactional service intent can
                # be inferred, let normal capability/validator flows handle unsupported requests.
                return None
            inferred_transactional_intent = inferred
            if (
                inferred_transactional_intent == IntentType.TABLE_BOOKING
                and room_only_booking
                and phase_supports_room_booking
            ):
                return None
            if not requested_label and self._phase_has_intent_compatible_service(
                intent=inferred_transactional_intent,
                phase_services=phase_services,
                strict=True,
            ):
                strict_match = self._match_phase_service_from_rows(
                    message=message,
                    services=phase_services,
                    min_score=0.72,
                )
                if strict_match is not None:
                    return None

        if requested_label and self._phase_services_include_requested_label(
            requested_label=requested_label,
            phase_services=phase_services,
        ):
            return None
        if is_transactional:
            if (
                intent == IntentType.TABLE_BOOKING
                and room_only_booking
                and phase_supports_room_booking
            ):
                return None
            has_compatible_service = self._phase_has_intent_compatible_service(
                intent=intent,
                phase_services=phase_services,
            )
            if (
                has_compatible_service
                and not requested_label
                and intent in {IntentType.ORDER_FOOD, IntentType.ROOM_SERVICE}
            ):
                return None

        current_phase_name = self._phase_label(current_phase_id)
        service_label = requested_label or self._phase_gate_intent_label(inferred_transactional_intent)
        available_names = [
            str(service.get("name") or "").strip()
            for service in phase_services
            if isinstance(service, dict) and str(service.get("name") or "").strip()
        ]
        available_names = [name for name in available_names if name][:4]
        fallback_service = self._find_intent_compatible_service(
            intent=inferred_transactional_intent,
            services=all_phase_services,
            exclude_phase_id=current_phase_id,
        )
        fallback_phase_id = self._normalize_phase_identifier(fallback_service.get("phase_id")) if isinstance(fallback_service, dict) else ""
        fallback_phase_name = self._phase_label(fallback_phase_id) if fallback_phase_id else ""
        response_text = self._build_phase_gate_sales_response(
            service_name=service_label,
            current_phase_id=current_phase_id,
            current_phase_name=current_phase_name,
            service_phase_id=fallback_phase_id,
            service_phase_name=fallback_phase_name,
            available_names=available_names,
        )
        additional_lines = self._phase_gate_additional_unavailable_lines(
            message=message,
            current_phase_id=current_phase_id,
            all_phase_services=all_phase_services,
            phase_services=phase_services,
            primary_service_name=service_label,
        )
        if additional_lines:
            response_text = f"{response_text} {' '.join(additional_lines)}".strip()
        response_text = self._dedupe_response_sentences(response_text) or str(response_text or "").strip()

        suggestions = list(available_names[:3])
        suggestions.extend(["Show available services", "Ask another question"])
        return {
            "service_id": "",
            "service_name": service_label,
            "current_phase_id": current_phase_id,
            "current_phase_name": current_phase_name,
            "service_phase_id": "",
            "service_phase_name": "",
            "response_text": response_text,
            "suggested_actions": suggestions,
            "phase_service_unavailable": True,
        }

    def _phase_services_for_id(
        self,
        *,
        capabilities_summary: dict[str, Any],
        phase_id: str,
        limit: int = 6,
    ) -> list[str]:
        normalized_phase = self._normalize_phase_identifier(phase_id)
        if not normalized_phase:
            return []
        services = capabilities_summary.get("service_catalog", [])
        if not isinstance(services, list):
            return []
        names: list[str] = []
        for service in services:
            if not isinstance(service, dict):
                continue
            if not bool(service.get("is_active", True)):
                continue
            service_phase_id = self._normalize_phase_identifier(service.get("phase_id"))
            if service_phase_id != normalized_phase:
                continue
            service_name = str(service.get("name") or service.get("id") or "").strip()
            if not service_name:
                continue
            if service_name in names:
                continue
            names.append(service_name)
            if len(names) >= max(1, int(limit or 6)):
                break
        return names

    @staticmethod
    def _derive_phase_from_dates(check_in: Any, check_out: Any) -> str:
        """Derive the guest journey phase from stay dates vs today.

        pre_checkin (arrival is still in the future) / during_stay (today falls
        within the stay) / post_checkout (the stay is already over). Returns ''
        when the dates cannot be parsed, so the caller-supplied phase is kept.
        """
        from datetime import date as _date

        def _parse(value: Any) -> "date | None":
            token = str(value or "").strip()[:10]
            if not token:
                return None
            try:
                return _date.fromisoformat(token)
            except Exception:
                return None

        ci = _parse(check_in)
        co = _parse(check_out)
        today = _date.today()
        if ci and ci > today:
            return "pre_checkin"
        if ci and co and ci <= today <= co:
            return "during_stay"
        if co and co < today:
            return "post_checkout"
        if ci and ci <= today and not co:
            return "during_stay"
        return ""
