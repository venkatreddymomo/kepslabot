"""When each in-room dining dish can actually be ordered.

The menu states serving times as section prose - "All Tandoori Kebabs will be
available from 12 noon to 3 pm and 7 pm to 11 pm" - and never says which dishes
that covers. It also writes items in three different shapes, so no parser reads
it cleanly (three attempts over the real 41k-char menu returned 152, 169 and 352
items). Asking a model to resolve dish -> section -> window on every turn, over
that text, is what made it accept a Tandoori Murgh at 5:36 PM.

So the answer is written down once in `config/ird_serving_windows.json` and read
from here. Code resolves the clock; the model is handed a short list of names and
does no timing work at all.

TWO RULES THIS MODULE MUST NEVER BREAK
--------------------------------------
1. It answers "can this be ordered right now", NEVER "do we serve this". A closed
   dish is still on the menu and must be offered back with its hours. Nothing
   here should ever be used to filter the menu the model sees.
2. It fails OPEN. A missing table, a malformed entry, an unparseable clock, an
   ambiguous name - all resolve to AVAILABLE. A timing feature must never be the
   reason a guest cannot order food.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# A menu line stating serving hours in prose - "Timing: 7:00 AM - 10:30 AM",
# "Note: All Tandoori Kebabs will be available from 12 noon to 3 pm and 7 pm to
# 11 pm". Both halves must match: a line needs the word AND a real clock range,
# so a dish merely described as "available in two sizes" is left alone.
_NOTE_RE = re.compile(r"\b(timing|available)\b", re.I)
_TIME_RANGE_RE = re.compile(
    r"\d{1,2}(:\d{2})?\s*(am|pm|noon|midnight)?\s*(?:[-‐-―]|to|till|until)\s*"
    r"\d{1,2}(:\d{2})?\s*(am|pm|noon|midnight)",
    re.I,
)

_TABLE_PATH = Path(__file__).resolve().parent.parent / "config" / "ird_serving_windows.json"
_CACHE: dict[str, Any] | None = None
_CACHE_MTIME: float | None = None


def _load_table() -> dict[str, Any]:
    """The whole file, cached until it changes on disk. Any failure yields an empty
    table, which means every dish is available - see rule 2."""
    global _CACHE, _CACHE_MTIME
    try:
        mtime = _TABLE_PATH.stat().st_mtime
    except OSError:
        logger.error(
            "[IRD_AVAIL] serving-window table missing at %s - every dish will be "
            "treated as available", _TABLE_PATH,
        )
        return {}
    if _CACHE is not None and _CACHE_MTIME == mtime:
        return _CACHE
    try:
        with _TABLE_PATH.open(encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("top level is not an object")
    except (OSError, ValueError) as exc:
        logger.error(
            "[IRD_AVAIL] serving-window table unreadable (%r) - every dish will be "
            "treated as available", exc,
        )
        return {}
    _CACHE, _CACHE_MTIME = data, mtime
    return data


def _hotel_table(hotel_code: str) -> dict[str, Any]:
    entry = _load_table().get(str(hotel_code or "").strip())
    return entry if isinstance(entry, dict) else {}


def _norm(name: str) -> str:
    """Fold a dish name to something two spellings of it can agree on. Punctuation
    and case vary constantly between what the menu prints, what the guest types and
    what the model echoes back.

    Apostrophes are DELETED rather than turned into a space, so "Baker's Basket"
    and "Bakers Basket" fold together instead of becoming "baker s basket" and
    "bakers basket", which share no word and so never matched.
    """
    s = str(name or "").lower()
    s = s.replace("&", " and ")
    s = re.sub(r"['‘’ʼ]", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _words(norm_name: str) -> set[str]:
    return {w for w in norm_name.split() if w}


def _parse_hhmm(value: str) -> int | None:
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(value or ""))
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    return h * 60 + mi


def _window_open(start: int, end: int, now: int) -> bool:
    """A window that ends before it starts crosses midnight (23:00-07:00), and both
    sides of the wrap count as open."""
    if end > start:
        return start <= now < end
    return now >= start or now < end


def _fmt(minutes: int) -> str:
    minutes %= 24 * 60
    h, m = divmod(minutes, 60)
    return f"{h % 12 or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"


def _periods(hotel_code: str) -> dict[str, list[tuple[int, int]]]:
    out: dict[str, list[tuple[int, int]]] = {}
    for name, spans in (_hotel_table(hotel_code).get("periods") or {}).items():
        if not isinstance(spans, list):
            continue
        parsed = []
        for span in spans:
            if not (isinstance(span, (list, tuple)) and len(span) == 2):
                continue
            a, b = _parse_hhmm(span[0]), _parse_hhmm(span[1])
            if a is not None and b is not None and a != b:
                parsed.append((a, b))
        if parsed:
            out[str(name)] = parsed
    return out


def _items(hotel_code: str) -> dict[str, list[str]]:
    """Dish -> period names. Keys starting with `_` are commentary, not dishes."""
    out: dict[str, list[str]] = {}
    for name, periods in (_hotel_table(hotel_code).get("items") or {}).items():
        if str(name).startswith("_") or not isinstance(periods, list):
            continue
        key = _norm(name)
        if key:
            out[key] = [str(p) for p in periods]
    return out


def _now_minutes(now: datetime) -> int:
    return now.hour * 60 + now.minute


def _describe(period_names: list[str], periods: dict[str, list[tuple[int, int]]], now_m: int) -> tuple[bool, str, str]:
    """(open_now, "7:00 AM-10:30 AM", "tomorrow at 7:00 AM") for one dish."""
    spans: list[tuple[int, int]] = []
    for p in period_names:
        spans.extend(periods.get(p, []))
    if not spans:
        return True, "", ""  # unknown period name -> available (rule 2)
    if any(_window_open(a, b, now_m) for a, b in spans):
        return True, "", ""
    served = " and ".join(f"{_fmt(a)}-{_fmt(b)}" for a, b in spans)
    later = [a for a, _ in spans if a > now_m]
    when = f"today at {_fmt(min(later))}" if later else f"tomorrow at {_fmt(min(a for a, _ in spans))}"
    return False, served, when


def item_status(hotel_code: str, item_name: str, now: datetime) -> dict[str, Any]:
    """Can this dish be ordered right now?

    `{"available": bool, "served": str, "next_opens": str, "matched": str}`.

    Matching is on WORD SETS, not substrings, and only ever decisive when the name
    genuinely identifies one dish:

    * same words            -> decisive ("idli", "  IDLI  ", "Baker's Basket")
    * key's words ⊆ query   -> decisive; this is the normal case, because the model
      declares menu names ("2x Dosa (Plain or Masala)")
    * query ⊂ key's words   -> decisive ONLY if the query has two or more words
      ("aloo paratha" -> the breakfast paratha), never on one

    That last restriction is the whole point. The table lists restricted dishes
    only, so it cannot see that a bare "paratha" might mean the all-day Kerala
    Paratha rather than the breakfast one - matching it would refuse a guest food
    the kitchen would happily cook. A single word that only partly matches is left
    available and the model handles it from the closed list, which names the dish
    in full along with its hours.
    """
    free = {"available": True, "served": "", "next_opens": "", "matched": ""}
    key = _norm(item_name)
    if not key:
        return free
    table, periods = _items(hotel_code), _periods(hotel_code)
    if not table or not periods:
        return free

    qw = _words(key)
    matched = []
    for k in table:
        kw = _words(k)
        if not kw:
            continue
        if kw == qw or kw <= qw or (qw < kw and len(qw) >= 2):
            matched.append(k)
    if not matched:
        return free
    if len(matched) > 1:
        # Two restricted dishes answer to the same words. If they agree on the
        # verdict it does not matter which; if they disagree, say available.
        verdicts = {_describe(table[k], periods, _now_minutes(now))[0] for k in matched}
        if len(verdicts) > 1:
            logger.info(
                "[IRD_AVAIL] %r matches %d table entries that disagree (%s) - "
                "treating as available rather than guessing",
                item_name, len(matched), matched[:4],
            )
            return free
        matched = [sorted(matched, key=len)[0]]

    ok, served, when = _describe(table[matched[0]], periods, _now_minutes(now))
    return {"available": ok, "served": served, "next_opens": when, "matched": matched[0]}


def annotate_menu(hotel_code: str, menu_text: str, now: datetime) -> str:
    """Write each restricted dish's serving time AND today's verdict into the menu,
    beside the dish.

    Two failures, one line. Both were seen in real runs:

    * A separate list of closed dishes only ever says which dishes ARE restricted.
      Concluding a dish is UNrestricted means noticing it is absent from that list,
      and absence is weak evidence. At 13:34 the bot told a guest that Filter
      Coffee - an all-day drink, nowhere on the closed list - was "served only from
      7:00 AM to 10:30 AM". The data was right; it invented the window anyway.
    * Stating the bare hours here instead invited the model to compare them to the
      clock itself, which is the arithmetic this whole module exists to remove. At
      13:46 it read "12:00 PM-3:00 PM and 7:00 PM-11:00 PM" off a Tandoori Murgh -
      a window it was INSIDE - and refused the dish anyway.

    So the line carries the answer, not the ingredients for one:

        Serving time: 12:00 PM-3:00 PM and 7:00 PM-11:00 PM - AVAILABLE NOW
        Serving time: 7:00 AM-10:30 AM - NOT AVAILABLE NOW (back tomorrow at 7:00 AM)

    and every other dish conspicuously has no such line, which is a far stronger
    signal than not appearing in a list somewhere else. Nothing is left to infer
    and nothing is left to compute.

    The verdict changes only when a window boundary is crossed - six times a day
    for this menu - so the system-prompt cache prefix holds between boundaries.
    That is the right trade: a cache miss costs a little prefill, a wrong verdict
    costs the guest their dinner.
    """
    text = str(menu_text or "")
    if not text.strip():
        return text
    table, periods = _items(hotel_code), _periods(hotel_code)
    if not table or not periods:
        return text

    # Dish -> the line to print, for dishes that are actually restricted. A dish
    # whose only period is the all-day baseline is not restricted and gets nothing.
    now_m = _now_minutes(now)
    original = {
        _norm(k): k
        for k in (_hotel_table(hotel_code).get("items") or {})
        if not str(k).startswith("_")
    }
    hours: dict[str, str] = {}
    baseline = periods.get("all_day") or []
    for key, names in table.items():
        spans: list[tuple[int, int]] = []
        for n in names:
            spans.extend(periods.get(n, []))
        if not spans:
            continue
        # A dish listed with ONLY the all-day baseline is unrestricted, and is in
        # the table on purpose - to be said so out loud. The tawa breads sit among
        # tandoor breads that are restricted, and two of them share a word with the
        # breakfast-only paratha; unlisted, they inherited their neighbours' hours:
        # "Kerala Paratha is served from 7:00 AM to 10:30 AM" and "Phulka is not
        # currently available as a menu item" - both wrong, both from a real run.
        if spans == baseline:
            hours[key] = "AVAILABLE ALL DAY - no serving restriction, orderable at any hour"
            continue
        ok, served, when = _describe(names, periods, now_m)
        served = served or " and ".join(f"{_fmt(a)}-{_fmt(b)}" for a, b in spans)
        # AN OPEN DISH GETS NO HOURS TO RECITE. Printing them alongside "AVAILABLE
        # NOW" gave the model something to narrate, and with two windows it
        # narrated the wrong one: at 13:54 a Tandoori Murgh that was open said
        # "available from 12:00 PM to 3:00 PM and 7:00 PM to 11:00 PM, it will be
        # back later today at 7:00 PM" and then offered alternatives "available
        # now". Right now it is simply available, like any other dish, and there is
        # nothing to explain. The hours appear only when they are the reason the
        # guest cannot have it - and the menu's own section note still states them
        # for anyone who asks outright.
        hours[key] = (
            "AVAILABLE NOW" if ok
            else f"NOT AVAILABLE NOW - served {served}, back {when}" if when
            else f"NOT AVAILABLE NOW - served {served}"
        )
    if not hours:
        return text

    out: list[str] = []
    pending: str | None = None   # the dish whose Price line we are waiting for
    since = 0                    # lines seen since that name, to bound the reach
    written = 0
    dropped = 0
    for raw in text.split("\n"):
        stripped = raw.strip()
        # ── THE MENU'S OWN PROSE NOTES GO ─────────────────────────────────
        # "Note: All Tandoori Kebabs will be available from 12 noon to 3 pm and
        # 7 pm to 11 pm" is the last thing in the payload that invites the model
        # to compare a clock to a window, and it took the invitation: at 14:07,
        # with the dish's own line reading AVAILABLE NOW, it recited that note
        # instead and told the guest the kebab "will be back at 7:00 PM today".
        # The note is also the one that never says which dishes it covers, which
        # is why the table exists. Every dish it governs now carries its own
        # answer, so the note is redundant as well as harmful.
        if _NOTE_RE.search(stripped) and _TIME_RANGE_RE.search(stripped):
            out.append("(Serving times are shown on each dish below.)")
            dropped += 1
            pending, since = None, 0
            continue
        # ── A SET'S CONTENTS ARE NOT DISHES WITH HOURS ───────────────────
        # The breakfast combos list what comes with them, and those lists end
        # "Tea / Coffee". So the menu contains, three times, the word Coffee a
        # dozen lines under a NOT-AVAILABLE-until-7am stamp - and asked for a
        # Filter Coffee (all-day, on no closed list, carrying no timing of its
        # own) the bot quoted back exactly those breakfast hours. Naming the dish
        # on the stamp was not enough; the list itself has to say what it is.
        if stripped.lower() == "includes":
            out.append(
                "Includes (the components of this set - they come WITH it, are not "
                "separately orderable dishes, and have no serving times of their own):"
            )
            continue
        out.append(raw)
        if not stripped:
            continue
        if pending and re.match(r"^price\s*:", stripped, re.I):
            # NAME THE DISH ON THE LINE. A bare "Serving time: NOT AVAILABLE NOW -
            # served 7:00 AM-10:30 AM" sat directly above each breakfast combo's
            # "Includes" list, and those lists end with "Tea / Coffee". So the menu
            # contained, three times over, the word Coffee a few lines under a
            # NOT-AVAILABLE stamp - and asked for a Filter Coffee (an all-day drink,
            # absent from every closed list and carrying no timing of its own) the
            # bot quoted back exactly those breakfast hours. Saying WHICH dish the
            # line is about stops the stamp reading as though it governs whatever
            # words happen to follow it.
            _name = original.get(pending, pending)
            out.append(f"Serving time for {_name}: {hours[pending]}")
            written += 1
            pending = None
            continue
        # A dish name is its own line and its Price follows within a line or two
        # (usually just `Type:`). Restricted dish names ALSO appear inside combo
        # listings - "Indian Breakfast" offers a choice of Idli or Uttappam - and
        # those have no Price of their own. Bounding the reach stops such a name
        # drifting down and annotating the next unrelated dish's Price instead.
        if pending is not None:
            since += 1
            if since > 3:
                pending, since = None, 0
        key = _norm(stripped)
        if key in hours:
            pending, since = key, 0
    logger.info(
        "[IRD_AVAIL] annotated %d menu item(s) with their serving times; "
        "replaced %d prose timing note(s)", written, dropped,
    )
    return "\n".join(out)


def covers(hotel_code: str) -> bool:
    """Does a usable per-dish table exist for this hotel?

    Distinguishes "the table says nothing is restricted right now" from "there is
    no table", which are the same empty result but opposite situations: the first
    is an answer, the second means fall back to the menu's section prose.
    """
    return bool(_periods(hotel_code)) and bool(_items(hotel_code))


def build_availability_block(hotel_code: str, now: datetime) -> dict[str, Any]:
    """What the model is told about timing this turn - one short list of names.

    Two modes, and always the shorter list:

    * Normal hours - name the handful of dishes that are shut. Everything else on
      the menu is open, which is most of it.
    * Outside the all-day window - invert and name only what IS open, because the
      closed set would be the whole menu. That set is exactly the dishes carrying
      a period that is open now, so it never has to be enumerated from the menu.

    Empty dict when there is nothing to say, which leaves every dish available.
    """
    periods, table = _periods(hotel_code), _items(hotel_code)
    if not periods or not table:
        return {}
    now_m = _now_minutes(now)
    original = {_norm(k): k for k in (_hotel_table(hotel_code).get("items") or {}) if not str(k).startswith("_")}

    # "all_day" is the baseline every unlisted dish is on. If it is shut, the menu
    # at large is shut and only the explicitly-open dishes can be ordered.
    baseline = periods.get("all_day")
    baseline_open = (not baseline) or any(_window_open(a, b, now_m) for a, b in baseline)

    rule_never_refuse = (
        "This list is about TIMING ONLY. It is NEVER the menu and never says what "
        "the hotel serves. If a guest asks for a dish that is on the menu but not "
        "available at this hour, say we DO serve it, give the hours it is served "
        "and when it is next available, and offer something else - NEVER say we do "
        "not have it. "
        # ── AND THE HOURS DO NOT MOVE BECAUSE THE GUEST PUSHES ────────────────
        # These times are the kitchen's, not a policy to be negotiated. Asked for
        # idli at night the bot declined correctly, then gave in to "I don't
        # care", "I'll pay extra" and "the website says 24/7" - promising food
        # nobody would cook and, worse, inventing a fee and a special
        # arrangement to justify it. Pressure is not new information.
        "THESE HOURS DO NOT CHANGE BECAUSE THE GUEST INSISTS. If they push, "
        "repeat the hours warmly and hold: no exceptions, no 'let me check with "
        "the chef', no extra charge, no special arrangement, and never a promise "
        "that someone might make it anyway. You may not invent a fee, a "
        "surcharge, or an off-hours favour - none exist. Only these two things "
        "move a guest forward: what IS available now, and the time the dish they "
        "want comes back. If they are unhappy after that, offer to pass it to "
        "the team - do not agree to serve it."
    )

    if baseline_open:
        shut = []
        for key, period_names in table.items():
            ok, served, when = _describe(period_names, periods, now_m)
            if not ok:
                shut.append({"item": original.get(key, key), "served": served, "returns": when})
        if not shut:
            return {}
        shut.sort(key=lambda r: r["item"])
        # EVERY DISH THAT HAS ANY RESTRICTION AT ALL, open or shut.
        #
        # The closed list alone only ever says which dishes ARE shut; "this dish
        # has no timing" then has to be inferred from absence, and the model would
        # not make that leap - asked for a Filter Coffee (all-day, on no closed
        # list, carrying no timing of its own) it answered "available only during
        # breakfast hours, 7:00 AM to 10:30 AM" across five separate runs. The
        # breakfast combos list their contents ending "Tea / Coffee", and it
        # bridged the two.
        #
        # A COUNTABLE LIST BEATS AN INFERENCE. Naming all 27 dishes that carry any
        # restriction turns "Filter Coffee has no timing" from something to deduce
        # into something to look up and fail to find.
        # EACH ONE CARRIES ITS VERDICT. Listing the names alone was not enough and
        # was actively harmful: naming Tandoori Murgh as "restricted" had the model
        # treat it as CLOSED and recite hours at 13:37, inside its own 12-3 window.
        # Restricted does not mean shut - it means "this one has hours, and here is
        # what they say right now".
        every = []
        for key in sorted(table, key=lambda k: original.get(k, k)):
            _sp: list[tuple[int, int]] = []
            for _n in table[key]:
                _sp.extend(periods.get(_n, []))
            # An all-day entry is in the table to be affirmed, not restricted - it
            # belongs in the menu annotation, not in a list of dishes with hours.
            if _sp == (baseline or []):
                continue
            ok, served, when = _describe(table[key], periods, now_m)
            row = {"item": original.get(key, key), "available_now": ok}
            if not ok:
                row["served"] = served
                row["returns"] = when
            every.append(row)
        _open_n = sum(1 for r in every if r["available_now"])
        return {
            "as_of": _fmt(now_m),
            "mode": "these_are_NOT_available_now",
            "items": shut,
            "every_dish_that_has_any_serving_restriction": every,
            "rule": (
                f"`every_dish_that_has_any_serving_restriction` is the COMPLETE list "
                f"of the {len(every)} dishes on this menu that have serving hours at "
                f"all, and each says whether it can be ordered RIGHT NOW - "
                f"{_open_n} of them currently can. Being on that list does NOT mean "
                "unavailable; read its `available_now`. EVERY DISH NOT ON THAT LIST - "
                "every other drink, coffee, dessert, curry, bread and burger - has NO "
                "serving hours whatsoever and can be ordered at any time. Before "
                "telling a guest anything is unavailable, FIND ITS NAME IN THAT LIST "
                "AND READ ITS `available_now`; if the name is not there at all, it is "
                "available and you simply take the order. Never borrow hours from "
                "something merely similar - a breakfast set listing 'Tea / Coffee' "
                "among its contents says nothing about the coffees sold separately. "
                "`items` is the same information for the shut ones only. "
                + rule_never_refuse
            ),
        }

    open_now = sorted(
        original.get(key, key)
        for key, period_names in table.items()
        if _describe(period_names, periods, now_m)[0]
    )
    return {
        "as_of": _fmt(now_m),
        "mode": "ONLY_these_are_available_now",
        "items": open_now,
        "rule": (
            "The kitchen is on its limited overnight menu. ONLY the dishes listed "
            "here can be ordered right now; anything else on the menu is served "
            "again later in the day. " + rule_never_refuse
        ),
    }
