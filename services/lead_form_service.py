"""
Lead-form service - read/write the opening lead-capture form definition.

The definition lives in GHN_CHATBOT_FORMS, the table the Lumira/SIA widget
already renders its forms from. We do not own that table, so this module is
deliberately narrow:

  * Rows we own are keyed by ``Bot_Id`` = our own ``new_bot_hotels.id`` as a
    string (67 = Zone, 68 = Sarovar, 69 = Mango). Lumira's own bots use a
    disjoint set of ids (10, 18, 53, 55, 57, 60, 61, 91), so our rows can never
    shadow theirs.
  * ``Form_Key`` is NOT unique in this table - the same key exists once per bot
    (``otp_validation_form`` appears five times). Every statement here is
    therefore scoped by BOTH ``Bot_Id`` AND ``Form_Key``. Scoping by Form_Key
    alone would edit another tenant's form.

The JSON shape mirrors the rows Lumira already stores, field for field, so the
existing widget can render ours without a translation layer: fields carry a
semantic ``fieldType`` (FULL_NAME / EMAIL / MOBILE_NUMBER / CITY) rather than
relying on their label, and OTP settings live under ``otp_validation_config``.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from sqlalchemy import text as _text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# The key Lumira's newest bots use for the auto-loading customer-information
# form (bots 60/61, and the live SIA widget's bot_config.formKey). Matching it
# keeps our rows readable by the same frontend code.
LEAD_FORM_KEY = "customer_information"

# Semantic field types the backend recognises. The label is free text and may be
# renamed by an operator; the fieldType is what code keys off (which field is
# the phone number, which is the email) so renaming a label never breaks OTP.
FIELD_TYPES = (
    "FULL_NAME",
    "EMAIL",
    "MOBILE_NUMBER",
    "ALTERNATE_MOBILE_NUMBER",
    "CITY",
    "CUSTOM",
)

# Zone's four hardcoded fields, as they render today. A hotel with no row yet
# gets exactly this, so turning the feature on changes nothing until someone
# actually edits the form.
DEFAULT_FIELDS: list[dict[str, Any]] = [
    {
        "type": "text",
        "label": "Full name",
        "order": 1,
        "required": True,
        "fieldType": "FULL_NAME",
        "placeholder": "Your full name",
        "validation": {"minLength": 2, "maxLength": 40},
        "textValidationType": "PLAIN",
    },
    {
        # Rendered with the country-code picker beside it, as today.
        "type": "tel",
        "label": "Phone number",
        "order": 2,
        "required": True,
        "fieldType": "MOBILE_NUMBER",
        "placeholder": "Phone number",
        "numberValidationType": "PHONE",
    },
    {
        "type": "email",
        "label": "Email",
        "order": 3,
        "required": True,
        "fieldType": "EMAIL",
        "placeholder": "you@example.com",
        "validation": {},
        "textValidationType": "EMAIL",
    },
    {
        # A dropdown, not a free-text box: the options are the live property
        # manifest, so they are filled in at render time rather than stored.
        "type": "select",
        "label": "Destination",
        "order": 4,
        "required": True,
        "fieldType": "CITY",
        "placeholder": "Choose the city you want to stay in…",
        "options": [],
    },
]

# OTP off by default: an existing tenant must opt in, never wake up to a new
# verification wall in front of their lead form.
DEFAULT_OTP_CONFIG: dict[str, Any] = {
    "enabled": False,
    # "phone" | "email" | "both" - Lumira's own rows have no channel key at all
    # (their OTP is WhatsApp-only). This is our extension; readers that don't
    # know it simply fall back to phone.
    "channel": "phone",
    "allowSkip": True,
    "otpLength": 6,
    "showResendTimer": True,
    "allowChangeNumber": True,
    "maxResendAttempts": 3,
    "otpValiditySeconds": 300,
    "resendCooldownSeconds": 60,
    "otpValidationFormId": "otp_validation_form",
}

# The exact strings the widget renders today: heading and subtitle come from
# widget_deployment_service.LEAD_FORM_DEFAULTS, the button label is inlined in
# the form's JSX. Keeping them identical means a hotel that never edits the
# form sees no change at all once the widget starts reading from here.
DEFAULT_COPY: dict[str, str] = {
    "title": "Hey there! Let's get you set up \U0001f60a",
    "description": (
        "Just a few quick details so we can tailor your stay — your name, "
        "number, email, and the destination you have in mind. Then we'll dive "
        "right in!"
    ),
    "submit_label": "Continue",
    "success_message": "",
    # Small print under the form. Empty by default: only a hotel that has
    # actually agreed consent wording gets a consent line rendered.
    "consent": "",
}


def _clean(value: Any) -> str:
    return str(value or "").strip()


async def hotel_id_for_code(db: AsyncSession, hotel_code: str) -> Optional[int]:
    """Our ``new_bot_hotels.id`` for a hotel_code - the Bot_Id we write under."""
    code = _clean(hotel_code).upper()
    if not code:
        return None
    row = (
        await db.execute(
            _text("SELECT id FROM new_bot_hotels WHERE UPPER(code) = :c LIMIT 1"),
            {"c": code},
        )
    ).fetchone()
    return int(row[0]) if row else None


def _coerce_content(raw: Any) -> dict[str, Any]:
    """``Content-JSON`` comes back as dict on some drivers, str on others."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (str, bytes)):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def slugify_label(label: str) -> str:
    """A field id in the shape GHN submissions use: "Phone number" -> phone_number.

    Submissions stored in GHN_KEBO_FORM_RESULTS carry an ``id`` per field
    alongside ``fieldType``, derived from the label. Generating it here (rather
    than at submit time) keeps the id stable even if someone later renames the
    label, so historical submissions stay comparable.
    """
    text = re.sub(r"[^a-z0-9]+", "_", _clean(label).lower()).strip("_")
    return text or "field"


def _normalize_field(field: Any, index: int) -> Optional[dict[str, Any]]:
    """One editor row -> the stored field shape. Returns None for junk."""
    if not isinstance(field, dict):
        return None
    label = _clean(field.get("label"))
    if not label:
        return None
    field_type = _clean(field.get("fieldType")).upper() or "CUSTOM"
    if field_type not in FIELD_TYPES:
        field_type = "CUSTOM"

    out: dict[str, Any] = {
        # Keep an explicitly supplied id: renaming a label must not silently
        # repoint an existing field at a new id.
        "id": _clean(field.get("id")) or slugify_label(label),
        "type": _clean(field.get("type")) or "text",
        "label": label,
        "order": index + 1,
        "required": bool(field.get("required", False)),
        "fieldType": field_type,
        "placeholder": _clean(field.get("placeholder")) or f"Enter {label.lower()}",
    }
    if isinstance(field.get("validation"), dict):
        out["validation"] = field["validation"]
    for key in ("textValidationType", "numberValidationType"):
        value = _clean(field.get(key))
        if value:
            out[key] = value
    if isinstance(field.get("options"), list):
        out["options"] = [_clean(o) for o in field["options"] if _clean(o)]
    return out


def _normalize_otp_config(raw: Any) -> dict[str, Any]:
    """Merge a submitted OTP block over the defaults, clamping the numbers."""
    out = dict(DEFAULT_OTP_CONFIG)
    if not isinstance(raw, dict):
        return out

    channel = _clean(raw.get("channel")).lower()
    if channel in ("phone", "email", "both"):
        out["channel"] = channel

    for key in ("enabled", "allowSkip", "showResendTimer", "allowChangeNumber"):
        if key in raw:
            out[key] = bool(raw[key])

    # Bounds keep an operator typo from producing a 1-digit code or a 12-hour
    # window; the DB has no constraints of its own.
    for key, low, high in (
        ("otpLength", 4, 8),
        ("maxResendAttempts", 0, 10),
        ("otpValiditySeconds", 30, 1800),
        ("resendCooldownSeconds", 0, 600),
    ):
        if key in raw:
            try:
                out[key] = max(low, min(high, int(raw[key])))
            except (TypeError, ValueError):
                pass
    return out


def _defaults(hotel_code: str) -> dict[str, Any]:
    return {
        "hotel_code": _clean(hotel_code).upper(),
        "form_key": LEAD_FORM_KEY,
        "exists": False,
        "is_active": True,
        "auto_load_form": True,
        # Run the built-in fields through the same normaliser a saved form gets,
        # so an unsaved hotel still carries a field `id`. Returning them raw meant
        # submissions from a hotel nobody had opened in the Forms tab recorded
        # `id: null` on every field.
        "fields": [f for f in (_normalize_field(dict(x), i) for i, x in enumerate(DEFAULT_FIELDS)) if f],
        "otp_config": dict(DEFAULT_OTP_CONFIG),
        **DEFAULT_COPY,
    }


async def get_lead_form(db: AsyncSession, hotel_code: str) -> dict[str, Any]:
    """The saved lead form for a hotel, or the built-in defaults if unsaved.

    Never raises for a missing row: a hotel that has never opened the Forms tab
    still renders the form it renders today.
    """
    out = _defaults(hotel_code)
    bot_id = await hotel_id_for_code(db, hotel_code)
    if bot_id is None:
        return out
    out["bot_id"] = bot_id

    row = (
        (
            await db.execute(
                _text(
                    """
                    SELECT ID, Title, Description, Submit_Label, Success_Message,
                           Is_Active, auto_load_form, `Content-JSON` AS content
                    FROM GHN_CHATBOT_FORMS
                    WHERE Bot_Id = :bot AND Form_Key = :fk
                    LIMIT 1
                    """
                ),
                {"bot": str(bot_id), "fk": LEAD_FORM_KEY},
            )
        )
        .mappings()
        .fetchone()
    )
    if not row:
        return out

    content = _coerce_content(row["content"])
    fields = content.get("fields")
    otp_raw = content.get("otp_validation_config")

    out.update(
        {
            "exists": True,
            "id": int(row["ID"]),
            "title": _clean(row["Title"]) or out["title"],
            "description": _clean(row["Description"]) or out["description"],
            "submit_label": _clean(row["Submit_Label"]) or out["submit_label"],
            "success_message": _clean(row["Success_Message"]) or out["success_message"],
            # Consent has no column of its own in GHN_CHATBOT_FORMS, so it rides
            # in Content-JSON. An empty saved value is meaningful (the hotel
            # removed its consent line), so this does NOT fall back to defaults.
            "consent": _clean(content.get("consent")),
            "is_active": bool(row["Is_Active"]),
            "auto_load_form": bool(row["auto_load_form"]),
        }
    )
    if isinstance(fields, list) and fields:
        normalized = [_normalize_field(f, i) for i, f in enumerate(fields)]
        out["fields"] = [f for f in normalized if f]
    if isinstance(otp_raw, dict):
        out["otp_config"] = _normalize_otp_config(otp_raw)
    return out


async def save_lead_form(
    db: AsyncSession, hotel_code: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Upsert this hotel's lead form. Scoped by (Bot_Id, Form_Key)."""
    bot_id = await hotel_id_for_code(db, hotel_code)
    if bot_id is None:
        raise ValueError(f"Unknown hotel_code: {hotel_code!r}")

    raw_fields = payload.get("fields")
    if not isinstance(raw_fields, list) or not raw_fields:
        raise ValueError("A form needs at least one field.")
    normalized = [_normalize_field(f, i) for i, f in enumerate(raw_fields)]
    fields = [f for f in normalized if f]
    if not fields:
        raise ValueError("A form needs at least one field with a label.")

    otp_config = _normalize_otp_config(payload.get("otp_config"))

    # OTP needs something to send to. Refuse a config that verifies a channel
    # the form does not collect, rather than failing later at send time.
    present = {f["fieldType"] for f in fields}
    if otp_config["enabled"]:
        channel = otp_config["channel"]
        if channel in ("phone", "both") and "MOBILE_NUMBER" not in present:
            raise ValueError(
                "OTP over phone needs a field of type MOBILE_NUMBER on this form."
            )
        if channel in ("email", "both") and "EMAIL" not in present:
            raise ValueError("OTP over email needs a field of type EMAIL on this form.")

    content = {
        "fields": fields,
        "otp_validation_config": otp_config,
        "consent": _clean(payload.get("consent")),
        "nextActions": [{"type": "cta_button", "order": 1, "value": "ALL_CTAS"}],
    }
    params = {
        "bot": str(bot_id),
        "fk": LEAD_FORM_KEY,
        # Title is varchar(100) and Success_Message varchar(100) in the live
        # table - truncate rather than let MySQL reject the whole save.
        "title": (_clean(payload.get("title")) or DEFAULT_COPY["title"])[:100],
        "desc": (_clean(payload.get("description")) or DEFAULT_COPY["description"])[
            :1000
        ],
        "submit": (
            _clean(payload.get("submit_label")) or DEFAULT_COPY["submit_label"]
        )[:100],
        "success": (
            _clean(payload.get("success_message")) or DEFAULT_COPY["success_message"]
        )[:100],
        "active": 1 if payload.get("is_active", True) else 0,
        "auto": 1 if payload.get("auto_load_form", True) else 0,
        "cj": json.dumps(content, ensure_ascii=False),
    }

    existing = (
        await db.execute(
            _text(
                "SELECT ID FROM GHN_CHATBOT_FORMS "
                "WHERE Bot_Id = :bot AND Form_Key = :fk LIMIT 1"
            ),
            {"bot": str(bot_id), "fk": LEAD_FORM_KEY},
        )
    ).fetchone()

    if existing:
        await db.execute(
            _text(
                """
                UPDATE GHN_CHATBOT_FORMS
                SET Title           = :title,
                    Description     = :desc,
                    Submit_Label    = :submit,
                    Success_Message = :success,
                    `Content-JSON`  = :cj,
                    Is_CI_Form      = 1,
                    auto_load_form  = :auto,
                    Is_Active       = :active
                WHERE Bot_Id = :bot AND Form_Key = :fk
                """
            ),
            params,
        )
        action = "updated"
    else:
        await db.execute(
            _text(
                """
                INSERT INTO GHN_CHATBOT_FORMS
                    (Bot_Id, Title, Description, `Content-JSON`, Form_Key,
                     Submit_Label, Success_Message, Is_CI_Form,
                     auto_load_form, Is_Active)
                VALUES
                    (:bot, :title, :desc, :cj, :fk,
                     :submit, :success, 1, :auto, :active)
                """
            ),
            params,
        )
        action = "created"

    await db.commit()
    logger.info("lead form %s for hotel_code=%s (Bot_Id=%s)", action, hotel_code, bot_id)

    saved = await get_lead_form(db, hotel_code)
    saved["action"] = action
    return saved
