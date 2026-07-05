"""OpenRouter LLM factory + token/cost accounting.

Every stage gets its ChatOpenAI client from here, pointed at OpenRouter's
OpenAI-compatible endpoint. Since the transport is OpenAI-compatible,
LangSmith tracing works unchanged (it wraps the LangChain call).

A CostTracker aggregates token usage across the stages of a single article
run and converts it to USD using the per-model rates below (PRD Section 12).
The OpenRouter 5.5% credit fee is applied once at the end via `with_fee`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from langchain_openai import ChatOpenAI

from blog_pipeline.config import OPENROUTER_BASE_URL, get_settings

# USD per 1M tokens (input, output). Anthropic list rates as of the PRD's
# July 2026 snapshot; OpenRouter passes these through with no inference markup.
# Verify against provider pages before relying on these for real budgeting.
MODEL_RATES: dict[str, tuple[float, float]] = {
    "anthropic/claude-haiku-4.5": (1.00, 5.00),
    "anthropic/claude-sonnet-5": (3.00, 15.00),
    "anthropic/claude-opus-4.8": (5.00, 25.00),
    "anthropic/claude-fable-5": (5.00, 25.00),
}
OPENROUTER_FEE = 0.055  # credit top-up fee applied to the LLM subtotal


def make_llm(model: str, temperature: float = 0.7, **kwargs) -> ChatOpenAI:
    """Build a ChatOpenAI client routed through OpenRouter for `model`."""
    settings = get_settings()
    if not settings.openrouter_api_key:
        raise RuntimeError(
            "OPENROUTER_API_KEY is not set — required for all LLM stages."
        )
    return ChatOpenAI(
        model=model,
        temperature=temperature,
        api_key=settings.openrouter_api_key,
        base_url=OPENROUTER_BASE_URL,
        default_headers={
            "HTTP-Referer": "https://github.com/erfansalehi/shopify-blog-pipeline",
            "X-Title": "Shopify Blog Pipeline",
        },
        **kwargs,
    )


def cost_for(model: str, input_tokens: int, output_tokens: int) -> float:
    """USD cost (pre-fee) for a single call given token counts."""
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
        """Total including the OpenRouter credit fee on the LLM subtotal."""
        return round(self.usd * (1 + OPENROUTER_FEE), 6)
