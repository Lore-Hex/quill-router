from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from scripts.pricing import refresh
from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.providers import friendli
from trusted_router import provider_lifecycle
from trusted_router.catalog import endpoints_for_model

_QWEN_CUTOFF = datetime(2026, 8, 5, 0, 0, tzinfo=UTC)
_EXAONE_CUTOFF = datetime(2026, 8, 20, 0, 0, tzinfo=UTC)
_QWEN = "qwen/qwen3-235b-a22b-2507"
_QWEN_UPSTREAM = "Qwen/Qwen3-235B-A22B-Instruct-2507"
_EXAONE = "lgai-exaone/k-exaone-236b-a23b"
_EXAONE_UPSTREAM = "LGAI-EXAONE/K-EXAONE-236B-A23B"
_GLM = "z-ai/glm-5.2"


def test_friendli_qwen_retires_at_announced_instant() -> None:
    assert not provider_lifecycle.provider_model_retired(
        "friendli",
        _QWEN,
        _QWEN_UPSTREAM,
        at=_QWEN_CUTOFF - timedelta(microseconds=1),
    )
    assert provider_lifecycle.provider_model_retired(
        "friendli",
        _QWEN,
        _QWEN_UPSTREAM,
        at=_QWEN_CUTOFF,
    )
    assert not provider_lifecycle.provider_model_retired(
        "friendli",
        _GLM,
        "zai-org/GLM-5.2",
        at=_QWEN_CUTOFF,
    )


def test_friendli_exaone_retires_at_announced_instant() -> None:
    assert not provider_lifecycle.provider_model_retired(
        "friendli",
        _EXAONE,
        _EXAONE_UPSTREAM,
        at=_EXAONE_CUTOFF - timedelta(microseconds=1),
    )
    assert provider_lifecycle.provider_model_retired(
        "friendli",
        _EXAONE,
        _EXAONE_UPSTREAM,
        at=_EXAONE_CUTOFF,
    )
    assert not provider_lifecycle.provider_model_retired(
        "another-provider",
        _EXAONE,
        _EXAONE_UPSTREAM,
        at=_EXAONE_CUTOFF,
    )


def test_friendli_qwen_retirement_is_provider_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _QWEN_CUTOFF)

    providers = {endpoint.provider for endpoint in endpoints_for_model(_QWEN)}

    assert "friendli" not in providers
    assert {"atlas-cloud", "crusoe"}.issubset(providers)


def test_friendli_exaone_catalog_route_retires_on_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        provider_lifecycle,
        "_utc_now",
        lambda: _EXAONE_CUTOFF - timedelta(microseconds=1),
    )
    assert "friendli" in {
        endpoint.provider for endpoint in endpoints_for_model(_EXAONE)
    }

    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _EXAONE_CUTOFF)
    assert "friendli" not in {
        endpoint.provider for endpoint in endpoints_for_model(_EXAONE)
    }


def test_hourly_refresh_cannot_restore_retired_friendli_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = ProviderPricingResult(
        slug="friendli",
        prices={
            _QWEN: ModelPrice(200_000, 800_000),
            _EXAONE: ModelPrice(200_000, 800_000),
            _GLM: ModelPrice(1_400_000, 4_400_000),
        },
        source="api",
        fetched_url=friendli.URL,
    )

    monkeypatch.setattr(
        provider_lifecycle,
        "_utc_now",
        lambda: _QWEN_CUTOFF - timedelta(microseconds=1),
    )
    before = refresh._index_provider_prices({"friendli": result})
    assert "friendli" in before[_QWEN]
    assert "friendli" in before[_EXAONE]

    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _QWEN_CUTOFF)
    after_qwen = refresh._index_provider_prices({"friendli": result})
    assert _QWEN not in after_qwen
    assert "friendli" in after_qwen[_EXAONE]
    assert "friendli" in after_qwen[_GLM]

    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _EXAONE_CUTOFF)
    after_exaone = refresh._index_provider_prices({"friendli": result})
    assert _QWEN not in after_exaone
    assert _EXAONE not in after_exaone
    assert "friendli" in after_exaone[_GLM]


def test_friendli_parser_drops_retired_model_even_if_feed_is_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "data": [
            {
                "id": _QWEN_UPSTREAM,
                "pricing": {"input": "0.0000002", "output": "0.0000008"},
            },
            {
                "id": _EXAONE_UPSTREAM,
                "pricing": {"input": "0.0000002", "output": "0.0000008"},
            },
            {
                "id": "zai-org/GLM-5.2",
                "pricing": {"input": "0.0000014", "output": "0.0000044"},
            },
        ]
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

    monkeypatch.setattr(friendli.httpx, "Client", FakeClient)

    monkeypatch.setattr(
        provider_lifecycle,
        "_utc_now",
        lambda: _QWEN_CUTOFF - timedelta(microseconds=1),
    )
    before = friendli.fetch()
    assert _QWEN in before.prices
    assert _EXAONE in before.prices

    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _QWEN_CUTOFF)
    after_qwen = friendli.fetch()
    assert _QWEN not in after_qwen.prices
    assert _QWEN not in friendli._DISCOVERED_MANIFEST_ROWS
    assert _EXAONE in after_qwen.prices
    assert _GLM in after_qwen.prices

    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _EXAONE_CUTOFF)
    after_exaone = friendli.fetch()
    assert _QWEN not in after_exaone.prices
    assert _EXAONE not in after_exaone.prices
    assert _EXAONE not in friendli._DISCOVERED_MANIFEST_ROWS
    assert _GLM in after_exaone.prices


def test_friendli_qwen_native_mapping_matches_announced_route() -> None:
    assert friendli.UPSTREAM_ID_MAP[_QWEN] == _QWEN_UPSTREAM


def test_friendli_exaone_native_mapping_matches_announced_route() -> None:
    assert friendli.UPSTREAM_ID_MAP[_EXAONE] == _EXAONE_UPSTREAM
