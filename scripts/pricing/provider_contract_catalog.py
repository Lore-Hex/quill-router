"""Strict parsing for TrustedRouter's canonical provider catalog contract."""

from __future__ import annotations

import re
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from scripts.pricing.base import ModelPrice
from scripts.pricing.model_ids import remember_upstream_id

_MODEL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._/-]*$")
_OWNER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_TOP_LEVEL_FIELDS = frozenset({"object", "data"})
_MODEL_FIELDS = frozenset(
    {
        "id",
        "object",
        "owned_by",
        "name",
        "type",
        "context_length",
        "max_output_tokens",
        "endpoints",
        "input_modalities",
        "output_modalities",
        "capabilities",
        "pricing",
        "lifecycle",
    }
)
_CAPABILITY_FIELDS = frozenset(
    {"streaming", "tools", "structured_output", "reasoning", "prompt_caching"}
)
_PRICING_FIELDS = frozenset(
    {
        "currency",
        "unit",
        "input",
        "output",
        "cached_input",
        "cache_write",
        "minimum_request",
    }
)
_LIFECYCLE_FIELDS = frozenset(
    {"status", "deprecation_at", "retirement_at", "replacement_model_id"}
)
_ENDPOINTS = frozenset({"chat/completions", "responses"})
_INPUT_MODALITIES = frozenset({"text", "image", "audio", "video", "file"})
_OUTPUT_MODALITIES = frozenset({"text", "image", "audio"})
_LIFECYCLE_STATUSES = frozenset({"active", "deprecated", "retired"})
_MICRODOLLARS_PER_DOLLAR = Decimal("1000000")


def _require_exact_fields(
    value: object,
    expected: frozenset[str],
    *,
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must be an object")
    keys = frozenset(str(key) for key in value)
    missing = sorted(expected - keys)
    extra = sorted(keys - expected)
    if missing or extra:
        raise RuntimeError(f"{label} fields invalid: missing={missing}, extra={extra}")
    return value


def _require_string(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"{label} must be a non-empty string")
    return value


def _require_positive_int(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RuntimeError(f"{label} must be a positive integer")
    return value


def _require_string_set(
    value: object,
    *,
    allowed: frozenset[str],
    label: str,
) -> list[str]:
    if not isinstance(value, list) or not value:
        raise RuntimeError(f"{label} must be a non-empty array")
    if any(not isinstance(item, str) for item in value):
        raise RuntimeError(f"{label} entries must be strings")
    parsed = [str(item) for item in value]
    if len(parsed) != len(set(parsed)):
        raise RuntimeError(f"{label} entries must be unique")
    unsupported = sorted(set(parsed) - allowed)
    if unsupported:
        raise RuntimeError(f"{label} has unsupported values: {unsupported}")
    return parsed


def _require_bool(value: object, *, label: str) -> bool:
    if not isinstance(value, bool):
        raise RuntimeError(f"{label} must be a boolean")
    return value


def _decimal(value: object, *, label: str, nullable: bool = False) -> Decimal | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"(0|[1-9][0-9]*)(\.[0-9]+)?", value):
        raise RuntimeError(f"{label} must be a non-negative decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise RuntimeError(f"{label} is not a valid decimal") from exc
    if not parsed.is_finite() or parsed < 0:
        raise RuntimeError(f"{label} must be finite and non-negative")
    return parsed


def _microdollars_per_million(value: Decimal, *, label: str) -> int:
    scaled = value * _MICRODOLLARS_PER_DOLLAR
    rounded = scaled.to_integral_value(ROUND_HALF_UP)
    if scaled != rounded:
        raise RuntimeError(f"{label} exceeds microdollar-per-million precision")
    return int(rounded)


def _nullable_timestamp(value: object, *, label: str) -> str | None:
    if value is None:
        return None
    text = _require_string(value, label=label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise RuntimeError(f"{label} must include a timezone")
    return text


def discover_provider_contract_catalog(
    payload: object,
    *,
    upstream_id_map: dict[str, str],
) -> tuple[dict[str, ModelPrice], dict[str, dict[str, Any]]]:
    """Validate and normalize one canonical provider `/v1/models` response."""

    top = _require_exact_fields(payload, _TOP_LEVEL_FIELDS, label="catalog")
    if top["object"] != "list":
        raise RuntimeError("catalog.object must equal 'list'")
    source_rows = top["data"]
    if not isinstance(source_rows, list):
        raise RuntimeError("catalog.data must be an array")

    prices: dict[str, ModelPrice] = {}
    discovered: dict[str, dict[str, Any]] = {}
    for index, source in enumerate(source_rows):
        label = f"catalog.data[{index}]"
        row = _require_exact_fields(source, _MODEL_FIELDS, label=label)
        model_id = _require_string(row["id"], label=f"{label}.id")
        if _MODEL_ID_RE.fullmatch(model_id) is None:
            raise RuntimeError(f"{label}.id is not a canonical model id")
        if row["object"] != "model" or row["type"] != "chat":
            raise RuntimeError(f"{label} must describe a chat model")
        owner = _require_string(row["owned_by"], label=f"{label}.owned_by")
        if _OWNER_RE.fullmatch(owner) is None:
            raise RuntimeError(f"{label}.owned_by is invalid")
        name = _require_string(row["name"], label=f"{label}.name")
        context_length = _require_positive_int(
            row["context_length"], label=f"{label}.context_length"
        )
        max_output_tokens = _require_positive_int(
            row["max_output_tokens"], label=f"{label}.max_output_tokens"
        )
        endpoints = _require_string_set(
            row["endpoints"], allowed=_ENDPOINTS, label=f"{label}.endpoints"
        )
        input_modalities = _require_string_set(
            row["input_modalities"],
            allowed=_INPUT_MODALITIES,
            label=f"{label}.input_modalities",
        )
        output_modalities = _require_string_set(
            row["output_modalities"],
            allowed=_OUTPUT_MODALITIES,
            label=f"{label}.output_modalities",
        )

        capabilities = _require_exact_fields(
            row["capabilities"], _CAPABILITY_FIELDS, label=f"{label}.capabilities"
        )
        parsed_capabilities = {
            key: _require_bool(value, label=f"{label}.capabilities.{key}")
            for key, value in capabilities.items()
        }
        pricing = _require_exact_fields(
            row["pricing"], _PRICING_FIELDS, label=f"{label}.pricing"
        )
        if pricing["currency"] != "USD" or pricing["unit"] != "per_1m_tokens":
            raise RuntimeError(f"{label}.pricing must use USD per_1m_tokens")
        prompt = _decimal(pricing["input"], label=f"{label}.pricing.input")
        completion = _decimal(pricing["output"], label=f"{label}.pricing.output")
        cached = _decimal(
            pricing["cached_input"],
            label=f"{label}.pricing.cached_input",
            nullable=True,
        )
        cache_write = _decimal(
            pricing["cache_write"],
            label=f"{label}.pricing.cache_write",
            nullable=True,
        )
        minimum_request = _decimal(
            pricing["minimum_request"], label=f"{label}.pricing.minimum_request"
        )
        if prompt is None or completion is None or minimum_request is None:
            raise RuntimeError(f"{label}.pricing token prices must not be null")
        if minimum_request != 0:
            raise RuntimeError(f"{label}.pricing.minimum_request is not supported yet")
        if cache_write not in (None, Decimal(0)):
            raise RuntimeError(f"{label}.pricing.cache_write is not supported yet")
        if parsed_capabilities["prompt_caching"] != (cached is not None):
            raise RuntimeError(
                f"{label}.capabilities.prompt_caching must match pricing.cached_input"
            )

        lifecycle = _require_exact_fields(
            row["lifecycle"], _LIFECYCLE_FIELDS, label=f"{label}.lifecycle"
        )
        lifecycle_status = _require_string(
            lifecycle["status"], label=f"{label}.lifecycle.status"
        )
        if lifecycle_status not in _LIFECYCLE_STATUSES:
            raise RuntimeError(f"{label}.lifecycle.status is invalid")
        deprecation_at = _nullable_timestamp(
            lifecycle["deprecation_at"], label=f"{label}.lifecycle.deprecation_at"
        )
        retirement_at = _nullable_timestamp(
            lifecycle["retirement_at"], label=f"{label}.lifecycle.retirement_at"
        )
        replacement = lifecycle["replacement_model_id"]
        if replacement is not None:
            replacement = _require_string(
                replacement, label=f"{label}.lifecycle.replacement_model_id"
            )
            if _MODEL_ID_RE.fullmatch(replacement) is None:
                raise RuntimeError(f"{label}.lifecycle.replacement_model_id is invalid")

        if lifecycle_status == "retired":
            continue
        if "chat/completions" not in endpoints or "text" not in output_modalities:
            continue

        supported_features = ["chat", "completion"]
        if parsed_capabilities["tools"]:
            supported_features.append("tools")
        if parsed_capabilities["structured_output"]:
            supported_features.extend(["json_mode", "structured_outputs"])
        if parsed_capabilities["reasoning"]:
            supported_features.append("reasoning")

        remember_upstream_id(upstream_id_map, model_id, model_id)
        discovered[model_id] = {
            "id": model_id,
            "upstream_id": model_id,
            "display_name": name,
            "context_length": context_length,
            "max_output_tokens": max_output_tokens,
            "endpoints": endpoints,
            "input_modalities": input_modalities,
            "output_modalities": output_modalities,
            "supported_features": supported_features,
            "lifecycle_status": lifecycle_status,
            "deprecation_at": deprecation_at,
            "retirement_at": retirement_at,
            "replacement_model_id": replacement,
            "routable": True,
        }
        prices[model_id] = ModelPrice(
            _microdollars_per_million(prompt, label=f"{label}.pricing.input"),
            _microdollars_per_million(completion, label=f"{label}.pricing.output"),
            prompt_cached_micro_per_m=(
                _microdollars_per_million(
                    cached, label=f"{label}.pricing.cached_input"
                )
                if cached is not None
                else None
            ),
        )
    return prices, discovered
