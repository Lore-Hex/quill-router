"""AkashML authenticated priced catalog and route canaries."""

from pathlib import Path

from scripts.pricing.providers._direct_openai import DirectOpenAIProvider, DirectOpenAIProviderSpec

SLUG = "akashml"
BASE_URL = "https://api.akashml.com/v1"
URL = f"{BASE_URL}/models"
MANIFEST_PATH = Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/akashml.json"
MANIFEST_STALE_FALLBACK = True
CATALOG = DirectOpenAIProvider(
    DirectOpenAIProviderSpec(
        slug=SLUG,
        base_url=BASE_URL,
        api_key_env="AKASHML_API_KEY",
        explicit_model_map={},
        # DeepSeek was absent from /models and returned model_not_found on
        # 2026-09-08. Requiring it would pin every refresh to the stale catalog.
        expected_models=("qwen/qwen3.8-27b",),
    ),
    manifest_path=MANIFEST_PATH,
)
UPSTREAM_ID_MAP = CATALOG.upstream_id_map
fetch = CATALOG.fetch
write_provider_manifest = CATALOG.write_provider_manifest
