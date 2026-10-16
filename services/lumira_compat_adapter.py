"""
Lumira compatibility adapter.

Maps Lumira-style request/response contracts to the internal ChatRequest/ChatResponse
without changing the bot's core ticketing logic.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from schemas.chat import ChatRequest, ChatResponse


def _clean_str(value: Any) -> str:
    return str(value or "").strip()


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = _clean_str(value).lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    return False


def _to_int(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(str(value).strip())
    except Exception:
        return None


def _to_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(str(value).strip())
    except Exception:
        return None


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _fallback_session_id(prefix: str, seed: str = "") -> str:
    normalized_seed = _clean_str(seed)
    if normalized_seed:
        return f"{prefix}_{normalized_seed}"
    return f"{prefix}_{uuid4().hex[:16]}"


def _resolve_hotel_code_from_entity_map(entity_id: Any, hotel_code: Any = "") -> str:
    """Resolve a numeric guest-journey entity id to the configured hotel code.

    WhatsApp guest-journey payloads often carry only `entity_id=6311`. Our
    tenant/runtime config is keyed by hotel code
    (`sarovar_portico_sriperumbudur`), so if we keep the numeric id as the
    hotel_code the turn loads zero hotel-scoped services and books.
    """
    from config.settings import settings

    code = _clean_str(hotel_code)
    if code and not code.isdigit():
        return code

    entity = _clean_str(entity_id) or code
    if not entity:
        return code
    from services import prebooking_settings_service

    # Entity ids set in the admin (Bot Training -> Pre-booking).
    admin_owner = prebooking_settings_service.hotel_for_entity(entity)
    if admin_owner:
        return admin_owner

    raw = _clean_str(getattr(settings, "ticketing_property_entity_map_json", ""))
    if not raw:
        return code
    try:
        import json as _json

        parsed = _json.loads(raw)
    except (ValueError, TypeError):
        return code
    if not isinstance(parsed, dict):
        return code

    for mapped_code, properties in parsed.items():
        if not isinstance(properties, dict):
            continue
        if any(_clean_str(v) == entity for v in properties.values()):
            return _clean_str(mapped_code) or code
    return code


def _resolve_widget_group(entity_id: Any, hotel_code: str) -> tuple[str, str]:
    """Resolve the real Kepsla (widget_id, group_id) for a guest-journey turn.

    Engage gets both straight off the widget payload; WhatsApp payloads carry
    neither, only a numeric entity_id. The widget/group map is keyed by hotel
    code, so reverse TICKETING_PROPERTY_ENTITY_MAP_JSON (hotel_code ->
    {property: entity_id}) to recover the code. Without this the
    KEBO_ENGAGE_SESSIONS row is written with widget_id NULL and the kebo
    dashboard — which scopes Chat History by widget_id — drops it.
    """
    import json as _json

    from config.settings import settings
    from services.customer_details_service import CustomerDetailsService

    code = _clean_str(hotel_code)
    if code and not code.isdigit():
        widget_id, group_id = CustomerDetailsService.resolve_widget_group(code)
        if widget_id:
            return widget_id, group_id

    entity = _clean_str(entity_id) or code
    if not entity:
        return "", ""
    from services import prebooking_settings_service

    admin_owner = prebooking_settings_service.hotel_for_entity(entity)
    if admin_owner:
        return CustomerDetailsService.resolve_widget_group(admin_owner)

    raw = _clean_str(getattr(settings, "ticketing_property_entity_map_json", ""))
    if not raw:
        return "", ""
    try:
        parsed = _json.loads(raw)
    except (ValueError, TypeError):
        return "", ""
    if not isinstance(parsed, dict):
        return "", ""

    for mapped_code, properties in parsed.items():
        if not isinstance(properties, dict):
            continue
        if any(_clean_str(v) == entity for v in properties.values()):
            return CustomerDetailsService.resolve_widget_group(mapped_code)
    return "", ""


def build_guest_journey_chat_request(payload: dict[str, Any]) -> ChatRequest:
    body = _as_dict(payload)
    incoming_meta = _as_dict(body.get("metadata"))

    message = _clean_str(body.get("message"))
    wa_number = _clean_str(
        body.get("waNumber")
        or body.get("wa_number")
        or body.get("guest_phone")
        or body.get("guestPhone")
    )
    session_id = _clean_str(body.get("session_id") or body.get("sessionId"))
    if not session_id:
        session_id = _fallback_session_id("gj", wa_number)

    phase = _clean_str(body.get("phase"))
    entity_id = body.get("entity_id") if body.get("entity_id") is not None else body.get("entityId")
    group_id = body.get("group_id") if body.get("group_id") is not None else body.get("groupId")
    guest_id = body.get("guest_id") if body.get("guest_id") is not None else body.get("guestId")
    room_number = _clean_str(body.get("room_number") or body.get("roomNumber"))
    guest_name = _clean_str(body.get("guest_name") or body.get("guestName"))
    interactive_id = body.get("interactiveId")

    hotel_code = _clean_str(
        body.get("hotel_code")
        or body.get("hotelCode")
        or entity_id
        or group_id
        or "DEFAULT"
    )
    hotel_code = _resolve_hotel_code_from_entity_map(entity_id, hotel_code)
    channel = _clean_str(body.get("channel") or incoming_meta.get("channel") or "whatsapp")

    metadata: dict[str, Any] = dict(incoming_meta)
    metadata.setdefault("flow", "guest_journey")
    metadata.setdefault("channel", channel)
    metadata.setdefault("ticket_source", "whatsapp_bot")
    if phase:
        metadata["phase"] = phase
    if entity_id not in (None, ""):
        metadata["entity_id"] = entity_id
        metadata.setdefault("organisation_id", entity_id)
    if group_id not in (None, ""):
        metadata["group_id"] = group_id
    if guest_id not in (None, ""):
        metadata["guest_id"] = guest_id
    if room_number:
        metadata["room_number"] = room_number
    if guest_name:
        metadata["guest_name"] = guest_name
    if wa_number:
        metadata["wa_number"] = wa_number
        metadata.setdefault("guest_phone", wa_number)
    if interactive_id not in (None, ""):
        metadata["interactive_id"] = interactive_id
        # Lumira often uses this as the latest message marker.
        metadata.setdefault("message_id", interactive_id)

    # Stamp widget/group/conversation so the KEBO_ENGAGE_SESSIONS row this turn
    # produces is visible on the kebo dashboard's Chat History. Engage carries
    # these on its payload; WhatsApp does not, so resolve them from config.
    widget_id = body.get("widget_id") if body.get("widget_id") is not None else body.get("widgetId")
    resolved_widget, resolved_group = _resolve_widget_group(entity_id, hotel_code)
    if _clean_str(widget_id):
        metadata["widget_id"] = widget_id
    elif resolved_widget:
        metadata["widget_id"] = resolved_widget
    if "group_id" not in metadata and resolved_group:
        metadata["group_id"] = resolved_group

    # WhatsApp has no conversation concept — one continuous thread per guest,
    # so the stable gj_<number> session doubles as the conversation id.
    conversation_id = _clean_str(body.get("conversation_id") or body.get("conversationId"))
    metadata["conversation_id"] = conversation_id or session_id

    return ChatRequest(
        session_id=session_id,
        message=message,
        hotel_code=hotel_code,
        guest_phone=wa_number or None,
        channel=channel,
        metadata=metadata,
    )


def build_engage_chat_request(payload: dict[str, Any], session_id_header: str = "") -> ChatRequest:
    body = _as_dict(payload)
    incoming_meta = _as_dict(body.get("metadata"))

    message = _clean_str(body.get("message"))
    session_id = _clean_str(session_id_header or body.get("session_id") or body.get("sessionId"))
    if not session_id:
        session_id = _fallback_session_id(
            "eng",
            _clean_str(body.get("conversation_id") or body.get("conversationId")),
        )

    entity_id = body.get("entity_id") if body.get("entity_id") is not None else body.get("entityId")
    group_id = body.get("group_id") if body.get("group_id") is not None else body.get("groupId")
    message_id = body.get("message_id") if body.get("message_id") is not None else body.get("messageId")
    conversation_id = _clean_str(body.get("conversation_id") or body.get("conversationId"))
    widget_id = body.get("widget_id") if body.get("widget_id") is not None else body.get("widgetId")
    city = _clean_str(body.get("city"))
    country = _clean_str(body.get("country"))

    hotel_code = _clean_str(
        body.get("hotel_code")
        or body.get("hotelCode")
        or entity_id
        or group_id
        or "DEFAULT"
    )
    hotel_code = _resolve_hotel_code_from_entity_map(entity_id, hotel_code)
    channel = _clean_str(body.get("channel") or incoming_meta.get("channel") or "web_widget")

    # Normalize Lumira phase value to Kebo internal format.
    # e.g. "During Stay" → "during_stay", "Pre Booking" → "pre_booking"
    _phase_raw = _clean_str(body.get("phase"))
    _phase_norm = _phase_raw.lower().replace(" ", "_").replace("-", "_")
    _phase_aliases: dict[str, str] = {
        "duringstay": "during_stay",
        "in_stay": "during_stay",
        "instay": "during_stay",
        "prebooking": "pre_booking",
        "pre_checkin": "pre_booking",
        "postcheckout": "post_checkout",
        "post_checkout": "post_checkout",
    }
    _phase_kebo = _phase_aliases.get(_phase_norm, _phase_norm) if _phase_norm else ""

    metadata: dict[str, Any] = dict(incoming_meta)
    # When the caller passes phase="During Stay", set flow AND ticket_source to
    # during_stay values so chat_service does NOT classify the request as pre_booking.
    # chat_service treats flow="engage" OR ticket_source="booking_bot" as is_prebooking=True,
    # which would block in-stay services even when the guest has already checked in.
    _is_during_stay = _phase_kebo in {"during_stay", "in_stay", "instay"}
    if _is_during_stay:
        metadata.setdefault("flow", "during_stay")
        metadata.setdefault("ticket_source", "service")
    else:
        metadata.setdefault("flow", "engage")
        metadata.setdefault("ticket_source", "booking_bot")
    if _phase_kebo:
        metadata["phase"] = _phase_kebo
    else:
        metadata.setdefault("phase", "pre_booking")
    metadata.setdefault("channel", channel)
    if entity_id not in (None, ""):
        metadata["entity_id"] = entity_id
        metadata.setdefault("organisation_id", entity_id)
    if group_id not in (None, ""):
        metadata["group_id"] = group_id
    if message_id not in (None, ""):
        metadata["message_id"] = message_id
    if conversation_id:
        metadata["conversation_id"] = conversation_id
    if widget_id not in (None, ""):
        metadata["widget_id"] = widget_id
    if city:
        metadata["city"] = city
    if country:
        metadata["country"] = country

    return ChatRequest(
        session_id=session_id,
        message=message,
        hotel_code=hotel_code,
        channel=channel,
        metadata=metadata,
    )


def _fallback_ticket_summary(metadata: dict[str, Any]) -> dict[str, Any]:
    fallback: dict[str, Any] = {}
    ticket_id = _clean_str(metadata.get("ticket_id"))
    if ticket_id:
        fallback["ticket_id"] = ticket_id
    status = _clean_str(metadata.get("ticket_status"))
    if status:
        fallback["ticket_status"] = status
    category = _clean_str(metadata.get("ticket_category"))
    if category:
        fallback["category"] = category
    sub_category = _clean_str(metadata.get("ticket_sub_category"))
    if sub_category:
        fallback["sub_category"] = sub_category
    priority = _clean_str(metadata.get("ticket_priority"))
    if priority:
        fallback["priority"] = priority
    issue_summary = metadata.get("ticket_summary")
    if isinstance(issue_summary, str) and issue_summary.strip():
        fallback["issue"] = issue_summary.strip()
    return fallback


def resolve_ticket_summary(
    metadata: dict[str, Any],
    *,
    as_list: bool,
) -> list[dict[str, Any]] | dict[str, Any] | None:
    candidates = (
        metadata.get("ticket_api_response"),
        metadata.get("ticket_update_response"),
        metadata.get("ticket_record"),
        metadata.get("ticket_summary"),
    )
    for value in candidates:
        if isinstance(value, list):
            normalized = [item for item in value if isinstance(item, dict)]
            if not normalized:
                continue
            return normalized if as_list else normalized[0]
        if isinstance(value, dict):
            return [value] if as_list else value

    fallback = _fallback_ticket_summary(metadata)
    if fallback:
        return [fallback] if as_list else fallback
    return [] if as_list else None


def build_guest_journey_response(
    chat_response: ChatResponse,
    *,
    source_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source = _as_dict(source_payload)
    metadata = _as_dict(chat_response.metadata)

    ticket_created = _to_bool(metadata.get("ticket_created"))
    ticket_summary = resolve_ticket_summary(metadata, as_list=True)

    room_number = _clean_str(
        metadata.get("room_number")
        or source.get("room_number")
        or source.get("roomNumber")
    )
    outlet_id = (
        metadata.get("outlet_id")
        if metadata.get("outlet_id") is not None
        else source.get("outlet_id")
    )

    category = _clean_str(metadata.get("ticket_category"))
    if not category:
        category = "conversation" if not ticket_created else "request"
    sub_category = _clean_str(
        metadata.get("ticket_sub_category")
        or metadata.get("ticketing_matched_case")
    )
    display_text = _clean_str(chat_response.display_message or chat_response.message)

    payload: dict[str, Any] = {
        "is_ticket_intent": ticket_created,
        "response": display_text,
        "category": category,
        "sub_category": sub_category,
        "room_number": room_number,
        "needs_escalation": _to_bool(
            metadata.get("needs_escalation") or metadata.get("requires_human_handoff")
        ),
        "name_of_file": _clean_str(
            metadata.get("name_of_file")
            or metadata.get("file_name")
            or metadata.get("filename")
        ),
    }
    if ticket_summary:
        payload["ticket_summary"] = ticket_summary

    # Every key below is ALWAYS emitted, even when empty. Lumira merges its
    # usage_fields() dict into every success response and always sends
    # media/url, so the Java caller may read these keys unconditionally — a
    # missing key can throw on a typed client and the guest silently gets no
    # reply. Matching Lumira's shape exactly removes that whole failure class.
    input_tokens = _to_int(metadata.get("input_tokens")) or 0
    output_tokens = _to_int(metadata.get("output_tokens")) or 0
    total_tokens = _to_int(metadata.get("total_tokens")) or (input_tokens + output_tokens)
    cost = _to_float(metadata.get("cost")) or 0.0

    payload["input_tokens"] = input_tokens
    payload["output_tokens"] = output_tokens
    payload["total_tokens"] = total_tokens
    payload["cost"] = round(cost, 4)

    media = metadata.get("media")
    url = metadata.get("url")
    payload["media"] = media if media not in (None, "") else None
    payload["url"] = url if url not in (None, "") else None
    payload["outlet_id"] = outlet_id if outlet_id not in (None, "") else ""

    return payload


def build_engage_response(
    chat_response: ChatResponse,
    *,
    source_payload: dict[str, Any] | None = None,
    session_id: str = "",
) -> dict[str, Any]:
    source = _as_dict(source_payload)
    metadata = _as_dict(chat_response.metadata)

    ticket_created = _to_bool(metadata.get("ticket_created"))
    ticket_summary = resolve_ticket_summary(metadata, as_list=False)

    message_id = source.get("message_id")
    if message_id is None:
        message_id = source.get("messageId")
    if message_id is None:
        message_id = metadata.get("message_id")

    total_cost = _to_float(metadata.get("total_cost") or metadata.get("cost")) or 0.0
    total_tokens = _to_int(metadata.get("total_tokens")) or 0
    display_text = _clean_str(chat_response.display_message or chat_response.message)

    payload: dict[str, Any] = {
        "message": display_text,
        "render": metadata.get("render"),
        "city": _clean_str(source.get("city") or metadata.get("city")),
        "country": _clean_str(source.get("country") or metadata.get("country")),
        "is_ticket_intent": ticket_created,
        "render_type": metadata.get("render_type"),
        "message_id": message_id,
        "additional_data": {
            "total_cost": round(total_cost, 4),
            "total_tokens": int(total_tokens),
            "session_id": _clean_str(session_id or chat_response.session_id),
            "message_id": message_id,
            "entity_id": source.get("entity_id")
            if source.get("entity_id") is not None
            else source.get("entityId"),
            "group_id": source.get("group_id")
            if source.get("group_id") is not None
            else source.get("groupId"),
            "sender": "bot",
        },
    }
    if ticket_summary:
        payload["ticket_summary"] = ticket_summary
    return payload
