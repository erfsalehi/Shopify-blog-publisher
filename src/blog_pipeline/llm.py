"""Google AI Studio LLM factory + token/cost accounting.

Every stage gets its ChatOpenAI client from here, pointed at Google AI
Studio's OpenAI-compatible endpoint. Since the transport is OpenAI-compatible,
`.with_structured_output()` and LangSmith tracing both work unchanged (they
wrap the LangChain call, not the transport).

A CostTracker aggregates token usage across the stages of a single article
run. AI Studio's free tier is rate-limited rather than billed, so MODEL_RATES
is empty by default (cost shows as $0) — fill it in with real per-token rates
if/when you move to a paid Gemini API tier.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any

from langchain_openai import ChatOpenAI

from blog_pipeline.config import GOOGLE_BASE_URL, get_settings

# USD per 1M tokens (input, output). Empty on the free tier — AI Studio's
# free models are gated by rate limits, not per-token billing.
MODEL_RATES: dict[str, tuple[float, float]] = {}
PROVIDER_FEE = 0.0  # no credit-top-up fee on AI Studio, unlike OpenRouter

# HTTP statuses / error signatures worth retrying: transient over-capacity and
# rate-limit responses. Gemini's free tier throws 503 UNAVAILABLE and 429
# RESOURCE_EXHAUSTED under load — both usually clear within seconds.
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
_RETRYABLE_MARKERS = (
    "unavailable", "overloaded", "rate limit", "resource_exhausted",
    "try again", "temporarily",
)


def make_llm(
    model: str,
    temperature: float = 0.7,
    max_retries: int = 2,
    max_tokens: int | None = None,
    **kwargs,
) -> ChatOpenAI:
    """Build a ChatOpenAI client routed through Google AI Studio for `model`."""
    settings = get_settings()
    if not settings.google_api_key:
        raise RuntimeError(
            "GOOGLE_API_KEY is not set — required for all LLM stages."
        )
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    return ChatOpenAI(
        model=model,
        temperature=temperature,
        api_key=settings.google_api_key,
        base_url=GOOGLE_BASE_URL,
        max_retries=max_retries,
        **kwargs,
    )


def _is_retryable(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if isinstance(status, int) and status in _RETRYABLE_STATUSES:
        return True
    msg = str(exc).lower()
    return any(m in msg for m in _RETRYABLE_MARKERS)


def _backoff_seconds(attempt: int) -> float:
    return min(2.0 * (2 ** attempt) + random.uniform(0, 0.5), 15.0)


def structured_invoke(
    *,
    model: str,
    schema: type,
    messages: list,
    temperature: float = 0.4,
    stage: str = "",
    cost: "CostTracker | None" = None,
    fallbacks: list[str] | None = None,
    max_attempts: int = 3,
    max_tokens: int | None = None,
) -> Any:
    """Invoke `model` for structured output, retrying transient errors with
    backoff and falling back across other models if it keeps failing.

    Centralizes the make_llm + with_structured_output + invoke + cost.record
    pattern so every stage gets the same resilience against Gemini's free-tier
    503/429 spikes without duplicating retry logic. Returns the parsed schema
    instance; re-raises the last error only if every model is exhausted.
    """
    settings = get_settings()
    fb = settings.llm_fallback_models_list if fallbacks is None else fallbacks
    chain = [model] + [m for m in fb if m != model]

    last_exc: Exception | None = None
    for m in chain:
        structured = make_llm(
            m, temperature=temperature, max_tokens=max_tokens
        ).with_structured_output(schema, include_raw=True)
        for attempt in range(max_attempts):
            try:
                res = structured.invoke(messages)
                if cost is not None and res.get("raw") is not None:
                    cost.record(stage, m, res["raw"])
                return res["parsed"]
            except Exception as e:  # noqa: BLE001 — need broad transient handling
                last_exc = e
                if _is_retryable(e) and attempt < max_attempts - 1:
                    time.sleep(_backoff_seconds(attempt))
                    continue
                break  # exhausted this model's retries -> try the next one
    assert last_exc is not None
    raise last_exc


def cost_for(model: str, input_tokens: int, output_tokens: int) -> float:
    """USD cost for a single call given token counts (0.0 on the free tier)."""
    in_rate, out_rate = MODEL_RATES.get(model, (0.0, 0.0))
    return (input_tokens * in_rate + output_tokens * out_rate) / 1_000_000


@dataclass
class CostTracker:
    """Accumulates token usage/cost across the stages of one run."""

    input_tokens: int = 0
    output_tokens: int = 0
    usd: float = 0.0
    by_stage: dict[str, float] = field(default_factory=dict)

    def record(self, stage: str, model: str, response) -> None:
        """Pull usage_metadata off a LangChain AIMessage and tally it."""
        usage = getattr(response, "usage_metadata", None) or {}
        in_tok = int(usage.get("input_tokens", 0))
        out_tok = int(usage.get("output_tokens", 0))
        cost = cost_for(model, in_tok, out_tok)
        self.input_tokens += in_tok
        self.output_tokens += out_tok
        self.usd += cost
        self.by_stage[stage] = self.by_stage.get(stage, 0.0) + cost

    def with_fee(self) -> float:
        """Total including any provider fee on top of the LLM subtotal."""
        return round(self.usd * (1 + PROVIDER_FEE), 6)
