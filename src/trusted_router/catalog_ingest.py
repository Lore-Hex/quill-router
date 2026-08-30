"""Import-time catalog ingestion: builds the model + endpoint registries from
the OpenRouter snapshot and the provider-native manifests, and seeds embeddings.

Extracted from catalog.py (#38). Pure producers — they take the static data +
pricing helpers and RETURN model/endpoint dicts; they never read the live
MODELS/MODEL_ENDPOINTS registries (catalog.py owns those and does the import-time
merge). No dependency on catalog.py, so no import cycle.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from trusted_router.catalog_capabilities import (
    manifest_supported_parameters,
    union_supported_parameters,
)
from trusted_router.catalog_data import (
    _EMBEDDING_SPECS,
    _PROVIDER_SERVED_MODEL_ALLOWLIST,
    _PROVIDER_UNSERVED_CREDITS_MODELS,
    _UNSERVED_CREDITS_MODELS,
    GATEWAY_PREPAID_PROVIDER_SLUGS,
    META_MODEL_IDS,
    PROVIDERS,
    Model,
    ModelEndpoint,
    _EmbeddingSpec,
)
from trusted_router.pricing import (
    PriceTier,
    _as_positive_int,
    _customer_price,
    _customer_price_from_dollars_per_token,
    _flat_tier,
    _optional_customer_price_from_dollars_per_token,
    _priced,
    _provider_manifest_customer_price,
    _provider_manifest_optional_price_cost,
    _provider_manifest_price_cost,
    _provider_manifest_price_scale,
    _provider_manifest_price_tiers,
    _read_pricing_tiers,
    customer_fixed_price_microdollars,
)
from trusted_router.provider_contracts import (
    provider_model_operator_held,
    provider_model_uses_passthrough_retail_price,
)
from trusted_router.provider_lifecycle import provider_model_retired
from trusted_router.provider_manifest_policy import (
    EXPIRED_PROVIDER_MANIFEST as _EXPIRED_PROVIDER_MANIFEST,
)
from trusted_router.provider_manifest_policy import (
    EXPIRING_PROVIDER_MANIFEST_SLUGS,
    RUNTIME_ONLY_PROVIDER_MANIFEST_SLUGS,
)
from trusted_router.provider_manifest_policy import (
    provider_manifest_valid_until as _provider_manifest_valid_until,
)


def _positive_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if parsed > 0 else None


_SUPPORTED_GATEWAY_MODALITIES = frozenset({"text", "image"})


def _modalities(value: object, *, default: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(value, list):
        return default
    normalized = tuple(
        dict.fromkeys(
            item.strip().lower()
            for item in value
            if (
                isinstance(item, str)
                and item.strip()
                and item.strip().lower() in _SUPPORTED_GATEWAY_MODALITIES
            )
        )
    )
    return normalized or default


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
        supported_parameters=union_supported_parameters(
            model.supported_parameters,
            manifest_supported_parameters(
                {},
                supports_chat=model.supports_chat,
                supports_embeddings=model.supports_embeddings,
            ),
        ),
        prompt_price_microdollars_per_million_tokens=model.prompt_price_microdollars_per_million_tokens,
        completion_price_microdollars_per_million_tokens=model.completion_price_microdollars_per_million_tokens,
        published_prompt_price_microdollars_per_million_tokens=model.published_prompt_price_microdollars_per_million_tokens,
        published_completion_price_microdollars_per_million_tokens=model.published_completion_price_microdollars_per_million_tokens,
        request_price_microdollars=model.request_price_microdollars,
    )


def _build_endpoints(models: dict[str, Model]) -> dict[str, ModelEndpoint]:
    endpoints: dict[str, ModelEndpoint] = {}
    for model in models.values():
        # Async media has provider-specific request shapes and fixed per-job
        # quotes. Its endpoints are registered explicitly by the media catalog;
        # synthesizing a token-priced chat endpoint here creates a duplicate
        # route with the wrong upstream model id.
        if model.id in META_MODEL_IDS or model.supports_video:
            continue
        provider = PROVIDERS[model.provider]
        if model.prepaid_available and provider.slug in GATEWAY_PREPAID_PROVIDER_SLUGS:
            endpoint = _endpoint(model, usage_type="Credits")
            endpoints[endpoint.id] = endpoint
        if model.byok_available and provider.supports_byok:
            endpoint = _endpoint(model, usage_type="BYOK")
            endpoints[endpoint.id] = endpoint
    return endpoints


_INGEST_PATH = Path(__file__).parent / "data" / "openrouter_snapshot.json"

_PROVIDER_MODELS_DIR = Path(__file__).parent / "data" / "provider_models"

# These provider catalogs require credentials that are intentionally unavailable
# to GitHub Actions until the operator explicitly approves that trust expansion.
# Their routes fail closed at manifest expiry without freezing unrelated catalog
# updates. Fresh authenticated discovery advances the deadline automatically.
_RUNTIME_ONLY_PROVIDER_MANIFEST_SLUGS = RUNTIME_ONLY_PROVIDER_MANIFEST_SLUGS
_EXPIRING_PROVIDER_MANIFEST_SLUGS = EXPIRING_PROVIDER_MANIFEST_SLUGS


def _apply_provider_manifest_expiry(
    endpoints: dict[str, ModelEndpoint],
) -> dict[str, ModelEndpoint]:
    """Attach one provider-scoped deadline to every manifest-backed route.

    Some media routes are installed statically after supplemental ingestion,
    so applying the policy at the final endpoint boundary is what guarantees
    chat, image, video, Credits, and BYOK routes all fail closed together.
    """
    deadlines: dict[str, datetime] = {}
    for provider_slug in _EXPIRING_PROVIDER_MANIFEST_SLUGS:
        path = _PROVIDER_MODELS_DIR / f"{provider_slug}.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError):
            deadlines[provider_slug] = _EXPIRED_PROVIDER_MANIFEST
            continue
        deadlines[provider_slug] = (
            _provider_manifest_valid_until(provider_slug, raw) or _EXPIRED_PROVIDER_MANIFEST
        )

    return {
        endpoint_id: (
            replace(endpoint, catalog_valid_until=deadlines[endpoint.provider])
            if endpoint.provider in deadlines
            else endpoint
        )
        for endpoint_id, endpoint in endpoints.items()
    }


# These providers publish authoritative model catalogs. Their generated
# manifests, rather than OpenRouter's provider inventory, determine which
# generic provider routes exist for both prepaid and BYOK. This prevents dark,
# dedicated-only, or retired model IDs from reappearing through the shared
# snapshot, while each hourly refresh can add a new route without a second
# hand-maintained Python allowlist.
_AUTHORITATIVE_PROVIDER_MANIFEST_SLUGS = frozenset(
    {
        "cerebras",
        "cloudflare-workers-ai",
        "crusoe",
        "anthropic",
        "baseten",
        "friendli",
        "google-ai-studio",
        "grok",
        "kimi",
        "novita",
        "near-ai",
        "phala",
        "telnyx",
        "together",
        "wafer",
        "engy",
        "pearl",
        "stepfun",
        "relace",
        "recraft",
        "bfl",
        "decart",
        "fal",
        "nvidia-nim",
        "wandb",
        "nscale",
        "databricks",
        "zero-g",
        "azure",
        "scaleway",
        "featherless",
        "sakana",
        "jina",
        "openrouter",
    }
)


def _authoritative_provider_model_ids(provider_slug: str) -> frozenset[str]:
    """Return fail-closed route model IDs for an authoritative manifest.

    Explicit embedding specs remain eligible alongside dynamically discovered
    embedding rows. A missing or malformed manifest therefore disables dynamic
    routes without accidentally disabling a separately verified static embedding.
    """
    allowed = {
        str(spec["id"]) for spec in _EMBEDDING_SPECS if spec.get("provider") == provider_slug
    }
    path = _PROVIDER_MODELS_DIR / f"{provider_slug}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return frozenset(allowed)
    raw_models = raw.get("models") if isinstance(raw, dict) else None
    if not isinstance(raw_models, list):
        return frozenset(allowed)
    for row in raw_models:
        if not isinstance(row, dict) or row.get("routable") is False:
            continue
        if row.get("model_type") not in (None, "chat", "image", "video", "embedding"):
            continue
        endpoint_types = {str(item) for item in (row.get("endpoints") or [])}
        if not endpoint_types.intersection({"chat/completions", "images", "videos", "embeddings"}):
            continue
        model_id = row.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        if provider_model_operator_held(provider_slug, model_id):
            continue
        upstream_id = str(row.get("upstream_id") or model_id)
        if provider_model_retired(provider_slug, model_id, upstream_id):
            continue
        allowed.add(model_id)
    return frozenset(allowed)


_AUTHOR_TO_PROVIDER_SLUG: dict[str, str] = {
    "anthropic": "anthropic",
    "openai": "openai",
    # The local/test provider client uses the native Gemini API adapter, while
    # production routing remains endpoint-based and can still prefer Vertex.
    # Keep the model-level default on AI Studio so live local calls never fall
    # through to a synthetic response merely because Vertex is the publisher.
    "google": "google-ai-studio",
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
    "xiaomi": "xiaomi",
    "phala": "phala",
    "zero-g": "zero-g",
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
    # atlas-cloud (onboarded #244) advertises these openai/* models but its
    # router returns HTTP 400 "router not found" — 100% synthetic failure / 0
    # success as of 2026-07-20. Provider-scoped: the same model ids on
    # first-party OpenAI and other providers are unaffected, and atlas-cloud's
    # open-weight catalog plus the six openai models it actually serves
    # (gpt-4.1-mini, gpt-5.4-mini, gpt-5.5, gpt-5.6-luna, gpt-5.6-sol,
    # gpt-5.6-terra) remain routable.
    # Parasail retired Mistral Small 3.2 on 2026-07-24. The evidence is a
    # clean boundary rather than a failure rate: the route's last success was
    # 2026-07-24 08:16 UTC and EVERY subsequent probe (14 of them, through
    # 2026-07-26) returned an upstream 404. A model that works and then stops,
    # with no interleaving, is a retirement; a flaky one alternates.
    # Provider-scoped, and deliberately narrow: parasail's other routes are
    # healthy (gemma-3-27b-it 34/34, gpt-oss-120b 24/24,
    # llama-4-maverick 24/24), so this is one model going away, not a provider
    # outage.
    "parasail": frozenset(
        {
            "mistralai/mistral-small-3.2-24b-instruct",
            "mistral-small-3.2-24b-instruct",
        }
    ),
    "atlas-cloud": frozenset(
        {
            "openai/gpt-4.1",
            "gpt-4.1",
            "openai/gpt-4.1-nano",
            "gpt-4.1-nano",
            "openai/gpt-4o",
            "gpt-4o",
            "openai/gpt-4o-mini",
            "gpt-4o-mini",
            "openai/gpt-5",
            "gpt-5",
            "openai/gpt-5-chat",
            "gpt-5-chat",
            "openai/gpt-5-codex",
            "gpt-5-codex",
            "openai/gpt-5-mini",
            "gpt-5-mini",
            "openai/gpt-5-nano",
            "gpt-5-nano",
            "openai/gpt-5-pro",
            "gpt-5-pro",
            "openai/gpt-5.1",
            "gpt-5.1",
            "openai/gpt-5.1-chat",
            "gpt-5.1-chat",
            "openai/gpt-5.1-codex",
            "gpt-5.1-codex",
            "openai/gpt-5.1-codex-mini",
            "gpt-5.1-codex-mini",
            "openai/gpt-5.2",
            "gpt-5.2",
            "openai/gpt-5.2-chat",
            "gpt-5.2-chat",
            "openai/gpt-5.2-codex",
            "gpt-5.2-codex",
            # Confirmed 2026-07-26: 11/11 synthetic failures, HTTP 400
            # "router not found" — same phantom as its siblings. It was held
            # back from the original sweep because it had no samples yet.
            "openai/gpt-5.1-codex-max",
            "gpt-5.1-codex-max",
            "openai/gpt-5.3-codex",
            "gpt-5.3-codex",
            "openai/o1",
            "o1",
            "openai/o3",
            "o3",
            "openai/o3-mini",
            "o3-mini",
            "openai/o3-pro",
            "o3-pro",
            "openai/o4-mini",
            "o4-mini",
        }
    ),
    # Xiaomi retired the MiMo V2 family upstream on 2026-06-29; these manifest
    # rows are historical fallback metadata and have shown 100% probe failure
    # since 2026-06-29. Keep provider-scoped: V2.5 Xiaomi routes remain alive.
    "xiaomi": frozenset(
        {
            "xiaomi/mimo-v2-flash",
            "mimo-v2-flash",
            "xiaomi/mimo-v2-pro",
            "mimo-v2-pro",
        }
    ),
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
    # Tinfoil notified users that GLM 5.1 and Qwen3-VL-30B are deprecated on
    # 2026-06-22. Keep this provider-scoped: GLM 5.1 / Qwen routes on other
    # providers are unaffected, while Tinfoil callers should move to glm-5-2
    # and gemma4-31b respectively.
    "tinfoil": frozenset(
        {
            "z-ai/glm-5.1",
            "glm-5-1",
            "qwen/qwen3-vl-30b",
            "qwen/qwen3-vl-30b-a3b-instruct",
            "qwen3-vl-30b",
        }
    ),
    # Novita notified customers that these DeepSeek and Qwen model APIs retire
    # on 2026-07-01 00:00 UTC. Replacement routes are deepseek-v4-flash,
    # qwen3.6-27b, and qwen3.6-35b-a3b. This is provider-scoped: the same
    # model ids on other providers remain routable if those providers still
    # serve them.
    "novita": frozenset(
        {
            "deepseek/deepseek-r1-distill-qwen-14b",
            "deepseek/deepseek-r1-distill-qwen-32b",
            "qwen/qwen3-14b",
            "qwen/qwen3-30b-a3b",
            "qwen/qwen3-30b-a3b-instruct-2507",
            "qwen/qwen3-30b-a3b-thinking-2507",
            "qwen/qwen3-32b",
            "qwen/qwen3-8b",
            "qwen/qwen3-next-80b-a3b-thinking",
            "qwen/qwen3-vl-30b-a3b-thinking",
            "qwen/qwen3-vl-32b-instruct",
            "qwen/qwen3-vl-32b-thinking",
            "qwen/qwen3-vl-8b-instruct",
            "qwen/qwen3-vl-8b-thinking",
            # 100% MODEL_NOT_AVAILABLE-class probe failures since 2026-06-05.
            "baidu/ernie-4.5-vl-28b-a3b",
            # 100% MODEL_NOT_AVAILABLE-class probe failures since 2026-06-23.
            "meta-llama/llama-3-70b-instruct",
            # route-health first sweep 2026-07-18, 100% failure.
            "zai-org/glm-4.5",
            # route-health 2026-07-18, 100% failure, upstream 404.
            "elephant",
        }
    ),
    # Friendli notified customers that GLM-5 serverless Model APIs stop being
    # supported at 2026-07-03 00:00 UTC. Dedicated endpoints are unaffected, but
    # TrustedRouter's Friendli route is the serverless API, so remove only this
    # provider/model pair from routable candidates. Friendli also dropped
    # Llama 3.3 70B from serverless /models around 2026-06-26; it has shown
    # 100% probe failure since 2026-06-26.
    "friendli": frozenset(
        {
            "z-ai/glm-5",
            "zai-org/GLM-5",
            "meta-llama/llama-3.3-70b-instruct",
            "meta-llama-3.3-70b-instruct",
        }
    ),
    # Google retired the Gemini 3.1 Flash Lite preview id on 2026-07-09; the
    # direct Gemini preview route has shown 100% probe failure since then. GA
    # flash-lite routes on reseller providers are unaffected.
    "google-ai-studio": frozenset(
        {
            "google/gemini-3.1-flash-lite-preview",
            "gemini-3.1-flash-lite-preview",
            # route-health 2026-07-18, 100% failure, upstream 404.
            "google/gemma-3n-e4b-it",
            # Retired by Google 2026-07-26. These read as HTTP 429 on our
            # existing (grandfathered) AI Studio project, which looks like a
            # quota problem and counts against public leaderboard uptime. A
            # probe with a freshly-issued key returns the real cause:
            # 404 "no longer available to new users" / "no longer available".
            # The 429 was masking a deprecation, so rotating the key would not
            # have fixed these — only quarantining them does.
            "google/gemini-2.5-pro",
            "gemini-2.5-pro",
            "google/gemini-2.0-flash-001",
            "gemini-2.0-flash-001",
            "google/gemini-2.0-flash-lite-001",
            "gemini-2.0-flash-lite-001",
            # Verified with a newly uploaded BYOK key on 2026-07-28: the
            # generateContent API returns 404 "no longer available to new
            # users". Keep reseller and Vertex routes for this model intact.
            "google/gemini-2.5-flash-lite",
            "gemini-2.5-flash-lite",
        }
    ),
    "google-vertex": frozenset(
        {
            "google/gemini-3.1-flash-lite-preview",
            "gemini-3.1-flash-lite-preview",
        }
    ),
    # Makora's AMD Llama 3.3 70B FP8 KV row was added on 2026-07-03 but never
    # served a request; probes hang or 502, giving 100% failure since 2026-07-03.
    "makora": frozenset(
        {
            "amd/llama-3.3-70b-instruct-fp8-kv",
            "amd/Llama-3.3-70B-Instruct-FP8-KV",
            # route-health first sweep 2026-07-18, 100% failure.
            "qwen/qwen3.6-27b",
            "openai/gpt-oss-120b",
        }
    ),
    # These resellers list Claude ids their APIs do not actually serve: 100%
    # provider_error in synthetic probes since the first sample, verified
    # 2026-07-17. Anthropic-direct and Lightning routes are unaffected.
    "gmi": frozenset(
        {
            "anthropic/claude-fable-5",
            "anthropic/claude-sonnet-5",
            "anthropic/claude-opus-4.1",
            # route-health first sweep 2026-07-18, 100% failure.
            "x-ai/grok-4.5",
            "anthropic/claude-sonnet-4.5",
            "openai/gpt-5.6-luna",
            "qwen/qwen3.5-27b",
            "google/gemini-3-flash-preview",
        }
    ),
    "deepinfra": frozenset(
        {
            "anthropic/claude-fable-5",
            "anthropic/claude-sonnet-5",
            # route-health flag 2026-07-18 (post-first-sweep): 100% failure over 6 samples.
            "google/gemini-3.5-flash",
            # route-health first sweep 2026-07-18, 100% failure.
            "z-ai/glm-5.1",
            "moonshotai/kimi-k2.7-code",
            "qwen/qwen3-32b",
            # route-health 2026-07-18, 100% failure, upstream 404.
            "nousresearch/hermes-3-llama-3.1-70b",
        }
    ),
    "phala": frozenset(
        {
            "anthropic/claude-sonnet-5",
            "anthropic/claude-opus-4.1",
            # route-health first sweep 2026-07-18, 100% failure.
            "anthropic/claude-opus-4.8",
            "deepseek/deepseek-v4-pro",
        }
    ),
    # route-health first sweep 2026-07-18, 100% failure. These ids may be
    # remappable to newer upstream ids — revisit with per-provider manifest hooks.
    # route-health first sweep 2026-07-18, 100% failure.
    "lightning": frozenset(
        {
            "openai/gpt-5.6-sol",
            "openai/gpt-5.6-luna",
            "openai/gpt-4-turbo-preview",
            # route-health 2026-07-18, 100% failure, upstream provider_error
            # before Lightning attempted an upstream request.
            "openai/gpt-5",
            "openai/gpt-5-mini",
            "openai/o3",
            "openai/o3-mini",
        }
    ),
    # route-health first sweep 2026-07-18, 100% failure. These ids may be
    # remappable to newer upstream ids — revisit with per-provider manifest hooks.
    "kimi": frozenset(
        {
            "moonshotai/kimi-k2",
            "moonshotai/kimi-k2-0905",
            # route-health 2026-07-18, 100% failure, upstream 404.
            "moonshotai/kimi-k2-thinking",
        }
    ),
    # route-health first sweep 2026-07-18, 100% failure.
    "openai": frozenset(
        {
            "openai/gpt-4-turbo-preview",
            "openai/gpt-5.2-chat",
        }
    ),
    # route-health first sweep 2026-07-18, 100% failure.
    "mistral": frozenset(
        {
            "mistralai/mistral-small-24b-instruct-2501",
            # route-health 2026-07-18, 100% failure, upstream 404.
            "mistralai/mixtral-8x22b-instruct",
        }
    ),
    # route-health first sweep 2026-07-18, 100% failure.
    "meta": frozenset(
        {
            "meta/muse-spark-1.1",
        }
    ),
    # route-health first sweep 2026-07-18, 100% failure.
    "deepseek": frozenset(
        {
            "deepseek/deepseek-v3.1-terminus",
            # route-health 2026-07-18, 100% failure, upstream 400.
            "deepseek/deepseek-v3.2-exp",
            "deepseek/deepseek-chat-v3-0324",
            "deepseek/deepseek-r1-distill-llama-70b",
            "deepseek/deepseek-r1-0528",
        }
    ),
}


def _is_provider_deprecated_model(
    provider_slug: str,
    model_id: str,
    upstream_id: str | None,
) -> bool:
    if provider_model_retired(provider_slug, model_id, upstream_id):
        return True
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
    catalog uniformly applies the cost+5.5% / $0.01/M-floor formula."""
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
            cached_price = _optional_customer_price_from_dollars_per_token(
                pricing.get("input_cache_read")
            )
            # Tier-aware pricing: read multi-tier from snapshot if present;
            # otherwise synthesize a single-tier list from the headline rate.
            try:
                tiers = _read_pricing_tiers(pricing, "prompt") or _flat_tier(
                    prompt_price, completion_price, prompt_cached=cached_price
                )
            except ValueError:
                # A malformed tiered snapshot must not collapse to its cheaper
                # low-context headline rate.
                continue
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
        architecture = raw_model.get("architecture")
        if not isinstance(architecture, dict):
            architecture = {}
        supported_parameters = union_supported_parameters(
            *(
                manifest_supported_parameters(raw_ep)
                for _p, _c, _t, _slug, raw_ep in per_endpoint_prices
            )
        )
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
            supported_parameters=supported_parameters,
            input_modalities=_modalities(
                architecture.get("input_modalities"),
                default=("text",),
            ),
            output_modalities=_modalities(
                architecture.get("output_modalities"),
                default=("text",),
            ),
            prepaid_available=prepaid_available,
            byok_available=any(
                PROVIDERS[slug].supports_byok for _p, _c, _t, slug, _ep in per_endpoint_prices
            ),
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
                    supported_parameters=manifest_supported_parameters(raw_ep),
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
                    supported_parameters=manifest_supported_parameters(raw_ep),
                    prompt_price_microdollars_per_million_tokens=prompt_price,
                    completion_price_microdollars_per_million_tokens=completion_price,
                    published_prompt_price_microdollars_per_million_tokens=prompt_price,
                    published_completion_price_microdollars_per_million_tokens=completion_price,
                    price_tiers=tiers,
                    published_price_tiers=tiers,
                )

    return models, endpoints


def _supplemental_provider_models_and_endpoints() -> tuple[
    dict[str, Model], dict[str, ModelEndpoint]
]:
    """Read provider-native model manifests for providers whose live API
    lists more routes than OpenRouter's endpoint feed. These manifests
    preserve exact upstream model IDs and authoritative downstream prices, so
    the control plane can authorize routes the attested gateway can actually
    call and bill. Most are provider-direct; Meta Muse is explicitly labelled
    as Meta via OpenRouter.

    Novita, Nebius, MiniMax, Crusoe, Cerebras, Google, Fireworks, DeepInfra,
    Moonshot/Kimi, and Z.AI currently use this path because their
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
        "google-ai-studio",
        "google-vertex",
        "fireworks",
        "deepinfra",
        "deepseek",
        "grok",
        "gmi",
        "lightning",
        "mistral",
        "openai",
        "together",
        "phala",
        "siliconflow",
        "venice",
        "parasail",
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
        "nvidia-nim",
        "wandb",
        "nscale",
        "databricks",
        "zero-g",
        "kimi",
        "zai",
        "tinfoil",
        "near-ai",
        "xiaomi",
        "alibaba",
        "azure",
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
        "fal",
        "meta",
        "openrouter",
    ):
        path = _PROVIDER_MODELS_DIR / f"{provider_slug}.json"
        if not path.exists() or provider_slug not in PROVIDERS:
            continue
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw_models = raw.get("models")
        if not isinstance(raw_models, list):
            continue
        catalog_valid_until = _provider_manifest_valid_until(provider_slug, raw)
        provider = PROVIDERS[provider_slug]
        price_scale = _provider_manifest_price_scale(raw)
        for raw_model in raw_models:
            if not isinstance(raw_model, dict):
                continue
            # Discovery-only metadata rows must never produce catalog routes.
            if raw_model.get("routable") is False:
                continue
            model_id = raw_model.get("id")
            if not isinstance(model_id, str) or not model_id:
                continue
            if provider_model_operator_held(provider_slug, model_id):
                continue
            upstream_id = raw_model.get("upstream_id")
            if not isinstance(upstream_id, str) or not upstream_id:
                upstream_id = model_id
            if _is_provider_deprecated_model(provider_slug, model_id, upstream_id):
                continue
            if raw_model.get("model_type") not in (None, "chat", "image"):
                continue
            endpoint_types = {str(item) for item in (raw_model.get("endpoints") or [])}
            if not endpoint_types.intersection({"chat/completions", "images"}):
                continue

            prompt_cost = _provider_manifest_price_cost(
                raw_model.get("input_token_price_per_m"),
                price_scale=price_scale,
            )
            completion_cost = _provider_manifest_price_cost(
                raw_model.get("output_token_price_per_m"),
                price_scale=price_scale,
            )
            cached_raw = raw_model.get("cached_input_token_price_per_m")
            cached_cost = _provider_manifest_optional_price_cost(
                cached_raw,
                price_scale=price_scale,
            )
            raw_request_price = raw_model.get("fixed_request_price_microdollars", 0)
            if (
                isinstance(raw_request_price, bool)
                or not isinstance(raw_request_price, int)
                or raw_request_price < 0
            ):
                continue
            request_price = customer_fixed_price_microdollars(raw_request_price)
            if raw_model.get("model_type") == "image":
                # These providers bill per generated image. The enclave sends
                # an exact fixed-price hold; applying the global token-price
                # floor here would add a second, prompt-length-dependent charge.
                prompt_price = 0
                completion_price = 0
                cached_price = None
                tiers = _flat_tier(0, 0)
            else:
                apply_markup = not provider_model_uses_passthrough_retail_price(
                    provider_slug,
                    model_id,
                )
                prompt_price = _provider_manifest_customer_price(
                    prompt_cost,
                    apply_markup=apply_markup,
                )
                completion_price = _provider_manifest_customer_price(
                    completion_cost,
                    apply_markup=apply_markup,
                )
                cached_price = (
                    _provider_manifest_customer_price(
                        cached_cost,
                        apply_markup=apply_markup,
                    )
                    if cached_cost is not None
                    else None
                )
                try:
                    tiers = _provider_manifest_price_tiers(
                        raw_model,
                        prompt_price,
                        completion_price,
                        cached_price,
                        price_scale=price_scale,
                        apply_markup=apply_markup,
                    )
                except ValueError:
                    # A malformed pricing tier is an accounting ambiguity. Do
                    # not create a route at the cheaper headline price.
                    continue
            publisher = (
                _author_provider(model_id, [{"tr_provider_slug": provider_slug}]) or provider_slug
            )
            context_length = _as_positive_int(raw_model.get("context_length"))
            name = str(raw_model.get("display_name") or raw_model.get("title") or model_id)
            supported_parameters = manifest_supported_parameters(raw_model)
            reliability = raw_model.get("reliability")
            if not isinstance(reliability, dict):
                reliability = {}

            model = Model(
                id=model_id,
                name=name,
                provider=publisher,
                context_length=context_length,
                upstream_id=upstream_id,
                supports_chat="chat/completions" in endpoint_types,
                supports_messages=publisher == "anthropic",
                supported_parameters=supported_parameters,
                input_modalities=_modalities(
                    raw_model.get("input_modalities"),
                    default=("text",),
                ),
                output_modalities=_modalities(
                    raw_model.get("output_modalities"),
                    default=("text",),
                ),
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
                request_price_microdollars=request_price,
                price_tiers=tiers,
                published_price_tiers=tiers,
            )
            existing = models.get(model_id)
            if existing is None:
                models[model_id] = model
            else:
                models[model_id] = replace(
                    existing,
                    input_modalities=tuple(
                        dict.fromkeys((*existing.input_modalities, *model.input_modalities))
                    ),
                    output_modalities=tuple(
                        dict.fromkeys((*existing.output_modalities, *model.output_modalities))
                    ),
                )

            if provider_slug in GATEWAY_PREPAID_PROVIDER_SLUGS:
                credits_id = f"{model_id}@{provider_slug}/prepaid"
                endpoints[credits_id] = ModelEndpoint(
                    id=credits_id,
                    model_id=model_id,
                    provider=provider_slug,
                    usage_type="Credits",
                    upstream_id=upstream_id,
                    supported_parameters=supported_parameters,
                    prompt_price_microdollars_per_million_tokens=prompt_price,
                    completion_price_microdollars_per_million_tokens=completion_price,
                    published_prompt_price_microdollars_per_million_tokens=prompt_price,
                    published_completion_price_microdollars_per_million_tokens=completion_price,
                    request_price_microdollars=request_price,
                    price_tiers=tiers,
                    published_price_tiers=tiers,
                    first_token_timeout_seconds=_positive_float(
                        reliability.get("first_token_timeout_seconds")
                    ),
                    completion_timeout_seconds=_positive_float(
                        reliability.get("completion_timeout_seconds")
                    ),
                    stream_idle_timeout_seconds=_positive_float(
                        reliability.get("stream_idle_timeout_seconds")
                    ),
                    catalog_valid_until=catalog_valid_until,
                )
            if provider.supports_byok:
                byok_id = f"{model_id}@{provider_slug}/byok"
                endpoints[byok_id] = ModelEndpoint(
                    id=byok_id,
                    model_id=model_id,
                    provider=provider_slug,
                    usage_type="BYOK",
                    upstream_id=upstream_id,
                    supported_parameters=supported_parameters,
                    prompt_price_microdollars_per_million_tokens=prompt_price,
                    completion_price_microdollars_per_million_tokens=completion_price,
                    published_prompt_price_microdollars_per_million_tokens=prompt_price,
                    published_completion_price_microdollars_per_million_tokens=completion_price,
                    request_price_microdollars=request_price,
                    price_tiers=tiers,
                    published_price_tiers=tiers,
                    first_token_timeout_seconds=_positive_float(
                        reliability.get("first_token_timeout_seconds")
                    ),
                    completion_timeout_seconds=_positive_float(
                        reliability.get("completion_timeout_seconds")
                    ),
                    stream_idle_timeout_seconds=_positive_float(
                        reliability.get("stream_idle_timeout_seconds")
                    ),
                    catalog_valid_until=catalog_valid_until,
                )
    return models, endpoints


def _embedding_models() -> dict[str, Model]:
    """Seed the embedding-model catalog (input-only pricing).

    Provider manifests override the checked-in fallback price when an hourly
    first-party parser has produced a current embedding row. Static specs
    remain the last-known-good fallback for providers that do not publish a
    parseable price source.
    """
    models: dict[str, Model] = {}
    for spec in _EMBEDDING_SPECS:
        if spec["provider"] not in PROVIDERS:
            continue
        manifest_cost = _embedding_manifest_cost(spec)
        if manifest_cost is None:
            prompt_price, published_price, _cost = _priced(spec["cost_dollars_per_million"])
        else:
            prompt_price = _customer_price(manifest_cost)
            published_price = prompt_price
        models[spec["id"]] = Model(
            id=spec["id"],
            name=spec["name"],
            provider=spec["provider"],
            context_length=spec["context_length"],
            upstream_id=spec["upstream_id"],
            supports_chat=False,
            supports_messages=False,
            supports_embeddings=True,
            supported_parameters=manifest_supported_parameters(
                {}, supports_chat=False, supports_embeddings=True
            ),
            prepaid_available=True,
            byok_available=True,
            prompt_price_microdollars_per_million_tokens=prompt_price,
            completion_price_microdollars_per_million_tokens=0,
            published_prompt_price_microdollars_per_million_tokens=published_price,
            published_completion_price_microdollars_per_million_tokens=0,
            price_tiers=_flat_tier(prompt_price, 0, None),
            published_price_tiers=_flat_tier(published_price, 0, None),
        )
    for path in sorted(_PROVIDER_MODELS_DIR.glob("*.json")):
        provider_slug = path.stem
        provider = PROVIDERS.get(provider_slug)
        if provider is None or not provider.supports_embeddings:
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rows = raw.get("models") if isinstance(raw, dict) else None
        if not isinstance(rows, list):
            continue
        price_scale = _provider_manifest_price_scale(raw)
        for row in rows:
            if (
                not isinstance(row, dict)
                or row.get("routable") is False
                or row.get("model_type") != "embedding"
            ):
                continue
            endpoints = {str(item) for item in (row.get("endpoints") or [])}
            if "embeddings" not in endpoints:
                continue
            model_id = row.get("id")
            upstream_id = row.get("upstream_id")
            if not isinstance(model_id, str) or not model_id:
                continue
            if not isinstance(upstream_id, str) or not upstream_id:
                continue
            input_cost = _provider_manifest_price_cost(
                row.get("input_token_price_per_m"),
                price_scale=price_scale,
            )
            if input_cost <= 0:
                continue
            prompt_price = _customer_price(input_cost)
            context_length = _as_positive_int(row.get("context_length")) or 8192
            input_modalities = tuple(
                str(value) for value in (row.get("input_modalities") or ["text"])
            )
            models[model_id] = Model(
                id=model_id,
                name=str(row.get("display_name") or model_id),
                provider=provider_slug,
                context_length=context_length,
                upstream_id=upstream_id,
                supports_chat=False,
                supports_messages=False,
                supports_embeddings=True,
                supported_parameters=manifest_supported_parameters(
                    {}, supports_chat=False, supports_embeddings=True
                ),
                input_modalities=input_modalities,
                output_modalities=("embeddings",),
                prepaid_available=provider.supports_prepaid,
                byok_available=provider.supports_byok,
                prompt_price_microdollars_per_million_tokens=prompt_price,
                completion_price_microdollars_per_million_tokens=0,
                published_prompt_price_microdollars_per_million_tokens=prompt_price,
                published_completion_price_microdollars_per_million_tokens=0,
                price_tiers=_flat_tier(prompt_price, 0, None),
                published_price_tiers=_flat_tier(prompt_price, 0, None),
            )
    return models


def _embedding_manifest_cost(spec: _EmbeddingSpec) -> int | None:
    """Return a provider-manifest input cost in microdollars/M, if valid."""
    path = _PROVIDER_MODELS_DIR / f"{spec['provider']}.json"
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    rows = raw.get("models")
    if not isinstance(rows, list):
        return None
    price_scale = _provider_manifest_price_scale(raw)
    for row in rows:
        if not isinstance(row, dict) or row.get("id") != spec["id"]:
            continue
        if row.get("model_type") != "embedding":
            return None
        if "embeddings" not in {str(item) for item in (row.get("endpoints") or [])}:
            return None
        cost = _provider_manifest_price_cost(
            row.get("input_token_price_per_m"),
            price_scale=price_scale,
        )
        return cost if cost > 0 else None
    return None


# Providers through which we route Anthropic-authored (anthropic/*) models on
# Credits. Anthropic-direct only today; add "vertex"/"bedrock" here if/when
# first-party Claude routing through those surfaces is enabled. Resellers that
# merely list Claude ids are intentionally excluded (see _keep policy below).
_ANTHROPIC_FIRST_PARTY_PROVIDERS: frozenset[str] = frozenset(
    # First-party Anthropic surfaces are OK for Claude Credits routing; resellers
    # are not. Vertex + Bedrock included per product decision (2026-07-18) so
    # Claude-on-Vertex/Bedrock Credits routes are permitted when they exist.
    {"anthropic", "google-vertex", "bedrock", "aws-bedrock"}
)


def _provider_manifest_dark_model_ids() -> dict[str, frozenset[str]]:
    """Return provider-native routes explicitly held by fresh discovery."""

    dark: dict[str, frozenset[str]] = {}
    for path in _PROVIDER_MODELS_DIR.glob("*.json"):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        provider_slug = raw.get("provider")
        rows = raw.get("models")
        if not isinstance(provider_slug, str) or not isinstance(rows, list):
            continue
        dark[provider_slug] = frozenset(
            row["id"]
            for row in rows
            if isinstance(row, dict)
            and isinstance(row.get("id"), str)
            and row.get("routable") is False
        )
    return dark


def _filter_unserved_provider_endpoints(
    endpoints: dict[str, ModelEndpoint],
    *,
    explicit_model_ids: frozenset[str] = frozenset(),
) -> dict[str, ModelEndpoint]:
    """Drop a provider's prepaid (Credits) endpoints for models it doesn't
    serve on our account. Only Credits routes use OUR provider key, so only
    those 502 on an account mismatch — BYOK routes use the customer's own key
    (their account may serve a different model set), so they're left intact.

    Six complementary filters apply:
      * provider deprecation — drop a disabled upstream route on one provider for
        every usage type (Nebius June 2026 retirements).
      * discovery hold   — drop prepaid routes explicitly held by a fresh
        provider-native manifest (failed canary, missing price, or delisting).
      * allowlist        — keep only manifest-listed routes for authoritative
        providers and account-verified Credits models for static allowlists.
      * model denylist    — drop the listed Credits models on EVERY provider (GPT-5.4/pro).
      * provider denylist — drop a Credits model on ONE provider only (gmi closed models).
      * Claude first-party — route Anthropic-authored models via Anthropic only
        for Credits, never resellers (policy; see _ANTHROPIC_FIRST_PARTY_PROVIDERS).
    """
    allow = dict(_PROVIDER_SERVED_MODEL_ALLOWLIST)
    dark = _provider_manifest_dark_model_ids()
    for provider_slug in _AUTHORITATIVE_PROVIDER_MANIFEST_SLUGS:
        allow[provider_slug] = _authoritative_provider_model_ids(provider_slug)

    def _keep(endpoint: ModelEndpoint) -> bool:
        # Async media routes are registered only after their provider-native
        # queue contracts are implemented and tested. Chat /models manifests
        # do not list video models, so applying the chat allowlist here would
        # incorrectly remove those explicit routes.
        if provider_model_operator_held(endpoint.provider, endpoint.model_id):
            return False
        if endpoint.model_id in explicit_model_ids:
            return True
        if _is_provider_deprecated_model(
            endpoint.provider, endpoint.model_id, endpoint.upstream_id
        ):
            return False
        if endpoint.usage_type == "Credits" and endpoint.model_id in dark.get(
            endpoint.provider, frozenset()
        ):
            return False
        if (
            endpoint.provider in _AUTHORITATIVE_PROVIDER_MANIFEST_SLUGS
            and endpoint.model_id not in allow[endpoint.provider]
        ):
            return False
        if endpoint.usage_type != "Credits":
            return True
        # Policy (2026-07-18): serve Anthropic-authored models via Anthropic
        # directly for Credits, not resellers. Resellers list Claude ids they
        # mostly don't actually serve (uniform upstream 404s) and add no value
        # over first-party; keeping them only produced dead routes and alert
        # noise. BYOK is untouched — a customer's own reseller key is their
        # choice. Extend _ANTHROPIC_FIRST_PARTY_PROVIDERS if first-party
        # Vertex/Bedrock Claude routing is ever enabled.
        if (
            endpoint.model_id.startswith("anthropic/")
            and endpoint.provider not in _ANTHROPIC_FIRST_PARTY_PROVIDERS
        ):
            return False
        if endpoint.provider in allow and endpoint.model_id not in allow[endpoint.provider]:
            return False
        if endpoint.model_id in _UNSERVED_CREDITS_MODELS:
            return False
        if endpoint.model_id in _PROVIDER_UNSERVED_CREDITS_MODELS.get(
            endpoint.provider, frozenset()
        ):
            return False
        return True

    return {endpoint_id: endpoint for endpoint_id, endpoint in endpoints.items() if _keep(endpoint)}
