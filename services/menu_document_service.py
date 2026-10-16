"""
Menu document delivery.

When an in-house guest asks a dining service agent for "the menu" (send / share /
see the menu / menu pdf), we attach the property's real menu PDF to the response
metadata as `documents`, and the widget renders a branded download card.

Design notes
------------
- The URL is ALWAYS the known, hosted file from `MENU_DOCUMENTS` below — never a
  URL typed by the LLM. The model can hallucinate file links; we never trust one.
- URLs are stored RELATIVE (`/static/menus/...`). The chat UI is always served
  from the backend origin (admin `/chat` and the embedded iframe both load from
  the backend), so a relative href resolves to the backend that hosts the file.
- Menu selection is per-service: the in_room_dining agent sends the IRD menu, the
  restaurant_reservation agent sends the restaurant menu. Adding a menu = one line.
"""

from __future__ import annotations

import re
from typing import Any, Optional


# hotel_code (lowercased) -> service_id -> menu descriptor.
# Add a new menu by adding one row. `kind` drives the widget card icon.
MENU_DOCUMENTS: dict[str, dict[str, dict[str, str]]] = {
    "sarovar_portico_sriperumbudur": {
        "in_room_dining": {
            "title": "In-Room Dining Menu",
            "url": "/static/menus/sarovar_ird.pdf",
            "kind": "pdf",
        },
        # "restaurant_reservation": {
        #     "title": "Restaurant Menu",
        #     "url": "/static/menus/sarovar_restaurant.pdf",
        #     "kind": "pdf",
        # },
    },
}


def _norm(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


# A menu WORD must be present, plus either a request cue (send/see/pdf/...) or the
# whole message is essentially just "menu" / "the menu please". This keeps us off
# incidental uses of the word ("I saw it on the menu board outside").
_MENU_WORD_RE = re.compile(r"\b(menu|carte|food\s*(?:list|card))\b", re.IGNORECASE)
_MENU_CUE_RE = re.compile(
    r"\b(send|share|show|see|view|get|have|give|pull\s*up|bring|pdf|card|link|"
    r"download|open|attach|display|forward|drop)\b",
    re.IGNORECASE,
)
_MENU_BARE_RE = re.compile(
    r"^\s*(?:the\s+|a\s+|your\s+)?(?:food\s+)?menu(?:\s*(?:please|pls|card|pdf|now))?\s*[?.!]*\s*$",
    re.IGNORECASE,
)


def is_menu_file_request(text: Any) -> bool:
    """True when the guest is asking for the menu FILE (not just discussing food)."""
    t = str(text or "").strip()
    if not t or not _MENU_WORD_RE.search(t):
        return False
    if _MENU_BARE_RE.match(t):
        return True
    return bool(_MENU_CUE_RE.search(t))


_DINING_SERVICES = ("in_room_dining", "room_dining", "room_service", "restaurant")


def is_dining_service(service_agent_id: Any) -> bool:
    """Did the router hand this turn to a food service?

    This is the ROUTER'S OWN judgement, read back as a fact — not an attempt to
    re-guess the guest's intent from their wording. "I want to order something" and
    the order-food button both land on a dining service, so asking which service is
    active answers "is this guest thinking about food" without matching a phrase.
    """
    svc = _norm(service_agent_id)
    return bool(svc) and any(marker in svc for marker in _DINING_SERVICES)


def admin_menu_documents(
    *,
    hotel_code: Any,
    user_text: Any,
    active_property_ids: Any = (),
    router_book_ids: Any = (),
    already_sent: Any = (),
) -> Optional[list[dict[str, str]]]:
    """Menus uploaded in Bot Training -> Pre-booking. None for a hotel that is
    not admin-managed (it keeps MENU_DOCUMENTS above).

    Sent when the guest asks for the menu itself - in any phase, every time -
    and, unasked, when the main book the router chose for the answer is one the
    admin ticked as a menu topic (Dining & Restaurants). The router lists the
    book the question is about first: "what restaurants do you have?" opens
    Dining & Restaurants first, while "tell me about the hotel" or a typed
    hotel name also open it but further down, and those are not dining
    questions. The books that merely landed in the answerer's context are not
    the router's decision, so they are never read here. An unasked menu goes
    out once per conversation (`already_sent` holds the ids already in the
    chat). Which menus: the hotel in focus plus any set for every hotel; all of
    them when no single hotel is in focus.

    The link is our own /api/widget/menu/... route, which redirects to a fresh
    S3 link on every open, so it still works when the guest scrolls back days
    later. Each document carries its menu id as `menu_id`."""
    from services import prebooking_settings_service

    menus = prebooking_settings_service.menus_for(hotel_code, list(active_property_ids or []))
    if menus is None:
        return None
    if not menus:
        return []
    if not is_menu_file_request(user_text):
        topics = set((prebooking_settings_service.settings_for(hotel_code) or {}).get("menu_topics") or [])
        main_book = str(next(iter(router_book_ids or []), "") or "").strip().lower()
        if main_book not in topics:
            return []
        sent = {str(s) for s in (already_sent or [])}
        menus = [m for m in menus if str(m.get("id") or "") not in sent]
    code = str(hotel_code or "").strip().lower()
    return [
        {
            "title": str(menu.get("title") or "Menu"),
            "url": f"/api/widget/menu/{code}/{menu.get('id')}",
            "kind": "pdf",
            "menu_id": str(menu.get("id") or ""),
        }
        for menu in menus
    ]


def build_menu_document(
    *,
    hotel_code: Any,
    service_agent_id: Any = "",
    phase_id: Any = "",
    user_text: Any = "",
    already_sent: bool = False,
) -> Optional[dict[str, str]]:
    """
    Return the menu descriptor to attach, or None.

    The menu goes out on two occasions:

    * THE FIRST TIME the guest sets out to order food — meaning the router handed
      the turn to the in-room dining service, whether they typed the intent or
      tapped the order-food button. Making them ask for the menu separately is a
      wasted turn. `already_sent` keeps this to once, so the same PDF is not
      re-attached to every dish they discuss afterwards.
    * ANY TIME THEY EXPLICITLY ASK for it ("send me the menu"), however many
      times, and regardless of `already_sent` — an explicit request is never
      refused on the grounds that we sent it earlier.

    Nothing else attaches it. An earlier version also fired when the answerer's
    knowledge-base context happened to contain a dining book, on the theory that
    the router had "opened the dining pages". It had not chosen anything: those
    labels are the RETRIEVAL SET, and a vague turn ("yes") loads the whole book
    list, dining included. A guest reporting slow Wi-Fi before check-in was handed
    the room-service menu under the apology. Routing is a decision; retrieval is a
    side-effect — only the decision may trigger this.

    The URL is always the hosted file below, never one the model produced.
    """
    explicitly_asked = is_menu_file_request(user_text)
    if not explicitly_asked:
        # Unprompted delivery: only on the turn the guest sets out to order, and
        # only once. `service_agent_id` is the router's own routing decision, read
        # back as a fact — not an attempt to re-read the guest's mind.
        if already_sent or not is_dining_service(service_agent_id):
            return None
        # ...and not to a guest we KNOW is not in-house yet. Room service does not
        # exist for someone who has not checked in, so pushing them its menu is
        # noise. They can still ASK for it (handled above) — a direct request is
        # answered in any phase.
        #
        # Deliberately fail-OPEN on an unknown phase: only a phase we positively
        # read as something other than during_stay blocks. Not every channel
        # stamps one, and a gate that treats "I don't know" as "no" silently
        # switches the whole feature off wherever the stamp is missing — which is
        # exactly what happened when this read a metadata key nobody writes. The
        # routing check above is the real defence; this only refines it.
        phase = _norm(phase_id)
        if phase and phase != "during_stay":
            return None

    menus = MENU_DOCUMENTS.get(_norm(hotel_code))
    if not menus:
        return None

    svc = _norm(service_agent_id)
    if "restaurant" in svc and menus.get("restaurant_reservation"):
        return menus["restaurant_reservation"]
    if ("in_room_dining" in svc or "room_dining" in svc or "room_service" in svc) and menus.get(
        "in_room_dining"
    ):
        return menus["in_room_dining"]

    # Generic in-stay menu ask with no dining service pinned -> room-service menu.
    return menus.get("in_room_dining") or next(iter(menus.values()), None)
