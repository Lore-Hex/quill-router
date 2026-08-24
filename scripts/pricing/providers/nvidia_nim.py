"""NVIDIA hosted NIM discovery with fail-closed chat canaries.

NVIDIA's authenticated ``/models`` response deliberately spans several API
families and does not expose a model type. We retain every row for discovery,
but only publish rows that either appear in NVIDIA's LLM reference or do not
match a bounded non-chat family. Every newly eligible row must then pass the
provider's OpenAI-compatible chat endpoint before it becomes routable.
"""

from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from bs4 import BeautifulSoup

from scripts.pricing.base import (
    ModelPrice,
    ProviderPricingResult,
    fetch_html,
    validate,
)
from scripts.pricing.manifest import (
    apply_canary_results,
    models_requiring_canary,
    write_discovered_chat_manifest,
)
from scripts.pricing.model_ids import canonicalize_native_model_id
from scripts.pricing.openai_catalog import probe_openai_chat

SLUG = "nvidia-nim"
BASE_URL = "https://integrate.api.nvidia.com/v1"
CATALOG_URL = f"{BASE_URL}/models"
LLM_REFERENCE_URL = "https://docs.api.nvidia.com/nim/reference/llm-apis"
URL = "https://docs.api.nvidia.com/nim/docs/run-anywhere"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/nvidia-nim.json"
)

# NVIDIA does not publish a per-token price for the hosted API Catalog. Keep
# unpriced preview capacity out of cheapest-route selection with a conservative
# accounting rate. The provider remains excluded from the cross-provider price
# index, and this fallback can be replaced as soon as NVIDIA publishes a rate.
_CONSERVATIVE_HOSTED_PRICE = ModelPrice(2_000_000, 10_000_000)
_CANARY_FAILURE_REASONS = (
    "provider-canary-failed",
    "production-entitlement-required",
    "unsupported-chat-endpoint",
)
_NON_CHAT_MARKERS = (
    "/ai-synthetic-video-detector",
    "/arctic-embed",
    "/bge-",
    "content-safety",
    "/deplot",
    "/diffusiongemma-",
    "/embed-",
    "/embedqa-",
    "/ising-calibration-",
    "/muse-glimmer-",
    "/nemoretriever-parse",
    "/nemotron-parse",
    "/nv-embed",
    "/nvclip",
    "/rerank",
    "/riva-translate",
    "/topic-control",
    "-embed-",
    "-reward",
    "guardrail",
    "moderation",
    "nemoguard",
    "safety-guard",
)
_VISION_MARKERS = (
    "fuyu",
    "kosmos",
    "multimodal",
    "neva",
    "paligemma",
    "vila",
    "vision",
    "-vl",
)

_DISCOVERED_ROWS: dict[str, dict[str, Any]] = {}
INCLUDE_IN_PRICE_INDEX = False
MANIFEST_STALE_FALLBACK = True


def _canonical_model_id(native_id: str) -> str | None:
    """Normalize NVIDIA author aliases to TrustedRouter catalog IDs."""

    author, separator, model = native_id.strip().partition("/")
    if not separator:
        return None
    author_aliases = {
        "meta": "meta-llama",
        "nv-mistralai": "mistralai",
        "thinking machines": "thinkingmachines",
    }
    normalized = f"{author_aliases.get(author.casefold(), author)}/{model}"
    return canonicalize_native_model_id(normalized)


def parse_llm_reference_model_ids(html: str) -> frozenset[str]:
    """Extract the chat-model parents from NVIDIA's LLM reference sidebar."""

    soup = BeautifulSoup(html, "html.parser")
    heading = next(
        (
            item
            for item in soup.find_all("h2")
            if item.get_text(" ", strip=True).casefold() == "large language models"
        ),
        None,
    )
    section = heading.find_parent("section") if heading is not None else None
    if section is None:
        raise RuntimeError("nvidia-nim: LLM reference has no Large Language models section")

    result: set[str] = set()
    for link in section.select("a.Sidebar-link_parent"):
        label = link.get_text(" ", strip=True).replace(" / ", "/")
        model_id = _canonical_model_id(label)
        if model_id is not None:
            result.add(model_id)
    if len(result) < 10:
        raise RuntimeError("nvidia-nim: LLM reference returned fewer than 10 chat models")
    return frozenset(result)


def _looks_like_chat_model(model_id: str) -> bool:
    """Select plausible chat rows; the paid-path response canary is definitive."""

    return not any(marker in model_id for marker in _NON_CHAT_MARKERS)


def _input_modalities(model_id: str) -> list[str]:
    if any(marker in model_id for marker in _VISION_MARKERS):
        return ["text", "image"]
    return ["text"]


def _scheduled_canary_bucket(model_id: str) -> int:
    """Spread healthy-route rechecks evenly across 24 hourly refreshes."""

    return hashlib.sha256(model_id.encode("utf-8")).digest()[0] % 24


def _scheduled_routable_rechecks(
    candidates: set[str],
    *,
    now: datetime | None = None,
) -> frozenset[str]:
    if not MANIFEST_PATH.exists():
        return frozenset()
    try:
        raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return frozenset()
    rows = raw.get("models") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        return frozenset()
    hour = (now or datetime.now(UTC)).astimezone(UTC).hour
    return frozenset(
        model_id
        for row in rows
        if isinstance(row, dict)
        and isinstance((model_id := row.get("id")), str)
        and model_id in candidates
        and row.get("routable", True) is not False
        and _scheduled_canary_bucket(model_id) == hour
    )


def _models_to_canary(
    candidates: set[str],
    *,
    now: datetime | None = None,
) -> frozenset[str]:
    """Canary new/held routes and a bounded daily slice of healthy routes."""

    checked: set[str] = set()
    for reason in _CANARY_FAILURE_REASONS:
        checked.update(
            models_requiring_canary(
                MANIFEST_PATH,
                candidates,
                failure_reason=reason,
            )
        )
    checked.update(_scheduled_routable_rechecks(candidates, now=now))
    return frozenset(checked)


def _run_canaries(
    checked: frozenset[str],
    *,
    api_key: str,
    upstream_ids: dict[str, str],
) -> frozenset[str]:
    if not checked:
        return frozenset()

    def healthy(model_id: str) -> bool:
        return probe_openai_chat(
            base_url=BASE_URL,
            api_key=api_key,
            model=upstream_ids[model_id],
            max_tokens=64,
            require_message=True,
        )

    # Bounded concurrency keeps the first migration practical without turning
    # an hourly catalog refresh into a provider load test.
    with ThreadPoolExecutor(max_workers=min(3, len(checked))) as pool:
        results = dict(zip(checked, pool.map(healthy, checked), strict=True))
    return frozenset(model_id for model_id, passed in results.items() if passed)


def fetch() -> ProviderPricingResult:
    """Discover the live catalog and canary newly eligible chat routes."""

    global _DISCOVERED_ROWS  # noqa: PLW0603
    key = os.environ.get("NVIDIA_NIM_API_KEY", "").strip()
    if not key:
        raise RuntimeError("nvidia-nim: NVIDIA_NIM_API_KEY is required")

    with httpx.Client(timeout=20, follow_redirects=False) as client:
        response = client.get(
            CATALOG_URL,
            headers={"Authorization": f"Bearer {key}"},
        )
        response.raise_for_status()
        payload = response.json()
    raw_models = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(raw_models, list):
        raise RuntimeError("nvidia-nim: authenticated catalog has no data list")

    documented = parse_llm_reference_model_ids(fetch_html(LLM_REFERENCE_URL))
    discovered: dict[str, dict[str, Any]] = {}
    candidates: set[str] = set()
    upstream_ids: dict[str, str] = {}
    for raw in raw_models:
        upstream_id = raw.get("id") if isinstance(raw, dict) else None
        if not isinstance(upstream_id, str):
            continue
        model_id = _canonical_model_id(upstream_id)
        if model_id is None:
            continue

        prior_upstream_id = upstream_ids.get(model_id)
        if prior_upstream_id is not None and prior_upstream_id != upstream_id:
            raise RuntimeError(
                "nvidia-nim: canonical model collision for "
                f"{model_id}: {prior_upstream_id!r} and {upstream_id!r}"
            )
        if model_id in discovered:
            continue

        upstream_ids[model_id] = upstream_id
        row: dict[str, Any] = {
            "id": model_id,
            "upstream_id": upstream_id,
            "display_name": upstream_id,
            "status": 1,
        }
        if _looks_like_chat_model(model_id):
            candidates.add(model_id)
            row.update(
                {
                    "model_type": "chat",
                    "input_modalities": _input_modalities(model_id),
                    "output_modalities": ["text"],
                    "endpoints": ["chat/completions"],
                }
            )
        else:
            row.update(
                {
                    "model_type": "discovery",
                    "input_modalities": [],
                    "output_modalities": [],
                    "endpoints": [],
                    "routable": False,
                    "routable_reason": "unsupported-chat-endpoint",
                }
            )
        discovered[model_id] = row

    if len(discovered) < 10:
        raise RuntimeError("nvidia-nim: live catalog unexpectedly returned fewer than 10 models")
    if len(candidates) < 10:
        raise RuntimeError("nvidia-nim: fewer than 10 chat candidates survived classification")

    checked = _models_to_canary(candidates)
    healthy = _run_canaries(checked, api_key=key, upstream_ids=upstream_ids)
    apply_canary_results(
        discovered,
        checked_model_ids=checked,
        healthy_model_ids=healthy,
    )

    # Keep a positive accounting price on discovery-only rows as well. The
    # shared manifest reconciler then preserves their explicit non-chat hold
    # instead of replacing it with the generic awaiting-price state.
    prices = {model_id: _CONSERVATIVE_HOSTED_PRICE for model_id in discovered}
    errors = validate(prices, discovered)
    if errors:
        raise RuntimeError("; ".join(errors))
    _DISCOVERED_ROWS = discovered
    return ProviderPricingResult(
        slug=SLUG,
        prices=prices,
        source="api",
        fetched_url=URL,
        include_in_price_index=INCLUDE_IN_PRICE_INDEX,
        notes=[
            f"discovered {len(discovered)} live models, {len(documented)} documented LLMs, "
            f"and {len(candidates)} chat candidates",
            f"canaried {len(checked)} new or held routes; {len(healthy)} passed",
        ],
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    """Publish canaried chat rows while retaining non-chat discovery rows."""

    if not _DISCOVERED_ROWS:
        raise RuntimeError("nvidia-nim: fetch must succeed before writing manifest")
    if MANIFEST_PATH.exists():
        raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        rows = raw.get("models") if isinstance(raw, dict) else None
        if not isinstance(rows, list):
            raise RuntimeError("nvidia-nim: existing manifest has no models list")
        current_chat_ids = {
            model_id
            for model_id, row in _DISCOVERED_ROWS.items()
            if row.get("model_type") == "chat"
        }
        migrated = False
        for row in rows:
            if not isinstance(row, dict):
                continue
            reason = row.get("routable_reason")
            model_id = row.get("id")
            migratable = reason == "production-entitlement-required" or (
                reason == "unsupported-chat-endpoint" and model_id in current_chat_ids
            )
            if not migratable:
                continue
            # These are machine-owned classification holds, not operator
            # policy. Map a now-chat-capable row to managed canary state so a
            # fresh success can supersede it; true non-chat rows stay held.
            row["routable_reason"] = "provider-canary-failed"
            migrated = True
        if migrated:
            MANIFEST_PATH.write_text(
                json.dumps(raw, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
    return write_discovered_chat_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        discovered_rows=_DISCOVERED_ROWS,
        source_url=CATALOG_URL,
        pricing_source_url=URL,
    )
