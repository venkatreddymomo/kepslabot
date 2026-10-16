"""What is the ticket-edit classifier actually being asked?

It flagged "this is about a locked food order" on 15 turns out of 15 and was wrong
every time, including on "what time is checkout?". This captures the REAL system
prompt and payload it receives - the call is not written to new_bot_log_llm, so
this is the only way to see it - and then asks it the exact messages that misfired.
"""
import asyncio
import io
import json
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

from services import llm_orchestration_service as M
from services.ticketing_service import ticketing_service as TS

S = M.llm_orchestration_service
CAPTURED = {}


async def _spy(*, messages, model="", temperature=0.0, max_tokens=0, trace_context=None, **kw):
    """Capture what the classifier was given, then answer nothing so the caller bails."""
    CAPTURED["messages"] = messages
    return {}


class _Ctx:
    hotel_code = "sarovar_portico_sriperumbudur"
    session_id = "probe"
    pending_data: dict = {}
    pending_action = ""


FAKE_TICKETS = [
    {"ticket_ref": "10934", "issue": "In-room dining order for room 1400: 2 Chicken Burger",
     "category": "In-Room Dining", "sub_category": "in-room_dining",
     "description": "Food order", "room_number": "1400",
     "created_at": "2026-08-21T10:00:00+00:00"},          # OLD -> locked
    {"ticket_ref": "10939", "issue": "Table Reservation: Outlet: SPR Kitchen; Guests: 4",
     "category": "In-Room Dining", "sub_category": "table reservation",
     "description": "Table booking", "room_number": "1400",
     "created_at": "2026-08-21T12:25:00+00:00"},
]


async def main():
    S._chat_with_json = _spy          # type: ignore[assignment]
    # Get past the two cheap gates so the classifier is actually reached.
    TS.session_has_known_tickets = lambda *_a, **_k: True
    async def _fake_list(*_a, **_k):
        return FAKE_TICKETS
    TS.list_session_tickets = _fake_list
    try:
        await S._maybe_handle_ticket_edit(
            context=_Ctx(), user_message="what time is checkout?",
        )
    except Exception as ex:
        print("(call bailed as expected: {})".format(str(ex)[:70]))

    msgs = CAPTURED.get("messages")
    if not msgs:
        print("\nThe classifier was never reached — it is gated before the LLM call.")
        print("That itself is the finding: with no committed ticket it does not run.")
        return

    sysmsg = "\n".join(m["content"] for m in msgs if m.get("role") == "system")
    print("\nsystem prompt: {} chars".format(len(sysmsg)))

    # How much of it is about the locked case vs everything else?
    low = sysmsg.lower()
    marks = ["locked", "already being prepared", "changeable", "kitchen"]
    lines = sysmsg.split("\n")
    locked_chars = sum(len(l) for l in lines if any(m in l.lower() for m in marks))
    print("lines mentioning locked/kitchen/changeable: {} chars ({:.0f}% of the prompt)".format(
        locked_chars, 100.0 * locked_chars / max(1, len(sysmsg))))
    print("\n--- the loudest lines ---")
    for l in lines:
        if any(m in l.lower() for m in marks):
            print("   " + l.strip()[:150])


asyncio.run(main())


def interrogate():
    """Ask the REAL classifier, with its REAL prompt, the messages that misfired."""
    import json as _j
    from openai import OpenAI
    key = [l.split("=", 1)[1].strip() for l in open(".env", encoding="utf-8")
           if l.startswith("OPENAI_API_KEY=")][0]
    client = OpenAI(api_key=key)
    msgs = CAPTURED["messages"]
    sysmsg = [m for m in msgs if m.get("role") == "system"]
    base = _j.loads([m for m in msgs if m.get("role") == "user"][0]["content"])

    CASES = [
        ("what time is checkout?", "nothing to do with any ticket"),
        ("thanks, that's all for now!", "not a request at all"),
        ("can you change the venue to Chola Lounge instead of SPR Kitchen?", "TABLE booking"),
        ("i need extra towels", "new housekeeping request"),
        ("cancel my whole order", "genuinely about the locked food order"),
    ]
    print("\n=== what the classifier answers (locked food 10934 + open table 10939) ===")
    for text, note in CASES:
        p = dict(base)
        p["user_message"] = text
        r = client.chat.completions.create(
            model="gpt-4o-mini", temperature=0.0, max_tokens=200,
            response_format={"type": "json_object"},
            messages=sysmsg + [{"role": "user", "content": _j.dumps(p, ensure_ascii=False)}],
        )
        d = _j.loads(r.choices[0].message.content or "{}")
        print("\n  {!r}  ({})".format(text, note))
        print("     is_edit                = {}".format(d.get("is_edit")))
        print("     is_locked_order_request= {}".format(d.get("is_locked_order_request")))
        print("     locked_ticket_ref      = {!r}".format(d.get("locked_ticket_ref")))
        print("     ticket_ref             = {!r}".format(d.get("ticket_ref")))
        print("     reply                  = {!r}".format(str(d.get("reply"))[:95]))


interrogate()
