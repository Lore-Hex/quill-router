"""Provider manifest rows pinned in tests, whatever the hosts list today.

The committed manifests are rebuilt hourly from provider feeds and move on: a
host delists a model and the refresh tombstones its row. A rule that needs a
host's route (a lifecycle cutover, a host's price schedule) runs on rows pinned
here instead: served in this process by serve_manifest_rows, or, for a catalog
built in a fresh process at a chosen instant, in a copy of today's manifests
from pinned_manifests.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest

from trusted_router import catalog_ingest, catalog_registry
from trusted_router.routes import catalog as catalog_routes


def _deepseek_row(model_id: str, display_name: str, **extra: Any) -> dict[str, Any]:
    upstream_id = model_id.removeprefix("deepseek/")
    return {
        "display_name": display_name,
        "title": upstream_id,
        "model_type": "chat",
        "input_modalities": ["text"],
        "output_modalities": ["text"],
        "endpoints": ["chat/completions"],
        "status": 1,
        "id": model_id,
        "upstream_id": upstream_id,
        "input_token_price_per_m": 150000,
        "output_token_price_per_m": 600000,
        "cached_input_token_price_per_m": 3000,
        "context_length": 1048576,
        "supported_features": ["function-calling", "json-mode", "reasoning-effort"],
        **extra,
    }


# DeepSeek's own V4 routes as its manifest lists them. Its time-of-day price
# schedule (provider_lifecycle) applies to DeepSeek's direct routes for these.
DEEPSEEK_V4_FLASH = _deepseek_row("deepseek/deepseek-v4-flash", "deepseek-v4-flash")
DEEPSEEK_V4_PRO = _deepseek_row("deepseek/deepseek-v4-pro", "DeepSeek-V4-Pro")
DEEPSEEK_FLASH = _deepseek_row(
    "deepseek/deepseek-flash", "DeepSeek V4.1 Flash (rolling)", input_modalities=["text", "image"],
)
DEEPSEEK_DIRECT_ROWS = (DEEPSEEK_V4_FLASH, DEEPSEEK_V4_PRO, DEEPSEEK_FLASH)

# Anthropic's Claude Opus 5 route as its manifest lists it.
ANTHROPIC_CLAUDE_OPUS_5 = {
    "display_name": "Claude Opus 5",
    "title": "claude-opus-5",
    "model_type": "chat",
    "input_modalities": ["text", "image"],
    "output_modalities": ["text"],
    "endpoints": ["chat/completions"],
    "status": 1,
    "id": "anthropic/claude-opus-5",
    "context_length": 1000000,
    "max_output_tokens": 128000,
    "features": ["function-calling", "structured-outputs", "reasoning"],
    "upstream_id": "claude-opus-5",
    "created_at": "2026-07-24T00:00:00Z",
    "input_token_price_per_m": 5000000,
    "output_token_price_per_m": 25000000,
    "cached_input_token_price_per_m": 500000,
}

# GMI's verified Kimi K3 and HY4 Preview routes as its manifest lists them.
GMI_KIMI_K3 = {
    "id": "moonshotai/kimi-k3",
    "upstream_id": "moonshotai/kimi-k3",
    "display_name": "moonshotai/Kimi: K3",
    "title": "moonshotai/kimi-k3",
    "context_length": 1048576,
    "max_output_tokens": 65535,
    "input_token_price_per_m": 3000000,
    "output_token_price_per_m": 15000000,
    "cached_input_token_price_per_m": 300000,
    "model_type": "chat",
    "features": ["reasoning", "function-calling", "structured-outputs", "serverless"],
    "input_modalities": ["text"],
    "output_modalities": ["text"],
    "endpoints": ["chat/completions"],
    "status": 1,
}
GMI_HY4_PREVIEW = {
    "display_name": "tencent/hy4-preview",
    "title": "tencent/hy4-preview",
    "model_type": "chat",
    "input_modalities": ["text"],
    "output_modalities": ["text"],
    "endpoints": ["chat/completions"],
    "status": 1,
    "id": "tencent/hy4-preview",
    "upstream_id": "tencent/hy4-preview",
    "context_length": 262144,
    "input_token_price_per_m": 834000,
    "output_token_price_per_m": 2501000,
    "cached_input_token_price_per_m": 42000,
}

# Phala's pass-through Kimi K3 route as its manifest lists it.
PHALA_KIMI_K3 = {
    "id": "moonshotai/kimi-k3",
    "upstream_id": "moonshotai/kimi-k3",
    "display_name": "MoonshotAI: Kimi K3",
    "title": "moonshotai/kimi-k3",
    "context_length": 1048576,
    "max_output_tokens": 1048576,
    "input_token_price_per_m": 3000000,
    "output_token_price_per_m": 15000000,
    "cached_input_token_price_per_m": 300000,
    "model_type": "chat",
    "features": ["reasoning", "function-calling", "structured-outputs", "serverless"],
    "input_modalities": ["text", "image", "video"],
    "output_modalities": ["text"],
    "endpoints": ["chat/completions"],
    "status": 1,
    "supported_features": ["json_mode", "logprobs", "reasoning", "structured_outputs", "tools"],
    "supported_sampling_parameters": [
        "frequency_penalty", "logit_bias", "max_tokens", "min_p", "presence_penalty",
        "repetition_penalty", "seed", "stop", "temperature", "top_k", "top_p",
    ],
    "provider_route_class": "standard_pass_through",
}

# The two approved OpenRouter-only routes as its manifest lists them.
OPENROUTER_SEED_2_1_TURBO = {
    "id": "bytedance-seed/seed-2-1-turbo",
    "upstream_id": "bytedance-seed/seed-2-1-turbo",
    "display_name": "ByteDance Seed: Seed 2.1 Turbo",
    "context_length": 262144,
    "endpoints": ["chat/completions"],
    "input_modalities": ["text", "image", "video"],
    "output_modalities": ["text"],
    "input_token_price_per_m": 500000,
    "output_token_price_per_m": 2500000,
    "reliability": {
        "first_token_timeout_seconds": 45,
        "completion_timeout_seconds": 600,
        "stream_idle_timeout_seconds": 120,
    },
    "max_completion_tokens": 235929,
    "supported_parameters": [
        "reasoning", "include_reasoning", "frequency_penalty", "max_tokens", "temperature",
        "top_p", "stop", "tools", "response_format", "structured_outputs", "tool_choice",
    ],
    "pricing_source": (
        "https://openrouter.ai/api/v1/models/bytedance-seed/seed-2-1-turbo/endpoints"
    ),
}
OPENROUTER_UNION_ALPHA = {
    "id": "stealth/union-alpha",
    "upstream_id": "stealth/union-alpha",
    "display_name": "Union Alpha",
    "context_length": 262144,
    "max_completion_tokens": 131072,
    "endpoints": ["chat/completions"],
    "input_modalities": ["text", "image"],
    "output_modalities": ["text"],
    "supported_parameters": [
        "max_tokens", "temperature", "top_p", "tools", "tool_choice", "response_format",
    ],
    "input_token_price_per_m": 0,
    "output_token_price_per_m": 0,
    "pricing_source": "https://openrouter.ai/api/v1/models/stealth/union-alpha/endpoints",
    "reliability": {
        "first_token_timeout_seconds": 45,
        "completion_timeout_seconds": 600,
        "stream_idle_timeout_seconds": 120,
    },
    "missing_since": "2026-09-17",
}

# Pearl's GLM-5.3 route as its manifest lists it.
PEARL_GLM_5_3 = {
    "display_name": "Z.AI: GLM-5.3",
    "title": "zai-org/GLM-5.3",
    "model_type": "chat",
    "input_modalities": ["text"],
    "output_modalities": ["text"],
    "endpoints": ["chat/completions"],
    "status": 1,
    "id": "z-ai/glm-5.3",
    "upstream_id": "zai-org/GLM-5.3",
    "context_length": 1000000,
    "max_output_tokens": 1000000,
    "supported_features": [
        "chat", "completion", "tools", "json_mode", "reasoning", "structured_outputs",
        "prompt_caching",
    ],
    "supported_sampling_parameters": [
        "temperature", "top_p", "top_k", "frequency_penalty", "presence_penalty", "stop", "seed",
        "max_tokens",
    ],
    "routable": True,
    "input_token_price_per_m": 1100000,
    "output_token_price_per_m": 3500000,
    "cached_input_token_price_per_m": 230000,
}

# OpenAI's own routes as its manifest lists them, for rules of OpenAI's
# routes: its prepaid ZDR contract, Priority processing, image billing.
OPENAI_GPT_4O_MINI = {
    "display_name": "gpt-4o-mini",
    "title": "gpt-4o-mini",
    "model_type": "chat",
    "input_modalities": ["text"],
    "output_modalities": ["text"],
    "endpoints": ["chat/completions"],
    "status": 1,
    "id": "openai/gpt-4o-mini",
    "upstream_id": "gpt-4o-mini",
    "created": 1721172741,
    "routable": True,
    "input_token_price_per_m": 150000,
    "output_token_price_per_m": 600000,
    "cached_input_token_price_per_m": 75000,
}
OPENAI_GPT_5_5 = {
    "display_name": "gpt-5.5",
    "title": "gpt-5.5",
    "model_type": "chat",
    "input_modalities": ["text"],
    "output_modalities": ["text"],
    "endpoints": ["chat/completions"],
    "status": 1,
    "id": "openai/gpt-5.5",
    "upstream_id": "gpt-5.5",
    "created": 1776824847,
    "routable": True,
    "input_token_price_per_m": 5000000,
    "output_token_price_per_m": 30000000,
    "cached_input_token_price_per_m": 500000,
    "price_tiers": [
        {
            "max_prompt_tokens": 272000,
            "input_token_price_per_m": 5000000,
            "output_token_price_per_m": 30000000,
            "cached_input_token_price_per_m": 500000,
        },
        {
            "max_prompt_tokens": None,
            "input_token_price_per_m": 10000000,
            "output_token_price_per_m": 45000000,
            "cached_input_token_price_per_m": 1000000,
        },
    ],
}
OPENAI_GPT_5_6_SOL = {
    "display_name": "gpt-5.6-sol",
    "title": "gpt-5.6-sol",
    "model_type": "chat",
    "input_modalities": ["text"],
    "output_modalities": ["text"],
    "endpoints": ["chat/completions"],
    "status": 1,
    "id": "openai/gpt-5.6-sol",
    "upstream_id": "gpt-5.6-sol",
    "created": 1782228018,
    "routable": True,
    "input_token_price_per_m": 4000000,
    "output_token_price_per_m": 20000000,
    "cached_input_token_price_per_m": 400000,
    "price_tiers": [
        {
            "max_prompt_tokens": 272000,
            "input_token_price_per_m": 4000000,
            "output_token_price_per_m": 20000000,
            "cached_input_token_price_per_m": 400000,
        },
        {
            "max_prompt_tokens": None,
            "input_token_price_per_m": 8000000,
            "output_token_price_per_m": 30000000,
            "cached_input_token_price_per_m": 800000,
        },
    ],
}
OPENAI_GPT_IMAGE_2_5 = tuple(
    {
        "display_name": f"GPT Image 2.5 {variant.title()}",
        "title": f"gpt-image-2.5-{variant}",
        "model_type": "image",
        "input_modalities": ["text"],
        "output_modalities": ["image"],
        "endpoints": ["images"],
        "status": 1,
        "id": f"openai/gpt-image-2.5-{variant}",
        "upstream_id": f"gpt-image-2.5-{variant}",
        "routable": True,
        "input_token_price_per_m": 5000000,
        "output_token_price_per_m": 30000000,
        "cached_input_token_price_per_m": 1250000,
    }
    for variant in ("flare", "sunburst")
)

# The routes the DeepSeek V4 Pro 0813 release leaf is built from
# (catalog_registry._install_deepseek_v4_pro_release_routes), as their
# manifests listed them before Fireworks retired its route on 2026-09-25.
DEEPSEEK_V4_PRO_0813_ROUTES: tuple[tuple[str, dict[str, Any]], ...] = (
    ("deepseek", DEEPSEEK_V4_PRO),
    ("baseten", {
        "display_name": "DeepSeek V4 Pro 0813",
        "title": "deepseek-ai/DeepSeek-V4-Pro-0813",
        "model_type": "chat",
        "input_modalities": ["text"],
        "output_modalities": ["text"],
        "endpoints": ["chat/completions"],
        "status": 1,
        "id": "deepseek/deepseek-v4-pro-0813",
        "upstream_id": "deepseek-ai/DeepSeek-V4-Pro-0813",
        "context_length": 1048576,
        "supported_features": ["tools", "json_mode", "structured_outputs", "reasoning"],
        "supported_sampling_parameters": ["temperature", "stop"],
        "input_token_price_per_m": 1320000,
        "output_token_price_per_m": 3960000,
        "cached_input_token_price_per_m": 132000,
        "max_output_tokens": 262144,
    }),
    ("fireworks", {
        "display_name": "DeepSeek V4 Pro 0813 on Fireworks",
        "title": "accounts/fireworks/models/deepseek-v4-pro-0813",
        "model_type": "chat",
        "input_modalities": ["text"],
        "output_modalities": ["text"],
        "endpoints": ["chat/completions"],
        "status": 1,
        "id": "deepseek/deepseek-v4-pro-0813",
        "upstream_id": "accounts/fireworks/models/deepseek-v4-pro-0813",
        "retirement_at": "2026-09-25T00:00:00Z",
        "context_length": 1048576,
        "created": 1786637155,
        "input_token_price_per_m": 1320000,
        "output_token_price_per_m": 3960000,
        "cached_input_token_price_per_m": 44000,
    }),
)

# Run in the subprocess before the catalog is built.
USE_PINNED_MANIFESTS = """
from pathlib import Path as _Path
from trusted_router import catalog_ingest as _catalog_ingest
_catalog_ingest._PROVIDER_MODELS_DIR = _Path(__import__("os").environ["TR_TEST_PROVIDER_MODELS_DIR"])
"""


def pinned_manifests(
    directory: Path, rows: Iterable[tuple[str, dict[str, Any]]]
) -> dict[str, str]:
    """Copy today's manifests into `directory`, put each (provider, row) in
    place of that provider's row with the same id, and return the environment
    for a subprocess that runs USE_PINNED_MANIFESTS first."""
    manifests = directory / "provider_models"
    shutil.copytree(catalog_ingest._PROVIDER_MODELS_DIR, manifests)
    for provider, row in rows:
        path = manifests / f"{provider}.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["models"] = [r for r in raw["models"] if r.get("id") != row["id"]] + [dict(row)]
        path.write_text(json.dumps(raw), encoding="utf-8")
    environ = dict(os.environ)
    environ["TR_TEST_PROVIDER_MODELS_DIR"] = str(manifests)
    return environ


def serve_manifest_rows(
    monkeypatch: pytest.MonkeyPatch,
    directory: Path,
    provider: str,
    rows: Iterable[dict[str, Any]],
) -> None:
    """Serve `provider`'s routes for these manifest rows in this process's
    registry, built by the catalog's own manifest ingestion, in place of any
    route with the same id. A model the registry lacks is added with them."""
    manifests = directory / "served_provider_models"
    manifests.mkdir(parents=True, exist_ok=True)
    manifest = {"provider": provider, "models": [dict(row) for row in rows]}
    (manifests / f"{provider}.json").write_text(json.dumps(manifest), encoding="utf-8")
    with monkeypatch.context() as patch:
        patch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", manifests)
        models, endpoints = catalog_ingest._supplemental_provider_models_and_endpoints()
    assert endpoints, f"the catalog built no {provider} route from {manifest}"
    for model_id, model in models.items():
        if model_id not in catalog_registry.MODELS:
            monkeypatch.setitem(catalog_registry.MODELS, model_id, model)
    for endpoint_id, endpoint in endpoints.items():
        monkeypatch.setitem(catalog_registry.MODEL_ENDPOINTS, endpoint_id, endpoint)
    # The public catalog projection is cached per price period: this test
    # builds its own from these routes, and the process's cache is left as it was.
    projection = catalog_routes._public_catalog_payload
    monkeypatch.setattr(
        catalog_routes, "_public_catalog_payload", lru_cache(maxsize=1)(projection.__wrapped__)
    )
