"""Cohere first-party embedding pricing refresh."""

from __future__ import annotations

from pathlib import Path

from scripts.pricing.base import ProviderPricingResult, fetch_provider
from scripts.pricing.manifest import write_embedding_provider_manifest

SLUG = "cohere"
URL = "https://cohere.com/pricing"
EXPECTED_MODELS = ["cohere/embed-v4.0"]
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "cohere.json"
)


def fetch() -> ProviderPricingResult:
    return fetch_provider(slug=SLUG, url=URL, expected_models=EXPECTED_MODELS)


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    return write_embedding_provider_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        required_model_ids=frozenset(EXPECTED_MODELS),
    )
