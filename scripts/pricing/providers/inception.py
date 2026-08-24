"""Inception Labs public priced Mercury catalog and keyed canary."""

from pathlib import Path

from scripts.pricing.providers._direct_openai import DirectOpenAIProvider, DirectOpenAIProviderSpec

SLUG = "inception"
BASE_URL = "https://api.inceptionlabs.ai/v1"
URL = f"{BASE_URL}/models"
MANIFEST_PATH = Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/inception.json"
MANIFEST_STALE_FALLBACK = True
EXPLICIT_MODEL_MAP = {"mercury-2": "inception/mercury-2"}
CATALOG = DirectOpenAIProvider(
    DirectOpenAIProviderSpec(
        slug=SLUG,
        base_url=BASE_URL,
        api_key_env="INCEPTION_API_KEY",
        explicit_model_map=EXPLICIT_MODEL_MAP,
        expected_models=("inception/mercury-2",),
    ),
    manifest_path=MANIFEST_PATH,
)
UPSTREAM_ID_MAP = CATALOG.upstream_id_map
fetch = CATALOG.fetch
write_provider_manifest = CATALOG.write_provider_manifest
