"""
Kebo agent-handoff bridge.

The kebo (Sarovar) agent dashboard does NOT connect to our /ws/agent. When an
agent takes over a chat there, the dashboard writes the agent's replies to
GHN_PROD_BAK.KEBO_ENGAGE_SESSIONS (sender='agent') and transfer markers
(sender='system', message_type='handover') — all under OUR session_id. Our guest
widget talks to our own /ws/chat, so those agent messages never reach the guest
and our bot keeps answering.

This bridge polls KES per connected guest session, pushes the kebo agent's
messages to the guest's WebSocket using the SAME frames the widget already
renders ('agent_message' / 'system_handover'), and tracks whether a kebo agent
is currently active so the bot can stay muted.

Safe by construction: per-session poller cancelled on disconnect; all sends and
DB reads are best-effort and never raise into the chat path.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import WebSocket
from sqlalchemy import text

logger = logging.getLogger(__name__)

_POLL_SECONDS = 3.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _enabled() -> bool:
    try:
        from config.settings import settings
        return bool(getattr(settings, "kebo_agent_bridge_enabled", True))
    except Exception:
        return True


class KeboAgentBridge:
    def __init__(self) -> None:
        self._active: set[str] = set()          # sessions a kebo agent is handling
        self._last_id: dict[str, int] = {}       # last KES id pushed per session
        self._pollers: dict[str, asyncio.Task] = {}
        self._ws: dict[str, WebSocket] = {}      # current guest socket per session

    # -- state -----------------------------------------------------------
    def is_agent_active(self, session_id: str) -> bool:
        return session_id in self._active

    # -- lifecycle -------------------------------------------------------
    def ensure_poller(self, session_id: str, websocket: WebSocket) -> None:
        if not session_id or not _enabled():
            return
        existing = self._pollers.get(session_id)
        # Already polling for THIS socket — nothing to do.
        if existing and not existing.done() and self._ws.get(session_id) is websocket:
            return
        # Socket changed (reconnect / new tab) — restart against the new socket.
        if existing and not existing.done():
            existing.cancel()
        self._ws[session_id] = websocket
        try:
            self._pollers[session_id] = asyncio.create_task(
                self._poll_loop(session_id, websocket)
            )
        except RuntimeError:
            pass  # no running loop (tests)

    def stop(self, session_id: str) -> None:
        task = self._pollers.pop(session_id, None)
        if task and not task.done():
            task.cancel()
        self._ws.pop(session_id, None)
        self._active.discard(session_id)
        self._last_id.pop(session_id, None)

    # -- guest -> kebo (so the agent sees the guest's reply) -------------
    def relay_guest_message(self, *, session_id: str, hotel_code: str, text_msg: str) -> None:
        """Write the guest's message to KES (sender=USER) while a kebo agent is
        handling the session, so it appears in the agent's dashboard."""
        try:
            from services.customer_details_service import customer_details_service
            from services.engage_session_service import engage_session_service

            widget_id, group_id = customer_details_service.resolve_widget_group(hotel_code)
            engage_session_service.write_turn_bg(
                session_id=session_id,
                sender="USER",
                message=text_msg,
                widget_id=widget_id,
                group_id=group_id,
                message_type="text",
            )
        except Exception:
            logger.debug("[kebo_bridge] relay_guest_message skipped", exc_info=True)

    # -- poller ----------------------------------------------------------
    async def _poll_loop(self, session_id: str, websocket: WebSocket) -> None:
        from models.database import AsyncSessionLocal

        try:
            # Start from the current tail so we only push NEW agent activity.
            async with AsyncSessionLocal() as session:
                cur = (await session.execute(
                    text("SELECT COALESCE(MAX(id),0) FROM KEBO_ENGAGE_SESSIONS WHERE session_id=:s"),
                    {"s": session_id},
                )).scalar()
                self._last_id[session_id] = int(cur or 0)

            while True:
                await asyncio.sleep(_POLL_SECONDS)
                last = int(self._last_id.get(session_id, 0))
                async with AsyncSessionLocal() as session:
                    rows = (await session.execute(
                        text(
                            "SELECT id, sender, message, message_type, to_responder "
                            "FROM KEBO_ENGAGE_SESSIONS "
                            "WHERE session_id=:s AND id>:lid "
                            "AND LOWER(COALESCE(sender,'')) IN ('agent','system') "
                            "ORDER BY id ASC LIMIT 50"
                        ),
                        {"s": session_id, "lid": last},
                    )).mappings().fetchall()

                for r in rows:
                    rid = int(r["id"])
                    if rid > self._last_id.get(session_id, 0):
                        self._last_id[session_id] = rid
                    sender = str(r["sender"] or "").lower()
                    msg = str(r["message"] or "")
                    to_resp = str(r["to_responder"] or "").upper()
                    low = msg.lower()

                    if sender == "agent":
                        self._active.add(session_id)
                        await self._push(websocket, {
                            "type": "agent_message",
                            "text": msg,
                            "agent_name": "Agent",
                            "timestamp": _now(),
                        })
                    elif sender == "system":
                        # Handover markers toggle the muted/active state.
                        if to_resp == "AGENT" or ("transferred to" in low and "digital assistant" not in low):
                            self._active.add(session_id)
                            await self._push(websocket, {
                                "type": "system_handover",
                                "to_responder": "AGENT",
                                "message": msg or "You're now connected with an agent.",
                                "timestamp": _now(),
                            })
                        elif to_resp == "BOT" or "digital assistant" in low:
                            self._active.discard(session_id)
                            await self._push(websocket, {
                                "type": "system_handover",
                                "to_responder": "BOT",
                                "message": msg or "You're back with the assistant.",
                                "timestamp": _now(),
                            })
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.warning("[kebo_bridge] poll loop ended session=%s: %r", session_id, exc)
            self._active.discard(session_id)

    async def _push(self, websocket: WebSocket, payload: dict[str, Any]) -> None:
        try:
            await websocket.send_text(json.dumps(payload, default=str))
        except Exception:
            pass


kebo_agent_bridge = KeboAgentBridge()
