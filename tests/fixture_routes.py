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
keep_a_changed_catalog_out_of_the_caches, which conftest calls once, drops a
cached projection the moment it is computed from a catalog that is not the
session's, and every test starts with the session's projections cached.
"""

from __future__ import annotations

import functools
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
    A projection reads the registry through the names of the modules that
    compute it, and a test may bind one of those names to a dict of its own."""
    return {
        (module_name, name): vars(module)[name]
        for module_name, module in list(sys.modules.items())
        if module is not None and module_name.split(".")[0] == "trusted_router"
        for name in ("MODELS", "MODEL_ENDPOINTS")
        if name in vars(module)
    }


# The session's catalog, set once by keep_a_changed_catalog_out_of_the_caches:
# each module's name for a registry dict, and what each dict held.
_SESSION_BINDINGS: dict[tuple[str, str], Any] = {}
_SESSION_CONTENTS: list[tuple[dict[str, Any], list[tuple[str, Any]]]] = []


def the_catalog_is_the_sessions() -> bool:
    """No item was put into or taken out of a registry dict, and no module's
    MODELS or MODEL_ENDPOINTS is bound to another dict."""
    return all(
        vars(sys.modules[module_name]).get(name) is value
        for (module_name, name), value in _SESSION_BINDINGS.items()
        if sys.modules.get(module_name) is not None
    ) and all(list(registry.items()) == items for registry, items in _SESSION_CONTENTS)


def _dropping_a_changed_catalog(cached: Any) -> Callable[..., Any]:
    @functools.wraps(cached)
    def projection(*args: Any) -> Any:
        misses = cached.cache_info().misses
        value = cached(*args)
        if cached.cache_info().misses != misses and not the_catalog_is_the_sessions():
            cached.cache_clear()
        return value

    # What bypass_catalog_caches and a test's own cache read and clear.
    projection.__wrapped__ = cached.__wrapped__
    projection.cache_clear = cached.cache_clear  # type: ignore[attr-defined]
    projection.cache_info = cached.cache_info  # type: ignore[attr-defined]
    return projection


def keep_a_changed_catalog_out_of_the_caches() -> None:
    """From this call on, a cached projection computed while the catalog is not
    the one the process has now (the session's) is dropped as soon as it is
    computed. The caller still gets it; no later test does.

    Each projection is cached for the process. A test that changed the
    registry and built an app while a cache was empty (an earlier test had
    cleared it) left a projection of its own catalog there, and every later
    test in the process read it. The public listing then named routes the
    registry did not have, in whichever run ordered the tests that way: the
    whole-catalog check in tests/test_request_capabilities.py failed so on
    2026-10-04 and 2026-10-05, and once held back the hourly price refresh.

    conftest calls this when the session's catalog is complete, and before
    every test restore_the_session_catalog and warm_catalog_caches. Only the
    first call in a process does anything: a test that runs pytest inside
    pytest on a copy of conftest (tests/test_lock_order_guard.py) calls it
    again, with whatever catalog its own test has by then.
    """
    if _SESSION_BINDINGS:
        return
    _SESSION_BINDINGS.update(_registry_bindings())
    _SESSION_CONTENTS.extend(
        (registry, list(registry.items()))
        for registry in {id(value): value for value in _SESSION_BINDINGS.values()}.values()
        if isinstance(registry, dict)
    )
    for module, name in _cached_projections():
        setattr(module, name, _dropping_a_changed_catalog(getattr(module, name)))


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


def warm_catalog_caches() -> None:
    """Compute whichever projections the caches lack. Before every test, once
    the catalog is the session's again, so a test that changes the registry
    reads the session's projections whatever ran before it, as it does when
    nothing has emptied a cache, and does not compute its own on every read. A
    test that wants its projections computed from its own catalog asks for
    that with bypass_catalog_caches."""
    (payload_module, _), *others = _cached_projections()
    # The payload is cached under the current price period.
    payload_module._current_catalog_payload()
    for module, name in others:
        getattr(module, name)()


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
