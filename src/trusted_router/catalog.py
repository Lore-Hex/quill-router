from __future__ import annotations

from dataclasses import dataclass

from trusted_router.money import (
    MICRODOLLARS_PER_CENT,
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


def _one_cent_less_per_million(published_dollars_per_million: str) -> int:
    return max(0, dollars_to_microdollars(published_dollars_per_million) - MICRODOLLARS_PER_CENT)


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
    "anthropic": Provider(slug="anthropic", name="Anthropic", supports_messages=True),
    "openai": Provider(slug="openai", name="OpenAI", supports_embeddings=True),
    "gemini": Provider(slug="gemini", name="Gemini", supports_embeddings=True),
    "cerebras": Provider(slug="cerebras", name="Cerebras"),
    "deepseek": Provider(slug="deepseek", name="DeepSeek"),
    "mistral": Provider(slug="mistral", name="Mistral"),
    "kimi": Provider(slug="kimi", name="Kimi", supports_prepaid=True),
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
        prompt_price_microdollars_per_million_tokens=0,
        completion_price_microdollars_per_million_tokens=0,
        published_prompt_price_microdollars_per_million_tokens=MICRODOLLARS_PER_CENT,
        published_completion_price_microdollars_per_million_tokens=MICRODOLLARS_PER_CENT,
    ),
    "anthropic/claude-opus-4.7": Model(
        id="anthropic/claude-opus-4.7",
        name="Claude Opus 4.7",
        provider="vertex",
        context_length=200_000,
        supports_messages=True,
        prepaid_available=True,
        byok_available=False,
        prompt_price_microdollars_per_million_tokens=_one_cent_less_per_million("5"),
        completion_price_microdollars_per_million_tokens=_one_cent_less_per_million("25"),
        published_prompt_price_microdollars_per_million_tokens=dollars_to_microdollars("5"),
        published_completion_price_microdollars_per_million_tokens=dollars_to_microdollars("25"),
    ),
    "anthropic/claude-3-5-sonnet": Model(
        id="anthropic/claude-3-5-sonnet",
        name="Claude 3.5 Sonnet",
        provider="anthropic",
        context_length=200_000,
        supports_messages=True,
        prompt_price_microdollars_per_million_tokens=_one_cent_less_per_million("3"),
        completion_price_microdollars_per_million_tokens=_one_cent_less_per_million("15"),
        published_prompt_price_microdollars_per_million_tokens=dollars_to_microdollars("3"),
        published_completion_price_microdollars_per_million_tokens=dollars_to_microdollars("15"),
    ),
    "openai/gpt-4o-mini": Model(
        id="openai/gpt-4o-mini",
        name="GPT-4o mini",
        provider="openai",
        context_length=128_000,
        supports_embeddings=True,
        prompt_price_microdollars_per_million_tokens=_one_cent_less_per_million("1"),
        completion_price_microdollars_per_million_tokens=_one_cent_less_per_million("4"),
        published_prompt_price_microdollars_per_million_tokens=dollars_to_microdollars("1"),
        published_completion_price_microdollars_per_million_tokens=dollars_to_microdollars("4"),
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
        prompt_price_microdollars_per_million_tokens=_one_cent_less_per_million("0.30"),
        completion_price_microdollars_per_million_tokens=_one_cent_less_per_million("2.50"),
        published_prompt_price_microdollars_per_million_tokens=dollars_to_microdollars("0.30"),
        published_completion_price_microdollars_per_million_tokens=dollars_to_microdollars("2.50"),
    ),
    "google/gemini-1.5-flash": Model(
        id="google/gemini-1.5-flash",
        name="Gemini 1.5 Flash",
        provider="gemini",
        context_length=1_000_000,
        supports_embeddings=True,
        prompt_price_microdollars_per_million_tokens=_one_cent_less_per_million("1"),
        completion_price_microdollars_per_million_tokens=_one_cent_less_per_million("3"),
        published_prompt_price_microdollars_per_million_tokens=dollars_to_microdollars("1"),
        published_completion_price_microdollars_per_million_tokens=dollars_to_microdollars("3"),
    ),
    "deepseek/deepseek-v4-flash": Model(
        id="deepseek/deepseek-v4-flash",
        name="DeepSeek V4 Flash",
        provider="deepseek",
        context_length=1_000_000,
        prompt_price_microdollars_per_million_tokens=_one_cent_less_per_million("0.14"),
        completion_price_microdollars_per_million_tokens=_one_cent_less_per_million("0.28"),
        published_prompt_price_microdollars_per_million_tokens=dollars_to_microdollars("0.14"),
        published_completion_price_microdollars_per_million_tokens=dollars_to_microdollars("0.28"),
    ),
    "deepseek/deepseek-v4-pro": Model(
        id="deepseek/deepseek-v4-pro",
        name="DeepSeek V4 Pro",
        provider="deepseek",
        context_length=1_000_000,
        prompt_price_microdollars_per_million_tokens=_one_cent_less_per_million("0.435"),
        completion_price_microdollars_per_million_tokens=_one_cent_less_per_million("0.87"),
        published_prompt_price_microdollars_per_million_tokens=dollars_to_microdollars("0.435"),
        published_completion_price_microdollars_per_million_tokens=dollars_to_microdollars("0.87"),
    ),
    "mistral/mistral-small-2603": Model(
        id="mistral/mistral-small-2603",
        name="Mistral Small 4",
        provider="mistral",
        context_length=256_000,
        prompt_price_microdollars_per_million_tokens=_one_cent_less_per_million("0.15"),
        completion_price_microdollars_per_million_tokens=_one_cent_less_per_million("0.60"),
        published_prompt_price_microdollars_per_million_tokens=dollars_to_microdollars("0.15"),
        published_completion_price_microdollars_per_million_tokens=dollars_to_microdollars("0.60"),
    ),
    "mistral/mistral-medium-3-5": Model(
        id="mistral/mistral-medium-3-5",
        name="Mistral Medium 3.5",
        provider="mistral",
        context_length=256_000,
        prompt_price_microdollars_per_million_tokens=_one_cent_less_per_million("1.50"),
        completion_price_microdollars_per_million_tokens=_one_cent_less_per_million("7.50"),
        published_prompt_price_microdollars_per_million_tokens=dollars_to_microdollars("1.50"),
        published_completion_price_microdollars_per_million_tokens=dollars_to_microdollars("7.50"),
    ),
    "kimi/kimi-k2.6": Model(
        id="kimi/kimi-k2.6",
        name="Kimi K2.6",
        provider="kimi",
        upstream_id="kimi-k2.6",
        context_length=256_000,
        prepaid_available=True,
        byok_available=True,
        prompt_price_microdollars_per_million_tokens=_one_cent_less_per_million("0.95"),
        completion_price_microdollars_per_million_tokens=_one_cent_less_per_million("4.00"),
        published_prompt_price_microdollars_per_million_tokens=dollars_to_microdollars("0.95"),
        published_completion_price_microdollars_per_million_tokens=dollars_to_microdollars("4.00"),
    ),
    "kimi/kimi-k2.5": Model(
        id="kimi/kimi-k2.5",
        name="Kimi K2.5",
        provider="kimi",
        upstream_id="kimi-k2.5",
        context_length=256_000,
        prepaid_available=True,
        byok_available=True,
        prompt_price_microdollars_per_million_tokens=_one_cent_less_per_million("0.60"),
        completion_price_microdollars_per_million_tokens=_one_cent_less_per_million("3.00"),
        published_prompt_price_microdollars_per_million_tokens=dollars_to_microdollars("0.60"),
        published_completion_price_microdollars_per_million_tokens=dollars_to_microdollars("3.00"),
    ),
    "cerebras/llama3.1-8b": Model(
        id="cerebras/llama3.1-8b",
        name="Llama 3.1 8B on Cerebras",
        provider="cerebras",
        context_length=8_192,
        prompt_price_microdollars_per_million_tokens=_one_cent_less_per_million("1"),
        completion_price_microdollars_per_million_tokens=_one_cent_less_per_million("1"),
        published_prompt_price_microdollars_per_million_tokens=dollars_to_microdollars("1"),
        published_completion_price_microdollars_per_million_tokens=dollars_to_microdollars("1"),
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


MODEL_ENDPOINTS: dict[str, ModelEndpoint] = _build_endpoints(MODELS)


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
        "discount_microdollars_per_million_tokens": MICRODOLLARS_PER_CENT,
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
