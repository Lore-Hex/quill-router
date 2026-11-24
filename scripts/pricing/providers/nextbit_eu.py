"""NextBit's separate Spain-only, zero-retention catalog and inference API."""

from pathlib import Path
from typing import Any

from scripts.pricing.providers._direct_openai import DirectOpenAIProvider, DirectOpenAIProviderSpec
from scripts.pricing.providers.nextbit import EXPLICIT_MODEL_MAP

SLUG = "nextbit-eu"
BASE_URL = "https://eu.api.nextbit256.com/v1"
URL = f"{BASE_URL}/models"
MANIFEST_PATH = Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/nextbit-eu.json"
MANIFEST_STALE_FALLBACK = True
# Operator route holds. Discovery, a passing price fetch or a private canary
# never clears one: the shared writer re-applies it at the manifest boundary
# and the held price stays out of the index. To lift a hold, delete the entry
# (and regenerate the manifest) only after NextBit serves the route again and
# a paid-path canary on the operator account succeeds.
_OPERATOR_HOLD_REASONS: dict[str, str] = {
    # 2026-11-22 ~15:34Z onward: every configured-route probe fails with
    # HTTP 429 rate_limit_error (Sentry QUILL-ROUTER-HT). The automatic
    # 24-hour outage hold (provider-route-unserved) expired on 2026-11-24
    # and the route failed again as soon as it was re-admitted. The public
    # provider performance page shows 0% uptime for this route while
    # NextBit EU's other probed routes serve at 100%, so only this route is
    # held. Lift it as described above, once a direct probe returns 200.
    "google/gemma-3-27b-it": "provider-access-unavailable",
}


def validate_eu_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Do not silently publish a changed residency contract or fall back to the
    # global catalog if this endpoint starts advertising different locations.
    for row in rows:
        datacenters = row.get("datacenters")
        if not isinstance(datacenters, list) or not datacenters or any(
            not isinstance(dc, dict) or dc.get("country_code") != "ES" for dc in datacenters
        ):
            raise RuntimeError("nextbit-eu: catalog no longer declares Spain-only inference")
    return rows


CATALOG = DirectOpenAIProvider(
    DirectOpenAIProviderSpec(
        slug=SLUG,
        base_url=BASE_URL,
        api_key_env="NEXTBIT_API_KEY",
        explicit_model_map=EXPLICIT_MODEL_MAP,
        normalize_rows=validate_eu_rows,
        canary_max_tokens=512,
        canary_expected_content="PONG",
        canary_require_usage=True,
        canary_require_message=True,
        canary_prompt="Reply with exactly PONG and nothing else.",
        canary_extra_body={"temperature": 0},
        operator_hold_reasons=_OPERATOR_HOLD_REASONS,
    ),
    manifest_path=MANIFEST_PATH,
)
UPSTREAM_ID_MAP = CATALOG.upstream_id_map
fetch = CATALOG.fetch
write_provider_manifest = CATALOG.write_provider_manifest
