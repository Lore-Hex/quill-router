from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from scripts.pricing import refresh
from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.providers import deepinfra
from trusted_router import provider_lifecycle
from trusted_router.catalog import MODEL_ENDPOINTS, endpoints_for_model
from trusted_router.catalog_data import ModelEndpoint

_CUTOFF = datetime(2026, 8, 17, 0, 0, tzinfo=UTC)
_TERMINUS = "deepseek/deepseek-v3.1-terminus"
_TERMINUS_UPSTREAM = "deepseek-ai/DeepSeek-V3.1-Terminus"
_FLASH = "deepseek/deepseek-v4-flash-0731"
_FLASH_UPSTREAM = "deepseek-ai/DeepSeek-V4-Flash-0731"
_QWEN_CUTOFF = datetime(2026, 8, 24, 0, 0, tzinfo=UTC)
_QWEN_THINKING = "qwen/qwen3-235b-a22b-thinking-2507"
_QWEN_THINKING_UPSTREAM = "Qwen/Qwen3-235B-A22B-Thinking-2507"
_QWEN_REPLACEMENT = "qwen/qwen3.6-35b-a3b"
_QWEN_REPLACEMENT_UPSTREAM = "Qwen/Qwen3.6-35B-A3B"


def test_deepinfra_terminus_retires_at_announced_date() -> None:
    assert not provider_lifecycle.provider_model_retired(
        "deepinfra",
        _TERMINUS,
        _TERMINUS_UPSTREAM,
        at=_CUTOFF - timedelta(microseconds=1),
    )
    assert provider_lifecycle.provider_model_retired(
        "deepinfra",
        _TERMINUS,
        _TERMINUS_UPSTREAM,
        at=_CUTOFF,
    )
    assert not provider_lifecycle.provider_model_retired(
        "deepinfra",
        _FLASH,
        _FLASH_UPSTREAM,
        at=_CUTOFF,
    )


def test_deepinfra_terminus_retirement_is_provider_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Provider discovery may remove or add routes independently of an announced
    # cutoff. Use a self-contained catalog so this test covers only the
    # provider-scoped lifecycle rule.
    for endpoint_id, endpoint in tuple(MODEL_ENDPOINTS.items()):
        if endpoint.model_id == _TERMINUS:
            monkeypatch.delitem(MODEL_ENDPOINTS, endpoint_id)
    for provider, upstream_id in (
        ("deepinfra", _TERMINUS_UPSTREAM),
        ("novita", "deepseek/deepseek-v3.1-terminus"),
    ):
        endpoint = ModelEndpoint(
            id=f"{_TERMINUS}@{provider}/prepaid",
            model_id=_TERMINUS,
            provider=provider,
            usage_type="Credits",
            upstream_id=upstream_id,
        )
        monkeypatch.setitem(MODEL_ENDPOINTS, endpoint.id, endpoint)

    monkeypatch.setattr(
        provider_lifecycle,
        "_utc_now",
        lambda: _CUTOFF - timedelta(microseconds=1),
    )
    before = {endpoint.provider for endpoint in endpoints_for_model(_TERMINUS)}
    assert before == {"deepinfra", "novita"}

    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)
    after = {endpoint.provider for endpoint in endpoints_for_model(_TERMINUS)}

    assert after == {"novita"}


def test_hourly_refresh_cannot_restore_retired_deepinfra_terminus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = ProviderPricingResult(
        slug="deepinfra",
        prices={
            _TERMINUS: ModelPrice(270_000, 950_000),
            _FLASH: ModelPrice(80_000, 180_000),
        },
        source="api",
        fetched_url="https://api.deepinfra.com/v1/openai/models",
    )

    monkeypatch.setattr(
        provider_lifecycle,
        "_utc_now",
        lambda: _CUTOFF - timedelta(microseconds=1),
    )
    before = refresh._index_provider_prices({"deepinfra": result})
    assert "deepinfra" in before[_TERMINUS]

    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)
    after = refresh._index_provider_prices({"deepinfra": result})

    assert _TERMINUS not in after
    assert "deepinfra" in after[_FLASH]


def test_deepinfra_qwen_thinking_retires_at_announced_date() -> None:
    assert not provider_lifecycle.provider_model_retired(
        "deepinfra",
        _QWEN_THINKING,
        _QWEN_THINKING_UPSTREAM,
        at=_QWEN_CUTOFF - timedelta(microseconds=1),
    )
    assert provider_lifecycle.provider_model_retired(
        "deepinfra",
        _QWEN_THINKING,
        _QWEN_THINKING_UPSTREAM,
        at=_QWEN_CUTOFF,
    )
    assert not provider_lifecycle.provider_model_retired(
        "deepinfra",
        _QWEN_REPLACEMENT,
        _QWEN_REPLACEMENT_UPSTREAM,
        at=_QWEN_CUTOFF,
    )


def test_deepinfra_qwen_thinking_retirement_is_provider_scoped() -> None:
    assert provider_lifecycle.provider_model_retired(
        "deepinfra",
        _QWEN_THINKING,
        _QWEN_THINKING_UPSTREAM,
        at=_QWEN_CUTOFF,
    )
    for provider in ("alibaba", "chutes", "novita", "venice"):
        assert not provider_lifecycle.provider_model_retired(
            provider,
            _QWEN_THINKING,
            at=_QWEN_CUTOFF,
        )


def test_hourly_refresh_cannot_restore_retired_deepinfra_qwen_thinking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = ProviderPricingResult(
        slug="deepinfra",
        prices={
            _QWEN_THINKING: ModelPrice(230_000, 2_300_000),
            _QWEN_REPLACEMENT: ModelPrice(80_000, 240_000),
        },
        source="api",
        fetched_url="https://api.deepinfra.com/v1/openai/models",
    )

    monkeypatch.setattr(
        provider_lifecycle,
        "_utc_now",
        lambda: _QWEN_CUTOFF - timedelta(microseconds=1),
    )
    before = refresh._index_provider_prices({"deepinfra": result})
    assert "deepinfra" in before[_QWEN_THINKING]

    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _QWEN_CUTOFF)
    after = refresh._index_provider_prices({"deepinfra": result})

    assert _QWEN_THINKING not in after
    assert "deepinfra" in after[_QWEN_REPLACEMENT]


def test_deepinfra_parser_filters_retired_qwen_from_stale_feed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def row(native_id: str) -> dict[str, object]:
        return {
            "id": native_id,
            "metadata": {
                "pricing": {"input_tokens": 0.23, "output_tokens": 2.3}
            },
        }

    payload = {
        "data": [row(_QWEN_THINKING_UPSTREAM), row(_QWEN_REPLACEMENT_UPSTREAM)]
    }

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return payload

    class FakeClient:
        def __init__(self, **_kwargs: object) -> None:
            return None

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def get(self, *_args: object, **_kwargs: object) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr(deepinfra.httpx, "Client", FakeClient)
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _QWEN_CUTOFF)

    result = deepinfra.fetch()

    assert _QWEN_THINKING not in result.prices
    assert _QWEN_THINKING not in deepinfra._DISCOVERED_MANIFEST_ROWS
    assert _QWEN_REPLACEMENT in result.prices
    assert _QWEN_REPLACEMENT in deepinfra._DISCOVERED_MANIFEST_ROWS


def test_deepinfra_manifest_records_announced_qwen_replacement() -> None:
    rows = {
        row["id"]: row
        for row in json.loads(deepinfra.MANIFEST_PATH.read_text())["models"]
    }

    retired = rows[_QWEN_THINKING]
    assert retired["retirement_at"] == "2026-08-24T00:00:00Z"
    assert retired["replacement_model_id"] == _QWEN_REPLACEMENT
