"""Ticketing methods for ChatService (extracted mixin).

Behavior-preserving split: these 44 methods + the two ticketing
constants were moved verbatim out of services/chat_service.py. ChatService
inherits ChatServiceTicketingMixin, so ``self.*`` resolution is unchanged.
This module never imports chat_service — no import cycle.
"""
import logging
import re
from typing import Any

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
from core.complexity_router import ProcessingPath
from core.context_manager import context_manager
from config.settings import settings
from services.config_service import config_service
from services.db_config_service import db_config_service
from services.conversation_memory_service import conversation_memory_service
from services.ticketing_agent_service import ticketing_agent_service
from services.ticketing_service import ticketing_service
from handlers.base_handler import HandlerResult

logger = logging.getLogger("services.chat_service")

_TICKETING_PENDING_ACTIONS = {
    "escalate_complaint",
    "collect_ticket_room_number",
    "collect_ticket_issue_details",
    "collect_ticket_identity_details",
    "confirm_ticket_creation",
    "collect_ticket_update_note",
    "confirm_ticket_escalation",
}
_INTERNAL_PENDING_TICKET_DRAFT_KEY = "_pending_ticket_draft"


class ChatServiceTicketingMixin:
    """Ticketing / complaint handling methods mixed into ChatService."""

    # Which team owns each service, in the SAME words the split judge is given,
    # so its answer can be matched against the ticket about to be raised. Only a
    # naming table - the real department is still resolved by ticketing_service
    # exactly as before; this never decides where a ticket goes.
    _TEAM_BY_SERVICE_ID: dict[str, str] = {
        "maintenance": "Engineering",
        "housekeeping": "Housekeeping",
        "in_room_dining": "Food & Beverage",
        "table_reservation": "Food & Beverage",
        "support_duringstay": "Front Office",
        "support_prechkin": "Front Office",
        "support_postcheckout": "Front Office",
    }

    async def _plan_team_split(
        self, *, guest_message: str, service_id: Any, issue_text: str = "",
    ) -> "dict | None":
        """Does this one message need more than one team? Returns the plan or None.

        Lives here, shared, because tickets are created from FOUR different call
        sites and the first version of this fix was wired into only one of them.
        It worked in the test that happened to take that path and did nothing at
        all on the path real maintenance requests actually use - one ticket, both
        teams' work on it, exactly the bug it was meant to fix. A guard that
        covers one of four doors is not a guard.
        """
        # ── SPLIT THE REQUEST, NOT THE LAST THING TYPED ──────────────────────
        # The ticket is not raised on the turn the guest states the request. It
        # is raised on the turn they CONFIRM it, and on that turn the message is
        # "yes please". Splitting that, the judge answered - correctly - that
        # nothing was asked for at all, returned {"teams": []}, and every
        # two-team request went out as one bundled ticket. The same judge had
        # split the real sentence into Engineering + Housekeeping perfectly one
        # turn earlier; it was never wrong, it was being shown the wrong text.
        # The issue is the summary of what is actually being raised, so that is
        # what has to be split; the typed message is only the fallback for turns
        # that have no summary yet.
        msg = str(issue_text or "").strip() or str(guest_message or "").strip()
        if not msg:
            return None
        team = self._TEAM_BY_SERVICE_ID.get(
            str(service_id or "").strip().lower(), "")
        try:
            # Imported here, not at module scope: chat_service imports this mixin
            # and llm_orchestration_service imports back, so a top-level import
            # is a cycle.
            from services.llm_orchestration_service import llm_orchestration_service

            return await llm_orchestration_service._work_for_a_second_team(
                guest_message=msg, raised_issue="", raised_department=team)
        except Exception as exc:
            logger.warning("[SPLIT] plan skipped (non-fatal): %r", exc)
            return None

    async def _live_departments(self, *, context: Any) -> "list[dict]":
        """The hotel's real departments, straight from its ticketing entity.

        Read live rather than listed here, so a department the hotel adds is
        understood without a code change - and so a team that does not exist can
        never be invented into a ticket.
        """
        try:
            _pd = context.pending_data if isinstance(context.pending_data, dict) else {}
            _int = _pd.get("_integration") if isinstance(_pd.get("_integration"), dict) else {}
            entity = (_int.get("entity_id") or _int.get("organisation_id")
                      or _int.get("org_id"))
            if not entity:
                return []
            from integrations.lumira_ticketing_repository import (
                lumira_ticketing_repository as _ltr,
            )
            from models.database import AsyncSessionLocal as _ASL

            async with _ASL() as _db:
                return await _ltr.fetch_departments_of_entity(_db, entity) or []
        except Exception as exc:
            logger.warning("[TICKET_PLAN] department list unavailable (non-fatal): %r", exc)
            return []

    async def _plan_tickets(
        self, *, context: Any, capabilities_summary: Any = None,
        pre_arrival: bool = False, current_reply: str = "",
    ) -> "list[dict] | None":
        """What tickets does this conversation need right now? [] for most turns.

        The single entry point to ticket planning. Gathers what the judge needs -
        the recent conversation, the hotel's live teams and services, and what is
        already open - and hands back a list the caller raises. It never edits a
        reply and never decides what the guest is told.
        """
        try:
            _turns: list[dict] = []
            for _m in (context.get_recent_messages(14) or []):
                _role = str(getattr(getattr(_m, "role", None), "value",
                                    getattr(_m, "role", ""))).lower()
                _txt = str(getattr(_m, "content", "") or "").strip()
                if not _txt:
                    continue
                _turns.append({"role": "guest" if _role == "user" else "assistant",
                               "text": _txt[:600]})
            # ── THE REPLY THIS TURN IS ABOUT TO SEND ────────────────────────
            # The assistant's message is appended to the conversation by the API
            # route, AFTER the function this planner runs inside has returned -
            # so the window above is the guest talking almost alone. Measured on
            # a live turn: 12 entries, 10 guest, 2 assistant, and the only two
            # were form confirmations written by a different route. Every "yes"
            # arrives orphaned, with no question above it.
            #
            # That is how a two-team request lost half of itself: the guest said
            # "the ac is rattling and the tv remote has no batteries, also
            # collect 3 shirts", the bot answered "passed to Engineering and
            # Housekeeping", and the planner - which could see neither the
            # confirming question nor that promise - raised the laundry alone.
            # The promise the guest just heard is exactly what this has to
            # honour, so it goes in last, where recency puts it in view.
            _reply_now = str(current_reply or "").strip()
            if _reply_now:
                _turns.append({"role": "assistant", "text": _reply_now[:600]})
            # None = could not decide (caller leaves the old behaviour alone);
            # [] = decided nothing needs raising (caller BLOCKS the ticket).
            if not _turns:
                return None

            _depts = await self._live_departments(context=context)
            if not _depts:
                return None

            _svcs: list[dict] = []
            _cat = (capabilities_summary or {}).get("service_catalog") \
                if isinstance(capabilities_summary, dict) else None
            for _s in (_cat or []):
                if isinstance(_s, dict):
                    _svcs.append({"name": str(_s.get("name") or _s.get("id") or ""),
                                  "what_it_handles": str(_s.get("description") or "")})

            _sid = str(getattr(context, "session_id", "") or "").strip()
            _open: list[str] = []
            if _sid:
                for _r in (await ticketing_service.list_session_tickets(_sid, limit=15) or []):
                    if str(_r.get("status") or "open").strip().lower() in ("", "open"):
                        _t = str(_r.get("issue") or _r.get("description") or "").strip()
                        if _t:
                            _open.append(_t)

            # What the ordering flow already has in hand. Without this the planner
            # raised a SECOND Food & Beverage ticket for "place a separate
            # vegetarian order" while that order's own ticket was being raised by
            # the ordering flow - the guest's intention read as a job for staff.
            _food: list[str] = []
            _has_unplaced_cart = False
            try:
                from services import ird_cart_book as _pbk
                from services import ird_cart_store as _pbs

                _prow = await _pbs.load(
                    hotel_code=str(getattr(context, "hotel_code", "") or ""),
                    session_id=_sid, create=False) if _sid else None
                if _prow:
                    for _sl in _pbk.slots_from_row(_prow):
                        if not _sl.get("items"):
                            continue
                        # WHETHER THE KITCHEN ACTUALLY HAS IT decides whether the
                        # planner should keep its hands off. Saying only "this
                        # food is in an order" was too broad: a second order went
                        # to PLACED on an unrelated turn with no ticket behind it,
                        # the planner had been told not to touch food in the book,
                        # and the kitchen never heard about it at all.
                        _ref = str(_sl.get("ticket_ref") or "").strip()
                        if (not _ref) and str(_sl.get("status") or "") == _pbk.ONGOING:
                            _has_unplaced_cart = True
                        _food.append("order %d - %s, %s: %s" % (
                            _sl["n"], _sl.get("status") or "ongoing",
                            ("ALREADY TICKETED as %s" % _ref) if _ref
                            else "NO TICKET RAISED YET",
                            _pbk.render_dishes(_sl)))
            except Exception:
                pass

            from services.llm_orchestration_service import llm_orchestration_service

            _planned = await llm_orchestration_service._tickets_this_turn_needs(
                recent_turns=_turns, services=_svcs, departments=_depts,
                already_open=_open, food_orders=_food,
                room=str((context.pending_data or {}).get("room_number") or ""))
            if not _planned:
                return _planned

            # ── AN UNPLACED CART IS NOT A TICKET, WHATEVER THE TURN SOUNDS LIKE ──
            # A guest adding a latte is told "Added. Anything else?" - an ordering
            # exchange, so the judge kept reading it as a request to send to the
            # kitchen. It is not: the dish is in a cart the guest has not asked to
            # place, and the ordering flow raises that order's ticket when they
            # do. Told this three different ways in the prompt it still slipped,
            # and food tickets are not something to keep re-phrasing at, so the
            # book decides it here. Only the FOOD team is affected; every other
            # team's ticket passes through untouched.
            if _has_unplaced_cart:
                _kept = [t for t in _planned
                         if "food" not in str(t.get("department") or "").lower()
                         and "beverage" not in str(t.get("department") or "").lower()]
                if len(_kept) != len(_planned):
                    logger.info(
                        "[TICKET_PLAN] dropped %d food ticket(s) - the guest has a "
                        "cart they have not asked to place yet",
                        len(_planned) - len(_kept))
                _planned = _kept

            # ── NOTHING THAT NEEDS A ROOM GETS RAISED BEFORE ARRIVAL ─────────
            # The router's phase gate coerces in-stay-only requests to route=book
            # before check-in, but this planner is reached from a different path
            # (the reply-claims-staff-were-told net), so it raised a wake-up call
            # and a laundry pickup for a guest whose stay was a year away. Asked
            # per ticket, not by keyword, and only before arrival - the same
            # judgement the router uses, applied where the work is actually
            # created.
            if pre_arrival and _planned:
                _survivors: list[dict] = []
                for _t in _planned:
                    _issue = str(_t.get("issue") or "").strip()
                    _in_stay = None
                    if _issue:
                        try:
                            _in_stay = await llm_orchestration_service\
                                ._subject_is_in_stay_only(
                                    context=context, user_message=_issue)
                        except Exception:
                            _in_stay = None
                    if _in_stay is True:
                        logger.info(
                            "[TICKET_PLAN] pre-arrival phase gate — not raising %r "
                            "for %s; it can only be done once the guest is in "
                            "the hotel", _issue[:70], _t.get("department"))
                        continue
                    _survivors.append(_t)
                return _survivors
            return _planned
        except Exception as exc:
            logger.warning("[TICKET_PLAN] planning skipped (non-fatal): %r", exc)
            return None

    async def _raise_planned_tickets(
        self, *, plan: "list[dict]", context: Any, guest_message: str,
        sub_category: str = "", priority: str = "medium", phase: str = "",
    ) -> "list[str]":
        """Raise every ticket the planner asked for. Returns the refs created.

        Sequential on purpose: the API returns one reference per call, and a
        failure on the third must never cost the first two.
        """
        refs: list[str] = []
        for t in (plan or []):
            try:
                payload = ticketing_service.build_lumira_ticket_payload(
                    context=context,
                    issue=str(t.get("issue") or ""),
                    message=str(guest_message or ""),
                    category=str(t.get("department") or ""),
                    sub_category=sub_category,
                    priority=priority,
                    phase=phase,
                    department_id=str(t.get("department_id") or ""),
                    department_manager=str(t.get("department") or ""),
                )
                _room = str((context.pending_data or {}).get("room_number") or "").strip()
                if _room:
                    payload["room_number"] = _room
                res = await ticketing_service.create_ticket(payload)
                _ref = str(getattr(res, "ticket_id", "") or "").strip()
                if _ref:
                    refs.append(_ref)
                logger.info("[TICKET_PLAN] raised %s for %s: %r (ok=%s dashboard=%s)",
                            _ref or "-", t.get("department"),
                            str(t.get("issue"))[:60], getattr(res, "success", None),
                            getattr(res, "saved_to_dashboard", None))
            except Exception as exc:
                logger.warning("[TICKET_PLAN] could not raise %r's ticket "
                               "(non-fatal): %r", t.get("department"), exc)
        return refs

    async def _trim_already_raised(
        self, *, context: Any, issue_text: str,
    ) -> "str | None":
        """Drop the parts of this request that staff already have open.

        The issue is rebuilt from the whole conversation every turn, so it
        restates work that was already passed on: once a broken AC and 2 extra
        pillows were raised, asking for towels produced "AC not cooling and 2
        extra pillows and 2 extra towels" and re-raised all three. Nothing
        subtracted what was already in hand.

        Returns the remaining request, "" when all of it is already open, or None
        when nothing could be decided. None and "" are NOT the same: None means
        raise it unchanged. This only ever fails towards raising, because a
        duplicate ticket is a nuisance while a dropped request is a guest waiting
        for something nobody is bringing.

        Runs BEFORE the team split, so the split only ever sees genuinely new
        work and each team's ticket is new by construction.
        """
        issue = str(issue_text or "").strip()
        sid = str(getattr(context, "session_id", "") or "").strip()
        if not issue or not sid:
            return None
        try:
            rows = await ticketing_service.list_session_tickets(sid, limit=15)
            # Only work staff are still holding. A request already completed must
            # never silence a guest asking for it again.
            open_issues = [
                str(r.get("issue") or r.get("description") or "").strip()
                for r in (rows or [])
                if str(r.get("status") or "open").strip().lower() in ("", "open")
            ]
            open_issues = [t for t in open_issues if t]
            if not open_issues:
                return issue

            from services.llm_orchestration_service import llm_orchestration_service

            kept = await llm_orchestration_service._work_not_already_raised(
                new_request=issue, already_raised=open_issues)
            if kept is None:
                return None
            if not kept.strip():
                logger.info("[DEDUPE] all of %r is already open (%d ticket(s)) - "
                            "not raising it again", issue[:70], len(open_issues))
                return ""
            if kept.strip() != issue:
                logger.info("[DEDUPE] already open, dropped from this ticket: "
                            "%r -> %r", issue[:70], kept.strip()[:70])
            return kept.strip()
        except Exception as exc:
            logger.warning("[DEDUPE] skipped (non-fatal): %r", exc)
            return None

    async def _raise_other_team_tickets(
        self, *, plan: "dict | None", context: Any, guest_message: str,
        sub_category: str, priority: str, phase: str,
    ) -> None:
        """Raise one ticket per OTHER team, one after the other.

        Sequential on purpose: the external API returns one reference per call,
        and a failure on the second must never cost us the first - so each is
        created and logged on its own, and an exception is contained to that team.
        """
        for other in (plan or {}).get("others", []):
            try:
                payload = ticketing_service.build_lumira_ticket_payload(
                    context=context,
                    issue=str(other["issue"]),
                    message=str(guest_message or ""),
                    category=str(other["department"]),
                    sub_category=sub_category,
                    priority=priority,
                    phase=phase,
                    department_id="",
                    department_manager=str(other["department"]),
                )
                result = await ticketing_service.create_ticket(payload)
                logger.info(
                    "[SPLIT] team %r raised separately: ticket=%s ok=%s issue=%r",
                    other["department"], getattr(result, "ticket_id", None),
                    getattr(result, "success", None), str(other["issue"])[:70])
            except Exception as exc:
                logger.warning("[SPLIT] could not raise %r's request "
                               "(non-fatal): %r", other.get("department"), exc)

    def _resolve_pure_llm_ticket_service(
        self,
        *,
        capabilities_summary: dict[str, Any],
        llm_result: Any,
    ) -> dict[str, Any] | None:
        service_catalog = capabilities_summary.get("service_catalog", [])
        if not isinstance(service_catalog, list):
            return None

        id_index: dict[str, dict[str, Any]] = {}
        name_index: dict[str, dict[str, Any]] = {}
        for service in service_catalog:
            if not isinstance(service, dict):
                continue
            service_id = self._normalize_service_identifier(service.get("id"))
            if service_id:
                id_index[service_id] = service
            service_name = self._normalize_service_identifier(service.get("name"))
            if service_name and service_name not in name_index:
                name_index[service_name] = service

        llm_output = llm_result.llm_output if isinstance(getattr(llm_result, "llm_output", None), dict) else {}
        pending_data = llm_result.pending_data if isinstance(getattr(llm_result, "pending_data", None), dict) else {}
        entities = self._extract_full_kb_entities_for_handler(llm_result)

        service_id_candidates = (
            llm_output.get("service_id"),
            llm_output.get("target_service_id"),
            llm_output.get("resolved_service_id"),
            llm_output.get("workflow_id"),
            pending_data.get("service_id"),
            pending_data.get("target_service_id"),
            pending_data.get("resolved_service_id"),
            pending_data.get("workflow_id"),
            entities.get("service_id"),
            entities.get("target_service_id"),
            entities.get("resolved_service_id"),
            entities.get("workflow_id"),
            entities.get("booking_sub_category"),
            pending_data.get("booking_sub_category"),
            pending_data.get("sub_category"),
            llm_output.get("ticket_sub_category"),
        )
        for candidate in service_id_candidates:
            key = self._normalize_service_identifier(candidate)
            if key and key in id_index:
                return id_index[key]

        service_name_candidates = (
            llm_output.get("service_name"),
            llm_output.get("target_service"),
            pending_data.get("service_name"),
            pending_data.get("target_service"),
            entities.get("service_name"),
            entities.get("target_service"),
        )
        for candidate in service_name_candidates:
            key = self._normalize_service_identifier(candidate)
            if key and key in name_index:
                return name_index[key]
        return None

    async def _maybe_create_pure_llm_ticket(
        self,
        *,
        request: ChatRequest,
        context: ConversationContext,
        capabilities_summary: dict[str, Any],
        llm_result: Any,
        effective_intent: IntentType,
    ) -> dict[str, Any]:
        ticket_source = "pure_llm"
        if not self._is_ticketing_plugin_enabled():
            return {
                "ticket_create_skipped": True,
                "ticket_create_skip_reason": "ticketing_plugin_disabled",
                "ticket_source": ticket_source,
            }
        if not ticketing_service.is_ticketing_enabled(capabilities_summary):
            return {
                "ticket_create_skipped": True,
                "ticket_create_skip_reason": "ticketing_service_disabled",
                "ticket_source": ticket_source,
            }

        pending = llm_result.pending_data if isinstance(getattr(llm_result, "pending_data", None), dict) else {}
        if str(pending.get("ticket_id") or "").strip():
            return {
                "ticket_create_skipped": True,
                "ticket_create_skip_reason": "ticket_already_present_in_pending_data",
                "ticket_source": ticket_source,
            }

        llm_ticketing_preference = self._extract_full_kb_ticketing_preference(llm_result)
        if llm_ticketing_preference is not True:
            return {}
        ticket_ready_to_create = self._is_pure_llm_ticket_ready(llm_result)
        if not ticket_ready_to_create:
            return {
                "ticket_create_skipped": True,
                "ticket_create_skip_reason": "ticket_not_ready_to_create",
                "ticket_source": ticket_source,
            }

        service = self._resolve_pure_llm_ticket_service(
            capabilities_summary=capabilities_summary,
            llm_result=llm_result,
        )
        if not isinstance(service, dict):
            return {
                "ticket_create_skipped": True,
                "ticket_create_skip_reason": "service_unresolved_for_ticketing",
                "ticket_source": ticket_source,
            }

        selected_phase = self._get_selected_phase_context(
            context=context,
            pending_data=context.pending_data if isinstance(context.pending_data, dict) else {},
            entities=self._extract_full_kb_entities_for_handler(llm_result),
        )
        current_phase_id = self._normalize_phase_identifier(selected_phase.get("selected_phase_id"))
        service_phase_id = self._normalize_phase_identifier(service.get("phase_id"))
        if current_phase_id and service_phase_id and current_phase_id != service_phase_id:
            return {
                "ticket_create_skipped": True,
                "ticket_create_skip_reason": "phase_service_mismatch",
                "ticket_source": ticket_source,
                "phase_gate_service_id": str(service.get("id") or "").strip(),
                "phase_gate_service_name": str(service.get("name") or "").strip(),
                "phase_gate_current_phase_id": current_phase_id,
                "phase_gate_current_phase_name": str(selected_phase.get("selected_phase_name") or "").strip(),
                "phase_gate_service_phase_id": service_phase_id,
                "phase_gate_service_phase_name": str(service.get("phase_name") or self._phase_label(service_phase_id)).strip(),
            }

        if "ticketing_enabled" in service and not bool(service.get("ticketing_enabled")):
            return {
                "ticket_create_skipped": True,
                "ticket_create_skip_reason": "phase_service_ticketing_disabled",
                "ticket_source": ticket_source,
                "phase_gate_service_id": str(service.get("id") or "").strip(),
                "phase_gate_service_name": str(service.get("name") or "").strip(),
                "phase_gate_current_phase_id": current_phase_id,
                "phase_gate_current_phase_name": str(selected_phase.get("selected_phase_name") or "").strip(),
                "phase_gate_service_phase_id": service_phase_id,
                "phase_gate_service_phase_name": str(service.get("phase_name") or self._phase_label(service_phase_id)).strip(),
            }

        llm_output = llm_result.llm_output if isinstance(getattr(llm_result, "llm_output", None), dict) else {}
        entities = self._extract_full_kb_entities_for_handler(llm_result)
        issue = self._first_non_empty(
            llm_output.get("ticket_issue"),
            pending.get("ticket_issue"),
            entities.get("ticket_issue"),
            llm_output.get("ticket_summary"),
            pending.get("ticket_summary"),
            entities.get("ticket_summary"),
            self._extract_full_kb_ticketing_reason(llm_result),
            llm_result.normalized_query,
            request.message,
        )
        if not issue:
            return {
                "ticket_create_skipped": True,
                "ticket_create_skip_reason": "empty_issue",
                "ticket_source": ticket_source,
            }

        # ── WHAT OF THIS DO STAFF ALREADY HAVE? ──────────────────────────────
        # Before anything else is decided, because it changes what this ticket
        # says and whether it should exist at all.
        _kept = await self._trim_already_raised(context=context, issue_text=issue)
        if _kept is not None and not _kept:
            return {
                "ticket_create_skipped": True,
                "ticket_create_skip_reason": "work_already_raised_this_session",
                "ticket_source": ticket_source,
            }
        if _kept:
            issue = _kept

        category = self._first_non_empty(
            llm_output.get("ticket_category"),
            pending.get("ticket_category"),
            entities.get("ticket_category"),
        ).lower()
        if category not in {"complaint", "request", "query"}:
            category = "complaint" if effective_intent in {IntentType.COMPLAINT, IntentType.HUMAN_REQUEST} else "request"

        sub_category = self._first_non_empty(
            llm_output.get("ticket_sub_category"),
            pending.get("ticket_sub_category"),
            entities.get("ticket_sub_category"),
            pending.get("sub_category"),
            entities.get("sub_category"),
            str(service.get("id") or "").strip(),
        ) or "general_request"

        priority = self._first_non_empty(
            llm_output.get("ticket_priority"),
            pending.get("ticket_priority"),
            entities.get("ticket_priority"),
        ).upper()
        if priority not in {"LOW", "MEDIUM", "HIGH", "CRITICAL"}:
            priority = "MEDIUM"

        phase_name = self._first_non_empty(
            selected_phase.get("selected_phase_name"),
            str(service.get("phase_name") or ""),
            self._phase_label(current_phase_id),
            "During Stay",
        )
        room_number = self._first_non_empty(
            llm_result.room_number,
            context.room_number,
            pending.get("room_number"),
            entities.get("room_number"),
        )

        # ── DOES THIS MESSAGE NEED A SECOND TEAM? ────────────────────────────
        # Asked BEFORE the ticket is built, because the answer decides what THIS
        # ticket says too: without the trim it read "Fix AC not cooling and
        # provide 2 extra pillows", so Engineering was handed Housekeeping's work
        # and both teams thought the pillows were theirs.
        _split = await self._plan_team_split(
            guest_message=str(getattr(request, "message", "") or ""),
            service_id=service.get("id"), issue_text=str(issue or ""))
        if _split and _split.get("first_issue"):
            logger.info("[SPLIT] this ticket keeps %r (was %r)",
                        str(_split["first_issue"])[:70], str(issue)[:70])
            issue = str(_split["first_issue"]).strip()

        try:
            payload = ticketing_service.build_lumira_ticket_payload(
                context=context,
                issue=issue,
                message=issue,
                category=category,
                sub_category=sub_category,
                priority=priority,
                phase=phase_name,
                # Left blank on purpose: this ticket is raised BY THE BOT, so
                # the source is resolved from the channel (web widget ->
                # booking_bot). It used to say "manual", which told the
                # dashboard a human had typed it in and cost the ticket its
                # place in the conversation on the chat-history page.
                source="",
            )
            if room_number:
                payload["room_number"] = room_number
            create_result = await ticketing_service.create_ticket(payload)
        except Exception as exc:
            logger.exception("pure_llm_ticket_create_failed")
            return {
                "ticket_create_failed": True,
                "ticket_error": str(exc),
                "ticket_source": ticket_source,
            }

        if not create_result.success:
            return {
                "ticket_create_failed": True,
                "ticket_error": str(create_result.error or "ticket_create_failed"),
                "ticket_source": ticket_source,
                "ticket_api_status_code": create_result.status_code,
                "ticket_api_response": create_result.response,
            }

        # The first team's ticket exists; now the others, one after the other.
        await self._raise_other_team_tickets(
            plan=_split, context=context,
            guest_message=str(getattr(request, "message", "") or ""),
            sub_category=sub_category, priority=priority, phase=phase_name)

        return {
            "ticket_created": True,
            "ticket_id": str(create_result.ticket_id or "").strip(),
            "ticket_status": "open",
            "ticket_category": category,
            "ticket_sub_category": sub_category,
            "ticket_priority": priority,
            "ticket_summary": issue[:180],
            "ticket_source": ticket_source,
            "room_number": room_number,
            "ticket_api_status_code": create_result.status_code,
            "ticket_api_response": create_result.response,
            "ticket_service_id": str(service.get("id") or "").strip(),
            "ticket_service_name": str(service.get("name") or service.get("id") or "").strip(),
        }

    def _can_create_orchestration_ticket_now(
        self,
        *,
        decision: OrchestrationDecision,
        policy_result: Any,
        next_state: ConversationState,
        pending_action: str | None,
    ) -> tuple[bool, str]:
        if not bool(decision.ticket.required):
            return False, "ticket_not_required"
        # A respond_only turn must NEVER spawn a ticket. When a guest cancels
        # something that was never committed (e.g. a form they never submitted),
        # the orchestrator correctly resolves the turn to a plain reply, but the
        # decision can still carry a stale ticket.required=True from the service
        # it passed through. Creating a ticket here produced a spurious "cancel"
        # ticket and overwrote the reply with the generic booking template.
        # Cancels/edits of REAL tickets are handled upstream by
        # _maybe_handle_ticket_edit (adds a note + flips status to cancel on the
        # existing ticket), never here.
        if str(getattr(decision, "action", "") or "").strip().lower() == "respond_only":
            return False, "respond_only_no_ticket"
        # A FORM-MODE service raises its ticket when the guest SUBMITS the form
        # (POST /form_submit builds it from the collected fields). Creating one
        # here beats the form to it: the turn is marked ticket_created, the form
        # builder then skips, and the guest is left with a promise of a form that
        # never opens — while the ticket that did get raised carries no name,
        # phone or concern. Seen live on "connect me to the front desk", which
        # logged a Front Office ticket with nothing in it.
        _svc_id = str(getattr(decision, "target_service_id", "") or "").strip()
        if _svc_id:
            try:
                _svc = config_service.get_service(_svc_id)
                if _svc and str(_svc.get("ticketing_mode") or "").strip().lower() == "form":
                    return False, "form_mode_ticket_raised_on_submit"
            except Exception:
                pass
        # Do not require final action text to remain "create_ticket" here.
        # Downstream quality guards can rephrase action while ticket intent
        # remains explicitly ready via decision.ticket fields.
        if not bool(decision.ticket.ready_to_create):
            return False, "ticket_not_ready_to_create"
        if bool(decision.missing_fields):
            return False, "ticket_missing_required_fields"
        _pending = str(pending_action or "").strip().lower()
        # Confirmation state is the trigger for ticket creation — do not block it
        _is_confirmation_turn = _pending.startswith("confirm_")
        if _pending and not _is_confirmation_turn:
            return False, "ticket_pending_followup"
        # ...and the waiting-state check must agree with the line above. It did not:
        # a confirm_* pending action is exactly what makes _derive_orchestration_state
        # return AWAITING_CONFIRMATION, so the check we had just exempted came
        # straight back here and blocked the same turn. A guest who confirmed a food
        # order was told it was on its way while the ticket was dropped on that
        # contradiction. AWAITING_INFO / AWAITING_SELECTION still block, as they mean
        # we are genuinely missing something.
        if not _is_confirmation_turn and self._is_waiting_state_for_user_input(next_state):
            return False, "ticket_waiting_for_user_input"
        return True, ""

    async def _maybe_manage_room_booking_ticket(
        self,
        *,
        context: ConversationContext,
        response_text: str,
        decision: Any = None,
        user_message: str = "",
        selected_phase_context: dict[str, Any] | None = None,
    ) -> tuple[str, bool]:
        """Manage room-booking staff tickets across a conversation.

        - Explicit BOOK intent -> create a silent ticket (reply offers website link OR
          team follow-up, generated by the prompt).
        - Per-PROPERTY dedup: same property again = no new ticket.
        - Different property while one exists -> ASK to cancel the MOST RECENT prior
          booking (control reply, no link yet); next turn yes = cancel+create, no = create.
        - 'change dates' -> UPDATE the in-focus booking's ticket (both dashboards).

        Returns (response_text, is_control_response). A control response is authored here
        (cancel question / post-confirm / date confirmation) and must NOT be rewritten.
        """
        import logging as _lg
        from services.booking_assistance_state import details_submitted as _rb_details_submitted

        if decision is None or not bool(getattr(settings, "ticketing_room_booking_silent_ticket_enabled", True)):
            return response_text, False
        pd = context.pending_data if isinstance(context.pending_data, dict) else None
        if pd is None:
            return response_text, False
        _msg = str(user_message or "").strip()
        _sid = str(getattr(context, "session_id", "") or "")
        _conv_uid = str(getattr(context, "conversation_uid", "") or pd.get("conversation_uid") or "").strip()

        # A SINGLE-PROPERTY tenant has nothing to choose between, but on turn 1 the
        # conversation carries no property scope yet — so this lane would ask
        # "Which property would you like to book?" about the only hotel there is,
        # and the booking lead would never be raised. Seed the scope from the
        # tenant's own manifest. Multi-property tenants (Zone: 24) are untouched.
        if not pd.get("_active_property_ids") and not pd.get("selected_property_ids"):
            try:
                _biz = await db_config_service.get_business_info()
                if int((_biz or {}).get("property_count") or 0) == 1:
                    for _entry in ((_biz or {}).get("multi_property_location_manifest") or []):
                        if not isinstance(_entry, dict):
                            continue
                        _ids = [str(i).strip() for i in (_entry.get("property_ids") or []) if str(i).strip()]
                        _names = [str(n).strip() for n in (_entry.get("property_names") or []) if str(n).strip()]
                        if len(_ids) == 1:
                            pd["_active_property_ids"] = _ids
                            if _names:
                                pd["_active_property_names"] = _names
                            break
            except Exception as _e:
                _lg.getLogger(__name__).debug("single-property scope seed skipped: %s", _e)

        # (0a) ABANDON a pending switch when the guest named a DIFFERENT
        # property/location this turn (the router selected ids that do NOT overlap
        # the switch's target). They've moved on from the cancel/keep question, so
        # do NOT force their new request into a yes/no — drop the pending switch
        # and let the normal flow handle what they actually asked. Fixes: a pending
        # "cancel Jaipur or keep?" swallowing "change my location to Kolkata".
        _pending0 = pd.get("_rb_pending_switch")
        if isinstance(_pending0, dict) and _pending0 and _msg:
            _cur_sel = set(
                str(x).strip()
                for x in (getattr(decision, "selected_property_ids", None) or [])
                if str(x).strip()
            )
            _pend_new = set(
                str(x).strip() for x in (_pending0.get("new_scope_ids") or []) if str(x).strip()
            )
            if _cur_sel and _pend_new and not (_cur_sel & _pend_new):
                pd.pop("_rb_pending_switch", None)
                pd.pop("_rb_switch_reask_count", None)
                _lg.getLogger(__name__).info(
                    "RB switch abandoned (guest named a different property) session=%s", _sid
                )

        # (0) Resolve a pending property-SWITCH confirmation (runs on ANY turn).
        pending = pd.get("_rb_pending_switch")
        if isinstance(pending, dict) and pending and _msg:
            verdict = await self._rb_classify_confirm(_msg)
            prev_prop = str(pending.get("prev_property") or "your previous booking")
            new_prop = str(pending.get("new_property") or "")
            _lg.getLogger(__name__).info("RB switch-confirm verdict=%s prev=%r new=%r session=%s", verdict, prev_prop, new_prop, _sid)
            if verdict == "unclear":
                # The guest did NOT answer cancel/keep — they asked something else
                # (a normal question, a location change, a new topic). The cancel/keep
                # is a nice-to-have confirmation, NOT a gate: do NOT hijack the turn or
                # re-ask. Default to KEEP (the safe, non-destructive choice) — silently
                # set up the new booking, keep the earlier one, drop the pending
                # question — and let THIS turn answer what the guest actually asked.
                try:
                    await self._rb_create_booking(
                        context=context, pd=pd, prop=new_prop,
                        scope_key=str(pending.get("new_scope_key") or ""),
                        scope_ids=list(pending.get("new_scope_ids") or []),
                        check_in=str(pending.get("check_in") or ""),
                        check_out=str(pending.get("check_out") or ""),
                        entity_override=str(pending.get("new_entity") or ""),
                        selected_phase_context=selected_phase_context,
                    )
                except Exception as _e:
                    _lg.getLogger(__name__).warning("RB unclear-default-keep create failed: %s", _e)
                pd.pop("_rb_pending_switch", None)
                pd.pop("_rb_switch_reask_count", None)
                _lg.getLogger(__name__).info(
                    "RB switch defaulted to KEEP (guest moved on) prev=%r new=%r session=%s",
                    prev_prop, new_prop, _sid,
                )
                return response_text, False
            lead = ""
            if verdict == "yes":
                _cancel_ok = True
                try:
                    _cancel_res = await ticketing_service.cancel_room_booking_ticket(
                        session_id=_sid, property_name=prev_prop,
                        conversation_uid=_conv_uid, reason=f"Guest switched to {new_prop}.",
                    )
                    _cancel_ok = bool(getattr(_cancel_res, "success", True))
                except Exception as _e:
                    _cancel_ok = False
                    _lg.getLogger(__name__).warning("RB cancel-on-switch failed: %s", _e)
                pd["_rb_bookings"] = [
                    b for b in (pd.get("_rb_bookings") or [])
                    if str(b.get("property") or "") != prev_prop
                ]
                # Say "cancelled" only when the cancel landed. Hotels set up in the
                # Pre-booking tab (T24 onward) get a refused cancel reported back
                # (see _mutate_room_booking_ticket); every other hotel keeps the old
                # line, whose cancel result was never checked.
                from services import prebooking_settings_service as _pbs_sw

                if _cancel_ok or not _pbs_sw.is_admin_managed(str(getattr(context, "hotel_code", "") or "").strip().lower()):
                    lead = f"Done — I've cancelled your earlier request for **{prev_prop}**. "
                else:
                    _lg.getLogger(__name__).warning(
                        "RB cancel-on-switch NOT accepted prev=%r session=%s", prev_prop, _sid,
                    )
                    lead = (
                        f"I couldn't cancel your earlier request for **{prev_prop}** here, so our team "
                        f"will take care of it. "
                    )
            await self._rb_create_booking(
                context=context, pd=pd, prop=new_prop,
                scope_key=str(pending.get("new_scope_key") or ""),
                scope_ids=list(pending.get("new_scope_ids") or []),
                check_in=str(pending.get("check_in") or ""), check_out=str(pending.get("check_out") or ""),
                entity_override=str(pending.get("new_entity") or ""),
                selected_phase_context=selected_phase_context,
            )
            pd.pop("_rb_pending_switch", None)
            pd.pop("_rb_switch_reask_count", None)
            # Make the website button render for the new property on this turn.
            try:
                _ids = list(pending.get("new_scope_ids") or [])
                if _ids:
                    decision.selected_property_ids = _ids
                    pd["_active_property_ids"] = _ids
            except Exception:
                pass
            resp = (
                lead
                + f"I've set up your booking request for **{new_prop}**. "
                + self._rb_followup_clause(below=True)
                + (f"\n<<SHOW_BOOKING_BUTTON: {new_prop}>>" if new_prop else "")
            )
            return resp, True

        # ── from here, only turns routed to the room-booking handler ──
        _target = str(getattr(decision, "target_service_id", "") or getattr(decision, "service_id", "") or "")
        if self._normalize_service_identifier(_target) not in self._RB_ROOM_TARGETS or not _msg:
            return response_text, False

        intent = await self._rb_classify_intent(_msg, response_text)
        # A room price / availability question at a hotel whose booking engine
        # shows live rates: one line and one "Check live rates" button. After the
        # booking form is sent the agent's own answer stands, like every button.
        # Also after the booking form is sent: answered by the agent, "is it available
        # on 3 Nov?" came back as "your booking request for 3 November is already with
        # our team" for a request sent for 1-3 December. The link answers it plainly.
        if intent == "price":
            _prop_now, _sk, _sids = self._rb_scope(context, pd)
            _lg.getLogger(__name__).info("RB intent=price -> live rates button msg=%r session=%s", _msg[:60], _sid)
            # This turn's chips were started from the agent's answer that this reply
            # replaces ("...would you like our team to reach out?" gave "Yes, please /
            # No, thanks"). Drop that job; the chat route then builds the chips from
            # the reply the guest actually sees.
            try:
                from services.llm_orchestration_service import llm_orchestration_service as _los_chips
                _los_chips._cancel_post_response_chip_job(_sid)
            except Exception as _chip_exc:
                _lg.getLogger(__name__).debug("RB price chips reset skipped: %s", _chip_exc)
            return await self._rb_live_rates_reply(
                context, pd, _msg, _prop_now, submitted=_rb_details_submitted(context)
            ), True
        # ADD A ROOM to the request the guest already sent (Mango only - the
        # classifier answers add_room for a team-only hotel alone). Our own reply
        # says "If you would like to change the dates or add another room, just
        # tell me here", yet "i need to add another room" came back as BOOK and got
        # that same sentence again, three times, while the ticket never heard of it
        # (Mango, 9 Oct). Before the booking form is sent there is nothing to add
        # to, so it stays an ordinary booking request.
        if intent == "add_room":
            from services.booking_assistance_state import booking_ledger as _rb_booking_ledger

            _sent = [b for b in _rb_booking_ledger(context) if b.get("details_submitted") is True]
            if not _sent:
                intent = "book"
            else:
                _prop_now, _sk, _sids = self._rb_scope(context, pd)
                _key_now = str(_prop_now or "").strip().casefold()
                target_bk = next(
                    (b for b in _sent if _key_now and str(b.get("property") or "").strip().casefold() == _key_now),
                    None,
                ) or _sent[-1]
                tprop = str(target_bk.get("property") or _prop_now or "")
                _res = None
                try:
                    # A note only: the request line and description hold what the
                    # guest typed in the form (guests, dates), and an issue rewrite
                    # here would overwrite them.
                    _res = await ticketing_service.update_room_booking_ticket(
                        session_id=_sid, property_name=tprop, conversation_uid=_conv_uid,
                        note=f'Guest asked to add another room to this booking request: "{_msg[:200]}"',
                    )
                except Exception as _e:
                    _lg.getLogger(__name__).warning("RB add-room update failed: %s", _e)
                _lg.getLogger(__name__).info(
                    "RB intent=add_room prop=%r updated=%s session=%s",
                    tprop, bool(getattr(_res, "success", False)), _sid,
                )
                if tprop and getattr(_res, "success", False):
                    # The chips were started from the agent's answer this replaces.
                    try:
                        from services.llm_orchestration_service import llm_orchestration_service as _los_chips
                        _los_chips._cancel_post_response_chip_job(_sid)
                    except Exception as _chip_exc:
                        _lg.getLogger(__name__).debug("RB add-room chips reset skipped: %s", _chip_exc)
                    return (
                        f"Done — I've added another room to your booking request for **{tprop}**"
                        + self._rb_dates_phrase(
                            str(target_bk.get("check_in") or ""), str(target_bk.get("check_out") or "")
                        )
                        + ". Our team will confirm it with you."
                    ), True
                # The ticket could not be reached: say where the request stands
                # (the BOOK path below) rather than claim a change nobody made.
                intent = "book"
        if intent not in ("book", "change_dates"):
            _lg.getLogger(__name__).info("RB intent=%s msg=%r session=%s", intent, _msg[:60], _sid)
            return response_text, False
        check_in, check_out = await self._rb_extract_dates(_msg)
        _lg.getLogger(__name__).info(
            "RB intent=%s dates=%r-%r msg=%r session=%s",
            intent, check_in, check_out, _msg[:60], _sid,
        )

        prop, scope_key, scope_ids = self._rb_scope(context, pd)
        bookings = list(pd.get("_rb_bookings") or [])

        # (2) DATE CHANGE -> update the in-focus (or most recent) booking's ticket.
        if intent == "change_dates":
            if not bookings:
                return response_text, False
            target_bk = next((b for b in bookings if scope_key and b.get("scope_key") == scope_key), None) or bookings[-1]
            _in, _out = str(check_in or "").strip(), str(check_out or "").strip()
            if (_in and not _out) or (_out and not _in):
                # Guest gave ONE new date ("make it 25 March") — MOVE the stay to that
                # date; do not merge it with the old counterpart into a phantom range.
                ci, co = (_in or _out), ""
            else:
                ci = _in or str(target_bk.get("check_in") or "")
                co = _out or str(target_bk.get("check_out") or "")
            target_bk["check_in"], target_bk["check_out"] = ci, co
            pd["_rb_bookings"] = bookings
            tprop = str(target_bk.get("property") or prop or "")
            room = str((pd.get("room_type") or pd.get("room_name") or "") if pd else "").strip()
            try:
                await ticketing_service.update_room_booking_ticket(
                    session_id=_sid, property_name=tprop, conversation_uid=_conv_uid,
                    new_issue=self._rb_make_issue(tprop, room, ci, co),
                    note=f"Guest updated stay dates to {ci} - {co}.",
                )
            except Exception as _e:
                _lg.getLogger(__name__).warning("RB date update failed: %s", _e)
            # Team-only hotel, form not sent yet: same as the rebook-with-dates path.
            if tprop and self._rb_team_only_hotel() and not _rb_details_submitted(context):
                return self._rb_booking_link_reply(tprop, ci, co, context), True
            when = self._rb_dates_span(ci, co)
            return (
                f"Done — I've updated your booking request for **{tprop}** to {when}. "
                + self._rb_followup_clause()
                + (f"\n<<SHOW_BOOKING_BUTTON: {tprop}>>" if tprop else "")
            ), True

        # (3) BOOK.
        if self._rb_needs_property_choice(pd):
            # Pre-booking-tab hotels (T24 onward): the room-booking agent's prompt
            # (DB) covers this case - each hotel's rooms, then "which hotel would you
            # like?" - so its answer stands. Swapping it for the fixed question below
            # threw that answer away in a fresh conversation (no hotel in focus yet)
            # and the guest got a bare "Which property would you like to book?".
            # No ticket either way until a hotel is chosen.
            from services import prebooking_settings_service as _pbs_choice

            if str(response_text or "").strip() and _pbs_choice.is_admin_managed(
                str(getattr(context, "hotel_code", "") or "").strip().lower()
            ):
                _lg.getLogger(__name__).info("RB book, no hotel chosen -> agent's answer kept session=%s", _sid)
                # The guest's next message names the hotel to book; a tapped hotel
                # name then continues the booking instead of opening its intro.
                pd["_awaiting_hotel_for"] = _target
                return response_text, False
            return self._rb_property_choice_reply(pd), True
        room = str((pd.get("room_type") or pd.get("room_name") or "") if pd else "").strip()
        # Resolve the FMS entity (robust dedup + property-switch detection).
        try:
            _probe = ticketing_service.build_lumira_ticket_payload(
                context=context, issue=self._rb_make_issue(prop, room, check_in, check_out),
                message="", category="room_booking", sub_category="room_booking",
                priority="LOW", phase="", source="room_booking_intent",
            )
            entity = str(_probe.get("organisation_id") or "").strip().lower()
        except Exception:
            entity = ""
        # SAME property already booked? (match on scope key OR FMS entity OR the
        # property name). The name is what catches a booking the guest made
        # through the in-chat details FORM: that path (submit_booking_assistance)
        # appends its row with property/dates/ticket_ref but NO scope_key and NO
        # entity, so the first two tests both missed it, this ran on as a first
        # booking, and the fire-and-forget create raised a SECOND, dateless
        # ticket one turn later — while the reply, which reads the same row's
        # `details_submitted`, correctly said the request was already with the
        # team. Words and actions disagreed.
        # The form is only how MANGO trips this (it is the team-only hotel).
        # Zone trips the same matcher harder — 8 same-session/same-property
        # duplicates on prod to Mango's 4, one Saket session with 5 — because
        # scope_key and entity drift between turns, so both tests miss for a
        # property already booked. The property name is the one key every writer
        # of this ledger fills in, so it catches both. Widening a dedup test can
        # only suppress a duplicate, never invent one.
        _prop_key = str(prop or "").strip().casefold()
        matched = next(
            (
                b for b in bookings
                if (scope_key and b.get("scope_key") == scope_key)
                or (entity and b.get("entity") == entity)
                or (_prop_key and str(b.get("property") or "").strip().casefold() == _prop_key)
            ),
            None,
        )
        if matched is not None:
            mprop = str(matched.get("property") or prop or "")
            old_ci, old_co = str(matched.get("check_in") or ""), str(matched.get("check_out") or "")
            _in, _out = str(check_in or "").strip(), str(check_out or "").strip()
            if (_in and not _out) or (_out and not _in):
                # Single new date = MOVE the stay to that date, not a range with the old bound.
                new_ci, new_co = (_in or _out), ""
            else:
                new_ci, new_co = (_in or old_ci), (_out or old_co)
            # Re-booking the SAME property with NEW dates ("actually book it for Nov 1-3")
            # is a DATE CHANGE — update the existing ticket; never drop the new dates.
            if (check_in or check_out) and (new_ci, new_co) != (old_ci, old_co):
                matched["check_in"], matched["check_out"] = new_ci, new_co
                pd["_rb_bookings"] = bookings
                try:
                    await ticketing_service.update_room_booking_ticket(
                        session_id=_sid, property_name=mprop, conversation_uid=_conv_uid,
                        new_issue=self._rb_make_issue(mprop, room, new_ci, new_co),
                        note=f"Guest updated stay dates to {new_ci} - {new_co}.",
                    )
                except Exception as _e:
                    _lg.getLogger(__name__).warning("RB rebook date update failed: %s", _e)
                if not mprop:
                    return response_text, False
                # Team-only hotel, form not sent yet: the silent ticket carries the
                # new dates, but the guest has sent nothing - ask for the details.
                if self._rb_team_only_hotel() and not _rb_details_submitted(context):
                    return self._rb_booking_link_reply(mprop, new_ci, new_co, context), True
                when = self._rb_dates_span(new_ci, new_co)
                return (
                    f"Done — I've updated your booking request for **{mprop}** to {when}. "
                    + self._rb_followup_clause()
                    + f"\n<<SHOW_BOOKING_BUTTON: {mprop}>>"
                ), True
            # Same property, no new dates -> just re-surface the link.
            # EXCEPT for a team-only hotel: there is no website link to re-surface,
            # and its "share your booking details" button is already spent for this
            # conversation, so repeating that line points the guest at a button that
            # is no longer on screen. Tell them where the request actually stands.
            # Only once the FORM is in: the ledger row is written when the button is
            # first SHOWN (silent lead ticket), so without this a guest who never
            # filled the form was told "already with our team" (Mango, 7 Oct). Not
            # submitted yet -> the link reply below offers the button again.
            if mprop and self._rb_team_only_hotel() and _rb_details_submitted(context):
                return (
                    f"Your booking request for **{mprop}**"
                    + self._rb_dates_phrase(old_ci, old_co)
                    + " is already with our team, and they will get back to you to confirm."
                    + (" You can also book directly on our website using Book on website."
                       if self._rb_live_rates_enabled() else "")
                    + " If you would like to change the dates or add another room, just tell me here."
                ), True
            return (self._rb_booking_link_reply(mprop, old_ci, old_co, context), True) if mprop else (response_text, False)
        others = [
            b for b in bookings
            if (entity and b.get("entity") and b.get("entity") != entity)
            or (scope_key and b.get("scope_key") and b.get("scope_key") != scope_key)
        ]
        if others:
            prev = others[-1]  # MOST RECENT prior booking for a different property
            # Capture the NEW property's entity NOW (scope is correct this turn) so the
            # post-confirm create uses the right FMS id even if the yes/no turn's scope
            # has drifted back to the previous property.
            pd["_rb_pending_switch"] = {
                "new_property": prop, "new_scope_key": scope_key, "new_scope_ids": scope_ids,
                "new_entity": entity,
                "check_in": check_in, "check_out": check_out,
                "prev_property": str(prev.get("property") or ""),
            }
            return (
                f"You already have a booking request for **{prev.get('property')}**. Would you like to cancel that "
                f"one, or keep it? Once you let me know, I'll set up your request for **{prop}**."
            ), True
        # First booking for this property -> create + a clean two-option reply with the link.
        await self._rb_create_booking(
            context=context, pd=pd, prop=prop, scope_key=scope_key, scope_ids=scope_ids,
            check_in=check_in, check_out=check_out,
            selected_phase_context=selected_phase_context,
        )
        return (self._rb_booking_link_reply(prop, check_in, check_out, context), True) if prop else (response_text, False)

    async def _create_silent_room_booking_ticket_bg(
        self, *, context: ConversationContext, payload: dict[str, Any], prop: str, pd: dict | None
    ) -> None:
        """Background create for the silent room-booking ticket — keeps the slow
        external push (and its 503 retries) off the guest's reply path. The ticket is
        persisted to the DB / KePSLA page even when the external API push fails."""
        import logging as _lg
        try:
            phase_value = str(payload.get("phase") or "").strip().lower()
            if phase_value == "pre booking":
                # Belt-and-suspenders guard for the website-booking flow: even if
                # older session context still carries a real guest id from a prior
                # stay lookup, a pre-booking lead ticket must not push it to the
                # external dashboard identity path.
                payload["guest_id"] = ""

            try:
                from services.customer_details_service import customer_details_service

                integration = {}
                if isinstance(pd, dict):
                    raw_integration = pd.get("_integration")
                    if isinstance(raw_integration, dict):
                        integration = raw_integration
                widget_id, group_id = customer_details_service.resolve_widget_group(
                    str(getattr(context, "hotel_code", "") or "").strip()
                )
                if widget_id:
                    conversation_id = (
                        str((pd or {}).get("_active_conversation_uid") or "").strip()
                        or str(getattr(context, "conversation_uid", "") or "").strip()
                        or str(integration.get("conversation_id") or "").strip()
                        or str(getattr(context, "session_id", "") or "").strip()
                    )
                    customer_details_service.write_bg(
                        session_id=str(getattr(context, "session_id", "") or "").strip(),
                        conversation_id=conversation_id,
                        widget_id=widget_id,
                        group_id=group_id,
                        guest_name=str(
                            payload.get("guest_name")
                            or getattr(context, "guest_name", "")
                            or ""
                        ).strip(),
                        email=str(
                            (pd or {}).get("email")
                            or integration.get("guest_email")
                            or ""
                        ).strip(),
                        phone=str(
                            getattr(context, "guest_phone", "")
                            or (pd or {}).get("phone")
                            or integration.get("guest_phone")
                            or ""
                        ).strip(),
                        property_name=str(payload.get("property_name") or prop or "").strip(),
                        topic=str(payload.get("issue") or "Room booking request").strip(),
                        form_type=str(
                            getattr(settings, "customer_details_form_type", "personal_information")
                            or "personal_information"
                        ),
                        processing_status=str(
                            getattr(settings, "customer_details_processing_status", "PROCESSED")
                            or "PROCESSED"
                        ),
                    )
            except Exception as _identity_exc:
                _lg.getLogger(__name__).warning(
                    "Silent room-booking identity mirror skipped: %s", _identity_exc
                )

            result = await ticketing_service.create_ticket(payload)
            # Dedup is already recorded inline (pending_data['_room_booking_ticket_props'])
            # before this background task runs, so nothing to persist here.
            _lg.getLogger(__name__).info(
                "Silent room-booking ticket: success=%s id=%s entity=%s err=%r property=%r session=%s",
                getattr(result, "success", None), getattr(result, "ticket_id", None),
                payload.get("organisation_id"), getattr(result, "error", None),
                prop, getattr(context, "session_id", "?"),
            )
        except Exception as _e:
            _lg.getLogger(__name__).warning("Silent room-booking ticket failed: %s", _e)

    async def _maybe_handle_full_kb_ticketing_phase_gate(
        self,
        *,
        request: ChatRequest,
        context: ConversationContext,
        capabilities_summary: dict[str, Any],
        routing_decision: Any,
        llm_result: Any,
        effective_intent: IntentType,
        effective_confidence: float,
        pending_data: dict[str, Any] | None = None,
        entities: dict[str, Any] | None = None,
        db_session=None,
    ) -> ChatResponse | None:
        current_pending = pending_data if isinstance(pending_data, dict) else {}
        effective_user_message = str(request.message or "").strip()
        phase_gate_message = effective_user_message
        if context.messages:
            latest_msg = context.messages[-1]
            if getattr(latest_msg, "role", None) == MessageRole.USER:
                latest_text = str(getattr(latest_msg, "content", "") or "").strip()
                if latest_text:
                    phase_gate_message = latest_text
        message_lower = str(phase_gate_message or "").strip().lower()
        # Avoid over-gating informational/profile-style FAQ turns in full-KB mode.
        profile_memory_query = bool(
            re.search(
                r"\b(remember|call me|my room(?:\s*number)?\s*is|what room did i|which room did i|what is my room)\b",
                message_lower,
            )
        )
        has_operational_ticket_signal = (
            self._looks_like_operational_issue_for_ticketing(message_lower)
            or self._looks_like_strong_ticket_issue_marker(message_lower)
        )
        has_pending_ticket_draft = isinstance(
            current_pending.get(_INTERNAL_PENDING_TICKET_DRAFT_KEY),
            dict,
        )
        should_run_phase_gate = True
        if has_pending_ticket_draft:
            should_run_phase_gate = False
        elif str(context.pending_action or "").strip().lower() == "collect_ticket_room_number":
            should_run_phase_gate = False
        elif profile_memory_query:
            should_run_phase_gate = False
        elif effective_intent == IntentType.FAQ and "?" in message_lower:
            faq_action_like = (
                self._is_action_request_text(phase_gate_message)
                or has_operational_ticket_signal
                or self._looks_like_booking_change_request(phase_gate_message)
            )
            should_run_phase_gate = faq_action_like
        elif effective_intent == IntentType.HUMAN_REQUEST and not self._is_action_request_text(
            phase_gate_message
        ):
            should_run_phase_gate = False
        if not should_run_phase_gate:
            return None

        entity_map = entities if isinstance(entities, dict) else self._extract_full_kb_entities_for_handler(llm_result)
        phase_gate_intent = effective_intent
        if has_operational_ticket_signal and effective_intent in {
            IntentType.FAQ,
            IntentType.MENU_REQUEST,
            IntentType.HUMAN_REQUEST,
            IntentType.COMPLAINT,
        }:
            phase_gate_intent = IntentType.COMPLAINT
        contextual_sanity_issue = self._detect_contextual_sanity_issue(
            message=phase_gate_message,
            intent=phase_gate_intent,
            context=context,
            pending_data=current_pending,
            entities=entity_map,
        )
        gate_payload: dict[str, Any] | None = None
        gate_source = "full_kb_ticketing_phase_gate"
        if contextual_sanity_issue is not None:
            gate_payload = contextual_sanity_issue
            gate_source = "full_kb_contextual_sanity_gate"

        # Complaint-like turns should primarily use contextual sanity checks;
        # avoid brittle catalog mismatch gating for generic "room" wording.
        if gate_payload is None and phase_gate_intent == IntentType.COMPLAINT:
            return None

        # For human-request turns, rely on phase-intent availability checks
        # instead of lexical mismatch gating to avoid false out-of-phase blocks.
        if gate_payload is None:
            use_mismatch_gate = phase_gate_intent not in {IntentType.HUMAN_REQUEST, IntentType.COMPLAINT}
            phase_gate = None
            if use_mismatch_gate:
                phase_gate = self._detect_ticketing_phase_service_mismatch(
                    message=phase_gate_message,
                    context=context,
                    pending_data=current_pending,
                    entities=entity_map,
                )
            if phase_gate is None:
                phase_gate = self._detect_phase_service_unavailable_for_intent(
                    message=phase_gate_message,
                    intent=phase_gate_intent,
                    context=context,
                    pending_data=current_pending,
                    entities=entity_map,
                )
            gate_payload = phase_gate

        if gate_payload is None:
            return None

        response_text = await self._compose_policy_guardrail_response(
            user_message=phase_gate_message,
            context=context,
            capabilities_summary=capabilities_summary,
            intent=effective_intent,
            gate_payload=gate_payload,
            fallback_text="",
            response_source=gate_source,
        )
        if not response_text:
            return None
        if effective_intent == IntentType.HUMAN_REQUEST:
            handoff_line = "If you'd like, I can connect you with our staff for further help."
            if handoff_line.lower() not in response_text.lower():
                response_text = f"{response_text} {handoff_line}".strip()
        identity_match = self._match_identity_response(
            effective_user_message,
            capabilities_summary,
            context,
        )
        if identity_match is not None:
            identity_text = str(identity_match.get("response_text") or "").strip()
            if identity_text and identity_text.lower() not in response_text.lower():
                response_text = f"{identity_text} {response_text}".strip()
        response_text = self._dedupe_response_sentences(response_text) or str(response_text or "").strip()

        if context.pending_action:
            internal_pending_entries = conversation_memory_service.internal_pending_entries(context.pending_data)
            context.pending_action = None
            context.pending_data = conversation_memory_service.retain_safe_internal_entries(
                context.pending_data,
            )
            context.pending_data.pop("_clarification_attempts", None)

        context.state = ConversationState.IDLE
        llm_trace_id = str(getattr(llm_result, "trace_id", "") or "")
        llm_status = str(getattr(llm_result, "status", "") or "")
        llm_normalized_query = str(getattr(llm_result, "normalized_query", "") or "")
        llm_raw_intent = str(getattr(llm_result, "raw_intent", "") or "")
        assistant_metadata = {
            "intent": effective_intent.value,
            "confidence": max(0.0, min(1.0, float(effective_confidence or 0.0))),
            "channel": context.channel,
            "kb_only_mode": True,
            "full_kb_llm_mode": True,
            "full_kb_llm_passthrough_mode": bool(getattr(settings, "full_kb_llm_passthrough_mode", False)),
            "full_kb_trace_id": llm_trace_id,
            "response_source": gate_source,
            "full_kb_ticketing_handler": True,
            "full_kb_ticketing_phase_gate": gate_source == "full_kb_ticketing_phase_gate",
            "contextual_sanity_gate": gate_source == "full_kb_contextual_sanity_gate",
            "contextual_sanity_code": str(gate_payload.get("code") or ""),
            "phase_gate_service_id": str(gate_payload.get("service_id") or ""),
            "phase_gate_service_name": str(gate_payload.get("service_name") or ""),
            "phase_gate_current_phase_id": str(gate_payload.get("current_phase_id") or ""),
            "phase_gate_current_phase_name": str(gate_payload.get("current_phase_name") or ""),
            "phase_gate_service_phase_id": str(gate_payload.get("service_phase_id") or ""),
            "phase_gate_service_phase_name": str(gate_payload.get("service_phase_name") or ""),
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

        gate_actions = gate_payload.get("suggested_actions")
        if isinstance(gate_actions, list):
            phase_suggestions = [str(item).strip() for item in gate_actions if str(item).strip()]
        else:
            phase_suggestions = ["Show available services", "Ask another question"]
        suggested_actions = self._finalize_user_query_suggestions(
            phase_suggestions,
            state=context.state,
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
                "normalized_query": llm_normalized_query,
                "raw_intent": llm_raw_intent,
                "pending_action": context.pending_action,
            },
            "classified_intent": effective_intent.value,
            "classified_confidence": max(0.0, min(1.0, float(effective_confidence or 0.0))),
            "routing_path": routing_path,
            "routing_score": routing_score,
            "routing_signals": routing_signals,
            "response_source": gate_source,
            "kb_only_mode": True,
            "full_kb_llm_mode": True,
            "full_kb_llm_passthrough_mode": bool(getattr(settings, "full_kb_llm_passthrough_mode", False)),
            "full_kb_trace_id": llm_trace_id,
            "full_kb_status": llm_status,
            "full_kb_ticketing_handler": True,
            "full_kb_ticketing_phase_gate": gate_source == "full_kb_ticketing_phase_gate",
            "contextual_sanity_gate": gate_source == "full_kb_contextual_sanity_gate",
            "contextual_sanity_code": str(gate_payload.get("code") or ""),
            "phase_gate_service_id": str(gate_payload.get("service_id") or ""),
            "phase_gate_service_name": str(gate_payload.get("service_name") or ""),
            "phase_gate_current_phase_id": str(gate_payload.get("current_phase_id") or ""),
            "phase_gate_current_phase_name": str(gate_payload.get("current_phase_name") or ""),
            "phase_gate_service_phase_id": str(gate_payload.get("service_phase_id") or ""),
            "phase_gate_service_phase_name": str(gate_payload.get("service_phase_name") or ""),
            "phase_service_unavailable": bool(gate_payload.get("phase_service_unavailable")),
            "memory_summary": memory_snapshot.get("summary", ""),
            "memory_facts": memory_snapshot.get("facts", {}),
            "memory_recent_changes": memory_snapshot.get("recent_changes", []),
        }
        return ChatResponse(
            session_id=request.session_id,
            message=response_text,
            intent=effective_intent,
            confidence=max(0.0, min(1.0, float(effective_confidence or 0.0))),
            state=context.state,
            suggested_actions=suggested_actions,
            metadata=metadata,
        )

    async def _maybe_route_full_kb_ticketing_handler(
        self,
        *,
        request: ChatRequest,
        context: ConversationContext,
        capabilities_summary: dict[str, Any],
        routing_decision: Any,
        llm_result: Any,
        effective_intent: IntentType,
        effective_confidence: float,
        current_pending_action: str | None,
        pending_action_target: str | None,
        rewrite_applied: bool,
        more_options_rewrite_applied: bool,
        db_session=None,
    ) -> ChatResponse | None:
        takeover_enabled = self._is_ticketing_plugin_takeover_enabled()
        llm_ticketing_preference = self._extract_full_kb_ticketing_preference(llm_result)
        llm_ticketing_reason = self._extract_full_kb_ticketing_reason(llm_result)
        entities = self._extract_full_kb_entities_for_handler(llm_result)
        current_pending = context.pending_data if isinstance(context.pending_data, dict) else {}
        effective_user_message = str(request.message or "").strip()
        fallback_routed_intent, fallback_reason, fallback_source = self._resolve_unified_complaint_routing_intent(
            message=effective_user_message,
            effective_intent=effective_intent,
            current_pending_action=current_pending_action,
            pending_action_target=pending_action_target,
            llm_response_text=str(getattr(llm_result, "response_text", "") or ""),
            llm_ticketing_preference=llm_ticketing_preference,
            next_state=getattr(llm_result, "next_state", ConversationState.IDLE),
        )
        if not takeover_enabled and fallback_routed_intent is None:
            return None

        if takeover_enabled:
            phase_gate_response = await self._maybe_handle_full_kb_ticketing_phase_gate(
                request=request,
                context=context,
                capabilities_summary=capabilities_summary,
                routing_decision=routing_decision,
                llm_result=llm_result,
                effective_intent=effective_intent,
                effective_confidence=effective_confidence,
                pending_data=current_pending,
                entities=entities,
                db_session=db_session,
            )
            if phase_gate_response is not None:
                return phase_gate_response

        phase_context = self._get_selected_phase_context(
            context=context,
            pending_data=current_pending,
            entities=entities,
        )
        ticketing_agent_decision = await ticketing_agent_service.decide_async(
            intent=effective_intent,
            message=effective_user_message,
            llm_response_text=getattr(llm_result, "response_text", ""),
            llm_ticketing_preference=llm_ticketing_preference,
            current_pending_action=current_pending_action,
            pending_action_target=pending_action_target,
            selected_phase_id=phase_context.get("selected_phase_id", ""),
            selected_phase_name=phase_context.get("selected_phase_name", ""),
            conversation_excerpt=self._build_ticketing_case_context_text(
                context=context,
                latest_user_message=effective_user_message,
                llm_response_text=getattr(llm_result, "response_text", ""),
            ),
        )
        decision_activate = bool(getattr(ticketing_agent_decision, "activate", False))
        decision_reason = str(getattr(ticketing_agent_decision, "reason", "") or "").strip()
        decision_source = str(getattr(ticketing_agent_decision, "source", "") or "").strip()
        decision_route = str(getattr(ticketing_agent_decision, "route", "") or "")
        decision_matched_case = str(getattr(ticketing_agent_decision, "matched_case", "") or "")
        routed_intent = getattr(ticketing_agent_decision, "routed_intent", None) or effective_intent

        if not decision_activate and fallback_routed_intent is not None:
            decision_activate = True
            routed_intent = fallback_routed_intent
            decision_reason = fallback_reason or decision_reason
            decision_source = fallback_source or decision_source
            if not decision_route:
                decision_route = "complaint_handler"
        elif fallback_routed_intent is not None and routed_intent not in {
            IntentType.COMPLAINT,
            IntentType.HUMAN_REQUEST,
        }:
            routed_intent = fallback_routed_intent
            decision_reason = fallback_reason or decision_reason
            decision_source = fallback_source or decision_source

        if not decision_activate:
            return None

        if not takeover_enabled and routed_intent != IntentType.COMPLAINT:
            return None

        if routed_intent == IntentType.COMPLAINT:
            entities.setdefault("ticketing_flow", True)
            entities.setdefault("ticket_route_reason", decision_reason or fallback_reason)
            entities.setdefault("ticket_route_source", decision_source or fallback_source)
        if llm_ticketing_reason:
            entities.setdefault("ticket_reason", llm_ticketing_reason)
        elif decision_reason:
            entities.setdefault("ticket_reason", decision_reason)
        intent_result = IntentResult(
            intent=routed_intent,
            confidence=max(0.0, min(1.0, float(effective_confidence or 0.0))),
            entities=entities,
            requires_confirmation=routed_intent in {IntentType.ORDER_FOOD, IntentType.TABLE_BOOKING},
        )
        handler_result = await self._dispatch_to_handler(
            effective_user_message,
            intent_result,
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
        if llm_result.room_number:
            context.room_number = str(llm_result.room_number)
        context.state = next_state

        assistant_metadata = {
            "intent": intent_result.intent.value,
            "confidence": intent_result.confidence,
            "channel": context.channel,
            "kb_only_mode": True,
            "full_kb_llm_mode": True,
            "full_kb_llm_passthrough_mode": bool(getattr(settings, "full_kb_llm_passthrough_mode", False)),
            "full_kb_trace_id": llm_result.trace_id,
            "full_kb_raw_intent": llm_result.raw_intent,
            "full_kb_normalized_query": llm_result.normalized_query,
            "response_source": "full_kb_llm_handler",
            "full_kb_ticketing_handler": True,
            "full_kb_ticketing_requested": llm_ticketing_preference,
            "full_kb_ticketing_reason": llm_ticketing_reason,
            "full_kb_ticketing_agent_route": decision_route,
            "full_kb_ticketing_agent_decision_reason": decision_reason,
            "full_kb_ticketing_agent_decision_source": decision_source,
            "full_kb_ticketing_agent_matched_case": decision_matched_case,
            "affirmative_selection_rewrite_applied": rewrite_applied,
            "more_options_rewrite_applied": more_options_rewrite_applied,
        }
        if handler_result.metadata:
            assistant_metadata.update(handler_result.metadata)
        context.add_message(
            MessageRole.ASSISTANT,
            handler_result.response_text,
            metadata=assistant_metadata,
        )
        conversation_memory_service.capture_assistant_message(
            context,
            handler_result.response_text,
            metadata=assistant_metadata,
        )
        await self._refresh_summary_async(context)
        await context_manager.save_context(context, db_session=db_session)
        memory_snapshot = conversation_memory_service.get_snapshot(context)

        if handler_result.suggested_actions:
            suggested_actions = handler_result.suggested_actions
        else:
            suggested_actions = self._build_contextual_suggested_actions(
                next_state,
                intent_result.intent,
                context.pending_action,
                capabilities_summary,
                context.pending_data,
            )
        suggested_actions = self._finalize_user_query_suggestions(
            suggested_actions,
            state=next_state,
            intent=intent_result.intent,
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
                "normalized_query": llm_result.normalized_query,
                "raw_intent": llm_result.raw_intent,
                "pending_action": context.pending_action,
            },
            "classified_intent": intent_result.intent.value,
            "classified_confidence": intent_result.confidence,
            "routing_path": routing_path,
            "routing_score": routing_score,
            "routing_signals": routing_signals,
            "response_source": "full_kb_llm_handler",
            "kb_only_mode": True,
            "full_kb_llm_mode": True,
            "full_kb_llm_passthrough_mode": bool(getattr(settings, "full_kb_llm_passthrough_mode", False)),
            "full_kb_trace_id": llm_result.trace_id,
            "full_kb_status": llm_result.status,
            "full_kb_ticketing_handler": True,
            "full_kb_ticketing_requested": llm_ticketing_preference,
            "full_kb_ticketing_reason": llm_ticketing_reason,
            "full_kb_ticketing_agent_route": decision_route,
            "full_kb_ticketing_agent_decision_reason": decision_reason,
            "full_kb_ticketing_agent_decision_source": decision_source,
            "full_kb_ticketing_agent_matched_case": decision_matched_case,
            "memory_summary": memory_snapshot.get("summary", ""),
            "memory_facts": memory_snapshot.get("facts", {}),
            "memory_recent_changes": memory_snapshot.get("recent_changes", []),
            "routed_handler": intent_result.intent.value,
        }
        if handler_result.metadata:
            metadata.update(handler_result.metadata)

        return ChatResponse(
            session_id=request.session_id,
            message=handler_result.response_text,
            intent=intent_result.intent,
            confidence=intent_result.confidence,
            state=next_state,
            suggested_actions=suggested_actions,
            metadata=metadata,
        )

    @staticmethod
    def _is_ticketing_pending_action(action: str | None) -> bool:
        normalized = str(action or "").strip().lower()
        return normalized in _TICKETING_PENDING_ACTIONS

    @staticmethod
    def _looks_like_strong_ticket_issue_marker(msg_lower: str) -> bool:
        text = str(msg_lower or "").strip().lower()
        if not text:
            return False
        markers = (
            "complaint",
            "issue",
            "problem",
            "not working",
            "broken",
            "dirty",
            "cockroach",
            "roach",
            "refund",
            "billing issue",
            "wrong charge",
            "human agent",
            "talk to human",
            "escalate",
            "manager",
        )
        return any(marker in text for marker in markers)

    @staticmethod
    def _ticket_log_text(value: Any, *, max_chars: int = 220) -> str:
        text = str(value or "").strip()
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + "..."

    def _log_full_kb_ticket_event(
        self,
        *,
        event: str,
        ticket_source: str,
        context: ConversationContext,
        effective_intent: IntentType | None = None,
        next_state: ConversationState | None = None,
        pending_action: str | None = None,
        reason: str = "",
        matched_case: str = "",
        sub_category: str = "",
        ticket_id: str = "",
        error: str = "",
        issue: str = "",
    ) -> None:
        level = logging.INFO
        if event in {"failed"}:
            level = logging.WARNING
        if event in {"not_applicable"}:
            level = logging.DEBUG
        logger.log(
            level,
            (
                "full_kb_ticket_event event=%s source=%s session_id=%s intent=%s state=%s "
                "pending_action=%s reason=%s matched_case=%s sub_category=%s ticket_id=%s issue=%s error=%s"
            ),
            event,
            ticket_source,
            str(getattr(context, "session_id", "") or ""),
            str(getattr(effective_intent, "value", effective_intent or "") or ""),
            str(getattr(next_state, "value", next_state or "") or ""),
            str(pending_action or ""),
            self._ticket_log_text(reason),
            self._ticket_log_text(matched_case),
            self._ticket_log_text(sub_category),
            self._ticket_log_text(ticket_id),
            self._ticket_log_text(issue),
            self._ticket_log_text(error),
        )

    async def _maybe_create_full_kb_transaction_ticket(
        self,
        *,
        context: ConversationContext,
        capabilities_summary: dict[str, Any],
        current_pending_action: str | None,
        effective_intent: IntentType,
        next_state: ConversationState,
        pending_data_snapshot: dict[str, Any] | None,
        response_text: str,
    ) -> dict[str, Any]:
        """
        Create Lumira-style backend ticket for confirmed transactional flows in
        full-KB mode, without changing assistant response text.
        """
        ticket_source = "full_kb_llm_transaction"

        if not self._is_ticketing_plugin_enabled():
            self._log_full_kb_ticket_event(
                event="not_applicable",
                ticket_source=ticket_source,
                context=context,
                effective_intent=effective_intent,
                next_state=next_state,
                pending_action=current_pending_action,
                reason="ticketing_plugin_disabled",
            )
            return {}
        pending_action = str(current_pending_action or "").strip().lower()
        if effective_intent != IntentType.CONFIRMATION_YES:
            self._log_full_kb_ticket_event(
                event="not_applicable",
                ticket_source=ticket_source,
                context=context,
                effective_intent=effective_intent,
                next_state=next_state,
                pending_action=pending_action,
                reason="intent_not_confirmation_yes",
            )
            return {}
        if next_state not in {ConversationState.COMPLETED, ConversationState.IDLE}:
            self._log_full_kb_ticket_event(
                event="not_applicable",
                ticket_source=ticket_source,
                context=context,
                effective_intent=effective_intent,
                next_state=next_state,
                pending_action=pending_action,
                reason="state_not_finalized",
            )
            return {}
        if not ticketing_service.is_ticketing_enabled(capabilities_summary):
            self._log_full_kb_ticket_event(
                event="not_applicable",
                ticket_source=ticket_source,
                context=context,
                effective_intent=effective_intent,
                next_state=next_state,
                pending_action=pending_action,
                reason="ticketing_service_disabled",
            )
            return {}

        pending = pending_data_snapshot if isinstance(pending_data_snapshot, dict) else {}
        if str(pending.get("ticket_id") or "").strip():
            # Avoid duplicating when some upstream path already created one.
            self._log_full_kb_ticket_event(
                event="not_applicable",
                ticket_source=ticket_source,
                context=context,
                effective_intent=effective_intent,
                next_state=next_state,
                pending_action=pending_action,
                reason="ticket_already_present_in_pending_data",
            )
            return {}

        is_order_confirmation = pending_action == "confirm_order"
        is_booking_confirmation = pending_action in {
            "confirm_booking",
            "confirm_room_booking",
            "confirm_room_availability_check",
        } or self._looks_like_booking_confirmation_response(response_text)

        if not is_order_confirmation and not is_booking_confirmation:
            self._log_full_kb_ticket_event(
                event="not_applicable",
                ticket_source=ticket_source,
                context=context,
                effective_intent=effective_intent,
                next_state=next_state,
                pending_action=pending_action,
                reason="pending_action_not_transaction_confirmation",
            )
            return {}

        resolved_phase = self._resolve_current_ticketing_phase(
            context=context,
            pending_data=pending,
            entities={},
        )
        if is_order_confirmation:
            issue = self._build_order_ticket_issue_from_pending(pending, response_text=response_text)
            phase = resolved_phase or "during_stay"
            sub_category = "order_food"
        else:
            pre_sub_category = self._infer_transaction_booking_sub_category(
                pending_data=pending,
                response_text=response_text,
            )
            issue = self._build_booking_ticket_issue_from_pending(
                pending,
                response_text=response_text,
                sub_category_override=pre_sub_category,
            )
            phase = resolved_phase or "pre_checkin"
            sub_category = pre_sub_category

        if not issue:
            self._log_full_kb_ticket_event(
                event="not_applicable",
                ticket_source=ticket_source,
                context=context,
                effective_intent=effective_intent,
                next_state=next_state,
                pending_action=pending_action,
                reason="empty_transaction_issue",
            )
            return {}

        skip_phase_gate_for_room_booking_confirmation = (
            is_booking_confirmation
            and str(sub_category or "").strip().lower() == "room_booking"
        )
        phase_gate = None
        if not skip_phase_gate_for_room_booking_confirmation:
            phase_gate = self._detect_ticketing_phase_service_mismatch(
                message=issue,
                context=context,
                pending_data=pending,
                entities={},
            )
            if phase_gate is None:
                phase_gate = self._detect_phase_service_unavailable_for_intent(
                    message=issue,
                    intent=effective_intent,
                    context=context,
                    pending_data=pending,
                    entities={},
                )
        if phase_gate is not None:
            skip_reason = (
                "phase_service_unavailable"
                if bool(phase_gate.get("phase_service_unavailable"))
                else "phase_service_mismatch"
            )
            self._log_full_kb_ticket_event(
                event="skipped",
                ticket_source=ticket_source,
                context=context,
                effective_intent=effective_intent,
                next_state=next_state,
                pending_action=pending_action,
                reason=skip_reason,
                sub_category=sub_category,
                issue=issue,
            )
            return {
                "ticket_create_skipped": True,
                "ticket_create_skip_reason": skip_reason,
                "ticket_source": ticket_source,
                "full_kb_ticketing_phase_gate": True,
                "phase_gate_response_text": str(phase_gate.get("response_text") or ""),
                "phase_gate_service_id": str(phase_gate.get("service_id") or ""),
                "phase_gate_service_name": str(phase_gate.get("service_name") or ""),
                "phase_gate_current_phase_id": str(phase_gate.get("current_phase_id") or ""),
                "phase_gate_current_phase_name": str(phase_gate.get("current_phase_name") or ""),
                "phase_gate_service_phase_id": str(phase_gate.get("service_phase_id") or ""),
                "phase_gate_service_phase_name": str(phase_gate.get("service_phase_name") or ""),
                "phase_service_unavailable": bool(phase_gate.get("phase_service_unavailable")),
            }

        ticketing_toggle_gate = self._detect_ticketing_phase_service_ticketing_disabled(
            message=issue,
            context=context,
            pending_data=pending,
            entities={},
        )
        if ticketing_toggle_gate is not None:
            self._log_full_kb_ticket_event(
                event="skipped",
                ticket_source=ticket_source,
                context=context,
                effective_intent=effective_intent,
                next_state=next_state,
                pending_action=pending_action,
                reason="phase_service_ticketing_disabled",
                sub_category=sub_category,
                issue=issue,
            )
            return {
                "ticket_create_skipped": True,
                "ticket_create_skip_reason": "phase_service_ticketing_disabled",
                "ticket_source": ticket_source,
                "full_kb_ticketing_phase_gate": True,
                "phase_gate_response_text": str(ticketing_toggle_gate.get("response_text") or ""),
                "phase_gate_service_id": str(ticketing_toggle_gate.get("service_id") or ""),
                "phase_gate_service_name": str(ticketing_toggle_gate.get("service_name") or ""),
                "phase_gate_current_phase_id": str(ticketing_toggle_gate.get("current_phase_id") or ""),
                "phase_gate_current_phase_name": str(ticketing_toggle_gate.get("current_phase_name") or ""),
                "phase_gate_service_phase_id": str(ticketing_toggle_gate.get("service_phase_id") or ""),
                "phase_gate_service_phase_name": str(ticketing_toggle_gate.get("service_phase_name") or ""),
            }

        configured_cases = ticketing_agent_service.get_configured_cases()
        matched_case = ""
        if configured_cases:
            phase_context = self._get_selected_phase_context(
                context=context,
                pending_data=pending,
                entities={},
            )
            matched_case = await ticketing_agent_service.match_configured_case_async(
                message=issue,
                conversation_excerpt=self._build_ticketing_case_context_text(
                    context=context,
                    latest_user_message=issue,
                    llm_response_text=response_text,
                ),
                llm_response_text=response_text,
                selected_phase_id=phase_context.get("selected_phase_id", ""),
                selected_phase_name=phase_context.get("selected_phase_name", ""),
            )
        if not matched_case:
            matched_case = "generalized_unconfigured_transaction_fallback"
        if is_booking_confirmation:
            sub_category = self._infer_transaction_booking_sub_category(
                pending_data=pending,
                response_text=response_text,
                matched_case=matched_case,
            )
            if not sub_category:
                sub_category = "booking"
            issue = self._build_booking_ticket_issue_from_pending(
                pending,
                response_text=response_text,
                sub_category_override=sub_category,
            )
            if not issue:
                issue = str(response_text or "").strip() or "Booking confirmed."

        self._log_full_kb_ticket_event(
            event="attempted",
            ticket_source=ticket_source,
            context=context,
            effective_intent=effective_intent,
            next_state=next_state,
            pending_action=pending_action,
            matched_case=matched_case,
            sub_category=sub_category,
            issue=issue,
        )
        try:
            payload = ticketing_service.build_lumira_ticket_payload(
                context=context,
                issue=issue,
                message=issue,
                category="request",
                sub_category=sub_category,
                priority="medium",
                phase=phase,
                source=str(pending.get("ticket_source") or ""),
                group_id=pending.get("group_id"),
                message_id=pending.get("message_id"),
                input_tokens=pending.get("input_tokens"),
                output_tokens=pending.get("output_tokens"),
                total_tokens=pending.get("total_tokens"),
                cost=pending.get("cost"),
            )
            create_result = await ticketing_service.create_ticket(payload)
        except Exception as exc:
            self._log_full_kb_ticket_event(
                event="failed",
                ticket_source=ticket_source,
                context=context,
                effective_intent=effective_intent,
                next_state=next_state,
                pending_action=pending_action,
                matched_case=matched_case,
                sub_category=sub_category,
                issue=issue,
                error=str(exc),
            )
            return {
                "ticket_create_failed": True,
                "ticket_error": str(exc),
                "ticket_source": ticket_source,
            }

        if not create_result.success:
            self._log_full_kb_ticket_event(
                event="failed",
                ticket_source=ticket_source,
                context=context,
                effective_intent=effective_intent,
                next_state=next_state,
                pending_action=pending_action,
                matched_case=matched_case,
                sub_category=sub_category,
                issue=issue,
                error=str(create_result.error or "ticket_create_failed"),
            )
            return {
                "ticket_create_failed": True,
                "ticket_error": str(create_result.error or "ticket_create_failed"),
                "ticket_source": ticket_source,
                "ticket_api_status_code": create_result.status_code,
                "ticket_api_response": create_result.response,
            }

        created_ticket_id = str(create_result.ticket_id or "").strip()
        self._log_full_kb_ticket_event(
            event="created",
            ticket_source=ticket_source,
            context=context,
            effective_intent=effective_intent,
            next_state=next_state,
            pending_action=pending_action,
            matched_case=matched_case,
            sub_category=sub_category,
            ticket_id=created_ticket_id,
            issue=issue,
        )
        return {
            "ticket_created": True,
            "ticket_id": created_ticket_id,
            "ticket_status": "open",
            "ticket_category": "request",
            "ticket_sub_category": sub_category,
            "ticket_priority": "medium",
            "ticket_summary": issue[:180],
            "ticket_source": ticket_source,
            "ticket_api_status_code": create_result.status_code,
            "ticket_api_response": create_result.response,
        }

    @staticmethod
    def _is_ticketing_plugin_enabled() -> bool:
        return bool(getattr(settings, "ticketing_plugin_enabled", True))

    @staticmethod
    def _is_ticketing_plugin_takeover_enabled() -> bool:
        return (
            bool(getattr(settings, "ticketing_plugin_enabled", True))
            and bool(getattr(settings, "ticketing_plugin_takeover_mode", False))
        )

    async def _maybe_create_full_kb_plugin_ticket(
        self,
        *,
        request: ChatRequest,
        context: ConversationContext,
        capabilities_summary: dict[str, Any],
        llm_result: Any,
        effective_intent: IntentType,
        next_state: ConversationState,
        current_pending_action: str | None,
        pending_data_snapshot: dict[str, Any] | None,
        response_text: str,
    ) -> dict[str, Any]:
        """
        Plugin ticketing path: create backend ticket without altering user-facing text.
        """
        ticket_source = "full_kb_llm_plugin"
        if not self._is_ticketing_plugin_enabled():
            self._log_full_kb_ticket_event(
                event="not_applicable",
                ticket_source=ticket_source,
                context=context,
                effective_intent=effective_intent,
                next_state=next_state,
                pending_action=current_pending_action,
                reason="ticketing_plugin_disabled",
            )
            return {}
        if not ticketing_service.is_ticketing_enabled(capabilities_summary):
            self._log_full_kb_ticket_event(
                event="not_applicable",
                ticket_source=ticket_source,
                context=context,
                effective_intent=effective_intent,
                next_state=next_state,
                pending_action=current_pending_action,
                reason="ticketing_service_disabled",
            )
            return {}

        msg = str(request.message or "").strip()
        msg_lower = msg.lower()
        if not msg_lower:
            self._log_full_kb_ticket_event(
                event="not_applicable",
                ticket_source=ticket_source,
                context=context,
                effective_intent=effective_intent,
                next_state=next_state,
                pending_action=current_pending_action,
                reason="empty_user_message",
            )
            return {}
        pending = pending_data_snapshot if isinstance(pending_data_snapshot, dict) else {}
        pending_ticket_draft = self._get_pending_plugin_ticket_draft(
            pending_data_snapshot=pending,
            context=context,
        )
        room_number_only_reply = self._looks_like_room_number_reply(msg_lower)
        llm_ticketing_preference = self._extract_full_kb_ticketing_preference(llm_result)
        phase_context = self._get_selected_phase_context(
            context=context,
            pending_data=context.pending_data if isinstance(context.pending_data, dict) else {},
            entities={},
        )
        ticketing_agent_decision = await ticketing_agent_service.decide_async(
            intent=effective_intent,
            message=msg,
            llm_response_text=response_text,
            llm_ticketing_preference=llm_ticketing_preference,
            current_pending_action=current_pending_action,
            pending_action_target=str(getattr(llm_result, "pending_action", "") or ""),
            selected_phase_id=phase_context.get("selected_phase_id", ""),
            selected_phase_name=phase_context.get("selected_phase_name", ""),
            conversation_excerpt=self._build_ticketing_case_context_text(
                context=context,
                latest_user_message=msg,
                llm_response_text=response_text,
            ),
        )
        matched_case = str(getattr(ticketing_agent_decision, "matched_case", "") or "").strip()
        decision_activate = bool(getattr(ticketing_agent_decision, "activate", False))
        decision_routed_intent = getattr(ticketing_agent_decision, "routed_intent", None)
        decision_reason = str(getattr(ticketing_agent_decision, "reason", "") or "").strip()
        decision_source = str(getattr(ticketing_agent_decision, "source", "") or "").strip()

        if (
            effective_intent in {
            IntentType.GREETING,
            IntentType.CONFIRMATION_YES,
            IntentType.CONFIRMATION_NO,
            IntentType.UNCLEAR,
            }
            and not decision_activate
        ):
            self._log_full_kb_ticket_event(
                event="not_applicable",
                ticket_source=ticket_source,
                context=context,
                effective_intent=effective_intent,
                next_state=next_state,
                pending_action=current_pending_action,
                reason="intent_not_eligible_for_plugin_ticket",
                matched_case=matched_case,
            )
            return {}
        if effective_intent in {IntentType.ORDER_FOOD, IntentType.TABLE_BOOKING}:
            # For booking/order intents, create ticket only on explicit complaint/escalation
            # asks. Normal transactional flow must wait for confirmation path.
            transactional_escalation_signal = (
                self._looks_like_ticketing_request(msg_lower)
                or self._looks_like_strong_ticket_issue_marker(msg_lower)
            )
            if not transactional_escalation_signal and not pending_ticket_draft:
                self._log_full_kb_ticket_event(
                    event="not_applicable",
                    ticket_source=ticket_source,
                    context=context,
                    effective_intent=effective_intent,
                    next_state=next_state,
                    pending_action=current_pending_action,
                    reason="transactional_intent_deferred_to_confirmation_path",
                    matched_case=matched_case,
                )
                return {}
        if room_number_only_reply and not pending_ticket_draft:
            # Slot-detail only message; do not create a new ticket.
            self._log_full_kb_ticket_event(
                event="not_applicable",
                ticket_source=ticket_source,
                context=context,
                effective_intent=effective_intent,
                next_state=next_state,
                pending_action=current_pending_action,
                reason="room_number_only_reply",
            )
            return {}

        if str(pending.get("ticket_id") or "").strip():
            self._log_full_kb_ticket_event(
                event="not_applicable",
                ticket_source=ticket_source,
                context=context,
                effective_intent=effective_intent,
                next_state=next_state,
                pending_action=current_pending_action,
                reason="ticket_already_present_in_pending_data",
            )
            return {}
        if pending_ticket_draft and room_number_only_reply:
            decision_activate = True
            draft_routed_intent_raw = str(pending_ticket_draft.get("routed_intent") or "").strip().lower()
            if draft_routed_intent_raw == IntentType.HUMAN_REQUEST.value:
                decision_routed_intent = IntentType.HUMAN_REQUEST
            else:
                decision_routed_intent = IntentType.COMPLAINT
            draft_case = str(pending_ticket_draft.get("matched_case") or "").strip()
            if draft_case:
                matched_case = draft_case
            decision_reason = "resume_pending_ticket_draft"
            decision_source = "chat_service_pending_ticket_draft"
        # Rule-based fallback intent inference removed — LLM orchestrator owns all routing decisions.
        if not decision_activate:
            self._log_full_kb_ticket_event(
                event="not_applicable",
                ticket_source=ticket_source,
                context=context,
                effective_intent=effective_intent,
                next_state=next_state,
                pending_action=current_pending_action,
                reason=decision_reason or "ticketing_agent_not_activated",
                matched_case=matched_case,
            )
            return {}
        if decision_routed_intent not in {IntentType.COMPLAINT, IntentType.HUMAN_REQUEST}:
            self._log_full_kb_ticket_event(
                event="not_applicable",
                ticket_source=ticket_source,
                context=context,
                effective_intent=effective_intent,
                next_state=next_state,
                pending_action=current_pending_action,
                reason=f"unsupported_routed_intent:{decision_routed_intent}",
                matched_case=matched_case,
            )
            return {}

        entities = self._extract_full_kb_entities_for_handler(llm_result)
        if decision_routed_intent == IntentType.HUMAN_REQUEST:
            category = "request"
            sub_category = "human_handoff"
            priority = "high"
        else:
            category = self._derive_plugin_ticket_category(
                effective_intent=effective_intent,
                entities=entities,
                message=msg,
            )
            sub_category = self._infer_plugin_ticket_sub_category(
                effective_intent=effective_intent,
                entities=entities,
                message=msg,
                matched_case=matched_case,
            )
            priority = self._derive_plugin_ticket_priority(
                entities=entities,
                message=msg,
            )
        if pending_ticket_draft:
            category = str(pending_ticket_draft.get("category") or category).strip() or category
            sub_category = str(pending_ticket_draft.get("sub_category") or sub_category).strip() or sub_category
            priority = str(pending_ticket_draft.get("priority") or priority).strip() or priority
        resolved_phase = self._resolve_current_ticketing_phase(
            context=context,
            pending_data=pending,
            entities=entities,
        )
        phase = str(
            entities.get("phase")
            or pending.get("phase")
            or (pending_ticket_draft or {}).get("phase")
            or resolved_phase
            or "during_stay"
        ).strip() or "during_stay"

        issue = str(
            entities.get("issue")
            or pending.get("issue")
            or (pending_ticket_draft or {}).get("issue")
            or llm_result.normalized_query
            or msg
        ).strip()
        if not issue:
            issue = msg
        if not issue:
            self._log_full_kb_ticket_event(
                event="not_applicable",
                ticket_source=ticket_source,
                context=context,
                effective_intent=effective_intent,
                next_state=next_state,
                pending_action=current_pending_action,
                reason="empty_issue",
                matched_case=matched_case,
            )
            return {}

        bypass_service_phase_gates = decision_routed_intent in {IntentType.COMPLAINT, IntentType.HUMAN_REQUEST}
        if not bypass_service_phase_gates:
            phase_gate = self._detect_ticketing_phase_service_mismatch(
                message=msg or issue,
                context=context,
                pending_data=pending,
                entities=entities,
            )
            if phase_gate is None:
                phase_gate_intent = decision_routed_intent or effective_intent
                phase_gate = self._detect_phase_service_unavailable_for_intent(
                    message=msg or issue,
                    intent=phase_gate_intent,
                    context=context,
                    pending_data=pending,
                    entities=entities,
                )
            if phase_gate is not None:
                skip_reason = (
                    "phase_service_unavailable"
                    if bool(phase_gate.get("phase_service_unavailable"))
                    else "phase_service_mismatch"
                )
                self._log_full_kb_ticket_event(
                    event="skipped",
                    ticket_source=ticket_source,
                    context=context,
                    effective_intent=effective_intent,
                    next_state=next_state,
                    pending_action=current_pending_action,
                    reason=skip_reason,
                    matched_case=matched_case,
                    sub_category=sub_category,
                    issue=issue,
                )
                return {
                    "ticket_create_skipped": True,
                    "ticket_create_skip_reason": skip_reason,
                    "ticket_source": ticket_source,
                    "full_kb_ticketing_phase_gate": True,
                    "phase_gate_response_text": str(phase_gate.get("response_text") or ""),
                    "phase_gate_service_id": str(phase_gate.get("service_id") or ""),
                    "phase_gate_service_name": str(phase_gate.get("service_name") or ""),
                    "phase_gate_current_phase_id": str(phase_gate.get("current_phase_id") or ""),
                    "phase_gate_current_phase_name": str(phase_gate.get("current_phase_name") or ""),
                    "phase_gate_service_phase_id": str(phase_gate.get("service_phase_id") or ""),
                    "phase_gate_service_phase_name": str(phase_gate.get("service_phase_name") or ""),
                    "phase_service_unavailable": bool(phase_gate.get("phase_service_unavailable")),
                    "full_kb_ticketing_agent_decision_reason": decision_reason,
                    "full_kb_ticketing_agent_decision_source": decision_source,
                    "full_kb_ticketing_agent_matched_case": matched_case,
                }

            ticketing_toggle_gate = self._detect_ticketing_phase_service_ticketing_disabled(
                message=msg or issue,
                context=context,
                pending_data=pending,
                entities=entities,
            )
            if ticketing_toggle_gate is not None:
                self._log_full_kb_ticket_event(
                    event="skipped",
                    ticket_source=ticket_source,
                    context=context,
                    effective_intent=effective_intent,
                    next_state=next_state,
                    pending_action=current_pending_action,
                    reason="phase_service_ticketing_disabled",
                    matched_case=matched_case,
                    sub_category=sub_category,
                    issue=issue,
                )
                return {
                    "ticket_create_skipped": True,
                    "ticket_create_skip_reason": "phase_service_ticketing_disabled",
                    "ticket_source": ticket_source,
                    "full_kb_ticketing_phase_gate": True,
                    "phase_gate_response_text": str(ticketing_toggle_gate.get("response_text") or ""),
                    "phase_gate_service_id": str(ticketing_toggle_gate.get("service_id") or ""),
                    "phase_gate_service_name": str(ticketing_toggle_gate.get("service_name") or ""),
                    "phase_gate_current_phase_id": str(ticketing_toggle_gate.get("current_phase_id") or ""),
                    "phase_gate_current_phase_name": str(ticketing_toggle_gate.get("current_phase_name") or ""),
                    "phase_gate_service_phase_id": str(ticketing_toggle_gate.get("service_phase_id") or ""),
                    "phase_gate_service_phase_name": str(ticketing_toggle_gate.get("service_phase_name") or ""),
                    "full_kb_ticketing_agent_decision_reason": decision_reason,
                    "full_kb_ticketing_agent_decision_source": decision_source,
                    "full_kb_ticketing_agent_matched_case": matched_case,
                }

        room_number = (
            str(llm_result.room_number or "").strip()
            or str(context.room_number or "").strip()
            or str(pending.get("room_number") or "").strip()
            or str((pending_ticket_draft or {}).get("room_number") or "").strip()
            or self._extract_room_number_from_text(msg)
        )
        missing_required_fields = self._missing_plugin_ticket_details(
            phase=phase,
            room_number=room_number,
            issue=issue,
            context=context,
            pending=pending,
            entities=entities,
        )
        if missing_required_fields:
            pending_draft = self._build_pending_plugin_ticket_draft(
                context=context,
                issue=issue,
                category=category,
                sub_category=sub_category,
                priority=priority,
                phase=phase,
                matched_case=matched_case,
                routed_intent=decision_routed_intent,
                pending=pending,
                entities=entities,
                existing_draft=pending_ticket_draft,
            )
            self._log_full_kb_ticket_event(
                event="skipped",
                ticket_source=ticket_source,
                context=context,
                effective_intent=effective_intent,
                next_state=next_state,
                pending_action=current_pending_action,
                reason="missing_required_details",
                matched_case=matched_case,
                sub_category=sub_category,
                issue=issue,
            )
            return {
                "ticket_create_skipped": True,
                "ticket_create_skip_reason": "missing_required_details",
                "ticket_missing_fields": missing_required_fields,
                "pending_ticket_draft": pending_draft,
                "ticket_pending_action": "collect_ticket_room_number"
                if "room_number" in {str(field).strip().lower() for field in missing_required_fields}
                else "collect_ticket_issue_details",
                "ticket_source": ticket_source,
                "full_kb_ticketing_agent_decision_reason": decision_reason,
                "full_kb_ticketing_agent_decision_source": decision_source,
                "full_kb_ticketing_agent_matched_case": matched_case,
            }

        self._log_full_kb_ticket_event(
            event="attempted",
            ticket_source=ticket_source,
            context=context,
            effective_intent=effective_intent,
            next_state=next_state,
            pending_action=current_pending_action,
            matched_case=matched_case,
            sub_category=sub_category,
            issue=issue,
        )
        try:
            payload = ticketing_service.build_lumira_ticket_payload(
                context=context,
                issue=issue,
                message=issue,
                category=category,
                sub_category=sub_category,
                priority=priority,
                phase=phase,
                source=str(pending.get("ticket_source") or ""),
                group_id=pending.get("group_id"),
                message_id=pending.get("message_id"),
                input_tokens=pending.get("input_tokens"),
                output_tokens=pending.get("output_tokens"),
                total_tokens=pending.get("total_tokens"),
                cost=pending.get("cost"),
            )
            if room_number:
                payload["room_number"] = room_number
            create_result = await ticketing_service.create_ticket(payload)
        except Exception as exc:
            self._log_full_kb_ticket_event(
                event="failed",
                ticket_source=ticket_source,
                context=context,
                effective_intent=effective_intent,
                next_state=next_state,
                pending_action=current_pending_action,
                matched_case=matched_case,
                sub_category=sub_category,
                issue=issue,
                error=str(exc),
            )
            return {
                "ticket_create_failed": True,
                "ticket_error": str(exc),
                "ticket_source": ticket_source,
            }

        if not create_result.success:
            self._log_full_kb_ticket_event(
                event="failed",
                ticket_source=ticket_source,
                context=context,
                effective_intent=effective_intent,
                next_state=next_state,
                pending_action=current_pending_action,
                matched_case=matched_case,
                sub_category=sub_category,
                issue=issue,
                error=str(create_result.error or "ticket_create_failed"),
            )
            return {
                "ticket_create_failed": True,
                "ticket_error": str(create_result.error or "ticket_create_failed"),
                "ticket_source": ticket_source,
                "ticket_api_status_code": create_result.status_code,
                "ticket_api_response": create_result.response,
            }

        created_ticket_id = str(create_result.ticket_id or "").strip()
        self._log_full_kb_ticket_event(
            event="created",
            ticket_source=ticket_source,
            context=context,
            effective_intent=effective_intent,
            next_state=next_state,
            pending_action=current_pending_action,
            matched_case=matched_case,
            sub_category=sub_category,
            ticket_id=created_ticket_id,
            issue=issue,
        )
        return {
            "ticket_created": True,
            "ticket_id": created_ticket_id,
            "ticket_status": "open",
            "ticket_category": category,
            "ticket_sub_category": sub_category,
            "ticket_priority": priority,
            "ticket_summary": issue[:180],
            "ticket_source": ticket_source,
            "room_number": room_number,
            "ticket_resumed_from_pending_draft": bool(pending_ticket_draft and room_number_only_reply),
            "clear_pending_ticket_draft": True,
            "full_kb_ticketing_agent_decision_reason": decision_reason,
            "full_kb_ticketing_agent_decision_source": decision_source,
            "full_kb_ticketing_agent_matched_case": matched_case,
            "ticket_api_status_code": create_result.status_code,
            "ticket_api_response": create_result.response,
        }

    def _get_pending_plugin_ticket_draft(
        self,
        *,
        pending_data_snapshot: dict[str, Any] | None,
        context: ConversationContext,
    ) -> dict[str, Any] | None:
        snapshot = pending_data_snapshot if isinstance(pending_data_snapshot, dict) else {}
        context_pending = context.pending_data if isinstance(context.pending_data, dict) else {}
        draft_candidates = (
            snapshot.get(_INTERNAL_PENDING_TICKET_DRAFT_KEY),
            snapshot.get("pending_ticket_draft"),
            context_pending.get(_INTERNAL_PENDING_TICKET_DRAFT_KEY),
            context_pending.get("pending_ticket_draft"),
        )
        for candidate in draft_candidates:
            if isinstance(candidate, dict) and candidate:
                return dict(candidate)
        return None

    def _build_pending_plugin_ticket_draft(
        self,
        *,
        context: ConversationContext,
        issue: str,
        category: str,
        sub_category: str,
        priority: str,
        phase: str,
        matched_case: str,
        routed_intent: IntentType | None,
        pending: dict[str, Any],
        entities: dict[str, Any],
        existing_draft: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        draft = dict(existing_draft or {})
        draft.update(
            {
                "issue": str(issue or "").strip(),
                "category": str(category or "").strip().lower() or "request",
                "sub_category": str(sub_category or "").strip() or "general_request",
                "priority": str(priority or "").strip().lower() or "medium",
                "phase": str(phase or "").strip() or "during_stay",
                "matched_case": str(matched_case or draft.get("matched_case") or "").strip(),
                "routed_intent": (
                    routed_intent.value
                    if isinstance(routed_intent, IntentType)
                    else str(draft.get("routed_intent") or IntentType.COMPLAINT.value).strip().lower()
                ),
            }
        )
        integration = ticketing_service.get_integration_context(context)

        room_number = self._first_non_empty(
            pending.get("room_number"),
            entities.get("room_number"),
            draft.get("room_number"),
            context.room_number,
            integration.get("room_number"),
        )
        if room_number:
            draft["room_number"] = room_number

        guest_name = self._first_non_empty(
            pending.get("guest_name"),
            entities.get("guest_name"),
            draft.get("guest_name"),
            context.guest_name,
            integration.get("guest_name") if isinstance(integration, dict) else "",
        )
        if guest_name:
            draft["guest_name"] = guest_name

        guest_phone = self._first_non_empty(
            pending.get("guest_phone"),
            entities.get("guest_phone"),
            draft.get("guest_phone"),
            context.guest_phone,
            integration.get("guest_phone") if isinstance(integration, dict) else "",
            integration.get("wa_number") if isinstance(integration, dict) else "",
        )
        if guest_phone:
            draft["guest_phone"] = guest_phone

        return draft

    @staticmethod
    def _missing_plugin_ticket_details(
        *,
        phase: str,
        room_number: str,
        issue: str,
        context: ConversationContext,
        pending: dict[str, Any],
        entities: dict[str, Any],
    ) -> list[str]:
        missing: list[str] = []
        issue_text = str(issue or "").strip()
        if len(issue_text) < 5:
            missing.append("issue")

        phase_norm = str(phase or "").strip().lower().replace(" ", "_")
        integration = ticketing_service.get_integration_context(context)
        flow_norm = str(
            integration.get("flow")
            or integration.get("bot_mode")
            or ""
        ).strip().lower().replace("-", "_")
        source_norm = str(
            pending.get("ticket_source")
            or integration.get("ticket_source")
            or integration.get("source")
            or ""
        ).strip().lower().replace("-", "_")
        is_prebooking = (
            phase_norm in {"pre_booking", "booking", "pre_checkin"}
            or flow_norm in {"engage", "booking", "booking_bot", "pre_booking", "prebooking"}
            or source_norm in {"booking_bot", "engage", "booking"}
        )
        is_during_stay = (
            phase_norm in {"during_stay", "duringstay", "in_stay", "instay"}
            or flow_norm in {"during_stay", "duringstay", "in_stay", "instay", "service", "operations"}
            or source_norm in {"service", "during_stay", "in_stay", "operations"}
        )

        resolved_room = str(
            room_number
            or context.room_number
            or pending.get("room_number")
            or integration.get("room_number")
            or entities.get("room_number")
            or ""
        ).strip()

        resolved_phone = str(
            pending.get("guest_phone")
            or integration.get("guest_phone")
            or integration.get("wa_number")
            or context.guest_phone
            or entities.get("guest_phone")
            or ""
        ).strip()
        resolved_guest_id = str(
            pending.get("guest_id")
            or integration.get("guest_id")
            or entities.get("guest_id")
            or ""
        ).strip()
        resolved_name = str(
            pending.get("guest_name")
            or integration.get("guest_name")
            or context.guest_name
            or entities.get("guest_name")
            or ""
        ).strip()

        if is_prebooking:
            if not (resolved_phone or resolved_name or resolved_guest_id):
                missing.append("guest_identity")
            return missing

        if is_during_stay:
            if not resolved_room:
                missing.append("room_number")
            return missing

        if phase_norm == "post_checkout":
            if not (resolved_phone or resolved_name or resolved_room or resolved_guest_id):
                missing.append("guest_identity")
            return missing

        if not (resolved_room or (resolved_phone and resolved_name) or resolved_guest_id):
            missing.append("contact_or_room")
        return missing

    def _has_minimum_plugin_ticket_details(
        self,
        *,
        phase: str,
        room_number: str,
        issue: str,
        context: ConversationContext,
        pending: dict[str, Any],
        entities: dict[str, Any],
    ) -> bool:
        return not self._missing_plugin_ticket_details(
            phase=phase,
            room_number=room_number,
            issue=issue,
            context=context,
            pending=pending,
            entities=entities,
        )

    @staticmethod
    def _derive_plugin_ticket_category(
        *,
        effective_intent: IntentType,
        entities: dict[str, Any],
        message: str,
    ) -> str:
        explicit = str(entities.get("category") or entities.get("categorization") or "").strip().lower()
        if explicit:
            return explicit
        if effective_intent == IntentType.COMPLAINT:
            return "complaint"
        text = str(message or "").lower()
        if any(marker in text for marker in ("broken", "not working", "dirty", "cockroach", "roach", "problem")):
            return "complaint"
        return "request"

    @staticmethod
    def _derive_plugin_ticket_priority(
        *,
        entities: dict[str, Any],
        message: str,
    ) -> str:
        explicit = str(entities.get("priority") or "").strip().lower()
        if explicit in {"low", "medium", "high", "critical"}:
            return explicit
        text = str(message or "").lower()
        if any(
            marker in text
            for marker in ("fire", "smoke", "medical emergency", "unsafe", "security threat")
        ):
            return "critical"
        if any(
            marker in text
            for marker in ("urgent", "asap", "immediately", "critical", "emergency", "cockroach", "roach", "pest")
        ):
            return "high"
        if any(marker in text for marker in ("whenever", "not urgent", "later")):
            return "low"
        return "medium"

    @staticmethod
    def _infer_plugin_ticket_sub_category(
        *,
        effective_intent: IntentType,
        entities: dict[str, Any],
        message: str,
        matched_case: str = "",
    ) -> str:
        explicit = str(
            entities.get("sub_category")
            or entities.get("sub_categorization")
            or ""
        ).strip().lower()
        if explicit:
            return explicit

        text = str(message or "").lower()
        case_text = str(matched_case or "").strip().lower()
        if effective_intent == IntentType.ROOM_SERVICE:
            if any(marker in text for marker in ("towel", "blanket", "pillow", "amenities", "toiletries", "hairdryer")):
                return "amenities"
            if any(marker in text for marker in ("housekeeping", "cleaning", "clean room")):
                return "housekeeping"
            if any(marker in text for marker in ("laundry", "iron", "dry clean")):
                return "laundry"
            if any(marker in text for marker in ("ac", "air conditioner", "repair", "broken", "not working", "maintenance", "leak")):
                return "maintenance"
            return "room_service"

        combined = f"{text} {case_text}".strip()
        if any(marker in combined for marker in ("cockroach", "roach", "pest", "bed bug", "insect")):
            return "housekeeping"
        if any(marker in text for marker in ("taxi", "airport", "pickup", "drop", "transport", "shuttle", "chauffeur", "cab")):
            return "transport"
        if any(marker in text for marker in ("spa", "massage", "wellness", "therapy", "treatment")):
            return "spa_booking"
        if any(
            marker in text
            for marker in ("room booking", "book a room", "book room", "stay booking", "check in", "check-out", "check out", "suite")
        ):
            return "room_booking"
        if any(marker in text for marker in ("food", "order", "meal", "menu", "dining", "in-room dining")):
            return "order_food"
        if any(marker in text for marker in ("billing", "invoice", "refund", "wrong charge", "payment")):
            return "billing"
        if any(marker in text for marker in ("table", "reservation", "book table", "restaurant booking")):
            return "table_booking"
        if any(marker in case_text for marker in ("spa", "massage", "wellness", "therapy", "treatment")):
            return "spa_booking"
        if any(
            marker in case_text
            for marker in ("room booking", "book a room", "book room", "stay booking", "check in", "check-out", "check out", "suite")
        ):
            return "room_booking"
        if any(marker in case_text for marker in ("taxi", "airport", "pickup", "drop", "transport", "shuttle", "chauffeur", "cab")):
            return "transport"
        if any(marker in case_text for marker in ("food", "order", "meal", "menu", "dining", "in-room dining")):
            return "order_food"
        if any(marker in case_text for marker in ("billing", "invoice", "refund", "wrong charge", "payment")):
            return "billing"
        if any(marker in case_text for marker in ("table", "reservation", "book table", "restaurant booking")):
            return "table_booking"
        return "complaint"

    def _build_order_ticket_issue_from_pending(
        self,
        pending_data: dict[str, Any],
        *,
        response_text: str,
    ) -> str:
        item_name = self._extract_order_item_name(pending_data) or "food order"
        quantity = self._extract_order_quantity_value(pending_data)
        total_raw = self._first_non_empty(
            pending_data.get("order_total"),
            pending_data.get("total"),
        )
        extra = self._first_non_empty(
            pending_data.get("order_additional_request"),
            pending_data.get("special_request"),
        )
        summary = f"Food order confirmed: {(str(quantity) if quantity is not None else '1')} x {item_name}"
        if total_raw:
            summary += f", total Rs.{total_raw}"
        if extra:
            summary += f", notes: {extra}"
        if summary.strip():
            return summary.strip()
        return str(response_text or "").strip()

    def _build_booking_ticket_issue_from_pending(
        self,
        pending_data: dict[str, Any],
        *,
        response_text: str,
        sub_category_override: str = "",
    ) -> str:
        pending = pending_data if isinstance(pending_data, dict) else {}
        fallback = str(response_text or "").strip()
        sub_category = str(sub_category_override or "").strip().lower() or self._infer_transaction_booking_sub_category(
            pending_data=pending,
            response_text=response_text,
        )

        service_name = self._first_non_empty(
            pending.get("service_name"),
            pending.get("restaurant_name"),
            pending.get("booking_service"),
            pending.get("service"),
        )
        party_size = self._first_non_empty(
            pending.get("party_size"),
            pending.get("guests"),
            pending.get("guest_count"),
            pending.get("booking_party_size"),
        )
        booking_time = self._first_non_empty(
            pending.get("time"),
            pending.get("booking_time"),
        )
        booking_date = self._first_non_empty(
            pending.get("date"),
            pending.get("booking_date"),
            pending.get("stay_date_range"),
        )

        if sub_category == "room_booking":
            room_type = self._first_non_empty(
                pending.get("room_type"),
                pending.get("room_name"),
                service_name,
                "room stay",
            )
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
            parts = [f"Room booking confirmed: {room_type}"]
            if check_in and check_out:
                parts.append(f"from {check_in} to {check_out}")
            elif booking_date:
                parts.append(f"for {booking_date}")
            if party_size:
                parts.append(f"for {party_size} guests")
            summary = " ".join(parts).strip()
            if summary:
                if summary.lower() in {"room booking confirmed: room stay"} and fallback:
                    return fallback
                return summary

        if sub_category == "spa_booking":
            treatment = service_name or "spa treatment"
            parts = [f"Spa booking confirmed: {treatment}"]
            if booking_time:
                parts.append(f"at {booking_time}")
            if booking_date and booking_date.lower() not in {"today", "tonight"}:
                parts.append(f"on {booking_date}")
            if party_size:
                parts.append(f"for {party_size} guests")
            summary = " ".join(parts).strip()
            if summary:
                if summary.lower() in {"spa booking confirmed: spa treatment"} and fallback:
                    return fallback
                return summary

        if sub_category == "table_booking":
            service_label = service_name or "table booking"
            parts: list[str] = [f"Table booking confirmed: {service_label}"]
            if party_size:
                parts.append(f"for {party_size} guests")
            if booking_time:
                parts.append(f"at {booking_time}")
            if booking_date and booking_date.lower() not in {"today", "tonight"}:
                parts.append(f"on {booking_date}")
            summary = " ".join(parts).strip()
            if summary:
                if summary.lower() in {"table booking confirmed: table booking"} and fallback:
                    return fallback
                return summary

        if sub_category == "transport_booking":
            service_label = service_name or "transport booking"
            parts: list[str] = [f"Transport request confirmed: {service_label}"]
            if party_size:
                parts.append(f"for {party_size} guests")
            if booking_time:
                parts.append(f"at {booking_time}")
            if booking_date and booking_date.lower() not in {"today", "tonight"}:
                parts.append(f"on {booking_date}")
            summary = " ".join(parts).strip()
            if summary:
                return summary

        generic_label = service_name or "service booking"
        parts = [f"Booking confirmed: {generic_label}"]
        if party_size:
            parts.append(f"for {party_size} guests")
        if booking_time:
            parts.append(f"at {booking_time}")
        if booking_date and booking_date.lower() not in {"today", "tonight"}:
            parts.append(f"on {booking_date}")
        generic_summary = " ".join(parts).strip()
        if generic_summary:
            return generic_summary

        return fallback or "Booking confirmed."

    def _should_route_full_kb_ticketing_handler(
        self,
        *,
        effective_intent: IntentType,
        current_pending_action: str | None,
        pending_action_target: str | None,
        message: str,
        llm_response_text: str,
        llm_ticketing_preference: bool | None,
    ) -> bool:
        msg_lower = str(message or "").strip().lower()
        current_pending = str(current_pending_action or "").strip().lower()
        if current_pending.startswith("confirm_"):
            binary_reply = self._detect_simple_binary_reply(message)
            explicit_complaint_evidence = (
                self._looks_like_ticketing_request(msg_lower)
                or self._looks_like_strong_ticket_issue_marker(msg_lower)
                or self._looks_like_operational_issue_for_ticketing(msg_lower)
            )
            # Confirmation lock: yes/no replies should stay in the active
            # transactional flow unless user shows explicit complaint intent.
            if binary_reply is not None and not explicit_complaint_evidence:
                return False

        # Keep order slot-collection conversational.
        # If the user is still giving order details (for example quantity),
        # do not jump to complaint/ticket handler on a spurious model label.
        # Allow switching only on explicit complaint/ticket asks.
        if self._is_order_pending_action(current_pending_action) or self._is_order_pending_action(pending_action_target):
            if self._is_ticketing_pending_action(pending_action_target):
                return True
            if self._is_ticketing_pending_action(current_pending_action):
                return True
            if not msg_lower:
                return False
            if self._looks_like_ticketing_request(msg_lower):
                return True
            if self._looks_like_strong_ticket_issue_marker(msg_lower):
                return True
            return False

        # Keep room/table booking flow conversational.
        # Do not auto-switch to complaint ticketing during slot collection just
        # because the model emitted `requires_ticket=true`.
        if effective_intent == IntentType.TABLE_BOOKING:
            if self._is_ticketing_pending_action(pending_action_target):
                return True
            if self._is_ticketing_pending_action(current_pending_action):
                return True
            if not msg_lower:
                return False
            if self._looks_like_ticketing_request(msg_lower):
                return True
            if self._looks_like_operational_issue_for_ticketing(msg_lower):
                return True
            return False

        if llm_ticketing_preference is True:
            return True

        # Keep core order/booking flows intact unless LLM explicitly asks for ticketing
        # or we are already in a ticket-confirm/update pending action.
        if effective_intent in {IntentType.ORDER_FOOD}:
            if self._is_ticketing_pending_action(pending_action_target):
                return True
            if (
                effective_intent in {IntentType.CONFIRMATION_YES, IntentType.CONFIRMATION_NO}
                and self._is_ticketing_pending_action(current_pending_action)
            ):
                return True
            return False

        if effective_intent in {IntentType.COMPLAINT, IntentType.HUMAN_REQUEST}:
            return True
        if self._is_ticketing_pending_action(pending_action_target):
            return True
        if (
            effective_intent in {IntentType.CONFIRMATION_YES, IntentType.CONFIRMATION_NO}
            and self._is_ticketing_pending_action(current_pending_action)
        ):
            return True
        if llm_ticketing_preference is False:
            return False
        if not msg_lower:
            return False
        if self._looks_like_ticketing_request(msg_lower):
            return True
        if self._looks_like_operational_issue_for_ticketing(msg_lower):
            return True
        if self._llm_response_implies_staff_action(llm_response_text):
            return True
        return False

    @staticmethod
    def _extract_full_kb_ticketing_preference(llm_result: Any) -> bool | None:
        llm_output = llm_result.llm_output if isinstance(llm_result.llm_output, dict) else {}
        pending_data = llm_result.pending_data if isinstance(llm_result.pending_data, dict) else {}
        candidates = (
            llm_output.get("requires_ticket"),
            llm_output.get("is_ticket_intent"),
            llm_output.get("ticket_required"),
            llm_output.get("ticketing_required"),
            pending_data.get("requires_ticket"),
            pending_data.get("is_ticket_intent"),
            pending_data.get("ticketing_flow"),
        )
        for value in candidates:
            parsed = ChatService._coerce_optional_bool(value)
            if parsed is not None:
                return parsed
        return None

    @staticmethod
    def _extract_full_kb_ticket_ready_flag(llm_result: Any) -> bool | None:
        llm_output = llm_result.llm_output if isinstance(llm_result.llm_output, dict) else {}
        pending_data = llm_result.pending_data if isinstance(llm_result.pending_data, dict) else {}
        candidates = (
            llm_output.get("ticket_ready_to_create"),
            llm_output.get("ready_to_create"),
            llm_output.get("ticket_ready"),
            llm_output.get("ticket_create_ready"),
            pending_data.get("ticket_ready_to_create"),
            pending_data.get("ready_to_create"),
            pending_data.get("ticket_ready"),
            pending_data.get("ticket_create_ready"),
        )
        for value in candidates:
            parsed = ChatService._coerce_optional_bool(value)
            if parsed is not None:
                return parsed
        return None

    @classmethod
    def _is_pure_llm_ticket_ready(cls, llm_result: Any) -> bool:
        explicit_ready = cls._extract_full_kb_ticket_ready_flag(llm_result)
        pending_action = str(getattr(llm_result, "pending_action", "") or "").strip()
        next_state = getattr(llm_result, "next_state", ConversationState.IDLE)
        waiting_for_user = pending_action or next_state in {
            ConversationState.AWAITING_CONFIRMATION,
            ConversationState.AWAITING_INFO,
            ConversationState.AWAITING_SELECTION,
        }
        if explicit_ready is not None:
            return bool(explicit_ready) and not waiting_for_user
        return not waiting_for_user and next_state in {
            ConversationState.COMPLETED,
            ConversationState.ESCALATED,
            ConversationState.PROCESSING_ORDER,
        }

    @staticmethod
    def _extract_full_kb_ticketing_reason(llm_result: Any) -> str:
        llm_output = llm_result.llm_output if isinstance(llm_result.llm_output, dict) else {}
        pending_data = llm_result.pending_data if isinstance(llm_result.pending_data, dict) else {}
        reason = (
            llm_output.get("ticket_reason")
            or llm_output.get("ticketing_reason")
            or pending_data.get("ticket_reason")
            or pending_data.get("ticketing_reason")
            or ""
        )
        return str(reason or "").strip()

    @staticmethod
    def _build_ticketing_case_context_text(
        *,
        context: ConversationContext,
        latest_user_message: str,
        llm_response_text: str = "",
        max_messages: int = 14,
        max_chars: int = 6000,
    ) -> str:
        lines: list[str] = []
        history_messages = (
            context.messages
            if max_messages <= 0
            else context.get_recent_messages(max_messages)
        )
        for msg in history_messages:
            role = "User" if msg.role == MessageRole.USER else "Assistant"
            content = str(msg.content or "").strip()
            if not content:
                continue
            lines.append(f"{role}: {content}")

        latest_user = str(latest_user_message or "").strip()
        if latest_user:
            lines.append(f"Latest User: {latest_user}")
        assistant_text = str(llm_response_text or "").strip()
        if assistant_text:
            lines.append(f"Assistant Draft: {assistant_text}")

        joined = "\n".join(lines).strip()
        limit = max(1200, int(max_chars or 6000))
        if len(joined) <= limit:
            return joined
        return joined[-limit:]

    async def _evaluate_ticketing_status_for_turn(
        self,
        *,
        message: str,
        context: ConversationContext,
        intent: IntentType,
        response_text: str = "",
        current_pending_action: str | None = None,
        pending_action_target: str | None = None,
        capabilities_summary: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_message = self._normalize_multi_ask_query_text(message)
        ticketing_plugin_enabled = bool(self._is_ticketing_plugin_enabled())
        ticketing_service_enabled = bool(ticketing_service.is_ticketing_enabled(capabilities_summary))

        base_status = {
            "ticketing_status_checked": bool(normalized_message),
            "ticketing_plugin_enabled": ticketing_plugin_enabled,
            "ticketing_service_enabled": ticketing_service_enabled,
            "ticketing_required": False,
            "ticketing_create_allowed": False,
            "ticketing_route": "none",
            "ticketing_reason": "",
            "ticketing_source": "",
            "ticketing_matched_case": "",
            "ticketing_skip_reason": "",
        }
        if not normalized_message:
            return base_status

        try:
            phase_context = self._get_selected_phase_context(
                context=context,
                pending_data=context.pending_data if isinstance(context.pending_data, dict) else {},
                entities={},
            )
            decision = await ticketing_agent_service.decide_async(
                intent=intent,
                message=normalized_message,
                llm_response_text=str(response_text or ""),
                llm_ticketing_preference=None,
                current_pending_action=current_pending_action,
                pending_action_target=pending_action_target,
                selected_phase_id=phase_context.get("selected_phase_id", ""),
                selected_phase_name=phase_context.get("selected_phase_name", ""),
                conversation_excerpt=self._build_ticketing_case_context_text(
                    context=context,
                    latest_user_message=normalized_message,
                    llm_response_text=response_text,
                ),
            )
        except Exception as exc:
            status = dict(base_status)
            status["ticketing_status_checked"] = False
            status["ticketing_skip_reason"] = f"ticketing_decision_error:{exc}"
            return status

        status = dict(base_status)
        status["ticketing_required"] = bool(getattr(decision, "activate", False))
        status["ticketing_route"] = str(getattr(decision, "route", "") or "none")
        status["ticketing_reason"] = str(getattr(decision, "reason", "") or "")
        status["ticketing_source"] = str(getattr(decision, "source", "") or "")
        status["ticketing_matched_case"] = str(getattr(decision, "matched_case", "") or "")

        if not status["ticketing_required"]:
            return status
        if not ticketing_plugin_enabled:
            status["ticketing_skip_reason"] = "ticketing_plugin_disabled"
            return status
        if not ticketing_service_enabled:
            status["ticketing_skip_reason"] = "ticketing_service_disabled"
            return status

        phase_gate = self._detect_ticketing_phase_service_mismatch(
            message=normalized_message,
            context=context,
            pending_data=context.pending_data if isinstance(context.pending_data, dict) else {},
            entities={},
        )
        if phase_gate is None:
            phase_gate = self._detect_phase_service_unavailable_for_intent(
                message=normalized_message,
                intent=intent,
                context=context,
                pending_data=context.pending_data if isinstance(context.pending_data, dict) else {},
                entities={},
            )
        if phase_gate is not None:
            status["ticketing_skip_reason"] = (
                "phase_service_unavailable"
                if bool(phase_gate.get("phase_service_unavailable"))
                else "phase_service_mismatch"
            )
            status["phase_gate_service_id"] = str(phase_gate.get("service_id") or "")
            status["phase_gate_service_name"] = str(phase_gate.get("service_name") or "")
            status["phase_gate_current_phase_id"] = str(phase_gate.get("current_phase_id") or "")
            status["phase_gate_service_phase_id"] = str(phase_gate.get("service_phase_id") or "")
            return status

        ticketing_toggle_gate = self._detect_ticketing_phase_service_ticketing_disabled(
            message=normalized_message,
            context=context,
            pending_data=context.pending_data if isinstance(context.pending_data, dict) else {},
            entities={},
        )
        if ticketing_toggle_gate is not None:
            status["ticketing_skip_reason"] = "phase_service_ticketing_disabled"
            status["phase_gate_service_id"] = str(ticketing_toggle_gate.get("service_id") or "")
            status["phase_gate_service_name"] = str(ticketing_toggle_gate.get("service_name") or "")
            status["phase_gate_current_phase_id"] = str(ticketing_toggle_gate.get("current_phase_id") or "")
            status["phase_gate_service_phase_id"] = str(ticketing_toggle_gate.get("service_phase_id") or "")
            return status

        status["ticketing_create_allowed"] = True
        return status

    @staticmethod
    def _looks_like_operational_issue_for_ticketing(msg_lower: str) -> bool:
        text = str(msg_lower or "").strip().lower()
        if not text:
            return False

        complaint_markers = (
            "cockroach",
            "roach",
            "not working",
            "broken",
            "dirty",
            "bad smell",
            "smell",
            "leak",
            "water leak",
            "no water",
            "no hot water",
            "ac not",
            "air conditioner",
            "wifi not",
            "tv not",
            "noise",
            "billing issue",
            "wrong charge",
            "refund",
        )
        operational_request_nouns = (
            "towel",
            "blanket",
            "pillow",
            "soap",
            "shampoo",
            "housekeeping",
            "laundry",
            "clean room",
            "cleaning",
            "maintenance",
            "room service",
        )
        request_verbs = (
            "i need",
            "need ",
            "please send",
            "send ",
            "bring ",
            "arrange ",
            "require ",
            "please arrange",
        )
        if any(marker in text for marker in complaint_markers):
            return True
        if "room service" in text:
            return True
        if any(noun in text for noun in operational_request_nouns) and any(
            verb in text for verb in request_verbs
        ):
            return True
        return False

    def _infer_plugin_ticket_fallback_intent(
        self,
        *,
        message: str,
        effective_intent: IntentType,
        next_state: ConversationState,
    ) -> IntentType | None:
        msg = str(message or "").strip()
        msg_lower = msg.lower()
        if not msg_lower:
            return None
        if effective_intent in {IntentType.GREETING, IntentType.UNCLEAR, IntentType.CONFIRMATION_NO}:
            return None
        if effective_intent == IntentType.CONFIRMATION_YES and next_state in {
            ConversationState.AWAITING_CONFIRMATION,
            ConversationState.AWAITING_INFO,
            ConversationState.AWAITING_SELECTION,
        }:
            return None

        if self._looks_like_information_query(msg_lower):
            return None
        if self._looks_like_ticketing_request(msg_lower):
            return IntentType.COMPLAINT
        if self._looks_like_operational_issue_for_ticketing(msg_lower):
            return IntentType.COMPLAINT
        if self._looks_like_loss_or_security_issue(msg_lower):
            return IntentType.COMPLAINT
        if self._looks_like_booking_change_request(msg_lower):
            return IntentType.COMPLAINT
        if self._is_action_request_text(msg):
            return IntentType.COMPLAINT
        return None

    def _apply_full_kb_plugin_ticket_followup_state(
        self,
        *,
        combined_ticket_meta: dict[str, Any],
        current_pending_action: str | None,
        pending_action_target: str | None,
        pending_data_target: dict[str, Any] | None,
        internal_pending_entries: dict[str, Any],
        next_state: ConversationState,
        clear_pending_data: bool,
    ) -> tuple[str | None, dict[str, Any] | None, ConversationState, bool]:
        meta = combined_ticket_meta if isinstance(combined_ticket_meta, dict) else {}
        if not meta:
            return pending_action_target, pending_data_target, next_state, clear_pending_data

        pending_draft = meta.get("pending_ticket_draft")
        if isinstance(pending_draft, dict) and pending_draft:
            internal_pending_entries[_INTERNAL_PENDING_TICKET_DRAFT_KEY] = dict(pending_draft)

        skip_reason = str(
            meta.get("ticket_skip_reason")
            or meta.get("ticket_create_skip_reason")
            or meta.get("ticketing_skip_reason")
            or ""
        ).strip().lower()
        if skip_reason == "missing_required_details":
            missing_fields = {
                str(field).strip().lower()
                for field in (meta.get("ticket_missing_fields") or [])
                if str(field).strip()
            }
            pending_action_hint = str(meta.get("ticket_pending_action") or "").strip().lower()
            if pending_action_hint:
                pending_action_target = pending_action_hint
            if "room_number" in missing_fields:
                pending_action_target = "collect_ticket_room_number"
                next_state = ConversationState.AWAITING_INFO
                clear_pending_data = False

        if bool(meta.get("clear_pending_ticket_draft")):
            internal_pending_entries.pop(_INTERNAL_PENDING_TICKET_DRAFT_KEY, None)

        if bool(meta.get("ticket_resumed_from_pending_draft")):
            internal_pending_entries.pop(_INTERNAL_PENDING_TICKET_DRAFT_KEY, None)
            if str(current_pending_action or "").strip().lower() == "collect_ticket_room_number":
                pending_action_target = None
            if next_state == ConversationState.ESCALATED:
                next_state = ConversationState.IDLE
            clear_pending_data = False

        return pending_action_target, pending_data_target, next_state, clear_pending_data

    @staticmethod
    def _looks_like_ticket_update_note(msg_lower: str) -> bool:
        if not msg_lower:
            return False
        if msg_lower in {"yes", "no", "cancel", "stop"}:
            return False
        # Free-form note text should generally be consumed by ticket-update flow.
        return len(msg_lower.split()) >= 3

    @staticmethod
    def _looks_like_ticket_identity_details_reply(msg_lower: str) -> bool:
        if not msg_lower:
            return False
        if msg_lower in {"cancel", "stop", "no", "not now"}:
            return True
        if bool(re.search(r"(?:\+?\d[\d\s\-()]{6,}\d)", msg_lower)):
            return True
        if "name" in msg_lower and len(msg_lower.split()) >= 2:
            return True
        if len(msg_lower.split()) >= 2 and all(token.isalpha() for token in msg_lower.split()[:2]):
            return True
        return False

    def _resolve_current_ticketing_phase(
        self,
        *,
        context: ConversationContext,
        pending_data: dict[str, Any] | None = None,
        entities: dict[str, Any] | None = None,
    ) -> str:
        pending = pending_data if isinstance(pending_data, dict) else {}
        entity_map = entities if isinstance(entities, dict) else {}
        ctx_pending = (
            context.pending_data
            if isinstance(getattr(context, "pending_data", None), dict)
            else {}
        )

        # ── Explicit phase selection is AUTHORITATIVE and STICKY ──────────────
        # When a phase was explicitly chosen for this conversation (request
        # metadata.selected_phase_id, persisted as pending_data["_selected_phase_id"]
        # in api/routes/chat.py), it must win over every auto-derivation below and
        # stay put for the whole conversation. Without this, the per-turn resolver
        # ignored the selection and fell through to the "during_stay" default, which
        # then filtered the router's phase-scoped services down to zero (e.g. a
        # pre_booking session lost its support_request / room_booking_request
        # services). We consult the persisted context store too so the choice
        # survives across turns even if a caller passes a thin pending_data.
        explicit_phase = self._normalize_phase_identifier(
            entity_map.get("selected_phase_id")
            or pending.get("_selected_phase_id")
            or pending.get("selected_phase_id")
            or ctx_pending.get("_selected_phase_id")
        )
        if explicit_phase:
            return explicit_phase

        # If booking context has a resolved phase from dates, use it as primary source
        if getattr(context, "booking_phase", None):
            normalized_bk = self._normalize_phase_identifier(context.booking_phase)
            if normalized_bk:
                return normalized_bk
        integration = ticketing_service.get_integration_context(context)
        candidates = (
            entity_map.get("phase"),
            pending.get("phase"),
            integration.get("phase"),
        )
        for candidate in candidates:
            normalized = self._normalize_phase_identifier(candidate)
            if normalized:
                return normalized

        flow = str(integration.get("flow") or integration.get("bot_mode") or "").strip().lower()
        if flow in {"engage", "booking", "booking_bot"}:
            return "pre_booking"
        return "during_stay"

    def _infer_phase_gate_ticketing_intent(
        self,
        *,
        message: str,
        intent: IntentType,
    ) -> IntentType | None:
        if intent not in {IntentType.COMPLAINT, IntentType.HUMAN_REQUEST}:
            return None
        inferred = self._infer_phase_gate_transactional_intent(message)
        if inferred is not None:
            return inferred
        if self._looks_like_operational_issue_for_ticketing(message):
            return IntentType.ROOM_SERVICE
        return None

    def _detect_ticketing_phase_service_mismatch(
        self,
        *,
        message: str,
        context: ConversationContext,
        pending_data: dict[str, Any] | None = None,
        entities: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        try:
            services = config_service.get_services()
        except Exception:
            services = []
        if not isinstance(services, list):
            services = []

        matched_service = self._resolve_service_from_phase_signals(
            services=services,
            entities=entities if isinstance(entities, dict) else {},
            pending_data=pending_data if isinstance(pending_data, dict) else {},
        )
        llm_only_phase_gate = bool(getattr(settings, "chat_phase_gate_llm_only", True))
        if matched_service is None:
            if llm_only_phase_gate:
                # LLM-first mode still needs a semantic safety-net when service_id is omitted.
                matched_service = self._match_phase_service_from_rows(
                    message=message,
                    services=services,
                    min_score=0.72,
                )
            else:
                matched_service = self._match_phase_managed_service_request(message)
        if matched_service is None:
            return None

        current_phase_id = self._resolve_current_ticketing_phase(
            context=context,
            pending_data=pending_data,
            entities=entities,
        )
        service_phase_id = self._normalize_phase_identifier(matched_service.get("phase_id"))
        if not current_phase_id or not service_phase_id or service_phase_id == current_phase_id:
            return None

        service_name = str(matched_service.get("service_name") or "This service").strip()
        current_phase_name = self._phase_label(current_phase_id)
        service_phase_name = self._phase_label(service_phase_id)
        current_phase_services = [
            service
            for service in services
            if isinstance(service, dict)
            and bool(service.get("is_active", True))
            and self._normalize_phase_identifier(service.get("phase_id")) == current_phase_id
        ]
        available_names = [
            str(service.get("name") or service.get("id") or "").strip()
            for service in current_phase_services
            if str(service.get("name") or service.get("id") or "").strip()
        ]
        response_text = self._build_phase_gate_sales_response(
            service_name=service_name,
            current_phase_id=current_phase_id,
            current_phase_name=current_phase_name,
            service_phase_id=service_phase_id,
            service_phase_name=service_phase_name,
            available_names=available_names,
        )
        additional_lines = self._phase_gate_additional_unavailable_lines(
            message=message,
            current_phase_id=current_phase_id,
            all_phase_services=[
                service
                for service in services
                if isinstance(service, dict)
                and bool(service.get("is_active", True))
                and self._normalize_phase_identifier(service.get("phase_id"))
            ],
            phase_services=current_phase_services,
            primary_service_name=service_name,
        )
        if additional_lines:
            response_text = f"{response_text} {' '.join(additional_lines)}".strip()
        response_text = self._dedupe_response_sentences(response_text) or str(response_text or "").strip()
        return {
            "service_id": str(matched_service.get("service_id") or "").strip(),
            "service_name": service_name,
            "current_phase_id": current_phase_id,
            "current_phase_name": current_phase_name,
            "service_phase_id": service_phase_id,
            "service_phase_name": service_phase_name,
            "response_text": response_text,
        }

    @staticmethod
    def _is_phase_service_ticketing_enabled(service: dict[str, Any]) -> bool:
        if not isinstance(service, dict):
            return True
        if "ticketing_enabled" not in service:
            return True
        value = service.get("ticketing_enabled")
        if value is None:
            return True
        return bool(value)

    def _detect_ticketing_phase_service_ticketing_disabled(
        self,
        *,
        message: str,
        context: ConversationContext,
        pending_data: dict[str, Any] | None = None,
        entities: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        try:
            services = config_service.get_services()
        except Exception:
            services = []
        if not isinstance(services, list):
            services = []

        matched_service = self._resolve_service_from_phase_signals(
            services=services,
            entities=entities if isinstance(entities, dict) else {},
            pending_data=pending_data if isinstance(pending_data, dict) else {},
        )
        llm_only_phase_gate = bool(getattr(settings, "chat_phase_gate_llm_only", True))
        if matched_service is None:
            if llm_only_phase_gate:
                matched_service = self._match_phase_service_from_rows(
                    message=message,
                    services=services,
                    min_score=0.72,
                )
            else:
                matched_service = self._match_phase_managed_service_request(message)
        if matched_service is None:
            return None

        current_phase_id = self._resolve_current_ticketing_phase(
            context=context,
            pending_data=pending_data,
            entities=entities,
        )
        service_phase_id = self._normalize_phase_identifier(matched_service.get("phase_id"))
        if not current_phase_id or not service_phase_id:
            return None
        if current_phase_id != service_phase_id:
            return None
        if self._is_phase_service_ticketing_enabled(matched_service):
            return None

        service_name = str(matched_service.get("service_name") or "This service").strip()
        current_phase_name = self._phase_label(current_phase_id)
        response_text = (
            f"Ticketing is not enabled for {service_name} in {current_phase_name} phase."
        )
        return {
            "service_id": str(matched_service.get("service_id") or "").strip(),
            "service_name": service_name,
            "current_phase_id": current_phase_id,
            "current_phase_name": current_phase_name,
            "service_phase_id": service_phase_id,
            "service_phase_name": self._phase_label(service_phase_id),
            "response_text": response_text,
        }

    async def _build_ticketing_phase_gate_handler_result(
        self,
        phase_gate: dict[str, Any],
        *,
        user_message: str,
        intent: IntentType,
        context: ConversationContext,
        capabilities_summary: dict[str, Any],
    ) -> HandlerResult:
        response_text = await self._compose_policy_guardrail_response(
            user_message=user_message,
            context=context,
            capabilities_summary=capabilities_summary,
            intent=intent,
            gate_payload=phase_gate,
            fallback_text="",
            response_source="ticketing_phase_gate",
        )
        if not response_text:
            response_text = "I can help with services available in your current phase."
        response_text = self._dedupe_response_sentences(response_text) or response_text
        raw_actions = phase_gate.get("suggested_actions")
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
                "response_source": "ticketing_phase_gate",
                "ticketing_phase_gate": True,
                "phase_gate_service_id": str(phase_gate.get("service_id") or ""),
                "phase_gate_service_name": str(phase_gate.get("service_name") or ""),
                "phase_gate_current_phase_id": str(phase_gate.get("current_phase_id") or ""),
                "phase_gate_current_phase_name": str(phase_gate.get("current_phase_name") or ""),
                "phase_gate_service_phase_id": str(phase_gate.get("service_phase_id") or ""),
                "phase_gate_service_phase_name": str(phase_gate.get("service_phase_name") or ""),
                "phase_service_unavailable": bool(phase_gate.get("phase_service_unavailable")),
            },
        )

    @staticmethod
    def _looks_like_ticketing_request(msg_lower: str) -> bool:
        """Detect ticket status/update/create asks and route to complaint workflow."""
        if not msg_lower:
            return False

        status_markers = (
            "ticket status",
            "status of ticket",
            "check ticket",
            "my ticket",
            "complaint status",
        )
        update_markers = (
            "update ticket",
            "ticket update",
            "add note",
            "append note",
            "update my complaint",
            "follow up ticket",
            "ticket follow up",
        )
        create_markers = (
            "raise complaint",
            "file complaint",
            "create ticket",
            "raise ticket",
            "log complaint",
        )
        return any(marker in msg_lower for marker in (status_markers + update_markers + create_markers))

    @staticmethod
    def _apply_ticket_metadata_contract(metadata: dict[str, Any]) -> dict[str, Any]:
        """Normalize ticket metadata keys across runtime paths."""
        normalized = dict(metadata or {})
        ticket_skip_reason = str(normalized.get("ticket_skip_reason") or "").strip()
        if not ticket_skip_reason:
            ticket_skip_reason = str(normalized.get("ticket_create_skip_reason") or "").strip()
        if not ticket_skip_reason:
            ticket_skip_reason = str(normalized.get("ticketing_skip_reason") or "").strip()
        if ticket_skip_reason:
            normalized["ticket_skip_reason"] = ticket_skip_reason
            normalized.setdefault("ticket_created", False)
        return normalized
