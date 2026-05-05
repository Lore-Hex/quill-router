from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
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
    stores_content: bool = False


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
    prompt_price_microdollars_per_million_tokens: int = 0
    completion_price_microdollars_per_million_tokens: int = 0
    published_prompt_price_microdollars_per_million_tokens: int = 0
    published_completion_price_microdollars_per_million_tokens: int = 0


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

    @property
    def is_byok(self) -> bool:
        return self.usage_type.lower() == "byok"


class ModelPricingKwargs(TypedDict):
    prompt_price_microdollars_per_million_tokens: int
    completion_price_microdollars_per_million_tokens: int
    published_prompt_price_microdollars_per_million_tokens: int
    published_completion_price_microdollars_per_million_tokens: int


# Uniform pricing: customer pays cost + 10%, floor $0.10/M tokens. Same
# value goes into both `prompt_price_*` and `published_*` — TR no longer
# runs the 1¢/M "discount theater". The floor catches free upstream tiers
# so the catalog never advertises $0/M to end users.
_PRICE_MARKUP_RATIO = Decimal("1.10")
_PRICE_FLOOR_MICRODOLLARS_PER_M = 100_000  # $0.10 per million tokens.


def _customer_price(cost_microdollars_per_million: int) -> int:
    """Apply the markup formula. Input/output in microdollars per million tokens."""
    marked_up = int(
        (Decimal(cost_microdollars_per_million) * _PRICE_MARKUP_RATIO).to_integral_value()
    )
    return max(marked_up, _PRICE_FLOOR_MICRODOLLARS_PER_M)


def _priced(cost_dollars_per_million: str | int | float) -> tuple[int, int, int]:
    """Return (prompt_price, published_price, cost_microdollars) for a
    dollars-per-million cost. prompt_price == published_price under the
    uniform formula; cost is preserved as a third value for any consumer
    that wants the upstream-paid amount (e.g. the per-endpoint detail page)."""
    cost = dollars_to_microdollars(cost_dollars_per_million)
    customer = _customer_price(cost)
    return customer, customer, cost


def _hand_priced(prompt_dollars: str, completion_dollars: str) -> ModelPricingKwargs:
    """Splat into a Model() ctor for hand-curated entries. Replaces the old
    `_one_cent_less_per_million` + `dollars_to_microdollars` doubled-up
    spelling."""
    prompt_price, prompt_published, _ = _priced(prompt_dollars)
    completion_price, completion_published, _ = _priced(completion_dollars)
    return {
        "prompt_price_microdollars_per_million_tokens": prompt_price,
        "completion_price_microdollars_per_million_tokens": completion_price,
        "published_prompt_price_microdollars_per_million_tokens": prompt_published,
        "published_completion_price_microdollars_per_million_tokens": completion_published,
    }


def _customer_price_from_dollars_per_token(price_per_token: str) -> tuple[int, int, int]:
    """Variant for OpenRouter-shaped inputs (dollars/token strings).
    Returns the same triple as `_priced`."""
    if not price_per_token:
        return _PRICE_FLOOR_MICRODOLLARS_PER_M, _PRICE_FLOOR_MICRODOLLARS_PER_M, 0
    try:
        per_token = Decimal(str(price_per_token))
    except Exception:  # noqa: BLE001 — malformed snapshot rows are dropped to floor.
        return _PRICE_FLOOR_MICRODOLLARS_PER_M, _PRICE_FLOOR_MICRODOLLARS_PER_M, 0
    cost = int(
        (per_token * MICRODOLLARS_PER_DOLLAR * TOKENS_PER_MILLION).to_integral_value()
    )
    customer = _customer_price(cost)
    return customer, customer, cost


PROVIDERS: dict[str, Provider] = {
    "trustedrouter": Provider(
        slug="trustedrouter",
        name="TrustedRouter",
        supports_messages=True,
        supports_embeddings=False,
        supports_prepaid=True,
        supports_byok=True,
    ),
    "vertex": Provider(
        slug="vertex",
        name="Google Vertex",
        supports_messages=True,
        supports_embeddings=True,
        supports_prepaid=True,
        supports_byok=False,
    ),
    "anthropic": Provider(slug="anthropic", name="Anthropic", supports_messages=True, supports_prepaid=True),
    "openai": Provider(slug="openai", name="OpenAI", supports_embeddings=True, supports_prepaid=True),
    "gemini": Provider(slug="gemini", name="Gemini", supports_embeddings=True, supports_prepaid=True),
    "cerebras": Provider(slug="cerebras", name="Cerebras", supports_prepaid=True),
    "deepseek": Provider(slug="deepseek", name="DeepSeek", supports_prepaid=True),
    "mistral": Provider(slug="mistral", name="Mistral", supports_prepaid=True),
    "kimi": Provider(slug="kimi", name="Kimi", supports_prepaid=True),
    "zai": Provider(slug="zai", name="Z.AI", supports_prepaid=True),
}


AUTO_MODEL_ID = "trustedrouter/auto"
DEFAULT_AUTO_MODEL_ORDER = [
    "anthropic/claude-opus-4.7",
    "anthropic/claude-3-5-sonnet",
    "openai/gpt-4o-mini",
    "google/gemini-1.5-flash",
    "deepseek/deepseek-v4-flash",
    "kimi/kimi-k2.6",
    "mistral/mistral-small-2603",
    "cerebras/llama3.1-8b",
]


MODELS: dict[str, Model] = {
    AUTO_MODEL_ID: Model(
        id=AUTO_MODEL_ID,
        name="TrustedRouter Auto",
        provider="trustedrouter",
        context_length=200_000,
        supports_messages=False,
        prepaid_available=True,
        byok_available=True,
        # Auto's intrinsic price is 0 — billing happens at the chosen
        # candidate's price. The /v1/models shape derives a min/max range
        # from the candidate set so the dashboard doesn't show $0.
        prompt_price_microdollars_per_million_tokens=0,
        completion_price_microdollars_per_million_tokens=0,
        published_prompt_price_microdollars_per_million_tokens=0,
        published_completion_price_microdollars_per_million_tokens=0,
    ),
    "anthropic/claude-opus-4.7": Model(
        id="anthropic/claude-opus-4.7",
        name="Claude Opus 4.7",
        # Default to direct Anthropic (api.anthropic.com). Users who want
        # Vertex routing can pin per-request with provider.only=["vertex"].
        # Was "vertex" historically; switched after 2026-05-04 because the
        # GCP project doesn't yet have Anthropic-on-Vertex quota approved.
        provider="anthropic",
        context_length=200_000,
        supports_messages=True,
        prepaid_available=True,
        byok_available=False,
        **_hand_priced("5", "25"),
    ),
    "anthropic/claude-3-5-sonnet": Model(
        id="anthropic/claude-3-5-sonnet",
        name="Claude 3.5 Sonnet",
        provider="anthropic",
        context_length=200_000,
        supports_messages=True,
        prepaid_available=True,
        byok_available=True,
        **_hand_priced("3", "15"),
    ),
    "openai/gpt-4o-mini": Model(
        id="openai/gpt-4o-mini",
        name="GPT-4o mini",
        provider="openai",
        context_length=128_000,
        supports_embeddings=True,
        prepaid_available=True,
        byok_available=True,
        **_hand_priced("1", "4"),
    ),
    "vertex/gemini-2.5-flash": Model(
        id="vertex/gemini-2.5-flash",
        name="Gemini 2.5 Flash on Vertex",
        provider="vertex",
        upstream_id="google/gemini-2.5-flash",
        context_length=1_000_000,
        supports_embeddings=True,
        prepaid_available=True,
        byok_available=False,
        **_hand_priced("0.30", "2.50"),
    ),
    "google/gemini-1.5-flash": Model(
        id="google/gemini-1.5-flash",
        name="Gemini 1.5 Flash",
        provider="gemini",
        context_length=1_000_000,
        supports_embeddings=True,
        prepaid_available=True,
        byok_available=True,
        **_hand_priced("1", "3"),
    ),
    "deepseek/deepseek-v4-flash": Model(
        id="deepseek/deepseek-v4-flash",
        name="DeepSeek V4 Flash",
        provider="deepseek",
        context_length=1_000_000,
        prepaid_available=True,
        byok_available=True,
        **_hand_priced("0.14", "0.28"),
    ),
    "deepseek/deepseek-v4-pro": Model(
        id="deepseek/deepseek-v4-pro",
        name="DeepSeek V4 Pro",
        provider="deepseek",
        context_length=1_000_000,
        prepaid_available=True,
        byok_available=True,
        **_hand_priced("0.435", "0.87"),
    ),
    "mistral/mistral-small-2603": Model(
        id="mistral/mistral-small-2603",
        name="Mistral Small 4",
        provider="mistral",
        context_length=256_000,
        prepaid_available=True,
        byok_available=True,
        **_hand_priced("0.15", "0.60"),
    ),
    "mistral/mistral-medium-3-5": Model(
        id="mistral/mistral-medium-3-5",
        name="Mistral Medium 3.5",
        provider="mistral",
        context_length=256_000,
        prepaid_available=True,
        byok_available=True,
        **_hand_priced("1.50", "7.50"),
    ),
    "kimi/kimi-k2.6": Model(
        id="kimi/kimi-k2.6",
        name="Kimi K2.6",
        provider="kimi",
        upstream_id="kimi-k2.6",
        context_length=256_000,
        prepaid_available=True,
        byok_available=True,
        **_hand_priced("0.95", "4.00"),
    ),
    "kimi/kimi-k2.5": Model(
        id="kimi/kimi-k2.5",
        name="Kimi K2.5",
        provider="kimi",
        upstream_id="kimi-k2.5",
        context_length=256_000,
        prepaid_available=True,
        byok_available=True,
        **_hand_priced("0.60", "3.00"),
    ),
    "cerebras/llama3.1-8b": Model(
        id="cerebras/llama3.1-8b",
        name="Llama 3.1 8B on Cerebras",
        provider="cerebras",
        context_length=8_192,
        prepaid_available=True,
        byok_available=True,
        **_hand_priced("1", "1"),
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
        if model.id == AUTO_MODEL_ID:
            continue
        provider = PROVIDERS[model.provider]
        if model.prepaid_available:
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

# OpenRouter publishes models as `{author}/{slug}` where author maps onto
# one of TR's keyed providers. This drops the `Model.provider` (publisher)
# field for an ingested entry.
_AUTHOR_TO_PROVIDER_SLUG: dict[str, str] = {
    "anthropic": "anthropic",
    "openai": "openai",
    "google": "gemini",
    "vertex": "vertex",
    "cerebras": "cerebras",
    "deepseek": "deepseek",
    "mistral": "mistral",
    "mistralai": "mistral",
    "moonshot": "kimi",
    "moonshotai": "kimi",
    "z-ai": "zai",
    "zhipu": "zai",
    "zhipuai": "zai",
}


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
    catalog uniformly applies the cost+10% / $0.10/M-floor formula."""
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
        raw_endpoints = [
            e for e in (raw_model.get("endpoints") or []) if isinstance(e, dict)
        ]
        if not raw_endpoints:
            continue
        publisher = _author_provider(model_id, raw_endpoints)
        if publisher is None:
            continue

        per_endpoint_prices: list[tuple[int, int, str, dict[str, Any]]] = []
        for raw_ep in raw_endpoints:
            slug = raw_ep.get("tr_provider_slug")
            if not isinstance(slug, str) or slug not in PROVIDERS:
                continue
            pricing = raw_ep.get("pricing") or {}
            prompt_price, _, _ = _customer_price_from_dollars_per_token(
                str(pricing.get("prompt") or "0")
            )
            completion_price, _, _ = _customer_price_from_dollars_per_token(
                str(pricing.get("completion") or "0")
            )
            per_endpoint_prices.append((prompt_price, completion_price, slug, raw_ep))

        if not per_endpoint_prices:
            continue

        # Model-level price = cheapest endpoint, so the /v1/models top-level
        # `pricing.prompt` doesn't lie when multiple providers serve the
        # same model at different tiers.
        cheapest_prompt = min(p for p, _c, _s, _e in per_endpoint_prices)
        cheapest_completion = min(c for _p, c, _s, _e in per_endpoint_prices)

        ctx_candidates = [
            int(raw_model.get("context_length") or 0),
            *(int(ep.get("context_length") or 0) for _p, _c, _s, ep in per_endpoint_prices),
        ]
        context_length = max(ctx_candidates) or 0

        models[model_id] = Model(
            id=model_id,
            name=str(raw_model.get("name") or model_id),
            provider=publisher,
            context_length=context_length,
            supports_chat=True,
            prepaid_available=True,
            byok_available=PROVIDERS[publisher].supports_byok,
            prompt_price_microdollars_per_million_tokens=cheapest_prompt,
            completion_price_microdollars_per_million_tokens=cheapest_completion,
            published_prompt_price_microdollars_per_million_tokens=cheapest_prompt,
            published_completion_price_microdollars_per_million_tokens=cheapest_completion,
        )

        for prompt_price, completion_price, slug, raw_ep in per_endpoint_prices:
            upstream_id = str(raw_ep.get("model_id") or model_id)
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
                )

    return models, endpoints


_INGESTED_MODELS, _INGESTED_ENDPOINTS = _ingested_models_and_endpoints()
# Snapshot of what's hand-coded BEFORE merging ingested models — used
# below to skip ingested endpoints for any model that was already
# hand-coded (those carry curated BYOK/messages flags whose endpoint
# shape we don't want to disrupt). Ingested-only models still get
# their full per-provider endpoint fan-out.
_HAND_CODED_MODEL_IDS = frozenset(MODELS.keys())

# Hand-coded entries win on ID collision so their operational flags
# (BYOK gating, embeddings, messages) survive intact — those reflect
# real upstream constraints (e.g. anthropic-on-Vertex quota, OpenRouter
# slug naming differences from TR's own slugs). Ingested entries fill in
# everything new (the 13 z-ai models, the 3 moonshotai models, etc).
# Pricing for the surviving hand-coded entries can be tightened in a
# follow-up PR — that's a smaller, more reviewable change.
for _ingested_id, _ingested_model in _INGESTED_MODELS.items():
    MODELS.setdefault(_ingested_id, _ingested_model)


MODEL_ENDPOINTS: dict[str, ModelEndpoint] = _build_endpoints(MODELS)
# Ingested per-provider endpoints fill in only models added via the
# ingest path. For collisions (model present in both hand-coded and
# ingested), keep the hand-coded endpoint shape so operational gating
# (BYOK availability, single-provider routing) doesn't silently flip.
for _endpoint_id, _ingested_endpoint in _INGESTED_ENDPOINTS.items():
    if _ingested_endpoint.model_id in _HAND_CODED_MODEL_IDS:
        continue
    MODEL_ENDPOINTS[_endpoint_id] = _ingested_endpoint


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


def _auto_price_range(
    attr: str,
) -> tuple[int, int]:
    """Return (min, max) of the requested price attribute across the
    Auto model's candidate set. Auto itself has no intrinsic price —
    the request lands on whatever model the router picks — so we
    surface the range so /v1/models doesn't show a misleading $0."""
    candidates = auto_candidate_models()
    values = [
        getattr(c, attr)
        for c in candidates
        if getattr(c, attr, 0) > 0
    ]
    if not values:
        return (0, 0)
    return (min(values), max(values))


def model_to_openrouter_shape(model: Model) -> dict[str, object]:
    provider = PROVIDERS[model.provider]
    is_auto = model.id == AUTO_MODEL_ID
    endpoints = endpoints_for_model(model.id)
    prepaid_available = any(endpoint.usage_type == "Credits" for endpoint in endpoints) or model.prepaid_available
    byok_available = any(endpoint.usage_type == "BYOK" for endpoint in endpoints) or (
        model.byok_available and PROVIDERS[model.provider].supports_byok
    )

    # For Auto, derive prompt/completion price from the candidate range
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
    if is_auto:
        prompt_min, prompt_max = _auto_price_range(
            "prompt_price_microdollars_per_million_tokens"
        )
        completion_min, completion_max = _auto_price_range(
            "completion_price_microdollars_per_million_tokens"
        )
        pub_prompt_min, pub_prompt_max = _auto_price_range(
            "published_prompt_price_microdollars_per_million_tokens"
        )
        pub_completion_min, pub_completion_max = _auto_price_range(
            "published_completion_price_microdollars_per_million_tokens"
        )

    pricing: dict[str, str] = {
        "prompt": microdollars_per_million_tokens_to_token_decimal(prompt_min),
        "completion": microdollars_per_million_tokens_to_token_decimal(completion_min),
    }
    if is_auto and (prompt_max != prompt_min or completion_max != completion_min):
        pricing["prompt_max"] = microdollars_per_million_tokens_to_token_decimal(prompt_max)
        pricing["completion_max"] = microdollars_per_million_tokens_to_token_decimal(
            completion_max
        )

    tr_block: dict[str, object] = {
        "provider": model.provider,
        "prepaid_available": prepaid_available,
        "byok_available": byok_available,
        "attested_gateway": provider.attested_gateway,
        "stores_content": provider.stores_content,
        "prompt_price_microdollars_per_million_tokens": prompt_min,
        "completion_price_microdollars_per_million_tokens": completion_min,
        "published_prompt_price_microdollars_per_million_tokens": pub_prompt_min,
        "published_completion_price_microdollars_per_million_tokens": pub_completion_min,
        # Uniform pricing means the customer pays the headline rate — no
        # secret 1¢/M discount layered on top. Field kept for OpenRouter
        # consumer compat, but always zero.
        "discount_microdollars_per_million_tokens": 0,
        "auto_candidates": [c.id for c in auto_candidate_models()] if is_auto else None,
        "endpoints": [
            {
                "id": endpoint.id,
                "provider": endpoint.provider,
                "provider_name": PROVIDERS[endpoint.provider].name,
                "usage_type": endpoint.usage_type,
                "upstream_id": endpoint.upstream_id,
                "attested_gateway": PROVIDERS[endpoint.provider].attested_gateway,
                "stores_content": PROVIDERS[endpoint.provider].stores_content,
            }
            for endpoint in endpoints
        ],
    }
    if is_auto:
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
        "architecture": {"modality": "text->text", "tokenizer": "unknown", "instruct_type": None},
        "pricing": pricing,
        "top_provider": {
            "context_length": model.context_length,
            "max_completion_tokens": None,
            "is_moderated": False,
        },
        "per_request_limits": None,
        "trustedrouter": tr_block,
    }
