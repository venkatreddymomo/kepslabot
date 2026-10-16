"""
Conversation Context Manager

Handles storing, retrieving, and managing conversation context.
Uses in-memory storage plus optional DB persistence.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from config.settings import settings
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import Conversation as DBConversation
from models.database import Guest, Hotel, Message as DBMessage
from schemas.chat import (
    ConversationContext,
    ConversationState,
    Message as ChatMessage,
    MessageRole,
)

logger = logging.getLogger(__name__)


class ContextManager:
    """Manages conversation contexts across sessions."""

    def __init__(
        self,
        ttl_hours: int = 24,
        local_store_enabled: Optional[bool] = None,
        local_store_file: Optional[str] = None,
    ):
        self.ttl_hours = ttl_hours
        # In-memory storage for development
        # Replace with Redis in production
        self._storage: Dict[str, dict] = {}
        self._expiry: Dict[str, datetime] = {}
        # Per-session write lock. Context is loaded as a fresh COPY per request
        # and saved by whole-dict overwrite, so two requests hitting the SAME
        # session concurrently (classically: the widget's location-intro turn and
        # its fire-and-forget lead-form submit fired together on a location pick)
        # each read-modify-write independently and the slower writer clobbers the
        # faster one's changes — e.g. the intro pins the chosen property scope,
        # then the lead-form's stale save drops it → the next chip answers the
        # wrong location / "which property?". Callers serialize their
        # read-modify-write section per session via `session_lock`.
        self._session_locks: Dict[str, asyncio.Lock] = {}
        configured_local_enabled = bool(
            getattr(settings, "conversation_local_store_enabled", True)
        )
        self._local_store_enabled = (
            configured_local_enabled
            if local_store_enabled is None
            else bool(local_store_enabled)
        )
        configured_local_file = str(
            getattr(settings, "conversation_local_store_file", "") or ""
        ).strip()
        effective_local_file = str(local_store_file or "").strip() or configured_local_file
        if not effective_local_file:
            effective_local_file = "./data/runtime/local_contexts.json"
        self._local_store_path = Path(effective_local_file)

    @staticmethod
    def _use_db_store(db_session: Optional[AsyncSession]) -> bool:
        return db_session is not None and bool(getattr(settings, "chat_db_first_mode", True))

    def session_lock(self, session_id: str) -> asyncio.Lock:
        """Per-session write lock (created on first use).

        Hold it across a request's read-modify-write of the session context
        (``get_or_create_context`` … ``save_context``) so two concurrent requests
        on the SAME session cannot clobber each other's ``pending_data``. Keyed by
        session_id, so it never serializes different sessions. A single visitor's
        turns are already effectively sequential, so the only thing this actually
        serializes is the pathological concurrent pair (intro turn + fire-and-
        forget lead-form submit) — which is exactly the bug it prevents.
        """
        key = str(session_id or "")
        lock = self._session_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[key] = lock
        return lock

    async def get_context(
        self,
        session_id: str,
        db_session: Optional[AsyncSession] = None,
    ) -> Optional[ConversationContext]:
        """Retrieve context for a session."""
        self._cleanup_expired()

        if session_id in self._storage:
            return ConversationContext(**self._storage[session_id])

        if not self._use_db_store(db_session):
            local_context = self._load_context_from_local_store(session_id)
            if local_context is not None:
                self._save_to_memory(local_context, persist_local=False)
            return local_context

        context = await self._get_context_from_db(session_id, db_session)
        if context:
            self._save_to_memory(context, persist_local=False)
        return context

    async def save_context(
        self,
        context: ConversationContext,
        db_session: Optional[AsyncSession] = None,
        *,
        force_persist: bool = False,
    ) -> None:
        """Save context for a session."""
        self._save_to_memory(
            context,
            persist_local=(
                not self._use_db_store(db_session)
                and self._is_local_store_enabled()
            ),
        )
        if (
            not force_persist
            and self._use_db_store(db_session)
            and bool(getattr(settings, "chat_response_first_persistence_enabled", True))
            and isinstance(getattr(context, "pending_data", None), dict)
            and bool(context.pending_data.get("_defer_summary_refresh"))
        ):
            return
        if self._use_db_store(db_session):
            if not force_persist and bool(getattr(settings, "chat_background_context_save_enabled", False)):
                # Persist off the critical path. The request's db_session closes
                # when the response returns, so the background task opens its own.
                # In-memory context is already saved above, so the next turn in
                # this process still sees the latest state — only the durable DB
                # write (over VPN) is deferred by a few hundred ms.
                asyncio.create_task(self._background_save_context_to_db(context))
            else:
                await self._save_context_to_db(context, db_session)

    async def _background_save_context_to_db(self, context: ConversationContext) -> None:
        """Fire-and-forget DB persistence with a FRESH session (the request
        session is gone by the time this runs). Best-effort: any failure only
        means this one turn isn't durably persisted — the in-memory copy stands
        and the next turn's save will re-persist the conversation.

        Serialized per session via `session_lock`: without it, an overlapping
        flush from the next turn races this one into a duplicate-key insert on
        the shared conversation/message rows (the old awaited path was already
        sequential per session)."""
        try:
            from models.database import AsyncSessionLocal

            async with self.session_lock(str(getattr(context, "session_id", "") or "")):
                async with AsyncSessionLocal() as session:
                    await self._save_context_to_db(context, session)
        except Exception as exc:  # noqa: BLE001 - best-effort background flush
            logger.warning("background_context_save_failed: %s", exc)

    async def set_turn_display_payload(
        self,
        session_id: str,
        payload: dict,
        conversation_uid: str = "",
        db_session: Optional[AsyncSession] = None,
    ) -> None:
        """Stamp the finalized display payload onto the latest assistant message.

        The assistant turn is persisted *during* process_message, before the
        route attaches display_message/images, so we can't include those at
        insert time. This runs a small UPDATE afterwards to enrich that row.

        Best-effort and fully optional: any failure leaves the row as plain
        text (today's behavior). MySQL forbids ORDER BY/LIMIT on a JOIN UPDATE,
        so we resolve the target id first, then UPDATE by id.
        """
        sid = str(session_id or "").strip()
        cuid = str(conversation_uid or "").strip()
        if not sid or not isinstance(payload, dict) or not payload:
            return
        try:
            payload_text = json.dumps(payload, default=str)
        except Exception:
            return
        if len(payload_text) > 400_000:  # guardrail against oversized blobs
            return

        from sqlalchemy import text as _text
        from models.database import AsyncSessionLocal

        select_sql_with_uid = _text(
            "SELECT m.id FROM new_bot_messages m "
            "JOIN new_bot_conversations c ON c.id = m.conversation_id "
            "WHERE c.session_id = :sid AND m.role = 'assistant' "
            "AND m.conversation_uid = :cuid "
            "ORDER BY m.id DESC LIMIT 1"
        )
        select_sql_without_uid = _text(
            "SELECT m.id FROM new_bot_messages m "
            "JOIN new_bot_conversations c ON c.id = m.conversation_id "
            "WHERE c.session_id = :sid AND m.role = 'assistant' "
            "ORDER BY m.id DESC LIMIT 1"
        )
        update_sql = _text(
            "UPDATE new_bot_messages SET display_payload = :payload WHERE id = :mid"
        )

        async def _run(session: AsyncSession) -> None:
            row = None
            if cuid:
                row = (await session.execute(select_sql_with_uid, {"sid": sid, "cuid": cuid})).first()
            if not row:
                row = (await session.execute(select_sql_without_uid, {"sid": sid})).first()
            if not row:
                return
            await session.execute(update_sql, {"payload": payload_text, "mid": row[0]})
            await session.commit()

        try:
            if db_session is not None:
                await _run(db_session)
            else:
                async with AsyncSessionLocal() as session:
                    await _run(session)
        except Exception as exc:
            logger.debug("[ctx] set_turn_display_payload failed session=%s: %s", sid, exc)

        # AND THE SAME ONTO THE IN-MEMORY COPY. The UPDATE above only touches the
        # database row, but get_context serves the cached context whenever the
        # session is still in memory — which it always is right after a turn. So
        # the row had the photos and the API handed back the copy without them,
        # and the widget replayed a picture-less answer for the whole life of the
        # session. Keep the two in step at the point of writing.
        try:
            cached = self._storage.get(sid)
            if not cached:
                return
            messages = cached.get("messages") or []
            for msg in reversed(messages):
                role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", None)
                role = getattr(role, "value", role)
                if str(role) != "assistant":
                    continue
                if isinstance(msg, dict):
                    meta = msg.get("metadata")
                    if not isinstance(meta, dict):
                        meta = {}
                        msg["metadata"] = meta
                else:
                    meta = getattr(msg, "metadata", None)
                    if not isinstance(meta, dict):
                        meta = {}
                        msg.metadata = meta
                meta["display_payload"] = payload
                break
        except Exception as exc:  # noqa: BLE001 — mirroring must never break a turn
            logger.debug("[ctx] display payload memory sync failed session=%s: %s", sid, exc)

    async def create_context(
        self,
        session_id: str,
        hotel_code: str,
        guest_phone: Optional[str] = None,
        channel: Optional[str] = None,
        db_session: Optional[AsyncSession] = None,
    ) -> ConversationContext:
        """Create a new conversation context."""
        context = ConversationContext(
            session_id=session_id,
            hotel_code=hotel_code,
            guest_phone=guest_phone,
            channel=self._normalize_channel(channel),
        )
        await self.save_context(context, db_session=db_session)
        return context

    async def get_or_create_context(
        self,
        session_id: str,
        hotel_code: str,
        guest_phone: Optional[str] = None,
        channel: Optional[str] = None,
        db_session: Optional[AsyncSession] = None,
    ) -> ConversationContext:
        """Get existing context or create new one."""
        context = await self.get_context(session_id, db_session=db_session)
        if context is None:
            context = await self.create_context(
                session_id=session_id,
                hotel_code=hotel_code,
                guest_phone=guest_phone,
                channel=channel,
                db_session=db_session,
            )
        elif channel:
            context.channel = self._normalize_channel(channel)
        return context

    async def update_state(
        self,
        session_id: str,
        state: ConversationState,
        pending_action: Optional[str] = None,
        pending_data: Optional[dict] = None,
        db_session: Optional[AsyncSession] = None,
    ) -> Optional[ConversationContext]:
        """Update conversation state."""
        context = await self.get_context(session_id, db_session=db_session)
        if context is None:
            return None

        context.state = state
        context.pending_action = pending_action
        if pending_data is not None:
            context.pending_data = pending_data

        await self.save_context(context, db_session=db_session)
        return context

    async def add_message(
        self,
        session_id: str,
        role: MessageRole,
        content: str,
        metadata: Optional[dict] = None,
        db_session: Optional[AsyncSession] = None,
    ) -> Optional[ConversationContext]:
        """Add a message to conversation history."""
        context = await self.get_context(session_id, db_session=db_session)
        if context is None:
            return None

        context.add_message(role, content, metadata)
        await self.save_context(context, db_session=db_session)
        return context

    async def delete_context(
        self,
        session_id: str,
        db_session: Optional[AsyncSession] = None,
    ) -> bool:
        """Delete a context."""
        deleted_memory = False
        if session_id in self._storage:
            del self._storage[session_id]
            if session_id in self._expiry:
                del self._expiry[session_id]
            deleted_memory = True

        deleted_db = False
        if self._use_db_store(db_session):
            result = await db_session.execute(
                select(DBConversation).where(DBConversation.session_id == session_id)
            )
            conversation = result.scalar_one_or_none()
            if conversation is not None:
                await db_session.delete(conversation)
                await db_session.commit()
                deleted_db = True

        deleted_local = self._remove_local_contexts([session_id])

        return deleted_memory or deleted_db or deleted_local

    async def list_sessions(self, db_session: Optional[AsyncSession] = None) -> list[str]:
        """List all active session IDs."""
        self._cleanup_expired()
        session_ids = set(self._storage.keys())

        if self._use_db_store(db_session):
            result = await db_session.execute(select(DBConversation.session_id))
            session_ids.update(result.scalars().all())
        else:
            session_ids.update(self._list_local_store_session_ids())

        return sorted(session_ids)

    def _cleanup_expired(self) -> None:
        """Remove expired sessions."""
        now = datetime.now(UTC)
        expired = [
            sid for sid, exp_time in self._expiry.items()
            if exp_time < now
        ]
        for sid in expired:
            self._storage.pop(sid, None)
            self._expiry.pop(sid, None)
            # Drop the per-session lock too, but only if nobody currently holds it
            # (a held lock will be released by its owner; popping it would let a
            # concurrent caller mint a second lock and lose the serialization).
            _lk = self._session_locks.get(sid)
            if _lk is not None and not _lk.locked():
                self._session_locks.pop(sid, None)
        if expired:
            self._remove_local_contexts(expired)

    async def get_conversation_summary(
        self,
        session_id: str,
        db_session: Optional[AsyncSession] = None,
    ) -> dict:
        """Get a summary of the conversation for debugging."""
        context = await self.get_context(session_id, db_session=db_session)
        if context is None:
            return {"error": "Session not found"}

        return {
            "session_id": session_id,
            "state": context.state.value,
            "message_count": len(context.messages),
            "guest_phone": context.guest_phone,
            "room_number": context.room_number,
            "channel": context.channel,
            "pending_action": context.pending_action,
            "pending_data": context.pending_data,
            "created_at": context.created_at.isoformat(),
            "updated_at": context.updated_at.isoformat(),
        }

    @staticmethod
    def _normalize_channel(channel: Optional[str]) -> str:
        """Normalize channel IDs to a compact set used in storage."""
        if not channel:
            return "web"

        normalized = channel.strip().lower().replace("-", "_")
        if normalized in {"web", "web_widget", "widget", "chat_widget"}:
            return "web"
        if normalized in {"wa", "whatsapp", "whats_app"}:
            return "whatsapp"
        return normalized[:20]

    def _save_to_memory(
        self,
        context: ConversationContext,
        *,
        persist_local: bool = True,
    ) -> None:
        """Write current context snapshot to in-memory store."""
        context.updated_at = datetime.now(UTC)
        self._storage[context.session_id] = context.model_dump(mode="json")
        self._expiry[context.session_id] = datetime.now(UTC) + timedelta(hours=self.ttl_hours)
        if persist_local:
            self._upsert_local_context(context)

    def _is_local_store_enabled(self) -> bool:
        return bool(self._local_store_enabled)

    def _read_local_store_map(self) -> dict[str, dict[str, Any]]:
        if not self._is_local_store_enabled():
            return {}

        path = self._local_store_path
        if not path.exists():
            return {}

        try:
            raw_text = path.read_text(encoding="utf-8")
            if not str(raw_text).strip():
                return {}
            parsed = json.loads(raw_text)
        except Exception as exc:
            logger.warning("Failed to read local conversation store at %s: %s", path, exc)
            return {}

        if not isinstance(parsed, dict):
            return {}

        contexts = parsed.get("contexts") if isinstance(parsed.get("contexts"), dict) else parsed
        if not isinstance(contexts, dict):
            return {}

        normalized: dict[str, dict[str, Any]] = {}
        for session_id, payload in contexts.items():
            if not isinstance(session_id, str) or not isinstance(payload, dict):
                continue
            normalized[session_id] = payload
        return normalized

    def _write_local_store_map(self, contexts: dict[str, dict[str, Any]]) -> bool:
        if not self._is_local_store_enabled():
            return False

        path = self._local_store_path
        payload = {
            "version": 1,
            "saved_at": datetime.now(UTC).isoformat(),
            "contexts": contexts,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_name(f"{path.name}.tmp")
            tmp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp_path.replace(path)
            return True
        except Exception as exc:
            logger.warning("Failed to write local conversation store at %s: %s", path, exc)
            return False

    def _upsert_local_context(self, context: ConversationContext) -> None:
        if not self._is_local_store_enabled():
            return
        contexts = self._read_local_store_map()
        contexts[context.session_id] = context.model_dump(mode="json")
        self._write_local_store_map(contexts)

    def _remove_local_contexts(self, session_ids: list[str]) -> bool:
        if not self._is_local_store_enabled() or not session_ids:
            return False

        contexts = self._read_local_store_map()
        changed = False
        for session_id in session_ids:
            if session_id in contexts:
                contexts.pop(session_id, None)
                changed = True
        if not changed:
            return False
        return self._write_local_store_map(contexts)

    def _load_context_from_local_store(self, session_id: str) -> Optional[ConversationContext]:
        contexts = self._read_local_store_map()
        payload = contexts.get(session_id)
        if not isinstance(payload, dict):
            return None
        try:
            return ConversationContext(**payload)
        except Exception as exc:
            logger.warning(
                "Invalid local context payload for session %s at %s: %s",
                session_id,
                self._local_store_path,
                exc,
            )
            return None

    def _list_local_store_session_ids(self) -> list[str]:
        contexts = self._read_local_store_map()
        return sorted(contexts.keys())

    async def _resolve_hotel(self, db_session: AsyncSession, hotel_code: str) -> Optional[Hotel]:
        """Resolve hotel by code from the DB only — never auto-create.

        Auto-creation used to mint phantom rows whenever a chat session landed
        for a code that had been deleted (or had a stale JSON file lying
        around). Now: case-insensitive lookup against ``new_bot_hotels``; if
        nothing matches, fall back to the canonical DEFAULT row when one
        exists, otherwise return None and let the caller surface a clean
        "hotel not found" error rather than silently working against a fresh
        placeholder.
        """
        code = (hotel_code or "").strip()
        if not code:
            code = "DEFAULT"

        # Exact match first (preserves the casing of the original row).
        result = await db_session.execute(select(Hotel).where(Hotel.code == code))
        hotel = result.scalar_one_or_none()
        if hotel is not None:
            return hotel

        # Case-insensitive fallback — match existing row regardless of case.
        result = await db_session.execute(
            select(Hotel).where(Hotel.code.ilike(code)).limit(1)
        )
        hotel = result.scalars().first()
        if hotel is not None:
            return hotel

        # Final fallback: canonical DEFAULT hotel when one exists.
        fallback = await db_session.execute(select(Hotel).where(Hotel.code == "DEFAULT"))
        default_hotel = fallback.scalar_one_or_none()
        return default_hotel

    async def _resolve_guest(
        self,
        db_session: AsyncSession,
        hotel_id: int,
        context: ConversationContext,
    ) -> Optional[Guest]:
        """Resolve guest by phone; creates a guest record if phone is available."""
        if not context.guest_phone:
            return None

        result = await db_session.execute(
            select(Guest).where(
                Guest.hotel_id == hotel_id,
                Guest.phone_number == context.guest_phone,
            )
        )
        guest = result.scalar_one_or_none()
        if guest is None:
            guest = Guest(
                hotel_id=hotel_id,
                phone_number=context.guest_phone,
                name=context.guest_name,
                room_number=context.room_number,
            )
            db_session.add(guest)
            await db_session.flush()
            return guest

        if context.guest_name:
            guest.name = context.guest_name
        if context.room_number:
            guest.room_number = context.room_number
        return guest

    async def _get_context_from_db(
        self,
        session_id: str,
        db_session: AsyncSession,
    ) -> Optional[ConversationContext]:
        """Reconstruct a ConversationContext from DB rows."""
        conv_result = await db_session.execute(
            select(DBConversation).where(DBConversation.session_id == session_id)
        )
        conversation = conv_result.scalar_one_or_none()
        if conversation is None:
            return None

        hotel_result = await db_session.execute(select(Hotel).where(Hotel.id == conversation.hotel_id))
        hotel = hotel_result.scalar_one_or_none()

        guest = None
        if conversation.guest_id:
            guest_result = await db_session.execute(select(Guest).where(Guest.id == conversation.guest_id))
            guest = guest_result.scalar_one_or_none()

        msg_result = await db_session.execute(
            select(DBMessage)
            .where(DBMessage.conversation_id == conversation.id)
            .order_by(DBMessage.created_at.asc(), DBMessage.id.asc())
        )
        db_messages = list(msg_result.scalars().all())

        chat_messages: list[ChatMessage] = []
        for msg in db_messages:
            try:
                role = MessageRole(msg.role)
            except ValueError:
                role = MessageRole.ASSISTANT

            metadata: dict[str, Any] = {}
            if msg.intent:
                metadata["intent"] = msg.intent
            if msg.confidence is not None:
                metadata["confidence"] = float(msg.confidence)
            metadata["channel"] = self._normalize_channel(
                getattr(msg, "channel", None) or getattr(conversation, "channel", None)
            )
            # Carry the per-thread id so get_recent_messages can scope LLM history
            # to the current conversation only.
            _cuid = getattr(msg, "conversation_uid", None)
            if _cuid:
                metadata["conversation_uid"] = str(_cuid)
            # THE RICH DISPLAY THIS TURN ACTUALLY RENDERED — the beautified text,
            # the form the guest filled in, a menu download card, the photos an
            # answer came with. It was being written on every such turn and read
            # back by nothing: this loader is what feeds GET /api/chat/session,
            # which is what the widget replays a conversation from, so 3130 rows
            # of it had been saved for nobody. Chat history showed plain text
            # where the guest had seen a form or a picture.
            _dp_raw = getattr(msg, "display_payload", None)
            if _dp_raw:
                try:
                    _dp = json.loads(_dp_raw) if isinstance(_dp_raw, str) else _dp_raw
                    if isinstance(_dp, dict) and _dp:
                        metadata["display_payload"] = _dp
                except (ValueError, TypeError):
                    pass  # a malformed blob must not cost us the message itself

            chat_messages.append(
                ChatMessage(
                    role=role,
                    content=msg.content,
                    timestamp=msg.created_at,
                    metadata=metadata,
                )
            )

        try:
            state = ConversationState(conversation.state)
        except ValueError:
            state = ConversationState.IDLE

        pending_data = getattr(conversation, "pending_data", None) or {}
        if isinstance(pending_data, str):
            try:
                pending_data = json.loads(pending_data)
            except json.JSONDecodeError:
                pending_data = {}
        if not isinstance(pending_data, dict):
            pending_data = {}

        return ConversationContext(
            session_id=conversation.session_id,
            hotel_code=hotel.code if hotel else "DEFAULT",
            guest_phone=guest.phone_number if guest else None,
            guest_name=guest.name if guest else None,
            room_number=guest.room_number if guest else None,
            channel=self._normalize_channel(getattr(conversation, "channel", None)),
            state=state,
            pending_action=getattr(conversation, "pending_action", None),
            pending_data=pending_data,
            messages=chat_messages,
            created_at=conversation.created_at,
            updated_at=conversation.updated_at,
        )

    async def _save_context_to_db(
        self,
        context: ConversationContext,
        db_session: AsyncSession,
    ) -> None:
        """Persist context snapshot to conversation/message tables."""
        try:
            context.channel = self._normalize_channel(context.channel)
            if not isinstance(context.pending_data, dict):
                context.pending_data = {}

            hotel = await self._resolve_hotel(db_session, context.hotel_code)
            if hotel is None:
                # Hotel not found in DB and no DEFAULT row exists. We refuse
                # to silently mint a placeholder row — that's how phantom
                # hotels used to keep coming back. Skip DB persistence for
                # this turn; the in-memory context still flows through the
                # local store fallback.
                logger.warning(
                    "context_save_skipped_unknown_hotel",
                    extra={"hotel_code": str(context.hotel_code or "")},
                )
                return
            guest = await self._resolve_guest(db_session, hotel.id, context)

            conv_result = await db_session.execute(
                select(DBConversation).where(DBConversation.session_id == context.session_id)
            )
            conversation = conv_result.scalar_one_or_none()

            if conversation is None:
                conversation = DBConversation(
                    session_id=context.session_id,
                    hotel_id=hotel.id,
                    guest_id=guest.id if guest else None,
                    state=context.state.value,
                    pending_action=context.pending_action,
                    pending_data=context.pending_data or {},
                    channel=context.channel,
                )
                db_session.add(conversation)
                await db_session.flush()
            else:
                conversation.hotel_id = hotel.id
                conversation.guest_id = guest.id if guest else conversation.guest_id
                conversation.state = context.state.value
                conversation.pending_action = context.pending_action
                conversation.pending_data = context.pending_data or {}
                conversation.channel = context.channel

            # Insert only messages not already persisted — deduped by (role,
            # content) MULTIPLICITY, not by positional count. A positional
            # `messages[existing_count:]` slice silently drops messages whenever
            # the in-memory list isn't perfectly aligned with the DB rows (e.g.
            # after a form-submit persists its turn and a later WS turn loads a
            # differently-shaped context) — that misalignment was dropping guest
            # messages from chat history. This is order-independent and
            # idempotent: re-running never double-inserts.
            from collections import Counter as _Counter
            existing_rows = await db_session.execute(
                select(DBMessage.role, DBMessage.content).where(
                    DBMessage.conversation_id == conversation.id
                )
            )
            _remaining = _Counter(
                (str(_r or ""), str(_c or "")) for _r, _c in existing_rows.all()
            )
            if context.messages:
                for msg in context.messages:
                    _key = (str(msg.role.value), str(msg.content or ""))
                    if _remaining.get(_key, 0) > 0:
                        # Already persisted — consume one and skip.
                        _remaining[_key] -= 1
                        continue
                    meta = msg.metadata or {}
                    # display_payload may be set on the message metadata (e.g. a
                    # form-submission user bubble). Serialize it so chat history
                    # can replay exactly what the guest saw.
                    _dp = meta.get("display_payload")
                    _dp_text = (
                        json.dumps(_dp, default=str)
                        if isinstance(_dp, dict) and _dp
                        else None
                    )
                    _msg_kwargs = dict(
                        conversation_id=conversation.id,
                        role=msg.role.value,
                        content=msg.content,
                        intent=meta.get("intent"),
                        confidence=meta.get("confidence"),
                        channel=self._normalize_channel(meta.get("channel") or context.channel),
                        created_at=msg.timestamp,
                    )
                    # Only set display_payload when present. SQLAlchemy omits unset
                    # columns from the INSERT, so normal turns keep persisting even
                    # if the display_payload migration hasn't run yet.
                    if _dp_text is not None:
                        _msg_kwargs["display_payload"] = _dp_text
                    # Tag the message with its chat thread so history can be scoped
                    # per conversation. Omitted when absent (legacy rows stay NULL).
                    _cuid = str(meta.get("conversation_uid") or "").strip()
                    if _cuid:
                        _msg_kwargs["conversation_uid"] = _cuid
                    db_session.add(DBMessage(**_msg_kwargs))

            await db_session.commit()
        except Exception:
            await db_session.rollback()
            raise


# Global instance
context_manager = ContextManager()
