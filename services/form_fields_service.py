"""
Form Fields Service

Extracts structured form field definitions from a service's ticketing_conditions.

Primary method: regex parsing (fast, no API call, always works).
Enhancement: LLM pass to improve labels/types when regex produces rough results.

Results are cached in memory by service_id for the lifetime of the process.
New services automatically get forms the first time they trigger a collect_info turn.
"""

import json
import logging
import re
from typing import Any

from config.settings import settings
from llm.client import llm_client

logger = logging.getLogger(__name__)

# In-memory cache: service_id -> list of field dicts
_form_fields_cache: dict[str, list[dict[str, Any]]] = {}

# ── Type heuristics ────────────────────────────────────────────────────────────

_DATE_WORDS   = {"date", "check-in", "check-out", "checkin", "checkout",
                 "arrival", "departure", "start", "end", "check in", "check out"}
_TIME_WORDS   = {"time", "appointment time", "hour"}
_TEL_WORDS    = {"phone", "mobile", "contact number", "telephone", "phone number",
                 "contact information", "contact info", "contact"}
_NUMBER_WORDS = {"number of guests", "guests", "no. of guests", "count",
                 "quantity", "pax", "persons", "people"}
_EMAIL_WORDS  = {"email", "email address", "e-mail"}
_AREA_WORDS   = {"description", "issue", "notes", "details", "message",
                 "brief description", "reason", "comment"}


def _infer_type(raw: str) -> str:
    low = raw.lower().strip()
    if any(w in low for w in _DATE_WORDS):
        return "date"
    if any(w in low for w in _TIME_WORDS):
        return "time"
    if any(w in low for w in _NUMBER_WORDS):
        return "number"
    if any(w in low for w in _EMAIL_WORDS):
        return "email"
    if any(w in low for w in _TEL_WORDS):
        return "tel"
    if any(w in low for w in _AREA_WORDS):
        return "textarea"
    return "text"


def _to_label(raw: str) -> str:
    """Convert raw field phrase to a title-case label."""
    cleaned = re.sub(r"\b(desired|preferred|specific|their|the guest.?s?|a brief)\b", "", raw, flags=re.I)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.")
    return cleaned.title() if cleaned else raw.title()


def _to_id(label: str) -> str:
    """Convert label to a snake_case id."""
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")


# ── Regex extraction ───────────────────────────────────────────────────────────

# Patterns to find the enumerated fields inside a ticketing_conditions sentence.
_FIELD_LIST_PATTERNS = [
    r"by providing (?:their |the (?:guest(?:'s)? ))?(.+?)(?:\.|$)",
    r"ensuring (?:the )?(?:guest(?:'s)? )?(.+?)(?:are|is) collected",
    r"(?:collecting|collect|collected) (?:the )?(?:following )?(?:details?|information|fields?)?[:\s]+(.+?)(?:\.|$)",
    r"after (?:the )?(?:guest )?(?:provides?|confirming|submitting) (.+?)(?:\.|$)",
    r"(?:requires?|requiring|must include)[:\s]+(.+?)(?:\.|$)",
]


def _regex_extract(ticketing_conditions: str) -> list[dict[str, Any]]:
    """
    Parse ticketing_conditions text with regex and return a list of field dicts.
    Returns an empty list if the text doesn't match any known pattern.
    """
    for pattern in _FIELD_LIST_PATTERNS:
        m = re.search(pattern, ticketing_conditions, re.IGNORECASE | re.DOTALL)
        if not m:
            continue

        raw_list = m.group(1).strip()

        # Split on commas / semicolons / " and "
        parts = re.split(r",\s*(?:and\s+)?|;\s*(?:and\s+)?|\s+and\s+", raw_list)

        fields: list[dict[str, Any]] = []
        for part in parts:
            part = part.strip(" .,;")
            # Remove parenthetical clarifications like "(phone number and email)"
            part = re.sub(r"\s*\(.*?\)", "", part).strip()
            if not part or len(part) < 2:
                continue

            label = _to_label(part)
            if not label:
                continue

            field_id = _to_id(label)
            field_type = _infer_type(part)

            fields.append({
                "id": field_id,
                "label": label,
                "type": field_type,
                "required": True,
            })

        if fields:
            return fields

    return []


# ── LLM extraction (enhancement, not primary) ─────────────────────────────────

_EXTRACT_SYSTEM_PROMPT = (
    "You are a form schema extractor. Given a ticketing condition description, "
    "extract the required fields as a JSON array.\n\n"
    "Each field object must have exactly these keys:\n"
    '  "id"       : snake_case identifier (e.g. "full_name", "checkin_date")\n'
    '  "label"    : human-readable label (e.g. "Full Name", "Check-in Date")\n'
    '  "type"     : one of "text", "date", "time", "number", "tel", "email", "textarea"\n'
    '  "required" : true or false\n\n'
    "Type assignment rules:\n"
    '- check-in, check-out, date, arrival, departure -> "date"\n'
    '- time, appointment time -> "time"\n'
    '- phone, contact, mobile -> "tel"\n'
    '- number of guests, count -> "number"\n'
    '- email -> "email"\n'
    '- description, issue, notes, details -> "textarea"\n'
    "- everything else -> \"text\"\n\n"
    "Return ONLY a valid JSON array, no markdown fences, no explanation."
)


async def _llm_extract(ticketing_conditions: str) -> list[dict[str, Any]]:
    """Attempt LLM-based extraction. Returns [] on any failure."""
    try:
        model = str(getattr(settings, "openai_model", None) or "gpt-4o-mini")

        completion = await llm_client.raw_chat_completion(
            model=model,
            messages=[
                {"role": "system", "content": _EXTRACT_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"Extract form fields from this ticketing condition:\n{ticketing_conditions}"
                    ),
                },
            ],
            temperature=0,
            max_tokens=600,
            trace_context={
                "component": "form_fields_extraction",
                "service_id": "form_fields_service",
            },
            purpose="Extract structured form fields from ticketing conditions",
        )

        raw = str(completion.choices[0].message.content or "").strip()
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1] if len(parts) > 1 else raw
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            return []

        validated: list[dict[str, Any]] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            fid   = str(item.get("id") or "").strip()
            label = str(item.get("label") or "").strip()
            ftype = str(item.get("type") or "text").strip()
            req   = bool(item.get("required", True))
            if not fid or not label:
                continue
            if ftype not in {"text", "date", "time", "number", "tel", "email", "textarea"}:
                ftype = "text"
            validated.append({"id": fid, "label": label, "type": ftype, "required": req})

        return validated

    except Exception as exc:
        logger.debug("form_fields_service: LLM extraction skipped: %s", exc)
        return []


# ── Public API ─────────────────────────────────────────────────────────────────

async def extract_form_fields(service: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Extract form fields from a service.

    If the service has ticketing_mode=form and form_config.fields defined,
    those are used directly (admin-configured). Otherwise falls back to
    regex/LLM extraction from ticketing_conditions text.

    Results are cached by service_id; LLM is called at most once per service
    per server restart.
    """
    service_id = str(service.get("id") or "").strip().lower()
    if not service_id:
        return []

    if service_id in _form_fields_cache:
        return _form_fields_cache[service_id]

    # ── Fast path: admin-configured form_config fields ────────────────────
    ticketing_mode = str(service.get("ticketing_mode") or "").strip().lower()
    form_config = service.get("form_config")
    if ticketing_mode == "form" and isinstance(form_config, dict):
        configured_fields = form_config.get("fields")
        if isinstance(configured_fields, list) and configured_fields:
            fields = []
            for f in configured_fields:
                if not isinstance(f, dict):
                    continue
                fid = str(f.get("id") or "").strip()
                if not fid:
                    continue
                entry: dict[str, Any] = {
                    "id": fid,
                    "label": str(f.get("label") or fid).strip(),
                    "type": str(f.get("type") or "text").strip(),
                    "required": bool(f.get("required", True)),
                }
                # Carry through optional per-field metadata so admin-supplied
                # validation prompts, options, prefill sources and deterministic
                # date/time slot constraints survive extraction and reach the widget.
                for optional_key in (
                    "description", "placeholder", "options", "validation_prompt",
                    "prefill_source", "default_value",
                    "slot_source_field", "slot_hours_map", "slot_hours", "slot_step_minutes",
                ):
                    if optional_key in f and f.get(optional_key) not in (None, ""):
                        entry[optional_key] = f.get(optional_key)
                fields.append(entry)
            if fields:
                logger.info(
                    "form_fields_service: using %d admin-configured field(s) for '%s'",
                    len(fields), service_id,
                )
                _form_fields_cache[service_id] = fields
                return fields

    # ── Fallback: regex + LLM extraction from ticketing_conditions ────────
    prompt_pack = service.get("service_prompt_pack") or {}
    ticketing_conditions = str(
        service.get("ticketing_conditions")
        or (prompt_pack.get("ticketing_conditions") if isinstance(prompt_pack, dict) else "")
        or service.get("ticketing_policy")
        or ""
    ).strip()

    if not ticketing_conditions:
        logger.debug("form_fields_service: no ticketing_conditions for '%s'", service_id)
        _form_fields_cache[service_id] = []
        return []

    # Step 1 — regex
    regex_fields = _regex_extract(ticketing_conditions)
    logger.info(
        "form_fields_service: regex extracted %d field(s) for '%s'",
        len(regex_fields), service_id,
    )

    # Step 2 — if regex gave enough fields, we're done
    if len(regex_fields) >= 2:
        _form_fields_cache[service_id] = regex_fields
        return regex_fields

    # Step 3 — try LLM for a richer result
    llm_fields = await _llm_extract(ticketing_conditions)
    if llm_fields:
        logger.info(
            "form_fields_service: LLM extracted %d field(s) for '%s'",
            len(llm_fields), service_id,
        )
        _form_fields_cache[service_id] = llm_fields
        return llm_fields

    # Step 4 — fall back to regex even if it returned only 1 field
    _form_fields_cache[service_id] = regex_fields
    return regex_fields


def invalidate_cache(service_id: str | None = None) -> None:
    """
    Invalidate the cached form fields for one service or for all services.
    Call when a service's ticketing_conditions are updated.
    """
    global _form_fields_cache
    if service_id:
        _form_fields_cache.pop(str(service_id).strip().lower(), None)
    else:
        _form_fields_cache.clear()


# ── Prefill (additive, opt-in per call) ───────────────────────────────────────
#
# Form fields can be pre-filled at the moment the form is shown so the guest
# does not retype information the bot already knows. Two sources:
#
#   1. Deterministic mapping from request metadata + conversation context:
#      name → guest_name, phone → guest_phone, room → room_number, etc.
#      No LLM call. Always safe.
#
#   2. Free-text fields (textarea-typed "issue/description/notes"): one cheap
#      LLM pass extracts a concise value from the user's triggering message.
#      Only fires when allow_llm=True AND such a field exists AND there is
#      enough text in the triggering message to be worth extracting.
#
# In both cases the produced value lands on `field["default_value"]` and the
# field stays editable on the frontend. `field["prefill_source"]` is set so the
# UI can show a small "auto-filled — edit if needed" badge.
#
# This is purely additive: when nothing matches, the field dict is unchanged.

_DETERMINISTIC_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "name": (
        "name", "full_name", "fullname", "guest_name", "customer_name",
        "your_name", "first_name",
    ),
    "phone": (
        "phone", "phone_number", "mobile", "mobile_number", "contact",
        "contact_number", "contact_info", "telephone", "guest_phone",
    ),
    "email": (
        "email", "email_address", "guest_email", "your_email",
    ),
    "room": (
        "room", "room_number", "room_no", "room_num", "roomnumber",
    ),
    "property": (
        "property", "property_name", "hotel", "hotel_name", "location",
    ),
}

_TEXTAREA_FREEFORM_HINTS: tuple[str, ...] = (
    "issue", "issue_details", "description", "details", "notes", "message",
    "reason", "comment", "complaint", "concern", "feedback", "request_details",
    "special_requests", "additional_info", "remarks",
)

_PREFILL_EXTRACT_SYSTEM_PROMPT = (
    "You extract one short free-text value to pre-fill a form field for the guest.\n"
    "You are given the form field label, the BOT'S CURRENT REPLY (the message this form is attached "
    "to), a conversation excerpt (most-recent turns at the bottom), the guest's most recent message, "
    "and a brief service context. Return the guest's intent for that field, in their words, trimmed "
    "to a clean sentence or two.\n\n"
    "PRIORITY (read carefully, in order):\n"
    "1. The 'BOT'S CURRENT REPLY' is the single most reliable anchor. The form is being shown as part "
    "of THAT reply, and the bot usually states the exact topic in it (e.g. \"confirming your interest "
    "in booking the Dighashree Auditorium\"). Anchor the value to the specific topic named there, and "
    "carry its specifics (the named venue / room / service / request), not a generic paraphrase.\n"
    "2. If the bot's current reply does not name a clear topic, fall back to the IMMEDIATE prior turn — "
    "the bot offered handoff for a specific topic and the guest accepted, or the bot opened the form on "
    "the user's last specific ask. Anchor to THAT immediate topic.\n"
    "3. The 'CONVERSATION EXCERPT' shows older turns too. Older topics in the excerpt are CONTEXT, "
    "NOT the answer. Do NOT pick an earlier, distinctive-sounding topic just because it stands out. "
    "NEVER invent a topic (e.g. 'conference room', 'AC repair') that does not appear in the bot's "
    "current reply or the immediate prior turn.\n"
    "4. If the guest's most recent message is a short affirmation ('yes', 'sure', 'go ahead'), it is "
    "consenting to whatever the bot's current reply / immediately preceding bot turn proposed.\n"
    "5. If the bot's current reply names no specific topic (e.g. a generic \"please review the details "
    "below and submit\"), do NOT return empty — fall back to the most recent prior ASSISTANT turn in the "
    "excerpt that DID name a concrete topic/request and anchor to THAT. Only return an empty value when "
    "neither the current reply nor any recent assistant turn names a usable topic. Never invent one.\n\n"
    "6. When 'PROPERTY THIS REQUEST IS FOR' is given, that is the ONLY property the value may "
    "name. The excerpt often still mentions a property the guest has since moved away from — "
    "never carry that one into the value, and never name two. If the value would name a "
    "different property than the one given, use the given one instead.\n\n"
    'Return STRICT JSON only: {"value": "..."} — no markdown fences. Do NOT add greetings, prefaces, '
    "or extra prose in the value (no \"Thank you for confirming\" — just the guest's request)."
)


def _active_property_for_prefill(context: Any) -> str:
    """The property this ticket is actually being raised for.

    The prefill reads the recent chat, and in a multi-property session that
    excerpt still holds the property the guest was on BEFORE they switched — so
    a request that correctly routed to Parra came back worded "...regarding
    Zone Connect by The Park, Calangute Goa". The routing already resolved the
    right property; handing the same answer to the prefill keeps the wording on
    the ticket and the property of the ticket from disagreeing.
    """
    try:
        pending = getattr(context, "pending_data", None)
        if isinstance(pending, dict):
            for key in ("_selected_property_scope_name", "selected_property_scope_name",
                        "_stay_property_name"):
                name = str(pending.get(key) or "").strip()
                if name:
                    return name
        return str(getattr(context, "booking_property_name", "") or "").strip()
    except Exception:
        return ""


def _normalize_field_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def _looks_like_freeform_textarea(field: dict[str, Any]) -> bool:
    ftype = str(field.get("type") or "").strip().lower()
    if ftype != "textarea":
        return False
    blob = f"{field.get('id') or ''} {field.get('label') or ''}".lower()
    return any(hint in blob for hint in _TEXTAREA_FREEFORM_HINTS)


def _looks_like_travel_point(field: dict[str, Any]) -> bool:
    """True for a pickup/drop/arrival point the GUEST supplies (airport, railway
    station, address they chose) — not the hotel's own property/location."""
    blob = f"{field.get('id') or ''} {field.get('label') or ''}".lower()
    return any(
        w in blob
        for w in ("pickup", "pick up", "pick-up", "dropoff", "drop off", "drop-off",
                  "drop ", "arrival", "departure")
    )


def _match_deterministic_alias(field: dict[str, Any]) -> str:
    """Return the deterministic category key (e.g. 'name') for this field, or ''."""
    fid = _normalize_field_key(field.get("id"))
    flabel = _normalize_field_key(field.get("label"))
    candidates = {fid, flabel}
    candidates.discard("")
    # A pickup/drop/arrival point is the guest's OWN travel location (airport,
    # railway station, address), never the hotel's property name — so never let
    # such a field match the 'property' category (which would auto-fill the
    # hotel name into "Pickup Location"). Name/phone/room still match normally.
    block_property = _looks_like_travel_point(field)
    for category, aliases in _DETERMINISTIC_FIELD_ALIASES.items():
        if category == "property" and block_property:
            continue
        alias_set = {_normalize_field_key(a) for a in aliases}
        if candidates & alias_set:
            return category
        for candidate in candidates:
            if any(candidate.endswith(f"_{a}") or candidate.startswith(f"{a}_") for a in alias_set):
                return category
    return ""


def _collect_prefill_sources(
    *,
    req_meta: dict[str, Any] | None,
    context: Any,
) -> dict[str, str]:
    """Pull deterministic guest/room/property values from metadata + context."""
    sources: dict[str, str] = {}

    def _set(category: str, raw: Any) -> None:
        text = str(raw or "").strip()
        if text and category not in sources:
            sources[category] = text

    safe_meta = req_meta if isinstance(req_meta, dict) else {}
    # Request metadata (set by widget loader / embed)
    _set("name", safe_meta.get("guest_name"))
    _set("phone", safe_meta.get("guest_phone"))
    _set("email", safe_meta.get("guest_email"))
    _set("room", safe_meta.get("room_number"))
    _set("property", safe_meta.get("booking_property_name"))
    # Booking-scoped fallbacks: a guest identified by their booking (rather than
    # a lead form) carries name/phone/room under booking_* keys. Without these
    # only lead-form guests get those fields prefilled; a checked-in / reserved
    # guest would have to retype their own name and phone. `_set` keeps the
    # un-prefixed values above as higher priority.
    _set("name", safe_meta.get("booking_guest_name"))
    _set("phone", safe_meta.get("booking_guest_phone"))
    _set("email", safe_meta.get("booking_guest_email"))
    _set("room", safe_meta.get("booking_room_number"))

    # Conversation context fallbacks
    if context is not None:
        _set("name", getattr(context, "guest_name", None))
        _set("phone", getattr(context, "guest_phone", None))
        _set("email", getattr(context, "guest_email", None))
        _set("room", getattr(context, "room_number", None))
        _set("property", getattr(context, "booking_property_name", None))
        pending = getattr(context, "pending_data", None)
        if isinstance(pending, dict):
            _set("name", pending.get("guest_name"))
            _set("phone", pending.get("guest_phone"))
            _set("email", pending.get("guest_email"))
            _set("room", pending.get("room_number"))
            _set("property", pending.get("_selected_property_scope_name"))
            _set("property", pending.get("_stay_property_name"))

    return sources


async def _extract_freeform_with_llm(
    *,
    field: dict[str, Any],
    triggering_user_message: str,
    service: dict[str, Any] | None,
    conversation_excerpt: str = "",
    bot_response_text: str = "",
    active_property_name: str = "",
) -> str:
    """Ask a cheap LLM pass for one short value for a freeform textarea field.

    `conversation_excerpt` is an optional rolled-up view of the recent chat
    (last few turns + any summary). When the triggering user message alone is
    too thin to extract intent (e.g. literal "yes please"), the excerpt gives
    the LLM enough context to pre-fill a meaningful value.

    `bot_response_text` is the bot's CURRENT reply — the message this form is
    attached to. It is the strongest anchor for the guest's intent because the
    bot almost always states the exact topic there (e.g. "confirming your
    interest in booking the Dighashree Auditorium"). Passing it both lets the
    model produce a specific, on-topic value and lets the hallucination guard
    below verify the value against what the bot actually said this turn.
    """
    try:
        model = str(getattr(settings, "openai_model", None) or "gpt-4o-mini")
        service_label = str((service or {}).get("name") or (service or {}).get("id") or "").strip()
        service_desc = str((service or {}).get("description") or "").strip()

        excerpt_block = ""
        excerpt_clean = re.sub(r"\s+", " ", str(conversation_excerpt or "").strip())
        if excerpt_clean:
            # Cap the excerpt so the prefill call stays cheap. The most recent
            # turns are most predictive; we just include the tail.
            if len(excerpt_clean) > 1500:
                excerpt_clean = excerpt_clean[-1500:]
            excerpt_block = f"CONVERSATION EXCERPT (most recent turns; use to infer intent if the GUEST MESSAGE is too thin):\n{excerpt_clean}\n\n"

        # The bot's current reply is the highest-signal anchor — the form is
        # being shown as part of it and it usually names the exact topic.
        current_reply_block = ""
        current_reply_clean = re.sub(r"\s+", " ", str(bot_response_text or "").strip())
        if current_reply_clean:
            if len(current_reply_clean) > 800:
                current_reply_clean = current_reply_clean[:800]
            current_reply_block = (
                "BOT'S CURRENT REPLY (the form is attached to THIS message — anchor the value to the "
                f"specific topic it names):\n{current_reply_clean}\n\n"
            )

        # The property the request is being filed against. The excerpt can still
        # name an earlier one the guest has moved on from, so state the resolved
        # property explicitly and let the prompt forbid substituting another.
        property_block = ""
        property_clean = re.sub(r"\s+", " ", str(active_property_name or "").strip())
        if property_clean:
            property_block = (
                "PROPERTY THIS REQUEST IS FOR (authoritative — if the value names a property "
                f"at all it MUST be this one):\n{property_clean}\n\n"
            )

        user_content = (
            f"FIELD LABEL: {field.get('label') or field.get('id')}\n"
            f"SERVICE: {service_label}\n"
            f"SERVICE CONTEXT: {service_desc}\n"
            f"{property_block}"
            f"{current_reply_block}"
            f"{excerpt_block}"
            f"GUEST MESSAGE (most recent user turn): {triggering_user_message.strip()}\n\n"
            'Return JSON: {"value": "..."}'
        )


        completion = await llm_client.raw_chat_completion(
            model=model,
            messages=[
                {"role": "system", "content": _PREFILL_EXTRACT_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            temperature=0,
            max_tokens=200,
            trace_context={
                "component": "form_fields_prefill",
                "service_id": str((service or {}).get("id") or ""),
            },
            purpose="Pre-fill freeform form field from triggering user message",
        )

        raw = str(completion.choices[0].message.content or "").strip()
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1] if len(parts) > 1 else raw
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            return ""
        value = str(parsed.get("value") or "").strip()
        # Don't echo trivial / off-topic content.
        if len(value) < 3:
            return ""

        # HALLUCINATION GUARD: the prefill model (gpt-4o-mini) has a strong
        # training-data bias that produces stock templates like "I need
        # assistance with booking a conference room" whenever it sees a
        # Support Request + Issue Details textarea + short trigger message
        # ("yes pls"), regardless of what the bot's prior turn was actually
        # about. To suppress this we require the prefilled value to share at
        # least one significant content token with what the bot ACTUALLY said
        # this turn — its current reply (highest signal) plus the most recent
        # ASSISTANT line in `conversation_excerpt`. If there's zero overlap,
        # the value is drifting to an unrelated topic and is dropped (field
        # stays blank instead of showing a wrong template).
        try:
            tail_assistant = ""
            for line in reversed(str(conversation_excerpt or "").splitlines()):
                line = line.strip()
                if line.upper().startswith("ASSISTANT:"):
                    tail_assistant = line[len("ASSISTANT:"):].strip()
                    break
            # The bot's current reply is the primary anchor; the prior assistant
            # line is a secondary anchor for affirmation turns where the current
            # reply is a bare "Please fill in the details below."
            anchor_text = f"{current_reply_clean} {tail_assistant}".strip()
            if anchor_text:
                # Common stopwords + common hotel-domain "filler" nouns that
                # aren't distinctive enough to anchor on (a generic mention of
                # "room" or "booking" doesn't tell us the value is on-topic).
                _stop = {
                    "the","a","an","and","or","but","of","to","at","in","on","for","with",
                    "this","that","these","those","is","are","was","were","be","been","being",
                    "have","has","had","do","does","did","will","would","could","should","may",
                    "might","can","you","your","our","we","us","i","me","my","they","them",
                    "their","it","its","into","from","by","as","if","so","not","no","yes",
                    "any","some","all","each","every","also","just","please","like","need",
                    "want","help","assist","assistance","know","tell","share","let","kindly",
                    "provide","details","information","info","more","about","get","also",
                    # generic hotel-domain nouns — too weak as on-topic anchors
                    "room","rooms","hotel","stay","guest","guests","book","booking","price",
                    "rate","cost","check","date","dates","time","made","made","make",
                }
                def _stem(w: str) -> str:
                    # Light stemming so "event"/"events", "wedding"/"weddings",
                    # "book"/"booking" count as the same anchor token.
                    for suf in ("ing", "ies", "es", "ed", "s"):
                        if len(w) > len(suf) + 2 and w.endswith(suf):
                            return w[: -len(suf)]
                    return w
                def _toks(s: str) -> set[str]:
                    return {
                        _stem(w)
                        for w in re.findall(r"[a-z]{4,}", str(s or "").lower())
                        if w not in _stop
                    }
                vt = _toks(value)
                bt = _toks(anchor_text)
                # Require at least ONE distinctive (non-generic) content
                # token from the prefilled value to appear in what the bot
                # actually said this turn. Generic words like "room" /
                # "booking" / "stay" no longer count as anchors — they're too
                # easy for a hallucinated template to share by accident.
                if vt and bt and not (vt & bt):
                    logger.warning(
                        "form_fields_service: prefill hallucination dropped — value=%r has no distinctive token overlap with bot's reply",
                        value[:80],
                    )
                    return ""
        except Exception:
            pass

        return value
    except Exception as exc:
        logger.debug("form_fields_service: prefill LLM extraction skipped: %s", exc)
        return ""


async def apply_prefill(
    fields: list[dict[str, Any]],
    *,
    req_meta: dict[str, Any] | None,
    context: Any,
    triggering_user_message: str = "",
    service: dict[str, Any] | None = None,
    allow_llm: bool = True,
    conversation_excerpt: str = "",
    bot_response_text: str = "",
) -> list[dict[str, Any]]:
    # Critical: `fields` comes from `extract_form_fields` which RETURNS THE
    # CACHED LIST BY REFERENCE. If a prior turn wrote `default_value` onto a
    # cached field dict, that value lingers into every subsequent turn — the
    # logic below short-circuits on "if field.get('default_value'): continue"
    # and the new turn's intent never overwrites the stale prefill.
    #
    # Strip the prefill-related keys from any cached field before computing
    # this turn's prefill. This keeps the cache valid (it's still the same
    # schema rows) while guaranteeing each call starts with a clean slate.
    if isinstance(fields, list):
        for _f in fields:
            if isinstance(_f, dict):
                _f.pop("default_value", None)
                _f.pop("prefill_source", None)
    """Mutate `fields` in place to add `default_value` / `prefill_source` where safe.

    Always returns the same list. When no prefill source matches, fields are
    unchanged — the frontend then renders blank, identical to today's behavior.
    """
    if not isinstance(fields, list) or not fields:
        return fields

    deterministic_sources = _collect_prefill_sources(req_meta=req_meta, context=context)

    # 1) Deterministic pass — no LLM
    for field in fields:
        if not isinstance(field, dict):
            continue
        if field.get("default_value"):
            continue
        category = _match_deterministic_alias(field)
        if not category:
            continue
        value = deterministic_sources.get(category, "")
        if not value:
            continue
        field["default_value"] = value
        field["prefill_source"] = "session_metadata"

    # 2) Freeform pass — single LLM call per matching field. Covers freeform
    #    textareas (issue/description/notes) AND pickup/drop location fields, so
    #    a "Pickup Location" is filled with the airport / railway / point the
    #    guest actually named in chat, not left blank or wrongly set to the hotel.
    if allow_llm and triggering_user_message and triggering_user_message.strip():
        for field in fields:
            if not isinstance(field, dict):
                continue
            if field.get("default_value"):
                continue
            if not (_looks_like_freeform_textarea(field) or _looks_like_travel_point(field)):
                continue
            value = await _extract_freeform_with_llm(
                field=field,
                triggering_user_message=triggering_user_message,
                service=service,
                conversation_excerpt=conversation_excerpt,
                bot_response_text=bot_response_text,
                active_property_name=_active_property_for_prefill(context),
            )
            if value:
                field["default_value"] = value
                field["prefill_source"] = "llm_extracted"

    return fields
