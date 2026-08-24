"""Aion Labs public priced catalog and keyed route canaries."""

from pathlib import Path

from scripts.pricing.providers._direct_openai import DirectOpenAIProvider, DirectOpenAIProviderSpec

SLUG = "aion-labs"
BASE_URL = "https://api.aionlabs.ai/v1"
URL = f"{BASE_URL}/models"
MANIFEST_PATH = Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/aion-labs.json"
MANIFEST_STALE_FALLBACK = True
CATALOG = DirectOpenAIProvider(
    DirectOpenAIProviderSpec(
        slug=SLUG,
        base_url=BASE_URL,
        api_key_env="AION_LABS_API_KEY",
        explicit_model_map={},
        expected_models=("aion-labs/aion-2.0", "aion-labs/aion-3.0", "aion-labs/aion-3.0-mini"),
    ),
    manifest_path=MANIFEST_PATH,
)
UPSTREAM_ID_MAP = CATALOG.upstream_id_map
fetch = CATALOG.fetch
write_provider_manifest = CATALOG.write_provider_manifest
