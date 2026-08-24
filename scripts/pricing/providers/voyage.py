"""Voyage AI first-party embedding pricing refresh."""

from __future__ import annotations

from pathlib import Path

from scripts.pricing.base import ProviderPricingResult, fetch_provider
from scripts.pricing.manifest import write_embedding_provider_manifest

SLUG = "voyage"
URL = "https://docs.voyageai.com/docs/pricing.md"
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
    return fetch_provider(slug=SLUG, url=URL, expected_models=EXPECTED_MODELS)


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    return write_embedding_provider_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        required_model_ids=frozenset({"voyage/voyage-3-large"}),
    )
