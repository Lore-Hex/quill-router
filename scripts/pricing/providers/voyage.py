"""Voyage AI first-party embedding pricing refresh."""

from __future__ import annotations

import json
import re
from pathlib import Path

from scripts.pricing.base import ProviderPricingResult, fetch_provider
from scripts.pricing.manifest import write_embedding_provider_manifest

SLUG = "voyage"
URL = "https://docs.voyageai.com/docs/pricing.md"
# Query plus one document, from docs.voyageai.com/docs/reranker.
RERANK_CONTEXT_LIMITS = {
    "rerank-3": 32_000,
    "rerank-3-lite": 32_000,
    "rerank-2.5": 32_000,
    "rerank-2.5-lite": 32_000,
    "rerank-2": 16_000,
    "rerank-2-lite": 8_000,
    "rerank-1": 8_000,
    "rerank-lite-1": 4_000,
}
EXPECTED_MODELS = [
    "voyage/voyage-4-large",
    "voyage/voyage-4",
    "voyage/voyage-4-lite",
    "voyage/voyage-context-3",
    "voyage/voyage-code-3",
    "voyage/voyage-finance-2",
    "voyage/voyage-law-2",
    "voyage/voyage-code-2",
    "voyage/voyage-3-large",
]
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "voyage.json"
)


def fetch() -> ProviderPricingResult:
    sources: list[str] = []

    def capture(source: str) -> str:
        sources.append(source)
        return source

    result = fetch_provider(slug=SLUG, url=URL, expected_models=EXPECTED_MODELS,
                            source_transform=capture)
    for model_id in sorted(set(re.findall(r"`(rerank-[^`]+)`", "\n".join(sources)))):
        if f"voyage/{model_id}" not in result.prices:
            result.notes.append(f"skipped {model_id}: no positive per-token price in the supported tables")
    result.price_index_model_ids = frozenset(key for key in result.prices
                                             if not key.startswith("voyage/rerank-"))
    return result


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    notes = write_embedding_provider_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        required_model_ids=frozenset({"voyage/voyage-3-large"}),
        rerank_model_ids=frozenset(key for key in result.prices if key.startswith("voyage/rerank-")),
    )
    raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    for row in raw["models"]:
        if not isinstance(row, dict) or row.get("model_type") != "rerank":
            continue
        context = RERANK_CONTEXT_LIMITS.get(row.get("upstream_id"))
        if context is None:
            row["routable"] = False
            row["routable_reason"] = "api-model-unconfirmed"
            notes.append(f"held {row.get('id')}: upstream id absent from API model documentation")
        elif row.get("context_length") is None:
            row["context_length"] = context
    MANIFEST_PATH.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return notes
