"""Informational inference-location evidence, never a routing eligibility source.

Company jurisdiction, API ingress and storage locality do not establish GPU
residency. Unknown claims stay unknown; provider catalog regions describe
availability, not an enforced pin or a receipt for an individual request.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from trusted_router.catalog_data import PROVIDERS


@dataclass(frozen=True)
class InferenceLocations:
    locations: tuple[str, ...] = ()
    scope: str = "No inference-location declaration has been verified for this provider."
    routing: str = "Not verified. Do not assume that the serving location is fixed."
    provider_pinning: str = "Not verified."
    trustedrouter_pinning: str = "No verified provider-specific location pin documented here."
    declaration: str = "Inference location not verified; no country-specific residency commitment."
    evidence: str = "Not yet reviewed"
    reviewed_on: str | None = None
    sources: tuple[tuple[str, str], ...] = ()


_NO_TR_PIN = "Not supported by the current TrustedRouter integration."

PROVIDER_INFERENCE_LOCATIONS = {
    "lyceum": InferenceLocations(
        scope="Lyceum documents model-specific serving locations; its German headquarters is not a fleet-wide EU residency guarantee.",
        routing="Placement and cross-region failover depend on the model; an exhaustive country list is not verified.",
        trustedrouter_pinning=_NO_TR_PIN,
        evidence="Public provider model-roster documentation",
        reviewed_on="2026-09-30",
        sources=(("Model-specific hosting and pricing", "https://lyceum.technology/magazine/eu-hosted-llm-api-lyceum-model-roster-prices/"),),
    ),
    "tencent": InferenceLocations(
        scope="The Singapore TokenHub ingress uses global resource scheduling. Ingress location is not GPU residency.",
        routing="Global scheduling; individual request location and country set are not verified.",
        provider_pinning="Tencent documents separate Guangzhou and US sites, but this integration uses the Singapore/global site only.",
        trustedrouter_pinning=_NO_TR_PIN,
        declaration="Global, dynamically scheduled inference; no fixed-country residency commitment.",
        evidence="Public TokenHub API documentation",
        reviewed_on="2026-09-29",
        sources=(("API regions and resource scheduling scope", "https://www.tencentcloud.com/document/product/1300/78941"),),
    ),
    "io-net": InferenceLocations(
        locations=("United States", "Canada"),
        scope=("io.net declares US and Canadian serving locations and data residency "
               "in its September 28, 2026 marketplace submission. This is provider-wide "
               "information, not an independently verified per-model or per-request location."),
        routing="Placement, cross-country failover and future location changes require provider confirmation.",
        provider_pinning="No per-request country or region pin verified for the shared inference API.",
        trustedrouter_pinning=_NO_TR_PIN,
        declaration="United States and Canada, provider-declared; no US-only or fixed-country guarantee on this route.",
        evidence="Provider-submitted marketplace information; linked public policy is not an inference-location guarantee",
        reviewed_on="2026-09-28",
        sources=(
            ("API documentation (not a location guarantee)", "https://io.net/docs/reference/ai-models/create-chat-completion"),
            ("Privacy policy (operator identity, not GPU residency)", "https://io.net/privacy"),
        ),
    ),
    "deepinfra": InferenceLocations(
        locations=("United States", "Canada (Toronto, announced capacity)"),
        scope=("DeepInfra describes US infrastructure and announced its first international "
               "data center in Toronto on July 8, 2026. This is a provider footprint, not an "
               "exhaustive or model-specific placement list. DeepSeek V4.1 Flash placement "
               "has not been confirmed; do not declare this route US-only."),
        routing="Individual request placement and cross-country failover are not verified.",
        provider_pinning="No self-service per-request region pin verified for this shared route.",
        trustedrouter_pinning=_NO_TR_PIN,
        declaration="US infrastructure plus announced Canadian capacity; model-specific residency unconfirmed.",
        evidence="Public provider statement",
        reviewed_on="2026-09-27",
        sources=(
            ("DeepInfra infrastructure statement", "https://deepinfra.com/"),
            ("DeepInfra Toronto announcement", "https://www.globenewswire.com/news-release/2026/7/8/3324125/0/en/deepinfra-expands-ai-inference-capacity-with-first-international-data-center-in-toronto.html"),
        ),
    ),
    "telnyx": InferenceLocations(
        locations=("United States", "Europe (Telnyx EU region; countries unspecified)",
                   "Australia", "United Arab Emirates"),
        scope=("Provider-wide GPU region set. Model availability is narrower; see the "
               "catalog snapshot below. Germany in the storage-locality documentation "
               "does not establish Germany as the inference country."),
        routing=("Dynamic, latency-based routing by default. Regional ingress is a preference; "
                 "capacity events and failover can move inference to another available region. "
                 "Model deployments may change."),
        provider_pinning=("Telnyx documents region + mode: strict on a matching regional ingress "
                          "domain for its own hosted models. Requests fail if the selected region "
                          "cannot serve them. Preferred mode is not a residency guarantee."),
        trustedrouter_pinning=("Not supported by the current TrustedRouter integration: it uses "
                               "api.telnyx.com without Telnyx's strict region controls. Do not send "
                               "Telnyx-specific region/mode fields as a TrustedRouter pin."),
        declaration=("Multi-region, dynamically routed among the model's available Telnyx "
                     "regions; no fixed inference-region commitment on this TrustedRouter route."),
        evidence="Public documentation and authenticated model catalog",
        reviewed_on="2026-09-26",
        sources=(
            ("Inference regions and strict pinning", "https://developers.telnyx.com/docs/inference/models/regions"),
            ("Processing versus storage", "https://developers.telnyx.com/docs/inference/data-residency"),
            ("Native model catalog (authentication required)", "https://api.telnyx.com/v2/ai/openai/models"),
        ),
    ),
    "pearl": InferenceLocations(
        locations=("United States (Central)", "Taiwan", "Australia"),
        scope=("Pearl supplied these serving regions in its marketplace application. This is "
               "provider-wide information, not a confirmed per-model or exhaustive failover "
               "list. GLM 5.3 Flash and DeepSeek V4.1 Flash do not declare regions in the "
               "reviewed native catalog."),
        routing="Per-model placement and cross-region failover behavior require provider confirmation.",
        provider_pinning="No per-request region-pinning contract verified in the reviewed API information.",
        trustedrouter_pinning=_NO_TR_PIN,
        declaration=("Multi-region provider: US Central, Taiwan and Australia declared; "
                     "model-specific inference location and exhaustive failover scope unconfirmed."),
        evidence="Provider-submitted marketplace information; no public per-model residency declaration",
        reviewed_on="2026-09-26",
        sources=(
            ("Native model catalog (authentication required)", "https://inference.pearlresearch.ai/v1/models"),
            ("Privacy policy (not a GPU-location declaration)", "https://pearlresearch.ai/legal/privacy"),
        ),
    ),
    "engy": InferenceLocations(
        scope=("Qwen3.8-27B and GLM 5.3 Flash are listed as Engy-operated ZDR models, "
               "but the reviewed catalog, docs and privacy policy do not disclose their "
               "GPU countries or a complete failover-location set."),
        routing="Worker location and failover geography are not disclosed in the reviewed sources.",
        provider_pinning="No documented per-request country or region pin found in the reviewed API docs.",
        trustedrouter_pinning=_NO_TR_PIN,
        declaration="Inference countries undisclosed; no verified country-specific residency commitment.",
        evidence="Public docs reviewed; physical inference geography unconfirmed",
        reviewed_on="2026-09-26",
        sources=(("API documentation", "https://engy.ai/docs"),
                 ("Privacy and processing path", "https://engy.ai/privacy")),
    ),
    "novita": InferenceLocations(
        scope=("No model-specific country list verified for the shared Qwen3.8-27B API route. "
               "Novita publishes worldwide GPU rental regions, but that list does not prove "
               "where this serverless model executes."),
        routing="Shared-route placement and failover geography require provider confirmation.",
        provider_pinning=("GPU-instance or dedicated-deployment region selection is not evidence "
                          "of a region pin for this shared model API."),
        trustedrouter_pinning=_NO_TR_PIN,
        declaration="Shared inference API; processing countries unconfirmed, no fixed-region commitment.",
        evidence="Public docs and native catalog reviewed; model-specific geography unconfirmed",
        reviewed_on="2026-09-26",
        sources=(
            ("GPU rental regions (different product)", "https://blogs.novita.ai/gpu-regions-zones/"),
            ("Native model catalog", "https://api.novita.ai/openai/v1/models"),
        ),
    ),
    "siliconflow": InferenceLocations(
        scope=("TrustedRouter uses api.siliconflow.com. Neither its hostname nor the "
               "provider's Singapore legal jurisdiction establishes the DeepSeek V4.1 Flash "
               "GPU location. No exhaustive route-specific country list verified."),
        routing="Per-model placement and cross-country failover behavior are unconfirmed.",
        provider_pinning="No verified per-request regional pin for the integrated shared endpoint.",
        trustedrouter_pinning=_NO_TR_PIN,
        declaration="Inference countries unconfirmed; no verified country-specific residency commitment.",
        evidence="Public API and privacy documentation reviewed; GPU geography unconfirmed",
        reviewed_on="2026-09-26",
        sources=(
            ("API documentation", "https://docs.siliconflow.com/"),
            ("Privacy policy (not a GPU-location declaration)", "https://docs.siliconflow.com/en/legals/privacy-policy"),
        ),
    ),
    "scaleway": InferenceLocations(
        locations=("France (Paris)",),
        scope="Scaleway's FAQ places its current Serverless inference fleet in Paris. Dedicated deployments are a separate product.",
        routing="Scaleway may expand Serverless hosting within Europe, with notification; France-only is not a permanent commitment.",
        provider_pinning="Scaleway recommends Dedicated Deployment for single-region processing.",
        trustedrouter_pinning=_NO_TR_PIN,
        declaration="Serverless: currently Paris, France, provider-declared; future expansion within Europe possible.",
        evidence="Public Serverless FAQ; not a per-request location receipt",
        reviewed_on="2026-09-27",
        sources=(("Scaleway inference-server locations", "https://www.scaleway.com/en/docs/generative-apis/faq/"),),
    ),
    "privatemode": InferenceLocations(
        locations=("European Union (countries unspecified)",),
        scope="Privatemode declares EU hosting for its inference service. This does not establish an individual worker's country.",
        routing="Changes within the EU and exact model placement are not specified by the reviewed declaration.",
        provider_pinning="No per-request country-selection contract verified.",
        trustedrouter_pinning=_NO_TR_PIN,
        declaration="EU-hosted inference, provider-declared; country and individual worker location unspecified.",
        evidence="Public provider statement; encryption attestation is separate from location evidence",
        reviewed_on="2026-09-27",
        sources=(("Privatemode EU hosting", "https://www.privatemode.ai/sovereign-ai"),),
    ),
    "neurometric": InferenceLocations(
        locations=("United States (AWS us-west-2)",),
        scope="Neurometric supplied AWS us-west-2 in its marketplace application. New models and exhaustive failover locations need separate confirmation.",
        routing="Model-specific placement and failover changes require provider confirmation.",
        provider_pinning="No per-request regional pin verified.",
        trustedrouter_pinning=_NO_TR_PIN,
        declaration="AWS us-west-2, provider-submitted hosting declaration; not a per-request receipt.",
        evidence="Provider-submitted marketplace information",
        reviewed_on="2026-09-27",
        sources=(("Provider catalog (authentication required)", "https://wharf.neurometric.ai/v1/models"),),
    ),
    "scaledown": InferenceLocations(
        locations=("United States",),
        scope="ScaleDown's marketplace application states that hosted processing occurs in the US and requires written consent for processing elsewhere. Customer-run VPC deployments are separate.",
        routing="Individual US sites and model placement are unspecified; confirm the applicable contract before relying on residency.",
        provider_pinning="No per-request regional pin verified.",
        trustedrouter_pinning=_NO_TR_PIN,
        declaration="US hosted processing, provider-submitted declaration; individual site unspecified.",
        evidence="Provider-submitted marketplace information; linked DPA for contractual review",
        reviewed_on="2026-09-27",
        sources=(("ScaleDown DPA", "https://scaledown.ai/dpa/"),),
    ),
}


@dataclass(frozen=True)
class Headquarters:
    location: str | None = None
    evidence: str = "Not verified; the API operator's legal country is not necessarily its headquarters."
    source_url: str | None = None
    reviewed_on: str | None = None


# Keep physical HQ separate from catalog_data's legacy headquarters_country,
# which is used for security filtering and actually identifies the API operator.
PROVIDER_HEADQUARTERS = {
    "io-net": Headquarters("West Hollywood, California, United States (operating address)", "Provider-submitted operating address; not independently verified as headquarters", "https://io.net", "2026-09-28"),
    "telnyx": Headquarters("Austin, Texas, United States", "Provider-submitted headquarters; company profile", "https://www.linkedin.com/company/telnyx", "2026-09-27"),
    "deepinfra": Headquarters("Palo Alto, California, United States", "Company profile", "https://www.linkedin.com/company/deep-infra", "2026-09-27"),
    "novita": Headquarters("San Francisco, California, United States", "Company profile", "https://www.linkedin.com/company/novita-ai-labs/", "2026-09-27"),
    "siliconflow": Headquarters("Singapore (international API operator)", "International operator company profile", "https://www.linkedin.com/company/siliconflow", "2026-09-27"),
    "pearl": Headquarters("Tel Aviv-Yafo, Israel", "Provider-submitted operating and registered address; public policy for contact", "https://pearlresearch.ai/legal/privacy", "2026-09-27"),
}


def provider_geography(provider_slug: str) -> dict[str, object]:
    """Same informational contract for every provider, including unknowns."""
    provider = PROVIDERS.get(provider_slug)
    return {
        "operator_country": provider.provider_headquarters_country if provider else None,
        "operator_country_scope": "Legal home of the API operator, not an inference-location guarantee.",
        "headquarters": asdict(PROVIDER_HEADQUARTERS.get(provider_slug, Headquarters())),
        "inference": asdict(provider_inference_locations(provider_slug)),
        "documentation_url": f"https://trustedrouter.com/providers/{provider_slug}#inference-locations" if provider else None,
    }


def provider_inference_locations(provider_slug: str) -> InferenceLocations:
    return PROVIDER_INFERENCE_LOCATIONS.get(provider_slug, InferenceLocations())


@dataclass(frozen=True)
class ModelLocationSnapshot:
    generated_at: str
    model_regions: dict[str, tuple[str, ...]]


_TELNYX_REGION_LABELS = {
    "USA": "United States",
    "EU": "Europe (Telnyx EU region; countries unspecified)",
    "AUS": "Australia",
    "UAE": "United Arab Emirates",
}


def telnyx_model_locations(payload: object) -> ModelLocationSnapshot:
    """Read availability only; fail unknown for malformed/undated snapshots."""
    empty = ModelLocationSnapshot("", {})
    if not isinstance(payload, dict) or payload.get("provider") != "telnyx":
        return empty
    rows = payload.get("models")
    generated_at = payload.get("generated_at")
    if not isinstance(rows, list) or not isinstance(generated_at, str) or not generated_at:
        return empty
    try:
        if datetime.fromisoformat(generated_at).tzinfo is None:
            return empty
    except ValueError:
        return empty
    regions_by_model = {}
    seen_models: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("routable") is False:
            continue
        model_id, regions = row.get("id"), row.get("provider_regions")
        if not isinstance(model_id, str) or not model_id:
            continue
        if model_id in seen_models:
            # Ambiguous rows must not let the last declaration hide a region.
            return empty
        seen_models.add(model_id)
        if not isinstance(regions, list) or not regions:
            continue
        # Unknown upstream codes are not silently dropped from the location set.
        if any(not isinstance(code, str) or code not in _TELNYX_REGION_LABELS for code in regions):
            continue
        regions_by_model[model_id] = tuple(dict.fromkeys(_TELNYX_REGION_LABELS[code] for code in regions))
    return ModelLocationSnapshot(generated_at, regions_by_model)


@lru_cache(maxsize=1)
def _telnyx_location_snapshot() -> ModelLocationSnapshot:
    path = Path(__file__).parent / "data" / "provider_models" / "telnyx.json"
    try:
        return telnyx_model_locations(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return ModelLocationSnapshot("", {})


def provider_model_locations(provider_slug: str) -> ModelLocationSnapshot:
    # Explicit opt-in: other providers' similarly named fields may describe
    # storage or ingress. A URL parameter is never used as a filesystem path.
    return _telnyx_location_snapshot() if provider_slug == "telnyx" else ModelLocationSnapshot("", {})


def inference_location_metadata(provider_slug: str, model_id: str) -> dict[str, object]:
    """Public availability evidence, never a receipt or a routing constraint.

    Keep a single shape for catalog endpoints and enclave response metadata.
    No integrated upstream currently supplies a verified serving-region field.
    In particular, the router's own region and edge POP headers are not one.
    """
    snapshot = provider_model_locations(provider_slug)
    regions = snapshot.model_regions.get(model_id, ())
    declaration = provider_inference_locations(provider_slug)
    return {
        "advertised_regions": list(regions),
        "advertised_region_scope": "model_default_tier" if regions else "unknown",
        "catalog_updated_at": snapshot.generated_at if regions else None,
        "provider_declared_locations": list(declaration.locations),
        "provider_declaration_reviewed_on": declaration.reviewed_on,
        "serving_region": None,
        "serving_region_status": "not_reported",
        "region_pinning_enforced": False,
        "documentation_url": f"https://trustedrouter.com/providers/{provider_slug}#inference-locations" if provider_slug in PROVIDERS else None,
    }
