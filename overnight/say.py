"""Say one thing to the bot and see what the server actually holds. No browser.

    python overnight/say.py "can i get 2 chicken burgers"
    python overnight/say.py --new "hi"        # start a fresh guest
    python overnight/say.py --book            # just show the book, say nothing

Posts straight to /api/chat/message, so a turn takes as long as the bot takes and
nothing else. The session id is kept in overnight/_chat/session.txt so successive
calls are the same guest talking.

Every turn prints the reply AND the order book row, because the reply is not
evidence - the whole question is whether the server holds what the guest was
told, and those came apart often enough to build a table over it.
"""
from __future__ import annotations

import datetime
import io
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parent.parent
BASE = os.environ.get("BASE_URL", "http://localhost:8011")
HOTEL = os.environ.get("HOTEL_CODE", "sarovar_portico_sriperumbudur")
BOX = ROOT / "overnight" / "_chat"
BOX.mkdir(parents=True, exist_ok=True)
SESSION_FILE = BOX / "session.txt"
LOG = BOX / "manual_log.txt"

_URL = [l.split("=", 1)[1].strip() for l in open(ROOT / ".env", encoding="utf-8")
        if l.startswith("DATABASE_URL=")][0].replace("mysql+aiomysql", "mysql+pymysql")
ENG = create_engine(_URL, connect_args={"connect_timeout": 15}, pool_pre_ping=True)


def new_session() -> str:
    sid = "session_%d_manual" % int(datetime.datetime.now().timestamp() * 1000)
    SESSION_FILE.write_text(sid, encoding="utf-8")
    return sid


def session_id() -> str:
    if SESSION_FILE.exists():
        s = SESSION_FILE.read_text(encoding="utf-8").strip()
        if s:
            return s
    return new_session()


def book(sid: str) -> str:
    """The row as the server holds it, plus the tickets the kitchen received."""
    try:
        with ENG.connect() as c:
            row = c.execute(text(
                "SELECT * FROM new_bot_ird_guest_orders WHERE hotel_code=:h "
                "AND session_id=:s"), {"h": HOTEL, "s": sid}).mappings().first()
            tks = c.execute(text(
                "SELECT ticket_ref, description FROM new_bot_tickets "
                "WHERE session_id=:s ORDER BY id"), {"s": sid}).mappings().all()
    except Exception as exc:
        return "  (book unreadable: %r)" % exc
    lines: list[str] = []
    if row:
        for n in range(1, 11):
            cart = row.get("cart_%d" % n)
            if isinstance(cart, str):
                try:
                    cart = json.loads(cart)
                except Exception:
                    cart = {}
            if not cart or not (cart or {}).get("items"):
                continue
            items = ", ".join("%s x%s" % (i.get("item"), i.get("qty"))
                              for i in cart["items"])
            extra = ""
            if cart.get("note"):
                extra += "  note=%r" % cart["note"]
            if row.get("ticket_ref_%d" % n):
                extra += "  ticket=%s" % row.get("ticket_ref_%d" % n)
            if row.get("placed_at_%d" % n):
                extra += "  at=%s" % str(row.get("placed_at_%d" % n))[11:19]
            lines.append("  slot %d [%s] %s%s"
                         % (n, row.get("status_%d" % n), items, extra))
    if not lines:
        lines.append("  (book empty)")
    for t in tks:
        lines.append("  TICKET %s: %s" % (t["ticket_ref"], t["description"]))
    return "\n".join(lines)


def say(sid: str, msg: str) -> str:
    body = json.dumps({"session_id": sid, "message": msg, "hotel_code": HOTEL,
                       "channel": "web",
                       "metadata": {"phase": "during_stay"}}).encode("utf-8")
    req = urllib.request.Request("%s/api/chat/message" % BASE, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return "HTTP %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:400])
    except Exception as exc:
        return "REQUEST FAILED: %r" % exc
    return str(data.get("display_message") or data.get("message") or "")


def main() -> int:
    args = [a for a in sys.argv[1:]]
    if "--new" in args:
        args.remove("--new")
        sid = new_session()
        print("new guest: %s" % sid)
    else:
        sid = session_id()
    if "--book" in args:
        print("session: %s\nBOOK:\n%s" % (sid, book(sid)))
        return 0
    if not args:
        print("nothing to say")
        return 2
    msg = " ".join(args)
    reply = say(sid, msg)
    block = "GUEST: %s\nBOT:\n%s\nBOOK:\n%s" % (msg, reply, book(sid))
    print(block)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write("[%s] %s\n%s\n\n" % (
            datetime.datetime.now().isoformat(timespec="seconds"), sid, block))
    return 0


if __name__ == "__main__":
    sys.exit(main())
