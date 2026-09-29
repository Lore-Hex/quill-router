"""A fixture model never outlives its test in a cached projection of the catalog.

The public catalog payload and the comparison indexes are cached for the
process. A test that serves its own routes and renders a page computes them
from its fixture catalog. If what it computed stayed cached, a later test
would see a model the catalog no longer carries (a comparison link that
answers 404), and which test failed would depend on the order tests ran in.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from tests.fixture_routes import serve_on_fixture_route
from tests.pinned_manifests import _deepseek_row, serve_manifest_rows
from trusted_router import dashboard
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
