from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from trusted_router import wafer_policy


@pytest.fixture(autouse=True)
def _clear_wafer_policy_cache() -> Iterator[None]:
    wafer_policy._wafer_zdr_index.cache_clear()
    yield
    wafer_policy._wafer_zdr_index.cache_clear()


def _set_manifest(monkeypatch, path: Path, payload: object) -> None:  # noqa: ANN001
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(wafer_policy, "WAFER_MANIFEST_PATH", path)
    wafer_policy._wafer_zdr_index.cache_clear()


def test_wafer_zdr_policy_indexes_canonical_and_native_ids(
    tmp_path: Path, monkeypatch
) -> None:  # noqa: ANN001
    path = tmp_path / "wafer.json"
    _set_manifest(
        monkeypatch,
        path,
        {
            "models": [
                {
                    "id": "moonshotai/kimi-k3",
                    "upstream_id": "Kimi-K3",
                    "zdr_supported": True,
                },
                {
                    "id": "moonshotai/kimi-k2.6",
                    "upstream_id": "Kimi-K2.6",
                    "zdr_supported": False,
                },
            ]
        },
    )

    assert wafer_policy.wafer_zdr_support("moonshotai/kimi-k3") is True
    assert wafer_policy.wafer_zdr_support("Kimi-K3") is True
    assert wafer_policy.wafer_zdr_support("Kimi-K2.6") is False
    assert wafer_policy.wafer_zdr_support("unknown/model") is None


def test_wafer_zdr_policy_fails_closed_on_invalid_manifest(
    tmp_path: Path, monkeypatch
) -> None:  # noqa: ANN001
    path = tmp_path / "wafer.json"
    _set_manifest(
        monkeypatch,
        path,
        {
            "models": [
                {
                    "id": "moonshotai/kimi-k3",
                    "upstream_id": "Kimi-K3",
                    "zdr_supported": "true",
                }
            ]
        },
    )

    assert wafer_policy.wafer_zdr_support("moonshotai/kimi-k3") is None
