from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from scripts.pricing.providers import deepinfra
from trusted_router import provider_lifecycle

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


def test_deepinfra_august_retirements_spare_their_successors() -> None:
    # The cutoffs themselves, scope and the refresh and catalog paths are
    # checked for every retirement in tests/test_provider_retirements_generic.py.
    assert not provider_lifecycle.provider_model_retired(
        "deepinfra",
        _FLASH,
        _FLASH_UPSTREAM,
        at=_CUTOFF,
    )
    assert not provider_lifecycle.provider_model_retired(
        "deepinfra",
        _QWEN_REPLACEMENT,
        _QWEN_REPLACEMENT_UPSTREAM,
        at=_QWEN_CUTOFF,
    )


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
