"""Arcee authenticated catalog joined to first-party token prices."""

from pathlib import Path

from scripts.pricing.providers._direct_openai import DirectOpenAIProvider, DirectOpenAIProviderSpec

SLUG = "arcee"
BASE_URL = "https://api.arcee.ai/api/v1"
URL = f"{BASE_URL}/models"
PRICING_URL = "https://www.arcee.ai/models"
MANIFEST_PATH = Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/arcee.json"
EXPLICIT_MODEL_MAP = {
    "trinity-mini": "arcee-ai/trinity-mini",
    "trinity-large-preview": "arcee-ai/trinity-large-preview",
    "trinity-large-thinking": "arcee-ai/trinity-large-thinking",
    "deepseek/deepseek-v4-flash-latest": "deepseek/deepseek-v4-flash",
}
CATALOG = DirectOpenAIProvider(
    DirectOpenAIProviderSpec(
        slug=SLUG,
        base_url=BASE_URL,
        api_key_env="ARCEE_API_KEY",
        explicit_model_map=EXPLICIT_MODEL_MAP,
        namespace_unqualified="arcee-ai",
        expected_models=("deepseek/deepseek-v4-pro-0813", "moonshotai/kimi-k3"),
        pricing_source_url=PRICING_URL,
    ),
    manifest_path=MANIFEST_PATH,
)
UPSTREAM_ID_MAP = CATALOG.upstream_id_map
fetch = CATALOG.fetch
write_provider_manifest = CATALOG.write_provider_manifest
