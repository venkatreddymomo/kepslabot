"""
Lead-form submissions -> GHN_KEBO_FORM_RESULTS.

That table is what the Kepsla dashboard reads to show what a guest actually
typed into the opening form. It is deliberately small:

    SESSION_ID | FORM_TITLE | FORM_METADATA (json) | AUTO_LOAD_FORM

``FORM_METADATA`` is ``{"fields": [...]}`` where each entry is the stored field
definition plus the guest's ``value`` — the same shape GHN's own bots write, so
their dashboard renders ours identically without a special case. None of the
routing ids (Bot_Id / entity_id / widget_id) belong here; those ride on the
websocket envelope only.

Every write is best-effort: this is a reporting side-effect, and a guest must
never see a form submission fail because a dashboard table was unavailable.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from sqlalchemy import text as _text

logger = logging.getLogger(__name__)


def _clean(value: Any) -> str:
    return str(value or "").strip()


# The submitted payload keys its answers by whatever the widget calls them, and
# those names do not track the field id: the opening form posts `phone` and
# `location`, while the stored fields are `phone_number` and `destination`.
# Matching on the SEMANTIC type instead of the id is what makes the merge
# survive an operator renaming a label.
_FIELD_TYPE_ALIASES: dict[str, tuple[str, ...]] = {
    "FULL_NAME": ("full_name", "fullname", "name", "guest_name"),
    "EMAIL": ("email", "email_id", "guest_email"),
    "MOBILE_NUMBER": ("phone", "phone_number", "mobile", "mobile_number", "guest_phone"),
    "ALTERNATE_MOBILE_NUMBER": ("alternate_phone", "alt_phone", "alternate_mobile_number"),
    "CITY": ("location", "city", "destination", "booking_location"),
}


def build_submitted_fields(
    fields: list[dict[str, Any]],
    answers: dict[str, Any],
) -> list[dict[str, Any]]:
    """Merge the guest's answers into the form's field definitions.

    Looks up each answer by field id, then by the aliases for its semantic
    fieldType, then by label — the widget keys its payload differently
    depending on which path submitted the form.
    """
    out: list[dict[str, Any]] = []
    if not isinstance(fields, list):
        return out

    # Case-insensitive view of the answers so "Full Name" matches "full_name".
    flat: dict[str, Any] = {}
    if isinstance(answers, dict):
        for key, value in answers.items():
            flat[_clean(key).lower()] = value

    for field in fields:
        if not isinstance(field, dict):
            continue
        entry = dict(field)
        fid = _clean(field.get("id"))
        ftype = _clean(field.get("fieldType"))
        label = _clean(field.get("label"))

        candidates: list[str] = [fid]
        candidates.extend(_FIELD_TYPE_ALIASES.get(ftype.upper(), ()))
        candidates.extend([ftype, label])

        value: Any = ""
        for candidate in candidates:
            if not candidate:
                continue
            hit = flat.get(candidate.lower())
            if hit not in (None, ""):
                value = hit
                break
        entry["value"] = _clean(value)
        out.append(entry)
    return out


async def record_submission(
    *,
    session_id: str,
    form_title: str,
    fields: list[dict[str, Any]],
    auto_load_form: bool = True,
    db_session=None,
) -> Optional[int]:
    """Write one submission row. Returns the new row id, or None on failure.

    Runs on its own DB session by default so it can never roll back or hold up
    the form-submit transaction that triggered it.
    """
    session_id = _clean(session_id)
    if not session_id:
        return None
    if not isinstance(fields, list) or not fields:
        return None

    payload = json.dumps({"fields": fields}, ensure_ascii=False)
    params = {
        "sid": session_id[:100],
        # FORM_TITLE is varchar(100) in the live table.
        "title": (_clean(form_title) or "Customer Information")[:100],
        "meta": payload,
        "auto": 1 if auto_load_form else 0,
    }
    statement = _text(
        """
        INSERT INTO GHN_KEBO_FORM_RESULTS
            (SESSION_ID, FORM_TITLE, FORM_METADATA, AUTO_LOAD_FORM)
        VALUES (:sid, :title, :meta, :auto)
        """
    )

    try:
        if db_session is not None:
            result = await db_session.execute(statement, params)
            await db_session.commit()
        else:
            from models.database import AsyncSessionLocal

            async with AsyncSessionLocal() as session:
                result = await session.execute(statement, params)
                await session.commit()
        row_id = getattr(result, "lastrowid", None)
        logger.info(
            "lead form submission recorded session=%s title=%s fields=%d",
            session_id,
            params["title"],
            len(fields),
        )
        return int(row_id) if row_id else None
    except Exception:
        # Missing table or no INSERT grant on this deployment: log once and
        # carry on. The lead itself is already safe in new_bot_sessions.
        logger.warning(
            "lead form submission not recorded for session=%s", session_id, exc_info=True
        )
        return None
