"""Infomaniak account-scoped chat, joined to current CHF prices and ECB FX."""

import re
from decimal import Decimal
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from defusedxml.ElementTree import fromstring

from scripts.pricing.base import fetch_html, fetch_json
from scripts.pricing.providers._direct_openai import DirectOpenAIProvider, DirectOpenAIProviderSpec
from scripts.pricing.providers.regolo import ECB_FX_URL, usd_per_eur

SLUG = "infomaniak"
# AI product IDs are routing coordinates, not credentials. Never accept a
# caller-supplied host/product URL in the enclave's provider transport.
BASE_URL = "https://api.infomaniak.com/2/ai/111565/openai/v1"
URL = "https://api.infomaniak.com/1/ai/models"
PRICING_URL = "https://www.infomaniak.com/en/hosting/ai-services/prices"
MANIFEST_PATH = Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/infomaniak.json"
MANIFEST_STALE_FALLBACK = True


def usd_per_chf(xml: str) -> Decimal:
    usd = usd_per_eur(xml)  # Shares the seven-day age and positive-rate gate.
    for node in fromstring(xml).iter():
        if node.attrib.get("currency") == "CHF":
            chf = Decimal(node.attrib["rate"])
            if chf.is_finite() and chf > 0:
                return usd / chf
    raise RuntimeError("infomaniak: ECB feed has no valid CHF rate")


def normalize_catalog(payload: object, html: str, rate: Decimal) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise RuntimeError("infomaniak: missing catalog data")
    if not rate.is_finite() or rate <= 0:
        raise RuntimeError("infomaniak: invalid CHF conversion")
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for row in payload["data"]:
        if not isinstance(row, dict) or row.get("type") != "llm" or row.get("info_status") != "ready":
            continue
        native_id = row.get("name")
        if not isinstance(native_id, str):
            continue
        title = soup.find("p", string=native_id)
        if title is None or title.parent is None or title.parent.parent is None:
            raise RuntimeError("infomaniak: ready model has no published price")
        text = title.parent.parent.get_text(" ", strip=True)
        prices = []
        for label in ("Input", "Output"):
            match = re.search(rf"{label} token:\s*CHF\s*(\d+(?:\.\d+)?)\s*/\s*1M tokens", text)
            if not match:
                raise RuntimeError("infomaniak: price currency or unit changed")
            prices.append(Decimal(match[1]) * rate / Decimal(1_000_000))
        context = row.get("max_token_input")
        if not isinstance(context, int) or isinstance(context, bool) or context <= 0:
            raise RuntimeError("infomaniak: missing context limit")
        rows.append({
            "id": native_id, "name": native_id, "context_length": context,
            "input_modalities": ["text"], "output_modalities": ["text"],
            "supported_features": ["streaming"],
            "pricing": {"prompt": str(prices[0]), "completion": str(prices[1])},
        })
    return rows


def load_catalog(api_key: str) -> list[dict[str, Any]]:
    return normalize_catalog(
        fetch_json(URL, extra_headers={"Authorization": f"Bearer {api_key}"}, follow_redirects=False),
        fetch_html(PRICING_URL), usd_per_chf(fetch_html(ECB_FX_URL)),
    )


CATALOG = DirectOpenAIProvider(
    DirectOpenAIProviderSpec(
        slug=SLUG, base_url=BASE_URL, api_key_env="INFOMANIAK_API_KEY",
        explicit_model_map={}, catalog_url=URL, pricing_source_url=PRICING_URL,
        catalog_loader=load_catalog, canary_max_tokens=512, canary_expected_content="PONG",
        canary_prompt="Reply with exactly PONG and nothing else.",
    ),
    manifest_path=MANIFEST_PATH,
)
UPSTREAM_ID_MAP = CATALOG.upstream_id_map
fetch = CATALOG.fetch
write_provider_manifest = CATALOG.write_provider_manifest
