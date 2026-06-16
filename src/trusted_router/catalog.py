from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, TypedDict

from trusted_router.money import (
    MICRODOLLARS_PER_DOLLAR,
    TOKENS_PER_MILLION,
    dollars_to_microdollars,
    microdollars_per_million_tokens_to_token_decimal,
)


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
    provider_confidential_compute: bool | None = None
    provider_e2ee: bool | None = None
    provider_policy: str = (
        "No public zero-retention, confidential-compute, or provider-side "
        "end-to-end-encryption claim is tracked yet."
    )
    provider_policy_url: str | None = None


# Privacy-posture tiers, lowest → highest. A request can demand a minimum
# tier (provider.min_privacy in the routing prefs); the router then only
# considers providers whose posture clears that bar. The tiers are nested:
# every confidential provider is also zero-retention; every zero-retention
# provider is also no-store.
PRIVACY_TIER_STANDARD = 0   # no tracked posture (would store content)
PRIVACY_TIER_NO_STORE = 1   # does not store request/response content
PRIVACY_TIER_ZERO_RETENTION = 2  # contractual / policy zero data retention
PRIVACY_TIER_CONFIDENTIAL = 3    # confidential compute + provider-side e2ee

# Friendly names a client can pass as provider.min_privacy. All map to a
# minimum tier rank above. "standard"/"any" is the default (no filter).
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


def provider_privacy_tier(provider: Provider) -> int:
    """The highest privacy bar a provider clears. Used to enforce a
    request's minimum-privacy routing preference. Note the TR gateway hop
    is always attested regardless of tier — this rank is about the
    UPSTREAM provider's posture, which is what varies."""
    if provider.provider_confidential_compute and provider.provider_e2ee:
        return PRIVACY_TIER_CONFIDENTIAL
    if provider.provider_zero_data_retention:
        return PRIVACY_TIER_ZERO_RETENTION
    if provider.stores_content is False:
        return PRIVACY_TIER_NO_STORE
    return PRIVACY_TIER_STANDARD


@dataclass(frozen=True)
class PriceTier:
    """One tier of context-conditional pricing. A request whose prompt
    token count is ≤ `max_prompt_tokens` uses this tier's rates. The
    LAST tier in `Model.price_tiers` MUST have `max_prompt_tokens=None`
    (uncapped fallback). Most models have exactly one tier.

    Both prompt and completion rates live on the tier — Gemini-Pro-shape
    pricing flips both rates when context crosses 200k tokens.

    `prompt_cached_*` is the discounted rate for prompt tokens that
    upstream reports as cache hits. None ⇒ upstream charges the same
    rate cached or not (rare; most providers offer a cache discount).
    Per-token billing splits the prompt into (uncached × full rate) +
    (cached × cached rate); see `cost_microdollars` in routes/helpers.
    """

    max_prompt_tokens: int | None
    prompt_price_microdollars_per_million_tokens: int
    completion_price_microdollars_per_million_tokens: int
    prompt_cached_price_microdollars_per_million_tokens: int | None = None


def _flat_tier(
    prompt: int,
    completion: int,
    prompt_cached: int | None = None,
) -> tuple[PriceTier, ...]:
    """Construct a length-1 tier tuple (the common case)."""
    return (
        PriceTier(
            max_prompt_tokens=None,
            prompt_price_microdollars_per_million_tokens=prompt,
            completion_price_microdollars_per_million_tokens=completion,
            prompt_cached_price_microdollars_per_million_tokens=prompt_cached,
        ),
    )


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


def select_price_tier(tiers: tuple[PriceTier, ...], prompt_tokens: int) -> PriceTier:
    """Pick the tier that applies to a request with `prompt_tokens` of
    input. Walks the tiers in order; returns the first one whose
    threshold accommodates the prompt size. The last tier always has
    max_prompt_tokens=None and is the catch-all.

    Used by the billing path to compute actual cost. For models with
    a single uncapped tier (the common case), this returns that tier
    regardless of `prompt_tokens`.
    """
    for tier in tiers:
        if tier.max_prompt_tokens is None or prompt_tokens <= tier.max_prompt_tokens:
            return tier
    # Should be unreachable — the last tier always matches due to
    # max_prompt_tokens=None — but defend against malformed catalog data.
    return tiers[-1]


class ModelPricingKwargs(TypedDict):
    prompt_price_microdollars_per_million_tokens: int
    completion_price_microdollars_per_million_tokens: int
    published_prompt_price_microdollars_per_million_tokens: int
    published_completion_price_microdollars_per_million_tokens: int


# Uniform pricing: customer pays cost + 10%, floor $0.01/M tokens. Same
# value goes into both `prompt_price_*` and `published_*` — TR no longer
# runs the 1¢/M "discount theater". The floor catches free upstream tiers
# so the catalog never advertises $0/M to end users; $0.01/M is ~10×
# margin over real per-request infra cost (~$0.00001/req on a typical
# 10K-token call), recovered via 10K-tokens × $0.01/M = $0.0001/req.
_PRICE_MARKUP_RATIO = Decimal("1.10")
_PRICE_FLOOR_MICRODOLLARS_PER_M = 10_000  # $0.01 per million tokens.


def _customer_price(cost_microdollars_per_million: int) -> int:
    """Apply the markup formula. Input/output in microdollars per million tokens."""
    marked_up = int(
        (Decimal(cost_microdollars_per_million) * _PRICE_MARKUP_RATIO).to_integral_value()
    )
    return max(marked_up, _PRICE_FLOOR_MICRODOLLARS_PER_M)


# ---------------------------------------------------------------------------
# Prompt-cache pricing
#
# The attested gateway reports cache_read_input_tokens /
# cache_creation_input_tokens at settle. Cached tokens are billed as a
# multiple of the endpoint's (already marked-up) prompt price, so the
# uniform x1.10 margin structure is preserved: provider charges
# cost x multiplier, we bill customer_price x multiplier.
#
# Multipliers mirror published provider pricing as of 2026-06:
#   anthropic: cache read 0.1x, 5-minute cache write 1.25x
#              (docs.anthropic.com/en/docs/build-with-claude/prompt-caching)
#   openai:    cached input 0.5x for the gpt-4o family; newer families
#              are CHEAPER (0.1x), so 0.5x is the safe-side bound that
#              never bills below our upstream cost. Tighten per-model
#              when the pricing scrapers track cached rates.
#   gemini/vertex: cached content 0.25x.
# Providers absent from the read table get NO discount (1.0x) until
# their cached pricing is verified — overcharging a discount we can't
# confirm is the safe failure mode for margin, and those providers
# rarely report cache fields anyway. Only Anthropic reports cache
# WRITES; the 1.25x default keeps any future writer safe-side too.
_CACHE_READ_PRICE_MULTIPLIER: dict[str, Decimal] = {
    "anthropic": Decimal("0.1"),
    "openai": Decimal("0.5"),
    "gemini": Decimal("0.25"),
    "vertex": Decimal("0.25"),
}
_CACHE_WRITE_PRICE_MULTIPLIER: dict[str, Decimal] = {
    "anthropic": Decimal("1.25"),
}
_DEFAULT_CACHE_READ_MULTIPLIER = Decimal("1")
_DEFAULT_CACHE_WRITE_MULTIPLIER = Decimal("1.25")


def cache_token_prices_microdollars(provider: str, prompt_price_microdollars: int) -> tuple[int, int]:
    """(cache-read, cache-write) customer price in microdollars per million
    tokens for one endpoint's prompt price."""
    prompt = Decimal(prompt_price_microdollars)
    read = _CACHE_READ_PRICE_MULTIPLIER.get(provider, _DEFAULT_CACHE_READ_MULTIPLIER)
    write = _CACHE_WRITE_PRICE_MULTIPLIER.get(provider, _DEFAULT_CACHE_WRITE_MULTIPLIER)
    return (
        int((prompt * read).to_integral_value()),
        int((prompt * write).to_integral_value()),
    )


def _priced(cost_dollars_per_million: str | int | float) -> tuple[int, int, int]:
    """Return (prompt_price, published_price, cost_microdollars) for a
    dollars-per-million cost. prompt_price == published_price under the
    uniform formula; cost is preserved as a third value for any consumer
    that wants the upstream-paid amount (e.g. the per-endpoint detail page)."""
    cost = dollars_to_microdollars(cost_dollars_per_million)
    customer = _customer_price(cost)
    return customer, customer, cost


def _customer_price_from_dollars_per_token(price_per_token: str) -> tuple[int, int, int]:
    """Variant for snapshot-shaped inputs (dollars/token strings).
    Returns the same triple as `_priced`."""
    if not price_per_token:
        return _PRICE_FLOOR_MICRODOLLARS_PER_M, _PRICE_FLOOR_MICRODOLLARS_PER_M, 0
    try:
        per_token = Decimal(str(price_per_token))
    except (InvalidOperation, ValueError):
        # Malformed snapshot rows are pinned to the price floor — better
        # to advertise $0.01/M than to crash module import or expose $0.
        return _PRICE_FLOOR_MICRODOLLARS_PER_M, _PRICE_FLOOR_MICRODOLLARS_PER_M, 0
    cost = int((per_token * MICRODOLLARS_PER_DOLLAR * TOKENS_PER_MILLION).to_integral_value())
    customer = _customer_price(cost)
    return customer, customer, cost


def _read_pricing_tiers(pricing: dict[str, Any], dimension: str) -> tuple[PriceTier, ...] | None:
    """Read `pricing.prompt_tiers` / `pricing.completion_tiers` arrays
    from the snapshot. Returns None if the snapshot has only flat
    pricing for this model — caller should construct a single-tier
    list from the headline rate in that case.

    Tier shape in the snapshot:
        prompt_tiers:     [{"max_prompt_tokens": int|None,
                            "prompt": "$/tok",
                            "input_cache_read": "$/tok"  # optional}]
        completion_tiers: [{"max_prompt_tokens": int|None, "completion": "$/tok"}]

    Both arrays have the same length and same `max_prompt_tokens`
    sequence. Returned PriceTier objects pair them up; cached prompt
    rate is parsed from `input_cache_read` (matches OR's convention).
    """
    raw_prompt = pricing.get("prompt_tiers")
    raw_completion = pricing.get("completion_tiers")
    if not isinstance(raw_prompt, list) or not isinstance(raw_completion, list):
        return None
    if not raw_prompt or len(raw_prompt) != len(raw_completion):
        return None
    tiers: list[PriceTier] = []
    for prompt_tier, completion_tier in zip(raw_prompt, raw_completion, strict=False):
        if not isinstance(prompt_tier, dict) or not isinstance(completion_tier, dict):
            return None
        threshold = prompt_tier.get("max_prompt_tokens")
        if threshold is not None and not isinstance(threshold, int):
            return None
        prompt_per_token = str(prompt_tier.get("prompt") or "")
        completion_per_token = str(completion_tier.get("completion") or "")
        prompt_micro, _pub, _cost = _customer_price_from_dollars_per_token(prompt_per_token)
        completion_micro, _pub2, _cost2 = _customer_price_from_dollars_per_token(
            completion_per_token
        )
        cached_micro: int | None = None
        cache_read = prompt_tier.get("input_cache_read")
        if cache_read:
            cached_micro, _pub3, _cost3 = _customer_price_from_dollars_per_token(str(cache_read))
        tiers.append(
            PriceTier(
                max_prompt_tokens=threshold,
                prompt_price_microdollars_per_million_tokens=prompt_micro,
                completion_price_microdollars_per_million_tokens=completion_micro,
                prompt_cached_price_microdollars_per_million_tokens=cached_micro,
            )
        )
    if tiers[-1].max_prompt_tokens is not None:
        # Snapshot data is malformed — last tier should be uncapped.
        # Return None so caller falls back to the headline rate.
        return None
    return tuple(tiers)


PROVIDERS: dict[str, Provider] = {
    "trustedrouter": Provider(
        slug="trustedrouter",
        name="TrustedRouter",
        supports_messages=True,
        supports_embeddings=False,
        supports_prepaid=True,
        supports_byok=True,
        stores_content=False,
        provider_zero_data_retention=True,
        provider_confidential_compute=True,
        provider_e2ee=True,
        provider_policy=(
            "TrustedRouter's attested gateway stores no prompt or output content. "
            "Provider compute policy still depends on the selected upstream route."
        ),
        provider_policy_url="https://trust.trustedrouter.com",
    ),
    "anthropic": Provider(
        slug="anthropic",
        name="Anthropic",
        supports_messages=True,
        supports_prepaid=True,
        provider_zero_data_retention=True,
        provider_policy=(
            "Marked ZDR via TrustedRouter's arrangement — zero retention is NOT "
            "Anthropic's public default; it applies to contracted / approved API "
            "usage, which TrustedRouter's deployed account is configured for. "
            "Anthropic does not train on API content. (Flagged content may be "
            "retained longer for Usage-Policy enforcement; non-Messages features "
            "may differ.)"
        ),
        provider_policy_url="https://platform.claude.com/docs/en/api/data-retention",
    ),
    "openai": Provider(
        slug="openai",
        name="OpenAI",
        supports_embeddings=True,
        supports_prepaid=True,
        provider_zero_data_retention=True,
        provider_policy=(
            "Marked ZDR for TrustedRouter's configured OpenAI / ChatGPT API account. "
            "This is not the public OpenAI API default for every account or endpoint; "
            "ZDR depends on eligible endpoint usage and account configuration."
        ),
        provider_policy_url="https://platform.openai.com/docs/models/default-usage-policies-by-endpoint",
    ),
    "gemini": Provider(
        slug="gemini",
        name="Gemini",
        supports_embeddings=True,
        supports_prepaid=True,
        provider_zero_data_retention=True,
        provider_policy=(
            "Marked ZDR for TrustedRouter's Google Gemini / Vertex generative-AI "
            "routes under Google's data-governance commitments for customer prompts "
            "and outputs."
        ),
        provider_policy_url="https://docs.cloud.google.com/vertex-ai/generative-ai/docs/data-governance",
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
            "https://cdn.deepseek.com/policies/en-US/deepseek-privacy-policy.html"
            "?locale=en_US"
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
    ),
    # xAI Grok — OpenAI-compatible chat completions at api.x.ai/v1.
    # As of 2026-05, headline model is grok-4.3 ($1.25/$2.50 per M).
    "grok": Provider(
        slug="grok",
        name="xAI Grok",
        supports_prepaid=True,
        provider_policy=(
            "xAI documents no training on API requests and 30-day default audit "
            "retention, with ZDR as an enterprise feature."
        ),
        provider_policy_url="https://docs.x.ai/docs/resources/faq-api/security",
    ),
    # Novita — multi-model serverless inference. OpenAI-compatible
    # at api.novita.ai/v3/openai. Hosts DeepSeek, Qwen, Llama,
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
        provider_policy=(
            "Parasail documents no input logging/storage for serverless and dedicated "
            "service paths, with different handling for batch service."
        ),
        provider_policy_url=(
            "https://docs.parasail.io/parasail-docs/security-and-account-management/"
            "data-privacy-retention"
        ),
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
    ),
}
# Vertex is intentionally excluded until TR's GCP project gets the
# Anthropic-on-Vertex / Gemini-on-Vertex quota approvals.

# Providers with a direct prepaid implementation in the attested
# quill-cloud-proxy llm_multi gateway. BYOK endpoints may exist for any
# keyed provider, but Credits endpoints must stay in sync with this set so
# the control plane cannot authorize a prepaid route the enclave cannot
# dispatch.
GATEWAY_PREPAID_PROVIDER_SLUGS = frozenset(
    {
        "anthropic",
        "openai",
        "gemini",
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
        # enclave's QUILL_PHALA_SECRET default + AWS bootstrap_server
        # now point at the confidential secret; model ids ship as
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
        "nebius",
        "minimax",
        # Cohere — embeddings only for now (native /v2/embed in the enclave).
        "cohere",
        # Voyage — embeddings only (OpenAI-shaped /v1/embeddings in the enclave).
        "voyage",
        # Xiaomi MiMo — OpenAI-compatible chat (api.xiaomimimo.com/v1).
        "xiaomi",
    }
)


AUTO_MODEL_ID = "trustedrouter/auto"
FREE_MODEL_ID = "trustedrouter/free"
CHEAP_MODEL_ID = "trustedrouter/cheap"
EU_MODEL_ID = "trustedrouter/eu"
ZDR_MODEL_ID = "trustedrouter/zdr"
E2E_MODEL_ID = "trustedrouter/e2e"
MONITOR_MODEL_ID = "trustedrouter/monitor"
FUSION_MODEL_ID = "trustedrouter/fusion"
META_MODEL_IDS = frozenset(
    {
        AUTO_MODEL_ID,
        FREE_MODEL_ID,
        CHEAP_MODEL_ID,
        EU_MODEL_ID,
        ZDR_MODEL_ID,
        E2E_MODEL_ID,
        MONITOR_MODEL_ID,
        FUSION_MODEL_ID,
    }
)

# EU-focused routing is a provider policy, not a hard data-residency promise.
# It keeps traffic on the EU regional attested gateway when the caller uses
# that base URL, then prefers European / EU-regionable / privacy-forward
# upstreams. Customers needing contractual residency should still pin a
# provider allowlist in their agreement and request body.
EU_FOCUSED_PROVIDER_ORDER: tuple[str, ...] = (
    "mistral",
    "gemini",
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
# IDs follow snapshot naming exactly. The picks span the 8 keyed
# providers so `trustedrouter/auto` rolls over across providers if any
# one is down. Each entry must have a provider-direct price in the
# snapshot — OR-only models can no longer reach the catalog (see
# scripts/pricing/refresh.py:_merge_snapshot).
#
# 2026-06 update: OpenAI's GPT-5.4 line (incl. gpt-5.4-nano) and the "-pro"
# tiers 502 on our key — verified via the gateway probe; see
# _PROVIDER_UNSERVED_CREDITS_MODELS. Route auto/monitor callers to
# openai/gpt-4.1-mini, which is served (verified OK) and is the current cheap
# mid-tier model. (gpt-5.5 works too but is the pricey flagship.)
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


# Catalog seed — only TR's Auto meta-model is hand-coded. Every other
# entry comes from `_INGESTED_MODELS` below, which is built from
# `data/openrouter_snapshot.json`. That guarantees pricing is uniformly
# `cost × 1.10, $0.01/M floor` (per the formula), and that the catalog
# lists every model from every provider TR has a key for — no
# hand-curated subset to drift out of sync with reality.
MODELS: dict[str, Model] = {
    AUTO_MODEL_ID: Model(
        id=AUTO_MODEL_ID,
        name="TrustedRouter Auto",
        provider="trustedrouter",
        context_length=200_000,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
        prompt_price_microdollars_per_million_tokens=0,
        completion_price_microdollars_per_million_tokens=0,
        published_prompt_price_microdollars_per_million_tokens=0,
        published_completion_price_microdollars_per_million_tokens=0,
    ),
    FREE_MODEL_ID: Model(
        id=FREE_MODEL_ID,
        name="TrustedRouter Free",
        provider="trustedrouter",
        context_length=128_000,
        supports_messages=False,
        prepaid_available=True,
        byok_available=False,
    ),
    CHEAP_MODEL_ID: Model(
        id=CHEAP_MODEL_ID,
        name="TrustedRouter Cheap",
        provider="trustedrouter",
        context_length=128_000,
        supports_messages=False,
        prepaid_available=True,
        byok_available=False,
    ),
    EU_MODEL_ID: Model(
        id=EU_MODEL_ID,
        name="TrustedRouter EU",
        provider="trustedrouter",
        context_length=200_000,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
    ),
    ZDR_MODEL_ID: Model(
        id=ZDR_MODEL_ID,
        name="TrustedRouter ZDR",
        provider="trustedrouter",
        context_length=200_000,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
    ),
    E2E_MODEL_ID: Model(
        id=E2E_MODEL_ID,
        name="TrustedRouter E2E",
        provider="trustedrouter",
        context_length=128_000,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
    ),
    MONITOR_MODEL_ID: Model(
        id=MONITOR_MODEL_ID,
        name="TrustedRouter Monitor",
        provider="trustedrouter",
        context_length=128_000,
        supports_messages=False,
        prepaid_available=True,
        byok_available=False,
    ),
    FUSION_MODEL_ID: Model(
        id=FUSION_MODEL_ID,
        name="TrustedRouter Fusion",
        provider="trustedrouter",
        context_length=200_000,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
    ),
}


def _endpoint(
    model: Model,
    *,
    usage_type: str,
    provider: str | None = None,
    upstream_id: str | None = None,
) -> ModelEndpoint:
    provider_slug = provider or model.provider
    suffix = "byok" if usage_type.lower() == "byok" else "prepaid"
    return ModelEndpoint(
        id=f"{model.id}@{provider_slug}/{suffix}",
        model_id=model.id,
        provider=provider_slug,
        usage_type="BYOK" if usage_type.lower() == "byok" else "Credits",
        upstream_id=upstream_id or model.upstream_id,
        prompt_price_microdollars_per_million_tokens=model.prompt_price_microdollars_per_million_tokens,
        completion_price_microdollars_per_million_tokens=model.completion_price_microdollars_per_million_tokens,
        published_prompt_price_microdollars_per_million_tokens=model.published_prompt_price_microdollars_per_million_tokens,
        published_completion_price_microdollars_per_million_tokens=model.published_completion_price_microdollars_per_million_tokens,
    )


def _build_endpoints(models: dict[str, Model]) -> dict[str, ModelEndpoint]:
    endpoints: dict[str, ModelEndpoint] = {}
    for model in models.values():
        if model.id in META_MODEL_IDS:
            continue
        provider = PROVIDERS[model.provider]
        if model.prepaid_available and provider.slug in GATEWAY_PREPAID_PROVIDER_SLUGS:
            endpoint = _endpoint(model, usage_type="Credits")
            endpoints[endpoint.id] = endpoint
        if model.byok_available and provider.supports_byok:
            endpoint = _endpoint(model, usage_type="BYOK")
            endpoints[endpoint.id] = endpoint
    return endpoints


# Folder where the OpenRouter ingest snapshot lives. Bundled into the
# wheel so production reads from disk; refreshed by
# `scripts/ingest_openrouter_catalog.py` and committed via PR.
_INGEST_PATH = Path(__file__).parent / "data" / "openrouter_snapshot.json"
_PROVIDER_MODELS_DIR = Path(__file__).parent / "data" / "provider_models"

# OpenRouter publishes models as `{author}/{slug}` where author maps onto
# one of TR's keyed providers. This drops the `Model.provider` (publisher)
# field for an ingested entry.
_AUTHOR_TO_PROVIDER_SLUG: dict[str, str] = {
    "anthropic": "anthropic",
    "openai": "openai",
    "google": "gemini",
    "cerebras": "cerebras",
    "deepseek": "deepseek",
    "mistral": "mistral",
    "mistralai": "mistral",
    "moonshot": "kimi",
    "moonshotai": "kimi",
    "z-ai": "zai",
    "zhipu": "zai",
    "zhipuai": "zai",
    "fireworks": "fireworks",
    "x-ai": "grok",
    "xai": "grok",
    "phala": "phala",
    # Keep Meta Llama's primary TR route on Cerebras even when the
    # OpenRouter endpoint snapshot temporarily exposes only a different
    # host. Cerebras is one of TR's direct prepaid/BYOK providers and
    # the gateway can call this upstream model id directly.
    "meta-llama": "cerebras",
    # `qwen/*`, `minimax/*` etc. fall back to whichever endpoint
    # provider serves them — Novita / SiliconFlow and others host
    # open-weight variants, and the endpoint provider determines which
    # TR-keyed provider answers the call.
}


_PROVIDER_DEPRECATED_UPSTREAM_MODELS: dict[str, frozenset[str]] = {
    # Fable was briefly added as an Anthropic supplement, then blocked. It is
    # also not tracked as a ZDR-capable route, so keep it out of the public
    # catalog and authorization path even if a future snapshot carries it.
    "anthropic": frozenset({"anthropic/claude-fable-5"}),
    # Nebius notified customers that these Token Factory model APIs / UI
    # entries will be disabled on 2026-06-22. This is provider-scoped:
    # equivalent model families on MiniMax, Kimi, Z.AI, Cerebras, etc. remain
    # routable if those providers still serve them. Drop both prepaid and BYOK
    # Nebius endpoints because the upstream model API itself is going away.
    "nebius": frozenset(
        {
            "deepseek-ai/DeepSeek-V3.2",
            "deepseek-ai/DeepSeek-V3.2-fast",
            "MiniMaxAI/MiniMax-M2.5-fast",
            "moonshotai/Kimi-K2.5",
            "moonshotai/Kimi-K2.5-fast",
            "openai/gpt-oss-120b-fast",
            "PrimeIntellect/INTELLECT-3",
            "Qwen/Qwen3-235B-A22B-Thinking-2507-fast",
            "Qwen/Qwen3-Next-80B-A3B-Thinking-fast",
            "Qwen/Qwen3.5-397B-A17B-fast",
            "zai-org/GLM-5",
        }
    ),
}


def _is_provider_deprecated_model(
    provider_slug: str,
    model_id: str,
    upstream_id: str | None,
) -> bool:
    deprecated = _PROVIDER_DEPRECATED_UPSTREAM_MODELS.get(provider_slug)
    if not deprecated:
        return False
    return model_id in deprecated or (upstream_id is not None and upstream_id in deprecated)


def _author_provider(model_id: str, endpoints: list[dict[str, Any]]) -> str | None:
    author = model_id.split("/", 1)[0].lower()
    if author in _AUTHOR_TO_PROVIDER_SLUG:
        return _AUTHOR_TO_PROVIDER_SLUG[author]
    if endpoints:
        slug = endpoints[0].get("tr_provider_slug")
        if isinstance(slug, str) and slug in PROVIDERS:
            return slug
    return None


def _ingested_models_and_endpoints() -> tuple[dict[str, Model], dict[str, ModelEndpoint]]:
    """Read the OpenRouter snapshot and return (models, endpoints) dicts.
    Pricing is run through `_customer_price_from_dollars_per_token` so the
    catalog uniformly applies the cost+10% / $0.01/M-floor formula."""
    if not _INGEST_PATH.exists():
        return {}, {}
    snapshot = json.loads(_INGEST_PATH.read_text(encoding="utf-8"))
    raw_models = snapshot.get("models")
    if not isinstance(raw_models, list):
        return {}, {}

    models: dict[str, Model] = {}
    endpoints: dict[str, ModelEndpoint] = {}

    for raw_model in raw_models:
        model_id = raw_model.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        raw_endpoints = [e for e in (raw_model.get("endpoints") or []) if isinstance(e, dict)]
        if not raw_endpoints:
            continue
        publisher = _author_provider(model_id, raw_endpoints)
        if publisher is None:
            continue

        per_endpoint_prices: list[tuple[int, int, tuple[PriceTier, ...], str, dict[str, Any]]] = []
        for raw_ep in raw_endpoints:
            slug = raw_ep.get("tr_provider_slug")
            if not isinstance(slug, str) or slug not in PROVIDERS:
                continue
            upstream_id = str(raw_ep.get("model_id") or model_id)
            if _is_provider_deprecated_model(slug, model_id, upstream_id):
                continue
            pricing = raw_ep.get("pricing") or {}
            prompt_price, _, _ = _customer_price_from_dollars_per_token(
                str(pricing.get("prompt") or "0")
            )
            completion_price, _, _ = _customer_price_from_dollars_per_token(
                str(pricing.get("completion") or "0")
            )
            # Cached input rate — Anthropic / OpenAI / DeepSeek / Z.AI
            # / Kimi / Novita / Venice all expose this; OR snapshot
            # uses `input_cache_read` as the field name.
            cached_price: int | None = None
            cache_read = pricing.get("input_cache_read")
            if cache_read:
                cached_price, _, _ = _customer_price_from_dollars_per_token(str(cache_read))
            # Tier-aware pricing: read multi-tier from snapshot if present;
            # otherwise synthesize a single-tier list from the headline rate.
            tiers = _read_pricing_tiers(pricing, "prompt") or _flat_tier(
                prompt_price, completion_price, prompt_cached=cached_price
            )
            per_endpoint_prices.append((prompt_price, completion_price, tiers, slug, raw_ep))

        if not per_endpoint_prices:
            continue

        # Model-level price = cheapest endpoint headline, so /v1/models
        # top-level `pricing.prompt` doesn't lie when multiple providers
        # serve the same model at different tiers.
        cheapest_prompt = min(p for p, _c, _t, _s, _e in per_endpoint_prices)
        cheapest_completion = min(c for _p, c, _t, _s, _e in per_endpoint_prices)
        # Tier list belongs to the cheapest endpoint (matches the
        # headline rate above).
        cheapest_tiers = next(t for p, _c, t, _s, _e in per_endpoint_prices if p == cheapest_prompt)

        ctx_candidates = [
            int(raw_model.get("context_length") or 0),
            *(int(ep.get("context_length") or 0) for _p, _c, _t, _s, ep in per_endpoint_prices),
        ]
        context_length = max(ctx_candidates) or 0

        # Anthropic-native `/v1/messages` is only available for models
        # Anthropic actually serves; for everything else, /v1/messages is
        # not supported even if Claude-on-OpenRouter etc. exist. Drive
        # the supports_messages flag off the publisher.
        supports_messages = publisher == "anthropic"
        prepaid_available = any(
            slug in GATEWAY_PREPAID_PROVIDER_SLUGS for _p, _c, _t, slug, _ep in per_endpoint_prices
        )
        models[model_id] = Model(
            id=model_id,
            name=str(raw_model.get("name") or model_id),
            provider=publisher,
            context_length=context_length,
            supports_chat=True,
            supports_messages=supports_messages,
            prepaid_available=prepaid_available,
            byok_available=PROVIDERS[publisher].supports_byok,
            prompt_price_microdollars_per_million_tokens=cheapest_prompt,
            completion_price_microdollars_per_million_tokens=cheapest_completion,
            published_prompt_price_microdollars_per_million_tokens=cheapest_prompt,
            published_completion_price_microdollars_per_million_tokens=cheapest_completion,
            price_tiers=cheapest_tiers,
            published_price_tiers=cheapest_tiers,
        )

        for prompt_price, completion_price, tiers, slug, raw_ep in per_endpoint_prices:
            upstream_id = str(raw_ep.get("model_id") or model_id)
            if slug in GATEWAY_PREPAID_PROVIDER_SLUGS:
                credits_id = f"{model_id}@{slug}/prepaid"
                endpoints[credits_id] = ModelEndpoint(
                    id=credits_id,
                    model_id=model_id,
                    provider=slug,
                    usage_type="Credits",
                    upstream_id=upstream_id,
                    prompt_price_microdollars_per_million_tokens=prompt_price,
                    completion_price_microdollars_per_million_tokens=completion_price,
                    published_prompt_price_microdollars_per_million_tokens=prompt_price,
                    published_completion_price_microdollars_per_million_tokens=completion_price,
                    price_tiers=tiers,
                    published_price_tiers=tiers,
                )
            if PROVIDERS[slug].supports_byok:
                byok_id = f"{model_id}@{slug}/byok"
                endpoints[byok_id] = ModelEndpoint(
                    id=byok_id,
                    model_id=model_id,
                    provider=slug,
                    usage_type="BYOK",
                    upstream_id=upstream_id,
                    prompt_price_microdollars_per_million_tokens=prompt_price,
                    completion_price_microdollars_per_million_tokens=completion_price,
                    published_prompt_price_microdollars_per_million_tokens=prompt_price,
                    published_completion_price_microdollars_per_million_tokens=completion_price,
                    price_tiers=tiers,
                    published_price_tiers=tiers,
                )

    return models, endpoints


def _as_positive_int(value: object) -> int:
    if not isinstance(value, int | str | float | bytes | bytearray):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(parsed, 0)


def _provider_manifest_price_scale(raw: dict[str, Any]) -> int:
    """Return the multiplier needed to turn provider-manifest price fields
    into microdollars per million tokens.

    Most manifests store true microdollars/M. Novita's `/models` feed stores
    prices 100x smaller than its public `$ /Mt` table, so its manifest carries
    an explicit scale to prevent the catalog from falling through to the
    global $0.01/M floor.
    """
    scale = _as_positive_int(raw.get("price_scale_to_microdollars_per_million_tokens"))
    return max(scale, 1)


def _provider_manifest_price_cost(value: object, *, price_scale: int) -> int:
    parsed = _as_positive_int(value)
    if parsed <= 0:
        return 0
    return parsed * price_scale


def _provider_manifest_price_tiers(
    raw_model: dict[str, Any],
    default_prompt_price: int,
    default_completion_price: int,
    default_cached_prompt_price: int | None,
    *,
    price_scale: int = 1,
) -> tuple[PriceTier, ...]:
    raw_tiers = raw_model.get("price_tiers")
    if not isinstance(raw_tiers, list) or not raw_tiers:
        return _flat_tier(
            default_prompt_price,
            default_completion_price,
            prompt_cached=default_cached_prompt_price,
        )

    tiers: list[PriceTier] = []
    for raw_tier in raw_tiers:
        if not isinstance(raw_tier, dict):
            return _flat_tier(
                default_prompt_price,
                default_completion_price,
                prompt_cached=default_cached_prompt_price,
            )
        raw_threshold = raw_tier.get("max_prompt_tokens")
        if raw_threshold is None:
            threshold = None
        elif isinstance(raw_threshold, int | str | float | bytes | bytearray):
            threshold = _as_positive_int(raw_threshold)
            if threshold <= 0:
                return _flat_tier(
                    default_prompt_price,
                    default_completion_price,
                    prompt_cached=default_cached_prompt_price,
                )
        else:
            return _flat_tier(
                default_prompt_price,
                default_completion_price,
                prompt_cached=default_cached_prompt_price,
            )

        prompt_cost = _provider_manifest_price_cost(
            raw_tier.get("input_token_price_per_m"),
            price_scale=price_scale,
        )
        completion_cost = _provider_manifest_price_cost(
            raw_tier.get("output_token_price_per_m"),
            price_scale=price_scale,
        )
        if prompt_cost <= 0 or completion_cost <= 0:
            return _flat_tier(
                default_prompt_price,
                default_completion_price,
                prompt_cached=default_cached_prompt_price,
            )
        cached_cost = _provider_manifest_price_cost(
            raw_tier.get("cached_input_token_price_per_m"),
            price_scale=price_scale,
        )
        cached_price = _customer_price(cached_cost) if cached_cost > 0 else None
        tiers.append(
            PriceTier(
                max_prompt_tokens=threshold,
                prompt_price_microdollars_per_million_tokens=_customer_price(prompt_cost),
                completion_price_microdollars_per_million_tokens=_customer_price(
                    completion_cost
                ),
                prompt_cached_price_microdollars_per_million_tokens=cached_price,
            )
        )

    if tiers[-1].max_prompt_tokens is not None:
        return _flat_tier(
            default_prompt_price,
            default_completion_price,
            prompt_cached=default_cached_prompt_price,
        )
    return tuple(tiers)


def _supplemental_provider_models_and_endpoints() -> tuple[
    dict[str, Model], dict[str, ModelEndpoint]
]:
    """Read provider-native model manifests for providers whose live API
    lists more routes than OpenRouter's endpoint feed. These manifests
    preserve exact upstream model IDs and provider-direct prices, so the
    control plane can authorize routes the attested gateway can actually
    call and bill.

    Novita, Nebius, MiniMax, Cerebras, Gemini, Fireworks, and Z.AI currently use this path because their
    live `/models` feeds expose working provider-direct routes before
    OpenRouter's public endpoint catalog catches up. Anthropic uses it for
    Claude Opus 4.8, which shipped after the snapshot — the attested gateway
    maps `anthropic/claude-opus-4.8` -> `claude-opus-4-8` algorithmically
    (internal/llm/anthropic.go), so the route works with no enclave change.
    """
    models: dict[str, Model] = {}
    endpoints: dict[str, ModelEndpoint] = {}
    for provider_slug in (
        "novita",
        "nebius",
        "minimax",
        "anthropic",
        "cerebras",
        "gemini",
        "fireworks",
        "zai",
        "xiaomi",
    ):
        path = _PROVIDER_MODELS_DIR / f"{provider_slug}.json"
        if not path.exists() or provider_slug not in PROVIDERS:
            continue
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw_models = raw.get("models")
        if not isinstance(raw_models, list):
            continue
        provider = PROVIDERS[provider_slug]
        price_scale = _provider_manifest_price_scale(raw)
        for raw_model in raw_models:
            if not isinstance(raw_model, dict):
                continue
            model_id = raw_model.get("id")
            if not isinstance(model_id, str) or not model_id:
                continue
            upstream_id = raw_model.get("upstream_id")
            if not isinstance(upstream_id, str) or not upstream_id:
                upstream_id = model_id
            if _is_provider_deprecated_model(provider_slug, model_id, upstream_id):
                continue
            if raw_model.get("model_type") not in (None, "chat"):
                continue
            if "chat/completions" not in {str(item) for item in (raw_model.get("endpoints") or [])}:
                continue

            prompt_cost = _provider_manifest_price_cost(
                raw_model.get("input_token_price_per_m"),
                price_scale=price_scale,
            )
            completion_cost = _provider_manifest_price_cost(
                raw_model.get("output_token_price_per_m"),
                price_scale=price_scale,
            )
            cached_cost = _provider_manifest_price_cost(
                raw_model.get("cached_input_token_price_per_m"),
                price_scale=price_scale,
            )
            prompt_price = _customer_price(prompt_cost)
            completion_price = _customer_price(completion_cost)
            cached_price = _customer_price(cached_cost) if cached_cost > 0 else None
            tiers = _provider_manifest_price_tiers(
                raw_model,
                prompt_price,
                completion_price,
                cached_price,
                price_scale=price_scale,
            )
            publisher = (
                _author_provider(model_id, [{"tr_provider_slug": provider_slug}]) or provider_slug
            )
            context_length = _as_positive_int(raw_model.get("context_length"))
            name = str(raw_model.get("display_name") or raw_model.get("title") or model_id)

            models[model_id] = Model(
                id=model_id,
                name=name,
                provider=publisher,
                context_length=context_length,
                upstream_id=upstream_id,
                supports_chat=True,
                supports_messages=False,
                # Availability comes from the explicit provider-native
                # endpoints below. Do not let _build_endpoints synthesize
                # publisher-direct routes for supplemental-only models
                # such as deepseek/deepseek-ocr-2@deepseek.
                prepaid_available=False,
                byok_available=False,
                prompt_price_microdollars_per_million_tokens=prompt_price,
                completion_price_microdollars_per_million_tokens=completion_price,
                published_prompt_price_microdollars_per_million_tokens=prompt_price,
                published_completion_price_microdollars_per_million_tokens=completion_price,
                price_tiers=tiers,
                published_price_tiers=tiers,
            )

            if provider_slug in GATEWAY_PREPAID_PROVIDER_SLUGS:
                credits_id = f"{model_id}@{provider_slug}/prepaid"
                endpoints[credits_id] = ModelEndpoint(
                    id=credits_id,
                    model_id=model_id,
                    provider=provider_slug,
                    usage_type="Credits",
                    upstream_id=upstream_id,
                    prompt_price_microdollars_per_million_tokens=prompt_price,
                    completion_price_microdollars_per_million_tokens=completion_price,
                    published_prompt_price_microdollars_per_million_tokens=prompt_price,
                    published_completion_price_microdollars_per_million_tokens=completion_price,
                    price_tiers=tiers,
                    published_price_tiers=tiers,
                )
            if provider.supports_byok:
                byok_id = f"{model_id}@{provider_slug}/byok"
                endpoints[byok_id] = ModelEndpoint(
                    id=byok_id,
                    model_id=model_id,
                    provider=provider_slug,
                    usage_type="BYOK",
                    upstream_id=upstream_id,
                    prompt_price_microdollars_per_million_tokens=prompt_price,
                    completion_price_microdollars_per_million_tokens=completion_price,
                    published_prompt_price_microdollars_per_million_tokens=prompt_price,
                    published_completion_price_microdollars_per_million_tokens=completion_price,
                    price_tiers=tiers,
                    published_price_tiers=tiers,
                )
    return models, endpoints


# --- Embedding models -----------------------------------------------------
# Hand-curated embedding catalog. Embeddings don't come from the OpenRouter
# chat snapshot or the chat-only provider manifests (`_supplemental_*` skips
# any model_type != chat), so they're seeded here with explicit upstream IDs
# + published per-million INPUT prices. Completion price is always 0 —
# embeddings bill input tokens only. Each model gets a Credits endpoint (if
# its provider is in GATEWAY_PREPAID_PROVIDER_SLUGS) + a BYOK endpoint,
# synthesized by `_build_endpoints` exactly like a chat model, because
# `prepaid_available`/`byok_available` are set.
#
# PRICES are the providers' published per-million input rates as of
# 2026-06-07 (markup + $0.01/M floor applied via `_priced`); the
# pricing-refresh job should true them up. supports_chat=False keeps chat
# routing from ever selecting an embedding model; supports_embeddings=True
# is what `/embeddings/models` and the embeddings route filter on.
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
        "provider": "gemini",
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
        "cost_dollars_per_million": "0.06",
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


def _embedding_models() -> dict[str, Model]:
    """Seed the embedding-model catalog (input-only pricing)."""
    models: dict[str, Model] = {}
    for spec in _EMBEDDING_SPECS:
        if spec["provider"] not in PROVIDERS:
            continue
        prompt_price, published_price, _cost = _priced(spec["cost_dollars_per_million"])
        models[spec["id"]] = Model(
            id=spec["id"],
            name=spec["name"],
            provider=spec["provider"],
            context_length=spec["context_length"],
            upstream_id=spec["upstream_id"],
            supports_chat=False,
            supports_messages=False,
            supports_embeddings=True,
            prepaid_available=True,
            byok_available=True,
            prompt_price_microdollars_per_million_tokens=prompt_price,
            completion_price_microdollars_per_million_tokens=0,
            published_prompt_price_microdollars_per_million_tokens=published_price,
            published_completion_price_microdollars_per_million_tokens=0,
            price_tiers=_flat_tier(prompt_price, 0, None),
            published_price_tiers=_flat_tier(published_price, 0, None),
        )
    return models


_EMBEDDING_MODELS = _embedding_models()


_INGESTED_MODELS, _INGESTED_ENDPOINTS = _ingested_models_and_endpoints()
_SUPPLEMENTAL_MODELS, _SUPPLEMENTAL_ENDPOINTS = _supplemental_provider_models_and_endpoints()
# The OpenRouter ingest snapshot is the primary catalog. Provider-native
# supplements add exact routes from providers whose live model API is
# ahead of OpenRouter's endpoint feed. Pricing across both paths goes
# through the same `cost × 1.10, $0.01/M floor` formula.
MODELS.update(_INGESTED_MODELS)
for _model_id, _model in _SUPPLEMENTAL_MODELS.items():
    MODELS.setdefault(_model_id, _model)
# Embedding models override any snapshot/supplemental collision: the
# hand-curated embedding entry (input-only pricing, supports_embeddings) is
# authoritative for these IDs. Merge BEFORE `_build_endpoints` so each gets
# its Credits + BYOK endpoints synthesized.
for _model_id, _model in _EMBEDDING_MODELS.items():
    MODELS[_model_id] = _model

MODEL_ENDPOINTS: dict[str, ModelEndpoint] = _build_endpoints(MODELS)
MODEL_ENDPOINTS.update(_INGESTED_ENDPOINTS)
MODEL_ENDPOINTS.update(_SUPPLEMENTAL_ENDPOINTS)

# --- Provider served-model allowlist -------------------------------------
# Our upstream accounts don't always match OpenRouter's provider→model map.
# Routing a model a provider doesn't actually host on our account returns an
# upstream error (the gateway surfaces it as a 502). When an allowlist is set
# for a provider, ONLY its listed models keep that provider's endpoints; routes
# for any other model on that provider are dropped before serving/routing.
#
# Cerebras (the key wired into the enclave) serves only gpt-oss-120b and
# glm-4.7 on our account — verified 2026-06-04 from the Cerebras dashboard —
# NOT the Llama models OpenRouter lists for Cerebras's GA tier. Without this
# filter every Llama-via-Cerebras route 502s, and because Cerebras is rank-0
# ("fastest") it gets tried first for those models. The provider-native
# Cerebras manifest publishes the two verified canonical routes plus
# cerebras/* convenience aliases that map to the same upstream IDs.
_PROVIDER_SERVED_MODEL_ALLOWLIST: dict[str, frozenset[str]] = {
    "cerebras": frozenset(
        {
            "openai/gpt-oss-120b",
            "cerebras/gpt-oss-120b",
            "z-ai/glm-4.7",
            "cerebras/zai-glm-4.7",
        }
    ),
}

# Inverse of the allowlist, but keyed by MODEL across ALL providers: specific
# prepaid (Credits) model ids that 502 on every provider that lists them, while
# every other model is kept. Use this for dead-everywhere models (an allowlist
# would force us to enumerate each provider's whole working set instead).
#
# OpenAI's GPT-5.4 line and the "-pro" tiers are closed models OpenAI does not
# serve on our key — verified 2026-06-04 via the gateway probe pinned to openai
# (gpt-5.5 => OK; gpt-5.4 / gpt-5.4-nano / gpt-5.4-pro / gpt-5.5-pro => 502).
# Because they are closed, no third-party prepaid host can serve them either:
# the snapshot's gmi endpoint for gpt-5.4-nano 502s too (verified). So drop
# their Credits routes on EVERY provider. (gpt-5.5 works and stays; BYOK routes
# are left intact as the customer's own responsibility.)
_UNSERVED_CREDITS_MODELS: frozenset[str] = frozenset(
    {
        "openai/gpt-5.4",
        "openai/gpt-5.4-nano",
        "openai/gpt-5.4-pro",
        "openai/gpt-5.5-pro",
    }
)

# Provider-keyed denylist: specific (provider, model) prepaid routes the
# OpenRouter snapshot lists but the provider's live API doesn't actually serve
# on our account — every one verified 502 pinned to that provider via the
# gateway probe, then cross-checked against the provider's own /models feed,
# 2026-06-04. Drop ONLY that provider's Credits route; the model still serves
# fine wherever it's real (its native provider and/or other hosts). Unlike the
# all-provider _UNSERVED_CREDITS_MODELS set, this is per provider, so a model
# that's dead on one host but healthy elsewhere keeps its working routes.
#
#   gmi      — open-weights GPU host; can't run the two closed models the
#              snapshot lists for it (anthropic/claude-opus-4.7, openai/gpt-5.5),
#              both of which serve fine on their native provider.
#   deepseek — DeepSeek-direct serves only deepseek-v4-flash/-v4-pro (its real
#              /models); the snapshot's chat-v3.1 and v3.2 routes 502.
#   nebius   — retired two older models still in the snapshot (gemma-2-2b-it,
#              Meta-Llama-3.1-8B-Instruct); its current /models has neither.
#   zai      — does not serve glm-4-32b or glm-4.7-flash (both absent from its
#              /models). NB: zai's glm-4.7 ALSO 502'd, but that was an ENCLAVE
#              model-id-map bug (zai serves glm-4.7 under the BARE id; the
#              enclave was sending "zai-glm-4.7") — fixed in quill-cloud-proxy
#              (zaiModelMap), so glm-4.7 is deliberately NOT dropped here.
#   gemini   — Google's Gemini API (closed gemini-* models) does NOT serve the
#              open-weights Gemma models on our key: every google/gemma-* route
#              pinned to gemini 502s (upstream_4xx), verified 2026-06-04. Gemma
#              is hosted by the open-weights providers (deepinfra/novita/parasail/
#              gmi/lightning), which work. gemini was ranked first for these, so
#              DEFAULT routing for Gemma was 502ing — drop gemini's Gemma routes.
_PROVIDER_UNSERVED_CREDITS_MODELS: dict[str, frozenset[str]] = {
    "gmi": frozenset({"anthropic/claude-opus-4.7", "openai/gpt-5.5"}),
    "openai": frozenset({"openai/gpt-oss-120b", "openai/gpt-oss-20b"}),
    "deepseek": frozenset({"deepseek/deepseek-chat-v3.1", "deepseek/deepseek-v3.2"}),
    "nebius": frozenset(
        {"google/gemma-2-2b-it", "meta-llama/Meta-Llama-3.1-8B-Instruct"}
    ),
    "zai": frozenset({"z-ai/glm-4-32b", "z-ai/glm-4.7-flash"}),
    "together": frozenset(
        {
            "meta-llama/llama-3.1-8b-instruct",
            "meta-llama/llama-3.1-70b-instruct",
            "qwen/qwen-2.5-72b-instruct",
        }
    ),
    "grok": frozenset({"x-ai/grok-4.20-multi-agent"}),
    # parasail — listed in the upstream snapshot, but Parasail's own chat API
    # returns 403 "deployment ... doesn't exist or isn't accessible" for these
    # routes on our operator key (direct API probe 2026-06-05). Keep BYOK
    # visible for customer accounts, but do not route prepaid traffic here.
    "parasail": frozenset({"qwen/qwen3-235b-a22b-2507", "z-ai/glm-5"}),
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
            # 2026-06-06 batch 3: dropped after a SERIALIZED re-test (cooldown +
            # 25s gaps, so not our own rate-limit). sao10k/xiaomimimo return a
            # fast explicit NOT_AVAILABLE; the rest queue with no first byte then
            # 429 (~60s) — i.e. Novita never usefully serves them on our key, so
            # they only 502 + burn the SLO. (NB: nebius Qwen3.5-397B-A17B-fast,
            # also slow, is deliberately KEPT — a 397B model legitimately takes
            # >20s to first token and we want it available.)
            "deepseek/deepseek-r1-distill-qwen-14b",
            "deepseek/deepseek-r1-distill-qwen-32b",
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
    "gemini": frozenset(
        {
            "google/gemma-3-4b-it",
            "google/gemma-3-12b-it",
            "google/gemma-3-27b-it",
            "google/gemma-4-26b-a4b-it",
            "google/gemma-4-31b-it",
        }
    ),
}


def _filter_unserved_provider_endpoints(
    endpoints: dict[str, ModelEndpoint],
) -> dict[str, ModelEndpoint]:
    """Drop a provider's prepaid (Credits) endpoints for models it doesn't
    serve on our account. Only Credits routes use OUR provider key, so only
    those 502 on an account mismatch — BYOK routes use the customer's own key
    (their account may serve a different model set), so they're left intact.

    Four complementary filters apply:
      * provider deprecation — drop a disabled upstream route on one provider for
        every usage type (Nebius June 2026 retirements).
      * allowlist        — keep ONLY the listed Credits models for a provider (Cerebras).
      * model denylist    — drop the listed Credits models on EVERY provider (GPT-5.4/pro).
      * provider denylist — drop a Credits model on ONE provider only (gmi closed models).
    """
    allow = _PROVIDER_SERVED_MODEL_ALLOWLIST

    def _keep(endpoint: ModelEndpoint) -> bool:
        if _is_provider_deprecated_model(
            endpoint.provider, endpoint.model_id, endpoint.upstream_id
        ):
            return False
        if endpoint.usage_type != "Credits":
            return True
        if endpoint.provider in allow and endpoint.model_id not in allow[endpoint.provider]:
            return False
        if endpoint.model_id in _UNSERVED_CREDITS_MODELS:
            return False
        if endpoint.model_id in _PROVIDER_UNSERVED_CREDITS_MODELS.get(
            endpoint.provider, frozenset()
        ):
            return False
        return True

    return {
        endpoint_id: endpoint
        for endpoint_id, endpoint in endpoints.items()
        if _keep(endpoint)
    }


MODEL_ENDPOINTS = _filter_unserved_provider_endpoints(MODEL_ENDPOINTS)


def endpoints_for_model(model_id: str) -> list[ModelEndpoint]:
    return [endpoint for endpoint in MODEL_ENDPOINTS.values() if endpoint.model_id == model_id]


def endpoint_for_id(endpoint_id: str | None) -> ModelEndpoint | None:
    if endpoint_id is None:
        return None
    return MODEL_ENDPOINTS.get(endpoint_id)


def default_endpoint_for_model(model: Model) -> ModelEndpoint | None:
    endpoints = endpoints_for_model(model.id)
    if not endpoints:
        return None
    for endpoint in endpoints:
        if endpoint.usage_type == "Credits":
            return endpoint
    return endpoints[0]


def auto_candidate_models(order: str | None = None) -> list[Model]:
    raw_ids = [
        item.strip()
        for item in (order.split(",") if order else DEFAULT_AUTO_MODEL_ORDER)
        if item.strip()
    ]
    candidates: list[Model] = []
    seen: set[str] = set()
    for model_id in raw_ids:
        if model_id == AUTO_MODEL_ID or model_id in seen:
            continue
        model = MODELS.get(model_id)
        if model is not None and model.supports_chat:
            candidates.append(model)
            seen.add(model_id)
    return candidates


def free_candidate_models(limit: int = 16) -> list[Model]:
    candidates = [
        model
        for model in MODELS.values()
        if _is_regular_chat_model(model) and model.id.endswith(":free")
    ]
    candidates.sort(key=_price_sort_key)
    return candidates[:limit]


def cheap_candidate_models(limit: int = 8) -> list[Model]:
    by_provider: dict[str, Model] = {}
    for model in MODELS.values():
        if not _is_regular_chat_model(model) or model.id.endswith(":free"):
            continue
        current = by_provider.get(model.provider)
        if current is None or _price_sort_key(model) < _price_sort_key(current):
            by_provider[model.provider] = model
    return sorted(by_provider.values(), key=_price_sort_key)[:limit]


def monitor_candidate_models(limit: int = 12) -> list[Model]:
    # Order is ASCENDING by cost-per-probe so the steady-state synthetic
    # spend hits the cheapest reliable model first; rollover only
    # escalates to pricier models when the cheap path fails. This keeps
    # the rollover-resilience signal AND cuts steady-state probe cost
    # ~12x vs. anthropic/claude-haiku-4.5 leading.
    #
    # Tiers 1-3 are ALL DeepSeek-family non-reasoning models —
    # v4-flash, v3.2, v4-pro. The lead (v4-flash) is served by 4
    # providers (deepseek, parasail, siliconflow, novita) so TR's
    # within-model routing already fans across providers transparently.
    # Tier 2 (v3.2) is same-family fallback for the cheap path; tier 3
    # (v4-pro) brings 2 ADDITIONAL providers (tinfoil + gmi) so a 6th
    # and 7th provider show up in the rollover ladder before crossing
    # to Mistral / OpenAI / etc.
    #
    # Leading with non-reasoning models (DeepSeek V4/V3.2, Mistral
    # Small, GPT-5.4 nano) avoids the reasoning_content failure mode
    # that drove the 2026-05 pong_mismatch surge — kimi-k2.6 / glm-4.6
    # stay in the rollover tail but won't be hit in steady state.
    #
    # Costs at 2026-06 prices ($/M tokens, in / out):
    #   deepseek/deepseek-v4-flash    0.154 / 0.308   ← lead (4 providers)
    #   deepseek/deepseek-v3.2        0.308 / 0.495   ← same-family backup
    #   deepseek/deepseek-v4-pro      0.478 / 0.957   ← +tinfoil +gmi
    #   mistralai/mistral-small-2603  0.165 / 0.660   ← cross-provider
    #   openai/gpt-4.1-mini           0.440 / 1.760
    #   z-ai/glm-4.5-air              0.220 / 1.210
    #   google/gemini-2.5-flash       0.330 / 2.750
    #   z-ai/glm-4.6                  0.660 / 2.420   ← reasoning, tail
    #   moonshotai/kimi-k2.6          0.880 / 3.850   ← reasoning, tail
    #   anthropic/claude-haiku-4.5    1.100 / 5.500   ← most expensive
    preferred_ids = [
        "deepseek/deepseek-v4-flash",
        "deepseek/deepseek-v3.2",
        "deepseek/deepseek-v4-pro",
        "mistralai/mistral-small-2603",
        "openai/gpt-4.1-mini",
        "z-ai/glm-4.5-air",
        "google/gemini-2.5-flash",
        "z-ai/glm-4.6",
        "moonshotai/kimi-k2.6",
        "anthropic/claude-haiku-4.5",
    ]
    candidates: list[Model] = []
    seen: set[str] = set()
    for model_id in preferred_ids:
        model = MODELS.get(model_id)
        if model is not None and _is_regular_chat_model(model) and not model.id.endswith(":free"):
            candidates.append(model)
            seen.add(model.id)
    for model in cheap_candidate_models(limit=limit * 2):
        if model.id not in seen:
            candidates.append(model)
            seen.add(model.id)
        if len(candidates) >= limit:
            break
    return candidates[:limit]


def _privacy_candidate_models(
    *,
    min_tier: int,
    preferred_providers: tuple[str, ...] = (),
    allowed_providers: frozenset[str] | None = None,
    limit: int = 12,
) -> list[Model]:
    """Unique chat models with at least one endpoint clearing min_tier.

    This builds the model-level rollover ladder. The routing layer forces the
    same privacy floor, so the gateway still picks only qualifying endpoints.
    """
    provider_rank = {provider: index for index, provider in enumerate(preferred_providers)}
    eligible: list[tuple[int, int, int, str, Model]] = []
    per_provider: dict[str, list[tuple[int, int, Model]]] = {}
    for endpoint in MODEL_ENDPOINTS.values():
        provider = PROVIDERS.get(endpoint.provider)
        model = MODELS.get(endpoint.model_id)
        if (
            provider is None
            or model is None
            or not _is_regular_chat_model(model)
            or model.id.endswith(":free")
            or provider_privacy_tier(provider) < min_tier
        ):
            continue
        if allowed_providers is not None and endpoint.provider not in allowed_providers:
            continue
        price = (
            endpoint.prompt_price_microdollars_per_million_tokens
            + endpoint.completion_price_microdollars_per_million_tokens
        )
        usage_rank = 0 if endpoint.usage_type == "Credits" else 1
        rank = provider_rank.get(endpoint.provider, len(provider_rank))
        eligible.append((rank, usage_rank, price, endpoint.provider, model))
        per_provider.setdefault(endpoint.provider, []).append((usage_rank, price, model))

    result: list[Model] = []
    seen: set[str] = set()
    for provider_slug in preferred_providers:
        options = sorted(
            per_provider.get(provider_slug, []),
            key=lambda item: (item[0], item[1], item[2].provider, item[2].id),
        )
        for _usage_rank, _price, model in options:
            if model.id not in seen:
                result.append(model)
                seen.add(model.id)
                break
        if len(result) >= limit:
            return result

    for _rank, _usage_rank, _price, _provider, model in sorted(
        eligible,
        key=lambda item: (item[0], item[1], item[2], item[3], item[4].provider, item[4].id),
    ):
        if model.id in seen:
            continue
        result.append(model)
        seen.add(model.id)
        if len(result) >= limit:
            break
    return result


def eu_candidate_models(limit: int = 12) -> list[Model]:
    return _privacy_candidate_models(
        min_tier=PRIVACY_TIER_STANDARD,
        preferred_providers=EU_FOCUSED_PROVIDER_ORDER,
        allowed_providers=frozenset(EU_FOCUSED_PROVIDER_ORDER),
        limit=limit,
    )


def zdr_candidate_models(limit: int = 12) -> list[Model]:
    return _privacy_candidate_models(
        min_tier=PRIVACY_TIER_ZERO_RETENTION,
        preferred_providers=("anthropic", "openai", "gemini", "tinfoil", "venice", "phala"),
        limit=limit,
    )


def e2e_candidate_models(limit: int = 12) -> list[Model]:
    return _privacy_candidate_models(
        min_tier=PRIVACY_TIER_CONFIDENTIAL,
        preferred_providers=("tinfoil", "venice", "phala", "gmi"),
        limit=limit,
    )


def meta_candidate_models(model_id: str) -> list[Model]:
    if model_id == AUTO_MODEL_ID:
        return auto_candidate_models()
    if model_id == FREE_MODEL_ID:
        return free_candidate_models()
    if model_id == CHEAP_MODEL_ID:
        return cheap_candidate_models()
    if model_id == EU_MODEL_ID:
        return eu_candidate_models()
    if model_id == ZDR_MODEL_ID:
        return zdr_candidate_models()
    if model_id == E2E_MODEL_ID:
        return e2e_candidate_models()
    if model_id == MONITOR_MODEL_ID:
        return monitor_candidate_models()
    if model_id == FUSION_MODEL_ID:
        return []
    return []


def _meta_route_kind(model_id: str) -> str:
    if model_id == FREE_MODEL_ID:
        return "free_pool"
    if model_id == CHEAP_MODEL_ID:
        return "cheap_pool"
    if model_id == EU_MODEL_ID:
        return "eu_pool"
    if model_id == ZDR_MODEL_ID:
        return "zdr_pool"
    if model_id == E2E_MODEL_ID:
        return "e2e_pool"
    if model_id == MONITOR_MODEL_ID:
        return "synthetic_monitor_pool"
    if model_id == FUSION_MODEL_ID:
        return "fusion_panel"
    if model_id == AUTO_MODEL_ID:
        return "auto_pool"
    return "model"


def _is_regular_chat_model(model: Model) -> bool:
    return model.id not in META_MODEL_IDS and model.supports_chat


def _price_sort_key(model: Model) -> tuple[int, str, str]:
    return (
        model.prompt_price_microdollars_per_million_tokens
        + model.completion_price_microdollars_per_million_tokens,
        model.provider,
        model.id,
    )


def _meta_price_range(
    model_id: str,
    attr: str,
) -> tuple[int, int]:
    """Return (min, max) of the requested price attribute across the
    Auto model's candidate set. Auto itself has no intrinsic price —
    the request lands on whatever model the router picks — so we
    surface the range so /v1/models doesn't show a misleading $0."""
    candidates = meta_candidate_models(model_id)
    values = [getattr(c, attr) for c in candidates if getattr(c, attr, 0) > 0]
    if not values:
        return (0, 0)
    return (min(values), max(values))


def _model_max_privacy_tier(model: Model, endpoints: list[ModelEndpoint]) -> int:
    """Highest privacy tier this model can be routed through. For meta
    models (auto/free/cheap), that's the best tier across the candidate
    pool — NOT the 'trustedrouter' pseudo-provider, which would falsely
    claim confidential for Auto. For regular models, the max across the
    model's own provider plus any serving endpoints."""
    providers: set[str] = set()
    if model.id in META_MODEL_IDS:
        for candidate in meta_candidate_models(model.id):
            providers.add(candidate.provider)
    else:
        providers.add(model.provider)
        for endpoint in endpoints:
            providers.add(endpoint.provider)
    tiers = [
        provider_privacy_tier(PROVIDERS[p]) for p in providers if p in PROVIDERS
    ]
    return max(tiers) if tiers else PRIVACY_TIER_STANDARD


def model_max_privacy_tier(model: Model) -> int:
    """Public wrapper: highest privacy tier `model` can be routed through,
    resolving its serving endpoints internally. Used by the router's
    min_privacy filter."""
    return _model_max_privacy_tier(model, endpoints_for_model(model.id))


def model_to_openrouter_shape(model: Model) -> dict[str, object]:
    provider = PROVIDERS[model.provider]
    is_meta = model.id in META_MODEL_IDS
    endpoints = endpoints_for_model(model.id)
    prepaid_available = (
        any(endpoint.usage_type == "Credits" for endpoint in endpoints) or model.prepaid_available
    )
    byok_available = any(endpoint.usage_type == "BYOK" for endpoint in endpoints) or (
        model.byok_available and PROVIDERS[model.provider].supports_byok
    )

    # For meta routers, derive prompt/completion price from the candidate range
    # rather than the catalog's hard-coded 0. Most OpenRouter-compat
    # consumers (browsers, marketplace listings, billing previews) read
    # `pricing.prompt` / `pricing.completion`; if those are 0, Auto
    # appears free in every dashboard. We report the cheapest candidate
    # as the headline price (matches OpenRouter's convention for their
    # `openrouter/auto` meta-model) and add `*_max` fields plus the
    # full candidate manifest so anything that wants a range can show one.
    prompt_min = model.prompt_price_microdollars_per_million_tokens
    prompt_max = prompt_min
    completion_min = model.completion_price_microdollars_per_million_tokens
    completion_max = completion_min
    pub_prompt_min = model.published_prompt_price_microdollars_per_million_tokens
    pub_prompt_max = pub_prompt_min
    pub_completion_min = model.published_completion_price_microdollars_per_million_tokens
    pub_completion_max = pub_completion_min
    if is_meta:
        prompt_min, prompt_max = _meta_price_range(
            model.id, "prompt_price_microdollars_per_million_tokens"
        )
        completion_min, completion_max = _meta_price_range(
            model.id, "completion_price_microdollars_per_million_tokens"
        )
        pub_prompt_min, pub_prompt_max = _meta_price_range(
            model.id, "published_prompt_price_microdollars_per_million_tokens"
        )
        pub_completion_min, pub_completion_max = _meta_price_range(
            model.id, "published_completion_price_microdollars_per_million_tokens"
        )

    pricing: dict[str, str] = {
        "prompt": microdollars_per_million_tokens_to_token_decimal(prompt_min),
        "completion": microdollars_per_million_tokens_to_token_decimal(completion_min),
    }
    if is_meta and (prompt_max != prompt_min or completion_max != completion_min):
        pricing["prompt_max"] = microdollars_per_million_tokens_to_token_decimal(prompt_max)
        pricing["completion_max"] = microdollars_per_million_tokens_to_token_decimal(completion_max)

    tr_block: dict[str, object] = {
        "provider": model.provider,
        "prepaid_available": prepaid_available,
        "byok_available": byok_available,
        "attested_gateway": provider.attested_gateway,
        # Gateway-scoped OpenRouter-compat flag: TrustedRouter does not
        # retain prompt/output content. Upstream provider retention still
        # varies and is exposed per endpoint below plus provider_* fields.
        "stores_content": False,
        "provider_zero_data_retention": provider.provider_zero_data_retention,
        "provider_confidential_compute": provider.provider_confidential_compute,
        "provider_e2ee": provider.provider_e2ee,
        "provider_policy": provider.provider_policy,
        "provider_policy_url": provider.provider_policy_url,
        # Highest privacy tier reachable for this model — the max across
        # every provider that serves it (a request can route to the best
        # one). Lets the picker / SEO pages show "this model can run
        # confidential" without re-deriving from raw posture flags.
        "privacy_tier": _model_max_privacy_tier(model, endpoints),
        "privacy_tier_label": PRIVACY_TIER_LABELS[
            _model_max_privacy_tier(model, endpoints)
        ],
        "prompt_price_microdollars_per_million_tokens": prompt_min,
        "completion_price_microdollars_per_million_tokens": completion_min,
        "published_prompt_price_microdollars_per_million_tokens": pub_prompt_min,
        "published_completion_price_microdollars_per_million_tokens": pub_completion_min,
        # Uniform pricing means the customer pays the headline rate — no
        # secret 1¢/M discount layered on top. Field kept for OpenRouter
        # consumer compat, but always zero.
        "discount_microdollars_per_million_tokens": 0,
        "auto_candidates": [c.id for c in meta_candidate_models(model.id)] if is_meta else None,
        "route_kind": _meta_route_kind(model.id) if is_meta else "model",
        "synthetic_monitor": model.id == MONITOR_MODEL_ID,
        "internal_only": model.id == MONITOR_MODEL_ID,
        # Capability flags so OpenRouter-compat clients (and TR's own chat
        # picker) can tell an embedding model from a chat model without
        # parsing `architecture.modality`.
        "supports_chat": model.supports_chat,
        "supports_embeddings": model.supports_embeddings,
        "endpoints": [
            {
                "id": endpoint.id,
                "provider": endpoint.provider,
                "provider_name": PROVIDERS[endpoint.provider].name,
                "usage_type": endpoint.usage_type,
                "upstream_id": endpoint.upstream_id,
                "attested_gateway": PROVIDERS[endpoint.provider].attested_gateway,
                "stores_content": PROVIDERS[endpoint.provider].stores_content,
                "provider_zero_data_retention": PROVIDERS[
                    endpoint.provider
                ].provider_zero_data_retention,
                "provider_confidential_compute": PROVIDERS[
                    endpoint.provider
                ].provider_confidential_compute,
                "provider_e2ee": PROVIDERS[endpoint.provider].provider_e2ee,
                "provider_policy": PROVIDERS[endpoint.provider].provider_policy,
                "provider_policy_url": PROVIDERS[endpoint.provider].provider_policy_url,
            }
            for endpoint in endpoints
        ],
    }
    if is_meta:
        tr_block["prompt_price_max_microdollars_per_million_tokens"] = prompt_max
        tr_block["completion_price_max_microdollars_per_million_tokens"] = completion_max
        tr_block["published_prompt_price_max_microdollars_per_million_tokens"] = pub_prompt_max
        tr_block["published_completion_price_max_microdollars_per_million_tokens"] = (
            pub_completion_max
        )

    return {
        "id": model.id,
        "name": model.name,
        "created": 0,
        "description": f"{model.name} via TrustedRouter",
        "context_length": model.context_length,
        "architecture": {
            "modality": (
                "text->embedding"
                if model.supports_embeddings and not model.supports_chat
                else "text->text"
            ),
            "tokenizer": "unknown",
            "instruct_type": None,
        },
        "pricing": pricing,
        "top_provider": {
            "context_length": model.context_length,
            "max_completion_tokens": None,
            "is_moderated": False,
        },
        "per_request_limits": None,
        "trustedrouter": tr_block,
    }


def provider_to_openrouter_shape(provider: Provider) -> dict[str, object]:
    return {
        "id": provider.slug,
        "name": provider.name,
        "supports_prepaid": provider.supports_prepaid,
        "supports_byok": provider.supports_byok,
        "attested_gateway": provider.attested_gateway,
        "stores_content": provider.stores_content,
        "provider_zero_data_retention": provider.provider_zero_data_retention,
        "provider_confidential_compute": provider.provider_confidential_compute,
        "provider_e2ee": provider.provider_e2ee,
        "provider_policy": provider.provider_policy,
        "provider_policy_url": provider.provider_policy_url,
    }


_PROVIDER_DISPLAY_ORDER = ("tinfoil", "venice")


def providers_for_display() -> tuple[Provider, ...]:
    """Provider transparency should lead with privacy-forward upstreams."""
    pinned = [PROVIDERS[slug] for slug in _PROVIDER_DISPLAY_ORDER if slug in PROVIDERS]
    pinned_slugs = {provider.slug for provider in pinned}
    return tuple(pinned + [provider for provider in PROVIDERS.values() if provider.slug not in pinned_slugs])
