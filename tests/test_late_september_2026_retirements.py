from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from scripts.pricing import refresh
from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.providers import openai, together
from trusted_router import catalog, provider_lifecycle
from trusted_router.catalog_data import ModelEndpoint

_OPENAI_CUTOFF = datetime(2026, 9, 28, tzinfo=UTC)
_TOGETHER_CUTOFF = datetime(2026, 9, 29, tzinfo=UTC)
_OPENAI_NATIVE = (
    "gpt-3.5-turbo-instruct", "babbage-002", "davinci-002", "gpt-3.5-turbo-1106",
)
_CASES = [
    *(('openai', f'openai/{native}', native, _OPENAI_CUTOFF) for native in _OPENAI_NATIVE),
    ('together', 'deepseek/deepseek-v4-flash-0731',
     'deepseek-ai/DeepSeek-V4-Flash-0731', _TOGETHER_CUTOFF),
    ('wafer', 'moonshotai/kimi-k2.6', 'Kimi-K2.6', datetime(2026, 9, 18, 19, tzinfo=UTC)),
]
_REPLACEMENT = "deepseek/deepseek-v4.1-flash"
_REPLACEMENT_NATIVE = "deepseek-ai/DeepSeek-V4.1-Flash"


@pytest.mark.parametrize(("provider", "model", "native", "cutoff"), _CASES)
def test_retirement_cutoff_preserves_other_providers_and_model_identity(
    monkeypatch: pytest.MonkeyPatch, provider: str, model: str, native: str, cutoff: datetime,
) -> None:
    retired = provider_lifecycle.provider_model_retired
    before = cutoff - timedelta(microseconds=1)
    assert not retired(provider, model, native, at=before)
    assert retired(provider, model, at=cutoff)
    assert retired(provider, "unknown-canonical", native, at=cutoff)
    assert not retired("unaffected-provider", model, native, at=cutoff)
    endpoints = {}
    for slug in (provider, "unaffected-provider"):
        for usage in ("Credits", "BYOK"):
            endpoint = ModelEndpoint(
                id=f"{model}@{slug}/{usage}", model_id=model, provider=slug,
                usage_type=usage, upstream_id=native,
            )
            endpoints[endpoint.id] = endpoint
    monkeypatch.setattr(catalog, "MODEL_ENDPOINTS", endpoints)
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: before)
    assert len(catalog.endpoints_for_model(model)) == 4
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: cutoff)
    remaining = catalog.endpoints_for_model(model)
    assert len(remaining) == 2
    assert {row.provider for row in remaining} == {"unaffected-provider"}
    assert {row.model_id for row in remaining} == {model}
    result = ProviderPricingResult(
        slug=provider, source="api", fetched_url="https://example.com/models",
        prices={model: ModelPrice(100_000, 200_000)},
    )
    assert not refresh._index_provider_prices({provider: result})


def test_openai_discovery_does_not_probe_or_republish_retired_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _OPENAI_CUTOFF)
    monkeypatch.setattr(openai, "MANIFEST_PATH", tmp_path / "openai.json")
    monkeypatch.setattr(openai, "UPSTREAM_ID_MAP", {})
    monkeypatch.setattr(openai, "_DISCOVERED_MANIFEST_ROWS", {})
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    prices = {f"openai/{native}": ModelPrice(100_000, 200_000)
              for native in (*_OPENAI_NATIVE, "gpt-5.6-terra")}
    monkeypatch.setattr(openai, "fetch_provider", lambda **_: ProviderPricingResult(
        slug="openai", source="api", fetched_url=openai.URL, prices=prices,
    ))
    monkeypatch.setattr(openai, "runtime_required_models", lambda _: set())
    monkeypatch.setattr(openai, "fetch_json", lambda *_, **__: {
        "data": [{"id": native} for native in (*_OPENAI_NATIVE, "gpt-5.6-terra")],
    })
    probes = []

    def probe(**kwargs: object) -> bool:
        probes.append(kwargs["model"])
        return True

    monkeypatch.setattr(openai, "probe_openai_chat", probe)
    result = openai.fetch()
    openai.write_provider_manifest(result)
    assert probes == ["gpt-5.6-terra"]
    assert set(openai._DISCOVERED_MANIFEST_ROWS) == {"openai/gpt-5.6-terra"}
    assert set(refresh._index_provider_prices({"openai": result})) == {"openai/gpt-5.6-terra"}
    assert openai._is_stable_chat_model({"id": "gpt-3.5-turbo-0125"})


@pytest.mark.parametrize("after_cutoff", [False, True])
def test_together_refresh_probes_replacement_missing_from_endpoint_feed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, after_cutoff: bool,
) -> None:
    _, old, old_native, _ = next(case for case in _CASES if case[0] == "together")
    probes = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            model = json.loads(request.content)["model"]
            probes.append(model)
            assert model == _REPLACEMENT_NATIVE
            return httpx.Response(200, json={"choices": [{"message": {"content": "PONG"}}]})
        if str(request.url) == together.URL:
            return httpx.Response(200, json=[
                {"id": native, "type": "chat", "context_length": 1048576,
                 "pricing": {"input": "0.3", "output": "1.2", "cached_input": "0.006"}}
                for native in (old_native, _REPLACEMENT_NATIVE)
            ])
        assert str(request.url) == together.SERVERLESS_ENDPOINTS_URL
        return httpx.Response(200, json={"data": [
            {"model": old_native, "type": "serverless", "state": "STARTED"},
        ]})

    monkeypatch.setattr(together.httpx, "HTTPTransport", lambda **_: httpx.MockTransport(respond))
    monkeypatch.setattr(together, "UPSTREAM_ID_MAP", dict(together.UPSTREAM_ID_MAP))
    monkeypatch.setattr(together, "_DISCOVERED_MANIFEST_ROWS", {})
    monkeypatch.setattr(together, "MANIFEST_PATH", tmp_path / "together.json")
    monkeypatch.setattr(together, "EXPECTED_MODELS", [_REPLACEMENT])
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: (
        _TOGETHER_CUTOFF if after_cutoff else _TOGETHER_CUTOFF - timedelta(microseconds=1)
    ))
    result = together.fetch()
    together.write_provider_manifest(result)
    assert probes == [_REPLACEMENT_NATIVE]
    assert set(result.prices) == ({_REPLACEMENT} if after_cutoff else {_REPLACEMENT, old})
    assert result.prices[_REPLACEMENT].tiers[0].prompt_cached_micro_per_m == 6_000
    assert together._DISCOVERED_MANIFEST_ROWS[_REPLACEMENT]["context_length"] == 1048576


def test_manifests_publish_dates_without_reviving_delisted_models() -> None:
    for module, model, cutoff, replacement in (
        (openai, "openai/gpt-3.5-turbo-1106", "2026-09-28T00:00:00Z", "openai/gpt-5.6-terra"),
        (together, "deepseek/deepseek-v4-flash-0731", "2026-09-29T00:00:00Z", _REPLACEMENT),
    ):
        rows = {row["id"]: row for row in json.loads(module.MANIFEST_PATH.read_text())["models"]}
        assert rows[model]["retirement_at"] == cutoff
        assert rows[model]["replacement_model_id"] == replacement
    assert rows[model]["routable"] is False
