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

Run in refresh-prices.yml as a non-failing visibility step (writes to the
Actions run summary). Pass --strict to fail CI on any gap.
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

ROOT = Path(__file__).resolve().parent.parent
PROVIDERS_DIR = ROOT / "scripts" / "pricing" / "providers"
MANIFEST_DIR = ROOT / "src" / "trusted_router" / "data" / "provider_models"
DEFAULT_MAX_AGE_DAYS = 14
ZAI_MODEL_DISCOVERY_URL = "https://r.jina.ai/https://docs.z.ai/devpack/latest-model"
_ZAI_MODEL_RE = re.compile(r"\bglm-\d+(?:\.\d+)?(?:-[a-z0-9]+(?:-[a-z0-9]+)*)?(?:\[1m\])?\b", re.I)


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
    if value in {"gpt-oss-120b", "zai-glm-4.7"}:
        return f"cerebras/{value}"
    return value or None


def _gemini_model_id(native_id: str) -> str | None:
    value = native_id.removeprefix("models/").strip()
    if not value:
        return None
    return f"google/{value.casefold()}"


_FIREWORKS_MODEL_IDS = {
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
    return _FIREWORKS_MODEL_IDS.get(value, value)


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
    "moonshotai/Kimi-K2.7-Code": "moonshotai/kimi-k2.7-code",
}


def _baseten_model_id(native_id: str) -> str | None:
    value = native_id.strip()
    if not value:
        return None
    return _BASETEN_MODEL_IDS.get(value, _identity_model_id(value))


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
    return None


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


_DISCOVERABLE_MANIFEST_PROVIDERS: tuple[
    tuple[str, str, tuple[str, ...], Callable[[str], str | None]], ...
] = (
    (
        "kimi",
        "https://api.moonshot.ai/v1/models",
        ("KIMI_API_KEY", "MOONSHOT_API_KEY"),
        _kimi_model_id,
    ),
    (
        "cerebras",
        "https://api.cerebras.ai/v1/models",
        ("CEREBRAS_API_KEY",),
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
        _identity_model_id,
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
        "https://api.together.xyz/v1/models",
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
        p.stem for p in PROVIDERS_DIR.glob("*.py") if p.stem not in {"__init__", "base", "_base"}
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
    if token and not is_gemini:
        headers["Authorization"] = f"Bearer {token}"
    request_url = url
    if is_gemini and token:
        sep = "&" if "?" in url else "?"
        request_url = f"{url}{sep}key={token}"
    req = urllib.request.Request(request_url, headers=headers)  # noqa: S310
    with urllib.request.urlopen(req, timeout=20) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8", errors="replace"))


def _json_model_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        rows = payload.get("data")
        if not isinstance(rows, list):
            rows = payload.get("models")
    else:
        rows = payload
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _manifest_provider_model_ids(slug: str) -> set[str]:
    path = MANIFEST_DIR / f"{slug}.json"
    if not path.exists():
        return set()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    ids: set[str] = set()
    for row in raw.get("models") or []:
        if not isinstance(row, dict):
            continue
        for key in ("id", "upstream_id"):
            value = row.get(key)
            if isinstance(value, str) and value:
                ids.add(value)
    return ids


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
    slug = re.sub(r"-fp8(?:-block)?$", "", slug)
    return f"z-ai/{slug}"


def _provider_glm_model_ids(payload: Any) -> set[str]:
    discovered: set[str] = set()
    for row in _json_model_rows(payload):
        for key in ("id", "name", "title"):
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
        warnings.append(f"zai: model discovery fetch failed ({type(exc).__name__}: {exc})")
        return warnings, info

    discovered = _discover_zai_coding_plan_models(zai_doc)
    missing = sorted(discovered - published_model_ids)
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
        published = published_model_ids | _manifest_provider_model_ids(slug)
        try:
            payload = fetch_json(url, env_names)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"{slug}: model discovery fetch failed ({type(exc).__name__}: {exc})")
            continue
        discovered_ids: set[str] = set()
        for row in _json_model_rows(payload):
            raw_id = row.get("id") or row.get("name")
            if not isinstance(raw_id, str):
                continue
            normalized = normalize(raw_id)
            if normalized:
                discovered_ids.add(normalized)
                provider_alias = raw_id.removeprefix("models/")
                if provider_alias:
                    discovered_ids.add(provider_alias)
        missing = sorted(discovered_ids - published)
        if missing:
            sample = ", ".join(missing[:8])
            extra = f" (+{len(missing) - 8} more)" if len(missing) > 8 else ""
            warnings.append(
                f"{slug}: live model API lists unpublished model(s) {sample}{extra} "
                f"— refresh provider_models/{slug}.json or add a provider-direct price source"
            )
        elif discovered_ids:
            info.append(f"{slug}: model discovery matched catalog ({len(discovered_ids)} id(s)) ✓")
        else:
            warnings.append(f"{slug}: model discovery returned no model ids")

    for slug, url, env_names in _GLM_DISCOVERABLE_PROVIDER_APIS:
        published = published_model_ids | _manifest_provider_model_ids(slug)
        try:
            payload = fetch_json(url, env_names)
        except Exception as exc:  # noqa: BLE001
            warnings.append(
                f"{slug}: GLM model discovery fetch failed ({type(exc).__name__}: {exc})"
            )
            continue
        discovered = _provider_glm_model_ids(payload)
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


def _run_audit(
    max_age_days: int,
    now: dt.datetime,
    *,
    check_model_discovery: bool = True,
    fetch_text: Callable[[str], str] = _fetch_text,
) -> tuple[list[str], list[str], list[str]]:
    """Return (warnings, info, hard_fail_warnings)."""
    from trusted_router.catalog import GATEWAY_PREPAID_PROVIDER_SLUGS, MODELS

    scrapers = _scraper_slugs()
    warnings: list[str] = []
    info: list[str] = []
    hard_fail_warnings: list[str] = []

    for slug in sorted(GATEWAY_PREPAID_PROVIDER_SLUGS):
        if slug in scrapers:
            info.append(f"{slug}: live scraper ✓")
            continue
        manifest = MANIFEST_DIR / f"{slug}.json"
        if not manifest.exists():
            warnings.append(
                f"{slug}: NO price source (no scraper, no manifest) — "
                f"catalog prices are hand-coded and never refresh"
            )
            continue
        age = _manifest_age_days(manifest, now)
        if age is None:
            warnings.append(f"{slug}: no scraper; manifest has no parseable generated_at")
        elif age > max_age_days:
            warnings.append(
                f"{slug}: no scraper; manifest is {age:.0f}d stale "
                f"(> {max_age_days}d) — prices may be wrong"
            )
        else:
            info.append(f"{slug}: manifest {age:.0f}d old (within {max_age_days}d) ✓")

    if check_model_discovery:
        discovery_warnings, discovery_info = _model_discovery_audit(
            fetch_text=fetch_text,
            published_model_ids=set(MODELS),
        )
        warnings.extend(discovery_warnings)
        hard_fail_warnings.extend(
            warning
            for warning in discovery_warnings
            if warning.startswith("zai:")
            or warning.startswith("kimi:")
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
) -> tuple[list[str], list[str]]:
    """Return (warnings, info)."""
    warnings, info, _hard_fail_warnings = _run_audit(
        max_age_days,
        now,
        check_model_discovery=check_model_discovery,
        fetch_text=fetch_text,
    )
    return warnings, info


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
