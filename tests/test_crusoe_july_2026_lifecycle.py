from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from scripts.pricing.providers import crusoe
from trusted_router import provider_lifecycle

_CUTOFF = datetime(2026, 7, 28, 18, 0, tzinfo=UTC)
_NEMOTRON = "nvidia/nemotron-3-ultra-550b"
_NEMOTRON_UPSTREAM = "nvidia/NVIDIA-Nemotron-3-Ultra-550B"
_GLM = "z-ai/glm-5.2"


def test_crusoe_nemotron_retirement_spares_glm() -> None:
    # The cutoff itself, scope and the refresh and catalog paths are checked
    # for every retirement in tests/test_provider_retirements_generic.py.
    assert not provider_lifecycle.provider_model_retired(
        "crusoe",
        _GLM,
        "zai/GLM-5.2",
        at=_CUTOFF,
    )


def test_crusoe_parser_drops_retired_model_even_if_feed_is_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "data": [
            {
                "id": _NEMOTRON_UPSTREAM,
                "pricing": {"prompt": "1.00", "completion": "3.20"},
            },
            {
                "id": "zai/GLM-5.2",
                "pricing": {"prompt": "1.40", "completion": "4.40"},
            },
            {
                "id": "deepseek-ai/Deepseek-V4-Flash",
                "pricing": {"prompt": "0.14", "completion": "0.28"},
            },
            {
                "id": "moonshotai/Kimi-K2.6",
                "pricing": {"prompt": "0.70", "completion": "3.50"},
            },
            {
                "id": "openai/gpt-oss-120b",
                "pricing": {"prompt": "0.05", "completion": "0.25"},
            },
        ]
    }

    class FakeResponse:
        status_code = 200

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

    monkeypatch.setattr(crusoe.httpx, "Client", FakeClient)

    monkeypatch.setattr(
        provider_lifecycle,
        "_utc_now",
        lambda: _CUTOFF - timedelta(microseconds=1),
    )
    before = crusoe.fetch()
    assert _NEMOTRON in before.prices

    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: _CUTOFF)
    after = crusoe.fetch()
    assert _NEMOTRON not in after.prices
    assert _NEMOTRON not in crusoe._DISCOVERED_MANIFEST_ROWS
    assert _GLM in after.prices


def test_crusoe_nemotron_native_mapping_matches_announced_route() -> None:
    assert crusoe.UPSTREAM_ID_MAP[_NEMOTRON] == _NEMOTRON_UPSTREAM
