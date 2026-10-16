"""
Session profile service — read/write helpers for new_bot_sessions.

new_bot_sessions holds ONE stable lead row per chat session, captured from the
first contact form a guest submits. It is an additional table: it does not
replace conversations, guests, messages, or tickets.

Write rule (upsert_session_profile):
- Called on every contact-form submit (the form is editable / re-shown per thread).
- Insert if the row is new; otherwise:
  - Lead identity (name/phone/email): SET-ONCE / FROZEN — captured from the first
    form submit and never overwritten afterwards. A guest editing the prefilled
    name on a LATER ticket form updates that ticket's own snapshot, not the
    session lead (per product decision: "the lead info shall never change later").
  - location: LATEST-WINS — the returning-guest flow re-asks location each visit
    via a dropdown, so the most recent non-empty value is authoritative.
  - Structural linkage (conversation_id/hotel/channel/source): fill blanks only.
  - raw_form_json: keep the ORIGINAL first-form payload as a historical record.
- Best-effort: failures are swallowed so they never break form submission.

Read rule (get_session_profile):
- Returns the stored stable identity (name/phone/email/location/hotel) for a
  session, for prefilling later tickets/forms and for showing a real name in
  history views. Conversational data (selected property, room type) is NOT
  stored here and must keep coming from chat context.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    AsyncSessionLocal,
    Conversation,
    Hotel,
    NewBotSession,
)

logger = logging.getLogger(__name__)


def _clean(value: Any) -> str:
    return str(value or "").strip()


async def upsert_session_profile(
    *,
    session_id: str,
    hotel_code: str | None = None,
    guest_name: str | None = None,
    guest_phone: str | None = None,
    guest_email: str | None = None,
    location: str | None = None,
    channel: str | None = None,
    source: str | None = None,
    raw_form: dict[str, Any] | None = None,
    db_session: AsyncSession | None = None,
) -> None:
    """Insert/update the per-session lead profile. See module docstring for rules.

    By default this opens its own DB session so a profile-write failure can
    never roll back the surrounding form-submit transaction.
    """
    sid = _clean(session_id)
    if not sid:
        return

    async def _do(session: AsyncSession) -> None:
        row = (await session.execute(
            select(NewBotSession).where(NewBotSession.session_id == sid)
        )).scalar_one_or_none()

        # Resolve conversation + hotel linkage from the existing conversation row.
        conv = (await session.execute(
            select(Conversation).where(Conversation.session_id == sid)
        )).scalar_one_or_none()
        conversation_id = conv.id if conv else None
        hotel_id = conv.hotel_id if conv else None

        hcode = _clean(hotel_code)
        if hotel_id is None and hcode:
            hotel = (await session.execute(
                select(Hotel).where(Hotel.code == hcode)
            )).scalar_one_or_none()
            hotel_id = hotel.id if hotel else None

        name = _clean(guest_name)
        phone = _clean(guest_phone)
        email = _clean(guest_email)
        loc = _clean(location)
        chan = _clean(channel)
        src = _clean(source)

        if row is None:
            session.add(NewBotSession(
                session_id=sid,
                conversation_id=conversation_id,
                hotel_id=hotel_id,
                hotel_code=hcode or None,
                guest_name=name or None,
                guest_phone=phone or None,
                guest_email=email or None,
                location=loc or None,
                channel=chan or None,
                source=src or None,
                raw_form_json=raw_form if isinstance(raw_form, dict) else None,
            ))
        else:
            # Structural linkage: fill blanks only (set once).
            if conversation_id is not None and row.conversation_id is None:
                row.conversation_id = conversation_id
            if hotel_id is not None and row.hotel_id is None:
                row.hotel_id = hotel_id
            if hcode and not _clean(row.hotel_code):
                row.hotel_code = hcode
            # Lead identity: SET-ONCE / FROZEN. The lead's name/phone/email are
            # locked to the first form submission and never change afterwards. A
            # guest editing the prefilled value on a later ticket form updates THAT
            # ticket's snapshot (handled in ticketing), not this session lead — so
            # we only fill a field that is still empty, and never overwrite an
            # already-captured value.
            if name and not _clean(row.guest_name):
                row.guest_name = name
            if phone and not _clean(row.guest_phone):
                row.guest_phone = phone
            if email and not _clean(row.guest_email):
                row.guest_email = email
            # location: LATEST-WINS — re-picked per visit via the returning-guest
            # location dropdown, so the most recent non-empty value is current.
            if loc:
                row.location = loc
            if chan and not _clean(row.channel):
                row.channel = chan
            if src and not _clean(row.source):
                row.source = src
            # Preserve the ORIGINAL first-form payload as a historical record.
            if isinstance(raw_form, dict) and not row.raw_form_json:
                row.raw_form_json = raw_form

        await session.commit()

    try:
        if db_session is not None:
            await _do(db_session)
        else:
            async with AsyncSessionLocal() as session:
                await _do(session)
    except Exception:
        logger.warning(
            "upsert_session_profile failed for session_id=%s", sid, exc_info=True
        )


async def get_session_profile(
    session_id: str,
    db_session: AsyncSession | None = None,
) -> dict[str, Any]:
    """Return the stored stable identity for a session, or {} if none.

    Intended as the pre-booking prefill source (priority just below an explicit
    value in the current request) and for showing a real name in history views.
    """
    sid = _clean(session_id)
    if not sid:
        return {}

    async def _do(session: AsyncSession) -> dict[str, Any]:
        row = (await session.execute(
            select(NewBotSession).where(NewBotSession.session_id == sid)
        )).scalar_one_or_none()
        if row is None:
            return {}
        return {
            "session_id": row.session_id,
            "conversation_id": row.conversation_id,
            "hotel_id": row.hotel_id,
            "hotel_code": row.hotel_code,
            "guest_name": row.guest_name,
            "guest_phone": row.guest_phone,
            "guest_email": row.guest_email,
            "location": row.location,
            "channel": row.channel,
            "source": row.source,
        }

    try:
        if db_session is not None:
            return await _do(db_session)
        async with AsyncSessionLocal() as session:
            return await _do(session)
    except Exception:
        logger.warning(
            "get_session_profile failed for session_id=%s", sid, exc_info=True
        )
        return {}
