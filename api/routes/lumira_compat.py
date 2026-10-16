"""
Lumira compatibility API routes.

These routes preserve Lumira request/response contracts while delegating all
business logic to the existing chat_service pipeline.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import get_db
from services.chat_service import chat_service
from services.engage_session_service import engage_session_service
from services.lumira_compat_adapter import (
    build_engage_chat_request,
    build_engage_response,
    build_guest_journey_chat_request,
    build_guest_journey_response,
)
from schemas.chat import ChatResponse

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Lumira Compatibility"])


# ---------------------------------------------------------------------------
# Form sync: Kebo form_config → GHN_CHATBOT_FORMS
# ---------------------------------------------------------------------------

def _kebo_fields_to_lumira_content_json(
    service_name: str,
    form_config: dict[str, Any],
    form_id: int,
) -> dict[str, Any]:
    """
    Convert Kebo service form_config.fields into the Content-JSON format
    that Lumira frontend expects in GHN_CHATBOT_FORMS.
    """
    # Delegates to the shared translator so a synced row is byte-identical to
    # what any other outbound path emits. The old inline version was missing
    # `fieldType` and `order`, which GHN's own rows always carry — without
    # `fieldType` nothing downstream can tell which field is the phone.
    from services import ghn_form_schema

    body = ghn_form_schema.to_ghn_content_json(form_config, service_name=service_name)
    # `form_id` is ours, not GHN's; keep it so existing readers of these synced
    # rows can still trace a row back to the service that produced it.
    body["form_id"] = form_id
    return body


async def _sync_kebo_forms_to_lumira(db: AsyncSession) -> dict[str, Any]:
    """
    Read all Kebo services that have lumira_form_key in form_config and
    upsert the corresponding row in GHN_CHATBOT_FORMS.

    This keeps the two tables in sync so Lumira frontend always renders
    the latest field definitions from Kebo's admin panel.
    """
    from sqlalchemy import text as _text

    synced = []
    skipped = []
    errors = []

    try:
        result = await db.execute(_text(
            """
            SELECT s.id, s.name, s.form_config, h.code as hotel_code
            FROM new_bot_services s
            JOIN new_bot_hotels h ON h.id = s.hotel_id
            WHERE s.form_config IS NOT NULL
              AND s.form_config LIKE '%lumira_form_key%'
            """
        ))
        rows = result.mappings().fetchall()
    except Exception as exc:
        return {"error": f"Service lookup failed: {exc}", "synced": [], "skipped": [], "errors": []}

    for row in rows:
        try:
            fc = json.loads(row["form_config"] or "{}")
            form_key = str(fc.get("lumira_form_key") or "").strip()
            entity_id = str(fc.get("lumira_entity_id") or "").strip()
            if not form_key:
                skipped.append({"service_id": row["id"], "reason": "lumira_form_key empty"})
                continue

            content_json = _kebo_fields_to_lumira_content_json(
                service_name=str(row["name"] or ""),
                form_config=fc,
                form_id=int(row["id"]),
            )
            content_json_str = json.dumps(content_json, ensure_ascii=False)
            service_name = str(row["name"] or "")

            # Scope by BOTH Bot_Id and Form_Key. Form_Key is NOT unique in this
            # table — `otp_validation_form` exists once per bot — so matching on
            # the key alone can update a different tenant's form.
            exists = await db.execute(
                _text(
                    "SELECT ID FROM GHN_CHATBOT_FORMS "
                    "WHERE Form_Key = :fk AND Bot_Id <=> :bot_id LIMIT 1"
                ),
                {"fk": form_key, "bot_id": entity_id or None},
            )
            existing = exists.fetchone()

            if existing:
                # UPDATE existing row
                await db.execute(
                    _text(
                        """
                        UPDATE GHN_CHATBOT_FORMS
                        SET Title          = :title,
                            `Content-JSON` = :cj,
                            Bot_Id         = :bot_id,
                            Submit_Label   = :submit_label,
                            Success_Message = :success_msg,
                            Is_Active      = 1
                        WHERE ID = :row_id
                        """
                    ),
                    {
                        "title": service_name,
                        "cj": content_json_str,
                        "bot_id": entity_id or None,
                        "submit_label": "Submit",
                        "success_msg": f"Your {service_name} request has been submitted successfully!",
                        "row_id": int(existing[0]),
                    },
                )
                synced.append({"form_key": form_key, "action": "updated", "service": service_name})
            else:
                # INSERT new row
                await db.execute(
                    _text(
                        """
                        INSERT INTO GHN_CHATBOT_FORMS
                            (Bot_Id, Title, `Content-JSON`, Form_Key,
                             Submit_Label, Success_Message, Is_Active)
                        VALUES
                            (:bot_id, :title, :cj, :fk,
                             :submit_label, :success_msg, 1)
                        """
                    ),
                    {
                        "bot_id": entity_id or None,
                        "title": service_name,
                        "cj": content_json_str,
                        "fk": form_key,
                        "submit_label": "Submit",
                        "success_msg": f"Your {service_name} request has been submitted successfully!",
                    },
                )
                synced.append({"form_key": form_key, "action": "inserted", "service": service_name})

        except Exception as exc:
            errors.append({"service_id": row["id"], "error": str(exc)})
            logger.exception("form sync failed for service %s: %s", row["id"], exc)

    await db.commit()
    return {"synced": synced, "skipped": skipped, "errors": errors}


@router.post("/lumira/sync-forms")
async def sync_forms(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """
    Sync Kebo service form definitions → GHN_CHATBOT_FORMS.

    Call this after adding/updating a service's form_config in the admin panel.
    Automatically keeps Lumira frontend in sync with Kebo's field definitions.
    """
    result = await _sync_kebo_forms_to_lumira(db)
    total = len(result.get("synced", []))
    logger.info("lumira form sync: %d synced, %d skipped, %d errors",
                total, len(result.get("skipped", [])), len(result.get("errors", [])))
    return {
        "status": "ok" if not result.get("error") else "error",
        "total_synced": total,
        **result,
    }


async def _run_form_trigger(
    response: ChatResponse,
    user_message: str = "",
    hotel_code: str = "",
) -> None:
    """Call the form trigger post-processor (same as the /chat route does).

    Args:
        hotel_code: The hotel code to set as context before calling the trigger.
                    Required so that db_config_service.get_services() returns the
                    correct hotel's services (not the DEFAULT fallback), since
                    chat_service.process_message() resets the hotel context after it returns.
    """
    try:
        from api.routes.chat import _inject_form_trigger
        from services.db_config_service import db_config_service as _dcs

        if hotel_code:
            # Set the hotel context for the form trigger call so that
            # config_service.get_service() loads the correct hotel's JSON config
            # (not the DEFAULT business_config.json), and db_config_service.get_services()
            # returns the correct hotel's DB services.
            # chat_service.process_message() resets the context after it returns, so we
            # must re-set it here.
            _token = _dcs.set_hotel_context(hotel_code)
            try:
                await _inject_form_trigger(
                    response,
                    user_message=user_message,
                )
            finally:
                _dcs.reset_hotel_context(_token)
        else:
            await _inject_form_trigger(
                response,
                user_message=user_message,
            )
    except Exception as exc:
        logger.warning("lumira _run_form_trigger failed (non-fatal): %r", exc)


def _normalize_error(exc: Exception) -> str:
    detail = str(exc).strip()
    return detail or exc.__class__.__name__


async def _attach_display_message(response: ChatResponse) -> None:
    """Pass the canonical answer through as the display message.

    Beautification is the answerer LLM's responsibility now (prompt enforces
    bullets + bold + italic). No post-processing LLM, no deterministic
    rewriter. What the answerer emits is what the guest sees.
    """
    if not isinstance(response.metadata, dict):
        response.metadata = {}
    canonical = str(response.message or "").strip()
    response.display_message = canonical
    response.metadata["display_message"] = canonical
    response.metadata["canonical_message"] = canonical


def _extract_session_meta(
    payload: dict[str, Any],
    chat_request_meta: dict[str, Any],
) -> dict[str, str]:
    """Pull entity_id / group_id / widget_id / conversation_id for session logging."""
    def _s(v: Any) -> str:
        return str(v or "").strip()

    return {
        "entity_id": _s(
            chat_request_meta.get("entity_id")
            or payload.get("entity_id")
            or payload.get("entityId")
        ),
        "group_id": _s(
            chat_request_meta.get("group_id")
            or payload.get("group_id")
            or payload.get("groupId")
        ),
        "widget_id": _s(
            chat_request_meta.get("widget_id")
            or payload.get("widget_id")
            or payload.get("widgetId")
        ),
        "conversation_id": _s(
            chat_request_meta.get("conversation_id")
            or payload.get("conversation_id")
            or payload.get("conversationId")
        ),
    }


@router.post("/guest-journey/message")
# Public aliases. nginx on bot.kepsla.ai only proxies /api/* to this app, so the
# bare paths above are unreachable from the internet. Both /api/ forms exist
# because Lumira registers its handler at BOTH "/guest-journey/message" and a
# no-prefix "/message" — the Java caller may hold a base URL and append either.
@router.post("/api/guest-journey/message")
@router.post("/api/message")
async def guest_journey_message(
    payload: dict[str, Any],
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Lumira-compatible guest-journey endpoint.
    """
    payload = payload if isinstance(payload, dict) else {}

    # Resolve hotel_code from entity_id, same as the engage endpoint below.
    # WhatsApp callers send entity_id=6311 and no hotel_code, so without this the
    # adapter falls back to hotel_code="6311" — which matches no row in
    # new_bot_hotels, so NO hotel loads: no prompts, no KB, no services, and the
    # bot answers "I don't have the hotel name / amenities" for everything.
    entity_id_raw = str(payload.get("entity_id") or payload.get("entityId") or "").strip()
    if entity_id_raw and not payload.get("hotel_code"):
        try:
            from sqlalchemy import text as _text
            _result = await db.execute(
                _text(
                    "SELECT h.code FROM new_bot_hotels h "
                    "JOIN new_bot_services s ON s.hotel_id = h.id "
                    "WHERE s.form_config IS NOT NULL "
                    "  AND s.form_config LIKE :pat "
                    "LIMIT 1"
                ),
                {"pat": f"%{entity_id_raw}%"},
            )
            _row = _result.fetchone()
            if _row and _row[0]:
                payload = dict(payload)  # make mutable copy
                payload["hotel_code"] = str(_row[0]).strip()
                logger.info(
                    "guest_journey_message: resolved entity_id=%s → hotel_code=%s",
                    entity_id_raw,
                    payload["hotel_code"],
                )
        except Exception as _exc:
            logger.debug("guest_journey_message: hotel_code lookup failed: %s", _exc)

    try:
        chat_request = build_guest_journey_chat_request(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.errors()) from exc

    # Write guest message row to KEBO_ENGAGE_SESSIONS (fire-and-forget)
    req_meta = chat_request.metadata if isinstance(chat_request.metadata, dict) else {}
    _sm = _extract_session_meta(payload, req_meta)
    engage_session_service.write_turn_bg(
        session_id=chat_request.session_id,
        sender="USER",
        message=str(chat_request.message or ""),
        **_sm,
    )

    try:
        chat_response = await chat_service.process_message(chat_request, db_session=db)
    except OperationalError:
        chat_response = await chat_service.process_message(chat_request, db_session=None)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=_normalize_error(exc)) from exc
    await _run_form_trigger(
        chat_response,
        user_message=str(chat_request.message or ""),
        hotel_code=str(chat_request.hotel_code or ""),
    )
    await _attach_display_message(chat_response)

    # Write bot response row to KEBO_ENGAGE_SESSIONS (fire-and-forget)
    resp_meta = chat_response.metadata if isinstance(chat_response.metadata, dict) else {}
    engage_session_service.write_turn_bg(
        session_id=chat_request.session_id,
        sender="BOT",
        message=str(chat_response.message or ""),
        render=str(resp_meta.get("render") or "") or None,
        message_type="form" if resp_meta.get("render") else "text",
        total_cost=float(resp_meta.get("total_cost") or resp_meta.get("cost") or 0.0),
        total_tokens=int(resp_meta.get("total_tokens") or 0),
        metadata=resp_meta,
        **_sm,
    )

    return build_guest_journey_response(chat_response, source_payload=payload)


@router.post("/engage-bot/message")
async def engage_message(
    payload: dict[str, Any],
    session_id: str | None = Header(default=None, alias="session_id"),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Lumira-compatible engage endpoint.
    """
    body = payload if isinstance(payload, dict) else {}
    if body.get("entity_id") in (None, "") and body.get("entityId") in (None, "") and body.get("group_id") in (None, "") and body.get("groupId") in (None, ""):
        raise HTTPException(
            status_code=400,
            detail="Missing 'entity_id' or 'group_id' in request body",
        )

    # Resolve hotel_code from entity_id so Kebo can load the right services.
    # Lumira passes integer entity_id; Kebo hotels use string codes like "ICONIQA".
    # We store lumira_entity_id in service form_config so we can reverse-lookup.
    entity_id_raw = str(body.get("entity_id") or body.get("entityId") or "").strip()
    if entity_id_raw and not body.get("hotel_code"):
        try:
            from sqlalchemy import text as _text
            _result = await db.execute(
                _text(
                    "SELECT h.code FROM new_bot_hotels h "
                    "JOIN new_bot_services s ON s.hotel_id = h.id "
                    "WHERE s.form_config IS NOT NULL "
                    "  AND s.form_config LIKE :pat "
                    "LIMIT 1"
                ),
                {"pat": f"%{entity_id_raw}%"},
            )
            _row = _result.fetchone()
            if _row and _row[0]:
                body = dict(body)  # make mutable copy
                body["hotel_code"] = str(_row[0]).strip()
                logger.debug("engage_message: resolved entity_id=%s → hotel_code=%s", entity_id_raw, body["hotel_code"])
        except Exception as _exc:
            logger.debug("engage_message: hotel_code lookup failed: %s", _exc)

    try:
        chat_request = build_engage_chat_request(body, session_id_header=session_id or "")
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.errors()) from exc

    # Write guest message row to KEBO_ENGAGE_SESSIONS (fire-and-forget)
    req_meta = chat_request.metadata if isinstance(chat_request.metadata, dict) else {}
    _sm = _extract_session_meta(body, req_meta)
    engage_session_service.write_turn_bg(
        session_id=chat_request.session_id,
        sender="USER",
        message=str(chat_request.message or ""),
        **_sm,
    )

    try:
        chat_response = await chat_service.process_message(chat_request, db_session=db)
    except OperationalError:
        chat_response = await chat_service.process_message(chat_request, db_session=None)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=_normalize_error(exc)) from exc
    await _run_form_trigger(
        chat_response,
        user_message=str(chat_request.message or ""),
        hotel_code=str(chat_request.hotel_code or ""),
    )
    await _attach_display_message(chat_response)

    # Write bot response row to KEBO_ENGAGE_SESSIONS (fire-and-forget)
    resp_meta = chat_response.metadata if isinstance(chat_response.metadata, dict) else {}
    engage_session_service.write_turn_bg(
        session_id=chat_request.session_id,
        sender="BOT",
        message=str(chat_response.message or ""),
        render=str(resp_meta.get("render") or "") or None,
        message_type="form" if resp_meta.get("render") else "text",
        total_cost=float(resp_meta.get("total_cost") or resp_meta.get("cost") or 0.0),
        total_tokens=int(resp_meta.get("total_tokens") or 0),
        metadata=resp_meta,
        **_sm,
    )

    return build_engage_response(
        chat_response,
        source_payload=body,
        session_id=chat_request.session_id,
    )


@router.post("/engage-bot/form-submit")
async def engage_form_submit(
    payload: dict[str, Any],
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Lumira-compatible form submission endpoint.

    Called by Lumira frontend after the guest fills and submits a popup form.
    Maps form_key → service_id, then creates the ticket via the existing
    ticketing pipeline.

    Expected payload:
    {
        "session_id": "abc123",
        "form_key": "room_service_form",
        "fields": { "room_number": "204", "meal": "pasta", "time": "7:30 PM" },
        "entity_id": "42",
        "widget_id": "5",
        "group_id": "10"
    }
    """
    from services.ticketing_service import ticketing_service

    body = payload if isinstance(payload, dict) else {}
    session_id = str(body.get("session_id") or "").strip()
    form_key = str(body.get("form_key") or "").strip()
    fields = body.get("fields") or body.get("form_data") or {}
    entity_id = str(body.get("entity_id") or body.get("entityId") or "").strip()
    widget_id = str(body.get("widget_id") or body.get("widgetId") or "").strip()
    group_id = str(body.get("group_id") or body.get("groupId") or "").strip()

    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")
    if not form_key:
        raise HTTPException(status_code=400, detail="form_key is required")
    if not isinstance(fields, dict):
        raise HTTPException(status_code=400, detail="fields must be a JSON object")

    # ── Find the service that owns this lumira_form_key ──────────────────
    # Direct SQL lookup across all services — avoids hotel-scoping issues
    # since entity_id (Lumira int) ≠ hotel_code (Kebo string like "ICONIQA").
    from sqlalchemy import text as _text

    service: dict[str, Any] | None = None
    hotel_code = "DEFAULT"
    try:
        # Use LIKE + Python-side JSON parse (avoids JSON_EXTRACT dialect issues)
        result = await db.execute(
            _text(
                """
                SELECT s.id, s.name, s.hotel_id, s.ticketing_enabled,
                       s.ticketing_mode, s.form_config,
                       h.code as hotel_code
                FROM new_bot_services s
                JOIN new_bot_hotels h ON h.id = s.hotel_id
                WHERE s.ticketing_enabled = 1
                  AND s.form_config IS NOT NULL
                  AND s.form_config LIKE :pattern
                """
            ),
            {"pattern": f"%{form_key}%"},
        )
        rows = result.mappings().fetchall()
        for row in rows:
            try:
                fc = json.loads(row["form_config"] or "{}")
                if str(fc.get("lumira_form_key") or "").strip() == form_key:
                    service = dict(row)
                    hotel_code = str(row["hotel_code"] or "DEFAULT")
                    break
            except Exception:
                continue
    except Exception as exc:
        logger.warning("engage_form_submit: service lookup failed: %s", exc)

    if not service:
        raise HTTPException(
            status_code=404,
            detail=f"No service configured for form_key '{form_key}'. "
                   f"Add lumira_form_key to the service's form_config in the admin panel.",
        )

    service_id = str(service.get("id") or "").strip()
    service_name = str(service.get("name") or service_id)

    # ── Write guest form submission to KEBO_ENGAGE_SESSIONS ───────────────
    engage_session_service.write_turn_bg(
        session_id=session_id,
        sender="USER",
        message=json.dumps(fields, ensure_ascii=False),
        entity_id=entity_id,
        group_id=group_id,
        widget_id=widget_id,
        render=form_key,
        message_type="form",
    )

    # ── Load context and create ticket ────────────────────────────────────
    from core.context_manager import context_manager
    from services.db_config_service import db_config_service
    from schemas.chat import ConversationContext

    success = False
    confirm_msg = ""
    try:
        # Set hotel context so services resolve correctly
        scope_token = db_config_service.set_hotel_context(hotel_code)

        # Load existing context or create a minimal one for this session
        ctx = await context_manager.get_context(
            session_id=session_id,
            db_session=db,
        )
        if ctx is None:
            ctx = ConversationContext(session_id=session_id, hotel_code=hotel_code)

        # Populate form fields into pending_data so build_lumira_ticket_payload can read them
        if not isinstance(ctx.pending_data, dict):
            ctx.pending_data = {}
        ctx.pending_data.update(fields)

        # Build a real, guest-facing ticket description (the guest's own words
        # when the form has a freeform field) instead of generic boilerplate —
        # same logic as the main /api/chat/form-submit path so both widgets
        # push an identical, meaningful description to the ticketing API.
        from api.routes.chat import _generate_form_ticket_description

        form_config_obj: dict[str, Any] = {}
        try:
            form_config_obj = json.loads(service.get("form_config") or "{}")
        except Exception:
            form_config_obj = {}
        if not isinstance(form_config_obj, dict):
            form_config_obj = {}

        merged_form_data = dict(ctx.pending_data) if isinstance(ctx.pending_data, dict) else {}
        merged_form_data.update({k: str(v) for k, v in fields.items() if str(v).strip()})

        issue_summary, detailed_message = await _generate_form_ticket_description(
            service={
                "id": service_id,
                "name": service_name,
                "form_config": form_config_obj,
            },
            service_name=service_name,
            service_id=service_id,
            merged_form_data=merged_form_data,
            req_meta={},
            context=ctx,
        )

        ticket_payload = ticketing_service.build_lumira_ticket_payload(
            context=ctx,
            issue=issue_summary,
            message=detailed_message,
            category=service_name,
            source="lumira_form",
            group_id=group_id or None,
        )
        ticket_payload["service_id"] = service_id
        ticket_payload["service_name"] = service_name
        ticket_payload["hotel_code"] = hotel_code
        # Merge form fields directly into payload
        for k, v in fields.items():
            ticket_payload.setdefault(str(k), str(v))

        _lead_capture_only = (
            bool(form_config_obj.get("lead_capture_only"))
            if isinstance(form_config_obj, dict)
            else False
        )
        if _lead_capture_only:
            # Info-collection form (e.g. the opening name+location form): capture
            # the lead identity below but NEVER create a support ticket.
            success = True
            confirm_msg = "Thanks — got your details. How can I help you today?"
        else:
            result = await ticketing_service.create_ticket(ticket_payload)
            success = bool(getattr(result, "success", False))
            if success:
                confirm_msg = (
                    f"Your {service_name} request has been submitted successfully! "
                    "Our team will be in touch shortly."
                )
            else:
                err = str(getattr(result, "error", "") or "").strip()
                confirm_msg = (
                    f"There was an issue submitting your {service_name} request. "
                    f"{err or 'Please try again or contact our staff.'}"
                )

        db_config_service.reset_hotel_context(scope_token)
    except Exception as exc:
        logger.exception("engage_form_submit: create_ticket failed: %s", exc)
        confirm_msg = "Something went wrong while submitting your request. Please try again."

    # ── Write bot confirmation to KEBO_ENGAGE_SESSIONS ────────────────────
    engage_session_service.write_turn_bg(
        session_id=session_id,
        sender="BOT",
        message=confirm_msg,
        entity_id=entity_id,
        group_id=group_id,
        widget_id=widget_id,
        message_type="text",
    )

    # ── Store guest identity in GHN_CHAT_BOT_CUSTOMER_DETAILS (keyed by
    #    Session_Id) so the dashboard shows the name against the ticket.
    #    Processing_Status=PROCESSED => no email is triggered. Fire-and-forget.
    try:
        from config.settings import settings as _settings

        if bool(getattr(_settings, "customer_details_logging_enabled", True)):
            from services.customer_details_service import customer_details_service

            _tp = locals().get("ticket_payload") or {}
            _lf = {str(k).strip().lower(): v for k, v in fields.items()} if isinstance(fields, dict) else {}

            def _pick(*names: str) -> str:
                for n in names:
                    v = _lf.get(n)
                    if v not in (None, ""):
                        return str(v).strip()
                return ""

            _map_widget, _map_group = customer_details_service.resolve_widget_group(hotel_code)
            customer_details_service.write_bg(
                session_id=session_id,
                conversation_id=str(body.get("conversation_id") or body.get("conversationId") or "").strip() or session_id,
                widget_id=_map_widget or widget_id,
                group_id=_map_group or group_id,
                guest_name=_pick("guest_name", "full_name", "name") or str(_tp.get("guest_name") or ""),
                email=_pick("email", "email_id", "email_address"),
                phone=_pick("phone", "mobile", "mobile_number", "contact", "contact_number"),
                city=_pick("city", "location"),
                country=_pick("country"),
                property_name=str(_tp.get("property_name") or "") or _pick("property_name", "property"),
                topic=str(_tp.get("issue") or service_name or ""),
                form_type=str(getattr(_settings, "customer_details_form_type", "ci_form") or "ci_form"),
                processing_status=str(getattr(_settings, "customer_details_processing_status", "PROCESSED") or "PROCESSED"),
                others={k: v for k, v in fields.items() if not str(k).startswith("_")} if isinstance(fields, dict) else None,
            )
    except Exception:
        logger.warning("engage_form_submit: customer_details write skipped", exc_info=True)

    return {
        "success": success,
        "message": confirm_msg,
        "session_id": session_id,
        "form_key": form_key,
        "service_id": service_id,
    }
