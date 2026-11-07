"""Baseten — provider-native model catalog and pricing.

Baseten publishes an OpenAI-compatible inference API at
https://inference.baseten.co/v1. Its `/models` response includes exact native
model ids, context lengths, and per-token prices as decimal strings. This
module converts those rates into TrustedRouter's integer microdollars per
million tokens and keeps `UPSTREAM_ID_MAP` in sync so the enclave calls the
provider-native id, not a guessed lowercase slug.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx

from scripts.pricing.base import (
    PROVIDER_FETCH_TIMEOUT,
    PROVIDER_FETCH_TRANSPORT_RETRIES,
    PROVIDER_FETCH_UA,
    ModelPrice,
    ProviderPricingResult,
    validate,
)
from scripts.pricing.manifest import apply_canary_results, write_discovered_chat_manifest
from scripts.pricing.openai_catalog import discover_openai_chat_catalog
from trusted_router.provider_contracts import (
    BASETEN_SYSTEMONE_HOLD_REASON,
    BASETEN_SYSTEMONE_PREFIX,
    baseten_systemone_model_reviewed,
)
from trusted_router.provider_lifecycle import provider_model_retired

SLUG = "baseten"
URL = "https://inference.baseten.co/v1/models"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "baseten.json"
)

EXPECTED_MODELS = [
    "deepseek/deepseek-v4-pro-0813",
    "z-ai/glm-5.2",
    "z-ai/glm-5.2-fast",
    "moonshotai/kimi-k3",
    "z-ai/glm-5.3",
]

_NATIVE_TO_OR_ID = {
    "openai/gpt-oss-120b": "openai/gpt-oss-120b",
    "zai-org/GLM-4.7": "z-ai/glm-4.7",
    "moonshotai/Kimi-K2.5": "moonshotai/kimi-k2.5",
    "zai-org/GLM-5": "z-ai/glm-5",
    "nvidia/Nemotron-120B-A12B": "nvidia/nemotron-120b-a12b",
    "zai-org/GLM-5.1": "z-ai/glm-5.1",
    "moonshotai/Kimi-K2.6": "moonshotai/kimi-k2.6",
    "deepseek-ai/DeepSeek-V4-Pro": "deepseek/deepseek-v4-pro",
    "deepseek-ai/DeepSeek-V4-Pro-0813": "deepseek/deepseek-v4-pro-0813",
    "nvidia/NVIDIA-Nemotron-3-Ultra-550B-A55B": "nvidia/nemotron-3-ultra-550b-a55b",
    "zai-org/GLM-5.2": "z-ai/glm-5.2",
    "zai-org/GLM-5.2-Fast": "z-ai/glm-5.2-fast",
    "moonshotai/Kimi-K2.7-Code": "moonshotai/kimi-k2.7-code",
    "thinkingmachines/inkling": "thinkingmachines/inkling-1m",
    "thinkingmachines/inkling-small": "thinkingmachines/inkling-small",
}

UPSTREAM_ID_MAP = {or_id: native_id for native_id, or_id in _NATIVE_TO_OR_ID.items()}
_DISCOVERED_MANIFEST_ROWS: dict[str, dict[str, Any]] = {}
SYSTEMONE_URL = "https://inference.baseten.co/v1/systemone"
SYSTEMONE_DOCS_URL = "https://docs.baseten.co/reference/inference-api/system-one"
DECISION_BILLING_HOLD_REASON = "decision-billing-contract-unsupported"
_DECISION_EXAMPLE_STATE = "Mia owns a red bicycle."
_DECISION_EXAMPLE_QUESTIONS = {
    "color": {
        "type": "choice",
        "instructions": "Which color is the bicycle?",
        "criteria": {"red": None, "blue": None},
    }
}


def _is_systemone(row: dict[str, Any]) -> bool:
    return str(row.get("upstream_id") or "").startswith(BASETEN_SYSTEMONE_PREFIX) or str(
        row.get("id") or ""
    ).startswith(BASETEN_SYSTEMONE_PREFIX)


def _held_systemone_ids(rows: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Unreviewed System One IDs keep the operator hold across every refresh."""
    return {
        model_id: BASETEN_SYSTEMONE_HOLD_REASON
        for model_id, row in rows.items()
        if _is_systemone(row)
        and not baseten_systemone_model_reviewed(model_id, str(row.get("upstream_id") or model_id))
    }


def probe_systemone(api_key: str, upstream_id: str) -> bool:
    """One tiny System One decision; checks identity, the answer and usage."""
    try:
        response = httpx.post(
            SYSTEMONE_URL,
            timeout=30,
            headers={"Authorization": f"Bearer {api_key}", "User-Agent": PROVIDER_FETCH_UA},
            json={"model": upstream_id, "state": _DECISION_EXAMPLE_STATE, "questions": _DECISION_EXAMPLE_QUESTIONS},
        )
        response.raise_for_status()
        body = response.json()
        usage = body["usage"]
        return bool(
            body["model"] == upstream_id
            and body["answers"]["color"]["choice"] == "red"
            and type(usage["input_tokens"]) is int
            and usage["input_tokens"] > 0
            and type(usage["output_tokens"]) is int
            and usage["output_tokens"] >= 0
        )
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return False


def classify_systemone_rows(
    prices: dict[str, ModelPrice],
    discovered: dict[str, dict[str, Any]],
    api_key: str | None,
) -> tuple[set[str], set[str]]:
    """Turn System One rows into decide routes; never chat.

    Baseten lists these in the chat /models feed with a text modality, but
    they are served only by POST /v1/systemone. Reviewed IDs with an
    input-only tariff are canaried on that endpoint every refresh (a tiny
    catalog), and become routable only when the canary passes.
    """
    checked: set[str] = set()
    healthy: set[str] = set()
    for model_id, row in discovered.items():
        if not _is_systemone(row):
            continue
        row.update(
            model_type="decision",
            endpoints=["decide"],
            # The gateway sends decision state as text. Baseten takes images
            # only inside a conversation state, which this route does not offer.
            input_modalities=["text"],
            output_modalities=["decision"],
            supported_features=[],
            supported_sampling_parameters=[],
        )
        if not baseten_systemone_model_reviewed(model_id, str(row.get("upstream_id") or model_id)):
            row.update(routable=False, routable_reason=BASETEN_SYSTEMONE_HOLD_REASON)
            continue
        price = prices.get(model_id)
        if price is None or len(price.tiers) != 1 or price.completion_micro_per_m != 0 or not price.prompt_micro_per_m:
            # The published decision contract is input-only. Anything else is
            # a different tariff that the input-only settlement cannot bill.
            row.update(routable=False, routable_reason=DECISION_BILLING_HOLD_REASON)
            prices.pop(model_id, None)
            continue
        # Decision rows carry no cache tariff.
        prices[model_id] = ModelPrice(price.prompt_micro_per_m, 0)
        row["documentation"] = {
            "description": f"Baseten-hosted System One decision model {row.get('display_name') or model_id}. Returns typed answers and probabilities, not chat text.",
            "input_format": "POST /v1/decide: text state and boolean/noul, choice or score questions. Text-only on this route; no streaming.",
            "output_format": "Verified answers and probabilities with inputTokens usage. Input-only billing; output tokens are free.",
            "example_input": json.dumps({"model": model_id, "state": _DECISION_EXAMPLE_STATE, "questions": _DECISION_EXAMPLE_QUESTIONS}),
        }
        checked.add(model_id)
        if api_key and probe_systemone(api_key, str(row["upstream_id"])):
            healthy.add(model_id)
    apply_canary_results(discovered, checked_model_ids=checked, healthy_model_ids=healthy)
    return checked, healthy


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_MANIFEST_ROWS  # noqa: PLW0603

    api_key = os.environ.get("BASETEN_API_KEY")
    headers = {"User-Agent": PROVIDER_FETCH_UA, "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    transport = httpx.HTTPTransport(retries=PROVIDER_FETCH_TRANSPORT_RETRIES)
    with httpx.Client(
        timeout=PROVIDER_FETCH_TIMEOUT,
        follow_redirects=True,
        transport=transport,
    ) as client:
        response = client.get(URL, headers=headers)
        response.raise_for_status()
        payload = response.json()

    rows = payload.get("data") if isinstance(payload, dict) else payload
    source_rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    prices, discovered = discover_openai_chat_catalog(
        source_rows,
        explicit_map=_NATIVE_TO_OR_ID,
        upstream_id_map=UPSTREAM_ID_MAP,
    )
    prices = {
        model_id: price
        for model_id, price in prices.items()
        if not provider_model_retired(
            SLUG,
            model_id,
            str(discovered.get(model_id, {}).get("upstream_id") or ""),
        )
    }
    discovered = {
        model_id: row
        for model_id, row in discovered.items()
        if not provider_model_retired(
            SLUG,
            model_id,
            str(row.get("upstream_id") or ""),
        )
    }
    # https://docs.baseten.co/inference/model-apis/decision-models
    # These use /v1/systemone despite the feed's text output modality.
    checked, healthy = classify_systemone_rows(prices, discovered, api_key)
    _DISCOVERED_MANIFEST_ROWS = discovered

    notes: list[str] = []
    if checked:
        notes.append(f"canaried {len(checked)} System One decision models, {len(healthy)} passed")
    errors = validate(prices, EXPECTED_MODELS)
    if errors:
        notes.append(f"validation notes: {errors}")
        raise RuntimeError("; ".join(errors))

    return ProviderPricingResult(
        slug=SLUG,
        prices=prices,
        source="api",
        fetched_url=URL,
        notes=notes,
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    return write_discovered_chat_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        discovered_rows=_DISCOVERED_MANIFEST_ROWS,
        source_url=URL,
        operator_hold_reasons=_held_systemone_ids(_DISCOVERED_MANIFEST_ROWS),
    )
