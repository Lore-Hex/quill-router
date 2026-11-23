"""Key-scoped Nito chat prices. Discovery is held and not enrolled in live refresh."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from scripts.pricing.base import ModelPrice, ProviderPricingResult, fetch_json
from scripts.pricing.manifest import write_discovered_chat_manifest

SLUG = "nito"
URL = "https://app.trynito.ai/api/v1/models"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/nito.json"
)
HOLD = "gateway-and-inference-verification-pending"
_rows: dict[str, dict[str, Any]] = {}
UPSTREAM_ID_MAP: dict[str, str] = {}


def discover(payload: object) -> tuple[dict[str, ModelPrice], dict[str, dict[str, Any]]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("nito: invalid catalog")
    prices: dict[str, ModelPrice] = {}
    rows: dict[str, dict[str, Any]] = {}
    for row in payload["data"]:
        if not isinstance(row, dict):
            raise ValueError("nito: invalid model row")
        if row.get("type") != "chat" or row.get("privacy_tier") != "confidential":
            continue
        # This is the ordinary HTTPS API, not Nito's client-encrypted protocol.
        # The native suffix selects their backend; it is NOT a TR privacy label.
        native = row.get("id")
        if not isinstance(native, str) or not native.endswith(":confidential"):
            raise ValueError("nito: mismatched model tier")
        model = native.removesuffix(":confidential")
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", model, re.ASCII) or model in rows:
            raise ValueError("nito: invalid or duplicate model identity")
        endpoints = row.get("supported_endpoints")
        if not isinstance(endpoints, list) or not all(isinstance(e, str) for e in endpoints):
            raise ValueError("nito: invalid supported endpoints")
        if "/v1/chat/completions" not in endpoints:
            continue
        p = row.get("pricing")
        if not isinstance(p, dict) or (p.get("currency_code"), p.get("unit")) != ("USD", "nanos"):
            raise ValueError("nito: invalid price units")
        inp, out, cache, reasoning = (
            p.get(k)
            for k in (
                "input_token_nanos",
                "output_token_nanos",
                "cache_read_input_token_nanos",
                "reasoning_output_token_nanos",
            )
        )
        if type(inp) is not int or type(out) is not int or min(inp, out) <= 0:
            raise ValueError("nito: invalid token rates")
        if cache is not None and (type(cache) is not int or not 0 <= cache <= inp):
            raise ValueError("nito: invalid cache rate")
        if reasoning is not None and (type(reasoning) is not int or reasoning != out):
            raise ValueError("nito: separate reasoning pricing is unsupported")
        prices[model] = ModelPrice(
            inp * 1000,
            out * 1000,
            prompt_cached_micro_per_m=None if cache is None else cache * 1000,
        )
        rows[model] = {
            "upstream_id": native,
            "model_id": model,
            "routable": False,
            "routable_reason": HOLD,
            "supported_features": [],
        }
    if not prices:
        raise ValueError("nito: no priced ordinary chat routes")
    return prices, rows


def fetch() -> ProviderPricingResult:
    _rows.clear()
    UPSTREAM_ID_MAP.clear()
    key = os.environ.get("NITO_API_KEY", "").strip()
    if not key:
        raise ValueError("nito: missing discovery credential")
    prices, rows = discover(
        fetch_json(URL, extra_headers={"Authorization": f"Bearer {key}"}, follow_redirects=False)
    )
    _rows.update(rows)
    UPSTREAM_ID_MAP.update({model: row["upstream_id"] for model, row in rows.items()})
    return ProviderPricingResult(
        slug=SLUG, prices=prices, source="api", fetched_url=URL, include_in_price_index=False
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    if not _rows or _rows.keys() != result.prices.keys():
        raise ValueError("nito: fresh matching catalog required")
    return write_discovered_chat_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        discovered_rows=_rows,
        source_url=URL,
        operator_hold_reasons=dict.fromkeys(_rows, HOLD),
    )
