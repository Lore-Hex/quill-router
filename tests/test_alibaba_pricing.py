from __future__ import annotations

from typing import Any

import httpx
import pytest

from scripts.pricing.base import ModelPrice, PriceTier
from scripts.pricing.providers import alibaba


def test_qwen_37_flash_uses_published_context_tiers() -> None:
    expected = ModelPrice(
        tiers=[
            PriceTier(
                max_prompt_tokens=32_000,
                prompt_micro_per_m=30_000,
                completion_micro_per_m=130_000,
                prompt_cached_micro_per_m=6_000,
            ),
            PriceTier(
                max_prompt_tokens=256_000,
                prompt_micro_per_m=100_000,
                completion_micro_per_m=400_000,
                prompt_cached_micro_per_m=20_000,
            ),
            PriceTier(
                max_prompt_tokens=None,
                prompt_micro_per_m=200_000,
                completion_micro_per_m=800_000,
                prompt_cached_micro_per_m=40_000,
            ),
        ]
    )

    assert alibaba._price("qwen3.7-flash") == expected  # noqa: SLF001
    assert alibaba._price("qwen3.7-flash-2026-07-15") == expected  # noqa: SLF001


def test_qwen_37_flash_is_a_required_alibaba_model() -> None:
    assert "qwen/qwen3.7-flash" in alibaba.EXPECTED_MODELS


def test_qwen_37_flash_native_ids_are_canonicalized() -> None:
    assert alibaba._canonical_model_id("qwen3.7-flash") == "qwen/qwen3.7-flash"  # noqa: SLF001
    assert (  # noqa: SLF001
        alibaba._canonical_model_id("qwen3.7-flash-2026-07-15")
        == "qwen/qwen3.7-flash-2026-07-15"
    )


def test_fetch_discovers_flash_alias_and_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALIBABA_API_KEY", "test-key")

    class FakeClient:
        def __init__(self, **_: Any) -> None:
            pass

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def get(self, url: str, *, headers: dict[str, str]) -> httpx.Response:
            assert url == alibaba.URL
            assert headers["Authorization"] == "Bearer test-key"
            return httpx.Response(
                200,
                request=httpx.Request("GET", url),
                json={
                    "data": [
                        {"id": "qwen3.7-flash"},
                        {"id": "qwen3.7-flash-2026-07-15"},
                        {"id": "qwen-unknown-unpriced"},
                    ]
                },
            )

    monkeypatch.setattr(alibaba.httpx, "Client", FakeClient)
    monkeypatch.setattr(alibaba, "EXPECTED_MODELS", ["qwen/qwen3.7-flash"])

    result = alibaba.fetch()

    assert set(result.prices) == {
        "qwen/qwen3.7-flash",
        "qwen/qwen3.7-flash-2026-07-15",
    }
    assert alibaba.UPSTREAM_ID_MAP == {
        "qwen/qwen3.7-flash": "qwen3.7-flash",
        "qwen/qwen3.7-flash-2026-07-15": "qwen3.7-flash-2026-07-15",
    }
