"""
OTP challenge for the opening lead form.

State lives in ``new_bot_conversations.pending_data['_otp_state']`` — no new
table. That key is whitelisted in conversation_memory_service.safe_keys, or a
flow completing mid-challenge would wipe it and every correct code would come
back "not found".

Everything that matters is enforced HERE, on the server:

  * the code is stored only as a salted hash — a leaked context never reveals it
  * expiry, resend cooldown, resend cap and wrong-attempt cap are all checked
    against stored timestamps, never against what the client sends
  * ``skip`` is a recorded decision, not a client-side dismissal, so a lead that
    was never verified is distinguishable afterwards

The client is told ``expiresAt`` / ``resendAttemptsLeft`` for display only.

Action names and error codes are copied verbatim from the existing kebo widget
(verify_otp / resend_otp / skip_otp_validation / INVALID_OTP / OTP_EXPIRED /
OTP_NOT_FOUND / MAX_RESEND_ATTEMPTS) so its screens work against this
unmodified.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from typing import Any, Optional

from sqlalchemy import text as _text

logger = logging.getLogger(__name__)

STATE_KEY = "_otp_state"

# Error codes the widget branches on.
ERR_INVALID = "INVALID_OTP"
ERR_EXPIRED = "OTP_EXPIRED"
ERR_NOT_FOUND = "OTP_NOT_FOUND"
ERR_MAX_RESEND = "MAX_RESEND_ATTEMPTS"
ERR_COOLDOWN = "RESEND_COOLDOWN"
ERR_MAX_ATTEMPTS = "MAX_VERIFY_ATTEMPTS"
ERR_DELIVERY = "DELIVERY_FAILED"

# A wrong-guess ceiling the config has no field for: without it a 6-digit code
# is brute-forceable within its own validity window.
MAX_VERIFY_ATTEMPTS = 5


def _now_ms() -> int:
    return int(time.time() * 1000)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _hash_code(code: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{code}".encode("utf-8")).hexdigest()


def _generate_code(length: int) -> str:
    length = max(4, min(8, int(length or 6)))
    return "".join(secrets.choice("0123456789") for _ in range(length))


# --------------------------------------------------------------------------
# State persistence — read/modify/write on the conversation's pending_data
# --------------------------------------------------------------------------

async def _load_state(session_id: str) -> tuple[Optional[dict[str, Any]], Optional[int]]:
    """Return (otp_state, conversation_row_id) for the newest conversation."""
    import json

    from models.database import AsyncSessionLocal

    session_id = _clean(session_id)
    if not session_id:
        return None, None
    try:
        async with AsyncSessionLocal() as db:
            row = (
                await db.execute(
                    _text(
                        "SELECT id, pending_data FROM new_bot_conversations "
                        "WHERE session_id = :sid ORDER BY id DESC LIMIT 1"
                    ),
                    {"sid": session_id},
                )
            ).fetchone()
        if not row:
            return None, None
        raw = row[1]
        if isinstance(raw, (str, bytes, bytearray)):
            raw = json.loads(raw or "{}")
        if not isinstance(raw, dict):
            return None, int(row[0])
        state = raw.get(STATE_KEY)
        return (state if isinstance(state, dict) else None), int(row[0])
    except Exception:
        logger.warning("otp: state load failed for %s", session_id, exc_info=True)
        return None, None


async def _save_state(session_id: str, state: Optional[dict[str, Any]]) -> bool:
    """Merge the OTP state into pending_data. ``None`` clears it."""
    import json

    from models.database import AsyncSessionLocal

    session_id = _clean(session_id)
    if not session_id:
        return False
    try:
        async with AsyncSessionLocal() as db:
            row = (
                await db.execute(
                    _text(
                        "SELECT id, pending_data FROM new_bot_conversations "
                        "WHERE session_id = :sid ORDER BY id DESC LIMIT 1"
                    ),
                    {"sid": session_id},
                )
            ).fetchone()
            if not row:
                logger.warning("otp: no conversation row for session %s", session_id)
                return False
            raw = row[1]
            if isinstance(raw, (str, bytes, bytearray)):
                raw = json.loads(raw or "{}")
            data = dict(raw) if isinstance(raw, dict) else {}
            if state is None:
                data.pop(STATE_KEY, None)
            else:
                data[STATE_KEY] = state
            await db.execute(
                _text("UPDATE new_bot_conversations SET pending_data = :pd WHERE id = :rid"),
                {"pd": json.dumps(data, ensure_ascii=False), "rid": int(row[0])},
            )
            await db.commit()
        return True
    except Exception:
        logger.warning("otp: state save failed for %s", session_id, exc_info=True)
        return False


# --------------------------------------------------------------------------
# Public surface
# --------------------------------------------------------------------------

def _details_for_client(state: dict[str, Any]) -> dict[str, Any]:
    """The otpDetails envelope the widget renders — never the code itself."""
    return {
        "otpId": state.get("otp_id", ""),
        "sentAt": state.get("sent_at", 0),
        "expiresAt": state.get("expires_at", 0),
        "sessionId": state.get("session_id", ""),
        "mobileNumber": state.get("target", ""),
        "channel": state.get("channel", "phone"),
        "nextResendAt": state.get("next_resend_at", 0),
        "totalAttempts": state.get("max_resend", 0),
        "otpValiditySeconds": state.get("validity_seconds", 0),
        "resendAttemptsLeft": state.get("resend_left", 0),
        "resendCooldownSeconds": state.get("cooldown_seconds", 0),
    }


async def start_challenge(
    *,
    session_id: str,
    channel: str,
    target: str,
    otp_config: dict[str, Any],
) -> dict[str, Any]:
    """Generate, store and send a code. Returns the client-facing result."""
    session_id = _clean(session_id)
    target = _clean(target)
    channel = _clean(channel).lower() or "phone"
    if not target:
        return {"success": False, "error_code": ERR_DELIVERY, "message": "No destination for the code."}

    cfg = otp_config if isinstance(otp_config, dict) else {}
    length = int(cfg.get("otpLength") or 6)
    validity = int(cfg.get("otpValiditySeconds") or 300)
    cooldown = int(cfg.get("resendCooldownSeconds") or 60)
    max_resend = int(cfg.get("maxResendAttempts") or 3)

    code = _generate_code(length)
    salt = secrets.token_hex(8)
    now = _now_ms()
    state: dict[str, Any] = {
        "otp_id": f"otp_{secrets.token_hex(5)}",
        "session_id": session_id,
        "channel": channel,
        "target": target,
        "salt": salt,
        "code_hash": _hash_code(code, salt),
        "sent_at": now,
        "expires_at": now + validity * 1000,
        "next_resend_at": now + cooldown * 1000,
        "validity_seconds": validity,
        "cooldown_seconds": cooldown,
        "max_resend": max_resend,
        "resend_left": max_resend,
        "verify_attempts": 0,
        "code_length": length,
        # Snapshot the skip policy as it stood when the challenge began, so an
        # operator turning skip off mid-session cannot strand a guest who is
        # already looking at a Skip button.
        "allow_skip": bool(cfg.get("allowSkip", True)),
        "verified": False,
        "skipped": False,
    }

    from services import otp_delivery

    delivery = await otp_delivery.send_otp(
        channel=channel, target=target, code=code, context={"session_id": session_id}
    )
    if not delivery.ok:
        return {
            "success": False,
            "error_code": ERR_DELIVERY,
            "message": "Could not send the code. Please try again.",
            "detail": delivery.detail,
        }

    await _save_state(session_id, state)
    logger.info(
        "otp: challenge started session=%s channel=%s provider=%s",
        session_id,
        channel,
        delivery.provider,
    )
    return {"success": True, "otpSent": True, "otpDetails": _details_for_client(state)}


async def verify(*, session_id: str, otp_id: str, code: str) -> dict[str, Any]:
    """Check a code. Consumes an attempt on every wrong guess."""
    state, _ = await _load_state(session_id)
    if not state or (otp_id and _clean(otp_id) != state.get("otp_id")):
        return {"success": False, "verified": False, "error_code": ERR_NOT_FOUND,
                "message": "That code has expired. Please request a new one."}

    if state.get("verified"):
        return {"success": True, "verified": True, "message": "Already verified."}

    if _now_ms() > int(state.get("expires_at") or 0):
        return {"success": False, "verified": False, "error_code": ERR_EXPIRED,
                "message": "OTP has expired. Please request a new one."}

    attempts = int(state.get("verify_attempts") or 0)
    if attempts >= MAX_VERIFY_ATTEMPTS:
        return {"success": False, "verified": False, "error_code": ERR_MAX_ATTEMPTS,
                "message": "Too many incorrect attempts. Please request a new code."}

    supplied = _clean(code)
    expected = str(state.get("code_hash") or "")
    # compare_digest: a plain == leaks timing information about the code.
    if not supplied or not hmac.compare_digest(
        _hash_code(supplied, str(state.get("salt") or "")), expected
    ):
        state["verify_attempts"] = attempts + 1
        await _save_state(session_id, state)
        return {"success": False, "verified": False, "error_code": ERR_INVALID,
                "message": "Invalid OTP. Please try again.",
                "attemptsLeft": max(0, MAX_VERIFY_ATTEMPTS - state["verify_attempts"])}

    state["verified"] = True
    state["verified_at"] = _now_ms()
    # The hash is no longer needed once verified; drop it rather than leave a
    # usable challenge sitting in the conversation.
    state.pop("code_hash", None)
    state.pop("salt", None)
    await _save_state(session_id, state)
    logger.info("otp: verified session=%s", session_id)
    return {"success": True, "verified": True, "message": "Verified successfully.",
            "render_type": "success_message"}


async def resend(*, session_id: str, otp_id: str = "") -> dict[str, Any]:
    """Issue a fresh code, honouring the cooldown and the resend cap."""
    state, _ = await _load_state(session_id)
    if not state or (otp_id and _clean(otp_id) != state.get("otp_id")):
        return {"success": False, "error_code": ERR_NOT_FOUND,
                "message": "Nothing to resend. Please start again."}

    if int(state.get("resend_left") or 0) <= 0:
        return {"success": False, "error": ERR_MAX_RESEND, "error_code": ERR_MAX_RESEND,
                "message": "Maximum resend attempts reached. Please try again later."}

    now = _now_ms()
    next_allowed = int(state.get("next_resend_at") or 0)
    if now < next_allowed:
        return {"success": False, "error_code": ERR_COOLDOWN,
                "message": "Please wait before requesting another code.",
                "retryInSeconds": max(1, (next_allowed - now) // 1000)}

    length = int(state.get("code_length") or 6)
    validity = int(state.get("validity_seconds") or 300)
    cooldown = int(state.get("cooldown_seconds") or 60)
    code = _generate_code(length)
    salt = secrets.token_hex(8)

    from services import otp_delivery

    delivery = await otp_delivery.send_otp(
        channel=str(state.get("channel") or "phone"),
        target=str(state.get("target") or ""),
        code=code,
        context={"session_id": session_id},
    )
    if not delivery.ok:
        return {"success": False, "error_code": ERR_DELIVERY,
                "message": "Could not resend the code. Please try again."}

    state.update({
        "salt": salt,
        "code_hash": _hash_code(code, salt),
        "sent_at": now,
        "expires_at": now + validity * 1000,
        "next_resend_at": now + cooldown * 1000,
        "resend_left": int(state.get("resend_left") or 0) - 1,
        # A resend is a fresh challenge: previous wrong guesses must not carry
        # over and lock the guest out of a code they have not even seen yet.
        "verify_attempts": 0,
    })
    await _save_state(session_id, state)
    logger.info("otp: resent session=%s left=%s", session_id, state["resend_left"])
    return {"success": True, "otpSent": True, "message": "OTP resent successfully",
            "otpDetails": _details_for_client(state)}


async def change_target(
    *, session_id: str, new_target: str, otp_config: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Point the challenge at a new number/address and send a fresh code."""
    state, _ = await _load_state(session_id)
    cfg = dict(otp_config or {})
    if state:
        # Carry the live config forward so a change of number cannot silently
        # reset validity or the resend budget to defaults.
        cfg.setdefault("otpLength", state.get("code_length"))
        cfg.setdefault("otpValiditySeconds", state.get("validity_seconds"))
        cfg.setdefault("resendCooldownSeconds", state.get("cooldown_seconds"))
        cfg.setdefault("maxResendAttempts", state.get("max_resend"))
    channel = str((state or {}).get("channel") or "phone")
    return await start_challenge(
        session_id=session_id, channel=channel, target=new_target, otp_config=cfg
    )


async def skip(*, session_id: str, otp_id: str = "") -> dict[str, Any]:
    """Record that the guest declined verification.

    Only honoured when the saved config allows it — a client that posts
    ``skip_otp_validation`` against a form with allowSkip off gets refused.
    """
    state, _ = await _load_state(session_id)
    if not state:
        return {"success": False, "error_code": ERR_NOT_FOUND,
                "message": "Nothing to skip."}
    if not bool(state.get("allow_skip", True)):
        return {"success": False, "error_code": "SKIP_NOT_ALLOWED",
                "message": "Verification is required to continue."}

    state["skipped"] = True
    state["skipped_at"] = _now_ms()
    state.pop("code_hash", None)
    state.pop("salt", None)
    await _save_state(session_id, state)
    logger.info("otp: skipped session=%s", session_id)
    return {"success": True, "skipped": True, "render_type": "otp_skip_result",
            "message": "Verification skipped."}


async def status(session_id: str) -> dict[str, Any]:
    """Whether this session verified, skipped, or never completed a challenge."""
    state, _ = await _load_state(session_id)
    if not state:
        return {"exists": False, "verified": False, "skipped": False}
    return {
        "exists": True,
        "verified": bool(state.get("verified")),
        "skipped": bool(state.get("skipped")),
        "channel": state.get("channel", ""),
        "target": state.get("target", ""),
    }
