"""Informational inference-location evidence, never a routing eligibility source.

Company jurisdiction, API ingress and storage locality do not establish GPU
residency. Unknown claims stay unknown; provider catalog regions describe
availability, not an enforced pin or a receipt for an individual request.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path


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
    "deepinfra": InferenceLocations(
        locations=("United States",),
        scope=("DeepInfra states that its own inference infrastructure uses US data centers. "
               "This is a provider-wide statement for self-hosted models, including the "
               "DeepSeek V4.1 Flash route, not per-request location attestation."),
        routing=("The specific US site is not disclosed or pinned. The published country "
                 "statement does not promise an immutable fleet or describe failover sites."),
        provider_pinning="No self-service per-request region pin verified for this shared route.",
        trustedrouter_pinning=_NO_TR_PIN,
        declaration="United States, provider-declared inference hosting; individual site unspecified.",
        evidence="Public provider statement",
        reviewed_on="2026-09-26",
        sources=(("DeepInfra infrastructure statement", "https://deepinfra.com/"),),
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
