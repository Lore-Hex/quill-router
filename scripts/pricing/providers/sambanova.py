"""SambaNova public priced catalog and keyed route canaries."""

from pathlib import Path

from scripts.pricing.providers._direct_openai import DirectOpenAIProvider, DirectOpenAIProviderSpec

SLUG = "sambanova"
BASE_URL = "https://api.sambanova.ai/v1"
URL = f"{BASE_URL}/models"
MANIFEST_PATH = Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/sambanova.json"
MANIFEST_STALE_FALLBACK = True
EXPLICIT_MODEL_MAP = {
    "DeepSeek-V3.1": "deepseek/deepseek-v3.1",
    "DeepSeek-V3.2": "deepseek/deepseek-v3.2",
    "Meta-Llama-3.3-70B-Instruct": "meta-llama/llama-3.3-70b-instruct",
    "MiniMax-M2.7": "minimax/minimax-m2.7",
    "MiniMax-M3": "minimax/minimax-m3",
    "gemma-4-31B-it": "google/gemma-4-31b-it",
    "gpt-oss-120b": "openai/gpt-oss-120b",
}
CATALOG = DirectOpenAIProvider(
    DirectOpenAIProviderSpec(
        slug=SLUG,
        base_url=BASE_URL,
        api_key_env="SAMBANOVA_API_KEY",
        explicit_model_map=EXPLICIT_MODEL_MAP,
        expected_models=("minimax/minimax-m3", "openai/gpt-oss-120b"),
    ),
    manifest_path=MANIFEST_PATH,
)
UPSTREAM_ID_MAP = CATALOG.upstream_id_map
fetch = CATALOG.fetch
write_provider_manifest = CATALOG.write_provider_manifest
