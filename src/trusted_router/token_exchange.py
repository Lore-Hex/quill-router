"""Public exchange evidence. Prices come from the same catalog as /models.

No persisted snapshots or last-good fallback: unavailable sources stay absent.
The consumer must also age checks while a page remains open.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from trusted_router.catalog import (
    endpoint_confidential_compute,
    endpoint_e2ee,
    endpoints_for_model,
)

MODELS = (
    ("z-ai/glm-5.3-flash", "GLM 5.3 Flash"),
    ("deepseek/deepseek-v4.1-flash", "DeepSeek V4.1 Flash"),
    ("openai/gpt-oss-120b", "GPT OSS 120B"),
)
PROFILES = {
    "shared-gcp": ("https://trustedrouter.com", None),
    "new-york": ("https://trustedrouter.com", "us_east4_regional_api"),
    "europe": ("https://trustedrouter.com", "eu_regional_api"),
    "dubai": ("https://azure.trustedrouter.com", "uaenorth_gateway"),
}


def confidential_prices() -> list[dict[str, Any]]:
    rows = []
    for model, label in MODELS:
        routes = [e for e in endpoints_for_model(model)
                  if e.provider == "tinfoil" and e.usage_type == "Credits"
                  and endpoint_confidential_compute(e) and endpoint_e2ee(e)]
        # Ambiguity is not permission to average or choose a cheaper route.
        if len(routes) != 1:
            continue
        route = routes[0]
        prices = (route.prompt_price_microdollars_per_million_tokens,
                  route.completion_price_microdollars_per_million_tokens)
        if any(value < 0 for value in prices):
            continue
        rows.append(dict(model=model, label=label, provider="tinfoil", endpoint_id=route.id,
                         input=format(Decimal(prices[0]) / 1_000_000, "f"),
                         output=format(Decimal(prices[1]) / 1_000_000, "f"),
                         source=f"https://trustedrouter.com/models/{model}#provider-tinfoil"))
    return rows


def _get(client: httpx.Client, url: str) -> dict[str, Any] | None:
    try:
        response = client.get(url)
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else None
    except (httpx.HTTPError, ValueError):
        return None


def exchange_evidence(profile: str) -> dict[str, Any]:
    origin, regional = PROFILES[profile]
    release_url = ("https://trust.trustedrouter.com/trust/azure-release.json"
                   if profile == "dubai" else
                   "https://trustedrouter.com/trust/gcp-release.json")
    with httpx.Client(timeout=5, follow_redirects=False) as client:
        status = _get(client, origin + "/status.json")
        release = _get(client, release_url)
    data = status.get("data", {}) if status else {}
    if not isinstance(data, dict):
        data = {}
    wanted = {"canonical_api", "model_inference"}
    if regional:
        wanted.add(regional)
    components = data.get("components", [])
    if not isinstance(components, list):
        components = []
    components = [c for c in components if isinstance(c, dict)]
    if release and release.get("release_metadata_status") in {"embedded", "stale", "unavailable"}:
        release = None
    return dict(
        generated_at=datetime.now(UTC).isoformat(),
        refresh_seconds=300,
        # Core probes run every three minutes (scripts/deploy/synthetic.sh).
        stale_after_seconds=360,
        prices=confidential_prices(),
        status_source=origin + "/status",
        components=[c for c in components if c.get("id") in wanted],
        attestation_check=next((c for c in components if c.get("id") == "attestation"), None),
        release=release,
        release_source=release_url,
    )
