"""Tencent TokenHub Singapore catalog, joined to its regional USD prices."""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup, Tag

from scripts.pricing.base import ModelPrice, PriceTier, ProviderPricingResult, fetch_html
from scripts.pricing.model_ids import canonicalize_unqualified_model_id
from scripts.pricing.providers._direct_openai import DirectOpenAIProvider, DirectOpenAIProviderSpec
from trusted_router.provider_lifecycle import TENCENT_OFF_PEAK_PRICES

SLUG = "tencent"
BASE_URL = "https://tokenhub-intl.tencentcloudmaas.com/v1"
URL = f"{BASE_URL}/models"
# The international documentation mirror serves the same first-party tables
# without the main site's JavaScript challenge. Region tabs must stay separate.
PRICING_URL = "https://intl.cloud.tencent.com/document/product/1300/78937"
MODELS_DOC_URL = "https://intl.cloud.tencent.com/document/product/1300/78934"
MANIFEST_PATH = Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/tencent.json"
MANIFEST_STALE_FALLBACK = True
_METADATA: dict[str, dict[str, Any]] = {}
_MODEL_NAMES: dict[str, str] = {}
_EXPLICIT_MAP: dict[str, str] = {}
_SCHEDULED_NAMES = {"deepseek-v4.1-flash", "deepseek-v4-flash 0731 ga", "deepseek-v4-pro 0813 ga"}


def _name(value: str) -> str:
    return " ".join(value.casefold().split())


def canonical_model_id(native_id: str) -> str | None:
    if native_id.startswith("hy"):
        return f"tencent/{native_id.lower()}"
    if native_id.startswith("step-"):
        return f"stepfun/{native_id.lower()}"
    return canonicalize_unqualified_model_id(native_id)


def _rows(table: Tag) -> list[list[str]]:
    """Expand real HTML rowspans, including Tencent's zero-span placeholders."""
    result: list[list[str]] = []
    pending: dict[int, tuple[str, int]] = {}
    for tr in table.find_all("tr"):
        values: list[str] = []
        cells = iter(tr.find_all(["th", "td"], recursive=False))
        cell = next(cells, None)
        while cell is not None or len(values) in pending:
            col = len(values)
            if col in pending:
                value, remaining = pending.pop(col)
                values.append(value)
                if remaining > 1:
                    pending[col] = (value, remaining - 1)
                if cell is not None and cell.get("rowspan") == "0":
                    cell = next(cells, None)
                continue
            assert cell is not None
            if cell.get("rowspan") == "0" or int(str(cell.get("colspan", 1))) != 1:
                raise ValueError("tencent: ambiguous price table span")
            value = cell.get_text(" ", strip=True).replace("\ufeff", "").strip()
            span = int(str(cell.get("rowspan", 1)))
            if span > 1:
                pending[col] = (value, span - 1)
            values.append(value)
            cell = next(cells, None)
        result.append(values)
    if pending:
        raise ValueError("tencent: incomplete table rowspan")
    return result


def _tokens(value: str) -> int | None:
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([km]?)", value.lower())
    if match is None:
        return None
    return int(Decimal(match[1]) * {"": 1, "k": 1024, "m": 1024 * 1024}[match[2]])


def parse_models(html: str) -> dict[str, dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        rows = _rows(table)
        if not rows or len(rows[0]) != 6 or "Supported Capabilities" not in rows[0]:
            continue
        metadata: dict[str, dict[str, Any]] = {}
        for cells in rows[1:]:
            if len(cells) != 6:
                raise ValueError("tencent: malformed model capabilities row")
            name, native_ids, capabilities, context, _max_input, max_output = cells
            # Vendor-direct aliases can have different prices and weights from
            # Tencent hosting. Do not collapse them into one canonical route.
            if "Vendor Direct" in name or "discontinued" in name.lower():
                continue
            features = [feature for label, feature in (
                ("Function Calling", "function-calling"),
                ("Structured Output", "json-mode"),
                ("Deep Reasoning", "reasoning-effort"),
                ("Caching", "prompt-cache"),
            ) if label in capabilities]
            for native_id in native_ids.split():
                if canonical_model_id(native_id) is None:
                    continue
                row: dict[str, Any] = {
                    "price_name": _name(name), "supported_features": features,
                    "context_length": _tokens(context), "max_output_tokens": _tokens(max_output),
                    "input_modalities": ["text"], "output_modalities": ["text"],
                }
                if "Image" in capabilities or "Multimodal Understanding" in capabilities:
                    row["input_modalities"].append("image")
                metadata[native_id] = row
        if not metadata:
            raise ValueError("tencent: empty language model table")
        return metadata
    raise ValueError("tencent: missing language model capabilities table")


def _singapore_table(html: str) -> Tag:
    soup = BeautifulSoup(html, "html.parser")
    for tabs in soup.select(".tse-tabs"):
        labels = [label.get_text(" ", strip=True) for label in tabs.select(".tse-tabs__item-label")]
        panels = tabs.select(":scope > .tse-tabs__cont")
        if "Singapore" not in labels or len(labels) != len(panels):
            continue
        table = panels[labels.index("Singapore")].find("table")
        if table is not None and "Peak/Off-Peak Billing" in table.get_text():
            return table
    raise ValueError("tencent: missing Singapore language-model USD price table")


def parse_prices(html: str) -> dict[str, ModelPrice]:
    rows = _rows(_singapore_table(html))
    if len(rows[0]) != 6 or any("USD / million tokens" not in c for c in rows[0][3:]):
        raise ValueError("tencent: unexpected currency or price units")
    grouped: dict[str, list[tuple[str, str, PriceTier]]] = {}
    for cells in rows[1:]:
        if len(cells) != 6:
            raise ValueError("tencent: malformed pricing row")
        name, condition, period, prompt, completion, cache = cells
        values = [int(Decimal(value) * 1_000_000) for value in (prompt, completion)]
        cached = None if cache == "-" else int(Decimal(cache) * 1_000_000)
        if min(values) <= 0 or cached is not None and not 0 <= cached <= values[0]:
            raise ValueError("tencent: invalid token price")
        grouped.setdefault(_name(name), []).append((condition, period, PriceTier(None, *values, cached)))
    prices: dict[str, ModelPrice] = {}
    for name, entries in grouped.items():
        if "vendor direct" in name:
            continue
        if name in _SCHEDULED_NAMES and [e[1] for e in entries] != ["OFF-PEAK", "PEAK"]:
            raise ValueError("tencent: scheduled pricing format changed; review runtime billing")
        if len(entries) == 1 and entries[0][:2] == ("-", "-"):
            prices[name] = ModelPrice(tiers=[entries[0][2]])
        elif [e[1] for e in entries] == ["OFF-PEAK", "PEAK"] and all(e[0] == "-" for e in entries):
            if name not in _SCHEDULED_NAMES:
                raise ValueError("tencent: new scheduled model requires runtime billing support")
            low, high = (entry[2] for entry in entries)
            if (high.prompt_micro_per_m, high.completion_micro_per_m, high.prompt_cached_micro_per_m) != (
                low.prompt_micro_per_m * 2, low.completion_micro_per_m * 2,
                None if low.prompt_cached_micro_per_m is None else low.prompt_cached_micro_per_m * 2,
            ):
                raise ValueError("tencent: peak multiplier changed; review billing schedule")
            prices[name] = ModelPrice(tiers=[low])
        elif [e[0] for e in entries] == ["Input length (0, 512k]", "Input length 512k+"] and all(e[1] == "-" for e in entries):
            low, high = (entry[2] for entry in entries)
            prices[name] = ModelPrice(tiers=[PriceTier(512 * 1024, low.prompt_micro_per_m, low.completion_micro_per_m, low.prompt_cached_micro_per_m), high])
        else:
            raise ValueError(f"tencent: unsupported pricing condition for {name}")
    return prices


def _normalize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    _METADATA.clear()
    _MODEL_NAMES.clear()
    _EXPLICIT_MAP.clear()
    documented = parse_models(fetch_html(MODELS_DOC_URL))
    normalized: list[dict[str, Any]] = []
    for source in rows:
        native_id = str(source.get("id", ""))
        model_id = canonical_model_id(native_id)
        metadata = documented.get(native_id)
        if source.get("status") != "online" or model_id is None or metadata is None:
            continue
        _EXPLICIT_MAP[native_id] = model_id
        _MODEL_NAMES[model_id] = metadata["price_name"]
        _METADATA[model_id] = {key: value for key, value in metadata.items() if key != "price_name" and value is not None}
        normalized.append({**source, **_METADATA[model_id]})
    return normalized


def _load_prices() -> dict[str, ModelPrice]:
    prices = parse_prices(fetch_html(PRICING_URL))
    result = {model_id: prices[name] for model_id, name in _MODEL_NAMES.items() if name in prices}
    for model_id, expected in TENCENT_OFF_PEAK_PRICES.items():
        price = result.get(model_id)
        if price is None:
            continue
        tier = price.tiers[0]
        if (tier.prompt_micro_per_m, tier.completion_micro_per_m, tier.prompt_cached_micro_per_m) != (
            expected.prompt_microdollars_per_million_tokens,
            expected.completion_microdollars_per_million_tokens,
            expected.prompt_cached_microdollars_per_million_tokens,
        ):
            raise ValueError(f"tencent: scheduled price changed for {model_id}; review runtime billing")
    return result


CATALOG = DirectOpenAIProvider(
    DirectOpenAIProviderSpec(
        slug=SLUG, base_url=BASE_URL, api_key_env="TENCENT_API_KEY",
        explicit_model_map=_EXPLICIT_MAP, normalize_rows=_normalize_rows,
        price_loader=_load_prices, pricing_source_url=PRICING_URL,
        # Published combo leaves have a pinned provider set in catalog_registry.
        operator_hold_reasons={"deepseek/deepseek-v4-pro-0813": "immutable-release-route-set"},
        canary_max_tokens=256, canary_expected_content="PONG",
        canary_prompt="Reply exactly PONG",
        expected_models=("tencent/hy3", "z-ai/glm-5.3-flash"),
    ), manifest_path=MANIFEST_PATH,
)
UPSTREAM_ID_MAP = CATALOG.upstream_id_map


def fetch() -> ProviderPricingResult:
    result = CATALOG.fetch()
    for model_id, row in CATALOG.discovered_rows.items():
        row.update(_METADATA.get(model_id, {}))
    return result


write_provider_manifest = CATALOG.write_provider_manifest
