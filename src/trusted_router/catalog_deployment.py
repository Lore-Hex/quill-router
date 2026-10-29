"""Temporary model admission holds for staggered gateway releases."""

from __future__ import annotations

import os
from dataclasses import replace

from trusted_router.catalog_data import Model, ModelEndpoint
from trusted_router.provider_contracts import (
    BEDROCK_RUNTIME_PROFILES,
    provider_model_available_from_gateway_region,
)
from trusted_router.regions import cloud_for_region


def _gateway_cloud_upgraded() -> bool:
    # Match Settings.effective_serving_region without loading credentials.
    region = os.environ.get("TR_SERVING_REGION") or os.environ.get("TR_PRIMARY_REGION")
    if region and cloud_for_region(region) == "gcp":
        return True
    if not region and os.environ.get("TR_ENVIRONMENT", "local").lower() in {"local", "test"}:
        return True
    return False


def unavailable_gateway_model_ids() -> frozenset[str]:
    # QCP bfac8577 adds the native Azure decision protocol. Only the GCP
    # gateway fleet has that release; secondary control planes update alone.
    return frozenset() if _gateway_cloud_upgraded() else frozenset({"microsoft/microsoft-decision-1"})


def unavailable_gateway_routes() -> frozenset[tuple[str, str]]:
    # QCP dc6eae3d adds these media protocols and the approved Privatemode
    # v1.59 trust pin. GCP's eight instances are verified; AWS/Azure are not.
    # Their automatic control-plane catch-up does not update their gateways.
    if _gateway_cloud_upgraded():
        return frozenset()
    return frozenset({
        ("deepseek/deepseek-ocr-2", "privatemode"),
        ("nomic-ai/nomic-embed-text", "tinfoil"),
        ("qwen/qwen3-embedding-4b", "privatemode"),
        ("openai/whisper-large-v3", "privatemode"),
        ("mistralai/voxtral-mini-3b-2507", "privatemode"),
        ("mistralai/voxtral-small-24b", "tinfoil"),
    })


def apply_gateway_admission_holds(
    models: dict[str, Model], endpoints: dict[str, ModelEndpoint],
) -> None:
    unavailable_models = unavailable_gateway_model_ids()
    unavailable_routes = unavailable_gateway_routes()
    # Bedrock requires QCP #535 (including its AWS vsock tunnel and Azure
    # sealed-bundle entry). Automatic CP catch-up cannot upgrade those gateways.
    legacy_bedrock_unavailable = not _gateway_cloud_upgraded()
    region = os.environ.get("TR_SERVING_REGION") or os.environ.get("TR_PRIMARY_REGION", "")
    if not region and os.environ.get("TR_ENVIRONMENT", "local").lower() in {"local", "test"}:
        # Match the local Settings default for previews/social-card generation.
        # Production with missing or unknown region evidence still fails closed.
        region = "us-central1"
    affected_models = {model_id for model_id, _ in unavailable_routes}
    for identity, endpoint in tuple(endpoints.items()):
        bedrock_runtime = endpoint.provider == "amazon-bedrock" and endpoint.model_id in BEDROCK_RUNTIME_PROFILES
        runtime_unavailable = (
            (endpoint.provider == "maple" or bedrock_runtime)
            and not provider_model_available_from_gateway_region(endpoint.provider, endpoint.model_id, region)
        )
        mantle_unavailable = endpoint.provider == "amazon-bedrock" and not bedrock_runtime and legacy_bedrock_unavailable
        if runtime_unavailable or mantle_unavailable or endpoint.model_id in unavailable_models or (
            endpoint.model_id, endpoint.provider
        ) in unavailable_routes:
            affected_models.add(endpoint.model_id)
            del endpoints[identity]
    for model_id in unavailable_models:
        models.pop(model_id, None)
    for model_id in affected_models:
        model = models.get(model_id)
        if model is None:
            continue
        remaining = [endpoint for endpoint in endpoints.values() if endpoint.model_id == model_id]
        if not remaining:
            models.pop(model_id)
        else:
            models[model_id] = replace(
                model,
                prepaid_available=any(not endpoint.is_byok for endpoint in remaining),
                byok_available=any(endpoint.is_byok for endpoint in remaining),
            )
