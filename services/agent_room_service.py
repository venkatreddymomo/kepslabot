"""
Agent Room Service

Manages live agent-guest chat rooms for the agent handoff feature.
When a human agent accepts an escalation, an AgentRoom is created that
bridges the guest's WebSocket (/ws/chat) with the agent's WebSocket (/ws/agent).

Production features:
  - Offline fallback timer: if no agent accepts within N seconds, bot auto-resumes
  - Multi-agent observer mode: secondary agents can watch/participate in a room
  - Audit logging: all human turns written to conversation context for bot continuity
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)

DEFAULT_FALLBACK_TIMEOUT_SECONDS = 300  # 5 minutes


@dataclass
class ObserverEntry:
    """A secondary agent observing (and optionally participating in) a room."""
    agent_ws: WebSocket
    agent_id: str
    agent_name: str
    joined_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class GuestSession:
    """An active bot-handled chat session visible to agents for monitoring."""
    session_id: str
    hotel_code: str
    guest_ws: WebSocket
    guest_info: dict
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    message_count: int = 0
    monitors: list[WebSocket] = field(default_factory=list)


@dataclass
class PendingEscalation:
    """A conversation waiting for a human agent to accept."""
    session_id: str
    hotel_code: str
    guest_ws: WebSocket
    preview: str          # last guest message (shown in agent inbox)
    guest_info: dict      # room_number, guest_name, phone etc.
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class AgentRoom:
    """An active agent-guest chat room."""
    session_id: str
    hotel_code: str
    guest_ws: WebSocket
    agent_ws: WebSocket     # primary agent
    agent_id: str
    agent_name: str
    guest_info: dict
    accepted_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Secondary agents watching / participating
    observers: list[ObserverEntry] = field(default_factory=list)


class AgentRoomService:
    """
    Manages pending escalations and active agent-guest rooms.

    Thread-safety: FastAPI runs on asyncio; all mutations happen in the
    event loop so no locks are needed for dict operations.
    """

    def __init__(self) -> None:
        # session_id → AgentRoom
        self._rooms: dict[str, AgentRoom] = {}
        # session_id → PendingEscalation
        self._pending: dict[str, PendingEscalation] = {}
        # hotel_code → list[WebSocket]  (agent connections watching this hotel)
        self._agent_connections: dict[str, list[WebSocket]] = {}
        # session_id → GuestSession  (all active bot-handled sessions)
        self._guest_sessions: dict[str, GuestSession] = {}

    # -- Pending escalations --------------------------------------------

    def add_pending(
        self,
        *,
        session_id: str,
        hotel_code: str,
        guest_ws: WebSocket,
        preview: str,
        guest_info: dict | None = None,
    ) -> PendingEscalation:
        esc = PendingEscalation(
            session_id=session_id,
            hotel_code=hotel_code,
            guest_ws=guest_ws,
            preview=preview,
            guest_info=guest_info or {},
        )
        self._pending[session_id] = esc
        logger.info("[AgentRoom] pending escalation added session=%s hotel=%s", session_id, hotel_code)
        return esc

    def get_pending(self, session_id: str) -> PendingEscalation | None:
        return self._pending.get(session_id)

    def get_all_pending_for_hotel(self, hotel_code: str) -> list[PendingEscalation]:
        return [p for p in self._pending.values() if p.hotel_code == hotel_code]

    def get_all_active_rooms_for_hotel(self, hotel_code: str) -> list[AgentRoom]:
        """Return all active (accepted) rooms for a given hotel."""
        return [r for r in self._rooms.values() if r.hotel_code == hotel_code]

    # -- Guest session tracking (all bot-handled sessions) -----------------

    def register_guest_session(
        self,
        session_id: str,
        hotel_code: str,
        guest_ws: WebSocket,
        guest_info: dict | None = None,
    ) -> tuple[GuestSession, bool]:
        """Upsert a bot-handled guest session. Returns (session, is_new)."""
        existing = self._guest_sessions.get(session_id)
        if existing:
            existing.guest_ws = guest_ws
            if guest_info:
                existing.guest_info.update({k: v for k, v in guest_info.items() if v})
            return existing, False
        session = GuestSession(
            session_id=session_id,
            hotel_code=hotel_code,
            guest_ws=guest_ws,
            guest_info=guest_info or {},
        )
        self._guest_sessions[session_id] = session
        return session, True

    def unregister_guest_session(self, session_id: str) -> GuestSession | None:
        return self._guest_sessions.pop(session_id, None)

    def get_guest_session(self, session_id: str) -> GuestSession | None:
        return self._guest_sessions.get(session_id)

    def get_all_guest_sessions_for_hotel(self, hotel_code: str) -> list[GuestSession]:
        """Return all active bot-only sessions for a given hotel (excludes agent rooms)."""
        return [
            s for s in self._guest_sessions.values()
            if s.hotel_code == hotel_code and s.session_id not in self._rooms
        ]

    def add_monitor(self, session_id: str, agent_ws: WebSocket) -> bool:
        """Add an agent WS as monitor for a bot session. Returns False if session not found."""
        session = self._guest_sessions.get(session_id)
        if not session:
            return False
        if agent_ws not in session.monitors:
            session.monitors.append(agent_ws)
        return True

    def remove_monitor_from_all(self, agent_ws: WebSocket) -> None:
        """Remove an agent WS from all session monitor lists (called on agent disconnect)."""
        for session in self._guest_sessions.values():
            session.monitors = [ws for ws in session.monitors if ws is not agent_ws]

    async def forward_to_monitors(self, session_id: str, payload: dict[str, Any]) -> None:
        """Forward a frame to all agents monitoring this session."""
        session = self._guest_sessions.get(session_id)
        if not session or not session.monitors:
            return
        session.message_count += 1
        dead: list[WebSocket] = []
        for ws in session.monitors:
            try:
                await ws.send_text(json.dumps(payload, default=str))
            except Exception:
                dead.append(ws)
        if dead:
            session.monitors = [ws for ws in session.monitors if ws not in dead]

    def takeover_session(
        self,
        session_id: str,
        hotel_code: str,
        agent_ws: WebSocket,
        agent_id: str,
        agent_name: str,
    ) -> AgentRoom | None:
        """
        Agent proactively takes over a bot session.
        Creates an AgentRoom directly without a PendingEscalation step.
        Returns None if the session is not tracked.
        """
        session = self._guest_sessions.get(session_id)
        if not session:
            return None
        room = AgentRoom(
            session_id=session_id,
            hotel_code=hotel_code,
            guest_ws=session.guest_ws,
            agent_ws=agent_ws,
            agent_id=agent_id,
            agent_name=agent_name,
            guest_info=session.guest_info,
        )
        self._rooms[session_id] = room
        logger.info(
            "[AgentRoom] agent takeover session=%s hotel=%s agent=%s",
            session_id, hotel_code, agent_name,
        )
        return room

    def remove_pending(self, session_id: str) -> None:
        self._pending.pop(session_id, None)

    # -- Active rooms ---------------------------------------------------

    def create_room(
        self,
        *,
        session_id: str,
        hotel_code: str,
        guest_ws: WebSocket,
        agent_ws: WebSocket,
        agent_id: str,
        agent_name: str,
        guest_info: dict | None = None,
    ) -> AgentRoom:
        room = AgentRoom(
            session_id=session_id,
            hotel_code=hotel_code,
            guest_ws=guest_ws,
            agent_ws=agent_ws,
            agent_id=agent_id,
            agent_name=agent_name,
            guest_info=guest_info or {},
        )
        self._rooms[session_id] = room
        self._pending.pop(session_id, None)  # no longer pending
        logger.info(
            "[AgentRoom] room created session=%s hotel=%s agent=%s",
            session_id, hotel_code, agent_name,
        )
        return room

    def has_active_agent(self, session_id: str) -> bool:
        return session_id in self._rooms

    def get_room(self, session_id: str) -> AgentRoom | None:
        return self._rooms.get(session_id)

    def close_room(self, session_id: str) -> AgentRoom | None:
        room = self._rooms.pop(session_id, None)
        if room:
            logger.info("[AgentRoom] room closed session=%s", session_id)
        return room

    # -- Observer (multi-agent) support --------------------------------

    def add_observer(
        self,
        session_id: str,
        agent_ws: WebSocket,
        agent_id: str,
        agent_name: str,
    ) -> bool:
        """Add a secondary agent as observer. Returns False if room not found."""
        room = self._rooms.get(session_id)
        if not room:
            return False
        # Avoid duplicate entries
        if any(o.agent_ws is agent_ws for o in room.observers):
            return True
        room.observers.append(
            ObserverEntry(agent_ws=agent_ws, agent_id=agent_id, agent_name=agent_name)
        )
        logger.info(
            "[AgentRoom] observer added session=%s agent=%s total_observers=%d",
            session_id, agent_name, len(room.observers),
        )
        return True

    def remove_observer(self, session_id: str, agent_ws: WebSocket) -> None:
        room = self._rooms.get(session_id)
        if room:
            room.observers = [o for o in room.observers if o.agent_ws is not agent_ws]

    def remove_agent_from_all_rooms(self, agent_ws: WebSocket) -> list[str]:
        """Called on agent disconnect. Returns session_ids of rooms where this WS was primary."""
        primary_sessions = [
            sid for sid, room in list(self._rooms.items())
            if room.agent_ws is agent_ws
        ]
        # Remove from observer lists in rooms
        for room in self._rooms.values():
            room.observers = [o for o in room.observers if o.agent_ws is not agent_ws]
        # Remove from bot session monitor lists
        self.remove_monitor_from_all(agent_ws)
        return primary_sessions

    # -- Agent connections (for broadcasting new escalations) ----------

    def register_agent(self, hotel_code: str, ws: WebSocket) -> None:
        self._agent_connections.setdefault(hotel_code, [])
        if ws not in self._agent_connections[hotel_code]:
            self._agent_connections[hotel_code].append(ws)
        logger.info(
            "[AgentRoom] agent registered hotel=%s total=%d",
            hotel_code, len(self._agent_connections[hotel_code]),
        )

    def unregister_agent(self, hotel_code: str, ws: WebSocket) -> None:
        conns = self._agent_connections.get(hotel_code, [])
        try:
            conns.remove(ws)
        except ValueError:
            pass

    def get_agent_connections(self, hotel_code: str) -> list[WebSocket]:
        return list(self._agent_connections.get(hotel_code, []))

    # -- Messaging helpers ----------------------------------------------

    async def send_to_guest(self, session_id: str, payload: dict[str, Any]) -> None:
        """Send a frame to the guest WebSocket for this session."""
        room = self._rooms.get(session_id)
        if not room:
            return
        try:
            await room.guest_ws.send_text(json.dumps(payload, default=str))
        except Exception as exc:
            logger.debug("[AgentRoom] send_to_guest failed session=%s: %s", session_id, exc)

    async def send_to_agent(self, session_id: str, payload: dict[str, Any]) -> None:
        """Send a frame to the primary agent WebSocket for this room."""
        room = self._rooms.get(session_id)
        if not room:
            return
        try:
            await room.agent_ws.send_text(json.dumps(payload, default=str))
        except Exception as exc:
            logger.debug("[AgentRoom] send_to_agent failed session=%s: %s", session_id, exc)

    async def send_to_all_agents_in_room(
        self, session_id: str, payload: dict[str, Any]
    ) -> None:
        """Send a frame to primary agent AND all observers for this room."""
        room = self._rooms.get(session_id)
        if not room:
            return
        dead_observers: list[WebSocket] = []
        # Primary agent
        try:
            await room.agent_ws.send_text(json.dumps(payload, default=str))
        except Exception as exc:
            logger.debug("[AgentRoom] send_to_primary_agent failed session=%s: %s", session_id, exc)
        # Observers
        for obs in room.observers:
            try:
                await obs.agent_ws.send_text(json.dumps(payload, default=str))
            except Exception:
                dead_observers.append(obs.agent_ws)
        if dead_observers:
            room.observers = [o for o in room.observers if o.agent_ws not in dead_observers]

    async def broadcast_to_agents(
        self, hotel_code: str, payload: dict[str, Any]
    ) -> None:
        """Broadcast a frame to all connected agents watching this hotel."""
        conns = self.get_agent_connections(hotel_code)
        dead: list[WebSocket] = []
        for ws in conns:
            try:
                await ws.send_text(json.dumps(payload, default=str))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.unregister_agent(hotel_code, ws)

    # -- Audit logging (context preservation for bot continuity) ------

    async def audit_agent_message(
        self,
        session_id: str,
        hotel_code: str,
        agent_name: str,
        text: str,
    ) -> None:
        """
        Write an agent turn to the conversation context so the bot has
        full history when it resumes after the agent resolves the session.
        """
        try:
            from core.context_manager import context_manager
            from schemas.chat import MessageRole
            ctx = await context_manager.get_context(session_id)
            if ctx:
                ctx.add_message(
                    role=MessageRole.ASSISTANT,
                    content=text,
                    metadata={
                        "source": "human_agent",
                        "agent_name": agent_name,
                        "session_id": session_id,
                    },
                )
                await context_manager.save_context(ctx)
        except Exception as exc:
            logger.debug("[AgentRoom] audit_agent_message failed session=%s: %s", session_id, exc)

    async def audit_guest_message(
        self,
        session_id: str,
        text: str,
    ) -> None:
        """
        Write a guest turn (sent while agent was active) to conversation context.
        This ensures the bot sees what the guest said during the human handoff.
        """
        try:
            from core.context_manager import context_manager
            from schemas.chat import MessageRole
            ctx = await context_manager.get_context(session_id)
            if ctx:
                ctx.add_message(
                    role=MessageRole.USER,
                    content=text,
                    metadata={"source": "guest_in_agent_mode"},
                )
                await context_manager.save_context(ctx)
        except Exception as exc:
            logger.debug("[AgentRoom] audit_guest_message failed session=%s: %s", session_id, exc)

    async def get_conversation_history(
        self, session_id: str, max_turns: int = 30
    ) -> list[dict]:
        """
        Fetch the last N conversation turns from context manager.
        Sent to an agent when they join/observe so they have full history.
        """
        try:
            from core.context_manager import context_manager
            ctx = await context_manager.get_context(session_id)
            if not ctx or not ctx.messages:
                return []
            msgs = ctx.messages[-max_turns:]
            return [
                {
                    "role": msg.role.value,
                    "content": msg.content,
                    "timestamp": (
                        msg.timestamp.isoformat()
                        if hasattr(msg.timestamp, "isoformat")
                        else str(msg.timestamp)
                    ),
                    "metadata": msg.metadata if isinstance(msg.metadata, dict) else {},
                }
                for msg in msgs
            ]
        except Exception as exc:
            logger.debug("[AgentRoom] get_conversation_history failed session=%s: %s", session_id, exc)
            return []

    # -- System notifications to guest ---------------------------------

    async def notify_guest_agent_joined(
        self, session_id: str, agent_name: str
    ) -> None:
        """Push system_handover frame to guest: bot muted, agent active."""
        await self.send_to_guest(session_id, {
            "type": "system_handover",
            "to_responder": "AGENT",
            "agent_name": agent_name,
            "message": f"{agent_name} has joined the conversation.",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    async def notify_guest_bot_resumed(self, session_id: str) -> None:
        """Push system_handover frame to guest: bot reactivated.

        Carries a `bot_followup` so the widget can render a real bot/assistant
        bubble (not just a centered system banner) — the bot greeting the guest
        back and checking whether anything else is needed.
        """
        followup_text = (
            "Glad we could help! Is everything resolved, or is there "
            "anything else I can help you with?"
        )
        room = self._rooms.get(session_id)
        ws = room.guest_ws if room else None
        if ws:
            try:
                await ws.send_text(json.dumps({
                    "type": "system_handover",
                    "to_responder": "BOT",
                    "message": "The agent has resolved your request. You're back with the assistant.",
                    "bot_followup": followup_text,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }, default=str))
            except Exception as exc:
                logger.debug("[AgentRoom] notify_guest_bot_resumed failed: %s", exc)

        # Persist the follow-up as a real assistant turn so chat history shows it
        # too (the live frame above only reaches the guest's socket). Best-effort.
        try:
            from core.context_manager import context_manager as _cm
            from schemas.chat import MessageRole as _MR
            ctx = await _cm.get_context(session_id)
            if ctx is not None:
                ctx.add_message(_MR.ASSISTANT, followup_text)
                await _cm.save_context(ctx)
        except Exception as exc:
            logger.debug("[AgentRoom] handback follow-up persist skipped: %s", exc)

    async def notify_pending_guest_waiting(self, guest_ws: WebSocket) -> None:
        """Push a waiting frame to guest immediately after escalation."""
        try:
            await guest_ws.send_text(json.dumps({
                "type": "system_handover",
                "to_responder": "WAITING",
                "message": "Connecting you with a live agent. Please wait...",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }, default=str))
        except Exception as exc:
            logger.debug("[AgentRoom] notify_pending_guest_waiting failed: %s", exc)

    # -- Offline fallback timer ----------------------------------------

    async def start_fallback_timer(
        self,
        session_id: str,
        timeout_seconds: int = DEFAULT_FALLBACK_TIMEOUT_SECONDS,
    ) -> None:
        """
        Wait `timeout_seconds`. If the escalation is still pending (no agent
        accepted), remove it from the queue and push system_handover BOT to
        the guest so the widget re-enables the bot automatically.

        Called via asyncio.create_task() from EscalationHandler.
        """
        await asyncio.sleep(timeout_seconds)

        esc = self._pending.pop(session_id, None)
        if not esc:
            # Already accepted or cleaned up — nothing to do
            return

        logger.info(
            "[AgentRoom] fallback timer fired session=%s (no agent in %ds) — restoring bot",
            session_id, timeout_seconds,
        )

        fallback_msg = (
            "Our team is currently unavailable. "
            "The assistant will continue helping you."
        )
        try:
            await esc.guest_ws.send_text(json.dumps({
                "type": "system_handover",
                "to_responder": "BOT",
                "message": fallback_msg,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }, default=str))
        except Exception as exc:
            logger.debug("[AgentRoom] fallback notify failed session=%s: %s", session_id, exc)

        # Also notify agents that the escalation expired
        try:
            await self.broadcast_to_agents(esc.hotel_code, {
                "type": "escalation_expired",
                "session_id": session_id,
                "reason": f"No agent accepted within {timeout_seconds}s",
            })
        except Exception:
            pass


# Module-level singleton
agent_room_service = AgentRoomService()
