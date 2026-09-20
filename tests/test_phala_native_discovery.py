"""Phala discovery must use its first-party catalog, never Redpill's."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from scripts import check_price_coverage
from scripts.pricing.providers import phala

CATALOG_URL = "https://inference.phala.com/v1/models"
MODEL_ID = "z-ai/glm-5.3"
CATALOG = {
    "data": [
        {
            "id": MODEL_ID,
            "name": "Z.ai: GLM 5.3",
            "context_length": 1_048_576,
            "max_output_length": 131_072,
            "pricing": {
                "prompt": "0.0000014",
                "completion": "0.0000044",
                "input_cache_read": "0.00000026",
            },
        }
    ]
}


@pytest.fixture(autouse=True)
def isolate_discovery_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(phala, "UPSTREAM_ID_MAP", dict(phala.UPSTREAM_ID_MAP))
    monkeypatch.setattr(phala, "_DISCOVERED_MANIFEST_ROWS", {})


def mock_catalog(
    monkeypatch: pytest.MonkeyPatch,
    *,
    payload: Any = CATALOG,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> list[httpx.Request]:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert str(request.url) == CATALOG_URL
        assert "authorization" not in request.headers
        return httpx.Response(status, content=json.dumps(payload), headers=headers)

    monkeypatch.setattr(
        phala.httpx,
        "HTTPTransport",
        lambda **_kwargs: httpx.MockTransport(handle),
    )
    return requests


def test_phala_first_party_catalog_preserves_prices_and_route_posture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PHALA_CONFIDENTIAL_API_KEY", "unused-test-secret")
    monkeypatch.setenv("PHALA_API_KEY", "unused-test-secret")
    requests = mock_catalog(monkeypatch)
    result = phala.fetch()
    assert len(requests) == 1
    assert result.fetched_url == CATALOG_URL
    tier = result.prices[MODEL_ID].tiers[0]
    assert tier.prompt_micro_per_m == 1_400_000
    assert tier.completion_micro_per_m == 4_400_000
    assert tier.prompt_cached_micro_per_m == 260_000
    row = phala._DISCOVERED_MANIFEST_ROWS[MODEL_ID]
    assert row["context_length"] == 1_048_576
    assert row["upstream_id"] == MODEL_ID
    assert row["provider_route_class"] == "standard_pass_through"

    manifest_path = tmp_path / "phala.json"
    manifest_path.write_text(json.dumps({"provider": "phala", "models": []}))
    monkeypatch.setattr(phala, "MANIFEST_PATH", manifest_path)
    phala.write_provider_manifest(result)
    manifest = json.loads(manifest_path.read_text())
    assert manifest["source"] == CATALOG_URL
    assert manifest["models"][0]["cached_input_token_price_per_m"] == 260_000


@pytest.mark.parametrize("status", [301, 302, 307, 308, 401, 429, 500, 503])
def test_phala_failure_never_follows_or_falls_back_to_redpill(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    requests = mock_catalog(
        monkeypatch,
        status=status,
        headers={"location": "https://api.redpill.ai/v1/models"},
    )
    with pytest.raises(httpx.HTTPStatusError):
        phala.fetch()
    assert len(requests) == 1
    assert phala._DISCOVERED_MANIFEST_ROWS == {}


@pytest.mark.parametrize(
    "payload",
    [None, [], {}, {"data": {}}, {"data": []}, {"data": [None]}, {"data": [{"id": MODEL_ID}]}],
)
def test_phala_invalid_or_unpriced_feed_cannot_replace_good_catalog(
    monkeypatch: pytest.MonkeyPatch, payload: Any, tmp_path: Path
) -> None:
    manifest_path = tmp_path / "phala.json"
    original = json.dumps({"provider": "phala", "models": [{"id": MODEL_ID}]})
    manifest_path.write_text(original)
    monkeypatch.setattr(phala, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(phala, "_DISCOVERED_MANIFEST_ROWS", {"stale": {}})
    mock_catalog(monkeypatch, payload=payload)
    with pytest.raises(RuntimeError, match="phala: /v1/models response"):
        phala.fetch()
    assert manifest_path.read_text() == original
    assert phala._DISCOVERED_MANIFEST_ROWS == {}


def test_phala_audit_and_committed_manifest_use_same_first_party_source() -> None:
    discovery = next(
        entry
        for entry in check_price_coverage._GLM_DISCOVERABLE_PROVIDER_APIS
        if entry[0] == "phala"
    )
    assert discovery == ("phala", CATALOG_URL, ())
    assert phala.URL == CATALOG_URL
    assert json.loads(phala.MANIFEST_PATH.read_text())["source"] == CATALOG_URL
