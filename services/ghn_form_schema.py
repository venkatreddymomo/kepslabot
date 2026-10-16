"""
Translate OUR service forms into the shape GHN/Lumira expects.

Service forms live in ``new_bot_services.form_config`` and stay there — that is
the source of truth, because it is the shape the rest of the bot reasons about
(trigger_field, pre_form_instructions, validation prompts fed to the LLM). This
module never writes anything. It only converts on the way OUT, when a payload
has to leave our system: an external API call, a notification, a dashboard row.

Our shape                          GHN shape
---------                          ---------
{id, label, type, required,        {id, type, label, order, required, fieldType,
 validation_prompt, options}        validation, placeholder,
                                    textValidationType | numberValidationType}

The important addition is ``fieldType`` — the semantic identity of a field.
Everything downstream (which value is the phone, which is the email) keys off
it, because ``label`` is free text an operator can rename at any time.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from services.lead_form_service import slugify_label

logger = logging.getLogger(__name__)

# Semantic type inferred from the field ID. Ordered: the first match wins, so
# "alternate_phone" resolves before "phone".
#
# Matching is on whole underscore-separated TOKENS, never substrings: a field
# labelled "Dish names with quantity" must not become FULL_NAME because the word
# "names" contains "name". A wrong fieldType is worse than CUSTOM — downstream
# code trusts it to say which value is the guest's phone.
_FIELD_TYPE_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "ALTERNATE_MOBILE_NUMBER",
        ("alternate_phone", "alt_phone", "alternate_mobile", "alternate_number"),
    ),
    ("FULL_NAME", ("full_name", "fullname", "guest_name", "your_name", "name")),
    ("EMAIL", ("email", "e_mail", "email_id")),
    ("MOBILE_NUMBER", ("phone", "phone_number", "mobile", "mobile_number", "contact_number", "whatsapp")),
    ("CITY", ("city", "location", "destination")),
)


def _token_match(haystack: str, pattern: str) -> bool:
    """True when `pattern` appears as whole token(s) in an underscore id.

    "full_name" matches "full_name" and "guest_full_name"; "name" matches "name"
    but NOT "names" or "dish_name_list".
    """
    tokens = [t for t in haystack.split("_") if t]
    parts = [p for p in pattern.split("_") if p]
    if not parts or len(parts) > len(tokens):
        return False
    for i in range(len(tokens) - len(parts) + 1):
        if tokens[i : i + len(parts)] == parts:
            return True
    return False

# Input type -> the validation hint GHN carries alongside it.
_TEXT_VALIDATION = {"text": "PLAIN", "textarea": "PLAIN", "email": "EMAIL"}
_NUMBER_VALIDATION = {"tel": "PHONE", "number": "NUMBER"}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def infer_field_type(field: dict[str, Any]) -> str:
    """Best guess at the semantic type of one of our fields.

    Reads the id first (ours are stable, snake_case and descriptive:
    ``full_name``, ``phone``, ``checkin_date``), then the label, then falls back
    to the input type. Anything unrecognised is CUSTOM, which is correct rather
    than a failure — most service-form fields genuinely are custom.
    """
    explicit = _clean(field.get("fieldType")).upper()
    if explicit:
        return explicit

    # The id is authoritative: ours are stable and descriptive (`full_name`,
    # `phone`, `checkin_date`). The label is prose an operator writes for the
    # guest and is far more likely to contain a misleading word.
    field_id = slugify_label(_clean(field.get("id")) or _clean(field.get("label")))
    for field_type, patterns in _FIELD_TYPE_PATTERNS:
        if any(_token_match(field_id, pattern) for pattern in patterns):
            return field_type

    ftype = _clean(field.get("type")).lower()
    if ftype == "email":
        return "EMAIL"
    if ftype == "tel":
        return "MOBILE_NUMBER"
    return "CUSTOM"


def to_ghn_field(field: dict[str, Any], index: int) -> Optional[dict[str, Any]]:
    """One of our service fields -> one GHN field. None if unusable."""
    if not isinstance(field, dict):
        return None
    label = _clean(field.get("label"))
    if not label:
        return None

    input_type = _clean(field.get("type")) or "text"
    out: dict[str, Any] = {
        "id": _clean(field.get("id")) or slugify_label(label),
        "type": input_type,
        "label": label,
        "order": index + 1,
        "required": bool(field.get("required", False)),
        "fieldType": infer_field_type(field),
        "placeholder": _clean(field.get("placeholder")) or f"Enter {label.lower()}",
    }

    # `validation_prompt` is guidance we feed the LLM when re-asking for a bad
    # answer. GHN's equivalent slot is the message shown under the input.
    prompt = _clean(field.get("validation_prompt"))
    out["validation"] = {"message": prompt} if prompt else {}

    if input_type in _NUMBER_VALIDATION:
        out["numberValidationType"] = _NUMBER_VALIDATION[input_type]
    elif input_type in _TEXT_VALIDATION:
        out["textValidationType"] = _TEXT_VALIDATION[input_type]

    if isinstance(field.get("options"), list):
        options = [_clean(o) for o in field["options"] if _clean(o)]
        if options:
            out["options"] = options
    return out


def to_ghn_fields(form_config: dict[str, Any]) -> list[dict[str, Any]]:
    """Every field of one service's form_config, in GHN shape."""
    if not isinstance(form_config, dict):
        return []
    raw = form_config.get("fields")
    if not isinstance(raw, list):
        return []
    converted = [to_ghn_field(f, i) for i, f in enumerate(raw)]
    return [f for f in converted if f]


def to_ghn_content_json(
    form_config: dict[str, Any],
    *,
    service_name: str = "",
) -> dict[str, Any]:
    """A service's form as a GHN ``Content-JSON`` body.

    Mirrors what GHN's own forms store: ``fields`` plus ``nextActions``. We do
    NOT emit their internal extras (form_id, type:"form") — their live CI form
    does not carry those either.
    """
    body: dict[str, Any] = {"fields": to_ghn_fields(form_config)}
    if _clean(service_name):
        body["title"] = _clean(service_name)
    body["nextActions"] = [{"type": "cta_button", "order": 1, "value": "ALL_CTAS"}]
    return body


def to_submission_payload(
    form_config: dict[str, Any],
    answers: dict[str, Any],
    *,
    session_id: str,
    form_key: str,
    entity_id: Any = None,
    bot_id: Any = None,
    widget_id: Any = None,
    message: str = "",
) -> dict[str, Any]:
    """A completed service form as the outbound ``form_submit`` payload.

    Field values are merged by id first, then by the semantic fieldType, then by
    label — the same rule the lead-form recorder uses, so a renamed label never
    loses an answer.
    """
    from services.lead_form_results_service import build_submitted_fields

    fields = build_submitted_fields(to_ghn_fields(form_config), answers or {})
    payload: dict[str, Any] = {
        "message": message or "",
        "session_id": _clean(session_id),
        "type": "form_submit",
        "form_key": _clean(form_key),
        "fields": fields,
    }
    # Routing ids are optional: they are only meaningful to the receiving side,
    # and not every hotel has all three mapped (Mango has no widget_id yet).
    if entity_id not in (None, ""):
        payload["entity_id"] = entity_id
    if bot_id not in (None, ""):
        payload["Bot_Id"] = str(bot_id)
    if widget_id not in (None, ""):
        payload["widget_id"] = str(widget_id)
    return payload
