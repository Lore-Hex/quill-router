"""Redpill's own catalog and credential, separate from Phala inference."""

from pathlib import Path

from scripts.pricing.providers._direct_openai import DirectOpenAIProvider, DirectOpenAIProviderSpec
from trusted_router.provider_contracts import redpill_token_limit_field as max_tokens_field

SLUG = "redpill"
BASE_URL = "https://api.redpill.ai/v1"
URL = f"{BASE_URL}/models"
MANIFEST_PATH = Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/redpill.json"
MANIFEST_STALE_FALLBACK = True

CATALOG = DirectOpenAIProvider(
    DirectOpenAIProviderSpec(
        slug=SLUG, base_url=BASE_URL, api_key_env="REDPILL_API_KEY",
        explicit_model_map={}, catalog_url=URL,
        canary_max_tokens=2048, canary_max_tokens_field=max_tokens_field,
        canary_expected_content="PONG",
        canary_prompt="Reply with exactly PONG and nothing else.",
    ),
    manifest_path=MANIFEST_PATH,
)
UPSTREAM_ID_MAP = CATALOG.upstream_id_map
fetch = CATALOG.fetch
write_provider_manifest = CATALOG.write_provider_manifest
