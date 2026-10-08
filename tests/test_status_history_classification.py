import datetime as dt

import pytest

from trusted_router.storage_models import SyntheticProbeSample, SyntheticRollup
from trusted_router.synthetic import status

NOW = dt.datetime(2026, 10, 7, 12, tzinfo=dt.UTC)


def rollup(period: str, up: int, down: int, trust: int = 0) -> SyntheticRollup:
    return SyntheticRollup(
        id="history", period=period, period_start="2026-10-07T11:00:00Z",
        component="api-us", target="us-central1", probe_type="tls_health",
        monitor_region="us-central1", target_region="us-central1",
        sample_count=up + down + trust, up_count=up, down_count=down,
        trust_degraded_count=trust, last_checked_at="2026-10-07T11:59:00Z",
    )


@pytest.mark.parametrize("period", ["day", "month"])
@pytest.mark.parametrize(
    ("up", "down", "trust", "expected"),
    [(9998, 2, 0, "degraded"), (10, 1, 0, "degraded"),
     (0, 1, 0, "down"), (0, 2, 0, "down"), (10, 0, 0, "up"),
     (10, 2, 1, "trust_degraded"), (0, 0, 0, "unknown")],
)
def test_history_distinguishes_partial_failures_from_outages(
    period: str, up: int, down: int, trust: int, expected: str,
) -> None:
    row = status._rollup_history([rollup(period, up, down, trust)], period=period)[0]
    assert row["status"] == expected
    assert row["groups"][0]["status"] == expected
    assert row["sample_count"] == up + down + trust
    total = up + down + trust
    assert row["uptime_percent"] == (round(100 * up / total, 4) if total else 0)


def test_window_classification_preserves_failure_counts_and_burn_rate() -> None:
    result = status._slo_window([], [rollup("hour", 9998, 2)], now=NOW, seconds=86400)
    assert result["overall_status"] == "degraded"
    assert result["status_counts"] == {"up": 9998, "down": 2, "degraded": 0,
                                       "routing_degraded": 0, "trust_degraded": 0, "unknown": 0}
    assert result["uptime_percent"] == 99.98
    assert result["bad_count"] == 2
    assert result["burn_rate"] == round(0.0002 / status.SLO_ERROR_BUDGET_FRACTION, 2)


def test_raw_and_stored_history_have_identical_classification() -> None:
    samples = [SyntheticProbeSample(
        id=str(i), probe_type="tls_health", target="us-central1", target_url="https://example.test",
        monitor_region="us-central1", target_region="us-central1", status=value,
        created_at="2026-10-07T11:59:00Z",
    ) for i, value in enumerate(["up"] * 10 + ["down"] * 2)]
    raw = status._rollup(samples)
    stored = status._rollup_from_rollups([rollup("hour", 10, 2)])
    assert raw["overall_status"] == stored["overall_status"] == "degraded"
    assert raw["groups"][0]["uptime_percent"] == stored["groups"][0]["uptime_percent"]


def test_current_outage_rule_remains_strict() -> None:
    assert status._aggregate_status_counts({"up": 10000, "down": 2}) == "down"
