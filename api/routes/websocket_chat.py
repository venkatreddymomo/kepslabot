"""
WebSocket chat endpoint.

Adds real-time bidirectional messaging on top of the existing HTTP API.
HTTP /api/chat/message is UNCHANGED — WebSocket is purely additive.

Protocol (JSON frames):
  Client → Server:
    {"type": "message",  "session_id": "…", "message": "…",
     "hotel_code": "…", "channel": "web_widget", "metadata": {…}}
    {"type": "ping"}

  Server → Client:
    {"type": "connected", "session_id": "…"}     on connect
    {"type": "typing"}                            immediately when message received
    {"type": "response",  "data": {…}}            full ChatResponse as dict
    {"type": "error",     "detail": "…"}          on processing failure
    {"type": "pong", "healthy": true|false, "detail": "…"}
                                                  reply to ping; `healthy` says
                                                  whether the backend can serve a
                                                  turn (the socket being open does
                                                  not) — it drives the widget's
                                                  Online / Offline indicator
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy.exc import OperationalError

from models.database import AsyncSessionLocal
from schemas.chat import ChatRequest
from schemas.orchestration import OrchestrationDecision
from services.message_dedup import message_dedup

logger = logging.getLogger(__name__)  # noqa: E501
router = APIRouter(tags=["WebSocket"])


# ---------------------------------------------------------------------------
# Backend health (drives the widget's Online / Offline dot)
# ---------------------------------------------------------------------------
#
# An accepted WebSocket only proves the socket handshake worked — it says
# NOTHING about whether the bot can answer. With the database down the socket
# still opens, still pongs, and the widget still claimed "Online" while every
# turn failed. The `connected` and `pong` frames now carry a real verdict from
# services.backend_health, which watches the database, the LLM provider AND
# recent turn outcomes. See that module for why each is needed.


def _backend_health() -> dict[str, Any]:
    """The current verdict. Never blocks — see BackendHealth.snapshot."""
    try:
        from services.backend_health import backend_health
        return backend_health.snapshot()
    except Exception:  # noqa: BLE001 — health must never break the socket
        return {"ok": True, "detail": "", "failing": [], "checks": {}}


def _mark_backend_unhealthy(detail: str) -> None:
    """A turn just failed — the most direct evidence the bot cannot answer."""
    try:
        from services.backend_health import backend_health
        backend_health.record_turn(ok=False, detail=detail)
    except Exception:
        pass


def _mark_turn_ok() -> None:
    """A turn completed — clears the consecutive-failure run."""
    try:
        from services.backend_health import backend_health
        backend_health.record_turn(ok=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Connection Manager
# ---------------------------------------------------------------------------

class ConnectionManager:
    """Tracks active WebSocket connections keyed by session_id."""

    def __init__(self) -> None:
        # session_id → WebSocket
        self._connections: dict[str, WebSocket] = {}

    async def connect(self, websocket: WebSocket, session_id: str) -> None:
        await websocket.accept()
        # If another tab opened the same session, close the old one gracefully
        old = self._connections.get(session_id)
        if old and old is not websocket:
            try:
                await old.close(code=1001)
            except Exception:
                pass
        self._connections[session_id] = websocket
        logger.info("[WS] connected session_id=%s  total=%d", session_id, len(self._connections))

    def disconnect(self, session_id: str, websocket: WebSocket | None = None) -> None:
        # Only evict if the socket tearing down is still the registered one. A
        # superseded socket (the guest reconnected → connect() already swapped in
        # the new socket) must NOT remove the live socket's registration when its
        # own handler finally unwinds — otherwise the session flips to offline and
        # later session-routed sends can't find the live socket.
        current = self._connections.get(session_id)
        if websocket is not None and current is not None and current is not websocket:
            logger.info("[WS] disconnect skipped (superseded socket) session_id=%s", session_id)
            return
        self._connections.pop(session_id, None)
        logger.info("[WS] disconnected session_id=%s  total=%d", session_id, len(self._connections))

    def is_connected(self, session_id: str) -> bool:
        """True when this session has a live guest WebSocket right now.

        Used by the admin chat-history list to show a session as "online" only
        while the guest's widget is actually connected — so a cleared/closed/
        left session drops to offline immediately instead of lingering for the
        2-hour recency window.
        """
        return bool(session_id) and session_id in self._connections

    async def send(self, websocket: WebSocket, payload: dict[str, Any]) -> None:
        try:
            await websocket.send_text(json.dumps(payload, default=str))
        except Exception as exc:
            logger.debug("[WS] send failed: %s", exc)

    async def send_to_session(
        self,
        session_id: str,
        payload: dict[str, Any],
        fallback: WebSocket | None = None,
    ) -> bool:
        """Send to the session's CURRENT live socket — not a captured reference.

        A guest can reconnect mid-turn: page reload, network blip, or (in the
        admin harness) switching the hotel_code re-opens the WebSocket. When that
        happens the slow orchestration coroutine is still holding the ORIGINAL
        socket, which is now closed. Sending the finished answer there drops it
        silently ("WebSocket is not connected") and the widget eventually times
        out with "taking longer than usual" even though a perfectly good answer
        was produced and cached. Routing by session_id delivers to whichever
        socket is live *now*; the client matches it back up by message_id.

        Returns True if a socket was found to send on (delivery is best-effort).
        """
        ws = self._connections.get(session_id) if session_id else None
        if ws is None:
            ws = fallback
        if ws is None:
            return False
        await self.send(ws, payload)
        return True

    @property
    def active_count(self) -> int:
        return len(self._connections)


manager = ConnectionManager()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_parse(raw: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    return None


async def _process_chat(
    frame: dict[str, Any],
) -> dict[str, Any]:
    """Run chat_service.process_message and return the response as a plain dict."""
    # Import here to avoid circular imports at module load time
    from services.chat_service import chat_service

    # Extract enriched fields sent by the frontend
    conversation_id: str | None = str(frame.get("conversation_id") or "").strip() or None
    visitor_last_msg_time: str | None = str(frame.get("visitor_last_msg_time") or "").strip() or None
    message_type: str = str(frame.get("message_type") or "text").strip()

    # Merge enriched fields into metadata so they reach the service layer
    base_metadata: dict[str, Any] = dict(frame.get("metadata") or {})
    if conversation_id:
        base_metadata.setdefault("conversation_id", conversation_id)
    if visitor_last_msg_time:
        base_metadata.setdefault("visitor_last_msg_time", visitor_last_msg_time)
    base_metadata.setdefault("message_type", message_type)
    # Generate a turn_trace_id so the suggestions API can find chip jobs keyed
    # to this specific turn. Without it the client always gets empty chips.
    turn_trace_id = base_metadata.get("turn_trace_id") or f"ws_{secrets.token_hex(12)}"
    base_metadata["turn_trace_id"] = turn_trace_id

    try:
        chat_req = ChatRequest(
            session_id=str(frame.get("session_id") or ""),
            message=str(frame.get("message") or ""),
            hotel_code=str(frame.get("hotel_code") or "DEFAULT"),
            guest_phone=frame.get("guest_phone"),
            channel=str(frame.get("channel") or "web_widget"),
            metadata=base_metadata,
        )
    except Exception as exc:
        raise ValueError(f"Invalid payload: {exc}") from exc

    if not chat_req.session_id:
        raise ValueError("session_id is required")
    if not chat_req.message.strip():
        raise ValueError("message cannot be empty")

    # -- Agent room mute check ------------------------------------------
    # If a human agent is actively handling this session, forward the
    # guest's message to the agent (and all observers) and return a silent
    # ack. The bot does NOT call process_message() while an agent is in
    # the room. The guest turn is also audited so the bot has full context
    # when it resumes after the agent resolves the session.
    from services.agent_room_service import agent_room_service
    from datetime import datetime, timezone as _tz
    if agent_room_service.has_active_agent(chat_req.session_id):
        _ts = datetime.now(_tz.utc).isoformat()
        # Route to primary agent + all observers
        await agent_room_service.send_to_all_agents_in_room(chat_req.session_id, {
            "type": "guest_message",
            "session_id": chat_req.session_id,
            "text": chat_req.message,
            "timestamp": _ts,
        })
        # Audit guest turn to conversation context for bot continuity
        await agent_room_service.audit_guest_message(
            session_id=chat_req.session_id,
            text=chat_req.message,
        )
        # Return a silent acknowledgment -- no bot response
        return {
            "session_id": chat_req.session_id,
            "message": "",
            "display_message": "",
            "state": "escalated",
            "success": True,
            "suggested_actions": [],
            "metadata": {"agent_handling": True},
        }

    # -- Kebo dashboard agent mute check --------------------------------
    # The kebo agent dashboard writes its replies to KEBO_ENGAGE_SESSIONS (not
    # our /ws/agent). The bridge poller marks the session active when it sees
    # them. While active, relay the guest's message to the agent (via KES) and
    # stay silent so the bot doesn't talk over the human agent.
    from services.kebo_agent_bridge import kebo_agent_bridge
    if kebo_agent_bridge.is_agent_active(chat_req.session_id):
        kebo_agent_bridge.relay_guest_message(
            session_id=chat_req.session_id,
            hotel_code=chat_req.hotel_code,
            text_msg=chat_req.message,
        )
        return {
            "session_id": chat_req.session_id,
            "message": "",
            "display_message": "",
            "state": "escalated",
            "success": True,
            "suggested_actions": [],
            "metadata": {"agent_handling": True, "agent_source": "kebo"},
        }

    logger.info(
        "[WS] Processing message  session_id=%s  conversation_id=%s  hotel=%s",
        chat_req.session_id, conversation_id or "(none)", chat_req.hotel_code,
    )

    # Push hotel context at the WS-handler level so it stays alive across
    # BOTH process_message AND the post-processing form-trigger call.
    # process_message also pushes (and pops) its own inner scope, but that
    # one closes before _inject_form_trigger runs — leaving its DB lookup
    # without a hotel scope and falling through to HotelNotFoundError.
    # Mirror the HTTP route's behaviour (api/routes/chat.py:3630).
    from services.db_config_service import db_config_service
    _ws_hotel_code = str(chat_req.hotel_code or "").strip()
    _ws_scope_token = (
        db_config_service.set_hotel_context(_ws_hotel_code)
        if _ws_hotel_code
        else None
    )

    # ── Deployment model pin ────────────────────────────────────────────────
    # A customer embed carries a widget_key; the harness doesn't. Resolve the
    # deployment's pinned model SERVER-SIDE (from the DB row — never the URL)
    # and pin this turn to it, so both the LLM call and the prompt variant it
    # loads follow the customer's model no matter what the harness dropdown
    # says. Unpinned/harness turns get "" and resolve as they always have.
    # Pushed alongside the hotel scope so it covers process_message AND the
    # post-processing form-trigger call below.
    from services.model_override_service import reset_pinned_model, set_pinned_model
    from services.widget_deployment_service import widget_deployment_service as _wds
    _ws_widget_key = str(frame.get("widget_key") or "").strip()
    _ws_pinned_model = (
        await _wds.model_for_widget_key(_ws_widget_key) if _ws_widget_key else ""
    )
    _ws_pin_token = set_pinned_model(_ws_pinned_model) if _ws_pinned_model else None
    if _ws_pinned_model:
        logger.info(
            "[WS] deployment pin active  widget_key=%s  model=%s",
            _ws_widget_key, _ws_pinned_model,
        )
    # ── Suggestion-chip prefetch (parity with the HTTP /api/chat/message route) ──
    # When the guest TAPS a suggestion chip, its answer was likely pre-cooked in
    # the background — serve that instead of re-running the whole pipeline. When
    # the guest TYPES instead, throw away any chip answers still cooking for this
    # session. Without this block the WS widget never consumed the prefetch cache:
    # every chip click paid the full pipeline cost and the background cooking was
    # wasted work that competed for the OpenAI rate limit (slowing typed turns).
    from services.suggestion_prefetch_service import suggestion_prefetch_service
    _pf_source_type = str(
        base_metadata.get("ui_source_type")
        or base_metadata.get("source_type")
        or ""
    ).strip().lower()
    _pf_batch_id = str(
        base_metadata.get("prefetch_batch_id")
        or base_metadata.get("ui_prefetch_batch_id")
        or ""
    ).strip()
    if _pf_source_type != "suggested_action":
        # Typed input starts a new turn → drop any chip answers still cooking.
        try:
            await suggestion_prefetch_service.cancel_session_prefetch(
                session_id=chat_req.session_id,
                reason="typed_input_new_turn_ws",
            )
        except Exception:
            pass

    async def _run_pipeline(_db_session: Any) -> Any:
        # Chip click → try the pre-cooked answer first. resolve_cached_answer
        # waits for an in-flight cook to FINISH rather than restarting it (a
        # fresh run has no head start, so it is slower). Falls through to the
        # live pipeline only if nothing was pre-cooked or the cook overran.
        if _pf_source_type == "suggested_action":
            _cached = await suggestion_prefetch_service.resolve_cached_answer(
                session_id=chat_req.session_id,
                user_message=str(chat_req.message or "").strip(),
                source_type=_pf_source_type,
                batch_id=_pf_batch_id,
                conversation_id=str((base_metadata or {}).get("conversation_id") or "").strip(),
            )
            if isinstance(_cached, dict) and _cached:
                from api.routes.chat import _build_prefetch_cached_response
                cached_response = await _build_prefetch_cached_response(
                    request=chat_req,
                    hotel_code=_ws_hotel_code,
                    cached_payload=_cached,
                    db=_db_session,
                )
                if cached_response is not None:
                    return cached_response
            # Cache miss → carry the chip's routing context into the live cook.
            try:
                _chip_ctx = await suggestion_prefetch_service.resolve_chip_click_context(
                    session_id=chat_req.session_id,
                    user_message=str(chat_req.message or "").strip(),
                    batch_id=_pf_batch_id,
                    conversation_id=str((base_metadata or {}).get("conversation_id") or "").strip(),
                )
                if isinstance(_chip_ctx, dict) and _chip_ctx:
                    base_metadata["chip_origin_responder"] = str(_chip_ctx.get("origin_responder") or base_metadata.get("chip_origin_responder") or "").strip()
                    base_metadata["chip_origin_service_id"] = str(_chip_ctx.get("origin_service_id") or base_metadata.get("chip_origin_service_id") or "").strip()
                    base_metadata["chip_target_service_id"] = str(_chip_ctx.get("target_service_id") or base_metadata.get("chip_target_service_id") or "").strip()
                    base_metadata["chip_active_property_ids"] = list(_chip_ctx.get("active_property_ids") or base_metadata.get("chip_active_property_ids") or [])
                    base_metadata["chip_selected_book_ids"] = list(_chip_ctx.get("selected_book_ids") or base_metadata.get("chip_selected_book_ids") or [])
                    base_metadata["chip_turn_trace_id"] = str(_chip_ctx.get("turn_trace_id") or base_metadata.get("chip_turn_trace_id") or "").strip()
                    base_metadata["chip_evidence_fingerprint"] = str(_chip_ctx.get("evidence_fingerprint") or base_metadata.get("chip_evidence_fingerprint") or "").strip()
                    chat_req.metadata = base_metadata
            except Exception:
                pass

        # ── Pre-stored location intro (LLM-free) ─────────────────────────────
        # A dropdown location pick (or the intro CTA) carries a curated answer
        # in the DB. Serve it straight — scope + both history turns are written
        # so the NEXT turn answers normally with full context. Returns None to
        # fall through to the live pipeline for everything else.
        # A hotel-choice button click is the same deal one level down — curated
        # copy for the property, tried first.
        from api.routes.chat import (
            _maybe_build_location_intro_response,
            _maybe_build_property_intro_response,
        )
        _intro_resp = await _maybe_build_property_intro_response(
            request=chat_req,
            hotel_code=_ws_hotel_code,
            source_type=_pf_source_type,
            db=_db_session,
        ) or await _maybe_build_location_intro_response(
            request=chat_req,
            hotel_code=_ws_hotel_code,
            source_type=_pf_source_type,
            db=_db_session,
        )
        if _intro_resp is not None:
            return _intro_resp

        return await chat_service.process_message(chat_req, db_session=_db_session)

    # Open the diagnostics turn context BEFORE the pipeline runs. Without this
    # the WS path — which is what the real widget uses — left _ACTIVE_TURN_CONTEXT
    # empty for the whole turn, so every LLM call llm/client.py logged fell back
    # to trace_meta alone and landed in new_bot_log_llm with hotel_code = ''.
    # That was 9,342 of 9,832 rows unattributable: the ONLY attributed rows in
    # the table came from the HTTP /message route, which does call this.
    from services.config_service import config_service as _ws_config_service
    from services.turn_diagnostics_service import turn_diagnostics_service

    _, _ws_turn_token = turn_diagnostics_service.begin_turn(
        request=chat_req,
        api_trace_id=turn_trace_id,
    )
    _ws_ctx = turn_diagnostics_service.get_turn_context()
    if _ws_ctx:
        # Keep the id the frontend already knows (chips are keyed to it) rather
        # than the fresh "turn-<hex>" begin_turn minted, so logs and chip jobs
        # still join on one value.
        _ws_ctx["turn_trace_id"] = turn_trace_id
        # Bill against the canonical tenant, not a placeholder: the frame may
        # carry "DEFAULT". Resolved into the LOG CONTEXT ONLY — chat_req is left
        # exactly as the pipeline has always received it on this path.
        _ws_ctx["hotel_code"] = _ws_config_service.resolve_hotel_code(
            chat_req.hotel_code
        )

    _ws_prev_bot_message = None
    try:
        # Serialize this turn's context read-modify-write per session so a
        # concurrent lead-form submit (HTTP) fired at the same instant as this
        # WS turn can't clobber the property scope this turn pins (Cause-B fix).
        # Per-session lock: never blocks other sessions.
        from core.context_manager import context_manager as _ws_cm_lock
        async with _ws_cm_lock.session_lock(chat_req.session_id):
            # The bot message this guest is answering, read BEFORE the turn runs.
            # The form trigger below loads the context after this turn's reply is
            # saved, so its "last assistant message" would be that reply.
            # Admin-managed hotels only (T24 onward); Zone, Mango and Sarovar keep
            # the behaviour they were tested with.
            try:
                from services import prebooking_settings_service as _ws_pbs
                from services.llm_orchestration_service import llm_orchestration_service as _ws_los
                if _ws_pbs.is_admin_managed(_ws_hotel_code.lower()):
                    _ws_pre_turn_ctx = await _ws_cm_lock.get_context(chat_req.session_id, db_session=None)
                    if _ws_pre_turn_ctx is not None:
                        _ws_prev_bot_message = str(
                            _ws_los._last_message_by_role(_ws_pre_turn_ctx, "assistant") or ""
                        )
            except Exception:
                _ws_prev_bot_message = None
            try:
                async with AsyncSessionLocal() as db_session:
                    response = await _run_pipeline(db_session)
            except OperationalError:
                # The pooled connection dropped mid-query (transient — e.g. a VPN
                # blip or a server-side idle timeout). Retry ONCE on a FRESH
                # connection so the turn still PERSISTS to new_bot_messages. The
                # memory-only fallback below does NOT write to the DB (db_session is
                # None → save_context skips it), so without this retry every
                # exchanged message during a blip would be missing from chat history
                # even though the bot replied.
                try:
                    async with AsyncSessionLocal() as db_session_retry:
                        response = await _run_pipeline(db_session_retry)
                except OperationalError:
                    # DB genuinely unavailable — last-resort in-memory fallback so
                    # the guest still gets a reply (this turn won't be persisted).
                    response = await _run_pipeline(None)

        # ── Post-processing parity with HTTP send_message handler ──────────
        # Steps 1-4 below mirror api/routes/chat.py exactly (excluding media
        # and chip prefetch which are intentionally skipped here).

        # 1. Strip "default property" placeholder artifacts from response text.
        # Skip for the curated location-intro body: the strip collapses runs of
        # whitespace to a single space, which would flatten its blank lines and
        # un-anchor the "### " sub-headers (breaking the widget's markdown).
        try:
            from api.routes.chat import _strip_default_property_references
            _resp_source = (
                response.metadata.get("response_source")
                if isinstance(getattr(response, "metadata", None), dict)
                else None
            )
            if response.message and _resp_source != "location_intro":
                response.message = _strip_default_property_references(response.message)
        except Exception:
            pass

        # 2. Normalise service_llm_label / service_llm_confidence so UI
        #    clients always receive a usable label (falls back to "main").
        try:
            from api.routes.chat import _enrich_service_llm_display_fields
            _enrich_service_llm_display_fields(response)
        except Exception:
            pass

        # 3. Form-trigger post-processor: when the bot is collecting info for a
        # ticketing-enabled service, this attaches form_trigger metadata so the
        # widget renders the inline form instead of plain text. The HTTP route
        # runs this same step (api/routes/chat.py); without it, WS responses
        # carry the "please fill the form" text but never the actual form schema.
        # Load the conversation context so the form-trigger prefill can build a
        # conversation excerpt and anchor the freeform "Issue Details" field to
        # the prior topic. Without it the WS path passed context=None: the prefill
        # had no history, echoed the prompt's example value, and the hallucination
        # guard dropped it — leaving Issue Details blank on the widget. The HTTP
        # route passes context (api/routes/chat.py), so parity requires it here.
        _ws_form_ctx = None
        try:
            from core.context_manager import context_manager as _ws_cm_form
            _ws_form_ctx = await _ws_cm_form.get_context(chat_req.session_id, db_session=None)
        except Exception as _ctx_exc:
            logger.warning("[WS] form-trigger context load failed (prefill degraded): %r", _ctx_exc)
            _ws_form_ctx = None

        try:
            from api.routes.chat import _inject_form_trigger
            await _inject_form_trigger(
                response,
                user_message=str(chat_req.message or ""),
                request_metadata=base_metadata,
                context=_ws_form_ctx,
                previous_bot_message=_ws_prev_bot_message,
            )
        except Exception as _form_exc:
            logger.warning("[WS] _inject_form_trigger failed (non-fatal): %r", _form_exc)

        # 4. Attach display_message — canonical pass-through so UI clients
        #    always get display_message without a separate LLM rewrite call.
        try:
            from api.routes.chat import _attach_display_message
            await _attach_display_message(response)
        except Exception:
            pass

        # 4b. Booking-button marker: if the room agent decided to offer the
        # website booking link, turn its marker into a booking_cta + strip it.
        try:
            from api.routes.chat import _apply_booking_cta_marker
            from core.context_manager import context_manager
            from services.booking_assistance_state import (
                details_submitted as _cta_submitted,
                team_booking_enabled as _cta_team_booking,
            )
            # Active property scope is stamped onto response.metadata by the
            # orchestration layer. Show the booking button only when exactly ONE
            # property is in focus (two+ → no button).
            _ws_meta = response.metadata if isinstance(response.metadata, dict) else {}
            _ws_active_ids = [str(x).strip() for x in (_ws_meta.get("active_property_ids") or []) if str(x).strip()]
            _ws_active_names = [str(x).strip() for x in (_ws_meta.get("active_property_names") or []) if str(x).strip()]
            if not _ws_active_ids:
                try:
                    from core.context_manager import context_manager as _ws_cm
                    _ws_ctx = await _ws_cm.get_context(chat_req.session_id, db_session=None)
                    _ws_pd = (_ws_ctx.pending_data if _ws_ctx and isinstance(_ws_ctx.pending_data, dict) else {})
                    _ws_active_ids = [str(x).strip() for x in (_ws_pd.get("_active_property_ids") or []) if str(x).strip()]
                    if not _ws_active_names:
                        _ws_active_names = [str(x).strip() for x in (_ws_pd.get("_active_property_names") or []) if str(x).strip()]
                except Exception:
                    pass
            _ws_room_names: list[str] = []
            if len(_ws_active_ids) == 1:
                _ws_room_names = [_ws_active_names[0]] if _ws_active_names else [_ws_active_ids[0].replace("_", " ").title()]
            _apply_booking_cta_marker(
                response,
                hotel_code=_ws_hotel_code,
                lead_active=bool(base_metadata.get("booking_lead_active")),
                link_clicked=bool(base_metadata.get("booking_link_clicked")),
                room_property_names=_ws_room_names,
                active_property_count=len(_ws_active_ids),
                user_message=str(getattr(chat_req, "message", "") or ""),
                # Wired here as well as on the HTTP route: the widget talks over
                # this socket, so a flag handled only in api/routes/chat.py never
                # reaches a real guest.
                # THE LEDGER DECIDES, NOT THE BROWSER — see the HTTP route. The
                # widget's own flag is lost on a reload or in a second tab while
                # the booking still exists on the server, and the guest was then
                # asked to submit details they had already sent.
                details_submitted=(
                    _cta_submitted(_ws_cta_ctx)
                    if (_ws_cta_ctx := await context_manager.get_context(chat_req.session_id, db_session=None)) is not None
                    and _cta_team_booking(_ws_hotel_code)
                    else bool(
                        (getattr(chat_req, "metadata", None) or {}).get("booking_details_submitted")
                    )
                ),
            )
        except Exception:
            pass

        # 5. Attach smart images — mirrors HTTP path (api/routes/chat.py).
        #    Must run after display_message so _is_form_visual_turn() check
        #    inside _attach_smart_images sees the final response state.
        # 5a. Topic images FIRST, inline. This one makes no LLM call (cached DB
        #     read), so it is cheap enough to run before the frame is sent —
        #     which it must be, because the deferred step below fires only
        #     AFTER the reply has gone out and so can never add images to it.
        try:
            from api.routes.chat import _attach_topic_images
            await _attach_topic_images(
                response=response,
                request=chat_req,
                hotel_code=_ws_hotel_code,
            )
        except Exception as _topic_exc:
            logger.warning("[WS] _attach_topic_images failed (non-fatal): %r", _topic_exc)

        # 5b. Events get the same one-tap path as rooms: the answer, then a
        #     button that opens the enquiry form. Must be wired HERE as well as
        #     in the HTTP route — the widget talks over this socket, so anything
        #     attached only in api/routes/chat.py never reaches a real guest.
        try:
            from api.routes.chat import _attach_event_cta
            from core.context_manager import context_manager as _evt_ctx_mgr
            await _attach_event_cta(
                response=response,
                hotel_code=_ws_hotel_code,
                context=await _evt_ctx_mgr.get_context(chat_req.session_id, db_session=None),
            )
        except Exception as _evt_exc:
            logger.warning("[WS] _attach_event_cta failed (non-fatal): %r", _evt_exc)

        # Every button for this turn is decided by now — drop any sentence
        # pointing at one that is not on screen. The widget talks over this
        # socket, so a cleanup wired only in api/routes/chat.py never reaches
        # a real guest.
        try:
            from api.routes.chat import _strip_unavailable_button_mentions
            _strip_unavailable_button_mentions(response)
        except Exception as _btn_exc:
            logger.warning("[WS] button-mention cleanup failed (non-fatal): %r", _btn_exc)

        try:
            from config.settings import settings as _ws_settings
            from api.routes.chat import (
                _attach_smart_images,
                _BACKGROUND_ENRICH_TASKS,
                _reap_background_enrich_task,
            )
            _ws_img_coro = _attach_smart_images(
                response=response,
                request=chat_req,
                hotel_code=_ws_hotel_code,
                trace_id="",
                turn_trace_id=turn_trace_id,
            )
            if bool(getattr(_ws_settings, "chat_defer_display_enrichment", False)):
                # Off the critical path: attach smart images in the background so
                # the WS reply is sent now (image selection can add ~hundreds of ms).
                _ws_img_task = asyncio.create_task(_ws_img_coro)
                _BACKGROUND_ENRICH_TASKS.add(_ws_img_task)
                _ws_img_task.add_done_callback(_reap_background_enrich_task)
            else:
                await _ws_img_coro
        except Exception as _img_exc:
            logger.warning("[WS] _attach_smart_images failed (non-fatal): %r", _img_exc)

        # 5b. Attach menu PDF download card — mirrors HTTP path. Deterministic +
        #     cheap (no LLM); fires on an explicit menu-file request, or for an
        #     admin-managed hotel when the router answered from a menu topic.
        try:
            from api.routes.chat import _attach_menu_document
            await _attach_menu_document(
                response=response,
                request=chat_req,
                hotel_code=_ws_hotel_code,
            )
        except Exception as _menu_exc:
            logger.warning("[WS] _attach_menu_document failed (non-fatal): %r", _menu_exc)

    finally:
        if _ws_scope_token is not None:
            db_config_service.reset_hotel_context(_ws_scope_token)
        if _ws_pin_token is not None:
            reset_pinned_model(_ws_pin_token)
        # MUST reset: one WS connection handles every turn in the same asyncio
        # task, so a leaked context would attribute the next turn to this hotel.
        turn_diagnostics_service.clear_turn(_ws_turn_token)

    # Observability: WS path lacks the HTTP route's audit/timing/diagnostics
    # logging, which makes form-trigger bugs invisible. Emit a single compact
    # summary so flow.log captures the post-orchestration state — what
    # service was targeted, what's in pending_data, whether form fields were
    # attached, etc. Mirror the most useful fields the audit log would carry.
    try:
        _resp_meta = response.metadata if isinstance(response.metadata, dict) else {}
        _entities = _resp_meta.get("entities") if isinstance(_resp_meta.get("entities"), dict) else {}
        _pending = _entities.get("pending_data") if isinstance(_entities.get("pending_data"), dict) else {}
        _form_fields = _resp_meta.get("form_fields") or []
        logger.info(
            "[WS] post-orchestration session=%s msg=%r state=%s service=%s action=%s pending_action=%s "
            "room_type=%r form_trigger=%s form_fields=%d form_service_id=%s",
            chat_req.session_id,
            str(chat_req.message or "")[:60],
            getattr(response.state, "value", response.state),
            _entities.get("target_service_id"),
            _entities.get("orchestration_action"),
            _entities.get("pending_action"),
            _pending.get("room_type"),
            _resp_meta.get("form_trigger"),
            len(_form_fields) if isinstance(_form_fields, list) else 0,
            _resp_meta.get("form_service_id"),
        )
    except Exception:
        pass

    # DB log: conversation turn → new_bot_log_messages. The WS path does not run
    # the HTTP route's conversation_audit/turn-end logging, so without this the
    # messages table stays empty for WebSocket chats. Best-effort.
    try:
        from services.db_log_sink import db_log_sink
        _audit_meta = response.metadata if isinstance(response.metadata, dict) else {}
        _phase = str(
            _audit_meta.get("selected_phase_id")
            or _audit_meta.get("current_phase_id")
            or _audit_meta.get("phase_gate_current_phase_id")
            or _audit_meta.get("phase")
            or ""
        )
        db_log_sink.emit(
            "conversation_audit",
            {
                "session_id": str(chat_req.session_id or ""),
                "hotel_code": str(chat_req.hotel_code or ""),
                "turn_trace_id": turn_trace_id,
                "component": "websocket_chat",
                "intent": str(_audit_meta.get("classified_intent") or ""),
                "routing": {"routing_path": str(_audit_meta.get("routing_path") or "")},
            },
            user_message=str(chat_req.message or ""),
            bot_message=str(getattr(response, "message", "") or ""),
            phase_id=_phase,
            service_id=str(_audit_meta.get("target_service_id") or getattr(response, "service_id", "") or ""),
        )
    except Exception:
        pass

    # Serialise to dict — ChatResponse is a pydantic model
    result = response.model_dump(mode="json")

    # CAPTURE THE DISPLAY *HERE*, in the same breath as the reply the guest gets.
    # _attach_smart_images runs in the BACKGROUND (chat_defer_display_enrichment)
    # and overwrites response.metadata["images"] with its own picks some time
    # after this line. Anything read later is that job's leftovers, not the turn:
    # a Jaipur intro that showed 10 facade photos was stored as 12 room photos,
    # and a phase decline that showed NO photo was stored with one. This call is
    # synchronous, so nothing can run between the serialise above and the read
    # here — the row and the reply are the same turn by construction. The write
    # itself is awaited further down; only the reading has to happen now.
    _ws_disp_payload: dict = {}
    try:
        from api.routes.chat import _build_turn_display_payload

        _ws_disp_payload = _build_turn_display_payload(response)
    except Exception as _disp_cap_exc:
        logger.debug("[WS] display capture failed (non-fatal): %r", _disp_cap_exc)

    # Enrich response to match production payload contract
    result["success"] = True
    if conversation_id and not result.get("conversation_id"):
        result["conversation_id"] = conversation_id

    # Ensure turn_trace_id surfaces in the response metadata so the client can
    # pass it back to /api/chat/suggestions for chip job lookup.
    if isinstance(result.get("metadata"), dict):
        result["metadata"].setdefault("turn_trace_id", turn_trace_id)
    else:
        result["metadata"] = {"turn_trace_id": turn_trace_id}

    # Schedule suggestion chips in the background (same job the HTTP handler runs).
    # Build a minimal OrchestrationDecision so schedule_post_response_chip_job
    # has enough context to generate chips without needing the full orchestration.
    try:
        from services.llm_orchestration_service import llm_orchestration_service
        from core.context_manager import context_manager
        bot_msg = str(result.get("message") or result.get("display_message") or "").strip()
        # The orchestration pipeline (run_orchestration) already schedules a
        # richly-grounded chip job for THIS turn_trace_id — with book-route
        # context, target/in-phase service KB, and router book ids. If we blindly
        # reschedule here with a minimal, grounding-starved decision we CANCEL
        # that rich job and replace it with a weaker one, which both lowers chip
        # quality and intermittently yields zero grounded candidates ("chips
        # sometimes don't show"). So only schedule from the WS wrapper as a
        # FALLBACK — when orchestration did NOT already schedule one for this
        # turn (early-return paths, errors, cache replays).
        _existing_chip_job = llm_orchestration_service.get_post_response_chip_job_status(
            session_id=str(chat_req.session_id or ""),
            assistant_turn_id=turn_trace_id,
        )
        _chip_job_already_scheduled = bool(_existing_chip_job.get("exists"))
        logger.info(
            "[WS] chip scheduling: bot_msg=%s session_id=%s orchestration_job_exists=%s",
            bool(bot_msg), chat_req.session_id, _chip_job_already_scheduled,
        )
        if bot_msg and chat_req.session_id and not _chip_job_already_scheduled:
            hotel_code_resolved = str(chat_req.hotel_code or "DEFAULT").strip()
            ctx = await context_manager.get_or_create_context(
                session_id=chat_req.session_id,
                hotel_code=hotel_code_resolved,
            )
            minimal_decision = OrchestrationDecision(
                action="respond_only",
                response_text=bot_msg,
                suggested_actions=list(result.get("suggested_actions") or []),
            )
            phase_meta = str(
                (result.get("metadata") or {}).get("selected_phase_id")
                or chat_req.metadata.get("phase")
                or "pre_booking"
            ).strip()
            llm_orchestration_service.schedule_post_response_chip_job(
                session_id=chat_req.session_id,
                assistant_turn_id=turn_trace_id,
                user_message=str(chat_req.message or "").strip(),
                context=ctx,
                decision=minimal_decision,
                selected_phase_id=phase_meta,
                selected_phase_name=phase_meta.replace("_", " ").title(),
            )
    except Exception:
        pass  # chips are non-critical

    # Write the display captured at serialise time. Deliberately does NOT re-read
    # `response` — by now the background image job may have replaced its images.
    try:
        from core.context_manager import context_manager as _cm_disp

        if _ws_disp_payload and chat_req.session_id:
            await _cm_disp.set_turn_display_payload(
                chat_req.session_id,
                _ws_disp_payload,
                conversation_uid=str(
                    (base_metadata or {}).get("conversation_id")
                    or (base_metadata or {}).get("conversationId")
                    or ""
                ).strip(),
                db_session=None,
            )
    except Exception as _disp_exc:
        logger.debug("[WS] set_turn_display_payload failed (non-fatal): %r", _disp_exc)

    logger.info(
        "[WS] Response ready  session_id=%s  state=%s  llm=%s  confidence=%s",
        result.get("session_id"), result.get("state"),
        result.get("service_llm_label"), result.get("service_llm_confidence"),
    )
    return result


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------

@router.websocket("/ws/chat")
async def websocket_chat_endpoint(websocket: WebSocket) -> None:
    """
    WebSocket endpoint for real-time chat.

    Query params (all optional, can also be sent in the first message frame):
      ?session_id=…   &hotel_code=…   &channel=…   &widget_key=…

    `widget_key` identifies a CUSTOMER EMBED (the loader puts it in the iframe
    URL; the harness has none). It is used only to look the deployment's pinned
    model up server-side — the model is never taken from the URL — so QA
    switching the harness dropdown cannot move a customer's live bot.
    """
    # Read connection-time params from query string
    params = dict(websocket.query_params)
    session_id: str = str(params.get("session_id") or "").strip()
    default_hotel_code: str = str(params.get("hotel_code") or "DEFAULT").strip()
    default_channel: str = str(params.get("channel") or "web_widget").strip()
    widget_key: str = str(params.get("widget_key") or "").strip()

    # Accept the connection (session_id may be empty until first message)
    await websocket.accept()
    logger.info("[WS] accepted  session_id=%s  hotel=%s", session_id or "(none)", default_hotel_code)

    if session_id:
        # Register with known session_id immediately
        old = manager._connections.get(session_id)
        if old and old is not websocket:
            try:
                await old.close(code=1001)
            except Exception:
                pass
        manager._connections[session_id] = websocket

    # Send connected confirmation. It carries the same health verdict the pong
    # does so the widget starts out honest — otherwise it would show "Online"
    # for a full ping interval before the first beat could correct it.
    _connect_health = _backend_health()
    await manager.send(websocket, {
        "type": "connected",
        "session_id": session_id,
        "healthy": bool(_connect_health["ok"]),
        "detail": str(_connect_health["detail"]),
    })

    # ── Agent-active restore (currentResponder equivalent) ────────────────
    # If a human agent is currently handling this session, immediately
    # push a system_handover frame so the widget re-enters agent mode on
    # page reload / reconnect — without any HTTP round-trip.
    # This is purely additive: only sent when session_id is already known
    # at connect time (i.e. the guest is resuming an existing session).
    if session_id:
        try:
            from services.agent_room_service import agent_room_service as _arm
            from datetime import datetime as _dt, timezone as _tz
            if _arm.has_active_agent(session_id):
                _room = _arm.get_room(session_id)
                _agent_name = _room.agent_name if _room else "Agent"
                await manager.send(websocket, {
                    "type": "system_handover",
                    "to_responder": "AGENT",
                    "agent_name": _agent_name,
                    "message": f"{_agent_name} is currently assisting you.",
                    "timestamp": _dt.now(_tz.utc).isoformat(),
                })
        except Exception:
            pass  # non-fatal: widget will learn agent is active on first message

    try:
        while True:
            raw = await websocket.receive_text()
            frame = _safe_parse(raw)

            if frame is None:
                await manager.send(websocket, {
                    "type": "error",
                    "detail": "Invalid JSON frame",
                })
                continue

            msg_type = str(frame.get("type") or "message").strip().lower()

            # ── ping / pong keep-alive ────────────────────────────────────
            if msg_type == "ping":
                # The pong doubles as the health beat: an open socket is not
                # evidence the bot can answer, so tell the widget what we
                # actually know rather than letting it assume "Online".
                _health = _backend_health()
                await manager.send(websocket, {
                    "type": "pong",
                    "healthy": bool(_health["ok"]),
                    "detail": str(_health["detail"]),
                })
                continue

            # ── chat message ──────────────────────────────────────────────
            if msg_type == "message":
                # Allow session_id to be sent in frame (first message before known)
                frame_session = str(frame.get("session_id") or "").strip()
                if frame_session and not session_id:
                    session_id = frame_session
                    manager._connections[session_id] = websocket
                elif frame_session:
                    session_id = frame_session  # allow override

                # Fill defaults from connection-time params
                if not frame.get("hotel_code"):
                    frame["hotel_code"] = default_hotel_code
                if not frame.get("channel"):
                    frame["channel"] = default_channel
                if not frame.get("session_id") and session_id:
                    frame["session_id"] = session_id
                # The deployment identity comes from the HANDSHAKE, never the
                # frame: overwrite (not setdefault) so a client-authored frame
                # can't claim a different deployment's pinned model mid-socket.
                frame["widget_key"] = widget_key

                # ── Idempotency: dedupe a retried message (same message_id) ────
                # The widget attaches a client message_id and may resend a message
                # over HTTP if the WS delivery-ack times out. Claim the key so a
                # message that actually arrived here is never processed twice.
                _msg_id = str(
                    (frame.get("metadata") or {}).get("message_id")
                    or frame.get("message_id")
                    or ""
                ).strip()
                _dedup_key = (session_id, _msg_id) if (session_id and _msg_id) else None
                _dedup_owned = False
                if _dedup_key:
                    _verdict, _cached = await message_dedup.claim(_dedup_key)
                    if _verdict == "owner":
                        _dedup_owned = True
                    else:  # duplicate
                        if _cached is not None:
                            await manager.send(websocket, {
                                "type": "response",
                                "data": _cached,
                                "message_id": _msg_id,
                                "dedup_hit": True,
                            })
                        # else: still in-flight elsewhere — the owner will reply.
                        continue

                # Immediately acknowledge with typing indicator (echo id so the
                # client can clear its delivery-ack timer for this exact message).
                await manager.send(websocket, {"type": "typing", "message_id": _msg_id})

                # ── Agent monitoring: register session + forward guest msg ──
                _msg_hotel = str(frame.get("hotel_code") or default_hotel_code or "").strip()
                if session_id and _msg_hotel and _msg_hotel.upper() != "DEFAULT":
                    try:
                        from services.agent_room_service import agent_room_service as _arm_ws
                        from datetime import datetime as _dt_ws, timezone as _tz_ws
                        if not _arm_ws.has_active_agent(session_id):
                            _fmeta = dict(frame.get("metadata") or {})
                            _ginfo = {k: v for k, v in {
                                "guest_name": str(_fmeta.get("guest_name") or "").strip() or None,
                                "room_number": str(_fmeta.get("room_number") or "").strip() or None,
                                "hotel_code": _msg_hotel,
                            }.items() if v}
                            _gs, _is_new = _arm_ws.register_guest_session(
                                session_id, _msg_hotel, websocket, _ginfo,
                            )
                            # Start the kebo agent-handoff bridge poller for this
                            # session (idempotent) so kebo-dashboard agent replies
                            # are pushed to this guest socket. KEBO HOTELS ONLY —
                            # KePSLA/other tenants keep our native /ws/agent path.
                            try:
                                from services.kebo_agent_bridge import kebo_agent_bridge as _kab_ws
                                from services.customer_details_service import customer_details_service as _cds_ws
                                if _cds_ws.is_kebo_hotel(_msg_hotel):
                                    _kab_ws.ensure_poller(session_id, websocket)
                            except Exception:
                                pass
                            if _is_new:
                                await _arm_ws.broadcast_to_agents(_msg_hotel, {
                                    "type": "session_started",
                                    "session_id": session_id,
                                    "guest_info": _ginfo,
                                    "started_at": _gs.started_at.isoformat(),
                                })
                            # Forward guest message to monitoring agents
                            await _arm_ws.forward_to_monitors(session_id, {
                                "type": "session_message",
                                "session_id": session_id,
                                "role": "guest",
                                "text": str(frame.get("message") or "").strip(),
                                "timestamp": _dt_ws.now(_tz_ws.utc).isoformat(),
                            })
                    except Exception:
                        pass  # monitoring is non-critical
                # ─────────────────────────────────────────────────────────────

                # ── THE GUEST'S CHAT ROW MUST EXIST BEFORE THE TICKET DOES ──
                # A ticket raised during this turn has to carry the guest row's
                # KEBO_ENGAGE_SESSIONS.id as `message_id`, or the dashboard has
                # nothing to anchor it to and never draws it inline in the
                # conversation on the chat-history page — it only reaches the
                # tickets list. Until now this row was written AFTER the turn
                # (see the mirror block below), so at ticket time it did not
                # exist; what travelled to the ticket API instead was the
                # widget's client-side UUID from metadata.message_id, which is
                # not an integer and landed in the int(11) column as 0/NULL.
                #
                # So: write the guest row FIRST, take its id, and overwrite
                # metadata.message_id with it. Everything downstream already
                # carries metadata.message_id into the ticket payload
                # (chat_service canonical_map -> integration -> _build_ticket_payload),
                # so nothing else needs to change.
                #
                # The client UUID is still needed for dedup/typing acks, so it is
                # read into _msg_id ABOVE this point and is unaffected.
                _kt_user_row_written = False
                try:
                    from services.customer_details_service import customer_details_service as _cds_pre
                    _pre_widget, _pre_group = _cds_pre.resolve_widget_group(_msg_hotel)
                    _pre_agent_active = False
                    try:
                        from services.agent_room_service import agent_room_service as _arm_pre
                        _pre_agent_active = _arm_pre.has_active_agent(session_id)
                    except Exception:
                        pass
                    _pre_user_text = str(frame.get("message") or "").strip()
                    # Kebo hotels only (_pre_widget is empty for everyone else),
                    # and never while a human agent owns the conversation.
                    if _pre_widget and _pre_user_text and not _pre_agent_active:
                        from services.engage_session_service import engage_session_service as _ess_pre
                        _pre_meta = dict(frame.get("metadata") or {})
                        _pre_conv = str(
                            frame.get("conversation_id") or frame.get("conversationId")
                            or _pre_meta.get("conversation_id") or _pre_meta.get("conversationId") or ""
                        ).strip() or session_id
                        _pre_entity = str(
                            _pre_meta.get("entity_id") or _pre_meta.get("entityId") or ""
                        ).strip()
                        # Fire-and-forget: we do NOT wait for the row id here.
                        # The write takes ~1.5s against the kebo DB and the guest
                        # is waiting on this turn, so blocking for it would put
                        # that straight onto every reply. We only need the row to
                        # EXIST before the ticket payload is built, which happens
                        # several seconds later (after the router and orchestration
                        # LLMs). ticketing_service resolves the id from the row at
                        # that point — see _resolve_engage_message_id.
                        _ess_pre.write_turn_bg(
                            session_id=session_id, sender="USER", message=_pre_user_text,
                            entity_id=_pre_entity, group_id=_pre_group, widget_id=_pre_widget,
                            conversation_id=_pre_conv, message_type="text",
                        )
                        _kt_user_row_written = True
                except Exception:
                    # Never let history/ticket-linking break a chat turn.
                    logger.debug("[WS] pre-turn guest row write skipped", exc_info=True)

                try:
                    response_data = await _process_chat(frame)
                    # The bot answered — clear any standing run of failures so
                    # the indicator recovers as soon as service does.
                    _mark_turn_ok()
                    if _dedup_owned and _dedup_key:
                        await message_dedup.complete(_dedup_key, response_data)
                    # Route to the session's live socket, not the captured one:
                    # the guest may have reconnected during orchestration, in
                    # which case `websocket` is closed and the answer would be
                    # lost. send_to_session lands it on whichever socket is live
                    # now; the answer is also cached above so an HTTP retry with
                    # the same message_id can recover it if no socket is live.
                    await manager.send_to_session(session_id, {
                        "type": "response",
                        "data": response_data,
                        "message_id": _msg_id,
                    }, fallback=websocket)
                    # Forward bot response to monitoring agents (non-critical)
                    if session_id and _msg_hotel and _msg_hotel.upper() != "DEFAULT":
                        try:
                            _bot_txt = str(
                                response_data.get("message") or
                                response_data.get("display_message") or ""
                            ).strip()
                            _resp_meta = response_data.get("metadata") or {}
                            if _bot_txt and not _resp_meta.get("agent_handling"):
                                from services.agent_room_service import agent_room_service as _arm2
                                from datetime import datetime as _dt2, timezone as _tz2
                                await _arm2.forward_to_monitors(session_id, {
                                    "type": "session_message",
                                    "session_id": session_id,
                                    "role": "bot",
                                    "text": _bot_txt,
                                    "timestamp": _dt2.now(_tz2.utc).isoformat(),
                                })
                        except Exception:
                            pass
                    # Mirror this WS turn into KEBO_ENGAGE_SESSIONS (kebo hotels
                    # ONLY) so the kebo dashboard shows the full conversation —
                    # not just form submits. Conversational turns travel over WS;
                    # the HTTP /api/chat/message hook never sees them.
                    try:
                        from services.customer_details_service import customer_details_service as _cds_kt
                        _kt_widget, _kt_group = _cds_kt.resolve_widget_group(_msg_hotel)
                        _kt_resp_meta = response_data.get("metadata") or {}
                        if _kt_widget and not _kt_resp_meta.get("agent_handling"):
                            from services.engage_session_service import engage_session_service as _ess_kt
                            _kt_fmeta = dict(frame.get("metadata") or {})
                            # The widget sends conversation_id at the TOP LEVEL of the
                            # WS frame (see useWebSocketChat.sendMessage), not inside
                            # metadata. Read the top-level value first so engage rows
                            # carry the real per-thread conversation_id; only fall back
                            # to session_id when no conversation_id was supplied at all.
                            _kt_conv = str(
                                frame.get("conversation_id") or frame.get("conversationId")
                                or _kt_fmeta.get("conversation_id") or _kt_fmeta.get("conversationId") or ""
                            ).strip() or session_id
                            _kt_entity = str(
                                _kt_fmeta.get("entity_id") or _kt_fmeta.get("entityId") or ""
                            ).strip()
                            _kt_user = str(frame.get("message") or "").strip()
                            _kt_bot = str(
                                response_data.get("message") or response_data.get("display_message") or ""
                            ).strip()
                            # Skipped when the pre-turn block above already wrote
                            # this guest row to get its id for the ticket.
                            if _kt_user and not _kt_user_row_written:
                                _ess_kt.write_turn_bg(
                                    session_id=session_id, sender="USER", message=_kt_user,
                                    entity_id=_kt_entity, group_id=_kt_group, widget_id=_kt_widget,
                                    conversation_id=_kt_conv, message_type="text",
                                )
                            if _kt_bot:
                                _ess_kt.write_turn_bg(
                                    session_id=session_id, sender="BOT", message=_kt_bot,
                                    entity_id=_kt_entity, group_id=_kt_group, widget_id=_kt_widget,
                                    conversation_id=_kt_conv, message_type="text",
                                )
                    except Exception:
                        pass
                except ValueError as exc:
                    if _dedup_owned and _dedup_key:
                        await message_dedup.release_if_pending(_dedup_key)
                    await manager.send_to_session(session_id, {
                        "type": "error",
                        "detail": str(exc),
                        "message_id": _msg_id,
                    }, fallback=websocket)
                except Exception as exc:
                    if _dedup_owned and _dedup_key:
                        await message_dedup.release_if_pending(_dedup_key)
                    logger.exception("[WS] process_chat failed session_id=%s", session_id)
                    # A turn that blew up is the most direct evidence there is
                    # that the bot cannot answer: degrade the indicator now
                    # rather than waiting on the next scheduled probe.
                    _mark_backend_unhealthy("turn_failed")
                    await manager.send_to_session(session_id, {
                        "type": "error",
                        "detail": "Internal server error — please try again",
                        "message_id": _msg_id,
                    }, fallback=websocket)
                continue

            # ── unknown frame type ────────────────────────────────────────
            await manager.send(websocket, {
                "type": "error",
                "detail": f"Unknown frame type: {msg_type!r}",
            })

    except WebSocketDisconnect:
        logger.info("[WS] client disconnected session_id=%s", session_id)
    except Exception as exc:
        logger.warning("[WS] unexpected error session_id=%s: %s", session_id, exc)
    finally:
        if session_id:
            manager.disconnect(session_id, websocket)
            # Stop the kebo agent-handoff bridge poller for this session.
            try:
                from services.kebo_agent_bridge import kebo_agent_bridge as _kab_fin
                _kab_fin.stop(session_id)
            except Exception:
                pass
        # Unregister from agent monitoring and notify agents the session ended
        try:
            from services.agent_room_service import agent_room_service as _arm_fin
            _gs_fin = _arm_fin.get_guest_session(session_id) if session_id else None
            if _gs_fin:
                _arm_fin.unregister_guest_session(session_id)
                await _arm_fin.broadcast_to_agents(_gs_fin.hotel_code, {
                    "type": "session_ended",
                    "session_id": session_id,
                })
        except Exception:
            pass
