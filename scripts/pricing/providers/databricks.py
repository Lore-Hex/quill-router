"""Databricks Foundation Model API catalog, pricing, and account canary."""

from __future__ import annotations

import os
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from scripts.pricing.base import (
    PROVIDER_FETCH_TIMEOUT,
    PROVIDER_FETCH_TRANSPORT_RETRIES,
    PROVIDER_FETCH_UA,
    ModelPrice,
    ProviderPricingResult,
    validate,
)
from scripts.pricing.manifest import (
    set_manifest_model_canary_states,
    write_discovered_chat_manifest,
)
from scripts.pricing.openai_catalog import probe_openai_chat

SLUG = "databricks"
PRICING_URL = "https://www.databricks.com/product/pricing/foundation-model-serving"
SUPPORTED_MODELS_URL = (
    "https://docs.databricks.com/aws/en/machine-learning/"
    "foundation-model-apis/supported-models"
)
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "databricks.json"
)

# Databricks publishes Foundation Model API rates in DBUs. The public PAYGO
# list-price conversion is $0.07/DBU; an account-specific contracted rate can
# be supplied without changing parser code or using binary floating point.
DEFAULT_DBU_USD = Decimal("0.07")

# Price-page label -> canonical model metadata. Endpoint names are checked
# against Databricks' live supported-model documentation on every refresh.
MODEL_CONFIG: dict[str, dict[str, Any]] = {
    "Kimi K3": {
        "id": "moonshotai/kimi-k3",
        "upstream_id": "databricks-kimi-k3",
        "display_name": "Kimi K3",
        "context_length": 1_048_576,
        "input_modalities": ["text", "image"],
        "features": ["chat", "completion", "reasoning", "prompt_caching"],
    },
    "GLM-5.2": {
        "id": "z-ai/glm-5.2",
        "upstream_id": "databricks-glm-5-2",
        "display_name": "GLM 5.2",
        "context_length": 1_048_576,
        "features": ["chat", "completion", "reasoning", "prompt_caching"],
    },
    "Inkling": {
        "id": "thinkingmachines/inkling",
        "upstream_id": "databricks-inkling",
        "display_name": "Inkling",
        "context_length": 1_048_576,
        "input_modalities": ["text", "image"],
        "features": ["chat", "completion", "reasoning", "prompt_caching"],
    },
    "Qwen 3.5 122B": {
        "id": "qwen/qwen3.5-122b-a10b",
        "upstream_id": "databricks-qwen35-122b-a10b",
        "display_name": "Qwen 3.5 122B A10B",
        "context_length": 262_144,
        "max_output_tokens": 25_000,
        "features": ["chat", "completion", "reasoning"],
    },
    "Qwen 3 Next 80B": {
        "id": "qwen/qwen3-next-80b-a3b-instruct",
        "upstream_id": "databricks-qwen3-next-80b-a3b-instruct",
        "display_name": "Qwen3 Next 80B A3B Instruct",
        "context_length": 262_144,
        "features": ["chat", "completion"],
    },
    "GPT OSS 120B": {
        "id": "openai/gpt-oss-120b",
        "upstream_id": "databricks-gpt-oss-120b",
        "display_name": "GPT OSS 120B",
        "context_length": 131_072,
        "features": ["chat", "completion", "reasoning"],
    },
    "GPT OSS 20B": {
        "id": "openai/gpt-oss-20b",
        "upstream_id": "databricks-gpt-oss-20b",
        "display_name": "GPT OSS 20B",
        "context_length": 131_072,
        "features": ["chat", "completion", "reasoning"],
    },
    "Llama 4 Maverick": {
        "id": "meta-llama/llama-4-maverick",
        "upstream_id": "databricks-llama-4-maverick",
        "display_name": "Llama 4 Maverick",
        "context_length": 1_048_576,
        "input_modalities": ["text", "image"],
        "features": ["chat", "completion"],
    },
    "Llama 3.3 70B": {
        "id": "meta-llama/llama-3.3-70b-instruct",
        "upstream_id": "databricks-meta-llama-3-3-70b-instruct",
        "display_name": "Llama 3.3 70B Instruct",
        "context_length": 131_072,
        "features": ["chat", "completion"],
    },
    "Gemma 3 12B": {
        "id": "google/gemma-3-12b-it",
        "upstream_id": "databricks-gemma-3-12b",
        "display_name": "Gemma 3 12B",
        "context_length": 131_072,
        "input_modalities": ["text", "image"],
        "features": ["chat", "completion"],
    },
    "Llama 3.1 8B": {
        "id": "meta-llama/llama-3.1-8b-instruct",
        "upstream_id": "databricks-meta-llama-3-1-8b-instruct",
        "display_name": "Llama 3.1 8B Instruct",
        "context_length": 131_072,
        "features": ["chat", "completion"],
    },
}
EXPECTED_MODELS = [str(row["id"]) for row in MODEL_CONFIG.values()]
UPSTREAM_ID_MAP = {
    str(row["id"]): str(row["upstream_id"]) for row in MODEL_CONFIG.values()
}

# Submitted to Databricks as Tier 2 account quota requests on 2026-08-19.
# These values are deliberately metadata-only until Databricks confirms the
# increase. Treating a submitted request as granted capacity would make the
# router over-admit traffic and turn predictable fallback into avoidable 429s.
_TIER_2_RATE_LIMIT_MODEL_IDS = frozenset(
    {
        "moonshotai/kimi-k3",
        "thinkingmachines/inkling",
        "z-ai/glm-5.2",
    }
)
_TIER_2_RATE_LIMIT_REQUEST = {
    "tier": 2,
    "status": "submitted",
    "submitted_on": "2026-08-19",
    "input_tokens_per_minute": 1_000_000,
    "output_tokens_per_minute": 100_000,
}
_DISCOVERED_MANIFEST_ROWS: dict[str, dict[str, Any]] = {}
_LIVE_CANARY_CHECKED_MODEL_IDS: set[str] = set()
_LIVE_CANARY_HEALTHY_MODEL_IDS: set[str] = set()


class _TableRows(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._row is not None and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None


def _table_rows(html: str) -> list[list[str]]:
    parser = _TableRows()
    parser.feed(html)
    return parser.rows


def _dbu_usd() -> Decimal:
    raw = os.environ.get("DATABRICKS_DBU_USD", str(DEFAULT_DBU_USD))
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise RuntimeError("databricks: DATABRICKS_DBU_USD must be decimal") from exc
    if not value.is_finite() or value <= 0 or value > Decimal("10"):
        raise RuntimeError("databricks: DATABRICKS_DBU_USD is outside safety bounds")
    return value


def _dbu_per_m_to_microdollars(value: str, *, dbu_usd: Decimal) -> int | None:
    normalized = value.replace(",", "").strip().casefold()
    if not normalized or normalized in {"n/a", "na", "-"}:
        return None
    try:
        dbus = Decimal(normalized)
    except InvalidOperation:
        return None
    if not dbus.is_finite() or dbus < 0:
        return None
    # Published DBU rates are rounded to three decimals and are chosen to map
    # to cent-denominated USD/M token prices (for example 42.857 DBU -> $3).
    # Quantize after conversion so the table's rounding noise does not become
    # a surprising $3.00001 public price.
    usd_per_m = (dbus * dbu_usd).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int((usd_per_m * Decimal("1000000")).to_integral_value())


def _parse_catalog(
    pricing_html: str,
    supported_models_html: str,
) -> tuple[dict[str, ModelPrice], dict[str, dict[str, Any]]]:
    dbu_usd = _dbu_usd()
    price_rows = {row[0]: row for row in _table_rows(pricing_html) if row}
    prices: dict[str, ModelPrice] = {}
    discovered: dict[str, dict[str, Any]] = {}
    for label, config in MODEL_CONFIG.items():
        endpoint = str(config["upstream_id"])
        if endpoint not in supported_models_html:
            continue
        source = price_rows.get(label)
        if source is None or len(source) < 3:
            continue
        prompt = _dbu_per_m_to_microdollars(source[1], dbu_usd=dbu_usd)
        completion = _dbu_per_m_to_microdollars(source[2], dbu_usd=dbu_usd)
        cached = (
            _dbu_per_m_to_microdollars(source[3], dbu_usd=dbu_usd)
            if len(source) >= 4
            else None
        )
        if prompt is None or completion is None:
            continue
        model_id = str(config["id"])
        prices[model_id] = ModelPrice(
            prompt_micro_per_m=prompt,
            completion_micro_per_m=completion,
            prompt_cached_micro_per_m=cached,
        )
        row = {
            "id": model_id,
            "upstream_id": endpoint,
            "display_name": str(config["display_name"]),
            "title": str(config["display_name"]),
            "model_type": "chat",
            "input_modalities": list(config.get("input_modalities", ["text"])),
            "output_modalities": ["text"],
            "endpoints": ["chat/completions"],
            "context_length": int(config["context_length"]),
            "supported_features": list(config["features"]),
            "status": 1,
            "routable": True,
        }
        if config.get("max_output_tokens") is not None:
            row["max_output_tokens"] = int(config["max_output_tokens"])
        if model_id in _TIER_2_RATE_LIMIT_MODEL_IDS:
            row["account_rate_limit_request"] = dict(_TIER_2_RATE_LIMIT_REQUEST)
        discovered[model_id] = row
    return prices, discovered


def normalize_workspace_host(value: str) -> str:
    raw = value.strip()
    if not raw:
        raise RuntimeError("databricks: DATABRICKS_HOST is empty")
    if "://" not in raw:
        raw = f"https://{raw}"
    parsed = urlsplit(raw)
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    allowed = (
        hostname.endswith(".cloud.databricks.com")
        or hostname.endswith(".azuredatabricks.net")
        or hostname.endswith(".gcp.databricks.com")
    )
    if (
        parsed.scheme != "https"
        or not allowed
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise RuntimeError("databricks: DATABRICKS_HOST is not an approved workspace URL")
    return f"https://{hostname}"


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_MANIFEST_ROWS  # noqa: PLW0603
    global _LIVE_CANARY_CHECKED_MODEL_IDS  # noqa: PLW0603
    global _LIVE_CANARY_HEALTHY_MODEL_IDS  # noqa: PLW0603

    transport = httpx.HTTPTransport(retries=PROVIDER_FETCH_TRANSPORT_RETRIES)
    with httpx.Client(
        timeout=PROVIDER_FETCH_TIMEOUT,
        follow_redirects=True,
        transport=transport,
        headers={"User-Agent": PROVIDER_FETCH_UA},
    ) as client:
        pricing_response = client.get(PRICING_URL, headers={"Accept": "text/html"})
        pricing_response.raise_for_status()
        models_response = client.get(
            SUPPORTED_MODELS_URL,
            headers={"Accept": "text/html"},
        )
        models_response.raise_for_status()

    prices, discovered = _parse_catalog(pricing_response.text, models_response.text)
    _DISCOVERED_MANIFEST_ROWS = discovered
    errors = validate(prices, EXPECTED_MODELS)
    if errors:
        raise RuntimeError("; ".join(errors))

    token = (os.environ.get("DATABRICKS_TOKEN") or "").strip()
    host = (os.environ.get("DATABRICKS_HOST") or "").strip()
    if bool(token) != bool(host):
        raise RuntimeError("databricks: DATABRICKS_HOST and DATABRICKS_TOKEN must be set together")
    _LIVE_CANARY_CHECKED_MODEL_IDS = set()
    _LIVE_CANARY_HEALTHY_MODEL_IDS = set()
    if token and host:
        base_url = f"{normalize_workspace_host(host)}/serving-endpoints"
        for model_id, row in discovered.items():
            _LIVE_CANARY_CHECKED_MODEL_IDS.add(model_id)
            if probe_openai_chat(
                base_url=base_url,
                api_key=token,
                model=str(row["upstream_id"]),
                max_tokens=4,
            ):
                _LIVE_CANARY_HEALTHY_MODEL_IDS.add(model_id)

    return ProviderPricingResult(
        slug=SLUG,
        prices=prices,
        source="api",
        fetched_url=PRICING_URL,
        notes=[
            f"matched {len(discovered)} documented pay-per-token endpoints",
            f"converted DBU prices at ${_dbu_usd()}/DBU",
            "account canary "
            + (
                f"passed {len(_LIVE_CANARY_HEALTHY_MODEL_IDS)}/"
                f"{len(_LIVE_CANARY_CHECKED_MODEL_IDS)} models"
                if _LIVE_CANARY_CHECKED_MODEL_IDS
                else "not configured"
            ),
        ],
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    notes = write_discovered_chat_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        discovered_rows=_DISCOVERED_MANIFEST_ROWS,
        source_url=SUPPORTED_MODELS_URL,
    )
    # Public hourly refreshes intentionally do not receive production
    # Databricks credentials. Preserve the last private canary state unless
    # this process was explicitly configured to run an authenticated check.
    if os.environ.get("DATABRICKS_TOKEN") and os.environ.get("DATABRICKS_HOST"):
        set_manifest_model_canary_states(
            MANIFEST_PATH,
            checked_model_ids=_LIVE_CANARY_CHECKED_MODEL_IDS,
            healthy_model_ids=_LIVE_CANARY_HEALTHY_MODEL_IDS,
        )
    return notes
