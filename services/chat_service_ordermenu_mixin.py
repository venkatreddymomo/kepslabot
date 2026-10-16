"""ChatServiceOrderMenuMixin: methods extracted from chat_service.py (behavior-preserving).

Moved verbatim (22 methods). ChatService inherits this mixin, so ``self.*``
resolution is unchanged. This module never imports chat_service — no cycle.
"""
import json
import logging
import re
from difflib import SequenceMatcher
from typing import Any, Optional
from pydantic import BaseModel, Field

from schemas.chat import (
    ConversationContext,
    ConversationState,
    IntentType,
    MessageRole,
)
from llm.client import llm_client
from config.settings import settings

logger = logging.getLogger(__name__)


# Shared with the ChatService domain mixins — housed in chat_service_shared
# to avoid an import cycle (the mixins import them from there, not from here).
from services.chat_service_shared import (
    _coerce_optional_int,
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








logger = logging.getLogger("services.chat_service")




class ChatServiceOrderMenuMixin:
    @staticmethod
    def _is_order_pending_action(action: str | None) -> bool:
        normalized = str(action or "").strip().lower()
        return normalized in {
            "order_food",
            "collect_order_item",
            "collect_order_quantity",
            "collect_order_addons",
            "confirm_order",
        }

    @staticmethod
    def _is_order_flow(
        raw_intent: str,
        current_pending_action: str | None,
        pending_action_target: str | None,
        pending_data: dict[str, Any],
        response_text: str,
        intent: IntentType,
    ) -> bool:
        current = str(current_pending_action or "").strip().lower()
        target = str(pending_action_target or "").strip().lower()
        raw = str(raw_intent or "").strip().lower()
        combined = " ".join(
            part for part in (
                current,
                target,
                raw,
                str(intent.value if isinstance(intent, IntentType) else intent).lower(),
                str(response_text or "").lower(),
                " ".join(str(k).lower() for k in (pending_data or {}).keys()),
            )
            if part
        )
        if "order_food" in combined:
            return True
        if "confirm_order" in combined or "collect_order_" in combined:
            return True
        if "dish" in combined or "menu" in combined:
            return True
        return False

    def _extract_order_slot_updates(
        self,
        message: str,
        response_text: str,
        current_pending_action: str | None,
        pending_data: dict[str, Any],
    ) -> dict[str, Any]:
        updates: dict[str, Any] = {}
        msg = str(message or "").strip()
        msg_lower = msg.lower()
        pending = pending_data if isinstance(pending_data, dict) else {}
        current = str(current_pending_action or "").strip().lower()
        existing_item = self._extract_order_item_name(pending)
        option_candidates = self._extract_order_option_candidates(pending)

        selected_from_response = re.search(
            r"selected(?:\s+the)?\s+['\"]([^'\"]+)['\"]",
            str(response_text or ""),
            flags=re.IGNORECASE,
        )
        if selected_from_response:
            item = str(selected_from_response.group(1) or "").strip()
            if item:
                updates["order_item"] = item

        explicit_item = ""
        if current != "collect_order_addons":
            stripped_yes = self._strip_leading_yes(msg)
            if stripped_yes:
                explicit_item = stripped_yes
            else:
                patterns = (
                    r"\bi want\s+(.+)$",
                    r"\border\s+(.+)$",
                    r"\badd\s+(.+)$",
                    r"\bget me\s+(.+)$",
                )
                for pattern in patterns:
                    match = re.search(pattern, msg, flags=re.IGNORECASE)
                    if match:
                        explicit_item = str(match.group(1) or "").strip()
                        break

            if (
                not explicit_item
                and current in {"collect_order_item", "order_food"}
                and msg
                and not self._looks_like_information_query(msg_lower)
                and "?" not in msg
            ):
                explicit_item = msg

        if explicit_item:
            cleaned_item = explicit_item.strip(" .")
            cleaned_item = re.sub(r"^(?:a|an|the)\s+", "", cleaned_item, flags=re.IGNORECASE).strip()
            generic_tokens = {
                "something",
                "anything",
                "food",
                "menu",
                "drink",
                "drinks",
                "yes",
                "no",
                "confirm",
            }
            cleaned_norm = self._normalize_order_item_value(cleaned_item)
            if cleaned_item and cleaned_norm not in generic_tokens and re.search(r"[a-zA-Z]", cleaned_item):
                resolved_candidate_item = self._resolve_order_item_candidate_reference(
                    text=cleaned_item,
                    option_candidates=option_candidates,
                )
                if resolved_candidate_item:
                    updates["order_item"] = resolved_candidate_item
                elif self._is_ambiguous_short_reference_phrase(cleaned_item):
                    if len(option_candidates) == 1:
                        updates["order_item"] = option_candidates[0]
                else:
                    updates["order_item"] = cleaned_item

        guest_match = re.search(r"\bfor\s+(\d{1,2})\s*(?:guests?|people|persons|pax)\b", msg_lower)
        if guest_match:
            try:
                guests = int(guest_match.group(1))
                if 1 <= guests <= 20:
                    updates["order_guest_count"] = guests
            except (TypeError, ValueError):
                pass

        qty_match = re.search(
            r"\b(?:qty|quantity|x)?\s*(\d{1,2})\s*(?:x|qty|quantity|plate|plates|portion|portions|pcs|piece|pieces)?\b",
            msg_lower,
        )
        if qty_match:
            try:
                qty = int(qty_match.group(1))
                if 1 <= qty <= 20:
                    updates["order_quantity"] = qty
            except (TypeError, ValueError):
                pass

        if re.fullmatch(r"\d{1,2}", msg_lower):
            try:
                qty = int(msg_lower)
                if 1 <= qty <= 20:
                    updates["order_quantity"] = qty
            except (TypeError, ValueError):
                pass

        binary = self._detect_simple_binary_reply(msg)
        if current == "collect_order_addons":
            if binary == IntentType.CONFIRMATION_NO or any(
                token in msg_lower for token in ("nothing else", "no more", "that's all", "thats all")
            ):
                updates["order_addons_choice"] = "no"
            elif binary == IntentType.CONFIRMATION_YES:
                extra = self._strip_leading_yes(msg)
                if extra:
                    updates["order_additional_request"] = extra
                    updates["order_addons_choice"] = "yes"
            elif msg and msg_lower not in {"yes", "no", "cancel"}:
                updates["order_additional_request"] = msg
                updates["order_addons_choice"] = "yes"

        if str(updates.get("order_item") or "").strip():
            updates["order_option_candidates"] = []

        # Carry forward previously selected item if model didn't include it.
        if "order_item" not in updates:
            if existing_item:
                updates["order_item"] = existing_item

        return updates

    def _resolve_order_item_candidate_reference(
        self,
        *,
        text: str,
        option_candidates: list[str],
    ) -> str:
        candidates = [str(item).strip() for item in option_candidates if str(item).strip()]
        if not candidates:
            return ""

        normalized_text = self._normalize_order_item_value(text)
        if not normalized_text:
            return ""

        if normalized_text.isdigit():
            try:
                idx = int(normalized_text)
                if 1 <= idx <= len(candidates):
                    return candidates[idx - 1]
            except (TypeError, ValueError):
                pass

        normalized_candidate_map = {
            self._normalize_order_item_value(candidate): candidate
            for candidate in candidates
        }
        if normalized_text in normalized_candidate_map:
            return normalized_candidate_map[normalized_text]

        best_match = ""
        best_score = 0.0
        for candidate in candidates:
            candidate_norm = self._normalize_order_item_value(candidate)
            if not candidate_norm:
                continue
            score = SequenceMatcher(a=normalized_text, b=candidate_norm).ratio()
            if score > best_score:
                best_score = score
                best_match = candidate
        if best_match and best_score >= 0.75:
            return best_match
        return ""

    @staticmethod
    def _extract_order_option_candidates(pending_data: dict[str, Any]) -> list[str]:
        pending = pending_data if isinstance(pending_data, dict) else {}
        raw_candidates = pending.get("order_option_candidates")
        if not isinstance(raw_candidates, list):
            return []
        seen: set[str] = set()
        parsed: list[str] = []
        for entry in raw_candidates:
            text = re.sub(r"\s+", " ", str(entry or "").strip())
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            parsed.append(text)
        return parsed

    @staticmethod
    def _extract_order_option_candidates_from_response(response_text: str) -> list[str]:
        lines = str(response_text or "").splitlines()
        seen: set[str] = set()
        options: list[str] = []
        for line in lines:
            stripped = str(line or "").strip()
            if not stripped.startswith("-"):
                continue
            candidate = stripped.lstrip("-").strip()
            candidate = re.sub(
                r"\s*-\s*(?:rs\.?|₹)\s*[\d.,/()a-zA-Z-]+$",
                "",
                candidate,
                flags=re.IGNORECASE,
            ).strip()
            if not candidate:
                continue
            if not re.search(r"[a-zA-Z]", candidate):
                continue
            key = candidate.lower()
            if key in seen:
                continue
            seen.add(key)
            options.append(candidate)
        return options

    def _extract_order_item_name(self, pending_data: dict[str, Any]) -> str:
        pending = pending_data if isinstance(pending_data, dict) else {}
        return self._first_non_empty(
            pending.get("order_item"),
            pending.get("selected_item"),
            pending.get("item_name"),
            pending.get("dish_name"),
            pending.get("requested_item"),
        )

    def _extract_order_quantity_value(self, pending_data: dict[str, Any]) -> Optional[int]:
        pending = pending_data if isinstance(pending_data, dict) else {}
        raw = self._first_non_empty(
            pending.get("order_quantity"),
            pending.get("quantity"),
        )
        if not raw:
            return None
        try:
            value = int(float(raw))
        except (TypeError, ValueError):
            return None
        if value < 1 or value > 20:
            return None
        return value

    @staticmethod
    def _extract_order_addons_choice(pending_data: dict[str, Any]) -> str:
        pending = pending_data if isinstance(pending_data, dict) else {}
        value = str(pending.get("order_addons_choice") or "").strip().lower()
        if value in {"yes", "no"}:
            return value
        return ""

    def _build_order_quantity_prompt(self, item_name: str) -> str:
        if item_name:
            return (
                f"Great choice: {item_name}. "
                "How many portions would you like? "
                "If this is for a group, you can also share number of guests."
            )
        return "How many portions would you like to order? You can also share number of guests."

    def _build_order_addons_prompt(self, item_name: str, quantity: Optional[int]) -> str:
        item_text = item_name or "this item"
        qty_text = f"{quantity}" if quantity is not None else "1"
        return (
            f"Got it. I have {qty_text} x {item_text}. "
            "Would you like to add anything else or include a drink? (yes/no)"
        )

    def _build_order_review_prompt(self, pending_data: dict[str, Any]) -> str:
        item_name = self._extract_order_item_name(pending_data) or "your selected item"
        quantity = self._extract_order_quantity_value(pending_data)
        quantity_text = f"{quantity}" if quantity is not None else "1"
        extra = str((pending_data or {}).get("order_additional_request") or "").strip()
        if extra:
            summary = f"{quantity_text} x {item_name}, plus {extra}"
        else:
            summary = f"{quantity_text} x {item_name}"
        return f"Please review your order: {summary}. Would you like to confirm this order?"

    @staticmethod
    def _is_order_options_followup(
        message: str,
        item_name: str,
        current_pending_action: str | None,
    ) -> bool:
        pending = str(current_pending_action or "").strip().lower()
        if pending not in {"collect_order_quantity", "collect_order_item", "order_food"}:
            return False

        msg = re.sub(r"\s+", " ", str(message or "").strip().lower())
        if not msg:
            return False

        option_markers = (
            "which",
            "what",
            "show",
            "list",
            "options",
            "available",
            "do you have",
            "do u have",
            "do u hv",
            "more",
        )
        if not any(marker in msg for marker in option_markers):
            return False

        normalized_item = re.sub(r"\s+", " ", str(item_name or "").strip().lower())
        normalized_item = re.sub(r"^(?:a|an|the)\s+", "", normalized_item).strip()
        if normalized_item:
            item_tokens = [token for token in re.findall(r"[a-z0-9]+", normalized_item) if len(token) >= 3]
            if item_tokens and any(token in msg for token in item_tokens):
                return True

        return "options" in msg or "available" in msg or msg.startswith("which ") or msg.startswith("what ")

    @staticmethod
    def _derive_order_category_hint(message: str, item_name: str) -> str:
        normalized_item = re.sub(r"\s+", " ", str(item_name or "").strip().lower())
        normalized_item = re.sub(r"^(?:a|an|the)\s+", "", normalized_item).strip(" .")
        if normalized_item:
            return normalized_item

        msg = re.sub(r"\s+", " ", str(message or "").strip().lower())
        if not msg:
            return ""

        capture_patterns = (
            r"(?:which|what)\s+([a-z][a-z0-9\s-]{2,40})$",
            r"(?:show|list)\s+([a-z][a-z0-9\s-]{2,40})\s+(?:options|items)?$",
            r"(?:options|items)\s+for\s+([a-z][a-z0-9\s-]{2,40})$",
        )
        for pattern in capture_patterns:
            match = re.search(pattern, msg, flags=re.IGNORECASE)
            if not match:
                continue
            hint = re.sub(r"\s+", " ", str(match.group(1) or "").strip().lower())
            hint = re.sub(r"\b(?:options?|items?|menu)\b", "", hint).strip(" -.")
            if hint:
                return hint

        tokens = [token for token in re.findall(r"[a-z0-9]+", msg) if len(token) >= 3]
        if tokens:
            return " ".join(tokens[:2]).strip()
        return ""

    async def _build_order_options_list_response(
        self,
        category_hint: str,
        capabilities_summary: dict[str, Any],
        db_session=None,
        hotel_code: str | None = None,
        selected_phase_id: str = "",
        selected_phase_name: str = "",
    ) -> Optional[str]:
        hint = re.sub(r"\s+", " ", str(category_hint or "").strip().lower())
        hint = re.sub(r"^(?:a|an|the)\s+", "", hint).strip(" .")
        if not hint:
            return None

        option_map: dict[str, str] = {}
        option_display_map: dict[str, str] = {}

        def _add_option(name: str, price_text: str = "") -> None:
            option_name = re.sub(r"\s+", " ", str(name or "").strip())
            if not option_name:
                return
            key = option_name.lower()
            if key in option_map:
                if not option_map[key] and price_text:
                    option_map[key] = price_text
                return
            option_map[key] = price_text
            option_display_map[key] = option_name

        # Primary source: runtime menu DB.
        if db_session is not None:
            from sqlalchemy import or_, select
            from models.database import Hotel, MenuItem, Restaurant

            hotel_id = _coerce_optional_int(capabilities_summary.get("hotel_id"))
            if hotel_id is None:
                hotel_name = str(capabilities_summary.get("hotel_name") or "").strip()
                if hotel_name:
                    hotel_row = (
                        await db_session.execute(
                            select(Hotel).where(Hotel.name.ilike(hotel_name)).limit(1)
                        )
                    ).scalars().first()
                    if hotel_row is not None:
                        hotel_id = hotel_row.id
            if hotel_id is None:
                hotel_row = (
                    await db_session.execute(select(Hotel).where(Hotel.is_active == True).limit(1))  # noqa: E712
                ).scalars().first()
                if hotel_row is not None:
                    hotel_id = hotel_row.id

            if hotel_id is not None:
                stmt = (
                    select(MenuItem)
                    .join(Restaurant, MenuItem.restaurant_id == Restaurant.id)
                    .where(
                        Restaurant.hotel_id == hotel_id,
                        Restaurant.is_active == True,  # noqa: E712
                        MenuItem.is_available == True,  # noqa: E712
                    )
                )

                tokens = [token for token in re.findall(r"[a-z0-9]+", hint) if len(token) >= 3][:5]
                if tokens:
                    clauses = []
                    for token in tokens:
                        like_value = f"%{token}%"
                        clauses.extend(
                            [
                                MenuItem.name.ilike(like_value),
                                MenuItem.category.ilike(like_value),
                                MenuItem.description.ilike(like_value),
                            ]
                        )
                    stmt = stmt.where(or_(*clauses))

                rows = list((await db_session.execute(stmt)).scalars().all())
                for row in sorted(rows, key=lambda item: str(item.name or "").lower()):
                    name = str(row.name or "").strip()
                    if not name:
                        continue
                    price_text = ""
                    try:
                        price_text = f"Rs.{float(row.price):.0f}"
                    except (TypeError, ValueError):
                        price_text = ""
                    _add_option(name, price_text)

        # Secondary source: KB-wide extraction via LLM for broader semantic coverage.
        if len(option_map) < 4 and settings.openai_api_key:
            tenant_id = str(hotel_code or "default").strip() or "default"
            kb_text = await self._load_kb_text_with_db_preference(
                db_session=db_session,
                hotel_code=tenant_id,
                max_chars=90000,
            )
            if kb_text:
                phase_label = str(selected_phase_name or "").strip() or (
                    str(selected_phase_id or "").replace("_", " ").title()
                )
                phase_id = str(selected_phase_id or "").strip() or "unknown"
                llm_json: dict[str, Any] = {}
                try:
                    llm_json = await llm_client.chat_with_json(
                        messages=[
                            {
                                "role": "system",
                                "content": (
                                    "Extract menu options strictly from KB content.\n"
                                    f"Selected user journey phase: {phase_label} ({phase_id}).\n"
                                    "Return complete options for the requested category.\n"
                                    "Include items where category is explicit or clearly implied by item/description/section.\n"
                                    "No invented items. Return only JSON: "
                                    "{\"options\":[{\"name\":\"...\",\"price\":\"...\"}]}"
                                ),
                            },
                            {
                                "role": "user",
                                "content": json.dumps(
                                    {
                                        "category_hint": hint,
                                        "kb_content": kb_text,
                                        "existing_options": list(option_display_map.values()),
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        ],
                        temperature=0.0,
                    )
                except Exception:
                    llm_json = {}

                for name, price in self._extract_option_entries_from_llm_json(llm_json):
                    _add_option(name, price)

        if not option_map:
            return None

        ordered_names = sorted(option_map.keys())
        lines = []
        for key in ordered_names:
            display_name = option_display_map.get(key) or key.title()
            price_text = option_map.get(key) or ""
            if price_text:
                lines.append(f"- {display_name} - {price_text}")
            else:
                lines.append(f"- {display_name}")

        return (
            f"Here are all available {hint} options:\n\n"
            + "\n".join(lines)
            + "\n\nPlease tell me the exact item you'd like to order."
        )

    @staticmethod
    def _normalize_order_item_value(value: str) -> str:
        text = re.sub(r"\s+", " ", str(value or "").strip().lower())
        if not text:
            return ""
        text = re.sub(r"^(?:a|an|the)\s+", "", text).strip()
        text = re.sub(r"[^a-z0-9\s]+", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    async def _is_specific_order_item_from_menu(
        self,
        *,
        item_name: str,
        capabilities_summary: dict[str, Any],
        db_session=None,
    ) -> tuple[bool, bool]:
        normalized_item = self._normalize_order_item_value(item_name)
        if not normalized_item:
            return (False, False)
        if db_session is None:
            return (False, False)

        from sqlalchemy import or_, select
        from models.database import Hotel, MenuItem, Restaurant

        hotel_id = _coerce_optional_int(capabilities_summary.get("hotel_id"))
        if hotel_id is None:
            hotel_name = str(capabilities_summary.get("hotel_name") or "").strip()
            if hotel_name:
                hotel_row = (
                    await db_session.execute(
                        select(Hotel).where(Hotel.name.ilike(hotel_name)).limit(1)
                    )
                ).scalars().first()
                if hotel_row is not None:
                    hotel_id = hotel_row.id
        if hotel_id is None:
            hotel_row = (
                await db_session.execute(select(Hotel).where(Hotel.is_active == True).limit(1))  # noqa: E712
            ).scalars().first()
            if hotel_row is not None:
                hotel_id = hotel_row.id
        if hotel_id is None:
            return (False, False)

        tokens = [token for token in re.findall(r"[a-z0-9]+", normalized_item) if len(token) >= 2][:5]
        stmt = (
            select(MenuItem)
            .join(Restaurant, MenuItem.restaurant_id == Restaurant.id)
            .where(
                Restaurant.hotel_id == hotel_id,
                Restaurant.is_active == True,  # noqa: E712
                MenuItem.is_available == True,  # noqa: E712
            )
        )
        if tokens:
            clauses = []
            for token in tokens:
                like_value = f"%{token}%"
                clauses.extend(
                    [
                        MenuItem.name.ilike(like_value),
                        MenuItem.category.ilike(like_value),
                        MenuItem.description.ilike(like_value),
                    ]
                )
            stmt = stmt.where(or_(*clauses))
        rows = list((await db_session.execute(stmt)).scalars().all())
        if not rows:
            return (False, False)

        candidate_names: list[str] = []
        seen: set[str] = set()
        for row in rows:
            name = str(getattr(row, "name", "") or "").strip()
            if not name:
                continue
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            candidate_names.append(name)
        if not candidate_names:
            return (False, False)

        normalized_candidates = [self._normalize_order_item_value(name) for name in candidate_names]
        if normalized_item in normalized_candidates:
            return (True, True)
        if len(normalized_candidates) == 1 and SequenceMatcher(
            a=normalized_item,
            b=normalized_candidates[0],
        ).ratio() >= 0.9:
            return (True, True)
        return (False, True)

    async def _build_order_item_disambiguation_response(
        self,
        *,
        item_name: str,
        capabilities_summary: dict[str, Any],
        db_session=None,
        hotel_code: str | None = None,
        selected_phase_id: str = "",
        selected_phase_name: str = "",
    ) -> Optional[str]:
        normalized_item = self._normalize_order_item_value(item_name)
        if not normalized_item:
            return None

        item_is_specific, menu_available = await self._is_specific_order_item_from_menu(
            item_name=item_name,
            capabilities_summary=capabilities_summary,
            db_session=db_session,
        )
        if item_is_specific:
            return None

        options_response = await self._build_order_options_list_response(
            category_hint=normalized_item,
            capabilities_summary=capabilities_summary,
            db_session=db_session,
            hotel_code=hotel_code,
            selected_phase_id=selected_phase_id,
            selected_phase_name=selected_phase_name,
        )
        if options_response:
            return options_response
        if menu_available:
            return (
                f"I found multiple menu options for {normalized_item}. "
                "Please share the exact item name from the menu."
            )
        return None

    async def _match_menu_recommendation_response(
        self,
        message: str,
        context: ConversationContext,
        capabilities_summary: dict,
        db_session=None,
    ) -> Optional[dict]:
        """
        Deterministic menu recommendation helper for preference asks.
        """
        if context.pending_action:
            return None
        if context.state not in {ConversationState.IDLE, ConversationState.COMPLETED}:
            return None

        msg_lower = str(message or "").strip().lower()
        if not msg_lower:
            return None

        recommendation_markers = (
            "recommend",
            "suggest",
            "what should i eat",
            "what can i eat",
            "what food do you have",
            "what do you have",
            "show options",
            "show me options",
            "show more options",
            "more options",
            "in mood for",
            "craving",
            "hungry",
        )
        if not any(token in msg_lower for token in recommendation_markers):
            return None

        if db_session is None:
            return None

        prefer_nonveg = bool(
            re.search(r"\b(non[\s-]?veg|chicken|mutton|fish|prawn|meat|seafood)\b", msg_lower)
        )
        prefer_veg = bool(re.search(r"\b(vegetarian|vegan|veg)\b", msg_lower)) and not prefer_nonveg

        from sqlalchemy import select
        from models.database import Hotel, Restaurant, MenuItem

        hotel_id = _coerce_optional_int(capabilities_summary.get("hotel_id"))
        if hotel_id is None:
            hotel_name = str(capabilities_summary.get("hotel_name") or "").strip()
            if hotel_name:
                hotel_row = (
                    await db_session.execute(
                        select(Hotel).where(Hotel.name.ilike(hotel_name)).limit(1)
                    )
                ).scalars().first()
                if hotel_row is not None:
                    hotel_id = hotel_row.id
        if hotel_id is None:
            hotel_row = (
                await db_session.execute(select(Hotel).where(Hotel.is_active == True).limit(1))  # noqa: E712
            ).scalars().first()
            if hotel_row is not None:
                hotel_id = hotel_row.id
        if hotel_id is None:
            return None

        stmt = (
            select(MenuItem)
            .join(Restaurant, MenuItem.restaurant_id == Restaurant.id)
            .where(
                Restaurant.hotel_id == hotel_id,
                Restaurant.is_active == True,  # noqa: E712
                MenuItem.is_available == True,  # noqa: E712
            )
        )
        if prefer_nonveg and not prefer_veg:
            stmt = stmt.where(MenuItem.is_vegetarian == False)  # noqa: E712
        elif prefer_veg and not prefer_nonveg:
            stmt = stmt.where(MenuItem.is_vegetarian == True)  # noqa: E712

        rows = list((await db_session.execute(stmt)).scalars().all())
        if not rows:
            return None

        stopwords = {
            "i",
            "im",
            "in",
            "the",
            "a",
            "an",
            "for",
            "to",
            "my",
            "me",
            "show",
            "more",
            "options",
            "option",
            "want",
            "need",
            "food",
            "eat",
            "drink",
            "please",
            "some",
            "only",
            "now",
            "menu",
            "recommend",
            "suggest",
            "what",
            "which",
            "have",
            "available",
            "mood",
            "hungry",
            "craving",
            "nonveg",
            "non",
            "veg",
            "vegetarian",
            "vegan",
        }

        def extract_focus_terms(text: str) -> list[str]:
            return [
                token
                for token in re.findall(r"[a-z0-9]+", str(text or "").lower())
                if len(token) >= 3 and token not in stopwords
            ]

        focus_terms = extract_focus_terms(msg_lower)
        if any(marker in msg_lower for marker in ("show more options", "more options", "show more")):
            previous_user_messages = [
                str(msg.content or "")
                for msg in context.messages[:-1]
                if msg.role == MessageRole.USER and str(msg.content or "").strip()
            ]
            for previous_text in reversed(previous_user_messages):
                prev_terms = extract_focus_terms(previous_text)
                if prev_terms:
                    focus_terms = prev_terms
                    break

        filtered_rows = rows
        if focus_terms:
            matched_rows = []
            for row in rows:
                haystack = f"{str(row.name or '').lower()} {str(row.category or '').lower()}"
                if any(term in haystack for term in focus_terms):
                    matched_rows.append(row)
            if matched_rows:
                filtered_rows = matched_rows

        deduped_rows = []
        seen_item_names: set[str] = set()
        for item in sorted(filtered_rows, key=lambda entry: str(entry.name or "").lower()):
            item_name = str(item.name or "").strip()
            if not item_name:
                continue
            key = item_name.lower()
            if key in seen_item_names:
                continue
            seen_item_names.add(key)
            deduped_rows.append(item)

        if not deduped_rows:
            return None

        lines = [f"- {item.name} - Rs.{float(item.price):.0f}" for item in deduped_rows]
        if focus_terms:
            preference_label = " ".join(focus_terms[:2]).strip()
        else:
            preference_label = (
                "non-veg" if prefer_nonveg and not prefer_veg else ("veg" if prefer_veg and not prefer_nonveg else "menu")
            )
        return {
            "match_type": "menu_recommendation",
            "response_text": (
                f"Here are all available {preference_label} options:\n\n"
                + "\n".join(lines)
                + "\n\nTell me what you'd like to order."
            ),
        }

    def _match_restaurant_alias_menu_phrase(self, msg_lower: str, context: dict) -> Optional[str]:
        """
        Detect bare restaurant/IRD alias phrases and treat them as menu intent.
        Example: "in room dining", "ird", "ird menu".
        """
        if not isinstance(context, dict):
            return None
        normalized_msg = self._normalize_alias_text(msg_lower)
        if not normalized_msg:
            return None

        transactional_markers = ("book ", "reserve ", "order ", "deliver ", "send ", "confirm ", "cancel ")
        if any(marker in f"{normalized_msg} " for marker in transactional_markers):
            return None

        service_catalog = context.get("service_catalog", [])
        if not isinstance(service_catalog, list):
            return None

        for service in service_catalog:
            if not isinstance(service, dict):
                continue
            if not bool(service.get("is_active", True)):
                continue
            service_type = str(service.get("type") or "").strip().lower()
            if service_type and not any(token in service_type for token in ("restaurant", "dining", "food", "outlet")):
                continue

            aliases = []
            service_name = str(service.get("name") or "").strip()
            service_id = str(service.get("id") or "").strip()
            if service_name:
                aliases.append(service_name)
            if service_id:
                aliases.append(service_id)

            for alias in aliases:
                alias_norm = self._normalize_alias_text(alias)
                if not alias_norm:
                    continue
                if normalized_msg in {alias_norm, f"{alias_norm} menu", f"menu {alias_norm}"}:
                    return service_name or alias

        return None

    @staticmethod
    def _looks_like_food_delivery_order_request(msg_lower: str) -> bool:
        """
        Deterministically catch explicit food-order delivery asks so they don't
        get misrouted to room-service just because a room is mentioned.
        """
        if not msg_lower:
            return False

        order_markers = (
            "order",
            "deliver",
            "delivery",
            "send",
            "to my room",
            "room delivery",
        )
        food_markers = (
            "food",
            "menu",
            "pizza",
            "pasta",
            "biryani",
            "burger",
            "snack",
            "drink",
            "wine",
            "dessert",
            "soup",
            "meal",
            "chicken",
            "veg",
            "nonveg",
            "non-veg",
        )

        has_order_signal = any(marker in msg_lower for marker in order_markers)
        has_hunger_signal = ChatService._has_hunger_signal(msg_lower)
        if not has_order_signal and has_hunger_signal and "room" in msg_lower:
            has_order_signal = True

        has_food_signal = any(marker in msg_lower for marker in food_markers) or has_hunger_signal
        if not (has_order_signal and has_food_signal):
            return False

        # Explicit room-service operational asks should remain room_service.
        operational_markers = ("cleaning", "towel", "laundry", "amenities", "maintenance", "housekeeping")
        if any(marker in msg_lower for marker in operational_markers):
            return False
        return True

    @staticmethod
    def _looks_like_strong_menu_request(msg_lower: str) -> bool:
        if not msg_lower:
            return False
        if any(token in msg_lower for token in ("menu", "catalog", "items list", "offerings")):
            return True
        compact_tokens = [re.sub(r"[^a-z]", "", token) for token in msg_lower.split()]
        compact_tokens = [token for token in compact_tokens if token]
        for token in compact_tokens:
            if SequenceMatcher(a=token, b="menu").ratio() >= 0.8:
                return True
            if SequenceMatcher(a=token, b="catalog").ratio() >= 0.82:
                return True
        return False
