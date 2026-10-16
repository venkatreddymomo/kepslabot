"""
Per-hotel pre-booking settings, stored in the database and edited from the
admin portal (Bot Training -> Pre-booking).

Everything a pre-booking hotel customises lives in two new_bot_business_config
rows per hotel, so onboarding a hotel never needs a code change or an .env edit:

  json_section.prebooking   opening buttons and what follows the lead form,
                            buttons per service (open the service's form, or a
                            link - one per property when the hotel has several),
                            chips, form cancel button, photos muted per service,
                            reply style, outside recommendations
  json_section.ticketing    widget / group ids, allowed departments, and an FMS
                            entity id per property

The messages shown when a guest opens a location or taps a hotel live in
new_bot_location_intros (the table Zone's curated intros already use) and are
edited from the same admin tab.

A hotel with no row keeps the behaviour it had before these settings existed:
`get_section()` returns None and the caller falls back to its old .env / code
value. That is how Zone and Mango stay exactly as they are while T24 and every
later hotel are configured from the admin.

Property slugs are the keys of the per-property maps. Property names, picker
labels and aliases stay in new_bot_properties, where the bot already reads them;
`properties` in the prebooking section is only an index of those names, kept by
the admin routes so a reply that names a hotel finds that hotel's link.
"""

from __future__ import annotations

import copy
import json
import re
import time
from typing import Any, Iterable, Optional

from sqlalchemy import func, select

from models.database import AsyncSessionLocal, BusinessConfig, Hotel

SECTIONS = ("prebooking", "ticketing")

MAX_WELCOME_BUTTONS = 6
MAX_SERVICE_BUTTONS = 3
MAX_LABEL_CHARS = 40
MAX_MESSAGE_CHARS = 300
MAX_URL_CHARS = 500
MAX_DEPARTMENTS = 20

BULLET_CHOICES = ("•", "-")
EMOJI_CHOICES = ("none", "limited", "allowed")
# "message": the button sends its message as the guest's words.
# "intro":   the button shows the saved message for the hotel's location (for a
#            hotel with several properties in one place, the comparison with one
#            button per property). New visitors fill the lead form first.
WELCOME_ACTIONS = ("message", "intro")
# What the guest sees right after submitting the opening lead form.
AFTER_LEAD_FORM_CHOICES = ("intro", "buttons")
# "form": opens the service's own form; for room booking, the team-help form
#         that raises a ticket for the property in focus.
# "link": opens a link - the same one everywhere, or one per property.
SERVICE_BUTTON_ACTIONS = ("form", "link")

PRIORITY_CHOICES = ("LOW", "MEDIUM", "HIGH")

PREBOOKING_DEFAULTS: dict[str, Any] = {
    "welcome_buttons": [],
    "after_lead_form": "intro",
    "service_buttons": {},
    # Ticket priority per service ({service_id: LOW|MEDIUM|HIGH}). A service
    # with none keeps the default (pre-booking tickets are LOW).
    "service_priority": {},
    "chips_enabled": True,
    "form_cancel_button": True,
    "images_muted_services": [],
    "reply_style": {
        "bullet": "•",
        "emoji": "none",
        "no_trailing_offer": True,
        "enforce_language": False,
        "give_number_when_cannot_arrange": False,
    },
    "outside_recommendations": False,
    "properties": {},
    # {service_id: {name, row_id}} - an index kept by the admin routes.
    "services": {},
    # Menu PDFs, uploaded from the admin to S3: [{id, title, property_slug
    # ("" = every property), stored (s3:// pointer), file_name}]. Sent when a
    # guest asks for the menu; written by the menu upload routes alone.
    "menus": [],
    # KB book slugs (e.g. "dining_restaurants"). When one of these is the main
    # book the router chose for an answer, the menus go out without the guest
    # asking - each one once per conversation.
    "menu_topics": [],
}

MAX_MENUS = 12
MAX_MENU_TOPICS = 30

TICKETING_DEFAULTS: dict[str, Any] = {
    "widget_id": "",
    "group_id": "",
    "allowed_departments": [],
    "properties": {},
}

_DEFAULTS = {"prebooking": PREBOOKING_DEFAULTS, "ticketing": TICKETING_DEFAULTS}

# Chat turns read these on every message, so keep a short in-process cache.
# Admin saves invalidate it; the TTL only bounds other worker processes.
_CACHE_TTL_SECONDS = 60.0
_CACHE: dict[tuple[str, str], tuple[float, Optional[dict[str, Any]]]] = {}

_DIGITS_RE = re.compile(r"^\d*$")


class SettingsError(ValueError):
    """A submitted setting is invalid; the message is shown in the admin page."""


def defaults(section: str) -> dict[str, Any]:
    return copy.deepcopy(_DEFAULTS[section])


def with_defaults(section: str, saved: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Saved settings over the defaults, so the admin page always gets every field."""
    merged = defaults(section)
    if not isinstance(saved, dict):
        return merged
    for key, value in saved.items():
        if key not in merged:
            continue
        # Maps keyed by service / property id are taken whole; only the fixed
        # settings groups (reply_style) are merged key by key over the defaults.
        if isinstance(merged[key], dict) and isinstance(value, dict) and key not in (
            "properties", "service_buttons", "service_priority", "services"
        ):
            merged[key].update({k: v for k, v in value.items() if k in merged[key]})
        else:
            merged[key] = value
    return merged


def _text(value: Any, *, field: str, max_chars: int, required: bool = False) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if required and not text:
        raise SettingsError(f"{field} cannot be empty.")
    if len(text) > max_chars:
        raise SettingsError(f"{field} is longer than {max_chars} characters.")
    return text


def _bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _url(value: Any, *, field: str) -> str:
    url = str(value or "").strip()
    if not url:
        return ""
    if len(url) > MAX_URL_CHARS or not re.match(r"^https?://\S+$", url):
        raise SettingsError(f"{field} must be a full http(s) link.")
    return url


def _digits(value: Any, *, field: str) -> str:
    text = str(value or "").strip()
    if not _DIGITS_RE.match(text):
        raise SettingsError(f"{field} must contain digits only.")
    return text


def _service_button_list(service_id: str, entries: Any, known_properties: set[str]) -> list[dict[str, Any]]:
    """One service's buttons, cleaned. Accepts the older single-label shape
    ({"label": ...}) as one form button."""
    if isinstance(entries, (dict, str)):
        entries = [entries]
    out: list[dict[str, Any]] = []
    for index, entry in enumerate(entries or [], start=1):
        if isinstance(entry, str):
            entry = {"label": entry, "action": "form"}
        if not isinstance(entry, dict):
            continue
        where = f"{service_id} button {index}"
        label = _text(entry.get("label"), field=f"{where} label", max_chars=MAX_LABEL_CHARS)
        action = str(entry.get("action") or "form").strip().lower()
        if action not in SERVICE_BUTTON_ACTIONS:
            raise SettingsError(f"{where}: type must be one of {', '.join(SERVICE_BUTTON_ACTIONS)}.")
        url = _url(entry.get("url"), field=f"{where} link") if action == "link" else ""
        property_urls: dict[str, str] = {}
        if action == "link":
            raw_urls = entry.get("property_urls") if isinstance(entry.get("property_urls"), dict) else {}
            for slug, value in raw_urls.items():
                link = _url(value, field=f"{where} link for {slug}")
                if not link:
                    continue
                if known_properties and slug not in known_properties:
                    raise SettingsError(f"{where}: unknown property '{slug}'.")
                property_urls[str(slug)] = link
        if not label and not url and not property_urls:
            continue
        if not label:
            raise SettingsError(f"{where} needs a label.")
        if action == "link" and not url and not property_urls:
            raise SettingsError(f"{where} is a link button but has no link.")
        button: dict[str, Any] = {"label": label, "action": action}
        if action == "link":
            button["url"] = url
            button["property_urls"] = property_urls
        out.append(button)
    if len(out) > MAX_SERVICE_BUTTONS:
        raise SettingsError(f"{service_id}: at most {MAX_SERVICE_BUTTONS} buttons.")
    if sum(1 for b in out if b["action"] == "form") > 1:
        raise SettingsError(f"{service_id}: only one form button (a service has one form).")
    return out


def normalize_prebooking(
    raw: Any,
    *,
    service_ids: Iterable[str],
    property_slugs: Iterable[str] = (),
    book_slugs: Iterable[str] = (),
) -> dict[str, Any]:
    """Validate and clean everything except `properties`, which the admin routes
    fill from new_bot_properties (see index_properties)."""
    data = raw if isinstance(raw, dict) else {}
    known_services = {str(s) for s in service_ids}
    known_properties = {str(s) for s in property_slugs}
    known_books = {str(s) for s in book_slugs}
    clean = defaults("prebooking")

    buttons = []
    for index, row in enumerate(data.get("welcome_buttons") or [], start=1):
        if not isinstance(row, dict):
            continue
        label = _text(row.get("label"), field=f"Opening button {index} label", max_chars=MAX_LABEL_CHARS)
        message = _text(row.get("message"), field=f"Opening button {index} message", max_chars=MAX_MESSAGE_CHARS)
        action = str(row.get("action") or "message").strip().lower()
        if action not in WELCOME_ACTIONS:
            raise SettingsError(f"Opening button {index}: action must be one of {', '.join(WELCOME_ACTIONS)}.")
        if not label and not message:
            continue
        if not label:
            raise SettingsError(f"Opening button {index} needs a label.")
        if action == "message" and not message:
            raise SettingsError(f"Opening button {index} sends a message, so it needs one.")
        buttons.append({"label": label, "action": action, "message": message})
    if len(buttons) > MAX_WELCOME_BUTTONS:
        raise SettingsError(f"At most {MAX_WELCOME_BUTTONS} opening buttons.")
    clean["welcome_buttons"] = buttons

    after = str(data.get("after_lead_form") or clean["after_lead_form"]).strip().lower()
    if after not in AFTER_LEAD_FORM_CHOICES:
        raise SettingsError(f"After the lead form must be one of: {', '.join(AFTER_LEAD_FORM_CHOICES)}.")
    clean["after_lead_form"] = after

    service_buttons = {}
    raw_service_buttons = data.get("service_buttons") if isinstance(data.get("service_buttons"), dict) else {}
    for service_id, entries in raw_service_buttons.items():
        cleaned = _service_button_list(str(service_id), entries, known_properties)
        if not cleaned:
            continue
        if service_id not in known_services:
            raise SettingsError(f"Unknown service '{service_id}' for a service button.")
        service_buttons[str(service_id)] = cleaned
    clean["service_buttons"] = service_buttons

    priorities = {}
    raw_priorities = data.get("service_priority") if isinstance(data.get("service_priority"), dict) else {}
    for service_id, value in raw_priorities.items():
        level = str(value or "").strip().upper()
        if not level:
            continue
        if level not in PRIORITY_CHOICES:
            raise SettingsError(f"Ticket priority for {service_id} must be one of: {', '.join(PRIORITY_CHOICES)}.")
        if service_id not in known_services:
            raise SettingsError(f"Unknown service '{service_id}' for a ticket priority.")
        priorities[str(service_id)] = level
    clean["service_priority"] = priorities

    muted = [str(s) for s in (data.get("images_muted_services") or []) if str(s).strip()]
    unknown = [s for s in muted if s not in known_services]
    if unknown:
        raise SettingsError(f"Unknown service(s) for muted photos: {', '.join(unknown)}.")
    clean["images_muted_services"] = sorted(set(muted))

    # A topic whose book is no longer in the KB is dropped: the admin page only
    # lists the books that exist, so it could never be unticked there.
    topics = []
    for slug in data.get("menu_topics") or []:
        slug = str(slug or "").strip().lower()
        if slug and slug not in topics and (not known_books or slug in known_books):
            topics.append(slug)
    if len(topics) > MAX_MENU_TOPICS:
        raise SettingsError(f"At most {MAX_MENU_TOPICS} menu topics.")
    clean["menu_topics"] = topics

    clean["chips_enabled"] = _bool(data.get("chips_enabled", clean["chips_enabled"]))
    clean["form_cancel_button"] = _bool(data.get("form_cancel_button", clean["form_cancel_button"]))
    clean["outside_recommendations"] = _bool(data.get("outside_recommendations", clean["outside_recommendations"]))

    style = data.get("reply_style") if isinstance(data.get("reply_style"), dict) else {}
    out_style = clean["reply_style"]
    bullet = str(style.get("bullet", out_style["bullet"]) or "").strip()
    if bullet not in BULLET_CHOICES:
        raise SettingsError(f"Bullet style must be one of: {' '.join(BULLET_CHOICES)}.")
    emoji = str(style.get("emoji", out_style["emoji"]) or "").strip().lower()
    if emoji not in EMOJI_CHOICES:
        raise SettingsError(f"Emoji must be one of: {', '.join(EMOJI_CHOICES)}.")
    out_style["bullet"] = bullet
    out_style["emoji"] = emoji
    for flag in ("no_trailing_offer", "enforce_language", "give_number_when_cannot_arrange"):
        out_style[flag] = _bool(style.get(flag, out_style[flag]))
    return clean


def normalize_ticketing(raw: Any) -> dict[str, Any]:
    """Validate and clean everything except `properties` (see set_property_entry)."""
    data = raw if isinstance(raw, dict) else {}
    clean = defaults("ticketing")
    clean["widget_id"] = _digits(data.get("widget_id"), field="Widget ID")
    clean["group_id"] = _digits(data.get("group_id"), field="Group ID")
    departments = []
    for entry in data.get("allowed_departments") or []:
        name = _text(entry, field="Department name", max_chars=100)
        if name and name not in departments:
            departments.append(name)
    if len(departments) > MAX_DEPARTMENTS:
        raise SettingsError(f"At most {MAX_DEPARTMENTS} departments.")
    clean["allowed_departments"] = departments
    return clean


def set_property_entry(
    section_data: dict[str, Any],
    property_slug: str,
    fms_entity_id: str,
    *,
    name: str = "",
) -> dict[str, Any]:
    """Set (or clear) one property's FMS entity id in the ticketing section. The
    property's name is stored with it so a ticket that names the property (not
    its slug) still finds the right entity."""
    data = copy.deepcopy(section_data)
    properties = data.get("properties") if isinstance(data.get("properties"), dict) else {}
    entry = dict(properties.get(property_slug) or {})
    if fms_entity_id:
        entry["fms_entity_id"] = fms_entity_id
        if name:
            entry["name"] = name
    else:
        entry.pop("fms_entity_id", None)
        entry.pop("name", None)
    if entry:
        properties[property_slug] = entry
    else:
        properties.pop(property_slug, None)
    data["properties"] = properties
    return data


def index_properties(section_data: dict[str, Any], properties: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Store each property's name and aliases in the prebooking section, so the
    chat can match a hotel named in a reply to its per-property link without a
    database read. Rebuilt on every admin save from new_bot_properties."""
    data = copy.deepcopy(section_data)
    index: dict[str, dict[str, Any]] = {}
    for prop in properties or []:
        slug = str((prop or {}).get("slug") or "").strip()
        if not slug:
            continue
        index[slug] = {
            "name": str(prop.get("name") or "").strip(),
            "aliases": [str(a).strip() for a in (prop.get("aliases") or []) if str(a).strip()],
        }
    data["properties"] = index
    return data


def clean_property_update(
    *,
    name: Any,
    location_label: Any,
    city: Any,
    aliases: Any,
    fms_entity_id: Any,
) -> dict[str, Any]:
    """Validate one property edit from the admin page."""
    clean_aliases: list[str] = []
    for alias in aliases or []:
        text = _text(alias, field="Alias", max_chars=100)
        if text and text.lower() not in {a.lower() for a in clean_aliases}:
            clean_aliases.append(text)
    if len(clean_aliases) > 20:
        raise SettingsError("At most 20 aliases per property.")
    return {
        "name": _text(name, field="Property name", max_chars=255, required=True),
        "location_label": _text(location_label, field="Picker label", max_chars=255, required=True),
        "city": _text(city, field="City", max_chars=100),
        "aliases": clean_aliases,
        "fms_entity_id": _digits(fms_entity_id, field="FMS entity ID"),
    }


def _code(hotel_code: Any) -> str:
    return str(hotel_code or "").strip().lower()


async def _load_rows(hotel_code: str = "") -> dict[str, dict[str, Optional[dict[str, Any]]]]:
    """{hotel_code: {section: saved value or None}} for one hotel, or for every hotel."""
    async with AsyncSessionLocal() as session:
        hotel_query = select(Hotel.id, Hotel.code)
        if hotel_code:
            hotel_query = hotel_query.where(func.lower(Hotel.code) == hotel_code)
        codes = {int(hid): _code(code) for hid, code in (await session.execute(hotel_query)).all()}
        loaded = {code: {section: None for section in SECTIONS} for code in codes.values()}
        if codes:
            rows = (await session.execute(
                select(BusinessConfig.hotel_id, BusinessConfig.config_key, BusinessConfig.config_value)
                .where(
                    BusinessConfig.hotel_id.in_(list(codes)),
                    BusinessConfig.config_key.in_([f"json_section.{s}" for s in SECTIONS]),
                )
            )).all()
            for hotel_id, key, raw in rows:
                try:
                    parsed = json.loads(raw) if raw else None
                except ValueError:
                    parsed = None
                loaded[codes[int(hotel_id)]][str(key).split(".", 1)[1]] = parsed if isinstance(parsed, dict) else None
    return loaded


def _store(loaded: dict[str, dict[str, Optional[dict[str, Any]]]]) -> None:
    now = time.monotonic()
    for code, sections in loaded.items():
        for section, value in sections.items():
            _CACHE[(code, section)] = (now, value)


async def warm_all() -> int:
    """Load every hotel's settings at startup, so synchronous readers have them
    from the first turn. Returns how many hotels are admin-managed."""
    loaded = await _load_rows()
    _store(loaded)
    return sum(1 for sections in loaded.values() if sections.get("prebooking") is not None)


async def refresh_if_stale(hotel_code: Any) -> None:
    """Re-read one hotel's settings when the cached copy is older than the TTL.
    Called at the start of every chat turn and by the widget bootstrap."""
    code = _code(hotel_code)
    if not code:
        return
    now = time.monotonic()
    entries = [_CACHE.get((code, section)) for section in SECTIONS]
    if all(entry and now - entry[0] <= _CACHE_TTL_SECONDS for entry in entries):
        return
    loaded = await _load_rows(code)
    _store(loaded or {code: {section: None for section in SECTIONS}})


def remember(hotel_code: Any, section: str, value: Optional[dict[str, Any]]) -> None:
    """Put a just-saved value in the cache, so the change applies immediately."""
    _CACHE[(_code(hotel_code), section)] = (time.monotonic(), copy.deepcopy(value))


def invalidate(hotel_code: Any) -> None:
    for section in SECTIONS:
        _CACHE.pop((_code(hotel_code), section), None)


async def get_section(hotel_code: Any, section: str, *, use_cache: bool = True) -> Optional[dict[str, Any]]:
    """The hotel's saved section, or None when the hotel has not saved one
    (callers then keep their old .env / code behaviour)."""
    if section not in SECTIONS:
        raise ValueError(f"unknown settings section {section!r}")
    code = _code(hotel_code)
    if not code:
        return None
    if not use_cache:
        invalidate(code)
    await refresh_if_stale(code)
    return peek_section(code, section)


def peek_section(hotel_code: Any, section: str) -> Optional[dict[str, Any]]:
    """Cached value without a DB read, for synchronous callers. Kept past the
    TTL until the next refresh (stale-while-revalidate); None means the hotel has
    not saved this section, so callers keep their old behaviour."""
    cached = _CACHE.get((_code(hotel_code), section))
    return copy.deepcopy(cached[1]) if cached else None


# ---- Readers used by the chat, widget and ticketing code ------------------

def settings_for(hotel_code: Any) -> Optional[dict[str, Any]]:
    """The hotel's pre-booking settings with defaults filled in, or None for a
    hotel that is not admin-managed (it keeps its old .env / code behaviour)."""
    saved = peek_section(hotel_code, "prebooking")
    return with_defaults("prebooking", saved) if saved is not None else None


def ticketing_for(hotel_code: Any) -> Optional[dict[str, Any]]:
    saved = peek_section(hotel_code, "ticketing")
    return with_defaults("ticketing", saved) if saved is not None else None


def is_admin_managed(hotel_code: Any) -> bool:
    return peek_section(hotel_code, "prebooking") is not None


def _all_saved(section: str) -> dict[str, dict[str, Any]]:
    return {
        code: with_defaults(section, value)
        for (code, cached_section), (_ts, value) in list(_CACHE.items())
        if cached_section == section and value is not None
    }


def _match_keys(slug: str, entry: dict[str, Any]) -> set[str]:
    keys = {slug.replace("_", " ").strip().lower(), str(entry.get("name") or "").strip().lower()}
    keys.update(str(a).strip().lower() for a in (entry.get("aliases") or []))
    keys.discard("")
    return keys


def property_slug_for(hotel_code: Any, *texts: Any) -> str:
    """The property named in `texts` (slug, name or alias); longest match wins.
    "" when none of this hotel's properties is named."""
    admin = settings_for(hotel_code)
    if admin is None:
        return ""
    blob = " ".join(str(t or "") for t in texts).lower().replace("_", " ")
    best_slug, best_len = "", 0
    for slug, entry in (admin.get("properties") or {}).items():
        for key in _match_keys(slug, entry or {}):
            if key in blob and len(key) > best_len:
                best_slug, best_len = slug, len(key)
    return best_slug


def link_url_for(hotel_code: Any, button: dict[str, Any], *texts: Any) -> str:
    """Where a link button goes for the property named in `texts`: that
    property's own link when it has one, else the button's shared link. "" when
    neither applies - the button is then left off, because sending a guest to
    the wrong hotel's page is worse than no button."""
    per_property = button.get("property_urls") or {}
    if per_property:
        slug = property_slug_for(hotel_code, *texts)
        if slug and per_property.get(slug):
            return str(per_property[slug])
    return str(button.get("url") or "")


def looks_like_room_booking(service_id: Any) -> bool:
    """Same test the chat uses to recognise the room-booking agent."""
    sid = re.sub(r"[^a-z0-9]+", "_", str(service_id or "").lower())
    if any(tok in sid for tok in ("support", "complaint", "callback", "event", "banquet", "venue",
                                  "spa", "dining", "table", "transfer", "cab", "taxi")):
        return False
    return ("room" in sid or "stay" in sid) and ("book" in sid or "reserv" in sid)


def room_booking_buttons(hotel_code: Any, *service_hints: Any) -> Optional[list[dict[str, Any]]]:
    """The room-booking service's buttons for an admin-managed hotel (None for
    any other hotel). The service that answered is tried first, then any
    service that looks like room booking."""
    admin = settings_for(hotel_code)
    if admin is None:
        return None
    configured = admin.get("service_buttons") or {}
    flat = {re.sub(r"[^a-z0-9]+", "", sid.lower()): sid for sid in configured}
    for hint in service_hints:
        sid = flat.get(re.sub(r"[^a-z0-9]+", "", str(hint or "").lower()))
        if sid and looks_like_room_booking(sid):
            return copy.deepcopy(configured[sid])
    for sid, buttons in configured.items():
        if looks_like_room_booking(sid):
            return copy.deepcopy(buttons)
    return []


def menus_for(hotel_code: Any, active_property_slugs: Iterable[str] = ()) -> Optional[list[dict[str, Any]]]:
    """Menus to send for an admin-managed hotel (None for any other hotel): the
    ones for the property in focus plus the ones for every property; all of them
    when no single property is in focus."""
    admin = settings_for(hotel_code)
    if admin is None:
        return None
    menus = [m for m in (admin.get("menus") or []) if isinstance(m, dict) and m.get("stored")]
    active = {str(s) for s in active_property_slugs if str(s).strip()}
    if len(active) != 1:
        return menus
    return [m for m in menus if not m.get("property_slug") or m.get("property_slug") in active]


def index_services(section_data: dict[str, Any], services: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Store each service's display name and DB row id, so a ticket stamped
    with either (the form-submit path uses both) still finds the service's
    settings. Rebuilt on every admin save."""
    data = copy.deepcopy(section_data)
    data["services"] = {
        str(svc.get("service_id")): {"name": str(svc.get("name") or ""), "row_id": str(svc.get("row_id") or "")}
        for svc in services or []
        if str((svc or {}).get("service_id") or "").strip()
    }
    return data


def service_priority_for(hotel_code: Any, *service_refs: Any) -> str:
    """The ticket priority an admin set for a service ("" = no setting). A ref
    may be the service id, its display name or its DB row id."""
    admin = settings_for(hotel_code)
    wanted = {re.sub(r"[^a-z0-9]+", "", str(r or "").lower()) for r in service_refs}
    wanted.discard("")
    if admin is None or not wanted:
        return ""
    index = admin.get("services") if isinstance(admin.get("services"), dict) else {}
    for sid, level in (admin.get("service_priority") or {}).items():
        entry = index.get(sid) or {}
        keys = {re.sub(r"[^a-z0-9]+", "", str(k or "").lower()) for k in (sid, entry.get("name"), entry.get("row_id"))}
        if wanted & keys:
            return str(level)
    return ""


def menu_by_id(hotel_code: Any, menu_id: Any) -> Optional[dict[str, Any]]:
    admin = settings_for(hotel_code)
    for menu in (admin or {}).get("menus") or []:
        if isinstance(menu, dict) and str(menu.get("id") or "") == str(menu_id or ""):
            return dict(menu)
    return None


def team_only_booking(hotel_code: Any) -> Optional[bool]:
    """True when an admin-managed hotel's room booking has a form button and no
    link: the team takes every booking. None for hotels not managed here."""
    buttons = room_booking_buttons(hotel_code)
    if buttons is None:
        return None
    has_link = any(b.get("action") == "link" for b in buttons)
    has_form = any(b.get("action") == "form" for b in buttons)
    return has_form and not has_link


def entity_map_overlay() -> dict[str, dict[str, str]]:
    """{hotel_code: {property name or slug words: FMS entity id}} for
    admin-managed hotels, in the shape of TICKETING_PROPERTY_ENTITY_MAP_JSON."""
    out: dict[str, dict[str, str]] = {}
    for code, ticketing in _all_saved("ticketing").items():
        inner: dict[str, str] = {}
        for slug, entry in (ticketing.get("properties") or {}).items():
            entity = str((entry or {}).get("fms_entity_id") or "").strip()
            if entity:
                for key in _match_keys(slug, entry or {}):
                    inner[key] = entity
        if inner:
            out[code] = inner
    return out


def hotel_for_entity(entity_id: Any) -> str:
    """The admin-managed hotel that owns an FMS entity id ("" when none), for
    channels that arrive with only an entity id (WhatsApp)."""
    wanted = str(entity_id or "").strip()
    if not wanted:
        return ""
    for code, inner in entity_map_overlay().items():
        if wanted in inner.values():
            return code
    return ""


def allowed_departments_overlay() -> dict[str, list[str]]:
    """{hotel_code: [department names]} for admin-managed hotels that set them."""
    return {
        code: list(ticketing.get("allowed_departments") or [])
        for code, ticketing in _all_saved("ticketing").items()
        if ticketing.get("allowed_departments")
    }


def single_entity_overlay() -> set[str]:
    """Admin-managed hotels whose properties all share one FMS entity."""
    return {code for code, inner in entity_map_overlay().items() if len(set(inner.values())) == 1}


async def welcome_config_for(hotel_code: Any) -> Optional[dict[str, Any]]:
    """What the widget needs at load: the opening buttons and what follows the
    lead form. None for hotels that are not admin-managed."""
    await refresh_if_stale(hotel_code)
    admin = settings_for(hotel_code)
    if admin is None:
        return None
    return {
        "buttons": copy.deepcopy(admin.get("welcome_buttons") or []),
        "after_lead_form": admin.get("after_lead_form") or "intro",
    }
