"""Inceptron live priced catalog with independent route canaries."""

from pathlib import Path

from scripts.pricing.providers._direct_openai import (
    DirectOpenAIProvider,
    DirectOpenAIProviderSpec,
)

SLUG = "inceptron"
URL = "https://api.inceptron.io/v1/models"
MANIFEST_PATH = Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/inceptron.json"
EXPECTED_MODELS = ["z-ai/glm-5.2", "moonshotai/kimi-k2.6", "moonshotai/kimi-k2.7-code"]
_NATIVE_TO_MODEL_ID = {
    "MiniMaxAI/MiniMax-M2.5": "minimax/minimax-m2.5",
    "zai-org/GLM-5.2": "z-ai/glm-5.2",
    "zai-org/GLM-5.3": "z-ai/glm-5.3",
    "zai-org/GLM-5.3-Flash": "z-ai/glm-5.3-flash",
    "moonshotai/Kimi-K2.6": "moonshotai/kimi-k2.6",
    "moonshotai/Kimi-K2.7-Code": "moonshotai/kimi-k2.7-code",
    "deepseek-ai/DeepSeek-V4-Flash-0731": "deepseek/deepseek-v4-flash-0731",
}
CATALOG = DirectOpenAIProvider(
    DirectOpenAIProviderSpec(
        slug=SLUG,
        base_url="https://api.inceptron.io/v1",
        api_key_env="INCEPTRON_API_KEY",
        explicit_model_map=_NATIVE_TO_MODEL_ID,
        expected_models=tuple(EXPECTED_MODELS),
    ),
    manifest_path=MANIFEST_PATH,
)
UPSTREAM_ID_MAP = CATALOG.upstream_id_map
fetch = CATALOG.fetch
write_provider_manifest = CATALOG.write_provider_manifest
