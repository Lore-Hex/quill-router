from datetime import UTC, datetime, timedelta

import pytest

from scripts.pricing.base import ModelPrice
from scripts.pricing.providers import upstage
from trusted_router.provider_manifest_policy import (
    EXPIRED_PROVIDER_MANIFEST,
    _provider_manifest_generated_deadline,
)

SCHEDULE = """
2026-08-05T22:00:00Z|2026-08-11T00:00:00Z|free
2026-08-11T00:00:00Z|2026-09-11T00:00:00Z|input=0.03|cached=0.006|output=0.12
2026-09-11T00:00:00Z|2026-10-10T00:00:00Z|input=0.09|cached=0.018|output=0.36
"""


def document(schedule: str = SCHEDULE) -> str:
    rates = "".join(
        f'<div class="pricing-feature-v2"><div data-rate="{axis}">{price}</div>'
        '<div data-rate-unit="">1M tokens</div></div>'
        for axis, price in (("input", "0.30"), ("cached", "0.06"), ("output", "1.20"))
    )
    return (
        '<div class="pricing-card-v2"><h4>Solar Pro 4</h4>'
        f'{rates}<div data-promo="schedule">{schedule}</div></div>'
    )


@pytest.mark.parametrize(
    ("date", "prompt", "cached", "completion", "deadline"),
    [
        ("2026-08-06", 0, 0, 0, "2026-08-11"),
        ("2026-09-10", 30_000, 6_000, 120_000, "2026-09-11"),
        ("2026-09-11", 90_000, 18_000, 360_000, "2026-10-10"),
        ("2026-09-14", 90_000, 18_000, 360_000, "2026-10-10"),
        ("2026-10-10", 300_000, 60_000, 1_200_000, None),
    ],
)
def test_named_rates_and_exact_promotion_boundaries(date, prompt, cached, completion, deadline):
    now = datetime.fromisoformat(date).replace(tzinfo=UTC)
    prices, valid_until = upstage._parse_pricing_document(document(), now=now)
    assert prices == {
        "upstage/solar-pro4": ModelPrice(prompt, completion, prompt_cached_micro_per_m=cached)
    }
    expected = datetime.fromisoformat(deadline).replace(tzinfo=UTC) if deadline else None
    assert valid_until == expected


@pytest.mark.parametrize(
    "schedule",
    [
        "broken",
        "2026-09-11|2026-10-10|free",
        "2026-10-10T00:00:00Z|2026-09-11T00:00:00Z|free",
        "2026-09-11T00:00:00Z|2026-10-10T00:00:00Z|input=0.09|output=0.36",
        "2026-09-11T00:00:00Z|2026-10-10T00:00:00Z|input=NaN|cached=0.01|output=0.36",
        "2026-09-11T00:00:00Z|2026-10-10T00:00:00Z|input=0.09|cached=0.1|output=0.36",
        SCHEDULE + "2026-09-12T00:00:00Z|2026-10-11T00:00:00Z|free",
    ],
)
def test_malformed_schedule_fails_closed(schedule):
    with pytest.raises(RuntimeError):
        upstage._parse_pricing_document(document(schedule), now=datetime(2026, 9, 14, tzinfo=UTC))


@pytest.mark.parametrize("replacement", ["1K tokens", "1M images", ""])
def test_wrong_unit_fails_closed(replacement):
    with pytest.raises(RuntimeError, match="unit"):
        upstage._parse_pricing(document().replace("1M tokens", replacement))


def test_empty_template_schedule_uses_regular_rates():
    prices, deadline = upstage._parse_pricing_document(document(""), now=datetime(2026, 9, 14, tzinfo=UTC))
    assert prices["upstage/solar-pro4"] == ModelPrice(300_000, 1_200_000, prompt_cached_micro_per_m=60_000)
    assert deadline is None


@pytest.mark.parametrize("value", [None, 123, "nonsense", "2026-09-15"])
def test_invalid_promotion_expiry_fails_closed(value):
    raw = {"generated_at": "2026-09-14T00:00:00Z", "pricing_valid_until": value}
    assert _provider_manifest_generated_deadline(raw, 14) == EXPIRED_PROVIDER_MANIFEST


def test_promotion_expiry_can_only_shorten_manifest_lifetime():
    generated = datetime(2026, 9, 14, tzinfo=UTC)
    raw = {"generated_at": generated.isoformat()}
    assert _provider_manifest_generated_deadline(raw, 14) == generated + timedelta(days=14)
    raw["pricing_valid_until"] = (generated + timedelta(days=1)).isoformat()
    assert _provider_manifest_generated_deadline(raw, 14) == generated + timedelta(days=1)
    raw["pricing_valid_until"] = (generated + timedelta(days=30)).isoformat()
    assert _provider_manifest_generated_deadline(raw, 14) == generated + timedelta(days=14)


def test_writer_persists_and_clears_promotion_expiry(monkeypatch, tmp_path):
    import json

    path = tmp_path / "upstage.json"
    path.write_text('{"models": []}')
    monkeypatch.setattr(upstage.CATALOG, "manifest_path", path)
    monkeypatch.setattr(upstage.CATALOG, "write_provider_manifest", lambda _: ["written"])
    deadline = datetime(2026, 10, 10, tzinfo=UTC)
    monkeypatch.setattr(upstage, "_PRICING_VALID_UNTIL", deadline)
    assert upstage.write_provider_manifest(None) == ["written"]
    assert json.loads(path.read_text())["pricing_valid_until"] == deadline.isoformat()
    monkeypatch.setattr(upstage, "_PRICING_VALID_UNTIL", None)
    upstage.write_provider_manifest(None)
    assert "pricing_valid_until" not in json.loads(path.read_text())
