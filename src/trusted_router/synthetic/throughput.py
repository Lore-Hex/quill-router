"""Deterministic route selection and spend bounds for sustained benchmarks."""

from __future__ import annotations

import datetime as dt
import hashlib
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
THROUGHPUT_INTERVAL_SECONDS = 60
THROUGHPUT_BATCH_SIZE = 5


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

    for route in sorted(routes, key=route_key):
        if route not in selected:
            selected.append(route)
        if len(selected) >= limit:
            break
    # Selection uses importance, recency, price, and provider breadth. Once the
    # set is chosen, use a stable provider round robin so routine price
    # refreshes do not reshuffle every route's slot and one five-probe batch
    # does not burst against a single provider.
    by_provider: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for route in selected[:limit]:
        by_provider[route[0]].append(route)
    for provider_routes in by_provider.values():
        provider_routes.sort()
    ordered: list[tuple[str, str]] = []
    for index in range(max(len(routes) for routes in by_provider.values())):
        for provider in sorted(by_provider):
            provider_routes = by_provider[provider]
            if index < len(provider_routes):
                ordered.append(provider_routes[index])
    return ordered


def throughput_slot(
    *,
    now_epoch_seconds: float | None = None,
    interval_seconds: int = THROUGHPUT_INTERVAL_SECONDS,
) -> int:
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    now = time.time() if now_epoch_seconds is None else now_epoch_seconds
    return int(now // interval_seconds)


def throughput_slots_for_batch(
    *,
    now_epoch_seconds: float | None = None,
    interval_seconds: int = THROUGHPUT_INTERVAL_SECONDS,
    batch_size: int = THROUGHPUT_BATCH_SIZE,
) -> list[int]:
    """Return the minute slots owned by the current deterministic batch.

    Cloud Scheduler can retry or overlap Cloud Run Job executions. Grouping
    five logical minute slots into one five-minute execution makes each batch
    bounded, while deterministic sample identities make retries overwrite the
    same Bigtable rows instead of inflating sample counts.
    """
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    current_slot = throughput_slot(
        now_epoch_seconds=now_epoch_seconds,
        interval_seconds=interval_seconds,
    )
    last_slot = current_slot - (current_slot % batch_size)
    first_slot = max(last_slot - batch_size + 1, 0)
    return list(range(first_slot, last_slot + 1))


def throughput_sample_identity(
    slot: int,
    provider: str,
    model: str,
    *,
    interval_seconds: int = THROUGHPUT_INTERVAL_SECONDS,
) -> tuple[str, str]:
    """Return a stable sample id and timestamp for one route/slot."""
    if slot < 0:
        raise ValueError("slot must be non-negative")
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    route_digest = hashlib.sha256(f"{provider}\0{model}".encode()).hexdigest()[:16]
    created_at = (
        dt.datetime.fromtimestamp(slot * interval_seconds, tz=dt.UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    return f"bench-throughput-{slot}-{route_digest}", created_at


def choose_throughput_target(
    candidates: list[tuple[str, str]],
    *,
    now_epoch_seconds: float | None = None,
    interval_seconds: int = THROUGHPUT_INTERVAL_SECONDS,
) -> tuple[str, str] | None:
    """Choose one route by scheduler time slot, giving exact round-robin coverage."""
    if not candidates:
        return None
    slot = throughput_slot(
        now_epoch_seconds=now_epoch_seconds,
        interval_seconds=interval_seconds,
    )
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
        if endpoint.provider == provider
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
