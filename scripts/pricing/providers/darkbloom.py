"""Darkbloom authenticated OpenAI-compatible catalog with embedded prices."""

from pathlib import Path

from scripts.pricing.providers._direct_openai import DirectOpenAIProvider, DirectOpenAIProviderSpec

SLUG = "darkbloom"
BASE_URL = "https://api.darkbloom.dev/v1"
URL = f"{BASE_URL}/models"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/darkbloom.json"
)
MANIFEST_STALE_FALLBACK = True

MODEL_MAP = {
    "gemma-4-26b": "google/gemma-4-26b",
    "qwen3-vl-30b-a3b-instruct": "qwen/qwen3-vl-30b-a3b-instruct",
    "qwen3.5-35b-a3b": "qwen/qwen3.5-35b-a3b",
    "gpt-oss-20b": "openai/gpt-oss-20b",
    "qwen3.6-35b-a3b-vl-mtp-mxfp8": "qwen/qwen3.6-35b-a3b-vl-mtp-mxfp8",
}

CATALOG = DirectOpenAIProvider(
    DirectOpenAIProviderSpec(
        slug=SLUG,
        base_url=BASE_URL,
        api_key_env="DARKBLOOM_API_KEY",
        explicit_model_map=MODEL_MAP,
        expected_models=tuple(MODEL_MAP.values()),
        canary_max_tokens=64,
        canary_expected_content="PONG",
    ),
    manifest_path=MANIFEST_PATH,
)
UPSTREAM_ID_MAP = CATALOG.upstream_id_map
fetch = CATALOG.fetch
write_provider_manifest = CATALOG.write_provider_manifest
