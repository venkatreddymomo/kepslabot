"""
Agent WebSocket endpoint -- /ws/agent

Hotel staff connect here to see live escalations and chat with guests.

Query params (required):
  ?hotel_code=...   &agent_id=...   &agent_name=...

Protocol (JSON frames):

  Agent -> Server:
    {"type": "accept",   "session_id": "..."}                  accept escalation
    {"type": "observe",  "session_id": "..."}                  join as observer (secondary agent)
    {"type": "message",  "session_id": "...", "text": "..."}   send reply to guest
    {"type": "typing",   "session_id": "..."}                  agent is typing
    {"type": "resolve",  "session_id": "..."}                  close room, return to bot
    {"type": "whisper",  "session_id": "...", "text": "..."}   internal note (not seen by guest)
    {"type": "ping"}

  Server -> Agent:
    {"type": "connected",           "agent_id": "...", "hotel_code": "..."}
    {"type": "new_escalation",      "session_id": "...", "preview": "...",
                                    "guest_info": {...}, "created_at": "..."}
    {"type": "history",             "session_id": "...", "messages": [...]}
    {"type": "guest_message",       "session_id": "...", "text": "...", "timestamp": "..."}
    {"type": "guest_typing",        "session_id": "..."}
    {"type": "escalation_accepted", "session_id": "...", "by_agent": "..."}
    {"type": "escalation_expired",  "session_id": "...", "reason": "..."}
    {"type": "observer_joined",     "session_id": "...", "agent_name": "...",
                                    "observer_count": N}
    {"type": "accepted",            "session_id": "...", "guest_info": {...}}
    {"type": "observing",           "session_id": "...", "guest_info": {...}}
    {"type": "resolved",            "session_id": "..."}
    {"type": "pong"}
    {"type": "error",               "detail": "..."}
    {"type": "whisper_ack",         "session_id": "..."}
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from services.agent_room_service import agent_room_service

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Agent WebSocket"])


def _safe_parse(raw: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    return None


async def _send(ws: WebSocket, payload: dict[str, Any]) -> None:
    try:
        await ws.send_text(json.dumps(payload, default=str))
    except Exception as exc:
        logger.debug("[WSAgent] send failed: %s", exc)


@router.websocket("/ws/agent")
async def websocket_agent_endpoint(websocket: WebSocket) -> None:
    """
    WebSocket endpoint for human agents (hotel staff).
    Each agent connects once per browser tab; hotel_code scopes visibility.

    Production features supported:
      - Multi-agent observer mode (secondary agents watch/participate)
      - Conversation history replay on accept/observe
      - Full audit logging of agent turns to context manager
      - Offline fallback timer (triggered from escalation handler)
    """
    params = dict(websocket.query_params)
    hotel_code: str = str(params.get("hotel_code") or "").strip()
    agent_id: str   = str(params.get("agent_id")   or "").strip()
    agent_name: str = str(params.get("agent_name") or agent_id or "Agent").strip()

    if not hotel_code or not agent_id:
        await websocket.close(code=4001)
        return

    await websocket.accept()
    agent_room_service.register_agent(hotel_code, websocket)
    logger.info("[WSAgent] connected agent=%s hotel=%s", agent_id, hotel_code)

    # Send connected confirmation + all pending escalations for this hotel
    await _send(websocket, {
        "type": "connected",
        "agent_id": agent_id,
        "agent_name": agent_name,
        "hotel_code": hotel_code,
    })

    # Replay all pending escalations for this hotel
    pending = agent_room_service.get_all_pending_for_hotel(hotel_code)
    for esc in pending:
        await _send(websocket, {
            "type": "new_escalation",
            "session_id": esc.session_id,
            "preview": esc.preview,
            "guest_info": esc.guest_info,
            "created_at": esc.created_at.isoformat(),
        })

    # Replay all active (already-accepted) rooms so the inbox shows every
    # live conversation — not just ones waiting for acceptance.
    active_rooms = agent_room_service.get_all_active_rooms_for_hotel(hotel_code)
    for room in active_rooms:
        await _send(websocket, {
            "type": "escalation_accepted",
            "session_id": room.session_id,
            "by_agent": room.agent_name,
            "guest_info": room.guest_info,
            "accepted_at": room.accepted_at.isoformat() if room.accepted_at else "",
        })

    # Replay all bot-only sessions so agents see every ongoing conversation
    bot_sessions = agent_room_service.get_all_guest_sessions_for_hotel(hotel_code)
    for gs in bot_sessions:
        await _send(websocket, {
            "type": "active_session",
            "session_id": gs.session_id,
            "guest_info": gs.guest_info,
            "started_at": gs.started_at.isoformat(),
            "message_count": gs.message_count,
        })

    try:
        while True:
            raw = await websocket.receive_text()
            frame = _safe_parse(raw)

            if frame is None:
                await _send(websocket, {"type": "error", "detail": "Invalid JSON"})
                continue

            msg_type = str(frame.get("type") or "").strip().lower()

            # -- ping --------------------------------------------------
            if msg_type == "ping":
                await _send(websocket, {"type": "pong"})
                continue

            # -- accept escalation -------------------------------------
            if msg_type == "accept":
                session_id = str(frame.get("session_id") or "").strip()
                if not session_id:
                    await _send(websocket, {"type": "error", "detail": "session_id required"})
                    continue

                pending_esc = agent_room_service.get_pending(session_id)
                if not pending_esc:
                    await _send(websocket, {
                        "type": "error",
                        "detail": f"No pending escalation for session {session_id}",
                    })
                    continue

                # Create the room (also removes from pending)
                room = agent_room_service.create_room(
                    session_id=session_id,
                    hotel_code=hotel_code,
                    guest_ws=pending_esc.guest_ws,
                    agent_ws=websocket,
                    agent_id=agent_id,
                    agent_name=agent_name,
                    guest_info=pending_esc.guest_info,
                )

                # Notify guest: agent has joined
                await agent_room_service.notify_guest_agent_joined(session_id, agent_name)

                # Replay conversation history to accepting agent
                history = await agent_room_service.get_conversation_history(session_id)
                if history:
                    await _send(websocket, {
                        "type": "history",
                        "session_id": session_id,
                        "messages": history,
                    })

                # Confirm to this agent
                await _send(websocket, {
                    "type": "accepted",
                    "session_id": session_id,
                    "guest_info": room.guest_info,
                })

                # Notify all other agents: this escalation was taken
                await agent_room_service.broadcast_to_agents(hotel_code, {
                    "type": "escalation_accepted",
                    "session_id": session_id,
                    "by_agent": agent_name,
                })

                logger.info("[WSAgent] agent=%s accepted session=%s", agent_name, session_id)
                continue

            # -- observe (join as secondary agent) ---------------------
            if msg_type == "observe":
                session_id = str(frame.get("session_id") or "").strip()
                if not session_id:
                    await _send(websocket, {"type": "error", "detail": "session_id required"})
                    continue

                joined = agent_room_service.add_observer(
                    session_id=session_id,
                    agent_ws=websocket,
                    agent_id=agent_id,
                    agent_name=agent_name,
                )
                if not joined:
                    await _send(websocket, {
                        "type": "error",
                        "detail": f"No active room for session {session_id}",
                    })
                    continue

                room = agent_room_service.get_room(session_id)

                # Replay conversation history to observer
                history = await agent_room_service.get_conversation_history(session_id)
                if history:
                    await _send(websocket, {
                        "type": "history",
                        "session_id": session_id,
                        "messages": history,
                    })

                # Confirm observer mode to this agent
                await _send(websocket, {
                    "type": "observing",
                    "session_id": session_id,
                    "guest_info": room.guest_info if room else {},
                })

                # Notify primary agent and existing observers that someone joined
                observer_count = len(room.observers) if room else 0
                await agent_room_service.send_to_all_agents_in_room(session_id, {
                    "type": "observer_joined",
                    "session_id": session_id,
                    "agent_name": agent_name,
                    "observer_count": observer_count,
                })

                logger.info("[WSAgent] agent=%s observing session=%s", agent_name, session_id)
                continue

            # -- send message to guest ---------------------------------
            if msg_type == "message":
                session_id = str(frame.get("session_id") or "").strip()
                text = str(frame.get("text") or "").strip()
                if not session_id or not text:
                    await _send(websocket, {"type": "error", "detail": "session_id and text required"})
                    continue

                room = agent_room_service.get_room(session_id)
                if not room:
                    await _send(websocket, {"type": "error", "detail": "No active room for session"})
                    continue

                # Deliver to guest
                await agent_room_service.send_to_guest(session_id, {
                    "type": "agent_message",
                    "text": text,
                    "agent_name": agent_name,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })

                # Echo to all agents in room (primary + observers) so everyone sees it
                await agent_room_service.send_to_all_agents_in_room(session_id, {
                    "type": "agent_message_echo",
                    "session_id": session_id,
                    "text": text,
                    "agent_name": agent_name,
                    "agent_id": agent_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })

                # Audit: write agent turn to conversation context for bot continuity
                await agent_room_service.audit_agent_message(
                    session_id=session_id,
                    hotel_code=hotel_code,
                    agent_name=agent_name,
                    text=text,
                )
                continue

            # -- agent typing ------------------------------------------
            if msg_type == "typing":
                session_id = str(frame.get("session_id") or "").strip()
                if session_id:
                    await agent_room_service.send_to_guest(session_id, {
                        "type": "agent_typing",
                    })
                continue

            # -- resolve -----------------------------------------------
            if msg_type == "resolve":
                session_id = str(frame.get("session_id") or "").strip()
                if not session_id:
                    continue

                # Notify guest: bot is back
                await agent_room_service.notify_guest_bot_resumed(session_id)

                # Close the room (also clears observers)
                agent_room_service.close_room(session_id)

                # Confirm to agent
                await _send(websocket, {
                    "type": "resolved",
                    "session_id": session_id,
                })

                logger.info("[WSAgent] agent=%s resolved session=%s", agent_name, session_id)
                continue

            # -- monitor bot session (subscribe to live messages) ------
            if msg_type == "monitor":
                session_id = str(frame.get("session_id") or "").strip()
                if not session_id:
                    await _send(websocket, {"type": "error", "detail": "session_id required"})
                    continue

                added = agent_room_service.add_monitor(session_id, websocket)
                if not added:
                    await _send(websocket, {
                        "type": "error",
                        "detail": f"No active bot session for {session_id}",
                    })
                    continue

                # Replay full conversation history to monitoring agent
                history = await agent_room_service.get_conversation_history(session_id)
                if history:
                    await _send(websocket, {
                        "type": "history",
                        "session_id": session_id,
                        "messages": history,
                    })

                await _send(websocket, {"type": "monitoring", "session_id": session_id})
                logger.info("[WSAgent] agent=%s monitoring session=%s", agent_name, session_id)
                continue

            # -- takeover (agent proactively takes over bot session) ---
            if msg_type == "takeover":
                session_id = str(frame.get("session_id") or "").strip()
                if not session_id:
                    await _send(websocket, {"type": "error", "detail": "session_id required"})
                    continue

                # If there's already a pending escalation, use the normal accept path
                pending_esc = agent_room_service.get_pending(session_id)
                if pending_esc:
                    room = agent_room_service.create_room(
                        session_id=session_id,
                        hotel_code=hotel_code,
                        guest_ws=pending_esc.guest_ws,
                        agent_ws=websocket,
                        agent_id=agent_id,
                        agent_name=agent_name,
                        guest_info=pending_esc.guest_info,
                    )
                else:
                    room = agent_room_service.takeover_session(
                        session_id=session_id,
                        hotel_code=hotel_code,
                        agent_ws=websocket,
                        agent_id=agent_id,
                        agent_name=agent_name,
                    )

                if not room:
                    await _send(websocket, {
                        "type": "error",
                        "detail": f"No active session found for {session_id}",
                    })
                    continue

                # Notify guest: agent has joined
                await agent_room_service.notify_guest_agent_joined(session_id, agent_name)

                # Replay conversation history
                history = await agent_room_service.get_conversation_history(session_id)
                if history:
                    await _send(websocket, {
                        "type": "history",
                        "session_id": session_id,
                        "messages": history,
                    })

                # Confirm this agent is now the primary handler
                await _send(websocket, {
                    "type": "accepted",
                    "session_id": session_id,
                    "guest_info": room.guest_info,
                })

                # Notify all agents: session taken over (remove from bot sessions list)
                await agent_room_service.broadcast_to_agents(hotel_code, {
                    "type": "session_taken_over",
                    "session_id": session_id,
                    "by_agent": agent_name,
                })

                logger.info("[WSAgent] agent=%s took over session=%s", agent_name, session_id)
                continue

            # -- whisper (internal note, guest never sees) -------------
            if msg_type == "whisper":
                session_id = str(frame.get("session_id") or "").strip()
                text = str(frame.get("text") or "").strip()
                # Echo whisper to all agents in room so team sees it
                if session_id and text:
                    await agent_room_service.send_to_all_agents_in_room(session_id, {
                        "type": "whisper",
                        "session_id": session_id,
                        "text": text,
                        "from_agent": agent_name,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })
                await _send(websocket, {"type": "whisper_ack", "session_id": frame.get("session_id")})
                continue

            await _send(websocket, {"type": "error", "detail": f"Unknown frame type: {msg_type!r}"})

    except WebSocketDisconnect:
        logger.info("[WSAgent] disconnected agent=%s hotel=%s", agent_id, hotel_code)
    except Exception as exc:
        logger.warning("[WSAgent] error agent=%s: %s", agent_id, exc)
    finally:
        agent_room_service.unregister_agent(hotel_code, websocket)

        # Remove from all rooms (primary and observer) and handle primary disconnects
        primary_sessions = agent_room_service.remove_agent_from_all_rooms(websocket)
        for session_id in primary_sessions:
            try:
                await agent_room_service.notify_guest_bot_resumed(session_id)
            except Exception:
                pass
            agent_room_service.close_room(session_id)
            logger.info(
                "[WSAgent] room auto-closed on agent disconnect session=%s agent=%s",
                session_id, agent_id,
            )
