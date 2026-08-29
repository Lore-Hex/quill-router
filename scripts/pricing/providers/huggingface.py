"""Hugging Face Inference Providers with deterministic downstream pinning."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from scripts.pricing.providers._direct_openai import (
    DirectOpenAIProvider,
    DirectOpenAIProviderSpec,
)

SLUG = "huggingface"
BASE_URL = "https://router.huggingface.co/v1"
URL = f"{BASE_URL}/models"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "huggingface.json"
)
MANIFEST_STALE_FALLBACK = True
_PROVIDER_SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def _positive_decimal(value: object) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed > 0 else None


def _normalize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Choose one live, priced downstream per model and pin it in the ID.

    Hugging Face prices are dollars per million tokens, while the shared
    OpenAI-catalog normalizer consumes dollars per token. The conversion stays
    in Decimal form so binary JSON float artifacts cannot move an integer
    microdollar rate by one unit.
    """

    normalized: dict[str, dict[str, Any]] = {}
    million = Decimal("1000000")
    for source in rows:
        model_id = source.get("id")
        providers = source.get("providers")
        if not isinstance(model_id, str) or not model_id.strip() or not isinstance(providers, list):
            continue
        model_id = model_id.strip()

        candidates: list[tuple[Decimal, Decimal, str, dict[str, Any]]] = []
        for provider in providers:
            if not isinstance(provider, dict) or provider.get("status") != "live":
                continue
            provider_slug = provider.get("provider")
            pricing = provider.get("pricing")
            if (
                not isinstance(provider_slug, str)
                or _PROVIDER_SLUG_RE.fullmatch(provider_slug) is None
                or not isinstance(pricing, dict)
            ):
                continue
            prompt = _positive_decimal(pricing.get("input"))
            completion = _positive_decimal(pricing.get("output"))
            if prompt is None or completion is None:
                continue
            candidates.append((completion, prompt, provider_slug, provider))
        if not candidates:
            continue

        completion, prompt, provider_slug, provider = min(
            candidates,
            key=lambda candidate: (candidate[0], candidate[1], candidate[2]),
        )
        architecture = source.get("architecture")
        row: dict[str, Any] = {
            "id": model_id,
            "upstream_id": f"{model_id}:{provider_slug}",
            "name": f"{model_id} via {provider_slug}",
            "pricing": {
                "input": str(prompt / million),
                "output": str(completion / million),
            },
        }
        context_length = provider.get("context_length") or source.get("context_length")
        if context_length is not None:
            row["context_length"] = context_length
        if isinstance(architecture, dict):
            for field in ("input_modalities", "output_modalities"):
                value = architecture.get(field)
                if isinstance(value, list):
                    row[field] = value
        collision_key = model_id.casefold()
        previous = normalized.get(collision_key)
        if previous is not None and previous != row:
            raise RuntimeError(
                "huggingface: duplicate model rows resolve to different pinned routes: "
                f"{previous['upstream_id']!r} != {row['upstream_id']!r}"
            )
        normalized[collision_key] = row
    return sorted(normalized.values(), key=lambda row: str(row["id"]).casefold())


CATALOG = DirectOpenAIProvider(
    DirectOpenAIProviderSpec(
        slug=SLUG,
        base_url=BASE_URL,
        api_key_env=("HUGGING_FACE_API_KEY", "HF_TOKEN"),
        explicit_model_map={},
        catalog_url=URL,
        pricing_source_url="https://huggingface.co/docs/inference-providers/pricing",
        normalize_rows=_normalize_rows,
        accept_normalized_upstream_id=True,
        canary_max_tokens=4,
        canary_concurrency=8,
    ),
    manifest_path=MANIFEST_PATH,
)
UPSTREAM_ID_MAP = CATALOG.upstream_id_map
fetch = CATALOG.fetch
write_provider_manifest = CATALOG.write_provider_manifest
