"""
KePSLA - Main Application Entry Point

Thin assembler: install platform hardening, build the FastAPI app, wire
middleware + routers, and expose a handful of top-level routes. The heavy
lifting lives in dedicated modules:

  - bootstrap.console_logging  → Windows hardening + console logging
  - bootstrap.lifespan         → startup/shutdown handler
  - bootstrap.scraper_proxy    → KB scraper subprocess + reverse proxy
  - bootstrap.dev_processes    → admin_ui build + Vite dev server
  - api.middleware.gateway     → gateway middleware + exception handler
"""
from bootstrap.console_logging import configure_console_logging, install_platform_hardening

# Install Windows console hardening (WMI-hang fix + safe stdout/stderr wrappers)
# BEFORE any third-party import that could transitively trigger the WMI query
# (watchfiles / sqlalchemy) or write to a broken console handle.
install_platform_hardening()

import os

# Configure console logging on import so the reload worker (which imports this
# module) prints app request lines + errors to the terminal. Re-run in lifespan
# startup too, to survive uvicorn's own logging configuration.
configure_console_logging()

from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

from config.settings import settings
from api.routes.chat import router as chat_router
from api.routes.admin import router as admin_router
from api.routes.lumira_compat import router as lumira_compat_router
from api.routes.widget import public_router as widget_public_router, admin_router as widget_admin_router
from api.routes.forms import admin_router as forms_admin_router
from api.routes.websocket_chat import router as websocket_router
from api.routes.websocket_agent import router as websocket_agent_router
from api.middleware.widget_origin import WidgetOriginMiddleware
from api.middleware.gateway import gateway_middleware, global_exception_handler
from bootstrap.lifespan import lifespan
from bootstrap.scraper_proxy import proxy_to_kb_scraper


_BASE_DIR = Path(__file__).resolve().parent


app = FastAPI(
    title=settings.app_name,
    description="Hotel Chatbot API - Conversational AI for guest services",
    version="2.0.0",
    lifespan=lifespan,
)

# Global exception handler — returns JSON (never HTML/text) for all unhandled
# exceptions, so Starlette's ServerErrorMiddleware never emits a bare 500 body.
app.exception_handler(Exception)(global_exception_handler)

# Gateway hardening middleware: trace-id, auth guard, rate limiting, tracing.
app.middleware("http")(gateway_middleware)

# CORS middleware (broad, for admin/dev). Per-widget enforcement happens in
# WidgetOriginMiddleware below, which sets specific Access-Control-Allow-Origin
# headers when a widget_key is present (required for cross-origin credentials).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Widget origin allowlist (Starlette runs middleware LIFO, so adding after CORS
# means this runs first on the inbound path — it can short-circuit unauthorized
# embed requests before they hit any route).
app.add_middleware(WidgetOriginMiddleware)

# Compress responses. The embedded widget pulls ~582 KB of JS/CSS on first open
# and every byte of it was going over the wire uncompressed — the single largest
# cause of the several-second blank frame customers see after clicking the
# launcher. These bundles gzip to roughly a third of that. `minimum_size` keeps
# it off small JSON turn responses, where the CPU is not worth the few bytes.
app.add_middleware(GZipMiddleware, minimum_size=1024)

# Mount static files and UI bundles
_STATIC_DIR = _BASE_DIR / "static"
_ADMIN_UI_DIST_DIR = (_BASE_DIR / "admin_ui" / "dist").resolve()
_CHAT_UI_FILE = (_ADMIN_UI_DIST_DIR / "chat.html").resolve()

if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
else:
    print(f"Warning: static directory not found: {_STATIC_DIR}")

# Include routers
app.include_router(chat_router)
# ORDER MATTERS: widget_admin_router mounts under /admin, and admin_router ends
# with an SPA catch-all `GET /admin/{full_path:path}` that 404s anything starting
# with "api/". Registered after admin_router, every GET /admin/api/widget/* was
# swallowed by that catch-all (POST/PUT still landed, since the catch-all is
# GET-only) — which is why listing widget deployments 404'd. Keep it ahead of
# admin_router so its own routes win; the catch-all still handles real SPA paths.
app.include_router(widget_admin_router)
# Same reason as widget_admin_router above: /admin/api/forms/* must beat
# admin_router's GET /admin/{full_path:path} SPA catch-all.
app.include_router(forms_admin_router)
app.include_router(admin_router)
app.include_router(lumira_compat_router)
app.include_router(widget_public_router)
app.include_router(websocket_router)
app.include_router(websocket_agent_router)


@app.api_route("/kb-scraper", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
@app.api_route("/kb-scraper/", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def kb_scraper_proxy_root(request: Request):
    """Reverse-proxy KB scraper root under same app port."""
    return await proxy_to_kb_scraper(request, "")


@app.api_route(
    "/kb-scraper/{proxy_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
async def kb_scraper_proxy_path(proxy_path: str, request: Request):
    """Reverse-proxy KB scraper subpaths under same app port."""
    return await proxy_to_kb_scraper(request, proxy_path)


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Open admin first for setup."""
    return RedirectResponse(url="/admin", status_code=307)


def _stamp_widget_cookie(response: Response, request: Request) -> None:
    """If /chat was loaded with ?widget_key=..., persist it as a SameSite=None;Secure
    cookie so subsequent /api/chat/* calls from inside the iframe carry the key.
    The WidgetOriginMiddleware reads this cookie when validating origins."""
    widget_key = (request.query_params.get("widget_key") or "").strip()
    if not widget_key:
        return
    is_https = (request.headers.get("x-forwarded-proto") or request.url.scheme or "").lower() == "https"
    response.set_cookie(
        key="kebo_widget_key",
        value=widget_key,
        max_age=60 * 60 * 24 * 30,  # 30 days
        httponly=False,  # readable by iframe JS so it can attach as X-Widget-Key too
        secure=is_https,
        samesite="none" if is_https else "lax",
        path="/",
    )


@app.get("/chat", response_class=HTMLResponse)
async def chat_ui(request: Request):
    """Serve the React chat harness — proxies to Vite dev server in development."""
    vite_base = os.environ.get("ADMIN_DEV_URL", "").strip()
    if vite_base:
        target = f"{vite_base.rstrip('/')}/admin/chat.html"
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                resp = await client.get(target)
            if resp.status_code == 200:
                html_resp = HTMLResponse(content=resp.text)
                _stamp_widget_cookie(html_resp, request)
                return html_resp
        except Exception:
            pass
    if not _CHAT_UI_FILE.exists():
        return HTMLResponse(
            "<h3>KePSLA API is running.</h3><p>Chat UI build is unavailable. Build admin_ui with npm run build.</p>",
            status_code=503,
        )
    # The chat.html entry point is NOT content-hashed (its JS/CSS assets are), so
    # it must be revalidated on every load — otherwise a phone can heuristically
    # cache an old chat.html that points at a stale bundle, which never picks up
    # theme/code changes (the "works on laptop, old colors on phone" bug). ETag
    # makes revalidation cheap (304 when unchanged).
    file_resp = FileResponse(
        _CHAT_UI_FILE,
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )
    _stamp_widget_cookie(file_resp, request)
    return file_resp


@app.get("/widget.js", include_in_schema=False)
async def widget_js():
    """Serve the embeddable Kebo chat widget loader script."""
    widget_file = _ADMIN_UI_DIST_DIR / "widget.js"
    if not widget_file.exists():
        raise HTTPException(status_code=404, detail="widget.js not built yet — run npm run build in admin_ui")
    # Revalidate the loader on every load (cheap 304 via ETag) so loader changes
    # — and the DB-driven theme fetch it performs — reach already-visited phones
    # promptly instead of being pinned for up to an hour.
    return FileResponse(
        str(widget_file),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


@app.get("/health")
@app.get("/api/health", include_in_schema=False)
async def health_check(response: Response):
    """Health check endpoint. /api/health is an alias for external probers.

    This used to return "healthy" from a literal — it answered "is the process
    running", which an external prober can already tell from the TCP connect.
    It now reports whether the bot can actually SERVE: database, LLM provider and
    recent turn outcomes. Degraded returns 503 so an uptime monitor pages on it.
    """
    from services.backend_health import backend_health

    snapshot = backend_health.snapshot()
    if not snapshot["ok"]:
        response.status_code = 503
    return {
        "status": "healthy" if snapshot["ok"] else "degraded",
        "checks": snapshot["checks"],
        "failing": snapshot["failing"],
        "app": settings.app_name,
        "environment": settings.app_env,
    }


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Silence browser favicon requests when no icon file is configured."""
    return Response(status_code=204)


@app.get("/api/config")
async def get_config():
    """Get public configuration for frontend."""
    return {
        "app_name": settings.app_name,
        "max_message_length": 2000,
        "confidence_threshold": settings.intent_confidence_threshold,
    }


if __name__ == "__main__":
    from bootstrap import dev_processes, scraper_proxy

    # Ensure admin_ui/dist is fresh BEFORE Vite/uvicorn start, so the admin UI
    # works even if Vite dev server fails to compile or start.
    dev_processes.ensure_admin_dist_fresh()

    if settings.is_development:
        os.environ.setdefault("ADMIN_DEV_URL", f"http://localhost:{dev_processes.VITE_DEV_PORT}")
        os.environ.setdefault("KEPSLA_BACKEND_PORT", str(settings.port))

    vite_process = dev_processes.start_vite_process() if settings.is_development else None
    if settings.kb_scraper_auto_start:
        scraper_proxy.start_scraper_process()
    try:
        uvicorn.run(
            "main:app",
            host=settings.host,
            port=settings.port,
            reload=settings.is_development,
            reload_excludes=[
                "admin_ui/*",
                "node_modules/*",
                "*.log",
                "*.db",
                "*.db-wal",
                "*.db-shm",
                "output/*",
                "logs/*",
            ],
            access_log=True,
            log_level="info",
        )
    finally:
        scraper_proxy.stop_scraper_process()
        dev_processes.stop_vite_process(vite_process)
