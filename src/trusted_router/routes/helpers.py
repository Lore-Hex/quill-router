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


async def json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except JSONDecodeError as exc:
        raise api_error(400, "Malformed JSON", "bad_request") from exc
    if not isinstance(body, dict):
        raise api_error(400, "JSON body must be an object", "bad_request")
    return body


def cost_microdollars(model: Model, input_tokens: int, output_tokens: int) -> int:
    return (
        token_cost_microdollars(input_tokens, model.prompt_price_microdollars_per_million_tokens)
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
