"""
Admin API for the opening lead-capture form.

Backs the KePSLA "Forms" tab. One form per bot (hotel_code); the definition is
stored in GHN_CHATBOT_FORMS - see services/lead_form_service for why that table
and how rows are scoped.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text as _text
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import get_db
from services import lead_form_service

logger = logging.getLogger(__name__)

admin_router = APIRouter(prefix="/admin/api/forms", tags=["Forms Admin"])


class FormFieldInput(BaseModel):
    label: str
    type: Optional[str] = "text"
    fieldType: Optional[str] = "CUSTOM"
    required: Optional[bool] = False
    placeholder: Optional[str] = None
    validation: Optional[Dict[str, Any]] = None
    textValidationType: Optional[str] = None
    numberValidationType: Optional[str] = None
    options: Optional[List[str]] = None


class OtpConfigInput(BaseModel):
    enabled: Optional[bool] = None
    channel: Optional[str] = None  # phone | email | both
    allowSkip: Optional[bool] = None
    otpLength: Optional[int] = None
    showResendTimer: Optional[bool] = None
    allowChangeNumber: Optional[bool] = None
    maxResendAttempts: Optional[int] = None
    otpValiditySeconds: Optional[int] = None
    resendCooldownSeconds: Optional[int] = None


class SaveLeadForm(BaseModel):
    hotel_code: str
    title: Optional[str] = None
    description: Optional[str] = None
    submit_label: Optional[str] = None
    success_message: Optional[str] = None
    consent: Optional[str] = None
    is_active: Optional[bool] = True
    auto_load_form: Optional[bool] = True
    fields: List[FormFieldInput]
    otp_config: Optional[OtpConfigInput] = None


@admin_router.get("/bots")
async def list_bots(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """The bots an operator can pick in the Forms tab.

    DEFAULT is the fallback config holder, not a real tenant, so it never
    appears as something you can design a guest-facing form for.
    """
    rows = (
        (
            await db.execute(
                _text(
                    "SELECT id, code, name FROM new_bot_hotels "
                    "WHERE is_active = 1 AND UPPER(code) <> 'DEFAULT' "
                    "ORDER BY name"
                )
            )
        )
        .mappings()
        .all()
    )
    return {
        "bots": [
            {"id": int(r["id"]), "hotel_code": r["code"], "name": r["name"]}
            for r in rows
        ]
    }


@admin_router.get("/field-types")
async def list_field_types() -> dict[str, Any]:
    """Semantic field types, with the input type each one implies.

    The editor uses this so an operator picks "Phone number" rather than
    remembering that it must be type=number + numberValidationType=PHONE.
    """
    return {
        "field_types": [
            {"value": "FULL_NAME", "label": "Full name", "type": "text"},
            {"value": "EMAIL", "label": "Email", "type": "email"},
            {"value": "MOBILE_NUMBER", "label": "Phone number", "type": "number"},
            {
                "value": "ALTERNATE_MOBILE_NUMBER",
                "label": "Alternate phone",
                "type": "number",
            },
            {"value": "CITY", "label": "City / destination", "type": "text"},
            {"value": "CUSTOM", "label": "Custom field", "type": "text"},
        ]
    }


@admin_router.get("")
async def get_form(
    hotel_code: str, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """This bot's lead form. Returns the built-in defaults if never saved."""
    try:
        return {"form": await lead_form_service.get_lead_form(db, hotel_code)}
    except Exception as exc:
        logger.exception("lead form read failed for %s", hotel_code)
        raise HTTPException(status_code=500, detail=f"Could not load form: {exc}")


@admin_router.put("")
async def save_form(
    body: SaveLeadForm, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """Create or update this bot's lead form."""
    payload = body.model_dump(exclude_none=True)
    try:
        saved = await lead_form_service.save_lead_form(db, body.hotel_code, payload)
    except ValueError as exc:
        # Operator error (no fields, OTP channel with no matching field) - the
        # editor shows these inline, so they are 400s rather than 500s.
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        await db.rollback()
        logger.exception("lead form save failed for %s", body.hotel_code)
        raise HTTPException(status_code=500, detail=f"Could not save form: {exc}")
    return {"form": saved}
