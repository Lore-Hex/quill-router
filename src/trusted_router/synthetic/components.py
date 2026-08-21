from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from trusted_router.storage_models import SyntheticProbeSample, SyntheticRollup

if TYPE_CHECKING:
    from trusted_router.config import Settings

# Target name of the billing/settlement and provider-fallback probes. Unlike
# the gateway targets these do not come from the region topology — every
# deployment runs them against its own control plane — so they are always
# part of the applicable set.
CONTROL_PLANE_TARGET = "control-plane"

# Bucket rollups land in when a sample maps to no public component at all
# (the diagnostic gateway_cold_path / gateway_reused_path timings). It is an
# internal bucket, never a published component: rendering it produced public
# "Uncategorized — Major outage" rows carrying an internal error slug.
UNCATEGORIZED_COMPONENT = "uncategorized"

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
# Liveness/ops probe types (synthetic/fleet.py): scheduler heartbeats and
# cross-cloud peer policing. They ride the synthetic-sample pipeline for
# storage and streak alerts but are NOT evidence about this deployment's
# service, so they must stay out of every component, every SLO, and — most
# importantly — the monitor-freshness clock: a heartbeat from a loop that is
# not the probe fleet must never make a dead probe fleet look fresh.
OPS_PROBE_TYPES = frozenset(
    {"client_telemetry_ingest", "heartbeat", "peer_monitor", "remediation"}
)
# Deep end-to-end model calls: a real OpenAI-SDK chat completion and a real
# Responses round-trip, output-verified. These probes previously fed NO
# component: they could fail 100% (as they did on AWS and Azure on
# 2026-08-10, every pong failing with 402) while the page rendered green. A
# probe that is recorded but surfaced nowhere is a signal that reports
# success without measuring — this set is what makes the model path render.
MODEL_INFERENCE_PROBES = {"openai_sdk_pong", "responses_pong"}

COMPONENT_PROBES: dict[str, set[str]] = {
    "canonical_api": REGIONAL_GATEWAY_PROBES,
    "us_central1_regional_api": REGIONAL_GATEWAY_PROBES,
    "us_east4_regional_api": REGIONAL_GATEWAY_PROBES,
    "eu_regional_api": REGIONAL_GATEWAY_PROBES,
    "sa_regional_api": REGIONAL_GATEWAY_PROBES,
    "eu_west_1_gateway": REGIONAL_GATEWAY_PROBES,
    "eu_west_3_gateway": REGIONAL_GATEWAY_PROBES,
    "attestation": {"attestation_nonce"},
    "billing_settlement": BILLING_PROBES,
    "provider_fallback": {"provider_fallback"},
    "image_generation": IMAGE_GENERATION_PROBES,
    "model_inference": MODEL_INFERENCE_PROBES,
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

# FULL CATALOGUE of every component any TrustedRouter deployment has ever
# published. This is deliberately NOT the published list: a component only
# belongs on a deployment's public status page if that deployment can
# actually sample it (see applicable_component_definitions). The catalogue
# stays complete so 24-month historical rollups naming a component that no
# longer applies here still resolve to a human-readable name.
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
        "id": "sa_regional_api",
        "name": "South America Regional API",
        "description": "São Paulo attested TLS reachability and trust checks.",
    },
    # Per-REGION components. Unlike the regional entries above they do not
    # have their own hostname: every AWS EU request goes to one anycast name
    # that Global Accelerator points at whichever region it prefers, so the
    # canonical probe alone cannot tell "both regions healthy" from "one
    # region dead". Their samples come from targets pinned to a specific
    # load balancer (SyntheticTarget.connect_host), which is why one dead
    # region turns exactly one of these red while Canonical API can stay
    # green. Published only where those targets are configured.
    #
    # NAMED "GATEWAY", NOT "ENCLAVE", and the description says so out loud.
    # connect_host is a region's NLB, and that NLB fronts an Auto Scaling
    # group of enclaves (quill-cloud-proxy tools/deploy-aws-nitro.sh: target
    # group with an ELB health check, ASG spread across every AZ subnet). The
    # probe therefore reaches ONE arbitrary healthy member. Calling the row
    # "EU West 1 Enclave" would re-create the exact defect this feature
    # exists to remove, one level down: a crash-looping AZ-1a enclave is
    # dropped from the target group, every probe lands on 1b, and a row
    # claiming to measure "the Ireland enclave" stays green at half capacity.
    # Per-enclave granularity needs per-AZ zonal NLB names
    # (eu-west-1a.<nlb>.elb.eu-west-1.amazonaws.com) as additional entries —
    # configuration, no code change — not a more confident label here.
    {
        "id": "eu_west_1_gateway",
        "name": "EU West 1 Gateway (Ireland)",
        "description": (
            "Ireland load balancer addressed directly: attested TLS and "
            "measurement-pinned attestation from a healthy Ireland enclave "
            "behind it. Green means the region is serving, not that every "
            "Ireland enclave is healthy."
        ),
    },
    {
        "id": "eu_west_3_gateway",
        "name": "EU West 3 Gateway (Paris)",
        "description": (
            "Paris load balancer addressed directly: attested TLS and "
            "measurement-pinned attestation from a healthy Paris enclave "
            "behind it. Green means the region is serving, not that every "
            "Paris enclave is healthy."
        ),
    },
    # Azure. NAMED "GATEWAY" for the same reason as the AWS rows above, even
    # though this one currently fronts a SINGLE container group: the name must
    # not promise per-enclave granularity the probe cannot deliver, and a
    # second replica or region would silently make an "Enclave" label a lie.
    {
        "id": "uaenorth_gateway",
        "name": "UAE North Gateway (Dubai)",
        "description": (
            "Azure confidential container addressed directly: publicly-trusted "
            "TLS minted inside the TEE and SEV-SNP attestation through MAA "
            "from a healthy UAE North enclave behind it."
        ),
    },
    {
        "id": "southeastasia_gateway",
        "name": "Southeast Asia Gateway (Singapore)",
        "description": (
            "Azure confidential container addressed directly: publicly-trusted "
            "TLS minted inside the TEE and SEV-SNP attestation through MAA "
            "from a healthy Southeast Asia enclave behind it."
        ),
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
        "description": (
            "Normalized multi-provider image generation and binary image validation "
            "through the public attested API."
        ),
    },
    {
        "id": "model_inference",
        "name": "Model Inference",
        "description": (
            "End-to-end model calls through the attested gateway: OpenAI SDK and "
            "Responses round-trips with verified output."
        ),
    },
)

# Which probe target has to exist for a catalogue component to be
# measurable. This is the inverse of sample_component_ids() below — keep the
# two in sync — and it is what scopes the published list to the running
# deployment. Kept out of COMPONENT_DEFINITIONS on purpose: those dicts are
# spread verbatim into the public status payload.
COMPONENT_PROBE_TARGETS: dict[str, str] = {
    "canonical_api": "canonical",
    "us_central1_regional_api": "us-central1",
    "us_east4_regional_api": "us-east4",
    "eu_regional_api": "europe-west4",
    "sa_regional_api": "southamerica-east1",
    # These names are the TR_SYNTHETIC_GATEWAY_REGION_TARGETS entry names the
    # AWS EU control plane configures; nothing publishes them anywhere else.
    "eu_west_1_gateway": "eu-west-1",
    "eu_west_3_gateway": "eu-west-3",
    # The Azure control plane's TR_SYNTHETIC_GATEWAY_REGION_TARGETS entry name
    # (scripts/deploy/azure_control_plane.sh). Absent everywhere else, which is
    # what keeps this row off the AWS and GCP status pages.
    "uaenorth_gateway": "uaenorth",
    "southeastasia_gateway": "southeastasia",
    "attestation": "canonical",
    "billing_settlement": CONTROL_PLANE_TARGET,
    "provider_fallback": CONTROL_PLANE_TARGET,
    "image_generation": "canonical",
    # Pongs only run against non-pinned targets holding the monitor's live
    # API key (probes.py: `api_key and target.paid_probes and not
    # target.connect_host`), which in practice is the canonical target every
    # deployment configures.
    "model_inference": "canonical",
}

# Components fed by a target PINNED to one region's endpoint
# (SyntheticTarget.connect_host) rather than by the address customers
# resolve. Two things must know the difference:
#
#   * shared, service-wide rows and metrics must EXCLUDE them, or a
#     diagnostic probe of a region the accelerator has already health-checked
#     out of rotation corrupts a number describing the served path;
#   * the overall headline must INCLUDE them, or "All Systems Operational"
#     sits directly above a red region row.
GATEWAY_REGION_COMPONENT_IDS: frozenset[str] = frozenset(
    {
        "eu_west_1_gateway",
        "eu_west_3_gateway",
        "uaenorth_gateway",
        "southeastasia_gateway",
    }
)
# Regional API components also feed deploy automation through
# status.current.checks. Keep this set separate from
# GATEWAY_REGION_COMPONENT_IDS: the latter deliberately controls the public
# overall-status banner for pinned failover gateways, while these GCP rows
# must remain diagnostic and outside the router-core SLO.
REGIONAL_API_COMPONENT_IDS: frozenset[str] = frozenset(
    {
        "us_central1_regional_api",
        "us_east4_regional_api",
        "eu_regional_api",
        "sa_regional_api",
    }
)
MACHINE_REGION_COMPONENT_IDS: frozenset[str] = (
    GATEWAY_REGION_COMPONENT_IDS | REGIONAL_API_COMPONENT_IDS
)
# Their probe target names, derived from the map above so the two cannot
# drift apart.
GATEWAY_REGION_TARGET_NAMES: frozenset[str] = frozenset(
    COMPONENT_PROBE_TARGETS[component_id] for component_id in GATEWAY_REGION_COMPONENT_IDS
)

# A probe target is necessary but not always sufficient. Image generation
# runs against the "canonical" target — which every deployment has — yet its
# samples come from a SEPARATE scheduled job that only
# scripts/deploy/synthetic.sh creates. Target presence alone therefore keeps
# publishing a component the AWS EU cloud can never sample, so measurability
# also consults the capability flag that says whether this deployment
# schedules the job at all.
COMPONENT_REQUIRED_CAPABILITIES: dict[str, Callable[[Settings], bool]] = {
    "image_generation": lambda settings: settings.synthetic_image_probe_enabled,
    # Pongs are billable calls gated on the monitor key being configured; a
    # deployment without one never samples the model path, so publishing the
    # row there would be a permanent "unknown".
    "model_inference": lambda settings: bool(settings.synthetic_monitor_api_key),
}


def deployment_probe_targets(settings: Settings) -> frozenset[str]:
    """Probe target names this deployment's monitor can actually sample."""
    # Imported here, not at module scope: probes.py imports this module, so a
    # top-level import would be circular. Deriving the names from the single
    # place that builds the monitor's targets is the point — a second,
    # hand-maintained list is exactly how the published components drifted
    # away from the probes in the first place.
    from trusted_router.synthetic.probes import configured_targets

    return frozenset(
        {target.name for target in configured_targets(settings)} | {CONTROL_PLANE_TARGET}
    )


def applicable_component_definitions(settings: Settings) -> tuple[dict[str, str], ...]:
    """Catalogue components this deployment can produce samples for.

    A public status page must only assert things it measures. The AWS EU
    cloud has no us-central1, us-east4, europe-west4, or southamerica-east1
    gateway, so publishing those components there would produce permanent
    "unknown" rows — which reads as "we are not sure our own service works" and is
    worse than not listing them at all. Scope comes from configuration
    (regions + synthetic_regional_probes_enabled), never a per-cloud list.
    """
    targets = deployment_probe_targets(settings)
    applicable: list[dict[str, str]] = []
    for definition in COMPONENT_DEFINITIONS:
        component_id = str(definition["id"])
        required = COMPONENT_PROBE_TARGETS.get(component_id)
        # An unmapped catalogue entry is published rather than hidden:
        # silently dropping a component nobody has classified yet would be
        # the worse failure. test_component_probe_targets_cover_the_catalogue
        # keeps the map complete so this branch stays theoretical.
        if required is not None and required not in targets:
            continue
        capability = COMPONENT_REQUIRED_CAPABILITIES.get(component_id)
        if capability is not None and not capability(settings):
            continue
        applicable.append(definition)
    return tuple(applicable)


def published_gateway_region_components(settings: Settings) -> tuple[str, ...]:
    """Pinned per-region component ids this deployment actually publishes.

    Empty everywhere the setting is unset (GCP, and AWS EU before the
    endpoints are configured), which is what keeps every consumer of it a
    no-op on those deployments.
    """
    published = {str(definition["id"]) for definition in applicable_component_definitions(settings)}
    return tuple(
        str(definition["id"])
        for definition in COMPONENT_DEFINITIONS
        if str(definition["id"]) in GATEWAY_REGION_COMPONENT_IDS
        and str(definition["id"]) in published
    )


def published_machine_region_components(settings: Settings) -> tuple[str, ...]:
    """Regional component ids exposed to deploy and watchdog automation."""
    published = {str(definition["id"]) for definition in applicable_component_definitions(settings)}
    return tuple(
        str(definition["id"])
        for definition in COMPONENT_DEFINITIONS
        if str(definition["id"]) in MACHINE_REGION_COMPONENT_IDS
        and str(definition["id"]) in published
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
    if sample.target == "southamerica-east1" and sample.probe_type in REGIONAL_GATEWAY_PROBES:
        ids.append("sa_regional_api")
    if sample.target == "eu-west-1" and sample.probe_type in REGIONAL_GATEWAY_PROBES:
        ids.append("eu_west_1_gateway")
    if sample.target == "eu-west-3" and sample.probe_type in REGIONAL_GATEWAY_PROBES:
        ids.append("eu_west_3_gateway")
    if sample.target == "uaenorth" and sample.probe_type in REGIONAL_GATEWAY_PROBES:
        ids.append("uaenorth_gateway")
    if sample.target == "southeastasia" and sample.probe_type in REGIONAL_GATEWAY_PROBES:
        ids.append("southeastasia_gateway")
    # "Attestation" is a SHARED, service-wide row that predates the pinned
    # targets, and it is scoped to the addresses customers actually resolve.
    # Folding the pinned per-region probes in here averaged a public number
    # over targets that carry no traffic: with Paris dead and Ireland serving
    # 100% of requests fully attested, the row read "Trust degraded, 66.67%
    # (24h)" — and a third region would have made the same single-region
    # outage read 50%. The per-region rows above already carry that signal.
    if (
        sample.probe_type == "attestation_nonce"
        and sample.target not in GATEWAY_REGION_TARGET_NAMES
    ):
        ids.append("attestation")
    if sample.target == "control-plane" and sample.probe_type in BILLING_PROBES:
        ids.append("billing_settlement")
    if sample.target == "control-plane" and sample.probe_type == "provider_fallback":
        ids.append("provider_fallback")
    if sample.target == "canonical" and sample.probe_type in IMAGE_GENERATION_PROBES:
        ids.append("image_generation")
    # Any non-pinned target: each deployment's pongs prove that deployment's
    # model path. Deliberately NOT part of the router_core SLO class — a pong
    # failure can be a provider brownout, and the July scoping decision
    # (provider-effective failures do not burn router availability) stands.
    # Visibility is the component's job; the SLO math is unchanged.
    if (
        sample.probe_type in MODEL_INFERENCE_PROBES
        and sample.target not in GATEWAY_REGION_TARGET_NAMES
    ):
        ids.append("model_inference")
    return ids


def sample_slo_class_ids(sample: SyntheticProbeSample) -> list[str]:
    if sample.error_type in MONITOR_CONFIGURATION_ERROR_TYPES:
        return []
    return _slo_class_ids(probe_type=sample.probe_type, target=sample.target)


def is_router_origin_error(error_type: str | None) -> bool:
    """Return whether a benchmark failure happened before provider invocation."""
    return bool(
        error_type
        and (error_type in MONITOR_CONFIGURATION_ERROR_TYPES or error_type.startswith("router_"))
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
    if (target == "canonical" and probe_type in REGIONAL_GATEWAY_PROBES) or (
        target == "control-plane" and probe_type in BILLING_PROBES | {"provider_fallback"}
    ):
        ids.append("router_core")
    if probe_type in CONTROL_PLANE_PROBES:
        ids.append("control_plane")
    return ids


def component_probe_types(component_id: str) -> set[str]:
    return set(COMPONENT_PROBES.get(component_id, set()))


def component_name(component_id: str) -> str:
    # Resolves against the FULL catalogue on purpose: a 24-month rollup can
    # name a component this deployment no longer publishes, and it still has
    # to render with its real name rather than a slug.
    for definition in COMPONENT_DEFINITIONS:
        if definition["id"] == component_id:
            return definition["name"]
    return component_id.replace("_", " ").title()


def public_slo_definitions() -> tuple[dict[str, Any], ...]:
    return SLO_DEFINITIONS
