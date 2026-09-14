from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from scripts.pricing import refresh
from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.providers import together
from trusted_router import catalog, catalog_ingest, provider_lifecycle
from trusted_router.catalog_data import ModelEndpoint

_CUTOFF = datetime(2026, 9, 14, tzinfo=UTC)
_RETIRING = (
    ("google/gemma-4-31b-it", "google/gemma-4-31B-it"),
    ("openai/gpt-oss-20b", "openai/gpt-oss-20b"),
    ("thinkingmachines/inkling-small", "thinkingmachines/Inkling-Small"),
    ("intfloat/multilingual-e5-large-instruct", "intfloat/multilingual-e5-large-instruct"),
)
_REPLACEMENT = "z-ai/glm-5.3-flash"
_REPLACEMENT_NATIVE = "zai-org/GLM-5.3-Flash"


def test_together_manifest_records_migrations_and_verified_replacement() -> None:
    rows = {row["id"]: row for row in json.loads(together.MANIFEST_PATH.read_text())["models"]}
    for model_id, replacement in (
        ("google/gemma-4-31b-it", _REPLACEMENT),
        ("openai/gpt-oss-20b", "qwen/qwen3.5-9b"),
    ):
        assert rows[model_id]["retirement_at"] == "2026-09-14T00:00:00Z"
        assert rows[model_id]["replacement_model_id"] == replacement
    assert _REPLACEMENT in together.EXPECTED_MODELS
    assert together.UPSTREAM_ID_MAP[_REPLACEMENT] == _REPLACEMENT_NATIVE
    assert rows[_REPLACEMENT]["upstream_id"] == _REPLACEMENT_NATIVE


@pytest.mark.parametrize(("model_id", "native_id"), _RETIRING)
def test_together_september_cutoff_and_provider_scope(model_id: str, native_id: str) -> None:
    assert provider_lifecycle.TOGETHER_SEPTEMBER_2026_RETIREMENT_AT == _CUTOFF
    retired = provider_lifecycle.provider_model_retired
    assert not retired("together", model_id, native_id, at=_CUTOFF - timedelta(microseconds=1))
    assert retired("together", model_id, at=_CUTOFF)
    assert retired("together", "unknown-canonical-id", native_id, at=_CUTOFF)
    assert not retired("deepinfra", model_id, native_id, at=_CUTOFF)
    assert not retired("together", _REPLACEMENT, _REPLACEMENT_NATIVE, at=_CUTOFF)
    assert not retired("together", "qwen/qwen3.5-9b", "Qwen/Qwen3.5-9B", at=_CUTOFF)


@pytest.mark.parametrize(("model_id", "native_id"), _RETIRING)
def test_together_runtime_cutoff_preserves_model_identity(
    monkeypatch: pytest.MonkeyPatch, model_id: str, native_id: str,
) -> None:
    endpoints = {}
    for provider in ("together", "deepinfra"):
        for usage in ("Credits", "BYOK"):
            endpoint = ModelEndpoint(
                id=f"{model_id}@{provider}/{usage}", model_id=model_id,
                provider=provider, usage_type=usage, upstream_id=native_id,
            )
            endpoints[endpoint.id] = endpoint
    monkeypatch.setattr(catalog, "MODEL_ENDPOINTS", endpoints)
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF - timedelta(microseconds=1))
    assert len(catalog.endpoints_for_model(model_id)) == 4
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)
    after = catalog.endpoints_for_model(model_id)
    assert len(after) == 2
    assert {endpoint.provider for endpoint in after} == {"deepinfra"}
    assert {endpoint.model_id for endpoint in after} == {model_id}


def test_together_stale_prices_cannot_restore_retired_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    retired = {model_id for model_id, _ in _RETIRING}
    result = ProviderPricingResult(
        slug="together", source="api", fetched_url=together.URL,
        prices={model_id: ModelPrice(150_000, 500_000) for model_id in retired | {_REPLACEMENT}},
    )
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF - timedelta(microseconds=1))
    assert set(refresh._index_provider_prices({"together": result})) == retired | {_REPLACEMENT}
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)
    assert set(refresh._index_provider_prices({"together": result})) == {_REPLACEMENT}


@pytest.mark.parametrize("manifest", [None, "broken", '{"models": []}'])
def test_static_embedding_allowlist_obeys_retirement_even_without_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, manifest: str | None,
) -> None:
    model_id, native_id = _RETIRING[-1]
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", tmp_path)
    monkeypatch.setattr(catalog_ingest, "_EMBEDDING_SPECS", [
        {"id": model_id, "upstream_id": native_id, "provider": provider}
        for provider in ("together", "deepinfra")
    ])
    if manifest is not None:
        (tmp_path / "together.json").write_text(manifest)
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF - timedelta(microseconds=1))
    assert catalog_ingest._authoritative_provider_model_ids("together") == {model_id}
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)
    assert not catalog_ingest._authoritative_provider_model_ids("together")
    assert catalog_ingest._authoritative_provider_model_ids("deepinfra") == {model_id}


@pytest.mark.parametrize("after_cutoff", [False, True])
def test_together_stale_started_feed_and_callable_replacement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, after_cutoff: bool,
) -> None:
    # Embeddings are not ingested by the chat parser, but the runtime rule
    # above also protects old embedding endpoints already in a snapshot.
    native_ids = [native_id for _, native_id in _RETIRING[:-1]]
    probes = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            model = json.loads(request.content)["model"]
            probes.append(model)
            assert model == _REPLACEMENT_NATIVE
            return httpx.Response(200, json={"choices": [{"message": {"content": "PONG"}}]})
        if str(request.url) == together.URL:
            return httpx.Response(200, json=[
                {"id": native, "type": "chat", "context_length": 131072,
                 "pricing": {"input": "0.15", "output": "0.5", "cached_input": "0.03"}}
                for native in [*native_ids, _REPLACEMENT_NATIVE]
            ])
        assert str(request.url) == together.SERVERLESS_ENDPOINTS_URL
        return httpx.Response(200, json={"data": [
            {"model": native, "type": "serverless",
             "state": "STOPPED" if native == _REPLACEMENT_NATIVE else "STARTED"}
            for native in [*native_ids, _REPLACEMENT_NATIVE]
        ]})

    monkeypatch.setenv("TOGETHER_API_KEY", "test-key")
    monkeypatch.setattr(together.httpx, "HTTPTransport", lambda **_: httpx.MockTransport(respond))
    monkeypatch.setattr(together, "UPSTREAM_ID_MAP", dict(together.UPSTREAM_ID_MAP))
    monkeypatch.setattr(together, "_DISCOVERED_MANIFEST_ROWS", {})
    monkeypatch.setattr(together, "MANIFEST_PATH", tmp_path / "together.json")
    # Only test the replacement and a retired expected model. Other expected
    # models have independent feed/probe coverage in test_together_pricing.
    monkeypatch.setattr(together, "EXPECTED_MODELS", [_REPLACEMENT, _RETIRING[0][0]])
    together.UPSTREAM_ID_MAP[_RETIRING[0][0]] = _RETIRING[0][1]
    monkeypatch.setattr(
        provider_lifecycle, "_utc_now",
        lambda: _CUTOFF if after_cutoff else _CUTOFF - timedelta(microseconds=1),
    )
    result = together.fetch()
    expected = {_REPLACEMENT}
    if not after_cutoff:
        expected.update(model_id for model_id, _ in _RETIRING[:-1])
    assert set(result.prices) == expected
    assert set(together._DISCOVERED_MANIFEST_ROWS) == expected
    assert result.prices[_REPLACEMENT].tiers[0].prompt_cached_micro_per_m == 30_000
    assert probes == [_REPLACEMENT_NATIVE]
    together.write_provider_manifest(result)
    rows = json.loads(together.MANIFEST_PATH.read_text())["models"]
    assert {row["id"] for row in rows} == expected
