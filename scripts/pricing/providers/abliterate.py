"""Abliterate's own catalog and published USD/M token prices.

This is abliterate.ai, not the unrelated abliteration.ai. Paid routes stay
held until its API reports usage, including internal reasoning/tool calls.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from scripts.pricing.base import ModelPrice, fetch_html
from scripts.pricing.providers._direct_openai import DirectOpenAIProvider, DirectOpenAIProviderSpec

SLUG = "abliterate"
BASE_URL = "https://abliterate.ai/api/v1"
URL = f"{BASE_URL}/models"
PRICING_URL = "https://abliterate.ai/docs"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src/trusted_router/data/provider_models/abliterate.json"
)
MANIFEST_STALE_FALLBACK = True
EXPLICIT_MODEL_MAP = {
    name: f"abliterate/{name}"
    for name in (
        "abliterate-0.3-fast",
        "abliterate-0.3-balanced",
        "abliterate-0.3-clever",
        "abliterated-research-0.1",
    )
}
HOLD_REASON = "upstream-usage-unavailable"


def _asset_url(reference: str) -> str:
    url = urljoin(PRICING_URL, reference)
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "abliterate.ai"
        or not parsed.path.startswith("/assets/")
        or not parsed.path.endswith(".js")
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("abliterate: unexpected public documentation asset")
    return url


def _parse_prices(source: str) -> dict[str, ModelPrice]:
    # The provider publishes prices in its React documentation cards, not
    # /models. Read data only; never execute remote JavaScript.
    cards = re.findall(r'\{name:"([^"\\]+)",[^{}]*\}', source)
    prices: dict[str, ModelPrice] = {}
    for name in cards:
        if name not in EXPLICIT_MODEL_MAP:
            continue
        matches = re.findall(
            r'\{name:"' + re.escape(name)
            + r'",[^{}]*?input_per_m:"\$([0-9]+(?:\.[0-9]+)?)",'
            r'output_per_m:"\$([0-9]+(?:\.[0-9]+)?)"\}',
            source,
        )
        if len(matches) != 1 or name in {key.removeprefix("abliterate/") for key in prices}:
            raise RuntimeError(f"abliterate: ambiguous or malformed price for {name}")
        prompt, completion = (int(Decimal(value) * 1_000_000) for value in matches[0])
        if prompt <= 0 or completion <= 0:
            raise RuntimeError(f"abliterate: nonpositive price for {name}")
        prices[EXPLICIT_MODEL_MAP[name]] = ModelPrice(prompt, completion)
    if set(prices) != set(EXPLICIT_MODEL_MAP.values()):
        raise RuntimeError("abliterate: incomplete documentation pricing table")
    return prices


def _load_prices() -> dict[str, ModelPrice]:
    soup = BeautifulSoup(fetch_html(PRICING_URL), "html.parser")
    scripts = [
        str(node["src"])
        for node in soup.select('script[type="module"][src]')
    ]
    if len(scripts) != 1:
        raise RuntimeError("abliterate: ambiguous documentation entry asset")
    entry_url = _asset_url(scripts[0])
    entry = fetch_html(entry_url)
    chunks = set(re.findall(r'\./DocsView-[A-Za-z0-9_-]+\.js', entry))
    if len(chunks) != 1:
        raise RuntimeError("abliterate: documentation pricing asset not found")
    return _parse_prices(fetch_html(_asset_url(urljoin(entry_url, chunks.pop()))))


CATALOG = DirectOpenAIProvider(
    DirectOpenAIProviderSpec(
        slug=SLUG,
        base_url=BASE_URL,
        api_key_env="ABLITERATE_API_KEY",
        explicit_model_map=EXPLICIT_MODEL_MAP,
        expected_models=tuple(EXPLICIT_MODEL_MAP.values()),
        price_loader=_load_prices,
        pricing_source_url=PRICING_URL,
        operator_hold_reasons=dict.fromkeys(EXPLICIT_MODEL_MAP.values(), HOLD_REASON),
    ),
    manifest_path=MANIFEST_PATH,
)
UPSTREAM_ID_MAP = CATALOG.upstream_id_map
fetch = CATALOG.fetch
write_provider_manifest = CATALOG.write_provider_manifest
