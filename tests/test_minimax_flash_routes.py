from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from scripts.pricing.providers import gmi, novita, together, venice
from trusted_router import provider_lifecycle
from trusted_router.catalog import MODELS, endpoints_for_model
from trusted_router.catalog_data import _PROVIDER_UNSERVED_CREDITS_MODELS
from trusted_router.request_capabilities import endpoint_capabilities
from trusted_router.routing import canonical_model_id

MODEL = "minimax/minimax-m3.1-flash"
ALIAS = "minimax/minimax-m3-1-flash"
NATIVE = "MiniMaxAI/MiniMax-M3.1-Flash"


def test_flash_has_verified_prepaid_routes_and_preserves_native_ids() -> None:
    routes = {e.provider: e for e in endpoints_for_model(MODEL) if e.usage_type == "Credits"}
    # The 2026-10-25 refresh added six prepaid hosts that price and canary
    # the launch model; each keeps its own native id.
    assert {p: e.upstream_id for p, e in routes.items()} == {
        "boundless": MODEL,  # Boundless lists the canonical id at the launch price
        "featherless": NATIVE,  # Featherless serves the HF id with tools
        "gmi": NATIVE,
        "minimax": "MiniMax-M3.1-Flash",
        "nebius": NATIVE,  # Nebius serves the HF id at the launch price
        "novita": MODEL,
        "nvidia-nim": "minimaxai/minimax-m3.1-flash",  # NIM lowercases the HF id
        "parasail": NATIVE,
        "together": NATIVE,
        "venice": "minimax-m3-1-flash",
        "wafer": "MiniMax-M3.1-Flash",  # Wafer serves MiniMax's own id
        "zro": "minimax-m3.1-flash",  # ZRO serves the bare model name
    }
    # NIM declares no tool support and carries NVIDIA's conservative hosted
    # rate, so it never wins cheapest-route selection for this model.
    assert {p: endpoint_capabilities(MODELS[MODEL], e)["tools"] for p, e in routes.items()} == {
        p: p != "nvidia-nim" for p in routes
    }
    nim_prompt = routes["nvidia-nim"].price_tiers[0].prompt_price_microdollars_per_million_tokens
    assert all(
        e.price_tiers[0].prompt_price_microdollars_per_million_tokens < nim_prompt
        for p, e in routes.items() if p != "nvidia-nim"
    )
    assert MODEL not in _PROVIDER_UNSERVED_CREDITS_MODELS["novita"]
    assert MODEL in gmi._VERIFIED_PREPAID_MODELS
    assert any(e.provider == "gmi" and e.usage_type == "BYOK" for e in endpoints_for_model(MODEL))


def test_novita_flash_uses_account_price_units() -> None:
    row = next(row for row in json.loads(novita.MANIFEST_PATH.read_text())["models"] if row["id"] == MODEL)
    price = novita._source_price(row)
    assert price is not None
    assert (price.prompt_micro_per_m, price.completion_micro_per_m, price.tiers[0].prompt_cached_micro_per_m) == (
        150000, 600000, 15000,
    )


def test_venice_flash_refresh_and_legacy_requests_share_one_model(client: TestClient) -> None:
    assert venice._model_id("minimax-m3-1-flash") == MODEL
    assert ALIAS not in MODELS
    assert canonical_model_id(ALIAS) == MODEL
    assert canonical_model_id(ALIAS + ":nitro") == MODEL
    assert client.get(f"/v1/models/{ALIAS}/endpoints").json() == client.get(f"/v1/models/{MODEL}/endpoints").json()
    response = client.get(f"/models/{ALIAS}", follow_redirects=False)
    assert response.status_code == 301
    assert response.headers["location"] == f"/models/{MODEL}"


@pytest.mark.parametrize("probe_status", [200, 404, 429])
def test_together_flash_refresh_checks_missing_serverless_feed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, probe_status: int,
) -> None:
    # Probe mechanics, not lifecycle: Together retires MiniMax M3 on 2026-11-06.
    monkeypatch.setattr(
        provider_lifecycle, "_utc_now", lambda: datetime(2026, 11, 5, tzinfo=UTC),
    )
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json=[{
                "id": "MiniMaxAI/MiniMax-M3", "type": "chat",
                "pricing": {"input": 0.3, "output": 1.2},
            }, {
                "id": NATIVE, "type": "chat", "running": False,
                "context_length": 1048576,
                "pricing": {"input": 0.15, "output": 0.6, "cached_input": 0.015},
            }])
        if request.url.path == "/v1/endpoints":
            return httpx.Response(200, json={"data": [{
                "model": "MiniMaxAI/MiniMax-M3", "type": "serverless", "state": "STARTED",
            }]})
        assert request.method == "POST"
        payload = json.loads(request.content)
        assert payload["model"] == NATIVE
        assert payload["max_tokens"] == 1
        calls.append(payload["model"])
        return httpx.Response(probe_status, json={"error": {"code": "model_not_available"}})

    client_class = httpx.Client

    def client(**kwargs: Any) -> httpx.Client:
        kwargs["transport"] = httpx.MockTransport(handle)
        return client_class(**kwargs)

    monkeypatch.setenv("TOGETHER_API_KEY", "test")
    monkeypatch.setattr(together.httpx, "Client", client)
    monkeypatch.setattr(together, "_DISCOVERED_MANIFEST_ROWS", {})
    monkeypatch.setattr(together, "UPSTREAM_ID_MAP", dict(together.UPSTREAM_ID_MAP))
    manifest = tmp_path / "together.json"
    monkeypatch.setattr(together, "MANIFEST_PATH", manifest)
    if probe_status == 429:
        with pytest.raises(RuntimeError, match="ambiguous availability"):
            together.fetch()
    else:
        result = together.fetch()
        assert (MODEL in result.prices) is (probe_status == 200)
        if probe_status == 200:
            price = result.prices[MODEL]
            assert (price.prompt_micro_per_m, price.completion_micro_per_m) == (150000, 600000)
            assert price.tiers[0].prompt_cached_micro_per_m == 15000
            together.write_provider_manifest(result)
            row = next(row for row in json.loads(manifest.read_text())["models"] if row["id"] == MODEL)
            assert row["id"] == MODEL
            assert row["upstream_id"] == NATIVE
    assert calls == [NATIVE]
