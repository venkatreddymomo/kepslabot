"""Edge-case conversation — the shapes the standard 50-turn run never exercises.

Deliberately covers what we have NOT tested:
  • "no" / declines (every previous run only ever said yes)
  • quantity changes, removing one item while adding another
  • ordering the same dish twice (duplicate handling)
  • vegetarian preference vs non-veg suggestions (the veg safety net)
  • allergy and price questions
  • cancelling the WHOLE order vs one item
  • changing a booking twice
  • trying to change an order after the kitchen window has passed
  • ambiguous "ok", typos, several requests in one message
  • asking for things the hotel does not do
"""

EDGE_TURNS = [
    # --- decline paths: never tested before ---
    "hi",
    "Order food",
    "whats good for dinner",
    "no thanks, i'll decide myself",
    "show me the vegetarian options",
    "one paneer butter masala",              # veg guest from here on
    "actually do you have butter chicken",   # non-veg question from a veg guest
    "no, keep it vegetarian",
    # --- quantities and edits ---
    "make it 3 naans instead of 2",
    "actually remove the naan and add a dosa",
    "add another paneer butter masala",      # duplicate of an existing line
    "no wait, cancel that second one",
    # --- questions, not orders ---
    "whats in the paneer butter masala",
    "how much is it",
    "does it contain nuts",
    "im allergic to dairy, is it safe",
    # --- confirm / decline the order itself ---
    "ok",                                    # ambiguous single word
    "no not yet",
    "actually yes place it now",
    # --- after placing: locked window + recall ---
    "what did i order",
    "can i add a dessert to that order",
    "actually cancel my whole order",
    "what did i order",
    # --- table booking, changed twice ---
    "book a table for 2 tomorrow at 7pm",
    "actually make it 8pm",
    "no make it 9pm and 4 people",
    "actually cancel the table booking",
    # --- multi-request in one message ---
    "i need towels and the ac fixed",
    "yes",
    # --- typos and informal ---
    "can i get sm wtr in my room pls",
    "yes",
    # --- things the hotel does not do ---
    "can you book me a flight to delhi",
    "can you get me cigarettes",
    "do you have a swimming pool",
    # --- politeness / closing that is not an order ---
    "no thats all thanks",
    "actually one more thing",
    "whats the checkout time",
    "no i meant late checkout",
    # --- language + recall at the end ---
    "mujhe apna order dubara batao",
    "ok english",
    "did my ac get fixed",
    "thanks bye",
]
