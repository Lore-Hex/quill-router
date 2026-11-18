from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from scripts.pricing.providers import baseten
from trusted_router import provider_lifecycle

_CUTOFF = datetime(2026, 7, 25, 0, 0, tzinfo=UTC)
_RETIRING = {
    "z-ai/glm-5": "zai-org/GLM-5",
    "z-ai/glm-5.1": "zai-org/GLM-5.1",
    "moonshotai/kimi-k2.5": "moonshotai/Kimi-K2.5",
    "nvidia/nemotron-120b-a12b": "nvidia/Nemotron-120B-A12B",
}
_SUCCESSORS = {
    "z-ai/glm-5.2": "zai-org/GLM-5.2",
    "z-ai/glm-5.2-fast": "zai-org/GLM-5.2-Fast",
    "moonshotai/kimi-k2.6": "moonshotai/Kimi-K2.6",
    "moonshotai/kimi-k2.7-code": "moonshotai/Kimi-K2.7-Code",
    "nvidia/nemotron-3-ultra-550b-a55b": (
        "nvidia/NVIDIA-Nemotron-3-Ultra-550B-A55B"
    ),
}


def test_baseten_july_successors_are_not_retired() -> None:
    # The cutoff itself, scope and the refresh and catalog paths are checked
    # for every retirement in tests/test_provider_retirements_generic.py.
    for model_id, upstream_id in _SUCCESSORS.items():
        assert not provider_lifecycle.provider_model_retired(
            "baseten",
            model_id,
            upstream_id,
            at=_CUTOFF,
        )


def test_baseten_parser_filters_retired_rows_from_stale_feed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "data": [
            {
                "id": upstream_id,
                "pricing": {"input": "0.000001", "output": "0.000003"},
            }
            for upstream_id in {*_RETIRING.values(), *_SUCCESSORS.values()}
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

    monkeypatch.setattr(baseten.httpx, "Client", FakeClient)

    monkeypatch.setattr(
        provider_lifecycle,
        "_utc_now",
        lambda: _CUTOFF - timedelta(microseconds=1),
    )
    before = baseten.fetch()
    assert set(_RETIRING).issubset(before.prices)

    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)
    after = baseten.fetch()
    assert set(_RETIRING).isdisjoint(after.prices)
    assert set(_RETIRING).isdisjoint(baseten._DISCOVERED_MANIFEST_ROWS)
    assert set(_SUCCESSORS).issubset(after.prices)


def test_baseten_native_mappings_match_announced_routes() -> None:
    for model_id, upstream_id in _RETIRING.items():
        assert baseten.UPSTREAM_ID_MAP[model_id] == upstream_id
    for model_id, upstream_id in _SUCCESSORS.items():
        assert baseten.UPSTREAM_ID_MAP[model_id] == upstream_id
