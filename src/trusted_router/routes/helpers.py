from __future__ import annotations

from json import JSONDecodeError
from typing import Any

from fastapi import Request

from trusted_router.catalog import Model
from trusted_router.errors import api_error
from trusted_router.money import (
    dollars_to_microdollars,
    microdollars_to_float,
    token_cost_microdollars,
)
from trusted_router.pricing import resolve_request_rates


async def json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except JSONDecodeError as exc:
        raise api_error(400, "Malformed JSON", "bad_request") from exc
    if not isinstance(body, dict):
        raise api_error(400, "JSON body must be an object", "bad_request")
    return body


def cost_microdollars(
    model: Model,
    input_tokens: int,
    output_tokens: int,
    *,
    cached_input_tokens: int = 0,
) -> int:
    """Compute the per-request cost in microdollars.

    Picks the right price tier based on `input_tokens` (the prompt
    size). For models with a single uncapped tier (the common case),
    this returns the headline rate × tokens. For Gemini-2.5-Pro-shape
    models with context-conditional tiers, prompts ≤200k pay the low
    tier and prompts >200k pay the high tier — both prompt AND
    completion rates flip to the high tier when the prompt does.

    `cached_input_tokens` is the number of input tokens upstream
    reported as cache hits. Those tokens bill at the cached rate (if
    the tier defines one) and the remainder at the full prompt rate.
    Most providers offer a 50-90% discount on cache hits; for example
    Anthropic Sonnet at $3/M input drops to $0.30/M for cache reads.
    Convention: `input_tokens` is the TOTAL prompt size and
    `cached_input_tokens` is a subset of it (NOT additional). This
    matches how OpenAI, Anthropic, Gemini, DeepSeek all report.

    `model.price_tiers` is empty only for hand-coded meta-models
    (`trustedrouter/auto`, etc.) whose flat rates are 0 anyway. Fall
    back to the headline-rate fields in that case.
    """
    cached_input_tokens = max(0, min(cached_input_tokens, input_tokens))
    uncached_input_tokens = input_tokens - cached_input_tokens

    rates = resolve_request_rates(
        model.price_tiers,
        headline_prompt_micro_per_m=model.prompt_price_microdollars_per_million_tokens,
        headline_completion_micro_per_m=model.completion_price_microdollars_per_million_tokens,
        total_prompt_tokens=input_tokens,
    )
    if not model.price_tiers:
        return (
            token_cost_microdollars(
                input_tokens,
                rates.prompt_price_microdollars_per_million_tokens,
            )
            + token_cost_microdollars(
                output_tokens,
                rates.completion_price_microdollars_per_million_tokens,
            )
        )
    cached_rate = (
        rates.prompt_cached_price_microdollars_per_million_tokens
        if rates.prompt_cached_price_microdollars_per_million_tokens is not None
        else rates.prompt_price_microdollars_per_million_tokens
    )
    return (
        token_cost_microdollars(
            uncached_input_tokens,
            rates.prompt_price_microdollars_per_million_tokens,
        )
        + token_cost_microdollars(cached_input_tokens, cached_rate)
        + token_cost_microdollars(
            output_tokens,
            rates.completion_price_microdollars_per_million_tokens,
        )
    )


def integer_body_field(
    body: dict[str, Any],
    field: str,
    *,
    default: int,
    minimum: int,
) -> int:
    raw = body.get(field, default)
    if raw is None:
        raw = default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise api_error(400, f"{field} must be an integer", "bad_request") from exc
    if value < minimum:
        raise api_error(400, f"{field} must be at least {minimum}", "bad_request")
    return value


def float_body_field(
    body: dict[str, Any],
    field: str,
    *,
    default: float,
    minimum: float,
) -> float:
    raw = body.get(field, default)
    if raw is None:
        raw = default
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise api_error(400, f"{field} must be a number", "bad_request") from exc
    if value < minimum:
        raise api_error(400, f"{field} must be at least {minimum}", "bad_request")
    return value


def money_body_field_microdollars(
    body: dict[str, Any],
    field: str,
    *,
    default: object,
    minimum_microdollars: int,
) -> int:
    raw = body.get(field, default)
    if raw is None:
        raw = default
    try:
        value = dollars_to_microdollars(raw)
    except ValueError as exc:
        raise api_error(400, f"{field} must be a dollar amount", "bad_request") from exc
    if value < minimum_microdollars:
        minimum = microdollars_to_float(minimum_microdollars)
        raise api_error(400, f"{field} must be at least {minimum:g}", "bad_request")
    return value
