"""
Runtime chat-model override.

Lets the /chat test harness pick which OpenAI chat/completion model every LLM
call uses, at runtime, in the DEPLOYED build — so QA can compare models without
editing .env and rebuilding the Docker image each time.

Design:
- Process-global (the app runs a single uvicorn worker) and persisted to a
  small JSON file so the choice survives a restart.
- ONLY chat/completion models are overridden. Embeddings are left untouched
  (they use a different model family: text-embedding-3-*).
- Applied at the single OpenAI choke point (llm/client.py), so it transparently
  covers every caller (main router, service router, continuity, book answerer,
  service agent, beautifier, summary, suggestions, ticketing LLM, ...).

Scope (IMPORTANT — the harness must never move a customer's bot):
- The harness override above is process-global, so on its own it would force
  EVERY caller — including guests on a customer's embedded widget — onto
  whatever QA picked in the dropdown. The harness page and the embedded widget
  are the same component (ChatHarness.tsx) on the same hotel, so hotel scope
  can NOT separate them.
- The separator is the DEPLOYMENT PIN: an embedded widget carries a widget_key,
  whose deployment row may pin a model. The request boundary resolves that pin
  once per turn into `_pinned_model_ctx` (a ContextVar — one worker serves the
  harness and every embed concurrently, so a plain global would leak across
  requests).
- Precedence is `pin > harness override > caller/env default`. The harness has
  no widget_key (it isn't embedded) => never pinned => the dropdown still works
  for QA. A customer embed always has one => the dropdown can never reach it.
  That falls out of the architecture rather than relying on a flag being right.
- The pin is ALWAYS resolved server-side from the deployment row, never read
  from the iframe URL, so a guest editing the URL can't choose a model.

`AVAILABLE_MODELS` is the single source of truth: the harness dropdown renders
exactly this list and POST validates against it, so adding/renaming a model is a
one-line change here.
"""

from __future__ import annotations

import json
import logging
import threading
from contextvars import ContextVar, Token
from pathlib import Path

from config.settings import settings

logger = logging.getLogger(__name__)

# The models the /chat harness offers. Labels are human; ids are sent verbatim.
# Transport is decided by the id in llm/client.py `_is_openrouter_model`: an id with
# a "provider/" slash routes to the OpenRouter client; a bare id routes to the direct
# OpenAI client (OpenAI api key). So the GPT-* ids below are BARE → they use the OpenAI
# API key directly. Only GLM / Claude (OpenRouter-only) keep a provider prefix.
AVAILABLE_MODELS: list[dict[str, str]] = [
    {"id": "gpt-4.1-mini", "label": "GPT 4.1 mini"},
    {"id": "gpt-5.4-mini", "label": "GPT 5.4 mini"},
    {"id": "gpt-5.4-nano", "label": "GPT 5.4 nano"},
    {"id": "gpt-5.6-luna", "label": "GPT 5.6 luna"},
    {"id": "z-ai/glm-5.2", "label": "GLM 5.2 (OpenRouter)"},
    {"id": "anthropic/claude-haiku-4.5", "label": "Claude Haiku 4.5 (OpenRouter)"},
]
_ALLOWED_IDS = {str(m["id"]).strip() for m in AVAILABLE_MODELS}

_STORE_FILE = Path("./data/runtime/model_override.json")
_lock = threading.Lock()
_state: dict[str, str] = {"model": ""}

# Per-request deployment pin. Set at the chat request boundary (WS connect / HTTP
# turn) from the widget_key's deployment row; "" = not pinned (harness/dev).
# A ContextVar (not a global) because one worker serves the harness and every
# customer embed concurrently.
_pinned_model_ctx: ContextVar[str] = ContextVar("pinned_chat_model", default="")


def _load() -> None:
    try:
        if _STORE_FILE.exists():
            data = json.loads(_STORE_FILE.read_text(encoding="utf-8"))
            model = str((data or {}).get("model") or "").strip()
            if model in _ALLOWED_IDS:
                _state["model"] = model
    except Exception:
        logger.exception("model_override_load_failed")


def _persist() -> None:
    try:
        _STORE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STORE_FILE.write_text(
            json.dumps({"model": _state.get("model", "")}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        logger.exception("model_override_persist_failed")


def available_models() -> list[dict[str, str]]:
    return [dict(m) for m in AVAILABLE_MODELS]


def default_model() -> str:
    """The env-configured base chat model (used when no override is active)."""
    return str(getattr(settings, "openai_model", "") or "").strip()


def get_model_override() -> str:
    """Active override chat model id, or '' when none is set."""
    return _state.get("model", "") or ""


# ---- Deployment pin (per-request) ----------------------------------------


def set_pinned_model(model: str | None) -> Token:
    """Pin this request's chat model to a customer deployment's configured model.

    Call at the request boundary with the value resolved from the deployment row
    (NEVER from a client-supplied URL param). Returns a Token to reset with.
    Unknown ids are ignored (stored as "") so a stale row can't wedge the bot on
    a model the build can't route — it degrades to normal resolution."""
    value = str(model or "").strip()
    if value and value not in _ALLOWED_IDS:
        logger.warning("pinned_model_ignored unsupported=%r", value)
        value = ""
    return _pinned_model_ctx.set(value)


def reset_pinned_model(token: Token) -> None:
    try:
        _pinned_model_ctx.reset(token)
    except Exception:
        pass


def get_pinned_model() -> str:
    """This request's pinned model, or '' when not pinned.

    A widget deployment pin wins; otherwise the CURRENT hotel's entry in
    CHAT_LLM_MODEL_BY_HOTEL_JSON. Every request path (WS, HTTP, chip pre-cache)
    scopes the hotel in db_config_service before any LLM call, so listing one
    hotel moves only that hotel's turns - hotels not listed resolve exactly as
    before."""
    try:
        pinned = _pinned_model_ctx.get() or ""
    except Exception:
        pinned = ""
    return pinned or _hotel_env_model()


def _hotel_env_model() -> str:
    raw = str(getattr(settings, "chat_llm_model_by_hotel_json", "") or "").strip()
    if raw:
        try:
            mapping = json.loads(raw)
            from services.db_config_service import db_config_service

            hotel = str(db_config_service.get_current_hotel_code() or "").strip().lower()
        except Exception:
            return ""
        if not isinstance(mapping, dict) or not hotel or hotel == "default":
            return ""
        for key, value in mapping.items():
            if str(key or "").strip().lower() == hotel:
                model = str(value or "").strip()
                return model if model in _ALLOWED_IDS else ""
    return _prebooking_hotel_model()


def _prebooking_hotel_model() -> str:
    """CHAT_LLM_PREBOOKING_HOTELS_MODEL for a hotel set up in the Bot Training ->
    Pre-booking tab (T24 onward), else "". Zone, Mango and Sarovar have no
    Pre-booking row, so they resolve exactly as before."""
    model = str(getattr(settings, "chat_llm_prebooking_hotels_model", "") or "").strip()
    if model not in _ALLOWED_IDS:
        return ""
    try:
        from services import prebooking_settings_service
        from services.db_config_service import db_config_service

        hotel = str(db_config_service.get_current_hotel_code() or "").strip().lower()
        if hotel and hotel != "default" and prebooking_settings_service.is_admin_managed(hotel):
            return model
    except Exception:
        return ""
    return ""


def resolve_active_model() -> str:
    """The chat model a NON-role call runs on: pin > harness override > env default.

    Single source of truth shared by llm/client (which model to CALL) and
    prompt_registry (which model's PROMPTS to load), so the two can never
    disagree — a 4.1-pinned embed running 5.4-tuned prompts would be worse than
    either model alone.

    In MULTI-MODEL mode the harness override is skipped (it is disabled), so this
    returns pin > env default. Role-specific callers (router / answerer / chips)
    use resolve_role_model() below instead."""
    if multi_model_enabled():
        return (get_pinned_model() or default_model() or "").strip()
    return (get_pinned_model() or get_model_override() or default_model() or "").strip()


# ---- Multi-model role assignment -----------------------------------------
#
# Routing decisions run on the model that ROUTES best (4.1); guest-facing
# ANSWERS and suggestion CHIPS run on the model that WRITES best (5.4). Each
# role also loads THAT model's prompt rows, because every role call site passes
# its resolved model into prompt_registry.get(model=...). So within ONE turn 4.1
# routes with 4.1's prompts and 5.4 answers with 5.4's prompts.
#
# While multi-model is enabled the /chat harness dropdown is DISABLED (a no-op)
# — otherwise a single-model pick would silently collapse the split and make the
# comparison meaningless. A deployment PIN still wins outright, so a customer's
# embedded widget is never moved by this.
ROLE_MODEL_DEFAULTS: dict[str, str] = {
    # routing -> 4.1
    "main_router": "gpt-4.1-mini",
    "service_router": "gpt-4.1-mini",
    "continuity_router": "gpt-4.1-mini",
    "answer_first_guard": "gpt-4.1-mini",
    # answering + chips -> 5.4
    "book_answerer": "gpt-5.4-mini",
    "response_surface": "gpt-5.4-mini",
    "next_suggestions": "gpt-5.4-mini",
}

# Optional per-role env override -> the settings attribute holding it. Non-empty
# wins over the baked-in default, so a single role can be retuned from .env.
_ROLE_ENV_SETTINGS: dict[str, str] = {
    "main_router": "chat_llm_main_router_model",
    "service_router": "chat_llm_service_router_model",
    "continuity_router": "chat_llm_continuity_router_model",
    "answer_first_guard": "chat_llm_answer_first_guard_model",
    "book_answerer": "chat_llm_book_answerer_model",
    "response_surface": "chat_llm_response_surface_model",
    "next_suggestions": "chat_llm_next_suggestions_model",
}


def multi_model_enabled() -> bool:
    """True when each role runs on its own model (and the harness dropdown is off)."""
    try:
        return bool(getattr(settings, "multi_model_enabled", True))
    except Exception:
        return False


def _role_env_model(role: str) -> str:
    attr = _ROLE_ENV_SETTINGS.get(role, "")
    if not attr:
        return ""
    return str(getattr(settings, attr, "") or "").strip()


def resolve_role_model(role: str) -> str:
    """The chat model a given orchestration role runs on.

    MULTI-MODEL ON:  pin > per-role env > ROLE_MODEL_DEFAULTS > env default.
                     The harness override is deliberately SKIPPED — the dropdown
                     is disabled in this mode.
    MULTI-MODEL OFF: pin > harness override > per-role env > env default
                     (the historical single-model behaviour).

    Callers pass this result to BOTH the LLM call and prompt_registry.get(model=)
    so 'model called' and 'prompt loaded' stay in lockstep per role."""
    r = str(role or "").strip()
    pin = get_pinned_model()
    if pin:
        return pin
    if not multi_model_enabled():
        override = get_model_override()
        if override:
            return override
    env_model = _role_env_model(r)
    if env_model:
        return env_model
    return ROLE_MODEL_DEFAULTS.get(r, "") or default_model()


def set_model_override(model: str | None) -> str:
    """Set (or clear, when falsy) the override. Returns the stored value.

    Raises ValueError when `model` is non-empty and not in the allowlist.
    """
    value = str(model or "").strip()
    if value and value not in _ALLOWED_IDS:
        raise ValueError(f"unsupported model: {value}")
    with _lock:
        _state["model"] = value
        _persist()
    logger.info("model_override_set model=%r", value or "(cleared)")
    return value


_load()
