"""A fixture model never outlives its test in a cached projection of the catalog.

The public catalog payload and the comparison indexes are cached for the
process. A test that serves its own routes and renders a page computes them
from its fixture catalog. If what it computed stayed cached, a later test
would see a model the catalog no longer carries (a comparison link that
answers 404), and which test failed would depend on the order tests ran in.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from tests.fixture_routes import (
    clear_catalog_caches,
    keep_a_changed_catalog_out_of_the_caches,
    restore_the_session_catalog,
    serve_on_fixture_route,
    the_catalog_is_the_sessions,
)
from tests.pinned_manifests import _deepseek_row, serve_manifest_rows
from trusted_router import catalog, dashboard
from trusted_router.catalog_data import ModelEndpoint
from trusted_router.routes import catalog as catalog_routes

PROJECTIONS = ["comparison pairs", "comparison index", "comparison neighbors", "public catalog"]


def _projections_naming(model_id: str) -> list[str]:
    def _in_rows(index: dict[str, tuple[tuple[str, ...], ...]]) -> bool:
        return any(
            model_id in key or any(model_id in str(row[0]) for row in rows)
            for key, rows in index.items()
        )

    found = []
    if any(model.id == model_id for pair in dashboard._model_comparison_pairs() for model in pair):
        found.append("comparison pairs")
    if _in_rows(dashboard._model_comparison_index()):
        found.append("comparison index")
    if _in_rows(dashboard._model_comparison_neighbor_index()):
        found.append("comparison neighbors")
    if any(shape.get("id") == model_id for shape in catalog_routes._current_catalog_payload().shapes):
        found.append("public catalog")
    return found


def _on_a_fixture_route(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    return serve_on_fixture_route(
        monkeypatch, "fixture/cache-probe", "deepinfra", author="deepinfra"
    ).model_id


def _on_manifest_rows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    serve_manifest_rows(
        monkeypatch, tmp_path, "deepseek", [_deepseek_row("deepseek/cache-probe", "Cache Probe")]
    )
    return "deepseek/cache-probe"


@pytest.mark.parametrize("serve", [_on_a_fixture_route, _on_manifest_rows])
def test_a_fixture_model_never_outlives_its_test_in_a_cached_projection(
    serve: Callable[[pytest.MonkeyPatch, Path], str], tmp_path: Path
) -> None:
    with pytest.MonkeyPatch.context() as monkeypatch:
        model_id = serve(monkeypatch, tmp_path)
        # Control: while it is served, every projection is computed from the
        # fixture catalog and names the model.
        assert _projections_naming(model_id) == PROJECTIONS
    assert _projections_naming(model_id) == []


# A vehicle (tests/catalog_vehicles.py), so the catalog always carries it.
_RIDDEN = "anthropic/claude-haiku-4.5"


def _a_route_the_catalog_lacks() -> ModelEndpoint:
    template = next(
        endpoint for endpoint in catalog.endpoints_for_model(_RIDDEN) if not endpoint.is_byok
    )
    return replace(template, id=f"{_RIDDEN}@deepinfra/prepaid", provider="deepinfra")


def _put_into_the_registry(monkeypatch: pytest.MonkeyPatch) -> str:
    route = _a_route_the_catalog_lacks()
    monkeypatch.setitem(catalog.MODEL_ENDPOINTS, route.id, route)
    return route.id


def _bound_over_the_registry(monkeypatch: pytest.MonkeyPatch) -> str:
    route = _a_route_the_catalog_lacks()
    monkeypatch.setattr(catalog, "MODEL_ENDPOINTS", {**catalog.MODEL_ENDPOINTS, route.id: route})
    return route.id


def _listed_route_ids() -> set[str]:
    return {
        route["id"]
        for shape in catalog_routes._current_catalog_payload().shapes
        for route in shape["trustedrouter"].get("endpoints", ())
    }


@pytest.mark.parametrize("change", [_put_into_the_registry, _bound_over_the_registry])
def test_a_catalog_changed_by_hand_never_outlives_its_test_in_a_cached_projection(
    change: Callable[[pytest.MonkeyPatch], str],
) -> None:
    """A test that changes the registry itself, with neither helper above and
    without bypass_catalog_caches, and reads a projection whose cache is empty.
    Fifty test files change the registry by hand, and most use neither helper.
    What such a test computed used to stay cached: the listing then named a
    decide test's routes to the whole-catalog check in
    tests/test_request_capabilities.py."""
    # An earlier test's `cache_clear()` leaves the cache like this.
    clear_catalog_caches()
    with pytest.MonkeyPatch.context() as monkeypatch:
        route_id = change(monkeypatch)
        # Control: the cache was empty, so the listing was computed from the
        # changed catalog.
        assert route_id in _listed_route_ids()
    assert route_id not in _listed_route_ids()


def test_the_session_catalog_is_recorded_once() -> None:
    """conftest is imported again by a test that runs pytest inside pytest on
    a copy of it. That second call must not take the catalog of the test it
    is called in for the session's, or wrap the projections twice."""
    projection = catalog_routes._public_catalog_payload
    with pytest.MonkeyPatch.context() as monkeypatch:
        route_id = _put_into_the_registry(monkeypatch)
        keep_a_changed_catalog_out_of_the_caches()
        assert not the_catalog_is_the_sessions()
    assert catalog_routes._public_catalog_payload is projection
    assert route_id not in catalog.MODEL_ENDPOINTS
    assert the_catalog_is_the_sessions()


def test_a_route_taken_out_and_put_back_keeps_its_place() -> None:
    """Undoing monkeypatch.delitem puts the route back at the end of the
    registry. Its place there is the order a model's routes are listed and
    tried in, so the next test must not start from that order."""
    as_it_was = list(catalog.MODEL_ENDPOINTS)
    first = next(endpoint.id for endpoint in catalog.endpoints_for_model(_RIDDEN))
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.delitem(catalog.MODEL_ENDPOINTS, first)
    # Control: this is what a test's own teardown leaves.
    assert list(catalog.MODEL_ENDPOINTS) != as_it_was
    assert list(catalog.MODEL_ENDPOINTS)[-1] == first
    assert restore_the_session_catalog() == "order"
    assert list(catalog.MODEL_ENDPOINTS) == as_it_was
    assert restore_the_session_catalog() == ""


_INNER = """
from tests.fixture_routes import clear_catalog_caches
from trusted_router import catalog
from trusted_router.routes import catalog as catalog_routes

ORDER = list(catalog.MODEL_ENDPOINTS)


def test_leaves_another_order_and_an_empty_cache(monkeypatch):
    monkeypatch.delitem(catalog.MODEL_ENDPOINTS, ORDER[0])
    clear_catalog_caches()


def test_starts_from_the_session_order_with_the_listing_cached():
    assert list(catalog.MODEL_ENDPOINTS) == ORDER
    assert catalog_routes._public_catalog_payload.cache_info().currsize == 1
"""


def test_every_test_starts_from_the_session_catalog(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """conftest's own hook, run for real: pytest inside pytest on a copy of
    conftest, as tests/test_lock_order_guard.py does. The first inner test
    leaves the registry in another order and a cache empty; the second must
    find neither."""
    (tmp_path / "conftest.py").write_text(Path(__file__).with_name("conftest.py").read_text())
    inner = tmp_path / "test_inner_catalog.py"
    inner.write_text(_INNER)
    result = pytest.main([
        str(inner), "-q", "-p", "no:cacheprovider", "--basetemp=" + str(tmp_path / "inner-tmp"),
        "--confcutdir=" + str(tmp_path), "--import-mode=importlib", "-o", "addopts=",
    ])
    output = capsys.readouterr().out
    assert result == pytest.ExitCode.OK, output
    assert "2 passed" in output
