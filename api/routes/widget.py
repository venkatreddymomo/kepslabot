"""
Widget API Routes

Public endpoints used by the embeddable loader, plus admin endpoints for
managing widget deployments. All admin endpoints live under /admin/api/widget/*
so they sit alongside the rest of the admin surface; the public bootstrap lives
under /api/widget/bootstrap.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, File, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel

from services import prebooking_settings_service
from services.widget_deployment_service import widget_deployment_service


public_router = APIRouter(prefix="/api/widget", tags=["Widget Public"])
admin_router = APIRouter(prefix="/admin/api/widget", tags=["Widget Admin"])


# ---- Pydantic models -----------------------------------------------------


class ThemeIn(BaseModel):
    brand_color: Optional[str] = None
    accent_color: Optional[str] = None
    bg_color: Optional[str] = None
    text_color: Optional[str] = None


class SizeIn(BaseModel):
    width: Optional[int] = None
    height: Optional[int] = None


class CreateDeployment(BaseModel):
    hotel_code: str
    name: Optional[str] = None
    allowed_origins: Optional[List[str]] = None
    theme: Optional[ThemeIn] = None
    size: Optional[SizeIn] = None
    position: Optional[str] = None
    bot_name: Optional[str] = None
    phase: Optional[str] = None
    auto_open: Optional[bool] = None
    status: Optional[str] = None
    logo_url: Optional[str] = None
    session_ttl_hours: Optional[int] = None
    idle_timeout_minutes: Optional[int] = None
    # Pin every guest turn on this embed to one chat model (and that model's
    # prompt variant), immune to the /chat harness dropdown. "" / null = unpinned.
    model: Optional[str] = None


class UpdateDeployment(BaseModel):
    hotel_code: Optional[str] = None
    name: Optional[str] = None
    allowed_origins: Optional[List[str]] = None
    theme: Optional[ThemeIn] = None
    size: Optional[SizeIn] = None
    position: Optional[str] = None
    bot_name: Optional[str] = None
    phase: Optional[str] = None
    auto_open: Optional[bool] = None
    status: Optional[str] = None
    logo_url: Optional[str] = None
    session_ttl_hours: Optional[int] = None
    idle_timeout_minutes: Optional[int] = None
    # See CreateDeployment.model. "" clears the pin (back to harness/env default).
    model: Optional[str] = None


# ---- Public bootstrap ----------------------------------------------------


def _host_origin(request: Request) -> str:
    """Build the origin the loader should use for the iframe URL."""
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme or "http"
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    if not host:
        return f"{proto}://{request.url.hostname}"
    return f"{proto}://{host}"


@public_router.get("/menu/{hotel_code}/{menu_id}")
async def menu_file(hotel_code: str, menu_id: str):
    """A menu PDF uploaded in Bot Training -> Pre-booking, by a link that never
    expires: chat history keeps this link, and every open is redirected to a
    fresh short-lived S3 link."""
    from fastapi.responses import RedirectResponse

    from services.s3_storage_service import s3_storage_service

    code = hotel_code.strip().lower()
    await prebooking_settings_service.refresh_if_stale(code)
    menu = prebooking_settings_service.menu_by_id(code, menu_id)
    if not menu:
        raise HTTPException(status_code=404, detail="Menu not found")
    url = s3_storage_service.resolve_display_url(str(menu.get("stored") or ""))
    if not url:
        raise HTTPException(status_code=404, detail="Menu not available")
    return RedirectResponse(url=url, status_code=302, headers={"Cache-Control": "no-store"})


@public_router.get("/bootstrap")
async def bootstrap(
    request: Request,
    response: Response,
    widget_key: Optional[str] = None,
    hotel_code: Optional[str] = None,
):
    """
    Public, unauthenticated bootstrap endpoint consumed by the loader.

    The response only contains embed-safe fields (no secrets, no PII). The loader
    uses it to build the iframe URL and theme the launcher button.
    """
    # Never cache: this carries the live DB deployment theme (brand color, bot
    # name). Caching it would re-introduce the "old colors stick around" bug after
    # an operator changes branding in Admin > Widget.
    response.headers["Cache-Control"] = "no-store"
    host = _host_origin(request)
    origin = (request.headers.get("origin") or request.headers.get("referer") or "").strip()

    # Path 1: widget_key supplied — primary, production path.
    if widget_key:
        payload = await widget_deployment_service.bootstrap_payload(widget_key, host_origin=host)
        if not payload:
            raise HTTPException(status_code=404, detail="Unknown or inactive widget_key")
        if not await widget_deployment_service.origin_allowed(widget_key, origin):
            # Soft-fail: loader will gracefully not render. Return 403 so the
            # browser surfaces the issue in devtools without leaking config.
            raise HTTPException(status_code=403, detail="Origin not allowed for this widget")
        payload["lead_form"] = await widget_deployment_service.lead_form_for_hotel(
            payload.get("hotel_code") or ""
        )
        payload["welcome_service_ctas"] = widget_deployment_service.welcome_service_ctas_for_hotel(
            payload.get("hotel_code") or ""
        )
        # Admin-managed hotels (Bot Training -> Pre-booking): opening buttons and
        # what follows the lead form. None for every other hotel.
        payload["prebooking"] = await prebooking_settings_service.welcome_config_for(
            payload.get("hotel_code") or ""
        )
        return payload

    # Path 2: hotel_code supplied — MVP fallback so existing snippets keep working.
    # Prefers the hotel's active deployment branding (so a direct /chat?hotel_code=
    # page matches the operator-configured theme), else property-JSON defaults.
    if hotel_code:
        defaults = await widget_deployment_service.public_defaults_for_hotel(hotel_code.strip().lower())
        return {
            "widget_key": None,
            "hotel_code": hotel_code.strip().lower(),
            "bot_name": defaults["bot_name"],
            "phase": defaults["phase"],
            "theme": defaults["theme"],
            "size": defaults["size"],
            "position": defaults["position"],
            "auto_open": defaults["auto_open"],
            "logo_url": defaults.get("logo_url", ""),
            "session_ttl_hours": defaults.get("session_ttl_hours", 24),
            "idle_timeout_minutes": defaults.get("idle_timeout_minutes", 30),
            "lead_form": await widget_deployment_service.lead_form_for_hotel(
                hotel_code.strip().lower()
            ),
            "welcome_service_ctas": widget_deployment_service.welcome_service_ctas_for_hotel(
                hotel_code.strip().lower()
            ),
            "prebooking": await prebooking_settings_service.welcome_config_for(
                hotel_code.strip().lower()
            ),
            "iframe_url": f"{host}/chat",
        }

    raise HTTPException(status_code=400, detail="widget_key or hotel_code required")


# ---- Admin CRUD ----------------------------------------------------------


@admin_router.get("/deployments")
async def list_deployments(hotel_code: Optional[str] = None):
    return {"deployments": await widget_deployment_service.list(hotel_code=hotel_code)}


@admin_router.get("/deployments/{widget_key}")
async def get_deployment(widget_key: str):
    dep = await widget_deployment_service.get(widget_key)
    if not dep:
        raise HTTPException(status_code=404, detail="Deployment not found")
    return dep


@admin_router.post("/deployments")
async def create_deployment(body: CreateDeployment):
    payload: Dict[str, Any] = body.model_dump(exclude_none=True)
    if not payload.get("hotel_code"):
        raise HTTPException(status_code=400, detail="hotel_code is required")
    try:
        return await widget_deployment_service.create(payload["hotel_code"], payload)
    except ValueError as exc:
        # e.g. an unsupported `model` id — reject rather than pin a customer to
        # a model the build can't route.
        raise HTTPException(status_code=400, detail=str(exc))


@admin_router.put("/deployments/{widget_key}")
async def update_deployment(widget_key: str, body: UpdateDeployment):
    payload: Dict[str, Any] = body.model_dump(exclude_none=True)
    try:
        updated = await widget_deployment_service.update(widget_key, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not updated:
        raise HTTPException(status_code=404, detail="Deployment not found")
    return updated


@admin_router.delete("/deployments/{widget_key}")
async def delete_deployment(widget_key: str):
    ok = await widget_deployment_service.delete(widget_key)
    if not ok:
        raise HTTPException(status_code=404, detail="Deployment not found")
    return {"ok": True}


@admin_router.post("/deployments/{widget_key}/rotate-key")
async def rotate_key(widget_key: str):
    rotated = await widget_deployment_service.rotate_key(widget_key)
    if not rotated:
        raise HTTPException(status_code=404, detail="Deployment not found")
    return rotated


@admin_router.post("/logo-upload")
async def upload_widget_logo(file: UploadFile = File(...)):
    """Upload a logo image and return its static URL."""
    import shutil
    import uuid
    from pathlib import Path

    LOGO_DIR = Path(__file__).parent.parent.parent / "static" / "uploads" / "logos"
    LOGO_DIR.mkdir(parents=True, exist_ok=True)

    # Validate file type
    ext = Path(file.filename or "logo.png").suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif"):
        raise HTTPException(
            status_code=400,
            detail="Invalid file type. Allowed: png, jpg, jpeg, svg, webp, gif",
        )

    filename = f"{uuid.uuid4().hex}{ext}"
    dest = LOGO_DIR / filename
    with dest.open("wb") as out:
        shutil.copyfileobj(file.file, out)

    return {"url": f"/static/uploads/logos/{filename}"}
