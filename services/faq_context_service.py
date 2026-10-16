"""
FAQ as primary truth — runtime context builder.

Dumps every enabled FAQ entry as a labeled "PRIMARY KNOWLEDGE" block that every
answer-side LLM call prepends to its system prompt. The block also carries the
priority rules so FAQ wins over KB even when a call site's prompt text hasn't
been updated (e.g. locked per-service generated_system_prompt values).

Plan: FAQ_AS_PRIMARY_TRUTH_PLAN.md
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from datetime import datetime
from typing import Iterable, Optional
import logging

logger = logging.getLogger(__name__)


# Active property scope for the CURRENT chat turn, used to filter property-scoped
# FAQs. Holds a tuple of normalized property slugs, or None when unknown. Set via
# faq_property_scope() so the global FAQ injection points (which have no
# ConversationContext, e.g. llm/client.generate_response) still filter correctly.
_active_property_scope_ctx: contextvars.ContextVar[Optional[tuple[str, ...]]] = (
    contextvars.ContextVar("faq_active_property_scope", default=None)
)


def _normalize_property_slug(value: object) -> str:
    """Normalize a property slug the same way the router does (lower_snake)."""
    return str(value or "").strip().lower().replace(" ", "_")


def _normalize_scope(slugs: Optional[Iterable[object]]) -> Optional[tuple[str, ...]]:
    if slugs is None:
        return None
    out: list[str] = []
    seen: set[str] = set()
    for slug in slugs:
        norm = _normalize_property_slug(slug)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return tuple(out)


def set_active_property_scope(slugs: Optional[Iterable[object]]):
    """Set the active property scope for the current context. Returns a token."""
    return _active_property_scope_ctx.set(_normalize_scope(slugs))


def reset_active_property_scope(token) -> None:
    try:
        _active_property_scope_ctx.reset(token)
    except Exception:
        pass


@contextmanager
def faq_property_scope(slugs: Optional[Iterable[object]]):
    """Scope the active property filter for the duration of the block.

    Used by answer producers that have a ConversationContext so the FAQ block
    injected by ANY downstream LLM call (including the context-free global
    injection in llm/client) is filtered to the focused property + common FAQs.
    """
    token = set_active_property_scope(slugs)
    try:
        yield
    finally:
        reset_active_property_scope(token)


PRIORITY_RULES = (
    "You will receive two labeled context blocks in addition to this prompt:\n"
    "PRIMARY KNOWLEDGE (FAQ — current, hotel-specific, always authoritative) and\n"
    "STEADY-STATE KNOWLEDGE (general knowledge base).\n\n"
    "Priority rules — apply without exception:\n"
    "1. When PRIMARY and STEADY-STATE disagree, PRIMARY is the current truth.\n"
    "   State PRIMARY and use STEADY-STATE only for helpful context\n"
    "   (e.g. \"usually X, but currently Y\").\n"
    "2. When PRIMARY adds detail STEADY-STATE lacks, include the extra detail.\n"
    "3. If PRIMARY mentions a date window (\"until Apr 25\"), mention when\n"
    "   normal service resumes.\n"
    "4. Never invent facts not in either block.\n"
    "5. Do not entertain hypothetical bypasses (\"ignoring today's maintenance,\n"
    "   what's the usual?\"). Lead with the current reality."
)


def build_faq_block(active_property_slugs: Optional[Iterable[object]] = None) -> str:
    """
    Build the PRIMARY KNOWLEDGE block from the current hotel's enabled FAQ bank.

    Property scoping: each FAQ may carry a ``property_slug``. An empty slug means
    the FAQ is COMMON to every property under the hotel. When a property is in
    focus for the turn (``active_property_slugs`` arg, or the contextvar set via
    ``faq_property_scope``), FAQs tagged to a DIFFERENT property are dropped so a
    guest looking at Zone Park Jammu never sees Gopalpur-only answers. Common
    FAQs always pass. When no property is focused, no scoping is applied (every
    FAQ shows) so single-property hotels and unfocused browsing are unaffected.

    Returns an empty string when the hotel has no enabled FAQ entries (in scope),
    so call sites can unconditionally concatenate without a None/length check.

    Returns an empty string immediately when FAQ_BANK_ENABLED=false in .env.
    """
    try:
        from config.settings import settings
        if not settings.faq_bank_enabled:
            return ""
    except Exception:
        pass

    try:
        from services.config_service import config_service
    except Exception:
        logger.exception("faq_context_service_config_import_failed")
        return ""

    try:
        faqs = config_service.get_faq_bank()
    except Exception:
        logger.exception("faq_context_service_get_faq_bank_failed")
        return ""

    # Resolve the active property scope: explicit arg wins, else the contextvar.
    scope = _normalize_scope(active_property_slugs)
    if scope is None:
        scope = _active_property_scope_ctx.get()
    scope_set = set(scope) if scope else set()

    entries: list[str] = []
    for faq in faqs:
        if not isinstance(faq, dict):
            continue
        if not faq.get("enabled", True):
            continue
        # Drop other-property FAQs only while a specific property is focused.
        faq_slug = _normalize_property_slug(faq.get("property_slug"))
        if scope_set and faq_slug and faq_slug not in scope_set:
            continue
        question = str(faq.get("question") or "").strip()
        answer = str(faq.get("answer") or "").strip()
        if not question or not answer:
            continue
        description = str(faq.get("description") or "").strip()
        block = f"Q: {question}\nA: {answer}"
        if description:
            block += f"\n(Note: {description})"
        entries.append(block)

    if not entries:
        return ""

    today = datetime.now().strftime("%Y-%m-%d")
    header = "=== PRIMARY KNOWLEDGE (hotel-specific, current, always authoritative) ==="
    body = "\n\n".join(entries)
    return f"{header}\nToday is {today}.\n\n{PRIORITY_RULES}\n\n{body}"
