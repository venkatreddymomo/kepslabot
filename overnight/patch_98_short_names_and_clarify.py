"""Two prompt rules for service 98, both from the 50-turn run.

3) SHORT DISH NAMES. The guest said "add a filter coffee". The menu calls it
   "Filter Coffee (South Indian Style)". The bot replied "Filter Coffee is not on
   the menu, but we do have Filter Coffee (South Indian Style)" and then asked an
   extra question to confirm - so the guest's "yes" bought them a second question
   instead of an order, and the coffee never reached the kitchen. One naming
   difference, two failures.

4) WHEN A REPLY MAKES NO SENSE, ASK. A bare "yes" arrived when nothing was
   pending (the order had just been placed). Rather than guess, the bot should
   say what it thinks they mean and ask.

Both are rules the model can apply by reading the menu and the conversation in
front of it - no word lists.
"""
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parent.parent
ANCHOR = "- NEVER TURN \"NOT RIGHT NOW\" INTO \"WE DON'T HAVE IT\"."

NEW = """- A SHORTER NAME IS THE SAME DISH. Guests say "filter coffee", not "Filter Coffee (South Indian Style)"; "dosa", not "Dosa (Plain or Masala)"; "paneer tikka", not "Jaitooni Paneer Tikka". When what they said clearly points at one dish on the menu, THAT IS THE DISH. Use the full menu name in your reply and carry straight on with the order. NEVER say a dish "is not on the menu" because the guest used a shorter or slightly different name for it - it is on the menu, and saying otherwise tells them the hotel does not serve something it does. Do not spend a turn asking them to confirm a name when only one dish it could mean exists; only ask when their words genuinely fit two or more different dishes, and then name those dishes.
- IF WHAT THEY SAID DOES NOT MAKE SENSE HERE, ASK - DO NOT GUESS. A bare "yes", "ok" or "that one" when you have not asked anything, or a message you cannot place in this conversation, means you are missing something. Say briefly what you think they might mean and ask - "Did you mean you'd like to add something else to your order?" Never answer such a message by listing what services you offer, and never invent a question they did not ask. One short clarifying question is always better than a confident wrong answer.
"""


def main() -> int:
    url = [l.split("=", 1)[1].strip() for l in open(ROOT / ".env", encoding="utf-8")
           if l.startswith("DATABASE_URL=")][0].replace("mysql+aiomysql", "mysql+pymysql")
    eng = create_engine(url, connect_args={"connect_timeout": 10})
    with eng.connect() as c:
        prompt = str(c.execute(text(
            "SELECT generated_system_prompt FROM new_bot_services WHERE id=98")).scalar() or "")
    print(f"current: {len(prompt)} chars")

    if "A SHORTER NAME IS THE SAME DISH" in prompt:
        print("already applied - nothing to do")
        return 0
    i = prompt.find(ANCHOR)
    if i < 0:
        print(f"ABORT: anchor not found: {ANCHOR!r}")
        return 2

    new_prompt = prompt[:i] + NEW + prompt[i:]
    assert "A SHORTER NAME IS THE SAME DISH" in new_prompt
    assert "DOES NOT MAKE SENSE HERE, ASK" in new_prompt
    assert ANCHOR in new_prompt
    assert len(new_prompt) > len(prompt)

    with eng.begin() as c:
        c.execute(text("UPDATE new_bot_services SET generated_system_prompt=:p, "
                       "generated_system_prompt_override=1 WHERE id=98"), {"p": new_prompt})
    with eng.connect() as c:
        row = dict(c.execute(text(
            "SELECT generated_system_prompt, generated_system_prompt_override "
            "FROM new_bot_services WHERE id=98")).first()._mapping)
    ap = str(row["generated_system_prompt"] or "")
    ok = ("A SHORTER NAME IS THE SAME DISH" in ap
          and "DOES NOT MAKE SENSE HERE, ASK" in ap
          and int(row["generated_system_prompt_override"] or 0) == 1)
    print(f"written: {len(ap)} chars, LOCK={row['generated_system_prompt_override']}")
    print("VERIFIED OK" if ok else "VERIFY FAILED")
    print("\nRESTART the backend.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
