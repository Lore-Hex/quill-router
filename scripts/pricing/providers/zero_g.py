"""0G router catalog, pricing, and account canary.

The authenticated OpenAI-compatible ``/v1/models`` endpoint is the source of
truth for every model available to TrustedRouter's unrestricted 0G account.
TrustedRouter uses ordinary 0G routing and deliberately makes no confidential
compute, attestation, or end-to-end-encryption claim for these routes.

Only chat models are published here. 0G's image and speech models require
different API surfaces and remain absent until those product paths exist.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from scripts.pricing.base import (
    ModelPrice,
    ProviderPricingResult,
    fetch_json,
    validate,
)
from scripts.pricing.manifest import (
    set_manifest_canary_state,
    write_discovered_chat_manifest,
)
from scripts.pricing.model_ids import canonicalize_unqualified_model_id
from scripts.pricing.openai_catalog import (
    dollars_per_token_to_micro_per_m,
    positive_int,
    probe_openai_chat,
)

SLUG = "zero-g"
BASE_URL = "https://router-api.0g.ai/v1"
URL = f"{BASE_URL}/models"
API_KEY_ENV = "ZERO_G_ALL_API_KEY"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "zero-g.json"
)
EXPECTED_MODELS = [
    "zero-g/0gm-1.0-35b-a3b",
    "anthropic/claude-opus-5",
    "minimax/minimax-m3",
    "moonshotai/kimi-k3",
    "openai/gpt-5.6-sol",
    "qwen/qwen3.7-plus",
    "z-ai/glm-5.2",
]
CANARY_MODEL = "0gm-1.0-35b-a3b"

_MODEL_ID_OVERRIDES = {
    "claude-opus-4-8": "anthropic/claude-opus-4.8",
    "qwen3-vl-30b": "qwen/qwen3-vl-30b-a3b-instruct",
}
_DISCOVERED_MANIFEST_ROWS: dict[str, dict[str, Any]] = {}
_LIVE_CANARY_OK = False
_LEGACY_PRIVATE_FIELDS = frozenset(
    {
        "tee_type",
        "tee_verifier",
        "private_provider_count",
    }
)


def _canonical_model_id(native_id: str) -> str | None:
    normalized = native_id.strip().casefold().replace("_", "-")
    if not normalized:
        return None
    if normalized in _MODEL_ID_OVERRIDES:
        return _MODEL_ID_OVERRIDES[normalized]
    if normalized.startswith("0gm-"):
        return f"zero-g/{normalized}"
    if normalized == "hy3":
        return "tencent/hy3"
    for prefix, author in (
        ("claude-", "anthropic"),
        ("gemini-", "google"),
        ("gpt-", "openai"),
        ("mistral-", "mistralai"),
        ("command-", "cohere"),
    ):
        if normalized.startswith(prefix):
            return f"{author}/{normalized}"
    return canonicalize_unqualified_model_id(normalized)


def _modalities(value: object, *, default: list[str]) -> list[str]:
    if not isinstance(value, list):
        return default
    allowed = {"text", "image", "audio", "video", "file"}
    result = list(
        dict.fromkeys(
            str(item).strip().casefold()
            for item in value
            if str(item).strip().casefold() in allowed
        )
    )
    return result or default


def _chat_model(row: dict[str, Any]) -> bool:
    return row.get("type") == "chatbot" or row.get("serviceType") == "chat"


def parse_catalog(
    payload: object,
) -> tuple[dict[str, ModelPrice], dict[str, dict[str, Any]]]:
    """Normalize 0G's authenticated router catalog into prepaid chat routes."""

    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise RuntimeError("0G /v1/models response has no data list")

    prices: dict[str, ModelPrice] = {}
    discovered: dict[str, dict[str, Any]] = {}
    for source in payload["data"]:
        if not isinstance(source, dict) or not _chat_model(source):
            continue
        native_id = source.get("id")
        if not isinstance(native_id, str):
            continue
        model_id = _canonical_model_id(native_id)
        if model_id is None:
            continue

        pricing = source.get("pricing_usd")
        if not isinstance(pricing, dict):
            continue
        prompt = dollars_per_token_to_micro_per_m(pricing.get("prompt"))
        completion = dollars_per_token_to_micro_per_m(pricing.get("completion"))
        cached = dollars_per_token_to_micro_per_m(pricing.get("cached_prompt"))
        if prompt is None or completion is None or prompt <= 0 or completion <= 0:
            continue
        prices[model_id] = ModelPrice(
            prompt_micro_per_m=prompt,
            completion_micro_per_m=completion,
            prompt_cached_micro_per_m=cached,
        )

        architecture = source.get("architecture")
        if not isinstance(architecture, dict):
            architecture = {}
        parameters = source.get("supported_parameters")
        parameter_set = (
            {str(item).casefold() for item in parameters} if isinstance(parameters, list) else set()
        )
        features = ["chat", "completion"]
        if "tools" in parameter_set:
            features.append("tools")
        if "response_format" in parameter_set:
            features.extend(["json_mode", "structured_outputs"])
        if parameter_set & {"thinking", "enable_thinking", "reasoning_effort"}:
            features.append("reasoning")
        if cached is not None:
            features.append("prompt_caching")

        display_name = str(source.get("name") or native_id)
        discovered[model_id] = {
            "id": model_id,
            "upstream_id": native_id,
            "display_name": display_name,
            "title": display_name,
            "model_type": "chat",
            "context_length": positive_int(source.get("context_length")) or 131_072,
            "max_output_tokens": positive_int(source.get("max_completion_tokens")) or 32_768,
            "input_modalities": _modalities(architecture.get("input_modalities"), default=["text"]),
            "output_modalities": _modalities(
                architecture.get("output_modalities"), default=["text"]
            ),
            "endpoints": ["chat/completions"],
            "supported_features": features,
            "supported_parameters": sorted(parameter_set),
            "status": 1,
            "routable": _LIVE_CANARY_OK,
            "routable_reason": None if _LIVE_CANARY_OK else "provider-canary-failed",
            "trust_mode": "standard",
            "verifiability": None,
            "tee_attested": False,
            "provider_route_count": positive_int(source.get("provider_count")) or 1,
            "pricing_policy": "authenticated-router-catalog",
        }
    return prices, discovered


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_MANIFEST_ROWS, _LIVE_CANARY_OK  # noqa: PLW0603

    api_key = os.environ.get(API_KEY_ENV)
    if not api_key:
        raise RuntimeError(f"{API_KEY_ENV} is required for 0G model discovery")
    payload = fetch_json(
        URL,
        extra_headers={"Authorization": f"Bearer {api_key}"},
    )
    prices, discovered = parse_catalog(payload)
    errors = validate(prices, EXPECTED_MODELS)
    if errors:
        raise RuntimeError("; ".join(errors))

    _LIVE_CANARY_OK = probe_openai_chat(
        base_url=BASE_URL,
        api_key=api_key,
        model=CANARY_MODEL,
        expected_content="PONG",
        max_tokens=256,
    )
    for row in discovered.values():
        row["routable"] = _LIVE_CANARY_OK
        if _LIVE_CANARY_OK:
            row.pop("routable_reason", None)
        else:
            row["routable_reason"] = "provider-canary-failed"
    _DISCOVERED_MANIFEST_ROWS = discovered

    return ProviderPricingResult(
        slug=SLUG,
        prices=prices,
        source="api",
        fetched_url=URL,
        notes=[
            f"discovered {len(discovered)} authenticated 0G chat models",
            f"unrestricted account canary {'passed' if _LIVE_CANARY_OK else 'failed'}",
            "standard routing; no confidential-compute, attestation, or E2EE claim",
        ],
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    notes = write_discovered_chat_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        discovered_rows=_DISCOVERED_MANIFEST_ROWS,
        source_url=URL,
    )
    raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    raw["_about"] = (
        "Provider-native unrestricted 0G router catalog. Chat model availability, "
        "capabilities, and account-billable prices refresh hourly from the "
        "authenticated /v1/models endpoint. TrustedRouter makes no ZDR, "
        "confidential-compute, attestation, or end-to-end-encryption claim for "
        "these standard routes."
    )
    for row in raw.get("models", []):
        if not isinstance(row, dict):
            continue
        for field in _LEGACY_PRIVATE_FIELDS:
            row.pop(field, None)
    MANIFEST_PATH.write_text(
        json.dumps(raw, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    set_manifest_canary_state(MANIFEST_PATH, healthy=_LIVE_CANARY_OK)
    return notes
