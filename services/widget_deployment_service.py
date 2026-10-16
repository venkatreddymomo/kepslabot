"""
Widget Deployment Service

Manages embeddable chat widget deployments. Each deployment has:
  - widget_key: public, rotatable identifier exposed in customer snippets
  - hotel_code: tenant scope
  - allowed_origins: list of host domains permitted to embed this widget
  - theme/size/position: branding overrides

Storage: MySQL table `new_bot_widget_deployments` (one row per widget_key).
All public methods are async and use AsyncSessionLocal.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from sqlalchemy import delete, select, update
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import defer

from models.database import AsyncSessionLocal, WidgetDeployment

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent.parent
_PROPERTIES_DIR = _BASE_DIR / "config" / "properties"

# Flipped to False the first time the DB reports the `model` column is missing
# (migration not applied yet). While False every query omits the column and the
# service behaves EXACTLY as it did before deployment pinning existed, so a
# restart that lands before the migration degrades to "nobody is pinned" rather
# than 500-ing the bootstrap and blanking the widget on customer sites.
_model_column_supported = True

# widget_key -> (pinned_model, cached_at). Read once per chat turn on the guest
# hot path; deployment rows change ~never, and every write path invalidates.
_model_cache: Dict[str, tuple] = {}
_MODEL_CACHE_TTL_SECONDS = 300.0


# widget_key -> (deployment_dict_or_None, cached_at). A SINGLE bootstrap request
# looked this same row up four times — bootstrap_payload, origin_allowed, and
# twice more from WidgetOriginMiddleware — each opening its own session and
# paying a full round trip. Deployment rows change only when an operator edits
# the widget, and every write path below invalidates, so this is the same
# trade-off _model_cache already makes on the chat hot path.
_deployment_cache: Dict[str, tuple] = {}
_DEPLOYMENT_CACHE_TTL_SECONDS = 30.0


def _invalidate_model_cache() -> None:
    _model_cache.clear()
    _deployment_cache.clear()


def _is_missing_model_column(exc: Exception) -> bool:
    msg = str(getattr(exc, "orig", exc) or exc).lower()
    return "1054" in msg or "unknown column" in msg


def _select_deployment():
    """SELECT for WidgetDeployment, omitting `model` when it isn't in the DB yet."""
    stmt = select(WidgetDeployment)
    if not _model_column_supported:
        stmt = stmt.options(defer(WidgetDeployment.model))
    return stmt

DEFAULT_THEME = {
    "brand_color": "#FF6816",
    "accent_color": "#FF6816",
    "bg_color": "#FFFFFF",
    "text_color": "#1A1A2E",
}

DEFAULT_SIZE = {
    "width": 380,
    "height": 620,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_origin(value: str) -> str:
    """Normalize an origin to scheme://host[:port], lowercased. '*' passes through."""
    raw = (value or "").strip()
    if not raw:
        return ""
    if raw == "*":
        return "*"
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parsed = urlparse(raw)
        if not parsed.scheme or not parsed.netloc:
            return ""
        return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
    except Exception:
        return ""


def _normalize_color(value: Any, fallback: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return fallback
    if raw.startswith("#"):
        return raw
    if len(raw) in (3, 4, 6, 8) and all(c in "0123456789abcdefABCDEF" for c in raw):
        return "#" + raw
    return raw


def _clamp(num: Any, fallback: int, lo: int, hi: int) -> int:
    try:
        v = int(num)
    except Exception:
        return fallback
    return max(lo, min(hi, v))


def _coerce_json(value: Any, fallback: Any) -> Any:
    """JSON columns may come back as dict/list (MySQL JSON) or string (legacy)."""
    if value is None:
        return fallback
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return fallback
    return fallback


class WidgetDeploymentService:
    """DB-backed deployment store. One row per widget_key."""

    # ---- Helpers ---------------------------------------------------------

    @staticmethod
    def _gen_key() -> str:
        # 24 url-safe chars ~ 144 bits of entropy. Prefixed for log-grep.
        return "wk_" + secrets.token_urlsafe(18)

    @staticmethod
    def _hotel_code(value: Any) -> str:
        return str(value or "").strip().lower() or "default"

    def _hotel_defaults(self, hotel_code: str) -> Dict[str, Any]:
        """Pull theme/branding defaults from the property JSON if available."""
        defaults = {
            "bot_name": "Assistant",
            "welcome_message": "",
            "theme": dict(DEFAULT_THEME),
            "size": dict(DEFAULT_SIZE),
            "position": "right",
            "auto_open": False,
            "phase": "pre_booking",
            "session_ttl_hours": 24,
            "idle_timeout_minutes": 30,
            "logo_url": "",
        }
        try:
            path = _PROPERTIES_DIR / f"{hotel_code}.json"
            if not path.exists():
                return defaults
            payload = json.loads(path.read_text(encoding="utf-8"))
            business = (payload or {}).get("business", {}) or {}
            if business.get("bot_name"):
                defaults["bot_name"] = str(business["bot_name"]).strip() or defaults["bot_name"]
            if business.get("welcome_message"):
                defaults["welcome_message"] = str(business["welcome_message"]).strip()
            if business.get("logo_url"):
                defaults["logo_url"] = str(business["logo_url"]).strip()
            theme_src = business.get("widget_theme") or business.get("theme") or {}
            if isinstance(theme_src, dict):
                for k in ("brand_color", "accent_color", "bg_color", "text_color"):
                    if theme_src.get(k):
                        defaults["theme"][k] = _normalize_color(theme_src[k], defaults["theme"][k])
        except Exception:
            pass
        return defaults

    def _normalize_record(self, hotel_code: str, body: Dict[str, Any]) -> Dict[str, Any]:
        defaults = self._hotel_defaults(hotel_code)
        theme_in = (body.get("theme") or {}) if isinstance(body.get("theme"), dict) else {}
        size_in = (body.get("size") or {}) if isinstance(body.get("size"), dict) else {}

        theme = {
            "brand_color": _normalize_color(theme_in.get("brand_color"), defaults["theme"]["brand_color"]),
            "accent_color": _normalize_color(theme_in.get("accent_color"), defaults["theme"]["accent_color"]),
            "bg_color": _normalize_color(theme_in.get("bg_color"), defaults["theme"]["bg_color"]),
            "text_color": _normalize_color(theme_in.get("text_color"), defaults["theme"]["text_color"]),
        }
        size = {
            "width": _clamp(size_in.get("width"), defaults["size"]["width"], 280, 600),
            "height": _clamp(size_in.get("height"), defaults["size"]["height"], 360, 900),
        }
        position = str(body.get("position") or defaults["position"]).strip().lower()
        if position not in ("left", "right"):
            position = "right"

        allowed_origins_in = body.get("allowed_origins") or []
        if not isinstance(allowed_origins_in, list):
            allowed_origins_in = []
        normalized_origins: List[str] = []
        for origin in allowed_origins_in:
            n = _normalize_origin(str(origin))
            if n and n not in normalized_origins:
                normalized_origins.append(n)

        bot_name = str(body.get("bot_name") or defaults["bot_name"]).strip() or defaults["bot_name"]
        phase = str(body.get("phase") or defaults["phase"]).strip().lower() or defaults["phase"]
        status = str(body.get("status") or "active").strip().lower()
        if status not in ("active", "inactive"):
            status = "active"
        name = str(body.get("name") or "").strip() or f"{hotel_code} widget"
        logo_url = str(body.get("logo_url") or "").strip()
        try:
            session_ttl_hours = max(1, min(int(body.get("session_ttl_hours") or 24), 720))
        except (TypeError, ValueError):
            session_ttl_hours = 24

        raw_idle = body.get("idle_timeout_minutes")
        if raw_idle is None:
            idle_timeout_minutes = defaults["idle_timeout_minutes"]
        else:
            try:
                idle_timeout_minutes = int(raw_idle)
            except (TypeError, ValueError):
                idle_timeout_minutes = defaults["idle_timeout_minutes"]
            if idle_timeout_minutes < 0:
                idle_timeout_minutes = 0
            elif idle_timeout_minutes > 720:
                idle_timeout_minutes = 720

        return {
            "hotel_code": hotel_code,
            "name": name,
            "status": status,
            "allowed_origins": normalized_origins,
            "theme": theme,
            "size": size,
            "position": position,
            "bot_name": bot_name,
            "phase": phase,
            "auto_open": bool(body.get("auto_open", defaults["auto_open"])),
            "logo_url": logo_url,
            "session_ttl_hours": session_ttl_hours,
            "idle_timeout_minutes": idle_timeout_minutes,
            "model": self._normalize_model(body.get("model")),
        }

    @staticmethod
    def _normalize_model(value: Any) -> str:
        """Validate a pinned model id against the allowlist. '' = unpinned.

        Rejects unknown ids at the write boundary so a typo can't silently pin a
        customer to a model the build can't route."""
        raw = str(value or "").strip()
        if not raw:
            return ""
        from services.model_override_service import AVAILABLE_MODELS

        allowed = {str(m["id"]).strip() for m in AVAILABLE_MODELS}
        if raw not in allowed:
            raise ValueError(f"unsupported model: {raw}")
        return raw

    @staticmethod
    def _row_to_dict(row: WidgetDeployment) -> Dict[str, Any]:
        """Convert a WidgetDeployment row to the legacy JSON-shape dict."""
        created = row.created_at.isoformat() if row.created_at else ""
        updated = row.updated_at.isoformat() if row.updated_at else ""
        return {
            "widget_key": row.widget_key,
            "hotel_code": row.hotel_code,
            "name": row.name,
            "status": row.status,
            "allowed_origins": _coerce_json(row.allowed_origins, []),
            "theme": _coerce_json(row.theme, dict(DEFAULT_THEME)),
            "size": _coerce_json(row.sizes, dict(DEFAULT_SIZE)),
            "position": row.position,
            "bot_name": row.bot_name,
            "phase": row.phase,
            "auto_open": bool(row.auto_open),
            "logo_url": row.logo_url or "",
            "session_ttl_hours": int(row.session_ttl_hours or 24),
            "idle_timeout_minutes": int(row.idle_timeout_minutes or 30),
            # Guarded: pre-migration the column is deferred and unreadable.
            "model": (str(row.model or "") if _model_column_supported else ""),
            "created_at": created,
            "updated_at": updated,
        }

    # ---- Public API ------------------------------------------------------

    async def list(self, hotel_code: Optional[str] = None) -> List[Dict[str, Any]]:
        async def run() -> List[Dict[str, Any]]:
            async with AsyncSessionLocal() as s:
                stmt = _select_deployment()
                if hotel_code:
                    stmt = stmt.where(WidgetDeployment.hotel_code == self._hotel_code(hotel_code))
                stmt = stmt.order_by(WidgetDeployment.created_at.asc())
                rows = (await s.execute(stmt)).scalars().all()
                return [self._row_to_dict(r) for r in rows]

        return await self._with_model_column_fallback(run)

    async def get(self, widget_key: str) -> Optional[Dict[str, Any]]:
        if not widget_key:
            return None

        cached = _deployment_cache.get(widget_key)
        if cached is not None and (time.monotonic() - cached[1]) < _DEPLOYMENT_CACHE_TTL_SECONDS:
            return cached[0]

        async def run() -> Optional[Dict[str, Any]]:
            async with AsyncSessionLocal() as s:
                row = (
                    await s.execute(
                        _select_deployment().where(WidgetDeployment.widget_key == widget_key)
                    )
                ).scalar_one_or_none()
                return self._row_to_dict(row) if row is not None else None

        result = await self._with_model_column_fallback(run)
        # A miss is cached too: an unknown key is exactly what a scraper hammers,
        # and re-querying for every one of those is the expensive answer.
        _deployment_cache[widget_key] = (result, time.monotonic())
        return result

    @staticmethod
    async def _with_model_column_fallback(run):
        """Run `run()`; on a missing-`model`-column error, disable the column
        process-wide and retry once. Keeps the widget alive on customer sites if
        the app is restarted before the migration is applied."""
        global _model_column_supported
        try:
            return await run()
        except (OperationalError, ProgrammingError) as exc:
            if _model_column_supported and _is_missing_model_column(exc):
                logger.warning(
                    "widget_deployments.model column not present yet; "
                    "treating every deployment as unpinned until the migration is applied"
                )
                _model_column_supported = False
                return await run()
            raise

    async def create(self, hotel_code: str, body: Dict[str, Any]) -> Dict[str, Any]:
        hotel_code = self._hotel_code(hotel_code)
        record = self._normalize_record(hotel_code, body or {})
        async with AsyncSessionLocal() as s:
            # Generate a key that doesn't collide.
            while True:
                key = self._gen_key()
                exists = (
                    await s.execute(
                        select(WidgetDeployment.id).where(WidgetDeployment.widget_key == key)
                    )
                ).first()
                if not exists:
                    break
            row = WidgetDeployment(
                widget_key=key,
                hotel_code=record["hotel_code"],
                name=record["name"],
                status=record["status"],
                allowed_origins=record["allowed_origins"],
                theme=record["theme"],
                sizes=record["size"],
                position=record["position"],
                bot_name=record["bot_name"],
                phase=record["phase"],
                auto_open=bool(record["auto_open"]),
                logo_url=record["logo_url"],
                session_ttl_hours=record["session_ttl_hours"],
                idle_timeout_minutes=record["idle_timeout_minutes"],
                model=record["model"] or None,
            )
            # NOTE: `async with s.begin()` raises InvalidRequestError here — the
            # widget_key uniqueness SELECT above already auto-began the
            # transaction. Commit the ambient one instead.
            s.add(row)
            await s.commit()
            await s.refresh(row)
            _invalidate_model_cache()
            return self._row_to_dict(row)

    async def update(self, widget_key: str, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        async with AsyncSessionLocal() as s:
            row = (
                await s.execute(
                    _select_deployment().where(WidgetDeployment.widget_key == widget_key)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            existing = self._row_to_dict(row)
            hotel_code = self._hotel_code(body.get("hotel_code") or existing.get("hotel_code"))
            merged = {**existing, **(body or {})}
            record = self._normalize_record(hotel_code, merged)
            row.hotel_code = record["hotel_code"]
            row.name = record["name"]
            row.status = record["status"]
            row.allowed_origins = record["allowed_origins"]
            row.theme = record["theme"]
            row.sizes = record["size"]
            row.position = record["position"]
            row.bot_name = record["bot_name"]
            row.phase = record["phase"]
            row.auto_open = bool(record["auto_open"])
            row.logo_url = record["logo_url"]
            row.session_ttl_hours = record["session_ttl_hours"]
            row.idle_timeout_minutes = record["idle_timeout_minutes"]
            if _model_column_supported:
                row.model = record["model"] or None
            # See create(): the SELECT above already opened the transaction, so
            # `async with s.begin()` would raise instead of saving. This is why
            # every deployment update used to 500.
            await s.commit()
            await s.refresh(row)
            _invalidate_model_cache()
            return self._row_to_dict(row)

    async def delete(self, widget_key: str) -> bool:
        async with AsyncSessionLocal() as s:
            async with s.begin():
                result = await s.execute(
                    delete(WidgetDeployment).where(WidgetDeployment.widget_key == widget_key)
                )
            _invalidate_model_cache()
            return int(result.rowcount or 0) > 0

    async def rotate_key(self, widget_key: str) -> Optional[Dict[str, Any]]:
        async with AsyncSessionLocal() as s:
            row = (
                await s.execute(
                    _select_deployment().where(WidgetDeployment.widget_key == widget_key)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            while True:
                new_key = self._gen_key()
                exists = (
                    await s.execute(
                        select(WidgetDeployment.id).where(WidgetDeployment.widget_key == new_key)
                    )
                ).first()
                if not exists:
                    break
            # See create(): the collision-check SELECT above already opened the
            # transaction, so `async with s.begin()` would raise here too.
            await s.execute(
                update(WidgetDeployment)
                .where(WidgetDeployment.widget_key == widget_key)
                .values(widget_key=new_key)
            )
            await s.commit()
            _invalidate_model_cache()
            return await self.get(new_key)

    # ---- Pinned model (server-side resolution for the chat request boundary) --

    async def model_for_widget_key(self, widget_key: str) -> str:
        """The chat model pinned to this deployment, or '' when unpinned.

        Called once per chat turn, so it's cached (deployment rows change ~never;
        every write path above invalidates). Returns '' for unknown/inactive keys
        so a bad key degrades to normal model resolution rather than erroring the
        guest's turn."""
        key = str(widget_key or "").strip()
        if not key or not _model_column_supported:
            return ""

        cached = _model_cache.get(key)
        if cached and (time.monotonic() - cached[1]) <= _MODEL_CACHE_TTL_SECONDS:
            return cached[0]

        try:
            dep = await self.get(key)
        except Exception:
            logger.exception("widget_model_lookup_failed key=%s", key)
            return ""
        model = ""
        if dep and dep.get("status") == "active":
            model = str(dep.get("model") or "").strip()
        _model_cache[key] = (model, time.monotonic())
        return model

    # ---- Bootstrap (public, served to the loader) ------------------------

    async def bootstrap_payload(self, widget_key: str, host_origin: str = "") -> Optional[Dict[str, Any]]:
        """Return the public, embed-safe config the loader needs to mount the iframe."""
        dep = await self.get(widget_key)
        if not dep or dep.get("status") != "active":
            return None

        host = (host_origin or "").rstrip("/")
        chat_url = f"{host}/chat" if host else "/chat"

        theme = dict(dep["theme"])
        logo_url = dep.get("logo_url") or ""
        position = dep["position"]

        # Bot Training wizard branding (ui_settings, Step 3) overrides the stored
        # deployment theme — same precedence as the hotel_code path
        # (public_defaults_for_hotel). Without this merge a wizard color change is
        # invisible to widget_key embeds, because the loader only reads this
        # deployment row (often still the default theme). Keeps both bootstrap
        # paths symmetric so the wizard is the single source of truth for branding.
        ui = await self._ui_branding_for_hotel(dep["hotel_code"])
        if ui.get("theme"):
            theme = {**theme, **ui["theme"]}
        if ui.get("logo_url"):
            logo_url = ui["logo_url"]
        if ui.get("position"):
            position = ui["position"]

        return {
            "widget_key": widget_key,
            "hotel_code": dep["hotel_code"],
            "bot_name": dep["bot_name"],
            "phase": dep["phase"],
            "theme": theme,
            "size": dep["size"],
            "position": position,
            "auto_open": dep["auto_open"],
            "logo_url": logo_url,
            "session_ttl_hours": dep.get("session_ttl_hours") or 24,
            "idle_timeout_minutes": dep.get("idle_timeout_minutes", 30),
            "iframe_url": chat_url,
        }

    async def _ui_branding_for_hotel(self, hotel_code: str) -> Dict[str, Any]:
        """Branding the operator set in the Bot Training wizard (Step 3).

        Those colors are saved to the per-hotel ``ui_settings`` config (theme
        keys: primary_color/accent_color/background_color/text_color) — NOT to a
        widget deployment — so the standalone /chat page must read them here to
        stay on-brand. Returns a partial {theme, logo_url, position} or {} on any
        failure. Scoped to the given hotel_code via the db_config context var.
        """
        try:
            from services.db_config_service import db_config_service

            with db_config_service.scoped_hotel(hotel_code):
                ui = await db_config_service.get_ui_settings()
        except Exception:
            return {}
        if not isinstance(ui, dict) or not ui:
            return {}

        out: Dict[str, Any] = {}
        theme_src = ui.get("theme") if isinstance(ui.get("theme"), dict) else {}
        mapping = {
            "primary_color": "brand_color",
            "accent_color": "accent_color",
            "background_color": "bg_color",
            "text_color": "text_color",
        }
        theme = {
            dst: _normalize_color(theme_src.get(src), "")
            for src, dst in mapping.items()
            if theme_src.get(src)
        }
        theme = {k: v for k, v in theme.items() if v}
        if theme:
            out["theme"] = theme
        if ui.get("logo_url"):
            out["logo_url"] = str(ui["logo_url"]).strip()
        widget_src = ui.get("widget") if isinstance(ui.get("widget"), dict) else {}
        pos = str(widget_src.get("position") or "").strip().lower()
        if pos in ("left", "right"):
            out["position"] = pos
        return out

    # Copy shown above the opening lead-capture form. Historically hardcoded in
    # ChatHarness.tsx, which is shared by every tenant — so a single-property or
    # formal-tone hotel got Zone's plural, playful wording. Each hotel can now
    # override it via ui_settings.widget.lead_form; the defaults below are the
    # exact strings that used to be inlined, so tenants that set nothing render
    # byte-identically to before.
    LEAD_FORM_DEFAULTS: Dict[str, str] = {
        "title": "Hey there! Let's get you set up \U0001f60a",
        "subtitle": (
            "Just a few quick details so we can tailor your stay — your name, "
            "number, email, and the destination you have in mind. Then we'll dive right in!"
        ),
        "consent": "",
    }

    def welcome_service_ctas_for_hotel(self, hotel_code: str) -> list:
        """CTA button labels shown under the opening bot message for this hotel.

        Opt-IN map from settings.chat_welcome_service_ctas_json. A hotel that is
        not listed gets an empty list and renders no buttons, exactly as before,
        so this can never change another tenant's widget.
        """
        import json as _json

        from config.settings import settings as _settings

        code = self._hotel_code(hotel_code)
        if not code:
            return []
        from services import prebooking_settings_service

        if prebooking_settings_service.is_admin_managed(code):
            # Admin-managed hotels use `welcome_buttons` (label + message) from
            # the Pre-booking tab instead of this label-only list.
            return []
        raw = str(getattr(_settings, "chat_welcome_service_ctas_json", "") or "").strip()
        if not raw:
            return []
        try:
            parsed = _json.loads(raw)
        except Exception:
            logger.warning("chat_welcome_service_ctas_json is not valid JSON; ignoring")
            return []
        if not isinstance(parsed, dict):
            return []
        for key, value in parsed.items():
            if str(key or "").strip().lower() != code:
                continue
            if not isinstance(value, list):
                return []
            return [str(v).strip() for v in value if str(v or "").strip()][:4]
        return []

    async def lead_form_for_hotel(self, hotel_code: str) -> Dict[str, Any]:
        """Per-hotel lead-form copy, fields and OTP settings.

        Three sources, later winning:
          1. LEAD_FORM_DEFAULTS      — the strings that used to be inlined.
          2. ui_settings.widget.lead_form — the copy-only override that shipped
             before the Forms tab existed; still honoured so nobody's saved
             wording disappears.
          3. The Forms tab row in GHN_CHATBOT_FORMS — fields, OTP, and copy.

        ``consent`` is empty by default: only a hotel that has actually agreed
        consent wording gets a consent line rendered.

        Every step is best-effort. A hotel that has never opened the Forms tab,
        or a DB that is briefly unreachable, still gets the legacy copy and the
        legacy four fields — the widget never renders an empty form.
        """
        out: Dict[str, Any] = dict(self.LEAD_FORM_DEFAULTS)

        # (2) legacy copy-only override
        try:
            from services.db_config_service import db_config_service

            with db_config_service.scoped_hotel(self._hotel_code(hotel_code)):
                ui = await db_config_service.get_ui_settings()
            if isinstance(ui, dict) and ui:
                widget_src = ui.get("widget") if isinstance(ui.get("widget"), dict) else {}
                lead_src = (
                    widget_src.get("lead_form")
                    if isinstance(widget_src.get("lead_form"), dict)
                    else {}
                )
                for key in ("title", "subtitle", "consent"):
                    value = str(lead_src.get(key) or "").strip()
                    if value:
                        out[key] = value
        except Exception:
            logger.debug("lead form: ui_settings override unavailable", exc_info=True)

        # (3) the Forms tab row — the operator-editable definition
        try:
            from models.database import AsyncSessionLocal
            from services import lead_form_service

            async with AsyncSessionLocal() as session:
                saved = await lead_form_service.get_lead_form(session, hotel_code)

            out["fields"] = saved.get("fields") or []
            out["otp_config"] = saved.get("otp_config") or {}
            out["submit_label"] = saved.get("submit_label") or "Continue"
            out["auto_load_form"] = bool(saved.get("auto_load_form", True))
            out["form_key"] = saved.get("form_key") or ""
            # Copy from a SAVED row outranks the legacy override; an unsaved
            # hotel keeps whatever step 2 resolved.
            if saved.get("exists"):
                if str(saved.get("title") or "").strip():
                    out["title"] = saved["title"]
                if str(saved.get("description") or "").strip():
                    out["subtitle"] = saved["description"]
                # Consent is assigned even when empty: clearing the line in the
                # Forms tab has to actually remove it, not silently fall back to
                # whatever ui_settings still holds.
                out["consent"] = str(saved.get("consent") or "")
        except Exception:
            # No row, or the table is unreachable: fall back to the four fields
            # the widget has always rendered rather than showing nothing.
            logger.warning("lead form: falling back to built-in fields", exc_info=True)
            try:
                from services import lead_form_service

                out["fields"] = [dict(f) for f in lead_form_service.DEFAULT_FIELDS]
                out["otp_config"] = dict(lead_form_service.DEFAULT_OTP_CONFIG)
            except Exception:
                out["fields"] = []
                out["otp_config"] = {}
            out.setdefault("submit_label", "Continue")
            out.setdefault("auto_load_form", True)

        return out

    async def public_defaults_for_hotel(self, hotel_code: str) -> Dict[str, Any]:
        """Embed-safe branding for the hotel_code bootstrap path.

        Resolution (later wins): property-JSON defaults -> active widget
        deployment branding (Admin > Widget) -> Bot Training wizard branding
        (ui_settings, Step 3). The wizard wins because that's the per-hotel theme
        operators actually edit, and it's the source the copy-paste embed snippet
        is generated from. Never returns the widget_key (origin-gated path only).
        """
        hotel_code = self._hotel_code(hotel_code)
        try:
            deployments = await self.list(hotel_code=hotel_code)
        except Exception:
            # DB unavailable — degrade to property-JSON defaults rather than 500.
            deployments = []
        active = [d for d in deployments if d.get("status") == "active"]
        if active:
            dep = active[-1]  # list() orders by created_at asc -> newest last
            result: Dict[str, Any] = {
                "bot_name": dep["bot_name"],
                "phase": dep["phase"],
                "theme": dict(dep["theme"]),
                "size": dep["size"],
                "position": dep["position"],
                "auto_open": dep["auto_open"],
                "logo_url": dep.get("logo_url", ""),
                "session_ttl_hours": dep.get("session_ttl_hours", 24),
                "idle_timeout_minutes": dep.get("idle_timeout_minutes", 30),
            }
        else:
            result = self._hotel_defaults(hotel_code)

        # Wizard branding (ui_settings) overrides for the hotel_code path.
        ui = await self._ui_branding_for_hotel(hotel_code)
        if ui.get("theme"):
            result = {**result, "theme": {**(result.get("theme") or {}), **ui["theme"]}}
        if ui.get("logo_url"):
            result["logo_url"] = ui["logo_url"]
        if ui.get("position"):
            result["position"] = ui["position"]
        return result

    # ---- Origin enforcement ---------------------------------------------

    async def origin_allowed(self, widget_key: str, origin: str) -> bool:
        """Check whether `origin` is in the deployment's allowlist."""
        dep = await self.get(widget_key)
        if not dep:
            return False
        allowed = dep.get("allowed_origins") or []
        if not allowed:
            # Empty allowlist = wildcard (only useful for dev; admins should set domains).
            return True
        if "*" in allowed:
            return True
        normalized = _normalize_origin(origin)
        return bool(normalized) and normalized in allowed


widget_deployment_service = WidgetDeploymentService()
