"""Xiaomi MiMo first-party pricing refresh."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup, Tag

from scripts.pricing.base import ProviderPricingResult, fetch_html, fetch_provider
from trusted_router.provider_lifecycle import (
    XIAOMI_MIMO_V25_PRO_ULTRASPEED_RETIREMENT_AT,
)

SLUG = "xiaomi"
PUBLIC_PRICING_URL = "https://mimo.mi.com/docs/en-US/price/pay-as-you-go"
URL = PUBLIC_PRICING_URL
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "xiaomi.json"
)

EXPECTED_MODELS = [
    "xiaomi/mimo-v2.5",
    "xiaomi/mimo-v2.5-pro",
]
_ULTRASPEED_MODEL_ID = "xiaomi/mimo-v2.5-pro-ultraspeed"


def _spec_value(section: Tag, label: str) -> str:
    labels = section.find_all("span", string=label)
    if len(labels) != 1 or labels[0].parent is None:
        raise ValueError(f"xiaomi: missing or ambiguous {label}")
    cells = labels[0].parent.find_all("span", recursive=False)
    if len(cells) != 2 or cells[0] != labels[0]:
        raise ValueError(f"xiaomi: unexpected {label} layout")
    return cells[1].get_text(" ", strip=True)


def _token_limit(value: str) -> int:
    match = re.fullmatch(r"([1-9][0-9]*)([KM]?) tokens", value)
    if match is None:
        raise ValueError("xiaomi: unrecognized token limit")
    return int(match[1]) * {"": 1, "K": 1024, "M": 1024 * 1024}[match[2]]


def _new_chat_model(model_id: str, *, created: int) -> dict[str, Any]:
    # Only fetch same-origin model cards named by the official USD price table.
    # Never copy another model's context/capabilities for an unseen release.
    if not re.fullmatch(r"xiaomi/mimo-v[0-9]+(?:\.[0-9]+)?(?:-[a-z0-9]+)*", model_id):
        raise ValueError("xiaomi: invalid priced model id")
    upstream_id = model_id.removeprefix("xiaomi/")
    source_url = f"https://mimo.mi.com/models/en-US/{upstream_id}"
    soup = BeautifulSoup(fetch_html(source_url), "html.parser")
    titles = soup.find_all("h1")
    if len(titles) != 1 or titles[0].get_text(strip=True).casefold() != upstream_id:
        raise ValueError(f"xiaomi: model card identity mismatch for {model_id}")
    headings = soup.find_all("h2", string="Model Specs")
    section = headings[0].find_parent("section") if len(headings) == 1 else None
    if section is None:
        raise ValueError(f"xiaomi: missing model specs for {model_id}")
    # Do not infer vision/audio support from marketing claims such as Omni-Modal.
    if _spec_value(section, "Input Modality") != "Text" or (
        _spec_value(section, "Output Modality") != "Text"
    ):
        raise ValueError(f"xiaomi: unreviewed chat modalities for {model_id}")
    context = _token_limit(_spec_value(section, "Context Window"))
    output = _token_limit(_spec_value(section, "Max Output"))
    if output > context:
        raise ValueError(f"xiaomi: output limit exceeds context for {model_id}")
    capability_labels = section.find_all("p", string="Capabilities")
    if len(capability_labels) != 1 or capability_labels[0].parent is None:
        raise ValueError(f"xiaomi: missing capabilities for {model_id}")
    capabilities = {
        span.get_text(strip=True)
        for span in capability_labels[0].parent.find_all("span")
    }
    feature_labels = {
        "Tool Call": "function-calling",
        "Deep Thinking": "reasoning",
        "Structured Output": "structured-output",
    }
    title = titles[0].get_text(strip=True)
    return {
        "id": model_id,
        "upstream_id": upstream_id,
        "display_name": f"Xiaomi {title.replace('-', ' ')}",
        "title": title,
        "created": created,
        "context_length": context,
        "max_output_tokens": output,
        "model_type": "chat",
        "endpoints": ["chat/completions"],
        "features": ["serverless", *[
            feature for label, feature in feature_labels.items() if label in capabilities
        ]],
        "input_modalities": ["text"],
        "output_modalities": ["text"],
        "metadata_source": source_url,
    }


def fetch() -> ProviderPricingResult:
    return fetch_provider(
        slug=SLUG,
        url=URL,
        expected_models=EXPECTED_MODELS,
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    rows = raw.get("models")
    if not isinstance(rows, list):
        raise RuntimeError("xiaomi manifest has no models list")

    now = datetime.now(UTC).replace(microsecond=0)
    known = {row.get("id") for row in rows if isinstance(row, dict)}
    for model_id in sorted(result.prices.keys() - known):
        rows.append(_new_chat_model(model_id, created=int(now.timestamp())))

    updated: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        model_id = row.get("id")
        if not isinstance(model_id, str):
            continue
        if model_id == _ULTRASPEED_MODEL_ID:
            row["retirement_at"] = (
                XIAOMI_MIMO_V25_PRO_ULTRASPEED_RETIREMENT_AT.astimezone(UTC)
                .isoformat()
                .replace("+00:00", "Z")
            )
        price = result.prices.get(model_id)
        if price is None:
            continue
        if len(price.tiers) != 1 or price.tiers[0].max_prompt_tokens is not None:
            raise ValueError(f"xiaomi: unreviewed tiered price for {model_id}")
        tier = price.tiers[0]
        row["input_token_price_per_m"] = tier.prompt_micro_per_m
        row["output_token_price_per_m"] = tier.completion_micro_per_m
        if tier.prompt_cached_micro_per_m is not None:
            row["cached_input_token_price_per_m"] = tier.prompt_cached_micro_per_m
        else:
            row.pop("cached_input_token_price_per_m", None)
        updated.append(model_id)

    missing = sorted(set(EXPECTED_MODELS) - set(updated))
    if missing:
        raise RuntimeError(f"xiaomi manifest did not update expected model(s): {missing}")

    raw["source"] = PUBLIC_PRICING_URL
    raw["_note"] = (
        "Xiaomi MiMo provider-native routes. Real-time PAYG prices and cached "
        "input prices are refreshed hourly from Xiaomi's official overseas USD "
        "table. Newly priced chat models require an identity-matching official "
        "model-spec page before publication. Legacy rows and provider-scoped "
        "retirement metadata are preserved; new releases do not retarget old IDs."
    )
    raw["generated_at"] = now.isoformat().replace("+00:00", "Z")
    raw["model_count"] = len(rows)
    MANIFEST_PATH.write_text(
        json.dumps(raw, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return [f"xiaomi: refreshed provider_models/xiaomi.json ({len(updated)} priced rows)"]
