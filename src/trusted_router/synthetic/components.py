from __future__ import annotations

from typing import Any

from trusted_router.storage_models import SyntheticProbeSample, SyntheticRollup

REGIONAL_GATEWAY_PROBES = {"tls_health", "attestation_nonce"}
CONTROL_PLANE_PROBES = {"control_plane_health"}
IMAGE_GENERATION_PROBES = {"image_generation"}
BILLING_PROBES = {
    "gateway_authorize",
    "gateway_settle",
    # Retained so existing 24-month rollups remain visible after the split.
    "gateway_authorize_settle",
}
MONITOR_CONFIGURATION_ERROR_TYPES = frozenset(
    {
        "monitor_account_unavailable",
        "monitor_workspace_paused",
    }
)

COMPONENT_PROBES: dict[str, set[str]] = {
    "canonical_api": REGIONAL_GATEWAY_PROBES,
    "us_central1_regional_api": REGIONAL_GATEWAY_PROBES,
    "us_east4_regional_api": REGIONAL_GATEWAY_PROBES,
    "eu_regional_api": REGIONAL_GATEWAY_PROBES,
    "attestation": {"attestation_nonce"},
    "billing_settlement": BILLING_PROBES,
    "provider_fallback": {"provider_fallback"},
    "image_generation": IMAGE_GENERATION_PROBES,
}

SLO_DEFINITIONS: tuple[dict[str, str], ...] = (
    {
        "id": "router_core",
        "name": "Router Core",
        "description": (
            "Attested TLS, authorization, route candidates, provider fallback, "
            "and settlement/refund durability."
        ),
    },
    {
        "id": "control_plane",
        "name": "Control Plane",
        "description": "Dashboard, billing, key management, docs, and public status surfaces.",
    },
)

COMPONENT_DEFINITIONS: tuple[dict[str, str], ...] = (
    {
        "id": "canonical_api",
        "name": "Canonical API",
        "description": "api.trustedrouter.com attested TLS reachability and trust checks.",
    },
    {
        "id": "us_central1_regional_api",
        "name": "US Central Regional API",
        "description": "US Central attested TLS reachability and trust checks.",
    },
    {
        "id": "us_east4_regional_api",
        "name": "US East Regional API",
        "description": "US East attested TLS reachability and trust checks.",
    },
    {
        "id": "eu_regional_api",
        "name": "EU Regional API",
        "description": "EU attested TLS reachability and trust checks.",
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
    {
        "id": "image_generation",
        "name": "Image Generation",
        "description": "Public attested Gemini image generation and binary image validation.",
    },
)


def sample_component_ids(sample: SyntheticProbeSample) -> list[str]:
    if sample.error_type in MONITOR_CONFIGURATION_ERROR_TYPES:
        return []
    ids: list[str] = []
    if sample.target == "canonical" and sample.probe_type in REGIONAL_GATEWAY_PROBES:
        ids.append("canonical_api")
    if sample.target == "us-central1" and sample.probe_type in REGIONAL_GATEWAY_PROBES:
        ids.append("us_central1_regional_api")
    if sample.target == "us-east4" and sample.probe_type in REGIONAL_GATEWAY_PROBES:
        ids.append("us_east4_regional_api")
    if sample.target == "europe-west4" and sample.probe_type in REGIONAL_GATEWAY_PROBES:
        ids.append("eu_regional_api")
    if sample.probe_type == "attestation_nonce":
        ids.append("attestation")
    if sample.target == "control-plane" and sample.probe_type in BILLING_PROBES:
        ids.append("billing_settlement")
    if sample.target == "control-plane" and sample.probe_type == "provider_fallback":
        ids.append("provider_fallback")
    if sample.target == "canonical" and sample.probe_type in IMAGE_GENERATION_PROBES:
        ids.append("image_generation")
    return ids


def sample_slo_class_ids(sample: SyntheticProbeSample) -> list[str]:
    if sample.error_type in MONITOR_CONFIGURATION_ERROR_TYPES:
        return []
    return _slo_class_ids(probe_type=sample.probe_type, target=sample.target)


def is_router_origin_error(error_type: str | None) -> bool:
    """Return whether a benchmark failure happened before provider invocation."""
    return bool(
        error_type
        and (
            error_type in MONITOR_CONFIGURATION_ERROR_TYPES
            or error_type.startswith("router_")
        )
    )


def rollup_slo_class_ids(rollup: SyntheticRollup) -> list[str]:
    ids = _slo_class_ids(probe_type=rollup.probe_type, target=rollup.target)
    if "router_core" not in ids:
        return ids
    # Samples can feed more than one public display component. Select exactly
    # one component for each Router Core dimension so SLO rollups never count
    # the same underlying probe twice.
    expected_component = {
        "tls_health": "canonical_api",
        "attestation_nonce": "canonical_api",
        "gateway_authorize": "billing_settlement",
        "gateway_settle": "billing_settlement",
        "gateway_authorize_settle": "billing_settlement",
        "provider_fallback": "provider_fallback",
    }.get(rollup.probe_type)
    if rollup.component != expected_component:
        ids.remove("router_core")
    return ids


def _slo_class_ids(*, probe_type: str, target: str) -> list[str]:
    ids: list[str] = []
    if (
        (target == "canonical" and probe_type in REGIONAL_GATEWAY_PROBES)
        or (
            target == "control-plane"
            and probe_type in BILLING_PROBES | {"provider_fallback"}
        )
    ):
        ids.append("router_core")
    if probe_type in CONTROL_PLANE_PROBES:
        ids.append("control_plane")
    return ids


def component_probe_types(component_id: str) -> set[str]:
    return set(COMPONENT_PROBES.get(component_id, set()))


def component_name(component_id: str) -> str:
    for definition in COMPONENT_DEFINITIONS:
        if definition["id"] == component_id:
            return definition["name"]
    return component_id.replace("_", " ").title()


def public_component_definitions() -> tuple[dict[str, Any], ...]:
    return COMPONENT_DEFINITIONS


def public_slo_definitions() -> tuple[dict[str, Any], ...]:
    return SLO_DEFINITIONS
