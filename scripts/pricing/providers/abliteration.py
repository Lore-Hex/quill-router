"""Abliteration.ai's authenticated, per-token priced catalog (not Abliterate.ai)."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from scripts.pricing.providers._direct_openai import DirectOpenAIProvider, DirectOpenAIProviderSpec

SLUG = "abliteration"
BASE_URL = "https://api.abliteration.ai/v1"
URL = f"{BASE_URL}/models"
MANIFEST_PATH = Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/abliteration.json"
MANIFEST_STALE_FALLBACK = True


def canonical_model_id(native_id: str) -> str | None:
    if re.fullmatch(r"[a-z0-9][a-z0-9._-]*", native_id):
        return f"{SLUG}/{native_id}"
    return None


def _amount(pricing: dict[str, Any], field: str) -> Decimal:
    try:
        value = Decimal(str(pricing[field]))
    except (KeyError, InvalidOperation, ValueError) as exc:
        raise RuntimeError(f"abliteration: invalid {field} price") from exc
    if not value.is_finite() or value < 0 or value * 10**12 != (value * 10**12).to_integral_value():
        raise RuntimeError(f"abliteration: invalid {field} price or precision")
    return value


def normalize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        native_id = row.get("id")
        if not isinstance(native_id, str) or canonical_model_id(native_id) is None or native_id in seen:
            raise RuntimeError("abliteration: invalid or duplicate model ID")
        seen.add(native_id)
        access = row.get("access")
        if not isinstance(access, dict):
            raise RuntimeError("abliteration: missing access declaration")
        if access.get("available") is not True or access.get("locked") is not False or row.get("is_ready") is not True:
            continue
        pricing = row.get("pricing")
        if not isinstance(pricing, dict):
            raise RuntimeError("abliteration: missing per-token pricing")
        if pricing.get("currency", "USD") != "USD" or pricing.get("unit", "per_token") != "per_token":
            raise RuntimeError("abliteration: unexpected pricing currency or unit")
        prompt, completion, cached = (_amount(pricing, field) for field in ("prompt", "completion", "input_cache_read"))
        if prompt <= 0 or completion <= 0 or cached > prompt:
            raise RuntimeError("abliteration: invalid input/output/cache prices")
        # Web tools have a separate meter and are not covered by the ZDR policy.
        for field in ("request", "image", "web_search", "input_cache_write"):
            if field in pricing and _amount(pricing, field) != 0:
                raise RuntimeError(f"abliteration: unsupported {field} charge")
        features = [feature for feature in row.get("supported_features", []) if feature != "web_search"]
        if "prompt_caching" not in features:
            features.append("prompt_caching")
        normalized.append({**row, "supported_features": features})
    return normalized


CATALOG = DirectOpenAIProvider(
    DirectOpenAIProviderSpec(
        slug=SLUG, base_url=BASE_URL, api_key_env="ABLITERATION_API_KEY",
        explicit_model_map={}, model_id_resolver=canonical_model_id,
        normalize_rows=normalize_rows,
        canary_max_tokens=512, canary_expected_content="PONG",
        canary_require_usage=True, canary_require_message=True,
        canary_extra_body={"reasoning_effort": "low"},
    ), manifest_path=MANIFEST_PATH,
)
UPSTREAM_ID_MAP = CATALOG.upstream_id_map
fetch = CATALOG.fetch
write_provider_manifest = CATALOG.write_provider_manifest
