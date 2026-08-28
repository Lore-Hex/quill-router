"""Static catalog data: hand-authored providers, model/endpoint dataclasses,
privacy-tier + orchestration constants, and default model orders.

Extracted from catalog.py (#38). Pure data + dataclasses — depends only on the
pricing types (PriceTier), never on the live MODELS/MODEL_ENDPOINTS registries
(built at import time in catalog.py from this data + the ingested snapshot).
catalog.py re-exports these names for backward compatibility.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TypedDict

from trusted_router.pricing import PriceTier


@dataclass(frozen=True)
class Provider:
    slug: str
    name: str
    supports_chat: bool = True
    supports_messages: bool = False
    supports_embeddings: bool = False
    supports_prepaid: bool = False
    supports_byok: bool = True
    attested_gateway: bool = True
    # Conservative default: assume an upstream provider stores request /
    # response content unless we've VERIFIED otherwise from its published
    # policy. Overclaiming privacy (labelling a storing/training provider
    # as "no-store") is the one thing a verifiable-privacy product must
    # never do — so the floor is "assume stored", and providers earn a
    # higher tier only with an explicit, cited flag below.
    stores_content: bool = True
    provider_zero_data_retention: bool | None = None
    # Some upstream privacy agreements apply only to TrustedRouter's managed
    # provider account. Keep that distinct from provider-wide ZDR so customer
    # BYOK credentials never inherit contractual controls they may not have.
    prepaid_zero_data_retention: bool = False
    prepaid_zero_data_retention_effective_on: str | None = None
    provider_confidential_compute: bool | None = None
    provider_e2ee: bool | None = None
    provider_policy: str = (
        "No public zero-retention, confidential-compute, or provider-side "
        "end-to-end-encryption claim is tracked yet."
    )
    provider_policy_url: str | None = None
    provider_headquarters_country: str | None = None


PRIVACY_TIER_STANDARD = 0  # no tracked posture (would store content)

PRIVACY_TIER_NO_STORE = 1  # does not store request/response content

PRIVACY_TIER_ZERO_RETENTION = 2  # contractual / policy zero data retention

PRIVACY_TIER_CONFIDENTIAL = 3  # confidential compute + provider-side e2ee

PRIVACY_TIER_ALIASES: dict[str, int] = {
    "standard": PRIVACY_TIER_STANDARD,
    "any": PRIVACY_TIER_STANDARD,
    "no_store": PRIVACY_TIER_NO_STORE,
    "no-store": PRIVACY_TIER_NO_STORE,
    "nostore": PRIVACY_TIER_NO_STORE,
    "zdr": PRIVACY_TIER_ZERO_RETENTION,
    "zero_retention": PRIVACY_TIER_ZERO_RETENTION,
    "zero-retention": PRIVACY_TIER_ZERO_RETENTION,
    "confidential": PRIVACY_TIER_CONFIDENTIAL,
    "e2e": PRIVACY_TIER_CONFIDENTIAL,
    "e2ee": PRIVACY_TIER_CONFIDENTIAL,
    "max": PRIVACY_TIER_CONFIDENTIAL,
    "maximum": PRIVACY_TIER_CONFIDENTIAL,
}

PRIVACY_TIER_LABELS: dict[int, str] = {
    PRIVACY_TIER_STANDARD: "Standard",
    PRIVACY_TIER_NO_STORE: "No-store",
    PRIVACY_TIER_ZERO_RETENTION: "Zero retention",
    PRIVACY_TIER_CONFIDENTIAL: "Confidential + E2EE",
}

# provider_headquarters_country records the legal home of the entity that
# OPERATES the API endpoint TrustedRouter routes to, as an ISO 3166-1 alpha-2
# code, taken from that entity's own published terms, privacy policy, or
# regulatory filing. Each provider below carries a comment with the entity name
# and the source URL the code was read from.
#
# Three rules keep this field honest:
#   1. It is a jurisdiction signal about the operator, NOT a data-residency or
#      processing-location claim. A US operator may still serve a request from
#      hardware outside the US, and an EU-registered operator may not.
#   2. It is separate from where the MODEL was built. Model-creator countries
#      live in MODEL_ORIGINS below, and the two disagree often (Chinese-origin
#      open weights served by a Singapore or US operator, for example).
#   3. None means "checked and not established", never "unknown, assume the
#      convenient answer". provider.jurisdiction filtering treats a missing
#      country as a non-match, so leaving it None is the conservative outcome;
#      every None is recorded in PROVIDER_JURISDICTION_UNVERIFIED with what was
#      checked, and tests fail on a provider that is in neither state.
PROVIDER_JURISDICTION_US = "US"

PROVIDER_JURISDICTION_CA = "CA"

PROVIDER_JURISDICTION_CN = "CN"

PROVIDER_JURISDICTION_DE = "DE"

PROVIDER_JURISDICTION_FR = "FR"

PROVIDER_JURISDICTION_GB = "GB"

PROVIDER_JURISDICTION_IL = "IL"

PROVIDER_JURISDICTION_KR = "KR"

PROVIDER_JURISDICTION_NL = "NL"

PROVIDER_JURISDICTION_SE = "SE"

PROVIDER_JURISDICTION_SG = "SG"

# Providers whose operating entity TrustedRouter checked and could not pin down.
# The value records what was read and why it was not enough, so the next audit
# starts where this one stopped instead of repeating it. Keys must be provider
# slugs whose provider_headquarters_country is None.
PROVIDER_JURISDICTION_UNVERIFIED: dict[str, str] = {
    **{
        slug: (
            "Checked the provider API and product documentation, but the "
            "contracting API operator's incorporation country has not yet been "
            "established from first-party legal terms. Jurisdiction filters "
            "therefore exclude this route."
        )
        for slug in (
            "aion-labs",
            "akashml",
            "arcee",
            "byteplus",
            "inception",
            "io-net",
            "krea",
            "liquid",
            "mancer",
            "modal",
            "nextbit",
            "perceptron",
            "perplexity",
            "reka",
            "riverflow",
            "sail-research",
            "sakana",
            "sambanova",
        )
    },
    "openrouter": (
        "Checked OpenRouter's published privacy and logging documentation. "
        "Downstream operators are selected by OpenRouter and their legal "
        "entity and country are not published per-route, so jurisdiction "
        "filters conservatively exclude these routes."
    ),
    "phala": (
        "Checked phala.com/terms (names Hashforest Technology LLC, California "
        "law) and redpill.ai/terms for api.redpill.ai, the endpoint TrustedRouter "
        "routes to: its body names Hashforest Technology Pte. Ltd while its "
        "footer reads Hashforest Technology LLC. A US-suffix and a "
        "Singapore-suffix entity name for the same service cannot both be the "
        "operator, so no country is recorded until Phala publishes one name."
    ),
    "engy": (
        "Checked engy.ai, engy.ai/terms, and engy.ai/privacy: no legal entity, "
        "registered address, or governing-law clause appears on any of them. "
        "Third-party coverage ties Engy to a Bittensor subnet operated by a team "
        "called Hanlin AI, with no incorporation record located."
    ),
    "relace": (
        "Checked relace.ai, docs.relace.ai, and the linked legal pages. The "
        "public material does not identify the API operator's incorporation "
        "country, so jurisdiction filters conservatively exclude this route."
    ),
    "recraft": (
        "Checked recraft.ai legal terms and privacy pages. They publish a US "
        "mailing address and New York governing law, but do not identify the "
        "API operator's legal entity or incorporation country."
    ),
}


# Jurisdiction and privacy controls that already exist. Reuse these; a second
# mechanism for the same rule is a second thing to keep true:
#   * US_PROVIDER_ONLY_MODEL_IDS (below) is the request-time rule that pins
#     OpenPatcher and Athena ids to US-operated providers. routing.py turns a
#     match into provider_jurisdiction=US on the request.
#   * The provider.jurisdiction request preference in routing.py filters
#     candidate endpoints against provider_headquarters_country. It accepts only
#     'us' today; new codes here do not widen the API on their own.
#   * EU_MODEL_ID ("trustedrouter/eu") and EU_FOCUSED_PROVIDER_ORDER carry the
#     EU-focused provider preference. That order is a routing preference, not a
#     claim that every provider in it is EU-based.
#   * PRIVACY_TIER_* plus Provider.stores_content and the ZDR, confidential-
#     compute, and E2EE flags carry retention and confidentiality posture.
#     Jurisdiction is orthogonal: a US operator can be Standard tier and a
#     non-US operator can be ZDR.
#   * MODEL_ORIGINS (end of file) carries model-creator countries. Do not read a
#     provider's country as its models' origin, or the reverse.


@dataclass(frozen=True)
class ModelProviderPrivacyOverride:
    privacy_tier: int
    stores_content: bool | None = None
    provider_zero_data_retention: bool | None = None
    provider_confidential_compute: bool | None = None
    provider_e2ee: bool | None = None
    provider_policy: str | None = None
    provider_policy_url: str | None = None


# Audited 2026-07-27 against Venice's live catalog and attestation evidence.
# Venice's labels are not accepted as hardware proof: no current route exposes
# a client-verifiable chain from the live TLS key through immutable source and
# hardware measurements to committed model weights. Any new Venice route
# therefore defaults to Standard. The model-level list below records only
# Venice's separate, policy-backed "Private" retention claim.
_VENICE_PRIVATE_MODEL_IDS = frozenset(
    {
        "qwen/qwen3-235b-a22b-thinking-2507",
        "qwen/qwen3.5-9b",
        "qwen/qwen3.6-27b",
        "z-ai/glm-4.6",
        "z-ai/glm-4.7",
        "z-ai/glm-4.7-flash",
        "z-ai/glm-5",
        "z-ai/glm-5.1",
        "z-ai/glm-5.2",
    }
)
_VENICE_PRIVATE_POLICY = (
    "Venice's live model catalog marks this exact route private. Venice describes "
    "Private as contract-enforced zero data retention. This is policy-backed, not "
    "hardware-verified. TrustedRouter cannot verify a complete chain from the live "
    "endpoint to immutable source and model weights, so the route is not tracked as "
    "TEE or E2EE and is excluded from trustedrouter/e2e."
)


_MODEL_PROVIDER_PRIVACY_OVERRIDES: dict[tuple[str, str], ModelProviderPrivacyOverride] = {
    **{
        (model_id, "openai"): ModelProviderPrivacyOverride(
            privacy_tier=PRIVACY_TIER_STANDARD,
            provider_zero_data_retention=False,
            provider_confidential_compute=False,
            provider_e2ee=False,
            provider_policy=(
                "OpenAI's video generation route is tracked separately from the "
                "managed text account's ZDR posture. TrustedRouter deletes the "
                "generated provider asset after relaying it, but does not claim "
                "provider-side zero retention for Sora video jobs."
            ),
            provider_policy_url="https://developers.openai.com/api/docs/guides/video-generation",
        )
        for model_id in ("openai/sora-2", "openai/sora-2-pro")
    },
    (
        "anthropic/claude-fable-5",
        "*",
    ): ModelProviderPrivacyOverride(
        privacy_tier=PRIVACY_TIER_STANDARD,
        provider_zero_data_retention=False,
        provider_policy=(
            "Claude Fable 5 is available, but it is not "
            "tracked as ZDR for TrustedRouter. It is excluded from "
            "trustedrouter/zdr and provider.min_privacy=zdr routing."
        ),
        provider_policy_url="https://platform.claude.com/docs/en/api/data-retention",
    ),
    (
        "moonshotai/kimi-k2.6",
        "wafer",
    ): ModelProviderPrivacyOverride(
        privacy_tier=PRIVACY_TIER_STANDARD,
        provider_zero_data_retention=False,
        provider_policy=(
            "Wafer withdrew ZDR support for Kimi-K2.6 on 2026-06-26 "
            "(capabilities.zdr.supported=false in their /v1/models). The "
            "route is served at standard tier and excluded from "
            "trustedrouter/zdr and provider.min_privacy=zdr routing."
        ),
    ),
    **{
        (model_id, "phala"): ModelProviderPrivacyOverride(
            privacy_tier=PRIVACY_TIER_STANDARD,
            stores_content=True,
            provider_zero_data_retention=False,
            provider_confidential_compute=False,
            provider_e2ee=False,
            provider_policy=(
                "Phala currently serves this model through an upstream-author "
                "pass-through route, not a phala/* Confidential AI endpoint. "
                "TrustedRouter therefore makes no ZDR, confidential-compute, or "
                "E2EE claim for this exact route."
            ),
            provider_policy_url=(
                "https://docs.phala.com/phala-cloud/confidential-ai/"
                "confidential-model/confidential-ai-api"
            ),
        )
        for model_id in (
            "moonshotai/kimi-k3",
            "z-ai/glm-5.2",
            "z-ai/glm-5.3-flash",
        )
    },
    **{
        (model_id, "venice"): ModelProviderPrivacyOverride(
            privacy_tier=PRIVACY_TIER_ZERO_RETENTION,
            provider_zero_data_retention=True,
            provider_policy=_VENICE_PRIVATE_POLICY,
            provider_policy_url="https://venice.ai/privacy",
        )
        for model_id in _VENICE_PRIVATE_MODEL_IDS
    },
}


@dataclass(frozen=True)
class Model:
    id: str
    name: str
    provider: str
    context_length: int
    upstream_id: str | None = None
    supports_chat: bool = True
    supports_messages: bool = False
    supports_embeddings: bool = False
    supports_video: bool = False
    supported_parameters: tuple[str, ...] = ()
    input_modalities: tuple[str, ...] = ("text",)
    output_modalities: tuple[str, ...] = ("text",)
    prepaid_available: bool = False
    byok_available: bool = True
    # Headline (low-tier) rates: what /v1/models displays. For
    # tier-aware billing, use `price_tiers` instead and pick the right
    # tier based on the actual prompt token count.
    prompt_price_microdollars_per_million_tokens: int = 0
    completion_price_microdollars_per_million_tokens: int = 0
    published_prompt_price_microdollars_per_million_tokens: int = 0
    published_completion_price_microdollars_per_million_tokens: int = 0
    request_price_microdollars: int = 0
    minimum_charge_microdollars: int = 0
    # Full tier list for context-conditional pricing. Defaults to a
    # single tier matching the headline rates above; the ingest path
    # populates multi-tier values when the snapshot carries them.
    price_tiers: tuple[PriceTier, ...] = ()
    published_price_tiers: tuple[PriceTier, ...] = ()
    hidden_public_metadata: bool = False


@dataclass(frozen=True)
class ModelEndpoint:
    id: str
    model_id: str
    provider: str
    usage_type: str
    upstream_id: str | None = None
    supported_parameters: tuple[str, ...] = ()
    prompt_price_microdollars_per_million_tokens: int = 0
    completion_price_microdollars_per_million_tokens: int = 0
    published_prompt_price_microdollars_per_million_tokens: int = 0
    published_completion_price_microdollars_per_million_tokens: int = 0
    request_price_microdollars: int = 0
    price_tiers: tuple[PriceTier, ...] = ()
    published_price_tiers: tuple[PriceTier, ...] = ()
    first_token_timeout_seconds: float | None = None
    completion_timeout_seconds: float | None = None
    stream_idle_timeout_seconds: float | None = None
    catalog_valid_until: datetime | None = None

    @property
    def is_byok(self) -> bool:
        return self.usage_type.lower() == "byok"

    def catalog_is_current(self, *, at: datetime | None = None) -> bool:
        if self.catalog_valid_until is None:
            return True
        current = at or datetime.now(UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        return current.astimezone(UTC) < self.catalog_valid_until


PROVIDERS: dict[str, Provider] = {
    "trustedrouter": Provider(
        slug="trustedrouter",
        name="TrustedRouter",
        supports_messages=True,
        supports_embeddings=False,
        supports_prepaid=True,
        # BYOK attaches to concrete upstream providers. TrustedRouter
        # orchestration aliases may fan out across multiple managed routes, so
        # the pseudo-provider itself is intentionally credits-only.
        supports_byok=False,
        stores_content=False,
        provider_zero_data_retention=True,
        provider_confidential_compute=True,
        provider_e2ee=True,
        provider_policy=(
            "TrustedRouter's attested gateway stores no prompt or output content. "
            "Provider compute policy still depends on the selected upstream route."
        ),
        provider_policy_url="https://trust.trustedrouter.com",
        provider_headquarters_country=PROVIDER_JURISDICTION_US,
    ),
    "openrouter": Provider(
        slug="openrouter",
        name="OpenRouter",
        supports_prepaid=True,
        supports_byok=False,
        stores_content=True,
        provider_zero_data_retention=False,
        provider_confidential_compute=False,
        provider_e2ee=False,
        provider_policy=(
            "TrustedRouter sends requests through its attested gateway to "
            "OpenRouter, which routes them to a downstream operator. Routes "
            "here are Standard privacy and are excluded from ZDR, "
            "confidential-compute, and E2EE routing."
        ),
        provider_policy_url="https://openrouter.ai/docs/features/privacy-and-logging",
    ),
    "meta": Provider(
        slug="meta",
        name="Meta via OpenRouter",
        supports_prepaid=True,
        supports_byok=False,
        stores_content=True,
        provider_zero_data_retention=False,
        provider_confidential_compute=False,
        provider_e2ee=False,
        provider_policy=(
            "TrustedRouter sends requests through its attested gateway to "
            "OpenRouter, which routes them to Meta. This downstream route is "
            "not marked zero-retention, confidential-compute, or end-to-end "
            "encrypted."
        ),
        provider_policy_url="https://openrouter.ai/docs/features/privacy-and-logging",
        provider_headquarters_country=PROVIDER_JURISDICTION_US,
    ),
    "anthropic": Provider(
        slug="anthropic",
        name="Anthropic",
        supports_messages=True,
        supports_prepaid=True,
        provider_zero_data_retention=False,
        provider_policy=(
            "Not currently marked ZDR in TrustedRouter. Anthropic may offer "
            "contracted or account-specific data-retention terms, but this provider "
            "is excluded from trustedrouter/zdr until that posture is reverified."
        ),
        provider_policy_url="https://platform.claude.com/docs/en/api/data-retention",
        provider_headquarters_country=PROVIDER_JURISDICTION_US,
    ),
    "openai": Provider(
        slug="openai",
        name="OpenAI",
        supports_embeddings=True,
        supports_prepaid=True,
        provider_zero_data_retention=False,
        # Verified 2026-07-29 against the managed production project:
        # a Responses request with store=true succeeded, then retrieval
        # returned 404. BYOK remains outside this account-scoped flag.
        prepaid_zero_data_retention=True,
        prepaid_zero_data_retention_effective_on="2026-07-28",
        provider_policy=(
            "Contracted Zero Data Retention is active for TrustedRouter's managed "
            "OpenAI account, effective July 28, 2026, and live enforcement was "
            "verified on July 29, 2026. This guarantee applies only to "
            "TrustedRouter-funded prepaid routes; customer BYOK credentials use the "
            "data controls on the customer's own OpenAI organization or project."
        ),
        provider_policy_url=(
            "https://developers.openai.com/api/docs/guides/your-data"
            "#data-retention-controls-for-abuse-monitoring"
        ),
        provider_headquarters_country=PROVIDER_JURISDICTION_US,
    ),
    "google-ai-studio": Provider(
        slug="google-ai-studio",
        name="Google AI Studio",
        supports_embeddings=True,
        supports_prepaid=True,
        provider_zero_data_retention=False,
        provider_policy=(
            "Not currently marked ZDR in TrustedRouter. Google AI Studio and the "
            "Gemini Developer API have product- and billing-specific data-use terms, "
            "so this route stays outside trustedrouter/zdr."
        ),
        provider_policy_url="https://ai.google.dev/gemini-api/terms",
        provider_headquarters_country=PROVIDER_JURISDICTION_US,
    ),
    "google-vertex": Provider(
        slug="google-vertex",
        name="Google Vertex AI",
        supports_prepaid=True,
        supports_byok=False,
        provider_zero_data_retention=False,
        prepaid_zero_data_retention=True,
        prepaid_zero_data_retention_effective_on="2026-07-28",
        provider_policy=(
            "TrustedRouter's managed Vertex AI account is covered by contractual "
            "Zero Data Retention. This guarantee applies only to TrustedRouter-funded "
            "prepaid routes. TrustedRouter does not invoke Google Search or Maps "
            "grounding or Gemini Live session resumption on these routes. Google AI "
            "Studio is classified separately."
        ),
        provider_policy_url=(
            "https://docs.cloud.google.com/vertex-ai/generative-ai/docs/"
            "vertex-ai-zero-data-retention"
        ),
        provider_headquarters_country=PROVIDER_JURISDICTION_US,
    ),
    "cerebras": Provider(
        slug="cerebras",
        name="Cerebras",
        supports_prepaid=True,
        stores_content=False,
        provider_zero_data_retention=True,
        provider_policy=(
            "Tracked as provider-ZDR. Cerebras documents zero-retention inference "
            "and ZDR-compliant ephemeral prompt caching that is never persisted."
        ),
        provider_policy_url="https://inference-docs.cerebras.ai/capabilities/prompt-caching",
        provider_headquarters_country=PROVIDER_JURISDICTION_US,
    ),
    "deepseek": Provider(
        slug="deepseek",
        name="DeepSeek",
        supports_prepaid=True,
        provider_zero_data_retention=False,
        provider_policy=(
            "Not ZDR. DeepSeek's published privacy policy says prompts/inputs may be "
            "collected and personal data may be used to train or improve machine "
            "learning models and algorithms."
        ),
        provider_policy_url=(
            "https://cdn.deepseek.com/policies/en-US/deepseek-privacy-policy.html?locale=en_US"
        ),
        # DeepSeek's own privacy policy names Hangzhou DeepSeek Artificial
        # Intelligence Co., Ltd., registered in China, as data controller. The
        # endpoint routed to here is api.deepseek.com.
        # https://cdn.deepseek.com/policies/en-US/deepseek-privacy-policy.html
        provider_headquarters_country=PROVIDER_JURISDICTION_CN,
    ),
    "mistral": Provider(
        slug="mistral",
        name="Mistral",
        supports_prepaid=True,
        provider_policy=(
            "No provider-ZDR claim is tracked here. This is separate from any "
            "no-training or enterprise retention commitments Mistral may offer."
        ),
        provider_policy_url="https://docs.mistral.ai/admin/security-access/privacy",
        # Mistral AI, a French SAS registered at 15 rue des Halles, 75001 Paris,
        # RCS Paris 952 418 325, per its own legal notice.
        # https://legal.mistral.ai/legal-notice
        provider_headquarters_country=PROVIDER_JURISDICTION_FR,
    ),
    "kimi": Provider(
        slug="kimi",
        name="Kimi",
        supports_prepaid=True,
        provider_policy=(
            "No provider-ZDR claim is tracked here. Kimi/Moonshot policy source is linked "
            "for users who need to review API retention and processing terms."
        ),
        provider_policy_url="https://platform.kimi.ai/docs/agreement/userprivacy",
        # MOONSHOT AI PTE. LTD., Singapore. The privacy policy linked above --
        # the one governing the routed endpoint api.moonshot.ai -- opens:
        # "Our services are provided and controlled by MOONSHOT AI PTE. LTD.
        # ... in Singapore", and states storage on "secure servers located in
        # Singapore". Verified 2026-08-17 by rendering the page; a plain fetch
        # returns an empty JS shell, which is how an earlier revision of this
        # entry came to record CN from the LAB's about page (Moonshot AI,
        # Beijing) instead of the operator's own policy. The lab is Chinese and
        # is recorded as such in the model-origin map; the operator is not.
        # https://platform.kimi.ai/docs/agreement/userprivacy
        provider_headquarters_country=PROVIDER_JURISDICTION_SG,
    ),
    "zai": Provider(
        slug="zai",
        name="Z.AI",
        supports_prepaid=True,
        provider_policy=(
            "No provider-ZDR claim is tracked here. Z.AI/BigModel policy source is linked "
            "for users who need to review API retention and processing terms."
        ),
        provider_policy_url="https://open.bigmodel.cn/usercenter/agreement/privacy",
        # Z.AI's terms of use, published in its developer documentation and
        # covering the api.z.ai service routed to here, name JINGSHENG HENGXING
        # TECHNOLOGY PTE. LTD as the operator of Z.ai; those terms are governed
        # by Singapore law with SIAC arbitration seated in Singapore.
        # https://docs.z.ai/legal-agreement/terms-of-use
        # Jurisdiction nuance: the GLM weights served through it come from the
        # Beijing-headquartered lab behind the Z.ai brand (see MODEL_ORIGINS,
        # where z-ai and zai-org are recorded as CN), and Z.AI's China platform
        # runs under a separate Chinese entity. A user avoiding Chinese
        # jurisdiction should read this SG code as the API operator only.
        provider_headquarters_country=PROVIDER_JURISDICTION_SG,
    ),
    # Together AI hosts a broad open-weight catalog (Llama, DeepSeek
    # incl. DeepSeek-OCR, Qwen, Mixtral) plus image gen (FLUX) and
    # embeddings — categories TR didn't otherwise cover. OpenAI-
    # compatible chat completions at api.together.xyz/v1.
    "together": Provider(
        slug="together",
        name="Together",
        supports_embeddings=True,
        supports_prepaid=True,
        stores_content=False,
        provider_zero_data_retention=True,
        provider_policy=(
            "Tracked as provider ZDR. Together documents that inference inputs "
            "and outputs are not stored by default; temporary prompt caching may "
            "be used for performance, and sharing content for training is opt-in."
        ),
        provider_policy_url="https://docs.together.ai/docs/privacy-and-security",
        provider_headquarters_country=PROVIDER_JURISDICTION_US,
    ),
    # Fireworks AI — OpenAI-compatible serverless inference at
    # api.fireworks.ai/inference/v1. The live account currently exposes a
    # compact high-value set: Kimi, DeepSeek, GLM, and GPT OSS routes.
    "fireworks": Provider(
        slug="fireworks",
        name="Fireworks AI",
        supports_prepaid=True,
        provider_policy=(
            "No provider-ZDR claim is tracked here. Fireworks publishes "
            "security, privacy, and zero-retention documentation; enable a "
            "contracted ZDR posture before marking this provider as ZDR."
        ),
        provider_policy_url="https://trust.fireworks.ai",
        provider_headquarters_country=PROVIDER_JURISDICTION_US,
    ),
    # xAI Grok — OpenAI-compatible chat completions at api.x.ai/v1.
    # As of 2026-07, headline model is grok-4.5 ($2/$6 per M, 500k ctx).
    "grok": Provider(
        slug="grok",
        name="xAI Grok",
        supports_prepaid=True,
        provider_policy=(
            "xAI documents no training on API requests and 30-day default audit "
            "retention, with ZDR as an enterprise feature."
        ),
        provider_policy_url="https://docs.x.ai/docs/resources/faq-api/security",
        provider_headquarters_country=PROVIDER_JURISDICTION_US,
    ),
    # Novita — multi-model serverless inference. OpenAI-compatible
    # at api.novita.ai/openai/v1. Hosts DeepSeek, Qwen, Llama,
    # GLM, Kimi (and many more) at competitive rates.
    "novita": Provider(
        slug="novita",
        name="Novita AI",
        supports_prepaid=True,
        provider_policy=(
            "No provider-ZDR claim is tracked here. Novita's privacy policy says "
            "personal information is not used for model training; customer-content "
            "processing is governed by customer agreements."
        ),
        provider_policy_url="https://novita.ai/legal/privacy-policy",
        # United States. Novita's terms select Delaware law with exclusive
        # jurisdiction in Wilmington and publish no operating entity, so this is
        # not read from a public document: it is recorded from TrustedRouter's
        # own account relationship with the provider (operator knowledge,
        # 2026-08-17). Kept distinct in the comment from the entries that cite a
        # published policy, so the basis is never overstated.
        provider_headquarters_country=PROVIDER_JURISDICTION_US,
    ),
    # Phala publishes Intel TDX / NVIDIA Confidential Compute evidence and
    # signed request receipts. TrustedRouter does not yet verify that evidence
    # on every request, so Phala must not satisfy the provider-E2EE routing
    # floor until the gateway enforces the complete receipt chain.
    "phala": Provider(
        slug="phala",
        name="Phala",
        supports_prepaid=True,
        stores_content=False,
        provider_zero_data_retention=True,
        provider_confidential_compute=True,
        provider_e2ee=False,
        provider_policy=(
            "Phala publishes TDX/GPU attestation and signed request receipts, "
            "but TrustedRouter does not yet verify the complete receipt chain "
            "on every routed request. The route is therefore not classified as "
            "provider E2EE and is excluded from trustedrouter/e2e."
        ),
        provider_policy_url=("https://docs.phala.com/phala-cloud/confidential-ai/verify/overview"),
        # No country recorded: see PROVIDER_JURISDICTION_UNVERIFIED["phala"].
    ),
    # SiliconFlow — Chinese serverless inference with 200+ open-weight
    # models. OpenAI-compatible at api.siliconflow.com/v1.
    "siliconflow": Provider(
        slug="siliconflow",
        name="SiliconFlow",
        supports_prepaid=True,
        provider_policy=(
            "No provider-ZDR claim is tracked here. SiliconFlow's privacy policy source "
            "is linked for retention and interaction-data terms."
        ),
        provider_policy_url="https://docs.siliconflow.com/en/legals/privacy-policy",
        # SiliconFlow's international terms of service, covering the
        # siliconflow.com platform whose api.siliconflow.com endpoint is routed
        # to here, name SILICONFLOW TECHNOLOGY PTE. LTD., registered in
        # Singapore; clause 14.1 applies the laws of the Republic of Singapore.
        # https://docs.siliconflow.com/en/legals/terms-of-service
        # Jurisdiction nuance: SiliconFlow's separate China platform on the .cn
        # domain is not this route, and many of the weights served here come from
        # Chinese labs (see MODEL_ORIGINS).
        provider_headquarters_country=PROVIDER_JURISDICTION_SG,
    ),
    # Tinfoil — TEE-attested confidential inference. Verified-no-logs
    # via remote attestation. **Also on-brand for TR's trust story.**
    # OpenAI-compatible at inference.tinfoil.sh/v1.
    "tinfoil": Provider(
        slug="tinfoil",
        name="Tinfoil",
        supports_prepaid=True,
        stores_content=False,
        provider_zero_data_retention=True,
        provider_confidential_compute=True,
        provider_e2ee=True,
        provider_policy=(
            "Tracked as a confidential inference provider with attested "
            "provider compute and no prompt/output logging claims."
        ),
        provider_policy_url="https://tinfoil.sh/security-and-privacy-faq",
        provider_headquarters_country=PROVIDER_JURISDICTION_US,
    ),
    # NEAR AI direct endpoints terminate TLS inside the measured model TEE.
    # TrustedRouter verifies the live TLS SPKI, fresh nonce, Intel TDX quote,
    # NVIDIA GPU evidence, compose-manager action log, and release-pinned
    # workload before sending prompt bytes. No ZDR/no-store claim is inferred
    # from confidential compute alone.
    "near-ai": Provider(
        slug="near-ai",
        name="NEAR AI",
        supports_prepaid=True,
        supports_byok=False,
        provider_confidential_compute=True,
        provider_e2ee=True,
        provider_policy=(
            "TrustedRouter connects directly to the model workload and verifies "
            "the live TLS key, Intel TDX quote, NVIDIA GPUs, deployment action "
            "log, and pinned workload inside the TrustedRouter enclave before "
            "sending content. Verification fails closed. No separate ZDR claim "
            "is currently tracked."
        ),
        provider_policy_url="https://docs.near.ai/cloud/verification/tls/",
        # Jasnah, Inc. d/b/a NEAR AI identifies itself as a Delaware
        # corporation in its first-party Acceptable Use Policy.
        # https://near.ai/acceptable-use-policy
        provider_headquarters_country=PROVIDER_JURISDICTION_US,
    ),
    # Venice's privacy posture is model-specific. Its Private routes carry a
    # policy-backed ZDR claim, while Anonymized routes may be retained by the
    # downstream model provider. Venice does not currently provide the complete
    # independently verifiable source/hardware/model-weight chain required for
    # TrustedRouter to classify any Venice route as TEE or E2EE.
    "venice": Provider(
        slug="venice",
        name="Venice",
        supports_prepaid=True,
        stores_content=True,
        provider_zero_data_retention=False,
        provider_confidential_compute=False,
        provider_e2ee=False,
        provider_policy=(
            "Mixed model-specific posture. TrustedRouter cannot independently verify "
            "a complete chain from a live Venice endpoint through immutable source and "
            "hardware measurements to committed model weights. Venice is therefore "
            "not tracked as confidential or E2EE. Exact routes marked Private in "
            "Venice's live catalog qualify only as policy-backed ZDR through "
            "endpoint-specific records; Anonymized routes do not."
        ),
        provider_policy_url="https://venice.ai/privacy",
        provider_headquarters_country=PROVIDER_JURISDICTION_US,
    ),
    # Parasail — serverless inference platform. Hosts Llama, Qwen,
    # Gemma 4 family, plus their own quantized variants
    # (parasail-* aliases). OpenAI-compatible at api.parasail.io/v1.
    # No public pricing API — pricing scraper falls back to a static
    # table per family until they expose machine-readable rates.
    "parasail": Provider(
        slug="parasail",
        name="Parasail",
        supports_prepaid=True,
        stores_content=False,
        provider_zero_data_retention=True,
        provider_policy=(
            "Tracked as ZDR for serverless and dedicated inference. Parasail documents "
            "no storage or logging of submitted input on those service paths, retention "
            "only while generating and delivering output, and no training on input or "
            "output. Batch service is excluded from this claim; TrustedRouter does not "
            "route Parasail traffic through batch."
        ),
        provider_policy_url=(
            "https://docs.parasail.io/parasail-docs/security-and-account-management/"
            "data-privacy-retention"
        ),
        provider_headquarters_country=PROVIDER_JURISDICTION_US,
    ),
    # Lightning AI — Lightning's hosted inference. OpenAI-compatible at
    # lightning.ai/api/v1. Pricing is published per-model in their
    # /v1/models response (input_cost_per_token + output_cost_per_token),
    # which the scraper consumes directly without scraping HTML.
    "lightning": Provider(
        slug="lightning",
        name="Lightning AI",
        supports_prepaid=True,
        provider_policy=(
            "No provider-ZDR claim is tracked here. Lightning's general privacy and "
            "security documentation is linked for retention review."
        ),
        provider_policy_url="https://lightning.ai/legal/privacy",
        provider_headquarters_country=PROVIDER_JURISDICTION_US,
    ),
    # GMI Cloud — confidential-GPU inference hosted on H100/H200.
    # OpenAI-compatible at api.gmi-serving.com/v1. Pricing is in the
    # /v1/models response under each model's `pricing` block (per-token
    # rates as strings).
    "gmi": Provider(
        slug="gmi",
        name="GMI Cloud",
        supports_prepaid=True,
        provider_policy=(
            "GMI runs isolated/VPC GPU inference, but that is network isolation, "
            "NOT an attested TEE — so no confidential-compute, zero-retention, or "
            "E2EE claim is marked. Retention/training terms are unverified (the "
            "published policy page is JavaScript-only and would not render)."
        ),
        provider_policy_url="https://gmicloud.ai/legal/privacy",
        provider_headquarters_country=PROVIDER_JURISDICTION_US,
    ),
    # FriendliAI — OpenAI-compatible serverless Model API at
    # api.friendli.ai/serverless/v1. Hosts GLM 5.2 plus a compact
    # high-value open-model catalog. Pricing + upstream IDs are read
    # directly from /models by scripts/pricing/providers/friendli.py.
    "friendli": Provider(
        slug="friendli",
        name="FriendliAI",
        supports_prepaid=True,
        provider_policy=(
            "No provider-ZDR claim is tracked here. Friendli's legal/privacy "
            "terms are linked for users who need to review API data handling."
        ),
        provider_policy_url="https://friendli.ai/terms",
        # FriendliAI's own terms name FriendliAI Corp. with a San Francisco, CA
        # address, apply the laws of the State of California, and place
        # non-arbitrated disputes in San Francisco County courts.
        # https://friendli.ai/terms
        provider_headquarters_country=PROVIDER_JURISDICTION_US,
    ),
    # Baseten — OpenAI-compatible Model APIs at inference.baseten.co/v1.
    # Public catalog + pricing is exposed from /v1/models; prompt/output
    # prices are dollars per token and are converted to integer microdollars
    # by scripts/pricing/providers/baseten.py.
    "baseten": Provider(
        slug="baseten",
        name="Baseten",
        supports_prepaid=True,
        stores_content=False,
        provider_zero_data_retention=True,
        provider_policy=(
            "Baseten states that it does not store synchronous Model API inputs "
            "or outputs by default. TrustedRouter uses the synchronous Model API "
            "path; Baseten documents separate temporary input storage for async "
            "inference."
        ),
        provider_policy_url="https://docs.baseten.co/observability/security",
        provider_headquarters_country=PROVIDER_JURISDICTION_US,
    ),
    # Telnyx Inference — OpenAI-compatible chat completions at
    # api.telnyx.com/v2/ai/openai. The authenticated model feed is joined
    # hourly with Telnyx's current pricing page and public x402 catalog.
    "telnyx": Provider(
        slug="telnyx",
        name="Telnyx",
        supports_prepaid=True,
        provider_policy=(
            "No provider-ZDR or confidential-compute claim is tracked here. "
            "Telnyx's privacy policy is linked for users who need to review "
            "inference data handling."
        ),
        provider_policy_url="https://telnyx.com/privacy-policy",
        provider_headquarters_country=PROVIDER_JURISDICTION_US,
    ),
    # Wafer — OpenAI-compatible serverless API at pass.wafer.ai/v1. Wafer
    # supports request-scoped ZDR with `Wafer-ZDR: required`; providers.py's
    # live-provider allowlist sends that header on Wafer routes. The provider
    # itself is not marked globally ZDR because several Wafer models explicitly
    # report zdr_supported=false.
    "wafer": Provider(
        slug="wafer",
        name="Wafer",
        supports_prepaid=True,
        provider_policy=(
            "Wafer supports request-scoped ZDR via Wafer-ZDR: required on "
            "supported models; model-level support differs, so TrustedRouter "
            "keeps provider-level claims conservative."
        ),
        provider_policy_url="https://docs.wafer.ai/serverless/api-reference",
        provider_headquarters_country=PROVIDER_JURISDICTION_US,
    ),
    # Crusoe Managed Inference — OpenAI-compatible API at
    # api.inference.crusoecloud.com/v1. Publishes model availability,
    # supported parameters, context, and pricing in /v1/models; TR keeps
    # provider-native upstream IDs in data/provider_models/crusoe.json.
    "crusoe": Provider(
        slug="crusoe",
        name="Crusoe",
        supports_prepaid=True,
        provider_policy=(
            "No provider-ZDR claim is tracked here. Crusoe's Managed Inference "
            "docs and pricing/catalog pages are linked for model and API "
            "data-handling review."
        ),
        provider_policy_url="https://docs.crusoecloud.com/managed-inference/overview/",
        provider_headquarters_country=PROVIDER_JURISDICTION_US,
    ),
    # Makora Inference — OpenAI-compatible API at inference.makora.com/v1.
    # Its authenticated /v1/models feed supplies model IDs, context windows,
    # capabilities, and account-billable prices to the generated manifest.
    "makora": Provider(
        slug="makora",
        name="Makora",
        supports_prepaid=True,
        provider_policy=(
            "No provider-ZDR claim is tracked here. Makora's inference and "
            "privacy documentation are linked for users who need to review API "
            "data handling."
        ),
        provider_policy_url="https://www.makora.com/privacy-policy",
        provider_headquarters_country=PROVIDER_JURISDICTION_US,
    ),
    "chutes": Provider(
        slug="chutes",
        name="Chutes",
        supports_prepaid=True,
        stores_content=False,
        provider_zero_data_retention=True,
        provider_confidential_compute=True,
        provider_e2ee=True,
        provider_policy=(
            "TrustedRouter encrypts each request to an attested Chutes workload "
            "and verifies Intel TDX plus NVIDIA GPU attestation inside the "
            "TrustedRouter enclave before sending content. Verification fails "
            "closed. Chutes also documents no prompt/output storage or training."
        ),
        provider_policy_url="https://chutes.ai/docs/core-concepts/security-architecture",
        provider_headquarters_country=PROVIDER_JURISDICTION_US,
    ),
    "digitalocean": Provider(
        slug="digitalocean",
        name="DigitalOcean Gradient AI",
        supports_prepaid=True,
        provider_policy=(
            "No provider-ZDR claim is tracked here. DigitalOcean's Gradient AI "
            "model and pricing documentation is linked for data-handling review."
        ),
        provider_policy_url="https://docs.digitalocean.com/products/inference/",
        provider_headquarters_country=PROVIDER_JURISDICTION_US,
    ),
    "cloudflare-workers-ai": Provider(
        slug="cloudflare-workers-ai",
        name="Cloudflare Workers AI",
        supports_prepaid=True,
        supports_byok=False,
        provider_policy=(
            "No provider-ZDR claim is tracked here. Cloudflare's Workers AI "
            "documentation is linked for model and data-handling review."
        ),
        provider_policy_url="https://developers.cloudflare.com/workers-ai/",
        provider_headquarters_country=PROVIDER_JURISDICTION_US,
    ),
    "inceptron": Provider(
        slug="inceptron",
        name="Inceptron",
        supports_prepaid=True,
        supports_byok=False,
        stores_content=False,
        provider_zero_data_retention=True,
        provider_policy=(
            "Inceptron documents zero retention by default: API prompts and "
            "outputs are processed transiently and discarded after processing."
        ),
        provider_policy_url="https://www.inceptron.io/privacy",
        provider_headquarters_country=PROVIDER_JURISDICTION_SE,
    ),
    "morph": Provider(
        slug="morph",
        name="Morph",
        supports_prepaid=True,
        supports_byok=False,
        provider_policy=(
            "Morph's public paid tier documents up to 30 days of content "
            "retention. Zero retention is reserved for enterprise contracts."
        ),
        provider_policy_url="https://www.morphllm.com/privacy",
        provider_headquarters_country=PROVIDER_JURISDICTION_US,
    ),
    "atlas-cloud": Provider(
        slug="atlas-cloud",
        name="Atlas Cloud",
        supports_prepaid=True,
        supports_byok=False,
        provider_policy=(
            "Atlas Cloud publishes a platform zero-retention policy, while its "
            "aggregated model routes can involve downstream providers. "
            "TrustedRouter keeps the public route tier conservative pending "
            "downstream-path contractual verification."
        ),
        provider_policy_url="https://www.atlascloud.ai/zero-data-retention",
        provider_headquarters_country=PROVIDER_JURISDICTION_US,
    ),
    "streamlake": Provider(
        slug="streamlake",
        name="StreamLake",
        supports_prepaid=True,
        supports_byok=False,
        provider_policy=(
            "No provider-ZDR claim is tracked. StreamLake's public privacy "
            "policy is linked for API data-handling review."
        ),
        provider_policy_url=("https://www.streamlake.ai/document/DOC/mgkci47q13qr66h9i54"),
        provider_headquarters_country=PROVIDER_JURISDICTION_SG,
    ),
    "neurometric": Provider(
        slug="neurometric",
        name="Neurometric AI",
        supports_prepaid=True,
        supports_byok=False,
        stores_content=False,
        provider_zero_data_retention=False,
        provider_confidential_compute=False,
        provider_e2ee=False,
        provider_policy=(
            "Neurometric states that TrustedRouter requests run with upstream "
            "trace logging disabled: prompts and completions are not written "
            "to observability or object storage, and only aggregate request "
            "counts and token totals are retained. This is classified as "
            "no-store, not contractual ZDR or confidential compute."
        ),
        provider_policy_url="https://www.neurometric.ai/privacy",
        provider_headquarters_country=PROVIDER_JURISDICTION_US,
    ),
    "engy": Provider(
        slug="engy",
        name="Engy",
        supports_prepaid=True,
        supports_byok=False,
        stores_content=False,
        provider_zero_data_retention=True,
        provider_confidential_compute=False,
        provider_e2ee=False,
        provider_policy=(
            "Engy states that prompts, outputs, tool arguments, images, and "
            "embeddings are not stored or used for training. It retains "
            "request metadata such as model, token counts, cost, and latency. "
            "Engy's verified-inference sampling is not a user-verifiable TEE "
            "attestation, so this route is ZDR but not confidential or E2EE."
        ),
        provider_policy_url="https://engy.ai/privacy",
        # No country recorded: see PROVIDER_JURISDICTION_UNVERIFIED["engy"].
    ),
    "pearl": Provider(
        slug="pearl",
        name="Pearl Research Labs",
        supports_prepaid=True,
        supports_byok=False,
        provider_zero_data_retention=False,
        provider_confidential_compute=False,
        provider_e2ee=False,
        provider_policy=(
            "Pearl Research states that it retains operational metadata for "
            "security and does not train on customer data. No public "
            "contractual zero-data-retention terms are linked, so "
            "TrustedRouter classifies these routes as Standard and excludes "
            "them from ZDR, confidential-compute, and E2EE routing."
        ),
        provider_policy_url="https://pearlresearch.ai/legal/privacy",
        provider_headquarters_country=PROVIDER_JURISDICTION_IL,
    ),
    "stepfun": Provider(
        slug="stepfun",
        name="StepFun",
        supports_prepaid=True,
        supports_byok=False,
        provider_zero_data_retention=False,
        provider_confidential_compute=False,
        provider_e2ee=False,
        provider_policy=(
            "StepFun's public privacy policy does not provide contractual "
            "zero retention or confidential-compute guarantees for API "
            "requests. TrustedRouter therefore classifies this route as "
            "Standard."
        ),
        provider_policy_url="https://platform.stepfun.com/legal/privacy-policy.html",
        provider_headquarters_country=PROVIDER_JURISDICTION_CN,
    ),
    "relace": Provider(
        slug="relace",
        name="Relace",
        supports_prepaid=True,
        supports_byok=False,
        provider_zero_data_retention=False,
        provider_confidential_compute=False,
        provider_e2ee=False,
        provider_policy=(
            "Relace's public API documentation does not publish contractual "
            "zero retention, confidential compute, or end-to-end encryption "
            "for hosted open-model requests. This route is Standard."
        ),
        provider_policy_url="https://docs.relace.ai/api-reference/introduction",
    ),
    "recraft": Provider(
        slug="recraft",
        name="Recraft",
        supports_chat=False,
        supports_prepaid=True,
        supports_byok=False,
        provider_zero_data_retention=False,
        provider_confidential_compute=False,
        provider_e2ee=False,
        provider_policy=(
            "Recraft's developer terms say API inputs and outputs are not "
            "used to train its models, but do not promise zero retention or "
            "confidential compute. This route is Standard."
        ),
        provider_policy_url="https://www.recraft.ai/legal/terms",
    ),
    "bfl": Provider(
        slug="bfl",
        name="Black Forest Labs",
        supports_chat=False,
        supports_prepaid=True,
        supports_byok=False,
        provider_zero_data_retention=False,
        provider_confidential_compute=False,
        provider_e2ee=False,
        provider_policy=(
            "Black Forest Labs does not publish contractual zero retention, "
            "confidential compute, or end-to-end encryption for the hosted "
            "FLUX API. This route is Standard."
        ),
        provider_policy_url="https://bfl.ai/legal/developer-terms-of-service",
        # Black Forest Labs GmbH identifies Freiburg, Germany as its legal
        # address. https://bfl.ai/imprint
        provider_headquarters_country=PROVIDER_JURISDICTION_DE,
    ),
    "decart": Provider(
        slug="decart",
        name="Decart",
        supports_chat=False,
        supports_prepaid=True,
        supports_byok=False,
        provider_zero_data_retention=False,
        provider_confidential_compute=False,
        provider_e2ee=False,
        provider_policy=(
            "Decart's public terms permit service-related use of submitted "
            "content and do not promise zero retention or confidential "
            "compute. This route is Standard."
        ),
        provider_policy_url="https://decart.ai/terms",
        # Decart.AI, Inc. publishes a Wilmington, Delaware address in its
        # terms. https://decart.ai/terms
        provider_headquarters_country=PROVIDER_JURISDICTION_US,
    ),
    "nvidia-nim": Provider(
        slug="nvidia-nim",
        name="NVIDIA NIM",
        supports_chat=True,
        supports_prepaid=True,
        supports_byok=False,
        provider_zero_data_retention=False,
        provider_confidential_compute=False,
        provider_e2ee=False,
        provider_policy=(
            "NVIDIA's hosted NIM API Catalog endpoints are preview services for "
            "development and prototyping. TrustedRouter exposes chat-capable models "
            "from its configured API Catalog key as Standard routes. NVIDIA requires "
            "an NVIDIA AI Enterprise entitlement for production deployments. NVIDIA "
            "does not publish hosted preview token rates, so TrustedRouter uses a "
            "conservative fallback price and excludes these routes from price indexing."
        ),
        provider_policy_url="https://docs.api.nvidia.com/nim/docs/run-anywhere",
        provider_headquarters_country=PROVIDER_JURISDICTION_US,
    ),
    "wandb": Provider(
        slug="wandb",
        name="Weights & Biases",
        supports_prepaid=True,
        supports_byok=False,
        stores_content=True,
        provider_zero_data_retention=False,
        provider_confidential_compute=False,
        provider_e2ee=False,
        provider_policy=(
            "W&B Serverless Inference runs on CoreWeave infrastructure. W&B "
            "does not publish a zero-retention, confidential-compute, or "
            "end-to-end-encryption commitment for this service, so these "
            "routes use the Standard privacy tier. TrustedRouter does not "
            "enable W&B Weave tracing for provider calls."
        ),
        provider_policy_url="https://wandb.ai/site/inference/",
        # Weights and Biases, LLC is the US entity identified by W&B's terms.
        # https://wandb.ai/site/terms/
        provider_headquarters_country=PROVIDER_JURISDICTION_US,
    ),
    "nscale": Provider(
        slug="nscale",
        name="Nscale",
        supports_chat=True,
        supports_embeddings=True,
        supports_prepaid=True,
        supports_byok=False,
        stores_content=True,
        provider_zero_data_retention=False,
        provider_confidential_compute=False,
        provider_e2ee=False,
        provider_policy=(
            "Nscale publishes a general nonlogging statement for serverless "
            "inference, but TrustedRouter has not verified a contractual "
            "zero-retention commitment for this account. These routes remain "
            "Standard and make no confidential-compute or E2EE claim."
        ),
        provider_policy_url="https://docs.nscale.com/changelog/changelog",
        provider_headquarters_country=PROVIDER_JURISDICTION_GB,
    ),
    "databricks": Provider(
        slug="databricks",
        name="Databricks",
        supports_prepaid=True,
        supports_byok=False,
        stores_content=True,
        provider_zero_data_retention=False,
        provider_confidential_compute=False,
        provider_e2ee=False,
        provider_policy=(
            "Databricks Foundation Model APIs are a Designated Service and "
            "pay-per-token workloads are HIPAA compliant. Databricks may "
            "temporarily process or store inputs and outputs for abuse "
            "prevention, and may process data outside the originating cloud "
            "or region. This route is therefore standard privacy, not ZDR, "
            "confidential compute, or end-to-end encrypted."
        ),
        provider_policy_url=(
            "https://docs.databricks.com/aws/en/machine-learning/foundation-model-apis/compliance"
        ),
        provider_headquarters_country=PROVIDER_JURISDICTION_US,
    ),
    "zero-g": Provider(
        slug="zero-g",
        name="0G Private Computer",
        supports_prepaid=True,
        supports_byok=False,
        stores_content=False,
        provider_zero_data_retention=None,
        provider_confidential_compute=True,
        provider_e2ee=False,
        provider_policy=(
            "0G labels these routes private, TeeML, TEE-attested, and healthy. "
            "TrustedRouter requests that mode with X-0G-Provider-Trust-Mode: "
            "private and excludes standard and TeeTLS routes. The current live "
            "0G router path does not expose the route quote, audited code "
            "measurement, or response authentication needed for TrustedRouter "
            "to verify and encrypt each request end to end, so 0G is not in the "
            "trustedrouter/e2e pool."
        ),
        provider_policy_url="https://pc.0g.ai/models",
        # Zero Gravity Labs, Inc., a Delaware corporation, is named as data
        # controller in the privacy policy for the 0g.ai domain that hosts the
        # routed endpoint (router-api.0g.ai), and that policy applies Delaware
        # law. https://0g.ai/privacy-policy
        # Jurisdiction nuance: the separate 0G Foundation is a Cayman Islands
        # foundation company whose own policy covers 0gfoundation.ai, not this
        # endpoint. https://www.0gfoundation.ai/privacy-policy
        provider_headquarters_country=PROVIDER_JURISDICTION_US,
    ),
    # DeepInfra — large open-weight catalog (Llama, Gemma 4, Qwen,
    # DeepSeek, etc.). OpenAI-compatible at api.deepinfra.com/v1/openai.
    # Pricing in the /v1/openai/models response under
    # metadata.pricing.{input_tokens,output_tokens} as USD per million.
    "deepinfra": Provider(
        slug="deepinfra",
        name="DeepInfra",
        supports_prepaid=True,
        stores_content=False,
        provider_zero_data_retention=False,
        provider_policy=(
            "Tracked as no-store, not strict ZDR. DeepInfra documents memory-only "
            "handling and no training for ordinary inference, but reserves the "
            "right to log a small portion of requests for debugging or security. "
            "Google- and Anthropic-backed routes also inherit those vendors' terms."
        ),
        provider_policy_url="https://docs.deepinfra.com/account/data-privacy",
        # DEEP INFRA, INC., a Delaware corporation at 2625 Middlefield Road
        # #460, Palo Alto, CA 94306, per its own terms, which apply Delaware law.
        # https://deepinfra.com/terms
        provider_headquarters_country=PROVIDER_JURISDICTION_US,
    ),
    # Nebius Token Factory — OpenAI-compatible shared inference for
    # open-weight models. The /v1/models feed publishes exact upstream
    # model IDs with mixed-case authors, so TR carries a provider-native
    # supplement and passes upstream_id through unchanged.
    "nebius": Provider(
        slug="nebius",
        name="Nebius Token Factory",
        supports_prepaid=True,
        stores_content=False,
        provider_zero_data_retention=True,
        provider_policy=(
            "Marked ZDR via TrustedRouter's arrangement — Nebius RETAINS inputs/"
            "outputs by default (for speculative decoding); zero retention is an "
            "opt-in control, which the deployed Nebius account has enabled. Nebius "
            "does not train on customer data."
        ),
        provider_policy_url="https://docs.studio.nebius.com/legal/legal-quick-guide",
        # Nebius's own legal guide for Token Factory, the service whose
        # api.tokenfactory.nebius.com endpoint is routed to here, says the
        # service is supplied by Nebius B.V., a company incorporated in the
        # Netherlands and a subsidiary of Nebius Group N.V. (NASDAQ: NBIS).
        # https://docs.tokenfactory.nebius.com/legal/legal-quick-guide
        provider_headquarters_country=PROVIDER_JURISDICTION_NL,
    ),
    # MiniMax first-party API. OpenAI-compatible at api.minimax.io/v1;
    # public TR IDs use the OpenRouter-style minimax/<slug> form while
    # endpoint.upstream_id preserves MiniMax's exact mixed-case ID.
    "minimax": Provider(
        slug="minimax",
        name="MiniMax",
        supports_prepaid=True,
        provider_policy=(
            "No provider-ZDR claim is tracked here. MiniMax's product privacy overview "
            "is linked for users who need to review API/open-platform terms."
        ),
        provider_policy_url="https://www.minimax.io/privacy-policy-v2.html",
        # MiniMax Group Inc. (稀宇科技) is based in Shanghai, China and listed in
        # Hong Kong (SEHK: 100). https://en.wikipedia.org/wiki/MiniMax_Group
        # Nanonoble Pte. Ltd., 152 Beach Road, #14-02 Gateway East, Singapore
        # 189721 -- the Service Provider named by the MiniMax Open Platform
        # terms governing the routed endpoint api.minimax.io, under Singapore
        # law with SIAC arbitration. Verified 2026-08-17. An earlier revision
        # recorded CN and claimed the reachable legal pages named no non-China
        # operator; they do -- the terms render only under JavaScript, so a
        # plain fetch missed them. Same fact pattern as Z.AI, recorded the same
        # way. The lab is Chinese and is recorded as such in the origin map.
        # https://platform.minimax.io/protocol/terms-of-service
        provider_headquarters_country=PROVIDER_JURISDICTION_SG,
    ),
    # Thinking Machines Lab Tinker sampler. The 256K Inkling endpoint is
    # provider-native and OpenAI-compatible. Keep its privacy posture
    # conservative until account-specific retention terms are documented.
    "thinkingmachines": Provider(
        slug="thinkingmachines",
        name="Thinking Machines Lab",
        supports_prepaid=True,
        provider_policy=(
            "No provider-ZDR claim is tracked here. Thinking Machines Lab's "
            "model and pricing documentation is linked for model capability "
            "and API review."
        ),
        provider_policy_url="https://tinker-docs.thinkingmachines.ai/tinker/models/",
        provider_headquarters_country=PROVIDER_JURISDICTION_US,
    ),
    # Xiaomi MiMo — OpenAI-compatible chat (api.xiaomimimo.com/v1). MiMo-V2 /
    # V2.5 agent models. Models + prices are in data/provider_models/xiaomi.json.
    "xiaomi": Provider(
        slug="xiaomi",
        name="Xiaomi MiMo",
        supports_prepaid=True,
        provider_policy=(
            "No provider-ZDR claim is tracked here. Xiaomi MiMo's open-platform "
            "terms are linked for users who need to review API data handling."
        ),
        provider_policy_url="https://platform.xiaomimimo.com/",
        # Xiaomi Corporation's 2025 annual report gives its head office and
        # principal place of business as Xiaomi Campus, Anningzhuang Road,
        # Haidian District, Beijing, PRC, with a registered office in the Cayman
        # Islands and a Hong Kong place of business; it is listed on HKEX (1810).
        # https://ir.mi.com/system/files-encrypted/nasdaq_kms/assets/2026/04/28/5-29-08/Xiaomi%202025%20AR_EN.pdf
        # Jurisdiction nuance: the MiMo platform terms and privacy pages under
        # mimo.mi.com render only under JavaScript and name Xiaomi without a
        # contracting entity, so the group's operating home is what is recorded.
        provider_headquarters_country=PROVIDER_JURISDICTION_CN,
    ),
    # Alibaba Cloud Model Studio / DashScope — workspace-scoped OpenAI-compatible
    # endpoint. The configured key is for an EU Central / Frankfurt MAAS
    # workspace, so provider-native model availability comes from that
    # workspace's /compatible-mode/v1/models response.
    "alibaba": Provider(
        slug="alibaba",
        name="Alibaba Cloud Model Studio",
        supports_prepaid=True,
        supports_byok=False,
        provider_policy=(
            "No provider-ZDR claim is tracked here. Alibaba Cloud Model Studio "
            "model availability and pricing are linked for users who need to "
            "review API data handling and regional deployment scope."
        ),
        provider_policy_url="https://www.alibabacloud.com/help/en/model-studio/model-pricing",
        # Alibaba Group Holding Limited gives its principal executive offices as
        # 969 West Wen Yi Road, Yuhang District, Hangzhou, China.
        # https://www.alibabagroup.com/en-US/faqs-corporate-information
        # Jurisdiction nuance: Alibaba Cloud's membership agreement picks the
        # contracting entity from the customer's billing address — Alibaba Cloud
        # (Singapore) Private Limited, Alibaba (Netherlands) B.V. for the EEA,
        # Alibaba Cloud US LLC, and others — so the entity billing TrustedRouter
        # may not be the Chinese parent, and the configured workspace runs in the
        # eu-central-1 Frankfurt region. The membership agreement alone cannot
        # settle it -- it selects the contracting entity by billing address and
        # names only non-China entities -- so this is recorded from
        # TrustedRouter's own account relationship with the provider (operator
        # knowledge, 2026-08-17), which is the invoice-level fact the agreement
        # points at. The lab (Qwen) is Chinese and is recorded separately in the
        # origin map.
        # https://www.alibabacloud.com/help/en/legal/latest/alibaba-cloud-international-website-membership-agreement
        provider_headquarters_country=PROVIDER_JURISDICTION_CN,
    ),
    # Microsoft Foundry direct deployments in eastus2. Availability and exact
    # regional list prices are synchronized from Azure's account-scoped model,
    # quota, deployment, and Retail Prices APIs. Do not infer ZDR from Azure's
    # enterprise positioning: this route stays Standard until Lore Hex has an
    # applicable written retention agreement and verifies account enforcement.
    "azure": Provider(
        slug="azure",
        name="Microsoft Azure AI Foundry",
        supports_messages=True,
        supports_prepaid=True,
        supports_byok=False,
        provider_zero_data_retention=False,
        provider_confidential_compute=False,
        provider_e2ee=False,
        provider_policy=(
            "No provider-ZDR, confidential-compute, or E2EE claim is currently "
            "tracked for TrustedRouter's Microsoft Foundry account. Azure model "
            "availability and pricing are synchronized directly from the account."
        ),
        provider_policy_url=(
            "https://learn.microsoft.com/en-us/azure/ai-foundry/responsible-ai/openai/data-privacy"
        ),
        provider_headquarters_country=PROVIDER_JURISDICTION_US,
    ),
    "ltx": Provider(
        slug="ltx",
        name="Lightricks LTX",
        supports_chat=False,
        supports_prepaid=True,
        supports_byok=False,
        provider_policy=(
            "No provider-ZDR, confidential-compute, or E2EE claim is tracked for "
            "the LTX video API. TrustedRouter relays and does not durably store "
            "prompt or generated video content."
        ),
        provider_policy_url="https://ltx.io/terms-of-use",
        # Lightricks Ltd., Yesha'yahu Leibowitz 30, Jerusalem, Israel, is the
        # named data controller in the LTX Platform privacy policy, whose data
        # protection officer answers at dpo@ltx.io — the ltx.io service routed to
        # here. https://static.lightricks.com/legal/Privacy%20Policy%20-%20LTX%20Platform.pdf
        # Jurisdiction nuance: the LTX Studio terms contract through Lightricks
        # US Inc. (Chicago, IL) when the customer is incorporated in a US state,
        # so the billing counterparty can be American while the operator and
        # controller stay Israeli.
        # https://static.lightricks.com/legal/LTXS-Terms%20of%20Service%20Online.pdf
        provider_headquarters_country=PROVIDER_JURISDICTION_IL,
    ),
    "runway": Provider(
        slug="runway",
        name="Runway",
        supports_chat=False,
        supports_prepaid=True,
        supports_byok=False,
        provider_policy=(
            "No provider-ZDR, confidential-compute, or E2EE claim is tracked for "
            "the Runway video API. TrustedRouter relays and does not durably store "
            "prompt or generated video content."
        ),
        provider_policy_url="https://runwayml.com/privacy-policy",
        provider_headquarters_country=PROVIDER_JURISDICTION_US,
    ),
    "kling": Provider(
        slug="kling",
        name="Kling AI",
        supports_chat=False,
        supports_prepaid=True,
        supports_byok=False,
        provider_policy=(
            "No provider-ZDR, confidential-compute, or E2EE claim is tracked for "
            "the Kling video API. TrustedRouter relays and does not durably store "
            "prompt or generated video content."
        ),
        provider_policy_url="https://kling.ai/privacy-policy",
        # China. Kling AI is Kuaishou Technology's product (Kuaishou head
        # office: Haidian District, Beijing). Its policy pages answer HTTP 446
        # to our fetches, so this is recorded from TrustedRouter's own account
        # relationship with the provider (operator knowledge, 2026-08-17)
        # rather than from a published document.
        provider_headquarters_country=PROVIDER_JURISDICTION_CN,
    ),
    "upstage": Provider(
        slug="upstage",
        name="Upstage",
        supports_prepaid=True,
        supports_byok=False,
        provider_policy=(
            "No contractual zero-retention, confidential-compute, or E2EE claim "
            "is tracked for TrustedRouter's Upstage account. This route is Standard."
        ),
        provider_policy_url="https://console.upstage.ai/docs/getting-started/models",
        provider_headquarters_country=PROVIDER_JURISDICTION_KR,
    ),
    "sail-research": Provider(
        slug="sail-research",
        name="Sail Research",
        supports_prepaid=True,
        supports_byok=False,
        provider_policy=(
            "No contractual zero-retention, confidential-compute, or E2EE claim "
            "is tracked for TrustedRouter's Sail Research account. This route is "
            "Standard."
        ),
        provider_policy_url="https://docs.sailresearch.com/",
    ),
    "perplexity": Provider(
        slug="perplexity",
        name="Perplexity",
        supports_prepaid=True,
        supports_byok=False,
        provider_policy=(
            "No contractual zero-retention, confidential-compute, or E2EE claim "
            "is tracked for TrustedRouter's Perplexity account. Sonar runs with "
            "low search context and its first-party token and fixed successful-request "
            "charges settle exactly. This route is Standard."
        ),
        provider_policy_url="https://docs.perplexity.ai/",
    ),
    "reka": Provider(
        slug="reka",
        name="Reka AI",
        supports_prepaid=True,
        supports_byok=False,
        provider_policy=(
            "No contractual zero-retention, confidential-compute, or E2EE claim "
            "is tracked for TrustedRouter's Reka account. This route is Standard."
        ),
        provider_policy_url="https://docs.reka.ai/",
    ),
    "nextbit": Provider(
        slug="nextbit",
        name="NextBit",
        supports_prepaid=True,
        supports_byok=False,
        provider_policy=(
            "No contractual zero-retention, confidential-compute, or E2EE claim "
            "is tracked for TrustedRouter's NextBit account. This route is Standard."
        ),
        provider_policy_url="https://nextbit256.com/",
    ),
    "akashml": Provider(
        slug="akashml",
        name="AkashML",
        supports_prepaid=True,
        supports_byok=False,
        provider_policy=(
            "No contractual zero-retention, confidential-compute, or E2EE claim "
            "is tracked for TrustedRouter's AkashML account. This route is Standard."
        ),
        provider_policy_url="https://akashml.com/",
    ),
    "mancer": Provider(
        slug="mancer",
        name="Mancer",
        supports_prepaid=True,
        supports_byok=False,
        provider_policy=(
            "No contractual zero-retention, confidential-compute, or E2EE claim "
            "is tracked for TrustedRouter's Mancer account. This route is Standard."
        ),
        provider_policy_url="https://mancer.tech/docs-api/",
    ),
    "aion-labs": Provider(
        slug="aion-labs",
        name="Aion Labs",
        supports_prepaid=True,
        supports_byok=False,
        provider_policy=(
            "No contractual zero-retention, confidential-compute, or E2EE claim "
            "is tracked for TrustedRouter's Aion Labs account. This route is Standard."
        ),
        provider_policy_url="https://aionlabs.ai/",
    ),
    "sambanova": Provider(
        slug="sambanova",
        name="SambaNova",
        supports_prepaid=True,
        supports_byok=False,
        provider_policy=(
            "No contractual zero-retention, confidential-compute, or E2EE claim "
            "is tracked for TrustedRouter's SambaNova account. This route is Standard."
        ),
        provider_policy_url="https://docs.sambanova.ai/",
    ),
    "arcee": Provider(
        slug="arcee",
        name="Arcee AI",
        supports_prepaid=True,
        supports_byok=False,
        provider_policy=(
            "No contractual zero-retention, confidential-compute, or E2EE claim "
            "is tracked for TrustedRouter's Arcee account. This route is Standard."
        ),
        provider_policy_url="https://docs.arcee.ai/",
    ),
    "perceptron": Provider(
        slug="perceptron",
        name="Perceptron",
        supports_prepaid=False,
        supports_byok=False,
        provider_policy=(
            "No contractual zero-retention, confidential-compute, or E2EE claim "
            "is tracked for TrustedRouter's Perceptron account. Its public catalog "
            "is priced, but the configured inference credential did not authenticate "
            "during the live canary, so routes remain dark."
        ),
        provider_policy_url="https://perceptron.cloud/docs/inference",
    ),
    "inception": Provider(
        slug="inception",
        name="Inception Labs",
        supports_prepaid=True,
        supports_byok=False,
        provider_policy=(
            "No contractual zero-retention, confidential-compute, or E2EE claim "
            "is tracked for TrustedRouter's Inception account. This route is Standard."
        ),
        provider_policy_url="https://docs.inceptionlabs.ai/",
    ),
    "sakana": Provider(
        slug="sakana",
        name="Sakana AI",
        supports_prepaid=True,
        supports_byok=False,
        provider_policy=(
            "No contractual zero-retention, confidential-compute, or E2EE claim "
            "is tracked for TrustedRouter's Sakana account. Version-pinned routes "
            "use Sakana's first-party token prices and authenticated availability."
        ),
        provider_policy_url="https://console.sakana.ai/privacy-policy",
    ),
    # These providers are recorded so their public provider pages and compliance
    # posture are explicit, but they are not admitted to the chat gateway. Their
    # credentials either address an asynchronous media/deployment API or their
    # account does not expose exact billable token prices yet.
    "krea": Provider(
        slug="krea",
        name="Krea",
        supports_chat=False,
        supports_prepaid=True,
        supports_byok=False,
        provider_policy=(
            "Krea exposes an asynchronous media API, not a shared OpenAI-compatible "
            "chat catalog. TrustedRouter supports Krea 2 Medium with exact fixed "
            "per-image billing. The route remains dark until its paid generation "
            "canary succeeds. No ZDR or confidential-compute claim is tracked."
        ),
        provider_policy_url="https://docs.krea.ai/",
    ),
    "modal": Provider(
        slug="modal",
        name="Modal",
        supports_chat=False,
        supports_prepaid=False,
        supports_byok=False,
        provider_policy=(
            "The configured Modal credentials deploy workloads; they do not identify "
            "a shared model catalog. Modal remains non-routable until explicit "
            "deployment endpoints and prices are configured."
        ),
        provider_policy_url="https://modal.com/docs",
    ),
    "byteplus": Provider(
        slug="byteplus",
        name="BytePlus ModelArk",
        supports_prepaid=False,
        supports_byok=False,
        provider_policy=(
            "BytePlus ModelArk requires region-specific activated model endpoint IDs. "
            "The API key alone is insufficient to create safe routes, so this "
            "provider remains non-routable pending endpoint configuration."
        ),
        provider_policy_url="https://docs.byteplus.com/en/docs/ModelArk",
    ),
    "riverflow": Provider(
        slug="riverflow",
        name="Riverflow",
        supports_chat=False,
        supports_prepaid=False,
        supports_byok=False,
        provider_policy=(
            "Riverflow is a variable-credit media workflow API, not a shared "
            "token-priced chat endpoint. Its configured credential is rejected by "
            "the live MCP endpoint, and no exact per-job billable receipt is exposed. "
            "It remains fail-closed until both contracts are available."
        ),
        provider_policy_url=("https://www.riverflow.ai/research/introducing-sourceful-riverflow-1"),
    ),
    "io-net": Provider(
        slug="io-net",
        name="IO Intelligence",
        supports_prepaid=True,
        supports_byok=False,
        provider_policy=(
            "IO Intelligence publishes exact per-token prices in its authenticated "
            "catalog. TrustedRouter admits only priced routes that pass a live chat "
            "canary. No contractual ZDR, confidential-compute, or E2EE claim is "
            "tracked for this account, so these routes are Standard."
        ),
        provider_policy_url="https://docs.io.net/docs/io-intelligence",
    ),
    "scaleway": Provider(
        slug="scaleway",
        name="Scaleway",
        supports_prepaid=True,
        supports_byok=False,
        provider_policy=(
            "Scaleway routes are discovered from its authenticated Model as a "
            "Service catalog and priced from Scaleway's first-party EUR price "
            "feed with a bounded FX reserve. No contractual ZDR, confidential-"
            "compute, or E2EE claim is tracked for this account, so these routes "
            "are Standard."
        ),
        provider_policy_url="https://www.scaleway.com/en/pricing/model-as-a-service/",
        provider_headquarters_country=PROVIDER_JURISDICTION_FR,
    ),
    "featherless": Provider(
        slug="featherless",
        name="Featherless AI",
        supports_prepaid=True,
        supports_byok=False,
        provider_policy=(
            "Featherless routes are limited to chat models explicitly available "
            "on TrustedRouter's current plan with exact prices in Featherless's "
            "authenticated catalog. No contractual ZDR, confidential-compute, or "
            "E2EE claim is tracked for this account, so these routes are Standard."
        ),
        provider_policy_url="https://docs.featherless.ai/",
        # Featherless's terms identify the API operator as a Delaware LLC.
        # https://featherless.ai/legal/terms-of-service
        provider_headquarters_country=PROVIDER_JURISDICTION_US,
    ),
    "jina": Provider(
        slug="jina",
        name="Jina AI",
        supports_chat=False,
        supports_embeddings=True,
        supports_prepaid=True,
        supports_byok=False,
        provider_policy=(
            "Jina routes are embedding-only and use exact prices from Jina's "
            "authenticated model catalog. No contractual ZDR, confidential-compute, "
            "or E2EE claim is tracked for this account, so these routes are Standard."
        ),
        provider_policy_url="https://jina.ai/embeddings/",
        provider_headquarters_country=PROVIDER_JURISDICTION_DE,
    ),
    "ovhcloud": Provider(
        slug="ovhcloud",
        name="OVHcloud",
        supports_prepaid=False,
        supports_byok=False,
        provider_policy=(
            "Not routable: the configured OVH credential is not an AI Endpoints "
            "access token and the live model catalog rejects it. The provider stays "
            "listed while a valid inference token is obtained."
        ),
        provider_policy_url="https://help.ovhcloud.com/csm/en-public-cloud-ai-endpoints-getting-started?id=kb_article_view&sysparm_article=KB0065407",
        provider_headquarters_country=PROVIDER_JURISDICTION_FR,
    ),
    "vultr": Provider(
        slug="vultr",
        name="Vultr",
        supports_prepaid=False,
        supports_byok=False,
        provider_policy=(
            "Not routable: the configured credential is rejected by both Vultr's "
            "account API and Serverless Inference API. Vultr issues a distinct "
            "inference key per subscription; the provider stays listed until that "
            "key is available."
        ),
        provider_policy_url="https://docs.vultr.com/products/serverless-inference/",
        provider_headquarters_country=PROVIDER_JURISDICTION_US,
    ),
    "liquid": Provider(
        slug="liquid",
        name="Liquid AI",
        supports_chat=False,
        supports_prepaid=False,
        supports_byok=False,
        provider_policy=(
            "Liquid's configured developer credentials target model deployment and "
            "bundling rather than a shared token-priced inference API. The provider "
            "remains non-routable until a concrete hosted endpoint is configured."
        ),
        provider_policy_url="https://docs.liquid.ai/",
    ),
    # Cohere — first-party embeddings (embed-v4.0, embed-*-v3.0) plus
    # Command chat models. Embeddings are Cohere's flagship retrieval
    # product; chat is registered but TR currently only catalogs Cohere
    # embedding models. NOT OpenAI-shaped: the enclave talks to Cohere's
    # native POST /v2/embed ({model, texts, input_type, embedding_types})
    # and adapts the response to the OpenAI embeddings envelope.
    "cohere": Provider(
        slug="cohere",
        name="Cohere",
        supports_embeddings=True,
        supports_prepaid=True,
        stores_content=False,
        provider_zero_data_retention=True,
        provider_policy=(
            "Marked ZDR — Cohere does not retain prompt/response content for "
            "TrustedRouter's configured account and does not train on customer "
            "API data. (Not a confidential-compute/TEE provider.)"
        ),
        provider_policy_url="https://cohere.com/security",
        # Cohere Inc., 171 John Street, Suite 200, Toronto, ON Canada M5T 1X3,
        # described in its own privacy policy as a Canadian company subject to
        # Canadian federal privacy laws; its terms of use apply Ontario law and
        # place disputes in Toronto courts. https://cohere.com/privacy
        provider_headquarters_country=PROVIDER_JURISDICTION_CA,
    ),
    # Voyage AI — first-party retrieval embeddings (voyage-3-large etc.).
    # OpenAI-shaped: the enclave talks to api.voyageai.com/v1/embeddings with
    # {model, input} and Bearer auth, so the existing OpenAI-compatible
    # embeddings adapter dispatches it by base-URL + key swap.
    "voyage": Provider(
        slug="voyage",
        name="Voyage AI",
        supports_embeddings=True,
        supports_prepaid=True,
        stores_content=False,
        provider_zero_data_retention=True,
        provider_policy=(
            "Marked ZDR — Voyage AI does not retain prompt content for "
            "TrustedRouter's configured account and does not train on customer "
            "API data. (Not a confidential-compute/TEE provider.)"
        ),
        provider_policy_url="https://www.voyageai.com/privacy",
        provider_headquarters_country=PROVIDER_JURISDICTION_US,
    ),
}

GATEWAY_PREPAID_PROVIDER_SLUGS = frozenset(
    {
        "anthropic",
        "openai",
        "google-ai-studio",
        "google-vertex",
        "cerebras",
        "deepseek",
        "mistral",
        "kimi",
        "zai",
        "together",
        "fireworks",
        # New providers — all OpenAI-compatible chat completions, so
        # the existing enclave OpenAI-shape adapter can dispatch them
        # by switching base URL + auth header.
        "grok",
        "novita",
        # 2026-05-13: Phala re-enabled with the CORRECT confidential-
        # AI key. The 2026-05-12 attempt failed because we were
        # routing via the "redpill" upstream pass-through tier
        # (key 401s on chat completions even though /v1/models 200s)
        # — that key works for catalog browsing but isn't entitled
        # to chat. The fix: cloud.phala.com dashboard issues a
        # separate key for the GPU-TEE-attested confidential-AI
        # tier, stored as PHALA_CONFIDENTIAL_API_KEY → Secret
        # Manager `trustedrouter-phala-confidential-api-key`. The
        # enclave's QUILL_PHALA_SECRET default points at the confidential
        # secret; model ids ship as
        # `phala/<bare>` (per docs.phala.com/phala-cloud/confidential-ai)
        # via phalaModelMap in byok.go. Verified working live with
        # phala/gpt-oss-120b and phala/deepseek-v3.2 returning 200.
        "phala",
        "siliconflow",
        "tinfoil",
        "near-ai",
        "venice",
        # 2026-05-11 batch (all OpenAI-compatible chat completions).
        # All three host google/gemma-4 family which gives TR three
        # independent prepaid routes for the same open-weight model
        # — useful for both price arbitrage in the auto-router and
        # availability isolation when one provider is degraded.
        "parasail",
        "lightning",
        "gmi",
        "deepinfra",
        "friendli",
        "baseten",
        "telnyx",
        "thinkingmachines",
        "wafer",
        "crusoe",
        "makora",
        "chutes",
        "digitalocean",
        "cloudflare-workers-ai",
        "inceptron",
        "morph",
        "atlas-cloud",
        "streamlake",
        "neurometric",
        "engy",
        "pearl",
        "stepfun",
        "relace",
        "recraft",
        "bfl",
        "decart",
        "wandb",
        "nscale",
        "databricks",
        "zero-g",
        "upstage",
        "sail-research",
        "reka",
        "nextbit",
        "akashml",
        "mancer",
        "aion-labs",
        "sambanova",
        "arcee",
        "inception",
        "io-net",
        "scaleway",
        "featherless",
        "sakana",
        "perplexity",
        "krea",
        "nvidia-nim",
        "jina",
        "nebius",
        "minimax",
        # Alibaba Cloud Model Studio — Frankfurt workspace. The production
        # key is entitled to inference and its provider-native catalog is
        # refreshed from the workspace-scoped /models endpoint.
        "alibaba",
        "azure",
        # Direct asynchronous video APIs. These are handled by provider-native
        # adapters inside the attested gateway, not by the chat adapter.
        "ltx",
        "runway",
        "kling",
        # Cohere — embeddings only for now (native /v2/embed in the enclave).
        "cohere",
        # Voyage — embeddings only (OpenAI-shaped /v1/embeddings in the enclave).
        "voyage",
        # Xiaomi MiMo — OpenAI-compatible chat (api.xiaomimimo.com/v1).
        "xiaomi",
        # Meta-hosted Muse is currently exposed through OpenRouter's standard
        # inference API. The public provider label says "Meta via OpenRouter"
        # and the privacy posture remains standard/non-ZDR.
        "meta",
        # OpenRouter as a transport in its own right: prepaid credits only,
        # no provider-direct key. Models are added as routes are enabled.
        "openrouter",
    }
)

AUTO_MODEL_ID = "trustedrouter/auto"

FREE_MODEL_ID = "trustedrouter/free"

CHEAP_MODEL_ID = "trustedrouter/cheap"

FAST_MODEL_ID = "trustedrouter/fast"

EU_MODEL_ID = "trustedrouter/eu"

ZDR_MODEL_ID = "trustedrouter/zdr"

E2E_MODEL_ID = "trustedrouter/e2e"

CONFIDENTIAL_MODEL_ID = "trustedrouter/confidential"

# Upstream privacy floors enforced by routing aliases. Keep this as the single
# source of truth for authorization, public catalog copy, and recommendation
# surfaces. General-purpose aliases such as auto and cheap intentionally have
# no implicit privacy floor; callers can add provider.min_privacy explicitly.
ROUTING_MODEL_MIN_PRIVACY_TIERS: dict[str, int] = {
    ZDR_MODEL_ID: PRIVACY_TIER_ZERO_RETENTION,
    E2E_MODEL_ID: PRIVACY_TIER_CONFIDENTIAL,
}

MONITOR_MODEL_ID = "trustedrouter/monitor"

DEEPSEEK_V4_PRO_0423_MODEL_ID = "deepseek/deepseek-v4-pro-0423"

DEEPSEEK_V4_PRO_0813_MODEL_ID = "deepseek/deepseek-v4-pro-0813"

SOCRATES_1_0_MODEL_ID = "trustedrouter/socrates-1.0"

SOCRATES_1_1_MODEL_ID = "trustedrouter/socrates-1.1"

SOCRATES_2_0_MODEL_ID = "trustedrouter/socrates-2.0"

SOCRATES_MODEL_ID = "trustedrouter/socrates"

ADVISOR_MODEL_ID = "trustedrouter/advisor"

SUBAGENT_MODEL_ID = "trustedrouter/subagent"

ARISTOTLE_1_0_MODEL_ID = "trustedrouter/aristotle-1.0"

ARISTOTLE_1_1_MODEL_ID = "trustedrouter/aristotle-1.1"

ARISTOTLE_2_0_MODEL_ID = "trustedrouter/aristotle-2.0"

ARISTOTLE_MODEL_ID = "trustedrouter/aristotle"

PLATO_1_0_MODEL_ID = "trustedrouter/plato-1.0"

PLATO_3_0_MODEL_ID = "trustedrouter/plato-3.0"

PLATO_MODEL_ID = "trustedrouter/plato"

PLATO_PRO_1_0_MODEL_ID = "trustedrouter/plato-pro-1.0"

PLATO_PRO_2_0_MODEL_ID = "trustedrouter/plato-pro-2.0"

PLATO_PRO_MODEL_ID = "trustedrouter/plato-pro"

SOCRATES_PRO_1_0_MODEL_ID = "trustedrouter/socrates-pro-1.0"

SOCRATES_PRO_MODEL_ID = "trustedrouter/socrates-pro"

SOCRATES_PRO_PLUS_1_0_MODEL_ID = "trustedrouter/socrates-pro-plus-1.0"

SOCRATES_PRO_PLUS_MODEL_ID = "trustedrouter/socrates-pro-plus"

OPEN_PATCHER_S1_MODEL_ID = "trustedrouter/openpatcher-s1"

OPEN_PATCHER_S2_MODEL_ID = "trustedrouter/openpatcher-s2"

OPEN_PATCHER_S3_MODEL_ID = "trustedrouter/openpatcher-s3"

OPEN_PATCHER_A1_MODEL_ID = "trustedrouter/openpatcher-a1"

OPEN_PATCHER_FAST1_MODEL_ID = "trustedrouter/openpatcher-fast1"

OPEN_PATCHER_G1_MODEL_ID = "trustedrouter/openpatcher-g1"

OPEN_PATCHER_G2_MODEL_ID = "trustedrouter/openpatcher-g2"

OPEN_PATCHER_G3_MODEL_ID = "trustedrouter/openpatcher-g3"

ATHENA_MODEL_ID = "trustedrouter/athena"

ATHENA_1_0_MODEL_ID = "trustedrouter/athena-1.0"

ATHENA_2_0_MODEL_ID = "trustedrouter/athena-2.0"

LIBERTY_1_0_MODEL_ID = "trustedrouter/liberty-1.0"

LIBERTY_1_0_1M_MODEL_ID = "trustedrouter/liberty-1.0-1m"

LIBERTY_2_0_MODEL_ID = "trustedrouter/liberty-2.0"

PARASAIL_LIBERTY_2_0_MODEL_ID = "parasail/liberty-2.0"

LIBERTY_3_0_MODEL_ID = "trustedrouter/liberty-3.0"

US_PROVIDER_ONLY_MODEL_IDS = frozenset(
    {
        OPEN_PATCHER_S1_MODEL_ID,
        OPEN_PATCHER_A1_MODEL_ID,
        OPEN_PATCHER_FAST1_MODEL_ID,
        OPEN_PATCHER_G1_MODEL_ID,
        ATHENA_1_0_MODEL_ID,
        ATHENA_2_0_MODEL_ID,
        ATHENA_MODEL_ID,
    }
)

SYNTH_MODEL_ID = "trustedrouter/synth"

IRIS_MODEL_ID = "trustedrouter/iris"

PROMETHEUS_MODEL_ID = "trustedrouter/prometheus"

ZEUS_MODEL_ID = "trustedrouter/zeus"

IRIS_1_0_MODEL_ID = "trustedrouter/iris-1.0"

IRIS_2_0_MODEL_ID = "trustedrouter/iris-2.0"

IRIS_3_0_MODEL_ID = "trustedrouter/iris-3.0"

PROMETHEUS_1_0_MODEL_ID = "trustedrouter/prometheus-1.0"

PROMETHEUS_1_0_1M_MODEL_ID = "trustedrouter/prometheus-1.0-1m"

PROMETHEUS_2_0_MODEL_ID = "trustedrouter/prometheus-2.0"

PROMETHEUS_3_0_MODEL_ID = "trustedrouter/prometheus-3.0"

ZEUS_1_0_MODEL_ID = "trustedrouter/zeus-1.0"

ZEUS_1_0_MINI_MODEL_ID = "trustedrouter/zeus-1.0-mini"

ZEUS_2_0_MODEL_ID = "trustedrouter/zeus-2.0"

SYNTH_CODE_MODEL_ID = "trustedrouter/synth-code"

IRIS_CODE_MODEL_ID = "trustedrouter/iris-code"

PROMETHEUS_CODE_MODEL_ID = "trustedrouter/prometheus-code"

ZEUS_CODE_MODEL_ID = "trustedrouter/zeus-code"

IRIS_CODE_1_0_MODEL_ID = "trustedrouter/iris-code-1.0"

PROMETHEUS_CODE_1_0_MODEL_ID = "trustedrouter/prometheus-code-1.0"

ZEUS_CODE_1_0_MODEL_ID = "trustedrouter/zeus-code-1.0"

FUSION_MODEL_ID = "trustedrouter/fusion"

FUSION_CODE_MODEL_ID = "trustedrouter/fusion-code"

SELECTOR_MODEL_ID = "trustedrouter/selector"

MAPREDUCE_MODEL_ID = "trustedrouter/mapreduce"

META_MODEL_IDS = frozenset(
    {
        AUTO_MODEL_ID,
        FREE_MODEL_ID,
        CHEAP_MODEL_ID,
        FAST_MODEL_ID,
        EU_MODEL_ID,
        ZDR_MODEL_ID,
        E2E_MODEL_ID,
        CONFIDENTIAL_MODEL_ID,
        MONITOR_MODEL_ID,
        SOCRATES_1_0_MODEL_ID,
        SOCRATES_1_1_MODEL_ID,
        SOCRATES_2_0_MODEL_ID,
        SOCRATES_MODEL_ID,
        ADVISOR_MODEL_ID,
        SUBAGENT_MODEL_ID,
        ARISTOTLE_1_0_MODEL_ID,
        ARISTOTLE_1_1_MODEL_ID,
        ARISTOTLE_2_0_MODEL_ID,
        ARISTOTLE_MODEL_ID,
        PLATO_1_0_MODEL_ID,
        PLATO_3_0_MODEL_ID,
        PLATO_MODEL_ID,
        PLATO_PRO_1_0_MODEL_ID,
        PLATO_PRO_2_0_MODEL_ID,
        PLATO_PRO_MODEL_ID,
        SOCRATES_PRO_1_0_MODEL_ID,
        SOCRATES_PRO_MODEL_ID,
        SOCRATES_PRO_PLUS_1_0_MODEL_ID,
        SOCRATES_PRO_PLUS_MODEL_ID,
        OPEN_PATCHER_S1_MODEL_ID,
        OPEN_PATCHER_S2_MODEL_ID,
        OPEN_PATCHER_S3_MODEL_ID,
        OPEN_PATCHER_A1_MODEL_ID,
        OPEN_PATCHER_FAST1_MODEL_ID,
        OPEN_PATCHER_G1_MODEL_ID,
        OPEN_PATCHER_G2_MODEL_ID,
        OPEN_PATCHER_G3_MODEL_ID,
        ATHENA_1_0_MODEL_ID,
        ATHENA_2_0_MODEL_ID,
        ATHENA_MODEL_ID,
        LIBERTY_1_0_MODEL_ID,
        LIBERTY_1_0_1M_MODEL_ID,
        LIBERTY_2_0_MODEL_ID,
        PARASAIL_LIBERTY_2_0_MODEL_ID,
        LIBERTY_3_0_MODEL_ID,
        SYNTH_MODEL_ID,
        IRIS_MODEL_ID,
        PROMETHEUS_MODEL_ID,
        ZEUS_MODEL_ID,
        IRIS_1_0_MODEL_ID,
        IRIS_2_0_MODEL_ID,
        IRIS_3_0_MODEL_ID,
        PROMETHEUS_1_0_MODEL_ID,
        PROMETHEUS_1_0_1M_MODEL_ID,
        PROMETHEUS_2_0_MODEL_ID,
        PROMETHEUS_3_0_MODEL_ID,
        ZEUS_1_0_MODEL_ID,
        ZEUS_1_0_MINI_MODEL_ID,
        ZEUS_2_0_MODEL_ID,
        SYNTH_CODE_MODEL_ID,
        IRIS_CODE_MODEL_ID,
        PROMETHEUS_CODE_MODEL_ID,
        ZEUS_CODE_MODEL_ID,
        IRIS_CODE_1_0_MODEL_ID,
        PROMETHEUS_CODE_1_0_MODEL_ID,
        ZEUS_CODE_1_0_MODEL_ID,
        FUSION_MODEL_ID,
        FUSION_CODE_MODEL_ID,
        SELECTOR_MODEL_ID,
        MAPREDUCE_MODEL_ID,
    }
)

ORCHESTRATION_PRIMITIVE_NAMES = frozenset(
    {
        "advisor",
        "synth",
        "selector",
        "mapreduce",
        "subagent",
    }
)

ORCHESTRATION_PRIMITIVE_BY_MODEL_ID: dict[str, str] = {
    ADVISOR_MODEL_ID: "advisor",
    SUBAGENT_MODEL_ID: "subagent",
    SYNTH_MODEL_ID: "synth",
    SYNTH_CODE_MODEL_ID: "synth",
    FUSION_MODEL_ID: "synth",
    FUSION_CODE_MODEL_ID: "synth",
    SELECTOR_MODEL_ID: "selector",
    MAPREDUCE_MODEL_ID: "mapreduce",
}

CANONICAL_ORCHESTRATION_MODEL_ID: dict[str, str] = {
    SOCRATES_MODEL_ID: SOCRATES_2_0_MODEL_ID,
    ARISTOTLE_MODEL_ID: ARISTOTLE_2_0_MODEL_ID,
    PLATO_MODEL_ID: PLATO_3_0_MODEL_ID,
    PLATO_PRO_MODEL_ID: PLATO_PRO_2_0_MODEL_ID,
    SOCRATES_PRO_MODEL_ID: SOCRATES_PRO_1_0_MODEL_ID,
    SOCRATES_PRO_PLUS_MODEL_ID: SOCRATES_PRO_PLUS_1_0_MODEL_ID,
    IRIS_MODEL_ID: IRIS_3_0_MODEL_ID,
    PROMETHEUS_MODEL_ID: PROMETHEUS_3_0_MODEL_ID,
    ZEUS_MODEL_ID: ZEUS_2_0_MODEL_ID,
    ATHENA_MODEL_ID: ATHENA_2_0_MODEL_ID,
    IRIS_CODE_MODEL_ID: IRIS_CODE_1_0_MODEL_ID,
    PROMETHEUS_CODE_MODEL_ID: PROMETHEUS_CODE_1_0_MODEL_ID,
    ZEUS_CODE_MODEL_ID: ZEUS_CODE_1_0_MODEL_ID,
    FUSION_MODEL_ID: SYNTH_MODEL_ID,
    FUSION_CODE_MODEL_ID: SYNTH_CODE_MODEL_ID,
}

# Public routing aliases resolve before candidate selection. Keep this map
# separate from the orchestration aliases above: these names select the same
# routing policy, not a versioned orchestration preset.
ROUTING_MODEL_ALIAS_TARGETS: dict[str, str] = {
    CONFIDENTIAL_MODEL_ID: E2E_MODEL_ID,
}

ORCHESTRATION_LEGACY_ALIAS_MODEL_IDS = frozenset({FUSION_MODEL_ID, FUSION_CODE_MODEL_ID})

ORCHESTRATION_ROLLING_ALIAS_MODEL_IDS = frozenset(
    {
        SOCRATES_MODEL_ID,
        ARISTOTLE_MODEL_ID,
        PLATO_MODEL_ID,
        PLATO_PRO_MODEL_ID,
        SOCRATES_PRO_MODEL_ID,
        SOCRATES_PRO_PLUS_MODEL_ID,
        IRIS_MODEL_ID,
        PROMETHEUS_MODEL_ID,
        ZEUS_MODEL_ID,
        ATHENA_MODEL_ID,
        IRIS_CODE_MODEL_ID,
        PROMETHEUS_CODE_MODEL_ID,
        ZEUS_CODE_MODEL_ID,
    }
)

ORCHESTRATION_PRIMITIVE_MODEL_IDS = frozenset(
    {
        ADVISOR_MODEL_ID,
        SUBAGENT_MODEL_ID,
        SYNTH_MODEL_ID,
        SYNTH_CODE_MODEL_ID,
        SELECTOR_MODEL_ID,
        MAPREDUCE_MODEL_ID,
    }
)

EU_FOCUSED_PROVIDER_ORDER: tuple[str, ...] = (
    "mistral",
    "google-vertex",
    "openai",
    "anthropic",
    "tinfoil",
    "venice",
    "phala",
    "deepinfra",
    "nebius",
    "together",
    "cerebras",
)

US_FOCUSED_PROVIDER_ORDER: tuple[str, ...] = (
    "openai",
    "google-vertex",
    "google-ai-studio",
    "anthropic",
    "together",
    "fireworks",
    "baseten",
    "groq",
    "cerebras",
    "tinfoil",
    "deepinfra",
    "amazon-bedrock",
    "azure",
    "xai",
)

# The default `trustedrouter/auto` ladder — a PREFERENCE order, not a
# guarantee. The guarantee lives in routing: a request carrying
# provider.min_privacy or provider.jurisdiction filters this list BEFORE any
# provider is contacted, and 400s if nothing qualifies. So an entry that
# cannot satisfy a given request is never called for that request; it simply
# is not a candidate.
#
# Properties pinned by tests rather than trusted to review:
#
#   1. Privacy and jurisdiction requirements are applied before authorization;
#      an incompatible leading model is skipped rather than silently weakening
#      a caller's requested policy.
#   2. The ladder spans MORE THAN ONE provider. The 0813 leader itself has
#      release-pinned first-party and Baseten routes, and it is followed by
#      independent model/provider families.
#
# Order: strongest current DeepSeek release first, then cheap qualifying
# fallbacks. Privacy and jurisdiction requirements are still applied before
# any candidate is authorized.
#
# Anthropic sits at the BOTTOM deliberately. Its endpoints are currently
# PRIVACY_TIER_STANDARD (not yet zero-retention), so it is filtered out of any
# request that demands ZDR — but it remains a capable last-resort fallback for
# requests with no privacy floor, and it moves up on its own merits the moment
# its endpoint tier is raised.
DEFAULT_AUTO_MODEL_ORDER = [
    DEEPSEEK_V4_PRO_0813_MODEL_ID,
    "deepseek/deepseek-v4-flash-0731",
    "moonshotai/kimi-k3",
    "z-ai/glm-5.2",
    "openai/gpt-4.1-mini",
    "google/gemini-2.5-flash",
    "moonshotai/kimi-k2.6",
    "minimax/minimax-m3",
    "openai/gpt-5.4-mini",
    "anthropic/claude-sonnet-4.6",
]

# Released orchestration presets are immutable. Never change the component
# graph behind a versioned model ID; introduce a new preset version and move
# only its rolling alias. This is especially important for G1/G2 and the
# historical 0423 DeepSeek graphs used below.
SYNTH_IRIS_1_MODEL_ORDER = (
    "minimax/minimax-m3",
    "moonshotai/kimi-k2.6",
    DEEPSEEK_V4_PRO_0423_MODEL_ID,
)

SYNTH_BUDGET_MODEL_ORDER = (
    "minimax/minimax-m3",
    "moonshotai/kimi-k2.6",
    DEEPSEEK_V4_PRO_0813_MODEL_ID,
)

SYNTH_IRIS_2_MODEL_ORDER = (
    "minimax/minimax-m3",
    "moonshotai/kimi-k3",
    DEEPSEEK_V4_PRO_0423_MODEL_ID,
)

SYNTH_IRIS_3_MODEL_ORDER = (
    "minimax/minimax-m3",
    "moonshotai/kimi-k3",
    DEEPSEEK_V4_PRO_0813_MODEL_ID,
)

SYNTH_PROMETHEUS_1_MODEL_ORDER = (
    "minimax/minimax-m3",
    "moonshotai/kimi-k2.6",
    "z-ai/glm-5.2",
    "google/gemma-4-31b-it",
    DEEPSEEK_V4_PRO_0423_MODEL_ID,
)

SYNTH_QUALITY_MODEL_ORDER = (
    "minimax/minimax-m3",
    "moonshotai/kimi-k2.6",
    "z-ai/glm-5.2",
    "google/gemma-4-31b-it",
    DEEPSEEK_V4_PRO_0813_MODEL_ID,
)

SYNTH_QUALITY_1M_MODEL_ORDER = (
    "minimax/minimax-m3",
    "xiaomi/mimo-v2.5-pro",
    "z-ai/glm-5.2",
    DEEPSEEK_V4_PRO_0423_MODEL_ID,
)

SYNTH_PROMETHEUS_2_MODEL_ORDER = (
    "minimax/minimax-m3",
    "moonshotai/kimi-k3",
    "z-ai/glm-5.2",
    DEEPSEEK_V4_PRO_0423_MODEL_ID,
    "xiaomi/mimo-v2.5-pro",
)

SYNTH_PROMETHEUS_3_MODEL_ORDER = (
    "minimax/minimax-m3",
    "moonshotai/kimi-k3",
    "z-ai/glm-5.2",
    DEEPSEEK_V4_PRO_0813_MODEL_ID,
    "xiaomi/mimo-v2.5-pro",
)

LIBERTY_1_0_MODEL_ORDER = (
    "thinkingmachines/inkling",
    "nvidia/nemotron-3-ultra-550b-a55b",
    "google/gemma-4-31b-it",
)

LIBERTY_1_0_1M_MODEL_ORDER = (
    "thinkingmachines/inkling-1m",
    "nvidia/nemotron-3-ultra-550b-a55b",
)

SYNTH_FRONTIER_1_MODEL_ORDER = (
    "anthropic/claude-opus-4.8",
    "openai/gpt-5.5",
    "google/gemini-3.1-pro-preview",
    "google/gemini-3.5-flash",
    "minimax/minimax-m3",
    "z-ai/glm-5.2",
    "xiaomi/mimo-v2.5-pro",
    DEEPSEEK_V4_PRO_0423_MODEL_ID,
)

SYNTH_FRONTIER_MINI_MODEL_ORDER = (
    "google/gemini-3.1-pro-preview",
    "google/gemini-3.5-flash",
    "minimax/minimax-m3",
    "z-ai/glm-5.2",
    "xiaomi/mimo-v2.5-pro",
    DEEPSEEK_V4_PRO_0423_MODEL_ID,
)

SYNTH_FRONTIER_MODEL_ORDER = (
    "anthropic/claude-opus-4.8",
    "openai/gpt-5.5",
    "google/gemini-3.1-pro-preview",
    "google/gemini-3.5-flash",
    "minimax/minimax-m3",
    "z-ai/glm-5.2",
    "xiaomi/mimo-v2.5-pro",
    DEEPSEEK_V4_PRO_0813_MODEL_ID,
)

SYNTH_CODE_BUDGET_1_MODEL_ORDER = (
    "minimax/minimax-m3",
    "moonshotai/kimi-k2.7-code",
    DEEPSEEK_V4_PRO_0423_MODEL_ID,
)

SYNTH_CODE_BUDGET_MODEL_ORDER = (
    "minimax/minimax-m3",
    "moonshotai/kimi-k2.7-code",
    DEEPSEEK_V4_PRO_0813_MODEL_ID,
)

SYNTH_CODE_QUALITY_1_MODEL_ORDER = (
    "minimax/minimax-m3",
    "moonshotai/kimi-k2.7-code",
    "z-ai/glm-5.2",
    "google/gemma-4-31b-it",
    DEEPSEEK_V4_PRO_0423_MODEL_ID,
)

SYNTH_CODE_QUALITY_MODEL_ORDER = (
    "minimax/minimax-m3",
    "moonshotai/kimi-k2.7-code",
    "z-ai/glm-5.2",
    "google/gemma-4-31b-it",
    DEEPSEEK_V4_PRO_0813_MODEL_ID,
)

SYNTH_CODE_FRONTIER_MODEL_ORDER = (
    "anthropic/claude-opus-4.8",
    "openai/gpt-5.5",
    "anthropic/claude-sonnet-4.6",
    "google/gemini-3.1-pro-preview",
    "moonshotai/kimi-k2.7-code",
)

SOCRATES_WORKER_MODEL_ORDER = ("cerebras/gpt-oss-120b", "deepseek/deepseek-v4-flash")

SOCRATES_ADVISOR_MODEL_ORDER = (SOCRATES_PRO_1_0_MODEL_ID,)

SOCRATES_CATALOG_MODEL_ORDER = (
    "cerebras/gpt-oss-120b",
    "deepseek/deepseek-v4-flash",
    "cerebras/zai-glm-4.7",
    "xiaomi/mimo-v2.5-pro-ultraspeed",
    "anthropic/claude-opus-4.8",
)

SOCRATES_1_1_WORKER_MODEL_ORDER = (
    "xiaomi/mimo-v2.5-pro-ultraspeed",
    "minimax/minimax-m3",
    "z-ai/glm-5.2-fast",
    "deepseek/deepseek-v4-flash",
)

SOCRATES_1_1_CATALOG_MODEL_ORDER = (
    *SOCRATES_1_1_WORKER_MODEL_ORDER,
    ZEUS_1_0_MODEL_ID,
)

SOCRATES_2_0_WORKER_MODEL_ORDER = (
    "xiaomi/mimo-v2.5-pro-ultraspeed",
    "minimax/minimax-m3",
    "z-ai/glm-5.2-fast",
    DEEPSEEK_V4_PRO_0813_MODEL_ID,
)

SOCRATES_2_0_CATALOG_MODEL_ORDER = (
    *SOCRATES_2_0_WORKER_MODEL_ORDER,
    ZEUS_2_0_MODEL_ID,
)

SELECTOR_CATALOG_MODEL_ORDER = (
    *SYNTH_QUALITY_MODEL_ORDER,
    "moonshotai/kimi-k2.7-code",
    "minimax/minimax-m3",
)

MAPREDUCE_CATALOG_MODEL_ORDER = (
    "deepseek/deepseek-v4-flash",
    "minimax/minimax-m3",
    "cerebras/gpt-oss-120b",
    *SYNTH_QUALITY_MODEL_ORDER,
)

ADVISOR_CATALOG_MODEL_ORDERS: dict[str, tuple[str, ...]] = {
    SOCRATES_1_0_MODEL_ID: SOCRATES_CATALOG_MODEL_ORDER,
    SOCRATES_1_1_MODEL_ID: SOCRATES_1_1_CATALOG_MODEL_ORDER,
    SOCRATES_2_0_MODEL_ID: SOCRATES_2_0_CATALOG_MODEL_ORDER,
    SOCRATES_MODEL_ID: SOCRATES_2_0_CATALOG_MODEL_ORDER,
    ADVISOR_MODEL_ID: SOCRATES_CATALOG_MODEL_ORDER,
    SUBAGENT_MODEL_ID: (
        "deepseek/deepseek-v4-flash",
        "cerebras/gpt-oss-120b",
        "anthropic/claude-sonnet-5",
    ),
    ARISTOTLE_1_0_MODEL_ID: (
        "deepseek/deepseek-v4-flash",
        *SYNTH_FRONTIER_1_MODEL_ORDER,
    ),
    ARISTOTLE_1_1_MODEL_ID: (
        "z-ai/glm-5.2-fast",
        "z-ai/glm-5.2",
        ZEUS_1_0_MODEL_ID,
    ),
    ARISTOTLE_MODEL_ID: (
        "z-ai/glm-5.2-fast",
        "z-ai/glm-5.2",
        ZEUS_2_0_MODEL_ID,
    ),
    ARISTOTLE_2_0_MODEL_ID: (
        "z-ai/glm-5.2-fast",
        "z-ai/glm-5.2",
        ZEUS_2_0_MODEL_ID,
    ),
    PLATO_1_0_MODEL_ID: (
        "deepseek/deepseek-v4-flash",
        "z-ai/glm-5.2",
        *SYNTH_PROMETHEUS_1_MODEL_ORDER,
    ),
    PLATO_MODEL_ID: (
        DEEPSEEK_V4_PRO_0813_MODEL_ID,
        PROMETHEUS_3_0_MODEL_ID,
    ),
    PLATO_3_0_MODEL_ID: (
        DEEPSEEK_V4_PRO_0813_MODEL_ID,
        PROMETHEUS_3_0_MODEL_ID,
    ),
    PLATO_PRO_1_0_MODEL_ID: (
        "z-ai/glm-5.2",
        PROMETHEUS_1_0_1M_MODEL_ID,
    ),
    PLATO_PRO_2_0_MODEL_ID: (
        "z-ai/glm-5.2",
        PROMETHEUS_2_0_MODEL_ID,
    ),
    PLATO_PRO_MODEL_ID: (
        "z-ai/glm-5.2",
        PROMETHEUS_2_0_MODEL_ID,
    ),
    SOCRATES_PRO_1_0_MODEL_ID: (
        "cerebras/zai-glm-4.7",
        "xiaomi/mimo-v2.5-pro-ultraspeed",
        "anthropic/claude-opus-4.8",
    ),
    SOCRATES_PRO_MODEL_ID: (
        "cerebras/zai-glm-4.7",
        "xiaomi/mimo-v2.5-pro-ultraspeed",
        "anthropic/claude-opus-4.8",
    ),
    SOCRATES_PRO_PLUS_1_0_MODEL_ID: SOCRATES_1_1_CATALOG_MODEL_ORDER,
    SOCRATES_PRO_PLUS_MODEL_ID: SOCRATES_1_1_CATALOG_MODEL_ORDER,
    OPEN_PATCHER_A1_MODEL_ID: (
        OPEN_PATCHER_S1_MODEL_ID,
        PROMETHEUS_1_0_MODEL_ID,
    ),
    OPEN_PATCHER_FAST1_MODEL_ID: (
        "z-ai/glm-5.2-fast",
        OPEN_PATCHER_A1_MODEL_ID,
    ),
    OPEN_PATCHER_G1_MODEL_ID: (
        "z-ai/glm-5.2-fast",
        "z-ai/glm-5.2",
        "moonshotai/kimi-k2.7-code",
        PROMETHEUS_1_0_1M_MODEL_ID,
    ),
    OPEN_PATCHER_G2_MODEL_ID: (
        "moonshotai/kimi-k3",
        "google/gemma-4-31b-it",
        PROMETHEUS_2_0_MODEL_ID,
    ),
    OPEN_PATCHER_G3_MODEL_ID: (
        "moonshotai/kimi-k3",
        "google/gemma-4-31b-it",
        PROMETHEUS_3_0_MODEL_ID,
    ),
    ATHENA_1_0_MODEL_ID: (
        "z-ai/glm-5.2-fast",
        "z-ai/glm-5.2",
        ZEUS_1_0_MINI_MODEL_ID,
        "moonshotai/kimi-k2.7-code",
        "moonshotai/kimi-k2.6",
    ),
    ATHENA_2_0_MODEL_ID: (
        "z-ai/glm-5.2-fast",
        "z-ai/glm-5.2",
        ZEUS_2_0_MODEL_ID,
        "moonshotai/kimi-k2.7-code",
        "moonshotai/kimi-k2.6",
    ),
    ATHENA_MODEL_ID: (
        "z-ai/glm-5.2-fast",
        "z-ai/glm-5.2",
        ZEUS_2_0_MODEL_ID,
        "moonshotai/kimi-k2.7-code",
        "moonshotai/kimi-k2.6",
    ),
    LIBERTY_2_0_MODEL_ID: (
        "nvidia/nemotron-3-ultra-550b-a55b",
        LIBERTY_1_0_1M_MODEL_ID,
        LIBERTY_1_0_MODEL_ID,
    ),
    PARASAIL_LIBERTY_2_0_MODEL_ID: (
        "nvidia/nemotron-3-ultra-550b-a55b",
        LIBERTY_1_0_1M_MODEL_ID,
        LIBERTY_1_0_MODEL_ID,
    ),
    LIBERTY_3_0_MODEL_ID: (
        "nvidia/nemotron-3-ultra-550b-a55b",
        "google/gemma-4-31b-it",
        "openai/gpt-oss-120b",
        LIBERTY_1_0_1M_MODEL_ID,
        "thinkingmachines/inkling",
    ),
}


class _EmbeddingSpec(TypedDict):
    id: str
    name: str
    provider: str
    upstream_id: str
    context_length: int
    cost_dollars_per_million: str


_EMBEDDING_SPECS: tuple[_EmbeddingSpec, ...] = (
    # OpenAI — api.openai.com/v1/embeddings (OpenAI-shaped)
    {
        "id": "openai/text-embedding-3-large",
        "name": "OpenAI Text Embedding 3 Large",
        "provider": "openai",
        "upstream_id": "text-embedding-3-large",
        "context_length": 8191,
        "cost_dollars_per_million": "0.13",
    },
    {
        "id": "openai/text-embedding-3-small",
        "name": "OpenAI Text Embedding 3 Small",
        "provider": "openai",
        "upstream_id": "text-embedding-3-small",
        "context_length": 8191,
        "cost_dollars_per_million": "0.02",
    },
    {
        "id": "openai/text-embedding-ada-002",
        "name": "OpenAI Text Embedding Ada 002",
        "provider": "openai",
        "upstream_id": "text-embedding-ada-002",
        "context_length": 8191,
        "cost_dollars_per_million": "0.10",
    },
    # Google Gemini — generativelanguage.googleapis.com/v1beta :embedContent
    {
        "id": "google/gemini-embedding-001",
        "name": "Gemini Embedding 001",
        "provider": "google-ai-studio",
        "upstream_id": "gemini-embedding-001",
        "context_length": 2048,
        "cost_dollars_per_million": "0.15",
    },
    # Together — api.together.xyz/v1/embeddings (OpenAI-shaped). Only the
    # SERVERLESS embedding model is carried: verified live against Together
    # on our account 2026-06-07. (m2-bert + bge-large-en are retired;
    # BAAI/bge-base-en-v1.5 is listed in /v1/models but is dedicated-only —
    # the serverless endpoint 400s it "create a dedicated endpoint" — so it's
    # intentionally excluded. multilingual-e5-large-instruct returns 200.)
    {
        "id": "intfloat/multilingual-e5-large-instruct",
        "name": "Multilingual E5 Large Instruct",
        "provider": "together",
        "upstream_id": "intfloat/multilingual-e5-large-instruct",
        "context_length": 512,
        "cost_dollars_per_million": "0.02",
    },
    # Cohere — api.cohere.com/v2/embed (NATIVE shape; enclave adapts to OpenAI)
    {
        "id": "cohere/embed-v4.0",
        "name": "Cohere Embed v4.0",
        "provider": "cohere",
        "upstream_id": "embed-v4.0",
        "context_length": 128_000,
        "cost_dollars_per_million": "0.12",
    },
    {
        "id": "cohere/embed-english-v3.0",
        "name": "Cohere Embed English v3.0",
        "provider": "cohere",
        "upstream_id": "embed-english-v3.0",
        "context_length": 512,
        "cost_dollars_per_million": "0.10",
    },
    {
        "id": "cohere/embed-multilingual-v3.0",
        "name": "Cohere Embed Multilingual v3.0",
        "provider": "cohere",
        "upstream_id": "embed-multilingual-v3.0",
        "context_length": 512,
        "cost_dollars_per_million": "0.10",
    },
    # Voyage AI — api.voyageai.com/v1/embeddings (OpenAI-shaped). voyage-3-large
    # is top-tier retrieval-per-dollar; supports MRL output dims + int8/binary
    # quantization (callers pass `dimensions` / `output_dtype`).
    {
        "id": "voyage/voyage-3-large",
        "name": "Voyage 3 Large",
        "provider": "voyage",
        "upstream_id": "voyage-3-large",
        "context_length": 32_000,
        "cost_dollars_per_million": "0.18",
    },
    # Qwen3-Embedding-8B — open model, served serverlessly by DeepInfra
    # (api.deepinfra.com/v1/openai/embeddings, OpenAI-shaped). Tops MTEB; 4096
    # dims with MRL. Routed via DeepInfra so TR runs no GPU. (Verify the route
    # is live on our DeepInfra key via the daily embeddings probe.)
    {
        "id": "Qwen/Qwen3-Embedding-8B",
        "name": "Qwen3 Embedding 8B",
        "provider": "deepinfra",
        "upstream_id": "Qwen/Qwen3-Embedding-8B",
        "context_length": 32_000,
        "cost_dollars_per_million": "0.01",
    },
)

_PROVIDER_SERVED_MODEL_ALLOWLIST: dict[str, frozenset[str]] = {
    # 2026-07-18: GMI's /models listing is aspirational — 7d synthetic probes
    # show exactly four models served on our account (590-670 successes each)
    # while the other ~45 listed models have ZERO successes ever (uniform
    # upstream 404 "No matching target server found"). Route Credits traffic
    # only to the verified set; BYOK stays visible (customer accounts may
    # differ). A new GMI model earns its way in via probe successes.
    "gmi": frozenset(
        {
            "deepseek/deepseek-v4-pro",
            "moonshotai/kimi-k3",
            "z-ai/glm-5",
            "z-ai/glm-5.1",
            "z-ai/glm-5.2",
        }
    ),
}

_UNSERVED_CREDITS_MODELS: frozenset[str] = frozenset(
    {
        "openai/gpt-5.4",
        "openai/gpt-5.4-nano",
        "openai/gpt-5.4-pro",
        "openai/gpt-5.5-pro",
    }
)

_PROVIDER_UNSERVED_CREDITS_MODELS: dict[str, frozenset[str]] = {
    "anthropic": frozenset(
        {
            # Undated alias stopped serving on TR's operator key ~2026-06-21.
            # Credits-only drop keeps BYOK visible for customers whose own keys
            # may still serve it; PROMOTE to _PROVIDER_DEPRECATED_UPSTREAM_MODELS
            # after Anthropic's formal retirement on 2026-08-05.
            "anthropic/claude-opus-4.1",
        }
    ),
    "gmi": frozenset(
        {
            "anthropic/claude-opus-4.7",
            "openai/gpt-5.5",
            # 2026-07-15: the snapshot route returns 404 when pinned to GMI.
            # Keep the directly verified Baseten route.
            "nvidia/nemotron-3-ultra-550b-a55b",
            # 2026-06-24: GMI returns HTTP 200 with an empty assistant message
            # for these Gemma 4 routes when pinned through the live gateway.
            # Treat as unserved for prepaid routing until GMI returns usable
            # text; leave customer BYOK routes visible.
            "google/gemma-4-26b-a4b-it",
            "google/gemma-4-31b-it",
        }
    ),
    "openai": frozenset({"openai/gpt-oss-120b", "openai/gpt-oss-20b"}),
    "deepseek": frozenset({"deepseek/deepseek-chat-v3.1", "deepseek/deepseek-v3.2"}),
    "nebius": frozenset({"google/gemma-2-2b-it", "meta-llama/Meta-Llama-3.1-8B-Instruct"}),
    "zai": frozenset({"z-ai/glm-4-32b", "z-ai/glm-4.7-flash"}),
    "grok": frozenset({"x-ai/grok-4.20-multi-agent"}),
    # parasail — listed in the upstream snapshot, but Parasail's own chat API
    # returns 403 "deployment ... doesn't exist or isn't accessible" for these
    # routes on our operator key (direct API probe 2026-06-05). Keep BYOK
    # visible for customer accounts, but do not route prepaid traffic here.
    "parasail": frozenset(
        {
            "qwen/qwen3-235b-a22b-2507",
            "z-ai/glm-5",
            # 2026-06-24: live gateway probes pinned to Parasail return
            # deterministic provider 403 "Forbidden" HTML for these routes
            # on the operator key. These are config/unserved, not downtime.
            "deepseek/deepseek-v3.2",
            "moonshotai/kimi-k2.5",
            "stepfun/step-3.5-flash",
            "z-ai/glm-4.7",
        }
    ),
    # novita — Novita's /models currently lists these ids, but chat returns
    # MODEL_NOT_AVAILABLE / SERVICE_NOT_AVAILABLE for the exact routes below
    # on our operator key (direct API probes 2026-06-05/06). Other Novita
    # failures observed the same hour were overload/timeouts (ttfb_exceeded),
    # so THOSE stay counted as provider health and are NOT dropped here — they
    # work when Novita isn't overloaded. Second batch (2026-06-06) added after a
    # verified sweep cross-checked each failing route's error class.
    "novita": frozenset(
        {
            "meta-llama/llama-3-8b-instruct",
            "qwen/qwen2.5-vl-72b-instruct",
            "qwen/qwen3-4b-fp8",
            # 2026-06-06: persistent MODEL_NOT_AVAILABLE / SERVICE_NOT_AVAILABLE
            "baidu/ernie-4.5-21B-a3b-thinking",
            "baidu/ernie-4.5-300b-a47b-paddle",
            "baidu/ernie-4.5-vl-28b-a3b-thinking",
            "deepseek/deepseek-r1-0528-qwen3-8b",
            "nousresearch/hermes-2-pro-llama-3-8b",
            "qwen/qwen2.5-7b-instruct",
            "qwen/qwen3-30b-a3b-fp8",
            "qwen/qwen3-32b-fp8",
            # 2026-06-24: explicit MODEL_NOT_AVAILABLE from Novita.
            "deepseek/deepseek-prover-v2-671b",
            # 2026-06-06 batch 3: dropped after a SERIALIZED re-test (cooldown +
            # 25s gaps, so not our own rate-limit). sao10k/xiaomimimo return a
            # fast explicit NOT_AVAILABLE; the rest queue with no first byte then
            # 429 (~60s) — i.e. Novita never usefully serves them on our key, so
            # they only 502 + burn the SLO. (NB: nebius Qwen3.5-397B-A17B-fast,
            # also slow, is deliberately KEPT — a 397B model legitimately takes
            # >20s to first token and we want it available.)
            "qwen/qwen3-8b-fp8",
            "meta-llama/llama-3.2-3b-instruct",
            "gryphe/mythomax-l2-13b",
            "paddlepaddle/paddleocr-vl",
            "sao10k/l3-70b-euryale-v2.1",
            "xiaomimimo/mimo-v2-flash",
        }
    ),
    # minimax — first-party MiniMax-M2.1 and MiniMax-M2.5 return a 200 stream
    # containing only finish_reason=stop and no content on our operator key
    # (verified via pinned gateway probes 2026-06-05). Highspeed variants,
    # M2, M2.7, and M3 stream content correctly, so only suppress these two
    # Credits routes. BYOK remains available because customer accounts can
    # have different model behavior/entitlements.
    "minimax": frozenset({"minimax/minimax-m2.1", "minimax/minimax-m2.5"}),
    "google-ai-studio": frozenset(
        {
            "google/gemma-3-4b-it",
            "google/gemma-3-12b-it",
            "google/gemma-3-27b-it",
            "google/gemma-4-26b-a4b-it",
            "google/gemma-4-31b-it",
        }
    ),
    "google-vertex": frozenset(
        {
            "google/gemma-3-4b-it",
            "google/gemma-3-12b-it",
            "google/gemma-3-27b-it",
            "google/gemma-4-26b-a4b-it",
            "google/gemma-4-31b-it",
        }
    ),
}

_PROVIDER_DISPLAY_ORDER = ("tinfoil", "near-ai")


# Legacy compatibility aliases (advisor/synth primitives) — completes
# ORCHESTRATION_PRIMITIVE_BY_MODEL_ID within this module so a direct import
# sees the full mapping, not just the base entries (codex #101).
for _advisor_model_id in (
    SOCRATES_1_0_MODEL_ID,
    SOCRATES_1_1_MODEL_ID,
    SOCRATES_2_0_MODEL_ID,
    SOCRATES_MODEL_ID,
    ARISTOTLE_1_0_MODEL_ID,
    ARISTOTLE_1_1_MODEL_ID,
    ARISTOTLE_2_0_MODEL_ID,
    ARISTOTLE_MODEL_ID,
    PLATO_1_0_MODEL_ID,
    PLATO_3_0_MODEL_ID,
    PLATO_MODEL_ID,
    PLATO_PRO_1_0_MODEL_ID,
    PLATO_PRO_2_0_MODEL_ID,
    PLATO_PRO_MODEL_ID,
    SOCRATES_PRO_1_0_MODEL_ID,
    SOCRATES_PRO_MODEL_ID,
    SOCRATES_PRO_PLUS_1_0_MODEL_ID,
    SOCRATES_PRO_PLUS_MODEL_ID,
    OPEN_PATCHER_A1_MODEL_ID,
    OPEN_PATCHER_FAST1_MODEL_ID,
    OPEN_PATCHER_G1_MODEL_ID,
    OPEN_PATCHER_G2_MODEL_ID,
    OPEN_PATCHER_G3_MODEL_ID,
    ATHENA_1_0_MODEL_ID,
    ATHENA_2_0_MODEL_ID,
    ATHENA_MODEL_ID,
    LIBERTY_2_0_MODEL_ID,
    PARASAIL_LIBERTY_2_0_MODEL_ID,
    LIBERTY_3_0_MODEL_ID,
):
    ORCHESTRATION_PRIMITIVE_BY_MODEL_ID[_advisor_model_id] = "advisor"

for _synth_model_id in (
    IRIS_MODEL_ID,
    PROMETHEUS_MODEL_ID,
    ZEUS_MODEL_ID,
    IRIS_1_0_MODEL_ID,
    IRIS_2_0_MODEL_ID,
    IRIS_3_0_MODEL_ID,
    PROMETHEUS_1_0_MODEL_ID,
    PROMETHEUS_1_0_1M_MODEL_ID,
    PROMETHEUS_2_0_MODEL_ID,
    PROMETHEUS_3_0_MODEL_ID,
    ZEUS_1_0_MODEL_ID,
    ZEUS_1_0_MINI_MODEL_ID,
    ZEUS_2_0_MODEL_ID,
    IRIS_CODE_MODEL_ID,
    PROMETHEUS_CODE_MODEL_ID,
    ZEUS_CODE_MODEL_ID,
    IRIS_CODE_1_0_MODEL_ID,
    PROMETHEUS_CODE_1_0_MODEL_ID,
    ZEUS_CODE_1_0_MODEL_ID,
    OPEN_PATCHER_S1_MODEL_ID,
    OPEN_PATCHER_S2_MODEL_ID,
    OPEN_PATCHER_S3_MODEL_ID,
    LIBERTY_1_0_MODEL_ID,
    LIBERTY_1_0_1M_MODEL_ID,
):
    ORCHESTRATION_PRIMITIVE_BY_MODEL_ID[_synth_model_id] = "synth"


# ---------------------------------------------------------------------------
# MODEL ORIGIN (creator lab) METADATA
# ---------------------------------------------------------------------------
# Where a model was BUILT, keyed by the vendor prefix of its TrustedRouter model
# id (the part before the slash). This answers a different question from
# Provider.provider_headquarters_country above, which records where the operator
# of the routed API endpoint is legally based. A request for GLM through
# api.z.ai reaches a Singapore operator running weights from a Beijing lab, and
# both rows are needed to describe that honestly.
#
# Rules this table follows:
#   * One row per vendor prefix that actually appears in the catalog. Each cites
#     the lab's own page, its licence, or a regulatory filing where one states a
#     location, and a named reference work where the lab publishes none.
#   * country is an ISO 3166-1 alpha-2 code for the creator's home, or None when
#     the prefix has no single creator country. A None row must carry a note
#     saying why, and no row may claim a country without a source_url.
#   * Case variants are separate keys, because provider feeds publish upstream
#     ids verbatim (Qwen and qwen, MiniMaxAI and minimax) and the catalog keeps
#     them that way.
#   * Some prefixes are a serving namespace rather than a lab: lightning-ai and
#     cerebras publish other labs' weights under their own name. Those rows are
#     recorded with country None so nobody reads a hosting brand as an origin.
#   * A model's origin country is not a claim about that model's licence, export
#     status, or the jurisdiction any given request runs in.


@dataclass(frozen=True)
class ModelOrigin:
    country: str | None
    lab_name: str
    source_url: str | None = None
    note: str = ""


_MODEL_ORIGIN_US_TRUSTEDROUTER = ModelOrigin(
    country=PROVIDER_JURISDICTION_US,
    lab_name="TrustedRouter",
    source_url="https://trustedrouter.com/",
    note=(
        "First-party record: the Liberty, Athena, OpenPatcher, and orchestration "
        "families are built by TrustedRouter, a US company. The OpenPatcher and "
        "Athena ids in US_PROVIDER_ONLY_MODEL_IDS additionally force US-operated "
        "provider routes at request time."
    ),
)

_MODEL_ORIGIN_CN_ALIBABA = ModelOrigin(
    country=PROVIDER_JURISDICTION_CN,
    lab_name="Alibaba (Qwen)",
    source_url="https://www.alibabacloud.com/en/solutions/generative-ai/qwen",
    note=(
        "Alibaba Cloud states it provides the Qwen model series to the "
        "open-source community; Alibaba Group Holding Limited gives its "
        "principal executive offices as Hangzhou, China "
        "(https://www.alibabagroup.com/en-US/faqs-corporate-information)."
    ),
)

_MODEL_ORIGIN_CN_DEEPSEEK = ModelOrigin(
    country=PROVIDER_JURISDICTION_CN,
    lab_name="DeepSeek",
    source_url="https://cdn.deepseek.com/policies/en-US/deepseek-privacy-policy.html",
    note=(
        "DeepSeek's privacy policy names Hangzhou DeepSeek Artificial "
        "Intelligence Co., Ltd., registered in China, as data controller."
    ),
)

_MODEL_ORIGIN_CN_ZHIPU = ModelOrigin(
    country=PROVIDER_JURISDICTION_CN,
    lab_name="Z.ai (GLM)",
    source_url="https://en.wikipedia.org/wiki/Zhipu_AI",
    note=(
        "The GLM family comes from the Beijing-headquartered lab that renamed "
        "itself Z.ai in 2025, formerly Beijing Zhipu Huazhang Technology Co., "
        "Ltd. The API TrustedRouter routes to is operated by a Singapore entity "
        "(see the zai provider row), which is a separate fact."
    ),
)

_MODEL_ORIGIN_CN_MINIMAX = ModelOrigin(
    country=PROVIDER_JURISDICTION_CN,
    lab_name="MiniMax",
    source_url="https://en.wikipedia.org/wiki/MiniMax_Group",
    note="MiniMax Group Inc. is based in Shanghai, China and listed in Hong Kong (SEHK: 100).",
)

_MODEL_ORIGIN_CN_XIAOMI = ModelOrigin(
    country=PROVIDER_JURISDICTION_CN,
    lab_name="Xiaomi (MiMo)",
    source_url=(
        "https://ir.mi.com/system/files-encrypted/nasdaq_kms/assets/2026/04/28/"
        "5-29-08/Xiaomi%202025%20AR_EN.pdf"
    ),
    note=(
        "Xiaomi Corporation's 2025 annual report gives its head office and "
        "principal place of business as Xiaomi Campus, Haidian District, "
        "Beijing, PRC, with a Cayman Islands registered office."
    ),
)

_MODEL_ORIGIN_INDEPENDENT_SAO10K = ModelOrigin(
    country=None,
    lab_name="Sao10K (independent model author)",
    source_url="https://huggingface.co/Sao10K",
    note=(
        "These are community fine-tunes published by a pseudonymous individual. "
        "The author's profile discloses no company, legal entity, or country, so "
        "no origin country is recorded rather than inferred from a handle."
    ),
)

MODEL_ORIGINS: dict[str, ModelOrigin] = {
    # --- United States ---
    "trustedrouter": _MODEL_ORIGIN_US_TRUSTEDROUTER,
    "openai": ModelOrigin(
        country=PROVIDER_JURISDICTION_US,
        lab_name="OpenAI",
        source_url="https://openai.com/policies/row-terms-of-use/",
        note=(
            "OpenAI's terms of use name OpenAI OpCo, LLC, a Delaware company at "
            "1455 3rd Street, San Francisco, CA 94158."
        ),
    ),
    "anthropic": ModelOrigin(
        country=PROVIDER_JURISDICTION_US,
        lab_name="Anthropic",
        source_url="https://www.anthropic.com/legal/commercial-terms",
        note=(
            "Anthropic's commercial terms contract through Anthropic, PBC under "
            "California law outside the EEA, Switzerland, and the UK, where "
            "Anthropic Ireland, Limited applies instead."
        ),
    ),
    "google": ModelOrigin(
        country=PROVIDER_JURISDICTION_US,
        lab_name="Google DeepMind",
        source_url="https://s206.q4cdn.com/479360582/files/doc_financials/2025/q4/GOOG-10-K-2025.pdf",
        note=(
            "Gemini and Gemma come from Google DeepMind, part of Alphabet Inc., "
            "whose 10-K gives principal executive offices at 1600 Amphitheatre "
            "Parkway, Mountain View, California. The code records that parent's "
            "home, not the location of every team that worked on a model."
        ),
    ),
    "meta-llama": ModelOrigin(
        country=PROVIDER_JURISDICTION_US,
        lab_name="Meta",
        source_url="https://developer.meta.com/ai/llama4/license/",
        note=(
            "The Llama licence names Meta Platforms, Inc. for licensees outside "
            "the EEA and Switzerland, and Meta Platforms Ireland Limited within "
            "them."
        ),
    ),
    "nvidia": ModelOrigin(
        country=PROVIDER_JURISDICTION_US,
        lab_name="NVIDIA",
        source_url="https://investor.nvidia.com/governance/contact-the-board/default.aspx",
        note=(
            "The Nemotron family comes from NVIDIA Corporation, whose corporate "
            "address for stockholder communications is 2788 San Tomas "
            "Expressway, Santa Clara, California 95051."
        ),
    ),
    "x-ai": ModelOrigin(
        country=PROVIDER_JURISDICTION_US,
        lab_name="xAI",
        source_url="https://x.ai/legal/terms-of-service",
        note=(
            "xAI's consumer terms take legal notices at 1450 Page Mill Rd., Palo "
            "Alto, CA 94304 and are governed by the laws of the State of Texas."
        ),
    ),
    "thinkingmachines": ModelOrigin(
        country=PROVIDER_JURISDICTION_US,
        lab_name="Thinking Machines Lab",
        source_url="https://en.wikipedia.org/wiki/Thinking_Machines_Lab",
        note=(
            "Thinking Machines Lab Inc. is a public benefit corporation based in "
            "San Francisco, California. Its own site names the company without "
            "stating a location."
        ),
    ),
    "microsoft": ModelOrigin(
        country=PROVIDER_JURISDICTION_US,
        lab_name="Microsoft Research",
        source_url="https://www.microsoft.com/en-us/privacy/privacystatement",
        note=(
            "The Phi family is built by Microsoft Research. Microsoft's privacy "
            "statement identifies Microsoft Corporation at One Microsoft Way, "
            "Redmond, Washington 98052, United States."
        ),
    ),
    "recraft": ModelOrigin(
        country=PROVIDER_JURISDICTION_US,
        lab_name="Recraft",
        source_url="https://www.recraft.ai/legal/terms",
        note=(
            "Recraft's terms name Recraft Inc. and give its notices address as "
            "450 Townsend Street, San Francisco, California 94107."
        ),
    ),
    "ibm-granite": ModelOrigin(
        country=PROVIDER_JURISDICTION_US,
        lab_name="IBM Research",
        source_url="https://research.ibm.com/blog/introducing-granite-4-2",
        note=(
            "IBM Research identifies Granite 4.2 as its model family. IBM's "
            "corporate contact page lists International Business Machines "
            "Corporation in Armonk, New York, United States "
            "(https://www.ibm.com/contact/global)."
        ),
    ),
    # --- Canada ---
    "cohere": ModelOrigin(
        country=PROVIDER_JURISDICTION_CA,
        lab_name="Cohere",
        source_url="https://cohere.com/privacy",
        note=(
            "Cohere Inc., 171 John Street, Suite 200, Toronto, ON Canada, "
            "described in its own privacy policy as a Canadian company subject "
            "to Canadian federal privacy laws."
        ),
    ),
    # --- France ---
    "mistralai": ModelOrigin(
        country=PROVIDER_JURISDICTION_FR,
        lab_name="Mistral AI",
        source_url="https://legal.mistral.ai/legal-notice",
        note=(
            "Mistral AI is a French SAS registered at 15 rue des Halles, 75001 "
            "Paris, RCS Paris 952 418 325."
        ),
    ),
    # --- Germany ---
    "jina-ai": ModelOrigin(
        country=PROVIDER_JURISDICTION_DE,
        lab_name="Jina AI",
        source_url="https://jina.ai/en-US/contact-sales/",
        note=(
            "Jina's first-party company page identifies Jina AI GmbH as the "
            "company headquarters in Germany and the issuer of API invoices."
        ),
    ),
    "black-forest-labs": ModelOrigin(
        country=PROVIDER_JURISDICTION_DE,
        lab_name="Black Forest Labs",
        source_url="https://bfl.ai/legal/imprint",
        note=(
            "Black Forest Labs' imprint names BFL GmbH and gives its registered "
            "address in Freiburg im Breisgau, Germany."
        ),
    ),
    # --- Israel ---
    "decart": ModelOrigin(
        country=PROVIDER_JURISDICTION_IL,
        lab_name="Decart",
        source_url="https://www.decart.ai/articles/sequoia-backed-decart-raises-21m-in-seed-funding",
        note=(
            "Decart's own company article identifies the lab that built its "
            "Lucy and Oasis models as an Israeli startup."
        ),
    ),
    # --- South Korea ---
    "upstage": ModelOrigin(
        country=PROVIDER_JURISDICTION_KR,
        lab_name="Upstage",
        source_url="https://www.upstage.ai/privacy-policy/updated-jun-01-2026",
        note=(
            "Upstage's privacy policy gives its company address in Gangnam-gu, "
            "Seoul, Republic of Korea."
        ),
    ),
    # --- China ---
    "qwen": _MODEL_ORIGIN_CN_ALIBABA,
    "Qwen": _MODEL_ORIGIN_CN_ALIBABA,
    "deepseek": _MODEL_ORIGIN_CN_DEEPSEEK,
    "deepseek-ai": _MODEL_ORIGIN_CN_DEEPSEEK,
    "z-ai": _MODEL_ORIGIN_CN_ZHIPU,
    "zai-org": _MODEL_ORIGIN_CN_ZHIPU,
    "minimax": _MODEL_ORIGIN_CN_MINIMAX,
    "minimaxai": _MODEL_ORIGIN_CN_MINIMAX,
    "MiniMaxAI": _MODEL_ORIGIN_CN_MINIMAX,
    "xiaomi": _MODEL_ORIGIN_CN_XIAOMI,
    "xiaomimimo": _MODEL_ORIGIN_CN_XIAOMI,
    "stepfun": ModelOrigin(
        country=PROVIDER_JURISDICTION_CN,
        lab_name="StepFun",
        source_url="https://www.stepfun.com/legal/terms",
        note=(
            "StepFun's terms name Shanghai StepFun Intelligent Technology Co., "
            "Ltd. as the company providing its models and services."
        ),
    ),
    "moonshotai": ModelOrigin(
        country=PROVIDER_JURISDICTION_CN,
        lab_name="Moonshot AI (Kimi)",
        source_url="https://www.moonshot.ai/about",
        note=(
            "Beijing Moonshot AI Technology Co., Ltd. gives its address as "
            "Haidian District, Beijing, China."
        ),
    ),
    "bytedance": ModelOrigin(
        country=PROVIDER_JURISDICTION_CN,
        lab_name="ByteDance (Seed, Doubao)",
        source_url="https://en.wikipedia.org/wiki/ByteDance",
        note=(
            "ByteDance is headquartered in Haidian, Beijing, China, while its "
            "associated entity ByteDance Ltd is incorporated in the Cayman "
            "Islands. ByteDance's own site lists offices in roughly 120 cities "
            "without naming a headquarters."
        ),
    ),
    "baidu": ModelOrigin(
        country=PROVIDER_JURISDICTION_CN,
        lab_name="Baidu (ERNIE)",
        source_url="https://www.sec.gov/Archives/edgar/data/1329099/000119312524068527/d584913d20f.htm",
        note=(
            "Baidu, Inc.'s Form 20-F gives principal executive offices at Baidu "
            "Campus, No. 10 Shangdi 10th Street, Haidian District, Beijing, PRC."
        ),
    ),
    "tencent": ModelOrigin(
        country=PROVIDER_JURISDICTION_CN,
        lab_name="Tencent (Hunyuan)",
        source_url=(
            "https://static.www.tencent.com/storage/uploads/2019/11/09/"
            "da62661e976ea6cf64551dc5cdf079ea.pdf"
        ),
        note=(
            "Tencent Holdings Limited's 2018 annual report gives its group head "
            "office as Tencent Binhai Towers, Nanshan District, Shenzhen, PRC, "
            "with a principal place of business in Hong Kong."
        ),
    ),
    "inclusionai": ModelOrigin(
        country=PROVIDER_JURISDICTION_CN,
        lab_name="Ant Group (inclusionAI: Ling, Ring)",
        source_url="https://huggingface.co/inclusionAI",
        note=(
            "The inclusionAI organisation describes itself as the home of Ant "
            "Group's AGI initiative, and Ant Group's own offices page gives its "
            "principal business office as Z Space, No. 556 Xixi Road, Hangzhou, "
            "China (https://www.antgroup.com/en/about/our-offices)."
        ),
    ),
    "kwaipilot": ModelOrigin(
        country=PROVIDER_JURISDICTION_CN,
        lab_name="Kuaishou (Kwaipilot KAT)",
        source_url="https://huggingface.co/Kwaipilot",
        note=(
            "The Kwaipilot organisation describes itself as the AI team from "
            "Kuaishou, whose head office is in Haidian District, Beijing, PRC "
            "per its HKEX annual report "
            "(https://www1.hkexnews.hk/listedco/listconews/sehk/2022/0419/2022041900053.pdf)."
        ),
    ),
    # --- No single creator country ---
    "sao10k": _MODEL_ORIGIN_INDEPENDENT_SAO10K,
    "Sao10K": _MODEL_ORIGIN_INDEPENDENT_SAO10K,
    "aion-labs": ModelOrigin(
        country=None,
        lab_name="Aion Labs",
        source_url="https://www.aionlabs.ai/docs/quickstart/",
        note=(
            "The official API documentation identifies Aion Labs as the model "
            "publisher but does not name a legal entity or country. It is not "
            "treated as the unrelated pharma venture studio at aionlabs.com."
        ),
    ),
    "thedrummer": ModelOrigin(
        country=None,
        lab_name="TheDrummer",
        source_url="https://huggingface.co/TheDrummer/models",
        note=(
            "The models are published by a pseudonymous Hugging Face account "
            "that does not disclose a legal entity or country."
        ),
    ),
    "lightning-ai": ModelOrigin(
        country=None,
        lab_name="Lightning AI (serving namespace)",
        source_url=None,
        note=(
            "Not a creator prefix: these ids are Lightning AI's hosted routes "
            "for weights built elsewhere — DeepSeek's V4 Pro, OpenAI's gpt-oss, "
            "NVIDIA's Nemotron — so origin varies per model and is read from "
            "those labs' rows instead."
        ),
    ),
    "cerebras": ModelOrigin(
        country=None,
        lab_name="Cerebras (serving namespace)",
        source_url=None,
        note=(
            "Not a creator prefix: these ids are Cerebras-served routes for "
            "weights built elsewhere — OpenAI's gpt-oss, Z.ai's GLM, Google's "
            "Gemma. Cerebras Systems Inc. is itself a US company with principal "
            "executive offices in Sunnyvale, California, which is recorded as "
            "the cerebras PROVIDER jurisdiction, not as a model origin."
        ),
    ),
}

# Vendor prefixes at or above this many catalog models must have a MODEL_ORIGINS
# row. Three is the smallest count that is a family rather than a one-off route:
# it keeps every lab whose weights appear as a series in the catalog under
# citation, without forcing a country onto single mirrored uploads whose
# publisher TrustedRouter has no primary source for. Prefixes below the
# threshold are welcome in the table and several are already there.
MODEL_ORIGIN_REQUIRED_MODEL_COUNT = 3


def model_origin_for_model_id(model_id: str) -> ModelOrigin | None:
    """Return the recorded origin of a model id's vendor prefix, if any."""
    prefix, _, rest = model_id.partition("/")
    if not rest:
        return None
    return MODEL_ORIGINS.get(prefix)
