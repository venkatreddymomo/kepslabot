"""
Response Beautifier Service

Builds display-only variants of assistant text.
"""

from __future__ import annotations

import asyncio
import time
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any

from config.settings import settings
from llm.client import llm_client

class ResponseBeautifierService:
    """Format outbound assistant text for cleaner chat presentation."""

    _COLLECTION_REQUEST_PATTERN = re.compile(
        r"(?P<lemma>(?:could\s+you\s+)?(?:please|kindly)\s+share(?:\s+your)?\s+)"
        r"(?P<fields>[^.?!\n]+)"
        r"(?P<end>[.?!]?)",
        flags=re.IGNORECASE,
    )

    @staticmethod
    def _strip_markdown_headers(text: str) -> str:
        """Convert markdown headers (# / ## / ###) to plain text lines."""
        return re.sub(r"(?m)^#{1,6}\s+", "", str(text or ""))

    @staticmethod
    def _strip_markdown_emphasis(text: str) -> str:
        """Preserve both **bold** and *italic* / _italic_ so the LLM's
        intentional emphasis (proper nouns, signature features, sensory
        highlights) survives into the rendered chat reply.

        We intentionally do NOT strip italic any more — the chat UI renders
        single-asterisk italic as italic, and the LLM uses it (per the style
        contract) to highlight scannable phrases inside a bullet. Stripping
        broke the entire italic contract.
        """
        return str(text or "")

    @staticmethod
    def _normalize_bullet_markers(text: str) -> str:
        lines: list[str] = []
        for raw_line in str(text or "").split("\n"):
            stripped = raw_line.strip()
            if not stripped:
                lines.append("")
                continue
            if re.match(r"^[-*]\s+", stripped):
                item = re.sub(r"^[-*]\s+", "", stripped)
                lines.append(f"- {item}")
                continue
            if stripped.startswith("• "):
                lines.append(f"- {stripped[2:].strip()}")
                continue
            lines.append(raw_line.rstrip())
        return "\n".join(lines)

    @staticmethod
    def _split_detail_fields(raw_fields: str) -> list[str]:
        fields = str(raw_fields or "").strip()
        if not fields:
            return []

        # Remove trailing purpose clause for cleaner bullets.
        fields = re.split(
            r"\bso\s+(?:that\s+)?(?:i|we)\b|\bto\s+(?:help|proceed|continue|forward)\b",
            fields,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip(" .,:;")
        if not fields:
            return []

        normalized = re.sub(r"\s+\band\b\s+", ", ", fields, flags=re.IGNORECASE)
        parts = [segment.strip(" .,:;") for segment in normalized.split(",")]
        cleaned_parts: list[str] = []
        for part in parts:
            if not part:
                continue
            item = re.sub(r"^(?:the|your)\s+", "", part, flags=re.IGNORECASE).strip(" .,:;")
            if not item:
                continue
            cleaned_parts.append(item)
        return cleaned_parts

    def _format_collection_request(self, text: str) -> str:
        source = str(text or "")
        match = self._COLLECTION_REQUEST_PATTERN.search(source)
        if not match:
            return source

        fields_segment = str(match.group("fields") or "")
        example_note = ""
        example_parts = re.findall(r"\(([^)]*)\)", fields_segment)
        if example_parts:
            for raw_note in example_parts:
                if re.search(r"(?:for\s+example|e\.?g\.?)", raw_note, flags=re.IGNORECASE):
                    example_note = re.sub(
                        r"^(?:for\s+example|e\.?g\.?)[\s:.-]*",
                        "",
                        raw_note.strip(),
                        flags=re.IGNORECASE,
                    ).strip()
                    break
        fields_without_notes = re.sub(r"\([^)]*\)", "", fields_segment).strip()

        parts = self._split_detail_fields(fields_without_notes)
        if len(parts) < 2:
            return source

        # ONLY BULLETISE AN ACTUAL LIST OF FIELDS. This rule exists to turn
        # "please share your name, email and phone number" into a tidy list, but
        # it matched any sentence opening "please share your ..." and split it on
        # every comma and "and". The booking line
        #   "please share your booking details using the button below, and our
        #    team will get back to you to confirm"
        # came out as two nonsense bullets, the second of which is not something
        # a guest can share at all. A field the guest can type is short - "name",
        # "email", "check-in date" - so anything long enough to be a clause means
        # this is prose, and prose is left alone.
        if any(len(re.findall(r"\w+", item)) > 4 for item in parts):
            return source

        prefix = source[: match.start()].rstrip().rstrip(",;:")
        suffix = source[match.end() :].lstrip()

        bullets = "\n".join(f"- {item}" for item in parts)
        replacement = f"Please share the following details:\n{bullets}"
        if example_note:
            replacement = f"{replacement}\nExample: {example_note}"

        rebuilt = replacement
        if prefix:
            rebuilt = f"{prefix}\n{replacement}"
        if suffix:
            rebuilt = f"{rebuilt}\n{suffix}"
        return rebuilt

    @staticmethod
    def _cleanup_spacing(text: str) -> str:
        compact = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
        compact = re.sub(r"\n{3,}", "\n\n", compact)
        compact = re.sub(r"[ \t]+\n", "\n", compact)
        return compact.strip()

    def beautify_response_text(self, text: str) -> str:
        if not str(text or "").strip():
            return ""
        cleaned = self._strip_markdown_headers(text)
        cleaned = self._strip_markdown_emphasis(cleaned)
        cleaned = self._normalize_bullet_markers(cleaned)
        cleaned = self._format_collection_request(cleaned)
        cleaned = self._cleanup_spacing(cleaned)
        return cleaned

    @staticmethod
    def _comparison_text(value: str) -> str:
        text = str(value or "").strip().lower()
        text = re.sub(r"\s+", " ", text)
        return text

    @staticmethod
    def _normalize_digits(value: str) -> str:
        """Fold native-script digits onto ASCII so figures can be compared.

        A faithful Hindi or Punjabi reply writes the same time in its own
        numerals - 7:30 becomes ७:३० (Devanagari) or ੭:੩੦ (Gurmukhi). The number
        guard below compares figures as TEXT, so those read as different tokens,
        the guard concluded the time had been dropped, and it threw the correct
        translation away and shipped the untranslated English draft instead.
        Every reply carrying a time or a price was affected.

        Folding here can only ever make MORE figures match, never fewer, so no
        reply that passed the guard before can start failing it.
        """
        text = str(value or "")
        if text.isascii():
            return text
        out: list[str] = []
        for ch in text:
            if not ch.isascii() and ch.isdigit():
                try:
                    out.append(str(unicodedata.digit(ch)))
                    continue
                except (TypeError, ValueError):
                    pass
            out.append(ch)
        return "".join(out)

    @classmethod
    def _extract_number_tokens(cls, value: str) -> set[str]:
        return set(re.findall(r"\b\d+\b", cls._normalize_digits(value)))

    @classmethod
    def _significant_number_tokens(cls, value: str) -> set[str]:
        """Numbers a faithful translation can never legitimately spell out.

        The plain number check exists so the formatter cannot quietly drop a
        time, price or policy figure. Translation is different: "book 1 day in
        advance" is correctly rendered "oka roju mundu" in Telugu or "ondu dina
        modalu" in Kannada, and the digit disappearing is the translation
        working, not a fact going missing. Counting that as data loss threw the
        whole translated reply away and shipped the untranslated draft instead
        - the guest asked in Telugu and got Hindi back.

        So on the translation path enforce only the figures that carry a fact
        the guest acts on: anything two digits or longer, and any number sitting
        inside a time, decimal or price (15:00, 2.5, 1,200). A bare single digit
        may be spelled out in the target language.
        """
        text = cls._normalize_digits(value)
        significant: set[str] = set()
        for match in re.finditer(r"\b\d+\b", text):
            token = match.group(0)
            if len(token) >= 2:
                significant.add(token)
                continue
            # A lone digit counts as a fact when it is part of a clock time or
            # carries a unit ("3 PM", "2 hrs", "5%", "$4"). Written as a bare
            # count ("1 day in advance") it is ordinary vocabulary that the
            # target language is free to spell out.
            before = text[max(0, match.start() - 1):match.start()]
            after = text[match.end():match.end() + 8]
            if before in {":", ".", ",", "$", "%"}:
                significant.add(token)
            elif re.match(
                r"\s*(?:[:.,%]|am\b|pm\b|hrs?\b|hours?\b|o'clock\b)",
                after,
                re.IGNORECASE,
            ):
                significant.add(token)
        return significant

    @staticmethod
    def _extract_word_tokens(value: str) -> set[str]:
        return {
            token
            for token in re.findall(r"[a-z0-9]+", str(value or "").lower())
            if len(token) >= 2
        }

    @staticmethod
    def _contains_non_latin_script(value: str) -> bool:
        """Reject native-script leakage when the router requires Latin text."""
        return bool(
            re.search(
                "["
                "\u0400-\u052f"  # Cyrillic
                "\u0600-\u06ff"  # Arabic
                "\u0900-\u097f"  # Devanagari
                "\u0980-\u09ff"  # Bengali
                "\u0a00-\u0a7f"  # Gurmukhi
                "\u0a80-\u0aff"  # Gujarati
                "\u0b80-\u0bff"  # Tamil
                "\u0c00-\u0c7f"  # Telugu
                "\u0c80-\u0cff"  # Kannada
                "\u0d00-\u0d7f"  # Malayalam
                "\u0e00-\u0e7f"  # Thai
                "\u3040-\u30ff"  # Japanese kana
                "\u3400-\u9fff"  # CJK
                "\uac00-\ud7af"  # Hangul
                "]",
                str(value or ""),
            )
        )

    @staticmethod
    def _control_markers(value: str) -> set[str]:
        return set(re.findall(r"<<[^<>]+>>|FOCUS:\s*[^\r\n]+", str(value or "")))

    @staticmethod
    def _looks_dense_single_block(text: str) -> bool:
        compact = str(text or "").strip()
        if not compact:
            return False
        return len(compact) >= 320 and compact.count("\n") <= 1

    @staticmethod
    def _is_structured_chat_style(text: str) -> bool:
        content = str(text or "").strip()
        if not content:
            return False
        if content.count("\n") < 1:
            return False
        if re.search(r"(?m)^\s*[-*]\s+\S+", content):
            return True
        if re.search(r"(?m)^\s*\d+\.\s+\S+", content):
            return True
        if re.search(r"(?m)^\s*[A-Za-z][^:\n]{2,40}:\s*$", content):
            return True
        if re.search(r"(?m)^\s*\*\*[^*\n]{2,60}\*\*:?$", content):
            return True
        return False

    def _rewrite_guardrails_pass(
        self,
        *,
        base_text: str,
        candidate_text: str,
        allow_language_rewrite: bool = False,
        target_script: str = "",
    ) -> tuple[bool, str]:
        base = self._comparison_text(base_text)
        candidate = self._comparison_text(candidate_text)
        if not candidate:
            return False, "empty_candidate"
        if candidate.startswith("i'm having trouble processing that right now"):
            return False, "llm_fallback_message"
        if "```" in candidate_text:
            return False, "contains_code_fence"
        if self._control_markers(base_text) != self._control_markers(candidate_text):
            return False, "control_markers_changed"
        if (
            allow_language_rewrite
            and str(target_script or "").strip().casefold() == "latin"
            and self._contains_non_latin_script(candidate_text)
        ):
            return False, "wrong_script"

        base_len = len(base)
        candidate_len = len(candidate)
        if base_len > 0:
            if candidate_len > max(1800, int(base_len * 2.5)):
                return False, "candidate_too_long"
            if candidate_len < max(8, int(base_len * 0.25)):
                return False, "candidate_too_short"

        if not allow_language_rewrite:
            base_tokens = self._extract_word_tokens(base_text)
            overlap_ratio = 0.0
            if base_tokens:
                candidate_tokens = self._extract_word_tokens(candidate_text)
                overlap_ratio = len(base_tokens & candidate_tokens) / max(1, len(base_tokens))
                if overlap_ratio < 0.68:
                    return False, "low_token_overlap"

            similarity = SequenceMatcher(a=base, b=candidate).ratio()
            min_similarity = float(getattr(settings, "chat_display_beautifier_min_similarity", 0.45) or 0.45)
            normalized_min_similarity = max(0.08, min(0.9, min_similarity))
            if similarity < normalized_min_similarity and overlap_ratio < 0.68:
                return False, "low_similarity"

        confirmation_phrase = str(getattr(settings, "chat_confirmation_phrase", "yes confirm") or "yes confirm").strip()
        if confirmation_phrase:
            phrase_lower = confirmation_phrase.lower()
            in_base = phrase_lower in base
            in_candidate = phrase_lower in candidate
            if in_base and not in_candidate:
                return False, "confirmation_phrase_lost"
            if not in_base and in_candidate:
                return False, "confirmation_phrase_added"

        if allow_language_rewrite:
            base_numbers = self._significant_number_tokens(base_text)
            candidate_numbers = self._significant_number_tokens(candidate_text)
        else:
            base_numbers = self._extract_number_tokens(base_text)
            candidate_numbers = self._extract_number_tokens(candidate_text)
        if base_numbers:
            retained = len(base_numbers & candidate_numbers)
            if retained < len(base_numbers):
                return False, "numeric_details_dropped"

        return True, ""

    async def beautify_display_text(
        self,
        text: str,
        *,
        state: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        base_text = self.beautify_response_text(text)
        meta: dict[str, Any] = {
            "display_beautifier_enabled": bool(getattr(settings, "chat_display_beautifier_enabled", True)),
            "display_beautifier_llm_used": False,
            "display_beautifier_retry_used": False,
            "display_beautifier_applied": False,
            "display_beautifier_fallback_reason": "",
            "display_beautifier_language_enforced": False,
        }

        request_metadata = metadata if isinstance(metadata, dict) else {}
        response_language = request_metadata.get("response_language")
        if not isinstance(response_language, dict):
            response_language = {}
        target_language = str(response_language.get("language") or "").strip()
        target_script = str(response_language.get("script") or "").strip()
        target_style = str(response_language.get("style") or "").strip()
        language_reference_text = str(
            request_metadata.get("language_reference_text") or ""
        ).strip()[:500]
        language_switch = bool(request_metadata.get("language_switch"))
        language_enforcement = bool(target_language and target_script)

        # NOTHING TO TRANSLATE — DON'T RISK THE TRANSLATOR ON A GOOD REPLY.
        # Asking for English while the draft is already plain A-Z prose is a
        # no-op translation, and running it anyway is pure downside: observed
        # live on 2026-09-28, the answerer produced correct English ("190 sq.
        # ft. with a choice of king or twin beds") and this pass rewrote it into
        # the LATIN LANGUAGE ("190 pedes quadrati cum optionibus lecti regii")
        # after reading its own "REQUIRED SCRIPT: Latin" line as the target.
        # Skipping here removes that failure mode outright AND drops one LLM
        # call from every ordinary English turn, so enforcement only ever costs
        # latency on a turn that genuinely needs translating. Formatting still
        # runs below - only the LANGUAGE contract is stood down.
        # `language_switch` is the guard that makes this safe. Latin script does
        # NOT prove the draft is English - romanized Hindi is Latin too, and
        # skipping on script alone shipped "Yeh dono kamre ... barabar ke size
        # ke hain" to a guest who had asked in English and whose router verdict
        # said English. So only stand down when the conversation was ALREADY in
        # this language: no switch in play means the draft has no other language
        # to have drifted into. Every turn where the language actually moves
        # still gets enforced.
        # NOTE: `strict_language_enforcement` deliberately does NOT bypass this.
        # Enforcement mode is a TRANSLATOR - its prompt orders it to keep the
        # draft's bullet and heading style "exactly as it is" - so turning it on
        # silently turns BEAUTIFYING OFF, and the model then failed to preserve
        # what it was told to preserve: `**Premium City View**` on its own line
        # came back as plain text welded onto the previous sentence, which also
        # broke the markdown list into literal dashes. On a turn whose target is
        # English, with no language switch and no foreign script in the draft,
        # there is nothing to translate, so running the translator can only
        # damage a good reply. Standing down here returns the turn to the normal
        # formatting path, which is what prod does. Language is still enforced on
        # every turn where it can actually have drifted.
        # NOTHING TO TRANSLATE - DO NOT LET THE MODEL NEAR THE REPLY.
        # Running this pass on every turn was tried and reverted: given a short
        # draft with no facts in it ("I am here to assist with your stay...") the
        # model obeyed the formatting instruction to produce bullets by INVENTING
        # three, including a "complimentary Wi-Fi, air conditioning, and daily
        # housekeeping" line that appears nowhere in the knowledge base. On the
        # enforcement path the overlap and similarity guards are deliberately
        # skipped (a real translation shares no tokens with its source), so a
        # hallucinating pass has no net under it - which is exactly why it must
        # not run when it has nothing to do.
        #
        # Known gap, accepted knowingly: romanized Hindi/Punjabi is Latin script,
        # so a conversation that has drifted into Hinglish can keep answering in
        # Hinglish after an English question. A wrong-language reply is recoverable
        # by the guest; an invented amenity is not.
        # The skip is OFF: romanized Hindi/Punjabi is Latin script, so standing
        # down on "target English + Latin draft" let a Hinglish reply answer an
        # English question. It runs on every enforced turn now. The reason that
        # was unsafe before - the pass inventing bullets out of a short draft -
        # is addressed in its prompt, which now states outright that the
        # formatting rules never justify adding content.
        _ = language_switch
        meta["display_beautifier_language_enforced"] = language_enforcement

        if not base_text:
            return "", meta
        if not meta["display_beautifier_enabled"]:
            return base_text, meta
        # Skip only on very short replies (single ack lines, one-word answers)
        # — anything longer should still get a bold pass for proper-noun emphasis.
        if not language_enforcement and len(base_text) <= 60:
            meta["display_beautifier_fallback_reason"] = "already_readable"
            return base_text, meta
        _max_input_chars = max(
            6000 if language_enforcement else 240,
            240,
            int(getattr(settings, "chat_display_beautifier_max_input_chars", 1400) or 1400),
        )
        if len(base_text) > _max_input_chars:
            meta["display_beautifier_fallback_reason"] = "input_too_long"
            return base_text, meta
        # Skip the LLM beautifier only when the text is BOTH already structured
        # (bullets/numbered/labels) AND already has bold emphasis applied.
        # Otherwise we always run the beautifier so labels get bold consistently.
        already_has_bold = bool(re.search(r"\*\*[^*\n]+\*\*", base_text))
        if (
            not language_enforcement
            and self._is_structured_chat_style(base_text)
            and already_has_bold
        ):
            meta["display_beautifier_fallback_reason"] = "already_readable"
            return base_text, meta

        use_llm = bool(getattr(settings, "chat_display_beautifier_use_llm", True))
        if not use_llm:
            meta["display_beautifier_fallback_reason"] = "llm_disabled"
            return base_text, meta
        if not str(getattr(settings, "openai_api_key", "") or "").strip():
            meta["display_beautifier_fallback_reason"] = "missing_openai_api_key"
            return base_text, meta

        response_source = str(request_metadata.get("response_source") or "").strip()
        model = (
            str(getattr(settings, "chat_display_beautifier_model", "") or "").strip()
            or str(getattr(settings, "chat_llm_response_surface_model", "") or "").strip()
            or None
        )
        temperature = (
            0.0
            if language_enforcement
            else float(getattr(settings, "chat_display_beautifier_temperature", 0.2) or 0.2)
        )
        _configured_max = max(80, int(getattr(settings, "chat_display_beautifier_max_tokens", 420) or 420))
        # Scale max_tokens based on input length so long responses (room lists,
        # comparisons) aren't truncated by the beautifier LLM.  Roughly 1 token
        # ≈ 4 chars; we allow ~1.3x headroom for formatting additions.
        _input_estimated_tokens = max(80, len(base_text) // 3)
        max_tokens = max(_configured_max, min(_input_estimated_tokens, 2048))
        timeout_seconds = max(
            8.0 if language_enforcement else 0.25,
            float(getattr(settings, "chat_display_beautifier_timeout_ms", 900) or 900) / 1000.0,
        )
        confirmation_phrase = str(getattr(settings, "chat_confirmation_phrase", "yes confirm") or "yes confirm").strip()
        confirmation_present = bool(
            confirmation_phrase
            and confirmation_phrase.lower() in self._comparison_text(base_text)
        )

        if language_enforcement:
            system_prompt = (
                "You are the final language enforcer and formatting editor for a hotel concierge reply.\n"
                # Title-cased so the target never arrives as a lowercase
                # "english", which reads as incidental next to a capitalised
                # script name and helped the model treat the SCRIPT as the
                # target language.
                f"REQUIRED OUTPUT LANGUAGE: {target_language.strip().title()}\n"
                # "Latin" alone is genuinely ambiguous - it is both a script and
                # a language, and the model resolved it the wrong way, returning
                # a reply written in the Latin LANGUAGE. Name the writing system
                # explicitly and rule the language out.
                f"REQUIRED SCRIPT (the writing system only, NEVER the target language): {target_script}\n"
                "'Latin' as a SCRIPT means the ordinary A-Z alphabet. It NEVER means the Latin language: "
                "never produce Latin (Roman) prose, and never treat the script name as something to translate into.\n"
                f"REQUIRED STYLE: {target_style or 'natural'}\n"
                "Translate EVERY guest-visible phrase, connector, label, opening, and closing into the required language. "
                "The latest guest message is the style reference. Do not imitate the draft's language.\n"
                "If REQUIRED SCRIPT is Latin, output Latin letters only for prose; never use native-script characters.\n"
                "If the draft opens with a yes/no acknowledgement, keep its meaning exactly - never turn a refusal into an agreement - but write it in the required language.\n"
                "Any yes/no word must be the standard one in the required language - Hindi haan/nahin, Punjabi haan/nahin, Telugu avunu/ledu, Kannada houdu/illa, Tamil aam/illai, Malayalam athe/illa, Marathi ho/nahi, Bengali hyan/na - never one carried over from another language, and never a word that means something else there.\n"
                "Use accurate, natural grammar. Silently audit the final reply for words or script left over from the draft language.\n"
                "Preserve every fact, qualification, proper noun, number, time, date, price, policy, ID, confirmation phrase, "
                "and <<...>> or FOCUS marker. Do not add or remove facts.\n"
                # THIS PASS FORMATS AS WELL AS TRANSLATES.
                # It used to only be told to "keep the draft's style exactly as
                # it is", which made turning enforcement on silently turn
                # BEAUTIFYING OFF - and the model then failed at the one job it
                # had, returning `**Premium City View**` as plain text welded
                # onto the sentence above it. Carrying the same formatting
                # contract the non-enforcement pass uses means a translated
                # reply comes back as well-structured as an untranslated one,
                # which is what makes it safe to run this on every turn.
                "Formatting style requirements (these NEVER justify inventing content - "
                "if the draft has only one or two sentences and no list in it, return those "
                "sentences translated and otherwise unchanged, with NO bullets):\n"
                "- Do NOT output long paragraphs.\n"
                "- Use one short lead-in line only when useful.\n"
                "- Then use bullet points for facts/options/details ALREADY IN THE DRAFT, one per line. "
                "Never write a bullet whose content is not in the draft.\n"
                "- Keep a section label on its OWN line, never joined to the sentence before it.\n"
                "- Preserve **bold** exactly where the draft has it, and keep the draft's own "
                "bullet character rather than swapping it for another.\n"
                "- Separate sections with a blank line.\n"
                "- Keep wording close to the source: translate and format, do not rewrite.\n"
                "Do not use markdown headers (#, ##, ###) or code fences. "
                "Return only the final guest-visible text.\n"
            )
        else:
            system_prompt = (
            "You are a display-only formatting editor for concierge replies.\n"
            "Goal: make the response clean, scannable, and point-based.\n"
            "Formatting style requirements:\n"
            "- Do NOT output long paragraphs.\n"
            "- Use one short lead-in line only when useful.\n"
            "- Then use bullet points ('- ') for facts/options/details.\n"
            "- Keep wording close to source; this is formatting-first, not rewriting.\n"
            "Hard constraints:\n"
            "- Preserve the draft's LANGUAGE and SCRIPT exactly — if the draft is in Hindi, Japanese, romanized Hinglish, or any non-English language, keep it in that same language. NEVER translate the draft.\n"
            "- Do NOT use markdown headers (#, ##, ###) — never output lines starting with #.\n"
            "- Do NOT add any new fact or claim.\n"
            "- Do NOT remove any existing fact or claim.\n"
            "- Preserve all numbers, ranges, names, dates, policies, constraints, and IDs.\n"
            "- Keep explicit confirmation phrase unchanged when present.\n"
            "- Return plain text only.\n"
        )
        if language_enforcement:
            user_prompt = (
                f"Conversation state: {str(state or '').strip() or 'unknown'}\n"
                f"Response source: {response_source or 'unknown'}\n"
                f"Required language: {target_language}\n"
                f"Required script: {target_script}\n"
                f"Required style: {target_style or 'natural'}\n"
                f"Language switch: {language_switch}\n"
                f"Latest guest message (language/style reference): {language_reference_text or 'not provided'}\n"
                f"Confirmation phrase in draft: {(confirmation_phrase if confirmation_present else 'NOT_PRESENT')}\n"
                "Do not add the confirmation phrase if it is not in the draft.\n\n"
                f"Draft response to translate and format:\n{base_text}"
            )
        else:
            user_prompt = (
            f"Conversation state: {str(state or '').strip() or 'unknown'}\n"
            f"Response source: {response_source or 'unknown'}\n"
            f"Confirmation phrase in draft: {(confirmation_phrase if confirmation_present else 'NOT_PRESENT')}\n"
            "Do not add the confirmation phrase if it is not present in the draft.\n\n"
            "Please keep the exact content but improve readability and structure.\n\n"
            f"Draft response:\n{base_text}"
        )

        _started_at = time.perf_counter()
        try:
            rewritten = await asyncio.wait_for(
                llm_client.chat(
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    trace_context={
                        "actor": "display_beautifier",
                        "service_id": "display_beautifier",
                        "service_name": "Display Beautifier",
                    },
                ),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            meta["display_beautifier_fallback_reason"] = "beautifier_timeout"
            rewritten = ""
        except Exception:
            rewritten = ""

        meta["display_beautifier_llm_used"] = True
        meta["display_beautifier_latency_ms"] = int(
            (time.perf_counter() - _started_at) * 1000
        )
        candidate = self._cleanup_spacing(str(rewritten or ""))
        if not candidate:
            meta["display_beautifier_fallback_reason"] = "empty_llm_output"
            return base_text, meta

        passes, reason = self._rewrite_guardrails_pass(
            base_text=base_text,
            candidate_text=candidate,
            allow_language_rewrite=language_enforcement,
            target_script=target_script,
        )
        if not passes:
            meta["display_beautifier_fallback_reason"] = reason
            return base_text, meta

        if self._looks_dense_single_block(base_text) and not self._is_structured_chat_style(candidate):
            retry_prompt = (
                f"{user_prompt}\n\n"
                "Retry with STRICT formatting:\n"
                "- No long paragraphs.\n"
                "- Start with a short context line only when needed.\n"
                "- Then use bullet points for each distinct fact cluster.\n"
                "- Keep all factual content unchanged.\n"
            )
            try:
                retried = await asyncio.wait_for(
                    llm_client.chat(
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": retry_prompt},
                        ],
                        model=model,
                        temperature=max(0.0, min(temperature, 0.15)),
                        max_tokens=max_tokens,
                        trace_context={
                            "actor": "display_beautifier",
                            "service_id": "display_beautifier",
                            "service_name": "Display Beautifier",
                        },
                    ),
                    timeout=timeout_seconds,
                )
            except asyncio.TimeoutError:
                retried = ""
            except Exception:
                retried = ""
            retry_candidate = self._cleanup_spacing(str(retried or ""))
            if retry_candidate:
                retry_passes, retry_reason = self._rewrite_guardrails_pass(
                    base_text=base_text,
                    candidate_text=retry_candidate,
                    allow_language_rewrite=language_enforcement,
                    target_script=target_script,
                )
                if retry_passes and self._is_structured_chat_style(retry_candidate):
                    candidate = retry_candidate
                    meta["display_beautifier_retry_used"] = True
                elif retry_reason:
                    meta["display_beautifier_fallback_reason"] = retry_reason

        meta["display_beautifier_applied"] = (
            self._comparison_text(candidate) != self._comparison_text(base_text)
        )
        return candidate, meta


response_beautifier_service = ResponseBeautifierService()
