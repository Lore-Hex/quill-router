"""Pinned provider contract identifiers shared by runtime and refresh code."""

SAKANA_FUGU_MODEL_ID = "sakana-ai/fugu-ultra-v1.1"
SAKANA_NAMAZU_MODEL_ID = "sakana-ai/sakana-namazu-v1.0"
SAKANA_NAMAZU_ROUTE_HOLD_REASON = "provider-geographic-restriction"
NEXTBIT_UNSLOPNEMO_MODEL_ID = "thedrummer/unslopnemo-12b-v4.1"
NEXTBIT_UNSLOPNEMO_HOLD_REASON = "provider-alias-unavailable"
STREAMLAKE_KIMI_K2_0905_MODEL_ID = "moonshotai/kimi-k2-instruct-0905"
STREAMLAKE_KIMI_K2_0905_HOLD_REASON = "provider-model-not-found"
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
        ("sakana", SAKANA_NAMAZU_MODEL_ID),
        # A listed alias is not proof of serving availability. Require a
        # reviewed recovery before refresh can re-enable this route.
        ("nextbit", NEXTBIT_UNSLOPNEMO_MODEL_ID),
        # October 17: repeated production 404s reproduced directly as
        # model_not_found on the operator key; Kimi K2.6 succeeds on that key.
        # Public catalog/pricing still list this ID. Require a reviewed live
        # recovery before re-enabling; no official retirement date is known.
        ("streamlake", STREAMLAKE_KIMI_K2_0905_MODEL_ID),
        # September 20: discovery admits these standard RedPill IDs, but the
        # enclave's reviewed Phala dispatch map does not. Keep them out of
        # routing until a reviewed adapter release, not an attestation bypass.
        ("phala", "z-ai/glm-5.3"),
        ("phala", "z-ai/glm-5.3-flash"),
        # October 15: every NEAR AI GLM 5.3 Flash request since 2026-10-12
        # 18:06Z is refused by the gateway's attestation sidecar ("deployment
        # history is outside the reviewed policy"): NEAR appended a deployment
        # action on the reviewed host, so the pinned action-log hash in
        # quill-cloud-proxy near_ai_policy.json no longer matches. Hold the
        # route, not the verification, until a reviewed re-pin ships.
        ("near-ai", "z-ai/glm-5.3-flash"),
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
# Clairic needs the native /ai/run adapter. Publish only after the GCP fleet
# rolls; older AWS/Azure gateways must not send extraction tasks to chat.
CLAIRIC_GATEWAY_REGIONS = frozenset({"us-central1", "us-east4", "europe-west4", "us-west1"})
# Regolo's decision adapter also requires a cloud-local credential. The Azure
# bundle has no Regolo entry; only the GCP fleet is verified for this route.
EXPLICIT_GATEWAY_REGIONS_BY_PROVIDER_MODEL = {
    ("cloudflare-workers-ai", "cloudflare/clairic"): CLAIRIC_GATEWAY_REGIONS,
    ("regolo", "cloudflare/clef-flash"): frozenset({"us-central1", "us-east4", "europe-west4", "us-west1"}),
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


def provider_model_operator_held(
    provider_slug: str, model_id: str, upstream_id: str | None = None,
) -> bool:
    if provider_slug in PROVIDER_INCIDENT_HOLD_REASONS:
        return True
    native_prefix = _NATIVE_HOSTED_MODEL_PREFIXES.get(provider_slug)
    native_id = upstream_id or model_id
    if native_prefix is not None and not native_id.startswith(native_prefix):
        if (provider_slug, native_id) not in _NATIVE_HOSTED_MODEL_EXCEPTIONS:
            return True
    return (provider_slug, model_id) in OPERATOR_HELD_PROVIDER_MODELS


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


def provider_model_available_from_gateway_region(
    provider_slug: str,
    model_id: str,
    gateway_region: str,
) -> bool:
    if provider_slug == "amazon-bedrock":
        return gateway_region in BEDROCK_GATEWAY_REGIONS
    required_regions = EXPLICIT_GATEWAY_REGIONS_BY_PROVIDER_MODEL.get((provider_slug, model_id))
    if required_regions is not None:
        return gateway_region in required_regions
    unsupported = UNSUPPORTED_GATEWAY_REGIONS_BY_PROVIDER_MODEL.get(
        (provider_slug, model_id),
        frozenset(),
    )
    return gateway_region not in unsupported
