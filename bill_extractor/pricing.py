"""Token prices, for estimating cost per bill in evaluations.

US dollars per million tokens, Anthropic first-party API list prices (checked November 2026).
Prices change: update this table rather than trusting it blindly. Models not listed (local
Ollama, vLLM, unknown OpenAI models) report no cost estimate rather than a wrong one.
"""

PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    # model: (input, output)
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}


def price_for(model: str) -> tuple[float, float] | None:
    return PRICES_PER_MTOK.get(model)


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """Dollar cost of one call, or None when the model has no known price."""
    price = price_for(model)
    if price is None:
        return None
    return (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000
