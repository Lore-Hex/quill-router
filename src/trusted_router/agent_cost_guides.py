"""Catalog-backed examples for the agent and gateway cost guides."""

from __future__ import annotations

import json
from bisect import bisect_right
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

from trusted_router import catalog_data, provider_lifecycle
from trusted_router.catalog import (
    MODEL_ENDPOINTS,
    MODELS,
    PROVIDERS,
    Model,
    ModelEndpoint,
    endpoints_for_model,
    published_context_length,
    published_max_completion_tokens,
)
from trusted_router.pricing import (
    STANDARD_PRICE_MARKUP_BASIS_POINTS,
    PriceTier,
    resolve_request_rates,
)

DEEPSEEK_MODEL = "deepseek/deepseek-v4.1-flash"
COST_GUIDE_ROUTES = (
    (DEEPSEEK_MODEL, "deepinfra"),
    ("openai/gpt-6-luna", "openai"),
    ("anthropic/claude-sonnet-5.5", "anthropic"),
    ("openai/gpt-6.2-sol", "openai"),
    ("anthropic/claude-opus-5.5", "anthropic"),
)
REQUESTS = 10_000
INPUT_TOKENS = 4_000
OUTPUT_TOKENS = 1_000
CHEAP_SHARE = Decimal("0.8")
ESCALATION_SHARE = Decimal("0.2")
_MILLION = Decimal(1_000_000)
ALIAS_POLICIES = {
    "auto": "Broad model pool with provider fallback.",
    "cheap": "Paid model pool ordered by input plus output prices.",
    "e2e": "Routes requiring verified confidential compute, provider E2EE and explicit ZDR.",
    "fast": "Explicit Cerebras model pool for speed-oriented routing.",
    "synth": "Multiple model candidates combined into one synthesized answer.",
    "synth-code": "Code-tuned multi-model synthesis.",
    "zdr": "Routes restricted to zero data retention providers.",
}


def _money(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), ",.2f")


def _model_name(model: Model) -> str:
    return model.name.split(": ", 1)[-1]


def _price_date_label(stamp: str) -> str:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(UTC).strftime("%B %-d, %Y, %H:%M UTC")


def _price_dates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group the rows' manifest times by the minute the page shows.

    One refresh writes the provider manifests a second or so apart, so their
    stamps differ while their labels agree. Grouping by the stamp then listed
    a provider beside the same displayed time; grouping by the label lists a
    provider only when its prices were collected at a different shown time.
    A group's machine-readable time is its earliest stamp.
    """
    if not rows:
        return []
    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        label = _price_date_label(row["as_of"])
        group = groups.setdefault(label, {"timestamp": row["as_of"], "providers": [], "rows": 0})
        group["timestamp"] = min(group["timestamp"], row["as_of"])
        group["rows"] += 1
        if row["provider_name"] not in group["providers"]:
            group["providers"].append(row["provider_name"])
    common = max(groups, key=lambda label: groups[label]["rows"])
    return [{
        "timestamp": group["timestamp"],
        "label": label,
        "providers": group["providers"],
    } for label, group in sorted(groups.items(), key=lambda item: item[0] != common)]


def _rate(value: Decimal) -> str:
    return format(value, ".2f") if value == value.quantize(Decimal("0.01")) else format(value, "f")


def _manifest(provider: str) -> dict[str, Any] | None:
    path = Path(__file__).parent / "data" / "provider_models" / f"{provider}.json"
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None


def _source(manifest: dict[str, Any] | None, model_id: str) -> dict[str, Any] | None:
    if manifest is None:
        return None
    row = next((row for row in manifest["models"] if row["id"] == model_id), None)
    if row is None or not row.get("price_tiers"):
        return row
    return next((tier for tier in row["price_tiers"] if tier["max_prompt_tokens"] is None
                 or INPUT_TOKENS <= tier["max_prompt_tokens"]), None)


def _endpoint(model_id: str, provider: str) -> ModelEndpoint | None:
    return next((e for e in endpoints_for_model(model_id)
                 if e.provider == provider and e.usage_type == "Credits"), None)


def _cache_tier(endpoint: ModelEndpoint) -> PriceTier | None:
    return next((tier for tier in endpoint.price_tiers if tier.max_prompt_tokens is None
                 or INPUT_TOKENS <= tier.max_prompt_tokens), None)


def _config(
    published_models: list[Model], generation: str, *, full: bool = False, limits_only: bool = False,
) -> dict[str, Any] | None:
    if not published_models or (generation == "v1" and any(
        model.max_completion_tokens is None for model in published_models
    )):
        return None
    models = {}
    for model in published_models:
        limit = {"context": model.context_length}
        if model.max_completion_tokens is not None:
            limit["output"] = model.max_completion_tokens
        models[model.id] = {"limit": limit} if limits_only else {
            "name": _model_name(model), "limit": limit,
        }
    provider: dict[str, Any] = {"models": models}
    if full:
        provider.update(name="TrustedRouter")
        if generation == "v2":
            provider.update(env=["TRUSTEDROUTER_API_KEY"],
                            package="@opencode/ai/providers/openai-compatible")
        else:
            provider.update(npm="@ai-sdk/openai-compatible")
    return {
        "$schema": "https://opencode.ai/config.json",
        "model": "trustedrouter/" + published_models[0].id,
        "providers" if generation == "v2" else "provider": {"trustedrouter": provider},
    }


def _published_model(model_id: str) -> Model | None:
    model = MODELS.get(model_id)
    if model is None:
        return None
    endpoints = endpoints_for_model(model_id)
    if not model_id.startswith("trustedrouter/") and not any(
        endpoint.usage_type == "Credits" for endpoint in endpoints
    ):
        return None
    return replace(
        model, context_length=published_context_length(model),
        max_completion_tokens=published_max_completion_tokens(model, endpoints),
    )


@lru_cache(maxsize=1)
def _catalog_expiries(revision: tuple[int, str]) -> tuple[datetime, ...]:
    # Alias limits depend on routing pools beyond the guide's example models.
    return tuple(sorted({endpoint.catalog_valid_until for endpoint in MODEL_ENDPOINTS.values()
                         if endpoint.catalog_valid_until is not None}))


def _context_revision() -> tuple[tuple[int, str], int]:
    revision = provider_lifecycle.provider_catalog_revision()
    # Manifest expiry is independent of lifecycle cutovers and price periods.
    # Use the same clock and inclusive expiry boundary as catalog_is_current().
    now = catalog_data._utc_now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return revision, bisect_right(_catalog_expiries(revision), now)


def opencode_context() -> dict[str, Any]:
    # The full-provider template adds request-specific settings to nested dicts.
    return deepcopy(_opencode_context(_context_revision()))


@lru_cache(maxsize=1)
def _opencode_context(revision: tuple[tuple[int, str], int]) -> dict[str, Any]:
    aliases: list[dict[str, Any]] = [{"model": model, "policy": policy}
               for alias, policy in ALIAS_POLICIES.items()
               if (model := _published_model(f"trustedrouter/{alias}")) is not None]
    example = _published_model(DEEPSEEK_MODEL)
    models = [example] if example is not None else []
    return {
        "opencode_model": example, "opencode_aliases": aliases,
        "opencode_model_name": _model_name(example) if example is not None else None,
        "opencode_v2": _config(models, "v2"),
        "opencode_v1": _config(models, "v1"),
        "opencode_full": _config(models, "v2", full=True),
        "opencode_full_v1": _config(models, "v1", full=True),
        "opencode_limits": _config(
            [row["model"] for row in aliases if row["model"].context_length < 1_000_000], "v2", limits_only=True,
        ),
    }


def gateway_cost_context() -> dict[str, Any]:
    return _gateway_cost_context(_context_revision())


@lru_cache(maxsize=1)
def _gateway_cost_context(revision: tuple[tuple[int, str], int]) -> dict[str, Any]:
    """Calculate illustrative bills using current provider manifests and retail rates."""
    fee_rate = Decimal(STANDARD_PRICE_MARKUP_BASIS_POINTS) / 10_000
    rows: list[dict[str, Any]] = []
    totals: dict[str, Decimal] = {}
    provider_totals: dict[str, Decimal] = {}
    for model_id, provider in COST_GUIDE_ROUTES:
        manifest = _manifest(provider)
        source = _source(manifest, model_id)
        model = MODELS.get(model_id)
        endpoint = _endpoint(model_id, provider)
        provider_info = PROVIDERS.get(provider)
        if manifest is None or source is None or model is None or endpoint is None or provider_info is None:
            continue
        rates = resolve_request_rates(
            endpoint.price_tiers,
            headline_prompt_micro_per_m=endpoint.prompt_price_microdollars_per_million_tokens,
            headline_completion_micro_per_m=endpoint.completion_price_microdollars_per_million_tokens,
            total_prompt_tokens=INPUT_TOKENS,
        )
        provider_input = Decimal(source["input_token_price_per_m"]) / _MILLION
        provider_output = Decimal(source["output_token_price_per_m"]) / _MILLION
        retail_input = Decimal(rates.prompt_price_microdollars_per_million_tokens) / _MILLION
        retail_output = Decimal(rates.completion_price_microdollars_per_million_tokens) / _MILLION
        provider_cost = REQUESTS * (
            INPUT_TOKENS * provider_input + OUTPUT_TOKENS * provider_output
        ) / _MILLION
        fee = provider_cost * fee_rate
        per_request_micro = sum(
            (tokens * rate).quantize(Decimal(1), rounding=ROUND_HALF_UP)
            for tokens, rate in ((INPUT_TOKENS, retail_input), (OUTPUT_TOKENS, retail_output))
        )
        total = REQUESTS * max(per_request_micro, Decimal(1)) / _MILLION
        totals[model_id] = total
        provider_totals[model_id] = provider_cost
        rows.append({
            "model_id": model_id, "name": _model_name(model), "provider": provider,
            "provider_name": provider_info.name,
            "as_of": manifest["generated_at"], "source_url": manifest["source"],
            "provider_input": _rate(provider_input), "provider_output": _rate(provider_output),
            "retail_input": _rate(retail_input), "retail_output": _rate(retail_output),
            "provider_cost": _money(provider_cost), "fee": _money(fee),
            "rounding": _money(total - provider_cost - fee).replace(",", ""), "total": _money(total),
        })
    by_model = {row["model_id"]: row for row in rows}
    small_id, mid_id = COST_GUIDE_ROUTES[1][0], COST_GUIDE_ROUTES[2][0]
    small, mid = totals.get(small_id), totals.get(mid_id)
    deepseek = MODELS.get(DEEPSEEK_MODEL)
    routes = []
    for endpoint in endpoints_for_model(DEEPSEEK_MODEL):
        manifest = _manifest(endpoint.provider)
        provider_info = PROVIDERS.get(endpoint.provider)
        if (deepseek is None or endpoint.usage_type != "Credits" or provider_info is None
                or manifest is None or _source(manifest, DEEPSEEK_MODEL) is None):
            continue
        rates = resolve_request_rates(
            endpoint.price_tiers,
            headline_prompt_micro_per_m=endpoint.prompt_price_microdollars_per_million_tokens,
            headline_completion_micro_per_m=endpoint.completion_price_microdollars_per_million_tokens,
            total_prompt_tokens=INPUT_TOKENS,
        )
        prompt = Decimal(rates.prompt_price_microdollars_per_million_tokens) / _MILLION
        output = Decimal(rates.completion_price_microdollars_per_million_tokens) / _MILLION
        routes.append({"provider": endpoint.provider, "input": _rate(prompt),
                       "provider_name": provider_info.name,
                       "output": _rate(output), "sort_price": prompt + output,
                       "workload_price": INPUT_TOKENS * prompt + OUTPUT_TOKENS * output,
                       "as_of": manifest["generated_at"]})
    routes.sort(key=lambda r: (r["sort_price"], r["provider"]))
    figures: dict[str, Any] = {}
    percentages: dict[str, Decimal] = {}
    if small is not None and mid is not None:
        mixed = CHEAP_SHARE * small + (1 - CHEAP_SHARE) * mid
        figures.update(mixed_total=_money(mixed), baseline=_money(mid), saving=_money(mid - mixed),
                       escalation_total=_money(small + ESCALATION_SHARE * mid))
        percentages["escalation_percent"] = ESCALATION_SHARE * 100
        if mid:
            percentages["saving_percent"] = (mid - mixed) / mid * 100
        if mid > small:
            percentages["halving_percent"] = mid / (2 * (mid - small)) * 100
            percentages["break_even_percent"] = (mid - provider_totals[mid_id]) / (mid - small) * 100
    if routes and deepseek is not None:
        low, high = routes[0], routes[-1]
        figures.update(route_low=low, route_high=high, route_count=len(routes),
                       route_dates=_price_dates([low, high]),
                       deepseek={"name": _model_name(deepseek), "model_id": deepseek.id})
        if high["workload_price"]:
            percentages["route_saving_percent"] = (1 - low["workload_price"] / high["workload_price"]) * 100
    cache_endpoint = _endpoint(small_id, COST_GUIDE_ROUTES[1][1])
    cache_tier = _cache_tier(cache_endpoint) if cache_endpoint is not None else None
    if small_id in by_model and cache_tier is not None and cache_tier.prompt_cached_price_microdollars_per_million_tokens is not None:
        cache_price = Decimal(cache_tier.prompt_cached_price_microdollars_per_million_tokens) / _MILLION
        cache_input = Decimal(by_model[small_id]["retail_input"])
        if cache_input:
            figures.update(cache_input=_rate(cache_input), cache_read=_rate(cache_price))
            percentages["cache_saving_percent"] = (1 - cache_price / cache_input) * 100
    displays = {key: _money(value).rstrip("0").rstrip(".") for key, value in percentages.items()}
    for key, places in (("halving_percent", 0), ("route_saving_percent", 0), ("break_even_percent", 1)):
        if key in percentages:
            displays[key] = format(percentages[key], f".{places}f")
    return {"cost_guide": {
        **figures, **displays, **{key + "_exact": str(value) for key, value in percentages.items()},
        "sdk_model": MODELS.get("openai/gpt-5.4-mini"),
        "cheap_alias": MODELS.get("trustedrouter/cheap"),
        "rows": rows, "small": by_model.get(small_id), "mid": by_model.get(mid_id),
        "price_dates": _price_dates(rows),
        "requests": REQUESTS, "input_tokens": INPUT_TOKENS, "output_tokens": OUTPUT_TOKENS,
        "input_millions": REQUESTS * INPUT_TOKENS // 1_000_000,
        "output_millions": REQUESTS * OUTPUT_TOKENS // 1_000_000,
        "fee_percent": str(fee_rate * 100).rstrip("0").rstrip("."),
        "fee_per_hundred": _money(100 * fee_rate), "hundred_total": _money(100 * (1 + fee_rate)),
        "cheap_percent": str(CHEAP_SHARE * 100).rstrip("0").rstrip("."),
        "premium_percent": str((1 - CHEAP_SHARE) * 100).rstrip("0").rstrip("."),
        "scale": [{"inference": _money(Decimal(n)), "fee": _money(n * fee_rate),
                   "total": _money(n * (1 + fee_rate))} for n in (1000, 10000, 100000)],
    }}
