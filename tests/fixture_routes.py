"""Serve a model on a fixture route for one test.

A billing, routing or settlement rule needs a route to ride on, not today's
catalog: the catalog is rebuilt hourly from provider feeds, and a provider
delisting a model must not fail a rule that is not about that model.

The route and, when the catalog lacks it, the model go into the registry's own
dicts with monkeypatch.setitem, so routing sees them and the test's teardown
restores the catalog. Privacy and ZDR posture come from the static PROVIDERS
table. Lifecycle retirements still apply, so a route a retirement names is
refused here instead of silently leaving the candidates. The process-wide
projections of the catalog are computed from the fixture catalog, uncached, for
the test's duration, so fixture state never outlives the test.
"""

from __future__ import annotations

from typing import Any

import pytest

from trusted_router.catalog import MODEL_ENDPOINTS, MODELS, endpoints_for_model
from trusted_router.catalog_data import Model, ModelEndpoint


def _cached_projections() -> tuple[tuple[Any, str], ...]:
    """The process-wide cached public projections of the catalog."""
    from trusted_router import dashboard
    from trusted_router.routes import catalog as catalog_routes

    return (
        (catalog_routes, "_public_catalog_payload"),
        (dashboard, "_model_comparison_pairs"),
        (dashboard, "_model_comparison_index"),
        (dashboard, "_model_comparison_neighbor_index"),
    )


def bypass_catalog_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    """For this test, compute the cached public projections of the catalog on
    every call and cache none of them."""
    for module, name in _cached_projections():
        cached = getattr(module, name)
        monkeypatch.setattr(module, name, getattr(cached, "__wrapped__", cached))


def clear_catalog_caches() -> None:
    """Empty each cached public projection of the catalog that is still a cache."""
    for module, name in _cached_projections():
        cache_clear = getattr(getattr(module, name), "cache_clear", None)
        if cache_clear is not None:
            cache_clear()


def serve_on_fixture_route(
    monkeypatch: pytest.MonkeyPatch,
    model_id: str,
    host: str,
    *,
    author: str,
    usage_type: str = "Credits",
    context_length: int = 131_072,
    model: Model | None = None,
    **route_fields: Any,
) -> ModelEndpoint:
    """Serve `model_id` on `host`; `author` is the model's publisher slug.

    `route_fields` override the route's defaults (upstream id, prices, ...).
    The model the catalog already carries is kept as it is; otherwise `model`
    is carried, or a chat model.
    """
    bypass_catalog_caches(monkeypatch)
    monkeypatch.setitem(
        MODELS,
        model_id,
        MODELS.get(model_id)
        or model
        or Model(id=model_id, name=model_id, provider=author, context_length=context_length),
    )
    fields: dict[str, Any] = {
        "upstream_id": f"fixture-{host}",
        "prompt_price_microdollars_per_million_tokens": 1_000_000,
        "completion_price_microdollars_per_million_tokens": 3_000_000,
        **route_fields,
    }
    suffix = "byok" if usage_type == "BYOK" else "prepaid"
    route = ModelEndpoint(
        id=f"{model_id}@{host}/{suffix}",
        model_id=model_id,
        provider=host,
        usage_type=usage_type,
        **fields,
    )
    monkeypatch.setitem(MODEL_ENDPOINTS, route.id, route)
    assert route.id in {endpoint.id for endpoint in endpoints_for_model(model_id)}, (
        f"fixture: a lifecycle retirement refuses {route.id}"
    )
    return route


def drop_routes(monkeypatch: pytest.MonkeyPatch, model_id: str) -> None:
    """Take every route of `model_id` out of the catalog for this test, so the
    fixture routes it serves next are the model's only routes."""
    bypass_catalog_caches(monkeypatch)
    for endpoint_id, endpoint in list(MODEL_ENDPOINTS.items()):
        if endpoint.model_id == model_id:
            monkeypatch.delitem(MODEL_ENDPOINTS, endpoint_id)
