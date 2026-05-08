from __future__ import annotations

from json import JSONDecodeError
from typing import Any

from fastapi import Request

from trusted_router.catalog import Model, select_price_tier
from trusted_router.errors import api_error
from trusted_router.money import (
    dollars_to_microdollars,
    microdollars_to_float,
    token_cost_microdollars,
)


async def json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except JSONDecodeError as exc:
        raise api_error(400, "Malformed JSON", "bad_request") from exc
    if not isinstance(body, dict):
        raise api_error(400, "JSON body must be an object", "bad_request")
    return body


def cost_microdollars(model: Model, input_tokens: int, output_tokens: int) -> int:
    """Compute the per-request cost in microdollars.

    Picks the right price tier based on `input_tokens` (the prompt
    size). For models with a single uncapped tier (the common case),
    this returns the headline rate × tokens. For Gemini-2.5-Pro-shape
    models with context-conditional tiers, prompts ≤200k pay the low
    tier and prompts >200k pay the high tier — both prompt AND
    completion rates flip to the high tier when the prompt does.

    `model.price_tiers` is empty only for hand-coded meta-models
    (`trustedrouter/auto`, etc.) whose flat rates are 0 anyway. Fall
    back to the headline-rate fields in that case.
    """
    tiers = model.price_tiers
    if tiers:
        tier = select_price_tier(tiers, input_tokens)
        return (
            token_cost_microdollars(
                input_tokens, tier.prompt_price_microdollars_per_million_tokens
            )
            + token_cost_microdollars(
                output_tokens,
                tier.completion_price_microdollars_per_million_tokens,
            )
        )
    return (
        token_cost_microdollars(
            input_tokens, model.prompt_price_microdollars_per_million_tokens
        )
        + token_cost_microdollars(
            output_tokens,
            model.completion_price_microdollars_per_million_tokens,
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
