"""Meta Model API Standard-tier discovery; never opt callers into training."""

from decimal import Decimal
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from scripts.pricing.base import fetch_html, fetch_json
from scripts.pricing.providers._direct_openai import DirectOpenAIProvider, DirectOpenAIProviderSpec

SLUG = "meta-direct"
BASE_URL = "https://api.meta.ai/v1"
URL = f"{BASE_URL}/models"
PRICING_URL = "https://dev.meta.ai/docs/pricing-rate-limits"
MANIFEST_PATH = Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/meta-direct.json"
MANIFEST_STALE_FALLBACK = True


def normalize_catalog(payload: object, html: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise RuntimeError("meta-direct: missing catalog data")
    soup = BeautifulSoup(html, "html.parser")
    heading = next((h for h in soup.find_all(["h2", "h3"]) if h.get_text(" ", strip=True) == "Standard tier"), None)
    if heading is None:
        raise RuntimeError("meta-direct: Standard tier pricing section missing")
    model_ids: set[str] = set()
    rates: dict[str, str] = {}
    for node in heading.next_siblings:
        if getattr(node, "name", None) in {"h2", "h3"}:
            break
        if not hasattr(node, "find_all"):
            continue
        model_ids.update(c.get_text(strip=True) for c in node.find_all("code"))
        tables = [node] if getattr(node, "name", None) == "table" else node.find_all("table")
        for table in tables:
            headers = [cell.get_text(" ", strip=True) for cell in table.find_all("th")]
            if headers != ["Usage", "Price per 1M tokens"]:
                raise RuntimeError("meta-direct: price unit changed")
            for row in table.find_all("tr"):
                cells = row.find_all("td")
                if len(cells) != 2:
                    continue
                label, amount = (c.get_text(" ", strip=True) for c in cells)
                if label in rates or not amount.startswith("$"):
                    raise RuntimeError("meta-direct: duplicate rate or unexpected currency")
                value = Decimal(amount.removeprefix("$"))
                if not value.is_finite() or value <= 0:
                    raise RuntimeError("meta-direct: invalid Standard price")
                rates[label] = str(value / Decimal(1_000_000))
    if not model_ids or set(rates) != {"Input", "Output", "Cached input"}:
        raise RuntimeError("meta-direct: incomplete Standard tier pricing")
    rows = []
    for row in payload["data"]:
        native_id = row.get("id") if isinstance(row, dict) else None
        # Exact membership in the Standard section is intentional. Contributor
        # tiers train on prompts, and non-chat modalities have different meters.
        if native_id not in model_ids or "contributor" in native_id:
            continue
        rows.append({
            "id": native_id, "name": native_id, "context_length": 1_048_576,
            "input_modalities": ["text", "image"], "output_modalities": ["text"],
            "supported_features": ["streaming", "function-calling", "reasoning"],
            "pricing": {"prompt": rates["Input"], "completion": rates["Output"],
                        "input_cache_read": rates["Cached input"]},
        })
    return rows


def load_catalog(api_key: str) -> list[dict[str, Any]]:
    return normalize_catalog(
        fetch_json(URL, extra_headers={"Authorization": f"Bearer {api_key}"}, follow_redirects=False),
        fetch_html(PRICING_URL),
    )


CATALOG = DirectOpenAIProvider(
    DirectOpenAIProviderSpec(
        slug=SLUG, base_url=BASE_URL, api_key_env="META_API_KEY",
        explicit_model_map={}, namespace_unqualified="meta", catalog_url=URL,
        pricing_source_url=PRICING_URL, catalog_loader=load_catalog,
        canary_max_tokens=2048, canary_expected_content="PONG",
        canary_prompt="Reply with exactly PONG and nothing else.",
    ),
    manifest_path=MANIFEST_PATH,
)
UPSTREAM_ID_MAP = CATALOG.upstream_id_map
fetch = CATALOG.fetch
write_provider_manifest = CATALOG.write_provider_manifest
