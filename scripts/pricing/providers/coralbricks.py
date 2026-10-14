"""CoralBricks' live catalog, using the enclave's no-paid-retention tariff."""

from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from scripts.pricing.providers._direct_openai import DirectOpenAIProvider, DirectOpenAIProviderSpec

SLUG = "coralbricks"
BASE_URL = "https://inference.coralbricks.ai/v1"
URL = f"{BASE_URL}/models"
MANIFEST_PATH = Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/coralbricks.json"
MANIFEST_STALE_FALLBACK = True
EXPLICIT_MODEL_MAP = {
    "glm-5.3-fast": "z-ai/glm-5.3",
    "deepseek-v4.1-flash-fast": "deepseek/deepseek-v4.1-flash",
}


def _per_token(value: object) -> str:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise RuntimeError("coralbricks: missing or invalid USD-per-million price") from None
    if not amount.is_finite() or amount < 0:
        raise RuntimeError("coralbricks: invalid USD-per-million price")
    return str(amount / Decimal(1_000_000))


def normalize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    seen: set[str] = set()
    for source in rows:
        native = source.get("id")
        if not isinstance(native, str) or not native.strip() or native in seen:
            raise RuntimeError("coralbricks: missing or duplicate model ID")
        seen.add(native)
        if source.get("supports_chat") is not True:
            continue
        prices = source.get("pricing")
        if not isinstance(prices, dict):
            raise RuntimeError("coralbricks: missing pricing")
        # Paid retention is explicitly disabled by the enclave. Do not add the
        # optional cache-write surcharge to ordinary input or free cache reads.
        _per_token(prices.get("cache_write_per_m"))
        features = ["streaming", "prompt_caching"]
        if source.get("supports_tools") is True:
            features.append("tools")
        if source.get("supports_reasoning") is True:
            features.append("reasoning")
        normalized.append({
            **source,
            "pricing": {
                "prompt": _per_token(prices.get("input_per_m")),
                "completion": _per_token(prices.get("output_per_m")),
                "input_cache_read": _per_token(prices.get("cached_input_per_m")),
            },
            "input_modalities": ["text", "image"] if source.get("supports_image_input") is True else ["text"],
            "output_modalities": ["text"],
            "supported_features": features,
        })
    return normalized


CATALOG = DirectOpenAIProvider(
    DirectOpenAIProviderSpec(
        slug=SLUG, base_url=BASE_URL, api_key_env="CORALBRICKS_API_KEY",
        explicit_model_map=EXPLICIT_MODEL_MAP, namespace_unqualified=SLUG,
        normalize_rows=normalize_rows,
        canary_max_tokens=512, canary_expected_content="PONG",
        canary_require_usage=True, canary_require_message=True,
        canary_prompt="Reply with exactly PONG and nothing else.",
        canary_extra_body={"temperature": 0, "reasoning_effort": "low", "prompt_cache_retention": "off"},
    ), manifest_path=MANIFEST_PATH,
)
UPSTREAM_ID_MAP = CATALOG.upstream_id_map
fetch = CATALOG.fetch
write_provider_manifest = CATALOG.write_provider_manifest
