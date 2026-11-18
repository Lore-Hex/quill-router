from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.providers import xiaomi
from trusted_router import provider_lifecycle

_CUTOFF = datetime(2026, 9, 7, 16, 0, tzinfo=UTC)
_ULTRASPEED = "xiaomi/mimo-v2.5-pro-ultraspeed"
_ULTRASPEED_UPSTREAM = "mimo-v2.5-pro-ultraspeed"
_PRO = "xiaomi/mimo-v2.5-pro"


def test_xiaomi_ultraspeed_cutoff_is_the_announced_local_date_and_spares_pro() -> None:
    # The cutoff itself, scope and the refresh and catalog paths are checked
    # for every retirement in tests/test_provider_retirements_generic.py.
    assert provider_lifecycle.XIAOMI_MIMO_V25_PRO_ULTRASPEED_RETIREMENT_AT == _CUTOFF
    assert not provider_lifecycle.provider_model_retired(
        "xiaomi",
        _PRO,
        "mimo-v2.5-pro",
        at=_CUTOFF,
    )


def test_xiaomi_manifest_records_retirement_without_inventing_replacement() -> None:
    rows = {
        row["id"]: row
        for row in json.loads(xiaomi.MANIFEST_PATH.read_text(encoding="utf-8"))["models"]
    }

    assert rows[_ULTRASPEED]["retirement_at"] == "2026-09-07T16:00:00Z"
    assert "replacement_model_id" not in rows[_ULTRASPEED]


def test_xiaomi_manifest_refresh_restores_retirement_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = tmp_path / "xiaomi.json"
    raw = json.loads(xiaomi.MANIFEST_PATH.read_text(encoding="utf-8"))
    ultraspeed = next(row for row in raw["models"] if row["id"] == _ULTRASPEED)
    ultraspeed.pop("retirement_at", None)
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr(xiaomi, "MANIFEST_PATH", manifest_path)

    xiaomi.write_provider_manifest(
        ProviderPricingResult(
            slug="xiaomi",
            prices={
                row["id"]: ModelPrice(
                    row["input_token_price_per_m"],
                    row["output_token_price_per_m"],
                    prompt_cached_micro_per_m=row.get("cached_input_token_price_per_m"),
                )
                for row in raw["models"]
            },
            source="fixture",
            fetched_url=xiaomi.URL,
        )
    )

    refreshed = json.loads(manifest_path.read_text(encoding="utf-8"))
    refreshed_ultraspeed = next(row for row in refreshed["models"] if row["id"] == _ULTRASPEED)
    assert refreshed_ultraspeed["retirement_at"] == "2026-09-07T16:00:00Z"
