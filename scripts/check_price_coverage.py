#!/usr/bin/env python3
"""Coverage + staleness audit for the hourly price-refresh system.

A price change can only be CAUGHT for a provider whose prices the refresh
actually re-reads each run. This reports the gaps:
  * prepaid providers (GATEWAY_PREPAID_PROVIDER_SLUGS) with NO live scraper
    in scripts/pricing/providers/ — they rely on a static manifest (which
    drifts) or have no price source at all (hand-coded catalog prices that
    never refresh, e.g. Cohere embeddings);
  * provider_models/<slug>.json manifests whose `generated_at` is older than
    --max-age-days (stale → may serve wrong prices).

Run in refresh-prices.yml as a report-producing gate. Stale authenticated
fallback manifests and required model-discovery gaps block publication while
the last known-good catalog remains live. Pass --strict to fail on every gap.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from scripts.pricing.base import (
    fetch_json as fetch_provider_json,
)
from scripts.pricing.base import (
    read_stale_provider_manifest,
)
from scripts.pricing.model_ids import (
    canonicalize_native_model_id,
    canonicalize_unqualified_model_id,
)
from scripts.pricing.providers import (
    aion_labs,
    akashml,
    arcee,
    bfl,
    decart,
    featherless,
    inception,
    io_net,
    jina,
    krea,
    mancer,
    near_ai,
    nextbit,
    nscale,
    nvidia_nim,
    perplexity,
    recraft,
    reka,
    relace,
    sail_research,
    sakana,
    sambanova,
    scaleway,
    stepfun,
    upstage,
    wandb,
)
from scripts.pricing.video_sources import (
    VIDEO_PRICE_PROVIDER_SLUGS,
    audit_video_price_sources,
)
from trusted_router.provider_manifest_policy import (
    EXPIRED_PROVIDER_MANIFEST,
    EXPIRING_PROVIDER_MANIFEST_SLUGS,
    RUNTIME_ONLY_PROVIDER_MANIFEST_SLUGS,
    provider_manifest_valid_until,
)

ROOT = Path(__file__).resolve().parent.parent
PROVIDERS_DIR = ROOT / "scripts" / "pricing" / "providers"
MANIFEST_DIR = ROOT / "src" / "trusted_router" / "data" / "provider_models"
DEFAULT_MAX_AGE_DAYS = 14
ZAI_MODEL_DISCOVERY_URL = "https://docs.z.ai/devpack/latest-model.md"
_ZAI_MODEL_RE = re.compile(r"\bglm-\d+(?:\.\d+)?(?:-[a-z0-9]+(?:-[a-z0-9]+)*)?(?:\[1m\])?\b", re.I)

# One parser can own pricing for more than one separately routed provider.
# Gemini pricing is shared between AI Studio and existing Vertex endpoints;
# availability remains provider-specific and is never synthesized here.
_SHARED_LIVE_SCRAPER_OWNERS = {
    "google-ai-studio": "gemini",
    "google-vertex": "gemini",
}


def _identity_model_id(native_id: str) -> str | None:
    value = native_id.strip()
    return value or None


def _minimax_model_id(native_id: str) -> str | None:
    value = native_id.strip()
    if not value:
        return None
    return f"minimax/{value.casefold()}"


def _cerebras_model_id(native_id: str) -> str | None:
    value = native_id.strip()
    return {
        "gpt-oss-120b": "openai/gpt-oss-120b",
        "zai-glm-4.7": "z-ai/glm-4.7",
        "gemma-4-31b": "google/gemma-4-31b-it",
        "qwen-3.8-27b": "qwen/qwen3.8-27b",
        "qwen3.8-27b": "qwen/qwen3.8-27b",
    }.get(value) or canonicalize_unqualified_model_id(value)


def _gemini_model_id(native_id: str) -> str | None:
    value = native_id.removeprefix("models/").strip()
    if not value:
        return None
    return f"google/{value.casefold()}"


def _canonical_provider_model_id(native_id: str) -> str | None:
    """Normalize vendor-native IDs using the shared catalog rules."""
    value = native_id.strip()
    if not value:
        return None
    return canonicalize_native_model_id(value) or canonicalize_unqualified_model_id(value)


def _novita_model_id(native_id: str) -> str | None:
    """Match the conservative normalization used by the Novita refresher."""

    return _canonical_provider_model_id(native_id)


_FIREWORKS_MODEL_IDS = {
    "accounts/fireworks/models/kimi-k3": "moonshotai/kimi-k3",
    "accounts/fireworks/models/kimi-k2p6": "moonshotai/kimi-k2.6",
    "accounts/fireworks/models/kimi-k2p5": "moonshotai/kimi-k2.5",
    "accounts/fireworks/models/kimi-k2p7-code": "moonshotai/kimi-k2.7-code",
    "accounts/fireworks/models/deepseek-v4-pro": "deepseek/deepseek-v4-pro",
    "accounts/fireworks/models/glm-5p2": "z-ai/glm-5.2",
    "accounts/fireworks/routers/glm-5p2-fast": "z-ai/glm-5.2-fast",
    "accounts/fireworks/models/glm-5p1": "z-ai/glm-5.1",
    "accounts/fireworks/models/gpt-oss-120b": "openai/gpt-oss-120b",
}


def _fireworks_model_id(native_id: str) -> str | None:
    value = native_id.strip()
    if not value:
        return None
    return _FIREWORKS_MODEL_IDS.get(value) or canonicalize_unqualified_model_id(value)


_BASETEN_MODEL_IDS = {
    "openai/gpt-oss-120b": "openai/gpt-oss-120b",
    "zai-org/GLM-4.7": "z-ai/glm-4.7",
    "moonshotai/Kimi-K2.5": "moonshotai/kimi-k2.5",
    "zai-org/GLM-5": "z-ai/glm-5",
    "nvidia/Nemotron-120B-A12B": "nvidia/nemotron-120b-a12b",
    "zai-org/GLM-5.1": "z-ai/glm-5.1",
    "moonshotai/Kimi-K2.6": "moonshotai/kimi-k2.6",
    "deepseek-ai/DeepSeek-V4-Pro": "deepseek/deepseek-v4-pro",
    "nvidia/NVIDIA-Nemotron-3-Ultra-550B-A55B": "nvidia/nemotron-3-ultra-550b-a55b",
    "zai-org/GLM-5.2": "z-ai/glm-5.2",
    "zai-org/GLM-5.2-Fast": "z-ai/glm-5.2-fast",
    "moonshotai/Kimi-K2.7-Code": "moonshotai/kimi-k2.7-code",
}


def _baseten_model_id(native_id: str) -> str | None:
    value = native_id.strip()
    if not value:
        return None
    return _BASETEN_MODEL_IDS.get(value, _identity_model_id(value))


_TELNYX_MODEL_IDS = {
    "google/gemma-2b-it": "google/gemma-2b-it",
    "meta-llama/Llama-3.3-70B-Instruct": "meta-llama/llama-3.3-70b-instruct",
    "meta-llama/Meta-Llama-3.1-70B-Instruct": "meta-llama/llama-3.1-70b-instruct",
    "meta-llama/Meta-Llama-3.1-8B-Instruct": "meta-llama/llama-3.1-8b-instruct",
    "MiniMaxAI/MiniMax-M2.7": "minimax/minimax-m2.7",
    "MiniMaxAI/MiniMax-M3-MXFP8": "minimax/minimax-m3",
    "moonshotai/Kimi-K2.5": "moonshotai/kimi-k2.5",
    "moonshotai/Kimi-K2.6": "moonshotai/kimi-k2.6",
    "moonshotai/Kimi-K3": "moonshotai/kimi-k3",
    "Qwen/Qwen3-235B-A22B": "qwen/qwen3-235b-a22b",
    "zai-org/GLM-5.1-FP8": "z-ai/glm-5.1",
    "zai-org/GLM-5.2": "z-ai/glm-5.2",
}


def _telnyx_model_id(native_id: str) -> str | None:
    value = native_id.strip()
    if not value:
        return None
    return _TELNYX_MODEL_IDS.get(value) or canonicalize_native_model_id(value)


_WAFER_MODEL_IDS = {
    "GLM-5.1": "z-ai/glm-5.1",
    "GLM-5.2": "z-ai/glm-5.2",
    "GLM-5.2-Fast": "z-ai/glm-5.2-fast",
    "glm5.2-fast": "z-ai/glm-5.2-fast",
    "Kimi-K2.6": "moonshotai/kimi-k2.6",
    "Kimi-K2.7-Code": "moonshotai/kimi-k2.7-code",
    "Qwen3.5-397B-A17B": "qwen/qwen3.5-397b-a17b",
    "Qwen3.6-35B-A3B": "qwen/qwen3.6-35b-a3b",
    "qwen3.6-max-preview": "qwen/qwen3.6-max-preview",
    "qwen3.7-max": "qwen/qwen3.7-max",
    "deepseek-v4-flash": "deepseek/deepseek-v4-flash",
    "deepseek-v4-pro": "deepseek/deepseek-v4-pro",
    "MiniMax-M3": "minimax/minimax-m3",
}


def _wafer_model_id(native_id: str) -> str | None:
    value = native_id.strip()
    if not value:
        return None
    return _WAFER_MODEL_IDS.get(value)


_CRUSOE_MODEL_IDS = {
    "deepseek-ai/DeepSeek-V3-0324": "deepseek/deepseek-v3-0324",
    "deepseek-ai/Deepseek-V4-Flash": "deepseek/deepseek-v4-flash",
    "deepseek-ai/DeepSeek-V4-Pro": "deepseek/deepseek-v4-pro",
    "google/gemma-4-31b-it": "google/gemma-4-31b-it",
    "meta-llama/Llama-3.3-70B-Instruct": "meta-llama/llama-3.3-70b-instruct",
    "moonshotai/Kimi-K2.6": "moonshotai/kimi-k2.6",
    "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B": "nvidia/nemotron-3-nano-30b-a3b",
    "nvidia/Nemotron-3-Nano-Omni-Reasoning-30B-A3B": (
        "nvidia/nemotron-3-nano-omni-reasoning-30b-a3b"
    ),
    "nvidia/NVIDIA-Nemotron-3-Super-120B-A12B": "nvidia/nemotron-3-super-120b-a12b",
    "nvidia/NVIDIA-Nemotron-3-Ultra-550B": "nvidia/nemotron-3-ultra-550b",
    "openai/gpt-oss-120b": "openai/gpt-oss-120b",
    "Qwen/Qwen3-235B-A22B-Instruct-2507": "qwen/qwen3-235b-a22b-2507",
    "yutori/n1.5": "yutori/n1.5",
    "zai/GLM-5.1": "z-ai/glm-5.1",
    "zai/GLM-5.2": "z-ai/glm-5.2",
}


def _crusoe_model_id(native_id: str) -> str | None:
    value = native_id.strip()
    if not value:
        return None
    return _CRUSOE_MODEL_IDS.get(value, _identity_model_id(value))


_MAKORA_MODEL_IDS = {
    "deepseek-ai/DeepSeek-V4-Flash": "deepseek/deepseek-v4-flash",
    "deepseek-ai/DeepSeek-V4-Pro": "deepseek/deepseek-v4-pro",
    "google/gemma-4-26B-A4B": "google/gemma-4-26b-a4b-it",
    "zai-org/GLM-5.2-FP8": "z-ai/glm-5.2",
    "zai-org/GLM-5.2-NVFP4": "z-ai/glm-5.2-nvfp4",
    "moonshotai/Kimi-K2.7-Code": "moonshotai/kimi-k2.7-code",
    "amd/Llama-3.3-70B-Instruct-FP8-KV": "amd/llama-3.3-70b-instruct-fp8-kv",
    "meta-llama/Llama-3.3-70B-Instruct": "meta-llama/llama-3.3-70b-instruct",
    "unsloth/Qwen3.6-27B-NVFP4": "qwen/qwen3.6-27b",
    "unsloth/Qwen3.6-35B-A3B-NVFP4": "qwen/qwen3.6-35b-a3b",
}


def _makora_model_id(native_id: str) -> str | None:
    value = native_id.strip()
    if not value:
        return None
    return _MAKORA_MODEL_IDS.get(value, _identity_model_id(value))


def _alibaba_model_id(native_id: str) -> str | None:
    value = native_id.strip()
    if not value:
        return None
    lowered = value.casefold()
    if lowered.startswith("glm-"):
        return f"z-ai/{lowered}"
    if lowered.startswith("kimi-"):
        return f"moonshotai/{lowered}"
    if lowered.startswith("deepseek-"):
        return f"deepseek/{lowered}"
    if lowered.startswith("qwen") or lowered.startswith("qwq"):
        return f"qwen/{lowered}"
    if lowered.startswith("minimax-"):
        return f"minimax/{lowered}"
    if "/" in lowered:
        return lowered
    return f"alibaba/{lowered}"


def _kimi_model_id(native_id: str) -> str | None:
    value = native_id.strip().casefold()
    # Moonshot exposes this dynamic alias without a separately published
    # price. Publishing it as a billable route would require guessing which
    # concrete model/rate it selects, so the strict discovery gate ignores it.
    if value == "moonshot-v1-auto":
        return None
    if value.startswith(("kimi-", "moonshot-v1-")):
        return f"moonshotai/{value}"
    return None


def _openai_model_id(native_id: str) -> str | None:
    value = native_id.strip().casefold()
    if value.startswith(("gpt-", "o1", "o3", "o4", "chat-latest")):
        return f"openai/{value}"
    return None


def _grok_model_id(native_id: str) -> str | None:
    value = native_id.strip().casefold()
    return f"x-ai/{value}" if value.startswith("grok-") else None


def _mistral_model_id(native_id: str) -> str | None:
    value = native_id.strip().casefold()
    return f"mistralai/{value}" if value else None


_DISCOVERABLE_MANIFEST_PROVIDERS_BASE: tuple[
    tuple[str, str, tuple[str, ...], Callable[[str], str | None]], ...
] = (
    (
        "openai",
        "https://api.openai.com/v1/models",
        ("OPENAI_API_KEY", "CHATGPT_API_KEY"),
        _openai_model_id,
    ),
    (
        "grok",
        "https://api.x.ai/v1/language-models",
        ("GROK_API_KEY", "XAI_API_KEY"),
        _grok_model_id,
    ),
    (
        "deepseek",
        "https://api.deepseek.com/models",
        ("DEEPSEEK_API_KEY",),
        canonicalize_unqualified_model_id,
    ),
    (
        "mistral",
        "https://api.mistral.ai/v1/models",
        ("MISTRAL_API_KEY",),
        _mistral_model_id,
    ),
    (
        "zai",
        "https://api.z.ai/api/paas/v4/models",
        ("ZAI_API_KEY",),
        canonicalize_unqualified_model_id,
    ),
    (
        "kimi",
        "https://api.moonshot.ai/v1/models",
        ("KIMI_API_KEY", "MOONSHOT_API_KEY"),
        _kimi_model_id,
    ),
    (
        "cerebras",
        "https://api.cerebras.ai/public/v1/models",
        (),
        _cerebras_model_id,
    ),
    (
        "google-ai-studio",
        "https://generativelanguage.googleapis.com/v1beta/models",
        ("GEMINI_API_KEY",),
        _gemini_model_id,
    ),
    (
        "fireworks",
        "https://api.fireworks.ai/inference/v1/models",
        ("FIREWORKS_API_KEY", "FIREWORKS_AI_API_KEY"),
        _fireworks_model_id,
    ),
    (
        "minimax",
        "https://api.minimax.io/v1/models",
        ("MINIMAX_API_KEY", "MINIMAX_TOKEN_PLAN_API_KEY"),
        _minimax_model_id,
    ),
    (
        "nebius",
        "https://api.tokenfactory.nebius.com/v1/models",
        ("NEBIUS_API_KEY", "NEBIUS_TOKEN_FACTORY_API_KEY"),
        _identity_model_id,
    ),
    (
        "novita",
        "https://api.novita.ai/openai/v1/models",
        ("NOVITA_API_KEY",),
        _novita_model_id,
    ),
    (
        "friendli",
        "https://api.friendli.ai/serverless/v1/models",
        ("FRIENDLI_API_KEY",),
        _identity_model_id,
    ),
    (
        "baseten",
        "https://inference.baseten.co/v1/models",
        ("BASETEN_API_KEY",),
        _baseten_model_id,
    ),
    (
        "telnyx",
        "https://api.telnyx.com/v2/ai/openai/models",
        ("TELNYX_API_KEY",),
        _telnyx_model_id,
    ),
    (
        "wafer",
        "https://pass.wafer.ai/v1/models",
        ("WAFER_API_KEY",),
        _wafer_model_id,
    ),
    (
        "crusoe",
        "https://api.inference.crusoecloud.com/v1/models",
        ("CRUSOE_API_KEY",),
        _crusoe_model_id,
    ),
    (
        "makora",
        "https://inference.makora.com/v1/models",
        ("MAKORA_API_KEY", "MAKORA_OPTIMIZE_TOKEN"),
        _makora_model_id,
    ),
    (
        "alibaba",
        "https://ws-el6e4bpnggpx7g88.eu-central-1.maas.aliyuncs.com/compatible-mode/v1/models",
        ("ALIBABA_API_KEY", "DASHSCOPE_API_KEY", "ALIYUN_API_KEY"),
        _alibaba_model_id,
    ),
    (
        "neurometric",
        "https://wharf.neurometric.ai/v1/models",
        ("NEUROMETRIC_API_KEY",),
        _identity_model_id,
    ),
    (
        "near-ai",
        near_ai.CATALOG_URL,
        ("NEAR_API_KEY",),
        near_ai.canonical_model_id,
    ),
    (
        "engy",
        "https://api.engy.ai/v1/models",
        ("ENGY_API_KEY",),
        canonicalize_unqualified_model_id,
    ),
    (
        "pearl",
        "https://inference.pearlresearch.ai/v1/models",
        ("PEARL_RESEARCH_API_KEY",),
        _canonical_provider_model_id,
    ),
    (
        "io-net",
        io_net.URL,
        io_net.CATALOG.api_key_envs,
        io_net.CATALOG.model_id,
    ),
    (
        "nscale",
        nscale.URL,
        ("NSCALE_API_KEY",),
        nscale._canonical_id,
    ),
)

_DIRECT_OPENAI_DISCOVERY_MODULES = (
    upstage,
    sail_research,
    reka,
    nextbit,
    akashml,
    mancer,
    aion_labs,
    sambanova,
    arcee,
    inception,
)

# These providers use the same direct OpenAI catalog adapter, but their
# credentials are available to the hourly refresh workflow. Keep them out of
# the runtime-only set so a missing workflow secret is a deployment error, not
# an intentionally skipped discovery check.
_CI_DIRECT_OPENAI_DISCOVERY_MODULES = (
    perplexity,
    scaleway,
    featherless,
    sakana,
    wandb,
)

_STALE_MANIFEST_PROVIDER_MODULES = (
    *_DIRECT_OPENAI_DISCOVERY_MODULES,
    *_CI_DIRECT_OPENAI_DISCOVERY_MODULES,
    io_net,
    jina,
    krea,
    near_ai,
    bfl,
    decart,
    nscale,
    nvidia_nim,
    recraft,
    relace,
    stepfun,
)

_STALE_MANIFEST_PROVIDER_MODULE_BY_SLUG = {
    module.SLUG: module
    for module in _STALE_MANIFEST_PROVIDER_MODULES
    if bool(getattr(module, "MANIFEST_STALE_FALLBACK", False))
}
_OPTIONAL_STALE_MANIFEST_PROVIDER_SLUGS = frozenset(_STALE_MANIFEST_PROVIDER_MODULE_BY_SLUG)

_RUNTIME_ONLY_DISCOVERY_SLUGS = RUNTIME_ONLY_PROVIDER_MANIFEST_SLUGS
_DIRECT_OPENAI_DISCOVERY_SLUGS = frozenset(
    module.SLUG for module in (*_DIRECT_OPENAI_DISCOVERY_MODULES, nscale)
)

_DISCOVERABLE_MANIFEST_PROVIDERS = _DISCOVERABLE_MANIFEST_PROVIDERS_BASE + tuple(
    (
        module.SLUG,
        module.CATALOG.spec.catalog_url or f"{module.CATALOG.spec.base_url.rstrip('/')}/models",
        module.CATALOG.api_key_envs,
        module.CATALOG.model_id,
    )
    for module in (
        *_DIRECT_OPENAI_DISCOVERY_MODULES,
        *_CI_DIRECT_OPENAI_DISCOVERY_MODULES,
    )
)

_GLM_DISCOVERABLE_PROVIDER_APIS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "deepinfra",
        "https://api.deepinfra.com/v1/openai/models",
        ("DEEPINFRA_API_KEY",),
    ),
    (
        "fireworks",
        "https://api.fireworks.ai/inference/v1/models",
        ("FIREWORKS_API_KEY", "FIREWORKS_AI_API_KEY"),
    ),
    (
        "novita",
        "https://api.novita.ai/openai/v1/models",
        ("NOVITA_API_KEY",),
    ),
    (
        "gmi",
        "https://api.gmi-serving.com/v1/models",
        ("GMI_API_KEY",),
    ),
    (
        "together",
        "https://api.together.xyz/v1/endpoints?type=serverless",
        ("TOGETHER_API_KEY",),
    ),
    (
        "phala",
        "https://api.redpill.ai/v1/models",
        ("PHALA_CONFIDENTIAL_API_KEY", "PHALA_API_KEY"),
    ),
    (
        "siliconflow",
        "https://api.siliconflow.com/v1/models",
        ("SILICON_FLOW_API_KEY", "SILICONFLOW_API_KEY"),
    ),
    (
        "venice",
        "https://api.venice.ai/api/v1/models",
        ("VENICE_API_KEY",),
    ),
    (
        "parasail",
        "https://api.parasail.io/v1/models",
        ("PARASAIL_API_KEY",),
    ),
    (
        "friendli",
        "https://api.friendli.ai/serverless/v1/models",
        ("FRIENDLI_API_KEY",),
    ),
    (
        "baseten",
        "https://inference.baseten.co/v1/models",
        ("BASETEN_API_KEY",),
    ),
    (
        "wafer",
        "https://pass.wafer.ai/v1/models",
        ("WAFER_API_KEY",),
    ),
    (
        "crusoe",
        "https://api.inference.crusoecloud.com/v1/models",
        ("CRUSOE_API_KEY",),
    ),
)


def _scraper_slugs() -> set[str]:
    if not PROVIDERS_DIR.is_dir():
        return set()
    return {
        p.stem.replace("_", "-")
        for p in PROVIDERS_DIR.glob("*.py")
        if p.stem not in {"__init__", "base", "_base"}
    }


def _manifest_age_days(path: Path, now: dt.datetime) -> float | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    gen = raw.get("generated_at")
    if not isinstance(gen, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(gen.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return (now - parsed).total_seconds() / 86400.0


def _fetch_text(url: str) -> str:
    if not url.startswith("https://"):
        raise ValueError(f"refusing non-HTTPS model discovery URL: {url}")
    req = urllib.request.Request(  # noqa: S310 - URL scheme is checked above.
        url,
        headers={
            "Accept": "text/markdown,text/plain,text/html;q=0.8,*/*;q=0.5",
            "User-Agent": "TrustedRouterModelDiscovery/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as response:  # noqa: S310
        return response.read().decode("utf-8", errors="replace")


def _fetch_json(url: str, env_names: tuple[str, ...]) -> Any:
    if not url.startswith("https://"):
        raise ValueError(f"refusing non-HTTPS model discovery URL: {url}")
    headers = {
        "Accept": "application/json",
        "User-Agent": "TrustedRouterModelDiscovery/1.0",
    }
    token = next((os.environ.get(name) for name in env_names if os.environ.get(name)), None)
    is_gemini = "generativelanguage.googleapis.com" in url
    if token:
        if is_gemini:
            # Google accepts API keys in this header. Never put credentials in
            # the URL, which can be copied into retry logs and proxy traces.
            headers["x-goog-api-key"] = token
        else:
            headers["Authorization"] = f"Bearer {token}"
    # Authenticated discovery must never replay a provider key to a redirect
    # target. The shared httpx helper also centralizes retries and timeouts;
    # redirects are disabled here because a moved catalog URL must be reviewed
    # before credentials are sent to it.
    return fetch_provider_json(
        url,
        extra_headers=headers,
        follow_redirects=False,
    )


def _json_model_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        rows = payload.get("data")
        if not isinstance(rows, list):
            rows = payload.get("models")
    else:
        rows = payload
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _manifest_provider_model_ids(slug: str) -> set[str]:
    routable, unresolved, classified, _new_unresolved = _manifest_provider_model_state(slug)
    return routable | unresolved | classified


def _manifest_provider_model_state(
    slug: str,
) -> tuple[set[str], set[str], set[str], set[str]]:
    """Return routable, awaiting-price, classified, and new-awaiting IDs."""

    path = MANIFEST_DIR / f"{slug}.json"
    if not path.exists():
        return set(), set(), set(), set()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set(), set(), set(), set()
    routable: set[str] = set()
    unresolved: set[str] = set()
    classified: set[str] = set()
    new_unresolved: set[str] = set()
    for row in raw.get("models") or []:
        if not isinstance(row, dict):
            continue
        row_ids: set[str] = set()
        for key in ("id", "upstream_id"):
            value = row.get(key)
            if isinstance(value, str) and value:
                row_ids.add(value)
        if row.get("routable") is not False:
            routable.update(row_ids)
        elif row.get("routable_reason") == "awaiting-price":
            unresolved.update(row_ids)
            if row.get("unresolved_since"):
                new_unresolved.update(row_ids)
        else:
            classified.update(row_ids)
    return routable, unresolved, classified, new_unresolved


def _active_discovery_row(row: dict[str, Any]) -> bool:
    status = row.get("status")
    if isinstance(status, int) and not isinstance(status, bool) and status != 1:
        return False
    if isinstance(status, str) and status.casefold() in {
        "disabled",
        "inactive",
        "offline",
        "retired",
        "deprecated",
    }:
        return False
    state = row.get("state")
    if isinstance(state, str) and state.casefold() in {
        "disabled",
        "failed",
        "inactive",
        "offline",
        "stopped",
    }:
        return False
    endpoints = row.get("endpoints")
    if isinstance(endpoints, list) and endpoints:
        normalized = {str(value).casefold() for value in endpoints}
        if "chat/completions" not in normalized and "responses" not in normalized:
            return False
    output_modalities = row.get("output_modalities")
    if isinstance(output_modalities, list) and output_modalities:
        if "text" not in {str(value).casefold() for value in output_modalities}:
            return False
    return True


_STANDARD_GEMINI_TEXT_ID_RE = re.compile(r"^google/gemini-\d+(?:\.\d+)?-(?:pro|flash|flash-lite)$")


def _required_discovery_model(slug: str, model_id: str) -> bool:
    if slug == "google-ai-studio":
        return _STANDARD_GEMINI_TEXT_ID_RE.fullmatch(model_id) is not None
    return slug in {
        "cerebras",
        "kimi",
        "makora",
        "minimax",
        "novita",
    }


def _discover_zai_coding_plan_models(text: str) -> set[str]:
    """Return OpenRouter-style Z.AI IDs mentioned in the Coding Plan docs.

    Z.AI has started announcing flagship coding models on the Coding Plan docs
    before the token-pricing page or OpenRouter snapshot catches up. This
    scanner is intentionally narrow: it only captures GLM model IDs from that
    page and normalizes them to TR's public `z-ai/...` namespace.
    """
    models: set[str] = set()
    for match in _ZAI_MODEL_RE.finditer(text):
        slug = match.group(0).lower()
        slug = slug.removesuffix("[1m]")
        # The docs often repeat the same model in env-var examples, model
        # arrays, and prose. A set keeps the audit stable.
        models.add(f"z-ai/{slug}")
    return models


def _normalize_glm_model_id(native_id: str) -> str | None:
    value = native_id.strip().casefold()
    if not value:
        return None
    value = value.removeprefix("accounts/fireworks/models/")
    value = value.removeprefix("zai-org/")
    value = value.removeprefix("z-ai/")
    value = value.removeprefix("models/")
    value = value.replace("_", "-")
    value = re.sub(r"glm-(\d+)p(\d+)", r"glm-\1.\2", value)
    value = re.sub(r"glm-(\d+)-(\d+)", r"glm-\1.\2", value)
    match = _ZAI_MODEL_RE.fullmatch(value)
    if not match:
        return None
    slug = match.group(0).removesuffix("[1m]")
    # FP8 is a deployment quantization used by providers such as GMI and
    # Parasail, not a distinct public/billable model in their manifests.
    # Preserve semantic variants (for example -fast and -nvfp4), which can
    # have different routing and prices.
    slug = re.sub(r"-fp8(?:-block|-lora)?$", "", slug)
    return f"z-ai/{slug}"


def _provider_glm_model_ids(payload: Any) -> set[str]:
    discovered: set[str] = set()
    for row in _json_model_rows(payload):
        if not _active_discovery_row(row):
            continue
        for key in ("id", "name", "title", "model"):
            raw_id = row.get(key)
            if not isinstance(raw_id, str):
                continue
            normalized = _normalize_glm_model_id(raw_id)
            if normalized:
                discovered.add(normalized)
    return discovered


def _is_required_provider_glm_model_id(model_id: str) -> bool:
    """Return true for new flagship GLM releases that must be published.

    Provider APIs expose many legacy GLM variants. Those are useful visibility
    warnings, but they should not block the release bot. GLM 5.2 is the current
    published flagship line; future GLM 5.x and 6.x+ releases should fail closed
    until the catalog has explicit routes/prices.
    """
    match = re.match(r"^z-ai/glm-(\d+)(?:\.(\d+))?(?:-[a-z0-9-]+)?$", model_id)
    if not match:
        return False
    major = int(match.group(1))
    minor = int(match.group(2) or 0)
    return major > 5 or (major == 5 and minor >= 2)


def _safe_fetch_error(url: str, exc: Exception) -> str:
    """Describe a failed fetch without copying headers or redirect URLs."""

    host = urlsplit(url).hostname or "unknown-host"
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if not isinstance(status, int):
        status = getattr(exc, "code", None)
    status_text = f" status={status}" if isinstance(status, int) else ""
    return f"{type(exc).__name__}{status_text} host={host}"


def _model_discovery_audit(
    *,
    fetch_text: Callable[[str], str],
    fetch_json: Callable[[str, tuple[str, ...]], Any] = _fetch_json,
    published_model_ids: set[str],
) -> tuple[list[str], list[str]]:
    warnings: list[str] = []
    info: list[str] = []
    try:
        zai_doc = fetch_text(ZAI_MODEL_DISCOVERY_URL)
    except Exception as exc:  # noqa: BLE001
        warnings.append(
            f"zai: model discovery fetch failed ({_safe_fetch_error(ZAI_MODEL_DISCOVERY_URL, exc)})"
        )
    else:
        discovered = _discover_zai_coding_plan_models(zai_doc)
        # This page is Z.AI's own availability contract. A route published by
        # another provider must not hide a missing Z.AI route.
        missing = sorted(discovered - _manifest_provider_model_ids("zai"))
        if missing:
            warnings.append(
                "zai: Coding Plan docs mention unpublished model(s) "
                f"{', '.join(missing)} — add/update provider_models/zai.json or the snapshot"
            )
        elif discovered:
            info.append(f"zai: model discovery matched catalog ({len(discovered)} docs model(s)) ✓")
        else:
            warnings.append("zai: model discovery found no GLM model ids in Coding Plan docs")

    for slug, url, env_names, normalize in _DISCOVERABLE_MANIFEST_PROVIDERS:
        routable, unresolved, classified, new_unresolved = _manifest_provider_model_state(slug)
        # Provider-specific manifests are authoritative for provider route
        # coverage.  The global catalog can contain the same model through a
        # different provider and must not hide this provider's unresolved row.
        published = routable or published_model_ids
        if slug in _RUNTIME_ONLY_DISCOVERY_SLUGS and not any(
            os.environ.get(env_name) for env_name in env_names
        ):
            info.append(
                f"{slug}: authenticated discovery intentionally disabled; "
                "committed manifest age gate active ✓"
            )
            continue
        try:
            payload = fetch_json(url, env_names)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"{slug}: model discovery fetch failed ({_safe_fetch_error(url, exc)})")
            continue
        discovered_ids: set[str] = set()
        for row in _json_model_rows(payload):
            if not _active_discovery_row(row):
                continue
            raw_id = row.get("id") or row.get("name")
            if not isinstance(raw_id, str):
                continue
            provider_alias = raw_id.removeprefix("models/")
            # Preserve already-classified provider-native IDs even when they
            # do not belong to a public model family. New unknown aliases do
            # not become launch blockers merely because an API leaked them.
            if provider_alias in published | unresolved | classified:
                discovered_ids.add(provider_alias)
                continue
            normalized = normalize(raw_id)
            if normalized:
                discovered_ids.add(normalized)
                if provider_alias:
                    discovered_ids.add(provider_alias)
        missing = discovered_ids - published - unresolved - classified
        unresolved_live = discovered_ids & unresolved
        required_missing = sorted(
            model_id
            for model_id in missing
            if _required_discovery_model(slug, model_id)
            or _is_required_provider_glm_model_id(model_id)
        )
        optional_missing = sorted(missing - set(required_missing))
        required_new_unresolved = sorted(
            model_id
            for model_id in unresolved_live & new_unresolved
            if _required_discovery_model(slug, model_id)
            or _is_required_provider_glm_model_id(model_id)
        )
        older_unresolved = sorted(unresolved_live - set(required_new_unresolved))
        if required_missing:
            sample = ", ".join(required_missing[:8])
            extra = f" (+{len(required_missing) - 8} more)" if len(required_missing) > 8 else ""
            warnings.append(
                f"{slug}: live model API lists required unpublished model(s) "
                f"{sample}{extra} — refresh provider_models/{slug}.json or add a "
                "provider-direct price source"
            )
        if required_new_unresolved:
            warnings.append(
                f"{slug}: newly discovered required model(s) still await a price: "
                f"{', '.join(required_new_unresolved[:8])}"
            )
        if optional_missing:
            warnings.append(
                f"{slug}: live model API lists review-only unpublished model(s) "
                f"{', '.join(optional_missing[:8])}"
            )
        if older_unresolved:
            warnings.append(
                f"{slug}: manifest has unresolved live model(s) awaiting price review: "
                f"{', '.join(older_unresolved[:8])}"
            )
        if not discovered_ids:
            warnings.append(f"{slug}: model discovery returned no model ids")
        elif not (missing or unresolved_live):
            info.append(f"{slug}: model discovery matched catalog ({len(discovered_ids)} id(s)) ✓")

    for slug, url, env_names in _GLM_DISCOVERABLE_PROVIDER_APIS:
        # Provider coverage is provider-specific. A model being routable via
        # one host must never mask a newly available route from another host.
        published = _manifest_provider_model_ids(slug)
        try:
            payload = fetch_json(url, env_names)
        except Exception as exc:  # noqa: BLE001
            warnings.append(
                f"{slug}: GLM model discovery fetch failed ({_safe_fetch_error(url, exc)})"
            )
            continue
        provider_rows = [row for row in _json_model_rows(payload) if _active_discovery_row(row)]
        if not provider_rows:
            warnings.append(f"{slug}: GLM model discovery returned no model ids")
            continue
        discovered = _provider_glm_model_ids(payload)
        if not discovered:
            info.append(f"{slug}: live model catalog currently lists no GLM routes ✓")
            continue
        missing = sorted(discovered - published)
        required_missing = [
            model_id for model_id in missing if _is_required_provider_glm_model_id(model_id)
        ]
        optional_missing = [model_id for model_id in missing if model_id not in required_missing]
        if required_missing:
            warnings.append(
                f"{slug}: live GLM current model API lists unpublished model(s) "
                f"{', '.join(required_missing)} — add/update provider_models/{slug}.json"
            )
        if optional_missing:
            warnings.append(
                f"{slug}: live GLM variant model API lists unpublished model(s) "
                f"{', '.join(optional_missing)} — review before publishing"
            )
        if discovered and not missing:
            info.append(f"{slug}: GLM model discovery matched catalog ({len(discovered)} id(s)) ✓")
    return warnings, info


def _audit_fallback_manifest(
    slug: str,
    *,
    max_age_days: int,
    now: dt.datetime,
) -> tuple[str | None, str | None]:
    """Return one hard warning or one coverage line for a fallback manifest."""

    manifest = MANIFEST_DIR / f"{slug}.json"
    source_label = "live scraper fallback manifest"
    if not manifest.exists():
        return (
            f"{slug}: NO price source ({source_label} missing) — "
            "catalog prices cannot refresh safely",
            None,
        )
    try:
        raw = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        return (
            f"{slug}: {source_label} is invalid ({type(exc).__name__})",
            None,
        )
    deadline = provider_manifest_valid_until(
        slug,
        raw,
        max_age_days=max_age_days,
    )
    if deadline is None or deadline == EXPIRED_PROVIDER_MANIFEST:
        return f"{slug}: {source_label} fails runtime route validity checks", None
    remaining_days = (deadline - now).total_seconds() / 86_400
    age = max_age_days - remaining_days
    if deadline <= now:
        return (
            f"{slug}: {source_label} is {age:.0f}d stale "
            f"(>= {max_age_days}d) — provider routes are quarantined",
            None,
        )
    module = _STALE_MANIFEST_PROVIDER_MODULE_BY_SLUG[slug]
    _result, manifest_error = read_stale_provider_manifest(
        slug=slug,
        manifest_path=manifest,
        include_in_price_index=bool(getattr(module, "INCLUDE_IN_PRICE_INDEX", True)),
    )
    if manifest_error is not None:
        return f"{slug}: {source_label} is invalid ({manifest_error})", None
    if remaining_days <= 3:
        remaining = max(remaining_days, 0)
        return (
            f"{slug}: {source_label} expires in {remaining:.0f}d "
            f"at the {max_age_days}d provider-route deadline",
            None,
        )
    return (
        None,
        f"{slug}: {source_label} {max(age, 0):.0f}d old (within {max_age_days}d) ✓",
    )


def _run_audit(
    max_age_days: int,
    now: dt.datetime,
    *,
    check_model_discovery: bool = True,
    fetch_text: Callable[[str], str] = _fetch_text,
) -> tuple[list[str], list[str], list[str]]:
    """Return (warnings, info, hard_fail_warnings)."""
    from trusted_router.catalog import GATEWAY_PREPAID_PROVIDER_SLUGS, MODELS

    scraper_modules = _scraper_slugs()
    scrapers = scraper_modules | {
        provider
        for provider, owner in _SHARED_LIVE_SCRAPER_OWNERS.items()
        if owner in scraper_modules
    }
    warnings: list[str] = []
    info: list[str] = []
    hard_fail_warnings: list[str] = []

    expiry_policy_mismatch = _OPTIONAL_STALE_MANIFEST_PROVIDER_SLUGS.symmetric_difference(
        EXPIRING_PROVIDER_MANIFEST_SLUGS
    )
    if expiry_policy_mismatch:
        warning = (
            f"fallback manifest expiry policy mismatch: {', '.join(sorted(expiry_policy_mismatch))}"
        )
        warnings.append(warning)
        hard_fail_warnings.append(warning)

    runtime_only_without_age_gate = _RUNTIME_ONLY_DISCOVERY_SLUGS - (
        set(GATEWAY_PREPAID_PROVIDER_SLUGS) & set(_OPTIONAL_STALE_MANIFEST_PROVIDER_SLUGS)
    )
    if runtime_only_without_age_gate:
        warning = (
            "runtime-only discovery provider(s) lack a prepaid manifest age gate: "
            f"{', '.join(sorted(runtime_only_without_age_gate))}"
        )
        warnings.append(warning)
        hard_fail_warnings.append(warning)

    if _DIRECT_OPENAI_DISCOVERY_SLUGS != _RUNTIME_ONLY_DISCOVERY_SLUGS:
        warning = (
            "authenticated discovery provider policy mismatch: modules="
            f"{', '.join(sorted(_DIRECT_OPENAI_DISCOVERY_SLUGS))}; policy="
            f"{', '.join(sorted(_RUNTIME_ONLY_DISCOVERY_SLUGS))}"
        )
        warnings.append(warning)
        hard_fail_warnings.append(warning)

    if check_model_discovery:
        video_prices = audit_video_price_sources(fetch_text)
        warnings.extend(video_prices.warnings)
        info.extend(video_prices.info)
        hard_fail_warnings.extend(video_prices.hard_failures)

    for slug in sorted(GATEWAY_PREPAID_PROVIDER_SLUGS):
        if slug in VIDEO_PRICE_PROVIDER_SLUGS:
            if not check_model_discovery:
                info.append(f"{slug}: official fixed-cost video price gate (network skipped) ✓")
            continue
        uses_stale_manifest_fallback = slug in _OPTIONAL_STALE_MANIFEST_PROVIDER_SLUGS
        if slug in scrapers and not uses_stale_manifest_fallback:
            info.append(f"{slug}: live scraper ✓")
            continue
        if uses_stale_manifest_fallback:
            warning, covered = _audit_fallback_manifest(
                slug,
                max_age_days=max_age_days,
                now=now,
            )
            if warning is not None:
                warnings.append(warning)
                # Runtime-only authenticated catalogs expire their own routes
                # dynamically. Their stale manifest remains an operator alert,
                # but cannot freeze unrelated providers' price publication.
                if slug not in EXPIRING_PROVIDER_MANIFEST_SLUGS:
                    hard_fail_warnings.append(warning)
            elif covered is not None:
                info.append(covered)
            continue
        manifest = MANIFEST_DIR / f"{slug}.json"
        source_label = "no scraper; manifest"
        if not manifest.exists():
            warning = (
                f"{slug}: NO price source ({source_label} missing) — "
                "catalog prices cannot refresh safely"
            )
            warnings.append(warning)
            hard_fail_warnings.append(warning)
            continue
        age = _manifest_age_days(manifest, now)
        if age is None:
            warning = f"{slug}: {source_label} has no parseable generated_at"
            warnings.append(warning)
            hard_fail_warnings.append(warning)
        elif age > max_age_days:
            warning = (
                f"{slug}: {source_label} is {age:.0f}d stale "
                f"(> {max_age_days}d) — prices may be wrong"
            )
            warnings.append(warning)
            hard_fail_warnings.append(warning)
        else:
            info.append(f"{slug}: {source_label} {max(age, 0):.0f}d old (within {max_age_days}d) ✓")

    # Discovery-only providers do not create billable routes. Keep stale or
    # malformed manifests visible as operator warnings, but never let one
    # freeze unrelated providers' price publication. Runtime routing has its
    # own provider-scoped expiry gate for every provider that can bill users.
    for slug in sorted(
        _OPTIONAL_STALE_MANIFEST_PROVIDER_SLUGS - set(GATEWAY_PREPAID_PROVIDER_SLUGS)
    ):
        warning, covered = _audit_fallback_manifest(
            slug,
            max_age_days=max_age_days,
            now=now,
        )
        if warning is not None:
            warnings.append(warning)
        elif covered is not None:
            info.append(covered)

    if check_model_discovery:
        discovery_warnings, discovery_info = _model_discovery_audit(
            fetch_text=fetch_text,
            published_model_ids=set(MODELS),
        )
        warnings.extend(discovery_warnings)
        hard_fail_warnings.extend(
            warning
            for warning in discovery_warnings
            if "required unpublished model" in warning
            or "newly discovered required model" in warning
            or "live GLM current model API lists unpublished" in warning
        )
        info.extend(discovery_info)

    return warnings, info, hard_fail_warnings


def audit(
    max_age_days: int,
    now: dt.datetime,
    *,
    check_model_discovery: bool = True,
    fetch_text: Callable[[str], str] = _fetch_text,
) -> tuple[list[str], list[str], list[str]]:
    """Return (warnings, info, hard_fail_warnings)."""
    return _run_audit(
        max_age_days,
        now,
        check_model_discovery=check_model_discovery,
        fetch_text=fetch_text,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS)
    parser.add_argument("--strict", action="store_true", help="exit 1 if any gaps found")
    parser.add_argument(
        "--skip-model-discovery",
        action="store_true",
        help="skip network checks for provider docs that announce new models before pricing pages",
    )
    parser.add_argument(
        "--strict-model-discovery",
        action="store_true",
        help="exit 1 when provider docs mention unpublished models or model discovery fails",
    )
    parser.add_argument("--now", default=None, help="ISO timestamp override (testing)")
    args = parser.parse_args(argv)

    now = dt.datetime.now(dt.UTC)
    if args.now:
        now = dt.datetime.fromisoformat(args.now.replace("Z", "+00:00"))
        if now.tzinfo is None:
            now = now.replace(tzinfo=dt.UTC)

    warnings, info, hard_fail_warnings = _run_audit(
        args.max_age_days,
        now,
        check_model_discovery=not args.skip_model_discovery,
    )

    print("## Price-source coverage")
    if warnings:
        print("")
        print("⚠️ Gaps (price changes for these may be MISSED — review manually):")
        for w in warnings:
            print(f"  - {w}")
            print(f"::warning title=Price/model coverage gap::{w}")
    else:
        print("")
        print("All prepaid providers have a fresh price source.")
    if info:
        print("")
        print("Covered:")
        for i in info:
            print(f"  - {i}")
    if args.strict and warnings:
        return 1
    if args.strict_model_discovery and hard_fail_warnings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
