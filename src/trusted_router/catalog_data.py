"""Static catalog data: hand-authored providers, model/endpoint dataclasses,
privacy-tier + orchestration constants, and default model orders.

Extracted from catalog.py (#38). Pure data + dataclasses — depends only on the
pricing types (PriceTier), never on the live MODELS/MODEL_ENDPOINTS registries
(built at import time in catalog.py from this data + the ingested snapshot).
catalog.py re-exports these names for backward compatibility.
"""

from __future__ import annotations

from dataclasses import dataclass
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

PROVIDER_JURISDICTION_US = "US"


@dataclass(frozen=True)
class ModelProviderPrivacyOverride:
    privacy_tier: int
    provider_zero_data_retention: bool | None = None
    provider_policy: str | None = None
    provider_policy_url: str | None = None


_MODEL_PROVIDER_PRIVACY_OVERRIDES: dict[tuple[str, str], ModelProviderPrivacyOverride] = {
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
    prepaid_available: bool = False
    byok_available: bool = True
    # Headline (low-tier) rates: what /v1/models displays. For
    # tier-aware billing, use `price_tiers` instead and pick the right
    # tier based on the actual prompt token count.
    prompt_price_microdollars_per_million_tokens: int = 0
    completion_price_microdollars_per_million_tokens: int = 0
    published_prompt_price_microdollars_per_million_tokens: int = 0
    published_completion_price_microdollars_per_million_tokens: int = 0
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
    prompt_price_microdollars_per_million_tokens: int = 0
    completion_price_microdollars_per_million_tokens: int = 0
    published_prompt_price_microdollars_per_million_tokens: int = 0
    published_completion_price_microdollars_per_million_tokens: int = 0
    price_tiers: tuple[PriceTier, ...] = ()
    published_price_tiers: tuple[PriceTier, ...] = ()

    @property
    def is_byok(self) -> bool:
        return self.usage_type.lower() == "byok"


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
        # Activation gate: flip only after the managed production key passes
        # the store=true/create then retrieve-must-fail ZDR smoke on/after the
        # contractual effective date. BYOK remains outside this flag.
        prepaid_zero_data_retention=False,
        prepaid_zero_data_retention_effective_on="2026-07-28",
        provider_policy=(
            "Contracted Zero Data Retention is scheduled to begin for "
            "TrustedRouter's managed OpenAI account on July 28, 2026. OpenAI remains "
            "outside trustedrouter/zdr until live activation verification passes. "
            "Once verified, the guarantee will apply only to TrustedRouter-funded "
            "prepaid routes; customer BYOK credentials use the data controls on the "
            "customer's own OpenAI organization or project."
        ),
        provider_policy_url="https://platform.openai.com/docs/models/default-usage-policies-by-endpoint",
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
        provider_policy=(
            "Not currently marked ZDR in TrustedRouter. Vertex AI documents a "
            "separate zero-data-retention configuration process, but this managed "
            "route stays outside trustedrouter/zdr until its live account settings "
            "are verified."
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
        provider_zero_data_retention=True,
        provider_policy=(
            "Tracked as provider-ZDR. Cerebras documents ZDR-compliant ephemeral "
            "prompt caching and no persisted prompt cache data."
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
            "Marked ZDR via TrustedRouter's arrangement — Together's ZDR is an "
            "opt-in account/privacy setting, NOT the public default, and the "
            "deployed Together account has it enabled. Together does not train "
            "on content without opt-in."
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
    ),
    # Phala (RedPill) — confidential AI inference inside Intel TDX
    # / NVIDIA Confidential Compute enclaves. Verified attestation,
    # end-to-end encrypted prompts. **On-brand for TR's trust story.**
    # OpenAI-compatible at api.red-pill.ai/v1.
    "phala": Provider(
        slug="phala",
        name="Phala",
        supports_prepaid=True,
        stores_content=False,
        provider_zero_data_retention=True,
        provider_confidential_compute=True,
        provider_e2ee=True,
        provider_policy=(
            "Tracked as a confidential AI provider with provider-side "
            "attestation and encrypted prompt transport."
        ),
        provider_policy_url="https://docs.phala.com/confidential-ai-inference/host-llm-in-tee",
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
    # Venice.AI — privacy-focused LLM gateway. No-logs, no-censoring
    # positioning. OpenAI-compatible at api.venice.ai/api/v1.
    "venice": Provider(
        slug="venice",
        name="Venice",
        supports_prepaid=True,
        stores_content=False,
        provider_zero_data_retention=True,
        provider_confidential_compute=True,
        provider_e2ee=True,
        provider_policy=(
            "Tracked as confidential — Venice documents no logging or storage of "
            "prompts/responses plus TEE-isolated, end-to-end-encrypted inference. "
            "(Caveat: requests Venice proxies to external frontier models inherit "
            "those providers' policies; TR routes Venice-native open models here.)"
        ),
        provider_policy_url="https://docs.venice.ai/overview/privacy",
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
    ),
    # Baseten — OpenAI-compatible Model APIs at inference.baseten.co/v1.
    # Public catalog + pricing is exposed from /v1/models; prompt/output
    # prices are dollars per token and are converted to integer microdollars
    # by scripts/pricing/providers/baseten.py.
    "baseten": Provider(
        slug="baseten",
        name="Baseten",
        supports_prepaid=True,
        provider_policy=(
            "No provider-ZDR claim is tracked here. Baseten's inference and "
            "security documentation are linked for users who need to review API "
            "data handling."
        ),
        provider_policy_url="https://docs.baseten.co/inference/overview",
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
    # The public /v1/models feed exposes model IDs and context windows, but not
    # prices. TR carries provider-native IDs in data/provider_models/makora.json
    # and sources prices from Makora's public homepage lineup where published.
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
    # DeepInfra — large open-weight catalog (Llama, Gemma 4, Qwen,
    # DeepSeek, etc.). OpenAI-compatible at api.deepinfra.com/v1/openai.
    # Pricing in the /v1/openai/models response under
    # metadata.pricing.{input_tokens,output_tokens} as USD per million.
    "deepinfra": Provider(
        slug="deepinfra",
        name="DeepInfra",
        supports_prepaid=True,
        stores_content=False,
        provider_zero_data_retention=True,
        provider_policy=(
            "Tracked as provider ZDR — DeepInfra documents memory-only handling "
            "with no storage of API content and no training on submitted API data. "
            "(Exception: requests to Google/Anthropic-backed models inherit those "
            "vendors' policies.)"
        ),
        provider_policy_url="https://docs.deepinfra.com/account/data-privacy",
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
    ),
    # Alibaba Cloud Model Studio / DashScope — workspace-scoped OpenAI-compatible
    # endpoint. The configured key is for an EU Central / Frankfurt MAAS
    # workspace, so provider-native model availability comes from that
    # workspace's /compatible-mode/v1/models response.
    "alibaba": Provider(
        slug="alibaba",
        name="Alibaba Cloud Model Studio",
        supports_prepaid=False,
        supports_byok=False,
        provider_policy=(
            "No provider-ZDR claim is tracked here. Alibaba Cloud Model Studio "
            "model availability and pricing are linked for users who need to "
            "review API data handling and regional deployment scope."
        ),
        provider_policy_url="https://www.alibabacloud.com/help/en/model-studio/model-pricing",
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
        "thinkingmachines",
        "wafer",
        "crusoe",
        "makora",
        "nebius",
        "minimax",
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

SOCRATES_1_0_MODEL_ID = "trustedrouter/socrates-1.0"

SOCRATES_1_1_MODEL_ID = "trustedrouter/socrates-1.1"

SOCRATES_MODEL_ID = "trustedrouter/socrates"

ADVISOR_MODEL_ID = "trustedrouter/advisor"

SUBAGENT_MODEL_ID = "trustedrouter/subagent"

ARISTOTLE_1_0_MODEL_ID = "trustedrouter/aristotle-1.0"

ARISTOTLE_1_1_MODEL_ID = "trustedrouter/aristotle-1.1"

ARISTOTLE_MODEL_ID = "trustedrouter/aristotle"

PLATO_1_0_MODEL_ID = "trustedrouter/plato-1.0"

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

OPEN_PATCHER_A1_MODEL_ID = "trustedrouter/openpatcher-a1"

OPEN_PATCHER_FAST1_MODEL_ID = "trustedrouter/openpatcher-fast1"

OPEN_PATCHER_G1_MODEL_ID = "trustedrouter/openpatcher-g1"

OPEN_PATCHER_G2_MODEL_ID = "trustedrouter/openpatcher-g2"

ATHENA_MODEL_ID = "trustedrouter/athena"

LIBERTY_1_0_MODEL_ID = "trustedrouter/liberty-1.0"

LIBERTY_1_0_1M_MODEL_ID = "trustedrouter/liberty-1.0-1m"

LIBERTY_2_0_MODEL_ID = "trustedrouter/liberty-2.0"

LIBERTY_3_0_MODEL_ID = "trustedrouter/liberty-3.0"

US_PROVIDER_ONLY_MODEL_IDS = frozenset(
    {
        OPEN_PATCHER_S1_MODEL_ID,
        OPEN_PATCHER_A1_MODEL_ID,
        OPEN_PATCHER_FAST1_MODEL_ID,
        OPEN_PATCHER_G1_MODEL_ID,
        ATHENA_MODEL_ID,
    }
)

SYNTH_MODEL_ID = "trustedrouter/synth"

IRIS_MODEL_ID = "trustedrouter/iris"

PROMETHEUS_MODEL_ID = "trustedrouter/prometheus"

ZEUS_MODEL_ID = "trustedrouter/zeus"

IRIS_1_0_MODEL_ID = "trustedrouter/iris-1.0"

IRIS_2_0_MODEL_ID = "trustedrouter/iris-2.0"

PROMETHEUS_1_0_MODEL_ID = "trustedrouter/prometheus-1.0"

PROMETHEUS_1_0_1M_MODEL_ID = "trustedrouter/prometheus-1.0-1m"

PROMETHEUS_2_0_MODEL_ID = "trustedrouter/prometheus-2.0"

ZEUS_1_0_MODEL_ID = "trustedrouter/zeus-1.0"

ZEUS_1_0_MINI_MODEL_ID = "trustedrouter/zeus-1.0-mini"

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
        SOCRATES_MODEL_ID,
        ADVISOR_MODEL_ID,
        SUBAGENT_MODEL_ID,
        ARISTOTLE_1_0_MODEL_ID,
        ARISTOTLE_1_1_MODEL_ID,
        ARISTOTLE_MODEL_ID,
        PLATO_1_0_MODEL_ID,
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
        OPEN_PATCHER_A1_MODEL_ID,
        OPEN_PATCHER_FAST1_MODEL_ID,
        OPEN_PATCHER_G1_MODEL_ID,
        OPEN_PATCHER_G2_MODEL_ID,
        ATHENA_MODEL_ID,
        LIBERTY_1_0_MODEL_ID,
        LIBERTY_1_0_1M_MODEL_ID,
        LIBERTY_2_0_MODEL_ID,
        LIBERTY_3_0_MODEL_ID,
        SYNTH_MODEL_ID,
        IRIS_MODEL_ID,
        PROMETHEUS_MODEL_ID,
        ZEUS_MODEL_ID,
        IRIS_1_0_MODEL_ID,
        IRIS_2_0_MODEL_ID,
        PROMETHEUS_1_0_MODEL_ID,
        PROMETHEUS_1_0_1M_MODEL_ID,
        PROMETHEUS_2_0_MODEL_ID,
        ZEUS_1_0_MODEL_ID,
        ZEUS_1_0_MINI_MODEL_ID,
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
    SOCRATES_MODEL_ID: SOCRATES_1_1_MODEL_ID,
    ARISTOTLE_MODEL_ID: ARISTOTLE_1_1_MODEL_ID,
    PLATO_MODEL_ID: PLATO_PRO_1_0_MODEL_ID,
    PLATO_PRO_MODEL_ID: PLATO_PRO_2_0_MODEL_ID,
    SOCRATES_PRO_MODEL_ID: SOCRATES_PRO_1_0_MODEL_ID,
    SOCRATES_PRO_PLUS_MODEL_ID: SOCRATES_PRO_PLUS_1_0_MODEL_ID,
    IRIS_MODEL_ID: IRIS_2_0_MODEL_ID,
    PROMETHEUS_MODEL_ID: PROMETHEUS_2_0_MODEL_ID,
    ZEUS_MODEL_ID: ZEUS_1_0_MODEL_ID,
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

DEFAULT_AUTO_MODEL_ORDER = [
    "anthropic/claude-opus-4.7",
    "anthropic/claude-sonnet-4.6",
    "openai/gpt-4.1-mini",
    "google/gemini-2.5-flash",
    "deepseek/deepseek-v4-flash",
    "minimax/minimax-m3",
    "moonshotai/kimi-k2.6",
    "mistralai/mistral-small-2603",
    "z-ai/glm-4.6",
]

SYNTH_BUDGET_MODEL_ORDER = (
    "minimax/minimax-m3",
    "moonshotai/kimi-k2.6",
    "deepseek/deepseek-v4-pro",
)

SYNTH_IRIS_2_MODEL_ORDER = (
    "minimax/minimax-m3",
    "moonshotai/kimi-k3",
    "deepseek/deepseek-v4-pro",
)

SYNTH_QUALITY_MODEL_ORDER = (
    "minimax/minimax-m3",
    "moonshotai/kimi-k2.6",
    "z-ai/glm-5.2",
    "google/gemma-4-31b-it",
    "deepseek/deepseek-v4-pro",
)

SYNTH_QUALITY_1M_MODEL_ORDER = (
    "minimax/minimax-m3",
    "xiaomi/mimo-v2.5-pro",
    "z-ai/glm-5.2",
    "deepseek/deepseek-v4-pro",
)

SYNTH_PROMETHEUS_2_MODEL_ORDER = (
    "minimax/minimax-m3",
    "moonshotai/kimi-k3",
    "z-ai/glm-5.2",
    "deepseek/deepseek-v4-pro",
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

SYNTH_FRONTIER_MODEL_ORDER = (
    "anthropic/claude-opus-4.8",
    "openai/gpt-5.5",
    "google/gemini-3.1-pro-preview",
    "google/gemini-3.5-flash",
    "minimax/minimax-m3",
    "z-ai/glm-5.2",
    "xiaomi/mimo-v2.5-pro",
    "deepseek/deepseek-v4-pro",
)

SYNTH_FRONTIER_MINI_MODEL_ORDER = (
    "google/gemini-3.1-pro-preview",
    "google/gemini-3.5-flash",
    "minimax/minimax-m3",
    "z-ai/glm-5.2",
    "xiaomi/mimo-v2.5-pro",
    "deepseek/deepseek-v4-pro",
)

SYNTH_CODE_BUDGET_MODEL_ORDER = (
    "minimax/minimax-m3",
    "moonshotai/kimi-k2.7-code",
    "deepseek/deepseek-v4-pro",
)

SYNTH_CODE_QUALITY_MODEL_ORDER = (
    "minimax/minimax-m3",
    "moonshotai/kimi-k2.7-code",
    "z-ai/glm-5.2",
    "google/gemma-4-31b-it",
    "deepseek/deepseek-v4-pro",
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
    SOCRATES_MODEL_ID: SOCRATES_1_1_CATALOG_MODEL_ORDER,
    ADVISOR_MODEL_ID: SOCRATES_CATALOG_MODEL_ORDER,
    SUBAGENT_MODEL_ID: (
        "deepseek/deepseek-v4-flash",
        "cerebras/gpt-oss-120b",
        "anthropic/claude-sonnet-5",
    ),
    ARISTOTLE_1_0_MODEL_ID: (
        "deepseek/deepseek-v4-flash",
        *SYNTH_FRONTIER_MODEL_ORDER,
    ),
    ARISTOTLE_1_1_MODEL_ID: (
        "z-ai/glm-5.2-fast",
        "z-ai/glm-5.2",
        ZEUS_1_0_MODEL_ID,
    ),
    ARISTOTLE_MODEL_ID: (
        "z-ai/glm-5.2-fast",
        "z-ai/glm-5.2",
        ZEUS_1_0_MODEL_ID,
    ),
    PLATO_1_0_MODEL_ID: (
        "deepseek/deepseek-v4-flash",
        "z-ai/glm-5.2",
        *SYNTH_QUALITY_MODEL_ORDER,
    ),
    PLATO_MODEL_ID: (
        "z-ai/glm-5.2",
        PROMETHEUS_1_0_1M_MODEL_ID,
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
    ATHENA_MODEL_ID: (
        "z-ai/glm-5.2-fast",
        "z-ai/glm-5.2",
        ZEUS_1_0_MINI_MODEL_ID,
        "moonshotai/kimi-k2.7-code",
        "moonshotai/kimi-k2.6",
    ),
    LIBERTY_2_0_MODEL_ID: (
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
    "cerebras": frozenset(
        {
            "openai/gpt-oss-120b",
            "cerebras/gpt-oss-120b",
            "z-ai/glm-4.7",
            "cerebras/zai-glm-4.7",
        }
    ),
    # 2026-07-18: GMI's /models listing is aspirational — 7d synthetic probes
    # show exactly four models served on our account (590-670 successes each)
    # while the other ~45 listed models have ZERO successes ever (uniform
    # upstream 404 "No matching target server found"). Route Credits traffic
    # only to the verified set; BYOK stays visible (customer accounts may
    # differ). A new GMI model earns its way in via probe successes.
    "gmi": frozenset(
        {
            "deepseek/deepseek-v4-pro",
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
            # 2026-07-18: GMI's authenticated /models feed still advertises
            # Kimi K3, but direct prepaid inference returns 404 "No matching
            # target server found". Keep BYOK visible for accounts with
            # different capacity while removing the broken operator route.
            "moonshotai/kimi-k3",
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
    "together": frozenset(
        {
            "meta-llama/llama-3.1-8b-instruct",
            "meta-llama/llama-3.1-70b-instruct",
            # 2026-07-15: the snapshot route returns 404 when pinned to
            # Together. Keep the directly verified Baseten route.
            "nvidia/nemotron-3-ultra-550b-a55b",
            "qwen/qwen-2.5-72b-instruct",
        }
    ),
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

_PROVIDER_DISPLAY_ORDER = ("tinfoil", "venice")


# Legacy compatibility aliases (advisor/synth primitives) — completes
# ORCHESTRATION_PRIMITIVE_BY_MODEL_ID within this module so a direct import
# sees the full mapping, not just the base entries (codex #101).
for _advisor_model_id in (
    SOCRATES_1_0_MODEL_ID,
    SOCRATES_1_1_MODEL_ID,
    SOCRATES_MODEL_ID,
    ARISTOTLE_1_0_MODEL_ID,
    ARISTOTLE_1_1_MODEL_ID,
    ARISTOTLE_MODEL_ID,
    PLATO_1_0_MODEL_ID,
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
    ATHENA_MODEL_ID,
    LIBERTY_2_0_MODEL_ID,
    LIBERTY_3_0_MODEL_ID,
):
    ORCHESTRATION_PRIMITIVE_BY_MODEL_ID[_advisor_model_id] = "advisor"

for _synth_model_id in (
    IRIS_MODEL_ID,
    PROMETHEUS_MODEL_ID,
    ZEUS_MODEL_ID,
    IRIS_1_0_MODEL_ID,
    IRIS_2_0_MODEL_ID,
    PROMETHEUS_1_0_MODEL_ID,
    PROMETHEUS_1_0_1M_MODEL_ID,
    PROMETHEUS_2_0_MODEL_ID,
    ZEUS_1_0_MODEL_ID,
    ZEUS_1_0_MINI_MODEL_ID,
    IRIS_CODE_MODEL_ID,
    PROMETHEUS_CODE_MODEL_ID,
    ZEUS_CODE_MODEL_ID,
    IRIS_CODE_1_0_MODEL_ID,
    PROMETHEUS_CODE_1_0_MODEL_ID,
    ZEUS_CODE_1_0_MODEL_ID,
    OPEN_PATCHER_S1_MODEL_ID,
    OPEN_PATCHER_S2_MODEL_ID,
    LIBERTY_1_0_MODEL_ID,
    LIBERTY_1_0_1M_MODEL_ID,
):
    ORCHESTRATION_PRIMITIVE_BY_MODEL_ID[_synth_model_id] = "synth"
