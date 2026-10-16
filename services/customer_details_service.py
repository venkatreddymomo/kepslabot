"""
Customer details service — writes lead identity to GHN_CHAT_BOT_CUSTOMER_DETAILS.

The Kepsla dashboard / Lumira engage bot read guest identity (name, email,
phone) from this table keyed by Session_Id (see Lumira's
fetch_engage_user_details). That table is normally populated by the closed Java
widget backend; our bot submits tickets directly and bypasses it, so the guest
name shows blank. We write the row ourselves on form submit.

Design:
- write_bg() is fire-and-forget — it NEVER blocks or breaks the chat response.
- Processing_Status defaults to 'PROCESSED' so the Java email job does NOT pick
  these rows up (no emails are triggered).
- One row per form submission (the table is an append log; readers take the
  newest row for a Session_Id).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

_INSERT_SQL = text(
    """
    INSERT INTO GHN_CHAT_BOT_CUSTOMER_DETAILS
        (Session_Id, Conversation_Id, Widget_Id, Group_Id, Form_Id, Form_Type,
         Guest_Name, First_Name, Last_Name, Email_Id, Mobile_number,
         Country, City, Property_Name, Topic, Consent_Given, Others,
         Processing_Status)
    VALUES
        (:session_id, :conversation_id, :widget_id, :group_id, :form_id, :form_type,
         :guest_name, :first_name, :last_name, :email, :mobile,
         :country, :city, :property_name, :topic, :consent, :others,
         :processing_status)
    """
)

_UPDATE_SQL = text(
    """
    UPDATE GHN_CHAT_BOT_CUSTOMER_DETAILS
    SET Conversation_Id = :conversation_id,
        Widget_Id = :widget_id,
        Group_Id = :group_id,
        Form_Id = :form_id,
        Form_Type = :form_type,
        Guest_Name = :guest_name,
        First_Name = :first_name,
        Last_Name = :last_name,
        Email_Id = :email,
        Mobile_number = :mobile,
        Country = :country,
        City = :city,
        Property_Name = :property_name,
        Topic = :topic,
        Consent_Given = :consent,
        Others = :others,
        Processing_Status = :processing_status
    WHERE Session_Id = :session_id
    """
)


_UPDATE_CITY_SQL = text(
    """
    UPDATE GHN_CHAT_BOT_CUSTOMER_DETAILS
    SET City = :city
    WHERE Session_Id = :session_id
    """
)


def _to_int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return None


def _split_name(full_name: str) -> tuple[str, str]:
    parts = str(full_name or "").strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


class CustomerDetailsService:
    """Writes one row per form submission to GHN_CHAT_BOT_CUSTOMER_DETAILS."""

    @staticmethod
    def resolve_widget_group(hotel_code: str) -> tuple[str, str]:
        """Return (widget_id, group_id) for a hotel from
        settings.ticketing_widget_group_map_json. The dashboard scopes the
        guest-name lookup by Widget_Id/Group_Id, so these must be the real
        Kepsla ids (e.g. Zone/Sarovar -> widget 53, group 244). ("","") if none.
        """
        from config.settings import settings
        from services import prebooking_settings_service

        # Ids set in the admin (Bot Training -> Pre-booking -> Ticket routing).
        admin = prebooking_settings_service.ticketing_for(hotel_code)
        if admin is not None and str(admin.get("widget_id") or "").strip():
            return str(admin.get("widget_id") or "").strip(), str(admin.get("group_id") or "").strip()

        raw = str(getattr(settings, "ticketing_widget_group_map_json", "") or "").strip()
        if not raw:
            return "", ""
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return "", ""
        entry = parsed.get(str(hotel_code or "").strip().lower()) if isinstance(parsed, dict) else None
        if not isinstance(entry, dict):
            return "", ""
        return (
            str(entry.get("widget_id") or "").strip(),
            str(entry.get("group_id") or "").strip(),
        )

    @classmethod
    def is_kebo_hotel(cls, hotel_code: str) -> bool:
        """True only for hotels wired to the kebo/Sarovar dashboard (i.e. they
        have a widget/group mapping). Used to scope the kebo-specific writes
        (KEBO_ENGAGE_SESSIONS, CUSTOMER_DETAILS, agent bridge) so they NEVER run
        for KePSLA / other tenants — those keep their own history/handoff/tickets
        unchanged."""
        widget_id, _group_id = cls.resolve_widget_group(hotel_code)
        return bool(widget_id)

    def build_params(
        self,
        *,
        session_id: str,
        guest_name: str = "",
        email: str = "",
        phone: str = "",
        city: str = "",
        country: str = "",
        property_name: str = "",
        topic: str = "",
        conversation_id: str = "",
        widget_id: str = "",
        group_id: str = "",
        form_type: str = "ci_form",
        form_id: Any = None,
        consent: bool = False,
        others: dict[str, Any] | None = None,
        processing_status: str = "PROCESSED",
    ) -> dict[str, Any]:
        first, last = _split_name(guest_name)
        return {
            "session_id": str(session_id or "") or None,
            "conversation_id": (str(conversation_id or "") or None),
            "widget_id": _to_int_or_none(widget_id),
            "group_id": _to_int_or_none(group_id),
            "form_id": _to_int_or_none(form_id),
            "form_type": (str(form_type or "ci_form") or None),
            "guest_name": (str(guest_name or "").strip() or None),
            "first_name": (first or None),
            "last_name": (last or None),
            "email": (str(email or "").strip() or None),
            "mobile": (str(phone or "").strip() or None),
            "country": (str(country or "").strip() or None),
            "city": (str(city or "").strip() or None),
            "property_name": (str(property_name or "").strip() or None),
            "topic": (str(topic or "").strip()[:500] or None),
            "consent": 1 if consent else 0,
            "others": (
                json.dumps(others, ensure_ascii=False, default=str) if others else None
            ),
            "processing_status": (str(processing_status or "PROCESSED") or "PROCESSED"),
        }

    @staticmethod
    def _nonnull_text(value: Any) -> str:
        return str(value or "").strip()

    @classmethod
    def _merge_existing_params(
        cls,
        params: dict[str, Any],
        existing_row: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if not isinstance(existing_row, dict):
            return dict(params)

        merged = dict(params)
        field_map = {
            "conversation_id": "Conversation_Id",
            "widget_id": "Widget_Id",
            "group_id": "Group_Id",
            "form_id": "Form_Id",
            "form_type": "Form_Type",
            "guest_name": "Guest_Name",
            "first_name": "First_Name",
            "last_name": "Last_Name",
            "email": "Email_Id",
            "mobile": "Mobile_number",
            "country": "Country",
            "city": "City",
            "property_name": "Property_Name",
            "topic": "Topic",
            "consent": "Consent_Given",
            "others": "Others",
            "processing_status": "Processing_Status",
        }
        for param_key, db_key in field_map.items():
            current = merged.get(param_key)
            existing = existing_row.get(db_key)
            if current in (None, "", 0, "0") and existing not in (None, ""):
                merged[param_key] = existing
        return merged

    async def write(self, *, db_session: AsyncSession | None = None, **kwargs: Any) -> None:
        try:
            params = self.build_params(**kwargs)
            sid = params.get("session_id")

            async def _insert_once(session: AsyncSession) -> None:
                # Keep ONE identity row per session to avoid dashboard fan-out,
                # but update it in place when later submissions carry richer or
                # corrected details.
                if sid:
                    existing_result = (
                        await session.execute(
                            text(
                                "SELECT Conversation_Id, Widget_Id, Group_Id, Form_Id, Form_Type, "
                                "Guest_Name, First_Name, Last_Name, Email_Id, Mobile_number, "
                                "Country, City, Property_Name, Topic, Consent_Given, Others, "
                                "Processing_Status "
                                "FROM GHN_CHAT_BOT_CUSTOMER_DETAILS "
                                "WHERE Session_Id = :s LIMIT 1"
                            ),
                            {"s": sid},
                        )
                    ).mappings().first()
                    if existing_result:
                        merged = self._merge_existing_params(params, dict(existing_result))
                        await session.execute(_UPDATE_SQL, merged)
                        return
                await session.execute(_INSERT_SQL, params)

            if db_session is not None:
                await _insert_once(db_session)
                # caller owns the transaction
            else:
                from models.database import AsyncSessionLocal

                async with AsyncSessionLocal() as session:
                    await _insert_once(session)
                    await session.commit()
        except Exception as exc:
            logger.warning(
                "customer_details write failed (silent): session_id=%s error=%r",
                kwargs.get("session_id"),
                exc,
            )

    async def update_city(self, *, session_id: str, city: str) -> None:
        """Set City on the EXISTING row for this session. Never inserts.

        The destination a guest picks from the dropdown at the start of a new
        conversation is the current one, so it replaces whatever the opening
        lead form captured days earlier. Update-only on purpose: a pick made
        before any form submit must not create a half-empty identity row.
        """
        sid = str(session_id or "").strip()
        value = str(city or "").strip()
        if not sid or not value:
            return
        try:
            from models.database import AsyncSessionLocal

            async with AsyncSessionLocal() as session:
                await session.execute(
                    _UPDATE_CITY_SQL, {"session_id": sid, "city": value}
                )
                await session.commit()
        except Exception as exc:
            logger.warning(
                "customer_details city update failed (silent): session_id=%s error=%r",
                sid,
                exc,
            )

    def update_city_bg(self, **kwargs: Any) -> None:
        """Schedule update_city() as a background task; never blocks the reply."""
        try:
            asyncio.create_task(self.update_city(**kwargs))
        except RuntimeError:
            pass

    def write_bg(self, **kwargs: Any) -> None:
        """Schedule write() as a background task; never blocks the response."""
        try:
            asyncio.create_task(self.write(**kwargs))
        except RuntimeError:
            # No running event loop (e.g. during tests) — skip silently.
            pass


customer_details_service = CustomerDetailsService()
