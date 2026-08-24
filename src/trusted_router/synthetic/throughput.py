"""Deterministic route selection and spend bounds for sustained benchmarks."""

from __future__ import annotations

import datetime as dt
import json
import time
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any

from trusted_router.synthetic.probes import rotation_candidates

_RECENT_MODEL_COUNT = 24
_RECENT_MODEL_BONUS = 5_000
_RECENT_MODEL_STEP = 50
# Keep a small cross-family baseline in the sustained benchmark while each
# model is callable. Catalog growth must not silently crowd these comparison
# anchors out, and a real retirement must not freeze the whole refresh.
THROUGHPUT_ANCHOR_MODELS = (
    "anthropic/claude-opus-5",
    "moonshotai/kimi-k3",
    "z-ai/glm-5.2",
    "google/gemini-3.6-flash",
)
THROUGHPUT_INTERVAL_SECONDS = 60


def throughput_candidates(*, limit: int = 200) -> list[tuple[str, str]]:
    """Return the deterministic high-value provider/model throughput set.

    A route is important when it participates in a public TrustedRouter alias
    or named orchestration model, is a recent catalog launch, or is the best
    available representative for a provider. Every active chat provider is
    represented before the remaining highest-scored routes fill the budget.
    """
    if limit <= 0:
        return []

    from trusted_router.routing import _THROUGHPUT_RANK

    pool = rotation_candidates()
    routes = [(provider, model) for provider, models in pool.items() for model in models]
    if not routes:
        return []

    importance = _model_importance(routes)
    releases = _model_release_epochs()
    prices = {route: credits_endpoint_prices(*route) or (2**63 - 1, 2**63 - 1) for route in routes}

    def route_key(route: tuple[str, str]) -> tuple[int, int, int, int, int, str, str]:
        provider, model = route
        prompt_price, completion_price = prices[route]
        return (
            -importance.get(model, 0),
            -releases.get(model, 0),
            _THROUGHPUT_RANK.get(provider, 80),
            completion_price,
            prompt_price,
            model,
            provider,
        )

    selected: list[tuple[str, str]] = []
    # Preserve provider breadth even when one popular model has many hosts.
    for provider in sorted(pool):
        provider_routes = [(provider, model) for model in pool[provider]]
        if provider_routes:
            selected.append(min(provider_routes, key=route_key))

    # Preserve one route for each reviewed comparison anchor. Use the same
    # deterministic scoring as the main selection so provider choice remains
    # stable, and skip anchors that no longer have a callable Credits route.
    for model in THROUGHPUT_ANCHOR_MODELS:
        model_routes = [route for route in routes if route[1] == model]
        if model_routes:
            anchor = min(model_routes, key=route_key)
            if anchor not in selected:
                selected.append(anchor)

    for route in sorted(routes, key=route_key):
        if route not in selected:
            selected.append(route)
        if len(selected) >= limit:
            break
    return selected[:limit]


def choose_throughput_target(
    candidates: list[tuple[str, str]],
    *,
    now_epoch_seconds: float | None = None,
    interval_seconds: int = THROUGHPUT_INTERVAL_SECONDS,
) -> tuple[str, str] | None:
    """Choose one route by scheduler time slot, giving exact round-robin coverage."""
    if not candidates:
        return None
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    now = time.time() if now_epoch_seconds is None else now_epoch_seconds
    slot = int(now // interval_seconds)
    return candidates[slot % len(candidates)]


def projected_monthly_cost_microdollars(
    candidates: list[tuple[str, str]],
    *,
    input_tokens: int = 64,
    output_tokens: int = 512,
    interval_seconds: int = THROUGHPUT_INTERVAL_SECONDS,
    days: int = 30,
) -> int:
    """Conservative full-cap monthly spend for the scheduled route rotation."""
    if not candidates or days <= 0:
        return 0
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")

    from trusted_router.money import token_cost_microdollars

    cycle_cost = 0
    for provider, model in candidates:
        prices = credits_endpoint_prices(provider, model)
        if prices is None:
            continue
        prompt_price, completion_price = prices
        cycle_cost += token_cost_microdollars(input_tokens, prompt_price)
        cycle_cost += token_cost_microdollars(output_tokens, completion_price)

    invocations = days * 24 * 60 * 60 // interval_seconds
    cycles = (invocations + len(candidates) - 1) // len(candidates)
    return cycle_cost * cycles


def _model_importance(routes: list[tuple[str, str]]) -> dict[str, int]:
    from trusted_router.catalog import META_MODEL_IDS, meta_candidate_models

    scores: defaultdict[str, int] = defaultdict(int)

    def concrete_models(model_id: str, seen: frozenset[str]) -> list[str]:
        if model_id in seen:
            return []
        next_seen = frozenset((*seen, model_id))
        concrete: list[str] = []
        for model in meta_candidate_models(model_id):
            if model.id in META_MODEL_IDS:
                concrete.extend(concrete_models(model.id, next_seen))
            else:
                concrete.append(model.id)
        return list(dict.fromkeys(concrete))

    for alias in sorted(META_MODEL_IDS):
        for index, model_id in enumerate(concrete_models(alias, frozenset())):
            scores[model_id] += 1_000 + max(0, 100 - index)

    active_models = {model for _, model in routes}
    recent_models = sorted(
        active_models,
        key=lambda model: (-_model_release_epochs().get(model, 0), model),
    )[:_RECENT_MODEL_COUNT]
    for index, model_id in enumerate(recent_models):
        scores[model_id] += _RECENT_MODEL_BONUS - index * _RECENT_MODEL_STEP
    return dict(scores)


def credits_endpoint_prices(provider: str, model: str) -> tuple[int, int] | None:
    from trusted_router.catalog import MODEL_ENDPOINTS, effective_endpoint

    endpoints = [
        effective_endpoint(endpoint)
        for endpoint in MODEL_ENDPOINTS.values()
        if endpoint.catalog_is_current()
        and endpoint.provider == provider
        and endpoint.model_id == model
        and endpoint.usage_type == "Credits"
    ]
    if not endpoints:
        return None
    endpoint = min(
        endpoints,
        key=lambda item: (
            item.completion_price_microdollars_per_million_tokens,
            item.prompt_price_microdollars_per_million_tokens,
            item.id,
        ),
    )
    return (
        endpoint.prompt_price_microdollars_per_million_tokens,
        endpoint.completion_price_microdollars_per_million_tokens,
    )


@lru_cache(maxsize=1)
def _model_release_epochs() -> dict[str, int]:
    """Read optional release timestamps from catalog snapshot/manifests."""
    data_root = Path(__file__).resolve().parents[1] / "data"
    paths = [
        data_root / "openrouter_snapshot.json",
        *sorted((data_root / "provider_models").glob("*.json")),
    ]
    releases: dict[str, int] = {}
    for path in paths:
        try:
            payload = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        for row in payload.get("models") or []:
            if not isinstance(row, dict):
                continue
            model_id = str(row.get("id") or "")
            if not model_id:
                continue
            epoch = _release_epoch(row.get("created") or row.get("created_at"))
            if epoch > releases.get(model_id, 0):
                releases[model_id] = epoch
    return releases


def _release_epoch(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        pass
    if not value:
        return 0
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return int(parsed.timestamp())
