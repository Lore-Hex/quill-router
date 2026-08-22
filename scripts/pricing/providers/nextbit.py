"""NextBit 256 public priced catalog and keyed route canaries."""

from pathlib import Path

from scripts.pricing.providers._direct_openai import (
    DirectOpenAIProvider,
    DirectOpenAIProviderSpec,
)

SLUG = "nextbit"
BASE_URL = "https://api.nextbit256.com/v1"
URL = f"{BASE_URL}/models"
MANIFEST_PATH = Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/nextbit.json"
EXPLICIT_MODEL_MAP = {
    "alia-salamandra:40b": "bsc-lt/alia-40b-instruct-2601",
    "deepseek:v4-flash-0731": "deepseek/deepseek-v4-flash-0731",
    "euryale:33-70b": "sao10k/l3.3-70b-euryale-v2.3",
    "gemma-2:27b-it": "google/gemma-2-27b-it",
    "gemma4:26b-a4b": "google/gemma-4-26b-a4b-it",
    "mythomax:13b": "gryphe/mythomax-l2-13b",
    "qwen3:14b": "qwen/qwen3-14b",
    "remm-slerp:l2-13b": "undi95/remm-slerp-l2-13b",
    "unslopnemo:12b": "thedrummer/unslopnemo-12b-v4.1",
}
CATALOG = DirectOpenAIProvider(
    DirectOpenAIProviderSpec(
        slug=SLUG,
        base_url=BASE_URL,
        api_key_env="NEXTBIT_API_KEY",
        explicit_model_map=EXPLICIT_MODEL_MAP,
        expected_models=("deepseek/deepseek-v4-flash-0731", "google/gemma-4-26b-a4b-it"),
    ),
    manifest_path=MANIFEST_PATH,
)
UPSTREAM_ID_MAP = CATALOG.upstream_id_map
fetch = CATALOG.fetch
write_provider_manifest = CATALOG.write_provider_manifest
