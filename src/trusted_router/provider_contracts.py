"""Pinned provider contract identifiers shared by runtime and refresh code."""

MAPLE_MODEL_ID = "dealignai/deepseek-v4.1-flash-1017-uncensored"
MAPLE_NATIVE_ID = "deepseek-v4-1-flash-1017-uncensored"
BASETEN_SYSTEMONE_PREFIX = "decision-models/"
BASETEN_SYSTEMONE_HOLD_REASON = "systemone-adapter-not-implemented"
CONFIDENTIAL_AI_RETIRED_MODEL_IDS = frozenset({
    "deepseek/deepseek-v4-flash-0731", "deepseek/deepseek-v4.1-flash",
})
MAKORA_NON_CHAT_MODEL_IDS = frozenset({
    "cloudflare/clef-flash", "perplexity-ai/pplx-decider-v1.2",
})
# The Transport V2 helper and cloud-local key must be verified in each fleet
# before its region is admitted. Unknown regions remain unavailable.
MAPLE_GATEWAY_REGIONS: frozenset[str] = frozenset({
    "us-central1", "us-east4", "europe-west4", "us-west1",
    "aws-eu-west-1", "aws-eu-west-3", "azure-uaenorth", "azure-australiaeast",
})

# AWS BootstrapData.Region and Azure QUILL_AZURE_REGION carry native names.
# These are alternate wire names for reviewed fleets, not additional locations.
_NATIVE_GATEWAY_ORIGINS = {
    "aws-eu-west-1": "eu-west-1",
    "aws-eu-west-3": "eu-west-3",
    "azure-uaenorth": "uaenorth",
    "azure-australiaeast": "australiaeast",
}


def qualified_gateway_origin_names(regions: frozenset[str]) -> frozenset[str]:
    """Admit a native wire name only while its exact fleet remains qualified."""
    return regions | frozenset(
        native for fleet, native in _NATIVE_GATEWAY_ORIGINS.items() if fleet in regions
    )

SAKANA_FUGU_MODEL_ID = "sakana-ai/fugu-ultra-v1.1"
SAKANA_NAMAZU_MODEL_ID = "sakana-ai/sakana-namazu-v1.0"
SAKANA_NAMAZU_ROUTE_HOLD_REASON = "provider-geographic-restriction"
NEXTBIT_UNSLOPNEMO_MODEL_ID = "thedrummer/unslopnemo-12b-v4.1"
NEXTBIT_UNSLOPNEMO_HOLD_REASON = "provider-alias-unavailable"
NEXTBIT_UNAVAILABLE_MODEL_IDS = frozenset({
    NEXTBIT_UNSLOPNEMO_MODEL_ID,
    # October 22: absent from /models and direct authenticated chat returns
    # HTTP 400 "Model alias ... not found or not default". Require recovery
    # evidence before a stale catalog or hourly refresh can restore routing.
    "undi95/remm-slerp-l2-13b",
    "sao10k/l3.3-70b-euryale-v2.3",
})
STREAMLAKE_KIMI_K2_0905_MODEL_ID = "moonshotai/kimi-k2-instruct-0905"
STREAMLAKE_KIMI_K2_0905_HOLD_REASON = "provider-model-not-found"
PEARL_GEMMA_4_31B_MODEL_ID = "google/gemma-4-31b-it"
PEARL_GEMMA_4_31B_HOLD_REASON = "provider-model-not-found"
THIRD_PARTY_PASSTHROUGH_HOLD_REASON = "third-party-passthrough-disabled"
# These providers also advertise third-party API wrappers. Only their native
# hosted namespaces may route here; the original providers serve passthroughs.
_NATIVE_HOSTED_MODEL_PREFIXES = {
    "lightning": "lightning-ai/",
    "cloudflare-workers-ai": "@cf/",
}
_NATIVE_HOSTED_MODEL_EXCEPTIONS = frozenset({
    # Reviewed Workers AI early-access deployment, before model-search listing.
    ("cloudflare-workers-ai", "moonshotai/kimi-k3"),
})
# Hold a provider's prepaid routes when its operator key is rejected, e.g.
# {"crusoe": "operator-credential-rejected"}. Catalog discovery can succeed
# without validating inference credentials, so clear a hold only after a
# paid-path canary with the replacement key. Holds never affect a customer's
# own BYOK credentials. Crusoe (held October 3) was cleared on October 5 after
# its replacement key passed paid-path canaries.
PREPAID_PROVIDER_HOLD_REASONS: dict[str, str] = {
    # November 6: direct API HTTP 429 / code 1102, "Account balance not
    # enough". Joseph requested the managed route stay unavailable. Clear
    # only after funding and a successful paid-path recovery canary.
    "kling": "operator-credit-exhausted",
    # Sonar is now reformulated as Agent API calls. Its reported input/search
    # costs no longer reconcile with the old fixed-fee catalog contract.
    # Keep managed billing dark until a reviewed adapter accounts for them.
    # https://docs.perplexity.ai/docs/agent-api/migrate-from-sonar/overview
    "perplexity": "billing-contract-unverified",
}
# Lightning revoked platform access during its October 16 security incident.
# This applies to both managed and customer keys. Require reviewed recovery,
# including fresh credentials and canaries, before removing the hold.
# https://lightning.ai/ (October 2026 Security Incident notice)
PROVIDER_INCIDENT_HOLD_REASONS = {"lightning": "provider-security-incident"}
OPERATOR_HELD_PROVIDER_MODELS = frozenset(
    {
        # OpenAI publishes these weights but its hosted API does not serve
        # them. A customer key cannot turn publisher identity into a route.
        # Keep actual GPT-OSS hosts available for prepaid and BYOK requests.
        ("openai", "openai/gpt-oss-20b"),
        ("openai", "openai/gpt-oss-120b"),
        ("sakana", SAKANA_NAMAZU_MODEL_ID),
        # A listed alias is not proof of serving availability. Require a
        # reviewed recovery before refresh can re-enable this route.
        *(("nextbit", model_id) for model_id in NEXTBIT_UNAVAILABLE_MODEL_IDS),
        # October 17: repeated production 404s reproduced directly as
        # model_not_found on the operator key; Kimi K2.6 succeeds on that key.
        # Public catalog/pricing still list this ID. Require a reviewed live
        # recovery before re-enabling; no official retirement date is known.
        ("streamlake", STREAMLAKE_KIMI_K2_0905_MODEL_ID),
        # October 28: authenticated inference returns 404 model_not_found for
        # the catalog's exact native ID. Discovery alone cannot restore it.
        ("pearl", PEARL_GEMMA_4_31B_MODEL_ID),
        # September 20: discovery admits these standard RedPill IDs, but the
        # enclave's reviewed Phala dispatch map does not. Keep them out of
        # routing until a reviewed adapter release, not an attestation bypass.
        ("phala", "z-ai/glm-5.3"),
        ("phala", "z-ai/glm-5.3-flash"),
        # NEAR AI GLM 5.3 Flash was held October 15 to November 2026 while the
        # gateway sidecar refused its drifted attestation. quill-cloud-proxy
        # #720 re-pinned both pool members to their reviewed dstack 0.5.12
        # identities. Only the GCP fleet runs that release, so the route is
        # gated to it in EXPLICIT_GATEWAY_REGIONS_BY_PROVIDER_MODEL below.
    }
)
EXACT_GLOBAL_SETTLEMENT_PROVIDER_MODELS = frozenset(
    {
        ("sakana", SAKANA_FUGU_MODEL_ID),
    }
)

# Native task meters include provider-side preprocessing. Preserve the exact
# reported input count, and do not apply the generic output-token price floor.
INPUT_ONLY_PROVIDER_MODELS = frozenset(
    ("scaledown", f"scaledown/{task}")
    for task in ("compress", "summarize", "extract", "classify")
)
INPUT_ONLY_PROVIDER_MODELS |= {("cloudflare-workers-ai", "cloudflare/clairic")}
EXACT_GLOBAL_SETTLEMENT_PROVIDER_MODELS |= INPUT_ONLY_PROVIDER_MODELS
PASSTHROUGH_RETAIL_PROVIDER_MODELS = frozenset(
    {
        # Match the public Fugu price used by other router marketplaces while
        # preserving Sakana's exact first-party cost in its manifest.
        ("sakana", SAKANA_FUGU_MODEL_ID),
    }
)
UNSUPPORTED_GATEWAY_REGIONS_BY_PROVIDER_MODEL = {
    # Sakana's API terms exclude the EEA, UK, and Switzerland, and the provider
    # may enforce that boundary by source IP. The Europe gateway therefore
    # cannot authorize Fugu until Sakana expands its supported regions.
    ("sakana", SAKANA_FUGU_MODEL_ID): frozenset({"europe-west4"}),
    ("sakana", SAKANA_NAMAZU_MODEL_ID): frozenset({"europe-west4"}),
}
# QCP #535 must be verified on a gateway fleet before it can serve Bedrock.
# An upgraded GCP control plane may also authorize older AWS/Azure gateways.
BEDROCK_GATEWAY_REGIONS = frozenset({"us-central1", "us-east4", "europe-west4", "us-west1"})
BEDROCK_RUNTIME_PROFILES = {
    "anthropic/claude-haiku-5.5": "us.anthropic.claude-haiku-5-5",
    "anthropic/claude-sonnet-5.5": "us.anthropic.claude-sonnet-5-5",
    "anthropic/claude-opus-5.5": "us.anthropic.claude-opus-5-5",
    "anthropic/claude-sonnet-5": "us.anthropic.claude-sonnet-5",
    "anthropic/claude-opus-4.8": "us.anthropic.claude-opus-4-8",
}
# Runtime dispatch needs QCP #630, included in the measured QCP #624 release.
# Keep this separate from the legacy Mantle adapter's GCP-only admission.
BEDROCK_RUNTIME_GATEWAY_REGIONS: frozenset[str] = frozenset({
    "us-central1", "us-east4", "europe-west4", "us-west1",
    "aws-eu-west-1", "aws-eu-west-3", "azure-uaenorth", "azure-australiaeast",
})
# Clairic needs the native /ai/run adapter. Publish only after the GCP fleet
# rolls; older AWS/Azure gateways must not send extraction tasks to chat.
CLAIRIC_GATEWAY_REGIONS = frozenset({"us-central1", "us-east4", "europe-west4", "us-west1"})
# Baseten's System One models, reviewed against its published contract
# (https://docs.baseten.co/reference/inference-api/system-one): POST
# /v1/systemone, input-only tariff in /models. They route only as decisions,
# never as chat. Any other decision-models/ ID remains held until reviewed.
BASETEN_SYSTEMONE_MODEL_IDS: frozenset[str] = frozenset({
    "decision-models/baseten-elephant-9b-latest",
    "decision-models/baseten-mammoth-27b-latest",
    "decision-models/baseten-joey-2b-latest",
})
# The gateway's Baseten System One adapter (quill-cloud-proxy) must be deployed
# to a fleet before it can serve these. Older gateways send hosted decisions to
# /evaluate, which Baseten does not serve.
BASETEN_SYSTEMONE_GATEWAY_REGIONS = frozenset({"us-central1", "us-east4", "europe-west4", "us-west1"})
# NEAR AI's attestation re-pin (quill-cloud-proxy #720) is live only on the
# GCP fleet. AWS and Azure gateways still embed the pre-re-pin
# near_ai_policy.json, refuse NEAR's current attestation and return 502.
# Remove when AWS/Azure gateways run quill-cloud-proxy >= 366ffd91 (NEAR
# policy re-pin, QCP #720).
NEAR_AI_GATEWAY_REGIONS = frozenset({"us-central1", "us-east4", "europe-west4", "us-west1"})
# Regolo's decision adapter also requires a cloud-local credential. The Azure
# bundle has no Regolo entry; only the GCP fleet is verified for this route.
EXPLICIT_GATEWAY_REGIONS_BY_PROVIDER_MODEL = {
    ("cloudflare-workers-ai", "cloudflare/clairic"): CLAIRIC_GATEWAY_REGIONS,
    ("regolo", "cloudflare/clef-flash"): frozenset({"us-central1", "us-east4", "europe-west4", "us-west1"}),
    **{("baseten", model_id): BASETEN_SYSTEMONE_GATEWAY_REGIONS for model_id in BASETEN_SYSTEMONE_MODEL_IDS},
    # Remove when AWS/Azure gateways run quill-cloud-proxy >= 366ffd91 (NEAR
    # policy re-pin, QCP #720).
    ("near-ai", "z-ai/glm-5.3-flash"): NEAR_AI_GATEWAY_REGIONS,
}

# Fugu reports additive provider-side orchestration tokens only after a call.
# They are included in exact settlement, but may exceed the caller-derived
# estimate. Regional quota leases cap settlement to their initial escrow, so
# Fugu must stay on the global typed ledger until leases support exact overruns.

# Sakana's terms exclude the EEA, UK, and Switzerland, and its edge returns an
# HTML 403 to the europe-west4 gateway. The canonical API currently includes
# every gateway region in one DNS answer, so a region-local exclusion would
# make ordinary Namazu calls fail nondeterministically. Keep the discovered
# model visible but globally unroutable until canonical steering can guarantee
# a supported egress region without bypassing Sakana's geographic policy.


def baseten_systemone_model_reviewed(model_id: str, upstream_id: str | None = None) -> bool:
    return model_id in BASETEN_SYSTEMONE_MODEL_IDS and (upstream_id or model_id) == model_id


def provider_model_operator_held(
    provider_slug: str, model_id: str, upstream_id: str | None = None,
) -> bool:
    if provider_slug in PROVIDER_INCIDENT_HOLD_REASONS:
        return True
    native_prefix = _NATIVE_HOSTED_MODEL_PREFIXES.get(provider_slug)
    native_id = upstream_id or model_id
    # November 6: the provider removed these IDs and returns 401
    # model_not_authorized. Do not silently substitute the new dated model.
    if provider_slug == "confidential-ai" and (
        model_id.casefold() in CONFIDENTIAL_AI_RETIRED_MODEL_IDS
        or native_id.casefold().replace("deepseek-ai/", "deepseek/", 1)
        in CONFIDENTIAL_AI_RETIRED_MODEL_IDS
    ):
        return True
    # Makora's generic model feed advertises text output for these, but its
    # chat API rejects them. Stale manifests must not restore either key path.
    if provider_slug == "makora" and (
        model_id.casefold() in MAKORA_NON_CHAT_MODEL_IDS
        or native_id.casefold() in MAKORA_NON_CHAT_MODEL_IDS
    ):
        return True
    # System One decisions share /models with chat but use a different API.
    # Only reviewed IDs, under their exact native ID, may route, and catalog
    # ingest admits them only as decide routes. Everything else stays held.
    if provider_slug == "baseten" and (
        native_id.startswith(BASETEN_SYSTEMONE_PREFIX)
        or model_id.startswith(BASETEN_SYSTEMONE_PREFIX)
    ):
        return not baseten_systemone_model_reviewed(model_id, upstream_id)
    if native_prefix is not None and not native_id.startswith(native_prefix):
        if (provider_slug, native_id) not in _NATIVE_HOSTED_MODEL_EXCEPTIONS:
            return True
    return (provider_slug, model_id) in OPERATOR_HELD_PROVIDER_MODELS


def provider_route_reviewed_hold(
    provider_slug: str, model_id: str, upstream_id: str | None = None,
) -> bool:
    """A reviewed operator decision keeps this route dark: an operator hold,
    a provider incident, or a prepaid billing hold. Such a route is held on
    purpose, not drifted, so freshness reports read it from here."""
    return (
        provider_slug in PREPAID_PROVIDER_HOLD_REASONS
        or provider_model_operator_held(provider_slug, model_id, upstream_id)
    )


def provider_model_requires_exact_global_settlement(
    provider_slug: str,
    model_id: str,
) -> bool:
    return (provider_slug, model_id) in EXACT_GLOBAL_SETTLEMENT_PROVIDER_MODELS


def provider_model_uses_passthrough_retail_price(
    provider_slug: str,
    model_id: str,
) -> bool:
    return (provider_slug, model_id) in PASSTHROUGH_RETAIL_PROVIDER_MODELS


# Prompt accounting on the settle wire. A request's input count EXCLUDES its
# cached tokens (cache reads and writes are counted beside it) when the enclave
# relays Anthropic-native usage: the anthropic slug itself, and the two
# resellers that dispatch an anthropic/ model to Anthropic's own runtime
# (enclave-go/internal/llm/multi.go: amazon-bedrock for an upstream id under
# us.anthropic., google-vertex for a direct model id under claude-). Every
# other provider's input count includes its cached subset. The synchronous
# settle (settle_outbox_apply.normalized_prompt_accounting), the outbox repair
# path, the async settle boundary, the frozen async billing snapshot
# (billing_snapshot's prompt convention) and Stage D's delivered-usage billing
# (storage_gcp_stage_d.normalized_delivered_prompt: the heartbeat's running
# charge and cap check, the reaper's snapshot booking and its generation) all
# read this one rule, keyed on the provider slug and the router model id, the
# two fields every one of them has.
# tests/test_resold_claude_cache_accounting.py holds it to the enclave's
# dispatch conditions over the whole catalog.
CACHE_EXCLUSIVE_INPUT_PROVIDERS = frozenset({"anthropic"})
CACHE_EXCLUSIVE_INPUT_RESELLERS = frozenset({"amazon-bedrock", "google-vertex"})
CACHE_EXCLUSIVE_INPUT_PUBLISHER_PREFIX = "anthropic/"


def provider_input_excludes_cache(provider_slug: str, model_id: str | None = None) -> bool:
    """Whether the reported input count of a request served by this provider,
    for this router model id, excludes its cached tokens.

    Without a model id (a caller that knows the slug alone) the answer is the
    slug's: true for anthropic, false for every other provider.
    """
    # Equality, not set membership: any value (a slug of another type included)
    # answers as `provider_slug == "anthropic"` did, without raising.
    if any(provider_slug == name for name in CACHE_EXCLUSIVE_INPUT_PROVIDERS):
        return True
    return (
        model_id is not None
        and any(provider_slug == name for name in CACHE_EXCLUSIVE_INPUT_RESELLERS)
        and model_id.startswith(CACHE_EXCLUSIVE_INPUT_PUBLISHER_PREFIX)
    )


# Endpoints whose provider reports its own context size to choose a price tier
# (the settle body's price_tier_input_tokens). Only Fugu defines one.
PRIVATE_PRICE_TIER_BASIS_PROVIDER_MODELS = frozenset({("sakana", SAKANA_FUGU_MODEL_ID)})


def provider_model_has_private_price_tier_basis(provider_slug: str, model_id: str) -> bool:
    """Whether the provider reports its own context size to choose a price tier.

    The synchronous settle reads the reported basis for these pinned models
    alone. A frozen async billing snapshot selects tiers by the total prompt
    and cannot represent it, so authorize answers such a request
    async_eligible false.
    """
    return any(
        provider_slug == provider and model_id == model
        for provider, model in PRIVATE_PRICE_TIER_BASIS_PROVIDER_MODELS
    )


def provider_model_available_from_gateway_region(
    provider_slug: str,
    model_id: str,
    gateway_region: str,
) -> bool:
    if provider_slug == "maple":
        return model_id == MAPLE_MODEL_ID and gateway_region in qualified_gateway_origin_names(MAPLE_GATEWAY_REGIONS)
    if provider_slug == "amazon-bedrock":
        return gateway_region in (
            qualified_gateway_origin_names(BEDROCK_RUNTIME_GATEWAY_REGIONS) if model_id in BEDROCK_RUNTIME_PROFILES
            else BEDROCK_GATEWAY_REGIONS
        )
    required_regions = EXPLICIT_GATEWAY_REGIONS_BY_PROVIDER_MODEL.get((provider_slug, model_id))
    if required_regions is not None:
        return gateway_region in required_regions
    unsupported = UNSUPPORTED_GATEWAY_REGIONS_BY_PROVIDER_MODEL.get(
        (provider_slug, model_id),
        frozenset(),
    )
    return gateway_region not in unsupported
