"""
Escalation Handler for KePSLA

Handles HUMAN_REQUEST intent by reading the escalation configuration
from config_service and returning an appropriate escalation response.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from handlers.base_handler import BaseHandler, HandlerResult
from integrations.lumira_ticketing_repository import lumira_ticketing_repository
from schemas.chat import ConversationContext, ConversationState, IntentResult, MessageRole
from services.config_service import config_service
from services.ticketing_agent_service import ticketing_agent_service
from services.ticketing_service import ticketing_service

logger = logging.getLogger(__name__)


class EscalationHandler(BaseHandler):
    """Handles requests to escalate to a human agent."""

    async def handle(
        self,
        message: str,
        intent_result: IntentResult,
        context: ConversationContext,
        capabilities: dict,
        db_session=None,
    ) -> Optional[HandlerResult]:
        try:
            # Load escalation settings from business config
            config = config_service.load_config()
            escalation = config.get("escalation", {})

            escalation_message = escalation.get(
                "escalation_message",
                "Let me connect you with our team for better assistance.",
            )
            request_text = str(message or "").strip() or "Guest requested human assistance"
            requested_service = str(
                intent_result.entities.get("requested_service")
                or intent_result.entities.get("service_name")
                or ""
            ).strip()
            ticket_issue_text = self._build_handoff_ticket_issue(
                context=context,
                user_message=request_text,
                requested_service=requested_service,
            )

            latest_ticket = ticketing_service.get_latest_ticket(context)
            ticket_id = str(latest_ticket.get("id") or "").strip()
            ticket_created = False
            ticket_create_error = ""
            matched_case = await ticketing_agent_service.match_configured_case_async(
                message=f"{request_text} {requested_service}".strip(),
                conversation_excerpt=ticket_issue_text,
                llm_response_text=escalation_message,
            )
            if not matched_case:
                matched_case = ticketing_agent_service.match_configured_case(
                    f"{request_text} {requested_service}".strip()
                )
            allow_ticket_create = bool(matched_case)

            if not ticket_id and allow_ticket_create and ticketing_service.is_ticketing_enabled(capabilities):
                payload = ticketing_service.build_lumira_ticket_payload(
                    context=context,
                    issue=ticket_issue_text,
                    message=ticket_issue_text,
                    category="request",
                    sub_category="human_handoff",
                    priority="high",
                    phase="in_stay",
                )
                create_result = await ticketing_service.create_ticket(payload)
                if create_result.success:
                    ticket_id = str(create_result.ticket_id or "").strip()
                    ticket_created = bool(ticket_id)
                    if create_result.assigned_id:
                        latest_ticket["assigned_id"] = str(create_result.assigned_id).strip()
                else:
                    ticket_create_error = create_result.error
            elif not ticket_id and not allow_ticket_create:
                ticket_create_error = "ticket_skipped_no_matching_configured_case"

            handoff_completed = False
            handoff_error = ""
            if ticketing_service.is_handoff_enabled(capabilities):
                integration = ticketing_service.get_integration_context(context)
                conversation_id = str(
                    integration.get("conversation_id")
                    or context.session_id
                    or ""
                ).strip()
                agent_id = str(
                    latest_ticket.get("assigned_id")
                    or integration.get("agent_id")
                    or ""
                ).strip()
                if not agent_id and db_session is not None:
                    agent_id = await lumira_ticketing_repository.fetch_agent_for_handover(
                        db_session,
                        department_id=(
                            latest_ticket.get("department_id")
                            or integration.get("department_id")
                            or ""
                        ),
                        entity_id=integration.get("entity_id") or integration.get("organisation_id"),
                        group_id=integration.get("group_id"),
                    )
                widget_id = str(integration.get("widget_id") or "").strip()
                handoff_result = await ticketing_service.handoff_to_agent(
                    conversation_id=conversation_id,
                    session_id=context.session_id,
                    reason=request_text,
                    agent_id=agent_id,
                    widget_id=widget_id,
                    channel=str(integration.get("channel") or context.channel or "").strip(),
                    flow=str(integration.get("flow") or "").strip(),
                    wa_number=str(
                        integration.get("wa_number")
                        or integration.get("guest_phone")
                        or context.guest_phone
                        or ""
                    ).strip(),
                    entity_id=str(
                        integration.get("entity_id")
                        or integration.get("organisation_id")
                        or ""
                    ).strip(),
                )
                handoff_completed = handoff_result.success
                handoff_error = handoff_result.error

            ticket_line = f"\nTicket ID: {ticket_id}" if ticket_id else ""
            if handoff_completed:
                escalation_message = (
                    "I have connected you to a human agent now. "
                    "Someone from our team will join shortly."
                    f"{ticket_line}"
                )
            elif ticket_id:
                escalation_message = f"{escalation_message}{ticket_line}"

            # -- Agent handoff: register as pending escalation ----------
            # Notify all connected agents for this hotel about the new
            # escalation, and tell the guest they're in the queue.
            try:
                from services.agent_room_service import agent_room_service
                from api.routes.websocket_chat import manager as ws_manager
                guest_ws = ws_manager._connections.get(context.session_id)
                if guest_ws:
                    esc_preview = self._compact_text(request_text, max_len=120)
                    guest_info = {
                        "room_number": str(context.pending_data.get("room_number") or "").strip() if hasattr(context, "pending_data") else "",
                        "guest_name": str(getattr(context, "guest_name", "") or "").strip(),
                        "hotel_code": str(getattr(context, "hotel_code", "") or "").strip(),
                    }
                    _hotel_code = str(getattr(context, "hotel_code", "") or "DEFAULT").strip()
                    agent_room_service.add_pending(
                        session_id=context.session_id,
                        hotel_code=_hotel_code,
                        guest_ws=guest_ws,
                        preview=esc_preview,
                        guest_info=guest_info,
                    )
                    import asyncio
                    asyncio.create_task(
                        agent_room_service.notify_pending_guest_waiting(guest_ws)
                    )
                    asyncio.create_task(
                        agent_room_service.broadcast_to_agents(
                            _hotel_code,
                            {
                                "type": "new_escalation",
                                "session_id": context.session_id,
                                "preview": esc_preview,
                                "guest_info": guest_info,
                                "created_at": __import__("datetime").datetime.utcnow().isoformat(),
                            }
                        )
                    )
                    # Offline fallback: if no agent accepts within the configured
                    # timeout, the bot auto-resumes and the escalation is cleared.
                    _fallback_timeout = int(
                        escalation.get("fallback_timeout_seconds", 300)
                    )
                    asyncio.create_task(
                        agent_room_service.start_fallback_timer(
                            context.session_id, _fallback_timeout
                        )
                    )
            except Exception as _esc_exc:
                logger.warning("[EscalationHandler] agent_room_service notify failed (non-fatal): %r", _esc_exc)

            return HandlerResult(
                response_text=escalation_message,
                next_state=ConversationState.ESCALATED,
                suggested_actions=["Return to bot"],
                metadata={
                    "escalation_reason": "user_requested",
                    "timestamp": datetime.utcnow().isoformat(),
                    "ticket_id": ticket_id,
                    "ticket_assigned_id": str(latest_ticket.get("assigned_id") or "").strip(),
                    "ticket_created": ticket_created,
                    "ticket_create_error": ticket_create_error,
                    "ticket_matched_case": matched_case,
                    "ticket_creation_policy": "configured_cases_only",
                    "handoff_completed": handoff_completed,
                    "handoff_error": handoff_error,
                },
            )

        except Exception as exc:
            logger.exception("EscalationHandler error: %s", exc)
            return HandlerResult(
                response_text="Let me connect you with our team for better assistance.",
                next_state=ConversationState.ESCALATED,
                suggested_actions=["Return to bot"],
                metadata={
                    "escalation_reason": "user_requested",
                    "timestamp": datetime.utcnow().isoformat(),
                },
            )

    @staticmethod
    def _compact_text(value: str, *, max_len: int = 180) -> str:
        cleaned = " ".join(str(value or "").split()).strip()
        if len(cleaned) <= max_len:
            return cleaned
        return cleaned[: max(1, max_len - 3)].rstrip() + "..."

    def _build_handoff_ticket_issue(
        self,
        *,
        context: ConversationContext,
        user_message: str,
        requested_service: str = "",
    ) -> str:
        headline = "Guest requested human handoff"
        service_text = self._compact_text(requested_service, max_len=60)
        if service_text:
            headline += f" for {service_text}"
        headline += "."

        current_request = self._compact_text(user_message, max_len=220)
        recent_user_turns: list[str] = []
        for msg in reversed(context.messages):
            if msg.role != MessageRole.USER:
                continue
            text = self._compact_text(msg.content, max_len=120)
            if not text:
                continue
            if text in recent_user_turns:
                continue
            recent_user_turns.append(text)
            if len(recent_user_turns) >= 4:
                break
        recent_user_turns.reverse()

        details: list[str] = []
        if current_request:
            details.append(f"Current request: {current_request}.")
        if recent_user_turns:
            details.append(f"Recent guest context: {' | '.join(recent_user_turns)}.")

        full_text = " ".join([headline, *details]).strip()
        return self._compact_text(full_text, max_len=600)
