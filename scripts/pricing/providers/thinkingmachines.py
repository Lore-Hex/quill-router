"""Thinking Machines Lab Tinker model pricing refresh."""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

from scripts.pricing.base import ModelPrice, ProviderPricingResult, fetch_json
from scripts.pricing.manifest import set_manifest_model_canary_states
from scripts.pricing.openai_catalog import probe_openai_chat
from scripts.pricing.parsers.thinkingmachines import _SAMPLER_IDS, _SERVERLESS_IDS

SLUG = "thinkingmachines"
URL = "https://tinker-docs.thinkingmachines.ai/tinker/models/"
SERVERLESS_URL = "https://tinker-docs.thinkingmachines.ai/tinker/serverless.json"
SAMPLER_URL = "https://tinker-docs.thinkingmachines.ai/tinker/models.json"
EXPECTED_MODELS = [
    "thinkingmachines/inkling",
    "thinkingmachines/inkling-2-mini",
    "thinkingmachines/inkling-2-small",
    "thinkingmachines/inkling-small",
    "z-ai/glm-5.3",
]
BASE_URL = "https://tinker.thinkingmachines.dev/services/tinker-prod/oai/api/v1"
API_KEY_ENVS = ("THINKING_MACHINES_API_KEY", "TINKER_API_KEY")
# Listed in serverless.json, but TR's Tinker key gets a model-level 4xx for it:
# the configured-route probe classified six straight attempts as
# unsupported_route (Sentry QUILL-ROUTER-FD, 2026-11-08). Serverless inference
# is a per-model beta, so this is account access, not a TR adapter defect.
# Inkling and Inkling-Small serve on the same key and stay routable.
#
# The hold lives in the committed manifest, so price refreshes keep it. It
# lifts by itself the first time an authenticated canary to the exact upstream
# ID returns 200: run the refresh with THINKING_MACHINES_API_KEY set (the
# hourly workflow does not receive that key today). To lift it by hand once
# access is confirmed, delete the row's "routable"/"routable_reason" fields.
# The 2026-11-08 rejections were for the earlier serverless ID
# (":sampling-nvfp4" suffix); the canary probes the row's current upstream_id,
# which follows the exact ID serverless.json lists.
#
# Inkling 2 Mini (added 2026-11-25) starts under the same hold. Tinker lists
# its paid 256K sampler row, but TR's key has never been shown to serve it
# (Inkling 2 Small's listing did not imply access), so it is not routed until
# the access canary gets a 200 from its exact upstream ID.
ACCESS_HOLD_REASON = "provider-access-unavailable"
OPERATOR_HOLDS = {
    "thinkingmachines/inkling-2-mini": ACCESS_HOLD_REASON,
    "thinkingmachines/inkling-2-small": ACCESS_HOLD_REASON,
}
_ACCESS_CANARY_CHECKED: set[str] = set()
_ACCESS_CANARY_HEALTHY: set[str] = set()
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "thinkingmachines.json"
)


# Every key Tinker documents for each feed (tinker/models/, "Machine-Readable
# Pricing"), including models.json's temporary-discount fields. A priced row
# with any other key (a unit, currency, tier, range or condition) is refused,
# not read as one flat price. Neither feed states a unit today: the docs give
# per-million-token "$x" strings, so a stated unit is a format change that
# needs review, whatever it says.
_DOCUMENTED_ROW_KEYS = {
    ("input", "cached_input", "output"): frozenset(
        {"name", "url", "tinker_id", "context", "input", "cached_input", "output"}
    ),
    ("prefill", "cached_prefill", "sample"): frozenset(
        {
            "name", "url", "tinker_id", "type", "arch", "size", "params",
            "active_params", "context", "prefill", "cached_prefill", "sample",
            "train", "note", "original_prefill", "original_cached_prefill",
            "original_sample", "original_train",
        }
    ),
}
_PRICE_TEXT = re.compile(r"\$\d+(?:\.\d{1,6})?")
# Shapes every live row has (2026-11-19): ids like "org/Name:peft:262144:
# sampling-nvfp4", contexts 32K to 1M, and one discount wording on all eight
# discounted rows ("Limited-time 50% discount").
_TINKER_ID = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*(?::peft:[1-9][0-9]*)?(?::sampling-[a-z0-9]+)?"
)
_CONTEXT = re.compile(r"[1-9][0-9]*[KM]")
# Letters, digits, hyphens and dots, with at least one dot and no spaces.
_DNS_NAME = re.compile(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_DISCOUNT_NOTE = re.compile(r"Limited-time [1-9][0-9]?% discount")
_DISCOUNT_FIELDS = frozenset(
    {"original_prefill", "original_cached_prefill", "original_sample", "original_train"}
)


def _matching(pattern: re.Pattern[str]):  # noqa: ANN202
    return lambda value: isinstance(value, str) and pattern.fullmatch(value) is not None


def _https_url(value: object) -> bool:
    # The docs: "The url field is an empty string when no model-card link is
    # provided." Otherwise it must be an https URL whose host is a plain DNS
    # name and whose port, if any, is valid.
    if not isinstance(value, str):
        return False
    if value == "":
        return True
    try:
        parts = urlsplit(value)
        parts.port  # noqa: B018 - raises ValueError on a malformed or out-of-range port
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and parts.hostname is not None
        and _DNS_NAME.fullmatch(parts.hostname) is not None
    )


# Documented value shape of every allowlisted field. A dict or list where a
# string or count belongs (a nested unit, condition or note) is refused.
_FIELD_SHAPES = {
    "name": lambda value: isinstance(value, str) and bool(value.strip()),
    "url": _https_url,
    "tinker_id": _matching(_TINKER_ID),
    "context": _matching(_CONTEXT),
    "type": lambda value: isinstance(value, str) and bool(value.strip()),
    "arch": lambda value: isinstance(value, str) and bool(value.strip()),
    "size": lambda value: isinstance(value, str) and bool(value.strip()),
    "params": lambda value: type(value) is int and value > 0,
    "active_params": lambda value: type(value) is int and value > 0,
    "note": _matching(_DISCOUNT_NOTE),
    **{
        field: _matching(_PRICE_TEXT)
        for field in (
            "input", "cached_input", "output", "prefill", "cached_prefill", "sample", "train",
            *_DISCOUNT_FIELDS,
        )
    },
}


def _json_prices(
    payload: object, model_ids: dict[str, str], fields: tuple[str, str, str]
) -> dict[str, ModelPrice]:
    if not isinstance(payload, list):
        raise RuntimeError("thinkingmachines: pricing feed must be a list")
    row_keys = _DOCUMENTED_ROW_KEYS[fields]
    prices: dict[str, ModelPrice] = {}
    for row in payload:
        if not isinstance(row, dict):
            raise RuntimeError("thinkingmachines: malformed pricing row")
        native_id = row.get("tinker_id")
        if not isinstance(native_id, str) or native_id not in model_ids:
            continue
        unreviewed = sorted(set(row) - row_keys)
        if unreviewed:
            raise RuntimeError(
                f"thinkingmachines: unreviewed pricing field(s) {unreviewed} for {native_id}"
            )
        malformed = sorted(key for key, value in row.items() if not _FIELD_SHAPES[key](value))
        if malformed:
            raise RuntimeError(
                f"thinkingmachines: malformed pricing field(s) {malformed} for {native_id}"
            )
        if "note" in row and not _DISCOUNT_FIELDS <= row.keys():
            raise RuntimeError(f"thinkingmachines: note without original prices for {native_id}")
        amounts = []
        for field in fields:
            value = row.get(field)
            if not isinstance(value, str) or _PRICE_TEXT.fullmatch(value) is None:
                raise RuntimeError(f"thinkingmachines: invalid {field} price for {native_id}")
            amounts.append(int(Decimal(value[1:]) * 1_000_000))
        prompt, cached, completion = amounts
        if prompt <= 0 or completion <= 0 or cached > prompt:
            raise RuntimeError(f"thinkingmachines: invalid token prices for {native_id}")
        model_id = model_ids[native_id]
        price = ModelPrice(prompt, completion, prompt_cached_micro_per_m=cached)
        if model_id in prices and prices[model_id] != price:
            raise RuntimeError(f"thinkingmachines: conflicting prices for {native_id}")
        prices[model_id] = price
    missing = set(model_ids.values()) - prices.keys()
    if missing:
        raise RuntimeError(f"thinkingmachines: missing exact model prices: {sorted(missing)}")
    return prices


def fetch() -> ProviderPricingResult:
    # These are the provider's documented stable interfaces. HTML columns can
    # move when presentation-only fields such as active parameters are added.
    prices = _json_prices(
        fetch_json(SERVERLESS_URL), _SERVERLESS_IDS, ("input", "cached_input", "output")
    )
    prices.update(
        _json_prices(fetch_json(SAMPLER_URL), _SAMPLER_IDS, ("prefill", "cached_prefill", "sample"))
    )
    notes = ["verified exact deployed IDs against official serverless.json and models.json"]
    notes.extend(_run_access_canary())
    return ProviderPricingResult(
        slug=SLUG,
        prices=prices,
        source="api",
        fetched_url=URL,
        notes=notes,
    )


def _api_key() -> str | None:
    for name in API_KEY_ENVS:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return None


def _held_upstream_ids() -> dict[str, str]:
    """Rows currently under the access hold, keyed to their exact upstream ID."""

    if not MANIFEST_PATH.exists():
        return {}
    rows = json.loads(MANIFEST_PATH.read_text(encoding="utf-8")).get("models")
    if not isinstance(rows, list):
        return {}
    return {
        row["id"]: row["upstream_id"]
        for row in rows
        if isinstance(row, dict)
        and isinstance(row.get("id"), str)
        and isinstance(row.get("upstream_id"), str)
        and row.get("routable") is False
        and row.get("routable_reason") == ACCESS_HOLD_REASON
    }


def _run_access_canary() -> list[str]:
    """Recheck held routes only; healthy siblings are never probed or touched."""

    _ACCESS_CANARY_CHECKED.clear()
    _ACCESS_CANARY_HEALTHY.clear()
    key = _api_key()
    held = _held_upstream_ids()
    if key is None or not held:
        return []
    for model_id, upstream_id in sorted(held.items()):
        _ACCESS_CANARY_CHECKED.add(model_id)
        if probe_openai_chat(
            base_url=BASE_URL, api_key=key, model=upstream_id, max_tokens=64
        ):
            _ACCESS_CANARY_HEALTHY.add(model_id)
    return [
        f"access canary passed {len(_ACCESS_CANARY_HEALTHY)}/"
        f"{len(_ACCESS_CANARY_CHECKED)} held route(s)"
    ]


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    rows = raw.get("models")
    if not isinstance(rows, list):
        raise RuntimeError("thinkingmachines manifest has no models list")

    updated: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        model_id = row.get("id")
        if not isinstance(model_id, str):
            continue
        price = result.prices.get(model_id)
        if price is None:
            continue
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
        raise RuntimeError(f"thinkingmachines manifest did not update expected model(s): {missing}")

    raw["source"] = URL
    raw["generated_at"] = (
        datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    raw["model_count"] = len(rows)
    MANIFEST_PATH.write_text(
        json.dumps(raw, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    if _ACCESS_CANARY_CHECKED:
        set_manifest_model_canary_states(
            MANIFEST_PATH,
            checked_model_ids=_ACCESS_CANARY_CHECKED,
            healthy_model_ids=_ACCESS_CANARY_HEALTHY,
            failure_reason=ACCESS_HOLD_REASON,
        )
    return [
        "thinkingmachines: refreshed provider_models/thinkingmachines.json "
        f"({len(updated)} priced rows)"
    ]
