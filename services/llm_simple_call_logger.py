"""
Simple per-call LLM usage logger.

Writes one JSON record per LLM call with:
- time taken
- input/output/total tokens
- cost
- plain-English purpose
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from config.settings import settings

_logger = logging.getLogger(__name__)


def _safe_int(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def _safe_float(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    if parsed != parsed:
        return 0.0
    return max(0.0, parsed)


def _truncate_text(value: Any, max_chars: int) -> str:
    text = str(value or "")
    if len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    return f"{text[:max_chars]}\n...[TRUNCATED {omitted} chars]"


class LLMSimpleCallLogger:
    """Append-only JSONL logger for LLM call accounting."""

    # USD per 1M tokens. A model that is MISSING here prices at $0.00 — which is
    # exactly how 254M tokens of gpt-4.1-mini / gpt-5.4-mini traffic reached
    # new_bot_log_llm with total_cost_usd = 0 while the table still only knew the
    # gpt-4o family. Add every model here (or via the `llm_model_pricing_json`
    # env override) the DAY it goes into rotation; anything unlisted now warns
    # once and lands with pricing_source="unpriced" so the gap is visible in SQL
    # instead of reading as a genuinely free call.
    # OpenAI list prices verified 2026-09-10 against developers.openai.com/api/docs/pricing.
    _DEFAULT_PRICING_USD_PER_1M: dict[str, dict[str, float]] = {
        "gpt-5.4": {"input": 2.50, "output": 15.00},
        "gpt-5.4-mini": {"input": 0.75, "output": 4.50},
        "gpt-5.4-nano": {"input": 0.20, "output": 1.25},
        "gpt-5.6-luna": {"input": 0.20, "output": 1.20},
        "gpt-4.1-mini": {"input": 0.40, "output": 1.60},
        "gpt-4.1-nano": {"input": 0.10, "output": 0.40},
        "gpt-4o": {"input": 2.50, "output": 10.00},
        "gpt-4o-mini": {"input": 0.15, "output": 0.60},
        "text-embedding-3-small": {"input": 0.02, "output": 0.0},
        "text-embedding-3-large": {"input": 0.13, "output": 0.0},
    }

    def __init__(self) -> None:
        self.enabled = bool(getattr(settings, "llm_call_simple_logging_enabled", True))
        self.log_file = Path(str(getattr(settings, "llm_call_simple_log_file", "./logs/llm_call_metrics.jsonl")))
        self.max_text_chars = max(200, int(getattr(settings, "llm_call_simple_text_max_chars", 1200) or 1200))
        self._lock = threading.Lock()
        self._pricing = dict(self._DEFAULT_PRICING_USD_PER_1M)
        self._pricing_origin: dict[str, str] = {k: "default" for k in self._pricing}
        self._unpriced_seen: set[str] = set()
        self._load_pricing_override()

    def _load_pricing_override(self) -> None:
        raw = str(getattr(settings, "llm_model_pricing_json", "") or "").strip()
        if not raw:
            return
        try:
            parsed = json.loads(raw)
        except Exception:
            return
        if not isinstance(parsed, dict):
            return
        normalized: dict[str, dict[str, float]] = {}
        for model_name, rates in parsed.items():
            if not isinstance(rates, dict):
                continue
            name = str(model_name or "").strip()
            if not name:
                continue
            normalized[name] = {
                "input": _safe_float(rates.get("input") or rates.get("input_per_million") or 0.0),
                "output": _safe_float(rates.get("output") or rates.get("output_per_million") or 0.0),
            }
        if normalized:
            self._pricing.update(normalized)
            self._pricing_origin.update({k: "override" for k in normalized})

    def _resolve_pricing(self, model: str) -> tuple[float, float, str]:
        """Return (input_rate, output_rate, source) per 1M tokens.

        `source` is what lands in new_bot_log_llm.pricing_source: "override" when
        the rate came from llm_model_pricing_json, "default" from the table above,
        and "unpriced" when this model has no rate at all — the case that used to
        be indistinguishable from a free call.
        """
        name = str(model or "").strip()
        if not name:
            return 0.0, 0.0, "unpriced"
        # OpenRouter ids arrive provider-qualified ("openai/gpt-5.4-mini"); the
        # rate table is keyed on the bare model name, and 160 calls have already
        # logged under the prefixed form.
        bare = name.split("/", 1)[1] if "/" in name else name
        for candidate in (name, bare):
            if candidate in self._pricing:
                rates = self._pricing[candidate]
                return (
                    _safe_float(rates.get("input")),
                    _safe_float(rates.get("output")),
                    self._pricing_origin.get(candidate, "default"),
                )
        # Model snapshots such as "gpt-4o-2024-08-06". Longest key first, so
        # "gpt-5.4-mini-<date>" matches "gpt-5.4-mini" and not "gpt-5.4".
        for known_name in sorted(self._pricing, key=len, reverse=True):
            if bare.startswith(f"{known_name}-"):
                rates = self._pricing[known_name]
                return (
                    _safe_float(rates.get("input")),
                    _safe_float(rates.get("output")),
                    self._pricing_origin.get(known_name, "default"),
                )
        self._warn_unpriced(name)
        return 0.0, 0.0, "unpriced"

    def _warn_unpriced(self, model: str) -> None:
        """Say it out loud, once per model — a $0 cost row is otherwise silent."""
        with self._lock:
            if model in self._unpriced_seen:
                return
            self._unpriced_seen.add(model)
        _logger.warning(
            "LLM cost logging: no pricing for model %r — its calls will log "
            "total_cost_usd=0 with pricing_source='unpriced'. Add a rate to "
            "LLMSimpleCallLogger._DEFAULT_PRICING_USD_PER_1M or to the "
            "llm_model_pricing_json env override.",
            model,
        )

    def _compute_cost(
        self,
        *,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> tuple[float, float, float, float, float, str]:
        input_rate, output_rate, source = self._resolve_pricing(model)
        input_cost = (_safe_int(input_tokens) / 1_000_000.0) * input_rate
        output_cost = (_safe_int(output_tokens) / 1_000_000.0) * output_rate
        total_cost = input_cost + output_cost
        return input_cost, output_cost, total_cost, input_rate, output_rate, source

    def log_call(
        self,
        *,
        call_id: str,
        operation: str,
        model: str,
        purpose: str,
        duration_ms: float,
        status: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        total_tokens: int = 0,
        session_id: str = "",
        session_key: str = "",
        hotel_code: str = "",
        channel: str = "",
        trace_id: str = "",
        turn_trace_id: str = "",
        route: str = "",
        component: str = "",
        caller_module: str = "",
        caller_function: str = "",
        input_preview: str = "",
        output_preview: str = "",
        error: str = "",
    ) -> dict[str, Any]:
        input_tok = _safe_int(input_tokens)
        output_tok = _safe_int(output_tokens)
        total_tok = _safe_int(total_tokens) or (input_tok + output_tok)
        input_cost, output_cost, total_cost, input_rate, output_rate, pricing_source = self._compute_cost(
            model=model,
            input_tokens=input_tok,
            output_tokens=output_tok,
        )
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "call_id": str(call_id or "").strip(),
            "operation": str(operation or "").strip(),
            "model": str(model or "").strip(),
            "what_this_call_is_for": str(purpose or "").strip() or "LLM call",
            "status": str(status or "").strip() or "unknown",
            "duration_ms": round(_safe_float(duration_ms), 2),
            "input_tokens": input_tok,
            "output_tokens": output_tok,
            "total_tokens": total_tok,
            "input_cost_usd": round(input_cost, 8),
            "output_cost_usd": round(output_cost, 8),
            "total_cost_usd": round(total_cost, 8),
            # Carried so new_bot_log_llm records WHICH rate produced that cost —
            # db_log_sink already maps these four columns, the producer just
            # never filled them, leaving input_rate/pricing_source NULL on every
            # row and the numbers unauditable.
            "input_rate": input_rate,
            "output_rate": output_rate,
            "currency": "USD",
            "pricing_source": pricing_source,
            "session_id": str(session_id or "").strip(),
            "session_key": str(session_key or "").strip(),
            "hotel_code": str(hotel_code or "").strip(),
            "channel": str(channel or "").strip(),
            "trace_id": str(trace_id or "").strip(),
            "turn_trace_id": str(turn_trace_id or "").strip(),
            "route": str(route or "").strip(),
            "component": str(component or "").strip(),
            "caller_module": str(caller_module or "").strip(),
            "caller_function": str(caller_function or "").strip(),
            "input_preview": _truncate_text(input_preview, self.max_text_chars),
            "output_preview": _truncate_text(output_preview, self.max_text_chars),
            "error": _truncate_text(error, self.max_text_chars) if error else "",
        }
        if not self.enabled:
            return dict(record)
        try:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            encoded = json.dumps(record, ensure_ascii=False)
            with self._lock:
                with self.log_file.open("a", encoding="utf-8") as fh:
                    fh.write(encoded + "\n")
        except Exception:
            return dict(record)
        return dict(record)


llm_simple_call_logger = LLMSimpleCallLogger()
