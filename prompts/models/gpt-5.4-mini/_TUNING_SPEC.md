# gpt-5.4-mini prompt set — design notes

## Status: READY (authored locally; push to DB when write access is available)

All 20 keys in this folder are the 5.4 variant set. They start as Zone's live content
(extracted from the 2026-06-30 freeze snapshot — the DB was unreachable at authoring
time) so they are behaviourally identical to 4.1, EXCEPT the two behaviour-critical
prompts, which have a **5.4 operating contract** prepended at the very top:

- `orchestrator.book_answerer.md` — a "5.4 OPERATING CONTRACT" block (answer-length
  floor, cover-every-property, KB-only grounding, don't-decline-info). Fixes the 5.4
  terseness / one-liner failures and the "pet friendly → none" style misses.
- `orchestrator.main_router.md` — a "5.4 SCOPE CONTRACT" block (fleet-attribute broaden,
  compare-union, recall). Front-loads the scope intent 5.4 was skimming.

## Why this shape (not a ground-up rewrite)

5.4 is a capable but terse instruction-follower that **skims rules buried deep in a long
prompt** and **defaults to one-liners**. The current prompts are long and repetitive
(built over ~90 revisions of edge-case fixes). Two facts drove the design:

1. The fix 5.4 needs is **salience**, not new rules: the same rules, stated imperatively
   and placed at the TOP (5.4 weights the opening heavily) + marked "overrides on
   conflict". That is the operating-contract block.
2. The long tail of the prompt encodes many hard-won safety / phase / edge-case fixes.
   **Deleting them blindly (untested) would regress those.** So the contracts stay intact
   below; we slim later, WITH a test pass, not before one.

## Deliberately deferred (do WITH live testing, not before)

- **Aggressive slimming** of the ~86k book_answerer / ~56k main_router bodies (removing
  duplicate "WRONG (verbatim failure)" examples, consolidating restated rules). This is
  where the "current prompt is too much" win is — but each cut needs a test to confirm no
  regression. Do it iteratively once 5.4 is live and testable.
- **Scope rules #1-4 depend on a shared code block.** The router's FLEET-ATTRIBUTE /
  compare / recall rules are ALSO appended in Python (`_resolve_main_route_with_llm`),
  identical for both models. The 5.4 SCOPE CONTRACT front-loads the intent in the DB row,
  which usually suffices — but if live 5.4 testing still misses, move that specific code
  block into a model-variant registry key (prompt-only, decided after testing).
- **`reasoning_effort` / `verbosity`** for gpt-5.x in `llm/client._adapt_kwargs_for_model`
  (code change; latency + length lever) — out of scope for this prompts-only batch.

## Test protocol (after pushing to DB + restart, on 5.4)

Replay the failing transcript: Digha intro → "is this nice for a family" → Jaipur →
"give me details" (must be 3-6 fact bullets, NOT a label) → "compare this with the
previous hotel" (must cover both) → "what's the first hotel we discussed" (must name
Digha) → "are any of your hotels pet friendly" (must broaden to the fleet, not "none") →
"which has a rooftop pool" (must find Kolkata). 3 counter-examples each. Confirm 4.1 is
unchanged (select 4.1 → identical to today).
