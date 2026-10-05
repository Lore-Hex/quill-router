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

A test that changes the registry without these helpers is covered too:
conftest calls start_from_the_session_catalog before every test, which puts
the registry back as the session had it and throws away any projection a test
left in a cache.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
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


def _registry_bindings() -> dict[tuple[str, str], Any]:
    """What each loaded trusted_router module calls MODELS and MODEL_ENDPOINTS.
    Code reads the registry through its own module's names, and a test may
    bind one of those names to a dict of its own."""
    return {
        (module_name, name): vars(module)[name]
        for module_name, module in list(sys.modules.items())
        if module is not None and module_name.split(".")[0] == "trusted_router"
        for name in ("MODELS", "MODEL_ENDPOINTS")
        if name in vars(module)
    }


def _read_projections() -> list[tuple[str, Any, Callable[[], Any]]]:
    """Each cached projection: a name for it, the cache, and a call that reads
    it as the application does."""
    (payload_module, payload_name), *others = _cached_projections()
    return [
        # The payload is cached under the current price period.
        (payload_name, getattr(payload_module, payload_name), payload_module._current_catalog_payload),
        *((name, getattr(module, name), getattr(module, name)) for module, name in others),
    ]


# The session's catalog, recorded once: each module's name for a registry
# dict, what each dict held, and the projections computed from them.
_SESSION_BINDINGS: dict[tuple[str, str], Any] = {}
_SESSION_CONTENTS: list[tuple[dict[str, Any], list[tuple[str, Any]]]] = []
_SESSION_PROJECTIONS: dict[str, Any] = {}


def record_the_session_catalog() -> None:
    """Record the catalog the process has now as the session's: the data, and
    the vehicles conftest put back. conftest calls this when that catalog is
    complete, and start_from_the_session_catalog before every test.

    Only the first call in a process does anything. A test that runs pytest
    inside pytest on a copy of conftest (tests/test_lock_order_guard.py) calls
    it again, with whatever catalog its own test has by then.
    """
    if _SESSION_BINDINGS:
        return
    _SESSION_BINDINGS.update(_registry_bindings())
    _SESSION_CONTENTS.extend(
        (registry, list(registry.items()))
        for registry in {id(value): value for value in _SESSION_BINDINGS.values()}.values()
        if isinstance(registry, dict)
    )
    for name, _, read in _read_projections():
        _SESSION_PROJECTIONS[name] = read()


def restore_the_session_catalog() -> str:
    """Make the registry the session's again: the same dicts under the same
    names, holding the same items in the same order. Says what it found:
    "" (nothing to do), "order" (the same items in another order) or "items"
    (other items, or a name bound to another dict).

    Another order is what monkeypatch itself leaves: undoing a delitem puts
    the item back at the end of its dict. A route's place in MODEL_ENDPOINTS
    is the order a model's routes are listed and tried in, so without this a
    test saw them in an order that depended on which tests ran before it.
    """
    found = ""
    for (module_name, name), value in _SESSION_BINDINGS.items():
        module = sys.modules.get(module_name)
        if module is not None and vars(module).get(name) is not value:
            setattr(module, name, value)
            found = "items"
    for registry, items in _SESSION_CONTENTS:
        now = list(registry.items())
        if now == items:
            continue
        if found != "items" and not (len(now) == len(items) and dict(now) == dict(items)):
            found = "items"
        found = found or "order"
        registry.clear()
        registry.update(items)
    return found


def start_from_the_session_catalog() -> None:
    """Before every test: the registry as the session had it, and in each
    cache the session's own projection.

    Each projection is cached for the process. A test that changed the
    registry and built an app while a cache was empty (an earlier test had
    cleared it) left a projection of its own catalog there, and every later
    test in the process read it. The public listing then named routes the
    registry did not have, in whichever run ordered the tests that way: the
    whole-catalog check in tests/test_request_capabilities.py failed so on
    2026-10-04 and 2026-10-05, and once held back the hourly price refresh.

    So what a cache holds at a test's start is either the object this
    function put there, or it is thrown away and computed again now, when no
    test's catalog, clock or history is in place. It does not ask how a test
    came to fill a cache, so it does not depend on which of a projection's
    inputs the test changed.

    A test whose cache is full reads the session's projections even while it
    has changed the registry. One that wants its projections computed from
    its own catalog asks for that with bypass_catalog_caches.
    """
    restore_the_session_catalog()
    for name, cached, read in _read_projections():
        misses = cached.cache_info().misses
        value = read()
        if cached.cache_info().misses == misses and value is not _SESSION_PROJECTIONS.get(name):
            # A test cached this. Whatever it was computed from, it is not
            # the session's.
            cached.cache_clear()
            value = read()
        _SESSION_PROJECTIONS[name] = value


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
