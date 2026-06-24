"""AI IQ integration for public model-selection pages.

AI IQ publishes model-level public scores keyed by its own short IDs
(`opus-4.8`, `kimi-k2.7-code`, ...). TrustedRouter model IDs are provider
qualified (`anthropic/claude-opus-4.8`), so this module is the single place
that maps between the two.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Iterable, Mapping
from threading import RLock
from typing import Any, TypedDict, cast

AI_IQ_BASE_URL = "https://aiiq.org"
AI_IQ_API_MODELS_URL = f"{AI_IQ_BASE_URL}/api/models"
AI_IQ_CACHE_TTL_SECONDS = 6 * 60 * 60
AI_IQ_TIMEOUT_SECONDS = 2.0


class AiIqModel(TypedDict, total=False):
    id: str
    name: str
    provider: str
    rank: int
    iq: int
    url: str
    dimensions: dict[str, Any]
    methodology_version: str
    updated_at: str


class AiIqCatalogPayload(TypedDict):
    source: str
    source_url: str
    api_version: str
    methodology_version: str
    updated_at: str
    models: dict[str, AiIqModel]


_CACHE_LOCK = RLock()
_CACHE: tuple[float, dict[str, Any]] | None = None

_TEST_MODELS: tuple[dict[str, Any], ...] = (
    {
        "id": "gpt-5.5",
        "name": "GPT-5.5",
        "provider": "OpenAI",
        "rank": 2,
        "iq": 128,
        "url": f"{AI_IQ_BASE_URL}/models/gpt-5.5/",
        "dimensions": {"math": 130, "coding": 129},
        "updatedAt": "2026-06-23T17:41:57.352Z",
    },
    {
        "id": "opus-4.8",
        "name": "Claude Opus 4.8",
        "provider": "Anthropic",
        "rank": 3,
        "iq": 128,
        "url": f"{AI_IQ_BASE_URL}/models/opus-4.8/",
        "dimensions": {"reasoning": 130, "coding": 128},
        "updatedAt": "2026-06-23T17:41:57.352Z",
    },
    {
        "id": "gemini-3.1-pro",
        "name": "Gemini 3.1 Pro",
        "provider": "Google",
        "rank": 5,
        "iq": 123,
        "url": f"{AI_IQ_BASE_URL}/models/gemini-3.1-pro/",
        "dimensions": {"multimodal": 126, "reasoning": 121},
        "updatedAt": "2026-06-23T17:41:57.352Z",
    },
    {
        "id": "gemini-3.5-flash",
        "name": "Gemini 3.5 Flash",
        "provider": "Google",
        "rank": 7,
        "iq": 121,
        "url": f"{AI_IQ_BASE_URL}/models/gemini-3.5-flash/",
        "dimensions": {"multimodal": 122, "speed": 124},
        "updatedAt": "2026-06-23T17:41:57.352Z",
    },
    {
        "id": "kimi-k2.6",
        "name": "Kimi K2.6",
        "provider": "Moonshot AI",
        "rank": 9,
        "iq": 116,
        "url": f"{AI_IQ_BASE_URL}/models/kimi-k2.6/",
        "dimensions": {"coding": 118, "reasoning": 114},
        "updatedAt": "2026-06-23T17:41:57.352Z",
    },
    {
        "id": "glm-5.2",
        "name": "GLM 5.2",
        "provider": "Z.ai",
        "rank": 12,
        "iq": 114,
        "url": f"{AI_IQ_BASE_URL}/models/glm-5.2/",
        "dimensions": {"coding": 116, "reasoning": 113},
        "updatedAt": "2026-06-23T17:41:57.352Z",
    },
    {
        "id": "kimi-k2.7-code",
        "name": "Kimi K2.7 Code",
        "provider": "Moonshot AI",
        "rank": 16,
        "iq": 113,
        "url": f"{AI_IQ_BASE_URL}/models/kimi-k2.7-code/",
        "dimensions": {"coding": 119, "agentic": 114},
        "updatedAt": "2026-06-23T17:41:57.352Z",
    },
    {
        "id": "deepseek-v4-pro",
        "name": "DeepSeek V4 Pro",
        "provider": "DeepSeek",
        "rank": 25,
        "iq": 109,
        "url": f"{AI_IQ_BASE_URL}/models/deepseek-v4-pro/",
        "dimensions": {"coding": 111, "reasoning": 108},
        "updatedAt": "2026-06-23T17:41:57.352Z",
    },
    {
        "id": "minimax-m3",
        "name": "MiniMax M3",
        "provider": "MiniMax",
        "rank": 27,
        "iq": 109,
        "url": f"{AI_IQ_BASE_URL}/models/minimax-m3/",
        "dimensions": {"long_context": 112, "general": 108},
        "updatedAt": "2026-06-23T17:41:57.352Z",
    },
    {
        "id": "gemma-4-31b",
        "name": "Gemma 4 31B",
        "provider": "Google",
        "rank": 52,
        "iq": 95,
        "url": f"{AI_IQ_BASE_URL}/models/gemma-4-31b/",
        "dimensions": {"multimodal": 98, "general": 94},
        "updatedAt": "2026-06-23T17:41:57.352Z",
    },
)

_FALLBACK_PAYLOAD: dict[str, Any] = {
    "apiVersion": "1",
    "methodologyVersion": "2026-06-14-abstract-reorder-software-split",
    "updatedAt": "2026-06-23T17:41:57.352Z",
    "models": list(_TEST_MODELS),
}


def ai_iq_candidates(model_id: str) -> tuple[str, ...]:
    """Return possible AI IQ IDs for a TrustedRouter model id.

    Keep this conservative: exact short slugs first, then obvious aliases like
    `claude-opus-4.8` -> `opus-4.8` and `gemini-3.1-pro-preview` ->
    `gemini-3.1-pro`. We do not collapse unrelated product tiers.
    """
    cleaned = model_id.strip().lower()
    slug = cleaned.rsplit("/", 1)[-1]
    candidates: list[str] = [cleaned, slug]
    if slug.startswith("claude-"):
        candidates.append(slug.removeprefix("claude-"))
    if slug.endswith("-it"):
        candidates.append(slug.removesuffix("-it"))
    for candidate in list(candidates):
        candidates.append(_strip_release_suffix(candidate))
        if candidate.startswith("claude-"):
            candidates.append(_strip_release_suffix(candidate.removeprefix("claude-")))
    seen: set[str] = set()
    ordered: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            ordered.append(candidate)
    return tuple(ordered)


def ai_iq_for_model(model_id: str, *, test_mode: bool = False) -> AiIqModel | None:
    payload = _models_payload(test_mode=test_mode)
    lookup = _models_by_id(payload)
    methodology_version = _string(payload.get("methodologyVersion"))
    updated_at = _string(payload.get("updatedAt"))
    for candidate in ai_iq_candidates(model_id):
        row = lookup.get(candidate)
        if row is not None:
            return _public_model_row(
                row,
                methodology_version=methodology_version,
                updated_at=updated_at,
            )
    return None


def ai_iq_catalog_payload(
    model_ids: Iterable[str],
    *,
    test_mode: bool = False,
) -> AiIqCatalogPayload:
    payload = _models_payload(test_mode=test_mode)
    api_version = _string(payload.get("apiVersion")) or "1"
    methodology_version = _string(payload.get("methodologyVersion"))
    updated_at = _string(payload.get("updatedAt"))
    models: dict[str, AiIqModel] = {}
    for model_id in model_ids:
        ai_iq = ai_iq_for_model(model_id, test_mode=test_mode)
        if ai_iq is not None:
            models[model_id] = ai_iq
    return {
        "source": "AI IQ",
        "source_url": f"{AI_IQ_BASE_URL}/api/",
        "api_version": api_version,
        "methodology_version": methodology_version,
        "updated_at": updated_at,
        "models": models,
    }


def _strip_release_suffix(value: str) -> str:
    value = re.sub(r"-(?:preview|experimental)(?:-[a-z0-9.]+)?$", "", value)
    value = re.sub(r"-(?:instruct|thinking|chat)$", "", value)
    return value


def _models_payload(*, test_mode: bool) -> dict[str, Any]:
    global _CACHE
    if test_mode:
        return dict(_FALLBACK_PAYLOAD)
    now = time.monotonic()
    with _CACHE_LOCK:
        if _CACHE is not None and now - _CACHE[0] < AI_IQ_CACHE_TTL_SECONDS:
            return _CACHE[1]
    try:
        payload = _fetch_live_models()
    except (TimeoutError, OSError, urllib.error.URLError, json.JSONDecodeError, ValueError):
        with _CACHE_LOCK:
            if _CACHE is not None:
                return _CACHE[1]
        return dict(_FALLBACK_PAYLOAD)
    with _CACHE_LOCK:
        _CACHE = (time.monotonic(), payload)
    return payload


def _fetch_live_models() -> dict[str, Any]:
    request = urllib.request.Request(  # noqa: S310 - fixed HTTPS URL constant.
        AI_IQ_API_MODELS_URL,
        headers={
            "accept": "application/json",
            "user-agent": "TrustedRouter/1.0 (+https://trustedrouter.com)",
        },
    )
    with urllib.request.urlopen(  # noqa: S310 - fixed HTTPS URL constant.
        request,
        timeout=AI_IQ_TIMEOUT_SECONDS,
    ) as response:
        body = response.read()
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        msg = "AI IQ /api/models returned an unexpected payload"
        raise ValueError(msg)
    return cast(dict[str, Any], payload)


def _models_by_id(payload: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    rows: dict[str, Mapping[str, Any]] = {}
    raw_models = payload.get("models")
    if not isinstance(raw_models, list):
        return rows
    for raw in raw_models:
        if not isinstance(raw, Mapping):
            continue
        ai_iq_id = _string(raw.get("id")).lower()
        if ai_iq_id:
            rows[ai_iq_id] = raw
    return rows


def _public_model_row(
    row: Mapping[str, Any],
    *,
    methodology_version: str,
    updated_at: str,
) -> AiIqModel:
    ai_iq_id = _string(row.get("id"))
    url = _string(row.get("url")) or f"{AI_IQ_BASE_URL}/models/{ai_iq_id}/"
    output: AiIqModel = {
        "id": ai_iq_id,
        "name": _string(row.get("name")),
        "provider": _string(row.get("provider")),
        "url": url,
        "methodology_version": methodology_version,
        "updated_at": _string(row.get("updatedAt")) or updated_at,
    }
    rank = _int(row.get("rank"))
    iq = _int(row.get("iq"))
    if rank is not None:
        output["rank"] = rank
    if iq is not None:
        output["iq"] = iq
    dimensions = row.get("dimensions")
    if isinstance(dimensions, dict):
        output["dimensions"] = dict(dimensions)
    return output


def _string(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None
