"""
How an OTP actually reaches the guest.

This is the ONLY file that needs to change when real credentials arrive. The
rest of the OTP flow — generation, hashing, expiry, attempt limits, verify,
resend, skip — is provider-agnostic and already finished.

Pick a provider with ``OTP_DELIVERY_PROVIDER`` in .env:

    log    (default) write the code to the application log and return success.
           Lets the whole flow be exercised end-to-end with no account at all.
    smtp   email, via OTP_SMTP_* settings.
    sms    phone, via an HTTP gateway described by OTP_SMS_* settings.

To add a real provider: write a ``_send_via_<name>`` coroutine with the same
signature as ``_send_via_log`` and register it in ``_PROVIDERS``. Nothing else
in the codebase needs to know.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


class DeliveryResult:
    """Outcome of one send attempt."""

    def __init__(self, ok: bool, provider: str, detail: str = "") -> None:
        self.ok = ok
        self.provider = provider
        self.detail = detail

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"DeliveryResult(ok={self.ok}, provider={self.provider!r}, detail={self.detail!r})"


async def _send_via_log(
    *, channel: str, target: str, code: str, context: dict[str, Any]
) -> DeliveryResult:
    """Pretend to send, and log the code so a tester can read it back.

    Deliberately logs at WARNING: this must be impossible to miss in a log, and
    a deployment still running this provider is a deployment where nobody is
    actually receiving codes.
    """
    logger.warning(
        "[OTP:log] channel=%s target=%s code=%s session=%s "
        "(no real delivery — set OTP_DELIVERY_PROVIDER to send for real)",
        channel,
        target,
        code,
        context.get("session_id", ""),
    )
    return DeliveryResult(True, "log", "logged")


async def _send_via_smtp(
    *, channel: str, target: str, code: str, context: dict[str, Any]
) -> DeliveryResult:
    """Email the code. Requires OTP_SMTP_* settings; not configured yet."""
    from config.settings import settings

    host = str(getattr(settings, "otp_smtp_host", "") or "").strip()
    if not host:
        return DeliveryResult(False, "smtp", "OTP_SMTP_HOST is not configured")
    # Intentionally unimplemented until real credentials exist. Returning a
    # failure (rather than raising) keeps the caller's error path identical to
    # a genuine send failure, so this is exercised by the same tests.
    return DeliveryResult(False, "smtp", "smtp provider not implemented yet")


async def _send_via_sms(
    *, channel: str, target: str, code: str, context: dict[str, Any]
) -> DeliveryResult:
    """Send the code by SMS/WhatsApp. Requires OTP_SMS_* settings."""
    from config.settings import settings

    endpoint = str(getattr(settings, "otp_sms_endpoint", "") or "").strip()
    if not endpoint:
        return DeliveryResult(False, "sms", "OTP_SMS_ENDPOINT is not configured")
    return DeliveryResult(False, "sms", "sms provider not implemented yet")


_PROVIDERS: dict[str, Callable[..., Awaitable[DeliveryResult]]] = {
    "log": _send_via_log,
    "smtp": _send_via_smtp,
    "sms": _send_via_sms,
}


def active_provider(channel: str) -> str:
    """Which provider handles this channel, honouring the configured default.

    ``OTP_DELIVERY_PROVIDER=auto`` routes by channel — email over smtp, phone
    over sms — which is what a fully configured deployment wants. Any explicit
    value wins, so a tester can force ``log`` for both.
    """
    from config.settings import settings

    configured = str(getattr(settings, "otp_delivery_provider", "log") or "log").strip().lower()
    if configured and configured != "auto":
        return configured if configured in _PROVIDERS else "log"
    return "smtp" if str(channel or "").lower() == "email" else "sms"


async def send_otp(
    *, channel: str, target: str, code: str, context: dict[str, Any] | None = None
) -> DeliveryResult:
    """Deliver one code. Never raises — callers branch on ``.ok``."""
    provider = active_provider(channel)
    handler = _PROVIDERS.get(provider, _send_via_log)
    try:
        return await handler(
            channel=channel, target=target, code=code, context=context or {}
        )
    except Exception as exc:  # pragma: no cover - provider-specific failures
        logger.exception("OTP delivery failed via %s", provider)
        return DeliveryResult(False, provider, str(exc))
