"""Intersect General Compute's authenticated catalog with its official prices."""

import re
from decimal import Decimal
from pathlib import Path
from typing import Any

from scripts.pricing.base import fetch_html, fetch_json
from scripts.pricing.providers._direct_openai import DirectOpenAIProvider, DirectOpenAIProviderSpec

SLUG = "general-compute"
BASE_URL = "https://api.generalcompute.com/v1"
URL = f"{BASE_URL}/models"
PRICING_URL = "https://docs.generalcompute.com/models.md"
MANIFEST_PATH = Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/general-compute.json"
MANIFEST_STALE_FALLBACK = True
MODEL_MAP = {
    "gpt-oss-120b": "openai/gpt-oss-120b",
    "deepseek-v3.1": "deepseek/deepseek-v3.1",
    "deepseek-v3.2": "deepseek/deepseek-v3.2",
    "minimax-m2.7": "minimax/minimax-m2.7",
    "gemma-4-31B-it": "google/gemma-4-31b-it",
}


def normalize_catalog(payload: object, markdown: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise RuntimeError("general-compute: missing catalog data")
    available = {r.get("id") for r in payload["data"] if isinstance(r, dict)}
    header = ["Model", "Model ID", "Context", "Input / 1M tokens", "Output / 1M tokens", "Capabilities"]
    if not any([c.strip() for c in line.strip().strip("|").split("|")] == header for line in markdown.splitlines()):
        raise RuntimeError("general-compute: price unit or table schema changed")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in markdown.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 6 or not re.fullmatch(r"`[^`]+`", cells[1]):
            continue
        native_id = cells[1][1:-1]
        if native_id not in available:
            continue
        if native_id in seen:
            raise RuntimeError("general-compute: duplicate price row")
        seen.add(native_id)
        match = re.fullmatch(r"(\d+)(k)?", cells[2], re.I)
        if not match:
            raise RuntimeError("general-compute: missing context limit")
        context = int(match[1]) * (1000 if match[2] else 1)
        if any(not re.fullmatch(r"\\?\$\d+(?:\.\d+)?", c) for c in cells[3:5]):
            raise RuntimeError("general-compute: invalid USD price")
        prices = [Decimal(c.replace("\\$", "").removeprefix("$")) for c in cells[3:5]]
        if context <= 0 or any(not v.is_finite() or v <= 0 for v in prices):
            raise RuntimeError("general-compute: invalid price or context")
        rows.append({
            "id": native_id, "name": cells[0].replace("**", ""), "context_length": context,
            "input_modalities": ["text"], "output_modalities": ["text"],
            "supported_features": ["streaming"] + (["reasoning"] if "Reasoning" in cells[5] else []),
            "pricing": {"prompt": str(prices[0] / Decimal(1_000_000)),
                        "completion": str(prices[1] / Decimal(1_000_000))},
        })
    if not rows:
        raise RuntimeError("general-compute: no live models with exact official prices")
    if "gemma-4-31B-it" in available - seen:
        # Advertised in /models but absent from the official price table.
        rows.append({"id": "gemma-4-31B-it", "name": "Gemma 4 31B"})
    return rows


def load_catalog(api_key: str) -> list[dict[str, Any]]:
    return normalize_catalog(
        fetch_json(URL, extra_headers={"Authorization": f"Bearer {api_key}"}, follow_redirects=False),
        fetch_html(PRICING_URL),
    )


CATALOG = DirectOpenAIProvider(
    DirectOpenAIProviderSpec(
        slug=SLUG, base_url=BASE_URL, api_key_env="GENERAL_COMPUTE_KEY",
        explicit_model_map=MODEL_MAP, namespace_unqualified=SLUG, catalog_url=URL,
        pricing_source_url=PRICING_URL, catalog_loader=load_catalog,
        reviewed_unpriced_model_ids=frozenset({"google/gemma-4-31b-it"}),
        canary_max_tokens=1024, canary_expected_content="PONG",
        canary_prompt="Reply with exactly PONG and nothing else.",
    ),
    manifest_path=MANIFEST_PATH,
)
UPSTREAM_ID_MAP = CATALOG.upstream_id_map
fetch = CATALOG.fetch
write_provider_manifest = CATALOG.write_provider_manifest
