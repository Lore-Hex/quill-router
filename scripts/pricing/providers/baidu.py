"""Baidu Qianfan international catalog with exact embedded USD token prices."""

from pathlib import Path

from scripts.pricing.providers._direct_openai import DirectOpenAIProvider, DirectOpenAIProviderSpec

SLUG = "baidu"
BASE_URL = "https://qianfan.baidubce.com/v2"
CATALOG_URL = "https://qianfan.baidubce.com/v1/models"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/baidu.json"
)
MANIFEST_STALE_FALLBACK = True

MODEL_MAP = {
    "deepseek-v3.2-intl": "deepseek/deepseek-v3.2",
    "glm-5": "z-ai/glm-5",
    "qianfan-ocr-fast": "baidu/qianfan-ocr-fast",
    "deepseek-v4-pro": "deepseek/deepseek-v4-pro",
    "deepseek-v4-flash": "deepseek/deepseek-v4-flash",
    "glm-5.1": "z-ai/glm-5.1",
    "kimi-k2.6": "moonshotai/kimi-k2.6",
    "glm-5.2": "z-ai/glm-5.2",
    "mimo-v2.5": "xiaomi/mimo-v2.5",
    "deepseek-v4-flash-0731": "deepseek/deepseek-v4-flash-0731",
    "hy3": "tencent/hy3",
}

CATALOG = DirectOpenAIProvider(
    DirectOpenAIProviderSpec(
        slug=SLUG,
        base_url=BASE_URL,
        catalog_url=CATALOG_URL,
        api_key_env="BAIDU_API_KEY",
        explicit_model_map=MODEL_MAP,
        expected_models=(
            "deepseek/deepseek-v4-flash-0731",
            "deepseek/deepseek-v4-pro",
            "z-ai/glm-5.2",
        ),
        canary_max_tokens=64,
        canary_expected_content="PONG",
    ),
    manifest_path=MANIFEST_PATH,
)
UPSTREAM_ID_MAP = CATALOG.upstream_id_map
fetch = CATALOG.fetch
write_provider_manifest = CATALOG.write_provider_manifest
