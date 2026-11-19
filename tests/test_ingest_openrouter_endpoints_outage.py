"""OpenRouter /endpoints outages must not become a partial catalog snapshot.

On 2026-11-19 09:49Z OpenRouter listed its models but 404'd every
/models/{id}/endpoints lookup. The ingest treated each 404 as "no TR
endpoints", so the hourly refresh dropped 95 of 174 catalog models.
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest

from scripts import ingest_openrouter_catalog as ingest


def _model(index: int) -> dict[str, Any]:
    return {"id": f"author/model-{index}", "name": f"Model {index}"}


def _endpoint() -> dict[str, Any]:
    return {
        "provider_name": "DeepSeek",
        "model_id": "author/native",
        "tag": "deepseek",
        "pricing": {"prompt": "0.000001", "completion": "0.000002"},
    }


def _build(
    monkeypatch: pytest.MonkeyPatch,
    count: int,
    unavailable: set[int],
    *,
    error: bool = False,
) -> dict[str, Any]:
    models = [_model(index) for index in range(count)]
    monkeypatch.setattr(ingest, "fetch_models", lambda _client: models)

    def fetch_endpoints(_client: Any, model_id: str) -> list[dict[str, Any]] | None:
        index = int(model_id.rsplit("-", 1)[1])
        if index in unavailable:
            if error:
                raise httpx.ConnectError("upstream down")
            return None
        return [_endpoint()]

    monkeypatch.setattr(ingest, "fetch_endpoints", fetch_endpoints)
    return ingest.build_snapshot()


def test_fetch_endpoints_distinguishes_404_from_an_empty_endpoint_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "missing" in request.url.path:
            return httpx.Response(404, json={"error": "not found"})
        return httpx.Response(200, json={"data": {"endpoints": []}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert ingest.fetch_endpoints(client, "author/missing") is None
        assert ingest.fetch_endpoints(client, "author/present") == []


def test_every_endpoint_lookup_404ing_refuses_to_build(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ingest.OpenRouterEndpointsOutage, match="174/174"):
        _build(monkeypatch, 174, set(range(174)))


def test_majority_endpoint_errors_refuse_to_build(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ingest.OpenRouterEndpointsOutage, match="60/100"):
        _build(monkeypatch, 100, set(range(60)), error=True)


def test_isolated_endpoint_404s_still_build_without_those_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _build(monkeypatch, 100, set(range(9)))
    assert snapshot["model_count"] == 91


def test_tiny_catalog_tolerates_a_single_404(monkeypatch: pytest.MonkeyPatch) -> None:
    # Below the absolute floor, a fraction alone does not prove an outage.
    snapshot = _build(monkeypatch, 2, {0})
    assert snapshot["model_count"] == 1


def test_just_over_the_fraction_limit_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ingest.OpenRouterEndpointsOutage):
        _build(monkeypatch, 100, set(range(26)))
    snapshot = _build(monkeypatch, 100, set(range(25)))
    assert snapshot["model_count"] == 75
