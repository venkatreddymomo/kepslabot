"""
Admin API Routes

Endpoints for managing business configuration and settings.
Now uses database-backed storage (MySQL) with JSON fallback.
"""

import asyncio
import json
import time
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, Depends, UploadFile, File, Form
from fastapi.routing import APIRoute

from core.text_utils import normalize_identifier
from fastapi.responses import HTMLResponse, FileResponse
from starlette.responses import Response
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from pathlib import Path
import re
import tempfile
from uuid import uuid4

import hashlib
from sqlalchemy import select, text

from services.db_config_service import db_config_service
from services.config_service import config_service  # Keep for JSON fallback
from services.rag_service import rag_service
from services.rag_job_service import rag_job_service
from services.evaluation_metrics_service import evaluation_metrics_service
from services.observability_service import observability_service
from services.gateway_service import gateway_service
from services.menu_ocr_plugin_service import menu_ocr_plugin_service
from services.prompt_writer_service import generate_service_system_prompt
from services.step_trace_service import step_trace_service
from services import prebooking_settings_service
from config.settings import settings
from llm.client import llm_client
from models.database import AsyncSessionLocal, Hotel, Guest, Booking, KBFile, ImageAsset, Property


class AdminStepTraceRoute(APIRoute):
    """Route-level step tracing for all admin endpoints."""

    def get_route_handler(self):
        original_route_handler = super().get_route_handler()
        route_path = str(self.path or "").strip()

        async def custom_route_handler(request: Request) -> Response:
            trace_id = str(getattr(request.state, "trace_id", "") or "").strip()
            endpoint = str(request.url.path or route_path or "/admin").strip()
            method = str(request.method or "GET").strip().upper()
            hotel_code = str(
                getattr(request.state, "hotel_code", "")
                or request.query_params.get("hotel_code")
                or request.query_params.get("tenant_id")
                or ""
            ).strip()
            start_ts = time.perf_counter()

            step_trace_service.log_event(
                "admin_route_start",
                payload={"route_path": route_path},
                trace_id=trace_id,
                hotel_code=hotel_code,
                endpoint=endpoint,
                method=method,
                component="api.routes.admin",
                step=route_path or endpoint,
                stage="start",
            )

            try:
                response = await original_route_handler(request)
            except HTTPException as exc:
                duration_ms = round((time.perf_counter() - start_ts) * 1000.0, 2)
                step_trace_service.log_event(
                    "admin_route_failed",
                    payload={"route_path": route_path, "duration_ms": duration_ms},
                    trace_id=trace_id,
                    hotel_code=hotel_code,
                    endpoint=endpoint,
                    method=method,
                    status_code=int(exc.status_code),
                    component="api.routes.admin",
                    step=route_path or endpoint,
                    stage="failed",
                    error=str(exc.detail),
                )
                raise
            except Exception as exc:
                duration_ms = round((time.perf_counter() - start_ts) * 1000.0, 2)
                step_trace_service.log_event(
                    "admin_route_failed",
                    payload={"route_path": route_path, "duration_ms": duration_ms},
                    trace_id=trace_id,
                    hotel_code=hotel_code,
                    endpoint=endpoint,
                    method=method,
                    status_code=500,
                    component="api.routes.admin",
                    step=route_path or endpoint,
                    stage="failed",
                    error=str(exc),
                )
                raise

            duration_ms = round((time.perf_counter() - start_ts) * 1000.0, 2)
            step_trace_service.log_event(
                "admin_route_end",
                payload={"route_path": route_path, "duration_ms": duration_ms},
                trace_id=trace_id,
                hotel_code=hotel_code,
                endpoint=endpoint,
                method=method,
                status_code=int(getattr(response, "status_code", 200) or 200),
                component="api.routes.admin",
                step=route_path or endpoint,
                stage="completed",
            )
            return response

        return custom_route_handler


router = APIRouter(prefix="/admin", tags=["Admin"], route_class=AdminStepTraceRoute)

_ADMIN_UI_DIST_DIR = (Path(__file__).resolve().parents[2] / "admin_ui" / "dist").resolve()
_ADMIN_UI_INDEX_FILE = (_ADMIN_UI_DIST_DIR / "index.html").resolve()


def _safe_print(*args, **kwargs) -> None:
    """print() wrapper that swallows OSError (Windows Errno 22 stdout issue)."""
    try:
        print(*args, **kwargs)
    except Exception:
        pass


_ADMIN_DB_FAST_FALLBACK_TIMEOUT_SECONDS = max(
    0.2,
    float(getattr(settings, "admin_db_fast_fallback_timeout_seconds", 1.5) or 1.5),
)
_ADMIN_DB_UNAVAILABLE_BACKOFF_SECONDS = max(
    1.0,
    float(getattr(settings, "admin_db_unavailable_backoff_seconds", 15.0) or 15.0),
)
_admin_db_unavailable_until = 0.0


def _admin_db_is_available_now() -> bool:
    return time.monotonic() >= float(_admin_db_unavailable_until or 0.0)


def _mark_admin_db_unavailable() -> None:
    global _admin_db_unavailable_until
    _admin_db_unavailable_until = (
        time.monotonic() + _ADMIN_DB_UNAVAILABLE_BACKOFF_SECONDS
    )


async def _call_db_config_with_fast_fallback(operation: str, coroutine):
    """
    Execute DB config call with fast timeout and temporary backoff.
    This keeps admin UX responsive when DB/VPN is unreachable.
    """
    global _admin_db_unavailable_until
    if not _admin_db_is_available_now():
        if hasattr(coroutine, "close"):
            try:
                coroutine.close()
            except Exception:
                pass
        return False, None
    try:
        result = await asyncio.wait_for(
            coroutine,
            timeout=_ADMIN_DB_FAST_FALLBACK_TIMEOUT_SECONDS,
        )
        _admin_db_unavailable_until = 0.0
        return True, result
    except Exception as error:
        error_text = str(error or "").strip() or type(error).__name__
        _safe_print(f"DB fast-fallback ({operation}) error: {error_text}")
        _mark_admin_db_unavailable()
        return False, None


# ============ Pydantic Models for Request Validation ============

class UpdateBusinessInfo(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    city: Optional[str] = None
    bot_name: Optional[str] = None
    welcome_message: Optional[str] = None
    timezone: Optional[str] = None
    currency: Optional[str] = None


class UpdateCapability(BaseModel):
    enabled: Optional[bool] = None
    description: Optional[str] = None
    hours: Optional[str] = None


class AddService(BaseModel):
    id: str
    name: str
    type: str
    description: Optional[str] = None
    cuisine: Optional[str] = None
    hours: Optional[dict] = None
    delivery_zones: Optional[list] = None
    is_active: bool = True
    ticketing_plugin_enabled: Optional[bool] = None
    ticketing_cases: Optional[List[Any]] = None
    ticketing_enabled: Optional[bool] = None
    ticketing_mode: Optional[str] = None
    ticketing_policy: Optional[str] = None
    form_config: Optional[Dict[str, Any]] = None
    phase_id: Optional[str] = None
    is_builtin: Optional[bool] = None
    service_prompt_pack: Optional[Dict[str, Any]] = None


class AgentPluginSlotInput(BaseModel):
    id: str
    label: Optional[str] = None
    prompt: Optional[str] = None
    required: bool = True


class AgentPluginFactInput(BaseModel):
    id: Optional[str] = None
    text: str
    source: Optional[str] = None
    tags: Optional[List[str]] = None
    status: Optional[str] = None
    approved_by: Optional[str] = None


class UpdateAgentPluginFactInput(BaseModel):
    text: Optional[str] = None
    source: Optional[str] = None
    tags: Optional[List[str]] = None
    status: Optional[str] = None
    approved_by: Optional[str] = None


class AddAgentPlugin(BaseModel):
    id: str
    name: str
    industry: Optional[str] = "custom"
    service_id: Optional[str] = None
    service_category: Optional[str] = "transactional"
    description: Optional[str] = None
    trigger_phrases: Optional[List[str]] = None
    slot_schema: Optional[List[AgentPluginSlotInput]] = None
    confirmation_required: Optional[bool] = True
    channels: Optional[List[str]] = None
    is_active: bool = True
    response_templates: Optional[Dict[str, str]] = None
    knowledge_scope: Optional[Dict[str, Any]] = None
    knowledge_facts: Optional[List[AgentPluginFactInput]] = None
    strict_facts_only: Optional[bool] = True
    tool_bindings: Optional[List[Dict[str, Any]]] = None
    metadata: Optional[Dict[str, Any]] = None
    version: Optional[int] = 1


class UpdateAgentPlugin(BaseModel):
    name: Optional[str] = None
    industry: Optional[str] = None
    service_id: Optional[str] = None
    service_category: Optional[str] = None
    description: Optional[str] = None
    trigger_phrases: Optional[List[str]] = None
    slot_schema: Optional[List[AgentPluginSlotInput]] = None
    confirmation_required: Optional[bool] = None
    channels: Optional[List[str]] = None
    is_active: Optional[bool] = None
    response_templates: Optional[Dict[str, str]] = None
    knowledge_scope: Optional[Dict[str, Any]] = None
    knowledge_facts: Optional[List[AgentPluginFactInput]] = None
    strict_facts_only: Optional[bool] = None
    tool_bindings: Optional[List[Dict[str, Any]]] = None
    metadata: Optional[Dict[str, Any]] = None
    version: Optional[int] = None


class UpdateAgentPluginSettings(BaseModel):
    enabled: Optional[bool] = None
    shared_context: Optional[bool] = None
    strict_mode: Optional[bool] = None
    strict_unavailable_response: Optional[str] = None


class ApproveAgentPluginFactRequest(BaseModel):
    approved_by: Optional[str] = "staff"


class AddFAQEntry(BaseModel):
    id: Optional[str] = None
    question: str
    answer: str
    description: Optional[str] = None
    tags: Optional[List[str]] = None
    enabled: bool = True
    # Property scope: empty/None = COMMON to all properties under the hotel; a
    # slug scopes the FAQ to one property (carried as property_id for the UI).
    property_slug: Optional[str] = None
    property_id: Optional[int] = None


class UpdateFAQEntry(BaseModel):
    question: Optional[str] = None
    answer: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[List[str]] = None
    enabled: Optional[bool] = None
    property_slug: Optional[str] = None
    property_id: Optional[int] = None


class AddToolConfig(BaseModel):
    id: Optional[str] = None
    name: str
    description: Optional[str] = None
    type: Optional[str] = "workflow"
    handler: Optional[str] = None
    channels: Optional[List[str]] = None
    enabled: bool = True
    requires_confirmation: Optional[bool] = False
    ticketing_plugin_enabled: Optional[bool] = None
    ticketing_cases: Optional[List[Any]] = None


class UpdateToolConfig(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    type: Optional[str] = None
    handler: Optional[str] = None
    channels: Optional[List[str]] = None
    enabled: Optional[bool] = None
    requires_confirmation: Optional[bool] = None
    ticketing_plugin_enabled: Optional[bool] = None
    ticketing_cases: Optional[List[Any]] = None



class ApplyTemplate(BaseModel):
    template_name: str
    business_id: str
    business_name: str
    city: str
    bot_name: Optional[str] = "Assistant"


class ImportConfig(BaseModel):
    config_json: str


class UpdateOnboardingBusiness(BaseModel):
    id: Optional[str] = None
    name: Optional[str] = None
    type: Optional[str] = None
    city: Optional[str] = None
    location: Optional[str] = None
    address: Optional[str] = None
    timezone: Optional[str] = None
    currency: Optional[str] = None
    language: Optional[str] = None
    timestamp_format: Optional[str] = None
    bot_name: Optional[str] = None
    welcome_message: Optional[str] = None
    contact_email: Optional[str] = None
    contact_phone: Optional[str] = None
    website: Optional[str] = None
    channels: Optional[Dict[str, bool]] = None


class UpdatePromptConfig(BaseModel):
    template_id: Optional[str] = None
    system_prompt: Optional[str] = None
    classifier_prompt: Optional[str] = None
    response_style: Optional[str] = None


class ApplyPromptTemplateRequest(BaseModel):
    template_id: str


class UpdateKnowledgeConfig(BaseModel):
    sources: Optional[List[str]] = None
    notes: Optional[str] = None
    expected_property_count: Optional[int] = None
    nlu_policy: Optional[Dict[str, Any]] = None


class UpdateUISettings(BaseModel):
    theme: Optional[Dict[str, str]] = None
    widget: Optional[Dict[str, Any]] = None
    channels: Optional[Dict[str, Dict[str, Any]]] = None
    industry_features: Optional[List[str]] = None
    logo_url: Optional[str] = None


class CompileServiceKBRequest(BaseModel):
    service_id: Optional[str] = None
    force: Optional[bool] = False
    max_facts_per_service: Optional[int] = None
    preserve_manual: Optional[bool] = True
    published_by: Optional[str] = "admin"


class UpdateServiceKBManualFactsRequest(BaseModel):
    service_id: str
    plugin_id: Optional[str] = None
    facts: Optional[List[str]] = None
    published_by: Optional[str] = "admin"


class UpdateServiceKBMenuDocumentsRequest(BaseModel):
    service_id: str
    plugin_id: Optional[str] = None
    documents: Optional[List[Dict[str, Any]]] = None
    published_by: Optional[str] = "admin"
    replace: Optional[bool] = False


class RAGReindexRequest(BaseModel):
    tenant_id: Optional[str] = None
    business_type: Optional[str] = None
    clear_existing: bool = True
    file_paths: Optional[List[str]] = None


class RAGQueryRequest(BaseModel):
    question: str
    tenant_id: Optional[str] = None
    hotel_name: Optional[str] = None
    city: Optional[str] = None
    business_type: Optional[str] = None


class RAGStartJobRequest(BaseModel):
    tenant_id: Optional[str] = None
    business_type: Optional[str] = None
    clear_existing: bool = True
    file_paths: Optional[List[str]] = None


class SuggestServiceDescriptionRequest(BaseModel):
    name: str
    phase_id: Optional[str] = None
    user_intent: Optional[str] = None  # freeform "what do you expect" text from admin


def _normalize_tenant(value: str) -> str:
    return str(value or "default").strip().lower().replace(" ", "_")


def _normalize_identifier(value: Any) -> str:
    return normalize_identifier(value)


def _normalize_phase_identifier(value: Any) -> str:
    from services.config_service import config_service as _cs
    return _cs._normalize_phase_identifier(value)


def _resolve_scoped_rag_tenant(request: Request, requested_tenant: Any) -> str:
    """
    Resolve tenant for RAG operations with property-scope safety.

    Rules:
    - If explicit tenant is non-default, honor it.
    - If explicit tenant is empty/default and admin property scope is non-default,
      use scoped property code to avoid accidental cross-tenant writes/reads.
    - Else fallback to business.id then default.
    """
    requested_norm = _normalize_tenant(str(requested_tenant or "").strip())
    scoped_norm = _normalize_tenant(str(getattr(request.state, "hotel_code", "") or "").strip())

    if requested_norm and requested_norm != "default":
        return requested_norm
    if scoped_norm and scoped_norm != "default":
        return scoped_norm
    if requested_norm:
        return requested_norm

    business = config_service.get_business_info()
    return _normalize_tenant(str(business.get("id") or "").strip() or "default")


async def _ensure_property_registered(property_code: str) -> None:
    """
    Best-effort registration of a property/tenant in both DB and scoped config files.
    This makes new properties discoverable in admin even if first touch is via RAG.
    """
    code = _normalize_tenant(property_code)
    if not code:
        return

    # Ensure DB hotel row exists when DB is available.
    try:
        await db_config_service.get_or_create_hotel(code)
    except Exception:
        pass

    # Ensure scoped config file exists and business.id is aligned.
    token = db_config_service.set_hotel_context(code)
    try:
        cfg = config_service.load_config()
        if isinstance(cfg, dict):
            business = cfg.setdefault("business", {})
            current_id = _normalize_tenant(str(business.get("id") or "").strip())
            if not current_id or current_id == "default":
                business["id"] = code
                config_service.save_config(cfg)
    except Exception:
        pass
    finally:
        db_config_service.reset_hotel_context(token)


def _safe_filename(filename: str) -> str:
    name = Path(filename or "upload.txt").name
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", name)
    return safe or f"file_{uuid4().hex[:8]}.txt"


def _is_kb_db_only_mode() -> bool:
    return bool(getattr(settings, "kb_db_only_mode", False))


def _build_kb_source_ref(*, kb_file_id: Any = None, stored_name: str = "") -> str:
    normalized_id = str(kb_file_id or "").strip()
    if normalized_id:
        return f"db://kb_files/{normalized_id}"
    normalized_stored = str(stored_name or "").strip()
    if normalized_stored:
        return f"db://kb_files/by-name/{normalized_stored}"
    return ""


def _source_matches_kb_file(source_value: Any, *, kb_file_id: Any, stored_name: str) -> bool:
    source = str(source_value or "").strip()
    if not source:
        return False
    normalized_id = str(kb_file_id or "").strip()
    normalized_stored = str(stored_name or "").strip()
    if normalized_id and source == f"db://kb_files/{normalized_id}":
        return True
    if normalized_stored and source == f"db://kb_files/by-name/{normalized_stored}":
        return True
    if normalized_stored and source.endswith(f"/{normalized_stored}"):
        return True
    return False


def _fallback_phase_service_description(service_name: str, phase_id: str = "") -> str:
    clean_name = str(service_name or "").strip()
    if not clean_name:
        return "Provide guest support for this service."
    return f"Provide guest support for {clean_name.lower()} requests."


def _resolve_admin_hotel_code(request: Request) -> str:
    """
    Resolve active admin property scope from request header/query with safe fallback.
    Header takes precedence so frontend can explicitly scope every API call.
    """
    requested = (
        str(request.headers.get("x-hotel-code") or "").strip()
        or str(request.query_params.get("hotel_code") or "").strip()
        or str(request.query_params.get("tenant_id") or "").strip()
    )
    if requested:
        return config_service.resolve_hotel_code(requested)

    business = config_service.get_business_info()
    return config_service.resolve_hotel_code(
        str(business.get("id") or "").strip() or "DEFAULT"
    )


async def _bind_admin_hotel_scope(request: Request):
    hotel_code = _resolve_admin_hotel_code(request)
    token = db_config_service.set_hotel_context(hotel_code)
    request.state.hotel_code = hotel_code
    try:
        yield
    finally:
        db_config_service.reset_hotel_context(token)


# ============ Health probe (no hotel-scope dependency) ============
# Registered BEFORE the global router.dependencies = [...] line below so the
# probe doesn't require a resolvable hotel context — it must be reachable
# from a fresh deploy host with no hotels yet, so a load balancer / Kubernetes
# liveness probe / CI smoke test can ask "is the bot's DB wired up?".

_REQUIRED_TABLES = (
    "new_bot_hotels",
    "new_bot_properties",
    "new_bot_services",
    "new_bot_business_config",
    "new_bot_kb_files",
    "new_bot_kb_books",
    "new_bot_kb_pages",
    "new_bot_kb_book_pages",
    "new_bot_prompt_registry",
    "new_bot_conversations",
)


@router.post("/api/dev/reload-prompts")
async def admin_dev_reload_prompts():  # touch

    """Force-reload the prompt registry cache + reseed any file-changed prompts.

    Dev-only convenience: after editing a `.md` under `prompts/defaults/<industry>/`
    and pushing it to the DB via the seed service, the running backend's
    in-process `prompt_registry._cache` can still hold the old content (5-min TTL).
    This endpoint invalidates that cache so the next prompt fetch reads from the DB
    you just updated. Also kicks off a fresh seed pass to catch any pending file
    changes you forgot to seed.
    """
    try:
        from services.prompt_registry_service import prompt_registry
        from services.prompt_seed_service import seed_prompts_from_files
        from services.weather_service import clear_weather_caches
        inserted, updated, unchanged, updated_keys = await seed_prompts_from_files()
        prompt_registry._invalidate_all()  # noqa: SLF001
        clear_weather_caches()
        return {
            "status": "ok",
            "cache_invalidated": True,
            "weather_cache_cleared": True,
            "seed": {
                "inserted": inserted,
                "updated": updated,
                "unchanged": unchanged,
                "updated_keys": list(updated_keys)[:50],
            },
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"reload failed: {exc}")


@router.get("/api/health")
async def admin_health_probe(deep: bool = False):
    """Deployment readiness probe.

    GET /admin/api/health        → fast: pings DB, returns flags + counts.
    GET /admin/api/health?deep=1 → also verifies every required table exists
                                   and returns row counts. Use this in your
                                   deploy pipeline to catch missing migrations.

    Returns 200 with a JSON report when healthy, 503 otherwise. The JSON has:
      - status: "ok" | "degraded" | "down"
      - db: { backend, connected, error?, tables: { name: rows } }
      - flags: which DB-first / JSON-skip settings are in effect
      - prompts: count of active rows in new_bot_prompt_registry
    """
    from sqlalchemy import text as _sql_text
    from models.database import AsyncSessionLocal
    from config.settings import settings as _settings

    report: Dict[str, Any] = {
        "status": "ok",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "db": {"backend": "", "connected": False},
        "flags": {
            "chat_db_first_mode": bool(getattr(_settings, "chat_db_first_mode", False)),
            "chat_db_overlay_runtime_config": bool(getattr(_settings, "chat_db_overlay_runtime_config", False)),
            "chat_db_allow_inmemory_fallback": bool(getattr(_settings, "chat_db_allow_inmemory_fallback", False)),
            "chat_skip_json_persist": bool(getattr(_settings, "chat_skip_json_persist", True)),
            "kb_db_only_mode": bool(getattr(_settings, "kb_db_only_mode", False)),
            "kb_read_from_tables": bool(getattr(_settings, "kb_read_from_tables", False)),
            "chat_db_prefer_kb_content": bool(getattr(_settings, "chat_db_prefer_kb_content", False)),
        },
    }

    db_url = str(getattr(_settings, "database_url", "") or "")
    report["db"]["backend"] = (
        "mysql" if db_url.startswith("mysql") else
        "postgres" if db_url.startswith("postgres") else
        "sqlite" if db_url.startswith("sqlite") else
        "unknown"
    )

    try:
        async with AsyncSessionLocal() as session:
            await session.execute(_sql_text("SELECT 1"))
            report["db"]["connected"] = True
            if deep:
                table_counts: Dict[str, Any] = {}
                missing: list[str] = []
                for tbl in _REQUIRED_TABLES:
                    try:
                        result = await session.execute(_sql_text(f"SELECT COUNT(*) FROM {tbl}"))
                        table_counts[tbl] = int(result.scalar_one() or 0)
                    except Exception as exc:
                        table_counts[tbl] = {"error": str(exc)[:200]}
                        missing.append(tbl)
                report["db"]["tables"] = table_counts
                if missing:
                    report["db"]["missing_or_unreadable_tables"] = missing
                    report["status"] = "degraded"
                # Active prompt count (proxy: are seeded prompts in DB?).
                try:
                    pr_result = await session.execute(
                        _sql_text(
                            "SELECT COUNT(*) FROM new_bot_prompt_registry "
                            "WHERE is_active = 1 AND hotel_id IS NULL"
                        )
                    )
                    report["prompts"] = {"active_industry_defaults": int(pr_result.scalar_one() or 0)}
                except Exception as exc:
                    report["prompts"] = {"error": str(exc)[:200]}
    except Exception as exc:
        report["db"]["connected"] = False
        report["db"]["error"] = str(exc)[:300]
        report["status"] = "down"

    if report["status"] == "down":
        return Response(
            content=json.dumps(report, ensure_ascii=False),
            status_code=503,
            media_type="application/json",
        )
    if report["status"] == "degraded":
        return Response(
            content=json.dumps(report, ensure_ascii=False),
            status_code=503,
            media_type="application/json",
        )
    return report


router.dependencies = [Depends(_bind_admin_hotel_scope)]


# ============ Business Config API (Database-backed) ============

@router.get("/api/properties")
async def list_admin_properties():
    """List active properties/hotels from DB only — no JSON fallback."""
    properties_by_code: Dict[str, Dict[str, str]] = {}

    from models.database import Hotel, AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(Hotel).where(Hotel.is_active == True).order_by(Hotel.code.asc())  # noqa: E712
            )
        ).scalars().all()
    for row in rows:
        raw_code = str(row.code or "").strip()
        if not raw_code:
            continue
        code = _normalize_tenant(raw_code)
        properties_by_code[code] = {
            "code": code,
            "name": str(row.name or row.code or code).strip(),
            "city": str(row.city or "").strip(),
        }

    properties = sorted(
        properties_by_code.values(),
        key=lambda row: str(row.get("code") or ""),
    )

    return {"properties": properties}


@router.get("/api/hotel-properties")
async def list_hotel_sub_properties(request: Request):
    """List sub-properties (new_bot_properties) under the active hotel scope.

    Hotel scope is resolved from the x-hotel-code header. Returns an empty
    list if the hotel exists but has no Property rows — the UI uses that to
    suppress the second dropdown.
    """
    hotel_id = await db_config_service.get_current_hotel_id()
    if not hotel_id:
        return {"hotel_id": None, "properties": []}

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(Property)
                .where(
                    Property.hotel_id == hotel_id,
                    Property.is_active == True,  # noqa: E712
                )
                .order_by(Property.is_default.desc(), Property.name.asc())
            )
        ).scalars().all()

    payload = [
        {
            "id": int(row.id),
            "slug": str(row.property_slug or "").strip(),
            "name": str(row.name or row.property_slug or "").strip(),
            "is_default": bool(row.is_default),
        }
        for row in rows
        if str(row.property_slug or "").strip()
    ]
    return {"hotel_id": int(hotel_id), "properties": payload}


# ============ Pre-booking settings (Bot Training -> Pre-booking tab) ============
# Every pre-booking customisation of a hotel is saved here, in the DB, so a new
# hotel is onboarded from the admin with no code or .env change. A hotel that has
# never saved these keeps its old behaviour. See services/prebooking_settings_service.py.


class PrebookingSettingsUpdate(BaseModel):
    settings: Dict[str, Any]


class HotelPropertyUpdate(BaseModel):
    name: str
    location_label: str
    city: str = ""
    aliases: List[str] = []
    fms_entity_id: str = ""


class PrebookingMessagesUpdate(BaseModel):
    # {location_id: message} and {property_slug: message}; "" removes one.
    locations: Dict[str, str] = {}
    properties: Dict[str, str] = {}


MAX_SAVED_MESSAGE_CHARS = 6000


def _json_field(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


async def _prebooking_admin_context() -> Dict[str, Any]:
    """The active hotel's properties and pre-booking services - what the
    Pre-booking tab edits per property and per service."""
    from models.database import BotService

    hotel_id = await db_config_service.get_current_hotel_id()
    if not hotel_id:
        raise HTTPException(status_code=404, detail="Hotel not found")
    async with AsyncSessionLocal() as session:
        properties = (await session.execute(
            select(Property)
            .where(Property.hotel_id == hotel_id, Property.is_active == True)  # noqa: E712
            .order_by(Property.is_default.desc(), Property.name.asc())
        )).scalars().all()
        services = (await session.execute(
            select(BotService)
            .where(
                BotService.hotel_id == hotel_id,
                BotService.phase_id == "pre_booking",
                BotService.is_active == True,  # noqa: E712
            )
            .order_by(BotService.name.asc())
        )).scalars().all()

    property_rows = []
    for row in properties:
        slug = str(row.property_slug or "").strip()
        if not slug or slug == "shared":
            continue
        location = _json_field(row.location)
        location = location if isinstance(location, dict) else {}
        aliases = _json_field(row.aliases)
        property_rows.append({
            "id": int(row.id),
            "slug": slug,
            "name": str(row.name or ""),
            "location_label": str(location.get("label") or ""),
            "city": str(location.get("city") or ""),
            "aliases": [str(a) for a in (aliases or []) if str(a).strip()],
        })
    # Locations exactly as the chat groups them (by picker label), so a saved
    # message is keyed the way the location picker and the intro lookup key it.
    groups: Dict[str, Dict[str, Any]] = {}
    for prop in property_rows:
        label = prop["location_label"] or prop["name"]
        location_id = config_service._normalize_slug(label) or prop["slug"]
        group = groups.setdefault(
            location_id,
            {"location_id": location_id, "label": label, "property_slugs": [], "property_names": []},
        )
        group["property_slugs"].append(prop["slug"])
        group["property_names"].append(prop["name"])
    return {
        "hotel_id": int(hotel_id),
        "properties": property_rows,
        "locations": sorted(groups.values(), key=lambda g: g["label"].lower()),
        "services": [
            {"service_id": str(row.service_id), "name": str(row.name or row.service_id), "row_id": int(row.id)}
            for row in services
        ],
    }


async def _prebooking_kb_books(hotel_id: int, property_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The hotel's active KB books, one entry per book slug (a book every
    property has is listed once) - what the menu topics are ticked from."""
    from models.database import KBBook

    names = {p["id"]: p["name"] for p in property_rows}
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(KBBook.book_slug, KBBook.title, KBBook.property_id)
            .where(KBBook.hotel_id == hotel_id, KBBook.is_active == True)  # noqa: E712
        )).all()
    books: Dict[str, Dict[str, Any]] = {}
    for slug, title, property_id in rows:
        slug = str(slug or "").strip().lower()
        if not slug:
            continue
        entry = books.setdefault(slug, {"slug": slug, "title": str(title or slug), "properties": []})
        name = names.get(int(property_id or 0))
        if name and name not in entry["properties"]:
            entry["properties"].append(name)
    return sorted(books.values(), key=lambda b: b["title"].lower())


async def _saved_location_messages(hotel_id: int) -> Dict[str, str]:
    """{location_id or "prop::<slug>": message} from new_bot_location_intros -
    the table the chat serves curated location and hotel messages from."""
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(
            text(
                "SELECT location_id, body FROM new_bot_location_intros "
                "WHERE hotel_id = :hid AND is_active = 1"
            ),
            {"hid": hotel_id},
        )).all()
    return {str(lid): str(body or "") for lid, body in rows}


@router.get("/api/config/prebooking")
async def get_prebooking_settings(request: Request):
    """Everything the Pre-booking tab shows: saved settings (or the defaults a
    first save would store), ticket routing, properties and services."""
    context = await _prebooking_admin_context()
    saved = await db_config_service.get_json_section("prebooking", None)
    ticketing = await db_config_service.get_json_section("ticketing", None)
    messages = await _saved_location_messages(context["hotel_id"])
    return {
        "hotel_code": request.state.hotel_code,
        "saved": isinstance(saved, dict),
        "settings": prebooking_settings_service.with_defaults(
            "prebooking", saved if isinstance(saved, dict) else None),
        "ticketing_saved": isinstance(ticketing, dict),
        "ticketing": prebooking_settings_service.with_defaults(
            "ticketing", ticketing if isinstance(ticketing, dict) else None),
        "properties": context["properties"],
        "locations": [
            {**group, "message": messages.get(group["location_id"], "")}
            for group in context["locations"]
        ],
        "property_messages": {
            prop["slug"]: messages.get("prop::" + prop["slug"], "") for prop in context["properties"]
        },
        "services": context["services"],
        "kb_books": await _prebooking_kb_books(context["hotel_id"], context["properties"]),
    }


async def _save_settings_section(
    request: Request, section: str, clean: Dict[str, Any], properties: List[Dict[str, Any]]
) -> Dict[str, Any]:
    if section == "prebooking":
        # The names the chat matches a hotel by, rebuilt from new_bot_properties.
        clean = prebooking_settings_service.index_properties(clean, properties)
        # Menus belong to the menu upload routes; keep whatever is stored.
        existing = await db_config_service.get_json_section(section, None)
        stored_menus = existing.get("menus") if isinstance(existing, dict) else None
        clean["menus"] = stored_menus if isinstance(stored_menus, list) else []
    else:
        # FMS ids belong to the property editor; keep whatever is stored.
        existing = await db_config_service.get_json_section(section, None)
        stored_properties = existing.get("properties") if isinstance(existing, dict) else None
        clean["properties"] = stored_properties if isinstance(stored_properties, dict) else {}
    await db_config_service.save_json_section(section, clean)
    prebooking_settings_service.remember(request.state.hotel_code, section, clean)
    return clean


@router.put("/api/config/prebooking")
async def update_prebooking_settings(update: PrebookingSettingsUpdate, request: Request):
    """Save buttons, toggles and reply style. Saving the first time switches the
    hotel from its old behaviour to these settings."""
    context = await _prebooking_admin_context()
    books = await _prebooking_kb_books(context["hotel_id"], context["properties"])
    try:
        clean = prebooking_settings_service.normalize_prebooking(
            update.settings,
            service_ids=[s["service_id"] for s in context["services"]],
            property_slugs=[p["slug"] for p in context["properties"]],
            book_slugs=[b["slug"] for b in books],
        )
    except prebooking_settings_service.SettingsError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    clean = prebooking_settings_service.index_services(clean, context["services"])
    saved = await _save_settings_section(request, "prebooking", clean, context["properties"])
    return {"saved": True, "settings": saved}


@router.put("/api/config/prebooking/messages")
async def update_prebooking_messages(update: PrebookingMessagesUpdate, request: Request):
    """Save the curated messages: one per location (for a location with several
    hotels, the comparison the guest sees with one button per hotel) and one per
    hotel (shown when the guest taps that hotel). Stored in
    new_bot_location_intros, which the chat already serves them from; an empty
    message removes the row and the bot answers that step itself."""
    context = await _prebooking_admin_context()
    hotel_id = context["hotel_id"]
    labels = {g["location_id"]: g["label"] for g in context["locations"]}
    names = {p["slug"]: p["name"] for p in context["properties"]}
    rows: List[tuple] = []
    for location_id, body in (update.locations or {}).items():
        if location_id not in labels:
            raise HTTPException(status_code=422, detail=f"Unknown location '{location_id}'.")
        rows.append((location_id, labels[location_id], str(body or "").strip()))
    for slug, body in (update.properties or {}).items():
        if slug not in names:
            raise HTTPException(status_code=422, detail=f"Unknown property '{slug}'.")
        rows.append(("prop::" + slug, names[slug], str(body or "").strip()))
    for key, _label, body in rows:
        if len(body) > MAX_SAVED_MESSAGE_CHARS:
            raise HTTPException(
                status_code=422,
                detail=f"The message for '{key}' is longer than {MAX_SAVED_MESSAGE_CHARS} characters.",
            )
    async with AsyncSessionLocal() as session:
        for key, label, body in rows:
            existing = (await session.execute(
                text("SELECT id FROM new_bot_location_intros WHERE hotel_id = :hid AND location_id = :lid"),
                {"hid": hotel_id, "lid": key},
            )).scalar_one_or_none()
            if not body:
                if existing is not None:
                    await session.execute(
                        text("DELETE FROM new_bot_location_intros WHERE id = :id"), {"id": existing}
                    )
                continue
            if existing is None:
                await session.execute(
                    text(
                        "INSERT INTO new_bot_location_intros "
                        "(hotel_id, location_id, location_label, body, is_active, created_at, updated_at) "
                        "VALUES (:hid, :lid, :label, :body, 1, NOW(), NOW())"
                    ),
                    {"hid": hotel_id, "lid": key, "label": label, "body": body},
                )
            else:
                await session.execute(
                    text(
                        "UPDATE new_bot_location_intros SET location_label = :label, body = :body, "
                        "is_active = 1, updated_at = NOW() WHERE id = :id"
                    ),
                    {"id": existing, "label": label, "body": body},
                )
        await session.commit()

    from services import location_intro_service

    # The chat caches intros under the hotel code exactly as requests send it.
    for code in {str(request.state.hotel_code or ""), str(request.state.hotel_code or "").lower(),
                 str(request.state.hotel_code or "").upper()}:
        location_intro_service.invalidate(code)
    messages = await _saved_location_messages(hotel_id)
    return {
        "saved": True,
        "locations": {g["location_id"]: messages.get(g["location_id"], "") for g in context["locations"]},
        "properties": {slug: messages.get("prop::" + slug, "") for slug in names},
    }


async def _saved_prebooking_or_409() -> Dict[str, Any]:
    current = await db_config_service.get_json_section("prebooking", None)
    if not isinstance(current, dict):
        raise HTTPException(
            status_code=409,
            detail="Save the pre-booking settings first; this hotel does not use them yet.",
        )
    return current


@router.post("/api/config/prebooking/menus")
async def upload_prebooking_menu(
    request: Request,
    file: UploadFile = File(...),
    title: str = Form(...),
    property_slug: str = Form(""),
):
    """Upload a menu PDF to S3 and list it for the hotel. The bot sends it when
    a guest asks for the menu or the bot answers about a ticked menu topic - the
    one for the hotel in focus, plus any set for every hotel."""
    from services.s3_storage_service import S3StorageError, s3_storage_service

    current = await _saved_prebooking_or_409()
    context = await _prebooking_admin_context()
    clean_title = re.sub(r"\s+", " ", str(title or "")).strip()
    if not clean_title or len(clean_title) > 80:
        raise HTTPException(status_code=422, detail="Give the menu a title of up to 80 characters.")
    slug = str(property_slug or "").strip()
    prop = next((p for p in context["properties"] if p["slug"] == slug), None)
    if slug and prop is None:
        raise HTTPException(status_code=422, detail=f"Unknown property '{slug}'.")
    menus = [m for m in (current.get("menus") or []) if isinstance(m, dict)]
    if len(menus) >= prebooking_settings_service.MAX_MENUS:
        raise HTTPException(
            status_code=422, detail=f"At most {prebooking_settings_service.MAX_MENUS} menus; remove one first."
        )
    data = await file.read()
    try:
        uploaded = await s3_storage_service.upload_pdf(
            file_bytes=data,
            hotel_code=str(request.state.hotel_code or ""),
            property_id=(prop or {}).get("id"),
            original_filename=str(file.filename or clean_title),
        )
    except S3StorageError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    menus.append({
        "id": uuid4().hex[:10],
        "title": clean_title,
        "property_slug": slug,
        "stored": uploaded.stored_value,
        "file_name": str(file.filename or ""),
    })
    current["menus"] = menus
    await db_config_service.save_json_section("prebooking", current)
    prebooking_settings_service.remember(request.state.hotel_code, "prebooking", current)
    return {"saved": True, "menus": menus}


@router.delete("/api/config/prebooking/menus/{menu_id}")
async def delete_prebooking_menu(menu_id: str, request: Request):
    """Stop sending a menu and delete its PDF from S3."""
    from services.s3_storage_service import s3_storage_service

    current = await _saved_prebooking_or_409()
    menus = [m for m in (current.get("menus") or []) if isinstance(m, dict)]
    target = next((m for m in menus if str(m.get("id")) == str(menu_id)), None)
    if target is None:
        raise HTTPException(status_code=404, detail="Menu not found")
    current["menus"] = [m for m in menus if m is not target]
    await db_config_service.save_json_section("prebooking", current)
    prebooking_settings_service.remember(request.state.hotel_code, "prebooking", current)
    await s3_storage_service.delete_stored(str(target.get("stored") or ""))
    return {"saved": True, "menus": current["menus"]}


@router.put("/api/config/ticketing")
async def update_ticketing_settings(update: PrebookingSettingsUpdate, request: Request):
    """Save widget / group ids and allowed departments."""
    try:
        clean = prebooking_settings_service.normalize_ticketing(update.settings)
    except prebooking_settings_service.SettingsError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"saved": True, "ticketing": await _save_settings_section(request, "ticketing", clean, [])}


@router.put("/api/hotel-properties/{property_id}")
async def update_hotel_sub_property(property_id: int, update: HotelPropertyUpdate, request: Request):
    """Edit one property of the active hotel: the name the bot speaks, its
    location-picker label, city and aliases, plus its FMS entity id. Refreshes
    the location picker so the change shows on the next chat."""
    try:
        values = prebooking_settings_service.clean_property_update(
            name=update.name,
            location_label=update.location_label,
            city=update.city,
            aliases=update.aliases,
            fms_entity_id=update.fms_entity_id,
        )
    except prebooking_settings_service.SettingsError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    # An FMS id lives in the ticketing settings, and writing it would switch the
    # hotel to admin-managed ticket routing. That switch only happens from the
    # settings save, never as a side effect here.
    ticketing = await db_config_service.get_json_section("ticketing", None)
    if not isinstance(ticketing, dict) and values["fms_entity_id"]:
        raise HTTPException(
            status_code=409,
            detail="Save the Ticket routing section first; this hotel does not use it yet.",
        )

    hotel_id = await db_config_service.get_current_hotel_id()
    async with AsyncSessionLocal() as session:
        row = (await session.execute(
            select(Property).where(Property.id == property_id, Property.hotel_id == hotel_id)
        )).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="Property not found for this hotel")
        location = _json_field(row.location)
        location = dict(location) if isinstance(location, dict) else {}
        location["label"] = values["location_label"]
        if values["city"]:
            location["city"] = values["city"]
        else:
            location.pop("city", None)
        row.name = values["name"]
        row.aliases = values["aliases"] or None
        row.location = location
        slug = str(row.property_slug)
        await session.commit()

    if isinstance(ticketing, dict):
        updated = prebooking_settings_service.set_property_entry(
            ticketing, slug, values["fms_entity_id"], name=values["name"]
        )
        await db_config_service.save_json_section("ticketing", updated)
        prebooking_settings_service.remember(request.state.hotel_code, "ticketing", updated)
    prebooking = await db_config_service.get_json_section("prebooking", None)
    if isinstance(prebooking, dict):
        # Keep the chat's hotel-name index in step with the rename.
        context = await _prebooking_admin_context()
        reindexed = prebooking_settings_service.index_properties(prebooking, context["properties"])
        await db_config_service.save_json_section("prebooking", reindexed)
        prebooking_settings_service.remember(request.state.hotel_code, "prebooking", reindexed)

    from services.kb_table_reader import invalidate_hotel_caches

    invalidate_hotel_caches(request.state.hotel_code)
    welcome = await refresh_welcome_cache(request)
    return {
        "saved": True,
        "property": {"id": property_id, "slug": slug, **values},
        "location_count": (welcome or {}).get("location_count") if isinstance(welcome, dict) else None,
    }


@router.get("/api/config")
async def get_business_config():
    """Get full business configuration from database."""
    try:
        return await db_config_service.get_full_config()
    except Exception as e:
        # Fallback to JSON
        _safe_print(f"DB error, using JSON fallback: {e}")
        return config_service.load_config()


@router.put("/api/config")
async def update_business_config(config: dict):
    """Update full business configuration in database."""
    try:
        if await db_config_service.save_full_config(config):
            return {"message": "Configuration saved to database"}
    except Exception as e:
        _safe_print(f"DB error, saving to JSON: {e}")

    # Fallback to JSON
    if config_service.save_config(config):
        return {"message": "Configuration saved to JSON"}
    raise HTTPException(status_code=500, detail="Failed to save configuration")


@router.get("/api/config/business")
async def get_business_info():
    """Get business basic info from database."""
    try:
        return await db_config_service.get_business_info()
    except Exception as e:
        _safe_print(f"DB error: {e}")
        return config_service.get_business_info()


@router.put("/api/config/business")
async def update_business_info(update: UpdateBusinessInfo):
    """Update business basic info in database."""
    updates = update.model_dump(exclude_unset=True)

    try:
        result = await db_config_service.update_business_info(updates)
        return result
    except Exception as e:
        _safe_print(f"DB error, using JSON: {e}")
        return config_service.update_business_info(updates)


@router.get("/api/config/onboarding/business")
async def get_onboarding_business():
    """
    Step 1 onboarding profile.
    Returns the extended business profile used by admin setup.
    """
    try:
        return await db_config_service.get_business_info()
    except Exception as e:
        _safe_print(f"DB error, using JSON onboarding business: {e}")
        return config_service.get_onboarding_business()


@router.put("/api/config/onboarding/business")
async def update_onboarding_business(update: UpdateOnboardingBusiness):
    """
    Step 1 onboarding profile update.
    Persists all extended fields in JSON config and core fields in DB when available.
    """
    updates = update.model_dump(exclude_unset=True)
    requested_code = _normalize_tenant(
        str(updates.get("id") or updates.get("code") or "").strip()
    )

    scope_token = None
    if requested_code:
        updates["id"] = requested_code
        updates["code"] = requested_code
        scope_token = db_config_service.set_hotel_context(requested_code)

    try:
        return await db_config_service.update_business_info(updates)
    except Exception as e:
        _safe_print(f"DB error, using JSON onboarding business update: {e}")
        return config_service.update_onboarding_business(updates)
    finally:
        if scope_token is not None:
            db_config_service.reset_hotel_context(scope_token)


@router.post("/api/config/onboarding/refresh-welcome")
async def refresh_welcome_cache(request: Request, short_welcome_only: Optional[bool] = None):
    """Recompute the multi-property welcome cache for the active hotel.

    Reads the live property catalog and writes back property_count,
    location_count, and multi_property_location_manifest into the business
    config. Lightweight counterpart to /api/rag/reindex — no KB re-ingest, no
    prompt regen.

    IMPORTANT: this writes via update_business_info (a MERGE into the DB
    business JSON), NOT config_service.refresh_multi_property_welcome_message.
    The shared refresh rebuilds the whole business blob from config_service's
    view, which for a DB-first tenant with no config file falls back to the
    hotel template and silently overwrites admin-set fields like bot_name. A
    merge only touches the multi-property keys and preserves everything else.

    short_welcome_only=True suppresses the verbose auto-generated welcome dump —
    use for tenants with a frontend location picker (e.g. zone), which show a
    short greeting + dropdown instead.
    """
    from services.kb_table_reader import get_property_catalog_db

    code = _normalize_tenant(
        str(request.headers.get("x-hotel-code") or "").strip()
    ) or db_config_service.get_current_hotel_code()
    scope_token = db_config_service.set_hotel_context(code) if code else None
    try:
        rows = await get_property_catalog_db(hotel_code=code)
        rows = [dict(r) for r in (rows or []) if isinstance(r, dict)]
        manifest = config_service._build_location_manifest_from_property_catalog(rows)
        property_count = len(rows)
        location_count = len(manifest)

        # Read the current business config so we preserve every admin-set field
        # (bot_name, welcome_message, theme, etc.), then merge ONLY the
        # multi-property cache keys and write the DB section snapshot DIRECTLY
        # via save_json_section. We do NOT route through update_business_info:
        # that round-trips through a local JSON file whose write is skipped in
        # deploy mode, so property_count never lands in the snapshot that
        # get_business_info reads.
        existing = await db_config_service.get_business_info()
        merged = dict(existing) if isinstance(existing, dict) else {}
        # resolved_welcome_message is a derived field — don't persist it.
        merged.pop("resolved_welcome_message", None)
        merged["property_count"] = property_count
        merged["location_count"] = location_count
        merged["multi_property_location_manifest"] = manifest
        # Picker tenants don't show the auto dump; clear any stale one.
        merged["multi_property_welcome_message"] = ""
        if short_welcome_only is not None:
            merged["short_welcome_only"] = bool(short_welcome_only)
        await db_config_service.save_json_section("business", merged)

        return {
            "hotel_code": code,
            "short_welcome_only": short_welcome_only,
            "bot_name": merged.get("bot_name"),
            "property_count": property_count,
            "location_count": location_count,
        }
    finally:
        if scope_token is not None:
            db_config_service.reset_hotel_context(scope_token)


@router.get("/api/config/onboarding/prompts")
async def get_onboarding_prompts():
    """Step 2 onboarding prompt configuration."""
    try:
        return await db_config_service.get_prompts()
    except Exception as e:
        _safe_print(f"DB error, using JSON onboarding prompts: {e}")
        return config_service.get_prompts()


@router.put("/api/config/onboarding/prompts")
async def update_onboarding_prompts(update: UpdatePromptConfig):
    """Step 2 onboarding prompt configuration update."""
    updates = update.model_dump(exclude_unset=True)
    try:
        return await db_config_service.update_prompts(updates)
    except Exception as e:
        _safe_print(f"DB error, using JSON onboarding prompts update: {e}")
        if config_service.update_prompts(updates):
            return config_service.get_prompts()
        raise HTTPException(status_code=500, detail="Failed to save prompt configuration")


@router.get("/api/config/onboarding/prompt-templates")
async def list_prompt_templates():
    """List available system prompt templates for onboarding."""
    return config_service.list_prompt_templates()


@router.post("/api/config/onboarding/prompts/apply-template")
async def apply_prompt_template(data: ApplyPromptTemplateRequest):
    """Apply a system prompt template and optional NLU defaults."""
    try:
        applied = config_service.apply_prompt_template(data.template_id)
        try:
            prompts = applied.get("prompts", {}) if isinstance(applied, dict) else {}
            knowledge = applied.get("knowledge_base", {}) if isinstance(applied, dict) else {}
            if isinstance(prompts, dict):
                await db_config_service.update_prompts(prompts)
            if isinstance(knowledge, dict):
                await db_config_service.update_knowledge_config(knowledge)
        except Exception as db_sync_err:
            _safe_print(f"DB sync warning (apply template): {db_sync_err}")
        return {"message": f"Prompt template {data.template_id} applied", "data": applied}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Prompt template not found")


@router.get("/api/config/onboarding/knowledge")
async def get_onboarding_knowledge():
    """Step 3 onboarding: knowledge sources + NLU do/don't rules."""
    try:
        return await db_config_service.get_knowledge_config()
    except Exception as e:
        _safe_print(f"DB error, using JSON onboarding knowledge: {e}")
        return config_service.get_knowledge_config()


@router.put("/api/config/onboarding/knowledge")
async def update_onboarding_knowledge(update: UpdateKnowledgeConfig):
    """Step 3 onboarding update: knowledge sources + NLU do/don't rules."""
    updates = update.model_dump(exclude_unset=True)
    try:
        return await db_config_service.update_knowledge_config(updates)
    except Exception as e:
        _safe_print(f"DB error, using JSON onboarding knowledge update: {e}")
        return config_service.update_knowledge_config(updates)


@router.get("/api/config/onboarding/knowledge/conflicts")
async def get_onboarding_knowledge_conflicts():
    """
    Step 3 onboarding validation:
    detect conflicts between configured services and uploaded KB facts.
    """
    return config_service.get_knowledge_conflict_report()


# ============ Evaluation + Observability APIs ============

@router.get("/api/evaluation/summary")
async def get_evaluation_summary(hours: int = 24):
    """Routing/retrieval/policy quality summary for dashboard."""
    window = max(1, min(int(hours), 168))
    return evaluation_metrics_service.get_summary(hours=window)


@router.get("/api/evaluation/events")
async def get_evaluation_events(limit: int = 100):
    """Recent evaluated chat events."""
    capped = max(1, min(int(limit), 500))
    events = evaluation_metrics_service.get_recent_events(limit=capped)
    return {"events": events, "count": len(events)}


@router.get("/api/observability/status")
async def get_observability_status():
    """Runtime diagnostics: logger + gateway controls."""
    return {
        "observability": observability_service.get_status(),
        "gateway": gateway_service.snapshot_state(),
    }


@router.get("/api/observability/events")
async def get_observability_events(limit: int = 100, event: str = ""):
    """Tail observability JSONL events (newest first)."""
    capped = max(1, min(int(limit), 500))
    rows = observability_service.read_recent_events(limit=capped, event_filter=event)
    return {
        "events": rows,
        "count": len(rows),
        "event_filter": str(event or ""),
    }


# ============ RAG Management API ============

@router.get("/api/rag/status")
async def get_rag_status(request: Request, tenant_id: Optional[str] = None):
    """Get RAG backend/index status for a tenant."""
    resolved_tenant = _resolve_scoped_rag_tenant(request, tenant_id)
    scope_token = db_config_service.set_hotel_context(resolved_tenant)
    try:
        status = rag_service.get_status(tenant_id=resolved_tenant)
        status["backend_mission"] = "tenant-scoped retrieval for web widget + whatsapp"
        status["knowledge_sources_configured"] = config_service.get_knowledge_config().get("sources", [])
        return status
    finally:
        db_config_service.reset_hotel_context(scope_token)


@router.post("/api/rag/reindex")
async def reindex_rag(data: RAGReindexRequest, request: Request):
    """Ingest/chunk knowledge docs and rebuild tenant-scoped RAG index."""
    resolved_tenant = _resolve_scoped_rag_tenant(request, data.tenant_id)
    await _ensure_property_registered(resolved_tenant)
    scope_token = db_config_service.set_hotel_context(resolved_tenant)
    try:
        business = config_service.get_business_info()
        resolved_business_type = data.business_type or business.get("type", "generic")
        config_service.invalidate_kb_docs_cache(resolved_tenant)

        file_paths = data.file_paths
        if file_paths is None:
            knowledge_sources = config_service.get_knowledge_config().get("sources", [])
            if not isinstance(knowledge_sources, list):
                knowledge_sources = []
            normalized_sources = [
                str(source).strip()
                for source in knowledge_sources
                if str(source).strip()
            ]
            file_paths = normalized_sources or None

        report = await rag_service.ingest_from_knowledge_base(
            tenant_id=resolved_tenant,
            business_type=resolved_business_type,
            clear_existing=data.clear_existing,
            file_paths=file_paths,
        )
        try:
            config_service.rebuild_structured_kb_library(max_sources=50, save=True)
            library_index = await config_service.ensure_structured_kb_llm_books(max_sources=50, force=True)
            if isinstance(library_index, dict):
                report["library_books"] = len(library_index.get("books") or [])
                report["library_pages"] = len(library_index.get("pages") or [])
                report["library_coverage_ratio"] = float(
                    ((library_index.get("coverage") or {}).get("coverage_ratio") or 0.0)
                )
                report["library_book_index_generator"] = str(
                    library_index.get("book_index_generator") or ""
                ).strip()
                report["library_source_signature"] = str(
                    library_index.get("source_signature") or ""
                ).strip()
                try:
                    from services.flow_logger import log_book_routing_event

                    log_book_routing_event(
                        event="kb_reindex_library_summary",
                        tenant_id=str(resolved_tenant or ""),
                        route="index",
                        payload={
                            "books": int(report.get("library_books") or 0),
                            "pages": int(report.get("library_pages") or 0),
                            "coverage_ratio": float(report.get("library_coverage_ratio") or 0.0),
                            "book_index_generator": str(report.get("library_book_index_generator") or ""),
                            "source_signature": str(report.get("library_source_signature") or ""),
                        },
                    )
                except Exception:
                    pass
        except Exception as exc:
            report["library_reindex_warning"] = str(exc)

        try:
            from services.flow_logger import log_reindex
            log_reindex(
                tenant_id=resolved_tenant,
                files=report.get("files") or (file_paths or []),
                chunks_created=int(report.get("chunks_indexed") or 0),
                backend=str(report.get("backend_used") or "local"),
                clear_existing=data.clear_existing,
            )
        except Exception:
            pass

        # ── Enrich service KB + regenerate prompts after reindex ──
        config_service.invalidate_kb_docs_cache(resolved_tenant)
        try:
            await config_service.enrich_service_kb_records(published_by="system")
            report["kb_enrichment"] = "ok"
        except Exception as _enrich_err:
            report["kb_enrichment_warning"] = str(_enrich_err)
        try:
            if not bool(getattr(settings, "reindex_regenerate_service_prompts_enabled", False)):
                # Disabled by default: a reindex must NOT clobber hand-tuned service
                # prompts. They regenerate only on an explicit service edit/save.
                report["prompts_regenerated"] = "skipped (auto-regen disabled)"
            else:
                # Source of truth for services is the DB. JSON's services array
                # may contain stale leftovers from old dev sessions and would
                # otherwise trigger LLM prompt generation for services the user
                # never created in admin.
                services = await db_config_service.get_services()
                _regen_count = 0
                for svc in (services or []):
                    svc_id = str(svc.get("id") or "").strip()
                    if not svc_id:
                        continue
                    kb_record = config_service.get_service_kb_record(service_id=svc_id)
                    if isinstance(kb_record, dict):
                        svc["extracted_knowledge"] = str(kb_record.get("extracted_knowledge") or "").strip()
                    prompt = await generate_service_system_prompt(svc)
                    if prompt:
                        await db_config_service.save_generated_prompt(svc_id, prompt)
                        _regen_count += 1
                report["prompts_regenerated"] = _regen_count
        except Exception as _prompt_err:
            report["prompt_regen_warning"] = str(_prompt_err)

        try:
            welcome_refresh = await config_service.refresh_multi_property_welcome_message(
                hotel_code=resolved_tenant,
            )
            if isinstance(welcome_refresh, dict) and welcome_refresh:
                report["multi_property_welcome"] = {
                    "property_count": int(welcome_refresh.get("property_count") or 0),
                    "location_count": int(welcome_refresh.get("location_count") or 0),
                }
        except Exception as _welcome_err:
            report["multi_property_welcome_warning"] = str(_welcome_err)

        return report
    finally:
        db_config_service.reset_hotel_context(scope_token)


@router.post("/api/rag/query")
async def debug_rag_query(data: RAGQueryRequest, request: Request):
    """Debug endpoint to test retrieval + grounded answer generation."""
    question = str(data.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="question is required")

    resolved_tenant = _resolve_scoped_rag_tenant(request, data.tenant_id)
    scope_token = db_config_service.set_hotel_context(resolved_tenant)
    try:
        business = config_service.get_business_info()
        resolved_business_type = data.business_type or business.get("type", "generic")
        resolved_name = data.hotel_name or business.get("name", "Business")
        resolved_city = data.city or business.get("city", "")

        answer = await rag_service.answer_question(
            question=question,
            hotel_name=resolved_name,
            city=resolved_city,
            tenant_id=resolved_tenant,
            business_type=resolved_business_type,
        )
        if answer is None:
            return {
                "handled": False,
                "reason": "no_retrieval_match_or_low_confidence",
                "tenant_id": resolved_tenant,
            }
        return {
            "handled": True,
            "tenant_id": resolved_tenant,
            "answer": answer.answer,
            "confidence": answer.confidence,
            "sources": answer.sources,
        }
    finally:
        db_config_service.reset_hotel_context(scope_token)


@router.post("/api/rag/upload")
async def upload_rag_files(
    request: Request,
    files: List[UploadFile] = File(...),
    tenant_id: str = Form(default=""),
    add_to_sources: bool = Form(default=True),
):
    """Upload knowledge files for one tenant/property and append them to that tenant's KB set."""
    if not files:
        raise HTTPException(status_code=400, detail="No files provided")

    resolved_tenant = _resolve_scoped_rag_tenant(request, tenant_id)
    await _ensure_property_registered(resolved_tenant)
    normalized_tenant = _normalize_tenant(resolved_tenant)
    scope_token = db_config_service.set_hotel_context(resolved_tenant)

    try:
        _files_wiped = 0
        _db_deleted = 0

        saved: list[dict[str, Any]] = []
        for file in files:
            safe_name = _safe_filename(file.filename or "")
            target_name = f"{uuid4().hex[:8]}_{safe_name}"
            content = await file.read()
            saved.append(
                {
                    "original_name": file.filename or safe_name,
                    "saved_name": target_name,
                    "path": _build_kb_source_ref(stored_name=target_name),
                    "source_ref": _build_kb_source_ref(stored_name=target_name),
                    "size_bytes": len(content),
                    "storage": "db",
                }
            )
            await file.close()

            # Persist KB content to DB so files survive restarts / machine changes.
            try:
                text_content = content.decode("utf-8", errors="replace")
                content_hash = hashlib.sha256(content).hexdigest()
                await db_config_service.save_kb_file(
                    original_name=file.filename or safe_name,
                    stored_name=target_name,
                    content=text_content,
                    content_hash=content_hash,
                )
            except Exception as _kb_err:
                import traceback
                _safe_print(f"[DB] KB file save FAILED: {_kb_err}")
                _safe_print(traceback.format_exc())

        # Populate stable DB source refs with real KB file ids.
        try:
            rows = await db_config_service.list_kb_files()
        except Exception:
            rows = []
        rows_by_stored_name: dict[str, dict[str, Any]] = {}
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            stored_name = str(row.get("stored_name") or "").strip()
            if stored_name:
                rows_by_stored_name[stored_name] = row
        for entry in saved:
            stored_name = str(entry.get("saved_name") or "").strip()
            row = rows_by_stored_name.get(stored_name, {})
            source_ref = _build_kb_source_ref(
                kb_file_id=row.get("id"),
                stored_name=stored_name,
            )
            if source_ref:
                entry["source_ref"] = source_ref
                entry["path"] = source_ref

        if add_to_sources:
            try:
                _existing_cfg = await db_config_service.get_knowledge_config()
            except Exception:
                _existing_cfg = config_service.get_knowledge_config()
            existing_sources = _existing_cfg.get("sources", []) if isinstance(_existing_cfg, dict) else []
            if not isinstance(existing_sources, list):
                existing_sources = []
            sources = [str(source).strip() for source in existing_sources if str(source).strip()]
            if _is_kb_db_only_mode():
                sources = [source for source in sources if source.startswith("db://kb_files/")]
            for entry in saved:
                source_ref = str(entry.get("source_ref") or entry.get("path") or "").strip()
                if source_ref and source_ref not in sources:
                    sources.append(source_ref)
            try:
                await db_config_service.update_knowledge_config({"sources": sources})
            except Exception as kb_cfg_err:
                _safe_print(f"DB sync warning (kb sources update): {kb_cfg_err}")
                config_service.update_knowledge_config({"sources": sources})

        config_service.invalidate_kb_docs_cache(resolved_tenant)

        # Log KB upload event to flow.log
        try:
            from services.flow_logger import log_kb_upload
            for entry in saved:
                log_kb_upload(
                    files_wiped_disk=_files_wiped,
                    db_records_deleted=_db_deleted,
                    new_file_name=entry["original_name"],
                    new_file_bytes=int(entry.get("size_bytes") or 0),
                    saved_path=entry["path"],
                    tenant_id=normalized_tenant,
                )
        except Exception:
            pass

        # Trigger service knowledge refresh + prompt regeneration after KB upload.
        # This must never fail the upload response path.
        async def _refresh_kb_and_prompts():
            try:
                await config_service.enrich_service_kb_records(published_by="system")
            except Exception as _enrich_err:
                _safe_print(f"[KB] Service-KB refresh failed (non-fatal): {_enrich_err}")
            # Regenerate system prompts for all services so they use updated KB.
            # Read services from DB (source of truth) — JSON's `services` array
            # may contain stale leftovers from old sessions which would
            # otherwise trigger LLM prompt generation for services the user
            # never created in admin.
            try:
                if not bool(getattr(settings, "reindex_regenerate_service_prompts_enabled", False)):
                    _safe_print("[KB] Service prompt auto-regen disabled — skipping (edit a service to regenerate).")
                else:
                    services = await db_config_service.get_services()
                    for svc in (services or []):
                        if not isinstance(svc, dict) or not bool(svc.get("is_active", True)):
                            continue
                        svc_id = str(svc.get("id") or "").strip()
                        if not svc_id:
                            continue
                        # Get the latest extracted_knowledge for the service
                        kb_record = config_service.get_service_kb_record(service_id=svc_id)
                        if isinstance(kb_record, dict):
                            svc["extracted_knowledge"] = str(kb_record.get("extracted_knowledge") or "").strip()
                        prompt = await generate_service_system_prompt(svc)
                        if prompt:
                            await db_config_service.save_generated_prompt(svc_id, prompt)
                            _safe_print(f"[KB] Regenerated system prompt for service '{svc_id}'")
            except Exception as _prompt_err:
                _safe_print(f"[KB] Prompt regeneration failed (non-fatal): {_prompt_err}")
        try:
            asyncio.create_task(_refresh_kb_and_prompts())
        except Exception as enrich_schedule_error:
            _safe_print(f"[KB] Service-KB refresh scheduling failed (non-fatal): {enrich_schedule_error}")

        return {
            "tenant_id": normalized_tenant,
            "uploaded_count": len(saved),
            "files": saved,
            "add_to_sources": add_to_sources,
            "reindex_required": True,
        }
    finally:
        db_config_service.reset_hotel_context(scope_token)


@router.get("/api/rag/files")
async def list_rag_files(
    request: Request,
    tenant_id: str = "",
):
    """List uploaded KB files for one tenant/property."""
    resolved_tenant = _resolve_scoped_rag_tenant(request, tenant_id)
    await _ensure_property_registered(resolved_tenant)
    normalized_tenant = _normalize_tenant(resolved_tenant)
    scope_token = db_config_service.set_hotel_context(resolved_tenant)

    try:
        try:
            _knowledge_cfg = await db_config_service.get_knowledge_config()
        except Exception:
            _knowledge_cfg = config_service.get_knowledge_config()
        selected_sources_raw = _knowledge_cfg.get("sources", []) if isinstance(_knowledge_cfg, dict) else []
        if not isinstance(selected_sources_raw, list):
            selected_sources_raw = []
        selected_sources: set[str] = {
            str(source).strip() for source in selected_sources_raw if str(source).strip()
        }

        rows = await db_config_service.list_kb_files()
        files: list[dict[str, Any]] = []
        for row in rows:
            stored_name = str(row.get("stored_name") or "").strip()
            source_ref = _build_kb_source_ref(
                kb_file_id=row.get("id"),
                stored_name=stored_name,
            )
            is_selected = bool(
                source_ref and (
                    source_ref in selected_sources
                    or any(
                        _source_matches_kb_file(
                            selected_source,
                            kb_file_id=row.get("id"),
                            stored_name=stored_name,
                        )
                        for selected_source in selected_sources
                    )
                )
            )
            files.append(
                {
                    **row,
                    "path": source_ref,
                    "source_ref": source_ref,
                    "storage": "db",
                    "exists_on_disk": False,
                    "is_selected": is_selected,
                }
            )

        return {
            "tenant_id": normalized_tenant,
            "files": files,
            "selected_sources": sorted(selected_sources),
        }
    finally:
        db_config_service.reset_hotel_context(scope_token)


@router.delete("/api/rag/files/{stored_name}")
async def delete_rag_file(
    stored_name: str,
    request: Request,
    tenant_id: str = "",
):
    """Delete one uploaded KB file for one tenant/property."""
    safe_stored_name = Path(str(stored_name or "")).name
    if not safe_stored_name or safe_stored_name != str(stored_name or ""):
        raise HTTPException(status_code=400, detail="Invalid KB file name")

    resolved_tenant = _resolve_scoped_rag_tenant(request, tenant_id)
    await _ensure_property_registered(resolved_tenant)
    normalized_tenant = _normalize_tenant(resolved_tenant)
    scope_token = db_config_service.set_hotel_context(resolved_tenant)

    try:
        rows = await db_config_service.list_kb_files()
        matched_row = None
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            if str(row.get("stored_name") or "").strip() == safe_stored_name:
                matched_row = row
                break
        db_deleted = await db_config_service.delete_kb_file(safe_stored_name)
        disk_deleted = False

        selected_sources_raw = config_service.get_knowledge_config().get("sources", [])
        if not isinstance(selected_sources_raw, list):
            selected_sources_raw = []
        matched_id = matched_row.get("id") if isinstance(matched_row, dict) else None
        next_sources = [
            str(source).strip()
            for source in selected_sources_raw
            if str(source).strip()
            and not _source_matches_kb_file(
                source,
                kb_file_id=matched_id,
                stored_name=safe_stored_name,
            )
        ]
        removed_from_sources = next_sources != selected_sources_raw
        if removed_from_sources:
            try:
                await db_config_service.update_knowledge_config({"sources": next_sources})
            except Exception as kb_cfg_err:
                _safe_print(f"DB sync warning (kb source delete): {kb_cfg_err}")
                config_service.update_knowledge_config({"sources": next_sources})

        config_service.invalidate_kb_docs_cache(resolved_tenant)
        try:
            asyncio.create_task(config_service.enrich_service_kb_records(published_by="system"))
        except Exception:
            pass

        return {
            "tenant_id": normalized_tenant,
            "stored_name": safe_stored_name,
            "source_ref": _build_kb_source_ref(kb_file_id=matched_id, stored_name=safe_stored_name),
            "db_deleted": db_deleted,
            "disk_deleted": disk_deleted,
            "removed_from_sources": removed_from_sources,
            "reindex_required": True,
        }
    finally:
        db_config_service.reset_hotel_context(scope_token)


@router.post("/api/rag/jobs/start")
async def start_rag_index_job(data: RAGStartJobRequest, request: Request):
    """Start background RAG indexing job."""
    resolved_tenant = _resolve_scoped_rag_tenant(request, data.tenant_id)
    await _ensure_property_registered(resolved_tenant)
    scope_token = db_config_service.set_hotel_context(resolved_tenant)
    try:
        business = config_service.get_business_info()
        resolved_business_type = data.business_type or business.get("type", "generic")

        file_paths = data.file_paths
        if file_paths is None:
            knowledge_sources = config_service.get_knowledge_config().get("sources", [])
            if not isinstance(knowledge_sources, list):
                knowledge_sources = []
            file_paths = [
                str(source).strip()
                for source in knowledge_sources
                if str(source).strip()
            ]

        job = await rag_job_service.start_index_job(
            tenant_id=resolved_tenant,
            business_type=resolved_business_type,
            clear_existing=data.clear_existing,
            file_paths=file_paths,
        )
        return job
    finally:
        db_config_service.reset_hotel_context(scope_token)


@router.get("/api/rag/jobs")
async def list_rag_jobs(limit: int = 20):
    """List recent background RAG indexing jobs."""
    jobs = await rag_job_service.list_jobs(limit=limit)
    return {"jobs": jobs, "count": len(jobs)}


@router.get("/api/rag/jobs/{job_id}")
async def get_rag_job(job_id: str):
    """Get one background indexing job status."""
    job = await rag_job_service.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="RAG job not found")
    return job


@router.get("/api/config/onboarding/ui")
async def get_onboarding_ui():
    """Step 4 onboarding: channel + branding + customization settings."""
    try:
        return await db_config_service.get_ui_settings()
    except Exception as e:
        _safe_print(f"DB error, using JSON onboarding ui settings: {e}")
        return config_service.get_ui_settings()


@router.put("/api/config/onboarding/ui")
async def update_onboarding_ui(update: UpdateUISettings):
    """Step 4 onboarding update: channel + branding + customization settings."""
    updates = update.model_dump(exclude_unset=True)
    try:
        return await db_config_service.update_ui_settings(updates)
    except Exception as e:
        _safe_print(f"DB error, using JSON onboarding ui update: {e}")
        return config_service.update_ui_settings(updates)


@router.get("/api/config/capabilities")
async def get_config_capabilities():
    """Get all capabilities from database."""
    try:
        return await db_config_service.get_capabilities()
    except Exception as e:
        _safe_print(f"DB error: {e}")
        return config_service.get_capabilities()


@router.put("/api/config/capabilities/{capability_id}")
async def update_config_capability(capability_id: str, update: UpdateCapability):
    """Update a capability in database."""
    updates = update.model_dump(exclude_unset=True)

    try:
        if await db_config_service.update_capability(capability_id, updates):
            return {"message": f"Capability {capability_id} updated in database"}
    except Exception as e:
        _safe_print(f"DB error: {e}")

    if config_service.update_capability(capability_id, updates):
        return {"message": f"Capability {capability_id} updated in JSON"}
    raise HTTPException(status_code=404, detail="Capability not found")


@router.post("/api/config/capabilities")
async def add_config_capability(capability_id: str, data: UpdateCapability):
    """Add a new capability to database."""
    updates = data.model_dump(exclude_unset=True)

    try:
        if await db_config_service.add_capability(capability_id, updates):
            return {"message": f"Capability {capability_id} added"}
    except Exception as e:
        _safe_print(f"DB error: {e}")

    raise HTTPException(status_code=500, detail="Failed to add capability")


@router.delete("/api/config/capabilities/{capability_id}")
async def delete_config_capability(capability_id: str):
    """Delete a capability from database."""
    try:
        if await db_config_service.delete_capability(capability_id):
            return {"message": f"Capability {capability_id} deleted"}
    except Exception as e:
        _safe_print(f"DB error: {e}")

    raise HTTPException(status_code=500, detail="Failed to delete capability")


async def _generate_and_save_prompt(service_id: str) -> None:
    """Background task: generate an LLM system prompt for a service and persist it."""
    try:
        ok, services = await _call_db_config_with_fast_fallback(
            "get_services",
            db_config_service.get_services(),
        )
        services = services if (ok and isinstance(services, list)) else config_service.get_services()
        sid = str(service_id or "").strip().lower()
        service = next((s for s in (services or []) if str(s.get("id") or "").strip().lower() == sid), None)
        if not service:
            _safe_print(f"[PromptWriter] Service '{service_id}' not found after save, skipping generation.")
            return
        # LOCK GUARD: if the service's system prompt is locked / manually
        # overridden (generated_system_prompt_override=True), never auto-regenerate
        # it — that would clobber the custom prompt. Admin must explicitly unlock
        # (unlock-prompt endpoint) to re-enable LLM generation.
        if bool(service.get("generated_system_prompt_override")):
            _safe_print(f"[PromptWriter] Service '{service_id}' prompt is locked — skipping auto-regeneration.")
            return
        prompt = await generate_service_system_prompt(service)
        if prompt:
            db_prompt_ok = await db_config_service.save_generated_prompt(service_id, prompt)
            if not db_prompt_ok:
                # DB save failed (service may only exist in JSON) â€” persist to JSON directly.
                config_service.update_service(service_id, {"generated_system_prompt": prompt})
                _safe_print(f"[PromptWriter] Prompt saved to JSON for '{service_id}' ({len(prompt)} chars)")
            else:
                _safe_print(f"[PromptWriter] Prompt saved to DB for '{service_id}' ({len(prompt)} chars)")
            try:
                from services.flow_logger import log_prompt_regen
                pack = service.get("service_prompt_pack") or {}
                extracted_kb = str(
                    service.get("extracted_knowledge")
                    or (pack.get("extracted_knowledge") if isinstance(pack, dict) else "")
                    or ""
                )
                log_prompt_regen(
                    service_id=service_id,
                    service_name=str(service.get("name") or service_id),
                    extracted_kb_chars=len(extracted_kb),
                    generated_prompt_chars=len(prompt),
                )
            except Exception:
                pass
    except Exception as e:
        _safe_print(f"[PromptWriter] Background generation failed for '{service_id}': {e}")


async def _log_service_save_snapshot(
    *,
    service_id: str,
    action: str,
    source: str,
    success: bool,
    error: str = "",
) -> None:
    """Write a service persistence snapshot to debugging logs."""
    try:
        sid = str(service_id or "").strip().lower()
        if not sid:
            return
        ok, services = await _call_db_config_with_fast_fallback(
            "get_services",
            db_config_service.get_services(),
        )
        rows = services if (ok and isinstance(services, list)) else config_service.get_services()
        if not isinstance(rows, list):
            rows = []
        svc = next(
            (item for item in rows if str(item.get("id") or "").strip().lower() == sid),
            None,
        )
        pack = (svc or {}).get("service_prompt_pack") or {}
        if not isinstance(pack, dict):
            pack = {}
        from services.flow_logger import log_service_config_save

        log_service_config_save(
            action=str(action or "").strip() or "unknown_action",
            source=str(source or "").strip() or "unknown_source",
            service_id=sid,
            service_name=str((svc or {}).get("name") or sid).strip(),
            description_len=len(str((svc or {}).get("description") or "").strip()),
            ticketing_policy_len=len(str((svc or {}).get("ticketing_policy") or "").strip()),
            extracted_knowledge_len=len(str(pack.get("extracted_knowledge") or "").strip()),
            generated_prompt_len=len(str((svc or {}).get("generated_system_prompt") or "").strip()),
            success=bool(success),
            error=str(error or "").strip(),
        )
    except Exception:
        pass


@router.get("/api/config/services")
async def get_config_services():
    """Get all services from database."""
    ok, db_services = await _call_db_config_with_fast_fallback(
        "get_services",
        db_config_service.get_services(),
    )
    if ok and isinstance(db_services, list):
        return db_services
    return config_service.get_services()


def _invalidate_form_cache(service_id: str) -> None:
    """Invalidate form fields cache when a service is created/updated."""
    try:
        from services.form_fields_service import invalidate_cache
        invalidate_cache(service_id)
    except Exception:
        pass


@router.post("/api/config/services")
async def add_config_service(service: AddService):
    """Add a new service to database."""
    payload = service.model_dump()
    ok, db_saved = await _call_db_config_with_fast_fallback(
        "add_service",
        db_config_service.add_service(payload),
    )
    if ok and db_saved:
        _invalidate_form_cache(str(payload.get("id") or ""))
        asyncio.create_task(_generate_and_save_prompt(payload.get("id")))
        asyncio.create_task(
            _log_service_save_snapshot(
                service_id=str(payload.get("id") or ""),
                action="add_service",
                source="db",
                success=True,
            )
        )
        return {"message": "Service added to database"}

    if config_service.add_service(payload):
        service_id_for_enrichment = payload.get("id")
        _invalidate_form_cache(str(service_id_for_enrichment or ""))
        asyncio.create_task(config_service.enrich_service_kb_records(
            service_id=service_id_for_enrichment, published_by="system"
        ))
        asyncio.create_task(_generate_and_save_prompt(service_id_for_enrichment))
        asyncio.create_task(
            _log_service_save_snapshot(
                service_id=str(service_id_for_enrichment or ""),
                action="add_service",
                source="json_fallback",
                success=True,
            )
        )
        return {"message": "Service added to JSON"}
    raise HTTPException(status_code=500, detail="Failed to add service")


@router.put("/api/config/services/{service_id}")
async def update_config_service(service_id: str, update: dict):
    """Update a service in database."""
    ok, db_updated = await _call_db_config_with_fast_fallback(
        "update_service",
        db_config_service.update_service(service_id, update),
    )
    if ok and db_updated:
        _invalidate_form_cache(service_id)
        asyncio.create_task(_generate_and_save_prompt(service_id))
        asyncio.create_task(
            _log_service_save_snapshot(
                service_id=service_id,
                action="update_service",
                source="db",
                success=True,
            )
        )
        return {"message": f"Service {service_id} updated in database"}

    if config_service.update_service(service_id, update):
        _invalidate_form_cache(service_id)
        asyncio.create_task(config_service.enrich_service_kb_records(
            service_id=service_id, published_by="system"
        ))
        asyncio.create_task(_generate_and_save_prompt(service_id))
        asyncio.create_task(
            _log_service_save_snapshot(
                service_id=service_id,
                action="update_service",
                source="json_fallback",
                success=True,
            )
        )
        return {"message": f"Service {service_id} updated in JSON"}
    raise HTTPException(status_code=404, detail="Service not found")


@router.post("/api/config/services/{service_id}/regenerate-prompt")
async def regenerate_service_prompt(service_id: str):
    """Regenerate the LLM-written system prompt for a service (blocking â€” returns new prompt)."""
    ok, services = await _call_db_config_with_fast_fallback(
        "get_services",
        db_config_service.get_services(),
    )
    services = services if (ok and isinstance(services, list)) else config_service.get_services()
    sid = str(service_id or "").strip().lower()
    service = next((s for s in (services or []) if str(s.get("id") or "").strip().lower() == sid), None)
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")
    # LOCK GUARD: a locked/overridden prompt must not be regenerated — it would
    # clobber the custom prompt. Caller must unlock first (unlock-prompt endpoint).
    if bool(service.get("generated_system_prompt_override")):
        return {
            "generated_system_prompt": str(service.get("generated_system_prompt") or ""),
            "skipped": "locked",
            "message": "Prompt is locked; regeneration skipped. Unlock first to regenerate.",
        }
    prompt = await generate_service_system_prompt(service)
    if not prompt:
        raise HTTPException(status_code=500, detail="Prompt generation failed")
    await db_config_service.save_generated_prompt(service_id, prompt)
    try:
        from services.flow_logger import log_prompt_regen
        pack = service.get("service_prompt_pack") or {}
        extracted_kb = str(
            service.get("extracted_knowledge")
            or (pack.get("extracted_knowledge") if isinstance(pack, dict) else "")
            or ""
        )
        log_prompt_regen(
            service_id=service_id,
            service_name=str(service.get("name") or service_id),
            extracted_kb_chars=len(extracted_kb),
            generated_prompt_chars=len(prompt),
        )
    except Exception:
        pass
    return {"generated_system_prompt": prompt}


@router.put("/api/config/services/{service_id}/prompt")
async def save_service_prompt(service_id: str, body: dict):
    """Save a manually edited system prompt for a service."""
    prompt = str(body.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt field is required")
    saved = await db_config_service.save_generated_prompt(service_id, prompt)
    if not saved:
        # DB save failed â€” fall back to JSON so service prompt is always persisted.
        json_saved = config_service.update_service(service_id, {"generated_system_prompt": prompt})
        if not json_saved:
            await _log_service_save_snapshot(
                service_id=service_id,
                action="manual_prompt_save",
                source="db",
                success=False,
                error="service_not_found_in_db_or_json",
            )
            raise HTTPException(status_code=404, detail="Service not found")
        await _log_service_save_snapshot(
            service_id=service_id,
            action="manual_prompt_save",
            source="json_fallback",
            success=True,
        )
        return {"message": "Prompt saved"}
    await _log_service_save_snapshot(
        service_id=service_id,
        action="manual_prompt_save",
        source="db",
        success=True,
    )
    return {"message": "Prompt saved"}


@router.delete("/api/config/services/clear-all")
async def clear_all_config_services():
    """Delete all services from database and JSON fallback."""
    json_cleared = config_service.clear_services()
    db_config_service.mark_all_services_deleted()

    ok, db_cleared = await _call_db_config_with_fast_fallback(
        "clear_services",
        db_config_service.clear_services(),
    )
    if ok and db_cleared:
        db_config_service.clear_service_delete_tombstones()
        return {"message": "All services deleted from database and JSON"}

    if json_cleared:
        return {"message": "All services deleted from JSON; DB reconciliation queued"}
    return {"message": "Service clear queued for DB reconciliation"}


@router.delete("/api/config/services/{service_id}")
async def delete_config_service(service_id: str):
    """Delete a service from database."""
    normalized_id = _normalize_identifier(service_id)
    if not normalized_id:
        raise HTTPException(status_code=400, detail="Invalid service_id")

    json_deleted = config_service.delete_service(normalized_id)
    db_config_service.mark_service_deleted(normalized_id)

    ok, db_deleted = await _call_db_config_with_fast_fallback(
        "delete_service",
        db_config_service.delete_service(normalized_id),
    )
    if ok and db_deleted:
        db_config_service.unmark_service_deleted(normalized_id)
        return {"message": "Service deleted from database and JSON"}

    if ok and not db_deleted:
        # DB was reachable and the row was already absent.
        db_config_service.unmark_service_deleted(normalized_id)
        if json_deleted:
            return {"message": "Service deleted from JSON; DB already clean"}
        return {"message": "Service already deleted"}

    if json_deleted:
        return {"message": "Service deleted from JSON; DB reconciliation queued"}
    return {"message": "Service delete queued for DB reconciliation"}


@router.get("/api/config/phases")
async def get_config_phases():
    """Get configured journey phases."""
    ok, db_phases = await _call_db_config_with_fast_fallback(
        "get_journey_phases",
        db_config_service.get_journey_phases(),
    )
    if ok and db_phases:
        return db_phases
    return config_service.get_journey_phases()


@router.put("/api/config/phases")
async def update_config_phases(phases: List[Dict[str, Any]]):
    """Replace journey phases."""
    if not isinstance(phases, list):
        raise HTTPException(status_code=400, detail="Phases payload must be a list")
    ok, db_updated = await _call_db_config_with_fast_fallback(
        "update_journey_phases",
        db_config_service.update_journey_phases(phases),
    )
    if ok and db_updated:
        return {"message": "Journey phases updated in database"}

    if config_service.update_journey_phases(phases):
        return {"message": "Journey phases updated in JSON"}
    raise HTTPException(status_code=500, detail="Failed to update journey phases")


@router.get("/api/config/phases/{phase_id}/services")
async def get_config_phase_services(phase_id: str):
    """Get services mapped to one journey phase."""
    normalized_phase_id = _normalize_phase_identifier(phase_id)
    if not normalized_phase_id:
        raise HTTPException(status_code=400, detail="Invalid phase_id")

    ok, db_services = await _call_db_config_with_fast_fallback(
        "get_services_for_phase",
        db_config_service.get_services(),
    )
    services = db_services if ok and isinstance(db_services, list) else config_service.get_services()

    phase_services = [
        dict(service)
        for service in services
        if _normalize_phase_identifier(service.get("phase_id")) == normalized_phase_id
    ]
    return phase_services


@router.get("/api/config/phases/{phase_id}/prebuilt-services")
async def get_config_phase_prebuilt_services(phase_id: str):
    """Get prebuilt service templates for a journey phase."""
    templates = config_service.get_prebuilt_phase_services(phase_id)
    ok, db_services = await _call_db_config_with_fast_fallback(
        "get_services_for_prebuilt_phase",
        db_config_service.get_services(),
    )
    services = db_services if ok and isinstance(db_services, list) else config_service.get_services()

    existing_ids = {
        _normalize_identifier(service.get("id"))
        for service in services
        if isinstance(service, dict)
    }

    enriched: List[Dict[str, Any]] = []
    for template in templates:
        row = dict(template)
        row["is_installed"] = _normalize_identifier(row.get("id")) in existing_ids
        enriched.append(row)
    return enriched


@router.post("/api/config/phases/service-description/suggest")
async def suggest_phase_service_description(payload: SuggestServiceDescriptionRequest):
    """Suggest a short service description from service name."""
    name = str(payload.name or "").strip()
    phase_id = _normalize_phase_identifier(payload.phase_id or "")
    if not name:
        raise HTTPException(status_code=400, detail="Service name is required")

    user_intent = str(payload.user_intent or "").strip()
    fallback = _fallback_phase_service_description(name, phase_id)
    if not str(settings.openai_api_key or "").strip():
        return {"description": fallback, "source": "fallback"}

    prompt_phase = phase_id or "general"
    if user_intent:
        system_msg = (
            "You write clear, professional service descriptions for a hotel chatbot admin panel. "
            "The admin has described what they expect from this service in plain language. "
            "Rewrite it as a crisp 1-2 sentence description suitable for a service catalog. "
            "Keep it factual and specific â€” no fluff. Return only the description text."
        )
        user_msg = (
            f"Service name: {name}\n"
            f"Journey phase: {prompt_phase}\n"
            f"Admin's expectation: {user_intent}\n\n"
            "Refine this into a clean service description."
        )
        max_tok = 120
    else:
        system_msg = (
            "You write concise admin catalog descriptions for chatbot services. "
            "Return one clear sentence (under 25 words), plain text only. "
            "Be specific to what the service does for hotel guests."
        )
        user_msg = (
            f"Service name: {name}\n"
            f"Journey phase: {prompt_phase}\n"
            "Write a practical description for hotel guest support configuration."
        )
        max_tok = 80
    messages = [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": user_msg},
    ]
    try:
        raw = await llm_client.chat(messages, temperature=0.2, max_tokens=max_tok)
        suggestion = re.sub(r"\s+", " ", str(raw or "").strip()).strip("\"'")
        if not suggestion or "having trouble processing" in suggestion.lower():
            return {"description": fallback, "source": "fallback"}
        if len(suggestion) > 400:
            suggestion = suggestion[:400].rstrip()
        return {"description": suggestion, "source": "llm"}
    except Exception:
        return {"description": fallback, "source": "fallback"}


@router.post("/api/config/phases/ticketing-conditions/suggest")
async def suggest_ticketing_conditions(payload: dict):
    """Suggest or refine ticketing trigger conditions for a service."""
    service_name = str(payload.get("service_name") or "").strip()
    service_description = str(payload.get("service_description") or "").strip()
    current_conditions = str(payload.get("current_conditions") or "").strip()
    if not service_name:
        raise HTTPException(status_code=422, detail="service_name is required")

    if current_conditions:
        system_msg = (
            "You are a hotel chatbot configuration expert. "
            "Refine the given ticketing trigger condition into a clear, precise rule. "
            "1-3 sentences. Be specific about what guest action or data triggers the ticket. "
            "Return only the condition text, nothing else."
        )
        user_msg = (
            f"Service: {service_name}\n"
            f"Description: {service_description}\n"
            f"Current condition to refine: {current_conditions}\n\n"
            "Refine into a clear ticket creation trigger rule."
        )
    else:
        system_msg = (
            "You are a hotel chatbot configuration expert. "
            "Write a clear, specific rule for WHEN the bot should create a backend ticket for a service. "
            "Focus on the key guest confirmation step and required data collected. "
            "1-3 sentences. Return only the condition text, nothing else."
        )
        user_msg = (
            f"Service: {service_name}\n"
            f"Description: {service_description}\n\n"
            "Write a ticket creation trigger condition for this service."
        )
    try:
        raw = await llm_client.chat(
            [{"role": "system", "content": system_msg}, {"role": "user", "content": user_msg}],
            temperature=0.2,
            max_tokens=150,
        )
        conditions = re.sub(r"\s+", " ", str(raw or "").strip()).strip("\"'")
        return {"conditions": conditions or ""}
    except Exception as exc:
        return {"conditions": "", "reason": str(exc)}


@router.get("/api/config/agent-plugins/settings")
async def get_agent_plugin_settings():
    """Get global service-agent plugin settings."""
    return config_service.get_agent_plugin_settings()


@router.put("/api/config/agent-plugins/settings")
async def update_agent_plugin_settings(update: UpdateAgentPluginSettings):
    """Update global service-agent plugin settings."""
    updates = update.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No settings updates provided")
    return config_service.update_agent_plugin_settings(updates)


@router.get("/api/config/agent-plugins")
async def get_agent_plugins(
    active_only: bool = False,
    channel: Optional[str] = None,
    industry: Optional[str] = None,
    service_id: Optional[str] = None,
):
    """List configured service-agent plugins."""
    return config_service.get_agent_plugins(
        active_only=active_only,
        channel=channel,
        industry=industry,
        service_id=service_id,
    )


@router.get("/api/config/agent-plugins/{plugin_id}")
async def get_agent_plugin(plugin_id: str):
    """Get one configured service-agent plugin."""
    plugin = config_service.get_agent_plugin(plugin_id)
    if plugin is None:
        raise HTTPException(status_code=404, detail="Agent plugin not found")
    return plugin


@router.post("/api/config/agent-plugins")
async def add_agent_plugin(plugin: AddAgentPlugin):
    """Add (or upsert) one service-agent plugin."""
    payload = plugin.model_dump(exclude_unset=True)
    if config_service.add_agent_plugin(payload):
        return {"message": "Agent plugin saved"}
    raise HTTPException(status_code=400, detail="Invalid agent plugin payload")


@router.put("/api/config/agent-plugins/{plugin_id}")
async def update_agent_plugin(plugin_id: str, update: UpdateAgentPlugin):
    """Update one service-agent plugin."""
    updates = update.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No plugin updates provided")
    if config_service.update_agent_plugin(plugin_id, updates):
        return {"message": f"Agent plugin {plugin_id} updated"}
    raise HTTPException(status_code=404, detail="Agent plugin not found")


@router.delete("/api/config/agent-plugins/clear-all")
async def clear_agent_plugins():
    """Delete all configured service-agent plugins."""
    if config_service.clear_agent_plugins():
        return {"message": "All agent plugins deleted"}
    raise HTTPException(status_code=500, detail="Failed to clear agent plugins")


@router.delete("/api/config/agent-plugins/{plugin_id}")
async def delete_agent_plugin(plugin_id: str):
    """Delete one service-agent plugin."""
    if config_service.delete_agent_plugin(plugin_id):
        return {"message": f"Agent plugin {plugin_id} deleted"}
    raise HTTPException(status_code=404, detail="Agent plugin not found")


@router.get("/api/config/agent-plugins/{plugin_id}/facts")
async def get_agent_plugin_facts(plugin_id: str, status: Optional[str] = None):
    """List fact entries for one service-agent plugin."""
    plugin = config_service.get_agent_plugin(plugin_id)
    if not plugin:
        raise HTTPException(status_code=404, detail="Agent plugin not found")
    return config_service.get_agent_plugin_facts(plugin_id, status=status)


@router.post("/api/config/agent-plugins/{plugin_id}/facts")
async def add_agent_plugin_fact(plugin_id: str, fact: AgentPluginFactInput):
    """Add one fact entry (pending approval by default) to a plugin."""
    payload = fact.model_dump(exclude_unset=True)
    created = config_service.add_agent_plugin_fact(plugin_id, payload)
    if created:
        return {"message": "Fact added to approval queue", "fact": created}
    raise HTTPException(status_code=400, detail="Invalid fact payload or plugin not found")


@router.put("/api/config/agent-plugins/{plugin_id}/facts/{fact_id}")
async def update_agent_plugin_fact(plugin_id: str, fact_id: str, fact: UpdateAgentPluginFactInput):
    """Update one fact entry on a plugin."""
    updates = fact.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fact updates provided")
    updated = config_service.update_agent_plugin_fact(plugin_id, fact_id, updates)
    if updated:
        return {"message": "Fact updated", "fact": updated}
    raise HTTPException(status_code=404, detail="Fact or plugin not found")


@router.post("/api/config/agent-plugins/{plugin_id}/facts/{fact_id}/approve")
async def approve_agent_plugin_fact(
    plugin_id: str,
    fact_id: str,
    payload: ApproveAgentPluginFactRequest,
):
    """Approve one fact entry so runtime can use it."""
    approved = config_service.approve_agent_plugin_fact(
        plugin_id=plugin_id,
        fact_id=fact_id,
        approved_by=str(payload.approved_by or "staff"),
    )
    if approved:
        return {"message": "Fact approved", "fact": approved}
    raise HTTPException(status_code=404, detail="Fact or plugin not found")


@router.post("/api/config/agent-plugins/{plugin_id}/facts/{fact_id}/reject")
async def reject_agent_plugin_fact(plugin_id: str, fact_id: str):
    """Reject one fact entry so runtime ignores it."""
    rejected = config_service.reject_agent_plugin_fact(plugin_id=plugin_id, fact_id=fact_id)
    if rejected:
        return {"message": "Fact rejected", "fact": rejected}
    raise HTTPException(status_code=404, detail="Fact or plugin not found")


@router.delete("/api/config/agent-plugins/{plugin_id}/facts/{fact_id}")
async def delete_agent_plugin_fact(plugin_id: str, fact_id: str):
    """Delete one fact entry from a plugin."""
    if config_service.delete_agent_plugin_fact(plugin_id=plugin_id, fact_id=fact_id):
        return {"message": "Fact deleted"}
    raise HTTPException(status_code=404, detail="Fact or plugin not found")


@router.get("/api/config/service-kb")
async def get_service_kb_records(
    service_id: Optional[str] = None,
    plugin_id: Optional[str] = None,
    active_only: bool = True,
):
    """List service KB records for a service/plugin."""
    try:
        section = await db_config_service.get_json_section("service_kb", default=None)
        if isinstance(section, dict):
            records = section.get("records", [])
            if isinstance(records, list):
                normalized_service = _normalize_identifier(service_id)
                normalized_plugin = _normalize_identifier(plugin_id)
                filtered: list[dict[str, Any]] = []
                for record in records:
                    if not isinstance(record, dict):
                        continue
                    record_service = _normalize_identifier(record.get("service_id"))
                    record_plugin = _normalize_identifier(record.get("plugin_id"))
                    if normalized_service and record_service != normalized_service:
                        continue
                    if normalized_plugin and record_plugin != normalized_plugin:
                        continue
                    if active_only and not bool(record.get("is_active", True)):
                        continue
                    filtered.append(dict(record))
                if filtered:
                    return filtered
    except Exception:
        pass

    return config_service.get_service_kb_records(
        service_id=service_id,
        plugin_id=plugin_id,
        active_only=active_only,
    )


@router.get("/api/config/service-kb/record")
async def get_service_kb_record(
    service_id: Optional[str] = None,
    plugin_id: Optional[str] = None,
    active_only: bool = True,
):
    """Get one service KB record for a service/plugin."""
    try:
        rows = await get_service_kb_records(
            service_id=service_id,
            plugin_id=plugin_id,
            active_only=active_only,
        )
        if isinstance(rows, list) and rows:
            rows.sort(
                key=lambda item: int(item.get("version") or 0),
                reverse=True,
            )
            return rows[0]
    except Exception:
        pass

    payload = config_service.get_service_kb_record(
        service_id=service_id,
        plugin_id=plugin_id,
        active_only=active_only,
    )
    if not payload:
        raise HTTPException(status_code=404, detail="Service KB record not found")
    return payload


@router.post("/api/config/service-kb/compile")
async def compile_service_kb_records(payload: CompileServiceKBRequest):
    """Compile service knowledge packs from KB + admin config for one/all services."""
    result = config_service.compile_service_kb_records(
        service_id=payload.service_id,
        force=bool(payload.force),
        max_facts_per_service=payload.max_facts_per_service,
        preserve_manual=bool(payload.preserve_manual),
        published_by=str(payload.published_by or "admin"),
    )
    # After compiling facts, also run LLM enrichment
    try:
        enrich_result = await config_service.enrich_service_kb_records(
            service_id=payload.service_id,
            force=bool(payload.force),
            published_by=str(payload.published_by or "admin"),
        )
        result["llm_enrichment"] = enrich_result
    except Exception:
        pass  # Non-blocking
    try:
        cfg = config_service.load_config()
        await db_config_service.save_json_section(
            "service_kb",
            cfg.get("service_kb", {}) if isinstance(cfg, dict) else {},
        )
    except Exception as sync_err:
        result["service_kb_db_sync_warning"] = str(sync_err)
    return result


def _extract_editable_sections(raw: str) -> dict[str, Any]:
    """
    Best-effort parse for wrapped JSON KB payloads:
    - {"editable": {...}}
    - {"data": "<json-string-with-editable>"}
    """
    import json as _json

    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        outer = _json.loads(text)
    except Exception:
        return {}
    if not isinstance(outer, dict):
        return {}

    if isinstance(outer.get("editable"), dict):
        return outer.get("editable") or {}

    data_field = outer.get("data")
    if not isinstance(data_field, str):
        return {}
    try:
        inner = _json.loads(data_field)
    except Exception:
        return {}
    if isinstance(inner, dict) and isinstance(inner.get("editable"), dict):
        return inner.get("editable") or {}
    return inner if isinstance(inner, dict) else {}


def _stringify_kb_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False, indent=2).strip()
        except Exception:
            return str(value).strip()
    return str(value).strip()


def _load_kb_text_for_extraction(max_sources: int = 500) -> str:
    """
    Load full KB text from configured sources for extraction (DB-first).
    """
    kb_docs = config_service.get_full_kb_documents(
        max_sources=max_sources,
        max_source_chars=None,
    )
    blocks: list[str] = []

    for doc in kb_docs:
        raw = str(doc.get("content") or "")
        raw = str(raw or "")
        if not raw.strip():
            continue
        source_name = str(doc.get("source_name") or "kb_source").strip() or "kb_source"

        editable = _extract_editable_sections(raw)
        if editable:
            section_lines: list[str] = [f"=== SOURCE: {source_name} ==="]
            for key, value in editable.items():
                value_text = _stringify_kb_value(value)
                if not value_text:
                    continue
                section_name = str(key or "").strip() or "section"
                section_lines.append(f"=== {section_name} ===")
                section_lines.append(value_text)
            block = "\n".join(section_lines).strip()
            if block:
                blocks.append(block)
            continue

        blocks.append(f"=== SOURCE: {source_name} ===\n{raw.strip()}")

    return "\n\n".join(blocks).strip()


def _candidate_kb_alias_tenants(requested_property: str, limit: int = 5) -> list[str]:
    """
    Return likely tenant aliases for KB fallback when active property has no KB files.
    Conservative scoring: common-prefix only, no fuzzy edit-distance jumps.
    """
    requested = _normalize_tenant(requested_property)
    if not requested:
        return []

    requested_compact = requested.replace("_", "")
    if not requested_compact:
        return []

    try:
        known_codes = sorted(config_service._discover_known_hotel_codes())
    except Exception:
        known_codes = []

    scored: list[tuple[int, int, str]] = []
    for code in known_codes:
        normalized = _normalize_tenant(code)
        if not normalized or normalized == requested:
            continue

        compact = normalized.replace("_", "")
        if not compact:
            continue

        # Accept only strong prefix-family signals.
        if not (
            compact.startswith(requested_compact)
            or requested_compact.startswith(compact)
            or normalized.startswith(f"{requested}_")
        ):
            continue

        common_prefix = 0
        for left, right in zip(requested_compact, compact):
            if left != right:
                break
            common_prefix += 1
        if common_prefix < 4:
            continue

        scored.append((-common_prefix, len(compact), normalized))

    scored.sort()
    return [code for _, _, code in scored[: max(1, int(limit or 5))]]


def _pull_kb_log(msg: str) -> None:
    """Write a timestamped line to logs/pull_from_kb_debug.txt â€” guaranteed to persist even if stdout is suppressed."""
    import datetime as _dt
    try:
        log_path = Path(__file__).resolve().parent.parent.parent / "logs" / "pull_from_kb_debug.txt"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with log_path.open("a", encoding="utf-8") as _f:
            _f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def _extract_verbatim_kb_lines_for_service(
    *,
    kb_text: str,
    service_name: str,
    service_description: str,
    max_chars: int = 0,
) -> str:
    """
    Deterministic fallback extractor:
    return exact KB lines that match service keywords, without rewording.
    """
    raw = str(kb_text or "")
    if not raw.strip():
        return ""

    stopwords = {
        "the", "and", "for", "with", "from", "that", "this", "your", "guest", "guests",
        "service", "services", "request", "support", "help", "allows", "allow",
        "available", "submit", "staff",
    }

    text_for_tokens = f"{service_name} {service_description}".lower()
    tokens = [
        token
        for token in re.findall(r"[a-z0-9][a-z0-9_-]{2,}", text_for_tokens)
        if token not in stopwords and len(token) >= 4
    ]

    # Domain hints for common hotel room-booking intents.
    if any(tok in {"room", "booking", "book", "stay", "checkin", "checkout"} for tok in tokens):
        tokens.extend(
            [
                "room",
                "suite",
                "check in",
                "check-out",
                "check out",
                "occupancy",
                "rate",
                "tariff",
                "king",
                "twin",
                "double",
                "single",
                "premier",
                "deluxe",
                "amenities",
            ]
        )

    token_set = sorted({tok.strip().lower() for tok in tokens if tok.strip()})
    if not token_set:
        return ""

    lines = raw.splitlines()
    picked_indexes: set[int] = set()
    for idx, line in enumerate(lines):
        normalized_line = str(line or "").lower()
        if not normalized_line.strip():
            continue
        if any(tok in normalized_line for tok in token_set):
            picked_indexes.add(idx)
            # include near context line(s) for readability while preserving verbatim source text
            if idx > 0:
                picked_indexes.add(idx - 1)
            if idx + 1 < len(lines):
                picked_indexes.add(idx + 1)

    if not picked_indexes:
        return ""

    ordered = sorted(picked_indexes)
    output_lines: list[str] = []
    previous_idx = -99
    total_chars = 0
    for idx in ordered:
        # keep paragraph breaks between distant matches
        if output_lines and idx - previous_idx > 2:
            output_lines.append("")
        line = lines[idx]
        line_len = len(line) + 1
        if max_chars and max_chars > 0 and total_chars + line_len > max_chars:
            break
        output_lines.append(line)
        total_chars += line_len
        previous_idx = idx

    return "\n".join(output_lines).strip()


def _split_kb_text_for_llm(
    *,
    kb_text: str,
    chunk_chars: int = 18000,
) -> list[str]:
    """
    Split KB text into line-safe chunks to avoid single-request prompt truncation.
    """
    text = str(kb_text or "").strip()
    if not text:
        return []
    chunk_size = max(2000, int(chunk_chars or 18000))
    if len(text) <= chunk_size:
        return [text]

    chunks: list[str] = []
    current_lines: list[str] = []
    current_chars = 0
    for line in text.splitlines():
        line_len = len(line) + 1
        if current_lines and current_chars + line_len > chunk_size:
            chunks.append("\n".join(current_lines).strip())
            current_lines = [line]
            current_chars = line_len
        else:
            current_lines.append(line)
            current_chars += line_len
    if current_lines:
        chunks.append("\n".join(current_lines).strip())
    return [chunk for chunk in chunks if chunk]


async def _load_kb_text_from_db_for_property(
    *,
    hotel_code: str,
) -> str:
    """
    DB fallback for Pull-from-KB when file-based KB sources are empty.
    Returns concatenated text from all KB file records for the property.
    """
    code = config_service.resolve_hotel_code(hotel_code)
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Hotel.id).where(Hotel.code.ilike(code)).limit(1)
            )
            hotel_id = result.scalar_one_or_none()
            if hotel_id is None:
                return ""

            rows_result = await session.execute(
                select(KBFile.stored_name, KBFile.content)
                .where(KBFile.hotel_id == hotel_id)
                .order_by(KBFile.id.asc())
            )
            rows = rows_result.all()
            if not rows:
                return ""

            blocks: list[str] = []
            for stored_name, content in rows:
                body = str(content or "").strip()
                if not body:
                    continue
                source_name = str(stored_name or "kb_file").strip() or "kb_file"
                blocks.append(f"=== SOURCE: {source_name} (db) ===\n{body}")
            return "\n\n".join(blocks).strip()
    except Exception:
        return ""


@router.post("/api/config/service-kb/preview-extract")
async def preview_extract_service_kb(payload: dict, request: Request):
    """
    Pull from KB with chunked extraction so large KB inputs are not silently dropped.
    Extracts verbatim every piece of information relevant to the given service.
    """
    # Outer safety net — guarantee a JSON response even if an unexpected
    # OSError (Windows Errno 22 stdout issue) or similar escapes inner handlers.
    try:
        return await _preview_extract_service_kb_impl(payload, request)
    except HTTPException:
        raise  # Let FastAPI handle HTTP errors normally
    except Exception as _outer_exc:
        import traceback as _outer_tb
        _pull_kb_log(f"OUTER CRASH: {_outer_exc!r}\n{_outer_tb.format_exc()}")
        return {"extracted_knowledge": "", "reason": f"server_error: {_outer_exc}"}


async def _preview_extract_service_kb_impl(payload: dict, request: Request):
    """Inner implementation for preview-extract (separated for safety wrapper)."""
    _pull_kb_log("=== ENDPOINT HIT ===")
    _pull_kb_log(f"[DIAG] payload type={type(payload).__name__} keys={list(payload.keys()) if isinstance(payload, dict) else 'N/A'}")
    payload_data = payload if isinstance(payload, dict) else {}
    service_name = str(payload_data.get("service_name") or "").strip()
    service_description = str(payload_data.get("service_description") or "").strip()
    _pull_kb_log(f"[DIAG] parsed service_name='{service_name}'")
    existing_menu_facts_raw = payload_data.get("existing_menu_facts") or []
    if not isinstance(existing_menu_facts_raw, (list, tuple)):
        existing_menu_facts_raw = [existing_menu_facts_raw]
    existing_menu_facts = [
        str(f).strip()
        for f in existing_menu_facts_raw
        if str(f).strip()
    ]
    _pull_kb_log(f"service_name='{service_name}' desc_chars={len(service_description)}")

    if not service_name:
        raise HTTPException(status_code=422, detail="service_name is required")

    try:
        _pull_kb_log("[DIAG] entering try block — resolving property")
        active_property = _normalize_tenant(
            str(getattr(request.state, "hotel_code", "") or "").strip() or "default"
        )
        requested_property = active_property
        _pull_kb_log(f"[DIAG] active_property='{active_property}' — resolving KB paths")
        source_docs = config_service.get_full_kb_documents(max_sources=500, max_source_chars=None)
        source_paths = [
            str(doc.get("source_path") or doc.get("source_name") or "").strip()
            for doc in source_docs
            if isinstance(doc, dict) and str(doc.get("source_path") or doc.get("source_name") or "").strip()
        ]
        _pull_kb_log(
            f"active_property='{active_property}' source_count={len(source_paths)}"
        )

        kb_text = _load_kb_text_for_extraction(max_sources=500)
        _pull_kb_log(f"_load_kb_text_for_extraction returned {len(kb_text)} chars")
        if not kb_text.strip():
            kb_text = config_service.get_full_kb_text()
            _pull_kb_log(f"fallback get_full_kb_text returned {len(kb_text)} chars")
        if not kb_text.strip():
            kb_text = await _load_kb_text_from_db_for_property(
                hotel_code=active_property,
            )
            _pull_kb_log(f"db fallback KB returned {len(kb_text)} chars")
        if not kb_text.strip():
            alias_candidates = _candidate_kb_alias_tenants(active_property, limit=6)
            if alias_candidates:
                _pull_kb_log(
                    f"[DIAG] no KB in '{active_property}', trying alias candidates={alias_candidates}"
                )
            for alias_property in alias_candidates:
                alias_token = db_config_service.set_hotel_context(alias_property)
                try:
                    alias_source_docs = config_service.get_full_kb_documents(max_sources=500, max_source_chars=None)
                    alias_source_paths = [
                        str(doc.get("source_path") or doc.get("source_name") or "").strip()
                        for doc in alias_source_docs
                        if isinstance(doc, dict) and str(doc.get("source_path") or doc.get("source_name") or "").strip()
                    ]
                    alias_kb_text = _load_kb_text_for_extraction(max_sources=500)
                    if not alias_kb_text.strip():
                        alias_kb_text = config_service.get_full_kb_text()
                    if not alias_kb_text.strip():
                        alias_kb_text = await _load_kb_text_from_db_for_property(
                            hotel_code=alias_property,
                        )
                    if not alias_kb_text.strip():
                        continue
                    source_paths = alias_source_paths
                    kb_text = alias_kb_text
                    active_property = alias_property
                    _pull_kb_log(
                        f"[DIAG] alias fallback picked '{active_property}' source_count={len(source_paths)} kb_chars={len(kb_text)}"
                    )
                    break
                except Exception as alias_exc:
                    _pull_kb_log(f"[DIAG] alias fallback failed for '{alias_property}': {alias_exc}")
                finally:
                    db_config_service.reset_hotel_context(alias_token)
        if not kb_text.strip():
            _pull_kb_log("no_kb_content - returning early")
            return {
                "extracted_knowledge": "",
                "reason": "no_kb_content",
                "active_property": requested_property,
                "resolved_property": active_property,
                "source_count": len(source_paths),
                "hint": "Upload/reindex KB in this property scope and ensure sources exist for this property.",
            }

        chunk_chars = 18000
        try:
            chunk_chars = max(4000, min(int(payload_data.get("chunk_chars") or 18000), 32000))
        except Exception:
            chunk_chars = 18000
        kb_chunks = _split_kb_text_for_llm(kb_text=kb_text, chunk_chars=chunk_chars)
        _pull_kb_log(
            f"kb_chars={len(kb_text)} chunk_count={len(kb_chunks)} chunk_chars={chunk_chars} model={settings.openai_model}"
        )
    except Exception as exc:
        import traceback as _tb
        _pull_kb_log(f"PRELOAD EXCEPTION: {exc}\n{_tb.format_exc()}")
        return {"extracted_knowledge": "", "reason": f"preload_error: {exc}"}

    menu_block = ""
    if existing_menu_facts:
        facts_text = "\n".join(f"- {f}" for f in existing_menu_facts[:300])
        menu_block = (
            f"\n\nThe following facts were already captured from an uploaded menu file. "
            f"Do NOT re-extract or repeat these - only extract KB content NOT already covered here:\n\n"
            f"{facts_text}"
        )

    try:
        _pull_kb_log("[DIAG] attempting property-scoped extraction")
        scoped_result = await config_service.build_property_aware_service_knowledge(
            service_name=service_name,
            service_description=service_description,
            existing_menu_facts=existing_menu_facts,
            max_sources=50,
            max_scope_chars=max(32000, min(len(kb_text) or 90000, 200000)),
            max_properties=50,
        )
        scoped_extracted = str((scoped_result or {}).get("extracted_knowledge") or "").strip()
        _pull_kb_log(
            f"[DIAG] property-scoped result: mode={scoped_result.get('mode')} "
            f"matched_ids={scoped_result.get('matched_property_ids')} "
            f"scope_count={len(scoped_result.get('property_scopes', []))} "
            f"extracted_chars={len(scoped_extracted)}"
        )
        if scoped_extracted:
            property_scopes = scoped_result.get("property_scopes", [])
            property_index = scoped_result.get("property_index", {})
            detected_properties = []
            if isinstance(property_index, dict):
                for row in property_index.get("properties", []) if isinstance(property_index.get("properties", []), list) else []:
                    if not isinstance(row, dict):
                        continue
                    name = str(row.get("name") or "").strip()
                    if name:
                        detected_properties.append(name)
            return {
                "extracted_knowledge": scoped_extracted,
                "reason": "ok_property_scoped",
                "extraction_mode": "property_scoped_llm",
                "chunk_count": 0,
                "property_scope_mode": str(scoped_result.get("mode") or "").strip(),
                "matched_property_ids": scoped_result.get("matched_property_ids", []),
                "property_scope_count": len(property_scopes) if isinstance(property_scopes, list) else 0,
                "expected_property_count": int(property_index.get("expected_property_count") or 0) if isinstance(property_index, dict) else 0,
                "detected_property_count": int(property_index.get("detected_property_count") or len(detected_properties)) if isinstance(property_index, dict) else len(detected_properties),
                "count_mismatch": bool(property_index.get("count_mismatch")) if isinstance(property_index, dict) else False,
                "detected_properties": detected_properties,
                "active_property": requested_property,
                "resolved_property": active_property,
            }
    except Exception as scoped_exc:
        import traceback as _scoped_tb
        _pull_kb_log(f"[DIAG] property-scoped extraction failed, falling back: {scoped_exc}\n{_scoped_tb.format_exc()}")

    system_prompt = (
        f"You are a knowledge extractor for a hotel chatbot.\n\n"
        f"SERVICE NAME: {service_name}\n"
        f"SERVICE DESCRIPTION: {service_description}\n\n"
        f"YOUR TASK:\n"
        f"Scan the ENTIRE knowledge base below - every section - and extract every piece of information "
        f"that belongs to or directly supports '{service_name}'. "
        f"Section headers are hints but the content itself determines relevance.\n\n"
        f"STRICT RULES:\n"
        f"1. Copy text EXACTLY word-for-word. No rephrasing, summarising, or paraphrasing.\n"
        f"2. Do NOT omit any relevant detail - prices, timings, policies, item names must all be copied in full.\n"
        f"3. Do NOT invent or add anything not present in the KB.\n"
        f"4. Extract ALL relevant content from ALL sections. If a line appears multiple times in the KB, keep it as-is; "
        f"do NOT merge, rewrite, normalize, or compress repeated lines.\n"
        f"5. Do NOT include content that is clearly about a completely different service "
        f"(e.g. restaurant dining menus when extracting for spa, parking details when extracting for medical).\n"
        f"6. Preserve original structure: section headers, sub-keys, bullet points, values.\n"
        f"7. Preserve original casing, punctuation, symbols, units, dates, and numbers exactly.\n"
        f"8. If the KB contains absolutely nothing relevant to this service, return exactly: NO_RELEVANT_INFO\n"
        f"{menu_block}"
    )

    try:
        _t0 = time.perf_counter()
        extracted_parts: list[str] = []
        chunks_to_process = kb_chunks or [kb_text]
        total_chunks = len(chunks_to_process)

        for idx, chunk in enumerate(chunks_to_process, start=1):
            user_prompt = (
                f"HOTEL KNOWLEDGE BASE CHUNK {idx}/{total_chunks}:\n\n{chunk}\n\n"
                f"---\n\n"
                f"Scan this KB chunk and extract VERBATIM every piece of information "
                f"relevant to '{service_name}' ({service_description}). "
                f"Copy it exactly as written - every name, price, timing, policy and detail. "
                f"Do not change any word, punctuation, symbol, or number. "
                f"If this chunk has nothing relevant, return exactly: NO_RELEVANT_INFO"
            )
            _pull_kb_log(
                f"calling LLM chunk={idx}/{total_chunks} model={settings.openai_model} "
                f"system_chars={len(system_prompt)} user_chars={len(user_prompt)}"
            )
            result = await llm_client.chat(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.0,
                max_tokens=8192,
                trace_context={"actor": "pull_from_kb", "service_name": service_name, "chunk": idx},
            )
            extracted_chunk = str(result or "").strip()
            _pull_kb_log(f"chunk={idx} raw response preview: {repr(extracted_chunk[:200])}")
            if not extracted_chunk:
                continue
            if extracted_chunk.upper().startswith("NO_RELEVANT_INFO"):
                continue
            extracted_parts.append(extracted_chunk)

        _dur = round((time.perf_counter() - _t0) * 1000)
        extracted = "\n\n---\n\n".join(extracted_parts).strip()
        if not extracted:
            heuristic_fallback = _extract_verbatim_kb_lines_for_service(
                kb_text=kb_text,
                service_name=service_name,
                service_description=service_description,
                max_chars=0,
            )
            if heuristic_fallback:
                extracted = heuristic_fallback
                _pull_kb_log(
                    f"LLM returned no relevant chunks, heuristic fallback used chars={len(extracted)}"
                )

        _pull_kb_log(f"LLM done in {_dur}ms extracted_chars={len(extracted)}")
        try:
            from services.flow_logger import log_pull_from_kb
            log_pull_from_kb(
                service_name=service_name,
                service_description=service_description,
                kb_chars=len(kb_text),
                extracted_chars=len(extracted),
                extraction_mode="chunked_llm",
            )
        except Exception:
            pass

        return {
            "extracted_knowledge": extracted,
            "reason": "ok_llm" if extracted else "no_relevant_info",
            "extraction_mode": "chunked_llm",
            "chunk_count": total_chunks,
        }
    except Exception as exc:
        import traceback as _tb
        _pull_kb_log(f"EXCEPTION: {exc}\n{_tb.format_exc()}")
        return {"extracted_knowledge": "", "reason": str(exc)}


@router.put("/api/config/service-kb/manual-facts")
async def update_service_kb_manual_facts(payload: UpdateServiceKBManualFactsRequest):
    """
    Replace manual override facts for one service knowledge pack.
    Auto-extracted facts are preserved; manual rows are replaced by provided list.
    """
    record = config_service.set_service_kb_manual_facts(
        service_id=payload.service_id,
        plugin_id=payload.plugin_id,
        facts=list(payload.facts or []),
        published_by=str(payload.published_by or "admin"),
    )
    if not record:
        raise HTTPException(status_code=400, detail="Failed to update service KB manual facts")
    try:
        cfg = config_service.load_config()
        await db_config_service.save_json_section(
            "service_kb",
            cfg.get("service_kb", {}) if isinstance(cfg, dict) else {},
        )
    except Exception:
        pass
    return {"message": "Service KB manual facts updated", "record": record}


@router.put("/api/config/service-kb/menu-documents")
async def update_service_kb_menu_documents(payload: UpdateServiceKBMenuDocumentsRequest):
    """
    Attach OCR menu documents to one service KB record.
    These documents are preserved as structured service-scoped menu artifacts.
    """
    record = config_service.set_service_kb_menu_documents(
        service_id=payload.service_id,
        plugin_id=payload.plugin_id,
        documents=list(payload.documents or []),
        published_by=str(payload.published_by or "admin"),
        replace=bool(payload.replace),
    )
    if not record:
        raise HTTPException(status_code=400, detail="Failed to update service KB menu documents")
    try:
        cfg = config_service.load_config()
        await db_config_service.save_json_section(
            "service_kb",
            cfg.get("service_kb", {}) if isinstance(cfg, dict) else {},
        )
    except Exception:
        pass
    return {"message": "Service KB menu documents updated", "record": record}


def _menu_dietary_labels(item: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    veg_value = str(item.get("veg") or "").strip().lower()
    if veg_value == "v":
        labels.append("Vegetarian")
    elif veg_value == "vg":
        labels.append("Vegan")
    elif veg_value:
        labels.append(veg_value.upper())

    non_veg_value = str(item.get("non_veg") or "").strip().lower()
    if non_veg_value in {"non-veg", "non_veg", "nv"}:
        labels.append("Non-Vegetarian")
    elif non_veg_value in {"s", "seafood", "fish"}:
        labels.append("Seafood")
    elif non_veg_value:
        labels.append(non_veg_value.title())
    return labels


def _render_menu_payload_as_text(menu_payload: dict[str, Any]) -> str:
    if not isinstance(menu_payload, dict):
        return ""

    lines: list[str] = []
    menu_name = str(menu_payload.get("menu_name") or "").strip()
    normalized_menu_name = re.sub(r"\s+", " ", menu_name).strip().lower()
    if menu_name:
        lines.append(menu_name)

    other_text: list[str] = []
    seen_other_text: set[str] = set()
    for entry in (menu_payload.get("other_text") or []):
        text = str(entry or "").strip()
        if not text:
            continue
        normalized = re.sub(r"\s+", " ", text).strip().lower()
        if not normalized:
            continue
        if normalized_menu_name and normalized == normalized_menu_name:
            continue
        if normalized in seen_other_text:
            continue
        seen_other_text.add(normalized)
        other_text.append(text)
    if other_text:
        if lines:
            lines.append("")
        lines.extend(other_text)

    items = menu_payload.get("items") or []
    if not isinstance(items, list):
        items = []

    page_values = {
        int(item.get("page"))
        for item in items
        if isinstance(item, dict) and str(item.get("page") or "").strip().isdigit()
    }
    use_page_sections = len(page_values) > 1
    grouped_items: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        section_label = str(item.get("dish_type") or "").strip()
        if not section_label:
            page_no = str(item.get("page") or "").strip()
            if use_page_sections and page_no:
                section_label = f"Page {page_no}"
            else:
                section_label = "Menu Items"
        grouped_items.setdefault(section_label, []).append(item)

    for section_label, section_items in grouped_items.items():
        if lines:
            lines.append("")
        lines.append(section_label)
        for item in section_items:
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            price = str(item.get("price") or "").strip()
            heading = f"{name} - INR {price}" if price else name
            lines.append(heading)

            meta_bits: list[str] = []
            dietary_labels = _menu_dietary_labels(item)
            if dietary_labels:
                meta_bits.append(f"Dietary: {', '.join(dietary_labels)}")
            kcal = str(item.get("kcal") or "").strip()
            if kcal:
                meta_bits.append(f"Calories: {kcal}")
            allergens = item.get("allergens") or []
            if isinstance(allergens, list):
                allergen_list = [str(entry or "").strip() for entry in allergens if str(entry or "").strip()]
                if allergen_list:
                    meta_bits.append(f"Allergens: {', '.join(allergen_list)}")
            if meta_bits:
                lines.append(f"  {' | '.join(meta_bits)}")

            description = str(item.get("description") or "").strip()
            if description:
                lines.append(f"  {description}")

    notes = [
        str(entry or "").strip()
        for entry in (menu_payload.get("notes") or [])
        if str(entry or "").strip()
    ]
    if notes:
        if lines:
            lines.append("")
        lines.append("Notes")
        for entry in notes:
            lines.append(f"- {entry}")

    footer_text = [
        str(entry or "").strip()
        for entry in (menu_payload.get("footer_text") or [])
        if str(entry or "").strip()
    ]
    if footer_text:
        if lines:
            lines.append("")
        lines.append("Footer")
        for entry in footer_text:
            lines.append(f"- {entry}")

    return "\n".join(line.rstrip() for line in lines if line is not None).strip()


def _build_menu_document_payload(
    *,
    file_name: str,
    ocr_result: dict[str, Any],
    formatted_text: str,
    raw_ocr_json: str,
) -> dict[str, Any]:
    summary = ocr_result.get("summary", {})
    if not isinstance(summary, dict):
        summary = {}
    trace = ocr_result.get("trace", {})
    if not isinstance(trace, dict):
        trace = {}
    ocr_payload = ocr_result.get("ocr_raw_output", {})
    if not isinstance(ocr_payload, dict):
        ocr_payload = {}
    fact_lines = ocr_result.get("fact_lines", [])
    if not isinstance(fact_lines, list):
        fact_lines = []

    run_id = re.sub(r"[^a-z0-9_]+", "_", str(trace.get("run_id") or "").strip().lower()).strip("_")
    if not run_id:
        run_id = re.sub(r"[^a-z0-9_]+", "_", str(file_name or "menu_upload").strip().lower()).strip("_")
    if not run_id:
        run_id = f"menu_doc_{uuid4().hex[:8]}"

    return {
        "id": run_id,
        "menu_name": str(summary.get("menu_name") or ocr_payload.get("menu_name") or "").strip() or None,
        "source_file": str(file_name or "").strip() or None,
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "trace": trace,
        "fact_lines": [str(entry or "").strip() for entry in fact_lines if str(entry or "").strip()],
        "ocr_raw_output": ocr_payload,
        "ocr_raw_output_text": raw_ocr_json,
        "formatted_text": str(formatted_text or "").strip(),
        "raw_text": str(ocr_result.get("raw_text") or "").strip(),
    }


@router.get("/api/agent-builder/menu-ocr/status")
async def get_menu_ocr_status():
    """Get standalone menu OCR plugin status."""
    return menu_ocr_plugin_service.get_status()


@router.post("/api/agent-builder/menu-ocr/scan")
async def scan_menu_ocr_for_agent_builder(
    file: UploadFile = File(...),
    service_name: str = Form(default=""),
    max_facts: int = Form(default=100),
):
    """
    Run standalone Menu OCR plugin for one uploaded menu file and return
    one-line fact suggestions.
    """
    safe_name = _safe_filename(file.filename or "menu_upload.pdf")
    try:
        content = await file.read()
    finally:
        await file.close()

    if not content:
        raise HTTPException(status_code=400, detail="Uploaded menu file is empty")

    bounded_max_facts = max(10, min(int(max_facts), 300))
    with tempfile.TemporaryDirectory(prefix="builder_menu_ocr_") as tmp_dir:
        upload_path = Path(tmp_dir) / safe_name
        upload_path.write_bytes(content)
        try:
            ocr_result = menu_ocr_plugin_service.scan_menu(
                upload_path=upload_path,
                menu_name_hint=str(service_name or safe_name),
                max_facts=bounded_max_facts,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    menu_payload = ocr_result.get("ocr_raw_output", {})
    if not isinstance(menu_payload, dict):
        menu_payload = {}

    # Pass OCR structured output through LLM for clean plain-text formatting
    raw_ocr_json = str(ocr_result.get("ocr_raw_output_text") or "").strip()
    if not raw_ocr_json and menu_payload:
        raw_ocr_json = json.dumps(menu_payload, indent=2, ensure_ascii=False)
    seed_formatted_text = _render_menu_payload_as_text(menu_payload)
    formatted_text = seed_formatted_text or raw_ocr_json
    if raw_ocr_json and str(settings.openai_api_key or "").strip():
        menu_name_hint = str(menu_payload.get("menu_name") or service_name or safe_name).strip() or safe_name
        try:
            messages = [
                {
                    "role": "system",
                    "content": (
                        "You are a menu formatter. You will receive raw JSON output from an OCR pipeline "
                        "that extracted a restaurant/hotel menu. Your job is to reformat it into clean, "
                        "readable plain text â€” like a well-laid-out menu a human would read.\n\n"
                        "Rules:\n"
                        "- Do NOT add any information that is not present in the input.\n"
                        "- Do NOT remove any information from the input.\n"
                        "- Organise by category/section if categories are present.\n"
                        "- For each item include: name, description (if any), price (if any), dietary tags (veg/non-veg, if any).\n"
                        "- Use clean formatting: section headings, item names, indented details.\n"
                        "- Output plain text only â€” no markdown, no JSON, no bullet symbols unless they aid readability.\n"
                        "- Preserve all notes, footer text, and allergen info from the input."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Format this OCR menu output into clean plain text:\n\n{raw_ocr_json}",
                },
            ]
            messages = [
                {
                    "role": "system",
                    "content": (
                        "You format OCR-extracted restaurant menu JSON into clean plain text. "
                        "The input is a menu_formatted.json-style payload from a hotel or restaurant menu OCR pipeline.\n\n"
                        "Output rules:\n"
                        "- Output plain text only.\n"
                        "- Keep every factual detail present in the input. Do not invent or infer missing details.\n"
                        "- Organize the menu into clear sections or categories using dish_type when present.\n"
                        "- For each menu item, include the item name, description, price, dietary markers, allergens, calories, and any seafood or non-vegetarian markers when present.\n"
                        "- Preserve menu title, service hours, notes, footer text, tax notes, allergy warnings, and legends when present.\n"
                        "- If a field is missing, omit that field instead of guessing.\n"
                        "- Make the result read like a clean guest-facing menu, not like JSON, logs, or a fact list."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Format this menu_formatted.json payload for the menu '{menu_name_hint}'.\n\n"
                        "Return a clean plain-text menu with section headings and neatly grouped item details.\n\n"
                        f"JSON INPUT:\n{raw_ocr_json}"
                        + (
                            f"\n\nREFERENCE RENDERING:\n{seed_formatted_text}"
                            if seed_formatted_text
                            else ""
                        )
                    ),
                },
            ]
            llm_formatted_text = await llm_client.chat(messages, temperature=0.1, max_tokens=4000)
            formatted_text = str(llm_formatted_text or "").strip() or formatted_text
        except Exception:
            formatted_text = seed_formatted_text or raw_ocr_json

    ocr_result["formatted_text"] = formatted_text
    ocr_result["menu_document"] = _build_menu_document_payload(
        file_name=safe_name,
        ocr_result=ocr_result,
        formatted_text=formatted_text,
        raw_ocr_json=raw_ocr_json,
    )
    return ocr_result


@router.get("/api/agent-builder/menu-ocr/logs")
async def list_menu_ocr_logs(limit: int = 20):
    """List recent OCR scan logs (latest first)."""
    return {"logs": menu_ocr_plugin_service.list_recent_logs(limit=limit)}


@router.get("/api/agent-builder/menu-ocr/logs/{run_id}")
async def get_menu_ocr_log(run_id: str):
    """Get one OCR scan log by trace/run ID."""
    payload = menu_ocr_plugin_service.get_log(run_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="OCR log not found")
    return payload


@router.get("/api/config/faq-bank")
async def get_config_faq_bank():
    """Get admin FAQ bank entries."""
    if not settings.faq_bank_enabled:
        return []
    try:
        return await db_config_service.get_faq_bank()
    except Exception as e:
        _safe_print(f"DB error: {e}")
        return config_service.get_faq_bank()


@router.post("/api/config/faq-bank")
async def add_config_faq_entry(faq: AddFAQEntry):
    """Add a FAQ bank entry."""
    payload = faq.model_dump(exclude_unset=True)
    try:
        if await db_config_service.add_faq_entry(payload):
            return {"message": "FAQ entry added"}
    except Exception as e:
        _safe_print(f"DB error: {e}")

    if config_service.add_faq_entry(payload):
        return {"message": "FAQ entry added to JSON"}
    raise HTTPException(status_code=500, detail="Failed to add FAQ entry")


@router.put("/api/config/faq-bank/{faq_id}")
async def update_config_faq_entry(faq_id: str, update: UpdateFAQEntry):
    """Update a FAQ bank entry."""
    updates = update.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No FAQ updates provided")

    try:
        if await db_config_service.update_faq_entry(faq_id, updates):
            return {"message": f"FAQ entry {faq_id} updated"}
    except Exception as e:
        _safe_print(f"DB error: {e}")

    if config_service.update_faq_entry(faq_id, updates):
        return {"message": f"FAQ entry {faq_id} updated in JSON"}
    raise HTTPException(status_code=404, detail="FAQ entry not found")


@router.delete("/api/config/faq-bank/{faq_id}")
async def delete_config_faq_entry(faq_id: str):
    """Delete a FAQ bank entry."""
    try:
        if await db_config_service.delete_faq_entry(faq_id):
            return {"message": f"FAQ entry {faq_id} deleted"}
    except Exception as e:
        _safe_print(f"DB error: {e}")

    if config_service.delete_faq_entry(faq_id):
        return {"message": f"FAQ entry {faq_id} deleted from JSON"}
    raise HTTPException(status_code=404, detail="FAQ entry not found")


@router.post("/api/config/faq-bank/bulk-upload")
async def bulk_upload_faq_bank(
    file: UploadFile = File(...),
    property_slug: str = Form(""),
    property_id: Optional[int] = Form(None),
):
    """
    Bulk upload FAQ entries from a CSV, JSON, XLSX, or TXT file.

    CSV format (first row must be headers):
        question,answer
        question,answer,description

    JSON format:
        [{"question": "...", "answer": "...", "description": "..."}, ...]

    XLSX format (first row must be headers): same columns as CSV.

    TXT format: each Q/A pair separated by a blank line. The first line is the
    question, the following non-blank line(s) until the next blank line are the
    answer. Lines starting with "Q:" / "A:" are also accepted.

    Returns a summary: { added, skipped, failed, errors }.
    """
    import csv as _csv
    import io as _io
    import json as _json

    if not settings.faq_bank_enabled:
        raise HTTPException(status_code=403, detail="FAQ bank is disabled (FAQ_BANK_ENABLED=false)")

    filename = (file.filename or "").lower().strip()
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    entries: list[dict] = []
    parse_errors: list[str] = []

    # ── JSON branch ────────────────────────────────────────────────────────
    if filename.endswith(".json"):
        try:
            data = _json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}") from exc
        if not isinstance(data, list):
            raise HTTPException(status_code=400, detail="JSON must be a list of objects")
        for i, row in enumerate(data, start=1):
            if not isinstance(row, dict):
                parse_errors.append(f"Row {i}: not an object, skipped")
                continue
            q = str(row.get("question") or "").strip()
            a = str(row.get("answer") or "").strip()
            if not q or not a:
                parse_errors.append(f"Row {i}: missing question or answer, skipped")
                continue
            entries.append({
                "question": q,
                "answer": a,
                "description": str(row.get("description") or "").strip() or None,
                "enabled": bool(row.get("enabled", True)),
            })

    # ── XLSX branch ────────────────────────────────────────────────────────
    elif filename.endswith(".xlsx"):
        try:
            from openpyxl import load_workbook  # type: ignore
        except ImportError as exc:
            raise HTTPException(
                status_code=500,
                detail="openpyxl is not installed on the server; cannot parse .xlsx",
            ) from exc
        try:
            wb = load_workbook(filename=_io.BytesIO(raw), read_only=True, data_only=True)
            ws = wb.active
            if ws is None:
                raise HTTPException(status_code=400, detail="XLSX has no active worksheet")
            rows_iter = ws.iter_rows(values_only=True)
            header_row = next(rows_iter, None)
            if not header_row:
                raise HTTPException(status_code=400, detail="XLSX is empty")
            headers = [str(h or "").strip().lower() for h in header_row]
            if "question" not in headers or "answer" not in headers:
                raise HTTPException(
                    status_code=400,
                    detail=f"XLSX must have 'question' and 'answer' columns. Found: {headers}",
                )
            q_idx = headers.index("question")
            a_idx = headers.index("answer")
            d_idx = headers.index("description") if "description" in headers else -1
            for i, row in enumerate(rows_iter, start=2):
                if row is None:
                    continue
                q = str(row[q_idx] or "").strip() if q_idx < len(row) else ""
                a = str(row[a_idx] or "").strip() if a_idx < len(row) else ""
                if not q or not a:
                    parse_errors.append(f"Row {i}: missing question or answer, skipped")
                    continue
                desc = ""
                if d_idx >= 0 and d_idx < len(row):
                    desc = str(row[d_idx] or "").strip()
                entries.append({
                    "question": q,
                    "answer": a,
                    "description": desc or None,
                    "enabled": True,
                })
            wb.close()
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Failed to parse XLSX: {exc}") from exc

    # ── TXT branch ─────────────────────────────────────────────────────────
    elif filename.endswith(".txt"):
        try:
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = raw.decode("latin-1")
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Failed to decode TXT: {exc}") from exc

        # Each Q/A pair separated by a blank line. The first non-blank line is
        # the question; following non-blank lines are the answer (joined with
        # newlines). "Q:" / "A:" prefixes are stripped if present.
        blocks = [block.strip() for block in text.replace("\r\n", "\n").split("\n\n")]
        for block_idx, block in enumerate(blocks, start=1):
            if not block:
                continue
            lines = [ln.rstrip() for ln in block.split("\n") if ln.strip()]
            if not lines:
                continue
            first_line = lines[0]
            q_line = first_line
            answer_lines: list[str]
            # Explicit "Q:" / "A:" markers
            lower = first_line.lstrip().lower()
            if lower.startswith("q:") or lower.startswith("question:"):
                q_line = first_line.split(":", 1)[1].strip()
                answer_lines = []
                for ln in lines[1:]:
                    sub_lower = ln.lstrip().lower()
                    if sub_lower.startswith("a:") or sub_lower.startswith("answer:"):
                        answer_lines.append(ln.split(":", 1)[1].strip())
                    else:
                        answer_lines.append(ln.strip())
            else:
                answer_lines = [ln.strip() for ln in lines[1:]]
            q = q_line.strip()
            a = "\n".join([ln for ln in answer_lines if ln]).strip()
            if not q or not a:
                parse_errors.append(f"Block {block_idx}: missing question or answer, skipped")
                continue
            entries.append({
                "question": q,
                "answer": a,
                "description": None,
                "enabled": True,
            })

    # ── CSV branch (default) ────────────────────────────────────────────────
    else:
        try:
            # utf-8-sig handles Excel BOM; fall back to latin-1 for Windows CSV exports
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = raw.decode("latin-1")
            reader = _csv.DictReader(_io.StringIO(text))
            if reader.fieldnames is None:
                raise HTTPException(status_code=400, detail="CSV has no header row")
            # Normalise header names: lowercase + strip
            headers = [str(h or "").strip().lower() for h in reader.fieldnames]
            if "question" not in headers or "answer" not in headers:
                raise HTTPException(
                    status_code=400,
                    detail=f"CSV must have 'question' and 'answer' columns. Found: {headers}",
                )
            for i, row in enumerate(reader, start=2):
                q = str(row.get("question") or "").strip()
                a = str(row.get("answer") or "").strip()
                if not q or not a:
                    parse_errors.append(f"Row {i}: missing question or answer, skipped")
                    continue
                entries.append({
                    "question": q,
                    "answer": a,
                    "description": str(row.get("description") or "").strip() or None,
                    "enabled": True,
                })
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Failed to parse CSV: {exc}") from exc

    if not entries:
        raise HTTPException(
            status_code=400,
            detail=f"No valid entries found. Parse issues: {parse_errors or ['empty file or wrong format']}",
        )

    # ── Insert entries ─────────────────────────────────────────────────────
    # Apply the chosen property scope to every uploaded row. Empty slug = the
    # FAQ is COMMON to all properties under this hotel.
    _bulk_property_slug = str(property_slug or "").strip().lower().replace(" ", "_")
    added = 0
    failed: list[str] = []

    for entry in entries:
        payload = {k: v for k, v in entry.items() if v is not None}
        if _bulk_property_slug:
            payload["property_slug"] = _bulk_property_slug
        if property_id is not None:
            payload["property_id"] = property_id
        try:
            inserted = False
            try:
                inserted = await db_config_service.add_faq_entry(payload)
            except Exception as db_exc:
                _safe_print(f"[faq-bulk-upload] DB error: {db_exc}")
            if not inserted:
                inserted = config_service.add_faq_entry(payload)
            if inserted:
                added += 1
            else:
                failed.append(entry["question"][:60])
        except Exception as exc:
            failed.append(f"{entry['question'][:60]} ({exc})")

    return {
        "added": added,
        "skipped": len(parse_errors),
        "failed": len(failed),
        "errors": parse_errors + ([f"Insert failed: {q}" for q in failed] if failed else []),
    }


@router.get("/api/config/tools")
async def get_config_tools():
    """Get admin tools/workflow definitions."""
    try:
        return await db_config_service.get_tools()
    except Exception as e:
        _safe_print(f"DB error: {e}")
        return config_service.get_tools()


@router.post("/api/config/tools")
async def add_config_tool(tool: AddToolConfig):
    """Add a tool/workflow entry."""
    payload = tool.model_dump(exclude_unset=True)
    try:
        if await db_config_service.add_tool(payload):
            return {"message": "Tool added"}
    except Exception as e:
        _safe_print(f"DB error: {e}")

    if config_service.add_tool(payload):
        return {"message": "Tool added to JSON"}
    raise HTTPException(status_code=500, detail="Failed to add tool")


@router.put("/api/config/tools/{tool_id}")
async def update_config_tool(tool_id: str, update: UpdateToolConfig):
    """Update a tool/workflow entry."""
    updates = update.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No tool updates provided")

    try:
        if await db_config_service.update_tool(tool_id, updates):
            return {"message": f"Tool {tool_id} updated"}
    except Exception as e:
        _safe_print(f"DB error: {e}")

    if config_service.update_tool(tool_id, updates):
        return {"message": f"Tool {tool_id} updated in JSON"}
    raise HTTPException(status_code=404, detail="Tool not found")


@router.delete("/api/config/tools/{tool_id}")
async def delete_config_tool(tool_id: str):
    """Delete a tool/workflow entry."""
    try:
        if await db_config_service.delete_tool(tool_id):
            return {"message": f"Tool {tool_id} deleted"}
    except Exception as e:
        _safe_print(f"DB error: {e}")

    if config_service.delete_tool(tool_id):
        return {"message": f"Tool {tool_id} deleted from JSON"}
    raise HTTPException(status_code=404, detail="Tool not found")




@router.get("/api/config/escalation")
async def get_escalation_config():
    """Get escalation settings from database."""
    try:
        return await db_config_service.get_escalation_config()
    except Exception as e:
        _safe_print(f"DB error: {e}")
        return config_service.get_escalation_config()


@router.put("/api/config/escalation")
async def update_escalation_config(update: dict):
    """Update escalation settings in database."""
    try:
        if await db_config_service.update_escalation_config(update):
            return {"message": "Escalation settings updated in database"}
    except Exception as e:
        _safe_print(f"DB error: {e}")

    if config_service.update_escalation_config(update):
        return {"message": "Escalation settings updated in JSON"}
    raise HTTPException(status_code=500, detail="Failed to update")


@router.get("/api/config/templates")
async def list_templates():
    """List available configuration templates."""
    return config_service.list_templates()


@router.post("/api/config/templates/apply")
async def apply_template(data: ApplyTemplate):
    """Apply a template with custom business info."""
    try:
        config = config_service.apply_template(
            data.template_name,
            {
                "id": data.business_id,
                "name": data.business_name,
                "city": data.city,
                "bot_name": data.bot_name,
            }
        )
        # Also save to database
        await db_config_service.save_full_config(config)
        return {"message": f"Template {data.template_name} applied", "config": config}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Template not found")


@router.get("/api/config/export")
async def export_config():
    """Export config as JSON."""
    try:
        config = await db_config_service.get_full_config()
        import json
        return {"config_json": json.dumps(config, indent=2, ensure_ascii=False)}
    except Exception:
        return {"config_json": config_service.export_config()}


@router.post("/api/config/import")
async def import_config(data: ImportConfig):
    """Import config from JSON."""
    import json
    try:
        config = json.loads(data.config_json)
        await db_config_service.save_full_config(config)
        return {"message": "Configuration imported to database"}
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    except Exception as e:
        _safe_print(f"DB error: {e}")
        if config_service.import_config(data.config_json):
            return {"message": "Configuration imported to JSON"}
        raise HTTPException(status_code=400, detail="Import failed")


# ============ Database Status API ============

@router.get("/api/db/status")
async def get_db_status():
    """Get database connection status and table info."""
    from models.database import engine
    from sqlalchemy import text

    try:
        async with engine.connect() as conn:
            # Test connection
            await conn.execute(text("SELECT 1"))

            # Get table counts
            tables = [
                "new_bot_hotels", "new_bot_restaurants", "new_bot_menu_items",
                "new_bot_guests", "new_bot_orders", "new_bot_order_items",
                "new_bot_conversations", "new_bot_messages",
                "new_bot_business_config", "new_bot_capabilities", "new_bot_intents",
                "new_bot_services", "new_bot_kb_files",
            ]

            counts = {}
            for table in tables:
                try:
                    result = await conn.execute(text(f"SELECT COUNT(*) FROM {table}"))
                    counts[table] = result.scalar()
                except:
                    counts[table] = "table not found"

            return {
                "status": "connected",
                "database": "GHN_PROD_BAK",
                "host": "172.16.5.32",
                "tables": counts
            }
    except Exception as e:
        return {
            "status": "disconnected",
            "error": str(e),
            "note": "Make sure OpenVPN is connected"
        }


@router.post("/api/db/sync")
async def sync_json_to_db():
    """Sync JSON config to database."""
    try:
        config = config_service.load_config()
        await db_config_service.save_full_config(config)
        return {"message": "JSON config synced to database"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Sync failed: {e}")


@router.post("/api/db/init")
async def init_database():
    """Initialize/create database tables."""
    from models.database import init_db
    try:
        await init_db()
        return {"message": "Database tables created/verified"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Init failed: {e}")


# ============ Tickets API (new_bot_tickets + local JSON fallback) ============

def _ticket_hotel_clause(hotel_code: str) -> tuple[str, Dict[str, Any]]:
    """Return (WHERE fragment, params) for hotel filtering on tickets."""
    code = str(hotel_code or "").strip().upper()
    if code and code not in ("DEFAULT", ""):
        return "AND t.hotel_code = :hotel_code", {"hotel_code": code}
    return "", {}


@router.get("/api/tickets/stats")
async def get_tickets_stats(
    request: Request,
    date_from: Optional[str] = None,
    date_to:   Optional[str] = None,
):
    """Return ticket counts by status for the stats dashboard.
    Honors date_from / date_to so stat cards reflect the same window as the list.
    """
    from sqlalchemy import text as _text
    from models.database import engine as _engine
    hotel_code = (
        getattr(request.state, "hotel_code", "")
        or request.headers.get("x-hotel-code", "")
        or ""
    ).strip().upper()
    hotel_clause, params = _ticket_hotel_clause(hotel_code)
    date_clause = ""
    if date_from:
        date_clause += " AND DATE(t.created_at) >= :date_from"
        params["date_from"] = date_from
    if date_to:
        date_clause += " AND DATE(t.created_at) <= :date_to"
        params["date_to"] = date_to
    try:
        async with _engine.connect() as conn:
            result = await conn.execute(
                _text(f"""
                    SELECT
                        COUNT(*)                                                         AS total,
                        SUM(CASE WHEN t.status = 'open'        THEN 1 ELSE 0 END)       AS open_count,
                        SUM(CASE WHEN t.status = 'in_progress' THEN 1 ELSE 0 END)       AS in_progress,
                        SUM(CASE WHEN t.status = 'closed'      THEN 1 ELSE 0 END)       AS closed,
                        SUM(CASE WHEN t.status = 'escalated'   THEN 1 ELSE 0 END)       AS escalated,
                        SUM(CASE WHEN t.status = 'cancelled'   THEN 1 ELSE 0 END)       AS cancelled
                    FROM new_bot_tickets t
                    WHERE 1=1 {hotel_clause} {date_clause}
                """),
                params,
            )
            row = result.mappings().fetchone() or {}
            return {
                "total":       int(row.get("total")       or 0),
                "open":        int(row.get("open_count")  or 0),
                "in_progress": int(row.get("in_progress") or 0),
                "closed":      int(row.get("closed")      or 0),
                "escalated":   int(row.get("escalated")   or 0),
                "cancelled":   int(row.get("cancelled")   or 0),
            }
    except Exception as e:
        _safe_print(f"[tickets/stats] DB error: {e}")
        return {"total": 0, "open": 0, "in_progress": 0, "closed": 0, "escalated": 0, "cancelled": 0}


@router.get("/api/tickets")
async def list_tickets(
    request: Request,
    status:     Optional[str] = None,
    priority:   Optional[str] = None,
    phase:      Optional[str] = None,
    department: Optional[str] = None,
    search:     Optional[str] = None,
    date_from:  Optional[str] = None,
    date_to:    Optional[str] = None,
    limit:      int = 200,
    offset:     int = 0,
):
    """List tickets from new_bot_tickets with full filtering.

    Also merges any locally stored tickets (JSON) that haven't been saved to DB yet.
    """
    from sqlalchemy import text as _text
    from models.database import engine as _engine
    hotel_code = (
        getattr(request.state, "hotel_code", "")
        or request.headers.get("x-hotel-code", "")
        or ""
    ).strip().upper()
    hotel_clause, params = _ticket_hotel_clause(hotel_code)

    where_extra = ""
    if status and status.lower() != "all":
        # Multi-select: accept comma-separated list of statuses.
        status_values = [s.strip().lower() for s in str(status).split(",") if s.strip()]
        status_values = [s for s in status_values if s and s != "all"]
        if len(status_values) == 1:
            where_extra += " AND t.status = :status"
            params["status"] = status_values[0]
        elif len(status_values) > 1:
            placeholders = []
            for idx, val in enumerate(status_values):
                key = f"status_{idx}"
                placeholders.append(f":{key}")
                params[key] = val
            where_extra += f" AND t.status IN ({', '.join(placeholders)})"
    if priority and priority.lower() != "all":
        where_extra += " AND t.priority = :priority"
        params["priority"] = priority.lower()
    if phase and phase.lower() != "all":
        where_extra += " AND t.phase = :phase"
        params["phase"] = phase.lower()
    if department and department.lower() != "all":
        where_extra += " AND t.department = :department"
        params["department"] = department
    if search:
        where_extra += (
            " AND (t.guest_name LIKE :search"
            " OR t.room_number LIKE :search"
            " OR t.issue LIKE :search"
            " OR t.description LIKE :search"
            " OR t.ticket_ref LIKE :search"
            " OR t.assignee LIKE :search)"
        )
        params["search"] = f"%{search}%"
    if date_from:
        where_extra += " AND DATE(t.created_at) >= :date_from"
        params["date_from"] = date_from
    if date_to:
        where_extra += " AND DATE(t.created_at) <= :date_to"
        params["date_to"] = date_to

    params["limit"]  = max(1, min(int(limit), 500))
    params["offset"] = max(0, int(offset))

    try:
        async with _engine.connect() as conn:
            rows = await conn.execute(
                _text(f"""
                    SELECT
                        t.id                AS ticket_db_id,
                        t.ticket_ref,
                        t.session_id,
                        t.conversation_uid,
                        t.guest_name,
                        t.guest_id,
                        t.room_number,
                        t.hotel_code,
                        t.property_name,
                        t.source,
                        t.issue,
                        t.description,
                        t.category,
                        t.sub_category,
                        t.phase,
                        t.priority,
                        t.status,
                        t.department,
                        t.assignee,
                        t.is_escalated,
                        t.escalation_stage,
                        t.manager_notes,
                        t.sentiment_score,
                        t.sla_due_at,
                        t.closed_at,
                        t.created_at,
                        t.updated_at
                    FROM new_bot_tickets t
                    WHERE 1=1 {hotel_clause} {where_extra}
                    ORDER BY t.created_at DESC
                    LIMIT :limit OFFSET :offset
                """),
                params,
            )
            tickets = []
            for row in rows.mappings():
                def _s(v): return str(v) if v is not None else None
                def _b(v): return bool(v) if v is not None else False
                tickets.append({
                    "ticket_db_id":    int(row["ticket_db_id"]),
                    "ticket_ref":      _s(row["ticket_ref"]) or str(row["ticket_db_id"]),
                    "session_id":      _s(row["session_id"]),
                    "conversation_uid":_s(row["conversation_uid"]),
                    "guest_name":      _s(row["guest_name"]) or "Guest",
                    "guest_id":        _s(row["guest_id"]),
                    "room_number":     _s(row["room_number"]),
                    "hotel_code":      _s(row["hotel_code"]),
                    "property_name":   _s(row["property_name"]),
                    "source":          _s(row["source"]) or "web",
                    "issue":           _s(row["issue"]) or "",
                    "description":     _s(row["description"]),
                    "category":        _s(row["category"]) or "complaint",
                    "sub_category":    _s(row["sub_category"]),
                    "phase":           _s(row["phase"]),
                    "priority":        _s(row["priority"]) or "medium",
                    "status":          _s(row["status"]) or "open",
                    "department":      _s(row["department"]),
                    "assignee":        _s(row["assignee"]),
                    "is_escalated":    _b(row["is_escalated"]),
                    "escalation_stage":_s(row["escalation_stage"]),
                    "manager_notes":   _s(row["manager_notes"]),
                    "sentiment_score": _s(row["sentiment_score"]),
                    "sla_due_at":      str(row["sla_due_at"])  if row["sla_due_at"]  else None,
                    "closed_at":       str(row["closed_at"])   if row["closed_at"]   else None,
                    "created_at":      str(row["created_at"])  if row["created_at"]  else None,
                    "updated_at":      str(row["updated_at"])  if row["updated_at"]  else None,
                })
            return {"tickets": tickets, "total": len(tickets)}
    except Exception as e:
        _safe_print(f"[tickets] DB error: {e}")
        # Fallback: serve from local JSON store
        import json
        from pathlib import Path as _P
        path = _P("./data/ticketing/local_tickets.json")
        if not path.exists():
            return {"tickets": [], "total": 0, "source": "empty"}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            local_tickets = list(data.get("tickets") or [])
            local_tickets.sort(key=lambda t: t.get("created_at", ""), reverse=True)
            return {"tickets": local_tickets, "total": len(local_tickets), "source": "local_json"}
        except Exception:
            return {"tickets": [], "total": 0, "source": "error"}


class CreateTicketInput(BaseModel):
    guest_name:   str
    description:  str
    property_name: Optional[str] = None
    room_number:  Optional[str] = None
    phase:        Optional[str] = None        # booking / pre_checkin / during_stay / post_checkout
    priority:     Optional[str] = "medium"
    department:   Optional[str] = None        # one of the 4 hardcoded labels
    status:       Optional[str] = "open"
    assignee:     Optional[str] = None


@router.post("/api/tickets")
async def create_manual_ticket(request: Request, data: CreateTicketInput):
    """Staff-created ticket. Writes directly to new_bot_tickets — no Lumira call."""
    from models.database import AsyncSessionLocal, Ticket, Hotel, Property
    from sqlalchemy import select as _select
    hotel_code = (
        getattr(request.state, "hotel_code", "")
        or request.headers.get("x-hotel-code", "")
        or ""
    ).strip().upper()
    try:
        async with AsyncSessionLocal() as db_session:
            hotel_id = None
            if hotel_code and hotel_code != "DEFAULT":
                row = await db_session.execute(
                    _select(Hotel.id).where(Hotel.code == hotel_code)
                )
                hotel_id = row.scalar_one_or_none()

            # Property must be one of THIS hotel's real properties — staff can't
            # file a ticket against an arbitrary place (e.g. "Bangalore"). Only
            # enforced when (a) a property was supplied and (b) the hotel actually
            # has property rows; single-property hotels with none keep free text.
            prop_name = (data.property_name or "").strip()
            if prop_name and hotel_id:
                valid_props = (
                    await db_session.execute(
                        _select(Property.name).where(
                            Property.hotel_id == hotel_id,
                            Property.is_active == True,  # noqa: E712
                        )
                    )
                ).scalars().all()
                valid_set = {str(p or "").strip().lower() for p in valid_props if str(p or "").strip()}
                if valid_set and prop_name.lower() not in valid_set:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Invalid property '{prop_name}' for this hotel. Pick one of its configured properties.",
                    )

            ticket = Ticket(
                hotel_id=hotel_id,
                hotel_code=hotel_code or None,
                source="manual",
                guest_name=data.guest_name.strip() or None,
                room_number=(data.room_number or "").strip() or None,
                property_name=(data.property_name or "").strip() or None,
                description=data.description.strip() or None,
                issue=data.description.strip() or None,
                phase=(data.phase or "").strip().lower() or None,
                priority=(data.priority or "medium").strip().lower(),
                department=(data.department or "").strip() or None,
                status=(data.status or "open").strip().lower(),
                assignee=(data.assignee or "").strip() or None,
                is_escalated=False,
            )
            db_session.add(ticket)
            await db_session.commit()
            await db_session.refresh(ticket)
            new_id = int(ticket.id)
            # Backfill ticket_ref using db id (matches the "TKT-{id}" fallback used by the list endpoint).
            ticket.ticket_ref = f"TKT-{new_id}"
            await db_session.commit()
            return {"ok": True, "ticket_db_id": new_id, "ticket_ref": ticket.ticket_ref}
    except HTTPException:
        # Validation rejections (e.g. invalid property) must surface as-is, not
        # be masked as a generic 500 by the handler below.
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Create failed: {e}")


class UpdateTicketInput(BaseModel):
    status:         Optional[str] = None
    priority:       Optional[str] = None
    assignee:       Optional[str] = None
    department:     Optional[str] = None
    manager_notes:  Optional[str] = None
    is_escalated:   Optional[bool] = None
    description:    Optional[str] = None


@router.put("/api/tickets/{ticket_db_id}")
async def update_ticket(ticket_db_id: int, data: UpdateTicketInput):
    """Update a ticket's status, assignee, notes etc."""
    from models.database import Ticket
    try:
        async with AsyncSessionLocal() as db_session:
            ticket = await db_session.get(Ticket, ticket_db_id)
            if not ticket:
                raise HTTPException(status_code=404, detail="Ticket not found")
            if data.status is not None:
                ticket.status = data.status.lower()
                if data.status.lower() == "closed":
                    from datetime import datetime, timezone
                    ticket.closed_at = datetime.now(timezone.utc)
            if data.priority    is not None: ticket.priority      = data.priority.lower()
            if data.assignee    is not None: ticket.assignee      = data.assignee
            if data.department  is not None: ticket.department    = data.department
            if data.manager_notes is not None: ticket.manager_notes = data.manager_notes
            if data.is_escalated is not None:  ticket.is_escalated  = data.is_escalated
            if data.description  is not None:  ticket.description   = data.description
            await db_session.commit()
            return {"ok": True, "ticket_db_id": ticket_db_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Update failed: {e}")


# ============ Bookings / Guest Management ============


# What a photo shows, as the chat reads it (image_tool_service.GALLERY_CATEGORY_ORDER plus two).
# `facade` and `room` make up the gallery shown when a guest picks a hotel; `room_detail` is a room
# shot kept out of that gallery (it has no cap) but still matched in chat by title; `other` is never
# offered unless a reply names its exact title. A row without a category never reaches the gallery.
CONTENT_IMAGE_CATEGORIES = (
    "facade", "room", "room_detail", "dining", "pool", "spa", "banquet", "lobby", "fitness", "kids", "other",
)


def _normalize_content_image_category(value: Any) -> Optional[str]:
    """'' or None -> None (no category); anything else must be one of CONTENT_IMAGE_CATEGORIES."""
    raw = str(value or "").strip().lower()
    if not raw:
        return None
    if raw not in CONTENT_IMAGE_CATEGORIES:
        raise HTTPException(
            status_code=400,
            detail=f"Category must be one of: {', '.join(CONTENT_IMAGE_CATEGORIES)}",
        )
    return raw


def _content_image_tags(title: str) -> list[str]:
    """Tags from the title, as the importer derives them (only written when a category is set)."""
    from services.image_tool_service import ImageToolService

    return ImageToolService._derive_tags_from_title(str(title or ""))


class CreateContentHotelImageInput(BaseModel):
    title: str
    image_url: str
    source_label: Optional[str] = None
    # None = shared across all properties for this hotel.
    # Integer = scope to that specific sub-property (new_bot_properties.id).
    property_id: Optional[int] = None
    # One of CONTENT_IMAGE_CATEGORIES; None leaves the row uncategorised (as before).
    category: Optional[str] = None


class UpdateContentHotelImageInput(BaseModel):
    title: Optional[str] = None
    image_url: Optional[str] = None
    source_label: Optional[str] = None
    # Reassign property scope. Convention matches the rest of this PUT:
    # absent/None = don't change. To explicitly clear (make shared), pass 0
    # which is treated as a sentinel since real property IDs start at 1.
    property_id: Optional[int] = None
    # Sent = change it ("" clears it); absent = leave it alone.
    category: Optional[str] = None


def _normalize_content_image_title(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _normalize_content_optional_text(value: Any) -> Optional[str]:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    return text or None


def _is_http_image_url(value: str) -> bool:
    url = str(value or "").strip().lower()
    return url.startswith("http://") or url.startswith("https://")


def _image_asset_to_dict(asset: ImageAsset) -> dict:
    # Stored value may be an s3:// pointer (uploaded, private bucket) or a plain
    # http(s) paste URL. resolve_display_url() presigns the former and passes the
    # latter through, so the admin UI always receives a browser-loadable link.
    stored_url = str(asset.image_url or "").strip()
    from services.s3_storage_service import s3_storage_service
    is_uploaded = s3_storage_service.is_managed_value(stored_url)
    display_url = s3_storage_service.resolve_display_url(stored_url) if stored_url else ""
    return {
        "id": int(asset.id),
        "hotel_id": int(asset.hotel_id),
        "property_id": int(asset.property_id) if asset.property_id is not None else None,
        "title": str(asset.title or "").strip(),
        "description": str(asset.description or "").strip(),
        "image_url": display_url,
        "is_uploaded": is_uploaded,
        "category": str(asset.category or "").strip(),
        "tags": asset.tags if isinstance(asset.tags, list) else [],
        "source_label": str(asset.source_label or "").strip(),
        "is_active": bool(asset.is_active),
        "priority": int(asset.priority or 0),
    }


async def _validate_image_property_id(
    *,
    property_id: Optional[int],
    hotel_id: int,
) -> Optional[int]:
    """Ensure the provided property_id (if any) belongs to the active hotel.

    Returns the validated int, or None for "shared" scope. Raises 400 on a
    mismatch so a buggy UI can't accidentally cross-link images to another
    hotel's property.
    """
    if property_id is None:
        return None
    try:
        pid = int(property_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="property_id must be an integer")
    if pid <= 0:
        # 0 / negatives are treated as "no property" — caller wanted shared.
        return None

    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(Property).where(
                    Property.id == pid,
                    Property.hotel_id == hotel_id,
                )
            )
        ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=400,
            detail="property_id does not belong to the active hotel",
        )
    return pid


@router.get("/api/content/hotel-images")
async def list_content_hotel_images(request: Request):
    """
    List image rows from bot_hotel_images_scraper for the currently scoped hotel.

    Hotel scope is resolved from x-hotel-code (via router dependency).

    Optional query params:
      - property_id=<int>     → only images scoped to that sub-property
      - property_id=shared    → only shared images (property_id IS NULL)
      - (absent)              → all images for the hotel
    """
    scoped_code = _normalize_tenant(str(getattr(request.state, "hotel_code", "") or "").strip())
    hotel_id = await db_config_service.get_current_hotel_id()

    raw_property_filter = str(request.query_params.get("property_id") or "").strip().lower()
    property_filter_kind = "all"
    property_filter_id: Optional[int] = None
    if raw_property_filter in {"shared", "null", "none", "0"}:
        property_filter_kind = "shared"
    elif raw_property_filter:
        try:
            property_filter_id = int(raw_property_filter)
            property_filter_kind = "specific"
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="property_id must be an integer or 'shared'")

    async with AsyncSessionLocal() as session:
        hotel_row = (
            await session.execute(select(Hotel).where(Hotel.id == hotel_id))
        ).scalar_one_or_none()
        if hotel_row is None:
            raise HTTPException(status_code=404, detail="Active hotel not found")

        query = (
            select(ImageAsset)
            .where(ImageAsset.hotel_id == hotel_id)
            .order_by(ImageAsset.priority.desc(), ImageAsset.id.asc())
        )
        if property_filter_kind == "shared":
            query = query.where(ImageAsset.property_id.is_(None))
        elif property_filter_kind == "specific" and property_filter_id is not None:
            query = query.where(ImageAsset.property_id == property_filter_id)

        rows = (await session.execute(query)).scalars().all()

    images = [_image_asset_to_dict(row) for row in rows]
    return {
        "hotel": {
            "id": int(hotel_row.id),
            "code": _normalize_tenant(str(hotel_row.code or scoped_code or "default")),
            "name": str(hotel_row.name or hotel_row.code or "").strip(),
            "city": str(hotel_row.city or "").strip(),
        },
        "property_filter": {
            "kind": property_filter_kind,
            "property_id": property_filter_id,
        },
        "images": images,
        "total": len(images),
    }


@router.post("/api/content/hotel-images")
async def create_content_hotel_image(body: CreateContentHotelImageInput, request: Request):
    """Create one image row in bot_hotel_images_scraper for the active hotel scope."""
    normalized_title = _normalize_content_image_title(body.title)
    if not normalized_title:
        raise HTTPException(status_code=400, detail="Title cannot be empty")

    normalized_url = str(body.image_url or "").strip()
    if not _is_http_image_url(normalized_url):
        raise HTTPException(status_code=400, detail="Image URL must start with http:// or https://")

    hotel_id = await db_config_service.get_current_hotel_id()
    source_label = _normalize_content_optional_text(body.source_label)
    category = _normalize_content_image_category(body.category)
    validated_property_id = await _validate_image_property_id(
        property_id=body.property_id,
        hotel_id=hotel_id,
    )

    async with AsyncSessionLocal() as session:
        row = ImageAsset(
            hotel_id=hotel_id,
            property_id=validated_property_id,
            title=normalized_title,
            image_url=normalized_url,
            source_label=source_label,
            category=category,
            tags=_content_image_tags(normalized_title) if category else None,
            is_active=True,
            priority=0,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)

    # Auto-register the smart_image_gallery tool for this hotel so the chat
    # pipeline actually surfaces the uploaded images. Without this, an admin
    # uploads images via Content Manager but `is_enabled_for_hotel` stays
    # False and chat responses never include them.
    try:
        from services.image_tool_service import image_tool_service
        scoped_code = str(getattr(request.state, "hotel_code", "") or "").strip()
        if scoped_code:
            await image_tool_service.ensure_tool_registered(scoped_code, enabled=True)
            # Drop the 10-min candidate cache so the new row appears in chat
            # immediately rather than after the next TTL expiry.
            image_tool_service.invalidate_candidate_cache(scoped_code)
            image_tool_service.invalidate_enabled_cache(scoped_code)
    except Exception as exc:
        _safe_print(f"[content] image tool auto-register failed: {exc}")

    return {"image": _image_asset_to_dict(row)}


@router.post("/api/content/hotel-images/upload")
async def upload_content_hotel_image(
    request: Request,
    file: UploadFile = File(...),
    title: str = Form(...),
    source_label: Optional[str] = Form(default=None),
    property_id: Optional[str] = Form(default=None),
    category: Optional[str] = Form(default=None),
):
    """Upload an image file to S3 and create one image row for the active hotel scope.

    Mirrors create_content_hotel_image but the image_url comes from an S3 upload
    instead of a pasted URL. property_id form field: integer = scope to that
    sub-property; absent / "shared" / "0" = shared across the hotel.
    """
    from services.s3_storage_service import s3_storage_service, S3StorageError

    if not s3_storage_service.is_enabled():
        raise HTTPException(
            status_code=503,
            detail="Image uploads are not configured (S3_BUCKET is empty)",
        )

    normalized_title = _normalize_content_image_title(title)
    if not normalized_title:
        raise HTTPException(status_code=400, detail="Title cannot be empty")

    try:
        content = await file.read()
    finally:
        await file.close()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    hotel_id = await db_config_service.get_current_hotel_id()
    label = _normalize_content_optional_text(source_label)
    normalized_category = _normalize_content_image_category(category)

    raw_property = str(property_id or "").strip().lower()
    property_id_int: Optional[int] = None
    if raw_property and raw_property not in {"shared", "null", "none", "0"}:
        try:
            property_id_int = int(raw_property)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="property_id must be an integer or 'shared'")
    validated_property_id = await _validate_image_property_id(
        property_id=property_id_int,
        hotel_id=hotel_id,
    )

    scoped_code = str(getattr(request.state, "hotel_code", "") or "").strip() or "default"
    try:
        upload = await s3_storage_service.upload_image(
            file_bytes=content,
            content_type=file.content_type or "",
            hotel_code=scoped_code,
            property_id=validated_property_id,
            original_filename=file.filename or "",
        )
    except S3StorageError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    async with AsyncSessionLocal() as session:
        row = ImageAsset(
            hotel_id=hotel_id,
            property_id=validated_property_id,
            title=normalized_title,
            image_url=upload.stored_value,
            source_label=label,
            category=normalized_category,
            tags=_content_image_tags(normalized_title) if normalized_category else None,
            is_active=True,
            priority=0,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)

    # Same auto-register as create: ensure the smart_image_gallery tool is on
    # so the uploaded image actually surfaces in chat, then bust the caches.
    try:
        from services.image_tool_service import image_tool_service
        if scoped_code:
            await image_tool_service.ensure_tool_registered(scoped_code, enabled=True)
            image_tool_service.invalidate_candidate_cache(scoped_code)
            image_tool_service.invalidate_enabled_cache(scoped_code)
    except Exception as exc:
        _safe_print(f"[content] image tool auto-register failed: {exc}")

    return {"image": _image_asset_to_dict(row)}


@router.put("/api/content/hotel-images/{image_id}")
async def update_content_hotel_image(image_id: int, body: UpdateContentHotelImageInput, request: Request):
    """
    Update editable content-manager fields for one image row
    (currently title + image_url) scoped to the active hotel.
    """
    updates: Dict[str, Any] = {}
    if body.title is not None:
        normalized_title = _normalize_content_image_title(body.title)
        if not normalized_title:
            raise HTTPException(status_code=400, detail="Title cannot be empty")
        updates["title"] = normalized_title
    if body.image_url is not None:
        normalized_url = str(body.image_url or "").strip()
        if not _is_http_image_url(normalized_url):
            raise HTTPException(status_code=400, detail="Image URL must start with http:// or https://")
        updates["image_url"] = normalized_url
    if body.source_label is not None:
        updates["source_label"] = _normalize_content_optional_text(body.source_label)
    # Sent = change it ("" clears it), absent = leave it - so a title-only edit behaves as before.
    category_change_requested = "category" in (body.model_fields_set or set())
    if category_change_requested:
        updates["category"] = _normalize_content_image_category(body.category)

    hotel_id = await db_config_service.get_current_hotel_id()

    # property_id reassignment uses the same "explicit" convention as the
    # rest of this PUT: send it to change, omit to leave alone. 0/negative
    # is treated as "clear" (NULL = shared).
    property_reassign_requested = "property_id" in (body.model_fields_set or set())
    if property_reassign_requested:
        updates["property_id"] = await _validate_image_property_id(
            property_id=body.property_id,
            hotel_id=hotel_id,
        )

    if not updates:
        raise HTTPException(status_code=400, detail="At least one editable field is required")

    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(ImageAsset).where(
                    ImageAsset.id == image_id,
                    ImageAsset.hotel_id == hotel_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="Image not found for selected hotel")

        # An uploaded image is stored as an s3:// pointer; the URL the admin UI
        # received is a transient presigned link. Never let it be written back
        # as the new image_url — that would clobber the stable key. Title /
        # label / property scope remain editable.
        from services.s3_storage_service import s3_storage_service
        if "image_url" in updates and s3_storage_service.is_managed_value(row.image_url):
            updates.pop("image_url", None)

        for key, value in updates.items():
            setattr(row, key, value)
        if category_change_requested and updates.get("category"):
            row.tags = _content_image_tags(row.title)
        await session.commit()
        await session.refresh(row)

    try:
        from services.image_tool_service import image_tool_service
        scoped_code = str(getattr(request.state, "hotel_code", "") or "").strip()
        if scoped_code:
            image_tool_service.invalidate_candidate_cache(scoped_code)
    except Exception:
        pass

    return {"image": _image_asset_to_dict(row)}


@router.delete("/api/content/hotel-images/{image_id}")
async def delete_content_hotel_image(image_id: int, request: Request):
    """Delete one image row from bot_hotel_images_scraper for active hotel scope."""
    hotel_id = await db_config_service.get_current_hotel_id()

    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(ImageAsset).where(
                    ImageAsset.id == image_id,
                    ImageAsset.hotel_id == hotel_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="Image not found for selected hotel")

        deleted_image_url = str(row.image_url or "").strip()
        await session.delete(row)
        await session.commit()

    # Remove the backing S3 object, but only for objects we actually uploaded.
    # Paste-URL rows (third-party CDNs) are left untouched by owns_stored().
    try:
        from services.s3_storage_service import s3_storage_service
        if s3_storage_service.owns_stored(deleted_image_url):
            await s3_storage_service.delete_stored(deleted_image_url)
    except Exception as exc:
        _safe_print(f"[content] S3 object cleanup failed: {exc}")

    try:
        from services.image_tool_service import image_tool_service
        scoped_code = str(getattr(request.state, "hotel_code", "") or "").strip()
        if scoped_code:
            image_tool_service.invalidate_candidate_cache(scoped_code)
    except Exception:
        pass

    return {"deleted": True, "image_id": image_id}


# ---------------- Bulk CSV/Excel upload of hotel images ----------------


def _parse_bulk_csv_rows(content: bytes) -> List[List[str]]:
    import csv as _csv
    import io as _io

    text = content.decode("utf-8-sig", errors="replace")
    delimiter = ","
    sample = text[:8192]
    if sample.strip():
        try:
            sniffed = _csv.Sniffer().sniff(sample, delimiters=",;\t|")
            delimiter = sniffed.delimiter or ","
        except Exception:
            delimiter = ","
    reader = _csv.reader(_io.StringIO(text), delimiter=delimiter)
    return [["" if cell is None else str(cell) for cell in row] for row in reader]


def _parse_bulk_xlsx_rows(content: bytes, sheet_name: Optional[str]) -> tuple[List[List[str]], List[str]]:
    import io as _io
    try:
        from openpyxl import load_workbook  # type: ignore
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Excel parsing requires openpyxl, which is not installed: {exc}",
        )

    try:
        wb = load_workbook(_io.BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not read Excel file: {exc}")

    sheet_names = list(wb.sheetnames)
    if not sheet_names:
        wb.close()
        return [], []

    target = sheet_name if sheet_name and sheet_name in sheet_names else sheet_names[0]
    ws = wb[target]
    rows: List[List[str]] = []
    for row in ws.iter_rows(values_only=True):
        rows.append(["" if c is None else str(c) for c in row])
    wb.close()
    return rows, sheet_names


def _looks_like_http_url(value: str) -> bool:
    v = str(value or "").strip().lower()
    return v.startswith("http://") or v.startswith("https://")


def _parse_bulk_image_file(
    content: bytes,
    filename: str,
    sheet_name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Parse a CSV or XLSX file into validated bulk-upload rows.

    Reads only the first 2 columns: column 1 = label, column 2 = image URL.
    Auto-skips a header row (heuristic: row 1 column 2 is not an http(s) URL).
    Trims whitespace, drops empty rows, validates URL prefix and label presence,
    and dedupes within the upload by lowercased label (latest occurrence wins).
    """
    name = (filename or "").strip().lower()
    sheet_names: List[str] = []

    if name.endswith(".csv") or name.endswith(".tsv") or name.endswith(".txt"):
        rows = _parse_bulk_csv_rows(content)
    elif name.endswith(".xlsx"):
        rows, sheet_names = _parse_bulk_xlsx_rows(content, sheet_name)
    elif name.endswith(".xls"):
        raise HTTPException(
            status_code=400,
            detail="Old .xls files are not supported. Please save the file as .xlsx and try again.",
        )
    else:
        raise HTTPException(
            status_code=400,
            detail="Unsupported file type. Please upload a .csv or .xlsx file.",
        )

    valid: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    seen: Dict[str, int] = {}
    header_skipped = False
    # Column 4 is a category ONLY when the header row names it "category" (the template does).
    # Older sheets keep notes past column 3, and those must stay ignored exactly as before.
    category_col: Optional[int] = None

    for idx, raw in enumerate(rows):
        row_no = idx + 1
        # Column layout (1-indexed for humans):
        #   1 → ignored (row number / arbitrary index column)
        #   2 → label
        #   3 → image URL
        #   4 → category, only when the header row says "category"
        # Anything else past column 3 is silently dropped.
        label_raw = raw[1] if len(raw) > 1 else ""
        url_raw = raw[2] if len(raw) > 2 else ""
        label = _normalize_content_image_title(label_raw)
        url = str(url_raw or "").strip()

        if idx == 0 and not _looks_like_http_url(url):
            header_skipped = True
            if len(raw) > 3 and str(raw[3] or "").strip().lower() == "category":
                category_col = 3
            continue

        category: Optional[str] = None
        if category_col is not None:
            category_raw = str((raw[category_col] if len(raw) > category_col else "") or "").strip().lower()
            if category_raw:
                if category_raw not in CONTENT_IMAGE_CATEGORIES:
                    failed.append({
                        "row": row_no,
                        "label": label,
                        "image_url": url,
                        "reason": f"Category '{category_raw}' is not one of: {', '.join(CONTENT_IMAGE_CATEGORIES)}",
                    })
                    continue
                category = category_raw

        if not label and not url:
            continue

        if not label:
            failed.append({
                "row": row_no,
                "label": "",
                "image_url": url,
                "reason": "Label (column 2) is empty",
            })
            continue
        if not url:
            failed.append({
                "row": row_no,
                "label": label,
                "image_url": "",
                "reason": "Image URL (column 3) is empty",
            })
            continue
        if not _is_http_image_url(url):
            failed.append({
                "row": row_no,
                "label": label,
                "image_url": url,
                "reason": "Image URL must start with http:// or https://",
            })
            continue

        key = label.lower()
        if key in seen:
            prev_idx = seen[key]
            prev = valid[prev_idx]
            failed.append({
                "row": prev["row"],
                "label": prev["label"],
                "image_url": prev["image_url"],
                "reason": f"Duplicate label within file — superseded by row {row_no}",
            })
            valid[prev_idx] = {"row": row_no, "label": label, "image_url": url, "category": category}
            continue

        seen[key] = len(valid)
        valid.append({"row": row_no, "label": label, "image_url": url, "category": category})

    return {
        "valid": valid,
        "failed": failed,
        "sheet_names": sheet_names,
        "header_skipped": header_skipped,
    }


@router.post("/api/content/hotel-images/bulk/preview")
async def preview_content_hotel_images_bulk(
    request: Request,
    file: UploadFile = File(...),
    sheet: Optional[str] = Form(default=None),
):
    """Parse an uploaded CSV/XLSX and return a preview without writing to DB."""
    try:
        content = await file.read()
    finally:
        await file.close()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    parsed = _parse_bulk_image_file(content, file.filename or "", sheet)
    valid = parsed["valid"]
    return {
        "filename": file.filename or "",
        "sheet_names": parsed["sheet_names"],
        "selected_sheet": sheet or (parsed["sheet_names"][0] if parsed["sheet_names"] else ""),
        "header_skipped": parsed["header_skipped"],
        "totals": {
            "valid": len(valid),
            "failed": len(parsed["failed"]),
        },
        "preview": valid[:10],
        "failed": parsed["failed"],
    }


@router.post("/api/content/hotel-images/bulk")
async def commit_content_hotel_images_bulk(
    request: Request,
    file: UploadFile = File(...),
    sheet: Optional[str] = Form(default=None),
    property_id: Optional[str] = Form(default=None),
):
    """
    Bulk upsert hotel images from CSV/XLSX into bot_hotel_images_scraper.

    Match key: lowercased, whitespace-collapsed label (column 1) scoped to the
    active hotel + property. Existing rows in the SAME (hotel, property) scope
    are updated (image_url + source_label); new rows are inserted.

    Cross-property labels do NOT collide: "Sea Breeze Suite" can exist for two
    different sub-properties as two distinct rows. Without the scope-aware
    match, the upsert would clobber rows that belong to a different property.

    `property_id`: form field. Integer = scope all uploaded rows to that
    sub-property. Absent / "shared" / 0 = mark them as shared.
    """
    try:
        content = await file.read()
    finally:
        await file.close()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    parsed = _parse_bulk_image_file(content, file.filename or "", sheet)
    valid = parsed["valid"]
    failed = list(parsed["failed"])

    hotel_id = await db_config_service.get_current_hotel_id()
    source_label = _normalize_content_optional_text(file.filename or "")

    raw_property = str(property_id or "").strip().lower()
    property_id_int: Optional[int] = None
    if raw_property and raw_property not in {"shared", "null", "none", "0"}:
        try:
            property_id_int = int(raw_property)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="property_id must be an integer or 'shared'")
    validated_property_id = await _validate_image_property_id(
        property_id=property_id_int,
        hotel_id=hotel_id,
    )

    inserted_count = 0
    updated_count = 0

    async with AsyncSessionLocal() as session:
        existing_query = select(ImageAsset).where(ImageAsset.hotel_id == hotel_id)
        if validated_property_id is None:
            existing_query = existing_query.where(ImageAsset.property_id.is_(None))
        else:
            existing_query = existing_query.where(ImageAsset.property_id == validated_property_id)
        existing_rows = (await session.execute(existing_query)).scalars().all()
        by_label: Dict[str, ImageAsset] = {}
        for asset in existing_rows:
            key = _normalize_content_image_title(asset.title or "").lower()
            if key:
                by_label[key] = asset

        for row in valid:
            key = row["label"].lower()
            existing = by_label.get(key)
            row_category = row.get("category")
            if existing is not None:
                existing.title = row["label"]
                existing.image_url = row["image_url"]
                if source_label:
                    existing.source_label = source_label
                if row_category:
                    existing.category = row_category
                    existing.tags = _content_image_tags(row["label"])
                updated_count += 1
            else:
                new_row = ImageAsset(
                    hotel_id=hotel_id,
                    property_id=validated_property_id,
                    title=row["label"],
                    image_url=row["image_url"],
                    source_label=source_label,
                    category=row_category or None,
                    tags=_content_image_tags(row["label"]) if row_category else None,
                    is_active=True,
                    priority=0,
                )
                session.add(new_row)
                by_label[key] = new_row
                inserted_count += 1

        await session.commit()

    try:
        from services.image_tool_service import image_tool_service
        scoped_code = str(getattr(request.state, "hotel_code", "") or "").strip()
        if scoped_code and (inserted_count > 0 or updated_count > 0):
            await image_tool_service.ensure_tool_registered(scoped_code, enabled=True)
            image_tool_service.invalidate_candidate_cache(scoped_code)
            image_tool_service.invalidate_enabled_cache(scoped_code)
    except Exception as exc:
        _safe_print(f"[content] image tool auto-register failed: {exc}")

    return {
        "filename": file.filename or "",
        "sheet_names": parsed["sheet_names"],
        "header_skipped": parsed["header_skipped"],
        "totals": {
            "inserted": inserted_count,
            "updated": updated_count,
            "failed": len(failed),
        },
        "failed": failed,
    }


@router.get("/api/content/hotel-images/bulk/template")
async def download_content_hotel_images_template():
    """Static sample CSV showing the expected shape.

    Column 1 is reserved for a row number / index (ignored on import — the
    parser keeps its own row counter for error reporting). Column 2 is the
    label, column 3 is the image URL, column 4 the optional category (read only
    when its header says "category"; see CONTENT_IMAGE_CATEGORIES). The first
    three match the spreadsheets admins were already maintaining; the parser
    tolerates an arbitrary column-1 value so existing files don't need to be
    re-keyed.
    """
    sample = (
        "row,label,image_url,category\r\n"
        "1,Hotel exterior,https://example.com/images/exterior.jpg,facade\r\n"
        "2,Deluxe Suite,https://example.com/images/deluxe-suite.jpg,room\r\n"
        "3,Deluxe Suite - bathroom,https://example.com/images/deluxe-suite-bath.jpg,room_detail\r\n"
        "4,Poolside Lounge,https://example.com/images/pool.jpg,pool\r\n"
        "5,Lobby,https://example.com/images/lobby.jpg,lobby\r\n"
    )
    return Response(
        content=sample,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="hotel_images_template.csv"',
        },
    )


class CreateGuestInput(BaseModel):
    phone_number: str
    name: Optional[str] = None


class CreateBookingInput(BaseModel):
    guest_phone: str
    guest_name: Optional[str] = None
    property_name: Optional[str] = None
    room_number: Optional[str] = None
    room_type: Optional[str] = None
    check_in_date: str  # YYYY-MM-DD
    check_out_date: str  # YYYY-MM-DD
    num_guests: Optional[int] = 1
    status: Optional[str] = "reserved"
    source_channel: Optional[str] = "web"
    special_requests: Optional[str] = None


class UpdateBookingInput(BaseModel):
    property_name: Optional[str] = None
    room_number: Optional[str] = None
    room_type: Optional[str] = None
    check_in_date: Optional[str] = None
    check_out_date: Optional[str] = None
    num_guests: Optional[int] = None
    status: Optional[str] = None
    special_requests: Optional[str] = None


def _generate_confirmation_code(hotel_code: str, booking_id: int) -> str:
    """Generate a human-readable confirmation code like KHIL-260406-003."""
    from datetime import date as _date
    today = _date.today()
    prefix = (hotel_code or "HTL").upper()[:6]
    date_part = today.strftime("%y%m%d")
    seq = str(booking_id).zfill(3)
    return f"{prefix}-{date_part}-{seq}"


def _resolve_phase_from_dates(check_in_date, check_out_date) -> str:
    """Derive guest journey phase from booking dates vs today."""
    from datetime import date as _date
    today = _date.today()
    if today < check_in_date:
        return "pre_checkin"
    if check_in_date <= today <= check_out_date:
        return "during_stay"
    return "post_checkout"


def _booking_to_dict(booking: Booking, guest: Guest = None) -> dict:
    """Serialize a Booking ORM object to a JSON-safe dict."""
    phase = _resolve_phase_from_dates(booking.check_in_date, booking.check_out_date)
    result = {
        "booking_id": booking.id,
        "hotel_id": booking.hotel_id,
        "guest_id": booking.guest_id,
        "confirmation_code": booking.confirmation_code,
        "property_name": booking.property_name,
        "room_number": booking.room_number,
        "room_type": booking.room_type,
        "check_in_date": str(booking.check_in_date),
        "check_out_date": str(booking.check_out_date),
        "num_guests": booking.num_guests,
        "status": booking.status,
        "source_channel": booking.source_channel,
        "special_requests": booking.special_requests,
        "phase": phase,
        "created_at": str(booking.created_at) if booking.created_at else None,
    }
    if guest:
        result["guest_name"] = guest.name
        result["guest_phone"] = guest.phone_number
    return result


@router.get("/api/guests")
async def list_guests(hotel_code: str = "DEFAULT"):
    """List all guests for a hotel."""
    async with AsyncSessionLocal() as session:
        hotel_row = (await session.execute(
            select(Hotel).where(Hotel.code == hotel_code)
        )).scalar_one_or_none()
        if not hotel_row:
            raise HTTPException(status_code=404, detail=f"Hotel '{hotel_code}' not found")
        rows = (await session.execute(
            select(Guest).where(Guest.hotel_id == hotel_row.id).order_by(Guest.id.desc())
        )).scalars().all()
        guests = []
        for g in rows:
            guests.append({
                "id": g.id,
                "phone_number": g.phone_number,
                "name": g.name,
                "hotel_id": g.hotel_id,
            })
        return {"guests": guests, "total": len(guests)}


@router.post("/api/guests")
async def create_guest(hotel_code: str, body: CreateGuestInput):
    """Create a guest for a hotel."""
    async with AsyncSessionLocal() as session:
        hotel_row = (await session.execute(
            select(Hotel).where(Hotel.code == hotel_code)
        )).scalar_one_or_none()
        if not hotel_row:
            raise HTTPException(status_code=404, detail=f"Hotel '{hotel_code}' not found")
        # Check if guest already exists
        existing = (await session.execute(
            select(Guest).where(Guest.hotel_id == hotel_row.id, Guest.phone_number == body.phone_number)
        )).scalar_one_or_none()
        if existing:
            if body.name and not existing.name:
                existing.name = body.name
                await session.commit()
            return {"guest_id": existing.id, "phone_number": existing.phone_number, "name": existing.name, "created": False}
        guest = Guest(hotel_id=hotel_row.id, phone_number=body.phone_number, name=body.name)
        session.add(guest)
        await session.commit()
        await session.refresh(guest)
        return {"guest_id": guest.id, "phone_number": guest.phone_number, "name": guest.name, "created": True}


@router.get("/api/bookings")
async def list_bookings(hotel_code: str = "DEFAULT", phase: Optional[str] = None, guest_id: Optional[int] = None):
    """List bookings for a hotel, optionally filtered by phase and/or guest."""
    from datetime import date as _date
    async with AsyncSessionLocal() as session:
        hotel_row = (await session.execute(
            select(Hotel).where(Hotel.code == hotel_code)
        )).scalar_one_or_none()
        if not hotel_row:
            raise HTTPException(status_code=404, detail=f"Hotel '{hotel_code}' not found")

        query = select(Booking, Guest).join(Guest, Booking.guest_id == Guest.id).where(
            Booking.hotel_id == hotel_row.id
        )

        if guest_id:
            query = query.where(Booking.guest_id == guest_id)

        # Phase-based date filtering
        today = _date.today()
        if phase == "pre_checkin":
            query = query.where(Booking.check_in_date > today, Booking.status.in_(["reserved"]))
        elif phase == "during_stay":
            query = query.where(Booking.check_in_date <= today, Booking.check_out_date >= today)
        elif phase == "post_checkout":
            query = query.where(Booking.check_out_date < today)

        query = query.order_by(Booking.id.desc())
        rows = (await session.execute(query)).all()
        bookings = [_booking_to_dict(b, g) for b, g in rows]
        return {"bookings": bookings, "total": len(bookings)}


@router.get("/api/bookings/{booking_id}")
async def get_booking(booking_id: int):
    """Get a single booking by ID."""
    async with AsyncSessionLocal() as session:
        row = (await session.execute(
            select(Booking, Guest).join(Guest, Booking.guest_id == Guest.id).where(Booking.id == booking_id)
        )).one_or_none()
        if not row:
            raise HTTPException(status_code=404, detail="Booking not found")
        booking, guest = row
        return _booking_to_dict(booking, guest)


@router.post("/api/bookings")
async def create_booking(hotel_code: str, body: CreateBookingInput):
    """Create a new booking. Auto-creates guest if needed."""
    from datetime import date as _date
    try:
        ci = _date.fromisoformat(body.check_in_date)
        co = _date.fromisoformat(body.check_out_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Dates must be YYYY-MM-DD format")
    if co <= ci:
        raise HTTPException(status_code=400, detail="check_out_date must be after check_in_date")

    async with AsyncSessionLocal() as session:
        hotel_row = (await session.execute(
            select(Hotel).where(Hotel.code == hotel_code)
        )).scalar_one_or_none()
        if not hotel_row:
            raise HTTPException(status_code=404, detail=f"Hotel '{hotel_code}' not found")

        # Find or create guest
        guest = (await session.execute(
            select(Guest).where(Guest.hotel_id == hotel_row.id, Guest.phone_number == body.guest_phone)
        )).scalar_one_or_none()
        if not guest:
            guest = Guest(hotel_id=hotel_row.id, phone_number=body.guest_phone, name=body.guest_name)
            session.add(guest)
            await session.flush()
        elif body.guest_name and not guest.name:
            guest.name = body.guest_name

        # Create booking with temporary confirmation code
        booking = Booking(
            hotel_id=hotel_row.id,
            guest_id=guest.id,
            confirmation_code="TEMP",
            property_name=body.property_name,
            room_number=body.room_number,
            room_type=body.room_type,
            check_in_date=ci,
            check_out_date=co,
            num_guests=body.num_guests or 1,
            status=body.status or "reserved",
            source_channel=body.source_channel,
            special_requests=body.special_requests,
        )
        session.add(booking)
        await session.flush()

        # Generate real confirmation code using booking ID
        booking.confirmation_code = _generate_confirmation_code(hotel_code, booking.id)
        await session.commit()
        await session.refresh(booking)

        return _booking_to_dict(booking, guest)


@router.put("/api/bookings/{booking_id}")
async def update_booking(booking_id: int, body: UpdateBookingInput):
    """Update an existing booking."""
    from datetime import date as _date
    async with AsyncSessionLocal() as session:
        booking = (await session.execute(
            select(Booking).where(Booking.id == booking_id)
        )).scalar_one_or_none()
        if not booking:
            raise HTTPException(status_code=404, detail="Booking not found")

        if body.property_name is not None:
            booking.property_name = body.property_name
        if body.room_number is not None:
            booking.room_number = body.room_number
        if body.room_type is not None:
            booking.room_type = body.room_type
        if body.check_in_date is not None:
            booking.check_in_date = _date.fromisoformat(body.check_in_date)
        if body.check_out_date is not None:
            booking.check_out_date = _date.fromisoformat(body.check_out_date)
        if body.num_guests is not None:
            booking.num_guests = body.num_guests
        if body.status is not None:
            booking.status = body.status
        if body.special_requests is not None:
            booking.special_requests = body.special_requests

        await session.commit()
        await session.refresh(booking)
        guest = (await session.execute(
            select(Guest).where(Guest.id == booking.guest_id)
        )).scalar_one_or_none()
        return _booking_to_dict(booking, guest)


@router.delete("/api/bookings/{booking_id}")
async def delete_booking(booking_id: int):
    """Delete a booking."""
    async with AsyncSessionLocal() as session:
        booking = (await session.execute(
            select(Booking).where(Booking.id == booking_id)
        )).scalar_one_or_none()
        if not booking:
            raise HTTPException(status_code=404, detail="Booking not found")
        await session.delete(booking)
        await session.commit()
        return {"deleted": True, "booking_id": booking_id}


# ============ Prompt Registry ============

class PromptUpsertInput(BaseModel):
    content: str
    description: Optional[str] = None
    variables: Optional[List[str]] = None
    scope: str = "hotel"  # "hotel" (override) or "industry" (default)


class PromptRegenerateInput(BaseModel):
    instruction: str


_PROMPT_GROUPS = {
    "orchestrator": "Orchestrator",
    "service_writer": "Service Writer",
    "ticketing": "Ticketing",
    "chat": "Chat",
}


def _prompt_group_for(key: str) -> str:
    head = (key.split(".", 1)[0] if "." in key else key).strip().lower()
    return _PROMPT_GROUPS.get(head, "Other")


@router.get("/api/prompts")
async def list_prompts(hotel_code: Optional[str] = None):
    """List every effective prompt for the current/given hotel, grouped by category."""
    from services.prompt_registry_service import prompt_registry
    effective = await prompt_registry.list_effective_for_hotel(hotel_code=hotel_code)
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for item in effective:
        group = _prompt_group_for(item.key)
        groups.setdefault(group, []).append({
            "key": item.key,
            "source": item.source,
            "industry": item.industry,
            "has_override": item.has_override,
            "variables": item.variables,
            "description": item.description,
            "version": item.version,
        })
    return {"groups": groups}


@router.get("/api/prompts/{key}")
async def get_prompt(key: str, hotel_code: Optional[str] = None):
    """Effective + industry default + hotel override (if any) for one key."""
    from services.prompt_registry_service import prompt_registry
    effective = await prompt_registry.list_effective_for_hotel(hotel_code=hotel_code)
    match = next((p for p in effective if p.key == key), None)
    if not match:
        raise HTTPException(status_code=404, detail=f"Prompt {key!r} not found")
    return {
        "key": match.key,
        "content": match.content,
        "source": match.source,
        "industry": match.industry,
        "has_override": match.has_override,
        "industry_default_content": match.industry_default_content,
        "variables": match.variables,
        "description": match.description,
        "version": match.version,
    }


@router.put("/api/prompts/{key}")
async def upsert_prompt(key: str, body: PromptUpsertInput, hotel_code: Optional[str] = None):
    """
    Upsert a prompt.
    scope='hotel' creates/updates a hotel override (requires resolvable hotel context).
    scope='industry' updates the industry default (use sparingly — files are the source of truth).
    """
    from services.prompt_registry_service import prompt_registry
    if body.scope == "industry":
        hotel_id, industry = await prompt_registry._resolve_hotel_and_industry()  # noqa: SLF001
        record = await prompt_registry.upsert(
            key,
            body.content,
            industry=industry,
            variables=body.variables,
            description=body.description,
            updated_by="admin",
        )
    else:
        if hotel_code:
            token = db_config_service.set_hotel_context(hotel_code)
        else:
            token = None
        try:
            hotel_id = await db_config_service.get_current_hotel_id()
        finally:
            if token is not None:
                db_config_service.reset_hotel_context(token)
        if not hotel_id:
            raise HTTPException(status_code=400, detail="No hotel context resolved")
        record = await prompt_registry.upsert(
            key,
            body.content,
            hotel_id=hotel_id,
            variables=body.variables,
            description=body.description,
            updated_by="admin",
        )
    return {
        "key": record.key,
        "version": record.version,
        "hotel_id": record.hotel_id,
        "industry": record.industry,
    }


@router.delete("/api/prompts/{key}")
async def delete_prompt_override(key: str, hotel_code: Optional[str] = None):
    """Drop the hotel override for a prompt — falls back to industry default."""
    from services.prompt_registry_service import prompt_registry
    if hotel_code:
        token = db_config_service.set_hotel_context(hotel_code)
    else:
        token = None
    try:
        hotel_id = await db_config_service.get_current_hotel_id()
    finally:
        if token is not None:
            db_config_service.reset_hotel_context(token)
    if not hotel_id:
        raise HTTPException(status_code=400, detail="No hotel context resolved")
    deleted = await prompt_registry.delete_override(key, hotel_id)
    return {"deleted": bool(deleted)}


@router.post("/api/prompts/{key}/regenerate")
async def regenerate_prompt(key: str, body: PromptRegenerateInput, hotel_code: Optional[str] = None):
    """LLM rewrite of the current prompt according to a plain-English instruction. Not saved."""
    from services.prompt_registry_service import prompt_registry
    instruction = (body.instruction or "").strip()
    if not instruction:
        raise HTTPException(status_code=400, detail="instruction is required")
    try:
        rewrite = await prompt_registry.regenerate_from_instruction(
            key, instruction, hotel_code=hotel_code
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"key": key, "rewrite": rewrite}


@router.post("/api/services/{service_id}/lock-prompt")
async def lock_service_prompt(service_id: int):
    """Lock a service's generated_system_prompt so auto-regen will skip it."""
    from models.database import BotService
    async with AsyncSessionLocal() as session:
        row = (await session.execute(
            select(BotService).where(BotService.id == service_id)
        )).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="Service not found")
        row.generated_system_prompt_override = True
        await session.commit()
    return {"service_id": service_id, "locked": True}


@router.post("/api/services/{service_id}/unlock-prompt")
async def unlock_service_prompt(service_id: int):
    """Unlock and immediately regenerate the system prompt for one service."""
    from models.database import BotService
    async with AsyncSessionLocal() as session:
        row = (await session.execute(
            select(BotService).where(BotService.id == service_id)
        )).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="Service not found")
        row.generated_system_prompt_override = False
        service_payload = {
            "id": row.id,
            "name": row.name,
            "type": row.service_type,
            "description": row.description,
            "ticketing_enabled": row.ticketing_enabled,
            "ticketing_mode": row.ticketing_mode,
            "ticketing_policy": row.ticketing_policy,
            "service_prompt_pack": row.service_prompt_pack,
            "extracted_knowledge": row.extracted_knowledge,
            "phase_id": row.phase_id,
            "hours": row.hours,
            "delivery_zones": row.delivery_zones,
            "cuisine": row.cuisine,
            "form_config": row.form_config,
        }
        regenerated = await generate_service_system_prompt(service_payload)
        if regenerated:
            row.generated_system_prompt = regenerated
        await session.commit()
    return {"service_id": service_id, "locked": False, "regenerated": bool(regenerated)}


@router.post("/api/services/regenerate-all-prompts")
async def regenerate_all_service_prompts(hotel_code: Optional[str] = None):
    """Regenerate generated_system_prompt for every non-locked service of the current hotel."""
    from models.database import BotService
    if hotel_code:
        token = db_config_service.set_hotel_context(hotel_code)
    else:
        token = None
    try:
        hotel_id = await db_config_service.get_current_hotel_id()
    finally:
        if token is not None:
            db_config_service.reset_hotel_context(token)
    if not hotel_id:
        raise HTTPException(status_code=400, detail="No hotel context resolved")
    regenerated = 0
    skipped = 0
    errors: list[str] = []
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(BotService).where(BotService.hotel_id == hotel_id)
        )).scalars().all()
        print(f"[regenerate-all] hotel_id={hotel_id} found {len(rows)} services")
        for row in rows:
            print(f"[regenerate-all]   service={row.name} override={getattr(row, 'generated_system_prompt_override', 'N/A')}")
            if bool(getattr(row, "generated_system_prompt_override", False)):
                skipped += 1
                continue
            try:
                prompt_pack = row.service_prompt_pack or {}
                if not isinstance(prompt_pack, dict):
                    prompt_pack = {}
                payload = {
                    "id": row.service_id,
                    "name": row.name,
                    "type": row.type,
                    "description": row.description,
                    "ticketing_enabled": row.ticketing_enabled,
                    "ticketing_mode": getattr(row, "ticketing_mode", None),
                    "ticketing_policy": getattr(row, "ticketing_policy", None),
                    "service_prompt_pack": prompt_pack,
                    "extracted_knowledge": prompt_pack.get("extracted_knowledge"),
                    "phase_id": getattr(row, "phase_id", None),
                    "hours": prompt_pack.get("hours"),
                    "delivery_zones": prompt_pack.get("delivery_zones"),
                    "cuisine": prompt_pack.get("cuisine"),
                    "form_config": getattr(row, "form_config", None),
                }
                print(f"[regenerate-all]   calling generate_service_system_prompt for {row.name}...")
                new_prompt = await generate_service_system_prompt(payload)
                print(f"[regenerate-all]   result for {row.name}: {len(new_prompt) if new_prompt else 'None'} chars")
                if new_prompt:
                    row.generated_system_prompt = new_prompt
                    regenerated += 1
            except Exception as exc:
                import traceback
                print(f"[regenerate-all]   ERROR for {row.name}: {exc}")
                traceback.print_exc()
                errors.append(f"{row.name}: {exc}")
        await session.commit()
    result: dict = {"regenerated": regenerated, "skipped_locked": skipped}
    if errors:
        result["errors"] = errors
    return result


# ============ Chat History (new_bot_* tables) ============


def _chat_hotel_clause(hotel_code: str) -> tuple[str, Dict[str, Any]]:
    """Return (WHERE clause fragment, params) for hotel filtering."""
    code = str(hotel_code or "").strip().upper()
    if code and code not in ("DEFAULT", ""):
        return "AND h.code = :hotel_code", {"hotel_code": code}
    return "", {}


@router.get("/api/engage-sessions/stats")
async def get_chat_history_stats(request: Request):
    """
    Quick counts for the stats bar.
    Active = last message within 2 hours (time-based, reliable regardless of c.state field).
    """
    from sqlalchemy import text as _text
    from models.database import engine as _engine

    hotel_code = str(
        getattr(request.state, "hotel_code", "")
        or request.headers.get("x-hotel-code", "")
        or ""
    ).strip().upper()

    hotel_clause, params = _chat_hotel_clause(hotel_code)

    try:
        async with _engine.connect() as conn:
            r = await conn.execute(
                _text(f"""
                    SELECT
                        COUNT(DISTINCT c.id) AS total,
                        SUM(CASE
                            WHEN COALESCE(last_msg.last_ts, c.created_at) >= DATE_SUB(NOW(), INTERVAL 2 HOUR)
                            THEN 1 ELSE 0
                        END) AS active,
                        SUM(CASE WHEN DATE(c.created_at) = CURDATE() THEN 1 ELSE 0 END) AS today
                    FROM new_bot_conversations c
                    LEFT JOIN new_bot_hotels h ON h.id = c.hotel_id
                    LEFT JOIN (
                        SELECT conversation_id, MAX(created_at) AS last_ts
                        FROM new_bot_messages
                        GROUP BY conversation_id
                    ) last_msg ON last_msg.conversation_id = c.id
                    WHERE 1=1 {hotel_clause}
                """),
                params,
            )
            row = r.mappings().first()
            return {
                "total":  int(row["total"]  or 0),
                "active": int(row["active"] or 0),
                "today":  int(row["today"]  or 0),
            }
    except Exception as exc:
        return {"total": 0, "active": 0, "today": 0, "error": str(exc)}


@router.get("/api/engage-sessions")
async def list_engage_sessions(
    request: Request,
    limit: int = 100,
    offset: int = 0,
    search: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    state: Optional[str] = None,
    channel: Optional[str] = None,
):
    """
    List chat sessions from new_bot_conversations joined with
    new_bot_messages / new_bot_guests / new_bot_hotels.
    One row per conversation (session_id is unique), newest first.
    """
    from sqlalchemy import text as _text
    from models.database import engine as _engine

    hotel_code = str(
        getattr(request.state, "hotel_code", "")
        or request.headers.get("x-hotel-code", "")
        or ""
    ).strip().upper()

    hotel_clause, params = _chat_hotel_clause(hotel_code)

    where_extra = ""
    if search:
        where_extra += (
            " AND (g.name LIKE :search"
            " OR g.phone_number LIKE :search"
            " OR ns.guest_name LIKE :search"
            " OR ns.guest_phone LIKE :search"
            " OR ns.guest_email LIKE :search"
            " OR c.session_id LIKE :search)"
        )
        params["search"] = f"%{search}%"

    if date_from:
        where_extra += " AND c.created_at >= :date_from"
        params["date_from"] = date_from

    if date_to:
        where_extra += " AND c.created_at <= :date_to"
        params["date_to"] = date_to + "T23:59:59"

    if channel and channel.lower() not in ("all", ""):
        where_extra += " AND LOWER(c.channel) = :channel"
        params["channel"] = channel.lower()

    # Time-based active/closed filter using HAVING on the aggregated last_activity.
    # This is the permanent fix — decoupled from c.state which may not be reliably updated.
    # Active  = last message within 2 hours.
    # Closed  = last message older than 2 hours (or no messages at all).
    having_clause = ""
    if state == "active":
        having_clause = "HAVING COALESCE(MAX(m.created_at), c.created_at) >= DATE_SUB(NOW(), INTERVAL 2 HOUR)"
    elif state == "closed":
        having_clause = "HAVING COALESCE(MAX(m.created_at), c.created_at) < DATE_SUB(NOW(), INTERVAL 2 HOUR)"

    params["limit"]  = max(1, min(int(limit), 300))
    params["offset"] = max(0, int(offset))

    try:
        async with _engine.connect() as conn:
            result = await conn.execute(
                _text(f"""
                    SELECT
                        c.id                                                    AS conversation_id,
                        -- One row PER SESSION (session-wise history page). The widget
                        -- threads by conversation_id, but the KePSLA Chat History page
                        -- shows ONE entry per stable visitor session with the FULL
                        -- transcript (per product decision). conversation_uid is left
                        -- blank so the transcript endpoint returns all session messages.
                        ''                                                      AS conversation_uid,
                        c.session_id,
                        c.state,
                        c.channel,
                        c.created_at                                            AS started_at,
                        COALESCE(MAX(m.created_at), c.created_at)              AS last_activity,
                        COUNT(m.id)                                             AS message_count,
                        SUM(CASE WHEN m.role = 'user' THEN 1 ELSE 0 END)      AS user_turns,
                        SUBSTRING_INDEX(GROUP_CONCAT(
                            CASE WHEN m.role = 'user' THEN m.content END
                            ORDER BY m.id DESC SEPARATOR '\x01'
                        ), '\x01', 1)                                          AS last_user_message,
                        SUBSTRING_INDEX(GROUP_CONCAT(
                            CASE WHEN m.role = 'assistant' THEN m.content END
                            ORDER BY m.id DESC SEPARATOR '\x01'
                        ), '\x01', 1)                                          AS last_bot_message,
                        COALESCE(NULLIF(g.name, ''), ns.guest_name)             AS guest_name,
                        COALESCE(NULLIF(g.phone_number, ''), ns.guest_phone)    AS guest_phone,
                        ns.guest_email                                          AS guest_email,
                        ns.location                                             AS guest_location,
                        g.room_number                                           AS room_number,
                        g.check_in_date,
                        g.check_out_date,
                        g.property_name                                         AS property_name,
                        h.name                                                  AS hotel_name,
                        h.code                                                  AS hotel_code_val
                    FROM new_bot_conversations c
                    LEFT JOIN new_bot_messages m ON m.conversation_id = c.id
                    LEFT JOIN new_bot_guests   g ON g.id = c.guest_id
                    LEFT JOIN new_bot_hotels   h ON h.id = c.hotel_id
                    LEFT JOIN new_bot_sessions ns ON ns.session_id = c.session_id
                    WHERE 1=1 {hotel_clause} {where_extra}
                    GROUP BY
                        c.id,
                        c.session_id, c.state, c.channel, c.created_at,
                        g.name, g.phone_number, g.room_number,
                        g.check_in_date, g.check_out_date, g.property_name,
                        h.name, h.code,
                        ns.guest_name, ns.guest_phone, ns.guest_email, ns.location
                    {having_clause}
                    ORDER BY last_activity DESC
                    LIMIT :limit OFFSET :offset
                """),
                params,
            )
            rows = result.mappings().all()

            from datetime import datetime, timezone, timedelta
            _two_hours_ago = datetime.now(timezone.utc) - timedelta(hours=2)

            # Live-presence sources (in-memory, this worker): a session is "online"
            # only while the guest's widget WebSocket is actually connected, or a
            # human agent is actively handling it. This decouples the green dot from
            # the 2-hour recency heuristic so cleared/closed/left sessions drop to
            # offline at once. Best-effort: any failure falls back to the time rule.
            try:
                from api.routes.websocket_chat import manager as _ws_manager
            except Exception:
                _ws_manager = None
            try:
                from services.agent_room_service import agent_room_service as _arm_live
            except Exception:
                _arm_live = None

            def _session_is_live(sid: str) -> bool:
                if not sid:
                    return False
                try:
                    if _ws_manager is not None and _ws_manager.is_connected(sid):
                        return True
                    if _arm_live is not None and _arm_live.has_active_agent(sid):
                        return True
                except Exception:
                    pass
                return False

            sessions = []
            # Rows are ordered by last_activity DESC, so the FIRST thread we see for
            # a given session_id is its most-recent one — only that thread can be
            # "live" (older threads of the same visitor are finished).
            _live_thread_seen: set[str] = set()
            for row in rows:
                last_msg = str(row["last_user_message"] or row["last_bot_message"] or "")
                sid = str(row["session_id"] or "")
                # Recency (2-hour rule) is kept for the All/Active/Closed tab buckets.
                last_ts_raw = row["last_activity"]
                try:
                    if isinstance(last_ts_raw, str):
                        last_ts = datetime.fromisoformat(last_ts_raw.replace("Z", "+00:00"))
                    else:
                        last_ts = last_ts_raw
                    if last_ts.tzinfo is None:
                        last_ts = last_ts.replace(tzinfo=timezone.utc)
                    is_recent = last_ts >= _two_hours_ago
                except Exception:
                    is_recent = False

                # "online now" = live WebSocket / active agent — but only for the
                # newest thread of the session (the one the guest is actually on).
                is_live = _session_is_live(sid) and (sid not in _live_thread_seen)
                if sid:
                    _live_thread_seen.add(sid)

                sessions.append({
                    "conversation_id":   int(row["conversation_id"]),
                    "conversation_uid":  str(row["conversation_uid"] or ""),
                    "session_id":        sid,
                    "state":             str(row["state"] or "idle"),
                    "is_active":         is_live,
                    "is_live":           is_live,
                    "is_recent":         is_recent,
                    "channel":           str(row["channel"] or "web"),
                    "started_at":        str(row["started_at"] or ""),
                    "last_activity":     str(row["last_activity"] or ""),
                    "message_count":     int(row["message_count"] or 0),
                    "user_turns":        int(row["user_turns"] or 0),
                    "last_message":      last_msg[:200],
                    "last_user_message": str(row["last_user_message"] or "")[:200],
                    "last_bot_message":  str(row["last_bot_message"] or "")[:200],
                    "guest_name":        str(row["guest_name"] or ""),
                    "guest_phone":       str(row["guest_phone"] or ""),
                    "guest_email":       str(row["guest_email"] or ""),
                    "guest_location":    str(row["guest_location"] or ""),
                    "room_number":       str(row["room_number"] or ""),
                    "check_in_date":     str(row["check_in_date"] or ""),
                    "check_out_date":    str(row["check_out_date"] or ""),
                    "property_name":     str(row["property_name"] or ""),
                    "hotel_name":        str(row["hotel_name"] or ""),
                    "hotel_code":        str(row["hotel_code_val"] or ""),
                })

            return {
                "sessions": sessions,
                "total":    len(sessions),
                "offset":   params["offset"],
            }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to load chat sessions: {exc}")


@router.get("/api/engage-sessions/{session_id:path}")
async def get_engage_session_messages(session_id: str, conversation_uid: str = ""):
    """
    Return messages for one chat THREAD from new_bot_messages, ordered
    chronologically. Also returns conversation metadata.

    conversation_uid (optional) scopes the transcript to a single rotating
    thread (dustbin / 30-min idle start a new one). "legacy:<id>" selects the
    pre-uid messages (NULL conversation_uid). Omitted → all messages (back-compat).
    """
    from sqlalchemy import text as _text
    from models.database import engine as _engine

    session_id = str(session_id or "").strip()
    conversation_uid = str(conversation_uid or "").strip()
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")

    # Per-thread scoping clause for the messages query.
    _uid_clause = ""
    _uid_params: dict[str, Any] = {}
    if conversation_uid:
        if conversation_uid.startswith("legacy:"):
            _uid_clause = " AND m.conversation_uid IS NULL"
        else:
            _uid_clause = " AND m.conversation_uid = :cuid"
            _uid_params["cuid"] = conversation_uid

    try:
        async with _engine.connect() as conn:
            # Conversation metadata
            conv_r = await conn.execute(
                _text("""
                    SELECT c.id, c.state, c.channel, c.created_at,
                           COALESCE(NULLIF(g.name, ''), ns.guest_name)          AS guest_name,
                           COALESCE(NULLIF(g.phone_number, ''), ns.guest_phone) AS guest_phone,
                           ns.guest_email AS guest_email, ns.location AS guest_location,
                           g.room_number, g.check_in_date, g.check_out_date,
                           g.property_name, h.name AS hotel_name, h.code AS hotel_code
                    FROM new_bot_conversations c
                    LEFT JOIN new_bot_guests g ON g.id = c.guest_id
                    LEFT JOIN new_bot_hotels h ON h.id = c.hotel_id
                    LEFT JOIN new_bot_sessions ns ON ns.session_id = c.session_id
                    WHERE c.session_id = :sid
                    LIMIT 1
                """),
                {"sid": session_id},
            )
            conv = (conv_r.mappings().first() or {})

            # Messages
            # Detect the optional display_payload column so this endpoint works
            # whether or not the migration (tools/migration_add_display_payload.sql)
            # has run yet — never 500 just because the column is missing.
            try:
                _col_r = await conn.execute(_text(
                    "SELECT COUNT(*) FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'new_bot_messages' "
                    "AND COLUMN_NAME = 'display_payload'"
                ))
                _has_display_col = bool(_col_r.scalar())
            except Exception:
                _has_display_col = False
            _dp_select = ", m.display_payload" if _has_display_col else ""

            msg_r = await conn.execute(
                _text(f"""
                    SELECT m.id, m.role, m.content, m.intent,
                           m.confidence, m.created_at, m.channel{_dp_select}
                    FROM new_bot_conversations c
                    JOIN new_bot_messages m ON m.conversation_id = c.id
                    WHERE c.session_id = :sid{_uid_clause}
                    -- Order by insertion id, which is the TRUE conversation order:
                    -- every turn appends the user message then the assistant reply
                    -- (chat_service adds the user msg at the start of the turn, the
                    -- assistant at the end), so ids are monotonic with the real
                    -- sequence. created_at is NOT reliable for this — a turn can take
                    -- longer than a minute (so a message's stored timestamp skews past
                    -- later turns) and the chip/prefetch + deferred-persist paths can
                    -- stamp the bot reply at or before its own question. id has none of
                    -- those problems and is unique, so no secondary sort is needed.
                    ORDER BY m.id ASC
                """),
                {"sid": session_id, **_uid_params},
            )
            rows = msg_r.mappings().all()

            # Message timestamps are stored UTC-naive. Emit them as UTC-aware ISO
            # ("...+00:00") so the browser converts to the viewer's local time
            # (e.g. IST) — otherwise a naive string is parsed as local and the
            # thread shows times ~5.5h off versus what the guest actually saw.
            from datetime import timezone as _tz_msg

            def _iso_utc(value) -> str:
                if value is None:
                    return ""
                if hasattr(value, "isoformat"):
                    dt = value if value.tzinfo else value.replace(tzinfo=_tz_msg.utc)
                    return dt.isoformat()
                s = str(value).strip()
                if not s:
                    return ""
                # naive "YYYY-MM-DD HH:MM:SS" → treat as UTC
                if "T" not in s and " " in s:
                    s = s.replace(" ", "T")
                return s if (s.endswith("Z") or "+" in s[10:]) else f"{s}+00:00"

            import json as _json_msg

            messages = []
            for row in rows:
                # Rich display the guest actually saw (NULL on older rows → the UI
                # falls back to plain `message` text, so this stays compatible).
                _dp_raw = row.get("display_payload")
                _dp: dict = {}
                if _dp_raw:
                    try:
                        _dp = _dp_raw if isinstance(_dp_raw, dict) else _json_msg.loads(_dp_raw)
                        if not isinstance(_dp, dict):
                            _dp = {}
                    except Exception:
                        _dp = {}
                messages.append({
                    "id":         int(row["id"]),
                    "session_id": session_id,
                    "sender":     "USER" if str(row["role"] or "") == "user" else "BOT",
                    "role":       str(row["role"] or ""),
                    "message":    str(row["content"] or ""),
                    "display_message": str(_dp.get("display_message") or ""),
                    "images":     _dp.get("images") if isinstance(_dp.get("images"), list) else [],
                    "form_fields": _dp.get("form_fields") if isinstance(_dp.get("form_fields"), list) else [],
                    "form_submission": _dp.get("form_submission") if isinstance(_dp.get("form_submission"), dict) else None,
                    "intent":     str(row["intent"] or ""),
                    "confidence": float(row["confidence"] or 0),
                    "timestamp":  _iso_utc(row["created_at"]),
                    "channel":    str(row["channel"] or ""),
                })

            return {
                "session_id":   session_id,
                "conversation": {
                    "state":        str(conv.get("state") or "idle"),
                    "channel":      str(conv.get("channel") or "web"),
                    "started_at":   str(conv.get("created_at") or ""),
                    "guest_name":   str(conv.get("guest_name") or ""),
                    "guest_phone":  str(conv.get("guest_phone") or ""),
                    "guest_email":  str(conv.get("guest_email") or ""),
                    "guest_location": str(conv.get("guest_location") or ""),
                    "room_number":  str(conv.get("room_number") or ""),
                    "check_in_date":  str(conv.get("check_in_date") or ""),
                    "check_out_date": str(conv.get("check_out_date") or ""),
                    "property_name":  str(conv.get("property_name") or ""),
                    "hotel_name":   str(conv.get("hotel_name") or ""),
                    "hotel_code":   str(conv.get("hotel_code") or ""),
                },
                "messages": messages,
                "count":    len(messages),
            }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to load session messages: {exc}")


# ============ Admin UI ============

_VITE_HOP_BY_HOP = frozenset({"connection", "keep-alive", "transfer-encoding", "upgrade", "content-length"})


def _looks_like_admin_asset_request(full_path: str) -> bool:
    """Detect static/module asset paths so missing files don't fall back to index.html."""
    path = str(full_path or "").strip().lstrip("/")
    if not path:
        return False
    if path.startswith(("assets/", "images/", "node_modules/", "@vite/", "@react-refresh")):
        return True
    leaf = Path(path).name
    return "." in leaf


async def _proxy_vite_request(request: Request, admin_path: str):
    """Forward request to Vite dev server when ADMIN_DEV_URL is set; returns None otherwise."""
    import os as _os
    import httpx as _httpx
    from fastapi.responses import Response as _Response

    vite_base = _os.environ.get("ADMIN_DEV_URL", "").strip()
    if not vite_base:
        return None

    path = (admin_path or "").lstrip("/")
    target = f"{vite_base.rstrip('/')}/admin/{path}" if path else f"{vite_base.rstrip('/')}/admin/"
    query = str(request.url.query or "").strip()
    if query:
        target = f"{target}?{query}"

    headers = {k: v for k, v in request.headers.items() if k.lower() not in {"host", "content-length"}}
    try:
        async with _httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            resp = await client.request(request.method, target, headers=headers, content=await request.body())
        content_type = str(resp.headers.get("content-type") or "").lower()
        # If Vite is up but returned 5xx or an empty body for the SPA shell,
        # fall back to the static dist build so the admin UI still loads
        # instead of showing a blank page.
        if int(resp.status_code) >= 500:
            return None
        if (not path) and int(resp.status_code) == 200 and len(resp.content or b"") < 50:
            return None
        # Vite can return index.html (200 text/html) for missing module chunks.
        # Convert that to 404 so browsers don't fail with MIME mismatch.
        if (
            _looks_like_admin_asset_request(path)
            and int(resp.status_code) == 200
            and "text/html" in content_type
        ):
            return _Response(
                content='{"detail":"Admin asset not found"}',
                status_code=404,
                headers={"content-type": "application/json"},
            )
        resp_headers = {k: v for k, v in resp.headers.items() if k.lower() not in _VITE_HOP_BY_HOP}
        return _Response(content=resp.content, status_code=resp.status_code, headers=resp_headers)
    except Exception:
        return None


@router.get("", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    """Serve the React admin dashboard shell."""
    vite = await _proxy_vite_request(request, "")
    if vite is not None:
        return vite
    if _ADMIN_UI_INDEX_FILE.exists():
        return FileResponse(_ADMIN_UI_INDEX_FILE)
    return HTMLResponse(
        "<h3>Admin UI build not found.</h3><p>Build React app in <code>admin_ui</code> using <code>npm run build</code>.</p>",
        status_code=503,
    )


def _resolve_admin_asset(full_path: str) -> Optional[Path]:
    path = str(full_path or "").strip().lstrip("/")
    if not path:
        return None
    candidate = (_ADMIN_UI_DIST_DIR / path).resolve()
    try:
        candidate.relative_to(_ADMIN_UI_DIST_DIR)
    except ValueError:
        return None
    if candidate.is_file():
        return candidate
    return None


@router.get("/{full_path:path}", include_in_schema=False)
async def admin_spa_fallback(full_path: str, request: Request):
    """Serve static assets and SPA fallback for React admin routes."""
    if str(full_path or "").startswith("api/"):
        raise HTTPException(status_code=404, detail="Not found")

    vite = await _proxy_vite_request(request, full_path)
    if vite is not None:
        return vite

    asset = _resolve_admin_asset(full_path)
    if asset:
        # Vite content-hashes everything under assets/ (chat-6xcMeWk4.js), so the
        # filename CHANGES whenever the bytes do — the file at a given name can
        # never go stale and is safe to cache for a year. Without this the widget
        # re-validated all seven bundles on every single open: seven conditional
        # round-trips before a returning guest saw anything. A new build ships new
        # filenames, so this cannot pin anyone to an old bundle.
        #
        # Deliberately NOT applied to the un-hashed entry points (index.html /
        # chat.html), which must keep revalidating or a phone caches a shell
        # pointing at bundles that no longer exist.
        headers = None
        if str(full_path or "").lstrip("/").startswith("assets/"):
            headers = {"Cache-Control": "public, max-age=31536000, immutable"}
        return FileResponse(asset, headers=headers)

    # Important: for missing chunks/assets return 404, not index.html.
    # Returning HTML here causes MIME errors in the browser for module scripts.
    if _looks_like_admin_asset_request(full_path):
        raise HTTPException(status_code=404, detail="Admin asset not found")

    if _ADMIN_UI_INDEX_FILE.exists():
        return FileResponse(_ADMIN_UI_INDEX_FILE)

    return HTMLResponse(
        "<h3>Admin UI build not found.</h3><p>Build React app in <code>admin_ui</code> using <code>npm run build</code>.</p>",
        status_code=503,
    )

