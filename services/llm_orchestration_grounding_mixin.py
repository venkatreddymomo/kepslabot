"""LLMOrchestrationGroundingMixin: methods extracted from llm_orchestration_service.py (behavior-preserving).

Moved verbatim (47 methods). LLMOrchestrationService inherits this mixin, so ``self.*``
resolution is unchanged. This module never imports llm_orchestration_service — no cycle.
"""
from __future__ import annotations
import asyncio
import time as _time
import json
import logging
import re
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any
from config.settings import settings
from llm.client import llm_client
from services.geo_distance_service import geo_distance_service
from services.google_places_service import google_places_service
from services.web_lookup_service import web_lookup_service
from schemas.chat import ConversationContext
from schemas.orchestration import OrchestrationDecision
from services.config_service import config_service
from services.form_mode_service import (
    strip_form_confirmation_instructions,
)
from services.prompt_registry_service import (
    PromptMissingError,
    prompt_registry,
)
from services.llm_call_logging import _log_llm_call

logger = logging.getLogger("services.llm_orchestration_service")


@lru_cache(maxsize=8)
def _ird_menu_item_types(menu_text: str) -> dict[str, str]:
    """Every dish the menu names, mapped to the `Type:` the MENU gives it.

    The menu writes each item as its name, then a `Type:` line a line or two below:

        Tandoori Murgh

        Type: Non Veg
        Price: ₹850

    So the answer to "is this vegetarian" is already written down for every dish,
    and nothing has to be inferred from the dish's name. Cached because the menu is
    ~41k chars and only changes when the KB does.
    """
    out: dict[str, str] = {}
    prev = ""
    for raw in str(menu_text or "").split("\n"):
        line = raw.strip()
        if line.lower().startswith("type:"):
            kind = line.split(":", 1)[1].strip().lower()
            if prev and kind:
                # First spelling wins: a name repeated under a page header is the
                # same dish, and the repeat carries the same Type.
                out.setdefault(prev.lower(), kind)
        if line:
            prev = line
    return out





# A reply asserting the order has gone to the kitchen.
_CLAIMS_PLACED = re.compile(
    r"(?:has been|is|are|was|were)\s+(?:now\s+)?placed|order placed"
    r"|on (?:its|their|the) way|sent to the kitchen|being prepared"
    r"|confirmed and placed", re.I)


class LLMOrchestrationGroundingMixin:
    @classmethod
    def _kb_model_context_window(cls, model_name: str | None = None) -> int:
        """Return the active model's context window in tokens. Falls back to
        a safe 128k default for unknown OpenAI models."""
        candidate = (model_name or "").strip().lower()
        if not candidate:
            candidate = str(getattr(settings, "openai_model", "") or "").strip().lower()
        if not candidate:
            return 128_000
        # Strip date suffixes / version pins (e.g. gpt-4o-2024-08-06 → gpt-4o).
        base = candidate
        for known in cls._OPENAI_CONTEXT_WINDOW_TOKENS:
            if candidate.startswith(known):
                base = known
                break
        return cls._OPENAI_CONTEXT_WINDOW_TOKENS.get(base, 128_000)

    @classmethod
    def _compute_kb_char_budget(cls, model_name: str | None = None) -> int:
        """Phase 5: adaptive character budget for KB content in one turn.

        budget_tokens = model_context_window - reserved_tokens
        budget_chars  = budget_tokens * 4    (rough avg English chars/token)

        Reserved tokens cover: system prompt, conversation history, FAQ block,
        response reservation, and a 5% safety margin. Configurable via
        settings.llm_context_reserved_tokens.
        """
        window_tokens = cls._kb_model_context_window(model_name)
        reserved = max(2_000, int(getattr(settings, "llm_context_reserved_tokens", 19_000) or 19_000))
        available_tokens = max(2_000, window_tokens - reserved)
        # 4 chars/token is a common conservative estimate for English text.
        return available_tokens * 4

    @staticmethod
    def _out_of_scope_guardrail_directive(pre_arrival: bool = False) -> str:
        """Recency-placed, PROPERTY-AGNOSTIC scope guardrail (applies to every
        hotel + every phase). The bot is a hotel assistant only: it must politely
        decline anything unrelated to THIS hotel or the guest's stay (jokes,
        maths, general knowledge, world affairs, other businesses) instead of
        answering it — and it must respond to a self-harm / health-crisis message
        with care + a pointer to professional help, never a hotel handoff. Written
        as judgement guidance (not an exhaustive list) so it generalises without
        hardcoding every case."""
        return (
            "=== YOU ARE STRICTLY THIS HOTEL'S ASSISTANT — NOTHING ELSE (read late, obey) ===\n"
            "You exist ONLY to help with THIS hotel and the guest's own stay/booking/trip — its rooms, amenities, dining, services, policies, their reservation, and getting to / around the hotel and its local area. You are NOT a general chatbot, search engine, tutor, calculator or entertainer. If a message is not about this hotel or the guest's stay, you do NOT answer it — full stop, no matter how simple or how insistently they ask. Use your JUDGEMENT; don't try to enumerate every case.\n"
            "1) PURE OFF-TOPIC — general knowledge, trivia, jokes/riddles, maths or calculations, coding, world affairs, politics, geography/capitals, sports scores, celebrity facts, opinions, homework: reply with ONE short warm line that you can't help with that (you're just the hotel's assistant, here for their stay) and, if it fits, nudge back to how you CAN help. Do NOT answer it, invent, open a form, or offer a handoff. This INCLUDES jokes, riddles, puns and 'fun facts' — NEVER actually tell or compose one, not even a short or 'harmless' one, and never as a lead-in before redirecting; no matter how nicely or repeatedly the guest asks, do not produce joke content at all. Being playful and giving them the joke is a FAILURE. THIS IS A HARD RULE WITH ZERO EXCEPTIONS: for 'tell me a joke', 'make me laugh', 'a fun fact', 'say something funny', 'got any jokes' — your ONLY correct output is the one-line decline + redirect, with NOT A SINGLE word of joke / riddle / pun / fun-fact content anywhere in the reply. Outputting any joke content at all = a hard failure, even if you then redirect. Always decline and redirect instead — e.g. 'Ha — I'll leave the jokes to the comedians; I'm just here to help with your stay. Anything I can do for you at the hotel?'.\n"
            "2) A LOCAL SERVICE OR ARRANGEMENT THE HOTEL DOESN'T DIRECTLY PROVIDE — e.g. an outside taxi/cab, a ride somewhere the hotel doesn't shuttle to, a local errand, a local table reservation: do NOT invent any details and do NOT flatly refuse. Say plainly you don't have information on that and can't arrange it directly yourself, then OFFER to pass the request to our staff and raise it — e.g. 'I don't have details on that and can't arrange it directly, but I can pass this to our team and raise a request for you — would you like me to?'. Their 'yes' opens the GENERAL staff-request form (the support/'talk to our team' form), NOT a specific service form. THE TEST for whether this handoff is even warranted: could the hotel's OWN staff realistically arrange or help with it LOCALLY, as part of hosting this guest? If yes → offer the handoff. If it is instead a commercial booking a hotel would NEVER transact on a guest's behalf — booking a FLIGHT, a stay at a DIFFERENT / other hotel, train/bus/event/movie tickets, online shopping, another company's product — then it is pure off-topic: handle it under (1) as a PLAIN one-line decline, with NO handoff and NO form. SHARP LINE (apply it): a local RIDE the hotel could put a cab/driver on (to a temple, mall, market, station, the airport) is arrange-able → offer the handoff; but BUYING A TICKET on someone else's service — a TRAIN ticket, bus ticket, flight, or movie/event ticket — or any purchase from another business is NOT something a hotel sells → PLAIN decline, NEVER a handoff. So 'a cab to the temple' = handoff, but 'a train ticket' = decline. NOTE: the hotel's OWN services are different — e.g. its own airport/railway shuttle, housekeeping, dining, maintenance — route those to that service as normal, not here.\n"
            "3) THE ONE EXCEPTION — a message about self-harm, suicide, or an urgent physical or mental-health crisis: do NOT brush it off and do NOT give medical advice. Respond briefly, gently and seriously, and urge them to reach out right now to local emergency services or a qualified professional / crisis helpline. This is human care, not a hotel task — no form, no department, no ticket.\n"
            "4) DON'T OVER-DECLINE: a question that IS about this hotel, the guest's booking/stay, or the local area around the hotel (directions, nearby places, how to reach the hotel) is IN scope — answer it normally from the KB.\n"
            # ── ANYTHING THAT COULD HURT SOMEONE IS NOT OURS TO ARRANGE ──────
            # There was no rule about this ANYWHERE - not in code, not in any
            # prompt - so the same request went three ways in one session: "I
            # need knife" raised ticket 11409 "Deliver a knife to the guest in
            # room 1400", the next ask opened a form, and a third time it
            # refused. The refusals were the base model's own instinct, not
            # anything the hotel had decided, which is exactly why it flipped.
            "5) ANYTHING UNSAFE — NEVER ARRANGE IT, NEVER RAISE A TICKET FOR IT. If the guest asks for something that could be used to hurt someone or themselves, or that is plainly unsafe or not a hotel's to hand over — a weapon of any kind, a knife or blade, a tool as a weapon, a lighter or fuel to start a fire, alcohol for a minor, medication or drugs, anything illegal — you do NOT arrange it, do NOT add it to an order, do NOT open a form for it and do NOT raise a request with any team. Decline in ONE short, calm, non-judgemental line and offer what you CAN help with — e.g. 'That's not something I can arrange, but I'm happy to help with anything else for your stay.' Use JUDGEMENT rather than a list: the test is whether handing it over could cause harm. Ordinary room-service cutlery that ARRIVES WITH A MEAL is part of that meal and is not a request for a weapon; a knife asked for on its own is. If you are unsure, decline gently and offer to connect them to the front desk — a person can judge it. NEVER explain how to obtain such an item, and never soften the decline into a promise that someone will bring it.\n"
            # ── BEFORE ARRIVAL, NOTHING IN THE ROOM IS ARRANGEABLE ───────
            # The router already refuses to ROUTE these, but the REPLY was
            # never told, so it kept saying "Sure - I can help with the
            # wake-up call" and "the 5:00 AM wake-up call is noted". A
            # downstream net then read those sentences and raised real
            # tickets to make them true, for a stay a year away. The words
            # are where this has to stop.
            + ("6) THE GUEST HAS NOT CHECKED IN YET. Anything that can only happen once they are physically in the hotel with a room - a wake-up call, laundry or pressing, housekeeping, room service / in-room dining, an in-room fault, an in-room treatment, an item delivered to the room - CANNOT be arranged, scheduled, noted or passed to a team now. Do NOT say 'noted', 'done', 'I have arranged that', 'I will let the team know' or anything that sounds like it has been actioned, because nothing has been. Say warmly that it is handled once they check in - e.g. 'Once you are checked in, the team can set that up for you.' Things that genuinely CAN be done before arrival - their booking, early check-in, airport pickup, questions about the hotel or its policies - are unaffected: handle those normally.\n"
               if pre_arrival else "")
        )

    @staticmethod
    def _router_says_off_topic_directive() -> str:
        """Sent ONLY on a turn the router itself classified as off-topic.

        The difference from the two scope directives above is not the wording, it
        is who decided. Those ask the answering model to judge its own guest's
        message and then decline it - and it will not, reliably. "Tell me a joke"
        was answered with a joke under both, because a model mid-conversation with
        a guest is pulled to be helpful and an instruction in its own prompt is
        just one more voice against that pull.

        Here the judgement is already made, by a model that is not in the
        conversation. This does not ask for a decision. It states one and says
        what to write, and it is the last thing read.
        """
        return (
            "=== THIS MESSAGE IS OFF-TOPIC — ALREADY DECIDED, DO NOT RE-JUDGE ===\n"
            "This message is not about this hotel, the guest's stay or booking, or "
            "the area around the hotel. That call has been made and is not yours to "
            "revisit, however reasonable or harmless answering would feel.\n"
            "WRITE EXACTLY THIS SHAPE AND NOTHING MORE: one short, warm line saying "
            "it is outside what you help with, then one line offering the hotel "
            "instead. Two sentences. In the guest's language.\n"
            "YOU MAY NOT INCLUDE THE THING THEY ASKED FOR — not the joke, not the "
            "riddle, not the pun, not the fun fact, not the answer to the sum, not "
            "the trivia, not a shortened or partial version, and not a 'here's one "
            "anyway' before redirecting. Any of it in the reply is a failure even "
            "if the redirect follows.\n"
            "Do NOT open a form, raise a request, or offer to pass this to staff.\n"
            "ONE EXCEPTION: if the message is about self-harm or a health crisis, "
            "ignore all of the above - reply briefly and kindly and urge them to "
            "contact local emergency services or a professional right now."
        )

    @staticmethod
    def _no_trailing_offer_directive() -> str:
        """Answer-shape rule for Zone: say less, and don't sign off with an offer.

        Both halves of this already existed inside the book_answerer prompt rows
        and neither held. Measured on the live bot, past the 5-minute registry
        cache each time:

          - the sign-off ban was ignored 26 minutes after the edit;
          - "answer short, decide yourself what matters" produced 1,494 chars,
            a full section-by-section readout of dining, amenities and offers.

        The wording was not the problem — placement was. One instruction inside
        a 103k-char prompt is too dilute to compete. The codebase already solves
        this for the language rule and the Sarovar grounding gate by appending a
        short directive near the end, where recency makes it win. Same trick.

        Deliberately no length cap and no list of required sections: a cap made
        the bot drop the line saying what the hotel actually was, and a required
        list is just as rigid. The judgement stays with the model; this only
        tells it to exercise it.
        """
        return (
            "FINAL RULE — HOW TO ANSWER:\n"
            "Answer the question that was asked and nothing more. Do NOT walk "
            "through every section the knowledge base happens to hold on this "
            "property — a guest asking about one thing does not want dining, "
            "amenities, facilities and offers appended to it. Pick what genuinely "
            "matters for this question and this hotel, say it plainly, and leave "
            "the rest for the guest to ask about.\n"
            "Never volunteer a room's square footage or area. Guests choose a room "
            "by who it sleeps, the bed, the view and what makes it different — not "
            "by a number in sq. ft. State the size ONLY when the guest explicitly "
            "asks how big a room is.\n"
            # An arithmetic ban lived here briefly and was removed. It did stop
            # "what is 27 times 43" (3/3 runs, up from roughly half), but it also
            # made the bot refuse "how many nights between 20th and 23rd
            # December?" — a real booking question — even with an explicit
            # exception written for stay arithmetic. Blocking a guest's own
            # date maths is a worse outcome than occasionally answering a times
            # table, so the trade was not worth making.

            "Your last sentence must NOT be an offer to supply more information. "
            "Forbidden endings, in any wording or language: \"Let me know if you'd "
            "like ...\", \"If you want, I can also share/tell/give ...\", \"If you'd "
            "like, I can also ...\", \"I can also help with ...\", \"Would you like "
            "to know more about ...\", \"Feel free to ask ...\". The guest can see "
            "the chips and can ask their own next question. End on the last fact."
        )

    @staticmethod
    async def _db_prompt_text(key: str) -> str:
        """The current hotel's prompt row for `key` (its own row, else the industry
        default), or "" when there is none. Hotels set up in the Pre-booking tab
        keep every rule's wording in the DB, editable from the admin, like the rest
        of their prompts."""
        from services.prompt_registry_service import PromptMissingError, prompt_registry

        try:
            return str(await prompt_registry.get(key) or "").strip()
        except PromptMissingError:
            logger.warning("prompt row %r missing - rule not sent", key)
            return ""
        except Exception as exc:
            logger.warning("prompt row %r unavailable - rule not sent: %r", key, exc)
            return ""

    async def _reply_style_directive(
        self, context: Any, key: str, *, legacy_hotel: str, legacy_text: Any
    ) -> str:
        """The wording of a Pre-booking reply-style rule, or "" when it is off.

        Hotels set up in the Pre-booking tab: switched there, worded by the DB row
        `reply_style.<key>`. Every other hotel keeps the old one-hotel gate and the
        wording in code, unchanged."""
        from services import prebooking_settings_service

        code = str(getattr(context, "hotel_code", "") or "").strip().lower()
        admin = prebooking_settings_service.settings_for(code)
        if admin is None:
            return legacy_text() if code == legacy_hotel else ""
        if not bool((admin.get("reply_style") or {}).get(key, False)):
            return ""
        return await self._db_prompt_text(f"reply_style.{key}")

    async def _admin_emoji_directive(self, context: Any) -> str:
        """Emoji rule for hotels set up in the Pre-booking tab, worded by the DB
        row `reply_style.emoji_none` / `reply_style.emoji_limited` ("allowed"
        sends nothing)."""
        from services import prebooking_settings_service

        admin = prebooking_settings_service.settings_for(getattr(context, "hotel_code", ""))
        emoji = str(((admin or {}).get("reply_style") or {}).get("emoji") or "")
        if emoji in ("none", "limited"):
            return await self._db_prompt_text(f"reply_style.emoji_{emoji}")
        return ""

    @staticmethod
    def _mango_cannot_arrange_directive() -> str:
        """Mango's handoff rule, sent after the general guardrail so it wins.

        Replayed on the logged Mango turns with gpt-4.1-mini: with this rule only
        in the prompt row, "can you book a cab" got "I can pass this to our team"
        in 5/5 runs; sent here, 5/5 gave the hotel's number and stopped."""
        return (
            "=== THIS HOTEL: WHAT YOU CANNOT ARRANGE (read late, obey) ===\n"
            "This hotel's own rule replaces the general 'offer to pass it to our staff' guidance above. "
            "For anything you cannot arrange yourself - a cab or taxi, luggage help, a table, anything "
            "delivered to a room - do NOT offer to pass it to the team, raise a request or connect them. "
            "Say plainly that you cannot arrange it and give the hotel's phone number exactly as your "
            "instructions state it, then stop."
        )

    @staticmethod
    def _mango_veg_asks_directive() -> str:
        """Mango's KB names no vegetarian dish, only "Signature dish: Butter Chicken".

        Asked for veg food (7 Oct), the answerer listed Butter Chicken in 4 of 4
        logged veg turns, even after "in veg food items don't give non veg items".
        Replayed on those turns with gpt-4.1-mini: the same rule edited into the
        prompt row still named it (1-4/4); sent here, 0/4 on every veg turn, while
        "give me non veg options" and "tell me about Purple Mango" kept it 4/4."""
        return (
            "=== VEG / VEGAN / JAIN ASKS ===\n"
            "When the guest asks for vegetarian, vegan or Jain food (\"veg options\", \"only veg\", \"Jain food "
            "for kids\"), answer ONLY with what fits that diet. Do not mention any non-vegetarian dish at all - "
            "not as a signature dish, not as a menu highlight, not with a \"(non-veg)\" label, not to say it is "
            "excluded. If your sources name no vegetarian dishes, say what they DO state (e.g. pure-veg, vegan "
            "and Jain food is available) and that the specific dish names aren't in your details. Never claim a "
            "cuisine or dish style the sources don't list (e.g. \"authentic Punjabi dishes for kids\") - say the "
            "guest can request it and the team will do their best to accommodate. A question that is not about "
            "a vegetarian / vegan / Jain diet is answered normally."
        )

    @staticmethod
    def _hotel_scope_only_directive() -> str:
        """SHORT scope rule injected late into the answerer.

        The long `_out_of_scope_guardrail_directive()` above says the same thing in
        ~4k chars and was measured to be no better: maths/general-knowledge decline
        either way, and the joke case fails under BOTH (the model's helpfulness
        instinct beats the instruction, however emphatic). So we pay ~10x fewer
        tokens on EVERY turn for the same behaviour. Keep this terse."""
        return (
            "=== SCOPE (read last, obey) ===\n"
            "Decide ONE thing first: is THIS message about this hotel, or the guest's own stay, "
            "booking or trip? That includes rooms, rates, availability, facilities, dining, location, "
            "policies, a complaint about their booking or stay, and asking to reach staff — and it "
            "still counts when they name a city or property you do not have.\n"
            "IF YES -> IN SCOPE. Answer it. When you hold no data for it (a city or property that is "
            "not yours), say plainly that you do not have one there and point to what you DO have. "
            "A hotel, booking or stay question is never refused as off-topic.\n"
            "IF NO -> OFF TOPIC: maths or calculations, general knowledge, trivia, jokes or riddles, "
            "coding, news, politics, another company's products. Do NOT answer it and do NOT attempt a "
            "partial answer — not even when the answer is obvious or the guest insists. Give ONE short "
            "friendly line that it is outside what you help with, then offer the hotel instead. Never "
            "open a form for it.\n"
            "IF THE MESSAGE HAS NO CLEAR TOPIC AT ALL -> do not bounce the question back. Name two or "
            "three things that are genuinely useful for THIS property and THIS conversation so they can "
            "pick, chosen from what you actually know about this hotel. Do not recite a fixed menu of "
            "capabilities, and do NOT do this when the message does have a topic — answer that instead.\n"
            "ANSWER PLAINLY: answer the guest's actual question in natural language. Never echo how the "
            "knowledge base stores a fact — no field labels, no 'is listed as', no 'shows as'. If something "
            "is unavailable, lead with a plain 'No' and say so in one line; never open with 'Yes' and then "
            "describe it as unavailable.\n"
            "CRISIS — overrides the above, but ONLY when THIS message expresses self-harm, suicide or a "
            "medical/mental-health crisis: reply briefly and kindly and urge them to contact local "
            "emergency services or a professional right now. No form, no handoff. If an EARLIER message "
            "did but this one is an ordinary hotel question, the crisis is over — answer normally and do "
            "NOT repeat crisis guidance.\n"
        )

    @staticmethod
    def _prebooking_framing_directive() -> str:
        """Recency-placed PHASE-FRAMING guard for pre_booking only. The guest has
        NOT booked or stayed yet, so replies must be framed around exploring /
        finding / booking the right hotel & room — never around 'your stay' (which
        implies they are already a guest). Injected only when phase == pre_booking."""
        return (
            "=== PRE-BOOKING FRAMING (read late, obey) ===\n"
            "The guest has NOT booked or stayed yet — they are still choosing a hotel/room. Frame every reply around helping them EXPLORE and BOOK: finding the right property, room, offer, and completing the booking. Do NOT talk as if they are already staying with us: avoid phrases like 'your stay', 'during your stay', 'enjoy your stay', 'here at the hotel with us'. Prefer wording like 'your booking', 'your trip', 'the hotel you're considering', or 'planning your stay'. This is tone/framing only — it never changes the facts you give.\n"
            "BOOKING REFINEMENT (no reservation exists yet): if the guest adjusts their dates, occupancy, room, or property BEFORE actually booking — e.g. 'change it to 22-25 August', 'make it 3 adults', 'actually the other property' — treat it as refining what they intend to book. Acknowledge the new detail and point them to complete the booking (the website / booking link) for those dates. Do NOT say you 'can't change the dates' or 'can't change your booking', and do NOT offer to connect them to reservations 'to update a reservation' — there is nothing booked to update. (A real modification only applies once a booking is actually confirmed.)\n"
        )

    @staticmethod
    def _bare_booking_object_directive() -> str:
        """Recency rule: a bare booking verb ('book one' / 'book it') means the IMMEDIATE
        prior topic — the thing your last reply was about — never a room looked at earlier.
        Injected last so it wins over the dominant room-booking gravity of the prompt + KB."""
        return (
            "=== A BARE 'BOOK' VERB = YOUR IMMEDIATE PRIOR TOPIC (read this) ===\n"
            "If the guest's latest message is a bare booking/execution verb with NO explicit object "
            "— 'book', 'book one', 'book it', 'book this', 'do it', 'yes book', 'reserve one' — the "
            "object is the IMMEDIATE PRIOR TOPIC: the specific thing YOUR OWN LAST reply was about. "
            "It is NEVER a different thing discussed several turns earlier. If your last reply was about "
            "the airport shuttle / transfer / pickup / parking or any amenity, then 'book one' means "
            "booking THAT amenity — answer for it (e.g. the pre-arrival 'arranged once your booking is "
            "confirmed' line), NOT a room. A room the guest looked at earlier does NOT capture a later "
            "bare 'book' once the conversation has moved on to a different subject. Resolve the object "
            "from your LAST message first, then answer accordingly.\n"
            "PRE-BOOKING OVERRIDE: in the pre_booking phase the guest is choosing a HOTEL to book, so the funnel goal is the ROOM. A bare 'book it' / 'book' / 'reserve it' there defaults to booking the ROOM at the active property. Treat it as booking a specific service/amenity ONLY if the prior turn was explicitly about arranging/ordering that bookable service. If the immediate prior topic was just an amenity the guest was EVALUATING ('does it have a jacuzzi / spa / pool?') — especially one the property does NOT even have — a bare 'book it' means book the ROOM, and you must NEVER reply that it's 'only available once you're checked in'. If you genuinely cannot tell whether they mean the room or something else, ASK 'Would you like to book a room at <property>?' rather than guess."
        )

    @staticmethod
    def _multi_property_booking_choice_directive() -> str:
        """Recency-placed rule: a booking ask with 2+ properties in scope and none
        chosen must ASK WHICH hotel first — never a property-less booking link or a
        generic 'book on our website' line (which strands the guest). Placed late so
        it beats the strong room-booking gravity of the prompt when many hotels are
        in scope. No-op when exactly one property is in scope."""
        return (
            "=== A BOOKING NEEDS ONE HOTEL FIRST — ASK WHICH WHEN SEVERAL ARE IN SCOPE (read late, obey) ===\n"
            "If the guest's LATEST message wants to BOOK a room/stay ('book a room', 'i want to book', 'i need a room', 'reserve a room', 'make a booking', \"let's book\", 'book a stay') AND 2+ properties are in scope (active_property_ids / book_properties has 2+) AND the guest has NOT named which specific hotel to book:\n"
            "- Do NOT give a booking website link, do NOT emit a `<<SHOW_BOOKING_BUTTON>>` marker, and do NOT write a generic property-less line like 'You can complete your booking on our website'. Any of those strands the guest with no way to choose.\n"
            "- INSTEAD, ASK which hotel they want to book: list the in-scope hotels by NAME, one short line each (name + city or one distinguishing fact), and end with a friendly 'Which one would you like to book?'.\n"
            "- Only AFTER the guest picks/names ONE hotel do you give THAT hotel's booking link.\n"
            "This fires ONLY when 2+ properties are in scope and none is chosen. When exactly ONE property is in scope, book it normally (give the link). A plain info question is unaffected.\n"
        )

    @staticmethod
    def _answer_scope_directive() -> str:
        """Recency-placed rule fixing two answer-scoping misses observed live:
        (a) when several properties' books are loaded but the guest's question
        points at ONE of them (anaphora / ordinal / descriptor), the answerer used
        to describe the FIRST loaded property instead of the referenced one; and
        (b) a 'what cities do you have' coverage question was answered about a
        single property instead of listing every location. Both are answer-side —
        the router scoped correctly. No-op for a plain single-property question."""
        return (
            "=== ANSWER THE SCOPE THE GUEST ACTUALLY MEANS (understand intent — do NOT pattern-match words) ===\n"
            "1) THE PROPERTY IN FOCUS. As a conversation narrows, the guest builds up a single property they're focused on. When their LATEST message is ABOUT one specific property — even without naming it — answer about THAT property, not whichever property's books happen to be loaded first. Work out which one they mean from the conversation itself: a pronoun ('does IT have a spa', 'is it pet-friendly') means the property just singled out; 'the second one' means the property at that position in the list you last gave; 'the cheaper / beachside / boutique one' means the property those facts fit. Name the property in your reply so it's unambiguous. Never default to the first/alphabetical property when the guest clearly meant a specific other one. (These are ILLUSTRATIONS of resolving a reference from context — the real rule is: figure out which property the guest is focused on and answer about that one. Only ask a clarifying question if two truly fit equally; a clear reference is always answered directly.)\n"
            "2) FOOTPRINT vs RECALL — two questions that both mention 'cities' but mean OPPOSITE scopes. Decide by what the guest actually wants, not by the word 'cities':\n"
            "   - FOOTPRINT — the guest wants to know the BRAND's overall reach: which cities/destinations the COMPANY operates in, where all the hotels are, the full set of places they could book (e.g. 'what cities do you have hotels in', 'where are all your hotels'). Answer from the `all_locations` data — list EVERY city in it, one line per city with its hotel(s). This is the whole map; do not limit it to the active/loaded property.\n"
            "   - RECALL — the guest wants what THIS conversation covered: the cities/hotels WE actually talked about (e.g. 'which cities did we discuss', 'the ones we looked at', 'remind me what we covered'). Answer ONLY from `discussed_properties_in_order` — list just those. NEVER dump the whole `all_locations` map for a recall.\n"
            "   Same surface word, opposite scope — the tell is whether they're asking about what the brand HAS (footprint → all) or about what WE DISCUSSED (recall → only discussed).\n"
            "3) ALL-NETWORK SUPERLATIVE — 'your cheapest hotel overall', 'the most premium of all your hotels', 'your best hotel', 'which hotel is nicest' (across the WHOLE brand, not the active one). You only have the ACTIVE property's books loaded, so you can NOT truly rank every hotel — especially on PRICE (rates aren't in the KB). Do NOT answer as if it were about the active hotel, and do NOT pivot to listing that one hotel's room types. Instead: if it's about a KNOWN attribute you can reason about (e.g. 'most premium' → a property with suites / a spa / a rooftop pool), recommend one or two by name with the reason; if it's about PRICE / 'cheapest' (which you cannot compare across all hotels), say plainly you can't rank all hotels on price from here and ask for a city or budget to narrow — or offer to have the team share pricing. Never silently answer a whole-brand superlative about just the one loaded hotel.\n"
        )

    @staticmethod
    def _build_booking_context_prompt_block(context: Any) -> str:
        """Build a reusable prompt block with booking context + phase behavioral rules.
        Appended to orchestrator, service router, service agent, and suggestion prompts."""
        lines: list[str] = []
        from services.booking_assistance_state import (
            team_booking_enabled, booking_ledger, details_submitted,
        )
        team_booking = team_booking_enabled(getattr(context, "hotel_code", ""))
        if team_booking:
            submitted = details_submitted(context)
            lines.append("\n=== BOOKING ASSISTANCE STATE (current server state; overrides old chat wording) ===")
            lines.append(f"details_submitted: {str(submitted).lower()}")
            # NAME THE FORM THAT IS SPENT. "The booking form is no longer available"
            # was read by the support agent as covering its OWN callback form: a guest
            # who had sent their room details asked "Can someone call me now" and got
            # only phone numbers, no form (replayed 6/6; this wording opens it 6/6).
            lines.append(
                "available_action: none. The room-booking form ('Share your booking details') "
                "has already been used - never ask the guest to tap it or submit their "
                "room-booking details again. This applies ONLY to the room-booking form: a "
                "callback / support request or an event enquiry still opens its own form as normal."
                if submitted else
                "available_action: share_booking_details. A booking-assistance form can be shown. "
                "For booking intent, direct the guest to that form; do not collect its fields in chat. "
                "For an information question, answer it directly without a booking pitch."
            )

        # --- Booking requests this guest has ALREADY submitted ---
        # The fact lives in `_rb_bookings`, which `_public_pending_data` strips
        # before any prompt is built - so without this the answerer was handed a
        # rule ("if the details are already in, say where it stands") and never
        # the field it tests. It answered the only way it could: by pitching the
        # same form again. Give it the facts and let it write the sentence.
        #
        # Gated to hotels on the team-only booking list, so no other tenant's
        # prompt changes by a single byte.
        try:
            _rb_list = [row for row in booking_ledger(context) if row.get("details_submitted") is True]
        except Exception:
            _rb_list = []
        if _rb_list:
            try:
                _rb_on = team_booking
            except Exception:
                _rb_on = False
            if _rb_on:
                lines.append("\n=== BOOKING REQUESTS THIS GUEST HAS ALREADY SENT ===")
                for _bk in _rb_list:
                    if not isinstance(_bk, dict):
                        continue
                    _p = str(_bk.get("property") or "").strip()
                    _ci = str(_bk.get("check_in") or "").strip()
                    _co = str(_bk.get("check_out") or "").strip()
                    _g = str(_bk.get("guests") or "").strip()
                    _bits = [b for b in (
                        f"property: {_p}" if _p else "",
                        f"dates: {_ci} to {_co}" if (_ci and _co) else (f"date: {_ci or _co}" if (_ci or _co) else ""),
                        f"guests: {_g}" if _g else "",
                    ) if b]
                    if _bits:
                        lines.append("- " + " | ".join(_bits))
                lines.append(
                    "These requests are ALREADY with the team. The 'Share your booking details' button "
                    "has been used and is no longer on screen, so never tell the guest to tap it and "
                    "never ask them to share their booking details again."
                )
                lines.append(
                    "If they ask to book again WITHOUT giving new dates: tell them their request is "
                    "already with the team, say the dates back to them, and offer the two things they "
                    "can still do - change the dates, or add another room."
                )
                lines.append(
                    "If they give NEW dates, treat it as a change to the SAME request: confirm the new "
                    "dates back to them and never imply a second request was raised."
                )
                lines.append(
                    "If they ask about anything else, just answer that normally and do not mention the "
                    "booking at all."
                )
        # --- Booking context ---
        # Render when we have a booking id OR the guest's stay dates, so the block
        # authoritatively pins the guest's OWN check-in/out even on a plain turn
        # with no booking_id set — this stops the answerer inventing stay dates.
        _bk_has_dates = bool(
            getattr(context, "booking_check_in_date", None)
            and getattr(context, "booking_check_out_date", None)
        )
        if getattr(context, "booking_id", None) or _bk_has_dates:
            lines.append("\n=== ACTIVE BOOKING CONTEXT ===")
            if getattr(context, "booking_guest_name", None):
                lines.append(f"Guest Name: {context.booking_guest_name}")
            if getattr(context, "room_number", None):
                lines.append(f"Room Number: {context.room_number}")
            if getattr(context, "booking_room_type", None):
                lines.append(f"Room Type: {context.booking_room_type}")
            if getattr(context, "booking_property_name", None):
                lines.append(f"Property: {context.booking_property_name}")
            if getattr(context, "booking_confirmation_code", None):
                lines.append(f"Confirmation Code: {context.booking_confirmation_code}")
            _bk_ci = getattr(context, "booking_check_in_date", None) or ""
            _bk_co = getattr(context, "booking_check_out_date", None) or ""
            if _bk_ci and _bk_co:
                lines.append(f"Stay: {_bk_ci} to {_bk_co}")
                lines.append(
                    "IMPORTANT: Any date the guest selects for services (spa, dining, transport, etc.) "
                    f"MUST fall within their stay dates ({_bk_ci} to {_bk_co}). "
                    "If they request a date outside this window, politely inform them and ask for a valid date."
                )
            lines.append(
                "NAME USAGE RULE (strict): Do NOT begin every reply with 'Hi {name}!' or any name-led "
                "greeting — that reads robotic, not human. Use the guest's first name ONLY when it "
                "genuinely adds value: the very first greeting of the session, a personal confirmation "
                "('Done, {name} — booked for 3 PM.'), apologising, or acknowledging something sensitive. "
                "For ordinary informational answers, follow-up replies, and mid-conversation turns, do "
                "NOT use their name at all. Aim for AT MOST one name mention every several turns. "
                "Humans don't repeat each other's names in every message — mirror that. "
                "Reference room number and property only when it actually helps the answer. "
                "NEVER include raw booking metadata (guest ID, confirmation codes, phone/email) in "
                "response text unless the guest explicitly asks for it."
            )

            lines.append(
                "TIMING QUESTION RULE: When the guest asks WHEN they can check in / check out, "
                "what time their stay starts/ends, or any question combining a time-of-day policy "
                "with their specific stay, ALWAYS combine the guest's specific check-in/check-out DATE "
                "(from this booking context) with the property's check-in/check-out TIME (from KB policy). "
                "Never give only the time or only the date when both are available in context — "
                "ground the policy time on the guest's actual stay date."
            )

            lines.append(
                "SPECIFICITY RULE: When ACTIVE BOOKING CONTEXT exists, interpret the guest's message through "
                "that context first. In pre_checkin, during_stay, and post_checkout, default to the guest's "
                "own reservation or stay when the message is ambiguous. Prefer the most specific grounded answer "
                "available, using this priority order: active booking context, booked-property context, general "
                "property policy, then broad fallback knowledge. If active booking context provides a specific "
                "date, room type, property, or stay detail, do not replace it with a generic policy answer. "
                "If both personal stay data and general property policy are relevant, combine them naturally in "
                "one answer. Only answer generically when the guest is clearly asking a general question, asking "
                "hypothetically, asking about another property, or when no stay-specific detail is available. "
                "Before finalizing the reply, silently check whether active booking context makes the answer more "
                "personal, more specific, or more accurate. If it does, use it."
            )

        # --- Phase behavioral rules ---
        phase = str(getattr(context, "booking_phase", None) or "").strip()
        if phase:
            lines.append(f"\n=== PHASE RULES ({phase}) ===")
            if phase == "pre_checkin":
                lines.append(
                    "The guest has a confirmed booking but has NOT checked in yet.\n"
                    "Only services assigned to the pre_checkin phase in the admin config are available.\n"
                    "Do NOT offer or dispatch any service that belongs to a different phase.\n"
                    "If the guest asks for something only available during their stay, phrase it naturally: "
                    "'We can arrange that once you arrive' or 'This will be available during your stay'."
                )
            elif phase == "during_stay":
                lines.append(
                    "The guest is currently checked in and staying at the property.\n"
                    "Only services assigned to the during_stay phase in the admin config are available.\n"
                    "Do NOT offer or dispatch any service that belongs to a different phase.\n"
                    "Any service scheduling must fall within their stay dates.\n"
                    "Be proactive: offer relevant available services based on their room and property."
                )
            elif phase == "post_checkout":
                lines.append(
                    "The guest has already checked out.\n"
                    "Only services assigned to the post_checkout phase in the admin config are available.\n"
                    "Do NOT offer or dispatch any service that belongs to a different phase.\n"
                    "If the guest asks for in-stay services, phrase naturally: "
                    "'Since you have already checked out, I can help with [available services] "
                    "or planning your next visit'."
                )
            elif phase == "pre_booking":
                lines.append(
                    "The guest is exploring options and has no confirmed booking yet.\n"
                    "Only services assigned to the pre_booking phase in the admin config are available.\n"
                    "Do NOT offer or dispatch any service that belongs to a different phase.\n"
                    "Goal: help the guest find the right option and complete a booking."
                )

        return "\n".join(lines)

    @classmethod
    def _service_is_room_stay_booking(cls, service: Any, service_id: str = "") -> bool:
        """True only for the room/stay booking lead service — the one whose
        in-chat contact form we bypass in favour of the website booking link.

        Deliberately conservative: hall / venue / event / spa / dining / table /
        transfer / support services that merely share the verb 'book' are
        excluded so this never hijacks a genuinely different intake.
        """
        sid = cls._normalize_identifier(service_id)
        if not sid and isinstance(service, dict):
            sid = cls._normalize_identifier(service.get("id"))
        name_norm = ""
        if isinstance(service, dict):
            name_norm = cls._normalize_identifier(service.get("name"))
        blob = f"{sid} {name_norm}"
        _exclude = (
            "support", "complaint", "issue", "help", "contact_staff", "callback",
            "hall", "venue", "event", "banquet", "wedding", "conference", "party",
            "spa", "salon", "massage", "wellness", "dining", "restaurant", "table",
            "food", "drink", "housekeep", "laundry", "transfer", "shuttle", "cab",
            "taxi", "airport",
        )
        if any(tok in blob for tok in _exclude):
            return False
        if sid in {"room_booking", "room_booking_request", "stay_booking", "room_stay_booking"}:
            return True
        has_room = ("room" in blob) or ("stay" in blob)
        has_book = ("book" in blob) or ("reserv" in blob)
        return has_room and has_book

    @classmethod
    def _canonical_property_scope_fields(
        cls,
        *,
        active_property_ids: Any = None,
        active_property_names: Any = None,
        selected_property_scope_id: Any = "",
        selected_property_scope_ids: Any = None,
        selected_property_scope_name: Any = "",
        property_name_map: Any = None,
    ) -> dict[str, Any]:
        active_ids_raw = (
            list(active_property_ids)
            if isinstance(active_property_ids, list)
            else ([str(active_property_ids).strip()] if str(active_property_ids or "").strip() else [])
        )
        selected_ids_raw = (
            list(selected_property_scope_ids)
            if isinstance(selected_property_scope_ids, list)
            else ([str(selected_property_scope_ids).strip()] if str(selected_property_scope_ids or "").strip() else [])
        )
        normalized_active_ids = cls._normalize_property_id_list(active_ids_raw)
        normalized_selected_ids = cls._normalize_property_id_list(selected_ids_raw)
        normalized_selected_id = str(selected_property_scope_id or "").strip()
        normalized_ids = normalized_active_ids or normalized_selected_ids
        if not normalized_ids and normalized_selected_id:
            normalized_ids = [normalized_selected_id]
        if not normalized_ids:
            return {}

        normalized_names = cls._normalize_property_name_list(active_property_names)
        resolved_name_map = cls._normalize_property_name_map(property_name_map)
        primary_name = str(selected_property_scope_name or "").strip()
        if normalized_ids and resolved_name_map.get(normalized_ids[0]):
            primary_name = resolved_name_map.get(normalized_ids[0], "")
        elif normalized_active_ids and normalized_names:
            primary_name = normalized_names[0]
        elif not primary_name and normalized_names:
            primary_name = normalized_names[0]

        canonical_names: list[str] = []
        for index, property_id in enumerate(normalized_ids):
            mapped_name = resolved_name_map.get(property_id, "")
            if mapped_name:
                canonical_names.append(mapped_name)
                continue
            if index < len(normalized_names):
                canonical_names.append(normalized_names[index])
                continue
            if len(normalized_ids) == 1 and primary_name:
                canonical_names.append(primary_name)
                continue
            canonical_names.append(property_id)

        if not primary_name and canonical_names:
            primary_name = canonical_names[0]

        return {
            "active_property_ids": list(normalized_ids),
            "active_property_names": list(canonical_names),
            "selected_property_scope_id": normalized_ids[0],
            "selected_property_scope_ids": list(normalized_ids),
            "selected_property_scope_name": primary_name or canonical_names[0],
        }

    @classmethod
    def _sync_property_scope_pending(
        cls,
        *,
        pending_data: dict[str, Any],
        property_ids: Any,
        active_property_names: Any = None,
        selected_property_name: Any = "",
        property_name_map: Any = None,
    ) -> dict[str, Any]:
        if not isinstance(pending_data, dict):
            return {}
        scope_fields = cls._canonical_property_scope_fields(
            active_property_ids=property_ids,
            active_property_names=active_property_names,
            selected_property_scope_name=selected_property_name,
            property_name_map=property_name_map or pending_data.get("_property_name_map"),
        )
        if not scope_fields:
            return {}

        mirror_fields = {
            "active_property_ids": "_active_property_ids",
            "active_property_names": "_active_property_names",
            "selected_property_scope_id": "_selected_property_scope_id",
            "selected_property_scope_ids": "_selected_property_scope_ids",
            "selected_property_scope_name": "_selected_property_scope_name",
        }
        for public_key, private_key in mirror_fields.items():
            value = scope_fields.get(public_key)
            normalized_value = list(value) if isinstance(value, list) else value
            pending_data[private_key] = normalized_value
            pending_data[public_key] = list(value) if isinstance(value, list) else value
        if property_name_map is not None or pending_data.get("_property_name_map") is not None:
            pending_data["_property_name_map"] = cls._normalize_property_name_map(
                property_name_map or pending_data.get("_property_name_map")
            )
        return scope_fields

    @classmethod
    def _normalize_scope_match_phrase(cls, value: Any) -> str:
        return re.sub(r"[^a-z0-9]+", " ", cls._normalize_ws_lower(value)).strip()

    @staticmethod
    def _build_available_property_scope(
        property_catalog: Any,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        available_properties: list[dict[str, Any]] = []
        available_locations_map: dict[str, dict[str, Any]] = {}
        for row in property_catalog or []:
            if not isinstance(row, dict):
                continue
            property_id = LLMOrchestrationService._normalize_identifier(row.get("id"))
            if not property_id:
                continue
            property_name = str(row.get("name") or property_id).strip() or property_id
            aliases = [
                str(item).strip()
                for item in list(row.get("aliases") or [])[:6]
                if str(item).strip()
            ]
            location_payload = row.get("location")
            location_label = ""
            if isinstance(location_payload, dict):
                location_label = (
                    str(location_payload.get("label") or "").strip()
                    or str(location_payload.get("city") or "").strip()
                    or str(location_payload.get("location") or "").strip()
                    or str(location_payload.get("name") or "").strip()
                )
            elif isinstance(location_payload, str):
                location_label = str(location_payload).strip()
            available_properties.append(
                {
                    "id": property_id,
                    "name": property_name,
                    "aliases": aliases,
                    "location": location_label,
                }
            )
            if not location_label:
                continue
            location_id = LLMOrchestrationService._normalize_identifier(location_label)
            if not location_id:
                continue
            entry = available_locations_map.setdefault(
                location_id,
                {
                    "id": location_id,
                    "label": location_label,
                    "property_ids": [],
                    "property_names": [],
                },
            )
            if property_id not in entry["property_ids"]:
                entry["property_ids"].append(property_id)
                entry["property_names"].append(property_name)
        return available_properties, list(available_locations_map.values())

    @classmethod
    def _service_route_names_unsupported_property_scope(
        cls,
        *,
        user_message: Any,
        router_reason: Any = "",
        available_properties: list[dict[str, Any]] | None = None,
        available_locations: list[dict[str, Any]] | None = None,
        selected_property_ids: list[str] | None = None,
    ) -> bool:
        """True when a support route appears to name a non-catalog property/location.

        This is a generic catalog invariant, not a city-specific rule: support can
        ask "which property?" when none was named, but it should not open a form
        for a property/location the catalog does not contain.
        """
        if selected_property_ids:
            return False
        # Use the guest's wording only. Router reasons can contain generic
        # phrases like "supported hotels" and should never create a false
        # unsupported-property signal.
        text = cls._compact_message_text(user_message).lower()
        if not text:
            return False

        generic_terms = {
            "zone",
            "park",
            "the park",
            "by the park",
            "connect",
            "zone connect",
            "property",
            "hotel",
            "location",
            "branch",
            "resort",
        }
        supported_terms: set[str] = set()
        for row in available_properties or []:
            if not isinstance(row, dict):
                continue
            for raw in [row.get("name"), row.get("location"), *(row.get("aliases") or [])]:
                term = cls._compact_message_text(raw).lower()
                if term and len(term) >= 4 and term not in generic_terms:
                    supported_terms.add(term)
        for row in available_locations or []:
            if not isinstance(row, dict):
                continue
            for raw in [row.get("label"), row.get("city"), row.get("name")]:
                term = cls._compact_message_text(raw).lower()
                if term and len(term) >= 4 and term not in generic_terms:
                    supported_terms.add(term)

        if any(term in text for term in supported_terms):
            return False

        scope_cue = r"(?:property|hotel|location|branch|resort)"
        candidate_patterns = (
            rf"\b(?:for|at|in|about|regarding|with)\s+(?:your|our|the|this|that)?\s*([a-z][a-z0-9 '&-]{{2,60}}?)\s+{scope_cue}\b",
            rf"\b([a-z][a-z0-9 '&-]{{2,45}}?)\s+{scope_cue}\b",
        )
        ignore_words = {
            "your",
            "our",
            "the",
            "this",
            "that",
            "my",
            "a",
            "an",
            "any",
            "current",
            "existing",
            "hotel",
            "property",
            "location",
            "branch",
            "resort",
            "complaint",
            "issue",
            "support",
            "request",
            "raise",
            "file",
            "log",
            "for",
            "at",
            "in",
            "about",
            "regarding",
            "with",
            "i",
            "we",
            "you",
            "want",
            "need",
            "have",
            "has",
            "had",
            "to",
            "do",
            "does",
            "can",
            "could",
            "would",
            "should",
            "please",
            "zone",
            "park",
            "connect",
        }
        for pattern in candidate_patterns:
            for match in re.finditer(pattern, text):
                candidate = cls._compact_message_text(match.group(1)).lower()
                words = [word for word in re.findall(r"[a-z0-9]+", candidate) if word not in ignore_words]
                if any(len(word) >= 4 for word in words):
                    return True
        return False

    @staticmethod
    def _is_common_property_scope_id(property_id: str) -> bool:
        normalized = str(property_id or "").strip().lower()
        return normalized in {"", "shared", "common"}

    def _book_row_relevance_score(self, *, user_message: str, row: dict[str, Any]) -> float:
        if not isinstance(row, dict):
            return 0.0
        query_text = self._normalize_ws_lower(user_message)
        if not query_text:
            return 0.0
        query_tokens = self._tokenize_router_text(query_text)
        if not query_tokens:
            return 0.0

        title = self._normalize_ws_lower(row.get("title"))
        description = self._normalize_ws_lower(row.get("description"))
        aliases_text = self._normalize_ws_lower(
            " ".join(str(item).strip() for item in list(row.get("aliases") or []))
        )
        keywords_text = self._normalize_ws_lower(
            " ".join(str(item).strip() for item in list(row.get("keywords") or []))
        )
        title_tokens = self._tokenize_router_text(title)
        description_tokens = self._tokenize_router_text(description)
        alias_tokens = self._tokenize_router_text(aliases_text)
        keyword_tokens = self._tokenize_router_text(keywords_text)

        score = 0.0
        score += 2.2 * len(query_tokens & title_tokens)
        score += 1.8 * len(query_tokens & description_tokens)
        score += 1.2 * len(query_tokens & alias_tokens)
        score += 1.0 * len(query_tokens & keyword_tokens)
        if query_text and (query_text in title or query_text in description):
            score += 3.0
        return score

    def _expand_book_ids_for_unresolved_multi_property(
        self,
        *,
        user_message: str,
        selected_book_ids: list[str],
        book_catalog: list[dict[str, Any]],
        active_property_ids: list[str],
        max_selected_book_ids: int,
    ) -> list[str]:
        selected: list[str] = []
        for value in selected_book_ids:
            book_id = str(value or "").strip()
            if not book_id or book_id in selected:
                continue
            selected.append(book_id)
        if not selected:
            return selected
        if active_property_ids:
            return selected[:max_selected_book_ids]
        if not isinstance(book_catalog, list) or not book_catalog:
            return selected[:max_selected_book_ids]

        rows_by_book_id: dict[str, list[dict[str, Any]]] = {}
        rows_by_property: dict[str, list[dict[str, Any]]] = {}
        property_names: dict[str, str] = {}
        for row in book_catalog:
            if not isinstance(row, dict):
                continue
            book_id = str(row.get("id") or "").strip()
            if not book_id:
                continue
            property_id = self._normalize_identifier(row.get("property_id"))
            if self._is_common_property_scope_id(property_id):
                continue
            rows_by_book_id.setdefault(book_id, []).append(row)
            rows_by_property.setdefault(property_id, []).append(row)
            property_name = str(row.get("property_name") or "").strip()
            if property_id and property_name and property_id not in property_names:
                property_names[property_id] = property_name

        if len(rows_by_property) <= 1:
            return selected[:max_selected_book_ids]

        # If the message clearly targets one property, keep the router's narrow scope.
        normalized_message = self._normalize_ws_lower(user_message)
        explicit_property_hits: set[str] = set()
        for property_id, rows in rows_by_property.items():
            phrases = {
                self._normalize_ws_lower(property_id.replace("_", " ")),
                self._normalize_ws_lower(property_names.get(property_id, "")),
            }
            for row in rows[:4]:
                phrases.add(self._normalize_ws_lower(row.get("property_name")))
            for phrase in phrases:
                if phrase and len(phrase) >= 3 and phrase in normalized_message:
                    explicit_property_hits.add(property_id)
                    break
        if len(explicit_property_hits) == 1:
            return selected[:max_selected_book_ids]

        selected_property_ids: set[str] = set()
        for book_id in selected:
            for row in rows_by_book_id.get(book_id, []):
                property_id = self._normalize_identifier(row.get("property_id"))
                if not self._is_common_property_scope_id(property_id):
                    selected_property_ids.add(property_id)

        missing_property_ids = [
            property_id
            for property_id in rows_by_property.keys()
            if property_id not in selected_property_ids
        ]
        if not missing_property_ids:
            return selected[:max_selected_book_ids]

        overview_markers = {
            "hotel information",
            "hotel overview",
            "overview",
            "about",
            "property details",
            "property overview",
        }
        for property_id in missing_property_ids:
            if len(selected) >= max_selected_book_ids:
                break
            candidates = rows_by_property.get(property_id, [])
            if not candidates:
                continue
            scored_candidates = sorted(
                candidates,
                key=lambda row: (
                    self._book_row_relevance_score(user_message=user_message, row=row),
                    int(row.get("page_count") or 0),
                ),
                reverse=True,
            )
            chosen_book_id = ""
            for row in scored_candidates:
                candidate_id = str(row.get("id") or "").strip()
                if not candidate_id or candidate_id in selected:
                    continue
                relevance = self._book_row_relevance_score(
                    user_message=user_message,
                    row=row,
                )
                if relevance > 0:
                    chosen_book_id = candidate_id
                    break
            if not chosen_book_id:
                for row in scored_candidates:
                    candidate_id = str(row.get("id") or "").strip()
                    if not candidate_id or candidate_id in selected:
                        continue
                    row_blob = self._normalize_ws_lower(
                        f"{row.get('title') or ''} {row.get('description') or ''}"
                    )
                    if any(marker in row_blob for marker in overview_markers):
                        chosen_book_id = candidate_id
                        break
            if not chosen_book_id:
                for row in scored_candidates:
                    candidate_id = str(row.get("id") or "").strip()
                    if candidate_id and candidate_id not in selected:
                        chosen_book_id = candidate_id
                        break
            if chosen_book_id:
                selected.append(chosen_book_id)

        # For unresolved multi-property broad asks (e.g. "tell me about your hotel"),
        # enrich thin properties so one deeply-detailed property does not dominate.
        min_pages_per_property = max(
            2,
            int(getattr(settings, "chat_main_router_multi_property_min_pages", 6) or 6),
        )
        max_books_per_property = max(
            1,
            int(getattr(settings, "chat_main_router_multi_property_max_books_per_property", 3) or 3),
        )
        if not explicit_property_hits:
            selected_rows_by_property: dict[str, list[dict[str, Any]]] = {}
            for book_id in selected:
                for row in rows_by_book_id.get(book_id, []):
                    property_id = self._normalize_identifier(row.get("property_id"))
                    if self._is_common_property_scope_id(property_id):
                        continue
                    selected_rows_by_property.setdefault(property_id, []).append(row)

            for property_id, candidates in rows_by_property.items():
                if len(selected) >= max_selected_book_ids:
                    break
                prop_rows = list(selected_rows_by_property.get(property_id, []))
                selected_page_sum = sum(int(row.get("page_count") or 0) for row in prop_rows)
                selected_book_count = len(
                    {
                        str(row.get("id") or "").strip()
                        for row in prop_rows
                        if str(row.get("id") or "").strip()
                    }
                )
                if selected_page_sum >= min_pages_per_property and selected_book_count >= 1:
                    continue
                if selected_book_count >= max_books_per_property:
                    continue

                topical_markers = (
                    "overview",
                    "information",
                    "room",
                    "suite",
                    "amenit",
                    "dining",
                    "restaurant",
                    "wellness",
                    "spa",
                    "event",
                    "location",
                )
                ranked_candidates = sorted(
                    candidates,
                    key=lambda row: (
                        int(any(marker in self._normalize_ws_lower(
                            f"{row.get('title') or ''} {row.get('description') or ''}"
                        ) for marker in topical_markers)),
                        self._book_row_relevance_score(user_message=user_message, row=row),
                        int(row.get("page_count") or 0),
                    ),
                    reverse=True,
                )
                for row in ranked_candidates:
                    candidate_id = str(row.get("id") or "").strip()
                    if not candidate_id or candidate_id in selected:
                        continue
                    selected.append(candidate_id)
                    selected_page_sum += int(row.get("page_count") or 0)
                    selected_book_count += 1
                    if len(selected) >= max_selected_book_ids:
                        break
                    if (
                        selected_page_sum >= min_pages_per_property
                        or selected_book_count >= max_books_per_property
                    ):
                        break

        return selected[:max_selected_book_ids]

    def _generic_kb_ticketing_enabled(self) -> bool:
        return bool(getattr(settings, "chat_llm_generic_kb_ticketing_enabled", True))

    def _is_generic_kb_request(self, decision: OrchestrationDecision | None) -> bool:
        if not isinstance(decision, OrchestrationDecision):
            return False
        if not self._generic_kb_ticketing_enabled():
            return False
        if bool(getattr(decision.ticket, "generic_request", False)):
            return True
        metadata = decision.metadata if isinstance(decision.metadata, dict) else {}
        return self._coerce_bool(
            metadata.get("generic_kb_request") or metadata.get("kb_grounded_request"),
            default=False,
        )

    def _normalize_generic_kb_request_decision(
        self,
        decision: OrchestrationDecision,
    ) -> OrchestrationDecision:
        if not self._is_generic_kb_request(decision):
            return decision

        if not isinstance(decision.metadata, dict):
            decision.metadata = {}
        decision.ticket.generic_request = True
        decision.metadata["generic_kb_request"] = True

        evidence = self._normalize_ticket_evidence(
            decision.ticket.evidence
            or decision.metadata.get("generic_kb_evidence")
            or decision.metadata.get("kb_evidence")
        )
        if evidence:
            decision.ticket.evidence = list(evidence)
            decision.metadata["generic_kb_evidence"] = list(evidence)

        if decision.ticket.phase_applicable is None:
            if "generic_kb_phase_applicable" in decision.metadata:
                decision.ticket.phase_applicable = self._coerce_bool(
                    decision.metadata.get("generic_kb_phase_applicable"),
                    default=False,
                )
            elif "kb_phase_applicable" in decision.metadata:
                decision.ticket.phase_applicable = self._coerce_bool(
                    decision.metadata.get("kb_phase_applicable"),
                    default=False,
                )
        if decision.ticket.phase_applicable is not None:
            decision.metadata["generic_kb_phase_applicable"] = bool(decision.ticket.phase_applicable)
        if bool(decision.ticket.approval_required):
            decision.metadata["generic_kb_approval_required"] = True

        if not str(decision.ticket.category or "").strip():
            decision.ticket.category = "request"
        if not str(decision.ticket.sub_category or "").strip():
            decision.ticket.sub_category = "generic_kb_request"

        if not isinstance(decision.pending_data_updates, dict):
            decision.pending_data_updates = {}
        decision.pending_data_updates.setdefault("generic_kb_request", True)
        if decision.ticket.phase_applicable is not None:
            decision.pending_data_updates.setdefault(
                "generic_kb_phase_applicable",
                bool(decision.ticket.phase_applicable),
            )
        if bool(decision.ticket.approval_required):
            decision.pending_data_updates.setdefault("generic_kb_approval_required", True)
        if decision.ticket.evidence:
            decision.pending_data_updates.setdefault("generic_kb_evidence", list(decision.ticket.evidence))
        if str(decision.ticket.sub_category or "").strip():
            decision.pending_data_updates.setdefault("ticket_sub_category", decision.ticket.sub_category)
        if str(decision.ticket.issue or "").strip():
            decision.pending_data_updates.setdefault("generic_kb_issue", str(decision.ticket.issue).strip())
        if str(decision.ticket.reason or "").strip():
            decision.pending_data_updates.setdefault("generic_kb_reason", str(decision.ticket.reason).strip())

        action = str(decision.action or "").strip().lower()
        if action == "collect_info" and not str(decision.pending_action or "").strip():
            if bool(decision.ticket.required) and not bool(decision.missing_fields) and not bool(decision.ticket.ready_to_create):
                decision.pending_action = "confirm_generic_kb_request"
            else:
                decision.pending_action = "collect_generic_kb_request"
        if action == "create_ticket" and bool(decision.ticket.ready_to_create):
            decision.pending_action = None

        return decision

    @staticmethod
    def _split_kb_text_for_llm(*, kb_text: str, chunk_chars: int) -> list[str]:
        text = str(kb_text or "").strip()
        if not text:
            return []
        target = max(4000, int(chunk_chars or 18000))
        if len(text) <= target:
            return [text]

        chunks: list[str] = []
        current_lines: list[str] = []
        current_chars = 0
        for raw_line in text.splitlines():
            line = raw_line.rstrip()
            line_len = len(line) + 1
            if current_lines and current_chars + line_len > target:
                chunks.append("\n".join(current_lines).strip())
                current_lines = []
                current_chars = 0
            current_lines.append(line)
            current_chars += line_len
        if current_lines:
            chunks.append("\n".join(current_lines).strip())
        return [chunk for chunk in chunks if chunk]

    @staticmethod
    def _build_kb_source_manifest(kb_text: str, *, max_sources: int = 120) -> list[str]:
        names: list[str] = []
        seen: set[str] = set()
        for match in re.finditer(r"^=== SOURCE: (.+?) ===$", str(kb_text or ""), flags=re.MULTILINE):
            name = str(match.group(1) or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            names.append(name)
            if len(names) >= max_sources:
                break
        return names

    @staticmethod
    def _build_property_scope_history_text(
        *,
        context: ConversationContext,
        max_messages: int = 12,
        max_chars: int = 5000,
    ) -> str:
        messages = getattr(context, "messages", []) or []
        parts: list[str] = []
        for item in messages[-max_messages:] if isinstance(messages, list) else []:
            if isinstance(item, dict):
                role = str(item.get("role") or "").strip().lower()
                content = str(item.get("content") or "").strip()
            else:
                role = str(getattr(item, "role", "") or "").strip().lower()
                content = str(getattr(item, "content", "") or "").strip()
            if not content:
                continue
            parts.append(f"{role or 'message'}: {content}")
        history_text = "\n".join(parts).strip()
        if max_chars > 0 and len(history_text) > max_chars:
            history_text = history_text[-max_chars:]
        return history_text

    async def _prepare_full_kb_context_for_message(
        self,
        *,
        user_message: str,
        context: ConversationContext,
        full_kb_text: str,
    ) -> dict[str, Any]:
        raw_text = str(full_kb_text or "").strip()
        if not raw_text:
            return {
                "full_kb_text": "",
                "property_scope": {
                    "mode": "unscoped",
                    "active_property_id": "",
                    "active_property_name": "",
                    "matched_property_ids": [],
                    "history_property_ids": [],
                    "selected_property_ids": [],
                    "property_manifest": [],
                    "expected_property_count": 0,
                    "detected_property_count": 0,
                    "count_mismatch": False,
                    "requires_clarification": False,
                    "clarification_question": "",
                },
            }

        inline_budget = max(
            12000,
            int(getattr(settings, "full_kb_llm_max_kb_chars", 180000) or 180000),
        )
        property_scope: dict[str, Any] = {
            "mode": "unscoped",
            "active_property_id": "",
            "active_property_name": "",
            "matched_property_ids": [],
            "history_property_ids": [],
            "selected_property_ids": [],
            "property_manifest": [],
            "expected_property_count": 0,
            "detected_property_count": 0,
            "count_mismatch": False,
            "requires_clarification": False,
            "clarification_question": "",
        }

        service_knowledge_block = ""
        kb_without_service_block = raw_text
        service_marker = "=== SERVICE KNOWLEDGE BASE ==="
        if service_marker in raw_text:
            prefix, _sep, suffix = raw_text.partition(service_marker)
            kb_without_service_block = prefix.strip()
            service_knowledge_block = f"{service_marker}\n{suffix.strip()}".strip()

        history_text = self._build_property_scope_history_text(context=context)
        pending_raw = context.pending_data if isinstance(context.pending_data, dict) else {}
        seed_property_id = self._normalize_identifier(
            pending_raw.get("_selected_property_scope_id")
            or pending_raw.get("selected_property_id")
            or ""
        )
        try:
            property_scope = await config_service.build_property_scoped_kb_context(
                user_message=user_message,
                history_text=history_text,
                seed_property_id=seed_property_id,
                max_sources=200,
                max_scope_chars=max(inline_budget, 90000),
                preloaded_full_kb_text=raw_text,
                allow_llm_generation=False,
            )
        except Exception:
            property_scope = dict(property_scope)

        prepared_text = str(
            (property_scope.get("scoped_full_kb_text") if isinstance(property_scope, dict) else "")
            or kb_without_service_block
            or raw_text
        ).strip()
        if service_knowledge_block:
            prepared_text = (
                f"{prepared_text}\n\n{service_knowledge_block}".strip()
                if prepared_text
                else service_knowledge_block
            )

        if len(prepared_text) <= inline_budget:
            return {
                "full_kb_text": prepared_text,
                "property_scope": property_scope,
            }

        if not bool(getattr(settings, "full_kb_llm_chunk_scan_enabled", True)):
            return {
                "full_kb_text": prepared_text[:inline_budget],
                "property_scope": property_scope,
            }
        if not str(settings.openai_api_key or "").strip():
            return {
                "full_kb_text": prepared_text[:inline_budget],
                "property_scope": property_scope,
            }

        max_scan_chunks = max(
            1,
            int(getattr(settings, "full_kb_llm_chunk_max_chunks", 24) or 24),
        )
        chunk_chars = max(
            4000,
            int(getattr(settings, "full_kb_llm_chunk_chars", 18000) or 18000),
        )
        if len(prepared_text) > chunk_chars * max_scan_chunks:
            chunk_chars = max(chunk_chars, (len(prepared_text) + max_scan_chunks - 1) // max_scan_chunks)
        chunks = self._split_kb_text_for_llm(kb_text=prepared_text, chunk_chars=chunk_chars)
        if not chunks:
            return {
                "full_kb_text": prepared_text[:inline_budget],
                "property_scope": property_scope,
            }

        model = (
            str(getattr(settings, "llm_orchestration_model", "") or "").strip()
            or str(getattr(settings, "openai_model", "") or "").strip()
            or None
        )
        temperature = float(getattr(settings, "full_kb_llm_temperature", 0.1) or 0.1)
        source_manifest = self._build_kb_source_manifest(prepared_text)
        try:
            system_prompt = await prompt_registry.get("orchestrator.kb_chunk_scan")
        except PromptMissingError:
            logger.exception("orchestrator_kb_chunk_scan_prompt_missing")
            return {
                "full_kb_text": prepared_text[:inline_budget],
                "property_scope": property_scope,
            }

        extracted_parts: list[str] = []
        for index, chunk in enumerate(chunks, start=1):
            user_prompt = (
                f"GUEST MESSAGE:\n{str(user_message or '').strip()}\n\n"
                f"KB CHUNK {index}/{len(chunks)}:\n{chunk}\n\n"
                "Return only the relevant facts from this KB chunk. "
                "If none, return exactly: NO_RELEVANT_INFO"
            )
            try:
                result = await llm_client.chat(
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    model=model,
                    temperature=temperature,
                    max_tokens=2200,
                    trace_context={
                        "responder_type": "main",
                        "agent": "full_kb_chunk_scan",
                        "session_id": str(getattr(context, "session_id", "")),
                        "chunk_index": index,
                        "chunk_count": len(chunks),
                    },
                )
            except Exception:
                continue
            text = str(result or "").strip()
            if not text or text.upper().startswith("NO_RELEVANT_INFO"):
                continue
            if text not in extracted_parts:
                extracted_parts.append(text)

        if not extracted_parts:
            return {
                "full_kb_text": prepared_text[:inline_budget],
                "property_scope": property_scope,
            }

        result_budget = max(
            8000,
            int(getattr(settings, "full_kb_llm_chunk_result_chars", 32000) or 32000),
        )
        sections: list[str] = []
        if source_manifest:
            sections.append(
                "=== FULL KB SOURCES INCLUDED ===\n"
                + "\n".join(f"- {name}" for name in source_manifest)
            )
        sections.append(
            "=== QUESTION-RELEVANT FACTS FROM FULL KB CHUNK SCAN ===\n"
            + "\n\n---\n\n".join(extracted_parts)
        )
        prepared = "\n\n".join(section for section in sections if section).strip()
        _log_llm_call(
            label="FULL KB CHUNK SCAN SUMMARY",
            session_id=str(getattr(context, "session_id", "")),
            user_message=user_message,
            system_prompt="",
            payload={
                "raw_kb_chars": len(raw_text),
                "chunk_count": len(chunks),
                "result_chars": len(prepared),
                "source_manifest": source_manifest[:50],
            },
        )
        return {
            "full_kb_text": prepared[:result_budget],
            "property_scope": property_scope,
        }

    def _service_kb_by_service(self, capabilities_summary: dict[str, Any]) -> dict[str, list[str]]:
        records = capabilities_summary.get("service_kb_records", [])
        if not isinstance(records, list):
            return {}
        mapping: dict[str, list[str]] = {}
        for row in records[:220]:
            if not isinstance(row, dict):
                continue
            service_id = self._normalize_identifier(row.get("service_id"))
            if not service_id:
                continue
            facts_value = row.get("facts")
            facts: list[str] = []
            if isinstance(facts_value, list):
                for fact in facts_value:
                    if not isinstance(fact, dict):
                        continue
                    if str(fact.get("status") or "approved").strip().lower() not in {"approved", ""}:
                        continue
                    text = str(fact.get("text") or "").strip()
                    if not text:
                        continue
                    facts.append(text[:320])
                    if len(facts) >= 30:
                        break
            mapping[service_id] = facts
        return mapping

    @staticmethod
    def _parse_property_scoped_knowledge_sections(text: str) -> dict[str, Any]:
        raw = str(text or "").strip()
        if not raw or "=== PROPERTY:" not in raw and "=== COMMON KNOWLEDGE ===" not in raw:
            return {"common": "", "properties": []}
        common_lines: list[str] = []
        properties: list[dict[str, Any]] = []
        current_kind = ""
        current_property: dict[str, Any] | None = None
        header_pattern = re.compile(r"^=== PROPERTY:\s*(.+?)(?:\s*\|\s*ALIASES:\s*(.+?))?\s*===\s*$", re.IGNORECASE)

        def flush_current() -> None:
            nonlocal current_property
            if current_kind == "property" and isinstance(current_property, dict):
                content = "\n".join(current_property.pop("_lines", [])).strip()
                if content:
                    current_property["content"] = content
                    properties.append(current_property)
            current_property = None

        for line in raw.splitlines():
            if re.match(r"^=== COMMON KNOWLEDGE ===\s*$", line.strip(), flags=re.IGNORECASE):
                flush_current()
                current_kind = "common"
                continue
            header_match = header_pattern.match(line.strip())
            if header_match:
                flush_current()
                current_kind = "property"
                property_name = re.sub(r"\s+", " ", str(header_match.group(1) or "").strip())
                aliases_raw = str(header_match.group(2) or "").strip()
                aliases = [
                    re.sub(r"\s+", " ", alias.strip())
                    for alias in aliases_raw.split(",")
                    if alias.strip()
                ]
                current_property = {
                    "property_name": property_name,
                    "aliases": aliases,
                    "_lines": [],
                }
                continue
            if current_kind == "common":
                common_lines.append(line)
            elif current_kind == "property" and isinstance(current_property, dict):
                current_property.setdefault("_lines", []).append(line)
        flush_current()
        return {
            "common": "\n".join(common_lines).strip(),
            "properties": properties,
        }

    def _scope_service_knowledge_to_runtime_property(
        self,
        *,
        service_knowledge: str,
        user_message: str,
        context: ConversationContext,
    ) -> str:
        """Filter extracted_knowledge to only the active property/properties.

        Priority order:
        1. LLM-determined ``_active_property_ids`` (set by orchestrator)
        2. Legacy ``_selected_property_scope_ids`` / ``_selected_property_scope_name``
        3. No active properties → return full KB (all properties, no truncation)
        """
        parsed = self._parse_property_scoped_knowledge_sections(service_knowledge)
        properties = parsed.get("properties", [])
        if not isinstance(properties, list) or not properties:
            return str(service_knowledge or "").strip()

        common = str(parsed.get("common") or "").strip()
        pending_raw = context.pending_data if isinstance(context.pending_data, dict) else {}

        # ── Get active property IDs from LLM state ──
        active_ids = list(pending_raw.get("_active_property_ids") or [])
        # Legacy fallback
        if not active_ids:
            legacy_ids = pending_raw.get("_selected_property_scope_ids") or []
            if isinstance(legacy_ids, str):
                legacy_ids = [legacy_ids]
            active_ids = [str(pid).strip() for pid in legacy_ids if str(pid).strip()]
        if not active_ids:
            legacy_name = str(pending_raw.get("_selected_property_scope_name") or "").strip()
            if legacy_name:
                active_ids = [legacy_name]

        # Stay-property-pin fallback: if the guest has a confirmed booking at a
        # specific property, use that as the scope even when no active_ids are set.
        # This ensures service agents (banquet, dining, etc.) default to the guest's
        # stay property rather than showing all properties.
        if not active_ids:
            stay_property_id = str(pending_raw.get("_stay_property_id") or "").strip()
            stay_property_name = str(pending_raw.get("_stay_property_name") or "").strip()
            if stay_property_name:
                active_ids = [stay_property_name]
            elif stay_property_id:
                active_ids = [stay_property_id]

        if not active_ids:
            # No active properties — return full KB with all properties
            return str(service_knowledge or "").strip()

        # Match active IDs against parsed property sections. Normalize both slug-style
        # ids (`goa_north`) and human labels (`Goa North`) so scoped KB selection does
        # not drop out when the runtime state and KB headers use different separators.
        def _scope_key_variants(value: Any) -> list[str]:
            raw = str(value or "").strip().lower()
            if not raw:
                return []
            normalized = re.sub(r"[^a-z0-9]+", " ", raw).strip()
            compact = normalized.replace(" ", "")
            # KB property headers often read "Zone Connect By The Park <City>"
            # while runtime ids are slugs like "zone_connect_park_<city>" (no
            # "by the"). Drop filler articles/prepositions so the two forms match.
            # Distinctive brand tokens like "connect" and "park" are KEPT, so two
            # properties in the same city ("Zone Connect ... Coimbatore" vs
            # "Zone ... Coimbatore") still resolve to different sections.
            _fillers = {"by", "the", "of", "a", "an", "at"}
            dearticled = " ".join(
                tok for tok in normalized.split() if tok not in _fillers
            ).strip()
            dearticled_compact = dearticled.replace(" ", "")
            result: list[str] = []
            for candidate in (raw, normalized, compact, dearticled, dearticled_compact):
                if candidate and candidate not in result:
                    result.append(candidate)
            return result

        _active_lowers = [
            _scope_key_variants(aid)
            for aid in active_ids
            if str(aid).strip()
        ]
        matched: list[dict[str, Any]] = []
        for row in properties:
            if not isinstance(row, dict):
                continue
            prop_name = str(row.get("property_name") or "").strip()
            aliases = [str(a).strip() for a in (row.get("aliases") or []) if str(a).strip()]
            all_labels = []
            for label in [prop_name] + aliases:
                all_labels.extend(_scope_key_variants(label))
            _is_match = any(
                _aid in lbl or lbl in _aid
                for active_variants in _active_lowers
                for _aid in active_variants
                for lbl in all_labels
                if len(lbl) >= 3 and len(_aid) >= 3
            )
            if _is_match:
                matched.append(row)

        if matched:
            # Active properties become the FULL-detail scope. Any property the
            # guest discussed earlier and moved on from ("previously discussed")
            # is kept as a brief, clearly-labelled context block so the agent
            # never fully loses it — if the guest references the other property
            # again the router widens scope, but in the meantime the agent can
            # still acknowledge it instead of pretending it doesn't exist.
            matched_keys = {
                id(row) for row in matched
            }
            past_rows: list[dict[str, Any]] = []
            past_ctx = pending_raw.get("_past_property_context")
            past_ctx = past_ctx if isinstance(past_ctx, dict) else {}
            if past_ctx:
                _past_variants = [
                    _scope_key_variants(pid) for pid in past_ctx.keys() if str(pid).strip()
                ]
                for row in properties:
                    if not isinstance(row, dict) or id(row) in matched_keys:
                        continue
                    prop_name = str(row.get("property_name") or "").strip()
                    aliases = [str(a).strip() for a in (row.get("aliases") or []) if str(a).strip()]
                    all_labels = []
                    for label in [prop_name] + aliases:
                        all_labels.extend(_scope_key_variants(label))
                    if any(
                        _pid in lbl or lbl in _pid
                        for variants in _past_variants
                        for _pid in variants
                        for lbl in all_labels
                        if len(lbl) >= 3 and len(_pid) >= 3
                    ):
                        past_rows.append(row)
            return self._build_scoped_kb_text(
                common=common, property_rows=matched, past_rows=past_rows
            )

        # No matches found — return full KB rather than losing data
        return self._build_scoped_kb_text(common=common, property_rows=[])

    @staticmethod
    def _build_scoped_kb_text(
        *,
        common: str,
        property_rows: list[dict[str, Any]],
        past_rows: list[dict[str, Any]] | None = None,
    ) -> str:
        """Assemble scoped KB text from common knowledge + matched property rows.

        ``property_rows`` are the ACTIVE properties (full detail). ``past_rows``
        are previously-discussed properties kept as a short context block so the
        agent can acknowledge them without losing focus on the active one(s).
        """
        parts: list[str] = []
        if common:
            parts.append("=== COMMON KNOWLEDGE ===\n" + common)
        for row in property_rows:
            property_name = str(row.get("property_name") or "").strip()
            aliases = [str(a).strip() for a in (row.get("aliases") or []) if str(a).strip()]
            content = str(row.get("content") or "").strip()
            if not content:
                continue
            alias_block = f" | ALIASES: {', '.join(aliases[:6])}" if aliases else ""
            parts.append(f"=== PROPERTY: {property_name}{alias_block} ===\n{content}")
        for row in (past_rows or []):
            if not isinstance(row, dict):
                continue
            property_name = str(row.get("property_name") or "").strip()
            content = str(row.get("content") or "").strip()
            if not (property_name or content):
                continue
            # Trim past-property detail so it stays light; full detail comes back
            # if the router moves it into the active scope again.
            snippet = content[:1200].rstrip()
            if len(content) > 1200:
                snippet += " …"
            parts.append(
                f"=== PREVIOUSLY DISCUSSED PROPERTY (context only): {property_name} ===\n"
                f"{snippet}\n"
                "(The guest moved on from this property. If they ask about it again, "
                "you may briefly acknowledge it, but keep your focus on the active property above.)"
            )
        return "\n\n".join(parts).strip() if parts else ""

    @classmethod
    def _is_room_booking_service(
        cls,
        *,
        service_id: str,
        service_name: str = "",
        description: str = "",
        trigger_field_id: str = "",
    ) -> bool:
        norm_id = cls._normalize_identifier(service_id)
        norm_name = cls._compact_message_text(service_name)
        norm_desc = cls._compact_message_text(description)
        norm_trigger = cls._normalize_identifier(trigger_field_id)
        if norm_trigger == "room_type" and "room_booking" in norm_id:
            return True
        if "room booking" in norm_name or "book room" in norm_name:
            return True
        return any(
            marker in norm_desc
            for marker in (
                "room booking",
                "booking rooms",
                "book rooms",
                "book a room",
                "exploring and/or booking rooms",
                "exploring and booking rooms",
            )
        )

    @classmethod
    def _message_requests_booking_critical_room_detail(cls, message: str) -> bool:
        compact = cls._compact_message_text(message)
        if not compact:
            return False
        markers = (
            "price",
            "prices",
            "rate",
            "rates",
            "cost",
            "costs",
            "tariff",
            "how much",
            "size",
            "sizes",
            "area",
            "dimensions",
            "sq ft",
            "square feet",
            "sqm",
            "availability",
            "available",
            "vacancy",
            "vacancies",
            "rooms left",
            "compare",
            "comparison",
            "difference",
            "versus",
            " vs ",
            "cheapest",
            "cheaper",
            "affordable",
            "best value",
            "bigger",
            "larger",
            "smallest",
            "largest",
        )
        return any(marker in compact for marker in markers)

    @classmethod
    def _normalize_room_booking_missing_detail_handoff(
        cls,
        *,
        decision: OrchestrationDecision,
        user_message: str,
        service_id: str,
        service_name: str = "",
        description: str = "",
        trigger_field_id: str = "",
    ) -> OrchestrationDecision:
        if not cls._is_room_booking_service(
            service_id=service_id,
            service_name=service_name,
            description=description,
            trigger_field_id=trigger_field_id,
        ):
            return decision
        if str(decision.action or "").strip().lower() != "respond_only":
            return decision
        if str(decision.pending_action or "").strip():
            return decision
        if bool((decision.metadata or {}).get("context_switched")):
            return decision
        if not cls._message_requests_booking_critical_room_detail(user_message):
            return decision
        if not cls._response_signals_missing_room_detail(decision.response_text):
            return decision
        if cls._response_offers_staff_handoff(decision.response_text):
            return decision
        decision.response_text = (
            "- I don't have that detail right now.\n"
            "- Would you like our team to reach out to share that with you?"
        )
        decision.suggested_actions = []
        decision.metadata["room_booking_missing_detail_handoff_forced"] = True
        return decision

    @classmethod
    def _assistant_message_invites_room_booking(cls, last_assistant_message: str) -> bool:
        text = str(last_assistant_message or "").strip()
        compact = cls._compact_message_text(text)
        if not compact:
            return False
        if cls._extract_confirmable_room_from_last_assistant(text):
            return True
        booking_markers = (
            "assist you with booking",
            "help you with booking",
            "would you like assistance with booking",
            "would you like me to assist you with booking",
            "would you like to book",
            "would you like to proceed with booking",
            "book a room",
            "booking a room",
            "book your stay",
            "room booking",
            "proceed with booking",
            "continue with booking",
        )
        return any(marker in compact for marker in booking_markers) and any(
            token in compact for token in ("room", "suite", "stay")
        )

    @classmethod
    def _build_room_booking_selection_response(cls, room_candidates: list[str]) -> str:
        candidates = cls._dedupe_labels(room_candidates, limit=8)
        if not candidates:
            return (
                "I can help with the booking. Please tell me which room type you'd like to book so I can continue."
            )
        lines = ["I can help with the booking. Please choose a room type to continue:"]
        lines.extend(f"- {candidate}" for candidate in candidates)
        return "\n".join(lines)

    @classmethod
    def _rewrite_room_booking_response_for_room_selection(
        cls,
        response_text: str,
        room_candidates: list[str],
    ) -> str:
        cleaned = strip_form_confirmation_instructions(response_text)
        for pattern in (
            r"\s*(?:once\s+confirmed[\s,.-]*)?please\s+fill\s+in\s+the\s+booking\s+details\s+below\.?",
            r"\s*please\s+fill\s+in\s+the\s+details\s+below\.?",
            r"\s*please\s+fill\s+in\s+the\s+form\s+below\.?",
            r"\s*the\s+form\s+will\s+appear(?:\s+automatically)?\.?",
            r"\s*please\s+proceed\s+with\s+the\s+booking\s+details(?:\s+below)?\.?",
        ):
            cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
        cleaned = re.sub(r"[ \t]+([,.;!?])", r"\1", cleaned)
        cleaned = re.sub(r"([.!?])\s*[.!?]+", r"\1", cleaned)
        cleaned = re.sub(r",\s*\.", ".", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
        if not cleaned:
            return cls._build_room_booking_selection_response(room_candidates)

        compact = cls._compact_message_text(cleaned)
        selection_markers = (
            "which specific room type",
            "which room type",
            "let me know which specific room type",
            "let me know which room type",
            "please choose a room type",
            "please choose which room type",
            "please confirm which specific room type",
        )
        if any(marker in compact for marker in selection_markers):
            return cleaned
        if cleaned.endswith((".", "!", "?")):
            return f"{cleaned}\nPlease let me know which specific room type you'd like to book."
        return f"{cleaned}.\nPlease let me know which specific room type you'd like to book."

    async def _maybe_area_web_fact(
        self,
        *,
        user_message: str,
        kb_content: str,
        context: "ConversationContext",
    ) -> str:
        """Search the web for a neighbourhood question the KB cannot answer.

        Only reached when the router opened the Location & Transport book, so
        it never runs on an ordinary turn. Returns "" on anything unexpected —
        the answerer then declines exactly as it did before, which is correct
        and is never a guess.
        """
        if not web_lookup_service.is_enabled():
            return ""
        from services import prebooking_settings_service

        _admin = prebooking_settings_service.settings_for(getattr(context, "hotel_code", ""))
        if _admin is not None and not _admin.get("outside_recommendations", False):
            # Pre-booking tab: "Recommend places outside the hotel" is off.
            return ""
        message = str(user_message or "").strip()
        if len(message) < 6:
            return ""

        pending = context.pending_data if isinstance(context.pending_data, dict) else {}
        hints = self._normalize_property_id_list(list(pending.get("_active_property_ids") or []))
        if not hints:
            return ""
        # EVERY property in focus gets looked up, not just a single one. A guest
        # sitting on a two-hotel city intro asking "any vets nearby?" must get an
        # answer for both hotels — they are in different neighbourhoods, so one
        # answer would be wrong for the other. Capped so a broad scope (a whole
        # portfolio) cannot fan out into a dozen calls on one turn.
        hints = hints[:3]

        try:
            from services.image_tool_service import image_tool_service

            hotel_code = str(getattr(context, "hotel_code", "") or "").strip()
            targets: list[tuple[int, str, str]] = []
            for hint in hints:
                property_id = await image_tool_service.resolve_active_property_id(
                    hotel_code=hotel_code, property_hint=str(hint)
                )
                if property_id is None:
                    continue
                property_name, city = await self._property_name_and_city(property_id)
                if city:
                    targets.append((int(property_id), property_name, city))
            if not targets:
                return ""

            # PRIORITY ORDER
            # 1. KB first. If the hotel already publishes this (airport, station,
            #    bus stand, hospital, its own facilities), neither lookup runs at
            #    all — the KB answers and the turn costs nothing extra. This is
            #    what stopped "how far is the airport" burning 5s on a search for
            #    something already on file.
            # 2. Otherwise web search AND OpenStreetMap together, IN PARALLEL.
            #    They cover each other's weaknesses: web understands a vague or
            #    misspelt place and knows what it actually is, OSM supplies the
            #    exact road distance (measured: web was out by 31% and 64%).
            #    Running them together costs max(web, osm), not the sum.
            decision = await self._classify_area_question(message)
            place = str(decision.get("place") or "").strip()
            started = _time.monotonic()
            blocks: list[str] = []

            # The split that matters is whether the guest NAMED a place, not
            # whether the KB covers it.
            #
            # "How far is X" wants one exact distance. "What's nearby for
            # chilling" wants a list. Keying off kb_covered got the airport
            # right (covered -> distance) but sent "how far is Calangute Beach?"
            # into the nearby-places SEARCH, which answers with a list of
            # beaches instead of the distance to that one — while Routes
            # resolves it exactly: 1,877 m, 9 min drive, 24 min walk.
            if place and google_places_service.is_enabled():
                for pid, pname, pcity in targets[:1]:
                    try:
                        origin = await geo_distance_service.property_origin(pid)
                        if not origin:
                            continue
                        d = await google_places_service.distance_to_place(
                            origin=origin, place=place, city=pcity
                        )
                        # Belt and braces on the classifier. Asked "which
                        # pharmacy is open right now?" it once returned
                        # place="pharmacy", which geocoded to a pharmacy 62 km
                        # away and produced "the only nearby fact I have is
                        # pharmacy is about 62.5 km". A landmark someone asks
                        # the distance to is not 60 km from where they are
                        # standing; if it looks like that, treat it as a
                        # category and fall through to the nearby search.
                        if d and d.get("meters", 0) > 60_000:
                            logger.info(
                                "[AREA] %r resolved %0.1f km away — treating as a "
                                "category, not a landmark",
                                place, d["meters"] / 1000,
                            )
                            d = None
                        if d:
                            return google_places_service.distance_as_kb_fact(d, pname)
                    except Exception:
                        pass

            if decision.get("kb_covered"):
                # Covered by the hotel's own records and no specific place to
                # measure — answer from the KB with no external call at all.
                logger.info("[AREA] KB covers it — no external lookup")
                return ""

            # GOOGLE MAPS FIRST, web search only as the fallback.
            #
            # OpenStreetMap was dropped because the public Nominatim geocoder
            # returns 429 at our rate (a volunteer service capped near 1 req/s,
            # ~10 needed per question, each rejection ~3s — one turn reached
            # 44s and still answered nothing).
            #
            # Web search alone then supplied names but no distances: measured
            # against real routing it was out by 31% and 64% on two of five
            # places, and fabricated a maps URL every time. It was also
            # inconsistent — on the live bot "any hospitals nearby?" returned
            # nothing for Calangute while "where can I get medicines?" answered
            # fine seconds later in the same session.
            #
            # Google answers both, with real road distance and drive time, in
            # about a second. Measured across all four rebuilt properties:
            # 1.0-2.8s per lookup versus 11-30s for web search.
            google_rows: dict[str, list[Any]] = {}
            if google_places_service.is_enabled():
                async def _g(pid: int, pname: str):
                    try:
                        origin = await geo_distance_service.property_origin(pid)
                        if not origin:
                            return pname, []
                        return pname, await google_places_service.nearby(
                            question=message, origin=origin
                        )
                    except Exception:
                        return pname, []

                # One search per property in scope, so a two-hotel city gets an
                # answer for both. Their circles overlap though, and joining the
                # results without dedupe listed the same place twice at two
                # different distances — "Zone by The Park Hotel, Coimbatore" at
                # 1.1 km and again at 7.8 km, measured from each hotel. Keep the
                # first sighting, which is the nearer one.
                _seen_places: set[str] = set()
                for _pname, _rows in await asyncio.gather(
                    *[_g(pid, pname) for pid, pname, _c in targets]
                ):
                    _fresh = []
                    for _r in _rows or []:
                        _k = str(_r.get("name") or "").strip().lower()
                        if not _k or _k in _seen_places:
                            continue
                        _seen_places.add(_k)
                        _fresh.append(_r)
                    if _fresh:
                        google_rows[_pname] = _fresh
                if google_rows:
                    for pname, rows in google_rows.items():
                        blocks.append(google_places_service.as_kb_fact(rows, pname))
                    logger.info(
                        "[AREA] google places answered %d/%d properties in %.1fs",
                        len(google_rows), len(targets), _time.monotonic() - started,
                    )

            # Only the properties Google could not answer for fall through.
            targets = [t for t in targets if t[1] not in google_rows]
            if not targets:
                return "\n".join(b for b in blocks if b).strip()

            jobs: list[Any] = []
            kinds: list[tuple[str, str]] = []
            for _pid, pname, pcity in targets:
                jobs.append(
                    web_lookup_service.lookup(
                        question=message, hotel_name=pname, city=pcity
                    )
                )
                kinds.append(("web", pname))

            outcomes = await asyncio.gather(*jobs, return_exceptions=True)

            for (kind, pname), outcome in zip(kinds, outcomes):
                if kind == "web" and isinstance(outcome, str) and outcome:
                    blocks.append(web_lookup_service.as_kb_fact(outcome, pname))

            # Name any property we could NOT find anything for. Dropping it
            # silently let a two-hotel answer look complete when only one hotel
            # had been covered.
            answered_names = {
                pname for (kind, pname), outcome in zip(kinds, outcomes)
                if (kind == "web" and isinstance(outcome, str) and outcome)
                or (kind == "osm" and isinstance(outcome, dict) and outcome)
            }
            missing = [pname for _pid, pname, _pcity in targets if pname not in answered_names]
            if blocks and missing:
                blocks.append(
                    "- No nearby results were found for: "
                    + "; ".join(missing)
                    + ". Say so plainly for that property rather than leaving it out."
                )
            logger.info(
                "[AREA] %.1fs place=%r properties=%d blocks=%d missing=%d",
                _time.monotonic() - started, place or "-", len(targets),
                len(blocks), len(missing),
            )
        except Exception as exc:
            logger.warning("area lookup failed: %s", exc)
            return ""

        # Both hotels answered? Tell the answerer to keep them separate, or it
        # merges two neighbourhoods into one list the guest cannot act on.
        if len(blocks) > 1:
            blocks.append(
                "- The blocks above cover DIFFERENT properties. Group the nearby "
                "places under each hotel's name; never merge them into one list."
            )
        return ("\n" + "\n").join(blocks)

    @staticmethod
    async def _property_name_and_city(property_id: int) -> tuple[str, str]:
        """(name, city) for a property id — the city is the search fence."""
        try:
            import json as _json

            from sqlalchemy import select as _select

            from models.database import AsyncSessionLocal, Property

            async with AsyncSessionLocal() as session:
                row = (
                    await session.execute(_select(Property).where(Property.id == int(property_id)))
                ).scalars().first()
            if row is None:
                return "", ""
            location = getattr(row, "location", None)
            if isinstance(location, str):
                try:
                    location = _json.loads(location)
                except Exception:
                    location = None
            city = (
                str((location or {}).get("city") or "").strip()
                if isinstance(location, dict)
                else ""
            )
            return str(getattr(row, "name", "") or "").strip(), city
        except Exception:
            return "", ""

    async def _classify_area_question(self, message: str) -> dict[str, Any]:
        """Decide how to handle a question about the area.

        Returns {"kb_covered": bool, "place": str}.

        `kb_covered` is the important one: the hotel already publishes its own
        distance to the airport, railway station and bus stand, and its own
        facilities are in the KB. For those, no external lookup should run at
        all — previously they fell through to a web search and wasted ~5s
        answering something already on file.

        HOSPITAL used to be on that list, and it was wrong. Counted across the
        24 Zone properties: 24/24 publish a "Distance from airport", but 0/24
        publish any hospital distance and only 6 mention the word at all. So
        "what hospitals are nearby" was classified as already-covered, skipped
        the lookup, and the answerer then declined — while "any parks nearby",
        which was never on the list, searched and answered fine.
        """
        try:
            raw = await asyncio.wait_for(
                llm_client.chat_with_json(
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "A hotel guest asked something about the hotel's area.\n"
                                'Return {"kb_covered": true|false, "place": "<name or empty>"}.\n'
                                "kb_covered = true when they ask about the AIRPORT, "
                                "RAILWAY STATION, BUS STAND, or the hotel's own "
                                "rooms / dining / facilities / policies. The hotel already "
                                "publishes those, so nothing external is needed.\n"
                                "Only the hotel's OWN outlets count as covered dining. "
                                "Anywhere to eat or drink OUTSIDE the hotel — 'dining options "
                                "outside the hotel', 'restaurants nearby', 'cafes around here', "
                                "'somewhere else to eat', 'any good bars near you' — is an ASK "
                                "ABOUT THE AREA that the hotel does not publish: kb_covered = "
                                "false, always look it up. Treating it as covered is why the bot "
                                "answered 'no outside dining details are available'.\n"
                                "HOSPITALS, clinics, pharmacies and doctors are NOT covered "
                                "by the hotel's own records — always look those up.\n"
                                "place = ONE specific named landmark they want the distance "
                                "to — a proper name you could point to on a map, like 'Hawa "
                                "Mahal', 'Eden Gardens', 'Phoenix Marketcity', 'Calangute "
                                "Beach'.\n"
                                "It must be EMPTY for a category. 'pharmacy', 'hospital', "
                                "'restaurant', 'cafe', 'atm', 'park', 'bar' are kinds of "
                                "place, not places — those are general nearby asks and must "
                                "return an empty place. Returning 'pharmacy' here sent the "
                                "bot measuring the distance to a pharmacy 62 km away instead "
                                "of listing the ones next door.\n"
                                "Also empty for 'any parks nearby' or 'where can I eat'.\n"
                                "Both false/empty is fine — that means a general nearby ask.\n"
                                "Never invent a place that is not in the message."
                            ),
                        },
                        {"role": "user", "content": str(message or "")[:400]},
                    ],
                    model=(
                        str(getattr(settings, "chat_suggestions_llm_model", "") or "").strip()
                        or None
                    ),
                    temperature=0.0,
                    max_tokens=60,
                    trace_context={
                        "component": "services.llm_orchestration_grounding_mixin",
                        "what_this_call_is_for": "Route an area question: KB / OSM / web",
                    },
                ),
                timeout=4.0,
            )
        except Exception:
            return {"kb_covered": False, "place": ""}
        place = str((raw or {}).get("place") or "").strip()
        return {
            "kb_covered": bool((raw or {}).get("kb_covered")),
            "place": place if len(place) >= 3 else "",
        }

    async def _run_book_answerer_llm(
        self,
        *,
        user_message: str,
        book_context_text: str,
        history_bundle: dict[str, Any],
        faq_candidates: list[dict[str, Any]],
        selected_phase_id: str,
        selected_phase_name: str,
        context: ConversationContext,
        memory_snapshot: dict[str, Any],
        in_phase_services: list[dict[str, Any]] | None = None,
        out_of_phase_services: list[dict[str, Any]] | None = None,
        book_route_context: dict[str, Any] | None = None,
        router_selected_property_ids: list[str] | None = None,
        wait_for_chips: bool = False,
    ) -> tuple[str, list[str], list[str], str]:
        """Single-call answerer used when main_router decided route=book/clarify/auto.

        The router IS the orchestrator. Once it has selected books and decided
        no service execution is needed, this single LLM call produces the final
        answer. Receives the books the router chose, the recent history, FAQ
        candidates, and a small list of available/unavailable services so it
        can be phase-aware without seeing the full hotel config.
        """
        if not str(settings.openai_api_key or "").strip():
            return "", [], [], ""

        def _extract_property_markers_from_book_context(
            text: str,
            *,
            max_properties: int = 10,
        ) -> list[dict[str, Any]]:
            markers: list[dict[str, Any]] = []
            counts: dict[str, int] = {}
            order: list[str] = []
            for raw_line in str(text or "").splitlines():
                line = str(raw_line or "").strip()
                if not line.startswith("[PROPERTY:"):
                    continue
                marker = re.search(r"\[PROPERTY:\s*([^\|\]]+)", line, flags=re.IGNORECASE)
                if not marker:
                    continue
                name = re.sub(r"\s+", " ", str(marker.group(1) or "").strip())
                if not name:
                    continue
                if name not in counts:
                    counts[name] = 0
                    order.append(name)
                counts[name] += 1
            for name in order[: max(1, max_properties)]:
                markers.append({"name": name, "marker_count": int(counts.get(name) or 0)})
            return markers

        def _extract_book_labels_from_context(text: str) -> list[str]:
            """Book titles the router opened for THIS turn.

            KB blocks are tagged `[PROPERTY:x | BOOK:y | PAGE:z]`, so the books
            already tell us what the turn is about — rooms, dining, spa... That
            is the topic signal for picking images, and it costs nothing: the
            router has already decided, so there are no extra tokens and no
            extra latency (which asking the answerer for a marker would add).
            """
            seen: list[str] = []
            for raw_line in str(text or "").splitlines():
                line = str(raw_line or "").strip()
                if not line.startswith("[PROPERTY:"):
                    continue
                hit = re.search(r"\|\s*BOOK:\s*([^\|\]]+)", line, flags=re.IGNORECASE)
                if not hit:
                    continue
                label = re.sub(r"\s+", " ", str(hit.group(1) or "").strip())
                if label and label not in seen:
                    seen.append(label)
            return seen

        def _build_multi_property_answerer_contract(
            properties: list[dict[str, Any]],
        ) -> str:
            names = [
                str(item.get("name") or "").strip()
                for item in properties
                if isinstance(item, dict) and str(item.get("name") or "").strip()
            ]
            if len(names) < 2:
                return ""
            count = len(names)
            preview = ", ".join(names[:12])
            if count > 12:
                preview = preview + f", ... ({count} total)"
            return (
                "MULTI-PROPERTY CONTRACT:\n"
                f"- {count} properties are in scope: {preview}.\n"
                "- The guest has not chosen one property yet.\n"
                "- Help them compare or narrow down without silently picking one.\n"
                "- Keep the answer compact and easy to scan.\n"
                "- If the answer differs by property, cover each relevant property briefly.\n"
                "- If the answer is effectively the same across them, answer once instead of repeating it.\n"
                "- Do not dump every fact. Surface only the most decision-useful differences.\n"
                "- End with one natural next step that helps the guest narrow down.\n"
            )

        kb_char_limit = max(
            4000,
            int(getattr(settings, "chat_llm_book_answerer_max_kb_chars", 120000) or 120000),
        )
        summary_char_limit = max(
            300,
            int(
                getattr(settings, "chat_llm_book_answerer_memory_summary_chars", 1400) or 1400
            ),
        )
        history_max_messages = max(
            2,
            int(getattr(settings, "chat_llm_book_answerer_history_messages", 16) or 16),
        )
        history_max_chars = max(
            500,
            int(getattr(settings, "chat_llm_book_answerer_history_chars", 8000) or 8000),
        )
        history_per_item_cap = max(180, min(900, history_max_chars // max(1, history_max_messages)))

        compact_history: list[dict[str, str]] = []
        history_total_chars = 0
        raw_history = history_bundle.get("history_last_10", [])
        if isinstance(raw_history, list):
            for entry in list(raw_history)[-history_max_messages:]:
                if not isinstance(entry, dict):
                    continue
                role = str(entry.get("role") or "").strip().lower()
                if role not in {"user", "assistant", "system"}:
                    role = "user"
                content = str(entry.get("content") or "").strip()
                if not content:
                    continue
                remaining = history_max_chars - history_total_chars
                if remaining <= 0:
                    break
                clipped = content[: min(history_per_item_cap, remaining)]
                compact_history.append({"role": role, "content": clipped})
                history_total_chars += len(clipped)

        last_user_compact = str(history_bundle.get("last_user_message") or "").strip()[:600]
        last_assistant_compact = str(history_bundle.get("last_assistant_message") or "").strip()[:700]

        kb_content = str(book_context_text or "").strip()[:kb_char_limit]

        # ── Live neighbourhood lookup (web) ─────────────────────────────────
        # The KB holds the hotel's OWN published distances. Anything else about
        # the surrounding area — somewhere to eat, a vet, what's worth seeing —
        # is looked up on the web and appended as a sourced fact, so the bot can
        # answer instead of declining.
        #
        # Gated on the router having opened the Location & Transport book, which
        # is a free signal — no classifier runs on an ordinary turn, so nothing
        # here touches the latency of a normal reply.
        # Assigned unconditionally: the safety net below reads it on every turn,
        # and it is only set inside the gate.
        _live_fact = ""
        try:
            _opened = _extract_book_labels_from_context(kb_content)
            if any("location" in str(b).lower() for b in _opened):
                _live_fact = await self._maybe_area_web_fact(
                    user_message=user_message,
                    kb_content=kb_content,
                    context=context,
                )
                if _live_fact:
                    kb_content = kb_content + "\n\n" + _live_fact
        except Exception as _geo_exc:
            logger.warning("area web lookup skipped: %r", _geo_exc)
        pending = context.pending_data if isinstance(context.pending_data, dict) else {}
        active_property_ids = self._normalize_property_id_list(
            list(pending.get("_active_property_ids") or [])
        )
        past_property_context = dict(pending.get("_past_property_context") or {})

        # Build [{id, name}] from the books that fed this answer's KB. The book
        # context has property_id/property_slug + property_name per book — that's
        # the canonical, deduplicated source for FOCUS resolution. Falls back to
        # name-only markers parsed out of the KB text when book context is absent
        # (e.g. legacy `auto`-route fallback that builds context_text only).
        #
        # Stub-property filter: some hotels carry a placeholder property called
        # "Default" (or empty / "shared" / "common") that holds cross-property
        # KB pages. It is NOT a real bookable property and must never appear
        # as a heading in a guest reply. Treat such names as invisible — books
        # under them still flow through to the answerer, but the property
        # roster the answerer sees does not include them.
        _stub_property_names = {
            "default",
            "shared",
            "common",
            "all_properties",
            "all properties",
            "default_property",
            "default property",
            "property_default",
        }

        def _is_stub_property_scope(value: str) -> bool:
            raw = str(value or "").strip().lower()
            if not raw:
                return True
            normalized = re.sub(r"[^a-z0-9]+", " ", raw).strip()
            if raw in _stub_property_names or normalized in _stub_property_names:
                return True
            if normalized in {"default property", "shared property", "common property"}:
                return True
            if normalized.startswith("default "):
                return True
            if normalized.endswith(" property") and normalized.split()[0] in {"default", "shared", "common"}:
                return True
            return False
        book_properties: list[dict[str, Any]] = []
        seen_prop_ids: set[str] = set()
        if isinstance(book_route_context, dict):
            for book in (book_route_context.get("books") or []):
                if not isinstance(book, dict):
                    continue
                pid = (
                    self._normalize_identifier(book.get("property_slug"))
                    or self._normalize_identifier(book.get("property_id"))
                )
                pname = str(book.get("property_name") or "").strip()
                if not pid or pid in seen_prop_ids:
                    continue
                if _is_stub_property_scope(pname):
                    continue
                if _is_stub_property_scope(pid):
                    continue
                seen_prop_ids.add(pid)
                book_properties.append({"id": pid, "name": pname or pid})
        if not book_properties:
            for marker in _extract_property_markers_from_book_context(kb_content):
                pname = str((marker or {}).get("name") or "").strip()
                if not pname:
                    continue
                if _is_stub_property_scope(pname):
                    continue
                pid = self._normalize_identifier(pname)
                if not pid or pid in seen_prop_ids:
                    continue
                if _is_stub_property_scope(pid):
                    continue
                seen_prop_ids.add(pid)
                book_properties.append({"id": pid, "name": pname})

        _history_available_properties: list[dict[str, Any]] = []
        _history_seen_ids: set[str] = set()
        for property_id, property_name in self._normalize_property_name_map(
            pending.get("_property_name_map") if isinstance(pending, dict) else {}
        ).items():
            if not property_id or property_id in _history_seen_ids or not property_name:
                continue
            _history_seen_ids.add(property_id)
            _history_available_properties.append({"id": property_id, "name": property_name})
        for row in book_properties:
            if not isinstance(row, dict):
                continue
            property_id = self._normalize_identifier(row.get("id"))
            property_name = str(row.get("name") or "").strip()
            if not property_id or property_id in _history_seen_ids or not property_name:
                continue
            _history_seen_ids.add(property_id)
            _history_available_properties.append({"id": property_id, "name": property_name})

        # NARROW-SCOPE FILTER: when the guest has explicitly narrowed to a
        # SINGLE property (active_property_ids has exactly one id), strip any
        # other properties out of `book_properties` so they can't bleed into
        # the answerer's reply. Without this, stale entries from earlier
        # broader queries (broaden-fan-out, multi-property comparisons, prefetch
        # warming) sit in book_route_context and the LLM occasionally pivots to
        # unrelated properties — e.g. mentioning "Ira Orchid Sambhajinagar — no
        # spa" inside a Fort Jadhavgadh conversation.
        # When active_property_ids is empty or has 2+ entries the multi-property
        # fan-out is intentional, so leave book_properties alone.
        #
        # GATE: only strip when THIS turn's router did NOT deliberately broaden.
        # For a fleet / "which hotel" / compare / "other than X" ask the router
        # correctly selects many properties (router_selected_property_ids has
        # 2+), and the multi-property book_properties is the CORRECT answer set —
        # stripping it back to the single stale pin is exactly the bug where the
        # bot said "I only have Digha". When the router stayed narrow (0/1 id)
        # the strip still runs and cleans stale bleed-in from earlier broad
        # queries, as before.
        _router_broadened_this_turn = (
            isinstance(router_selected_property_ids, list)
            and len(router_selected_property_ids) > 1
        )
        if (
            isinstance(active_property_ids, list)
            and len(active_property_ids) == 1
            and len(book_properties) > 1
            and not _router_broadened_this_turn
        ):
            _active_only_id = self._normalize_identifier(active_property_ids[0])
            book_properties = [
                bp for bp in book_properties
                if self._normalize_identifier(bp.get("id")) == _active_only_id
            ]

        # Fire the HELP-FIRST contract whenever 2+ properties are in scope and
        # the guest hasn't committed to a single one yet. This covers both:
        #   - active_property_ids empty + multiple book_properties (first turn,
        #     no scope locked in)
        #   - active_property_ids has 2+ ids (router correctly broadened to a
        #     location with multiple properties — e.g. "rooms in mumbai" when
        #     there are 2 mumbai properties). Without this case the contract
        #     never fires and the answerer silently picks one property.
        unresolved_multi_property = bool(
            len(book_properties) > 1
            and (not active_property_ids or len(active_property_ids) >= 2)
        )
        past_property_context, _discussed_clean = self._reconcile_discussed_property_state(
            context=context,
            available_properties=_history_available_properties,
            active_property_ids=active_property_ids,
            past_property_context=past_property_context,
            memory_summary=(memory_snapshot or {}).get("summary") or "",
        )
        _direct_recall_reply = self._direct_discussed_hotel_recall_reply(
            user_message=user_message,
            discussed_properties=_discussed_clean,
        )
        if _direct_recall_reply:
            return _direct_recall_reply, [], []
        _now_book = datetime.now(UTC)
        # ── THE GUEST'S OWN REGISTERED DETAILS ───────────────────────────────
        # Asked "what's my phone number", the router sent the turn here and this
        # lane held neither the number nor any rule about it, so it answered — quite
        # correctly, from what it had — "I don't have your phone number listed",
        # while the profile had held it the whole time. These are the GUEST's own
        # details only; the hotel's own numbers stay in the KB where they belong.
        _mf_book = (memory_snapshot or {}).get("facts") or {}
        _pd_book = context.pending_data if isinstance(
            getattr(context, "pending_data", None), dict) else {}
        _guest_profile = {
            _k: str(_v).strip()
            for _k, _v in {
                "guest_name": _pd_book.get("guest_name") or _mf_book.get("guest_name")
                or getattr(context, "booking_guest_name", "") or "",
                "guest_phone": _pd_book.get("guest_phone") or _mf_book.get("guest_phone") or "",
                "guest_email": _pd_book.get("guest_email") or _mf_book.get("guest_email") or "",
                "room_number": _pd_book.get("room_number") or _mf_book.get("room_number") or "",
            }.items()
            if str(_v or "").strip()
        }

        _turn_response_language = self._response_language_preference(
            user_message=str(
                getattr(context, "_turn_guest_language_message", "") or user_message or ""
            ),
            history_rows=compact_history,
            last_assistant_message=last_assistant_compact,
            router_language=getattr(context, "_turn_response_language", None),
        )
        _previous_response_language = self._last_assistant_response_language(context)
        _language_switch = bool(
            _previous_response_language
            and _turn_response_language
            and str(_previous_response_language.get("language") or "").strip().casefold()
            != str(_turn_response_language.get("language") or "").strip().casefold()
        )
        (
            _answer_history,
            _answer_last_assistant,
            _language_switch_history_suppressed,
        ) = self._history_for_explicit_language_switch(
            history_rows=compact_history,
            last_assistant_message=last_assistant_compact,
            current_user_message=str(user_message or ""),
            language_switch=_language_switch,
        )

        payload = {
            "user_message": str(user_message or "").strip(),
            "current_date": _now_book.strftime("%Y-%m-%d"),
            "guest_profile": self._sanitize_json(_guest_profile),
            "faq_candidates": self._sanitize_json(faq_candidates),
            "memory_summary": self._summary_within_budget((memory_snapshot or {}).get("summary"), summary_char_limit),
            "history_last_10": _answer_history,
            "response_language": self._sanitize_json(_turn_response_language),
            # Compatibility alias for older DB prompt rows.
            "response_language_preference": self._sanitize_json(_turn_response_language),
            "previous_response_language": self._sanitize_json(_previous_response_language),
            "language_switch": _language_switch,
            "language_switch_history_suppressed": _language_switch_history_suppressed,
            "last_user_message": last_user_compact,
            "last_assistant_message": _answer_last_assistant,
            "selected_phase": {"id": selected_phase_id, "name": selected_phase_name},
            "services_available_now": self._sanitize_json(in_phase_services or []),
            "services_unavailable_now": self._sanitize_json(out_of_phase_services or []),
            "active_property_ids": active_property_ids,
            "book_properties": self._sanitize_json(book_properties),
            "past_property_context": self._sanitize_json(past_property_context),
            # Every hotel discussed this conversation, oldest FIRST → most-recent
            # LAST. Lets the answerer resolve 'the previous/first/last hotel' and
            # 'summarize everything we discussed' WITHOUT changing the active
            # property (the current hotel stays in active_property_ids).
            "discussed_properties_in_order": self._sanitize_json(_discussed_clean),
            # Per-hotel stay dates the guest gave, kept separately so a later hotel's
            # dates never overwrite an earlier one's. Lets the answerer answer
            # "what dates did I pick for <hotel>" exactly, even after the transcript
            # scrolls out of the window on a long conversation.
            "stay_dates_by_property": self._sanitize_json(
                ((memory_snapshot or {}).get("facts") or {}).get("stay_dates_by_property") or {}
            ),
            # Full brand footprint (every city + its hotels) for COVERAGE questions
            # like 'what cities do you have', so the answer is complete even when
            # only one property is active / loaded.
            "all_locations": self._sanitize_json(list((pending or {}).get("_all_locations_compact") or [])),
        }

        # --- WEATHER INJECTION FOR BOOK ANSWERER ---
        # Weather was fetched upstream once per turn using the router's
        # `weather_for` flag. Just read the stashed result here.
        _turn_wx = (
            context.pending_data.get("_turn_live_weather")
            if isinstance(context.pending_data, dict)
            else None
        )
        if isinstance(_turn_wx, dict) and _turn_wx:
            payload["live_weather"] = _turn_wx
        # -------------------------

        # Multi-model: answering runs on 5.4 and loads 5.4's book-answerer prompt.
        model = self._role_model("book_answerer")
        try:
            system_prompt = await prompt_registry.get("orchestrator.book_answerer", model=model)
        except PromptMissingError:
            logger.exception("orchestrator_book_answerer_prompt_missing")
            return "", [], [], ""
        # Phase-aware answerer directives: the pre-arrival framing below (Wi-Fi
        # "shared once checked in", "hotel information and room booking only") is
        # correct ONLY before arrival. It must NOT leak into during_stay /
        # post_checkout, where the guest is already checked in. Pre-arrival branch
        # is kept byte-identical to the previous behavior.
        _ba_is_prearrival = self._normalize_phase_identifier(selected_phase_id) in {
            "pre_booking",
            "pre_checkin",
        }
        _ba_wifi_directive = (
            "5) IN-STAY CREDENTIALS — Wi-Fi password, room key, room number, door code: a guest gets these AT "
            "CHECK-IN, never before. If asked pre-arrival ('what's the wifi password'), say it's shared once they've "
            "checked in. Do NOT claim you simply 'don't have it', and do NOT offer a staff callback to send a "
            "password — it is a check-in credential, not a conversion blocker.\n"
        ) if _ba_is_prearrival else (
            "5) IN-STAY CREDENTIALS — The guest is CHECKED IN, so the Wi-Fi password / room key / door code are "
            "already available to them. If they ask for the Wi-Fi password, share it if the KB provides it; otherwise "
            "tell them it's on their in-room welcome card or available from the front desk. NEVER say it's 'shared "
            "once you've checked in' (they already have), and never decline it as a pre-arrival matter.\n"
        )
        _ba_domain_directive = (
            "\n=== STAY IN HOTEL DOMAIN ===\n"
            "If the guest asks for something unrelated to hotel discovery, hotel information, policies, property contact, "
            "or room booking — such as maths, jokes, buying a car, or other generic chat — do NOT answer that unrelated "
            "task. Reply in one short line that you can help with hotel information and room booking only, then stop.\n"
        ) if _ba_is_prearrival else (
            "\n=== STAY IN HOTEL DOMAIN ===\n"
            "If the guest asks for something unrelated to the hotel or their stay — maths, jokes, world affairs, buying "
            "a car, sports scores, external apps — do NOT answer that unrelated task. In ONE short, warm line, steer "
            "back to how you can help with their stay (their room, dining, services, requests), varying the wording "
            "naturally, then stop. Do NOT say 'hotel information and room booking only' during the stay.\n"
        )
        system_prompt = (
            str(system_prompt or "").rstrip()
            + self._build_guest_point_style_contract()
            + (
                "\n\n=== PRE-ARRIVAL SCOPE (in-stay actions cannot happen yet) ===\n"
                "Check selected_phase. When the guest has NOT yet arrived / checked in (a pre-booking / pre-arrival / "
                "pre-check-in stage), anything that can ONLY happen once they are physically staying is not possible "
                "now — housekeeping (cleaning, towels, room fixes), in-room maintenance (AC, plumbing, TV), room "
                "service / in-room dining delivery, spa appointments, restaurant table reservations for the stay, AND a "
                "CURRENT-PROBLEM report about an in-room thing where the guest speaks as if ALREADY in a room ('I have "
                "an issue with my bed', 'my bed is broken', 'my room is dirty', 'the AC isn't working in my room'). The "
                "guest has no room yet, so these cannot be real. For ANY of these: warmly DECLINE — say "
                "it is something the team takes care of during their stay / once they have checked in — and do NOT "
                "offer a staff callback, do NOT say 'would you like our team to reach out', do NOT suggest connecting "
                "them to anyone. Then pivot to booking or a pre-arrival question. This does NOT apply to booking a "
                "room/stay, event/venue/banquet bookings, general information questions (e.g. 'what spa services do "
                "you have'), a plain question ABOUT a room feature ('what kind of bed do rooms have', 'is the bed "
                "king-size', 'do rooms have a bathtub') which you ANSWER from the KB, help with an existing booking, or "
                "an explicit request to talk to a human — those proceed normally. Judge the request; never use a fixed "
                "word list. A request to BOOK A ROOM/SUITE (including a named room type — 'book the suite', 'id like to "
                "book the suite', 'i want to book the suite', 'book the Deluxe') is a ROOM BOOKING and proceeds normally; "
                "NEVER decline it as an in-stay action, even when the immediately prior turn declined a real in-stay "
                "request (water, towels, housekeeping). A prior in-stay decline must NOT carry over onto a room booking. "
                "Never mention internal phase names.\n"
                "\n=== SMART PHASE DECLINE — KB-GATED, AND PHRASED FOR WHEN THE SERVICE STARTS (overrides any fixed decline line below) ===\n"
                "Before declining ANY request as a phase/timing matter, run these checks IN ORDER. Never emit a fixed, "
                "one-size-fits-all sentence — judge each request:\n"
                "1) IS IT AN INFO QUESTION OR AN EXECUTION ASK? First decide whether the guest is ASKING ABOUT a service "
                "or asking you to DO / ARRANGE it. 'do you provide X', 'do you have X', 'is there X', 'what X do you offer', "
                "'tell me about X' = INFORMATION. ANSWER it from the KB (does it exist, what it is). NEVER answer an info "
                "question with a phase-decline line. The decline is ONLY for a real execution attempt ('book X', 'order "
                "X', 'arrange X', 'send X').\n"
                "   1b) INFO ABOUT AN IN-STAY-ONLY SERVICE BEFORE ARRIVAL — CONFIRM IT EXISTS, BUT ADD A TIMING NOTE: when "
                "the info question is about a service that can only be USED once the guest is checked in (room service / "
                "ordering food to the room, spa, housekeeping, laundry, in-room amenities) AND the phase is pre_booking or "
                "pre_checkin (they have NOT checked in), confirm the hotel OFFERS it but add a short note that it becomes "
                "available once they've checked in — so you NEVER imply they can use it right now. This is NOT a decline (you "
                "ARE answering the info); it is honest timing. Example: for 'can I order food to my room?' answer 'Yes, the "
                "hotel offers 24-hour room service — you'll be able to order to your room once you've checked in.', never a "
                "flat 'Yes, you can order food to your room' that reads as usable now. Do not open a form or raise a request for it.\n"
                "2) FOR A GENUINE EXECUTION ASK that belongs during the stay, decline on the PRE-BOOKING BOUNDARY FIRST. "
                "Do not turn the decline into a claim like 'we do not offer biryani room service' or 'there is no towel service' "
                "unless the guest asked as an information question. For during-stay execution, the key point is that this chat "
                "does not handle it.\n"
                "3) Once you know it is a genuine execution attempt that cannot transact in the pre-arrival phase, decline "
                "with wording that matches WHEN the hotel can first deliver it:\n"
                "   - IN-STAY services (consumed on-site by a checked-in guest): spa / salon / massage, in-room dining / "
                "room service / food or drink 'to my room', restaurant table, gym / pool, housekeeping / laundry / towels "
                "/ maintenance / AC fixes, 'send X to my room'. These start AFTER CHECK-IN → decline with 'once you're "
                "checked in' (no staff callback).\n"
                "   - PRE-ARRIVAL services (staff set them up before/at arrival, NOT tied to being physically present): "
                "airport pickup / airport transfer / shuttle / hotel-arranged cab, early check-in or late check-out "
                "requests, special-occasion or accessibility arrangements. These become available ONCE THE BOOKING IS "
                "CONFIRMED → say so in those words (e.g. \"Airport pickup is something the team arranges once your booking "
                "is confirmed.\"); do NOT say 'once you're checked in' for these. You MAY then offer to have the team help "
                "arrange it, since staff can coordinate it from anywhere.\n"
                "   Deciding question: when can the hotel FIRST deliver this? Only after the guest is physically in a "
                "room → IN-STAY wording. Set up before/at arrival → PRE-ARRIVAL wording. Judge it; never rely on a fixed "
                "word list.\n"
                "4) ONLY AFTER deciding it is an INFORMATION question may you check whether the service is offered. Look in "
                "book_knowledge_base, services_available_now, and services_unavailable_now. If the service is not mentioned "
                "anywhere, say so plainly. Do NOT use the absence check to answer an execution request that should simply be "
                "declined on the pre-booking boundary.\n"
                + _ba_wifi_directive +
                "\n=== COVER EVERY ACTIVE PROPERTY (multi-property answers) ===\n"
                "When active_property_ids / book_properties contains 2+ properties and the guest's question is about the "
                "location or the hotels generally ('tell me about the hotels here', 'what makes each special', 'what do "
                "you have in <city>'), you MUST cover EVERY in-scope property — name each one and give whatever facts the "
                "KB has for it. Never silently describe only one and reduce the other(s) to 'the other property'. If one "
                "property has thin KB, still NAME it and share what little is there. Only focus on a single property once "
                "the guest has narrowed to it.\n"
                "\n=== CONTINUE THE EXACT OFFERED BRANCH ===\n"
                "If the immediately previous assistant turn ended by offering ONE specific next branch — for example a "
                "'best for' summary, a 'veg dinner option', a 'late-night option', or a similar narrow continuation — and "
                "the guest replies with a clear acceptance like 'yes', 'yes please', 'give it', or equivalent, answer THAT "
                "exact offered branch. Do NOT simply restate the broader source information you already gave.\n"
                "\n=== NO WEAK RECOMMENDATIONS ===\n"
                "Do NOT turn thin facts into rankings or recommendations. If the KB only gives generic facts like outlet "
                "name, cuisine, timings, or a broad dietary flag, that is NOT enough to claim 'best', 'strongest choice', "
                "'ideal', or similar. Present those options neutrally unless the KB clearly supports a comparison.\n"
                "\n=== NO SPECULATIVE FOLLOW-UP OFFERS ===\n"
                "Do not keep ending replies with extra optional branches unless the next step is clearly grounded and "
                "meaningfully different from the answer you just gave. If the suggested follow-up would only repeat the same "
                "facts in slightly different words, do not offer it.\n"
                + _ba_domain_directive +
                "\n=== BOOKING-WEBSITE / BOOKING-FLOW FAILURE ===\n"
                "If the guest says the website, booking page, or payment flow is crashing / failing / not loading, acknowledge "
                "the frustration briefly and move to HUMAN HELP, not a broad fallback. Offer the hotel/team contact or say you "
                "can connect them with the team if that path is supported. Do NOT promise rates, availability, route guidance, "
                "or that you can complete the booking from this chat unless the KB or payload explicitly supports it.\n"
            )
        )

        # `model` was resolved above (self._role_model("book_answerer")).
        book_answerer_max_tokens = max(
            240,
            int(getattr(settings, "chat_llm_book_answerer_max_tokens", 700) or 700),
        )
        # The GAP FOLLOW-UP rule tells the answerer to hand the guest a phone
        # number when it can't answer. But the router only opens the books the
        # question seems to need, so on a policy/amenity gap the contact book is
        # absent and there is NO number in context — the rule would have nothing
        # to offer and would silently fall back to the handoff alone. Append one
        # compact contact line per in-scope property. Skipped when the contact
        # book is already open, so nothing is duplicated.
        if kb_content and book_properties and "__contact_information" not in kb_content:
            try:
                from services.kb_table_reader import fetch_pages_by_slug_db

                _names = {
                    str(p.get("id") or "").strip(): str(p.get("name") or p.get("id") or "")
                    for p in book_properties
                    if str(p.get("id") or "").strip()
                }
                _contact_pages = await fetch_pages_by_slug_db(
                    hotel_code=str(getattr(context, "hotel_code", "") or ""),
                    page_slugs=[f"canon__{s}__contact_information" for s in _names],
                    property_slugs=list(_names),
                )
                _contact_lines: list[str] = []
                for _page in _contact_pages:
                    _slug = str(_page.get("id") or "")
                    _prop = _slug[len("canon__"):-len("__contact_information")]
                    _text = str(_page.get("text") or "").strip()
                    if _text:
                        _contact_lines.append(f"{_names.get(_prop, _prop)}\n{_text}")
                if _contact_lines:
                    kb_content = (
                        kb_content
                        + "\n\n=== PROPERTY CONTACT (for the GAP FOLLOW-UP rule) ===\n"
                        "Use these ONLY when the guest asked for contact details, or when you "
                        "cannot answer and are telling them who to call. Never volunteer them "
                        "alongside an answer you already gave, and never use one property's "
                        "number for another.\n"
                        + "\n\n".join(_contact_lines)
                    )
            except Exception as _contact_exc:
                logger.warning("book answerer contact block skipped: %r", _contact_exc)

        # Place the per-property KB content in its own stable system message so
        # OpenAI's automatic prompt cache can hit on turn 2+ within the same
        # property session. The cache prefix is [system_prompt] + [kb_block];
        # the per-turn JSON (user message, history, faq) sits after and varies.
        kb_block = (
            "BOOK KNOWLEDGE BASE (use as `book_knowledge_base` referenced in instructions):\n\n"
            + kb_content
        ) if kb_content else ""
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
        ]
        # Booking context (stay dates, property, room) so the answerer can state
        # the guest's OWN check-in/check-out date when they ask ("when's my check-in").
        _ba_booking_block = self._build_booking_context_prompt_block(context)
        if _ba_booking_block:
            messages.append({"role": "system", "content": _ba_booking_block})
        if unresolved_multi_property:
            multi_property_contract = _build_multi_property_answerer_contract(
                book_properties
            )
            if multi_property_contract:
                messages.append({"role": "system", "content": multi_property_contract})
        if kb_block:
            messages.append({"role": "system", "content": kb_block})
        messages.append(
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}
        )
        # Scope rule: placed AFTER the user turn (recency) so off-topic asks — maths,
        # general knowledge, jokes — are declined instead of answered, and a self-harm
        # message gets care rather than a hotel handoff. Uses the SHORT variant: the
        # long guardrail measured identically (maths/knowledge fixed, joke still fails)
        # while costing ~4k chars on every turn. Must stay BEFORE the language rule.
        messages.append(
            {"role": "system", "content": self._hotel_scope_only_directive()}
        )
        # And when the router has ALREADY judged this one off-topic, say so
        # outright rather than leaving the answerer to reach the same conclusion
        # on its own — which, for jokes, it reliably does not.
        if bool(getattr(context, "_router_off_topic", False)):
            messages.append(
                {"role": "system", "content": self._router_says_off_topic_directive()}
            )
        # THE GUEST'S OWN CONTACT DETAILS ARE NOT THE HOTEL'S. This directive was
        # written for exactly this failure and then never attached to anything —
        # dead code in every lane. Hooked up here, next to the profile now in the
        # payload, so "what's my phone number" is answered from the guest's own
        # record and a reservations@ address from the KB is never handed back as
        # theirs. Only sent when we actually hold something of theirs.
        if _guest_profile:
            messages.append(
                {"role": "system", "content": self._own_contact_grounding_directive()}
            )
        # SAROVAR-ONLY answer-grounding gate (the topic-match gate): rephrase only a
        # fact about the EXACT subject asked; never substitute a nearby/different fact;
        # UNKNOWN != No. Placed after the scope rule and before the language rule so it
        # wins for grounding by recency while language stays the very last message. Gated
        # on hotel so no other tenant's answerer behaviour changes.
        if str(getattr(context, "hotel_code", "") or "").strip().lower() == "sarovar_portico_sriperumbudur":
            messages.append(
                {"role": "system", "content": self._sarovar_answer_grounding_gate_directive()}
            )
        # ZONE-ONLY: no "Let me know if you'd like..." sign-off. Placed here, second
        # from last, for the same reason the language rule is last — recency. The
        # equivalent rule inside the 103k prompt row was measured not to hold.
        # Admin-managed hotels switch the same rule on from the Pre-booking tab,
        # worded by their DB row.
        _no_offer_rule = await self._reply_style_directive(
            context, "no_trailing_offer", legacy_hotel="zone",
            legacy_text=self._no_trailing_offer_directive,
        )
        if _no_offer_rule:
            messages.append({"role": "system", "content": _no_offer_rule})
        # ── THE SCOPE + SAFETY GUARDRAIL, WHICH WAS NEVER ACTUALLY SENT ──────
        # `_out_of_scope_guardrail_directive()` has existed for a long time and
        # was referenced only by a comment - defined, never injected. That is why
        # jokes, maths and "capital of france" sailed through, and why a request
        # for a knife could raise "Deliver a knife to the guest in room 1400" one
        # turn and be refused the next: nothing in the system had an opinion, so
        # the base model improvised a different one each time. Placed second-last,
        # immediately before the language rule, which must stay the very last.
        messages.append(
            {"role": "system",
             "content": self._out_of_scope_guardrail_directive(
                 pre_arrival=str(selected_phase_id or "").strip().lower()
                 .replace(" ", "_").replace("-", "_")
                 in {"pre_booking", "pre_checkin"})}
        )
        # MANGO-ONLY: the guardrail above tells the model to OFFER to pass a cab or
        # similar request to staff, and it wins by recency over Mango's own prompt,
        # which says to give the hotel's number instead (Mango's tickets reach no
        # one yet). Placed right after it so Mango's rule is the one that holds.
        # Admin-managed hotels switch it on from the Pre-booking tab, worded by
        # their DB row.
        _give_number_rule = await self._reply_style_directive(
            context, "give_number_when_cannot_arrange", legacy_hotel="mango_grove_chandigarh",
            legacy_text=self._mango_cannot_arrange_directive,
        )
        if _give_number_rule:
            messages.append({"role": "system", "content": _give_number_rule})
        # Mango only (its KB names no vegetarian dish), right after its
        # cannot-arrange rule as before. Not tied to the Pre-booking switch above,
        # so an admin-managed hotel never picks up Mango's veg rule.
        if str(getattr(context, "hotel_code", "") or "").strip().lower() == "mango_grove_chandigarh":
            messages.append(
                {"role": "system", "content": self._mango_veg_asks_directive()}
            )
        _emoji_rule = await self._admin_emoji_directive(context)
        if _emoji_rule:
            messages.append({"role": "system", "content": _emoji_rule})
        # Language rule MUST stay the VERY LAST message (after the user turn + all other
        # directives). Recency makes it win reliably so the bot mirrors the guest's language.
        messages.append(
            {"role": "system", "content": self._reply_language_directive()}
        )
        # Kick off the parallel chip generator BEFORE we await the answerer.
        # Chip generation is much smaller (~140 max tokens, ~5k input) than the
        # answerer (~700 max tokens, ~30k input), so it almost always finishes
        # first and the answerer's wall-clock time dominates. If chips happen
        # to take longer (rare), the caller skips them rather than holding the
        # response — the frontend's chip-upgrade endpoint can fill them later.
        chips_task: asyncio.Task | None = None
        if wait_for_chips:
            chips_task = asyncio.create_task(
                self._run_quick_chips_llm(
                    user_message=user_message,
                    context=context,
                    selected_phase_id=selected_phase_id,
                    selected_phase_name=selected_phase_name,
                    book_properties=book_properties,
                    active_property_ids=active_property_ids,
                    in_phase_services=in_phase_services,
                    out_of_phase_services=out_of_phase_services,
                    book_context_text=kb_content,
                )
            )
        try:
            answer = await llm_client.chat(
                messages=messages,
                model=model,
                temperature=0.0 if _language_switch else 0.2,
                max_tokens=book_answerer_max_tokens,
                trace_context={
                    "responder_type": "main",
                    "agent": "book_answerer",
                    "session_id": str(getattr(context, "session_id", "")),
                    "selected_phase_id": selected_phase_id,
                },
            )
        except Exception:
            if chips_task is not None:
                chips_task.cancel()
            return "", [], [], ""

        # Defensive: strip a stray `IMAGES: ...` line if a model ever emits one,
        # so it can never leak into the visible reply. We do NOT ask for it —
        # image topics come from the books the router already opened, which
        # costs no extra tokens and no extra latency.
        _pre_cleaned, _unused_image_topic = self._split_image_topic(
            str(answer or "").strip()
        )
        cleaned, _ignored_inline_chips, focus_ids = (
            self._split_answer_chips_and_focus(_pre_cleaned)
        )

        # Default focus to ALL known book_properties when the LLM emits no
        # explicit IDs (empty, "all", "none", "-"). Without this, the
        # `_active_property_ids` slot stays empty across turns, so the next
        # turn's KB scoping has no anchor and the bot can drift to a
        # different property mid-conversation. Defaulting to "all" preserves
        # the current open-set context — the LLM still narrows when the
        # guest names a single property on a later turn.
        if not focus_ids and isinstance(book_properties, list) and book_properties:
            focus_ids = [
                str(bp.get("id") or "").strip()
                for bp in book_properties
                if isinstance(bp, dict) and str(bp.get("id") or "").strip()
            ]

        # Ship the answer the moment it's ready. Chips are a nice-to-have, NOT
        # part of the user-perceived response wall-clock. If the chips_task has
        # already finished by the time we get here, take its result for free;
        # otherwise let it keep running in the background and let the frontend's
        # chip-upgrade endpoint (`/api/chat/suggestions`) deliver chips on a
        # second hop. We do not wait, even briefly — every ms of grace adds up
        # across turns and the user explicitly asked us to never block on chips.
        #
        # EXCEPTION: when called from the chip-speculation path (wait_for_chips=
        # True), latency doesn't matter (we're already running in the background)
        # and we must reliably store chips alongside the cached decision so the
        # next click level of the cascade has chips to speculate on. In that
        # mode we await the chips_task with a generous bound.
        chips: list[str] = []
        if wait_for_chips and chips_task is not None:
            # 3s ceiling (was 10s). Speculations are background work — they
            # should never tie up an OpenAI connection for long. If chips
            # haven't returned in 3s, drop them; the cached decision still
            # ships, just without chips for the next-level cascade.
            try:
                chip_result = await asyncio.wait_for(
                    asyncio.shield(chips_task), timeout=3.0
                )
                if isinstance(chip_result, list):
                    chips = list(chip_result)
            except (asyncio.TimeoutError, Exception):
                if not chips_task.done():
                    chips_task.cancel()
                chips = []
        elif chips_task is not None:
            if chips_task.done():
                try:
                    chip_result = chips_task.result()
                    if isinstance(chip_result, list):
                        chips = list(chip_result)
                except Exception:
                    chips = []
            # else: leave chips_task running in the background.
        # SAFETY NET for verified nearby results.
        #
        # Google only runs on a genuine nearby ask (the router must have opened
        # Location & Transport, and the classifier must say the KB does not
        # cover it), and everything it returns is inside the 100 km fence. So
        # once results exist there is nothing left to judge — the guest asked,
        # we found real places, they should see them.
        #
        # The answerer disagreed about a third of the time. Measured over 30
        # conversations: medicines 2/3, party 2/3, cafes 2/3, open-now 2/3 —
        # each failure a reply saying it did not have the information while
        # holding four verified places. Two rounds of firmer wording moved it
        # barely at all, because one instruction inside a 103k-char prompt
        # cannot reliably beat the caution rules around it.
        #
        # So the wording stays (a natural reply is nicer when it works) and this
        # only fires when the reply mentions NONE of the places found.
        # A raw dump used to be substituted here when the answerer ignored the
        # verified places. It was removed: with no filtering and no judgement it
        # published Google's rows verbatim — phone numbers, street addresses,
        # ratings, and on one turn a Hilton in California. Writing the answer is
        # the bot's job; Google's job is to supply the facts. The block itself
        # now tells the bot that nearby places are in scope and to pick the ones
        # that fit, which is the right place for that instruction.
        if _live_fact and "NEARBY PLACES" in _live_fact:
            found = re.findall(r"^-\s+([^|\n]+?)\s*\|", _live_fact, flags=re.M)
            named = [n.strip() for n in found if len(n.strip()) > 2]
            if named and not any(n.lower() in (cleaned or "").lower() for n in named):
                logger.warning(
                    "[AREA] answerer named none of the %d verified places it was given",
                    len(named),
                )

        _book_labels = _extract_book_labels_from_context(book_context_text)
        # Record against the session so the image step can read it directly.
        # Passing it through decision/response metadata does not work: that
        # payload is filtered before it reaches the image attach step.
        try:
            from services.image_tool_service import image_tool_service as _img_svc

            _img_svc.note_books(
                str(getattr(context, "session_id", "") or "").strip(), _book_labels
            )
        except Exception:
            pass
        return cleaned, list(chips), list(focus_ids or []), _book_labels

    def _persist_book_answerer_focus(
        self,
        *,
        context: ConversationContext,
        focus_ids: list[str],
        selected_phase_id: str,
        selected_property_name: str = "",
    ) -> None:
        """Mirror the heavy-orchestrator's property-scope persistence for the
        book_answerer / auto short-circuits, so the FOCUS line the LLM emitted
        actually narrows the next turn's KB load.

        - Empty focus_ids → leave existing scope untouched (keeps prior turn's focus).
        - Non-empty focus_ids → set as `_active_property_ids`, move dropped ids into
          `_past_property_context`, and refresh the legacy single-id mirrors used by
          downstream code (`_selected_property_scope_id`, `_selected_property_scope_ids`).
        - Stay-property pin (during_stay etc.) is reapplied so the guest's actual
          stay property is never silently dropped from focus.
        """
        if not isinstance(focus_ids, list) or not focus_ids:
            return
        if not isinstance(context.pending_data, dict):
            context.pending_data = {}

        normalized_focus = self._normalize_property_id_list(
            [str(pid).strip() for pid in focus_ids if str(pid).strip()]
        )
        if not normalized_focus:
            return

        normalized_focus, _ = self._apply_stay_property_pin(
            context=context,
            selected_phase_id=selected_phase_id,
            property_ids=normalized_focus,
            source="post_book_answerer",
        )
        if not normalized_focus:
            return

        prev_active = self._normalize_property_id_list(
            list(context.pending_data.get("_active_property_ids") or [])
        )
        prev_active_names = list(context.pending_data.get("_active_property_names") or [])
        past_ctx = dict(context.pending_data.get("_past_property_context") or {})

        # Same fleet-scope guard as the orchestrator path (see _apply_main_router_*
        # move-to-past): a fleet-attribute / broaden turn made ~the whole catalog
        # active for ONE turn. When the FOCUS line narrows back to a few, do NOT
        # roll all of those into "previously discussed" — the guest engaged with
        # none of them individually. 6+ = a broaden, not genuine interest.
        _prev_was_fleet_scope = len(prev_active) >= 6
        for old_id in ([] if _prev_was_fleet_scope else prev_active):
            if not old_id or old_id in normalized_focus or old_id in past_ctx:
                continue
            old_idx = prev_active.index(old_id) if old_id in prev_active else -1
            fallback_old_name = (
                prev_active_names[old_idx] if 0 <= old_idx < len(prev_active_names) else old_id
            )
            old_name = self._resolve_property_name_from_pending(
                pending_data=context.pending_data,
                property_id=old_id,
                fallback=fallback_old_name,
            )
            past_ctx[old_id] = f"Previously discussed: {old_name}"
        if len(past_ctx) > 10:
            past_ctx = dict(list(past_ctx.items())[-10:])

        context.pending_data["_past_property_context"] = past_ctx
        self._sync_property_scope_pending(
            pending_data=context.pending_data,
            property_ids=normalized_focus,
            active_property_names=list(context.pending_data.get("_active_property_names") or []),
            selected_property_name=selected_property_name,
        )

    @staticmethod
    def _book_response_looks_complete(decision: OrchestrationDecision) -> bool:
        """Structural check: does the response look like a finished, well-formed
        book-route answer that doesn't need the answer-first guard's review.

        Pure shape — no keyword matching, no content analysis. Reads only:
        the LLM's own output fields (`answered_current_query`, `action`,
        `pending_action`), response length, and bullet count.
        """
        text = str(getattr(decision, "response_text", "") or "").strip()
        if len(text) < 80:
            return False
        bullet_count = sum(
            1 for line in text.splitlines() if line.lstrip().startswith("- ")
        )
        if bullet_count < 2:
            return False
        action = str(getattr(decision, "action", "") or "").strip().lower()
        if action and action != "respond_only":
            return False
        pending_action = str(getattr(decision, "pending_action", "") or "").strip()
        if pending_action:
            return False
        # The model's own self-assessment — if the answerer flagged the reply
        # as not addressing the question, defer to the guard for review.
        answered = getattr(decision, "answered_current_query", True)
        if answered is False:
            return False
        return True

    @staticmethod
    def _is_sarovar_ird(context: "ConversationContext", service_id: str) -> bool:
        return (
            str(service_id or "").strip().lower() == "in_room_dining"
            and str(getattr(context, "hotel_code", "") or "").strip().lower()
            == "sarovar_portico_sriperumbudur"
        )

    def _ird_guest_already_saw_the_order(
        self, context: "ConversationContext", service_id: str
    ) -> bool:
        """True when a VALIDATED in-room-dining order is already on the table.

        The premature-ticket gate assumes that if the conversation was never in a
        `confirm_*` pending state then the guest cannot have seen a summary, so a
        `create_ticket` must be a model getting ahead of itself. In-room dining does
        not work that way: its checkout summary is driven by the validated cart, and
        the model routinely presents that summary on a `respond_only` turn while
        leaving `pending_action` empty. The gate then ate the guest's first "yes",
        blanked `ready_to_create`, re-rendered the very same question and raised no
        ticket - the double-confirm loop, with the food never reaching the kitchen.

        A non-empty `_ird_cart` is the server's OWN validated record that the order
        was built, priced and shown, so the gate's premise simply does not hold. It
        deliberately says nothing about whether the guest agreed - that judgement
        stays with the model's `ready_to_create`, which this only stops the gate from
        overwriting.
        """
        if not self._is_sarovar_ird(context, service_id):
            return False
        pending = context.pending_data if isinstance(context.pending_data, dict) else {}
        cart = pending.get("_ird_cart") or []
        # Called from every gate that can drop a placement, at the exact moment the
        # order is either placed or silently lost. Logged either way, because the
        # failure mode is invisible from the outside: the guest is handed the same
        # summary again and nothing says why.
        logger.info(
            "[IRD_CONFIRM_GATE] validated cart holds %d line(s) -> %s",
            len(cart),
            "the guest HAS seen this order, so a ready_to_create is their "
            "confirmation landing" if cart else
            "NO CART, so this is not treated as a confirmation and the order "
            "will not be placed this turn",
        )
        return bool(cart)

    def _ird_menu_word_set(self, menu_text: str) -> set[str]:
        """Lowercased significant words that appear anywhere in the IRD menu KB.
        Used only to REJECT an ordered item when NONE of its words appear on the
        menu at all (a fully-invented item). Conservative by design: any word
        overlap keeps the item, so real items are never dropped by a spelling/variant
        mismatch. `menu_text` MUST be the same runtime-resolved, property-scoped menu
        the agent sees (the `extracted_knowledge` local), not a trimmed service dict."""
        return {
            w for w in re.findall(r"[a-z]{3,}", str(menu_text or "").lower())
            if w not in self._IRD_ORDER_STOPWORDS
        }

    @staticmethod
    def _ird_dish_availability(context: "ConversationContext", name: str) -> dict[str, Any]:
        """Can this dish be ordered at the guest's local time right now?

        Thin wrapper over the per-dish table so the vetting loop stays readable.
        FAILS OPEN on absolutely everything — no table, no hotel, a bad clock, an
        exception — because a timing feature must never be the reason a guest
        cannot order food.
        """
        try:
            from datetime import datetime as _dt
            from zoneinfo import ZoneInfo as _ZI

            from services.ird_availability import item_status

            # The guest's clock, not the server's — the same one the menu annotation
            # and the availability list are built from, so what the model was shown
            # and what this enforces can never disagree about the hour.
            try:
                _tz = _ZI(str(getattr(settings, "hotel_timezone", "") or "UTC").strip() or "UTC")
            except Exception:
                _tz = None
            return item_status(
                str(getattr(context, "hotel_code", "") or ""), name, _dt.now(_tz)
            )
        except Exception as exc:
            logger.warning(
                "[IRD_AVAIL] availability check skipped for %r (%r) - treating as "
                "available", name, exc,
            )
            return {"available": True, "served": "", "next_opens": "", "matched": ""}

    def _ird_item_is_real(self, name: str, menu_words: set[str]) -> bool:
        """True unless the item is fully off-menu (none of its significant words
        appear anywhere on the menu). Empty/degenerate names are treated as real so
        we never silently drop something ambiguous."""
        sig = [
            w for w in re.findall(r"[a-z]{3,}", str(name or "").lower())
            if w not in self._IRD_ORDER_STOPWORDS
        ]
        if not sig:
            return True
        return any(w in menu_words for w in sig)

    @staticmethod
    def _ird_fmt_price(v: Any) -> str:
        try:
            n = int(round(float(str(v).replace("₹", "").replace(",", "").strip())))
            return f"₹{n:,}"
        except (TypeError, ValueError):
            return ""

    def _render_ird_cart_lines(self, cart: list[dict[str, Any]]) -> str:
        """One line per dish: the DISH NAME carries the emphasis, because that is what
        a guest scans for when checking their order is right. Quantity and price sit
        beside it in plain text. Bold + bullets only — this tenant's replies carry no
        emoji."""
        lines = []
        for l in cart:
            price = self._ird_fmt_price(l.get("price"))
            try:
                qty = max(1, int(l.get("qty") or 1))
            except (TypeError, ValueError):
                qty = 1
            line = f"• **{l.get('item')}**"
            if qty > 1:
                line += f" × {qty}"
            if price:
                line += f" — {price}" + (" each" if qty > 1 else "")
            lines.append(line)
        return "\n".join(lines)

    @staticmethod
    def _ird_display_order_no(pending: dict) -> int:
        """Which order number the guest is looking at right now.

        An order already placed keeps the number it was given in the ledger; a
        cart still being built is the next one. Returns 0 when this conversation
        has only ever had one order, which is the signal not to number it at all —
        "Order 1" in a chat with a single order is noise.
        """
        try:
            pd = pending if isinstance(pending, dict) else {}
            ledger = [r for r in (pd.get("_ird_placed_orders") or []) if isinstance(r, dict)]
            ref = str(pd.get("_ird_order_ticket_ref") or "").strip()
            if ref:
                for r in ledger:
                    if str(r.get("ticket_ref") or "").strip() == ref:
                        return int(r.get("n") or 0)
            return len(ledger) + 1 if ledger else 0
        except Exception:
            return 0

    def _render_ird_summary(
        self,
        cart: list[dict[str, Any]],
        room: str = "",
        already_placed: bool = False,
        order_no: int = 0,
        placing: bool = False,
    ) -> str:
        """Server-authoritative checkout summary built from the validated cart, so what
        the guest sees always matches what is actually ordered.

        `order_no` is only passed once a conversation holds more than one order.
        It heads the summary "Order 2" so the guest can see which of their orders
        this is, and — because the cart holds one order at a time — the total
        below it is that order's total alone. A guest placing a second order was
        previously shown a total that swept in food already on its way.
        """
        total = 0
        have_total = True
        for l in cart:
            try:
                total += int(round(float(str(l.get("price")).replace("₹", "").replace(",", "")))) * max(1, int(l.get("qty") or 1))
            except (TypeError, ValueError):
                have_total = False
        # A CART UNTIL IT IS PLACED, AN ORDER AFTER. Heading an unplaced cart
        # "Your order" tells the guest the kitchen already has it — they stop
        # choosing, and later ask why food that was never sent has not arrived.
        # The word changes at the only moment that changes anything: placement.
        head = (
            (f"**Order {int(order_no)}**" if int(order_no or 0) > 1 else "**Your order**")
            if already_placed else "**Your cart**"
        ) + (f" for room **{room}**" if room else "")
        body = self._render_ird_cart_lines(cart)
        tail = f"\n\n**Total: {self._ird_fmt_price(total)}**" if have_total and total else ""
        allg = [f"• **{l.get('item')}**: {l.get('allergens')}" for l in cart if str(l.get("allergens") or "").strip() and str(l.get("allergens")).lower() not in {"none", "no listed allergens", "none mentioned"}]
        # Allergens get their own headed block rather than a trailing clause — they are
        # the one thing here that can actually harm someone. Only real ones are listed:
        # repeating "None mentioned" under every dish buries the one that matters.
        allergen_block = ("\n\n**Allergens**\n" + "\n".join(allg)) if allg else ""
        # ONE closing question, answerable with "yes". The old compound ask — confirm
        # it's complete AND you're okay with allergens AND shall I place it — cannot be
        # answered by the bare "yes" guests actually send, which is how the bot ended up
        # asking the same thing three turns running.
        # Once the food is with the kitchen there is nothing left to agree to.
        # Asking again reads as though the order never went, and a guest who
        # answers "yes" is confirming something that already happened.
        # THREE STATES, NOT TWO. The middle one is the turn the guest has just
        # agreed and the order is going in: asking again there makes them answer
        # the same question twice, which is exactly what happened to a guest who
        # said "go ahead" and was asked "Shall I place this order for your room
        # now?" while their ticket was being written.
        closing = (
            "Sending this to the kitchen now."
            if placing
            else "This is on its way to your room."
            if already_placed
            else "Shall I place this order?"
        )
        return f"{head}\n{body}{tail}{allergen_block}\n\n{closing}"

    @staticmethod
    def _sarovar_ird_fix_veg_grouping(text: str, menu_text: str = "") -> str:
        """Deterministic safety net for the Sarovar IRD menu render: if the model
        placed a NON-VEG item under a 'Vegetarian' heading, move it under the
        'Non-Vegetarian' heading. gpt-4.1-mini occasionally ignores the STRICT
        VEG/NON-VEG rule and the KB 'Type:' tags on the free-text highlights render.
        On ANY unexpected structure this is a NO-OP — it can never make the text worse
        than what the model produced.

        Whether a dish is non-veg is READ FROM THE MENU'S OWN `Type:` line, not
        guessed from its name. This used to run off a word list ("chicken", "murgh",
        "fish", …), which is both a hardcoded behaviour rule and wrong in both
        directions: a vegetarian dish whose name contains one of those words was
        filed as meat, and a meat dish named after none of them stayed under
        Vegetarian — which is the one mistake a vegetarian guest actually cares
        about. The menu states the answer for every item; nothing here needs to
        infer it. An item the menu does not cover is LEFT WHERE THE MODEL PUT IT."""
        try:
            src = str(text or "")
            if "vegetarian" not in src.lower():
                return text
            import re as _re
            lines = src.split("\n")
            veg_idx = nonveg_idx = -1
            for i, ln in enumerate(lines):
                l = ln.lower()
                if "vegetarian" not in l:
                    continue
                if "non-veg" in l or "non veg" in l or "nonveg" in l:
                    if nonveg_idx < 0:
                        nonveg_idx = i
                elif veg_idx < 0:
                    veg_idx = i
            if veg_idx < 0 or nonveg_idx < 0 or veg_idx >= nonveg_idx:
                return text  # need a Veg section that precedes a Non-Veg section
            # name -> the menu's own Type: for that dish. Empty when there is no
            # menu to read, and then nothing is moved at all.
            types = _ird_menu_item_types(str(menu_text or ""))

            def is_bullet(ln: str) -> bool:
                s = ln.lstrip()
                return s.startswith("•") or s.startswith("-") or s.startswith("*")

            def item_name(ln: str) -> str:
                m = _re.search(r"\*\*(.+?)\*\*", ln)
                if m:
                    return m.group(1).strip().lower()
                s = _re.sub(r"^[\s•\-\*]+", "", ln)
                return _re.split(r"\s*[-–—:]\s*|₹", s)[0].strip().lower()

            def is_nonveg(ln: str) -> bool:
                """True ONLY when the menu itself says this dish is non-veg.
                Unknown means leave it alone — moving a dish we cannot identify is
                exactly how a veg item ends up under the meat heading."""
                nm = item_name(ln)
                if not nm or not types:
                    return False
                kind = types.get(nm)
                if kind is None:
                    # The render may shorten or extend a name ("Filter Coffee" for
                    # "Filter Coffee (South Indian Style)"). Accept an unambiguous
                    # prefix match; never guess between two candidates.
                    hits = [
                        v for k, v in types.items()
                        if k.startswith(nm) or nm.startswith(k)
                    ]
                    if len(set(hits)) != 1:
                        return False
                    kind = hits[0]
                return "non" in kind

            veg_bullet_idxs = [i for i in range(veg_idx + 1, nonveg_idx) if is_bullet(lines[i])]
            nonveg_bullet_idxs: list[int] = []
            for i in range(nonveg_idx + 1, len(lines)):
                if is_bullet(lines[i]):
                    nonveg_bullet_idxs.append(i)
                elif lines[i].strip() == "":
                    if nonveg_bullet_idxs:
                        break
                    continue
                else:
                    break
            misplaced = [i for i in veg_bullet_idxs if is_nonveg(lines[i])]
            if not misplaced:
                return text  # nothing to fix
            existing_nonveg_names = {item_name(lines[i]) for i in nonveg_bullet_idxs}
            additions = [lines[i] for i in misplaced if item_name(lines[i]) not in existing_nonveg_names]
            drop = set(misplaced)
            last_nonveg = nonveg_bullet_idxs[-1] if nonveg_bullet_idxs else nonveg_idx
            out: list[str] = []
            for i, ln in enumerate(lines):
                if i in drop:
                    continue
                out.append(ln)
                if i == last_nonveg and additions:
                    out.extend(additions)
                    additions = []
            result = "\n".join(out)
            return result if result.strip() else text
        except Exception:
            return text

    @staticmethod
    def _ird_sync_pending_updates(context: "ConversationContext", decision: "OrchestrationDecision") -> None:
        """Mirror the durable IRD cart keys into decision.pending_data_updates so they
        survive the end-of-turn pending_data rebuild. Finalisation rebuilds
        context.pending_data from public slots + decision.pending_data_updates + a
        snapshot of internal keys taken earlier — so a direct context.pending_data
        write inside the service agent is NOT reliably carried to the next turn. Routing
        the keys through decision.pending_data_updates (which is always merged in) makes
        the cart / placed-orders persist across topic switches."""
        try:
            if not isinstance(decision.pending_data_updates, dict):
                decision.pending_data_updates = {}
            pd = context.pending_data if isinstance(context.pending_data, dict) else {}
            for k in (
                "_ird_cart",
                "_ird_placed_orders",
                "_ird_declined",
                # Which ticket the placed order became, and when — the pair that
                # decides whether the next dish joins this order or starts a new
                # one. Without mirroring these they are lost between turns and
                # every add would look like a fresh order again.
                "_ird_order_ticket_ref",
                "_ird_order_placed_at",
                "_ird_open_order_items",
                # Which order number the guest is currently being shown, so
                # "Order 2" stays "Order 2" on the next turn instead of resetting.
                "_ird_current_order_no",
                # Orders the guest has explicitly moved on from, and the flag that
                # says the cart was just reset for a fresh one. Both decide how the
                # NEXT placement is written, so both have to survive this turn.
                "_ird_closed_order_refs",
                "_ird_fresh_order",
                # What has already passed the "did the guest really ask for this?"
                # check. Without mirroring it the verdict is lost between turns and
                # every item is re-judged against a window that may no longer hold
                # the message where the guest ordered it.
                "_ird_vetted_items",
            ):
                if k in pd:
                    decision.pending_data_updates[k] = pd[k]
        except Exception:
            pass

    @staticmethod
    def _ird_normalize_order_items(decision: "OrchestrationDecision") -> None:
        """Accept the order the model declared whether it sent a list or a string.

        `order_items` is sometimes returned as real JSON and sometimes as a JSON
        string — the same model, same prompt, different turns. Everything
        downstream tested `isinstance(raw_items, list)`, so on a string turn the
        order silently vanished: no validated cart, which meant the menu check,
        the off-menu drop and the veg/non-veg guard were all skipped, the kitchen
        ticket fell back to whatever the model had written, and the guest's
        confirmation was model prose rather than the real order.

        Nothing here judges the order — it only settles the shape so the checks
        that do judge it always get to run.
        """
        try:
            pdu = decision.pending_data_updates
            if not isinstance(pdu, dict):
                return
            raw = pdu.get("order_items")
            if not isinstance(raw, str) or not raw.strip():
                return
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                parsed = [parsed]
            if isinstance(parsed, list):
                pdu["order_items"] = parsed
                logger.info(
                    "[SAROVAR_IRD] order_items arrived as a string — parsed %d item(s) "
                    "so the menu checks can run", len(parsed),
                )
        except (TypeError, ValueError) as exc:
            logger.warning("[SAROVAR_IRD] could not read declared order_items: %r", exc)

    @staticmethod
    def _ird_order_flow_directive() -> str:
        """How ordering works here, one rule per state of the order.

        This replaced ~7,200 characters of accumulated rules that had grown to
        contradict each other outright: rule 3 said "NEVER SAY AN ORDER IS
        PLACED, CONFIRMED, OR ON ITS WAY" while rule 0 said "say the order is on
        its way", there were two rules numbered 5 and two numbered 6, and half of
        it described payload fields that no longer exist. The bot answered
        differently on identical turns because it was reading instructions that
        disagreed, and every fix for one symptom broke another.

        `order_book` carries the state and every rule hangs off it. Nothing here
        restates what the book already says.
        """
        return (
            "IN-ROOM DINING - HOW ORDERING WORKS HERE:\n"
            "\n"
            "`order_book` is the guest's real order. Each entry has a `status`, "
            "and the status decides what you do:\n"
            "\n"
            "- ongoing = not sent yet. Show it and ask ONE yes/no question that "
            "means 'shall I place this order?'. Do not say it is placed - it is "
            "not, and the system writes that confirmation itself once the guest "
            "agrees.\n"
            "- placed, can_be_changed true = with the kitchen but not started. "
            "The guest can still ADD OR REMOVE ANYTHING. This is UPDATING the "
            "order, not placing it: make the change, say the order is updated and "
            "on its way, and NEVER ask to place or confirm it again. BUT IF THE "
            "GUEST SAYS THEY WANT A NEW OR SEPARATE ORDER, DO THAT INSTEAD - put "
            "the dish in a new order and offer to place it. Never ask them to "
            "choose between joining and separate; they already told you, or the "
            "status decides.\n"
            "- placed with can_be_changed false, or processing = the kitchen has "
            "started. Nothing in it can change. A further dish opens a NEW order, "
            "which you then offer to place.\n"
            "- delivered = finished. Mention it only if asked.\n"
            "\n"
            "FOUR THINGS THAT ARE ALWAYS TRUE:\n"
            "1) A dish the guest asks for is theirs. Put it in the order - never "
            "answer a request for food by refusing. If they ask for something "
            "they already ordered, they want another one; that is a real order, "
            "not a duplicate.\n"
            "2) If you tell the guest a dish was added or removed, you MUST emit "
            "`pending_data_updates.order_items` on that same turn with the "
            "complete list for that one order, AND `order_slot` saying which "
            "order it is - a slot number from order_book, or 'new' for a fresh "
            "order. Without order_items nothing is recorded, no ticket is raised "
            "and no food is made.\n"
            "3) Never ask what you can answer yourself. Whether a dish joins the "
            "existing order or starts a new one is decided by the status above, "
            "not by the guest - do not ask. And never ask 'shall I add X?' when "
            "they just asked for X.\n"
            "4) When they say place it, confirm it, send it - in any words - do "
            "it on that turn. Do not re-list the order, do not offer more dishes, "
            "do not ask again. One guest had to say it three times.\n"
            "\n"
            "LAY IT OUT SO IT CAN BE SCANNED: one dish per line with its price, "
            "then the total, then any real allergens. No paragraphs.\n"
        )

    def _ird_rewrite_order_stores(
        self,
        *,
        context: "ConversationContext",
        ticket_ref: str,
        lines: list[dict[str, Any]],
    ) -> None:
        """Point the placed-order ledger and the durable food profile at whatever
        the kitchen ticket now says.

        THREE separate stores describe one order: the cart the guest is shown, the
        `_ird_placed_orders` ledger that `food_order_status` is built from, and the
        `latest_order` / `order_history` memory facts that recall reads. Only the
        cart was being corrected after a ticket edit. So cancelling one dish off a
        placed order rewrote the kitchen ticket correctly - and the bot then went on
        telling the guest that dish was on its way, on a total ₹500 higher than the
        order the kitchen actually held. Either all three follow the ticket or none
        of them mean anything.
        """
        ref = str(ticket_ref or "").strip()
        if not ref:
            return

        def _num(v: Any) -> "float | None":
            try:
                return float(str(v).replace("₹", "").replace(",", "").strip())
            except (TypeError, ValueError):
                return None

        _line_totals = [
            (_num(l.get("price")) * max(1, int(l.get("qty") or 1)))
            if _num(l.get("price")) is not None else None
            for l in lines
        ]
        total = (
            int(sum(_line_totals))
            if _line_totals and all(t is not None for t in _line_totals) else None
        )

        try:
            pd = context.pending_data if isinstance(context.pending_data, dict) else None
            if pd is not None:
                ledger = [
                    r for r in (pd.get("_ird_placed_orders") or []) if isinstance(r, dict)
                ]
                for row in ledger:
                    if str(row.get("ticket_ref") or "").strip() != ref:
                        continue
                    # Both `item` and `name` are written for the same value: the
                    # existing readers disagree about which key they use.
                    row["items"] = [
                        {
                            "item": str(l.get("item") or "").strip(),
                            "name": str(l.get("item") or "").strip(),
                            "qty": int(l.get("qty") or 1),
                            "price": l.get("price"),
                            "allergens": l.get("allergens"),
                        }
                        for l in lines
                    ]
                    if total is not None:
                        row["total"] = total
                pd["_ird_placed_orders"] = ledger
                pd["_ird_open_order_items"] = [
                    {"item": str(l.get("item") or "").strip(),
                     "qty": int(l.get("qty") or 1)}
                    for l in lines
                ]
        except Exception as exc:
            logger.warning("[TICKET_EDIT] ledger rewrite skipped (non-fatal): %r", exc)

        try:
            from services.conversation_memory_service import conversation_memory_service as _cms

            mem = _cms.ensure_memory(context)
            facts = mem.setdefault("facts", {})
            fh = mem.setdefault("fact_history", [])
            new_items = [
                {
                    "name": str(l.get("item") or "").strip(),
                    "qty": int(l.get("qty") or 1),
                    "price": l.get("price"),
                    "allergens": l.get("allergens"),
                }
                for l in lines
            ]

            def _rewrite(rec: Any) -> Any:
                if not isinstance(rec, dict):
                    return rec
                if str(rec.get("ticket_ref") or "").strip() != ref:
                    return rec
                out = {**rec, "items": new_items}
                if total is not None:
                    out["total"] = total
                return out

            latest = facts.get("latest_order")
            if isinstance(latest, dict) and str(latest.get("ticket_ref") or "").strip() == ref:
                _cms._set_fact(
                    facts=facts, fact_history=fh, key="latest_order",
                    value=_rewrite(latest), source_message="[ird_edited]",
                    change_type="set",
                )
            _cms._set_fact(
                facts=facts, fact_history=fh, key="order_history",
                value=[_rewrite(r) for r in (facts.get("order_history") or [])],
                source_message="[ird_edited]", change_type="set",
            )
            logger.info(
                "[TICKET_EDIT] ledger + food profile now match ticket %s: %s",
                ref, [str(l.get("item")) for l in lines],
            )
        except Exception as exc:
            logger.warning("[TICKET_EDIT] food-profile rewrite skipped (non-fatal): %r", exc)

    @staticmethod
    def _ird_commit_placement(
        *,
        context: "ConversationContext",
        decision: "OrchestrationDecision",
        ticket_ref: str = "",
    ) -> str:
        """Record a food order as placed — called ONLY once its ticket really exists.

        The order-state pass hands the placement over instead of announcing it
        (see `ird_placement` there), because at that point gates further down the
        turn can still stop the ticket. Everything that asserts "this food was
        ordered" therefore happens here, behind a written ticket: the guest's
        confirmation, the durable food profile that later recall and allergy
        warnings read from, and the placed-at stamp the edit window runs off.

        Returns the confirmation to show the guest, or "" when there is nothing to
        commit (the ordinary case for every non-placement turn).
        """
        try:
            placement = (decision.metadata or {}).get("ird_placement")
            if not isinstance(placement, dict) or not placement:
                return ""
            from datetime import datetime as _dt, timezone as _tz

            order_record = (
                placement.get("order_record")
                if isinstance(placement.get("order_record"), dict) else {}
            )
            summary = str(placement.get("summary") or "").strip()
            room = str(placement.get("room") or "").strip()
            # STAMP THE TICKET ON THE RECORD. The durable food profile carried the
            # items and a `status` but no reference, so when the guest later
            # cancelled that order nothing could find the record to mark. Recall
            # then read `latest_order` back — still saying "placed" — and told the
            # guest their cancelled food was on its way, minutes after the ticket
            # went to `cancelled`. Without a ref there is no way to connect the two.
            if order_record and str(ticket_ref or "").strip():
                order_record["ticket_ref"] = str(ticket_ref).strip()

            # Durable food memory — memory facts survive topic switches, unlike raw
            # pending_data keys. This is the guest's food profile: it powers order
            # recall ("what did I order?") and allergen-aware recommendations.
            try:
                from services.conversation_memory_service import conversation_memory_service as _cms

                _mem = _cms.ensure_memory(context)
                _facts = _mem.setdefault("facts", {})
                _fh = _mem.setdefault("fact_history", [])
                _cms._set_fact(
                    facts=_facts, fact_history=_fh, key="latest_order",
                    value=order_record, source_message="[ird_placed]", change_type="set",
                )
                _hist = list(_facts.get("order_history") or [])
                _hist.append(order_record)
                _cms._set_fact(
                    facts=_facts, fact_history=_fh, key="order_history",
                    value=_hist[-10:], source_message="[ird_placed]", change_type="set",
                )
                _mem["updated_at"] = _dt.now(_tz.utc).isoformat()
            except Exception as _me:
                logger.warning("[SAROVAR_IRD] memory order capture failed: %r", _me)

            if isinstance(context.pending_data, dict):
                context.pending_data["_ird_order_placed_at"] = _dt.now(_tz.utc).isoformat()
                # Which order is now live, and what is on it. Both are written HERE,
                # after the completed-flow cleanup that runs on the ticket path has
                # already emptied pending_data — written any earlier they were wiped,
                # so the next dish the guest added had nothing to build on and relied
                # entirely on the model re-listing the whole order from memory.
                if str(ticket_ref or "").strip():
                    context.pending_data["_ird_order_ticket_ref"] = str(ticket_ref).strip()
                _items = [
                    {
                        "item": str(i.get("name") or "").strip(),
                        "qty": int(i.get("qty") or 1),
                        "price": i.get("price"),
                        "allergens": i.get("allergens"),
                    }
                    for i in (order_record.get("items") or [])
                    if isinstance(i, dict) and str(i.get("name") or "").strip()
                ]
                if _items:
                    context.pending_data["_ird_open_order_items"] = _items
                    # Cumulative record of what has actually gone to the kitchen this
                    # conversation, keyed by dish. Once an order locks, this is what a
                    # later order is measured against so already-cooking food is not
                    # sent again — see the delta in chat_service.
                    _placed = context.pending_data.get("_ird_placed_items")
                    _placed = dict(_placed) if isinstance(_placed, dict) else {}
                    for _i in _items:
                        _key = str(_i.get("item") or "").strip().lower()
                        if not _key:
                            continue
                        try:
                            _q = max(1, int(_i.get("qty") or 1))
                        except (TypeError, ValueError):
                            _q = 1
                        _placed[_key] = max(int(_placed.get(_key) or 0), _q)
                    context.pending_data["_ird_placed_items"] = _placed

                # ── THE ORDER LEDGER ────────────────────────────────────────
                # One row per order this conversation has actually sent to the
                # kitchen, numbered the way the guest sees them ("Order 2").
                #
                # `_ird_placed_orders` was already mirrored between turns and read
                # in three places — the re-entry check, the recall guard, and the
                # food context handed to the model — but nothing ever WROTE it, so
                # all three read an empty list. That is why the model was told a
                # ticket ref and a timestamp and never WHAT was on the order.
                #
                # Keyed by ticket_ref: adding to an order updates its row rather
                # than adding a second one, so the numbering follows real orders
                # and not the number of times an order was touched.
                #
                # Items carry BOTH `item` and `name` for the same value, because
                # the existing readers disagree about which key to use and neither
                # should have to change.
                # Nothing identifiable went to the kitchen means there is no order
                # to record — an empty row would burn a number the guest never sees.
                if _items:
                    _ref_now = str(ticket_ref or "").strip()
                    _ledger = [
                        r for r in (context.pending_data.get("_ird_placed_orders") or [])
                        if isinstance(r, dict)
                    ]
                    _row = next(
                        (r for r in _ledger if str(r.get("ticket_ref") or "") == _ref_now),
                        None,
                    ) if _ref_now else None
                    # Whether this turn STARTED an order or ADDED to one already
                    # with the kitchen. The guest is told which, because "Order
                    # placed" on an addition reads as a second order arriving.
                    context.pending_data["_ird_last_was_addition"] = _row is not None
                    if _row is None:
                        # ── THE ORDER IS ALREADY IN THE LEDGER, UNDER ITS SLOT ──
                        # The book projects every placed order into this ledger
                        # numbered by its SLOT, but the ticket ref is only known
                        # here - so matching on the ref missed the row that was
                        # just written and appended a SECOND one numbered by
                        # ledger length. Slot 1 was then announced as "Order 2"
                        # and slot 2 as "Order 3", every single time. Claim the
                        # unreferenced row instead of inventing another number.
                        _row = next(
                            (r for r in reversed(_ledger)
                             if not str(r.get("ticket_ref") or "").strip()),
                            None,
                        )
                        if _row is not None:
                            _row["ticket_ref"] = _ref_now
                            _row.setdefault("room", room)
                        else:
                            _row = {
                                "n": len(_ledger) + 1,
                                "ticket_ref": _ref_now,
                                "room": room,
                                "placed_at": context.pending_data.get("_ird_order_placed_at") or "",
                            }
                            _ledger.append(_row)
                    _row["items"] = [
                        {
                            "item": str(_i.get("item") or "").strip(),
                            "name": str(_i.get("item") or "").strip(),
                            "qty": int(_i.get("qty") or 1),
                            "price": _i.get("price"),
                            "allergens": _i.get("allergens"),
                        }
                        for _i in _items
                    ]
                    _row["total"] = order_record.get("total")
                    _row["status"] = "placed"
                    context.pending_data["_ird_placed_orders"] = _ledger[-10:]
                    context.pending_data["_ird_current_order_no"] = int(_row.get("n") or 1)
                    # The fresh-cart flag has done its job the moment that order is
                    # written. Leaving it set would disable the already-cooking
                    # guard for every later order in the conversation, which is how
                    # the kitchen ends up making the same dish twice.
                    context.pending_data.pop("_ird_fresh_order", None)
            decision.metadata.pop("ird_placement", None)
            decision.metadata["ird_placement_committed"] = True
            logger.info("[SAROVAR_IRD] order committed as placed: %s", summary[:120])
            if not summary:
                return ""
            # The dishes carry the emphasis — this is the line a guest re-reads to check
            # the kitchen heard them correctly.
            items = "\n".join(
                f"• **{i.get('name')}**" + (f" × {int(i.get('qty') or 1)}" if int(i.get('qty') or 1) > 1 else "")
                for i in (order_record.get("items") or [])
                if isinstance(i, dict) and str(i.get("name") or "").strip()
            ) or f"• **{summary}**"
            where = f" to room **{room}**" if room else ""
            # NAME WHAT HAPPENED. A conversation can now hold several orders, so
            # "Order placed" alone leaves the guest guessing which one this was and
            # whether their earlier food is still coming. The number is only shown
            # once there IS more than one order — numbering a lone order is noise.
            _pd_c = context.pending_data if isinstance(context.pending_data, dict) else {}
            _no = int(_pd_c.get("_ird_current_order_no") or 0)
            _ledger_len = len([
                r for r in (_pd_c.get("_ird_placed_orders") or []) if isinstance(r, dict)
            ])
            _addition = bool(_pd_c.get("_ird_last_was_addition"))
            if _no > 1 or _ledger_len > 1:
                _hdr = (
                    f"**Added to Order {_no}**" if _addition
                    else f"**Order {_no} placed**"
                )
            else:
                # A conversation with a single order does not need it numbered.
                _hdr = "**Added to your order**" if _addition else "**Order placed**"
            return (
                f"{_hdr} — on its way{where}.\n{items}\n\nEnjoy your meal!"
            )
        except Exception as exc:
            logger.warning("[SAROVAR_IRD] placement commit failed: %r", exc)
            return ""

    async def _ird_reply_must_match_the_book(
        self, *, context: "ConversationContext", decision: "OrchestrationDecision",
        room: str = "", placing_now: bool = False,
    ) -> None:
        """The guest may not be told about food the book does not hold.

        The model is not reliable about writing the order down: two turns with
        byte-identical input at temperature 0 produced a write and a no-write, and
        on the no-write turn it still told the guest "your order is confirmed and
        placed" for 2 Arrabbiata Pasta that reached nothing. No prompt rule fixes
        a coin flip, so the reply is checked against the book and corrected when
        they disagree.

        Deliberately narrow. It only acts when the reply CLAIMS a dish is in the
        order - offering, describing, pricing or declining a dish is untouched -
        and only when the book cannot support the claim. A correct reply is never
        rewritten.
        """
        text = str(getattr(decision, "response_text", "") or "").strip()
        if not text:
            return
        from services import ird_cart_book as bk
        from services import ird_cart_store as bs

        row = await bs.load(
            hotel_code=str(getattr(context, "hotel_code", "") or ""),
            session_id=str(getattr(context, "session_id", "") or ""),
            create=False)
        slots = bk.slots_from_row(row) if row else []
        live = [sl for sl in slots if sl.get("items")]
        held = {str(i.get("item", "")).strip().lower()
                for sl in live for i in (sl.get("items") or [])}
        held |= {re.sub(r"\s*\([^)]*\)", "", h).strip() for h in held}

        claimed = await self._ird_dishes_the_reply_claims_are_ordered(reply_text=text)
        if claimed is None:
            return

        # ── CLAIMED PLACED, BUT STILL SITTING IN THE CART ────────────────────
        # A dish CAN be in the book and still not have gone anywhere. The bot
        # told a guest "your separate order for 2 Arrabbiata Pasta has been
        # placed and is on its way" while that slot was `ongoing` with no ticket
        # - the pasta was written down but never sent, and the guest had no
        # reason to ask again. Existence is not placement.
        # NOT ON THE TURN THAT IS PLACING IT. The status flips to `placed` later
        # in the turn, when the ticket is actually written - so on the placement
        # turn itself the slot still reads `ongoing` and this check called a true
        # statement a lie, rewrote "your order is placed" into "shall I place this
        # order?", and the guest could never get past it. Three times in a row.
        # IS THIS TURN PLACING THE ORDER? Read every signal, because they do not
        # all arrive together: `ready_to_create` is set late, `ticket.required`
        # may still be false while the model has already chosen `create_ticket`.
        # Checking only one of them made this guard rewrite a real placement into
        # "shall I place this order?" - the guest said place it, then yes, then
        # yes again, and nothing was ever sent to the kitchen.
        _ticket = getattr(decision, "ticket", None)
        # ── ONLY EXEMPT A TURN THAT WILL REALLY PLACE SOMETHING ──────────────
        # `action == create_ticket` alone is NOT enough. When the model never
        # wrote the order, the cart is empty, so the reconciliation below refuses
        # to force a ticket - and if this guard also stands down on the same
        # signal, the guest is told "your order has been placed, on its way" and
        # NOTHING is raised. That is exactly what QA found twice: a Rasmalai and
        # a Tiramisu confirmed to a guest with zero tickets, on the turns where
        # the bot had described the dish and asked rather than adding it.
        _has_something_to_place = bool(
            live or (context.pending_data or {}).get("_ird_cart"))
        _this_turn_places = bool(
            placing_now
            or (_ticket is not None and getattr(_ticket, "required", False))
            or (str(getattr(decision, "action", "") or "").strip().lower()
                == "create_ticket" and _has_something_to_place)
        )
        if _CLAIMS_PLACED.search(text) and not _this_turn_places:
            waiting = [sl for sl in live if sl.get("status") == bk.ONGOING]
            for sl in waiting:
                names = {re.sub(r"\s*\([^)]*\)", "", str(i.get("item", "")).strip().lower())
                         for i in (sl.get("items") or [])}
                if any(n and any(n in c.lower() or c.lower() in n for c in claimed)
                       for n in names):
                    logger.warning(
                        "[IRD_BOOK] the reply said slot %d was placed, but it is still "
                        "waiting - showing the order and asking instead", sl["n"])
                    decision.response_text = self._render_ird_summary(
                        sl.get("items") or [], room=room,
                        already_placed=False, order_no=sl["n"])
                    return
        phantom = [
            c for c in claimed
            if not any(h and (h in c.lower() or c.lower() in h) for h in held)
        ]
        if not phantom:
            return

        logger.warning(
            "[IRD_BOOK] the reply promised %s, which the book does not hold - "
            "the guest will not be told about food nobody is making", phantom)
        # ANSWER ABOUT THE DISH THEY ASKED FOR. Showing an unrelated order instead
        # is honest but useless: the guest asked for a Filter Coffee and got a
        # status line about their phulka. Say plainly that it is not down yet and
        # offer to add it, which is the only thing that moves them forward.
        _named = phantom[0] if len(phantom) == 1 else ", ".join(phantom)
        decision.response_text = (
            "Sorry - I have not actually got the %s onto an order yet, so nothing "
            "has gone to the kitchen for it. Shall I add it and place it for you?"
            % _named
        )

    async def _work_for_a_second_team(
        self, *, guest_message: str, raised_issue: str, raised_department: str,
    ) -> "dict | None":
        """Which teams does this one message actually need, and what is each one's part?

        One request reaches one team, so a message covering two of them loses the
        second silently: a guest reported a broken AC and asked for 2 extra
        pillows, both went onto one Engineering ticket, and Housekeeping was never
        told - three times out of three. Telling the model not to bundle did not
        hold, because bundling was the only thing the system could actually do.

        Returns {"first_issue": <the raised team's own part>, "others": [
        {"department":..., "issue":...}, ...]} or None when one team covers it.

        ASKING THIS AS "IS ANYTHING LEFT OVER?" DOES NOT WORK, and that is worth
        recording. The already-raised issue text is itself the bundled one ("Fix
        AC not cooling and provide 2 extra pillows"), so a judge shown it can
        reason, perfectly correctly, that the pillows are already handled and
        return nothing. It did exactly that, intermittently, which looked like
        model flakiness and was really a contradictory question. So it is not
        asked what is left over. It is asked to split the guest's message by team,
        knowing nothing about what was raised - a question with one right answer -
        and the CODE then takes the part belonging to the team being raised now
        and turns every other part into its own ticket.
        """
        msg = str(guest_message or "").strip()
        if not msg:
            return None
        system_prompt = (
            "A hotel guest sent one message. Split what they asked for by which "
            "team has to do it. "
            "The teams: Housekeeping (towels, pillows, linen, cleaning, "
            "toiletries, amenities), Engineering (anything broken or not working - "
            "AC, plumbing, TV, lights), Food & Beverage (food and drink), Front "
            "Office (billing, checkout, luggage, general front desk).\n"
            "Return STRICT JSON: {\"teams\": [{\"department\": \"<team>\", "
            "\"issue\": \"<one sentence covering ONLY that team's part>\"}, ...]}\n"
            "USUALLY THERE IS EXACTLY ONE TEAM - return a single entry. A long or "
            "rambling request for one thing is still one team. Only return two or "
            "more when the guest genuinely asked for different things owned by "
            "different teams.\n"
            "The entries must be disjoint: everything the guest asked for belongs "
            "to exactly one entry, and no entry may mention another's work. Each "
            "issue should name the room number if the message gives one.\n"
            "Return an empty list if the message asks for nothing at all."
        )
        try:
            raw = await self._chat_with_json(
                messages=[
                    {"role": "system", "content": system_prompt},
                    # Deliberately NOT told what was already raised - see above.
                    {"role": "user", "content": json.dumps({
                        "guest_message": msg[:600],
                    }, ensure_ascii=False)},
                ],
                model=(getattr(settings, "llm_judge_model", "") or settings.openai_model),
                temperature=0.0, max_tokens=400,
                trace_context={"responder_type": "main", "agent": "second_team_split"},
            )
            teams = (raw or {}).get("teams") if isinstance(raw, dict) else None
            if not isinstance(teams, list) or len(teams) < 2:
                return None
            parsed = [
                {"department": str(t.get("department") or "").strip(),
                 "issue": str(t.get("issue") or "").strip()}
                for t in teams if isinstance(t, dict)
            ]
            parsed = [t for t in parsed if t["department"] and t["issue"]]
            if len(parsed) < 2:
                return None
            # Which entry is the ticket we are about to raise? Match on the
            # department name; if none matches, keep the first entry as ours so a
            # naming mismatch can never drop a part on the floor.
            want = str(raised_department or "").strip().lower()
            mine = next((t for t in parsed
                         if t["department"].strip().lower() == want), None)
            if mine is None:
                mine = parsed[0]
            others = [t for t in parsed if t is not mine]
            if not others:
                return None
            logger.info("[SPLIT] %d teams in this message: %s", len(parsed),
                        ", ".join(t["department"] for t in parsed))
            return {"first_issue": mine["issue"], "others": others}
        except Exception as exc:
            logger.warning("[SPLIT] team split failed (non-fatal): %s", exc)
            return None

    async def _which_order_does_the_guest_mean(
        self, *, guest_message: str, orders: "list[dict]",
    ) -> str:
        """Which open order is this message about? A slot number, "new", or "".

        Asked only when the guest has MORE THAN ONE order open and the model did
        not say which it meant. Until now the code guessed from the dish list -
        a set of dishes sharing nothing with the open cart was assumed to be a
        new order - and that guess is blind to the only thing that actually says
        so, which is what the guest said. A woman ordering for herself and then
        "another order for my husband, he's vegetarian" had both merged into one
        slot: her tonic and dessert served up inside his order, on one bill.

        Returns "" when it cannot tell, and the caller then behaves exactly as it
        did before - the existing heuristics stay as the fallback, this only ever
        replaces a guess with an answer.
        """
        msg = str(guest_message or "").strip()
        live = [o for o in (orders or []) if isinstance(o, dict) and o.get("items")]
        if not msg or len(live) < 2:
            return ""
        summary = [
            {"order": o.get("slot"),
             "status": o.get("status"),
             "dishes": [str(i.get("item", "")) for i in (o.get("items") or [])]}
            for o in live
        ]
        system_prompt = (
            "A hotel guest has more than one in-room dining order open at once - "
            "for example one for themselves and one for someone else. They have "
            "just sent a message about food.\n"
            "Say WHICH order the message is about.\n"
            "Return STRICT JSON: {\"order\": <the order number>} to mean an "
            "existing order, {\"order\": \"new\"} to start another one, or "
            "{\"order\": \"\"} when you cannot tell.\n"
            "Food for a DIFFERENT PERSON ('for my husband', 'for my daughter'), "
            "or an explicit 'separate'/'another' order, is \"new\" unless they "
            "clearly point at one of the orders listed.\n"
            "Adding to, changing or asking about food already listed in one of "
            "the orders is THAT order's number.\n"
            "Return \"\" if the message does not pick one out."
        )
        try:
            raw = await self._chat_with_json(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps({
                        "guest_message": msg[:600], "open_orders": summary,
                    }, ensure_ascii=False)},
                ],
                model=(getattr(settings, "llm_judge_model", "") or settings.openai_model),
                temperature=0.0, max_tokens=80,
                trace_context={"responder_type": "main", "agent": "which_order"},
            )
            if not isinstance(raw, dict):
                return ""
            picked = str(raw.get("order") or "").strip().lower()
            if picked == "new":
                return "new"
            # Only ever an order that is actually open.
            return picked if picked in {str(o.get("slot")) for o in live} else ""
        except Exception as exc:
            logger.warning("[IRD_BOOK] which-order check failed (non-fatal): %s", exc)
            return ""

    async def _tickets_this_turn_needs(
        self, *, recent_turns: "list[dict]", services: "list[dict]",
        departments: "list[dict]", already_open: "list[str]", room: str = "",
        food_orders: "list[str] | None" = None,
    ) -> "list[dict] | None":
        """Every ticket this turn should raise, one per team. [] when none.

        THE ONE PLACE THAT DECIDES TICKETS. Until now the same LLM call both
        talked to the guest and decided ticketing, so the two could disagree - and
        did. Asked about a leaking tap, an uncleaned room and a club sandwich, the
        router handed the whole message to In-Room Dining; the food agent has no
        tap and no mop, emitted `respond_only` because there was no food order to
        place, and still wrote "I've passed this to Engineering and Housekeeping."
        Nothing was raised. Challenged, it repeated the claim. Three teams' work
        vanished because the deciding and the talking were the same call.

        This is only the deciding. It never writes a word the guest sees.

        Nothing here is hardcoded: the teams come from the hotel's live department
        list and the services from the live catalogue, so a new department or
        service is understood the day it is added. The caller checks every
        department back against that same list before raising anything.
        """
        turns = [t for t in (recent_turns or []) if isinstance(t, dict)]
        depts = [
            {"department": str(d.get("department_name") or d.get("name") or "").strip(),
             "id": str(d.get("department_id") or d.get("id") or "").strip()}
            for d in (departments or []) if isinstance(d, dict)
        ]
        depts = [d for d in depts if d["department"]]
        # None means "could not decide", [] means "decided: nothing to raise".
        # The caller blocks a ticket on [] and leaves the old behaviour alone on
        # None, so a missing department list must never read as "raise nothing".
        if not turns or not depts:
            return None
        catalogue = [
            {"service": str(s.get("name") or s.get("id") or "").strip(),
             "handles": str(s.get("what_it_handles") or s.get("description") or "").strip()[:180]}
            for s in (services or []) if isinstance(s, dict)
        ]
        system_prompt = (
            "You decide which STAFF TICKETS a hotel conversation needs. You never "
            "talk to the guest and nothing you write is shown to them.\n"
            "Read the conversation and return one ticket per TEAM that has real "
            "work to do.\n"
            "Return STRICT JSON: {\"tickets\": [{\"department\": \"<a department "
            "name exactly as listed>\", \"issue\": \"<one sentence naming only "
            "that team's part>\"}, ...]}\n"
            "\n"
            "RAISE a ticket only for work the guest has actually ASKED FOR and, "
            "where the assistant asked them to confirm, CONFIRMED. A question is "
            "not a request: 'do you do laundry?' asks whether the service exists "
            "and needs no ticket; 'please collect my laundry' does. Something the "
            "assistant merely offered, or the guest declined or was still "
            "choosing, is not a request.\n"
            # ── THE SAFETY RULE HAS TO REACH THE TICKET SIDE TOO ─────────────
            # The reply correctly refused a knife - and this judge, which only
            # ever sees the conversation, folded it into the housekeeping ticket
            # anyway: "Guest requests 2 extra towels and a knife." Staff would
            # have delivered exactly what the guest had just been refused. A
            # guardrail on the words alone is only half of one.
            "NEVER RAISE ANYTHING UNSAFE, and never let it ride along inside "
            "another ticket. If part of what the guest asked for could be used to "
            "hurt someone - a weapon, a knife or blade, a lighter or fuel, drugs "
            "or medication, anything illegal - leave that part out entirely and "
            "raise only the rest. If a request is ONLY that, return no ticket for "
            "it at all. Ordinary cutlery arriving with a meal is part of that "
            "meal and is fine; the same item asked for on its own is not.\n"
            "NEVER raise work the assistant EXPLICITLY told the guest is not "
            "possible - closed for the night, off the menu, outside its hours, "
            "not something the hotel does. Sending staff a job the guest was told "
            "would not happen is worse than sending nothing: a club sandwich "
            "declined as 'the kitchen is closed' must not reach the kitchen. "
            "SILENCE IS NOT A REFUSAL - something the guest asked for that the "
            "assistant simply did not mention is still work they asked for, and "
            "still needs its ticket.\n"
            "ALREADY OPEN work must NOT be raised again. The open list is what "
            "staff already have; return only what is genuinely new. If the guest "
            "is chasing or asking about something already open, that is not a new "
            "ticket.\n"
            "SPLIT BY TEAM. A message covering two teams' work is two tickets, "
            "each naming only its own part and never mentioning the other's. Use "
            "as many as the work genuinely needs.\n"
            "Every `department` MUST be copied exactly from the list you are "
            "given. Never invent one. If no listed team fits, leave it out.\n"
            "FOOD ORDERS: `food_orders` lists the guest's orders and says, for "
            "each, whether the kitchen has been told.\n"
            "- ALREADY TICKETED -> never raise it again.\n"
            "- still being chosen (ongoing) -> raise nothing; they have not "
            "finished ordering. Never raise a ticket for the INTENTION to order "
            "('I'd like a separate order for my husband').\n"
            "- PLACED but NO TICKET RAISED YET -> the guest believes that food is "
            "coming and the kitchen has never heard of it. RAISE IT, to the food "
            "team, listing exactly the dishes shown for that order.\n"
            "Food asked for in passing alongside other teams' work, outside any "
            "order, also needs a ticket from you.\n"
            "Dishes that were only DISCUSSED - asked about, priced, recommended, "
            "declined as unavailable, or still being chosen - are NOT an order, "
            "however the assistant phrased its reply. A ticket was once raised "
            "for '3 masala dosa and 1 filter coffee' that the assistant had just "
            "refused as out of hours, purely because it said 'the team will take "
            "care of it'.\n"
            "BUT A CONFIRMED ORDER MISSING FROM `food_orders` STILL NEEDS RAISING. "
            "If the guest clearly ordered dishes and confirmed them, and those "
            "dishes appear in NO order above, then the order failed to be written "
            "down: the guest has been told their food is coming and the kitchen "
            "has never heard of it. Raise it to the food team with exactly the "
            "dishes they confirmed. ORDERED-AND-CONFIRMED is the test, not whether "
            "it reached the list - a guest who said yes is owed their food.\n"
            "That last rule applies ONLY when the dishes are in NO order above. If "
            "they are sitting in an order marked ongoing, the guest is still "
            "building it and has not asked for it to be sent - leave it alone, "
            "however much the exchange sounds like ordering. Adding a dish to a "
            "cart is not placing an order.\n"
            "Name the room number in each issue when the conversation gives one.\n"
            "Return {\"tickets\": []} when nothing needs raising - that is the "
            "ordinary answer for most turns."
        )
        try:
            raw = await self._chat_with_json(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps({
                        "conversation": turns[-12:],
                        "room_number": str(room or ""),
                        "teams_available": [d["department"] for d in depts],
                        "services_this_hotel_offers": catalogue,
                        "already_open_with_staff": [str(x) for x in (already_open or [])][:15],
                        "food_orders": [str(x) for x in (food_orders or [])][:10],
                    }, ensure_ascii=False)},
                ],
                model=(getattr(settings, "llm_judge_model", "") or settings.openai_model),
                temperature=0.0, max_tokens=600,
                trace_context={"responder_type": "main", "agent": "ticket_planner"},
            )
            rows = (raw or {}).get("tickets") if isinstance(raw, dict) else None
            if not isinstance(rows, list):
                return None
            # CODE ENFORCES THE TEAM. The judge may only name a team the hotel
            # actually has; anything else is dropped rather than guessed at.
            _known = {d["department"].strip().lower(): d for d in depts}
            out: list[dict] = []
            for r in rows:
                if not isinstance(r, dict):
                    continue
                name = str(r.get("department") or "").strip()
                issue = str(r.get("issue") or "").strip()
                hit = _known.get(name.lower())
                if not hit or not issue:
                    if name or issue:
                        logger.info("[TICKET_PLAN] dropped %r/%r - not a team this "
                                    "hotel has", name, issue[:50])
                    continue
                out.append({"department": hit["department"],
                            "department_id": hit["id"], "issue": issue})
            if out:
                logger.info("[TICKET_PLAN] %d ticket(s): %s", len(out),
                            "; ".join("%s: %s" % (t["department"], t["issue"][:45])
                                      for t in out))
            return out
        except Exception as exc:
            logger.warning("[TICKET_PLAN] planning failed (non-fatal): %s", exc)
            return None

    async def _cancel_names_what_it_means(self, *, user_message: str) -> bool:
        """Does this cancel actually NAME what to cancel, or just point at it?

        "cancel the spa booking" names it. "actually cancel that" does not - it
        points, and what it points at is whatever the guest was last dealing
        with. The cancel matcher answers TRUE for anything unnamed on purpose,
        because context is normally all the guest gives; but that makes an unnamed
        cancel land on whichever ticket happens to be in front of the code. A
        guest with a form open on screen said "actually cancel that" and their
        FOOD ORDER was matched instead - and the rest of the same message, a spa
        booking, was dropped on the floor.

        So the caller asks this first: a cancel that names nothing must not reach
        past whatever is open in front of the guest to touch an older ticket.
        """
        msg = str(user_message or "").strip()
        if not msg:
            return False
        system_prompt = (
            "A hotel guest wants to cancel something. Say whether their message "
            "NAMES what they want cancelled, or only points at it.\n"
            "Return STRICT JSON: {\"names_it\": true|false}\n"
            "TRUE - it names the thing: 'cancel the spa booking', 'cancel my "
            "table reservation', 'cancel the food order', 'cancel the towels'.\n"
            "FALSE - it only points: 'cancel that', 'cancel it', 'actually no', "
            "'forget it', 'never mind', 'we don't need it anymore'.\n"
            "Naming the thing means saying WHICH service or item, in any words. "
            "A pronoun is not a name."
        )
        try:
            raw = await self._chat_with_json(
                messages=[{"role": "system", "content": system_prompt},
                          {"role": "user", "content": msg[:400]}],
                model=(getattr(settings, "llm_judge_model", "") or settings.openai_model),
                temperature=0.0, max_tokens=40,
                trace_context={"responder_type": "main", "agent": "cancel_names_it"},
            )
            return bool(isinstance(raw, dict) and raw.get("names_it") is True)
        except Exception as exc:
            logger.warning("[TICKET_EDIT] cancel-naming check failed (non-fatal): %s", exc)
            # Unknown means "assume it named something", which leaves the existing
            # behaviour exactly as it was.
            return True

    async def _reply_claims_staff_were_told(self, *, reply_text: str) -> bool:
        """Does this reply tell the guest their request has been passed to staff?

        The safety net behind the planner. A reply that says "I've passed this to
        Engineering and Housekeeping" when no ticket exists is the worst failure
        this system has - the guest stops worrying, and nobody is coming. So the
        assistant's own words are treated as a REQUEST FOR A TICKET: if it told
        the guest staff were told, staff get told.

        Only counts an assertion that it is DONE. Offering to raise something,
        asking whether to go ahead, or saying a team can help is not a claim.
        """
        text = str(reply_text or "").strip()
        if not text:
            return False
        system_prompt = (
            "You are reading one message a hotel assistant just sent a guest.\n"
            "Say whether it tells the guest that their request HAS BEEN passed to "
            "hotel staff / a team / a department, or that it has been raised, "
            "logged, noted for action, or that someone will attend to it.\n"
            "Return STRICT JSON: {\"claims_raised\": true|false}\n"
            "TRUE: 'I've passed this to Engineering', 'your request has been "
            "logged', 'the team will attend to it shortly', 'Done - housekeeping "
            "will bring them'.\n"
            "FALSE: offering or asking - 'shall I raise this?', 'should I go "
            "ahead?', 'would you like me to inform them?', 'our team can help "
            "with that', or simply answering a question."
        )
        try:
            raw = await self._chat_with_json(
                messages=[{"role": "system", "content": system_prompt},
                          {"role": "user", "content": text[:1200]}],
                model=(getattr(settings, "llm_judge_model", "") or settings.openai_model),
                temperature=0.0, max_tokens=40,
                trace_context={"responder_type": "main", "agent": "claims_raised"},
            )
            return bool(isinstance(raw, dict) and raw.get("claims_raised") is True)
        except Exception as exc:
            logger.warning("[TICKET_PLAN] claim check failed (non-fatal): %s", exc)
            return False

    async def _is_the_guest_asking_about_a_dish(self, *, user_message: str) -> bool:
        """Is this message about FOOD ITSELF - a dish, a drink, what there is to eat?

        Only the service that owns the menu can answer that. The hotel-info lane
        has the property KB but no menu, so it answered "do you have idli" with
        "I don't have idli listed specifically" - while the same question one turn
        into a food order got "We do serve Idli, 7:00 AM to 10:30 AM". Idli is on
        the menu. Turning a guest away from food the kitchen actually cooks is
        worse than any wording problem.

        Deliberately narrow, and it is a gate rather than a router: a general
        "which service is this?" judge answered "the front desk" for "what time
        does the bar close", which would have dragged ordinary hotel questions out
        of the lane that answers them properly. This asks one thing only, and the
        caller still has to resolve WHICH service owns the menu afterwards.
        """
        msg = str(user_message or "").strip()
        if not msg:
            return False
        system_prompt = (
            "A hotel guest sent one message. Say whether they are asking about "
            "FOOD OR DRINK ITSELF - whether a dish exists, what it costs, what "
            "there is to eat or drink, or asking to order something.\n"
            "Return STRICT JSON: {\"about_food\": true|false}\n"
            "TRUE: 'do you have idli', 'any burgers?', \"what's there to eat\", "
            "'what desserts do you have', 'how much is the paneer tikka', 'send "
            "up a coffee', and ALSO when a NAMED DISH is served - 'what time is "
            "idli served', 'until when can i get dosa'.\n"
            "FALSE: anything about the hotel rather than the food - where an "
            "outlet is, the gym, the pool, wifi, check-in and check-out, laundry, "
            "a table booking, or a room. Also FALSE for the opening hours of a "
            "PLACE: 'what time does the bar close', 'when does the restaurant "
            "open', 'what time is breakfast'.\n"
            "The line is WHAT is being asked about, not what kind of question it "
            "is. Hours of a PLACE -> false. Anything about a DISH, including the "
            "hours of that dish -> true, because only the menu knows when a "
            "particular dish can be ordered to the room."
        )
        try:
            raw = await self._chat_with_json(
                messages=[{"role": "system", "content": system_prompt},
                          {"role": "user", "content": msg[:400]}],
                model=(getattr(settings, "llm_judge_model", "") or settings.openai_model),
                temperature=0.0, max_tokens=40,
                trace_context={"responder_type": "main", "agent": "about_food_gate"},
            )
            return bool(isinstance(raw, dict) and raw.get("about_food") is True)
        except Exception as exc:
            logger.warning("[MAIN_ROUTER] food-question gate failed (non-fatal): %s", exc)
            return False

    async def _dishes_the_guest_just_accepted(
        self, *, last_bot_message: str, guest_message: str,
    ) -> "list[dict]":
        """The guest said yes to a dish you offered — which one, and how many?

        The mirror of `_ird_dishes_the_reply_claims_are_ordered`. That one catches
        a reply promising food the book does not hold; this catches the opposite,
        which turned out to be the more common failure: the bot offered a
        Tiramisu, the guest answered "yes and can i also get 2 towels", and the
        model wrote NOTHING to the cart. The book stayed at 2 burgers, the reply
        is rendered from the book so it cheerfully offered a dessert again, and
        the ticket planner - reading the conversation rather than the book -
        raised a kitchen ticket for a Tiramisu that existed in no order at all.
        Kitchen, book and guest all disagreeing, from one missing write.

        Returns [] whenever nothing was clearly accepted, which is the ordinary
        answer. Only an offer the guest plainly took is worth a line.
        """
        offer = str(last_bot_message or "").strip()
        reply = str(guest_message or "").strip()
        if not offer or not reply:
            return []
        system_prompt = (
            "A hotel assistant offered a guest something from the menu, and the "
            "guest replied. Say which dishes - if any - the guest has just "
            "ACCEPTED.\n"
            "Return STRICT JSON: {\"accepted\": [{\"item\": \"<dish name exactly "
            "as the assistant wrote it>\", \"qty\": <number>}, ...]}\n"
            "ACCEPTED means they agreed to have it: 'yes', 'sure', 'go ahead', "
            "'that one', 'the tiramisu please'. Default qty to 1 unless they say "
            "otherwise.\n"
            "Return an EMPTY list when: the assistant offered nothing; the guest "
            "declined, ignored it or changed the subject; the guest asked a "
            "question instead of accepting; or the assistant offered SEVERAL "
            "dishes and the guest's reply does not make clear WHICH - a bare "
            "'yes' to a list of three sandwiches picks none of them.\n"
            "Only dishes the ASSISTANT actually named in that message. Never "
            "invent one, and never carry over food from earlier in the chat.\n"
            # ── A RESTAURANT IS NOT A DISH ──────────────────────────────────
            # This runs on turns another lane answered, so the message it is
            # shown is often not a menu at all. Handed a list of the hotel's
            # outlets and the guest's 'spr kitchen please, 4 people, tonight
            # 8:30pm', it returned {item: "SPR Kitchen", qty: 4} - a venue,
            # written into the food cart as four helpings of a dish that does
            # not exist, on its way to the kitchen.
            "The assistant's message is NOT always a menu. It may be a list of "
            "restaurants, bars or other venues, a table-reservation reply, or "
            "anything else. A VENUE, OUTLET, RESTAURANT, BAR or ROOM NAME IS NOT "
            "A DISH - if the guest is choosing where to sit, what time to book, "
            "or how many people are coming, they have accepted NO food: return "
            "an empty list. Only return something you are confident is an actual "
            "item of food or drink the guest agreed to have."
        )
        try:
            raw = await self._chat_with_json(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps({
                        "assistant_offered": offer[:900],
                        "guest_replied": reply[:400],
                    }, ensure_ascii=False)},
                ],
                model=(getattr(settings, "llm_judge_model", "") or settings.openai_model),
                temperature=0.0, max_tokens=200,
                trace_context={"responder_type": "main", "agent": "accepted_offer"},
            )
            rows = (raw or {}).get("accepted") if isinstance(raw, dict) else None
            if not isinstance(rows, list):
                return []
            out: list[dict] = []
            for r in rows:
                if not isinstance(r, dict):
                    continue
                name = str(r.get("item") or "").strip()
                if not name:
                    continue
                try:
                    qty = max(1, int(r.get("qty") or 1))
                except (TypeError, ValueError):
                    qty = 1
                out.append({"item": name, "qty": qty})
            return out
        except Exception as exc:
            logger.warning("[IRD_BOOK] accepted-offer check failed (non-fatal): %s", exc)
            return []

    async def _service_for_the_latest_turn(
        self, *, recent_turns: "list[str]", latest_message: str,
        services: "list[dict]",
    ) -> str:
        """Which service should handle the guest's LATEST message? "" if unclear.

        The single-message judge cannot answer this - "how much will it cost"
        means nothing alone - and handing it the transcript instead makes it
        answer about the FIRST topic it sees: given a food order followed by
        three turns of arranging a taxi, it said in-room dining. So the latest
        turn is separated from its context here, and the prompt says plainly
        that the earlier turns are only there to disambiguate it.

        Exists because the router emitted `service_id: "in_room_dining"` with
        `reason: "continuing the TRANSPORT service flow"` - it had understood the
        turn and named the stale service anyway, and the guest asking the price
        of an airport drop was shown their burger cart.
        """
        msg = str(latest_message or "").strip()
        catalogue = [
            {"id": str(s.get("id") or "").strip(),
             "name": str(s.get("name") or "").strip(),
             "handles": str(s.get("what_it_handles") or s.get("description") or "").strip()[:200]}
            for s in (services or []) if isinstance(s, dict) and str(s.get("id") or "").strip()
        ]
        if not msg or not catalogue:
            return ""
        system_prompt = (
            "A hotel guest is mid-conversation. You are given the recent turns "
            "and then their LATEST message.\n"
            "Say which ONE service should handle THE LATEST MESSAGE.\n"
            "Return STRICT JSON: {\"service_id\": \"<an id from the list, or "
            "empty string>\"}\n"
            "THE EARLIER TURNS ARE CONTEXT ONLY. They are there to tell you what "
            "a short message like 'how much will it cost', 'yes', or 'what time' "
            "is ABOUT. They are NOT the answer. A guest who ordered food an hour "
            "ago and is now arranging a taxi is asking about the TAXI.\n"
            "Weigh the most recent exchange most heavily: whatever the guest and "
            "the assistant were just discussing is almost always what a short "
            "follow-up refers to.\n"
            "The id MUST be copied exactly from the list. Never invent one.\n"
            "Return \"\" when the latest message is small talk, a general question "
            "about the hotel, or does not ask anyone to DO anything."
        )
        try:
            raw = await self._chat_with_json(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps({
                        "earlier_turns": [str(t)[:220] for t in (recent_turns or [])][-6:],
                        "LATEST_message_to_route": msg[:400],
                        "services": catalogue,
                    }, ensure_ascii=False)},
                ],
                model=(getattr(settings, "llm_judge_model", "") or settings.openai_model),
                temperature=0.0, max_tokens=120,
                trace_context={"responder_type": "main", "agent": "service_for_latest_turn"},
            )
            if not isinstance(raw, dict):
                return ""
            picked = str(raw.get("service_id") or "").strip().lower()
            return picked if picked in {c["id"].lower() for c in catalogue} else ""
        except Exception as exc:
            logger.warning("[MAIN_ROUTER] latest-turn service check failed "
                           "(non-fatal): %s", exc)
            return ""

    async def _service_this_message_belongs_to(
        self, *, user_message: str, services: "list[dict]",
    ) -> str:
        """Which of THESE services should handle this message? "" when none fits.

        The main router sometimes answers "this needs a service" and then fails to
        name one - `route=service`, `service_id=null`. The name was being thrown
        away along with the verdict, dropping the turn into the conversational
        lane, so a broken AC and 2 extra pillows were answered with "please
        contact the front desk" and never reached Engineering or Housekeeping.

        Given only the message and the live service catalogue, this names the
        service. It never invents one: the id must come from the list, and the
        caller checks it against the same list again before using it. Returning ""
        is always safe - the caller then behaves exactly as it did before.
        """
        msg = str(user_message or "").strip()
        catalogue = [
            {"id": str(s.get("id") or "").strip(),
             "name": str(s.get("name") or "").strip(),
             "handles": str(s.get("what_it_handles") or s.get("description") or "").strip()[:220]}
            for s in (services or []) if isinstance(s, dict) and str(s.get("id") or "").strip()
        ]
        if not msg or not catalogue:
            return ""
        system_prompt = (
            "A hotel guest sent one message. Say which ONE of the hotel's "
            "services should handle it.\n"
            "Return STRICT JSON: {\"service_id\": \"<an id from the list, or "
            "empty string>\"}\n"
            "The id MUST be copied exactly from the list you are given. Never "
            "invent one.\n"
            "Return \"\" when the message is small talk, a general question about "
            "the hotel, or does not ask anyone to DO anything.\n"
            "When the message asks for more than one team's help, name the "
            "service for the FIRST thing they asked for - the rest is handled "
            "elsewhere."
        )
        try:
            raw = await self._chat_with_json(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps({
                        "guest_message": msg[:600], "services": catalogue,
                    }, ensure_ascii=False)},
                ],
                model=(getattr(settings, "llm_judge_model", "") or settings.openai_model),
                temperature=0.0, max_tokens=120,
                trace_context={"responder_type": "main", "agent": "service_recovery"},
            )
            if not isinstance(raw, dict):
                return ""
            picked = str(raw.get("service_id") or "").strip().lower()
            # Only ever a name that was on the list we handed it.
            return picked if picked in {c["id"].lower() for c in catalogue} else ""
        except Exception as exc:
            logger.warning("[MAIN_ROUTER] service recovery failed (non-fatal): %s", exc)
            return ""

    async def _work_not_already_raised(
        self, *, new_request: str, already_raised: "list[str]",
    ) -> "str | None":
        """What in this request has NOT already been passed to staff?

        The ticket issue is rebuilt from the WHOLE conversation on every turn, so
        a guest who adds one thing gets everything they ever asked for restated:
        after a broken AC and 2 extra pillows were already raised, asking for
        towels produced "AC not cooling and 2 extra pillows and 2 extra towels"
        and re-raised all three. Two duplicate tickets went to teams already
        working on them, and nothing subtracted what was already in hand.

        Returns the remaining request, "" when every part of it is already open,
        or None when it cannot tell - and None means RAISE IT UNCHANGED. A
        duplicate ticket is a nuisance; a dropped request is a guest waiting for
        something nobody is bringing, so this only ever fails towards raising.

        This is NOT the "is anything left over?" question that was tried and
        reverted on the team split - see `_work_for_a_second_team`. That one was
        contradictory because the text shown as already-raised WAS the text being
        judged. Here the open tickets are genuinely different, earlier records,
        and the question has one right answer.
        """
        req = str(new_request or "").strip()
        if not req:
            return None
        open_items = [str(x or "").strip() for x in (already_raised or [])]
        open_items = [x for x in open_items if x]
        if not open_items:
            # Nothing is open, so nothing can be a repeat. Costs no LLM call on
            # the ordinary first-request turn.
            return req
        system_prompt = (
            "A hotel guest already has these requests OPEN with staff. A new "
            "summary of what they need has just been written, and it is rebuilt "
            "from the whole conversation each time - so it usually repeats work "
            "that was already passed on.\n"
            "Say which part of the new summary is NOT already open.\n"
            "Return STRICT JSON with BOTH keys, ALWAYS present:\n"
            "{\"all_already_open\": true|false, \"remaining\": \"<the part that is "
            "NOT already open, as one sentence>\"}\n"
            "Every part already open -> all_already_open true, remaining \"\".\n"
            "Otherwise -> all_already_open false, and remaining carries ONLY the "
            "genuinely new part; if none of it is already open, that is the whole "
            "new summary.\n"
            "Same work means the same thing asked for: 'AC not cooling' and 'the "
            "air conditioning is not cooling' are the same; '2 extra towels' and "
            "'2 extra pillows' are different things and neither covers the other. "
            "Keep the room number in what you return."
        )
        try:
            raw = await self._chat_with_json(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps({
                        "already_open": open_items[:15],
                        "new_summary": req[:600],
                    }, ensure_ascii=False)},
                ],
                model=(getattr(settings, "llm_judge_model", "") or settings.openai_model),
                temperature=0.0, max_tokens=300,
                trace_context={"responder_type": "main", "agent": "work_already_raised"},
            )
            # Decided from the flag the judge emits, never from an absent key.
            # Asked for a bare "remaining" it answered {} when everything was
            # already open - indistinguishable from a response that failed to
            # parse, and reading that as "drop the ticket" would bin real
            # requests on any malformed reply. So the verdict is its own field
            # and anything else here means "raise it unchanged".
            if not isinstance(raw, dict):
                return None
            flag = raw.get("all_already_open")
            rest = raw.get("remaining")
            if flag is True:
                return ""
            if flag is False and isinstance(rest, str) and rest.strip():
                return rest.strip()
            return None
        except Exception as exc:
            logger.warning("[DEDUPE] already-raised check failed (non-fatal): %s", exc)
            return None

    async def _ird_dishes_the_reply_claims_are_ordered(
        self, *, reply_text: str,
    ) -> "list[str] | None":
        """Which dishes does this reply tell the guest are IN their order?

        Not "which dishes are mentioned" - a reply may recommend, describe or
        decline a dozen dishes without claiming any of them was ordered. Only the
        ones it asserts are in the order, on the way, added, or being prepared.

        Exists because a reply said "Filter Coffee is already on its way with your
        Chicken Burgers in your first order" on a turn where the model wrote
        nothing and the book held only burgers. The coffee was never ordered and
        never would be; the guest would have waited for it. The book made that
        detectable - there is now one record to check a claim against - and this
        is the check.

        Returns [] when it claims nothing, or None when it cannot tell. Both mean
        "do not act", because rewriting a good reply is its own harm.
        """
        text = str(reply_text or "").strip()
        if not text:
            return None
        system_prompt = (
            "You are reading one message a hotel in-room dining assistant just sent "
            "a guest. List ONLY the dishes it tells the guest are already IN THEIR "
            "ORDER - ordered, added, placed, on the way, being prepared, in their "
            "cart. "
            "Return STRICT JSON: {\"claimed\":[\"<dish name as written>\", ...]}\n"
            "DO NOT list a dish that is merely offered, recommended, described, "
            "priced, listed as available, or declined. 'Would you like a Filter "
            "Coffee?' claims nothing. 'We have Tiramisu at 500' claims nothing. "
            "'Your Filter Coffee is on its way' DOES claim it. "
            "If it claims nothing, return an empty list."
        )
        try:
            raw = await self._chat_with_json(
                messages=[{"role": "system", "content": system_prompt},
                          {"role": "user", "content": text[:1200]}],
                model=(getattr(settings, "llm_judge_model", "") or settings.openai_model), temperature=0.0, max_tokens=160,
                trace_context={"responder_type": "main", "agent": "ird_reply_claims"},
            )
            if not isinstance(raw, dict) or not isinstance(raw.get("claimed"), list):
                return None
            return [str(x).strip() for x in raw["claimed"] if str(x or "").strip()]
        except Exception as exc:
            logger.warning("[IRD_BOOK] claim check failed (non-fatal): %s", exc)
            return None

    async def _ird_items_only_the_assistant_suggested(
        self,
        *,
        recent_turns: list[dict[str, str]],
        candidate_items: list[str],
    ) -> "list[str] | None":
        """Which of these items got into the order because the ASSISTANT named
        them, and never because the guest asked?

        Per ITEM, not per turn. A turn-level check is not enough: the model
        re-declares its whole running list every turn, so six dishes it had only
        SUGGESTED sailed in on the next turn — the one where the guest really did
        order two things, and the turn was legitimately an ordering turn. That is
        also how a guest who said "I'm allergic to dairy" ended up with an
        Arrabbiata Pasta on their ticket: the assistant had offered it two turns
        earlier and then quietly carried it forward.

        ASKS FOR THE OPPOSITE ANSWER TO THE OBVIOUS ONE, on purpose. Asking
        "which did the guest ask for?" and dropping the remainder made silence
        the reason to delete something, so an item whose ordering message had
        scrolled out of the window was thrown away — it removed food guests had
        really ordered three separate times, which is why enforcement was turned
        off. Asking "which did the ASSISTANT introduce?" makes deletion the thing
        that has to be positively established. Uncertainty now keeps the dish.

        Returns the names to drop (empty list = drop nothing), or None when it
        cannot tell, which also means drop nothing.
        """
        names = [str(n).strip() for n in candidate_items if str(n or "").strip()]
        if not names or not recent_turns:
            return None
        system_prompt = (
            "You are reading a hotel in-room dining chat. The assistant is about to put "
            "the listed items into the guest's order. Find the ones that are there ONLY "
            "because the assistant mentioned them.\n"
            "Return STRICT JSON: {\"assistant_suggested_only\":[\"<exact item name>\", ...]}\n"
            "List an item ONLY when BOTH are true: the assistant brought it up (a menu "
            "highlight, a recommendation, an answer to 'what's good' or 'what do you "
            "have', an offer like 'would you like to add X?'), AND the guest never took "
            "it up — no 'yes', no 'sure', no naming it themselves, anywhere in this "
            "conversation.\n"
            "DO NOT list an item the guest asked for in their own words at any point, "
            "even long ago, and even if their wording differs — 'tandoori murg' is "
            "Tandoori Murgh, 'naan x2' is 2 Garlic Naan. Match on what they meant.\n"
            "DO NOT list an item you are unsure about. If you cannot see clear evidence "
            "the assistant introduced it and the guest ignored it, leave it out. An item "
            "you leave out stays in the guest's order, which is the safe outcome: a "
            "stray line is visible on the confirmation and can be corrected, while "
            "deleting food someone actually ordered means it never arrives.\n"
            "Copy item names EXACTLY as given. If nothing qualifies, return an empty list."
        )
        try:
            raw = await self._chat_with_json(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",
                     "content": json.dumps(
                         {"conversation": recent_turns, "items": names}, ensure_ascii=False)},
                ],
                model=(getattr(settings, "llm_judge_model", "") or settings.openai_model),
                temperature=0.0,
                max_tokens=300,
                trace_context={"responder_type": "main", "agent": "ird_item_provenance"},
            )
            if not isinstance(raw, dict) or not isinstance(
                raw.get("assistant_suggested_only"), list
            ):
                return None
            drop = {
                str(x).strip().lower()
                for x in raw["assistant_suggested_only"] if str(x or "").strip()
            }
            return [n for n in names if n.lower() in drop]
        except Exception as exc:
            logger.warning("_ird_items_the_guest_asked_for failed (non-fatal): %s", exc)
            return None

    async def _ird_guest_wants_a_separate_order(
        self,
        *,
        user_message: str,
        open_order_items: list[dict[str, Any]],
    ) -> "bool | None":
        """Is the guest starting a SEPARATE order, or adding to the open one?

        Both are ordinary things to say and only the guest knows which they mean:
        "also send a coke" joins the order they just placed, "I want to place a
        new order" does not. Getting it wrong in one direction sends the kitchen
        two tickets and two trips for one meal; in the other it quietly shows a
        guest a total that includes food they have already been served, which is
        the bug this exists to fix.

        Deliberately one message, one question. The ordering agent is already
        juggling the menu, the cart and the reply, and a field buried inside that
        answer is not steady enough to decide something the guest gets charged for.

        Returns True (separate), False (same order), or None when it cannot tell.
        None means SAME ORDER — that is the existing behaviour and the safer
        default: a wrongly merged order is visible to the guest on the
        confirmation and can still be corrected, while a wrongly split one has
        already reached the kitchen as two tickets.
        """
        msg = str(user_message or "").strip()
        if not msg:
            return None
        listed = ", ".join(
            f"{int(i.get('qty') or 1)} x {str(i.get('item') or i.get('name') or '').strip()}"
            for i in (open_order_items or [])
            if isinstance(i, dict) and str(i.get("item") or i.get("name") or "").strip()
        )
        system_prompt = (
            "A hotel guest already has a food order with the kitchen. Below is "
            "everything they have said SINCE that order went in, oldest first, "
            "separated by ' | '. Decide ONE thing: across those messages, are they "
            "starting a SEPARATE new order, or adding to the order they already have?"
            "\n"
            "Guests split this over two turns - they announce the new order first "
            "and name the dishes after - so judge the messages TOGETHER. An earlier "
            "'I want to place a new order' still counts even when the latest message "
            "is only food."
            "\n"
            "They just as often say BOTH IN ONE BREATH: 'I want to place a new order "
            "- 2 idli', 'new order, one dosa please', 'can I do a separate order of "
            "coffee'. That is STILL a separate order. Dishes named alongside the "
            "announcement are the CONTENTS of the new order; they do not turn it into "
            "an addition, so do not let food appearing in the same sentence pull you "
            "to false. What decides it is whether a FRESH order is described anywhere "
            "in these messages - not whether they also said what they want in it."
            "\n"
            "Return STRICT JSON: {\"separate_order\":bool,\"why\":\"<a few words>\"}"
            "\n"
            "separate_order=true ONLY when they say so in some form - 'a new order', "
            "'another order', 'separate order', 'place a second order', 'order again'. "
            "The word 'new' is not required; what matters is that they are describing a "
            "FRESH order rather than more of the current one."
            "\n"
            "separate_order=false when they are adding to what they already have - "
            "'also send X', 'add a coke', 'and 2 naan', 'can you include Y', or they "
            "simply name more food with no framing at all. Naming a dish on its own is "
            "NOT a new order."
            "\n"
            "THE VERB 'ORDER' IS NOT AN ANNOUNCEMENT. 'can I order the continental "
            "breakfast', 'I want to order a dosa', 'order me a coffee', 'can I order "
            "this' are people asking for FOOD, using the ordinary word for it. They "
            "say nothing about a second order and must be false. What makes it true is "
            "a word that COUNTS the order - new, another, second, separate, again - "
            "not the verb on its own. Reading 'can I order X' as a fresh order closed a "
            "guest's open order while they were merely asking whether a dish was "
            "available, and the dish turned out to be off-hours anyway."
            "\n"
            "ASKING IS NOT ORDERING EITHER. 'do you have X', 'is X available', 'what "
            "time is X served', 'how much is X' are questions. Whatever the answer, "
            "they are not starting an order - false."
            "\n"
            "false is the safe answer. Adding to an order the guest can still see and "
            "correct costs them nothing; splitting one they meant to add to sends the "
            "kitchen a second ticket for the same meal. When unsure, answer false."
        )
        try:
            raw = await self._chat_with_json(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(
                        {"guest_said": msg[:400], "their_current_order": listed},
                        ensure_ascii=False)},
                ],
                model=(getattr(settings, "llm_judge_model", "") or settings.openai_model),
                temperature=0.0,
                max_tokens=60,
                trace_context={"responder_type": "main", "agent": "ird_separate_order"},
            )
            if not isinstance(raw, dict) or "separate_order" not in raw:
                return None
            return bool(raw.get("separate_order"))
        except Exception as exc:
            logger.warning("_ird_guest_wants_a_separate_order failed (non-fatal): %s", exc)
            return None

    async def _sarovar_ird_apply_order_state(
        self,
        *,
        context: "ConversationContext",
        decision: "OrchestrationDecision",
        menu_text: str,
    ) -> None:
        """Read the model's declared `order_items`, drop fully-off-menu items, store
        the validated order as `context.pending_data['ird_order']`, and — when the
        model is placing the order — rebuild the ticket issue from that validated
        order so the kitchen ticket can never contain a declined/invented item."""
        try:
            pdu = decision.pending_data_updates if isinstance(decision.pending_data_updates, dict) else {}
            raw_items = pdu.get("order_items")
            # ── THE GUEST SAID YES AND NOTHING WAS WRITTEN DOWN ──────────────
            # Only when the model emitted no items at all - which is exactly the
            # turn this goes wrong on. It offered a Tiramisu, the guest said "yes
            # and can i also get 2 towels", and no order_items came back: the
            # book stayed at 2 burgers, the reply (rendered FROM the book)
            # offered a dessert again, and the ticket planner raised a kitchen
            # ticket for a dish that existed in no order. Three parties, three
            # different answers, from one missing write.
            #
            # Filling it in HERE rather than at ticket time is deliberate: this
            # runs before the reply is finalised, so the cart, the reply and the
            # ticket all come from the same corrected order. Everything below -
            # off-menu validation, the veg/non-veg guard, pricing - still runs on
            # it exactly as if the model had written it itself.
            if not (isinstance(raw_items, list) and raw_items):
                try:
                    _last_bot = self._last_message_by_role(context, "assistant")
                    _accepted = await self._dishes_the_guest_just_accepted(
                        last_bot_message=str(_last_bot or ""),
                        guest_message=str(
                            self._last_message_by_role(context, "user") or ""),
                    )
                    if _accepted:
                        logger.info(
                            "[IRD_BOOK] the guest accepted %s but the model wrote "
                            "nothing — writing it to the cart",
                            [a["item"] for a in _accepted])
                        raw_items = _accepted
                        pdu["order_items"] = _accepted
                except Exception as _acc_exc:
                    logger.warning("[IRD_BOOK] accepted-offer recovery skipped "
                                   "(non-fatal): %r", _acc_exc)
            menu_words = self._ird_menu_word_set(menu_text)
            ticket = getattr(decision, "ticket", None)
            # ── THE MODEL CONTRADICTED ITSELF; BELIEVE THE DECISION ──────────
            # Seen on a live placement turn: {"action": "create_ticket",
            # "ticket": {"required": false}}. Create the ticket, and no ticket
            # required, in the same object. The ticket wins downstream, so
            # nothing was raised - while the reply told the guest their order was
            # "placed and on its way". The guest said "place it", then "yes",
            # then "yes" again, and the kitchen never heard.
            #
            # `action` is the decision; the ticket block is how it gets carried
            # out. When they disagree and there is genuinely something to place,
            # the decision stands.
            if (ticket is not None
                    and str(getattr(decision, "action", "") or "").strip().lower()
                    == "create_ticket"
                    and not getattr(ticket, "required", False)
                    and (context.pending_data or {}).get("_ird_cart")):
                ticket.required = True
                ticket.ready_to_create = True
                logger.warning(
                    "[SAROVAR_IRD] action=create_ticket but ticket.required was "
                    "false - honouring the decision so the order really goes")
            _ready = bool(ticket is not None and ticket.required and ticket.ready_to_create)
            if not isinstance(context.pending_data, dict):
                return
            room = str((context.pending_data or {}).get("room_number") or "").strip()
            newly: list[str] = []

            # (a) The model declared a structured order this turn — revalidate it against
            # the menu and refresh the durable in-progress cart. Skipped when the model
            # declared nothing, or there is no menu text to validate against (fail safe:
            # never drop an item we cannot check).
            if isinstance(raw_items, list) and menu_words:
                # ── IS THE ORDER ALREADY PLACED STILL OPEN TO CHANGES? ────────
                # The cart deliberately survives a placement so the guest can add
                # to the SAME order. That only holds while the ticket behind it is
                # still `open` and inside the edit window — once staff pick it up
                # (in progress / closed / cancelled) or the window lapses, the
                # kitchen is committed and anything further is a NEW order. Clear
                # the cart at exactly that moment so the next dish starts clean
                # instead of resending food already being cooked.
                # Keyed off whether ANY food ticket of this session is still open
                # and inside the window — not off a remembered ref, which did not
                # survive between turns and so never locked anything.
                # ── THE GUEST ASKED FOR A SEPARATE ORDER ─────────────────────
                # The cart deliberately survives a placement so more food joins
                # the SAME order and the kitchen makes one trip. That is right for
                # "oh, also send a coke" and wrong for "I want to place a new
                # order": the second order was folded into the first ticket and
                # the guest was shown a running total that included food already
                # on its way. Only the guest knows which they meant, so a single
                # narrow judgement is asked, and code carries out the split.
                #
                # Judged on everything the guest has said SINCE that order was
                # placed, not on the latest message alone. They announce the new
                # order and name the food in separate turns — "I want to place a
                # new order", then "1 idli and 1 juice" — and this only runs on the
                # turn the dishes arrive, where the last message is just food and
                # reads as an ordinary addition. Bounded by the placement time, so
                # once the split happens the trigger cannot be seen again.
                #
                # Starting a new order is the same transition that already happens
                # when the kitchen window lapses (just below): cart, vetting and
                # the open-order pointer are dropped so the next dish starts clean.
                # The one addition is remembering the ref we have moved on from,
                # because the ticket lookup searches the database for any recent
                # open food ticket and would otherwise find the order the guest
                # just closed and merge into it anyway.
                _wants_new_order = False
                if context.pending_data.get("_ird_order_placed_at"):
                    try:
                        from datetime import datetime as _dtx, timezone as _tzx

                        _cut = None
                        try:
                            _cut = _dtx.fromisoformat(
                                str(context.pending_data.get("_ird_order_placed_at"))
                                .replace("Z", "+00:00")
                            )
                            if _cut.tzinfo is None:
                                _cut = _cut.replace(tzinfo=_tzx.utc)
                        except (TypeError, ValueError):
                            _cut = None
                        _asks: list[str] = []
                        for _m in (context.get_recent_messages(16) or []):
                            _r = str(getattr(getattr(_m, "role", None), "value",
                                             getattr(_m, "role", ""))).lower()
                            if _r != "user":
                                continue
                            _txt = str(getattr(_m, "content", "") or "").strip()
                            if not _txt:
                                continue
                            _ts = getattr(_m, "timestamp", None)
                            if _cut is not None and _ts is not None:
                                try:
                                    if _ts.tzinfo is None:
                                        _ts = _ts.replace(tzinfo=_tzx.utc)
                                    if _ts <= _cut:
                                        continue
                                except (TypeError, AttributeError):
                                    pass
                            _asks.append(_txt)
                        if _asks:
                            _wants_new_order = bool(
                                await self._ird_guest_wants_a_separate_order(
                                    user_message=" | ".join(_asks[-4:]),
                                    open_order_items=list(
                                        context.pending_data.get("_ird_open_order_items") or []
                                    ),
                                )
                            )
                    except Exception as _sep_exc:
                        # Never let this cost a turn — silence here simply leaves
                        # the existing merge behaviour in place.
                        logger.warning("[SAROVAR_IRD] separate-order check skipped: %r", _sep_exc)
                        _wants_new_order = False
                if _wants_new_order:
                    _closed = [
                        str(r).strip()
                        for r in (context.pending_data.get("_ird_closed_order_refs") or [])
                        if str(r or "").strip()
                    ]
                    _leaving = str(context.pending_data.get("_ird_order_ticket_ref") or "").strip()
                    if _leaving and _leaving not in _closed:
                        _closed.append(_leaving)
                    context.pending_data["_ird_closed_order_refs"] = _closed[-10:]
                    context.pending_data["_ird_cart"] = []
                    context.pending_data.pop("_ird_vetted_items", None)
                    context.pending_data.pop("_ird_order_ticket_ref", None)
                    context.pending_data.pop("_ird_order_placed_at", None)
                    context.pending_data.pop("_ird_open_order_items", None)
                    # Everything in the cart from here was asked for AFTER this
                    # reset, so there is nothing stale to subtract from it. Without
                    # this, a guest whose new order repeats a dish from the last one
                    # ("another dosa") would have it cancelled out by the
                    # already-sent tally and receive nothing.
                    context.pending_data["_ird_fresh_order"] = True
                    logger.info(
                        "[SAROVAR_IRD] the guest asked for a separate order - closing "
                        "order %s and starting a clean cart", _leaving or "(unplaced)",
                    )
                    # What that order took with it. The model keeps these in view
                    # and will write them into the next cart unless the code
                    # refuses them, which is how a guest who asked for idli was
                    # shown - and nearly charged for - the dosa and coffee already
                    # on their way.
                    try:
                        _closed_names = {
                            str(_l.get("item") or "").strip().lower()
                            for _l in (_cur_cart or []) if isinstance(_l, dict)
                        }
                        _closed_names |= {
                            str(_i.get("name") or "").strip().lower()
                            for _r in (context.pending_data.get("_ird_placed_orders") or [])
                            if isinstance(_r, dict)
                            for _i in (_r.get("items") or []) if isinstance(_i, dict)
                        }
                        context.pending_data["_ird_closed_items"] = sorted(
                            n for n in _closed_names if n
                        )
                    except Exception:
                        pass
                if context.pending_data.get("_ird_order_placed_at") and not _wants_new_order:
                    try:
                        from services.ticketing_service import ticketing_service as _ts

                        _open = await _ts.find_recent_open_ticket(
                            session_id=str(getattr(context, "session_id", "") or ""),
                            category_markers=("dining", "food", "beverage"),
                            within_minutes=int(
                                getattr(settings, "ird_edit_window_minutes", 5) or 0
                            ),
                        )
                        _placed_ref = str((_open or {}).get("ticket_ref") or "").strip()
                        if not _placed_ref:
                            logger.info(
                                "[SAROVAR_IRD] the placed order is no longer editable — "
                                "starting a fresh cart for anything ordered now."
                            )
                            context.pending_data["_ird_cart"] = []
                            # A fresh cart also needs fresh vetting: what the guest
                            # asked for in the order that just closed says nothing
                            # about what they want now, and letting it vouch for a
                            # dish would put the earlier order's food back on the
                            # kitchen ticket unasked.
                            context.pending_data.pop("_ird_vetted_items", None)
                            context.pending_data.pop("_ird_order_ticket_ref", None)
                            context.pending_data.pop("_ird_order_placed_at", None)
                            context.pending_data.pop("_ird_open_order_items", None)
                    except Exception as _ed_exc:
                        logger.warning("[SAROVAR_IRD] editability check failed: %r", _ed_exc)

                # Recall guard: after an order is placed (cart empty, a placed order on
                # file), a status turn where the model merely re-lists the SAME placed
                # order — adding no genuinely new on-menu item — must NOT resurrect that
                # order as a pending cart. Leave the cart empty and let the model answer
                # the status from placed_orders.
                _placed_list = context.pending_data.get("_ird_placed_orders") or []
                _cur_cart = context.pending_data.get("_ird_cart") or []
                if _placed_list and not _cur_cart and not _ready:
                    # ── MORE OF THE SAME DISH IS NEW WORK ────────────────────
                    # This compared NAMES only, so "one more butter naan" on an
                    # order that already had 3 naan looked like the model simply
                    # re-listing that order, and the whole order pass returned
                    # here - silently, with no log. The naan was never written
                    # to the cart, never reached a ticket, and the guest was
                    # told twice that it had been added. Quantity is what tells
                    # a re-list from an addition: same dish, higher count, is a
                    # request. Re-listing repeats the SAME counts.
                    _last_qty: dict[str, int] = {}
                    for l in (_placed_list[-1].get("items") or []):
                        if not isinstance(l, dict):
                            continue
                        _n = str(l.get("item", "")).strip().lower()
                        if not _n:
                            continue
                        try:
                            _q = int(l.get("qty") or 1)
                        except (TypeError, ValueError):
                            _q = 1
                        _last_qty[_n] = _last_qty.get(_n, 0) + max(_q, 1)

                    def _is_new(_it: Any) -> bool:
                        if not isinstance(_it, dict):
                            return False
                        _nm = str(_it.get("name") or _it.get("item") or "").strip()
                        if not _nm or not self._ird_item_is_real(_nm, menu_words):
                            return False
                        _was = _last_qty.get(_nm.lower())
                        if _was is None:
                            return True          # a dish that order never had
                        try:
                            _now_q = int(_it.get("qty") or 1)
                        except (TypeError, ValueError):
                            _now_q = 1
                        return _now_q > _was     # more of it than was placed

                    _new_real = [it for it in raw_items if _is_new(it)]
                    if not _new_real:
                        # DIAGNOSTIC: this used to return with nothing in the log
                        # at all, which is why a lost dish looked like the order
                        # pass had simply never run.
                        logger.info(
                            "[SAROVAR_IRD] recall guard: the model re-listed the "
                            "placed order with no added quantity (%r) — leaving "
                            "the cart empty",
                            [str(i.get("name") or i.get("item") or "")
                             for i in raw_items if isinstance(i, dict)][:6],
                        )
                        self._ird_sync_pending_updates(context, decision)
                        return
                # Guest's own recent words, for the veg/non-veg safety net below.
                veg_mismatch: list[str] = []
                closed_now: list[dict[str, str]] = []
                try:
                    _recent_msgs = context.get_recent_messages(20) or []
                except Exception:
                    _recent_msgs = []
                _guest_words_blob = " ".join(
                    str(getattr(m, "content", "") or "").lower()
                    for m in _recent_msgs
                    if str(getattr(getattr(m, "role", None), "value", getattr(m, "role", ""))).lower() == "user"
                )
                _guest_said_veg = bool(re.search(r"\b(paneer|vegetarian|veg|tofu)\b", _guest_words_blob))
                cart_new: list[dict[str, Any]] = []
                dropped: list[str] = []
                for it in raw_items:
                    if not isinstance(it, dict):
                        continue
                    name = str(it.get("name") or it.get("item") or "").strip()
                    if not name:
                        continue
                    if not self._ird_item_is_real(name, menu_words):
                        dropped.append(name)
                        continue
                    # ── ON THE MENU, BUT NOT AT THIS HOUR ────────────────────
                    # Kept strictly apart from `dropped` above. That list means "we
                    # do not serve this at all"; this one means "we serve it, just
                    # not now" — and answering the second with the first tells a
                    # guest the hotel has no idli, which is both untrue and the kind
                    # of thing they stop asking about. Different list, different
                    # sentence. Resolved per dish in code so the model never has to
                    # compare a clock to a section note it had to guess at.
                    _av = self._ird_dish_availability(context, name)
                    if not _av.get("available", True):
                        closed_now.append({
                            "item": name,
                            "served": str(_av.get("served") or ""),
                            "returns": str(_av.get("next_opens") or ""),
                        })
                        continue
                    # VEG/NON-VEG SAFETY NET: _ird_item_is_real can't catch a
                    # real-but-WRONG substitution (the model resolving "paneer butter
                    # masala" -> "Butter Chicken" on the shared word "butter"; the
                    # substitute IS on the menu). If this line is a non-veg dish, the
                    # guest asked for vegetarian, and they never named this non-veg item
                    # themselves, drop it so the wrong dish can never reach the kitchen.
                    _nlow = name.lower()
                    if (
                        _guest_said_veg
                        and any(w in _nlow for w in self._IRD_NONVEG_WORDS)
                        and not any(w in _guest_words_blob for w in self._IRD_NONVEG_WORDS if w in _nlow)
                    ):
                        veg_mismatch.append(name)
                        continue
                    try:
                        qty = int(float(it.get("qty") or it.get("quantity") or 1))
                    except (TypeError, ValueError):
                        qty = 1
                    # A quantity of zero is how the model says the guest removed
                    # this item — it appeared as "2 Garlic Naan, 0 Tiramisu" on a
                    # kitchen ticket after a cancellation. Rounding that up to 1
                    # (which is what max(1, qty) did) cooks and bills a dish the
                    # guest had just cancelled, so drop the line instead.
                    if qty <= 0:
                        logger.info(
                            "[SAROVAR_IRD] %r came back with qty %s - the guest removed "
                            "it, so it is dropped rather than rounded up to 1", name, qty,
                        )
                        continue
                    line = {"item": name, "qty": qty}
                    # Carry the model's stated price/allergens for the item when present
                    # (menu-grounded already); the menu remains the single source.
                    for k_src, k_dst in (("price", "price"), ("unit_price", "price"),
                                        ("allergens", "allergens"),
                                        # WHICH VERSION THEY ASKED FOR. Menu items
                                        # like "Phulka (Plain or Ghee)" are one dish
                                        # with options; the model names the dish and
                                        # puts the option here. Dropped at this line,
                                        # the reply said "2 x Phulka (Ghee)" and the
                                        # kitchen ticket said "Phulka" - the guest
                                        # gets the plain bread they did not order.
                                        ("variant", "variant"), ("choice", "variant")):
                        v = it.get(k_src)
                        if v not in (None, "") and k_dst not in line:
                            line[k_dst] = v
                    cart_new.append(line)
                # ── SUGGESTING IS NOT ORDERING ───────────────────────────────
                # `order_items` is the cart, and the model writes to it whenever it
                # talks about food — including when it is only ANSWERING a question.
                # Asked "whats good for dinner" it replied with the whole Midnight
                # Menu, wrote all nine dishes into order_items and set
                # pending_action=confirm_booking. Every check below passes them (they
                # are real menu items at real prices), the cart is then treated as the
                # authoritative record of what to cook, and a guest who ordered two
                # things was sent nine.
                #
                # Checked per ITEM, not per turn: the model re-declares its whole
                # running list every turn, so a turn-level check let all nine back in
                # on the next turn — the one where the guest really was ordering. Only
                # NEW arrivals are judged; anything already in the cart is untouched.
                # The judge reads the conversation rather than matching words, and on
                # any uncertainty it keeps the item, because losing food a guest asked
                # for is worse than a stray line they can still correct.
                # An item that has ALREADY passed this check must never face it
                # again. The judge only sees a window of recent messages, so after
                # a detour (the guest reports a broken AC mid-order, we hand off
                # and come back) the original "one tandoori murg and 2 garlic
                # naan" has scrolled out of view — and the cart is rebuilt from
                # scratch on the way back, so those items look new. The judge then
                # answers honestly for what it can see and DELETES food the guest
                # really did order. Remembering what has been vetted keeps that
                # decision made once, on the turn where the evidence is present.
                _vetted = {
                    str(x).strip().lower()
                    for x in (context.pending_data.get("_ird_vetted_items") or [])
                    if str(x or "").strip()
                }
                # ── A CLOSED ORDER STAYS CLOSED ──────────────────────
                # Items from an order the guest has already been served only
                # belong in this cart if they asked for them AGAIN. Their own
                # words decide it: a fresh "another masala dosa" is honoured,
                # while the model re-listing yesterday's order is not.
                _closed = {
                    str(x).strip().lower()
                    for x in (context.pending_data.get("_ird_closed_items") or [])
                    if str(x or "").strip()
                }
                # ── FEED THIS GUARD FROM THE BOOK ────────────────────────────
                # It only ever knew about closed orders through _ird_closed_items,
                # which the book path does not maintain - so it read an empty set
                # and dropped nothing. A guest asking for a NEW order of pasta got
                # a second order carrying the pasta AND the two burgers already
                # being cooked, because the model re-declares its whole running
                # list every turn. The book knows exactly what is already with the
                # kitchen; hand it over and the existing filter works unchanged.
                if bool(getattr(settings, "ird_order_book_enabled", False)):
                    try:
                        from services import ird_cart_book as _bk0
                        from services import ird_cart_store as _bs0

                        _row0 = await _bs0.load(
                            hotel_code=str(getattr(context, "hotel_code", "") or ""),
                            session_id=str(getattr(context, "session_id", "") or ""),
                            create=False)
                        if _row0:
                            # REPLACE, do not add to, the old list. _ird_closed_items
                            # accumulates and is never cleared, so a dish sitting in
                            # an order that is still ONGOING was listed as already
                            # placed and dropped - the guest's Tiramisu, waiting to
                            # be sent, was deleted as a duplicate of itself. The
                            # book knows which orders really went to the kitchen.
                            _closed = {
                                str(_it0.get("item", "")).strip().lower()
                                for _sl0 in _bk0.slots_from_row(_row0)
                                if _sl0.get("status") in (_bk0.PLACED, _bk0.PROCESSING,
                                                          _bk0.DELIVERED)
                                for _it0 in (_sl0.get("items") or [])
                                if str(_it0.get("item", "")).strip()
                            }
                    except Exception as _c_exc:
                        logger.warning("[IRD_BOOK] could not read closed items from "
                                       "the book (non-fatal): %r", _c_exc)
                # DIAGNOSTIC: a guard that only logs when it FIRES is
                # indistinguishable from one that never runs. On the 50-turn run
                # this dropped nothing while a second ticket carried the first
                # order's burgers, and the log could not say which of the two it
                # was. Logged unconditionally so the next run can tell.
                logger.info(
                    "[SAROVAR_IRD] closed-order filter: closed_items READ=%r, "
                    "cart lines=%r, fresh=%r",
                    sorted(_closed),
                    [str(l.get("item") or "") for l in cart_new],
                    context.pending_data.get("_ird_fresh_order"),
                )
                if _closed:
                    # ONLY WHAT THEY JUST SAID. This searched EVERY recent guest
                    # message, so a dish stayed "asked for again" forever: the guest
                    # named Chicken Burger when they first ordered it, and from then
                    # on that word sat in the history and re-admitted it to every
                    # later cart. Asking for a separate order of pasta produced a
                    # six-item ticket carrying the burgers and coffees already with
                    # the kitchen. Asking again means asking again NOW.
                    _last_user = ""
                    for m in reversed(_recent_msgs or []):
                        _role = str(getattr(getattr(m, "role", None), "value",
                                            getattr(m, "role", ""))).lower()
                        if _role == "user":
                            _last_user = str(getattr(m, "content", "") or "")
                            break
                    _said = _last_user.lower()
                    _kept = []
                    for _line in cart_new:
                        _nm = str(_line.get("item") or "").strip().lower()
                        if _nm and _nm in _closed and _nm not in _said:
                            logger.info(
                                "[SAROVAR_IRD] %r belongs to an order already placed and "
                                "the guest has not asked for it again - keeping it out of "
                                "the new order", _line.get("item"),
                            )
                            continue
                        _kept.append(_line)
                    cart_new = _kept

                _prev_names = {
                    str(l.get("item", "")).strip().lower()
                    for l in (_cur_cart or []) if isinstance(l, dict)
                } | _vetted
                _incoming_new = [
                    l for l in cart_new
                    if str(l.get("item", "")).strip().lower() not in _prev_names
                ]
                # Enforcement is OFF (it deleted food guests had really ordered), and
                # the verdict below is discarded when it is off - so ASK FIRST whether
                # anyone will act on the answer. The flag used to be read after the
                # call, which billed a gpt-4o-mini round trip on every turn that added
                # an item, purely to log a line and throw it away.
                if _incoming_new and bool(getattr(
                    settings, "ird_provenance_refusal_enabled", False
                )):
                    _turns: list[dict[str, str]] = []
                    for m in _recent_msgs[-16:]:
                        _role = str(getattr(getattr(m, "role", None), "value",
                                            getattr(m, "role", ""))).lower()
                        _txt = str(getattr(m, "content", "") or "").strip()
                        if _txt:
                            _turns.append({
                                "who": "guest" if _role == "user" else "assistant",
                                "said": _txt[:700],
                            })
                    _suggested = await self._ird_items_only_the_assistant_suggested(
                        recent_turns=_turns,
                        candidate_items=[str(l.get("item", "")) for l in _incoming_new],
                    )
                    if _suggested:
                        # ONLY WHAT WAS POSITIVELY IDENTIFIED. The judge now names
                        # the dishes it can show the assistant introduced and the
                        # guest never took up; everything else — including anything
                        # it was unsure about — stays in the order. Silence is no
                        # longer a reason to delete food, which is what made the
                        # earlier version unsafe to switch on.
                        _drop = {a.strip().lower() for a in _suggested}
                        _refused = [
                            str(l.get("item"))
                            for l in _incoming_new
                            if str(l.get("item", "")).strip().lower() in _drop
                        ]
                        if _refused and not bool(getattr(
                            settings, "ird_provenance_refusal_enabled", False
                        )):
                            logger.warning(
                                "[SAROVAR_IRD] provenance WOULD have dropped %s "
                                "(enforcement off) - cart left intact", _refused,
                            )
                            _refused = []
                        if _refused:
                            logger.info(
                                "[SAROVAR_IRD] the guest never asked for %s - refusing "
                                "them; keeping %s",
                                _refused,
                                [l.get("item") for l in cart_new
                                 if str(l.get("item", "")).strip().lower()
                                 not in _drop],
                            )
                            # KEEP EVERY LINE THE JUDGE DID NOT NAME. The name
                            # tested here used to hold the guest-named whitelist
                            # and no longer exists, so this raised NameError - and
                            # the except around the whole post-process turned that
                            # into the cart never being written at all. A guest was
                            # told "I have added 1 Tiramisu" and the kitchen ticket
                            # went out with only the burgers. `_drop` is scoped to
                            # the incoming candidates, so lines already in the cart
                            # survive this filter by construction.
                            cart_new = [
                                l for l in cart_new
                                if str(l.get("item", "")).strip().lower() not in _drop
                            ]
                            # The model wrote its message BEFORE we refused anything,
                            # so it is still showing the guest an "order summary"
                            # listing dishes that will not be ordered. Leaving that
                            # in place means the guest reads one order and receives
                            # another. When its text names a dish we just refused,
                            # replace it with the summary built from the validated
                            # cart — the same server-authoritative render used at
                            # checkout, so what they see is what they get.
                            # NEVER rewrite the message on the turn that actually
                            # places the order. That reply is a confirmation —
                            # "Done, your order is on its way" — and the summary
                            # ends by ASKING whether to place it, so swapping one
                            # for the other told a guest whose order had just gone
                            # to the kitchen that nothing had happened yet, and
                            # invited them to order it a second time.
                            _placing = bool(
                                ticket is not None
                                and getattr(ticket, "required", False)
                                and getattr(ticket, "ready_to_create", False)
                            )
                            _txt = str(getattr(decision, "response_text", "") or "")
                            if _placing:
                                logger.info(
                                    "[SAROVAR_IRD] not rewriting the reply on the "
                                    "placement turn - it is a confirmation, not a summary",
                                )
                            elif _txt and any(r.lower() in _txt.lower() for r in _refused):
                                decision.response_text = self._render_ird_summary(
                                    cart_new, room=room,
                                    order_no=self._ird_display_order_no(context.pending_data),
                                )
                                logger.info(
                                    "[SAROVAR_IRD] the reply still listed refused items - "
                                    "rebuilt it from the validated cart (%d item(s))",
                                    len(cart_new),
                                )
                # ── THE ORDER BOOK, WHEN IT IS SWITCHED ON ───────────────
                # cart_new has already been through every check above - off-menu
                # items dropped, serving times applied, veg mismatches caught,
                # provenance settled - so this writes VALIDATED lines and nothing
                # else. The book then regenerates _ird_cart and its siblings, so
                # the hundred-odd readers below and downstream keep working while
                # they stop being separately maintained copies.
                #
                # Why here rather than instead of this function: the validation
                # above is the reason a declined dish never reaches the kitchen.
                # The book replaces where the order is KEPT, not what is allowed
                # into it.
                _book_written = False
                if bool(getattr(settings, "ird_order_book_enabled", False)):
                    try:
                        from services import ird_cart_book as _bk
                        from services import ird_cart_store as _bs

                        _hc = str(getattr(context, "hotel_code", "") or "")
                        _sid = str(getattr(context, "session_id", "") or "")
                        _win = int(getattr(settings, "ird_edit_window_minutes", 5) or 0)
                        # the hotel's clock, not the server's - see
                        # ird_cart_book.now_local
                        _now_bk = _bk.now_local()
                        _row = await _bs.load(hotel_code=_hc, session_id=_sid,
                                              guest={"room_number": room})
                        if _row:
                            _slots = _bk.slots_from_row(_row)
                            # ── WHICH ORDER DID THEY MEAN? ASK, DON'T GUESS ──
                            # Only when the guest has two or more orders open AND
                            # the model did not name one. The code's fallback
                            # guesses from the dish list, which cannot see the one
                            # thing that settles it - "another order for my
                            # husband". Whatever comes back is fed in as the
                            # model's own `order_slot`, so everything downstream
                            # is unchanged; "" leaves the old behaviour exactly.
                            _slot_said = str(pdu.get("order_slot") or "").strip().lower()
                            if not _slot_said:
                                _live_now = [sl for sl in _slots if sl.get("items")]
                                if len(_live_now) >= 2:
                                    _said = ""
                                    for _m2 in reversed(
                                            context.get_recent_messages(8) or []):
                                        _r2 = str(getattr(
                                            getattr(_m2, "role", None), "value",
                                            getattr(_m2, "role", ""))).lower()
                                        if _r2 != "user":
                                            continue
                                        _said = str(getattr(_m2, "content", "") or "").strip()
                                        break
                                    _asked = await self._which_order_does_the_guest_mean(
                                        guest_message=_said,
                                        orders=[{"slot": sl["n"],
                                                 "status": sl.get("status"),
                                                 "items": sl.get("items")}
                                                for sl in _live_now])
                                    if _asked:
                                        logger.info(
                                            "[IRD_BOOK] guest has %d orders open and the "
                                            "model named none - they meant %r",
                                            len(_live_now), _asked)
                                        pdu["order_slot"] = _asked
                            _slots, _changed = _bk.apply_write(
                                _slots,
                                {"items": cart_new,
                                 # WHICH order the model means. It had no way to
                                 # say before, so it could not express "put this
                                 # in a NEW order" and asked the guest instead -
                                 # then wrote nothing at all. "new" leaves slot
                                 # unset so a fresh one is opened.
                                 **({} if str(pdu.get("order_slot") or "").strip().lower()
                                    in ("", "new") else {"slot": pdu.get("order_slot")}),
                                 # Dishes the guest asked to DROP, named by the
                                 # model. Absence can never remove anything - see
                                 # apply_write - so this is the only way a dish
                                 # comes off an order, and the only reason
                                 # "actually drop the medu vada" now works.
                                 "remove": (pdu.get("order_remove")
                                            if isinstance(pdu.get("order_remove"), list)
                                            else []),
                                 "status": _bk.PLACED if _ready else _bk.ONGOING},
                                now=_now_bk, window_minutes=_win,
                                # The guest asking for a SEPARATE order is already
                                # decided upstream; without it a new order joins
                                # the open one and the two get merged.
                                # ── A FRESH ORDER IS OPENED ONCE, NOT EVERY TURN
                                # `_ird_fresh_order` is only cleared when an order
                                # is COMMITTED as placed, so every turn between
                                # the split and the placement asked for another
                                # new slot. The guest's 2 chai went into slot 2 as
                                # ongoing, then their "yes" opened slot 3 and
                                # placed THAT - leaving slot 2 orphaned, which in
                                # turn made the planner treat the session as
                                # having an unplaced cart and drop the food ticket
                                # entirely. Once the fresh order exists in the
                                # book (ongoing, no ticket), keep writing to it.
                                prefer_new=bool(
                                    (context.pending_data.get("_ird_fresh_order")
                                     and not any(
                                         sl.get("items")
                                         and sl.get("status") == _bk.ONGOING
                                         and not str(sl.get("ticket_ref") or "").strip()
                                         for sl in _slots))
                                    or str(pdu.get("order_slot") or "").strip().lower()
                                    == "new"),
                            )
                            if _changed:
                                await _bs.save_slots(
                                    hotel_code=_hc, session_id=_sid,
                                    slots=[sl for sl in _slots if sl["n"] in _changed])
                                _bk.project_to_pending_data(_slots, context.pending_data)
                                # THE TICKET FOR THIS TURN IS STILL BEING BUILT.
                                # The projection keeps only `ongoing` slots in
                                # _ird_cart, which is right for the NEXT turn's
                                # reads but empties the list this turn's ticket is
                                # composed from - so a placement fell back to the
                                # model's own wording, and a dish added to an
                                # order already with the kitchen never reached its
                                # ticket at all. Whatever the book just wrote is
                                # what this turn has to act on.
                                context.pending_data["_ird_cart"] = cart_new
                                _book_written = True
                                # ── DO NOT ASK FOR WHAT IS ALREADY DONE ──────────
                                # The model kept closing on "shall I place this
                                # updated order?" for an order the book says is
                                # already with the kitchen, and "shall I add the
                                # Filter Coffee?" for a dish it had just written
                                # into that order. Three separate prompt rules say
                                # not to and it still did, so the book answers it
                                # instead of asking. A genuine offer of something
                                # NEW is left alone.
                                _fixed = _bk.fix_stale_closing(
                                    str(getattr(decision, "response_text", "") or ""),
                                    _slots)
                                if _fixed:
                                    logger.info(
                                        "[IRD_BOOK] dropped a closing question the "
                                        "book had already answered")
                                    decision.response_text = _fixed
                                # ── THE MONEY IS THE BOOK'S, NOT THE MODEL'S ────
                                # It was given every line total and the order
                                # total and told not to add anything up, and still
                                # announced ₹2,825 for an order of ₹2,325 -
                                # intermittently, which is worse than always,
                                # because the next total was right. Arithmetic is
                                # not something to ask for and hope for. Whatever
                                # number it typed, the book's figure replaces it.
                                _money = _bk.fix_totals(
                                    str(getattr(decision, "response_text", "") or ""),
                                    _slots)
                                if _money:
                                    logger.info(
                                        "[IRD_BOOK] corrected a total the model got "
                                        "wrong - the book's figure now stands")
                                    decision.response_text = _money
                                # ── THE TICKET FOLLOWS THE BOOK, AUTOMATICALLY ──
                                # If a slot already has a ticket and its contents
                                # changed, the kitchen's copy is now stale. Left to
                                # the model to trigger, it did not: the book held
                                # "2 Chicken Burger, 1 Filter Coffee" while ticket
                                # LOCAL-133 still read "2 Chicken Burgers", and the
                                # guest had been told the coffee was added. The
                                # book is the source, so the ticket is rewritten
                                # from it here rather than when the model happens
                                # to ask for it.
                                for _sl in _slots:
                                    if _sl["n"] not in _changed:
                                        continue
                                    _ref_now = str(_sl.get("ticket_ref") or "").strip()
                                    if not _ref_now or _sl.get("status") not in (
                                        _bk.PLACED, _bk.PROCESSING
                                    ):
                                        continue
                                    _desc = _bk.ticket_description(_sl, room=room)
                                    try:
                                        from services.ticketing_service import (
                                            ticketing_service as _ts,
                                        )

                                        _res = await _ts.update_ticket_details(
                                            session_id=str(getattr(
                                                context, "session_id", "") or ""),
                                            ticket_ref=_ref_now,
                                            description=_desc,
                                            # The dashboard ignores `issue` on an
                                            # update, so the dish list has to
                                            # travel in the note or the kitchen
                                            # keeps cooking the old order.
                                            # ASCII only: the dashboard stores
                                            # manager_notes in a latin-1 column
                                            # and an em-dash arrives as a
                                            # replacement character.
                                            note=("Order updated by the guest - it "
                                                  "now reads: %s" % _desc),
                                        )
                                        logger.info(
                                            "[IRD_BOOK] ticket %s rewritten from slot "
                                            "%d: %s (ok=%s)", _ref_now, _sl["n"],
                                            _desc[:90], getattr(_res, "success", None))
                                    except Exception as _tx:
                                        logger.warning(
                                            "[IRD_BOOK] could not update ticket %s "
                                            "from the book (non-fatal): %r",
                                            _ref_now, _tx)
                                logger.info(
                                    "[IRD_BOOK] wrote %d line(s) to slot %s (%s)",
                                    len(cart_new), _changed,
                                    "placed" if _ready else "ongoing")
                            else:
                                logger.warning(
                                    "[IRD_BOOK] refused the write - every slot is a "
                                    "live order, or the target is already cooking")
                    except Exception as _bk_exc:
                        # Fails open to the old key: a book we cannot write must
                        # never be the reason an order is lost.
                        logger.warning("[IRD_BOOK] write failed, falling back to "
                                       "pending_data (non-fatal): %r", _bk_exc)
                        _book_written = False
                if not _book_written:
                    context.pending_data["_ird_cart"] = cart_new  # durable in-progress cart
                # Remember what has been accepted into the order, so a later turn
                # never re-litigates it against a window that no longer holds the
                # message where the guest asked for it.
                _keep_vetted = {
                    str(l.get("item", "")).strip()
                    for l in cart_new if str(l.get("item") or "").strip()
                }
                if _keep_vetted:
                    context.pending_data["_ird_vetted_items"] = sorted(
                        {str(x) for x in (context.pending_data.get("_ird_vetted_items") or [])}
                        | _keep_vetted
                    )
                # Track which off-menu items we've already told the guest about, so we
                # decline a NEW one but don't nag about a re-listed one.
                declined_seen = {str(x).lower() for x in (context.pending_data.get("_ird_declined") or [])}
                newly = [d for d in dropped if str(d).lower() not in declined_seen]
                for d in dropped:
                    declined_seen.add(str(d).lower())
                if dropped:
                    context.pending_data["_ird_declined"] = sorted(declined_seen)
                    logger.info("[SAROVAR_IRD] dropped off-menu item(s): %s (new: %s)", dropped, newly)
                    decision.metadata["ird_dropped_off_menu"] = dropped[:6]

                # ── "WE SERVE THAT — JUST NOT RIGHT NOW" ─────────────────────
                # Written HERE, in code, rather than left to the model, because the
                # one thing this must never turn into is "we don't have that". The
                # dish is on the menu; the guest is told so, given the hours and the
                # time it returns, and offered something else. Anything else they
                # ordered on the same turn stays in the cart — a single closed dish
                # must not throw away the rest of an order.
                if closed_now:
                    decision.metadata["ird_closed_now"] = closed_now[:6]
                    logger.info(
                        "[SAROVAR_IRD] on the menu but outside its serving window: %s",
                        [c["item"] for c in closed_now],
                    )
                    _parts = []
                    for _c in closed_now:
                        _line = f"**{_c['item']}**"
                        if _c.get("served"):
                            _line += f" is served {_c['served']}"
                        if _c.get("returns"):
                            _line += f", so it'll be available again {_c['returns']}"
                        _parts.append(_line)
                    _resp = (
                        "We do serve "
                        + " and ".join(c["item"] for c in closed_now)
                        + " — it's just outside its serving time right now.\n\n"
                        + "\n".join(f"• {p}" for p in _parts)
                    )
                    if cart_new:
                        _resp += (
                            "\n\nThe rest of your order is still here:\n"
                            + self._render_ird_cart_lines(cart_new)
                            + "\n\nWould you like to add something else in the meantime, "
                            "or shall I place what you have?"
                        )
                    else:
                        _resp += "\n\nCan I get you something else from the menu in the meantime?"
                    decision.response_text = _resp
                    decision.action = "respond_only"
                    if ticket is not None:
                        ticket.required = False
                        ticket.ready_to_create = False
                    decision.pending_action = ""
                    self._ird_sync_pending_updates(context, decision)
                    return

                # A veg/non-veg mismatch was dropped: never place a wrong dish or a
                # silent partial order — stop here and ask the guest to re-pick a
                # vegetarian dish. The rest of the cart (e.g. naan) is kept in progress.
                if veg_mismatch:
                    context.pending_data["_ird_veg_mismatch"] = veg_mismatch
                    logger.info("[SAROVAR_IRD] blocked veg/non-veg mismatch: %s (guest asked vegetarian)", veg_mismatch)
                    decision.metadata["ird_veg_mismatch_blocked"] = veg_mismatch[:6]
                    _vm = " and ".join(veg_mismatch)
                    _resp = (
                        f"I'm sorry — **{_vm}** is a non-vegetarian dish, and it looks like you'd like a vegetarian "
                        "option, so I haven't added it. Could you pick a vegetarian dish from the menu"
                    )
                    _resp += ", and I'll add it to your order?" if cart_new else "?"
                    decision.response_text = _resp
                    decision.action = "respond_only"
                    if ticket is not None:
                        ticket.required = False
                        ticket.ready_to_create = False
                    decision.pending_action = ""
                    self._ird_sync_pending_updates(context, decision)
                    return

            # (b) The current durable cart — this turn's refresh, or a prior turn's cart
            # that survived topic switches (the key is underscore-prefixed, so slot
            # resets between turns keep it, unlike the old plain `ird_order`).
            cart = list(context.pending_data.get("_ird_cart") or [])

            # (c) PLACEMENT — guest confirmed and the F&B ticket is being raised. Move the
            # cart into the durable PLACED-ORDERS list so later status/recall turns
            # ("how long will my food take", "room service is taking forever") answer from
            # the REAL order instead of reconstructing it from chat (which conflates a
            # declined item as ordered). Then empty the in-progress cart and rebuild the
            # ticket from the cart so no dropped item can reach the kitchen.
            if _ready and cart:
                from datetime import datetime as _dt, timezone as _tz

                def _num(v: Any):
                    try:
                        return float(str(v).replace(",", "").replace("₹", "").strip())
                    except Exception:
                        return None

                _line_totals = [
                    (_num(l.get("price")) * int(l.get("qty") or 1))
                    if _num(l.get("price")) is not None else None
                    for l in cart
                ]
                order_total = (
                    int(sum(_line_totals))
                    if _line_totals and all(t is not None for t in _line_totals)
                    else None
                )
                order_record = {
                    "items": [
                        {
                            "name": l.get("item"),
                            "qty": int(l.get("qty") or 1),
                            "price": l.get("price"),
                            "allergens": l.get("allergens"),
                        }
                        for l in cart
                    ],
                    "total": order_total,
                    "room": room,
                    "status": "placed",
                    "placed_at": _dt.now(_tz.utc).isoformat(timespec="seconds"),
                }
                summary = ", ".join(f"{l['qty']} {l['item']}" for l in cart)
                # ── NOTHING IS ANNOUNCED AS PLACED FROM HERE ─────────────────
                # This runs while the order is still only INTENDED. Gates further
                # down the turn can still stop the ticket being written, and they
                # did: the guest was told "Done — your order is on its way" and
                # thanked for a dessert that never reached the kitchen, because
                # this line had already spoken for a ticket that was cancelled a
                # moment later.
                #
                # So the placement is handed over rather than declared. chat_service
                # commits it — the confirmation wording, the durable food memory and
                # the placed-at stamp — but only once the ticket actually exists on
                # both dashboards. See _ird_commit_placement.
                #
                # The cart is NOT cleared here. While the ticket this order raises is
                # still open and inside the edit window, the guest can keep adding to
                # the SAME order — so the cart must survive for the next turn to build
                # on, and the ticket gets rewritten with the full list. It is cleared
                # instead at the moment that ticket stops being editable (see the add
                # path above), which is when a further dish is genuinely a new order.
                decision.metadata["ird_placement"] = {
                    "order_record": order_record,
                    "summary": summary,
                    "room": room,
                }
                # Until that commit lands, the guest sees the order itself — the real
                # items, prices and allergens from the validated cart — and is asked to
                # confirm. If the ticket does get written this is replaced by the
                # confirmation; if it does not, the guest has an accurate summary and a
                # question, which is the honest thing to be left holding.
                # NEVER A QUESTION ON THE TURN WE ARE PLACING. This used to render
                # with already_placed = "is a ticket ref on file", which is EMPTY
                # for a guest's first order - the ref is written afterwards, once
                # the ticket exists. So the closing line came out "Shall I place
                # this order?" on the very turn the order went to the kitchen: the
                # guest said "go ahead", the order was committed, and they were
                # asked the same question again. Nothing to do with which words
                # they used to agree.
                #
                # It stays a statement rather than becoming "placed!" because gates
                # further down can still stop the ticket, and claiming an order that
                # never reached the kitchen is the worse lie. chat_service replaces
                # this with the real confirmation the moment the ticket exists.
                decision.response_text = self._render_ird_summary(
                    cart, room=room,
                    placing=True,
                    order_no=self._ird_display_order_no(context.pending_data),
                )
                self._ird_sync_pending_updates(context, decision)
                return

            # (d) Non-placement turns: a NEW off-menu decline, or the checkout summary,
            # both rendered from the validated cart so the guest text matches the order.
            _pa = str(getattr(decision, "pending_action", "") or "").lower()
            is_summary_turn = (
                bool(cart)
                and ticket is not None
                and bool(ticket.required)
                and not bool(ticket.ready_to_create)
                and ("confirm" in _pa or "summary" in str(decision.response_text or "").lower())
            )
            if newly and not is_summary_turn:
                items_txt = " and ".join(newly)
                msg = f"Sorry, we don't have **{items_txt}** on our in-room dining menu."
                if cart:
                    msg += (
                        "\n\n**Your order so far**\n"
                        + self._render_ird_cart_lines(cart)
                        + "\n\nShall I place this order?"
                    )
                else:
                    msg += " Would you like to see today's menu options?"
                decision.response_text = msg
            elif is_summary_turn:
                decision.response_text = self._render_ird_summary(
                    cart, room=room,
                    already_placed=bool(
                        str(context.pending_data.get("_ird_order_ticket_ref") or "").strip()
                    ),
                    order_no=self._ird_display_order_no(context.pending_data),
                )

            # Rebuild the ticket issue from the validated cart so a dropped item can
            # never reach the kitchen, regardless of what the model wrote.
            if cart and ticket is not None and bool(ticket.required):
                summary = ", ".join(f"{l['qty']} {l['item']}" for l in cart)
                # Item list intentionally NOT written here — chat_service composes
                # it from the validated cart when it writes to KePSLA and the
                # external dashboard. This line used to overwrite the ticket with
                # only the CURRENT turn's cart, which is how a dish added to an
                # open order replaced the food already ordered.
                pass
            # ── THE REPLY MAY NOT PROMISE FOOD THE BOOK DOES NOT HOLD ────────
            # RUNS ON EVERY FOOD TURN, and that is the whole point. It used to sit
            # inside the block above, which only executes when the model emits
            # order items - so on the one kind of turn that needs checking, where
            # the model wrote nothing at all, no check ran. A guest was told
            # "your order is confirmed and placed" for 2 Arrabbiata Pasta that
            # were never written anywhere and never reached the kitchen.
            #
            # The model is not reliable here: two turns with byte-identical input
            # at temperature 0 produced a write and a no-write. So the reply is
            # checked against the book instead of trusted.
            if bool(getattr(settings, "ird_order_book_enabled", False)):
                try:
                    await self._ird_reply_must_match_the_book(
                        context=context, decision=decision, room=room,
                        placing_now=_ready)
                except Exception as _pm:
                    logger.warning("[IRD_BOOK] claim check skipped (non-fatal): %r", _pm)
            self._ird_sync_pending_updates(context, decision)
        except Exception as _exc:  # never let order-state bookkeeping break a reply
            logger.exception(
                "[SAROVAR_IRD] ORDER STATE WAS NOT WRITTEN - the post-process "
                "raised, so the cart the guest was just shown does not exist "
                "server-side and will not reach the kitchen: %r", _exc)
