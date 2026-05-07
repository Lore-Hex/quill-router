from __future__ import annotations

from typing import Any

from trusted_router.storage_models import SyntheticProbeSample

API_PROBES = {"tls_health", "attestation_nonce", "openai_sdk_pong", "responses_pong"}

COMPONENT_DEFINITIONS: tuple[dict[str, str], ...] = (
    {
        "id": "canonical_api",
        "name": "Canonical API",
        "description": "api.quillrouter.com chat, Responses, TLS, and attestation checks.",
    },
    {
        "id": "eu_regional_api",
        "name": "EU Regional API",
        "description": "api-europe-west4.quillrouter.com regional attested gateway checks.",
    },
    {
        "id": "attestation",
        "name": "Attestation",
        "description": "Nonce and digest verification for public attested gateways.",
    },
    {
        "id": "billing_settlement",
        "name": "Billing and Settlement",
        "description": "Authorize, settle, and accounting path used by the gateway.",
    },
    {
        "id": "provider_fallback",
        "name": "Provider Fallback",
        "description": "Fail-first route selection and rollover to the next healthy provider.",
    },
)


def sample_component_ids(sample: SyntheticProbeSample) -> list[str]:
    ids: list[str] = []
    if sample.target == "canonical" and sample.probe_type in API_PROBES:
        ids.append("canonical_api")
    if sample.target == "europe-west4" and sample.probe_type in API_PROBES:
        ids.append("eu_regional_api")
    if sample.probe_type == "attestation_nonce":
        ids.append("attestation")
    if sample.target == "control-plane" and sample.probe_type == "gateway_authorize_settle":
        ids.append("billing_settlement")
    if sample.target == "control-plane" and sample.probe_type == "provider_fallback":
        ids.append("provider_fallback")
    return ids


def component_name(component_id: str) -> str:
    for definition in COMPONENT_DEFINITIONS:
        if definition["id"] == component_id:
            return definition["name"]
    return component_id.replace("_", " ").title()


def public_component_definitions() -> tuple[dict[str, Any], ...]:
    return COMPONENT_DEFINITIONS
