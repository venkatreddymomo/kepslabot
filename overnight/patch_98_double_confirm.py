"""4.1 - the double-confirm loop.

The server only ever asks ONE question ("Shall I place this order?", rendered by
_render_ird_summary with the allergens listed directly above it). The prompt told
the model to wait for TWO confirmations, so the guest's "yes" - which answers the
only question actually asked - never satisfied the rule, ticket.ready_to_create
was never set, and is_summary_turn re-rendered the same question. Loop.

Make the prompt agree with the renderer: one question, one yes.
"""
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from sqlalchemy import create_engine, text

SERVICE_ID = 98

# (anchor, replacement) - each anchor MUST appear exactly once.
EDITS = [
    (
        '2. ALLERGENS: for EVERY item, state its allergens exactly as in the menu '
        '(say "no listed allergens" where the menu\'s allergen line is None / None '
        'mentioned). Then explicitly ask the guest to confirm they\'re okay with these '
        'allergens.',

        '2. ALLERGENS: for EVERY item, state its allergens exactly as in the menu '
        '(say "no listed allergens" where the menu\'s allergen line is None / None '
        'mentioned). LIST them above your closing question - do NOT ask a separate '
        '"are you okay with these allergens?" question. The guest reads them right '
        'before they answer, so the one closing question covers them.',
    ),
    (
        "- Only AFTER the guest explicitly confirms BOTH (a) the order is complete and "
        "correct and (b) they're fine with the allergens do you place it. Do NOT place "
        "the order, and do NOT mention any ticket/backend process, before that "
        "confirmation.",

        "- ONE QUESTION, ONE YES. You ask exactly one closing question - \"Shall I "
        "place this order?\" - with the items, total and allergens listed directly "
        "above it. A plain agreement to THAT question (\"yes\", \"place it\", \"go "
        "ahead\", \"confirm\") is the guest confirming the order AND the allergens "
        "together, and you place it on THAT SAME TURN. NEVER wait for a second "
        "confirmation and NEVER re-ask a question the guest has already answered - "
        "re-asking reads as though you did not hear them. Do NOT place the order, and "
        "do NOT mention any ticket/backend process, before that one confirmation.",
    ),
]


def main() -> int:
    url = [l.split("=", 1)[1].strip() for l in open(".env", encoding="utf-8")
           if l.startswith("DATABASE_URL=")][0].replace("mysql+aiomysql", "mysql+pymysql")
    engine = create_engine(url, connect_args={"connect_timeout": 10})

    with engine.begin() as conn:
        row = conn.execute(text(
            "SELECT generated_system_prompt, generated_system_prompt_override "
            "FROM new_bot_services WHERE id=:i"), {"i": SERVICE_ID}).first()
        if row is None:
            print("FAIL: service {} not found".format(SERVICE_ID))
            return 1
        prompt = row._mapping["generated_system_prompt"]
        lock = row._mapping["generated_system_prompt_override"]
        before = len(prompt)

        for idx, (anchor, replacement) in enumerate(EDITS, 1):
            count = prompt.count(anchor)
            if count != 1:
                print("FAIL: edit {} anchor found {} times (need exactly 1)".format(idx, count))
                print("      anchor starts: {!r}".format(anchor[:90]))
                return 1
            prompt = prompt.replace(anchor, replacement)
            print("edit {} applied".format(idx))

        # The lock must survive, or the prompt auto-regenerates and the fix is gone.
        conn.execute(text(
            "UPDATE new_bot_services SET generated_system_prompt=:p, "
            "generated_system_prompt_override=1 WHERE id=:i"),
            {"p": prompt, "i": SERVICE_ID})
        print("\nservice {} prompt {} -> {} chars, override stays 1 (was {})".format(
            SERVICE_ID, before, len(prompt), lock))

    # Read it back from the DB rather than trusting the in-memory string.
    with engine.connect() as conn:
        saved = conn.execute(text(
            "SELECT generated_system_prompt, generated_system_prompt_override "
            "FROM new_bot_services WHERE id=:i"), {"i": SERVICE_ID}).first()._mapping
        ok = True
        for idx, (anchor, replacement) in enumerate(EDITS, 1):
            if anchor in saved["generated_system_prompt"]:
                print("VERIFY FAIL: edit {} old text still present".format(idx))
                ok = False
            if replacement not in saved["generated_system_prompt"]:
                print("VERIFY FAIL: edit {} new text absent".format(idx))
                ok = False
        if int(saved["generated_system_prompt_override"] or 0) != 1:
            print("VERIFY FAIL: override lock is not 1")
            ok = False
        print("VERIFIED IN DB" if ok else "VERIFY FAILED")
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
