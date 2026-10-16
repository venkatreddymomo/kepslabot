"""Booking methods for ChatService (extracted mixin).

Behavior-preserving split: these 27 room/spa/transport booking
methods were moved verbatim out of services/chat_service.py. ChatService
inherits ChatServiceBookingMixin, so ``self.*`` resolution is unchanged.
This module never imports chat_service — no import cycle.
"""
import logging
import re
from datetime import date
from typing import Any

from schemas.chat import (
    ConversationContext,
    ConversationState,
    IntentType,
    IntentResult,
    MessageRole,
)
from config.settings import settings
from services.ticketing_service import ticketing_service
from handlers import handler_registry
from handlers.base_handler import HandlerResult

logger = logging.getLogger("services.chat_service")


class ChatServiceBookingMixin:
    """Room / spa / transport booking-flow methods mixed into ChatService."""

    def _rb_team_only_hotel(self) -> bool:
        """True when this hotel has no booking engine, so the booking flow is
        'share your details and the team confirms' rather than a website link.
        Opt-in per hotel; any hotel not listed behaves exactly as before."""
        try:
            from api.routes.chat import _booking_team_only_for_hotel
            from services.db_config_service import db_config_service
            return bool(_booking_team_only_for_hotel(db_config_service.get_current_hotel_code()))
        except Exception:
            return False

    def _rb_live_rates_enabled(self) -> bool:
        """True when this team-only hotel has a live-rates booking-engine link.

        Mango only: hotels set up in the Bot Training -> Pre-booking tab (T24
        onward) never take this path, even if listed in the setting."""
        try:
            from services import prebooking_settings_service
            from services.booking_assistance_state import live_rates_url_template
            from services.db_config_service import db_config_service

            code = db_config_service.get_current_hotel_code()
            if prebooking_settings_service.is_admin_managed(str(code or "").strip().lower()):
                return False
            return bool(live_rates_url_template(code))
        except Exception:
            return False

    async def _rb_live_rates_reply(
        self, context: Any, pd: dict, user_message: str, prop: str, submitted: bool = False
    ) -> str:
        """One line pointing at "Book on website" and the team form, plus the dates marker.

        The website button opens the hotel's own booking engine with the guest's dates; when
        they gave none it opens tonight to tomorrow, as the hotel's website does, and
        they pick dates there. The bot never states a price itself."""
        from datetime import date, datetime, timedelta
        from zoneinfo import ZoneInfo

        today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        earlier = [
            str(getattr(m, "content", "") or "")
            for m in (getattr(context, "messages", None) or [])
            if str(getattr(getattr(m, "role", ""), "value", getattr(m, "role", ""))) == "user"
            and str(getattr(m, "content", "") or "").strip() != str(user_message or "").strip()
        ]
        ci_raw, co_raw = await self._rb_extract_stay_dates_iso(user_message, earlier)

        def _parse(value: str) -> date | None:
            try:
                return date.fromisoformat(str(value or "").strip())
            except ValueError:
                return None

        ci, co = _parse(ci_raw), _parse(co_raw)
        if ci is not None and ci < today:
            ci, co = None, None
        if ci is not None and (co is None or co <= ci):
            co = ci + timedelta(days=1)
        guest_dates = ci is not None
        if ci is None:
            ci, co = today, today + timedelta(days=1)
        where = f"**{prop}**" if prop else "the hotel"
        if submitted:
            # The booking form is spent: just the website link, nothing about the request.
            when = f" from {ci.strftime('%d %b %Y')} to {co.strftime('%d %b %Y')}" if guest_dates else ""
            pick = "" if guest_dates else " - just pick your dates there"
            text = f"You can check live availability and rates for {where}{when} on our website using Book on website{pick}."
        elif guest_dates:
            when = f" from {ci.strftime('%d %b %Y')} to {co.strftime('%d %b %Y')}"
            text = (
                f"You can see live rates and availability for {where}{when} and book directly using Book on "
                "website, or share your booking details and our team will get back to you to confirm."
            )
        else:
            text = (
                f"You can see live rates and availability for {where} using Book on website - just pick your "
                "dates there - or share your booking details and our team will get back to you to confirm."
            )
        return text + f"\n<<SHOW_LIVE_RATES: {ci.isoformat()}|{co.isoformat()}>>"

    def _rb_live_marker_from_wording(self, check_in: str, check_out: str) -> str:
        """<<SHOW_LIVE_RATES>> for dates the guest stated ('10 Nov', '12 Nov'), or "" when
        they cannot be read - the button then opens tonight to tomorrow."""
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        today = datetime.now(ZoneInfo("Asia/Kolkata")).date()

        def _read(value: str):
            try:
                d = self._parse_compact_calendar_date(value, reference_year=today.year)
            except Exception:
                return None
            if d is not None and d < today:
                try:
                    d = d.replace(year=d.year + 1)
                except ValueError:
                    return None
            return d

        ci, co = _read(check_in), _read(check_out)
        if ci is None:
            return ""
        if co is None or co <= ci:
            co = ci + timedelta(days=1)
        return f"\n<<SHOW_LIVE_RATES: {ci.isoformat()}|{co.isoformat()}>>"

    def _rb_followup_clause(self, *, below: bool = False) -> str:
        """Trailing clause after a booking create/update line.

        A team-only hotel has no booking engine, so pointing the guest at a
        website link strands them — the same reason `_rb_booking_link_reply`
        already branches on `_rb_team_only_hotel()`. That guard was applied
        there and on the "already with the team" path, but MISSED on the
        create / date-update paths, which is why a team-only hotel was still
        being told to "proceed via the website link".

        Hotels NOT on the opt-in list (Zone) return exactly the wording they
        returned before this helper existed, byte for byte — `below` selects
        between the two pre-existing variants ("website link below," vs
        "website link,") so no caller's copy changes.
        """
        if self._rb_team_only_hotel():
            return "Our team will get back to you to confirm."
        if below:
            return "You can proceed via the website link below, or our team will get back to assist you."
        return "You can proceed via the website link, or our team will get back to assist you."

    def _rb_booking_link_reply(
        self, prop: str, check_in: str = "", check_out: str = "", context: Any = None
    ) -> str:
        """The two-option booking reply: complete on the website OR team follow-up, + the button marker.

        For a team-only hotel (no booking engine) there is no website link to
        point at, so the copy instead sends the guest to the details button —
        pointing at a link that isn't rendered stranded the guest. Hotels not on
        that opt-in list keep the original wording exactly.
        """
        _team_only = self._rb_team_only_hotel()
        if _team_only:
            # ALREADY SENT? TELL THEM WHERE IT STANDS, DON'T ASK AGAIN.
            # The button is spent once the form is submitted, so repeating
            # "share your booking details using the button below" points at
            # something that is no longer on screen. The cleanup downstream
            # removes that sentence, which left this reply as a bare "Great
            # choice!" and nothing else - worse than the original bug. Say the
            # booking back to them instead, the same way the rebook path does.
            try:
                from services.booking_assistance_state import booking_ledger, details_submitted
                if context is not None and details_submitted(context):
                    _rows = [r for r in booking_ledger(context) if r.get("details_submitted")]
                    _row = _rows[-1] if _rows else {}
                    _when = self._rb_dates_phrase(
                        str(_row.get("check_in") or check_in),
                        str(_row.get("check_out") or check_out),
                    )
                    _prop = str(_row.get("property") or prop)
                    return (
                        f"Your booking request for **{_prop}**{_when} is already with our team, "
                        "and they will get back to you to confirm."
                        + (" You can also book directly on our website using Book on website."
                           if self._rb_live_rates_enabled() else "")
                        + " If you would like to change the dates or add another room, just tell me here."
                    )
            except Exception:
                pass
            if self._rb_live_rates_enabled():
                # Mango: both buttons - book on the hotel's own engine, or the team form.
                return (
                    f"Great choice! For your stay at **{prop}**"
                    + self._rb_dates_phrase(check_in, check_out)
                    + ", you can book directly at live rates using Book on website, or share your"
                    " booking details and our team will get back to you to confirm."
                    + f"\n<<SHOW_BOOKING_BUTTON: {prop}>>"
                    + self._rb_live_marker_from_wording(check_in, check_out)
                )
            return (
                f"Great choice! For your stay at **{prop}**"
                + self._rb_dates_phrase(check_in, check_out)
                + ", please share your booking details using the button below, and our team"
                " will get back to you to confirm."
                + f"\n<<SHOW_BOOKING_BUTTON: {prop}>>"
            )
        return (
            f"Great choice! You can complete your booking for **{prop}**"
            + self._rb_dates_phrase(check_in, check_out)
            + " via the website link below, or our team will get back to assist you."
            + f"\n<<SHOW_BOOKING_BUTTON: {prop}>>"
        )

    async def _rb_create_booking(
        self, *, context: ConversationContext, pd: dict, prop: str, scope_key: str,
        scope_ids: list[str], check_in: str, check_out: str,
        selected_phase_context: dict[str, Any] | None, entity_override: str = "",
    ) -> str:
        """Build the payload, fire-and-forget the create, and record the booking. Returns the FMS entity."""
        import logging as _lg, asyncio as _asyncio
        room = str((pd.get("room_type") or pd.get("room_name") or "") if pd else "").strip()
        issue = self._rb_make_issue(prop, room, check_in, check_out)
        phase_text = ""
        if isinstance(selected_phase_context, dict):
            phase_text = str(selected_phase_context.get("selected_phase_name") or "").strip()
        conv_uid = str(getattr(context, "conversation_uid", "") or (pd.get("conversation_uid") if pd else "") or "").strip()
        payload = ticketing_service.build_lumira_ticket_payload(
            context=context, issue=issue, message=issue,
            category="room_booking", sub_category="room_booking",
            priority="LOW", phase=phase_text, source="room_booking_intent",
        )
        payload["service_id"] = "room_booking_request"
        if prop:
            payload["property_name"] = prop          # find this row later (update / cancel)
        if conv_uid:
            payload["_conversation_uid"] = conv_uid  # scope finds to THIS conversation
        payload["room_number"] = ""                  # pre-arrival: never a room number
        # Use the entity captured when the new property was in focus (post-confirm
        # create), so a drifted scope on the yes/no turn can't mis-route the FMS id.
        if str(entity_override or "").strip():
            payload["organisation_id"] = str(entity_override).strip()
        entity = str(payload.get("organisation_id") or "").strip().lower()
        bookings = list(pd.get("_rb_bookings") or [])
        bookings.append({
            "property": prop, "entity": entity, "scope_key": scope_key,
            "check_in": check_in, "check_out": check_out,
        })
        pd["_rb_bookings"] = bookings
        _coro = self._create_silent_room_booking_ticket_bg(context=context, payload=payload, prop=prop, pd=pd)
        try:
            _asyncio.get_running_loop().create_task(_coro)
        except RuntimeError:
            await _coro
        _lg.getLogger(__name__).info(
            "RB booking recorded: property=%r entity=%s dates=%r-%r session=%s",
            prop, entity, check_in, check_out, getattr(context, "session_id", "?"),
        )
        return entity

    @staticmethod
    def _looks_like_booking_confirmation_response(response_text: str) -> bool:
        text = str(response_text or "").strip().lower()
        if not text:
            return False
        positive_markers = (
            "booking has been confirmed",
            "booking is confirmed",
            "successfully booked",
            "booking confirmed",
            "reservation confirmed",
            "booking request has been received",
            "request has been received",
            "staff team will verify it and get back",
        )
        if any(marker in text for marker in positive_markers):
            return True
        return ("booked" in text or "confirmed" in text) and "order" not in text

    def _infer_transaction_booking_sub_category(
        self,
        *,
        pending_data: dict[str, Any],
        response_text: str,
        matched_case: str = "",
    ) -> str:
        pending = pending_data if isinstance(pending_data, dict) else {}
        explicit = self._first_non_empty(
            pending.get("sub_category"),
            pending.get("booking_sub_category"),
            pending.get("booking_type"),
        ).lower().replace(" ", "_")
        if explicit in {"spa_booking", "room_booking", "transport_booking"}:
            return explicit
        explicit_table = explicit == "table_booking"

        service_name = self._first_non_empty(
            pending.get("service_name"),
            pending.get("restaurant_name"),
            pending.get("booking_service"),
            pending.get("service"),
        ).lower()
        primary_text = " ".join(
            [
                str(response_text or "").lower(),
                service_name,
                str(pending.get("room_type") or "").lower(),
                str(pending.get("check_in") or pending.get("stay_checkin_date") or "").lower(),
                str(pending.get("check_out") or pending.get("stay_checkout_date") or "").lower(),
                str(pending.get("stay_date_range") or "").lower(),
            ]
        )
        if any(
            marker in primary_text
            for marker in ("room", "suite", "stay", "check in", "check-in", "checkin", "checkout", "check-out")
        ) or any(
            pending.get(key)
            for key in ("room_type", "check_in", "check_out", "stay_checkin_date", "stay_checkout_date", "stay_date_range")
        ):
            return "room_booking"
        if any(
            marker in primary_text
            for marker in (
                "transport",
                "airport transfer",
                "airport pickup",
                "airport drop",
                "pickup",
                "drop",
                "cab",
                "taxi",
                "ride",
                "shuttle",
                "chauffeur",
            )
        ):
            return "transport_booking"
        if any(marker in primary_text for marker in ("spa", "massage", "wellness", "therapy", "treatment")):
            return "spa_booking"
        if any(
            marker in primary_text
            for marker in (
                "table",
                "restaurant",
                "reservation",
                "book table",
                "dining",
            )
        ):
            return "table_booking"
        case_hint = str(matched_case or "").lower()
        if any(
            marker in case_hint
            for marker in ("room booking", "book room", "stay booking", "check in", "check-out")
        ):
            return "room_booking"
        if any(
            marker in case_hint
            for marker in (
                "transport",
                "airport transfer",
                "airport pickup",
                "airport drop",
                "pickup",
                "drop",
                "cab",
                "taxi",
                "ride",
                "shuttle",
                "chauffeur",
            )
        ):
            return "transport_booking"
        if any(marker in case_hint for marker in ("spa", "massage", "wellness", "therapy", "treatment")):
            return "spa_booking"
        if any(marker in case_hint for marker in ("table", "restaurant", "reservation", "book table", "dining")):
            return "table_booking"
        if explicit_table and service_name and not any(
            marker in service_name
            for marker in (
                "room",
                "suite",
                "spa",
                "massage",
                "transport",
                "airport",
                "pickup",
                "drop",
                "cab",
                "taxi",
                "shuttle",
                "chauffeur",
            )
        ):
            return "table_booking"
        return ""

    @staticmethod
    def _is_room_booking_flow(
        raw_intent: str,
        current_pending_action: str | None,
        pending_action_target: str | None,
        pending_data: dict[str, Any],
        response_text: str,
    ) -> bool:
        raw = str(raw_intent or "").strip().lower()
        current = str(current_pending_action or "").strip().lower()
        target = str(pending_action_target or "").strip().lower()
        combined = " ".join(
            part for part in (
                raw,
                current,
                target,
                str(response_text or "").lower(),
                " ".join(str(k).lower() for k in (pending_data or {}).keys()),
            )
            if part
        )
        if "room_booking" in combined or "stay_booking" in combined:
            return True
        if "select_room_type" in combined or "collect_room_booking_details" in combined:
            return True
        return False

    def _build_room_booking_review_prompt(self, pending_data: dict[str, Any]) -> str:
        pending = pending_data if isinstance(pending_data, dict) else {}
        room_type = self._first_non_empty(
            pending.get("room_type"),
            pending.get("room_name"),
            pending.get("service_name"),
            "your selected room",
        )
        if room_type and self._is_generic_room_reference(room_type):
            room_type = "your selected room"
        check_in = self._first_non_empty(
            pending.get("check_in"),
            pending.get("stay_checkin_date"),
            pending.get("checkin_date"),
        )
        check_out = self._first_non_empty(
            pending.get("check_out"),
            pending.get("stay_checkout_date"),
            pending.get("checkout_date"),
        )
        guests = self._first_non_empty(
            pending.get("guest_count"),
            pending.get("party_size"),
            pending.get("guests"),
        )

        parts: list[str] = [room_type]
        if check_in and check_out:
            parts.append(f"from {check_in} to {check_out}")
        elif self._first_non_empty(pending.get("stay_date_range")):
            parts.append(f"for {self._first_non_empty(pending.get('stay_date_range'))}")
        if guests:
            parts.append(f"for {guests} guests")

        summary = " ".join(part for part in parts if str(part or "").strip()).strip()
        if not summary:
            summary = "your room booking request"
        return (
            f"Please review your room booking request: {summary}. "
            "Would you like to confirm this booking?"
        )

    def _build_room_booking_confirmation_success_response(self, pending_data: dict[str, Any]) -> str:
        pending = pending_data if isinstance(pending_data, dict) else {}
        room_type = self._first_non_empty(
            pending.get("room_type"),
            pending.get("room_name"),
            pending.get("service_name"),
            "your selected room",
        )
        if room_type and self._is_generic_room_reference(room_type):
            room_type = "your selected room"
        check_in = self._first_non_empty(
            pending.get("check_in"),
            pending.get("stay_checkin_date"),
            pending.get("checkin_date"),
        )
        check_out = self._first_non_empty(
            pending.get("check_out"),
            pending.get("stay_checkout_date"),
            pending.get("checkout_date"),
        )
        guests = self._first_non_empty(
            pending.get("guest_count"),
            pending.get("party_size"),
            pending.get("guests"),
        )

        details: list[str] = [room_type]
        if check_in and check_out:
            details.append(f"from {check_in} to {check_out}")
        elif self._first_non_empty(pending.get("stay_date_range")):
            details.append(f"for {self._first_non_empty(pending.get('stay_date_range'))}")
        if guests:
            details.append(f"for {guests} guests")

        details_text = " ".join(part for part in details if str(part or "").strip()).strip()
        if not details_text:
            details_text = "your requested stay"
        return (
            f"Your room booking request has been received: {details_text}. "
            "Our staff team will verify it and get back to you shortly. Is there anything else I can help you with?"
        )

    def _missing_room_booking_fields(
        self,
        pending_data: dict[str, Any],
        memory_facts: dict[str, Any],
    ) -> list[str]:
        pending = pending_data if isinstance(pending_data, dict) else {}
        facts = memory_facts if isinstance(memory_facts, dict) else {}

        room_type = self._first_non_empty(
            pending.get("room_type"),
            pending.get("room_name"),
            pending.get("selected_room_type"),
            facts.get("room_type"),
            facts.get("room_name"),
            facts.get("selected_room_type"),
        )
        if room_type and self._is_generic_room_reference(room_type):
            room_type = ""

        party_size = self._first_non_empty(
            pending.get("party_size"),
            pending.get("guest_count"),
            pending.get("guests"),
            facts.get("party_size"),
            facts.get("guest_count"),
            facts.get("guests"),
        )
        checkin_date = self._first_non_empty(
            pending.get("stay_checkin_date"),
            pending.get("check_in"),
            pending.get("checkin_date"),
            facts.get("stay_checkin_date"),
            facts.get("check_in"),
            facts.get("checkin_date"),
        )
        checkout_date = self._first_non_empty(
            pending.get("stay_checkout_date"),
            pending.get("check_out"),
            pending.get("checkout_date"),
            facts.get("stay_checkout_date"),
            facts.get("check_out"),
            facts.get("checkout_date"),
        )
        date_range = self._first_non_empty(
            pending.get("stay_date_range"),
            facts.get("stay_date_range"),
            pending.get("booking_day"),
            facts.get("booking_day"),
        )
        integration = pending.get("_integration", {})
        if not isinstance(integration, dict):
            integration = {}
        phase_hint = self._normalize_phase_identifier(
            pending.get("phase")
            or integration.get("phase")
            or facts.get("phase")
        )
        flow_hint = self._normalize_flow_identifier(
            integration.get("flow")
            or integration.get("bot_mode")
        )
        if phase_hint in {"during_stay", "post_checkout"}:
            requires_contact_details = False
        elif phase_hint in {"pre_booking", "pre_checkin"}:
            requires_contact_details = True
        elif flow_hint in {"engage", "booking", "booking_bot", "pre_booking", "prebooking"}:
            requires_contact_details = True
        else:
            requires_contact_details = True
        guest_name = self._first_non_empty(
            pending.get("guest_name"),
            integration.get("guest_name"),
            facts.get("guest_name"),
        )
        guest_phone = self._first_non_empty(
            pending.get("guest_phone"),
            integration.get("guest_phone"),
            integration.get("wa_number"),
            facts.get("guest_phone"),
        )

        missing: list[str] = []
        if not room_type:
            missing.append("room_type")
        if not party_size:
            missing.append("guest_count")
        if not checkin_date and not date_range:
            missing.append("checkin_date")
        if not checkout_date and not date_range:
            missing.append("checkout_date")
        if requires_contact_details:
            if not guest_name:
                missing.append("guest_name")
            if not guest_phone:
                missing.append("guest_phone")
        return missing

    def _build_room_booking_missing_details_prompt(
        self,
        missing_fields: list[str],
        pending_data: dict[str, Any],
    ) -> str:
        room_type = self._first_non_empty(
            pending_data.get("room_type"),
            pending_data.get("room_name"),
        )
        if room_type and self._is_generic_room_reference(room_type):
            room_type = ""
        parts: list[str] = []
        requested_room_type = self._first_non_empty(
            pending_data.get("requested_room_type"),
        )
        room_type_candidates_raw = pending_data.get("room_type_candidates")
        room_type_candidates = [
            str(candidate or "").strip()
            for candidate in (room_type_candidates_raw if isinstance(room_type_candidates_raw, list) else [])
            if str(candidate or "").strip()
        ]
        if "room_type" in missing_fields and requested_room_type and room_type_candidates:
            options = ", ".join(room_type_candidates[:8])
            parts.append(
                f'I could not match "{requested_room_type}" to our available room types ({options}).'
            )
        if room_type:
            parts.append(f"I can proceed with {room_type}.")
        else:
            parts.append("I can help with your room booking.")

        asks: list[str] = []
        if "room_type" in missing_fields:
            asks.append("preferred room type")
        if "guest_count" in missing_fields:
            asks.append("number of guests")
        if "checkin_date" in missing_fields:
            asks.append("check-in date")
        if "checkout_date" in missing_fields:
            asks.append("check-out date")
        if "guest_name" in missing_fields:
            asks.append("full name")
        if "guest_phone" in missing_fields:
            asks.append("contact phone number")

        if asks:
            parts.append("Please share your " + ", ".join(asks) + ".")
        else:
            parts.append("Please share your check-in and check-out dates.")

        return " ".join(parts).strip()

    @staticmethod
    def _build_room_booking_missing_suggested_actions(missing_fields: list[str]) -> list[str]:
        actions: list[str] = []
        if "room_type" in missing_fields:
            actions.append("Share preferred room type")
        if "guest_count" in missing_fields:
            actions.append("Share number of guests")
        if "checkin_date" in missing_fields or "checkout_date" in missing_fields:
            actions.append("Share check-in and check-out dates")
        if "guest_name" in missing_fields:
            actions.append("Share full name")
        if "guest_phone" in missing_fields:
            actions.append("Share contact number")
        deduped: list[str] = []
        seen: set[str] = set()
        for item in actions:
            key = str(item or "").strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(str(item))
        return deduped[:6]

    @staticmethod
    def _clear_room_booking_date_fields(
        pending_data: dict[str, Any],
        *,
        clear_check_in: bool = False,
        clear_check_out: bool = False,
    ) -> None:
        if not isinstance(pending_data, dict):
            return
        if clear_check_in:
            pending_data.pop("check_in", None)
            pending_data.pop("stay_checkin_date", None)
            pending_data.pop("checkin_date", None)
        if clear_check_out:
            pending_data.pop("check_out", None)
            pending_data.pop("stay_checkout_date", None)
            pending_data.pop("checkout_date", None)
        if clear_check_in or clear_check_out:
            pending_data.pop("stay_date_range", None)

    def _validate_room_booking_dates_in_pending(
        self,
        pending_data: dict[str, Any],
    ) -> dict[str, Any] | None:
        pending = pending_data if isinstance(pending_data, dict) else {}
        check_in_text = self._first_non_empty(
            pending.get("check_in"),
            pending.get("stay_checkin_date"),
            pending.get("checkin_date"),
        )
        check_out_text = self._first_non_empty(
            pending.get("check_out"),
            pending.get("stay_checkout_date"),
            pending.get("checkout_date"),
        )
        if not check_in_text and not check_out_text:
            return None

        today_local = date.today()
        current_year = today_local.year
        parsed_check_in = (
            self._parse_compact_calendar_date(check_in_text, reference_year=current_year)
            if check_in_text
            else None
        )
        parsed_check_out = (
            self._parse_compact_calendar_date(check_out_text, reference_year=current_year)
            if check_out_text
            else None
        )

        if (
            parsed_check_in is not None
            and parsed_check_out is not None
            and parsed_check_out < parsed_check_in
            and not re.search(r"\b\d{4}\b", str(check_out_text))
        ):
            try:
                adjusted_checkout = parsed_check_out.replace(year=parsed_check_out.year + 1)
                if adjusted_checkout > parsed_check_in:
                    parsed_check_out = adjusted_checkout
            except ValueError:
                pass

        if parsed_check_in is not None:
            normalized_check_in = parsed_check_in.isoformat()
            pending["check_in"] = normalized_check_in
            pending["stay_checkin_date"] = normalized_check_in
        if parsed_check_out is not None:
            normalized_check_out = parsed_check_out.isoformat()
            pending["check_out"] = normalized_check_out
            pending["stay_checkout_date"] = normalized_check_out
        if parsed_check_in is not None and parsed_check_out is not None:
            pending["stay_date_range"] = f"{parsed_check_in.isoformat()} to {parsed_check_out.isoformat()}"

        today_label = self._format_calendar_date_full(today_local)
        if parsed_check_in is not None and parsed_check_in < today_local:
            check_in_label = self._format_calendar_date_full(parsed_check_in)
            self._clear_room_booking_date_fields(pending, clear_check_in=True, clear_check_out=False)
            return {
                "code": "room_booking_checkin_in_past",
                "response_text": (
                    f"Your check-in date ({check_in_label}) is in the past. "
                    f"Today's date is {today_label}. Please share a future check-in date."
                ),
                "suggested_actions": [
                    "Share future check-in date",
                    "Share check-in and check-out dates",
                ],
            }

        if parsed_check_out is not None and parsed_check_out < today_local:
            check_out_label = self._format_calendar_date_full(parsed_check_out)
            self._clear_room_booking_date_fields(pending, clear_check_in=False, clear_check_out=True)
            return {
                "code": "room_booking_checkout_in_past",
                "response_text": (
                    f"Your check-out date ({check_out_label}) is in the past. "
                    f"Today's date is {today_label}. Please share a future check-out date."
                ),
                "suggested_actions": [
                    "Share future check-out date",
                    "Share check-in and check-out dates",
                ],
            }

        if (
            parsed_check_in is not None
            and parsed_check_out is not None
            and parsed_check_out <= parsed_check_in
        ):
            check_in_label = self._format_calendar_date_full(parsed_check_in)
            check_out_label = self._format_calendar_date_full(parsed_check_out)
            self._clear_room_booking_date_fields(pending, clear_check_in=False, clear_check_out=True)
            return {
                "code": "room_booking_checkout_not_after_checkin",
                "response_text": (
                    f"Check-out must be after check-in. I currently have check-in on {check_in_label} "
                    f"and check-out on {check_out_label}. Please share a check-out date after check-in."
                ),
                "suggested_actions": [
                    "Share updated check-out date",
                    "Share check-in and check-out dates",
                ],
            }

        return None

    def _extract_room_booking_slot_updates(
        self,
        message: str,
        current_pending_action: str | None,
        state: ConversationState,
        pending_data: dict[str, Any],
        memory_facts: dict[str, Any],
        conversation_context: ConversationContext | None = None,
    ) -> dict[str, Any]:
        """
        Deterministically extract room-booking slot updates from compact user
        replies so context doesn't depend only on model JSON fidelity.
        """
        pending = pending_data if isinstance(pending_data, dict) else {}
        facts = memory_facts if isinstance(memory_facts, dict) else {}
        msg = str(message or "").strip()
        msg_lower = msg.lower()
        if not msg_lower:
            return {}

        room_context = self._is_room_detail_collection_context(
            current_pending_action=current_pending_action,
            state=state,
            pending_data=pending,
            memory_facts=facts,
            message=msg_lower,
        )
        if not room_context:
            return {}

        month_map = {
            "jan": "Jan", "january": "Jan",
            "feb": "Feb", "february": "Feb",
            "mar": "Mar", "march": "Mar",
            "apr": "Apr", "april": "Apr",
            "may": "May",
            "jun": "Jun", "june": "Jun",
            "jul": "Jul", "july": "Jul",
            "aug": "Aug", "august": "Aug",
            "sep": "Sep", "sept": "Sep", "september": "Sep",
            "oct": "Oct", "october": "Oct",
            "nov": "Nov", "november": "Nov",
            "dec": "Dec", "december": "Dec",
        }
        month_token = (
            r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
            r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
        )

        def _month_norm(token: str) -> str:
            return month_map.get(str(token or "").strip().lower(), "")

        def _format(month: str, day: str) -> str:
            m = _month_norm(month)
            try:
                d = int(str(day or "").strip())
            except (TypeError, ValueError):
                return ""
            if not m or d < 1 or d > 31:
                return ""
            return f"{m} {d:02d}"

        updates: dict[str, Any] = {}

        range_month_first = re.search(
            rf"\b(?P<month>{month_token})\s*(?P<start>\d{{1,2}})\s*(?:-|to|through|till|until)\s*(?P<end>\d{{1,2}})(?:[,\s]+(?P<guests>\d{{1,2}}))?\b",
            msg_lower,
        )
        range_month_last = re.search(
            rf"\b(?P<start>\d{{1,2}})\s*(?:-|to|through|till|until)\s*(?P<end>\d{{1,2}})\s*(?P<month>{month_token})(?:[,\s]+(?P<guests>\d{{1,2}}))?\b",
            msg_lower,
        )

        chosen = range_month_first or range_month_last
        if chosen:
            month = str(chosen.group("month") or "")
            start = str(chosen.group("start") or "")
            end = str(chosen.group("end") or "")
            start_fmt = _format(month, start)
            end_fmt = _format(month, end)
            if start_fmt and end_fmt:
                updates["stay_checkin_date"] = start_fmt
                updates["stay_checkout_date"] = end_fmt
                updates["stay_date_range"] = f"{start_fmt} to {end_fmt}"
            guests_raw = chosen.group("guests")
            if guests_raw:
                try:
                    guests_value = int(guests_raw)
                    if 1 <= guests_value <= 20:
                        updates["guest_count"] = guests_value
                except (TypeError, ValueError):
                    pass

        guests_match = re.search(r"\b(?:for|party of)?\s*(\d{1,2})\s*(?:guests?|people|persons|pax)\b", msg_lower)
        if guests_match:
            try:
                guests_value = int(guests_match.group(1))
                if 1 <= guests_value <= 20:
                    updates["guest_count"] = guests_value
            except (TypeError, ValueError):
                pass

        # Bare numeric reply in room-detail collection generally means guest count.
        if re.fullmatch(r"\d{1,2}", msg_lower):
            known_dates = self._first_non_empty(
                pending.get("check_in"),
                pending.get("stay_checkin_date"),
                pending.get("checkin_date"),
                facts.get("check_in"),
                facts.get("stay_checkin_date"),
                facts.get("checkin_date"),
            ) and self._first_non_empty(
                pending.get("check_out"),
                pending.get("stay_checkout_date"),
                pending.get("checkout_date"),
                facts.get("check_out"),
                facts.get("stay_checkout_date"),
                facts.get("checkout_date"),
            )
            if known_dates:
                try:
                    guests_value = int(msg_lower)
                    if 1 <= guests_value <= 20:
                        updates["guest_count"] = guests_value
                except (TypeError, ValueError):
                    pass

        persisted_candidates_raw = pending.get("room_type_candidates")
        persisted_candidates = [
            str(candidate or "").strip()
            for candidate in (persisted_candidates_raw if isinstance(persisted_candidates_raw, list) else [])
            if str(candidate or "").strip()
        ]
        room_candidates = self._merge_room_type_candidates(
            persisted_candidates,
            self._extract_room_type_candidates_from_context_messages(conversation_context),
        )
        if room_candidates:
            updates["room_type_candidates"] = room_candidates
        matched_room = self._match_room_type_candidate_from_message(msg_lower, room_candidates)
        if matched_room:
            updates["room_type"] = matched_room

        if not str(updates.get("guest_phone") or "").strip():
            guest_phone = self._extract_guest_phone_candidate(msg)
            if guest_phone:
                updates["guest_phone"] = guest_phone
        if not str(updates.get("guest_name") or "").strip():
            guest_name = self._extract_guest_name_candidate(msg)
            if guest_name:
                updates["guest_name"] = guest_name

        return updates

    @staticmethod
    def _is_room_booking_pending_action(action: str | None) -> bool:
        pending = str(action or "").strip().lower()
        return pending in {
            "room_booking",
            "collect_room_booking_details",
            "select_room_type",
            "confirm_room_booking",
            "confirm_room_availability_check",
        }

    def _is_room_booking_intent_context(
        self,
        *,
        message: str,
        intent_result: IntentResult,
        context: ConversationContext,
    ) -> bool:
        pending_action = str(context.pending_action or "").strip().lower()
        if self._is_room_booking_pending_action(pending_action):
            return True

        entities = intent_result.entities if isinstance(intent_result.entities, dict) else {}
        entity_markers = (
            str(entities.get("booking_sub_category") or ""),
            str(entities.get("booking_type") or ""),
            str(entities.get("custom_intent") or ""),
            str(entities.get("resolved_intent") or ""),
            str(entities.get("service_name") or ""),
            str(entities.get("room_type") or ""),
        )
        entity_blob = " ".join(marker.lower() for marker in entity_markers if str(marker or "").strip())
        if any(marker in entity_blob for marker in ("room_booking", "room", "suite", "stay", "check in", "check-out")):
            return True

        pending_data = context.pending_data if isinstance(context.pending_data, dict) else {}
        if any(
            key in pending_data
            for key in (
                "room_type",
                "room_name",
                "stay_checkin_date",
                "stay_checkout_date",
                "check_in",
                "check_out",
                "stay_date_range",
                "guest_count",
            )
        ):
            return True

        return self._looks_like_room_stay_booking_request(message)

    def _extract_room_booking_entity_updates(self, entities: dict[str, Any]) -> dict[str, Any]:
        updates: dict[str, Any] = {}
        data = entities if isinstance(entities, dict) else {}

        room_type = self._first_non_empty(
            data.get("room_type"),
            data.get("room_name"),
        )
        if room_type and not self._is_generic_room_reference(room_type):
            updates["room_type"] = room_type

        guests_raw = self._first_non_empty(
            data.get("guest_count"),
            data.get("party_size"),
            data.get("guests"),
        )
        if guests_raw:
            try:
                guests = int(float(str(guests_raw)))
                if 1 <= guests <= 20:
                    updates["guest_count"] = guests
            except (TypeError, ValueError):
                pass

        check_in = self._first_non_empty(
            data.get("check_in"),
            data.get("stay_checkin_date"),
            data.get("checkin_date"),
        )
        check_out = self._first_non_empty(
            data.get("check_out"),
            data.get("stay_checkout_date"),
            data.get("checkout_date"),
        )
        date_range = self._first_non_empty(
            data.get("stay_date_range"),
            data.get("date_range"),
            data.get("date"),
        )
        if check_in:
            updates["stay_checkin_date"] = check_in
        if check_out:
            updates["stay_checkout_date"] = check_out
        if date_range and "to" in str(date_range).lower():
            updates["stay_date_range"] = date_range
        return updates

    async def _handle_room_booking_intent_flow(
        self,
        *,
        message: str,
        intent_result: IntentResult,
        context: ConversationContext,
        capabilities: dict[str, Any],
        db_session=None,
    ) -> HandlerResult:
        msg_lower = re.sub(r"\s+", " ", str(message or "").strip().lower())
        pending = dict(context.pending_data) if isinstance(context.pending_data, dict) else {}

        if intent_result.intent == IntentType.CONFIRMATION_NO or msg_lower in {"cancel", "stop", "no", "nope"}:
            return HandlerResult(
                response_text="No problem, I have cancelled this room booking request.",
                next_state=ConversationState.IDLE,
                pending_action=None,
                pending_data={},
                suggested_actions=["Show available room types", "Ask another question"],
            )

        entity_updates = self._extract_room_booking_entity_updates(
            intent_result.entities if isinstance(intent_result.entities, dict) else {}
        )
        if entity_updates:
            pending.update(entity_updates)

        slot_updates = self._extract_room_booking_slot_updates(
            message=message,
            current_pending_action=context.pending_action,
            state=context.state,
            pending_data=pending,
            memory_facts={},
            conversation_context=context,
        )
        if slot_updates:
            pending.update(slot_updates)

        persisted_candidates_raw = pending.get("room_type_candidates")
        persisted_candidates = [
            str(candidate or "").strip()
            for candidate in (persisted_candidates_raw if isinstance(persisted_candidates_raw, list) else [])
            if str(candidate or "").strip()
        ]
        room_candidates = self._merge_room_type_candidates(
            persisted_candidates,
            self._extract_room_type_candidates_from_context_messages(context),
        )
        if room_candidates:
            pending["room_type_candidates"] = room_candidates

        current_room_type = self._first_non_empty(
            pending.get("room_type"),
            pending.get("room_name"),
            pending.get("selected_room_type"),
        )
        if current_room_type:
            if self._is_generic_room_reference(current_room_type):
                pending.pop("room_type", None)
                pending.pop("room_name", None)
                pending.pop("selected_room_type", None)
            elif room_candidates:
                resolved_room = self._resolve_room_type_candidate(current_room_type, room_candidates)
                if resolved_room:
                    pending["room_type"] = resolved_room
                    pending.pop("room_name", None)
                    pending.pop("selected_room_type", None)
                    pending.pop("requested_room_type", None)
                else:
                    pending["requested_room_type"] = current_room_type
                    pending.pop("room_type", None)
                    pending.pop("room_name", None)
                    pending.pop("selected_room_type", None)

        pending.setdefault("service_name", "Room Booking")
        pending.setdefault("booking_sub_category", "room_booking")
        pending.setdefault("booking_type", "room_booking")
        if "guest_count" in pending and not str(pending.get("party_size") or "").strip():
            pending["party_size"] = str(pending.get("guest_count"))
        if str(pending.get("stay_checkin_date") or "").strip() and not str(pending.get("check_in") or "").strip():
            pending["check_in"] = str(pending.get("stay_checkin_date") or "").strip()
        if str(pending.get("stay_checkout_date") or "").strip() and not str(pending.get("check_out") or "").strip():
            pending["check_out"] = str(pending.get("stay_checkout_date") or "").strip()
        pending = self._enrich_pending_with_known_facts(
            pending_data=pending,
            context=context,
            memory_snapshot={"facts": self._memory_facts_from_context(context)},
            entities=intent_result.entities if isinstance(intent_result.entities, dict) else {},
        )
        date_validation_issue = self._validate_room_booking_dates_in_pending(pending)
        if date_validation_issue is not None:
            response_text = str(date_validation_issue.get("response_text") or "").strip()
            suggestions = date_validation_issue.get("suggested_actions")
            suggested_actions = (
                [str(item).strip() for item in suggestions if str(item).strip()]
                if isinstance(suggestions, list)
                else []
            )
            if not response_text:
                response_text = (
                    f"Today's date is {self._format_calendar_date_full(date.today())}. "
                    "Please share valid future check-in and check-out dates."
                )
            return HandlerResult(
                response_text=response_text,
                next_state=ConversationState.AWAITING_INFO,
                pending_action="collect_room_booking_details",
                pending_data=pending,
                suggested_actions=suggested_actions or ["Share check-in and check-out dates"],
                metadata={
                    "room_booking_flow": True,
                    "room_booking_date_validation": str(date_validation_issue.get("code") or ""),
                },
            )

        missing = self._missing_room_booking_fields(
            pending_data=pending,
            memory_facts=self._memory_facts_from_context(context),
        )
        pending_action = str(context.pending_action or "").strip().lower()
        if (
            intent_result.intent == IntentType.CONFIRMATION_YES
            and pending_action in {"confirm_booking", "confirm_room_booking", "confirm_room_availability_check"}
            and not missing
        ):
            booking_handler = handler_registry.get_handler(IntentType.TABLE_BOOKING)
            if booking_handler is not None:
                context.pending_action = "confirm_booking"
                context.pending_data = pending
                return await booking_handler.handle(
                    message,
                    intent_result,
                    context,
                    capabilities,
                    db_session,
                )

        if missing:
            response_text = self._build_room_booking_missing_details_prompt(
                missing_fields=missing,
                pending_data=pending,
            )
            return HandlerResult(
                response_text=response_text,
                next_state=ConversationState.AWAITING_INFO,
                pending_action="collect_room_booking_details",
                pending_data=pending,
                suggested_actions=(
                    self._build_room_booking_missing_suggested_actions(missing)
                    or ["Show available room types", "Share number of guests", "Share check-in and check-out dates"]
                ),
                metadata={"room_booking_flow": True},
            )

        pending.pop("requested_room_type", None)
        pending.pop("room_type_candidates", None)
        response_text = self._build_room_booking_review_prompt(pending)
        confirmation_phrase = str(getattr(settings, "chat_confirmation_phrase", "yes confirm")).strip() or "yes confirm"
        if bool(getattr(settings, "chat_require_strict_confirmation_phrase", True)):
            response_text = self._ensure_explicit_confirmation_instruction(
                response_text=response_text,
                confirmation_phrase=confirmation_phrase,
            )
        return HandlerResult(
            response_text=response_text,
            next_state=ConversationState.AWAITING_CONFIRMATION,
            pending_action="confirm_booking",
            pending_data=pending,
            suggested_actions=[confirmation_phrase, "cancel"],
            metadata={"room_booking_flow": True},
        )

    @staticmethod
    def _looks_like_room_booking_detail_followup(message: str) -> bool:
        msg_lower = str(message or "").strip().lower()
        if not msg_lower:
            return False

        has_guest_detail = bool(
            re.search(r"\b\d{1,2}\s*(?:guests?|people|persons|pax)\b", msg_lower)
        ) or bool(re.search(r"\b(?:for|foe|fpr|fr)\s+\d{1,2}\b", msg_lower))
        has_date_detail = bool(
            re.search(
                r"\b(?:from\s+[^.?!,]{1,30}\s+to\s+[^.?!,]{1,30}|"
                r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s*\d{1,2}\s*(?:-|to)\s*\d{1,2}|"
                r"\d{1,2}(?:st|nd|rd|th)?\s*(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*|"
                r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s*\d{1,2}(?:st|nd|rd|th)?|"
                r"\d{1,2}\s*[-/]\s*\d{1,2})\b",
                msg_lower,
            )
        ) or any(marker in msg_lower for marker in ("check in", "check-in", "check out", "check-out", "checkin", "checkout"))
        has_structured_detail = ChatService._looks_like_service_detail_reply(msg_lower) and bool(
            re.search(r"\b\d{1,2}\b", msg_lower)
        )
        contact_detail_query = any(
            marker in msg_lower
            for marker in (
                "contact info",
                "contact information",
                "my contact",
                "phone number",
                "mobile number",
                "my number",
                "my name",
                "name and contact",
                "do you have my contact",
            )
        )
        return has_guest_detail or has_date_detail or has_structured_detail or contact_detail_query

    def _recent_user_turn_mentions_room_booking(self, context: ConversationContext, current_message: str) -> bool:
        current_norm = re.sub(r"\s+", " ", str(current_message or "").strip().lower())
        skipped_current = False
        for msg in reversed(context.messages):
            text = str(msg.content or "").strip()
            if not text:
                continue
            norm = re.sub(r"\s+", " ", text.lower())
            if msg.role == MessageRole.USER:
                if not skipped_current and norm == current_norm:
                    skipped_current = True
                    continue
                if self._looks_like_room_stay_booking_request(text):
                    return True
                if any(
                    marker in norm
                    for marker in (
                        "room available",
                        "room type",
                        "cheapest room",
                        "stay",
                        "check in",
                        "check-out",
                        "check out",
                        "book room",
                    )
                ):
                    return True
            elif msg.role == MessageRole.ASSISTANT:
                asks_stay_dates = (
                    ("check-in" in norm or "check in" in norm)
                    and ("check-out" in norm or "check out" in norm)
                )
                asks_guest_count = "guest" in norm or "guests" in norm
                mentions_room_flow = any(
                    marker in norm
                    for marker in (
                        "room booking",
                        "room type",
                        "for your stay",
                        "cheapest room",
                    )
                )
                if asks_stay_dates and asks_guest_count:
                    return True
                if mentions_room_flow and asks_guest_count:
                    return True
        return False

    @staticmethod
    def _looks_like_room_stay_booking_request(message: str) -> bool:
        """
        Detect hotel-stay booking language so it is not misrouted into
        slot/service booking flows.
        """
        msg_lower = str(message or "").strip().lower()
        if not msg_lower:
            return False

        # Exclude operational room-service asks.
        if any(
            term in msg_lower
            for term in ("room service", "housekeeping", "cleaning", "towel", "laundry", "maintenance")
        ):
            return False

        # Exclude obvious slot-based booking asks.
        if any(term in msg_lower for term in ("table", "appointment", "slot", "service")):
            return False

        booking_like = any(term in msg_lower for term in ("book", "booking", "reserve", "stay"))
        room_stay_like = any(
            term in msg_lower
            for term in (
                "room",
                "suite",
                "suites",
                "villa",
                "residence",
                "check in",
                "check-in",
                "check out",
                "check-out",
                "checkin",
                "checkout",
                "night",
                "nights",
            )
        )
        has_date_window = bool(
            re.search(
                r"\b(from\s+.+\s+to\s+.+|\d{1,2}(?:st|nd|rd|th)?\s*(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec))\b",
                msg_lower,
            )
        )

        has_room_enquiry_phrase = bool(
            re.search(r"\b(looking\s+for|need|want|require)\s+(?:a\s+)?room\b", msg_lower)
            or "room for" in msg_lower
        )

        return bool(room_stay_like and (booking_like or has_date_window or has_room_enquiry_phrase))

    @staticmethod
    def _looks_like_spa_booking_action(msg_lower: str) -> bool:
        text = str(msg_lower or "").strip().lower()
        if not text:
            return False
        if not any(token in text for token in ("spa", "massage", "wellness", "therapy", "treatment")):
            return False

        action_markers = (
            "book",
            "booking",
            "reserve",
            "schedule",
            "confirm",
            "i want",
            "i need",
            "please book",
            "set up",
        )
        has_action = any(marker in text for marker in action_markers)
        if not has_action:
            return False

        info_only_markers = (
            "show",
            "list",
            "what",
            "which",
            "package",
            "packages",
            "timing",
            "hours",
            "available",
            "tell me about",
        )
        if "?" in text and not has_action:
            return False
        if any(marker in text for marker in info_only_markers) and not any(
            marker in text for marker in ("book", "reserve", "schedule", "confirm")
        ):
            return False
        return True

    @staticmethod
    def _looks_like_booking_change_request(message: str) -> bool:
        text = str(message or "").strip().lower()
        if not text:
            return False
        change_markers = ("cancel", "cancellation", "modify", "change", "postpone", "reschedule", "update")
        booking_markers = (
            "booking",
            "reservation",
            "check in",
            "check-in",
            "check out",
            "check-out",
            "stay",
            "pickup",
            "drop",
            "transfer",
            "room",
            "table",
            "spa",
            "appointment",
        )
        return any(marker in text for marker in change_markers) and any(marker in text for marker in booking_markers)

    @staticmethod
    def _resolve_booking_change_target(message: str) -> str:
        text = str(message or "").strip().lower()
        if not text:
            return ""
        if any(marker in text for marker in ("pickup", "drop", "transfer", "airport", "cab", "taxi", "transport")):
            return "transport"
        if any(marker in text for marker in ("spa", "massage", "treatment", "wellness", "appointment")):
            return "spa_booking"
        if any(marker in text for marker in ("table", "restaurant", "dining", "reservation")):
            return "table_booking"
        if any(marker in text for marker in ("room", "stay", "check in", "check-in", "check out", "check-out")):
            return "room_booking"
        return ""

    @staticmethod
    def _has_spa_booking_marker(message: str) -> bool:
        text = str(message or "").strip().lower()
        if not text:
            return False
        markers = ("spa", "massage", "wellness", "therapy", "treatment", "recreation", "pool")
        return any(re.search(rf"\b{re.escape(marker)}\b", text) for marker in markers)

    @staticmethod
    def _has_transport_booking_marker(message: str) -> bool:
        text = str(message or "").strip().lower()
        if not text:
            return False
        markers = (
            "transport",
            "cab",
            "taxi",
            "pickup",
            "pick up",
            "drop",
            "airport transfer",
            "ride",
            "shuttle",
        )
        return any(re.search(rf"\b{re.escape(marker)}\b", text) for marker in markers)

    def _phase_has_room_booking_support(self, phase_services: list[dict[str, Any]]) -> bool:
        room_markers = (
            "room booking",
            "room discovery",
            "stay booking",
            "room",
            "suite",
            "check in",
            "check-out",
            "check out",
            "checkin",
            "checkout",
        )
        for service in phase_services:
            text = self._service_phase_gate_text_blob(service)
            if not text:
                continue
            for marker in room_markers:
                if re.search(rf"\b{re.escape(marker)}\b", text):
                    return True
        return False

    def _phase_has_booking_subtype_support(
        self,
        *,
        phase_services: list[dict[str, Any]],
        subtype: str,
    ) -> bool:
        subtype_key = str(subtype or "").strip().lower()
        marker_map = {
            "spa": ("spa", "massage", "wellness", "therapy", "treatment", "recreation", "pool"),
            "transport": (
                "transport",
                "cab",
                "taxi",
                "pickup",
                "pick up",
                "drop",
                "airport transfer",
                "ride",
                "shuttle",
            ),
        }
        markers = marker_map.get(subtype_key, ())
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
