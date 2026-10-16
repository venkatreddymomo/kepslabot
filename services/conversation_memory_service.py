"""
Conversation Memory Service

Maintains long-term conversational memory per session:
1) rolling summary of the full conversation
2) latest structured facts (last-write-wins)
3) fact change history for traceability/evaluation

Memory is persisted inside context.pending_data["_memory"] so no schema
migration is required.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import re
from typing import Any

from config.settings import settings
from llm.client import llm_client
from schemas.chat import ConversationContext, ConversationState, MessageRole


class ConversationMemoryService:
    """Service for retention beyond a short message window."""

    MEMORY_KEY = "_memory"
    MAX_FACT_HISTORY = 40
    MAX_BOOKING_HISTORY = 20
    MAX_ORDER_HISTORY = 20
    MAX_TICKET_HISTORY = 25
    # Rebuild the running summary EVERY turn (was 6 → stale for up to 6 messages).
    # The refresh is fire-and-forget in the background (chat_async_summary_enabled),
    # so the reply never waits: it goes out with the last-10 messages + whatever
    # summary is already stored, and the fresh one is ready by the next turn.
    SUMMARY_REFRESH_INTERVAL = 1
    # Room for the structured per-hotel note (hotels + their own dates) — was 900,
    # which truncated multi-hotel conversations.
    SUMMARY_MAX_LEN = 2000
    MAX_CONTEXTUAL_QUERY_LEN = 420

    _TIME_PATTERN = re.compile(
        r"\b((?:[01]?\d|2[0-3]):[0-5]\d(?:\s*(?:am|pm))?|(?:[1-9]|1[0-2])\s*(?:am|pm)|(?:[01]?\d|2[0-3])\s*(?:hrs|hours))\b",
        re.IGNORECASE,
    )
    _ROOM_PATTERN = re.compile(
        r"\b(?:my\s+)?room(?:\s*(?:number|no\.?))?\s*(?:is|=|:)?\s*([a-zA-Z0-9-]{2,10})\b",
        re.IGNORECASE,
    )
    # "for 4" / "party of 6" → party size, BUT NOT "for 31 feb" / "for 20 march"
    # (that number is a DATE, not a guest count) — negative lookahead for a month.
    _PARTY_PATTERN = re.compile(
        r"\b(?:for|party of)\s+(\d{1,2})\b"
        r"(?!\s*(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec))",
        re.IGNORECASE,
    )
    _MONTH_ALIASES = {
        "jan": "Jan",
        "january": "Jan",
        "feb": "Feb",
        "february": "Feb",
        "mar": "Mar",
        "march": "Mar",
        "apr": "Apr",
        "april": "Apr",
        "may": "May",
        "jun": "Jun",
        "june": "Jun",
        "jul": "Jul",
        "july": "Jul",
        "aug": "Aug",
        "august": "Aug",
        "sep": "Sep",
        "sept": "Sep",
        "september": "Sep",
        "oct": "Oct",
        "october": "Oct",
        "nov": "Nov",
        "november": "Nov",
        "dec": "Dec",
        "december": "Dec",
    }
    _MONTH_TOKEN = (
        r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
        r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
    )
    _DATE_RANGE_MONTH_FIRST_PATTERN = re.compile(
        rf"\b(?P<month>{_MONTH_TOKEN})\s*(?P<start>\d{{1,2}})(?:st|nd|rd|th)?\s*"
        rf"(?:-|to|through|till|until)\s*(?P<end>\d{{1,2}})(?:st|nd|rd|th)?\b",
        re.IGNORECASE,
    )
    _DATE_RANGE_FROM_TO_PATTERN = re.compile(
        rf"\bfrom\s*(?P<start>\d{{1,2}})(?:st|nd|rd|th)?\s*(?P<start_month>{_MONTH_TOKEN})?\s*"
        rf"(?:-|to|through|till|until)\s*(?P<end>\d{{1,2}})(?:st|nd|rd|th)?\s*(?P<end_month>{_MONTH_TOKEN})\b",
        re.IGNORECASE,
    )
    _SINGLE_DATE_MONTH_FIRST_PATTERN = re.compile(
        rf"\b(?P<month>{_MONTH_TOKEN})\s*(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\b",
        re.IGNORECASE,
    )
    _SINGLE_DATE_DAY_FIRST_PATTERN = re.compile(
        rf"\b(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s*(?P<month>{_MONTH_TOKEN})\b",
        re.IGNORECASE,
    )
    _SHORT_FOLLOWUP_PATTERN = re.compile(
        r"^\s*(?:"
        r"how much(?: cost| price| charges?)?|"
        r"what about price|"
        r"price\??|"
        r"cost\??|"
        r"charges?\??|"
        r"and price\??|"
        r"what time\??|"
        r"when\??|"
        r"how long\??|"
        r"availability\??|"
        r"is it available\??"
        r")\s*$",
        re.IGNORECASE,
    )

    def ensure_memory(self, context: ConversationContext) -> dict[str, Any]:
        """Ensure memory structure exists in pending_data and return it."""
        if not isinstance(context.pending_data, dict):
            context.pending_data = {}

        memory = context.pending_data.get(self.MEMORY_KEY)
        if not isinstance(memory, dict):
            memory = {
                "summary": "",
                "facts": {},
                "fact_history": [],
                "last_summarized_count": 0,
                "updated_at": datetime.now(UTC).isoformat(),
            }
            context.pending_data[self.MEMORY_KEY] = memory
            return memory

        if not isinstance(memory.get("facts"), dict):
            memory["facts"] = {}
        if not isinstance(memory.get("fact_history"), list):
            memory["fact_history"] = []
        if not isinstance(memory.get("summary"), str):
            memory["summary"] = ""
        if not isinstance(memory.get("last_summarized_count"), int):
            memory["last_summarized_count"] = 0
        memory["updated_at"] = datetime.now(UTC).isoformat()
        return memory

    @staticmethod
    def internal_pending_entries(pending_data: Any) -> dict[str, Any]:
        """Get reserved internal keys (prefixed with underscore)."""
        if not isinstance(pending_data, dict):
            return {}
        return {
            key: value
            for key, value in pending_data.items()
            if isinstance(key, str) and key.startswith("_")
        }

    def retain_safe_internal_entries(
        self,
        pending_data: Any,
        *,
        preserve_deferred_refresh: bool = True,
        preserve_last_service: bool = False,
        preserve_task_state: bool = False,
        extra_keys: list[str] | tuple[str, ...] | set[str] | None = None,
    ) -> dict[str, Any]:
        """
        Keep only internal context that is safe to carry across a completed flow.

        This intentionally drops stale execution continuity markers like
        `_last_service_id` and `_task_state` unless the caller explicitly keeps
        them. Property scope, memory, stay/booking anchors, and phase context
        survive so the next turn still answers in the correct scope.
        """
        if not isinstance(pending_data, dict):
            return {}

        safe_keys = {
            self.MEMORY_KEY,
            "_integration",
            # WHICH CHAT THREAD THIS TURN BELONGS TO. Not flow state at all — it
            # identifies the conversation, and it is what `add_message` stamps
            # onto every message so history can be scoped per thread. Left out,
            # a flow completing mid-turn erased it BETWEEN the guest's message
            # and the reply to it: the guest's row carried the thread id, the
            # assistant's was written with conversation_uid NULL, and the widget
            # — which shows only rows matching the current thread — replayed a
            # history of the guest talking to nobody. Same shape as the
            # `_ird_closed_items` bug: a key the whole turn depends on, dropped
            # by a cleanup that only meant to forget the flow.
            "_active_conversation_uid",
            # In-flight OTP for the opening lead form: the hashed code, its
            # expiry and the attempts left. A flow completing between sending
            # the code and the guest typing it would otherwise wipe the
            # challenge, and every correct code would come back "not found" —
            # the same shape as the `_ird_closed_items` bug above.
            "_otp_state",
            "_property_name_map",
            "_known_property_scope_count",
            "_selected_phase_id",
            "_selected_phase",
            "_active_property_ids",
            "_active_property_names",
            "_selected_property_scope_id",
            "_selected_property_scope_ids",
            "_selected_property_scope_name",
            "_past_property_context",
            "_stay_property_id",
            "_stay_property_name",
            "_stay_property_pin_enabled",
            "_stay_property_pin_last_source",
            # The menu PDF is handed over ONCE per conversation. Completing a flow
            # (placing a food order, say) must not erase that, or the next time food
            # came up the same PDF was attached all over again.
            "_menu_doc_sent",
            # WHAT THE KITCHEN HAS ALREADY BEEN SENT. Placing an order completes a
            # flow, so this cleanup ran and erased the record of the order that had
            # just been placed — leaving the next turn unable to tell new food from
            # food already cooking. A guest who then added one dessert had the whole
            # earlier order sent again as a second ticket. This is precisely the
            # "internal context that is safe to carry across a completed flow" this
            # list exists for: it survives so the next order can be measured
            # against it.
            "_ird_placed_items",
            "_ird_open_order_items",
            "_ird_order_ticket_ref",
            "_ird_order_placed_at",
            # WHAT THE GUEST HAS ALREADY ASKED TO BOOK. Submitting the booking
            # form COMPLETES a flow, so this cleanup ran and erased the record of
            # the booking that had just been made — the exact moment it starts
            # mattering. Everything built on it then died with it: the "your
            # request is already with our team, for these dates" reply, the
            # date-change path that updates the existing ticket instead of
            # raising a second one, and the property-switch prompt. A guest who
            # submitted their details and then said "I'd like to book a room" was
            # handed the same "share your booking details" line again, pointing
            # at a button already spent. Same shape as `_ird_placed_items` above:
            # a ledger of what has been DONE, which is precisely the internal
            # context this list exists to carry across a completed flow.
            "_rb_bookings",
            # THE LEDGER OF ORDERS THIS CONVERSATION HAS PLACED, and the
            # bookkeeping that goes with it: which order number the guest is on,
            # which orders they have explicitly moved on from, and whether the
            # cart was just reset for a fresh one. A guest can place an order,
            # raise a complaint (which completes a flow and runs this cleanup) and
            # come back to order again - without these, the second order forgets
            # the first ever existed and cannot be numbered or kept apart from it.
            "_ird_placed_orders",
            "_ird_current_order_no",
            "_ird_closed_order_refs",
            "_ird_fresh_order",
            # WHICH TICKETS THE GUEST FILLED IN THEMSELVES. Their request line is
            # rendered from form fields, so a later edit may annotate it but must
            # never replace it with free text. Whitelisted because a form is
            # submitted on one turn and edited many turns later - left out, the
            # protection would last exactly one turn, which is how
            # `_ird_closed_items` failed before it.
            "_form_built_ticket_refs",
            # WHICH SERVICES' FORMS HAVE BEEN SUBMITTED (e.g. the event enquiry).
            # Written at form submit, then this cleanup ran in the SAME request and
            # erased it, so the "already submitted" check never saw it and the
            # "Share your event details" button came back on every banquet answer.
            "_form_submitted_services",
            # The post-form "In the meantime, want to explore more...?" closer, so a
            # bare "yes" to it next turn is read as exploring, not a staff handoff.
            "_post_form_closer",
            # THE ORDER THE GUEST IS STILL BUILDING. Raising a ticket for anything
            # else completes a flow, and this cleanup then erased a food order that
            # was still in progress: a guest who ordered a tandoori murgh and two
            # garlic naans, reported a broken AC, let us hand that to Engineering,
            # and came back to finish was shown an order containing only the
            # dessert. On dining turns the cart is put back afterwards by
            # _ird_sync_pending_updates, which is why this only ever broke when the
            # completed flow belonged to some OTHER service and no dining decision
            # ran to restore it. Keeping it here is not the same as never clearing
            # it - the cart is cleared deliberately, at the moment the placed order
            # stops being editable and a further dish is genuinely a new order.
            "_ird_cart",
            # ...and the provenance verdicts that go with it, so items already shown
            # to have been asked for are not re-judged against a window that no
            # longer reaches back to the message where the guest ordered them.
            "_ird_vetted_items",
            # The "which one should I cancel?" list the guest is answering. It lives
            # for one turn and is read before anything else on the next one.
            "_cancel_menu",
            # ...and the "which hotel?" a booking or support request is waiting on.
            "_awaiting_hotel_for",
        }
        if preserve_deferred_refresh:
            safe_keys.add("_defer_summary_refresh")
        if preserve_last_service:
            safe_keys.update({"_last_service_id", "_last_service_name"})
        if preserve_task_state:
            safe_keys.add("_task_state")
        if extra_keys:
            safe_keys.update(
                str(key).strip()
                for key in extra_keys
                if str(key).strip().startswith("_")
            )

        safe_prefixes = (
            "_latest_booking_",
        )
        retained: dict[str, Any] = {}
        for key, value in pending_data.items():
            if not isinstance(key, str) or not key.startswith("_"):
                continue
            if key in safe_keys or any(key.startswith(prefix) for prefix in safe_prefixes):
                retained[key] = value
        return retained

    # Pending-data keys that are NOT conversation-scoped and must survive a
    # conversation rotation. Everything else (memory, property scope, stay/phase/
    # topic anchors, cached book context, task state, …) is wiped so a fresh
    # conversation_id genuinely starts clean. Lead identity is intentionally NOT
    # listed here — it re-prefills from new_bot_sessions; the typed context fields
    # (guest_name/guest_phone) are not touched by this reset.
    CONVERSATION_DURABLE_KEYS = ("_integration",)

    def reset_pending_for_conversation_rotation(
        self,
        context: ConversationContext,
        new_conversation_uid: Any,
    ) -> bool:
        """Wipe conversation-scoped state when a NEW conversation_id arrives.

        Context is loaded per ``session_id`` so ``pending_data`` is shared across
        every conversation a visitor has. Without this reset, scope/memory from a
        prior conversation (e.g. ``_active_property_ids`` locked to one property)
        leaks into a fresh chat. Message history is already conversation-scoped via
        ``_active_conversation_uid``; this brings pending_data + flow state in line.

        Returns True iff a rotation was detected and the reset was applied.
        """
        new_uid = str(new_conversation_uid or "").strip()
        if not new_uid:
            return False
        pending = context.pending_data if isinstance(context.pending_data, dict) else {}
        prev_uid = str(pending.get("_active_conversation_uid") or "").strip()
        # First turn of a session (no prior uid) is already fresh — nothing to wipe.
        # Same conversation continuing — keep scope/memory as-is.
        if not prev_uid or prev_uid == new_uid:
            return False

        preserved = {
            key: value
            for key, value in pending.items()
            if key in self.CONVERSATION_DURABLE_KEYS
        }
        preserved["_active_conversation_uid"] = new_uid
        context.pending_data = preserved

        # Reset conversation-scoped flow state living on the context object.
        context.state = ConversationState.IDLE
        context.pending_action = None
        context.suspended_services = []
        context.resume_prompt_sent = False
        # The booked / stay property identity also lives on the context object (not
        # pending_data, which we wiped above). A fresh conversation must NOT inherit
        # the previous conversation's booked property — otherwise a newly-explored
        # property gets mislabelled with the old one (e.g. exploring Digha but the
        # booking reply/ticket said "Lansdowne"). Clear it so the new chat starts on
        # the active scope only.
        for _attr in ("booking_property_name", "booking_property_id", "booking_property_scope_id"):
            if hasattr(context, _attr):
                try:
                    setattr(context, _attr, None)
                except Exception:
                    pass
        return True

    def merge_with_internal(
        self,
        new_pending_data: Any,
        internal_entries: dict[str, Any],
    ) -> dict[str, Any]:
        """Merge handler pending data with preserved internal entries."""
        merged: dict[str, Any] = {}
        if isinstance(new_pending_data, dict):
            merged.update(new_pending_data)
        for key, value in (internal_entries or {}).items():
            merged.setdefault(key, value)
        return merged

    def capture_user_message(self, context: ConversationContext, message: str) -> None:
        """
        Extract memory updates from the user message and store them.
        Deterministic extraction handles common service/booking details.
        """
        memory = self.ensure_memory(context)
        facts = memory.setdefault("facts", {})
        fact_history = memory.setdefault("fact_history", [])

        msg = str(message or "").strip()
        if not msg:
            return
        self._capture_context_profile_facts(
            context=context,
            facts=facts,
            fact_history=fact_history,
            source_message=msg[:300],
        )
        msg_lower = msg.lower()
        is_correction = self._looks_like_correction(msg_lower)

        extracted = self._extract_facts(msg)
        topic_hint = self._infer_topic_hint(msg_lower)
        if topic_hint:
            extracted["last_user_topic"] = topic_hint
        if is_correction:
            self._apply_correction_inference(extracted, facts)
        for key, value in extracted.items():
            self._set_fact(
                facts=facts,
                fact_history=fact_history,
                key=key,
                value=value,
                source_message=msg[:300],
                change_type="corrected" if (facts.get(key) is not None and is_correction) else "set",
            )

        if len(fact_history) > self.MAX_FACT_HISTORY:
            memory["fact_history"] = fact_history[-self.MAX_FACT_HISTORY :]

        memory["last_user_message"] = msg[:400]
        memory["updated_at"] = datetime.now(UTC).isoformat()

    def capture_assistant_message(
        self,
        context: ConversationContext,
        message: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """
        Capture durable transactional details emitted by handlers/assistant.
        This keeps reservation and order info retrievable in later turns.
        """
        memory = self.ensure_memory(context)
        facts = memory.setdefault("facts", {})
        fact_history = memory.setdefault("fact_history", [])

        meta = metadata if isinstance(metadata, dict) else {}
        msg = str(message or "").strip()
        now_iso = datetime.now(UTC).isoformat()
        self._capture_context_profile_facts(
            context=context,
            facts=facts,
            fact_history=fact_history,
            source_message=msg[:300] if msg else "[assistant_metadata]",
        )
        # End of turn: the router has finalized the active hotel and the date
        # resolver has written the resolved stay dates. Pin those dates to the
        # hotel in focus NOW — synchronously, so it can't race the next turn (the
        # background summary could run after the scope already moved to another
        # hotel, which mis-attributed the dates). Pass the guest's message this turn
        # so we only attribute when they actually talked about dates.
        self._attribute_dates_to_active_property(
            context, facts, str(memory.get("last_user_message") or "")
        )

        booking_ref = str(meta.get("booking_ref") or "").strip()
        if booking_ref:
            booking_record = {
                "reference": booking_ref,
                "restaurant": str(meta.get("booking_restaurant") or meta.get("restaurant_name") or "").strip(),
                "party_size": str(meta.get("booking_party_size") or "").strip(),
                "time": str(meta.get("booking_time") or "").strip(),
                "date": str(meta.get("booking_date") or "").strip(),
                "status": "confirmed",
                "updated_at": now_iso,
            }
            self._set_fact(
                facts=facts,
                fact_history=fact_history,
                key="latest_booking",
                value=booking_record,
                source_message=msg[:300],
            )
            self._set_fact(
                facts=facts,
                fact_history=fact_history,
                key="last_booking_ref",
                value=booking_ref,
                source_message=msg[:300],
            )
            if booking_record.get("time"):
                self._set_fact(
                    facts=facts,
                    fact_history=fact_history,
                    key="booking_time",
                    value=booking_record["time"],
                    source_message=msg[:300],
                )
            if booking_record.get("date"):
                self._set_fact(
                    facts=facts,
                    fact_history=fact_history,
                    key="booking_day",
                    value=booking_record["date"],
                    source_message=msg[:300],
                )
            history = list(facts.get("booking_history") or [])
            history.append(booking_record)
            self._set_fact(
                facts=facts,
                fact_history=fact_history,
                key="booking_history",
                value=history[-self.MAX_BOOKING_HISTORY :],
                source_message=msg[:300],
            )

        order_id = meta.get("order_id")
        if order_id is not None and str(order_id).strip():
            order_record = {
                "id": str(order_id).strip(),
                "items": list(meta.get("order_items") or []),
                "total": meta.get("order_total"),
                "restaurant_id": meta.get("restaurant_id"),
                "status": "confirmed",
                "updated_at": now_iso,
            }
            self._set_fact(
                facts=facts,
                fact_history=fact_history,
                key="latest_order",
                value=order_record,
                source_message=msg[:300],
            )
            self._set_fact(
                facts=facts,
                fact_history=fact_history,
                key="last_order_id",
                value=order_record["id"],
                source_message=msg[:300],
            )
            history = list(facts.get("order_history") or [])
            history.append(order_record)
            self._set_fact(
                facts=facts,
                fact_history=fact_history,
                key="order_history",
                value=history[-self.MAX_ORDER_HISTORY :],
                source_message=msg[:300],
            )

        ticket_id = meta.get("ticket_id")
        if ticket_id is not None and str(ticket_id).strip():
            ticket_record = {
                "id": str(ticket_id).strip(),
                "status": str(meta.get("ticket_status") or "open").strip().lower() or "open",
                "category": str(meta.get("ticket_category") or "").strip().lower(),
                "sub_category": str(meta.get("ticket_sub_category") or "").strip().lower(),
                "priority": str(meta.get("ticket_priority") or "").strip().lower(),
                "department_id": str(meta.get("ticket_department_id") or "").strip(),
                "assigned_id": str(
                    meta.get("ticket_assigned_id")
                    or meta.get("assigned_id")
                    or meta.get("agent_id")
                    or ""
                ).strip(),
                "room_number": str(
                    meta.get("room_number")
                    or context.room_number
                    or ""
                ).strip(),
                "source": str(meta.get("ticket_source") or "").strip(),
                "updated_at": now_iso,
            }
            if meta.get("ticket_summary"):
                ticket_record["summary"] = str(meta.get("ticket_summary")).strip()

            self._set_fact(
                facts=facts,
                fact_history=fact_history,
                key="latest_ticket",
                value=ticket_record,
                source_message=msg[:300],
            )
            self._set_fact(
                facts=facts,
                fact_history=fact_history,
                key="last_ticket_id",
                value=ticket_record["id"],
                source_message=msg[:300],
            )
            history = list(facts.get("ticket_history") or [])
            history.append(ticket_record)
            self._set_fact(
                facts=facts,
                fact_history=fact_history,
                key="ticket_history",
                value=history[-self.MAX_TICKET_HISTORY :],
                source_message=msg[:300],
            )

        if meta.get("room_number"):
            self._set_fact(
                facts=facts,
                fact_history=fact_history,
                key="room_number",
                value=str(meta["room_number"]).strip(),
                source_message=msg[:300],
            )

        guest_preferences_meta = meta.get("guest_preferences")
        if isinstance(guest_preferences_meta, list):
            normalized_preferences: list[str] = []
            for item in guest_preferences_meta:
                text = str(item or "").strip().lower()
                if not text:
                    continue
                text = re.sub(r"[^a-z0-9 /-]+", " ", text)
                text = re.sub(r"\s+", " ", text).strip()
                if len(text) < 3 or text in normalized_preferences:
                    continue
                normalized_preferences.append(text[:80])
                if len(normalized_preferences) >= 8:
                    break
            if normalized_preferences:
                self._set_fact(
                    facts=facts,
                    fact_history=fact_history,
                    key="guest_preferences",
                    value=normalized_preferences,
                    source_message=msg[:300],
                )

        if meta.get("flight_time"):
            self._set_fact(
                facts=facts,
                fact_history=fact_history,
                key="flight_time",
                value=str(meta["flight_time"]).strip(),
                source_message=msg[:300],
            )

        if meta.get("transport_type"):
            self._set_fact(
                facts=facts,
                fact_history=fact_history,
                key="last_transport_type",
                value=str(meta["transport_type"]).strip(),
                source_message=msg[:300],
            )

        memory["last_assistant_message"] = msg[:400]
        memory["updated_at"] = now_iso

    def _capture_context_profile_facts(
        self,
        *,
        context: ConversationContext,
        facts: dict[str, Any],
        fact_history: list[dict[str, Any]],
        source_message: str,
    ) -> None:
        pending = context.pending_data if isinstance(context.pending_data, dict) else {}
        integration = pending.get("_integration", {})
        if not isinstance(integration, dict):
            integration = {}

        profile_map = {
            "guest_name": str(
                context.guest_name
                or pending.get("guest_name")
                or integration.get("guest_name")
                or ""
            ).strip(),
            "guest_phone": str(
                context.guest_phone
                or pending.get("guest_phone")
                or integration.get("guest_phone")
                or integration.get("wa_number")
                or ""
            ).strip(),
            "room_number": str(
                context.room_number
                or pending.get("room_number")
                or integration.get("room_number")
                or ""
            ).strip(),
            "phase": str(integration.get("phase") or pending.get("phase") or "").strip(),
            "stay_checkin_date": str(
                pending.get("stay_checkin_date")
                or pending.get("check_in")
                or integration.get("stay_checkin_date")
                or integration.get("check_in")
                or ""
            ).strip(),
            "stay_checkout_date": str(
                pending.get("stay_checkout_date")
                or pending.get("check_out")
                or integration.get("stay_checkout_date")
                or integration.get("check_out")
                or ""
            ).strip(),
        }

        for key, value in profile_map.items():
            if not value:
                continue
            self._set_fact(
                facts=facts,
                fact_history=fact_history,
                key=key,
                value=value,
                source_message=source_message,
            )

    def contextualize_follow_up_query(
        self,
        context: ConversationContext,
        user_query: str,
    ) -> dict[str, Any]:
        """
        Rewrite terse follow-up user queries with conversation memory context.
        Returns metadata for observability and downstream routing.
        """
        query = str(user_query or "").strip()
        if not query:
            return {"query": query, "rewritten": False, "reason": "empty"}

        memory = self.ensure_memory(context)
        facts = memory.get("facts", {}) if isinstance(memory.get("facts"), dict) else {}

        should_rewrite = self._should_rewrite_follow_up(query)
        if not should_rewrite:
            return {"query": query, "rewritten": False, "reason": "not_followup"}

        previous_user = self._previous_user_message(context)
        contextual_parts: list[str] = []
        if previous_user:
            contextual_parts.append(f"previous_request={previous_user[:160]}")

        last_topic = str(facts.get("last_user_topic") or "").strip()
        if last_topic:
            contextual_parts.append(f"topic={last_topic}")

        facts_snippet = []
        for key in (
            "route",
            "flight_time",
            "pickup_time",
            "booking_time",
            "booking_day",
            "departure_time",
            "departure_day",
            "room_number",
        ):
            value = facts.get(key)
            if value:
                facts_snippet.append(f"{key}={value}")
        if facts_snippet:
            contextual_parts.append("known_facts=" + ", ".join(facts_snippet[:5]))

        if not contextual_parts:
            return {"query": query, "rewritten": False, "reason": "no_context"}

        rewritten = f"{query} | Context: {'; '.join(contextual_parts)}"
        rewritten = rewritten[: self.MAX_CONTEXTUAL_QUERY_LEN]
        return {
            "query": rewritten,
            "rewritten": True,
            "reason": "followup_context_added",
            "base_query": query,
            "contextual_parts": contextual_parts,
        }

    def get_snapshot(self, context: ConversationContext) -> dict[str, Any]:
        """Return a safe memory snapshot for prompt assembly and debug metadata."""
        memory = self.ensure_memory(context)
        return {
            "summary": str(memory.get("summary") or "").strip(),
            "facts": dict(memory.get("facts") or {}),
            "recent_changes": list(memory.get("fact_history") or [])[-5:],
            "last_summarized_count": int(memory.get("last_summarized_count") or 0),
        }

    async def maybe_refresh_summary(self, context: ConversationContext) -> None:
        """Refresh rolling summary periodically to retain long conversations."""
        if isinstance(getattr(context, "pending_data", None), dict):
            if bool(context.pending_data.get("_defer_summary_refresh")):
                return
        memory = self.ensure_memory(context)
        message_count = len(context.messages)
        last = int(memory.get("last_summarized_count") or 0)

        if message_count < 4:
            return
        if (message_count - last) < self.SUMMARY_REFRESH_INTERVAL:
            return

        summary = await self._build_summary(context, memory)
        memory["summary"] = summary[: self.SUMMARY_MAX_LEN]
        memory["last_summarized_count"] = message_count
        memory["updated_at"] = datetime.now(UTC).isoformat()

    async def refresh_summary(self, context: ConversationContext) -> None:
        """Force summary refresh for modes that require maximum continuity."""
        memory = self.ensure_memory(context)
        summary = await self._build_summary(context, memory)
        memory["summary"] = summary[: self.SUMMARY_MAX_LEN]
        memory["last_summarized_count"] = len(context.messages)
        memory["updated_at"] = datetime.now(UTC).isoformat()

    async def _build_summary(self, context: ConversationContext, memory: dict[str, Any]) -> str:
        """Generate summary with LLM when available, otherwise use deterministic fallback."""
        previous_summary = str(memory.get("summary") or "").strip()
        facts = memory.setdefault("facts", {})
        if not isinstance(facts, dict):
            facts = {}
            memory["facts"] = facts
        recent_changes = list(memory.get("fact_history") or [])[-8:]

        # CONVERSATION-SCOPED transcript: build the summary ONLY from the current
        # conversation's messages, never the raw session history. get_recent_messages
        # filters by the active conversation_uid; a prior conversation must never leak
        # into this one's summary (the memory itself is already wiped on rotation, but
        # context.messages is not, so we must scope here too).
        if hasattr(context, "get_recent_messages"):
            recent_msgs = context.get_recent_messages(18)
        else:
            recent_msgs = context.messages[-18:]
        recent_turns = []
        for msg in recent_msgs:
            role = getattr(msg.role, "value", str(msg.role))
            recent_turns.append(f"{role.upper()}: {msg.content}")
        transcript = "\n".join(recent_turns) if recent_turns else "No transcript available."

        # Code-maintained spine the summary MUST copy verbatim: every hotel
        # discussed (in order) with its OWN dates. Built from pending_data + facts,
        # so names & dates cannot be paraphrased away.
        hotels_block = self._discussed_hotels_block(context, facts)

        if settings.openai_api_key:
            prompt = (
                "You maintain the running memory of a hotel concierge conversation.\n"
                "Rewrite the memory as a compact, STRUCTURED note (not a paragraph). Rules:\n"
                "- PRESERVE WORD-FOR-WORD every hotel name and every date. NEVER paraphrase, round, or drop a date or a hotel name.\n"
                "- Keep EACH hotel with ITS OWN dates and what the guest asked/decided about it. Never move one hotel's dates onto another or merge hotels.\n"
                "- Record the guest's CURRENT language from the latest guest turns; if they switched (e.g. 'back to english'), use the NEW language.\n"
                "- Record party size / purpose and any OPEN request (e.g. 'wants booking link for Jaipur').\n"
                "- New/corrected values override old ones. NEVER invent a booking, date, or plan that was not actually stated.\n"
                "- No secrets or internal data.\n\n"
                "Output EXACTLY this template:\n"
                "LANGUAGE: <current language>\n"
                "GUEST: <purpose / party size, or 'unknown'>\n"
                "HOTELS DISCUSSED (oldest first):\n"
                "  - <Hotel name> | dates: <exact dates or 'none'> | notes: <what they asked/decided>\n"
                "OPEN REQUESTS: <list, or 'none'>\n\n"
                "Hotels discussed + known dates (AUTHORITATIVE — copy these names and dates exactly):\n"
                f"{hotels_block}\n\n"
                f"Previous memory note:\n{previous_summary or 'None'}\n\n"
                f"Known Facts JSON:\n{json.dumps(facts, ensure_ascii=False)}\n\n"
                f"Recent Fact Changes JSON:\n{json.dumps(recent_changes, ensure_ascii=False)}\n\n"
                f"Recent Transcript:\n{transcript}\n"
            )
            summary_model = str(getattr(settings, "chat_summary_model", "") or "").strip() or None
            try:
                summary = await llm_client.chat(
                    messages=[{"role": "system", "content": prompt}],
                    model=summary_model,
                    temperature=0.1,
                    max_tokens=500,
                )
                cleaned = str(summary or "").strip()
                if cleaned:
                    return cleaned
            except Exception:
                pass

        return self._fallback_summary(context, facts, recent_changes)

    def _fallback_summary(
        self,
        context: ConversationContext,
        facts: dict[str, Any],
        recent_changes: list[dict[str, Any]],
    ) -> str:
        """Deterministic summary fallback for local/dev mode."""
        # Conversation-scoped: only this conversation's messages, never the session's.
        scoped_msgs = (
            context.get_recent_messages(60)
            if hasattr(context, "get_recent_messages")
            else context.messages
        )
        user_messages = [
            msg.content.strip()
            for msg in scoped_msgs
            if msg.role == MessageRole.USER and msg.content.strip()
        ]

        parts: list[str] = []
        if user_messages:
            parts.append(f"Initial user request: {user_messages[0][:120]}")
            parts.append(f"Latest user request: {user_messages[-1][:140]}")

        if facts:
            compact_facts = ", ".join(
                f"{key}={value}"
                for key, value in list(facts.items())[:8]
            )
            parts.append(f"Current known details: {compact_facts}")

        if recent_changes:
            compact_changes = "; ".join(
                f"{item.get('key')}->{item.get('new_value')}"
                for item in recent_changes[-3:]
            )
            parts.append(f"Recent updates: {compact_changes}")

        if not parts:
            return "Conversation started. No durable details captured yet."
        return " | ".join(parts)

    @staticmethod
    def _looks_like_correction(msg_lower: str) -> bool:
        correction_markers = (
            "change",
            "changed",
            "update",
            "updated",
            "instead",
            "actually",
            "correction",
            "make it",
            "reschedule",
        )
        return any(marker in msg_lower for marker in correction_markers)

    def _extract_facts(self, message: str) -> dict[str, Any]:
        """Extract commonly useful details from free-form user text."""
        msg = str(message or "").strip()
        msg_lower = msg.lower()
        extracted: dict[str, Any] = {}

        room_match = self._ROOM_PATTERN.search(msg)
        if room_match:
            room_candidate = room_match.group(1).upper()
            if any(ch.isdigit() for ch in room_candidate):
                extracted["room_number"] = room_candidate

        party_match = self._PARTY_PATTERN.search(msg)
        if party_match:
            extracted["party_size"] = party_match.group(1)

        stay_dates = self._extract_stay_dates(msg_lower)
        if stay_dates:
            extracted.update(stay_dates)

        time_match = self._TIME_PATTERN.search(msg_lower)
        if time_match:
            time_value = time_match.group(1).strip().replace("  ", " ")
            if any(token in msg_lower for token in ("leave", "leaving", "checkout", "check out", "check-out", "depart")):
                extracted["departure_time"] = time_value
            elif any(token in msg_lower for token in ("flight", "arrival", "terminal")):
                extracted["flight_time"] = time_value
            elif any(token in msg_lower for token in ("pickup", "pick up", "drop", "transfer", "cab", "taxi")):
                extracted["pickup_time"] = time_value
            elif any(token in msg_lower for token in ("book", "booking", "table", "reservation")):
                extracted["booking_time"] = time_value
            else:
                extracted["mentioned_time"] = time_value

        if "tomorrow" in msg_lower:
            if any(token in msg_lower for token in ("leave", "leaving", "checkout", "check out", "depart")):
                extracted["departure_day"] = "tomorrow"
            elif "book" in msg_lower or "reservation" in msg_lower:
                extracted["booking_day"] = "tomorrow"
            else:
                extracted["mentioned_day"] = "tomorrow"
        elif "today" in msg_lower:
            extracted["mentioned_day"] = "today"
        elif "tonight" in msg_lower:
            extracted["mentioned_day"] = "tonight"

        # Transport route hint: "from X to Y"
        route_match = re.search(r"\bfrom\s+([a-z ]{2,40})\s+to\s+([a-z ]{2,40})\b", msg_lower)
        if route_match:
            source = route_match.group(1).strip()
            destination = route_match.group(2).strip()
            extracted["route"] = f"{source} -> {destination}"

        return extracted

    def _extract_stay_dates(self, msg_lower: str) -> dict[str, Any]:
        """
        Extract stay/booking date hints, including compact ranges like
        'feb 20-23' and correction-style messages.
        """
        extracted: dict[str, Any] = {}
        if not msg_lower:
            return extracted

        stay_context = any(
            token in msg_lower
            for token in (
                "stay",
                "book",
                "booking",
                "reservation",
                "check in",
                "check-in",
                "checkin",
                "check out",
                "check-out",
                "checkout",
                "room",
            )
        )
        if not stay_context:
            return extracted

        range_match = self._DATE_RANGE_MONTH_FIRST_PATTERN.search(msg_lower)
        if range_match:
            month_token = str(range_match.group("month") or "")
            start_day = range_match.group("start")
            end_day = range_match.group("end")
            start = self._format_month_day(month_token, start_day)
            end = self._format_month_day(month_token, end_day)
            if start and end:
                extracted["stay_checkin_date"] = start
                extracted["stay_checkout_date"] = end
                extracted["stay_date_range"] = f"{start} to {end}"
                extracted.setdefault("booking_day", extracted["stay_date_range"])
                return extracted

        from_to_match = self._DATE_RANGE_FROM_TO_PATTERN.search(msg_lower)
        if from_to_match:
            start_day = from_to_match.group("start")
            end_day = from_to_match.group("end")
            start_month = str(from_to_match.group("start_month") or from_to_match.group("end_month") or "")
            end_month = str(from_to_match.group("end_month") or start_month)
            start = self._format_month_day(start_month, start_day)
            end = self._format_month_day(end_month, end_day)
            if start and end:
                extracted["stay_checkin_date"] = start
                extracted["stay_checkout_date"] = end
                extracted["stay_date_range"] = f"{start} to {end}"
                extracted.setdefault("booking_day", extracted["stay_date_range"])
                return extracted

        single_month_first = self._SINGLE_DATE_MONTH_FIRST_PATTERN.search(msg_lower)
        single_day_first = self._SINGLE_DATE_DAY_FIRST_PATTERN.search(msg_lower)
        month_token = ""
        day_token = ""
        if single_month_first:
            month_token = str(single_month_first.group("month") or "")
            day_token = str(single_month_first.group("day") or "")
        elif single_day_first:
            month_token = str(single_day_first.group("month") or "")
            day_token = str(single_day_first.group("day") or "")

        single_date = self._format_month_day(month_token, day_token)
        if not single_date:
            return extracted

        if any(token in msg_lower for token in ("check out", "check-out", "checkout")):
            extracted["stay_checkout_date"] = single_date
        elif any(token in msg_lower for token in ("check in", "check-in", "checkin")):
            extracted["stay_checkin_date"] = single_date
        else:
            extracted["stay_checkin_date"] = single_date
        extracted.setdefault("booking_day", single_date)
        return extracted

    def _format_month_day(self, month_token: str, day_token: str) -> str:
        month_key = str(month_token or "").strip().lower()
        month_norm = self._MONTH_ALIASES.get(month_key)
        if not month_norm:
            return ""
        try:
            day_value = int(str(day_token or "").strip())
        except (TypeError, ValueError):
            return ""
        if day_value < 1 or day_value > 31:
            return ""
        return f"{month_norm} {day_value:02d}"

    @staticmethod
    def _infer_topic_hint(msg_lower: str) -> str:
        if any(token in msg_lower for token in ("airport", "flight", "transfer", "cab", "taxi", "pickup", "drop")):
            return "transport"
        if any(token in msg_lower for token in ("book", "booking", "reservation", "table")):
            return "reservation"
        if any(token in msg_lower for token in ("order", "menu", "food", "dish", "price")):
            return "menu_or_order"
        if any(token in msg_lower for token in ("leave", "leaving", "checkout", "check out", "depart")):
            return "checkout_or_departure"
        if any(token in msg_lower for token in ("room service", "housekeeping", "cleaning", "towel", "amenities")):
            return "room_service"
        return ""

    @staticmethod
    def _message_mentions_dates(text: str) -> bool:
        """True when the guest's message actually talks about stay dates this turn."""
        t = str(text or "").lower()
        if not t:
            return False
        # month name followed by a day number ("december 22", "march 10")
        if re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b", t):
            return True
        if re.search(r"\b20\d\d\b", t):                       # a year
            return True
        if re.search(r"\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b", t):  # 12/22 or 12-22-2026
            return True
        return any(kw in t for kw in (
            "date", "check in", "check-in", "checkin", "check out", "checkout",
            "nights", "same dates", "those dates",
        ))

    @staticmethod
    def _message_signals_same_dates(text: str) -> bool:
        """True when the guest says the SAME dates apply (booking the same dates at
        more than one hotel is legitimate)."""
        t = str(text or "").lower()
        return bool(t) and any(kw in t for kw in (
            "same date", "same day", "those date", "these date", "same as",
            " too", " also", " as well", "for both", "both hotel", "the same",
        ))

    def _attribute_dates_to_active_property(
        self,
        context: ConversationContext,
        facts: dict[str, Any],
        last_user_message: str = "",
    ) -> None:
        """Attribute the resolved stay dates to the hotel in focus, keyed per hotel so
        one hotel's dates never overwrite another's. Called synchronously at end of
        turn (active hotel + resolved dates are final). Fires ONLY when the guest's
        message THIS turn actually mentioned dates — a plain hotel switch must not make
        the new hotel inherit the previous one's persisted dates. Booking the SAME
        dates at multiple hotels IS allowed when the guest says so."""
        pending = context.pending_data if isinstance(getattr(context, "pending_data", None), dict) else {}
        ci = str(pending.get("stay_checkin_date") or getattr(context, "booking_check_in_date", "") or "").strip()
        co = str(pending.get("stay_checkout_date") or getattr(context, "booking_check_out_date", "") or "").strip()
        if not ci and not co:
            return
        if not self._message_mentions_dates(last_user_message):
            return
        active = [str(p).strip() for p in (pending.get("_active_property_ids") or []) if str(p).strip()]
        if len(active) != 1:
            return
        pid = active[0]
        by_prop = facts.get("stay_dates_by_property")
        if not isinstance(by_prop, dict):
            by_prop = {}
            facts["stay_dates_by_property"] = by_prop
        # If these exact dates already belong to a DIFFERENT hotel, only copy them here
        # when the guest signalled the SAME dates apply (some people book the same dates
        # at several hotels). Without that signal, treat it as the previous hotel's
        # stale global slot and do NOT inherit it.
        if not self._message_signals_same_dates(last_user_message):
            for other_pid, other in by_prop.items():
                if other_pid == pid or not isinstance(other, dict):
                    continue
                if str(other.get("checkin") or "") == ci and str(other.get("checkout") or "") == co:
                    return
        entry = dict(by_prop.get(pid) or {})
        if ci:
            entry["checkin"] = ci
        if co:
            entry["checkout"] = co
        name_map = pending.get("_property_name_map")
        if isinstance(name_map, dict) and str(name_map.get(pid) or "").strip():
            entry["name"] = str(name_map.get(pid)).strip()
        by_prop[pid] = entry

    def _discussed_hotels_block(
        self,
        context: ConversationContext,
        facts: dict[str, Any],
    ) -> str:
        """Code-built list of hotels discussed (in order) with each hotel's OWN
        dates — the exact spine the summary is told to copy verbatim (never
        paraphrase). Sourced from pending_data + facts, not from an LLM."""
        pending = context.pending_data if isinstance(getattr(context, "pending_data", None), dict) else {}
        order = [str(p).strip() for p in (pending.get("_discussed_property_order") or []) if str(p).strip()]
        name_map = pending.get("_property_name_map") if isinstance(pending.get("_property_name_map"), dict) else {}
        by_prop = facts.get("stay_dates_by_property") if isinstance(facts.get("stay_dates_by_property"), dict) else {}
        past_ctx = pending.get("_past_property_context") if isinstance(pending.get("_past_property_context"), dict) else {}
        for pid in by_prop:
            if pid not in order:
                order.append(pid)
        if not order:
            return "None yet."
        lines: list[str] = []
        for pid in order:
            dates = by_prop.get(pid) if isinstance(by_prop.get(pid), dict) else {}
            name = str(name_map.get(pid) or dates.get("name") or pid).strip()
            ci = str(dates.get("checkin") or "").strip()
            co = str(dates.get("checkout") or "").strip()
            if ci and co:
                date_str = f"{ci} to {co}"
            elif ci:
                date_str = ci
            else:
                date_str = "none"
            note = str(past_ctx.get(pid) or "").strip()[:120] if past_ctx.get(pid) else ""
            lines.append(f"  - {name} | dates: {date_str}" + (f" | {note}" if note else ""))
        return "\n".join(lines)

    def _set_fact(
        self,
        facts: dict[str, Any],
        fact_history: list[dict[str, Any]],
        key: str,
        value: Any,
        source_message: str,
        change_type: str = "set",
    ) -> None:
        old_value = facts.get(key)
        if old_value == value:
            return
        facts[key] = value
        fact_history.append(
            {
                "key": key,
                "old_value": old_value,
                "new_value": value,
                "change_type": change_type,
                "source_message": source_message,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )

    def _previous_user_message(self, context: ConversationContext) -> str:
        """
        Return the previous user message before the latest one in the context.
        """
        seen_latest = False
        for msg in reversed(context.messages):
            if msg.role != MessageRole.USER:
                continue
            if not seen_latest:
                seen_latest = True
                continue
            value = str(msg.content or "").strip()
            if value:
                return value
        return ""

    def _should_rewrite_follow_up(self, query: str) -> bool:
        query_clean = str(query or "").strip()
        if not query_clean:
            return False

        tokens = re.findall(r"[a-z0-9]+", query_clean.lower())
        if len(tokens) <= 3:
            followup_tokens = {"price", "cost", "charges", "timing", "time", "when", "how", "much", "available"}
            return any(token in followup_tokens for token in tokens)

        if self._SHORT_FOLLOWUP_PATTERN.match(query_clean):
            return True

        followup_markers = (
            "how much",
            "what about",
            "and price",
            "and timing",
            "and cost",
            "how long",
        )
        return any(marker in query_clean.lower() for marker in followup_markers)

    @staticmethod
    def _apply_correction_inference(extracted: dict[str, Any], existing_facts: dict[str, Any]) -> None:
        """
        If the user sends a correction without repeating the original field name,
        map generic values to the most likely previously-set key.
        Example: "change it to 3pm" -> updates existing departure_time.
        """
        if "mentioned_time" in extracted:
            inferred_targets = ("departure_time", "pickup_time", "flight_time", "booking_time")
            for target in inferred_targets:
                if target in existing_facts:
                    extracted[target] = extracted.pop("mentioned_time")
                    break

        if "mentioned_day" in extracted:
            inferred_targets = ("departure_day", "booking_day")
            for target in inferred_targets:
                if target in existing_facts:
                    extracted[target] = extracted.pop("mentioned_day")
                    break


# Global singleton
conversation_memory_service = ConversationMemoryService()
