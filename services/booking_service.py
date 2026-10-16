"""
Booking Service

Handles creation and lookup of guest bookings in the database.
Used by booking_handler (text confirm) and chat.py (form submit) to persist
booking records that drive the phase-aware context system.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import AsyncSessionLocal, Booking, Guest, Hotel

logger = logging.getLogger(__name__)


def generate_confirmation_code(hotel_code: str, booking_id: int) -> str:
    """Generate a human-readable confirmation code like KHIL-260406-003."""
    prefix = (hotel_code or "HTL").upper()[:6]
    date_part = date.today().strftime("%y%m%d")
    seq = str(booking_id).zfill(3)
    return f"{prefix}-{date_part}-{seq}"


def resolve_phase_from_dates(
    check_in_date: date,
    check_out_date: date,
    reference_date: date | None = None,
) -> str:
    """Derive guest journey phase from booking dates vs today."""
    today = reference_date or date.today()
    if today < check_in_date:
        return "pre_checkin"
    if check_in_date <= today <= check_out_date:
        return "during_stay"
    return "post_checkout"


async def find_or_create_guest(
    session: AsyncSession,
    hotel_id: int,
    phone_number: str,
    name: str | None = None,
    *,
    check_in_date: date | None = None,
    check_out_date: date | None = None,
    room_type: str | None = None,
    property_name: str | None = None,
    property_scope_id: str | None = None,
    room_number: str | None = None,
) -> Guest:
    """Find existing guest by hotel + phone, or create a new one.

    Stay details (dates / room type / property / room number) are denormalized
    onto the Guest row whenever provided so the chat layer can answer
    "when is my checkin" without joining to bookings every turn.
    """
    guest = (await session.execute(
        select(Guest).where(Guest.hotel_id == hotel_id, Guest.phone_number == phone_number)
    )).scalar_one_or_none()
    if guest:
        if name and not guest.name:
            guest.name = name
        if check_in_date:
            guest.check_in_date = check_in_date
        if check_out_date:
            guest.check_out_date = check_out_date
        if room_type:
            guest.room_type = room_type
        if property_name:
            guest.property_name = property_name
        if property_scope_id:
            guest.property_scope_id = property_scope_id
        if room_number:
            guest.room_number = room_number
        return guest
    guest = Guest(
        hotel_id=hotel_id,
        phone_number=phone_number,
        name=name,
        check_in_date=check_in_date,
        check_out_date=check_out_date,
        room_type=room_type,
        property_name=property_name,
        property_scope_id=property_scope_id,
        room_number=room_number,
    )
    session.add(guest)
    await session.flush()
    return guest


async def create_booking(
    *,
    hotel_code: str,
    guest_phone: str,
    guest_name: str | None = None,
    property_name: str | None = None,
    property_scope_id: str | None = None,
    room_number: str | None = None,
    room_type: str | None = None,
    check_in_date: date,
    check_out_date: date,
    num_guests: int = 1,
    status: str = "reserved",
    source_channel: str | None = None,
    special_requests: str | None = None,
    db_session: AsyncSession | None = None,
) -> dict[str, Any]:
    """Create a booking record. Returns a dict with booking details."""

    async def _do_create(session: AsyncSession) -> dict[str, Any]:
        hotel = (await session.execute(
            select(Hotel).where(Hotel.code == hotel_code)
        )).scalar_one_or_none()
        if not hotel:
            logger.warning("create_booking: hotel '%s' not found", hotel_code)
            return {}

        resolved_scope_id = str(property_scope_id or "").strip()
        resolved_property_name = str(property_name or "").strip()
        if resolved_property_name or resolved_scope_id:
            try:
                from services.config_service import config_service

                property_hints = [
                    value
                    for value in [resolved_scope_id, resolved_property_name]
                    if str(value or "").strip()
                ]
                if property_hints:
                    property_resolution = await config_service.resolve_property_scope_ids(
                        property_hints=property_hints,
                        max_sources=200,
                        limit=1,
                    )
                    resolved_rows = (
                        property_resolution.get("resolved", [])
                        if isinstance(property_resolution, dict)
                        else []
                    )
                    if isinstance(resolved_rows, list) and resolved_rows:
                        first_row = resolved_rows[0] if isinstance(resolved_rows[0], dict) else {}
                        resolved_scope_id = str(first_row.get("id") or resolved_scope_id).strip()
                        resolved_property_name = str(
                            first_row.get("name") or resolved_property_name
                        ).strip()
            except Exception:
                logger.exception(
                    "create_booking: property scope resolution failed hotel=%s property=%s scope_id=%s",
                    hotel_code,
                    property_name,
                    property_scope_id,
                )

        guest = await find_or_create_guest(
            session,
            hotel.id,
            guest_phone,
            guest_name,
            check_in_date=check_in_date,
            check_out_date=check_out_date,
            room_type=room_type,
            property_name=resolved_property_name or property_name,
            property_scope_id=resolved_scope_id or property_scope_id,
            room_number=room_number,
        )

        booking = Booking(
            hotel_id=hotel.id,
            guest_id=guest.id,
            confirmation_code="TEMP",
            property_name=resolved_property_name or property_name,
            property_scope_id=resolved_scope_id or property_scope_id,
            room_number=room_number,
            room_type=room_type,
            check_in_date=check_in_date,
            check_out_date=check_out_date,
            num_guests=num_guests,
            status=status,
            source_channel=source_channel,
            special_requests=special_requests,
        )
        session.add(booking)
        await session.flush()
        booking.confirmation_code = generate_confirmation_code(hotel_code, booking.id)
        await session.commit()
        await session.refresh(booking)

        phase = resolve_phase_from_dates(check_in_date, check_out_date)
        return {
            "booking_id": booking.id,
            "guest_id": guest.id,
            "confirmation_code": booking.confirmation_code,
            "guest_name": guest.name,
            "guest_phone": guest.phone_number,
            "property_name": booking.property_name,
            "property_scope_id": booking.property_scope_id,
            "room_number": booking.room_number,
            "room_type": booking.room_type,
            "check_in_date": str(booking.check_in_date),
            "check_out_date": str(booking.check_out_date),
            "status": booking.status,
            "phase": phase,
        }

    if db_session:
        return await _do_create(db_session)
    async with AsyncSessionLocal() as session:
        return await _do_create(session)


async def get_booking_by_id(
    booking_id: int,
    db_session: AsyncSession | None = None,
) -> Optional[Booking]:
    """Fetch a single booking by ID."""
    async def _do(session: AsyncSession):
        return (await session.execute(
            select(Booking).where(Booking.id == booking_id)
        )).scalar_one_or_none()

    if db_session:
        return await _do(db_session)
    async with AsyncSessionLocal() as session:
        return await _do(session)


async def get_booking_context(
    booking_id: int | None = None,
    metadata: dict[str, Any] | None = None,
    db_session: AsyncSession | None = None,
) -> dict[str, Any]:
    """
    Resolve full booking context from either a booking_id or request metadata.
    Returns a dict with all booking fields + resolved phase, or empty dict if no booking.
    """
    bid = booking_id
    if not bid and metadata:
        raw = metadata.get("booking_id")
        if raw:
            try:
                bid = int(raw)
            except (TypeError, ValueError):
                pass

    if not bid:
        return {}

    booking = await get_booking_by_id(bid, db_session)
    if not booking:
        return {}

    # Load guest
    async def _load_guest(session):
        return (await session.execute(
            select(Guest).where(Guest.id == booking.guest_id)
        )).scalar_one_or_none()

    if db_session:
        guest = await _load_guest(db_session)
    else:
        async with AsyncSessionLocal() as session:
            guest = await _load_guest(session)

    phase = resolve_phase_from_dates(booking.check_in_date, booking.check_out_date)
    return {
        "booking_id": booking.id,
        "guest_id": booking.guest_id,
        "confirmation_code": booking.confirmation_code,
        "guest_name": guest.name if guest else None,
        "guest_phone": guest.phone_number if guest else None,
        "property_name": booking.property_name,
        "property_scope_id": booking.property_scope_id,
        "room_number": booking.room_number,
        "room_type": booking.room_type,
        "check_in_date": booking.check_in_date,
        "check_out_date": booking.check_out_date,
        "num_guests": booking.num_guests,
        "status": booking.status,
        "phase": phase,
    }
