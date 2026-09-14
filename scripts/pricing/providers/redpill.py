"""RedPill's provider-owned catalog; never infer privacy from its is_tee flag."""

import re
from pathlib import Path

from scripts.pricing.providers._direct_openai import DirectOpenAIProvider, DirectOpenAIProviderSpec

SLUG = "redpill"
MANIFEST_STALE_FALLBACK = True
BASE_URL = "https://api.redpill.ai/v1"
URL = f"{BASE_URL}/models"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src/trusted_router/data/provider_models/redpill.json"
)

def _token_field(model_id: str) -> str:
    match = re.match(r"openai/(?:gpt-(\d+)|o(?:1|3|4)(?:-|$))", model_id.casefold())
    if match and (match[1] is None or int(match[1]) >= 5):
        return "max_completion_tokens"
    return "max_tokens"


CATALOG = DirectOpenAIProvider(
    DirectOpenAIProviderSpec(
        slug=SLUG,
        base_url=BASE_URL,
        api_key_env="REDPILL_API_KEY",
        explicit_model_map={},
        canary_max_tokens=128,
        canary_max_tokens_field=_token_field,
        canary_require_message=True,
    ),
    manifest_path=MANIFEST_PATH,
)
UPSTREAM_ID_MAP = CATALOG.upstream_id_map
fetch = CATALOG.fetch
write_provider_manifest = CATALOG.write_provider_manifest
