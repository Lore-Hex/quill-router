from __future__ import annotations

import logging
import threading
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
from trusted_router.storage_models import RateLimitHit
from trusted_router.storage_rate_limits import InMemoryRateLimits

log = logging.getLogger(__name__)

_CLIENT_EVENT_RATE_LIMITS = InMemoryRateLimits(lock=threading.RLock())


async def json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except JSONDecodeError as exc:
        raise api_error(400, "Malformed JSON", "bad_request") from exc
    if not isinstance(body, dict):
        raise api_error(400, "JSON body must be an object", "bad_request")
    return body


async def read_json_body_bounded(request: Request, max_bytes: int) -> bytes:
    """Read a request stream without ever buffering more than the allowed body."""
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > max_bytes:
            raise api_error(413, "Request body is too large", "payload_too_large")
        body.extend(chunk)
    return bytes(body)


def enforce_rate_limit(
    namespace: str,
    subject: str,
    limit: int,
    *,
    window_seconds: int,
) -> RateLimitHit | None:
    """Apply a bounded process-local rate limit, failing open on limiter errors."""
    if limit <= 0:
        return None
    try:
        return _CLIENT_EVENT_RATE_LIMITS.hit(
            namespace=namespace,
            subject=subject,
            limit=limit,
            window_seconds=window_seconds,
        )
    except Exception:  # noqa: BLE001 - telemetry must never depend on its limiter.
        log.exception(
            "client_events.rate_limit_unavailable",
            extra={"namespace": namespace},
        )
        return None


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
