"""
Chat API Routes

Endpoints for chat functionality and testing.
"""

import json
import asyncio
import logging
import traceback as _traceback
import re
import time
from datetime import date, timedelta
from time import perf_counter as _perf_counter
from fastapi import APIRouter, Body, HTTPException, Depends, Request
from fastapi.routing import APIRoute
from starlette.responses import Response
from typing import Any, Optional, List
from pydantic import BaseModel, Field
from sqlalchemy import select

from schemas.chat import ChatRequest, ChatResponse, ConversationContext, ConversationState, MessageRole
from schemas.orchestration import OrchestrationDecision, SuggestionChip
from llm.client import llm_client
from services.chat_service import chat_service
from services.message_dedup import message_dedup
from services.evaluation_metrics_service import evaluation_metrics_service
from services.observability_service import observability_service
from services.backend_trace_service import backend_trace_service
from services.everything_trace_service import everything_trace_service
from services.conversation_audit_service import conversation_audit_service
from services.turn_diagnostics_service import turn_diagnostics_service
from services.db_config_service import db_config_service
from services.config_service import config_service
from services.conversation_memory_service import conversation_memory_service
from services.llm_orchestration_service import llm_orchestration_service
from services.suggestion_prefetch_service import suggestion_prefetch_service
from services.image_tool_service import image_tool_service
from services import menu_document_service
from services import prebooking_settings_service
from services.step_trace_service import step_trace_service
from services.chat_step_timing_service import chat_step_timing_service
from services import new_detailed_logger
from config.settings import settings
from services.booking_assistance_state import (
    team_booking_enabled,
    details_submitted as booking_details_submitted,
    prefetch_revision,
    last_response_language,
    live_rates_url,
    live_rates_url_template,
)
from core.context_manager import context_manager
from models.database import AsyncSessionLocal, KBFile, Hotel, get_db
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import OperationalError


logger = logging.getLogger(__name__)

# Keeps deferred display/smart-image enrichment tasks alive after the response
# returns (asyncio only weakly references tasks, so without this they can be
# garbage-collected mid-flight). The done-callback discards + retrieves any
# exception so it never surfaces as "exception was never retrieved".
_BACKGROUND_ENRICH_TASKS: set = set()


def _reap_background_enrich_task(task: "asyncio.Task") -> None:
    _BACKGROUND_ENRICH_TASKS.discard(task)
    try:
        exc = task.exception()
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        return
    if exc is not None:
        logger.warning("deferred_display_enrichment_failed: %s", exc)


class ChatStepTraceRoute(APIRoute):
    """Route-level step tracing for all chat endpoints."""

    def get_route_handler(self):
        original_route_handler = super().get_route_handler()
        route_path = str(self.path or "").strip()

        async def custom_route_handler(request: Request) -> Response:
            trace_id = str(getattr(request.state, "trace_id", "") or "").strip()
            endpoint = str(request.url.path or route_path or "/api/chat").strip()
            method = str(request.method or "GET").strip().upper()
            session_id = str(
                request.path_params.get("session_id")
                or request.query_params.get("session_id")
                or ""
            ).strip()
            hotel_code = str(
                getattr(request.state, "hotel_code", "")
                or request.query_params.get("hotel_code")
                or ""
            ).strip()
            start_ts = _perf_counter()

            step_trace_service.log_event(
                "chat_route_start",
                payload={"route_path": route_path},
                trace_id=trace_id,
                session_id=session_id,
                hotel_code=hotel_code,
                endpoint=endpoint,
                method=method,
                component="api.routes.chat",
                step=route_path or endpoint,
                stage="start",
            )

            try:
                response = await original_route_handler(request)
            except HTTPException as exc:
                duration_ms = round((_perf_counter() - start_ts) * 1000.0, 2)
                step_trace_service.log_event(
                    "chat_route_failed",
                    payload={"route_path": route_path, "duration_ms": duration_ms},
                    trace_id=trace_id,
                    session_id=session_id,
                    hotel_code=hotel_code,
                    endpoint=endpoint,
                    method=method,
                    status_code=int(exc.status_code),
                    component="api.routes.chat",
                    step=route_path or endpoint,
                    stage="failed",
                    error=str(exc.detail),
                )
                raise
            except Exception as exc:
                duration_ms = round((_perf_counter() - start_ts) * 1000.0, 2)
                step_trace_service.log_event(
                    "chat_route_failed",
                    payload={"route_path": route_path, "duration_ms": duration_ms},
                    trace_id=trace_id,
                    session_id=session_id,
                    hotel_code=hotel_code,
                    endpoint=endpoint,
                    method=method,
                    status_code=500,
                    component="api.routes.chat",
                    step=route_path or endpoint,
                    stage="failed",
                    error=str(exc),
                )
                raise

            duration_ms = round((_perf_counter() - start_ts) * 1000.0, 2)
            step_trace_service.log_event(
                "chat_route_end",
                payload={"route_path": route_path, "duration_ms": duration_ms},
                trace_id=trace_id,
                session_id=session_id,
                hotel_code=hotel_code,
                endpoint=endpoint,
                method=method,
                status_code=int(getattr(response, "status_code", 200) or 200),
                component="api.routes.chat",
                step=route_path or endpoint,
                stage="completed",
            )
            return response

        return custom_route_handler


router = APIRouter(prefix="/api/chat", tags=["Chat"], route_class=ChatStepTraceRoute)

_DEFAULT_CHAT_TEST_PHASE_PROFILES: dict[str, dict[str, str]] = {}

# Placeholder entity/guest ids used by the test harness for hotels NOT wired
# to Lumira (no lumira_entity_id on any service form_config, no matching guest
# in GHN_FF_GUEST_INFO). Keeps the harness one-click for new hotels — admin
# doesn't have to retype ids every test. Production paths never read this.
_TEST_FALLBACK_ENTITY_ID = "5703"
_TEST_FALLBACK_GUEST_IDS = {
    "pre_checkin": "921346",
    "during_stay": "921348",
    "post_checkout": "921347",
}


async def _fetch_real_test_profiles(
    hotel_code: str,
    db: AsyncSession,
) -> dict[str, dict[str, str]]:
    """Build a phase->profile map using REAL ids resolved from the GHN tables.

    entity_id comes from the hotel's `lumira_entity_id` (stored on any
    of its services' form_config). guest_id is only looked up for phases
    where a real guest record exists (pre_checkin, during_stay,
    post_checkout). pre_booking visitors are new leads with no guest_id
    yet, so we leave it empty for that phase.

    When the hotel is NOT wired to Lumira (no lumira_entity_id anywhere),
    falls back to harness placeholders so the tester doesn't have to retype
    ids every session. The placeholders are clearly test-only and never used
    by the production Lumira request path.
    """
    from sqlalchemy import text as _text

    # 1) Resolve the GUEST-INFO entity. This is DECOUPLED from ticketing: prefer
    #    an explicit `guest_info_entity_id` in any service form_config; only fall
    #    back to `lumira_entity_id` (the ticketing entity) when the two are the
    #    same for this hotel; else the harness placeholder. Setting guest lookup
    #    via guest_info_entity_id lets you point at the FF guest entity without
    #    changing where tickets are pushed.
    entity_id = ""
    try:
        result = await db.execute(
            _text(
                """
                SELECT s.form_config
                FROM new_bot_services s
                JOIN new_bot_hotels h ON h.id = s.hotel_id
                WHERE h.code = :code
                  AND s.form_config IS NOT NULL
                  AND (s.form_config LIKE '%guest_info_entity_id%'
                       OR s.form_config LIKE '%lumira_entity_id%')
                """
            ),
            {"code": hotel_code},
        )
        lumira_fallback = ""
        for (fc_raw,) in result.fetchall():
            try:
                fc = json.loads(fc_raw)
            except Exception:
                continue
            gie = str(fc.get("guest_info_entity_id") or "").strip()
            if gie:
                entity_id = gie
                break
            if not lumira_fallback:
                lumira_fallback = str(fc.get("lumira_entity_id") or "").strip()
        if not entity_id:
            entity_id = lumira_fallback
    except Exception:
        logger.debug("test_profiles: entity_id lookup failed for %s", hotel_code, exc_info=True)

    used_fallback_entity = False
    if not entity_id:
        entity_id = _TEST_FALLBACK_ENTITY_ID
        used_fallback_entity = True

    base = {
        "entity_id": entity_id,
        "organisation_id": entity_id,
        "ticket_source": "whatsapp_bot",
    }

    def _phase_from_dates(cin: Any, cout: Any) -> str:
        """Derive the guest's journey phase from their stay dates vs today."""
        today = date.today()
        try:
            ci = cin.date() if hasattr(cin, "date") else cin
            co = cout.date() if hasattr(cout, "date") else cout
        except Exception:
            return ""
        if ci and ci > today:
            return "pre_checkin"
        if ci and co and ci <= today <= co:
            return "during_stay"
        if co and co < today:
            return "post_checkout"
        if ci and ci <= today and not co:
            return "during_stay"
        return ""

    # 2) Pull EVERY guest for the entity and bucket into the phase their dates
    #    imply (pre_checkin / during_stay / post_checkout). Carry the full
    #    identity so the widget greets by real name, answers real check-in dates,
    #    and prefills forms with the real phone/email. First (most recent) guest
    #    per phase wins.
    guests_by_phase: dict[str, dict[str, str]] = {}
    if not used_fallback_entity:
        try:
            result = await db.execute(
                _text(
                    """
                    SELECT ID, GUEST_NAME, CHECK_IN_DATE, CHECK_OUT_DATE,
                           MOBILE_NUMBER_1, EMAIL_ID_1, ROOM_NUMBER
                    FROM GHN_FF_PROD.GHN_FF_GUEST_INFO
                    WHERE ENTITY_ID = :entity_id
                    ORDER BY CHECK_IN_DATE DESC
                    """
                ),
                {"entity_id": entity_id},
            )
            for gid, gname, cin, cout, mob, email, room in result.fetchall():
                phase_id = _phase_from_dates(cin, cout)
                if not phase_id or phase_id in guests_by_phase:
                    continue
                prof = {**base, "guest_id": str(gid or "").strip()}
                if str(gname or "").strip():
                    prof["guest_name"] = str(gname).strip()
                if str(mob or "").strip():
                    prof["guest_phone"] = str(mob).strip()
                if str(email or "").strip():
                    prof["guest_email"] = str(email).strip()
                if str(room or "").strip():
                    prof["room_number"] = str(room).strip()
                try:
                    if cin:
                        prof["check_in_date"] = (cin.date() if hasattr(cin, "date") else cin).strftime("%Y-%m-%d")
                    if cout:
                        prof["check_out_date"] = (cout.date() if hasattr(cout, "date") else cout).strftime("%Y-%m-%d")
                except Exception:
                    pass
                guests_by_phase[phase_id] = prof
        except Exception:
            logger.debug("test_profiles: real guest lookup failed for entity_id=%s", entity_id, exc_info=True)

    profiles: dict[str, dict[str, str]] = {
        "pre_booking": {**base, "flow": "booking_bot"},
    }
    profiles.update(guests_by_phase)
    if used_fallback_entity and not guests_by_phase:
        # Hotel not wired to a guest-info entity — fill phase guest_ids with
        # placeholders so the harness is still one-click usable.
        for phase_id, fallback_gid in _TEST_FALLBACK_GUEST_IDS.items():
            profiles[phase_id] = {**base, "guest_id": fallback_gid}

    return profiles


def _normalize_phase_identifier(value: Any) -> str:
    raw = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not raw:
        return ""
    aliases = {
        "prebooking": "pre_booking",
        "booking": "pre_checkin",
        "precheckin": "pre_checkin",
        "duringstay": "during_stay",
        "instay": "during_stay",
        "in_stay": "during_stay",
        "postcheckout": "post_checkout",
    }
    return aliases.get(raw, raw)


def _normalize_chat_test_profile(profile: Any) -> dict[str, str]:
    if not isinstance(profile, dict):
        return {}

    # Keep only integration-relevant profile fields to avoid leaking arbitrary keys.
    keys = {
        "guest_id",
        "entity_id",
        "organisation_id",
        "room_number",
        "guest_phone",
        "guest_name",
        "group_id",
        "ticket_source",
        "flow",
    }
    normalized: dict[str, str] = {}
    for key in keys:
        value = profile.get(key)
        if value in (None, ""):
            continue
        text = str(value).strip()
        if text:
            normalized[key] = text

    # Accept alternate spellings for organisation/entity IDs.
    if "organisation_id" not in normalized:
        alt = str(profile.get("organization_id") or profile.get("org_id") or "").strip()
        if alt:
            normalized["organisation_id"] = alt
    if "entity_id" not in normalized:
        alt = str(profile.get("organisation_id") or profile.get("organization_id") or profile.get("org_id") or "").strip()
        if alt:
            normalized["entity_id"] = alt
    if "organisation_id" not in normalized and "entity_id" in normalized:
        normalized["organisation_id"] = normalized["entity_id"]

    return normalized


def _load_chat_test_phase_profiles() -> dict[str, dict[str, str]]:
    raw = str(getattr(settings, "chat_test_phase_profiles_json", "") or "").strip()
    parsed: Any = {}
    if raw:
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {}

    defaults: dict[str, dict[str, str]] = {}
    for phase_key, profile_payload in _DEFAULT_CHAT_TEST_PHASE_PROFILES.items():
        phase_id = _normalize_phase_identifier(phase_key)
        if not phase_id:
            continue
        profile = _normalize_chat_test_profile(profile_payload)
        if profile:
            defaults[phase_id] = profile

    overrides: dict[str, dict[str, str]] = {}

    if isinstance(parsed, list):
        for row in parsed:
            if not isinstance(row, dict):
                continue
            phase_id = _normalize_phase_identifier(row.get("phase") or row.get("phase_id"))
            if not phase_id:
                continue
            profile = _normalize_chat_test_profile(row)
            if profile:
                overrides[phase_id] = profile
    elif isinstance(parsed, dict):
        for phase_key, profile_payload in parsed.items():
            phase_id = _normalize_phase_identifier(phase_key)
            if not phase_id:
                continue
            profile = _normalize_chat_test_profile(profile_payload)
            if profile:
                overrides[phase_id] = profile

    merged = dict(defaults)
    merged.update(overrides)
    return merged


def _normalize_service_identifier(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _sanitize_suggestions(raw: list[str], limit: int = 4) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = " ".join(str(item or "").strip().split())
        if not text:
            continue
        if len(text) > 90:
            continue
        word_count = len(text.split())
        # Upper bound is generous because legitimate chips often carry a
        # multi-word proper noun — e.g. "I'd like to know more about Zone
        # Connect Park Coimbatore" (10 words) or "Tell me about the dining
        # options at Zone Park Coimbatore". The 90-char cap above is the real
        # guard against rambling; an 8-word cap here silently dropped these and
        # left only 2 chips.
        if word_count < 2 or word_count > 14:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(text)
        if len(cleaned) >= max(1, int(limit or 4)):
            break
    return cleaned


_CHIP_DISALLOWED_PHRASES = [
    "book",
    "booking",
    "book now",
    "reserve",
    "reservation",
    "availability",
    "book a table",
    "reserve a table",
    "request",
    "airport transfer",
    "rates",
    "rate",
    "price",
    "cost",
    "cheap",
    "cheapest",
    "breakfast",
    "photo",
    "photos",
    "picture",
    "pictures",
    "image",
    "images",
    "see room",
    "show me photo",
]


def _chip_is_informational_positive(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", str(text or "").strip().lower())
    if not normalized:
        return False
    # Whole-word/phrase match so a banned word doesn't trip on an innocent
    # substring (e.g. "rate" inside "celebrate", "cost" inside "Costa").
    for phrase in _CHIP_DISALLOWED_PHRASES:
        if re.search(r"\b" + re.escape(phrase) + r"\b", normalized):
            return False
    return True


def _filter_chip_quality(raw: list[str], *, limit: int = 3) -> list[str]:
    cleaned = _sanitize_suggestions(raw, limit=max(3, limit * 2))
    filtered = [chip for chip in cleaned if _chip_is_informational_positive(chip)]
    return _sanitize_suggestions(filtered, limit=limit)


def _suppress_recently_asked_chip_texts(
    chips: list[SuggestionChip],
    *,
    runtime_snapshot: dict[str, Any],
    lookback_messages: int = 10,
) -> list[SuggestionChip]:
    history_rows = runtime_snapshot.get("conversation_history")
    if not isinstance(history_rows, list) or not history_rows:
        return chips
    recent_user_texts: set[str] = set()
    for row in history_rows[-max(1, int(lookback_messages or 10)):]:
        if not isinstance(row, dict):
            continue
        role = str(row.get("role") or "").strip().lower()
        if role != "user":
            continue
        normalized = _normalize_suggestions_turn_text(row.get("content"))
        if normalized:
            recent_user_texts.add(normalized)
    if not recent_user_texts:
        return chips

    filtered: list[SuggestionChip] = []
    for chip in chips:
        chip_text = _normalize_suggestions_turn_text(chip.text)
        if chip_text and chip_text in recent_user_texts:
            continue
        filtered.append(chip)
    return filtered


def _coerce_conversation_state(value: Any, default: ConversationState = ConversationState.IDLE) -> ConversationState:
    raw = str(value or "").strip().lower()
    if not raw:
        return default
    for item in ConversationState:
        if item.value == raw:
            return item
    return default


def _strip_default_property_references(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return raw
    # Remove sentence fragments that reference placeholder "Default property"
    # (including underscore variant), then clean any leftover lines.
    cleaned = re.sub(
        r"[^.!?\n]*default[_\s]+property[^.!?\n]*[.!?]?",
        " ",
        raw,
        flags=re.IGNORECASE,
    )
    lines = []
    for line in cleaned.splitlines():
        lowered = line.lower()
        if "default property" in lowered or "default_property" in lowered:
            continue
        lines.append(line)
    cleaned_text = "\n".join(lines).strip()
    cleaned_text = re.sub(r"\n{3,}", "\n\n", cleaned_text)
    # SPACES AND TABS ONLY - NEVER NEWLINES. `\s` includes \n, so `\s{2,}`
    # collapsed the blank line between sections into a single space and undid
    # the line directly above it. A reply that read
    #     ...two types of rooms:
    #
    #     **Premium City View**
    #     - 190 sq ft
    # arrived as "...two types of rooms: **Premium City View**\n- 190 sq ft",
    # welding the heading onto the sentence before it and flattening the whole
    # structure. Only runs of spaces/tabs are noise worth collapsing.
    cleaned_text = re.sub(r"[ \t]{2,}", " ", cleaned_text)
    return cleaned_text.strip()


def _extract_prefetch_pending_data_public(pending_data_raw: Any) -> dict[str, Any]:
    return llm_orchestration_service._coerce_public_pending(
        pending_data_raw if isinstance(pending_data_raw, dict) else {}
    )


def _requires_contextual_suggestion_generation(
    *,
    pending_data_public: dict[str, Any],
    pending_action: str,
    active_service: dict[str, Any],
) -> bool:
    if str(pending_action or "").strip():
        return True
    if isinstance(active_service, dict):
        for key in ("id", "service_id", "name"):
            if str(active_service.get(key) or "").strip():
                return True
    scope_keys = (
        "active_property_ids",
        "active_property_names",
        "selected_property_scope_id",
        "selected_property_scope_ids",
        "selected_property_scope_name",
        "stay_property_id",
        "stay_property_name",
    )
    for key in scope_keys:
        value = pending_data_public.get(key)
        if isinstance(value, list) and value:
            return True
        if isinstance(value, dict) and value:
            return True
        if str(value or "").strip():
            return True
    return False


async def _load_suggestions_runtime_snapshot(
    *,
    session_id: str,
    db: AsyncSession | None,
) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "context": None,
        "conversation_state": "",
        "pending_action": "",
        "pending_data_raw": {},
        "pending_data_public": {},
        "known_context": {},
        "memory_snapshot": {},
        "phase_id_hint": "",
        "active_service_id": "",
        "active_service": {},
        "conversation_history": [],
    }
    session_key = str(session_id or "").strip()
    if not session_key:
        return snapshot

    context: ConversationContext | None = None
    try:
        context = await context_manager.get_context(session_key, db_session=db)
    except Exception:
        context = None
    if context is None and db is not None:
        try:
            context = await context_manager.get_context(session_key, db_session=None)
        except Exception:
            context = None
    snapshot["context"] = context
    if context is None:
        return snapshot

    snapshot["conversation_state"] = str(context.state.value if context.state else "").strip()
    snapshot["pending_action"] = str(context.pending_action or "").strip()
    pending_data_raw = context.pending_data if isinstance(context.pending_data, dict) else {}
    pending_data_public = _extract_prefetch_pending_data_public(pending_data_raw)
    snapshot["pending_data_raw"] = pending_data_raw
    snapshot["pending_data_public"] = pending_data_public
    snapshot["known_context"] = {
        "guest_name": str(context.guest_name or "").strip(),
        "room_number": str(context.room_number or "").strip(),
    }
    snapshot["memory_snapshot"] = conversation_memory_service.get_snapshot(context)

    phase_candidates = [
        pending_data_public.get("phase"),
        (pending_data_raw.get("_integration") or {}).get("phase")
        if isinstance(pending_data_raw.get("_integration"), dict)
        else "",
        pending_data_raw.get("_selected_phase_id"),
        (pending_data_raw.get("_selected_phase") or {}).get("id")
        if isinstance(pending_data_raw.get("_selected_phase"), dict)
        else "",
    ]
    for candidate in phase_candidates:
        normalized = _normalize_phase_identifier(candidate)
        if normalized:
            snapshot["phase_id_hint"] = normalized
            break

    active_service_candidates = [
        pending_data_public.get("service_id"),
        pending_data_public.get("service"),
        pending_data_raw.get("_last_service_id"),
        snapshot["pending_action"],
    ]
    for candidate in active_service_candidates:
        normalized_service = _normalize_service_identifier(candidate)
        if not normalized_service:
            continue
        svc = config_service.get_service(normalized_service)
        if not svc:
            continue
        snapshot["active_service_id"] = normalized_service
        snapshot["active_service"] = {
            "id": str(svc.get("id") or normalized_service).strip(),
            "name": str(svc.get("name") or normalized_service).strip(),
            "type": str(svc.get("type") or "service").strip(),
            "description": str(svc.get("description") or "").strip(),
        }
        break

    if hasattr(context, "get_recent_messages"):
        total_chars = 0
        history_rows: list[dict[str, str]] = []
        for msg in context.get_recent_messages(12):
            content = str(msg.content or "").strip()
            if not content or total_chars >= 6000:
                break
            clipped = content[: min(6000 - total_chars, 850)]
            history_rows.append(
                {
                    "role": str(getattr(msg.role, "value", "") or "").strip(),
                    "content": clipped,
                }
            )
            total_chars += len(clipped)
        snapshot["conversation_history"] = history_rows

    return snapshot


def _normalize_suggestions_turn_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _is_suggestions_request_stale(
    *,
    runtime_snapshot: dict[str, Any],
    last_bot_message: str,
) -> bool:
    # Only gate on role: if the last stored message is from the user, the
    # conversation has advanced past this turn and chips are stale.
    # Content-equality was removed because post-processing steps
    # (_inject_form_trigger, _strip_default_property_references) mutate
    # response.message AFTER chat_service saves the original text to context,
    # so the stored vs. client strings never match — causing a false stale.
    # turn_trace_id matching in await_post_response_chip_objects already
    # ensures the chips are for the correct turn.
    _ = last_bot_message  # kept in signature for backward compatibility
    history = runtime_snapshot.get("conversation_history")
    if not isinstance(history, list) or not history:
        return False
    last_row = history[-1] if isinstance(history[-1], dict) else {}
    last_role = str(last_row.get("role") or "").strip().lower()
    return last_role == "user"


async def _schedule_prefetch_for_visible_suggestions(
    *,
    request: "SuggestionsRequest",
    hotel_code: str,
    runtime_snapshot: dict[str, Any],
    suggestions: list[Any],
) -> Optional[str]:
    session_id = str(request.session_id or "").strip()
    if not session_id or not suggestions:
        return None
    if not bool(getattr(settings, "chat_suggestion_prefetch_enabled", True)):
        return None
    if not str(getattr(settings, "openai_api_key", "") or "").strip():
        return None
    # A HOTEL NAMED NOW CONTINUES THE REQUEST (Pre-booking tab hotels). After "which
    # hotel is this about?" the live turn reads `_awaiting_hotel_for` and carries the
    # tapped hotel into that request; a pre-cook never sees that key and cooked the
    # hotel's overview, which the tap then replayed with no form (T24, 15 Oct). The
    # turn's chip snapshot keeps public keys only, so read the saved context.
    if prebooking_settings_service.is_admin_managed(str(hotel_code or "").strip().lower()):
        try:
            _ctx_now = await context_manager.get_context(session_id, db_session=None)
        except Exception:
            _ctx_now = None
        _pd_now = _ctx_now.pending_data if _ctx_now is not None and isinstance(_ctx_now.pending_data, dict) else {}
        if str(_pd_now.get("_awaiting_hotel_for") or "").strip():
            return None

    phase_id = _normalize_phase_identifier(request.current_phase or "pre_booking") or "pre_booking"
    snapshot_phase_id = _normalize_phase_identifier(runtime_snapshot.get("phase_id_hint"))
    if snapshot_phase_id:
        phase_id = snapshot_phase_id
    phase_name = phase_id.replace("_", " ").title()

    service_catalog: list[dict[str, Any]] = []
    try:
        db_services = await db_config_service.get_services()
        if isinstance(db_services, list):
            service_catalog = [dict(row) for row in db_services if isinstance(row, dict)]
    except Exception:
        service_catalog = []
    if not service_catalog:
        try:
            service_catalog = [
                dict(row)
                for row in (config_service.get_services() or [])
                if isinstance(row, dict)
            ]
        except Exception:
            service_catalog = []

    phase_services = [
        row
        for row in service_catalog
        if bool(row.get("is_active", True))
        and _normalize_phase_identifier(row.get("phase_id")) == phase_id
    ][:20]

    try:
        capabilities_summary = config_service.get_capability_summary(hotel_code)
    except Exception:
        capabilities_summary = {}

    try:
        return await suggestion_prefetch_service.schedule_batch(
            session_id=session_id,
            conversation_id=str(request.conversation_id or "").strip(),
            hotel_code=hotel_code,
            assistant_turn_id=str(request.assistant_turn_id or "").strip(),
            phase_id=phase_id,
            conversation_state=str(runtime_snapshot.get("conversation_state") or "").strip(),
            last_bot_message=str(request.last_bot_message or "").strip(),
            triggering_user_message=str(request.user_message or "").strip(),
            suggestions=suggestions,
            conversation_history=list(runtime_snapshot.get("conversation_history") or []),
            active_service=dict(runtime_snapshot.get("active_service") or {}),
            services_allowed_in_phase=phase_services,
            knowledge_base_excerpt={"full_kb_text": "", "service_knowledge_corpus": ""},
            # Read from the RAW pending data: `pending_data_public` has already
            # had every underscore key stripped, so the booking ledger is not in
            # it. A pre-cooked answer without this is written as though the
            # guest never submitted the form.
            booking_ledger=[
                row
                for row in (
                    (runtime_snapshot.get("pending_data_raw") or {}).get("_rb_bookings") or []
                )
                if isinstance(row, dict)
            ],
            turn_revision=(prefetch_revision(runtime_snapshot.get("context"))
                           if team_booking_enabled(hotel_code) else ""),
            response_language=last_response_language(runtime_snapshot.get("context")),
            capabilities_summary=capabilities_summary,
            memory_snapshot=dict(runtime_snapshot.get("memory_snapshot") or {}),
            selected_phase_context={
                "selected_phase_id": phase_id,
                "selected_phase_name": phase_name,
            },
            pending_action=str(runtime_snapshot.get("pending_action") or "").strip(),
            pending_data_public=dict(runtime_snapshot.get("pending_data_public") or {}),
            known_context=dict(runtime_snapshot.get("known_context") or {}),
        )
    except Exception:
        logger.exception(
            "suggestions_prefetch_schedule_failed session_id=%s hotel_code=%s",
            session_id,
            hotel_code,
        )
        return None


def _prefetch_language_matches(user_message: Any, cached_payload: Any) -> bool:
    """False when a pre-cooked answer is in a different script than the guest.

    A chip answer is written MINUTES before it is served, and the language it
    was written in is frozen into it. The live path's language enforcement
    cannot help: a cache hit skips `process_message` entirely. Live evidence
    (2026-09-28) - one Mango conversation held three languages at once and a
    plain English question was answered out of the cache in Devanagari.

    This is a pure script comparison: no LLM, no router, nothing to wait for,
    so it costs nothing on the turns that are fine. Measured over 320 real
    Mango turns it disagrees on 2 (0.6%) - those two fall through to the live
    pipeline and are answered properly instead of being served a wrong-language
    answer. It cannot catch ROMANIZED Hindi/Punjabi, which is Latin script by
    definition; the live enforcement pass covers that case.
    """
    try:
        text = str(user_message or "").strip()
        if not text or not isinstance(cached_payload, dict):
            return True
        answer = " ".join(
            str(cached_payload.get(key) or "")
            for key in ("message", "display_message", "response_text")
        ).strip()
        if not answer:
            return True
        guest = llm_orchestration_service._script_language_hint(text)
        reply = llm_orchestration_service._script_language_hint(answer)
        return str(guest.get("language") or "") == str(reply.get("language") or "")
    except Exception:
        # Never let this gate cost a guest their answer.
        return True


def _prefetch_contract_matches(context: Any, request: ChatRequest, cached_payload: dict) -> bool:
    if not team_booking_enabled(request.hotel_code):
        return _prefetch_language_matches(request.message, cached_payload)
    meta = cached_payload.get("metadata") or {}
    conversation_id = str((request.metadata or {}).get("conversation_id") or "")
    language = meta.get("response_language") or {}
    return bool(
        meta.get("prefetch_turn_revision") == prefetch_revision(context, conversation_id)
        and language.get("language") and language.get("script")
        and meta.get("language_contract_verified")
    )


async def _build_prefetch_cached_response(
    *,
    request: ChatRequest,
    hotel_code: str,
    cached_payload: dict[str, Any],
    db: AsyncSession | None,
) -> ChatResponse | None:
    try:
        context = await context_manager.get_or_create_context(
            session_id=request.session_id,
            hotel_code=hotel_code,
            guest_phone=request.guest_phone,
            channel=request.channel or (request.metadata or {}).get("channel") or None,
            db_session=db,
        )
    except OperationalError:
        context = await context_manager.get_or_create_context(
            session_id=request.session_id,
            hotel_code=hotel_code,
            guest_phone=request.guest_phone,
            channel=request.channel or (request.metadata or {}).get("channel") or None,
            db_session=None,
        )
    # Both transports call this while holding the session lock. Validate against
    # LIVE state before replaying cached state, writing history, or adding a CTA.
    if not _prefetch_contract_matches(context, request, cached_payload):
        logger.info("prefetch rejected: current turn contract changed session=%s", request.session_id)
        return None
    request_channel = request.channel or (request.metadata or {}).get("channel") or context.channel
    if request_channel:
        context.channel = str(request_channel).strip()

    user_text = str(request.message or "").strip()
    assistant_text = str(cached_payload.get("message") or "").strip()
    if not assistant_text:
        assistant_text = "I can help with that. Could you share one more detail so I can answer accurately?"

    # Same reason as the location-intro short-circuit below: serving a chip from
    # the prefetch cache skips process_message, so nothing sets
    # `_active_conversation_uid` and add_message has no thread id to stamp. Both
    # turns then persist NULL and the widget drops them when replaying the
    # thread — a chip answer the guest saw would vanish on reload.
    if not isinstance(context.pending_data, dict):
        context.pending_data = {}
    _pf_meta = request.metadata if isinstance(request.metadata, dict) else {}
    _pf_conv_uid = str(
        _pf_meta.get("conversation_id") or _pf_meta.get("conversationId") or ""
    ).strip()
    if _pf_conv_uid:
        conversation_memory_service.reset_pending_for_conversation_rotation(
            context, _pf_conv_uid
        )
        context.pending_data["_active_conversation_uid"] = _pf_conv_uid

    user_meta = {
        "channel": context.channel,
        "prefetch_cache_hit": True,
    }
    context.add_message(MessageRole.USER, user_text, metadata=user_meta)

    incoming_meta = cached_payload.get("metadata")
    if not isinstance(incoming_meta, dict):
        incoming_meta = {}
    if not isinstance(context.pending_data, dict):
        context.pending_data = {}

    cached_pending_public = _extract_prefetch_pending_data_public(
        incoming_meta.get("pending_data_public")
    )
    restored_pending = suggestion_prefetch_service._rehydrate_pending_data_public(
        cached_pending_public
    )
    if restored_pending:
        context.pending_data.update(restored_pending)

    cached_decision_raw = incoming_meta.get("cached_decision")
    cached_decision: OrchestrationDecision | None = None
    if isinstance(cached_decision_raw, dict):
        try:
            cached_decision = OrchestrationDecision.model_validate(cached_decision_raw)
        except Exception:
            cached_decision = None
    if cached_decision is not None and isinstance(cached_decision.pending_data_updates, dict):
        context.pending_data.update(
            suggestion_prefetch_service._rehydrate_pending_data_public(
                cached_decision.pending_data_updates
            )
        )
        if str(cached_decision.pending_action or "").strip():
            context.pending_action = cached_decision.pending_action

    selected_phase_id = _normalize_phase_identifier(
        incoming_meta.get("selected_phase_id")
        or context.pending_data.get("_selected_phase_id")
        or ""
    )
    if selected_phase_id:
        context.pending_data["_selected_phase_id"] = selected_phase_id
    selected_phase_name = str(
        incoming_meta.get("selected_phase_name")
        or (selected_phase_id.replace("_", " ").title() if selected_phase_id else "")
    ).strip()

    target_service_id = _normalize_service_identifier(
        incoming_meta.get("target_service_id")
        or (cached_decision.target_service_id if cached_decision is not None else "")
        or context.pending_data.get("_last_service_id")
        or ""
    )
    target_service_name = str(
        incoming_meta.get("target_service_name")
        or context.pending_data.get("_last_service_name")
        or ""
    ).strip()
    target_service: dict[str, Any] = {}
    if target_service_id:
        service_row = config_service.get_service(target_service_id) or {}
        target_service = {
            "id": str(service_row.get("id") or target_service_id).strip(),
            "name": str(service_row.get("name") or target_service_name or target_service_id).strip(),
            "type": str(service_row.get("type") or "service").strip(),
            "description": str(service_row.get("description") or "").strip(),
        }
        target_service_name = target_service["name"]
        context.pending_data["_last_service_id"] = target_service["id"]
        context.pending_data["_last_service_name"] = target_service["name"]

    selected_property_ids = llm_orchestration_service._normalize_property_id_list(
        list(
            (
                cached_decision.selected_property_ids
                if cached_decision is not None
                else incoming_meta.get("selected_property_ids")
            )
            or incoming_meta.get("selected_property_scope_ids")
            or context.pending_data.get("_selected_property_scope_ids")
            or context.pending_data.get("_active_property_ids")
            or []
        )
    )
    selected_property_name = str(
        (cached_decision.selected_property_name if cached_decision is not None else "")
        or incoming_meta.get("selected_property_name")
        or context.pending_data.get("_selected_property_scope_name")
        or ""
    ).strip()
    if selected_property_ids:
        llm_orchestration_service._persist_book_answerer_focus(
            context=context,
            focus_ids=selected_property_ids,
            selected_phase_id=selected_phase_id or "pre_booking",
            selected_property_name=selected_property_name,
        )

    cached_suggested_actions = _filter_chip_quality(
        _sanitize_suggestions(
            list(
                (
                    cached_decision.suggested_actions
                    if cached_decision is not None
                    else []
                )
                or cached_payload.get("suggested_actions")
                or incoming_meta.get("suggested_actions")
                or []
            ),
            limit=3,
        ),
        limit=3,
    )
    assistant_meta = {
        "channel": context.channel,
        "prefetch_cache_hit": True,
        "response_source": str(incoming_meta.get("response_source") or "suggestion_prefetch_cache"),
        "prefetch_batch_id": str(incoming_meta.get("prefetch_batch_id") or ""),
        "prefetch_entry_id": str(incoming_meta.get("prefetch_entry_id") or ""),
        "prefetch_chip_text": str(incoming_meta.get("prefetch_chip_text") or ""),
        "prefetch_similarity": incoming_meta.get("prefetch_similarity"),
        "prefetch_match_mode": str(incoming_meta.get("prefetch_match_mode") or ""),
        "response_language": dict(incoming_meta.get("response_language") or {}),
    }
    context.add_message(MessageRole.ASSISTANT, assistant_text, metadata=assistant_meta)

    target_state = _coerce_conversation_state(
        cached_payload.get("state"),
        default=context.state if isinstance(context.state, ConversationState) else ConversationState.IDLE,
    )
    context.state = target_state

    decision_for_followups = (
        cached_decision.model_copy(deep=True)
        if cached_decision is not None
        else OrchestrationDecision(
            action="respond_only",
            response_text=assistant_text,
            suggested_actions=cached_suggested_actions,
            selected_property_name=selected_property_name,
            selected_property_ids=selected_property_ids,
            target_service_id=target_service_id,
            metadata={"source": "suggestion_prefetch_cache"},
        )
    )
    if not str(decision_for_followups.response_text or "").strip():
        decision_for_followups.response_text = assistant_text
    if cached_suggested_actions and not decision_for_followups.suggested_actions:
        decision_for_followups.suggested_actions = list(cached_suggested_actions)

    if request.session_id:
        try:
            llm_orchestration_service.schedule_post_response_chip_job(
                session_id=request.session_id,
                assistant_turn_id=str((request.metadata or {}).get("turn_trace_id") or "").strip(),
                user_message=user_text,
                context=context,
                decision=decision_for_followups,
                selected_phase_id=selected_phase_id or "pre_booking",
                selected_phase_name=selected_phase_name or "Pre Booking",
                target_service=target_service or None,
                memory_snapshot=conversation_memory_service.get_snapshot(context),
            )
        except Exception:
            pass

    try:
        await context_manager.save_context(context, db_session=db)
    except Exception:
        await context_manager.save_context(context, db_session=None)

    outgoing_meta = dict(incoming_meta)
    if team_booking_enabled(hotel_code):
        outgoing_meta["booking_details_submitted"] = booking_details_submitted(context)
    outgoing_meta.update(
        {
            "message_count": len(context.messages),
            "response_source": "suggestion_prefetch_cache",
            "prefetch_cache_hit": True,
            "prefetch_batch_id": str(incoming_meta.get("prefetch_batch_id") or ""),
            "prefetch_entry_id": str(incoming_meta.get("prefetch_entry_id") or ""),
            "prefetch_chip_text": str(incoming_meta.get("prefetch_chip_text") or ""),
            "prefetch_similarity": incoming_meta.get("prefetch_similarity"),
            "prefetch_match_mode": str(incoming_meta.get("prefetch_match_mode") or ""),
            "prefetch_threshold": incoming_meta.get("prefetch_threshold"),
            "selected_phase_id": selected_phase_id or None,
            "selected_phase_name": selected_phase_name or None,
            "selected_property_ids": selected_property_ids,
            "selected_property_name": selected_property_name or None,
            "target_service_id": target_service_id or None,
            "target_service_name": target_service_name or None,
        }
    )

    _pf_response = ChatResponse(
        session_id=request.session_id,
        message=assistant_text,
        display_message=str(cached_payload.get("display_message") or "").strip() or None,
        state=target_state,
        suggested_actions=cached_suggested_actions,
        metadata=outgoing_meta,
    )

    # A chip served from the prefetch cache skips process_message entirely, so
    # the live path's CTA attach never ran for it. The pre-cooked reply still
    # SAYS "Tap Share your booking details below" — written by the room-booking
    # service agent during prefetch — while no button is rendered, which strands
    # the guest on a dead end (observed live: the sentence with only chips under
    # it). Re-run the SAME attach here, keyed off the cached turn's own service
    # id, so the sentence and the button arrive together exactly as they do on a
    # live turn. `details_submitted` is read from the booking ledger so a guest
    # who already sent their details is not offered the spent button again.
    # Opt-IN by hotel_code, via the SAME team-only list the booking flow already
    # uses. Only a team-only hotel narrates "Tap Share your booking details" in
    # its pre-cooked answer, so only it can be stranded by the missing button.
    # Every other tenant (Zone) keeps the previous behaviour untouched — no
    # button is added to a chip answer that never had one.
    try:
        _pf_needs_cta = _booking_team_only_for_hotel(getattr(context, "hotel_code", ""))
    except Exception:
        _pf_needs_cta = False
    try:
        if not _pf_needs_cta:
            return _pf_response
        # WRITE THIS ON THE RESPONSE, NOT ON `outgoing_meta`.
        # ChatResponse is a pydantic model and validation COPIES the dict it is
        # given, so `_pf_response.metadata is not outgoing_meta`. Setting the
        # service id on `outgoing_meta` after construction therefore never
        # reached the object `_apply_booking_cta_marker` actually reads: it saw
        # no responder label, concluded this was not a room-booking turn, and
        # attached nothing. That is why a chip answer still said "Tap Share your
        # booking details below" with only chips underneath - the attach ran and
        # silently decided it had nothing to do.
        if not isinstance(_pf_response.metadata, dict):
            _pf_response.metadata = {}
        if not _pf_response.metadata.get("orchestration_target_service_id"):
            _pf_response.metadata["orchestration_target_service_id"] = str(
                target_service_id or ""
            )
        _pf_prop_names = [selected_property_name] if selected_property_name else []
        _pf_prop_ids = list(selected_property_ids or [])
        _pf_pending = context.pending_data if isinstance(context.pending_data, dict) else {}
        _apply_booking_cta_marker(
            _pf_response,
            hotel_code=str(getattr(context, "hotel_code", "") or ""),
            room_property_names=_pf_prop_names,
            room_property_ids=_pf_prop_ids,
            active_property_count=len(_pf_prop_ids),
            user_message=user_text,
            details_submitted=booking_details_submitted(context),
        )
    except Exception:
        logger.debug("prefetch cta attach failed", exc_info=True)

    return _pf_response


# ── Pre-stored location intro (LLM-free dropdown short-circuit) ──────────────
# When a multi-property guest picks a city from the dropdown, we serve a curated
# intro straight from new_bot_location_intros and skip the whole LLM pipeline.
# Property scope AND both conversation turns (the guest's "I'm exploring X" plus
# the curated body) are written to context, so the NEXT turn answers normally —
# knowing which property is active and what the guest was just shown. Because
# the served turn involves no model at all, 4.1 and 5.4 behave identically here.

# The two CTA source_types the frontend uses when a location is picked or the
# intro CTA fires. Only these (or an explicit metadata.intro_city) may trigger
# the short-circuit, so an arbitrary message that merely mentions a city never
# hijacks the pipeline.
_LOCATION_INTRO_SOURCE_TYPES = {"cta_location_picker", "cta_book_stay"}

# The two exact phrases the frontend sends when a city is picked from the
# dropdown (see ChatHarness.handleLocationPickerSelect / handleCtaContactSubmit).
# Matched as a SECONDARY trigger because the WS transport currently rewrites
# ui_source_type to "typed_input" (the hook reads metadata.source_type, which
# buildRequestMetadata never sets), so the source_type gate alone misses every
# WebSocket location pick. The message text is intact over WS, and the city is
# still validated against the closed-set manifest, so this can't hijack an
# arbitrary message — and it degrades safely to the LLM when it doesn't match.
_INTRO_PHRASE_RE = re.compile(
    r"^\s*(?:i'm exploring .+?\btell me about the hotels there\b"
    r"|tell me more about the .+?\bpropert(?:y|ies)\b)",
    re.IGNORECASE | re.DOTALL,
)


def _match_intro_city(
    manifest: list[dict[str, Any]],
    explicit_city: str,
    message: str,
) -> Optional[dict[str, Any]]:
    """Resolve a manifest entry (city -> property ids/names) for this turn.

    Explicit metadata.intro_city wins. Otherwise fall back to a CLOSED-SET
    match: a manifest label must appear as a whole word in the message. The
    closed set (the tenant's own city labels) is what makes bare text matching
    safe — we never free-parse an open-ended city out of the sentence.
    """
    norm = config_service._normalize_slug
    entries = [e for e in (manifest or []) if isinstance(e, dict)]

    want = norm(explicit_city)
    if want:
        for entry in entries:
            if norm(entry.get("label")) == want or norm(entry.get("id")) == want:
                return entry
        return None

    msg_l = str(message or "").lower()
    if not msg_l:
        return None
    # Longest label first so "Port Blair" is tried before any shorter overlap.
    for entry in sorted(entries, key=lambda e: -len(str(e.get("label") or ""))):
        label = str(entry.get("label") or "").strip().lower()
        if label and re.search(r"\b" + re.escape(label) + r"\b", msg_l):
            return entry
    return None


async def _build_location_intro_response(
    *,
    request: ChatRequest,
    hotel_code: str,
    entry: dict[str, Any],
    body: str,
    db: AsyncSession | None,
) -> ChatResponse:
    """Serve a curated location intro with full scope + history, no LLM call.

    Mirrors _build_prefetch_cached_response: writes the user turn and the canned
    assistant turn into context, pins property scope exactly as a real location
    pick would, schedules the async chip job, and returns the body.
    """
    try:
        context = await context_manager.get_or_create_context(
            session_id=request.session_id,
            hotel_code=hotel_code,
            guest_phone=request.guest_phone,
            channel=request.channel or (request.metadata or {}).get("channel") or None,
            db_session=db,
        )
    except OperationalError:
        context = await context_manager.get_or_create_context(
            session_id=request.session_id,
            hotel_code=hotel_code,
            guest_phone=request.guest_phone,
            channel=request.channel or (request.metadata or {}).get("channel") or None,
            db_session=None,
        )

    request_channel = request.channel or (request.metadata or {}).get("channel") or context.channel
    if request_channel:
        context.channel = str(request_channel).strip()

    if not isinstance(context.pending_data, dict):
        context.pending_data = {}

    # STAMP THE CHAT THREAD BEFORE WRITING EITHER TURN. This short-circuit skips
    # process_message entirely, so nothing else sets `_active_conversation_uid` —
    # and add_message reads the thread id from exactly there. Both turns were
    # being persisted with conversation_uid NULL, and the widget replays only the
    # rows matching the thread it is resuming: a guest picked a city, read the
    # curated intro (with its photo), reloaded, and the intro was simply gone.
    # Mirrors what the main chat path and the form-submit path already do.
    _li_meta = request.metadata if isinstance(request.metadata, dict) else {}
    _li_conv_uid = str(
        _li_meta.get("conversation_id") or _li_meta.get("conversationId") or ""
    ).strip()
    if _li_conv_uid:
        conversation_memory_service.reset_pending_for_conversation_rotation(
            context, _li_conv_uid
        )
        context.pending_data["_active_conversation_uid"] = _li_conv_uid

    user_text = str(request.message or "").strip()
    context.add_message(
        MessageRole.USER,
        user_text,
        metadata={"channel": context.channel, "location_intro": True},
    )

    property_ids = llm_orchestration_service._normalize_property_id_list(
        list(entry.get("property_ids") or [])
    )
    property_names = [str(n).strip() for n in (entry.get("property_names") or []) if str(n).strip()]
    label = str(entry.get("label") or "").strip()

    # Align scope order with the DISPLAY order in the curated body. The manifest
    # sorts properties alphabetically, which can differ from the order the guest
    # actually reads (e.g. Goa: body shows Calangute→Parra but the manifest emits
    # Parra→Calangute). If the scope-id order and the text order disagree, ordinal
    # anaphora ("the second one") becomes ambiguous and models resolve it
    # differently. Sorting the scope by first-mention in the body removes that.
    if len(property_ids) > 1 and len(property_names) == len(property_ids):
        import re as _re

        def _norm(s: str) -> str:
            return _re.sub(r"[^a-z0-9]+", "", str(s or "").lower())

        _nbody = _norm(body)

        def _first_mention(pair: tuple[str, str]) -> int:
            idx = _nbody.find(_norm(pair[1]))
            return idx if idx >= 0 else 10**9

        _ordered = sorted(zip(property_ids, property_names), key=_first_mention)
        property_ids = [pid for pid, _ in _ordered]
        property_names = [nm for _, nm in _ordered]
    # A single-property city gets a concrete pin name; a multi-property city
    # leaves the single-name mirror blank (the scope is the id list) and uses
    # the city label for display.
    selected_property_name = property_names[0] if len(property_names) == 1 else ""

    if property_ids:
        llm_orchestration_service._persist_book_answerer_focus(
            context=context,
            focus_ids=property_ids,
            selected_phase_id="pre_booking",
            selected_property_name=selected_property_name or label,
        )
    context.pending_data["_selected_phase_id"] = "pre_booking"

    body = str(body or "").strip()
    context.add_message(
        MessageRole.ASSISTANT,
        body,
        metadata={"channel": context.channel, "response_source": "location_intro"},
    )
    context.state = ConversationState.IDLE

    # Multi-property city: the curated body describes each hotel and ends on a
    # "which one would you like to explore?" line, so give the guest one button
    # per property instead of making them type the name. Order matches the body
    # (property_names was re-sorted by first mention above), so the buttons read
    # top-to-bottom in the same order the guest just read about them.
    # Single-property cities get none — there is nothing to choose between.
    #
    # Deliberately a DEDICATED metadata key rather than `suggested_actions`:
    # that field feeds the chip pipeline, and reusing it would (a) duplicate the
    # property names as bottom chips and (b) risk in-bubble buttons appearing on
    # every other turn that happens to carry suggested actions.
    property_choices: list[str] = list(property_names) if len(property_names) > 1 else []

    # Show the place, not just describe it. The guest has just picked a city, so
    # every property in scope gets a small showcase set (facade first, then a
    # room, then other facilities) — for BOTH hotels when the city has two.
    #
    # This is a direct gallery read ordered by category, NOT the reply-text
    # title matching the normal image path uses: on an intro turn the curated
    # body never happens to name an image, so title matching always returned
    # nothing. Scoped to this turn only, and it fails soft (no images) if the
    # gallery has not been categorised yet.
    intro_images: list[dict[str, Any]] = []
    try:
        _resolved_pids: list[int] = []
        for _pid_hint in property_ids:
            _rid = await image_tool_service.resolve_active_property_id(
                hotel_code=hotel_code, property_hint=str(_pid_hint)
            )
            if _rid is not None and _rid not in _resolved_pids:
                _resolved_pids.append(_rid)
        if _resolved_pids:
            intro_images = await image_tool_service.select_gallery_for_properties(
                hotel_code=hotel_code,
                property_ids=_resolved_pids,
                # 0 = no cap. The welcome shows the building and EVERY room
                # the property has a photo of, rather than a sample of three —
                # a guest choosing a hotel wants to see the rooms. Largest
                # today is a two-hotel Goa welcome at 18 images.
                per_property_limit=0,
                category_quota=(("facade", 0), ("room", 0)),
            )
    except Exception as _img_exc:
        logger.warning("location-intro image attach skipped (non-fatal): %r", _img_exc)
        intro_images = []

    decision_for_followups = OrchestrationDecision(
        action="respond_only",
        response_text=body,
        suggested_actions=[],
        selected_property_name=selected_property_name,
        selected_property_ids=property_ids,
        metadata={"source": "location_intro", "location_id": str(entry.get("id") or "")},
    )

    if request.session_id:
        try:
            llm_orchestration_service.schedule_post_response_chip_job(
                session_id=request.session_id,
                assistant_turn_id=str((request.metadata or {}).get("turn_trace_id") or "").strip(),
                user_message=user_text,
                context=context,
                decision=decision_for_followups,
                selected_phase_id="pre_booking",
                selected_phase_name="Pre Booking",
                target_service=None,
                memory_snapshot=conversation_memory_service.get_snapshot(context),
            )
        except Exception:
            pass

    try:
        await context_manager.save_context(context, db_session=db)
    except Exception:
        await context_manager.save_context(context, db_session=None)

    _intro_response = ChatResponse(
        session_id=request.session_id,
        message=body,
        state=ConversationState.IDLE,
        suggested_actions=[],
        metadata={
            "message_count": len(context.messages),
            "response_source": "location_intro",
            "location_id": str(entry.get("id") or ""),
            "property_choices": property_choices,
            **({"images": intro_images} if intro_images else {}),
            "selected_phase_id": "pre_booking",
            "selected_phase_name": "Pre Booking",
            "selected_property_ids": property_ids,
            "selected_property_name": selected_property_name or None,
        },
    )

    # PERSIST THE GALLERY THIS INTRO JUST SHOWED. This handler short-circuits the
    # whole pipeline — that is the point of it, the answer is curated and needs no
    # model — but it also meant it skipped the step that records what the turn
    # looked like. The guest picked a city, read the intro with its photos,
    # reloaded, and got the same words with the pictures gone. Everything else it
    # needs is already assembled above; this just writes it down.
    try:
        _intro_payload = _build_turn_display_payload(_intro_response)
        if _intro_payload and request.session_id:
            await context_manager.set_turn_display_payload(
                request.session_id,
                _intro_payload,
                conversation_uid=_li_conv_uid,
                db_session=None,
            )
    except Exception as _intro_disp_exc:  # noqa: BLE001 — never fail the intro
        logger.debug("location intro display payload skipped: %r", _intro_disp_exc)

    return _intro_response


async def _maybe_build_property_intro_response(
    *,
    request: ChatRequest,
    hotel_code: str,
    source_type: str,
    db: AsyncSession | None,
) -> Optional[ChatResponse]:
    """The curated message for a hotel the guest just TAPPED, or None.

    Same idea as the city intro one level down: the guest picked from a list, so
    the answer is known before they pick and there is nothing for a model to
    work out. Serving it from the DB makes it instant and exact.

    It replaces an instruction to the answerer that did not hold — tapping New
    Market kept opening with "Located in the vibrant heart of Kolkata near
    cultural landmarks like Indian Museum, Howrah Bridge, Victoria Memorial",
    describing the city the guest had read about one message earlier.

    Matched on the message being EXACTLY a property name, not on the click's
    source_type: the WS transport rewrites ui_source_type to "typed_input" (see
    the note above _INTRO_PHRASE_RE), so a source_type gate misses every pick a
    guest actually makes. Requiring the whole message to be the name keeps it
    safe — "is Parra Goa pet friendly?" is a question and still goes to the
    pipeline.
    """
    try:
        from services import location_intro_service

        text = str(request.message or "").strip()
        # cheap reject before touching the cache: a pick is a bare name
        if not text or len(text) > 70 or "?" in text:
            return None

        intro = await location_intro_service.find_property_intro_by_name(
            hotel_code=hotel_code, text=text
        )
        if not intro or not str(intro.get("body") or "").strip():
            return None  # not a property name, or no curated copy → normal pipeline
        # A HOTEL NAMED IN ANSWER TO "WHICH HOTEL?" CONTINUES THAT REQUEST (hotels set
        # up in the Pre-booking tab, T24 onward). After "Which hotel would you like to
        # book?" the guest tapped T24 Retro and got its intro, and the booking stopped
        # there (14 Oct). Zone, Mango and Sarovar keep the intro.
        if prebooking_settings_service.is_admin_managed(str(hotel_code or "").strip().lower()):
            _pick_ctx = await context_manager.get_context(request.session_id, db_session=None)
            _pick_pd = _pick_ctx.pending_data if _pick_ctx and isinstance(_pick_ctx.pending_data, dict) else {}
            if str(_pick_pd.get("_awaiting_hotel_for") or "").strip():
                logger.info(
                    "[PROPERTY-INTRO] %s picked for %s - continuing that request, no intro",
                    text[:40], _pick_pd.get("_awaiting_hotel_for"),
                )
                return None

        slug = str(intro.get("slug") or "").strip()
        name = str(intro.get("label") or "").strip()
        logger.info("[PROPERTY-INTRO] serving curated pick  hotel=%s  property=%s", hotel_code, slug)
        # A one-property "city": scope pinning, history, the facade+rooms gallery
        # and the chip job are all identical to a single-property location pick,
        # so reuse that builder rather than growing a second copy of it.
        return await _build_location_intro_response(
            request=request,
            hotel_code=hotel_code,
            entry={
                "id": f"prop::{slug}",
                "label": name,
                "property_ids": [slug],
                "property_names": [name],
            },
            body=str(intro["body"]).strip(),
            db=db,
        )
    except Exception:
        logger.exception("[PROPERTY-INTRO] short-circuit failed; falling through to pipeline")
        return None


def _persist_picked_destination(
    *, session_id: str, hotel_code: str, label: str
) -> None:
    """Record the destination the guest just picked from the dropdown.

    The opening lead form's "Destination" field is captured once per session.
    The dropdown shown at the start of every LATER conversation re-asks it, but
    its answer was only ever sent as a chat sentence and never written back — so
    the stored city stayed frozen at the first pick while the guest moved on to
    other destinations (one session held "Gopalpur" across 53 conversations).

    Writes both places that answer "which destination?": the session profile
    (location is latest-wins) and the dashboard's City column. Fire-and-forget —
    a failure here must never cost the guest their reply.
    """
    sid = str(session_id or "").strip()
    city = str(label or "").strip()
    if not sid or not city:
        return

    async def _write() -> None:
        try:
            from services import session_profile_service

            await session_profile_service.upsert_session_profile(
                session_id=sid,
                hotel_code=str(hotel_code or "").strip() or None,
                location=city,
            )
        except Exception:
            logger.warning(
                "location-pick: session location update skipped", exc_info=True
            )
        try:
            from services.customer_details_service import customer_details_service

            await customer_details_service.update_city(session_id=sid, city=city)
        except Exception:
            logger.warning(
                "location-pick: customer_details city update skipped", exc_info=True
            )

    try:
        asyncio.create_task(_write())
    except RuntimeError:
        # No running loop (tests / sync callers) — skip silently.
        pass


async def _maybe_build_location_intro_response(
    *,
    request: ChatRequest,
    hotel_code: str,
    source_type: str,
    db: AsyncSession | None,
) -> Optional[ChatResponse]:
    """Return a canned location-intro ChatResponse, or None to fall through.

    None is returned (→ normal LLM pipeline) whenever anything is off: feature
    disabled, wrong source_type, non-English, no city match, or no seeded row.
    Never raises onto the hot path.
    """
    try:
        if not bool(getattr(settings, "location_intro_shortcut_enabled", True)):
            return None

        st = str(source_type or "").strip().lower()
        meta = request.metadata if isinstance(request.metadata, dict) else {}
        explicit_city = str(meta.get("intro_city") or "").strip()
        message_text = str(request.message or "")
        # Admin-managed hotels (Bot Training -> Pre-booking): an opening button
        # or the lead form asks for the saved message outright.
        admin_managed = prebooking_settings_service.is_admin_managed(hotel_code)
        admin_asked = admin_managed and bool(meta.get("show_location_intro"))

        is_trigger = (
            bool(explicit_city)
            or admin_asked
            or st in _LOCATION_INTRO_SOURCE_TYPES
            or bool(_INTRO_PHRASE_RE.match(message_text))
        )
        if not is_trigger:
            return None

        # English-only: the canned bodies are English. A non-English request
        # falls through to the LLM (which mirrors the guest's language).
        lang = str(meta.get("language") or meta.get("reply_language") or "").strip().lower()
        if lang and not lang.startswith("en"):
            return None

        from services.kb_table_reader import get_property_catalog_db
        from services import location_intro_service

        rows = await get_property_catalog_db(hotel_code=hotel_code)
        rows = [dict(r) for r in (rows or []) if isinstance(r, dict)]
        if not rows:
            return None
        manifest = config_service._build_location_manifest_from_property_catalog(rows)
        if not manifest:
            return None

        entry = _match_intro_city(manifest, explicit_city, str(request.message or ""))
        if entry is None and admin_managed and len(manifest) == 1:
            # One hotel, or several hotels in one place (T24): there is only one
            # location, so it is the one the guest means.
            entry = manifest[0]
        if entry is None:
            return None

        location_id = str(entry.get("id") or "").strip()
        if not location_id:
            return None

        # The pick is confirmed against the closed-set manifest by here, so record
        # it BEFORE the curated-copy lookup: a city with no seeded intro row falls
        # through to the LLM below, but the guest still chose that destination.
        _persist_picked_destination(
            session_id=str(getattr(request, "session_id", "") or ""),
            hotel_code=hotel_code,
            label=str(entry.get("label") or ""),
        )

        intro = await location_intro_service.get_intro(
            hotel_code=hotel_code, location_id=location_id
        )
        if not intro or not str(intro.get("body") or "").strip():
            return None

        logger.info(
            "[LOCATION-INTRO] serving canned intro  hotel=%s  city=%s  props=%s",
            hotel_code, location_id, entry.get("property_ids"),
        )
        return await _build_location_intro_response(
            request=request,
            hotel_code=hotel_code,
            entry=entry,
            body=str(intro["body"]).strip(),
            db=db,
        )
    except Exception:
        logger.exception("[LOCATION-INTRO] short-circuit failed; falling through to pipeline")
        return None


def _service_kb_fact_lines(record: dict[str, Any], max_items: int = 16) -> list[str]:
    rows = record.get("facts", []) if isinstance(record, dict) else []
    if not isinstance(rows, list):
        return []
    lines: list[str] = []
    for fact in rows:
        if not isinstance(fact, dict):
            continue
        text = str(fact.get("text") or "").strip()
        if not text:
            continue
        lines.append(text)
        if len(lines) >= max_items:
            break
    return lines


def _display_kb_source_name(value: str) -> str:
    name = str(value or "").strip()
    if not name:
        return "kb_source"
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = re.sub(r"^[0-9a-f]{8}_", "", name, flags=re.IGNORECASE)
    return cleaned or name


async def _load_all_kb_text_from_db(
    *,
    db: AsyncSession,
    hotel_code: str,
    max_chars: int = 50000,
) -> str:
    """Best-effort DB fallback for combined KB text when file sources are stale/empty."""
    code = config_service.resolve_hotel_code(hotel_code)
    hotel_id: Optional[int] = None
    try:
        result = await db.execute(
            select(Hotel.id).where(Hotel.code.ilike(code)).limit(1)
        )
        hotel_id = result.scalar_one_or_none()
        if hotel_id is None:
            return ""

        rows = await db.execute(
            select(KBFile.stored_name, KBFile.content)
            .where(KBFile.hotel_id == hotel_id)
            .order_by(KBFile.id.asc())
        )
        blocks: list[str] = []
        for stored_name, content in rows.all():
            body = str(content or "").strip()
            if not body:
                continue
            source_name = _display_kb_source_name(str(stored_name or "kb_file"))
            blocks.append(f"=== SOURCE: {source_name} (db) ===\n{body}")
        combined = "\n\n".join(blocks).strip()
        if not combined:
            return ""
        if max_chars and max_chars > 0:
            return combined[:max_chars]
        return combined
    except Exception:
        return ""


def _record_evaluation_event(request: ChatRequest, response: ChatResponse, trace_id: str) -> None:
    """Best-effort evaluation event recording."""
    if not bool(getattr(settings, "evaluation_metrics_enabled", True)):
        return
    try:
        evaluation_metrics_service.record_chat_response(
            request=request,
            response=response,
            trace_id=trace_id,
        )
    except Exception as record_error:
        observability_service.log_event(
            "evaluation_metrics_record_failed",
            {
                "trace_id": trace_id,
                "session_id": request.session_id,
                "hotel_code": request.hotel_code,
                "error": str(record_error),
            },
        )


def _trace_chat_event(
    event: str,
    *,
    trace_id: str = "",
    turn_trace_id: str = "",
    http_request: Request | None = None,
    request: ChatRequest | None = None,
    response: ChatResponse | None = None,
    status_code: int | None = None,
    error: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    request_meta = request.metadata if request is not None and isinstance(request.metadata, dict) else {}
    response_meta = response.metadata if response is not None and isinstance(response.metadata, dict) else {}
    endpoint = (
        str(http_request.url.path or "").strip()
        if http_request is not None and getattr(http_request, "url", None) is not None
        else "/api/chat/message"
    )
    method = (
        str(http_request.method or "").strip()
        if http_request is not None
        else "POST"
    )
    payload = {
        "request_message": str(request.message or "").strip() if request is not None else "",
        "request_message_length": len(str(request.message or "")) if request is not None else 0,
        "request_metadata_keys": sorted([str(k) for k in request_meta.keys()])[:80],
        "response_message_length": len(str(response.message or "")) if response is not None else 0,
        "response_state": str(getattr(response.state, "value", response.state) or "") if response is not None else "",
        "response_intent": str(response_meta.get("classified_intent") or "") if response_meta else "",
        "response_source": str(response_meta.get("response_source") or "") if response_meta else "",
        "routing_path": str(response_meta.get("routing_path") or "") if response_meta else "",
        "ticket_created": bool(response_meta.get("ticket_created", False)) if response_meta else False,
    }
    if extra:
        payload.update(extra)
    session_id = str(request.session_id or "").strip() if request is not None else ""
    hotel_code = str(request.hotel_code or "").strip() if request is not None else ""
    channel = (
        str(request.channel or request_meta.get("channel") or "").strip()
        if request is not None
        else ""
    )

    backend_trace_service.log_event(
        event,
        payload,
        trace_id=trace_id,
        turn_trace_id=turn_trace_id,
        session_id=session_id,
        hotel_code=hotel_code,
        channel=channel,
        endpoint=endpoint,
        method=method,
        status_code=status_code,
        component="api.routes.chat.send_message",
        error=error,
    )
    everything_trace_service.log_event(
        event,
        {
            **payload,
            "request_metadata": request_meta,
            "response_metadata": response_meta,
            "request_message_full": str(request.message or "") if request is not None else "",
            "response_message_full": str(response.message or "") if response is not None else "",
        },
        trace_id=trace_id,
        turn_trace_id=turn_trace_id,
        session_id=session_id,
        hotel_code=hotel_code,
        channel=channel,
        endpoint=endpoint,
        method=method,
        status_code=status_code,
        component="api.routes.chat.send_message",
        error=error,
    )
    if turn_trace_id:
        turn_diagnostics_service.log_event(event, payload)


def _enrich_service_llm_display_fields(response: ChatResponse) -> None:
    """Ensure service-label display fields are always present for UI clients."""
    if not isinstance(response.metadata, dict):
        response.metadata = {}
    metadata = response.metadata

    label = str(
        response.service_llm_label
        or metadata.get("service_llm_label")
        or metadata.get("orchestration_target_service_id")
        or ""
    ).strip()
    if not label:
        label = "main"

    confidence_candidates = (
        response.service_llm_confidence,
        metadata.get("service_llm_confidence"),
        metadata.get("classified_confidence"),
        metadata.get("confidence"),
    )
    parsed_confidence: Optional[float] = None
    for candidate in confidence_candidates:
        try:
            value = float(candidate)
        except (TypeError, ValueError):
            continue
        if value != value:  # NaN guard
            continue
        parsed_confidence = max(0.0, min(1.0, value))
        break

    response.service_llm_label = label
    response.service_llm_confidence = parsed_confidence
    metadata["service_llm_label"] = label
    if parsed_confidence is not None:
        metadata["service_llm_confidence"] = parsed_confidence


# Marker the Room Booking agent emits when IT decides the guest should proceed
# to booking. The decision is the LLM's; this just turns that decision into a
# UI button. The marker is stripped from the visible reply.
_BOOKING_CTA_MARKER = "<<SHOW_BOOKING_BUTTON>>"


def _load_property_booking_url_map() -> dict[str, dict[str, str]]:
    """Parse settings.chat_property_booking_url_map_json.

    Shape: { hotel_code: { property_match_substring: booking_url } }. Keys
    lowercased. The map is tiny so parsing per call is negligible.
    """
    raw = str(getattr(settings, "chat_property_booking_url_map_json", "") or "")
    if not str(raw or "").strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for code, prop_map in parsed.items():
        if not isinstance(prop_map, dict):
            continue
        inner = {
            str(k or "").strip().lower(): str(v or "").strip()
            for k, v in prop_map.items()
            if str(k or "").strip() and str(v or "").strip()
        }
        code_key = str(code or "").strip().lower()
        if code_key and inner:
            out[code_key] = inner
    return out


def _resolve_property_booking_url(hotel_code: str, *texts: str) -> str:
    """Return the real per-property booking URL for the given property name/slug.

    Longest matching key wins (so "connect park coimbatore" beats "coimbatore"
    and "new market" beats "kolkata"). Returns "" when nothing matches so the
    caller keeps its placeholder/business-website fallback.
    """
    inner = _load_property_booking_url_map().get(str(hotel_code or "").strip().lower())
    if not inner:
        return ""
    blob = " ".join(str(t or "") for t in texts).lower().replace("_", " ")
    if not blob.strip():
        return ""
    best_url, best_len = "", 0
    for key, url in inner.items():
        if key and key in blob and len(key) > best_len:
            best_len, best_url = len(key), url
    return best_url


_BOOKING_DATE_MONTHS: dict[str, int] = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_BOOKING_DATE_MONTH_NAMES: dict[int, str] = {
    1: "January", 2: "February", 3: "March", 4: "April", 5: "May", 6: "June",
    7: "July", 8: "August", 9: "September", 10: "October", 11: "November",
    12: "December",
}


def _detect_impossible_calendar_date(user_message: str) -> str:
    """Return a clarification message when the guest named an impossible calendar
    date (e.g. "31 feb", "30 feb", "29 feb" in a non-leap year); else "".

    Deterministic calendar check — the LLM catches these only inconsistently
    (prod logs showed "book 31 feb" accepted on one turn and rejected on the
    next). Only month-NAME forms are considered ("31 feb", "feb 31"); bare
    numeric d/m is ambiguous and skipped, so false positives on ordinary text
    are essentially nil. Valid dates return "" and never alter the reply.
    """
    import calendar as _calendar
    import datetime as _datetime
    import re as _re

    text = str(user_message or "").strip().lower()
    if not text:
        return ""
    months_alt = "|".join(sorted(_BOOKING_DATE_MONTHS, key=len, reverse=True))
    candidates: list[tuple[int, int]] = []
    for m in _re.finditer(r"\b(\d{1,2})\s*(?:st|nd|rd|th)?\s+(" + months_alt + r")\b", text):
        candidates.append((int(m.group(1)), _BOOKING_DATE_MONTHS[m.group(2)]))
    for m in _re.finditer(r"\b(" + months_alt + r")\s+(\d{1,2})(?:st|nd|rd|th)?\b", text):
        candidates.append((int(m.group(2)), _BOOKING_DATE_MONTHS[m.group(1)]))
    if not candidates:
        return ""
    _year_match = _re.search(r"\b(20\d{2})\b", text)
    year = int(_year_match.group(1)) if _year_match else _datetime.date.today().year
    for day, month in candidates:
        max_day = _calendar.monthrange(year, month)[1]
        if day < 1 or day > max_day:
            month_name = _BOOKING_DATE_MONTH_NAMES[month]
            if month == 2 and day == 29 and max_day == 28:
                return (
                    f"{month_name} 29 isn't a valid date in {year} - {year} isn't a "
                    "leap year. Could you share a valid check-in date?"
                )
            return (
                f"{month_name} {day} isn't a valid date - {month_name} has only "
                f"{max_day} days"
                + (f" in {year}" if month == 2 else "")
                + ". Could you share a valid check-in date?"
            )
    # Valid calendar date(s) — now reject any that are in the PAST: a booking
    # check-in can't be before today. Year = an explicit 4-digit year if the
    # guest gave one, else the current year.
    today = _datetime.date.today()
    for day, month in candidates:
        max_day = _calendar.monthrange(year, month)[1]
        if not (1 <= day <= max_day):
            continue
        try:
            picked = _datetime.date(year, month, day)
        except ValueError:
            continue
        if picked < today:
            month_name = _BOOKING_DATE_MONTH_NAMES[month]
            if not _year_match:
                return (
                    f"{month_name} {day} has already passed this year. Did you mean "
                    f"{month_name} {day}, {year + 1} - or a different date? Please share a future check-in date."
                )
            return (
                f"{month_name} {day}, {year} is in the past. Could you share a future check-in date?"
            )
    return ""


def _apply_booking_cta_marker(
    response: ChatResponse,
    *,
    hotel_code: str = "",
    lead_active: bool = False,
    link_clicked: bool = False,
    room_property_names: list[str] | None = None,
    room_property_ids: list[str] | None = None,
    active_property_count: int | None = None,
    user_message: str = "",
    details_submitted: bool = False,
) -> None:
    """Attach a `booking_cta` (rendered as a "Book on website" button) when:
      (1) the Room Booking agent emitted the <<SHOW_BOOKING_BUTTON>> marker
          (LLM-decided) — strip the marker and attach; OR
      (2) a CTA booking lead is already active (the guest gave their details up
          front via "Book a Stay") AND this turn was answered by the room-booking
          agent — so the button rides along with the room types/offers.

    Case (2) is small deterministic glue: when we KNOW the guest's details are on
    file and the room agent is talking rooms, surfacing the booking button is
    always useful. The conversational content stays LLM-driven.
    """
    import re as _re

    # Marker can be bare ("<<SHOW_BOOKING_BUTTON>>") or carry a property name
    # ("<<SHOW_BOOKING_BUTTON: Zone Connect Park Calangute Goa>>"). The agent emits
    # one per property it's giving a link for (e.g. two for "give me both links").
    _marker_re = _re.compile(r"<<SHOW_BOOKING_BUTTON(?::\s*([^>]+?))?\s*>>", _re.IGNORECASE)

    def _placeholder_url(property_name: str) -> str:
        # Placeholder per-property booking URL — swap for the real per-property
        # links (or KB-sourced URLs) when provided.
        slug = _re.sub(r"[^a-z0-9]+", "-", str(property_name or "").strip().lower()).strip("-")
        if not slug:
            slug = str(hotel_code or (response.metadata or {}).get("hotel_code") or "book").strip().lower() or "book"
        return f"https://booking.example.com/{slug}"

    def _collect_markers(text: object) -> list[str]:
        return [m.group(1).strip() if m.group(1) else "" for m in _marker_re.finditer(str(text or ""))]

    def _strip(text: object) -> str:
        cleaned = _marker_re.sub("", str(text or ""))
        # Remove stray control characters (e.g. a leftover STX/\x02 next to a
        # marker) that otherwise show up as a broken glyph in the reply. Keep
        # newline (\n) and tab (\t); strip the rest of C0.
        cleaned = _re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", cleaned)
        cleaned = _re.sub(r"\n{3,}", "\n\n", cleaned).strip()
        return cleaned

    # LIVE RATES: the room-booking lane names the guest's dates with
    # <<SHOW_LIVE_RATES: check_in|check_out>> (a price question, or a booking ask
    # with dates). It asks for the buttons as explicitly as the booking marker
    # does; the dates go into the "Book on website" link built further down.
    _live_re = _re.compile(r"<<SHOW_LIVE_RATES:\s*(\d{4}-\d{2}-\d{2})\|(\d{4}-\d{2}-\d{2})\s*>>", _re.IGNORECASE)
    _meta_live = response.metadata if isinstance(response.metadata, dict) else {}
    _live_dates: tuple[str, str] | None = None
    for _field in (response.message, getattr(response, "display_message", ""),
                   _meta_live.get("display_message"), _meta_live.get("canonical_message")):
        _m_live = _live_re.search(str(_field or ""))
        if _m_live:
            _live_dates = (_m_live.group(1), _m_live.group(2))
            break
    if _live_dates is not None:
        def _strip_live(text: object) -> str:
            return _re.sub(r"\n{3,}", "\n\n", _live_re.sub("", str(text or ""))).strip()
        for _attr in ("message", "display_message"):
            _val = getattr(response, _attr, "") or ""
            if _val:
                try:
                    setattr(response, _attr, _strip_live(_val))
                except Exception:
                    pass
        if isinstance(response.metadata, dict):
            for _key in ("display_message", "canonical_message"):
                if response.metadata.get(_key):
                    response.metadata[_key] = _strip_live(response.metadata[_key])

    # A team-only hotel with a live-rates link (Mango): the website button is never
    # spent - not by sending the team form, not by an earlier click. Only "Share
    # your booking details" is one-time.
    _team_only_live = bool(team_booking_enabled(hotel_code) and live_rates_url_template(hotel_code))

    # THE GUEST HAS ALREADY SENT THEIR DETAILS. The button is spent for the rest
    # of the conversation (`once_per_conversation`), so offering it again reads
    # as if we lost the first submission. Only the BUTTON is withheld here - what
    # the reply SAYS is the agent's job, and it is told the booking exists
    # through `_rb_bookings` in its own context (see submit_booking_assistance).
    # Only let the response metadata SPEAK when it actually carries the flag.
    # It is set on the prefetch path; on a live turn the key is absent, and
    # reading it unconditionally overwrote a true `details_submitted` with
    # False. That is why a guest who had already submitted was told "please
    # share your booking details using the button below" with no button under
    # it - the caller knew the booking existed, and this line threw that away.
    if team_booking_enabled(hotel_code) and isinstance(response.metadata, dict) \
            and "booking_details_submitted" in response.metadata:
        details_submitted = bool(response.metadata.get("booking_details_submitted"))
    # A HOTEL WITH SEVERAL PROPERTIES FOLLOWS ZONE: THE BUTTONS ALWAYS SHOW.
    # Hotels set up in the Pre-booking tab with two or more properties (T24
    # onward): a guest who sent their details for one hotel may book the other,
    # so an earlier submission spends nothing and every booking turn gets its
    # buttons. On T24 the guest sent the team form for one hotel, then asked to
    # book the other; every later booking reply, including "you can proceed via
    # the website link below", came with no buttons. Single-property hotels keep
    # the rule below; Zone, Mango and Sarovar are unchanged.
    if details_submitted:
        try:
            _admin_now = prebooking_settings_service.settings_for(hotel_code)
            if _admin_now is not None and len(
                [k for k in (_admin_now.get("properties") or {}) if k]
            ) >= 2:
                details_submitted = False
        except Exception:
            pass
    if details_submitted:
        _meta_ds = response.metadata if isinstance(response.metadata, dict) else {}
        _ds_booking_turn = bool(
            _live_dates is not None
            or any(_marker_re.search(str(_f or "")) for _f in (
                response.message, getattr(response, "display_message", ""),
                _meta_ds.get("display_message"), _meta_ds.get("canonical_message"),
            ))
            or ("room" in str(getattr(response, "service_llm_label", "") or _meta_ds.get("service_llm_label")
                              or _meta_ds.get("orchestration_target_service_id") or "").lower()
                and "book" in str(getattr(response, "service_llm_label", "") or _meta_ds.get("service_llm_label")
                                  or _meta_ds.get("orchestration_target_service_id") or "").lower())
        )
        if isinstance(response.metadata, dict):
            response.metadata.pop("assist_cta", None)
            response.metadata.pop("booking_cta", None)
        # The marker is the agent's own emitted field, so it still has to be
        # stripped - otherwise the raw "<<SHOW_BOOKING_BUTTON: ...>>" prints to
        # the guest as text.
        for _attr in ("message", "display_message"):
            _val = str(getattr(response, _attr, "") or "")
            if _val and _marker_re.search(_val):
                try:
                    setattr(response, _attr, _strip(_val))
                except Exception:
                    pass
        if isinstance(response.metadata, dict):
            for _key in ("display_message", "canonical_message"):
                _val = str(response.metadata.get(_key) or "")
                if _val and _marker_re.search(_val):
                    response.metadata[_key] = _strip(_val)

        # THE BUTTON IS GONE, SO THE SENTENCE NAMING IT MUST GO TOO.
        # The prompt already forbids this four separate ways - the agent is sent
        # `details_submitted: true`, `available_action: none. ... Never ask the
        # guest to tap it`, `never tell the guest to tap it` and the list of
        # requests ALREADY SENT - and the model still produced "Share your
        # booking details button par tap kar sakte hain" (verified against the
        # exact prompt it received, 2026-09-28). A prompt-only guard cannot make
        # this invariant hold; the code that WITHHOLDS the button is the only
        # thing that knows for certain it is not on screen, so it is what has to
        # take the sentence out. Same shape as the attach on the other side:
        # there the reply's claim summons the button, here the button's absence
        # retracts the claim.
        #
        # Sentence-scoped, and only sentences carrying the button's own label,
        # so the rest of the answer is untouched. The label stays in English
        # even inside a Hindi or Punjabi reply, which is what makes this work
        # across languages. Fails safe: if removal would empty the reply, the
        # original is kept - a stale sentence beats a blank answer.
        _label = "share your booking details"
        def _drop_button_sentences(text: str) -> str:
            if _label not in text.casefold():
                return text
            kept: list[str] = []
            for _line in text.split("\n"):
                if _label not in _line.casefold():
                    kept.append(_line)
                    continue
                # Keep the parts of the line that do not name the button.
                _parts = _re.split(r"(?<=[.!?])\s+", _line)
                _survivors = [p for p in _parts if _label not in p.casefold()]
                kept.append(" ".join(_survivors).strip())
            _out = _re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
            return _out or text

        for _attr in ("message", "display_message"):
            _val = str(getattr(response, _attr, "") or "")
            if _val:
                try:
                    setattr(response, _attr, _drop_button_sentences(_val))
                except Exception:
                    pass
        if isinstance(response.metadata, dict):
            for _key in ("display_message", "canonical_message"):
                _val = str(response.metadata.get(_key) or "")
                if _val:
                    response.metadata[_key] = _drop_button_sentences(_val)
            # Mango: the website link stays on every booking / room / price turn after
            # the team form is sent - the guest's dates when named, else tonight.
            if _team_only_live and _ds_booking_turn:
                if _live_dates is None:
                    from datetime import datetime as _dt_ds, timedelta as _td_ds
                    from zoneinfo import ZoneInfo as _Zone_ds

                    _t_ds = _dt_ds.now(_Zone_ds("Asia/Kolkata")).date()
                    _live_dates = (_t_ds.isoformat(), (_t_ds + _td_ds(days=1)).isoformat())
                _live_url_after = live_rates_url(hotel_code, *_live_dates)
                if _live_url_after:
                    response.metadata["booking_cta"] = {"url": _live_url_after, "label": "Book on website"}
        return

    metadata = response.metadata if isinstance(response.metadata, dict) else {}
    # Gather property names from every marker across the message fields.
    marker_props: list[str] = []
    for _field in (
        response.message,
        getattr(response, "display_message", ""),
        metadata.get("display_message"),
        metadata.get("canonical_message"),
    ):
        marker_props.extend(_collect_markers(_field))
    marker_present = bool(_marker_re.search(str(response.message or ""))
        or _marker_re.search(str(getattr(response, "display_message", "") or ""))
        or _marker_re.search(str(metadata.get("display_message") or ""))
        or _marker_re.search(str(metadata.get("canonical_message") or ""))
        or _live_dates is not None)

    # Case (2): lead active + this turn was the room-booking agent.
    _responder_label = str(
        getattr(response, "service_llm_label", "")
        or metadata.get("service_llm_label")
        or metadata.get("orchestration_target_service_id")
        or ""
    ).strip().lower()
    _is_room_responder = "room" in _responder_label and "book" in _responder_label

    # ── Invalid booking-date guard (deterministic) ────────────────────────────
    # On a booking turn, if the guest asked to book on an impossible calendar date
    # ("31 feb", "30 feb", "29 feb" in a non-leap year), don't hand them a booking
    # link — ask for a valid date instead. Runs before any button is attached so
    # the link never rides along with a nonsense date. Valid dates are ignored.
    _booking_turn = bool(
        marker_present
        or _is_room_responder
        or (isinstance(response.metadata, dict) and response.metadata.get("booking_cta"))
    )
    if _booking_turn:
        _bad_date_msg = _detect_impossible_calendar_date(user_message)
        if _bad_date_msg:
            response.message = _bad_date_msg
            try:
                response.display_message = _bad_date_msg
            except Exception:
                pass
            if isinstance(response.metadata, dict):
                response.metadata["display_message"] = _bad_date_msg
                response.metadata["canonical_message"] = _bad_date_msg
                response.metadata.pop("booking_cta", None)
                response.metadata["booking_date_invalid"] = True
                # Strip any form the booking flow attached to this turn — an
                # invalid-date reply must be a plain "give me a valid date" line,
                # NEVER a contact/lead form. Without this, the form rendered on
                # top of the message and blocked the whole chat.
                for _fk in (
                    "form_trigger", "form_fields", "form_service_id",
                    "render", "render_type", "support_form_submit_required",
                ):
                    response.metadata.pop(_fk, None)
                response.metadata["booking_date_invalid_form_suppressed"] = True
            return
    # Auto-show the "Book on website" button whenever the room-booking agent is
    # the responder (it's presenting rooms/offers, so the guest is in the booking
    # lane and the website link belongs alongside). Suppress it once the guest
    # has clicked it — it returns only on an explicit ask, which arrives as the
    # LLM marker handled above. A CTA lead being active also qualifies but is no
    # longer required. The room agent itself cannot reliably emit the marker
    # (its strict JSON/format contract drops the <<...>> token), so we attach the
    # button deterministically here based on who answered + the active scope.
    # Show the button on room-agent turns, but NOT when two or more properties
    # are in focus (the guest hasn't narrowed down yet). We only suppress when
    # the count is DEFINITIVELY >= 2; an unknown/zero count still shows the
    # button (so it never silently disappears in the normal single-property
    # flow where the count may not be surfaced).
    _too_many_properties = isinstance(active_property_count, int) and active_property_count >= 2

    # When 2+ properties are still in focus, NO booking button — the guest must
    # pick one first (we show each property's name + description instead). This
    # overrides BOTH the auto-button and any link marker the answerer emitted:
    # strip the marker text so it doesn't leak, but attach no booking_cta.
    if _too_many_properties:
        if marker_present:
            if response.message:
                response.message = _strip(response.message)
            if getattr(response, "display_message", None):
                response.display_message = _strip(response.display_message) or None
            if isinstance(response.metadata, dict):
                if response.metadata.get("display_message"):
                    response.metadata["display_message"] = _strip(response.metadata["display_message"])
                if response.metadata.get("canonical_message"):
                    response.metadata["canonical_message"] = _strip(response.metadata["canonical_message"])
            # A BUTTON FOR EACH HOTEL, NOT NONE (hotels set up in the Pre-booking tab,
            # T24 onward). The reply offered the booking link with two hotels in
            # focus and every button was taken away, so "You can complete your room
            # booking on our website" reached the guest twice with nothing to click
            # (T24, 14 Oct). Each hotel in focus gets its own button, named for it,
            # so nothing is chosen for the guest. Zone, Mango and Sarovar unchanged.
            try:
                _admin_multi = prebooking_settings_service.settings_for(hotel_code)
                if _admin_multi is not None and isinstance(response.metadata, dict) \
                        and not response.metadata.get("booking_cta"):
                    _props_multi = _admin_multi.get("properties") or {}
                    _focus_multi = [
                        str(x).strip() for x in (response.metadata.get("active_property_ids") or [])
                        if str(x).strip() in _props_multi
                    ] or [slug for slug in _props_multi if slug]
                    _links_multi = [
                        b for b in (prebooking_settings_service.room_booking_buttons(hotel_code, _responder_label) or [])
                        if b.get("action") == "link"
                    ]
                    _ctas_multi: list[dict[str, str]] = []
                    _seen_multi: set[str] = set()
                    for _link in _links_multi:
                        _label_multi = str(_link.get("label") or "Book on website")
                        for _slug in _focus_multi:
                            _entry = _props_multi.get(_slug) or {}
                            _own_url = str((_link.get("property_urls") or {}).get(_slug) or "")
                            _url = _own_url or str(_link.get("url") or "")
                            if not _url or _url in _seen_multi:
                                continue
                            _seen_multi.add(_url)
                            _short = next(
                                (str(a).strip() for a in (_entry.get("aliases") or []) if str(a).strip()),
                                str(_entry.get("name") or _slug).strip(),
                            )
                            _ctas_multi.append({
                                "url": _url,
                                # A shared link is one button for all; a hotel's own link is named for it.
                                "label": f"{_label_multi} · {_short}" if _own_url else _label_multi,
                            })
                    if _ctas_multi:
                        response.metadata["booking_cta"] = _ctas_multi[0] if len(_ctas_multi) == 1 else _ctas_multi
            except Exception:
                logger.debug("per-hotel booking buttons skipped", exc_info=True)
        return

    auto_button = bool(_is_room_responder and (not link_clicked or _team_only_live))

    # CONTEXT GUARD (UI plumbing, not routing): the deterministic auto-button
    # fires on EVERY room-agent turn, but a booking link only belongs on turns
    # that actually present rooms/offers or move toward booking — NOT on a plain
    # info answer that ends in a staff-handoff offer ("can I get an extra bed?"
    # → "...would you like our team to reach out?"). When the agent EXPLICITLY
    # decided to show the link it emits the marker, so the marker path is never
    # suppressed here; we only hold back the *automatic* button when this turn
    # reads as a handoff/decline/info-gap rather than a rooms/offers reply.
    if auto_button and not marker_present:
        _texts = " ".join(
            str(t or "")
            for t in (
                response.message,
                getattr(response, "display_message", ""),
                (response.metadata or {}).get("display_message") if isinstance(response.metadata, dict) else "",
            )
        ).lower()
        _handoff_or_decline = any(
            phrase in _texts
            for phrase in (
                "team to reach out",
                "our team reach out",
                "team get in touch",
                "team to get in touch",
                "connect you with",
                "connect you to",
                "have someone reach",
                "have our team",
                "staff to connect",
                "get back to you",
                "can't do that through chat",
                "cannot do that through chat",
                "can't arrange that through chat",
                "i don't have that detail",
                "i do not have that detail",
                "don't have that information",
            )
        )
        # If the turn also clearly presents rooms/offers (a ⭐ offer line or an
        # explicit room/offer/booking framing), it's a genuine booking-lane reply
        # even if it tacks on a handoff line — keep the button in that case.
        _has_room_or_offer = ("⭐" in _texts) or ("room type" in _texts) or ("book on our website" in _texts) or ("complete your booking" in _texts)
        if _handoff_or_decline and not _has_room_or_offer:
            auto_button = False

    if not marker_present and not auto_button:
        return

    if marker_present:
        if response.message:
            response.message = _strip(response.message)
        if getattr(response, "display_message", None):
            response.display_message = _strip(response.display_message) or None
        if isinstance(response.metadata, dict):
            if response.metadata.get("display_message"):
                response.metadata["display_message"] = _strip(response.metadata["display_message"])
            if response.metadata.get("canonical_message"):
                response.metadata["canonical_message"] = _strip(response.metadata["canonical_message"])

    if isinstance(response.metadata, dict) and not response.metadata.get("booking_cta"):
        ctas: list[dict[str, str]] = []
        seen_urls: set[str] = set()
        # Property names to build buttons for: properties the agent NAMED in
        # markers take priority; otherwise (the deterministic auto-button) use
        # the active property scope passed in, so each in-focus property gets its
        # own "Book on website" button (e.g. two buttons when two are shown).
        # Build (property_name, property_id) pairs. Marker-named properties take
        # priority; otherwise use the active scope (names + aligned ids). The id
        # (slug) makes URL resolution robust to display-name variants.
        _marker_names = [str(p or "").strip() for p in marker_props if str(p or "").strip()]
        if _marker_names:
            _pairs = [(n, "") for n in _marker_names]
        else:
            _scope_names = [str(n).strip() for n in (room_property_names or []) if str(n).strip()]
            _scope_ids = [str(i).strip() for i in (room_property_ids or [])]
            _pairs = [
                (_scope_names[i], _scope_ids[i] if i < len(_scope_ids) else "")
                for i in range(len(_scope_names))
            ]
        # Admin-managed hotels (Bot Training -> Pre-booking) take their buttons
        # from the room-booking service's button list: each link button gives
        # the link of the property in focus (or its shared link), the form button
        # becomes the team-help form below. No placeholder link ever - a property
        # with no link gets no website button. Other hotels: unchanged.
        _admin = prebooking_settings_service.settings_for(hotel_code)
        _room_buttons = (
            prebooking_settings_service.room_booking_buttons(hotel_code, _responder_label) or []
            if _admin is not None else []
        )
        _admin_links = [b for b in _room_buttons if b.get("action") == "link"]
        _admin_form = next((b for b in _room_buttons if b.get("action") == "form"), None)
        for prop, prop_id in (_pairs or ([("", "")] if _admin is not None else [])):
            prop = str(prop or "").strip()
            if _admin is not None:
                for _link in _admin_links:
                    url = prebooking_settings_service.link_url_for(hotel_code, _link, prop, prop_id)
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        ctas.append({"url": url, "label": str(_link.get("label") or "Book on website")})
                continue
            # Real per-property booking URL when configured; else placeholder.
            url = _resolve_property_booking_url(hotel_code, prop, prop_id) or _placeholder_url(prop)
            # Label is always the short, generic "Book on website" — the URL is
            # still per-property (slug from the property name), so the right
            # property's site opens; only the visible text is kept compact.
            label = "Book on website"
            if url in seen_urls:
                continue
            seen_urls.add(url)
            ctas.append({"url": url, "label": label})
        # No property name resolved (count is 0/unknown, or no marker named one).
        # Multi-property (count >= 2) was already suppressed by the early return
        # above, so reaching here means a single or unknown scope — show a generic
        # "Book on website" button rather than dropping it entirely.
        if not ctas and _admin is None:
            ctas.append({"url": _placeholder_url(""), "label": "Book on website"})
        # Team-only hotels have no real booking engine, so the website link is
        # dropped entirely and only the team button below is offered.
        _team_only = _booking_team_only_for_hotel(hotel_code)
        # Single → object (back-compat); multiple → list. Frontend handles both.
        if not _team_only and ctas:
            response.metadata["booking_cta"] = ctas[0] if len(ctas) == 1 else ctas
        # A team-only hotel whose booking engine is configured for live rates
        # (Mango, via CHAT_LIVE_RATES_URL_BY_HOTEL_JSON) gets that engine's search
        # beside the team button: the guest's dates when the lane named them,
        # else tonight to tomorrow, as the hotel's own website opens it.
        elif _team_only and live_rates_url_template(hotel_code):
            if _live_dates is None:
                from datetime import datetime as _dt_live, timedelta as _td_live
                from zoneinfo import ZoneInfo as _Zone_live

                _today_live = _dt_live.now(_Zone_live("Asia/Kolkata")).date()
                _live_dates = (_today_live.isoformat(), (_today_live + _td_live(days=1)).isoformat())
            _live_url = live_rates_url(hotel_code, *_live_dates)
            if _live_url:
                response.metadata["booking_cta"] = {"url": _live_url, "label": "Book on website"}
        # Alongside the website link, offer help from the team. Set here (rather
        # than on its own trigger) so it inherits every suppression rule the
        # booking button already has — multi-property, handoff/decline turns and
        # the invalid-date guard all return before this point.
        # The property name rides along so the submitted details can be matched
        # to the right room-booking ticket.
        _assist_property = ""
        if _marker_names:
            _assist_property = _marker_names[0]
        elif room_property_names:
            _assist_property = str(room_property_names[0] or "").strip()
        if _admin is not None:
            if _admin_form is not None:
                response.metadata["assist_cta"] = {
                    "label": str(_admin_form.get("label") or "Get help from our team"),
                    "property_name": _assist_property,
                    # Team-only booking: the one form takes the booking, so it is
                    # spent once submitted - as for the legacy team-only hotels.
                    **({"once_per_conversation": True} if _team_only else {}),
                }
        else:
            response.metadata["assist_cta"] = {
                "label": "Share your booking details" if _team_only else "Get help from our team",
                "property_name": _assist_property,
                # Team-only hotels take the booking through this one form, so once
                # the guest has submitted it the button must not reappear for the
                # rest of the conversation. Absent for every other tenant, whose
                # button keeps its per-message behaviour exactly as before.
                **({"once_per_conversation": True} if _team_only else {}),
            }


# Words that exist inside this system and must never be read by a guest. A guest
# asking about laundry was told what "the KB" did and did not specify - our
# filing cabinet is not their concern and naming it tells them the bot is reading
# off something rather than answering. The answerer prompt already forbids this
# ("never echo how the knowledge base stores a fact") and it still came out, so
# it is redacted here instead of asked for again.
#
# Deliberately TINY. This is not a content filter and must never grow into one:
# it only renames our own vocabulary, it changes no fact and no decision, and
# every substitution is logged so a leak stays visible rather than being quietly
# papered over.
# Each pattern swallows any article in front of the term, because replacing the
# noun alone leaves "The our information does not specify" - worse than the leak
# it fixed. "KB" additionally refuses to match after a digit so a file size is
# never mangled.
_INTERNAL_VOCAB = (
    (re.compile(r"\b(?:the|our|my|its|this)\s+knowledge[ _-]?base\b", re.I),
     "our information"),
    (re.compile(r"\bknowledge[ _-]?base\b", re.I), "our information"),
    # The article is case-insensitive but KB itself is not, so "the KB" is caught
    # at the start of a sentence without "kb" in an ordinary word being touched.
    (re.compile(r"\b(?i:the|our|my|its|this)\s+KB\b"), "our information"),
    (re.compile(r"(?<![\d\s]\s)(?<!\d)\bKB\b"), "our information"),
    (re.compile(r"\b(?:the\s+)?(?:pending_data|book_answerer|"
                r"service_prompt_pack|extracted_knowledge)\b", re.I),
     "our records"),
)


def _redact_internal_vocabulary(text: str) -> str:
    """Rename our own plumbing out of a guest-visible reply.

    Keeps the sentence readable: the replacement takes the capitalisation of what
    it replaced, so a term that opened a sentence still opens one.
    """
    out = str(text or "")
    for pattern, replacement in _INTERNAL_VOCAB:
        def _swap(match: "re.Match[str]") -> str:
            hit = match.group(0)
            return (replacement[:1].upper() + replacement[1:]
                    if hit[:1].isupper() else replacement)

        new = pattern.sub(_swap, out)
        if new != out:
            logger.warning("[LEAK] internal vocabulary reached the guest and was "
                           "redacted: %r", pattern.pattern)
            out = new
    return out


async def _attach_display_message(response: ChatResponse) -> None:
    """Pass the canonical answer through as the display message.

    Beautification is now the answerer LLM's responsibility — the prompt
    enforces the bullets + bold + italic format directly. No post-processing
    LLM call, no deterministic markdown rewriter. What the answerer emits is
    what the guest sees.
    """
    if not isinstance(response.metadata, dict):
        response.metadata = {}
    canonical = _redact_internal_vocabulary(str(response.message or "").strip())
    response.display_message = canonical
    response.metadata["display_message"] = canonical
    response.metadata["canonical_message"] = canonical


def _is_form_visual_turn(response: ChatResponse) -> bool:
    metadata = response.metadata if isinstance(response.metadata, dict) else {}
    if bool(metadata.get("form_trigger")):
        return True
    form_fields = metadata.get("form_fields")
    if isinstance(form_fields, list) and bool(form_fields):
        return True
    if str(metadata.get("form_service_id") or "").strip():
        return True
    return False


def _is_text_confirmation_turn(response: ChatResponse) -> bool:
    state_value = str(getattr(response.state, "value", response.state) or "").strip().lower()
    if state_value == "awaiting_confirmation":
        return True

    metadata = response.metadata if isinstance(response.metadata, dict) else {}
    entities = metadata.get("entities") if isinstance(metadata.get("entities"), dict) else {}

    pending_candidates = (
        metadata.get("pending_action"),
        metadata.get("ticket_pending_action"),
        entities.get("pending_action"),
    )
    for candidate in pending_candidates:
        pending = str(candidate or "").strip().lower()
        if pending.startswith("confirm_"):
            return True

    confirmation_phrase = re.sub(
        r"\s+",
        " ",
        str(getattr(settings, "chat_confirmation_phrase", "yes confirm") or "").strip().lower(),
    )
    if not confirmation_phrase:
        confirmation_phrase = "yes confirm"

    suggested_actions = response.suggested_actions if isinstance(response.suggested_actions, list) else []
    normalized_actions = [
        re.sub(r"\s+", " ", str(item or "").strip().lower())
        for item in suggested_actions
        if str(item or "").strip()
    ]
    if normalized_actions:
        if confirmation_phrase in normalized_actions:
            return True
        if "cancel" in normalized_actions and any(action.startswith("confirm") for action in normalized_actions):
            return True

    text_candidates = [
        str(response.display_message or "").strip().lower(),
        str(response.message or "").strip().lower(),
        str(metadata.get("display_message") or "").strip().lower(),
    ]
    for text in text_candidates:
        if not text:
            continue
        if confirmation_phrase in re.sub(r"\s+", " ", text):
            return True
        if re.search(r"\b(?:type|reply|respond|say)\b.{0,35}\bconfirm\b", text):
            return True
        if re.search(r"\b(?:please\s+)?confirm\b.{0,40}\?", text):
            return True

    return False


def _build_turn_display_payload(response: ChatResponse) -> dict:
    """Collect the rich display this turn rendered so chat history can replay
    exactly what the guest saw: beautified text, form schema, menu documents and
    image cards.

    Images used to be excluded here deliberately. That made a guest who picked a
    city, read the property intro with its photo and then reloaded see the same
    answer with the picture missing — the history did not match what they had
    just been shown. They are persisted now.

    Returns an empty dict when there's nothing beyond plain text worth storing."""
    meta = response.metadata if isinstance(response.metadata, dict) else {}
    payload: dict = {}
    display = str(getattr(response, "display_message", "") or "").strip()
    canonical = str(getattr(response, "message", "") or "").strip()
    if display and display != canonical:
        payload["display_message"] = display
    form_fields = meta.get("form_fields")
    if isinstance(form_fields, list) and form_fields:
        payload["form_fields"] = form_fields
        # Same fallback chain the live turn uses to decide where a submit goes.
        # Without it a restored form could come back with no service id and
        # silently do nothing when the guest pressed Submit.
        fsid = str(
            meta.get("form_service_id")
            or meta.get("pending_service_id")
            or meta.get("orchestration_target_service_id")
            or ""
        ).strip()
        if fsid:
            payload["form_service_id"] = fsid
        # The form was OPEN on this turn, and whether it carried the "Cancel
        # request" button. Without these a reloaded turn had the fields but no
        # way to know it should re-open the form the guest was mid-way through.
        if meta.get("form_trigger"):
            payload["form_trigger"] = True
        if meta.get("form_cancellable"):
            payload["form_cancellable"] = True
    # Unlike image cards, menu documents ARE persisted so a guest scrolling back
    # in chat history still sees the download card.
    documents = meta.get("documents")
    if isinstance(documents, list) and documents:
        payload["documents"] = documents
    # Image cards, so scrolling back shows the photo the answer came with.
    # Capped: a payload column is not a place to accumulate an unbounded gallery.
    images = meta.get("images")
    if isinstance(images, list) and images:
        payload["images"] = images[:12]
    # The "Book on website" button. Without this a replayed booking turn lost its
    # button AND showed the raw <<SHOW_BOOKING_BUTTON: ...>> marker as text: the
    # message is persisted before _apply_booking_cta_marker strips the marker and
    # attaches the CTA, so history kept the marker and had no button to render.
    cta = meta.get("booking_cta")
    if cta:
        payload["booking_cta"] = cta
    # "Get help from our team" — sits UNDER the website button on room-booking
    # turns. It was not persisted, so a reload brought the booking link back and
    # silently dropped the help button that had been beneath it.
    event_cta = meta.get("event_cta")
    if isinstance(event_cta, dict) and event_cta:
        payload["event_cta"] = event_cta
    assist = meta.get("assist_cta")
    if assist:
        payload["assist_cta"] = assist
    # Admin-configured link buttons of the service that answered.
    links = meta.get("service_links")
    if isinstance(links, list) and links:
        payload["service_links"] = links
    return payload


async def _attach_menu_document(
    *,
    response: ChatResponse,
    request: ChatRequest,
    hotel_code: str,
) -> None:
    """
    Attach the property's real menu PDF as `metadata.documents` when the guest asks
    a dining service for the menu file. The URL is the known hosted file, never one
    produced by the LLM. No-op when the hotel has no menu or the ask isn't a menu
    request. Mirrors _attach_smart_images; called from both HTTP and WS paths.
    """
    try:
        if not isinstance(response.metadata, dict):
            response.metadata = {}
        metadata = response.metadata
        # Has this conversation already been given the menu? Remembered on the
        # context so the unprompted copy goes out ONCE — after that the guest can
        # talk about ten dishes without the same PDF re-appearing under each reply.
        _session_id = str(getattr(request, "session_id", "") or "").strip()
        _ctx = None
        _already_sent = False
        if _session_id:
            try:
                _ctx = await context_manager.get_context(_session_id)
                if _ctx is not None and isinstance(getattr(_ctx, "pending_data", None), dict):
                    _already_sent = bool(_ctx.pending_data.get("_menu_doc_sent"))
            except Exception:
                _ctx = None  # never let this block the menu card

        # Which phase the guest is in — resolved the same way every other caller
        # in this module resolves it. `response.metadata["phase"]` is NOT it: that
        # key is never written, so reading it yields "" on every turn and any gate
        # built on it silently blocks everything.
        _ctx_pending = getattr(_ctx, "pending_data", None) if _ctx is not None else None
        # Admin-managed hotels: the menus uploaded in Bot Training -> Pre-booking,
        # when the guest asks for the menu or the main book the router chose for
        # the answer is ticked there as a menu topic (a chip answer served from
        # the prefetch cache keeps the router's books from when it was cooked).
        _router_books = metadata.get("router_book_ids")
        _menus_sent = (
            [str(m) for m in (_ctx_pending.get("_menu_docs_sent") or [])]
            if isinstance(_ctx_pending, dict) else []
        )
        _admin_docs = menu_document_service.admin_menu_documents(
            hotel_code=hotel_code,
            user_text=str(getattr(request, "message", "") or ""),
            active_property_ids=(
                list(_ctx_pending.get("_active_property_ids") or [])
                if isinstance(_ctx_pending, dict) else []
            ),
            router_book_ids=list(_router_books or []) if isinstance(_router_books, list) else [],
            already_sent=_menus_sent,
        )
        if _admin_docs is not None:
            if _admin_docs:
                docs = metadata.get("documents")
                docs = docs if isinstance(docs, list) else []
                for _doc in _admin_docs:
                    _doc = dict(_doc)
                    _menu_id = str(_doc.pop("menu_id", "") or "")
                    if _menu_id and _menu_id not in _menus_sent:
                        _menus_sent.append(_menu_id)
                    if not any(isinstance(d, dict) and d.get("url") == _doc["url"] for d in docs):
                        docs.append(_doc)
                metadata["documents"] = docs
                logger.info("[MENU_DOC] %d admin menu(s) sent for hotel=%s", len(_admin_docs), hotel_code)
                # Remembered for this conversation, so the next dining answer
                # does not repeat a menu that is already in the chat.
                if _ctx is not None and isinstance(_ctx_pending, dict):
                    try:
                        _ctx_pending["_menu_docs_sent"] = _menus_sent
                        await context_manager.save_context(_ctx)
                    except Exception as _mark_exc:
                        logger.warning("[MENU_DOC] could not record menus sent: %r", _mark_exc)
            return
        phase_id = _normalize_phase_identifier(
            (getattr(request, "metadata", None) or {}).get("phase")
            or metadata.get("selected_phase_id")
            or (_ctx_pending.get("_selected_phase_id") if isinstance(_ctx_pending, dict) else "")
            or ""
        )

        doc = menu_document_service.build_menu_document(
            hotel_code=hotel_code,
            # Which service the router chose. `service_agent_id` is not set on every
            # path, but the label the router emits always is ("In-Room Dining"), and
            # it normalises to the same identifier — so read whichever is present
            # rather than concluding no service was picked.
            service_agent_id=str(
                metadata.get("service_agent_id")
                or metadata.get("service_llm_label")
                or ""
            ),
            phase_id=phase_id,
            user_text=str(getattr(request, "message", "") or ""),
            already_sent=_already_sent,
        )
        if not doc:
            return
        docs = metadata.get("documents")
        if not isinstance(docs, list):
            docs = []
        if not any(isinstance(d, dict) and d.get("url") == doc.get("url") for d in docs):
            docs.append(dict(doc))
        metadata["documents"] = docs
        if _ctx is not None and not _already_sent:
            try:
                if isinstance(getattr(_ctx, "pending_data", None), dict):
                    _ctx.pending_data["_menu_doc_sent"] = True
                    await context_manager.save_context(_ctx)
                    logger.info(
                        "[MENU_DOC] menu sent for session=%s — it will not be "
                        "attached again unless the guest asks for it", _session_id,
                    )
            except Exception as _mark_exc:
                logger.warning("[MENU_DOC] could not record menu-sent: %r", _mark_exc)
    except Exception as exc:  # non-fatal: a missing menu card must never break the turn
        logger.warning("[MENU_DOC] attach failed (non-fatal): %r", exc)


async def _attach_topic_images(
    *,
    response: ChatResponse,
    request: ChatRequest,
    hotel_code: str,
) -> bool:
    """Attach images for the KB books the router opened on this turn.

    Runs INLINE, before the reply is sent. That is safe because it makes no LLM
    call — it reads an in-process TTL-cached candidate list and orders it — so
    it costs about a millisecond. The LLM/title-matching selector stays on the
    deferred background path where it belongs.

    Why it exists: with `chat_defer_display_enrichment` on, `_attach_smart_images`
    is scheduled as a background task AFTER the frame has already been sent, so
    anything it attached could never reach the guest on that turn. Mid-chat
    replies about rooms/dining/spa therefore showed no pictures at all.

    Returns True when images were attached.
    """
    if not bool(getattr(settings, "chat_smart_images_enabled", False)):
        return False
    if not isinstance(response.metadata, dict):
        response.metadata = {}
    metadata = response.metadata
    if metadata.get("images"):
        return False
    if _is_form_visual_turn(response) or _is_text_confirmation_turn(response):
        return False
    # This hotel has no photo of what this service talks about, so anything the
    # matcher finds is the wrong thing. Opt-in per hotel+service; empty for
    # everyone else.
    if _images_muted_for_turn(hotel_code, response):
        metadata["smart_image_selection_reason"] = "muted_for_service"
        metadata["smart_image_tool_used"] = False
        return False

    session_key = str(request.session_id or "").strip()
    # consume: these labels belong to THIS turn's book-answerer reply. Leaving
    # them behind meant the next service-agent turn reused them.
    book_labels = image_tool_service.books_for(session_key, consume=True)
    metadata["smart_image_books_seen"] = book_labels

    scope_ids: list[int] = []
    for hint in (metadata.get("active_property_ids") or []):
        if not str(hint).strip():
            continue
        try:
            resolved = await image_tool_service.resolve_active_property_id(
                hotel_code=hotel_code, property_hint=str(hint)
            )
        except Exception:
            resolved = None
        if resolved is not None and resolved not in scope_ids:
            scope_ids.append(resolved)
    if not scope_ids:
        return False

    # A REPLY THAT COVERS TWO HOTELS needs a picture of each, and a way to pick
    # one. Title matching gives neither. Asked for a family-friendly hotel in
    # Goa the reply described both properties, wrote "pool" five times, and got
    # back two POOL photos of a single hotel — the guest saw one hotel's water
    # twice and had to type a name to go further.
    #
    # The properties the reply actually NAMES are the ones it covers, so they
    # drive both: one exterior each, and one button each. Names come from the
    # gallery rows the image path already loads (in-process TTL cache), so this
    # costs no extra database work on the turn.
    reply_text = str(getattr(response, "message", "") or "")

    def _flat(s: Any) -> str:
        # punctuation-insensitive: the KB name carries a comma the reply may
        # not ("...The Park, Parra Goa" vs "...The Park Parra Goa")
        return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()

    named_ids: list[int] = []
    id_to_name: dict[int, str] = {}
    try:
        _cands = await image_tool_service.list_candidate_assets(
            hotel_code=hotel_code, limit=0, active_property_ids=scope_ids
        )
        for _row in _cands or []:
            _pid, _nm = _row.get("property_id"), str(_row.get("property_name") or "").strip()
            if _pid is not None and _nm:
                id_to_name.setdefault(int(_pid), _nm)
        _body = _flat(reply_text)
        _hits = [(_body.find(_flat(nm)), pid) for pid, nm in id_to_name.items()
                 if _flat(nm) and _flat(nm) in _body]
        # first-mentioned first, so the buttons read in the order just described
        named_ids = [pid for _pos, pid in sorted(_hits)]
    except Exception as _multi_exc:
        logger.warning("multi-property image/choice scan skipped (non-fatal): %r", _multi_exc)

    if len(named_ids) >= 2:
        # Buttons regardless of whether photos come back — being able to choose
        # is the point, and a property with no categorised gallery must not
        # cost the guest the shortcut.
        metadata["property_choices"] = [id_to_name[p] for p in named_ids]
        # ONE picture per hotel, but OF WHAT THE REPLY IS ABOUT. This used to be
        # a flat facade quota, so "do both Goa properties have pools?" answered
        # with two photos of two buildings even though all six pool photos were
        # sitting right there. The exterior is only the right picture when the
        # turn has no visual subject of its own, so it becomes the fallback
        # rather than the rule — and each property is matched separately, which
        # keeps the one-each guarantee that made this branch necessary.
        images = []
        try:
            _amenity: dict[int, dict[str, Any]] = {}
            for _pid in named_ids:
                _picked = await image_tool_service.select_by_title_mentions(
                    hotel_code=hotel_code,
                    property_ids=[_pid],
                    text=reply_text,
                    limit=3,
                )
                # Only a shared AMENITY may displace the exterior in this slot.
                # It stands for the hotel itself, so it has to be something that
                # tells the two apart: "do both have pools?" is answered by a
                # pool each, but a hotels-in-Kolkata listing mentioning "Zone
                # Deluxe" is not answered by two bedrooms — the guest is picking
                # between buildings. Rooms, lobbies and the `other` pile (where
                # landmarks live — "1.4 km from Calangute Beach" matched the
                # photo titled "Beach") all fall through to the facade below.
                _hit = [
                    _i for _i in (_picked or [])
                    if str(_i.get("category") or "").strip().lower()
                    in ("pool", "spa", "dining", "fitness", "banquet")
                ]
                if _hit:
                    _amenity[_pid] = _hit[0]

            # …and only when EVERY hotel offers the SAME one, which is what
            # makes the turn a comparison. "Do both have pools?" gives a pool
            # each. "What hotels do you have in Kolkata?" happened to mention
            # all-day dining, matching a restaurant for one hotel and nothing
            # for the other — a restaurant beside a building answers nothing,
            # so a partial or mixed match falls back to exteriors for all.
            _shared = (
                len(_amenity) == len(named_ids)
                and len({str(v.get("category") or "").lower() for v in _amenity.values()}) == 1
            )
            for _pid in named_ids:
                if _shared:
                    images.append(_amenity[_pid])
                    continue
                images.extend(await image_tool_service.select_gallery_for_properties(
                    hotel_code=hotel_code,
                    property_ids=[_pid],
                    per_property_limit=1,
                    category_quota=(("facade", 1),),
                ) or [])
        except Exception as exc:
            metadata["smart_image_topic_error"] = f"{type(exc).__name__}: {exc}"
            images = []
        if images:
            image_tool_service.note_shown(session_key, [int(i.get("id") or 0) for i in images])
            metadata["images"] = images
            metadata["smart_image_tool_used"] = True
            metadata["smart_image_selection_reason"] = "multi_property_one_each"
            metadata["smart_image_books"] = book_labels
            return True
        return False

    # PICKING A HOTEL gets the same guaranteed set as the city welcome: one
    # exterior plus three rooms. It is the second half of the same flow, and
    # leaving it to title matching made it a coin flip — the Goa pick reply
    # happened to write "Junior Suite" so four photos appeared, while the
    # Kolkata one described its rooms differently and showed none.
    #
    # A message that resolves to a property name IS the choice-button click:
    # that is exactly what the button sends. Guarded so an ordinary question
    # cannot qualify.
    _picked_id = None
    _msg = str(getattr(request, "message", "") or "").strip()
    if 3 < len(_msg) <= 70 and "?" not in _msg:
        try:
            _picked_id = await image_tool_service.resolve_active_property_id(
                hotel_code=hotel_code, property_hint=_msg
            )
        except Exception:
            _picked_id = None
    if _picked_id is not None:
        try:
            images = await image_tool_service.select_gallery_for_properties(
                hotel_code=hotel_code,
                property_ids=[_picked_id],
                per_property_limit=0,
                category_quota=(("facade", 0), ("room", 0)),
            )
        except Exception as exc:
            metadata["smart_image_topic_error"] = f"{type(exc).__name__}: {exc}"
            return False
        if images:
            image_tool_service.note_shown(session_key, [int(i.get("id") or 0) for i in images])
            metadata["images"] = images
            metadata["smart_image_tool_used"] = True
            metadata["smart_image_selection_reason"] = "property_pick_default_set"
            metadata["smart_image_books"] = book_labels
            return True

    # Mid-chat images come from ONE rule: does the reply actually name a photo
    # we hold? The gallery titles are the real thing-names ("Junior Suite",
    # "Cafe C", "Flurys"), so a reply that mentions one gets that exact picture.
    #
    # The book→category map used to run here as well, and it padded answers with
    # a photo of the building whenever the router had opened Location &
    # Transport. Replayed over 41 scored turns, dropping it loses pictures on 8
    # — six of which were a facade attached to a distance question, and one a
    # LOBBY photo answering "does it have a pool?". Noise, not information.
    #
    # The curated set still runs for the welcome message, where showing the
    # place IS the point: see _build_location_intro_response.
    try:
        images = await image_tool_service.select_by_title_mentions(
            hotel_code=hotel_code,
            property_ids=scope_ids,
            text=str(getattr(response, "message", "") or ""),
            limit=0,          # every photo the reply actually names
            exclude_ids=image_tool_service.shown_for(session_key),
        )
        reason = "reply_title_match"
    except Exception as exc:
        metadata["smart_image_topic_error"] = f"{type(exc).__name__}: {exc}"
        return False
    if not images:
        return False

    image_tool_service.note_shown(session_key, [int(i.get("id") or 0) for i in images])
    metadata["images"] = images
    metadata["smart_image_tool_used"] = True
    metadata["smart_image_selection_reason"] = reason
    metadata["smart_image_books"] = book_labels
    return True


async def _attach_smart_images(
    *,
    response: ChatResponse,
    request: ChatRequest,
    hotel_code: str,
    trace_id: str = "",
    turn_trace_id: str = "",
) -> None:
    """
    Attach LLM-selected image cards when smart image tool is enabled for the hotel.
    If the tool is disabled, this function does nothing and never calls the LLM.
    """
    if not isinstance(response.metadata, dict):
        response.metadata = {}
    metadata = response.metadata
    if not bool(getattr(settings, "chat_smart_images_enabled", False)):
        metadata["smart_image_selection_reason"] = "disabled_by_setting"
        metadata["smart_image_tool_used"] = False
        return

    if _is_form_visual_turn(response):
        metadata["smart_image_selection_reason"] = "skipped_form_turn"
        metadata["smart_image_tool_used"] = False
        return

    if _is_text_confirmation_turn(response):
        metadata["smart_image_selection_reason"] = "skipped_confirmation_turn"
        metadata["smart_image_tool_used"] = False
        return

    if _images_muted_for_turn(hotel_code, response):
        metadata["smart_image_selection_reason"] = "muted_for_service"
        metadata["smart_image_tool_used"] = False
        return

    # Previously a hardcoded state-guard skipped image attach whenever the
    # turn ended in `awaiting_info` / `awaiting_confirmation` / `processing_order`.
    # That blanket-skipped images for every service_agent informational turn
    # (room listings, spa descriptions, dining info) — not just form prompts.
    # The form-turn / confirmation-turn checks above already cover the cases
    # where attaching images would be visually wrong; outside those, the LLM
    # selector returns an empty list when no image is relevant, so trusting
    # it here gives images on service_agent answers (e.g. "what rooms are
    # available") without putting images on form panels.

    try:
        tool_enabled = await image_tool_service.is_enabled_for_hotel(hotel_code)
    except Exception as exc:
        # Fail-open for image lookup: if tool-config lookup fails transiently,
        # still attempt title-grounded selection from gallery assets.
        metadata["smart_image_tool_error"] = f"tool_lookup_failed: {exc}"
        tool_enabled = True

    if not tool_enabled:
        return

    intent_value = str(metadata.get("classified_intent") or "").strip()
    user_text = str(request.message or "").strip()
    assistant_text = str(response.display_message or response.message or "").strip()
    if not assistant_text:
        return

    # Identify the active sub-property scope (from the orchestrator's view) so
    # the image selector can pull the right subset. Falls through to None when
    # the hotel is single-property or the orchestrator hasn't locked a scope —
    # the selector then returns all images, matching legacy behavior.
    active_property_hint = ""
    active_property_hint_source = ""
    selected_ids = metadata.get("selected_property_ids")
    if isinstance(selected_ids, list) and selected_ids:
        active_property_hint = str(selected_ids[0] or "").strip()
        active_property_hint_source = "selected_property_ids"
    if not active_property_hint:
        active_property_hint = str(metadata.get("selected_property_name") or "").strip()
        if active_property_hint:
            active_property_hint_source = "selected_property_name"

    # Durable-FOCUS scope: the main (non-cached) orchestration path does NOT
    # echo `selected_property_*` into metadata — it persists the locked scope
    # as `active_property_ids` (property slugs). Trust that authoritative scope
    # BEFORE scraping the reply text. Reply-text scanning false-matches plain
    # geographic mentions (e.g. "gateway to Jammu & Kashmir" pulling the Jammu
    # property into scope and leaking a Jammu image into a Pathankot reply).
    _focus_scope_ids: list[int] = []
    if not active_property_hint:
        _focus_hints = [
            str(x).strip()
            for x in (metadata.get("active_property_ids") or [])
            if str(x).strip()
        ]
        if len(_focus_hints) == 1:
            active_property_hint = _focus_hints[0]
            active_property_hint_source = "active_property_focus"
        elif len(_focus_hints) >= 2:
            for _h in _focus_hints:
                try:
                    _rid = await image_tool_service.resolve_active_property_id(
                        hotel_code=hotel_code,
                        property_hint=_h,
                    )
                except Exception:
                    _rid = None
                if _rid is not None and _rid not in _focus_scope_ids:
                    _focus_scope_ids.append(_rid)
            if _focus_scope_ids:
                active_property_hint_source = "active_property_focus_multi"

    # Cold-start fallback: when the orchestrator hasn't locked a scope yet
    # (first turn, FOCUS line not yet persisted), scan the bot's reply text
    # for a property name. Avoids the cold-start leak where the first turn
    # surfaces an image of an unrelated property. Gated by a setting so
    # behavior can be reverted instantly.
    _assistant_text_matched_property_ids: list[int] = []
    if (
        not active_property_hint
        and not _focus_scope_ids
        and bool(getattr(settings, "chat_smart_images_text_scope_fallback", True))
        and assistant_text
    ):
        try:
            from models.database import AsyncSessionLocal as _ImgScopeSession, Property as _ImgScopeProperty
            from services.db_config_service import db_config_service as _img_db_cfg
            with _img_db_cfg.scoped_hotel(hotel_code):
                _hotel_id = await _img_db_cfg.get_current_hotel_id()
            if _hotel_id:
                async with _ImgScopeSession() as _sess:
                    _rows = (
                        await _sess.execute(
                            select(_ImgScopeProperty).where(
                                _ImgScopeProperty.hotel_id == _hotel_id,
                                _ImgScopeProperty.is_active == True,  # noqa: E712
                            )
                        )
                    ).scalars().all()
                lowered = assistant_text.lower()
                # Build (label, property) candidates from name + aliases, longest first.
                # Longest match wins so "Lotus Eco Beach Resort Konark" beats "Konark".
                _label_to_prop: list[tuple[str, Any]] = []
                for _r in _rows:
                    _labels = [str(_r.name or "").strip()]
                    if isinstance(_r.aliases, list):
                        _labels.extend(str(a or "").strip() for a in _r.aliases)
                    for _lbl in _labels:
                        if _lbl and len(_lbl) >= 4:
                            _label_to_prop.append((_lbl.lower(), _r))
                _label_to_prop.sort(key=lambda t: len(t[0]), reverse=True)
                # Collect ALL distinct properties whose name/aliases appear in
                # the assistant text. For single-property turns this becomes
                # the scope hint as before; for multi-property comparison
                # turns we keep the full set so the image selector can scope
                # to ONLY the mentioned properties (instead of falling back
                # to every image in the hotel, which leaks unrelated property
                # images into the carousel — e.g. a Porvorim pool image
                # showing up while the bot is summarizing Pune properties).
                _matched_ids: set[int] = set()
                _first_match: Any = None
                for _lbl, _prop in _label_to_prop:
                    if _lbl in lowered:
                        _matched_ids.add(int(_prop.id))
                        if _first_match is None:
                            _first_match = _prop
                if len(_matched_ids) == 1 and _first_match is not None:
                    active_property_hint = str(_first_match.name or "").strip()
                    active_property_hint_source = "assistant_text_fallback"
                elif len(_matched_ids) >= 2:
                    # Multi-property turn — no single hint, but stash the set
                    # so downstream can pass it as active_property_ids.
                    active_property_hint_source = "assistant_text_fallback_multi"
                _assistant_text_matched_property_ids = list(_matched_ids)
        except Exception as _scan_exc:
            metadata["smart_image_text_scope_fallback_error"] = f"{type(_scan_exc).__name__}: {_scan_exc}"

    active_property_id: int | None = None
    if active_property_hint:
        try:
            active_property_id = await image_tool_service.resolve_active_property_id(
                hotel_code=hotel_code,
                property_hint=active_property_hint,
            )
        except Exception as exc:
            metadata["smart_image_property_resolve_error"] = f"{type(exc).__name__}: {exc}"
            active_property_id = None
    metadata["smart_image_active_property_id"] = active_property_id
    metadata["smart_image_active_property_hint"] = active_property_hint
    metadata["smart_image_active_property_hint_source"] = active_property_hint_source

    # Keep a bounded budget so image selection never stalls the reply.
    _, _smart_image_hard_cap_ms = _resolve_smart_image_timeouts_ms()
    _llm_image_budget_seconds = max(
        0.4,
        float(_smart_image_hard_cap_ms) / 1000.0 - 0.2,
    )
    # When the reply summarizes multiple properties (no single scope hint),
    # pass the full set of mentioned property ids so the image selector
    # restricts candidates to only those properties. Without this the picker
    # falls back to every image in the hotel and unrelated images leak in
    # (e.g. a Porvorim pool image surfacing under a Pune-properties reply).
    _multi_scope_ids: list[int] | None = None
    if _focus_scope_ids:
        # Authoritative multi-property FOCUS (orchestrator-locked) wins over the
        # reply-text scan.
        _multi_scope_ids = list(_focus_scope_ids)
    elif active_property_id is None and len(_assistant_text_matched_property_ids) >= 2:
        _multi_scope_ids = list(_assistant_text_matched_property_ids)
    metadata["smart_image_active_property_ids"] = _multi_scope_ids

    # Safety net: if the conversation has a KNOWN active property scope but we
    # could not resolve it to any concrete property id, do NOT fall back to the
    # whole-hotel image pool — that is exactly the cross-property leak path.
    # Better to attach no image than another property's image.
    _has_known_active_scope = bool(active_property_hint) or bool(
        [x for x in (metadata.get("active_property_ids") or []) if str(x).strip()]
    )
    if active_property_id is None and not _multi_scope_ids and _has_known_active_scope:
        metadata["smart_image_selection_reason"] = "property_scope_unresolved_skip"
        metadata["smart_image_tool_used"] = False
        return

    selection: dict[str, Any] | None = None
    try:
        selection = await asyncio.wait_for(
            image_tool_service.llm_select_images(
                hotel_code=hotel_code,
                user_message=user_text,
                assistant_message=assistant_text,
                intent=intent_value,
                max_images=0,
                trace_context={
                    "trace_id": trace_id,
                    "turn_trace_id": turn_trace_id,
                    "session_id": str(request.session_id or "").strip(),
                    "route": "/api/chat/message",
                },
                active_property_id=active_property_id,
                active_property_ids=_multi_scope_ids,
            ),
            timeout=_llm_image_budget_seconds,
        )
    except asyncio.TimeoutError:
        metadata["smart_image_selection_reason"] = "llm_timeout"
        metadata["smart_image_tool_used"] = False
        return
    except Exception as exc:
        metadata["smart_image_selection_reason"] = f"llm_error: {type(exc).__name__}"
        metadata["smart_image_tool_used"] = False
        return
    if not isinstance(selection, dict):
        metadata["smart_image_tool_used"] = False
        return

    images = selection.get("images") if isinstance(selection, dict) else []
    if isinstance(images, list) and images:
        metadata["images"] = images
    metadata["smart_image_tool_used"] = bool(selection.get("tool_used")) if isinstance(selection, dict) else False
    metadata["smart_image_selection_reason"] = (
        str(selection.get("selection_reason") or "").strip()
        if isinstance(selection, dict)
        else ""
    )
    if isinstance(selection, dict) and selection.get("candidate_count") is not None:
        metadata["smart_image_candidate_count"] = int(selection.get("candidate_count") or 0)


def _resolve_smart_image_timeouts_ms() -> tuple[int, int]:
    configured_timeout_ms = max(
        50,
        int(getattr(settings, "chat_smart_images_attach_timeout_ms", 250) or 250),
    )
    hard_cap_ms = max(
        50,
        int(getattr(settings, "chat_smart_images_attach_hard_cap_ms", 1200) or 1200),
    )
    if bool(getattr(settings, "chat_smart_images_fast_title_match_only", True)):
        configured_timeout_ms = max(configured_timeout_ms, 4500)
        hard_cap_ms = max(hard_cap_ms, 5000, configured_timeout_ms)
    else:
        hard_cap_ms = max(hard_cap_ms, configured_timeout_ms)
    return configured_timeout_ms, hard_cap_ms


async def _refresh_summary_for_session(
    *,
    session_id: str,
    hotel_code: str = "",
) -> None:
    session_key = str(session_id or "").strip()
    if not session_key:
        return

    scope_token = db_config_service.set_hotel_context(hotel_code) if str(hotel_code or "").strip() else None
    use_db_store = bool(getattr(settings, "chat_db_first_mode", False))
    try:
        if use_db_store:
            async with AsyncSessionLocal() as summary_db:
                context = await context_manager.get_context(session_key, db_session=summary_db)
                if context is None:
                    return
                if not isinstance(context.pending_data, dict):
                    context.pending_data = {}
                context.pending_data.pop("_defer_summary_refresh", None)
                await conversation_memory_service.maybe_refresh_summary(context)
                await context_manager.save_context(context, db_session=summary_db)
        else:
            context = await context_manager.get_context(session_key, db_session=None)
            if context is None:
                return
            if not isinstance(context.pending_data, dict):
                context.pending_data = {}
            context.pending_data.pop("_defer_summary_refresh", None)
            await conversation_memory_service.maybe_refresh_summary(context)
            await context_manager.save_context(context, db_session=None)
    except Exception:
        logger.exception("background_summary_refresh_failed session_id=%s", session_key)
    finally:
        if scope_token:
            db_config_service.reset_hotel_context(scope_token)


def _queue_summary_refresh(*, session_id: str, hotel_code: str = "") -> None:
    try:
        asyncio.create_task(
            _refresh_summary_for_session(
                session_id=session_id,
                hotel_code=hotel_code,
            )
        )
    except Exception:
        logger.exception("background_summary_refresh_schedule_failed session_id=%s", str(session_id or "").strip())


_FORM_INVITE_RE = re.compile(
    r"\.?\s*(?:so\s+)?please\s+(?:fill\s+in|complete|review|provide)\s+"
    r"(?:the\s+)?(?:details|form|booking\s+details|information)\s*"
    r"(?:below|form)?[^.]*\.?\s*",
    re.IGNORECASE,
)


def _strip_form_invite(response: Any) -> None:
    """Delete "please fill in the details below" from a reply that has no form.

    Used on the paths where the form builder bails out. The invitation is
    normally removed and re-added by the builder itself, so when the builder
    returns early the sentence is left pointing at a form that will never
    render.
    """

    def _clean(text: Any) -> str:
        out = _FORM_INVITE_RE.sub(" ", str(text or "")).strip()
        out = re.sub(r"\s{2,}", " ", out)
        return re.sub(r"\s+([.,!?])", r"\1", out).strip(" .,;:") + "." if out else ""

    try:
        if getattr(response, "message", None):
            response.message = _clean(response.message)
        if getattr(response, "display_message", None):
            response.display_message = _clean(response.display_message)
        meta = getattr(response, "metadata", None)
        if isinstance(meta, dict) and meta.get("display_message"):
            meta["display_message"] = _clean(meta["display_message"])
    except Exception:
        pass


def _looks_like_generic_form_open_reply(text: str) -> bool:
    """True when the bot reply is the content-free support/booking form-open line
    (e.g. "Please review the details below and submit…"). Such replies name no
    topic, so they are a poor anchor for the Issue Details prefill."""
    t = re.sub(r"\s+", " ", str(text or "").strip().lower())
    if not t:
        return False
    markers = (
        "review the details below",
        "fill in the details below",
        "details below and submit",
        "fill in the booking details below",
        "few quick details",
    )
    return any(m in t for m in markers)


_BOOKING_LEAD_CONTACT_ALIASES: dict[str, tuple[str, ...]] = {
    "guest_name": ("guest_name", "guest_full_name", "full_name", "name"),
    "guest_phone": (
        "guest_phone",
        "phone",
        "phone_number",
        "contact_number",
        "contact_phone",
        "mobile",
        "mobile_number",
    ),
    "guest_email": ("guest_email", "email", "email_address"),
}

# Location is the dropdown value the guest picks in the form — NOT the selected
# property name (that stays conversational). Kept separate so new_bot_sessions
# stores the chosen location without ever confusing it with property scope.
_SESSION_LOCATION_ALIASES: tuple[str, ...] = (
    "location",
    "preferred_location",
    "city",
    "preferred_city",
    "area",
)


def _extract_session_profile_fields(
    *,
    merged_form_data: dict[str, Any],
    req_meta: dict[str, Any],
    context: Any,
) -> dict[str, str]:
    """Resolve the stable identity fields stored in new_bot_sessions.

    Looks in the submitted/merged form first, then request metadata, then
    conversation pending_data. Reuses the canonical contact aliases so it lines
    up with how the rest of the form pipeline reads name/phone/email.
    """
    form = merged_form_data if isinstance(merged_form_data, dict) else {}
    meta = req_meta if isinstance(req_meta, dict) else {}
    pending = (
        context.pending_data
        if context is not None and isinstance(getattr(context, "pending_data", None), dict)
        else {}
    )

    def _pick(aliases: tuple[str, ...]) -> str:
        for source in (form, meta, pending):
            for alias in aliases:
                value = str(source.get(alias) or "").strip()
                if value:
                    return value
        return ""

    name = _first_non_empty_text(
        _pick(_BOOKING_LEAD_CONTACT_ALIASES["guest_name"]),
        getattr(context, "guest_name", None),
    )
    phone = _normalize_phone_text(
        _first_non_empty_text(
            _pick(_BOOKING_LEAD_CONTACT_ALIASES["guest_phone"]),
            getattr(context, "guest_phone", None),
        )
    )
    email = _pick(_BOOKING_LEAD_CONTACT_ALIASES["guest_email"])
    location = _pick(_SESSION_LOCATION_ALIASES)
    return {
        "guest_name": name,
        "guest_phone": phone,
        "guest_email": email,
        "location": location,
    }


def _normalize_booking_lead_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def _strip_booking_lead_form_copy(text: str) -> str:
    cleaned = str(text or "").strip()
    if not cleaned:
        return ""
    patterns = (
        r"\.?\s*please\s+fill\s+in\s+the\s+(?:booking\s+)?details\s+below\.?",
        r"\.?\s*please\s+review\s+the\s+details\s+below\s+and\s+submit\.?",
        r"\.?\s*please\s+fill\s+in\s+the\s+form\s+below\.?",
        r"\.?\s*just\s+a\s+few\s+quick\s+details\.?",
    )
    for pattern in patterns:
        cleaned = re.sub(pattern, ".", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = re.sub(r"\s+([,.;!?])", r"\1", cleaned)
    cleaned = re.sub(r"([!?])\.", r"\1", cleaned)
    cleaned = re.sub(r"\.\s*\.", ".", cleaned).strip(" ,")
    return cleaned or str(text or "").strip()


def _booking_lead_required_contact_fields(service: dict[str, Any] | None) -> list[str]:
    form_config = service.get("form_config") if isinstance(service, dict) and isinstance(service.get("form_config"), dict) else {}
    fields = form_config.get("fields") if isinstance(form_config.get("fields"), list) else []
    required: list[str] = []
    recognized: list[str] = []
    for field in fields:
        if not isinstance(field, dict):
            continue
        candidates = (
            _normalize_booking_lead_key(field.get("id")),
            _normalize_booking_lead_key(field.get("label")),
        )
        canonical = ""
        for target, aliases in _BOOKING_LEAD_CONTACT_ALIASES.items():
            if any(candidate and candidate in aliases for candidate in candidates):
                canonical = target
                break
        if not canonical:
            continue
        if canonical not in recognized:
            recognized.append(canonical)
        if field.get("required") is not False and canonical not in required:
            required.append(canonical)
    if required:
        return required
    if recognized:
        return recognized
    return ["guest_name", "guest_phone", "guest_email"]


def _booking_lead_contact_value(
    *,
    request_metadata: dict[str, Any] | None,
    context: Any = None,
    canonical_field: str,
) -> str:
    aliases = _BOOKING_LEAD_CONTACT_ALIASES.get(canonical_field, ())
    pending_data = context.pending_data if context is not None and isinstance(getattr(context, "pending_data", None), dict) else {}
    for source in (
        request_metadata if isinstance(request_metadata, dict) else {},
        pending_data if isinstance(pending_data, dict) else {},
    ):
        for alias in aliases:
            value = str(source.get(alias) or "").strip()
            if value:
                return value
    return ""


def _booking_lead_has_required_contact_fields(
    *,
    request_metadata: dict[str, Any] | None,
    context: Any = None,
    service: dict[str, Any] | None,
) -> tuple[bool, list[str]]:
    missing: list[str] = []
    for canonical_field in _booking_lead_required_contact_fields(service):
        value = _booking_lead_contact_value(
            request_metadata=request_metadata,
            context=context,
            canonical_field=canonical_field,
        )
        if canonical_field == "guest_email":
            if not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", value):
                missing.append(canonical_field)
        elif canonical_field == "guest_phone":
            if len(re.sub(r"\D+", "", value)) < 7:
                missing.append(canonical_field)
        else:
            if len(value) < 2:
                missing.append(canonical_field)
    return (len(missing) == 0), missing


def _looks_like_room_type_selection_turn(user_text: str, context: Any = None) -> bool:
    pending_data = context.pending_data if context is not None and isinstance(getattr(context, "pending_data", None), dict) else {}
    candidates = pending_data.get("room_type_candidates") if isinstance(pending_data, dict) else []
    if not isinstance(candidates, list):
        return False
    if not user_text or "?" in user_text:
        return False
    for candidate in candidates:
        normalized_candidate = _normalize_suggestions_turn_text(candidate)
        if not normalized_candidate:
            continue
        if user_text in {
            normalized_candidate,
            f"the {normalized_candidate}",
            f"{normalized_candidate} please",
        }:
            return True
    return False


def _looks_like_booking_slot_value_turn(user_text: str, context: Any = None) -> bool:
    if not user_text:
        return False
    if re.search(r"[^\s@]+@[^\s@]+\.[^\s@]+", user_text):
        return True
    if re.search(r"\b\d{7,15}\b", re.sub(r"[^\d+]", " ", user_text)):
        return True
    if re.search(r"\b\d{4}-\d{1,2}-\d{1,2}\b", user_text):
        return True
    if re.search(r"\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b", user_text):
        return True
    if re.search(
        r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\b",
        user_text,
        flags=re.IGNORECASE,
    ):
        return True
    if any(
        marker in user_text
        for marker in (
            "check in",
            "check-in",
            "check out",
            "check-out",
            "checkout",
            "guest",
            "guests",
            "adult",
            "adults",
            "child",
            "children",
            "night",
            "nights",
            "full name",
            "contact number",
        )
    ):
        return True
    return _looks_like_room_type_selection_turn(user_text, context)


def _is_explicit_booking_lead_resume_turn(
    *,
    user_message: str,
    request_metadata: dict[str, Any] | None,
    context: Any = None,
) -> bool:
    meta = request_metadata if isinstance(request_metadata, dict) else {}
    source_type = _normalize_suggestions_turn_text(
        meta.get("ui_source_type") or meta.get("source_type")
    )
    if source_type in {"cta_book_stay", "cta_location_picker"}:
        return True
    user_text = _normalize_suggestions_turn_text(
        user_message or meta.get("ui_source_text") or ""
    )
    if not user_text:
        return False
    if user_text in {"yes", "yes confirm", "confirm", "ok", "okay", "sure", "proceed", "continue"}:
        return True
    if re.search(
        r"\b(?:i\s+(?:want|wanna|need|would\s+like)\s+to\s+book|book\s+(?:it|this|that|now|me|my)|"
        r"help\s+me\s+book|let'?s\s+book|reserve|reservation|go\s+ahead|proceed|continue|"
        r"complete\s+(?:the|my)\s+booking|confirm)\b",
        user_text,
        flags=re.IGNORECASE,
    ):
        return True
    return _looks_like_booking_slot_value_turn(user_text, context)


def _last_topic_assistant_message(context: Any, *, exclude_text: str = "") -> str:
    """Most recent ASSISTANT turn that actually named a topic — skipping the
    current/throwaway form-open line. Used to anchor the freeform prefill to the
    real subject the guest just discussed (e.g. the venue/event proposal)."""
    msgs = list(getattr(context, "messages", []) or [])
    # Scoped to the current thread: after a clear, the newest topic-naming reply
    # on the context can still belong to the conversation the guest just cleared,
    # and anchoring to it prefills the form with the old property.
    try:
        active_cuid = str(context._active_conversation_uid() or "").strip()
    except Exception:
        active_cuid = ""
    exclude_norm = re.sub(r"\s+", " ", str(exclude_text or "").strip().lower())
    for m in reversed(msgs):
        if active_cuid:
            meta = getattr(m, "metadata", None)
            if isinstance(meta, dict):
                cuid = str(meta.get("conversation_uid") or "").strip()
                if cuid and cuid != active_cuid:
                    continue
        role_raw = getattr(m, "role", None)
        role = getattr(role_raw, "value", None) or str(role_raw or "").lower()
        if role != "assistant":
            continue
        content = str(getattr(m, "content", "") or "").strip()
        if not content or len(content) < 12:
            continue
        norm = re.sub(r"\s+", " ", content.lower())
        if norm == exclude_norm:
            continue
        if _looks_like_generic_form_open_reply(content):
            continue
        return content
    return ""


def _booking_team_only_for_hotel(hotel_code: Any) -> bool:
    """True when this hotel's room-booking turn offers ONLY the team button.

    Opt-IN list from settings.chat_booking_team_only_hotels_json — a hotel that
    is not named there keeps both buttons ("Book on website" + "Get help from
    our team") exactly as before, so this can never change another tenant's
    flow. For a property with no real booking engine, where sending the guest to
    a homepage is worse than taking their details.
    """
    code = str(hotel_code or "").strip().lower()
    if not code:
        return False
    admin_team_only = prebooking_settings_service.team_only_booking(code)
    if admin_team_only is not None:
        # Room Booking has a form button and no link button = team-only booking.
        return admin_team_only
    raw = str(getattr(settings, "chat_booking_team_only_hotels_json", "") or "").strip()
    if not raw:
        return False
    try:
        parsed = json.loads(raw)
    except Exception:
        logger.warning("chat_booking_team_only_hotels_json is not valid JSON; ignoring")
        return False
    if not isinstance(parsed, list):
        return False
    return code in {str(item or "").strip().lower() for item in parsed}


def _flatten_service_identifier(value: Any) -> str:
    """"Room Booking", "room_booking" and "room-booking" are one identifier.

    The service that answered reaches us under whichever spelling the path that
    set it happened to use — the id on one, the display label on another — so
    both have to collapse to the same token before they can be compared.
    """
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def _muted_image_services_for_hotel(hotel_code: Any) -> set[str]:
    """Service identifiers whose replies must carry no photos, for this hotel.

    Opt-IN map from settings.chat_images_muted_services_json. A hotel that is
    not named there gets an empty set and keeps images exactly as before, so
    this can never reach another tenant.
    """
    code = str(hotel_code or "").strip().lower()
    if not code:
        return set()
    admin = prebooking_settings_service.settings_for(code)
    if admin is not None:
        return {
            _flatten_service_identifier(item)
            for item in (admin.get("images_muted_services") or [])
            if _flatten_service_identifier(item)
        }
    raw = str(getattr(settings, "chat_images_muted_services_json", "") or "").strip()
    if not raw:
        return set()
    try:
        parsed = json.loads(raw)
    except Exception:
        logger.warning("chat_images_muted_services_json is not valid JSON; ignoring")
        return set()
    if not isinstance(parsed, dict):
        return set()
    wanted = None
    for key, value in parsed.items():
        if str(key or "").strip().lower() == code:
            wanted = value
            break
    if not isinstance(wanted, list):
        return set()
    return {
        _flatten_service_identifier(item)
        for item in wanted
        if _flatten_service_identifier(item)
    }


def _images_muted_for_turn(hotel_code: Any, response: ChatResponse) -> bool:
    """True when the service that answered THIS turn must show no photos.

    Which service answered is read the way the rest of this module reads it:
    `service_agent_id` is not set on every path, but the label the router emits
    always is, and both normalise to the same token.
    """
    muted = _muted_image_services_for_hotel(hotel_code)
    if not muted:
        return False
    metadata = response.metadata if isinstance(response.metadata, dict) else {}
    for candidate in (
        metadata.get("service_agent_id"),
        metadata.get("orchestration_target_service_id"),
        metadata.get("service_llm_label"),
        getattr(response, "service_llm_label", ""),
    ):
        token = _flatten_service_identifier(candidate)
        if token and token in muted:
            logger.info(
                "[images] MUTED: %s has no photo of what %r covers, so this reply "
                "carries none rather than the wrong one", hotel_code, token,
            )
            return True
    return False


# Every guest-facing button whose label a reply might name. Kept in one place so
# a new button only has to be listed here to be covered.
_CTA_BUTTON_LABELS = (
    "share your booking details",
    "share your event details",
)


def _strip_unavailable_button_mentions(response: ChatResponse) -> None:
    """Delete any sentence that points at a button this turn is not rendering.

    Runs LAST, after every CTA attach has decided what the guest will actually
    see, so it can compare the words against the buttons rather than guessing.
    The model is told not to name a spent button and does it anyway - first for
    room booking, then, once that was patched in the booking path alone, for
    events. Both were the same bug in two places, so the check belongs here:
    whatever the reply names, if the button is not attached, the sentence goes.

    Sentence-scoped, and only sentences carrying a button's own label, so the
    rest of the answer is untouched. The labels stay in English inside a Hindi
    or Punjabi reply, which is what makes this work across languages. Fails
    safe: if removal would empty the text, the original is kept.
    """
    import re as _re

    meta = response.metadata if isinstance(response.metadata, dict) else {}
    present: set[str] = set()
    for _key in ("assist_cta", "event_cta", "booking_cta"):
        _cta = meta.get(_key)
        if isinstance(_cta, dict):
            _label = str(_cta.get("label") or "").strip().casefold()
            if _label:
                present.add(_label)
    missing = [lbl for lbl in _CTA_BUTTON_LABELS if lbl not in present]
    if not missing:
        return

    def _clean(text: str) -> str:
        low = text.casefold()
        if not any(lbl in low for lbl in missing):
            return text
        kept: list[str] = []
        for line in text.split("\n"):
            low_line = line.casefold()
            if not any(lbl in low_line for lbl in missing):
                kept.append(line)
                continue
            parts = _re.split(r"(?<=[.!?])\s+", line)
            kept.append(
                " ".join(p for p in parts if not any(lbl in p.casefold() for lbl in missing)).strip()
            )
        out = _re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
        return out or text

    for _attr in ("message", "display_message"):
        _val = str(getattr(response, _attr, "") or "")
        if _val:
            try:
                setattr(response, _attr, _clean(_val))
            except Exception:
                pass
    if isinstance(response.metadata, dict):
        for _key in ("display_message", "canonical_message"):
            _val = str(response.metadata.get(_key) or "")
            if _val:
                response.metadata[_key] = _clean(_val)


async def _attach_admin_service_button(
    *, meta: dict, response: ChatResponse, hotel_code: str, admin: dict, context: Any = None
) -> None:
    """Admin-managed hotels: the service that answered this turn gets the
    buttons set for it on the Pre-booking tab.

    - a link button opens its link: the link of the property in focus when the
      hotel has one per property, else the shared link; with two or more
      properties in focus and no shared link it waits until the guest picks;
    - the form button opens that service's own form, once per conversation per
      service - once its form is in, the button is spent.

    Room booking is not handled here: its buttons ride the booking flow
    (`_apply_booking_cta_marker`), which already knows when a booking is being
    discussed and which property it is for."""
    buttons = admin.get("service_buttons") or {}
    if not buttons:
        return
    responder = ""
    for candidate in (
        meta.get("service_agent_id"),
        meta.get("orchestration_target_service_id"),
        meta.get("service_llm_label"),
        getattr(response, "service_llm_label", ""),
    ):
        token = _flatten_service_identifier(candidate)
        if token:
            responder = token
            break
    if not responder:
        return
    from models.database import AsyncSessionLocal as _Session, BotService as _BotService
    from services.db_config_service import db_config_service as _dcs
    from services.form_fields_service import extract_form_fields

    scope = _dcs.set_hotel_context(hotel_code)
    try:
        service = None
        for candidate in (await _dcs.get_services()) or []:
            if not isinstance(candidate, dict):
                continue
            if responder in {
                _flatten_service_identifier(candidate.get("id")),
                _flatten_service_identifier(candidate.get("name")),
            }:
                service = dict(candidate)
                break
        if not service:
            return
        service_id = str(service.get("id") or "")
        configured = buttons.get(service_id) or []
        if not configured or prebooking_settings_service.looks_like_room_booking(service_id):
            return
        pending = getattr(context, "pending_data", None) or {}
        active = [str(p) for p in (pending.get("_active_property_ids") or []) if str(p).strip()]
        links = []
        for button in configured:
            if button.get("action") != "link":
                continue
            url = prebooking_settings_service.link_url_for(
                hotel_code, button, *(active if len(active) == 1 else [])
            )
            if url:
                links.append({"url": url, "label": str(button.get("label") or "")})
        if links:
            meta["service_links"] = links
        form_button = next((b for b in configured if b.get("action") == "form"), None)
        label = str((form_button or {}).get("label") or "").strip()
        if not label:
            return
        # The submit path records the service either by its id or by its row id.
        submitted_as = {_flatten_service_identifier(service_id)}
        hotel_id = await _dcs.get_current_hotel_id()
        async with _Session() as _session:
            row_id = (await _session.execute(
                select(_BotService.id).where(
                    _BotService.hotel_id == hotel_id, _BotService.service_id == service_id
                )
            )).scalar_one_or_none()
        if row_id is not None:
            submitted_as.add(str(row_id))
        done = (getattr(context, "pending_data", None) or {}).get("_form_submitted_services") or []
        if any(_flatten_service_identifier(s) in submitted_as or str(s) in submitted_as for s in done):
            logger.info("[service_button] skip: %s form already submitted in this conversation", service_id)
            return
        fields = await extract_form_fields(service)
    except Exception as exc:
        logger.warning("[service_button] skipped: %r", exc)
        return
    finally:
        _dcs.reset_hotel_context(scope)
    if not fields:
        return
    logger.info("[service_button] offering %s as a button (%d fields)", service_id, len(fields))
    meta["event_cta"] = {
        "label": label,
        "service_id": service_id,
        "fields": fields,
        "once_per_conversation": True,
    }


async def _attach_event_cta(
    *, response: ChatResponse, hotel_code: str, context: Any = None
) -> None:
    """Offer the event enquiry as a BUTTON, not as a question.

    The banquet agent used to finish a venue answer with "Would you like to go
    ahead and submit an event enquiry?" — which makes the guest type "yes" and
    wait another turn for a form we could have handed them immediately. The room
    lane already does the right thing (`assist_cta` -> "Share your booking
    details"), so events get the same: the answer, then one button that opens
    the enquiry form.

    Opt-IN via chat_welcome_service_ctas_json, which is exactly the set of
    hotels that sell events. A hotel that is not listed gets nothing here and
    its payload is unchanged.
    """
    if not isinstance(response.metadata, dict):
        response.metadata = {}
    meta = response.metadata
    # A form is already open, or we have already offered — nothing to add.
    if meta.get("form_trigger") or meta.get("event_cta"):
        logger.info("[event_cta] skip: a form is already open on this turn")
        return
    _admin = prebooking_settings_service.settings_for(hotel_code)
    if _admin is not None:
        await _attach_admin_service_button(
            meta=meta, response=response, hotel_code=hotel_code, admin=_admin, context=context
        )
        return
    # THE ENQUIRY IS ALREADY IN — the button is spent for this conversation.
    # The widget retires it after one submission (`once_per_conversation`), so
    # attaching it again produces a reply that says "tap Share your event
    # details" over nothing. Server and browser have to agree on this, which is
    # what `_form_submitted_services` records at submit time.
    try:
        _pd = getattr(context, "pending_data", None) if context is not None else None
        _done = (_pd or {}).get("_form_submitted_services") or []
        if any("banquet" in str(s).lower() or "event" in str(s).lower() for s in _done):
            logger.info("[event_cta] skip: the event enquiry has already been submitted")
            return
    except Exception:
        pass
    try:
        from services.widget_deployment_service import widget_deployment_service

        _ctas = widget_deployment_service.welcome_service_ctas_for_hotel(hotel_code)
        if not _ctas:
            logger.info("[event_cta] skip: %r configures no service CTAs", hotel_code)
            return
    except Exception as _cfg_exc:
        logger.warning("[event_cta] skip: CTA config lookup failed: %r", _cfg_exc)
        return

    responder = ""
    for candidate in (
        meta.get("service_agent_id"),
        meta.get("orchestration_target_service_id"),
        meta.get("service_llm_label"),
        getattr(response, "service_llm_label", ""),
    ):
        token = _flatten_service_identifier(candidate)
        if token:
            responder = token
            break
    if "banquet" not in responder and "event" not in responder:
        logger.info("[event_cta] skip: this turn was answered by %r, not the event lane", responder)
        return

    # The fields come from the service's own form_config, so the button opens
    # exactly the form the guest would have got by answering "yes".
    # The service is admin-created, so it lives in the DATABASE, not the JSON
    # config — `config_service.get_service` returns None for it. Resolve it the
    # same way the form-trigger path does: JSON first, then the DB.
    try:
        from services.form_fields_service import extract_form_fields

        service = config_service.get_service("banquet_event_enquiry")
        if not service:
            from services.db_config_service import db_config_service as _dcs

            for _candidate in (await _dcs.get_services()) or []:
                if not isinstance(_candidate, dict):
                    continue
                if _flatten_service_identifier(_candidate.get("id")) == "banqueteventenquiry":
                    service = dict(_candidate)
                    break
        if not service:
            logger.warning("[event_cta] banquet_event_enquiry not found in JSON or DB")
            return
        fields = await extract_form_fields(service)
    except Exception as exc:
        logger.warning("[event_cta] could not load the event form (skipping): %r", exc)
        return
    if not fields:
        logger.warning("[event_cta] the event service has no form fields")
        return
    logger.info("[event_cta] offering the event form as a button (%d fields)", len(fields))

    meta["event_cta"] = {
        "label": "Share your event details",
        "service_id": "banquet_event_enquiry",
        "fields": fields,
        # One enquiry per conversation: once they have sent it, the button has
        # done its job and repeating it reads as if we lost the first one.
        "once_per_conversation": True,
    }


def _form_cancel_enabled_for_hotel(hotel_code: Any) -> bool:
    """True when this hotel's in-chat service forms get a "Cancel request" button.

    Opt-IN list from settings.chat_form_cancel_enabled_hotels_json — a hotel that
    is not named there keeps the form exactly as before. Mirrors the shape of the
    suggestion-chip opt-out list so per-tenant UI switches stay in one place (.env).
    """
    code = str(hotel_code or "").strip().lower()
    if not code:
        return False
    admin = prebooking_settings_service.settings_for(code)
    if admin is not None:
        return bool(admin.get("form_cancel_button", True))
    raw = str(getattr(settings, "chat_form_cancel_enabled_hotels_json", "") or "").strip()
    if not raw:
        return False
    try:
        parsed = json.loads(raw)
    except Exception:
        logger.warning("chat_form_cancel_enabled_hotels_json is not valid JSON; ignoring")
        return False
    if not isinstance(parsed, list):
        return False
    return code in {str(item or "").strip().lower() for item in parsed}


async def _inject_form_trigger(
    response: ChatResponse,
    *,
    user_message: str = "",
    prefetched_services: Optional[List[dict[str, Any]]] = None,
    request_metadata: Optional[dict[str, Any]] = None,
    context: Any = None,
    previous_bot_message: Optional[str] = None,
) -> None:
    """
    When the bot is actively collecting info for a ticketing-enabled service,
    extract that service's form fields and attach them to the response metadata
    so the frontend renders an inline form instead of plain text Q&A.

    `request_metadata` and `context` are optional. When supplied, form fields
    are pre-filled (deterministic mapping + optional LLM textarea extraction)
    so the guest does not retype information the bot already knows.
    """
    import sys as _sys
    from services.form_fields_service import apply_prefill, extract_form_fields
    from services.config_service import config_service
    from services.form_mode_service import (
        canonicalize_trigger_pending_data,
        infer_trigger_value_from_message,
        normalize_form_field_key,
        normalize_trigger_missing_fields,
        resolve_trigger_field_value,
        strip_form_confirmation_instructions,
    )

    if not isinstance(response.metadata, dict):
        response.metadata = {}
    meta = response.metadata
    if not isinstance(meta.get("entities"), dict):
        meta["entities"] = {}
    entities_meta = meta["entities"]
    state_val = str(getattr(response.state, "value", response.state) or "").strip().lower()
    orchestration_action = str(
        entities_meta.get("orchestration_action")
        or meta.get("orchestration_action")
        or ""
    ).strip().lower()
    missing_fields_raw = (
        entities_meta.get("missing_fields")
        or meta.get("missing_fields")
        or []
    )
    if isinstance(missing_fields_raw, (list, tuple)):
        missing_fields = [str(item).strip() for item in missing_fields_raw if str(item).strip()]
    elif str(missing_fields_raw or "").strip():
        missing_fields = [str(missing_fields_raw).strip()]
    else:
        missing_fields = []
    pending_action_hint = str(
        entities_meta.get("pending_action")
        or meta.get("pending_action")
        or ""
    ).strip().lower()
    response_text = str(response.message or "").strip().lower()
    display_text = str(
        getattr(response, "display_message", "")
        or meta.get("display_message")
        or ""
    ).strip().lower()
    message_collection_hint = any(
        phrase in response_text or phrase in display_text
        for phrase in (
            "please fill in the details below",
            "please fill in the booking details below",
            "please fill in the form below",
            "please proceed with the booking details",
        )
    )
    collecting_signal = (
        state_val == "awaiting_info"
        or orchestration_action == "collect_info"
        or bool(missing_fields)
        or pending_action_hint.startswith("collect_")
        or message_collection_hint
    )
    print(
        (
            f"[form_trigger] state={state_val!r} "
            f"orchestration_action={orchestration_action!r} "
            f"missing_fields={missing_fields!r} "
            f"pending_action_hint={pending_action_hint!r} "
            f"message_collection_hint={message_collection_hint} "
            f"collecting_signal={collecting_signal}"
        ),
        file=_sys.stderr,
        flush=True,
    )

    if meta.get("form_trigger") or meta.get("ticket_created"):
        print(f"[form_trigger] SKIP: already has form_trigger or ticket_created", file=_sys.stderr, flush=True)
        # No form is coming on this turn. The service agent often ends its ack
        # with "Please fill in the details below." because that sentence is
        # normally stripped and re-appended further down — but that code sits
        # after this early return, so on a skip the invitation survives and the
        # guest is told to fill in a form that never appears. Seen live: the
        # request was logged straight to a ticket ("connect me to the front
        # desk"), the form was correctly skipped, and the reply still asked for
        # details. Only strip when no form_trigger is set; a real form keeps it.
        if not meta.get("form_trigger"):
            _strip_form_invite(response)
        return

    # â”€â”€ Resolve target_service_id from ALL available sources â”€â”€
    _norm_svc = lambda s: str(s or "").strip().lower().replace(" ", "_").replace("-", "_")
    target_service_id = ""
    # Source 1: entities.target_service_id (always populated by chat_service)
    if not target_service_id:
        target_service_id = str(entities_meta.get("target_service_id") or "").strip()
    # Source 2: top-level metadata
    if not target_service_id:
        target_service_id = str(
            meta.get("orchestration_target_service_id")
            or meta.get("pending_service_id")
            or ""
        ).strip()
    # Source 3: service_llm_label (display name fallback)
    if not target_service_id:
        label = str(
            meta.get("service_llm_label")
            or getattr(response, "service_llm_label", "")
            or ""
        ).strip()
        if label and label.lower() != "main":
            target_service_id = label

    print(f"[form_trigger] target_service_id={target_service_id!r}", file=_sys.stderr, flush=True)

    # A FORM ANSWERS THE LAST BOT MESSAGE, NOTHING OLDER.
    # The router guard can refuse to route a contextless "yes" to support and the
    # form still appeared, because the turn's leftover state (awaiting_info /
    # collect_form_details / support_request) is what this function reads - the
    # route is never consulted here. So a yes that accepted nothing still got the
    # guest a form asking for their name, phone and email.
    # The judge that ran earlier this turn read the PREVIOUS bot message and
    # answered whether anything was actually offered. If it says the guest agreed
    # to nothing, no form opens - whatever the leftover state says. A guest who
    # asked for staff in their own words is untouched: that is not a bare yes.
    try:
        _sid_for_form = str(getattr(context, "session_id", "") or "").strip()
        # The message the guest was answering. The WS route loads `context` AFTER
        # this turn was saved, so its newest assistant message is this turn's own
        # reply (no "?" in "Please review the details below...") and the check
        # below skipped the form on a real "yes"; that route now passes the
        # message it read before the turn ran. HTTP passes a pre-turn context.
        _prev_bot = str(
            previous_bot_message
            if previous_bot_message is not None
            else llm_orchestration_service._last_message_by_role(context, "assistant") or ""
        )
        # THE JUDGE IS NOT ALLOWED TO OVERRULE A QUESTION THAT IS RIGHT THERE.
        # It called "Would you like our team to reach out to share that with you?"
        # an offer of NOTHING, this skip fired on a real acceptance, stripping the
        # invitation left the reply empty, and the guest was shown "No response".
        # So the skip now needs both: the judge saying nothing was offered AND the
        # previous bot message containing no question at all. When the bot plainly
        # asked something, its answer gets the form, whatever the judge thinks.
        _prev_bot_asked = "?" in _prev_bot
        if (
            _sid_for_form
            and not _prev_bot_asked
            and llm_orchestration_service._bare_yes_accepted_nothing(
                session_id=_sid_for_form, user_message=user_message,
            )
        ):
            print(
                "[form_trigger] SKIP: the last bot message asked nothing and offered "
                f"nothing, so this bare agreement opens no form (service={target_service_id!r})",
                file=_sys.stderr, flush=True,
            )
            _strip_form_invite(response)
            # The reply on these turns is usually NOTHING BUT the invitation, so
            # stripping empties it. Restoring the original would tell the guest to
            # fill in a form that is not there, and leaving it empty showed them
            # "No response" - both were tried, both are worse. A bare yes that
            # accepted nothing is a turn where the bot genuinely does not know what
            # is being agreed to, so it asks. This is the one sentence in this
            # function, and only for that dead end.
            if not str(response.message or "").strip():
                _ask = "Sure - what would you like help with?"
                response.message = _ask
                if getattr(response, "display_message", None) is not None:
                    response.display_message = _ask
                _meta_now = getattr(response, "metadata", None)
                if isinstance(_meta_now, dict) and _meta_now.get("display_message"):
                    _meta_now["display_message"] = _ask
            # NO FORM ON SCREEN, SO NOTHING MAY WAIT FOR ONE (Mango).
            # This turn parked the session on collect_form_details. With the form
            # stripped nothing can answer it, and the guest's next "connect me to
            # staff" was refused its form too ("trigger_field 'needs_callback' not
            # yet in pending_data", state escalated - Mango, 9 Oct). Un-park it the
            # way the form's own Cancel button does. The session is re-read rather
            # than the object passed in saved: on the HTTP path that object is the
            # snapshot from before this turn.
            try:
                from services.booking_assistance_state import team_booking_enabled as _ba_team_only

                if _ba_team_only(str(getattr(context, "hotel_code", "") or "")):
                    _live_ctx = await context_manager.get_context(_sid_for_form)
                    if _live_ctx is not None and str(
                        getattr(_live_ctx, "pending_action", "") or ""
                    ) == "collect_form_details":
                        _live_ctx.pending_action = None
                        _live_ctx.state = ConversationState.IDLE
                        async with AsyncSessionLocal() as _unpark_db:
                            await context_manager.save_context(_live_ctx, db_session=_unpark_db)
                        print(
                            "[form_trigger] no form shown, so the session is no longer "
                            "waiting for one (pending_action cleared)",
                            file=_sys.stderr, flush=True,
                        )
            except Exception as _unpark_exc:
                print(f"[form_trigger] un-park after skip failed: {_unpark_exc!r}", file=_sys.stderr, flush=True)
            return
    except Exception:
        pass  # never block a legitimate form on a bookkeeping error

    if not target_service_id:
        print(f"[form_trigger] SKIP: no target_service_id found", file=_sys.stderr, flush=True)
        return

    # Room/stay bookings are ALWAYS text + website link (with a silent staff
    # ticket) — they must NEVER render an in-chat form. Hard-skip form injection
    # for the room-booking service regardless of its config, so a booking turn
    # that sets pending_action='collect_check_in' (or any collect_* signal) can
    # never pop a contact form on top of the reply and block the chat.
    _ROOM_BOOKING_SVCS = {"room_booking", "room_booking_request", "stay_booking", "room_stay_booking"}
    if _norm_svc(target_service_id) in _ROOM_BOOKING_SVCS:
        print(f"[form_trigger] SKIP: room-booking is text/website-only, no form", file=_sys.stderr, flush=True)
        return

    # â”€â”€ Load service definition (JSON first, then DB fallback) â”€â”€
    service = config_service.get_service(target_service_id)
    if not service:
        # Try normalized version (spaces â†’ underscores)
        service = config_service.get_service(_norm_svc(target_service_id))
    # DB fallback: service may exist only in the database (admin-created)
    if not service:
        try:
            _db_svcs = (
                [dict(row) for row in (prefetched_services or []) if isinstance(row, dict)]
                if isinstance(prefetched_services, list)
                else []
            )
            if not _db_svcs:
                from services.db_config_service import db_config_service as _dcs
                _db_svcs = await _dcs.get_services()
            _target_norm = _norm_svc(target_service_id)
            for _candidate in (_db_svcs or []):
                if not isinstance(_candidate, dict):
                    continue
                if _norm_svc(_candidate.get("id")) == _target_norm or _norm_svc(_candidate.get("name")) == _target_norm:
                    service = dict(_candidate)
                    break
        except Exception as _db_exc:
            print(f"[form_trigger] DB fallback error: {_db_exc!r}", file=_sys.stderr, flush=True)
    ticketing_enabled = bool(service.get("ticketing_enabled", False)) if service else False
    print(f"[form_trigger] service={service.get('id') if service else None}, ticketing_enabled={ticketing_enabled}", file=_sys.stderr, flush=True)

    if not service or not ticketing_enabled:
        print(f"[form_trigger] SKIP: service not found or ticketing not enabled", file=_sys.stderr, flush=True)
        return

    # Respect ticketing_mode: only inject form UI when mode is "form".
    # "text" mode keeps the conversational text collection (no form injected).
    # "none" means ticketing is disabled (already caught above via ticketing_enabled).
    ticketing_mode = str(service.get("ticketing_mode") or "").strip().lower()
    if ticketing_mode == "text":
        print(f"[form_trigger] SKIP: ticketing_mode=text, using conversational collection", file=_sys.stderr, flush=True)
        return
    if ticketing_mode == "none":
        print(f"[form_trigger] SKIP: ticketing_mode=none", file=_sys.stderr, flush=True)
        return
    # If ticketing_mode is empty/unset, default to form injection for backward compat
    if ticketing_mode and ticketing_mode not in ("form", "text", "none"):
        ticketing_mode = "form"

    # â”€â”€ Trigger-field gate: do not show form until the trigger field is collected â”€â”€
    # The service's form_config.trigger_field specifies which field (e.g. room_type,
    # terminal) must be known before the form is shown.
    # We check TWO signals:
    #   1. trigger field is explicitly listed in missing_fields â†’ definitely not collected
    #   2. trigger field value is absent from pending_data â†’ not collected yet
    # The form only shows when the trigger field is NOT in missing_fields AND its
    # value IS present in pending_data.
    svc_form_config = service.get("form_config") if isinstance(service.get("form_config"), dict) else {}
    trigger_field_cfg = svc_form_config.get("trigger_field") if isinstance(svc_form_config.get("trigger_field"), dict) else {}
    trigger_field_id = normalize_form_field_key(trigger_field_cfg.get("id"))
    trigger_collected = False
    if trigger_field_id:
        trigger_field_label = str(trigger_field_cfg.get("label") or trigger_field_id).strip()
        collected_data = (
            entities_meta.get("pending_data")
            or meta.get("pending_data")
            or {}
        )
        collected_data, _trigger_alias_key, _ = canonicalize_trigger_pending_data(
            collected_data,
            trigger_field_id,
            trigger_field_label,
        )
        entities_meta["pending_data"] = dict(collected_data)
        meta["pending_data"] = dict(collected_data)
        _, trigger_value_raw = resolve_trigger_field_value(
            collected_data,
            trigger_field_id,
            trigger_field_label,
        )
        trigger_value = str(trigger_value_raw or "").strip()
        if not trigger_value:
            # Only trust the guest's own words for trigger inference here.
            # Inferring from assistant-generated text can manufacture a room/type
            # that the guest never explicitly selected.
            inferred_key, inferred_value = infer_trigger_value_from_message(
                collected_data,
                trigger_field_id,
                trigger_field_label,
                user_message,
            )
            if inferred_value:
                infer_field = normalize_form_field_key(inferred_key) or trigger_field_id
                collected_data[infer_field] = inferred_value
                entities_meta["pending_data"] = dict(collected_data)
                meta["pending_data"] = dict(collected_data)
                trigger_value = str(inferred_value).strip()
                meta["form_trigger_inferred_trigger_value"] = True
                print(
                    f"[form_trigger] inferred trigger_field '{trigger_field_id}' from message: {trigger_value!r}",
                    file=_sys.stderr,
                    flush=True,
                )
        missing_fields = normalize_trigger_missing_fields(
            missing_fields,
            trigger_field_id,
            trigger_field_label,
            trigger_value_present=bool(trigger_value),
        )
        entities_meta["missing_fields"] = list(missing_fields)
        meta["missing_fields"] = list(missing_fields)
        missing_lower = [normalize_form_field_key(f) for f in missing_fields]
        trigger_missing = trigger_field_id in missing_lower
        trigger_collected = bool(trigger_value) and not trigger_missing
        if trigger_collected:
            if orchestration_action == "respond_only":
                orchestration_action = "collect_info"
                entities_meta["orchestration_action"] = orchestration_action
                meta["orchestration_action"] = orchestration_action
                meta["form_mode_forced_collect_info"] = True
            if not str(entities_meta.get("pending_action") or "").strip():
                entities_meta["pending_action"] = "collect_form_details"
            if not str(meta.get("pending_action") or "").strip():
                meta["pending_action"] = "collect_form_details"
            print(
                f"[form_trigger] trigger_field '{trigger_field_id}' collected: {trigger_value!r}",
                file=_sys.stderr,
                flush=True,
            )
        elif trigger_missing:
            print(
                f"[form_trigger] SKIP: trigger_field '{trigger_field_id}' still in missing_fields",
                file=_sys.stderr, flush=True,
            )
            return
        else:
            if not collecting_signal:
                print(
                    f"[form_trigger] SKIP: no collection signal and trigger_field '{trigger_field_id}' missing",
                    file=_sys.stderr,
                    flush=True,
                )
                return
            # LLM explicitly signalled collect_form_details — the trigger was confirmed
            # conversationally even though pending_data does not carry the trigger-field value.
            # Trust the LLM's intent and bypass the trigger-field gate.
            if pending_action_hint == "collect_form_details":
                _room_booking_trigger = (
                    _norm_svc(target_service_id).startswith("room_booking")
                    and trigger_field_id == "room_type"
                )
                if _room_booking_trigger:
                    print(
                        "[form_trigger] SKIP: room booking form requires an explicit room_type before bypassing trigger gate",
                        file=_sys.stderr,
                        flush=True,
                    )
                    return
                if orchestration_action == "respond_only":
                    orchestration_action = "collect_info"
                    entities_meta["orchestration_action"] = orchestration_action
                    meta["orchestration_action"] = orchestration_action
                    meta["form_mode_forced_collect_info"] = True
                print(
                    f"[form_trigger] trigger bypass: pending_action=collect_form_details, "
                    f"proceeding without '{trigger_field_id}' value",
                    file=_sys.stderr,
                    flush=True,
                )
                # Fall through to extract_form_fields below
            else:
                print(
                    f"[form_trigger] SKIP: trigger_field '{trigger_field_id}' not yet in pending_data "
                    f"(available keys: {list(collected_data.keys())})",
                    file=_sys.stderr, flush=True,
                )
                return
    else:
        if not collecting_signal:
            print(f"[form_trigger] SKIP: no collection signal", file=_sys.stderr, flush=True)
            return
        if orchestration_action == "respond_only" and not message_collection_hint:
            print(
                f"[form_trigger] SKIP: orchestration_action=respond_only (no form hint, no trigger field)",
                file=_sys.stderr,
                flush=True,
            )
            return

    # ── CTA single-lead: skip the redundant booking form on a genuine
    # booking/collect turn. We only reach here when the form WOULD open (all the
    # collect-info gates above passed). If the guest already created a booking
    # lead this session (via "Book a Stay" → contact form), we already have their
    # name/phone/email — so do NOT re-open the form or create a second ticket.
    # Hand them the booking button instead and update the existing lead.
    # (Placed AFTER the collect gates so informational room/offer questions —
    # which never reach here — are still answered normally by the agent.)
    _meta_for_lead = request_metadata if isinstance(request_metadata, dict) else {}
    _lead_active = bool(_meta_for_lead.get("booking_lead_active"))
    _lead_service_id = _norm_svc(_meta_for_lead.get("booking_lead_service_id"))
    if _lead_active and _lead_service_id and _norm_svc(target_service_id) == _lead_service_id:
        _is_explicit_resume = _is_explicit_booking_lead_resume_turn(
            user_message=str(user_message or ""),
            request_metadata=_meta_for_lead,
            context=context,
        )
        _has_required_contact, _missing_contact_fields = _booking_lead_has_required_contact_fields(
            request_metadata=_meta_for_lead,
            context=context,
            service=service,
        )
        if not _is_explicit_resume:
            print(
                "[form_trigger] SKIP: booking lead is active but this turn is informational, "
                "so continue with the normal response/form path",
                file=_sys.stderr,
                flush=True,
            )
        elif not _has_required_contact:
            print(
                f"[form_trigger] SKIP: booking lead is active but contact details are incomplete "
                f"(missing={_missing_contact_fields!r})",
                file=_sys.stderr,
                flush=True,
            )
        else:
            _lead_ticket_id = str(_meta_for_lead.get("booking_lead_id") or "").strip()
            print(
                f"[form_trigger] SKIP: booking lead already active (lead_id={_lead_ticket_id!r}) — "
                f"no duplicate form/ticket; preserve the room reply and attach booking CTA",
                file=_sys.stderr, flush=True,
            )
            try:
                _biz_lead = await db_config_service.get_business_info() or {}
            except Exception:
                _biz_lead = {}
            _lead_url = str(_biz_lead.get("booking_engine_url") or _biz_lead.get("website") or "").strip()
            # Resolve the single active property so the button is property-specific
            # (and suppressed when 2+ are in focus, matching the rest of the flow).
            _lead_pd = context.pending_data if (context is not None and isinstance(getattr(context, "pending_data", None), dict)) else {}
            _lead_ids = [str(x).strip() for x in (_lead_pd.get("_active_property_ids") or []) if str(x).strip()]
            _lead_names = [str(n).strip() for n in (_lead_pd.get("_active_property_names") or []) if str(n).strip()]
            _lead_prop = ""
            if len(_lead_ids) == 1:
                _lead_prop = _lead_names[0] if _lead_names else _lead_ids[0].replace("_", " ").title()
            try:
                _preserved_msg = _strip_booking_lead_form_copy(str(response.message or "").strip())
                if _preserved_msg:
                    response.message = _preserved_msg
                if hasattr(response, "display_message"):
                    _display_msg = _strip_booking_lead_form_copy(str(getattr(response, "display_message", "") or ""))
                    if _display_msg:
                        response.display_message = _display_msg
                if isinstance(response.metadata, dict):
                    _meta_display = _strip_booking_lead_form_copy(str(response.metadata.get("display_message") or ""))
                    _meta_canonical = _strip_booking_lead_form_copy(str(response.metadata.get("canonical_message") or ""))
                    if _meta_display:
                        response.metadata["display_message"] = _meta_display
                    if _meta_canonical:
                        response.metadata["canonical_message"] = _meta_canonical
                    if _lead_prop:
                        import re as _re_lead
                        _slug = _re_lead.sub(r"[^a-z0-9]+", "-", _lead_prop.lower()).strip("-")
                        # Prefer the real per-property booking URL (matched on the
                        # property name + slug), then any tenant-wide lead url, then
                        # a placeholder slug.
                        _lead_hotel = str(
                            getattr(context, "hotel_code", "")
                            or (request_metadata or {}).get("hotel_code")
                            or ""
                        ).strip()
                        _real_url = _resolve_property_booking_url(
                            _lead_hotel,
                            _lead_prop,
                            (_lead_ids[0] if len(_lead_ids) == 1 else ""),
                        )
                        _url = _real_url or _lead_url or f"https://booking.example.com/{_slug}"
                        response.metadata["booking_cta"] = {"url": _url, "label": "Book on website"}
                    elif len(_lead_ids) <= 1:
                        # Scope unknown (0 active) — keep a generic button. With 2+
                        # active, show no button (guest must pick a property first).
                        response.metadata["booking_cta"] = {"url": _lead_url or "https://booking.example.com/book", "label": "Book on website"}
            except Exception as _msg_exc:
                print(f"[form_trigger] lead CTA attach skipped: {_msg_exc!r}", file=_sys.stderr, flush=True)
            if _lead_ticket_id and str(user_message or "").strip():
                try:
                    import asyncio as _aio
                    from services.ticketing_service import ticketing_service as _tsvc
                    _note = f"Guest update: {str(user_message).strip()[:300]}"
                    _aio.create_task(_tsvc.update_ticket(_lead_ticket_id, _note))
                except Exception as _upd_exc:
                    print(f"[form_trigger] lead update skipped: {_upd_exc!r}", file=_sys.stderr, flush=True)
            return

    fields = await extract_form_fields(service)
    print(f"[form_trigger] extracted {len(fields)} field(s): {[f['id'] for f in fields]}", file=_sys.stderr, flush=True)

    if not fields:
        print(f"[form_trigger] SKIP: no fields extracted", file=_sys.stderr, flush=True)
        return

    # Best-effort pre-fill: deterministic mapping from request metadata + context,
    # plus optional LLM extraction for freeform textarea fields. Failures here
    # must NEVER break the form trigger flow — fall back to blank fields.
    #
    # Build a short rolled-up excerpt of recent conversation turns so the LLM
    # prefill for freeform textareas (e.g. "Issue Details") can infer a value
    # even when the triggering user message is a bare "yes". Gated by a
    # setting flag so the feature can be reverted via .env without code edits.
    conversation_excerpt = ""
    try:
        if bool(getattr(settings, "chat_form_prefill_use_history", True)) and context is not None:
            # Only THIS conversation. Clearing the chat starts a new thread but
            # leaves the old turns on the context, and reading straight past that
            # boundary prefilled a form about the property the guest had just
            # cleared away — a guest who dropped Digha and moved to Chennai got
            # "...regarding Zone by The Park, Digha" typed into the issue field.
            # Messages carry the thread id for exactly this reason.
            try:
                _active_cuid = str(context._active_conversation_uid() or "").strip()
            except Exception:
                _active_cuid = ""

            def _same_thread(msg: Any) -> bool:
                if not _active_cuid:
                    return True  # thread id not stamped: keep the old behaviour
                meta = getattr(msg, "metadata", None)
                if not isinstance(meta, dict):
                    return True
                cuid = str(meta.get("conversation_uid") or "").strip()
                return (not cuid) or cuid == _active_cuid

            _all_msgs = [m for m in (getattr(context, "messages", []) or []) if _same_thread(m)]
            recent_msgs = _all_msgs[-8:]
            excerpt_parts: list[str] = []
            summary_text = str(getattr(context, "summary", "") or "").strip()
            if summary_text:
                excerpt_parts.append(f"SUMMARY: {summary_text}")
            for m in recent_msgs:
                role_raw = getattr(m, "role", None)
                role = getattr(role_raw, "value", None) or str(role_raw or "").lower()
                if role not in {"user", "assistant"}:
                    continue
                content = str(getattr(m, "content", "") or "").strip()
                if not content:
                    continue
                excerpt_parts.append(f"{role.upper()}: {content}")
            conversation_excerpt = "\n".join(excerpt_parts)
    except Exception as _excerpt_exc:
        print(f"[form_trigger] excerpt build failed (continuing without): {_excerpt_exc!r}", file=_sys.stderr, flush=True)
        conversation_excerpt = ""

    # The bot's current reply is normally the strongest prefill anchor (it names
    # the confirmed topic, e.g. "…booking the Dighashree Auditorium"). BUT on a
    # support/booking form-open turn the reply is the content-free "Please review
    # the details below…", which names nothing — so the freeform extractor would
    # return blank. In that case anchor to the most recent prior assistant turn
    # that DID name a topic (e.g. the venue/event the guest just discussed).
    _current_reply = str(
        getattr(response, "message", "")
        or getattr(response, "display_message", "")
        or ""
    )
    _anchor_reply = _current_reply
    try:
        if _looks_like_generic_form_open_reply(_current_reply) or bool(
            meta.get("support_form_submit_required")
        ):
            _prior_topic = _last_topic_assistant_message(
                context, exclude_text=_current_reply
            )
            if _prior_topic:
                _anchor_reply = _prior_topic
    except Exception:
        _anchor_reply = _current_reply

    try:
        prefill_meta = request_metadata if isinstance(request_metadata, dict) else {}
        fields = await apply_prefill(
            fields,
            req_meta=prefill_meta,
            context=context,
            triggering_user_message=str(user_message or ""),
            service=service,
            conversation_excerpt=conversation_excerpt,
            bot_response_text=_anchor_reply,
            allow_llm=True,
        )
        prefilled_ids = [
            str(f.get("id") or "")
            for f in fields
            if isinstance(f, dict) and f.get("default_value")
        ]
        if prefilled_ids:
            print(
                f"[form_trigger] prefilled {len(prefilled_ids)} field(s): {prefilled_ids}",
                file=_sys.stderr,
                flush=True,
            )
    except Exception as _prefill_exc:
        print(f"[form_trigger] prefill failed (continuing with blank fields): {_prefill_exc!r}", file=_sys.stderr, flush=True)

    response.metadata["form_trigger"] = True
    response.metadata["form_fields"] = fields
    response.metadata["form_service_id"] = target_service_id
    # An open form switches the guest's text box OFF, so a guest who does not
    # want to fill it in has no way out. Tell the widget it may render a
    # "Cancel request" button beside Submit. Set ONLY when the hotel opted in,
    # so every other tenant's payload is byte-identical to before.
    if _form_cancel_enabled_for_hotel(getattr(context, "hotel_code", "")):
        response.metadata["form_cancellable"] = True

    # Strip only form-field listings from the bot message (the form UI handles those).
    # Keep room/service descriptions, features, and other informational content intact.
    import re as _re

    # Build a set of field-like keywords so we only strip lines that name form fields,
    # not lines that describe room features or amenities.
    _field_keywords: set[str] = set()
    for _f in fields:
        _field_keywords.add(str(_f.get("id") or "").strip().lower().replace("_", " "))
        _field_keywords.add(str(_f.get("label") or "").strip().lower())
    _field_keywords.discard("")

    def _line_is_field_listing(line_text: str) -> bool:
        """Return True if a bullet/numbered line looks like a form field label."""
        # Extract the text after the bullet/number marker
        inner = _re.sub(r"^[-*\u2022]\s+|^\d+[.)]\s+", "", line_text.strip())
        inner = _re.sub(r"\*{1,2}", "", inner).strip().rstrip(":").strip().lower()
        if not inner:
            return True  # empty bullet â†’ safe to remove
        # Check if this line matches a known form field label/id
        for kw in _field_keywords:
            if kw in inner or inner in kw:
                return True
        # Generic "provide your X" patterns are field prompts
        if _re.match(r"^(your\s+|the\s+|guest.?s?\s+)?(full\s+)?name", inner):
            return True
        if _re.match(r"^(phone|contact|email|check.?in|check.?out|date|time|guests?|special)", inner):
            return True
        return False

    def _strip_field_list(msg: str) -> str:
        if not msg:
            return "Please fill in the details below."
        # Remove only bullet/numbered lines that look like form field labels
        cleaned_lines = []
        for line in msg.split("\n"):
            stripped = line.strip()
            is_bullet = bool(_re.match(r"^[-*\u2022]\s+", stripped) or _re.match(r"^\d+[.)]\s+", stripped))
            if is_bullet and _line_is_field_listing(stripped):
                continue
            cleaned_lines.append(line)
        cleaned = "\n".join(cleaned_lines).strip()
        # Remove trailing prompts like "could you please provide the following details:"
        cleaned = _re.sub(
            r"[,.]?\s*(?:could you |can you |please |kindly )*(?:provide|share|give|send)\s+(?:us\s+|me\s+)?(?:the\s+)?(?:following\s+)?(?:details|information|info)\s*(?:to proceed[^?:.]*)?[?:.]?\s*$",
            ".",
            cleaned,
            flags=_re.IGNORECASE,
        ).strip()
        # Remove dangling clauses that truly end with a colon and nothing after
        # (e.g. "and includes:" at end of text). Do NOT strip mid-sentence "offers:" etc.
        cleaned = _re.sub(
            r"(?:\s*(?:,\s*)?(?:and|which|that|,)\s+)?(?:offers|includes|featuring|features|comes with|has|provides)\s*:\s*$",
            ".",
            cleaned,
            flags=_re.IGNORECASE,
        ).strip()
        # Remove LLM-generated "Please fill in the details below" since we append our own
        cleaned = _re.sub(
            r"\.?\s*Please fill in the (?:details|form|booking details) below\.?\s*",
            ". ",
            cleaned,
            flags=_re.IGNORECASE,
        ).strip()
        # Strip any redundant "would you like our team to reach out / connect you / get back to you"
        # sentence from a form-opening ack — the form IS the handoff, so re-offering it is wrong.
        # Prompt rule covers this but the LLM occasionally still emits it; this is a belt-and-suspenders.
        cleaned = _re.sub(
            r"(?:[.,]?\s*)?(?:would you like|should we|want our team to|can i have someone|let me know if you('|’)?d? like)[^.?!]*?(?:team|someone|us|we)[^.?!]*?(?:reach out|get back|connect|follow up|assist)[^.?!]*?[?.!]",
            "",
            cleaned,
            flags=_re.IGNORECASE,
        ).strip()
        # Clean orphan leading conjunctions left after stripping ("., and our team will…" → "and our team will…")
        cleaned = _re.sub(
            r"(?:^|\.\s*),?\s*(?:and|so|but)\s+(our team|they|we|someone)\b",
            r". \1",
            cleaned,
            flags=_re.IGNORECASE,
        ).strip()
        # Collapse a comma left abutting a sentence break after a clause was
        # stripped (or the LLM emitted a truncated first clause), e.g.
        # "…room decorations,. Our team…" or "…decorations, . Our team…" →
        # "…decorations. Our team…". Must run BEFORE the double-period collapse
        # so any resulting ".." cascade is then flattened. The existing rules
        # only handled ".."/". ."/"! ." — a ",." collision passed through.
        cleaned = _re.sub(r",\s*([.!?])", r"\1", cleaned)
        # Fix double periods, ". ." artifacts, or space-period patterns
        cleaned = _re.sub(r"\.\s*\.", ".", cleaned)
        cleaned = _re.sub(r"\s+\.", ".", cleaned)
        # Fix punctuation collisions left when a clause was stripped between a
        # terminator and the next word, e.g. "Great choice!. to proceed…" →
        # "Great choice! To proceed…". Collapse "!." / "?." / ";." to the
        # stronger mark, drop a stray leading "." then a lowercase word, and
        # recapitalize the first letter of every sentence so we never ship a
        # mid-sentence lowercase fragment.
        cleaned = _re.sub(r"([!?;:])\s*\.", r"\1", cleaned)
        cleaned = _re.sub(r"\.\s*\.", ".", cleaned)

        def _sentence_case(text: str) -> str:
            text = text.strip()
            if not text:
                return text
            # Capitalize the first alphabetic char of the whole string and the
            # first alphabetic char after every sentence terminator.
            def _cap(m: "_re.Match[str]") -> str:
                return m.group(1) + m.group(2).upper()
            text = _re.sub(r"(^|[.!?]\s+)([a-z])", _cap, text)
            return text

        cleaned = _sentence_case(cleaned)
        # Clean up trailing colons or empty sentences
        cleaned = _re.sub(r"[:\s]+$", ".", cleaned).strip()
        if cleaned and cleaned != msg.strip():
            # Strip ANY trailing punctuation/space (commas, semicolons, periods,
            # dashes) before appending, so we never get "…Digha,. Please fill…".
            cleaned = cleaned.rstrip(" .,;:-—") + ". Please fill in the details below."
        return cleaned or "Please fill in the details below."

    # Pre-booking-tab hotels keep web addresses and emails whole.
    _keep_addr = prebooking_settings_service.is_admin_managed(
        str(getattr(context, "hotel_code", "") or "").strip().lower()
    )
    response.message = _strip_field_list(
        strip_form_confirmation_instructions(response.message, keep_web_addresses=_keep_addr)
    )
    if response.display_message:
        response.display_message = _strip_field_list(
            strip_form_confirmation_instructions(response.display_message, keep_web_addresses=_keep_addr)
        )
    # Also clean metadata.display_message since frontend reads it first
    if isinstance(response.metadata, dict) and response.metadata.get("display_message"):
        response.metadata["display_message"] = _strip_field_list(
            strip_form_confirmation_instructions(
                str(response.metadata["display_message"]), keep_web_addresses=_keep_addr
            )
        )

    # ── Lumira render key ──────────────────────────────────────────────────
    # If the service has a lumira_form_key configured, set metadata["render"]
    # so the Lumira frontend knows which popup form to display.
    # build_engage_response() in lumira_compat_adapter already reads this field.
    _lumira_form_key = str(svc_form_config.get("lumira_form_key") or "").strip()
    if _lumira_form_key:
        response.metadata["render"] = _lumira_form_key
        response.metadata["render_type"] = "form"
        print(
            f"[form_trigger] lumira render set: render={_lumira_form_key!r}",
            file=_sys.stderr, flush=True,
        )

    print(f"[form_trigger] SUCCESS: form_trigger set for service={target_service_id!r}", file=_sys.stderr, flush=True)


class FormSubmitRequest(BaseModel):
    """Payload for direct form-based ticket creation."""
    session_id: str
    hotel_code: str = "DEFAULT"
    service_id: str
    form_data: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class FormCancelRequest(BaseModel):
    """Payload for backing out of an open in-chat form WITHOUT submitting it."""
    session_id: str
    hotel_code: str = "DEFAULT"
    service_id: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class FormValidateRequest(BaseModel):
    """Payload for LLM-based form field validation."""
    service_id: str
    form_fields: list[dict[str, Any]] = Field(default_factory=list)
    form_data: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


_FORM_VALIDATE_SYSTEM_PROMPT = """\
You are a smart form-field validator for a hotel service request.
You will receive a list of form fields (with id, label, type) and the values the guest entered.

Your job: validate each field using your knowledge of real-world formats. Be helpful â€” catch genuinely wrong data but accept anything plausible.

CRITICAL: Match the validation rule to each field based ONLY on its "type" attribute (date, time, text, tel, textarea, etc.). Do NOT infer validation from the field's label or id. For example, a field with type="text" must be validated as text even if its label contains the word "date" or "time".

Rules per field type:

- **Name fields** (full name, guest name): Accept any text that could be a name, including single names (e.g., "Sana", "Raj"), hyphenated names, names with apostrophes (e.g., "O'Brien"), or names with accents. Reject ONLY if it contains numbers, is a single character, or is obviously not a name (e.g., "asdf", "xxx").

- **Phone / tel fields**: The value includes a country code prefix (e.g., +91, +1, +44). You MUST strictly validate phone numbers using your knowledge of each country's telecom rules. This is NOT a lenient field â€” phone validation must be accurate.
  * India (+91): EXACTLY 10 digits after +91. First digit MUST be 6, 7, 8, or 9. Numbers starting with 0, 1, 2, 3, 4, or 5 are INVALID in India. Example: +911234567890 â†’ REJECT (starts with 1). +919876543210 â†’ ACCEPT (starts with 9).
  * US/Canada (+1): EXACTLY 10 digits. First digit of area code cannot be 0 or 1.
  * UK (+44): 10-11 digits after +44.
  * UAE (+971): EXACTLY 9 digits after +971.
  * For other countries, use your knowledge of their phone number format.
  * Reject obviously fake/placeholder numbers: all-same digits (9999999999, 1111111111), sequential (1234567890, 9876543210), all-zeros, or any pattern no real person would have. A real phone number should look random/natural.
  * Think like a human â€” would a hotel receptionist accept this number? If it looks fake, reject it politely.

- **Email fields**: Must have @ and a dot after @. Accept most formats.
- **Date fields** (type=date): Must be a valid date. Reject past dates. If check-in and check-out both exist, check-out must be after check-in.
- **Time fields** (type=time): Must be a valid time in HH:MM or HH:MM:SS format (24-hour or 12-hour with AM/PM). Accept any reasonable time value. Do NOT apply date validation rules to time fields.
- **Number fields**: Must be a positive whole number. Reject only values above 100.
- **Textarea / description fields**: Must have at least 2 real words. Reject only single characters or random key-mashing.
- **Other text fields**: Accept any reasonable non-empty text.

For fields OTHER than phone, be lenient â€” when in doubt, accept. For phone numbers, be STRICT and follow the country rules above exactly.

Return ONLY a valid JSON object:
{"valid": true, "errors": []}
or
{"valid": false, "errors": [{"field_id": "...", "message": "..."}]}

Error messages must be short and helpful. Do NOT wrap in markdown. Return raw JSON only.\
"""


def _validate_required_form_fields(
    *,
    form_fields: list[dict[str, Any]],
    form_data: dict[str, Any],
) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    seen: set[str] = set()
    safe_fields = form_fields if isinstance(form_fields, list) else []
    safe_data = form_data if isinstance(form_data, dict) else {}

    for field in safe_fields:
        if not isinstance(field, dict):
            continue
        field_id = str(field.get("id") or "").strip()
        if not field_id or field_id in seen or not bool(field.get("required")):
            continue
        value = str(safe_data.get(field_id) or "").strip()
        if value:
            continue
        label = str(field.get("label") or field_id.replace("_", " ").title()).strip()
        field_type = str(field.get("type") or "").strip().lower()
        options = field.get("options") if isinstance(field.get("options"), list) else []
        verb = "select" if field_type in {"select", "dropdown", "radio"} or options else "enter"
        errors.append(
            {
                "field_id": field_id,
                "message": f"Please {verb} {label}.",
            }
        )
        seen.add(field_id)
    return errors


def _validate_phone_fields(
    *,
    form_fields: list[dict[str, Any]],
    form_data: dict[str, Any],
) -> list[dict[str, str]]:
    """Validate phone fields with Google's libphonenumber (the ``phonenumbers``
    package) so a number is accepted only when it is actually valid for the
    chosen country code — e.g. a 10-digit number under +971 (which expects 9) is
    rejected, fixing the "country code change isn't validated" bug at the source.

    The widget submits a phone in E.164 (e.g. ``"+12025550123"``; older clients
    sent ``"+1 2025550123"``) which ``phonenumbers.parse(raw, None)`` reads
    directly via the + prefix. Values without a country-code prefix can't be
    anchored to a region reliably, so they're skipped here (logged). If the
    library is unavailable this is a no-op (logged) — never blocks submission
    spuriously.
    """
    errors: list[dict[str, str]] = []
    try:
        import phonenumbers  # declared in requirements.txt
    except Exception:
        logger.warning("form_phone_validation_skipped reason=phonenumbers_unavailable")
        return errors
    safe_fields = form_fields if isinstance(form_fields, list) else []
    safe_data = form_data if isinstance(form_data, dict) else {}
    for field in safe_fields:
        if not isinstance(field, dict):
            continue
        field_id = str(field.get("id") or "").strip()
        if not field_id:
            continue
        ftype = str(field.get("type") or "").strip().lower()
        hint = f"{field_id} {field.get('label') or ''}".lower()
        is_phone = (
            ftype in {"tel", "phone"}
            or "phone" in hint
            or "mobile" in hint
            or "contact number" in hint
        )
        if not is_phone:
            continue
        raw = str(safe_data.get(field_id) or "").strip()
        if not raw:
            # empty -> required-field check covers it.
            continue
        if "+" not in raw:
            # no country code -> we can't anchor a region here; defer to the
            # frontend check, but make the gap visible.
            logger.warning("form_phone_validation_skipped reason=no_country_code field_id=%s", field_id)
            continue
        try:
            parsed = phonenumbers.parse(raw, None)
            valid = phonenumbers.is_valid_number(parsed)
        except Exception:
            valid = False
        if not valid:
            errors.append(
                {
                    "field_id": field_id,
                    "message": "Please enter a valid phone number for the selected country code.",
                }
            )
    return errors


def _first_present_field_id(source: dict[str, Any], keys: tuple[str, ...]) -> str:
    if not isinstance(source, dict):
        return ""
    for key in keys:
        if key in source:
            return key
    return ""


def _validate_checkin_checkout_sequence(
    *,
    form_data: dict[str, Any],
) -> list[dict[str, str]]:
    safe_form_data = form_data if isinstance(form_data, dict) else {}
    check_in = _pick_first_parsed_date(safe_form_data, keys=_STAY_CHECKIN_KEYS)
    check_out = _pick_first_parsed_date(safe_form_data, keys=_STAY_CHECKOUT_KEYS)
    if check_in is None or check_out is None:
        return []
    if check_out > check_in:
        return []
    checkout_field_id = _first_present_field_id(safe_form_data, _STAY_CHECKOUT_KEYS) or "checkout_date"
    return [
        {
            "field_id": checkout_field_id,
            "message": "Check-out date must be after check-in date.",
        }
    ]


def _merge_validation_errors(*groups: list[dict[str, str]]) -> list[dict[str, str]]:
    merged: list[dict[str, str]] = []
    seen: set[str] = set()
    for group in groups:
        for row in group or []:
            if not isinstance(row, dict):
                continue
            field_id = str(row.get("field_id") or "").strip()
            if not field_id or field_id in seen:
                continue
            seen.add(field_id)
            merged.append(
                {
                    "field_id": field_id,
                    "message": str(row.get("message") or "Please correct this field.").strip(),
                }
            )
    return merged


@router.post("/form-validate")
async def validate_form(request: FormValidateRequest) -> dict[str, Any]:
    """
    Validate required form fields before allowing submission.
    """
    safe_fields = request.form_fields if isinstance(request.form_fields, list) else []
    safe_data = request.form_data if isinstance(request.form_data, dict) else {}
    errors = _merge_validation_errors(
        _validate_required_form_fields(
            form_fields=safe_fields,
            form_data=safe_data,
        ),
        _validate_checkin_checkout_sequence(form_data=safe_data),
        _validate_phone_fields(form_fields=safe_fields, form_data=safe_data),
    )
    return {"valid": len(errors) == 0, "errors": errors}


_FORM_CONFIRM_SYSTEM_PROMPT = """\
You are a friendly hotel assistant. A guest just submitted a service request and a ticket has been created.

Your output is a JSON object with TWO string fields. Both fields are REQUIRED — never return one without the other. Schema:

{
  "ack": "<sentences 1-2 here, optionally followed by the booking link line>",
  "closer": "<sentence 4 here>"
}

`ack` (2 sentences, no markdown, no bullets):
  Sentence 1 — Receipt acknowledgement: warmly CONFIRM that we have RECEIVED their details / request, weaving the relevant details naturally (use the property name if provided). It must clearly tell the guest their information has come through to us — e.g. "Thanks <name> — we've received your details for <request>." Do not say "thank you for providing" filler; make it a genuine confirmation of receipt.
  Sentence 2 — Next-step: state that our team will GET BACK TO them / REACH OUT shortly to confirm availability and finalize the details. Phrase it as "we'll get back to you" / "our team will reach out to you". Do NOT claim the booking is confirmed. Do NOT use celebratory finality ("see you soon", "looking forward to welcoming you", "your stay is booked", "have a wonderful stay"). It is a REQUEST awaiting staff follow-up — phrase it that way.
Post-ack BOOKING INVITATION (conditional, applies to `ack` only):
  - If a `Booking URL` is present in the user input AND the service is a ROOM BOOKING / room reservation / stay request (look at the service name to decide), append one more short, warm line at the end of `ack` inviting the guest to complete their booking using the button below. Example shapes:
      "If you'd like to lock in your stay right away, you can complete your booking using the button below."
      "And whenever you're ready, you can secure your stay directly using the button below."
  - NEVER paste the raw web address / URL into the text — a "Book on website" button is rendered separately below your message. Just refer to "the button below".
  - If the service is NOT a room booking (support, complaint, in-stay request, etc.), DO NOT mention any booking button.
  - If no `Booking URL` was provided, do not mention booking online at all.

`closer` (exactly ONE re-engagement question):
  - Look at the `Tenant is single-property` line in the user input — it decides how you phrase this.
  - If a property name was provided AND `Tenant is single-property: no` (the hotel has MULTIPLE properties): ask whether they'd like to explore more about that specific property (rooms, dining, things to do), look at another property, or if there's anything else. Example shape: "In the meantime, want to explore more about <Property> — rooms, dining, things to do — or are you considering another of our properties?"
  - If a property name was provided AND `Tenant is single-property: yes` (this hotel has only ONE property): ask ONLY whether they'd like to explore more about the hotel (rooms, dining, things to do) or if there's anything else. NEVER mention "another property", "other properties", or "choose a location" — there is only ONE. Example shape: "In the meantime, want to explore more about <Property> — rooms, dining, things to do — or is there anything else I can help you with?"
  - If no property name was provided: just "Is there anything else I can help you with?"
  - Always exactly ONE concise question — never multiple, never a paragraph.

Hard rules:
- Both `ack` and `closer` MUST be present and non-empty.
- Do NOT use markdown formatting in either field (no bold, no bullets, no headers).
- Do NOT list the fields back as a structured list — weave them naturally.
- Do NOT say "thank you for providing" or similar filler.
- Do NOT mention ticket IDs, reference numbers, or internal codes.
- Never imply the booking is confirmed/finalized — staff confirmation still pending.
- Return STRICT JSON only — no markdown fences, no preamble, no extra keys.\
"""


def _strip_confirmation_reference_mentions(text: str) -> str:
    """Remove stray ticket/reference phrases from guest-facing confirmation copy."""
    cleaned = str(text or "").strip()
    if not cleaned:
        return ""
    cleaned = re.sub(
        r"(?:^|[\s.])(?:your\s+)?reference\s*(?:is|:)\s*#?[A-Za-z0-9-]+\b\.?",
        ".",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"(?:^|[\s.])ticket\s*(?:id|reference)\s*(?:is|:)\s*#?[A-Za-z0-9-]+\b\.?",
        ".",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"\s+([,.;!?])", r"\1", cleaned)
    cleaned = re.sub(r"([.!?])\s*[.!?]+", r"\1", cleaned)
    return cleaned.strip()


async def _generate_confirmation_message(
    service_name: str,
    form_data: dict[str, Any],
    property_name: str = "",
    booking_url: str = "",
    single_property: bool = False,
) -> tuple[str, str]:
    """Generate post-ticket confirmation as TWO separate strings: (ack, closer).

    The frontend renders these as two distinct assistant bubbles — the ack
    (acknowledgement + pending wording) feels like the system
    confirming the request, and the closer (one re-engagement question) feels
    like the bot pivoting back to helping the guest. Splitting them avoids the
    wall-of-text feel of one combined message.

    Returns (ack_text, closer_text). closer_text may be empty when the LLM
    fails — caller falls back to a generic question in that case.
    """
    try:
        model = str(getattr(settings, "openai_model", None) or "gpt-4o-mini")

        details = "; ".join(
            f"{k.replace('_', ' ').title()}: {v}"
            for k, v in form_data.items()
            if str(v).strip()
        )

        property_line = (
            f"Active property: {property_name.strip()}\n"
            if str(property_name or "").strip()
            else ""
        )
        booking_url_line = (
            f"Booking URL: {booking_url.strip()}\n"
            if str(booking_url or "").strip()
            else ""
        )

        user_prompt = (
            f"Service: {service_name}\n"
            f"{property_line}"
            f"{booking_url_line}"
            f"Tenant is single-property: {'yes' if single_property else 'no'}\n"
            f"Guest provided details: {details}"
        )

        completion = await llm_client.raw_chat_completion(
            model=model,
            messages=[
                {"role": "system", "content": _FORM_CONFIRM_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.7,
            max_tokens=280,
            response_format={"type": "json_object"},
            trace_context={
                "component": "form_confirmation_message",
                "route": "/api/chat/form-submit",
            },
            purpose="Generate post-ticket confirmation message for the guest",
        )

        raw = str(completion.choices[0].message.content or "").strip()
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1] if len(parts) > 1 else raw
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        try:
            parsed = json.loads(raw) if raw else {}
        except Exception:
            parsed = {}
        ack_text = ""
        closer_text = ""
        if isinstance(parsed, dict):
            ack_text = _strip_confirmation_reference_mentions(
                str(parsed.get("ack") or "").strip()
            )
            closer_text = _strip_confirmation_reference_mentions(
                str(parsed.get("closer") or "").strip()
            )
        # ── THE ACKNOWLEDGEMENT IS THE HALF THAT MATTERS ─────────────────────
        # This returned as soon as EITHER half came back, so an LLM answer with
        # an empty `ack` and a filled `closer` left the guest with nothing but
        # "In the meantime, want to explore more about Sarovar Portico — rooms,
        # dining, things to do...". They had just submitted a booking form; the
        # ticket was raised correctly and they were told nothing about it at all.
        # The closer is the pleasantry, the ack is the answer - so a missing ack
        # falls through to the static one below instead of being shipped empty.
        if ack_text:
            return ack_text, closer_text
        if closer_text:
            logger.info("form-confirm: LLM returned a closer with no "
                        "acknowledgement — using the standard ack with it")
            _kept_closer = closer_text
    except Exception as exc:
        logger.warning("form-confirm: LLM message generation failed: %s", exc)

    # Fallback — static strings, still split into two
    ack_text = (
        f"Thanks — we've received your details for your {service_name} request."
        f" Our team will get back to you shortly to confirm availability and finalize the details."
    )
    if str(property_name or "").strip():
        if single_property:
            closer_text = (
                f"In the meantime, want to explore more about {property_name.strip()} "
                f"— rooms, dining, things to do — or is there anything else I can help you with?"
            )
        else:
            closer_text = (
                f"In the meantime, want to explore more about {property_name.strip()} "
                f"— rooms, dining, things to do — or are you considering another of our properties?"
            )
    else:
        closer_text = "Is there anything else I can help you with?"
    # A closer the LLM did write is kept; only the missing ack is filled in.
    try:
        if _kept_closer:
            closer_text = _kept_closer
    except NameError:
        pass
    return ack_text, closer_text


# Internal routing/scoping keys the widget submits alongside the real form
# fields. They are meaningless to operations staff and must NEVER be dumped into
# the ticket message (they leaked as "Active Property Ids: [...]; Selected
# Property Scope Id: ...; Service Id: ...; Entity Id: ..." noise).
_INTERNAL_TICKET_FIELD_KEYS: frozenset[str] = frozenset(
    {
        "active_property_ids",
        "active_property_names",
        "selected_property_scope_id",
        "selected_property_scope_ids",
        "selected_property_scope_name",
        "service_id",
        "service_name",
        "entity_id",
        "conversation_id",
        "conversationid",
        "session_id",
        "sessionid",
        "group_id",
        "widget_id",
    }
)


def _sanitize_form_ticket_fields(form_data: dict[str, Any]) -> list[tuple[str, str]]:
    if not isinstance(form_data, dict):
        return []
    pairs: list[tuple[str, str]] = []
    for raw_key, raw_value in form_data.items():
        key = str(raw_key or "").strip()
        if not key or key.startswith("_") or key.lower() in _INTERNAL_TICKET_FIELD_KEYS:
            continue
        value = str(raw_value or "").strip()
        if not value:
            continue
        label = key.replace("_", " ").strip().title()
        pairs.append((label, value))
    return pairs


# Field-name hints that mark a guest's own freeform description of their
# request (typically rendered as a "textarea" in the form). The text in such
# a field is the truest ticket description, so we promote it into the issue
# line instead of generic "<service> request" boilerplate.
_FREEFORM_FIELD_HINTS: tuple[str, ...] = (
    "description", "issue", "note", "detail", "message", "reason",
    "comment", "request", "concern", "problem", "query", "feedback",
    "remark", "complaint", "enquiry", "inquiry",
)


def _condense_to_two_lines(text: Any, *, max_chars: int = 240) -> str:
    """Collapse freeform text into a single-spaced, ~2-line headline.

    The ticketing dashboard renders the issue as a short headline, so we strip
    newlines, collapse runs of whitespace, and cap the length at a word
    boundary (adding an ellipsis when truncated).
    """
    raw = str(text or "").strip()
    if not raw:
        return ""
    collapsed = re.sub(r"\s+", " ", raw).strip()
    if len(collapsed) <= max_chars:
        return collapsed
    cut = collapsed[:max_chars].rstrip()
    space = cut.rfind(" ")
    if space > max_chars * 0.6:
        cut = cut[:space].rstrip()
    return f"{cut}…"


def _extract_guest_request_text(
    *,
    form_config: dict[str, Any],
    merged_form_data: dict[str, Any],
) -> str:
    """Return the guest's own freeform description from the submitted form.

    Prefers fields typed as "textarea" whose id/label looks descriptive
    (issue / description / details / notes …); falls back to scanning raw
    submitted keys by the same hints when the form has no field list.
    Returns "" when the form carries no real freeform answer.
    """
    if not isinstance(merged_form_data, dict):
        return ""
    fields = form_config.get("fields") if isinstance(form_config, dict) else None
    fields = fields if isinstance(fields, list) else []

    # (priority, value) — lower priority wins; longer text breaks ties.
    candidates: list[tuple[int, str]] = []
    seen_ids: set[str] = set()
    for field in fields:
        if not isinstance(field, dict):
            continue
        fid = str(field.get("id") or "").strip()
        if not fid:
            continue
        value = str(merged_form_data.get(fid) or "").strip()
        if not value:
            continue
        ftype = str(field.get("type") or "").strip().lower()
        blob = f"{fid} {field.get('label') or ''}".lower()
        hinted = any(hint in blob for hint in _FREEFORM_FIELD_HINTS)
        if ftype == "textarea" and hinted:
            candidates.append((0, value))
        elif ftype == "textarea" or hinted:
            candidates.append((1, value))
        else:
            continue
        seen_ids.add(fid)

    for key, raw_value in merged_form_data.items():
        k = str(key or "").strip()
        if not k or k.startswith("_") or k in seen_ids:
            continue
        if any(hint in k.lower() for hint in _FREEFORM_FIELD_HINTS):
            value = str(raw_value or "").strip()
            if value:
                candidates.append((2, value))

    if not candidates:
        return ""
    candidates.sort(key=lambda item: (item[0], -len(item[1])))
    best = candidates[0][1].strip()
    # Trivial confirmations ("yes", "ok") aren't a real description.
    if len(best) < 3:
        return ""
    return best


def _clean_field_label(label: Any) -> str:
    """A form label as it should read on a ticket.

    Labels carry guidance meant for the guest filling the form — "(e.g. Massage,
    Asian Therapy)", "(optional)" — which is noise to the staff member reading
    the request. Strip the parentheticals, keep the name of the field.
    """
    text = re.sub(r"\s*\([^)]*\)", "", str(label or "")).strip()
    return re.sub(r"\s+", " ", text).strip(" :-")


def _declared_form_pairs(
    form_config: dict[str, Any],
    merged_form_data: dict[str, Any],
) -> list[tuple[str, str]]:
    """The answers the guest actually gave, in the order the form asked for them.

    Keyed off the form's DECLARED fields — the exact set the guest saw — rather
    than the merged blob, which also carries internal context (guest_id, stay
    dates, scope ids). Falls back to the sanitized raw values for legacy forms
    that declare no field list.
    """
    fields = form_config.get("fields") if isinstance(form_config, dict) else None
    if not isinstance(fields, list) or not fields:
        # Legacy/dynamic form with no declared field list — the caller falls back
        # to its previous behaviour rather than guessing from the merged blob.
        return []
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for field in fields:
        if not isinstance(field, dict):
            continue
        fid = str(field.get("id") or "").strip()
        if not fid or fid in seen:
            continue
        seen.add(fid)
        value = str(merged_form_data.get(fid) or "").strip()
        if not value:
            continue
        label = _clean_field_label(field.get("label")) or fid.replace("_", " ").title()
        pairs.append((label, value))
    return pairs


def _build_form_ticket_description_fallback(
    *,
    service_name: str,
    service_purpose: str,
    property_name: str,
    trigger_label: str,
    trigger_value: str,
    merged_form_data: dict[str, Any],
    guest_request_text: str = "",
    form_config: dict[str, Any] | None = None,
) -> tuple[str, str]:
    declared_pairs = _declared_form_pairs(
        form_config if isinstance(form_config, dict) else {}, merged_form_data
    )
    detail_pairs = declared_pairs or _sanitize_form_ticket_fields(merged_form_data)
    detail_text = "; ".join(f"{k}: {v}" for k, v in detail_pairs)

    # THE GUEST'S WORDS STILL LEAD — THEY JUST NO LONGER ERASE THE REST.
    # One freeform field used to become the WHOLE issue line, and every other
    # answer was dropped. A spa booking for a Massage on 2026-08-20 at 17:00
    # reached the spa team as the single word "Massage": the optional "Any
    # Preferences" box matched a "notes" hint and replaced the summary instead of
    # joining it. Staff could not see WHEN the guest wanted to come, and the
    # cancel matcher — which is shown this line and little else — could not tell
    # the request was a spa booking at all.
    #
    # Leading with the guest's own description is right and stays (for a
    # "how can our team help?" form that text IS the request). What follows it now
    # is every OTHER answer they gave, in the order the form asked. Values already
    # contained in the lead are skipped, so nothing is said twice.
    guest_text = _condense_to_two_lines(guest_request_text)
    if declared_pairs:
        # A form whose only field is a free-text box IS the guest's description
        # ("how can our team help?"), so it reads as their own sentence. Any form
        # with more than one answer is listed field by field, in the order the
        # guest was asked — nothing is picked out as the important one and
        # nothing is dropped.
        body = (
            declared_pairs[0][1]
            if len(declared_pairs) == 1
            else "; ".join(f"{label}: {value}" for label, value in declared_pairs)
        )
        # Name the service first, every time. Which service a request belongs to
        # was previously only implied — "Massage" said nothing about a spa — and
        # this line is what staff read in a queue and what the cancel matcher
        # reads to work out which request a guest means.
        issue = f"{service_name}: {body}" if service_name else body
        issue = _condense_to_two_lines(issue, max_chars=240)
    elif guest_text:
        # No declared fields to read (legacy form) — the guest's own words, as before.
        issue = guest_text[:240].strip()
    else:
        issue = f"{service_name} request"
        if trigger_value:
            issue = f"{issue} ({trigger_value})"
        if property_name:
            issue = f"{issue} at {property_name}"
        issue = issue[:180].strip()

    # Headline: avoid the doubled "Support Request request" when the service
    # name already ends in "request".
    _name = str(service_name or "").strip()
    if not _name:
        headline = "Guest submitted a request."
    elif _name.lower().endswith("request"):
        headline = f"Guest submitted a {_name}."
    else:
        headline = f"Guest submitted a {_name} request."

    message_parts: list[str] = [headline]
    if property_name:
        message_parts.append(f"Property: {property_name}.")
    # NOTE: service_purpose (service.description) is the LLM router's trigger
    # instruction for this deployment ("Use this form ONLY when..."), NOT a
    # guest-facing purpose. It must never be echoed into the ticket message.
    if trigger_label and trigger_value:
        message_parts.append(f"Selected {trigger_label}: {trigger_value}.")
    if detail_text:
        message_parts.append(f"Provided details: {detail_text[:850].strip()}.")
    message_parts.append("Please review and action this request.")
    message = " ".join(part for part in message_parts if part).strip()[:1200]
    return issue, message


async def _generate_form_ticket_description(
    *,
    service: dict[str, Any],
    service_name: str,
    service_id: str,
    merged_form_data: dict[str, Any],
    req_meta: dict[str, Any],
    context: Any,
) -> tuple[str, str]:
    form_config = service.get("form_config") if isinstance(service, dict) else {}
    form_config = form_config if isinstance(form_config, dict) else {}
    trigger_field = form_config.get("trigger_field") if isinstance(form_config, dict) else {}
    trigger_field = trigger_field if isinstance(trigger_field, dict) else {}

    trigger_id = str(trigger_field.get("id") or "").strip()
    trigger_label = str(trigger_field.get("label") or "").strip()
    if not trigger_label and trigger_id:
        trigger_label = trigger_id.replace("_", " ").title()

    trigger_value = ""
    if trigger_id:
        try:
            from services.form_mode_service import resolve_trigger_field_value

            _matched_key, matched_value = resolve_trigger_field_value(
                merged_form_data,
                trigger_id,
                trigger_label,
            )
            trigger_value = str(matched_value or "").strip()
        except Exception:
            trigger_value = str(merged_form_data.get(trigger_id) or "").strip()

    context_pending = context.pending_data if isinstance(getattr(context, "pending_data", None), dict) else {}
    property_name = _first_non_empty_text(
        req_meta.get("booking_property_name"),
        req_meta.get("property_name"),
        req_meta.get("hotel_name"),
        merged_form_data.get("property_name"),
        merged_form_data.get("property"),
        getattr(context, "booking_property_name", None),
        context_pending.get("_selected_property_scope_name"),
        context_pending.get("_stay_property_name"),
    )

    service_purpose = _first_non_empty_text(
        service.get("description"),
        service.get("ticketing_conditions"),
        service.get("ticketing_policy"),
    )

    guest_request_text = _extract_guest_request_text(
        form_config=form_config,
        merged_form_data=merged_form_data,
    )

    fallback_issue, fallback_message = _build_form_ticket_description_fallback(
        service_name=service_name,
        service_purpose=service_purpose,
        property_name=property_name,
        trigger_label=trigger_label,
        trigger_value=trigger_value,
        merged_form_data=merged_form_data,
        guest_request_text=guest_request_text,
        form_config=form_config,
    )

    if not str(getattr(settings, "openai_api_key", "") or "").strip():
        return fallback_issue, fallback_message

    # The LLM polish here was adding 5-20s to every form submission. The
    # deterministic fallback already produces a perfectly usable issue +
    # message from the form fields. Operations staff get the same content,
    # just slightly less prosey. Default to fallback; the LLM polish stays
    # available behind a setting for hotels that want the prettier wording
    # and accept the latency cost. Even when enabled we cap the LLM call
    # at a hard timeout — a slow OpenAI response can never block the form.
    if not bool(getattr(settings, "chat_form_ticket_llm_polish_enabled", False)):
        return fallback_issue, fallback_message

    llm_payload = {
        "service": {
            "id": str(service_id or "").strip(),
            "name": str(service_name or "").strip(),
            "purpose": str(service_purpose or "").strip(),
        },
        "property_name": str(property_name or "").strip(),
        "guest_request": str(guest_request_text or "").strip(),
        "trigger": {
            "field_id": str(trigger_id or "").strip(),
            "field_label": str(trigger_label or "").strip(),
            "value": str(trigger_value or "").strip(),
        },
        "form_values": [{k: v} for k, v in _sanitize_form_ticket_fields(merged_form_data)],
        "instructions": {
            "issue_max_chars": 180,
            "message_max_chars": 1200,
            "style": "internal staff note, concise, operationally actionable",
        },
    }
    system_prompt = (
        "You write internal ticket text for hotel operations staff.\n"
        "Return STRICT JSON only in this exact shape:\n"
        "{\"issue\":\"...\",\"message\":\"...\"}\n"
        "Rules:\n"
        "1) issue: a concise summary (max 2 lines, under 180 chars) of what the guest actually needs.\n"
        "   When guest_request is present, base the issue on it (summarise the guest's own words);\n"
        "   only fall back to the service name when guest_request is empty.\n"
        "2) message: concise actionable description under 1200 chars.\n"
        "3) Include what the request is for, property context when present, selected trigger option when present,\n"
        "   and only the relevant collected values.\n"
        "4) Do not use markdown, bullet points, or placeholders.\n"
        "5) Do not invent details.\n"
    )

    polish_timeout_seconds = max(
        1.0,
        float(getattr(settings, "chat_form_ticket_llm_polish_timeout_ms", 4000) or 4000) / 1000.0,
    )

    try:
        result = await asyncio.wait_for(
            llm_client.chat_with_json(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(llm_payload, ensure_ascii=False)},
                ],
                temperature=0.1,
            ),
            timeout=polish_timeout_seconds,
        )
        if isinstance(result, dict):
            issue = str(result.get("issue") or "").strip()
            message = str(result.get("message") or "").strip()
            if issue and message:
                return issue[:180], message[:1200]
    except asyncio.TimeoutError:
        logger.warning(
            "form-ticket-description: LLM polish exceeded %.1fs — falling back to deterministic description",
            polish_timeout_seconds,
        )
    except Exception as exc:
        logger.warning("form-ticket-description: LLM generation failed: %s", exc)

    return fallback_issue, fallback_message


def _first_non_empty_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _normalize_phone_text(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return ""
    if raw.startswith("+"):
        return f"+{digits}"
    return digits


def _parse_iso_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


_STAY_CHECKIN_KEYS: tuple[str, ...] = (
    "stay_checkin_date",
    "check_in",
    "checkin",
    "check_in_date",
    "checkin_date",
    "booking_check_in_date",
)
_STAY_CHECKOUT_KEYS: tuple[str, ...] = (
    "stay_checkout_date",
    "check_out",
    "checkout",
    "check_out_date",
    "checkout_date",
    "booking_check_out_date",
)
_SERVICE_DATE_FIELD_HINT = re.compile(
    r"(date|check[ _-]?in|check[ _-]?out|arrival|departure|event|appointment|pickup|dropoff|drop)",
    re.IGNORECASE,
)
_SERVICE_DATE_EXCLUDED_KEYS: set[str] = {
    *_STAY_CHECKIN_KEYS,
    *_STAY_CHECKOUT_KEYS,
    "created_at",
    "updated_at",
    "booking_id",
}
_VALIDATION_CRITICAL_FIELD_HINT = re.compile(
    r"(phone|mobile|contact|email|date|check[ _-]?in|check[ _-]?out|arrival|departure|event)",
    re.IGNORECASE,
)


def _parse_flexible_date(value: Any) -> date | None:
    """Parse YYYY-MM-DD dates, including datetime-like strings containing a date token."""
    direct = _parse_iso_date(value)
    if direct is not None:
        return direct

    text = str(value or "").strip()
    if not text:
        return None

    # Accept common human-entered formats like DD-MM-YYYY / DD/MM/YYYY.
    dmy_match = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b", text)
    if dmy_match:
        try:
            day = int(dmy_match.group(1))
            month = int(dmy_match.group(2))
            year = int(dmy_match.group(3))
            return date(year, month, day)
        except ValueError:
            return None

    token_match = re.search(r"\b\d{4}-\d{2}-\d{2}\b", text)
    if not token_match:
        return None
    return _parse_iso_date(token_match.group(0))


def _pick_first_parsed_date(*sources: Any, keys: tuple[str, ...]) -> date | None:
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in keys:
            parsed = _parse_flexible_date(source.get(key))
            if parsed is not None:
                return parsed
    return None


def _looks_like_service_date_field(field_id: str, label: str = "", field_type: str = "") -> bool:
    ftype = str(field_type or "").strip().lower()
    if ftype in {"date", "datetime", "datetime-local"}:
        return True
    # If the field has an explicit non-date type, trust it â€” do NOT override
    # with keyword heuristics. This prevents time, text, and other fields from
    # being misclassified as date fields just because their id/label contains
    # keywords like "arrival", "departure", "event", etc.
    if ftype and ftype not in {"date", "datetime", "datetime-local"}:
        return False
    joined = f"{field_id} {label}".strip()
    if not joined:
        return False
    return bool(_SERVICE_DATE_FIELD_HINT.search(joined))


def _collect_service_date_field_ids(
    *,
    form_fields: list[dict[str, Any]],
    merged_form_data: dict[str, Any],
) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()

    def _add(field_id: str) -> None:
        normalized = str(field_id or "").strip()
        if not normalized:
            return
        if normalized.startswith("_"):
            return
        if normalized.lower() in _SERVICE_DATE_EXCLUDED_KEYS:
            return
        if normalized in seen:
            return
        seen.add(normalized)
        ids.append(normalized)

    for field in form_fields or []:
        if not isinstance(field, dict):
            continue
        field_id = str(field.get("id") or "").strip()
        if not field_id:
            continue
        if _looks_like_service_date_field(
            field_id=field_id,
            label=str(field.get("label") or ""),
            field_type=str(field.get("type") or ""),
        ):
            _add(field_id)

    # Build a lookup of field_id -> field_type from form_fields so the
    # keyword heuristic below can respect explicit non-date types.
    _field_type_by_id: dict[str, str] = {}
    _declared_field_ids: set[str] = set()
    for field in form_fields or []:
        if isinstance(field, dict):
            _fid = str(field.get("id") or "").strip()
            _ftype = str(field.get("type") or "").strip()
            if _fid:
                _declared_field_ids.add(_fid)
            if _fid and _ftype:
                _field_type_by_id[_fid] = _ftype

    for key, value in (merged_form_data or {}).items():
        field_id = str(key or "").strip()
        if not field_id:
            continue
        if field_id.startswith("_"):
            continue
        if field_id.lower() in _SERVICE_DATE_EXCLUDED_KEYS:
            continue
        if not str(value or "").strip():
            continue
        # When a form schema exists, validate only declared field IDs.
        # This avoids false positives from alias/mismatch keys in pending data
        # (for example "arrival_time" when the configured field is "arrival"
        # with type=time).
        if _declared_field_ids and field_id not in _declared_field_ids:
            continue
        if not _looks_like_service_date_field(
            field_id=field_id,
            field_type=_field_type_by_id.get(field_id, ""),
        ):
            continue
        _add(field_id)

    return ids

def _validate_service_date_boundaries(
    *,
    phase_id: str,
    form_fields: list[dict[str, Any]],
    merged_form_data: dict[str, Any],
    req_meta: dict[str, Any],
    context: Any = None,
) -> list[dict[str, str]]:
    phase = _normalize_phase_identifier(phase_id)
    if phase not in {"pre_checkin", "during_stay", "post_checkout"}:
        return []

    safe_form_data = merged_form_data if isinstance(merged_form_data, dict) else {}
    safe_meta = req_meta if isinstance(req_meta, dict) else {}
    pending = getattr(context, "pending_data", None) if context is not None else None
    safe_pending = pending if isinstance(pending, dict) else {}

    booking_context: dict[str, Any] = {}
    if context is not None:
        booking_context = {
            "booking_check_in_date": getattr(context, "booking_check_in_date", None),
            "booking_check_out_date": getattr(context, "booking_check_out_date", None),
        }

    stay_check_in = _pick_first_parsed_date(
        safe_meta,
        safe_form_data,
        safe_pending,
        booking_context,
        keys=_STAY_CHECKIN_KEYS,
    )
    stay_check_out = _pick_first_parsed_date(
        safe_meta,
        safe_form_data,
        safe_pending,
        booking_context,
        keys=_STAY_CHECKOUT_KEYS,
    )

    date_field_ids = _collect_service_date_field_ids(
        form_fields=form_fields,
        merged_form_data=safe_form_data,
    )
    if not date_field_ids:
        return []

    # Fail closed: for in-stay/post-checkout phases we must know booking dates
    # before accepting any service date.
    if stay_check_in is None or stay_check_out is None:
        first_id = date_field_ids[0]
        return [{
            "field_id": first_id,
            "message": "We could not verify your stay dates. Please refresh and try again.",
        }]

    if stay_check_out < stay_check_in:
        stay_check_in, stay_check_out = stay_check_out, stay_check_in

    field_label_by_id: dict[str, str] = {}
    for field in form_fields or []:
        if not isinstance(field, dict):
            continue
        field_id = str(field.get("id") or "").strip()
        if not field_id:
            continue
        label = str(field.get("label") or "").strip()
        if label:
            field_label_by_id[field_id] = label

    errors: list[dict[str, str]] = []
    for field_id in date_field_ids:
        raw_value = safe_form_data.get(field_id)
        text_value = str(raw_value or "").strip()
        if not text_value:
            continue

        parsed_value = _parse_flexible_date(raw_value)
        if parsed_value is None:
            label = field_label_by_id.get(field_id) or field_id.replace("_", " ").title()
            errors.append({
                "field_id": field_id,
                "message": f"Please enter a valid date for {label} (YYYY-MM-DD).",
            })
            continue

        if phase in {"pre_checkin", "during_stay"}:
            if parsed_value < stay_check_in or parsed_value > stay_check_out:
                errors.append({
                    "field_id": field_id,
                    "message": (
                        f"Please select a date within your stay "
                        f"({stay_check_in.isoformat()} to {stay_check_out.isoformat()})."
                    ),
                })
        elif phase == "post_checkout" and parsed_value < stay_check_out:
            errors.append({
                "field_id": field_id,
                "message": (
                    "Please select a date on or after your check-out date "
                    f"({stay_check_out.isoformat()})."
                ),
            })

    return errors


def _build_fail_closed_validation_errors(
    *,
    form_fields: list[dict[str, Any]],
    form_data: dict[str, Any],
) -> list[dict[str, str]]:
    safe_fields = form_fields if isinstance(form_fields, list) else []
    safe_data = form_data if isinstance(form_data, dict) else {}
    critical_ids: list[str] = []
    seen: set[str] = set()

    for field in safe_fields:
        if not isinstance(field, dict):
            continue
        field_id = str(field.get("id") or "").strip()
        if not field_id:
            continue
        if field_id in seen:
            continue
        field_type = str(field.get("type") or "").strip().lower()
        label = str(field.get("label") or "").strip()
        required = bool(field.get("required"))
        has_value = bool(str(safe_data.get(field_id) or "").strip())
        looks_critical = (
            field_type in {"date", "datetime", "datetime-local", "tel", "phone", "email", "number"}
            or bool(_VALIDATION_CRITICAL_FIELD_HINT.search(f"{field_id} {label}".strip()))
            or required
        )
        if looks_critical and has_value:
            seen.add(field_id)
            critical_ids.append(field_id)

    if not critical_ids:
        fallback = ""
        for field in safe_fields:
            if not isinstance(field, dict):
                continue
            candidate = str(field.get("id") or "").strip()
            if candidate:
                fallback = candidate
                break
        if fallback:
            critical_ids.append(fallback)

    return [
        {
            "field_id": field_id,
            "message": "Validation is temporarily unavailable. Please try again.",
        }
        for field_id in critical_ids
    ]


def _coerce_positive_int(value: Any, *, default: int = 1, maximum: int = 20) -> int:
    try:
        parsed = int(str(value or "").strip())
    except (TypeError, ValueError):
        return default
    if parsed <= 0:
        return default
    return min(parsed, maximum)


def _extract_prebooking_booking_payload(
    *,
    service_name: str,
    form_data: dict[str, Any],
    req_meta: dict[str, Any],
    context: Any,
) -> dict[str, Any]:
    form = form_data if isinstance(form_data, dict) else {}
    meta = req_meta if isinstance(req_meta, dict) else {}
    pending = (
        context.pending_data
        if getattr(context, "pending_data", None) is not None and isinstance(context.pending_data, dict)
        else {}
    )

    # Only an actual ROOM / STAY booking form may persist a booking record — which
    # advances the guest to pre_checkin and PINS the stay property. A support /
    # venue / callback / lead form must NOT: submitting one shouldn't lock the guest
    # to a property or flip the phase, otherwise a later "book a room at <other
    # city>" gets the booked property merged back in, routes to the main answerer
    # instead of the room agent (so no ticket + cross-property answers), and room
    # bookings get wrongly declined as "available once you're checked in". Zone books
    # rooms on the website with no form, so this correctly never fires for Zone.
    _svc = str(service_name or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not (("room" in _svc and "book" in _svc) or _svc in {"room_booking_request", "room_stay_booking", "stay_booking", "booking"}):
        return {}

    phase_hint = _normalize_phase_identifier(
        _first_non_empty_text(
            meta.get("phase"),
            meta.get("phase_id"),
            meta.get("booking_phase"),
        )
    )
    flow_hint = str(meta.get("flow") or "").strip().lower()
    if phase_hint and phase_hint != "pre_booking":
        return {}
    if not phase_hint and flow_hint not in {"booking", "booking_bot", "engage"}:
        return {}

    def _pick(*keys: str) -> str:
        for key in keys:
            if key in form and str(form.get(key) or "").strip():
                return str(form.get(key) or "").strip()
        for key in keys:
            if key in meta and str(meta.get(key) or "").strip():
                return str(meta.get(key) or "").strip()
        return ""

    guest_phone = _normalize_phone_text(
        _first_non_empty_text(
            _pick(
                "guest_phone",
                "phone",
                "phone_number",
                "contact_number",
                "mobile",
                "booking_guest_phone",
                "wa_number",
                "waNumber",
            ),
            getattr(context, "guest_phone", None),
        )
    )
    if not guest_phone:
        user_id_as_phone = _normalize_phone_text(meta.get("user_id"))
        if len("".join(ch for ch in user_id_as_phone if ch.isdigit())) >= 10:
            guest_phone = user_id_as_phone
    if not guest_phone:
        return {}

    check_in = _parse_iso_date(
        _pick(
            "check_in",
            "checkin",
            "check_in_date",
            "checkin_date",
            "arrival_date",
            "booking_check_in_date",
            "stay_checkin_date",
        )
    )
    check_out = _parse_iso_date(
        _pick(
            "check_out",
            "checkout",
            "check_out_date",
            "checkout_date",
            "departure_date",
            "booking_check_out_date",
            "stay_checkout_date",
        )
    )
    if check_in and not check_out:
        check_out = check_in + timedelta(days=1)
    elif check_out and not check_in:
        check_in = check_out - timedelta(days=1)
    elif check_in and check_out and check_out <= check_in:
        check_out = check_in + timedelta(days=1)
    if check_in is None or check_out is None:
        return {}

    guest_name = _first_non_empty_text(
        _pick("guest_name", "full_name", "name", "user_name", "booking_guest_name"),
        getattr(context, "guest_name", None),
    )
    property_name = _first_non_empty_text(
        _pick(
            "property_name",
            "property",
            "hotel_name",
            "hotel",
            "destination",
            "booking_property_name",
        ),
        getattr(context, "booking_property_name", None),
    )
    # Fallback: use property scope set during multi-property selection
    if not property_name:
        property_name = _first_non_empty_text(
            meta.get("_selected_property_scope_name"),
            form.get("_selected_property_scope_name"),
        )
    if not property_name:
        _active_names = meta.get("_active_property_names") or form.get("_active_property_names")
        if isinstance(_active_names, list) and len(_active_names) == 1:
            property_name = str(_active_names[0]).strip()
    property_scope_id = _first_non_empty_text(
        _pick(
            "property_scope_id",
            "property_id",
            "booking_property_scope_id",
            "booking_property_id",
        ),
        getattr(context, "booking_property_scope_id", None),
        pending.get("_selected_property_scope_id"),
        pending.get("_stay_property_id"),
    )
    if not property_scope_id:
        active_scope_ids = meta.get("_active_property_ids") or form.get("_active_property_ids")
        if not isinstance(active_scope_ids, list):
            active_scope_ids = pending.get("_active_property_ids")
        if isinstance(active_scope_ids, list) and len(active_scope_ids) == 1:
            property_scope_id = str(active_scope_ids[0]).strip()
    room_number = _pick("room_number", "room_no", "booking_room_number")
    room_type = _pick("room_type", "room_category", "booking_room_type")
    guests_count = _coerce_positive_int(
        _pick("num_guests", "guests", "guest_count", "party_size", "adults"),
        default=1,
        maximum=20,
    )
    status = str(_pick("status", "booking_status") or "reserved").strip().lower()
    if status not in {"reserved", "confirmed", "checked_in", "checked_out", "cancelled"}:
        status = "reserved"
    special_requests = _pick("special_requests", "notes", "requirements", "request_notes")
    source_channel = _first_non_empty_text(meta.get("channel"), getattr(context, "channel", None), "web")

    return {
        "guest_phone": guest_phone,
        "guest_name": guest_name or None,
        "property_name": property_name or None,
        "property_scope_id": property_scope_id or None,
        "room_number": room_number or None,
        "room_type": room_type or None,
        "check_in_date": check_in,
        "check_out_date": check_out,
        "num_guests": guests_count,
        "status": status,
        "source_channel": source_channel,
        "special_requests": special_requests or None,
        "derived_phase_hint": phase_hint or "pre_booking",
        "service_name": str(service_name or "").strip(),
    }


async def _persist_prebooking_booking_if_possible(
    *,
    hotel_code: str,
    service_name: str,
    form_data: dict[str, Any],
    req_meta: dict[str, Any],
    context: Any,
    db_session: AsyncSession,
) -> dict[str, Any]:
    payload = _extract_prebooking_booking_payload(
        service_name=service_name,
        form_data=form_data,
        req_meta=req_meta,
        context=context,
    )
    if not payload:
        return {}

    try:
        from services.booking_service import create_booking

        created = await create_booking(
            hotel_code=hotel_code,
            guest_phone=str(payload.get("guest_phone") or "").strip(),
            guest_name=str(payload.get("guest_name") or "").strip() or None,
            property_name=str(payload.get("property_name") or "").strip() or None,
            property_scope_id=str(payload.get("property_scope_id") or "").strip() or None,
            room_number=str(payload.get("room_number") or "").strip() or None,
            room_type=str(payload.get("room_type") or "").strip() or None,
            check_in_date=payload.get("check_in_date"),
            check_out_date=payload.get("check_out_date"),
            num_guests=int(payload.get("num_guests") or 1),
            status=str(payload.get("status") or "reserved").strip() or "reserved",
            source_channel=str(payload.get("source_channel") or "web").strip() or "web",
            special_requests=str(payload.get("special_requests") or "").strip() or None,
            db_session=db_session,
        )
        return created if isinstance(created, dict) else {}
    except Exception as exc:
        logger.warning(
            "pre-booking auto-persist skipped due to error (session_id=%s): %s",
            str(getattr(context, "session_id", "") or "").strip(),
            str(exc),
        )
        return {}


@router.post("/booking-assistance")
async def submit_booking_assistance(
    payload: dict[str, Any] = Body(...),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    # Serialize with both HTTP and WebSocket chat turns so an older response
    # cannot overwrite the ledger as the form completes.
    async with context_manager.session_lock(str(payload.get("session_id") or "")):
        return await _submit_booking_assistance_locked(payload, db)


async def _submit_booking_assistance_locked(
    payload: dict[str, Any], db: AsyncSession,
) -> dict[str, Any]:
    """Attach the guest's booking details to the EXISTING room-booking ticket.

    Flow: the room-booking turn already renders "Book on website"; next to it the
    widget offers help from the team. That opens a small form (dates, guests, a
    note). Submitting it lands here.

    We deliberately UPDATE rather than create: the room-booking flow already
    raised a silent ticket for this stay (stamped with `property_name` so it can
    be found again), so raising a second one would give staff two rows for one
    guest. Only `description` and `message` are rewritten — no note is appended.

    Falls back to creating a ticket ONLY when no booking ticket exists for the
    session yet (e.g. the guest opened the form before any booking intent was
    recorded), so the request is never silently dropped.
    """
    from services.ticketing_service import ticketing_service
    from services.config_service import config_service

    session_id = str(payload.get("session_id") or "").strip()
    hotel_code = config_service.resolve_hotel_code(str(payload.get("hotel_code") or "").strip())
    check_in = str(payload.get("check_in") or "").strip()
    check_out = str(payload.get("check_out") or "").strip()
    guests = str(payload.get("guests") or "").strip()
    guest_note = str(payload.get("message") or "").strip()
    property_name = str(payload.get("property_name") or "").strip()

    if not session_id:
        return {"success": False, "message": "session_id is required"}

    # Staff have to phone this guest back, so the ticket must carry the stay
    # details AND the contact — previously it landed with neither.
    guest_name = str(payload.get("guest_name") or "").strip()
    guest_phone = str(payload.get("guest_phone") or "").strip()
    guest_email = str(payload.get("guest_email") or "").strip()

    detail_lines = [
        f"Property: {property_name}" if property_name else "",
        f"Check-in: {check_in}" if check_in else "",
        f"Check-out: {check_out}" if check_out else "",
        f"Guests: {guests}" if guests else "",
        f"Guest name: {guest_name}" if guest_name else "",
        f"Phone: {guest_phone}" if guest_phone else "",
        f"Email: {guest_email}" if guest_email else "",
        f"Guest note: {guest_note}" if guest_note else "",
    ]
    details = "\n".join(line for line in detail_lines if line)
    where = f" — {property_name}" if property_name else ""
    description = (
        f"BOOKING ASSISTANCE REQUESTED{where}\n"
        f"Guest asked the team to call back and help complete this booking.\n"
        f"{details}"
    ).strip()

    # THE STAY THE GUEST ASKED FOR, ON THE `issue` LINE ITSELF.
    # The ticket summariser writes the staff-facing description from `issue`
    # ALONE (it arrives as `request` in its context, and its prompt says to use
    # nothing else). Anything that exists only in `description` is invisible to
    # it, so it wrote a generic line and the guest's dates and party size never
    # reached the dashboard — ticket 14243 landed as "Assist guest with booking
    # at Mango Grove Hotel, Chandigarh." with nothing for the person making the
    # callback. Giving the summariser the facts lets it phrase the line itself
    # rather than us pinning fixed text over its output.
    stay_bits = [
        f"check-in {check_in}" if check_in else "",
        f"check-out {check_out}" if check_out else "",
        f"party of {guests}" if guests else "",
    ]
    stay_clause = ", ".join(bit for bit in stay_bits if bit)
    issue_line = f"Booking assistance requested{where}"
    if stay_clause:
        issue_line += f" ({stay_clause})"
    if guest_note:
        issue_line += f". Guest note: {guest_note[:160]}"

    scope_token = db_config_service.set_hotel_context(hotel_code) if hotel_code else None
    try:
        form_context = await context_manager.get_or_create_context(
            session_id=session_id, hotel_code=hotel_code, db_session=db,
        )
        conversation_id = str((payload.get("metadata") or {}).get("conversation_id") or "").strip()
        if conversation_id:
            conversation_memory_service.reset_pending_for_conversation_rotation(form_context, conversation_id)
            form_context.pending_data["_active_conversation_uid"] = conversation_id
        summary_payload = {
            "hotel_code": hotel_code, "issue": issue_line, "property_name": property_name,
            "category": "room_booking", "phase": "Pre Booking",
        }
        if team_booking_enabled(hotel_code):
            summary_payload["_booking_facts"] = {
                "check_in": check_in, "check_out": check_out, "guests": guests,
            }
        ticket_ref = await ticketing_service.find_session_booking_ticket_ref(
            session_id=session_id, property_name=property_name
        )
        if ticket_ref:
            if team_booking_enabled(hotel_code):
                summarized = await ticketing_service._apply_kepsla_description(summary_payload)
                description = str(summarized.get("_kepsla_description") or issue_line)
            result = await ticketing_service.update_ticket_details(
                session_id=session_id,
                ticket_ref=ticket_ref,
                description=description,
                message=details or description,
                # The external API can ignore issue edits; keep the updated
                # actionable request visible in its supported notes field too.
                note=description if team_booking_enabled(hotel_code) else "",
            )
            updated = bool(getattr(result, "success", False))
        else:
            updated = False

        if not updated:
            # No existing booking ticket to enrich — raise one so the request
            # still reaches the team.
            context = await context_manager.get_or_create_context(
                session_id=session_id, hotel_code=hotel_code, db_session=None
            )
            create_payload = ticketing_service.build_lumira_ticket_payload(
                context=context,
                issue=issue_line,
                message=details or description,
                category="room_booking",
                sub_category="room_booking",
                priority="LOW",
                phase="Pre Booking",
                source="booking_assistance_form",
            )
            create_payload["service_id"] = "room_booking_request"
            create_payload["description"] = description
            if team_booking_enabled(hotel_code):
                create_payload["_booking_facts"] = summary_payload["_booking_facts"]
            if property_name:
                create_payload["property_name"] = property_name
            create_payload["room_number"] = ""
            created = await ticketing_service.create_ticket(create_payload)
            if not getattr(created, "success", False):
                raise RuntimeError("Booking request was not accepted by ticketing")
            ticket_ref = str(getattr(created, "ticket_id", "") or "").strip()

        # TELL THE CONVERSATION WHAT JUST HAPPENED. The ticket is raised here,
        # but nothing wrote the booking back into the guest's context — so the
        # agent had no idea a request existed and answered the next "I'd like to
        # book a room" by pitching the same form again. `_rb_bookings` is the
        # ledger the whole room-booking flow already reads: the repeat-booking
        # reply, the date-change path that updates THIS ticket instead of
        # raising a second one, and the property-switch prompt all key off it.
        # Recording the fact here is what lets the agent answer for itself.
        try:
            _ctx = form_context
            if _ctx is not None:
                if not isinstance(getattr(_ctx, "pending_data", None), dict):
                    _ctx.pending_data = {}
                _pd = _ctx.pending_data
                _books = list(_pd.get("_rb_bookings") or [])
                _key = (property_name or "").strip().lower()
                _match = next(
                    (b for b in _books if str(b.get("property") or "").strip().lower() == _key),
                    None,
                )
                _record = {
                    "property": property_name,
                    "check_in": check_in,
                    "check_out": check_out,
                    "guests": guests,
                    "ticket_ref": ticket_ref,
                    "details_submitted": True,
                    "conversation_id": conversation_id,
                }
                if _match is not None:
                    _match.update({k: v for k, v in _record.items() if v})
                else:
                    _books.append(_record)
                _pd["_rb_bookings"] = _books
                # db_session is REQUIRED here: save_context only writes to the
                # database when one is supplied, and the next turn loads the
                # context from the database. Saving to the in-memory cache alone
                # meant the booking we had just recorded was overwritten before
                # the guest's very next message could see it — the ticket was
                # raised, the log said "recorded", and the prompt still arrived
                # with no booking in it.
                await context_manager.save_context(_ctx, db_session=db, force_persist=True)
                logger.info(
                    "booking-assistance: recorded booking in context session=%s property=%r dates=%r-%r ticket=%s",
                    session_id, property_name, check_in, check_out, ticket_ref or "-",
                )
        except Exception as _rec_exc:
            logger.warning("booking-assistance: could not record the booking in context: %r", _rec_exc)
            raise

        # THROW AWAY EVERY CHIP ANSWER COOKED BEFORE THIS SUBMISSION. Those were
        # written by a prefetch worker that, at the time, correctly believed the
        # guest had not yet sent their details, so they end with "Tap Share your
        # booking details below". The moment the form is submitted the serve
        # path starts withholding that button (`details_submitted`), and a
        # cached answer still naming it strands the guest on a dead end — seen
        # live on four consecutive turns. Carrying the ledger into the batch
        # fixes every batch built from here on; this fixes the ones already
        # sitting in the cache. Best-effort: a failure here only costs a slower
        # next turn.
        try:
            await suggestion_prefetch_service.cancel_session_prefetch(
                session_id=session_id,
                reason="booking_details_submitted",
            )
        except Exception:
            logger.debug("booking-assistance: prefetch cancel skipped", exc_info=True)

        logger.info(
            "booking-assistance: session=%s property=%r ticket=%s mode=%s",
            session_id, property_name, ticket_ref or "-", "update" if updated else "create",
        )
        return {
            "success": True,
            "ticket_id": ticket_ref,
            "message": (
                # Team-only hotels have no booking engine, so the guest never
                # completes the booking themselves — say the team confirms it.
                "Thank you — I've shared your details with our team. "
                "They will get back to you shortly to confirm your booking."
                if _booking_team_only_for_hotel(hotel_code)
                else "Thanks — I've passed your details to our team. "
                "They'll call you shortly to help with the booking and the best available rates."
            ),
        }
    except Exception as exc:
        logger.exception("booking-assistance failed session=%s: %s", session_id, exc)
        return {"success": False, "message": "Sorry, that didn't go through. Please try again."}
    finally:
        if scope_token is not None:
            db_config_service.reset_hotel_context(scope_token)


@router.post("/form-submit")
@router.post("/submit-form")
@router.post("/form_submit")
async def submit_form(
    request: FormSubmitRequest,
    http_request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Create a ticket directly from form submission data.
    Accept submitted form values as-is and create the ticket.
    """
    from services.ticketing_service import ticketing_service
    from services.config_service import config_service

    resolved_hotel_code = config_service.resolve_hotel_code(request.hotel_code)
    request.hotel_code = resolved_hotel_code
    scope_token = db_config_service.set_hotel_context(resolved_hotel_code)
    trace_id = str(getattr(http_request.state, "trace_id", "") or "").strip()
    endpoint = str(http_request.url.path or "/api/chat/form-submit").strip()
    method = str(http_request.method or "POST").strip().upper()
    incoming_meta = request.metadata if isinstance(request.metadata, dict) else {}
    step_trace_service.log_event(
        "chat_form_submit_start",
        payload={
            "service_id": str(request.service_id or "").strip(),
            "form_field_count": len(request.form_data or {}),
            "metadata_keys": sorted([str(k) for k in incoming_meta.keys()])[:80],
            "db_session_available": db is not None,
        },
        trace_id=trace_id,
        session_id=str(request.session_id or "").strip(),
        hotel_code=resolved_hotel_code,
        channel=str(incoming_meta.get("channel") or "").strip(),
        endpoint=endpoint,
        method=method,
        component="api.routes.chat.submit_form",
        step="request_received",
        stage="start",
    )
    # Serialize this form-submit's read-modify-write of the session context against
    # any concurrent turn on the SAME session (the widget fires this lead-form
    # submit fire-and-forget AT THE SAME INSTANT as the location-intro turn). Without
    # this, whichever saves last clobbers the other's pending_data — dropping the
    # property scope the intro just pinned, so the next chip answers the wrong
    # location. Per-session lock: never blocks other sessions.
    _sess_lock = context_manager.session_lock(request.session_id)
    await _sess_lock.acquire()
    try:
        requested_service_id = _normalize_service_identifier(request.service_id)
        service = config_service.get_service(requested_service_id)
        # DB fallback: service may exist only in the database (production mode)
        if not service:
            try:
                db_services = await db_config_service.get_services()
            except Exception:
                db_services = []
            for candidate in db_services:
                if _normalize_service_identifier(candidate.get("id")) == requested_service_id:
                    service = dict(candidate)
                    break
                if _normalize_service_identifier(candidate.get("name")) == requested_service_id:
                    service = dict(candidate)
                    break
        if not service:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Service '{request.service_id}' not found for hotel "
                    f"'{resolved_hotel_code}'."
                ),
            )

        # Lead-capture / info-collection form (e.g. the opening name+location
        # form at the start of the conversation): captures the guest's identity
        # for the session but NEVER raises a support ticket — it is informational,
        # not an actionable request. Recognised two ways:
        #
        #  1. METADATA MARKER (the real-world case): the widget tags the
        #     session-start lead form with `lead_source` (and `booking_location`)
        #     in metadata — markers actionable service forms never carry. This
        #     form is submitted under a TICKETING-ENABLED service (e.g.
        #     room_booking_request), so it must be detected independently of the
        #     service's ticketing flag, or it silently creates a spurious booking
        #     ticket at the very start of every new session.
        #  2. CONFIG FLAG: a service whose form_config sets `lead_capture_only`,
        #     honoured only when ticketing is disabled for that service so the
        #     flag can never silently turn a real ticketing service into a no-op.
        _incoming_form_meta = request.metadata if isinstance(request.metadata, dict) else {}
        _is_opening_lead_form = bool(
            str(_incoming_form_meta.get("lead_source") or "").strip()
        )
        _svc_form_config_early = service.get("form_config") if isinstance(service.get("form_config"), dict) else {}
        _lead_capture_only = (
            _is_opening_lead_form
            or (
                isinstance(_svc_form_config_early, dict)
                and bool(_svc_form_config_early.get("lead_capture_only"))
                and not bool(service.get("ticketing_enabled", False))
            )
        )

        if not bool(service.get("ticketing_enabled", False)) and not _lead_capture_only:
            raise HTTPException(status_code=400, detail="Ticketing is not enabled for this service.")

        resolved_service_id = str(service.get("id") or requested_service_id).strip()
        form_data = request.form_data or {}
        req_meta = request.metadata or {}
        service_name = str(service.get("name") or resolved_service_id)
        step_trace_service.log_event(
            "chat_form_submit_service_resolved",
            payload={
                "requested_service_id": requested_service_id,
                "resolved_service_id": resolved_service_id,
                "service_name": service_name,
                "ticketing_enabled": bool(service.get("ticketing_enabled", False)),
            },
            trace_id=trace_id,
            session_id=str(request.session_id or "").strip(),
            hotel_code=resolved_hotel_code,
            channel=str(req_meta.get("channel") or "").strip(),
            endpoint=endpoint,
            method=method,
            component="api.routes.chat.submit_form",
            step="service_resolution",
            stage="completed",
        )

        phase = str(
            service.get("phase_id")
            or req_meta.get("phase") or req_meta.get("phase_id")
            or ""
        )

        department_id = str(service.get("department_id") or req_meta.get("department_id") or "")
        department_name = str(service.get("department_name") or req_meta.get("department_name") or "")

        context = await context_manager.get_or_create_context(
            session_id=request.session_id,
            hotel_code=resolved_hotel_code,
        )
        # Tag every message this form-submit appends (the form summary + ack)
        # with the rotating per-thread conversation id so chat history threads
        # the intro form WITH its own conversation instead of orphaning it as a
        # NULL/legacy group. Mirror of the main chat path in chat_service.
        if not isinstance(context.pending_data, dict):
            context.pending_data = {}
        # #5 — An OPENING lead-capture form is part of the SAME conversation the
        # guest is already in (they picked a location moments ago). It is a
        # side-task (record the sales lead); it must NOT disturb that
        # conversation's property scope. Snapshot the scope keys now and restore
        # them right before saving IF this lead-capture ended up dropping them
        # (e.g. the conversation-rotation reset below). Belt-and-suspenders beside
        # the per-session lock, so the lead-save can never erase the chosen
        # location. Scoped to the opening lead form only — real mid-conversation
        # form-submits keep their normal reset behaviour.
        _fs_scope_snapshot: dict[str, Any] = {}
        if _is_opening_lead_form:
            _fs_scope_snapshot = {
                _k: context.pending_data[_k]
                for _k in (
                    "_active_property_ids", "_active_property_names",
                    "_selected_property_scope_id", "_selected_property_scope_ids",
                    "_selected_property_scope_name", "_selected_phase_id",
                    "_property_name_map", "_past_property_context",
                )
                if _k in context.pending_data
            }
        _fs_conv_uid = str(
            req_meta.get("conversation_id") or req_meta.get("conversationId") or ""
        ).strip()
        try:
            from services.db_log_sink import set_conversation_uid as _set_conv_uid_log
            _set_conv_uid_log(_fs_conv_uid)
        except Exception:
            pass
        if _fs_conv_uid:
            # Fresh conversation_id → drop any prior conversation's scope/memory so
            # the new thread starts clean (mirror of the main chat path).
            _fs_rotated = conversation_memory_service.reset_pending_for_conversation_rotation(
                context, _fs_conv_uid
            )
            context.pending_data["_active_conversation_uid"] = _fs_conv_uid
            if _fs_rotated:
                logger.info(
                    "[CONV_ROTATE] (form-submit) new conversation_id — wiped prior scope. "
                    "session=%s new_conv=%s",
                    getattr(context, "session_id", ""), _fs_conv_uid,
                )
        step_trace_service.log_event(
            "chat_form_submit_context_loaded",
            payload={
                "has_pending_data": isinstance(getattr(context, "pending_data", None), dict),
                "pending_action": str(getattr(context, "pending_action", "") or ""),
            },
            trace_id=trace_id,
            session_id=str(request.session_id or "").strip(),
            hotel_code=resolved_hotel_code,
            channel=str(req_meta.get("channel") or "").strip(),
            endpoint=endpoint,
            method=method,
            component="api.routes.chat.submit_form",
            step="context_load",
            stage="completed",
        )

        form_fields = []
        form_config = service.get("form_config") if isinstance(service.get("form_config"), dict) else {}
        if isinstance(form_config, dict):
            raw_fields = form_config.get("fields")
            if isinstance(raw_fields, list):
                form_fields = [field for field in raw_fields if isinstance(field, dict)]

        validation_errors = _merge_validation_errors(
            _validate_required_form_fields(
                form_fields=form_fields,
                form_data=form_data,
            ),
            _validate_checkin_checkout_sequence(form_data=form_data),
            _validate_phone_fields(form_fields=form_fields, form_data=form_data),
            _validate_service_date_boundaries(
                phase_id=phase,
                form_fields=form_fields,
                merged_form_data=form_data,
                req_meta=req_meta,
                context=context,
            ),
        )
        if validation_errors:
            step_trace_service.log_event(
                "chat_form_submit_validation_failed",
                payload={
                    "service_id": resolved_service_id,
                    "error_count": len(validation_errors),
                    "errors": validation_errors[:12],
                },
                trace_id=trace_id,
                session_id=str(request.session_id or "").strip(),
                hotel_code=resolved_hotel_code,
                channel=str(req_meta.get("channel") or "").strip(),
                endpoint=endpoint,
                method=method,
                status_code=400,
                component="api.routes.chat.submit_form",
                step="validation",
                stage="failed",
                error="required_fields_missing",
            )
            return {
                "success": False,
                "ticket_id": "",
                "message": "Please complete the required fields and try again.",
                "errors": validation_errors,
            }

        if isinstance(context.pending_data, dict):
            context.pending_data.update({
                k: str(v) for k, v in form_data.items() if str(v).strip()
            })
            context.pending_data["service_id"] = resolved_service_id
            context.pending_data["service_name"] = service_name
            if req_meta.get("guest_id"):
                context.pending_data.setdefault("guest_id", str(req_meta["guest_id"]))
            if req_meta.get("entity_id"):
                context.pending_data.setdefault("entity_id", str(req_meta["entity_id"]))
        else:
            context.pending_data = {
                k: str(v) for k, v in form_data.items() if str(v).strip()
            }

        room_number = str(
            form_data.get("room_number") or req_meta.get("room_number") or ""
        )
        if room_number:
            context.room_number = room_number

        # Capture the guest's identity from the form into the CONTEXT so it persists
        # to LATER tickets in this session (e.g. a room-booking ticket raised on a
        # following turn). This used to ride on the booking-persist path, which is now
        # correctly skipped for non-room forms (lead / support / venue) — but the guest
        # NAME and PHONE must still stick, otherwise those later tickets show "Guest" /
        # blank in the KePSLA + API dashboards.
        _form_name = str(
            form_data.get("full_name") or form_data.get("guest_name") or form_data.get("name") or ""
        ).strip()
        if _form_name:
            context.guest_name = _form_name
            # The name the guest typed on THIS form is authoritative. Overwrite any
            # stale `guest_name` cached in pending_data from an earlier turn (e.g.
            # the opening lead form) — the ticket payload and the dashboard identity
            # resolvers both check `guest_name` BEFORE `full_name`, so without this a
            # stale value silently shadows the edit and the ticket keeps the old
            # lead name on some turns but not others.
            if isinstance(context.pending_data, dict):
                for _name_key in ("guest_name", "full_name", "name"):
                    context.pending_data[_name_key] = _form_name
        _form_phone = str(
            form_data.get("phone") or form_data.get("phone_number") or form_data.get("guest_phone") or ""
        ).strip()
        if _form_phone and not str(getattr(context, "guest_phone", "") or "").strip():
            context.guest_phone = _form_phone

        # Merge pending_data (which includes trigger field values collected
        # before the form was shown) so ticket descriptions include full context.
        merged_form_data = dict(context.pending_data) if isinstance(context.pending_data, dict) else {}
        merged_form_data.update({k: str(v) for k, v in form_data.items() if str(v).strip()})

        issue_summary, detailed_message = await _generate_form_ticket_description(
            service=service,
            service_name=service_name,
            service_id=resolved_service_id,
            merged_form_data=merged_form_data,
            req_meta=req_meta,
            context=context,
        )

        ticket_payload = ticketing_service.build_lumira_ticket_payload(
            context=context,
            issue=issue_summary,
            message=detailed_message,
            category=service_name,
            priority=str(req_meta.get("priority") or "medium"),
            department_id=department_id,
            department_manager=department_name,
            phase=phase,
            source=str(req_meta.get("ticket_source") or "web_widget"),
        )

        ticket_payload["service_id"] = resolved_service_id
        ticket_payload["service_name"] = service_name

        for k, v in merged_form_data.items():
            ticket_payload.setdefault(k, str(v))

        step_trace_service.log_event(
            "chat_form_submit_ticket_create_start",
            payload={
                "service_id": resolved_service_id,
                "service_name": service_name,
                "payload_keys": sorted([str(k) for k in ticket_payload.keys()])[:120],
            },
            trace_id=trace_id,
            session_id=str(request.session_id or "").strip(),
            hotel_code=resolved_hotel_code,
            channel=str(req_meta.get("channel") or "").strip(),
            endpoint=endpoint,
            method=method,
            component="api.routes.chat.submit_form",
            step="ticket_create",
            stage="start",
        )
        if _lead_capture_only:
            # Info-collection form: capture the lead but do NOT create a ticket
            # and do NOT call the external ticketing API. Synthesize a success
            # result so the profile + dashboard writes and the confirmation below
            # still run, with no ticket id.
            from types import SimpleNamespace as _SimpleNamespace
            result = _SimpleNamespace(success=True, ticket_id="", error="")
            step_trace_service.log_event(
                "chat_form_submit_lead_capture_no_ticket",
                payload={"service_id": resolved_service_id, "service_name": service_name},
                trace_id=trace_id,
                session_id=str(request.session_id or "").strip(),
                hotel_code=resolved_hotel_code,
                channel=str(req_meta.get("channel") or "").strip(),
                endpoint=endpoint,
                method=method,
                component="api.routes.chat.submit_form",
                step="ticket_create",
                stage="skipped_lead_capture",
            )
        else:
            try:
                result = await ticketing_service.create_ticket(ticket_payload)
            except Exception:
                logger.exception("form-submit: create_ticket raised an exception")
                result = None  # fall through to local fallback below

        # ── Local fallback ────────────────────────────────────────────────────
        # If the external API is not configured, timed out, or returned an
        # error, persist the submission locally so the guest always gets a
        # success response. A local ticket (LOCAL-N) can be manually synced
        # or picked up by a future integration job.
        if not _lead_capture_only and (result is None or not getattr(result, "success", False)):
            _fallback_reason = str(getattr(result, "error", "") or "create_ticket_exception").strip() or "unknown"
            logger.warning(
                "form-submit: create_ticket failed (%s) — attempting local fallback for service=%s session=%s",
                _fallback_reason, resolved_service_id, str(request.session_id or "").strip(),
            )
            try:
                result = await ticketing_service._create_ticket_local(ticket_payload)
                if result.success:
                    logger.info(
                        "form-submit: local fallback succeeded ticket_id=%s session=%s",
                        result.ticket_id, str(request.session_id or "").strip(),
                    )
                    step_trace_service.log_event(
                        "chat_form_submit_ticket_local_fallback",
                        payload={"service_id": resolved_service_id, "fallback_reason": _fallback_reason,
                                 "ticket_id": result.ticket_id},
                        trace_id=trace_id,
                        session_id=str(request.session_id or "").strip(),
                        hotel_code=resolved_hotel_code,
                        channel=str(req_meta.get("channel") or "").strip(),
                        endpoint=endpoint,
                        method=method,
                        component="api.routes.chat.submit_form",
                        step="ticket_create_local_fallback",
                        stage="completed",
                    )
            except Exception as _local_exc:
                logger.exception("form-submit: local fallback also failed")
                step_trace_service.log_event(
                    "chat_form_submit_ticket_create_failed",
                    payload={"service_id": resolved_service_id},
                    trace_id=trace_id,
                    session_id=str(request.session_id or "").strip(),
                    hotel_code=resolved_hotel_code,
                    channel=str(req_meta.get("channel") or "").strip(),
                    endpoint=endpoint,
                    method=method,
                    status_code=500,
                    component="api.routes.chat.submit_form",
                    step="ticket_create",
                    stage="failed",
                    error="create_ticket_exception",
                )
                return {
                    "success": False,
                    "ticket_id": "",
                    "message": "Something went wrong while creating your request. Please try again.",
                }

        step_trace_service.log_event(
            "chat_form_submit_ticket_create_end",
            payload={
                "service_id": resolved_service_id,
                "ticket_success": bool(getattr(result, "success", False)),
                "ticket_id": str(getattr(result, "ticket_id", "") or ""),
            },
            trace_id=trace_id,
            session_id=str(request.session_id or "").strip(),
            hotel_code=resolved_hotel_code,
            channel=str(req_meta.get("channel") or "").strip(),
            endpoint=endpoint,
            method=method,
            component="api.routes.chat.submit_form",
            step="ticket_create",
            stage="completed" if bool(getattr(result, "success", False)) else "failed",
            error="" if bool(getattr(result, "success", False)) else str(getattr(result, "error", "") or ""),
        )

        if result.success:
            # ── REMEMBER THAT THIS TICKET CAME FROM A FORM ──────────────────
            # Its request line is rendered from fields the guest typed - pickup
            # point, date, time, flight number. The ticket-edit path may add a
            # note to it but must never replace that line with free text: asked
            # to add two guests to a room booking, the classifier matched an
            # airport pickup and rewrote it to "Room booking for 3 guests in
            # room 712.", losing every detail the guest had entered. Recorded
            # here, where it is simply known, instead of being guessed later
            # from the shape of the string.
            try:
                _fref = str(getattr(result, "ticket_id", "") or "").strip()
                if _fref and context is not None and isinstance(
                    getattr(context, "pending_data", None), dict
                ):
                    _seen = [
                        str(x) for x in
                        (context.pending_data.get("_form_built_ticket_refs") or [])
                        if str(x or "").strip()
                    ]
                    if _fref not in _seen:
                        _seen.append(_fref)
                    context.pending_data["_form_built_ticket_refs"] = _seen[-20:]
                    # WHICH SERVICES HAVE HAD THEIR FORM SUBMITTED.
                    # `_form_built_ticket_refs` records THAT a form was filled
                    # but not WHICH, so nothing server-side could tell that the
                    # event enquiry was already in. The widget knew (it hides
                    # the button after one submission) and the server did not,
                    # so it kept attaching the event CTA and the reply kept
                    # saying "tap Share your event details" over a button the
                    # browser had already retired. Recording the service id here
                    # - where it is simply known - is what lets the attach and
                    # the wording agree, the same way `_rb_bookings` does for
                    # room booking.
                    _svc = str(resolved_service_id or "").strip()
                    if _svc:
                        _done = [
                            str(x) for x in
                            (context.pending_data.get("_form_submitted_services") or [])
                            if str(x or "").strip()
                        ]
                        if _svc not in _done:
                            _done.append(_svc)
                        context.pending_data["_form_submitted_services"] = _done[-20:]
            except Exception:
                pass
            # Surface the active sub-property so the confirmation closer can
            # offer a meaningful continuation ("explore more about <X>, look
            # at another, or anything else?"). Falls through to None gracefully
            # for single-property tenants / when nothing was locked.
            _ctx_property_name = ""
            if context is not None:
                _pending = getattr(context, "pending_data", None)
                if isinstance(_pending, dict):
                    _ctx_property_name = str(
                        _pending.get("_selected_property_scope_name")
                        or _pending.get("selected_property_scope_name")
                        or _pending.get("_stay_property_name")
                        or ""
                    ).strip()
                if not _ctx_property_name:
                    _ctx_property_name = str(
                        getattr(context, "booking_property_name", "") or ""
                    ).strip()

            # Per-tenant booking URL. Prefer an explicit booking_engine_url if
            # configured, else fall back to the public website. Empty string is
            # fine — the prompt only includes the link when one is provided.
            _biz_for_link = {}
            try:
                _biz_for_link = await db_config_service.get_business_info() or {}
            except Exception:
                try:
                    _biz_for_link = config_service.get_business_info() or {}
                except Exception:
                    _biz_for_link = {}
            _booking_url = str(
                _biz_for_link.get("booking_engine_url")
                or _biz_for_link.get("website")
                or ""
            ).strip()

            # Single- vs multi-property signal for the confirmation closer, so a
            # single-property tenant is NEVER asked whether they're "considering
            # another of our properties". Data-driven: property_count /
            # location_count / the location manifest are written into the
            # business config by the welcome-cache refresh. Multi only when we
            # positively see 2+; unknown/absent defaults to single (safe — a
            # single-property tenant must never offer other properties).
            _single_property = True
            try:
                _prop_count = int(_biz_for_link.get("property_count") or 0)
            except Exception:
                _prop_count = 0
            try:
                _loc_count = int(_biz_for_link.get("location_count") or 0)
            except Exception:
                _loc_count = 0
            _manifest = _biz_for_link.get("multi_property_location_manifest") or []
            _manifest_len = len(_manifest) if isinstance(_manifest, (list, tuple)) else 0
            if _prop_count >= 2 or _loc_count >= 2 or _manifest_len >= 2:
                _single_property = False

            if _lead_capture_only:
                # Opening info-collection form: a neutral acknowledgement, NOT a
                # "request submitted / our team will reach out" ticket confirmation.
                confirm_ack, confirm_closer = (
                    "Thanks — got your details. How can I help you today?",
                    "",
                )
            else:
                confirm_ack, confirm_closer = await _generate_confirmation_message(
                    service_name=service_name,
                    form_data=form_data,
                    property_name=_ctx_property_name,
                    booking_url=_booking_url,
                    single_property=_single_property,
                )
        else:
            confirm_ack = (
                f"There was an issue submitting your {service_name} request."
                " Please try again or contact our staff."
            )
            confirm_closer = ""

        # Compose the message that goes into ChatResponse.message (kept as one
        # joined string for legacy clients / the message field). For chat
        # history persistence, the ack and closer are added as TWO separate
        # assistant turns so the UI renders them as distinct bubbles — the
        # ack feels like a system confirmation; the closer feels like the bot
        # pivoting back to help.
        confirm_msg = (
            f"{confirm_ack}\n\n{confirm_closer}".strip()
            if confirm_closer
            else confirm_ack
        )
        try:
            from schemas.chat import MessageRole
            # Persist the guest's submitted values as a USER bubble so chat
            # history shows the same "Field: value" summary the widget rendered
            # (it was previously display-only and never saved).
            try:
                # Build the stored summary from the form's DECLARED fields only
                # (the exact set the guest saw and filled), keyed by their labels
                # — NOT from merged_form_data, which carries internal context
                # (guest_id, stay dates, active/selected property scope ids,
                # entity_id, the trigger field, …). That context previously
                # spilled into chat history and showed up verbatim when the widget
                # re-rendered a past conversation (hotel switch / reconnect).
                _submitted = request.form_data if isinstance(request.form_data, dict) else {}
                _declared_fields = []
                _fc = service.get("form_config") if isinstance(service, dict) else None
                if isinstance(_fc, dict) and isinstance(_fc.get("fields"), list):
                    _declared_fields = _fc.get("fields")
                _fs_pairs: list[tuple[str, str]] = []
                _seen_ids: set[str] = set()
                for _field in _declared_fields:
                    if not isinstance(_field, dict):
                        continue
                    _fid = str(_field.get("id") or "").strip()
                    if not _fid or _fid in _seen_ids:
                        continue
                    _seen_ids.add(_fid)
                    _raw = _submitted.get(_fid)
                    if _raw is None or not str(_raw).strip():
                        _raw = merged_form_data.get(_fid)
                    _fvs = str(_raw).strip() if _raw is not None else ""
                    if not _fvs:
                        continue
                    _flabel = str(_field.get("label") or _fid.replace("_", " ").title()).strip()
                    _fs_pairs.append((_flabel, _fvs))
                # Fallback for legacy/dynamic forms with no declared field list:
                # use the guest-submitted values (never merged context) with the
                # same internal-field blocklist the ticket path uses.
                if not _declared_fields:
                    _fs_pairs = _sanitize_form_ticket_fields(_submitted)
                if _fs_pairs:
                    _fs_summary = "\n".join(f"{lbl}: {val}" for lbl, val in _fs_pairs)
                    context.add_message(
                        MessageRole.USER,
                        _fs_summary,
                        metadata={
                            "display_payload": {
                                "form_submission": {lbl: val for lbl, val in _fs_pairs},
                                "form_service_name": service_name,
                            }
                        },
                    )
            except Exception:
                pass
            context.add_message(MessageRole.ASSISTANT, confirm_ack)
            if confirm_closer:
                context.add_message(MessageRole.ASSISTANT, confirm_closer)
                # Remembered so the next turn can tell a bare "yes" to THIS closer
                # from a yes to a staff offer (post-form note, llm_orchestration_service).
                if isinstance(context.pending_data, dict):
                    context.pending_data["_post_form_closer"] = confirm_closer
        except Exception:
            pass

        booking_info: dict[str, Any] = {}
        if result.success:
            booking_info = await _persist_prebooking_booking_if_possible(
                hotel_code=resolved_hotel_code,
                service_name=service_name,
                form_data=merged_form_data,
                req_meta=req_meta,
                context=context,
                db_session=db,
            )
            if booking_info:
                try:
                    context.booking_id = int(booking_info.get("booking_id"))
                except (TypeError, ValueError):
                    pass
                context.booking_confirmation_code = str(
                    booking_info.get("confirmation_code") or context.booking_confirmation_code or ""
                ).strip() or None
                context.booking_property_name = str(
                    booking_info.get("property_name") or context.booking_property_name or ""
                ).strip() or None
                context.booking_property_scope_id = str(
                    booking_info.get("property_scope_id") or context.booking_property_scope_id or ""
                ).strip() or None
                context.booking_room_type = str(
                    booking_info.get("room_type") or context.booking_room_type or ""
                ).strip() or None
                context.booking_check_in_date = str(
                    booking_info.get("check_in_date") or context.booking_check_in_date or ""
                ).strip() or None
                context.booking_check_out_date = str(
                    booking_info.get("check_out_date") or context.booking_check_out_date or ""
                ).strip() or None
                context.booking_guest_name = str(
                    booking_info.get("guest_name") or context.booking_guest_name or ""
                ).strip() or None
                context.booking_phase = str(
                    booking_info.get("phase") or context.booking_phase or "pre_checkin"
                ).strip() or "pre_checkin"

                if not context.guest_name:
                    context.guest_name = str(booking_info.get("guest_name") or "").strip() or None
                if not context.guest_phone:
                    context.guest_phone = str(booking_info.get("guest_phone") or "").strip() or None
                if not context.room_number:
                    context.room_number = str(booking_info.get("room_number") or "").strip() or None

                if not isinstance(context.pending_data, dict):
                    context.pending_data = {}
                context.pending_data["_latest_booking_id"] = booking_info.get("booking_id")
                context.pending_data["_latest_booking_confirmation_code"] = str(
                    booking_info.get("confirmation_code") or ""
                ).strip()
                context.pending_data["_latest_booking_phase"] = str(
                    booking_info.get("phase") or "pre_checkin"
                ).strip()
                if str(booking_info.get("property_name") or "").strip():
                    context.pending_data["_stay_property_name"] = str(
                        booking_info.get("property_name") or ""
                    ).strip()
                booking_scope_id = str(booking_info.get("property_scope_id") or "").strip()
                booking_property_name = str(booking_info.get("property_name") or "").strip()
                if booking_scope_id:
                    context.pending_data["_stay_property_id"] = booking_scope_id
                    llm_orchestration_service._sync_property_scope_pending(
                        pending_data=context.pending_data,
                        property_ids=[booking_scope_id],
                        active_property_names=[booking_property_name] if booking_property_name else [],
                        selected_property_name=booking_property_name,
                    )
                    context.pending_data["_stay_property_pin_enabled"] = True
                if booking_property_name:
                    context.pending_data["_stay_property_name"] = booking_property_name
                    context.pending_data["stay_property_name"] = booking_property_name
                    if not booking_scope_id:
                        context.pending_data["_selected_property_scope_name"] = booking_property_name
                        context.pending_data["selected_property_scope_name"] = booking_property_name
                integration_snapshot = context.pending_data.get("_integration", {})
                integration_update = dict(integration_snapshot) if isinstance(integration_snapshot, dict) else {}
                guest_id_text = str(booking_info.get("guest_id") or "").strip()
                guest_phone_text = str(booking_info.get("guest_phone") or "").strip()
                if booking_scope_id:
                    integration_update.setdefault("booking_property_scope_id", booking_scope_id)
                if booking_property_name:
                    integration_update.setdefault("booking_property_name", booking_property_name)
                if guest_id_text:
                    integration_update.setdefault("guest_id", guest_id_text)
                    context.pending_data.setdefault("guest_id", guest_id_text)
                if guest_phone_text:
                    integration_update.setdefault("guest_phone", guest_phone_text)
                integration_update.setdefault("phase", "pre_booking")
                context.pending_data["_integration"] = integration_update

            # Clear booking flow state so the bot doesn't offer to "resume"
            # a booking that was already completed via form submission.
            from schemas.chat import ConversationState
            context.pending_action = None
            context.pending_data = conversation_memory_service.retain_safe_internal_entries(
                context.pending_data,
                preserve_deferred_refresh=False,
            )
            context.pending_data.pop("_clarification_attempts", None)
            context.state = ConversationState.IDLE
            # Remove this service from suspended list if present and clear any
            # stale resume prompts for a fully completed form flow.
            if isinstance(context.suspended_services, list):
                context.suspended_services = [
                    s for s in context.suspended_services
                    if _normalize_service_identifier((s or {}).get("service_id")) != _normalize_service_identifier(resolved_service_id)
                ]
            if not context.suspended_services:
                context.resume_prompt_sent = False
            # Remember the dropdown / opening-form location so the next chat turn
            # can activate ALL properties in that location (multi-property scope
            # seed in the orchestrator). The router then narrows by conversation.
            try:
                _seed_location_label = _extract_session_profile_fields(
                    merged_form_data=merged_form_data,
                    req_meta=req_meta,
                    context=context,
                ).get("location") or ""
                if _seed_location_label and isinstance(context.pending_data, dict):
                    context.pending_data["_session_location_label"] = _seed_location_label
            except Exception:
                pass
            # #5 — restore the property scope if this opening lead-capture dropped
            # it (only fills when scope is now empty → never overrides a scope the
            # form legitimately set), so the guest's chosen location survives.
            if _fs_scope_snapshot and isinstance(context.pending_data, dict):
                if not context.pending_data.get("_active_property_ids"):
                    for _sk, _sv in _fs_scope_snapshot.items():
                        context.pending_data.setdefault(_sk, _sv)
            try:
                await context_manager.save_context(context, db_session=db)
            except Exception:
                await context_manager.save_context(context, db_session=None)

            # Capture the stable lead identity for this session (new_bot_sessions).
            # Upsert is fill-blanks-only and runs on its own DB session, so it
            # records the first form's name/phone/email/location without ever
            # disturbing the form-submit transaction. Best-effort.
            try:
                from services import session_profile_service

                _profile_fields = _extract_session_profile_fields(
                    merged_form_data=merged_form_data,
                    req_meta=req_meta,
                    context=context,
                )
                await session_profile_service.upsert_session_profile(
                    session_id=str(request.session_id or "").strip(),
                    hotel_code=resolved_hotel_code,
                    guest_name=_profile_fields.get("guest_name"),
                    guest_phone=_profile_fields.get("guest_phone"),
                    guest_email=_profile_fields.get("guest_email"),
                    location=_profile_fields.get("location"),
                    channel=str(req_meta.get("channel") or "").strip() or None,
                    source=str(req_meta.get("ticket_source") or "web_widget").strip() or None,
                    raw_form=dict(form_data) if isinstance(form_data, dict) else None,
                )
            except Exception:
                logger.warning("form-submit: session profile upsert skipped", exc_info=True)

            # Mirror the opening lead form into GHN_KEBO_FORM_RESULTS, the table
            # the Kepsla dashboard reads to show what the guest typed. Scoped to
            # the lead form only: every other service form stays in our own
            # tables, which are the source of truth for them. Best-effort, and
            # on its own session — the lead is already safe in new_bot_sessions.
            try:
                # Match on what the CLIENT asked for, not resolved_service_id:
                # that one is rewritten to the service's numeric DB id.
                if requested_service_id == _normalize_service_identifier(
                    "room_booking_request"
                ):
                    from services import lead_form_results_service, lead_form_service
                    from models.database import AsyncSessionLocal as _LeadSession

                    async with _LeadSession() as _lead_db:
                        _lead_form = await lead_form_service.get_lead_form(
                            _lead_db, resolved_hotel_code
                        )
                    _submitted = lead_form_results_service.build_submitted_fields(
                        _lead_form.get("fields") or [],
                        merged_form_data if isinstance(merged_form_data, dict) else {},
                    )
                    await lead_form_results_service.record_submission(
                        session_id=str(request.session_id or "").strip(),
                        form_title=_lead_form.get("title") or "Customer Information",
                        fields=_submitted,
                        auto_load_form=bool(_lead_form.get("auto_load_form", True)),
                    )
            except Exception:
                logger.warning("form-submit: lead form result skipped", exc_info=True)

            step_trace_service.log_event(
                "chat_form_submit_end",
                payload={
                    "success": True,
                    "ticket_id": str(result.ticket_id or ""),
                    "booking_created": bool(booking_info),
                    "booking_id": booking_info.get("booking_id") if booking_info else None,
                },
                trace_id=trace_id,
                session_id=str(request.session_id or "").strip(),
                hotel_code=resolved_hotel_code,
                channel=str(req_meta.get("channel") or "").strip(),
                endpoint=endpoint,
                method=method,
                status_code=200,
                component="api.routes.chat.submit_form",
                step="response_build",
                stage="completed",
            )
            # Booking-link button: for room-booking services with a configured
            # booking URL, hand the frontend a booking_cta so it renders a
            # "Book on website" button instead of a raw text link. The
            # confirmation prompt was told to reference "the button below".
            _booking_cta_out = None
            if _booking_url and "room" in str(service_name or "").lower():
                _booking_cta_out = {"url": _booking_url, "label": "Book on website"}

            # ── Dashboard-facing writes (both keyed by session_id; entity_id =
            #    the FMS id already resolved onto the ticket payload, so rows
            #    attach to the correct property). All fire-and-forget. ─────────
            _eng_sid = str(request.session_id or "").strip()
            _eng_entity = str(ticket_payload.get("organisation_id") or "").strip()
            _eng_group = str(req_meta.get("group_id") or req_meta.get("groupId") or "").strip()
            _eng_widget = str(req_meta.get("widget_id") or req_meta.get("widgetId") or "").strip()
            # Real Kepsla widget/group ids (dashboard scopes the guest-name
            # lookup by these). Override the request values when configured.
            # _is_kebo gates the kebo-only writes below, so KePSLA / other
            # tenants are NEVER touched (their tickets/history are unchanged).
            _is_kebo = False
            try:
                from services.customer_details_service import customer_details_service as _cds
                _map_widget, _map_group = _cds.resolve_widget_group(resolved_hotel_code)
                _eng_widget = _map_widget or _eng_widget
                _eng_group = _map_group or _eng_group
                _is_kebo = bool(_map_widget)
            except Exception:
                pass
            _eng_conv = (
                str(req_meta.get("conversation_id") or req_meta.get("conversationId") or "").strip()
                or _eng_sid
            )

            # Lead identity fields + a CLEAN, human-readable form summary for the
            # chat-history row. NEVER dump raw pending_data / internal context
            # (that produced the giant JSON blob in the dashboard transcript).
            _cd = _extract_session_profile_fields(
                merged_form_data=merged_form_data, req_meta=req_meta, context=context,
            )
            _eng_form_lines: list[str] = []
            if _cd.get("guest_name"):
                _eng_form_lines.append(f"Full Name: {_cd['guest_name']}")
            if _cd.get("guest_phone"):
                _eng_form_lines.append(f"Phone: {_cd['guest_phone']}")
            if _cd.get("guest_email"):
                _eng_form_lines.append(f"Email: {_cd['guest_email']}")
            _eng_prop = str(ticket_payload.get("property_name") or _cd.get("location") or "").strip()
            if _eng_prop:
                _eng_form_lines.append(f"Property: {_eng_prop}")
            if str(issue_summary or "").strip():
                _eng_form_lines.append(f"Details: {str(issue_summary).strip()}")
            _eng_form_msg = "\n".join(_eng_form_lines) or str(issue_summary or "Form submitted")

            # 1) Mirror the lead turn into KEBO_ENGAGE_SESSIONS (chat history).
            if _is_kebo and bool(getattr(settings, "engage_session_logging_enabled", True)):
                try:
                    from services.engage_session_service import engage_session_service

                    engage_session_service.write_turn_bg(
                        session_id=_eng_sid,
                        sender="USER",
                        message=_eng_form_msg,
                        entity_id=_eng_entity,
                        group_id=_eng_group,
                        widget_id=_eng_widget,
                        conversation_id=_eng_conv,
                        message_type="form",
                    )
                    engage_session_service.write_turn_bg(
                        session_id=_eng_sid,
                        sender="BOT",
                        message=str(confirm_ack or confirm_msg or ""),
                        entity_id=_eng_entity,
                        group_id=_eng_group,
                        widget_id=_eng_widget,
                        conversation_id=_eng_conv,
                        message_type="text",
                    )
                except Exception:
                    logger.warning("form-submit: engage_session logging skipped", exc_info=True)

            # 2) Store the guest identity in GHN_CHAT_BOT_CUSTOMER_DETAILS so the
            #    dashboard shows the name against the ticket (keyed by Session_Id).
            #    Processing_Status=PROCESSED => the Java email job skips it.
            if _is_kebo and bool(getattr(settings, "customer_details_logging_enabled", True)):
                try:
                    from services.customer_details_service import customer_details_service

                    customer_details_service.write_bg(
                        session_id=_eng_sid,
                        conversation_id=_eng_conv,
                        widget_id=_eng_widget,
                        group_id=_eng_group,
                        guest_name=str(_cd.get("guest_name") or ticket_payload.get("guest_name") or ""),
                        email=str(_cd.get("guest_email") or ""),
                        phone=str(_cd.get("guest_phone") or ""),
                        city=str(_cd.get("location") or ""),
                        property_name=str(ticket_payload.get("property_name") or ""),
                        topic=str(issue_summary or service_name or ""),
                        form_type=str(getattr(settings, "customer_details_form_type", "ci_form") or "ci_form"),
                        processing_status=str(getattr(settings, "customer_details_processing_status", "PROCESSED") or "PROCESSED"),
                        others={
                            k: v for k, v in merged_form_data.items()
                            if not str(k).startswith("_")
                        },
                    )
                except Exception:
                    logger.warning("form-submit: customer_details write skipped", exc_info=True)

            return {
                "success": True,
                "ticket_id": result.ticket_id or "",
                # `message` is the ack only (sentences 1-3: acknowledgement + pending + ticket ID).
                # The frontend renders it inside the form's success area.
                "message": confirm_ack or confirm_msg,
                # `closer` is the single re-engagement question. The frontend
                # pushes it as a separate assistant bubble AFTER the form box
                # so it visually feels like the bot pivoting back to help.
                "closer": confirm_closer or "",
                "booking_cta": _booking_cta_out,
                "booking_created": bool(booking_info),
                "booking_id": booking_info.get("booking_id") if booking_info else None,
                "booking_confirmation_code": booking_info.get("confirmation_code") if booking_info else "",
                "booking_phase": booking_info.get("phase") if booking_info else "",
            }

        raw_error = str(result.error or "").strip()
        if "<html" in raw_error.lower() or len(raw_error) > 300:
            clean_error = "The booking system is temporarily unavailable"
        else:
            clean_error = raw_error or "Unknown error"

        step_trace_service.log_event(
            "chat_form_submit_end",
            payload={
                "success": False,
                "ticket_error": clean_error,
            },
            trace_id=trace_id,
            session_id=str(request.session_id or "").strip(),
            hotel_code=resolved_hotel_code,
            channel=str(req_meta.get("channel") or "").strip(),
            endpoint=endpoint,
            method=method,
            status_code=200,
            component="api.routes.chat.submit_form",
            step="response_build",
            stage="completed",
            error=clean_error,
        )
        return {
            "success": False,
            "ticket_id": "",
            "message": (
                f"Submission failed: {clean_error}."
                " Please try again or contact our staff."
            ),
        }
    finally:
        step_trace_service.log_event(
            "chat_form_submit_scope_reset",
            payload={},
            trace_id=trace_id,
            session_id=str(request.session_id or "").strip(),
            hotel_code=resolved_hotel_code,
            endpoint=endpoint,
            method=method,
            component="api.routes.chat.submit_form",
            step="scope_reset",
            stage="completed",
        )
        db_config_service.reset_hotel_context(scope_token)
        _sess_lock.release()


@router.post("/form-cancel")
@router.post("/form_cancel")
async def cancel_form(
    request: FormCancelRequest,
    http_request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Back out of an open in-chat form without submitting it.

    An open form switches the widget's text box OFF, so submit-or-abandon were
    the guest's only two exits. This is the third: it raises NO ticket (in form
    mode the ticket is only created on submit, so there is nothing to undo) and
    its real work happens on the SERVER — the turn that opened the form parked
    the session on pending_action='collect_form_details', and a live
    pending_action switches the router off, so a cancel that only closed the
    form in the browser would leave the next typed message routing ungrounded.

    So: clear the parked flow state, and write the cancellation into the
    conversation history so the next turn's model sees that the guest backed out
    instead of finding a gap. The acknowledgement is fixed text, deliberately
    LLM-free — it is instant, it cannot invent anything, and it cannot re-open
    the form it was called to close.
    """
    resolved_hotel_code = config_service.resolve_hotel_code(request.hotel_code)
    request.hotel_code = resolved_hotel_code
    trace_id = str(getattr(http_request.state, "trace_id", "") or "").strip()
    endpoint = str(http_request.url.path or "/api/chat/form-cancel").strip()
    method = str(http_request.method or "POST").strip().upper()
    req_meta = request.metadata if isinstance(request.metadata, dict) else {}

    # Same opt-in gate the form-open path checks. Re-checked here so the gate is
    # real and not merely cosmetic: a hotel that never renders the button cannot
    # have its session state cleared by a hand-rolled call to this endpoint.
    if not _form_cancel_enabled_for_hotel(resolved_hotel_code):
        raise HTTPException(
            status_code=400,
            detail=f"Form cancellation is not enabled for hotel '{resolved_hotel_code}'.",
        )

    scope_token = db_config_service.set_hotel_context(resolved_hotel_code)
    # Serialise against any concurrent turn on the SAME session, exactly as
    # form-submit does — both do a read-modify-write of the session context, and
    # whichever saved last would otherwise clobber the other.
    _sess_lock = context_manager.session_lock(request.session_id)
    await _sess_lock.acquire()
    try:
        requested_service_id = _normalize_service_identifier(request.service_id)
        service_name = ""
        if requested_service_id:
            service = config_service.get_service(requested_service_id)
            if not service:
                # Service may exist only in the database (admin-created).
                try:
                    for candidate in (await db_config_service.get_services()) or []:
                        if not isinstance(candidate, dict):
                            continue
                        if (
                            _normalize_service_identifier(candidate.get("id")) == requested_service_id
                            or _normalize_service_identifier(candidate.get("name")) == requested_service_id
                        ):
                            service = dict(candidate)
                            break
                except Exception:
                    service = None
            if service:
                service_name = str(service.get("name") or "").strip()
        label = service_name or requested_service_id.replace("_", " ").strip()

        context = await context_manager.get_or_create_context(
            session_id=request.session_id,
            hotel_code=resolved_hotel_code,
        )
        if not isinstance(context.pending_data, dict):
            context.pending_data = {}

        _prev_pending_action = str(getattr(context, "pending_action", "") or "")
        _fc_conv_uid = str(
            req_meta.get("conversation_id") or req_meta.get("conversationId") or ""
        ).strip()
        try:
            from services.db_log_sink import set_conversation_uid as _set_conv_uid_log
            _set_conv_uid_log(_fc_conv_uid)
        except Exception:
            pass
        if _fc_conv_uid:
            conversation_memory_service.reset_pending_for_conversation_rotation(
                context, _fc_conv_uid
            )

        # Un-park the flow. Same clearing form-submit performs on a completed
        # form: property scope, stay anchors and memory survive the sanitiser,
        # the in-flight service state does not.
        context.pending_action = None
        context.pending_data = conversation_memory_service.retain_safe_internal_entries(
            context.pending_data,
            preserve_deferred_refresh=False,
        )
        context.pending_data.pop("_clarification_attempts", None)
        context.state = ConversationState.IDLE
        if isinstance(context.suspended_services, list):
            context.suspended_services = [
                svc for svc in context.suspended_services
                if _normalize_service_identifier((svc or {}).get("service_id")) != requested_service_id
            ]
        if not context.suspended_services:
            context.resume_prompt_sent = False
        # The sanitiser drops the thread id (it is not on its safe list), so
        # restore it BEFORE adding messages — add_message stamps each one with
        # it, and an unstamped message is orphaned from the chat-history thread.
        if _fc_conv_uid:
            context.pending_data["_active_conversation_uid"] = _fc_conv_uid

        ack = "No problem, I have cancelled that request. Is there anything else I can help you with?"
        context.add_message(
            MessageRole.USER,
            (
                f"[Guest cancelled the {label} request without submitting the form.]"
                if label
                else "[Guest cancelled the open request without submitting the form.]"
            ),
            {"form_cancelled": True, "service_id": requested_service_id},
        )
        context.add_message(MessageRole.ASSISTANT, ack, {"source": "form_cancel_ack"})

        try:
            await context_manager.save_context(context, db_session=db)
        except Exception:
            await context_manager.save_context(context, db_session=None)

        step_trace_service.log_event(
            "chat_form_cancel_end",
            payload={
                "service_id": requested_service_id,
                "service_name": service_name,
                "cleared_pending_action": _prev_pending_action,
            },
            trace_id=trace_id,
            session_id=str(request.session_id or "").strip(),
            hotel_code=resolved_hotel_code,
            channel=str(req_meta.get("channel") or "").strip(),
            endpoint=endpoint,
            method=method,
            status_code=200,
            component="api.routes.chat.cancel_form",
            step="response_build",
            stage="completed",
        )
        logger.info(
            "[FORM_CANCEL] session=%s hotel=%s service=%s cleared_pending_action=%r",
            request.session_id, resolved_hotel_code, requested_service_id, _prev_pending_action,
        )
        return {"success": True, "message": ack, "service_id": requested_service_id}
    finally:
        db_config_service.reset_hotel_context(scope_token)
        _sess_lock.release()


@router.post("/message", response_model=ChatResponse)
async def send_message(
    request: ChatRequest,
    http_request: Request,
    db: AsyncSession = Depends(get_db),
) -> ChatResponse:
    """
    Process a chat message and return bot response.
    DB session is injected so handlers can persist orders, guests, etc.
    """
    trace_id = getattr(http_request.state, "trace_id", "")
    endpoint = str(http_request.url.path or "/api/chat/message").strip()
    method = str(http_request.method or "POST").strip().upper()
    _turn_start = _perf_counter()  # for new_detailed_logger duration tracking
    _step_timings: list[dict[str, Any]] = []
    _timing_status = "started"
    _timing_status_code = 0
    _timing_error = ""
    _timing_db_fallback = False
    _timing_response_message = ""
    _timing_response_state = ""
    _timing_response_source = ""
    response: Optional[ChatResponse] = None

    def _record_step(
        step: str,
        started_at: float,
        *,
        status: str = "completed",
        details: dict[str, Any] | None = None,
        error: str = "",
    ) -> None:
        _step_timings.append(
            {
                "step": str(step or "").strip(),
                "status": str(status or "").strip() or "completed",
                "duration_ms": round((_perf_counter() - float(started_at)) * 1000, 2),
                "details": details or {},
                "error": str(error or "").strip(),
            }
        )

    _setup_started = _perf_counter()
    resolved_hotel_code = config_service.resolve_hotel_code(request.hotel_code)
    request.hotel_code = resolved_hotel_code
    scope_token = db_config_service.set_hotel_context(resolved_hotel_code)

    # ── Deployment model pin (HTTP fallback path; mirrors websocket_chat) ─────
    # The widget retries over HTTP when the WS delivery-ack times out, so this
    # path must honour the customer's pinned model too — otherwise a retried
    # turn would silently answer on whatever the harness dropdown says. The key
    # is read from the request (query/header/cookie) by the same helper the
    # origin middleware uses; the MODEL itself is resolved server-side from the
    # deployment row, never from the request.
    from api.middleware.widget_origin import _extract_widget_key
    from services.model_override_service import reset_pinned_model, set_pinned_model
    from services.widget_deployment_service import widget_deployment_service as _wds_http

    _http_widget_key = _extract_widget_key(http_request)
    _http_pinned_model = (
        await _wds_http.model_for_widget_key(_http_widget_key) if _http_widget_key else ""
    )
    pin_token = set_pinned_model(_http_pinned_model) if _http_pinned_model else None

    if not isinstance(request.metadata, dict):
        request.metadata = {}

    # Set true once the guest's KEBO_ENGAGE_SESSIONS row has been written ahead of
    # the turn (see the pre-turn block below), so the post-turn mirror does not
    # write it a second time. Declared here so it is defined on EVERY path through
    # this handler, including the ones that never reach the pre-turn block.
    _http_user_row_written = False

    # ── Idempotency: dedupe a retried message (same client message_id) ─────────
    # The widget resends a message over HTTP when its WebSocket delivery-ack times
    # out. If that original already reached the server, return the cached answer
    # instead of running the whole pipeline again (no duplicate reply / ticket).
    _dedup_sid = str(request.session_id or "").strip()
    _dedup_mid = str(request.metadata.get("message_id") or "").strip()
    _dedup_key = (_dedup_sid, _dedup_mid) if (_dedup_sid and _dedup_mid) else None
    _dedup_owned = False
    if _dedup_key:
        _dedup_verdict, _dedup_cached = await message_dedup.claim(_dedup_key)
        if _dedup_verdict == "owner":
            _dedup_owned = True
        elif _dedup_cached is not None:
            # Completed duplicate — hand back the cached response. Reset the hotel
            # context we just set since we're returning before the main try/finally.
            db_config_service.reset_hotel_context(scope_token)
            if pin_token is not None:
                reset_pinned_model(pin_token)
            _dedup_cached = dict(_dedup_cached)
            _dedup_meta = _dedup_cached.get("metadata")
            if isinstance(_dedup_meta, dict):
                _dedup_meta = dict(_dedup_meta)
                _dedup_meta["dedup_hit"] = True
                _dedup_cached["metadata"] = _dedup_meta
            return ChatResponse(**_dedup_cached)
        # else: in-flight duplicate we couldn't await — fall through and process.

    prefetch_batch_id = str(
        request.metadata.get("prefetch_batch_id")
        or request.metadata.get("ui_prefetch_batch_id")
        or ""
    ).strip()
    prefetch_source_type = str(
        request.metadata.get("ui_source_type")
        or request.metadata.get("source_type")
        or ""
    ).strip().lower()
    # A TAPPED CHIP IS SPENT. The message text of a chip click IS the chip, so
    # this is where "the guest has already used that suggestion" is recorded, for
    # the rest of the conversation. Merely showing a chip records nothing: an
    # ignored suggestion is not a topic the guest has covered.
    if prefetch_source_type in {"suggested_action", "suggestion_chip", "chip"}:
        try:
            llm_orchestration_service.remember_chips_shown(
                request.session_id,
                str((request.metadata or {}).get("conversation_id") or "").strip(),
                [str(request.message or "").strip()],
            )
        except Exception:
            pass

    turn_trace_id, turn_token = turn_diagnostics_service.begin_turn(
        request=request,
        api_trace_id=trace_id,
    )
    _record_step(
        "turn_setup",
        _setup_started,
        details={
            "db_session_available": db is not None,
            "hotel_code": resolved_hotel_code,
        },
    )
    if trace_id:
        request.metadata.setdefault("trace_id", trace_id)
    request.metadata.setdefault("turn_trace_id", turn_trace_id)
    step_trace_service.log_event(
        "chat_message_step_start",
        payload={
            "message_length": len(str(request.message or "")),
            "prefetch_batch_id": prefetch_batch_id,
            "prefetch_source_type": prefetch_source_type,
            "db_session_available": db is not None,
        },
        trace_id=trace_id,
        turn_trace_id=turn_trace_id,
        session_id=str(request.session_id or "").strip(),
        hotel_code=resolved_hotel_code,
        channel=str(request.channel or (request.metadata or {}).get("channel") or "").strip(),
        endpoint=endpoint,
        method=method,
        component="api.routes.chat.send_message",
        step="request_received",
        stage="start",
    )
    _trace_chat_event(
        "chat_message_received",
        trace_id=trace_id,
        turn_trace_id=turn_trace_id,
        http_request=http_request,
        request=request,
        status_code=0,
        extra={
            "db_session_available": db is not None,
        },
    )
    prefetched_services: Optional[list[dict[str, Any]]] = None
    services_prefetch_error = ""
    services_prefetch_started = _perf_counter()
    _svc_cache_ttl_seconds = 300.0
    _svc_cache_hit = False
    _cache_context: ConversationContext | None = None
    _cache_context_mutated = False
    try:
        _request_channel = (
            request.channel
            or (request.metadata or {}).get("channel")
            or None
        )
        _cache_context = await context_manager.get_or_create_context(
            session_id=request.session_id,
            hotel_code=resolved_hotel_code,
            guest_phone=request.guest_phone,
            channel=_request_channel,
            db_session=db,
        )
        if not isinstance(_cache_context.pending_data, dict):
            _cache_context.pending_data = {}
        _pending = _cache_context.pending_data
        _cached_services = _pending.get("_cached_services")
        _cached_ts_raw = _pending.get("_cached_services_ts")
        _cached_ts = 0.0
        try:
            _cached_ts = float(_cached_ts_raw or 0.0)
        except (TypeError, ValueError):
            _cached_ts = 0.0
        if (
            isinstance(_cached_services, list)
            and _cached_services
            and (time.time() - _cached_ts) <= _svc_cache_ttl_seconds
        ):
            prefetched_services = [
                dict(item) for item in _cached_services if isinstance(item, dict)
            ]
            _svc_cache_hit = bool(prefetched_services)
    except Exception:
        _cache_context = None

    try:
        if not _svc_cache_hit:
            db_services = await db_config_service.get_services()
            if isinstance(db_services, list):
                prefetched_services = [dict(item) for item in db_services if isinstance(item, dict)]
                if _cache_context is not None:
                    if not isinstance(_cache_context.pending_data, dict):
                        _cache_context.pending_data = {}
                    _cache_context.pending_data["_cached_services"] = db_services
                    _cache_context.pending_data["_cached_services_ts"] = time.time()
                    _cache_context_mutated = True
    except Exception as _services_exc:
        services_prefetch_error = str(_services_exc)

    if _cache_context_mutated and _cache_context is not None:
        try:
            await context_manager.save_context(_cache_context, db_session=db)
        except Exception:
            pass
    _record_step(
        "prefetch_services",
        services_prefetch_started,
        status="failed" if services_prefetch_error else "completed",
        error=services_prefetch_error,
        details={
            "service_count": len(prefetched_services or []),
            "cache_hit": _svc_cache_hit,
        },
    )
    # Log chat turn start to flow.log + new_detailed.log
    _chat_meta = request.metadata or {}
    _chat_phase = str(_chat_meta.get("phase_id") or _chat_meta.get("phase") or getattr(request, "phase_id", "") or "").strip()
    _chat_guest_info = {k: v for k, v in _chat_meta.items() if k in ("room_number", "guest_name", "guest_id", "entity_id") and v}
    _flow_log_started = _perf_counter()

    async def _log_turn_start_async() -> None:
        try:
            from services.flow_logger import log_chat_turn
            from services.config_service import config_service as _cs

            meta = dict(_chat_meta or {})
            phase = str(_chat_phase or "").strip()
            guest_info = dict(_chat_guest_info or {})
            summary = str(meta.get("conversation_summary") or meta.get("summary") or "").strip()
            all_services = (
                [dict(item) for item in (prefetched_services or []) if isinstance(item, dict)]
                if isinstance(prefetched_services, list) and prefetched_services
                else _cs.get_services()
            )
            available_services = [str(s.get("id") or "") for s in (all_services or []) if s.get("is_active", True)]
            log_chat_turn(
                session_id=str(request.session_id or ""),
                user_message=str(request.message or ""),
                phase=phase,
                available_services=available_services,
                context_history_count=len(meta.get("conversation_history") or []),
                guest_info=guest_info,
                summary=summary,
            )
        except Exception as _flow_exc:
            import sys as _sys

            print(f"[flow_logger] async turn-start log failed: {_flow_exc!r}", file=_sys.stderr, flush=True)

    asyncio.create_task(_log_turn_start_async())
    _record_step(
        "log_chat_turn_start",
        _flow_log_started,
        status="scheduled",
    )

    # Seed the stable session-lead identity as soon as the guest is KNOWN (e.g.
    # from a booking / guest lookup), not only on form submit. The Chat History
    # page resolves a session's display name from new_bot_guests.name OR
    # new_bot_sessions.guest_name; for a pure Q&A session (no form) both are empty
    # and the dashboard falls back to the raw session id. Writing the known name
    # here fixes that. Fill-blanks-only + own DB session + fire-and-forget, so it
    # never overwrites a real lead, blocks the turn, or disturbs any transaction.
    _known_guest_name = str(
        _chat_meta.get("booking_guest_name")
        or _chat_meta.get("guest_name")
        or ""
    ).strip()
    if _known_guest_name:
        _seed_session_id = str(request.session_id or "").strip()
        _seed_hotel_code = str(request.hotel_code or "").strip() or None
        _seed_guest_phone = str(
            _chat_meta.get("booking_guest_phone") or _chat_meta.get("guest_phone") or ""
        ).strip() or None
        _seed_guest_email = str(_chat_meta.get("guest_email") or "").strip() or None
        _seed_channel = str(
            _chat_meta.get("channel") or getattr(request, "channel", "") or ""
        ).strip() or None

        async def _seed_session_identity_async() -> None:
            try:
                from services import session_profile_service
                await session_profile_service.upsert_session_profile(
                    session_id=_seed_session_id,
                    hotel_code=_seed_hotel_code,
                    guest_name=_known_guest_name,
                    guest_phone=_seed_guest_phone,
                    guest_email=_seed_guest_email,
                    channel=_seed_channel,
                    source="guest_lookup",
                )
            except Exception:
                import sys as _sys
                print("[session_profile] early identity seed failed", file=_sys.stderr, flush=True)

        if _seed_session_id:
            asyncio.create_task(_seed_session_identity_async())

    # -- new_detailed_logger: chat turn IN --
    _detailed_turn_in_error = ""
    _detailed_turn_in_started = _perf_counter()
    try:
        new_detailed_logger.log_chat_turn_in(
            session_id=str(request.session_id or ""),
            message=str(request.message or ""),
            hotel_code=str(request.hotel_code or ""),
            channel=str(getattr(request, "channel", "") or _chat_meta.get("channel") or ""),
            phase=_chat_phase,
            trace_id=trace_id,
            turn_trace_id=turn_trace_id,
            guest_info=_chat_guest_info or None,
            history_count=len(_chat_meta.get("conversation_history") or []),
            metadata=_chat_meta,
        )
    except Exception as _ndl_exc:
        _detailed_turn_in_error = str(_ndl_exc)
        import sys as _sys
        print(f"[new_detailed_logger] chat turn IN log failed: {_ndl_exc!r}", file=_sys.stderr, flush=True)
    _record_step(
        "log_new_detailed_turn_in",
        _detailed_turn_in_started,
        status="failed" if _detailed_turn_in_error else "completed",
        error=_detailed_turn_in_error,
    )

    try:
        prefetch_cancel_started = _perf_counter()
        prefetch_cancelled = 0
        if prefetch_source_type != "suggested_action":
            prefetch_cancelled = await suggestion_prefetch_service.cancel_session_prefetch(
                session_id=str(request.session_id or "").strip(),
                reason="typed_input_new_turn",
            )
        _record_step(
            "prefetch_cancel_non_chip_turn",
            prefetch_cancel_started,
            details={
                "source_type": prefetch_source_type,
                "cancelled_tasks": prefetch_cancelled,
            },
        )

        cached_prefetch_payload: Optional[dict[str, Any]] = None
        # Only do prefetch cache lookup for suggestion-chip clicks.
        # Typed free-text often carries a stale batch id and triggers expensive
        # embedding similarity checks that rarely hit.
        # Always attempt prefetch lookup for suggestion-chip clicks.
        # Some UI paths can miss propagating `prefetch_batch_id`; in that case
        # the resolver still has an exact-text path over recent session batches.
        should_check_prefetch = (prefetch_source_type == "suggested_action")
        prefetch_lookup_started = _perf_counter()
        if should_check_prefetch:
            step_trace_service.log_event(
                "chat_message_prefetch_lookup_start",
                payload={
                    "prefetch_batch_id": prefetch_batch_id,
                    "prefetch_source_type": prefetch_source_type,
                },
                trace_id=trace_id,
                turn_trace_id=turn_trace_id,
                session_id=str(request.session_id or "").strip(),
                hotel_code=resolved_hotel_code,
                channel=str(request.channel or (request.metadata or {}).get("channel") or "").strip(),
                endpoint=endpoint,
                method=method,
                component="api.routes.chat.send_message",
                step="prefetch_lookup",
                stage="start",
            )
            cached_prefetch_payload = await suggestion_prefetch_service.resolve_cached_answer(
                session_id=str(request.session_id or "").strip(),
                user_message=str(request.message or "").strip(),
                source_type=prefetch_source_type,
                batch_id=prefetch_batch_id,
                conversation_id=str((request.metadata or {}).get("conversation_id") or "").strip(),
            )
            step_trace_service.log_event(
                "chat_message_prefetch_lookup_end",
                payload={
                    "prefetch_hit": isinstance(cached_prefetch_payload, dict) and bool(cached_prefetch_payload),
                    "prefetch_batch_id": prefetch_batch_id,
                },
                trace_id=trace_id,
                turn_trace_id=turn_trace_id,
                session_id=str(request.session_id or "").strip(),
                hotel_code=resolved_hotel_code,
                channel=str(request.channel or (request.metadata or {}).get("channel") or "").strip(),
                endpoint=endpoint,
                method=method,
                component="api.routes.chat.send_message",
                step="prefetch_lookup",
                stage="completed",
            )
        prefetch_hit = isinstance(cached_prefetch_payload, dict) and bool(cached_prefetch_payload)
        _record_step(
            "prefetch_lookup",
            prefetch_lookup_started,
            details={
                "checked": should_check_prefetch,
                "hit": prefetch_hit,
                "batch_id": prefetch_batch_id,
                "source_type": prefetch_source_type,
            },
        )
        response_generation_started = _perf_counter()
        response = None
        if isinstance(cached_prefetch_payload, dict) and cached_prefetch_payload:
            async with context_manager.session_lock(request.session_id):
                response = await _build_prefetch_cached_response(
                    request=request,
                    hotel_code=resolved_hotel_code,
                    cached_payload=cached_prefetch_payload,
                    db=db,
                )
        prefetch_hit = response is not None
        if response is not None:
            selected_engine = "prefetch_cache"
            step_trace_service.log_event(
                "chat_message_engine_selected",
                payload={"engine": "prefetch_cache"},
                trace_id=trace_id,
                turn_trace_id=turn_trace_id,
                session_id=str(request.session_id or "").strip(),
                hotel_code=resolved_hotel_code,
                channel=str(request.channel or (request.metadata or {}).get("channel") or "").strip(),
                endpoint=endpoint,
                method=method,
                component="api.routes.chat.send_message",
                step="response_generation",
                stage="completed",
            )
        else:
            if should_check_prefetch and not prefetch_hit:
                chip_click_context = await suggestion_prefetch_service.resolve_chip_click_context(
                    session_id=str(request.session_id or "").strip(),
                    user_message=str(request.message or "").strip(),
                    batch_id=prefetch_batch_id,
                    conversation_id=str((request.metadata or {}).get("conversation_id") or "").strip(),
                )
                if isinstance(chip_click_context, dict) and chip_click_context:
                    metadata = dict(request.metadata or {})
                    metadata["chip_origin_responder"] = str(
                        chip_click_context.get("origin_responder") or metadata.get("chip_origin_responder") or ""
                    ).strip()
                    metadata["chip_origin_service_id"] = str(
                        chip_click_context.get("origin_service_id") or metadata.get("chip_origin_service_id") or ""
                    ).strip()
                    metadata["chip_target_service_id"] = str(
                        chip_click_context.get("target_service_id") or metadata.get("chip_target_service_id") or ""
                    ).strip()
                    metadata["chip_active_property_ids"] = list(
                        chip_click_context.get("active_property_ids") or metadata.get("chip_active_property_ids") or []
                    )
                    metadata["chip_selected_book_ids"] = list(
                        chip_click_context.get("selected_book_ids") or metadata.get("chip_selected_book_ids") or []
                    )
                    metadata["chip_turn_trace_id"] = str(
                        chip_click_context.get("turn_trace_id") or metadata.get("chip_turn_trace_id") or ""
                    ).strip()
                    metadata["chip_evidence_fingerprint"] = str(
                        chip_click_context.get("evidence_fingerprint")
                        or metadata.get("chip_evidence_fingerprint")
                        or ""
                    ).strip()
                    request.metadata = metadata
            # ── THE GUEST'S CHAT ROW MUST EXIST BEFORE THE TICKET DOES ─────
            # Same reason as the WS route: a ticket raised on this turn carries
            # the guest row's KEBO_ENGAGE_SESSIONS.id as `message_id`, and the
            # dashboard needs it to draw the ticket inline in the conversation.
            # This route is not just the harness — the embedded widget falls back
            # to it whenever the socket breaks (see safeHttpFallback in
            # useWebSocketChat), so a real guest on the Zone website lands here.
            #
            # Written BEFORE the turn and fire-and-forget: we never wait for the
            # id (the write is ~1.5s against the kebo DB), because
            # ticketing_service resolves the row at POST time instead.
            if bool(getattr(settings, "engage_session_logging_enabled", True)):
                try:
                    from services.engage_session_service import engage_session_service as _ess_pre_h
                    from services.customer_details_service import customer_details_service as _cds_pre_h

                    _ph_widget, _ph_group = _cds_pre_h.resolve_widget_group(resolved_hotel_code)
                    _ph_meta = request.metadata if isinstance(request.metadata, dict) else {}
                    _ph_user = str(request.message or "").strip()
                    # Kebo hotels only — _ph_widget is empty for every other tenant.
                    if _ph_widget and _ph_user:
                        _ph_sid = str(request.session_id or "").strip()
                        _ph_group = _ph_group or str(
                            _ph_meta.get("group_id") or _ph_meta.get("groupId") or ""
                        ).strip()
                        _ph_entity = str(
                            _ph_meta.get("entity_id") or _ph_meta.get("entityId") or ""
                        ).strip()
                        _ph_conv = (
                            str(_ph_meta.get("conversation_id") or _ph_meta.get("conversationId") or "").strip()
                            or _ph_sid
                        )
                        _ess_pre_h.write_turn_bg(
                            session_id=_ph_sid, sender="USER", message=_ph_user,
                            entity_id=_ph_entity, group_id=_ph_group, widget_id=_ph_widget,
                            conversation_id=_ph_conv, message_type="text",
                        )
                        _http_user_row_written = True
                except Exception:
                    logger.warning("pre-turn guest row write skipped", exc_info=True)

            # ── Pre-stored location intro (LLM-free), HTTP parity with WS ──
            # Serialize the whole turn's context read-modify-write per session so a
            # concurrent lead-form submit (or another turn) on the SAME session
            # can't clobber the property scope this turn pins (Cause-B fix).
            async with context_manager.session_lock(request.session_id):
                intro_response = await _maybe_build_property_intro_response(
                    request=request,
                    hotel_code=resolved_hotel_code,
                    source_type=prefetch_source_type,
                    db=db,
                ) or await _maybe_build_location_intro_response(
                    request=request,
                    hotel_code=resolved_hotel_code,
                    source_type=prefetch_source_type,
                    db=db,
                )
                if intro_response is not None:
                    response = intro_response
                    selected_engine = "location_intro"
                else:
                    response = await chat_service.process_message(
                        request,
                        db_session=db,
                        prefetched_services=prefetched_services,
                        defer_summary_refresh=True,
                    )
                    selected_engine = "chat_service"
            step_trace_service.log_event(
                "chat_message_engine_selected",
                payload={"engine": selected_engine},
                trace_id=trace_id,
                turn_trace_id=turn_trace_id,
                session_id=str(request.session_id or "").strip(),
                hotel_code=resolved_hotel_code,
                channel=str(request.channel or (request.metadata or {}).get("channel") or "").strip(),
                endpoint=endpoint,
                method=method,
                component="api.routes.chat.send_message",
                step="response_generation",
                stage="completed",
            )
        _record_step(
            "response_generation",
            response_generation_started,
            details={"engine": selected_engine},
        )
        # The location-intro body is curated (never contains "Default property"
        # placeholders) and carries intentional paragraph/heading formatting.
        # _strip_default_property_references collapses runs of whitespace to a
        # single space, which would flatten the blank lines and un-anchor the
        # "### " sub-headers — so skip it for that source.
        _is_location_intro = (
            isinstance(response, ChatResponse)
            and isinstance(response.metadata, dict)
            and response.metadata.get("response_source") == "location_intro"
        )
        if isinstance(response, ChatResponse) and not _is_location_intro:
            response.message = _strip_default_property_references(str(response.message or ""))
            if isinstance(response.metadata, dict):
                if isinstance(response.metadata.get("display_message"), str):
                    response.metadata["display_message"] = _strip_default_property_references(
                        str(response.metadata.get("display_message") or "")
                    )
                if isinstance(response.metadata.get("canonical_message"), str):
                    response.metadata["canonical_message"] = _strip_default_property_references(
                        str(response.metadata.get("canonical_message") or "")
                    )
        enrich_fields_started = _perf_counter()
        if not isinstance(response.metadata, dict):
            response.metadata = {}
        _enrich_service_llm_display_fields(response)
        _record_step("enrich_response_fields", enrich_fields_started)
        prefetch_fast_return = bool(
            selected_engine == "prefetch_cache"
            and isinstance(response.metadata, dict)
            and response.metadata.get("prefetch_cache_hit")
        )
        form_trigger_started = _perf_counter()
        display_attach_started = _perf_counter()
        smart_images_started = _perf_counter()
        if prefetch_fast_return:
            canonical_message = str(response.message or "").strip()
            display_message = str(response.display_message or "").strip() or canonical_message
            response.display_message = display_message or None
            response.metadata["display_message"] = display_message
            response.metadata["canonical_message"] = canonical_message
            response.metadata["prefetch_fast_return"] = True

            # FORM-FIELDS RE-PREFILL ON CACHE HIT.
            # The prefetch payload is keyed by chip/intent similarity, not by
            # session_id — so a cached payload can carry form_fields whose
            # `default_value` was computed for a totally different session.
            # `_inject_form_trigger` is skipped below for latency, but that
            # means apply_prefill never runs and stale default_values bleed
            # through into the wrong session's form. Fix: when the cached
            # payload contains form_fields, re-run apply_prefill against the
            # CURRENT session's context. The display-message + smart-images
            # paths stay skipped — only the prefill is re-done.
            #
            # Gated by `chat_prefetch_reprefill_form_fields` (default True).
            # Failures here are swallowed: worst case the user sees the
            # cached default_value, which is current (broken) behavior.
            _reprefill_form_status = "skipped"
            _reprefill_form_reason = "no_form_fields_in_cache"
            if bool(getattr(settings, "chat_prefetch_reprefill_form_fields", True)):
                _cached_form_fields = (
                    response.metadata.get("form_fields")
                    if isinstance(response.metadata, dict)
                    else None
                )
                if isinstance(_cached_form_fields, list) and _cached_form_fields:
                    try:
                        from services.form_fields_service import apply_prefill as _reprefill_apply
                        # Build conversation excerpt the same way _inject_form_trigger does.
                        _reprefill_excerpt = ""
                        if bool(getattr(settings, "chat_form_prefill_use_history", True)) and _cache_context is not None:
                            _r_msgs = list(getattr(_cache_context, "messages", []) or [])[-8:]
                            _r_parts: list[str] = []
                            _r_summary = str(getattr(_cache_context, "summary", "") or "").strip()
                            if _r_summary:
                                _r_parts.append(f"SUMMARY: {_r_summary}")
                            for _m in _r_msgs:
                                _role_raw = getattr(_m, "role", None)
                                _role = getattr(_role_raw, "value", None) or str(_role_raw or "").lower()
                                if _role not in {"user", "assistant"}:
                                    continue
                                _content = str(getattr(_m, "content", "") or "").strip()
                                if not _content:
                                    continue
                                _r_parts.append(f"{_role.upper()}: {_content}")
                            _reprefill_excerpt = "\n".join(_r_parts)
                        await _reprefill_apply(
                            _cached_form_fields,
                            req_meta=request.metadata if isinstance(request.metadata, dict) else None,
                            context=_cache_context,
                            triggering_user_message=str(request.message or ""),
                            service=None,
                            allow_llm=True,
                            conversation_excerpt=_reprefill_excerpt,
                            bot_response_text=str(
                                response.metadata.get("canonical_message")
                                or response.metadata.get("display_message")
                                or getattr(response, "message", "")
                                or ""
                            ),
                        )
                        # The cached_payload may itself reference the same field
                        # dicts (since the prefetch service stores the metadata
                        # by reference). Reflect the cleaned values back into
                        # response.metadata explicitly to be safe.
                        response.metadata["form_fields"] = _cached_form_fields
                        _reprefill_form_status = "completed"
                        _reprefill_form_reason = "reprefill_done"
                    except Exception as _reprefill_exc:
                        _reprefill_form_status = "failed"
                        _reprefill_form_reason = f"{type(_reprefill_exc).__name__}: {_reprefill_exc}"
                        import sys as _sys_rp
                        print(
                            f"[prefetch_fast_return] form_fields re-prefill failed: {_reprefill_exc!r}",
                            file=_sys_rp.stderr,
                            flush=True,
                        )
            _record_step(
                "inject_form_trigger",
                form_trigger_started,
                status=_reprefill_form_status if _reprefill_form_status != "skipped" else "skipped",
                details={
                    "reason": _reprefill_form_reason
                    if _reprefill_form_status != "skipped"
                    else "prefetch_cache_fast_return"
                },
            )
            _record_step(
                "attach_display_message",
                display_attach_started,
                status="skipped",
                details={"reason": "prefetch_cache_fast_return"},
            )
            _record_step(
                "attach_smart_images",
                smart_images_started,
                status="skipped",
                details={"reason": "prefetch_cache_fast_return"},
            )
        else:
            form_trigger_error = ""
            try:
                await _inject_form_trigger(
                    response,
                    user_message=request.message,
                    prefetched_services=prefetched_services,
                    request_metadata=request.metadata if isinstance(request.metadata, dict) else None,
                    context=_cache_context,
                )
            except Exception as _form_exc:
                form_trigger_error = str(_form_exc)
                import sys as _sys
                print(f"[form_trigger] EXCEPTION: {_form_exc!r}", file=_sys.stderr, flush=True)
            _record_step(
                "inject_form_trigger",
                form_trigger_started,
                status="failed" if form_trigger_error else "completed",
                error=form_trigger_error,
            )
            # Topic images inline (no LLM, cached read) so they ride THIS
            # response. The deferred smart-image task below runs after the
            # payload is built, so anything it attaches misses this turn.
            try:
                await _attach_topic_images(
                    response=response,
                    request=request,
                    hotel_code=resolved_hotel_code,
                )
            except Exception as _topic_exc:
                logger.warning("_attach_topic_images failed (non-fatal): %r", _topic_exc)

            # Events get the same one-tap path as rooms: the answer, then a
            # button that opens the enquiry form.
            try:
                await _attach_event_cta(response=response, hotel_code=resolved_hotel_code, context=await context_manager.get_context(request.session_id, db_session=None))
            except Exception as _evt_exc:
                logger.warning("_attach_event_cta failed (non-fatal): %r", _evt_exc)

            # Every button for this turn is decided by now — drop any sentence
            # pointing at one that is not on screen.
            try:
                _strip_unavailable_button_mentions(response)
            except Exception as _btn_exc:
                logger.warning("button-mention cleanup failed (non-fatal): %r", _btn_exc)

            display_attach_error = ""
            smart_images_error = ""
            display_timeout_seconds = max(
                0.2,
                float(getattr(settings, "chat_display_beautifier_timeout_ms", 900) or 900) / 1000.0,
            )
            _configured_smart_image_timeout_ms, _smart_image_hard_cap_ms = _resolve_smart_image_timeouts_ms()
            smart_image_timeout_seconds = max(
                0.05,
                float(min(_configured_smart_image_timeout_ms, _smart_image_hard_cap_ms)) / 1000.0,
            )
            display_task = asyncio.create_task(
                asyncio.wait_for(_attach_display_message(response), timeout=display_timeout_seconds)
            )
            smart_images_task = asyncio.create_task(
                asyncio.wait_for(
                    _attach_smart_images(
                        response=response,
                        request=request,
                        hotel_code=resolved_hotel_code,
                        trace_id=trace_id,
                        turn_trace_id=turn_trace_id,
                    ),
                    timeout=smart_image_timeout_seconds,
                )
            )
            if bool(getattr(settings, "chat_defer_display_enrichment", False)):
                # Off the critical path: let display-beautifier + smart images
                # finish in the background so the reply returns now (their DB-side
                # enrichment still lands for chat history). Results left None so
                # the exception handlers below no-op, and the steps record ~0ms.
                for _bg_task in (display_task, smart_images_task):
                    _BACKGROUND_ENRICH_TASKS.add(_bg_task)
                    _bg_task.add_done_callback(_reap_background_enrich_task)
                display_result = smart_images_result = None
            else:
                display_result, smart_images_result = await asyncio.gather(
                    display_task,
                    smart_images_task,
                    return_exceptions=True,
                )
            if isinstance(display_result, Exception):
                if isinstance(display_result, asyncio.TimeoutError):
                    if not isinstance(response.metadata, dict):
                        response.metadata = {}
                    response.metadata["display_beautifier_timeout"] = True
                    response.metadata.setdefault(
                        "display_beautifier_fallback_reason",
                        "beautifier_timeout",
                    )
                else:
                    display_attach_error = str(display_result)
            if isinstance(smart_images_result, Exception):
                if isinstance(smart_images_result, asyncio.TimeoutError):
                    if not isinstance(response.metadata, dict):
                        response.metadata = {}
                    response.metadata["smart_image_tool_timeout"] = True
                    response.metadata.setdefault(
                        "smart_image_selection_reason",
                        "attach_timeout",
                    )
                else:
                    smart_images_error = str(smart_images_result)
            _record_step(
                "attach_display_message",
                display_attach_started,
                status="failed" if display_attach_error else "completed",
                error=display_attach_error,
            )
            _record_step(
                "attach_smart_images",
                smart_images_started,
                status="failed" if smart_images_error else "completed",
                error=smart_images_error,
            )
        # Booking-button marker (room agent → website link button). Runs for both
        # cache and non-cache paths.
        try:
            # The active property scope is stamped onto response.metadata by the
            # orchestration layer (chat_service). The booking button shows only
            # when EXACTLY ONE property is in focus; with two or more in scope we
            # don't show it (the guest hasn't narrowed down yet).
            _resp_meta = response.metadata if isinstance(response.metadata, dict) else {}
            _active_ids = [str(x).strip() for x in (_resp_meta.get("active_property_ids") or []) if str(x).strip()]
            _active_names = [str(x).strip() for x in (_resp_meta.get("active_property_names") or []) if str(x).strip()]
            # Fallback: if the scope wasn't stamped on the response, read it from
            # the in-memory session context (no DB hit) — this is the post-turn
            # active scope the orchestrator persisted.
            if not _active_ids:
                try:
                    _ctx_pc = await context_manager.get_context(session_id, db_session=None)
                    _pc_pd = (_ctx_pc.pending_data if _ctx_pc and isinstance(_ctx_pc.pending_data, dict) else {})
                    _active_ids = [str(x).strip() for x in (_pc_pd.get("_active_property_ids") or []) if str(x).strip()]
                    if not _active_names:
                        _active_names = [str(x).strip() for x in (_pc_pd.get("_active_property_names") or []) if str(x).strip()]
                except Exception:
                    pass
            # Build a display name for the single active property (prefer the
            # stamped name; else title-case the slug id).
            _room_prop_names: list[str] = []
            _room_prop_ids: list[str] = []
            if len(_active_ids) == 1:
                _room_prop_ids = [_active_ids[0]]
                if _active_names:
                    _room_prop_names = [_active_names[0]]
                else:
                    _room_prop_names = [_active_ids[0].replace("_", " ").title()]
            _apply_booking_cta_marker(
                response,
                hotel_code=resolved_hotel_code,
                lead_active=bool((request.metadata or {}).get("booking_lead_active")) if isinstance(request.metadata, dict) else False,
                link_clicked=bool((request.metadata or {}).get("booking_link_clicked")) if isinstance(request.metadata, dict) else False,
                room_property_names=_room_prop_names,
                room_property_ids=_room_prop_ids,
                active_property_count=len(_active_ids),
                user_message=str(getattr(request, "message", "") or ""),
                # THE LEDGER DECIDES, NOT THE BROWSER. The widget only sends
                # this flag once it has seen a submission in its own tab, so a
                # reload, a second tab or a restored session all lose it while
                # the booking plainly still exists on the server. Read the
                # durable ledger and fall back to the browser's hint.
                details_submitted=(
                    booking_details_submitted(_cta_ctx)
                    if (_cta_ctx := await context_manager.get_context(request.session_id, db_session=None)) is not None
                    and team_booking_enabled(resolved_hotel_code)
                    else bool((request.metadata or {}).get("booking_details_submitted"))
                    if isinstance(request.metadata, dict) else False
                ),
            )
        except Exception:
            pass
        # Attach the menu PDF download card when the guest asked a dining service
        # for the menu file. Deterministic + cheap (no LLM); mirrors booking CTA.
        await _attach_menu_document(
            response=response,
            request=request,
            hotel_code=resolved_hotel_code,
        )
        metadata_finalize_started = _perf_counter()
        if trace_id:
            response.metadata.setdefault("trace_id", trace_id)
        response.metadata.setdefault("turn_trace_id", turn_trace_id)
        # Persist the rich display (beautified text + image cards + form schema)
        # onto this assistant turn so chat history replays what the guest saw.
        # Best-effort, own DB session — never blocks/breaks the response.
        try:
            _disp_payload = _build_turn_display_payload(response)
            if _disp_payload and session_id:
                await context_manager.set_turn_display_payload(
                    session_id,
                    _disp_payload,
                    conversation_uid=str(
                        (request.metadata or {}).get("conversation_id")
                        or (request.metadata or {}).get("conversationId")
                        or ""
                    ).strip(),
                    db_session=None,
                )
        except Exception:
            pass
        _record_step("response_metadata_finalize", metadata_finalize_started)
        evaluation_started = _perf_counter()
        _record_evaluation_event(request, response, trace_id)
        _record_step("evaluation_record", evaluation_started)
        _resp_meta = response.metadata if isinstance(response.metadata, dict) else {}

        def _log_turn_end_group_a() -> None:
            flow_response_log_error = ""
            flow_response_log_started = _perf_counter()
            try:
                from services.flow_logger import log_chat_response, log_orchestration_decision
                routed_svc = str(
                    _resp_meta.get("response_source")
                    or _resp_meta.get("routing_path")
                    or _resp_meta.get("classified_intent")
                    or "unknown"
                )
                log_chat_response(
                    session_id=str(request.session_id or ""),
                    routed_service=routed_svc,
                    final_response=str(response.message or ""),
                )
                orchestration = _resp_meta.get("orchestration_decision") or _resp_meta.get("decision")
                if orchestration and isinstance(orchestration, dict):
                    log_orchestration_decision(
                        session_id=str(request.session_id or ""),
                        decision=orchestration,
                    )
            except Exception as _flow_resp_exc:
                flow_response_log_error = str(_flow_resp_exc)
            _record_step(
                "log_chat_response_and_orchestration",
                flow_response_log_started,
                status="failed" if flow_response_log_error else "completed",
                error=flow_response_log_error,
            )

        def _log_turn_end_group_b() -> None:
            observability_service.log_event(
                "chat_message_processed",
                {
                    "trace_id": trace_id,
                    "session_id": request.session_id,
                    "hotel_code": request.hotel_code,
                    "channel": request.channel or (request.metadata or {}).get("channel") or "web",
                    "intent": str((_resp_meta or {}).get("classified_intent") or ""),
                    "state": str(getattr(response.state, "value", response.state) or ""),
                    "routing_path": str((_resp_meta or {}).get("routing_path") or ""),
                    "response_source": str((_resp_meta or {}).get("response_source") or ""),
                },
            )
            _trace_chat_event(
                "chat_message_processed",
                trace_id=trace_id,
                turn_trace_id=turn_trace_id,
                http_request=http_request,
                request=request,
                response=response,
                status_code=200,
                extra={
                    "db_fallback": False,
                },
            )
            step_trace_service.log_event(
                "chat_message_step_end",
                payload={
                    "db_fallback": False,
                    "response_state": str(getattr(response.state, "value", response.state) or ""),
                    "response_source": str((_resp_meta or {}).get("response_source") or ""),
                    "ticket_created": bool((_resp_meta or {}).get("ticket_created", False)),
                    "duration_ms": round((_perf_counter() - _turn_start) * 1000, 2),
                },
                trace_id=trace_id,
                turn_trace_id=turn_trace_id,
                session_id=str(request.session_id or "").strip(),
                hotel_code=resolved_hotel_code,
                channel=str(request.channel or (request.metadata or {}).get("channel") or "").strip(),
                endpoint=endpoint,
                method=method,
                status_code=200,
                component="api.routes.chat.send_message",
                step="response_emit",
                stage="completed",
            )

        def _log_turn_end_group_c() -> None:
            postprocess_started = _perf_counter()
            conversation_audit_service.log_turn(
                request=request,
                response=response,
                trace_id=trace_id,
                db_fallback=False,
            )
            turn_diagnostics_service.log_turn_end(
                request=request,
                response=response,
                db_fallback=False,
                error="",
            )
            _record_step("postprocess_and_turn_end", postprocess_started)
            # -- new_detailed_logger: chat turn SUCCESS --
            try:
                new_detailed_logger.log_chat_turn_out_success(
                    session_id=str(request.session_id or ""),
                    bot_reply=str(response.message or ""),
                    display_message=str(getattr(response, "display_message", "") or ""),
                    intent=str(_resp_meta.get("classified_intent") or ""),
                    routing_path=str(_resp_meta.get("routing_path") or ""),
                    response_source=str(_resp_meta.get("response_source") or ""),
                    service_label=str(getattr(response, "service_llm_label", "") or ""),
                    confidence=getattr(response, "service_llm_confidence", None),
                    state=str(getattr(response.state, "value", response.state) or ""),
                    duration_ms=round((_perf_counter() - _turn_start) * 1000, 2),
                    trace_id=trace_id,
                    turn_trace_id=turn_trace_id,
                    db_fallback=False,
                    ticket_created=bool(_resp_meta.get("ticket_created", False)),
                    suggestions=list(_resp_meta.get("suggestions") or []) or None,
                    orchestration=_resp_meta.get("orchestration_decision") or _resp_meta.get("decision") or None,
                    full_metadata=_resp_meta,
                )
            except Exception as _ndl_exc:
                import sys as _sys
                print(
                    f"[new_detailed_logger] chat turn SUCCESS log failed: {_ndl_exc!r}",
                    file=_sys.stderr,
                    flush=True,
                )

        async def _log_turn_end_async() -> None:
            await asyncio.gather(
                asyncio.to_thread(_log_turn_end_group_a),
                asyncio.to_thread(_log_turn_end_group_b),
                asyncio.to_thread(_log_turn_end_group_c),
                return_exceptions=True,
            )

        turn_end_log_schedule_started = _perf_counter()
        asyncio.create_task(_log_turn_end_async())
        _record_step(
            "log_turn_end_async",
            turn_end_log_schedule_started,
            status="scheduled",
        )
        _timing_status = "success"
        _timing_status_code = 200
        _timing_db_fallback = False
        _timing_error = ""
        _timing_response_message = str(response.message or "")
        _timing_response_state = str(getattr(response.state, "value", response.state) or "")
        _timing_response_source = str((_resp_meta or {}).get("response_source") or "")
        try:
            asyncio.create_task(
                _warm_prefetch_for_reply_suggestions(
                    request=request,
                    response=response,
                    hotel_code=resolved_hotel_code,
                )
            )
        except Exception:
            pass
        _queue_summary_refresh(
            session_id=str(request.session_id or ""),
            hotel_code=resolved_hotel_code,
        )
        # Mirror this conversational turn into KEBO_ENGAGE_SESSIONS so the kebo
        # dashboard shows the FULL transcript (not just form submits). Clean text
        # only, fire-and-forget — never blocks/breaks the response. SCOPED to
        # kebo hotels only; KePSLA/other tenants keep their own history untouched.
        if bool(getattr(settings, "engage_session_logging_enabled", True)):
            try:
                from services.engage_session_service import engage_session_service
                from services.customer_details_service import customer_details_service as _cds_t

                _t_widget, _t_group = _cds_t.resolve_widget_group(resolved_hotel_code)
                # Kebo hotels only: _t_widget is non-empty only for mapped hotels
                # (Zone). For KePSLA/other tenants the writes below are skipped,
                # so their KES / chat history is never touched.
                _t_meta = request.metadata if isinstance(request.metadata, dict) else {}
                _t_sid = str(request.session_id or "").strip()
                _t_group = _t_group or str(_t_meta.get("group_id") or _t_meta.get("groupId") or "").strip()
                _t_entity = str(_t_meta.get("entity_id") or _t_meta.get("entityId") or "").strip()
                _t_conv = (
                    str(_t_meta.get("conversation_id") or _t_meta.get("conversationId") or "").strip()
                    or _t_sid
                )
                _t_user = str(request.message or "").strip()
                _t_bot = str(response.message or "").strip()
                # Skipped when the pre-turn block above already wrote this row.
                if _t_widget and _t_user and not _http_user_row_written:
                    engage_session_service.write_turn_bg(
                        session_id=_t_sid, sender="USER", message=_t_user,
                        entity_id=_t_entity, group_id=_t_group, widget_id=_t_widget,
                        conversation_id=_t_conv, message_type="text",
                    )
                if _t_widget and _t_bot:
                    engage_session_service.write_turn_bg(
                        session_id=_t_sid, sender="BOT", message=_t_bot,
                        entity_id=_t_entity, group_id=_t_group, widget_id=_t_widget,
                        conversation_id=_t_conv, message_type="text",
                    )
            except Exception:
                logger.warning("chat turn engage_session logging skipped", exc_info=True)
        if _dedup_owned and _dedup_key and response is not None:
            try:
                await message_dedup.complete(_dedup_key, response.model_dump())
                _dedup_owned = False  # completed — finally must not release it
            except Exception:
                pass
        return response
    except OperationalError as e:
        db_first_mode = bool(getattr(settings, "chat_db_first_mode", False))
        allow_inmemory_fallback = bool(getattr(settings, "chat_db_allow_inmemory_fallback", True))
        if db_first_mode and not allow_inmemory_fallback:
            observability_service.log_event(
                "chat_message_failed_db_required",
                {
                    "trace_id": trace_id,
                    "session_id": request.session_id,
                    "hotel_code": request.hotel_code,
                    "error": str(e),
                },
            )
            conversation_audit_service.log_failed_turn(
                request=request,
                trace_id=trace_id,
                error=str(e),
                db_fallback=False,
            )
            _trace_chat_event(
                "chat_message_failed_db_required",
                trace_id=trace_id,
                turn_trace_id=turn_trace_id,
                http_request=http_request,
                request=request,
                status_code=503,
                error=str(e),
                extra={
                    "db_fallback": False,
                    "db_first_mode": True,
                    "db_required": True,
                },
            )
            turn_diagnostics_service.log_turn_end(
                request=request,
                response=None,
                db_fallback=False,
                error=f"db_required: {str(e)}",
            )
            # â”€â”€ new_detailed_logger: failed â€” db_required â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
            try:
                new_detailed_logger.log_chat_turn_out_failed(
                    session_id=str(request.session_id or ""),
                    message=str(request.message or ""),
                    hotel_code=str(request.hotel_code or ""),
                    trace_id=trace_id,
                    turn_trace_id=turn_trace_id,
                    duration_ms=round((_perf_counter() - _turn_start) * 1000, 2),
                    error_type="OperationalError(db_required)",
                    error_msg=str(e),
                    tb=_traceback.format_exc(),
                    db_fallback=False,
                    http_status=503,
                )
            except Exception:
                pass
            step_trace_service.log_event(
                "chat_message_step_end",
                payload={
                    "db_fallback": False,
                    "db_required": True,
                    "duration_ms": round((_perf_counter() - _turn_start) * 1000, 2),
                },
                trace_id=trace_id,
                turn_trace_id=turn_trace_id,
                session_id=str(request.session_id or "").strip(),
                hotel_code=resolved_hotel_code,
                channel=str(request.channel or (request.metadata or {}).get("channel") or "").strip(),
                endpoint=endpoint,
                method=method,
                status_code=503,
                component="api.routes.chat.send_message",
                step="response_emit",
                stage="failed",
                error=str(e),
            )
            _timing_status = "failed_db_required"
            _timing_status_code = 503
            _timing_db_fallback = False
            _timing_error = str(e)
            _timing_response_message = ""
            _timing_response_state = ""
            _timing_response_source = ""
            raise HTTPException(
                status_code=503,
                detail="Database is unavailable. DB-first mode requires database connectivity.",
            )

        print(f"API DB Error (fallback to in-memory): {e}")
        try:
            fallback_generation_started = _perf_counter()
            response = await chat_service.process_message(
                request,
                db_session=None,
                prefetched_services=prefetched_services,
                defer_summary_refresh=True,
            )
            _record_step(
                "response_generation_db_fallback",
                fallback_generation_started,
                details={"engine": "chat_service_fallback"},
            )
            if isinstance(response, ChatResponse):
                response.message = _strip_default_property_references(str(response.message or ""))
                if isinstance(response.metadata, dict):
                    if isinstance(response.metadata.get("display_message"), str):
                        response.metadata["display_message"] = _strip_default_property_references(
                            str(response.metadata.get("display_message") or "")
                        )
                    if isinstance(response.metadata.get("canonical_message"), str):
                        response.metadata["canonical_message"] = _strip_default_property_references(
                            str(response.metadata.get("canonical_message") or "")
                        )
            fallback_enrich_started = _perf_counter()
            if not isinstance(response.metadata, dict):
                response.metadata = {}
            _enrich_service_llm_display_fields(response)
            _record_step("enrich_response_fields_db_fallback", fallback_enrich_started)
            fallback_form_started = _perf_counter()
            fallback_form_error = ""
            try:
                await _inject_form_trigger(
                    response,
                    user_message=request.message,
                    prefetched_services=prefetched_services,
                    request_metadata=request.metadata if isinstance(request.metadata, dict) else None,
                    context=_cache_context,
                )
            except Exception as _fallback_form_exc:
                fallback_form_error = str(_fallback_form_exc)
            _record_step(
                "inject_form_trigger_db_fallback",
                fallback_form_started,
                status="failed" if fallback_form_error else "completed",
                error=fallback_form_error,
            )
            fallback_display_started = _perf_counter()
            fallback_images_started = _perf_counter()
            fallback_display_error = ""
            fallback_images_error = ""
            fallback_display_task = asyncio.create_task(_attach_display_message(response))
            fallback_images_task = asyncio.create_task(
                _attach_smart_images(
                    response=response,
                    request=request,
                    hotel_code=resolved_hotel_code,
                    trace_id=trace_id,
                    turn_trace_id=turn_trace_id,
                )
            )
            fallback_display_result, fallback_images_result = await asyncio.gather(
                fallback_display_task,
                fallback_images_task,
                return_exceptions=True,
            )
            if isinstance(fallback_display_result, Exception):
                fallback_display_error = str(fallback_display_result)
            if isinstance(fallback_images_result, Exception):
                fallback_images_error = str(fallback_images_result)
            await _attach_menu_document(
                response=response,
                request=request,
                hotel_code=resolved_hotel_code,
            )
            _record_step(
                "attach_display_message_db_fallback",
                fallback_display_started,
                status="failed" if fallback_display_error else "completed",
                error=fallback_display_error,
            )
            _record_step(
                "attach_smart_images_db_fallback",
                fallback_images_started,
                status="failed" if fallback_images_error else "completed",
                error=fallback_images_error,
            )
            fallback_metadata_started = _perf_counter()
            response.metadata["db_fallback"] = True
            if trace_id:
                response.metadata.setdefault("trace_id", trace_id)
            response.metadata.setdefault("turn_trace_id", turn_trace_id)
            _record_step("response_metadata_finalize_db_fallback", fallback_metadata_started)
            fallback_eval_started = _perf_counter()
            _record_evaluation_event(request, response, trace_id)
            _record_step("evaluation_record_db_fallback", fallback_eval_started)
            _resp_meta = response.metadata if isinstance(response.metadata, dict) else {}

            def _log_turn_end_db_fallback_group_a() -> None:
                # Keep group slots aligned with normal success path.
                return

            def _log_turn_end_db_fallback_group_b() -> None:
                observability_service.log_event(
                    "chat_message_processed_db_fallback",
                    {
                        "trace_id": trace_id,
                        "session_id": request.session_id,
                        "hotel_code": request.hotel_code,
                        "error": str(e),
                    },
                )
                _trace_chat_event(
                    "chat_message_processed_db_fallback",
                    trace_id=trace_id,
                    turn_trace_id=turn_trace_id,
                    http_request=http_request,
                    request=request,
                    response=response,
                    status_code=200,
                    extra={
                        "db_fallback": True,
                        "primary_db_error": str(e),
                    },
                )
                step_trace_service.log_event(
                    "chat_message_step_end",
                    payload={
                        "db_fallback": True,
                        "response_state": str(getattr(response.state, "value", response.state) or ""),
                        "response_source": str((_resp_meta or {}).get("response_source") or ""),
                        "duration_ms": round((_perf_counter() - _turn_start) * 1000, 2),
                    },
                    trace_id=trace_id,
                    turn_trace_id=turn_trace_id,
                    session_id=str(request.session_id or "").strip(),
                    hotel_code=resolved_hotel_code,
                    channel=str(request.channel or (request.metadata or {}).get("channel") or "").strip(),
                    endpoint=endpoint,
                    method=method,
                    status_code=200,
                    component="api.routes.chat.send_message",
                    step="response_emit",
                    stage="completed",
                )

            def _log_turn_end_db_fallback_group_c() -> None:
                fallback_postprocess_started = _perf_counter()
                conversation_audit_service.log_turn(
                    request=request,
                    response=response,
                    trace_id=trace_id,
                    db_fallback=True,
                )
                turn_diagnostics_service.log_turn_end(
                    request=request,
                    response=response,
                    db_fallback=True,
                    error=f"primary_db_failed: {str(e)}",
                )
                _record_step("postprocess_and_turn_end_db_fallback", fallback_postprocess_started)
                # -- new_detailed_logger: SUCCESS via DB-fallback path --
                try:
                    new_detailed_logger.log_chat_turn_out_success(
                        session_id=str(request.session_id or ""),
                        bot_reply=str(response.message or ""),
                        display_message=str(getattr(response, "display_message", "") or ""),
                        intent=str(_resp_meta.get("classified_intent") or ""),
                        routing_path=str(_resp_meta.get("routing_path") or ""),
                        response_source=str(_resp_meta.get("response_source") or ""),
                        service_label=str(getattr(response, "service_llm_label", "") or ""),
                        confidence=getattr(response, "service_llm_confidence", None),
                        state=str(getattr(response.state, "value", response.state) or ""),
                        duration_ms=round((_perf_counter() - _turn_start) * 1000, 2),
                        trace_id=trace_id,
                        turn_trace_id=turn_trace_id,
                        db_fallback=True,
                        ticket_created=bool(_resp_meta.get("ticket_created", False)),
                        suggestions=list(_resp_meta.get("suggestions") or []) or None,
                        orchestration=_resp_meta.get("orchestration_decision") or None,
                        full_metadata=_resp_meta,
                    )
                except Exception:
                    pass

            async def _log_turn_end_db_fallback_async() -> None:
                await asyncio.gather(
                    asyncio.to_thread(_log_turn_end_db_fallback_group_a),
                    asyncio.to_thread(_log_turn_end_db_fallback_group_b),
                    asyncio.to_thread(_log_turn_end_db_fallback_group_c),
                    return_exceptions=True,
                )

            fallback_turn_end_log_schedule_started = _perf_counter()
            asyncio.create_task(_log_turn_end_db_fallback_async())
            _record_step(
                "log_turn_end_db_fallback_async",
                fallback_turn_end_log_schedule_started,
                status="scheduled",
            )
            _timing_status = "success_db_fallback"
            _timing_status_code = 200
            _timing_db_fallback = True
            _timing_error = str(e)
            _timing_response_message = str(response.message or "")
            _timing_response_state = str(getattr(response.state, "value", response.state) or "")
            _timing_response_source = str((_resp_meta or {}).get("response_source") or "")
            _queue_summary_refresh(
                session_id=str(request.session_id or ""),
                hotel_code=resolved_hotel_code,
            )
            return response
        except Exception as fallback_error:
            print(f"API Fallback Error: {fallback_error}")
            observability_service.log_event(
                "chat_message_fallback_failed",
                {
                    "trace_id": getattr(http_request.state, "trace_id", ""),
                    "session_id": request.session_id,
                    "hotel_code": request.hotel_code,
                    "error": str(fallback_error),
                },
            )
            conversation_audit_service.log_failed_turn(
                request=request,
                trace_id=trace_id,
                error=str(fallback_error),
                db_fallback=True,
            )
            _trace_chat_event(
                "chat_message_failed_db_fallback",
                trace_id=trace_id,
                turn_trace_id=turn_trace_id,
                http_request=http_request,
                request=request,
                status_code=503,
                error=str(fallback_error),
                extra={
                    "db_fallback": True,
                    "primary_db_error": str(e),
                },
            )
            turn_diagnostics_service.log_turn_end(
                request=request,
                response=None,
                db_fallback=True,
                error=str(fallback_error),
            )
            # â”€â”€ new_detailed_logger: failed â€” db_fallback_error â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
            try:
                new_detailed_logger.log_chat_turn_out_failed(
                    session_id=str(request.session_id or ""),
                    message=str(request.message or ""),
                    hotel_code=str(request.hotel_code or ""),
                    trace_id=trace_id,
                    turn_trace_id=turn_trace_id,
                    duration_ms=round((_perf_counter() - _turn_start) * 1000, 2),
                    error_type=type(fallback_error).__name__,
                    error_msg=str(fallback_error),
                    tb=_traceback.format_exc(),
                    db_fallback=True,
                    http_status=503,
                )
            except Exception:
                pass
            step_trace_service.log_event(
                "chat_message_step_end",
                payload={
                    "db_fallback": True,
                    "duration_ms": round((_perf_counter() - _turn_start) * 1000, 2),
                },
                trace_id=trace_id,
                turn_trace_id=turn_trace_id,
                session_id=str(request.session_id or "").strip(),
                hotel_code=resolved_hotel_code,
                channel=str(request.channel or (request.metadata or {}).get("channel") or "").strip(),
                endpoint=endpoint,
                method=method,
                status_code=503,
                component="api.routes.chat.send_message",
                step="response_emit",
                stage="failed",
                error=str(fallback_error),
            )
            _timing_status = "failed_db_fallback"
            _timing_status_code = 503
            _timing_db_fallback = True
            _timing_error = str(fallback_error)
            _timing_response_message = ""
            _timing_response_state = ""
            _timing_response_source = ""
            raise HTTPException(
                status_code=503,
                detail="Database is unavailable and fallback processing failed.",
            )
    except Exception as e:
        print(f"API Error: {e}")
        _traceback.print_exc()
        observability_service.log_event(
            "chat_message_failed",
            {
                "trace_id": getattr(http_request.state, "trace_id", ""),
                "session_id": request.session_id,
                "hotel_code": request.hotel_code,
                "error": str(e),
            },
        )
        conversation_audit_service.log_failed_turn(
            request=request,
            trace_id=trace_id,
            error=str(e),
            db_fallback=False,
        )
        _trace_chat_event(
            "chat_message_failed",
            trace_id=trace_id,
            turn_trace_id=turn_trace_id,
            http_request=http_request,
            request=request,
            status_code=500,
            error=str(e),
            extra={
                "db_fallback": False,
            },
        )
        turn_diagnostics_service.log_turn_end(
            request=request,
            response=None,
            db_fallback=False,
            error=str(e),
        )
        # â”€â”€ new_detailed_logger: failed â€” general exception â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        try:
            new_detailed_logger.log_chat_turn_out_failed(
                session_id=str(request.session_id or ""),
                message=str(request.message or ""),
                hotel_code=str(request.hotel_code or ""),
                trace_id=trace_id,
                turn_trace_id=turn_trace_id,
                duration_ms=round((_perf_counter() - _turn_start) * 1000, 2),
                error_type=type(e).__name__,
                error_msg=str(e),
                tb=_traceback.format_exc(),
                db_fallback=False,
                http_status=500,
            )
        except Exception:
            pass
        step_trace_service.log_event(
            "chat_message_step_end",
            payload={
                "db_fallback": False,
                "duration_ms": round((_perf_counter() - _turn_start) * 1000, 2),
            },
            trace_id=trace_id,
            turn_trace_id=turn_trace_id,
            session_id=str(request.session_id or "").strip(),
            hotel_code=resolved_hotel_code,
            channel=str(request.channel or (request.metadata or {}).get("channel") or "").strip(),
            endpoint=endpoint,
            method=method,
            status_code=500,
            component="api.routes.chat.send_message",
            step="response_emit",
            stage="failed",
            error=str(e),
        )
        _timing_status = "failed"
        _timing_status_code = 500
        _timing_db_fallback = False
        _timing_error = str(e)
        _timing_response_message = ""
        _timing_response_state = ""
        _timing_response_source = ""
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        # Release an in-flight dedupe key if we own it but never completed (error
        # path) so a later retry of this message_id can be processed.
        if _dedup_owned and _dedup_key:
            try:
                await message_dedup.release_if_pending(_dedup_key)
            except Exception:
                pass
        step_trace_service.log_event(
            "chat_message_scope_reset",
            payload={},
            trace_id=trace_id,
            turn_trace_id=turn_trace_id,
            session_id=str(request.session_id or "").strip(),
            hotel_code=resolved_hotel_code,
            endpoint=endpoint,
            method=method,
            component="api.routes.chat.send_message",
            step="scope_reset",
            stage="completed",
        )
        scope_reset_started = _perf_counter()
        db_config_service.reset_hotel_context(scope_token)
        if pin_token is not None:
            reset_pinned_model(pin_token)
        _record_step("scope_reset", scope_reset_started)

        turn_ctx = turn_diagnostics_service.get_turn_context()
        llm_calls = []
        if isinstance(turn_ctx, dict) and isinstance(turn_ctx.get("_llm_call_metrics"), list):
            llm_calls = list(turn_ctx.get("_llm_call_metrics") or [])
        llm_total_duration = round(
            sum(float(item.get("duration_ms") or 0.0) for item in llm_calls if isinstance(item, dict)),
            2,
        )
        llm_total_input_tokens = int(
            sum(int(item.get("input_tokens") or 0) for item in llm_calls if isinstance(item, dict))
        )
        llm_total_output_tokens = int(
            sum(int(item.get("output_tokens") or 0) for item in llm_calls if isinstance(item, dict))
        )
        llm_total_tokens = int(
            sum(int(item.get("total_tokens") or 0) for item in llm_calls if isinstance(item, dict))
        )
        llm_total_cost = round(
            sum(float(item.get("total_cost_usd") or 0.0) for item in llm_calls if isinstance(item, dict)),
            8,
        )

        if not _timing_response_message and response is not None:
            _timing_response_message = str(response.message or "")
        if not _timing_response_state and response is not None:
            _timing_response_state = str(getattr(response.state, "value", response.state) or "")
        if not _timing_response_source and response is not None and isinstance(response.metadata, dict):
            _timing_response_source = str(response.metadata.get("response_source") or "")

        chat_step_timing_service.log_turn(
            {
                "event_type": "chat_message_timing_breakdown",
                "trace_id": str(trace_id or "").strip(),
                "turn_trace_id": str(turn_trace_id or "").strip(),
                "session_id": str(request.session_id or "").strip(),
                "hotel_code": str(resolved_hotel_code or "").strip(),
                "channel": str(request.channel or (request.metadata or {}).get("channel") or "").strip(),
                "endpoint": endpoint,
                "method": method,
                "status": _timing_status,
                "status_code": int(_timing_status_code or 0),
                "db_fallback": bool(_timing_db_fallback),
                "error": str(_timing_error or "").strip(),
                "request_message": str(request.message or ""),
                "response_message": _timing_response_message,
                "response_state": _timing_response_state,
                "response_source": _timing_response_source,
                "total_duration_ms": round((_perf_counter() - _turn_start) * 1000, 2),
                "step_count": len(_step_timings),
                "steps": _step_timings,
                "llm_call_count": len(llm_calls),
                "llm_total_duration_ms": llm_total_duration,
                "llm_total_input_tokens": llm_total_input_tokens,
                "llm_total_output_tokens": llm_total_output_tokens,
                "llm_total_tokens": llm_total_tokens,
                "llm_total_cost_usd": llm_total_cost,
                "llm_calls": llm_calls,
            }
        )
        turn_diagnostics_service.clear_turn(turn_token)


@router.get("/session/{session_id}")
async def get_session(session_id: str, db: AsyncSession = Depends(get_db)):
    """
    Get session details and conversation history.
    """
    db_fallback = False
    try:
        context = await context_manager.get_context(session_id, db_session=db)
    except OperationalError as e:
        if bool(getattr(settings, "chat_db_first_mode", False)) and not bool(
            getattr(settings, "chat_db_allow_inmemory_fallback", True)
        ):
            raise HTTPException(
                status_code=503,
                detail="Database is unavailable. DB-first mode requires database connectivity.",
            )
        print(f"API DB Error (get_session fallback to local store): {e}")
        context = await context_manager.get_context(session_id, db_session=None)
        db_fallback = True
    if context is None:
        raise HTTPException(status_code=404, detail="Session not found")

    payload = {
        "session_id": context.session_id,
        "state": context.state.value,
        "hotel_code": context.hotel_code,
        "guest_phone": context.guest_phone,
        "room_number": context.room_number,
        "channel": context.channel,
        "message_count": len(context.messages),
        "messages": [
            {
                "role": msg.role.value,
                "content": msg.content,
                "timestamp": msg.timestamp.isoformat(),
                "metadata": msg.metadata,
            }
            for msg in context.messages
        ],
        "pending_action": context.pending_action,
        "pending_data": context.pending_data,
        "booking_assistance": {
            "conversation_id": str((context.pending_data or {}).get("_active_conversation_uid") or ""),
            "details_submitted": booking_details_submitted(context) if team_booking_enabled(context.hotel_code) else False,
        },
        "created_at": context.created_at.isoformat(),
        "updated_at": context.updated_at.isoformat(),
    }
    if db_fallback:
        payload["db_fallback"] = True
    return payload


@router.delete("/session/{session_id}")
async def delete_session(session_id: str, db: AsyncSession = Depends(get_db)):
    """
    Delete a session and its history.
    """
    db_fallback = False
    try:
        deleted = await context_manager.delete_context(session_id, db_session=db)
    except OperationalError as e:
        if bool(getattr(settings, "chat_db_first_mode", False)) and not bool(
            getattr(settings, "chat_db_allow_inmemory_fallback", True)
        ):
            raise HTTPException(
                status_code=503,
                detail="Database is unavailable. DB-first mode requires database connectivity.",
            )
        print(f"API DB Error (delete_session fallback to local store): {e}")
        deleted = await context_manager.delete_context(session_id, db_session=None)
        db_fallback = True
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"message": "Session deleted", "session_id": session_id, "db_fallback": db_fallback}


@router.get("/sessions")
async def list_sessions(db: AsyncSession = Depends(get_db)):
    """
    List all active sessions.
    """
    db_fallback = False
    try:
        sessions = await context_manager.list_sessions(db_session=db)
    except OperationalError as e:
        if bool(getattr(settings, "chat_db_first_mode", False)) and not bool(
            getattr(settings, "chat_db_allow_inmemory_fallback", True)
        ):
            raise HTTPException(
                status_code=503,
                detail="Database is unavailable. DB-first mode requires database connectivity.",
            )
        print(f"API DB Error (list_sessions fallback to local store): {e}")
        sessions = await context_manager.list_sessions(db_session=None)
        db_fallback = True

    summaries = []
    for sid in sessions:
        summary_db_session = None if db_fallback else db
        try:
            summary = await context_manager.get_conversation_summary(
                sid, db_session=summary_db_session
            )
        except OperationalError:
            if bool(getattr(settings, "chat_db_first_mode", False)) and not bool(
                getattr(settings, "chat_db_allow_inmemory_fallback", True)
            ):
                raise HTTPException(
                    status_code=503,
                    detail="Database is unavailable. DB-first mode requires database connectivity.",
                )
            summary = await context_manager.get_conversation_summary(sid, db_session=None)
            db_fallback = True
        summaries.append(summary)
    return {"sessions": summaries, "count": len(summaries), "db_fallback": db_fallback}


@router.post("/session/{session_id}/reset")
async def reset_session(session_id: str, db: AsyncSession = Depends(get_db)):
    """
    Reset a session's state to IDLE without deleting history.
    """
    from schemas.chat import ConversationState
    db_fallback = False

    try:
        context = await context_manager.get_context(session_id, db_session=db)
        if context is None:
            raise HTTPException(status_code=404, detail="Session not found")
        context.state = ConversationState.IDLE
        context.pending_action = None
        context.pending_data = {}
        await context_manager.save_context(context, db_session=db)
    except OperationalError as e:
        if bool(getattr(settings, "chat_db_first_mode", False)) and not bool(
            getattr(settings, "chat_db_allow_inmemory_fallback", True)
        ):
            raise HTTPException(
                status_code=503,
                detail="Database is unavailable. DB-first mode requires database connectivity.",
            )
        print(f"API DB Error (reset_session fallback to local store): {e}")
        context = await context_manager.get_context(session_id, db_session=None)
        if context is None:
            raise HTTPException(status_code=404, detail="Session not found")
        context.state = ConversationState.IDLE
        context.pending_action = None
        context.pending_data = {}
        await context_manager.save_context(context, db_session=None)
        db_fallback = True

    return {"message": "Session reset", "state": "idle", "db_fallback": db_fallback}


@router.get("/session/{session_id}/profile")
async def get_session_profile_endpoint(session_id: str, db: AsyncSession = Depends(get_db)):
    """Return the stored stable lead identity for a session (for form prefill).

    Reads new_bot_sessions via session_profile_service. Used by the widget to
    prefill name/phone/email on a new conversation so the guest doesn't re-type
    their details (location is intentionally NOT prefilled — picked per thread).
    Best-effort: returns {} if nothing stored.
    """
    from services import session_profile_service
    try:
        profile = await session_profile_service.get_session_profile(session_id, db_session=db)
    except Exception:
        profile = {}
    return {
        "guest_name": str((profile or {}).get("guest_name") or ""),
        "guest_phone": str((profile or {}).get("guest_phone") or ""),
        "guest_email": str((profile or {}).get("guest_email") or ""),
        "location": str((profile or {}).get("location") or ""),
    }


@router.get("/properties")
async def list_properties(db: AsyncSession = Depends(get_db)):
    """Return available property codes for chat UI selection.

    DB-only. The dropdown reflects exactly the rows in `new_bot_hotels`
    (filtered to is_active=True). No JSON fallback — if a row's city/name
    is blank, that's a data fix in the admin save flow, not a render-time
    patch.
    """
    properties_by_code: dict[str, dict[str, str]] = {}
    rows = (
        await db.execute(
            select(Hotel).where(Hotel.is_active == True).order_by(Hotel.code.asc())  # noqa: E712
        )
    ).scalars().all()
    for row in rows:
        code = str(row.code or "").strip()
        if not code:
            continue
        normalized_code = str(code).strip().lower().replace(" ", "_")
        properties_by_code[normalized_code] = {
            "code": normalized_code,
            "name": str(row.name or code).strip(),
            "city": str(row.city or "").strip(),
        }

    properties: list[dict[str, str]] = sorted(
        properties_by_code.values(),
        key=lambda row: str(row.get("code") or ""),
    )

    return {"properties": properties}


class ModelOverrideRequest(BaseModel):
    # Empty string clears the override (falls back to the env-configured model).
    model: str = ""


@router.get("/model-override")
async def get_chat_model_override():
    """Return the harness model list + the currently active override.

    Powers the /chat test-harness model selector. `active` is "" when no
    override is set (all LLM calls use the env-configured models).

    When `multi_model_enabled` is true the selector is DISABLED: each role runs
    on its own model (see `role_models`) and a single-model pick would collapse
    the split, so the UI must render the dropdown read-only."""
    from services import model_override_service as mos

    multi = mos.multi_model_enabled()
    return {
        "available": mos.available_models(),
        "active": mos.get_model_override(),
        "default_model": mos.default_model(),
        "multi_model_enabled": multi,
        "role_models": (
            {role: mos.resolve_role_model(role) for role in mos.ROLE_MODEL_DEFAULTS}
            if multi
            else {}
        ),
    }


@router.post("/model-override")
async def set_chat_model_override(payload: ModelOverrideRequest):
    """Set (or clear) the runtime chat-model override used by every LLM call.

    Send `{"model": ""}` to clear. A non-empty model must be one of the ids from
    GET /api/chat/model-override → available[].id.

    Rejected while multi-model is enabled: the override is a no-op in that mode,
    so accepting it would silently mislead QA into thinking they'd switched
    models. Set MULTI_MODEL_ENABLED=false (and restart) to use the selector."""
    from services import model_override_service as mos

    if mos.multi_model_enabled():
        raise HTTPException(
            status_code=409,
            detail=(
                "Model selector is disabled while multi-model is enabled "
                "(routing runs on one model, answering/chips on another). "
                "Set MULTI_MODEL_ENABLED=false and restart to pick a single model."
            ),
        )
    try:
        stored = mos.set_model_override(payload.model)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "available": mos.available_models(),
        "active": stored,
        "default_model": mos.default_model(),
        "multi_model_enabled": False,
        "role_models": {},
    }


@router.get("/test-profiles")
async def get_chat_test_phase_profiles(
    hotel_code: Optional[str] = "DEFAULT",
    db: AsyncSession = Depends(get_db),
):
    """
    Return phase-to-profile mapping used by the chat test UI to auto-apply
    guest/entity metadata for dashboard ticket validation.

    Source of truth (in order):
      1. Live GHN lookup — entity_id from the hotel's lumira_entity_id,
         guest_id from GHN_FF_GUEST_INFO for post-arrival phases only.
         pre_booking deliberately has no guest_id (new visitors).
      2. CHAT_TEST_PHASE_PROFILES_JSON env override — when GHN has nothing
         to return (e.g. a brand-new hotel not yet wired to Lumira).
    """
    resolved_hotel_code = config_service.resolve_hotel_code(hotel_code)
    scope_token = db_config_service.set_hotel_context(resolved_hotel_code)
    try:
        try:
            configured_phases = config_service.get_journey_phases()
        except Exception:
            configured_phases = []

        phases_payload: list[dict[str, str]] = []
        if isinstance(configured_phases, list):
            for phase in configured_phases:
                if not isinstance(phase, dict):
                    continue
                phase_id = _normalize_phase_identifier(phase.get("id"))
                if not phase_id:
                    continue
                phase_name = str(phase.get("name") or "").strip() or phase_id.replace("_", " ").title()
                phases_payload.append({"id": phase_id, "name": phase_name})

        if not phases_payload:
            phases_payload = [
                {"id": "pre_booking", "name": "Pre Booking"},
                {"id": "pre_checkin", "name": "Pre Checkin"},
                {"id": "during_stay", "name": "During Stay"},
                {"id": "post_checkout", "name": "Post Checkout"},
            ]

        # A hotel run from the admin (Bot Training -> Pre-booking) is tested the
        # way its guests reach it: with no test profile, like a customer embed.
        # The fallback profile is another company's entity (5703) and a WhatsApp
        # ticket source; on T24 it sent every /chat test ticket to that entity's
        # only department (Housekeeping) and filed it as whatsapp_bot.
        from services import prebooking_settings_service as _pbs_tp
        if _pbs_tp.is_admin_managed(str(resolved_hotel_code or "").strip().lower()):
            return {"auto_apply_enabled": False, "profiles_by_phase": {}, "phases": phases_payload}

        profiles_by_phase: dict[str, dict[str, str]] = {}
        try:
            profiles_by_phase = await _fetch_real_test_profiles(resolved_hotel_code, db)
        except Exception:
            logger.warning(
                "test_profiles: real GHN lookup failed for %s",
                resolved_hotel_code,
                exc_info=True,
            )

        if not profiles_by_phase:
            profiles_by_phase = _load_chat_test_phase_profiles()

        return {
            "auto_apply_enabled": bool(getattr(settings, "chat_test_phase_profile_auto_apply", True)),
            "profiles_by_phase": profiles_by_phase,
            "phases": phases_payload,
        }
    finally:
        db_config_service.reset_hotel_context(scope_token)


class SuggestionsRequest(BaseModel):
    last_bot_message: str
    user_message: Optional[str] = None
    hotel_code: Optional[str] = "DEFAULT"
    current_phase: Optional[str] = "pre_booking"
    session_id: Optional[str] = None
    conversation_id: Optional[str] = None
    assistant_turn_id: Optional[str] = None


async def _warm_prefetch_for_reply_suggestions(
    *,
    request: ChatRequest,
    response: ChatResponse,
    hotel_code: str,
) -> None:
    if not bool(getattr(settings, "chat_suggestion_prefetch_enabled", True)):
        return
    if not str(getattr(settings, "openai_api_key", "") or "").strip():
        return
    session_id = str(request.session_id or "").strip()
    if not session_id:
        return

    response_meta = response.metadata if isinstance(response.metadata, dict) else {}
    assistant_turn_id = str(
        response_meta.get("turn_trace_id")
        or (request.metadata or {}).get("turn_trace_id")
        or ""
    ).strip()
    if not assistant_turn_id:
        return

    existing = await suggestion_prefetch_service.find_existing_batch_for_turn(
        session_id=session_id,
        assistant_turn_id=assistant_turn_id,
    )
    if existing:
        return

    raw_suggestions = (
        list(response.suggested_actions)
        if isinstance(getattr(response, "suggested_actions", None), list)
        else []
    )
    cleaned_suggestions = _filter_chip_quality(
        _sanitize_suggestions(raw_suggestions, limit=3),
        limit=3,
    )
    if not cleaned_suggestions:
        return
    visible_chips = [SuggestionChip(text=text) for text in cleaned_suggestions]

    runtime_snapshot = llm_orchestration_service.get_post_response_chip_runtime_snapshot(
        session_id=session_id,
        assistant_turn_id=assistant_turn_id,
    )
    phase_id = _normalize_phase_identifier(
        (request.metadata or {}).get("phase")
        or response_meta.get("selected_phase_id")
        or runtime_snapshot.get("phase_id_hint")
        or "pre_booking"
    ) or "pre_booking"
    if not runtime_snapshot:
        phase_name = phase_id.replace("_", " ").title()
        runtime_snapshot = {
            "conversation_state": str(getattr(response.state, "value", response.state) or "idle"),
            "pending_action": "",
            "pending_data_public": {},
            "known_context": {},
            "memory_snapshot": {},
            "phase_id_hint": phase_id,
            "phase_name_hint": phase_name,
            "active_service_id": "",
            "active_service": {},
            "conversation_history": [],
        }

    await _schedule_prefetch_for_visible_suggestions(
        request=SuggestionsRequest(
            last_bot_message=str(response.message or "").strip(),
            user_message=str(request.message or "").strip(),
            hotel_code=hotel_code,
            current_phase=phase_id,
            session_id=session_id,
            conversation_id=str((request.metadata or {}).get("conversation_id") or "").strip(),
            assistant_turn_id=assistant_turn_id,
        ),
        hotel_code=hotel_code,
        runtime_snapshot=runtime_snapshot,
        suggestions=visible_chips,
    )


@router.post("/suggestions")
async def get_contextual_suggestions(
    request: SuggestionsRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Deliver only canonical post-response suggestion chips.

    Flow:
    1) Wait briefly for the post-response chip job tied to the latest reply.
    2) If that reply is still current, schedule exact chip prefetch for those visible chips.
    3) Otherwise return no chips. There is intentionally no fallback generator here.
    """
    scope_token = None
    try:
        hotel_code = config_service.resolve_hotel_code(request.hotel_code)
        scope_token = db_config_service.set_hotel_context(hotel_code)
        session_id = str(request.session_id or "").strip()
        if not session_id:
            return {"suggestions": [], "prefetch_batch_id": None}
        # Tenant-level chip kill switch. The generator is already gated in
        # schedule_post_response_chip_job; this is the delivery-side backstop so
        # no chip from any other producer (seeded/cached) can reach the widget.
        if llm_orchestration_service.suggestion_chips_disabled_for_hotel(hotel_code):
            return {"suggestions": [], "prefetch_batch_id": None}

        configured_chip_wait = int(
            getattr(settings, "chat_post_response_chip_wait_ms", 2200) or 2200
        )
        # Keep suggestion polling snappy: prefer short waits and return
        # explicit pending/retry hints instead of blocking each poll.
        wait_ms = max(40, min(120, configured_chip_wait))
        ready = await llm_orchestration_service.await_post_response_chip_objects(
            session_id=session_id,
            assistant_turn_id=str(request.assistant_turn_id or "").strip(),
            wait_ms=wait_ms,
        )
        ready_chips = [
            chip
            for chip in list(ready or [])
            if isinstance(chip, SuggestionChip) and str(chip.text or "").strip()
        ]
        ready_quality = _filter_chip_quality(
            [str(chip.text or "").strip() for chip in ready_chips],
            limit=3,
        )
        logger.info(
            "[CHIP_API] session=%s turn=%s await_returned=%d quality=%d",
            session_id, str(request.assistant_turn_id or "")[:16],
            len(ready_chips), len(ready_quality),
        )
        if not ready_quality:
            job_status = llm_orchestration_service.get_post_response_chip_job_status(
                session_id=session_id,
                assistant_turn_id=str(request.assistant_turn_id or "").strip(),
            )
            if bool(job_status.get("exists")) and bool(job_status.get("pending")):
                return {
                    "suggestions": [],
                    "prefetch_batch_id": None,
                    "status": "pending",
                    "retry_after_ms": max(150, int(wait_ms * 1.2)),
                }
            observability_service.log_event(
                "suggestions_no_chip",
                {
                    "session_id": session_id,
                    "assistant_turn_id": str(request.assistant_turn_id or "").strip(),
                    "hotel_code": hotel_code,
                    "phase_id": str(request.current_phase or "").strip(),
                    "no_chip_reason": (
                        "filtered_low_confidence" if bool(ready_chips) else "generation_empty"
                    ),
                    "contextual_generation_expected": bool(job_status.get("exists")),
                    "pending_action": "",
                    "active_service_id": "",
                    "ready_chip_count": len(ready_chips),
                    "quality_chip_count": len(ready_quality),
                },
            )
            logger.info(
                "[CHIP_TURN] delivered session=%s turn=%s result=no_visible_chips reason=%s ready=%d quality=%d visible=0",
                session_id,
                str(request.assistant_turn_id or "")[:16],
                "filtered_low_confidence" if bool(ready_chips) else "generation_empty",
                len(ready_chips),
                len(ready_quality),
            )
            return {"suggestions": [], "prefetch_batch_id": None}
        ready_quality_keys = {
            _normalize_suggestions_turn_text(text)
            for text in ready_quality
            if str(text or "").strip()
        }
        visible_chips: list[SuggestionChip] = []
        seen_chip_keys: set[str] = set()
        for chip in ready_chips:
            key = _normalize_suggestions_turn_text(chip.text)
            if not key or key not in ready_quality_keys or key in seen_chip_keys:
                continue
            seen_chip_keys.add(key)
            visible_chips.append(chip)
            if len(visible_chips) >= len(ready_quality):
                break
        if not visible_chips:
            return {"suggestions": [], "prefetch_batch_id": None}

        current_runtime_snapshot = await _load_suggestions_runtime_snapshot(
            session_id=session_id,
            db=db,
        )
        if _is_suggestions_request_stale(
            runtime_snapshot=current_runtime_snapshot,
            last_bot_message=str(request.last_bot_message or "").strip(),
        ):
            observability_service.log_event(
                "suggestions_no_chip",
                {
                    "session_id": session_id,
                    "assistant_turn_id": str(request.assistant_turn_id or "").strip(),
                    "hotel_code": hotel_code,
                    "phase_id": str(request.current_phase or "").strip(),
                    "no_chip_reason": "context_unlocked",
                    "contextual_generation_expected": False,
                    "pending_action": str(current_runtime_snapshot.get("pending_action") or ""),
                    "active_service_id": str(current_runtime_snapshot.get("active_service_id") or ""),
                    "ready_chip_count": len(ready_chips),
                    "quality_chip_count": len(ready_quality),
                },
            )
            logger.info(
                "[CHIP_TURN] delivered session=%s turn=%s result=no_visible_chips reason=context_unlocked ready=%d quality=%d visible=0",
                session_id,
                str(request.assistant_turn_id or "")[:16],
                len(ready_chips),
                len(ready_quality),
            )
            return {"suggestions": [], "prefetch_batch_id": None}
        turn_runtime_snapshot = llm_orchestration_service.get_post_response_chip_runtime_snapshot(
            session_id=session_id,
            assistant_turn_id=str(request.assistant_turn_id or "").strip(),
        )
        runtime_snapshot = (
            turn_runtime_snapshot
            if isinstance(turn_runtime_snapshot, dict) and turn_runtime_snapshot
            else current_runtime_snapshot
        )
        visible_chips = _suppress_recently_asked_chip_texts(
            visible_chips,
            runtime_snapshot=runtime_snapshot if isinstance(runtime_snapshot, dict) else {},
        )
        if not visible_chips:
            observability_service.log_event(
                "suggestions_no_chip",
                {
                    "session_id": session_id,
                    "assistant_turn_id": str(request.assistant_turn_id or "").strip(),
                    "hotel_code": hotel_code,
                    "phase_id": str(request.current_phase or "").strip(),
                    "no_chip_reason": "duplicate_suppressed_all",
                    "contextual_generation_expected": True,
                    "pending_action": "",
                    "active_service_id": "",
                    "ready_chip_count": len(ready_chips),
                    "quality_chip_count": len(ready_quality),
                },
            )
            logger.info(
                "[CHIP_TURN] delivered session=%s turn=%s result=no_visible_chips reason=duplicate_suppressed_all ready=%d quality=%d visible=0",
                session_id,
                str(request.assistant_turn_id or "")[:16],
                len(ready_chips),
                len(ready_quality),
            )
            return {"suggestions": [], "prefetch_batch_id": None}

        prefetch_batch_id = await suggestion_prefetch_service.find_existing_batch_for_turn(
            session_id=session_id,
            assistant_turn_id=str(request.assistant_turn_id or "").strip(),
        )
        if not prefetch_batch_id:
            prefetch_batch_id = await _schedule_prefetch_for_visible_suggestions(
                request=request,
                hotel_code=hotel_code,
                runtime_snapshot=runtime_snapshot,
                suggestions=visible_chips,
            )
        logger.info(
            "[CHIP_TURN] delivered session=%s turn=%s result=visible ready=%d quality=%d visible=%d active_service=%s pending_action=%s",
            session_id,
            str(request.assistant_turn_id or "")[:16],
            len(ready_chips),
            len(ready_quality),
            len(visible_chips),
            str((runtime_snapshot or {}).get("active_service_id") or "").strip(),
            str((runtime_snapshot or {}).get("pending_action") or "").strip(),
        )
        # DB log: the chips actually delivered to the client (HTTP + WS both
        # poll this endpoint). Canonical chip chokepoint → new_bot_log_chips.
        try:
            from services.db_log_sink import db_log_sink
            db_log_sink.emit(
                "chips",
                {
                    "session_id": session_id,
                    "hotel_code": hotel_code,
                    "turn_trace_id": str(request.assistant_turn_id or ""),
                    "event": "chips_delivered",
                    "component": "chat.suggestions",
                    "chips": [str(chip.text or "").strip() for chip in visible_chips],
                    "chip_source": "post_response_chip_job",
                    "phase": str(request.current_phase or ""),
                    "service_id": str((runtime_snapshot or {}).get("active_service_id") or ""),
                },
            )
        except Exception:
            pass
        return {
            "suggestions": [str(chip.text or "").strip() for chip in visible_chips],
            "prefetch_batch_id": prefetch_batch_id,
        }
    except Exception:
        logger.exception(
            "suggestions_endpoint_error session_id=%s hotel_code=%r",
            str(request.session_id or "").strip(),
            request.hotel_code,
        )
        return {"suggestions": [], "prefetch_batch_id": None}
    finally:
        if scope_token is not None:
            db_config_service.reset_hotel_context(scope_token)
