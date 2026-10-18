"""
Estimated LLM cost per call.

Prices are USD per million tokens (input, output), taken from the providers'
public list prices when this table was written. They change; verify against
the provider pricing pages and override or extend them with the
LLM_PRICE_OVERRIDES setting, e.g.
LLM_PRICE_OVERRIDES='{"gpt-4o-mini": [0.15, 0.6]}'.

The estimate prices every input token at the uncached rate, so it slightly
overstates cost when prompt caching kicks in.
"""

from .base import LLMUsage

PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    # OpenAI
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    # Anthropic
    "claude-opus-5-5": (4.00, 20.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    # Offline
    "fake-heuristic-v1": (0.0, 0.0),
}


def _lookup(model: str, prices: dict[str, tuple[float, float]]) -> tuple[float, float] | None:
    if model in prices:
        return prices[model]
    # Providers often report dated snapshots (e.g. gpt-4o-mini-2024-07-18):
    # fall back to the longest known prefix.
    candidates = [name for name in prices if model.startswith(name + "-")]
    return prices[max(candidates, key=len)] if candidates else None


def estimate_cost_usd(
    usage: LLMUsage, overrides: dict[str, tuple[float, float]] | None = None
) -> float | None:
    """Estimated cost of one call, or None when the model's price is unknown."""
    prices = {**PRICES_PER_MTOK, **(overrides or {})}
    price = _lookup(usage.model, prices)
    if price is None:
        return None
    input_price, output_price = price
    cost = (usage.input_tokens * input_price + usage.output_tokens * output_price) / 1_000_000
    return round(cost, 8)


def usage_record(
    usage: LLMUsage, overrides: dict[str, tuple[float, float]] | None = None
) -> dict:
    """JSON-serialisable usage summary stored in audit log payloads."""
    return {
        "provider": usage.provider,
        "model": usage.model,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "latency_ms": usage.latency_ms,
        "cost_usd": estimate_cost_usd(usage, overrides),
    }
